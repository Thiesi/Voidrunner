"""NetBBS game events (door API 5, issue #25).

A NetBBS node with game events switched on for this door lists
`outbound.events` in `door_info.json`; the door then drops one JSON request
per promotion and silver or gold accolade earned live, and a Hall of Fame
standings snapshot whenever the order moves. Without that key nothing is
written at all.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from .support import DOOR_PYTHON, _VOIDRUNNER_PATH, _world_with_seed, vr

EVENTS = {"types": ["rank", "achievement", "medal", "capture", "perfect"],
          "events_per_hour": 12, "title_max": 80, "standings_rows": 10}


def door_info(tmp_path, *, events=True, user_id=77, **extra):
    outbound = {"label": "Voidrunner.door", "directory": "outbound", "results": str(tmp_path / "results")}
    if events:
        outbound["events"] = dict(EVENTS)
    info = {"door_api": 5, "user_id": user_id, "handle": "Tester", "outbound": outbound, **extra}
    path = tmp_path / "door_info.json"
    path.write_text(json.dumps(info), encoding="utf-8")
    return path, info


def requests(tmp_path) -> list[tuple[str, dict]]:
    drop = tmp_path / "outbound"
    if not drop.exists():
        return []
    return [(path.name, json.loads(path.read_text(encoding="utf-8"))) for path in sorted(drop.iterdir())]


def bound_world(tmp_path, monkeypatch, *, user_id=77, prepare=None):
    """A world saved to `tmp_path`, past its launch tick, reporting events."""
    path, info = door_info(tmp_path, user_id=user_id)
    monkeypatch.setenv("NETBBS_DOOR_INFO", str(path))
    world = _world_with_seed(42)
    if prepare is not None:
        prepare(world)
    world._checkpoint = lambda w: vr.persist(w, tmp_path, user_id)
    world.checkpoint()
    reporter = vr.GameEvents.from_door_info(info, tmp_path)
    assert reporter is not None
    reporter.bind(world)
    return world


def kinds(tmp_path) -> list:
    return [(body["event"]["type"], body["event"]["title"]) for _name, body in requests(tmp_path) if "event" in body]


# --- When to report -----------------------------------------------------------

@pytest.mark.parametrize("change", [
    lambda info: info["outbound"].pop("events"),
    lambda info: info.pop("outbound"),
    lambda info: info.update(user_id=0),
    lambda info: info.update(user_id="77"),
    lambda info: info["outbound"].update(events=True),
])
def test_no_reporter_without_events_or_an_account(tmp_path, monkeypatch, change):
    path, info = door_info(tmp_path)
    monkeypatch.setenv("NETBBS_DOOR_INFO", str(path))
    change(info)
    assert vr.GameEvents.from_door_info(info, tmp_path) is None


def test_no_reporter_without_a_netbbs_door_info(tmp_path, monkeypatch):
    # The same keys from a classic drop file or the Guest defaults mean nothing.
    _path, info = door_info(tmp_path)
    monkeypatch.delenv("NETBBS_DOOR_INFO", raising=False)
    assert vr.GameEvents.from_door_info(info, tmp_path) is None


def test_the_drop_directory_is_relative_to_door_info(tmp_path, monkeypatch):
    path, info = door_info(tmp_path)
    monkeypatch.setenv("NETBBS_DOOR_INFO", str(path))
    reporter = vr.GameEvents.from_door_info(info, tmp_path / "saves")
    assert reporter.directory == tmp_path / "outbound" and reporter.user_id == 77


# --- What is reported ---------------------------------------------------------

def test_a_live_promotion_is_reported_once_after_its_save(tmp_path, monkeypatch):
    world = bound_world(tmp_path, monkeypatch)
    world.save.pilot.credits = 25_000
    world.commit()
    # Independent Trader and Merchant Captain at once: one promotion, to the
    # rank reached, and not the rank accolades of the same names.
    assert kinds(tmp_path) == [("rank", "Merchant Captain")]
    world.commit()
    assert kinds(tmp_path) == [("rank", "Merchant Captain")]


def test_only_silver_and_gold_accolades_are_reported(tmp_path, monkeypatch):
    world = bound_world(tmp_path, monkeypatch)
    world.save.pilot.kills = 1
    world.commit()
    assert kinds(tmp_path) == []  # First Blood is bronze
    world.save.pilot.kills = 10
    world.commit()
    assert kinds(tmp_path) == [("achievement", "Raider Bane")]


def test_catch_up_unlocks_at_launch_are_not_reported(tmp_path, monkeypatch):
    def veteran(world):
        world.save.pilot.kills = 12
        world.save.turn = 150
        world.save.pilot.credits = 25_000

    world = bound_world(tmp_path, monkeypatch, prepare=veteran)
    assert {"kills_10", "hops_100"} <= set(world.save.accolades["unlocked"])
    world.commit()
    assert kinds(tmp_path) == []


def test_a_failed_save_reports_nothing(tmp_path, monkeypatch):
    world = bound_world(tmp_path, monkeypatch)
    world.save.pilot.kills = 10

    def fail(_world):
        raise OSError("disk full")

    world._checkpoint = fail
    with pytest.raises(vr.SaveError):
        world.commit()
    assert kinds(tmp_path) == []


def test_types_the_node_does_not_list_are_not_sent(tmp_path, monkeypatch):
    world = bound_world(tmp_path, monkeypatch)
    world.reporter.__self__.types = ["achievement"]
    world.save.pilot.credits = 25_000
    world.save.pilot.kills = 10
    world.commit()
    assert kinds(tmp_path) == [("achievement", "Raider Bane")]


def test_requests_match_the_contract(tmp_path, monkeypatch):
    world = bound_world(tmp_path, monkeypatch)
    world.save.pilot.kills = 10
    world.commit()
    world.save.pilot.credits = 25_000
    world.commit()
    found = requests(tmp_path)
    names = [name for name, _body in found]
    # Zero-padded, so name order is the order written; no temporary is left.
    assert names == sorted(names) and all(name.startswith("voidrunner-") and name.endswith(".json") for name in names)
    assert all(len((tmp_path / "outbound" / name).read_bytes()) <= 2048 for name, body in found if "event" in body)
    for _name, body in found:
        assert len(body) == 1
        if "event" in body:
            assert body["event"]["user_id"] == 77 and set(body["event"]) == {"type", "user_id", "title"}
            assert 0 < len(body["event"]["title"]) <= 80


# --- Standings ----------------------------------------------------------------

def standings(tmp_path) -> list[dict]:
    return [body["standings"] for _name, body in requests(tmp_path) if "standings" in body]


def test_standings_follow_the_hall_of_fame(tmp_path, monkeypatch):
    vr._write_json_atomic(tmp_path / "scores" / "12.json", {
        "user_id": 12, "handle": "Vex", "best_credits": 182_400, "rank": "Legend of the Frontier",
        "retirements": 0, "kills": 0, "missions_completed": 0})
    world = bound_world(tmp_path, monkeypatch)
    sent = standings(tmp_path)
    # Sent once at bind, so `!top` has a table before anything moves.
    assert len(sent) == 1 and sent[0]["title"] == "Hall of Fame"
    assert sent[0]["rows"][0] == {"user_id": 12, "value": "182,400 cr", "label": "Legend of the Frontier"}
    assert [row["user_id"] for row in sent[0]["rows"]] == [12, 77]
    assert sent[0]["rows"] == vr.GameEvents.standings_rows(world.reporter.__self__)
    # A commit that changes nothing sends nothing; one that overtakes does.
    world.commit()
    assert len(standings(tmp_path)) == 1
    world.save.pilot.credits = 200_000
    world.commit()
    latest = standings(tmp_path)[-1]
    assert [row["user_id"] for row in latest["rows"]] == [77, 12]
    assert latest["rows"][0]["value"] == "200,000 cr"


def test_standings_keep_the_node_row_limit(tmp_path, monkeypatch):
    for user_id in range(1, 16):
        vr._write_json_atomic(tmp_path / "scores" / f"{user_id}.json", {
            "user_id": user_id, "handle": f"P{user_id}", "best_credits": 1000 * user_id, "rank": "Rookie Hauler",
            "retirements": 0, "kills": 0, "missions_completed": 0})
    bound_world(tmp_path, monkeypatch, user_id=99)
    rows = standings(tmp_path)[0]["rows"]
    assert len(rows) == 10 and rows[0]["user_id"] == 15
    assert len({row["user_id"] for row in rows}) == 10


# --- The shipped door ---------------------------------------------------------

def run_door(tmp_path, commands: bytes, *, events=True):
    path, _info = door_info(tmp_path, events=events)
    return subprocess.run([DOOR_PYTHON, str(_VOIDRUNNER_PATH)], input=commands, capture_output=True, timeout=60,
                          env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path / "saves"),
                                   NETBBS_DOOR_INFO=str(path), DOOR_SPLASH="0", DOOR_SCENES="0"))


def veteran_save(tmp_path):
    world = _world_with_seed(42)
    world.save.pilot.kills = 12
    world.save.turn = 150
    vr.write_save(tmp_path / "saves", 77, world.save)


def test_the_door_reports_standings_but_not_history(tmp_path):
    veteran_save(tmp_path)
    result = run_door(tmp_path, b"Q")
    assert result.returncode == 0, result.stderr
    # Raider Bane and Long Hauler were met before this build opened the
    # career: unlocked and shown on the deck, never announced.
    assert kinds(tmp_path) == []
    rows = standings(tmp_path)[0]["rows"]
    assert [row["user_id"] for row in rows] == [77]


def test_the_door_writes_nothing_without_events(tmp_path):
    veteran_save(tmp_path)
    result = run_door(tmp_path, b"Q", events=False)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "outbound").exists()


def test_retirement_earnings_are_reported_through_the_hand_off(tmp_path, monkeypatch):
    # The finale screen's fresh world has no reporter; it hands its earnings
    # to `report_earned` once the retiring save succeeded.
    world = bound_world(tmp_path, monkeypatch)
    finale = next(a for a in vr.ACCOLADES if a.id == "finale_legend")
    bronze = next(a for a in vr.ACCOLADES if a.id == "retire_1")
    world.report_earned([], [bronze, finale])
    assert kinds(tmp_path) == [("achievement", "Frontier Legend")]
