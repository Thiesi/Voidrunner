"""A caller has so many jumps a real day (review).

A jump cost fuel and nothing else, so a career was as long as an evening.
These pin the allowance: how many jumps a day and how many carry over, when
the day turns, what a jump and a dock shift spend, that everything else stays
free, what the SysOp can set, what the caller is shown, and what a save keeps.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from .support import _world_with_seed, plain, vr


def _rationed(day=1000, per_day=None, seed=42):
    """A world on a board that rations jumps, with a calendar the test turns."""
    world = _world_with_seed(seed)
    clock = {"day": day}
    world.flight_clock = lambda: clock["day"]
    world.flight_per_day = vr.FLIGHT_PER_DAY if per_day is None else per_day
    world.save.pilot.credits = 50_000
    world.event_rng.random = lambda: 1.0  # no encounters, no inspections
    return world, clock


def _jump(world):
    here = world.here
    target = sorted(here.connections)[0]
    world.save.ship.fuel = vr.fuel_capacity(world.save.ship)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, target)
    return world.save.current_system != here.id


def test_twelve_a_day_and_a_world_without_a_clock_or_a_figure_is_not_rationed():
    assert (vr.FLIGHT_PER_DAY, vr.FLIGHT_BANK_DAYS) == (12, 2)
    plain_world = _world_with_seed(42)
    assert vr.flight_left(plain_world) is None and vr.flight_blocker(plain_world) is None
    world, _clock = _rationed()
    assert vr.flight_left(world) == 12  # a new pilot starts with one day's worth
    world.flight_per_day = 0            # the SysOp switched it off
    assert vr.flight_left(world) is None
    assert _jump(world) and world.save.flight == {}  # nothing is reckoned where nothing is rationed


def test_a_jump_spends_one_and_the_thirteenth_is_refused():
    world, _clock = _rationed()
    for flown in range(1, 13):
        assert _jump(world) and vr.flight_left(world) == 12 - flown
    assert world.save.flight == {"day": 1000, "left": 0} and world.save.turn == 12
    day, here = world.save.turn, world.save.current_system
    assert not _jump(world)
    assert (world.save.turn, world.save.current_system) == (day, here) and world.save.pending_travel is None
    assert vr.flight_blocker(world).startswith("No jumps left today. 12 more jumps at midnight, in ")
    assert "Trading and planning cost nothing" in vr.flight_blocker(world)


def test_the_day_turns_and_unflown_jumps_carry_over_up_to_two_days_worth():
    world, clock = _rationed()
    for _ in range(5):
        _jump(world)
    assert vr.flight_left(world) == 7
    clock["day"] += 1
    assert vr.flight_left(world) == 19          # the seven left over and the new twelve
    clock["day"] += 1
    assert vr.flight_left(world) == 24          # the bank is two days of jumps
    clock["day"] += 30
    assert vr.flight_left(world) == 24
    _jump(world)
    assert world.save.flight == {"day": clock["day"], "left": 23}
    # A clock that runs backwards earns nothing.
    clock["day"] -= 10
    assert vr.flight_left(world) == 23
    _jump(world)
    assert world.save.flight == {"day": clock["day"] + 10, "left": 22}


def test_everything_but_a_jump_is_free():
    world, _clock = _rationed()
    for _ in range(12):
        _jump(world)
    before = vr.flight_left(world)
    good = next(c for c in vr.LEGAL_COMMODITIES if vr.market_depth_quote(world, world.here.id, c)["stock"] > 0)
    vr.trade_cargo(world, good, 1, buying=True)
    vr.trade_cargo(world, good, 1, buying=False)
    vr.generate_mission_board(world)
    world.commit()
    assert vr.flight_left(world) == before == 0


def test_a_dock_shift_is_a_day_and_spends_a_jump():
    world, _clock = _rationed()
    world.save.pilot.credits = 0
    assert vr.dock_work_open(world)
    vr.work_dock_shift(world)
    assert vr.flight_left(world) == 11
    world.save.flight = {"day": 1000, "left": 0}
    world.save.pilot.credits = 0
    assert not vr.dock_work_open(world)  # no hours left for the foreman either


def test_an_interrupted_journey_is_finished_without_a_second_jump(monkeypatch):
    world, _clock = _rationed()
    world.save.flight = {"day": 1000, "left": 1}
    target = sorted(world.here.connections)[0]

    class Dropped(Exception):
        pass

    def drop(*args, **kwargs):
        raise Dropped

    monkeypatch.setattr(vr, "_resolve_random_travel_encounter", drop)
    with pytest.raises(Dropped), contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, target)
    assert world.save.flight["left"] == 0 and world.save.pending_travel is not None
    monkeypatch.undo()
    world.event_rng.random = lambda: 1.0
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, target)  # the hop already paid for goes through
    assert world.save.current_system == target and world.save.flight["left"] == 0


@pytest.mark.parametrize("value,figure", [("", 12), ("20", 20), ("1", 1), ("0", 0), ("off", 0), ("unlimited", 0),
                                          ("-3", 12), ("lots", 12), ("5000", 12), (" 8 ", 8)])
def test_the_sysop_sets_the_figure_and_nonsense_is_the_doors_own(monkeypatch, value, figure):
    monkeypatch.setenv("DOOR_JUMPS", value)
    assert vr.flight_setting() == figure


def test_the_deck_the_chart_and_the_guide_say_how_many_are_left():
    world, _clock = _rationed()
    assert plain(vr.status_band(world)[1]).endswith("Jumps 12")
    target = sorted(world.here.connections)[0]
    assert "jump" not in vr.departure_terms(world, target)  # the chart's header carries the count
    assert any("You have 12 jumps a real day. Unflown jumps carry over, up to 24" in line
               for line in vr.guide_reference_lines(world))
    world.save.flight = {"day": 1000, "left": 1}
    assert vr.departure_terms(world, target).endswith("one day passes. Your last jump today.")
    world.save.flight = {"day": 1000, "left": 0}
    assert plain(vr.status_band(world)[1]).endswith("Jumps 0")
    alerts = " ".join(" ".join(plain(row).split()) for row in vr.deck_alert_rows(world))
    assert "No jumps left today 12 more at midnight, in " in alerts and "[Z] lay up" in alerts
    # Where nothing is rationed, nothing is said.
    free = _world_with_seed(42)
    assert "Jumps" not in plain(vr.status_band(free)[1]) and "jump" not in vr.departure_terms(free, target)
    assert not any("jumps a real day" in line for line in vr.guide_reference_lines(free))


def test_the_chart_refuses_before_it_asks(monkeypatch):
    world, _clock = _rationed()
    world.save.flight = {"day": 1000, "left": 0}
    asked = []
    monkeypatch.setattr(vr, "confirm", lambda *a, **k: asked.append(a) or True)
    keys = iter(["A", "B"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        assert vr.screen_chart(vr.Palette(False), world) is None
    shown = " ".join(plain(output.getvalue()).split())
    assert asked == [] and "No jumps left today." in shown and "jumps 0" in shown and "fuel " in shown


def test_the_allowance_is_the_callers_and_a_save_keeps_it():
    world, _clock = _rationed()
    assert "flight" not in world.save.to_dict()
    for _ in range(3):
        _jump(world)
    record = world.save.to_dict()
    assert record["flight"] == {"day": 1000, "left": 9}
    assert vr.SaveData.from_dict(record).flight == {"day": 1000, "left": 9}
    for bad in ({"day": 1000}, {"day": -1, "left": 3}, {"day": 1000, "left": 99_999}, {"day": 1000, "left": 3, "x": 1}, [9]):
        with pytest.raises((vr.ResumeError, vr.UnsupportedSave)):
            vr.SaveData.from_dict({**record, "flight": bad})
    # Retiring starts a career, not a day: the allowance comes along.
    assert vr.retire_pilot(world.save).flight == {"day": 1000, "left": 9}


def test_the_wait_is_said_in_hours_and_minutes(monkeypatch):
    import datetime as real

    class Frozen(real.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 24, 18, 47, 30)

    monkeypatch.setattr(vr, "datetime", Frozen)
    assert vr.flight_wait() == "5h 12m" and vr.flight_day() == real.date(2026, 11, 24).toordinal()


def test_a_pinned_day_holds_the_calendar_still_and_nonsense_is_no_pin(monkeypatch):
    import datetime as real
    monkeypatch.setenv("DOOR_JUMPS_DAY", "2026-11-22")
    assert vr.flight_day() == real.date(2026, 11, 22).toordinal()
    assert vr.flight_wait() == "12h 0m"  # and stands at noon
    monkeypatch.setenv("DOOR_JUMPS_DAY", "someday")
    assert vr.flight_day() == vr._node_now().date().toordinal()


def test_the_executable_binds_the_calendar_and_the_sysops_figure(monkeypatch):
    """A domain-only world rations nothing; the door a caller runs does."""
    import inspect
    assert "world.flight_clock, world.flight_per_day = flight_day, flight_setting()" in inspect.getsource(vr._run_game)
    world = _world_with_seed(42)
    assert world.flight_clock is None and world.flight_per_day == 0


def test_the_wait_on_the_hour_and_in_the_last_minute(monkeypatch):
    import datetime as real

    def at(hour, minute, second):
        class Frozen(real.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 11, 24, hour, minute, second)
        monkeypatch.setattr(vr, "datetime", Frozen)
        return vr.flight_wait()

    assert at(18, 0, 0) == "6h 0m" and at(23, 30, 0) == "30m" and at(23, 59, 40) == "1m" and at(0, 0, 0) == "24h 0m"
