"""The storage layer: round-tripping, validation, locks, checkpoints and
preserving recovery.

Split out of `test_voidrunner_domain.py` (issue #422).
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

import pytest

from .support import _VOIDRUNNER_PATH, _door_stopped_at, _set_cargo, _world_with_seed, vr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows byte-range lock initialization")
def test_empty_windows_lease_reports_busy_without_writing_before_lock(tmp_path):
    import msvcrt
    import subprocess

    path = tmp_path / "empty.lock"
    path.write_bytes(b"")
    script = """
import runpy, sys
from pathlib import Path
vr = runpy.run_path(sys.argv[1])
try:
    with vr['_file_lease'](Path(sys.argv[2])):
        print('acquired')
except vr['PilotBusy']:
    print('busy')
"""
    args = [sys.executable, "-c", script, str(_VOIDRUNNER_PATH), str(path)]
    with path.open("r+b") as owner:
        msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
        blocked = subprocess.run(args, capture_output=True, timeout=5)
        assert blocked.returncode == 0 and not blocked.stderr
        assert blocked.stdout.strip() == b"busy"
    assert path.read_bytes() == b""
    acquired = subprocess.run(args, capture_output=True, timeout=5)
    assert acquired.returncode == 0 and not acquired.stderr
    assert acquired.stdout.strip() == b"acquired" and path.read_bytes() == b""


def test_save_data_round_trips_through_dict_including_missions_and_none_fields():
    save = vr._new_career("Roundtrip")
    save.active_missions.append(vr.Mission(
        id=1, kind="scan", description="survey it", reward=250,
        origin_system=0, target_system=5, deadline_turn=None,
    ))
    save.market_drift[3] = {"food": 1.2}
    restored = vr.SaveData.from_dict(save.to_dict())
    assert restored.pilot.handle == "Roundtrip"
    assert restored.active_missions[0].deadline_turn is None
    assert restored.active_missions[0].target_system == 5
    assert restored.market_drift[3]["food"] == 1.2


def test_write_and_load_save_round_trips_on_disk(tmp_path):
    save = vr._new_career("Disky")
    save.pilot.credits = 4321
    vr.write_save(tmp_path, user_id=77, save=save)

    loaded, is_new, notice = vr.load_or_create_save(tmp_path, user_id=77, handle="Disky")
    assert is_new is False
    assert notice is None
    assert loaded.pilot.credits == 4321
    assert loaded.seed == save.seed


def test_loading_an_existing_save_never_overwrites_the_chosen_callsign(tmp_path):
    """Dogfood-caught: a live login handle is only ever the *default*
    callsign at character creation -- once a save exists, the pilot's
    own chosen callsign must survive regardless of what the current
    login handle says, including when it's unchanged, changed, or a
    totally different account (a save is keyed by stable user_id, never
    handle -- see the module's own docstring). A prior version
    unconditionally wrote the login handle over the saved callsign on
    every single load, silently discarding it."""
    save = vr._new_career("Claude")
    save.pilot.handle = "Voyager1"  # the player's own chosen callsign
    vr.write_save(tmp_path, user_id=99, save=save)

    loaded, is_new, notice = vr.load_or_create_save(tmp_path, user_id=99, handle="Claude")

    assert is_new is False
    assert loaded.pilot.handle == "Voyager1"


def test_corrupt_save_is_preserved_in_place_without_automatic_reset(tmp_path):
    path = tmp_path / "5.json"
    path.write_text("not valid json{{{", encoding="utf-8")
    with pytest.raises(vr.ResumeError):
        vr.load_or_create_save(tmp_path, user_id=5, handle="Recovered")
    assert path.read_text(encoding="utf-8") == "not valid json{{{"
    assert not list(tmp_path.glob("5.corrupt-*"))


def test_write_save_is_atomic_no_tmp_file_left_behind(tmp_path):
    save = vr._new_career("Atomic")
    vr.write_save(tmp_path, user_id=9, save=save)
    assert (tmp_path / "9.json").exists()
    assert not (tmp_path / "9.json.tmp").exists()


def test_failed_station_checkpoint_never_announces_purchase(monkeypatch):
    world = _world_with_seed(42)

    def fail_save(current):
        raise OSError("disk full")

    world._checkpoint = fail_save
    monkeypatch.setattr(vr, "read_key", lambda: "P")
    monkeypatch.setattr(vr, "read_line_raw", lambda **kwargs: "1")
    output = io.StringIO()
    with contextlib.redirect_stdout(output), pytest.raises(vr.SaveError):
        vr._trade_commodity(vr.Palette(False), world, "food")
    assert "Bought" not in output.getvalue()


def test_save_failure_stops_main_without_success_or_more_actions(tmp_path, monkeypatch):
    world = _world_with_seed(42)
    original_persist = vr.persist
    attempts = []

    def fail_second_save(current, directory, user_id):
        attempts.append(current.save.to_dict())
        if len(attempts) == 2:
            raise OSError("disk full")
        original_persist(current, directory, user_id)

    class Terminal(io.StringIO):
        def reconfigure(self, **kwargs):
            pass

    output = Terminal()
    keys = iter(["M", "A", "P", " ", "Q"])
    with monkeypatch.context() as patch:
        patch.setattr(vr.sys, "stdout", output)
        patch.setattr(vr, "_load_door_info", lambda: {"handle": "Tester", "user_id": 77})
        patch.setattr(vr, "_default_save_dir", lambda: tmp_path)
        patch.setattr(vr, "load_or_create_save", lambda *args: (world.save, False, None))
        patch.setattr(vr, "persist", fail_second_save)
        patch.setattr(vr, "read_key", lambda: next(keys))
        patch.setattr(vr, "read_line_raw", lambda **kwargs: "1")
        assert vr.main() == 1
    assert "Save failed" in output.getvalue()
    assert "Bought" not in output.getvalue()
    assert next(keys) == "Q"  # only the error acknowledgement was consumed
    assert len(attempts) == 2  # no EOF retry writes an unacknowledged action
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert saved.cargo == {}
    assert saved.pilot.credits == 1200


def test_cancelled_career_does_not_create_save(tmp_path):
    import os
    import subprocess

    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path))
    env.pop("NETBBS_DOOR_INFO", None)
    result = subprocess.run(
        [sys.executable, str(_VOIDRUNNER_PATH)], input=b"NewPilot\rN",
        capture_output=True, env=env, timeout=10,
    )
    assert result.returncode == 0
    assert b"Career launch cancelled" in result.stdout
    assert not list(tmp_path.glob("*.json"))


def test_failed_atomic_replace_preserves_previous_save_and_removes_own_temp(tmp_path, monkeypatch):
    import os

    save = vr._new_career("Tester")
    vr.write_save(tmp_path, 77, save)
    previous = (tmp_path / "77.json").read_bytes()
    save.pilot.credits += 100

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        vr.write_save(tmp_path, 77, save)
    assert (tmp_path / "77.json").read_bytes() == previous
    assert not list(tmp_path.glob("*.tmp"))


def _schema_one_career(tmp_path, user_id: int = 77) -> bytes:
    """Write a career this build no longer opens, and return its exact bytes."""
    import json

    data = dict(_world_with_seed(42).save.to_dict(), schema_version=1)
    raw = json.dumps(data).encode("utf-8")
    (tmp_path / f"{user_id}.json").write_bytes(raw)
    return raw


def test_a_real_launch_refuses_an_old_career_and_changes_nothing(tmp_path):
    """The refusal is a screen with an offer, not an error (issue #421)."""
    original = _schema_one_career(tmp_path)
    with _door_stopped_at(tmp_path, b"B", b"[N] New career") as output:
        assert b"Career refused" in output and b"saved before this version" in output
    assert (tmp_path / "77.json").read_bytes() == original
    assert not list(tmp_path.glob("77.recovery-*.json"))


def test_a_real_launch_replaces_a_refused_career_only_after_registration(tmp_path):
    original = _schema_one_career(tmp_path)
    # Page to the offer, take it, accept the default callsign, confirm the launch.
    with _door_stopped_at(tmp_path, [b">>>>>>>>", b"N", b"\rY"], b"Station Services",
                          ready=b"Career refused"):
        pass
    archives = list(tmp_path.glob("77.recovery-*.json"))
    assert len(archives) == 1 and archives[0].read_bytes() == original
    replacement, is_new, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert not is_new and replacement.schema_version == vr.SCHEMA_VERSION and replacement.turn == 0


@pytest.mark.parametrize("broken", ["future_version", "missing_fields", "rng"])
def test_unreadable_resume_state_stops_without_replacing_career(tmp_path, broken):
    import json
    import os
    import subprocess

    world = _world_with_seed(42)
    data = world.save.to_dict()
    if broken == "rng":
        data["event_rng_state"] = [3, [1, 2], None]
    else:
        data["pending_travel"] = {"version": 999 if broken == "future_version" else 1}
    path = tmp_path / "77.json"
    original = json.dumps(data).encode("utf-8")
    path.write_bytes(original)
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_VOIDRUNNER_PATH)], input=b" ", capture_output=True,
        env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)),
        timeout=10,
    )
    assert result.returncode == 1
    assert "your saved career is unchanged" in " ".join(
        vr._ANSI_RE.sub("", result.stdout.decode("utf-8")).split()
    )
    assert not result.stderr
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_a_schema_one_career_is_refused_by_name_and_offered_a_replacement(tmp_path):
    """No migration: the refusal is the product decision, not a failure (#421)."""
    import json

    data = _world_with_seed(42).save.to_dict()
    data["schema_version"] = 1
    data.pop("contraband_standing_step")      # and a shape this build no longer knows
    path = tmp_path / "77.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(vr.OutdatedSave, match="saved before this version"):
        vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == before


def test_replacing_a_refused_career_retains_it_as_a_recovery_copy(tmp_path):
    import json

    old = json.dumps(dict(_world_with_seed(42).save.to_dict(), schema_version=1)).encode()
    (tmp_path / "77.json").write_bytes(old)
    fresh = vr._new_career("Tester")

    vr.replace_unsupported_career(tmp_path, 77, fresh)

    archives = list(tmp_path.glob("77.recovery-*.json"))
    assert len(archives) == 1 and archives[0].read_bytes() == old
    resumed, is_new, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert not is_new and resumed.pilot.handle == "Tester" and resumed.turn == 0


def test_a_docked_career_gains_its_resume_fields_without_regenerating_the_galaxy():
    data = _world_with_seed(42).save.to_dict()
    data.pop("pending_travel")   # both are optional while docked
    data.pop("event_rng_state")
    original = vr.generate_galaxy(data["seed"])
    world = vr.World(vr.SaveData.from_dict(data))
    assert world.save.pending_travel is None
    assert world.galaxy == original
    world.checkpoint()
    restored = vr.World(vr.SaveData.from_dict(world.save.to_dict()))
    assert restored.event_rng.random() == world.event_rng.random()


@pytest.mark.parametrize("fault", [
    "missing_bounty", "missing_escort", "duplicate_escort", "won_with_hp",
    "alive_without_hp", "escaped_without_hp", "destroyed_flag", "wrong_position",
])
def test_inconsistent_resume_stops_before_rewriting_save(tmp_path, fault):
    import json
    import os
    import subprocess

    world = _world_with_seed(42)
    mission = vr.Mission(1, "bounty", "Test contract", 500, 0, 1, pirate_tier=2)
    world.save.active_missions = [mission]
    travel = {
        "version": 1, "origin": 0, "destination": 1, "was_discovered": True,
        "destroyed": False, "phase": "primary", "primary": "bounty",
        "bounty": mission.to_dict(), "escorts": [], "escort_index": 0,
        "encounter": {"combat": {
            "pirate": {"name": "Raider", "tier": 2, "hp": 0, "hp_max": 50},
            "outcome": "won", "lines": [],
            "tactics": vr.new_tactics(vr.Pirate("Raider", 2, 0, 50)),
            "hull_before": world.save.ship.hull_hp,
        }},
    }
    combat = travel["encounter"]["combat"]
    if fault == "missing_bounty":
        world.save.active_missions = []
    elif fault in ("missing_escort", "duplicate_escort"):
        mission.kind = "escort"
        travel["phase"] = "escorts"
        travel["primary"] = "random"
        travel["escorts"] = [mission.to_dict()]
        if fault == "missing_escort":
            world.save.active_missions = []
        else:
            travel["escorts"].append(mission.to_dict())
    elif fault == "won_with_hp":
        combat["pirate"]["hp"] = 10
    elif fault == "alive_without_hp":
        combat["outcome"] = None
    elif fault == "escaped_without_hp":
        combat["outcome"] = "escaped"
    elif fault == "destroyed_flag":
        combat["outcome"] = "destroyed"
        combat["pirate"]["hp"] = 10
    elif fault == "wrong_position":
        world.save.current_system = 1
    world.save.pending_travel = travel
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


@pytest.mark.parametrize("sequence", [
    b"\x1b[A", b"\x1b[1;5B", b"\x1bOF", b"\x1b[15~", b"\x1b[[A",
    b"\x1b[<0;25;10M", b"\x1b[MABC", b"\x1b(I", b"\x1bY",
    b"\x1b]0;YBUY\x07", b"\x1bPBUY\x1b\\", b"\x9b1;5C",
    b"\x1b[200~MYAY\r\nBUY\x1b[201~", b"\x1b" + "界".encode("utf-8"),
])
def test_input_decoder_consumes_whole_terminal_key(sequence):
    reader = vr._DoorInput(lambda timeout: stream.read(1))
    stream = io.BytesIO(sequence + b"Z")
    assert reader.read_key() == vr.IGNORED_KEY
    assert reader.read_key() == "Z"
    with pytest.raises(EOFError):
        reader.read_key()


@pytest.mark.parametrize("text", ["Jörg", "界", "e\u0301", "Û", "🚀"])
def test_input_decoder_preserves_fragmented_utf8(text):
    events = []
    for byte in text.encode("utf-8"):
        events.extend([bytes([byte]), None])
    events.append(b"")
    reader = vr._DoorInput(lambda timeout: events.pop(0))
    result = []
    while True:
        try:
            key = reader.read_key()
        except EOFError:
            break
        if key != vr.IGNORED_KEY:
            result.append(key)
    assert "".join(result) == text


def test_input_decoder_bounds_long_control_strings_and_paste():
    stream = io.BytesIO(b"\x1b[200~" + b"Y" * 5000 + b"\x1b[201~Z")
    reader = vr._DoorInput(lambda timeout: stream.read(1))
    for _ in range(21):
        key = reader.read_key()
        assert len(reader.sequence) <= 8
        if key == "Z":
            break
        assert key == vr.IGNORED_KEY
    else:
        pytest.fail("did not reach the deliberate command after pasted data")


def test_real_pipe_lone_escape_is_absorbed_without_blocking_the_next_key(tmp_path):
    """The Escape itself now costs nothing at an action bar (#416), so what a real
    pipe has to prove is that it does not swallow or delay the key after it.

    The Escape and the key that follows are written as separate flushes, because a
    contiguous `ESC X` is a control-string introducer, not a lone Escape; and the
    acknowledgement is the deck redraw that only an accepted `X` produces, not the
    letter itself, which the title screen already contains (issue #416 review).
    """
    world = _world_with_seed(42)
    vr.persist(world, tmp_path, 77)
    with _door_stopped_at(tmp_path, [b"\x1b", b"X"], b"[X] Compact", ready=b"[X] Expand"):
        saved, is_new, notice = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert not is_new and notice is None
    assert saved.turn == 0
    assert saved.pilot.credits == 1200


def test_pickup_arrival_and_delivery_reward_resume_exactly_once(monkeypatch):
    import copy
    world = _world_with_seed(42)
    dest = world.here.connections[0]
    world.save.active_futures = [vr.FuturesContract(1, "food", 2, 22, 1, origin_system=dest,
                                                   principal=20, reserved=2)]
    world.save.active_missions = [vr.Mission(1, "delivery", "Pickup delivery", 500, 0, dest, commodity="food", quantity=2)]
    snapshots = []
    world._checkpoint = lambda current: snapshots.append(copy.deepcopy(current.save.to_dict()))
    monkeypatch.setattr(vr, "_resolve_random_travel_encounter", lambda *args: None)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, dest)
    expected = world.save.to_dict()
    assert world.save.pilot.credits == 1700
    assert not world.save.active_futures and not world.save.active_missions
    for snapshot in snapshots:
        if snapshot["pending_travel"] is None:
            continue
        resumed = vr.World(vr.SaveData.from_dict(snapshot))
        with contextlib.redirect_stdout(io.StringIO()):
            vr.screen_travel(vr.Palette(False), resumed, dest)
        assert resumed.save.to_dict() == expected


def test_the_import_takes_the_exclusion_a_restore_takes(tmp_path, monkeypatch):
    """Not the bare gate (issue #421 review).

    `pilot_session` releases the maintenance gate as soon as it owns its own
    pilot lock and then plays on holding only that, so an import under the bare
    gate could replace `scores/USER_ID.json` while its owner was checkpointing
    it. `maintenance_session` probes every pilot lock and is the only exclusion
    that answers "is anyone aboard".
    """
    import json

    path = tmp_path / "leaderboard.json"
    path.write_text(json.dumps([{"user_id": 77, "handle": "Old", "best_credits": 9000}]), encoding="utf-8")
    taken, real = [], vr.maintenance_session

    @contextlib.contextmanager
    def watched(save_dir):
        taken.append(save_dir)
        with real(save_dir):
            yield

    monkeypatch.setattr(vr, "maintenance_session", watched)
    vr.import_hall_of_fame(tmp_path)
    assert taken == [tmp_path]
    assert vr.load_hall_of_fame(tmp_path)[0]["best_credits"] == 9000


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_a_malformed_save_version_is_corruption_not_an_old_career(tmp_path, version):
    """`True` and `1.0` compare equal to 1; neither is a schema-1 career (#421 review)."""
    import json

    data = dict(_world_with_seed(42).save.to_dict(), schema_version=version)
    path = tmp_path / "77.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(vr.ResumeError) as raised:
        vr.load_or_create_save(tmp_path, 77, "Tester")
    assert not isinstance(raised.value, vr.OutdatedSave)
    assert path.read_bytes() == json.dumps(data).encode("utf-8")


def test_a_replacement_that_cannot_be_written_reaches_the_caller_as_a_save_failure(tmp_path, monkeypatch):
    """`main` translates `SaveError` here and nothing else (issue #421 review)."""
    import json

    old = json.dumps(dict(_world_with_seed(42).save.to_dict(), schema_version=1)).encode()
    (tmp_path / "77.json").write_bytes(old)
    real = vr._write_bytes_atomic
    monkeypatch.setattr(vr, "_write_bytes_atomic", lambda path, data: (_ for _ in ()).throw(
        OSError("disk gone")) if path.name == "77.json" else real(path, data))
    with pytest.raises(vr.SaveError):
        vr.replace_unsupported_career(tmp_path, 77, vr._new_career("Tester"))
    assert (tmp_path / "77.json").read_bytes() == old


def test_previous_checkpoint_tracks_changes_and_identical_writes_do_not_age_it(tmp_path):
    save = _world_with_seed(42).save
    vr.write_save(tmp_path, 77, save)
    original = (tmp_path / "77.json").read_bytes()
    save.pilot.credits += 100
    vr.write_save(tmp_path, 77, save)
    previous = tmp_path / "77.previous.json"
    assert previous.read_bytes() == original
    vr.write_save(tmp_path, 77, save)
    assert previous.read_bytes() == original
    current = (tmp_path / "77.json").read_bytes()
    save.pilot.credits += 100
    vr.write_save(tmp_path, 77, save)
    assert previous.read_bytes() == current


def test_invalid_outgoing_checkpoint_does_not_replace_either_saved_copy(tmp_path):
    save = _world_with_seed(42).save
    vr.write_save(tmp_path, 77, save)
    save.pilot.credits += 10
    vr.write_save(tmp_path, 77, save)
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    save.ship.fuel = -1
    with pytest.raises(vr.SaveError, match="invalid career"):
        vr.write_save(tmp_path, 77, save)
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.json")} == before


def test_failed_previous_copy_prevents_current_save_replacement(tmp_path, monkeypatch):
    save = _world_with_seed(42).save
    vr.write_save(tmp_path, 77, save)
    original = (tmp_path / "77.json").read_bytes()
    replace = vr.os.replace

    def fail_previous(source, target):
        if target.name == "77.previous.json":
            raise OSError("previous copy unavailable")
        return replace(source, target)

    monkeypatch.setattr(vr.os, "replace", fail_previous)
    save.pilot.credits += 10
    with pytest.raises(OSError):
        vr.write_save(tmp_path, 77, save)
    assert (tmp_path / "77.json").read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_pending_journey_is_validated_and_preserved_through_recovery(tmp_path, monkeypatch):
    import json

    world = _world_with_seed(42)
    world.event_rng.seed(0)
    destination = world.here.connections[0]
    snapshots = []
    world._checkpoint = lambda current: snapshots.append(json.dumps(current.save.to_dict()).encode())
    monkeypatch.setattr(vr, "read_key", lambda: "F")
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, destination)
    pending = next(raw for raw in snapshots if json.loads(raw)["pending_travel"] is not None)
    (tmp_path / "77.previous.json").write_bytes(pending)
    (tmp_path / "77.json").write_bytes(b"broken")
    restored = vr.restore_previous_career(tmp_path, 77, pending)
    assert restored.pending_travel == json.loads(pending)["pending_travel"]
    resumed = vr.World(restored)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), resumed, destination)
    assert resumed.save.to_dict() == world.save.to_dict()


def _broken_career_with_previous(tmp_path):
    save = _world_with_seed(42).save
    save.turn = 7
    save.pilot.handle = "Recovered Pilot"
    vr.write_save(tmp_path, 77, save)
    expected = (tmp_path / "77.json").read_bytes()
    save.turn = 8
    vr.write_save(tmp_path, 77, save)
    (tmp_path / "77.json").write_bytes(b"damaged original")
    return expected


@pytest.mark.parametrize("commands", [b"B", b"RNB", b"R", b"", b"\x1b[AQ"])
def test_real_recovery_back_decline_eof_and_special_keys_write_nothing(tmp_path, commands):
    import json
    import os
    import subprocess

    _broken_career_with_previous(tmp_path)
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}), encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=commands, capture_output=True,
                            env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)), timeout=10)
    assert result.returncode == (0 if commands.endswith((b"B", b"Q")) else 1)
    assert not result.stderr
    assert b"Career recovery" in result.stdout and b"Day 7" in result.stdout
    assert b"Pilot callsign" not in result.stdout
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.json")} == before


def test_real_confirmed_recovery_preserves_original_before_success_and_resumes(tmp_path):
    expected = _broken_career_with_previous(tmp_path)
    with _door_stopped_at(tmp_path, b"RY", b"Previous checkpoint restored"):
        restored, is_new, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert not is_new and restored.turn == 7
        archives = list(tmp_path.glob("77.recovery-*.json"))
        assert len(archives) == 1 and archives[0].read_bytes() == b"damaged original"
        assert (tmp_path / "77.previous.json").read_bytes() == expected
    with _door_stopped_at(tmp_path, b"Q", b"Welcome back, Recovered Pilot"):
        restored, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert restored.turn == 7


@pytest.mark.parametrize("failure", ["archive", "replacement", "changed", "full"])
def test_recovery_failure_keeps_primary_and_previous(tmp_path, monkeypatch, failure):
    expected = _broken_career_with_previous(tmp_path)
    if failure == "archive":
        monkeypatch.setattr(vr.tempfile, "NamedTemporaryFile", lambda **kw: (_ for _ in ()).throw(PermissionError()))
    elif failure == "replacement":
        replace = vr.os.replace

        def fail_primary(source, destination):
            if Path(destination).name == "77.json":
                raise OSError("primary replacement failed")
            return replace(source, destination)

        monkeypatch.setattr(vr.os, "replace", fail_primary)
    elif failure == "changed":
        expected = expected + b" "
    else:
        for index in range(vr.MAX_RECOVERY_COPIES):
            (tmp_path / f"77.recovery-{index}.json").write_bytes(b"retained")
    before_previous = (tmp_path / "77.previous.json").read_bytes()
    with pytest.raises((OSError, vr.ResumeError)):
        vr.restore_previous_career(tmp_path, 77, expected)
    assert (tmp_path / "77.json").read_bytes() == b"damaged original"
    assert (tmp_path / "77.previous.json").read_bytes() == before_previous
    if failure == "replacement":
        archives = list(tmp_path.glob("77.recovery-*.json"))
        assert len(archives) == 1 and archives[0].read_bytes() == b"damaged original"


@pytest.mark.parametrize("failure", ["write", "flush", "fsync", "close", "publish"])
def test_failed_recovery_archive_never_consumes_a_retained_slot(tmp_path, monkeypatch, failure):
    previous = _broken_career_with_previous(tmp_path)
    create_temporary = vr.tempfile.NamedTemporaryFile

    class FailingArchive:
        def __init__(self, **kwargs):
            self.file = create_temporary(**kwargs)
            self.name = self.file.name

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()
            if failure == "close":
                raise OSError("archive close failed")

        def write(self, data):
            if failure == "write":
                self.file.write(data[:3])
                raise OSError("archive write failed")
            return self.file.write(data)

        def flush(self):
            if failure == "flush":
                raise OSError("archive flush failed")
            self.file.flush()

        def fileno(self):
            return self.file.fileno()

    with monkeypatch.context() as patch:
        patch.setattr(vr.tempfile, "NamedTemporaryFile", FailingArchive)
        if failure in {"fsync", "publish"}:
            def fail(*args):
                raise OSError(f"archive {failure} failed")
            patch.setattr(vr.os, "fsync" if failure == "fsync" else "replace", fail)
        for _ in range(vr.MAX_RECOVERY_COPIES + 1):
            with pytest.raises(OSError, match="archive"):
                vr.restore_previous_career(tmp_path, 77, previous)
            assert not list(tmp_path.glob("77.recovery-*.json"))
            assert not list(tmp_path.glob("*.tmp"))
            assert (tmp_path / "77.json").read_bytes() == b"damaged original"
            assert (tmp_path / "77.previous.json").read_bytes() == previous

    vr.restore_previous_career(tmp_path, 77, previous)
    assert (tmp_path / "77.json").read_bytes() == previous
    archives = list(tmp_path.glob("77.recovery-*.json"))
    assert len(archives) == 1 and archives[0].read_bytes() == b"damaged original"


@pytest.mark.parametrize("full_archives", [False, True])
def test_missing_primary_requires_recovery_and_validated_previous_can_be_restored(tmp_path, full_archives):
    expected = _broken_career_with_previous(tmp_path)
    (tmp_path / "77.json").unlink()
    if full_archives:
        for index in range(vr.MAX_RECOVERY_COPIES):
            (tmp_path / f"77.recovery-{index}.json").write_bytes(b"retained")
    with pytest.raises(vr.ResumeError, match="missing"):
        vr.load_or_create_save(tmp_path, 77, "Tester")
    restored = vr.restore_previous_career(tmp_path, 77, expected)
    assert restored.turn == 7 and (tmp_path / "77.json").read_bytes() == expected


@pytest.mark.parametrize("kind", ["schema", "galaxy", "journey", "rng", "event", "journey_field",
                                 "encounter_field", "pirate_field", "combat_field", "snapshot_field"])
def test_future_formats_never_offer_or_allow_downgrade_recovery(tmp_path, monkeypatch, kind):
    import json
    import os
    import subprocess

    previous = _broken_career_with_previous(tmp_path)
    future = json.loads(previous)
    if kind in {"schema", "galaxy"}:
        future[f"{kind}_version"] = 99
    elif kind == "journey":
        future["pending_travel"] = {"version": 99}
    elif kind == "rng":
        future["event_rng_state"] = [99, [], None]
    elif kind == "event":
        future["active_event"] = {"economy": "Industrial", "commodity": "metals", "direction": "boom",
                                  "turns_remaining": 2, "description": "News", "future_rule": True}
    else:
        destination = vr.World(vr.SaveData.from_dict(future)).here.connections[0]
        travel = {"version": 1, "origin": 0, "destination": destination, "escort_index": 0, "phase": "primary",
                  "primary": "random", "encounter": {}, "escorts": [], "destroyed": False, "was_discovered": True,
                  "bounty": None}
        future["pending_travel"] = travel
        pirate = {"name": "Raider", "tier": 1, "hp": 35, "hp_max": 35}
        if kind == "journey_field":
            travel["future_rule"] = True
        elif kind == "encounter_field":
            travel["encounter"]["future_rule"] = True
        elif kind == "pirate_field":
            travel["encounter"]["pirate"] = dict(pirate, future_rule=True)
        elif kind == "combat_field":
            travel["encounter"]["combat"] = {"pirate": pirate, "outcome": None, "lines": [],
                                             "tactics": vr.new_tactics(vr.Pirate(**pirate)),
                                             "hull_before": 60, "future_rule": True}
        else:
            mission = vr.Mission(1, "bounty", "Raider", 100, 0, destination, pirate_tier=1).to_dict()
            future["active_missions"] = [mission]
            travel["primary"] = "bounty"
            travel["bounty"] = dict(mission, future_rule=True)
    path = tmp_path / "77.json"
    path.write_text(json.dumps(future), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(vr.UnsupportedSave) as error:
        vr.load_or_create_save(tmp_path, 77, "Tester")
    keys = iter("RB")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        assert vr.screen_save_recovery(vr.Palette(False), tmp_path, 77, error.value).save is None
    assert "[R] Restore" not in output.getvalue()
    with pytest.raises(vr.UnsupportedSave):
        vr.restore_previous_career(tmp_path, 77, previous)
    assert path.read_bytes() == original and not list(tmp_path.glob("77.recovery-*"))
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=b"RYB", capture_output=True,
                            env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)), timeout=10)
    assert result.returncode == 0 and not result.stderr
    assert b"[R] Restore" not in result.stdout and b"Pilot callsign" not in result.stdout
    assert path.read_bytes() == original and not list(tmp_path.glob("77.recovery-*"))


@pytest.mark.parametrize("problem", ["oversized", "unreadable", "full"])
def test_recovery_hides_restore_and_explains_impossible_preservation(tmp_path, monkeypatch, problem):
    _broken_career_with_previous(tmp_path)
    primary = tmp_path / "77.json"
    read = vr._read_save_bytes
    if problem == "oversized":
        primary.write_bytes(b"x" * (vr.MAX_SAVE_BYTES + 1))
    elif problem == "unreadable":
        def fail_primary(path):
            if path == primary:
                raise PermissionError("cannot read primary")
            return read(path)
        monkeypatch.setattr(vr, "_read_save_bytes", fail_primary)
    else:
        for index in range(vr.MAX_RECOVERY_COPIES):
            (tmp_path / f"77.recovery-{index}.json").write_bytes(b"retained")
    before = {path.name: path.read_bytes() for path in tmp_path.glob("*.json")}
    keys = iter("R" + "N" * 20 + "B")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        result = vr.screen_save_recovery(vr.Palette(False), tmp_path, 77, vr.ResumeError("Career unavailable"))
    rendered = " ".join(output.getvalue().split())
    assert result.save is None and result.exit_code == 0
    assert "[R] Restore" not in rendered and "manual recovery" in rendered
    reason = {"oversized": "file size", "unreadable": "cannot be read", "full": "copies are full"}[problem]
    assert reason in rendered
    assert {path.name: path.read_bytes() for path in tmp_path.glob("*.json")} == before


def test_missing_economy_event_fields_remain_recoverable_corruption(tmp_path, monkeypatch):
    import json
    previous = _broken_career_with_previous(tmp_path)
    broken = json.loads(previous)
    broken["active_event"] = {"economy": "Industrial"}
    (tmp_path / "77.json").write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(vr.ResumeError) as error:
        vr.load_or_create_save(tmp_path, 77, "Tester")
    assert not isinstance(error.value, vr.UnsupportedSave)
    keys = iter("B")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_save_recovery(vr.Palette(False), tmp_path, 77, error.value)
    assert "[R] Restore" in output.getvalue()


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
def test_recovery_pages_fit_terminal_and_restore_is_on_last_page(tmp_path, monkeypatch, terminal, width, height):
    _broken_career_with_previous(tmp_path)
    terminal(width, height)
    chunks, current = [], io.StringIO()
    keys = iter("N" * 40 + "B")

    def read():
        chunks.append(current.getvalue())
        current.seek(0)
        current.truncate(0)
        return next(keys)

    monkeypatch.setattr(vr, "read_key", read)
    with contextlib.redirect_stdout(current):
        vr.screen_save_recovery(vr.Palette(False), tmp_path, 77, vr.ResumeError("Damaged career"))
    for page in chunks:
        rows = page.splitlines()
        assert len(rows) <= height, (width, height, rows)
        assert all(vr._visible_width(row) <= width for row in rows)
    assert any("[R] Restore" in page for page in chunks)


@pytest.mark.parametrize("fail", [False, True])
def test_display_selection_saves_before_applying_and_acknowledging(monkeypatch, fail):
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    world = _world_with_seed(42)
    keys = iter(["4", "4", "B"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    output = io.StringIO()
    saves = []
    def checkpoint(current):
        assert vr._OUTPUT_STYLE == "auto"
        assert "Display saved:" not in output.getvalue()
        assert current.save.display_style == "plain"
        saves.append(current.save.display_style)
        if fail: raise vr.SaveError()
    world._checkpoint = checkpoint
    with contextlib.redirect_stdout(output):
        if fail:
            with pytest.raises(vr.SaveError): vr.screen_display_options(vr.Palette(False), world)
        else:
            vr.screen_display_options(vr.Palette(False), world)
    assert saves == ["plain"]
    if fail:
        assert vr._OUTPUT_STYLE == "auto" and "Display saved:" not in output.getvalue()
    else:
        assert vr._OUTPUT_STYLE == "plain"
        assert "Display saved:" in output.getvalue() and "Already using:" in output.getvalue()


@pytest.mark.parametrize("style,key", [("auto", b"1"), ("basic", b"2"), ("mono", b"3"), ("plain", b"4")])
def test_real_display_saved_before_ack_and_applied_from_restart_title(tmp_path, style, key):
    import os, subprocess
    world = _world_with_seed(42)
    world.save.display_style = "basic" if style == "auto" else "auto"
    vr.persist(world, tmp_path, 77)
    with _door_stopped_at(tmp_path, b"O" + key, b"Display saved:"):
        saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert saved.display_style == style
    before = (tmp_path / "77.json").read_bytes()
    for commands in (b"O", b"OBQ", b"O" + key + b"BQ"):
        result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=commands,
            capture_output=True, timeout=10, env=dict(os.environ,
                VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(tmp_path / "door_info.json")))
        assert result.returncode == 0 and not result.stderr
        assert b"Display Options" in result.stdout
        assert (tmp_path / "77.json").read_bytes() == before
        if style in ("mono", "plain"): assert b"\x1b" not in result.stdout
        if style == "basic": assert b"38;" not in result.stdout
        if style == "plain": assert result.stdout.isascii()


def test_a_tagged_market_row_still_fits_one_line(monkeypatch):
    """`Illegal [CRASH]` on a Haven contraband row overflowed 80 columns (#412 review)."""
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    world = _world_with_seed(303)
    haven = next(s for s in world.galaxy if s.economy == "Haven")
    world.save.current_system = haven.id; haven.discovered = True
    contraband = vr.CONTRABAND_COMMODITIES[0]
    world.save.active_event = {"economy": haven.economy, "commodity": contraband, "direction": "crash",
                               "turns_remaining": 4, "description": "Prices collapse",
                               "system_ids": [haven.id]}
    _set_cargo(world, {contraband: 7})
    rows = [row for row in vr.market_catalog_lines(world, [contraband]) if row.startswith("[")]
    assert rows and all(vr._visible_width(row) <= 79 for row in rows)
    assert "Illegal" in rows[0] and ("[CRASH]" in rows[0] or "[BOOM]" in rows[0])
    depth = {"stock": 96, "demand": 48}
    head = "[J] Narcotics: buy 1200cr; sell 1100cr."
    assert vr._market_row(head, depth, 7, []) == head + " Stock 96; demand 48; hold 7."
    tagged = vr._market_row(head, depth, 7, ["Illegal", "[CRASH]"])
    assert tagged == head + " Hold 7. Illegal [CRASH]"  # depth gives way first, the hold last
    assert vr._visible_width(tagged) <= 79
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 20)  # too narrow for any form: keep everything and wrap
    narrow = [row for row in vr.market_catalog_lines(world, [contraband]) if row.startswith("[")]
    assert "Stock" in narrow[0] and "hold 7" in narrow[0]


def test_first_flight_names_its_acceptance_action_on_every_page(monkeypatch, terminal):
    """The first screen of the game must not hide its one action (#412 review)."""
    terminal(80, 24)
    world = _world_with_seed(42)
    offer = vr.opening_assignment_offer(world)
    assert offer is not None
    frames = []
    output = io.StringIO()
    def choose():
        frames.append(output.getvalue()); output.seek(0); output.truncate(0)
        return "N" if len(frames) == 1 else "B"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output):
        assert vr._screen_opening_offer(vr.Palette(False), world, offer) is False
    plain = [vr._ANSI_RE.sub("", frame) for frame in frames]
    assert len(plain) > 1 and "[A] on last page." in plain[0]
    assert "[A] Accept contract" in plain[-1]
    assert all(len(frame.splitlines()) <= 24 for frame in plain)


def test_a_blocked_survey_does_not_advertise_scanning(monkeypatch):
    """"Revisiting cannot complete it" and "scanning may avoid travel" cannot both hold."""
    world = _world_with_seed(42)
    target = next(s for s in world.galaxy if s.id != 0)
    mission = vr.Mission(9, "scan", "Survey the drift", 250, 0, target.id)
    world.save.active_missions = [mission]
    target.discovered = False
    open_text = " ".join(vr.mission_details(world, mission))
    assert "Survey scanning may avoid travel." in open_text and "BLOCKED SURVEY" not in open_text
    target.discovered = True
    world.sync_discovered()
    blocked = " ".join(vr.mission_details(world, mission))
    assert "BLOCKED SURVEY" in blocked
    assert "Survey scanning may avoid travel." not in blocked
    assert "Area surveys require a scanner" not in blocked


@pytest.mark.parametrize("width,height", [(20, 12), (40, 12), (80, 24)])
def test_precomputed_portrait_pages_also_paginate_against_the_shown_footer(monkeypatch, terminal, width, height):
    """The viewport builds its pages ahead of `_draw_service_page`, so it needs the
    same single-page retry (issue #412 review)."""
    terminal(width, height)
    world = _world_with_seed(42)
    large, compact, details, title = vr.viewport_content(world, "1")
    footer = "[1-4] View [<] Prev [>] Next [B] Back: "
    pages = vr.portrait_pages(vr.Palette(False), large, compact, details, title, footer)
    shortened = vr.single_page_footer(footer, 1)
    assert shortened != footer
    if len(pages) == 1:
        # It fits: it must be the layout measured against the bar that will be shown.
        assert pages == vr._portrait_pages_for(vr.Palette(False), large, compact, details, title, shortened)
    else:
        assert len(vr._portrait_pages_for(vr.Palette(False), large, compact, details, title, shortened)) > 1


def test_the_rescue_tow_prepares_the_station_it_tows_you_to(monkeypatch):
    """Being towed home is a station transition, so Freeport's board is ready
    when the deck draws it (issue #417 review)."""
    world = _world_with_seed(42)
    world.checkpoint()
    destination = sorted(world.here.connections)[0]
    world.save.current_system = destination
    world.save.turn += vr.MISSION_BOARD_DAYS + 1  # the old board has expired
    world.save.ship.fuel = 0
    world.save.pilot.credits = 0
    _set_cargo(world, {})
    assert vr.is_stranded(world)
    keys = iter(["Q"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(vr, "pause", lambda p, msg=None: None)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_station_menu(vr.Palette(False), world)
    assert world.save.current_system == 0
    assert vr.posted_mission_offers(world)  # the board of the station just entered


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
def test_every_paged_screen_fits_and_offers_back_on_every_page(monkeypatch, terminal, width, height):
    """One table instead of a per-screen copy (issue #418).

    Each screen is walked to the last page its own counter advertises, using the
    paging key its own action bar offers, so a screen that pages with `N` is not
    silently redrawing page one.
    """
    import re
    terminal(width, height)
    screens = ["screen_chart", "screen_missions", "screen_market", "screen_shipyard",
               "screen_status", "screen_pilot_guide", "screen_trading_ledger",
               "screen_remembered_markets", "screen_economy_opportunities",
               "screen_specialists"]
    for name in screens:
        world = _world_with_seed(42)
        world.checkpoint()
        frames, output, reached = [], io.StringIO(), []
        def choose():
            frame = vr._ANSI_RE.sub("", output.getvalue()); output.seek(0); output.truncate(0)
            frames.append(frame)
            assert len(frames) < 400, f"{name} never reached its last page"
            counters = []
            for row in frame.splitlines():
                # The counter ends the title row; other numbers ("Fuel 24/24") precede it.
                counters = re.findall(r"(\d+)/(\d+)", " ".join(row.split()))
                if counters:
                    break
            assert counters, f"{name} draws no page counter in {frame!r}"
            page, count = int(counters[-1][0]), int(counters[-1][1])
            if page >= count:
                reached.append(count)
                return "B"
            forward = ">" if ("[>]" in frame or "[<>]" in frame or "[< >]" in frame) else "N"
            assert f"[{forward}]" in frame or "[<>]" in frame or "[< >]" in frame, f"{name} advertises no paging key"
            return forward
        monkeypatch.setattr(vr, "read_key", choose)
        with contextlib.redirect_stdout(output):
            getattr(vr, name)(vr.Palette(False), world)
        assert frames and reached, name
        assert len(frames) >= reached[0], f"{name} skipped pages"
        for frame in frames:
            assert all(vr._visible_width(row) <= width for row in frame.splitlines()), name
            assert len(frame.splitlines()) <= height, name
            assert "[B]" in frame or "[Q]" in frame, name


def test_save_validators_are_named_as_validators():
    for name in ("_validate_mission_boards", "_validate_trade_total", "_validate_tracked_mission_id",
                 "_validate_tactics", "_validate_warrant", "_validate_formation", "_validate_versioned"):
        assert callable(getattr(vr, name)), name
    assert not [name for name in vars(vr) if name.startswith("_load_") and name.endswith(
        ("_boards", "_total", "_mission_id"))]
