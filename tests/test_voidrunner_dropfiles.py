"""Classic BBS launch metadata and compatibility with NetBBS launches."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from .support import _VOIDRUNNER_PATH, vr


FIXTURES = Path(__file__).parent / "fixtures" / "dropfiles"


@pytest.fixture(autouse=True)
def no_host_environment(monkeypatch):
    monkeypatch.delenv("NETBBS_DOOR_INFO", raising=False)
    monkeypatch.delenv("VOIDRUNNER_SAVE_DIR", raising=False)


def drop_file(tmp_path, kind="DOOR32.SYS", *, changes=None, count=None,
              newline="\n", filename=None, encoding="utf-8"):
    lines = (FIXTURES / kind).read_text().splitlines()
    for line, value in (changes or {}).items():
        lines[line - 1] = value
    if count is not None:
        lines = lines[:count]
    target = tmp_path / (filename or kind)
    target.write_bytes((newline.join(lines) + newline).encode(encoding))
    return str(target)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("filename", [None, "dOoR32.sYs", "caller.dat"])
def test_door32_golden_file(tmp_path, newline, filename):
    info = vr._load_door_info(drop_file(tmp_path, newline=newline, filename=filename))
    assert info == {
        "handle": "Starfox", "user_id": 77, "node_name": "Test BBS",
        "terminal_width": 80, "terminal_height": 24, "color_depth": "16",
        "display_style": "basic", "drop_file_type": "DOOR32.SYS",
        "minutes_left": 25, "comm_type": 2, "comm_handle": 1234, "emulation": 1,
    }


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("count,handle", [(31, "Test Pilot"), (52, "Starfox")])
@pytest.mark.parametrize("filename", [None, "dOoR.sYs", "caller.dat"])
def test_door_sys_golden_file(tmp_path, newline, count, handle, filename):
    info = vr._load_door_info(drop_file(tmp_path, "DOOR.SYS", newline=newline,
                                      count=count, filename=filename))
    assert info == {
        "handle": handle, "user_id": 77, "node_name": "Local",
        "terminal_width": 80, "terminal_height": 30, "color_depth": "16",
        "display_style": "basic", "drop_file_type": "DOOR.SYS",
        "minutes_left": 25, "comm_port": "COM1:", "emulation": "GR",
    }


@pytest.mark.parametrize("encoding", ["utf-8", "cp437"])
def test_alias_encoding_and_empty_alias_fallback(tmp_path, encoding):
    info = vr._load_door_info(drop_file(tmp_path, changes={7: "Jäger"}, encoding=encoding))
    assert info["handle"] == "Jäger"
    info = vr._load_door_info(drop_file(tmp_path, changes={7: ""}))
    assert info["handle"] == "Test Pilot"
    info = vr._load_door_info(drop_file(tmp_path, "DOOR.SYS", changes={36: ""}))
    assert info["handle"] == "Test Pilot"


@pytest.mark.parametrize("kind,line,values", [
    ("DOOR32.SYS", 10, ["0"]), ("DOOR.SYS", 20, ["NG", "7E"]),
])
def test_ascii_emulation_defaults_to_plain(tmp_path, kind, line, values):
    for value in values:
        info = vr._load_door_info(drop_file(tmp_path, kind, changes={line: value}))
        assert info["display_style"] == "plain"
        assert info["color_depth"] == "none"


@pytest.mark.parametrize("kind,count,changes", [
    ("DOOR32.SYS", 0, {}), ("DOOR32.SYS", 10, {}), ("DOOR.SYS", 30, {}),
    ("DOOR32.SYS", None, {1: "3"}), ("DOOR32.SYS", None, {2: "-1"}),
    ("DOOR32.SYS", None, {5: "0"}), ("DOOR32.SYS", None, {5: "-77"}),
    ("DOOR32.SYS", None, {5: "seventy-seven"}),
    ("DOOR32.SYS", None, {9: "-1"}), ("DOOR32.SYS", None, {10: "99"}),
    ("DOOR32.SYS", None, {7: "Pilot\x1b[31m"}),
    ("DOOR.SYS", None, {1: "junk"}), ("DOOR.SYS", None, {26: "0"}),
    ("DOOR.SYS", None, {21: "0"}), ("DOOR.SYS", None, {19: "oops"}),
    ("DOOR.SYS", None, {20: "bad"}),
])
def test_malformed_file_uses_complete_defaults_and_warns(tmp_path, capsys, kind, count, changes):
    info = vr._load_door_info(drop_file(tmp_path, kind, count=count, changes=changes))
    assert info["user_id"] == 0 and info["handle"] == "Guest"
    assert info["terminal_height"] == 24 and info["display_style"] == "basic"
    assert "comm_handle" not in info and "minutes_left" not in info
    warning = capsys.readouterr().err
    assert "drop file ignored" in warning and "Guest defaults" in warning
    assert "UNUSED_TEST_PASSWORD" not in warning


def test_missing_or_unknown_file_warns(tmp_path, capsys):
    for path in (tmp_path / "absent", tmp_path / "random.txt"):
        if path.name == "random.txt":
            path.write_text("not a drop file")
        assert vr._load_door_info(str(path))["user_id"] == 0
        assert "drop file ignored" in capsys.readouterr().err


@pytest.mark.parametrize("data", [None, {}, {"user_id": 19, "handle": "NetPilot",
    "terminal_width": 64, "terminal_height": 20, "color_depth": "truecolor",
    "session_limit_seconds": 900}, [], "broken"])
def test_netbbs_always_takes_precedence(tmp_path, monkeypatch, capsys, data):
    netbbs = tmp_path / "door_info.json"
    if data is not None:
        netbbs.write_text("invalid JSON" if data == "broken" else json.dumps(data))
    monkeypatch.setenv("NETBBS_DOOR_INFO", str(netbbs))
    info = vr._load_door_info(drop_file(tmp_path))
    assert info == vr._load_door_info()
    assert info["node_name"] == "NetBBS"
    assert info["color_depth"] in ("256", "truecolor")
    assert "comm_type" not in info and "display_style" not in info
    if isinstance(data, dict):
        assert data.items() <= info.items()
    assert not capsys.readouterr().err


def test_default_save_directory_migration_and_explicit_override(tmp_path, monkeypatch):
    monkeypatch.setattr(vr.Path, "home", lambda: tmp_path)
    current, old = tmp_path / ".voidrunner", tmp_path / ".netbbs" / "voidrunner_saves"
    assert vr._default_save_dir(standalone=True) == current
    assert vr._default_save_dir() == old
    old.mkdir(parents=True)
    assert vr._default_save_dir(standalone=True) == old
    current.mkdir()
    assert vr._default_save_dir(standalone=True) == current
    assert vr._default_save_dir() == old
    monkeypatch.setenv("NETBBS_DOOR_INFO", "host.json")
    assert vr._default_save_dir(standalone=True) == old
    assert vr._default_save_dir() == old
    monkeypatch.setenv("VOIDRUNNER_SAVE_DIR", str(tmp_path / "explicit"))
    assert vr._default_save_dir(standalone=True) == tmp_path / "explicit"
    assert vr._default_save_dir() == tmp_path / "explicit"


@pytest.mark.parametrize("standalone_exists", [False, True])
def test_backup_imports_find_legacy_careers_without_a_door_environment(tmp_path, monkeypatch, standalone_exists):
    # netbbs.backup runs in the host, with neither door environment variable set.
    from voidrunner import _default_save_dir, PilotBusy, maintenance_session

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    legacy = tmp_path / ".netbbs" / "voidrunner_saves"
    legacy.mkdir(parents=True)
    career = legacy / "77.json"
    career.write_text('{"backup_fixture": true}')
    if standalone_exists:
        standalone = tmp_path / ".voidrunner"
        standalone.mkdir()
        (standalone / "77.json").write_text('{"standalone_fixture": true}')
    directory = _default_save_dir()
    assert directory == legacy
    assert issubclass(PilotBusy, Exception)
    with maintenance_session(directory):
        assert json.loads((directory / "77.json").read_text()) == {"backup_fixture": True}


@pytest.mark.parametrize("current_exists,legacy_exists", [(False, False), (False, True), (True, True)])
def test_game_launch_uses_standalone_directory_policy(tmp_path, current_exists, legacy_exists):
    current, legacy = tmp_path / ".voidrunner", tmp_path / ".netbbs" / "voidrunner_saves"
    if current_exists:
        current.mkdir()
    if legacy_exists:
        legacy.mkdir(parents=True)
    path = drop_file(tmp_path, changes={1: "0", 2: "0"})
    script = r'''
import runpy, sys
from pathlib import Path
Path.home = classmethod(lambda cls: Path(sys.argv[3]))
main = runpy.run_path(sys.argv[1])["main"]
sys.exit(main(sys.argv[2]))
'''
    result = subprocess.run([sys.executable, "-c", script, str(_VOIDRUNNER_PATH), path, str(tmp_path)],
                            input=b"Ace\rYQ", capture_output=True, env=dict(os.environ), timeout=60)
    assert result.returncode == 0, result.stderr
    expected = legacy if legacy_exists and not current_exists else current
    assert json.loads((expected / "77.json").read_text())["pilot"]["handle"] == "Ace"
    other = current if expected == legacy else legacy
    assert not (other / "77.json").exists()


@pytest.mark.parametrize("kind", ["DOOR32.SYS", "DOOR.SYS"])
def test_command_line_creates_and_resumes_by_record_number(tmp_path, kind):
    path = drop_file(tmp_path, kind, changes={1: "0", 2: "0"} if kind == "DOOR32.SYS" else {})
    saves = tmp_path / "saves"
    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(saves))
    args = [sys.executable, str(_VOIDRUNNER_PATH), path]
    result = subprocess.run(args, input=b"Ace\rYQ", capture_output=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
    saved = json.loads((saves / "77.json").read_text())
    assert saved["pilot"]["handle"] == "Ace" and saved["display_style"] == "basic"
    assert b"38;5;" not in result.stdout and b"38;2;" not in result.stdout
    # The host alias changes; the career and its chosen preset still belong to 77.
    saved["display_style"] = "mono"
    (saves / "77.json").write_text(json.dumps(saved))
    drop_file(tmp_path, kind, changes={7 if kind == "DOOR32.SYS" else 36: "Renamed"})
    result = subprocess.run(args, input=b"Q", capture_output=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
    saved = json.loads((saves / "77.json").read_text())
    assert saved["pilot"]["handle"] == "Ace" and saved["display_style"] == "mono"
    assert b"Docking clamps engaged" in result.stdout


def test_malformed_command_line_drop_file_does_not_crash(tmp_path):
    path = drop_file(tmp_path, count=3)
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH), path], input=b"",
                            capture_output=True, timeout=60,
                            env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path / "saves")))
    assert result.returncode == 0
    assert b"drop file ignored" in result.stderr
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize("netbbs", [False, True])
def test_resize_reloads_the_launch_metadata_source(tmp_path, netbbs):
    path = drop_file(tmp_path, "DOOR.SYS")
    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path / "saves"))
    if netbbs:
        host = tmp_path / "door_info.json"
        host.write_text(json.dumps({"terminal_width": 64, "terminal_height": 30}))
        env["NETBBS_DOOR_INFO"] = str(host)
    script = r'''
import json, os, runpy, sys
from pathlib import Path
main = runpy.run_path(sys.argv[1])["main"]
game = main.__globals__
title = game["screen_title"]
def resize_during_launch(*args):
    width = 64 if os.environ.get("NETBBS_DOOR_INFO") else 80
    assert (game["_OUTPUT_WIDTH"], game["_OUTPUT_HEIGHT"]) == (width, 30)
    game["_note_resize"]()
    assert game["take_resize"]() is False, "unchanged metadata changed the geometry"
    assert (game["_OUTPUT_WIDTH"], game["_OUTPUT_HEIGHT"]) == (width, 30)
    if os.environ.get("NETBBS_DOOR_INFO"):
        Path(os.environ["NETBBS_DOOR_INFO"]).write_text(json.dumps({
            "terminal_width": width, "terminal_height": 40}))
    else:
        path = Path(sys.argv[2])
        lines = path.read_text().splitlines()
        lines[20] = "40"
        path.write_text("\n".join(lines) + "\n")
    game["_note_resize"]()
    assert game["take_resize"]() is True, "rewritten metadata was not reloaded"
    assert (game["_OUTPUT_WIDTH"], game["_OUTPUT_HEIGHT"]) == (width, 40)
    title(*args)
game["screen_title"] = resize_during_launch
sys.exit(main(sys.argv[2]))
'''
    result = subprocess.run([sys.executable, "-c", script, str(_VOIDRUNNER_PATH), path],
                            input=b"Ace\rYQ", capture_output=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
