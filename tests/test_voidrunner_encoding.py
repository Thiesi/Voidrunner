"""The CP437 wire boundary, independently of color and saved Unicode state."""

import ast
import contextlib
import io
import json
import os
import subprocess
import sys

import pytest

from .support import _VOIDRUNNER_PATH, _world_with_seed, vr
from .test_voidrunner_dropfiles import drop_file
from .test_voidrunner_style import PRESETS, SCREENS, SIZES, render


@pytest.fixture(autouse=True)
def encoding_state(monkeypatch):
    monkeypatch.delenv("NETBBS_DOOR_INFO", raising=False)
    monkeypatch.setattr(vr, "_OUTPUT_ENCODING", "utf-8")
    monkeypatch.setattr(vr, "_DEFAULT_ENCODING", "utf-8")
    monkeypatch.setattr(vr, "_INPUT_READER", None)


def test_all_authored_glyphs_have_explicit_cp437_forms():
    # Covers artwork in every screen, including branches a sample career never
    # reaches. Do not let the fallback for arbitrary user text mask new artwork.
    tree = ast.parse(_VOIDRUNNER_PATH.read_text(encoding="utf-8"))
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
                  and node.body and isinstance(node.body[0], ast.Expr)
                  and isinstance(node.body[0].value, ast.Constant)
                  and isinstance(node.body[0].value.value, str)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            node.value.translate(vr._CP437_TRANSLATION).encode("cp437", "strict")


@pytest.mark.parametrize("style", PRESETS)
@pytest.mark.parametrize("width,height", SIZES)
@pytest.mark.parametrize("screen", sorted(SCREENS))
def test_screens_render_as_strict_cp437_without_replacement(monkeypatch, screen, width, height, style):
    # Reject unmapped screen artwork before the fallback for arbitrary names.
    convert = vr._cp437_text
    def strict(text):
        expected = text.translate(vr._CP437_TRANSLATION)
        expected.encode("cp437", "strict")
        assert convert(text) == expected
        return expected
    monkeypatch.setattr(vr, "_cp437_text", strict)
    monkeypatch.setattr(vr, "_OUTPUT_ENCODING", "cp437")
    actual = render(screen, monkeypatch, width, height, style)
    assert actual.strip()
    assert actual.encode("cp437").decode("cp437") == actual
    for line in actual.split("\r\n"):
        assert vr._visible_width(vr._ANSI_RE.sub("", line)) <= width


@pytest.mark.parametrize("width,height", SIZES)
@pytest.mark.parametrize("style", ["auto", "basic", "mono"])
def test_every_splash_frame_has_a_strict_cp437_mapping(monkeypatch, width, height, style):
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
    for frame in vr.splash_frames(width, height, {"handle": "Jäger"}):
        frame.translate(vr._CP437_TRANSLATION).encode("cp437", "strict")


@pytest.mark.parametrize("style", PRESETS)
def test_output_writes_cp437_bytes_and_preserves_terminal_controls(monkeypatch, style):
    raw = io.BytesIO()
    terminal = io.TextIOWrapper(raw, encoding="utf-8", newline="")
    monkeypatch.setattr(vr.sys, "stdout", terminal)
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
    vr.apply_display_encoding("cp437")
    vr.out("\x1b[2J\x1b[H\x1b[31m╭─┤ Jäger · → ▃\x1b[0m\r\n")
    result = raw.getvalue()
    assert result.startswith(b"\x1b[2J\x1b[H") and result.endswith(b"\r\n")
    assert b"J\x84ger" in result
    assert b"?" not in result
    if style != "plain":
        assert "┌─┤".encode("cp437") in result
    if style in ("mono", "plain"):
        assert b"\x1b[31m" not in result


def test_user_text_fallback_preserves_width_and_saved_name():
    text = "Jäger Łódź 界 🚀 e\u0301"
    mapped = vr._cp437_text(text)
    assert mapped.startswith("Jäger")
    assert "?" not in mapped
    assert vr._visible_width(mapped) == vr._visible_width(text)
    mapped.encode("cp437", "strict")


def test_every_cp437_high_byte_is_text_not_a_c1_control():
    for value in range(128, 256):
        char = bytes([value]).decode("cp437")
        source = io.BytesIO(bytes([value]) + b"Z")
        reader = vr._DoorInput(lambda timeout: source.read(1), "cp437")
        assert reader.read_key() == (char if char.isprintable() else vr.IGNORED_KEY)
        assert reader.read_key() == "Z"


@pytest.mark.parametrize("sequence", [b"\x1b[A", b"\x1bOF", b"\x1b[15~",
    b"\x1b]0;title\x07", b"\x1b[200~BUY\x1b[201~", b"\x1b\x84"])
def test_cp437_does_not_turn_escape_sequences_into_menu_commands(sequence):
    source = io.BytesIO(sequence + b"Z")
    reader = vr._DoorInput(lambda timeout: source.read(1), "cp437")
    assert reader.read_key() == vr.IGNORED_KEY
    assert reader.read_key() == "Z"


def test_saved_encoding_is_additive_validated_and_preserved_by_retirement():
    save = _world_with_seed(42).save
    data = save.to_dict()
    del data["display_encoding"]
    assert vr.SaveData.from_dict(data).display_encoding == "auto"
    for encoding in vr.DISPLAY_ENCODINGS:
        save.display_encoding = encoding
        assert vr.SaveData.from_dict(save.to_dict()).display_encoding == encoding
        assert vr.retire_pilot(save).display_encoding == encoding
    for invalid in (None, [], 1, "latin1"):
        data["display_encoding"] = invalid
        with pytest.raises(vr.ResumeError):
            vr.SaveData.from_dict(data)
        with pytest.raises(ValueError):
            vr.apply_display_encoding(invalid)


@pytest.mark.parametrize("netbbs", [False, True])
def test_display_options_commits_encoding_and_netbbs_keeps_utf8(monkeypatch, netbbs):
    if netbbs:
        monkeypatch.setenv("NETBBS_DOOR_INFO", "host.json")
    world = _world_with_seed(42)
    saved = []
    world._checkpoint = lambda current: saved.append(current.save.display_encoding)
    keys = iter(["E", "E", "B"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "basic")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        vr.screen_display_options(vr.Palette(False), world)
    if netbbs:
        assert saved == [] and vr._OUTPUT_ENCODING == "utf-8"
        assert "managed by NetBBS" in output.getvalue()
    else:
        assert saved == ["utf-8", "cp437"]
        assert world.save.display_encoding == vr._OUTPUT_ENCODING == "cp437"


def test_netbbs_overrides_a_saved_cp437_preference(monkeypatch):
    monkeypatch.setenv("NETBBS_DOOR_INFO", "host.json")
    raw = io.BytesIO()
    terminal = io.TextIOWrapper(raw, encoding="ascii", newline="")
    monkeypatch.setattr(vr.sys, "stdout", terminal)
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    vr.apply_display_encoding("cp437")
    vr.out("Jäger →")
    assert raw.getvalue() == "Jäger →".encode("utf-8")


def test_changing_encoding_updates_the_existing_input_decoder(monkeypatch):
    stream = io.BytesIO(b"\x84" + "ö".encode("utf-8"))
    reader = vr._DoorInput(lambda timeout: stream.read(1))
    monkeypatch.setattr(vr, "_INPUT_READER", reader)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.apply_display_encoding("cp437")
        assert reader.read_key() == "ä"
        vr.apply_display_encoding("utf-8")
        assert reader.read_key() == "ö"


def test_failed_encoding_save_keeps_the_active_wire_encoding(monkeypatch):
    world = _world_with_seed(42)
    world.save.display_encoding = "utf-8"
    def fail(current):
        raise OSError("disk full")
    world._checkpoint = fail
    monkeypatch.setattr(vr, "read_key", lambda: "E")
    output = io.StringIO()
    with contextlib.redirect_stdout(output), pytest.raises(vr.SaveError):
        vr.screen_display_options(vr.Palette(False), world)
    assert vr._OUTPUT_ENCODING == "utf-8"
    assert "Encoding saved" not in output.getvalue()


def test_cp437_callsign_roundtrips_and_saved_utf8_overrides_the_drop_file(tmp_path):
    path = drop_file(tmp_path, changes={1: "0", 2: "0"})
    saves = tmp_path / "saves"
    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(saves))
    args = [sys.executable, str(_VOIDRUNNER_PATH), path]
    result = subprocess.run(args, input="Jäger\rYQ".encode("cp437"),
                            capture_output=True, env=env, timeout=60)
    assert result.returncode == 0 and not result.stderr
    assert "Jäger".encode("cp437") in result.stdout
    file = saves / "77.json"
    data = json.loads(file.read_text())
    assert data["pilot"]["handle"] == "Jäger"
    data["display_encoding"] = "utf-8"
    file.write_text(json.dumps(data))
    result = subprocess.run(args, input=b"Q", capture_output=True, env=env, timeout=60)
    assert result.returncode == 0 and not result.stderr
    assert "Fly safe, Jäger".encode("utf-8") in result.stdout
