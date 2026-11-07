"""The local-scratch guards (tests/scratch_guard.py, tools/clean_scratch.py)."""

from __future__ import annotations

import os
import time

from . import scratch_guard as guard
from tools import clean_scratch


def _files(folder, n):
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (folder / f"{i}.txt").write_text("x", encoding="utf-8")


def test_basetemp_outside_the_checkout_or_at_the_fixed_name_is_allowed(tmp_path):
    root = tmp_path / "repo"
    assert guard.basetemp_problem(None, root) is None
    assert guard.basetemp_problem(tmp_path / "elsewhere", root) is None
    assert guard.basetemp_problem(root / ".test-tmp" / "pytest", root) is None


def test_a_fresh_basetemp_inside_the_checkout_is_refused(tmp_path):
    root = tmp_path / "repo"
    problem = guard.basetemp_problem(root / ".test-tmp" / "run-42", root)
    assert problem and ".test-tmp/pytest" in problem


def test_checkout_scratch_over_the_limit_is_refused(tmp_path):
    _files(tmp_path / ".test-tmp" / "a", 3)
    _files(tmp_path / "build" / "gallery", 3)
    assert guard.scratch_problem(tmp_path, limit=6) is None
    problem = guard.scratch_problem(tmp_path, limit=5)
    assert problem and "tools/clean_scratch.py" in problem


def test_only_this_projects_temp_entries_count(tmp_path):
    _files(tmp_path / "voidrunner-gallery-run-x", 4)
    _files(tmp_path / "someone-else", 100)
    assert guard.temp_files(tmp_path) == 4
    assert guard.temp_problem(tmp_path, limit=4) is None
    assert "--temp" in guard.temp_problem(tmp_path, limit=3)


def test_temp_cleanup_keeps_recent_entries_and_leaves_others_alone(tmp_path):
    stale, fresh, other = (tmp_path / n for n in ("voidrunner-old", "voidrunner-new", "unrelated"))
    for folder in (stale, fresh, other):
        _files(folder, 2)
    day_ago = time.time() - 2 * 86400
    for path in [stale, *stale.iterdir()]:
        os.utime(path, (day_ago, day_ago))
    assert clean_scratch.clean_temp(tmp_path) == 0
    assert not stale.exists() and fresh.exists() and other.exists()
    assert clean_scratch.clean_temp(tmp_path, hours=0) == 0
    assert not fresh.exists() and other.exists()


def test_checkout_cleanup_removes_only_ignored_scratch(tmp_path):
    _files(tmp_path / ".test-tmp" / "pytest", 2)
    _files(tmp_path / "build" / "gallery", 2)
    _files(tmp_path / "tests" / "__pycache__", 2)
    _files(tmp_path / ".venv" / "lib", 2)
    (tmp_path / "voidrunner.py").write_text("pass", encoding="utf-8")
    assert clean_scratch.clean(tmp_path) == 0
    assert not (tmp_path / ".test-tmp").exists() and not (tmp_path / "build").exists()
    assert not (tmp_path / "tests" / "__pycache__").exists()
    assert (tmp_path / ".venv" / "lib" / "0.txt").exists() and (tmp_path / "voidrunner.py").exists()
