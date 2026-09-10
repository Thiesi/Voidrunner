"""One hop, end to end: departure, encounters, customs, notoriety and
the routes that string hops together.

Split out of `test_voidrunner_domain.py` (issue #422).
"""

from __future__ import annotations

import contextlib
import io
import random
import sys

import pytest

from .support import _Sys, _VOIDRUNNER_PATH, _add_cargo, _door_stopped_at, _mission_details_world, _post_and_accept_test_mission, _set_cargo, _world_with_exploration_choice, _world_with_seed, vr


# leave the bounty active forever, turning its target system into a
# mandatory, unwinnable-at-current-gear ambush on every future visit) --


def _accept_bounty(world, *, target_system: int, pirate_tier: int = 2, reward: int = 800) -> "vr.Mission":
    mission = vr.Mission(id=1, kind="bounty", description="test bounty", reward=reward,
                          origin_system=world.save.current_system, target_system=target_system,
                          pirate_tier=pirate_tier)
    _post_and_accept_test_mission(world, mission)
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


def _travel_with_real_destruction(monkeypatch, world, dest_id: int) -> None:
    """Unlike `_travel_with_stubbed_combat`, this actually calls the
    real `destroy_ship` (setting `current_system`/`ship_destroyed_this_hop`
    exactly as a genuine combat loss would) rather than only faking the
    string `screen_combat` returns -- needed for regression tests of the
    "don't relocate to a destination the pilot never actually reached"
    fix below, which depends on those real side effects."""
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: (vr.destroy_ship(w), "destroyed")[1])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest_id)


def test_bounty_loss_does_not_relocate_to_the_unreached_destination(monkeypatch):
    """Regression guard for a real dogfood-caught bug: destroy_ship's
    own tow-home sets current_system to Freeport, but screen_travel's
    own unconditional arrival bookkeeping used to immediately overwrite
    that back to dest_id -- silently contradicting destroy_ship's own
    "you wake up at Freeport Anchorage" narration."""
    world = _world_with_seed(191)
    dest_id = world.here.connections[0]
    _accept_bounty(world, target_system=dest_id)

    _travel_with_real_destruction(monkeypatch, world, dest_id)

    assert world.save.current_system == 0


def test_destroyed_mid_hop_skips_customs_and_delivery_completion(monkeypatch):
    """A pilot towed home mid-transit was never actually *at* dest_id --
    a delivery mission targeting it must not complete, and a customs
    check (which only makes sense while actually docked somewhere)
    must not fire either."""
    world = _world_with_seed(192)
    dest_id = world.here.connections[0]
    _accept_bounty(world, target_system=dest_id)
    _add_cargo(world, vr.CONTRABAND_COMMODITIES[0], 3)
    world.by_id[dest_id].economy = "Industrial"  # not Haven, so contraband would normally risk a customs check
    world.event_rng.random = lambda: 0.0  # would force a customs check if reached

    _travel_with_real_destruction(monkeypatch, world, dest_id)

    assert vr.CONTRABAND_COMMODITIES[0] not in world.save.cargo  # destroy_ship cleared cargo, not customs


def test_dest_is_still_charted_even_when_the_ship_is_destroyed_en_route(monkeypatch):
    """Deliberately the opposite of the current_system fix above --
    charting a system's coordinates is treated as happening the moment
    something there is close enough to intercept the pilot, independent
    of whether they then survive to actually dock."""
    world = _world_with_seed(193)
    dest_id = world.here.connections[0]
    world.by_id[dest_id].discovered = False
    _accept_bounty(world, target_system=dest_id)

    _travel_with_real_destruction(monkeypatch, world, dest_id)

    assert world.by_id[dest_id].discovered is True


def test_second_escort_mission_wave_does_not_fire_after_the_first_ones_destroys_the_ship(monkeypatch):
    """Regression guard for the same class of bug as the bounty fix
    above, inside _resolve_escort_missions' own loop over multiple
    active escort contracts: a mid-loop destruction must not let a
    second contract's own wave fight a pilot who was just towed home."""
    world = _world_with_seed(194)
    dest_id = world.here.connections[0]
    m1 = vr.Mission(id=10, kind="escort", description="first convoy", reward=100,
                     origin_system=0, target_system=dest_id, pirate_tier=1,
                     deadline_turn=world.save.turn + 50)
    m2 = vr.Mission(id=11, kind="escort", description="second convoy", reward=100,
                     origin_system=0, target_system=dest_id, pirate_tier=1,
                     deadline_turn=world.save.turn + 50)
    _post_and_accept_test_mission(world, m1)
    _post_and_accept_test_mission(world, m2)
    # No bounty exists at dest_id here, so the ordinary random-encounter
    # roll isn't preempted -- world.event_rng is unseeded (see
    # _world_with_seed), so without this it can occasionally (flakily)
    # land on a derelict/distress encounter needing the real read_key(),
    # which crashes under pytest's captured stdout. Not a correctness
    # concern, just determinism.
    world.event_rng.random = lambda: 1.0

    calls = []

    def destroy_on_first_call(p, w, pirate):
        calls.append(pirate)
        vr.destroy_ship(w)
        return "destroyed"

    monkeypatch.setattr(vr, "screen_combat", destroy_on_first_call)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest_id)

    assert len(calls) == 1  # the second mission's own wave never fired
    assert world.save.current_system == 0


def test_winning_a_bounty_fight_completes_it_and_pays_the_reward(monkeypatch):
    world = _world_with_seed(21)
    dest_id = world.here.connections[0]
    mission = _accept_bounty(world, target_system=dest_id, reward=800)
    starting_credits = world.save.pilot.credits

    _travel_with_stubbed_combat(monkeypatch, world, dest_id, "won")

    assert mission not in world.save.active_missions
    assert world.save.pilot.missions_completed == 1
    assert world.save.pilot.credits == starting_credits + 800


def test_new_system_charted_announcement_prints_before_bounty_completion(monkeypatch):
    """Regression guard for a real dogfood-caught inconsistency: bounty/
    escort completions used to print *before* "New system charted",
    while delivery/scan completions (via check_mission_completions)
    always printed after it -- two similar "you arrived and this
    happened" moments reading in a different relative order depending
    on mission kind. Forces an undiscovered destination -- real bounty
    generation only ever targets already-discovered systems, but the
    ordering being tested doesn't depend on how the destination got
    into this state."""
    world = _world_with_seed(24)
    dest_id = world.here.connections[0]
    world.by_id[dest_id].discovered = False
    mission = _accept_bounty(world, target_system=dest_id, reward=500)
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "won")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest_id)
    text = buf.getvalue()

    assert mission not in world.save.active_missions
    assert "New system charted" in text and "Bounty complete" in text
    assert text.index("New system charted") < text.index("Bounty complete")


def test_new_system_charted_announcement_prints_before_escort_completion(monkeypatch):
    world = _world_with_seed(25)
    hops = vr.bfs_hops(world.by_id, world.save.current_system)
    dest_id = next(sid for sid, h in hops.items() if h == 1)
    world.by_id[dest_id].discovered = False
    mission = vr.Mission(id=2, kind="escort", description="test escort", reward=500,
                          origin_system=world.save.current_system, target_system=dest_id,
                          pirate_tier=1, deadline_turn=world.save.turn + 50)
    _post_and_accept_test_mission(world, mission)
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "won")
    # No bounty exists at dest_id here (unlike the sibling bounty test
    # above), so screen_travel's own bounty branch doesn't preempt the
    # ordinary random-encounter roll -- world.event_rng is unseeded
    # (see _world_with_seed), so without this it can occasionally
    # (flakily) land on a derelict/distress encounter, which calls the
    # real read_key() directly and crashes under pytest's captured
    # stdout. Suppressing it here isn't about correctness, just about
    # keeping this test deterministic.
    world.event_rng.random = lambda: 1.0

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest_id)
    text = buf.getvalue()

    assert mission not in world.save.active_missions
    assert "New system charted" in text and "Convoy delivered safely" in text
    assert text.index("New system charted") < text.index("Convoy delivered safely")


def test_random_encounter_uses_destination_threat_before_arrival(monkeypatch):
    """Use the advertised destination danger without moving the pilot early."""
    world = _world_with_seed(26)
    origin = world.here
    origin.danger = 3
    dest_id = world.here.connections[0]
    dest = world.by_id[dest_id]
    dest.danger = 0
    world.event_rng.random = lambda: 0.0  # always triggers an encounter
    world.event_rng.choices = lambda population, weights: ["pirate"]
    world.event_rng.randint = lambda a, b: 0  # pin generate_pirate's own +/-1 noise term

    captured_tiers = []
    real_generate_pirate = vr.generate_pirate

    def spy_generate_pirate(w, tier=None, *, danger=None):
        assert w.here.id == origin.id
        pirate = real_generate_pirate(w, tier=tier, danger=danger)
        captured_tiers.append(pirate.tier)
        return pirate

    monkeypatch.setattr(vr, "generate_pirate", spy_generate_pirate)
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "escaped")

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest_id)

    assert captured_tiers
    assert captured_tiers[0] == dest.danger == 0


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
    vr.read_key = lambda: "S"
    world.event_rng.random = lambda: 0.0  # always the salvage-success branch

    with contextlib.redirect_stdout(io.StringIO()):
        vr._encounter_derelict(vr.Palette(truecolor=False), world)

    assert world.save.pilot.credits > before_credits
    assert len(world.save.pilot.log) == before_log_len + 1


def test_derelict_board_trap_triggers_combat(monkeypatch):
    world = _world_with_seed(55)
    vr.read_key = lambda: "S"
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

    normalized = " ".join(buf.getvalue().split())
    assert "buy " in normalized and "sell " in normalized


def test_market_tip_with_no_discovered_neighbors_shows_fallback_without_crashing():
    world = _world_with_seed(60)
    dest = world.by_id[world.here.connections[0]]
    for system in world.galaxy:
        system.discovered = system.id == dest.id  # only dest itself, nothing "nearby"

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr._encounter_market_tip(vr.Palette(truecolor=False), world, dest)

    assert "nothing usable" in buf.getvalue()


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
    _add_cargo(world, "narcotics", 5)
    world.save.pilot.credits = 10_000
    monkeypatch.setattr(world.event_rng, "random", lambda: 0.99)  # affordable but refused
    vr.read_key = lambda: "P"

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_customs(vr.Palette(truecolor=False), world)

    assert world.save.pilot.notoriety == vr.NOTORIETY_PER_CUSTOMS_BUST


def test_customs_cooperative_surrender_does_not_raise_notoriety():
    world = _world_with_seed(72)
    _add_cargo(world, "narcotics", 5)
    vr.read_key = lambda: "S"

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_customs(vr.Palette(truecolor=False), world)

    assert world.save.pilot.notoriety == 0


def test_customs_successful_bribe_does_not_raise_notoriety():
    world = _world_with_seed(73)
    _add_cargo(world, "narcotics", 5)
    world.save.pilot.credits = 10_000
    world.event_rng.random = lambda: 0.0  # always the bribe-succeeds branch
    vr.read_key = lambda: "P"

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_customs(vr.Palette(truecolor=False), world)

    assert world.save.pilot.notoriety == 0


def test_wrong_bounty_kill_raises_notoriety_and_lowers_concord_rep(monkeypatch):
    world = _world_with_seed(74)
    dest_id = world.here.connections[0]
    _accept_bounty(world, target_system=dest_id)
    before_rep = world.save.pilot.reputation[vr.FACTION_CONCORD]
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "won")
    monkeypatch.setattr(vr, "new_bounty_warrant", lambda w, m: {"version": 1, "matches": False, "checked": False, "engaged": False})
    world.event_rng.random = lambda: 0.0

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest_id)

    assert world.save.pilot.notoriety == vr.NOTORIETY_PER_WRONG_BOUNTY_KILL
    assert world.save.pilot.reputation[vr.FACTION_CONCORD] < before_rep


def test_matching_bounty_win_leaves_notoriety_at_zero(monkeypatch):
    world = _world_with_seed(75)
    dest_id = world.here.connections[0]
    _accept_bounty(world, target_system=dest_id)
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "won")
    monkeypatch.setattr(vr, "new_bounty_warrant", lambda w, m: {"version": 1, "matches": True, "checked": False, "engaged": False})
    world.event_rng.random = lambda: 1.0

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

    assert "[S]" not in buf.getvalue()
    assert "UNAFFORDABLE" in buf.getvalue()
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

    monkeypatch.setattr(vr, "tactical_round", lambda w, p, t, a: _one_shot_kill(w, p))
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_notoriety_patrol(vr.Palette(truecolor=False), world)

    assert world.save.pilot.notoriety == 4 + 3
    assert world.save.pilot.reputation[vr.FACTION_CONCORD] < before_concord
    assert world.save.pilot.reputation[vr.FACTION_BLACKWAKE] > before_blackwake


def test_notoriety_patrol_loss_wipes_notoriety_via_destroy_ship(monkeypatch):
    world = _world_with_seed(82)
    world.save.pilot.notoriety = 10
    world.save.pilot.credits = 100_000
    vr.read_key = lambda: "F"

    def _one_shot_loss(world, patrol):
        world.save.ship.hull_hp = 0
        return 0, 999, ["one-shot loss"]

    monkeypatch.setattr(vr, "tactical_round", lambda w, p, t, a: _one_shot_loss(w, p))
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_notoriety_patrol(vr.Palette(truecolor=False), world)

    assert world.save.pilot.notoriety == 0
    assert world.save.current_system == 0


def _cheapest_jump_cost(world) -> int:
    here = world.here
    return min(vr.fuel_cost_for_jump(here, world.by_id[nid]) for nid in here.connections)


def test_not_stranded_with_cargo_even_at_zero_fuel_and_credits(monkeypatch):
    world = _world_with_seed(20)
    world.save.current_system = world.here.connections[0]
    world.save.ship.fuel = 0
    world.save.pilot.credits = 0
    _add_cargo(world, "ore", 1)
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


@pytest.mark.parametrize("fault", ["unknown", "uncharted", "fuel", "here", "pending", "boolean"])
def test_general_route_invalid_departure_is_read_only(fault):
    import copy
    world = _world_with_seed(103)
    target = next(s.id for s in world.galaxy if s.discovered and s.id != 0)
    if fault == "unknown": target = 999
    if fault == "uncharted": target = next(s.id for s in world.galaxy if not s.discovered)
    if fault == "fuel": world.save.ship.fuel = 0
    if fault == "here": target = 0
    if fault == "pending": world.save.pending_travel = {"existing": True}
    if fault == "boolean": target = True
    before = copy.deepcopy(world.save.to_dict()); rng = world.event_rng.getstate()
    with pytest.raises(vr.MissionError): vr.prepare_route_jump(world, target)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


@pytest.mark.parametrize("outcome", ["arrive", "divert"])
def test_general_route_flies_only_one_leg_until_another_command(monkeypatch, outcome):
    world = _world_with_seed(106)
    target = next(s for s in world.galaxy if len(vr.bfs_path(world.by_id, 0, s.id)) >= 2)
    target.discovered = True
    expected = vr.bfs_path(world.by_id, 0, target.id)[0]
    calls = []
    def travel(p, current, hop):
        calls.append(hop); current.save.current_system = hop if outcome == "arrive" else 0
    monkeypatch.setattr(vr, "screen_travel", travel)
    keys = iter("JB"); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_auto_route(vr.Palette(False), world, destination=target.id)
    assert calls == [expected]
    assert ("Last hop: arrived" if outcome == "arrive" else "Travel diverted") in output.getvalue()


@pytest.mark.parametrize("width,height", [(20,10), (40,12), (80,24)])
def test_general_route_pages_are_read_only_and_hide_unknown_details(monkeypatch, terminal, width, height):
    import copy, re
    world, mission = _mission_details_world("delivery")
    target = world.by_id[mission.target_system]; target.discovered = True
    vr.accept_mission(world, mission)
    path = vr.bfs_path(world.by_id, 0, target.id)
    for sid in path[:-1]: world.by_id[sid].discovered = False
    before = copy.deepcopy(world.save.to_dict()); rng = world.event_rng.getstate()
    terminal(width, height)
    output = io.StringIO(); frames = []
    def choose():
        frame = output.getvalue(); frames.append(frame); output.seek(0); output.truncate(0)
        match = re.search(r"Route Planner (\d+)/(\d+)", " ".join(frame.split()))
        assert match and len(frames) < 200
        return "B" if match[1] == match[2] else "N"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output): vr.screen_auto_route(vr.Palette(False), world, destination=target.id)
    assert all(len(frame.splitlines()) <= height for frame in frames)
    assert all(vr._visible_width(line) <= width for frame in frames for line in frame.splitlines())
    text = " ".join(" ".join(frames).split())
    assert target.name in text and "Contract #1" in text
    for sid in path[:-1]: assert world.by_id[sid].name not in text
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


def test_general_route_opens_on_a_destination_and_cancel_preserves_the_preview(monkeypatch):
    """The planner has nothing to show without a destination, so it picks one first
    (issue #415); cancelling a later change keeps the route already previewed."""
    world = _world_with_seed(42); chosen = next(s.id for s in world.galaxy if s.discovered and s.id != 0)
    destinations = iter([chosen, None]); choices_seen = []
    def pick(title, options):
        choices_seen.extend(options); return next(destinations)
    monkeypatch.setattr(vr, "_pick_trade_field", pick)
    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: pytest.fail("Entry asked a question"))
    keys = iter("DB"); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output: vr.screen_auto_route(vr.Palette(False), world)
    text = output.getvalue()
    assert text.count("Destination: " + world.by_id[chosen].name) == 2
    assert all(world.by_id[sid].discovered for sid, label in choices_seen)
    assert world.save.turn == 0


def test_cancelling_the_opening_route_picker_leaves_without_a_planner(monkeypatch):
    world = _world_with_seed(42)
    monkeypatch.setattr(vr, "_pick_trade_field", lambda title, options: None)
    monkeypatch.setattr(vr, "read_key", lambda: pytest.fail("Back from the picker must leave at once"))
    with contextlib.redirect_stdout(io.StringIO()) as output: vr.screen_auto_route(vr.Palette(False), world)
    assert "Route Planner" not in output.getvalue() and world.save.turn == 0


def test_general_route_all_destinations_reachable_in_compact_picker(monkeypatch, terminal):
    import re
    world = _world_with_seed(200)
    for station in world.galaxy: station.discovered = True
    terminal(20, 10)
    options = sorted([(s.id, s.name) for s in world.galaxy], key=lambda item: item[1])
    output = io.StringIO(); frames = []
    def choose():
        frame = output.getvalue(); frames.append(frame); output.seek(0); output.truncate(0)
        # Titles can wrap between words at 20 columns.
        match = re.search(r"Charted Destination (\d+)/(\d+)", " ".join(frame.split()))
        assert match
        if match[1] != match[2]: return "N"
        return re.findall(r"\[(\d)\] ", frame)[-1]
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output): selected = vr._pick_trade_field("Charted Destination", options)
    assert selected == options[-1][0]
    assert all(len(frame.splitlines()) <= 10 for frame in frames)
    assert all(vr._visible_width(line) <= 20 for frame in frames for line in frame.splitlines())


@pytest.mark.parametrize("commands", [b"CG1BQQ", b"CG1", b"CG1D1BQQ", b"CG1D1"])
def test_real_general_route_back_and_eof_preserve_career(tmp_path, commands):
    import json, os, subprocess
    world = _world_with_seed(42)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77); world.checkpoint()
    original = (tmp_path / "77.json").read_bytes()
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id":77, "handle":"Tester"}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=commands, capture_output=True,
        env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)), timeout=10)
    assert result.returncode == 0 and not result.stderr and b"Route Planner" in result.stdout
    if b"D1" in commands: assert b"Destination:" in result.stdout
    assert b"Charted Destination" in result.stdout  # the planner picks before it plans (#415)
    assert (tmp_path / "77.json").read_bytes() == original


@pytest.mark.parametrize("title", ["Charted Destination", "Destination", "Inspect Station"])
def test_destination_picker_keeps_wrapped_names_on_one_page(monkeypatch, terminal,title):
    import re
    terminal(20, 10)
    options=[(1,"Alpha"),(2,"Beta"),(3,"Yellowstone Deep"),(4,"Zeta")]
    output=io.StringIO(); frames=[]
    def choose():
        frame=output.getvalue();frames.append(frame);output.seek(0);output.truncate(0)
        match=re.search(re.escape(title)+r" (\d+)/(\d+)"," ".join(frame.split()))
        assert match
        return "B" if match[1]==match[2] else "N"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output): assert vr._pick_trade_field(title,options) is None
    containing=[frame for frame in frames if "Yellowstone" in frame or "Deep" in frame]
    assert len(containing)==1
    assert "Yellowstone" in containing[0] and "Deep" in containing[0]
    keyed=re.findall(r"\[(\d)\] Yellowstone",containing[0])
    assert len(keyed)==1 and re.search(r"^\s{4}Deep",containing[0],re.M)  # one key; the continuation is indented (#411)
    assert all(len(frame.splitlines())<=10 for frame in frames)
    assert all(vr._visible_width(line)<=20 for frame in frames for line in frame.splitlines())


@pytest.mark.parametrize("label",["Yellowstone Deep", "A very long station name " * 12])
def test_tiny_picker_keeps_oversized_label_as_one_read_through_choice(monkeypatch, terminal,label):
    terminal(15, 10)
    output=io.StringIO();frames=[]
    def choose():
        frame=output.getvalue();frames.append(frame);output.seek(0);output.truncate(0)
        assert len(frames)<200
        if "[1] Pick" in frame:return "1"
        assert "[1]" not in frame
        return "N"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):selected=vr._pick_trade_field("Charted Destination",[(77,label)])
    assert selected==77
    assert all("Choice 1" in frame for frame in frames)
    assert all(len(frame.splitlines())<=10 for frame in frames)
    assert all(vr._visible_width(line)<=15 for frame in frames for line in frame.splitlines())
    rows=[]
    for frame in frames:
        for row in frame.splitlines():
            if row.startswith("["):break            # the action bar, and its wrapped tail
            if row and row not in ("N","P") and not row.startswith("Choice "):rows.append(row)
    assert " ".join(" ".join(rows).split())==" ".join(label.split())


def test_oversized_picker_ignores_selection_on_incomplete_parts(monkeypatch, terminal):
    terminal(15, 10)
    output=io.StringIO();attempted=False
    def choose():
        nonlocal attempted
        frame=output.getvalue();output.seek(0);output.truncate(0)
        if not attempted:
            attempted=True
            assert "[1] Pick" not in frame
            return "1"
        return "1" if "[1] Pick" in frame else "N"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):assert vr._pick_trade_field("Destination",[(9,"unusually long label "*20)])==9


@pytest.mark.parametrize("back", [False, True])
def test_oversized_picker_keeps_choice_identity_when_returning_from_next_option(monkeypatch, terminal, back):
    terminal(15, 10)
    output = io.StringIO()
    returned = False
    frames = []
    def choose():
        nonlocal returned
        frame = output.getvalue()
        output.seek(0); output.truncate(0)
        frames.append(frame)
        assert len(frames) < 200
        if "[1] Next" in frame:
            returned = True
            return "P"
        if returned:
            assert "Choice 1" in frame and "[1] Pick" in frame
            return "B" if back else "1"
        return "N"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output):
        selected = vr._pick_trade_field("Destination", [(7, "unusually long label " * 20), (8, "Next")])
    assert returned
    assert selected == (None if back else 7)


@pytest.mark.parametrize("width,height",[(20,10),(40,10),(39,24),(40,12),(80,24)])
def test_map_list_advertises_only_available_view_actions(monkeypatch, terminal,width,height):
    terminal(width, height)
    world=_world_with_seed(42);before=world.save.to_dict()
    compact=width<40 or height<12
    commands=iter(["M","N","P","B"] if compact else ["L","M","B"])
    output=io.StringIO();frames=[]
    def choose():
        frame=output.getvalue();output.seek(0);output.truncate(0);frames.append(frame)
        if "Charted Systems" in " ".join(frame.split()):
            assert ("[M] Map" in frame) is not compact
        return next(commands)
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):vr.screen_galaxy_map(vr.Palette(False),world)
    if compact:
        assert all("Star Map:" not in frame for frame in frames)
    else:
        assert "Star Map:" in frames[-1]
    assert world.save.to_dict()==before


def test_general_route_fuel_topups_do_not_require_whole_route_in_tank():
    world = _world_with_seed(42)
    target = max(world.galaxy, key=lambda s: len(vr.bfs_path(world.by_id, 0, s.id)))
    target.discovered = True; path = vr.bfs_path(world.by_id, 0, target.id)
    world.save.ship.fuel = vr.fuel_cost_for_jump(world.here, world.by_id[path[0]], world.save.ship)
    assert vr.prepare_route_jump(world, target.id) == path[0]
    text = " ".join(vr.navigation_route_lines(world, target.id))
    assert "Refuelling is manual" in text and "Additional fuel cash" in text


def test_large_legacy_bounty_route_uses_bounded_contract_passes(monkeypatch):
    import json
    world=_world_with_seed(42)
    target=world.here.connections[0]
    data=world.save.to_dict()
    data["active_missions"]=[{"id":i+1,"kind":"bounty","description":"Legacy bounty", "reward":1,"origin_system":0,"target_system":target} for i in range(20_000)]
    assert len(json.dumps(data).encode("utf-8")) < vr.MAX_SAVE_BYTES
    world=vr.World(vr.SaveData.from_dict(json.loads(json.dumps(data))))
    class CountedMissions(list):
        visits=0
        def __iter__(self):
            for value in super().__iter__():
                self.visits+=1
                assert self.visits <= 3*len(self), "Repeated scans of a preserved large career"
                yield value
    missions=CountedMissions(world.save.active_missions);world.save.active_missions=missions
    calls=[];original=vr.bfs_path
    def path(*args):
        calls.append(args[1:])
        return original(*args)
    monkeypatch.setattr(vr,"bfs_path",path)
    lines=vr.route_mission_implications(world,[target])
    assert len(lines)==20_002
    assert "Contract #20000: objective day 39999" in lines[-1]
    assert len(calls)==1


@pytest.mark.parametrize("route", ["empty","one_visit","two_visits"])
def test_route_bounty_counts_preserve_target_order_and_skip_expired_jobs(route):
    world=_world_with_seed(42);world.save.turn=2
    first,second=world.here.connections[:2]
    world.save.active_missions=[
        vr.Mission(1,"bounty","Expired",1,0,first,deadline_turn=1),
        vr.Mission(2,"bounty","First",1,0,first,deadline_turn=2),
        vr.Mission(3,"bounty","Other target",1,0,second),
        vr.Mission(4,"bounty","Second",1,0,first)]
    path=[] if route=="empty" else ([first] if route=="one_visit" else [first,0,first])
    end=path[-1] if path else 0
    expected=[]
    for mission in world.save.active_missions:
        arrivals=[i for i,sid in enumerate(path,1) if sid==mission.target_system]
        needed=vr.preceding_bounties(world,mission)+1
        onward=vr.bfs_path(world.by_id,end,mission.target_system)
        jumps=arrivals[needed-1] if len(arrivals)>=needed else len(path)+len(onward)+2*(needed-len(arrivals)-bool(onward))
        expected.append(f"Contract #{mission.id}: objective day {world.save.turn+jumps} ")
    before=world.save.to_dict()
    lines=vr.route_mission_implications(world,path)[2:]
    assert all(line.startswith(prefix) for line,prefix in zip(lines,expected))
    assert world.save.to_dict()==before


def test_general_route_deadlines_include_enroute_objectives_and_bounty_queue():
    world = _world_with_seed(42)
    target = max(world.galaxy, key=lambda s: len(vr.bfs_path(world.by_id, 0, s.id)))
    path = vr.bfs_path(world.by_id, 0, target.id); first = path[0]
    world.save.active_missions = [
        vr.Mission(1, "delivery", "Enroute delivery", 500, 0, first, commodity="food", quantity=3, deadline_turn=1),
        vr.Mission(2, "bounty", "First bounty", 500, 0, first, pirate_tier=1),
        vr.Mission(3, "bounty", "Queued bounty", 500, 0, first, pirate_tier=1, deadline_turn=1)]
    lines = vr.route_mission_implications(world, path)
    assert "arrival day 1" in lines[2] and "within deadline" in lines[2] and "Missing delivery cargo" in lines[2]
    assert "objective day 1" in lines[3]
    assert "TRAVEL ESTIMATE LATE" in lines[4]


@pytest.mark.parametrize("destination", ["target","elsewhere","current"])
def test_charted_survey_has_no_promised_completion_or_contract_departure(destination,monkeypatch):
    import copy
    world,mission=_mission_details_world("scan");vr.accept_mission(world,mission)
    world.by_id[mission.target_system].discovered=True
    target=mission.target_system if destination=="target" else (0 if destination=="current" else next(s.id for s in world.galaxy if s.id not in (0,mission.target_system)))
    before=copy.deepcopy(world.save.to_dict());rng=world.event_rng.getstate()
    text=" ".join(vr.route_mission_implications(world,vr.bfs_path(world.by_id,0,target)))
    assert "BLOCKED SURVEY" in text and "no completion day" in text
    assert "objective day" not in text and "within deadline" not in text
    assert "survey blocked" in vr.mission_bearing(world,mission)
    assert "BLOCKED SURVEY" in " ".join(vr.mission_details(world,mission))
    assert "BLOCKED SURVEY" in " ".join(vr.mission_navigation_lines(world,mission,active=True))
    with pytest.raises(vr.MissionError,match="already charted"): vr.prepare_mission_jump(world,mission.id)
    keys=iter("JB");monkeypatch.setattr(vr,"read_key",lambda:next(keys))
    monkeypatch.setattr(world,"checkpoint",lambda:pytest.fail("Blocked survey saved"))
    with contextlib.redirect_stdout(io.StringIO()) as output: vr.screen_mission_navigation(vr.Palette(False),world,mission,active=True)
    assert "[J] Jump next" not in output.getvalue()
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng


def test_failed_discovery_leaves_survey_blocked_after_save_reload_and_revisit(tmp_path,monkeypatch):
    world=_world_with_seed(42);world.event_rng.seed(0)
    target=sorted(world.here.connections)[0];world.by_id[target].discovered=False
    mission=vr.Mission(1,"scan","Survey after failed flight",500,0,target)
    world.save.active_missions=[mission]
    world._checkpoint=lambda current:vr.persist(current,tmp_path,77);world.checkpoint()
    monkeypatch.setattr(vr,"_resolve_random_travel_encounter",lambda p,w,d:vr.destroy_ship(w))
    with contextlib.redirect_stdout(io.StringIO()): vr.screen_travel(vr.Palette(False),world,target)
    loaded,_,_=vr.load_or_create_save(tmp_path,77,"Tester");resumed=vr.World(loaded)
    assert resumed.by_id[target].discovered and resumed.save.active_missions and resumed.here.id==0
    assert "BLOCKED SURVEY" in " ".join(vr.route_mission_implications(resumed,[target]))
    credits=resumed.save.pilot.credits
    monkeypatch.setattr(vr,"_resolve_random_travel_encounter",lambda *args:None)
    with contextlib.redirect_stdout(io.StringIO()): vr.screen_travel(vr.Palette(False),resumed,target)
    assert resumed.save.active_missions and resumed.save.pilot.credits==credits


def test_uncharted_survey_estimate_uses_the_first_new_discovery_on_route():
    world,mission=_mission_details_world("scan");vr.accept_mission(world,mission)
    path=vr.bfs_path(world.by_id,0,mission.target_system)
    text=" ".join(vr.route_mission_implications(world,path))
    assert f"objective day {len(path)}" in text and "BLOCKED" not in text


@pytest.mark.parametrize("order", ["same_arrival", "reverse_arrivals", "expired_first", "short_first"])
def test_general_route_delivery_estimates_allocate_cargo_in_resolution_order(order):
    import copy
    world = _world_with_seed(42)
    destination = max(world.galaxy, key=lambda s: len(vr.bfs_path(world.by_id,0,s.id)))
    path = vr.bfs_path(world.by_id,0,destination.id)
    first, second = path[0], path[1]
    a = vr.Mission(1,"delivery","First delivery",500,0,first,commodity="food",quantity=3,deadline_turn=10)
    b = vr.Mission(2,"delivery","Second delivery",500,0,first,commodity="food",quantity=3,deadline_turn=10)
    if order == "reverse_arrivals": a.target_system=second
    if order == "expired_first": a.deadline_turn=0
    if order == "short_first": a.quantity=4
    world.save.active_missions=[a,b]; _set_cargo(world, {"food":3})
    before=copy.deepcopy(world.save.to_dict()); rng=world.event_rng.getstate()
    lines=vr.route_mission_implications(world,path)
    rows={mission.id:next(line for line in lines if line.startswith(f"Contract #{mission.id}:")) for mission in (a,b)}
    missing = 2 if order == "same_arrival" else 1
    if order == "expired_first":
        assert "TRAVEL ESTIMATE LATE" in rows[1] and "Missing delivery cargo" not in rows[2]
    else:
        assert "Missing delivery cargo" in rows[missing] and "completion day unknown" in rows[missing]
        assert "Missing delivery cargo" not in rows[3-missing]
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng


def test_invalid_customs_input_waits_without_mutating_cargo(monkeypatch):
    world = _world_with_seed(42)
    _set_cargo(world, {"weapons": 2})
    before = __import__("copy").deepcopy(world.save.to_dict())
    keys = iter(["?", "\r", "S"])

    def choose():
        assert world.save.to_dict() == before
        return next(keys)

    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_customs(vr.Palette(False), world)
    assert world.save.cargo == {}


def test_auto_route_checkpoints_each_completed_hop_before_next_hop(tmp_path, monkeypatch):
    world = _world_with_seed(42)
    for system in world.galaxy:
        system.discovered = True
    target = next(sid for sid, hops in vr.bfs_hops(world.by_id, 0).items() if hops == 3)
    path = vr.bfs_path(world.by_id, 0, target)
    world.save.ship.engine_tier = 3
    world.save.ship.fuel = vr.fuel_capacity(world.save.ship)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    vr.persist(world, tmp_path, 77)
    keys = iter("J" * len(path) + "B")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    visited = []

    def travel(p, current, dest):
        saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert saved.current_system == (visited[-1] if visited else 0)
        current.save.current_system = dest
        current.save.turn += 1
        visited.append(dest)

    monkeypatch.setattr(vr, "screen_travel", travel)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_auto_route(vr.Palette(False), world, destination=target)
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert visited == path
    assert saved.current_system == target
    assert saved.turn == 3


# Every recorded checkpoint must be a valid restart boundary, including the
# gap between a combat's terminal result and its parent's mission payout.
@pytest.mark.parametrize(
    "scenario,seed,keys,evidence",
    [
        ("quiet", 0, "F", "Jumping to"),
        ("pirate", 2, "F", "Raider contact"),
        ("squadron", 45, "F", "squadron contact: 2"),
        ("squadron_switch", 45, "T", "Target selected"),
        ("salvage", 26, "S", "Salvaged a derelict"),
        ("ambush", 160, "S", "weren't as dead"),
        ("distress", 8, "H", "Grateful survivors"),
        ("tip", 9, "F", "trader's data burst"),
        ("ignore_derelict", 26, "?I", "leave the derelict"),
        ("ignore_distress", 8, "?I", "continue past the distress"),
        ("bounty", 0, "F", "Bounty complete!"),
        ("bounty_brace", 0, "GFFG", "Guarding;"),
        ("bounty_report", 0, "VR", "Incorrect warrant closed"),
        ("bounty_withdraw", 0, "W", "withdraw before engaging"),
        ("bounty_loss", 0, "F", "Bounty failed"),
        ("bounty_escape", 0, "E", "escape"),
        ("bounty_dump", 0, "D", "dump cargo"),
        ("bounty_bribe", 0, "P", "peels off"),
        ("escorts", 0, "F", "Convoy delivered safely"),
        ("escort_loss", 0, "F", "Escort contract failed"),
        ("patrol_win", 9, "F", "Concord will not forget"),
        ("patrol_loss", 9, "F", "Freeport Anchorage"),
        ("patrol_surrender", 9, "S", "Notoriety cleared"),
        ("patrol_evade", 9, "EEEEEEEE", "break contact and escape"),
        ("customs_surrender", 4, "FS", "surrender 2 units"),
        ("customs_bribe", 4, "FP", "changes hands quietly"),
    ],
)
def test_every_travel_checkpoint_resumes_to_the_same_career(
    tmp_path, monkeypatch, scenario, seed, keys, evidence,
):
    import json

    world = _world_with_seed(42)
    world.event_rng.seed(seed)
    world.save.ship.hull_class = "Carrier"
    world.save.ship.hull_hp = vr.hull_hp_max(world.save.ship)
    world.save.ship.fuel = vr.fuel_capacity(world.save.ship)
    world.save.ship.weapon_tier = 3
    world.save.ship.shield_tier = 3
    world.save.pilot.credits = 10_000
    dest_id = next(s.id for s in world.galaxy if s.danger >= 4)
    if scenario == "bounty_report":
        monkeypatch.setattr(vr, "new_bounty_warrant", lambda w, m: {"version": 1, "matches": False, "checked": False, "engaged": False})
    if scenario.startswith("bounty") or scenario.startswith("customs"):
        if scenario.startswith("customs"):
            world.save.current_system = world.here.connections[0]
            dest_id = 0
            world.save.ship.weapon_tier = 4
        world.save.active_missions = [
            vr.Mission(1, "bounty", "Test bounty", 500, world.save.current_system,
                       dest_id, pirate_tier=0 if scenario.startswith("customs") else 2),
        ]
    if scenario in ("bounty_dump", "customs_surrender", "customs_bribe"):
        _set_cargo(world, {"weapons": 2})
    if scenario in ("escorts", "escort_loss"):
        # Two jobs to the same station with the same terms bar the reward: the
        # snapshot, not the id, is what tells the resumed wave which one it is.
        world.save.active_missions = [
            vr.Mission(7, "escort", "Convoy A", 500, 0, dest_id, pirate_tier=2),
            vr.Mission(9, "escort", "Convoy B", 600, 0, dest_id, pirate_tier=1),
        ]
        world.save.next_mission_id = 10
    if scenario.startswith("patrol"):
        world.save.pilot.notoriety = 20
    if scenario.endswith("loss"):
        world.save.ship.hull_hp = 1
        world.save.ship.weapon_tier = 0
        world.save.ship.shield_tier = 0
    # Exercise departure costs/settlement and arrival delivery in the same hop.
    world.save.ship.has_navigator = True
    world.save.active_futures = [vr.FuturesContract(1, "food", 2, 40, world.save.turn + 1,
                                                   world.save.current_system, 36, 2)]
    world.save.active_missions.append(
        vr.Mission(8, "delivery", "Food delivery", 200, world.save.current_system,
                   dest_id, commodity="food", quantity=2, deadline_turn=world.save.turn + 5)
    )
    snapshots = []
    position = 0

    def choose():
        nonlocal position
        choice = keys[position] if position < len(keys) else "F"
        position += 1
        assert position < 100, "scenario failed to terminate"
        return choice

    def record(current):
        snapshots.append((json.loads(json.dumps(current.save.to_dict())), position))

    monkeypatch.setattr(vr, "read_key", choose)
    world._checkpoint = record
    world.checkpoint()
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        vr.screen_travel(vr.Palette(False), world, dest_id)
    expected = json.loads(json.dumps(world.save.to_dict()))
    assert evidence in output.getvalue() + " ".join(world.save.pilot.log)
    assert expected["pending_travel"] is None
    assert len(snapshots) >= 5
    for saved, next_key in snapshots:
        if saved["pending_travel"] is None and saved["turn"] == expected["turn"]:
            continue  # this hop has already completed
        vr.write_save(tmp_path, 77, vr.SaveData.from_dict(saved))
        loaded, is_new, notice = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert not is_new and notice is None
        resumed = vr.World(loaded)
        position = next_key
        with contextlib.redirect_stdout(io.StringIO()):
            vr.screen_travel(vr.Palette(False), resumed, dest_id)
        actual = json.loads(json.dumps(resumed.save.to_dict()))
        assert actual == expected, (scenario, saved["pending_travel"], next_key)


def test_engineer_pays_for_hire_and_wages_on_twenty_medium_jumps():
    world = _world_with_seed(42)
    class Start:
        x, y = 0, 0
    class End:
        x, y = 30, 0
    base = vr.fuel_cost_for_jump(Start(), End())
    world.save.ship.has_engineer = True
    saved = base - vr.fuel_cost_for_jump(Start(), End(), world.save.ship)
    role = vr.CREW_ROLES["engineer"]
    assert 20 * (saved * 6 - role["wage"]) >= role["hire_cost"]
    assert role["wage"] < 6


def test_notoriety_above_one_hundred_survives_checkpoint_and_restart(tmp_path):
    world = _world_with_seed(42)
    world.save.pilot.notoriety = 99
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    world.save.pilot.notoriety += 3  # A patrol kill crosses the former artificial cap.
    world.checkpoint()
    loaded, is_new, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert not is_new and loaded.pilot.notoriety == 102
    loaded.pilot.notoriety = 1000
    vr.write_save(tmp_path, 77, loaded)
    assert vr.load_or_create_save(tmp_path, 77, "Tester")[0].pilot.notoriety == 1000


def test_unaffordable_customs_bribe_is_harmless_and_keeps_inspection_pending(monkeypatch):
    import copy
    world = _world_with_seed(42)
    _set_cargo(world, {"weapons": 2, "food": 1})
    world.save.pilot.credits = 0
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    calls = []
    def choose():
        if not calls:
            calls.append(1)
            return "P"
        assert world.save.to_dict() == before
        assert world.event_rng.getstate() == rng
        raise EOFError
    monkeypatch.setattr(vr, "read_key", choose)
    world._checkpoint = lambda current: pytest.fail("Unaffordable bribe checkpointed")
    with contextlib.redirect_stdout(io.StringIO()) as output, pytest.raises(EOFError):
        vr.screen_customs(vr.Palette(False), world)
    assert "Insufficient credits" in output.getvalue()
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
@pytest.mark.parametrize("credits", [0, 10_000])
@pytest.mark.parametrize("style", ["auto", "plain"])
def test_customs_pages_keep_complete_terms_without_mutation(monkeypatch, terminal, without_action_bar, width, height, credits, style):
    import copy, re
    terminal(width, height, style)
    world = _world_with_seed(42)
    _set_cargo(world, {"weapons": 2, "food": 1})
    world.save.pilot.credits = credits
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    frames, content = [], []
    output = io.StringIO()
    def choose():
        frame = output.getvalue(); output.seek(0); output.truncate(0)
        frames.append(frame)
        assert len(frame.splitlines()) <= height
        assert all(vr._visible_width(line) <= width for line in frame.splitlines())
        bar = " ".join(frame.split())                    # a narrow bar wraps (#400)
        assert "[S] Surrender" in bar and ("[<>] Page:" in bar or "/1" in bar)
        if not credits: assert "[B]" not in frame
        plain = vr._ANSI_RE.sub("", frame)
        content.append(re.sub(r"^[\s>]*Customs\s+[\d,]+cr\s+\d+/\d+", "", without_action_bar(plain)))
        assert world.save.to_dict() == before and world.event_rng.getstate() == rng
        page, count = map(int, re.search(r"Customs.*?(\d+)/(\d+)", frame, re.S).groups())
        if page == count: raise EOFError
        return ">"
    monkeypatch.setattr(vr, "read_key", choose)
    world._checkpoint = lambda current: pytest.fail("Browsing customs checkpointed")
    with contextlib.redirect_stdout(output), pytest.raises(EOFError):
        vr.screen_customs(vr.Palette(False), world)
    text = " ".join(" ".join(content).split())
    for phrase in ("2 units", "pay no fine", "60% acceptance", "Pay only if accepted", "If refused:", "no debt", "notoriety"):
        assert phrase in text


@pytest.mark.parametrize("action", ["?", "", "Q", "b", None])
def test_customs_invalid_domain_decision_has_no_effects(action):
    import copy
    world = _world_with_seed(42)
    _set_cargo(world, {"weapons": 2})
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    with pytest.raises(ValueError): vr.resolve_customs(world, action)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


def _world_waiting_at_customs():
    world = _world_with_seed(42)
    destination = next(i for i in world.here.connections if world.by_id[i].economy != "Haven")
    world.save.current_system = destination
    world.by_id[destination].discovered = True
    world.save.turn = 1
    _set_cargo(world, {"weapons": 2, "food": 1})
    world.save.pending_travel = {"version": 1, "origin": 0, "destination": destination,
        "was_discovered": False, "destroyed": False, "phase": "customs", "primary": "random",
        "bounty": None, "escorts": [], "escort_index": 0, "encounter": {"inspect": True}}
    return world


@pytest.mark.parametrize("commands,credits", [(b">", 10_000), (b"P>", 0), (b"?<>", 0)])
def test_real_customs_browsing_and_rejected_bribe_preserve_pending_save(tmp_path, commands, credits):
    import json, os, subprocess
    world = _world_waiting_at_customs()
    world.save.pilot.credits = credits
    world.save.pilot.highest_rank_seen = len(vr.RANKS) - 1
    vr.persist(world, tmp_path, 77)
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert saved.pending_travel["phase"] == "customs"
    before = (tmp_path / "77.json").read_bytes()
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester", "terminal_width": 40, "terminal_height": 12}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=commands,
        capture_output=True, timeout=10, env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)))
    assert result.returncode == 0 and not result.stderr
    assert b"Resuming your interrupted journey" in result.stdout and b"Customs" in result.stdout
    assert b"Command Deck" not in result.stdout
    if b"P" in commands: assert b"Insufficient credits" in result.stdout
    assert (tmp_path / "77.json").read_bytes() == before


@pytest.mark.parametrize("decision", ["surrender", "accepted", "refused"])
def test_customs_result_checkpoint_and_replay_do_not_repeat_effects(tmp_path, decision):
    world = _world_waiting_at_customs()
    cost = vr.customs_quote(world)[1]
    world.save.pilot.credits = cost
    world.event_rng.seed(0 if decision == "refused" else 1)
    vr.persist(world, tmp_path, 77)
    marker = b"You surrender" if decision == "surrender" else b"changes hands quietly" if decision == "accepted" else b"Bribe refused"
    command = b">S" if decision == "surrender" else b">P"
    with _door_stopped_at(tmp_path, command, marker):
        saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert saved.pending_travel["encounter"]["done"]
        assert saved.cargo["food"] == 1
        assert saved.pilot.credits == (cost if decision == "surrender" else 0)
        if decision == "accepted": assert saved.cargo["weapons"] == 2
        else:
            assert "weapons" not in saved.cargo
            assert saved.trading_ledger.cargo_loss_cost > 0
        if decision == "refused":
            assert saved.pilot.notoriety == vr.NOTORIETY_PER_CUSTOMS_BUST
            assert f"{cost}cr collected" in saved.pending_travel["encounter"]["result"][0]
    replay = vr.World(saved)
    before, rng = replay.save.to_dict(), replay.event_rng.getstate()
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_customs(vr.Palette(False), replay)
    assert marker.decode() in output.getvalue()
    assert replay.save.to_dict() == before and replay.event_rng.getstate() == rng


@pytest.mark.parametrize("fuel", [0, 1, 2, 3, 4, 5])
def test_distress_cost_preview_matches_each_possible_draw(monkeypatch, fuel):
    for draw in (2, 3, 4):
        world = _world_with_seed(42); world.save.ship.fuel = fuel
        before = world.save.pilot.credits
        costs = iter([draw, 180])
        monkeypatch.setattr(world.event_rng, "randint", lambda low, high: next(costs))
        monkeypatch.setattr(vr, "read_key", lambda: "H")
        text = " ".join(vr.distress_terms(world))
        low, high = min(fuel, 2), min(fuel, 4)
        assert f"spend {low if low == high else str(low) + '-' + str(high)} fuel" in text
        assert ("tank empty" in text) == (fuel <= 4)
        with contextlib.redirect_stdout(io.StringIO()): vr._encounter_distress_call(vr.Palette(False), world)
        assert world.save.ship.fuel == fuel - min(fuel, draw)
        assert world.save.pilot.credits == before + 180
        assert world.save.pilot.reputation[vr.FACTION_CONCORD] == 3


@pytest.mark.parametrize("kind", ["derelict", "distress"])
def test_real_exploration_browsing_and_disconnect_preserve_pending_save(tmp_path, kind):
    world = _world_with_exploration_choice(kind); vr.persist(world, tmp_path, 77)
    path = tmp_path / "77.json"; before = path.read_bytes()
    with _door_stopped_at(tmp_path, b">?<", b": <") as output:
        # This echo exists only after both navigation keys and invalid input were read.
        # The prompt itself is matched by its trailing colon, because a one-page screen
        # drops its paging tokens and no longer ends in "Page: " (#412).
        assert b": >" in output and b": ?" in output and b": <" in output
        assert path.read_bytes() == before


@pytest.mark.parametrize("kind,key,marker", [("derelict", b"I", b"leave the derelict"), ("distress", b"H", b"Grateful survivors")])
def test_real_exploration_decision_is_durable_before_result(tmp_path, kind, key, marker):
    world = _world_with_exploration_choice(kind); vr.persist(world, tmp_path, 77)
    fuel, credits = world.save.ship.fuel, world.save.pilot.credits
    with _door_stopped_at(tmp_path, key, marker):
        saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        if kind == "distress":
            assert fuel - 4 <= saved.ship.fuel <= fuel - 2
            assert credits + 60 <= saved.pilot.credits <= credits + 180
            assert saved.pilot.reputation[vr.FACTION_CONCORD] == 3
        else: assert saved.ship.fuel == fuel and saved.pilot.credits == credits


@pytest.mark.parametrize("field", ["workshop_spend", "workshop_material_cost"])
@pytest.mark.parametrize("value", [-1, "unknown"])
def test_invalid_workshop_ledger_preserves_original_bytes(tmp_path, field, value):
    import json
    data = _world_with_seed(42).save.to_dict(); data["trading_ledger"][field] = value
    path = tmp_path / "77.json"; path.write_text(json.dumps(data), encoding="utf-8"); before = path.read_bytes()
    with pytest.raises(vr.ResumeError): vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == before


@pytest.mark.parametrize("commands", [b"YS", b"YS1", b"YS1BBQQ", b"YS1RBBBQQ"])
def test_real_workshop_browsing_and_routes_preserve_career_bytes(tmp_path, commands):
    import json,os,subprocess
    world = _world_with_seed(42)
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    before = (tmp_path / "77.json").read_bytes()
    info = tmp_path / "door_info.json"; info.write_text(json.dumps({"user_id": 77, "handle": "Tester", "terminal_width": 40, "terminal_height": 12}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=commands, capture_output=True, timeout=10,
        env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)))
    assert result.returncode == 0 and not result.stderr and b"Specialist Workshops" in result.stdout
    assert (tmp_path / "77.json").read_bytes() == before


def test_public_workshop_routes_allow_only_the_listed_bearings_without_charting():
    import copy
    world = _world_with_seed(42); sites = set(vr.specialist_stations(world).values())
    for sid in sites:
        world.by_id[sid].discovered = False
        before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
        assert vr.prepare_route_jump(world, sid) == vr.bfs_path(world.by_id, 0, sid)[0]
        assert not world.by_id[sid].discovered
        assert world.save.to_dict() == before and world.event_rng.getstate() == rng
    other = next(sid for sid in world.by_id if sid and sid not in sites)
    world.by_id[other].discovered = False
    with pytest.raises(vr.MissionError, match="charted"): vr.prepare_route_jump(world, other)
    world.save.ship.fuel = 0
    with pytest.raises(vr.MissionError, match="fuel"): vr.prepare_route_jump(world, next(iter(sites)))


@pytest.mark.parametrize("standing", [-100, 0, 97, 98, 99, 100])
def test_distress_terms_disclose_actual_capped_standing_gain(monkeypatch, standing):
    world = _world_with_seed(42); world.save.pilot.reputation[vr.FACTION_CONCORD] = standing
    rng = world.event_rng.getstate()
    terms = " ".join(vr.distress_terms(world))
    gain = min(3, 100 - standing)
    assert f"Concord standing +{gain}" in terms
    assert world.event_rng.getstate() == rng
    monkeypatch.setattr(world.event_rng, "randint", lambda low, high: low)
    monkeypatch.setattr(vr, "read_key", lambda: "H")
    with contextlib.redirect_stdout(io.StringIO()): vr._encounter_distress_call(vr.Palette(False), world)
    assert world.save.pilot.reputation[vr.FACTION_CONCORD] - standing == gain


@pytest.mark.parametrize("module", list(vr.WORKSHOPS))
def test_station_portrait_uses_actual_economy_bearing_and_workshop(module):
    world=_world_with_seed(42); world.save.current_system=vr.specialist_stations(world)[module]
    large,compact,details,title=vr.viewport_content(world,"2"); here=world.here
    assert large==vr.PORTRAITS["port"][here.economy]["large"] and title==here.economy
    text=" ".join(details)
    for value in (here.station_name,here.name,vr.sector_for(here),vr.WORKSHOPS[module]["owner"],vr.WORKSHOPS[module]["name"]):assert value in text
    assert f"({here.x},{here.y})" in text and f"danger {here.danger}/5" in text


def test_the_concord_standing_a_contract_pays_is_visible_before_and_after():
    """A new progression route the player cannot see is not a route (#407)."""
    world = _world_with_seed(42)
    dest = sorted(world.here.connections)[0]
    mission = vr.Mission(1, "delivery", "Deliver", 300, 0, dest, commodity="food", quantity=2)
    world.save.active_missions = [mission]
    terms = " ".join(vr.mission_details(world, mission))
    assert f"+{vr.CONCORD_STANDING_PER_CONTRACT} Concord standing" in terms
    bounty = vr.Mission(2, "bounty", "Intercept", 500, 0, dest, pirate_tier=1)
    assert "Completion also pays" not in " ".join(vr.mission_details(world, bounty))  # the kill pays that, not the contract
    contact = " ".join(vr.faction_contact_lines(world, vr.FACTION_CONCORD))
    assert f"+{vr.CONCORD_STANDING_PER_CONTRACT} for every delivery, survey or escort contract" in contact
    assert f"{vr.CONTRABAND_STANDING_STEP}cr of net contraband trading gain" in " ".join(
        vr.faction_contact_lines(world, vr.FACTION_BLACKWAKE))
    _set_cargo(world, {"food": 2}); world.save.current_system = dest
    assert "Concord standing" in " ".join(vr.check_mission_completions(world))


def test_contraband_milestones_follow_the_shorter_step_without_recycling():
    world = _world_with_seed(42)
    vr.record_contraband_trade(world, "weapons", 249)
    assert world.save.pilot.reputation.get("blackwake", 0) == 0
    vr.record_contraband_trade(world, "weapons", 1)
    assert world.save.pilot.reputation["blackwake"] == 1
    vr.record_contraband_trade(world, "weapons", -250); vr.record_contraband_trade(world, "weapons", 250)
    assert world.save.pilot.reputation["blackwake"] == 1  # a recovered loss mints nothing
    vr.record_contraband_trade(world, "weapons", 250)
    assert world.save.pilot.reputation["blackwake"] == 2


def test_delivery_ceiling_is_ten_for_a_shuttle_and_scales_with_larger_holds():
    world = _world_with_seed(42)
    assert vr.delivery_contract_ceiling(world.save.ship) == 10
    world.save.ship.hull_class = "Carrier"; world.save.ship.cargo_tier = vr.UPGRADES["cargo"]["max_tier"]
    capacity = vr.cargo_capacity(world.save.ship)
    assert vr.delivery_contract_ceiling(world.save.ship) == capacity * 2 // 5 > 10


def test_shuttle_boards_are_unchanged_and_carrier_boards_post_bulk_deliveries():
    import random
    world = _world_with_seed(42)
    hops = vr.bfs_hops(world.by_id, world.here.id)
    before = [vr._generate_mission(world, "delivery", hops, rng=random.Random(seed)) for seed in range(40)]
    assert all(3 <= m.quantity <= 10 for m in before if m)
    world.save.ship.hull_class = "Carrier"; world.save.ship.cargo_tier = vr.UPGRADES["cargo"]["max_tier"]
    after = [vr._generate_mission(world, "delivery", hops, rng=random.Random(seed)) for seed in range(40)]
    ceiling = vr.delivery_contract_ceiling(world.save.ship)
    assert all(3 <= m.quantity <= ceiling for m in after if m) and any(m.quantity > 10 for m in after if m)
    assert [m.target_system for m in before if m] == [m.target_system for m in after if m]  # same draw sequence
    for small, big in zip(before, after):
        if small and big and big.quantity > small.quantity: assert big.reward > small.reward


def test_faction_and_workshop_blockers_name_what_is_missing():
    world = _world_with_seed(42)
    concord = vr.faction_join_blocker(world, vr.FACTION_CONCORD)
    assert isinstance(concord, str) and str(vr.FACTION_MEMBERSHIPS[vr.FACTION_CONCORD]["threshold"]) in concord
    world.save.pilot.reputation[vr.FACTION_CONCORD] = 100
    assert vr.faction_join_blocker(world, vr.FACTION_CONCORD) is None
    key = next(iter(vr.WORKSHOPS))
    quote = vr.workshop_quote(world, key)
    world.save.current_system = next(s.id for s in world.galaxy if s.id != quote["station"])
    assert vr.workshop_blocker(world, key) == "Visit this workshop before installing a module."
    world.save.current_system = quote["station"]
    world.save.pilot.credits = quote["credits"] - 1
    assert str(quote["credits"]) in vr.workshop_blocker(world, key).replace(",", "")
    world.save.pilot.credits = quote["credits"]
    _set_cargo(world, {})
    assert vr.COMMODITIES[quote["commodity"]]["label"] in vr.workshop_blocker(world, key)
    _set_cargo(world, {quote["commodity"]: quote["quantity"]})
    assert vr.workshop_blocker(world, key) is None  # every requirement met
    world.save.pending_travel = {"phase": "arrival"}
    assert vr.workshop_blocker(world, key) == "Finish the current journey first."
    world.save.pending_travel = None
    with pytest.raises(ValueError):
        vr.workshop_quote(world, "no-such-workshop")


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
def test_the_workshop_screen_fits_and_can_be_left(monkeypatch, terminal, width, height):
    """`screen_workshop` takes a workshop key, so it is not in the generic table (#423)."""
    terminal(width, height)
    world = _world_with_seed(42)
    world.checkpoint()
    frames, output = [], io.StringIO()
    def choose():
        frames.append(vr._ANSI_RE.sub("", output.getvalue())); output.seek(0); output.truncate(0)
        assert len(frames) < 60
        return "B"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output):
        vr.screen_workshop(vr.Palette(False), world, next(iter(vr.WORKSHOPS)))
    assert frames
    for frame in frames:
        assert all(vr._visible_width(row) <= width for row in frame.splitlines())
        assert len(frame.splitlines()) <= height
        assert "[B]" in frame or "[Q]" in frame


def test_the_hop_report_names_what_the_jump_cost(monkeypatch):
    """An ordinary staffed hop reported only "Jumping to ..." (issue #410 review)."""
    world = _world_with_seed(42)
    world.save.ship.has_gunner = True
    world.save.pilot.credits = 10_000
    destination = sorted(world.here.connections)[0]
    burn = vr.fuel_cost_for_jump(world.here, world.by_id[destination], world.save.ship)
    monkeypatch.setattr(vr, "_resolve_random_travel_encounter", lambda p, w, d: None)
    monkeypatch.setattr(vr, "read_key", lambda: "B")
    monkeypatch.setattr(vr, "pause", lambda p, msg=None: None)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, destination)
    report = " ".join(world.hop_report)
    assert f"{burn} fuel unit" in report and "on hand" in report
    assert "crew wages" in report  # a gunner was paid, and the report says so
    world.save.ship.has_gunner = False
    world.hop_report = []
    back = sorted(world.here.connections)[0]
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, back)
    assert "crew wages" not in " ".join(world.hop_report)  # nothing to report with no crew
