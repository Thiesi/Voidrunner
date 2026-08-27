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


def test_destroy_ship_clears_cargo_and_returns_player_to_freeport_with_full_hull():
    world = _world_with_seed(12)
    world.save.cargo["ore"] = 10
    world.save.current_system = world.here.connections[0]
    world.save.ship.hull_hp = 0

    vr.destroy_ship(world)

    assert world.save.cargo == {}
    assert world.save.current_system == 0
    assert world.save.ship.hull_hp == vr.hull_hp_max(world.save.ship)
    assert world.save.pilot.credits < 1200  # salvage fee charged


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
