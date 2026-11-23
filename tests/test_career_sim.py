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


@pytest.mark.parametrize("kind", ["trader", "smuggler", "contractor", "explorer", "careful"])
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


@pytest.fixture(scope="module")
def check():
    spec = importlib.util.spec_from_file_location("check_careers_under_test", ROOT / "tools" / "check_careers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _career(pilot, worth, *, outcome="flew", towed=0, seed=1000):
    return {"pilot": pilot, "seed": seed, "outcome": outcome, "days": 100, "towed": towed,
            "marks": {"100": {"credits": worth, "worth": worth}}}


def _healthy(check):
    return [_career(pilot, baseline) for pilot, baseline in check.BASELINE.items()]


def test_the_career_check_passes_careers_like_mains(check):
    assert check.problems(_healthy(check)) == []


def test_the_career_check_fails_a_stranded_a_stuck_and_a_towed_career(check):
    careers = _healthy(check) + [_career("trader", 355_000, outcome="stranded", seed=1001),
                                 _career("trader", 355_000, outcome="stuck: 'Act'", seed=1002),
                                 _career("trader", 355_000, towed=check.MAX_TOWED + 1, seed=1003)]
    found = check.problems(careers)
    assert len(found) == 3
    assert "seed 1001: stranded" in found[0] and "seed 1002: stuck" in found[1] and "towed" in found[2]


@pytest.mark.parametrize("factor", [0.5, 2.0])
def test_the_career_check_fails_a_rule_that_halves_or_doubles_a_career(check, factor):
    careers = [dict(career) for career in _healthy(check)]
    careers[2] = _career("contractor", check.BASELINE["contractor"] * factor)
    assert [line.split(":")[0] for line in check.problems(careers)] == ["contractor"]


def test_the_career_check_lets_ordinary_tuning_through(check):
    careers = [_career(pilot, baseline * factor)
               for (pilot, baseline), factor in zip(check.BASELINE.items(), (0.8, 1.2, 0.9, 1.3, 1.1), strict=True)]
    assert check.problems(careers) == []


def test_the_career_check_reads_a_simulated_career(sim, check):
    """The keys the check reads are the ones `fly_one` writes."""
    career = _fly(sim, "explorer", days=10)
    found = check.problems([career], mark=10)
    assert found[:3] == [f"{pilot}: no careers flown" for pilot in ("trader", "smuggler", "contractor")]
    # Day 10 is far below the day-100 band, which says the worth was read.
    assert len(found) == 5 and found[3].startswith("explorer: median day-10 net worth ")
    assert found[4] == "careful: no careers flown"


def test_the_summary_says_when_each_milestone_is_first_reached(sim):
    """How long the game lasts is asked in days to a hull, a rank and a stake, not in credits."""
    careers = [sim.fly_one((DOOR, "trader", seed, 60, {}, True, True)) for seed in (1003, 1004)]
    for career in careers:
        firsts = career["firsts"]
        assert firsts["Shuttle"] == 1 and firsts["rank 0"] == 1
        assert "Freighter" in firsts and firsts["Freighter"] <= firsts.get("Carrier", 60)
        assert set(firsts) <= {"Shuttle", "rank 0", "rank 1", *sim.MILESTONES}
    rows = sim.milestone_rows(careers)
    assert rows[0].split()[0] == "pilot" and "Freighter" in rows[0] and "Cutter" not in rows[0]
    day = sim.statistics.median(career["firsts"]["Freighter"] for career in careers)
    assert rows[1].split()[:3] == ["trader", f"{day:.0f}", "(2)"]
    lines = sim.summarize(careers, 60)
    assert rows[0] in lines and "these 60 days are 7.5 real days" in lines[-1]
    assert sim.milestone_rows([dict(career, firsts={}) for career in careers]) == []


def test_jumps_per_real_day_turn_the_dispatch_over_and_translate_the_days(sim):
    slow = sim.fly_one((DOOR, "trader", 1003, 40, {}, True, True, "S", 4))
    fast = sim.fly_one((DOOR, "trader", 1003, 40, {}, True, True, "S", 40))
    assert (slow["jumps_per_day"], fast["jumps_per_day"]) == (4, 40)
    # Forty jumps at four a day are ten dispatches to fly; at forty a day there is one.
    assert slow["dispatches"] >= fast["dispatches"] and fast["dispatches"] <= 1
    assert "At 4 jumps a real day these 40 days are 10.0 real days." in sim.summarize([slow], 40)[-1]
    # Left out, a job flies at the module's own figure, as every older caller of fly_one does.
    assert sim.fly_one((DOOR, "trader", 1003, 10, {}, True, True))["jumps_per_day"] == sim.JUMPS_PER_REAL_DAY


def test_every_pilot_has_a_baseline_the_career_check_holds_it_to(sim, check):
    assert set(check.BASELINE) == set(sim.PILOTS)


def test_the_careful_pilot_leaves_a_lane_that_has_run_dry(sim):
    """A plain pilot shuttles between two drained markets for good; the careful
    one sets out for a station it has not seen lately, and gets somewhere."""
    career = sim.fly_one((DOOR, "careful", 1173, 60, {}, True, True))
    plain = sim.fly_one((DOOR, "smuggler", 1173, 60, {}, True, True))
    assert career["roamed"] >= 1 and plain["roamed"] == 0
    assert career["outcome"] == "flew" and career["hull"] != "Shuttle" and plain["hull"] == "Shuttle"
    assert career["marks"][60]["worth"] > 5 * plain["marks"][60]["worth"]


def test_a_leg_between_two_stations_is_the_plan_from_the_dock(sim):
    """The careful pilot prices the leg after next with the rule every pilot plans the next with."""
    vr = sim.load_door(DOOR)
    pilot = sim.Pilot(vr, "careful", 1003)
    world = pilot.world
    room = vr.cargo_capacity(world.save.ship)
    for sid in world.here.connections:
        dest = world.by_id[sid]
        assert pilot.plan(dest) == pilot.leg(world.here, dest, room, world.save.pilot.credits)


def test_a_pilot_told_to_works_toward_a_faction_and_joins_it(sim):
    """`--factions`: a smuggler lets beaten raiders go for the Cartel, a
    contractor hands them to the Navy, and each joins the day the door allows."""
    ways = {"factions": True}
    smuggler = sim.fly_one((DOOR, "smuggler", 1012, 30, {}, True, True, "S", 8, ways))
    assert smuggler["firsts"]["Blackwake"] <= 30 and "Concord" not in smuggler["firsts"]
    assert smuggler["earned"]["grants"] == 2000 and smuggler["standing"]["blackwake"] >= 75
    contractor = sim.fly_one((DOOR, "contractor", 1119, 45, {}, True, True, "S", 8, ways))
    assert contractor["firsts"]["Concord"] <= 45 and contractor["prizes"].get("H", 0) > 0
    # Left alone, nobody joins anything and every wreck is stripped: the plain pilots are what they were.
    plain = sim.fly_one((DOOR, "contractor", 1119, 45, {}, True, True))
    assert "Concord" not in plain["firsts"] and set(plain["prizes"]) <= {"S"} and "grants" not in plain["earned"]


def test_a_pilot_told_to_holds_a_price_the_season_will_raise(sim):
    """`--futures`: orders are placed only at a station seen before, their goods
    count toward worth while they wait, and a collected order is the next leg's cargo."""
    career = sim.fly_one((DOOR, "careful", 1001, 60, {}, True, True, "S", 8, {"futures": True}))
    orders = career["orders"]
    assert orders["placed"] >= 1 and orders.get("collected", 0) <= orders["placed"]
    assert career["outcome"] == "flew"
    lines = sim.summarize([career], 60)
    assert any(line.strip().startswith(f"futures: {orders['placed']} orders in 1 of 1 careers") for line in lines)
    assert "orders" in sim.fly_one((DOOR, "careful", 1001, 10, {}, True, True)) and not sim.fly_one(
        (DOOR, "careful", 1001, 10, {}, True, True))["orders"]


def test_checkpoints_are_each_taken_on_their_own_day(sim):
    """A faction's joining day once overwrote every later checkpoint with the last day's worth."""
    career = sim.fly_one((DOOR, "smuggler", 1012, 60, {}, True, True, "S", 8, {"factions": True}))
    worths = [career["marks"][mark]["worth"] for mark in (20, 40, 60)]
    assert len(set(worths)) == 3
