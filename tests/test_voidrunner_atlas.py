"""The Viewport's station view is an atlas of every charted world (review).

The Viewport showed three pictures of things already on the deck. These pin the
atlas: which systems it turns through and in what order, what it says of each
and that it says nothing a pilot has not learned, that the picture and the text
portrait carry the same facts, how the keys turn its pages, that the yard still
opens the ship, and that nothing is written by looking.
"""

from __future__ import annotations

import contextlib
import copy
import io

import pytest

from .support import _world_with_seed, plain, vr


def _charted(seed: int = 42, count: int = 6):
    world = _world_with_seed(seed)
    for station in world.galaxy:
        station.discovered = station.id < count
    return world


def _dock(world, system_id: int, day: int):
    here, today = world.save.current_system, world.save.turn
    world.save.current_system, world.save.turn = system_id, day
    world.by_id[system_id].discovered = True
    vr.remember_local_market(world)
    world.save.current_system, world.save.turn = here, today


def test_the_atlas_turns_through_every_charted_system_by_sector_then_name():
    world = _charted()
    systems = vr.atlas_systems(world)
    assert set(systems) == {s.id for s in world.galaxy if s.discovered} and len(systems) == 6
    keys = [(vr.SECTOR_NAMES.index(vr.sector_for(world.by_id[sid])), world.by_id[sid].name) for sid in systems]
    assert keys == sorted(keys)
    # Where the ship is docked is in it even if the chart has somehow not caught up.
    world.here.discovered = False
    assert world.here.id in vr.atlas_systems(world)


def test_it_says_what_is_known_of_a_system_and_nothing_else():
    world = _charted()
    unseen = next(s for s in world.galaxy if s.discovered and s.id and s.id in world.traits)
    facts = dict((label or value, value) for label, value in vr.atlas_facts(world, unseen.id))
    assert facts["market "] == "not yet seen" and "charted, never docked" in facts
    assert vr.COMMODITIES[world.traits[unseen.id]["commodity"]]["label"] not in " ".join(facts.values())
    # Heard about on a dock: the lean, still not the good.
    world.save.flags[f"rumour_{unseen.id}"] = True
    facts = dict((label or value, value) for label, value in vr.atlas_facts(world, unseen.id))
    kind = world.traits[unseen.id]["kind"]
    assert facts["market "] == "said to have something " + ("cheap" if kind == "surplus" else "dear")
    # Docked there: the trait itself, and the day.
    _dock(world, unseen.id, 9)
    facts = dict((label or value, value) for label, value in vr.atlas_facts(world, unseen.id))
    assert facts["market "] == vr.trait_label(world.traits[unseen.id]) and facts["last docked "] == "day 9"
    assert "charted, never docked" not in facts


def test_the_page_for_here_says_so_and_every_page_says_how_much_of_the_galaxy_it_is():
    world = _charted()
    _dock(world, 0, 0)
    _dock(world, 3, 4)
    here = vr.atlas_facts(world, world.here.id)
    assert ("", "docked here now") in here
    systems = vr.atlas_systems(world)
    for sid in systems:
        last = vr.atlas_facts(world, sid)[-1]
        assert last == ("world ", f"{systems.index(sid) + 1} of 6 charted; 2 of {len(world.galaxy)} docked")
    assert vr.docked_systems(world.save) == 2


def test_a_stake_and_a_workshop_are_on_their_systems_pages():
    world = _charted(count=48)
    workshop_system = next(iter(vr.specialist_stations(world).values()))
    assert any(label == "Specialist: " for label, _value in vr.atlas_facts(world, workshop_system))
    world.save.flags["stake_3_1"] = world.save.flags["stake_3_2"] = True
    assert ("your stake ", f"step 2 of {len(vr.STAKE_COSTS)}") in vr.atlas_facts(world, 3)
    assert not any(label == "your stake " for label, _value in vr.atlas_facts(world, 4))


def test_the_picture_and_the_text_portrait_carry_the_same_facts():
    world = _charted()
    _dock(world, 2, 5)
    place = world.by_id[2]
    scene = vr.viewport_scene(world, "2", 80, 24, system=2)
    caption = "".join(piece for row in scene.lines for piece, _color in row)
    _large, _compact, details, title = vr.viewport_content(world, "2", system=2)
    text = " ".join(details)
    assert title == place.economy and scene.title == place.station_name.upper()
    for label, value in vr.atlas_facts(world, 2):
        assert vr._scene_text(value) in caption
        assert value in text or value[:1].upper() + value[1:] in text
    # Each world is drawn as itself: another system's page is another picture.
    other = vr.viewport_scene(world, "2", 80, 24, system=3)
    assert other.frame(other.still) != scene.frame(scene.still)
    # And with no page named, it is the world outside, as the station view always was.
    assert vr.viewport_scene(world, "2", 80, 24).title == world.here.station_name.upper()


def test_the_bar_offers_the_atlas_its_keys_and_only_there():
    assert vr.viewport_footer(80, "2") == "[1] Ship [2] Atlas [3] Discovery [N/P] World [B] Back: "
    assert vr.viewport_footer(80, "1") == "[1] Ship [2] Atlas [3] Discovery [B] Back: "
    assert vr.viewport_footer(40, "2") == "[1-3] View [N/P] World [B] Back: "
    assert vr.viewport_footer(40, "3") == "[1-3] View [B] Back: "


def _walk(monkeypatch, world, keys, view=None):
    frames, output = [], io.StringIO()
    typed = iter(keys)

    def choose():
        frames.append(plain(output.getvalue()))
        output.seek(0)
        output.truncate(0)
        return next(typed)

    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output):
        vr.screen_viewport(vr.Palette(False), world) if view is None else vr.screen_viewport(vr.Palette(False), world, view)
    return frames


def test_the_keys_turn_the_atlas_and_looking_writes_nothing(monkeypatch):
    world = _charted()
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    world._checkpoint = lambda w: pytest.fail("the atlas wrote a save")
    systems = vr.atlas_systems(world)
    start = systems.index(world.here.id)
    frames = _walk(monkeypatch, world, ["N", "N", "P", "P", "P", "1", "N", "2", "B"])
    names = [world.by_id[systems[(start + step) % len(systems)]].name for step in (0, 1, 2, 1, 0, -1)]
    for frame, name in zip(frames, names):
        assert name in frame and "[N/P] World" in frame
    # On the ship's view [N] turns nothing, and the atlas is where it was left.
    assert "[N/P] World" not in frames[6] and frames[6] == frames[7]
    assert names[-1] in frames[8]
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


def test_the_deck_opens_the_atlas_and_the_yard_opens_the_ship(monkeypatch):
    world = _charted()
    assert world.here.name in _walk(monkeypatch, world, ["B"])[0]
    ship = _walk(monkeypatch, world, ["B"], view="1")[0]
    assert world.save.ship.hull_class.upper() in ship.upper() and "[N/P] World" not in ship


def test_two_achievements_count_a_careers_harbors():
    world = _charted(count=48)
    ids = {accolade.id: accolade for accolade in vr.ACCOLADES}
    assert ids["harbor_12"].target == 12 and ids["harbor_36"].target == 36
    for system_id in range(11):
        _dock(world, system_id, system_id)
    assert "harbor_12" not in {accolade.id for accolade in vr.unlock_accolades(world.save)}
    _dock(world, 11, 11)
    assert "harbor_12" in {accolade.id for accolade in vr.unlock_accolades(world.save)}
