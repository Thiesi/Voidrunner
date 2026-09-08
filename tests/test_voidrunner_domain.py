"""Tests for the Voidrunner door's domain layer (netbbs.doors.bundled.
voidrunner) -- galaxy generation, economy, save round-tripping,
missions, and combat resolution. Loaded directly from its file path
rather than a normal `from netbbs.doors.bundled import voidrunner`
import -- same reasoning as `test_doors_runtime.py` running it and
`retro_trivia.py` this same way: this is the exact file NetBBS itself
launches as a standalone subprocess (see `netbbs.doors.runtime`), not
an ordinarily-imported library module, so testing it by path exercises
precisely what actually ships. This file just exercises the pure domain
functions in-process instead of the whole door end to end.

Regression-focused: several of these exist specifically to pin behavior
that would otherwise be easy to silently break (galaxy determinism/
connectivity, corrupt-save recovery, mission completion), not just to
restate what the code already visibly does.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import random
import sys
from pathlib import Path

import pytest

_VOIDRUNNER_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "netbbs" / "doors" / "bundled" / "voidrunner.py"
)


def _load_voidrunner():
    spec = importlib.util.spec_from_file_location("voidrunner_domain_under_test", _VOIDRUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # `dataclasses` (voidrunner.py uses `from __future__ import annotations`,
    # so field types are strings) resolves them via
    # `sys.modules[cls.__module__].__dict__` -- the module must already be
    # registered under its own name in sys.modules *before* exec_module
    # runs, or that lookup returns None and every @dataclass in the file
    # raises AttributeError at import time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vr = _load_voidrunner()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows byte-range lock initialization")
def test_empty_windows_lease_reports_busy_without_writing_before_lock(tmp_path):
    import msvcrt
    import subprocess

    path = tmp_path / "empty.lock"
    path.write_bytes(b"")
    script = """
import runpy, sys
from pathlib import Path
vr = runpy.run_path(sys.argv[1])
try:
    with vr['_file_lease'](Path(sys.argv[2])):
        print('acquired')
except vr['PilotBusy']:
    print('busy')
"""
    args = [sys.executable, "-c", script, str(_VOIDRUNNER_PATH), str(path)]
    with path.open("r+b") as owner:
        msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
        blocked = subprocess.run(args, capture_output=True, timeout=5)
        assert blocked.returncode == 0 and not blocked.stderr
        assert blocked.stdout.strip() == b"busy"
    assert path.read_bytes() == b""
    acquired = subprocess.run(args, capture_output=True, timeout=5)
    assert acquired.returncode == 0 and not acquired.stderr
    assert acquired.stdout.strip() == b"acquired" and path.read_bytes() == b""


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
    world.save.cargo = {"weapons": 1}
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
    world.save.cargo = {"food": 2}
    vr._acquire_cargo(world, "food", 3, 100)
    quote = vr.trade_route_quote(world, destination, "food", quantity, use_hold=True)
    cost, unknown = vr._dispose_cargo(world, "food", quantity, proceeds=quote["receipts"], kind="sale")
    assert (quote["cargo_cost"], quote["unknown_units"]) == (cost, unknown)
    assert quote["procurement"] == 0
    assert quote["margin"] is None


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
    world.save.cargo = {"food": 3}
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


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
@pytest.mark.parametrize("screen", ["memory", "route"])
def test_market_memory_and_route_pages_reach_the_end_within_terminal_size(monkeypatch, width, height, screen):
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
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
    output = io.StringIO()
    pages = []
    title = "Market Memory" if screen == "memory" else "Trade Route"

    def choose():
        value = output.getvalue()
        pages.append(value)
        output.seek(0)
        output.truncate(0)
        assert len(pages) < 2000
        match = re.search(re.escape(title) + r" (\d+)/(\d+)", " ".join(value.split()))
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


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
def test_trade_route_destination_picker_pages_keep_selection_and_back_available(monkeypatch, width, height):
    import re
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
    options = [(i, f"Very Long Station Destination Number {i}") for i in range(48)]
    output = io.StringIO()
    pages = []

    def choose():
        value = output.getvalue()
        pages.append(value)
        output.seek(0)
        output.truncate(0)
        match = re.search(r"Destination (\d+)/(\d+)", " ".join(value.split()))
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
        assert "[B]ack" in page


def test_trade_route_editing_fields_and_cancelling_is_read_only(monkeypatch):
    import copy
    world, _ = _world_with_market_memory()
    for sid in (2, 3):
        world.save.current_system = sid
        vr.remember_local_market(world)
    world.save.current_system = 0
    before = copy.deepcopy(world.save.to_dict())
    commands = iter("ED2C2QHHSB")
    monkeypatch.setattr(vr, "read_key", lambda: next(commands))
    monkeypatch.setattr(vr, "read_line_raw", lambda **kwargs: "2")
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_trade_route(vr.Palette(False), world)
    assert "x2" in output.getvalue()
    assert "existing hold cargo" in output.getvalue() and "buy new cargo here" in output.getvalue()
    assert world.save.to_dict() == before


@pytest.mark.parametrize("commands", [b"TMBRBBQ", b"TR", b"TREQ3\nBBBQ", b"TREQ3\n"])
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
    assert b"Trade Route 1/" in result.stdout
    if b"M" in commands:
        assert b"Market Memory 1/" in result.stdout
    if b"E" in commands:
        assert b"Route Draft 1/" in result.stdout and b"Quantity 1-" in result.stdout
    assert (tmp_path / "77.json").read_bytes() == original


def test_real_market_memory_is_saved_after_trade_and_arrival_before_acknowledgement(tmp_path):
    world = _world_with_seed(42)
    world.event_rng.seed(0)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    key = vr.LETTERS[vr.LEGAL_COMMODITIES.index("food")].encode()
    with _door_stopped_at(tmp_path, b"M" + key + b"B2\n", b"Bought 2x"):
        bought, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert bought.market_memory[0]["food"]["buy"] == vr.price_for(vr.World(bought), 0, "food")
    destination = sorted(world.here.connections)[0]
    jump_key = vr.CHART_CONNECTION_LETTERS[0].encode()
    marker = ("Station Services: " + world.by_id[destination].station_name).encode()
    with _door_stopped_at(tmp_path, b"C" + jump_key, marker):
        arrived, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert arrived.current_system == destination and arrived.pending_travel is None
        assert arrived.market_memory[destination]["food"]["day"] == 1
        assert arrived.market_memory[0]["food"] == bought.market_memory[0]["food"]


def test_trading_ledger_preserves_fifo_costs_and_unknown_legacy_stock(tmp_path):
    import json
    world = _world_with_seed(42)
    world.save.cargo = {"food": 2}
    legacy = world.save.to_dict()
    legacy.pop("cargo_basis")
    legacy.pop("trading_ledger")
    world = vr.World(vr.SaveData.from_dict(legacy))
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
    assert ledger.uncosted_sales == 2 * unit
    assert ledger.sales_revenue == unit and ledger.sales_cost == first_cost // 3
    assert world.save.cargo_basis["food"] == [[2, first_cost * 2 // 3], [2, second_cost]]
    vr.trade_cargo(world, "food", 4, buying=False)
    assert ledger.sales_cost == first_cost + second_cost
    assert not world.save.cargo_basis and not world.save.cargo
    assert vr.SaveData.from_dict(json.loads(json.dumps(world.save.to_dict()))).trading_ledger == ledger


def test_trading_ledger_partial_legacy_futures_keeps_exact_paid_remainder():
    world = _world_with_seed(42)
    world.save.active_futures = [vr.FuturesContract(id=1, commodity="food", quantity=3,
                                                   locked_price=100, settle_turn=0)]
    vr.settle_futures_contracts(world)
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


def test_trading_ledger_delivery_allocates_mixed_receipts_and_consumes_basis_once():
    world = _world_with_seed(42)
    world.save.cargo = {"food": 1}
    cost = 2 * vr.price_for(world, 0, "food")
    vr.trade_cargo(world, "food", 2, buying=True)
    world.save.active_missions = [vr.Mission(id=1, kind="delivery", description="Mixed load",
        reward=100, origin_system=0, target_system=0, commodity="food", quantity=3)]
    vr.check_mission_completions(world)
    ledger = world.save.trading_ledger
    assert (ledger.delivery_revenue, ledger.uncosted_deliveries, ledger.delivery_cost) == (67, 33, cost)
    assert ledger.sales_cost == ledger.sales_revenue == 0
    assert not world.save.cargo and not world.save.cargo_basis
    assert vr.check_mission_completions(world) == []
    assert ledger.delivery_revenue == 67


@pytest.mark.parametrize("loss", ["dump", "destroy", "customs", "refused_bribe"])
def test_trading_ledger_records_real_loss_paths(monkeypatch, loss):
    world = _world_with_seed(42)
    world.save.current_system = next(s.id for s in world.galaxy if s.economy == "Haven")
    world.save.cargo = {"weapons": 1}
    cost = 2 * vr.price_for(world, world.here.id, "weapons")
    vr.trade_cargo(world, "weapons", 2, buying=True)
    if loss == "dump":
        vr.dump_all_contraband(world)
    elif loss == "destroy":
        vr.destroy_ship(world)
    else:
        monkeypatch.setattr(vr, "read_key", lambda: "S" if loss == "customs" else "B")
        monkeypatch.setattr(world.event_rng, "random", lambda: 0.99)
        monkeypatch.setattr(vr, "pause", lambda p: None)
        with contextlib.redirect_stdout(io.StringIO()):
            vr.screen_customs(vr.Palette(False), world)
    ledger = world.save.trading_ledger
    assert ledger.cargo_loss_cost == cost and ledger.uncosted_losses == 1
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


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
def test_trading_ledger_pages_fit_and_do_not_write(monkeypatch, width, height):
    import copy
    world = _world_with_seed(42)
    vr.trade_cargo(world, "food", 3, buying=True)
    before = copy.deepcopy(world.save.to_dict())
    rng = world.event_rng.getstate()
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
    count = len(vr._trade_pages(vr.trading_ledger_lines(world), "Trading Ledger", "[M]arkets [R]oute [N]ext [P]rev [B]ack: "))
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
    key = vr.LETTERS[vr.LEGAL_COMMODITIES.index("food")].encode()
    with _door_stopped_at(tmp_path, b"M" + key + b"B3\n", b"Bought 3x"):
        bought, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert bought.cargo_basis == {"food": [[3, cost]]}
        assert bought.market_depth[0]["food"] == {"day": 0, "stock": 45, "demand": 96}
    unit = round(vr.price_for(vr.World(bought), 0, "food") * vr.SELL_SPREAD)
    with _door_stopped_at(tmp_path, b"M" + key + b"S2\n", b"Sold 2x"):
        sold, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert sold.cargo_basis == {"food": [[1, cost // 3]]}
        assert sold.market_depth[0]["food"] == {"day": 0, "stock": 47, "demand": 94}
        assert (sold.trading_ledger.sales_cost, sold.trading_ledger.sales_revenue) == (cost * 2 // 3, 2 * unit)
    with _door_stopped_at(tmp_path, b"T", b"Trading Ledger 1/"):
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
    assert b"Trading Ledger 1/" in result.stdout
    assert (tmp_path / "77.json").read_bytes() == original


def test_trading_ledger_retirement_starts_a_fresh_record():
    world = _world_with_seed(42)
    vr.trade_cargo(world, "food", 2, buying=True)
    vr.trade_cargo(world, "food", 1, buying=False)
    fresh = vr.retire_pilot(world.save)
    assert not fresh.cargo_basis and fresh.trading_ledger == vr.TradingLedger()


def test_trading_ledger_zero_legacy_quantities_do_not_block_cargo_cleanup():
    world = _world_with_seed(42)
    world.save.cargo = {"weapons": 0}
    vr.dump_all_contraband(world)
    assert not world.save.cargo and world.save.trading_ledger.since_day is None


@pytest.mark.parametrize("quantity", [0, 1])
def test_trading_ledger_combat_dump_accounts_only_for_real_cargo(monkeypatch, quantity):
    world = _world_with_seed(42)
    cost = vr.price_for(world, 0, "food") if quantity else 0
    if quantity:
        vr.trade_cargo(world, "food", 1, buying=True)
    else:
        world.save.cargo = {"food": 0}  # Valid older saves can retain zero entries.
    pirate = vr.generate_pirate(world, tier=1)
    monkeypatch.setattr(vr, "read_key", lambda: "D")
    monkeypatch.setattr(world.event_rng, "random", lambda: 0)
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.screen_combat(vr.Palette(False), world, pirate) == "escaped"
    assert world.save.trading_ledger.cargo_loss_cost == cost
    assert world.save.trading_ledger.uncosted_losses == 0
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


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
def test_trade_route_draft_cancel_retains_original_and_fits_pages(monkeypatch, width, height):
    import copy, re
    world, destination = _world_with_market_memory()
    initial = dict(destination=destination, commodity="food", quantity=1, use_hold=False)
    before = copy.deepcopy(world.save.to_dict()); rng = world.event_rng.getstate()
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width); monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
    output = io.StringIO(); frames = []
    def choose():
        frame = output.getvalue(); frames.append(frame); output.seek(0); output.truncate(0)
        match = re.search(r"Route Draft (\d+)/(\d+)", " ".join(frame.split()))
        assert match and len(frames) < 100
        if len(frames) == 1: return "H"
        return "B" if match[1] == match[2] else "N"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output): assert vr._edit_trade_route(world, initial) is None
    assert initial["use_hold"] is False and initial["quantity"] == 1
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng
    assert all(len(frame.splitlines()) <= height for frame in frames)
    assert all(vr._visible_width(line) <= width for frame in frames for line in frame.splitlines())


def test_trade_route_draft_rejection_keeps_edits_until_apply(monkeypatch):
    import copy
    world, destination = _world_with_market_memory()
    initial = dict(destination=destination, commodity="food", quantity=1, use_hold=False)
    before = copy.deepcopy(world.save.to_dict()); rng = world.event_rng.getstate()
    keys = iter("QHSHS")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(vr, "read_line_raw", lambda **kwargs: "3")
    with contextlib.redirect_stdout(io.StringIO()) as output: result = vr._edit_trade_route(world, initial)
    assert result == {**initial, "quantity": 3} and initial["quantity"] == 1
    assert "Cannot apply" in output.getvalue() and "Quantity: 3" in output.getvalue()
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
    world.save.cargo = {"food": 10}
    world.save.market_depth = {0: {"food": {"day": 0, "stock": 0, "demand": 0}}}
    before = copy.deepcopy(world.save.to_dict()); rng = world.event_rng.getstate()
    with pytest.raises(vr.TradeError, match="stock|Station can buy"):
        vr.trade_cargo(world, "food", 1, buying=buying)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


def test_market_depth_split_orders_and_buyback_cannot_restore_station_demand():
    world = _world_with_seed(42)
    world.save.ship.hull_class = "Carrier"; world.save.pilot.credits = 100000
    world.save.cargo = {"food": 100}
    for _ in range(96):
        vr.trade_cargo(world, "food", 1, buying=False)
    vr.trade_cargo(world, "food", 1, buying=True)
    assert vr.market_depth_quote(world, 0, "food")["demand"] == 0
    with pytest.raises(vr.TradeError, match="Station can buy 0"):
        vr.trade_cargo(world, "food", 1, buying=False)
    world.save.turn = 1
    vr.trade_cargo(world, "food", 5, buying=False)
    assert vr.market_depth_quote(world, 0, "food")["demand"] == 1


def test_market_depth_old_career_and_future_wholesale_terms_remain_usable():
    import copy
    world = _world_with_seed(42)
    data = copy.deepcopy(world.save.to_dict()); data.pop("market_depth")
    restored = vr.World(vr.SaveData.from_dict(data))
    assert restored.save.market_depth == {} and vr.market_depth_quote(restored, 0, "food")["stock"] == 48
    restored.save.market_depth = {0: {"food": {"day": 0, "stock": 0, "demand": 0}}}
    restored.save.pilot.credits = 100000
    vr.buy_futures_contract(restored, "food", 10, 5)
    restored.save.turn = 5
    vr.settle_futures_contracts(restored)
    assert restored.save.cargo["food"] == 10
    assert restored.save.trading_ledger.since_day == 5
    assert restored.save.market_depth[0]["food"] == {"day": 0, "stock": 0, "demand": 0}


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


@pytest.mark.parametrize("commands,buying", [(["B"], True), (["S"], False)])
def test_market_depth_exhausted_pool_reports_reason_without_quantity_prompt(monkeypatch, commands, buying):
    import contextlib, io
    world = _world_with_seed(42); world.save.cargo = {"food": 3}
    world.save.market_depth = {0: {"food": {"day": 0, "stock": 0, "demand": 0}}}
    keys = iter(commands); monkeypatch.setattr(vr, "read_command", lambda: next(keys))
    monkeypatch.setattr(vr, "read_line_raw", lambda **kwargs: pytest.fail("Exhausted pool prompted for quantity"))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf): vr._trade_commodity(vr.Palette(False), world, "food")
    text = " ".join(buf.getvalue().split())
    assert "Stock 0 (+3/day)" in text and "station buys 0 (+6/day)" in text
    assert "station stock" in text if buying else "demand is exhausted" in text


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
def test_market_depth_commodity_details_fit_every_page_without_replenishing(monkeypatch, width, height):
    import copy, re
    world = _world_with_seed(42); world.save.market_depth = {0: {"metals": {"day": 0, "stock": 3, "demand": 4}}}
    before = copy.deepcopy(world.save.to_dict()); rng = world.event_rng.getstate()
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width); monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
    output = io.StringIO(); frames = []
    def choose():
        frame = output.getvalue(); frames.append(frame); output.seek(0); output.truncate(0)
        match = re.search(r"Refined Metals Exchange (\d+)/(\d+)", " ".join(frame.split()))
        assert match and len(frames) < 100
        return "Q" if match[1] == match[2] else "N"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output): vr._trade_commodity(vr.Palette(False), world, "metals")
    assert "Stock 3 (+6/day)" in " ".join(" ".join(frames).split())
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng
    assert all(len(frame.splitlines()) <= height for frame in frames)
    assert all(vr._visible_width(line) <= width for frame in frames for line in frame.splitlines())


def test_market_depth_opening_assignment_does_not_quote_unavailable_procurement():
    world = _world_with_seed(42)
    assert vr.opening_assignment_offer(world) is not None
    world.save.market_depth[0] = {c: {"day": 0, "stock": 0, "demand": 0} for c in vr.COMMODITIES}
    assert vr.opening_assignment_offer(world) is None


def test_market_depth_contract_delivery_preserves_signed_terms_when_spot_demand_is_zero():
    world = _world_with_seed(42); world.save.cargo = {"food": 3}
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


# -- galaxy generation -------------------------------------------------


def test_generate_galaxy_is_a_pure_function_of_seed():
    a = vr.generate_galaxy(12345)
    b = vr.generate_galaxy(12345)
    assert [s.name for s in a] == [s.name for s in b]
    assert [s.economy for s in a] == [s.economy for s in b]
    assert [sorted(s.connections) for s in a] == [sorted(s.connections) for s in b]


def test_different_seeds_usually_produce_different_galaxies():
    a = vr.generate_galaxy(1)
    b = vr.generate_galaxy(2)
    assert [s.name for s in a] != [s.name for s in b]


def test_galaxy_is_fully_connected_from_home_system():
    galaxy = vr.generate_galaxy(999)
    by_id = {s.id: s for s in galaxy}
    reachable = vr.bfs_hops(by_id, 0)
    assert len(reachable) == len(galaxy)


def test_home_system_and_its_neighbors_start_discovered_nothing_else_does():
    galaxy = vr.generate_galaxy(42)
    home = galaxy[0]
    assert home.discovered is True
    for sid in home.connections:
        assert galaxy[sid].discovered is True
    far_systems = [s for s in galaxy if s.id != 0 and s.id not in home.connections]
    assert any(not s.discovered for s in far_systems)


def test_home_systems_direct_neighbors_never_exceed_a_safe_danger_ceiling():
    """Dogfood-caught: every system's danger tier is drawn from the same
    distribution regardless of distance from home, so before this fix a
    galaxy could seed a near-unwinnable tier-4 raider system one jump
    from Freeport, before a new character had any chance to earn a
    single upgrade. Checked across a wide range of seeds, not just one,
    since the original bug only showed up for *some* seeds -- a single
    lucky seed passing would have hidden the regression."""
    for seed in range(200):
        galaxy = vr.generate_galaxy(seed)
        home = galaxy[0]
        for nid in home.connections:
            assert galaxy[nid].danger <= 2, f"seed={seed} neighbor={nid} danger={galaxy[nid].danger}"


def test_every_system_has_at_least_one_connection():
    galaxy = vr.generate_galaxy(7)
    assert all(len(s.connections) >= 1 for s in galaxy)


# -- economy -------------------------------------------------------------


def _world_with_seed(seed: int) -> "vr.World":
    save = vr._new_career("Tester")
    save.seed = seed
    return vr.World(save)


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


# -- ship derived stats ---------------------------------------------------


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


# -- hull class ladder (branching Shuttle -> Freighter|Cutter -> Carrier) --


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

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr._hull_refit_screen(vr.Palette(truecolor=False), world, "Freighter", 15_000)

    assert world.save.ship.hull_class == "Shuttle"
    assert world.save.pilot.credits == 100
    assert "Need" in buf.getvalue()


def test_hull_refit_screen_declining_confirmation_makes_no_change(monkeypatch):
    world = _world_with_seed(31)
    world.save.pilot.credits = 20_000

    monkeypatch.setattr(vr, "read_key", lambda: "N")
    with contextlib.redirect_stdout(io.StringIO()):
        vr._hull_refit_screen(vr.Palette(truecolor=False), world, "Freighter", 15_000)

    assert world.save.ship.hull_class == "Shuttle"
    assert world.save.pilot.credits == 20_000


def test_hull_refit_screen_applies_the_refit_charges_credits_and_resets_hull_to_new_max(monkeypatch):
    world = _world_with_seed(32)
    world.save.pilot.credits = 20_000
    world.save.ship.hull_hp = 10  # damaged, below Shuttle's own max

    monkeypatch.setattr(vr, "read_key", lambda: "Y")
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

    monkeypatch.setattr(vr, "read_key", lambda: "Y")
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

    keys = iter(["G", "Y", "Q"])  # pick the first refit slot (Freighter), confirm, then leave
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_shipyard(vr.Palette(truecolor=False), world)

    text = buf.getvalue()
    assert "[G]" in text and "Freighter-Class Refit" in text
    assert "[H]" in text and "Cutter-Class Refit" in text
    assert world.save.ship.hull_class == "Freighter"


# -- save round-tripping ---------------------------------------------------


def test_save_data_round_trips_through_dict_including_missions_and_none_fields():
    save = vr._new_career("Roundtrip")
    save.active_missions.append(vr.Mission(
        id=1, kind="scan", description="survey it", reward=250,
        origin_system=0, target_system=5, deadline_turn=None,
    ))
    save.market_drift[3] = {"food": 1.2}
    restored = vr.SaveData.from_dict(save.to_dict())
    assert restored.pilot.handle == "Roundtrip"
    assert restored.active_missions[0].deadline_turn is None
    assert restored.active_missions[0].target_system == 5
    assert restored.market_drift[3]["food"] == 1.2


def test_write_and_load_save_round_trips_on_disk(tmp_path):
    save = vr._new_career("Disky")
    save.pilot.credits = 4321
    vr.write_save(tmp_path, user_id=77, save=save)

    loaded, is_new, notice = vr.load_or_create_save(tmp_path, user_id=77, handle="Disky")
    assert is_new is False
    assert notice is None
    assert loaded.pilot.credits == 4321
    assert loaded.seed == save.seed


def test_loading_an_existing_save_never_overwrites_the_chosen_callsign(tmp_path):
    """Dogfood-caught: a live login handle is only ever the *default*
    callsign at character creation -- once a save exists, the pilot's
    own chosen callsign must survive regardless of what the current
    login handle says, including when it's unchanged, changed, or a
    totally different account (a save is keyed by stable user_id, never
    handle -- see the module's own docstring). A prior version
    unconditionally wrote the login handle over the saved callsign on
    every single load, silently discarding it."""
    save = vr._new_career("Claude")
    save.pilot.handle = "Voyager1"  # the player's own chosen callsign
    vr.write_save(tmp_path, user_id=99, save=save)

    loaded, is_new, notice = vr.load_or_create_save(tmp_path, user_id=99, handle="Claude")

    assert is_new is False
    assert loaded.pilot.handle == "Voyager1"


def test_corrupt_save_is_preserved_in_place_without_automatic_reset(tmp_path):
    path = tmp_path / "5.json"
    path.write_text("not valid json{{{", encoding="utf-8")
    with pytest.raises(vr.ResumeError):
        vr.load_or_create_save(tmp_path, user_id=5, handle="Recovered")
    assert path.read_text(encoding="utf-8") == "not valid json{{{"
    assert not list(tmp_path.glob("5.corrupt-*"))


def test_write_save_is_atomic_no_tmp_file_left_behind(tmp_path):
    save = vr._new_career("Atomic")
    vr.write_save(tmp_path, user_id=9, save=save)
    assert (tmp_path / "9.json").exists()
    assert not (tmp_path / "9.json.tmp").exists()


# -- missions --------------------------------------------------------------


def _post_and_accept_test_mission(world, mission):
    """Post authored test terms before exercising the real acceptance command."""
    board = world.save.mission_boards.setdefault(world.save.current_system, {
        "refresh_turn": world.save.turn + vr.MISSION_BOARD_DAYS, "offers": [],
    })
    board["offers"].append(mission.to_dict())
    vr.accept_mission(world, mission)


def test_delivery_mission_completes_on_arrival_with_enough_cargo():
    world = _world_with_seed(4)
    dest = world.here.connections[0]
    mission = vr.Mission(id=1, kind="delivery", description="test delivery", reward=500,
                          origin_system=world.save.current_system, target_system=dest,
                          commodity="food", quantity=3, deadline_turn=None)
    _post_and_accept_test_mission(world, mission)
    world.save.cargo["food"] = 5
    world.save.current_system = dest

    messages = vr.check_mission_completions(world)

    assert any("Mission complete" in m for m in messages)
    assert world.save.cargo["food"] == 2
    assert world.save.pilot.credits == 1200 + 500
    assert mission not in world.save.active_missions


def test_delivery_mission_does_not_complete_with_insufficient_cargo():
    world = _world_with_seed(5)
    dest = world.here.connections[0]
    mission = vr.Mission(id=1, kind="delivery", description="test delivery", reward=500,
                          origin_system=world.save.current_system, target_system=dest,
                          commodity="food", quantity=3, deadline_turn=None)
    _post_and_accept_test_mission(world, mission)
    world.save.cargo["food"] = 1
    world.save.current_system = dest

    messages = vr.check_mission_completions(world)

    assert messages == []
    assert mission in world.save.active_missions


def test_expired_mission_is_dropped_with_a_message():
    world = _world_with_seed(6)
    mission = vr.Mission(id=1, kind="delivery", description="late delivery", reward=500,
                          origin_system=world.save.current_system, target_system=999,
                          commodity="food", quantity=3, deadline_turn=0)
    _post_and_accept_test_mission(world, mission)
    world.save.turn = 10

    messages = vr.check_mission_completions(world)

    assert any("expired" in m for m in messages)
    assert mission not in world.save.active_missions


def test_scan_mission_completes_when_target_system_is_discovered():
    world = _world_with_seed(8)
    mission = vr.Mission(id=1, kind="scan", description="survey", reward=200,
                          origin_system=world.save.current_system, target_system=17,
                          deadline_turn=None)
    _post_and_accept_test_mission(world, mission)

    messages = vr.check_mission_completions(world, just_discovered=17)

    assert any("Mission complete" in m for m in messages)
    assert world.save.pilot.credits == 1200 + 200


# -- bounty missions in screen_travel (dogfood-caught: losing used to
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
    world.save.cargo[vr.CONTRABAND_COMMODITIES[0]] = 3
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


def test_random_encounter_pirate_tier_still_uses_origin_system_danger(monkeypatch):
    """Guards the exact regression the "New system charted" reordering
    above had to avoid: generate_pirate's own tier defaults to
    `world.here.danger` (deliberately the *origin* system, not the
    destination -- see generate_pirate_squadron's own docstring), which
    only stays correct as long as `world.save.current_system` isn't
    flipped to the destination before the encounter resolves."""
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

    def spy_generate_pirate(w, tier=None):
        pirate = real_generate_pirate(w, tier=tier)
        captured_tiers.append(pirate.tier)
        return pirate

    monkeypatch.setattr(vr, "generate_pirate", spy_generate_pirate)
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "escaped")

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest_id)

    assert captured_tiers
    assert captured_tiers[0] == origin.danger  # not dest.danger (0)


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


# -- combat ------------------------------------------------------------


def test_fight_round_damages_both_sides_and_is_driven_by_world_event_rng():
    world = _world_with_seed(9)
    world.event_rng = random.Random(1)
    pirate = vr.Pirate(name="Test Raider", tier=1, hp=35, hp_max=35)
    starting_hull = world.save.ship.hull_hp

    dmg_to_pirate, dmg_to_player, lines = vr.fight_round(world, pirate)

    assert dmg_to_pirate > 0
    assert pirate.hp == 35 - dmg_to_pirate
    assert world.save.ship.hull_hp == starting_hull - dmg_to_player
    assert lines


def test_higher_weapon_tier_deals_more_damage_with_same_rng_sequence():
    world_weak = _world_with_seed(10)
    world_weak.event_rng = random.Random(42)
    world_strong = _world_with_seed(10)
    world_strong.event_rng = random.Random(42)
    world_strong.save.ship.weapon_tier = 4

    pirate_weak = vr.Pirate(name="X", tier=2, hp=100, hp_max=100)
    pirate_strong = vr.Pirate(name="X", tier=2, hp=100, hp_max=100)

    dmg_weak, _, _ = vr.fight_round(world_weak, pirate_weak)
    dmg_strong, _, _ = vr.fight_round(world_strong, pirate_strong)

    assert dmg_strong > dmg_weak


def test_shields_reduce_incoming_damage():
    world_bare = _world_with_seed(11)
    world_bare.event_rng = random.Random(7)
    world_shielded = _world_with_seed(11)
    world_shielded.event_rng = random.Random(7)
    world_shielded.save.ship.shield_tier = 3

    pirate_a = vr.Pirate(name="Y", tier=3, hp=1000, hp_max=1000)  # never dies mid-round
    pirate_b = vr.Pirate(name="Y", tier=3, hp=1000, hp_max=1000)

    _, dmg_bare, _ = vr.fight_round(world_bare, pirate_a)
    _, dmg_shielded, _ = vr.fight_round(world_shielded, pirate_b)

    assert dmg_shielded <= dmg_bare


# -- squadron fights ------------------------------------------------------


def _system_with_danger(world, danger: int):
    system = world.by_id[world.here.connections[0]]
    system.danger = danger
    return system


def test_squadron_never_spawns_below_the_minimum_danger():
    world = _world_with_seed(90)
    dest = _system_with_danger(world, vr.SQUADRON_MIN_DANGER - 1)
    world.event_rng.random = lambda: 0.0  # would always succeed the squadron roll, if it ran at all

    pirates = vr.generate_pirate_squadron(world, dest)

    assert len(pirates) == 1


def test_squadron_spawns_at_the_minimum_danger_when_the_roll_succeeds():
    world = _world_with_seed(91)
    dest = _system_with_danger(world, vr.SQUADRON_MIN_DANGER)
    world.event_rng.random = lambda: 0.0  # always below SQUADRON_CHANCE

    pirates = vr.generate_pirate_squadron(world, dest)

    assert len(pirates) == vr.SQUADRON_SIZE


def test_squadron_does_not_spawn_at_high_danger_when_the_roll_fails():
    world = _world_with_seed(92)
    dest = _system_with_danger(world, vr.SQUADRON_MIN_DANGER)
    world.event_rng.random = lambda: 1.0  # always above SQUADRON_CHANCE

    pirates = vr.generate_pirate_squadron(world, dest)

    assert len(pirates) == 1


def test_squadron_encounter_fights_every_ship_on_a_full_win(monkeypatch):
    world = _world_with_seed(93)
    dest = _system_with_danger(world, vr.SQUADRON_MIN_DANGER)
    world.event_rng.random = lambda: 0.0  # triggers the encounter, then spawns a squadron

    calls = []
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: calls.append(pirate) or "won")
    world.event_rng.choices = lambda population, weights: ["pirate"]

    with contextlib.redirect_stdout(io.StringIO()):
        vr._resolve_random_travel_encounter(vr.Palette(truecolor=False), world, dest)

    assert len(calls) == vr.SQUADRON_SIZE


def test_squadron_encounter_stops_after_an_escape_not_a_full_win(monkeypatch):
    world = _world_with_seed(94)
    dest = _system_with_danger(world, vr.SQUADRON_MIN_DANGER)
    world.event_rng.random = lambda: 0.0

    calls = []
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: calls.append(pirate) or "escaped")
    world.event_rng.choices = lambda population, weights: ["pirate"]

    with contextlib.redirect_stdout(io.StringIO()):
        vr._resolve_random_travel_encounter(vr.Palette(truecolor=False), world, dest)

    assert len(calls) == 1  # never reached the second ship


def test_destroy_ship_clears_cargo_and_returns_player_to_freeport_with_full_hull():
    world = _world_with_seed(12)
    world.save.cargo["ore"] = 10
    world.save.current_system = world.here.connections[0]
    world.save.ship.hull_hp = 0
    world.save.pilot.notoriety = 7

    vr.destroy_ship(world)

    assert world.save.cargo == {}
    assert world.save.current_system == 0
    assert world.save.ship.hull_hp == vr.hull_hp_max(world.save.ship)
    assert world.save.pilot.credits < 1200  # salvage fee charged
    assert world.save.pilot.notoriety == 0  # any ship loss wipes wanted status


# -- travel encounter variety -------------------------------------------


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
    vr.read_key = lambda: "B"
    world.event_rng.random = lambda: 0.0  # always the salvage-success branch

    with contextlib.redirect_stdout(io.StringIO()):
        vr._encounter_derelict(vr.Palette(truecolor=False), world)

    assert world.save.pilot.credits > before_credits
    assert len(world.save.pilot.log) == before_log_len + 1


def test_derelict_board_trap_triggers_combat(monkeypatch):
    world = _world_with_seed(55)
    vr.read_key = lambda: "B"
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


# -- player notoriety ----------------------------------------------------


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
    world.save.cargo["narcotics"] = 5
    world.save.pilot.credits = 0  # can't afford the bribe cost -> refused path
    vr.read_key = lambda: "B"

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_customs(vr.Palette(truecolor=False), world)

    assert world.save.pilot.notoriety == vr.NOTORIETY_PER_CUSTOMS_BUST


def test_customs_cooperative_surrender_does_not_raise_notoriety():
    world = _world_with_seed(72)
    world.save.cargo["narcotics"] = 5
    vr.read_key = lambda: "S"

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_customs(vr.Palette(truecolor=False), world)

    assert world.save.pilot.notoriety == 0


def test_customs_successful_bribe_does_not_raise_notoriety():
    world = _world_with_seed(73)
    world.save.cargo["narcotics"] = 5
    world.save.pilot.credits = 10_000
    world.event_rng.random = lambda: 0.0  # always the bribe-succeeds branch
    vr.read_key = lambda: "B"

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_customs(vr.Palette(truecolor=False), world)

    assert world.save.pilot.notoriety == 0


def test_wrong_bounty_kill_raises_notoriety_and_lowers_concord_rep(monkeypatch):
    world = _world_with_seed(74)
    dest_id = world.here.connections[0]
    _accept_bounty(world, target_system=dest_id)
    before_rep = world.save.pilot.reputation[vr.FACTION_CONCORD]
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "won")
    world.event_rng.random = lambda: 0.0  # always triggers the wrong-kill roll

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest_id)

    assert world.save.pilot.notoriety == vr.NOTORIETY_PER_WRONG_BOUNTY_KILL
    assert world.save.pilot.reputation[vr.FACTION_CONCORD] < before_rep


def test_bounty_win_without_the_wrong_kill_roll_leaves_notoriety_at_zero(monkeypatch):
    world = _world_with_seed(75)
    dest_id = world.here.connections[0]
    _accept_bounty(world, target_system=dest_id)
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "won")
    world.event_rng.random = lambda: 1.0  # never triggers the wrong-kill roll

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

    assert "Surrender" not in buf.getvalue()
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

    monkeypatch.setattr(vr, "fight_round", _one_shot_kill)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_notoriety_patrol(vr.Palette(truecolor=False), world)

    assert world.save.pilot.notoriety == 4 + 3
    assert world.save.pilot.reputation[vr.FACTION_CONCORD] < before_concord
    assert world.save.pilot.reputation[vr.FACTION_BLACKWAKE] > before_blackwake


def test_notoriety_patrol_loss_wipes_notoriety_via_destroy_ship(monkeypatch):
    world = _world_with_seed(82)
    world.save.pilot.notoriety = 10
    vr.read_key = lambda: "F"

    def _one_shot_loss(world, patrol):
        world.save.ship.hull_hp = 0
        return 0, 999, ["one-shot loss"]

    monkeypatch.setattr(vr, "fight_round", _one_shot_loss)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_notoriety_patrol(vr.Palette(truecolor=False), world)

    assert world.save.pilot.notoriety == 0
    assert world.save.current_system == 0


# -- stranded-pilot rescue --------------------------------------------


def _cheapest_jump_cost(world) -> int:
    here = world.here
    return min(vr.fuel_cost_for_jump(here, world.by_id[nid]) for nid in here.connections)


def test_not_stranded_with_cargo_even_at_zero_fuel_and_credits(monkeypatch):
    world = _world_with_seed(20)
    world.save.current_system = world.here.connections[0]
    world.save.ship.fuel = 0
    world.save.pilot.credits = 0
    world.save.cargo["ore"] = 1
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


# -- retirement / New Game+ ------------------------------------------------


def test_retire_pilot_increments_retirements_and_grants_cumulative_bonus():
    old_save = vr._new_career("Vet")
    old_save.pilot.retirements = 2
    old_save.pilot.credits = 300_000

    new_save = vr.retire_pilot(old_save)

    assert new_save.pilot.retirements == 3
    assert new_save.pilot.credits == 1200 + 3 * vr.RETIREMENT_STARTING_CREDITS_BONUS


def test_retire_pilot_resets_career_progress_and_keeps_handle():
    old_save = vr._new_career("Vet")
    old_save.pilot.handle = "Vet"
    old_save.pilot.credits = 300_000
    old_save.pilot.kills = 40
    old_save.pilot.missions_completed = 12
    old_save.pilot.notoriety = 9
    old_save.pilot.reputation["pirates"] = 5
    old_save.pilot.log.append("did something memorable")
    old_save.discovered = [0, 1, 2, 3]

    new_save = vr.retire_pilot(old_save)

    assert new_save.pilot.handle == "Vet"
    assert new_save.pilot.kills == 0
    assert new_save.pilot.missions_completed == 0
    assert new_save.pilot.notoriety == 0
    assert all(v == 0 for v in new_save.pilot.reputation.values())
    assert new_save.discovered == [0]
    assert any("Retired" in entry for entry in new_save.pilot.log)


def test_retire_pilot_rerolls_the_galaxy_seed():
    old_save = vr._new_career("Vet")
    original_seed = old_save.seed

    new_save = vr.retire_pilot(old_save)

    assert new_save.seed != original_seed


def test_pilot_from_dict_defaults_retirements_to_zero_for_old_saves():
    d = vr._new_career("Legacy").pilot.to_dict()
    del d["retirements"]

    pilot = vr.Pilot.from_dict(d)

    assert pilot.retirements == 0


def test_screen_status_offers_retirement_only_at_top_rank(monkeypatch):
    world = _world_with_seed(95)
    world.save.pilot.credits = 100  # far below top rank

    monkeypatch.setattr(vr, "read_key", lambda: (_ for _ in ()).throw(AssertionError("should not prompt")))
    monkeypatch.setattr(vr, "pause", lambda p, msg="Press any key to continue...": None)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_status(vr.Palette(truecolor=False), world)


def test_screen_status_retires_on_confirmation_at_top_rank(monkeypatch):
    world = _world_with_seed(96)
    world.save.pilot.credits = vr.RANKS[-1][0]
    old_seed = world.save.seed

    keys = iter(["R", "Y"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_status(vr.Palette(truecolor=False), world)

    assert world.save.pilot.retirements == 1
    assert world.save.seed != old_seed


def test_screen_status_declines_retirement_without_committing(monkeypatch):
    world = _world_with_seed(97)
    world.save.pilot.credits = vr.RANKS[-1][0]
    old_seed = world.save.seed

    keys = iter(["R", "N"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_status(vr.Palette(truecolor=False), world)

    assert world.save.pilot.retirements == 0
    assert world.save.seed == old_seed


# -- auto-route -------------------------------------------------------------


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


def test_auto_route_rejects_an_unknown_system_name(monkeypatch):
    world = _world_with_seed(101)
    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: "Nonexistent Place")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr._screen_auto_route(vr.Palette(truecolor=False), world)

    assert "no charted system matches" in buf.getvalue().lower()
    assert world.save.current_system == 0


def test_auto_route_wont_match_an_undiscovered_system_by_name(monkeypatch):
    world = _world_with_seed(102)
    undiscovered = next(s for s in world.galaxy if not s.discovered)

    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: undiscovered.name)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr._screen_auto_route(vr.Palette(truecolor=False), world)

    assert "no charted system matches" in buf.getvalue().lower()


def test_auto_route_declines_without_enough_fuel(monkeypatch):
    world = _world_with_seed(103)
    dest = next(s for s in world.galaxy if s.discovered and s.id != 0)
    world.save.ship.fuel = 0

    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: dest.name)
    calls = []
    monkeypatch.setattr(vr, "screen_travel", lambda p, w, hop_id: calls.append(hop_id))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr._screen_auto_route(vr.Palette(truecolor=False), world)

    assert "not enough fuel" in buf.getvalue().lower()
    assert calls == []


def test_auto_route_travels_every_hop_on_confirmation(monkeypatch):
    world = _world_with_seed(104)
    dest = next(s for s in world.galaxy if s.discovered and s.id != 0)
    world.save.ship.fuel = 999
    expected_path = vr.bfs_path(world.by_id, 0, dest.id)

    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: dest.name)
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)
    calls = []

    def fake_travel(p, w, hop_id):
        calls.append(hop_id)
        w.save.current_system = hop_id

    monkeypatch.setattr(vr, "screen_travel", fake_travel)

    with contextlib.redirect_stdout(io.StringIO()):
        vr._screen_auto_route(vr.Palette(truecolor=False), world)

    assert calls == expected_path
    assert world.save.current_system == dest.id


def test_auto_route_declines_on_confirmation_refusal(monkeypatch):
    world = _world_with_seed(105)
    dest = next(s for s in world.galaxy if s.discovered and s.id != 0)
    world.save.ship.fuel = 999

    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: dest.name)
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: False)
    calls = []
    monkeypatch.setattr(vr, "screen_travel", lambda p, w, hop_id: calls.append(hop_id))

    with contextlib.redirect_stdout(io.StringIO()):
        vr._screen_auto_route(vr.Palette(truecolor=False), world)

    assert calls == []


def test_auto_route_disambiguation_reserves_q_for_cancel_with_many_matches(monkeypatch):
    """Regression guard for a real dogfood-caught bug: a short, common
    substring can match 17+ discovered systems at once, and with plain
    `LETTERS` that reaches "Q" as a real row letter (the 17th) --
    colliding with this same prompt's own "[Q] cancel", so pressing Q
    would silently pick a system instead of backing out."""
    world = _world_with_seed(200)
    for system in world.galaxy:
        system.discovered = True  # every system is now a candidate for a 1-char search
    world.save.ship.fuel = 999

    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: "a")
    calls = []
    monkeypatch.setattr(vr, "screen_travel", lambda p, w, hop_id: calls.append(hop_id))
    # A single "Q" -- if this regresses, the buggy path selects a system
    # instead of canceling and falls through to `confirm()`, whose own
    # read-until-Y/N loop would call read_key() again; StopIteration
    # fails the test fast instead of hanging forever on a constant mock.
    keys = iter(["Q"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))

    with contextlib.redirect_stdout(io.StringIO()):
        vr._screen_auto_route(vr.Palette(truecolor=False), world)

    assert calls == []


def test_auto_route_stops_early_when_a_hop_diverts_the_plan(monkeypatch):
    """Simulates a mid-route ship loss: `screen_travel` sends the pilot
    back to Freeport instead of the planned hop, and the route must not
    barrel on to the next hop regardless."""
    world = _world_with_seed(106)
    dest = next(s for s in world.galaxy if s.id != 0 and len(vr.bfs_path(world.by_id, 0, s.id)) >= 2)
    dest.discovered = True  # otherwise no far-away system is nameable yet, this early in a career
    world.save.ship.fuel = 999

    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: dest.name)
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)
    calls = []

    def fake_travel(p, w, hop_id):
        calls.append(hop_id)
        w.save.current_system = 0  # diverted back to Freeport

    monkeypatch.setattr(vr, "screen_travel", fake_travel)

    with contextlib.redirect_stdout(io.StringIO()):
        vr._screen_auto_route(vr.Palette(truecolor=False), world)

    assert len(calls) == 1


# -- landmark systems --------------------------------------------------


def test_generate_landmark_never_picks_freeport():
    world = _world_with_seed(107)
    landmark = vr.generate_landmark(world.save.seed, world.galaxy)
    assert landmark["system_id"] != 0


def test_generate_landmark_is_deterministic_for_the_same_seed():
    world = _world_with_seed(108)
    first = vr.generate_landmark(world.save.seed, world.galaxy)
    second = vr.generate_landmark(world.save.seed, world.galaxy)
    assert first == second


def test_generate_landmark_does_not_call_the_galaxy_rng():
    """The seed-determinism invariant only allows appending brand-new
    `random.Random` calls at the very end of `generate_galaxy` itself --
    proves landmark generation uses a wholly separate RNG instance and
    never perturbs an existing save's galaxy layout."""
    seed = 109
    before = vr.generate_galaxy(seed)
    vr.generate_landmark(seed, before)
    after = vr.generate_galaxy(seed)
    for a, b in zip(before, after):
        assert a.x == b.x and a.y == b.y and a.connections == b.connections


def test_world_reset_computes_a_landmark():
    world = _world_with_seed(110)
    assert world.landmark["system_id"] in world.by_id
    assert world.landmark["system_id"] != 0


def test_landmark_available_here_true_only_at_the_landmark_system_and_uninvestigated():
    world = _world_with_seed(111)
    world.save.current_system = world.landmark["system_id"]
    assert vr.landmark_available_here(world)

    world.save.flags["landmark_investigated"] = True
    assert not vr.landmark_available_here(world)


def test_landmark_available_here_false_elsewhere():
    world = _world_with_seed(112)
    other = next(sid for sid in world.by_id if sid != world.landmark["system_id"])
    world.save.current_system = other
    assert not vr.landmark_available_here(world)


def test_screen_landmark_grants_reward_once_and_sets_flag():
    world = _world_with_seed(113)
    world.save.current_system = world.landmark["system_id"]
    before_credits = world.save.pilot.credits

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_landmark(vr.Palette(truecolor=False), world)

    assert world.save.flags["landmark_investigated"] is True
    assert world.save.pilot.credits == before_credits + world.landmark["reward_credits"]
    assert not vr.landmark_available_here(world)


def test_station_menu_offers_landmark_only_when_available(monkeypatch):
    world = _world_with_seed(114)
    world.save.current_system = world.landmark["system_id"]

    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)

    assert "[L]" in buf.getvalue()

    world.save.flags["landmark_investigated"] = True
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)

    assert "[L]" not in buf2.getvalue()


def test_retiring_resets_landmark_investigated_flag():
    old_save = vr._new_career("Vet")
    old_save.flags["landmark_investigated"] = True

    new_save = vr.retire_pilot(old_save)

    assert "landmark_investigated" not in new_save.flags


# -- career highlights -------------------------------------------------


def test_pilot_highlight_appends_and_caps_at_max_highlights():
    save = vr._new_career("Recorder")
    for i in range(vr.MAX_HIGHLIGHTS + 5):
        save.pilot.highlight(f"Event {i}")
    assert len(save.pilot.highlights) == vr.MAX_HIGHLIGHTS
    assert save.pilot.highlights[-1] == f"Event {vr.MAX_HIGHLIGHTS + 4}"
    assert save.pilot.highlights[0] == "Event 5"


def test_pilot_from_dict_defaults_highlights_and_rank_seen_for_old_saves():
    d = vr._new_career("Legacy").pilot.to_dict()
    del d["highlights"]
    del d["highest_rank_seen"]

    pilot = vr.Pilot.from_dict(d)

    assert pilot.highlights == []
    assert pilot.highest_rank_seen == 0


def test_check_rank_up_fires_once_per_rank_and_records_a_highlight():
    world = _world_with_seed(115)
    world.save.pilot.credits = vr.RANKS[1][0]

    title = vr.check_rank_up(world)
    assert title == vr.RANKS[1][1]
    assert world.save.pilot.highest_rank_seen == 1
    assert any("Promoted" in h for h in world.save.pilot.highlights)

    # Same rank again -- must not re-fire.
    assert vr.check_rank_up(world) is None


def test_check_rank_up_does_not_fire_for_a_fresh_career():
    world = _world_with_seed(116)
    assert vr.check_rank_up(world) is None


def test_check_rank_up_skips_ahead_correctly_on_a_big_jump():
    world = _world_with_seed(117)
    world.save.pilot.credits = vr.RANKS[-1][0]

    title = vr.check_rank_up(world)

    assert title == vr.RANKS[-1][1]
    assert world.save.pilot.highest_rank_seen == len(vr.RANKS) - 1


def test_first_kill_records_a_highlight_but_not_the_second(monkeypatch):
    world = _world_with_seed(118)
    pirate = vr.generate_pirate(world, tier=1)

    def win_the_fight(w, target):
        target.hp = 0
        return (0, 0, [])

    monkeypatch.setattr(vr, "fight_round", win_the_fight)
    monkeypatch.setattr(vr, "read_key", lambda: "F")

    with contextlib.redirect_stdout(io.StringIO()):
        outcome = vr.screen_combat(vr.Palette(truecolor=False), world, pirate)
    assert outcome == "won"
    assert sum("First kill" in h for h in world.save.pilot.highlights) == 1

    pirate2 = vr.generate_pirate(world, tier=1)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_combat(vr.Palette(truecolor=False), world, pirate2)
    assert sum("First kill" in h for h in world.save.pilot.highlights) == 1


def test_first_mission_completion_records_a_highlight():
    world = _world_with_seed(119)
    mission = vr.Mission(id=1, kind="delivery", description="Haul food", reward=100,
                          origin_system=0, target_system=0, commodity="food", quantity=1)
    world.save.active_missions.append(mission)
    world.save.cargo["food"] = 1

    msgs = vr.check_mission_completions(world)

    assert msgs
    assert any("First mission" in h for h in world.save.pilot.highlights)


def test_hull_refit_records_a_highlight(monkeypatch):
    world = _world_with_seed(120)
    world.save.pilot.credits = 100_000
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)

    with contextlib.redirect_stdout(io.StringIO()):
        vr._hull_refit_screen(vr.Palette(truecolor=False), world, "Freighter", 5000)

    assert any("Freighter-class hull refit" in h for h in world.save.pilot.highlights)


def test_landmark_investigation_records_a_highlight():
    world = _world_with_seed(121)
    world.save.current_system = world.landmark["system_id"]

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_landmark(vr.Palette(truecolor=False), world)

    assert any(world.landmark["label"] in h for h in world.save.pilot.highlights)


def test_retire_pilot_records_a_highlight_on_the_new_career():
    old_save = vr._new_career("Vet")
    old_save.pilot.credits = vr.RANKS[-1][0]

    new_save = vr.retire_pilot(old_save)

    assert any("Retired" in h for h in new_save.pilot.highlights)


def test_screen_status_shows_career_highlights(monkeypatch):
    world = _world_with_seed(122)
    world.save.pilot.highlight("Something notable happened.")
    monkeypatch.setattr(vr, "read_key", lambda: " ")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_status(vr.Palette(truecolor=False), world)

    assert "Career highlights" in buf.getvalue()
    assert "Something notable happened." in buf.getvalue()


def test_station_menu_announces_a_promotion(monkeypatch):
    world = _world_with_seed(123)
    world.save.pilot.credits = vr.RANKS[1][0]
    keys = iter([" ", "Q"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)

    assert "Promoted to" in buf.getvalue()


# -- dump all contraband ------------------------------------------------


def _some_contraband_commodity():
    return vr.CONTRABAND_COMMODITIES[0]


def test_has_contraband_false_for_an_empty_or_legal_only_hold():
    world = _world_with_seed(124)
    assert not vr.has_contraband(world)
    world.save.cargo["food"] = 5
    assert not vr.has_contraband(world)


def test_has_contraband_true_once_any_illegal_good_is_in_cargo():
    world = _world_with_seed(125)
    world.save.cargo[_some_contraband_commodity()] = 3
    assert vr.has_contraband(world)


def test_dump_all_contraband_clears_only_illegal_goods():
    world = _world_with_seed(126)
    contraband = _some_contraband_commodity()
    world.save.cargo[contraband] = 7
    world.save.cargo["food"] = 4

    msg = vr.dump_all_contraband(world)

    assert contraband not in world.save.cargo
    assert world.save.cargo["food"] == 4
    assert "7" in msg


def test_dump_all_contraband_grants_no_credits():
    world = _world_with_seed(127)
    world.save.cargo[_some_contraband_commodity()] = 10
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
    world.save.cargo[contraband] = 5
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: False)

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_dump_contraband(vr.Palette(truecolor=False), world)

    assert world.save.cargo[contraband] == 5


def test_screen_dump_contraband_clears_cargo_on_confirmation(monkeypatch):
    world = _world_with_seed(130)
    contraband = _some_contraband_commodity()
    world.save.cargo[contraband] = 5
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

    world.save.cargo[_some_contraband_commodity()] = 2
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)
    assert "[D]" in buf2.getvalue()


# -- escort missions -----------------------------------------------------


def _accepted_escort_mission(world, dest_id, tier=1):
    hops = vr.bfs_hops(world.by_id, 0)
    mission = vr.Mission(id=world.save.next_mission_id, kind="escort", description="Escort a supply convoy to Somewhere",
                          reward=500, origin_system=0, target_system=dest_id, pirate_tier=tier,
                          deadline_turn=world.save.turn + 50)
    _post_and_accept_test_mission(world, mission)
    return mission


def test_generate_mission_board_can_include_escort_missions():
    world = _world_with_seed(132)
    world.event_rng = random.Random(0)
    found = False
    for seed in range(200):
        world.save.turn = seed * vr.MISSION_BOARD_DAYS
        world.event_rng = random.Random(seed)
        board = vr.generate_mission_board(world)
        if any(m.kind == "escort" for m in board):
            found = True
            break
    assert found


def test_generate_escort_mission_spans_at_least_two_hops():
    world = _world_with_seed(133)
    hops = vr.bfs_hops(world.by_id, world.save.current_system)
    mission = vr._generate_mission(world, "escort", hops)
    assert mission is not None
    assert hops[mission.target_system] >= 2


def test_escort_wave_fires_on_every_hop_while_active(monkeypatch):
    world = _world_with_seed(134)
    dest = next(sid for sid in world.by_id[0].connections)
    far_dest = next(sid for sid, h in vr.bfs_hops(world.by_id, 0).items() if h >= 2)
    mission = _accepted_escort_mission(world, far_dest)

    calls = []
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: calls.append(pirate) or "won")
    world.event_rng.random = lambda: 1.0  # suppress ordinary encounters

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest)

    assert len(calls) == 1  # the wave fired this hop
    assert mission in world.save.active_missions  # not yet at target -- still active


def test_escort_completes_and_pays_out_on_arrival_at_target(monkeypatch):
    world = _world_with_seed(135)
    dest = next(sid for sid in world.by_id[0].connections)
    mission = _accepted_escort_mission(world, dest)
    before_credits = world.save.pilot.credits

    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "won")
    world.event_rng.random = lambda: 1.0

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest)

    assert mission not in world.save.active_missions
    assert world.save.pilot.credits == before_credits + mission.reward


def test_escort_fails_on_ship_loss(monkeypatch):
    world = _world_with_seed(136)
    dest = next(sid for sid in world.by_id[0].connections)
    mission = _accepted_escort_mission(world, dest)

    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "destroyed")
    world.event_rng.random = lambda: 1.0

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest)

    assert mission not in world.save.active_missions


def test_escort_fails_on_evasion_even_though_ship_survives(monkeypatch):
    """Evading protects the player's own ship, but the convoy is left
    behind either way -- the contract still fails, unlike a bounty's own
    "escaped" outcome, which leaves the mission active to retry."""
    world = _world_with_seed(137)
    dest = next(sid for sid in world.by_id[0].connections)
    mission = _accepted_escort_mission(world, dest)

    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "escaped")
    world.event_rng.random = lambda: 1.0

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest)

    assert mission not in world.save.active_missions
    assert "failed" in buf.getvalue().lower()


def test_multiple_escort_missions_each_get_their_own_wave(monkeypatch):
    world = _world_with_seed(138)
    dest = next(sid for sid in world.by_id[0].connections)
    m1 = _accepted_escort_mission(world, dest)
    far_dest = next(sid for sid, h in vr.bfs_hops(world.by_id, 0).items() if h >= 2 and sid != dest)
    m2 = _accepted_escort_mission(world, far_dest)

    calls = []
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: calls.append(pirate) or "won")
    world.event_rng.random = lambda: 1.0

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest)

    assert len(calls) == 2
    assert m1 not in world.save.active_missions  # completed -- arrived at its target
    assert m2 in world.save.active_missions  # still en route


# -- cross-save hall of fame ----------------------------------------------


def test_load_hall_of_fame_returns_empty_list_when_missing(tmp_path):
    assert vr.load_hall_of_fame(tmp_path) == []


def test_load_hall_of_fame_returns_empty_list_on_corrupt_file(tmp_path):
    (tmp_path / "leaderboard.json").write_text("not json{{{", encoding="utf-8")
    assert vr.load_hall_of_fame(tmp_path) == []


def test_load_hall_of_fame_returns_empty_list_when_not_a_list(tmp_path):
    (tmp_path / "leaderboard.json").write_text('{"oops": true}', encoding="utf-8")
    assert vr.load_hall_of_fame(tmp_path) == []


def test_update_hall_of_fame_creates_an_entry_for_a_new_pilot(tmp_path):
    save = vr._new_career("Newcomer")
    save.pilot.credits = 5000

    vr.update_hall_of_fame(tmp_path, 42, save)

    entries = vr.load_hall_of_fame(tmp_path)
    assert len(entries) == 1
    assert entries[0]["user_id"] == 42
    assert entries[0]["handle"] == "Newcomer"
    assert entries[0]["best_credits"] == 5000
    assert entries[0]["rank"] == vr.rank_for(5000)


def test_update_hall_of_fame_never_lowers_best_credits(tmp_path):
    save = vr._new_career("Vet")
    save.pilot.credits = 10_000
    vr.update_hall_of_fame(tmp_path, 1, save)

    save.pilot.credits = 500  # a losing streak, or a retirement's own reset
    vr.update_hall_of_fame(tmp_path, 1, save)

    entries = vr.load_hall_of_fame(tmp_path)
    assert entries[0]["best_credits"] == 10_000


def test_update_hall_of_fame_refreshes_non_credit_fields_every_time(tmp_path):
    save = vr._new_career("Vet")
    save.pilot.credits = 10_000
    vr.update_hall_of_fame(tmp_path, 1, save)

    save.pilot.credits = 500
    save.pilot.retirements = 3
    save.pilot.kills = 7
    vr.update_hall_of_fame(tmp_path, 1, save)

    entries = vr.load_hall_of_fame(tmp_path)
    assert entries[0]["retirements"] == 3
    assert entries[0]["kills"] == 7
    assert entries[0]["best_credits"] == 10_000  # still the high-water mark


def test_update_hall_of_fame_keeps_separate_pilots_separate(tmp_path):
    save_a = vr._new_career("Alice")
    save_a.pilot.credits = 3000
    save_b = vr._new_career("Bob")
    save_b.pilot.credits = 7000

    vr.update_hall_of_fame(tmp_path, 1, save_a)
    vr.update_hall_of_fame(tmp_path, 2, save_b)

    entries = vr.load_hall_of_fame(tmp_path)
    assert len(entries) == 2
    assert entries[0]["handle"] == "Bob"  # sorted by best_credits, descending
    assert entries[1]["handle"] == "Alice"


def test_update_hall_of_fame_caps_at_hall_of_fame_size(tmp_path):
    for uid in range(vr.HALL_OF_FAME_SIZE + 5):
        save = vr._new_career(f"Pilot{uid}")
        save.pilot.credits = uid
        vr.update_hall_of_fame(tmp_path, uid, save)

    entries = vr.load_hall_of_fame(tmp_path)
    assert len(entries) == vr.HALL_OF_FAME_SIZE
    # The lowest-credit pilots are omitted from the view, but retained on disk.
    assert entries[-1]["best_credits"] == 5


def test_persist_updates_both_the_save_and_the_hall_of_fame(tmp_path):
    save = vr._new_career("Persisted")
    save.pilot.credits = 4200
    world = vr.World(save)

    vr.persist(world, tmp_path, 7)

    assert (tmp_path / "7.json").exists()
    entries = vr.load_hall_of_fame(tmp_path)
    assert entries and entries[0]["user_id"] == 7


def test_screen_hall_of_fame_shows_no_pilots_message_when_empty(tmp_path, monkeypatch):
    world = _world_with_seed(139)
    monkeypatch.setattr(vr, "read_key", lambda: " ")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_hall_of_fame(vr.Palette(truecolor=False), world, tmp_path, 1)

    assert "No pilots recorded" in buf.getvalue()


def test_screen_hall_of_fame_marks_the_current_pilot(tmp_path, monkeypatch):
    save = vr._new_career("Me")
    save.pilot.credits = 9000
    vr.update_hall_of_fame(tmp_path, 5, save)
    other = vr._new_career("Someone Else")
    other.pilot.credits = 200
    vr.update_hall_of_fame(tmp_path, 6, other)

    world = _world_with_seed(140)
    monkeypatch.setattr(vr, "read_key", lambda: " ")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_hall_of_fame(vr.Palette(truecolor=False), world, tmp_path, 5)

    text = buf.getvalue()
    assert "Me" in text and "Someone Else" in text
    me_line = next(line for line in text.splitlines() if "Me" in line and "Someone" not in line)
    assert "*" in me_line


# -- named sectors ---------------------------------------------------------


class _Sys:
    def __init__(self, x, y, discovered=True):
        self.x = x
        self.y = y
        self.discovered = discovered


def test_sector_for_covers_the_full_coordinate_grid_without_error():
    world = _world_with_seed(141)
    for system in world.galaxy:
        sector = vr.sector_for(system)
        assert sector in vr.SECTOR_NAMES


def test_sector_for_is_a_pure_function_of_position():
    a = _Sys(10, 5)
    b = _Sys(10, 5)
    assert vr.sector_for(a) == vr.sector_for(b)


def test_sector_for_distinguishes_far_apart_corners():
    top_left = _Sys(0, 0)
    bottom_right = _Sys(99, 49)
    assert vr.sector_for(top_left) != vr.sector_for(bottom_right)


def test_sector_for_never_indexes_out_of_range_at_grid_edges():
    for x in (0, 99):
        for y in (0, 49):
            assert vr.sector_for(_Sys(x, y)) in vr.SECTOR_NAMES


def test_sector_assignment_does_not_touch_the_galaxy_rng():
    """sector_for takes no RNG at all -- proves calling it repeatedly
    never perturbs a subsequent generate_galaxy call for the same seed,
    protecting that function's own seed-determinism invariant."""
    seed = 142
    before = vr.generate_galaxy(seed)
    for system in before:
        vr.sector_for(system)
    after = vr.generate_galaxy(seed)
    for a, b in zip(before, after):
        assert a.x == b.x and a.y == b.y and a.connections == b.connections


def test_screen_galaxy_map_shows_nothing_charted_message_when_empty(monkeypatch):
    world = _world_with_seed(143)
    for system in world.galaxy:
        system.discovered = False
    monkeypatch.setattr(vr, "read_key", lambda: " ")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_galaxy_map(vr.Palette(truecolor=False), world)

    assert "Nothing charted yet" in buf.getvalue()


def test_screen_galaxy_map_lists_only_discovered_systems_grouped_by_sector(monkeypatch):
    world = _world_with_seed(144)
    monkeypatch.setattr(vr, "read_key", lambda: " ")

    discovered_names = {s.name for s in world.galaxy if s.discovered}
    undiscovered_names = {s.name for s in world.galaxy if not s.discovered}

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_galaxy_map(vr.Palette(truecolor=False), world)

    text = buf.getvalue()
    for name in discovered_names:
        assert name in text
    for name in undiscovered_names:
        assert name not in text
    assert any(sector in text for sector in vr.SECTOR_NAMES)


def test_screen_galaxy_map_marks_the_current_system(monkeypatch):
    world = _world_with_seed(145)
    monkeypatch.setattr(vr, "read_key", lambda: " ")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_galaxy_map(vr.Palette(truecolor=False), world)

    here_line = next(line for line in buf.getvalue().splitlines() if world.here.name in line)
    assert "*" in here_line
    assert "here" in here_line.lower()


def test_chart_screen_offers_view_full_chart(monkeypatch):
    world = _world_with_seed(146)
    monkeypatch.setattr(vr, "read_key", lambda: "Q")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_chart(vr.Palette(truecolor=False), world)

    assert "[V]" in buf.getvalue()


def test_chart_screen_v_key_opens_the_galaxy_map(monkeypatch):
    world = _world_with_seed(147)
    keys = iter(["V", " ", "Q"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_chart(vr.Palette(truecolor=False), world)

    assert "Charted Systems" in buf.getvalue()


# -- paid NPC crew ----------------------------------------------------------


def test_ship_from_dict_defaults_crew_fields_for_old_saves():
    save = vr._new_career("Legacy")
    d = save.ship.to_dict()
    del d["has_gunner"], d["has_engineer"], d["has_navigator"]

    ship = vr.Ship.from_dict(d)

    assert not ship.has_gunner and not ship.has_engineer and not ship.has_navigator


def test_gunner_adds_flat_combat_damage(monkeypatch):
    world = _world_with_seed(148)
    pirate = vr.Pirate(name="Target", tier=0, hp=999, hp_max=999)
    world.event_rng.randint = lambda a, b: a  # pin the random roll for a clean comparison

    dmg_without, _, _ = vr.fight_round(world, pirate)

    world.save.ship.has_gunner = True
    pirate2 = vr.Pirate(name="Target", tier=0, hp=999, hp_max=999)
    dmg_with, _, _ = vr.fight_round(world, pirate2)

    assert dmg_with == dmg_without + 3


def test_engineer_discounts_fuel_cost_but_never_below_one():
    world = _world_with_seed(149)
    a, b = world.by_id[0], world.by_id[world.by_id[0].connections[0]]
    base = vr.fuel_cost_for_jump(a, b)

    world.save.ship.has_engineer = True
    discounted = vr.fuel_cost_for_jump(a, b, world.save.ship)

    assert discounted == max(1, base - (base + 3) // 4)


def test_engineer_discount_never_goes_below_one_even_on_the_cheapest_jump():
    class _Sys:
        x = 0
        y = 0

    ship = vr.Ship(hull_class="Shuttle", hull_hp=60, fuel=24, has_engineer=True)
    assert vr.fuel_cost_for_jump(_Sys(), _Sys(), ship) == 1


def test_navigator_extends_scan_range(monkeypatch):
    world = _world_with_seed(150)
    world.save.ship.scanner_tier = 1

    without = 2 + world.save.ship.scanner_tier
    world.save.ship.has_navigator = True
    with_nav = 2 + world.save.ship.scanner_tier + (1 if world.save.ship.has_navigator else 0)

    assert with_nav == without + 1


def test_pay_crew_wages_deducts_for_each_hired_role():
    world = _world_with_seed(151)
    world.save.pilot.credits = 1000
    world.save.ship.has_gunner = True
    world.save.ship.has_navigator = True

    messages = vr.pay_crew_wages(world)

    assert messages == []
    expected = 1000 - vr.CREW_ROLES["gunner"]["wage"] - vr.CREW_ROLES["navigator"]["wage"]
    assert world.save.pilot.credits == expected
    assert world.save.ship.has_gunner and world.save.ship.has_navigator


def test_pay_crew_wages_resigns_a_crew_member_who_cant_be_paid():
    world = _world_with_seed(152)
    world.save.pilot.credits = 1
    world.save.ship.has_engineer = True

    messages = vr.pay_crew_wages(world)

    assert len(messages) == 1
    assert "resigns" in messages[0]
    assert not world.save.ship.has_engineer
    assert world.save.pilot.credits == 1  # never driven negative


def test_pay_crew_wages_never_drives_credits_negative():
    world = _world_with_seed(153)
    world.save.pilot.credits = 0
    world.save.ship.has_gunner = True
    world.save.ship.has_engineer = True
    world.save.ship.has_navigator = True

    vr.pay_crew_wages(world)

    assert world.save.pilot.credits == 0


def test_toggle_crew_hires_when_affordable(monkeypatch):
    world = _world_with_seed(154)
    world.save.pilot.credits = 10_000
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)

    with contextlib.redirect_stdout(io.StringIO()):
        vr._toggle_crew(vr.Palette(truecolor=False), world, "gunner")

    assert world.save.ship.has_gunner
    assert world.save.pilot.credits == 10_000 - vr.CREW_ROLES["gunner"]["hire_cost"]


def test_toggle_crew_refuses_when_unaffordable(monkeypatch):
    world = _world_with_seed(155)
    world.save.pilot.credits = 10
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)

    with contextlib.redirect_stdout(io.StringIO()):
        vr._toggle_crew(vr.Palette(truecolor=False), world, "gunner")

    assert not world.save.ship.has_gunner
    assert world.save.pilot.credits == 10


def test_toggle_crew_dismisses_on_confirmation(monkeypatch):
    world = _world_with_seed(156)
    world.save.ship.has_navigator = True
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)

    with contextlib.redirect_stdout(io.StringIO()):
        vr._toggle_crew(vr.Palette(truecolor=False), world, "navigator")

    assert not world.save.ship.has_navigator


def test_toggle_crew_keeps_crew_without_dismissal_confirmation(monkeypatch):
    world = _world_with_seed(157)
    world.save.ship.has_navigator = True
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: False)

    with contextlib.redirect_stdout(io.StringIO()):
        vr._toggle_crew(vr.Palette(truecolor=False), world, "navigator")

    assert world.save.ship.has_navigator


def test_screen_crew_lists_all_roles(monkeypatch):
    world = _world_with_seed(158)
    monkeypatch.setattr(vr, "read_key", lambda: "Q")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_crew(vr.Palette(truecolor=False), world)

    text = buf.getvalue()
    for info in vr.CREW_ROLES.values():
        assert info["label"] in text


def test_shipyard_offers_crew_option(monkeypatch):
    world = _world_with_seed(159)
    monkeypatch.setattr(vr, "read_key", lambda: "Q")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_shipyard(vr.Palette(truecolor=False), world)

    assert "[K] Crew" in buf.getvalue()


def test_shipyard_crew_row_letter_never_collides_with_an_upgrade_row():
    """Regression guard for a real dogfood-caught bug: Crew's own footer
    hotkey used to be "[C]", which is also Weapon Systems' own row
    letter (the 3rd of 6 UPGRADES entries) shown just above it on the
    same screen -- two different things both labeled "[C]" on one
    prompt, the exact ambiguity this file's own `refit_keys` comment
    already documents fixing once before."""
    upgrade_row_letters = set(vr.LETTERS[: len(vr.UPGRADES)])
    assert "K" not in upgrade_row_letters


def test_shipyard_k_key_opens_crew_screen(monkeypatch):
    world = _world_with_seed(160)
    keys = iter(["K", "Q", "Q"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_shipyard(vr.Palette(truecolor=False), world)

    assert "Crew Quarters" in buf.getvalue()


def test_chart_screen_reserves_sgv_and_never_assigns_them_to_a_connection(monkeypatch):
    """Regression guard for a real dogfood-caught bug: `_connect_systems`'s
    own extra-edge pass can give a single system up to ~7 connections
    (seen across a few thousand random seeds), and a plain `LETTERS[i]`
    assignment would silently give the 7th one the same row letter as
    the "[G]o to" hotkey -- permanently shadowing that connection,
    since "G" was checked as a fixed control key before ever falling
    through to the row lookup. Forces a system with 8 connections
    (more than "G"'s own position, 7th letter) and confirms the 8th
    one is both drawn with, and selectable via, a non-reserved letter."""
    world = _world_with_seed(190)
    world.here.connections = list(range(1, 9))  # 8 synthetic neighbors
    for sid in world.here.connections:
        world.by_id[sid].discovered = True
    world.save.ship.fuel = 999

    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_chart(vr.Palette(truecolor=False), world)

    for letter in "SGV":
        assert letter not in vr.CHART_CONNECTION_LETTERS[:8]

    eighth_letter = vr.CHART_CONNECTION_LETTERS[7]
    keys = iter([eighth_letter])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()):
        dest = vr.screen_chart(vr.Palette(truecolor=False), world)
    assert dest == 8  # the 8th synthetic connection, reachable via its own real letter


def test_screen_status_shows_hired_crew(monkeypatch):
    world = _world_with_seed(161)
    world.save.ship.has_gunner = True
    monkeypatch.setattr(vr, "read_key", lambda: " ")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_status(vr.Palette(truecolor=False), world)

    assert "Gunner" in buf.getvalue()


def test_retiring_resets_crew():
    old_save = vr._new_career("Vet")
    old_save.ship.has_gunner = True
    old_save.ship.has_engineer = True

    new_save = vr.retire_pilot(old_save)

    assert not new_save.ship.has_gunner and not new_save.ship.has_engineer


# -- scheduled economy events -----------------------------------------------


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
    affected = [s for s in world.galaxy if s.economy == event["economy"]]
    assert affected
    for system in affected:
        assert world.save.market_drift[system.id][event["commodity"]] in (
            vr.ECONOMY_EVENT_CRASH_LEVEL, vr.ECONOMY_EVENT_BOOM_LEVEL)


def test_tick_economy_event_reasserts_drift_level_each_turn_while_active():
    world = _world_with_seed(164)
    system = next(s for s in world.galaxy if s.economy == "Agricultural")
    world.save.active_event = {
        "economy": "Agricultural", "commodity": "food", "direction": "crash",
        "turns_remaining": 3, "description": "Food prices crash across every Agricultural system",
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
    monkeypatch.setattr(vr, "read_key", lambda: " ")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_status(vr.Palette(truecolor=False), world)

    visible = vr._ANSI_RE.sub("", buf.getvalue())
    normalized = " ".join(visible.replace("│", " ").split())
    assert "Economy event" in normalized
    assert "4 turn(s) left" in normalized


def test_retiring_resets_active_economy_event():
    old_save = vr._new_career("Vet")
    old_save.active_event = {
        "economy": "Haven", "commodity": "weapons", "direction": "crash",
        "turns_remaining": 5, "description": "Weapons prices crash across every Haven system",
    }

    new_save = vr.retire_pilot(old_save)

    assert new_save.active_event is None


# -- futures contracts -------------------------------------------------

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


def test_legacy_futures_refund_when_cargo_is_full():
    world = _world_with_seed(173)
    cap = vr.cargo_capacity(world.save.ship)
    world.save.active_futures = [vr.FuturesContract(1, "food", cap, 200, 5)]
    world.save.cargo["textiles"] = cap  # fill the hold with something else before settlement
    before_credits = world.save.pilot.credits
    contract = world.save.active_futures[0]
    world.save.turn += 5

    messages = vr.settle_futures_contracts(world)

    assert len(messages) == 1
    assert "refunded" in messages[0]
    assert "food" not in world.save.cargo
    assert world.save.pilot.credits == before_credits + contract.locked_price


def test_legacy_futures_settle_regardless_of_current_location():
    world = _world_with_seed(174)
    world.save.active_futures = [vr.FuturesContract(1, "food", 2, 26, 5)]
    world.save.current_system = world.by_id[0].connections[0]  # moved away before settlement
    world.save.turn += 5

    messages = vr.settle_futures_contracts(world)

    assert len(messages) == 1
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
    keys = iter("QS")
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


# -- faction endgame arcs ----------------------------------------------

def test_pilot_from_dict_defaults_faction_arc_fields_for_old_saves():
    save = vr._new_career("Legacy")
    d = save.pilot.to_dict()
    del d["has_concord_commission"], d["has_blackwake_made"]

    pilot = vr.Pilot.from_dict(d)

    assert not pilot.has_concord_commission
    assert not pilot.has_blackwake_made


def test_concord_commission_unavailable_below_threshold():
    world = _world_with_seed(180)
    world.save.pilot.reputation[vr.FACTION_CONCORD] = vr.CONCORD_COMMISSION_THRESHOLD - 1
    assert not vr.concord_commission_available(world)


def test_concord_commission_available_at_threshold():
    world = _world_with_seed(181)
    world.save.pilot.reputation[vr.FACTION_CONCORD] = vr.CONCORD_COMMISSION_THRESHOLD
    assert vr.concord_commission_available(world)


def test_concord_commission_unavailable_once_already_held():
    world = _world_with_seed(182)
    world.save.pilot.reputation[vr.FACTION_CONCORD] = vr.CONCORD_COMMISSION_THRESHOLD
    world.save.pilot.has_concord_commission = True
    assert not vr.concord_commission_available(world)


def test_blackwake_made_available_at_threshold():
    world = _world_with_seed(183)
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = vr.BLACKWAKE_MADE_THRESHOLD
    assert vr.blackwake_made_available(world)


def test_blackwake_made_unavailable_once_already_held():
    world = _world_with_seed(184)
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = vr.BLACKWAKE_MADE_THRESHOLD
    world.save.pilot.has_blackwake_made = True
    assert not vr.blackwake_made_available(world)


def test_bounty_reward_for_applies_the_commission_bonus():
    world = _world_with_seed(185)
    assert vr.bounty_reward_for(world, 100) == 100

    world.save.pilot.has_concord_commission = True
    assert vr.bounty_reward_for(world, 100) == round(100 * (1 + vr.CONCORD_COMMISSION_BOUNTY_BONUS))


def test_screen_concord_commission_grants_perk_and_bonus_on_confirmation(monkeypatch):
    world = _world_with_seed(186)
    before_credits = world.save.pilot.credits
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_concord_commission(vr.Palette(truecolor=False), world)

    assert world.save.pilot.has_concord_commission
    assert world.save.pilot.credits == before_credits + vr.CONCORD_COMMISSION_BONUS_CREDITS
    assert any("privateer" in h.lower() for h in world.save.pilot.highlights)


def test_screen_concord_commission_declines_without_confirmation(monkeypatch):
    world = _world_with_seed(187)
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: False)

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_concord_commission(vr.Palette(truecolor=False), world)

    assert not world.save.pilot.has_concord_commission


def test_screen_blackwake_made_grants_perk_and_bonus_on_confirmation(monkeypatch):
    world = _world_with_seed(188)
    before_credits = world.save.pilot.credits
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_blackwake_made(vr.Palette(truecolor=False), world)

    assert world.save.pilot.has_blackwake_made
    assert world.save.pilot.credits == before_credits + vr.BLACKWAKE_MADE_BONUS_CREDITS


def test_station_menu_offers_arcs_only_when_available(monkeypatch):
    world = _world_with_seed(189)
    monkeypatch.setattr(vr, "read_key", lambda: "Q")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)
    assert "[P]" not in buf.getvalue() and "[W]" not in buf.getvalue()

    world.save.pilot.reputation[vr.FACTION_CONCORD] = vr.CONCORD_COMMISSION_THRESHOLD
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = vr.BLACKWAKE_MADE_THRESHOLD
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)
    assert "[P]" in buf2.getvalue() and "[W]" in buf2.getvalue()


def test_retiring_resets_faction_arcs():
    old_save = vr._new_career("Vet")
    old_save.pilot.has_concord_commission = True
    old_save.pilot.has_blackwake_made = True

    new_save = vr.retire_pilot(old_save)

    assert not new_save.pilot.has_concord_commission
    assert not new_save.pilot.has_blackwake_made


# -- box/column alignment (dogfood pass, post-#190 visual overhaul) -------
#
# The tactical-HUD box rendering #190 introduced draws every screen as a
# "|...content...|" box whose outer border (the "|--- ... ---|" separator
# rows) is always exactly 79 columns wide. Content rows are supposed to
# right-pad to match that same width, but several screens either hand-
# typed a header row's spacing without counting it precisely, or let an
# unbounded piece of text (a mission description, a sector name, an
# upgrade's effect blurb) run past its column budget -- both silently
# push that one row's right-hand border past (or short of) where every
# other row's border sits, breaking the box. These tests pin the fixed
# cases directly rather than re-deriving the whole checker, since the
# box style itself (a fixed 79-column border) is what every one of these
# regressions would otherwise quietly reappear against.


def _assert_box_rows_match_border(text: str, label: str) -> None:
    stripped = [vr._ANSI_RE.sub("", line) for line in text.split("\r\n")]
    border_widths = {len(l) for l in stripped if l.strip().startswith(("╭", "├", "╰"))}
    assert len(border_widths) <= 1, f"{label}: inconsistent border widths {border_widths}"
    if not border_widths:
        return
    (border_width,) = border_widths
    for line in stripped:
        if line.strip().startswith("│"):
            assert line.rstrip().endswith("│"), f"{label}: right border missing: {line!r}"
            assert len(line) == border_width, (
                f"{label}: content row width {len(line)} != border width {border_width}: {line!r}"
            )


def test_screen_status_truncates_long_mission_descriptions_to_fit_the_box(monkeypatch):
    world = _world_with_seed(300)
    world.save.pilot.credits = 15_000
    world.save.active_missions = [
        vr.Mission(id=1, kind="escort",
                   description="Escort a supply convoy to Perrin's Folly (4 jump(s), raider activity expected)",
                   reward=900, origin_system=0, target_system=5, pirate_tier=3, deadline_turn=40),
    ]
    monkeypatch.setattr(vr, "read_key", lambda: "X")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_status(vr.Palette(truecolor=False), world)
    _assert_box_rows_match_border(buf.getvalue(), "screen_status")


def test_screen_status_credits_line_matches_box_border(monkeypatch):
    world = _world_with_seed(301)
    world.save.pilot.credits = 1_234_567
    monkeypatch.setattr(vr, "read_key", lambda: "X")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_status(vr.Palette(truecolor=False), world)
    _assert_box_rows_match_border(buf.getvalue(), "screen_status")


def test_screen_shipyard_rows_fit_the_box_at_every_tier(monkeypatch):
    world = _world_with_seed(302)
    world.save.pilot.credits = 100_000
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_shipyard(vr.Palette(truecolor=False), world)
    _assert_box_rows_match_border(buf.getvalue(), "screen_shipyard@tier0")

    for key in vr.UPGRADES:
        setattr(world.save.ship, f"{key}_tier", vr.UPGRADES[key]["max_tier"])
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        vr.screen_shipyard(vr.Palette(truecolor=False), world)
    _assert_box_rows_match_border(buf2.getvalue(), "screen_shipyard@maxed")


def test_screen_market_contraband_row_fits_the_box(monkeypatch):
    world = _world_with_seed(303)
    world.save.pilot.credits = 50_000
    haven = next(s for s in world.galaxy if s.economy == "Haven")
    world.save.current_system = haven.id
    haven.discovered = True
    world.save.cargo = {"weapons": 5, "narcotics": 3}
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_market(vr.Palette(truecolor=False), world)
    _assert_box_rows_match_border(buf.getvalue(), "screen_market@Haven")


def test_screen_station_menu_special_ops_rows_fit_the_box(monkeypatch):
    world = _world_with_seed(304)
    world.save.current_system = world.landmark["system_id"]
    world.by_id[world.save.current_system].discovered = True
    world.save.cargo = {"weapons": 2}
    world.save.pilot.reputation[vr.FACTION_CONCORD] = vr.CONCORD_COMMISSION_THRESHOLD
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = vr.BLACKWAKE_MADE_THRESHOLD
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)
    _assert_box_rows_match_border(buf.getvalue(), "screen_station_menu@all-special-ops")


def test_screen_chart_rows_fit_the_box_for_every_sector_name(monkeypatch):
    # Every one of the six named sectors (SECTOR_NAMES) is at least 12
    # characters -- longer than the chart row's own sector column used to
    # budget for -- so any discovered system, in any sector, is enough to
    # exercise the fix; picking the highest-degree system just maximizes
    # how many rows get checked in one pass.
    world = _world_with_seed(305)
    best = max(world.galaxy, key=lambda s: len(s.connections))
    world.save.current_system = best.id
    best.discovered = True
    for nid in best.connections:
        world.by_id[nid].discovered = True
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_chart(vr.Palette(truecolor=False), world)
    _assert_box_rows_match_border(buf.getvalue(), "screen_chart")


def test_screen_chart_uncharted_bearing_row_fits_the_box(monkeypatch):
    world = _world_with_seed(306)
    best = max(world.galaxy, key=lambda s: len(s.connections))
    world.save.current_system = best.id
    best.discovered = True
    # Leave every neighbor undiscovered to force the "??? (Uncharted
    # Bearing)" placeholder row instead of a real destination row.
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_chart(vr.Palette(truecolor=False), world)
    _assert_box_rows_match_border(buf.getvalue(), "screen_chart@uncharted")


def test_screen_hall_of_fame_rows_fit_the_box_at_max_field_widths(monkeypatch):
    world = _world_with_seed(307)
    import json
    import tempfile
    from pathlib import Path

    entries = [{
        "user_id": 1, "handle": "SixteenCharHandl", "best_credits": 999_999,
        "rank": vr.RANKS[-1][1], "retirements": 3, "kills": 120, "missions_completed": 88,
    }]
    save_dir = Path(tempfile.mkdtemp())
    (save_dir / "leaderboard.json").write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(vr, "read_key", lambda: "X")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_hall_of_fame(vr.Palette(truecolor=False), world, save_dir, 1)
    _assert_box_rows_match_border(buf.getvalue(), "screen_hall_of_fame")


def test_screen_customs_rows_fit_the_box_for_a_large_contraband_stash(monkeypatch):
    world = _world_with_seed(308)
    world.save.cargo = {"weapons": 20, "narcotics": 15}
    world.save.pilot.credits = 50_000
    monkeypatch.setattr(vr, "read_key", lambda: "S")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_customs(vr.Palette(truecolor=False), world)
    _assert_box_rows_match_border(buf.getvalue(), "screen_customs")


# The box-border checks above only pin the *right edge* of each row -- they
# can't catch a column that starts or ends in a different place from row to
# row, because a naive `f"{ansi_colored_value:<N}"` format spec counts the
# invisible escape bytes as part of N. Since every colored value in this
# module (`_gauge_bar`'s output, the market/chart/shipyard status strings)
# happens to share the same fixed-length ANSI overhead across rows, the
# resulting under-padding is *constant* and the box border still lands in
# the right place -- but the column itself silently drifts out of alignment
# with its neighbors. These tests pin the actual column position of the
# text immediately after each fixed-width colored field.


def test_screen_shipyard_effect_column_aligns_between_tiered_and_maxed_rows(monkeypatch):
    world = _world_with_seed(309)
    world.save.pilot.credits = 100_000
    for key in vr.UPGRADES:
        setattr(world.save.ship, f"{key}_tier", 0)
    world.save.ship.engine_tier = vr.UPGRADES["engine"]["max_tier"]
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_shipyard(vr.Palette(truecolor=False), world)
    _assert_box_rows_match_border(buf.getvalue(), "screen_shipyard@mixed-tiers")
    stripped = [vr._ANSI_RE.sub("", line) for line in buf.getvalue().split("\r\n")]
    effect_columns = {line.index("(") for line in stripped if "Tier" in line or "MAXED" in line}
    assert len(effect_columns) == 1, f"effect column drifted between rows: {effect_columns}"


def test_screen_missions_preserves_rewards_in_compact_entries(monkeypatch):
    world = _world_with_seed(310)
    world.save.pilot.credits = 5_000
    monkeypatch.setattr(
        vr, "posted_mission_offers",
        lambda world: [
            vr.Mission(id=1, kind="bounty", description="Short", reward=5,
                       origin_system=0, target_system=1, pirate_tier=1, deadline_turn=None),
            vr.Mission(id=2, kind="cargo", description="Also short", reward=123_456,
                       origin_system=0, target_system=2, pirate_tier=0, deadline_turn=None),
        ],
    )
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_missions(vr.Palette(truecolor=False), world)
    output = buf.getvalue()
    assert "+5cr" in output and "+123,456cr" in output
    assert "[1]" in output and "[2]" in output
    assert "Details" in output and "[B]ack" in output


def test_screen_chart_danger_and_fuel_columns_align_between_safe_and_danger_rows(monkeypatch):
    world = _world_with_seed(311)
    here = world.here
    # Force at least one safe (danger 0) and one dangerous connected system
    # so both branches of `danger_str` render in the same screen.
    assert len(here.connections) >= 2, "seed 311's start system needs 2+ connections for this test"
    safe_id, danger_id = here.connections[0], here.connections[1]
    world.by_id[safe_id].danger = 0
    world.by_id[danger_id].danger = 3
    for sid in (safe_id, danger_id):
        world.by_id[sid].discovered = True
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_chart(vr.Palette(truecolor=False), world)
    _assert_box_rows_match_border(buf.getvalue(), "screen_chart@safe-and-danger")
    stripped = [vr._ANSI_RE.sub("", line) for line in buf.getvalue().split("\r\n")]
    fuel_columns = {line.index("fuel") for line in stripped if "fuel" in line and "[" in line}
    assert len(fuel_columns) == 1, f"fuel-cost column drifted between rows: {fuel_columns}"


def test_screen_title_renders_full_splash_with_tagline_and_exact_box_width():
    p = vr.Palette(truecolor=False)
    for node, handle in [("Central BBS", "Alice"), ("A Very Long BBS Node Name Here", "SixteenCharHandl")]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            vr.screen_title(p, {"node_name": node, "handle": handle})
        output = buf.getvalue()
        assert "a NetBBS door game" in output
        assert "V O I D R U N N E R" in output
        stripped = [vr._ANSI_RE.sub("", line) for line in output.split("\r\n") if line.strip()]
        box_lines = [line for line in stripped if line.startswith(("╔", "║", "╠", "╚"))]
        assert len(box_lines) == 11
        widths = {len(line) for line in box_lines}
        assert widths == {79}, f"expected all splash box rows to be 79 visible chars, got {widths}"


def test_create_career_box_rows_fit_79_column_border(monkeypatch):
    p = vr.Palette(truecolor=False)
    monkeypatch.setattr(vr, "read_line_raw", lambda max_len=16, allowed=None: "TestPilot")
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        callsign = vr.create_career(p, {"handle": "SixteenCharHandl"})
    assert callsign == "TestPilot"
    _assert_box_rows_match_border(buf.getvalue(), "create_career@long-handle")
    stripped = [vr._ANSI_RE.sub("", line) for line in buf.getvalue().split("\r\n") if line.strip()]
    border_lines = [line for line in stripped if line.startswith(("╭", "╰"))]
    assert len(border_lines) == 2
    assert {len(line) for line in border_lines} == {79}


def test_screen_crew_and_galaxy_map_boxes_match_79_columns(monkeypatch):
    world = _world_with_seed(312)
    p = vr.Palette(truecolor=False)

    # screen_crew
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_crew(p, world)
    _assert_box_rows_match_border(buf.getvalue(), "screen_crew")
    stripped = [vr._ANSI_RE.sub("", line) for line in buf.getvalue().split("\r\n") if line.strip()]
    crew_borders = {len(line) for line in stripped if line.startswith(("╭", "├", "╰"))}
    assert crew_borders == {79}

    # screen_galaxy_map
    monkeypatch.setattr(vr, "pause", lambda p: None)
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        vr.screen_galaxy_map(p, world)
    _assert_box_rows_match_border(buf2.getvalue(), "screen_galaxy_map")
    stripped2 = [vr._ANSI_RE.sub("", line) for line in buf2.getvalue().split("\r\n") if line.strip()]
    map_borders = {len(line) for line in stripped2 if line.startswith(("╭", "╰"))}
    assert map_borders == {79}


def test_empty_mission_board_renders_clean_notice_and_back(monkeypatch):
    world = _world_with_seed(313)
    p = vr.Palette(truecolor=False)
    monkeypatch.setattr(vr, "posted_mission_offers", lambda w: [])
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_missions(p, world)
    output = buf.getvalue()
    assert "No contracts currently available" in output
    assert "[B]ack" in output


def test_commission_and_cartel_screens_use_tactical_framing(monkeypatch):
    world = _world_with_seed(314)
    p = vr.Palette(truecolor=False)
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: False)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_concord_commission(p, world)
    _assert_box_rows_match_border(buf.getvalue(), "screen_concord_commission")
    stripped = [vr._ANSI_RE.sub("", line) for line in buf.getvalue().split("\r\n") if line.strip()]
    concord_borders = {len(line) for line in stripped if line.startswith(("╭", "╰"))}
    assert concord_borders == {79}

    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        vr.screen_blackwake_made(p, world)
    _assert_box_rows_match_border(buf2.getvalue(), "screen_blackwake_made")
    stripped2 = [vr._ANSI_RE.sub("", line) for line in buf2.getvalue().split("\r\n") if line.strip()]
    blackwake_borders = {len(line) for line in stripped2 if line.startswith(("╭", "╰"))}
    assert blackwake_borders == {79}


def test_status_bar_separator_is_79_columns():
    world = _world_with_seed(315)
    p = vr.Palette(truecolor=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.draw_status_bar(p, world)
    stripped = [vr._ANSI_RE.sub("", line) for line in buf.getvalue().split("\r\n") if line.strip()]
    rule_line = [line for line in stripped if line.startswith("─") and set(line) == {"─"}]
    assert len(rule_line) == 1
    assert len(rule_line[0]) == 79


# Issue #310: exercise the real executable and kill it while nested menus
# still own input. Save-on-menu-exit and graceful-EOF tests miss this boundary.
@pytest.mark.parametrize(
    "commands,ack,field,expected",
    [
        (b"MAB1\r", b"Bought 1x Food", "cargo.food", 1),
        (b"YAY", b"Cargo Bay Expansion upgraded", "ship.cargo_tier", 1),
        (b"YKAY", b"Gunner hired", "ship.has_gunner", True),
        (b"YR2\r", b"Refueled 2 units", "ship.fuel", 22),
        (b"YPY", b"Hull repaired", "ship.hull_hp", 60),
        (b"MX1SY", b"Futures contract: 1x Food", "active_futures", "nonempty"),
        (b"B1NNNNNNNNA", b"Accepted:", "active_missions", "nonempty"),
        (b"DY", b"Jettisoned 1 units", "cargo", {}),
        (b"PY", b"Commission accepted", "pilot.has_concord_commission", True),
        (b"WY", b"Welcome to the family", "pilot.has_blackwake_made", True),
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
        world.save.cargo = {"weapons": 1}
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

    def read_until_ack():
        while len(output) < 128_000:
            byte = proc.stdout.read(1)
            if not byte:
                return
            output.extend(byte)
            if ack in output:
                reached.set()
                return

    reader = threading.Thread(target=read_until_ack)
    reader.start()
    try:
        proc.stdin.write(commands)
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


def test_failed_station_checkpoint_never_announces_purchase(monkeypatch):
    world = _world_with_seed(42)

    def fail_save(current):
        raise OSError("disk full")

    world._checkpoint = fail_save
    monkeypatch.setattr(vr, "read_key", lambda: "B")
    monkeypatch.setattr(vr, "read_line_raw", lambda **kwargs: "1")
    output = io.StringIO()
    with contextlib.redirect_stdout(output), pytest.raises(vr.SaveError):
        vr._trade_commodity(vr.Palette(False), world, "food")
    assert "Bought" not in output.getvalue()


def test_save_failure_stops_main_without_success_or_more_actions(tmp_path, monkeypatch):
    world = _world_with_seed(42)
    original_persist = vr.persist
    attempts = []

    def fail_second_save(current, directory, user_id):
        attempts.append(current.save.to_dict())
        if len(attempts) == 2:
            raise OSError("disk full")
        original_persist(current, directory, user_id)

    class Terminal(io.StringIO):
        def reconfigure(self, **kwargs):
            pass

    output = Terminal()
    keys = iter(["M", "A", "B", " ", "Q"])
    with monkeypatch.context() as patch:
        patch.setattr(vr.sys, "stdout", output)
        patch.setattr(vr, "_load_door_info", lambda: {"handle": "Tester", "user_id": 77})
        patch.setattr(vr, "_default_save_dir", lambda: tmp_path)
        patch.setattr(vr, "load_or_create_save", lambda *args: (world.save, False, None))
        patch.setattr(vr, "persist", fail_second_save)
        patch.setattr(vr, "read_key", lambda: next(keys))
        patch.setattr(vr, "read_line_raw", lambda **kwargs: "1")
        assert vr.main() == 1
    assert "Save failed" in output.getvalue()
    assert "Bought" not in output.getvalue()
    assert next(keys) == "Q"  # only the error acknowledgement was consumed
    assert len(attempts) == 2  # no EOF retry writes an unacknowledged action
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert saved.cargo == {}
    assert saved.pilot.credits == 1200


def test_cancelled_career_does_not_create_save(tmp_path):
    import os
    import subprocess

    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path))
    env.pop("NETBBS_DOOR_INFO", None)
    result = subprocess.run(
        [sys.executable, str(_VOIDRUNNER_PATH)], input=b"NewPilot\rN",
        capture_output=True, env=env, timeout=10,
    )
    assert result.returncode == 0
    assert b"Career launch cancelled" in result.stdout
    assert not list(tmp_path.glob("*.json"))


def test_invalid_customs_input_waits_without_mutating_cargo(monkeypatch):
    world = _world_with_seed(42)
    world.save.cargo = {"weapons": 2}
    before = __import__("copy").deepcopy(world.save.to_dict())
    keys = iter(["?", "\r", "S"])

    def choose():
        assert world.save.to_dict() == before
        return next(keys)

    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_customs(vr.Palette(False), world)
    assert world.save.cargo == {}


def test_scan_checkpoint_contains_discovery_and_mission_reward(tmp_path, monkeypatch):
    world = _world_with_seed(42)
    world.save.ship.scanner_tier = 1
    target = next(sid for sid, hops in vr.bfs_hops(world.by_id, 0).items()
                  if hops <= 3 and not world.by_id[sid].discovered)
    world.save.active_missions = [vr.Mission(1, "scan", "Survey", 500, 0, target)]
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    monkeypatch.setattr(world.event_rng, "choice", lambda choices: target)
    with contextlib.redirect_stdout(io.StringIO()):
        vr._do_scan(vr.Palette(False), world)
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert target in saved.discovered
    assert saved.pilot.credits == 1700
    assert saved.active_missions == []


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
    monkeypatch.setattr(vr, "read_line_raw", lambda **kwargs: world.by_id[target].name)
    monkeypatch.setattr(vr, "confirm", lambda *args: True)
    visited = []

    def travel(p, current, dest):
        saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert saved.current_system == (visited[-1] if visited else 0)
        current.save.current_system = dest
        current.save.turn += 1
        visited.append(dest)

    monkeypatch.setattr(vr, "screen_travel", travel)
    with contextlib.redirect_stdout(io.StringIO()):
        vr._screen_auto_route(vr.Palette(False), world)
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert visited == path
    assert saved.current_system == target
    assert saved.turn == 3


def test_retirement_keeps_checkpoint_binding(tmp_path):
    world = vr.World(vr._new_career("Retiring"),
                     checkpoint=lambda current: vr.persist(current, tmp_path, 77))
    world.reset(vr.retire_pilot(world.save))
    world.checkpoint()
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Retiring")
    assert saved.pilot.retirements == 1


def test_failed_atomic_replace_preserves_previous_save_and_removes_own_temp(tmp_path, monkeypatch):
    import os

    save = vr._new_career("Tester")
    vr.write_save(tmp_path, 77, save)
    previous = (tmp_path / "77.json").read_bytes()
    save.pilot.credits += 100

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        vr.write_save(tmp_path, 77, save)
    assert (tmp_path / "77.json").read_bytes() == previous
    assert not list(tmp_path.glob("*.tmp"))



# Every recorded checkpoint must be a valid restart boundary, including the
# gap between a combat's terminal result and its parent's mission payout.
@pytest.mark.parametrize(
    "scenario,seed,keys,evidence",
    [
        ("quiet", 0, "F", "Jumping to"),
        ("pirate", 2, "F", "Raider contact"),
        ("squadron", 45, "F", "squadron contact: 2"),
        ("salvage", 26, "B", "Salvaged a derelict"),
        ("ambush", 160, "B", "weren't as dead"),
        ("distress", 8, "H", "Grateful survivors"),
        ("tip", 9, "F", "trader's data burst"),
        ("ignore_derelict", 26, "?I", "leave the derelict"),
        ("ignore_distress", 8, "?I", "continue past the distress"),
        ("bounty", 0, "F", "Bounty complete!"),
        ("bounty_loss", 0, "F", "Bounty failed"),
        ("bounty_escape", 0, "E", "escape"),
        ("bounty_dump", 0, "D", "dump cargo"),
        ("bounty_bribe", 0, "B", "peels off"),
        ("escorts", 0, "F", "Convoy delivered safely"),
        ("escort_loss", 0, "F", "Escort contract failed"),
        ("patrol_win", 9, "F", "Concord will not forget"),
        ("patrol_loss", 9, "F", "Freeport Anchorage"),
        ("patrol_surrender", 9, "S", "Notoriety cleared"),
        ("patrol_evade", 9, "E", "break contact and escape"),
        ("customs_surrender", 4, "FS", "surrender 2 units"),
        ("customs_bribe", 4, "FB", "changes hands quietly"),
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
        world.save.cargo = {"weapons": 2}
    if scenario in ("escorts", "escort_loss"):
        # Deliberately shared legacy IDs: snapshots must distinguish the jobs.
        world.save.active_missions = [
            vr.Mission(7, "escort", "Convoy A", 500, 0, dest_id, pirate_tier=2),
            vr.Mission(7, "escort", "Convoy B", 600, 0, dest_id, pirate_tier=1),
        ]
    if scenario.startswith("patrol"):
        world.save.pilot.notoriety = 20
    if scenario.endswith("loss"):
        world.save.ship.hull_hp = 1
        world.save.ship.weapon_tier = 0
        world.save.ship.shield_tier = 0
    # Exercise departure costs/settlement and arrival delivery in the same hop.
    world.save.ship.has_navigator = True
    world.save.active_futures = [vr.FuturesContract(1, "food", 2, 40, world.save.turn + 1)]
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


@contextlib.contextmanager
def _door_stopped_at(tmp_path, commands: bytes, acknowledgement: bytes):
    """Run the shipped script, then force-kill while stdin is still open."""
    import json
    import os
    import subprocess
    import threading

    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}), encoding="utf-8")
    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info))
    proc = subprocess.Popen(
        [sys.executable, str(_VOIDRUNNER_PATH)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    reached = threading.Event()
    output = bytearray()

    def read_output():
        while len(output) < 128_000:
            byte = proc.stdout.read(1)
            if not byte:
                return
            output.extend(byte)
            if acknowledgement in output:
                reached.set()
                return

    reader = threading.Thread(target=read_output)
    reader.start()
    try:
        proc.stdin.write(commands)
        proc.stdin.flush()
        assert reached.wait(10), bytes(output).decode("utf-8", errors="replace")
        proc.kill()
        proc.wait(timeout=5)
        yield bytes(output)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        reader.join(timeout=5)
        assert not reader.is_alive()
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()


def test_combat_survives_kill_and_resumes_before_station_access(tmp_path, monkeypatch):
    import json

    world = _world_with_seed(42)
    world.event_rng.seed(0)
    destination = sorted(world.here.connections)[0]
    world.save.active_missions = [
        vr.Mission(1, "bounty", "Intercept raider", 500, 0, destination, pirate_tier=2),
    ]
    world.checkpoint()  # Include the station preparation before real startup.
    vr.persist(world, tmp_path, 77)
    initial = json.loads((tmp_path / "77.json").read_text(encoding="utf-8"))
    with _door_stopped_at(tmp_path, b"CAF", b" damage."):
        saved = json.loads((tmp_path / "77.json").read_text(encoding="utf-8"))
    assert saved["turn"] == initial["turn"] + 1
    combat = saved["pending_travel"]["encounter"]["combat"]
    assert 0 < combat["pirate"]["hp"] < combat["pirate"]["hp_max"]
    assert 0 < saved["ship"]["hull_hp"] < initial["ship"]["hull_hp"]
    assert saved["ship"]["fuel"] < initial["ship"]["fuel"]

    # A fresh executable must resume the opponent, not reveal a station menu.
    with _door_stopped_at(tmp_path, b"Q", b"Tactical Systems:") as output:
        assert b"Resuming your interrupted journey" in output
        assert b"Freeport Anchorage" not in output
        assert json.loads((tmp_path / "77.json").read_text(encoding="utf-8")) == saved

    # The entire career, including RNG position and bounty failure, matches an
    # uninterrupted fight. Use a different requested destination on resume to
    # prove a saved journey cannot be redirected around its encounter.
    expected = vr.World(vr.SaveData.from_dict(initial))
    resumed = vr.World(vr.SaveData.from_dict(saved))
    monkeypatch.setattr(vr, "read_key", lambda: "F")
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), expected, destination)
        vr.screen_travel(vr.Palette(False), resumed, 0)
    assert resumed.save.to_dict() == expected.save.to_dict()
    assert resumed.save.pending_travel is None
    assert resumed.save.current_system == 0
    assert not resumed.save.active_missions


def test_failed_combat_save_does_not_acknowledge_damage_or_retry(tmp_path, monkeypatch):
    import json

    world = _world_with_seed(42)
    destination = world.here.connections[0]
    world.save.active_missions = [vr.Mission(1, "bounty", "Raider", 500, 0, destination, pirate_tier=2)]
    reads = 0

    def checkpoint(current):
        if current.save.ship.hull_hp < 60:
            raise OSError("disk full")
        vr.persist(current, tmp_path, 77)

    def choose():
        nonlocal reads
        reads += 1
        return "F"

    world._checkpoint = checkpoint
    monkeypatch.setattr(vr, "read_key", choose)
    output = io.StringIO()
    with contextlib.redirect_stdout(output), pytest.raises(vr.SaveError):
        vr.screen_travel(vr.Palette(False), world, destination)
    assert reads == 1
    assert " damage." not in output.getvalue()
    saved = json.loads((tmp_path / "77.json").read_text(encoding="utf-8"))
    assert saved["ship"]["hull_hp"] == 60
    combat = saved["pending_travel"]["encounter"]["combat"]
    assert combat["pirate"]["hp"] == combat["pirate"]["hp_max"]


@pytest.mark.parametrize("broken", ["future_version", "missing_fields", "rng"])
def test_unreadable_resume_state_stops_without_replacing_career(tmp_path, broken):
    import json
    import os
    import subprocess

    world = _world_with_seed(42)
    data = world.save.to_dict()
    if broken == "rng":
        data["event_rng_state"] = [3, [1, 2], None]
    else:
        data["pending_travel"] = {"version": 999 if broken == "future_version" else 1}
    path = tmp_path / "77.json"
    original = json.dumps(data).encode("utf-8")
    path.write_bytes(original)
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_VOIDRUNNER_PATH)], input=b" ", capture_output=True,
        env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)),
        timeout=10,
    )
    assert result.returncode == 1
    assert "your saved career is unchanged" in " ".join(
        vr._ANSI_RE.sub("", result.stdout.decode("utf-8")).split()
    )
    assert not result.stderr
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_legacy_save_gains_resume_fields_without_regenerating_galaxy():
    data = _world_with_seed(42).save.to_dict()
    data.pop("pending_travel")
    data.pop("event_rng_state")
    original = vr.generate_galaxy(data["seed"])
    world = vr.World(vr.SaveData.from_dict(data))
    assert world.save.pending_travel is None
    assert world.galaxy == original
    world.checkpoint()
    restored = vr.World(vr.SaveData.from_dict(world.save.to_dict()))
    assert restored.event_rng.random() == world.event_rng.random()


@pytest.mark.parametrize("kind", ["bounty", "escort"])
@pytest.mark.parametrize("field,value", [
    ("pirate_tier", "2"), ("pirate_tier", 5), ("pirate_tier", True),
    ("reward", "500"), ("reward", -1), ("id", False), ("id", 0),
    ("origin_system", -1), ("target_system", vr.GALAXY_SYSTEM_COUNT),
    ("target_system", "1"), ("description", []), ("quantity", "2"),
    ("deadline_turn", -1), ("commodity", []), ("commodity", "unknown"),
])
def test_malformed_resume_mission_uses_recovery_error(kind, field, value):
    mission = vr.Mission(1, kind, "Test contract", 500, 0, 1, pirate_tier=2).to_dict()
    mission[field] = value
    travel = {
        "version": 1, "origin": 0, "destination": 1, "was_discovered": True,
        "destroyed": False, "phase": "primary", "primary": "bounty" if kind == "bounty" else "random",
        "bounty": mission if kind == "bounty" else None,
        "escorts": [mission] if kind == "escort" else [], "escort_index": 0, "encounter": {},
    }
    data = _world_with_seed(42).save.to_dict()
    data["pending_travel"] = travel
    with pytest.raises(vr.ResumeError, match="cannot be read"):
        vr.SaveData.from_dict(data)


@pytest.mark.parametrize("kind", ["bounty", "escort"])
def test_real_door_preserves_bad_resume_mission_and_shows_recovery(tmp_path, kind):
    import json
    import os
    import subprocess

    world = _world_with_seed(42)
    mission = vr.Mission(1, kind, "Test contract", 500, 0, 1, pirate_tier=2).to_dict()
    mission["pirate_tier"] = "2"
    world.save.pending_travel = {
        "version": 1, "origin": 0, "destination": 1, "was_discovered": True,
        "destroyed": False, "phase": "primary", "primary": "bounty" if kind == "bounty" else "random",
        "bounty": mission if kind == "bounty" else None,
        "escorts": [mission] if kind == "escort" else [], "escort_index": 0, "encounter": {},
    }
    # Simulate on-disk damage; the production writer now rejects this state.
    world.save.event_rng_state = world.event_rng.getstate()
    path = tmp_path / "77.json"
    path.write_text(json.dumps(world.save.to_dict()), encoding="utf-8")
    original = path.read_bytes()
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_VOIDRUNNER_PATH)], input=b" ", capture_output=True,
        env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)), timeout=10,
    )
    assert result.returncode == 1
    output = " ".join(vr._ANSI_RE.sub("", result.stdout.decode("utf-8")).split())
    assert "your saved career is unchanged" in output
    assert not result.stderr
    assert path.read_bytes() == original


@pytest.mark.parametrize("fault", [
    "missing_bounty", "missing_escort", "duplicate_escort", "won_with_hp",
    "alive_without_hp", "escaped_without_hp", "destroyed_flag", "wrong_position",
])
def test_inconsistent_resume_stops_before_rewriting_save(tmp_path, fault):
    import json
    import os
    import subprocess

    world = _world_with_seed(42)
    mission = vr.Mission(1, "bounty", "Test contract", 500, 0, 1, pirate_tier=2)
    world.save.active_missions = [mission]
    travel = {
        "version": 1, "origin": 0, "destination": 1, "was_discovered": True,
        "destroyed": False, "phase": "primary", "primary": "bounty",
        "bounty": mission.to_dict(), "escorts": [], "escort_index": 0,
        "encounter": {"combat": {
            "pirate": {"name": "Raider", "tier": 2, "hp": 0, "hp_max": 50},
            "outcome": "won", "lines": [],
        }},
    }
    combat = travel["encounter"]["combat"]
    if fault == "missing_bounty":
        world.save.active_missions = []
    elif fault in ("missing_escort", "duplicate_escort"):
        mission.kind = "escort"
        travel["phase"] = "escorts"
        travel["primary"] = "random"
        travel["escorts"] = [mission.to_dict()]
        if fault == "missing_escort":
            world.save.active_missions = []
        else:
            travel["escorts"].append(mission.to_dict())
    elif fault == "won_with_hp":
        combat["pirate"]["hp"] = 10
    elif fault == "alive_without_hp":
        combat["outcome"] = None
    elif fault == "escaped_without_hp":
        combat["outcome"] = "escaped"
    elif fault == "destroyed_flag":
        combat["outcome"] = "destroyed"
        combat["pirate"]["hp"] = 10
    elif fault == "wrong_position":
        world.save.current_system = 1
    world.save.pending_travel = travel
    # Simulate on-disk damage; the production writer now rejects this state.
    world.save.event_rng_state = world.event_rng.getstate()
    path = tmp_path / "77.json"
    path.write_text(json.dumps(world.save.to_dict()), encoding="utf-8")
    original = path.read_bytes()
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_VOIDRUNNER_PATH)], input=b" ", capture_output=True,
        env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)), timeout=10,
    )
    assert result.returncode == 1
    output = " ".join(vr._ANSI_RE.sub("", result.stdout.decode("utf-8")).split())
    assert "your saved career is unchanged" in output
    assert not result.stderr
    assert path.read_bytes() == original


@pytest.mark.parametrize("sequence", [
    b"\x1b[A", b"\x1b[1;5B", b"\x1bOF", b"\x1b[15~", b"\x1b[[A",
    b"\x1b[<0;25;10M", b"\x1b[MABC", b"\x1b(I", b"\x1bY",
    b"\x1b]0;YBUY\x07", b"\x1bPBUY\x1b\\", b"\x9b1;5C",
    b"\x1b[200~MYAY\r\nBUY\x1b[201~", b"\x1b" + "界".encode("utf-8"),
])
def test_input_decoder_consumes_whole_terminal_key(sequence):
    reader = vr._DoorInput(lambda timeout: stream.read(1))
    stream = io.BytesIO(sequence + b"Z")
    assert reader.read_key() == vr.IGNORED_KEY
    assert reader.read_key() == "Z"
    with pytest.raises(EOFError):
        reader.read_key()


def test_fragmented_sequences_survive_timeout_without_command_suffixes():
    events = iter([b"\x1b", None, b"[", b"1", None, b";", b"5", b"A", b"Z"])
    timeouts = []

    def read(timeout):
        timeouts.append(timeout)
        return next(events)

    reader = vr._DoorInput(read)
    assert reader.read_key() == vr.ESCAPE_KEY
    assert reader.read_key() == vr.IGNORED_KEY
    assert reader.read_key() == vr.IGNORED_KEY
    assert reader.read_key() == "Z"
    # Once a partial key has timed out, wait for actual data instead of causing
    # an endless menu redraw every timeout interval.
    assert timeouts == [None, vr._INPUT_TIMEOUT, None, vr._INPUT_TIMEOUT,
                        vr._INPUT_TIMEOUT, None, vr._INPUT_TIMEOUT,
                        vr._INPUT_TIMEOUT, None]


def test_lone_escape_does_not_swallow_next_deliberate_command():
    events = iter([b"\x1b", None, b"Q"])
    reader = vr._DoorInput(lambda timeout: next(events))
    assert reader.read_key() == vr.ESCAPE_KEY
    assert reader.read_key() == "Q"


@pytest.mark.parametrize("text", ["Jörg", "界", "e\u0301", "Û", "🚀"])
def test_input_decoder_preserves_fragmented_utf8(text):
    events = []
    for byte in text.encode("utf-8"):
        events.extend([bytes([byte]), None])
    events.append(b"")
    reader = vr._DoorInput(lambda timeout: events.pop(0))
    result = []
    while True:
        try:
            key = reader.read_key()
        except EOFError:
            break
        if key != vr.IGNORED_KEY:
            result.append(key)
    assert "".join(result) == text


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


def test_input_decoder_bounds_long_control_strings_and_paste():
    stream = io.BytesIO(b"\x1b[200~" + b"Y" * 5000 + b"\x1b[201~Z")
    reader = vr._DoorInput(lambda timeout: stream.read(1))
    for _ in range(21):
        key = reader.read_key()
        assert len(reader.sequence) <= 8
        if key == "Z":
            break
        assert key == vr.IGNORED_KEY
    else:
        pytest.fail("did not reach the deliberate command after pasted data")


def _use_decoded_input(monkeypatch, data):
    stream = io.BytesIO(data)
    reader = vr._DoorInput(lambda timeout: stream.read(1))
    monkeypatch.setattr(vr, "read_key", reader.read_key)


def test_unicode_line_editing_erases_wide_and_combining_characters(monkeypatch):
    _use_decoded_input(monkeypatch, "界e\u0301\x7f\x08Jörg\r\n7\r".encode("utf-8"))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        name = vr.read_line_raw(16, allowed=lambda c: c.isalnum() or bool(vr.unicodedata.combining(c)))
        quantity = vr.read_line_raw(5)
    assert name == "Jörg"
    assert quantity == "7"  # CRLF submits once, not an empty next field
    assert "\x08 \x08" * 3 in output.getvalue()


def test_numeric_input_rejects_unicode_digits_that_int_cannot_parse(monkeypatch):
    _use_decoded_input(monkeypatch, "²Ⅳ١３5\r".encode("utf-8"))
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.read_line_raw(5) == "5"


def test_text_input_bounds_columns_and_normalizes_name(monkeypatch):
    _use_decoded_input(monkeypatch, "界界界\re\u0301\r".encode("utf-8"))
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.read_line_raw(4, allowed=str.isalnum) == "界界"
        assert vr.read_line_raw(4, allowed=lambda c: True) == "é"


def test_escape_and_paste_cannot_confirm_action(monkeypatch):
    _use_decoded_input(monkeypatch, b"\x1bY\x1b[200~Y\x1b[201~\x1b[1;5YN")
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.confirm("Launch?", vr.Palette(False)) is False


def test_arrow_keys_cannot_accept_missions(monkeypatch):
    world = _world_with_seed(42)
    _use_decoded_input(monkeypatch, b"\x1b[A\x1bOBQ")
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_missions(vr.Palette(False), world)
    assert not world.save.active_missions


def test_unsupported_keys_do_not_acknowledge_result_pause(monkeypatch):
    _use_decoded_input(monkeypatch, b"\x1b[A\x1b[200~Y\x1b[201~KN")
    with contextlib.redirect_stdout(io.StringIO()):
        vr.pause(vr.Palette(False))
    assert vr.read_key() == "N"


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


def test_real_pipe_lone_escape_returns_without_waiting_for_another_byte(tmp_path):
    world = _world_with_seed(42)
    vr.persist(world, tmp_path, 77)
    with _door_stopped_at(tmp_path, b"\x1b", b"<key>"):
        saved, is_new, notice = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert not is_new and notice is None
    assert saved.turn == 0
    assert saved.pilot.credits == 1200


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


@pytest.mark.parametrize("text", ["\u017f", "\u0131", "\u00df"])
def test_unicode_cannot_alias_ascii_commands(monkeypatch, text):
    _use_decoded_input(monkeypatch, (text + "s").encode("utf-8"))
    assert vr.read_command() == vr.IGNORED_KEY
    assert vr.read_command() == "S"


@pytest.mark.parametrize("command", [b"P", b"X", b"p", b"x"])
def test_standalone_escape_does_not_capture_later_hotkeys(command):
    events = iter([b"\x1b", None, command, b"Q"])
    reader = vr._DoorInput(lambda timeout: next(events))
    assert reader.read_key() == vr.ESCAPE_KEY
    assert reader.read_key() == command.decode("ascii")
    assert reader.read_key() == "Q"


def test_control_string_started_before_timeout_retains_its_payload():
    events = iter([b"\x1b", b"P", None, b"Y", b"\x1b", b"\\", b"Q"])
    reader = vr._DoorInput(lambda timeout: next(events))
    assert reader.read_key() == vr.IGNORED_KEY
    assert reader.read_key() == vr.IGNORED_KEY
    assert reader.read_key() == "Q"


def test_posted_missions_survive_reopen_restart_and_acceptance(tmp_path):
    world = _world_with_seed(42)
    first = vr.generate_mission_board(world)
    rng = world.event_rng.getstate()
    assert vr.generate_mission_board(world) == first
    assert world.event_rng.getstate() == rng
    accepted = first[0]
    vr.accept_mission(world, accepted)
    vr.persist(world, tmp_path, 77)
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    restarted = vr.World(saved)
    assert vr.generate_mission_board(restarted) == first[1:]
    assert restarted.save.active_missions == [accepted]
    with pytest.raises(vr.MissionError):
        vr.accept_mission(restarted, accepted)


def test_board_refresh_boundary_and_stale_offer_rejection():
    world = _world_with_seed(42)
    first = vr.generate_mission_board(world)
    world.save.turn += vr.MISSION_BOARD_DAYS - 1
    assert vr.generate_mission_board(world) == first
    world.save.turn += 1
    fresh = vr.generate_mission_board(world)
    assert not {m.id for m in first} & {m.id for m in fresh}
    before = __import__("copy").deepcopy(world.save.to_dict())
    with pytest.raises(vr.MissionError, match="no longer posted"):
        vr.accept_mission(world, first[0])
    assert world.save.to_dict() == before


def test_exhausted_board_does_not_refill_before_refresh():
    world = _world_with_seed(42)
    board = vr.generate_mission_board(world)
    for mission in board:
        vr.accept_mission(world, mission)
        world.save.active_missions.clear()  # completed jobs must not refill offers
    assert vr.generate_mission_board(world) == []
    world.save.turn += vr.MISSION_BOARD_DAYS
    assert vr.generate_mission_board(world)


def test_all_station_boards_have_bounded_unique_offers():
    world = _world_with_seed(42)
    ids = []
    for station in world.galaxy:
        world.save.current_system = station.id
        board = vr.generate_mission_board(world)
        assert len(board) <= 4
        ids.extend(m.id for m in board)
    assert len(world.save.mission_boards) == vr.GALAXY_SYSTEM_COUNT
    assert len(ids) == len(set(ids))
    assert world.save.next_mission_id > max(ids)


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


@pytest.mark.parametrize("kind", ["delivery", "scan"])
@pytest.mark.parametrize("turn", [4, 5, 6])
def test_mission_deadlines_are_inclusive_and_checked_before_rewards(kind, turn):
    world = _world_with_seed(42)
    world.save.turn = turn
    world.save.current_system = 1
    world.save.cargo = {"food": 3}
    world.save.active_missions = [
        vr.Mission(1, kind, "Deadline", 500, 0, 1, commodity="food", quantity=3, deadline_turn=5),
    ]
    before = world.save.pilot.credits
    messages = vr.check_mission_completions(world, just_discovered=1)
    assert world.save.pilot.credits == before + (500 if turn <= 5 else 0)
    assert not world.save.active_missions
    if turn > 5:
        assert "expired" in messages[0]
        assert world.save.cargo == {"food": 3}


@pytest.mark.parametrize("kind", ["bounty", "escort"])
@pytest.mark.parametrize("deadline", [0, 1])
def test_departure_expires_combat_jobs_before_encounters(kind, deadline, monkeypatch):
    world = _world_with_seed(42)
    dest = world.here.connections[0]
    world.save.active_missions = [vr.Mission(1, kind, "Deadline", 500, 0, dest, deadline_turn=deadline, pirate_tier=1)]
    calls = []
    monkeypatch.setattr(vr, "screen_combat", lambda *args: calls.append(1) or "won")
    monkeypatch.setattr(vr, "_resolve_random_travel_encounter", lambda *args: None)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, dest)
    assert calls == ([1] if deadline == 1 else [])
    assert world.save.pilot.credits == 1200 + (500 if deadline == 1 else 0)
    assert not world.save.active_missions


def test_already_charted_survey_is_unavailable_without_replacement():
    world = _world_with_seed(42)
    target = next(s.id for s in world.galaxy if not s.discovered)
    mission = vr.Mission(1, "scan", "Survey", 500, 0, target)
    world.save.mission_boards[0] = {"refresh_turn": 3, "offers": [mission.to_dict()]}
    world.by_id[target].discovered = True
    assert vr.generate_mission_board(world) == []
    with pytest.raises(vr.MissionError, match="already charted"):
        vr.accept_mission(world, mission)
    assert world.save.mission_boards[0]["offers"] == [mission.to_dict()]


def test_legacy_duplicate_ids_repaired_only_after_pending_travel():
    world = _world_with_seed(42)
    world.save.active_missions = [
        vr.Mission(1, "escort", "A", 500, 0, 1, pirate_tier=1),
        vr.Mission(1, "escort", "B", 500, 0, 1, pirate_tier=1),
    ]
    world.save.pending_travel = {
        "version": 1, "origin": 0, "destination": 1, "was_discovered": True,
        "destroyed": False, "phase": "primary", "primary": "random", "bounty": None,
        "escorts": [m.to_dict() for m in world.save.active_missions], "escort_index": 0, "encounter": {},
    }
    world.save.event_rng_state = world.event_rng.getstate()
    resumed = vr.World(vr.SaveData.from_dict(world.save.to_dict()))
    resumed.checkpoint()
    assert [m.id for m in resumed.save.active_missions] == [1, 1]
    resumed.save.pending_travel = None
    resumed.checkpoint()
    ids = [m.id for m in resumed.save.active_missions]
    assert len(set(ids)) == 2
    assert resumed.save.next_mission_id > max(ids)


def test_legacy_over_limit_career_keeps_every_contract():
    world = _world_with_seed(42)
    world.save.active_missions = [vr.Mission(1, "scan", f"Legacy {i}", 500, 0, 1) for i in range(5)]
    restored = vr.World(vr.SaveData.from_dict(world.save.to_dict()))
    assert len(restored.save.active_missions) == 5
    assert len({m.id for m in restored.save.active_missions}) == 5
    offer = vr.generate_mission_board(restored)[0]
    with pytest.raises(vr.MissionError, match="at most"):
        vr.accept_mission(restored, offer)


def test_real_board_reopen_and_kill_retains_posted_terms(tmp_path):
    world = _world_with_seed(42)
    vr.persist(world, tmp_path, 77)
    with _door_stopped_at(tmp_path, b"B", b"Details"):
        first, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    with _door_stopped_at(tmp_path, b"B", b"Details"):
        reopened, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert first.mission_boards and reopened.mission_boards == first.mission_boards
    assert reopened.next_mission_id == first.next_mission_id
    assert reopened.event_rng_state == first.event_rng_state


@pytest.mark.parametrize("kind", ["bounty", "escort"])
@pytest.mark.parametrize("destroyed", [False, True])
def test_resumed_legacy_expired_combat_job_cannot_pay_or_start_another_wave(kind, destroyed, monkeypatch):
    world = _world_with_seed(42)
    world.save.turn = 2
    expired = vr.Mission(1, kind, "Expired", 500, 0, 1, deadline_turn=1, pirate_tier=1)
    later = vr.Mission(2, "escort", "Later", 500, 0, 1, deadline_turn=5, pirate_tier=1)
    world.save.active_missions = [expired] + ([later] if destroyed and kind == "escort" else [])
    travel = {
        "version": 1, "origin": 0, "destination": 1, "was_discovered": True,
        "destroyed": destroyed, "phase": "primary" if kind == "bounty" else "escorts",
        "primary": kind if kind == "bounty" else "random",
        "bounty": expired.to_dict() if kind == "bounty" else None,
        "escorts": [m.to_dict() for m in world.save.active_missions if m.kind == "escort"],
        "escort_index": 0, "encounter": {},
    }
    world.save.pending_travel = travel
    world.save.event_rng_state = world.event_rng.getstate()
    world = vr.World(vr.SaveData.from_dict(world.save.to_dict()))
    monkeypatch.setattr(vr, "screen_combat", lambda *args: pytest.fail("Expired job started combat"))
    with contextlib.redirect_stdout(io.StringIO()):
        if kind == "bounty":
            vr._resolve_bounty(vr.Palette(False), world, world.save.pending_travel)
        else:
            vr._resolve_escort_missions(vr.Palette(False), world, 1)
    assert world.save.pilot.credits == 1200
    assert expired not in world.save.active_missions
    assert world.save.pilot.missions_completed == 0
    if destroyed and kind == "escort":
        assert world.save.active_missions == [later]
        assert world.save.pending_travel["escort_index"] == 1


def test_board_browsing_never_checkpoints_or_changes_state(monkeypatch):
    import copy
    world = _world_with_seed(42)
    world.checkpoint()
    before = copy.deepcopy(world.save.to_dict())
    rng = world.event_rng.getstate()
    monkeypatch.setattr(world, "checkpoint", lambda: pytest.fail("Browsing wrote the save"))
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_missions(vr.Palette(False), world)
    assert world.save.to_dict() == before
    assert world.event_rng.getstate() == rng


def test_station_checkpoint_prepares_board_and_expires_jobs():
    world = _world_with_seed(42)
    world.save.turn = 5
    world.save.active_missions = [vr.Mission(1, "scan", "Expired", 500, 0, 1, deadline_turn=4)]
    writes = []
    world._checkpoint = lambda current: writes.append(current.save.to_dict())
    world.checkpoint()
    assert len(writes) == 1
    assert writes[0]["mission_boards"]["0"]["refresh_turn"] == 8
    assert not writes[0]["active_missions"]
    assert vr.posted_mission_offers(world)


def test_unprepared_board_browsing_does_not_generate_or_expire(monkeypatch):
    import copy
    world = _world_with_seed(42)
    world.save.turn = 5
    world.save.active_missions = [vr.Mission(1, "scan", "Expired", 500, 0, 1, deadline_turn=4)]
    before = copy.deepcopy(world.save.to_dict())
    monkeypatch.setattr(world, "checkpoint", lambda: pytest.fail("Browsing wrote the save"))
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_missions(vr.Palette(False), world)
    assert world.save.to_dict() == before


@pytest.mark.parametrize("seed", [0, 42, 310])
def test_board_preparation_does_not_advance_encounter_rng(seed):
    world = _world_with_seed(seed)
    before = world.event_rng.getstate()
    world.checkpoint()
    assert world.event_rng.getstate() == before
    assert vr.posted_mission_offers(world)



def _mission_details_world(kind="delivery"):
    world = _world_with_seed(42)
    target = next(s.id for s in world.galaxy if not s.discovered)
    mission = vr.Mission(1, kind, "Complete and unabridged contract objective", 500, 0, target,
                         commodity="food" if kind == "delivery" else None,
                         quantity=3 if kind == "delivery" else None, deadline_turn=10, pirate_tier=2)
    world.save.mission_boards[0] = {"refresh_turn": 3, "offers": [mission.to_dict()]}
    return world, mission


def test_mission_preview_back_and_paging_are_read_only(monkeypatch):
    import copy
    world, mission = _mission_details_world()
    before = copy.deepcopy(world.save.to_dict())
    rng = world.event_rng.getstate()
    keys = iter("1NPBB")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(world, "checkpoint", lambda: pytest.fail("Preview saved"))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        vr.screen_missions(vr.Palette(False), world)
    assert mission.description in output.getvalue()
    assert world.save.to_dict() == before
    assert world.event_rng.getstate() == rng
    assert not world.by_id[mission.target_system].discovered


def test_acceptance_requires_last_details_page_and_checkpoints_before_ack(monkeypatch):
    world, mission = _mission_details_world()
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 12)
    pages = vr._mission_text_pages(vr.mission_details(world, mission))
    assert len(pages) > 1
    # Premature A is ignored. Only the final A commits, before acknowledgement.
    keys = iter("A" + "N" * (len(pages) - 1) + "AK")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    output = io.StringIO()
    commits = []
    def commit(current):
        assert "Accepted:" not in output.getvalue()
        commits.append(current.save.to_dict())
    world._checkpoint = commit
    with contextlib.redirect_stdout(output):
        vr.screen_mission_details(vr.Palette(False), world, mission, active=False)
    assert len(commits) == 1
    assert [m.id for m in world.save.active_missions] == [mission.id]
    assert not world.save.mission_boards[0]["offers"]


@pytest.mark.parametrize("width,height", [(40, 24), (80, 24), (40, 12), (20, 10)])
def test_full_contract_details_fit_each_page_and_retain_back(monkeypatch, width, height):
    world, mission = _mission_details_world("escort")
    mission.description = "Very long objective " * 20
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
    output = io.StringIO()
    frames = []
    offset = 0
    count = len(vr._mission_text_pages(vr.mission_details(world, mission)))
    def key():
        nonlocal offset
        frame = output.getvalue()[offset:]
        offset = len(output.getvalue())
        frames.append(frame)
        rows = frame.split("\r\n")
        assert len(rows) <= height
        assert all(vr._visible_width(row) <= width for row in rows)
        assert "[B]ack" in frame
        return "N" if len(frames) < count else "B"
    monkeypatch.setattr(vr, "read_key", key)
    with contextlib.redirect_stdout(output):
        vr.screen_mission_details(vr.Palette(False), world, mission, active=False)
    body_rows = [row for row in vr._ANSI_RE.sub("", output.getvalue()).split("\r\n")
                 if not row.startswith(("Contract #", "[N]", "[B]", "[A]"))]
    joined = " ".join(" ".join(body_rows).split())
    assert "EVERY jump" in joined and "including detours" in joined
    assert "no cargo space" in joined
    assert len(frames) == count


def test_legacy_active_contract_list_is_paginated_and_selectable(monkeypatch):
    world = _world_with_seed(42)
    world.save.active_missions = [vr.Mission(i + 1, "scan", f"Survey {i}", 500, 0, 1) for i in range(30)]
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 40)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 24)
    output = io.StringIO()
    selected = []
    def key():
        return "N" if "Contracts 2/" not in output.getvalue() else "1" if not selected else "B"
    monkeypatch.setattr(vr, "read_key", key)
    monkeypatch.setattr(vr, "screen_mission_details", lambda p, w, m, active: selected.append((m.id, active)))
    with contextlib.redirect_stdout(output):
        vr.screen_missions(vr.Palette(False), world)
    assert selected and selected[0][0] > 1 and selected[0][1]
    assert "SURVEY" in output.getvalue()
    assert "CARGO" not in output.getvalue()
    assert len(world.save.active_missions) == 30


@pytest.mark.parametrize("kind", ["delivery", "scan", "bounty", "escort"])
def test_contract_terms_show_kind_obligations_and_cost_limits(kind):
    world, mission = _mission_details_world(kind)
    world.save.ship.has_navigator = True
    world.save.cargo = {"food": 1}
    lines = " ".join(vr.mission_details(world, mission))
    assert "inclusive" in lines and "Target danger: uncharted" in lines
    assert "Gross payout" in lines and "not total profit" in lines
    assert "repairs" in lines and "wages" in lines
    if kind == "delivery":
        price = vr.price_for(world, 0, "food")
        assert f"2 x {price} = {2 * price:,} cr" in lines
        assert "Delivery consumes the cargo" in lines
    elif kind == "scan":
        assert "scanner" in lines and "does not chart" in lines
    elif kind == "bounty":
        assert "tier 2" in lines and "mistaken-identity" in lines
    else:
        assert "EVERY jump" in lines and "Other escort contracts" in lines


@pytest.mark.parametrize("action", ["complete", "expire", "abandon"])
def test_tracking_roundtrips_and_clears_with_contract_removal(tmp_path, action):
    world, mission = _mission_details_world()
    vr.accept_mission(world, mission)
    vr.track_mission(world, mission.id)
    world.checkpoint()
    vr.persist(world, tmp_path, 77)
    save, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    world = vr.World(save)
    assert vr.tracked_mission(world).id == mission.id
    if action == "complete":
        world.save.current_system = mission.target_system
        world.save.cargo = {"food": 3}
        vr.check_mission_completions(world)
    elif action == "expire":
        world.save.turn = 11
        vr.expire_missions(world)
    else:
        vr.abandon_mission(world, mission.id)
    assert vr.tracked_mission(world) is None
    assert world.save.tracked_mission_id is None


def test_abandonment_cancel_is_read_only_and_confirmed_action_keeps_cargo(monkeypatch):
    import copy
    world, mission = _mission_details_world()
    vr.accept_mission(world, mission)
    vr.track_mission(world, mission.id)
    world.save.cargo = {"food": 3}
    before = copy.deepcopy(world.save.to_dict())
    keys = iter("DNB")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_mission_details(vr.Palette(False), world, mission, active=True)
    assert world.save.to_dict() == before
    keys = iter("DYK")
    commits = []
    world._checkpoint = lambda current: commits.append(current.save.to_dict())
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_mission_details(vr.Palette(False), world, mission, active=True)
    assert len(commits) == 1
    assert world.save.cargo == {"food": 3}
    assert world.save.pilot.credits == before["pilot"]["credits"]
    assert not world.save.active_missions and world.save.tracked_mission_id is None
    assert not world.save.mission_boards[0]["offers"]


def test_real_tracking_action_survives_termination_and_is_visible_on_return(tmp_path):
    world, mission = _mission_details_world("scan")
    vr.accept_mission(world, mission)
    world.checkpoint()
    vr.persist(world, tmp_path, 77)
    choice = len(vr.posted_mission_offers(world)) + 1
    with _door_stopped_at(tmp_path, f"B{choice}T".encode(), b"Tracking updated."):
        save, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert save.tracked_mission_id == mission.id
    with _door_stopped_at(tmp_path, b"", b"Tracked") as output:
        assert b"Tracked" in output


def test_tracking_and_abandonment_reject_pending_travel():
    world, mission = _mission_details_world()
    vr.accept_mission(world, mission)
    world.save.pending_travel = {"phase": "primary"}
    with pytest.raises(vr.MissionError, match="interrupted journey"):
        vr.track_mission(world, mission.id)
    with pytest.raises(vr.MissionError, match="interrupted journey"):
        vr.abandon_mission(world, mission.id)
    assert world.save.active_missions == [mission]


@pytest.mark.parametrize("keys,ack", [("T", "Tracking updated"), ("DY", "Abandoned:")])
def test_contract_management_save_failure_precedes_acknowledgement(monkeypatch, keys, ack):
    world, mission = _mission_details_world()
    vr.accept_mission(world, mission)
    commands = iter(keys)
    monkeypatch.setattr(vr, "read_key", lambda: next(commands))
    def fail(current):
        raise OSError("disk full")
    world._checkpoint = fail
    output = io.StringIO()
    with contextlib.redirect_stdout(output), pytest.raises(vr.SaveError):
        vr.screen_mission_details(vr.Palette(False), world, mission, active=True)
    assert ack not in output.getvalue()


@pytest.mark.parametrize("discovered", [False, True])
def test_tracked_chart_hint_identifies_the_actual_destination_key(monkeypatch, discovered):
    world, mission = _mission_details_world("scan")
    vr.accept_mission(world, mission)
    vr.track_mission(world, mission.id)
    first = vr.mission_route(world, mission)[0]
    world.by_id[first].discovered = discovered
    world.save.ship.fuel = 100
    key = vr.CHART_CONNECTION_LETTERS[sorted(world.here.connections).index(first)]
    monkeypatch.setattr(vr, "read_key", lambda: key)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        dest = vr.screen_chart(vr.Palette(False), world)
    assert dest == first
    assert f"Tracked route: [{key}]" in output.getvalue()
    assert world.by_id[first].discovered is discovered


def test_bounty_retry_requires_reentry_and_budgets_two_jumps():
    world, mission = _mission_details_world("bounty")
    vr.accept_mission(world, mission)
    world.save.current_system = mission.target_system
    world.save.turn = mission.deadline_turn - 1
    world.save.ship.has_navigator = True
    world.save.ship.fuel = 0
    route = vr.mission_route(world, mission)
    assert len(route) == 2 and route[-1] == mission.target_system
    lines = " ".join(vr.mission_details(world, mission))
    assert "jump back" in lines and "both jumps" in lines
    assert "shortest route misses the deadline" in lines
    assert "2 jump(s)" in vr.mission_bearing(world, mission)
    assert "wages 10 cr/jump, 20 cr total" in lines


@pytest.mark.parametrize("kind", ["bounty", "escort"])
@pytest.mark.parametrize("outcome", ["won", "destroyed", "expired"])
def test_pending_mission_resolution_checkpoint_clears_tracking(monkeypatch, kind, outcome):
    world, mission = _mission_details_world(kind)
    vr.accept_mission(world, mission)
    vr.track_mission(world, mission.id)
    world.save.pending_travel = {
        "phase": "primary" if kind == "bounty" else "escorts", "bounty": mission.to_dict(),
        "escorts": [mission.to_dict()] if kind == "escort" else [], "escort_index": 0,
        "encounter": {},
    }
    if outcome == "expired":
        world.save.turn = 11
    monkeypatch.setattr(vr, "screen_combat", lambda *args: outcome)
    seen = []
    def checkpoint(current):
        if not current.save.active_missions:
            assert current.save.pending_travel is not None
            assert current.save.tracked_mission_id is None
            seen.append(1)
    world._checkpoint = checkpoint
    with contextlib.redirect_stdout(io.StringIO()):
        if kind == "bounty":
            vr._resolve_bounty(vr.Palette(False), world, world.save.pending_travel)
        else:
            vr._resolve_escort_missions(vr.Palette(False), world, mission.target_system)
    assert seen



def test_queued_bounty_route_budgets_preceding_fights():
    world, mission = _mission_details_world("bounty")
    earlier = vr.Mission(9, "bounty", "Earlier", 500, 0, mission.target_system, pirate_tier=1)
    world.save.active_missions = [earlier]
    base = vr.bfs_path(world.by_id, 0, mission.target_system)
    assert len(vr.mission_route(world, mission)) == len(base) + 2
    vr.accept_mission(world, mission)
    assert len(vr.mission_route(world, mission)) == len(base) + 2
    assert "1 earlier contract" in " ".join(vr.mission_details(world, mission))
    world.save.current_system = mission.target_system
    assert len(vr.mission_route(world, mission)) == 4
    world.save.active_missions.remove(earlier)
    assert len(vr.mission_route(world, mission)) == 2


def test_tiny_contract_board_splits_entries_and_keeps_selection(monkeypatch):
    import re
    world, mission = _mission_details_world("escort")
    world.by_id[mission.target_system].name = "A particularly long system name that cannot fit one page"
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 20)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 10)
    output = io.StringIO()
    offset = 0
    frames = []
    selected = []
    def key():
        nonlocal offset
        frame = output.getvalue()[offset:]
        offset = len(output.getvalue())
        frames.append(frame)
        assert len(frame.split("\r\n")) <= 10
        assert all(vr._visible_width(row) <= 20 for row in frame.split("\r\n"))
        plain = vr._ANSI_RE.sub("", frame)
        page, total = map(int, re.search(r"Contracts (\d+)/(\d+)", plain).groups())
        return "N" if page < total else "1" if not selected else "B"
    monkeypatch.setattr(vr, "read_key", key)
    monkeypatch.setattr(vr, "screen_mission_details", lambda p, w, m, active: selected.append(m.id))
    with contextlib.redirect_stdout(output):
        vr.screen_missions(vr.Palette(False), world)
    assert len(frames) > 2 and selected == [mission.id]


def test_repeated_one_unit_contraband_recycling_cannot_unlock_membership(monkeypatch):
    world = _world_with_seed(42)
    world.here.economy = "Haven"
    world.save.pilot.credits = 10000
    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: "1")
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(38):
            monkeypatch.setattr(vr, "read_key", lambda: "B")
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
    assert split.save.pilot.reputation[vr.FACTION_BLACKWAKE] == 3
    vr.persist(split, tmp_path, 77)
    save, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    split = vr.World(save)
    vr.record_contraband_trade(split, "weapons", -1000)
    vr.record_contraband_trade(split, "weapons", 1000)
    assert split.save.pilot.reputation[vr.FACTION_BLACKWAKE] == 3
    vr.record_contraband_trade(split, "weapons", 500)
    assert split.save.pilot.reputation[vr.FACTION_BLACKWAKE] == 4


def test_legacy_trade_ledger_defaults_preserve_existing_standing():
    world = _world_with_seed(42)
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = 80
    data = world.save.to_dict()
    data.pop("contraband_trade_balance")
    data.pop("contraband_trade_milestones")
    loaded = vr.SaveData.from_dict(data)
    assert loaded.pilot.reputation[vr.FACTION_BLACKWAKE] == 80
    assert loaded.contraband_trade_balance == loaded.contraband_trade_milestones == 0


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
    world.save.cargo = {"ore": vr.cargo_capacity(world.save.ship)}
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
    monkeypatch.setattr(world, "checkpoint", lambda: pytest.fail("Draft persisted"))
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


def test_pickup_arrival_and_delivery_reward_resume_exactly_once(monkeypatch):
    import copy
    world = _world_with_seed(42)
    dest = world.here.connections[0]
    world.save.active_futures = [vr.FuturesContract(1, "food", 2, 22, 1, origin_system=dest, principal=20)]
    world.save.active_missions = [vr.Mission(1, "delivery", "Pickup delivery", 500, 0, dest, commodity="food", quantity=2)]
    snapshots = []
    world._checkpoint = lambda current: snapshots.append(copy.deepcopy(current.save.to_dict()))
    monkeypatch.setattr(vr, "_resolve_random_travel_encounter", lambda *args: None)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, dest)
    expected = world.save.to_dict()
    assert world.save.pilot.credits == 1700
    assert not world.save.active_futures and not world.save.active_missions
    for snapshot in snapshots:
        if snapshot["pending_travel"] is None:
            continue
        resumed = vr.World(vr.SaveData.from_dict(snapshot))
        with contextlib.redirect_stdout(io.StringIO()):
            vr.screen_travel(vr.Palette(False), resumed, dest)
        assert resumed.save.to_dict() == expected


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


def test_legacy_futures_keep_absent_pickup_metadata_across_checkpoints():
    original = vr.FuturesContract(1, "food", 1, 100, 5)
    data = original.to_dict()
    assert "origin_system" not in data and "principal" not in data
    assert vr.FuturesContract.from_dict(data).to_dict() == data


@contextlib.contextmanager
def _live_voidrunner(tmp_path, user_id=77, commands=b"", acknowledgement=b"Station Services"):
    """Own a real door process until the test exits, draining its output."""
    import json
    import os
    import subprocess
    import threading

    tmp_path.mkdir(parents=True, exist_ok=True)
    info = tmp_path / f"info-{user_id}.json"
    info.write_text(json.dumps({"user_id": user_id, "handle": "Tester"}), encoding="utf-8")
    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info))
    proc = subprocess.Popen([sys.executable, str(_VOIDRUNNER_PATH)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    output = bytearray()
    reached = threading.Event()

    def drain():
        while byte := proc.stdout.read(1):
            if len(output) < 128_000:
                output.extend(byte)
                if acknowledgement in output:
                    reached.set()

    reader = threading.Thread(target=drain)
    reader.start()
    try:
        proc.stdin.write(commands)
        proc.stdin.flush()
        assert reached.wait(10), output.decode("utf-8", errors="replace")
        yield proc, env
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        reader.join(timeout=5)
        assert not reader.is_alive()
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()


@pytest.mark.parametrize("new_career", [False, True])
def test_second_real_launch_cannot_load_or_replace_an_active_pilot(tmp_path, new_career):
    import subprocess

    if not new_career:
        vr.write_save(tmp_path, 77, _world_with_seed(42).save)
    ack = b"Pilot callsign" if new_career else b"Station Services"
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
    with _live_voidrunner(tmp_path, commands=b"MAB1\r", acknowledgement=b"Bought 1x Food") as (proc, env):
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


def test_simultaneous_processes_retain_every_pilot_score(tmp_path):
    import subprocess

    script = """
import runpy, sys
from pathlib import Path
vr = runpy.run_path(sys.argv[1])
uid = int(sys.argv[3])
directory = Path(sys.argv[2])
print('ready', flush=True)
sys.stdin.read(1)
with vr['pilot_session'](directory, uid):
    world = vr['World'](vr['_new_career']('Pilot' + str(uid)))
    for count in range(12):
        world.save.pilot.credits = uid * 1000 + count
        vr['persist'](world, directory, uid)
"""
    processes = []
    try:
        for uid in range(1, 9):
            processes.append(subprocess.Popen(
                [sys.executable, "-c", script, str(_VOIDRUNNER_PATH), str(tmp_path), str(uid)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for proc in processes:
            proc.stdin.write(b"x")
            proc.stdin.flush()
        for proc in processes:
            _, errors = proc.communicate(timeout=15)
            assert proc.returncode == 0, errors
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                pipe.close()
    entries = vr.load_hall_of_fame(tmp_path)
    assert {e["user_id"]: e["best_credits"] for e in entries} == {uid: uid * 1000 + 11 for uid in range(1, 9)}
    assert not list(tmp_path.rglob("*.tmp"))


def test_score_outside_top_twenty_retains_its_peak_on_return(tmp_path):
    veteran = vr._new_career("Veteran")
    veteran.pilot.credits = 9000
    vr.update_hall_of_fame(tmp_path, 1, veteran)
    for uid in range(2, 23):
        save = vr._new_career(f"Pilot{uid}")
        save.pilot.credits = 10_000 + uid
        vr.update_hall_of_fame(tmp_path, uid, save)
    assert all(e["user_id"] != 1 for e in vr.load_hall_of_fame(tmp_path))
    veteran.pilot.credits = 100
    vr.update_hall_of_fame(tmp_path, 1, veteran)
    # Temporarily reduce only the display population, as in a restored subset.
    retained = tmp_path / "subset"
    retained.mkdir()
    (retained / "scores").mkdir()
    (retained / "scores" / "1.json").write_bytes((tmp_path / "scores" / "1.json").read_bytes())
    assert vr.load_hall_of_fame(retained)[0]["best_credits"] == 9000


def test_legacy_scores_are_preserved_and_new_counters_take_precedence(tmp_path):
    import json

    legacy = [{"user_id": 77, "handle": "Old", "best_credits": 9000, "kills": 2}]
    path = tmp_path / "leaderboard.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    original = path.read_bytes()
    world = _world_with_seed(42)
    world.save.pilot.handle = "Renamed"
    world.save.pilot.kills = 10
    vr.persist(world, tmp_path, 77)
    entry = vr.load_hall_of_fame(tmp_path)[0]
    assert entry["best_credits"] == world.save.best_credits == 9000
    assert entry["handle"] == "Renamed" and entry["kills"] == 10
    assert path.read_bytes() == original


def test_failed_score_write_is_repaired_from_saved_peak_after_spending_and_retirement(tmp_path, monkeypatch):
    world = _world_with_seed(42)
    world.save.pilot.credits = 12_000
    replace = vr.os.replace

    def fail_score(source, target):
        if target.parent.name == "scores":
            raise OSError("score directory temporarily unavailable")
        return replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(vr.os, "replace", fail_score)
        vr.persist(world, tmp_path, 77)
    loaded, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert loaded.best_credits == 12_000
    assert vr.load_hall_of_fame(tmp_path) == []
    loaded.pilot.credits = 10
    retired = vr.retire_pilot(loaded)
    vr.persist(vr.World(retired), tmp_path, 77)
    assert vr.load_hall_of_fame(tmp_path)[0]["best_credits"] == 12_000
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize("malformed", [None, [], {"user_id": 77, "handle": [], "best_credits": 9},
                                      {"user_id": 77, "handle": "Bad", "best_credits": "9"}])
def test_malformed_score_entries_do_not_break_career_checkpoint(tmp_path, malformed):
    import json

    (tmp_path / "leaderboard.json").write_text(json.dumps([malformed]), encoding="utf-8")
    (tmp_path / "scores").mkdir()
    (tmp_path / "scores" / "77.json").write_text(json.dumps(malformed), encoding="utf-8")
    world = _world_with_seed(42)
    vr.persist(world, tmp_path, 77)
    assert vr.load_hall_of_fame(tmp_path)[0]["best_credits"] == world.save.pilot.credits


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


def test_legacy_optional_fields_and_overlimit_contracts_remain_compatible():
    import json

    world = _world_with_seed(42)
    world.save.active_missions = [vr.Mission(1, "bounty", "Old contract", 50, 0, 1, pirate_tier=1)] * 5
    data = world.save.to_dict()
    for key in ("galaxy_version", "mission_boards", "best_credits", "active_futures", "pending_travel",
                "event_rng_state", "next_mission_id", "flags", "market_drift"):
        data.pop(key)
    loaded = vr._decode_career(json.dumps(data).encode())
    assert loaded.galaxy_version == 1 and len(loaded.active_missions) == 5
    assert vr.generate_galaxy(loaded.seed) == vr.generate_galaxy(world.save.seed)
    resumed = vr.World(loaded)
    assert len({m.id for m in resumed.save.active_missions}) == 5


def test_previous_checkpoint_tracks_changes_and_identical_writes_do_not_age_it(tmp_path):
    save = _world_with_seed(42).save
    vr.write_save(tmp_path, 77, save)
    original = (tmp_path / "77.json").read_bytes()
    save.pilot.credits += 100
    vr.write_save(tmp_path, 77, save)
    previous = tmp_path / "77.previous.json"
    assert previous.read_bytes() == original
    vr.write_save(tmp_path, 77, save)
    assert previous.read_bytes() == original
    current = (tmp_path / "77.json").read_bytes()
    save.pilot.credits += 100
    vr.write_save(tmp_path, 77, save)
    assert previous.read_bytes() == current


def test_invalid_outgoing_checkpoint_does_not_replace_either_saved_copy(tmp_path):
    save = _world_with_seed(42).save
    vr.write_save(tmp_path, 77, save)
    save.pilot.credits += 10
    vr.write_save(tmp_path, 77, save)
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    save.ship.fuel = -1
    with pytest.raises(vr.SaveError, match="invalid career"):
        vr.write_save(tmp_path, 77, save)
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.json")} == before


def test_failed_previous_copy_prevents_current_save_replacement(tmp_path, monkeypatch):
    save = _world_with_seed(42).save
    vr.write_save(tmp_path, 77, save)
    original = (tmp_path / "77.json").read_bytes()
    replace = vr.os.replace

    def fail_previous(source, target):
        if target.name == "77.previous.json":
            raise OSError("previous copy unavailable")
        return replace(source, target)

    monkeypatch.setattr(vr.os, "replace", fail_previous)
    save.pilot.credits += 10
    with pytest.raises(OSError):
        vr.write_save(tmp_path, 77, save)
    assert (tmp_path / "77.json").read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_pending_journey_is_validated_and_preserved_through_recovery(tmp_path, monkeypatch):
    import json

    world = _world_with_seed(42)
    world.event_rng.seed(0)
    destination = world.here.connections[0]
    snapshots = []
    world._checkpoint = lambda current: snapshots.append(json.dumps(current.save.to_dict()).encode())
    monkeypatch.setattr(vr, "read_key", lambda: "F")
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, destination)
    pending = next(raw for raw in snapshots if json.loads(raw)["pending_travel"] is not None)
    (tmp_path / "77.previous.json").write_bytes(pending)
    (tmp_path / "77.json").write_bytes(b"broken")
    restored = vr.restore_previous_career(tmp_path, 77, pending)
    assert restored.pending_travel == json.loads(pending)["pending_travel"]
    resumed = vr.World(restored)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), resumed, destination)
    assert resumed.save.to_dict() == world.save.to_dict()


def _broken_career_with_previous(tmp_path):
    save = _world_with_seed(42).save
    save.turn = 7
    save.pilot.handle = "Recovered Pilot"
    vr.write_save(tmp_path, 77, save)
    expected = (tmp_path / "77.json").read_bytes()
    save.turn = 8
    vr.write_save(tmp_path, 77, save)
    (tmp_path / "77.json").write_bytes(b"damaged original")
    return expected


@pytest.mark.parametrize("commands", [b"B", b"RNB", b"R", b"", b"\x1b[AQ"])
def test_real_recovery_back_decline_eof_and_special_keys_write_nothing(tmp_path, commands):
    import json
    import os
    import subprocess

    _broken_career_with_previous(tmp_path)
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}), encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=commands, capture_output=True,
                            env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)), timeout=10)
    assert result.returncode == (0 if commands.endswith((b"B", b"Q")) else 1)
    assert not result.stderr
    assert b"Career recovery" in result.stdout and b"Day 7" in result.stdout
    assert b"Pilot callsign" not in result.stdout
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.json")} == before


def test_real_confirmed_recovery_preserves_original_before_success_and_resumes(tmp_path):
    expected = _broken_career_with_previous(tmp_path)
    with _door_stopped_at(tmp_path, b"RY", b"Previous checkpoint restored"):
        restored, is_new, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert not is_new and restored.turn == 7
        archives = list(tmp_path.glob("77.recovery-*.json"))
        assert len(archives) == 1 and archives[0].read_bytes() == b"damaged original"
        assert (tmp_path / "77.previous.json").read_bytes() == expected
    with _door_stopped_at(tmp_path, b"Q", b"Welcome back, Recovered Pilot"):
        restored, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert restored.turn == 7


@pytest.mark.parametrize("failure", ["archive", "replacement", "changed", "full"])
def test_recovery_failure_keeps_primary_and_previous(tmp_path, monkeypatch, failure):
    expected = _broken_career_with_previous(tmp_path)
    if failure == "archive":
        monkeypatch.setattr(vr.tempfile, "NamedTemporaryFile", lambda **kw: (_ for _ in ()).throw(PermissionError()))
    elif failure == "replacement":
        replace = vr.os.replace

        def fail_primary(source, destination):
            if Path(destination).name == "77.json":
                raise OSError("primary replacement failed")
            return replace(source, destination)

        monkeypatch.setattr(vr.os, "replace", fail_primary)
    elif failure == "changed":
        expected = expected + b" "
    else:
        for index in range(vr.MAX_RECOVERY_COPIES):
            (tmp_path / f"77.recovery-{index}.json").write_bytes(b"retained")
    before_previous = (tmp_path / "77.previous.json").read_bytes()
    with pytest.raises((OSError, vr.ResumeError)):
        vr.restore_previous_career(tmp_path, 77, expected)
    assert (tmp_path / "77.json").read_bytes() == b"damaged original"
    assert (tmp_path / "77.previous.json").read_bytes() == before_previous
    if failure == "replacement":
        archives = list(tmp_path.glob("77.recovery-*.json"))
        assert len(archives) == 1 and archives[0].read_bytes() == b"damaged original"


@pytest.mark.parametrize("failure", ["write", "flush", "fsync", "close", "publish"])
def test_failed_recovery_archive_never_consumes_a_retained_slot(tmp_path, monkeypatch, failure):
    previous = _broken_career_with_previous(tmp_path)
    create_temporary = vr.tempfile.NamedTemporaryFile

    class FailingArchive:
        def __init__(self, **kwargs):
            self.file = create_temporary(**kwargs)
            self.name = self.file.name

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()
            if failure == "close":
                raise OSError("archive close failed")

        def write(self, data):
            if failure == "write":
                self.file.write(data[:3])
                raise OSError("archive write failed")
            return self.file.write(data)

        def flush(self):
            if failure == "flush":
                raise OSError("archive flush failed")
            self.file.flush()

        def fileno(self):
            return self.file.fileno()

    with monkeypatch.context() as patch:
        patch.setattr(vr.tempfile, "NamedTemporaryFile", FailingArchive)
        if failure in {"fsync", "publish"}:
            def fail(*args):
                raise OSError(f"archive {failure} failed")
            patch.setattr(vr.os, "fsync" if failure == "fsync" else "replace", fail)
        for _ in range(vr.MAX_RECOVERY_COPIES + 1):
            with pytest.raises(OSError, match="archive"):
                vr.restore_previous_career(tmp_path, 77, previous)
            assert not list(tmp_path.glob("77.recovery-*.json"))
            assert not list(tmp_path.glob("*.tmp"))
            assert (tmp_path / "77.json").read_bytes() == b"damaged original"
            assert (tmp_path / "77.previous.json").read_bytes() == previous

    vr.restore_previous_career(tmp_path, 77, previous)
    assert (tmp_path / "77.json").read_bytes() == previous
    archives = list(tmp_path.glob("77.recovery-*.json"))
    assert len(archives) == 1 and archives[0].read_bytes() == b"damaged original"


@pytest.mark.parametrize("full_archives", [False, True])
def test_missing_primary_requires_recovery_and_validated_previous_can_be_restored(tmp_path, full_archives):
    expected = _broken_career_with_previous(tmp_path)
    (tmp_path / "77.json").unlink()
    if full_archives:
        for index in range(vr.MAX_RECOVERY_COPIES):
            (tmp_path / f"77.recovery-{index}.json").write_bytes(b"retained")
    with pytest.raises(vr.ResumeError, match="missing"):
        vr.load_or_create_save(tmp_path, 77, "Tester")
    restored = vr.restore_previous_career(tmp_path, 77, expected)
    assert restored.turn == 7 and (tmp_path / "77.json").read_bytes() == expected


@pytest.mark.parametrize("kind", ["schema", "galaxy", "journey", "rng", "event", "journey_field",
                                 "encounter_field", "pirate_field", "combat_field", "snapshot_field"])
def test_future_formats_never_offer_or_allow_downgrade_recovery(tmp_path, monkeypatch, kind):
    import json
    import os
    import subprocess

    previous = _broken_career_with_previous(tmp_path)
    future = json.loads(previous)
    if kind in {"schema", "galaxy"}:
        future[f"{kind}_version"] = 99
    elif kind == "journey":
        future["pending_travel"] = {"version": 99}
    elif kind == "rng":
        future["event_rng_state"] = [99, [], None]
    elif kind == "event":
        future["active_event"] = {"economy": "Industrial", "commodity": "metals", "direction": "boom",
                                  "turns_remaining": 2, "description": "News", "future_rule": True}
    else:
        destination = vr.World(vr.SaveData.from_dict(future)).here.connections[0]
        travel = {"version": 1, "origin": 0, "destination": destination, "escort_index": 0, "phase": "primary",
                  "primary": "random", "encounter": {}, "escorts": [], "destroyed": False, "was_discovered": True,
                  "bounty": None}
        future["pending_travel"] = travel
        pirate = {"name": "Raider", "tier": 1, "hp": 35, "hp_max": 35}
        if kind == "journey_field":
            travel["future_rule"] = True
        elif kind == "encounter_field":
            travel["encounter"]["future_rule"] = True
        elif kind == "pirate_field":
            travel["encounter"]["pirate"] = dict(pirate, future_rule=True)
        elif kind == "combat_field":
            travel["encounter"]["combat"] = {"pirate": pirate, "outcome": None, "lines": [], "future_rule": True}
        else:
            mission = vr.Mission(1, "bounty", "Raider", 100, 0, destination, pirate_tier=1).to_dict()
            future["active_missions"] = [mission]
            travel["primary"] = "bounty"
            travel["bounty"] = dict(mission, future_rule=True)
    path = tmp_path / "77.json"
    path.write_text(json.dumps(future), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(vr.UnsupportedSave) as error:
        vr.load_or_create_save(tmp_path, 77, "Tester")
    keys = iter("RB")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        assert vr.screen_save_recovery(vr.Palette(False), tmp_path, 77, error.value).save is None
    assert "[R]estore" not in output.getvalue()
    with pytest.raises(vr.UnsupportedSave):
        vr.restore_previous_career(tmp_path, 77, previous)
    assert path.read_bytes() == original and not list(tmp_path.glob("77.recovery-*"))
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=b"RYB", capture_output=True,
                            env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)), timeout=10)
    assert result.returncode == 0 and not result.stderr
    assert b"[R]estore" not in result.stdout and b"Pilot callsign" not in result.stdout
    assert path.read_bytes() == original and not list(tmp_path.glob("77.recovery-*"))


@pytest.mark.parametrize("problem", ["oversized", "unreadable", "full"])
def test_recovery_hides_restore_and_explains_impossible_preservation(tmp_path, monkeypatch, problem):
    _broken_career_with_previous(tmp_path)
    primary = tmp_path / "77.json"
    read = vr._read_save_bytes
    if problem == "oversized":
        primary.write_bytes(b"x" * (vr.MAX_SAVE_BYTES + 1))
    elif problem == "unreadable":
        def fail_primary(path):
            if path == primary:
                raise PermissionError("cannot read primary")
            return read(path)
        monkeypatch.setattr(vr, "_read_save_bytes", fail_primary)
    else:
        for index in range(vr.MAX_RECOVERY_COPIES):
            (tmp_path / f"77.recovery-{index}.json").write_bytes(b"retained")
    before = {path.name: path.read_bytes() for path in tmp_path.glob("*.json")}
    keys = iter("R" + "N" * 20 + "B")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        result = vr.screen_save_recovery(vr.Palette(False), tmp_path, 77, vr.ResumeError("Career unavailable"))
    rendered = " ".join(output.getvalue().split())
    assert result.save is None and result.exit_code == 0
    assert "[R]estore" not in rendered and "manual recovery" in rendered
    reason = {"oversized": "file size", "unreadable": "cannot be read", "full": "copies are full"}[problem]
    assert reason in rendered
    assert {path.name: path.read_bytes() for path in tmp_path.glob("*.json")} == before


def test_missing_economy_event_fields_remain_recoverable_corruption(tmp_path, monkeypatch):
    import json
    previous = _broken_career_with_previous(tmp_path)
    broken = json.loads(previous)
    broken["active_event"] = {"economy": "Industrial"}
    (tmp_path / "77.json").write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(vr.ResumeError) as error:
        vr.load_or_create_save(tmp_path, 77, "Tester")
    assert not isinstance(error.value, vr.UnsupportedSave)
    keys = iter("B")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_save_recovery(vr.Palette(False), tmp_path, 77, error.value)
    assert "[R]estore" in output.getvalue()


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
def test_recovery_pages_fit_terminal_and_restore_is_on_last_page(tmp_path, monkeypatch, width, height):
    _broken_career_with_previous(tmp_path)
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
    chunks, current = [], io.StringIO()
    keys = iter("N" * 40 + "B")

    def read():
        chunks.append(current.getvalue())
        current.seek(0)
        current.truncate(0)
        return next(keys)

    monkeypatch.setattr(vr, "read_key", read)
    with contextlib.redirect_stdout(current):
        vr.screen_save_recovery(vr.Palette(False), tmp_path, 77, vr.ResumeError("Damaged career"))
    for page in chunks:
        rows = page.splitlines()
        assert len(rows) <= height, (width, height, rows)
        assert all(vr._visible_width(row) <= width for row in rows)
    assert any("[R]estore" in page for page in chunks)


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


def test_opening_assignment_quotes_real_affordable_neighborhoods_without_rng_or_save_changes():
    import copy
    for seed in range(64):
        world = _world_with_seed(seed)
        world.checkpoint()
        before = copy.deepcopy(world.save.to_dict())
        rng_before = world.event_rng.getstate()
        offer = vr.opening_assignment_offer(world)
        assert offer is not None, seed
        assert offer.target_system in world.here.connections and offer.quantity == 3
        assert offer.commodity in vr.ECONOMY_DEMANDS[world.by_id[offer.target_system].economy]
        assert vr.COMMODITIES[offer.commodity]["legal"] and offer.deadline_turn is None
        fuel = vr.fuel_cost_for_jump(world.here, world.by_id[offer.target_system], world.save.ship)
        cost = 3 * vr.price_for(world, 0, offer.commodity)
        assert 2 * fuel <= world.save.ship.fuel and cost <= world.save.pilot.credits
        assert offer.reward - cost - 12 * fuel == 200
        assert world.save.to_dict() == before and world.event_rng.getstate() == rng_before
        assert vr.opening_assignment_offer(world).to_dict() == offer.to_dict()


@pytest.mark.parametrize("reason", ["later", "away", "taken", "broke", "full", "stale", "capacity", "pending"])
def test_opening_assignment_rejection_is_atomic(reason):
    import copy
    world = _world_with_seed(42)
    world.checkpoint()
    offer = vr.opening_assignment_offer(world)
    if reason == "later":
        world.save.turn = 1
    elif reason == "away":
        world.save.current_system = offer.target_system
    elif reason == "taken":
        world.save.flags["opening_assignment_taken"] = True
    elif reason == "broke":
        world.save.pilot.credits = 0
    elif reason == "full":
        other = next(c for c in vr.LEGAL_COMMODITIES if c != offer.commodity)
        world.save.cargo = {other: vr.cargo_capacity(world.save.ship)}
    elif reason == "stale":
        offer.reward += 1
    elif reason == "capacity":
        world.save.active_missions = [vr.Mission(100 + i, "delivery", "Existing", 10, 0, offer.target_system,
                                               commodity="food", quantity=1) for i in range(vr.MAX_ACTIVE_MISSIONS)]
    else:
        world.save.pending_travel = {"version": 1}
    before = copy.deepcopy(world.save.to_dict())
    with pytest.raises(vr.MissionError):
        vr.accept_opening_assignment(world, offer)
    assert world.save.to_dict() == before


def test_opening_assignment_is_tracked_durable_and_pays_only_once_without_deadline(tmp_path):
    world = _world_with_seed(42)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    offer = vr.opening_assignment_offer(world)
    vr.accept_opening_assignment(world, offer)
    world.checkpoint()
    loaded, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    world = vr.World(loaded, checkpoint=lambda current: vr.persist(current, tmp_path, 77))
    assert vr.tracked_mission(world).opening_assignment
    assert world.save.flags["opening_assignment_taken"] and vr.opening_assignment_offer(world) is None
    before = world.save.pilot.credits
    world.save.current_system = offer.target_system
    world.save.turn = 100
    world.save.cargo[offer.commodity] = 4
    assert len(vr.check_mission_completions(world)) == 1
    assert world.save.cargo[offer.commodity] == 1
    assert world.save.pilot.credits == before + offer.reward
    assert world.save.flags["opening_assignment_completed"]
    assert vr.check_mission_completions(world) == []
    world.checkpoint()
    loaded, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert loaded.pilot.missions_completed == 1 and loaded.pilot.credits == before + offer.reward
    assert "First Flight complete" in " ".join(vr.pilot_guide_lines(vr.World(loaded)))


def test_abandoned_opening_assignment_cannot_be_repeated_until_a_new_career():
    world = _world_with_seed(42)
    world.checkpoint()
    offer = vr.opening_assignment_offer(world)
    vr.accept_opening_assignment(world, offer)
    vr.abandon_mission(world, offer.id)
    assert vr.opening_assignment_offer(world) is None
    assert "First Flight is closed" in " ".join(vr.pilot_guide_lines(world))
    world.reset(vr.retire_pilot(world.save))
    assert not world.save.flags.get("opening_assignment_taken")
    assert vr.opening_assignment_offer(world) is not None


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
@pytest.mark.parametrize("screen", ["guide", "offer"])
def test_opening_guide_and_offer_pages_fit_and_browsing_is_read_only(monkeypatch, width, height, screen):
    import copy
    world = _world_with_seed(42)
    world.checkpoint()
    before = copy.deepcopy(world.save.to_dict())
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
    output = io.StringIO()
    pages = []
    guide_count = len(vr._mission_text_pages(vr.pilot_guide_lines(world), overhead=5))

    def choose():
        value = output.getvalue()
        pages.append(value)
        output.seek(0)
        output.truncate(0)
        assert world.save.to_dict() == before
        assert len(pages) <= 100
        return "B" if ("[A]ccept" in value if screen == "offer" else len(pages) == guide_count) else "N"

    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output):
        if screen == "guide":
            vr.screen_pilot_guide(vr.Palette(False), world)
        else:
            vr._screen_opening_offer(vr.Palette(False), world, vr.opening_assignment_offer(world))
    assert world.save.to_dict() == before
    for page in pages:
        rows = page.splitlines()
        assert len(rows) <= height, (width, height, rows)
        assert all(vr._visible_width(row) <= width for row in rows)
    if screen == "offer":
        assert all("[A]ccept" not in page for page in pages[:-1])


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
    market_key = vr.LETTERS[vr.LEGAL_COMMODITIES.index(offer.commodity)].encode()
    with _door_stopped_at(tmp_path, b"M" + market_key + b"B3\n", b"Bought 3x"):
        bought, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert bought.cargo[offer.commodity] == 3 and bought.pilot.credits == 1200 - cargo_cost
        assert bought.cargo_basis == {offer.commodity: [[3, cargo_cost]]}
    jump_key = vr.CHART_CONNECTION_LETTERS[sorted(world.here.connections).index(offer.target_system)].encode()
    with _door_stopped_at(tmp_path, b"C" + jump_key, b"Mission complete: First Flight:"):
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
    assert b"Pilot Guide 1/" in result.stdout
    if b"O" in commands:
        assert b"First Flight 1/" in result.stdout
    assert (tmp_path / "77.json").read_bytes() == before


@pytest.mark.parametrize("invalid_flag", [None, 0, 1, "yes"])
def test_invalid_opening_assignment_flag_is_rejected_before_checkpoint_write(tmp_path, invalid_flag):
    world = _world_with_seed(42)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    before = (tmp_path / "77.json").read_bytes()
    offer = vr.opening_assignment_offer(world)
    offer.opening_assignment = invalid_flag
    world.save.active_missions.append(offer)
    with pytest.raises(vr.SaveError):
        vr.write_save(tmp_path, 77, world.save)
    assert (tmp_path / "77.json").read_bytes() == before


def test_opening_quote_budgets_crew_and_return_fuel_before_acceptance():
    world = _world_with_seed(42)
    world.save.ship.has_gunner = True
    world.save.ship.has_engineer = True
    world.save.ship.fuel = 0
    offer = vr.opening_assignment_offer(world)
    assert offer is not None
    fuel = vr.fuel_cost_for_jump(world.here, world.by_id[offer.target_system], world.save.ship)
    price = vr.price_for(world, 0, offer.commodity)
    assert offer.reward == 3 * price + 12 * fuel + 2 * (15 + 2) + 200
    # Enough for three cheapest units alone is insufficient for the reserve/wages.
    world.save.pilot.credits = 3 * min(vr.price_for(world, 0, c) for c in vr.LEGAL_COMMODITIES)
    assert vr.opening_assignment_offer(world) is None


def test_ordinary_legacy_missions_keep_their_serialized_shape():
    mission = vr.Mission(1, "delivery", "Old job", 100, 0, 1, commodity="food", quantity=3)
    assert "opening_assignment" not in mission.to_dict()
    loaded = vr.Mission.from_dict(mission.to_dict())
    assert loaded.opening_assignment is False and loaded.to_dict() == mission.to_dict()


@pytest.mark.parametrize("corruption", ["missing_acceptance", "duplicate", "completed_but_active"])
def test_contradictory_opening_assignment_progress_cannot_be_saved(tmp_path, corruption):
    import dataclasses
    world = _world_with_seed(42)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    offer = vr.opening_assignment_offer(world)
    vr.accept_opening_assignment(world, offer)
    world.checkpoint()
    before = (tmp_path / "77.json").read_bytes()
    if corruption == "missing_acceptance":
        del world.save.flags["opening_assignment_taken"]
    elif corruption == "duplicate":
        world.save.active_missions.append(dataclasses.replace(offer, id=999))
    else:
        world.save.flags["opening_assignment_completed"] = True
    with pytest.raises(vr.SaveError):
        vr.write_save(tmp_path, 77, world.save)
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


def test_ordinary_mission_acceptance_rejects_an_opening_tag_without_mutating_the_board():
    import copy
    world = _world_with_seed(42)
    world.checkpoint()
    offer = vr.opening_assignment_offer(world)
    world.save.mission_boards[0]["offers"][0] = offer.to_dict()
    before = copy.deepcopy(world.save.to_dict())
    with pytest.raises(vr.MissionError, match="Pilot Guide"):
        vr.accept_mission(world, offer)
    assert world.save.to_dict() == before


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
    world.save.cargo[offer.commodity] = offer.quantity
    world.save.current_system = offer.target_system
    vr.check_mission_completions(world)
    assert world.save.flags["opening_assignment_completed"]
    assert any(m.description == "Earlier order" for m in world.save.active_missions)
