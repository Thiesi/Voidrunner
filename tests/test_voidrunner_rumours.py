"""Dockside talk points at a market worth finding (review).

A trait was found only by docking, so the way to look for one was to go
everywhere. These pin what the dock may say: only true things, only about
systems near enough to act on that the pilot has not docked at, the lean and
never the good, an uncharted system by its bearing and never its name; that the
chart keeps what was heard until the pilot has seen for themselves; that the
talk draws nothing from the event RNG; and that a hop carries it to the deck.
"""

from __future__ import annotations

import contextlib
import io

from .support import _world_with_seed, plain, vr


def _docked_days(world, days=range(1, 400)):
    """Every line the dock would say standing here on each of `days`, with the
    chart cleared between them."""
    said = []
    for day in days:
        world.save.turn = day
        heard = dict(world.save.flags)
        line = vr.dock_rumour(world)
        if line:
            new = [key for key in world.save.flags if key not in heard]
            said.append((day, line, int(new[0].split("_")[1])))
        world.save.flags = heard
    return said


def test_the_dock_talks_about_one_docking_in_three_and_only_says_true_things():
    spoken = total = 0
    for seed in range(40, 50):
        world = _world_with_seed(seed)
        if not vr.rumour_candidates(world):
            continue
        said = _docked_days(world, range(1, 121))
        spoken, total = spoken + len(said), total + 120
        for _day, line, system_id in said:
            trait = world.traits[system_id]
            assert vr.RUMOUR_WHAT[trait["kind"]] in line
            assert vr.COMMODITIES[trait["commodity"]]["label"] not in line  # the lean, never the good
            assert line.split(" ")[0] in {voice.split(" ")[0] for voice in vr.RUMOUR_VOICES[world.here.economy]}
            assert len(line) <= 110 and line.endswith(".")
    assert total and 0.25 <= spoken / total <= 0.42


def test_it_talks_only_about_systems_near_enough_to_act_on():
    world = _world_with_seed(42)
    here = world.here
    for system_id, via in vr.rumour_candidates(world):
        assert system_id in world.traits and system_id != here.id
        if via is None:
            assert system_id in here.connections
        else:
            assert via in here.connections and world.by_id[via].discovered
            assert system_id in world.by_id[via].connections and system_id not in here.connections
    # The next jump out is talked about before anything two jumps out.
    order = [via is None for _system, via in vr.rumour_candidates(world)]
    assert order == sorted(order, reverse=True)


def test_an_uncharted_system_is_a_bearing_and_never_a_name():
    for seed in range(40, 56):
        world = _world_with_seed(seed)
        for station in world.galaxy:
            station.discovered = station.id == 0 or station.id in world.here.connections and station.id % 2 == 0
        for _day, line, system_id in _docked_days(world, range(1, 60)):
            system = world.by_id[system_id]
            if system.discovered:
                assert system.name in line
            else:
                assert system.name not in line and f"the bearing at {system.x},{system.y}" in line


def test_what_is_heard_is_on_the_chart_until_the_pilot_has_seen_for_themselves():
    world = _world_with_seed(42)
    day, line, system_id = _docked_days(world)[0]
    world.save.turn = day
    assert vr.rumoured_trait(world, system_id) is None
    assert vr.dock_rumour(world) == line
    kind = world.traits[system_id]["kind"]
    assert world.save.flags[f"rumour_{system_id}"] is True and vr.rumoured_trait(world, system_id) == kind
    if system_id in world.here.connections:
        chart = " ".join(plain(text) for _sid, text in vr.chart_entries(world))
        assert ("cheap ?" if kind == "surplus" else "dear ?") in chart
    # Nobody tells the same pilot twice, and docking there ends the rumour.
    assert system_id not in dict(vr.rumour_candidates(world))
    here = world.save.current_system
    world.save.current_system = system_id
    vr.remember_local_market(world)
    world.save.current_system = here
    assert vr.rumoured_trait(world, system_id) is None and vr.known_trait(world, system_id) == world.traits[system_id]


def test_the_save_still_loads_with_talk_in_it():
    world = _world_with_seed(42)
    day, _line, _system = _docked_days(world)[0]
    world.save.turn = day
    assert vr.dock_rumour(world)
    again = vr.SaveData.from_dict(world.save.to_dict())
    assert {key for key in again.flags if key.startswith("rumour_")} == {key for key in world.save.flags if key.startswith("rumour_")}


def test_the_talk_draws_nothing_from_the_event_rng_and_a_quiet_dock_changes_nothing():
    world = _world_with_seed(42)
    state = world.event_rng.getstate()
    quiet = next(day for day in range(1, 60)
                 if vr.zlib.crc32(f"rumour:{world.save.seed}:{day}:{world.here.id}".encode()) % vr.RUMOUR_ONE_IN)
    world.save.turn = quiet
    before = world.save.to_dict()
    assert vr.dock_rumour(world) is None and world.save.to_dict() == before
    _docked_days(world, range(1, 60))
    assert world.event_rng.getstate() == state


def test_a_dock_with_nothing_new_nearby_says_nothing():
    world = _world_with_seed(42)
    for system_id, _via in vr.rumour_candidates(world):
        world.save.flags[f"rumour_{system_id}"] = True
    assert vr.rumour_candidates(world) == [] and _docked_days(world, range(1, 40)) == []


def test_a_hop_carries_the_talk_to_the_deck():
    carried = 0
    for seed in range(40, 70):
        world = _world_with_seed(seed)
        world.save.pilot.credits = 5_000
        world.event_rng.random = lambda: 1.0  # no encounter, no inspection
        target = sorted(world.here.connections)[0]
        with contextlib.redirect_stdout(io.StringIO()):
            vr.screen_travel(vr.Palette(False), world, target)
        heard = [key for key in world.save.flags if key.startswith("rumour_")]
        talk = [line for line in world.hop_report if any(what in line for what in vr.RUMOUR_WHAT.values())]
        assert len(talk) == len(heard) <= 1
        carried += len(talk)
    assert carried >= 3  # of thirty first jumps, some docks had something to say
