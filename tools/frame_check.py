"""Find what a terminal would show broken on one painted screen.

    from frame_check import frame_problems
    frame_problems(screen_text, width, height)   # [] when the screen is sound

The suite can prove that a screen builder returns rows that fit. It cannot see
the screen a caller is left looking at after the door has written a page, a
canopy and a bar into it by cursor address: two regressions in the review
(#69, #82) were only on that screen, and only at some sizes. This paints a
screen the way `tools/ansi_to_html.py` does for the gallery and reports:

- **overflow**: a character written past the last column, which a real
  terminal wraps onto the next row. The painted canvas is never wider than
  the terminal, so overflow cannot be measured afterwards; it is caught as it
  happens, when the emulator's deferred auto-wrap fires. Writing exactly up
  to the last column and then a line break is not overflow.
- **a ragged box**: a frame whose right border is not in one column from its
  top corner to its bottom corner, in either the Unicode or the ASCII
  (`plain` preset) spelling.
- **a replacement character** (U+FFFD), where bytes did not decode.

The gallery's `--check` runs this on every panel, and
`tests/test_voidrunner_first_days.py` on every stop of a scripted career.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ansi_to_html as term  # noqa: E402

TOP_LEFT, TOP_RIGHT = "╭╔┌┏", "╮╗┐┓"
BOTTOM_LEFT, BOTTOM_RIGHT = "╰╚└┗", "╯╝┘┛"
LEFT_EDGE, RIGHT_EDGE = "│║┃├╠┣|+", "│║┃┤╣┫|+"
RULE = "─━═-="


class CheckedScreen(term.Screen):
    """The gallery's emulator, noting each row a write ran off the end of."""

    def __init__(self, width: int = 80, height: int = 24):
        super().__init__(width, height)
        self.overflowed: list[int] = []

    def put(self, char: str) -> None:
        if self.col >= self.width:
            self.overflowed.append(self.row)
        super().put(char)


def _opens_box(row: list[str], col: int) -> bool:
    """A top-left corner with a rule after it. `+` is a corner only there:
    a lone one is a star on the chart."""
    if col + 1 >= len(row) or row[col + 1] not in RULE:
        return False
    return row[col] in TOP_LEFT or (row[col] == "+" and row[col + 1] in "-=")


def _closes_top(row: list[str], col: int) -> bool:
    """A top-right corner. In ASCII it is the first `+` no rule runs on from:
    the page counter can stand between it and the rule (`--- 1/1 +`)."""
    if row[col] in TOP_RIGHT:
        return True
    return row[col] == "+" and (col + 1 == len(row) or row[col + 1] not in "-=")


def _boxes(rows: list[list[str]]):
    """Every box on the canvas, as (top, left, right, bottom or None)."""
    for top, row in enumerate(rows):
        for left in range(len(row)):
            if not _opens_box(row, left):
                continue
            right = next((col for col in range(left + 1, len(row)) if _closes_top(row, col)), None)
            if right is None:
                yield top, left, None, None
                continue
            bottom = None
            for below in range(top + 1, len(rows)):
                cell = rows[below][left]
                if cell in BOTTOM_LEFT or (cell == "+" and left + 1 < len(row) and rows[below][left + 1] in "-="):
                    bottom = below
                    break
                if cell not in LEFT_EDGE:
                    break  # the box runs off the bottom of the screen, or its left side stops
            yield top, left, right, bottom


def frame_problems(screen: str, width: int, height: int) -> list[str]:
    """What is broken on `screen` painted at `width` x `height`; [] when nothing is."""
    canvas = CheckedScreen(width, height)
    canvas.feed(screen)
    rows = [[char for char, _ in row] for row in canvas.cells]
    problems = [f"a write ran past column {width} on row {row + 1}"
                for row in dict.fromkeys(canvas.overflowed)]
    for top, left, right, bottom in _boxes(rows):
        where = f"the box at row {top + 1}, column {left + 1}"
        if right is None:
            problems.append(f"{where} has no top-right corner on its row")
            continue
        last = bottom if bottom is not None else top
        for below in range(top + 1, last + 1 if bottom is not None else len(rows)):
            if bottom is None and rows[below][left] not in LEFT_EDGE:
                break
            cell = rows[below][right]
            closing = BOTTOM_RIGHT + "+" if below == bottom else RIGHT_EDGE
            if cell not in closing:
                problems.append(f"{where}: its right border is not in column {right + 1} "
                                f"on row {below + 1} ({cell!r} there)")
                break
    for number, row in enumerate(rows, 1):
        if "�" in row:
            problems.append(f"row {number} has a replacement character (U+FFFD)")
    return problems
