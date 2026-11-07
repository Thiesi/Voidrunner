"""Keep local test scratch bounded (README, "Local disk hygiene").

A sibling project on this host once let test and proof scratch pile up to
several million files, a new folder for every run and none ever deleted, and
the host went down; recovery took a day and an offline disk check. Moving the
pile to the system temp folder only moved it: there it passed half a million
entries and had to be cleared by hand.

Voidrunner had the same pattern on a small scale: one test made a fresh
`tempfile.mkdtemp()` folder on every run and never removed it. These checks run
before any test so it cannot grow back unnoticed:

- an explicit --basetemp inside the checkout must be `.test-tmp/pytest`, the
  one fixed name, so each run replaces the last;
- the checkout's ignored scratch (`.test-tmp`, `.pytest_cache`, `build`) must
  hold at most SCRATCH_LIMIT files. Above that, run
  `python tools/clean_scratch.py` first;
- this project's own entries in the system temp folder (`voidrunner-*`) must
  hold at most SCRATCH_LIMIT files. Above that, run
  `python tools/clean_scratch.py --temp` first.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

SCRATCH_DIRS = ('.test-tmp', '.pytest_cache', 'build')
LOCAL_BASETEMP = Path('.test-tmp') / 'pytest'
DEFAULT_LIMIT = 50_000
# pytest-of-* is left out: pytest bounds it itself (pyproject.toml keeps only
# the last run's failed tests), and it is shared with every other project.
TEMP_PATTERNS = ('voidrunner-*',)


def scratch_limit() -> int:
    return int(os.environ.get('VOIDRUNNER_SCRATCH_LIMIT', DEFAULT_LIMIT))


def count_files(root: Path, stop_after: int) -> int:
    """Files under root, counting at most stop_after + 1 so a huge tree stays cheap."""
    count, pending = 0, [root]
    while pending:
        try:
            entries = os.scandir(pending.pop())
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                        continue
                except OSError:
                    continue
                count += 1
                if count > stop_after:
                    return count
    return count


def basetemp_problem(basetemp: str | os.PathLike[str] | None, root: Path) -> str | None:
    if not basetemp:
        return None
    path, root = Path(basetemp).resolve(), root.resolve()
    if not path.is_relative_to(root) or path == (root / LOCAL_BASETEMP).resolve():
        return None
    return (f'--basetemp {path} is inside the checkout under a name that is never cleaned. '
            f'Omit --basetemp (pytest then uses the system temp folder and keeps only failed tests) '
            f'or use --basetemp={LOCAL_BASETEMP.as_posix()}, which every run replaces.')


def scratch_problem(root: Path, limit: int | None = None) -> str | None:
    limit = scratch_limit() if limit is None else limit
    total = 0
    for name in SCRATCH_DIRS:
        if (root / name).is_dir():
            total += count_files(root / name, limit - total)
            if total > limit:
                return (f'This checkout holds more than {limit:,} scratch files in '
                        f'{", ".join(SCRATCH_DIRS)}. Run `python tools/clean_scratch.py` '
                        f'(keep any gallery you still need somewhere else first). '
                        f'VOIDRUNNER_SCRATCH_LIMIT raises the limit for one run.')
    return None


def temp_folders(temp: Path | None = None) -> list[Path]:
    temp = Path(tempfile.gettempdir()) if temp is None else temp
    return sorted(path for pattern in TEMP_PATTERNS for path in temp.glob(pattern))


def temp_files(temp: Path | None = None, stop_after: int = 10**9) -> int:
    total = 0
    for path in temp_folders(temp):
        # A link counts as one entry: its target isn't this project's scratch.
        total += count_files(path, stop_after - total) if path.is_dir() and not path.is_symlink() else 1
        if total > stop_after:
            break
    return total


def temp_problem(temp: Path | None = None, limit: int | None = None) -> str | None:
    limit = scratch_limit() if limit is None else limit
    if temp_files(temp, limit) > limit:
        return (f'This project holds more than {limit:,} files in the system temp folder '
                f'({", ".join(TEMP_PATTERNS)}). The temp folder is not storage, and past half a million '
                f'entries it has taken this host down. Run `python tools/clean_scratch.py --temp`. '
                f'That keeps entries written to in the last 24 hours; once no gallery build is running, '
                f'`--temp --older-than 0` removes them too. '
                f'VOIDRUNNER_SCRATCH_LIMIT raises the limit for one run.')
    return None
