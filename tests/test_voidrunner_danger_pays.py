"""Danger pays (review).

A system's danger was a cost and nothing else, so the right way to play was to
stay out of it. These pin what a dangerous station now pays: more for whatever
it does not make itself, on both sides of the counter; hazard pay on a
contract that ends there; nothing at all in calm space; and that both are said
where the decision is made.
"""

from __future__ import annotations

import pytest

from .support import _world_with_seed, plain, vr


def _system(world, danger, *, not_danger=False):
    return next(s for s in world.galaxy if (s.danger == danger) != not_danger and s.id)


def _with_danger(world, danger):
    """Some station, made as dangerous as asked."""
    system = next(s for s in world.galaxy if s.id and s.economy != "Haven")
    system.danger = danger
    return system


def test_calm_space_is_exactly_what_it_was():
    world = _world_with_seed(42)
    assert set(vr.HAZARD_PRICE) == set(vr.HAZARD_PAY) == {4, 5}
    for danger in range(4):
        system = _with_danger(world, danger)
        assert all(vr.hazard_price_factor(system, c) == 1.0 for c in vr.COMMODITIES)
        assert vr.hazard_pay(world, system.id, 1_000) == 1_000
        assert not any(words == "dangerous space" for c in vr.COMMODITIES for words, _f in vr.price_reasons(world, system.id, c))


@pytest.mark.parametrize("danger", [4, 5])
def test_a_dangerous_station_asks_and_pays_more_for_what_it_does_not_make(danger):
    world = _world_with_seed(42)
    system = _with_danger(world, 1)
    calm = {c: (vr.price_for(world, system.id, c), vr.sell_price_for(world, system.id, c)) for c in vr.COMMODITIES}
    system.danger = danger
    factor = vr.HAZARD_PRICE[danger]
    made = [c for c in vr.COMMODITIES if vr.economy_price_factor(system.economy, c) < 1.0]
    assert made and len(made) < len(vr.COMMODITIES)
    for commodity in vr.COMMODITIES:
        buy, sell = vr.price_for(world, system.id, commodity), vr.sell_price_for(world, system.id, commodity)
        if commodity in made:
            assert (buy, sell) == calm[commodity]  # what it makes, it sells at what it always did
        else:
            assert abs(buy - calm[commodity][0] * factor) <= 1 and abs(sell - calm[commodity][1] * factor) <= 1
            assert sell < buy  # both sides move: nothing to be had by selling back across the same counter
            assert ("dangerous space", factor) in vr.price_reasons(world, system.id, commodity)
    assert vr.HAZARD_PRICE[5] > vr.HAZARD_PRICE[4] > 1


@pytest.mark.parametrize("danger,percent", [(4, 25), (5, 50)])
def test_a_contract_that_ends_in_dangerous_space_carries_hazard_pay(danger, percent):
    world = _world_with_seed(42)
    system = _with_danger(world, danger)
    assert vr.HAZARD_PAY[danger] == percent
    assert vr.hazard_pay(world, system.id, 1_000) == 1_000 + 10 * percent
    assert vr.hazard_pay(world, system.id, 333) == 333 * (100 + percent) // 100


def test_every_offer_a_board_posts_into_dangerous_space_is_paid_for_it():
    paid = 0
    for seed in range(40, 70):
        world = _world_with_seed(seed)
        hops = vr.bfs_hops(world.by_id, world.here.id)
        for system in world.galaxy:
            if system.id and hops.get(system.id, 9) <= 3:
                system.danger = 5  # every destination nearby is as bad as it gets
        for mission in vr.generate_mission_board(world):
            if world.by_id[mission.target_system].danger != 5 or mission.kind != "scan":
                continue
            base = vr.SURVEY_PAY[0] + hops[mission.target_system] * vr.SURVEY_PAY[1]
            edge = vr.sector_for(world.by_id[mission.target_system]) == "Farrider's Edge"  # where a survey pays double
            assert mission.reward == base * 150 // 100 * (vr.EDGE_SURVEY_PAY if edge else 1)
            paid += 1
    assert paid >= 3


def test_the_sheet_says_what_the_hazard_is_worth_and_calm_work_says_nothing():
    world = _world_with_seed(42)
    target = sorted(world.here.connections)[0]
    world.by_id[target].discovered = True
    world.by_id[target].danger = 5
    mission = vr.Mission(id=4, kind="scan", description="Survey", reward=900, origin_system=0, target_system=target)
    sheet = " ".join(" ".join(plain(line) for line in vr.mission_details(world, mission)).split())
    assert "HAZARD +50% hazard pay, in the payout" in sheet
    world.by_id[target].danger = 2
    assert "HAZARD" not in " ".join(plain(line) for line in vr.mission_details(world, mission))


def test_the_prices_own_reasons_still_multiply_to_the_quote_in_dangerous_space():
    import math
    world = _world_with_seed(42)
    system = _with_danger(world, 5)
    world.save.turn = 7
    for commodity in vr.COMMODITIES:
        product = math.prod(factor for _w, factor in vr.price_reasons(world, system.id, commodity))
        assert abs(vr.COMMODITIES[commodity]["base"] * product - vr.price_for(world, system.id, commodity)) <= 1.0
