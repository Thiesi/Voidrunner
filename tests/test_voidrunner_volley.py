"""An exchange of fire is seen in the window.

A fight's picture was still while everything that happened was a sentence and
two bars. These pin the shots that now cross it: that they are drawn only in
the window and never on the console, that every cell is put back as it was,
that the effect follows the rules every effect follows, and that the picture
and the shots agree on where the two ships are.
"""

from __future__ import annotations

import contextlib
import io
import re

import pytest

from .support import _world_with_seed, vr

_MOVE = re.compile(r"\x1b\[(\d+);(\d+)H")
_TOKEN = re.compile(r"(\x1b\[[0-9;]*[A-Za-z])")


@pytest.fixture
def window(monkeypatch):
    def open_(width: int, height: int, style: str = "auto"):
        monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
        monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
        monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
        monkeypatch.setattr(vr, "_OUTPUT_ENCODING", "utf-8")
        monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=True))
        monkeypatch.setattr(vr, "motion_interrupted", lambda: False)  # someone is watching
        monkeypatch.setattr(vr, "motion_pause", lambda seconds: True)
        vr._CANOPY_CACHE.clear()
    yield open_
    vr._CANOPY_CACHE.clear()
    vr._PENDING_VOLLEY = None


def _scene(world):
    scene = dict(vr.canopy_scene(world))
    scene.update(kind="combat", sky="alert", name="Hollow Fang", enemy="raider")
    return scene


def _play(scene, drawn, dealt=True, received=True):
    vr.volley_in_place_next(dealt, received)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr._volley_in_place(scene, drawn)
    return output.getvalue()


def _writes(text):
    """Every character the effect put down, in order, as (row, column, char), and where the cursor ended."""
    row = column = 0
    cells = []
    for token in _TOKEN.split(text):
        move = _MOVE.fullmatch(token)
        if move:
            row, column = int(move[1]) - 1, int(move[2]) - 1
        elif not token.startswith("\x1b"):
            for char in token:
                cells.append((row, column, char))
                column += 1
    return cells, (row, column)


@pytest.mark.parametrize("width,height,drawn", [(80, 24, 15), (120, 36, 15), (160, 48, 15)])
def test_the_shots_cross_the_window_and_leave_it_as_it_was(window, width, height, drawn):
    window(width, height)
    scene = _scene(_world_with_seed(42))
    original = vr.canopy_cells(scene, width, height)
    cells, cursor = _writes(_play(scene, drawn))
    assert len(cells) > 40  # two bolts and two flashes
    panel = vr._box_outer_width()
    for row, column, _char in cells:
        assert 0 <= row < height and 0 <= column < width
        assert not (row < drawn + 2 and column < panel)  # never the console or its action bar
        assert (row, column) != (height - 1, width - 1)  # never the cell a terminal scrolls on
    # The last thing written to every touched cell is what the picture had there.
    last = {}
    for row, column, char in cells:
        last[(row, column)] = char
    assert all(char == original[row][column][0] for (row, column), char in last.items())
    assert cursor == (drawn, 0)  # where the action bar expects it
    assert vr._PENDING_VOLLEY is None
    # The picture itself is untouched: one picture, painted once.
    assert vr.canopy_cells(scene, width, height) is original


def test_only_what_happened_is_shown(window):
    window(80, 24)
    scene = _scene(_world_with_seed(42))
    both = len(_writes(_play(scene, 15))[0])
    out_only = len(_writes(_play(scene, 15, received=False))[0])
    back_only = len(_writes(_play(scene, 15, dealt=False))[0])
    assert 0 < out_only < both and 0 < back_only < both
    vr.volley_in_place_next(False, False)
    assert vr._PENDING_VOLLEY is None  # nothing crossed: nothing to play


def test_the_effect_keeps_every_effects_rules(window, monkeypatch):
    scene = _scene(_world_with_seed(42))
    window(80, 24, "fast")
    assert _play(scene, 15) == "" and vr._PENDING_VOLLEY is None  # `fast` has no motion
    window(80, 24)
    monkeypatch.setattr(vr, "motion_interrupted", lambda: True)
    assert _play(scene, 15) == ""  # nobody watching, or a key already down
    # A key during the outgoing bolt ends the whole exchange, and the window is still put back.
    monkeypatch.setattr(vr, "motion_interrupted", lambda: False)
    monkeypatch.setattr(vr, "motion_pause", lambda seconds: False)
    original = vr.canopy_cells(scene, 80, 24)
    cells, cursor = _writes(_play(scene, 15))
    last = {}
    for row, column, char in cells:
        last[(row, column)] = char
    assert cells and all(char == original[row][column][0] for (row, column), char in last.items())
    assert cursor == (15, 0)
    # Docked, there is no fight to show.
    assert _play(dict(vr.canopy_scene(_world_with_seed(42))), 15) == ""


@pytest.mark.parametrize("width,height", [(80, 24), (120, 36), (160, 48)])
def test_the_picture_and_the_shots_agree_on_where_the_ships_are(window, width, height):
    window(width, height)
    scene = _scene(_world_with_seed(42))
    points = vr.combat_stage_points(scene, width, height)
    cells = vr.canopy_cells(scene, width, height)
    for who in ("own", "enemy"):
        cx, cy, half_w, half_h = points[who]
        char, fg, bg = cells[int(cy) // 2][int(cx)]
        assert char != " " or (bg and max(bg) > 100)  # a hull is drawn at its centre
        assert half_w > 1 and half_h > 0.5
    assert points["own"][0] < points["enemy"][0] or width >= 120  # the pilot on the left where they face each other
    # Too small for a window: nowhere to stage a fight, and nothing is played.
    window(40, 12)
    assert vr.combat_stage_points(scene, 40, 12) is None or vr.combat_stage_points(scene, 40, 12)["own"]
