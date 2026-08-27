"""Tests for the Voidrunner door's domain layer (netbbs.doors.bundled.
voidrunner) -- galaxy generation, economy, save round-tripping,
missions, and combat resolution. Loaded directly from its file path
rather than a normal `from netbbs.doors.bundled import voidrunner`
import -- same reasoning as `test_doors_runtime.py` running it and
`retro_trivia.py` this same way: this is the exact file NetBBS itself
launches as a standalone subprocess (see `netbbs.doors.runtime`), not
an ordinarily-imported library module, so testing it by path exercises
precisely what actually ships. This file just exercises the pure domain
functions in-process instead of the whole door end to end.

Regression-focused: several of these exist specifically to pin behavior
that would otherwise be easy to silently break (galaxy determinism/
connectivity, corrupt-save recovery, mission completion), not just to
restate what the code already visibly does.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import random
import sys
from pathlib import Path

_VOIDRUNNER_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "netbbs" / "doors" / "bundled" / "voidrunner.py"
)


def _load_voidrunner():
    spec = importlib.util.spec_from_file_location("voidrunner_domain_under_test", _VOIDRUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # `dataclasses` (voidrunner.py uses `from __future__ import annotations`,
    # so field types are strings) resolves them via
    # `sys.modules[cls.__module__].__dict__` -- the module must already be
    # registered under its own name in sys.modules *before* exec_module
    # runs, or that lookup returns None and every @dataclass in the file
    # raises AttributeError at import time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vr = _load_voidrunner()


# -- galaxy generation -------------------------------------------------


def test_generate_galaxy_is_a_pure_function_of_seed():
    a = vr.generate_galaxy(12345)
    b = vr.generate_galaxy(12345)
    assert [s.name for s in a] == [s.name for s in b]
    assert [s.economy for s in a] == [s.economy for s in b]
    assert [sorted(s.connections) for s in a] == [sorted(s.connections) for s in b]


def test_different_seeds_usually_produce_different_galaxies():
    a = vr.generate_galaxy(1)
    b = vr.generate_galaxy(2)
    assert [s.name for s in a] != [s.name for s in b]


def test_galaxy_is_fully_connected_from_home_system():
    galaxy = vr.generate_galaxy(999)
    by_id = {s.id: s for s in galaxy}
    reachable = vr.bfs_hops(by_id, 0)
    assert len(reachable) == len(galaxy)


def test_home_system_and_its_neighbors_start_discovered_nothing_else_does():
    galaxy = vr.generate_galaxy(42)
    home = galaxy[0]
    assert home.discovered is True
    for sid in home.connections:
        assert galaxy[sid].discovered is True
    far_systems = [s for s in galaxy if s.id != 0 and s.id not in home.connections]
    assert any(not s.discovered for s in far_systems)


def test_home_systems_direct_neighbors_never_exceed_a_safe_danger_ceiling():
    """Dogfood-caught: every system's danger tier is drawn from the same
    distribution regardless of distance from home, so before this fix a
    galaxy could seed a near-unwinnable tier-4 raider system one jump
    from Freeport, before a new character had any chance to earn a
    single upgrade. Checked across a wide range of seeds, not just one,
    since the original bug only showed up for *some* seeds -- a single
    lucky seed passing would have hidden the regression."""
    for seed in range(200):
        galaxy = vr.generate_galaxy(seed)
        home = galaxy[0]
        for nid in home.connections:
            assert galaxy[nid].danger <= 2, f"seed={seed} neighbor={nid} danger={galaxy[nid].danger}"


def test_every_system_has_at_least_one_connection():
    galaxy = vr.generate_galaxy(7)
    assert all(len(s.connections) >= 1 for s in galaxy)


# -- economy -------------------------------------------------------------


def _world_with_seed(seed: int) -> "vr.World":
    save = vr._new_career("Tester")
    save.seed = seed
    return vr.World(save)


def test_producing_economy_is_cheaper_than_demanding_economy_for_same_good():
    world = _world_with_seed(1)
    producer = next(s for s in world.galaxy if "food" in vr.ECONOMY_PRODUCES[s.economy])
    demander = next(s for s in world.galaxy if "food" in vr.ECONOMY_DEMANDS[s.economy])
    assert vr.price_for(world, producer.id, "food") < vr.price_for(world, demander.id, "food")


def test_nudging_drift_up_then_reverting_moves_price_back_toward_baseline():
    world = _world_with_seed(2)
    sid = world.galaxy[0].id
    vr._nudge_drift(world, sid, "food", 0.5)
    inflated = vr.price_for(world, sid, "food")
    for _ in range(50):
        vr.tick_price_reversion(world)
    reverted = vr.price_for(world, sid, "food")
    assert reverted < inflated


def test_drift_is_clamped_and_does_not_runaway():
    world = _world_with_seed(3)
    sid = world.galaxy[0].id
    for _ in range(100):
        vr._nudge_drift(world, sid, "food", 0.5)
    assert world.save.market_drift[sid]["food"] <= 1.6


# -- ship derived stats ---------------------------------------------------


def test_upgrade_tiers_increase_derived_capacities():
    ship = vr.Ship(hull_class="Shuttle", hull_hp=60, fuel=24)
    base_cargo = vr.cargo_capacity(ship)
    base_fuel = vr.fuel_capacity(ship)
    base_hull = vr.hull_hp_max(ship)
    ship.cargo_tier = 2
    ship.engine_tier = 1
    ship.hull_tier = 1
    assert vr.cargo_capacity(ship) > base_cargo
    assert vr.fuel_capacity(ship) > base_fuel
    assert vr.hull_hp_max(ship) > base_hull


def test_carrier_hull_class_has_higher_base_stats_than_shuttle_at_same_tiers():
    shuttle = vr.Ship(hull_class="Shuttle", hull_hp=60, fuel=24)
    carrier = vr.Ship(hull_class="Carrier", hull_hp=60, fuel=24)
    assert vr.cargo_capacity(carrier) > vr.cargo_capacity(shuttle)
    assert vr.hull_hp_max(carrier) > vr.hull_hp_max(shuttle)
    assert vr.fuel_capacity(carrier) > vr.fuel_capacity(shuttle)


# -- hull class ladder (branching Shuttle -> Freighter|Cutter -> Carrier) --


def test_hull_refits_branch_from_shuttle_into_freighter_or_cutter():
    targets = {target for target, _cost in vr.HULL_REFITS["Shuttle"]}
    assert targets == {"Freighter", "Cutter"}


def test_hull_refits_from_freighter_or_cutter_only_offer_carrier():
    assert [target for target, _cost in vr.HULL_REFITS["Freighter"]] == ["Carrier"]
    assert [target for target, _cost in vr.HULL_REFITS["Cutter"]] == ["Carrier"]


def test_carrier_has_no_further_refits():
    assert vr.HULL_REFITS["Carrier"] == []


def test_carrier_base_stats_exceed_both_freighter_and_cutter_in_every_dimension():
    """Carrier is the unified endgame hull, not a third competing
    tradeoff -- it must never be strictly worse than either mid-tier
    branch in any single stat, or a Freighter/Cutter owner would have a
    real reason never to take the final refit."""
    freighter = vr.Ship(hull_class="Freighter", hull_hp=1, fuel=0)
    cutter = vr.Ship(hull_class="Cutter", hull_hp=1, fuel=0)
    carrier = vr.Ship(hull_class="Carrier", hull_hp=1, fuel=0)
    for other in (freighter, cutter):
        assert vr.cargo_capacity(carrier) > vr.cargo_capacity(other)
        assert vr.fuel_capacity(carrier) > vr.fuel_capacity(other)
        assert vr.hull_hp_max(carrier) > vr.hull_hp_max(other)


def test_freighter_and_cutter_are_a_genuine_tradeoff_not_one_strictly_better():
    """The branching choice only means something if neither mid-tier hull
    dominates the other in every stat."""
    freighter = vr.Ship(hull_class="Freighter", hull_hp=1, fuel=0)
    cutter = vr.Ship(hull_class="Cutter", hull_hp=1, fuel=0)
    assert vr.cargo_capacity(freighter) > vr.cargo_capacity(cutter)
    assert vr.hull_hp_max(cutter) > vr.hull_hp_max(freighter)
    assert vr.fuel_capacity(cutter) > vr.fuel_capacity(freighter)


def test_hull_refit_screen_declines_without_enough_credits(monkeypatch):
    world = _world_with_seed(30)
    world.save.pilot.credits = 100
    monkeypatch.setattr(vr, "confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not prompt")))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr._hull_refit_screen(vr.Palette(truecolor=False), world, "Freighter", 15_000)

    assert world.save.ship.hull_class == "Shuttle"
    assert world.save.pilot.credits == 100
    assert "Need" in buf.getvalue()


def test_hull_refit_screen_declining_confirmation_makes_no_change(monkeypatch):
    world = _world_with_seed(31)
    world.save.pilot.credits = 20_000

    monkeypatch.setattr(vr, "read_key", lambda: "N")
    with contextlib.redirect_stdout(io.StringIO()):
        vr._hull_refit_screen(vr.Palette(truecolor=False), world, "Freighter", 15_000)

    assert world.save.ship.hull_class == "Shuttle"
    assert world.save.pilot.credits == 20_000


def test_hull_refit_screen_applies_the_refit_charges_credits_and_resets_hull_to_new_max(monkeypatch):
    world = _world_with_seed(32)
    world.save.pilot.credits = 20_000
    world.save.ship.hull_hp = 10  # damaged, below Shuttle's own max

    monkeypatch.setattr(vr, "read_key", lambda: "Y")
    before_log_len = len(world.save.pilot.log)
    with contextlib.redirect_stdout(io.StringIO()):
        vr._hull_refit_screen(vr.Palette(truecolor=False), world, "Freighter", 15_000)

    assert world.save.ship.hull_class == "Freighter"
    assert world.save.pilot.credits == 5_000
    assert world.save.ship.hull_hp == vr.hull_hp_max(world.save.ship)
    assert len(world.save.pilot.log) == before_log_len + 1


def test_hull_refit_narrative_names_the_actual_previous_class_not_a_hardcoded_one(monkeypatch):
    """Dogfood-shaped regression: the original single-hull-class refit's
    own narrative hardcoded "Your Shuttle is towed..." -- generalizing to
    a branching ladder means a Freighter or Cutter owner refitting into a
    Carrier must see their own actual previous class named, not a stale
    "Shuttle" literal."""
    world = _world_with_seed(33)
    world.save.ship.hull_class = "Cutter"
    world.save.pilot.credits = 50_000

    monkeypatch.setattr(vr, "read_key", lambda: "Y")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr._hull_refit_screen(vr.Palette(truecolor=False), world, "Carrier", 45_000)

    assert "Your Cutter is towed" in buf.getvalue()
    assert world.save.ship.hull_class == "Carrier"


def test_shipyard_offers_two_refit_choices_from_shuttle_and_one_after_committing(monkeypatch):
    """Integration-shaped: `screen_shipyard`'s own display/dispatch, not
    just the domain functions in isolation."""
    world = _world_with_seed(34)
    world.save.pilot.credits = 20_000

    keys = iter(["G", "Y", "Q"])  # pick the first refit slot (Freighter), confirm, then leave
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_shipyard(vr.Palette(truecolor=False), world)

    text = buf.getvalue()
    assert "[G]" in text and "Freighter-Class Refit" in text
    assert "[H]" in text and "Cutter-Class Refit" in text
    assert world.save.ship.hull_class == "Freighter"


# -- save round-tripping ---------------------------------------------------


def test_save_data_round_trips_through_dict_including_missions_and_none_fields():
    save = vr._new_career("Roundtrip")
    save.active_missions.append(vr.Mission(
        id=1, kind="scan", description="survey it", reward=250,
        origin_system=0, target_system=5, deadline_turn=None,
    ))
    save.market_drift[3] = {"food": 1.2}
    restored = vr.SaveData.from_dict(save.to_dict())
    assert restored.pilot.handle == "Roundtrip"
    assert restored.active_missions[0].deadline_turn is None
    assert restored.active_missions[0].target_system == 5
    assert restored.market_drift[3]["food"] == 1.2


def test_write_and_load_save_round_trips_on_disk(tmp_path):
    save = vr._new_career("Disky")
    save.pilot.credits = 4321
    vr.write_save(tmp_path, user_id=77, save=save)

    loaded, is_new, notice = vr.load_or_create_save(tmp_path, user_id=77, handle="Disky")
    assert is_new is False
    assert notice is None
    assert loaded.pilot.credits == 4321
    assert loaded.seed == save.seed


def test_loading_an_existing_save_never_overwrites_the_chosen_callsign(tmp_path):
    """Dogfood-caught: a live login handle is only ever the *default*
    callsign at character creation -- once a save exists, the pilot's
    own chosen callsign must survive regardless of what the current
    login handle says, including when it's unchanged, changed, or a
    totally different account (a save is keyed by stable user_id, never
    handle -- see the module's own docstring). A prior version
    unconditionally wrote the login handle over the saved callsign on
    every single load, silently discarding it."""
    save = vr._new_career("Claude")
    save.pilot.handle = "Voyager1"  # the player's own chosen callsign
    vr.write_save(tmp_path, user_id=99, save=save)

    loaded, is_new, notice = vr.load_or_create_save(tmp_path, user_id=99, handle="Claude")

    assert is_new is False
    assert loaded.pilot.handle == "Voyager1"


def test_corrupt_save_is_backed_up_not_silently_discarded(tmp_path):
    path = tmp_path / "5.json"
    path.write_text("not valid json{{{", encoding="utf-8")

    save, is_new, notice = vr.load_or_create_save(tmp_path, user_id=5, handle="Recovered")
    assert is_new is True
    assert notice is not None
    assert "preserved" in notice
    backups = list(tmp_path.glob("5.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "not valid json{{{"


def test_write_save_is_atomic_no_tmp_file_left_behind(tmp_path):
    save = vr._new_career("Atomic")
    vr.write_save(tmp_path, user_id=9, save=save)
    assert (tmp_path / "9.json").exists()
    assert not (tmp_path / "9.json.tmp").exists()


# -- missions --------------------------------------------------------------


def test_delivery_mission_completes_on_arrival_with_enough_cargo():
    world = _world_with_seed(4)
    dest = world.here.connections[0]
    mission = vr.Mission(id=1, kind="delivery", description="test delivery", reward=500,
                          origin_system=world.save.current_system, target_system=dest,
                          commodity="food", quantity=3, deadline_turn=None)
    vr.accept_mission(world, mission)
    world.save.cargo["food"] = 5
    world.save.current_system = dest

    messages = vr.check_mission_completions(world)

    assert any("Mission complete" in m for m in messages)
    assert world.save.cargo["food"] == 2
    assert world.save.pilot.credits == 1200 + 500
    assert mission not in world.save.active_missions


def test_delivery_mission_does_not_complete_with_insufficient_cargo():
    world = _world_with_seed(5)
    dest = world.here.connections[0]
    mission = vr.Mission(id=1, kind="delivery", description="test delivery", reward=500,
                          origin_system=world.save.current_system, target_system=dest,
                          commodity="food", quantity=3, deadline_turn=None)
    vr.accept_mission(world, mission)
    world.save.cargo["food"] = 1
    world.save.current_system = dest

    messages = vr.check_mission_completions(world)

    assert messages == []
    assert mission in world.save.active_missions


def test_expired_mission_is_dropped_with_a_message():
    world = _world_with_seed(6)
    mission = vr.Mission(id=1, kind="delivery", description="late delivery", reward=500,
                          origin_system=world.save.current_system, target_system=999,
                          commodity="food", quantity=3, deadline_turn=0)
    vr.accept_mission(world, mission)
    world.save.turn = 10

    messages = vr.check_mission_completions(world)

    assert any("expired" in m for m in messages)
    assert mission not in world.save.active_missions


def test_scan_mission_completes_when_target_system_is_discovered():
    world = _world_with_seed(8)
    mission = vr.Mission(id=1, kind="scan", description="survey", reward=200,
                          origin_system=world.save.current_system, target_system=17,
                          deadline_turn=None)
    vr.accept_mission(world, mission)

    messages = vr.check_mission_completions(world, just_discovered=17)

    assert any("Mission complete" in m for m in messages)
    assert world.save.pilot.credits == 1200 + 200


# -- bounty missions in screen_travel (dogfood-caught: losing used to
# leave the bounty active forever, turning its target system into a
# mandatory, unwinnable-at-current-gear ambush on every future visit) --


def _accept_bounty(world, *, target_system: int, pirate_tier: int = 2, reward: int = 800) -> "vr.Mission":
    mission = vr.Mission(id=1, kind="bounty", description="test bounty", reward=reward,
                          origin_system=world.save.current_system, target_system=target_system,
                          pirate_tier=pirate_tier)
    vr.accept_mission(world, mission)
    return mission


def _travel_with_stubbed_combat(monkeypatch, world, dest_id: int, outcome: str) -> None:
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: outcome)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest_id)


def test_losing_a_bounty_fight_clears_it_instead_of_leaving_a_permanent_ambush(monkeypatch):
    world = _world_with_seed(20)
    dest_id = world.here.connections[0]
    mission = _accept_bounty(world, target_system=dest_id)

    _travel_with_stubbed_combat(monkeypatch, world, dest_id, "destroyed")

    assert mission not in world.save.active_missions
    assert world.save.pilot.missions_completed == 0
    assert any("Bounty failed" in entry for entry in world.save.pilot.log)


def test_winning_a_bounty_fight_completes_it_and_pays_the_reward(monkeypatch):
    world = _world_with_seed(21)
    dest_id = world.here.connections[0]
    mission = _accept_bounty(world, target_system=dest_id, reward=800)
    starting_credits = world.save.pilot.credits

    _travel_with_stubbed_combat(monkeypatch, world, dest_id, "won")

    assert mission not in world.save.active_missions
    assert world.save.pilot.missions_completed == 1
    assert world.save.pilot.credits == starting_credits + 800


def test_escaping_a_bounty_fight_leaves_it_active_to_retry_later(monkeypatch):
    world = _world_with_seed(22)
    dest_id = world.here.connections[0]
    mission = _accept_bounty(world, target_system=dest_id)

    _travel_with_stubbed_combat(monkeypatch, world, dest_id, "escaped")

    assert mission in world.save.active_missions


def test_revisiting_a_system_with_a_still_active_bounty_triggers_it_again(monkeypatch):
    """Confirms the guaranteed-encounter re-trigger itself (the thing
    that made the original bug a repeating trap, not a one-off) still
    works -- only losing should stop it, an escape should not."""
    world = _world_with_seed(23)
    dest_id = world.here.connections[0]
    _accept_bounty(world, target_system=dest_id)
    origin_id = world.save.current_system
    # The trip back through origin_id is otherwise still subject to the
    # *ordinary* random-encounter roll (unrelated to the bounty, which
    # triggers unconditionally) -- world.event_rng is real, unseeded
    # entropy (see World.__init__), so without pinning it here this test
    # was genuinely flaky: an occasional unlucky roll at origin_id calls
    # the stubbed screen_combat a 3rd time and fails the assertion below.
    world.event_rng.random = lambda: 1.0  # always above every danger threshold

    calls = []
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: calls.append(1) or "escaped")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest_id)
        vr.screen_travel(vr.Palette(truecolor=False), world, origin_id)
        vr.screen_travel(vr.Palette(truecolor=False), world, dest_id)

    assert len(calls) == 2  # both visits to dest_id triggered the bounty fight


# -- combat ------------------------------------------------------------


def test_fight_round_damages_both_sides_and_is_driven_by_world_event_rng():
    world = _world_with_seed(9)
    world.event_rng = random.Random(1)
    pirate = vr.Pirate(name="Test Raider", tier=1, hp=35, hp_max=35)
    starting_hull = world.save.ship.hull_hp

    dmg_to_pirate, dmg_to_player, lines = vr.fight_round(world, pirate)

    assert dmg_to_pirate > 0
    assert pirate.hp == 35 - dmg_to_pirate
    assert world.save.ship.hull_hp == starting_hull - dmg_to_player
    assert lines


def test_higher_weapon_tier_deals_more_damage_with_same_rng_sequence():
    world_weak = _world_with_seed(10)
    world_weak.event_rng = random.Random(42)
    world_strong = _world_with_seed(10)
    world_strong.event_rng = random.Random(42)
    world_strong.save.ship.weapon_tier = 4

    pirate_weak = vr.Pirate(name="X", tier=2, hp=100, hp_max=100)
    pirate_strong = vr.Pirate(name="X", tier=2, hp=100, hp_max=100)

    dmg_weak, _, _ = vr.fight_round(world_weak, pirate_weak)
    dmg_strong, _, _ = vr.fight_round(world_strong, pirate_strong)

    assert dmg_strong > dmg_weak


def test_shields_reduce_incoming_damage():
    world_bare = _world_with_seed(11)
    world_bare.event_rng = random.Random(7)
    world_shielded = _world_with_seed(11)
    world_shielded.event_rng = random.Random(7)
    world_shielded.save.ship.shield_tier = 3

    pirate_a = vr.Pirate(name="Y", tier=3, hp=1000, hp_max=1000)  # never dies mid-round
    pirate_b = vr.Pirate(name="Y", tier=3, hp=1000, hp_max=1000)

    _, dmg_bare, _ = vr.fight_round(world_bare, pirate_a)
    _, dmg_shielded, _ = vr.fight_round(world_shielded, pirate_b)

    assert dmg_shielded <= dmg_bare


# -- squadron fights ------------------------------------------------------


def _system_with_danger(world, danger: int):
    system = world.by_id[world.here.connections[0]]
    system.danger = danger
    return system


def test_squadron_never_spawns_below_the_minimum_danger():
    world = _world_with_seed(90)
    dest = _system_with_danger(world, vr.SQUADRON_MIN_DANGER - 1)
    world.event_rng.random = lambda: 0.0  # would always succeed the squadron roll, if it ran at all

    pirates = vr.generate_pirate_squadron(world, dest)

    assert len(pirates) == 1


def test_squadron_spawns_at_the_minimum_danger_when_the_roll_succeeds():
    world = _world_with_seed(91)
    dest = _system_with_danger(world, vr.SQUADRON_MIN_DANGER)
    world.event_rng.random = lambda: 0.0  # always below SQUADRON_CHANCE

    pirates = vr.generate_pirate_squadron(world, dest)

    assert len(pirates) == vr.SQUADRON_SIZE


def test_squadron_does_not_spawn_at_high_danger_when_the_roll_fails():
    world = _world_with_seed(92)
    dest = _system_with_danger(world, vr.SQUADRON_MIN_DANGER)
    world.event_rng.random = lambda: 1.0  # always above SQUADRON_CHANCE

    pirates = vr.generate_pirate_squadron(world, dest)

    assert len(pirates) == 1


def test_squadron_encounter_fights_every_ship_on_a_full_win(monkeypatch):
    world = _world_with_seed(93)
    dest = _system_with_danger(world, vr.SQUADRON_MIN_DANGER)
    world.event_rng.random = lambda: 0.0  # triggers the encounter, then spawns a squadron

    calls = []
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: calls.append(pirate) or "won")
    world.event_rng.choices = lambda population, weights: ["pirate"]

    with contextlib.redirect_stdout(io.StringIO()):
        vr._resolve_random_travel_encounter(vr.Palette(truecolor=False), world, dest)

    assert len(calls) == vr.SQUADRON_SIZE


def test_squadron_encounter_stops_after_an_escape_not_a_full_win(monkeypatch):
    world = _world_with_seed(94)
    dest = _system_with_danger(world, vr.SQUADRON_MIN_DANGER)
    world.event_rng.random = lambda: 0.0

    calls = []
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: calls.append(pirate) or "escaped")
    world.event_rng.choices = lambda population, weights: ["pirate"]

    with contextlib.redirect_stdout(io.StringIO()):
        vr._resolve_random_travel_encounter(vr.Palette(truecolor=False), world, dest)

    assert len(calls) == 1  # never reached the second ship


def test_destroy_ship_clears_cargo_and_returns_player_to_freeport_with_full_hull():
    world = _world_with_seed(12)
    world.save.cargo["ore"] = 10
    world.save.current_system = world.here.connections[0]
    world.save.ship.hull_hp = 0
    world.save.pilot.notoriety = 7

    vr.destroy_ship(world)

    assert world.save.cargo == {}
    assert world.save.current_system == 0
    assert world.save.ship.hull_hp == vr.hull_hp_max(world.save.ship)
    assert world.save.pilot.credits < 1200  # salvage fee charged
    assert world.save.pilot.notoriety == 0  # any ship loss wipes wanted status


# -- travel encounter variety -------------------------------------------


def test_no_encounter_when_the_overall_roll_fails():
    world = _world_with_seed(50)
    dest = world.by_id[world.here.connections[0]]
    world.event_rng.random = lambda: 1.0  # always above every danger threshold

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr._resolve_random_travel_encounter(vr.Palette(truecolor=False), world, dest)

    assert buf.getvalue() == ""


def test_encounter_dispatches_to_pirate_kind(monkeypatch):
    world = _world_with_seed(51)
    dest = world.by_id[world.here.connections[0]]
    world.event_rng.random = lambda: 0.0  # always triggers an encounter
    world.event_rng.choices = lambda population, weights: ["pirate"]
    calls = []
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: calls.append(pirate) or "escaped")

    with contextlib.redirect_stdout(io.StringIO()):
        vr._resolve_random_travel_encounter(vr.Palette(truecolor=False), world, dest)

    assert len(calls) == 1


def test_encounter_dispatches_to_each_new_kind(monkeypatch):
    world = _world_with_seed(52)
    dest = world.by_id[world.here.connections[0]]
    world.event_rng.random = lambda: 0.0

    for kind, target in (
        ("derelict", "_encounter_derelict"),
        ("distress", "_encounter_distress_call"),
    ):
        calls = []
        monkeypatch.setattr(vr, target, lambda p, w, _calls=calls: _calls.append(1))
        world.event_rng.choices = lambda population, weights, _kind=kind: [_kind]
        with contextlib.redirect_stdout(io.StringIO()):
            vr._resolve_random_travel_encounter(vr.Palette(truecolor=False), world, dest)
        assert calls == [1], f"{kind} did not dispatch to {target}"
        monkeypatch.undo()


def test_derelict_ignore_leaves_world_unchanged():
    world = _world_with_seed(53)
    before_credits = world.save.pilot.credits
    vr.read_key = lambda: "I"

    with contextlib.redirect_stdout(io.StringIO()):
        vr._encounter_derelict(vr.Palette(truecolor=False), world)

    assert world.save.pilot.credits == before_credits


def test_derelict_board_success_grants_credits_and_logs():
    world = _world_with_seed(54)
    before_credits = world.save.pilot.credits
    before_log_len = len(world.save.pilot.log)
    vr.read_key = lambda: "B"
    world.event_rng.random = lambda: 0.0  # always the salvage-success branch

    with contextlib.redirect_stdout(io.StringIO()):
        vr._encounter_derelict(vr.Palette(truecolor=False), world)

    assert world.save.pilot.credits > before_credits
    assert len(world.save.pilot.log) == before_log_len + 1


def test_derelict_board_trap_triggers_combat(monkeypatch):
    world = _world_with_seed(55)
    vr.read_key = lambda: "B"
    world.event_rng.random = lambda: 0.99  # always the trap branch

    calls = []
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: calls.append(pirate) or "escaped")
    with contextlib.redirect_stdout(io.StringIO()):
        vr._encounter_derelict(vr.Palette(truecolor=False), world)

    assert len(calls) == 1


def test_distress_ignore_leaves_world_unchanged():
    world = _world_with_seed(56)
    before_credits = world.save.pilot.credits
    before_fuel = world.save.ship.fuel
    vr.read_key = lambda: "I"

    with contextlib.redirect_stdout(io.StringIO()):
        vr._encounter_distress_call(vr.Palette(truecolor=False), world)

    assert world.save.pilot.credits == before_credits
    assert world.save.ship.fuel == before_fuel


def test_distress_help_costs_fuel_grants_credits_and_reputation():
    world = _world_with_seed(57)
    before_credits = world.save.pilot.credits
    before_fuel = world.save.ship.fuel
    before_rep = world.save.pilot.reputation[vr.FACTION_CONCORD]
    vr.read_key = lambda: "H"

    with contextlib.redirect_stdout(io.StringIO()):
        vr._encounter_distress_call(vr.Palette(truecolor=False), world)

    assert world.save.pilot.credits > before_credits
    assert world.save.ship.fuel < before_fuel
    assert world.save.pilot.reputation[vr.FACTION_CONCORD] > before_rep


def test_distress_help_never_costs_more_fuel_than_available():
    world = _world_with_seed(58)
    world.save.ship.fuel = 1
    vr.read_key = lambda: "H"

    with contextlib.redirect_stdout(io.StringIO()):
        vr._encounter_distress_call(vr.Palette(truecolor=False), world)

    assert world.save.ship.fuel == 0


def test_market_tip_reveals_a_real_price_at_a_nearby_discovered_system():
    world = _world_with_seed(59)
    dest = world.by_id[world.here.connections[0]]
    dest.discovered = True

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr._encounter_market_tip(vr.Palette(truecolor=False), world, dest)

    assert "going for" in buf.getvalue()


def test_market_tip_with_no_discovered_neighbors_shows_fallback_without_crashing():
    world = _world_with_seed(60)
    dest = world.by_id[world.here.connections[0]]
    for system in world.galaxy:
        system.discovered = system.id == dest.id  # only dest itself, nothing "nearby"

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr._encounter_market_tip(vr.Palette(truecolor=False), world, dest)

    assert "nothing usable" in buf.getvalue()


# -- player notoriety ----------------------------------------------------


def test_notoriety_patrol_chance_is_zero_at_zero_notoriety():
    assert vr.notoriety_patrol_chance(0) == 0.0


def test_notoriety_patrol_chance_scales_with_notoriety_and_caps():
    assert vr.notoriety_patrol_chance(5) < vr.notoriety_patrol_chance(10)
    assert vr.notoriety_patrol_chance(1000) == vr.NOTORIETY_PATROL_MAX_CHANCE


def test_notoriety_fine_cost_scales_with_notoriety():
    assert vr.notoriety_fine_cost(0) < vr.notoriety_fine_cost(10)


def test_concord_patrol_tier_scales_with_notoriety_and_caps_at_four():
    world = _world_with_seed(70)
    world.save.pilot.notoriety = 0
    low = vr.generate_concord_patrol(world)
    world.save.pilot.notoriety = 1000
    high = vr.generate_concord_patrol(world)
    assert low.tier == 0
    assert high.tier == 4
    assert high.hp > low.hp


def test_pilot_save_round_trip_defaults_notoriety_for_old_saves_without_it():
    save = vr._new_career("Legacy")
    as_dict = save.to_dict()
    del as_dict["pilot"]["notoriety"]  # simulate a pre-notoriety save file
    restored = vr.SaveData.from_dict(as_dict)
    assert restored.pilot.notoriety == 0


def test_customs_bribe_refused_raises_notoriety(monkeypatch):
    world = _world_with_seed(71)
    world.save.cargo["narcotics"] = 5
    world.save.pilot.credits = 0  # can't afford the bribe cost -> refused path
    vr.read_key = lambda: "B"

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_customs(vr.Palette(truecolor=False), world)

    assert world.save.pilot.notoriety == vr.NOTORIETY_PER_CUSTOMS_BUST


def test_customs_cooperative_surrender_does_not_raise_notoriety():
    world = _world_with_seed(72)
    world.save.cargo["narcotics"] = 5
    vr.read_key = lambda: "S"

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_customs(vr.Palette(truecolor=False), world)

    assert world.save.pilot.notoriety == 0


def test_customs_successful_bribe_does_not_raise_notoriety():
    world = _world_with_seed(73)
    world.save.cargo["narcotics"] = 5
    world.save.pilot.credits = 10_000
    world.event_rng.random = lambda: 0.0  # always the bribe-succeeds branch
    vr.read_key = lambda: "B"

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_customs(vr.Palette(truecolor=False), world)

    assert world.save.pilot.notoriety == 0


def test_wrong_bounty_kill_raises_notoriety_and_lowers_concord_rep(monkeypatch):
    world = _world_with_seed(74)
    dest_id = world.here.connections[0]
    _accept_bounty(world, target_system=dest_id)
    before_rep = world.save.pilot.reputation[vr.FACTION_CONCORD]
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "won")
    world.event_rng.random = lambda: 0.0  # always triggers the wrong-kill roll

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest_id)

    assert world.save.pilot.notoriety == vr.NOTORIETY_PER_WRONG_BOUNTY_KILL
    assert world.save.pilot.reputation[vr.FACTION_CONCORD] < before_rep


def test_bounty_win_without_the_wrong_kill_roll_leaves_notoriety_at_zero(monkeypatch):
    world = _world_with_seed(75)
    dest_id = world.here.connections[0]
    _accept_bounty(world, target_system=dest_id)
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "won")
    world.event_rng.random = lambda: 1.0  # never triggers the wrong-kill roll

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest_id)

    assert world.save.pilot.notoriety == 0


def test_travel_dispatches_to_patrol_when_wanted_and_the_roll_succeeds(monkeypatch):
    world = _world_with_seed(76)
    dest_id = world.here.connections[0]
    world.save.pilot.notoriety = 20  # well above zero -- patrol chance > 0
    world.event_rng.random = lambda: 0.0  # always within the patrol chance
    calls = []
    monkeypatch.setattr(vr, "screen_notoriety_patrol", lambda p, w: calls.append(1))

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest_id)

    assert calls == [1]


def test_travel_never_dispatches_to_patrol_at_zero_notoriety(monkeypatch):
    world = _world_with_seed(77)
    dest_id = world.here.connections[0]
    world.save.pilot.notoriety = 0
    world.event_rng.random = lambda: 0.0  # would trigger everything else, but not this
    calls = []
    monkeypatch.setattr(vr, "screen_notoriety_patrol", lambda p, w: calls.append(1))
    monkeypatch.setattr(vr, "_resolve_random_travel_encounter", lambda p, w, dest: None)

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest_id)

    assert calls == []


def test_notoriety_patrol_evade_success_leaves_notoriety_and_reputation_unchanged():
    world = _world_with_seed(78)
    world.save.pilot.notoriety = 10
    before_notoriety = world.save.pilot.notoriety
    before_rep = dict(world.save.pilot.reputation)
    vr.read_key = lambda: "E"
    world.event_rng.random = lambda: 0.0  # always evades successfully

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_notoriety_patrol(vr.Palette(truecolor=False), world)

    assert world.save.pilot.notoriety == before_notoriety
    assert world.save.pilot.reputation == before_rep


def test_notoriety_patrol_surrender_clears_notoriety_and_charges_the_fine():
    world = _world_with_seed(79)
    world.save.pilot.notoriety = 10
    world.save.pilot.credits = 10_000
    before_credits = world.save.pilot.credits
    fine = vr.notoriety_fine_cost(10)
    vr.read_key = lambda: "S"

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_notoriety_patrol(vr.Palette(truecolor=False), world)

    assert world.save.pilot.notoriety == 0
    assert world.save.pilot.credits == before_credits - fine


def test_notoriety_patrol_surrender_is_not_offered_without_enough_credits():
    world = _world_with_seed(80)
    world.save.pilot.notoriety = 10
    world.save.pilot.credits = 0
    keys = iter(["S", "E"])  # "S" isn't a valid choice here -- must fall through, not crash
    vr.read_key = lambda: next(keys)
    world.event_rng.random = lambda: 0.0  # evade succeeds once actually reached

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_notoriety_patrol(vr.Palette(truecolor=False), world)

    assert "Surrender" not in buf.getvalue()
    assert world.save.pilot.notoriety == 10  # the stray "S" did nothing


def test_notoriety_patrol_win_raises_notoriety_further_and_flips_reputation(monkeypatch):
    world = _world_with_seed(81)
    world.save.pilot.notoriety = 4
    before_concord = world.save.pilot.reputation[vr.FACTION_CONCORD]
    before_blackwake = world.save.pilot.reputation[vr.FACTION_BLACKWAKE]
    vr.read_key = lambda: "F"

    def _one_shot_kill(world, patrol):
        patrol.hp = 0
        return 999, 0, ["one-shot kill"]

    monkeypatch.setattr(vr, "fight_round", _one_shot_kill)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_notoriety_patrol(vr.Palette(truecolor=False), world)

    assert world.save.pilot.notoriety == 4 + 3
    assert world.save.pilot.reputation[vr.FACTION_CONCORD] < before_concord
    assert world.save.pilot.reputation[vr.FACTION_BLACKWAKE] > before_blackwake


def test_notoriety_patrol_loss_wipes_notoriety_via_destroy_ship(monkeypatch):
    world = _world_with_seed(82)
    world.save.pilot.notoriety = 10
    vr.read_key = lambda: "F"

    def _one_shot_loss(world, patrol):
        world.save.ship.hull_hp = 0
        return 0, 999, ["one-shot loss"]

    monkeypatch.setattr(vr, "fight_round", _one_shot_loss)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_notoriety_patrol(vr.Palette(truecolor=False), world)

    assert world.save.pilot.notoriety == 0
    assert world.save.current_system == 0


# -- stranded-pilot rescue --------------------------------------------


def _cheapest_jump_cost(world) -> int:
    here = world.here
    return min(vr.fuel_cost_for_jump(here, world.by_id[nid]) for nid in here.connections)


def test_not_stranded_with_cargo_even_at_zero_fuel_and_credits(monkeypatch):
    world = _world_with_seed(20)
    world.save.current_system = world.here.connections[0]
    world.save.ship.fuel = 0
    world.save.pilot.credits = 0
    world.save.cargo["ore"] = 1
    assert vr.is_stranded(world) is False


def test_not_stranded_when_fuel_covers_the_cheapest_jump():
    world = _world_with_seed(21)
    world.save.current_system = world.here.connections[0]
    world.save.pilot.credits = 0
    world.save.ship.fuel = _cheapest_jump_cost(world)
    assert vr.is_stranded(world) is False


def test_not_stranded_when_credits_cover_the_fuel_shortfall():
    world = _world_with_seed(22)
    world.save.current_system = world.here.connections[0]
    world.save.ship.fuel = 0
    cheapest = _cheapest_jump_cost(world)
    world.save.pilot.credits = cheapest * 6  # exactly enough to buy the shortfall
    assert vr.is_stranded(world) is False


def test_stranded_when_no_cargo_not_enough_fuel_and_not_enough_credits():
    world = _world_with_seed(23)
    world.save.current_system = world.here.connections[0]
    world.save.ship.fuel = 0
    cheapest = _cheapest_jump_cost(world)
    world.save.pilot.credits = cheapest * 6 - 1  # one credit short
    assert vr.is_stranded(world) is True


def test_rescue_tows_a_stranded_pilot_home_and_refuels_enough_to_leave_again():
    world = _world_with_seed(24)
    away = world.here.connections[0]
    world.save.current_system = away
    world.save.ship.fuel = 0
    world.save.pilot.credits = 0
    assert vr.is_stranded(world) is True

    msg = vr.rescue_stranded_pilot(world)

    assert world.save.current_system == 0
    assert "tug" in msg.lower()
    home_cheapest = min(vr.fuel_cost_for_jump(world.by_id[0], world.by_id[nid]) for nid in world.by_id[0].connections)
    assert world.save.ship.fuel >= home_cheapest
    assert world.save.pilot.credits == 0  # no charge -- nothing to charge
    assert vr.is_stranded(world) is False


def test_rescue_when_already_home_just_tops_off_fuel_with_a_different_message():
    world = _world_with_seed(25)
    world.save.current_system = 0  # already at Freeport
    world.save.ship.fuel = 0
    world.save.pilot.credits = 0
    assert vr.is_stranded(world) is True

    msg = vr.rescue_stranded_pilot(world)

    assert world.save.current_system == 0
    assert "tug" not in msg.lower()
    assert "dockmaster" in msg.lower()
    assert vr.is_stranded(world) is False


def test_rescue_logs_a_pilot_note():
    world = _world_with_seed(26)
    world.save.current_system = world.here.connections[0]
    world.save.ship.fuel = 0
    world.save.pilot.credits = 0
    before = len(world.save.pilot.log)

    vr.rescue_stranded_pilot(world)

    assert len(world.save.pilot.log) == before + 1


def test_station_menu_auto_rescues_a_stranded_pilot_before_drawing(monkeypatch):
    """Integration-shaped: the UI layer's own hook (`screen_station_menu`),
    not just the domain functions in isolation -- proves a stranded save
    actually gets rescued on its very next menu draw, not merely that the
    domain functions work if a caller remembers to call them."""
    world = _world_with_seed(27)
    away = world.here.connections[0]
    world.save.current_system = away
    world.save.ship.fuel = 0
    world.save.pilot.credits = 0

    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        choice = vr.screen_station_menu(vr.Palette(truecolor=False), world)

    assert choice == "Q"
    assert world.save.current_system == 0
    assert not vr.is_stranded(world)
    assert "tug" in buf.getvalue().lower()


def test_bfs_hops_on_a_small_synthetic_graph():
    class _Sys:
        def __init__(self, connections):
            self.connections = connections

    by_id = {
        0: _Sys([1, 2]),
        1: _Sys([0, 3]),
        2: _Sys([0]),
        3: _Sys([1]),
    }
    hops = vr.bfs_hops(by_id, 0)
    assert hops == {0: 0, 1: 1, 2: 1, 3: 2}


# -- retirement / New Game+ ------------------------------------------------


def test_retire_pilot_increments_retirements_and_grants_cumulative_bonus():
    old_save = vr._new_career("Vet")
    old_save.pilot.retirements = 2
    old_save.pilot.credits = 300_000

    new_save = vr.retire_pilot(old_save)

    assert new_save.pilot.retirements == 3
    assert new_save.pilot.credits == 1200 + 3 * vr.RETIREMENT_STARTING_CREDITS_BONUS


def test_retire_pilot_resets_career_progress_and_keeps_handle():
    old_save = vr._new_career("Vet")
    old_save.pilot.handle = "Vet"
    old_save.pilot.credits = 300_000
    old_save.pilot.kills = 40
    old_save.pilot.missions_completed = 12
    old_save.pilot.notoriety = 9
    old_save.pilot.reputation["pirates"] = 5
    old_save.pilot.log.append("did something memorable")
    old_save.discovered = [0, 1, 2, 3]

    new_save = vr.retire_pilot(old_save)

    assert new_save.pilot.handle == "Vet"
    assert new_save.pilot.kills == 0
    assert new_save.pilot.missions_completed == 0
    assert new_save.pilot.notoriety == 0
    assert all(v == 0 for v in new_save.pilot.reputation.values())
    assert new_save.discovered == [0]
    assert any("Retired" in entry for entry in new_save.pilot.log)


def test_retire_pilot_rerolls_the_galaxy_seed():
    old_save = vr._new_career("Vet")
    original_seed = old_save.seed

    new_save = vr.retire_pilot(old_save)

    assert new_save.seed != original_seed


def test_pilot_from_dict_defaults_retirements_to_zero_for_old_saves():
    d = vr._new_career("Legacy").pilot.to_dict()
    del d["retirements"]

    pilot = vr.Pilot.from_dict(d)

    assert pilot.retirements == 0


def test_screen_status_offers_retirement_only_at_top_rank(monkeypatch):
    world = _world_with_seed(95)
    world.save.pilot.credits = 100  # far below top rank

    monkeypatch.setattr(vr, "read_key", lambda: (_ for _ in ()).throw(AssertionError("should not prompt")))
    monkeypatch.setattr(vr, "pause", lambda p, msg="Press any key to continue...": None)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_status(vr.Palette(truecolor=False), world)


def test_screen_status_retires_on_confirmation_at_top_rank(monkeypatch):
    world = _world_with_seed(96)
    world.save.pilot.credits = vr.RANKS[-1][0]
    old_seed = world.save.seed

    keys = iter(["R", "Y"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_status(vr.Palette(truecolor=False), world)

    assert world.save.pilot.retirements == 1
    assert world.save.seed != old_seed


def test_screen_status_declines_retirement_without_committing(monkeypatch):
    world = _world_with_seed(97)
    world.save.pilot.credits = vr.RANKS[-1][0]
    old_seed = world.save.seed

    keys = iter(["R", "N"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_status(vr.Palette(truecolor=False), world)

    assert world.save.pilot.retirements == 0
    assert world.save.seed == old_seed


# -- auto-route -------------------------------------------------------------


def test_bfs_path_returns_empty_for_same_start_and_dest():
    world = _world_with_seed(98)
    assert vr.bfs_path(world.by_id, 0, 0) == []


def test_bfs_path_matches_bfs_hops_distance_on_a_real_galaxy():
    world = _world_with_seed(99)
    hops = vr.bfs_hops(world.by_id, 0)
    for dest_id in (hops.keys() - {0}):
        path = vr.bfs_path(world.by_id, 0, dest_id)
        assert len(path) == hops[dest_id]
        assert path[-1] == dest_id


def test_bfs_path_every_consecutive_pair_is_a_real_connection():
    world = _world_with_seed(100)
    dest_id = next(sid for sid in world.by_id if sid != 0)
    path = vr.bfs_path(world.by_id, 0, dest_id)
    cur = 0
    for hop in path:
        assert hop in world.by_id[cur].connections
        cur = hop


def test_bfs_path_on_a_small_synthetic_graph():
    class _Sys:
        def __init__(self, connections):
            self.connections = connections

    by_id = {
        0: _Sys([1, 2]),
        1: _Sys([0, 3]),
        2: _Sys([0]),
        3: _Sys([1]),
    }
    assert vr.bfs_path(by_id, 0, 3) == [1, 3]


def test_auto_route_rejects_an_unknown_system_name(monkeypatch):
    world = _world_with_seed(101)
    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: "Nonexistent Place")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr._screen_auto_route(vr.Palette(truecolor=False), world)

    assert "no charted system matches" in buf.getvalue().lower()
    assert world.save.current_system == 0


def test_auto_route_wont_match_an_undiscovered_system_by_name(monkeypatch):
    world = _world_with_seed(102)
    undiscovered = next(s for s in world.galaxy if not s.discovered)

    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: undiscovered.name)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr._screen_auto_route(vr.Palette(truecolor=False), world)

    assert "no charted system matches" in buf.getvalue().lower()


def test_auto_route_declines_without_enough_fuel(monkeypatch):
    world = _world_with_seed(103)
    dest = next(s for s in world.galaxy if s.discovered and s.id != 0)
    world.save.ship.fuel = 0

    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: dest.name)
    calls = []
    monkeypatch.setattr(vr, "screen_travel", lambda p, w, hop_id: calls.append(hop_id))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr._screen_auto_route(vr.Palette(truecolor=False), world)

    assert "not enough fuel" in buf.getvalue().lower()
    assert calls == []


def test_auto_route_travels_every_hop_on_confirmation(monkeypatch):
    world = _world_with_seed(104)
    dest = next(s for s in world.galaxy if s.discovered and s.id != 0)
    world.save.ship.fuel = 999
    expected_path = vr.bfs_path(world.by_id, 0, dest.id)

    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: dest.name)
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)
    calls = []

    def fake_travel(p, w, hop_id):
        calls.append(hop_id)
        w.save.current_system = hop_id

    monkeypatch.setattr(vr, "screen_travel", fake_travel)

    with contextlib.redirect_stdout(io.StringIO()):
        vr._screen_auto_route(vr.Palette(truecolor=False), world)

    assert calls == expected_path
    assert world.save.current_system == dest.id


def test_auto_route_declines_on_confirmation_refusal(monkeypatch):
    world = _world_with_seed(105)
    dest = next(s for s in world.galaxy if s.discovered and s.id != 0)
    world.save.ship.fuel = 999

    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: dest.name)
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: False)
    calls = []
    monkeypatch.setattr(vr, "screen_travel", lambda p, w, hop_id: calls.append(hop_id))

    with contextlib.redirect_stdout(io.StringIO()):
        vr._screen_auto_route(vr.Palette(truecolor=False), world)

    assert calls == []


def test_auto_route_stops_early_when_a_hop_diverts_the_plan(monkeypatch):
    """Simulates a mid-route ship loss: `screen_travel` sends the pilot
    back to Freeport instead of the planned hop, and the route must not
    barrel on to the next hop regardless."""
    world = _world_with_seed(106)
    dest = next(s for s in world.galaxy if s.id != 0 and len(vr.bfs_path(world.by_id, 0, s.id)) >= 2)
    dest.discovered = True  # otherwise no far-away system is nameable yet, this early in a career
    world.save.ship.fuel = 999

    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: dest.name)
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)
    calls = []

    def fake_travel(p, w, hop_id):
        calls.append(hop_id)
        w.save.current_system = 0  # diverted back to Freeport

    monkeypatch.setattr(vr, "screen_travel", fake_travel)

    with contextlib.redirect_stdout(io.StringIO()):
        vr._screen_auto_route(vr.Palette(truecolor=False), world)

    assert len(calls) == 1


# -- landmark systems --------------------------------------------------


def test_generate_landmark_never_picks_freeport():
    world = _world_with_seed(107)
    landmark = vr.generate_landmark(world.save.seed, world.galaxy)
    assert landmark["system_id"] != 0


def test_generate_landmark_is_deterministic_for_the_same_seed():
    world = _world_with_seed(108)
    first = vr.generate_landmark(world.save.seed, world.galaxy)
    second = vr.generate_landmark(world.save.seed, world.galaxy)
    assert first == second


def test_generate_landmark_does_not_call_the_galaxy_rng():
    """The seed-determinism invariant only allows appending brand-new
    `random.Random` calls at the very end of `generate_galaxy` itself --
    proves landmark generation uses a wholly separate RNG instance and
    never perturbs an existing save's galaxy layout."""
    seed = 109
    before = vr.generate_galaxy(seed)
    vr.generate_landmark(seed, before)
    after = vr.generate_galaxy(seed)
    for a, b in zip(before, after):
        assert a.x == b.x and a.y == b.y and a.connections == b.connections


def test_world_reset_computes_a_landmark():
    world = _world_with_seed(110)
    assert world.landmark["system_id"] in world.by_id
    assert world.landmark["system_id"] != 0


def test_landmark_available_here_true_only_at_the_landmark_system_and_uninvestigated():
    world = _world_with_seed(111)
    world.save.current_system = world.landmark["system_id"]
    assert vr.landmark_available_here(world)

    world.save.flags["landmark_investigated"] = True
    assert not vr.landmark_available_here(world)


def test_landmark_available_here_false_elsewhere():
    world = _world_with_seed(112)
    other = next(sid for sid in world.by_id if sid != world.landmark["system_id"])
    world.save.current_system = other
    assert not vr.landmark_available_here(world)


def test_screen_landmark_grants_reward_once_and_sets_flag():
    world = _world_with_seed(113)
    world.save.current_system = world.landmark["system_id"]
    before_credits = world.save.pilot.credits

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_landmark(vr.Palette(truecolor=False), world)

    assert world.save.flags["landmark_investigated"] is True
    assert world.save.pilot.credits == before_credits + world.landmark["reward_credits"]
    assert not vr.landmark_available_here(world)


def test_station_menu_offers_landmark_only_when_available(monkeypatch):
    world = _world_with_seed(114)
    world.save.current_system = world.landmark["system_id"]

    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)

    assert "[L]" in buf.getvalue()

    world.save.flags["landmark_investigated"] = True
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)

    assert "[L]" not in buf2.getvalue()


def test_retiring_resets_landmark_investigated_flag():
    old_save = vr._new_career("Vet")
    old_save.flags["landmark_investigated"] = True

    new_save = vr.retire_pilot(old_save)

    assert "landmark_investigated" not in new_save.flags


# -- career highlights -------------------------------------------------


def test_pilot_highlight_appends_and_caps_at_max_highlights():
    save = vr._new_career("Recorder")
    for i in range(vr.MAX_HIGHLIGHTS + 5):
        save.pilot.highlight(f"Event {i}")
    assert len(save.pilot.highlights) == vr.MAX_HIGHLIGHTS
    assert save.pilot.highlights[-1] == f"Event {vr.MAX_HIGHLIGHTS + 4}"
    assert save.pilot.highlights[0] == "Event 5"


def test_pilot_from_dict_defaults_highlights_and_rank_seen_for_old_saves():
    d = vr._new_career("Legacy").pilot.to_dict()
    del d["highlights"]
    del d["highest_rank_seen"]

    pilot = vr.Pilot.from_dict(d)

    assert pilot.highlights == []
    assert pilot.highest_rank_seen == 0


def test_check_rank_up_fires_once_per_rank_and_records_a_highlight():
    world = _world_with_seed(115)
    world.save.pilot.credits = vr.RANKS[1][0]

    title = vr.check_rank_up(world)
    assert title == vr.RANKS[1][1]
    assert world.save.pilot.highest_rank_seen == 1
    assert any("Promoted" in h for h in world.save.pilot.highlights)

    # Same rank again -- must not re-fire.
    assert vr.check_rank_up(world) is None


def test_check_rank_up_does_not_fire_for_a_fresh_career():
    world = _world_with_seed(116)
    assert vr.check_rank_up(world) is None


def test_check_rank_up_skips_ahead_correctly_on_a_big_jump():
    world = _world_with_seed(117)
    world.save.pilot.credits = vr.RANKS[-1][0]

    title = vr.check_rank_up(world)

    assert title == vr.RANKS[-1][1]
    assert world.save.pilot.highest_rank_seen == len(vr.RANKS) - 1


def test_first_kill_records_a_highlight_but_not_the_second(monkeypatch):
    world = _world_with_seed(118)
    pirate = vr.generate_pirate(world, tier=1)

    def win_the_fight(w, target):
        target.hp = 0
        return (0, 0, [])

    monkeypatch.setattr(vr, "fight_round", win_the_fight)
    monkeypatch.setattr(vr, "read_key", lambda: "F")

    with contextlib.redirect_stdout(io.StringIO()):
        outcome = vr.screen_combat(vr.Palette(truecolor=False), world, pirate)
    assert outcome == "won"
    assert sum("First kill" in h for h in world.save.pilot.highlights) == 1

    pirate2 = vr.generate_pirate(world, tier=1)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_combat(vr.Palette(truecolor=False), world, pirate2)
    assert sum("First kill" in h for h in world.save.pilot.highlights) == 1


def test_first_mission_completion_records_a_highlight():
    world = _world_with_seed(119)
    mission = vr.Mission(id=1, kind="delivery", description="Haul food", reward=100,
                          origin_system=0, target_system=0, commodity="food", quantity=1)
    world.save.active_missions.append(mission)
    world.save.cargo["food"] = 1

    msgs = vr.check_mission_completions(world)

    assert msgs
    assert any("First mission" in h for h in world.save.pilot.highlights)


def test_hull_refit_records_a_highlight(monkeypatch):
    world = _world_with_seed(120)
    world.save.pilot.credits = 100_000
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)

    with contextlib.redirect_stdout(io.StringIO()):
        vr._hull_refit_screen(vr.Palette(truecolor=False), world, "Freighter", 5000)

    assert any("Freighter-class hull refit" in h for h in world.save.pilot.highlights)


def test_landmark_investigation_records_a_highlight():
    world = _world_with_seed(121)
    world.save.current_system = world.landmark["system_id"]

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_landmark(vr.Palette(truecolor=False), world)

    assert any(world.landmark["label"] in h for h in world.save.pilot.highlights)


def test_retire_pilot_records_a_highlight_on_the_new_career():
    old_save = vr._new_career("Vet")
    old_save.pilot.credits = vr.RANKS[-1][0]

    new_save = vr.retire_pilot(old_save)

    assert any("Retired" in h for h in new_save.pilot.highlights)


def test_screen_status_shows_career_highlights(monkeypatch):
    world = _world_with_seed(122)
    world.save.pilot.highlight("Something notable happened.")
    monkeypatch.setattr(vr, "read_key", lambda: " ")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_status(vr.Palette(truecolor=False), world)

    assert "Career highlights" in buf.getvalue()
    assert "Something notable happened." in buf.getvalue()


def test_station_menu_announces_a_promotion(monkeypatch):
    world = _world_with_seed(123)
    world.save.pilot.credits = vr.RANKS[1][0]
    keys = iter([" ", "Q"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)

    assert "Promoted to" in buf.getvalue()


# -- dump all contraband ------------------------------------------------


def _some_contraband_commodity():
    return vr.CONTRABAND_COMMODITIES[0]


def test_has_contraband_false_for_an_empty_or_legal_only_hold():
    world = _world_with_seed(124)
    assert not vr.has_contraband(world)
    world.save.cargo["food"] = 5
    assert not vr.has_contraband(world)


def test_has_contraband_true_once_any_illegal_good_is_in_cargo():
    world = _world_with_seed(125)
    world.save.cargo[_some_contraband_commodity()] = 3
    assert vr.has_contraband(world)


def test_dump_all_contraband_clears_only_illegal_goods():
    world = _world_with_seed(126)
    contraband = _some_contraband_commodity()
    world.save.cargo[contraband] = 7
    world.save.cargo["food"] = 4

    msg = vr.dump_all_contraband(world)

    assert contraband not in world.save.cargo
    assert world.save.cargo["food"] == 4
    assert "7" in msg


def test_dump_all_contraband_grants_no_credits():
    world = _world_with_seed(127)
    world.save.cargo[_some_contraband_commodity()] = 10
    before = world.save.pilot.credits

    vr.dump_all_contraband(world)

    assert world.save.pilot.credits == before


def test_screen_dump_contraband_does_nothing_without_contraband(monkeypatch):
    world = _world_with_seed(128)
    monkeypatch.setattr(vr, "read_key", lambda: (_ for _ in ()).throw(AssertionError("should not prompt")))

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_dump_contraband(vr.Palette(truecolor=False), world)


def test_screen_dump_contraband_declines_without_confirmation(monkeypatch):
    world = _world_with_seed(129)
    contraband = _some_contraband_commodity()
    world.save.cargo[contraband] = 5
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: False)

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_dump_contraband(vr.Palette(truecolor=False), world)

    assert world.save.cargo[contraband] == 5


def test_screen_dump_contraband_clears_cargo_on_confirmation(monkeypatch):
    world = _world_with_seed(130)
    contraband = _some_contraband_commodity()
    world.save.cargo[contraband] = 5
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_dump_contraband(vr.Palette(truecolor=False), world)

    assert contraband not in world.save.cargo


def test_station_menu_offers_dump_only_with_contraband_aboard(monkeypatch):
    world = _world_with_seed(131)
    monkeypatch.setattr(vr, "read_key", lambda: "Q")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)
    assert "[D]" not in buf.getvalue()

    world.save.cargo[_some_contraband_commodity()] = 2
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)
    assert "[D]" in buf2.getvalue()


# -- escort missions -----------------------------------------------------


def _accepted_escort_mission(world, dest_id, tier=1):
    hops = vr.bfs_hops(world.by_id, 0)
    mission = vr.Mission(id=999, kind="escort", description="Escort a supply convoy to Somewhere",
                          reward=500, origin_system=0, target_system=dest_id, pirate_tier=tier,
                          deadline_turn=world.save.turn + 50)
    vr.accept_mission(world, mission)
    return mission


def test_generate_mission_board_can_include_escort_missions():
    world = _world_with_seed(132)
    world.event_rng = random.Random(0)
    found = False
    for seed in range(200):
        world.event_rng = random.Random(seed)
        board = vr.generate_mission_board(world)
        if any(m.kind == "escort" for m in board):
            found = True
            break
    assert found


def test_generate_escort_mission_spans_at_least_two_hops():
    world = _world_with_seed(133)
    hops = vr.bfs_hops(world.by_id, world.save.current_system)
    mission = vr._generate_mission(world, "escort", hops)
    assert mission is not None
    assert hops[mission.target_system] >= 2


def test_escort_wave_fires_on_every_hop_while_active(monkeypatch):
    world = _world_with_seed(134)
    dest = next(sid for sid in world.by_id[0].connections)
    far_dest = next(sid for sid, h in vr.bfs_hops(world.by_id, 0).items() if h >= 2)
    mission = _accepted_escort_mission(world, far_dest)

    calls = []
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: calls.append(pirate) or "won")
    world.event_rng.random = lambda: 1.0  # suppress ordinary encounters

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest)

    assert len(calls) == 1  # the wave fired this hop
    assert mission in world.save.active_missions  # not yet at target -- still active


def test_escort_completes_and_pays_out_on_arrival_at_target(monkeypatch):
    world = _world_with_seed(135)
    dest = next(sid for sid in world.by_id[0].connections)
    mission = _accepted_escort_mission(world, dest)
    before_credits = world.save.pilot.credits

    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "won")
    world.event_rng.random = lambda: 1.0

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest)

    assert mission not in world.save.active_missions
    assert world.save.pilot.credits == before_credits + mission.reward


def test_escort_fails_on_ship_loss(monkeypatch):
    world = _world_with_seed(136)
    dest = next(sid for sid in world.by_id[0].connections)
    mission = _accepted_escort_mission(world, dest)

    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "destroyed")
    world.event_rng.random = lambda: 1.0

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest)

    assert mission not in world.save.active_missions


def test_escort_fails_on_evasion_even_though_ship_survives(monkeypatch):
    """Evading protects the player's own ship, but the convoy is left
    behind either way -- the contract still fails, unlike a bounty's own
    "escaped" outcome, which leaves the mission active to retry."""
    world = _world_with_seed(137)
    dest = next(sid for sid in world.by_id[0].connections)
    mission = _accepted_escort_mission(world, dest)

    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "escaped")
    world.event_rng.random = lambda: 1.0

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest)

    assert mission not in world.save.active_missions
    assert "failed" in buf.getvalue().lower()


def test_multiple_escort_missions_each_get_their_own_wave(monkeypatch):
    world = _world_with_seed(138)
    dest = next(sid for sid in world.by_id[0].connections)
    m1 = _accepted_escort_mission(world, dest)
    far_dest = next(sid for sid, h in vr.bfs_hops(world.by_id, 0).items() if h >= 2 and sid != dest)
    m2 = _accepted_escort_mission(world, far_dest)

    calls = []
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: calls.append(pirate) or "won")
    world.event_rng.random = lambda: 1.0

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest)

    assert len(calls) == 2
    assert m1 not in world.save.active_missions  # completed -- arrived at its target
    assert m2 in world.save.active_missions  # still en route


# -- cross-save hall of fame ----------------------------------------------


def test_load_hall_of_fame_returns_empty_list_when_missing(tmp_path):
    assert vr.load_hall_of_fame(tmp_path) == []


def test_load_hall_of_fame_returns_empty_list_on_corrupt_file(tmp_path):
    (tmp_path / "leaderboard.json").write_text("not json{{{", encoding="utf-8")
    assert vr.load_hall_of_fame(tmp_path) == []


def test_load_hall_of_fame_returns_empty_list_when_not_a_list(tmp_path):
    (tmp_path / "leaderboard.json").write_text('{"oops": true}', encoding="utf-8")
    assert vr.load_hall_of_fame(tmp_path) == []


def test_update_hall_of_fame_creates_an_entry_for_a_new_pilot(tmp_path):
    save = vr._new_career("Newcomer")
    save.pilot.credits = 5000

    vr.update_hall_of_fame(tmp_path, 42, save)

    entries = vr.load_hall_of_fame(tmp_path)
    assert len(entries) == 1
    assert entries[0]["user_id"] == 42
    assert entries[0]["handle"] == "Newcomer"
    assert entries[0]["best_credits"] == 5000
    assert entries[0]["rank"] == vr.rank_for(5000)


def test_update_hall_of_fame_never_lowers_best_credits(tmp_path):
    save = vr._new_career("Vet")
    save.pilot.credits = 10_000
    vr.update_hall_of_fame(tmp_path, 1, save)

    save.pilot.credits = 500  # a losing streak, or a retirement's own reset
    vr.update_hall_of_fame(tmp_path, 1, save)

    entries = vr.load_hall_of_fame(tmp_path)
    assert entries[0]["best_credits"] == 10_000


def test_update_hall_of_fame_refreshes_non_credit_fields_every_time(tmp_path):
    save = vr._new_career("Vet")
    save.pilot.credits = 10_000
    vr.update_hall_of_fame(tmp_path, 1, save)

    save.pilot.credits = 500
    save.pilot.retirements = 3
    save.pilot.kills = 7
    vr.update_hall_of_fame(tmp_path, 1, save)

    entries = vr.load_hall_of_fame(tmp_path)
    assert entries[0]["retirements"] == 3
    assert entries[0]["kills"] == 7
    assert entries[0]["best_credits"] == 10_000  # still the high-water mark


def test_update_hall_of_fame_keeps_separate_pilots_separate(tmp_path):
    save_a = vr._new_career("Alice")
    save_a.pilot.credits = 3000
    save_b = vr._new_career("Bob")
    save_b.pilot.credits = 7000

    vr.update_hall_of_fame(tmp_path, 1, save_a)
    vr.update_hall_of_fame(tmp_path, 2, save_b)

    entries = vr.load_hall_of_fame(tmp_path)
    assert len(entries) == 2
    assert entries[0]["handle"] == "Bob"  # sorted by best_credits, descending
    assert entries[1]["handle"] == "Alice"


def test_update_hall_of_fame_caps_at_hall_of_fame_size(tmp_path):
    for uid in range(vr.HALL_OF_FAME_SIZE + 5):
        save = vr._new_career(f"Pilot{uid}")
        save.pilot.credits = uid
        vr.update_hall_of_fame(tmp_path, uid, save)

    entries = vr.load_hall_of_fame(tmp_path)
    assert len(entries) == vr.HALL_OF_FAME_SIZE
    # The lowest-credit pilots were the ones dropped, not the highest.
    assert entries[-1]["best_credits"] == 5


def test_persist_updates_both_the_save_and_the_hall_of_fame(tmp_path):
    save = vr._new_career("Persisted")
    save.pilot.credits = 4200
    world = vr.World(save)

    vr.persist(world, tmp_path, 7)

    assert (tmp_path / "7.json").exists()
    entries = vr.load_hall_of_fame(tmp_path)
    assert entries and entries[0]["user_id"] == 7


def test_screen_hall_of_fame_shows_no_pilots_message_when_empty(tmp_path, monkeypatch):
    world = _world_with_seed(139)
    monkeypatch.setattr(vr, "read_key", lambda: " ")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_hall_of_fame(vr.Palette(truecolor=False), world, tmp_path, 1)

    assert "No pilots recorded" in buf.getvalue()


def test_screen_hall_of_fame_marks_the_current_pilot(tmp_path, monkeypatch):
    save = vr._new_career("Me")
    save.pilot.credits = 9000
    vr.update_hall_of_fame(tmp_path, 5, save)
    other = vr._new_career("Someone Else")
    other.pilot.credits = 200
    vr.update_hall_of_fame(tmp_path, 6, other)

    world = _world_with_seed(140)
    monkeypatch.setattr(vr, "read_key", lambda: " ")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_hall_of_fame(vr.Palette(truecolor=False), world, tmp_path, 5)

    text = buf.getvalue()
    assert "Me" in text and "Someone Else" in text
    me_line = next(line for line in text.splitlines() if "Me" in line and "Someone" not in line)
    assert "*" in me_line
