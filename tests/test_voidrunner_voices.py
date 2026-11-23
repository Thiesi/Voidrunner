"""The crew say a line when something happens (review).

A specialist was a name, a line of character on the roster, a bonus and a
wage. These pin that every one of the nine has something to say at the moments
their role speaks to, that a line fits a result row, that the talk draws nothing
from the event RNG, who speaks first when two moments coincide, and that a hop
carries the line to the deck, and the moments the game has added since: a
station taken in a fight, a raider run from, a score settled, a new sector and
the morning after a wreck.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from .support import _world_with_seed, vr

MOMENTS = {"gunner": {"kill", "hull", "called", "settled", "ran"}, "engineer": {"hull", "fuel", "mend", "wreck"},
           "navigator": {"chart", "market", "danger", "slip", "sector"}}


def _crewed(*roles: str, day: int = 2):
    world = _world_with_seed(42)
    for role in roles:
        setattr(world.save.ship, f"has_{role}", True)
    world.save.turn = day
    return world


def _say(world, *, new_system: bool = False, kills: int = 0) -> str | None:
    return vr.crew_voice(world, new_system=new_system, kills_before=world.save.pilot.kills - kills)


def test_every_specialist_has_two_lines_for_each_moment_their_role_speaks_to():
    names = {name: role for role, candidates in vr.CREW_CANDIDATES.items() for name, _ in candidates}
    assert set(vr.CREW_VOICES) == set(names)
    for name, voices in vr.CREW_VOICES.items():
        assert set(voices) == MOMENTS[names[name]], name
        for lines in voices.values():
            assert len(lines) == 2 and lines[0] != lines[1]
            for line in lines:
                assert 8 <= len(line) <= 56 and '"' not in line and line[-1] in ".!?", line
    every = [line for voices in vr.CREW_VOICES.values() for lines in voices.values() for line in lines]
    assert len(set(every)) == len(every)  # nobody says another's line


def test_a_ship_without_crew_is_silent():
    world = _world_with_seed(42)
    world.save.pilot.kills = 3
    world.save.ship.hull_hp = 5
    world.save.ship.fuel = 0
    assert _say(world, new_system=True, kills=1) is None


def test_a_gunner_speaks_of_a_kill_every_second_day_and_changes_the_line():
    said = []
    for day in range(2, 10):
        world = _crewed("gunner", day=day)
        world.save.pilot.kills = 1
        said.append(_say(world, kills=1))
    name = vr.crew_name(_crewed("gunner"), "gunner")
    assert [line is not None for line in said] == [True, False] * 4
    spoken = [line for line in said if line]
    assert all(line.startswith(f'{name}: "') and line.endswith('"') for line in spoken)
    assert spoken[0] != spoken[1] and spoken[0] == spoken[2]
    assert {line.split('"')[1] for line in spoken} == set(vr.CREW_VOICES[name]["kill"])


def test_a_hull_in_a_bad_way_is_always_spoken_to_by_the_engineer_or_failing_one_the_gunner():
    for day in (2, 3):
        world = _crewed("engineer", "gunner", day=day)
        world.save.ship.hull_hp = vr.hull_hp_max(world.save.ship) // 3
        assert _say(world).startswith(vr.crew_name(world, "engineer"))
        world.save.ship.has_engineer = False
        assert _say(world).startswith(vr.crew_name(world, "gunner"))
    world = _crewed("engineer")
    assert _say(world) is None  # an intact hull and a full tank: nothing to say


def test_a_navigator_speaks_of_a_new_system_its_market_and_its_danger():
    world = _crewed("navigator")
    name = vr.crew_name(world, "navigator")
    home = vr.sector_for(world.by_id[0])  # a first dock in another sector is a moment of its own
    plain_system = next(s for s in world.galaxy if s.id and s.id not in world.traits and s.danger < 4
                        and vr.sector_for(s) == home)
    trait_system = next(s for s in world.galaxy if s.id in world.traits and s.danger < 4 and vr.sector_for(s) == home)
    world.save.current_system = plain_system.id
    assert _say(world, new_system=True).split('"')[1] in vr.CREW_VOICES[name]["chart"]
    assert _say(world, new_system=False) is None
    world.save.current_system = trait_system.id
    assert _say(world, new_system=True).split('"')[1] in vr.CREW_VOICES[name]["market"]
    world.save.turn = 3  # a market of its own is always worth a word; a plain chart is not
    assert _say(world, new_system=True).split('"')[1] in vr.CREW_VOICES[name]["market"]
    world.save.current_system = plain_system.id
    assert _say(world, new_system=True) is None
    plain_system.danger = 4
    assert _say(world).split('"')[1] in vr.CREW_VOICES[name]["danger"]


def test_an_engineer_speaks_of_a_tank_nearly_dry():
    world = _crewed("engineer")
    world.save.ship.fuel = vr.CREW_LOW_FUEL
    assert _say(world).split('"')[1] in vr.CREW_VOICES[vr.crew_name(world, "engineer")]["fuel"]
    world.save.ship.fuel = vr.CREW_LOW_FUEL + 1
    assert _say(world) is None


def test_one_line_a_hop_and_the_kill_comes_first():
    world = _crewed("gunner", "engineer", "navigator")
    world.save.pilot.kills = 1
    world.save.ship.hull_hp = 5
    world.save.ship.fuel = 0
    line = _say(world, new_system=True, kills=1)
    assert line.startswith(vr.crew_name(world, "gunner")) and line.count('"') == 2


def test_the_talk_draws_nothing_from_the_event_rng():
    world = _crewed("gunner", "engineer", "navigator")
    world.save.pilot.kills = 1
    state = world.event_rng.getstate()
    before = world.save.to_dict()
    for day in range(1, 8):
        world.save.turn = day
        _say(world, new_system=True, kills=1)
    world.save.turn = before["turn"]
    assert world.event_rng.getstate() == state and world.save.to_dict() == before


def test_a_hop_carries_the_line_to_the_deck():
    world = _crewed("navigator", day=1)
    world.save.pilot.credits = 5_000
    world.event_rng.random = lambda: 1.0  # no encounter, no inspection
    target = next(world.by_id[sid] for sid in world.here.connections
                  if vr.sector_for(world.by_id[sid]) == vr.sector_for(world.here))
    target.discovered = False
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, target.id)
    assert world.save.turn == 2 and world.save.current_system == target.id
    name = vr.crew_name(world, "navigator")
    spoken = [line for line in world.hop_report if line.startswith(f"{name}: ")]
    assert len(spoken) == 1
    assert spoken[0].split('"')[1] in vr.CREW_VOICES[name]["market" if target.id in world.traits else "chart"]


@pytest.mark.parametrize("role", list(MOMENTS))
def test_each_role_has_a_moment_it_always_speaks_at(role):
    assert MOMENTS[role] & set(vr.CREW_VOICE_ALWAYS)
    assert all(vr.CREW_VOICE_ROLE[moment] in MOMENTS for moment in vr.CREW_VOICE_ROLE)


# --- the moments the game has added since (review) -----------------------------------------------


def test_each_specialist_speaks_to_the_station_they_took_in_the_fight():
    for role, event, moment in (("gunner", "called", "called"), ("engineer", "mend", "mend"), ("navigator", "slip", "slip")):
        world = _crewed(role)
        world.hop_events = {event}
        world.save.pilot.kills = 1
        line = _say(world, kills=1)
        assert line.split('"')[1] in vr.CREW_VOICES[vr.crew_name(world, role)][moment], role
        # Unused, the station is nothing to speak of.
        world.hop_events = set()
        said = _say(world, kills=1)
        assert said is None or said.split('"')[1] not in vr.CREW_VOICES[vr.crew_name(world, role)][moment]


def test_the_rule_functions_note_what_was_done_on_the_hop():
    world = _crewed("gunner", "engineer", "navigator")
    world.event_rng.seed(1)
    pirate = vr.Pirate("Ashclaw", 1, 35, 35)
    tactics = vr.new_tactics(pirate)
    assert world.hop_events == set()
    vr.tactical_round(world, pirate, tactics, "C")
    vr.tactical_round(world, pirate, tactics, "N")
    world.save.ship.hull_hp = max(1, world.save.ship.hull_hp - 20)
    vr.tactical_mend(world, pirate, tactics)
    assert world.hop_events == {"called", "slip", "mend"}
    vr.remember_rival(world, pirate, "paid")
    assert "ran" not in world.hop_events
    vr.remember_rival(world, pirate, "ran")
    vr.settle_rival(world, pirate)
    assert {"ran", "settled"} <= world.hop_events


def test_a_score_settled_is_always_the_gunners_and_comes_before_the_kill():
    for day in (2, 3):
        world = _crewed("gunner", day=day)
        world.hop_events = {"settled", "called"}
        world.save.pilot.kills = 1
        assert _say(world, kills=1).split('"')[1] in vr.CREW_VOICES[vr.crew_name(world, "gunner")]["settled"]
    world = _crewed("gunner")
    world.hop_events = {"ran"}
    assert _say(world).split('"')[1] in vr.CREW_VOICES[vr.crew_name(world, "gunner")]["ran"]


def test_the_morning_after_a_wreck_is_the_engineers_and_nobody_elses():
    for day in (2, 3):
        world = _crewed("gunner", "engineer", "navigator", day=day)
        world.save.ship.hull_hp = 5
        world.save.ship.fuel = 0
        line = vr.crew_voice(world, new_system=False, kills_before=world.save.pilot.kills, wrecked=True)
        assert line.split('"')[1] in vr.CREW_VOICES[vr.crew_name(world, "engineer")]["wreck"]
    world = _crewed("gunner", "navigator")
    world.save.ship.hull_hp = 5
    assert vr.crew_voice(world, new_system=False, kills_before=0, wrecked=True) is None


def test_a_navigator_speaks_of_the_first_dock_in_a_sector_the_ship_has_never_been_in():
    world = _crewed("navigator", day=3)
    name = vr.crew_name(world, "navigator")
    home = vr.sector_for(world.by_id[0])
    away = next(s for s in world.galaxy if vr.sector_for(s) != home)
    world.save.current_system = away.id
    assert _say(world, new_system=True).split('"')[1] in vr.CREW_VOICES[name]["sector"]
    # Once a station of that sector has been docked at, it is a sector like any other.
    other = next(s for s in world.galaxy if vr.sector_for(s) == vr.sector_for(away) and s.id != away.id)
    here = world.save.current_system
    world.save.current_system = other.id
    vr.remember_local_market(world)
    world.save.current_system = here
    said = _say(world, new_system=False)
    assert said is None or said.split('"')[1] not in vr.CREW_VOICES[name]["sector"]
    # Freeport's own sector is where the career began: never new.
    near = next(s for s in world.galaxy if s.id and vr.sector_for(s) == home and s.id not in world.traits and s.danger < 4)
    world.save.current_system = near.id
    assert _say(world, new_system=False) is None


def test_a_hop_that_ends_under_tow_carries_the_engineers_line_to_the_deck(monkeypatch):
    world = _crewed("engineer", "navigator", day=1)
    world.save.pilot.credits = 5_000
    world.event_rng.random = lambda: 1.0
    target = next(iter(sorted(world.here.connections)))

    def wreck(p, w, dest):
        vr.report_hop(w, [vr.destroy_ship(w)])

    monkeypatch.setattr(vr, "_resolve_random_travel_encounter", wreck)
    monkeypatch.setattr(vr, "play_scene", lambda make: True)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, target)
    assert world.save.current_system == 0 and world.save.tales == {}  # towed home: no dock, no story
    name = vr.crew_name(world, "engineer")
    spoken = [line for line in world.hop_report if line.startswith(f"{name}: ")]
    assert len(spoken) == 1 and spoken[0].split('"')[1] in vr.CREW_VOICES[name]["wreck"]
