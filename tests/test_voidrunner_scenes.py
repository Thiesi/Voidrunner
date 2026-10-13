"""The in-flight scenes: what they draw, when they play, and what they cost.

Scenes are motion under the splash's rules (issue #493 §3): absent from the
presets that ask for less, with nobody watching and with keys already waiting,
and over the moment a key arrives, which they take. These pin the compositions'
bounds at the sizes the door accepts, their size on the wire, the input rules,
and where in a voyage each one plays -- always after the commit it shows. How
they *look* is reviewed from rendered frames.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import time

import pytest

from .support import _add_cargo, _world_with_pending_fight, _world_with_seed, vr

CUP = re.compile(r"\x1b\[(\d+);(\d+)H")
SIZES = [(40, 12), (59, 17), (80, 24), (132, 50)]


@pytest.fixture(autouse=True)
def _scenes_switched_on(monkeypatch):
    """The suite turns scenes off (conftest); these tests are about them."""
    monkeypatch.delenv("DOOR_SCENES", raising=False)


@pytest.fixture
def depth(monkeypatch):
    def choose(style: str, truecolor: bool) -> None:
        monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
        monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=truecolor))
    return choose


def every_scene(width: int, height: int) -> dict[str, "vr._Scene"]:
    """One of each scene, on the hulls and in the variants that differ most."""
    scenes = {
        "jump": vr._JumpScene(width, height, hull="Carrier", destination="Kepler Reach",
                              burn=3, fuel=7, day=14, seed="t"),
        "jump-uncharted": vr._JumpScene(width, height, hull="Shuttle", destination=None,
                                        burn=2, fuel=1, day=2, seed="t"),
        "raider": vr._ContactScene(width, height, hull="Shuttle", kind="raider", names=["Red Jackal"],
                                   target=0, tier=3, seed="t"),
        "squadron": vr._ContactScene(width, height, hull="Freighter", kind="squadron",
                                     names=["Red Jackal", "Gutter Saint"], target=1, tier=2, seed="t"),
        "patrol": vr._ContactScene(width, height, hull="Cutter", kind="patrol", names=["CNS Vigilant"],
                                   target=0, tier=4, seed="t"),
        "escort": vr._ContactScene(width, height, hull="Carrier", kind="escort", names=["Red Jackal"],
                                   target=0, tier=1, seed="t"),
        "kill": vr._KillScene(width, height, hull="Cutter", patrol=False, name="Red Jackal", loot=220, seed="t"),
        "kill-patrol": vr._KillScene(width, height, hull="Shuttle", patrol=True, name="CNS Vigilant", loot=0, seed="t"),
        "lost": vr._LostScene(width, height, hull="Carrier", patrol=False, name="Red Jackal", seed="t"),
        "escape": vr._EscapeScene(width, height, hull="Freighter", patrol=True, name="CNS Vigilant", seed="t"),
        "scan": vr._ScanScene(width, height, hull="Freighter", units=12, seed="t"),
    }
    for economy in vr.ECONOMIES:
        scenes[f"dock-{economy}"] = vr._DockScene(width, height, economy=economy, station="Harbor Lights",
                                                  system="Kepler Reach", charted=economy == "Tech", seed="t")
    return scenes


def text_of(cells) -> str:
    return "\n".join("".join(ch for ch, _fg, _bg in row) for row in cells)


def wire_bytes(scene) -> int:
    previous = [[(" ", None, None)] * scene.width for _ in range(scene.height)]
    total = 0
    for index in range(scene.frames):
        current = scene.frame(index)
        total += len(vr._splash_delta(previous, current, red=True).encode())
        previous = current
    return total


@pytest.mark.parametrize("width,height", SIZES)
def test_every_frame_of_every_scene_fills_the_terminal_and_nothing_more(width, height, depth):
    depth("auto", True)
    for name, scene in every_scene(width, height).items():
        for index in range(scene.frames):
            cells = scene.frame(index)
            assert len(cells) == height, name
            for row in cells:
                assert len(row) == width, name
                assert sum(max(1, vr._char_width(ch)) if ch else 0 for ch, _fg, _bg in row) <= width, name
            # Never the bottom-right cell, where a terminal may scroll.
            assert cells[height - 1][width - 1] == (" ", None, None), name


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_a_scene_is_the_same_every_time_it_is_composed(width, height, depth):
    depth("auto", True)
    first, second = every_scene(width, height), every_scene(width, height)
    for name in first:
        assert [first[name].frame(i) for i in range(first[name].frames)] == \
               [second[name].frame(i) for i in range(second[name].frames)], name


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_the_last_frame_says_what_happened(width, height, depth):
    depth("auto", True)
    scenes = every_scene(width, height)

    def last(name: str) -> str:
        return text_of(scenes[name].frame(scenes[name].frames - 1))

    assert "JUMP" in last("jump") and "KEPLER REACH" in last("jump")
    assert "UNCHARTED" in last("jump-uncharted")
    assert "RAIDER CONTACT" in last("raider") and "Red Jackal" in last("raider")
    assert "Gutter Saint" in last("squadron")  # the ship the fight is with
    assert "CONCORD INTERDICTION" in last("patrol")
    assert "TARGET DESTROYED" in last("kill") and "+220cr" in last("kill")
    assert "CONCORD VESSEL DESTROYED" in last("kill-patrol")
    assert "SHIP LOST" in last("lost")
    assert "CONTACT BROKEN" in last("escape")
    assert "CONTRABAND DETECTED" in last("scan")
    assert "HARBOR LIGHTS" in last("dock-Agricultural") and "Kepler Reach" in last("dock-Agricultural")
    assert "new system charted" in last("dock-Tech") and "charted" not in last("dock-Haven")


def test_an_uncharted_destination_keeps_its_name(depth):
    depth("auto", True)
    scene = vr._JumpScene(80, 24, hull="Shuttle", destination=None, burn=2, fuel=1, day=2, seed="t")
    assert all("Kepler" not in text_of(scene.frame(i)) for i in range(scene.frames))


def test_a_caption_drops_whole_phrases_rather_than_cutting_one(depth):
    depth("auto", True)
    scene = vr._LostScene(40, 12, hull="Shuttle", patrol=False, name="Red Jackal", seed="t")
    detail = text_of(scene.frame(scene.frames - 1)).splitlines()[scene.caption_row + 1]
    assert "broke your hull" in detail and "tug" not in detail
    assert not detail.rstrip().endswith("·")


def test_caller_text_is_drawn_as_plain_characters(depth):
    depth("auto", True)
    scene = vr._ContactScene(80, 24, hull="Shuttle", kind="raider", names=["\x1b[31mEvil\x07 Name"],
                             target=0, tier=1, seed="t")
    cells = scene.frame(scene.frames - 1)
    assert all(ch == "" or not vr.unicodedata.category(ch).startswith("C") for row in cells for ch, _f, _b in row)
    assert "Evil Name" in text_of(cells)


def test_an_unknown_hull_or_economy_still_draws(depth):
    depth("auto", True)
    assert vr._JumpScene(80, 24, hull="Prototype", destination="X", burn=1, fuel=1, day=1, seed="t").frame(5)
    assert vr._DockScene(80, 24, economy="Unheard-of", station="S", system="Y", charted=False, seed="t").frame(5)


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24), (132, 50)])
def test_the_cursor_never_leaves_the_terminal(width, height, depth):
    depth("auto", True)
    cols, rows, top, left = vr._splash_box(width, height)
    for name, scene in every_scene(cols, rows).items():
        previous = [[(" ", None, None)] * cols for _ in range(rows)]
        for index in range(scene.frames):
            current = scene.frame(index)
            for row, column in CUP.findall(vr._splash_delta(previous, current, top, left, red=True)):
                assert 1 <= int(row) <= height and 1 <= int(column) <= width, name
            previous = current


# What a scene may send at 80x24, in bytes. About twice the splash, over a
# shorter run: the cells that move are the effect, and they are counted
# against the slowest depth's escapes.
WIRE_BUDGET = {("auto", True): 90_000, ("auto", False): 66_000, ("basic", False): 46_000}


@pytest.mark.parametrize("style,truecolor", list(WIRE_BUDGET))
def test_every_scene_stays_small_on_the_wire(style, truecolor, depth):
    depth(style, truecolor)
    budget = WIRE_BUDGET[(style, truecolor)]
    for name, scene in every_scene(80, 24).items():
        assert wire_bytes(scene) < budget, name


def test_every_frame_writes_something_or_holds_still(depth):
    """No frame may be expensive to compose: a scene plays while the caller waits."""
    depth("auto", True)
    for name, scene in every_scene(132, 50).items():
        started = time.perf_counter()
        for index in range(scene.frames):
            scene.frame(index)
        assert (time.perf_counter() - started) / scene.frames < vr.SCENE_FRAME_SECONDS, name


def test_sixteen_colors_let_a_scene_be_red_and_keep_the_splash_free_of_it():
    rust = vr._SCENE_COLORS["r"]
    assert vr._splash_16(rust, red=True) in (1, 9)
    assert vr._splash_16(rust) not in (1, 9)
    # The planet and the wordmark read as before either way.
    assert vr._splash_16((104, 64, 188), red=True) in (5, 13)
    assert vr._splash_16((255, 200, 60), red=True) in (3, 11)


# --- When a scene plays ------------------------------------------------------

def _a_scene(width: int, height: int):
    return vr._KillScene(width, height, hull="Shuttle", patrol=False, name="Red Jackal", loot=100, seed="t")


@pytest.mark.parametrize("style", ["fast", "mono", "plain"])
def test_presets_that_ask_for_less_get_no_scene_and_build_none(style, monkeypatch):
    written, built = [], []
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
    monkeypatch.setattr(vr, "out", written.append)
    assert vr.play_scene(lambda w, h: built.append(1) or _a_scene(w, h)) is False
    assert written == [] and built == []


def test_no_live_terminal_means_no_scene(monkeypatch):
    written = []
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    monkeypatch.setattr(vr, "_INPUT_READER", None)
    monkeypatch.setattr(vr, "out", written.append)
    assert vr.play_scene(_a_scene) is False
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


@pytest.mark.parametrize("value", ["0", "off", "No", " false "])
def test_the_environment_can_switch_the_scenes_off(live_terminal, monkeypatch, value):
    written = []
    monkeypatch.setenv("DOOR_SCENES", value)
    monkeypatch.setattr(vr, "out", written.append)
    assert vr.play_scene(_a_scene) is False
    assert written == []


def test_a_key_ends_the_scene_and_is_consumed_whole(live_terminal, monkeypatch):
    reader, write_fd = live_terminal
    written, pauses = [], []
    real_pause = vr.motion_pause

    def pause(seconds):
        pauses.append(seconds)
        if len(pauses) == 3:
            os.write(write_fd, b"\x1b[Af")  # an arrow and a letter: both for the scene
        return real_pause(0.2 if len(pauses) == 3 else 0.0)

    monkeypatch.setattr(vr, "motion_pause", pause)
    monkeypatch.setattr(vr, "out", written.append)
    assert vr.play_scene(_a_scene) is True
    assert len(pauses) == 3
    assert not reader.read_byte.waiting()
    assert "".join(written).endswith("\x1b[0m\x1b[2J\x1b[H\x1b[?25h")


def test_keys_typed_ahead_are_left_for_the_game(live_terminal, monkeypatch):
    reader, write_fd = live_terminal
    written = []
    monkeypatch.setattr(vr, "out", written.append)
    os.write(write_fd, b"F")
    assert vr.play_scene(_a_scene) is False
    assert written == []
    assert reader.read_key() == "F"


def test_a_resize_ends_the_scene_and_is_left_for_the_next_screen(live_terminal, monkeypatch):
    pauses = []

    def pause(seconds):
        pauses.append(seconds)
        if len(pauses) == 2:
            monkeypatch.setattr(vr, "_RESIZE_PENDING", True)
        return True

    monkeypatch.setattr(vr, "motion_pause", pause)
    monkeypatch.setattr(vr, "out", lambda text: None)
    assert vr.play_scene(_a_scene) is True
    assert len(pauses) == 2 and vr._RESIZE_PENDING is True


def test_left_to_run_it_holds_its_caption_and_goes_on_without_a_key(live_terminal, monkeypatch):
    """Unlike the splash, a scene never waits for a key: play goes on."""
    pauses = []
    monkeypatch.setattr(vr, "motion_pause", lambda seconds: pauses.append(seconds) or True)
    monkeypatch.setattr(vr, "out", lambda text: None)
    scene = _a_scene(80, 24)
    assert vr.play_scene(_a_scene) is True
    assert pauses == [vr.SCENE_FRAME_SECONDS] * scene.frames + [scene.hold]


def test_a_large_terminal_gets_the_capped_scene_centred(live_terminal, monkeypatch):
    sizes, written = [], []
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 300)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 120)
    monkeypatch.setattr(vr, "motion_pause", lambda seconds: True)
    monkeypatch.setattr(vr, "out", written.append)
    assert vr.play_scene(lambda w, h: sizes.append((w, h)) or _a_scene(w, h)) is True
    assert sizes == [(vr.SPLASH_MAX_WIDTH, vr.SPLASH_MAX_HEIGHT)]
    rows = [int(row) for frame in written for row, _col in CUP.findall(frame)]
    assert min(rows) > (120 - vr.SPLASH_MAX_HEIGHT) // 2


# --- Where in a voyage each scene plays --------------------------------------

@pytest.fixture
def scenes_played(monkeypatch):
    """Every scene the game asks for, by class, with the save as last committed
    at the moment it was asked: a scene must show what is already kept."""
    played: list[tuple[str, bool]] = []
    state = {"world": None, "committed": None}

    def record(make):
        scene = make(80, 24)
        world = state["world"]
        current = json.dumps(world.save.to_dict(), sort_keys=True) if world is not None else None
        played.append((type(scene).__name__, current == state["committed"]))
        return True

    def watch(world):
        state["world"] = world
        world._checkpoint = lambda w: state.update(committed=json.dumps(w.save.to_dict(), sort_keys=True))
        return played

    monkeypatch.setattr(vr, "play_scene", record)
    return watch


def test_a_quiet_hop_jumps_and_docks_after_each_commit(scenes_played, monkeypatch):
    world = _world_with_seed(42)
    played = scenes_played(world)
    dest_id = sorted(world.here.connections)[0]
    world.event_rng.random = lambda: 0.99  # no encounter, no patrol, no customs
    monkeypatch.setattr(vr, "read_key", lambda: pytest.fail("a quiet hop asks nothing"))
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, dest_id)
    assert played == [("_JumpScene", True), ("_DockScene", True)]


def test_a_fresh_fight_opens_on_contact_and_a_kill_ends_it(scenes_played, monkeypatch):
    world, pirate = _world_with_pending_fight()
    played = scenes_played(world)
    world.save.pending_travel["encounter"] = {}
    pirate.hp = 1
    monkeypatch.setattr(vr, "read_key", lambda: "F")
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.screen_combat(vr.Palette(False), world, pirate) == "won"
    assert played == [("_ContactScene", True), ("_KillScene", True)]
    # Resumed with the outcome kept, the fight replays nothing.
    played.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.screen_combat(vr.Palette(False), world, pirate) == "won"
    assert played == []


def test_a_resumed_fight_does_not_announce_its_contact_again(scenes_played, monkeypatch):
    world, pirate = _world_with_pending_fight()
    played = scenes_played(world)
    world.save.pending_travel["encounter"]["combat"]["pirate"]["hp"] = 1
    monkeypatch.setattr(vr, "read_key", lambda: "F")
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.screen_combat(vr.Palette(False), world, pirate) == "won"
    assert [name for name, _ in played] == ["_KillScene"]


def test_a_bribe_is_a_conversation_not_a_spectacle(scenes_played, monkeypatch):
    world, pirate = _world_with_pending_fight()
    played = scenes_played(world)
    world.save.pilot.credits = 100_000
    world.event_rng.random = lambda: 0.0  # the bribe is taken
    monkeypatch.setattr(vr, "read_key", lambda: "P")
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.screen_combat(vr.Palette(False), world, pirate) == "escaped"
    assert played == []


def test_the_contact_scene_tells_a_bounty_and_a_squadron_apart(monkeypatch):
    seen = []
    monkeypatch.setattr(vr, "play_scene", lambda make: seen.append(make(80, 24)) or True)
    world, pirate = _world_with_pending_fight()
    vr.play_contact_scene(world, pirate, {"warrant": {"engaged": False}}, patrol=False)
    pirates = [{"name": "Red Jackal"}, {"name": "Gutter Saint"}]
    vr.play_contact_scene(world, pirate, {"pirates": pirates, "index": 1}, patrol=False)
    vr.play_contact_scene(world, pirate, {}, patrol=True)
    assert [scene.kind for scene in seen] == ["bounty", "squadron", "patrol"]
    assert seen[1].target == 1 and seen[1].count == 2


def test_a_customs_inspection_sweeps_before_its_terms(scenes_played, monkeypatch):
    world = _world_with_seed(42)
    played = scenes_played(world)
    _add_cargo(world, vr.CONTRABAND_COMMODITIES[0], 4)
    world.commit()
    monkeypatch.setattr(vr, "read_key", lambda: "S")
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_customs(vr.Palette(False), world)
    assert played == [("_ScanScene", True)]
