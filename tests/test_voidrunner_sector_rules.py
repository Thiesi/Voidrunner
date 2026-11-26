"""A rule for each sector (review).

A sector was a name, a season and a story. These pin the one rule each sector
now has, taken from its story: what it changes and only that, that it is the
sector's and so true of every station in it, where a pilot is told, and that
nothing is saved for it.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import math
import sys
from pathlib import Path

import pytest

from .support import _world_with_seed, plain, vr

ROOT = Path(__file__).resolve().parent.parent
#: A point inside each sector of the three-by-two grid.
AT = {"Coreward Verge": (10, 10), "Auroral Span": (50, 10), "Farrider's Edge": (80, 10),
      "Hollow Reach": (10, 40), "The Long Dark": (50, 40), "Outer Fringe": (80, 40)}


def _put(system, sector):
    """Move a system into a sector: its position is what gives it one."""
    system.x, system.y = AT[sector]
    assert vr.sector_for(system) == sector
    return system


def _world_in(sector, seed=42):
    """A pilot docked at a station of `sector`, with a neighbour in a sector that has no rule about money."""
    world = _world_with_seed(seed)
    _put(world.here, sector)
    return world


def test_the_grid_puts_a_point_in_each_sector_and_every_rule_names_a_sector():
    world = _world_with_seed(42)
    system = world.galaxy[3]
    assert [vr.sector_for(_put(system, sector)) for sector in vr.SECTOR_NAMES] == vr.SECTOR_NAMES
    assert set(vr.SECTOR_RULES) <= set(vr.SECTOR_NAMES)
    for sector, (name, text) in vr.SECTOR_RULES.items():
        assert name and text.endswith(".") and vr.sector_rule_line(sector) == f"{sector}, {name.lower()}: {text}"
    assert vr.sector_rule_line("Nowhere") is None


# --- Auroral Span: seasons swing twice as far -------------------------------------

def test_a_season_in_the_span_swings_twice_as_far_and_turns_when_it_would_have():
    world = _world_with_seed(42)
    system = world.galaxy[5]
    for commodity in vr.LEGAL_COMMODITIES:
        for day in (0, 3, 9, 17, 28):
            usual = vr.season_factor(world, _put(system, "The Long Dark").id, commodity, day)
            period, sign = vr._season_of(world.save.seed, "The Long Dark", commodity)
            assert usual == pytest.approx(1 + vr.SEASON_SWING * sign * math.sin(2 * math.pi * day / period))
            span = vr.season_factor(world, _put(system, "Auroral Span").id, commodity, day)
            period, sign = vr._season_of(world.save.seed, "Auroral Span", commodity)
            assert span == pytest.approx(1 + 2 * vr.SEASON_SWING * sign * math.sin(2 * math.pi * day / period))
    assert vr.sector_season_swing("Auroral Span") == 2 * vr.SEASON_SWING == 0.24
    assert all(vr.sector_season_swing(sector) == vr.SEASON_SWING for sector in vr.SECTOR_NAMES if sector != "Auroral Span")
    # Contraband has no season anywhere, the Span included.
    assert vr.season_factor(world, _put(system, "Auroral Span").id, "weapons", 9) == 1.0


# --- Hollow Reach: half-price repairs, and ore and metals always in glut ------------

def test_ore_and_metals_are_in_glut_at_every_station_of_the_reach_and_nothing_else_moves():
    world = _world_with_seed(42)
    system = next(s for s in world.galaxy if s.id and s.id not in world.traits and s.danger < 4)
    elsewhere = {c: vr.price_for(world, _put(system, "The Long Dark").id, c) for c in vr.COMMODITIES}
    world.save.turn = 0   # level seasons: day zero
    here = {c: vr.price_for(world, _put(system, "Hollow Reach").id, c) for c in vr.COMMODITIES}
    assert vr.REACH_GLUT == ("ore", "metals") and vr.REACH_GLUT_PRICE == vr.TRAIT_PRICE["surplus"]
    for commodity in vr.COMMODITIES:
        base = vr.COMMODITIES[commodity]["base"] * vr.economy_price_factor(system.economy, commodity)
        if commodity in vr.REACH_GLUT:
            assert here[commodity] == max(1, round(base * 0.7)) < elsewhere[commodity]
            assert (f"Hollow Reach's old workings", 0.7) in vr.price_reasons(world, system.id, commodity)
            assert vr.sell_price_for(world, system.id, commodity) < here[commodity]   # both sides of the counter
        else:
            assert here[commodity] == elsewhere[commodity]
            assert not any("old workings" in words for words, _f in vr.price_reasons(world, system.id, commodity))
    for commodity in vr.COMMODITIES:   # and the reasons still multiply to the quote
        product = math.prod(factor for _w, factor in vr.price_reasons(world, system.id, commodity))
        assert abs(vr.COMMODITIES[commodity]["base"] * product - here[commodity]) <= 1.0


def test_a_yard_in_the_reach_repairs_at_half_and_one_on_the_edge_at_half_as_much_again(monkeypatch):
    ship = vr.starting_ship()
    world = _world_with_seed(42)
    assert vr.repair_cost_per_hp(ship) == vr.REPAIR_COST_PER_HP == 4
    prices = {sector: vr.repair_cost_per_hp(ship, _put(world.here, sector)) for sector in vr.SECTOR_NAMES}
    assert prices == {"Coreward Verge": 4, "Auroral Span": 4, "Farrider's Edge": 6,
                      "Hollow Reach": 2, "The Long Dark": 4, "Outer Fringe": 4}
    ship.has_engineer = True   # an engineer's discount first, then the yard's
    assert [vr.repair_cost_per_hp(ship, _put(world.here, sector)) for sector in ("Hollow Reach", "The Long Dark", "Farrider's Edge")] == [1, 3, 4]
    # The yard charges it, and says why its figure is what it is.
    for sector, unit, said in (("Hollow Reach", 2, "Hollow Reach: repairs at half price"),
                               ("Farrider's Edge", 6, "Farrider's Edge: repairs at half as much again"),
                               ("The Long Dark", 4, None)):
        world = _world_in(sector)
        world.save.ship.hull_hp -= 10
        world.save.pilot.credits = 1_000
        head = plain(vr.shipyard_lines(world)[0])
        assert f"repair {unit}cr/HP" in head.replace("⟦", "").replace("⟧", "") and ((said in head) if said else ("repairs at" not in head))
        monkeypatch.setattr(vr, "confirm", lambda prompt, p, **kw: True)
        with contextlib.redirect_stdout(io.StringIO()):
            vr._repair(vr.Palette(False), world)
        assert world.save.pilot.credits == 1_000 - 10 * unit and world.save.ship.hull_hp == vr.hull_hp_max(world.save.ship)


# --- Farrider's Edge: a survey into it pays double ------------------------------------

def test_a_survey_into_the_edge_pays_double_and_no_other_contract_does():
    world = _world_with_seed(42)
    target = sorted(world.here.connections)[0]
    _put(world.by_id[target], "Farrider's Edge")
    survey = vr.Mission(id=1, kind="scan", description="Survey", reward=900, origin_system=0, target_system=target)
    delivery = vr.Mission(id=2, kind="delivery", description="Deliver", reward=900, origin_system=0, target_system=target,
                          commodity="food", quantity=3)
    assert vr.sector_pay(world, survey) == 1_800 and vr.sector_pay(world, delivery) == 900
    sheet = " ".join(" ".join(plain(line).split()) for line in vr.mission_details(world, survey))
    assert "SECTOR Farrider's Edge: a survey pays double, in the payout" in sheet
    assert "SECTOR" not in " ".join(plain(line) for line in vr.mission_details(world, delivery))
    _put(world.by_id[target], "The Long Dark")
    assert vr.sector_pay(world, survey) == 900
    assert "SECTOR" not in " ".join(plain(line) for line in vr.mission_details(world, survey))


def test_a_board_posts_its_surveys_into_the_edge_at_double():
    doubled = 0
    for seed in range(40, 70):
        world = _world_with_seed(seed)
        hops = vr.bfs_hops(world.by_id, world.here.id)
        for system in world.galaxy:
            if system.id and system.danger < 4:
                _put(system, "Farrider's Edge")
        for mission in vr.generate_mission_board(world):
            if mission.kind == "scan" and world.by_id[mission.target_system].danger < 4:
                assert mission.reward == 2 * (vr.SURVEY_PAY[0] + hops[mission.target_system] * vr.SURVEY_PAY[1])
                doubled += 1
    assert doubled >= 5


# --- where a pilot is told --------------------------------------------------------------

def test_the_docks_say_the_rule_with_the_first_part_of_the_story_and_the_file_keeps_it():
    world = _world_in("Hollow Reach")
    world.save.tales = {}
    lines = vr.sector_tale(world)
    assert lines[0].startswith("Hollow Reach, I: ") and lines[1] == vr.sector_rule_line("Hollow Reach")
    assert lines[1] == "Hollow Reach, old workings: Yards here repair at half price, and raw ore and refined metals are always in glut."
    assert not any("old workings" in line for line in vr.sector_tale(world))  # said once, with the first part
    file = [" ".join(plain(line).split()) for line in vr.sector_file_lines(world)]
    assert "Old workings. Yards here repair at half price, and raw ore and refined metals are always in glut." in file
    # Every sector that has a rule says it the same way, and one that has none says only its story.
    for sector in vr.SECTOR_NAMES:
        other = _world_in(sector)
        other.save.tales = {}
        told = vr.sector_tale(other)
        assert told[0].startswith(f"{sector}, I: ")
        assert told[1:2] == ([vr.sector_rule_line(sector)] if sector in vr.SECTOR_RULES else [])


def test_nothing_is_saved_for_a_sectors_rule():
    world = _world_in("Hollow Reach")
    record = world.save.to_dict()
    assert not any("sector_rule" in key or "rule" == key for key in record)
    assert vr.SaveData.from_dict(record).to_dict() == record


# --- the simulator's table ------------------------------------------------------------------

def test_the_simulator_reports_trade_margin_by_sector():
    spec = importlib.util.spec_from_file_location("career_sim_sector_test", ROOT / "tools" / "career_sim.py")
    sim = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = sim
    spec.loader.exec_module(sim)
    results = [{"sector_margin": {"Hollow Reach": 60_000, "Auroral Span": 90_000, "Outer Fringe": 500},
                "sector_docked": {"Hollow Reach": 30, "Auroral Span": 30, "Outer Fringe": 4}}]
    assert sim.sector_margins(results) == {"Hollow Reach": 2_000.0, "Auroral Span": 3_000.0}  # too few dockings to say
    rows = sim.sector_rows(results)
    assert rows[0].split()[0] == "sector" and rows[1].startswith("Auroral Span") and rows[1].rstrip().endswith("+20%")
    assert rows[2].startswith("Hollow Reach") and rows[2].rstrip().endswith("-20%")
    assert sim.sector_rows([{}]) == []
    career = sim.fly_one((ROOT / "voidrunner.py", "trader", 1005, 20, {}, True, True))
    assert sum(career["sector_docked"].values()) >= 15 and set(career["sector_margin"]) <= set(vr.SECTOR_NAMES)
