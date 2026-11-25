"""Laying up: the last page of a call (review).

With the day's jumps flown the door had an alert and nothing more to say.
These pin the lay-up page: what it says of tomorrow (the jumps, the ones a
full bank is about to lose, the order, the streak), what it says is worth
settling tonight, that the keys it names are answered, where it is offered,
and what a caller is told on the way out.
"""

from __future__ import annotations

import contextlib
import datetime
import io

import pytest

from .support import _VOIDRUNNER_PATH, _set_cargo, _world_with_seed, plain, plain_bytes, vr

DAY = datetime.date(2026, 11, 22).toordinal()


def _docked(left=0, day=DAY, dispatch=True):
    """A caller on a board with a calendar, with `left` jumps of today's."""
    world = _world_with_seed(42)
    today = [day]
    world.flight_clock = lambda: today[0]
    world.flight_per_day = vr.FLIGHT_PER_DAY
    world.save.flight = {"day": day, "left": left}
    if dispatch:
        world.dispatch_clock = lambda: today[0]
    return world, today


def _rows(rows):
    return {label: (" ".join(plain(value).split()), tone) for label, value, tone in rows}


def _freeze(monkeypatch, hour=18, minute=47, second=30):
    import datetime as real

    class Frozen(real.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 24, hour, minute, second)

    monkeypatch.setattr(vr, "datetime", Frozen)


def test_tomorrow_is_what_is_left_and_a_day_more_and_a_full_bank_loses_the_rest():
    world, _today = _docked(left=0)
    assert vr.flight_tomorrow(world) == (12, 0)
    world.save.flight["left"] = 7
    assert vr.flight_tomorrow(world) == (19, 0)
    world.save.flight["left"] = 12
    assert vr.flight_tomorrow(world) == (24, 0)
    world.save.flight["left"] = 20
    assert vr.flight_tomorrow(world) == (24, 8)   # eight of the twenty will not be there tomorrow
    assert vr.flight_tomorrow(_world_with_seed(42)) is None


def test_the_page_says_what_tomorrow_brings(monkeypatch):
    _freeze(monkeypatch)
    world, today = _docked(left=0)
    rows = _rows(vr.lay_up_tomorrow_rows(world))
    assert rows["JUMPS"] == ("12 to fly after midnight, in 5h 12m", None) and "BANK" not in rows
    tomorrow = vr.daily_dispatch(DAY + 1)
    assert rows["ORDER"][0] == f"{vr.dispatch_text(tomorrow, short=True)} +1,000cr"
    assert rows["STREAK"] == ("none running; today's is still open [J] dispatch", "info")
    # Today's flown: tomorrow's is the second day of a run, and pays as one.
    vr.fly_dispatch(world, vr.dispatch_order(world))
    rows = _rows(vr.lay_up_tomorrow_rows(world))
    assert rows["STREAK"] == ("1 day running; tomorrow's makes it 2", "good")
    assert rows["ORDER"][0].endswith(f"+{vr.dispatch_reward(world.save, 2):,}cr") and vr.dispatch_reward(world.save, 2) == 1_100
    # The next day, not yet flown: the run is alive and about to end.
    today[0] += 1
    rows = _rows(vr.lay_up_tomorrow_rows(world))
    assert rows["STREAK"] == ("1 day running; today's is still open [J] dispatch", "bad")
    assert rows["ORDER"][0].endswith("+1,000cr")  # tomorrow's would start a new run
    # A bank about to overflow is said, in the tone of a loss.
    world.save.flight = {"day": today[0], "left": 20}
    rows = _rows(vr.lay_up_tomorrow_rows(world))
    assert rows["JUMPS"][0].startswith("24 to fly after midnight")
    assert rows["BANK"] == ("full: 8 jumps of today's will not carry over", "bad")


def test_tomorrows_order_is_given_by_the_same_rule_as_todays():
    world, today = _docked()
    for day in range(DAY, DAY + 14):
        today[0] = day
        assert vr.dispatch_order(world) == vr.dispatch_order_on(world, day)
    chart_day = next(day for day in range(DAY, DAY + 14) if vr.daily_dispatch(day)["kind"] == "chart")
    for system in world.galaxy:
        system.discovered = True
    assert vr.dispatch_order_on(world, chart_day)["kind"] == "contract"  # nothing left to chart


def test_the_page_says_what_to_settle_and_names_the_key_that_settles_it():
    world, _today = _docked()
    ship = world.save.ship
    ship.fuel, ship.hull_hp = vr.fuel_capacity(ship), vr.hull_hp_max(ship)
    rows = _rows(vr.lay_up_ready_rows(world))
    assert rows["FUEL"] == (f"{ship.fuel}/{ship.fuel}", "good") and rows["HULL"][1] == "good"
    assert rows["HOLD"] == (f"0/{vr.cargo_capacity(ship)}: load tomorrow's first run [M] market", "info")
    assert rows["CONTRACTS"] == ("none accepted [B] board", "info")
    assert rows["ROUTE"] == ("nothing tracked: plan one [C] chart", "info")
    # A ship that needs the yard says so, and a critical hull says it loudest.
    ship.fuel, ship.hull_hp = 3, 5
    rows = _rows(vr.lay_up_ready_rows(world))
    assert rows["FUEL"] == (f"3/{vr.fuel_capacity(ship)} [Y] refuel", "info")
    assert rows["HULL"] == (f"5/{vr.hull_hp_max(ship)} [Y] repair", "bad")
    ship.hull_hp = vr.hull_hp_max(ship) - 1
    assert _rows(vr.lay_up_ready_rows(world))["HULL"][1] == "info"


def test_a_loaded_hold_says_where_it_sold_best_and_a_tracked_contract_what_the_route_burns():
    world, _today = _docked()
    ship = world.save.ship
    _set_cargo(world, {"machinery": 10})
    assert _rows(vr.lay_up_ready_rows(world))["HOLD"] == (f"10/{vr.cargo_capacity(ship)} loaded", "good")
    near = sorted(world.here.connections)[0]
    world.by_id[near].discovered = True
    here_sell = vr.sell_price_for(world, world.here.id, "machinery")
    world.save.market_memory[near] = {"machinery": {"buy": here_sell + 60, "sell": here_sell + 50, "day": 0}}
    hold = _rows(vr.lay_up_ready_rows(world))["HOLD"]
    assert hold == (f"10/{vr.cargo_capacity(ship)}; Machinery sold for {here_sell + 50}cr at "
                    f"{world.by_id[near].name}, 1 jump", "good")
    # A tracked contract: the jumps and the fuel to its end, and whether the tank has it.
    mission = vr.Mission(id=7, kind="scan", description="Survey", reward=900, origin_system=0, target_system=near,
                         deadline_turn=9)
    world.save.active_missions.append(mission)
    world.save.tracked_mission_id = 7
    world.by_id[near].discovered = False
    burn = vr.fuel_cost_for_jump(world.here, world.by_id[near], ship)
    ship.fuel = burn
    rows = _rows(vr.lay_up_ready_rows(world))
    assert rows["CONTRACTS"] == ("1 accepted; next due day 9, today is day 0", None)
    assert rows["ROUTE"] == (f"#7 to {world.by_id[near].name}: 1 jump, {burn} fuel", "good")
    ship.fuel = burn - 1
    assert _rows(vr.lay_up_ready_rows(world))["ROUTE"] == (
        f"#7 to {world.by_id[near].name}: 1 jump, {burn} fuel, {burn - 1} aboard", "bad")


@pytest.mark.parametrize("width,height", [(80, 24), (64, 24), (40, 24)])
def test_the_page_is_one_page_and_fits_its_terminal(terminal, width, height):
    terminal(width, height)
    world, _today = _docked(left=20)   # the longest page: a bank about to overflow, a streak about to end
    vr.fly_dispatch(world, vr.dispatch_order(world))
    world.flight_clock = world.dispatch_clock = lambda: DAY + 1
    world.save.ship.fuel, world.save.ship.hull_hp = 3, 5
    lines = vr.lay_up_lines(world)
    assert sum("TOMORROW" in plain(line) or "BEFORE YOU GO" in plain(line) for line in lines) == 2
    assert all(plain(line)[:1] in vr.alert_glyphs() for line in lines[1:-1] if not line.startswith(vr.SECTION_MARK))
    pages = vr._service_pages(lines, "Lay-up", "[M] Market [Y] Yard [B] Board [C] Chart [J] Dispatch [Q] Back: ")
    if width >= 64:
        assert len(pages) == 1 and len(pages[0]) + 3 <= height
    for page in pages:
        assert all(vr._visible_width(line) <= width for row in page for line in plain(row).split("\n"))
    assert plain(lines[-1]) == "None of this costs a jump; the ship is safe in dock."


def test_the_page_answers_the_keys_it_names_and_the_deck_acts_on_them(monkeypatch):
    world, _today = _docked()
    for pressed, expected in (("Y", "Y"), ("M", "M"), ("B", "B"), ("C", "C"), ("J", "J"), ("Q", None)):
        keys = iter(["x", "S", pressed])   # keys the page does not name are not handed back
        monkeypatch.setattr(vr, "read_key", lambda: next(keys))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            assert vr.screen_lay_up(vr.Palette(False), world) == expected
        assert "Lay-up" in plain(output.getvalue())
    assert set(vr.LAY_UP_KEYS) == set("MYBCJ")


def test_it_is_offered_where_there_is_a_tomorrow_and_the_alert_names_its_key():
    assert not vr.lay_up_open(_world_with_seed(42))          # a domain-only world has no calendar
    world, _today = _docked(left=0, dispatch=False)
    assert vr.lay_up_open(world)
    alerts = " ".join(" ".join(plain(row).split()) for row in vr.deck_alert_rows(world))
    assert "No jumps left today 12 more at midnight, in " in alerts and "[Z] lay up" in alerts
    assert vr.flight_blocker(world).endswith("Trading and planning cost nothing; [Z] on the deck lays up.")
    assert any("[Z] on the deck lays up" in line for line in vr.guide_reference_lines(world))
    # Without a dispatch the page speaks of the jumps alone.
    assert set(_rows(vr.lay_up_tomorrow_rows(world))) == {"JUMPS"}
    # And with a dispatch but no ration, of the order alone.
    free = _world_with_seed(42)
    free.dispatch_clock = lambda: DAY
    assert vr.lay_up_open(free) and set(_rows(vr.lay_up_tomorrow_rows(free))) == {"ORDER", "STREAK"}


def test_the_dispatch_page_says_what_tomorrow_asks():
    world, _today = _docked()
    page = [" ".join(plain(row).split()) for row in vr.dispatch_lines(world, [], 1)]
    assert f"TOMORROW {vr.dispatch_text(vr.daily_dispatch(DAY + 1), short=True)}" in page


def test_a_caller_leaving_is_told_what_is_left_and_what_tomorrow_holds(monkeypatch):
    _freeze(monkeypatch)
    world, _today = _docked(left=3)
    assert vr.flight_farewell(world) == "3 jumps left today; 15 to fly after midnight, in 5h 12m."
    world.save.flight["left"] = 1
    assert vr.flight_farewell(world) == "1 jump left today; 13 to fly after midnight, in 5h 12m."
    world.save.flight["left"] = 0
    assert vr.flight_farewell(world) == "No jumps left today; 12 to fly after midnight, in 5h 12m."
    assert vr.flight_farewell(_world_with_seed(42)) is None


def test_the_real_door_lays_up_from_the_deck_and_says_goodbye_with_the_jumps(tmp_path):
    import json
    import os
    import subprocess
    import sys

    world = _world_with_seed(42)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester", "terminal_width": 80, "terminal_height": 24}),
                    encoding="utf-8")
    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info),
               DOOR_JUMPS="5", DOOR_JUMPS_DAY="2026-11-22")
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=b"ZQQ", capture_output=True, env=env, timeout=60)
    shown = b" ".join(plain_bytes(result.stdout).split()).decode("utf-8", "replace")
    assert result.returncode == 0 and not result.stderr
    assert "Lay-up" in shown and "JUMPS 10 to fly after midnight, in 12h 0m" in shown and "BEFORE YOU GO" in shown
    assert "Fly safe, Tester. 5 jumps left today; 10 to fly after midnight, in 12h 0m." in shown
    # The page hands a deck key back: Y from it is the yard.
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=b"ZYBQ", capture_output=True, env=env, timeout=60)
    assert result.returncode == 0 and b"Engineering Yard" in plain_bytes(result.stdout)
    # A board that rations nothing and has no dispatch has no lay-up, and says nothing of jumps on the way out.
    env.update(DOOR_JUMPS="0", DOOR_DISPATCH="0")
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=b"ZQ", capture_output=True, env=env, timeout=60)
    shown = plain_bytes(result.stdout)
    assert result.returncode == 0 and b"Lay-up" not in shown and b"after midnight" not in shown and b"Fly safe" in shown
