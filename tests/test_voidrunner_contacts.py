"""Concord, Blackwake and the Archive are one deck entry (review).

The deck listed three people as three services among fourteen. These pin the
one entry that replaced them: what its page says of each contact in every
state, that its three rows are a table at 80 columns, that each key opens the
page it names and Back comes home, and that the deck is twelve services in
four rows.
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


def _rows(world) -> dict[str, str]:
    rows = [" ".join(plain(line).split()) for line in vr.contact_lines(world)]
    return {row[1]: row for row in rows if row.startswith("[")}


def test_the_page_says_where_the_pilot_stands_with_each_faction(terminal):
    terminal(80, 24)
    world = _world_with_seed(42)
    rows = _rows(world)
    assert rows["P"] == "[P] Commander Edda Ro Concord Navy 0 of 75 standing"
    assert rows["W"] == "[W] Broker Rook Talan Blackwake Cartel 0 of 75 standing"
    world.save.pilot.reputation[vr.FACTION_CONCORD] = vr.CONCORD_COMMISSION_THRESHOLD
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = -12
    rows = _rows(world)
    assert rows["P"].endswith("75 of 75 standing, enough to join") and rows["W"].endswith("-12 of 75 standing")
    world.save.pilot.has_concord_commission = True
    assert _rows(world)["P"].endswith("member, perk active")
    world.save.pilot.reputation[vr.FACTION_CONCORD] = vr.FACTION_PERK_SUSPEND_AT
    assert _rows(world)["P"].endswith("member, perk suspended")


def test_the_page_says_what_maras_assignment_wants_and_how_full_the_files_are(terminal):
    terminal(80, 24)
    world = _world_with_seed(42)
    assert _rows(world)["A"] == "[A] Mara Venn Freeport Archive assignment open; files 0/18"
    world.save.flags["archive_v1_started"] = True
    world.save.tales = {"Hollow Reach": 3, "Outer Fringe": 1}
    assert _rows(world)["A"].endswith("record to recover; files 4/18")
    world.save.flags["archive_v1_recovered"] = True
    assert _rows(world)["A"].endswith("record aboard; files 4/18")
    world.save.flags["archive_v1_public"] = True
    assert _rows(world)["A"].endswith("assignment complete; files 4/18")


@pytest.mark.parametrize("width,height", [(40, 12), (64, 20), (80, 24), (160, 48)])
def test_the_page_fits_and_is_a_table_from_80_columns(terminal, width, height):
    terminal(width, height)
    world = _world_with_seed(42)
    # The longest thing each row can say.
    world.save.pilot.reputation[vr.FACTION_CONCORD] = vr.CONCORD_COMMISSION_THRESHOLD
    world.save.pilot.has_blackwake_made = True
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = vr.FACTION_PERK_SUSPEND_AT
    world.save.flags["archive_v1_public"] = True
    world.save.tales = {name: 3 for name in vr.SECTOR_NAMES}
    lines = vr.contact_lines(world)
    pages = vr._service_pages(lines, "Contacts", "[P] Concord [W] Blackwake [A] Archive [B] Back [<>] Page: ")
    assert all(vr._visible_width(row) <= vr._page_content_width() for page in pages for row in page)
    if width >= 80:
        rows = [plain(line) for line in lines if plain(line).startswith("[")]
        # Three aligned columns: the organisation and the standing each start in one column.
        assert len({row.index(word) for row, word in zip(rows, ("Concord Navy", "Blackwake Cartel", "Freeport Archive"))}) == 1
        assert len({row.index(word) for row, word in zip(rows, ("75 of 75", "member,", "assignment"))}) == 1
        assert len(pages) == 1


def test_each_key_opens_the_page_it_names_and_back_comes_home(monkeypatch):
    world = _world_with_seed(42)
    opened = []
    for name in ("screen_concord_commission", "screen_blackwake_made", "screen_archive"):
        monkeypatch.setattr(vr, name, lambda p, w, name=name: opened.append(name))
    keys = iter(["P", "W", "A", "X", "B"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_contacts(vr.Palette(False), world)
    assert opened == ["screen_concord_commission", "screen_blackwake_made", "screen_archive"]
    assert "[P] Concord [W] Blackwake [A] Archive [B] Back" in " ".join(plain(output.getvalue()).split())
    assert set(vr.CONTACT_KEYS) == {"P", "W", "A"}


def test_the_deck_is_twelve_services_in_four_rows(terminal):
    terminal(80, 24)
    world = _world_with_seed(42)
    world.save.turn = 3
    entries = vr.deck_service_entries(world)
    keys = [key for key, _ in entries]
    assert ("N", "Contacts") in entries and "P" not in keys and "W" not in keys
    # Thirteen with the dispatch on a board that has one; twelve without.
    assert len(entries) - ("J" in keys) == 11 and len(vr.menu_grid(entries)) == 4
    # The deck still answers [P] and [W], for fingers that know them.
    for key in "PWN":
        typed = iter([key])
        with contextlib.redirect_stdout(io.StringIO()):
            vr.read_key, kept = (lambda: next(typed)), vr.read_key
            try:
                assert vr.screen_station_menu(vr.Palette(False), world) == key
            finally:
                vr.read_key = kept
