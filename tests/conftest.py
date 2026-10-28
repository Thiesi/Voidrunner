"""Fixtures shared by the Voidrunner suite (issue #422)."""

from __future__ import annotations

import pytest

from .support import vr


@pytest.fixture(autouse=True)
def _no_door_splash(monkeypatch):
    """A door launched by a test opens straight on its first screen.

    A finished launch splash waits for a key, so a test that starts the real door
    and waits for its first prompt before typing would wait forever. The
    splash's own tests take this back off (`monkeypatch.delenv`).

    The in-flight scenes are off for the same kind of reason: a scene takes the
    key that ends it, and a test types its keys one at a time into a live pipe.
    The scenes' own tests take this back off too."""
    monkeypatch.setenv("DOOR_SPLASH", "0")
    monkeypatch.setenv("DOOR_SCENES", "0")
    # The status line is session state (issue #38): one test's result must not
    # be drawn in the next test's border.
    vr.clear_status(); vr._RESULT_LOG.clear()
    yield
    vr.clear_status(); vr._RESULT_LOG.clear()


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
