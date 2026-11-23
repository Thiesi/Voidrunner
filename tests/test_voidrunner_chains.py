"""A delivery's client may have more work at the far end (review, v1.3).

Every contract was a single errand. These pin the chain: which deliveries start
one, that the next leg is posted on the board of the station the last was
handed over at, what a leg carries, how far and for how much, that the board's
ordinary offers are untouched by it, that three legs end it with an
achievement, and that a leg is an ordinary contract in a save older builds load.
"""

from __future__ import annotations

import pytest

from .support import _world_with_seed, vr


def _deliver(world, mission):
    """Hand `mission` over at its target, through the door's own completion and station tick."""
    world.save.active_missions = [mission]
    vr._acquire_cargo(world, mission.commodity, mission.quantity, 10 * mission.quantity)
    world.save.current_system = mission.target_system
    world.by_id[mission.target_system].discovered = True
    messages = vr.check_mission_completions(world)
    world.advance_station_state()
    return messages


def _first_leg(world, number: int, target: int | None = None):
    target = sorted(world.here.connections)[0] if target is None else target
    return vr.Mission(id=number, kind="delivery", description=f"Deliver 5x Food to {world.by_id[target].name}",
                      reward=500, origin_system=world.here.id, target_system=target, commodity="food", quantity=5,
                      deadline_turn=world.save.turn + 20)


def _starting(world) -> int:
    """A contract number whose delivery starts a chain in this career."""
    return next(n for n in range(1, 200) if vr.zlib.crc32(f"chain:{world.save.seed}:{n}".encode()) % vr.CHAIN_ONE_IN == 0)


def _legs(world):
    return [m for m in vr.posted_mission_offers(world) if vr.chain_leg(m) > 1]


def test_one_delivery_in_three_starts_a_chain_and_nothing_else_does():
    world = _world_with_seed(42)
    starts = [vr.chain_continues(world, _first_leg(world, number)) for number in range(1, 301)]
    assert 70 <= sum(starts) <= 130
    survey = vr.Mission(id=_starting(world), kind="scan", description="Survey", reward=300, origin_system=0, target_system=3)
    assert not vr.chain_continues(world, survey)
    first_flight = _first_leg(world, _starting(world))
    first_flight.opening_assignment = True
    assert not vr.chain_continues(world, first_flight)


def test_the_next_leg_is_on_the_board_where_the_last_was_handed_over():
    world = _world_with_seed(42)
    mission = _first_leg(world, _starting(world))
    messages = _deliver(world, mission)
    assert "The client has more work: a second leg is on this station's board." in messages
    legs = _legs(world)
    assert len(legs) == 1 and vr.chain_leg(legs[0]) == 2
    leg = legs[0]
    assert leg.origin_system == world.here.id == mission.target_system
    assert leg.description.startswith("Second leg: deliver ") and world.by_id[leg.target_system].name in leg.description
    # Posted once: the promise is spent, and another docking does not post it again.
    assert not any(key.startswith("chain_") for key in world.save.flags)
    world.advance_station_state()
    assert len(_legs(world)) == 1
    # A delivery that starts nothing posts nothing.
    quiet = _world_with_seed(42)
    plain_number = next(n for n in range(1, 200) if not vr.chain_continues(quiet, _first_leg(quiet, n)))
    assert not any("more work" in line for line in _deliver(quiet, _first_leg(quiet, plain_number)))
    assert _legs(quiet) == []


@pytest.mark.parametrize("seed", range(40, 52))
def test_a_leg_goes_a_jump_or_two_on_and_pays_a_premium_for_its_size(seed):
    world = _world_with_seed(seed)
    for leg in (2, 3):
        offer = vr.chain_offer(world, world.here.id, leg, after=7)
        away = vr.bfs_hops(world.by_id, world.here.id)[offer.target_system]
        assert 1 <= away <= 2 and offer.kind == "delivery" and offer.origin_system == world.here.id
        made = [c for c in vr.LEGAL_COMMODITIES if c in vr.ECONOMY_PRODUCES[world.here.economy]]
        assert offer.commodity in (made or vr.LEGAL_COMMODITIES)  # something this station makes, where it makes anything
        ceiling = vr.delivery_contract_ceiling(world.save.ship)
        assert max(3, ceiling // 2) <= offer.quantity <= ceiling
        at_origin, per_jump, flat = vr.DELIVERY_PAY
        ordinary = offer.quantity * vr.COMMODITIES[offer.commodity]["base"] * (at_origin + per_jump * away) + flat
        assert offer.reward == round(ordinary * vr.CHAIN_PREMIUM[leg])
        assert offer.deadline_turn == world.save.turn + 10 + 4 * away
        assert offer == vr.chain_offer(world, world.here.id, leg, after=7)  # the same leg every time it is asked for
    assert vr.CHAIN_PREMIUM == {2: 1.25, 3: 1.5}


def test_three_legs_end_the_chain_with_an_achievement():
    world = _world_with_seed(42)
    _deliver(world, _first_leg(world, _starting(world)))
    second = _legs(world)[0]
    vr.accept_mission(world, second)
    messages = _deliver(world, second)
    assert "The client has more work: a final leg is on this station's board." in messages
    final = _legs(world)[0]
    assert vr.chain_leg(final) == 3 and final.description.startswith("Final leg: deliver ")
    assert "chain" not in {accolade.id for accolade in vr.unlock_accolades(world.save)}
    vr.accept_mission(world, final)
    messages = _deliver(world, final)
    assert "All three legs carried: the client will remember this ship." in messages
    assert not any("more work" in line for line in messages) and _legs(world) == []
    assert "chain" in {accolade.id for accolade in vr.unlock_accolades(world.save)}


def test_the_boards_ordinary_offers_are_what_they_would_have_been():
    with_chain, without = _world_with_seed(42), _world_with_seed(42)
    target = sorted(with_chain.here.connections)[0]
    _deliver(with_chain, _first_leg(with_chain, _starting(with_chain), target))
    quiet = next(n for n in range(1, 200) if not vr.chain_continues(without, _first_leg(without, n)))
    _deliver(without, _first_leg(without, quiet, target))
    ordinary = [(m.kind, m.description, m.reward, m.target_system) for m in vr.posted_mission_offers(with_chain) if vr.chain_leg(m) == 1]
    plain = [(m.kind, m.description, m.reward, m.target_system) for m in vr.posted_mission_offers(without)]
    # The leg takes a full board's last ordinary place and otherwise only adds to it.
    assert ordinary == plain[:len(ordinary)] and len(plain) - len(ordinary) in (0, 1)
    assert len(vr.posted_mission_offers(with_chain)) <= 4


def test_a_leg_is_an_ordinary_contract_in_a_save_that_still_loads():
    world = _world_with_seed(42)
    _deliver(world, _first_leg(world, _starting(world)))
    leg = _legs(world)[0]
    assert set(leg.to_dict()) <= {field.name for field in vr.dataclasses.fields(vr.Mission)}
    again = vr.World(vr.SaveData.from_dict(world.save.to_dict()))
    again.save.current_system = world.save.current_system
    assert [m.description for m in _legs(again)] == [leg.description]
    # It wants a slot like any offer, and can be left to lapse with the board.
    world.save.turn += vr.MISSION_BOARD_DAYS
    assert _legs(world) == []
