"""Host allowances expire even while the caller leaves a prompt unanswered."""

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from .support import _VOIDRUNNER_PATH, _world_with_seed, _drain_until, vr
from .test_voidrunner_dropfiles import drop_file
from .test_voidrunner_socket import SocketDoor
from .test_voidrunner_style import render
from tools.ansi_to_html import Screen


@pytest.mark.parametrize("info,seconds", [
    ({}, None), ({"minutes_left": 5}, None),
    ({"drop_file_type": "DOOR32.SYS", "minutes_left": 5}, 300),
    ({"drop_file_type": "DOOR.SYS", "minutes_left": 2}, 120),
    ({"session_limit_seconds": 3.5}, 3.5),
    ({"session_limit_seconds": 0}, 0),
    ({"session_limit_seconds": 90, "minutes_left": 5, "drop_file_type": "DOOR32.SYS"}, 90),
    *[({"session_limit_seconds": value}, None) for value in
      (None, True, "120", -1, float("nan"), float("inf"), 10**1000)],
])
def test_metadata_allowance(info, seconds):
    clock = vr._session_clock(info, 10)
    if seconds is None:
        assert clock is None
    else:
        assert clock.deadline == 10 + seconds
        assert clock.exit_at == 10 + seconds - min(1, seconds / 10)


def test_warning_preserves_partial_prompt_and_only_appears_once(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(vr.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 40)
    clock = vr._SessionClock(200, 0)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        clock.check(warn=True)
        assert not output.getvalue()
        now[0] = 20
        clock.check(warn=True)
        clock.check(warn=True)
    assert output.getvalue().count("Time low:") == 1
    terminal = Screen(40, 12)
    terminal.feed("\x1b[8;1HQuantity: 12")
    before = terminal.row, terminal.col
    terminal.feed(output.getvalue())
    assert (terminal.row, terminal.col) == before
    terminal.feed("3")
    assert "".join(cell[0] for cell in terminal.cells[7]).startswith("Quantity: 123")
    assert "".join(cell[0] for cell in terminal.cells[0]).startswith("Time low:")
    now[0] = 199
    with pytest.raises(vr._SessionExpired):
        clock.check()


def test_no_limit_preserves_blocking_read(monkeypatch):
    monkeypatch.setattr(vr, "_SESSION_CLOCK", None)
    waits = []
    def read(timeout):
        waits.append(timeout)
        return b"Q"
    assert vr._session_read(read, None) == b"Q"
    assert waits == [None]


@pytest.mark.parametrize("prefix", [b"", b"\x1b[", b"\xc3"])
def test_deadline_interrupts_idle_or_partial_keys(monkeypatch, prefix):
    now = [0.0]
    monkeypatch.setattr(vr.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(vr, "_SESSION_CLOCK", vr._SessionClock(2, 0))
    pending = bytearray(prefix)
    def read(timeout):
        if pending:
            return bytes([pending.pop(0)])
        assert timeout is not None
        now[0] += timeout
        return None
    decoder = vr._DoorInput(read)
    with pytest.raises(vr._SessionExpired):
        for _ in range(10):
            decoder.read_key()
    assert now[0] == pytest.approx(1.8)


@pytest.mark.parametrize("width,height", [(80, 24), (40, 12)])
@pytest.mark.parametrize("style", ["basic", "mono", "plain"])
def test_time_badge_survives_narrow_deck_pagination(monkeypatch, width, height, style):
    clock = vr._SessionClock(150, time.monotonic())
    monkeypatch.setattr(vr, "_SESSION_CLOCK", clock)
    output = render("deck", monkeypatch, width, height, style)
    assert "LOW TIME" in vr.ANSI_ESCAPE_RE.sub("", output)
    terminal = Screen(width, height)
    terminal.feed(output)
    assert any("LOW TIME" in "".join(cell[0] for cell in row) for row in terminal.cells)
    assert all(vr._visible_width(line) <= width for line in output.split("\r\n"))


def _seed(directory):
    world = _world_with_seed(42)
    world.save.display_style = "plain"
    vr.write_save(directory / "saves", 77, world.save)
    return world


@pytest.mark.parametrize("completed", [False, True])
def test_real_stdio_deadline_saves_purchase_and_releases_lease(tmp_path, completed):
    world = _seed(tmp_path)
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester", "session_limit_seconds": 3}))
    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path / "saves"),
               NETBBS_DOOR_INFO=str(info), DOOR_SPLASH="0")
    with subprocess.Popen([sys.executable, str(_VOIDRUNNER_PATH)], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) as proc:
        output = bytearray()
        reader = threading.Thread(target=_drain_until, args=(proc.stdout, output, (), ()))
        reader.start()
        try:
            proc.stdin.write(b"MAP1\r" if completed else b"MAP1\x1b[")
            proc.stdin.flush()
            # Keep stdin open: EOF must not be what ends this session.
            proc.wait(timeout=12)
        finally:
            if proc.poll() is None:
                proc.kill()
            reader.join(timeout=12)
        _, error = proc.communicate()
    assert proc.returncode == 0 and not error
    assert b"Time low:" in output and b"Career saved." in output
    saved, _, _ = vr.load_or_create_save(tmp_path / "saves", 77, "Tester")
    assert saved.cargo == ({"food": 1} if completed else {})
    assert (saved.pilot.credits < world.save.pilot.credits) == completed
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}))
    resumed = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=b"Q",
                             capture_output=True, env=env, timeout=12)
    assert resumed.returncode == 0 and b"Fly safe, Tester" in resumed.stdout
    assert b"Time low:" not in resumed.stdout and b"LOW TIME" not in resumed.stdout
    loaded, _, _ = vr.load_or_create_save(tmp_path / "saves", 77, "Tester")
    assert loaded.cargo == saved.cargo and loaded.pilot.credits == saved.pilot.credits


# Classic drop files only represent whole minutes. Shorten their parsed limit
# inside this subprocess while retaining the real inherited-socket I/O path.
SOCKET_LIMIT = '''
import runpy, sys
main = runpy.run_path(sys.argv[1])["main"]
game = main.__globals__
parse = game["_load_door_info"]
def info(*args):
    result = parse(*args)
    result["session_limit_seconds"] = 2
    return result
game["_load_door_info"] = info
'''


def test_socket_idle_expiry_checkpoints_and_keeps_host_connection(tmp_path):
    _seed(tmp_path)
    door = SocketDoor(tmp_path, script=SOCKET_LIMIT + '\nsys.exit(main(sys.argv[2]))\n')
    try:
        door.finish()
        assert b"Time low:" in door.output and b"Career saved." in door.output
        door.host.sendall(b"Back at the BBS")
        assert door.peer.recv(100) == b"Back at the BBS"
        vr.load_or_create_save(tmp_path / "saves", 77, "Tester")
    finally:
        door.close()


@pytest.mark.parametrize("fail_on", [1, 2])
def test_save_failure_is_never_retried_or_reported_as_success_on_expiry(tmp_path, fail_on):
    _seed(tmp_path)
    script = SOCKET_LIMIT + f'''
persist = game["persist"]
attempts = 0
def fail(*args):
    global attempts
    attempts += 1
    if attempts == {fail_on}:
        raise OSError("disk full")
    persist(*args)
game["persist"] = fail
result = main(sys.argv[2])
assert attempts == {fail_on}, attempts
sys.exit(result)
'''
    door = SocketDoor(tmp_path, script=script)
    try:
        door.finish(expected=1)
        assert b"Save failed" in door.output and b"Career saved" not in door.output
    finally:
        door.close()


def test_expiry_during_splash_hold(monkeypatch):
    clock = vr._SessionClock(0, time.monotonic())
    monkeypatch.setattr(vr, "_SESSION_CLOCK", clock)
    monkeypatch.setattr(vr, "_INPUT_READER", vr._DoorInput(lambda timeout: None))
    monkeypatch.setattr(vr, "_RESIZE_PENDING", False)
    with pytest.raises(vr._SessionExpired):
        vr._splash_hold()


def test_expired_new_career_exits_without_creating_a_save(tmp_path):
    path = drop_file(tmp_path, changes={1: "0", 9: "0"})
    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path / "saves"), DOOR_SPLASH="0")
    env.pop("NETBBS_DOOR_INFO", None)
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH), path], input=b"Ace\rYQ",
                            capture_output=True, env=env, timeout=12)
    assert result.returncode == 0 and not result.stderr
    assert b"Time limit reached" in result.stdout and b"Career saved" not in result.stdout
    assert not (tmp_path / "saves" / "77.json").exists()
