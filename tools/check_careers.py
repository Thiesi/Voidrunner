"""Fail when whole careers flown by `career_sim.py` went somewhere they should not.

    python tools/career_sim.py --seeds 50 --json build/careers.json
    python tools/check_careers.py build/careers.json

The suite tests rules one at a time, and `tests/test_career_sim.py` flies a
month per pilot to prove that a pilot can answer every prompt. Neither sees
what only shows across many careers: seven careers in 800 spent their run
being towed home before #90, and a rule that halves what a career earns
passes every unit test. This reads the simulator's records and fails on:

- a career that did not fly its days (`stranded`, or `stuck` at a prompt);
- a career towed home more than `MAX_TOWED` times;
- a pilot whose median day-100 net worth left the band around `BASELINE`.

The band is wide on purpose (`BAND`): a rule that halves or doubles a career
fails loudly, while ordinary tuning moves a median by a few per cent and does
not. When a change moves a pilot on purpose, update `BASELINE` in the same
pull request and quote the simulator's table there.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys

#: Median day-100 net worth per pilot on `main` when this check was written
#: (`career_sim.py --seeds 50`, 2026-11-23).
BASELINE = {"trader": 355_000, "smuggler": 387_000, "contractor": 257_000, "explorer": 340_000,
            "careful": 358_000}
#: How far a median may move from its baseline, as factors: half fails, double fails.
BAND = (0.6, 1.6)
#: Tows a career may need; it is 0 in every career on `main`.
MAX_TOWED = 2
#: The day whose net worth is compared.
MARK = 100


def problems(careers: list[dict], *, mark: int = MARK) -> list[str]:
    found = []
    for career in careers:
        name = f"{career.get('pilot')} seed {career.get('seed')}"
        if career["outcome"] != "flew":
            found.append(f"{name}: {career['outcome']} on day {career['days']}")
        if career["towed"] > MAX_TOWED:
            found.append(f"{name}: towed home {career['towed']} times (at most {MAX_TOWED})")
    low, high = BAND
    for pilot, baseline in BASELINE.items():
        flown = [career for career in careers if career.get("pilot") == pilot]
        if not flown:
            found.append(f"{pilot}: no careers flown")
            continue
        # JSON keys are strings; a record straight from `fly_one` has ints.
        worth = [(career["marks"].get(str(mark)) or career["marks"].get(mark) or {}).get("worth")
                 for career in flown]
        reached = [value for value in worth if value is not None]
        if len(reached) < len(flown):
            found.append(f"{pilot}: {len(flown) - len(reached)} of {len(flown)} careers did not reach day {mark}")
        if not reached:
            continue
        median = statistics.median(reached)
        if not low * baseline <= median <= high * baseline:
            found.append(f"{pilot}: median day-{mark} net worth {median:,.0f} is outside "
                         f"{low * baseline:,.0f} to {high * baseline:,.0f} (baseline {baseline:,})")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("careers", type=pathlib.Path, help="the file `career_sim.py --json` wrote")
    args = parser.parse_args()
    careers = json.loads(args.careers.read_text(encoding="utf-8"))
    found = problems(careers)
    for line in found:
        print(line)
    print(f"{len(careers)} careers, {len(found)} problem(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
