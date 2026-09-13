"""The presentation contract: the tests that can fail because a screen is grey.

Issue #493's acceptance criteria, one test apiece. They exist because the suite
that watched Voidrunner's entire visual design disappear could only ever assert
that a screen *fits* -- row counts, border widths, "every term survives the page
break" -- all of which stayed true of a wall of unstyled text. These assert what
a caller sees instead: that colour reaches the body at all, that a hotkey is not
the colour of a label, that a column starts where the column above it started,
that every preset renders deliberately, and that motion is optional and skippable.
"""

from __future__ import annotations

import contextlib
import io
import re
import time

import pytest

from .support import _world_with_pending_fight, _world_with_seed, plain, vr

# Every size the game supports, and the presets it supports them in.
SIZES = [(80, 24), (64, 20), (40, 12)]
PRESETS = list(vr.DISPLAY_STYLES)

# The Unicode half of the glyph vocabulary (issue #493 §4). None of it may reach
# a `plain` terminal: every one has an ASCII substitute, and a preset that lets
# one through is not a designed rendering, it is an accident.
UNICODE_GLYPHS = "".join(unicode_form for unicode_form, _ in vr._GLYPHS.values()) + vr._SPARK[0] + "╭╮╰╯│─├┤═║╔╗╚╝■"

FOREGROUND = re.compile(r"\x1b\[(?:1m\x1b\[)?(?:38;[25];|3[0-7]m|9[0-7]m)")


def body_rows(frame: str) -> list[str]:
    """The rows a page drew inside its frame, styling intact.

    `page_rows` takes the colour off, which is exactly what these tests are
    looking for, so they read the frame for themselves.
    """
    rows = []
    for row in frame.replace("\r\n", "\n").split("\n"):
        bare = vr._ANSI_RE.sub("", row)
        if bare.strip() and bare[0] in "│║|":
            rows.append(row)
    return rows


def _keys(monkeypatch, *presses):
    supply = iter(presses)
    monkeypatch.setattr(vr, "read_key", lambda: next(supply))


def draw_deck(p, world, monkeypatch):
    _keys(monkeypatch, "Q")
    vr.screen_station_menu(p, world)


def draw_market(p, world, monkeypatch):
    _keys(monkeypatch, "Q")
    vr.screen_market(p, world)


def draw_yard(p, world, monkeypatch):
    _keys(monkeypatch, "Q")
    vr.screen_shipyard(p, world)


def draw_record(p, world, monkeypatch):
    _keys(monkeypatch, "B")
    vr.screen_status(p, world)


def draw_board(p, world, monkeypatch):
    _keys(monkeypatch, "B")
    vr.screen_missions(p, world)


def draw_chart(p, world, monkeypatch):
    _keys(monkeypatch, "B")
    vr.screen_chart(p, world)


def draw_crew(p, world, monkeypatch):
    _keys(monkeypatch, "Q")
    vr.screen_crew(p, world)


def draw_display(p, world, monkeypatch):
    _keys(monkeypatch, "B")
    vr.screen_display_options(p, world)


def draw_customs(p, world, monkeypatch):
    from .support import _set_cargo
    _set_cargo(world, {"weapons": 3})
    _keys(monkeypatch, "S")
    vr.screen_customs(p, world)


def draw_combat(p, world, monkeypatch):
    """The fight, drawn through the same path `_screen_combat_session` uses."""
    fighting, pirate = _world_with_pending_fight()
    fighting.save.display_style = world.save.display_style
    tactics = fighting.save.pending_travel["encounter"]["combat"]["tactics"]
    title = f"Combat {fighting.save.pilot.credits:,}cr"
    bar = vr.combat_action_bar("F/G/E/D/P")
    lines = vr.combat_display_lines(fighting, pirate, ["Your shot connects for 9 damage."],
                                    patrol=False, tactics=tactics)
    pages = vr._service_pages(lines, title, bar)
    vr.draw_page(p, title, pages[0], 0, len(pages))


# The six screens issue #493 names in its first acceptance criterion, plus the
# four the rebuild touched hardest.
SCREENS = {
    "deck": draw_deck, "market": draw_market, "yard": draw_yard, "record": draw_record,
    "board": draw_board, "chart": draw_chart, "crew": draw_crew,
    "display": draw_display, "customs": draw_customs, "combat": draw_combat,
}


def render(screen: str, monkeypatch, width: int, height: int, style: str = "auto") -> str:
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
    monkeypatch.setattr(vr, "_LAST_PAGE_DRAWN", None)
    palette = vr.Palette(truecolor=True)
    monkeypatch.setattr(vr, "_PALETTE", palette)
    world = _world_with_seed(493)
    world.save.pilot.credits = 18_420
    world.save.turn = 12
    world.save.ship.hull_hp = 48
    world.save.ship.fuel = 17
    world.save.display_style = style
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        with contextlib.suppress(StopIteration):
            SCREENS[screen](palette, world, monkeypatch)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 1. Colour reaches the body.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("width,height", [(80, 24), (40, 12)])
@pytest.mark.parametrize("screen", sorted(SCREENS))
def test_every_body_row_carries_colour(monkeypatch, screen, width, height):
    """*This is the test that would have caught the whole regression.*

    `wrapped_group` wrapped every body row of every paged screen through
    `_mission_plain` -- `ANSI.sub("")` -- so the frame was the only styled thing
    on a page and no screen could be coloured even if it tried. Nothing in the
    old suite could fail because of that, which is why it survived two releases.
    """
    frame = render(screen, monkeypatch, width, height)
    rows = body_rows(frame)
    assert rows, f"{screen}: nothing drawn at {width}x{height}"
    colourless = [vr._ANSI_RE.sub("", row) for row in rows if not FOREGROUND.search(row)]
    assert not colourless, f"{screen} at {width}x{height}: uncoloured body rows {colourless}"


# ---------------------------------------------------------------------------
# 2. Roles are distinct.
# ---------------------------------------------------------------------------


def test_the_palette_roles_are_four_different_colours():
    palette = vr.Palette(truecolor=True)
    roles = {"hotkey": palette.gold, "label": palette.slate, "value": palette.ink, "frame": palette.hull}
    assert len(set(roles.values())) == len(roles), f"roles share a colour: {roles}"


@pytest.mark.parametrize("screen", ["deck", "market", "yard", "chart"])
def test_a_screen_uses_the_hotkey_label_and_value_roles_at_once(monkeypatch, screen):
    """Chrome is never the colour of content, and a hotkey is never the colour
    of the label beside it. Asserted on the drawn screen, not eyeballed."""
    frame = render(screen, monkeypatch, 80, 24)
    palette = vr.Palette(truecolor=True)
    for role, sequence in (("hotkey", palette.gold), ("label", palette.slate),
                           ("value", palette.ink), ("frame", palette.hull)):
        assert sequence in frame, f"{screen}: nothing is drawn in the {role} role"


# ---------------------------------------------------------------------------
# 3. Columns align.
# ---------------------------------------------------------------------------


def _runs(row: str) -> list[tuple[int, int]]:
    """(start, end) display columns of every run of text on a plain row."""
    spans, column = [], 0
    for match in re.finditer(r"\S+(?: \S+)*", row):
        start = vr._visible_width(row[:match.start()])
        spans.append((start, start + vr._visible_width(match.group(0))))
        column = match.end()
    return spans


def test_table_columns_start_and_end_where_the_column_above_them_did():
    """The contract `table` exists to keep: a left-aligned column starts on the
    same display column on every row, a right-aligned one ends on it."""
    vr._OUTPUT_WIDTH, vr._OUTPUT_HEIGHT = 80, 24
    rows = [["A", "1", "short"], ["BBBBBBBB", "22222", "a much longer cell"], ["CC", "333", "mid"]]
    drawn = [plain(row) for row in vr.table(["LEFT", "RIGHT", "TAIL"], rows, "lrl")]
    starts = {vr._visible_width(row) - vr._visible_width(row.lstrip()) for row in drawn}
    assert starts == {0}
    # The right-aligned column ends on one column, whatever the reading is.
    ends = {_runs(row)[1][1] for row in drawn[1:]}
    assert len(ends) == 1, f"right-aligned column ragged: {ends}"
    tails = {_runs(row)[2][0] for row in drawn[1:]}
    assert len(tails) == 1, f"third column starts in three places: {tails}"


def test_a_tables_headings_are_on_every_page_of_that_table(monkeypatch):
    """A caller who pages into the second half of the market is reading
    unlabelled numbers otherwise -- and the marks that say which rows are a
    table's are zero-width control characters, so anything sweeping controls off
    a row has to leave them alone."""
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 40)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 12)
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=True))
    world = _world_with_seed(493)
    lines = vr.market_catalog_lines(world, vr.LEGAL_COMMODITIES)
    pages = vr._service_pages(lines, "Market: 1,200cr", "[<] Prev [>] Next [B] Back: ")
    assert len(pages) > 1, "the market did not page at 40x12"
    for rows in pages:
        text = " ".join(plain(row) for row in rows)
        if not re.search(r"\[[A-Z]\] \w", text):
            continue  # a page of footnotes is not a page of the table
        assert "COMMODITY" in text and "BUY" in text, text


def test_a_stacked_table_keeps_every_column_and_still_fits(monkeypatch):
    """Stacking is a change of shape, not a smaller table. When one tail row is
    still too wide it runs on to a second, rather than dropping a figure or
    overflowing the frame (issue #493 review)."""
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 40)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 12)
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=True))
    entries = [{"user_id": number, "handle": f"Pilot-{number:02}",
                "best_credits": 1_000_000 - number, "rank": vr.RANKS[-1][1],
                "kills": 100 + number, "missions_completed": 200 + number,
                "retirements": number} for number in (1, 2)]
    rows = [part for line in vr.hall_of_fame_lines(entries, 2)
            for part in plain(line).split("\n")]
    for entry in entries:
        together = " ".join(rows)
        for figure in (entry["handle"], entry["rank"], f"{entry['best_credits']:,}cr",
                       str(entry["kills"]), str(entry["missions_completed"])):
            assert figure in together, f"{figure} was dropped rather than stacked"
    for row in rows:
        if row.startswith(" "):  # a record's own rows; the notes are prose and wrap
            assert vr._visible_width(row) <= vr._page_content_width(), repr(row)


def test_a_heading_or_a_rule_is_never_the_last_row_of_a_page(monkeypatch):
    """A rule or a set of column headings names what follows it, so it is never
    left at the foot of a page with the rows it names on the next one."""
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 40)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 12)
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=True))
    world = _world_with_seed(493)
    world.save.pilot.credits = 18_420
    fighting, pirate = _world_with_pending_fight()
    tactics = fighting.save.pending_travel["encounter"]["combat"]["tactics"]
    combat = vr.combat_display_lines(fighting, pirate, ["Your shot connects."],
                                     patrol=False, tactics=tactics)
    for lines, title in ((vr.shipyard_lines(world), "Engineering Yard"),
                         (vr.station_deck_lines(world), "Command Deck"),
                         (vr.crew_roster_lines(world), "Crew Roster"),
                         (combat, "Combat 1,200cr")):
        pages = vr._service_pages(lines, title, "[<] Prev [>] Next [B] Back: ")
        for number, rows in enumerate(pages, 1):
            assert rows, f"{title}: empty page {number}"
            assert any(row[:1] not in (vr.SECTION_MARK, vr.STICKY_MARK) for row in rows), \
                f"{title} page {number} is nothing but headings"
            if number < len(pages):
                assert rows[-1][:1] not in (vr.SECTION_MARK, vr.STICKY_MARK), \
                    f"{title} page {number} ends on a heading: {[plain(row) for row in rows]}"
        for rows in pages:
            assert len(rows) == len(set(rows)) or not any(
                row[:1] == vr.STICKY_MARK for row in rows), "a heading was drawn twice"


def test_every_key_the_deck_advertises_is_a_key_the_deck_answers(monkeypatch):
    """The dispatch is the authority on a hotkey, not the row that names it: an
    advertised key the deck does not accept only redraws (issue #493 review)."""
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 24)
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=True))
    world = _world_with_seed(493)
    world.save.ship.has_gunner = True          # a crew alert, so `[K]` is offered
    world.save.ship.hull_hp = 1                # a repair alert, so `[Y]` is
    from .support import _set_cargo
    _set_cargo(world, {"weapons": 2})          # contraband, so `[D]` is
    advertised = set(re.findall(r"\[([A-Z])\]", plain(" ".join(vr.station_deck_lines(world)))))
    assert {"K", "Y", "D"} <= advertised
    for key in sorted(advertised):
        keys = iter([key, "Q"])
        monkeypatch.setattr(vr, "read_key", lambda: next(keys))
        with contextlib.redirect_stdout(io.StringIO()):
            answered = vr.screen_station_menu(vr.Palette(truecolor=False), world)
        assert answered == key, f"the deck advertises [{key}] and does not answer it"


@pytest.mark.parametrize("width,height", SIZES)
def test_the_market_prices_line_up_on_every_row(monkeypatch, width, height):
    frame = render("market", monkeypatch, width, height)
    ends = set()
    for row in body_rows(frame):
        bare = plain(row)
        match = re.search(r"\[[A-Z]\] \S+(?: \S+)*?\s+(\d+)\s", bare)
        if match:
            ends.add(vr._visible_width(bare[:match.end(1)]))
    assert len(ends) == 1, f"the buy column lands in {len(ends)} places at {width}x{height}: {ends}"


# ---------------------------------------------------------------------------
# 4. It still fits, and 5. every preset is deliberate.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("style", PRESETS)
@pytest.mark.parametrize("width,height", SIZES)
@pytest.mark.parametrize("screen", sorted(SCREENS))
def test_every_screen_renders_legibly_in_every_preset(monkeypatch, screen, width, height, style):
    frame = render(screen, monkeypatch, width, height, style)
    lines = [vr._ANSI_RE.sub("", line) for line in frame.split("\r\n")]
    assert any(line.strip() for line in lines), f"{screen}/{style}: nothing drawn"
    for line in lines:
        assert vr._visible_width(line) <= width, f"{screen}/{style} at {width}: {line!r}"
    borders = {len(line) for line in lines if line.strip()[:1] in ("╭", "├", "╰", "+")}
    assert len(borders) <= 1, f"{screen}/{style}: inconsistent border widths {borders}"
    for line in lines:
        if line.strip()[:1] in ("│", "|"):
            assert len(line) == max(borders), f"{screen}/{style}: ragged row {line!r}"
    if style in ("mono", "plain"):
        assert not vr._ANSI_RE.search(frame), f"{screen}/{style} is styled"
    if style == "plain":
        left = sorted(set(frame) & set(UNICODE_GLYPHS))
        assert not left, f"{screen}/plain kept Unicode artwork: {left}"
        assert frame.isascii() or "█" not in frame


# ---------------------------------------------------------------------------
# 6. Motion is skippable and optional.
# ---------------------------------------------------------------------------


def test_the_colourless_presets_preview_themselves_colourlessly(monkeypatch):
    """Each preset previews itself, so the `mono` and `plain` samples have to
    arrive with no colour -- and a table colours any cell that has none of its
    own, which would have made those two previews a lie (issue #493 review)."""
    frame = render("display", monkeypatch, 80, 24, "auto")
    rows = [row for row in body_rows(frame) if "30/60" in vr._ANSI_RE.sub("", row)]
    assert len(rows) == len(PRESETS), rows
    def sample(row: str) -> str:
        """The preview itself: from its gauge to its last reading, with the
        frame's own right-hand border -- which is always hull-coloured -- left
        outside."""
        start = min((row.index(glyph) for glyph in ("█", "░", "#", ".") if glyph in row),
                    default=0)
        return row[start:row.rindex("12") + 2]

    samples = [sample(row) for row in rows]
    # auto, fast and basic show their colours; mono and plain show none.
    assert [bool(FOREGROUND.search(text)) for text in samples] == \
        [True, True, True, False, False], [vr._ANSI_RE.sub("", text) for text in samples]


@pytest.mark.parametrize("style,expected", [("auto", True), ("basic", True),
                                            ("fast", False), ("mono", False), ("plain", False)])
def test_motion_is_off_in_the_presets_that_exist_because_a_caller_wants_less(monkeypatch, style, expected):
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
    assert vr.motion_enabled() is expected


def test_motion_is_skipped_when_nothing_is_watching(monkeypatch):
    """No live input reader means a scripted caller, and an effect nobody can
    see is only a delay -- which is why the suite is not slower for this."""
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    monkeypatch.setattr(vr, "_INPUT_READER", None)
    assert vr.motion_interrupted() is True
    assert vr.motion_pause(5.0) is False


def test_a_keypress_already_waiting_ends_the_effect(monkeypatch):
    class Pressed:
        def waiting(self):
            return True

    class Reader:
        read_byte = Pressed()

    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    monkeypatch.setattr(vr, "_StdioBytes", Pressed)
    monkeypatch.setattr(vr, "_INPUT_READER", Reader())
    assert vr.motion_interrupted() is True
    started = time.monotonic()
    assert vr.motion_pause(5.0) is False
    assert time.monotonic() - started < 1.0, "a skipped effect still waited"


@pytest.mark.parametrize("intruder", ["\x1b[2J", "\x1b[10;10H", "\x1b(0", "\x07", "\x1b"])
def test_an_escape_inside_data_never_reaches_the_terminal(monkeypatch, intruder):
    """A row that carries an escape the game did not write is not "already
    styled" -- it is data with a terminal control in it. A score file's callsign
    is another pilot's text, and an accepted `\\x1b[2J` would clear the screen
    (issue #493 review)."""
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=True))
    def leftovers(row: str) -> str:
        """What is on the row once the game's own colour is taken off."""
        return vr.ANSI_ESCAPE_RE.sub("", row)

    styled = vr.style_body_line(f"Pilot {intruder}Nine: 12 victories")
    # Every escape left is one of ours, and every one of ours is a colour.
    assert all(sequence.endswith("m") for sequence in vr.ANSI_ESCAPE_RE.findall(styled))
    assert "\x1b" not in leftovers(styled) and "\x07" not in leftovers(styled)
    assert "Pilot Nine" in leftovers(styled)
    for row in vr.wrapped_group(f"Pilot {intruder}Nine"):
        assert all(sequence.endswith("m") for sequence in vr.ANSI_ESCAPE_RE.findall(row))
        assert "\x1b" not in leftovers(row)


def test_the_next_rank_follows_the_rank_the_career_kept(monkeypatch):
    """Rank is permanent here, and the record says so two rows further down. A
    progress row chosen from the *balance* told a Void Baron who had spent down
    to 1,200cr that they needed 3,800cr to become an Independent Trader, which
    is a rank they already hold (issue #493 review)."""
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 24)
    world = _world_with_seed(493)
    world.save.pilot.highest_rank_seen = 3          # Void Baron
    world.save.pilot.credits = 1_200                # spent down to a rookie balance
    text = plain(" ".join(vr.pilot_record_lines(world)))
    assert "Rank Void Baron" in text
    assert f"to {vr.RANKS[4][1]}" in text, text
    for lower in vr.RANKS[1:4]:
        assert f"to {lower[1]}" not in text


def test_drawing_a_page_twice_over_reveals_it_once(monkeypatch):
    """A reveal belongs to arriving somewhere. A key that changed nothing
    redraws the same page, and must cost the caller nothing."""
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 24)
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    monkeypatch.setattr(vr, "_LAST_PAGE_DRAWN", None)
    paused = []
    monkeypatch.setattr(vr, "motion_interrupted", lambda: False)
    monkeypatch.setattr(vr, "motion_pause", lambda seconds: paused.append(seconds) or True)
    palette = vr.Palette(truecolor=False)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.draw_page(palette, "Deck", ["one", "two", "three"], 0, 2)
        first = len(paused)
        vr.draw_page(palette, "Deck", ["one", "two", "three"], 0, 2)
        again = len(paused)
        vr.draw_page(palette, "Deck", ["four", "five"], 1, 2)
    assert first == 3, "the first draw did not reveal its rows"
    assert again == first, "an unchanged redraw revealed itself again"
    assert len(paused) > again, "paging to a new page did not reveal it"
    assert sum(paused) <= vr.MOTION_REVEAL_BUDGET * 2 + 1e-9


# -- in-place redraw, service labels and the label hue ---------------------


def test_a_screen_replaces_the_one_before_it(monkeypatch):
    """A door owns the terminal, so a new screen clears rather than scrolls.

    Voidrunner printed every screen underneath its predecessor, so a session was
    one long scroll and a caller's history filled with superseded copies of the
    command deck (issue #516). War Dialer has always cleared.
    """
    written: list[str] = []
    monkeypatch.setattr(vr, "out", written.append)
    vr.draw_page(vr.pal(), "TEST", ["a row"], 0, 1)
    assert "".join(written).startswith("\x1b[2J\x1b[H")


def test_every_paged_screen_goes_through_the_one_clearing_path():
    """One choke point, not a clear sprinkled per screen -- which is how both
    games lost their presentation to slices that each looked fine."""
    source = vr.__file__ and open(vr.__file__, encoding="utf-8").read()
    assert source.count("[2J") == 1, "the clear belongs in clear_screen() alone"


@pytest.mark.parametrize("label,count,noun", [
    ("Market: 7 goods", "7", " goods"),
    ("Board: 4 offers", "4", " offers"),
    ("Chart: 1 link", "1", " link"),
])
def test_a_preview_count_is_styled_as_the_value_it_is(label, count, noun):
    """`Board: 4 offers` is a label and a value, not one phrase: colouring the
    whole entry alike buried the number a caller scans the menu for (#518)."""
    p = vr.pal()
    drawn = vr._menu_entry("B", label)
    runs = dict((text, esc) for esc, text in re.findall(r"(\x1b\[[0-9;]*m)([^\x1b]*)", drawn) if text)
    assert runs.get(count) == p.ink, drawn
    assert runs.get(noun) == p.slate, drawn


def test_an_entry_with_no_count_stays_a_value():
    p = vr.pal()
    drawn = vr._menu_entry("S", "Status")
    assert p.ink in drawn and "Status" in plain(drawn)


def test_a_service_label_reads_as_a_label_not_a_sentence():
    """`Board 4 offers` parses as subject "Board 4", verb "offers"."""
    world = _world_with_seed(7)
    entries = dict((key, label) for key, label in vr.deck_service_entries(world))
    for key in ("M", "B", "C"):
        assert ": " in entries[key], entries[key]
    # And counts agree with their noun: `Chart 1 links` was a real screen.
    for label in entries.values():
        match = re.match(r"^[^:]+: (\d+) (\w+?)(s?)$", label)
        if match:
            number, _noun, plural_s = match.groups()
            assert (number != "1") == bool(plural_s), label


def test_labels_are_off_the_cockpits_own_hue():
    """Labels were `#7f8fae`, a desaturated blue on a blue cockpit (#519), so a
    label read as the same colour one shade down rather than a different kind
    of thing."""
    p = vr.Palette(truecolor=True)
    rgb = re.search(r"38;2;(\d+);(\d+);(\d+)", p.slate)
    red, green, blue = (int(value) for value in rgb.groups())
    assert not (blue > red and blue >= green), (red, green, blue)


def test_every_palette_role_stays_distinct_in_both_depths():
    p = vr.Palette(truecolor=True)
    roles = ["hull", "deep", "plasma", "gold", "ink", "slate", "mint", "amber", "alarm"]
    truecolour = [getattr(p, role) for role in roles]
    assert len(set(truecolour)) == len(roles)
    indexed = [getattr(vr.Palette(truecolor=False), role) for role in roles]
    assert len(set(indexed)) == len(roles)


# ---------------------------------------------------------------------------
# Issue #532: the action bar, and values that were declared as labels.
# ---------------------------------------------------------------------------


def _role_runs(drawn: str) -> dict[str, str]:
    """Each visible run of text in a styled row, mapped to the colour in force.

    The colour, not merely the last escape before the text: every component
    writes its role and then `BOLD`, so "the sequence immediately preceding" is
    the bold one on exactly the tokens these tests care most about.
    """
    runs, colour, cursor = {}, "", 0
    for match in re.finditer(r"\x1b\[[0-9;]*m", drawn):
        text = drawn[cursor:match.start()]
        if text:
            runs[text] = colour
        body = match.group(0)[2:-1]
        if body in ("", "0"):
            colour = ""
        elif body.startswith("38;"):
            colour = match.group(0)
        cursor = match.end()
    if drawn[cursor:]:
        runs[drawn[cursor:]] = colour
    return runs


def _role_of(drawn: str, word: str) -> str | None:
    """The SGR in force over the run whose text is exactly `word`."""
    for text, esc in _role_runs(drawn).items():
        if text.strip() == word:
            return esc
    return None


def _at(monkeypatch, width: int = 80, height: int = 24):
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=True))
    return vr.pal()


def test_the_action_bar_wears_the_same_roles_as_the_menu_above_it(monkeypatch):
    """It was the one row on a Voidrunner screen with no palette role at all:
    the same `[K] Label` one row higher, inside the box, came out with gold
    keys, while outside it was the terminal's default foreground (#532)."""
    p = _at(monkeypatch)
    runs = _role_runs(vr.style_action_bar("[O] Pilot [C] Jobs [B] Back: "))
    for key in ("[O]", "[C]", "[B]"):
        assert runs.get(key) == p.gold, runs
    for label in (" Pilot ", " Jobs ", " Back: "):
        assert runs.get(label) == p.ink, runs


def test_a_prompt_that_styles_itself_is_left_alone(monkeypatch):
    """The rule `style_body_line` already follows: a component's decision beats
    a pattern's guess, and thirty call sites print a prompt."""
    p = _at(monkeypatch)
    composed = f"{p.slate}Quantity (max {p.gold}12{p.slate}): {vr.RESET}"
    assert vr.style_action_bar(composed) == composed


def test_every_prompt_the_game_prints_carries_a_role(monkeypatch):
    """No screen may end in a row of default-foreground text."""
    _at(monkeypatch)
    written = []
    monkeypatch.setattr(vr, "out", lambda text: written.append(text))
    vr.out_prompt("[<] Prev [>] Next [B] Back: ")
    assert written and FOREGROUND.search(written[0]), written


def test_a_body_row_is_still_read_as_prose(monkeypatch):
    """`key_labels` is the action bar's rule, not every row's: `Press [B] to go
    back` is a sentence with a key in it, not a key with a label after it."""
    p = _at(monkeypatch)
    runs = _role_runs(vr.style_body_line("Press [B] to go back."))
    assert runs.get("[B]") == p.gold, runs
    assert runs.get(" to go back.") == p.slate, runs


def test_a_tables_headings_are_not_the_colour_of_its_labels(monkeypatch):
    """`PILOT RANK BEST CR` was styled `label`, so a table's headings were the
    colour of the sentence above them and the footnote below them, and the
    table never announced itself as one (#532)."""
    p = _at(monkeypatch)
    heading = vr.table(["PILOT", "RANK"], [["Thiesi", "Rookie"]], "ll",
                       styles=[["label", "value"]])[0]
    assert _role_of(heading, "PILOT") == p.hull, heading
    assert p.hull != p.slate


def test_no_cockpit_row_shows_its_label_and_its_value_in_one_role(monkeypatch):
    """`HULL ... 60/60 Intact` declared its fourth column `label`, so the row's
    own value came out the colour of the row's label (#532)."""
    p = _at(monkeypatch)
    drawn = "".join(vr.ship_gauge_rows(_world_with_seed(7)))
    for value in ("Intact", "Shuttle"):
        assert _role_of(drawn, value) == p.ink, (value, plain(drawn))
    assert _role_of(drawn, "HULL") == p.slate, plain(drawn)


def test_a_neutral_standing_is_a_value_and_its_bar_is_not(monkeypatch):
    """Absent severity means *value*, not *label* -- but the bar carrying the
    same absence is chrome and must not turn ink with it (#532)."""
    p = _at(monkeypatch)
    rows = vr.pilot_record_lines(_world_with_seed(7), "O")
    standing = [row for row in rows if "Neutral" in plain(row)]
    assert standing, [plain(row) for row in rows]
    for row in standing:
        assert _role_of(row, "Neutral") == p.ink, plain(row)
        bars = [text for text, esc in _role_runs(row).items()
                if text and set(text) <= {"█", "░"}]
        assert bars, plain(row)
        assert all(_role_runs(row)[bar] != p.ink for bar in bars), plain(row)


def test_an_empty_crew_is_still_a_value(monkeypatch):
    """`Crew none` had the label and the value in one colour: the populated
    branch was `ink` and the empty one had drifted to the label role, so the
    row said nothing about which half was which (#532)."""
    p = _at(monkeypatch)
    world = _world_with_seed(7)
    rows = vr.pilot_record_lines(world, "O")
    crew = [row for row in rows if plain(row).strip().startswith("Crew ")]
    assert crew, [plain(row) for row in rows]
    assert _role_of(crew[0], "Crew") == p.slate, plain(crew[0])
    assert _role_of(crew[0], "none") == p.ink, plain(crew[0])


def test_the_ledger_names_its_groups_instead_of_hyphenating_them(monkeypatch):
    """`HOLD -`, `TRAVEL -` and `LOCAL MARKET -` were headings wearing a hyphen
    in the middle of a paragraph; `section` is what the game says instead."""
    _at(monkeypatch)
    lines = vr.trading_ledger_lines(_world_with_seed(7))
    rules = [line[1:] for line in lines if line.startswith(vr.SECTION_MARK)]
    assert rules == ["MARGINS", "HOLD", "TRAVEL", "LOCAL MARKET"], rules
    for stale in ("HOLD - ", "TRAVEL - ", "LOCAL MARKET - "):
        assert not any(stale in plain(line) for line in lines), stale


@pytest.mark.parametrize("width,height", SIZES)
def test_every_ledger_figure_survives_every_width(monkeypatch, width, height):
    """Each spend row carries exactly one figure, so a dropped column is a row
    with nothing on it -- which a first cut of this table did at forty columns
    to `Cargo lost or surrendered`. Nothing there is optional; it stacks."""
    _at(monkeypatch, width, height)
    lines = vr.trading_ledger_lines(_world_with_seed(7))
    text = plain("\n".join(lines))
    spend = ("Cargo lost or surrendered", "Fuel purchases", "Crew wages paid",
             "Cancelled-order fees", "Workshop installations", "Workshop materials")
    for label in spend:
        assert label in text, (label, width)
    # Every one of them still carries its figure: six labels, six amounts.
    tail = text[text.index(spend[0]):]
    assert tail.count("cr") >= len(spend), tail


def test_the_hold_says_its_cost_is_the_whole_holdings(monkeypatch):
    """The prose this table replaced suffixed the figure with "total", and the
    figure is the sum over every lot. Without the qualifier a caller comparing
    it against a per-unit market price misreads a multi-lot holding (#532)."""
    _at(monkeypatch)
    world = _world_with_seed(7)
    world.save.cargo = {"machinery": 5}
    world.save.cargo_basis = {"machinery": [[5, 1250]]}
    text = " ".join(" ".join(plain(line) for line in vr.trading_ledger_lines(world)).split())
    assert "TOTAL COST" in text, text
    assert "1,250cr" in text, text


@pytest.mark.parametrize("width,height", SIZES)
def test_no_ledger_page_opens_with_an_orphaned_figure(monkeypatch, width, height):
    """A stacked record is a label row plus an indented figure row, and the two
    are one fact. Paged row-at-a-time, four of the six spend records split at
    40x12 -- a page ending `Cargo lost or surrendered` and the next opening
    with an unlabelled `0cr recorded cost` (#532 review).

    Asserted on the rendered pages rather than on the record strings, because
    pagination re-wraps: what a caller sees is that no page *starts* with a
    continuation. A table's own headings are indented too and are allowed to
    open a page -- that is what `sticky` means, and they name the rows under
    them rather than dangling from the page before.
    """
    _at(monkeypatch, width, height)
    world = _world_with_seed(7)
    world.save.cargo = {"machinery": 5}
    world.save.cargo_basis = {"machinery": [[5, 1250]]}
    footer = "[O] Opportunities [R] Route [M] Markets [N] Next [P] Prev [B] Back: "
    pages = vr._service_pages(vr.trading_ledger_lines(world), "Trading Ledger", footer)
    assert len(pages) > 1 or width >= 80, (width, len(pages))
    for number, page in enumerate(pages[1:], 2):
        first = next((row for row in page if plain(row).strip()), "")
        if vr.STICKY_MARK in first:
            continue
        assert not plain(first).startswith(" "), (
            width, number, plain(first), [plain(row) for row in pages[number - 2]])


def test_the_hall_of_fame_views_are_a_menu_not_a_sentence(monkeypatch):
    """As prose, `Completed` matched the good-tone severity pattern, so one
    view name in the list came out green for no reason (#532)."""
    p = _at(monkeypatch)
    rows = vr.hall_view_rows()
    assert rows[0] == vr.SECTION_MARK + "VIEWS", rows[0]
    drawn = "".join(rows[1:])
    for number, label in enumerate(vr.SCORE_CATEGORIES.values(), 1):
        assert _role_runs(drawn).get(f"[{number}]") == p.gold, drawn
        assert label in plain(drawn), label
    # The one that used to go green, and the role that made it.
    assert p.mint not in drawn, drawn
    assert p.mint in vr.style_body_line("Views: [5] Completed careers."), (
        "the pattern still means what it means; the menu simply no longer asks it")
