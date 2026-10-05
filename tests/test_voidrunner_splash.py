"""The launch splash: what it draws, at which sizes, and when it draws nothing.

The splash is motion, so it lives under motion's rules (issue #493 §3): absent
from the presets that ask for less and with nobody watching, and over the
moment a key arrives. These pin the composition's bounds at the sizes the door
accepts and the input rules; how it *looks* is reviewed from rendered frames.
"""

from __future__ import annotations

import os
import re

import pytest

from .support import vr

INFO = {"node_name": "Harbor Lights", "handle": "Carrier"}
CUP = re.compile(r"\x1b\[(\d+);(\d+)H")


@pytest.fixture
def depth(monkeypatch):
    def choose(style: str, truecolor: bool) -> None:
        monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
        monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=truecolor))
    return choose


def _layout(width: int, height: int, handle: str = "Carrier") -> "vr._SplashLayout":
    return vr._SplashLayout(width, height, "Harbor Lights", handle)


@pytest.mark.parametrize("width,height", [(40, 12), (71, 19), (72, 20), (80, 24), (132, 50)])
def test_every_frame_fills_the_terminal_and_nothing_more(width, height, depth):
    depth("auto", True)
    layout = _layout(width, height)
    for frame in range(vr.SPLASH_FRAMES):
        cells = vr.splash_frame(layout, frame)
        assert len(cells) == height
        for row in cells:
            assert len(row) == width
            # A wide character's second column is a continuation, never a glyph.
            assert sum(max(1, vr._char_width(ch)) if ch else 0 for ch, _fg, _bg in row) <= width
        # Never the bottom-right cell, where a terminal may scroll.
        assert cells[height - 1][width - 1] == (" ", None, None)


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24), (132, 50)])
def test_the_cursor_never_leaves_the_terminal(width, height, depth):
    depth("auto", True)
    for frame in vr.splash_frames(width, height, INFO):
        for row, column in CUP.findall(frame):
            assert 1 <= int(row) <= height and 1 <= int(column) <= width


def test_the_composition_is_large_from_72_by_20_and_compact_below():
    assert _layout(72, 20).large and _layout(80, 24).large
    assert not _layout(71, 24).large and not _layout(80, 19).large
    # The compact face makes the word in 39 columns: the door's floor holds it.
    assert _layout(40, 12).logo_w == 39


def test_the_finished_card_names_the_node_and_the_pilot(depth):
    depth("auto", True)
    cells = vr.splash_frame(_layout(80, 24), vr.SPLASH_FRAMES - 1)
    text = "\n".join("".join(ch for ch, _fg, _bg in row) for row in cells)
    assert "TACTICAL DEEP-SPACE TRADING & EXPLORATION" in text
    assert "Harbor Lights" in text and "Carrier" in text and "48 Star Systems" in text


def test_a_wide_callsign_survives_at_the_floor(depth):
    depth("auto", True)
    handle = "星" * 16
    cells = vr.splash_frame(_layout(40, 12, handle), vr.SPLASH_FRAMES - 1)
    text = "".join(ch for row in cells for ch, _fg, _bg in row)
    assert handle in text


def test_caller_text_is_drawn_as_plain_characters():
    assert vr._splash_plain("\x1b[31mevil\x07\r\nnode") == "evil node"


def test_every_frame_writes_something(depth):
    """Tooling waits for a door to go quiet; a frame that writes nothing would
    read as the splash having finished while it is still holding the screen."""
    for style, truecolor in (("auto", True), ("auto", False), ("basic", False)):
        depth(style, truecolor)
        assert all(vr.splash_frames(80, 24, INFO)), style


def test_the_splash_stays_small_on_the_wire(depth):
    depth("auto", True)
    assert sum(len(frame.encode()) for frame in vr.splash_frames(80, 24, INFO)) < 42_000
    depth("basic", False)
    assert sum(len(frame.encode()) for frame in vr.splash_frames(80, 24, INFO)) < 20_000


def test_sixteen_colors_keep_the_planet_and_the_wordmark_in_color(depth):
    depth("basic", False)
    # A violet reads as magenta and the gold as yellow, never as grey.
    assert vr._splash_16((104, 64, 188)) in (5, 13)
    assert vr._splash_16((255, 200, 60)) in (3, 11)
    # The text roles keep the palette's own sixteen-color readings.
    assert vr._splash_16((127, 163, 191)) == 6


@pytest.mark.parametrize("style", ["fast", "mono", "plain"])
def test_presets_that_ask_for_less_get_no_splash(style, monkeypatch):
    written = []
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
    monkeypatch.setattr(vr, "out", written.append)
    assert vr.play_splash(INFO) is False
    assert written == []


def test_no_live_terminal_means_no_splash(monkeypatch):
    written = []
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    monkeypatch.setattr(vr, "_INPUT_READER", None)
    monkeypatch.setattr(vr, "out", written.append)
    assert vr.play_splash(INFO) is False
    assert written == []


@pytest.fixture
def live_terminal(monkeypatch):
    """A real pipe behind the door's own reader, so `waiting()` is a real peek."""
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb", buffering=0)
    reader = vr._DoorInput(vr._StdioBytes(stream))
    monkeypatch.setattr(vr, "_INPUT_READER", reader)
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 24)
    monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=True))
    monkeypatch.setattr(vr, "_RESIZE_PENDING", False)
    yield reader, write_fd
    os.close(write_fd)
    stream.close()


def test_a_key_ends_the_splash_and_is_consumed_whole(live_terminal, monkeypatch):
    reader, write_fd = live_terminal
    written, frames_seen = [], []
    real_pause = vr.motion_pause

    def pause(seconds):
        frames_seen.append(seconds)
        if len(frames_seen) == 3:
            # An arrow key and a letter: two input units, both for the splash.
            os.write(write_fd, b"\x1b[Ax")
        return real_pause(0.2 if len(frames_seen) == 3 else 0.0)

    monkeypatch.setattr(vr, "motion_pause", pause)
    monkeypatch.setattr(vr, "out", written.append)
    assert vr.play_splash(INFO) is True
    assert len(frames_seen) == 3
    assert not reader.read_byte.waiting()
    # It leaves a clean screen and the cursor back.
    assert "".join(written).endswith("\x1b[0m\x1b[2J\x1b[H\x1b[?25h")


def test_keys_typed_ahead_are_left_for_the_game(live_terminal, monkeypatch):
    reader, write_fd = live_terminal
    written = []
    monkeypatch.setattr(vr, "out", written.append)
    os.write(write_fd, b"M")
    assert vr.play_splash(INFO) is False
    assert written == []
    assert reader.read_key() == "M"


def test_a_resize_ends_the_splash_and_is_left_for_the_next_screen(live_terminal, monkeypatch):
    frames_seen = []

    def pause(seconds):
        frames_seen.append(seconds)
        if len(frames_seen) == 2:
            monkeypatch.setattr(vr, "_RESIZE_PENDING", True)
        return True

    monkeypatch.setattr(vr, "motion_pause", pause)
    monkeypatch.setattr(vr, "out", lambda text: None)
    assert vr.play_splash(INFO) is True
    assert len(frames_seen) == 2
    assert vr._RESIZE_PENDING is True


@pytest.mark.parametrize("value", ["0", "off", "No", " false "])
def test_the_environment_can_switch_the_splash_off(live_terminal, monkeypatch, value):
    # A live terminal and a motion preset: everything the splash needs, but the
    # SysOp (or the door gallery) has set DOOR_SPLASH off.
    written = []
    monkeypatch.setattr(vr, "out", written.append)
    monkeypatch.setenv("DOOR_SPLASH", value)
    assert vr.play_splash(INFO) is False
    assert written == []


def test_a_large_terminal_gets_the_capped_scene_centred_and_promptly():
    frames = vr.splash_frames(500, 200, INFO)
    first = next(frames)
    rows = [int(row) for row in re.findall(r"\x1b\[(\d+);\d+H", first + "".join(frames))]
    cols = [int(col) for col in re.findall(r"\x1b\[\d+;(\d+)H", first)]
    top = (200 - vr.SPLASH_MAX_HEIGHT) // 2
    left = (500 - vr.SPLASH_MAX_WIDTH) // 2
    assert top < min(rows) and max(rows) <= top + vr.SPLASH_MAX_HEIGHT
    assert left < min(cols) and max(cols) <= left + vr.SPLASH_MAX_WIDTH
