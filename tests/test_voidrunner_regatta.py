"""The Regatta: one galaxy a month for every pilot (review).

A caller flies it in a second career beside their own. These pin the month and
its galaxy, where that career is kept and that the caller's own career is not
touched, the one allowance of jumps two careers share, what a career is worth
and what the table ranks, that a Regatta career ends with its month, what a
save and a score record keep, and the real door doing all of it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys

import pytest

from .support import _VOIDRUNNER_PATH, _set_cargo, _world_with_seed, plain, plain_bytes, vr

MONTH = "2026-11"


@pytest.fixture
def november(monkeypatch):
    monkeypatch.setenv("DOOR_REGATTA", MONTH)
    return MONTH


def _own(tmp_path, user_id=77):
    """A caller's own career, saved in `tmp_path`, on a board that rations jumps."""
    world = _world_with_seed(42)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, user_id)
    world.flight_clock, world.flight_per_day = (lambda: 1000), vr.FLIGHT_PER_DAY
    world.checkpoint()
    return world


# --- the month and its galaxy -------------------------------------------------------

def test_the_month_is_the_calendars_a_pinned_one_or_none(monkeypatch):
    monkeypatch.delenv("DOOR_REGATTA", raising=False)
    now = vr._node_now()
    assert vr.regatta_month() == f"{now.year:04d}-{now.month:02d}"
    for value, month in (("2026-11", "2026-11"), ("0", None), ("off", None), ("2026-13", None), ("november", None)):
        monkeypatch.setenv("DOOR_REGATTA", value)
        assert vr.regatta_month() == month
    assert vr.regatta_name("2026-11") == "November 2026" and vr.regatta_month_name("2027-01") == "January"
    assert vr.previous_month("2026-11") == "2026-10" and vr.previous_month("2027-01") == "2026-12"
    assert all(vr.regatta_month_valid(m) for m in ("2026-01", "2026-12")) and not any(
        vr.regatta_month_valid(m) for m in ("2026-00", "2026-13", "26-11", "2026-1", 202611, None))


def test_the_galaxy_follows_from_the_month_alone_and_is_the_same_for_everyone(tmp_path):
    seed = vr.regatta_seed(MONTH)
    assert seed == vr.regatta_seed(MONTH) and 1 <= seed < 2**31 and seed != vr.regatta_seed("2026-12")
    one = vr.World(vr.new_regatta_career(_world_with_seed(1).save, MONTH))
    two = vr.World(vr.new_regatta_career(_world_with_seed(2).save, MONTH))
    assert [(s.name, s.x, s.y, s.economy, s.danger) for s in one.galaxy] == [
        (s.name, s.x, s.y, s.economy, s.danger) for s in two.galaxy]
    assert one.traits == two.traits and one.landmark == two.landmark
    assert one.save.regatta == MONTH and one.save.pilot.credits == vr.STARTING_CREDITS and one.save.ship.hull_class == "Shuttle"
    assert vr.regatta_dir(tmp_path, MONTH) == tmp_path / "regatta" / "2026-11"


# --- a second career beside the first ---------------------------------------------------

def test_entering_makes_a_second_career_in_its_own_directory_and_leaves_the_first_alone(tmp_path, monkeypatch, november):
    own = _own(tmp_path)
    before = (tmp_path / "77.json").read_bytes()
    monkeypatch.setattr(vr, "confirm", lambda prompt, p, **kw: False)
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.enter_regatta(vr.Palette(False), own, tmp_path, 77) is None      # declined: nothing is made
    assert not (tmp_path / "regatta").exists()
    asked = []
    monkeypatch.setattr(vr, "confirm", lambda prompt, p, **kw: asked.append(plain(prompt)) or True)
    with contextlib.redirect_stdout(io.StringIO()):
        regatta = vr.enter_regatta(vr.Palette(False), own, tmp_path, 77)
    assert asked == ["Enter November's Regatta? A second career, in the galaxy every pilot flies this month."]
    assert regatta.save.regatta == MONTH and regatta.save.seed == vr.regatta_seed(MONTH)
    assert regatta.save.pilot.handle == own.save.pilot.handle and regatta.save.turn == 0
    folder = tmp_path / "regatta" / MONTH
    assert (folder / "77.json").exists() and (folder / "scores" / "77.json").exists()
    assert (tmp_path / "77.json").read_bytes() == before                             # the caller's own career, byte for byte
    # Entering again opens the same career: no question, no new one.
    regatta.save.pilot.credits = 5_000
    regatta.commit()
    asked.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        again = vr.enter_regatta(vr.Palette(False), own, tmp_path, 77)
    assert asked == [] and again.save.pilot.credits == 5_000 and again.save.regatta == MONTH


def test_it_is_off_where_the_sysop_switched_it_off(tmp_path, monkeypatch):
    monkeypatch.setenv("DOOR_REGATTA", "0")
    own = _own(tmp_path)
    assert vr.enter_regatta(vr.Palette(False), own, tmp_path, 77) is None and vr.regatta_notice(own) is None


def test_a_regatta_career_does_not_retire_and_a_save_refuses_one_in_the_wrong_galaxy():
    save = vr.new_regatta_career(_world_with_seed(42).save, MONTH)
    save.pilot.highest_rank_seen = len(vr.RANKS) - 1
    assert vr.career_finale_blocker(save, "legend") == "A Regatta career ends with its month; it does not retire."
    record = save.to_dict()
    assert record["regatta"] == MONTH and vr.SaveData.from_dict(record).regatta == MONTH
    assert "regatta" not in _world_with_seed(42).save.to_dict()
    for bad in ({"regatta": "2026-13"}, {"regatta": 7}, {"seed": record["seed"] + 1}, {"regatta": "2026-12"}):
        with pytest.raises((vr.ResumeError, vr.UnsupportedSave)):
            vr.SaveData.from_dict({**record, **bad})


# --- one allowance of jumps, two careers ------------------------------------------------

def test_two_careers_draw_on_the_one_allowance(tmp_path, monkeypatch, november):
    own = _own(tmp_path)
    monkeypatch.setattr(vr, "confirm", lambda prompt, p, **kw: True)
    with contextlib.redirect_stdout(io.StringIO()):
        regatta = vr.enter_regatta(vr.Palette(False), own, tmp_path, 77)
    assert vr.flight_left(own) == vr.flight_left(regatta) == 12
    for flown in range(1, 6):                       # five in the Regatta
        vr.spend_jump(regatta)
        assert vr.flight_left(regatta) == vr.flight_left(own) == 12 - flown
    for flown in range(1, 4):                       # three more in the caller's own career
        vr.spend_jump(own)
        assert vr.flight_left(own) == vr.flight_left(regatta) == 7 - flown
    assert own.save.flight == {"day": 1000, "left": 4} and regatta.save.flight == {"day": 1000, "left": 7}
    # The next day both have what one caller would: yesterday's four and today's twelve.
    own.flight_clock = regatta.flight_clock = lambda: 1001
    assert vr.flight_left(own) == vr.flight_left(regatta) == 16
    # And a career that was never flown puts no limit on the other.
    fresh = _world_with_seed(42)
    fresh.flight_clock, fresh.flight_per_day = (lambda: 1001), 12
    fresh.flight_sibling = lambda: {}
    assert vr.flight_left(fresh) == 12
    fresh.flight_sibling = lambda: {"day": 1001, "left": 2}
    assert vr.flight_left(fresh) == 2


def test_the_door_reads_the_other_careers_record_without_opening_it(tmp_path, november):
    assert vr.peek_flight(tmp_path, 77) == {}
    own = _own(tmp_path)
    assert vr.peek_flight(tmp_path, 77) == {}
    own.save.flight = {"day": 1000, "left": 3}
    own.commit()
    assert vr.peek_flight(tmp_path, 77) == {"day": 1000, "left": 3}
    (tmp_path / "78.json").write_text("not json", encoding="utf-8")
    (tmp_path / "79.json").write_text(json.dumps({"flight": {"day": -1, "left": 3}}), encoding="utf-8")
    assert vr.peek_flight(tmp_path, 78) == {} and vr.peek_flight(tmp_path, 79) == {}


# --- what a career is worth, and the table ------------------------------------------------

def test_net_worth_is_credits_cargo_at_cost_and_what_went_into_the_ship_and_stakes():
    world = _world_with_seed(42)
    save = world.save
    assert vr.career_worth(save) == save.pilot.credits == vr.STARTING_CREDITS
    _set_cargo(world, {"food": 10}, unit_cost=12)
    assert vr.career_worth(save) == vr.STARTING_CREDITS + 120
    save.ship.cargo_tier = 2
    modules = vr.UPGRADES["cargo"]["cost"](0) + vr.UPGRADES["cargo"]["cost"](1)
    assert vr.career_worth(save) == vr.STARTING_CREDITS + 120 + modules
    save.ship.hull_class = "Argosy"
    assert vr._hull_prices() == {"Shuttle": 0, "Freighter": 15_000, "Cutter": 15_000, "Carrier": 60_000,
                                 "Argosy": 60_000, "Frigate": 60_000}
    assert vr.career_worth(save) == vr.STARTING_CREDITS + 120 + modules + 60_000
    save.flags.update(stake_0_1=True, stake_0_2=True)
    assert vr.career_worth(save) == vr.STARTING_CREDITS + 120 + modules + 60_000 + 15_000 + 30_000
    # Spending credits on the ship does not lower it: that is the point of ranking by it.
    save.pilot.credits -= 1_000
    save.ship.weapon_tier = 1
    assert vr.UPGRADES["weapon"]["cost"](0) == 1_000


def test_a_regatta_careers_score_record_carries_its_standing_while_its_month_lasts(tmp_path, monkeypatch, november):
    own = _own(tmp_path)
    monkeypatch.setattr(vr, "confirm", lambda prompt, p, **kw: True)
    with contextlib.redirect_stdout(io.StringIO()):
        regatta = vr.enter_regatta(vr.Palette(False), own, tmp_path, 77)
    folder = vr.regatta_dir(tmp_path, MONTH)
    regatta.save.pilot.credits, regatta.save.turn = 9_000, 14
    regatta.commit()
    entry = vr._pilot_score(folder, 77)
    assert entry["regatta"] == {"month": MONTH, "worth": 9_000, "days": 14}
    assert "regatta" not in vr._pilot_score(tmp_path, 77)        # the caller's own record is as it was
    # The month turns: what the career does next no longer moves its standing.
    monkeypatch.setenv("DOOR_REGATTA", "2026-12")
    regatta.save.pilot.credits = 90_000
    regatta.commit()
    assert vr._pilot_score(folder, 77)["regatta"] == {"month": MONTH, "worth": 9_000, "days": 14}
    for bad in ({"month": "2026-13", "worth": 1, "days": 1}, {"month": MONTH, "worth": -1, "days": 1},
                {"month": MONTH, "worth": 1}, "first"):
        assert vr._score_regatta(bad) is None


def _entry(user_id, handle, worth, days, month=MONTH):
    return {"user_id": user_id, "handle": handle, "best_credits": worth, "retirements": 0, "kills": 0,
            "missions_completed": 0, "regatta": {"month": month, "worth": worth, "days": days}}


def test_the_table_ranks_by_net_worth_and_then_by_fewer_days(tmp_path, november, terminal):
    terminal(80, 24)
    folder = vr.regatta_dir(tmp_path, MONTH) / "scores"
    folder.mkdir(parents=True)
    for entry in (_entry(1, "Ada", 40_000, 30), _entry(2, "Bex", 90_000, 55), _entry(3, "Cy", 40_000, 22),
                  _entry(4, "Old", 500_000, 99, "2026-10")):
        (folder / f"{entry['user_id']}.json").write_text(json.dumps(entry), encoding="utf-8")
    standings = vr.regatta_standings(vr._load_score_records(folder.parent), MONTH)
    assert [entry["handle"] for entry in standings] == ["Bex", "Cy", "Ada"]      # last month's career is not on this table
    own = _own(tmp_path, 3)
    page = [" ".join(plain(line).split()) for line in vr.regatta_lines(own, tmp_path, 3)]
    assert page[0].startswith("NOVEMBER 2026 One galaxy this month, the same for every pilot on every board.")
    assert "1 Bex 90,000cr 55 Void Baron" in page and "2 Cy [YOU] 40,000cr 22 Merchant Captain" in page
    assert any(line.startswith("You have entered: 2nd of 3. [E] flies your Regatta career.") for line in page)
    assert "Both careers fly on your one daily allowance of jumps." in page
    stranger = _own(tmp_path, 9)
    page = " ".join(" ".join(plain(line).split()) for line in vr.regatta_lines(stranger, tmp_path, 9))
    assert "You have not entered. [E] enters: a second career in this month's galaxy" in page
    stranger.flight_per_day = 0
    assert "This board sets no limit on jumps, so the table also measures time spent." in " ".join(
        " ".join(plain(line).split()) for line in vr.regatta_lines(stranger, tmp_path, 9))
    assert [vr.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 103)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "103rd"]


def test_last_months_first_three_stay_on_the_page(tmp_path, monkeypatch, terminal):
    terminal(80, 24)
    monkeypatch.setenv("DOOR_REGATTA", "2026-12")
    folder = vr.regatta_dir(tmp_path, MONTH) / "scores"
    folder.mkdir(parents=True)
    for entry in (_entry(1, "Ada", 40_000, 30), _entry(2, "Bex", 90_000, 55), _entry(3, "Cy", 41_000, 22),
                  _entry(4, "Dov", 10, 1)):
        (folder / f"{entry['user_id']}.json").write_text(json.dumps(entry), encoding="utf-8")
    page = [" ".join(plain(line).split()) for line in vr.regatta_lines(_own(tmp_path), tmp_path, 77)]
    assert page[0].startswith("DECEMBER 2026") and "Nobody has entered this month yet." in page
    assert page[-3:] == ["1st Bex 90,000cr", "2nd Cy 41,000cr", "3rd Ada 40,000cr"]


# --- where a caller is told -----------------------------------------------------------------

def test_a_callers_own_career_is_told_once_a_month_and_not_in_its_first_days(tmp_path, november):
    own = _own(tmp_path)
    assert vr.regatta_notice(own) is None                       # day 0: a new pilot has enough to read
    own.save.turn = vr.REGATTA_TELL_DAY
    assert vr.regatta_notice(own) == "November's Regatta is open: one galaxy for every pilot. [R] on the deck."
    assert vr.regatta_notice(own) is None and own.save.flags["regatta_told_2026-11"] is True
    vr.SaveData.from_dict(own.save.to_dict())
    regatta = vr.World(vr.new_regatta_career(own.save, MONTH))
    regatta.save.turn = 9
    assert vr.regatta_notice(regatta) is None                    # it knows


def test_a_regatta_careers_deck_says_which_career_it_is(november, terminal):
    terminal(80, 24)
    regatta = vr.World(vr.new_regatta_career(_world_with_seed(42).save, MONTH))
    alerts = " ".join(" ".join(plain(row).split()) for row in vr.deck_alert_rows(regatta))
    assert "Regatta November 2026; your own career waits [R] table" in alerts
    assert "Regatta" not in " ".join(plain(row) for row in vr.deck_alert_rows(_world_with_seed(42)))
    pages = vr._service_pages(vr.station_deck_lines(regatta), "Command Deck: 1,200cr", "[X] Expand [Q] Exit: ")
    assert len(pages) == 1                                       # a first Regatta deck is still one page


def test_the_hall_of_fame_shows_the_way_only_where_there_is_a_regatta(tmp_path, monkeypatch):
    world = _own(tmp_path)
    for value, offered in (("2026-11", True), ("0", False)):
        monkeypatch.setenv("DOOR_REGATTA", value)
        keys = iter(["R", "B"])
        monkeypatch.setattr(vr, "read_key", lambda: next(keys))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = vr.screen_hall_of_fame(vr.Palette(False), world, tmp_path, 77)
        assert (result == "R") == offered and ("[R] Regatta" in plain(output.getvalue())) == offered


# --- the real door ----------------------------------------------------------------------------

def test_the_real_door_enters_flies_and_returns_and_keeps_two_careers(tmp_path):
    own = _world_with_seed(42)
    own.save.turn = 5
    vr.persist(own, tmp_path, 77)
    before = json.loads((tmp_path / "77.json").read_text(encoding="utf-8"))
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester", "terminal_width": 80, "terminal_height": 24}),
                    encoding="utf-8")
    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info),
               DOOR_REGATTA=MONTH, DOOR_JUMPS_DAY="2026-11-22")
    # Open the page, enter, say yes, look at the table from inside, return, and leave.
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=b"REYRBREQ", capture_output=True, env=env, timeout=90)
    shown = b" ".join(plain_bytes(result.stdout).split()).decode("utf-8", "replace")
    assert result.returncode == 0 and not result.stderr
    assert "November's Regatta is open: one galaxy for every pilot. [R] on the deck." in shown
    assert "The Regatta" in shown and "Enter November's Regatta?" in shown
    assert "Regatta November 2026; your own career waits" in shown and "You are flying it: 1st of 1." in shown
    assert "Back in your own career." in shown
    folder = tmp_path / "regatta" / MONTH
    regatta = json.loads((folder / "77.json").read_text(encoding="utf-8"))
    assert regatta["regatta"] == MONTH and regatta["seed"] == vr.regatta_seed(MONTH) and regatta["turn"] == 0
    score = json.loads((folder / "scores" / "77.json").read_text(encoding="utf-8"))
    assert score["regatta"] == {"month": MONTH, "worth": vr.STARTING_CREDITS, "days": 0}
    after = json.loads((tmp_path / "77.json").read_text(encoding="utf-8"))
    assert after["seed"] == before["seed"] and after["turn"] == 5 and "regatta" not in after
    assert after["flags"].get("regatta_told_2026-11") is True
    # A second call opens the Regatta career that is there: no question this time.
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=b"REQ", capture_output=True, env=env, timeout=90)
    shown = b" ".join(plain_bytes(result.stdout).split()).decode("utf-8", "replace")
    assert result.returncode == 0 and "Enter November's Regatta?" not in shown and "your own career waits" in shown
    # With the Regatta switched off the door has no such page, and the careers are untouched.
    env["DOOR_REGATTA"] = "0"
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=b"RQ", capture_output=True, env=env, timeout=90)
    assert result.returncode == 0 and b"The Regatta" not in plain_bytes(result.stdout)


def test_a_regatta_career_is_over_when_its_month_is_no_longer_the_month(monkeypatch):
    regatta = vr.World(vr.new_regatta_career(_world_with_seed(42).save, MONTH))
    own = _world_with_seed(42)
    monkeypatch.setenv("DOOR_REGATTA", MONTH)
    assert not vr.regatta_over(regatta) and not vr.regatta_over(own)
    monkeypatch.setenv("DOOR_REGATTA", "2026-12")       # the month turned with the career open
    assert vr.regatta_over(regatta) and not vr.regatta_over(own)
    monkeypatch.setenv("DOOR_REGATTA", "0")             # or the SysOp switched it off
    assert vr.regatta_over(regatta) and not vr.regatta_over(own)
