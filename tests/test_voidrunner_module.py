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
    coloured = vr.paginate(groups, 10, render=lambda row, index: f"<{row}>" if index == 1 else row)
    assert coloured[0][2] == "<b1>"
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
