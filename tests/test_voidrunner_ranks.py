"""What a rank brings besides its title (review, v1.3).

A rank was a title, a scene and a chat line. Each now brings one thing, all of
them about contracts: a fourth and a fifth slot, and a premium on every
contract's reward. These pin the ladder, that one payout rule carries the
premium to every screen, that a retained rank keeps its perk when the credits
are spent, and that a promotion says what it brought.
"""

from __future__ import annotations

import pytest

from .support import _post_and_accept_test_mission, _world_with_seed, plain, vr


def _ranked(index: int):
    world = _world_with_seed(42)
    world.save.pilot.credits = vr.RANKS[index][0]
    vr.check_rank_up(world)
    assert vr.career_rank_index(world.save.pilot) == index
    return world


def test_every_rank_above_the_first_brings_one_thing():
    assert len(vr.RANK_CONTRACT_SLOTS) == len(vr.RANK_CONTRACT_BONUS) == len(vr.RANK_PERKS) == len(vr.RANKS)
    assert vr.RANK_PERKS[0] == "" and all(vr.RANK_PERKS[1:])
    assert list(vr.RANK_CONTRACT_SLOTS) == sorted(vr.RANK_CONTRACT_SLOTS) and vr.RANK_CONTRACT_SLOTS[0] == vr.MAX_ACTIVE_MISSIONS
    assert list(vr.RANK_CONTRACT_BONUS) == sorted(vr.RANK_CONTRACT_BONUS) and vr.RANK_CONTRACT_BONUS[0] == 0
    # Each promotion changes exactly one of the two.
    for index in range(1, len(vr.RANKS)):
        changed = ((vr.RANK_CONTRACT_SLOTS[index] != vr.RANK_CONTRACT_SLOTS[index - 1])
                   + (vr.RANK_CONTRACT_BONUS[index] != vr.RANK_CONTRACT_BONUS[index - 1]))
        assert changed == 1, vr.RANKS[index][1]


@pytest.mark.parametrize("index,slots", [(0, 3), (1, 4), (2, 4), (3, 5), (4, 5)])
def test_a_rank_decides_how_many_contracts_a_pilot_may_hold(index, slots):
    world = _ranked(index)
    assert vr.contract_slots(world.save) == slots
    target = world.here.connections[0]
    world.save.active_missions = [vr.Mission(number + 1, "bounty", "Existing", 500, 0, target, pirate_tier=1)
                                  for number in range(slots - 1)]
    offer = vr.Mission(90, "bounty", "One more", 500, world.save.current_system, target, pirate_tier=1)
    _post_and_accept_test_mission(world, offer)
    assert len(world.save.active_missions) == slots
    with pytest.raises(vr.MissionError, match=f"at most {slots} active contracts"):
        _post_and_accept_test_mission(
            world, vr.Mission(91, "bounty", "Too many", 500, world.save.current_system, target, pirate_tier=1))


@pytest.mark.parametrize("index,percent", [(0, 0), (1, 0), (2, 50), (3, 50), (4, 100)])
def test_one_payout_rule_carries_the_premium_to_every_kind_of_contract(index, percent):
    world = _ranked(index)
    for kind in ("delivery", "scan", "bounty", "escort"):
        assert vr.mission_reward_for(world.save, kind, 1000) == 1000 + 10 * percent
    # On top of the Concord commission, for the two kinds that carry it.
    world.save.pilot.has_concord_commission = True
    world.save.pilot.reputation[vr.FACTION_CONCORD] = 100
    assert vr.mission_reward_for(world.save, "bounty", 1000) == 1250 * (100 + percent) // 100
    assert vr.mission_reward_for(world.save, "delivery", 1000) == 1000 + 10 * percent


def test_a_retained_rank_keeps_its_perk_when_the_credits_are_spent():
    world = _ranked(3)
    world.save.pilot.credits = 10
    assert vr.contract_slots(world.save) == 5 and vr.mission_reward_for(world.save, "delivery", 1000) == 1500


def test_a_promotion_says_what_it_brought(terminal):
    terminal(80, 24)
    assert vr.rank_perk_line("Independent Trader") == "Independent Trader brings a fourth contract slot."
    assert vr.rank_perk_line("Merchant Captain") == "Merchant Captain brings half as much again on every contract."
    assert vr.rank_perk_line("Rookie Hauler") == ""
    rows = [" ".join(plain(row).split()) for row in vr.pilot_record_lines(_world_with_seed(42))]
    assert "it brings a fourth contract slot" in rows
    assert not any(row.startswith("it brings") for row in
                   (" ".join(plain(row).split()) for row in vr.pilot_record_lines(_ranked(4))))


@pytest.mark.parametrize("kind", ["delivery", "scan"])
def test_a_delivery_and_a_survey_pay_what_their_screens_quoted(kind):
    """They paid the bare reward while every screen quoted the payout rule's
    figure; the two were the same number only until a rank brought a premium."""
    world = _ranked(2)
    target = world.here.connections[0]
    if kind == "delivery":
        mission = vr.Mission(7, "delivery", "Deliver food", 1000, 0, target, commodity="food", quantity=3)
        world.save.cargo = {"food": 3}
        world.save.cargo_basis = {"food": [[3, 30]]}
        world.save.trading_ledger.since_day = 0
    else:
        mission = vr.Mission(7, "scan", "Survey", 1000, 0, target)
    world.save.active_missions = [mission]
    quoted = vr.mission_reward_for(world.save, kind, mission.reward)
    assert quoted == 1500
    before = world.save.pilot.credits
    world.save.current_system = target
    world.by_id[target].discovered = True
    messages = vr.check_mission_completions(world, just_discovered=target)
    assert world.save.pilot.credits == before + quoted and f"+{quoted}cr" in " ".join(messages)
    if kind == "delivery":
        assert world.save.trading_ledger.delivery_revenue == quoted
    # A scanner survey pays half of the same figure.
    world = _ranked(4)
    world.save.active_missions = [vr.Mission(8, "scan", "Survey", 1000, 0, target)]
    before = world.save.pilot.credits
    vr.check_mission_completions(world, just_discovered=target, remote=True)
    assert world.save.pilot.credits == before + 1000


# --- what a contract pays (doubled once whole careers were flown) ---------------------

def test_every_offer_on_a_board_is_priced_by_the_named_figures():
    seen = set()
    for seed in range(40, 52):
        world = _world_with_seed(seed)
        hops = vr.bfs_hops(world.by_id, world.here.id)
        for mission in vr.generate_mission_board(world):
            away = hops[mission.target_system]
            if mission.kind == "scan":
                assert mission.reward == vr.SURVEY_PAY[0] + away * vr.SURVEY_PAY[1]
            elif mission.kind == "bounty":
                assert mission.reward == vr.BOUNTY_PAY[0] + mission.pirate_tier * vr.BOUNTY_PAY[1]
            elif mission.kind == "escort":
                assert mission.reward == (vr.ESCORT_PAY[0] + away * vr.ESCORT_PAY[1]
                                          + mission.pirate_tier * vr.ESCORT_PAY[2])
            else:
                value = mission.quantity * vr.COMMODITIES[mission.commodity]["base"]
                at_origin, per_jump, flat = vr.DELIVERY_PAY
                assert mission.reward == round(value * (at_origin + per_jump * away)) + flat
                assert mission.reward > value  # carrying the goods there is never a loss at their base price
            seen.add(mission.kind)
    assert seen == {"delivery", "scan", "bounty", "escort"}


def test_the_figures_are_twice_what_a_contract_paid():
    assert vr.SURVEY_PAY == (400, 160) and vr.BOUNTY_PAY == (600, 500) and vr.ESCORT_PAY == (500, 300, 300)
    assert vr.DELIVERY_PAY == (1.0, 0.3, 100)
    assert vr.RANK_CONTRACT_BONUS == (0, 0, 50, 50, 100)
