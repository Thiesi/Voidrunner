"""The window at 80x24: the rows the deck and a fight may take (review, v1.3).

At 80x24 the canopy's picture is the bottom ten rows of the screen, and the two
screens a pilot looks at most -- the Command Deck and a fight -- used to cover
all but four of them. These tests pin the rows those screens may take, and that
the combat table quotes exactly what a round resolves.
"""

from __future__ import annotations

import re

import pytest

from .support import _add_cargo, _set_cargo, _world_with_pending_fight, _world_with_seed, plain, vr

#: The first row of the canopy's band at 80x24 (`canopy_cells`): rows 14-23.
BAND_TOP = round(24 * 0.58)


def _console_rows(lines: list[str], title: str, footer: str) -> int:
    """Rows a one-page screen occupies: its frame, its body and its action bar."""
    pages = vr._service_pages(lines, title, footer)
    assert len(pages) == 1, f"{title} runs to {len(pages)} pages"
    return len(pages[0]) + 3


# --- the window is left open -------------------------------------------------

def test_the_command_deck_leaves_the_whole_window_open_at_80x24(terminal):
    terminal(80, 24)
    world = _world_with_seed(42)
    lines = vr.station_deck_lines(world)
    assert _console_rows(lines, "Command Deck: 1,200cr", "[X] Expand [Q] Exit: ") <= BAND_TOP
    rows = [plain(row) for row in lines]
    # Nothing was dropped to get there: every service is still on the deck.
    for key, _label in vr.deck_service_entries(world):
        assert any(f"[{key}] " in row for row in rows), key
    # The career row and the gauges share the hull's silhouette.
    assert rows[1].startswith("Day 0") and rows[2].startswith("HULL")


def test_the_deck_services_are_three_columns_wherever_a_third_fits(terminal):
    terminal(80, 24)
    world = _world_with_seed(42)
    world.save.pilot.credits = 10_000_000  # the longest Yard label there is
    entries = [f"[{key}] {label}" for key, label in vr.deck_service_entries(world)]
    assert vr.menu_columns(entries) == 3
    assert [key for key, _ in vr.deck_service_entries(world)][:3] == ["M", "C", "B"]


def test_a_landmark_service_is_named_without_its_article(terminal, monkeypatch):
    terminal(80, 24)
    world = _world_with_seed(42)
    monkeypatch.setattr(vr, "landmark_available_here", lambda world: True)
    world.landmark["label"] = "the Silent Cathedral"
    entries = vr.deck_service_entries(world)
    assert entries[-1] == ("L", "Silent Cathedral")
    assert vr.menu_columns([f"[{key}] {label}" for key, label in entries]) == 3


@pytest.mark.parametrize("exchange", [[], ["You hit the Raider for 11 damage.", "The Raider uses attack and hits you for 7 damage."]])
def test_a_fight_leaves_its_picture_in_view_at_80x24(terminal, exchange):
    terminal(80, 24)
    world, pirate = _world_with_pending_fight()
    _add_cargo(world, "food", 3)
    world.save.pilot.credits = 10_000
    tactics = world.save.pending_travel["encounter"]["combat"]["tactics"]
    lines = vr.combat_display_lines(world, pirate, exchange, patrol=False, tactics=tactics)
    rows = _console_rows(lines, "Combat 10,000cr", vr.combat_action_bar("F/G/E/D/P"))
    # The opening page clears the band entirely; a turn's page, two rows taller
    # for its exchange, still shows eight of its ten rows.
    assert rows <= (BAND_TOP + 2 if exchange else BAND_TOP)


def test_the_contact_is_composed_below_a_turns_page(monkeypatch):
    """The lock around the contact starts under the rows a turn's page covers."""
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 24)
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    monkeypatch.setattr(vr, "_OUTPUT_ENCODING", "utf-8")
    monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=True))
    vr._CANOPY_CACHE.clear()
    scene = {"kind": "combat", "sky": "alert", "hull": "Shuttle", "enemy": "raider", "name": "Probe", "seed": "combat:Probe"}
    cells = vr.canopy_cells(scene, 80, 24)
    art = [row for row, line in enumerate(cells) if any(ch not in " ·•+" for ch, _, _ in line)]
    assert art and min(art) >= BAND_TOP + 2
    vr._CANOPY_CACHE.clear()


# --- the combat table ---------------------------------------------------------

def _option(lines: list[str], key: str) -> str:
    return next(plain(row) for row in lines if plain(row).startswith(f"[{key}]"))


@pytest.mark.parametrize("profile", list(vr.TACTICAL_PROFILES))
@pytest.mark.parametrize("step", [0, 1, 2])
@pytest.mark.parametrize("action", ["F", "G"])
def test_the_options_table_quotes_the_shot_a_round_resolves(monkeypatch, profile, step, action):
    world = _world_with_seed(42)
    world.save.ship.weapon_tier = 2
    pirate = vr.Pirate("Probe", 2, 400, 400)
    tactics = {"version": 3, "profile": profile, "step": step, "brace_ready": True}
    row = _option(vr.combat_display_lines(world, pirate, [], patrol=False, tactics=tactics), action)
    found = re.search(r"deal (\d+)(?:-(\d+))?", row)
    low, high = int(found[1]), int(found[2] or found[1])
    for roll, quoted in ((9, low), (14, high)):
        fight, target = _world_with_seed(42), vr.Pirate("Probe", 2, 400, 400)
        fight.save.ship.weapon_tier = 2
        monkeypatch.setattr(fight.event_rng, "randint", lambda lo, hi, roll=roll: roll if (lo, hi) == (9, 14) else lo)
        dealt, _, _ = vr.tactical_round(fight, target, dict(tactics), action)
        assert dealt == quoted


def test_the_options_table_says_when_a_shot_ends_it_and_when_guard_is_spent():
    world = _world_with_seed(42)
    weak = vr.Pirate("Probe", 0, 5, 20)
    tactics = {"version": 3, "profile": "Raider", "step": 0, "brace_ready": False}
    lines = vr.combat_display_lines(world, weak, [], patrol=False, tactics=tactics)
    assert "destroys it" in _option(lines, "F")
    rows = [plain(row) for row in lines]
    assert not any(row.startswith("[G]") for row in rows)
    assert any(row.startswith("Guard") and "recharging" in row and "fire once first" in row for row in rows)


def test_every_option_names_its_price_on_its_own_row():
    world = _world_with_seed(42)
    _set_cargo(world, {"food": 6})
    world.save.pilot.credits = 10_000
    pirate = vr.Pirate("Probe", 1, 50, 50)
    tactics = vr.new_tactics(pirate)
    lines = vr.combat_display_lines(world, pirate, [], patrol=False, tactics=tactics)
    intent = next(plain(row) for row in lines if " incoming " in plain(row))
    incoming = re.search(r"incoming (\S+)", intent)[1]
    assert f"take {incoming}" in _option(lines, "F")
    for key in "ED":
        assert re.search(r"\d+% escape", _option(lines, key)) and f"else take {incoming}" in _option(lines, key)
    assert "1 unit overboard" in _option(lines, "D")  # an eighth of six units, one at least
    bribe = _option(lines, "P")
    assert f"{vr.bribe_cost(pirate)}cr" in bribe and "Blackwake +2" in bribe and f"else take {incoming}" in bribe
    # The cycle is on the row with the move it makes next picked out of it.
    steps = vr.tactical_steps(tactics)
    assert intent.startswith(f"{steps[0].upper()} > {steps[1]} > {steps[2]}")
    # Whose pattern it is, is on the contact's own row beside its tier.
    assert any(plain(row).startswith("Probe") and plain(row).endswith(f"tier 1 {tactics['profile']}") for row in lines)
