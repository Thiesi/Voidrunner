"""A terminal both wide and tall gets the lower screen as a stage.

At 160x48 the console is the top left and most pages leave everything under it
open, with the whole picture squeezed into the column beside it. These pin the
stage: from which size it applies, that the world and a fight are drawn across
the lower screen there and only there, that smaller terminals compose as they
did, and that the picture still costs what a picture may.
"""

from __future__ import annotations

import pytest

from .support import _world_with_seed, vr


@pytest.fixture
def terminal(monkeypatch):
    def open_(width: int, height: int):
        monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
        monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
        monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
        monkeypatch.setattr(vr, "_OUTPUT_ENCODING", "utf-8")
        monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=True))
        vr._CANOPY_CACHE.clear()
    yield open_
    vr._CANOPY_CACHE.clear()


def _lit(cells, rows, columns) -> int:
    """Cells in a region that carry a picture and not just sky: bright enough to be a world or a hull."""
    count = 0
    for row in rows:
        for column in columns:
            _char, fg, bg = cells[row][column]
            if any(color and max(color) >= 110 for color in (fg, bg)):
                count += 1
    return count


def _dock(world):
    return vr.canopy_scene(world)


def _fight(world):
    scene = dict(vr.canopy_scene(world))
    scene.update(kind="combat", sky="alert", name="Hollow Fang", enemy="raider")
    return scene


@pytest.mark.parametrize("width,height,staged", [(160, 48, True), (132, 43, True), (200, 60, True),
                                                 (132, 36, False), (120, 36, False), (80, 24, False)])
def test_the_world_is_drawn_across_the_lower_screen_from_forty_rows(terminal, width, height, staged):
    terminal(width, height)
    cells = vr.canopy_cells(_dock(_world_with_seed(42)), width, height)
    panel = vr._box_outer_width()
    # Under the console, well to the left of the column beside it, in the bottom quarter.
    under = _lit(cells, range(height * 3 // 4, height - 1), range(panel // 2, panel - 4))
    area = (height - 1 - height * 3 // 4) * (panel - 4 - panel // 2)
    if staged:
        assert under > area * 0.5  # the world is there
    elif width >= vr.WIDE_LAYOUT_FROM or width - panel >= 22:
        assert under < area * 0.15  # a side window: the picture keeps to its column
    assert vr.CANOPY_STAGE_HEIGHT == 40


def test_a_fight_is_flown_across_the_lower_screen(terminal):
    terminal(160, 48)
    world = _world_with_seed(42)
    cells = vr.canopy_cells(_fight(world), 160, 48)
    panel = vr._box_outer_width()
    lower = range(30, 46)
    own = _lit(cells, lower, range(20, 60))
    enemy = _lit(cells, lower, range(80, 130))
    assert own > 40 and enemy > 120  # the pilot's hull on the left, the contact on the right, both large
    assert enemy > own  # and the contact the larger
    # The column beside the console keeps the alert and the name, and no ships.
    assert _lit(cells, range(4, 22), range(panel + 2, 158)) < 30
    # Below forty rows the fight is in its column, as it was.
    terminal(132, 36)
    cells = vr.canopy_cells(_fight(world), 132, 36)
    panel = vr._box_outer_width()
    assert _lit(cells, range(24, 35), range(10, panel - 10)) < 20
    assert _lit(cells, range(6, 34), range(panel + 1, 131)) > 60


def test_the_station_and_the_name_stay_beside_the_console(terminal):
    terminal(160, 48)
    world = _world_with_seed(42)
    cells = vr.canopy_cells(_dock(world), 160, 48)
    panel = vr._box_outer_width()
    text = "".join(ch for ch, _fg, _bg in cells[1][panel:])
    assert world.here.station_name.upper() in text
    assert _lit(cells, range(10, 22), range(panel + 2, 158)) > 40  # the station, in the column


def test_a_staged_picture_is_painted_once_and_costs_what_a_picture_may(terminal):
    terminal(160, 48)
    world = _world_with_seed(42)
    scene = _dock(world)
    assert vr.canopy_cells(scene, 160, 48) is vr.canopy_cells(scene, 160, 48)  # composed once for the place
    painted = vr.canopy_paint(scene, vr._box_outer_width(), 20)
    assert len(painted.encode("utf-8")) < 120_000
