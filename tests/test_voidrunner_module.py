"""The module's own structural rules -- where things live and how they
are named and spelled.

Split out of `test_voidrunner_domain.py` (issue #422).
"""

from __future__ import annotations

import pytest

from .support import plain, vr


def test_paginate_keeps_groups_whole_splits_oversized_ones_and_never_repeats_a_letter():
    """The one paginator behind every paged screen (issue #418)."""
    groups = [["a1", "a2"], ["b1"], ["c1", "c2", "c3", "c4", "c5"]]
    assert vr.paginate(groups, 3) == [["a1", "a2", "b1"], ["c1", "c2", "c3"], ["c4", "c5"]]
    assert vr.paginate(groups, 10) == [["a1", "a2", "b1", "c1", "c2", "c3", "c4", "c5"]]
    colored = vr.paginate(groups, 10, render=lambda row, index: f"<{row}>" if index == 1 else row)
    assert colored[0][2] == "<b1>"
    keyed = vr.paginate([["A one"], ["A two"], ["B three"]], 10,
                        keys=[("A", 1), ("A", 2), ("B", 3)])
    # The selection key is styled by `keyed_rows` (issue #493 review).
    assert ([[plain(row) for row in rows] for rows, _ in keyed]
            == [["[A] A one"], ["[A] A two", "[B] B three"]])
    assert [choices for _, choices in keyed] == [{"A": 1}, {"A": 2, "B": 3}]  # a letter never repeats
    # An entry taller than a page carries its letter on every page it reaches (#411 review).
    split = vr.paginate([["one", "two", "three"]], 2, keys=[("A", 7)])
    assert ([[plain(row) for row in rows] for rows, _ in split]
            == [["[A] one", "    two"], ["[A] three"]])
    assert [choices for _, choices in split] == [{"A": 7}, {"A": 7}]


class _FakeTerminal:
    def isatty(self):
        return True

    def fileno(self):
        return 0


def test_a_standalone_console_reads_keys_one_at_a_time_and_restores_itself(monkeypatch):
    import os
    import shutil
    calls = []
    monkeypatch.setattr(vr.sys, "stdin", _FakeTerminal())
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback: os.terminal_size((140, 40)))
    monkeypatch.setenv("COLORTERM", "truecolor")
    if os.name == "nt":
        import ctypes
        class Kernel:
            def GetStdHandle(self, which): return which
            def GetConsoleMode(self, handle, mode): mode._obj.value = 0x1F7 if handle == -10 else 0x3; return 1
            def SetConsoleMode(self, handle, mode): calls.append((handle, mode)); return 1
        monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: Kernel())
    else:
        import termios
        import tty
        monkeypatch.setattr(termios, "tcgetattr", lambda fd: ["saved"])
        monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, attrs: calls.append(("restore", attrs)))
        monkeypatch.setattr(tty, "setcbreak", lambda fd: calls.append(("cbreak", fd)))
    info = {"terminal_width": 80, "terminal_height": 24, "color_depth": "256"}
    with vr._standalone_console(info):
        assert info == {"terminal_width": 140, "terminal_height": 40, "color_depth": "truecolor"}
        during = list(calls)
    if os.name == "nt":
        # Line input and echo off, VT input on; VT output on.
        assert during == [(-10, (0x1F7 & ~0x6) | 0x200), (-11, 0x3 | 0x4)]
        assert calls[2:] == [(-10, 0x1F7), (-11, 0x3)]
    else:
        assert during == [("cbreak", 0)] and calls[1:] == [("restore", ["saved"])]


def test_a_piped_standalone_run_is_left_as_it_is(monkeypatch):
    info = {"terminal_width": 80}
    monkeypatch.setattr(vr.sys, "stdin", type("Pipe", (), {"isatty": lambda self: False})())
    with vr._standalone_console(info):
        pass
    assert info == {"terminal_width": 80}
