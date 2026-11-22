"""Prices have seasons, and a futures order says what it bets on (review, v1.3).

Nothing in a price moved that a pilot could see coming, so a futures order was
never worth its fee. These pin the season: how far and how slowly it swings,
that a first day is untouched, that a sector moves together, that the market
and the order say truly where a price is going, that a lock at the bottom is
worth having and one at the top is not, and that nothing is saved for it.
"""

from __future__ import annotations

import pytest

from .support import _world_with_seed, plain, vr


def _factors(world, system_id, commodity, days=range(0, 200)):
    return [vr.season_factor(world, system_id, commodity, day) for day in days]


@pytest.mark.parametrize("seed", [40, 41, 42, 43])
def test_a_season_swings_an_eighth_either_way_over_a_month_or_so(seed):
    world = _world_with_seed(seed)
    for commodity in vr.LEGAL_COMMODITIES:
        swing = _factors(world, 0, commodity)
        assert swing[0] == 1.0  # day zero is level: a first day is what it was
        assert 1 - vr.SEASON_SWING - 1e-9 <= min(swing) < 0.9 and 1.1 < max(swing) <= 1 + vr.SEASON_SWING + 1e-9
        period, sign = vr._season_of(seed, vr.sector_for(world.here), commodity)
        assert vr.SEASON_PERIODS[0] <= period <= vr.SEASON_PERIODS[1] and sign in (1, -1)
        assert swing[period] == pytest.approx(1.0, abs=1e-9)  # and it comes round again
        assert max(abs(b - a) for a, b in zip(swing, swing[1:])) < 0.04  # slowly: under 4% a day


def test_goods_and_sectors_keep_their_own_time_and_a_sector_moves_together():
    world = _world_with_seed(42)
    here = world.here
    same = next(s for s in world.galaxy if s.id != here.id and vr.sector_for(s) == vr.sector_for(here))
    for commodity in vr.LEGAL_COMMODITIES:
        assert _factors(world, here.id, commodity, range(60)) == _factors(world, same.id, commodity, range(60))
    seasons = {vr._season_of(42, sector, commodity) for sector in vr.SECTOR_NAMES for commodity in vr.LEGAL_COMMODITIES}
    assert len(seasons) >= 12  # not one clock for the whole galaxy


def test_contraband_has_no_season_and_the_swing_can_be_switched_off(monkeypatch):
    world = _world_with_seed(42)
    world.save.turn = 9
    for commodity in vr.COMMODITIES:
        if not vr.COMMODITIES[commodity]["legal"]:
            assert vr.season_factor(world, 0, commodity) == 1.0 and vr.season_trend(world, 0, commodity) is None
    monkeypatch.setattr(vr, "SEASON_SWING", 0)
    assert all(vr.season_factor(world, 0, c) == 1.0 and vr.season_trend(world, 0, c) is None for c in vr.COMMODITIES)


def test_a_price_follows_its_season_and_a_first_day_is_untouched(monkeypatch):
    world = _world_with_seed(42)
    level = {c: vr.price_for(world, 0, c) for c in vr.LEGAL_COMMODITIES}
    monkeypatch.setattr(vr, "SEASON_SWING", 0)
    assert level == {c: vr.price_for(world, 0, c) for c in vr.LEGAL_COMMODITIES}
    monkeypatch.undo()
    moved = 0
    for day in range(1, 41):
        world.save.turn = day
        for commodity, base in level.items():
            price = vr.price_for(world, 0, commodity)
            assert price == max(1, round(base * vr.season_factor(world, 0, commodity))) or abs(price - base * vr.season_factor(world, 0, commodity)) <= 1
            assert vr.sell_price_for(world, 0, commodity) <= price
            moved += price != base
    assert moved > 100


def test_the_trend_says_truly_which_way_and_for_how_long():
    world = _world_with_seed(42)
    for day in range(0, 90, 3):
        world.save.turn = day
        for commodity in vr.LEGAL_COMMODITIES:
            rising, days = vr.season_trend(world, 0, commodity)
            assert 1 <= days <= vr.SEASON_PERIODS[1] // 2 + 1
            swing = _factors(world, 0, commodity, range(day, day + days + 2))
            steps = [b > a for a, b in zip(swing, swing[1:])]
            assert all(step == rising for step in steps[:days]) and steps[days] != rising


def test_the_market_marks_every_seasonal_price_and_explains_the_mark():
    world = _world_with_seed(42)
    world.save.turn = 9
    rows = [plain(row) for row in vr.market_catalog_lines(world, list(vr.LEGAL_COMMODITIES))]
    up, down = vr.glyph("rise"), vr.glyph("fall")
    for commodity in vr.LEGAL_COMMODITIES:
        rising, days = vr.season_trend(world, 0, commodity)
        row = next(r for r in rows if r.startswith("[") and vr.COMMODITIES[commodity]["label"] in r)
        assert row.rstrip().endswith(f"{up if rising else down}{days}")
    note = next(r for r in rows if r.startswith("Prices per unit."))
    assert f"{up}{down} rising or falling for that many more days." in note


def test_a_forecast_is_todays_price_carried_along_the_season():
    world = _world_with_seed(42)
    world.save.turn = 9
    for commodity in vr.LEGAL_COMMODITIES:
        for days in vr.FUTURES_DURATIONS:
            forecast = vr.season_forecast(world, 0, commodity, days)
            later = _world_with_seed(42)
            later.save.turn = 9 + days
            assert abs(forecast - vr.price_for(later, 0, commodity)) <= 1  # what the market will in fact ask


def _order_lines(world, commodity, quantity, duration):
    return [plain(line) for line in vr.futures_season_lines(world, commodity, quantity, duration)]


def test_a_lock_at_the_bottom_is_worth_having_and_one_at_the_top_is_a_loss():
    world = _world_with_seed(42)
    commodity = "electronics"
    swing = _factors(world, 0, commodity, range(0, 81))
    bottom = min(range(5, 60), key=lambda day: swing[day])
    top = max(range(5, 60), key=lambda day: swing[day])
    world.save.turn = bottom + 1  # the day after the lowest: on its way up
    lines = _order_lines(world, commodity, 10, 10)
    assert "is rising for" in lines[0] and "this order should save about" in lines[1]
    saved = int(lines[1].split("save about ")[1].split("cr")[0])
    principal, fee = vr.futures_quote(world, commodity, 10)
    assert saved > 0 and saved == (vr.season_forecast(world, 0, commodity, 10) - -(-(principal + fee) // 10)) * 10
    world.save.turn = top + 1
    lines = _order_lines(world, commodity, 10, 10)
    assert "is falling for" in lines[0] and "more than buying then" in lines[1]
    # The best lock there is: at the bottom, for the term that reaches nearest the top.
    world.save.turn = bottom
    best = max(vr.season_forecast(world, 0, commodity, days) for days in vr.FUTURES_DURATIONS)
    price = vr.price_for(world, 0, commodity)
    assert 1.10 < best / (price * (1 + vr.FUTURES_FEE_PERCENT / 100)) < 1.25


def test_contraband_orders_say_nothing_about_a_season():
    world = _world_with_seed(42)
    illegal = next(c for c in vr.COMMODITIES if not vr.COMMODITIES[c]["legal"])
    assert vr.futures_season_lines(world, illegal, 5, 10) == []


def test_nothing_is_saved_for_a_season():
    world = _world_with_seed(42)
    before = world.save.to_dict()
    world.save.turn = 30
    for commodity in vr.LEGAL_COMMODITIES:
        vr.price_for(world, 0, commodity), vr.season_trend(world, 0, commodity), vr.season_forecast(world, 0, commodity, 20)
    after = world.save.to_dict()
    after["turn"] = before["turn"]
    assert after == before
