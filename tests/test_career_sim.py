"""The career simulator flies real journeys and says the same thing twice.

`tools/career_sim.py` answers balance questions about every rule together by
flying whole careers through the door's own journeys. These pin what makes its
numbers worth reading: a career repeats for a seed, every kind of pilot gets
through a stretch of days without meeting a prompt it cannot answer, a setting
reaches the build it is flown on, and the summary names every pilot it flew.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOOR = str(ROOT / "voidrunner.py")


@pytest.fixture(scope="module")
def sim():
    spec = importlib.util.spec_from_file_location("career_sim_under_test", ROOT / "tools" / "career_sim.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)
    sys.modules.pop("voidrunner_sim", None)


def _fly(sim, kind="trader", seed=1003, days=20, settings=None, dispatch=True, stakes=True):
    return sim.fly_one((DOOR, kind, seed, days, settings or {}, dispatch, stakes))


def test_a_career_flies_the_same_way_twice(sim):
    first, second = _fly(sim), _fly(sim)
    assert first == second
    assert first["outcome"] == "flew" and first["days"] >= 20
    assert set(first["marks"]) == {10, 20} and first["marks"][20]["worth"] > 1_200  # it traded up from the start


@pytest.mark.parametrize("kind", ["trader", "smuggler", "contractor", "explorer"])
def test_every_pilot_answers_every_prompt_a_month_of_journeys_raises(sim, kind):
    for seed in (1001, 1007, 1014):
        career = _fly(sim, kind, seed, days=30)
        assert career["outcome"] == "flew", career["outcome"]
        assert career["pilot"] == kind and career["seed"] == seed


def test_a_contractor_works_the_board_and_a_trader_does_not(sim):
    assert _fly(sim, "contractor", days=40)["contracts"] > 0
    assert _fly(sim, "trader", days=40)["contracts"] == 0  # until it is broke, which this one never is


def test_a_setting_reaches_the_build_the_career_is_flown_on(sim):
    door = sim.load_door(DOOR)
    sim.apply_settings(door, {"CONTRABAND_STREET_PREMIUM": "1.0", "RANK_CONTRACT_SLOTS": "[3, 3, 3, 3, 3]"})
    assert door.CONTRABAND_STREET_PREMIUM == 1.0
    assert door.RANK_CONTRACT_SLOTS == (3, 3, 3, 3, 3)  # a tuple stays one
    with pytest.raises(SystemExit):
        sim.apply_settings(door, {"NO_SUCH_RULE": "1"})


def test_the_dispatch_and_the_stakes_can_be_left_out(sim):
    quiet = _fly(sim, days=40, dispatch=False, stakes=False)
    assert quiet["dispatches"] == 0 and quiet["stakes"] == 0


def test_the_summary_has_a_row_for_each_pilot_flown(sim):
    careers = [_fly(sim, kind, 1003, days=20) for kind in ("trader", "explorer")]
    lines = sim.summarize(careers, 20)
    assert lines[0].split()[:4] == ["pilot", "day", "10", "day"]
    assert [line.split()[0] for line in lines[1:3]] == ["trader", "explorer"]
    assert not any(line.startswith("smuggler") for line in lines)


def test_a_pilot_does_with_a_beaten_raider_what_it_was_told_to(sim):
    """`--prize` is each pilot's way with a wreck; where that is not offered -- a
    warrant's target, a full hold -- it takes the scrap."""
    stripped = sim.fly_one((DOOR, "contractor", 1003, 40, {}, True, True))
    assert stripped["prizes"] and set(stripped["prizes"]) == {"S"}
    spared = sim.fly_one((DOOR, "contractor", 1003, 40, {}, True, True, "L"))
    assert spared["prizes"].get("L", 0) > 0 and set(spared["prizes"]) <= {"L", "S"}
    assert spared["outcome"] == stripped["outcome"] == "flew"
