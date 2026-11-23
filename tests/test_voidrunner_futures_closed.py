"""The futures exchange is closed, and an order a save still holds is refunded (review).

Futures cost every simulated pilot who used them money, so the exchange is
gone. These pin what is left of it: a save written by a build that sold
futures is read, every open order is refunded in full the moment the career is
opened, its reserved stock goes back to the station, the pilot is told once on
the first deck, and nothing about futures is written again.
"""

from __future__ import annotations

import copy

import pytest

from .support import _world_with_seed, plain, vr


def _order(number=1, commodity="food", quantity=4, paid=44, origin=0, principal=40, reserved=4, settle=9):
    return {"id": number, "commodity": commodity, "quantity": quantity, "locked_price": paid, "settle_turn": settle,
            "origin_system": origin, "principal": principal, "reserved": reserved}


def _old_save(*orders, turn=6):
    """A save as a build with a futures exchange wrote it."""
    world = _world_with_seed(42)
    world.save.turn = turn
    record = world.save.to_dict()
    record["active_futures"] = list(orders)
    record["next_futures_id"] = len(orders) + 1
    return record


def test_an_open_order_is_refunded_in_full_when_the_career_is_opened():
    record = _old_save(_order(1, "food", 4, 44), _order(2, "machinery", 2, 151, principal=140, reserved=2))
    before = record["pilot"]["credits"]
    world = vr.World(vr.SaveData.from_dict(record))
    assert world.save.pilot.credits == before + 44 + 151  # goods and fee: the exchange closed, the pilot did not cancel
    assert world.save.closed_futures == []
    assert world.futures_refund == "The futures exchange has closed. Your 2 open orders were refunded in full: +195cr."
    assert world.save.pilot.log[-1] == world.futures_refund
    one = vr.World(vr.SaveData.from_dict(_old_save(_order())))
    assert one.futures_refund == "The futures exchange has closed. Your 1 open order was refunded in full: +44cr."


def test_the_stock_an_order_reserved_goes_back_to_its_station():
    record = _old_save(_order(1, "food", 4, 44, reserved=4))
    record["market_depth"] = {"0": {"food": {"day": 6, "stock": 10, "demand": 20}}}
    world = vr.World(vr.SaveData.from_dict(record))
    assert vr.market_depth_quote(world, 0, "food")["stock"] == 14
    # Never past what the station can hold.
    full = _old_save(_order(1, "food", 4, 44, reserved=4))
    ceiling = vr.market_depth_limits(vr.World(vr.SaveData.from_dict(_old_save())).by_id[0].economy, "food")["stock"]
    full["market_depth"] = {"0": {"food": {"day": 6, "stock": ceiling - 1, "demand": 20}}}
    assert vr.market_depth_quote(vr.World(vr.SaveData.from_dict(full)), 0, "food")["stock"] == ceiling


def test_a_contraband_order_is_undone_in_the_contraband_balance_too():
    record = _old_save(_order(1, "weapons", 2, 300, principal=280, reserved=2))
    record["contraband_trade_balance"] = -300
    world = vr.World(vr.SaveData.from_dict(record))
    assert world.save.contraband_trade_balance == 0 and world.save.pilot.credits == record["pilot"]["credits"] + 300


def test_nothing_about_futures_is_written_again_and_the_refund_is_paid_once():
    world = vr.World(vr.SaveData.from_dict(_old_save(_order())))
    written = world.save.to_dict()
    assert "active_futures" not in written and "next_futures_id" not in written and "closed_futures" not in written
    again = vr.World(vr.SaveData.from_dict(written))
    assert again.futures_refund is None and again.save.pilot.credits == world.save.pilot.credits
    # A save with no orders says nothing and pays nothing.
    quiet = vr.World(vr.SaveData.from_dict(_old_save()))
    assert quiet.futures_refund is None and quiet.save.pilot.credits == _old_save()["pilot"]["credits"]


def test_the_first_deck_says_so_once_and_a_journey_in_progress_does_not_lose_it():
    world = vr.World(vr.SaveData.from_dict(_old_save(_order())))
    notes = vr.launch_notes(world, is_new=False)
    assert notes[0].startswith("Welcome back") and notes[1] == world.futures_refund
    world.save.pending_travel = {"anything": True}
    assert vr.launch_notes(world, is_new=False) == [world.futures_refund]
    plain_world = _world_with_seed(42)
    assert not any("futures" in note for note in vr.launch_notes(plain_world, is_new=False))


@pytest.mark.parametrize("damage", [
    lambda order: order.pop("origin_system"),
    lambda order: order.__setitem__("commodity", "moonbeams"),
    lambda order: order.__setitem__("quantity", 0),
    lambda order: order.__setitem__("principal", 99),          # more than was paid
    lambda order: order.__setitem__("reserved", 9),            # more than was ordered
    lambda order: order.__setitem__("origin_system", 999),
])
def test_an_order_that_cannot_be_read_still_stops_the_load(damage):
    order = _order()
    damage(order)
    with pytest.raises((vr.ResumeError, vr.UnsupportedSave, ValueError)):
        vr.SaveData.from_dict(_old_save(order))


def test_the_market_the_deck_and_the_recap_no_longer_mention_futures(monkeypatch):
    world = _world_with_seed(42)
    assert vr.pilot_recap(world)[1] == "Commitments: 0 contracts."
    world.save.active_missions = [vr.Mission(1, "scan", "Survey", 300, 0, 3)]
    assert vr.pilot_recap(world)[1] == "Commitments: 1 contract."
    text = " ".join(plain(line) for line in vr.station_deck_lines(world, expanded=True) + vr.pilot_guide_lines(world)
                    + vr.trading_ledger_lines(world))
    assert "utures" not in text and "orders" not in text
    for gone in ("buy_futures_contract", "settle_futures_contracts", "screen_futures", "futures_alert_rows",
                 "season_forecast", "FUTURES_FEE_PERCENT"):
        assert not hasattr(vr, gone)
    # Seasons stay: the market still marks a price rising or falling.
    assert hasattr(vr, "season_trend") and vr.SEASON_SWING > 0
