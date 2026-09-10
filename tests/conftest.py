"""Fixtures shared by the Voidrunner suite (issue #422)."""

from __future__ import annotations

import pytest

from .support import vr


@pytest.fixture
def without_action_bar(monkeypatch):
    """Removes the action bar from the frame that has just been drawn.

    The bar is whatever `out_prompt` wrote last: it ends the frame, wrapping into
    as many rows as the width needs. Tests used to spot it by its hotkey style,
    which stopped telling it from a body row once every hotkey in the game was
    written `[K] Label` (issue #400), so ask the writer instead of guessing.
    """
    rows_written, real = [1], vr.out_prompt
    def spy(text):
        rows_written[0] = len(vr._wrap_output(text, max(1, vr._OUTPUT_WIDTH - 1)).split("\r\n"))
        real(text)
    monkeypatch.setattr(vr, "out_prompt", spy)
    def strip(text: str) -> str:
        rows = text.splitlines()
        del rows[max(0, len(rows) - rows_written[0]):]
        while rows and not rows[-1].strip(): rows.pop()
        return "\n".join(rows)
    return strip


@pytest.fixture
def terminal(monkeypatch):
    """Negotiate the door's terminal for one test.

    Some sixty screen tests set the width, the height and sometimes the display
    style by hand, three `monkeypatch.setattr` lines at a time, which buried what
    each test was actually about (issue #422).
    """
    def negotiate(width: int, height: int, style: str | None = None) -> None:
        monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
        monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
        if style is not None:
            monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
    return negotiate
