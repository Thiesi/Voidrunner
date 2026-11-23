"""Station stakes: something to own once everything is bought (review).

By day 45 of a trading career there is nothing left to buy. A stake is a share
in a station's yards, three steps at up to three stations, and what it buys is
a station that restocks faster for its holder. These pin the prices and the
limits, that only the pace of refill changes and never a price or a ceiling,
that nothing new is saved and an older build would still load the career, and
that the market says what is held.
"""

from __future__ import annotations

import contextlib
import io
import json

import pytest

from .support import _set_cargo, _world_with_seed, plain, vr


def _rich(credits: int = 1_000_000):
    world = _world_with_seed(42)
    world.save.pilot.credits = credits
    return world


def test_three_steps_at_three_stations_is_the_whole_ladder():
    world = _rich()
    assert vr.STAKE_COSTS == (15_000, 30_000, 60_000) and vr.STAKE_MAX_STATIONS == 3
    spent = 0
    stations = [0] + sorted(world.here.connections)[:1]
    stations.append(next(sid for sid in world.by_id if sid not in stations))
    for sid in stations:
        world.save.current_system = sid
        for step, cost in enumerate(vr.STAKE_COSTS, 1):
            before = world.save.pilot.credits
            message = vr.buy_stake(world)
            assert world.save.pilot.credits == before - cost and vr.stake_level(world.save, sid) == step
            assert f"step {step} of 3" in message and f"{step + 1} times as fast" in message
            spent += cost
        with pytest.raises(vr.TradeError, match="every step"):
            vr.buy_stake(world)
    assert spent == 315_000 and vr.stakes(world.save) == {sid: 3 for sid in sorted(stations)}
    # A fourth station is one too many.
    world.save.current_system = next(sid for sid in world.by_id if sid not in stations)
    with pytest.raises(vr.TradeError, match="3 stations"):
        vr.buy_stake(world)
    assert vr.accolade_metrics(world.save)["stake_steps"] == 9


def test_a_step_that_cannot_be_afforded_or_is_mid_journey_changes_nothing():
    world = _rich(14_999)
    before = json.dumps(world.save.to_dict(), sort_keys=True)
    with pytest.raises(vr.TradeError, match="15,000cr"):
        vr.buy_stake(world)
    assert json.dumps(world.save.to_dict(), sort_keys=True) == before and vr.stakes(world.save) == {}
    assert vr.stake_blocker(world) == "The next step costs 15,000cr."


@pytest.mark.parametrize("steps", [0, 1, 2, 3])
def test_a_stake_brings_stock_and_demand_back_faster_and_changes_nothing_else(steps):
    world = _rich()
    plain_quote = vr.market_depth_quote(world, 0, "machinery")
    prices = {c: (vr.price_for(world, 0, c), vr.sell_price_for(world, 0, c)) for c in vr.LEGAL_COMMODITIES}
    for _ in range(steps):
        vr.buy_stake(world)
    quote = vr.market_depth_quote(world, 0, "machinery")
    assert (quote["stock"], quote["demand"]) == (plain_quote["stock"], plain_quote["demand"])  # the station's ceilings
    assert quote["stock_rate"] == plain_quote["stock_rate"] * (steps + 1)
    assert quote["demand_rate"] == plain_quote["demand_rate"] * (steps + 1)
    assert prices == {c: (vr.price_for(world, 0, c), vr.sell_price_for(world, 0, c)) for c in vr.LEGAL_COMMODITIES}
    # Empty the pool, let two days pass: what comes back is the staked pace.
    vr.trade_cargo(world, "machinery", 24, buying=True)
    vr.trade_cargo(world, "machinery", 24, buying=False)
    _set_cargo(world, {})
    world.save.market_depth[0]["machinery"].update(stock=0, demand=0)
    world.save.turn += 2
    after = vr.market_depth_quote(world, 0, "machinery")
    assert after["stock"] == min(plain_quote["stock"], 2 * plain_quote["stock_rate"] * (steps + 1))
    assert after["demand"] == min(plain_quote["demand"], 2 * plain_quote["demand_rate"] * (steps + 1))
    # A station the pilot holds nothing at keeps its own pace.
    other = sorted(world.here.connections)[0]
    assert vr.market_depth_quote(world, other, "food")["stock_rate"] in (3, 6)


def test_nothing_new_is_saved_and_every_stored_figure_stays_inside_any_builds_limits():
    world = _rich()
    keys = set(world.save.to_dict())
    for _ in range(3):
        vr.buy_stake(world)
    vr.trade_cargo(world, "machinery", 10, buying=True)
    world.save.turn += 5
    vr.trade_cargo(world, "machinery", 10, buying=False)
    document = json.loads(json.dumps(world.save.to_dict()))
    assert set(document) == keys
    assert {name for name in document["flags"] if name.startswith("stake_")} == {"stake_0_1", "stake_0_2", "stake_0_3"}
    assert all(type(value) is bool for value in document["flags"].values())
    # The pools are within the ceilings a build without stakes would check.
    for commodity, pool in document["market_depth"]["0"].items():
        caps = vr.market_depth_limits("Industrial", commodity)
        assert 0 <= pool["stock"] <= caps["stock"] and 0 <= pool["demand"] <= caps["demand"]
    loaded = vr.SaveData.from_dict(document)
    assert vr.stakes(loaded) == {0: 3}


def test_stakes_belong_to_the_career_and_end_with_it():
    from .support import _finale_world
    world = _finale_world("legend")
    world.save.pilot.credits += 15_000
    vr.buy_stake(world)
    assert vr.stakes(world.save)
    assert vr.stakes(vr.finish_career(world.save, "legend")) == {}


def test_a_flag_that_only_looks_like_a_stake_is_not_one():
    world = _rich()
    world.save.flags.update({"stake_0_9": True, "stake_x_1": True, "stake_0_2": True, "stakeholder": True,
                             "stake_3_1": False})
    assert vr.stake_level(world.save, 0) == 1 and vr.stakes(world.save) == {0: 1}


@pytest.mark.parametrize("width,height", [(80, 24), (40, 12)])
def test_the_market_wears_the_stake_and_the_page_fits(terminal, width, height):
    terminal(width, height)
    world = _rich()
    goods = list(vr.LEGAL_COMMODITIES)
    assert "STAKE" not in plain(vr.market_catalog_lines(world, goods)[0])
    rows = [plain(row) for row in vr.stake_lines(world)]
    assert any(row.startswith("HELD") and "none" in row for row in rows)
    assert any(row.startswith("NEXT") and "step 1 for 15,000cr: 2 times as fast" in " ".join(row.split()) for row in rows)
    vr.buy_stake(world)
    vr.buy_stake(world)
    assert "STAKE 2/3" in plain(vr.market_catalog_lines(world, goods)[0])
    rows = [" ".join(plain(row).split()) for row in vr.stake_lines(world)]
    assert any("step 2 of 3" in row for row in rows) and any("come back 3 times as fast" in row for row in rows)
    assert any(row.startswith(world.here.station_name) and row.endswith("2/3 x3") for row in rows)
    for page in vr._service_pages(vr.stake_lines(world), "Station Stake: 1,000,000cr", "[P] Purchase [<>] Page [B] Back: "):
        assert len(page) + 3 <= height


def test_the_market_key_buys_a_step_after_confirmation(monkeypatch):
    world = _rich(20_000)
    keys = iter(["P", "N", "P", "Y", "B"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        said = vr.screen_stake(vr.Palette(False), world)
    assert vr.stake_level(world.save, 0) == 1 and world.save.pilot.credits == 5_000
    assert said and "step 1 of 3" in said and "Station Stake" in output.getvalue()
    assert "K" not in vr.MARKET_LETTERS[:9]  # the market's own letters never reach it
    assert vr.accolade_unlocked(world.save, "stake_1")
