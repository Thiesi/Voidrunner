"""Tactical ruleset 4: a contact that may jump its pattern (review).

A contact's pattern, its next move and that move's exact damage were all on the
panel, so one rule solved every fight. From tier 2 a contact may now make the
move after the one it is set up for. These pin how surely each tier is read and
what a scanner adds, that the jump is rolled when the contact acts and never
when the panel is drawn, that a fight saved under an older ruleset keeps its
certainty and its pattern, that low tiers stay on the random sequence they
always had, that the panel quotes the jump it may make, and that the three
patterns are now about equally hard.
"""

from __future__ import annotations

import copy
import re

import pytest

from .support import _world_with_seed, plain, vr


def _tactics(profile: str = "Raider", step: int = 0, version: int = vr.TACTICAL_RULESET_VERSION) -> dict:
    return {"version": version, "profile": profile, "step": step, "brace_ready": True}


# --- the read -------------------------------------------------------------------

@pytest.mark.parametrize("tier,scanner,expected", [(0, 0, 1.0), (1, 0, 1.0), (2, 0, 0.8), (3, 0, 0.7), (4, 0, 0.6),
                                                   (2, 2, 1.0), (3, 1, 0.8), (3, 2, 0.9), (4, 2, 0.8)])
def test_low_tiers_are_read_perfectly_and_a_scanner_reads_a_tenth_more_a_tier(tier, scanner, expected):
    ship = _world_with_seed(42).save.ship
    ship.scanner_tier = scanner
    assert vr.tactical_read(ship, tier, _tactics()) == pytest.approx(expected)
    # A fight keeps the ruleset it started under, and those were certain.
    assert vr.tactical_read(ship, tier, _tactics(version=3)) == 1.0


def test_a_jump_is_the_move_after_the_one_shown_and_the_cycle_moves_past_it():
    world = _world_with_seed(42)
    pirate = vr.Pirate("Probe", 4, 400, 400)
    for step, shown, jump in ((0, "attack", "volley"), (1, "volley", "recover"), (2, "recover", "attack")):
        tactics = _tactics("Raider", step)
        assert (vr.tactical_intent(tactics), vr.tactical_jump(tactics)) == (shown, jump)
        world.event_rng.random = lambda: 0.0   # read
        assert vr._tactical_act(world, pirate, tactics) == (shown, False) and tactics["step"] == (step + 1) % 3
        tactics = _tactics("Raider", step)
        world.event_rng.random = lambda: 0.99  # jumped
        assert vr._tactical_act(world, pirate, tactics) == (jump, True) and tactics["step"] == (step + 2) % 3


def test_a_contact_jumps_about_as_often_as_the_read_says():
    world = _world_with_seed(42)
    world.event_rng.seed(7)
    pirate = vr.Pirate("Probe", 3, 400, 400)
    jumps = sum(vr._tactical_act(world, pirate, _tactics())[1] for _ in range(4000))
    assert 0.27 <= jumps / 4000 <= 0.33


def test_a_jumped_move_is_rushed_and_says_so():
    world = _world_with_seed(42)
    ship = world.save.ship
    full = vr._tactical_incoming_damage(ship, 3, "volley", 9, _tactics())
    rushed = vr._tactical_incoming_damage(ship, 3, "volley", 9, _tactics(), jumped=True)
    assert rushed < full and rushed == -(-(9 + 8) * 160 * vr.READ_JUMP_PERCENT // 10000)
    pirate = vr.Pirate("Probe", 3, 400, 400)
    world.event_rng.random = lambda: 0.99
    _, lines = vr.tactical_retaliation(world, pirate, _tactics("Raider", 0))
    assert lines == [f"The Probe jumps its pattern to volley and hits you for {60 - ship.hull_hp} damage."]


# --- when it is rolled ------------------------------------------------------------

def test_the_panel_draws_nothing_and_the_jump_is_rolled_when_the_contact_acts():
    world = _world_with_seed(42)
    world.event_rng.seed(3)
    pirate = vr.Pirate("Probe", 3, 400, 400)
    tactics = _tactics()
    state, saved = world.event_rng.getstate(), copy.deepcopy(world.save.to_dict())
    for details in (False, True):
        vr.combat_display_lines(world, pirate, [], patrol=False, tactics=tactics, details=details)
    assert world.event_rng.getstate() == state and world.save.to_dict() == saved and tactics == _tactics()
    # Two worlds on one saved RNG state make the same move: a resumed turn cannot reroll it.
    twin = _world_with_seed(42)
    twin.event_rng.setstate(state)
    first = vr.tactical_round(world, pirate, tactics, "F")
    second = vr.tactical_round(twin, vr.Pirate("Probe", 3, 400, 400), _tactics(), "F")
    assert first == second


@pytest.mark.parametrize("tier", [0, 1])
@pytest.mark.parametrize("profile", ["Raider", "Bulwark"])
def test_a_low_tier_fight_is_on_the_random_sequence_it_always_had(tier, profile):
    """Read perfectly, a contact draws nothing extra: the same seed fights the
    same fight under ruleset 3 and ruleset 4."""
    logs = []
    for version in (3, 4):
        world = _world_with_seed(42)
        world.event_rng.seed(11)
        pirate = vr.Pirate("Probe", tier, 400, 400)
        tactics = _tactics(profile, version=version)
        logs.append([vr.tactical_round(world, pirate, tactics, "F")[:2] for _ in range(6)])
    assert logs[0] == logs[1]


def test_a_fight_saved_under_ruleset_three_never_jumps_and_keeps_its_pattern():
    world = _world_with_seed(42)
    world.event_rng.random = lambda: 0.99  # would jump every time, if it could
    pirate = vr.Pirate("Probe", 4, 400, 400)
    tactics = _tactics("Skirmisher", version=3)
    assert vr.tactical_steps(tactics) == ("harry", "attack", "volley")
    moves = []
    for _ in range(3):
        moves.append(vr._tactical_act(world, pirate, tactics))
    assert moves == [("harry", False), ("attack", False), ("volley", False)]
    assert vr.tactical_steps(_tactics("Skirmisher")) == ("harry", "volley", "recover")


# --- the panel --------------------------------------------------------------------

@pytest.mark.parametrize("profile", list(vr.TACTICAL_PROFILES))
@pytest.mark.parametrize("step", [0, 1, 2])
def test_the_panel_quotes_the_jump_a_contact_may_make_and_a_round_resolves_it(profile, step):
    world = _world_with_seed(42)
    world.save.ship.shield_tier = 1
    pirate = vr.Pirate("Probe", 3, 400, 400)
    tactics = _tactics(profile, step)
    rows = [plain(row) for row in vr.combat_display_lines(world, pirate, [], patrol=False, tactics=tactics)]
    row = next(row for row in rows if " incoming " in row)
    found = re.search(r"incoming (\d+)(?:-(\d+))?, or ([A-Z]+) (\d+)(?:-(\d+))? \((\d+)%\)", row)
    assert found, row
    assert found[3] == vr.tactical_jump(tactics).upper() and found[6] == "30"
    low, high = int(found[4]), int(found[5] or found[4])
    for roll, quoted in ((4, low), (9, high)):
        fight = _world_with_seed(42)
        fight.save.ship.shield_tier = 1
        fight.event_rng.random = lambda: 0.99
        fight.event_rng.randint = lambda lo, hi, roll=roll: roll if (lo, hi) == (4, 9) else lo
        damage, _ = vr.tactical_retaliation(fight, vr.Pirate("Probe", 3, 400, 400), _tactics(profile, step))
        assert damage == quoted


def test_a_contact_read_perfectly_shows_no_other_move_and_info_says_which_it_is():
    world = _world_with_seed(42)
    for tier, scanner, certain in ((1, 0, True), (2, 2, True), (2, 0, False), (4, 2, False)):
        world.save.ship.scanner_tier = scanner
        pirate = vr.Pirate("Probe", tier, 400, 400)
        rows = [plain(row) for row in vr.combat_display_lines(world, pirate, [], patrol=False, tactics=_tactics(),
                                                              details=True)]
        intent = next(row for row in rows if " incoming " in row)
        assert (", or " not in intent) == certain
        assert any(row.startswith("Read 100%: this contact makes the move shown.") for row in rows) == certain
        assert any("may jump its pattern" in row and "Long-Range Scanner" in row for row in rows) != certain
    assert vr.UPGRADES["scanner"]["effect"] == "+scan range, +read"


# --- the balance ------------------------------------------------------------------

def _wins(profile: str, tier: int, fights: int = 300) -> int:
    """A starting Shuttle that Guards whenever a volley is the likelier move."""
    name = next(name for name in vr.PIRATE_NAMES if vr.tactical_profile(name) == profile)
    world = _world_with_seed(1)
    ship = world.save.ship
    wins = 0
    for seed in range(fights):
        world.event_rng.seed(seed)
        ship.hull_hp = vr.hull_hp_max(ship)
        pirate = vr.Pirate(name, tier, 20 + 15 * tier, 20 + 15 * tier)
        tactics = vr.new_tactics(pirate)
        while pirate.hp > 0 and ship.hull_hp > 0:
            read = vr.tactical_read(ship, tier, tactics)
            volley = ((read if vr.tactical_intent(tactics) == "volley" else 0.0)
                      + ((1 - read) if vr.tactical_jump(tactics) == "volley" else 0.0))
            vr.tactical_round(world, pirate, tactics, "G" if tactics["brace_ready"] and volley >= 0.5 else "F")
        wins += ship.hull_hp > 0
    return wins


def test_no_pattern_is_a_walkover_or_a_wall_for_a_starting_shuttle_at_tier_three():
    """Under ruleset 3 these were 99%, 94% and 2%: which pattern a raider flies
    is a hash of its name, so a tier-3 fight was decided before it began."""
    rates = {profile: _wins(profile, 3) / 300 for profile in vr.TACTICAL_PROFILES}
    assert all(0.35 <= rate <= 0.85 for rate in rates.values()), rates
    assert max(rates.values()) - min(rates.values()) <= 0.3, rates


def test_a_starting_shuttle_still_wins_its_tier_two_fights():
    for profile in vr.TACTICAL_PROFILES:
        assert _wins(profile, 2, 150) >= 142, profile
