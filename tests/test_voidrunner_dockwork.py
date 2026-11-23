"""Freeport's docks have a shift for a pilot with nothing (review).

Losing a ship left a pilot at Freeport with no credits, a quarter of a hull and
an empty hold, and for a few careers that was a loop: fly the quarter-hull out,
lose it, wake at Freeport. These pin the floor under it: who the foreman has a
shift for and where, what a shift gives and what it costs, that the day really
passes, that the docks talk, that the deck, the wreck and the guide all say
where the work is, and that the work stops the moment a pilot can trade.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from .support import _world_with_seed, plain, vr


def _wrecked(seed=42, credits=0):
    """A pilot as the tug leaves them: at Freeport, broke, a quarter of a hull."""
    world = _world_with_seed(seed)
    world.save.pilot.credits = credits
    world.save.ship.hull_hp = vr.hull_hp_max(world.save.ship) // 4
    world.save.turn = 12
    return world


def test_the_foreman_has_a_shift_only_at_freeport_and_only_for_a_pilot_who_cannot_trade():
    world = _wrecked()
    assert vr.dock_work_open(world)
    world.save.pilot.credits = vr.DOCK_WORK_BELOW - 1
    assert vr.dock_work_open(world)
    world.save.pilot.credits = vr.DOCK_WORK_BELOW
    assert not vr.dock_work_open(world)
    with pytest.raises(ValueError):
        vr.work_dock_shift(world)
    away = _wrecked()
    away.save.current_system = sorted(away.here.connections)[0]
    assert not vr.dock_work_open(away)
    # A career's first day is not broke: the starting bank is twice the line.
    assert not vr.dock_work_open(_world_with_seed(42)) and vr.STARTING_CREDITS >= 2 * vr.DOCK_WORK_BELOW


def test_a_shift_pays_patches_the_hull_to_half_and_costs_the_day():
    world = _wrecked()
    full = vr.hull_hp_max(world.save.ship)
    lines = vr.work_dock_shift(world)
    assert world.save.pilot.credits == vr.DOCK_WORK_PAY and world.save.turn == 13
    assert world.save.ship.hull_hp == full // 2 == vr.dock_work_hull(world.save.ship)
    assert lines[0].split(" +")[0] in vr.DOCK_WORK_STORIES
    assert lines[0].endswith(f"+{vr.DOCK_WORK_PAY}cr; the yard patched your hull to {full // 2}/{full}.")
    assert "Worked a shift on Freeport's docks" in world.save.pilot.log[-1]
    # A hull already past half is left alone: the yard hands patch, they do not repair.
    world.save.ship.hull_hp = full - 1
    lines = vr.work_dock_shift(world)
    assert world.save.ship.hull_hp == full - 1 and lines[0].endswith(f"+{vr.DOCK_WORK_PAY}cr.")


def test_three_shifts_are_a_hold_of_cargo_and_then_the_foreman_stops_asking():
    world = _wrecked()
    shifts = 0
    while vr.dock_work_open(world):
        vr.work_dock_shift(world)
        shifts += 1
    assert shifts == 3 and world.save.pilot.credits == vr.DOCK_WORK_BELOW
    assert world.save.accolades["counters"]["shifts"] == 3
    assert "dock_hand" in {accolade.id for accolade in vr.unlock_accolades(world.save)}


def test_the_day_on_the_docks_is_a_day_contracts_run_down_and_the_crew_is_not_charged():
    world = _wrecked()
    world.save.ship.has_gunner = True
    due = vr.Mission(id=4, kind="scan", description="Survey", reward=300, origin_system=0, target_system=3,
                     deadline_turn=world.save.turn)
    world.save.active_missions = [due]
    lines = vr.work_dock_shift(world)
    assert world.save.active_missions == [] and any("Survey" in line for line in lines)
    assert world.save.ship.has_gunner and world.save.pilot.credits == vr.DOCK_WORK_PAY
    # The board is the new day's: it was prepared when the shift ended.
    assert world.save.mission_boards[0]["refresh_turn"] > world.save.turn


@pytest.mark.parametrize("seed", range(40, 48))
def test_the_docks_talk_whenever_there_is_something_to_say(seed):
    world = _wrecked(seed)
    there_is = bool(vr.rumour_candidates(world))
    lines = vr.work_dock_shift(world)
    heard = [key for key in world.save.flags if key.startswith("rumour_")]
    assert len(heard) == (1 if there_is else 0)
    if there_is:
        kind = world.traits[int(heard[0].removeprefix("rumour_"))]["kind"]
        assert lines[-1].endswith(f"{vr.RUMOUR_WHAT[kind]}.")


def test_an_arrival_still_hears_talk_one_time_in_three():
    """Asking for the talk outright is the shift's; an ordinary docking's draw is what it was."""
    world = _world_with_seed(42)
    heard = 0
    for day in range(1, 91):
        world.save.turn = day
        world.save.flags = {key: value for key, value in world.save.flags.items() if not key.startswith("rumour_")}
        heard += vr.dock_rumour(world) is not None
    assert 15 <= heard <= 45


def test_the_deck_the_wreck_and_the_guide_say_where_the_work_is():
    world = _wrecked()
    rows = " ".join(plain(row) for row in vr.deck_alert_rows(world))
    assert f"Dock work a day's shift pays {vr.DOCK_WORK_PAY}cr and patches the hull" in " ".join(rows.split())
    assert "[F] foreman" in rows
    world.save.pilot.credits = 5_000
    assert "Dock work" not in " ".join(plain(row) for row in vr.deck_alert_rows(world))
    # The wreck itself points at it, when it leaves the pilot with nothing.
    broke = _world_with_seed(42)
    broke.save.pilot.credits = 100
    assert vr.destroy_ship(broke).endswith("Freeport's docks have work for a pilot with nothing: [F] on the deck.")
    rich = _world_with_seed(42)
    rich.save.pilot.credits = 50_000
    assert "docks have work" not in vr.destroy_ship(rich)
    assert any("[F] on Freeport's deck" in line for line in vr.pilot_guide_lines(world))


def test_the_foremans_page_says_what_a_shift_is_and_w_works_it(monkeypatch):
    world = _wrecked()
    text = " ".join(vr.dock_work_lines(world))
    full = vr.hull_hp_max(world.save.ship)
    assert f"Pay: {vr.DOCK_WORK_PAY}cr" in text and f"patch it to {full // 2}/{full}" in text and "one day" in text
    keys = iter("W")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(vr, "read_command_at_prompt", lambda *a, **k: next(keys), raising=False)
    monkeypatch.setattr(vr, "_draw_service_page", lambda p, title, lines, footer, page, **k: (next(keys), 0, 1))
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_dock_work(vr.Palette(False), world)
    assert world.save.turn == 13 and world.save.pilot.credits == vr.DOCK_WORK_PAY
    # The deck that follows lists the day.
    assert world.hop_report and world.hop_report[0].split(" +")[0] in vr.DOCK_WORK_STORIES


def test_a_shifts_result_is_two_rows_of_an_80_column_deck_at_most():
    """The deck lists the day above its own rows; a third row of story was a second page."""
    room = 2 * (80 - 5) - len("Result: ")
    longest = max(vr.DOCK_WORK_STORIES, key=len)
    assert len(f"{longest} +{vr.DOCK_WORK_PAY}cr; the yard patched your hull to 999/999.") <= room
    assert len(set(vr.DOCK_WORK_STORIES)) == len(vr.DOCK_WORK_STORIES) >= 8
