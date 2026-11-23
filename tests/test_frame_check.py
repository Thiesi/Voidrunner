"""`tools/frame_check.py` sees what a terminal would show broken (#93).

The gallery's `--check` and the first-days walk both stand on it, so it has
to catch each fault it names, and pass a real page at every size and preset,
or every panel would fail at once and the check would be switched off.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

from .support import vr

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def frames():
    sys.path.insert(0, str(ROOT / "tools"))
    spec = importlib.util.spec_from_file_location("frame_check_under_test", ROOT / "tools" / "frame_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BOX = ["╭─ Title ──── 1/1 ╮",
       "│  a row          │",
       "├─ RULE ──────────┤",
       "│  another        │",
       "╰─────────────────╯"]
ASCII_BOX = ["+- Title ---- 1/1 +",
             "|  a row          |",
             "+- RULE ----------+",
             "|  another        |",
             "+-----------------+"]


def screen(rows):
    return "\r\n".join(rows)


@pytest.mark.parametrize("rows", [BOX, ASCII_BOX])
def test_a_square_box_has_no_problems(frames, rows):
    assert frames.frame_problems(screen(rows), 20, 6) == []


def test_a_row_that_runs_past_the_last_column_is_overflow(frames):
    rows = [*BOX[:3], "│  another        │ and on and on", BOX[4]]
    assert frames.frame_problems(screen(rows), 24, 6) == ["a write ran past column 24 on row 4"]


def test_writing_up_to_the_last_column_and_breaking_the_line_is_not_overflow(frames):
    assert frames.frame_problems(screen(["x" * 20, "y" * 20]), 20, 3) == []


@pytest.mark.parametrize("rows", [BOX, ASCII_BOX])
def test_a_box_whose_right_border_moves_is_ragged(frames, rows):
    ragged = list(rows)
    ragged[3] = ragged[3][:-2] + ragged[3][-1]  # one column short
    found = frames.frame_problems(screen(ragged), 20, 6)
    assert len(found) == 1 and "right border is not in column 19 on row 4" in found[0]


def test_a_box_without_a_corner_on_its_top_row_is_ragged(frames):
    found = frames.frame_problems(screen(["╭─ Title ────────", *BOX[1:]]), 20, 6)
    assert found == ["the box at row 1, column 1 has no top-right corner on its row"]


def test_a_box_cut_off_by_the_bottom_of_the_screen_is_checked_as_far_as_it_goes(frames):
    assert frames.frame_problems(screen(BOX[:4]), 20, 4) == []
    assert len(frames.frame_problems(screen([*BOX[:3], "│  another      │"]), 20, 4)) == 1


def test_a_lone_plus_is_a_star_not_a_corner(frames):
    assert frames.frame_problems(screen(["  +   ·  +  ", "    +"]), 20, 3) == []


def test_a_replacement_character_is_reported(frames):
    assert frames.frame_problems(screen(["caf�"]), 20, 2) == ["row 1 has a replacement character (U+FFFD)"]


@pytest.mark.parametrize("style", ["auto", "basic", "mono", "plain"])
@pytest.mark.parametrize("size", [(40, 12), (80, 24), (160, 48)])
def test_a_real_page_passes_at_every_size_and_preset(frames, capsys, terminal, style, size):
    """A page the door draws, frame and canopy and all, the way it draws it."""
    from .support import _world_with_seed
    width, height = size
    terminal(width, height, style)
    vr.apply_display_style(style)
    try:
        # Paged the way the guide's own screen pages it, which fits each row
        # to the frame first.
        pages = vr._mission_text_pages(vr.pilot_guide_lines(_world_with_seed(42)),
                                       overhead=5 + vr._page_frame_rows("Pilot Guide"))
        vr.draw_page(vr.pal(), "Pilot Guide", pages[0], 0, len(pages))
    finally:
        vr.apply_display_style("auto")
    drawn = capsys.readouterr().out
    assert "Pilot Guide" in drawn
    assert frames.frame_problems(drawn, width, height) == []
