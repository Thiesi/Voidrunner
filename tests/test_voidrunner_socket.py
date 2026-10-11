"""Inherited DOOR32 sockets, telnet framing, and career durability."""

import contextlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import time

import pytest

from .support import _VOIDRUNNER_PATH, _world_with_seed, vr
from .test_voidrunner_dropfiles import drop_file


@pytest.fixture
def wire():
    local, peer = socket.socketpair()
    transport = vr._SocketTransport(local.detach())
    try:
        yield transport, peer
    finally:
        transport.close()
        peer.close()


@pytest.mark.parametrize("command", [b"\xff\xfbM", b"\xff\xfcQ", b"\xff\xfdP", b"\xff\xfeY",
    b"\xff\xf1", b"\xff\xfa\x18MYP\xff\xffQ\xff\xf0"])
def test_telnet_commands_between_utf8_bytes_and_arrow_bytes(wire, command):
    transport, peer = wire
    peer.sendall(b"\xc3" + command + b"\xa4\x1b[" + command + b"AQ")
    reader = vr._DoorInput(transport, telnet=True)
    assert reader.read_key() == "ä"
    assert reader.read_key() == vr.IGNORED_KEY
    assert reader.read_key() == "Q"
    peer.settimeout(0.02)
    with pytest.raises(TimeoutError):
        peer.recv(1)  # the door did not negotiate options with the caller


def test_partial_telnet_sequences_survive_timeouts(wire):
    transport, peer = wire
    for fragment in (b"\xff", b"\xfa", b"\x18M", b"\xff\xff", b"Q", b"\xff"):
        peer.sendall(fragment)
        assert transport(0.005) is None
    peer.sendall(b"\xf0Z\xff\xff")
    assert transport(0.1) == b"Z"
    assert transport(0.1) == b"\xff"


@pytest.mark.parametrize("ending", [b"\r\0", b"\r\n", b"\r\xff\xfb\x01\0"])
def test_telnet_enter_is_one_key_and_does_not_swallow_the_next(wire, ending):
    transport, peer = wire
    peer.sendall(b"A" + ending + b"\nQ")
    reader = vr._DoorInput(transport, telnet=True)
    assert [reader.read_key() for _ in range(4)] == ["A", "\r", "\n", "Q"]


def test_output_escapes_iac_after_encoding_and_input_keeps_cp437(wire, monkeypatch):
    transport, peer = wire
    monkeypatch.delenv("NETBBS_DOOR_INFO", raising=False)
    monkeypatch.setattr(vr, "_SOCKET_TRANSPORT", transport)
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "basic")
    monkeypatch.setattr(vr, "_OUTPUT_ENCODING", "cp437")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        vr.out("ä\u00a0┌\r\n")
    assert output.getvalue() == ""
    assert peer.recv(20) == b"\x84\xff\xff\xda\r\n"
    peer.sendall(b"\x84Q")
    reader = vr._DoorInput(transport, "cp437", telnet=True)
    assert [reader.read_key(), reader.read_key()] == ["ä", "Q"]


def test_socket_motion_peeks_without_swallowing_input(wire, monkeypatch):
    transport, peer = wire
    monkeypatch.setattr(vr, "_INPUT_READER", vr._DoorInput(transport, telnet=True))
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "basic")
    assert not vr.motion_interrupted()
    peer.sendall(b"Q")
    assert vr.motion_interrupted()
    assert vr._INPUT_READER.read_key() == "Q"


def test_closed_socket_becomes_eof_and_output_loss(wire):
    transport, peer = wire
    peer.close()
    assert transport.waiting(0.1)
    with pytest.raises(EOFError):
        vr._DoorInput(transport, telnet=True).read_key()
    with pytest.raises(vr._CarrierLost):
        transport.write(b"after disconnect")


def test_nonblocking_socket_and_partial_writes_keep_the_hosts_mode(wire):
    transport, peer = wire
    transport.socket.setblocking(False)
    real_socket = transport.socket
    class PartialSocket:
        def __getattr__(self, name):
            return getattr(real_socket, name)
        def send(self, data):
            return real_socket.send(data[:2])
    transport.socket = PartialSocket()
    assert transport(0.005) is None
    peer.sendall(b"Q")
    assert transport(0.1) == b"Q"
    transport.write(b"A\xffB\xffC")
    peer.settimeout(1)
    received = bytearray()
    while len(received) < 7:
        received.extend(peer.recv(7 - len(received)))
    assert received == b"A\xff\xffB\xff\xffC"
    assert not real_socket.getblocking()


@pytest.mark.parametrize("operation", ["read", "write"])
def test_connection_reset_becomes_clean_carrier_loss(wire, operation):
    transport, peer = wire
    real_socket = transport.socket
    class ResetSocket:
        def __getattr__(self, name):
            return getattr(real_socket, name)
        def recv(self, count):
            raise ConnectionResetError("caller disconnected")
        def send(self, data):
            raise ConnectionResetError("caller disconnected")
    transport.socket = ResetSocket()
    peer.sendall(b"ready")
    if operation == "read":
        assert transport(0.1) == b""
    else:
        with pytest.raises(vr._CarrierLost):
            transport.write(b"screen")
    assert transport.lost


def test_closing_door_handle_does_not_shutdown_host_connection():
    host, peer = socket.socketpair()
    try:
        transport = vr._SocketTransport(host.dup().detach())
        transport.close()
        host.sendall(b"Returned to the BBS")
        assert peer.recv(100) == b"Returned to the BBS"
    finally:
        host.close()
        peer.close()


class SocketDoor:
    def __init__(self, directory, *, script=None):
        host, self.peer = socket.socketpair()
        self.host = host  # the BBS keeps its own connection while the door runs
        host.set_inheritable(True)
        path = drop_file(directory, changes={2: str(host.fileno())})
        self.output = bytearray()
        self.peer.settimeout(0.1)
        env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(directory / "saves"))
        env.pop("NETBBS_DOOR_INFO", None)
        command = ([sys.executable, "-c", script, str(_VOIDRUNNER_PATH), path] if script else
                   [sys.executable, str(_VOIDRUNNER_PATH), path])
        inheritance = {"close_fds": False} if os.name == "nt" else {"pass_fds": (host.fileno(),)}
        try:
            self.proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, env=env, **inheritance)
        except BaseException:
            host.close()
            self.peer.close()
            raise

    def read_until(self, marker=None):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if marker is not None and marker in re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", self.output):
                return
            try:
                block = self.peer.recv(8192)
            except TimeoutError:
                if self.proc.poll() is not None:
                    break
                continue
            if not block:
                break
            self.output.extend(block)
        if marker is not None:
            pytest.fail(f"Socket prompt {marker!r} missing: {bytes(self.output)[-2000:]!r}")

    def finish(self, *, drain=True, expected=0):
        if drain:
            self.read_until()
        stdout, stderr = self.proc.communicate(timeout=20)
        assert self.proc.returncode == expected, stderr
        assert stdout == b"", "socket output leaked onto stdout"
        assert stderr == b""

    def close(self):
        self.host.close()
        self.peer.close()
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.communicate(timeout=10)


@pytest.fixture
def socket_door(tmp_path):
    sessions = []
    def launch(**kwargs):
        door = SocketDoor(tmp_path, **kwargs)
        sessions.append(door)
        return door
    yield launch
    for door in sessions:
        door.close()


def test_real_inherited_socket_launch_and_resume(socket_door, tmp_path):
    door = socket_door()
    door.peer.sendall(b"J\x84ger\r\0YQ")
    door.finish()
    assert b"Fly safe, J\x84ger" in door.output
    door.host.sendall(b"Back at the BBS")
    assert door.peer.recv(100) == b"Back at the BBS"
    path = tmp_path / "saves" / "77.json"
    data = json.loads(path.read_text())
    assert data["pilot"]["handle"] == "Jäger"
    data["display_encoding"] = "utf-8"
    path.write_text(json.dumps(data))
    door = socket_door()
    door.peer.sendall(b"Q")
    door.finish()
    assert "Fly safe, Jäger".encode() in door.output


@pytest.mark.parametrize("completed", [False, True])
def test_carrier_drop_during_purchase_preserves_the_checkpoint(socket_door, tmp_path, completed):
    save_dir = tmp_path / "saves"
    world = _world_with_seed(42)
    world.save.display_style = "plain"
    vr.write_save(save_dir, 77, world.save)
    door = socket_door()
    door.peer.sendall(b"MAP")
    door.read_until(b"Enter or Esc cancels")
    if completed:
        door.peer.sendall(b"1\r\0")
        door.read_until(b"Bought")
    door.peer.shutdown(socket.SHUT_RDWR)
    door.peer.close()
    door.finish(drain=False)
    saved, _, _ = vr.load_or_create_save(save_dir, 77, "Tester")
    assert saved.cargo == ({"food": 1} if completed else {})
    if completed:
        assert saved.pilot.credits < world.save.pilot.credits
    else:
        assert saved.pilot.credits == world.save.pilot.credits
    # A fresh process can reacquire the released pilot lease and resume once.
    door = socket_door()
    door.peer.sendall(b"Q")
    door.finish()
    resumed, _, _ = vr.load_or_create_save(save_dir, 77, "Tester")
    assert resumed.cargo == saved.cargo
    assert resumed.pilot.credits == saved.pilot.credits


def test_invalid_socket_handle_fails_without_starting_a_guest_game(tmp_path):
    path = drop_file(tmp_path, changes={2: str(2**63)})
    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path / "saves"))
    env.pop("NETBBS_DOOR_INFO", None)
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH), path],
                            input=b"", capture_output=True, env=env, timeout=20)
    assert result.returncode == 1
    assert b"cannot open inherited DOOR32 socket" in result.stderr
    assert b"Traceback" not in result.stderr and not result.stdout
    assert not (tmp_path / "saves").exists()


@pytest.mark.parametrize("save_fails", [False, True])
def test_output_disconnect_during_purchase_does_not_mask_save_failure(socket_door, tmp_path, save_fails):
    world = _world_with_seed(42)
    world.save.display_style = "plain"
    save_dir = tmp_path / "saves"
    vr.write_save(save_dir, 77, world.save)
    script = r'''
import runpy, socket, sys
main = runpy.run_path(sys.argv[1])["main"]
game = main.__globals__
transport = game["_SocketTransport"]
write = transport.write
def disconnect_at_ack(self, data):
    if b"Bought" in data or b"Save failed" in data:
        self.socket.shutdown(socket.SHUT_WR)
    write(self, data)
transport.write = disconnect_at_ack
'''
    if save_fails:
        script += r'''
persist = game["persist"]
attempts = 0
def fail_second_save(*args):
    global attempts
    attempts += 1
    if attempts == 2:
        raise OSError("disk full")
    persist(*args)
game["persist"] = fail_second_save
'''
    script += '\nresult = main(sys.argv[2])\n'
    if save_fails:
        script += 'assert attempts == 2, "failed purchase was retried on disconnect"\n'
    script += 'sys.exit(result)\n'
    door = socket_door(script=script)
    door.peer.sendall(b"MAP1\r\0")
    door.finish(expected=1 if save_fails else 0)
    saved, _, _ = vr.load_or_create_save(save_dir, 77, "Tester")
    assert saved.cargo == ({} if save_fails else {"food": 1})
    assert b"Bought" not in door.output
    if save_fails:
        assert saved.pilot.credits == world.save.pilot.credits


def test_netbbs_metadata_keeps_stdio_despite_a_socket_dropfile(tmp_path):
    path = drop_file(tmp_path, changes={2: str(2**63)})
    host = tmp_path / "door_info.json"
    host.write_text(json.dumps({"user_id": 88, "handle": "NetPilot"}))
    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path / "saves"), NETBBS_DOOR_INFO=str(host))
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH), path],
                            input=b"Ace\rYQ", capture_output=True, env=env, timeout=20)
    assert result.returncode == 0 and not result.stderr
    assert b"Fly safe, Ace" in result.stdout
    assert (tmp_path / "saves" / "88.json").exists()
    assert not (tmp_path / "saves" / "77.json").exists()
