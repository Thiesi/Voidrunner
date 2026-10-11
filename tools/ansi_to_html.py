"""Turn raw ANSI captured from a terminal into HTML, one cell per character.

Copied from NetBBS's `scripts/website_ansi_to_html.py`, which renders the
screenshots on www.netbbs.org; here `tools/door_gallery.py` paints its panels
with the `Screen` emulator below. It also runs on its own:

    python tools/ansi_to_html.py raw.txt shot.html

Two things this has to get right, both learned the hard way:

* **One `<span>` per character**, each `display:inline-block; width:1ch`.
  No monospace *web* font gives box-drawing/block glyphs the same advance
  width a real terminal font does, so a multi-character run styled as one
  span accumulates sub-pixel error across a row and the long borders bend.
  The page pairs this with `white-space:pre`, so a capture wider than its
  box scrolls rather than re-flowing into nonsense.

* **Emulate the terminal, do not stream the bytes.** The chat screen sets a
  scroll region (`CSI 1;21r`), pins a status/input area on rows 21-24, and
  repaints it with absolute cursor moves and save/restore. Replaying that as
  a flat character stream repeats the status line after every message.
  Painting it into an 80x24 cell buffer -- what the caller's terminal
  actually does -- yields the screen they actually see.

Known limitation: one cell is advanced per code point, so a double-width
glyph (CJK) would occupy one column instead of two and a combining mark
would take a cell of its own, shifting the rest of the row. No capture the
site uses contains either. If one ever does, place characters by display
width and fold zero-width marks into the preceding cell.
"""

from __future__ import annotations

import argparse
import html
import re
from dataclasses import dataclass, replace
from pathlib import Path

# --- xterm 256-color palette -------------------------------------------

_BASE16 = [
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
]
_LEVELS = (0, 95, 135, 175, 215, 255)


def xterm256(index: int) -> tuple[int, int, int]:
    if index < 16:
        return _BASE16[index]
    if index < 232:
        index -= 16
        return (_LEVELS[index // 36], _LEVELS[index // 6 % 6], _LEVELS[index % 6])
    grey = 8 + 10 * (index - 232)
    return (grey, grey, grey)


#: What the embedded terminal's own CSS paints when a cell names no color
#: of its own -- `.term{background:#0a0c10}` and `pre.term-body{color:#c7cad4}`
#: in both pages. Reverse video swaps *effective* colors, so an inverted
#: cell that set neither has to be given these explicitly: there is no
#: "unset" for the swap to exchange.
DEFAULT_FG = (0xC7, 0xCA, 0xD4)
DEFAULT_BG = (0x0A, 0x0C, 0x10)


@dataclass(frozen=True)
class Style:
    fg: tuple[int, int, int] | None = None
    bg: tuple[int, int, int] | None = None
    bold: bool = False
    underline: bool = False
    reverse: bool = False

    def css(self) -> str:
        parts = []
        fg, bg = self.fg, self.bg
        if self.reverse:
            # SGR 7. A selected row in `pick_item` and in the file listing is
            # one inverted run as of v7.5.0, and dropping the attribute --
            # which this converter did until the captures were redone for
            # that release -- renders the cursor as ordinary text, in the
            # one shot chosen to show it.
            fg, bg = bg or DEFAULT_BG, fg or DEFAULT_FG
        if fg:
            parts.append("color:rgb(%d,%d,%d)" % fg)
        if bg:
            parts.append("background:rgb(%d,%d,%d)" % bg)
        if self.bold:
            parts.append("font-weight:600")
        if self.underline:
            parts.append("text-decoration:underline")
        parts.append("display:inline-block")
        parts.append("width:1ch")
        return ";".join(parts)


BLANK = Style()

_CSI = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")


class Screen:
    """A cell buffer painted the way the caller's terminal would paint it."""

    def __init__(self, width: int = 80, height: int = 24):
        self.width, self.height = width, height
        self.cells = [[(" ", BLANK) for _ in range(width)] for _ in range(height)]
        self.row = self.col = 0
        self.style = BLANK
        self.saved: tuple[int, int, Style] | None = None
        self.top, self.bottom = 0, height - 1   # scroll region, inclusive

    # -- painting ---------------------------------------------------------

    def put(self, char: str) -> None:
        if self.col >= self.width:          # deferred auto-wrap
            self.col = 0
            self.index()
        self.cells[self.row][self.col] = (char, self.style)
        self.col += 1

    def index(self) -> None:
        """Line feed: scroll the region when already at its bottom."""
        if self.row == self.bottom:
            del self.cells[self.top]
            self.cells.insert(self.bottom, [(" ", BLANK) for _ in range(self.width)])
        elif self.row < self.height - 1:
            self.row += 1

    def erase_line(self, mode: int) -> None:
        blank = (" ", BLANK)
        row = self.cells[self.row]
        if mode == 0:
            row[self.col:] = [blank] * (self.width - self.col)
        elif mode == 1:
            row[: self.col + 1] = [blank] * min(self.col + 1, self.width)
        else:
            row[:] = [blank] * self.width

    def erase_display(self, mode: int) -> None:
        blank = (" ", BLANK)
        if mode == 2:
            self.cells = [[blank] * self.width for _ in range(self.height)]
        elif mode == 0:
            self.erase_line(0)
            for r in range(self.row + 1, self.height):
                self.cells[r] = [blank] * self.width
        else:
            self.erase_line(1)
            for r in range(self.row):
                self.cells[r] = [blank] * self.width

    # -- SGR --------------------------------------------------------------

    def sgr(self, params: list[int]) -> None:
        if not params:
            params = [0]
        i = 0
        while i < len(params):
            p = params[i]
            if p == 0:
                self.style = BLANK
            elif p == 1:
                self.style = replace(self.style, bold=True)
            elif p in (21, 22):
                self.style = replace(self.style, bold=False)
            elif p == 4:
                # `_compose_status_line` underlines the row for an away caller.
                self.style = replace(self.style, underline=True)
            elif p == 24:
                self.style = replace(self.style, underline=False)
            elif p == 7:
                self.style = replace(self.style, reverse=True)
            elif p == 27:
                self.style = replace(self.style, reverse=False)
            elif 30 <= p <= 37:
                self.style = replace(self.style, fg=xterm256(p - 30))
            elif 90 <= p <= 97:
                self.style = replace(self.style, fg=xterm256(p - 90 + 8))
            elif p == 39:
                self.style = replace(self.style, fg=None)
            elif 40 <= p <= 47:
                self.style = replace(self.style, bg=xterm256(p - 40))
            elif 100 <= p <= 107:
                self.style = replace(self.style, bg=xterm256(p - 100 + 8))
            elif p == 49:
                self.style = replace(self.style, bg=None)
            elif p in (38, 48):
                target = "fg" if p == 38 else "bg"
                if i + 1 < len(params) and params[i + 1] == 5:
                    self.style = replace(self.style, **{target: xterm256(params[i + 2])})
                    i += 2
                elif i + 1 < len(params) and params[i + 1] == 2:
                    rgb = (params[i + 2], params[i + 3], params[i + 4])
                    self.style = replace(self.style, **{target: rgb})
                    i += 4
            i += 1

    # -- feeding ----------------------------------------------------------

    def feed(self, data: str) -> None:
        i = 0
        while i < len(data):
            ch = data[i]
            if ch == "\x1b":
                match = _CSI.match(data, i)
                if match:
                    self.csi(match.group(1), match.group(2))
                    i = match.end()
                    continue
                nxt = data[i + 1: i + 2]
                if nxt == "7":
                    self.saved = (self.row, self.col, self.style)
                elif nxt == "8" and self.saved:
                    self.row, self.col, self.style = self.saved
                i += 2
                continue
            if ch == "\n":
                self.index()
                self.col = 0
            elif ch == "\r":
                self.col = 0
            elif ch == "\b":
                self.col = max(0, self.col - 1)
            elif ch in "\a\x00":
                pass
            elif ch == "\t":
                self.col = min(self.width - 1, (self.col // 8 + 1) * 8)
            else:
                self.put(ch)
            i += 1

    def csi(self, raw: str, final: str) -> None:
        if raw.startswith("?"):
            return                                     # DEC private modes
        params = [int(p) if p else 0 for p in raw.split(";")] if raw else []

        def arg(n=0, default=1):
            return params[n] if n < len(params) and params[n] else default

        if final == "m":
            self.sgr(params)
        elif final in "Hf":
            self.row = min(self.height - 1, max(0, arg(0) - 1))
            self.col = min(self.width - 1, max(0, arg(1) - 1))
        elif final == "A":
            self.row = max(0, self.row - arg())
        elif final == "B":
            self.row = min(self.height - 1, self.row + arg())
        elif final == "C":
            self.col = min(self.width - 1, self.col + arg())
        elif final == "D":
            self.col = max(0, self.col - arg())
        elif final == "G":
            self.col = min(self.width - 1, max(0, arg(0) - 1))
        elif final == "J":
            self.erase_display(arg(0, 0))
        elif final == "K":
            self.erase_line(arg(0, 0))
        elif final == "r":
            self.top = max(0, arg(0) - 1)
            self.bottom = min(self.height - 1, arg(1, self.height) - 1)
            self.row = self.col = 0
        elif final == "s":
            self.saved = (self.row, self.col, self.style)
        elif final == "u" and self.saved:
            self.row, self.col, self.style = self.saved

    # -- output -----------------------------------------------------------

    def to_html(self) -> str:
        lines = []
        for row in self.cells:
            end = len(row)
            while end and row[end - 1] == (" ", BLANK):
                end -= 1
            lines.append("".join(
                '<span style="%s">%s</span>' % (style.css(), html.escape(char))
                for char, style in row[:end]
            ))
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)


def render(raw: str, *, width: int = 80, height: int = 24) -> str:
    screen = Screen(width, height)
    screen.feed(raw)
    return screen.to_html()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capture", type=Path, help="raw ANSI bytes from a node")
    parser.add_argument("output", type=Path, help="HTML fragment to write")
    parser.add_argument("--width", type=int, default=80)
    parser.add_argument("--height", type=int, default=24)
    args = parser.parse_args()

    # Read and write bytes, never text. On Windows, text mode turns the CR LF
    # that every `write_line` emits into CR CR LF; a page stored with CRLF
    # then translates those a second time, and HTML renders the result as a
    # blank line between every terminal row.
    raw = args.capture.read_bytes().decode("utf-8")
    body = render(raw, width=args.width, height=args.height)
    args.output.write_bytes(body.encode("utf-8"))
    print(f"wrote {args.output} ({len(body.splitlines())} rows)")


if __name__ == "__main__":
    main()
