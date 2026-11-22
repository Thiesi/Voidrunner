"""Scenes with presence, one Shuttle at the commission, and a chart with a frontier (review, v1.3).

At 80x24 a seven-pixel ship was three and a half rows of a twenty-row sky, the
commission screen drew the starting Shuttle twice, and the star map showed
nothing of the way on. These pin the size the scenes draw at, that every hull
still fits its sky, and what the chart may now draw past the charted systems.
"""

from __future__ import annotations

import pytest

from .support import _world_with_seed, plain, vr


@pytest.fixture
def truecolor(monkeypatch):
    def open_(width: int, height: int):
        monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
        monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
        monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
        monkeypatch.setattr(vr, "_OUTPUT_ENCODING", "utf-8")
        monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=True))
        vr._CANOPY_CACHE.clear()
    yield open_
    vr._CANOPY_CACHE.clear()


# --- the scenes ---------------------------------------------------------------------

@pytest.mark.parametrize("width,height,zoom", [(40, 12, 1), (64, 20, 1), (80, 24, 2), (120, 36, 2), (132, 50, 3)])
def test_a_scene_draws_two_pixels_to_one_from_an_80x24_terminal(width, height, zoom):
    cols, rows, _top, _left = vr._splash_box(width, height)
    scene = vr._SalvageScene(cols, rows, hull="Shuttle", credits=100, site=None, seed="t")
    assert scene.zoom == zoom


@pytest.mark.parametrize("hull", list(vr.HULL_CLASSES))
def test_every_hull_fits_its_sky_at_80x24_and_is_no_longer_a_speck(hull):
    scene = vr._ContactScene(80, 24, hull=hull, kind="raider", names=["Probe"], target=0, tier=1, seed="t")
    art_w, art_h = scene.sprite_size(vr._SCENE_HULLS[hull])
    assert art_h <= scene.sky_px - 6            # the whole ship, top to bottom
    assert art_h >= scene.sky_px // 3           # and a third of the sky, where it was a sixth
    assert art_w <= 40                          # half the screen at most, for the Carrier
    enemy_w, _ = scene.sprite_size(scene.enemy)
    assert 80 * 0.68 - enemy_w / 2 >= 80 * 0.22 + art_w / 2 - 1  # it does not run into the contact


# --- the commission -------------------------------------------------------------------

def test_the_commission_draws_the_starting_shuttle_once(truecolor):
    truecolor(80, 24)
    card = vr._commission_lines(vr.pal(), "Welcome.", None)
    assert any("Battered Shuttle" in plain(row) for row in card)
    hull_rows = [plain(row) for row in vr.pixel_hull(vr.starting_ship())]
    assert all(any(art.strip() and art.strip() in plain(row) for row in card) for art in hull_rows)  # on the card
    # The sky behind the card is the title's world and nothing else.
    cells = vr.canopy_cells({"kind": "void", "sky": "calm", "seed": "void"}, 80, 24)
    band_top = round(24 * 0.58)
    art_rows = [row for row, line in enumerate(cells) if any(ch not in " ·•+" for ch, _, _ in line)]
    assert art_rows and min(art_rows) >= band_top + 4  # the world's limb, low in the band: no ship above it


# --- the chart ------------------------------------------------------------------------

@pytest.mark.parametrize("a,b,expected", [
    ((1, 1), (9, 9), ((1, 1), (9, 9))),                # wholly inside
    ((5, 5), (25, 5), ((5, 5), (10, 5))),              # leaves by the right edge
    ((-10, 5), (5, 5), ((0, 5), (5, 5))),              # enters by the left
    ((5, -20), (5, 30), ((5, 0), (5, 10))),            # crosses top to bottom
    ((20, 20), (30, 30), None),                        # wholly outside
    ((-5, 5), (-1, 5), None),
])
def test_a_lane_is_cut_to_the_charts_own_rows(a, b, expected):
    got = vr._clip_segment(a, b, 0, 0, 10, 10)
    if expected is None:
        assert got is None
    else:
        assert all(x == pytest.approx(y) for pair, want in zip(got, expected) for x, y in zip(pair, want))


def _chart_cells(world, sector):
    """The picture's drawn cells, as (row, column) of everything that is not sky."""
    chart = vr.star_chart(world, [], None, sector, "[B] Back: ")
    canvas = {}
    row = column = 0
    import re
    for token in re.split(r"(\x1b\[[0-9;]*[A-Za-z])", chart):
        move = re.fullmatch(r"\x1b\[(\d+);(\d+)H", token)
        if move:
            row, column = int(move[1]) - 1, int(move[2]) - 1
        elif not token.startswith("\x1b"):
            for ch in token:
                if ch not in "\r\n":
                    canvas[(row, column)] = ch
                    column += 1
    return {cell for cell, ch in canvas.items() if ch not in " ·•+*."}


def test_the_chart_draws_the_way_on_from_a_charted_system(truecolor):
    truecolor(80, 24)
    world = _world_with_seed(42)
    sector = vr.SECTOR_NAMES.index(vr.sector_for(world.here))
    # One of home's neighbours, in this sector, that the pilot has not charted.
    unknown = next(world.by_id[sid] for sid in world.here.connections
                   if vr.sector_for(world.by_id[sid]) == vr.sector_for(world.here))
    unknown.discovered = False
    marked = _chart_cells(world, sector)
    picture = vr.star_chart(world, [], None, sector, "[B] Back: ")
    # The same chart without that lane has no mark for the bearing and no
    # dashed lane out to it.
    links = list(world.here.connections)
    world.here.connections = [sid for sid in links if sid != unknown.id]
    bare = _chart_cells(world, sector)
    assert bare < marked, "the bearing has no mark"
    assert vr.star_chart(world, [], None, sector, "[B] Back: ") != picture
    world.here.connections = links
    assert unknown.name not in plain(picture)  # marked, never named


def test_a_bearing_is_marked_and_never_named(truecolor):
    truecolor(120, 36)
    world = _world_with_seed(42)
    for station in world.galaxy:
        station.discovered = station.id == 0
    chart = plain(vr.star_chart(world, [], None, None, "[B] Back: "))
    assert world.here.name.upper() in chart
    for sid in world.here.connections:
        assert world.by_id[sid].name not in chart
