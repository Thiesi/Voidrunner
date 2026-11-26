"""Elite raiders: a fifth tier and a pattern of their own, in danger 5 only (review).

A finished ship beat tier 4 every time for a tenth of its hull, so the last
third of a career had no fight in it. These pin where an elite is met and
where it never is, how it fights, what tactical ruleset 6 changes and leaves
alone, what a save may hold, what beating one is worth, and that the staged
fights the figures came from still come out as they did.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from pathlib import Path

import pytest

from .support import _world_with_pending_fight, _world_with_seed, plain, strip_prizes, vr

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def fights():
    spec = importlib.util.spec_from_file_location("fight_sim_under_test", ROOT / "tools" / "fight_sim.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)
    sys.modules.pop("voidrunner_fight_sim", None)


def _elite(name=None):
    hull = vr.raider_hull(vr.ELITE_TIER)
    return vr.Pirate(name or vr.ELITE_NAMES[0], vr.ELITE_TIER, hull, hull)


def _finished(world):
    ship = world.save.ship
    ship.hull_class = "Carrier"
    for key, upgrade in vr.UPGRADES.items():
        setattr(ship, key + "_tier", upgrade["max_tier"])
    ship.hull_hp = vr.hull_hp_max(ship)
    return ship


# --- what an elite is -----------------------------------------------------------

def test_an_elite_is_known_by_its_name_and_flies_a_pattern_of_its_own():
    assert (vr.ELITE_TIER, vr.ELITE_DANGER, vr.RAIDER_TIER_MOST) == (5, 5, 5)
    assert len(set(vr.ELITE_NAMES)) == len(vr.ELITE_NAMES) >= 5 and not set(vr.ELITE_NAMES) & set(vr.PIRATE_NAMES)
    assert all(vr.tactical_profile(name) == "Ace" for name in vr.ELITE_NAMES)
    assert all(vr.tactical_profile(name) in vr.TACTICAL_PROFILES for name in vr.PIRATE_NAMES)
    assert "Ace" not in vr.TACTICAL_PROFILES and vr.ELITE_PROFILES == {"Ace": ("volley", "volley", "recover")}
    assert set(vr.ALL_PROFILES) == set(vr.TACTICAL_PROFILES) | {"Ace"}
    tactics = vr.new_tactics(_elite())
    assert tactics["profile"] == "Ace" and tactics["version"] == vr.TACTICAL_RULESET_VERSION >= vr.ELITE_RULESET_VERSION == 6
    assert vr.tactical_steps(tactics) == ("volley", "volley", "recover")
    assert vr.raider_hull(5) == 20 + 5 * 15 + vr.ELITE_HULL == 155 and vr.raider_hull(4) == 80


def test_ruleset_six_leaves_the_five_tiers_as_they_were():
    older, current = vr.TACTICAL_THREAT_BONUS_BY_VERSION[5], vr.TACTICAL_THREAT_BONUS_BY_VERSION[6]
    assert current[:5] == older == (0, 3, 6, 8, 34) and len(current) == 6 and current[5] > 2 * current[4]
    ship = _world_with_seed(42).save.ship
    for tier in range(5):
        for intent in vr.TACTICAL_INTENTS:
            assert (vr._tactical_incoming_damage(ship, tier, intent, 7, {"version": 5})
                    == vr._tactical_incoming_damage(ship, tier, intent, 7, {"version": 6}))


def test_it_is_read_no_better_than_half_and_a_scanner_still_helps():
    world = _world_with_seed(42)
    tactics = vr.new_tactics(_elite())
    assert vr.tactical_read(world.save.ship, vr.ELITE_TIER, tactics) == vr.READ_FLOOR == 0.5
    world.save.ship.scanner_tier = 2
    assert vr.tactical_read(world.save.ship, vr.ELITE_TIER, tactics) == pytest.approx(0.7)


# --- where one is met -------------------------------------------------------------

def test_only_the_most_dangerous_space_sends_one_and_about_a_third_of_the_time():
    world = _world_with_seed(42)
    for danger in range(5):
        for seed in range(200):
            world.event_rng.seed(seed)
            raider = vr.generate_pirate(world, danger=danger)
            assert raider.tier <= 4 and raider.name in vr.PIRATE_NAMES
    met = []
    for seed in range(600):
        world.event_rng.seed(seed)
        met.append(vr.generate_pirate(world, danger=5))
    elites = [raider for raider in met if raider.tier == vr.ELITE_TIER]
    assert 0.27 < len(elites) / len(met) < 0.43 and vr.ELITE_CHANCE == 0.35
    assert all(raider.name in vr.ELITE_NAMES and raider.hp == raider.hp_max == 155 for raider in elites)
    assert all(raider.name in vr.PIRATE_NAMES and raider.tier == 4 for raider in met if raider not in elites)
    assert {raider.name for raider in elites} == set(vr.ELITE_NAMES)


def test_an_elite_flies_alone_and_a_given_tier_is_the_tier_given():
    world = _world_with_seed(42)
    destination = next(system for system in world.galaxy if system.id != world.here.id)
    destination.danger = 5
    squadrons = 0
    for seed in range(400):
        world.event_rng.seed(seed)
        ships = vr.generate_pirate_squadron(world, destination)
        if len(ships) > 1:
            squadrons += 1
            assert all(ship.tier <= 4 and ship.name in vr.PIRATE_NAMES for ship in ships)
    assert squadrons > 60
    for seed in range(100):
        world.event_rng.seed(seed)
        bounty = vr.generate_pirate(world, 3, danger=5)
        assert bounty.tier == 3 and bounty.name in vr.PIRATE_NAMES


def test_the_hard_charter_does_not_make_more_of_them():
    standard, hard = _world_with_seed(42), _world_with_seed(42)
    hard.save.charter = "hard"
    for seed in range(300):
        standard.event_rng.seed(seed)
        hard.event_rng.seed(seed)
        usual, harder = vr.generate_pirate(standard, danger=5), vr.generate_pirate(hard, danger=5)
        assert (usual.tier == vr.ELITE_TIER) == (harder.tier == vr.ELITE_TIER) and usual.name == harder.name


def test_the_chart_warns_of_one_and_a_finished_ship_is_not_outclassed():
    world = _world_with_seed(42)
    assert vr.raider_tier_span(world, 5) == (4, 5) and vr.raider_tier_span(world, 4) == (3, 4)
    assert vr.raider_outclasses(world.save.ship, vr.ELITE_TIER)          # a Shuttle should not be there
    target = sorted(world.here.connections)[0]
    world.by_id[target].discovered, world.by_id[target].danger = True, 5
    assert "Raiders there can outclass this hull." in vr.departure_terms(world, target)
    _finished(world)
    assert not vr.raider_outclasses(world.save.ship, vr.ELITE_TIER)      # it is a fight, not a rout
    assert "outclass" not in vr.departure_terms(world, target)


# --- what a save may hold ---------------------------------------------------------

def test_a_fight_with_an_elite_is_a_save_that_loads_and_older_fights_keep_their_rules():
    world, _pirate = _world_with_pending_fight()
    combat = world.save.pending_travel["encounter"]["combat"]
    elite = _elite()
    combat["pirate"] = vr.dataclasses.asdict(elite)
    combat["tactics"] = vr.new_tactics(elite)
    vr.SaveData.from_dict(world.save.to_dict())
    combat["pirate"]["tier"] = 6
    with pytest.raises((vr.ResumeError, vr.UnsupportedSave)):
        vr.SaveData.from_dict(world.save.to_dict())
    combat["pirate"]["tier"] = 5
    # An Ace's pattern came with ruleset 6: a fight that claims it under an older one is not a fight this door made.
    combat["tactics"] = {"version": 5, "profile": "Ace", "step": 0, "brace_ready": True, "spent": ""}
    with pytest.raises((vr.ResumeError, vr.UnsupportedSave)):
        vr.SaveData.from_dict(world.save.to_dict())
    # A fight saved under ruleset 5 is flown to its end on ruleset 5's figures.
    named = vr.Pirate("Hollow Fang", 4, 80, 80)
    combat["pirate"] = vr.dataclasses.asdict(named)
    combat["tactics"] = vr.new_tactics(named, 5)
    restored = vr.SaveData.from_dict(world.save.to_dict())
    tactics = restored.pending_travel["encounter"]["combat"]["tactics"]
    assert tactics["version"] == 5 and vr.tactical_threat_bonus(tactics) == (0, 3, 6, 8, 34)


def test_an_elite_that_got_away_comes_back_as_strong_as_it_was():
    assert vr.rival_ship({"name": vr.ELITE_NAMES[1], "tier": 5}).tier == 5      # already the strongest there is
    assert vr.rival_ship({"name": vr.ELITE_NAMES[1], "tier": 5}).hp == 155
    assert vr.rival_ship({"name": "Hollow Fang", "tier": 4}).tier == 4            # a named raider never becomes one
    assert vr.rival_ship({"name": "Hollow Fang", "tier": 2}).tier == 3
    world = _world_with_seed(42)
    world.save.rivals = [{"name": vr.ELITE_NAMES[1], "tier": 5, "day": 0, "how": "ran"}]
    assert vr.SaveData.from_dict(world.save.to_dict()).rivals[0]["tier"] == 5
    world.save.rivals[0]["tier"] = 6
    with pytest.raises((vr.ResumeError, vr.UnsupportedSave)):
        vr.SaveData.from_dict(world.save.to_dict())


# --- what beating one is worth ----------------------------------------------------

def test_its_scrap_is_worth_three_of_its_tiers_and_beating_it_is_counted(monkeypatch):
    assert vr.prize_loot(_elite()) == 3 * (40 + 5 * 60) == 1_020 and vr.prize_loot(vr.Pirate("Ravage", 4, 80, 80)) == 280
    ids = {accolade.id: accolade for accolade in vr.ACCOLADES}
    assert (ids["elite_1"].target, ids["elite_1"].tier, ids["elite_5"].target, ids["elite_5"].tier) == (1, 2, 5, 3)
    assert "elites" in vr.ACCOLADE_COUNTERS
    world, _pirate = _world_with_pending_fight()
    elite = _elite()
    combat = world.save.pending_travel["encounter"]["combat"]
    combat["pirate"] = dict(vr.dataclasses.asdict(elite), hp=1)
    combat["tactics"] = vr.new_tactics(elite)
    strip_prizes(monkeypatch)
    monkeypatch.setattr(vr, "read_key", lambda: "F")
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.screen_combat(vr.Palette(False), world, elite) == "won"
    assert world.save.accolades["counters"]["elites"] == 1
    assert "elite_1" in {accolade.id for accolade in vr.unlock_accolades(world.save)} | set(world.save.accolades["unlocked"])
    # A named raider, however strong, is not one.
    other, pirate = _world_with_pending_fight()
    other.save.pending_travel["encounter"]["combat"]["pirate"]["hp"] = 1
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.screen_combat(vr.Palette(False), other, pirate) == "won"
    assert "elites" not in other.save.accolades["counters"]


# --- how it looks -----------------------------------------------------------------

def test_its_hull_is_its_own_and_the_size_of_the_others():
    art = vr._hostile_art(vr.ELITE_NAMES[0])
    assert art is vr._SCENE_ACE and art not in (vr._SCENE_RAIDER, vr._SCENE_BULWARK, vr._SCENE_SKIRMISHER)
    for other in (vr._SCENE_RAIDER, vr._SCENE_BULWARK, vr._SCENE_SKIRMISHER):
        assert (len(art), {len(row) for row in art}) == (len(other), {len(row) for row in other})
    assert all(pixel == "." or pixel in vr._SCENE_COLORS for row in art for pixel in row)
    assert art == tuple(reversed(art))  # symmetric about its spine


def test_the_panel_says_what_it_is_and_how_it_fights(terminal):
    terminal(80, 24)
    world, _pirate = _world_with_pending_fight()
    _finished(world)
    elite = _elite()
    tactics = vr.new_tactics(elite)
    panel = " ".join(" ".join(plain(line).split()) for line in vr.combat_display_lines(
        world, elite, [], patrol=False, tactics=tactics, details=True))
    assert "tier 5 Ace" in panel and "VOLLEY > volley > recover" in panel and "Ace pattern: volley > volley > recover." in panel


# --- the staged fights the figures came from ----------------------------------------

def test_a_finished_ship_walks_through_tier_four_and_has_a_fight_with_an_elite(fights):
    sim_vr = fights.load_door()
    assert {"starter", "mid", "finished"} <= set(fights.ships(sim_vr))
    easy = fights.run(sim_vr, "finished", "none", 4, 90)
    assert easy["won"] == easy["fights"] and easy["cost"] < 0.2 * easy["hull"]      # what was wrong
    alone = fights.run(sim_vr, "finished", "none", 5, 120)
    crewed = fights.run(sim_vr, "finished", "all", 5, 120)
    assert 0.3 < alone["won"] / alone["fights"] < 0.75 and not alone["outclassed"]  # a coin weighted by the pilot
    assert crewed["won"] / crewed["fights"] > 0.9 and 0.4 < crewed["cost"] / crewed["hull"] < 0.75
    mid = fights.run(sim_vr, "mid", "all", 5, 60)
    assert mid["outclassed"] and mid["won"] / mid["fights"] < 0.2                   # it is meant to run
    rows = fights.table([easy, alone, crewed, mid])
    assert rows[0].split()[:3] == ["ship", "crew", "tier"] and rows[-1].endswith("outclassed") and len(rows) == 5
    assert fights.names_for(sim_vr, 5) == [sim_vr.ELITE_NAMES[0]] and len(fights.names_for(sim_vr, 3)) == 3
