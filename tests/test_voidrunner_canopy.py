"""The canopy and the presentation built on it (Thiesi/Voidrunner#30).

Every paged screen is the console on a picture of where the ship is. These
tests pin what that picture may and may not do: be drawn only where someone can
see it, never touch a terminal's bottom-right cell, leave the page row-for-row
what it was, cost nothing on a page turn but the rows it gave back, and never
show anything about the galaxy the grid would not.
"""

from __future__ import annotations

import contextlib
import io
import re

import pytest

from .support import _world_with_pending_fight, _world_with_seed, plain, vr

CUP = re.compile(r"\x1b\[(\d+);(\d+)H")


@pytest.fixture
def canopy(monkeypatch):
    """A live full-palette truecolor terminal, as far as the canopy can tell."""
    def open_(width=80, height=24, *, style="auto", truecolor=True, world=None):
        monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
        monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
        monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
        monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=truecolor))
        monkeypatch.setattr(vr, "_live_terminal", lambda: True)
        monkeypatch.setattr(vr, "motion_interrupted", lambda: True)
        monkeypatch.setattr(vr, "_CANOPY_WORLD", world)
        monkeypatch.setattr(vr, "_CANOPY_SHOWN", None)
        monkeypatch.setattr(vr, "_CANOPY_STRIP_WIDTH", None)
        monkeypatch.setattr(vr, "_LAST_PAGE_DRAWN", None)
        monkeypatch.delenv("DOOR_CANOPY", raising=False)
        monkeypatch.delenv("DOOR_PIXELS", raising=False)
        vr._CANOPY_CACHE.clear()
    return open_


def _written(action) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        action()
    return buffer.getvalue()


@pytest.mark.parametrize("style,drawn", [("auto", True), ("fast", True), ("basic", False),
                                         ("mono", False), ("plain", False)])
def test_the_canopy_is_drawn_for_the_full_palette_presets_only(canopy, style, drawn):
    canopy(style=style, world=_world_with_seed(42))
    assert vr.canopy_active() is drawn


def test_no_canopy_without_a_live_terminal_or_when_the_sysop_says_so(canopy, monkeypatch):
    canopy(world=_world_with_seed(42))
    monkeypatch.setenv("DOOR_CANOPY", "0")
    assert not vr.canopy_active()
    monkeypatch.delenv("DOOR_CANOPY")
    monkeypatch.setattr(vr, "_live_terminal", lambda: False)
    assert not vr.canopy_active()


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24), (120, 36), (132, 50)])
def test_the_canopy_never_writes_the_bottom_right_cell_and_leaves_the_page_row_for_row(canopy, width, height):
    world = _world_with_seed(42)
    canopy(width, height, world=world)
    frame = _written(lambda: vr.draw_page(vr.pal(), "Deck", ["one", "two", "three"], 0, 1))
    for match in CUP.finditer(frame):
        row, column = int(match[1]), int(match[2])
        assert 1 <= row <= height and 1 <= column <= width
    # The last row is painted up to, never into, its last column.
    last = [m for m in CUP.finditer(frame) if int(m[1]) == height]
    for match in last:
        run = vr._ANSI_RE.sub("", frame[match.end():].split("\x1b[", 1)[0])
        assert int(match[2]) - 1 + len(run) <= width - 1
    # After the picture, the page itself: a frame row per line, as ever.
    page = frame[frame.rindex("\x1b[H") + 3:]
    rows = [vr._ANSI_RE.sub("", row) for row in page.split("\r\n") if row]
    assert rows[0].startswith("╭") and rows[-1].startswith("╰")
    assert [row.strip("│ ") for row in rows[1:-1]] == ["one", "two", "three"]


def test_turning_a_page_gives_back_only_the_rows_the_last_page_covered(canopy):
    canopy(80, 24, world=_world_with_seed(42))
    first = _written(lambda: vr.draw_page(vr.pal(), "Guide", [f"row {n}" for n in range(12)], 0, 2))
    assert "\x1b[2J" in first
    _written(lambda: vr.out_prompt("[N] Next [P] Prev [B] Back: "))
    _written(lambda: vr.out_line("N"))  # the echoed key, as `_draw_service_page` writes it
    second = _written(lambda: vr.draw_page(vr.pal(), "Guide", ["row"], 1, 2))
    assert "\x1b[2J" not in second, "the same sky was cleared and resent"
    starts = {int(m[1]): int(m[2]) for m in CUP.finditer(second)}
    # The short page covers rows 1-3. Every row from 4 to 16 -- the long page's
    # body and foot, its bar and the echo's line feed -- is given back to the
    # sky from its first column, and nothing below them is touched.
    assert {row for row, column in starts.items() if column == 1} == set(range(4, 17))
    assert max(starts) == 16
    assert len(second) < len(first) / 2

    # And a taller page after a short one: the short page's strip and echo
    # are under the new console, so only the sky beside it is given back.
    _written(lambda: vr.out_prompt("[N] Next [P] Prev [B] Back: "))
    _written(lambda: vr.out_line("N"))
    third = _written(lambda: vr.draw_page(vr.pal(), "Guide", [f"row {n}" for n in range(12)], 0, 2))
    assert "\x1b[2J" not in third
    assert all(column == vr._box_outer_width() + 1 for row, column in
               ((int(m[1]), int(m[2])) for m in CUP.finditer(third)) if row != 1)


def test_a_full_screen_picture_between_pages_means_a_fresh_canopy(canopy):
    """Deck, star map, Back: the chart was drawn after the deck's bar, so the
    deck must not keep what it thinks is its own sky (code review, #30)."""
    world = _world_with_seed(42)
    canopy(80, 24, world=world)
    _written(lambda: vr.draw_page(vr.pal(), "Deck", ["a"], 0, 1))
    _written(lambda: vr.out_prompt("[C] Chart [Q] Exit: "))
    _written(lambda: vr.out_line("C"))
    footer = "[N/P] Sector [O] Overview [L] List [I] Info [B] Back: "
    _written(lambda: (vr.clear_screen(), vr.out(vr.star_chart(world, [], None, None, footer)),
                      vr.chart_strip(), vr.out_prompt(footer)))
    _written(lambda: vr.out_line("B"))
    assert "\x1b[2J" in _written(lambda: vr.draw_page(vr.pal(), "Deck", ["a"], 0, 1))


def test_a_contracts_action_rows_sit_on_the_strip_and_keep_the_sky(canopy):
    canopy(80, 24, world=_world_with_seed(42))
    _written(lambda: vr.draw_page(vr.pal(), "First Flight", ["a"], 0, 2))
    row = _written(lambda: vr.out_action_row("[A] on last page."))
    r, g, b = vr.CANOPY_STRIP
    assert f"48;2;{r};{g};{b}" in row and plain(row).startswith("[A] on last page.")
    _written(lambda: vr.out_prompt("[N] Next [B] Back: "))
    _written(lambda: vr.out_line("N"))
    turned = _written(lambda: vr.draw_page(vr.pal(), "First Flight", ["b"], 1, 2))
    assert "\x1b[2J" not in turned
    assert max(int(m[1]) for m in CUP.finditer(turned)) == 6  # page 3, action row, bar, echo


def test_output_between_a_page_and_its_bar_is_not_mistaken_for_sky(canopy):
    canopy(80, 24, world=_world_with_seed(42))
    _written(lambda: vr.draw_page(vr.pal(), "First Flight", ["a"], 0, 2))
    _written(lambda: vr.out_line("[A] on last page."))
    _written(lambda: vr.out_prompt("[N] Next [B] Back: "))
    _written(lambda: vr.out_line("N"))
    assert "\x1b[2J" in _written(lambda: vr.draw_page(vr.pal(), "First Flight", ["b"], 1, 2))


def test_a_bar_after_an_unechoed_key_starts_off_the_strip(canopy):
    canopy(80, 24, world=_world_with_seed(42))
    _written(lambda: vr.draw_page(vr.pal(), "Contract", ["a"], 0, 1))
    _written(lambda: vr.out_prompt("[D] Abandon [B] Back: "))
    # `screen_mission_details` reads D without echoing it, then asks to confirm.
    assert _written(lambda: vr.out_prompt("Abandon it? [Y/N] ")).startswith(vr.RESET)


def test_the_strips_tint_ends_with_the_echoed_key(canopy):
    canopy(80, 24, world=_world_with_seed(42))
    _written(lambda: vr.draw_page(vr.pal(), "Deck", ["a"], 0, 1))
    _written(lambda: vr.out_prompt("[Q] Exit: "))
    echo = _written(lambda: vr.out_line("Q"))
    assert echo == "Q" + vr.RESET + "\r\n"
    assert _written(lambda: vr.out_line("Fly safe.")) == "Fly safe.\r\n"
    vr.chart_strip()
    _written(lambda: vr.out_prompt("[Q] Exit: "))
    assert _written(vr.clear_screen).startswith(vr.RESET + "\x1b[2J")


def test_the_canopy_is_not_composed_for_a_huge_terminal(canopy):
    canopy(vr.CANOPY_MAX_WIDTH + 1, 40, world=_world_with_seed(42))
    assert not vr.canopy_active()


def test_a_canopy_composed_in_truecolor_is_not_served_at_256(canopy, monkeypatch):
    world = _world_with_seed(42)
    canopy(80, 24, world=world)
    scene = vr.canopy_scene(world)
    deep = vr.canopy_cells(scene, 80, 24)
    monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=False))
    assert vr.canopy_cells(scene, 80, 24) is not deep
    assert vr.canopy_cells(scene, 80, 24)[0][0][2] == (0, 0, 0)


def test_anything_else_drawn_between_pages_means_a_fresh_canopy(canopy):
    canopy(80, 24, world=_world_with_seed(42))
    _written(lambda: vr.draw_page(vr.pal(), "Guide", ["a"], 0, 2))
    _written(lambda: vr.out_prompt("[N] Next [B] Back: "))
    _written(lambda: vr.out_line("Jumping to Kepler Reach and a great deal more besides..."))
    assert "\x1b[2J" in _written(lambda: vr.draw_page(vr.pal(), "Guide", ["b"], 1, 2))


def test_the_strip_leaves_the_cursor_where_the_bar_ends(canopy):
    canopy(80, 24, world=_world_with_seed(42))
    _written(lambda: vr.draw_page(vr.pal(), "Deck", ["one"], 0, 1))
    bar = _written(lambda: vr.out_prompt("[X] Expand [Q] Exit: "))
    # The bar is written over the strip, so its last characters are the last
    # thing on the wire before the key the caller types.
    assert plain(bar).endswith("[X] Expand [Q] Exit: ")
    r, g, b = vr.CANOPY_STRIP
    assert f"48;2;{r};{g};{b}" in bar, "the bar is not on its strip"


def test_a_fight_lights_the_cockpit_red(canopy):
    world, _ = _world_with_pending_fight()
    canopy(80, 24, world=world)
    assert vr.canopy_scene(world)["kind"] == "combat" and vr.red_alert()
    frame = _written(lambda: vr.draw_page(vr.pal(), "Combat", ["one"], 0, 1))
    r, g, b = vr.CANOPY_ALERT_GLASS
    assert f"48;2;{r};{g};{b}" in frame
    world.save.pending_travel = None
    assert vr.canopy_scene(world)["kind"] == "dock" and not vr.red_alert()


def test_a_wide_canopy_puts_its_picture_in_the_window_not_under_the_glass(canopy):
    world = _world_with_seed(42)
    canopy(120, 36, world=world)
    cells = vr.canopy_cells(vr.canopy_scene(world), 120, 36)
    # Under the console every cell is sky: one color, no block, no art.
    for row in cells[:30]:
        for ch, fg, bg in row[:vr._box_outer_width()]:
            assert ch in " ·•+", ch
    assert any(ch not in " ·•+" for row in cells for ch, _, _ in row[vr._box_outer_width() + 1:])


def test_one_canopy_serves_every_page_of_a_place(canopy):
    world = _world_with_seed(42)
    canopy(80, 24, world=world)
    scene = vr.canopy_scene(world)
    assert vr.canopy_cells(scene, 80, 24) is vr.canopy_cells(scene, 80, 24)


@pytest.mark.parametrize("encoding,quadrants,pixels,across", [
    ("utf-8", None, "", 2), ("utf-8", True, "", 2), ("utf-8", False, "", 1),
    ("cp437", None, "", 1), ("utf-8", None, "half", 1)])
def test_quadrant_blocks_only_where_they_can_be_drawn(monkeypatch, encoding, quadrants, pixels, across):
    monkeypatch.setattr(vr, "_OUTPUT_ENCODING", encoding)
    monkeypatch.setattr(vr, "_CANOPY_QUADRANTS", quadrants)
    monkeypatch.setenv("DOOR_PIXELS", pixels)
    assert vr.canopy_across() == across


def test_a_quadrant_cell_keeps_two_colors_exactly_and_splits_more_where_it_loses_least():
    red, blue = (200, 0, 0), (0, 0, 200)
    assert vr._quadrant_cell(red, blue, blue, blue) == ("▘", red, blue)
    assert vr._quadrant_cell(blue, blue, blue, blue) == (" ", None, blue)
    ch, fg, bg = vr._quadrant_cell(red, (190, 0, 0), blue, (0, 0, 190))
    assert ch == "▀" and fg[0] > 150 and bg[2] > 150


def test_below_truecolor_the_sky_is_black(canopy):
    world = _world_with_seed(42)
    canopy(80, 24, truecolor=False, world=world)
    cells = vr.canopy_cells(vr.canopy_scene(world), 80, 24)
    assert cells[0][0][2] == (0, 0, 0)
    assert vr.scene_sky("alert") is None


def test_the_star_chart_names_only_what_the_grid_may(canopy):
    world = _world_with_seed(42)
    canopy(120, 36, world=world)
    for station in world.galaxy:
        station.discovered = station.id < 6
    world.sync_discovered()
    target = next(s for s in world.galaxy if not s.discovered)
    chart = plain(vr.star_chart(world, [], target.id, None, "[B] Back: "))
    assert "Star Map: Galaxy" in chart and "danger 4+" in chart
    assert target.name in chart  # a contract's target is named, charted or not
    for station in world.galaxy:
        if not station.discovered and station.id != target.id:
            assert station.name not in chart, station.name
    assert world.here.name.upper() in chart


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24), (132, 50)])
def test_the_star_chart_fits_and_spares_the_bottom_right_cell(canopy, width, height):
    world = _world_with_seed(42)
    canopy(width, height, world=world)
    footer = "[N/P] Sector [O] Overview [L] List [I] Info [B] Back: "
    chart = vr.star_chart(world, [], None, None, footer)
    bar_rows = len(vr._wrap_output(footer, width - 1).split("\r\n"))
    # The cursor is left where the bar starts, so the bar ends a row above the
    # bottom and the echoed key's line feed cannot scroll the picture.
    assert chart.endswith(f"\x1b[{height - bar_rows};1H")
    for match in CUP.finditer(chart):
        assert int(match[1]) <= height
        if int(match[1]) == height:
            run = vr._ANSI_RE.sub("", chart[match.end():].split("\x1b[", 1)[0])
            assert int(match[2]) - 1 + len(run) <= width - 1


def test_the_pixel_hull_is_the_scenes_and_the_colorless_presets_keep_the_ascii(monkeypatch):
    ship = _world_with_seed(42).save.ship
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    rows = vr.ship_portrait(ship, "compact")
    assert len(rows) == (len(vr._SCENE_HULLS[ship.hull_class]) + 1) // 2
    assert max(vr._visible_width(row) for row in rows) <= len(vr._SCENE_HULLS[ship.hull_class][0])
    assert len(vr.ship_portrait(ship, "large")) == len(vr._SCENE_HULLS[ship.hull_class])
    for style in ("mono", "plain"):
        monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
        assert vr.ship_portrait(ship, "compact") == vr.PORTRAITS["ship"][ship.hull_class]["compact"]


@pytest.mark.parametrize("value,expected", [(0, "─────"), (10, "━━━━━"), (5, "━━╾──"),
                                            (1, "╾────"), (3, "━╾───")])
def test_a_gauge_reads_to_half_a_cell(monkeypatch, value, expected):
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    assert plain(vr.gauge(value, 10, 5)) == expected


def test_a_gauge_on_a_plain_terminal_is_ascii(monkeypatch):
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "plain")
    assert vr.gauge(5, 10, 6) == "###..."


@pytest.mark.parametrize("style", ["auto", "basic", "plain"])
@pytest.mark.parametrize("width", [1, 9, 40])
def test_the_lane_streak_fills_exactly_its_width(monkeypatch, style, width):
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
    for drawn in (0.0, 0.5, 1.0):
        streak = vr.lane_streak(width, drawn=drawn)
        assert vr._visible_width(streak) == width
    if style == "plain":
        assert "━" not in vr.lane_streak(width)


def test_danger_reads_as_pips_and_the_quote_as_a_mark_on_its_scale(monkeypatch):
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    assert plain(vr.danger_dots(3)) == "●●●○○"
    ladder = [10, 20, 30]
    assert plain(vr.price_scale(10, ladder, "good")).index("●") == 0
    assert plain(vr.price_scale(30, ladder, "caution")).index("●") == 6
    assert plain(vr.price_scale(20, ladder, "label")).index("●") == 3


def test_every_combat_option_is_on_the_first_page_with_a_bounty_to_identify(monkeypatch):
    """Issue #28: the identification paragraph used to push Bribe onto page two."""
    world, pirate = _world_with_pending_fight()
    world.save.pilot.credits = 10_000
    warrant = {"checked": False, "engaged": False, "matches": True}
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 24)
    tactics = world.save.pending_travel["encounter"]["combat"]["tactics"]
    lines = vr.combat_display_lines(world, pirate, [], patrol=False, tactics=tactics, warrant=warrant)
    bar = vr.combat_action_bar("F/G/E/P/V/W")
    first = " ".join(plain(row) for row in vr._service_pages(lines, "Combat 10,000cr", bar)[0])
    for option in ("[F] Fire", "[E] Evade", "[P] Pay bribe"):
        assert option in first, option
    order = [plain(row) for row in lines]
    assert order.index(next(r for r in order if "OPTIONS" in r)) < order.index(next(r for r in order if "IDENTIFICATION" in r))


def test_the_title_comes_round_again_while_it_waits():
    layout = vr._SplashLayout(80, 24, "ReLink", "Pilot")
    rest = vr.splash_frame(layout, vr.SPLASH_FRAMES - 1)
    ticks = [vr.splash_frame(layout, vr.SPLASH_FRAMES - 1, attract=tick) for tick in range(1, vr.SPLASH_FLYBY_TICKS)]
    assert all(frame[23][79] == (" ", None, None) for frame in ticks)
    assert any(frame != rest for frame in ticks)
    # The same tick is the same picture: attract mode is a function, not a state.
    assert vr.splash_frame(layout, 0, attract=7) == vr.splash_frame(layout, 0, attract=7)


def test_a_pickers_long_rows_stay_on_the_console_glass(canopy, monkeypatch):
    """Wrapped to a 120-column terminal, a long label or notice ran off the
    79-column glass onto the sky beside it (#33)."""
    canopy(120, 36, world=_world_with_seed(42))
    keys = iter("B")
    monkeypatch.setattr(vr, "read_command", lambda: next(keys))
    monkeypatch.setattr(vr, "_resized_while_idle", lambda: False)
    label = "Deliver 14 units of Medical Supplies to Kepler Reach for the Free Traders Union, reward 4,200cr"
    notice = "A notice about the exchange that is a long sentence, as wide as a sentence on a wide terminal gets."
    written = _written(lambda: vr._pick_trade_field("Charted Destination", [(1, label), (2, "Short")], notice=[notice]))
    rows = [plain(row) for row in written.split("\r\n")[1:]]
    listed = [row for row in rows if row.strip() and "Select" not in row]
    assert any(row.startswith("[1] Deliver") for row in listed)
    assert all(len(row.rstrip()) <= vr._box_outer_width() for row in listed)
    assert "reward 4,200cr" in " ".join(listed)
    # The picker cleared the screen, so the page after it draws a fresh sky.
    assert "\x1b[2J" in _written(lambda: vr.draw_page(vr.pal(), "Deck", ["a"], 0, 1))


def _commission(monkeypatch, keys, welcome=None):
    stream = iter(keys)
    monkeypatch.setattr(vr, "read_key", lambda: next(stream))
    result = []
    written = _written(lambda: result.append(vr.create_career(vr.pal(), {"handle": "Nib"}, welcome=welcome)))
    return result[0], written


def test_a_new_pilot_is_commissioned_on_the_canopy(canopy, monkeypatch):
    canopy(120, 36)
    callsign, written = _commission(monkeypatch, [*"Nova", "\r", "Y"])
    assert callsign == "Nova"
    prompt = written.index("Pilot callsign")
    # The page is drawn before the question, and again with the answer on it.
    assert "COMMISSION" in plain(written[:prompt]) and "enter it below" in plain(written[:prompt])
    second = plain(written[prompt:])
    assert "Callsign Nova" in second and "Launch Nova's career?" in second


def test_escape_at_the_commission_saves_no_career(canopy, monkeypatch):
    canopy(80, 24)
    callsign, written = _commission(monkeypatch, ["\r", vr.ESCAPE_KEY])
    assert callsign is None and "No career was saved." in plain(written)


def test_the_commission_states_the_terms_the_career_will_start_with(canopy, monkeypatch):
    """Said once: change the starting bank and the commission says so (#34)."""
    canopy(80, 24)
    monkeypatch.setattr(vr, "STARTING_CREDITS", 1500)
    _, written = _commission(monkeypatch, ["\r", "Y"])
    assert "1,500cr" in plain(written) and "1,200cr" not in plain(written)
    assert vr._new_career("Nib").pilot.credits == 1500


def test_the_commission_picks_the_callsign_out_in_gold(canopy, monkeypatch):
    canopy(80, 24)
    p = vr.pal()
    outdated = f"Welcome to the void, pilot. The dossier for {p.gold}Nib{p.muted} predates this version."
    for welcome in (None, outdated):
        _, written = _commission(monkeypatch, ["\r", "Y"], welcome)
        greeting = written[:written.index("COMMISSION")]
        # Gold for the callsign, then back to the page's prose, not the box's muted.
        assert f"{p.gold}Nib{p.text}" in greeting
