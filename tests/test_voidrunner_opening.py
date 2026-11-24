"""The staged opening: a first career's first decks (review).

A first career's first deck listed fourteen services at one weight. It now
opens on what a first turn needs and the rest arrives as it starts to matter.
These pin which services arrive when, that whatever a pilot is already involved
with is always there, that a hidden service still answers its key, that a
veteran sees everything, and that the arrival is announced once.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from .support import _finale_world, _world_with_seed, plain, vr

FIRST_DAY = {"M", "C", "B", "Y", "S", "G", "O"}


def _keys(world) -> set[str]:
    return {key for key, _ in vr.deck_service_entries(world)}


def test_a_first_career_opens_on_what_a_first_turn_needs():
    world = _world_with_seed(42)
    assert _keys(world) == FIRST_DAY
    world.save.turn = 1
    assert _keys(world) == FIRST_DAY | {"T", "V", "H"}
    world.save.turn = 2
    assert _keys(world) == FIRST_DAY | {"T", "V", "H"}
    world.save.turn = 3
    assert _keys(world) == FIRST_DAY | {"T", "V", "H", "N"}
    # The order is the deck's own, whatever has arrived.
    assert [key for key, _ in vr.deck_service_entries(world)][:6] == ["M", "C", "B", "Y", "S", "T"]


def test_a_first_deck_is_three_rows_of_services_at_80x24(terminal):
    terminal(80, 24)
    world = _world_with_seed(42)
    entries = [f"[{key}] {label}" for key, label in vr.deck_service_entries(world)]
    assert vr.menu_columns(entries) == 3 and len(vr.menu_grid(vr.deck_service_entries(world))) == 3
    pages = vr._service_pages(vr.station_deck_lines(world), "Command Deck: 1,200cr", "[X] Expand [Q] Exit: ")
    assert len(pages) == 1 and len(pages[0]) + 3 == 12  # twelve rows: the whole window and two to spare


@pytest.mark.parametrize("key,involve", [
    ("N", lambda save: save.pilot.reputation.__setitem__(vr.FACTION_CONCORD, 1)),
    ("N", lambda save: save.pilot.reputation.__setitem__(vr.FACTION_BLACKWAKE, -2)),
    ("N", lambda save: save.flags.__setitem__("archive_v1_started", True)),
    ("N", lambda save: save.tales.__setitem__("Hollow Reach", 1)),
    ("T", lambda save: setattr(save.trading_ledger, "since_day", 0)),
])
def test_whatever_a_pilot_is_already_involved_with_is_on_the_deck(key, involve):
    world = _world_with_seed(42)
    assert key not in _keys(world)
    involve(world.save)
    assert key in _keys(world) and vr.deck_service_day(world, key) == 0


def test_a_pilot_who_has_retired_sees_everything_from_day_zero():
    world = _finale_world("legend")
    fresh = vr.World(vr.finish_career(world.save, "legend"))
    assert fresh.save.turn == 0 and fresh.save.pilot.retirements == 1
    assert _keys(fresh) >= FIRST_DAY | {"T", "V", "H", "N"}
    assert not vr.deck_services_arriving(fresh)


@pytest.mark.parametrize("key", list("TVHPWN"))
def test_a_service_that_has_not_arrived_still_answers_its_key(monkeypatch, key):
    world = _world_with_seed(42)
    assert key not in _keys(world)
    monkeypatch.setattr(vr, "read_key", lambda: key)
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.screen_station_menu(vr.Palette(False), world) == key


def test_the_arrival_that_brings_a_service_says_so_once():
    world = _world_with_seed(42)
    assert vr.deck_services_arriving(world) == []
    world.save.turn = 1
    assert vr.deck_services_arriving(world) == [("T", "Ledger"), ("V", "Viewport"), ("H", "Hall of Fame")]
    world.save.trading_ledger.since_day = 0   # the Ledger was already there: it traded on day zero
    assert vr.deck_services_arriving(world) == [("V", "Viewport"), ("H", "Hall of Fame")]
    world.save.turn = 2
    assert vr.deck_services_arriving(world) == []
    world.save.turn = 3
    assert vr.deck_services_arriving(world) == [("N", "Contacts")]


def test_a_jump_reports_what_it_brought_to_the_deck(monkeypatch):
    world = _world_with_seed(42)
    world.event_rng.random = lambda: 1.0  # no encounter
    destination = sorted(world.here.connections)[0]
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, destination)
    assert world.save.turn == 1
    assert "New on the deck: [T] Ledger, [V] Viewport, [H] Hall of Fame." in world.hop_report


def test_the_guide_opens_on_the_game_and_says_what_arrives_later(terminal):
    terminal(80, 24)
    world = _world_with_seed(42)
    rows = [plain(row) for row in vr.pilot_guide_lines(world)]
    assert rows[0].startswith("Your ship is your livelihood.") and "[B] is Back" in rows[1]
    assert any("The deck fills in as you fly" in row and "[N] Contacts after your third" in row for row in rows)
    # First flight above the rule, reference below it.
    later = next(number for number, row in enumerate(rows) if "TRADING" in row)
    assert any(row.startswith("5. Upgrade") for row in rows[:later])
    assert any(row.startswith("Danger is a risk rating") for row in rows[later:])
    world.save.turn = 3
    assert not any("The deck fills in" in plain(row) for row in vr.pilot_guide_lines(world))


def test_disembark_is_the_action_bars_and_never_a_row_of_its_own(monkeypatch, terminal):
    """As the grid's last entry it was, some days, a fifth row with one key on
    it; the bar under the grid always says `[Q] Exit`."""
    terminal(80, 24)
    for day in range(5):
        world = _world_with_seed(42)
        world.save.turn = day
        assert "Q" not in _keys(world)
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    with contextlib.redirect_stdout(io.StringIO()) as output:
        assert vr.screen_station_menu(vr.Palette(False), _world_with_seed(42)) == "Q"
    assert "[Q] Exit" in " ".join(plain(output.getvalue()).split())
