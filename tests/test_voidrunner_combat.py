"""Fights: the tactical ruleset, squadrons, bounty identification and
what a fight costs.

Split out of `test_voidrunner_domain.py` (issue #422).
"""

from __future__ import annotations

import contextlib
import io
import random

import pytest

from .support import plain, plain as plainly, _add_cargo, _box_rows, _door_stopped_at, _mission_details_world, _set_cargo, _world_at_food_producer, _world_with_exploration_choice, _world_with_pending_fight, _world_with_seed, page_rows, page_text, page_title, vr


def test_fire_damages_both_sides_and_is_driven_by_world_event_rng():
    world = _world_with_seed(9)
    world.event_rng = random.Random(1)
    pirate = vr.Pirate(name="Test Raider", tier=1, hp=35, hp_max=35)
    starting_hull = world.save.ship.hull_hp

    dmg_to_pirate, dmg_to_player, lines = vr.tactical_round(world, pirate, vr.new_tactics(pirate), "F")

    assert dmg_to_pirate > 0
    assert pirate.hp == 35 - dmg_to_pirate
    assert world.save.ship.hull_hp == starting_hull - dmg_to_player
    assert lines


def test_higher_weapon_tier_deals_more_damage_with_same_rng_sequence():
    world_weak = _world_with_seed(10)
    world_weak.event_rng = random.Random(42)
    world_strong = _world_with_seed(10)
    world_strong.event_rng = random.Random(42)
    world_strong.save.ship.weapon_tier = 4

    pirate_weak = vr.Pirate(name="X", tier=2, hp=100, hp_max=100)
    pirate_strong = vr.Pirate(name="X", tier=2, hp=100, hp_max=100)

    dmg_weak, _, _ = vr.tactical_round(world_weak, pirate_weak, vr.new_tactics(pirate_weak), "F")
    dmg_strong, _, _ = vr.tactical_round(world_strong, pirate_strong, vr.new_tactics(pirate_strong), "F")

    assert dmg_strong > dmg_weak


def test_shields_reduce_incoming_damage():
    world_bare = _world_with_seed(11)
    world_bare.event_rng = random.Random(7)
    world_shielded = _world_with_seed(11)
    world_shielded.event_rng = random.Random(7)
    world_shielded.save.ship.shield_tier = 3

    pirate_a = vr.Pirate(name="Y", tier=3, hp=1000, hp_max=1000)  # never dies mid-round
    pirate_b = vr.Pirate(name="Y", tier=3, hp=1000, hp_max=1000)

    _, dmg_bare, _ = vr.tactical_round(world_bare, pirate_a, vr.new_tactics(pirate_a), "F")
    _, dmg_shielded, _ = vr.tactical_round(world_shielded, pirate_b, vr.new_tactics(pirate_b), "F")

    assert dmg_shielded <= dmg_bare


def _system_with_danger(world, danger: int):
    system = world.by_id[world.here.connections[0]]
    system.danger = danger
    return system


def test_squadron_never_spawns_below_the_minimum_danger():
    world = _world_with_seed(90)
    dest = _system_with_danger(world, vr.SQUADRON_MIN_DANGER - 1)
    world.event_rng.random = lambda: 0.0  # would always succeed the squadron roll, if it ran at all

    pirates = vr.generate_pirate_squadron(world, dest)

    assert len(pirates) == 1


def test_squadron_spawns_at_the_minimum_danger_when_the_roll_succeeds():
    world = _world_with_seed(91)
    dest = _system_with_danger(world, vr.SQUADRON_MIN_DANGER)
    world.event_rng.random = lambda: 0.0  # always below SQUADRON_CHANCE

    pirates = vr.generate_pirate_squadron(world, dest)

    assert len(pirates) == vr.SQUADRON_SIZE


def test_squadron_does_not_spawn_at_high_danger_when_the_roll_fails():
    world = _world_with_seed(92)
    dest = _system_with_danger(world, vr.SQUADRON_MIN_DANGER)
    world.event_rng.random = lambda: 1.0  # always above SQUADRON_CHANCE

    pirates = vr.generate_pirate_squadron(world, dest)

    assert len(pirates) == 1


def test_squadron_encounter_fights_every_ship_on_a_full_win(monkeypatch):
    world = _world_with_seed(93)
    dest = _system_with_danger(world, vr.SQUADRON_MIN_DANGER)
    world.event_rng.random = lambda: 0.0  # triggers the encounter, then spawns a squadron

    calls = []
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: calls.append(pirate) or "won")
    world.event_rng.choices = lambda population, weights: ["pirate"]

    with contextlib.redirect_stdout(io.StringIO()):
        vr._resolve_random_travel_encounter(vr.Palette(truecolor=False), world, dest)

    assert len(calls) == vr.SQUADRON_SIZE


def test_squadron_encounter_stops_after_an_escape_not_a_full_win(monkeypatch):
    world = _world_with_seed(94)
    dest = _system_with_danger(world, vr.SQUADRON_MIN_DANGER)
    world.event_rng.random = lambda: 0.0

    calls = []
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: calls.append(pirate) or "escaped")
    world.event_rng.choices = lambda population, weights: ["pirate"]

    with contextlib.redirect_stdout(io.StringIO()):
        vr._resolve_random_travel_encounter(vr.Palette(truecolor=False), world, dest)

    assert len(calls) == 1  # never reached the second ship


def test_destroy_ship_clears_cargo_and_returns_player_to_freeport_with_full_hull():
    world = _world_with_seed(12)
    _add_cargo(world, "ore", 10)
    world.save.current_system = world.here.connections[0]
    world.save.ship.hull_hp = 0
    world.save.pilot.notoriety = 7

    vr.destroy_ship(world)

    assert world.save.cargo == {}
    assert world.save.current_system == 0
    assert world.save.ship.hull_hp == vr.hull_hp_max(world.save.ship)
    assert world.save.pilot.credits == 1200 - vr.salvage_fee(world.save.ship)  # salvage fee charged
    assert world.save.pilot.notoriety == 7  # a raider kill does not clear wanted status (#402)


@pytest.mark.parametrize("commands", [b"CAYF", b"CRJF", b"CGD1JF"])
def test_combat_survives_kill_and_resumes_before_station_access(tmp_path, monkeypatch, commands):
    import json

    world = _world_with_seed(42)
    world.event_rng.seed(0)
    destination = sorted(world.here.connections)[0]
    world.save.active_missions = [
        vr.Mission(1, "bounty", "Intercept raider", 500, 0, destination, pirate_tier=2),
    ]
    if commands.startswith(b"CR"):
        world.save.tracked_mission_id = 1
    if commands.startswith(b"CG"):
        for station in world.galaxy: station.discovered = station.id in (0, destination)
        world.sync_discovered()
    world.checkpoint()  # Include the station preparation before real startup.
    vr.persist(world, tmp_path, 77)
    initial = json.loads((tmp_path / "77.json").read_text(encoding="utf-8"))
    with _door_stopped_at(tmp_path, commands, b" damage."):
        saved = json.loads((tmp_path / "77.json").read_text(encoding="utf-8"))
    assert saved["turn"] == initial["turn"] + 1
    combat = saved["pending_travel"]["encounter"]["combat"]
    assert 0 < combat["pirate"]["hp"] < combat["pirate"]["hp_max"]
    assert 0 < saved["ship"]["hull_hp"] < initial["ship"]["hull_hp"]
    assert saved["ship"]["fuel"] < initial["ship"]["fuel"]

    # A fresh executable must resume the opponent, not reveal a station menu.
    with _door_stopped_at(tmp_path, b"I", b"TACTICAL SYSTEMS") as output:
        assert b"Resuming your interrupted journey" in output
        assert b"Freeport Anchorage" not in output
        assert json.loads((tmp_path / "77.json").read_text(encoding="utf-8")) == saved

    # The entire career, including RNG position and bounty failure, matches an
    # uninterrupted fight. Use a different requested destination on resume to
    # prove a saved journey cannot be redirected around its encounter.
    expected = vr.World(vr.SaveData.from_dict(initial))
    resumed = vr.World(vr.SaveData.from_dict(saved))
    monkeypatch.setattr(vr, "read_key", lambda: "F")
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), expected, destination)
        vr.screen_travel(vr.Palette(False), resumed, 0)
    assert resumed.save.to_dict() == expected.save.to_dict()
    assert resumed.save.pending_travel is None
    assert resumed.save.current_system == destination
    assert resumed.save.pilot.kills == 1
    assert not resumed.save.active_missions


def test_failed_combat_save_does_not_acknowledge_damage_or_retry(tmp_path, monkeypatch):
    import json

    world = _world_with_seed(42)
    destination = world.here.connections[0]
    world.save.active_missions = [vr.Mission(1, "bounty", "Raider", 500, 0, destination, pirate_tier=2)]
    reads = 0

    def checkpoint(current):
        if current.save.ship.hull_hp < 60:
            raise OSError("disk full")
        vr.persist(current, tmp_path, 77)

    def choose():
        nonlocal reads
        reads += 1
        return "F"

    world._checkpoint = checkpoint
    monkeypatch.setattr(vr, "read_key", choose)
    output = io.StringIO()
    with contextlib.redirect_stdout(output), pytest.raises(vr.SaveError):
        vr.screen_travel(vr.Palette(False), world, destination)
    assert reads == 1
    assert " damage." not in output.getvalue()
    saved = json.loads((tmp_path / "77.json").read_text(encoding="utf-8"))
    assert saved["ship"]["hull_hp"] == 60
    combat = saved["pending_travel"]["encounter"]["combat"]
    assert combat["pirate"]["hp"] == combat["pirate"]["hp_max"]


@pytest.mark.parametrize("kind", ["bounty", "escort"])
@pytest.mark.parametrize("deadline", [0, 1])
def test_departure_expires_combat_jobs_before_encounters(kind, deadline, monkeypatch):
    world = _world_with_seed(42)
    dest = world.here.connections[0]
    world.save.active_missions = [vr.Mission(1, kind, "Deadline", 500, 0, dest, deadline_turn=deadline, pirate_tier=1)]
    calls = []
    monkeypatch.setattr(vr, "screen_combat", lambda *args: calls.append(1) or "won")
    monkeypatch.setattr(vr, "_resolve_random_travel_encounter", lambda *args: None)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, dest)
    assert calls == ([1] if deadline == 1 else [])
    assert world.save.pilot.credits == 1200 + (500 if deadline == 1 else 0)
    assert not world.save.active_missions


@pytest.mark.parametrize("kind", ["bounty", "escort"])
@pytest.mark.parametrize("destroyed", [False, True])
def test_a_resumed_expired_combat_job_cannot_pay_or_start_another_wave(kind, destroyed, monkeypatch):
    world = _world_with_seed(42)
    world.save.turn = 2
    expired = vr.Mission(1, kind, "Expired", 500, 0, 1, deadline_turn=1, pirate_tier=1)
    later = vr.Mission(2, "escort", "Later", 500, 0, 1, deadline_turn=5, pirate_tier=1)
    world.save.active_missions = [expired] + ([later] if destroyed and kind == "escort" else [])
    travel = {
        "version": 1, "origin": 0, "destination": 1, "was_discovered": True,
        "destroyed": destroyed, "phase": "primary" if kind == "bounty" else "escorts",
        "primary": kind if kind == "bounty" else "random",
        "bounty": expired.to_dict() if kind == "bounty" else None,
        "escorts": [m.to_dict() for m in world.save.active_missions if m.kind == "escort"],
        "escort_index": 0, "encounter": {},
    }
    world.save.pending_travel = travel
    world.save.event_rng_state = world.event_rng.getstate()
    world = vr.World(vr.SaveData.from_dict(world.save.to_dict()))
    monkeypatch.setattr(vr, "screen_combat", lambda *args: pytest.fail("Expired job started combat"))
    with contextlib.redirect_stdout(io.StringIO()):
        if kind == "bounty":
            vr._resolve_bounty(vr.Palette(False), world, world.save.pending_travel)
        else:
            vr._resolve_escort_missions(vr.Palette(False), world, 1)
    assert world.save.pilot.credits == 1200
    assert expired not in world.save.active_missions
    assert world.save.pilot.missions_completed == 0
    if destroyed and kind == "escort":
        assert world.save.active_missions == [later]
        assert world.save.pending_travel["escort_index"] == 1


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
@pytest.mark.parametrize("patrol", [False, True])
@pytest.mark.parametrize("style", ["auto", "plain"])
def test_combat_telemetry_pages_fit_and_browsing_preserves_exchange(monkeypatch, terminal, without_action_bar, width, height, patrol, style):
    import re
    terminal(width, height, style)
    world = _world_with_seed(42)
    _add_cargo(world, "food", 3)
    pirate = vr.generate_pirate(world, tier=2)
    snapshots, frames = [], []
    world._checkpoint = lambda current: snapshots.append(current.save.to_dict())
    output = io.StringIO()
    state = {"fired": False, "details": False, "saved": None, "rng": None}
    def choose():
        frame = output.getvalue(); output.seek(0); output.truncate(0)
        assert len(frame.splitlines()) <= height
        assert all(vr._visible_width(line) <= width for line in frame.splitlines())
        frames.append(without_action_bar(vr._ANSI_RE.sub("", frame)))
        # A one-page screen drops its paging tokens (#412); the counter is the oracle.
        bar = page_text(frame)                        # a narrow bar wraps (#400)
        assert "[I] Info" in bar and ("[<>] Page:" in bar or "/1" in page_title(frame))
        if not state["fired"]:
            state["fired"] = True
            return "F"
        if state["saved"] is None:
            state["saved"] = world.save.to_dict()
            state["rng"] = world.event_rng.getstate()
        assert world.save.to_dict() == state["saved"]
        assert world.event_rng.getstate() == state["rng"]
        page, count = map(int, re.search(r"Combat.*?(\d+)/(\d+)", frame, re.S).groups())
        if page < count: return ">"
        if not state["details"]:
            state["details"] = True
            return "I"
        raise EOFError
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output), pytest.raises(EOFError):
        vr._screen_combat_session(vr.Palette(False), world, pirate, patrol=patrol)
    assert len(snapshots) == 2
    # Join content rows only: a narrow page splits a phrase across frames, and the page
    # header and the echoed keypress would otherwise land between its two words. The
    # action bar is already gone -- `without_action_bar` took it off as it was written.
    def body(frame):
        return [row for row in page_rows(frame)
                if not re.match(r"Combat [\d,]+cr \d+/\d+\s*$", row)
                and not re.fullmatch(r"[A-Z0-9<>]", row)]
    text = " ".join(" ".join(row for frame in frames for row in body(frame)).split())
    # The fight's groups are named by rules across the frame now, which are
    # border rows; what is left in the body is the readings themselves.
    for label in ("damage.", "3/24", "Your hull", "Fuel", "Shields Tier"):
        assert label in text
    if patrol: assert "clear notoriety" in text and "no salvage" in text
    else: assert "one unit" in text and "only if accepted" in text and "refusal draws" in text


@pytest.mark.parametrize("patrol", [False, True])
def test_combat_telemetry_browsing_does_not_change_fight_result(monkeypatch, patrol):
    worlds = [_world_with_seed(42), _world_with_seed(42)]
    results = []
    for index, world in enumerate(worlds):
        world.event_rng.seed(42)
        pirate = vr.generate_pirate(world, tier=2)
        keys = iter("Q><Q?" * 5 if index else "")
        monkeypatch.setattr(vr, "read_key", lambda: next(keys, "F"))
        with contextlib.redirect_stdout(io.StringIO()):
            results.append(vr._screen_combat_session(vr.Palette(False), world, pirate, patrol=patrol))
    assert results[0] == results[1]
    assert worlds[0].save.to_dict() == worlds[1].save.to_dict()
    assert worlds[0].event_rng.getstate() == worlds[1].event_rng.getstate()


@pytest.mark.parametrize("patrol", [False, True])
def test_combat_telemetry_unaffordable_actions_have_terms_without_hotkeys(patrol):
    world = _world_with_seed(42)
    world.save.pilot.credits = 0
    pirate = vr.generate_pirate(world, tier=2)
    before, rng = world.save.to_dict(), world.event_rng.getstate()
    text = " ".join(vr.combat_display_lines(world, pirate, [], patrol=patrol, details=True, tactics=vr.new_tactics(pirate)))
    assert ("[S]" if patrol else "[B]") not in text
    assert "UNAFFORDABLE" in text
    assert str(vr.notoriety_fine_cost(0) if patrol else vr.bribe_cost(pirate)) + "cr" in text
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


@pytest.mark.parametrize("missing", ["tactics", "hull_before"])
def test_a_pending_fight_without_its_ruleset_or_hull_mark_is_refused(tmp_path, missing):
    """Pre-tactics fights were a retired schema's shape; there is no fallback (#421)."""
    import json
    world, _ = _world_with_pending_fight()
    document = world.save.to_dict()
    del document["pending_travel"]["encounter"]["combat"][missing]
    path = tmp_path / "77.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(vr.ResumeError): vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,value", [("version", 2), ("version", True), ("profile", "unknown"),
    ("profile", []), ("step", -1), ("step", 3), ("step", True), ("brace_ready", None), ("extra_rule", 1)])
def test_invalid_tactical_metadata_preserves_saved_career(tmp_path, field, value):
    import json
    tactics = {"version": 1, "profile": "Raider", "step": 0, "brace_ready": True}
    tactics[field] = value
    world, _ = _world_with_pending_fight(tactics=tactics)
    path = tmp_path / "77.json"
    path.write_text(json.dumps(world.save.to_dict()), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(vr.ResumeError): vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == before


def test_null_tactics_is_rejected_rather_than_defaulted(tmp_path):
    import json
    world, _ = _world_with_pending_fight()
    document = world.save.to_dict()
    document["pending_travel"]["encounter"]["combat"]["tactics"] = None
    path = tmp_path / "77.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(vr.ResumeError): vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == before


def test_tactical_brace_trades_firepower_for_damage_and_requires_fire_to_recharge():
    import copy
    original = _world_with_seed(42)
    original.event_rng.seed(31)
    original.save.ship.hull_hp = 60
    fighters = [copy.deepcopy(original), copy.deepcopy(original)]
    enemies = [vr.Pirate("Test", 2, 50, 50), vr.Pirate("Test", 2, 50, 50)]
    states = [{"version": 1, "profile": "Raider", "step": 1, "brace_ready": True} for _ in range(2)]
    fired = vr.tactical_round(fighters[0], enemies[0], states[0], "F")
    braced = vr.tactical_round(fighters[1], enemies[1], states[1], "G")
    assert 0 < braced[0] < fired[0] and 0 < braced[1] < fired[1]
    assert states[0]["brace_ready"] and not states[1]["brace_ready"]
    before = (copy.deepcopy(fighters[1].save.to_dict()), copy.deepcopy(states[1]), enemies[1].hp, fighters[1].event_rng.getstate())
    with pytest.raises(ValueError): vr.tactical_round(fighters[1], enemies[1], states[1], "G")
    assert before == (fighters[1].save.to_dict(), states[1], enemies[1].hp, fighters[1].event_rng.getstate())
    vr.tactical_round(fighters[1], enemies[1], states[1], "F")
    assert states[1]["brace_ready"]


@pytest.mark.parametrize("profile", list(vr.TACTICAL_PROFILES))
@pytest.mark.parametrize("step", [0, 1, 2])
@pytest.mark.parametrize("action", ["F", "G"])
def test_tactical_intent_displayed_damage_range_matches_resolution(monkeypatch, profile, step, action):
    import re
    world = _world_with_seed(42)
    world.save.ship.shield_tier = 1
    pirate = vr.Pirate("Probe", 2, 200, 200)
    tactics = {"version": 1, "profile": profile, "step": step, "brace_ready": True}
    lines = vr.combat_display_lines(world, pirate, [], patrol=False, tactics=tactics, details=True)
    label = next(plainly(line) for line in lines
                 if (plainly(line).startswith("[G]") if action == "G" else " intent " in plainly(line)))
    low, high = map(int, re.search(r"incoming (\d+)-(\d+)", label).groups())
    monkeypatch.setattr(world.event_rng, "randint", lambda lo, hi: hi)
    _, received, _ = vr.tactical_round(world, pirate, tactics, action)
    assert received == high and low <= high
    assert tactics["step"] == (step + 1) % 3


@pytest.mark.parametrize("action", ["E", "P"])
def test_failed_tactical_disengagement_uses_and_advances_visible_intent(monkeypatch, action):
    tactics = {"version": 1, "profile": "Raider", "step": 1, "brace_ready": True}
    world, pirate = _world_with_pending_fight(tactics=tactics)
    before = world.save.ship.hull_hp
    monkeypatch.setattr(world.event_rng, "random", lambda: 0.99)
    monkeypatch.setattr(world.event_rng, "randint", lambda lo, hi: hi)
    keys = iter([action])
    def choose():
        try: return next(keys)
        except StopIteration: raise EOFError
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(io.StringIO()), pytest.raises(EOFError):
        vr.screen_combat(vr.Palette(False), world, pirate)
    assert world.save.ship.hull_hp == before - vr._tactical_incoming_damage(world.save.ship, 2, "volley", 9, tactics)
    assert tactics["step"] == 2 and tactics["brace_ready"]


def test_tactical_brace_checkpoint_survives_real_kill_and_invalid_repeat(tmp_path):
    world = _world_with_seed(42)
    world.event_rng.seed(0)
    destination = sorted(world.here.connections)[0]
    world.save.active_missions = [vr.Mission(1, "bounty", "Intercept raider", 500, 0, destination, pirate_tier=2)]
    world.checkpoint(); vr.persist(world, tmp_path, 77)
    with _door_stopped_at(tmp_path, b"CAYG", b"Guarding;"):
        saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        combat = saved.pending_travel["encounter"]["combat"]
        assert combat["tactics"]["version"] == vr.TACTICAL_RULESET_VERSION and not combat["tactics"]["brace_ready"]
        assert combat["tactics"]["step"] == 1 and 0 < combat["pirate"]["hp"] < combat["pirate"]["hp_max"]
    before = (tmp_path / "77.json").read_bytes()
    with _door_stopped_at(tmp_path, b"GI", b"TACTICAL SYSTEMS"):
        assert (tmp_path / "77.json").read_bytes() == before


@pytest.mark.parametrize("build,tier,waves", [("starter", 2, 1), ("carrier", 4, 2)])
def test_tactical_seeded_probes_keep_fights_short_and_upgrade_threat_meaningful(build, tier, waves):
    import copy
    template = _world_with_seed(42)
    if build == "carrier":
        ship = template.save.ship
        ship.hull_class = "Carrier"; ship.hull_tier = 4; ship.weapon_tier = 4; ship.shield_tier = 3
        ship.has_gunner = True; ship.hull_hp = vr.hull_hp_max(ship)
    results = {}
    for profile in vr.TACTICAL_PROFILES:
        for strategy in ("v1_fire", "fire", "brace_volley"):
            wins = 0; rounds = []; damage = []
            for seed in range(64):
                world = copy.deepcopy(template); world.event_rng.seed(seed)
                start = world.save.ship.hull_hp; count = 0
                for wave in range(waves):
                    pirate = vr.Pirate("Probe", tier, 20 + tier * 15, 20 + tier * 15)
                    version = 1 if strategy == "v1_fire" else 2
                    tactics = {"version": version, "profile": profile, "step": 0, "brace_ready": True}
                    while pirate.hp > 0 and world.save.ship.hull_hp > 0:
                        assert count < 12
                        action = "G" if strategy == "brace_volley" and vr.tactical_intent(tactics) == "volley" and tactics["brace_ready"] else "F"
                        vr.tactical_round(world, pirate, tactics, action)
                        count += 1
                    if world.save.ship.hull_hp <= 0: break
                wins += world.save.ship.hull_hp > 0; rounds.append(count); damage.append(start - world.save.ship.hull_hp)
            results[profile, strategy] = (wins, max(rounds), sum(damage))
        old, fire, guarded = (results[profile, strategy] for strategy in ("v1_fire", "fire", "brace_volley"))
        assert fire[1] <= 8 and guarded[1] <= 8
        # Ruleset 2 exists because 1 was unwinnable at the top tiers (issue #406).
        if build == "starter":
            assert fire[0] >= old[0] and fire[0] >= 32 and guarded[0] >= fire[0]
        else:
            assert fire[0] == 64 and fire[2] <= old[2]
    if build == "carrier":
        assert results["Raider", "brace_volley"][2] < results["Raider", "fire"][2]
        assert results["Skirmisher", "fire"][2] < results["Skirmisher", "brace_volley"][2]


@pytest.mark.parametrize("danger", [0, 1, 2, 3, 4, 5])
def test_raider_tiers_follow_destination_danger_and_preserve_rng_draw_order(danger):
    import copy
    world = _world_with_seed(42)
    world.here.danger = max(0, 4 - danger)
    destination = next(s for s in world.galaxy if s.id != world.here.id)
    destination.danger = danger
    for seed in range(32):
        world.event_rng.seed(seed)
        expected = copy.deepcopy(world.event_rng)
        count = 2 if danger >= vr.SQUADRON_MIN_DANGER and expected.random() < vr.SQUADRON_CHANCE else 1
        expected_ships = []
        for _ in range(count):
            tier = max(0, min(4, danger + expected.randint(-1, 1)))
            expected_ships.append((tier, expected.choice(vr.PIRATE_NAMES)))
        pirates = vr.generate_pirate_squadron(world, destination)
        assert [(p.tier, p.name) for p in pirates] == expected_ships
        assert world.event_rng.getstate() == expected.getstate()


def _world_with_bounty_warrant(*, matches=True, checked=False, engaged=False):
    world, pirate = _world_with_pending_fight(tactics={"version": 1, "profile": "Raider", "step": 0, "brace_ready": True})
    state = world.save.pending_travel["encounter"]
    state["pirate"] = vr.dataclasses.asdict(pirate)
    state["warrant"] = {"version": 1, "matches": matches, "checked": checked, "engaged": engaged}
    world.by_id[world.save.pending_travel["destination"]].discovered = True
    return world, pirate, state["warrant"]


def test_bounty_warrant_truth_is_stable_and_does_not_consume_encounter_rng():
    import copy
    world = _world_with_seed(42)
    matches = []
    for mid in range(1, 101):
        mission = vr.Mission(mid, "bounty", "Target", 500, 0, 7, pirate_tier=2)
        before = world.event_rng.getstate()
        first = vr.new_bounty_warrant(world, mission)
        assert world.event_rng.getstate() == before
        world.event_rng.random()
        resumed = vr.World(vr.SaveData.from_dict(copy.deepcopy(world.save.to_dict())))
        assert vr.new_bounty_warrant(resumed, mission) == first
        matches.append(first["matches"])
    assert 5 <= matches.count(False) <= 25


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
@pytest.mark.parametrize("matches", [False, True])
def test_bounty_identification_risk_is_visible_and_paging_is_read_only(monkeypatch, terminal, width, height, matches):
    import copy, re
    terminal(width, height)
    world, pirate, warrant = _world_with_bounty_warrant(matches=matches)
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    output = io.StringIO(); frames = []
    def choose():
        frame = output.getvalue(); output.seek(0); output.truncate(0); frames.append(frame)
        assert len(frame.splitlines()) <= height
        assert all(vr._visible_width(line) <= width for line in frame.splitlines())
        assert world.save.to_dict() == before and world.event_rng.getstate() == rng
        page, count = map(int, re.search(r"Combat.*?(\d+)/(\d+)", frame, re.S).groups())
        if page == count: raise EOFError
        return ">"
    monkeypatch.setattr(vr, "read_key", choose)
    world._checkpoint = lambda current: pytest.fail("Identification browsing checkpointed")
    with contextlib.redirect_stdout(output), pytest.raises(EOFError): vr.screen_combat(vr.Palette(False), world, pirate)
    text = page_text(frames)
    assert "[V] Verify" in text and "[W] Withdraw" in text
    # The mismatch rate is on the identification panel, wherever the
    # terminal's height put it.
    assert "12%" in text
    assert "Identity mismatch confirmed" not in text


@pytest.mark.parametrize("matches,commands", [(False, "VR"), (False, "W"), (True, "VF"), (False, "VF"), (False, "F")])
def test_bounty_identification_choices_resolve_contract_with_explicit_consequences(monkeypatch, matches, commands):
    world, pirate, warrant = _world_with_bounty_warrant(matches=matches)
    world.save.ship.hull_class = "Carrier"; world.save.ship.hull_hp = vr.hull_hp_max(world.save.ship)
    world.save.ship.weapon_tier = 4; world.save.ship.has_gunner = True
    start_fuel, credits = world.save.ship.fuel, world.save.pilot.credits
    keys = iter(commands)
    monkeypatch.setattr(vr, "read_key", lambda: next(keys, "F"))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_travel(vr.Palette(False), world, world.save.pending_travel["destination"])
    assert world.save.pending_travel is None
    assert world.save.ship.fuel == start_fuel - ("V" in commands)
    if commands in ("VR", "W"):
        assert world.save.pilot.credits == credits
        assert world.save.pilot.missions_completed == world.save.pilot.kills == world.save.pilot.notoriety == 0
        assert bool(world.save.active_missions) == (commands == "W")
    else:
        assert world.save.pilot.missions_completed == world.save.pilot.kills == 1
        assert world.save.pilot.credits == credits + 500 + 160
        assert world.save.pilot.notoriety == (0 if matches else vr.NOTORIETY_PER_WRONG_BOUNTY_KILL)
        assert world.save.pilot.reputation[vr.FACTION_CONCORD] == (2 if matches else -1)
    if "V" in commands:
        said = plainly(output.getvalue())
        assert "Identity confirmed" in said if matches else "Identity mismatch confirmed" in said


@pytest.mark.parametrize("fault", ["checked", "engaged", "fuel"])
def test_bounty_verification_rejects_before_changing_any_state(fault):
    import copy
    world, _, warrant = _world_with_bounty_warrant()
    if fault == "fuel": world.save.ship.fuel = 0
    else: warrant[fault] = True
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    with pytest.raises(ValueError): vr.verify_bounty_identity(world, warrant)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


def test_bounty_identification_and_free_withdrawal_close_after_engagement(monkeypatch):
    import copy
    world, pirate, warrant = _world_with_bounty_warrant(matches=False)
    keys = iter(["F", "V", "W", "R"]); before = None; rng = None
    def choose():
        nonlocal before, rng
        if warrant["engaged"]:
            if before is None: before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
            assert world.save.to_dict() == before and world.event_rng.getstate() == rng
        try: return next(keys)
        except StopIteration: raise EOFError
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(io.StringIO()), pytest.raises(EOFError): vr.screen_combat(vr.Palette(False), world, pirate)
    assert warrant["engaged"] and not warrant["checked"]


@pytest.mark.parametrize("field,value", [("version", 2), ("version", True), ("matches", 1), ("checked", None), ("engaged", "yes"), ("extra", 1)])
def test_invalid_bounty_identification_metadata_preserves_career(tmp_path, field, value):
    import json
    world, _, warrant = _world_with_bounty_warrant()
    warrant[field] = value
    path = tmp_path / "77.json"; path.write_text(json.dumps(world.save.to_dict()), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(vr.ResumeError): vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == before


@pytest.mark.parametrize("fault", ["matching", "unchecked", "engaged", "wrong_phase", "damaged", "missing"])
def test_contradictory_bounty_report_is_rejected_without_overwriting(tmp_path, fault):
    import json
    world, _, warrant = _world_with_bounty_warrant(matches=False, checked=True)
    travel = world.save.pending_travel; combat = travel["encounter"]["combat"]
    combat["outcome"] = "reported"
    if fault == "matching": warrant["matches"] = True
    elif fault == "unchecked": warrant["checked"] = False
    elif fault == "engaged": warrant["engaged"] = True
    elif fault == "wrong_phase": travel["phase"] = "escorts"
    elif fault == "missing": travel["encounter"].pop("warrant")
    else: combat["pirate"]["hp"] -= 1
    path = tmp_path / "77.json"; path.write_text(json.dumps(world.save.to_dict()), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(vr.ResumeError): vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == before


def test_bounty_verification_and_report_survive_real_kills_without_duplicate_effects(tmp_path):
    world, _, warrant = _world_with_bounty_warrant(matches=False)
    vr.persist(world, tmp_path, 77)
    initial_fuel, credits = world.save.ship.fuel, world.save.pilot.credits
    with _door_stopped_at(tmp_path, b"V", b"Identity mismatch confirmed"):
        saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert saved.pending_travel["encounter"]["warrant"]["checked"]
        assert saved.ship.fuel == initial_fuel - 1
    with _door_stopped_at(tmp_path, b"R", b"Incorrect identity reported"):
        saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert saved.pilot.credits == credits and saved.ship.fuel == initial_fuel - 1
    # The parent may consume the checkpointed report before the reader kills it.
    if saved.active_missions:
        assert saved.pending_travel["encounter"]["combat"]["outcome"] == "reported"
        with _door_stopped_at(tmp_path, b"", b"Incorrect warrant closed"):
            saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert not saved.active_missions
    assert saved.pilot.credits == credits and saved.ship.fuel == initial_fuel - 1
    assert saved.pilot.kills == saved.pilot.missions_completed == saved.pilot.notoriety == 0


def test_preexisting_bounty_target_has_no_new_controls_or_undisclosed_penalty(monkeypatch):
    world, pirate = _world_with_pending_fight()
    world.save.pending_travel["encounter"]["pirate"] = vr.dataclasses.asdict(pirate)
    world.save.ship.hull_class = "Carrier"; world.save.ship.hull_hp = vr.hull_hp_max(world.save.ship)
    world.save.ship.weapon_tier = 4; world.save.ship.has_gunner = True
    monkeypatch.setattr(vr, "read_key", lambda: "F")
    monkeypatch.setattr(world.event_rng, "random", lambda: 0.0)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_travel(vr.Palette(False), world, world.save.pending_travel["destination"])
    assert world.save.pilot.notoriety == 0
    shown = plain(output.getvalue())
    assert "[V] Verify" not in shown and "[W] Withdraw" not in shown
    assert world.save.pilot.missions_completed == 1


@pytest.mark.parametrize("danger", range(6))
@pytest.mark.parametrize("ambush", [False, True])
def test_derelict_reward_and_opponent_match_destination_terms(monkeypatch, danger, ambush):
    world, _ = _world_with_pending_fight()
    travel = world.save.pending_travel; travel["primary"] = "random"; travel["bounty"] = None
    travel["encounter"] = {"kind": "derelict"}
    world.by_id[travel["destination"]].danger = danger
    world.here.danger = (danger + 2) % 5
    monkeypatch.setattr(vr, "read_key", lambda: "S")
    monkeypatch.setattr(world.event_rng, "random", lambda: 0.99 if ambush else 0.0)
    monkeypatch.setattr(world.event_rng, "randint", lambda low, high: high)
    pirates = []
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: pirates.append(pirate) or "escaped")
    before = world.save.pilot.credits
    text = " ".join(vr.derelict_terms(world))
    assert f"danger {danger}/5" in text and f"60-{100 + danger * 120}cr" in text
    with contextlib.redirect_stdout(io.StringIO()): vr._encounter_derelict(vr.Palette(False), world)
    if ambush: assert pirates[0].tier == min(4, danger + 1)
    else: assert world.save.pilot.credits == before + 100 + danger * 120


def _world_with_coordinated_squadron(*, engaged=False):
    world = _world_with_exploration_choice("pirate")
    pirates = [vr.Pirate("Hollow Fang", 4, 80, 80), vr.Pirate("Rust Wraith", 2, 50, 50)]
    world.save.pending_travel["encounter"] = {"kind": "pirate", "pirates": [vr.dataclasses.asdict(p) for p in pirates],
        "index": 0, "formation": {"version": 1, "engaged": engaged}, "combat": {
            "pirate": vr.dataclasses.asdict(pirates[0]), "outcome": None, "lines": [],
            "tactics": vr.new_tactics(pirates[0]), "hull_before": world.save.ship.hull_hp}}
    return world, pirates


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_squadron_terms_fit_and_show_cover_before_first_choice(monkeypatch, terminal, width, height):
    import copy, re
    world, pirates = _world_with_coordinated_squadron()
    terminal(width, height)
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    output = io.StringIO(); frames = []
    def choose():
        frame = output.getvalue(); output.seek(0); output.truncate(0); frames.append(frame)
        assert len(frame.splitlines()) <= height
        assert all(vr._visible_width(line) <= width for line in frame.splitlines())
        assert world.save.to_dict() == before and world.event_rng.getstate() == rng
        if len(frames) == 1:
            assert "+6" in frame and "Hollow Fang" in page_text(frame)
        page, count = map(int, re.search(r"Combat.*?(\d+)/(\d+)", frame, re.S).groups())
        if page == count: raise EOFError
        return ">"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output), pytest.raises(EOFError): vr.screen_combat(vr.Palette(False), world, pirates[0])
    text = page_text(frames)
    assert "[T] Target" in text and "Rust Wraith" in text and "both raiders" in text


def test_switching_squadron_target_preserves_resources_rng_and_round_state():
    import copy
    world, pirates = _world_with_coordinated_squadron()
    ship, pilot, rng = copy.deepcopy(world.save.ship), copy.deepcopy(world.save.pilot), world.event_rng.getstate()
    assert vr.squadron_cover(world) == 6
    vr.switch_squadron_target(world)
    state = world.save.pending_travel["encounter"]
    assert state["combat"]["pirate"]["name"] == pirates[1].name
    assert state["combat"]["tactics"] == vr.new_tactics(pirates[1])
    assert vr.squadron_cover(world) == 10
    assert world.save.ship == ship and world.save.pilot == pilot and world.event_rng.getstate() == rng
    vr.SaveData.from_dict(world.save.to_dict())
    vr.switch_squadron_target(world)
    assert state["combat"]["pirate"]["name"] == pirates[0].name


@pytest.mark.parametrize("braced", [False, True])
@pytest.mark.parametrize("intent", list(vr.TACTICAL_INTENTS))
def test_squadron_cover_matches_disclosed_combined_damage(monkeypatch, braced, intent):
    world, pirates = _world_with_coordinated_squadron(engaged=True)
    world.save.ship.shield_tier = 4
    tactics = next({"version": 1, "profile": p, "step": steps.index(intent), "brace_ready": True}
                  for p, steps in vr.TACTICAL_PROFILES.items() if intent in steps)
    for roll in (4, 9):
        world.save.ship.hull_hp = 400
        monkeypatch.setattr(world.event_rng, "randint", lambda low, high: roll)
        expected = vr._tactical_incoming_damage(world.save.ship, pirates[0].tier, intent, roll, tactics, braced=braced, cover=6)
        base = vr._tactical_incoming_damage(world.save.ship, pirates[0].tier, intent, roll, tactics)
        assert expected == ((base + 6 + 3) // 4 if braced else base + 6)
        actual, _ = vr.tactical_retaliation(world, pirates[0], dict(tactics), braced=braced)
        assert actual == expected and world.save.ship.hull_hp == 400 - actual


def test_squadron_cover_ends_after_the_first_raider():
    world, _ = _world_with_coordinated_squadron(engaged=True)
    state = world.save.pending_travel["encounter"]
    assert vr.squadron_cover(world) == 6
    state["index"] = 1
    assert vr.squadron_cover(world) == 0 and "covering fire has ended" in " ".join(vr.squadron_terms(world))


def test_a_two_raider_encounter_without_its_formation_is_refused():
    """Sequential pairs were a retired schema's shape (issue #421)."""
    world, _ = _world_with_coordinated_squadron()
    world.save.pending_travel["encounter"].pop("formation")
    with pytest.raises(vr.ResumeError): vr.World(vr.SaveData.from_dict(world.save.to_dict()))


@pytest.mark.parametrize("fault", ["version", "bool_version", "engaged", "extra", "null", "position", "kind", "target", "damage", "step"])
def test_invalid_squadron_formation_preserves_original_save(tmp_path, fault):
    import json
    world, _ = _world_with_coordinated_squadron()
    state = world.save.pending_travel["encounter"]; formation = state["formation"]
    if fault == "version": formation["version"] = 2
    elif fault == "bool_version": formation["version"] = True
    elif fault == "engaged": formation["engaged"] = 1
    elif fault == "extra": formation["extra"] = True
    elif fault == "null": state["formation"] = None
    elif fault == "position": state["index"] = 1
    elif fault == "kind": state["kind"] = "derelict"
    elif fault == "target": state["combat"]["pirate"]["name"] = "Wrong target"
    elif fault == "damage": state["combat"]["pirate"]["hp"] -= 1
    else: state["combat"]["tactics"]["step"] = 1
    path = tmp_path / "77.json"; path.write_text(json.dumps(world.save.to_dict()), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(vr.ResumeError): vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == before


def test_a_version_1_squadron_checkpoint_still_resumes_and_keeps_its_ruleset(tmp_path):
    """A fight checkpointed under the old curve must not become unloadable."""
    import json
    world, pirates = _world_with_coordinated_squadron()
    combat = world.save.pending_travel["encounter"]["combat"]
    combat["tactics"] = vr.new_tactics(pirates[0], 1)
    world.save.event_rng_state = world.event_rng.getstate()
    (tmp_path / "77.json").write_text(json.dumps(world.save.to_dict()), encoding="utf-8")
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert saved.pending_travel["encounter"]["combat"]["tactics"]["version"] == 1
    restored = vr.World(saved)
    assert vr.switch_squadron_target(restored)  # swapping target inside the fight keeps the old curve
    assert restored.save.pending_travel["encounter"]["combat"]["tactics"]["version"] == 1


def test_target_selection_closes_after_engagement_without_effects(monkeypatch):
    import copy
    world, pirates = _world_with_coordinated_squadron()
    keys = iter(["F", "T"]); before = None
    def choose():
        nonlocal before
        if world.save.pending_travel["encounter"]["formation"]["engaged"]:
            if before is None: before = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
            assert (world.save.to_dict(), world.event_rng.getstate()) == before
        try: return next(keys)
        except StopIteration: raise EOFError
    monkeypatch.setattr(vr, "read_key", choose)
    world.save.ship.hull_class = "Carrier"
    world.save.ship.hull_hp = vr.hull_hp_max(world.save.ship)
    with contextlib.redirect_stdout(io.StringIO()), pytest.raises(EOFError): vr.screen_combat(vr.Palette(False), world, pirates[0])
    with pytest.raises(ValueError): vr.switch_squadron_target(world)


def test_target_switch_survives_real_disconnect_before_acknowledgement(tmp_path):
    world, pirates = _world_with_coordinated_squadron(); vr.persist(world, tmp_path, 77)
    rng = world.event_rng.getstate()
    with _door_stopped_at(tmp_path, b"T", b"Target selected"):
        saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        state = saved.pending_travel["encounter"]
        assert state["pirates"][0]["name"] == pirates[1].name
        assert state["combat"]["pirate"]["name"] == pirates[1].name
        assert not state["formation"]["engaged"]
        assert vr.World(saved).event_rng.getstate() == rng


@pytest.mark.parametrize("lead,prefer_switch", [("Hollow Fang", False), ("Grimwire", True)])
def test_squadron_target_order_has_profile_dependent_tradeoffs(monkeypatch, lead, prefer_switch):
    damage = []
    for switch in (False, True):
        total = 0
        for seed in range(32):
            world, _ = _world_with_coordinated_squadron(); world.event_rng.seed(seed)
            state = world.save.pending_travel["encounter"]
            state["pirates"][0]["name"] = lead
            state["combat"]["pirate"] = dict(state["pirates"][0])
            state["combat"]["tactics"] = vr.new_tactics(vr.Pirate(**state["pirates"][0]))
            world.save.ship.hull_class = "Carrier"; world.save.ship.hull_hp = 400
            world.save.ship.weapon_tier = 3; world.save.ship.shield_tier = 3
            keys = iter(["T"] if switch else [])
            monkeypatch.setattr(vr, "read_key", lambda: next(keys, "F"))
            with contextlib.redirect_stdout(io.StringIO()):
                vr._resolve_random_travel_encounter(vr.Palette(False), world, world.by_id[world.save.pending_travel["destination"]])
            assert world.save.pilot.kills == 2
            total += 400 - world.save.ship.hull_hp
        damage.append(total)
    assert (damage[1] < damage[0]) == prefer_switch


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_review_combat_info_keeps_exchange_before_tactical_heading(monkeypatch, terminal, width, height):
    world = _world_with_seed(42); pirate = vr.Pirate("Raider", 0, 50, 50)
    terminal(width, height)
    lines = vr.combat_display_lines(world, pirate, ["Your last shot hit."], patrol=False, details=True, tactics=vr.new_tactics(pirate))
    pages = vr._service_pages(lines, "Combat 1,200cr", "[F/E/D/P] Act [I] Info [< >]Page: ")
    assert plainly(pages[0][0]) == "LAST EXCHANGE"
    kinds = [plainly(line) for line in lines]
    assert kinds.index("Your last shot hit.") < kinds.index("TACTICAL SYSTEMS")


@pytest.mark.parametrize("tier", [0, 4])
@pytest.mark.parametrize("cargo", [0, 1, 12, 24])
def test_review_combat_dump_terms_disclose_actual_escape_probability(tier, cargo):
    import copy
    world = _world_with_seed(42); _set_cargo(world, {"food": cargo})
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    pirate = vr.Pirate("Raider", tier, 80, 80)
    tactics = vr.new_tactics(pirate)
    line = next((row for row in vr.combat_display_lines(world, pirate, [], patrol=False, tactics=tactics) if row.startswith("[D]")), None)
    after = copy.deepcopy(world)
    if not cargo:
        assert line is None  # an empty hold has nothing to dump, so the action is not offered (#414)
        assert world.save.to_dict() == before and world.event_rng.getstate() == rng
        return
    vr._dispose_cargo(after, "food", 1)
    assert f"{vr.combat_evade_chance(after, pirate, dumped_cargo=True, tactics=tactics):.0%}" in line
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng
    assert "one unit" in line


@pytest.mark.parametrize("hull", [20, 60])
def test_review_combat_low_hull_warning_does_not_invent_one_hit_risk(hull):
    world = _world_with_seed(42); world.save.ship.hull_hp = hull
    weak = vr.Pirate("Weak raider", 0, 20, 20)
    lines = " ".join(vr.combat_display_lines(world, weak, [], patrol=False, tactics=vr.new_tactics(weak)))
    assert "another hit may destroy" not in lines
    assert ("LOW HULL" in lines) == (hull <= 20)


@pytest.mark.parametrize("patrol", [False, True])
def test_review_combat_kill_terms_name_both_factions_and_notoriety(patrol):
    world = _world_with_seed(42); pirate = vr.Pirate("Opponent", 2, 50, 50)
    lines = " ".join(vr.combat_display_lines(world, pirate, [], patrol=patrol, details=True, tactics=vr.new_tactics(pirate)))
    assert ("Concord -10" if patrol else "Concord +2") in lines
    assert ("Blackwake +3" if patrol else "Blackwake -1") in lines
    if patrol: assert "notoriety +3" in lines


@pytest.mark.parametrize("cargo", [0, 1])
def test_tactical_dump_terms_include_harry_escape_penalty(cargo):
    import copy
    world = _world_with_seed(42); _set_cargo(world, {"food": cargo})
    pirate = vr.Pirate("Rust Wraith", 2, 50, 50)
    tactics = {"version": 1, "profile": "Skirmisher", "step": 0, "brace_ready": True}
    line = next((row for row in vr.combat_display_lines(world, pirate, [], patrol=False, tactics=tactics) if row.startswith("[D]")), None)
    if not cargo:
        assert line is None  # no cargo, no Dump (#414)
        return
    after = copy.deepcopy(world)
    vr._dispose_cargo(after, "food", 1)
    expected = vr.combat_evade_chance(after, pirate, dumped_cargo=True, tactics=tactics)
    assert expected < vr.evade_chance(after, pirate, dumped_cargo=True)
    assert f"{expected:.0%}" in line


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
@pytest.mark.parametrize("details", [False, True])
def test_real_exchange_text_starts_on_first_combat_page(monkeypatch, terminal, width, height, details):
    world = _world_with_seed(42); pirate = vr.Pirate("Rust Wraith", 1, 80, 80)
    _, _, result = vr.tactical_round(world, pirate, vr.new_tactics(pirate), "F")
    terminal(width, height)
    lines = vr.combat_display_lines(world, pirate, result, patrol=False, details=details, tactics=vr.new_tactics(pirate))
    pages = vr._service_pages(lines, "Combat 1,200cr", "[F/E/D/P] Act [I] Info [< >]Page: ")
    assert len(pages[0]) > 1 and result[0].split()[0] in " ".join(pages[0][1:])
    text = " ".join(" ".join(plainly(row) for page in pages for row in page).split())
    for entry in result: assert " ".join(entry.split()) in text


@pytest.mark.parametrize("patrol", [False, True])
def test_peaceful_combat_action_discloses_its_standing_gain(patrol):
    world = _world_with_seed(42); pirate = vr.Pirate("Opponent", 1, 50, 50)
    lines = vr.combat_display_lines(world, pirate, [], patrol=patrol, details=True, tactics=vr.new_tactics(pirate))
    action = next(row for row in lines if row.startswith("[S]" if patrol else "[P]"))
    assert ("Concord +2" if patrol else "Blackwake +2") in action


def test_archive_and_landmark_reject_inflight_actions_before_effects():
    import copy
    world, _ = _world_with_pending_fight(); world.save.current_system = world.landmark["system_id"]
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    for action in ("A", "I", "P", "S"):
        with pytest.raises(ValueError, match="journey"): vr.archive_action(world, action)
    with pytest.raises(ValueError, match="journey"): vr.investigate_landmark(world)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


@pytest.mark.parametrize("identity", ["legacy", "unverified-match", "unverified-mismatch", "confirmed", "mismatch"])
@pytest.mark.parametrize("details", [False, True])
def test_bounty_combat_risk_terms_match_identification_state_without_rng(identity, details):
    import copy
    world = _world_with_seed(42)
    world.save.pending_travel = {"phase": "primary", "primary": "bounty", "encounter": {}}
    warrant = None if identity == "legacy" else {"version": 1, "matches": identity in ("unverified-match", "confirmed"),
        "checked": identity in ("confirmed", "mismatch"), "engaged": False}
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    pirate = vr.Pirate("Opponent", 2, 50, 50)
    text = " ".join(vr.combat_display_lines(world, pirate, [], patrol=False, details=details, warrant=warrant, tactics=vr.new_tactics(pirate)))
    assert "After a bounty victory" not in text  # Old random inquiry no longer applies.
    if identity.startswith("unverified"):
        assert "12%" in text and "Concord -3" in text and "+2 notoriety" in text
    elif identity == "mismatch":
        assert "Identity mismatch confirmed" in text and "Concord -3" in text and "+2 notoriety" in text
        assert "12%" not in text
    elif identity == "confirmed":
        assert "no mistaken-identity penalty" in text and "Concord -3" not in text
    else:
        assert "Concord -3" not in text and "12%" not in text
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


def test_personal_gunner_task_counts_actual_combat_once_after_reload(monkeypatch, tmp_path):
    world, pirate = _world_with_pending_fight()
    pending = world.save.pending_travel; world.save.pending_travel = None
    world.save.pilot.credits = 100000
    vr.hire_crew(world, "gunner"); world.save.ship.crew_records["gunner"]["paid_jumps"] = 5
    vr.accept_crew_assignment(world, "gunner"); world.save.pending_travel = pending
    world.save.ship.weapon_tier = 4; world.save.ship.shield_tier = 3
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    monkeypatch.setattr(vr, "read_key", lambda: "F")
    with contextlib.redirect_stdout(io.StringIO()): assert vr.screen_combat(vr.Palette(False), world, pirate) == "won"
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    resumed = vr.World(saved); before = saved.pilot.kills
    assert before == 1 and vr.crew_assignment_progress(resumed, "gunner") == (1, 1)
    with contextlib.redirect_stdout(io.StringIO()): assert vr.screen_combat(vr.Palette(False), resumed, pirate) == "won"
    assert saved.pilot.kills == before and vr.crew_assignment_progress(resumed, "gunner") == (1, 1)


@pytest.mark.parametrize("standing", [-53, -52, -51, -50, -49])
def test_faction_commission_uses_actual_post_combat_standing_at_bounty_payout(monkeypatch, tmp_path, standing):
    world, pirate = _world_with_pending_fight()
    world.save.pilot.has_concord_commission = True; world.save.pilot.reputation[vr.FACTION_CONCORD] = standing
    pirate.hp = 1
    state = world.save.pending_travel["encounter"]
    state["pirate"] = vr.dataclasses.asdict(pirate); state["combat"]["pirate"] = vr.dataclasses.asdict(pirate)
    before = world.save.pilot.credits; destination = world.save.pending_travel["destination"]
    terms = " ".join(vr.mission_details(world, world.save.active_missions[0]))
    assert "above -50 when paid" in terms
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    monkeypatch.setattr(vr, "read_key", lambda: "F")
    with contextlib.redirect_stdout(io.StringIO()): vr.screen_travel(vr.Palette(False), world, destination)
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert saved.pilot.reputation[vr.FACTION_CONCORD] == standing + 2
    assert saved.pilot.credits == before + 160 + (625 if standing + 2 > -50 else 500)
    assert saved.pilot.missions_completed == 1 and saved.pending_travel is None


def test_cancelling_returns_reserved_stock_bounded_by_the_station_ceiling():
    world = _world_at_food_producer()
    cap = vr.market_depth_limits(world.here.economy, "food")["stock"]
    vr.buy_futures_contract(world, "food", 24, 5)
    vr.buy_futures_contract(world, "food", 24, 5)
    first, second = world.save.active_futures
    assert vr.market_depth_quote(world, world.here.id, "food")["stock"] == cap - 48
    message = vr.cancel_futures_contract(world, first.id)
    assert "24 units returned" in message
    assert vr.market_depth_quote(world, world.here.id, "food")["stock"] == cap - 24
    world.save.turn += 40  # pool fully replenished meanwhile
    world.save.current_system = world.here.connections[0]  # remote cancellation
    vr.cancel_futures_contract(world, second.id)
    origin = first.origin_system
    assert vr.market_depth_quote(world, origin, "food")["stock"] == cap


def test_settlement_does_not_consume_stock_a_second_time():
    world = _world_at_food_producer()
    cap = vr.market_depth_limits(world.here.economy, "food")["stock"]
    vr.buy_futures_contract(world, "food", 10, 5)
    world.save.turn = 5
    assert vr.settle_futures_contracts(world) and world.save.cargo == {"food": 10}
    assert vr.market_depth_quote(world, world.here.id, "food")["stock"] == min(cap, cap - 10 + 5 * vr.market_depth_limits(world.here.economy, "food")["stock_rate"])


def test_a_cancelled_order_returns_exactly_the_stock_it_reserved():
    world = _world_at_food_producer()
    cap = vr.market_depth_limits(world.here.economy, "food")["stock"]
    ready = vr.FuturesContract(id=1, commodity="food", quantity=4, locked_price=100, settle_turn=0,
                               origin_system=world.here.id, principal=90, reserved=4)
    later = vr.FuturesContract(id=2, commodity="food", quantity=4, locked_price=100, settle_turn=9,
                               origin_system=world.here.id, principal=90, reserved=4)
    world.save.active_futures = [ready, later]
    world.save.next_futures_id = 3
    vr._consume_market_depth(world, "food", 8, buying=True)
    assert vr.settle_futures_contracts(world) == ["Futures contract settled: 4x Food delivered to your hold."]
    vr.cancel_futures_contract(world, 2)
    assert vr.market_depth_quote(world, world.here.id, "food")["stock"] == cap - 4
    assert not world.save.active_futures


@pytest.mark.parametrize("reserved", [-1, 25, "24", True])
def test_corrupt_futures_reservation_uses_preserving_recovery(reserved):
    world = _world_at_food_producer()
    vr.buy_futures_contract(world, "food", 24, 5)
    data = world.save.to_dict()
    data["active_futures"][0]["reserved"] = reserved
    with pytest.raises(vr.ResumeError):
        vr.SaveData.from_dict(data)


def test_a_reservation_larger_than_the_order_is_rejected():
    world = _world_at_food_producer()
    world.save.active_futures = [vr.FuturesContract(id=1, commodity="food", quantity=4, locked_price=100,
                                                    settle_turn=0, origin_system=world.here.id,
                                                    principal=90, reserved=4)]
    data = world.save.to_dict()
    data["active_futures"][0]["reserved"] = 5
    with pytest.raises(vr.ResumeError):
        vr.SaveData.from_dict(data)


def test_order_screens_show_stock_after_reservation(monkeypatch):
    world = _world_at_food_producer()
    cap = vr.market_depth_limits(world.here.economy, "food")["stock"]
    keys = iter(["U", "B"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(vr, "read_line_raw", lambda **kw: "10")
    with contextlib.redirect_stdout(io.StringIO()) as output:
        assert vr._screen_buy_futures(vr.Palette(False), world, "food") is None
    plain = page_text(output.getvalue())
    assert f"Station stock {cap}" in plain and f"{cap - 10} left after this order" in plain
    vr.buy_futures_contract(world, "food", 10, 5)
    keys = iter(["B"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr._screen_futures_order(vr.Palette(False), world, world.save.active_futures[0])
    assert "10 units reserved from that station's stock; cancelling returns them." in " ".join(plainly(output.getvalue()).split())


@pytest.mark.parametrize("hull_class", list(vr.HULL_REFITS))
@pytest.mark.parametrize("hull_tier", [0, vr.UPGRADES["hull"]["max_tier"]])
def test_salvage_fee_exceeds_a_full_repair_for_every_hull(hull_class, hull_tier):
    world = _world_with_seed(42)
    world.save.ship.hull_class, world.save.ship.hull_tier = hull_class, hull_tier
    world.save.ship.hull_hp = 1
    full_repair = (vr.hull_hp_max(world.save.ship) - 1) * 4
    assert vr.salvage_fee(world.save.ship) > full_repair


def test_raider_destruction_keeps_notoriety_and_patrol_destruction_clears_it():
    world = _world_with_seed(42); world.save.pilot.notoriety = 9
    world.save.pilot.credits = 100_000
    assert "Notoriety" not in vr.destroy_ship(world) and world.save.pilot.notoriety == 9
    world.save.pilot.notoriety = 9
    assert "Notoriety cleared" in vr.destroy_ship(world, patrol=True) and world.save.pilot.notoriety == 0


def test_combat_session_passes_the_patrol_flag_to_destruction(monkeypatch):
    for patrol, expected in ((False, 5), (True, 0)):
        world, pirate = _world_with_pending_fight()
        world.save.pilot.notoriety = 5; world.save.ship.hull_hp = 1
        world.save.pilot.credits = 100_000
        monkeypatch.setattr(vr, "read_key", lambda: "F")
        monkeypatch.setattr(vr, "tactical_round", lambda w, p, t, a: (0, 0, ["they fire"]) if not setattr(w.save.ship, "hull_hp", 0) else None)
        with contextlib.redirect_stdout(io.StringIO()):
            assert vr._screen_combat_session(vr.Palette(False), world, pirate, patrol=patrol) == "destroyed"
        assert world.save.pilot.notoriety == expected


@pytest.mark.parametrize("style", ["auto", "plain"])
@pytest.mark.parametrize("handle", ["Thiesi", "船長", "Ægir Ól"])
@pytest.mark.parametrize("width", [80, 40, 20])
def test_title_box_rows_share_one_display_width_at_every_width(monkeypatch, width, handle, style):
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width); monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_title(vr.Palette(False), {"node_name": "ReLink", "handle": handle})
    rows = _box_rows(output.getvalue())
    assert rows and {vr._visible_width(row) for row in rows} == {vr._box_outer_width()}
    plain = " ".join(" ".join(rows).split())
    assert ("V O I D R U N N E R" if width >= 21 else "VOIDRUNNER") in plain
    # The splash's meta fields are chips now (issue #493), so the colon went
    # with the label; a starfield strip opens the large composition.
    assert "PILOT" in plain and handle in plain and "NODE ReLink" in plain
    if width == 80:
        assert len(rows) == 11 and "Tactical Deep-Space Trading & Exploration" in plain and "48 Star Systems" in plain
    if width < 51:
        assert "█" not in plain and "#" not in plain  # no partial logo; the compact composition is complete


def test_title_large_composition_is_unchanged_at_eighty_columns(monkeypatch):
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    rows = vr.title_rows({"node_name": "Central BBS", "handle": "Alice"}, vr._box_inner_width())
    assert [kind for kind, _, _ in rows] == ["stars", "logo", "logo", "wordmark", "blank", "sub", "blank", "rule", "meta"]
    assert rows[-1][1] == "  NODE: Central BBS  │  PILOT: Alice  │  GALAXY: 48 Star Systems"


@pytest.mark.parametrize("handle", ["SixteenCharHandl", "船長"])
@pytest.mark.parametrize("width", [80, 40, 20])
def test_registration_box_fits_its_width_and_keeps_the_handle(monkeypatch, width, handle):
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
    monkeypatch.setattr(vr, "read_line_raw", lambda max_len=16, allowed=None: "")
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: False)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        assert vr.create_career(vr.Palette(False), {"handle": handle}) is None
    rows = _box_rows(output.getvalue())
    assert rows and {vr._visible_width(row) for row in rows} == {vr._box_outer_width()}
    assert handle in " ".join(" ".join(rows).split())


def test_market_rows_fit_eighty_columns_without_filler_status():
    world = _world_with_seed(42)
    goods = vr.LEGAL_COMMODITIES + vr.CONTRABAND_COMMODITIES
    world.save.current_system = next(s.id for s in world.galaxy if s.economy == "Haven")
    lines = vr.market_catalog_lines(world, goods)
    # The catalogue's own rows fit without wrapping; the notes below it are
    # prose, and prose wraps.
    assert all(vr._visible_width(line) <= 79 for line in lines if plainly(line).startswith("["))
    catalog = plainly(" ".join(lines))
    assert " Normal" not in catalog and "Illegal" in catalog
    assert "Only jumps advance days" in catalog


def test_contract_notes_appear_only_when_they_apply():
    world = _world_with_seed(42)
    dest = sorted(world.here.connections)[0]; world.by_id[dest].discovered = True
    delivery = vr.Mission(1, "delivery", "Deliver", 300, 0, dest, commodity="food", quantity=2)
    text = " ".join(vr.mission_details(world, delivery))
    assert "not total profit" in text and "cargo already aboard" not in text
    assert "crew wages" not in text and "Survey scanning" not in text and "Remote danger" not in text
    _set_cargo(world, {"food": 1})
    assert "as is cargo already aboard" in " ".join(vr.mission_details(world, delivery))
    world.save.ship.has_gunner = True
    assert "Budget keeps current crew wages" in " ".join(vr.mission_details(world, delivery))
    far = next(s.id for s in world.galaxy if not s.discovered and s.id != dest)
    scan = vr.Mission(2, "scan", "Survey", 250, 0, far)
    text = " ".join(vr.mission_details(world, scan))
    assert "Survey scanning may avoid travel." in text and "Remote danger remains unknown until charted." in text


def test_offer_page_one_points_to_accept_without_offering_it(monkeypatch, terminal):
    terminal(40, 12)
    world, mission = _mission_details_world()
    frames = []; output = io.StringIO()
    def choose():
        frames.append(output.getvalue()); output.seek(0); output.truncate(0)
        return "A" if len(frames) == 1 else "B"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output):
        vr.screen_mission_details(vr.Palette(False), world, mission, active=False)
    first = page_text(frames[0])
    assert "[A] on last page." in first and "[A] Accept" not in first
    assert not world.save.active_missions  # A on page 1 did not accept


def test_escape_answers_no_at_a_confirmation_and_ignores_unsupported_keys(monkeypatch):
    keys = iter([vr.IGNORED_KEY, "\x1b[A", vr.ESCAPE_KEY])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        assert vr.confirm("Repair 57 hull for 228cr?", vr.Palette(False)) is False
    assert "[Y/N, Esc=No]" in output.getvalue() and output.getvalue().rstrip().endswith("N")
    keys = iter(["y"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.confirm("Sure?", vr.Palette(False)) is True
