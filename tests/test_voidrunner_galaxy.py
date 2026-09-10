"""Galaxy generation, sectors, landmarks and the spatial map.

Split out of `test_voidrunner_domain.py` (issue #422).
"""

from __future__ import annotations

import contextlib
import io
import random
import sys

import pytest

from .support import _Sys, _VOIDRUNNER_PATH, _mission_details_world, _world_with_seed, vr


def test_generate_galaxy_is_a_pure_function_of_seed():
    a = vr.generate_galaxy(12345)
    b = vr.generate_galaxy(12345)
    assert [s.name for s in a] == [s.name for s in b]
    assert [s.economy for s in a] == [s.economy for s in b]
    assert [sorted(s.connections) for s in a] == [sorted(s.connections) for s in b]


def test_different_seeds_usually_produce_different_galaxies():
    a = vr.generate_galaxy(1)
    b = vr.generate_galaxy(2)
    assert [s.name for s in a] != [s.name for s in b]


def test_galaxy_is_fully_connected_from_home_system():
    galaxy = vr.generate_galaxy(999)
    by_id = {s.id: s for s in galaxy}
    reachable = vr.bfs_hops(by_id, 0)
    assert len(reachable) == len(galaxy)


def test_home_system_and_its_neighbors_start_discovered_nothing_else_does():
    galaxy = vr.generate_galaxy(42)
    home = galaxy[0]
    assert home.discovered is True
    for sid in home.connections:
        assert galaxy[sid].discovered is True
    far_systems = [s for s in galaxy if s.id != 0 and s.id not in home.connections]
    assert any(not s.discovered for s in far_systems)


def test_home_systems_direct_neighbors_never_exceed_a_safe_danger_ceiling():
    """Dogfood-caught: every system's danger tier is drawn from the same
    distribution regardless of distance from home, so before this fix a
    galaxy could seed a near-unwinnable tier-4 raider system one jump
    from Freeport, before a new character had any chance to earn a
    single upgrade. Checked across a wide range of seeds, not just one,
    since the original bug only showed up for *some* seeds -- a single
    lucky seed passing would have hidden the regression."""
    for seed in range(200):
        galaxy = vr.generate_galaxy(seed)
        home = galaxy[0]
        for nid in home.connections:
            assert galaxy[nid].danger <= 2, f"seed={seed} neighbor={nid} danger={galaxy[nid].danger}"


def test_every_system_has_at_least_one_connection():
    galaxy = vr.generate_galaxy(7)
    assert all(len(s.connections) >= 1 for s in galaxy)


def test_generate_landmark_never_picks_freeport():
    world = _world_with_seed(107)
    landmark = vr.generate_landmark(world.save.seed, world.galaxy)
    assert landmark["system_id"] != 0


def test_generate_landmark_is_deterministic_for_the_same_seed():
    world = _world_with_seed(108)
    first = vr.generate_landmark(world.save.seed, world.galaxy)
    second = vr.generate_landmark(world.save.seed, world.galaxy)
    assert first == second


def test_generate_landmark_does_not_call_the_galaxy_rng():
    """The seed-determinism invariant only allows appending brand-new
    `random.Random` calls at the very end of `generate_galaxy` itself --
    proves landmark generation uses a wholly separate RNG instance and
    never perturbs an existing save's galaxy layout."""
    seed = 109
    before = vr.generate_galaxy(seed)
    vr.generate_landmark(seed, before)
    after = vr.generate_galaxy(seed)
    for a, b in zip(before, after):
        assert a.x == b.x and a.y == b.y and a.connections == b.connections


def test_world_reset_computes_a_landmark():
    world = _world_with_seed(110)
    assert world.landmark["system_id"] in world.by_id
    assert world.landmark["system_id"] != 0


def test_landmark_available_here_true_only_at_the_landmark_system_and_uninvestigated():
    world = _world_with_seed(111)
    world.save.current_system = world.landmark["system_id"]
    assert vr.landmark_available_here(world)

    world.save.flags["landmark_investigated"] = True
    assert not vr.landmark_available_here(world)


def test_landmark_available_here_false_elsewhere():
    world = _world_with_seed(112)
    other = next(sid for sid in world.by_id if sid != world.landmark["system_id"])
    world.save.current_system = other
    assert not vr.landmark_available_here(world)


def test_screen_landmark_grants_reward_once_and_sets_flag(monkeypatch):
    keys = iter("IB")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    world = _world_with_seed(113)
    world.save.current_system = world.landmark["system_id"]
    before_credits = world.save.pilot.credits

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_landmark(vr.Palette(truecolor=False), world)

    assert world.save.flags["landmark_investigated"] is True
    assert world.save.pilot.credits == before_credits + world.landmark["reward_credits"]
    assert not vr.landmark_available_here(world)


def test_station_menu_offers_landmark_only_when_available(monkeypatch):
    world = _world_with_seed(114)
    world.save.current_system = world.landmark["system_id"]

    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)

    assert "[L]" in buf.getvalue()

    world.save.flags["landmark_investigated"] = True
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)

    assert "[L]" not in buf2.getvalue()


def test_retiring_resets_landmark_investigated_flag():
    old_save = vr._new_career("Vet")
    old_save.flags["landmark_investigated"] = True

    new_save = vr.retire_pilot(old_save)

    assert "landmark_investigated" not in new_save.flags


def test_sector_for_covers_the_full_coordinate_grid_without_error():
    world = _world_with_seed(141)
    for system in world.galaxy:
        sector = vr.sector_for(system)
        assert sector in vr.SECTOR_NAMES


def test_sector_for_is_a_pure_function_of_position():
    a = _Sys(10, 5)
    b = _Sys(10, 5)
    assert vr.sector_for(a) == vr.sector_for(b)


def test_sector_for_distinguishes_far_apart_corners():
    top_left = _Sys(0, 0)
    bottom_right = _Sys(99, 49)
    assert vr.sector_for(top_left) != vr.sector_for(bottom_right)


def test_sector_for_never_indexes_out_of_range_at_grid_edges():
    for x in (0, 99):
        for y in (0, 49):
            assert vr.sector_for(_Sys(x, y)) in vr.SECTOR_NAMES


def test_sector_assignment_does_not_touch_the_galaxy_rng():
    """sector_for takes no RNG at all -- proves calling it repeatedly
    never perturbs a subsequent generate_galaxy call for the same seed,
    protecting that function's own seed-determinism invariant."""
    seed = 142
    before = vr.generate_galaxy(seed)
    for system in before:
        vr.sector_for(system)
    after = vr.generate_galaxy(seed)
    for a, b in zip(before, after):
        assert a.x == b.x and a.y == b.y and a.connections == b.connections


@pytest.mark.parametrize("width,height", [(20,10), (40,12), (80,24)])
def test_spatial_map_and_exact_list_fit_terminal_and_preserve_career(monkeypatch, terminal, width, height):
    import copy, re
    world = _world_with_seed(144)
    for station in world.galaxy: station.discovered = True
    before = copy.deepcopy(world.save.to_dict()); rng = world.event_rng.getstate()
    terminal(width, height)
    output = io.StringIO(); frames = []; map_keys = iter("NPO L".replace(" ", ""))
    def choose():
        frame = output.getvalue(); frames.append(frame); output.seek(0); output.truncate(0)
        assert len(frames) < 200
        if "Star Map:" in frame: return next(map_keys)
        match = re.search(r"Charted Systems (\d+)/(\d+)", " ".join(frame.split()))
        assert match
        return "B" if match[1] == match[2] else "N"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output): vr.screen_galaxy_map(vr.Palette(False), world)
    assert all(len(frame.splitlines()) <= height for frame in frames)
    assert all(vr._visible_width(line) <= width for frame in frames for line in frame.splitlines())
    text = " ".join(" ".join(frames).split())
    for station in world.galaxy: assert station.name in text
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


def test_spatial_map_empty_chart_explains_current_bearing_only():
    world = _world_with_seed(143)
    for station in world.galaxy: station.discovered = False
    text = " ".join(vr.map_list_lines(world, [], None))
    assert "Nothing charted yet" in text
    assert "Freeport" not in text and "Uncharted" in text


def test_spatial_map_hides_uncharted_information_except_contract_target():
    world, mission = _mission_details_world("scan"); vr.accept_mission(world, mission); vr.track_mission(world, mission.id)
    path = vr.mission_route(world, mission)
    for sid in path: world.by_id[sid].discovered = False
    text = " ".join(vr.map_list_lines(world, path, mission.target_system))
    assert world.by_id[mission.target_system].name in text and "danger unknown" in text
    for station in world.galaxy:
        if not station.discovered and station.id != mission.target_system: assert station.name not in text
    detail = " ".join(vr.map_inspection_lines(world, mission.target_system, path, mission.target_system))
    assert "connections remain unknown" in detail
    assert world.by_id[mission.target_system].economy not in detail
    assert vr.map_system_ids(world, path, mission.target_system) == {s.id for s in world.galaxy if s.discovered} | set(path) | {0}


def test_spatial_map_markers_and_connections_are_semantic():
    world = _world_with_seed(42)
    for station in world.galaxy: station.discovered = False
    a, b, c = world.galaxy[:3]
    a.x,a.y = 0,0; b.x,b.y = 50,25; c.x,c.y = 99,49
    a.discovered = b.discovered = True
    a.connections=[1]; b.connections=[0,2]; c.connections=[1]
    grid = vr.spatial_map_grid(world, [], public_target=None, sector=None, columns=79, rows=20)
    text = "\n".join(grid)
    assert "@" in text and "o" in text and "." in text and ":" not in text
    grid = vr.spatial_map_grid(world, [1,2], public_target=2, sector=None, columns=79, rows=20)
    text = "\n".join(grid)
    assert "@" in text and "!" in text and "*" in text and ":" in text
    # Same projected cell must be labelled instead of silently losing a station.
    b.x,b.y = 50,25; c.x,c.y = 50,26
    text = "\n".join(vr.spatial_map_grid(world, [1,2], public_target=None, sector=None, columns=10, rows=4))
    assert "X" in text
    c.discovered = True
    text = "\n".join(vr.spatial_map_grid(world, [], public_target=None, sector=None, columns=10, rows=4))
    assert "+" in "\n".join(text.splitlines()[1:-1])


def test_spatial_map_sector_bounds_match_every_coordinate():
    for x in range(100):
        for y in range(50):
            sector = vr.SECTOR_NAMES.index(vr.sector_for(_Sys(x,y)))
            xmin,xmax,ymin,ymax = vr.map_bounds(sector)
            assert xmin <= x <= xmax and ymin <= y <= ymax


def test_spatial_map_current_position_and_repeated_route_legs_are_explicit():
    world = _world_with_seed(145)
    lines = vr.map_list_lines(world, [1,0,1], 1)
    here_line = next(line for line in lines if world.here.name in line)
    assert "@" in here_line and "here" in here_line
    assert "arrival leg(s): 1, 3" in " ".join(vr.map_inspection_lines(world, 1, [1,0,1], 1))


def test_spatial_map_preserves_tracked_objective_when_given_another_route(monkeypatch):
    world, mission = _mission_details_world("scan"); vr.accept_mission(world,mission); vr.track_mission(world,mission.id)
    supplied = [sorted(world.here.connections)[0]]
    seen=[]
    original=vr.spatial_map_grid
    def grid(current,path,**kwargs):
        seen.append((list(path),kwargs["public_target"]))
        return original(current,path,**kwargs)
    monkeypatch.setattr(vr,"spatial_map_grid",grid)
    monkeypatch.setattr(vr,"read_key",lambda:"B")
    with contextlib.redirect_stdout(io.StringIO()): vr.screen_galaxy_map(vr.Palette(False),world,path=supplied)
    assert seen==[(supplied,mission.target_system)]
    assert not world.by_id[mission.target_system].discovered


def test_route_screens_open_map_with_their_exact_path(monkeypatch):
    world, mission = _mission_details_world("bounty"); vr.accept_mission(world, mission)
    target = world.by_id[mission.target_system]; target.discovered = True
    opened = []
    monkeypatch.setattr(vr, "screen_galaxy_map", lambda p,w,**kw: opened.append(kw))
    keys = iter("VBVB"); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_mission_navigation(vr.Palette(False), world, mission, active=True)
        vr.screen_auto_route(vr.Palette(False), world, destination=target.id)
    assert opened == [{"path":vr.mission_route(world, mission), "public_target":target.id},
                      {"path":vr.bfs_path(world.by_id,0,target.id), "public_target":target.id}]


@pytest.mark.parametrize("width,height", [(20,10), (40,12), (80,24)])
def test_spatial_map_station_info_pages_preserve_every_known_link(monkeypatch, terminal, width, height):
    import re
    world = _world_with_seed(42)
    # Exercise a dense, long-lived chart without changing the graph generator.
    world.here.connections = list(range(1, len(world.galaxy)))
    for station in world.galaxy: station.discovered = True
    terminal(width, height)
    output = io.StringIO(); frames=[]
    def choose():
        frame=output.getvalue(); frames.append(frame); output.seek(0); output.truncate(0)
        match=re.search(r"Station Info (\d+)/(\d+)", " ".join(frame.split()))
        assert match
        return "B" if match[1] == match[2] else "N"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output): vr._screen_map_info(world, 0, [], None)
    assert all(len(frame.splitlines())<=height for frame in frames)
    assert all(vr._visible_width(line)<=width for frame in frames for line in frame.splitlines())
    text=" ".join(" ".join(frames).split())
    for station in world.galaxy: assert station.name in text


@pytest.mark.parametrize("commands", [b"CVBQQ", b"CV", b"CVLIBBQQ", b"CVLI1", b"CG1VB BQQ".replace(b" ",b"")])
def test_real_spatial_map_back_eof_and_inspection_preserve_career(tmp_path, commands):
    import json, os, subprocess
    world = _world_with_seed(42)
    world._checkpoint = lambda current: vr.persist(current,tmp_path,77); world.checkpoint()
    original = (tmp_path/"77.json").read_bytes()
    info=tmp_path/"door_info.json"; info.write_text(json.dumps({"user_id":77,"handle":"Tester"}),encoding="utf-8")
    result=subprocess.run([sys.executable,str(_VOIDRUNNER_PATH)],input=commands,capture_output=True,
        env=dict(os.environ,VOIDRUNNER_SAVE_DIR=str(tmp_path),NETBBS_DOOR_INFO=str(info)),timeout=10)
    assert result.returncode==0 and not result.stderr and b"Star Map:" in result.stdout
    if b"LI1" in commands: assert b"Station Info" in result.stdout
    assert (tmp_path/"77.json").read_bytes()==original


def test_chart_screen_offers_view_full_chart(monkeypatch):
    world = _world_with_seed(146)
    monkeypatch.setattr(vr, "read_key", lambda: "Q")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_chart(vr.Palette(truecolor=False), world)

    assert "[V]" in buf.getvalue()


def test_chart_screen_v_key_opens_the_galaxy_map(monkeypatch):
    world = _world_with_seed(147)
    keys = iter(["V", "B", "Q"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_chart(vr.Palette(truecolor=False), world)

    assert "Star Map:" in buf.getvalue()


def test_already_charted_survey_is_unavailable_without_replacement():
    world = _world_with_seed(42)
    target = next(s.id for s in world.galaxy if not s.discovered)
    mission = vr.Mission(1, "scan", "Survey", 500, 0, target)
    world.save.mission_boards[0] = {"refresh_turn": 3, "offers": [mission.to_dict()]}
    world.by_id[target].discovered = True
    assert vr.generate_mission_board(world) == []
    with pytest.raises(vr.MissionError, match="already charted"):
        vr.accept_mission(world, mission)
    assert world.save.mission_boards[0]["offers"] == [mission.to_dict()]


@pytest.mark.parametrize("seed", [42, 150])
@pytest.mark.parametrize("tier,navigator", [(1, False), (1, True), (2, False), (2, True)])
def test_area_survey_charts_exact_range_without_time_or_rng(seed, tier, navigator):
    import copy
    world = _world_with_seed(seed); world.save.ship.scanner_tier = tier; world.save.ship.has_navigator = navigator
    world.sync_discovered()
    hops = vr.bfs_hops(world.by_id, world.save.current_system)
    before = set(world.save.discovered)
    expected = {sid for sid, distance in hops.items() if distance <= 2 + tier + navigator and sid not in before}
    rng, turn, fuel, credits = world.event_rng.getstate(), world.save.turn, world.save.ship.fuel, world.save.pilot.credits
    observations = copy.deepcopy(world.save.market_memory)
    summary, report = vr.perform_survey(world)
    assert set(world.save.discovered) - before == expected
    assert world.save.ship.fuel == fuel - 2 and world.save.turn == turn and world.save.pilot.credits == credits
    assert world.event_rng.getstate() == rng and world.save.market_memory == observations
    assert str(len(expected)) in summary
    for sid in expected:
        system = world.by_id[sid]
        assert any(system.name in row and system.station_name in row and system.economy in row and f"danger {system.danger}/5" in row for row in report)
    saved = copy.deepcopy(world.save.to_dict())
    with pytest.raises(ValueError): vr.perform_survey(world)
    assert world.save.to_dict() == saved


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
def test_landmark_inspection_back_keeps_unclaimed_salvage(monkeypatch, terminal, width, height):
    import copy,re
    world = _world_with_seed(42); world.save.current_system = world.landmark["system_id"]
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    terminal(width, height)
    output, frames = io.StringIO(), []
    world._checkpoint = lambda w: pytest.fail("Inspecting landmark wrote a checkpoint")
    def choose():
        frame = output.getvalue(); output.seek(0); output.truncate(0); frames.append(frame)
        assert len(frame.splitlines()) <= height
        assert all(vr._visible_width(row) <= width for row in frame.splitlines())
        page, count = map(int, re.search(r"(\d+)/(\d+)", frame).groups())
        return "B" if page == count else ">"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output): vr.screen_landmark(vr.Palette(False), world)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng
    assert "Unclaimed salvage: 3000cr." in " ".join(" ".join(frames).split())


def test_a_commit_generates_no_galaxy_once_the_seed_is_known(monkeypatch, tmp_path):
    """Every commit validated twice and each validation built the galaxy up to
    three times, so one acknowledged trade cost six generations (issue #419).

    The world persists for real here, so the commit runs `_encode_career_checkpoint`
    and the validator rather than stopping at a `None` callback (#419 review).
    """
    world = _world_with_seed(42)
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.save.active_event = {"economy": world.here.economy, "commodity": "food", "direction": "boom",
                               "turns_remaining": 2, "description": "Prices spike across the region",
                               "system_ids": [world.here.id]}
    generated = []
    real = vr.generate_galaxy
    vr.galaxy_economies.cache_clear(); vr.galaxy_hops.cache_clear()
    monkeypatch.setattr(vr, "generate_galaxy", lambda seed: (generated.append(seed), real(seed))[1])
    world.checkpoint()
    assert (tmp_path / "77.json").exists(), "the commit must have gone through persistence"
    assert len(generated) <= 2, generated  # once per pure helper, however many validations run
    generated.clear()
    world.save.pilot.credits += 1
    world.commit()
    assert generated == []  # and never again for this seed
    world.save.active_event = None
    world.save.pilot.credits += 1
    world.commit()
    assert generated == []


def test_the_pure_galaxy_caches_answer_from_the_seed_alone():
    vr.galaxy_economies.cache_clear(); vr.galaxy_hops.cache_clear()
    economies = vr.galaxy_economies(42)
    galaxy = vr.generate_galaxy(42)
    assert list(economies) == [station.economy for station in galaxy]
    hops = vr.galaxy_hops(42, 0)
    real = vr.bfs_hops({station.id: station for station in galaxy}, 0)
    assert all(hops[sid] == real.get(sid, -1) for sid in range(vr.GALAXY_SYSTEM_COUNT))
    galaxy[1].discovered = True  # mutating a generated galaxy cannot reach the caches
    assert vr.galaxy_economies(42) is economies


def test_a_chart_entry_that_spans_pages_carries_its_letter_on_each(monkeypatch, terminal):
    """A page showing only a continuation still has to show the key that picks it."""
    import re
    terminal(20, 10)
    world = _world_with_seed(42)
    world.checkpoint()
    pages = vr._chart_pages(world, "Navigation", "[G] Route planner [B] Back: ", None)
    for rows, choices in pages:
        shown = {match[0] for row in rows for match in re.findall(r"^\[([A-Z])\] ", row)}
        assert set(choices) <= shown, (choices, rows)  # every offered letter is visible here
    spanning = [page for page in pages if any(row.startswith("    ") for row in page[0])]
    assert spanning or all(len(page[0]) <= 10 for page in pages)
