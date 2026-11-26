"""The three sector rules that are about danger and the law (review).

The Verge is patrolled, the Long Dark keeps a lamp, and the Fringe is
nobody's. These pin what each changes and only that, that the luck of an
encounter is the luck it was, and that every sector now has a rule and says it.
"""

from __future__ import annotations

import contextlib
import io
import random

import pytest

from .support import _world_with_seed, plain, vr
from .test_voidrunner_sector_rules import _put


# --- Coreward Verge: patrolled ----------------------------------------------------

def test_half_the_raiders_never_come_in_the_verge_and_the_draw_is_the_one_it_was():
    world = _world_with_seed(42)
    system = world.galaxy[5]
    kinds, weights = vr.encounter_kinds(_put(system, "The Long Dark"))
    assert kinds == list(vr.TRAVEL_ENCOUNTER_WEIGHTS) and weights == [55.0, 15.0, 15.0, 15.0]
    kinds, weights = vr.encounter_kinds(_put(system, "Coreward Verge"))
    assert kinds == ["pirate", "derelict", "distress", "tip", "none"] and weights == [27.5, 15.0, 15.0, 15.0, 27.5]
    assert sum(weights) == sum(vr.TRAVEL_ENCOUNTER_WEIGHTS.values())  # everything else is exactly as likely as it was
    # Flown: the same seeds meet about half the raiders, each on its one draw.
    met = {"Coreward Verge": 0, "The Long Dark": 0}
    for sector in met:
        _put(system, sector)
        for seed in range(2000):
            kinds, weights = vr.encounter_kinds(system)
            met[sector] += random.Random(seed).choices(kinds, weights=weights)[0] == "pirate"
    assert 0.42 < met["Coreward Verge"] / met["The Long Dark"] < 0.58


def test_customs_are_twice_as_strict_in_the_verge_and_absent_from_the_fringe():
    world = _world_with_seed(42)
    system = world.galaxy[5]
    for danger in range(6):
        system.danger = danger
        usual = vr.customs_check_chance(_put(system, "The Long Dark"))
        assert usual == pytest.approx(max(0.0, 0.15 + (5 - danger) * 0.03))
        assert vr.customs_check_chance(_put(system, "Coreward Verge")) == pytest.approx(min(0.95, 2 * usual))
        assert vr.customs_check_chance(_put(system, "Outer Fringe")) == 0.0
        assert all(vr.customs_check_chance(_put(system, sector)) == pytest.approx(usual)
                   for sector in ("Auroral Span", "Farrider's Edge", "Hollow Reach"))


def test_a_contract_finished_in_the_verge_earns_one_more_concord_standing():
    usual = vr.CONCORD_STANDING_PER_CONTRACT
    for sector, standing in (("Coreward Verge", usual + 1), ("The Long Dark", usual)):
        world = _world_with_seed(42)
        target = sorted(world.here.connections)[0]
        _put(world.by_id[target], sector)
        world.save.active_missions = [vr.Mission(id=1, kind="scan", description="Survey", reward=500, origin_system=0,
                                                 target_system=target)]
        world.save.current_system = target
        before = world.save.pilot.reputation[vr.FACTION_CONCORD]
        lines = vr.check_mission_completions(world, just_discovered=target)
        assert world.save.pilot.reputation[vr.FACTION_CONCORD] == before + standing
        assert f"+{standing} Concord standing" in lines[0]


# --- The Long Dark: the lamp ----------------------------------------------------------

def test_a_jump_into_the_dark_burns_one_more_fuel_and_no_engineer_saves_it(monkeypatch):
    world = _world_with_seed(42)
    here, ship = world.here, world.save.ship
    target = world.by_id[sorted(here.connections)[0]]
    # The same two stations, the same distance apart: only which sector the far one is in changes.
    monkeypatch.setattr(vr, "sector_for", lambda system: "Auroral Span")
    usual = vr.fuel_cost_for_jump(here, target, ship)
    monkeypatch.setattr(vr, "sector_for", lambda system: "The Long Dark" if system is target else "Auroral Span")
    assert vr.fuel_cost_for_jump(here, target, ship) == usual + vr.DARK_FUEL == usual + 1
    assert vr.fuel_cost_for_jump(target, here, ship) == usual   # out of the Dark costs what any jump of its length does
    ship.has_engineer = True
    dark = vr.fuel_cost_for_jump(here, target, ship)
    monkeypatch.setattr(vr, "sector_for", lambda system: "Auroral Span")
    assert dark == vr.fuel_cost_for_jump(here, target, ship) + 1


def test_a_dock_in_the_dark_puts_a_jumps_worth_in_the_tank_and_no_more_than_fits():
    world = _world_with_seed(42)
    ship = world.save.ship
    _put(world.here, "The Long Dark")
    worth = min(vr.fuel_cost_for_jump(world.here, world.by_id[sid], ship) for sid in world.here.connections)
    ship.fuel = 5
    assert vr.lamp_top_up(world) == [
        f"The Long Dark keeps its lamp lit: {vr.plural(worth, 'unit')} of fuel on the dock, no charge."]
    assert ship.fuel == 5 + worth
    ship.fuel = vr.fuel_capacity(ship)
    assert vr.lamp_top_up(world) == [] and ship.fuel == vr.fuel_capacity(ship)   # a full tank takes nothing
    ship.fuel = vr.fuel_capacity(ship) - 1
    assert "1 unit of fuel" in vr.lamp_top_up(world)[0] and ship.fuel == vr.fuel_capacity(ship)
    _put(world.here, "Auroral Span")
    ship.fuel = 5
    assert vr.lamp_top_up(world) == [] and ship.fuel == 5                        # and no other sector has a lamp


def test_a_real_jump_into_the_dark_burns_the_unit_and_the_dock_hands_a_jump_back(monkeypatch):
    world = _world_with_seed(42)
    here, ship = world.here, world.save.ship
    target = world.by_id[sorted(here.connections)[0]]
    _put(target, "The Long Dark")
    _put(here, "Auroral Span")
    burn = vr.fuel_cost_for_jump(here, target, ship)
    worth = min(vr.fuel_cost_for_jump(target, world.by_id[sid], ship) for sid in target.connections)
    ship.fuel = 20
    world.event_rng.random = lambda: 1.0   # no encounter, no inspection
    monkeypatch.setattr(vr, "play_scene", lambda make: True)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_travel(vr.Palette(False), world, target.id)
    assert world.save.current_system == target.id and ship.fuel == 20 - burn + worth
    said = " ".join(plain(output.getvalue()).split()) + " ".join(world.hop_report) + " ".join(world.save.pilot.log)
    assert ship.fuel > 20 - burn and ("keeps its lamp lit" in said or worth > 0)


# --- Outer Fringe: lawless ------------------------------------------------------------

def test_raiders_are_a_tier_stronger_in_the_fringe_and_the_luck_is_the_same():
    world = _world_with_seed(42)
    system = world.galaxy[5]
    for danger in range(5):
        for seed in range(40):
            world.event_rng.seed(seed)
            usual = vr.generate_pirate(world, danger=danger, system=_put(system, "The Long Dark"))
            state = world.event_rng.getstate()
            world.event_rng.seed(seed)
            fringe = vr.generate_pirate(world, danger=danger, system=_put(system, "Outer Fringe"))
            assert fringe.tier == min(4, usual.tier + 1) and fringe.name == usual.name
            assert world.event_rng.getstate() == state
    assert vr.generate_pirate(world, 2, danger=3, system=_put(system, "Outer Fringe")).tier == 2   # a given tier stays given
    assert vr.raider_tier_span(world, 2, _put(system, "Outer Fringe")) == (2, 4)
    assert vr.raider_tier_span(world, 2, _put(system, "The Long Dark")) == (1, 3) == vr.raider_tier_span(world, 2)
    world.save.charter = "hard"   # the hard charter and the Fringe both, and still no more than the top named tier
    assert vr.raider_tier_span(world, 2, _put(system, "Outer Fringe")) == (3, 4)
    assert vr.raider_tier_span(world, 5, system) == (4, 5)   # danger 5 sends what it sent


def test_the_chart_warns_of_the_fringes_raiders():
    world = _world_with_seed(42)
    target = world.by_id[sorted(world.here.connections)[0]]
    target.discovered, target.danger = True, 2
    _put(world.here, "Auroral Span")
    assert "outclass" not in vr.departure_terms(world, _put(target, "Auroral Span").id)
    assert "Raiders there can outclass this hull." in vr.departure_terms(world, _put(target, "Outer Fringe").id)


# --- all six --------------------------------------------------------------------------

def test_every_sector_has_a_rule_and_its_file_says_it():
    assert set(vr.SECTOR_RULES) == set(vr.SECTOR_NAMES)
    names = [vr.SECTOR_RULES[sector][0] for sector in vr.SECTOR_NAMES]
    assert names == ["Patrolled", "Restless seasons", "Far out", "Old workings", "The lamp", "Lawless"]
    world = _world_with_seed(42)
    for system in world.galaxy:
        system.discovered = True
    world.sync_discovered()
    world.save.tales = {sector: 1 for sector in vr.SECTOR_NAMES}
    file = " ".join(" ".join(plain(line).split()) for line in vr.sector_file_lines(world))
    told = 0
    for sector, (name, text) in vr.SECTOR_RULES.items():
        if vr.sector_stations(world, sector):
            assert f"{name}. {text}" in file
            told += 1
    assert told >= 4
