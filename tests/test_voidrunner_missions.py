"""Contracts: the board, acceptance, navigation, escorts and the
opening assignment.

Split out of `test_voidrunner_domain.py` (issue #422).
"""

from __future__ import annotations

import contextlib
import io
import random
import sys

import pytest

from .support import _VOIDRUNNER_PATH, _add_cargo, _door_stopped_at, _escort_world, _mission_details_world, _post_and_accept_test_mission, _set_cargo, _world_with_pending_fight, _world_with_seed, vr


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
def test_mission_navigation_pages_preserve_chart_and_career(monkeypatch, terminal, active, width, height):
    import copy, re
    world, mission = _mission_details_world("scan")
    if active: vr.accept_mission(world, mission)
    for sid in vr.mission_route(world, mission): world.by_id[sid].discovered = False
    before = copy.deepcopy(world.save.to_dict()); rng = world.event_rng.getstate()
    for boundary in ("checkpoint", "commit", "advance_station_state"):
        # Every write path, not just the historical name (#417 review).
        monkeypatch.setattr(world, boundary, lambda: pytest.fail("Route view saved"))
    terminal(width, height)
    output = io.StringIO(); frames=[]
    def choose():
        frame=output.getvalue(); frames.append(frame); output.seek(0); output.truncate(0)
        match=re.search(r"Contract Route #1 (\d+)/(\d+)", " ".join(frame.split()))
        assert match and len(frames) < 200
        return "B" if match[1] == match[2] else "N"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output): vr.screen_mission_navigation(vr.Palette(False),world,mission,active=active)
    assert all(len(frame.splitlines()) <= height for frame in frames)
    assert all(vr._visible_width(line) <= width for frame in frames for line in frame.splitlines())
    text=" ".join(" ".join(frames).split())
    assert world.by_id[mission.target_system].name in text and "danger unknown" in text
    for sid in vr.mission_route(world,mission)[:-1]: assert world.by_id[sid].name not in text
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng
    assert not world.by_id[mission.target_system].discovered


@pytest.mark.parametrize("fault", ["inactive","expired","fuel","at_target","pending","boolean_id"])
def test_mission_navigation_invalid_departure_changes_nothing(fault):
    import copy
    world,mission=_mission_details_world()
    if fault != "inactive": vr.accept_mission(world,mission)
    if fault == "expired": world.save.turn=mission.deadline_turn+1
    if fault == "fuel": world.save.ship.fuel=0
    if fault == "at_target": world.save.current_system=mission.target_system
    if fault == "pending": world.save.pending_travel={"existing":True}
    before=copy.deepcopy(world.save.to_dict()); rng=world.event_rng.getstate()
    with pytest.raises(vr.MissionError): vr.prepare_mission_jump(world,True if fault=="boolean_id" else mission.id)
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng


def test_mission_navigation_posted_route_cannot_jump_or_accept(monkeypatch):
    import copy
    world,mission=_mission_details_world(); before=copy.deepcopy(world.save.to_dict())
    keys=iter("JB"); monkeypatch.setattr(vr,"read_key",lambda:next(keys))
    monkeypatch.setattr(vr,"screen_travel",lambda *args:pytest.fail("Posted route launched travel"))
    with contextlib.redirect_stdout(io.StringIO()): vr.screen_mission_navigation(vr.Palette(False),world,mission,active=False)
    assert world.save.to_dict()==before and world.save.tracked_mission_id is None


@pytest.mark.parametrize("outcome", ["arrive","divert","fail"])
def test_mission_navigation_tracks_before_one_hop_and_recalculates_after_it(monkeypatch,outcome):
    world,mission=_mission_details_world(); vr.accept_mission(world,mission)
    first=vr.mission_route(world,mission)[0]; calls=[]; checkpoints=[]
    world._checkpoint=lambda current:checkpoints.append((current.save.turn,current.save.tracked_mission_id))
    def travel(p,current,destination):
        assert checkpoints[0]==(0,mission.id)
        calls.append(destination); current.save.turn+=1
        current.save.current_system=0 if outcome=="divert" else destination
        if outcome=="fail": current.save.active_missions.clear()
    monkeypatch.setattr(vr,"screen_travel",travel)
    keys=iter("JJB" if outcome=="fail" else "JB"); monkeypatch.setattr(vr,"read_key",lambda:next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output: vr.screen_mission_navigation(vr.Palette(False),world,mission,active=True)
    assert calls==[first] and world.save.turn==1
    if outcome=="divert": assert "Travel diverted" in output.getvalue()
    elif outcome=="fail": assert "no longer active" in output.getvalue() and world.save.tracked_mission_id is None
    else: assert "Last hop: arrived" in output.getvalue() and world.save.tracked_mission_id==mission.id


def test_mission_navigation_save_failure_stops_before_departure(monkeypatch):
    world,mission=_mission_details_world(); vr.accept_mission(world,mission)
    world._checkpoint=lambda current:(_ for _ in ()).throw(OSError("disk full"))
    monkeypatch.setattr(vr,"read_key",lambda:"J")
    monkeypatch.setattr(vr,"screen_travel",lambda *args:pytest.fail("Departed after save failure"))
    with contextlib.redirect_stdout(io.StringIO()),pytest.raises(vr.SaveError):
        vr.screen_mission_navigation(vr.Palette(False),world,mission,active=True)
    assert world.save.turn==0 and world.save.pending_travel is None


def test_mission_navigation_details_and_tracked_chart_open_same_contract(monkeypatch):
    world,mission=_mission_details_world(); vr.accept_mission(world,mission); vr.track_mission(world,mission.id)
    opened=[]; monkeypatch.setattr(vr,"screen_mission_navigation",lambda p,w,m,active:opened.append((m.id,active)))
    keys=iter("RBRQ"); monkeypatch.setattr(vr,"read_key",lambda:next(keys))
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_mission_details(vr.Palette(False),world,mission,active=True)
        vr.screen_chart(vr.Palette(False),world)
    assert opened==[(mission.id,True),(mission.id,True)]
    assert "R" not in vr.CHART_CONNECTION_LETTERS and "Q" not in vr.CHART_CONNECTION_LETTERS


def test_mission_navigation_budget_includes_bounty_reentry_and_manual_refuelling():
    world,mission=_mission_details_world("bounty"); vr.accept_mission(world,mission)
    world.save.current_system=mission.target_system; world.save.ship.fuel=0; world.save.ship.has_gunner=True
    world.save.turn=mission.deadline_turn-1
    lines=vr.mission_navigation_lines(world,mission,active=True); text=" ".join(lines)
    assert "Route: 2 jumps" in text and "wages 30cr" in text
    assert "Refuelling is manual" in text and "MISSES deadline" in text
    assert "leave/re-enter" in text


def test_mission_navigation_procurement_and_posted_escort_obligations():
    world, mission = _mission_details_world("delivery")
    world.here.economy = "Industrial"
    mission.commodity = "weapons"
    text = " ".join(vr.mission_navigation_lines(world, mission, active=False))
    assert "prohibits buying" in text and "spot stock" not in text
    mission.kind = "escort"
    text = " ".join(vr.mission_navigation_lines(world, mission, active=False))
    assert "EVERY jump" in text and "including detours" in text


def test_mission_navigation_large_legacy_queue_budgets_actual_page_number_width(monkeypatch, terminal):
    world,mission=_mission_details_world("bounty")
    world.save.active_missions=[vr.Mission(i,"bounty","Legacy bounty",500,0,mission.target_system,pirate_tier=1) for i in range(1,1002)]
    mission=world.save.active_missions[-1]
    terminal(20, 10)
    title=f"Contract Route #{mission.id}"; footer="[J] Jump next [N] Next [P] Prev [B] Back: "
    pages=vr._trade_pages(vr.mission_navigation_lines(world,mission,active=True),title,footer)
    assert len(pages)>999
    for index in (0,len(pages)-1):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            vr.out_line(); vr.out_line(f"{title} {index+1}/{len(pages)}")
            for line in pages[index]: vr.out_line(line)
            vr.out_prompt(footer)
        assert len(output.getvalue().splitlines())<=10
        assert all(vr._visible_width(line)<=20 for line in output.getvalue().splitlines())


@pytest.mark.parametrize("active,commands", [(False,b"B1RBBBQ"),(False,b"B1R"),(True,b"CRBQQ"),(True,b"CR")])
def test_real_mission_navigation_back_and_eof_preserve_career(tmp_path,active,commands):
    import json,os,subprocess
    world,mission=_mission_details_world("scan")
    if active:
        vr.accept_mission(world,mission); vr.track_mission(world,mission.id)
    world._checkpoint=lambda current:vr.persist(current,tmp_path,77); world.checkpoint()
    original=(tmp_path/"77.json").read_bytes()
    info=tmp_path/"door_info.json"; info.write_text(json.dumps({"user_id":77,"handle":"Tester"}),encoding="utf-8")
    result=subprocess.run([sys.executable,str(_VOIDRUNNER_PATH)],input=commands,capture_output=True,
        env=dict(os.environ,VOIDRUNNER_SAVE_DIR=str(tmp_path),NETBBS_DOOR_INFO=str(info)),timeout=10)
    assert result.returncode==0 and not result.stderr and b"Contract Route #1" in result.stdout
    assert (tmp_path/"77.json").read_bytes()==original
    assert not vr.World(vr.load_or_create_save(tmp_path,77,"Tester")[0]).by_id[mission.target_system].discovered


@pytest.mark.parametrize("commands", [b"CRJ", b"CGD1J"])
def test_real_mission_navigation_completes_delivery_once_before_retained_result(tmp_path, commands):
    import json,os,subprocess
    world=_world_with_seed(42); world.event_rng.seed(0)
    destination=sorted(world.here.connections)[0]
    mission=vr.Mission(1,"delivery","Navigation delivery",500,0,destination,commodity="food",quantity=3)
    world.save.active_missions=[mission]; world.save.tracked_mission_id=1; _set_cargo(world, {"food":3})
    if commands.startswith(b"CG"):
        for station in world.galaxy: station.discovered = station.id in (0, destination)
        world.sync_discovered()
    world._checkpoint=lambda current:vr.persist(current,tmp_path,77); world.checkpoint()
    credits=world.save.pilot.credits
    with _door_stopped_at(tmp_path,commands,b"Last hop: arrived"):
        saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
        assert saved.current_system==destination and saved.turn==1 and saved.pending_travel is None
        assert saved.pilot.credits==credits+500 and not saved.active_missions and not saved.cargo
        assert saved.tracked_mission_id is None
    info=tmp_path/"door_info.json"
    result=subprocess.run([sys.executable,str(_VOIDRUNNER_PATH)],input=b"Q",capture_output=True,
        env=dict(os.environ,VOIDRUNNER_SAVE_DIR=str(tmp_path),NETBBS_DOOR_INFO=str(info)),timeout=10)
    assert result.returncode==0 and not result.stderr
    resumed,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
    assert resumed.pilot.credits==saved.pilot.credits and resumed.turn==1


def test_delivery_mission_completes_on_arrival_with_enough_cargo():
    world = _world_with_seed(4)
    dest = world.here.connections[0]
    mission = vr.Mission(id=1, kind="delivery", description="test delivery", reward=500,
                          origin_system=world.save.current_system, target_system=dest,
                          commodity="food", quantity=3, deadline_turn=None)
    _post_and_accept_test_mission(world, mission)
    _add_cargo(world, "food", 5)
    world.save.current_system = dest

    messages = vr.check_mission_completions(world)

    assert any("Mission complete" in m for m in messages)
    assert world.save.cargo["food"] == 2
    assert world.save.pilot.credits == 1200 + 500
    assert mission not in world.save.active_missions


def test_delivery_mission_does_not_complete_with_insufficient_cargo():
    world = _world_with_seed(5)
    dest = world.here.connections[0]
    mission = vr.Mission(id=1, kind="delivery", description="test delivery", reward=500,
                          origin_system=world.save.current_system, target_system=dest,
                          commodity="food", quantity=3, deadline_turn=None)
    _post_and_accept_test_mission(world, mission)
    _add_cargo(world, "food", 1)
    world.save.current_system = dest

    messages = vr.check_mission_completions(world)

    assert messages == []
    assert mission in world.save.active_missions


def test_expired_mission_is_dropped_with_a_message():
    world = _world_with_seed(6)
    mission = vr.Mission(id=1, kind="delivery", description="late delivery", reward=500,
                          origin_system=world.save.current_system, target_system=999,
                          commodity="food", quantity=3, deadline_turn=0)
    _post_and_accept_test_mission(world, mission)
    world.save.turn = 10

    messages = vr.check_mission_completions(world)

    assert any("expired" in m for m in messages)
    assert mission not in world.save.active_missions


def test_scan_mission_completes_when_target_system_is_discovered():
    world = _world_with_seed(8)
    mission = vr.Mission(id=1, kind="scan", description="survey", reward=200,
                          origin_system=world.save.current_system, target_system=17,
                          deadline_turn=None)
    _post_and_accept_test_mission(world, mission)

    messages = vr.check_mission_completions(world, just_discovered=17)

    assert any("Mission complete" in m for m in messages)
    assert world.save.pilot.credits == 1200 + 200


def _accepted_escort_mission(world, dest_id, tier=1):
    hops = vr.bfs_hops(world.by_id, 0)
    mission = vr.Mission(id=world.save.next_mission_id, kind="escort", description="Escort a supply convoy to Somewhere",
                          reward=500, origin_system=0, target_system=dest_id, pirate_tier=tier,
                          deadline_turn=world.save.turn + 50)
    _post_and_accept_test_mission(world, mission)
    return mission


def test_generate_mission_board_can_include_escort_missions():
    world = _world_with_seed(132)
    world.event_rng = random.Random(0)
    found = False
    for seed in range(200):
        world.save.turn = seed * vr.MISSION_BOARD_DAYS
        world.event_rng = random.Random(seed)
        board = vr.generate_mission_board(world)
        if any(m.kind == "escort" for m in board):
            found = True
            break
    assert found


def test_generate_escort_mission_spans_at_least_two_hops():
    world = _world_with_seed(133)
    hops = vr.bfs_hops(world.by_id, world.save.current_system)
    mission = vr._generate_mission(world, "escort", hops)
    assert mission is not None
    assert hops[mission.target_system] >= 2


def test_escort_wave_fires_on_every_hop_while_active(monkeypatch):
    world = _world_with_seed(134)
    dest = next(sid for sid in world.by_id[0].connections)
    far_dest = next(sid for sid, h in vr.bfs_hops(world.by_id, 0).items() if h >= 2)
    mission = _accepted_escort_mission(world, far_dest)

    calls = []
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: calls.append(pirate) or "won")
    world.event_rng.random = lambda: 1.0  # suppress ordinary encounters

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest)

    assert len(calls) == 1  # the wave fired this hop
    assert mission in world.save.active_missions  # not yet at target -- still active


def test_escort_completes_and_pays_out_on_arrival_at_target(monkeypatch):
    world = _world_with_seed(135)
    dest = next(sid for sid in world.by_id[0].connections)
    mission = _accepted_escort_mission(world, dest)
    before_credits = world.save.pilot.credits

    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "won")
    world.event_rng.random = lambda: 1.0

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest)

    assert mission not in world.save.active_missions
    assert world.save.pilot.credits == before_credits + mission.reward


def test_escort_fails_on_ship_loss(monkeypatch):
    world = _world_with_seed(136)
    dest = next(sid for sid in world.by_id[0].connections)
    mission = _accepted_escort_mission(world, dest)

    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "destroyed")
    world.event_rng.random = lambda: 1.0

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest)

    assert mission not in world.save.active_missions


def test_escort_fails_on_evasion_even_though_ship_survives(monkeypatch):
    """Evading protects the player's own ship, but the convoy is left
    behind either way -- the contract still fails, unlike a bounty's own
    "escaped" outcome, which leaves the mission active to retry."""
    world = _world_with_seed(137)
    dest = next(sid for sid in world.by_id[0].connections)
    mission = _accepted_escort_mission(world, dest)

    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "escaped")
    world.event_rng.random = lambda: 1.0

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest)

    assert mission not in world.save.active_missions
    assert "failed" in buf.getvalue().lower()


def test_multiple_escort_missions_each_get_their_own_wave(monkeypatch):
    world = _world_with_seed(138)
    dest = next(sid for sid in world.by_id[0].connections)
    m1 = _accepted_escort_mission(world, dest)
    far_dest = next(sid for sid, h in vr.bfs_hops(world.by_id, 0).items() if h >= 2 and sid != dest)
    m2 = _accepted_escort_mission(world, far_dest)

    calls = []
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: calls.append(pirate) or "won")
    world.event_rng.random = lambda: 1.0

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(truecolor=False), world, dest)

    assert len(calls) == 2
    assert m1 not in world.save.active_missions  # completed -- arrived at its target
    assert m2 in world.save.active_missions  # still en route


def test_scan_checkpoint_contains_discovery_and_mission_reward(tmp_path, monkeypatch):
    world = _world_with_seed(42)
    world.save.ship.scanner_tier = 1
    target = next(sid for sid, hops in vr.bfs_hops(world.by_id, 0).items()
                  if hops <= 3 and not world.by_id[sid].discovered)
    world.save.active_missions = [vr.Mission(1, "scan", "Survey", 500, 0, target)]
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    keys = iter(["S", "B"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()):
        vr._do_scan(vr.Palette(False), world)
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert target in saved.discovered
    assert saved.pilot.credits == 1700
    assert saved.active_missions == []


@pytest.mark.parametrize("kind", ["bounty", "escort"])
@pytest.mark.parametrize("field,value", [
    ("pirate_tier", "2"), ("pirate_tier", 5), ("pirate_tier", True),
    ("reward", "500"), ("reward", -1), ("id", False), ("id", 0),
    ("origin_system", -1), ("target_system", vr.GALAXY_SYSTEM_COUNT),
    ("target_system", "1"), ("description", []), ("quantity", "2"),
    ("deadline_turn", -1), ("commodity", []), ("commodity", "unknown"),
])
def test_malformed_resume_mission_uses_recovery_error(kind, field, value):
    mission = vr.Mission(1, kind, "Test contract", 500, 0, 1, pirate_tier=2).to_dict()
    mission[field] = value
    travel = {
        "version": 1, "origin": 0, "destination": 1, "was_discovered": True,
        "destroyed": False, "phase": "primary", "primary": "bounty" if kind == "bounty" else "random",
        "bounty": mission if kind == "bounty" else None,
        "escorts": [mission] if kind == "escort" else [], "escort_index": 0, "encounter": {},
    }
    data = _world_with_seed(42).save.to_dict()
    data["pending_travel"] = travel
    with pytest.raises(vr.ResumeError, match="cannot be read"):
        vr.SaveData.from_dict(data)


@pytest.mark.parametrize("kind", ["bounty", "escort"])
def test_real_door_preserves_bad_resume_mission_and_shows_recovery(tmp_path, kind):
    import json
    import os
    import subprocess

    world = _world_with_seed(42)
    mission = vr.Mission(1, kind, "Test contract", 500, 0, 1, pirate_tier=2).to_dict()
    mission["pirate_tier"] = "2"
    world.save.pending_travel = {
        "version": 1, "origin": 0, "destination": 1, "was_discovered": True,
        "destroyed": False, "phase": "primary", "primary": "bounty" if kind == "bounty" else "random",
        "bounty": mission if kind == "bounty" else None,
        "escorts": [mission] if kind == "escort" else [], "escort_index": 0, "encounter": {},
    }
    # Simulate on-disk damage; the production writer now rejects this state.
    world.save.event_rng_state = world.event_rng.getstate()
    path = tmp_path / "77.json"
    path.write_text(json.dumps(world.save.to_dict()), encoding="utf-8")
    original = path.read_bytes()
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_VOIDRUNNER_PATH)], input=b" ", capture_output=True,
        env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)), timeout=10,
    )
    assert result.returncode == 1
    output = " ".join(vr._ANSI_RE.sub("", result.stdout.decode("utf-8")).split())
    assert "your saved career is unchanged" in output
    assert not result.stderr
    assert path.read_bytes() == original


def test_posted_missions_survive_reopen_restart_and_acceptance(tmp_path):
    world = _world_with_seed(42)
    first = vr.generate_mission_board(world)
    rng = world.event_rng.getstate()
    assert vr.generate_mission_board(world) == first
    assert world.event_rng.getstate() == rng
    accepted = first[0]
    vr.accept_mission(world, accepted)
    vr.persist(world, tmp_path, 77)
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    restarted = vr.World(saved)
    assert vr.generate_mission_board(restarted) == first[1:]
    assert restarted.save.active_missions == [accepted]
    with pytest.raises(vr.MissionError):
        vr.accept_mission(restarted, accepted)


def test_board_refresh_boundary_and_stale_offer_rejection():
    world = _world_with_seed(42)
    first = vr.generate_mission_board(world)
    world.save.turn += vr.MISSION_BOARD_DAYS - 1
    assert vr.generate_mission_board(world) == first
    world.save.turn += 1
    fresh = vr.generate_mission_board(world)
    assert not {m.id for m in first} & {m.id for m in fresh}
    before = __import__("copy").deepcopy(world.save.to_dict())
    with pytest.raises(vr.MissionError, match="no longer posted"):
        vr.accept_mission(world, first[0])
    assert world.save.to_dict() == before


def test_exhausted_board_does_not_refill_before_refresh():
    world = _world_with_seed(42)
    board = vr.generate_mission_board(world)
    for mission in board:
        vr.accept_mission(world, mission)
        world.save.active_missions.clear()  # completed jobs must not refill offers
    assert vr.generate_mission_board(world) == []
    world.save.turn += vr.MISSION_BOARD_DAYS
    assert vr.generate_mission_board(world)


def test_all_station_boards_have_bounded_unique_offers():
    world = _world_with_seed(42)
    ids = []
    for station in world.galaxy:
        world.save.current_system = station.id
        board = vr.generate_mission_board(world)
        assert len(board) <= 4
        ids.extend(m.id for m in board)
    assert len(world.save.mission_boards) == vr.GALAXY_SYSTEM_COUNT
    assert len(ids) == len(set(ids))
    assert world.save.next_mission_id > max(ids)


@pytest.mark.parametrize("kind", ["delivery", "scan"])
@pytest.mark.parametrize("turn", [4, 5, 6])
def test_mission_deadlines_are_inclusive_and_checked_before_rewards(kind, turn):
    world = _world_with_seed(42)
    world.save.turn = turn
    world.save.current_system = 1
    _set_cargo(world, {"food": 3})
    world.save.active_missions = [
        vr.Mission(1, kind, "Deadline", 500, 0, 1, commodity="food", quantity=3, deadline_turn=5),
    ]
    before = world.save.pilot.credits
    messages = vr.check_mission_completions(world, just_discovered=1)
    assert world.save.pilot.credits == before + (500 if turn <= 5 else 0)
    assert not world.save.active_missions
    if turn > 5:
        assert "expired" in messages[0]
        assert world.save.cargo == {"food": 3}


def test_duplicate_contract_ids_are_refused_rather_than_renumbered():
    """Shared ids were a retired schema's shape, repaired at every turn boundary;
    schema 2 states the invariant and drops the repair (issue #421)."""
    world = _world_with_seed(42)
    world.save.active_missions = [
        vr.Mission(1, "escort", "A", 500, 0, 1, pirate_tier=1),
        vr.Mission(1, "escort", "B", 500, 0, 1, pirate_tier=1),
    ]
    world.save.pending_travel = {
        "version": 1, "origin": 0, "destination": 1, "was_discovered": True,
        "destroyed": False, "phase": "primary", "primary": "random", "bounty": None,
        "escorts": [m.to_dict() for m in world.save.active_missions], "escort_index": 0, "encounter": {},
    }
    world.save.event_rng_state = world.event_rng.getstate()
    with pytest.raises(vr.ResumeError): vr.SaveData.from_dict(world.save.to_dict())


def test_an_over_limit_career_keeps_every_contract():
    world = _world_with_seed(42)
    world.save.active_missions = [vr.Mission(i + 1, "scan", f"Old job {i}", 500, 0, 1) for i in range(5)]
    world.save.next_mission_id = 6
    restored = vr.World(vr.SaveData.from_dict(world.save.to_dict()))
    assert len(restored.save.active_missions) == 5
    assert len({m.id for m in restored.save.active_missions}) == 5
    offer = vr.generate_mission_board(restored)[0]
    with pytest.raises(vr.MissionError, match="at most"):
        vr.accept_mission(restored, offer)


def test_real_board_reopen_and_kill_retains_posted_terms(tmp_path):
    world = _world_with_seed(42)
    vr.persist(world, tmp_path, 77)
    with _door_stopped_at(tmp_path, b"B", b"Details"):
        first, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    with _door_stopped_at(tmp_path, b"B", b"Details"):
        reopened, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert first.mission_boards and reopened.mission_boards == first.mission_boards
    assert reopened.next_mission_id == first.next_mission_id
    assert reopened.event_rng_state == first.event_rng_state


def test_board_browsing_never_checkpoints_or_changes_state(monkeypatch):
    import copy
    world = _world_with_seed(42)
    world.checkpoint()
    before = copy.deepcopy(world.save.to_dict())
    rng = world.event_rng.getstate()
    for boundary in ("checkpoint", "commit", "advance_station_state"):
        # Every write path, not just the historical name (#417 review).
        monkeypatch.setattr(world, boundary, lambda: pytest.fail("Browsing wrote the save"))
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_missions(vr.Palette(False), world)
    assert world.save.to_dict() == before
    assert world.event_rng.getstate() == rng


def test_station_checkpoint_prepares_board_and_expires_jobs():
    world = _world_with_seed(42)
    world.save.turn = 5
    world.save.active_missions = [vr.Mission(1, "scan", "Expired", 500, 0, 1, deadline_turn=4)]
    writes = []
    world._checkpoint = lambda current: writes.append(current.save.to_dict())
    world.checkpoint()
    assert len(writes) == 1
    assert writes[0]["mission_boards"]["0"]["refresh_turn"] == 8
    assert not writes[0]["active_missions"]
    assert vr.posted_mission_offers(world)


def test_unprepared_board_browsing_does_not_generate_or_expire(monkeypatch):
    import copy
    world = _world_with_seed(42)
    world.save.turn = 5
    world.save.active_missions = [vr.Mission(1, "scan", "Expired", 500, 0, 1, deadline_turn=4)]
    before = copy.deepcopy(world.save.to_dict())
    for boundary in ("checkpoint", "commit", "advance_station_state"):
        # Every write path, not just the historical name (#417 review).
        monkeypatch.setattr(world, boundary, lambda: pytest.fail("Browsing wrote the save"))
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_missions(vr.Palette(False), world)
    assert world.save.to_dict() == before


@pytest.mark.parametrize("seed", [0, 42, 310])
def test_board_preparation_does_not_advance_encounter_rng(seed):
    world = _world_with_seed(seed)
    before = world.event_rng.getstate()
    world.checkpoint()
    assert world.event_rng.getstate() == before
    assert vr.posted_mission_offers(world)


def test_mission_preview_back_and_paging_are_read_only(monkeypatch):
    import copy
    world, mission = _mission_details_world()
    before = copy.deepcopy(world.save.to_dict())
    rng = world.event_rng.getstate()
    keys = iter("1NPBB")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    for boundary in ("checkpoint", "commit", "advance_station_state"):
        # Every write path, not just the historical name (#417 review).
        monkeypatch.setattr(world, boundary, lambda: pytest.fail("Preview saved"))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        vr.screen_missions(vr.Palette(False), world)
    assert mission.description in output.getvalue()
    assert world.save.to_dict() == before
    assert world.event_rng.getstate() == rng
    assert not world.by_id[mission.target_system].discovered


def test_acceptance_requires_last_details_page_and_checkpoints_before_ack(monkeypatch):
    world, mission = _mission_details_world()
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 12)
    pages = vr._mission_text_pages(vr.mission_details(world, mission))
    assert len(pages) > 1
    # Premature A is ignored. Only the final A commits, before acknowledgement.
    keys = iter("A" + "N" * (len(pages) - 1) + "AK")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    output = io.StringIO()
    commits = []
    def commit(current):
        assert "Accepted:" not in output.getvalue()
        commits.append(current.save.to_dict())
    world._checkpoint = commit
    with contextlib.redirect_stdout(output):
        vr.screen_mission_details(vr.Palette(False), world, mission, active=False)
    assert len(commits) == 1
    assert [m.id for m in world.save.active_missions] == [mission.id]
    assert not world.save.mission_boards[0]["offers"]


@pytest.mark.parametrize("width,height", [(40, 24), (80, 24), (40, 12), (20, 10)])
def test_full_contract_details_fit_each_page_and_retain_back(monkeypatch, terminal, width, height):
    world, mission = _mission_details_world("escort")
    mission.description = "Very long objective " * 20
    terminal(width, height)
    output = io.StringIO()
    frames = []
    offset = 0
    count = len(vr._mission_text_pages(vr.mission_details(world, mission)))
    def key():
        nonlocal offset
        frame = output.getvalue()[offset:]
        offset = len(output.getvalue())
        frames.append(frame)
        rows = frame.split("\r\n")
        assert len(rows) <= height
        assert all(vr._visible_width(row) <= width for row in rows)
        assert "[B] Back" in frame
        return "N" if len(frames) < count else "B"
    monkeypatch.setattr(vr, "read_key", key)
    with contextlib.redirect_stdout(output):
        vr.screen_mission_details(vr.Palette(False), world, mission, active=False)
    body_rows = [row for row in vr._ANSI_RE.sub("", output.getvalue()).split("\r\n")
                 if not row.startswith(("Contract #", "[R]", "[N]", "[P]", "[B]", "[A]"))]
    joined = " ".join(" ".join(body_rows).split())
    assert "EVERY jump" in joined and "including detours" in joined
    assert "no cargo space" in joined
    assert len(frames) == count


def test_a_long_active_contract_list_is_paginated_and_selectable(monkeypatch, terminal):
    world = _world_with_seed(42)
    world.save.active_missions = [vr.Mission(i + 1, "scan", f"Survey {i}", 500, 0, 1) for i in range(30)]
    terminal(40, 24)
    output = io.StringIO()
    selected = []
    def key():
        return "N" if "Contracts 2/" not in output.getvalue() else "1" if not selected else "B"
    monkeypatch.setattr(vr, "read_key", key)
    monkeypatch.setattr(vr, "screen_mission_details", lambda p, w, m, active: selected.append((m.id, active)))
    with contextlib.redirect_stdout(output):
        vr.screen_missions(vr.Palette(False), world)
    assert selected and selected[0][0] > 1 and selected[0][1]
    assert "SURVEY" in output.getvalue()
    assert "CARGO" not in output.getvalue()
    assert len(world.save.active_missions) == 30


@pytest.mark.parametrize("kind", ["delivery", "scan", "bounty", "escort"])
def test_contract_terms_show_kind_obligations_and_cost_limits(kind):
    world, mission = _mission_details_world(kind)
    world.save.ship.has_navigator = True
    _set_cargo(world, {"food": 1})
    lines = " ".join(vr.mission_details(world, mission))
    assert "inclusive" in lines and "Target danger: uncharted" in lines
    assert "Gross payout" in lines and "not total profit" in lines
    assert "repairs" in lines and "wages" in lines
    if kind == "delivery":
        price = vr.price_for(world, 0, "food")
        assert f"2 x {price} = {2 * price:,} cr" in lines
        assert "Delivery consumes the cargo" in lines
    elif kind == "scan":
        assert "scanner" in lines and "does not chart" in lines
    elif kind == "bounty":
        assert "tier 2" in lines and "mistaken-identity" in lines
    else:
        assert "EVERY jump" in lines and "Other escort contracts" in lines


@pytest.mark.parametrize("action", ["complete", "expire", "abandon"])
def test_tracking_roundtrips_and_clears_with_contract_removal(tmp_path, action):
    world, mission = _mission_details_world()
    vr.accept_mission(world, mission)
    vr.track_mission(world, mission.id)
    world.checkpoint()
    vr.persist(world, tmp_path, 77)
    save, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    world = vr.World(save)
    assert vr.tracked_mission(world).id == mission.id
    if action == "complete":
        world.save.current_system = mission.target_system
        _set_cargo(world, {"food": 3})
        vr.check_mission_completions(world)
    elif action == "expire":
        world.save.turn = 11
        vr.expire_missions(world)
    else:
        vr.abandon_mission(world, mission.id)
    assert vr.tracked_mission(world) is None
    assert world.save.tracked_mission_id is None


def test_abandonment_cancel_is_read_only_and_confirmed_action_keeps_cargo(monkeypatch):
    import copy
    world, mission = _mission_details_world()
    vr.accept_mission(world, mission)
    vr.track_mission(world, mission.id)
    _set_cargo(world, {"food": 3})
    before = copy.deepcopy(world.save.to_dict())
    keys = iter("DNB")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_mission_details(vr.Palette(False), world, mission, active=True)
    assert world.save.to_dict() == before
    keys = iter("DYK")
    commits = []
    world._checkpoint = lambda current: commits.append(current.save.to_dict())
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_mission_details(vr.Palette(False), world, mission, active=True)
    assert len(commits) == 1
    assert world.save.cargo == {"food": 3}
    assert world.save.pilot.credits == before["pilot"]["credits"]
    assert not world.save.active_missions and world.save.tracked_mission_id is None
    assert not world.save.mission_boards[0]["offers"]


def test_real_tracking_action_survives_termination_and_is_visible_on_return(tmp_path):
    world, mission = _mission_details_world("scan")
    vr.accept_mission(world, mission)
    world.checkpoint()
    vr.persist(world, tmp_path, 77)
    choice = len(vr.posted_mission_offers(world)) + 1
    with _door_stopped_at(tmp_path, f"B{choice}T".encode(), b"Tracking updated."):
        save, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert save.tracked_mission_id == mission.id
    with _door_stopped_at(tmp_path, b"", b"Tracked") as output:
        assert b"Tracked" in output


def test_tracking_and_abandonment_reject_pending_travel():
    world, mission = _mission_details_world()
    vr.accept_mission(world, mission)
    world.save.pending_travel = {"phase": "primary"}
    with pytest.raises(vr.MissionError, match="interrupted journey"):
        vr.track_mission(world, mission.id)
    with pytest.raises(vr.MissionError, match="interrupted journey"):
        vr.abandon_mission(world, mission.id)
    assert world.save.active_missions == [mission]


@pytest.mark.parametrize("keys,ack", [("T", "Tracking updated"), ("DY", "Abandoned:")])
def test_contract_management_save_failure_precedes_acknowledgement(monkeypatch, keys, ack):
    world, mission = _mission_details_world()
    vr.accept_mission(world, mission)
    commands = iter(keys)
    monkeypatch.setattr(vr, "read_key", lambda: next(commands))
    def fail(current):
        raise OSError("disk full")
    world._checkpoint = fail
    output = io.StringIO()
    with contextlib.redirect_stdout(output), pytest.raises(vr.SaveError):
        vr.screen_mission_details(vr.Palette(False), world, mission, active=True)
    assert ack not in output.getvalue()


@pytest.mark.parametrize("discovered", [False, True])
def test_tracked_chart_hint_identifies_the_actual_destination_key(monkeypatch, discovered):
    world, mission = _mission_details_world("scan")
    vr.accept_mission(world, mission)
    vr.track_mission(world, mission.id)
    first = vr.mission_route(world, mission)[0]
    world.by_id[first].discovered = discovered
    world.save.ship.fuel = 100
    key = vr.CHART_CONNECTION_LETTERS[sorted(world.here.connections).index(first)]
    keys = iter([key, "Y"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        dest = vr.screen_chart(vr.Palette(False), world)
    assert dest == first
    assert f"[{key}]" in output.getvalue()
    assert "TRACKED NEXT" in " ".join(__import__("re").sub(r"\[[A-Z]\] ", "", vr._ANSI_RE.sub("", output.getvalue())).split())
    assert world.by_id[first].discovered is discovered


def test_bounty_retry_requires_reentry_and_budgets_two_jumps():
    world, mission = _mission_details_world("bounty")
    vr.accept_mission(world, mission)
    world.save.current_system = mission.target_system
    world.save.turn = mission.deadline_turn - 1
    world.save.ship.has_navigator = True
    world.save.ship.fuel = 0
    route = vr.mission_route(world, mission)
    assert len(route) == 2 and route[-1] == mission.target_system
    lines = " ".join(vr.mission_details(world, mission))
    assert "jump back" in lines and "both jumps" in lines
    assert "shortest route misses the deadline" in lines
    assert "2 jump(s)" in vr.mission_bearing(world, mission)
    assert "wages 10 cr/jump, 20 cr total" in lines


@pytest.mark.parametrize("kind", ["bounty", "escort"])
@pytest.mark.parametrize("outcome", ["won", "destroyed", "expired"])
def test_pending_mission_resolution_checkpoint_clears_tracking(monkeypatch, kind, outcome):
    world, mission = _mission_details_world(kind)
    vr.accept_mission(world, mission)
    vr.track_mission(world, mission.id)
    world.save.pending_travel = {
        "phase": "primary" if kind == "bounty" else "escorts", "bounty": mission.to_dict(),
        "escorts": [mission.to_dict()] if kind == "escort" else [], "escort_index": 0,
        "encounter": {},
    }
    if outcome == "expired":
        world.save.turn = 11
    monkeypatch.setattr(vr, "screen_combat", lambda *args: outcome)
    seen = []
    def checkpoint(current):
        if not current.save.active_missions:
            assert current.save.pending_travel is not None
            assert current.save.tracked_mission_id is None
            seen.append(1)
    world._checkpoint = checkpoint
    with contextlib.redirect_stdout(io.StringIO()):
        if kind == "bounty":
            vr._resolve_bounty(vr.Palette(False), world, world.save.pending_travel)
        else:
            vr._resolve_escort_missions(vr.Palette(False), world, mission.target_system)
    assert seen


def test_queued_bounty_route_budgets_preceding_fights():
    world, mission = _mission_details_world("bounty")
    earlier = vr.Mission(9, "bounty", "Earlier", 500, 0, mission.target_system, pirate_tier=1)
    world.save.active_missions = [earlier]
    base = vr.bfs_path(world.by_id, 0, mission.target_system)
    assert len(vr.mission_route(world, mission)) == len(base) + 2
    vr.accept_mission(world, mission)
    assert len(vr.mission_route(world, mission)) == len(base) + 2
    assert "1 earlier contract" in " ".join(vr.mission_details(world, mission))
    world.save.current_system = mission.target_system
    assert len(vr.mission_route(world, mission)) == 4
    world.save.active_missions.remove(earlier)
    assert len(vr.mission_route(world, mission)) == 2


def test_tiny_contract_board_splits_entries_and_keeps_selection(monkeypatch, terminal):
    import re
    world, mission = _mission_details_world("escort")
    world.by_id[mission.target_system].name = "A particularly long system name that cannot fit one page"
    terminal(20, 10)
    output = io.StringIO()
    offset = 0
    frames = []
    selected = []
    def key():
        nonlocal offset
        frame = output.getvalue()[offset:]
        offset = len(output.getvalue())
        frames.append(frame)
        assert len(frame.split("\r\n")) <= 10
        assert all(vr._visible_width(row) <= 20 for row in frame.split("\r\n"))
        plain = vr._ANSI_RE.sub("", frame)
        page, total = map(int, re.search(r"Contracts (\d+)/(\d+)", plain).groups())
        return "N" if page < total else "1" if not selected else "B"
    monkeypatch.setattr(vr, "read_key", key)
    monkeypatch.setattr(vr, "screen_mission_details", lambda p, w, m, active: selected.append(m.id))
    with contextlib.redirect_stdout(output):
        vr.screen_missions(vr.Palette(False), world)
    assert len(frames) > 2 and selected == [mission.id]


def test_optional_fields_default_and_an_over_limit_career_keeps_every_contract():
    import json

    world = _world_with_seed(42)
    world.save.active_missions = [vr.Mission(i, "bounty", f"Old contract {i}", 50, 0, 1, pirate_tier=1)
                                  for i in range(1, 6)]
    world.save.next_mission_id = 6
    data = world.save.to_dict()
    for key in ("galaxy_version", "mission_boards", "best_credits", "active_futures", "pending_travel",
                "event_rng_state"):
        data.pop(key)
    loaded = vr._decode_career(json.dumps(data).encode())
    assert loaded.galaxy_version == 1 and len(loaded.active_missions) == 5
    assert vr.generate_galaxy(loaded.seed) == vr.generate_galaxy(world.save.seed)
    resumed = vr.World(loaded)
    assert len({m.id for m in resumed.save.active_missions}) == 5


def test_opening_assignment_quotes_real_affordable_neighborhoods_without_rng_or_save_changes():
    import copy
    for seed in range(64):
        world = _world_with_seed(seed)
        world.checkpoint()
        before = copy.deepcopy(world.save.to_dict())
        rng_before = world.event_rng.getstate()
        offer = vr.opening_assignment_offer(world)
        assert offer is not None, seed
        assert offer.target_system in world.here.connections and offer.quantity == 3
        assert offer.commodity in vr.ECONOMY_DEMANDS[world.by_id[offer.target_system].economy]
        assert vr.COMMODITIES[offer.commodity]["legal"] and offer.deadline_turn is None
        fuel = vr.fuel_cost_for_jump(world.here, world.by_id[offer.target_system], world.save.ship)
        cost = 3 * vr.price_for(world, 0, offer.commodity)
        assert 2 * fuel <= world.save.ship.fuel and cost <= world.save.pilot.credits
        assert offer.reward - cost - 12 * fuel == 200
        assert world.save.to_dict() == before and world.event_rng.getstate() == rng_before
        assert vr.opening_assignment_offer(world).to_dict() == offer.to_dict()


@pytest.mark.parametrize("reason", ["later", "away", "taken", "broke", "full", "stale", "capacity", "pending"])
def test_opening_assignment_rejection_is_atomic(reason):
    import copy
    world = _world_with_seed(42)
    world.checkpoint()
    offer = vr.opening_assignment_offer(world)
    if reason == "later":
        world.save.turn = 1
    elif reason == "away":
        world.save.current_system = offer.target_system
    elif reason == "taken":
        world.save.flags["opening_assignment_taken"] = True
    elif reason == "broke":
        world.save.pilot.credits = 0
    elif reason == "full":
        other = next(c for c in vr.LEGAL_COMMODITIES if c != offer.commodity)
        _set_cargo(world, {other: vr.cargo_capacity(world.save.ship)})
    elif reason == "stale":
        offer.reward += 1
    elif reason == "capacity":
        world.save.active_missions = [vr.Mission(100 + i, "delivery", "Existing", 10, 0, offer.target_system,
                                               commodity="food", quantity=1) for i in range(vr.MAX_ACTIVE_MISSIONS)]
    else:
        world.save.pending_travel = {"version": 1}
    before = copy.deepcopy(world.save.to_dict())
    with pytest.raises(vr.MissionError):
        vr.accept_opening_assignment(world, offer)
    assert world.save.to_dict() == before


def test_opening_assignment_is_tracked_durable_and_pays_only_once_without_deadline(tmp_path):
    world = _world_with_seed(42)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    offer = vr.opening_assignment_offer(world)
    vr.accept_opening_assignment(world, offer)
    world.checkpoint()
    loaded, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    world = vr.World(loaded, checkpoint=lambda current: vr.persist(current, tmp_path, 77))
    assert vr.tracked_mission(world).opening_assignment
    assert world.save.flags["opening_assignment_taken"] and vr.opening_assignment_offer(world) is None
    before = world.save.pilot.credits
    world.save.current_system = offer.target_system
    world.save.turn = 100
    _add_cargo(world, offer.commodity, 4)
    assert len(vr.check_mission_completions(world)) == 1
    assert world.save.cargo[offer.commodity] == 1
    assert world.save.pilot.credits == before + offer.reward
    assert world.save.flags["opening_assignment_completed"]
    assert vr.check_mission_completions(world) == []
    world.checkpoint()
    loaded, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert loaded.pilot.missions_completed == 1 and loaded.pilot.credits == before + offer.reward
    assert "First Flight complete" in " ".join(vr.pilot_guide_lines(vr.World(loaded)))


def test_abandoned_opening_assignment_cannot_be_repeated_until_a_new_career():
    world = _world_with_seed(42)
    world.checkpoint()
    offer = vr.opening_assignment_offer(world)
    vr.accept_opening_assignment(world, offer)
    vr.abandon_mission(world, offer.id)
    assert vr.opening_assignment_offer(world) is None
    assert "First Flight is closed" in " ".join(vr.pilot_guide_lines(world))
    world.reset(vr.retire_pilot(world.save))
    assert not world.save.flags.get("opening_assignment_taken")
    assert vr.opening_assignment_offer(world) is not None


@pytest.mark.parametrize("invalid_flag", [None, 0, 1, "yes"])
def test_invalid_opening_assignment_flag_is_rejected_before_checkpoint_write(tmp_path, invalid_flag):
    world = _world_with_seed(42)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    before = (tmp_path / "77.json").read_bytes()
    offer = vr.opening_assignment_offer(world)
    offer.opening_assignment = invalid_flag
    world.save.active_missions.append(offer)
    with pytest.raises(vr.SaveError):
        vr.write_save(tmp_path, 77, world.save)
    assert (tmp_path / "77.json").read_bytes() == before


def test_an_ordinary_contract_keeps_its_serialized_shape():
    mission = vr.Mission(1, "delivery", "Old job", 100, 0, 1, commodity="food", quantity=3)
    assert "opening_assignment" not in mission.to_dict()
    loaded = vr.Mission.from_dict(mission.to_dict())
    assert loaded.opening_assignment is False and loaded.to_dict() == mission.to_dict()


@pytest.mark.parametrize("corruption", ["missing_acceptance", "duplicate", "completed_but_active"])
def test_contradictory_opening_assignment_progress_cannot_be_saved(tmp_path, corruption):
    import dataclasses
    world = _world_with_seed(42)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    offer = vr.opening_assignment_offer(world)
    vr.accept_opening_assignment(world, offer)
    world.checkpoint()
    before = (tmp_path / "77.json").read_bytes()
    if corruption == "missing_acceptance":
        del world.save.flags["opening_assignment_taken"]
    elif corruption == "duplicate":
        world.save.active_missions.append(dataclasses.replace(offer, id=999))
    else:
        world.save.flags["opening_assignment_completed"] = True
    with pytest.raises(vr.SaveError):
        vr.write_save(tmp_path, 77, world.save)
    assert (tmp_path / "77.json").read_bytes() == before


def test_ordinary_mission_acceptance_rejects_an_opening_tag_without_mutating_the_board():
    import copy
    world = _world_with_seed(42)
    world.checkpoint()
    offer = vr.opening_assignment_offer(world)
    world.save.mission_boards[0]["offers"][0] = offer.to_dict()
    before = copy.deepcopy(world.save.to_dict())
    with pytest.raises(vr.MissionError, match="Pilot Guide"):
        vr.accept_mission(world, offer)
    assert world.save.to_dict() == before


@pytest.mark.parametrize("deadline", [-1, 0, 1])
def test_area_survey_completes_all_matching_contracts_with_inclusive_deadlines(deadline):
    world = _world_with_seed(42); world.save.ship.scanner_tier = 1
    targets = vr.survey_candidates(world)[:2]
    world.save.active_missions = [vr.Mission(i, "scan", f"Survey {i}", 100, 0, sid, deadline_turn=deadline)
                                  for i, sid in enumerate(targets, 1)]
    _, report = vr.perform_survey(world)
    assert world.save.active_missions == []
    assert world.save.pilot.credits == (1200 if deadline < 0 else 1400)
    assert world.save.pilot.missions_completed == (0 if deadline < 0 else 2)
    assert sum("Mission complete" in row for row in report) == (0 if deadline < 0 else 2)


def test_underfunded_destruction_never_creates_debt_and_buys_only_the_hull_it_pays_for():
    world = _world_with_seed(42); ship = world.save.ship; maximum = vr.hull_hp_max(ship)
    world.save.pilot.credits = 100; ship.hull_hp = 10
    message = vr.destroy_ship(world)
    assert world.save.pilot.credits == 0 and ship.hull_hp == maximum // 4 + (maximum - maximum // 4) * 100 // vr.salvage_fee(ship) < maximum
    assert "the tug patched what 100cr covers" in message
    for paid in range(0, vr.salvage_fee(ship)):
        assert maximum // 4 <= vr.salvage_hull(ship, paid) < maximum  # never full until the whole fee is paid
    ship.hull_hp = 1
    vr.destroy_ship(world)
    assert world.save.pilot.credits == 0 and ship.hull_hp == maximum // 4 and world.save.current_system == 0
    world.save.pilot.credits = vr.salvage_fee(ship); vr.destroy_ship(world)
    assert ship.hull_hp == maximum and world.save.pilot.credits == 0


def test_destruction_is_never_cheaper_per_hull_point_than_repair():
    world = _world_with_seed(42); ship = world.save.ship; maximum = vr.hull_hp_max(ship)
    for credits in (0, 40, 100, 200, 1000):
        ship.hull_hp = 10; world.save.pilot.credits = credits
        repaired = 10 + min(maximum - 10, credits // 4)
        vr.destroy_ship(world)
        assert ship.hull_hp <= repaired + maximum // 4 or credits >= vr.salvage_fee(ship)  # the only free hull is the flyable quarter floor


def test_the_tow_never_hands_back_more_hull_than_the_fight_started_with():
    """A broke pilot at 10/400 cannot buy the quarter-hull floor by dying."""
    world = _world_with_seed(42); ship = world.save.ship
    ship.hull_class, ship.hull_tier = "Carrier", vr.UPGRADES["hull"]["max_tier"]
    maximum = vr.hull_hp_max(ship)
    assert maximum // 4 > 10  # the floor would otherwise be a free upgrade
    world.save.pilot.credits = 0
    vr.destroy_ship(world, hull_before=10)
    assert ship.hull_hp == 10
    for credits in (0, 20, 200, 1000):
        for before in (1, 10, maximum // 4, maximum):
            ship.hull_hp = before; world.save.pilot.credits = credits
            repaired = before + min(maximum - before, credits // 4)  # the yard charges four credits a hull point
            vr.destroy_ship(world, hull_before=before)
            assert ship.hull_hp <= repaired  # dying is never a cheaper repair than the yard


def test_a_patrol_kill_clears_notoriety_only_once_the_fine_is_paid_too():
    """Surrender stays the cheap way out of a wanted status (#402 review)."""
    world = _world_with_seed(42); ship = world.save.ship
    fee = vr.salvage_fee(ship)
    world.save.pilot.notoriety = 20
    fine = vr.notoriety_fine_cost(20)
    assert fine > fee  # the exploit only exists because the fee is bounded
    world.save.pilot.credits = fee + fine - 1
    message = vr.destroy_ship(world, patrol=True)
    assert world.save.pilot.notoriety == 20 and world.save.pilot.credits == 0
    assert f"The {fine}cr fine went unpaid" in message
    assert "unpaid" in world.save.pilot.log[-1]
    ship.hull_hp = 1; world.save.pilot.credits = fee + fine
    message = vr.destroy_ship(world, patrol=True)
    assert world.save.pilot.notoriety == 0 and world.save.pilot.credits == 0
    assert "Notoriety cleared" in message and ship.hull_hp == vr.hull_hp_max(ship)


def test_combat_screen_discloses_the_salvage_fee_when_hull_is_low():
    world = _world_with_seed(42); pirate = vr.Pirate("Opponent", 1, 50, 50)
    fee = vr.salvage_fee(world.save.ship)
    world.save.ship.hull_hp = vr.hull_hp_max(world.save.ship)
    calm = " ".join(vr.combat_display_lines(world, pirate, [], patrol=False, tactics=vr.new_tactics(pirate)))
    assert f"{fee}cr salvage fee" not in calm
    assert "notoriety stays" in " ".join(vr.combat_display_lines(world, pirate, [], patrol=False, details=True, tactics=vr.new_tactics(pirate)))
    world.save.pilot.notoriety = 6
    patrol_details = " ".join(vr.combat_display_lines(world, pirate, [], patrol=True, details=True, tactics=vr.new_tactics(pirate)))
    assert f"collects your {vr.notoriety_fine_cost(6)}cr fine and clears notoriety only once both are paid" in patrol_details
    world.save.pilot.notoriety = 0
    world.save.ship.hull_hp = 10
    low = " ".join(vr.combat_display_lines(world, pirate, [], patrol=False, tactics=vr.new_tactics(pirate)))
    assert f"LOW HULL: one third of maximum hull or less. Destruction: {fee}cr salvage fee" in low
    world.save.pilot.credits = 30
    low = " ".join(vr.combat_display_lines(world, pirate, [], patrol=False, tactics=vr.new_tactics(pirate)))
    assert "Destruction: 30cr salvage fee" in low and f"hull patched to {vr.salvage_hull(world.save.ship, 30)}/{vr.hull_hp_max(world.save.ship)}" in low
    assert f"destruction costs {fee}cr salvage" in " ".join(vr.station_deck_lines(world))


@pytest.mark.parametrize("outcome", ["escaped", "destroyed"])
def test_lost_escort_counts_once_and_logs_the_forfeited_reward(monkeypatch, outcome):
    world, mission = _escort_world(outcome)
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: outcome)
    with contextlib.redirect_stdout(io.StringIO()):
        vr._resolve_escort_missions(vr.Palette(False), world, mission.target_system)
    assert world.save.pilot.missions_failed == 1 and world.save.pilot.missions_expired == 0
    assert world.save.pilot.missions_completed == 0 and not world.save.active_missions
    assert any("Escort failed: Escort a convoy (lost 800cr)" in entry for entry in world.save.pilot.log)
    # Resuming the same hop after the checkpoint must not count it twice.
    with contextlib.redirect_stdout(io.StringIO()):
        vr._resolve_escort_missions(vr.Palette(False), world, mission.target_system)
    assert world.save.pilot.missions_failed == 1


def test_lost_bounty_counts_as_failed_and_closed_warrant_does_not(monkeypatch):
    for outcome, failed in (("destroyed", 1), ("reported", 0)):
        world, pirate = _world_with_pending_fight()
        combat = world.save.pending_travel["encounter"]["combat"]
        combat["outcome"], combat["lines"] = outcome, ["cached"]
        world.save.pending_travel["encounter"]["warrant"] = {"version": 1, "matches": False, "checked": True, "engaged": False}
        with contextlib.redirect_stdout(io.StringIO()):
            vr._resolve_bounty(vr.Palette(False), world, world.save.pending_travel)
        assert world.save.pilot.missions_failed == failed and not world.save.active_missions


def test_expiry_paths_count_as_expired_with_the_unpaid_reward():
    world = _world_with_seed(42)
    dest = sorted(world.here.connections)[0]
    world.save.active_missions = [vr.Mission(1, "delivery", "Deliver goods", 300, 0, dest, commodity="food", quantity=2, deadline_turn=1),
                                  vr.Mission(2, "scan", "Survey", 250, 0, dest, deadline_turn=1)]
    world.save.turn = 2
    messages = vr.expire_missions(world)
    assert len(messages) == 2 and world.save.pilot.missions_expired == 2 and world.save.pilot.missions_failed == 0
    assert "Mission expired: Deliver goods (unpaid 300cr)" in messages
    world.save.pilot.missions_expired = 0
    world, mission = _escort_world("escaped")
    mission.deadline_turn = 0; world.save.pending_travel["escorts"] = [mission.to_dict()]; world.save.active_missions = [mission]
    with contextlib.redirect_stdout(io.StringIO()):
        vr._resolve_escort_missions(vr.Palette(False), world, mission.target_system)
    assert world.save.pilot.missions_expired == 1 and world.save.pilot.missions_failed == 0


def test_abandonment_counts_as_failed_and_names_the_forfeit():
    world = _world_with_seed(42)
    dest = sorted(world.here.connections)[0]
    world.save.active_missions = [vr.Mission(3, "bounty", "Intercept raider", 500, 0, dest, pirate_tier=1)]
    message = vr.abandon_mission(world, 3)
    assert "forfeited 500cr" in message and "Cargo retained; no reward or fee." in message
    assert world.save.pilot.missions_failed == 1 and not world.save.active_missions


def test_completed_legal_contracts_earn_concord_standing():
    world = _world_with_seed(42)
    dest = sorted(world.here.connections)[0]
    world.save.active_missions = [vr.Mission(1, "delivery", "Deliver", 300, 0, dest, commodity="food", quantity=2)]
    _set_cargo(world, {"food": 2}); world.save.current_system = dest
    assert vr.check_mission_completions(world) and world.save.pilot.reputation["concord"] == vr.CONCORD_STANDING_PER_CONTRACT
    world.save.active_missions = [vr.Mission(2, "scan", "Survey", 250, 0, 5)]
    vr.check_mission_completions(world, just_discovered=5)
    assert world.save.pilot.reputation["concord"] == 2 * vr.CONCORD_STANDING_PER_CONTRACT


def test_escape_cancels_a_quantity_field_and_erases_the_typed_digits(monkeypatch):
    keys = iter(["1", "2", vr.ESCAPE_KEY])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        assert vr.read_line_raw(max_len=5) == ""
    assert output.getvalue().count("\x08 \x08") == 2
    world = _world_with_seed(42); before = world.save.to_dict()
    keys = iter(["P", "1", vr.ESCAPE_KEY]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        assert vr._trade_commodity(vr.Palette(False), world, "food") is None
    assert "Enter or Esc cancels" in output.getvalue() and world.save.to_dict() == before


def test_real_escape_at_the_departure_confirmation_keeps_the_pilot_docked(tmp_path):
    world = _world_with_seed(42); world.save.ship.fuel = vr.fuel_capacity(world.save.ship)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77); world.checkpoint()
    before = (tmp_path / "77.json").read_bytes()
    with _door_stopped_at(tmp_path, b"C" + vr.CHART_CONNECTION_LETTERS[0].encode() + b"\x1b", b"Departure cancelled; still docked."):
        assert (tmp_path / "77.json").read_bytes() == before


def test_every_printed_hotkey_uses_the_one_style():
    """`[K] Label` everywhere -- never `[B]ack`, never `[B]Back` (issue #400).

    The game used to spell hotkeys three ways, sometimes two of them in one action
    bar. Read the source rather than a sample of screens, because the styles that
    were missed last time were on the screens nobody thought to render.
    """
    import re
    source = _VOIDRUNNER_PATH.read_text(encoding="utf-8")
    # A printed hotkey's opening bracket never follows an identifier or another
    # closing bracket; that is what tells `[F]` in a message from `rows[0]` in code.
    glued = re.findall(r"(?<![\w\]])(\[[A-Z0-9<>][A-Z0-9<>/,\-]{0,11}\][A-Za-z]\w*)", source)
    assert not glued
    # The bars composed at runtime spell it the same way.
    for bar in (vr.combat_action_bar("F/G/E/D/P"),
                vr._detail_action_bar("A/R", {"A": "Accept", "R": "Route"})):
        assert not re.search(r"\]\S", bar)


def test_combat_action_bar_labels_every_verb_and_matches_the_body(monkeypatch):
    import re
    assert vr.combat_action_bar("F/G/E/D/P") == "[F] Fire [G] Guard [E] Evade [D] Dump [P] Pay bribe [I] Info [<>] Page: "
    world, pirate = _world_with_pending_fight(tactics={"version": 2, "profile": "Raider", "step": 0, "brace_ready": True})
    world.save.pilot.credits = 10_000; _set_cargo(world, {"food": 1})
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 60)
    frames = []; output = io.StringIO()
    def choose():
        frames.append(output.getvalue()); output.seek(0); output.truncate(0)
        raise EOFError
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output), pytest.raises(EOFError):
        vr._screen_combat_session(vr.Palette(False), world, pirate, patrol=False)
    frame = vr._ANSI_RE.sub("", frames[0])
    bar = frame.rstrip().splitlines()[-1]
    keys = {part[1] for part in bar.split() if part.startswith("[") and len(part) > 2 and part[1] != "<"} - {"I"}
    body_keys = {line[1] for line in frame.splitlines() if len(line) > 3 and line[0] == "[" and line[2] == "]" and line[3] == " "}
    assert keys == body_keys == {"F", "G", "E", "D", "P"}
    assert not re.search(r"\]\S", bar)                     # one hotkey style: `[K] Label` (#400)


def test_mission_expired_reads_the_deadline_and_nothing_else():
    world = _world_with_seed(42)
    target = sorted(world.here.connections)[0]
    open_ended = vr.Mission(1, "delivery", "Deliver", 100, 0, target, commodity="food", quantity=1)
    assert vr.mission_expired(world, open_ended) is False  # no deadline never expires
    dated = vr.Mission(2, "delivery", "Deliver", 100, 0, target, commodity="food", quantity=1, deadline_turn=3)
    world.save.turn = 3
    assert vr.mission_expired(world, dated) is False  # the deadline day is inclusive
    world.save.turn = 4
    assert vr.mission_expired(world, dated) is True
