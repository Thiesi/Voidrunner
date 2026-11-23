"""Prices have seasons, and a futures order says what it bets on (review).

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


def test_nothing_is_saved_for_a_season():
    world = _world_with_seed(42)
    before = world.save.to_dict()
    world.save.turn = 30
    for commodity in vr.LEGAL_COMMODITIES:
        vr.price_for(world, 0, commodity), vr.season_trend(world, 0, commodity)
    after = world.save.to_dict()
    after["turn"] = before["turn"]
    assert after == before


def test_the_mark_is_its_own_column_and_never_costs_the_spread_its_place():
    """Inside the SPREAD cell the mark widened that column, and on a day a
    contract's NEED was also showing the whole spread was dropped at 80 columns."""
    world = _world_with_seed(42)
    world.save.turn = 9
    goods = list(vr.LEGAL_COMMODITIES)
    free = [plain(row) for row in vr.market_catalog_lines(world, goods)]
    assert "SPREAD" in free[1] and free[1].rstrip().endswith("TREND")
    target = sorted(world.here.connections)[0]
    world.save.active_missions = [vr.Mission(id=4, kind="delivery", description="Deliver 3x Machinery", reward=300,
                                             origin_system=0, target_system=target, commodity="machinery", quantity=3)]
    needed = [plain(row) for row in vr.market_catalog_lines(world, goods)]
    heading = next(row for row in needed if row.startswith("COMMODITY"))
    assert "NEED" in heading and "SPREAD" in heading and "TREND" not in heading
    words = {"cheap", "typical", "dear"}
    assert all(row.split()[-1] in words for row in needed if row.startswith("["))  # every row keeps its word
    # The note explains the marks only where the table has room to show them.
    assert "rising or falling" not in next(row for row in needed if row.startswith("Prices per unit."))
    assert "rising or falling" in next(row for row in free if row.startswith("Prices per unit."))
