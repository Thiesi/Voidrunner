"""The daily dispatch: one order a real day, the same for every pilot (review).

What these pin: the order comes from the date alone and comes round fairly; it
exists only where a calendar is bound, so no other test depends on the day it
runs; doing what it asks flies it once, in the commit that saves the action;
the streak, the reward and the record follow the calendar; nothing new is saved
for it; and the board's score records carry it for the Dispatch page and the
Hall of Fame.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json

import pytest

from .support import _door_stopped_at, _finale_world, _set_cargo, _world_with_seed, plain, vr

DAY = datetime.date(2026, 11, 22).toordinal()  # a Sunday: the start of a dispatch week


def _order_day(kind: str, start: int = DAY, **match) -> int:
    """The first day from `start` whose order is `kind` (and matches `match`)."""
    for day in range(start, start + 400):
        order = vr.daily_dispatch(day)
        if order["kind"] == kind and all(order.get(key) == value for key, value in match.items()):
            return day
    raise AssertionError(f"no {kind} order in 400 days")


def _on(world, day: int):
    """Bind `world` to a calendar that reads `day`; returns a setter for later days."""
    today = [day]
    world.dispatch_clock = lambda: today[0]
    return lambda new: today.__setitem__(0, new)


def _farm_world(day: int):
    """A pilot docked at an Agricultural station with Machinery to sell."""
    world = _world_with_seed(42)
    farm = next(system for system in world.galaxy if system.economy == "Agricultural")
    world.save.current_system = farm.id
    farm.discovered = True
    _set_cargo(world, {"machinery": 24})
    return world, _on(world, day)


# --- the order ----------------------------------------------------------------

def test_an_order_comes_from_the_day_alone_and_a_week_asks_for_everything():
    assert vr.daily_dispatch(DAY) == vr.daily_dispatch(DAY)
    for week in range(DAY // 7, DAY // 7 + 60):
        orders = [vr.daily_dispatch(week * 7 + slot) for slot in range(7)]
        assert sorted(order["kind"] for order in orders) == sorted(vr._DISPATCH_WEEK)
        hauls = [(order["commodity"], order["economy"]) for order in orders if order["kind"] == "haul"]
        assert len(set(hauls)) == 3
        for commodity, economy in hauls:
            assert vr.COMMODITIES[commodity]["legal"] and economy != "Haven"
            assert commodity in vr.ECONOMY_DEMANDS[economy]


def test_no_order_is_yesterdays_over_again():
    texts = [vr.dispatch_text(vr.daily_dispatch(day)) for day in range(DAY, DAY + 800)]
    assert all(today != yesterday for yesterday, today in zip(texts, texts[1:]))


def test_an_order_reads_as_a_sentence_and_its_short_form_fits_the_deck():
    haul = vr.daily_dispatch(_order_day("haul", commodity="electronics", economy="Agricultural"))
    assert vr.dispatch_text(haul) == "Sell 12+ Electronics in one sale at an Agricultural station"
    assert vr.dispatch_text(haul, short=True) == "Sell 12+ Electronics at an Agricultural station"
    assert vr.dispatch_text(vr.daily_dispatch(_order_day("haul", economy="Mining"))).endswith("at a Mining station")
    assert vr.dispatch_text({"kind": "sale"}) == "Make one sale worth 2,000cr or more"


@pytest.mark.parametrize("value,expected", [("", "live"), ("0", None), ("off", None), ("No", None),
                                            ("2026-11-22", DAY), ("22.11.2026", None), ("tomorrow", None)])
def test_the_sysops_switch_turns_it_off_or_pins_the_day(monkeypatch, value, expected):
    monkeypatch.setenv("DOOR_DISPATCH", value)
    if expected == "live":
        assert vr.dispatch_day() == datetime.date.today().toordinal() or vr._NODE_ZONE is not None
    else:
        assert vr.dispatch_day() == expected


# --- no calendar, no dispatch --------------------------------------------------

def test_a_world_without_a_calendar_has_no_dispatch():
    world = _world_with_seed(42)
    assert world.dispatch_clock is None and vr.dispatch_order(world) is None
    assert vr.dispatch_deck_row(world) is None
    assert "J" not in dict(vr.deck_service_entries(world))
    farm, _ = _farm_world(DAY)
    farm.dispatch_clock = None
    before = farm.save.pilot.credits
    paid = vr.sell_price_for(farm, farm.here.id, "machinery") * 24
    vr.trade_cargo(farm, "machinery", 24, buying=False)
    farm.commit()
    assert farm.save.pilot.credits == before + paid and not farm.save.accolades.get("counters", {}).get("dispatches")


# --- flying it -----------------------------------------------------------------

def test_the_sale_an_order_asks_for_flies_it_once_in_the_commit_that_saves_it():
    day = _order_day("haul", commodity="machinery", economy="Agricultural")
    world, _ = _farm_world(day)
    saved = []
    world._checkpoint = lambda current: saved.append(json.loads(json.dumps(current.save.to_dict())))
    before = world.save.pilot.credits
    paid = vr.sell_price_for(world, world.here.id, "machinery") * 12
    vr.trade_cargo(world, "machinery", 12, buying=False)
    assert not vr.dispatch_flown(world)  # not until the commit that saves the sale
    world.commit()
    reward = vr.dispatch_reward(world.save, 1)
    assert vr.dispatch_flown(world) and world.save.pilot.credits == before + paid + reward
    # The reward and the record are in the document that recorded the sale.
    assert saved[-1]["pilot"]["credits"] == before + paid + reward
    assert saved[-1]["accolades"]["counters"]["dispatch_day"] == day
    assert any("Daily dispatch flown" in line for line in world.hop_report)
    assert vr.accolade_unlocked(world.save, "dispatch_1")
    # A second sale that also qualifies pays only what the station pays.
    credits = world.save.pilot.credits
    again = vr.sell_price_for(world, world.here.id, "machinery") * 12
    vr.trade_cargo(world, "machinery", 12, buying=False)
    world.commit()
    assert world.save.pilot.credits == credits + again
    assert vr.dispatch_record(world.save)["total"] == 1


@pytest.mark.parametrize("what", ["too few", "wrong goods", "wrong economy"])
def test_a_sale_that_is_not_the_order_does_not_fly_it(what):
    day = _order_day("haul", commodity="machinery", economy="Agricultural")
    world, _ = _farm_world(day)
    if what == "wrong economy":
        world.save.current_system = next(s.id for s in world.galaxy if s.economy == "Mining")
    if what == "wrong goods":
        _set_cargo(world, {"electronics": 24})
    vr.trade_cargo(world, "electronics" if what == "wrong goods" else "machinery", 11 if what == "too few" else 12,
                   buying=False)
    world.commit()
    assert not vr.dispatch_flown(world) and vr.dispatch_record(world.save)["total"] == 0


def test_one_big_sale_flies_a_sale_order_and_two_small_ones_do_not():
    day = _order_day("sale")
    world, _ = _farm_world(day)
    unit = vr.sell_price_for(world, world.here.id, "machinery")
    enough = -(-vr.DISPATCH_SALE_VALUE // unit)
    assert enough <= 24
    vr.trade_cargo(world, "machinery", enough - 1, buying=False)
    world.commit()
    vr.trade_cargo(world, "machinery", 1, buying=False)
    world.commit()
    assert not vr.dispatch_flown(world)
    _set_cargo(world, {"machinery": 24})
    vr.trade_cargo(world, "machinery", 24, buying=False)
    world.commit()
    assert vr.dispatch_flown(world)


@pytest.mark.parametrize("kind", ["kill", "contract", "chart"])
def test_a_fight_a_contract_and_a_chart_fly_their_orders(kind):
    world = _world_with_seed(42)
    _on(world, _order_day(kind))
    world.commit()
    assert not vr.dispatch_flown(world)  # what a career did before today does not count
    if kind == "kill":
        world.save.pilot.kills += 1
    elif kind == "contract":
        world.save.pilot.missions_completed += 1
    else:
        next(system for system in world.galaxy if not system.discovered).discovered = True
    world.commit()
    assert vr.dispatch_flown(world)


def test_what_a_career_did_before_this_session_does_not_fly_todays_order():
    world = _world_with_seed(42)
    world.save.pilot.kills = 9
    reloaded = vr.World(vr.SaveData.from_dict(json.loads(json.dumps(world.save.to_dict()))))
    _on(reloaded, _order_day("kill"))
    reloaded.commit()
    assert not vr.dispatch_flown(reloaded)


def test_a_pilot_with_nothing_left_to_chart_is_asked_for_a_contract():
    world = _world_with_seed(42)
    _on(world, _order_day("chart"))
    assert vr.dispatch_order(world)["kind"] == "chart"
    for system in world.galaxy:
        system.discovered = True
    order = vr.dispatch_order(world)
    assert order["kind"] == "contract" and vr.dispatch_text(order) == "Complete a contract"


# --- the streak and the reward --------------------------------------------------

def _fly(world) -> None:
    vr.fly_dispatch(world, vr.dispatch_order(world))


def test_a_streak_is_consecutive_days_and_a_missed_day_starts_it_again():
    world = _world_with_seed(42)
    set_day = _on(world, DAY)
    _fly(world)
    assert vr.dispatch_record(world.save)["streak"] == 1
    set_day(DAY + 1)
    assert vr.dispatch_streak(vr.dispatch_record(world.save), DAY + 1) == 1  # still alive today
    _fly(world)
    set_day(DAY + 2)
    _fly(world)
    record = vr.dispatch_record(world.save)
    assert (record["streak"], record["best"], record["total"], record["day"]) == (3, 3, 3, DAY + 2)
    set_day(DAY + 4)  # a day missed
    assert vr.dispatch_streak(vr.dispatch_record(world.save), DAY + 4) == 0
    _fly(world)
    record = vr.dispatch_record(world.save)
    assert (record["streak"], record["best"], record["total"]) == (1, 3, 4)
    vr.unlock_accolades(world.save)  # what a commit does
    assert vr.accolade_unlocked(world.save, "streak_3") and not vr.accolade_unlocked(world.save, "streak_7")


def test_the_reward_grows_with_rank_and_with_the_streak_up_to_half_again():
    world = _world_with_seed(42)
    base = vr.dispatch_reward(world.save, 1)
    assert vr.DISPATCH_REWARD == (1_000, 3_000, 5_000, 6_000, 7_000) and len(vr.DISPATCH_REWARD) == len(vr.RANKS)
    assert base == vr.DISPATCH_REWARD[0]
    assert [vr.dispatch_reward(world.save, streak) for streak in (2, 6, 7, 40)] == [1_100, 1_500, 1_500, 1_500]
    world.save.pilot.credits = 20_000
    vr.check_rank_up(world)
    assert vr.dispatch_reward(world.save, 1) == vr.DISPATCH_REWARD[2]
    assert vr.dispatch_reward(world.save, 6) == 7_500  # most of a jump on top, where it was a few hundred credits


# --- nothing new is saved -------------------------------------------------------

def test_the_record_is_five_integers_among_the_accolade_counters():
    world = _world_with_seed(42)
    keys = set(world.save.to_dict())
    _on(world, DAY)
    _fly(world)
    document = json.loads(json.dumps(world.save.to_dict()))
    assert set(document) == keys | {"accolades"}
    counters = document["accolades"]["counters"]
    assert set(counters) == {"dispatches", "dispatch_best", *vr.DISPATCH_FIELDS}
    assert all(type(value) is int for value in counters.values())
    loaded = vr.SaveData.from_dict(document)
    assert vr.dispatch_record(loaded) == vr.dispatch_record(world.save)


def test_the_record_rides_through_retirement():
    world = _finale_world("legend")
    set_day = _on(world, DAY)
    _fly(world)
    set_day(DAY + 1)
    _fly(world)
    record = vr.dispatch_record(world.save)
    successor = vr.finish_career(world.save, "legend")
    assert vr.dispatch_record(successor) == record


# --- the board ------------------------------------------------------------------

def test_a_score_record_carries_the_dispatch_and_a_malformed_one_is_left_out(tmp_path):
    world = _world_with_seed(42)
    vr.update_hall_of_fame(tmp_path, 77, world.save)
    assert "dispatch" not in json.loads((tmp_path / "scores" / "77.json").read_text(encoding="utf-8"))
    _on(world, DAY)
    _fly(world)
    vr.update_hall_of_fame(tmp_path, 77, world.save)
    path = tmp_path / "scores" / "77.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["dispatch"] == vr.dispatch_record(world.save)
    assert vr._load_score_records(tmp_path)[0]["dispatch"]["day"] == DAY
    stamp = path.stat().st_mtime_ns
    vr.update_hall_of_fame(tmp_path, 77, world.save)  # an unchanged record is not rewritten
    assert path.stat().st_mtime_ns == stamp
    for broken in ({"day": -1}, "yes", {**stored["dispatch"], "extra": 1}, {**stored["dispatch"], "streak": "3"}):
        path.write_text(json.dumps({**stored, "dispatch": broken}), encoding="utf-8")
        entry = vr._load_score_records(tmp_path)[0]
        assert "dispatch" not in entry and entry["handle"] == stored["handle"]


def _entry(user_id: int, handle: str, **dispatch) -> dict:
    record = {"day": DAY, "streak": 1, "best": 1, "total": 1, "at": 1_000}
    record.update(dispatch)
    return {"user_id": user_id, "handle": handle, "best_credits": 0, "retirements": 0, "kills": 0,
            "missions_completed": 0, "rank": "Rookie Hauler", "dispatch": record}


def test_the_hall_ranks_the_longest_runs_and_the_page_lists_who_flew_today(terminal):
    terminal(80, 24)
    entries = [_entry(1, "Ada", best=2, total=9, at=500), _entry(2, "Nib", best=7, streak=7, total=7, at=300),
               _entry(3, "Vex", best=7, streak=1, total=12, day=DAY - 3, at=10),
               {key: value for key, value in _entry(4, "Idle").items() if key != "dispatch"}]
    ranked = vr.achievement_ranking(entries, "dispatch")
    assert [entry["handle"] for entry in ranked] == ["Vex", "Nib", "Ada"]
    rows = [plain(row) for row in vr.dispatch_ranking_lines(ranked, 2, DAY)]
    vex, nib = (next(row for row in rows if name in row) for name in ("Vex", "Nib"))
    assert "7 days" in vex and "none" in vex and "12" in vex      # a run that has ended
    assert nib.count("7 days") == 2 and "[YOU]" in nib            # a run still alive
    world = _world_with_seed(42)
    _on(world, DAY)
    page = [plain(row) for row in vr.dispatch_lines(world, entries, 1)]
    today = page[page.index(next(row for row in page if "FLOWN TODAY" in row)) + 1:]
    listed = [name for row in today for name in ("Ada", "Nib", "Vex") if name in row]
    assert listed == ["Nib", "Ada"]  # in the order they flew it; Vex flew three days ago
    assert any(row.startswith("STATUS") and "open" in row for row in page)
    empty = [plain(row) for row in vr.dispatch_lines(world, [], 1)]
    assert "Nobody on this board has flown today's order yet." in empty


# --- on screen ------------------------------------------------------------------

@pytest.mark.parametrize("width,height", [(80, 24), (40, 12)])
def test_the_deck_says_todays_order_in_one_row_and_the_page_fits(terminal, width, height):
    terminal(width, height)
    world = _world_with_seed(42)
    set_day = _on(world, _order_day("haul", commodity="electronics", economy="Agricultural"))
    row = plain(vr.dispatch_deck_row(world))
    assert "DISPATCH" in row and "Sell 12+ Electronics at an Agricultural station" in row and "+1,000cr" in row
    assert ("J", "Dispatch") in vr.deck_service_entries(world)
    deck = vr.station_deck_lines(world)
    pages = vr._service_pages(deck, "Command Deck: 1,200cr", "[X] Expand [Q] Exit: ")
    if width == 80:
        assert vr._visible_width(vr.dispatch_deck_row(world)) <= vr._page_content_width()
        # One row of window for the order: a first deck is one page and well
        # inside the band, and the full deck of day three is fifteen rows.
        assert len(pages) == 1 and len(pages[0]) + 3 <= round(24 * 0.58)
        world.save.turn = 3
        full = vr._service_pages(vr.station_deck_lines(world), "Command Deck: 1,200cr", "[X] Expand [Q] Exit: ")
        # Fourteen rows since the three contacts became one entry: the console's whole part of the screen.
        assert len(full) == 1 and len(full[0]) + 3 == round(24 * 0.58)
        world.save.turn = 0
        assert vr.menu_columns([f"[{key}] {label}" for key, label in vr.deck_service_entries(world)]) == 3
    for page in vr._service_pages(vr.dispatch_lines(world, [], 1), "Daily Dispatch", "[<>] Page [B] Back: "):
        assert len(page) + 3 <= height
        assert all(vr._visible_width(line) <= width for row in page for line in plain(row).split("\n"))
    _fly(world)
    assert "DISPATCH flown" in plain(vr.dispatch_deck_row(world)) and "tomorrow" in plain(vr.dispatch_deck_row(world))
    set_day(world.dispatch_clock() + 1)
    assert "DISPATCH flown" not in plain(vr.dispatch_deck_row(world))  # a new day, a new order


def test_the_real_door_shows_the_order_and_opens_its_page(tmp_path, monkeypatch):
    monkeypatch.setenv("DOOR_DISPATCH", "2026-11-22")
    world = _world_with_seed(42)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    with _door_stopped_at(tmp_path, b"J", b"YOUR RECORD") as output:
        text = vr._ANSI_RE.sub("", bytes(output).decode("utf-8", "replace"))
        assert "DISPATCH" in text and "[J] Dispatch" in text and "TODAY'S ORDER" in text
        assert vr.dispatch_text(vr.daily_dispatch(DAY)) in " ".join(text.split())


def test_the_door_a_test_launches_has_no_dispatch(tmp_path):
    """`conftest` switches it off, so no other test depends on the date."""
    world = _world_with_seed(42)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    with _door_stopped_at(tmp_path, b"S", b"Pilot Record") as output:
        text = vr._ANSI_RE.sub("", bytes(output).decode("utf-8", "replace"))
        assert "DISPATCH" not in text and "[J] Dispatch" not in text


def test_the_decks_row_fits_at_the_top_rank_with_the_longest_streak(terminal):
    """The pay is five figures now: the widest order still leaves the row inside the deck."""
    terminal(80, 24)
    world = _world_with_seed(42)
    world.save.pilot.credits = 400_000
    vr.check_rank_up(world)
    set_day = _on(world, DAY)
    counters = vr._accolade_record(world.save)["counters"]
    assert vr.dispatch_reward(world.save, 31) == 10_500
    for day in range(DAY, DAY + 120):
        set_day(day)
        counters.update(dispatch_day=day - 1, dispatch_streak=30)
        row = plain(vr.dispatch_deck_row(world))
        assert "+10,500cr" in row and vr._visible_width(row) <= vr._page_content_width()
