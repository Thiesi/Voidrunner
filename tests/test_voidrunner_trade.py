"""The economy: markets, depth, remembered quotes, routes, the ledger,
futures orders, the shipyard and the ship's own derived stats.

Split out of `test_voidrunner_domain.py` (issue #422).
"""

from __future__ import annotations

import contextlib
import io
import random
import sys
import time

import pytest

from .support import plain, plain_bytes, shows_page, _VOIDRUNNER_PATH, _add_cargo, _door_stopped_at, _drain_until, _live_voidrunner, _set_cargo, _world_at_food_producer, _world_with_pending_fight, _world_with_seed, page_rows, page_text, page_title, vr


def _world_with_market_memory():
    world = _world_with_seed(42)
    destination = world.here.connections[0]
    world.save.current_system = destination
    world.by_id[destination].discovered = True
    vr.remember_local_market(world)
    world.save.current_system = 0
    vr.remember_local_market(world)
    world.save.discovered = [system.id for system in world.galaxy if system.discovered]
    return world, destination


def test_market_memory_observes_locally_and_stays_stale_until_revisited(tmp_path):
    import copy
    world, destination = _world_with_market_memory()
    initial = copy.deepcopy(world.save.market_memory[destination])
    rng = world.event_rng.getstate()
    world.save.turn = 7
    vr._nudge_drift(world, destination, "food", 0.5)
    world.checkpoint()
    assert world.save.market_memory[destination] == initial
    assert world.event_rng.getstate() == rng
    assert world.save.market_memory[0]["food"]["day"] == 7
    vr.persist(world, tmp_path, 77)
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert saved.market_memory[destination] == initial
    world = vr.World(saved)
    world.save.current_system = destination
    world.checkpoint()
    assert world.save.market_memory[destination]["food"]["day"] == 7
    assert world.save.market_memory[destination]["food"]["sell"] != initial["food"]["sell"]


def test_market_memory_does_not_invent_history_from_legacy_discoveries():
    world = _world_with_seed(42)
    data = world.save.to_dict()
    data.pop("market_memory")
    data["discovered"] = list(world.by_id)
    old = vr.World(vr.SaveData.from_dict(data))
    assert not old.save.market_memory
    old.checkpoint()
    assert set(old.save.market_memory) == {0}
    old.save.pending_travel = {}
    old.save.current_system = 1
    vr.remember_local_market(old)
    assert set(old.save.market_memory) == {0}


def test_market_memory_keeps_the_last_contraband_sale_quote_without_inventing_open_purchase():
    world = _world_with_seed(42)
    _set_cargo(world, {"weapons": 1})
    vr.remember_local_market(world)
    quote = dict(world.save.market_memory[0]["weapons"])
    assert quote["buy"] is None and quote["sell"] > 0
    world.save.cargo.clear()
    world.save.turn = 3
    vr.remember_local_market(world)
    assert world.save.market_memory[0]["weapons"] == quote
    assert world.save.market_memory[0]["food"]["day"] == 3


def test_market_memory_data_burst_records_only_the_revealed_remote_quote_without_extra_rng():
    world = _world_with_seed(42)
    for system in world.galaxy:
        system.discovered = True
    world.event_rng.seed(919)
    expected = random.Random()
    expected.setstate(world.event_rng.getstate())
    dest = world.here
    hops = vr.bfs_hops(world.by_id, dest.id)
    sid = expected.choice([sid for sid, h in hops.items() if 1 <= h <= 4 and world.by_id[sid].discovered])
    commodity = expected.choice(list(vr.COMMODITIES))
    unit = vr.price_for(world, sid, commodity)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr._encounter_market_tip(vr.Palette(False), world, dest)
    assert set(world.save.market_memory) == {0, sid}
    assert set(world.save.market_memory[sid]) == {commodity}
    assert world.save.market_memory[sid][commodity]["sell"] == round(unit * vr.SELL_SPREAD)
    assert world.event_rng.getstate() == expected.getstate()
    assert "Recorded on day 0" in output.getvalue()


def test_completed_market_memory_tip_replays_without_quotes_rng_or_checkpoint(monkeypatch):
    import copy
    world, destination = _world_with_market_memory()
    world.save.pending_travel = {"version": 1, "origin": 0, "destination": destination,
        "was_discovered": True, "destroyed": False, "phase": "primary", "primary": "random",
        "bounty": None, "escorts": [], "escort_index": 0,
        "encounter": {"kind": "tip", "done": True, "result": ["Previously recorded market report."]}}
    world.checkpoint()
    world = vr.World(vr.SaveData.from_dict(world.save.to_dict()))
    before = copy.deepcopy(world.save.to_dict())
    rng = world.event_rng.getstate()
    world._checkpoint = lambda current: pytest.fail("Completed report wrote another checkpoint")
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr._encounter_market_tip(vr.Palette(False), world, world.by_id[destination])
        vr._resolve_random_travel_encounter(vr.Palette(False), world, world.by_id[destination])
    assert output.getvalue().count("Previously recorded market report.") == 2
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


def test_trade_route_quote_uses_stale_sale_data_and_exact_fuel_wage_budget(monkeypatch):
    import copy
    world, destination = _world_with_market_memory()
    old_sale = world.save.market_memory[destination]["food"]["sell"]
    world.save.turn = 6
    world.save.ship.fuel = 0
    world.save.ship.has_engineer = world.save.ship.has_navigator = True
    world.save.pilot.credits = 0
    original = vr.price_for

    def local_only(current, sid, commodity):
        assert sid == current.here.id, "Advice queried a live remote market"
        return original(current, sid, commodity)

    monkeypatch.setattr(vr, "price_for", local_only)
    before = copy.deepcopy(world.save.to_dict())
    quote = vr.trade_route_quote(world, destination, "food", 3)
    path = vr.bfs_path(world.by_id, 0, destination)
    assert quote["observed_day"] == 0 and quote["receipts"] == old_sale * 3
    assert quote["cargo_cost"] == 3 * original(world, 0, "food")
    assert quote["wages"] == len(path) * (vr.CREW_ROLES["engineer"]["wage"] + vr.CREW_ROLES["navigator"]["wage"])
    assert quote["fuel_cash"] == quote["fuel"] * 6
    assert quote["cash_needed"] == quote["cargo_cost"] + quote["fuel_cash"] + quote["wages"]
    assert quote["margin"] == quote["receipts"] - quote["cash_needed"]
    assert "SHORT" in " ".join(vr.trade_route_lines(world, destination, "food", 3, False))
    assert world.save.to_dict() == before


@pytest.mark.parametrize("quantity", [1, 2, 3, 4, 5])
def test_trade_route_held_cargo_preview_matches_actual_fifo_disposal(quantity):
    world, destination = _world_with_market_memory()
    vr._acquire_cargo(world, "food", 2, 60)
    vr._acquire_cargo(world, "food", 3, 100)
    quote = vr.trade_route_quote(world, destination, "food", quantity, use_hold=True)
    cost = vr._dispose_cargo(world, "food", quantity, proceeds=quote["receipts"], kind="sale")
    assert quote["cargo_cost"] == cost
    assert quote["procurement"] == 0


def test_trade_route_known_hold_uses_actual_basis_not_current_purchase_price():
    world, destination = _world_with_market_memory()
    vr._acquire_cargo(world, "food", 2, 17)
    quote = vr.trade_route_quote(world, destination, "food", 2, use_hold=True)
    assert quote["cargo_cost"] == 17 and quote["procurement"] == 0
    assert quote["margin"] == quote["receipts"] - 17 - quote["fuel"] * 6


@pytest.mark.parametrize("deadline,conflict", [(0, False), (1, True), (None, True)])
def test_trade_route_delivery_conflicts_respect_arrival_deadline(deadline, conflict):
    world, destination = _world_with_market_memory()
    world.save.active_missions = [vr.Mission(id=1, kind="delivery", description="Reserved cargo",
        reward=100, origin_system=0, target_system=destination, commodity="food", quantity=3, deadline_turn=deadline)]
    quote = vr.trade_route_quote(world, destination, "food", 3)
    assert bool(quote["conflicts"]) is conflict
    assert (quote["margin"] is None) is conflict


def test_trade_route_does_not_claim_an_underfilled_delivery_consumes_the_load():
    world, destination = _world_with_market_memory()
    world.save.active_missions = [vr.Mission(id=1, kind="delivery", description="Larger delivery",
        reward=100, origin_system=0, target_system=destination, commodity="food", quantity=3)]
    quote = vr.trade_route_quote(world, destination, "food", 2)
    assert not quote["conflicts"] and quote["margin"] is not None


@pytest.mark.parametrize("deliveries,conflicts", [([3], []), ([3, 3], ["Delivery 2"]), ([4], ["Delivery 1"])])
def test_trade_route_new_purchase_uses_older_cargo_as_delivery_buffer(deliveries, conflicts):
    world, destination = _world_with_market_memory()
    _set_cargo(world, {"food": 3})
    world.save.active_missions = [vr.Mission(id=i, kind="delivery", description=f"Delivery {i}",
        reward=100, origin_system=0, target_system=destination, commodity="food", quantity=quantity)
        for i, quantity in enumerate(deliveries, 1)]
    quote = vr.trade_route_quote(world, destination, "food", 3)
    assert quote["conflicts"] == conflicts
    assert (quote["margin"] is None) is bool(conflicts)
    held = vr.trade_route_quote(world, destination, "food", 3, use_hold=True)
    assert bool(held["conflicts"]) is (deliveries[0] <= 3)


def test_trade_route_hides_uncharted_intermediate_names_and_rejects_oversized_legs(monkeypatch):
    world, _ = _world_with_market_memory()
    destination = max(world.by_id, key=lambda sid: len(vr.bfs_path(world.by_id, 0, sid)))
    path = vr.bfs_path(world.by_id, 0, destination)
    assert len(path) > 1
    world.save.current_system = destination
    vr.remember_local_market(world)
    world.save.current_system = 0
    for sid in path[:-1]:
        world.by_id[sid].discovered = False
    lines = " ".join(vr.trade_route_lines(world, destination, "food", 1, False))
    assert "Uncharted system" in lines and "danger unknown" in lines
    assert all(world.by_id[sid].name not in lines for sid in path[:-1])
    monkeypatch.setattr(vr, "fuel_capacity", lambda ship: 0)
    quote = vr.trade_route_quote(world, destination, "food", 1)
    assert not quote["feasible"]
    lines = " ".join(vr.trade_route_lines(world, destination, "food", 1, False))
    assert "INFEASIBLE" in lines and "Estimated margin" not in lines


@pytest.mark.parametrize("fault", ["unknown_quote", "same_station", "quantity", "boolean", "space", "missing_hold", "pending", "illegal"])
def test_trade_route_rejections_write_nothing(fault):
    import copy
    world, destination = _world_with_market_memory()
    commodity, quantity, held = "food", 1, False
    if fault == "unknown_quote": world.save.market_memory.clear()
    elif fault == "same_station": destination = 0
    elif fault == "quantity": quantity = -1
    elif fault == "boolean": quantity = True
    elif fault == "space": quantity = vr.cargo_capacity(world.save.ship) + 1
    elif fault == "missing_hold": held = True
    elif fault == "pending": world.save.pending_travel = {}
    elif fault == "illegal":
        commodity = "weapons"
        world.save.market_memory[destination][commodity] = {"day": 0, "buy": 100, "sell": 80}
    before = copy.deepcopy(world.save.to_dict())
    with pytest.raises(vr.TradeError):
        vr.trade_route_quote(world, destination, commodity, quantity, use_hold=held)
    assert world.save.to_dict() == before


@pytest.mark.parametrize("memory", [
    None, [], {"48": {}}, {"1": {}, "01": {}}, {"1": {"invalid": {}}},
    {"1": {"food": {"day": 1, "buy": 10, "sell": 8}}},
    {"1": {"food": {"day": False, "buy": 10, "sell": 8}}},
    {"1": {"food": {"day": 0, "buy": 0, "sell": 8}}},
    {"1": {"food": {"day": 0, "buy": 10, "sell": -1}}},
    {"1": {"food": {"day": 0, "buy": 10, "sell": 0}}},
    {"1": {"food": {"day": 0, "buy": 10}}},
    {"1": {"food": {"day": 0, "buy": 10, "sell": 8, "future": 1}}},
])
def test_market_memory_rejects_malformed_quotes_without_rewriting(tmp_path, memory):
    import json
    data = _world_with_seed(42).save.to_dict()
    data["market_memory"] = memory
    malformed = json.dumps(data).encode()
    (tmp_path / "77.json").write_bytes(malformed)
    with pytest.raises(vr.ResumeError):
        vr.load_or_create_save(tmp_path, 77, "Tester")
    assert (tmp_path / "77.json").read_bytes() == malformed


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
@pytest.mark.parametrize("screen", ["memory", "route"])
def test_market_memory_and_route_pages_reach_the_end_within_terminal_size(monkeypatch, terminal, width, height, screen):
    import copy
    import re
    world = _world_with_seed(42)
    for system in world.galaxy:
        world.save.current_system = system.id
        system.discovered = True
        vr.remember_local_market(world)
    world.save.current_system = 0
    world.save.turn = 9
    before = copy.deepcopy(world.save.to_dict())
    rng = world.event_rng.getstate()
    terminal(width, height)
    output = io.StringIO()
    pages = []
    title = "Market Memory" if screen == "memory" else "Trade Route"

    def choose():
        value = output.getvalue()
        pages.append(value)
        output.seek(0)
        output.truncate(0)
        assert len(pages) < 2000
        match = re.search(re.escape(title) + r"\s+(\d+)/(\d+)", page_title(value))
        assert match
        assert world.save.to_dict() == before
        return "B" if match[1] == match[2] else "N"

    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output):
        if screen == "memory":
            vr.screen_remembered_markets(vr.Palette(False), world)
        else:
            vr.screen_trade_route(vr.Palette(False), world)
    for page in pages:
        assert len(page.splitlines()) <= height, (width, height, page)
        assert all(vr._visible_width(row) <= width for row in page.splitlines())
    assert world.event_rng.getstate() == rng
    if screen == "memory":
        text = " ".join(" ".join(pages).split())
        assert all(system.name in text for system in world.galaxy)


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_trade_route_destination_picker_pages_keep_selection_and_back_available(monkeypatch, terminal, width, height):
    import re
    terminal(width, height)
    options = [(i, f"Very Long Station Destination Number {i}") for i in range(48)]
    output = io.StringIO()
    pages = []

    def choose():
        value = output.getvalue()
        pages.append(value)
        output.seek(0)
        output.truncate(0)
        # At the 40-column floor the bar needs a row more than the glued style
        # did (#400), so every label is now one row too tall for a shared page
        # and the picker heads each page "Choice n" instead; the counter is the
        # same either way.
        match = re.search(r"(\d+)/(\d+)", " ".join(value.split()))
        assert match and len(pages) < 500
        return "1" if match[1] == match[2] else "N"

    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output):
        selected = vr._pick_trade_field("Destination", options)
    assert 0 <= selected <= 47
    assert "Number 47" in " ".join(pages[-1].split()) or "47" in pages[-1]
    for page in pages:
        assert len(page.splitlines()) <= height, page
        assert all(vr._visible_width(row) <= width for row in page.splitlines())
        # A narrow bar wraps (#400), and its keys are coloured (#532), so this
        # reads what a caller reads rather than the bytes that carried it.
        assert "[B] Back" in " ".join(plain(page).split())


def test_trade_route_editing_fields_and_cancelling_is_read_only(monkeypatch):
    import copy
    world, _ = _world_with_market_memory()
    for sid in (2, 3):
        world.save.current_system = sid
        vr.remember_local_market(world)
    world.save.current_system = 0
    before = copy.deepcopy(world.save.to_dict())
    commands = iter("ED2C2UHHSB")
    monkeypatch.setattr(vr, "read_key", lambda: next(commands))
    monkeypatch.setattr(vr, "read_line_raw", lambda **kwargs: "2")
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_trade_route(vr.Palette(False), world)
    said = plain(output.getvalue())
    assert "x2" in said
    assert "existing hold cargo" in said and "buy new cargo here" in said
    assert world.save.to_dict() == before


@pytest.mark.parametrize("commands", [b"TMBRBBQ", b"TR", b"TREU3\nBBBQ", b"TREU3\n"])
def test_real_market_memory_and_route_back_or_eof_preserve_career(tmp_path, commands):
    import json
    import os
    import subprocess
    world, _ = _world_with_market_memory()
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    original = (tmp_path / "77.json").read_bytes()
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=commands, capture_output=True,
                            env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)), timeout=10)
    assert result.returncode == 0 and not result.stderr
    assert shows_page(result.stdout, "Trade Route")
    if b"M" in commands:
        assert shows_page(result.stdout, "Market Memory")
    if b"E" in commands:
        assert shows_page(result.stdout, "Route Draft")
        # `out_prompt` styles the bar, and the quantity range inside it is a
        # value, so the prompt is read through `plain_bytes` (issue #532).
        assert b"Quantity 1-" in plain_bytes(result.stdout)
    assert (tmp_path / "77.json").read_bytes() == original


def test_real_market_memory_is_saved_after_trade_and_arrival_before_acknowledgement(tmp_path):
    world = _world_with_seed(42)
    world.event_rng.seed(0)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    key = vr.MARKET_LETTERS[vr.LEGAL_COMMODITIES.index("food")].encode()
    with _door_stopped_at(tmp_path, b"M" + key + b"P2\n", b"Bought 2x"):
        bought, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert bought.market_memory[0]["food"]["buy"] == vr.price_for(vr.World(bought), 0, "food")
    destination = sorted(world.here.connections)[0]
    jump_key = vr.CHART_CONNECTION_LETTERS[0].encode()
    marker = world.by_id[destination].station_name.upper().encode()
    with _door_stopped_at(tmp_path, b"C" + jump_key + b"Y", marker):
        arrived, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert arrived.current_system == destination and arrived.pending_travel is None
        assert arrived.market_memory[destination]["food"]["day"] == 1
        assert arrived.market_memory[0]["food"] == bought.market_memory[0]["food"]


def test_trading_ledger_preserves_fifo_costs_across_a_restart(tmp_path):
    import json
    world = _world_with_seed(42)
    first_cost = 3 * vr.price_for(world, 0, "food")
    vr.trade_cargo(world, "food", 3, buying=True)
    second_cost = 2 * vr.price_for(world, 0, "food")
    vr.trade_cargo(world, "food", 2, buying=True)
    vr.persist(world, tmp_path, 77)
    save, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    world = vr.World(save)
    unit = round(vr.price_for(world, 0, "food") * vr.SELL_SPREAD)
    vr.trade_cargo(world, "food", 3, buying=False)
    ledger = world.save.trading_ledger
    assert ledger.sales_revenue == 3 * unit and ledger.sales_cost == first_cost
    assert world.save.cargo_basis["food"] == [[2, second_cost]]
    vr.trade_cargo(world, "food", 2, buying=False)
    assert ledger.sales_cost == first_cost + second_cost
    assert not world.save.cargo_basis and not world.save.cargo
    assert vr.SaveData.from_dict(json.loads(json.dumps(world.save.to_dict()))).trading_ledger == ledger


def test_trading_ledger_partial_disposal_keeps_the_exact_paid_remainder():
    world = _world_with_seed(42)
    vr._acquire_cargo(world, "food", 3, 100)
    for expected in (33, 66, 100):
        vr.trade_cargo(world, "food", 1, buying=False)
        assert world.save.trading_ledger.sales_cost == expected
    assert not world.save.cargo_basis


def test_trading_ledger_futures_basis_includes_fee_once_and_cancel_keeps_no_cargo():
    world = _world_with_seed(42)
    principal, fee = vr.futures_quote(world, "food", 3)
    vr.buy_futures_contract(world, "food", 3, 5)
    world.save.turn = 5
    vr.settle_futures_contracts(world)
    assert world.save.cargo_basis == {"food": [[3, principal + fee]]}
    assert vr.settle_futures_contracts(world) == []
    vr.buy_futures_contract(world, "food", 1, 5)
    order = world.save.active_futures[0]
    vr.cancel_futures_contract(world, order.id)
    assert world.save.trading_ledger.cancelled_fees == order.locked_price - order.principal
    assert world.save.cargo == {"food": 3}


def test_trading_ledger_delivery_records_the_payment_and_consumes_basis_once():
    world = _world_with_seed(42)
    cost = 3 * vr.price_for(world, 0, "food")
    vr.trade_cargo(world, "food", 3, buying=True)
    world.save.active_missions = [vr.Mission(id=1, kind="delivery", description="Full load",
        reward=100, origin_system=0, target_system=0, commodity="food", quantity=3)]
    vr.check_mission_completions(world)
    ledger = world.save.trading_ledger
    assert (ledger.delivery_revenue, ledger.delivery_cost) == (100, cost)
    assert ledger.sales_cost == ledger.sales_revenue == 0
    assert not world.save.cargo and not world.save.cargo_basis
    assert vr.check_mission_completions(world) == []
    assert ledger.delivery_revenue == 100


@pytest.mark.parametrize("loss", ["dump", "destroy", "customs", "refused_bribe"])
def test_trading_ledger_records_real_loss_paths(monkeypatch, loss):
    world = _world_with_seed(42)
    world.save.current_system = next(s.id for s in world.galaxy if s.economy == "Haven")
    cost = 2 * vr.price_for(world, world.here.id, "weapons")
    vr.trade_cargo(world, "weapons", 2, buying=True)
    if loss == "dump":
        vr.dump_all_contraband(world)
    elif loss == "destroy":
        vr.destroy_ship(world)
    else:
        monkeypatch.setattr(vr, "read_key", lambda: "S" if loss == "customs" else "P")
        monkeypatch.setattr(world.event_rng, "random", lambda: 0.99)
        monkeypatch.setattr(vr, "pause", lambda p: None)
        with contextlib.redirect_stdout(io.StringIO()):
            vr.screen_customs(vr.Palette(False), world)
    ledger = world.save.trading_ledger
    assert ledger.cargo_loss_cost == cost
    assert not world.save.cargo and not world.save.cargo_basis
    assert ledger.sales_revenue == ledger.delivery_revenue == 0


def test_trading_ledger_counts_paid_wages_and_fuel_only(monkeypatch):
    world = _world_with_seed(42)
    world.save.ship.has_engineer = world.save.ship.has_gunner = True
    world.save.pilot.credits = 2
    vr.pay_crew_wages(world)
    assert world.save.trading_ledger.wages == 2
    assert not world.save.ship.has_gunner and world.save.ship.has_engineer
    world.save.pilot.credits = 100
    world.save.ship.fuel -= 3
    monkeypatch.setattr(vr, "read_line_raw", lambda **kwargs: "3")
    with contextlib.redirect_stdout(io.StringIO()):
        vr._refuel(vr.Palette(False), world)
    assert world.save.trading_ledger.fuel_spend == 18 and world.save.pilot.credits == 82


@pytest.mark.parametrize("fault", ["quantity", "boolean", "commodity", "pending", "space", "credits", "absent", "illegal"])
def test_trading_ledger_rejected_commands_are_atomic(fault):
    import copy
    world = _world_with_seed(42)
    commodity, quantity, buying = "food", 1, True
    if fault == "quantity": quantity = 0
    elif fault == "boolean": quantity = True
    elif fault == "commodity": commodity = "invalid"
    elif fault == "pending": world.save.pending_travel = {}
    elif fault == "space": quantity = vr.cargo_capacity(world.save.ship) + 1
    elif fault == "credits": world.save.pilot.credits = 0
    elif fault == "absent": buying = False
    elif fault == "illegal": commodity = "weapons"
    before = copy.deepcopy(world.save.to_dict())
    with pytest.raises(vr.TradeError):
        vr.trade_cargo(world, commodity, quantity, buying=buying)
    assert world.save.to_dict() == before


@pytest.mark.parametrize("field,value", [
    ("cargo_basis", {"food": [[0, 3]]}), ("cargo_basis", {"food": [[4, 3]]}),
    ("cargo_basis", {"food": [[1, -3]]}), ("cargo_basis", {"food": [[True, 3]]}),
    ("cargo_basis", {"food": []}), ("cargo_basis", {"food": [[1, 3, 4]]}),
    ("trading_ledger", {"since_day": 1}), ("trading_ledger", {"since_day": None, "wages": 1}),
    ("trading_ledger", {"since_day": 0, "wages": -1}), ("trading_ledger", {"since_day": 0, "wages": True}),
    ("trading_ledger", {"since_day": 0, "future_stat": 1}),
])
def test_trading_ledger_rejects_malformed_storage_without_overwriting(tmp_path, field, value):
    import copy
    import json
    world = _world_with_seed(42)
    vr.trade_cargo(world, "food", 3, buying=True)
    vr.persist(world, tmp_path, 77)
    original = (tmp_path / "77.json").read_bytes()
    data = copy.deepcopy(world.save.to_dict())
    data[field] = value
    with pytest.raises(vr.ResumeError):
        vr.SaveData.from_dict(data)
    assert (tmp_path / "77.json").read_bytes() == original
    # The disk loading boundary also preserves the exact malformed bytes.
    malformed = json.dumps(data).encode()
    (tmp_path / "77.json").write_bytes(malformed)
    with pytest.raises(vr.ResumeError):
        vr.load_or_create_save(tmp_path, 77, "Tester")
    assert (tmp_path / "77.json").read_bytes() == malformed


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_trading_ledger_pages_fit_and_do_not_write(monkeypatch, terminal, width, height):
    import copy
    world = _world_with_seed(42)
    vr.trade_cargo(world, "food", 3, buying=True)
    before = copy.deepcopy(world.save.to_dict())
    rng = world.event_rng.getstate()
    terminal(width, height)
    count = len(vr._trade_pages(vr.trading_ledger_lines(world), "Trading Ledger", "[M] Markets [R] Route [N] Next [P] Prev [B] Back: "))
    output = io.StringIO()
    pages = []

    def choose():
        pages.append(output.getvalue())
        output.seek(0)
        output.truncate(0)
        assert world.save.to_dict() == before
        return "B" if len(pages) == count else "N"

    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output):
        vr.screen_trading_ledger(vr.Palette(False), world)
    assert world.event_rng.getstate() == rng
    for page in pages:
        assert len(page.splitlines()) <= height
        assert all(vr._visible_width(row) <= width for row in page.splitlines())


def test_trading_ledger_real_purchases_and_sales_survive_kill_without_duplicate_margin(tmp_path):
    world = _world_with_seed(42)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    cost = 3 * vr.price_for(world, 0, "food")
    key = vr.MARKET_LETTERS[vr.LEGAL_COMMODITIES.index("food")].encode()
    with _door_stopped_at(tmp_path, b"M" + key + b"P3\n", b"Bought 3x"):
        bought, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert bought.cargo_basis == {"food": [[3, cost]]}
        assert bought.market_depth[0]["food"] == {"day": 0, "stock": 45, "demand": 96}
    unit = round(vr.price_for(vr.World(bought), 0, "food") * vr.SELL_SPREAD)
    with _door_stopped_at(tmp_path, b"M" + key + b"S2\n", b"Sold 2x"):
        sold, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert sold.cargo_basis == {"food": [[1, cost // 3]]}
        assert sold.market_depth[0]["food"] == {"day": 0, "stock": 47, "demand": 94}
        assert (sold.trading_ledger.sales_cost, sold.trading_ledger.sales_revenue) == (cost * 2 // 3, 2 * unit)
    # The title and its counter sit at opposite ends of the border now, so
    # the marker is the title alone (issue #493).
    with _door_stopped_at(tmp_path, b"T", b"Trading Ledger"):
        viewed, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert viewed.trading_ledger == sold.trading_ledger
        assert viewed.market_depth == sold.market_depth


@pytest.mark.parametrize("commands", [b"TBQ", b"T"])
def test_trading_ledger_real_back_and_eof_preserve_career(tmp_path, commands):
    import json
    import os
    import subprocess
    world = _world_with_seed(42)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    original = (tmp_path / "77.json").read_bytes()
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=commands, capture_output=True,
                            env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)), timeout=10)
    assert result.returncode == 0 and not result.stderr
    assert shows_page(result.stdout, "Trading Ledger")
    assert (tmp_path / "77.json").read_bytes() == original


def test_trading_ledger_retirement_starts_a_fresh_record():
    world = _world_with_seed(42)
    vr.trade_cargo(world, "food", 2, buying=True)
    vr.trade_cargo(world, "food", 1, buying=False)
    fresh = vr.retire_pilot(world.save)
    assert not fresh.cargo_basis and fresh.trading_ledger == vr.TradingLedger()


def test_trading_ledger_zero_quantities_do_not_block_cargo_cleanup():
    world = _world_with_seed(42)
    _set_cargo(world, {"weapons": 0})
    vr.dump_all_contraband(world)
    assert not world.save.cargo and world.save.trading_ledger.since_day is None


@pytest.mark.parametrize("quantity", [0, 1])
def test_trading_ledger_combat_dump_accounts_only_for_real_cargo(monkeypatch, quantity):
    world = _world_with_seed(42)
    cost = vr.price_for(world, 0, "food") if quantity else 0
    if quantity:
        vr.trade_cargo(world, "food", 1, buying=True)
    else:
        _set_cargo(world, {"food": 0})  # Valid older saves can retain zero entries.
    pirate = vr.generate_pirate(world, tier=1)
    monkeypatch.setattr(vr, "read_key", lambda: "D" if quantity else "E")  # Dump is only offered with cargo aboard (#414)
    monkeypatch.setattr(world.event_rng, "random", lambda: 0)
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.screen_combat(vr.Palette(False), world, pirate) == "escaped"
    assert world.save.trading_ledger.cargo_loss_cost == cost
    assert not world.save.cargo_basis


def test_market_memory_rejects_uncharted_observation_before_exposing_station(tmp_path):
    import json
    world = _world_with_seed(42)
    sid = next(s.id for s in world.galaxy if s.id not in world.save.discovered)
    world.save.market_memory[sid] = {"food": {"day": 0, "buy": 12, "sell": 11}}
    raw = json.dumps(world.save.to_dict()).encode()
    (tmp_path / "77.json").write_bytes(raw)
    with pytest.raises(vr.ResumeError, match="outside the chart"):
        vr.load_or_create_save(tmp_path, 77, "Tester")
    assert (tmp_path / "77.json").read_bytes() == raw


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_trade_route_draft_cancel_retains_original_and_fits_pages(monkeypatch, terminal, width, height):
    import copy, re
    world, destination = _world_with_market_memory()
    initial = dict(destination=destination, commodity="food", quantity=1, use_hold=False)
    before = copy.deepcopy(world.save.to_dict()); rng = world.event_rng.getstate()
    terminal(width, height)
    output = io.StringIO(); frames = []
    def choose():
        frame = output.getvalue(); frames.append(frame); output.seek(0); output.truncate(0)
        match = re.search(r"Route Draft (\d+)/(\d+)", page_title(frame))
        assert match and len(frames) < 100
        if len(frames) == 1: return "H"
        return "B" if match[1] == match[2] else "N"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output): assert vr._edit_trade_route(vr.Palette(False), world, initial) is None
    assert initial["use_hold"] is False and initial["quantity"] == 1
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng
    assert all(len(frame.splitlines()) <= height for frame in frames)
    assert all(vr._visible_width(line) <= width for frame in frames for line in frame.splitlines())


def test_trade_route_draft_rejection_keeps_edits_until_apply(monkeypatch):
    import copy
    world, destination = _world_with_market_memory()
    initial = dict(destination=destination, commodity="food", quantity=1, use_hold=False)
    before = copy.deepcopy(world.save.to_dict()); rng = world.event_rng.getstate()
    keys = iter("UHSHS")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(vr, "read_line_raw", lambda **kwargs: "3")
    with contextlib.redirect_stdout(io.StringIO()) as output: result = vr._edit_trade_route(vr.Palette(False), world, initial)
    assert result == {**initial, "quantity": 3} and initial["quantity"] == 1
    assert "Cannot apply" in plain(output.getvalue()) and "Quantity: 3" in plain(output.getvalue())
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


@pytest.mark.parametrize("economy,commodity,stock,demand", [
    ("Agricultural", "food", 96, 48), ("Industrial", "food", 48, 96),
    ("Tech", "electronics", 96, 48), ("Mining", "electronics", 48, 48),
    ("Haven", "weapons", 96, 48),
])
def test_market_depth_limits_explain_production_and_demand(economy, commodity, stock, demand):
    limits = vr.market_depth_limits(economy, commodity)
    assert limits == {"stock": stock, "demand": demand,
                      "stock_rate": stock // 16, "demand_rate": demand // 16}


def test_market_depth_consumption_restart_and_replenishment_are_bounded(tmp_path):
    import copy
    world = _world_with_seed(42)
    rng = world.event_rng.getstate()
    before = copy.deepcopy(world.save.to_dict())
    assert vr.market_depth_quote(world, 0, "food")["stock"] == 48
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng
    vr.trade_cargo(world, "food", 20, buying=True)
    vr.trade_cargo(world, "food", 20, buying=False)
    pool = vr.market_depth_quote(world, 0, "food")
    assert pool["stock"] == 48 and pool["demand"] == 76
    vr.persist(world, tmp_path, 1)
    restored = vr.World(vr.load_or_create_save(tmp_path, 1, "Tester")[0])
    assert vr.market_depth_quote(restored, 0, "food") == pool
    restored.save.turn += 2
    assert vr.market_depth_quote(restored, 0, "food")["demand"] == 88
    assert restored.save.market_depth[0]["food"]["day"] == 0
    restored.save.turn += 10**6
    assert vr.market_depth_quote(restored, 0, "food")["demand"] == 96
    assert world.event_rng.getstate() == rng


@pytest.mark.parametrize("buying", [True, False])
def test_market_depth_rejected_trade_is_atomic_including_prices_basis_and_rng(buying):
    import copy
    world = _world_with_seed(42)
    _set_cargo(world, {"food": 10})
    world.save.market_depth = {0: {"food": {"day": 0, "stock": 0, "demand": 0}}}
    before = copy.deepcopy(world.save.to_dict()); rng = world.event_rng.getstate()
    with pytest.raises(vr.TradeError, match="stock|Station can buy"):
        vr.trade_cargo(world, "food", 1, buying=buying)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


def test_market_depth_split_orders_and_buyback_cannot_restore_station_demand():
    world = _world_with_seed(42)
    world.save.ship.hull_class = "Carrier"; world.save.pilot.credits = 100000
    _set_cargo(world, {"food": 100})
    for _ in range(96):
        vr.trade_cargo(world, "food", 1, buying=False)
    vr.trade_cargo(world, "food", 1, buying=True)
    assert vr.market_depth_quote(world, 0, "food")["demand"] == 0
    with pytest.raises(vr.TradeError, match="Station can buy 0"):
        vr.trade_cargo(world, "food", 1, buying=False)
    world.save.turn = 1
    vr.trade_cargo(world, "food", 5, buying=False)
    assert vr.market_depth_quote(world, 0, "food")["demand"] == 1


def test_market_depth_old_career_and_wholesale_orders_follow_station_stock():
    import copy
    world = _world_with_seed(42)
    data = copy.deepcopy(world.save.to_dict()); data.pop("market_depth")
    restored = vr.World(vr.SaveData.from_dict(data))
    assert restored.save.market_depth == {} and vr.market_depth_quote(restored, 0, "food")["stock"] == 48
    restored.save.market_depth = {0: {"food": {"day": 0, "stock": 0, "demand": 0}}}
    restored.save.pilot.credits = 100000
    with pytest.raises(vr.TradeError, match="station stock"):
        vr.buy_futures_contract(restored, "food", 10, 5)
    restored.save.turn = 4  # replenished stock (3/day) admits the order
    vr.buy_futures_contract(restored, "food", 10, 5)
    assert vr.market_depth_quote(restored, 0, "food")["stock"] == 2
    restored.save.turn = 9
    vr.settle_futures_contracts(restored)
    assert restored.save.cargo["food"] == 10
    assert restored.save.trading_ledger.since_day == 9
    assert restored.save.market_depth[0]["food"] == {"day": 4, "stock": 2, "demand": 24}  # settlement leaves the pool alone


def test_market_depth_observations_are_stale_and_reports_do_not_invent_quantities():
    world, destination = _world_with_market_memory()
    observed = dict(world.save.market_memory[destination]["food"])
    assert {"stock", "demand"} <= observed.keys()
    world.save.turn = 10
    world.save.market_depth[destination] = {"food": {"day": 10, "stock": 0, "demand": 0}}
    assert world.save.market_memory[destination]["food"] == observed
    vr._remember_market_quote(world, destination, "food", 12)
    assert "stock" not in world.save.market_memory[destination]["food"]


def test_market_depth_route_refuses_unavailable_procurement_and_labels_sale_limits():
    import copy
    world, destination = _world_with_market_memory()
    world.save.market_depth[0] = {"food": {"day": 0, "stock": 0, "demand": 96}}
    before = copy.deepcopy(world.save.to_dict()); rng = world.event_rng.getstate()
    with pytest.raises(vr.TradeError, match="local stock"):
        vr.trade_route_quote(world, destination, "food", 1)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng
    world.save.cargo = {"food": 3}; world.save.cargo_basis = {"food": [[3, 21]]}
    world.save.market_memory[destination]["food"]["demand"] = 2
    quote = vr.trade_route_quote(world, destination, "food", 3, use_hold=True)
    assert quote["margin"] is None and quote["demand_shortfall"]
    text = " ".join(vr.trade_route_lines(world, destination, "food", 3, True))
    assert "DEMAND WARNING" in text and "may replenish" in text
    assert world.event_rng.getstate() == rng and world.save.market_depth == {0: {"food": {"day": 0, "stock": 0, "demand": 96}}}


@pytest.mark.parametrize("depth", [
    [], {"99": {}}, {"0": {}, "00": {}}, {"0": {"gold": {}}},
    {"0": {"food": {"day": 1, "stock": 1, "demand": 1}}},
    {"0": {"food": {"day": 0, "stock": 49, "demand": 1}}},
    {"0": {"food": {"day": 0, "stock": 1, "demand": 97}}},
    {"0": {"food": {"day": 0, "stock": -1, "demand": 1}}},
    {"0": {"food": {"day": False, "stock": 1, "demand": 1}}},
    {"0": {"food": {"day": 0, "stock": 1}}},
    {"0": {"food": {"day": 0, "stock": 1, "demand": 1, "future": 1}}},
])
def test_market_depth_malformed_state_preserves_original_file(tmp_path, depth):
    import json
    world = _world_with_seed(42); data = world.save.to_dict(); data["market_depth"] = depth
    path = tmp_path / "1.json"; path.write_text(json.dumps(data), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(vr.ResumeError):
        vr.load_or_create_save(tmp_path, 1, "Tester")
    assert path.read_bytes() == original


@pytest.mark.parametrize("commands,buying", [(["P"], True), (["S"], False)])
def test_market_depth_exhausted_pool_reports_reason_without_quantity_prompt(monkeypatch, commands, buying):
    import contextlib, io
    world = _world_with_seed(42); _set_cargo(world, {"food": 3})
    world.save.market_depth = {0: {"food": {"day": 0, "stock": 0, "demand": 0}}}
    keys = iter(commands); monkeypatch.setattr(vr, "read_command", lambda: next(keys))
    monkeypatch.setattr(vr, "read_line_raw", lambda **kwargs: pytest.fail("Exhausted pool prompted for quantity"))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf): vr._trade_commodity(vr.Palette(False), world, "food")
    text = " ".join(plain(buf.getvalue()).split())
    assert "Stock 0 (+3/day)" in text and "station buys 0 (+6/day)" in text
    assert "station stock" in text if buying else "demand is exhausted" in text


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_market_depth_commodity_details_fit_every_page_without_replenishing(monkeypatch, terminal, width, height):
    import copy, re
    world = _world_with_seed(42); world.save.market_depth = {0: {"metals": {"day": 0, "stock": 3, "demand": 4}}}
    before = copy.deepcopy(world.save.to_dict()); rng = world.event_rng.getstate()
    terminal(width, height)
    output = io.StringIO(); frames = []
    def choose():
        frame = output.getvalue(); frames.append(frame); output.seek(0); output.truncate(0)
        match = re.search(r"Refined Metals Exchange (\d+)/(\d+)", page_title(frame))
        assert match and len(frames) < 100
        return "Q" if match[1] == match[2] else ">"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output): vr._trade_commodity(vr.Palette(False), world, "metals")
    assert "Stock 3 (+6/day)" in page_text(frames)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng
    assert all(len(frame.splitlines()) <= height for frame in frames)
    assert all(vr._visible_width(line) <= width for frame in frames for line in frame.splitlines())


def test_market_depth_opening_assignment_does_not_quote_unavailable_procurement():
    world = _world_with_seed(42)
    assert vr.opening_assignment_offer(world) is not None
    world.save.market_depth[0] = {c: {"day": 0, "stock": 0, "demand": 0} for c in vr.COMMODITIES}
    assert vr.opening_assignment_offer(world) is None


def test_market_depth_contract_delivery_preserves_signed_terms_when_spot_demand_is_zero():
    world = _world_with_seed(42); _set_cargo(world, {"food": 3})
    world.save.market_depth = {0: {"food": {"day": 0, "stock": 0, "demand": 0}}}
    world.save.active_missions = [vr.Mission(id=1, kind="delivery", description="Signed delivery", reward=500,
        origin_system=1, target_system=0, commodity="food", quantity=3)]
    credits = world.save.pilot.credits
    vr.check_mission_completions(world)
    assert world.save.pilot.credits == credits + 500 and not world.save.active_missions and not world.save.cargo
    assert world.save.market_depth[0]["food"] == {"day": 0, "stock": 0, "demand": 0}


@pytest.mark.parametrize("quantity_fields", [{"stock": 1}, {"demand": 1}, {"stock": True, "demand": 1}, {"stock": 1, "demand": 97}])
def test_market_depth_observed_quantities_are_paired_and_bounded(quantity_fields):
    world = _world_with_seed(42)
    world.save.market_memory[0] = {"food": {"day": 0, "buy": 12, "sell": 11, **quantity_fields}}
    with pytest.raises(vr.ResumeError): vr.SaveData.from_dict(world.save.to_dict())


@pytest.mark.parametrize("economy", vr.ECONOMIES)
@pytest.mark.parametrize("quantity", ["stock", "demand"])
def test_market_depth_remembered_quantities_respect_each_station_ceiling(tmp_path, economy, quantity):
    import json
    world = _world_with_seed(0)
    sid = next(s.id for s in world.galaxy if s.economy == economy)
    if sid not in world.save.discovered: world.save.discovered.append(sid)
    limits = vr.market_depth_limits(economy, "food")
    quote = {"day": 0, "buy": 12, "sell": 11, "stock": limits["stock"], "demand": limits["demand"]}
    world.save.market_memory[sid] = {"food": quote}
    assert not world.save.market_depth  # Price memory also validates without materialized pools.
    accepted = vr.SaveData.from_dict(world.save.to_dict())
    assert accepted.market_memory[sid]["food"][quantity] == limits[quantity]
    quote[quantity] += 1
    raw = json.dumps(world.save.to_dict()).encode(); (tmp_path / "77.json").write_bytes(raw)
    with pytest.raises(vr.ResumeError, match="remembered " + quantity):
        vr.load_or_create_save(tmp_path, 77, "Tester")
    assert (tmp_path / "77.json").read_bytes() == raw


def _world_with_trade_opportunities():
    world = _world_with_seed(42)
    world.save.ship.hull_class = "Carrier"; world.save.pilot.credits = 100000
    for system in world.galaxy:
        system.discovered = True; world.save.current_system = system.id
        vr.remember_local_market(world)
    world.save.current_system = 0; world.sync_discovered()
    return world


def test_trade_opportunities_are_bounded_affordable_and_use_no_live_remote_quotes(monkeypatch):
    import copy
    world = _world_with_trade_opportunities(); before = copy.deepcopy(world.save.to_dict()); rng = world.event_rng.getstate()
    original = vr.price_for
    def local_only(current, sid, commodity):
        assert sid == current.here.id
        return original(current, sid, commodity)
    monkeypatch.setattr(vr, "price_for", local_only)
    candidates = vr.trade_opportunities(world)
    assert len(candidates) == 6
    scores = [quote["margin"] / len(quote["legs"]) for quote in candidates]
    assert scores == sorted(scores, reverse=True)
    assert all(quote["cash_needed"] <= world.save.pilot.credits and quote["feasible"] and quote["margin"] > 0 for quote in candidates)
    assert all(quote["quantity"] <= world.save.market_memory[quote["destination"]][quote["commodity"]]["demand"] for quote in candidates)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng
    for sid in world.by_id:
        if sid: world.save.market_drift[sid] = {c: 0.6 for c in vr.COMMODITIES}
    assert vr.trade_opportunities(world) == candidates


@pytest.mark.parametrize("constraint", ["empty_memory", "empty_stock", "no_cash", "no_demand"])
def test_trade_opportunities_honor_missing_information_and_resource_limits(constraint):
    world = _world_with_trade_opportunities()
    if constraint == "empty_memory": world.save.market_memory.clear()
    if constraint == "empty_stock": world.save.market_depth[0] = {c: {"day": 0, "stock": 0, "demand": 0} for c in vr.COMMODITIES}
    if constraint == "no_cash": world.save.pilot.credits = 0
    if constraint == "no_demand":
        for quotes in world.save.market_memory.values():
            for quote in quotes.values(): quote["demand"] = 0
    assert vr.trade_opportunities(world) == []


def test_a_full_hold_offers_somewhere_to_sell_rather_than_nothing():
    """Nothing left to buy is not nothing left to plan (issue #415 review)."""
    world = _world_with_trade_opportunities()
    held = vr.cargo_capacity(world.save.ship)  # a hold filled at the local producing price
    _set_cargo(world, {"machinery": held})
    world.save.cargo_basis = {"machinery": [[held, held * vr.price_for(world, 0, "machinery")]]}
    candidates = vr.trade_opportunities(world)
    assert candidates and all(quote["use_hold"] and quote["commodity"] == "machinery" for quote in candidates)
    assert all(quote["quantity"] <= world.save.cargo["machinery"] for quote in candidates)
    assert all(quote["procurement"] == 0 for quote in candidates)  # selling what is aboard buys nothing
    _set_cargo(world, {})
    assert all(not quote["use_hold"] for quote in vr.trade_opportunities(world))


def test_trade_opportunities_reserve_travel_cash_and_exclude_contract_consumption():
    world = _world_with_trade_opportunities(); world.save.pilot.credits = 150; world.save.ship.fuel = 0
    world.save.ship.has_gunner = True
    candidates = vr.trade_opportunities(world)
    assert candidates and all(q["cash_needed"] <= 150 and q["fuel_cash"] > 0 and q["wages"] > 0 for q in candidates)
    first = candidates[0]
    world.save.active_missions = [vr.Mission(id=1, kind="delivery", description="Committed cargo", reward=1,
        origin_system=0, target_system=first["destination"], commodity=first["commodity"], quantity=1)]
    assert all((q["destination"], q["commodity"]) != (first["destination"], first["commodity"]) for q in vr.trade_opportunities(world))


def test_regional_economy_selects_local_bounded_regions_without_new_rng_calls():
    import random
    world = _world_with_seed(42); world.event_rng.seed(31)
    expected = random.Random(31)
    assert expected.random() < vr.ECONOMY_EVENT_CHANCE_PER_TURN
    economy = expected.choice(vr.ECONOMIES)
    expected.choice(sorted(set(vr.ECONOMY_PRODUCES[economy]) | set(vr.ECONOMY_DEMANDS[economy])))
    expected.choice(["crash", "boom"]); expected.randint(vr.ECONOMY_EVENT_MIN_TURNS, vr.ECONOMY_EVENT_MAX_TURNS)
    vr.tick_economy_event(world)
    event = world.save.active_event; ids = event["system_ids"]
    assert 1 <= len(ids) <= 3 and len(ids) == len(set(ids))
    hops = vr.bfs_hops(world.by_id, ids[0])
    assert all(world.by_id[sid].economy == event["economy"] and hops[sid] <= 2 for sid in ids)
    assert world.event_rng.getstate() == expected.getstate()
    assert set(world.save.market_drift) == set(ids)
    assert world.save.market_depth == {} and world.save.market_memory == {}


def test_regional_economy_saved_scope_survives_a_restart(tmp_path):
    world = _world_with_seed(42); world.event_rng.seed(31); vr.tick_economy_event(world)
    ids = list(world.save.active_event["system_ids"]); vr.persist(world, tmp_path, 77)
    restored = vr.World(vr.load_or_create_save(tmp_path, 77, "Tester")[0])
    remaining = restored.save.active_event["turns_remaining"]; rng = restored.event_rng.getstate()
    vr.tick_economy_event(restored)
    assert restored.save.active_event["system_ids"] == ids and restored.save.active_event["turns_remaining"] == remaining - 1
    assert restored.event_rng.getstate() == rng
    # An event with no region was economy-wide under the retired schema (#421).
    without_region = dict(restored.save.active_event); del without_region["system_ids"]
    restored.save.active_event = without_region
    with pytest.raises(vr.ResumeError): vr.SaveData.from_dict(restored.save.to_dict())


@pytest.mark.parametrize("ids", [[], [True], [99], [1, 1], [1, 2, 3, 4], "1", [0]])
def test_regional_economy_rejects_invalid_saved_regions_without_rewriting(tmp_path, ids):
    import json
    world = _world_with_seed(42)
    world.save.active_event = dict(economy="Agricultural", commodity="food", direction="boom", turns_remaining=3, description="News", system_ids=ids)
    raw = json.dumps(world.save.to_dict()).encode(); (tmp_path / "77.json").write_bytes(raw)
    with pytest.raises(vr.ResumeError): vr.load_or_create_save(tmp_path, 77, "Tester")
    assert (tmp_path / "77.json").read_bytes() == raw


def test_regional_economy_bulletin_names_targets_without_charting_or_revealing_other_threats():
    import copy
    world = _world_with_seed(42)
    anchor = next(s for s in world.galaxy if not s.discovered)
    world.save.active_event = dict(economy=anchor.economy, commodity="food", direction="boom", turns_remaining=1,
        description="Regional shortage", system_ids=[anchor.id])
    before = copy.deepcopy(world.save.to_dict()); rng = world.event_rng.getstate()
    text = " ".join(vr.economy_opportunity_lines(world, []))
    assert anchor.name in text and "danger unknown" in text and "event ends by arrival" in text
    assert "Lead: bring Food" in text and not world.save.market_memory
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


def test_regional_economy_market_tags_only_affected_stations_and_preserves_legality(monkeypatch):
    world = _world_with_seed(0)
    havens = [s for s in world.galaxy if s.economy == "Haven"]
    assert len(havens) > 1
    world.save.active_event = dict(economy="Haven", commodity="weapons", direction="boom", turns_remaining=3,
        description="Regional shortage", system_ids=[havens[0].id])
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    for index, station in enumerate(havens[:2]):
        world.save.current_system = station.id
        with contextlib.redirect_stdout(io.StringIO()) as output: vr.screen_market(vr.Palette(False), world)
        text = output.getvalue()
        assert "Illegal" in text and ("BOOM" in text) == (index == 0)


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_economy_opportunity_pages_reach_all_candidates_within_terminal_size(monkeypatch, terminal, width, height):
    import copy, re
    world = _world_with_trade_opportunities(); world.event_rng.seed(31); vr.tick_economy_event(world)
    before = copy.deepcopy(world.save.to_dict()); rng = world.event_rng.getstate()
    terminal(width, height)
    output = io.StringIO(); frames = []
    def choose():
        frame = output.getvalue(); frames.append(frame); output.seek(0); output.truncate(0)
        match = re.search(r"Opportunities (\d+)/(\d+)", page_title(frame))
        assert match and len(frames) < 300
        return "B" if match[1] == match[2] else "N"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output): vr.screen_economy_opportunities(vr.Palette(False), world)
    assert "[6]" in " ".join(frames)
    assert all(len(frame.splitlines()) <= height for frame in frames)
    assert all(vr._visible_width(line) <= width for frame in frames for line in frame.splitlines())
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


def test_trade_opportunity_selection_opens_the_matching_route_without_purchase(monkeypatch):
    import copy
    world = _world_with_trade_opportunities(); first = vr.trade_opportunities(world)[0]
    before = copy.deepcopy(world.save.to_dict()); selected=[]
    monkeypatch.setattr(vr, "screen_trade_route", lambda p, current, **kw: selected.append(kw["initial"]))
    keys = iter("1B"); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()): vr.screen_economy_opportunities(vr.Palette(False), world)
    assert selected == [{key: first[key] for key in ("destination", "commodity", "quantity", "use_hold")}]
    assert world.save.to_dict() == before


@pytest.mark.parametrize("commands", [b"TOBBQ", b"TO", b"TO1BBBQ"])
def test_real_economy_opportunity_back_eof_and_route_selection_leave_career_unchanged(tmp_path, commands):
    import json, os, subprocess
    world = _world_with_trade_opportunities()
    world.save.pilot.credits = 2500
    assert vr.trade_opportunities(world)
    world.event_rng.seed(31); vr.tick_economy_event(world)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77); world.checkpoint()
    original = (tmp_path / "77.json").read_bytes()
    info = tmp_path / "door_info.json"; info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=commands, capture_output=True,
        env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)), timeout=10)
    assert result.returncode == 0 and not result.stderr and shows_page(result.stdout, "Opportunities")
    if b"1" in commands: assert shows_page(result.stdout, "Trade Route")
    assert (tmp_path / "77.json").read_bytes() == original


def test_real_regional_news_is_saved_before_announcement_and_survives_kill(tmp_path):
    world = _world_with_seed(42); world.event_rng.seed(31)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77); world.checkpoint()
    with _door_stopped_at(tmp_path, b"C" + vr.CHART_CONNECTION_LETTERS[0].encode() + b"Y", b"Galaxy news:"):
        saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert saved.active_event and 1 <= len(saved.active_event["system_ids"]) <= 3
        event = dict(saved.active_event)
    restored = vr.World(vr.load_or_create_save(tmp_path, 77, "Tester")[0])
    assert restored.save.active_event == event


def test_regional_economy_absent_industry_falls_back_without_extra_random_choices(monkeypatch):
    world = _world_with_seed(1)
    assert not any(s.economy == "Haven" for s in world.galaxy)
    choices = iter(["Haven", "narcotics", "boom"])
    monkeypatch.setattr(world.event_rng, "random", lambda: 0)
    monkeypatch.setattr(world.event_rng, "choice", lambda seq: next(choices))
    monkeypatch.setattr(world.event_rng, "randint", lambda a, b: a)
    assert vr.tick_economy_event(world)
    event = world.save.active_event
    assert event["economy"] == world.here.economy and event["system_ids"]
    assert event["commodity"] in set(vr.ECONOMY_PRODUCES[event["economy"]]) | set(vr.ECONOMY_DEMANDS[event["economy"]])
    with pytest.raises(StopIteration): next(choices)


def test_regional_economy_rejects_distant_stations_even_with_matching_economy():
    world = _world_with_seed(0)
    pairs = [(a, b) for a in world.galaxy for b in world.galaxy
             if a.economy == b.economy and vr.bfs_hops(world.by_id, a.id)[b.id] > 2]
    assert pairs
    a, b = pairs[0]
    world.save.active_event = dict(economy=a.economy, commodity="food", direction="boom", turns_remaining=3,
        description="News", system_ids=[a.id, b.id])
    with pytest.raises(vr.ResumeError, match="event region"): vr.SaveData.from_dict(world.save.to_dict())


def test_trade_opportunities_label_legacy_unobserved_demand_without_inventing_it():
    import copy
    world = _world_with_trade_opportunities()
    for quotes in world.save.market_memory.values():
        for quote in quotes.values(): quote.pop("stock"); quote.pop("demand")
    before = copy.deepcopy(world.save.to_dict())
    candidates = vr.trade_opportunities(world)
    assert candidates and all(q["observed_demand"] is None for q in candidates)
    assert "capacity unobserved" in " ".join(vr.economy_opportunity_lines(world, candidates))
    assert world.save.to_dict() == before


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

    keys=iter("CB"); monkeypatch.setattr(vr,"read_key",lambda:next(keys))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr._hull_refit_screen(vr.Palette(truecolor=False), world, "Freighter", 15_000)

    assert world.save.ship.hull_class == "Shuttle"
    assert world.save.pilot.credits == 100
    assert "Need" in buf.getvalue()


def test_hull_refit_screen_declining_confirmation_makes_no_change(monkeypatch):
    world = _world_with_seed(31)
    world.save.pilot.credits = 20_000

    keys=iter("CNB"); monkeypatch.setattr(vr,"read_key",lambda:next(keys))
    with contextlib.redirect_stdout(io.StringIO()):
        vr._hull_refit_screen(vr.Palette(truecolor=False), world, "Freighter", 15_000)

    assert world.save.ship.hull_class == "Shuttle"
    assert world.save.pilot.credits == 20_000


def test_hull_refit_screen_applies_the_refit_charges_credits_and_resets_hull_to_new_max(monkeypatch):
    world = _world_with_seed(32)
    world.save.pilot.credits = 20_000
    world.save.ship.hull_hp = 10  # damaged, below Shuttle's own max
    world.checkpoint()  # The existing 20,000cr balance has already been earned.

    keys=iter("CY"); monkeypatch.setattr(vr,"read_key",lambda:next(keys))
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

    keys=iter("CY"); monkeypatch.setattr(vr,"read_key",lambda:next(keys))
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

    keys = iter([vr.YARD_LETTERS[len(vr.UPGRADES)], "C", "Y", "Q"])  # first refit slot (Freighter), commission, confirm, leave
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_shipyard(vr.Palette(truecolor=False), world)

    text = plain(buf.getvalue())
    assert "[G]" in text and "Freighter-Class" in text
    assert "[H]" in text and "Cutter-Class" in text
    assert world.save.ship.hull_class == "Freighter"


def _some_contraband_commodity():
    return vr.CONTRABAND_COMMODITIES[0]


def test_has_contraband_false_for_an_empty_or_legal_only_hold():
    world = _world_with_seed(124)
    assert not vr.has_contraband(world)
    _add_cargo(world, "food", 5)
    assert not vr.has_contraband(world)


def test_has_contraband_true_once_any_illegal_good_is_in_cargo():
    world = _world_with_seed(125)
    _add_cargo(world, _some_contraband_commodity(), 3)
    assert vr.has_contraband(world)


def test_dump_all_contraband_clears_only_illegal_goods():
    world = _world_with_seed(126)
    contraband = _some_contraband_commodity()
    _add_cargo(world, contraband, 7)
    _add_cargo(world, "food", 4)

    msg = vr.dump_all_contraband(world)

    assert contraband not in world.save.cargo
    assert world.save.cargo["food"] == 4
    assert "7" in msg


def test_dump_all_contraband_grants_no_credits():
    world = _world_with_seed(127)
    _add_cargo(world, _some_contraband_commodity(), 10)
    before = world.save.pilot.credits

    vr.dump_all_contraband(world)

    assert world.save.pilot.credits == before


def test_screen_dump_contraband_does_nothing_without_contraband(monkeypatch):
    world = _world_with_seed(128)
    monkeypatch.setattr(vr, "read_key", lambda: (_ for _ in ()).throw(AssertionError("should not prompt")))

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_dump_contraband(vr.Palette(truecolor=False), world)


def test_screen_dump_contraband_declines_without_confirmation(monkeypatch):
    world = _world_with_seed(129)
    contraband = _some_contraband_commodity()
    _add_cargo(world, contraband, 5)
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: False)

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_dump_contraband(vr.Palette(truecolor=False), world)

    assert world.save.cargo[contraband] == 5


def test_screen_dump_contraband_clears_cargo_on_confirmation(monkeypatch):
    world = _world_with_seed(130)
    contraband = _some_contraband_commodity()
    _add_cargo(world, contraband, 5)
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_dump_contraband(vr.Palette(truecolor=False), world)

    assert contraband not in world.save.cargo


def test_station_menu_offers_dump_only_with_contraband_aboard(monkeypatch):
    world = _world_with_seed(131)
    monkeypatch.setattr(vr, "read_key", lambda: "Q")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)
    assert "[D]" not in buf.getvalue()

    _add_cargo(world, _some_contraband_commodity(), 2)
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)
    assert "[D]" in buf2.getvalue()


def test_save_from_dict_defaults_active_event_to_none_for_old_saves():
    save = vr._new_career("Legacy")
    d = save.to_dict()
    del d["active_event"]

    loaded = vr.SaveData.from_dict(d)

    assert loaded.active_event is None


def test_tick_economy_event_does_not_start_one_when_roll_fails():
    world = _world_with_seed(162)
    world.event_rng.random = lambda: 1.0  # always fails the trigger roll

    msg = vr.tick_economy_event(world)

    assert msg is None
    assert world.save.active_event is None


def test_tick_economy_event_starts_one_and_applies_drift_immediately(monkeypatch):
    world = _world_with_seed(163)
    world.event_rng.random = lambda: 0.0  # always triggers
    world.event_rng.choice = lambda seq: seq[0]
    world.event_rng.randint = lambda a, b: a

    msg = vr.tick_economy_event(world)

    assert msg is not None
    assert world.save.active_event is not None
    event = world.save.active_event
    affected = [world.by_id[sid] for sid in event["system_ids"]]
    assert 1 <= len(affected) <= 3
    assert all(system.economy == event["economy"] for system in affected)
    assert set(world.save.market_drift) == set(event["system_ids"])
    for system in affected:
        assert world.save.market_drift[system.id][event["commodity"]] in (
            vr.ECONOMY_EVENT_CRASH_LEVEL, vr.ECONOMY_EVENT_BOOM_LEVEL)


def test_tick_economy_event_reasserts_drift_level_each_turn_while_active():
    world = _world_with_seed(164)
    system = next(s for s in world.galaxy if s.economy == "Agricultural")
    world.save.active_event = {
        "economy": "Agricultural", "commodity": "food", "direction": "crash",
        "turns_remaining": 3, "description": "Food prices crash across every Agricultural system",
        "system_ids": [system.id],
    }

    vr.tick_economy_event(world)
    world.save.market_drift[system.id]["food"] = 1.0  # simulate reversion trying to pull it back
    vr.tick_economy_event(world)

    assert world.save.market_drift[system.id]["food"] == vr.ECONOMY_EVENT_CRASH_LEVEL


def test_tick_economy_event_counts_down_and_ends():
    world = _world_with_seed(165)
    world.save.active_event = {
        "economy": "Agricultural", "commodity": "food", "direction": "crash",
        "turns_remaining": 1, "description": "Food prices crash across every Agricultural system",
        "system_ids": [next(s.id for s in world.galaxy if s.economy == "Agricultural")],
    }

    msg = vr.tick_economy_event(world)

    assert msg is not None
    assert "ended" in msg
    assert world.save.active_event is None


def test_tick_economy_event_never_starts_a_second_one_while_active():
    world = _world_with_seed(166)
    world.save.active_event = {
        "economy": "Mining", "commodity": "ore", "direction": "boom",
        "turns_remaining": 5, "description": "Raw Ore prices spike across every Mining system",
        "system_ids": [next(s.id for s in world.galaxy if s.economy == "Mining")],
    }
    world.event_rng.random = lambda: 0.0  # would otherwise always trigger a new one

    vr.tick_economy_event(world)

    assert world.save.active_event["economy"] == "Mining"
    assert world.save.active_event["turns_remaining"] == 4


def test_screen_market_tags_the_affected_commodity(monkeypatch):
    world = _world_with_seed(167)
    system = world.here
    system.economy = "Agricultural"
    world.save.active_event = {
        "economy": "Agricultural", "commodity": "food", "direction": "crash",
        "turns_remaining": 5, "description": "Food prices crash across every Agricultural system",
        "system_ids": [system.id],
    }
    monkeypatch.setattr(vr, "read_key", lambda: "Q")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_market(vr.Palette(truecolor=False), world)

    assert "CRASH" in buf.getvalue()


def test_screen_status_shows_active_economy_event(monkeypatch):
    world = _world_with_seed(168)
    world.save.active_event = {
        "economy": "Tech", "commodity": "electronics", "direction": "boom",
        "turns_remaining": 4, "description": "Electronics prices spike across every Tech system",
    }
    monkeypatch.setattr(vr, "read_key", lambda: "B")  # whitespace is absorbed at the prompt (#416)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_status(vr.Palette(truecolor=False), world)

    visible = vr._ANSI_RE.sub("", buf.getvalue())
    normalized = " ".join(visible.replace("│", " ").split())
    assert "Economy event" in normalized
    assert "4 day(s) left" in normalized


def test_retiring_resets_active_economy_event():
    old_save = vr._new_career("Vet")
    old_save.active_event = {
        "economy": "Haven", "commodity": "weapons", "direction": "crash",
        "turns_remaining": 5, "description": "Weapons prices crash across every Haven system",
    }

    new_save = vr.retire_pilot(old_save)

    assert new_save.active_event is None


def test_save_from_dict_defaults_futures_fields_for_old_saves():
    save = vr._new_career("Legacy")
    d = save.to_dict()
    del d["active_futures"], d["next_futures_id"]

    loaded = vr.SaveData.from_dict(d)

    assert loaded.active_futures == []
    assert loaded.next_futures_id == 1


def test_buy_futures_contract_charges_the_premium_and_locks_the_price():
    world = _world_with_seed(169)
    spot = vr.price_for(world, world.save.current_system, "food")
    before_credits = world.save.pilot.credits

    vr.buy_futures_contract(world, "food", 5, 10)

    expected_unit = spot + max(1, (spot * 8 + 99) // 100)
    assert len(world.save.active_futures) == 1
    contract = world.save.active_futures[0]
    assert contract.commodity == "food"
    assert contract.quantity == 5
    assert contract.locked_price == expected_unit * 5
    assert contract.settle_turn == world.save.turn + 10
    assert world.save.pilot.credits == before_credits - expected_unit * 5


def test_buy_futures_contract_increments_the_id_counter():
    world = _world_with_seed(170)
    vr.buy_futures_contract(world, "food", 1, 5)
    vr.buy_futures_contract(world, "textiles", 1, 5)

    ids = [c.id for c in world.save.active_futures]
    assert len(set(ids)) == 2
    assert world.save.next_futures_id == 3


def test_settle_futures_contracts_does_nothing_before_settle_turn():
    world = _world_with_seed(171)
    vr.buy_futures_contract(world, "food", 3, 10)

    messages = vr.settle_futures_contracts(world)

    assert messages == []
    assert len(world.save.active_futures) == 1


def test_settle_futures_contracts_delivers_to_cargo_when_due():
    world = _world_with_seed(172)
    vr.buy_futures_contract(world, "food", 3, 5)
    world.save.turn += 5

    messages = vr.settle_futures_contracts(world)

    assert len(messages) == 1
    assert "delivered" in messages[0]
    assert world.save.cargo.get("food") == 3
    assert world.save.active_futures == []


def test_a_full_hold_makes_ready_goods_wait_without_refund_or_lost_fee():
    world = _world_with_seed(173)
    cap = vr.cargo_capacity(world.save.ship)
    world.save.active_futures = [vr.FuturesContract(1, "food", cap, 200, 5, 0, 180, cap)]
    vr._acquire_cargo(world, "textiles", cap, 100)  # the hold is full of something else
    before_credits = world.save.pilot.credits
    world.save.turn += 5

    assert vr.settle_futures_contracts(world) == []
    assert "food" not in world.save.cargo
    assert world.save.pilot.credits == before_credits
    assert len(world.save.active_futures) == 1


def test_an_order_settles_only_at_the_station_that_holds_it():
    world = _world_with_seed(174)
    world.save.active_futures = [vr.FuturesContract(1, "food", 2, 26, 5, 0, 24, 2)]
    world.save.current_system = world.by_id[0].connections[0]  # moved away before settlement
    world.save.turn += 5

    assert vr.settle_futures_contracts(world) == []
    assert "food" not in world.save.cargo

    world.save.current_system = 0
    assert len(vr.settle_futures_contracts(world)) == 1
    assert world.save.cargo.get("food") == 2


def test_screen_market_offers_futures_exchange(monkeypatch):
    world = _world_with_seed(175)
    monkeypatch.setattr(vr, "read_key", lambda: "Q")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_market(vr.Palette(truecolor=False), world)

    assert "[X]" in buf.getvalue()


def test_screen_futures_lists_tradeable_goods_and_outstanding_contracts(monkeypatch):
    world = _world_with_seed(176)
    vr.buy_futures_contract(world, "food", 2, 5)
    monkeypatch.setattr(vr, "read_key", lambda: "Q")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_futures(vr.Palette(truecolor=False), world, vr.LEGAL_COMMODITIES)

    text = buf.getvalue()
    assert "Food" in text
    assert "Outstanding orders" in text


@pytest.mark.parametrize("width,height",[(40,12),(80,24)])
def test_futures_picker_pages_keep_terms_choices_and_return_position(monkeypatch, terminal,width,height):
    import re
    terminal(width, height)
    world=_world_with_seed(42);world.save.pilot.credits=10_000
    for commodity in ["food","textiles","medicine"]:vr.buy_futures_contract(world,commodity,1,5)
    before=world.save.to_dict();output=io.StringIO();frames=[];opened=[];last_page=None
    def inspect(p,w,contract):opened.append(contract.id)
    monkeypatch.setattr(vr,"_screen_futures_order",inspect)
    def choose():
        nonlocal last_page
        frame=output.getvalue();output.seek(0);output.truncate(0);frames.append(frame)
        assert len(frame.splitlines())<=height
        assert all(vr._visible_width(line)<=width for line in frame.splitlines())
        assert "[B]" in frame
        page,count=map(int,re.search(r"(\d+)/(\d+)",frame).groups())
        if opened:
            assert page==last_page
            return "B"
        if page==count:
            last_page=page
            return max(re.findall(r"\[(\d)\]",frame))
        return "N"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):vr.screen_futures(vr.Palette(False),world,vr.LEGAL_COMMODITIES)
    assert opened==[world.save.active_futures[-1].id]
    text=page_text(frames)
    assert "8%" in text and "Outstanding orders" in text
    assert world.save.to_dict()==before


@pytest.mark.parametrize("width,height",[(40,12),(80,24)])
@pytest.mark.parametrize("kind",["draft","order"])
def test_futures_draft_and_order_pages_fit_and_preserve_all_terms(monkeypatch, terminal,width,height,kind):
    import re
    terminal(width, height)
    world=_world_with_seed(42);vr.buy_futures_contract(world,"food",2,5)
    contract=world.save.active_futures[0]
    before=world.save.to_dict();frames=[];output=io.StringIO()
    def choose():
        frame=vr._ANSI_RE.sub("",output.getvalue());output.seek(0);output.truncate(0);frames.append(frame)
        assert len(frame.splitlines())<=height
        assert all(vr._visible_width(line)<=width for line in frame.splitlines())
        assert "[B] Back:" in page_text(frame)
        page,count=map(int,re.search(r"(\d+)/(\d+)",frame).groups())
        return "B" if page==count else ">"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):
        if kind=="draft":vr._screen_buy_futures(vr.Palette(False),world,"food")
        else:vr._screen_futures_order(vr.Palette(False),world,contract)
    text=page_text(frames)
    assert "nonrefundable" in text and "Pickup:" in text
    assert world.save.to_dict()==before


def test_futures_invalid_quantity_retains_draft_without_saving(monkeypatch):
    world=_world_with_seed(42);before=world.save.to_dict()
    keys=iter(["U","U","B"]);values=iter(["3","not a number"])
    monkeypatch.setattr(vr,"read_key",lambda:next(keys));monkeypatch.setattr(vr,"read_line_raw",lambda **kw:next(values))
    with contextlib.redirect_stdout(io.StringIO()) as output:vr._screen_buy_futures(vr.Palette(False),world,"food")
    text=plain(output.getvalue())
    assert "Quantity must fit" in text and text.count("Quantity: 3;")==2
    assert world.save.to_dict()==before


@pytest.mark.parametrize("commands",[b"MX1>U3\rTTBBQ",b"MX1>",b"MX1U\rBBQ",b"MX1SNBBQ"])
def test_real_responsive_futures_draft_back_cancel_and_eof_preserve_career(tmp_path,commands):
    import json,os,subprocess
    world=_world_with_seed(42)
    world._checkpoint=lambda current:vr.persist(current,tmp_path,77);world.checkpoint()
    before=(tmp_path/"77.json").read_bytes()
    info=tmp_path/"door_info.json";info.write_text(json.dumps({"user_id":77,"handle":"Tester","terminal_width":40,"terminal_height":12}),encoding="utf-8")
    result=subprocess.run([sys.executable,str(_VOIDRUNNER_PATH)],input=commands,capture_output=True,
        env=dict(os.environ,VOIDRUNNER_SAVE_DIR=str(tmp_path),NETBBS_DOOR_INFO=str(info)),timeout=10)
    assert result.returncode==0 and not result.stderr and b"Order Food:" in result.stdout
    if b"SN" in commands:assert b"Signing cancelled" in result.stdout
    assert (tmp_path/"77.json").read_bytes()==before


@pytest.mark.parametrize("action",["sign","cancel"])
def test_retained_futures_result_is_saved_before_disconnect(tmp_path,action):
    world=_world_with_seed(42)
    if action=="cancel":vr.buy_futures_contract(world,"food",2,5)
    world._checkpoint=lambda current:vr.persist(current,tmp_path,77);world.checkpoint()
    command=b"MX1SY" if action=="sign" else b"MXN4XY"
    marker=b"Result: Futures contract:" if action=="sign" else b"Result: Order cancelled:"
    with _door_stopped_at(tmp_path,command,marker):
        saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
        assert bool(saved.active_futures)==(action=="sign")
        assert saved.pilot.credits!=world.save.pilot.credits


@pytest.mark.parametrize("count",[0,1,3])
@pytest.mark.parametrize("notice_count",[0,30])
def test_picker_footer_only_offers_present_choices(monkeypatch, terminal,count,notice_count):
    import re
    terminal(80, 24)
    output=io.StringIO();frames=[]
    def choose():
        frame=plain(output.getvalue());output.seek(0);output.truncate(0);frames.append(frame)
        choices=[line for line in frame.splitlines() if re.match(r"\[\d\] Item",line)]
        if not choices:assert "Select" not in frame
        else:
            span="[1]" if len(choices)==1 else f"[1-{len(choices)}]"
            assert span+" Select" in frame
            assert "[1-4] Select" not in frame
        page,total=map(int,re.search(r"(\d+)/(\d+)",frame).groups())
        return "B" if page==total else "N"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):
        assert vr._pick_trade_field("Picker",[(i,f"Item {i}") for i in range(count)],max_choices=4,notice=["Notice"]*notice_count) is None
    assert frames


def test_buy_futures_rejects_invalid_duration_without_mutation():
    world = _world_with_seed(177)
    before = world.save.pilot.credits
    with pytest.raises(vr.TradeError, match="term"):
        vr.buy_futures_contract(world, "food", 10, 7)
    assert world.save.pilot.credits == before
    assert not world.save.active_futures


def test_screen_buy_futures_creates_a_contract_on_confirmation(monkeypatch):
    world = _world_with_seed(178)
    world.save.pilot.credits = 100_000
    keys = iter("US")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: "10")
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)
    monkeypatch.setattr(vr, "pause", lambda p: None)
    with contextlib.redirect_stdout(io.StringIO()):
        vr._screen_buy_futures(vr.Palette(False), world, "food")
    assert len(world.save.active_futures) == 1
    assert world.save.active_futures[0].quantity == 10


def test_screen_buy_futures_declines_on_confirmation_refusal(monkeypatch):
    world = _world_with_seed(179)
    keys = iter("SB")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: False)
    with contextlib.redirect_stdout(io.StringIO()):
        vr._screen_buy_futures(vr.Palette(False), world, "food")
    assert not world.save.active_futures


def test_retiring_resets_futures_contracts():
    old_save = vr._new_career("Vet")
    vr.buy_futures_contract(vr.World(old_save), "food", 2, 5)

    new_save = vr.retire_pilot(old_save)

    assert new_save.active_futures == []


# Issue #310: exercise the real executable and kill it while nested menus
# still own input. Save-on-menu-exit and graceful-EOF tests miss this boundary.
@pytest.mark.parametrize(
    "commands,ack,field,expected",
    [
        (b"MAP1\r", b"Bought 1x Food", "cargo.food", 1),
        (b"YAY", b"Cargo Bay Expansion upgraded", "ship.cargo_tier", 1),
        (b"YKAY", b"Gunner hired", "ship.has_gunner", True),
        (b"YR2\r", b"Refueled 2 units", "ship.fuel", 22),
        (b"YPY", b"Hull repaired", "ship.hull_hp", 60),
        (b"MX1SY", b"Futures contract: 1x Food", "active_futures", "nonempty"),
        (b"B1NNNNNNNNA", b"Accepted:", "active_missions", "nonempty"),
        (b"DY", b"Jettisoned 1 units", "cargo", {}),
        (b"PJY", b"Commission accepted", "pilot.has_concord_commission", True),
        (b"WJY", b"Welcome to the family", "pilot.has_blackwake_made", True),
    ],
)
def test_acknowledged_station_action_survives_forced_termination(
    tmp_path, commands, ack, field, expected,
):
    import json
    import os
    import subprocess
    import threading

    world = _world_with_seed(42)
    world.save.pilot.credits = 20_000
    world.save.pilot.highest_rank_seen = 2
    world.save.ship.fuel = 20
    world.save.ship.hull_hp = 50
    world.save.pilot.reputation = {f: 75 for f in vr.FACTIONS}
    if commands == b"DY":
        _set_cargo(world, {"weapons": 1})
    vr.write_save(tmp_path, 77, world.save)
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}), encoding="utf-8")
    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info))
    proc = subprocess.Popen(
        [sys.executable, str(_VOIDRUNNER_PATH)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    reached = threading.Event()
    output = bytearray()

    reader = threading.Thread(target=_drain_until, args=(proc.stdout, output, ack, reached))
    reader.start()
    try:
        # A list is written with a real gap between parts; see `_door_stopped_at`.
        parts = commands if isinstance(commands, list) else [commands]
        for index, part in enumerate(parts):
            if index:
                time.sleep(max(0.25, vr._INPUT_TIMEOUT * 5))
            proc.stdin.write(part)
            proc.stdin.flush()
        assert reached.wait(10), bytes(output).decode("utf-8", errors="replace")
        # Deliberately no menu-exit input, EOF or graceful quit.
        proc.kill()
        proc.wait(timeout=5)
        saved = json.loads((tmp_path / "77.json").read_text(encoding="utf-8"))
        value = saved
        for part in field.split("."):
            value = value[part]
        if expected == "nonempty":
            assert value
        else:
            assert value == expected
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        reader.join(timeout=5)
        assert not reader.is_alive()
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()


def test_a_refused_career_is_not_replaced_when_it_cannot_be_retained(tmp_path):
    import json

    old = json.dumps(dict(_world_with_seed(42).save.to_dict(), schema_version=1)).encode()
    (tmp_path / "77.json").write_bytes(old)
    for index in range(vr.MAX_RECOVERY_COPIES):
        (tmp_path / f"77.recovery-{index}.json").write_bytes(b"{}")

    with pytest.raises(vr.SaveError, match="Recovery copies are full"):
        vr.replace_unsupported_career(tmp_path, 77, vr._new_career("Tester"))

    assert (tmp_path / "77.json").read_bytes() == old


def test_fragmented_sequences_survive_timeout_without_command_suffixes():
    events = iter([b"\x1b", None, b"[", b"1", None, b";", b"5", b"A", b"Z"])
    timeouts = []

    def read(timeout):
        timeouts.append(timeout)
        return next(events)

    reader = vr._DoorInput(read)
    # The introducer arrives inside the reserved second timeout, so the fragmented
    # arrow is absorbed whole and never reported as a lone Escape (issue #413 review):
    # acting on that sentinel now cancels a field, so it must not name a partial key.
    assert reader.read_key() == vr.IGNORED_KEY
    assert reader.read_key() == vr.IGNORED_KEY
    assert reader.read_key() == "Z"
    # Once a partial key has timed out, wait for actual data instead of causing
    # an endless menu redraw every timeout interval.
    assert timeouts == [None, vr._INPUT_TIMEOUT, vr._INPUT_TIMEOUT, None,
                        vr._INPUT_TIMEOUT, None, vr._INPUT_TIMEOUT,
                        vr._INPUT_TIMEOUT, None]


def test_a_late_arrow_suffix_cannot_cancel_what_the_caller_was_typing():
    """A slow link fragments an arrow key; Escape cancels fields, so the sentinel
    must not be reported until the reserved introducer window has passed (#413)."""
    events = iter([b"\x1b", None, b"O", b"B", b"7"])
    reader = vr._DoorInput(lambda timeout: next(events))
    assert reader.read_key() == vr.IGNORED_KEY  # the whole delayed arrow, not an Escape
    assert reader.read_key() == "7"
    events = iter([b"\x1b", None, None, b"7"])
    reader = vr._DoorInput(lambda timeout: next(events))
    assert reader.read_key() == vr.ESCAPE_KEY  # nothing followed: a real, deliberate Escape
    assert reader.read_key() == "7"


def test_lone_escape_does_not_swallow_next_deliberate_command():
    events = iter([b"\x1b", None, b"Q"])
    reader = vr._DoorInput(lambda timeout: next(events))
    assert reader.read_key() == vr.ESCAPE_KEY
    assert reader.read_key() == "Q"


@pytest.mark.parametrize("bad", [b"\xff", b"\xc0\xaf", b"\xe2", b"\xed\xa0\x80"])
def test_malformed_utf8_does_not_lose_following_character(bad):
    stream = io.BytesIO(bad + b"Z")
    reader = vr._DoorInput(lambda timeout: stream.read(1))
    result = []
    while True:
        try:
            key = reader.read_key()
        except EOFError:
            break
        if key != vr.IGNORED_KEY:
            result.append(key)
    assert result == ["Z"]


def test_real_door_accepts_utf8_name_and_coalesces_crlf(tmp_path):
    import json
    import os
    import subprocess

    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path))
    env.pop("NETBBS_DOOR_INFO", None)
    result = subprocess.run(
        [sys.executable, str(_VOIDRUNNER_PATH)],
        input="界e\u0301\x7fJörg\r\nYQ".encode("utf-8"),
        capture_output=True, env=env, timeout=10,
    )
    assert result.returncode == 0
    assert not result.stderr
    saves = [p for p in tmp_path.glob("*.json") if p.name != "leaderboard.json"]
    assert len(saves) == 1
    assert json.loads(saves[0].read_text(encoding="utf-8"))["pilot"]["handle"] == "界Jörg"


@pytest.mark.parametrize("partial", [b"\x1b", b"\x1b[1;", b"\xc3", b"\x1b]Y", b"\x1b[200~Y"])
def test_real_pipe_eof_in_partial_key_never_launches_career(tmp_path, partial):
    import os
    import subprocess

    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path))
    env.pop("NETBBS_DOOR_INFO", None)
    result = subprocess.run(
        [sys.executable, str(_VOIDRUNNER_PATH)], input=partial,
        capture_output=True, env=env, timeout=10,
    )
    assert result.returncode == 0
    assert not result.stderr
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("prefix", [b"\x1bP", b"\x1bX", b"\x1b^", b"\x1b_", b"\x90", b"\x98", b"\x9e", b"\x9f"])
def test_st_only_control_strings_do_not_leak_keys_after_bel(prefix):
    stream = io.BytesIO(prefix + b"data\x07YBUY\x1b\\Z")
    reader = vr._DoorInput(lambda timeout: stream.read(1))
    assert reader.read_key() == vr.IGNORED_KEY
    assert reader.read_key() == "Z"


@pytest.mark.parametrize("start", [b"\x1b[200~", b"\x9b200~"])
@pytest.mark.parametrize("end", [b"\x1b[201~", b"\x9b201~"])
def test_paste_accepts_both_csi_terminators(start, end):
    stream = io.BytesIO(start + b"BUY" + end + b"Q")
    reader = vr._DoorInput(lambda timeout: stream.read(1))
    assert reader.read_key() == vr.IGNORED_KEY
    assert reader.read_key() == "Q"


def test_fragmented_escape_cannot_dismiss_result_pause(monkeypatch):
    events = iter([b"\x1b", None, b"[", b"A", b"K", b"N"])
    reader = vr._DoorInput(lambda timeout: next(events))
    monkeypatch.setattr(vr, "read_key", reader.read_key)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.pause(vr.Palette(False))
    assert reader.read_key() == "N"


def test_control_string_started_before_timeout_retains_its_payload():
    events = iter([b"\x1b", b"P", None, b"Y", b"\x1b", b"\\", b"Q"])
    reader = vr._DoorInput(lambda timeout: next(events))
    assert reader.read_key() == vr.IGNORED_KEY
    assert reader.read_key() == vr.IGNORED_KEY
    assert reader.read_key() == "Q"


def test_active_limit_rejects_without_consuming_posted_offer():
    import copy

    world = _world_with_seed(42)
    offered = vr.generate_mission_board(world)[0]
    world.save.active_missions = [
        vr.Mission(100 + i, "bounty", "Existing", 500, 0, 1, pirate_tier=1)
        for i in range(vr.MAX_ACTIVE_MISSIONS)
    ]
    before = copy.deepcopy(world.save.to_dict())
    with pytest.raises(vr.MissionError, match="at most"):
        vr.accept_mission(world, offered)
    assert world.save.to_dict() == before


def test_acceptance_rejects_unposted_or_altered_terms():
    world = _world_with_seed(42)
    invented = vr.Mission(1, "bounty", "Invented", 900, 0, 1, pirate_tier=1)
    with pytest.raises(vr.MissionError, match="no longer posted"):
        vr.accept_mission(world, invented)
    offer = vr.generate_mission_board(world)[0]
    original_reward = offer.reward
    offer.reward += 1
    with pytest.raises(vr.MissionError, match="no longer posted"):
        vr.accept_mission(world, offer)
    assert vr.generate_mission_board(world)[0].reward == original_reward


def test_a_new_offer_never_reuses_an_id_the_career_already_holds():
    world = _world_with_seed(42)
    world.save.active_missions = [vr.Mission(500, "scan", "Held", 500, 0, 1)]
    world.save.next_mission_id = 1  # a counter left behind the ids it hands out
    offers = vr.generate_mission_board(world)
    assert offers and all(offer.id > 500 for offer in offers)


def test_repeated_one_unit_contraband_recycling_cannot_unlock_membership(monkeypatch):
    world = _world_with_seed(42)
    world.here.economy = "Haven"
    world.save.pilot.credits = 10000
    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: "1")
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(38):
            monkeypatch.setattr(vr, "read_key", lambda: "P")
            vr._trade_commodity(vr.Palette(False), world, "weapons")
            monkeypatch.setattr(vr, "read_key", lambda: "S")
            vr._trade_commodity(vr.Palette(False), world, "weapons")
    assert world.save.pilot.credits < 10000
    assert world.save.pilot.reputation[vr.FACTION_BLACKWAKE] == 0
    assert not vr.blackwake_made_available(world)
    assert world.save.contraband_trade_balance < 0


def test_trade_milestones_survive_splitting_losses_and_restart(tmp_path):
    whole, split = _world_with_seed(42), _world_with_seed(42)
    vr.record_contraband_trade(whole, "weapons", -200)
    vr.record_contraband_trade(whole, "weapons", 1700)
    for _ in range(200):
        vr.record_contraband_trade(split, "weapons", -1)
    for _ in range(1700):
        vr.record_contraband_trade(split, "weapons", 1)
    assert whole.save.pilot.reputation == split.save.pilot.reputation
    earned = 1500 // vr.CONTRABAND_STANDING_STEP
    assert split.save.pilot.reputation[vr.FACTION_BLACKWAKE] == earned
    vr.persist(split, tmp_path, 77)
    save, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    split = vr.World(save)
    vr.record_contraband_trade(split, "weapons", -1000)
    vr.record_contraband_trade(split, "weapons", 1000)
    assert split.save.pilot.reputation[vr.FACTION_BLACKWAKE] == earned
    vr.record_contraband_trade(split, "weapons", 500)
    assert split.save.pilot.reputation[vr.FACTION_BLACKWAKE] == 2000 // vr.CONTRABAND_STANDING_STEP


def test_absent_contraband_totals_default_without_touching_standing():
    world = _world_with_seed(42)
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = 80
    data = world.save.to_dict()
    data.pop("contraband_trade_balance")
    data.pop("contraband_trade_milestones")
    loaded = vr.SaveData.from_dict(data)
    assert loaded.pilot.reputation[vr.FACTION_BLACKWAKE] == 80
    assert loaded.contraband_trade_balance == loaded.contraband_trade_milestones == 0


def test_new_futures_wait_for_origin_and_space_and_preserve_fee(tmp_path):
    world = _world_with_seed(42)
    before = world.save.pilot.credits
    vr.buy_futures_contract(world, "food", 3, 5)
    contract = world.save.active_futures[0]
    assert contract.principal < contract.locked_price
    world.save.turn = 5
    world.save.current_system = 1
    assert vr.settle_futures_contracts(world) == []
    assert not world.save.cargo
    world.save.current_system = 0
    _set_cargo(world, {"ore": vr.cargo_capacity(world.save.ship)})
    assert vr.settle_futures_contracts(world) == []
    assert world.save.active_futures == [contract]
    assert world.save.pilot.credits == before - contract.locked_price
    vr.persist(world, tmp_path, 77)
    save, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    restored = vr.World(save)
    vr.cancel_futures_contract(restored, contract.id)
    assert restored.save.pilot.credits == before - (contract.locked_price - contract.principal)
    assert not restored.save.active_futures
    with pytest.raises(vr.TradeError):
        vr.cancel_futures_contract(restored, contract.id)


def test_futures_fee_is_nonzero_and_cannot_be_reduced_by_order_splitting():
    world = _world_with_seed(42)
    for commodity in vr.LEGAL_COMMODITIES:
        principal, fee = vr.futures_quote(world, commodity, 12)
        unit_principal, unit_fee = vr.futures_quote(world, commodity, 1)
        assert principal == unit_principal * 12
        assert fee == unit_fee * 12 and unit_fee >= 1


@pytest.mark.parametrize("fault", ["negative", "bool", "too_large", "bad_term", "unaffordable", "illegal", "limit"])
def test_futures_rejection_is_atomic(fault):
    import copy
    world = _world_with_seed(42)
    commodity, quantity, duration = "food", 1, 5
    if fault == "negative": quantity = -1
    elif fault == "bool": quantity = True
    elif fault == "too_large": quantity = vr.cargo_capacity(world.save.ship) + 1
    elif fault == "bad_term": duration = 7
    elif fault == "unaffordable": world.save.pilot.credits = 0
    elif fault == "illegal": commodity = "weapons"
    elif fault == "limit":
        for _ in range(vr.MAX_FUTURES_CONTRACTS):
            vr.buy_futures_contract(world, "food", 1, 5)
    before = copy.deepcopy(world.save.to_dict())
    with pytest.raises(vr.TradeError):
        vr.buy_futures_contract(world, commodity, quantity, duration)
    assert world.save.to_dict() == before


def test_cancelled_contraband_futures_cannot_award_standing():
    world = _world_with_seed(42)
    world.here.economy = "Haven"
    world.save.pilot.credits = 10000
    for _ in range(40):
        vr.buy_futures_contract(world, "weapons", 1, 5)
        vr.cancel_futures_contract(world, world.save.active_futures[0].id)
    assert world.save.contraband_trade_balance < 0
    assert world.save.pilot.reputation[vr.FACTION_BLACKWAKE] == 0


def test_futures_draft_edit_and_back_write_nothing(monkeypatch):
    import copy
    world = _world_with_seed(42)
    before = copy.deepcopy(world.save.to_dict())
    commands = iter("QTTB")
    monkeypatch.setattr(vr, "read_key", lambda: next(commands))
    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: "3")
    for boundary in ("checkpoint", "commit", "advance_station_state"):
        # Every write path, not just the historical name (#417 review).
        monkeypatch.setattr(world, boundary, lambda: pytest.fail("Draft persisted"))
    with contextlib.redirect_stdout(io.StringIO()):
        vr._screen_buy_futures(vr.Palette(False), world, "food")
    assert world.save.to_dict() == before


def test_futures_cancel_decline_keeps_order_and_money(monkeypatch):
    import copy
    world = _world_with_seed(42)
    vr.buy_futures_contract(world, "food", 2, 5)
    before = copy.deepcopy(world.save.to_dict())
    commands = iter("XNB")
    monkeypatch.setattr(vr, "read_key", lambda: next(commands))
    with contextlib.redirect_stdout(io.StringIO()):
        vr._screen_futures_order(vr.Palette(False), world, world.save.active_futures[0])
    assert world.save.to_dict() == before


def test_departure_does_not_teleport_new_pickup_goods(monkeypatch):
    world = _world_with_seed(42)
    vr.buy_futures_contract(world, "food", 2, 5)
    world.save.turn = 4
    dest = world.here.connections[0]
    monkeypatch.setattr(vr, "_resolve_random_travel_encounter", lambda *args: None)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, dest)
    assert world.save.turn == 5
    assert not world.save.cargo and len(world.save.active_futures) == 1


def test_real_futures_cancellation_survives_forced_termination(tmp_path):
    world = _world_with_seed(42)
    before = world.save.pilot.credits
    vr.buy_futures_contract(world, "food", 2, 5)
    contract = world.save.active_futures[0]
    vr.persist(world, tmp_path, 77)
    index = len(vr.LEGAL_COMMODITIES)
    commands = b"MX" + b"N" * (index // 4) + str(index % 4 + 1).encode() + b"XY"
    with _door_stopped_at(tmp_path, commands, b"Order cancelled:"):
        saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert not saved.active_futures
    assert saved.pilot.credits == before - (contract.locked_price - contract.principal)


@pytest.mark.parametrize("field,value", [("origin_system", "0"), ("principal", -1), ("commodity", [])])
def test_new_futures_corrupt_metadata_uses_preserving_recovery(field, value):
    world = _world_with_seed(42)
    vr.buy_futures_contract(world, "food", 1, 5)
    data = world.save.to_dict()
    data["active_futures"][0][field] = value
    with pytest.raises(vr.ResumeError):
        vr.SaveData.from_dict(data)


def test_ready_pickup_prevents_premature_stranded_tow(monkeypatch):
    world = _world_with_seed(42)
    world.save.current_system = world.here.connections[0]
    vr.buy_futures_contract(world, "food", 2, 5)
    world.save.turn = 5
    world.save.ship.fuel = 0
    world.save.pilot.credits = 0
    station = world.save.current_system
    assert vr.is_stranded(world)
    snapshots = []
    world._checkpoint = lambda current: snapshots.append(current.save.to_dict())
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    with contextlib.redirect_stdout(io.StringIO()) as output:
        assert vr.screen_station_menu(vr.Palette(False), world) == "Q"
    assert world.save.current_system == station
    assert world.save.cargo == {"food": 2}
    assert not world.save.active_futures
    assert snapshots[0]["cargo"] == {"food": 2}
    assert "tug" not in output.getvalue().lower()


@pytest.mark.parametrize("fields", [("origin_system",), ("principal",), ("origin_system", "principal")])
def test_explicit_null_futures_metadata_preserves_original_save(tmp_path, fields):
    import json

    world = _world_with_seed(42)
    vr.buy_futures_contract(world, "food", 1, 5)
    data = world.save.to_dict()
    for field in fields:
        data["active_futures"][0][field] = None
    path = tmp_path / "77.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(vr.ResumeError):
        vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == before


def test_a_futures_order_round_trips_its_whole_shape():
    original = vr.FuturesContract(1, "food", 1, 100, 5, 3, 92, 1)
    data = original.to_dict()
    assert set(data) == {"id", "commodity", "quantity", "locked_price", "settle_turn",
                         "origin_system", "principal", "reserved"}
    assert vr.FuturesContract.from_dict(data) == original


@pytest.mark.parametrize("missing", ["origin_system", "principal", "reserved"])
def test_a_futures_order_missing_its_pickup_terms_is_refused(missing):
    """Remote-delivery orders were a retired schema's shape (issue #421)."""
    data = vr.FuturesContract(1, "food", 1, 100, 5, 3, 92, 1).to_dict()
    del data[missing]
    with pytest.raises(vr.ResumeError): vr.FuturesContract.from_dict(data)


@pytest.mark.parametrize("new_career", [False, True])
def test_second_real_launch_cannot_load_or_replace_an_active_pilot(tmp_path, new_career):
    import subprocess

    if not new_career:
        vr.write_save(tmp_path, 77, _world_with_seed(42).save)
    ack = b"Pilot callsign" if new_career else b"STATION SERVICES"
    with _live_voidrunner(tmp_path, acknowledgement=ack) as (_, env):
        before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*.json")}
        result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=b"Q",
                                capture_output=True, env=env, timeout=10)
        assert result.returncode == 0, result.stderr
        assert b"already has an active Voidrunner session" in result.stdout
        assert b"Welcome back" not in result.stdout and b"Pilot callsign" not in result.stdout
        assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*.json")} == before


@pytest.mark.parametrize("end", ["kill", "quit", "eof"])
def test_pilot_lease_releases_and_acknowledged_trade_survives(tmp_path, end):
    import subprocess

    vr.write_save(tmp_path, 77, _world_with_seed(42).save)
    with _live_voidrunner(tmp_path, commands=b"MAP1\r", acknowledgement=b"Bought 1x Food") as (proc, env):
        if end == "kill":
            proc.kill()
        elif end == "quit":
            proc.stdin.write(b"QQ")
            proc.stdin.flush()
        else:
            proc.stdin.close()
        proc.wait(timeout=5)
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=b"Q",
                            capture_output=True, env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    assert b"Welcome back" in result.stdout
    assert b"already has" not in result.stdout
    save, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert save.cargo == {"food": 1}
    assert (tmp_path / ".77.lock").exists()


def test_independent_pilots_and_installations_can_play_concurrently(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    vr.write_save(first, 77, _world_with_seed(42).save)
    vr.write_save(first, 88, _world_with_seed(43).save)
    vr.write_save(second, 77, _world_with_seed(44).save)
    with _live_voidrunner(first), _live_voidrunner(first, 88), _live_voidrunner(second):
        assert {e["user_id"] for e in vr.load_hall_of_fame(first)} == {77, 88}
        assert {e["user_id"] for e in vr.load_hall_of_fame(second)} == {77}


def test_a_busy_node_skips_the_import_and_keeps_it_for_the_next_launch(tmp_path, monkeypatch):
    import json

    path = tmp_path / "leaderboard.json"
    path.write_text(json.dumps([{"user_id": 77, "handle": "Old", "best_credits": 9000}]), encoding="utf-8")

    @contextlib.contextmanager
    def busy(save_dir):
        raise vr.PilotBusy
        yield  # pragma: no cover -- the raise is the whole point

    monkeypatch.setattr(vr, "maintenance_session", busy)
    vr.import_hall_of_fame(tmp_path)
    assert not (tmp_path / "scores").exists()

    monkeypatch.undo()
    vr.import_hall_of_fame(tmp_path)
    assert vr.load_hall_of_fame(tmp_path)[0]["best_credits"] == 9000


def test_the_import_never_replaces_a_record_it_could_not_read(tmp_path, monkeypatch):
    """A record that exists but will not read is not an absent record (#421 review).

    Replacing it with the old leaderboard's row would discard counters and
    achievement history this import cannot see -- and for a pilot whose career
    was refused, nothing would ever republish them.
    """
    import json

    (tmp_path / "leaderboard.json").write_text(
        json.dumps([{"user_id": 77, "handle": "Old", "best_credits": 9000}]), encoding="utf-8")
    stored = tmp_path / "scores" / "77.json"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_text(json.dumps({"user_id": 77, "handle": "Current", "best_credits": 40_000,
                                  "retirements": 2, "kills": 9, "missions_completed": 30}), encoding="utf-8")
    original = stored.read_bytes()

    monkeypatch.setattr(vr, "_read_score_json", lambda path, limit=vr.MAX_SAVE_BYTES:
                        None if path == stored else
                        json.loads(path.read_text(encoding="utf-8")))
    vr.import_hall_of_fame(tmp_path)
    assert stored.read_bytes() == original


@pytest.mark.parametrize("field,value", [
    ("schema_version", 999), ("schema_version", True), ("galaxy_version", 2),
    ("seed", "42"), ("turn", -1), ("current_system", 48),
    ("pilot", []), ("pilot.credits", -1), ("pilot.credits", True),
    ("pilot.reputation", {"unknown": 4}), ("pilot.log", "not a list"),
    ("pilot.handle", "Bad\x1b[2J"), ("pilot.highest_rank_seen", 100),
    ("ship.hull_class", "Unknown"), ("ship.fuel", 999), ("ship.hull_hp", -1),
    ("ship.engine_tier", 99), ("ship.has_gunner", "yes"),
    ("cargo", {"unknown": 1}), ("cargo", {"food": -1}), ("cargo", {"food": 999}),
    ("discovered", [0, 0]), ("discovered", [True]), ("discovered", [49]),
    ("market_drift", {"0": {"food": float("nan")}}),
    ("market_drift", {"0": {"food": -1}}), ("market_drift", {"48": {}}),
    ("flags", {"landmark_investigated": "no"}), ("active_event", []),
    ("active_missions", "bad"), ("active_futures", {}), ("future_field", {}),
])
def test_invalid_career_fields_preserve_original_bytes(tmp_path, field, value):
    import json

    data = _world_with_seed(42).save.to_dict()
    target = data
    keys = field.split(".")
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    path = tmp_path / "77.json"
    original = json.dumps(data).encode()
    path.write_bytes(original)
    with pytest.raises(vr.ResumeError):
        vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("raw", [b"[]", b"null", b"\xff\xfe\xff", b"{" + b'"seed":1,"seed":2}',
                                b"[" * 1100 + b"]" * 1100])
def test_invalid_json_document_stops_without_reset(tmp_path, raw):
    path = tmp_path / "77.json"
    path.write_bytes(raw)
    with pytest.raises(vr.ResumeError):
        vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == raw


def test_oversized_career_and_unreadable_file_never_start_new(tmp_path, monkeypatch):
    path = tmp_path / "77.json"
    path.write_bytes(b"x" * (vr.MAX_SAVE_BYTES + 1))
    with pytest.raises(vr.ResumeError, match="file size"):
        vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.stat().st_size == vr.MAX_SAVE_BYTES + 1
    monkeypatch.setattr(vr, "_read_save_bytes", lambda path: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(vr.ResumeError, match="could not be read"):
        vr.load_or_create_save(tmp_path, 77, "Tester")


def test_real_first_flight_survives_kills_through_acceptance_purchase_delivery_and_upgrade(tmp_path):
    import os
    import subprocess
    world = _world_with_seed(42)
    world.event_rng.seed(0)  # Ordinary first hop has no random encounter.
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    offer = vr.opening_assignment_offer(world)
    cargo_cost = 3 * vr.price_for(world, 0, offer.commodity)
    with _door_stopped_at(tmp_path, b"GO" + b"N" * 10 + b"A", b"First Flight accepted and tracked"):
        accepted, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert accepted.active_missions[0].opening_assignment and accepted.tracked_mission_id == offer.id
    market_key = vr.MARKET_LETTERS[vr.LEGAL_COMMODITIES.index(offer.commodity)].encode()
    with _door_stopped_at(tmp_path, b"M" + market_key + b"P3\n", b"Bought 3x"):
        bought, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert bought.cargo[offer.commodity] == 3 and bought.pilot.credits == 1200 - cargo_cost
        assert bought.cargo_basis == {offer.commodity: [[3, cargo_cost]]}
    jump_key = vr.CHART_CONNECTION_LETTERS[sorted(world.here.connections).index(offer.target_system)].encode()
    with _door_stopped_at(tmp_path, b"C" + jump_key + b"Y", b"Mission complete: First Flight:"):
        delivered, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert delivered.flags["opening_assignment_completed"]
        assert delivered.pilot.credits == 1200 - cargo_cost + offer.reward
        assert delivered.turn == 1 and not delivered.active_missions
        assert not delivered.cargo_basis
        assert (delivered.trading_ledger.delivery_cost, delivered.trading_ledger.delivery_revenue) == (cargo_cost, offer.reward)
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=b"QQ", capture_output=True,
                            env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path),
                                     NETBBS_DOOR_INFO=str(tmp_path / "door_info.json")), timeout=10)
    assert result.returncode == 0 and not result.stderr
    resumed, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert resumed.pending_travel is None and resumed.pilot.credits == delivered.pilot.credits
    assert resumed.trading_ledger == delivered.trading_ledger
    with _door_stopped_at(tmp_path, b"YAY", b"Cargo Bay Expansion upgraded to tier 1"):
        upgraded, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert upgraded.ship.cargo_tier == 1 and upgraded.pilot.credits == resumed.pilot.credits - 800


@pytest.mark.parametrize("commands", [b"GBQ", b"GOBBQ", b"G", b"GO"])
def test_real_opening_guide_back_and_eof_leave_career_unchanged(tmp_path, commands):
    import os
    import json
    import subprocess
    world = _world_with_seed(42)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    before = (tmp_path / "77.json").read_bytes()
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=commands, capture_output=True,
                            env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)), timeout=10)
    assert result.returncode == 0 and not result.stderr
    assert shows_page(result.stdout, "Pilot Guide")
    if b"O" in commands:
        assert shows_page(result.stdout, "First Flight")
    assert (tmp_path / "77.json").read_bytes() == before


def test_opening_tags_on_posted_offers_are_rejected_before_loading_or_writing(tmp_path):
    import json
    world = _world_with_seed(42)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    path = tmp_path / "77.json"
    before = path.read_bytes()
    offer = vr.opening_assignment_offer(world)
    world.save.mission_boards[0]["offers"][0] = offer.to_dict()
    with pytest.raises(vr.SaveError):
        vr.write_save(tmp_path, 77, world.save)
    assert path.read_bytes() == before
    malformed = json.dumps(world.save.to_dict()).encode("utf-8")
    path.write_bytes(malformed)
    with pytest.raises(vr.ResumeError, match="opening assignment placement"):
        vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == malformed


def test_opening_quote_avoids_cargo_already_promised_to_an_earlier_delivery():
    world = _world_with_seed(42)
    world.checkpoint()
    first = vr.opening_assignment_offer(world)
    world.save.active_missions.append(vr.Mission(100, "delivery", "Earlier order", 100, 0, first.target_system,
                                               commodity=first.commodity, quantity=1))
    offer = vr.opening_assignment_offer(world)
    assert offer is not None
    assert (offer.target_system, offer.commodity) != (first.target_system, first.commodity)
    vr.accept_opening_assignment(world, offer)
    _add_cargo(world, offer.commodity, offer.quantity)
    world.save.current_system = offer.target_system
    vr.check_mission_completions(world)
    assert world.save.flags["opening_assignment_completed"]
    assert any(m.description == "Earlier order" for m in world.save.active_missions)


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
@pytest.mark.parametrize("orders", [1, 3])
def test_cockpit_settlement_results_stay_inside_height_budget(monkeypatch, terminal, width, height, orders):
    import re
    terminal(width, height)
    world = _world_with_seed(42)
    for commodity in ["food", "textiles", "medicine"][:orders]:
        vr.buy_futures_contract(world, commodity, 1, 5)
    world.save.turn = 5
    settled, calls, snapshots, frames = [], [], [], []
    real_settle = vr.settle_futures_contracts
    def settle(current):
        calls.append(1)
        result = real_settle(current)
        settled.extend(result)
        return result
    monkeypatch.setattr(vr, "settle_futures_contracts", settle)
    world._checkpoint = lambda current: snapshots.append(current.save.to_dict())
    output = io.StringIO()
    def choose():
        frame = output.getvalue(); output.seek(0); output.truncate(0)
        frames.append(frame)
        assert len(frame.splitlines()) <= height
        assert all(vr._visible_width(line) <= width for line in frame.splitlines())
        assert "[Q] Exit:" in page_text(frame)    # a narrow bar wraps (#400)
        page, count = map(int, re.search(r"Command Deck:.*?(\d+)/(\d+)", frame, re.S).groups())
        return "Q" if page == count else ">"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output):
        assert vr.screen_station_menu(vr.Palette(False), world) == "Q"
    assert calls == [1] and len(snapshots) == 1 and len(settled) == orders
    content = []
    for frame in frames:
        plain = vr._ANSI_RE.sub("", frame)
        assert "Command Deck:" in page_title(frame)
        rows = page_rows(frame)  # one-page decks drop Prev/Next (#412)
        title, consumed = page_title(frame), ""
        while rows and consumed != title and title.startswith(f"{consumed} {rows[0]}".strip()):
            consumed = f"{consumed} {rows.pop(0)}".strip()  # unframed, the title is a row of its own
        stop = next((index for index, row in enumerate(rows) if row.startswith(("[<", "[X"))), len(rows))
        content.append(" ".join(rows[:stop]))
    text = " ".join(" ".join(content).split())
    for message in settled: assert text.count(message) == 1


@pytest.mark.parametrize("kind", ["derelict", "distress"])
@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
@pytest.mark.parametrize("style", ["auto", "plain"])
def test_exploration_terms_fit_and_browsing_has_no_effects(monkeypatch, terminal, kind, width, height, style):
    import copy, re
    world = _world_with_seed(42); world.save.ship.fuel = 3
    terminal(width, height, style)
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    output = io.StringIO(); frames = []
    def choose():
        frame = output.getvalue(); output.seek(0); output.truncate(0); frames.append(frame)
        assert len(frame.splitlines()) <= height
        assert all(vr._visible_width(line) <= width for line in frame.splitlines())
        assert world.save.to_dict() == before and world.event_rng.getstate() == rng
        assert "Fuel 3" in page_title(frame)
        page, count = map(int, re.search(r"(?:Derelict|Distress).*?(\d+)/(\d+)", frame, re.S).groups())
        if len(frames) == 1:
            first = page_text(frame)
            assert ("30%" in first) if kind == "derelict" else ("tank empty" in first)
            return "?"
        if page == count: raise EOFError
        return ">"
    monkeypatch.setattr(vr, "read_key", choose)
    world._checkpoint = lambda current: pytest.fail("Browsing checkpointed")
    with contextlib.redirect_stdout(output), pytest.raises(EOFError):
        getattr(vr, "_encounter_" + ("derelict" if kind == "derelict" else "distress_call"))(vr.Palette(False), world)
    text = page_text(frames)
    assert ("70%" in text and "30%" in text) if kind == "derelict" else "60-180cr" in text
    assert "[I] Ignore" in text


@pytest.mark.parametrize("fault", ["scanner", "fuel", "charted", "journey"])
def test_unavailable_survey_changes_nothing(fault):
    import copy
    world = _world_with_seed(42); world.save.ship.scanner_tier = 1
    if fault == "scanner": world.save.ship.scanner_tier = 0
    elif fault == "fuel": world.save.ship.fuel = 1
    elif fault == "charted":
        for system in world.galaxy: system.discovered = True
        world.sync_discovered()
    else: world.save.pending_travel = {"unfinished": True}
    saved, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    with pytest.raises(ValueError): vr.perform_survey(world)
    assert world.save.to_dict() == saved and world.event_rng.getstate() == rng


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
@pytest.mark.parametrize("style", ["auto", "plain"])
@pytest.mark.parametrize("fuel", [2, 24])
def test_survey_terms_before_and_after_scanning_fit_and_browsing_is_read_only(monkeypatch, terminal, width, height, style, fuel):
    import copy,re
    world = _world_with_seed(42); world.save.ship.scanner_tier = 1; world.save.ship.fuel = fuel
    terminal(width, height, style)
    for surveyed in (False, True):
        if surveyed: vr.perform_survey(world)
        saved, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
        out = io.StringIO(); frames = []
        def choose():
            frame = out.getvalue(); out.seek(0); out.truncate(0); frames.append(frame)
            assert "[B] Back" in page_text(frame)
            assert len(frame.splitlines()) <= height
            assert all(vr._visible_width(row) <= width for row in frame.splitlines())
            assert world.save.to_dict() == saved and world.event_rng.getstate() == rng
            if len(frames) == 1 and not surveyed and fuel == 2: assert "Tank empties" in page_text(frame)
            page, count = map(int, re.search(r"Survey.*?(\d+)/(\d+)", frame, re.S).groups())
            if page == count: return "B"
            return ">"
        monkeypatch.setattr(vr, "read_key", choose)
        world._checkpoint = lambda w: pytest.fail("Read-only survey view checkpointed")
        with contextlib.redirect_stdout(out): assert vr._do_scan(vr.Palette(False), world) is None


@pytest.mark.parametrize("commands", [b"CSBQ", b"CS", b"CS?><BQ"])
def test_real_survey_back_paging_and_eof_preserve_career(tmp_path, commands):
    import json,os,subprocess
    world = _world_with_seed(42); world.save.ship.scanner_tier = 1
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    original = (tmp_path / "77.json").read_bytes()
    info = tmp_path / "door_info.json"; info.write_text(json.dumps({"user_id": 77, "handle": "Tester", "terminal_width": 40, "terminal_height": 12}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=commands, capture_output=True,
        env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)), timeout=10)
    assert result.returncode == 0 and not result.stderr and b"Survey 1,200cr" in result.stdout
    assert (tmp_path / "77.json").read_bytes() == original


def test_survey_report_retains_every_contact_and_result_after_invalid_input(monkeypatch, terminal):
    import re
    world = _world_with_seed(42); world.save.ship.scanner_tier = 1
    targets = vr.survey_candidates(world)
    terminal(40, 12)
    out = io.StringIO(); frames = []; chose = False; invalid = False
    def choose():
        nonlocal chose, invalid
        frame = out.getvalue(); out.seek(0); out.truncate(0); frames.append(frame)
        assert len(frame.splitlines()) <= 12
        assert all(vr._visible_width(row) <= 40 for row in frame.splitlines())
        if not chose: chose = True; return "S"
        if not invalid: invalid = True; return "?"
        page, count = map(int, re.search(r"Survey.*?(\d+)/(\d+)", frame, re.S).groups())
        return "B" if page == count else ">"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(out): result = vr._do_scan(vr.Palette(False), world)
    text = page_text(frames)
    assert "Survey complete" in result
    for sid in targets: assert world.by_id[sid].name in text and world.by_id[sid].station_name in text
    assert world.save.ship.fuel == 22


@pytest.mark.parametrize("name,roll,label", [("Hollow Fang", 0.695, "70%"), ("Rust Wraith", 0.595, "60%")])
def test_dump_preview_matches_actual_post_disposal_escape_boundary(monkeypatch, name, roll, label):
    world = _world_with_seed(42); _set_cargo(world, {"food": 1})
    pirate = vr.Pirate(name, 0, 50, 50)
    # The actual new fight uses this opponent's initial intent, including Harry.
    line = next(row for row in vr.combat_display_lines(world, pirate, [], patrol=False, tactics=vr.new_tactics(pirate)) if row.startswith("[D]"))
    monkeypatch.setattr(vr, "read_key", lambda: "D")
    monkeypatch.setattr(world.event_rng, "random", lambda: roll)
    with contextlib.redirect_stdout(io.StringIO()): assert vr.screen_combat(vr.Palette(False), world, pirate) == "escaped"
    assert not world.save.cargo and label in line


def test_specialist_sites_are_distinct_deterministic_and_preserve_existing_world():
    import copy,dataclasses
    for seed in range(60):
        world = _world_with_seed(seed)
        before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
        galaxy = [dataclasses.asdict(s) for s in world.galaxy]
        sites = vr.specialist_stations(world)
        assert len(set(sites.values())) == 3 and 0 not in sites.values()
        assert set(sites) == {"cargo", "engine", "scanner"}
        world.galaxy.reverse()
        assert vr.specialist_stations(world) == sites
        world.galaxy.reverse()
        assert world.save.to_dict() == before and world.event_rng.getstate() == rng
        assert [dataclasses.asdict(s) for s in world.galaxy] == galaxy
        assert vr.specialist_stations(vr.World(vr.SaveData.from_dict(before))) == sites


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_specialist_directory_paging_keeps_all_named_sites_and_stable_keys(monkeypatch, terminal, width, height):
    import copy,re
    world = _world_with_seed(42)
    terminal(width, height)
    before = copy.deepcopy(world.save.to_dict()); output = io.StringIO(); frames = []
    def choose():
        frame = output.getvalue(); output.seek(0); output.truncate(0); frames.append(frame)
        assert len(frame.splitlines()) <= height
        assert all(vr._visible_width(row) <= width for row in frame.splitlines())
        page, count = map(int, re.search(r"Workshops\s+(\d+)/(\d+)", page_title(frame)).groups())
        return "B" if page == count else ">"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output): vr.screen_specialists(vr.Palette(False), world)
    assert world.save.to_dict() == before
    text = page_text(frames)
    for shop in vr.WORKSHOPS.values(): assert shop["name"] in text and shop["owner"] in text


def test_specialist_directory_flies_real_route_then_installs_with_known_materials(monkeypatch, tmp_path):
    world = _world_with_seed(42)
    destination = vr.specialist_stations(world)["cargo"]
    path = vr.bfs_path(world.by_id, 0, destination)
    world.save.cargo = {"metals": 2}; world.save.cargo_basis = {"metals": [[2, 100]]}
    world.save.trading_ledger.since_day = world.save.turn
    keys = iter("1R" + "J" * len(path) + "BIYBB")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(world.event_rng, "random", lambda: 0.99)
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    with contextlib.redirect_stdout(io.StringIO()): result = vr.screen_specialists(vr.Palette(False), world)
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert saved.current_system == destination and saved.turn == len(path)
    assert saved.ship.cargo_tier == 1 and saved.pilot.credits == 680 and not saved.cargo
    assert saved.ship.fuel < 24 and saved.pending_travel is None
    assert saved.trading_ledger.workshop_material_cost == 100
    assert "Iona Rusk installed" in result


@pytest.mark.parametrize("role,letter", [("gunner", b"A"), ("engineer", b"C"), ("navigator", b"D")])
def test_real_hire_records_the_previewed_identity_before_ack(tmp_path, role, letter):
    world = _world_with_seed(42); world.save.pilot.credits = 100_000
    world.save.pilot.highest_rank_seen = len(vr.RANKS) - 1
    identity = vr.crew_identity(world, role)
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    with _door_stopped_at(tmp_path, b"YK" + letter + b"Y", (vr.CREW_ROLES[role]["label"] + " hired.").encode()):
        saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert saved.ship.crew_records[role] == {"version": 1, "identity": identity, "paid_jumps": 0}
        assert getattr(saved.ship, "has_" + role)
        assert saved.pilot.credits == 100_000 - vr.CREW_ROLES[role]["hire_cost"]


@pytest.mark.parametrize("hull", list(vr.HULL_CLASSES))
@pytest.mark.parametrize("condition,fraction,marks", [("Intact",1,0),("Scuffed",.75,1),("Damaged",.5,3),("Critical",.2,6)])
def test_ship_portrait_damage_matches_real_hull_without_changing_ship(hull,condition,fraction,marks):
    import copy
    world=_world_with_seed(42); ship=world.save.ship; ship.hull_class=hull
    ship.hull_hp=int(vr.hull_hp_max(ship)*fraction)
    before=copy.deepcopy(world.save.to_dict()); rng=world.event_rng.getstate(); registry=copy.deepcopy(vr.PORTRAITS)
    large,compact,details,title=vr.viewport_content(world,"1")
    assert title==hull and condition in details[0] and f"{ship.hull_hp}/{vr.hull_hp_max(ship)}" in details[0]
    assert sum(row.count("x") for row in large)==marks
    assert sum(row.count("x") for row in compact)==marks
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng and vr.PORTRAITS==registry


@pytest.mark.parametrize("width,height", [(40,12),(80,24)])
@pytest.mark.parametrize("style", list(vr.DISPLAY_STYLES))
def test_every_portrait_has_distinct_complete_bounded_composition(monkeypatch, terminal,width,height,style):
    terminal(width, height, style)
    portraits=[art for category in vr.PORTRAITS.values() for art in category.values()]
    assert len(portraits)==13
    for layout in ("large","compact"):
        assert len({tuple(art[layout]) for art in portraits})==13
        assert all(row.isascii() and len(row)<=(38 if layout=="large" else 18) for art in portraits for row in art[layout])
    for art in portraits:
        details=["Real station information follows the complete portrait.", "Fuel 7/24; cargo 3/24 used."]
        footer="[<>] Page [B] Back: "; title="Portrait"
        pages=vr.portrait_pages(vr.Palette(True),art["large"],art["compact"],details,title,footer)
        rendered=[]
        for index,page in enumerate(pages):
            monkeypatch.setattr(vr,"read_key",lambda:"B")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                vr._draw_service_page(vr.Palette(True),title,[],footer,index,pages=pages)
            frame=output.getvalue(); rendered.append(frame)
            assert len(frame.splitlines())<=height
            assert all(vr._visible_width(row)<=width for row in frame.splitlines())
        chosen=art["large" if width>=40 and height>=16 else "compact"]
        plain_pages=[[vr._ANSI_RE.sub("",row) for row in page] for page in pages]
        assert sum(any(page[i:i+len(chosen)]==chosen for i in range(len(page))) for page in plain_pages)==1
        text=" ".join(" ".join(row.split()) for page in plain_pages for row in page)
        for detail in details:assert detail in text
        output="".join(rendered)
        if style in ("mono","plain"):assert "\x1b" not in output
        elif style=="basic":assert "\x1b[" in output and "38;" not in output
        else:assert "38;2;" in output
        if style=="plain":assert output.isascii()


@pytest.mark.parametrize("at_site,claimed", [(False,False),(True,False),(False,True),(True,True)])
def test_discovery_portrait_requires_visit_or_recorded_investigation(at_site,claimed):
    world=_world_with_seed(42)
    if at_site:world.save.current_system=world.landmark["system_id"]
    if claimed:world.save.flags["landmark_investigated"]=True
    large,compact,details,_=vr.viewport_content(world,"3")
    if at_site or claimed:
        assert large==vr.PORTRAITS["site"][world.landmark["label"]]["large"] and compact
        assert ("Salvage already claimed." if claimed else "Unclaimed salvage:") in " ".join(details)
    else:assert not large and not compact and world.landmark["label"] not in " ".join(details)


@pytest.mark.parametrize("commands", [b"V",b"V23><BQ",b"YV23><BQ"])
def test_real_viewport_browsing_preserves_career_bytes(tmp_path,commands):
    import json,os,subprocess
    world=_world_with_seed(42); world._checkpoint=lambda w:vr.persist(w,tmp_path,77); world.checkpoint()
    before=(tmp_path/"77.json").read_bytes()
    info=tmp_path/"door_info.json"; info.write_text(json.dumps({"user_id":77,"handle":"Tester","terminal_width":40,"terminal_height":12}),encoding="utf-8")
    result=subprocess.run([sys.executable,str(_VOIDRUNNER_PATH)],input=commands,capture_output=True,timeout=10,
        env=dict(os.environ,VOIDRUNNER_SAVE_DIR=str(tmp_path),NETBBS_DOOR_INFO=str(info)))
    assert result.returncode==0 and not result.stderr
    assert b"[1-3] View" in plain_bytes(result.stdout)
    assert (tmp_path/"77.json").read_bytes()==before


def test_achievement_invalid_lifetime_total_cannot_poison_the_career_save(tmp_path):
    import json
    path=tmp_path/"leaderboard.json";path.write_text(json.dumps([{"user_id":77,"handle":"Invalid","best_credits":10000,"retirements":2**63}]),encoding="utf-8")
    world=_world_with_seed(42);world._checkpoint=lambda w:vr.persist(w,tmp_path,77);world.checkpoint()
    saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
    assert saved.pilot.retirements==0 and vr._load_score_records(tmp_path)[0]["retirements"]==0


@pytest.mark.parametrize("screen", ["market", "shipyard", "crew", "chart"])
def test_b_is_back_on_every_letter_list_screen_and_writes_nothing(monkeypatch, screen):
    world = _world_with_seed(42); before = world.save.to_dict(); rng = world.event_rng.getstate()
    world._checkpoint = lambda current: pytest.fail("Back must not checkpoint")
    monkeypatch.setattr(vr, "read_key", lambda: "B")
    draw = {"market": vr.screen_market, "shipyard": vr.screen_shipyard, "crew": vr.screen_crew, "chart": vr.screen_chart}[screen]
    with contextlib.redirect_stdout(io.StringIO()) as output:
        result = draw(vr.Palette(False), world)
    plain = vr._ANSI_RE.sub("", output.getvalue())
    assert ("[B] Back" in plain or "[B] Back" in plain) and "[Q] Back" not in plain and result is None
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


def test_selection_letters_never_include_back_or_a_screen_hotkey():
    for letters, reserved in ((vr.MARKET_LETTERS, "XQ"), (vr.YARD_LETTERS, "RPKSVUQ"), (vr.CREW_LETTERS, "Q"),
                              (vr.CHART_CONNECTION_LETTERS, vr.CHART_RESERVED_LETTERS)):
        assert vr.BACK_KEY not in letters and not set(reserved) & set(letters)
    assert vr.letter_span(vr.CREW_LETTERS[:3]) == "A/C/D" and vr.letter_span(vr.MARKET_LETTERS[:7]) == "A,C-H"
    assert vr.letter_span([]) == "" and vr.letter_span(["A"]) == "A"


def test_old_second_connection_letter_no_longer_departs(monkeypatch):
    world = _world_with_seed(42); world.save.ship.fuel = 99
    assert len(world.here.connections) >= 2 and "B" not in vr.CHART_CONNECTION_LETTERS
    monkeypatch.setattr(vr, "read_key", lambda: "B")
    with contextlib.redirect_stdout(io.StringIO()) as output:
        assert vr.screen_chart(vr.Palette(False), world) is None
    assert "Depart for" not in output.getvalue()


@pytest.mark.parametrize("answer,discovered", [("N", True), ("Y", True), ("Y", False)])
def test_chart_departure_confirms_cost_and_danger_as_the_last_keystroke(monkeypatch, answer, discovered):
    world = _world_with_seed(42); world.save.ship.fuel = 99; before = world.save.to_dict()
    dest = sorted(world.here.connections)[0]; world.by_id[dest].discovered = discovered
    keys = iter([vr.CHART_CONNECTION_LETTERS[0], answer, "B"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        selected = vr.screen_chart(vr.Palette(False), world)
    plain = page_text(output.getvalue())
    cost = vr.fuel_cost_for_jump(world.here, world.by_id[dest], world.save.ship)
    name = world.by_id[dest].name if discovered else "an uncharted system"
    danger = f"danger {world.by_id[dest].danger}" if discovered else "danger unknown"
    assert f"Depart for {name}? {cost} fuel, {danger}, one day passes. [Y/N, Esc=No]" in plain
    if answer == "Y": assert selected == dest
    else: assert selected is None and "Departure cancelled; still docked." in plain
    assert world.save.to_dict() == before


def test_combat_b_is_harmless_and_p_pays_the_bribe(monkeypatch):
    world, pirate = _world_with_pending_fight()
    world.save.pilot.credits = 10_000; credits = world.save.pilot.credits
    monkeypatch.setattr(world.event_rng, "random", lambda: 0.0)  # bribe accepted
    keys = iter(["B", "P"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        outcome = vr._screen_combat_session(vr.Palette(False), world, pirate, patrol=False)
    plain = vr._ANSI_RE.sub("", output.getvalue())
    assert outcome == "escaped" and "[P] Pay bribe:" in plain and "[I] Info" in plain and "[Q] Info" not in plain
    assert world.save.pilot.credits == credits - vr.bribe_cost(pirate)


def test_customs_b_is_rejected_as_undisplayed_and_p_bribes(monkeypatch):
    world = _world_with_seed(42); _set_cargo(world, {"weapons": 2}); world.save.pilot.credits = 10_000
    monkeypatch.setattr(world.event_rng, "random", lambda: 0.0)
    keys = iter(["B", "P"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with pytest.raises(ValueError): vr.resolve_customs(world, "B")
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_customs(vr.Palette(False), world)
    plain = vr._ANSI_RE.sub("", output.getvalue())
    assert "Choose a displayed action" in plain and "[P] Pay bribe" in plain and "changes hands quietly" in plain


def test_futures_orders_reserve_station_stock_and_nudge_the_price():
    world = _world_at_food_producer()
    world.save.ship.hull_class = "Carrier"; world.save.ship.cargo_tier = vr.UPGRADES["cargo"]["max_tier"]
    cap = vr.market_depth_limits(world.here.economy, "food")["stock"]
    assert vr.cargo_capacity(world.save.ship) > cap
    spot_before = vr.price_for(world, world.here.id, "food")
    vr.buy_futures_contract(world, "food", 24, 5)
    contract = world.save.active_futures[0]
    assert contract.reserved == 24
    assert vr.market_depth_quote(world, world.here.id, "food")["stock"] == cap - 24
    assert vr.price_for(world, world.here.id, "food") > spot_before
    with pytest.raises(vr.TradeError, match="in stock"):
        vr.trade_cargo(world, "food", cap - 23, buying=True)
    with pytest.raises(vr.TradeError, match="station stock"):
        vr.buy_futures_contract(world, "food", cap - 23, 5)


def test_spot_and_futures_volume_cannot_exceed_replenished_stock_over_time():
    world = _world_at_food_producer()
    limits = vr.market_depth_limits(world.here.economy, "food")
    taken = 0
    for day in range(30):
        world.save.turn = day
        kind = "futures" if day % 2 else "spot"
        while True:
            try:
                if kind == "futures":
                    if len(world.save.active_futures) >= vr.MAX_FUTURES_CONTRACTS:
                        for order in list(world.save.active_futures):
                            world.save.active_futures.remove(order)  # collected elsewhere; keep the reservation
                    vr.buy_futures_contract(world, "food", 8, 5)
                else:
                    vr.trade_cargo(world, "food", 8, buying=True)
                    world.save.cargo.clear(); world.save.cargo_basis.clear()
                taken += 8
            except vr.TradeError:
                break
    assert taken <= limits["stock"] + 29 * limits["stock_rate"]
    assert taken >= limits["stock"]


def test_futures_order_exceeding_stock_is_rejected_before_any_change():
    import copy
    world = _world_at_food_producer()
    cap = vr.market_depth_limits(world.here.economy, "food")["stock"]
    world.save.ship.cargo_tier = vr.UPGRADES["cargo"]["max_tier"]
    world.save.ship.hull_class = "Carrier"
    assert vr.cargo_capacity(world.save.ship) > cap
    before = copy.deepcopy(world.save.to_dict())
    with pytest.raises(vr.TradeError, match="station stock"):
        vr.buy_futures_contract(world, "food", cap + 1, 5)
    assert world.save.to_dict() == before


def test_a_live_pilot_session_refuses_maintenance_and_maintenance_refuses_a_launch(tmp_path):
    """Both directions of the gate, with a real second process holding the lock.

    `maintenance_session` and `_maintenance_gate` had no game-side test at all:
    `test_backup.py` only checked capture ordering (issue #423).
    """
    import json
    import os
    import subprocess

    saves = tmp_path / "saves"
    saves.mkdir(parents=True, exist_ok=True)
    vr.persist(_world_with_seed(42), saves, 77)  # an existing career, so the door docks
    with _live_voidrunner(saves) as (proc, env):
        with pytest.raises(vr.PilotBusy):
            with vr.maintenance_session(saves):
                pytest.fail("maintenance must not start while a pilot is aboard")
        # And the same gate is what the backup component reports on.
        from netbbs import backup as backup_module
        with pytest.raises(backup_module.BackupError, match="active or undergoing maintenance"):
            with backup_module._voidrunner_maintenance(saves):
                pytest.fail("a capture must not start while a pilot is aboard")

    # The pilot has left: maintenance may start, and a launch during it reports busy.
    info = saves / "info-99.json"
    info.write_text(json.dumps({"user_id": 99, "handle": "Second"}), encoding="utf-8")
    with vr.maintenance_session(saves):
        launched = subprocess.run(
            [sys.executable, str(_VOIDRUNNER_PATH)], input=b"", capture_output=True, timeout=20,
            env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(saves), NETBBS_DOOR_INFO=str(info)))
    assert launched.returncode == 0 and not launched.stderr
    assert b"already has an active Voidrunner session" in launched.stdout  # the message wraps
    assert not (saves / "99.json").exists()  # a refused launch creates no career


def test_hull_condition_names_each_band_at_its_own_threshold():
    """A reversed mapping -- "Intact" for a wreck -- must fail this test."""
    world = _world_with_seed(42)
    ship = world.save.ship
    maximum = vr.hull_hp_max(ship)
    expected = ["Critical", "Damaged", "Scuffed", "Intact"]
    ship.hull_hp = maximum
    assert vr.hull_condition(ship) == "Intact"
    ship.hull_hp = maximum - 1
    assert vr.hull_condition(ship) == "Scuffed"
    ship.hull_hp = maximum // 2
    assert vr.hull_condition(ship) == "Damaged"
    ship.hull_hp = maximum // 2 + 1
    assert vr.hull_condition(ship) == "Scuffed"  # the halfway point is the boundary
    ship.hull_hp = maximum // 5
    assert vr.hull_condition(ship) == "Critical"
    ship.hull_hp = maximum // 5 + 1
    assert vr.hull_condition(ship) == "Damaged"
    ship.hull_hp = 1
    assert vr.hull_condition(ship) == "Critical"
    bands = []
    for hull in range(1, maximum + 1):
        ship.hull_hp = hull
        name = vr.hull_condition(ship)
        if not bands or bands[-1] != name:
            bands.append(name)
    assert bands == expected  # in that order, worst first, each band entered once


def test_bribe_chance_falls_with_the_opponent_and_rises_with_standing():
    world = _world_with_seed(42)
    weak, strong = vr.Pirate("Weak", 0, 20, 20), vr.Pirate("Strong", 4, 90, 90)
    assert 0.0 <= vr.bribe_chance(world, strong) < vr.bribe_chance(world, weak) <= 1.0
    baseline = vr.bribe_chance(world, weak)
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = 100
    assert vr.bribe_chance(world, weak) > baseline  # standing has to move it, upward
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = -100
    assert vr.bribe_chance(world, weak) <= baseline  # and hostility must not help
    assert vr.bribe_cost(strong) > vr.bribe_cost(weak)


def test_a_hold_route_the_pilot_cannot_afford_to_fly_is_not_an_opportunity():
    """Selling what is aboard buys nothing, but the trip still costs (issue #415 review)."""
    world = _world_with_trade_opportunities()
    held = vr.cargo_capacity(world.save.ship)
    _set_cargo(world, {"machinery": held})
    world.save.cargo_basis = {"machinery": [[held, held * vr.price_for(world, 0, "machinery")]]}
    world.save.ship.has_gunner = True
    world.save.ship.fuel = 0  # every leg needs a paid top-up now
    affordable = vr.trade_opportunities(world)
    assert affordable and all(quote["use_hold"] for quote in affordable)
    world.save.pilot.credits = 0
    assert vr.trade_opportunities(world) == []  # no cash for fuel or wages, so no route
