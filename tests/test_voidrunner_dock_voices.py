"""Dock voices: what a station is like when you arrive (review).

A quiet arrival said nothing about where the pilot had arrived. These pin the
lines the docks now have for each kind of economy, how often one is said and
that it is only said when nothing else was, that it takes no draw from the
event generator, and that every line fits the deck's rows.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from .support import _world_with_seed, plain, vr


def test_every_economy_has_eight_lines_of_its_own():
    assert set(vr.DOCK_VOICES) == set(vr.ECONOMIES) and vr.DOCK_VOICE_ONE_IN == 3
    every = [line for lines in vr.DOCK_VOICES.values() for line in lines]
    assert all(len(lines) == 8 for lines in vr.DOCK_VOICES.values()) and len(set(every)) == len(every) == 40
    for line in every:
        assert line == line.strip() and line[-1] == "." and line[0].isupper() and line.isascii()
        assert 30 <= len(line) <= 96, line                 # one row of the deck, or two
        assert "  " not in line and "!" not in line and " -- " not in line
    assert 400 < sum(len(line.split()) for line in every) < 700


@pytest.mark.parametrize("width", [80, 64, 40])
def test_a_line_takes_no_more_than_three_rows_of_a_deck(terminal, width):
    terminal(width, 24)
    for lines in vr.DOCK_VOICES.values():
        for line in lines:
            rows = vr.wrap_styled("Result: " + line, vr._page_content_width())
            assert len(rows) <= (2 if width >= 64 else 4), line


def test_a_dock_speaks_on_about_one_arrival_in_three_and_says_its_own_economys_lines():
    world = _world_with_seed(42)
    spoken, heard = 0, set()
    for day in range(1, 301):
        world.save.turn = day
        for system in world.galaxy[:12]:
            world.save.current_system = system.id
            line = vr.dock_voice(world)
            if line is not None:
                spoken += 1
                heard.add((system.economy, line))
                assert line in vr.DOCK_VOICES[system.economy]
    assert 0.27 < spoken / (300 * 12) < 0.40
    # Over a long career every line of every economy met comes round.
    for economy in {system.economy for system in world.galaxy[:12]}:
        assert {line for kind, line in heard if kind == economy} == set(vr.DOCK_VOICES[economy])


def test_it_is_the_same_for_the_same_day_and_draws_nothing():
    world = _world_with_seed(42)
    world.save.turn = next(day for day in range(1, 60) if (world.save.__setattr__("turn", day), vr.dock_voice(world))[1])
    state = world.event_rng.getstate()
    first = vr.dock_voice(world)
    assert first is not None and vr.dock_voice(world) == first and world.event_rng.getstate() == state
    other = _world_with_seed(43)
    other.save.turn = world.save.turn
    assert (vr.dock_voice(other), other.save.seed) != (first, world.save.seed)  # another career, another dock


def _arrive(world, monkeypatch, *, crew=None, rumour=None, tale=()):
    """Fly one quiet jump with the other arrival voices stubbed; returns what the arrival reported."""
    target = sorted(world.here.connections)[0]
    world.save.ship.fuel = vr.fuel_capacity(world.save.ship)
    world.event_rng.random = lambda: 1.0   # no encounter, no inspection
    monkeypatch.setattr(vr, "play_scene", lambda make: True)
    monkeypatch.setattr(vr, "crew_voice", lambda *a, **k: crew)
    monkeypatch.setattr(vr, "dock_rumour", lambda *a, **k: rumour)
    monkeypatch.setattr(vr, "sector_tale", lambda w: list(tale))
    monkeypatch.setattr(vr, "dock_voice", lambda w: "The dock says its piece.")
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_travel(vr.Palette(False), world, target)
    return " ".join(plain(output.getvalue()).split()) + " " + " ".join(world.hop_report)


def test_a_dock_speaks_only_when_nothing_else_did(monkeypatch):
    assert "The dock says its piece." in _arrive(_world_with_seed(42), monkeypatch)
    for said in ({"crew": 'Nia Voss: "Quiet lane."'}, {"rumour": "A foreman swears Xanthe is short of something."},
                 {"tale": ["Hollow Reach, I: The Reach was mined hollow and left."]}):
        report = _arrive(_world_with_seed(42), monkeypatch, **said)
        assert "The dock says its piece." not in report
