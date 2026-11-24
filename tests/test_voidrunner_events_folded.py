"""Economy events are folded into seasons (review).

An event was a 30% boom or crash in one good at a few stations for a
fortnight: the fourth thing moving a price, and the only one a pilot could not
see coming. These pin its going: nothing draws, saves or shows one, a save
that had one running is read and simply has none, a quiet day's luck is what
it was, and the ledger's bulletin reports the seasons of the pilot's own sector.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from .support import _world_with_seed, plain, vr


def test_nothing_starts_saves_or_shows_an_event():
    world = _world_with_seed(42)
    for gone in ("tick_economy_event", "economy_event_system_ids", "ECONOMY_EVENT_CHANCE_PER_TURN"):
        assert not hasattr(vr, gone)
    assert "active_event" not in world.save.to_dict()
    text = " ".join(plain(line) for line in vr.station_deck_lines(world, expanded=True) + vr.deck_alert_rows(world)
                    + vr.market_catalog_lines(world, vr.LEGAL_COMMODITIES) + vr.pilot_record_lines(world, "O"))
    assert "CRASH" not in text and "BOOM" not in text and "Economy event" not in text


def test_a_save_with_an_event_running_is_read_and_has_none():
    world = _world_with_seed(42)
    record = world.save.to_dict()
    record["active_event"] = {"economy": "Industrial", "commodity": "metals", "direction": "boom",
                              "turns_remaining": 6, "description": "Refined Metals prices spike near Freeport",
                              "system_ids": [0]}
    record["market_drift"] = {"0": {"metals": 1.3}}
    loaded = vr.World(vr.SaveData.from_dict(record))
    assert "active_event" not in loaded.save.to_dict()
    # The price it held is an ordinary drift now, and rests like any other.
    high = vr.price_for(loaded, 0, "metals")
    for _ in range(40):
        vr.tick_price_reversion(loaded)
    assert vr.price_for(loaded, 0, "metals") < high


def test_a_quiet_days_luck_is_what_it_was(monkeypatch):
    """The chance an event took one draw for each day: the draw stays, so a day
    on which no event would have begun leads to the encounters it did before."""
    world = _world_with_seed(42)
    world.event_rng.seed(7)
    twin = vr.random.Random(7)
    world.save.pilot.credits = 0  # at Freeport with nothing: the foreman has a shift
    vr.work_dock_shift(world)
    # A shift draws nothing else from the event RNG but each station's price
    # settling and this one draw; the next value must be the one after them.
    drawn = sum(len(table) for table in world.save.market_drift.values()) + 1
    for _ in range(drawn):
        twin.random()
    assert world.event_rng.random() == twin.random()


@pytest.mark.parametrize("day", [0, 6, 13, 27])
def test_the_ledgers_bulletin_reports_the_seasons_of_this_sector(day):
    world = _world_with_seed(42)
    world.save.turn = day
    lines = vr.season_bulletin_lines(world)
    assert lines[0].strip(chr(1) + " ") == f"SEASONS IN {vr.sector_for(world.here).upper()}"
    text = [" ".join(plain(line).split()) for line in lines[1:]]
    listed = 0
    for commodity in vr.LEGAL_COMMODITIES:
        trend = vr.season_trend(world, world.here.id, commodity)
        label = vr.COMMODITIES[commodity]["label"]
        row = next((row for row in text if row.startswith(label + " ")), None)
        if trend is None:
            assert row is None
            continue
        listed += 1
        rising, days = trend
        off = round((vr.season_factor(world, world.here.id, commodity) - 1) * 100)
        assert f"{'rising' if rising else 'falling'} {vr.plural(days, 'more day')}" in row
        assert f"{off:+d}% against usual" in row
    if listed:
        assert text[-1].startswith("The same across this sector.")
    else:
        assert text == ["Prices here are level today."]


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_the_bulletin_fits_and_heads_the_ledgers_opportunities(monkeypatch, width, height):
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
    world = _world_with_seed(42)
    world.save.turn = 9
    lines = vr.economy_opportunity_lines(world, [])
    assert lines[:len(vr.season_bulletin_lines(world))] == vr.season_bulletin_lines(world)
    assert "PUBLIC ECONOMY BULLETIN" not in " ".join(lines)
    for page in vr._service_pages(lines, "Opportunities", "[B] Back [<>] Page: "):
        assert all(vr._visible_width(row) <= vr._page_content_width() for row in page)
