"""The viewport, drawn: the ship, the station and the landmark as pictures.

A viewport picture is a screen, not a moment, so its rules are a screen's with
motion's on top: every fact the ASCII portrait carried is in its caption, the
key that ends its idle is the command the bar asked for, and motion obeys the
presets and `DOOR_SCENES` like a scene does. `mono`, `plain` and a terminal too
small for a picture over its caption keep the ASCII portraits. How the pictures
*look* is reviewed from rendered frames.
"""

from __future__ import annotations

import contextlib
import copy
import io
import os
import re

import pytest

from .support import _world_with_seed, vr

CUP = re.compile(r"\x1b\[(\d+);(\d+)H")
SIZES = [(40, 12), (80, 24), (132, 50)]


@pytest.fixture(autouse=True)
def _scenes_switched_on(monkeypatch):
    """The suite turns scenes off (conftest); motion here is the subject."""
    monkeypatch.delenv("DOOR_SCENES", raising=False)


@pytest.fixture
def depth(monkeypatch):
    def choose(style: str, truecolor: bool) -> None:
        monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
        monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=truecolor))
    return choose


def _text(cells) -> str:
    return "\n".join("".join(ch for ch, _fg, _bg in row) for row in cells)


def every_picture(width: int, height: int) -> dict[str, "vr._ViewportScene"]:
    world = _world_with_seed(42)
    pictures = {}
    for hull in vr._SCENE_HULLS:
        for condition, hp in (("Intact", "60/60"), ("Critical", "5/60")):
            pictures[f"{hull}-{condition}"] = vr._ViewportShip(
                width, height, hull=hull, condition=condition, hull_hp=hp, fuel="7/24", cargo="3/24",
                economy="Tech", seed="t")
    for economy in vr.ECONOMIES:
        pictures[economy] = vr._ViewportStation(width, height, station="Harbor Lights", economy=economy,
                                                items=[[("Kepler Reach", vr._SCENE_INK)]], seed="t")
    for site in vr.LANDMARK_FLAVORS:
        pictures[site["label"]] = vr._ViewportSite(width, height, site=site["label"], items=[],
                                                   prose=site["flavor"], seed="t")
    pictures["nothing"] = vr._ViewportSite(width, height, site=None, items=[], prose="Nothing yet.", seed="t")
    return {name: picture for name, picture in pictures.items() if picture.fits}


@pytest.mark.parametrize("width,height", SIZES)
def test_every_frame_of_every_picture_fills_the_terminal_and_nothing_more(width, height, depth):
    depth("auto", True)
    for name, picture in every_picture(width, height).items():
        for index in (0, picture.still, picture.still + 7, vr.VIEWPORT_MOTION_FRAMES - 1):
            cells = picture.frame(index)
            assert len(cells) == height, name
            assert all(len(row) == width for row in cells), name
            # The bottom row is the action bar's: the picture never draws on it.
            assert all(cell == (" ", None, None) for cell in cells[-1]), name


def test_ships_stations_and_the_scanner_fit_the_smallest_terminal():
    pictures = every_picture(40, 12)
    assert {f"{hull}-Intact" for hull in vr._SCENE_HULLS} <= set(pictures)
    assert set(vr.ECONOMIES) <= set(pictures) and "nothing" in pictures


@pytest.mark.parametrize("width,height", [(80, 24), (132, 50)])
def test_a_picture_is_the_same_every_time_it_is_composed(width, height, depth):
    depth("auto", True)
    first, second = every_picture(width, height), every_picture(width, height)
    for name in first:
        assert first[name].frame(first[name].still + 5) == second[name].frame(second[name].still + 5), name


def test_the_shutters_open_before_the_still(depth):
    depth("auto", True)
    picture = every_picture(80, 24)["Shuttle-Intact"]
    shut, still = picture.frame(0), picture.frame(picture.still)
    assert shut != still
    # Closed shutters cover the sky; the still frame shows the ship there.
    assert any(cell[0] != " " or cell[2] is not None for cell in shut[0])


def test_damage_shows_on_the_hull(depth):
    depth("auto", True)
    def hull_colors(condition):
        picture = vr._ViewportShip(80, 24, hull="Freighter", condition=condition, hull_hp="1/1", fuel="1/1",
                                   cargo="0/1", economy="Tech", seed="t")
        return dict(picture.hull)
    intact, critical = hull_colors("Intact"), hull_colors("Critical")
    assert intact.keys() == critical.keys()
    assert sum(intact[p] != critical[p] for p in intact) >= 9  # nine scorch marks


def _world_at_site(claimed: bool = False):
    world = _world_with_seed(42)
    world.save.current_system = world.landmark["system_id"]
    if claimed:
        world.save.flags["landmark_investigated"] = True
    return world


def test_the_ship_caption_carries_the_portraits_facts(depth):
    depth("auto", True)
    world = _world_with_seed(42)
    ship = world.save.ship
    ship.hull_hp = vr.hull_hp_max(ship) // 2
    text = _text(vr.viewport_scene(world, "1", 80, 24).frame(vr.VIEWPORT_SHUTTER_FRAMES))
    assert f"{ship.hull_class.upper()}  ·  DAMAGED" in text
    assert f"Hull {ship.hull_hp}/{vr.hull_hp_max(ship)}" in text
    assert f"Fuel {ship.fuel}/{vr.fuel_capacity(ship)}" in text
    assert f"Cargo {sum(world.save.cargo.values())}/{vr.cargo_capacity(ship)}" in text


@pytest.mark.parametrize("module", list(vr.WORKSHOPS))
def test_the_station_caption_carries_the_portraits_facts(module, depth):
    depth("auto", True)
    world = _world_with_seed(42)
    world.save.current_system = vr.specialist_stations(world)[module]
    here = world.here
    text = " ".join(_text(vr.viewport_scene(world, "2", 80, 24).frame(vr.VIEWPORT_SHUTTER_FRAMES)).split())
    for value in (here.station_name.upper(), here.name, f"({here.x},{here.y})", vr.sector_for(here), here.economy,
                  f"danger {here.danger}/5", vr.WORKSHOPS[module]["name"], vr.WORKSHOPS[module]["owner"]):
        assert value in text, value


@pytest.mark.parametrize("claimed", [False, True])
def test_the_discovery_caption_carries_the_portraits_facts(claimed, depth):
    depth("auto", True)
    world = _world_at_site(claimed)
    text = " ".join(_text(vr.viewport_scene(world, "3", 80, 24).frame(vr.VIEWPORT_SHUTTER_FRAMES)).split())
    assert world.landmark["label"].upper() in text
    assert ("Salvage already claimed." if claimed else
            f"Unclaimed salvage: {world.landmark['reward_credits']:,}cr") in text
    assert " ".join(world.landmark["flavor"].split()) in text


def test_an_unvisited_landmark_stays_a_secret(depth):
    depth("auto", True)
    world = _world_with_seed(42)
    text = _text(vr.viewport_scene(world, "3", 80, 24).frame(vr.VIEWPORT_SHUTTER_FRAMES))
    assert "NO DISCOVERY RECORDED" in text and world.landmark["label"].upper() not in text


@pytest.mark.parametrize("width", [40, 80, 132])
def test_the_action_bar_fits_on_the_pictures_bottom_row(width):
    footer = vr.viewport_footer(width)
    assert vr._visible_width(footer) <= width - 1
    assert "[B] Back" in footer and ("[1-3] View" in footer or "[3] Discovery" in footer)


@pytest.mark.parametrize("style,truecolor,budget", [("auto", True, 100_000), ("basic", False, 60_000)])
def test_opening_and_idling_stays_small_on_the_wire(style, truecolor, budget, depth):
    """Everything a picture sends from opening to holding still, at 80x24."""
    depth(style, truecolor)
    for name, picture in every_picture(80, 24).items():
        previous = [[(" ", None, None)] * 80 for _ in range(24)]
        sent = 0
        for index in range(vr.VIEWPORT_MOTION_FRAMES):
            current = picture.frame(index)
            sent += len(vr._splash_delta(previous, current, red=True).encode())
            previous = current
        assert sent < budget, (name, sent)


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


def _picture():
    return vr.viewport_scene(_world_with_seed(42), "2", 80, 24)


def test_the_key_that_ends_the_idle_is_the_command(live_terminal, monkeypatch):
    reader, write_fd = live_terminal
    written, frames = [], []

    def pause(seconds):
        frames.append(seconds)
        if len(frames) == 3:
            os.write(write_fd, b"3")
        return not reader.read_byte.waiting()

    monkeypatch.setattr(vr, "motion_pause", pause)
    monkeypatch.setattr(vr, "out", written.append)
    monkeypatch.setattr(vr, "read_key", reader.read_key)  # the pipe, not the runner's stdin
    assert vr.show_viewport_picture(_picture(), vr.viewport_footer(80), 0, 0) == "3"
    assert len(frames) == 3
    drawn = "".join(written)
    # Each idle frame hands the cursor back to the end of the bar.
    assert drawn.count(f"\x1b[24;{1 + len(vr.viewport_footer(80))}H") >= 3
    for row, column in CUP.findall(drawn):
        assert 1 <= int(row) <= 24 and 1 <= int(column) <= 80


def test_left_alone_the_idle_ends_and_the_picture_holds(live_terminal, monkeypatch):
    frames = []
    monkeypatch.setattr(vr, "motion_pause", lambda seconds: frames.append(seconds) or True)
    monkeypatch.setattr(vr, "read_command_at_prompt", lambda: "B")
    monkeypatch.setattr(vr, "out", lambda text: None)
    assert vr.show_viewport_picture(_picture(), vr.viewport_footer(80), 0, 0) == "B"
    assert len(frames) == vr.VIEWPORT_MOTION_FRAMES - 1


def test_a_resize_ends_the_idle_and_comes_back_as_the_resize_key(live_terminal, monkeypatch):
    frames = []

    def pause(seconds):
        frames.append(seconds)
        if len(frames) == 2:
            monkeypatch.setattr(vr, "_RESIZE_PENDING", True)
        return True

    monkeypatch.setattr(vr, "motion_pause", pause)
    monkeypatch.setattr(vr, "read_command_at_prompt", lambda: vr.RESIZE_KEY if vr._RESIZE_PENDING else "?")
    monkeypatch.setattr(vr, "out", lambda text: None)
    assert vr.show_viewport_picture(_picture(), vr.viewport_footer(80), 0, 0) == vr.RESIZE_KEY
    assert len(frames) == 2


@pytest.mark.parametrize("switch", ["fast", "scenes-off", "typed-ahead"])
def test_without_motion_the_picture_is_drawn_once(live_terminal, monkeypatch, switch):
    reader, write_fd = live_terminal
    if switch == "fast":
        monkeypatch.setattr(vr, "_OUTPUT_STYLE", "fast")
    elif switch == "scenes-off":
        monkeypatch.setenv("DOOR_SCENES", "0")
    else:
        os.write(write_fd, b"1")
    monkeypatch.setattr(vr, "motion_pause", lambda seconds: pytest.fail("idled without motion"))
    monkeypatch.setattr(vr, "read_command_at_prompt", lambda: "B")
    written = []
    monkeypatch.setattr(vr, "out", written.append)
    picture = _picture()
    vr.show_viewport_picture(picture, vr.viewport_footer(80), 0, 0)
    # The still frame, shutters open, and a key typed ahead left for the bar.
    blank = [[(" ", None, None)] * 80 for _ in range(24)]
    assert vr._splash_delta(blank, picture.frame(picture.still), red=True) in "".join(written)
    if switch == "typed-ahead":
        assert reader.read_key() == "1"


def _walk(monkeypatch, world, keys):
    frames, output = [], io.StringIO()
    keys = iter(keys)

    def choose():
        frames.append(output.getvalue())
        output.seek(0)
        output.truncate(0)
        return next(keys)

    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output):
        vr.screen_viewport(vr.Palette(True), world)
    return frames


@pytest.mark.parametrize("style", ["auto", "fast", "basic"])
def test_the_viewport_walks_every_picture_and_writes_nothing(monkeypatch, terminal, style):
    world = _world_at_site()
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    world._checkpoint = lambda w: pytest.fail("Viewport wrote a save")
    terminal(80, 24, style)
    frames = _walk(monkeypatch, world, ["2", "3", "1", "B"])
    plain = [vr._ANSI_RE.sub("", frame) for frame in frames]
    assert world.save.ship.hull_class.upper() in plain[0]
    assert world.here.station_name.upper() in plain[1]
    assert world.landmark["label"].upper() in plain[2]
    assert all("[B] Back" in frame for frame in plain)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


def test_a_site_too_long_for_a_small_terminal_keeps_its_ascii_portrait(monkeypatch, terminal):
    world = _world_at_site()
    terminal(40, 12, "auto")
    frames = _walk(monkeypatch, world, ["3", ">", ">", "B"])
    text = " ".join(" ".join(vr._ANSI_RE.sub("", frame) for frame in frames[1:]).split())
    # The ship is a picture; the landmark's story needs the room, so it pages.
    assert "Views: [1] Ship" not in vr._ANSI_RE.sub("", frames[0])
    assert "Views: [1] Ship" in text and re.search(r"\b1/\d", text)


@pytest.mark.parametrize("style", ["mono", "plain"])
def test_presets_without_color_keep_the_ascii_portraits(monkeypatch, terminal, style):
    world = _world_with_seed(42)
    terminal(80, 24, style)
    frames = _walk(monkeypatch, world, ["B"])
    assert "Views: [1] Ship" in frames[0] and "▀" not in frames[0]
