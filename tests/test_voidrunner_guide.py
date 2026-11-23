"""The Pilot Guide is two things: First Flight and a Reference (review).

The guide had grown to thirty paragraphs and about a thousand words, met all at
once. These pin its two parts: that First Flight is one page at 80x24 and holds
what a first turn needs, that the Reference is by topic and short, which of the
two [G] opens on, and that [R] and [F] turn between them.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from .support import _world_with_seed, plain, vr


@pytest.fixture
def terminal(monkeypatch):
    def size(width: int, height: int, style: str = "auto"):
        monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
        monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
        monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
        monkeypatch.setattr(vr, "_OUTPUT_ENCODING", "utf-8")
    return size


def _words(lines) -> int:
    return sum(len(plain(line).split()) for line in lines)


def test_first_flight_is_one_page_at_80x24_whatever_stage_it_is_at(terminal):
    terminal(80, 24)
    world = _world_with_seed(42)
    stages = [lambda save: None,
              lambda save: save.flags.__setitem__("opening_assignment_taken", True),
              lambda save: save.flags.__setitem__("opening_assignment_completed", True),
              lambda save: setattr(save, "turn", 4)]
    for stage in stages:
        stage(world.save)
        lines = vr.first_flight_lines(world)
        pages = vr._mission_text_pages(lines, overhead=5 + vr._page_frame_rows("Pilot Guide"))
        assert len(pages) == 1, [plain(line) for line in lines]
        assert _words(lines) <= 190


def test_first_flight_is_the_five_steps_of_a_trade_and_the_way_to_the_rest():
    world = _world_with_seed(42)
    rows = [plain(row) for row in vr.first_flight_lines(world)]
    assert rows[0].startswith("Your ship is your livelihood.")
    steps = [row for row in rows if row[:2] in ("1.", "2.", "3.", "4.", "5.")]
    assert [row.split(":")[0] for row in steps] == ["1. Buy", "2. Fuel", "3. Fly", "4. Sell", "5. Upgrade"]
    assert f"Each fuel unit costs {vr.FUEL_PRICE} cr." in steps[1]
    assert f"{vr.UPGRADES['cargo']['cost'](0):,} cr" in steps[4]
    assert any("Optional First Flight" in row and "[O] Offer" in row for row in rows)
    assert rows[-1] == "[R] Reference has the rest, a topic at a time."
    # Nothing of the later game: that is the Reference's.
    assert not any(word in " ".join(rows) for word in ("raider", "season", "Stake", "faction"))


def test_the_reference_is_by_topic_and_two_thirds_the_guide_it_replaced(terminal):
    terminal(80, 24)
    world = _world_with_seed(42)
    lines = vr.guide_reference_lines(world)
    headings = [line.strip(chr(1) + ' ') for line in lines if line.startswith(chr(1))]  # `section` rows
    assert headings == ["TRADING", "FLYING", "FIGHTING", "WORK", "TROUBLE"]
    assert _words(vr.pilot_guide_lines(world)) <= 720  # it was about 1,070
    text = " ".join(plain(line) for line in lines)
    for phrase in ("A beaten raider is yours to decide about.", "[F] on Freeport's deck", "[F] Files",
                   "Danger is a risk rating", "Raiders remember.", "[K] Stake"):
        assert phrase in text
    pages = vr._mission_text_pages(lines, overhead=5 + vr._page_frame_rows("Pilot Guide: Reference"))
    assert len(pages) <= 3
    assert vr.pilot_guide_lines(world) == vr.first_flight_lines(world) + lines


@pytest.mark.parametrize("day,retired,opens", [(0, 0, "Pilot Guide"), (4, 0, "Pilot Guide"),
                                               (5, 0, "Pilot Guide: Reference"), (0, 1, "Pilot Guide: Reference")])
def test_the_guide_opens_on_first_flight_for_a_careers_first_days_and_the_reference_after(monkeypatch, day, retired, opens):
    world = _world_with_seed(42)
    world.save.turn, world.save.pilot.retirements = day, retired
    titles = []
    monkeypatch.setattr(vr, "draw_page", lambda p, title, lines, page, count: titles.append(title))
    monkeypatch.setattr(vr, "read_command_at_prompt", lambda: "B")
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_pilot_guide(vr.Palette(False), world)
    assert titles == [opens]
    assert vr.GUIDE_FIRST_DAYS == 5


def test_r_and_f_turn_between_the_two_and_the_offer_is_first_flights(monkeypatch):
    world = _world_with_seed(42)
    seen = []
    monkeypatch.setattr(vr, "draw_page", lambda p, title, lines, page, count: seen.append(title))
    keys = iter(["R", "F", "F", "R", "O", "B"])
    monkeypatch.setattr(vr, "read_command_at_prompt", lambda: next(keys))
    offered = []
    monkeypatch.setattr(vr, "_screen_opening_offer", lambda p, w, offer: offered.append(offer) or False)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_pilot_guide(vr.Palette(False), world)
    assert seen == ["Pilot Guide", "Pilot Guide: Reference", "Pilot Guide", "Pilot Guide", "Pilot Guide: Reference",
                    "Pilot Guide: Reference"]
    assert offered == []  # [O] means nothing on the Reference
    bars = plain(output.getvalue())
    assert "[O] Offer [R] Reference" in bars and "[F] First Flight" in bars
