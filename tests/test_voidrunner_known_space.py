"""The star map opens on known space (review, v1.3).

The map framed a fixed sixth of the galaxy, so a new career's chart was three
stars in a corner. These pin the frame it opens on now: that it holds everything
the pilot knows of and the bearings that lead on, that it has a least size and
edges on steps of five, that the picture and the text grid both draw inside it
and still name nothing uncharted, and how the keys move between the three views.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from .support import _world_with_seed, plain, vr


def _fresh(seed: int = 42):
    world = _world_with_seed(seed)
    for station in world.galaxy:
        station.discovered = station.id == 0
    return world


@pytest.mark.parametrize("seed", range(40, 52))
def test_the_frame_holds_everything_known_and_every_way_on(seed):
    world = _fresh(seed)
    for sid in sorted(world.here.connections)[:2]:
        world.by_id[sid].discovered = True
    xmin, xmax, ymin, ymax = vr.known_space_bounds(world, [], None)
    inside = vr.map_system_ids(world, [], None) | vr.map_bearings(world)
    assert vr.map_bearings(world)  # a young career always has somewhere uncharted to go
    for sid in inside:
        station = world.by_id[sid]
        assert xmin <= station.x <= xmax and ymin <= station.y <= ymax
    assert 0 <= xmin < xmax <= 99 and 0 <= ymin < ymax <= 49
    # Edges on steps of five, so the frame holds still from one jump to the next.
    assert xmin % vr.MAP_KNOWN_STEP == 0 and (xmax + 1) % vr.MAP_KNOWN_STEP == 0 or xmax == 99
    assert ymin % vr.MAP_KNOWN_STEP == 0 and (ymax + 1) % vr.MAP_KNOWN_STEP == 0 or ymax == 49
    # Never a diagram of three stars: a least size, unless the galaxy's edge is in the way.
    assert xmax - xmin + 1 >= vr.MAP_KNOWN_LEAST[0] or xmin == 0 or xmax == 99
    assert ymax - ymin + 1 >= vr.MAP_KNOWN_LEAST[1] or ymin == 0 or ymax == 49


def test_the_frame_is_the_whole_galaxy_once_the_galaxy_is_charted():
    world = _world_with_seed(42)
    for station in world.galaxy:
        station.discovered = True
    xmin, xmax, ymin, ymax = vr.known_space_bounds(world, [], None)
    assert not vr.map_bearings(world)
    assert xmax - xmin >= 80 and ymax - ymin >= 35


def test_the_frame_reaches_out_to_an_objective_and_a_route():
    world = _fresh()
    far = max(world.galaxy, key=lambda s: abs(s.x - world.here.x) + abs(s.y - world.here.y))
    near = vr.known_space_bounds(world, [], None)
    xmin, xmax, ymin, ymax = vr.known_space_bounds(world, [], far.id)
    assert xmin <= far.x <= xmax and ymin <= far.y <= ymax
    assert (xmax - xmin) * (ymax - ymin) > (near[1] - near[0]) * (near[3] - near[2])


def test_the_text_grid_fills_its_frame_and_marks_the_ways_on():
    world = _fresh()
    frame = vr.known_space_bounds(world, [], None)
    home_sector = vr.SECTOR_NAMES.index(vr.sector_for(world.here))
    known = vr.spatial_map_grid(world, [], public_target=None, sector=None, columns=79, rows=18, bounds=frame)
    sector = vr.spatial_map_grid(world, [], public_target=None, sector=home_sector, columns=79, rows=18)
    assert "".join(known).count("@") == 1
    # Every way on from home is on the known-space grid; a sector's window may cut some off.
    assert "".join(known).count("?") == len(world.here.connections) >= "".join(sector).count("?")
    assert "".join(known).count(".") > "".join(sector).count(".") or "".join(sector).count("?") == len(world.here.connections)
    # The ship is no longer in a corner: it sits in the middle half of the grid, both ways.
    row = next(index for index, line in enumerate(known) if "@" in line)
    column = known[row].index("@")
    assert len(known) // 4 <= row <= 3 * len(known) // 4 and 79 // 4 <= column <= 3 * 79 // 4


def test_neither_drawing_names_or_describes_a_bearing(monkeypatch):
    world = _fresh()
    frame = vr.known_space_bounds(world, [], None)
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 24)
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    monkeypatch.setattr(vr, "_OUTPUT_ENCODING", "utf-8")
    monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=True))
    vr._CANOPY_CACHE.clear()
    picture = plain(vr.star_chart(world, [], None, None, "[B] Back: ", bounds=frame))
    vr._CANOPY_CACHE.clear()
    assert "Star Map: Known space" in picture and world.here.name.upper() in picture
    grid = "\n".join(vr.spatial_map_grid(world, [], public_target=None, sector=None, columns=79, rows=18, bounds=frame))
    for sid in vr.map_bearings(world):
        assert world.by_id[sid].name not in picture and world.by_id[sid].name not in grid


def test_the_heading_says_which_of_the_three_views_it_is():
    assert vr.map_heading(None, (0, 24, 0, 14)) == "Star Map: Known space"
    assert vr.map_heading(None, None) == "Star Map: Galaxy"
    assert vr.map_heading(2, None) == "Star Map: " + vr.SECTOR_NAMES[2]


def _map_session(world, keys: str, monkeypatch) -> list[str]:
    """The heading and action bar of each map drawn while `keys` are pressed."""
    typed = iter(keys)
    monkeypatch.setattr(vr, "read_key", lambda: next(typed))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_galaxy_map(vr.Palette(False), world)
    text = plain(output.getvalue())
    return [line.strip() for line in text.splitlines() if line.startswith("Star Map:") or "[N/P] Sector" in line]


def test_the_map_opens_on_known_space_and_the_keys_move_between_the_views(monkeypatch):
    world = _fresh()
    home = vr.sector_for(world.here)
    after = vr.SECTOR_NAMES[(vr.SECTOR_NAMES.index(home) + 1) % len(vr.SECTOR_NAMES)]
    seen = _map_session(world, "OONNOB", monkeypatch)
    headings = [line for line in seen if line.startswith("Star Map:")]
    assert headings == ["Star Map: Known space", "Star Map: Galaxy", "Star Map: Known space",
                        f"Star Map: {home}", f"Star Map: {after}", "Star Map: Galaxy"]
    bars = [line for line in seen if "[N/P] Sector" in line]
    # [O] offers the way out to the galaxy, and from the galaxy the way back.
    assert ["[O] Known" in bar for bar in bars[:6]] == [False, True, False, False, False, True]
    assert all("[O] Overview" in bar or "[O] Known" in bar for bar in bars)
