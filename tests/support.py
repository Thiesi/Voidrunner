"""The Voidrunner suite's shared support: the door module, and the worlds.

The door is loaded from its file path under a private module name rather than
through `netbbs.doors.bundled` -- same reasoning as `test_doors_runtime.py`
running it and `retro_trivia.py` this same way: this is the exact file NetBBS
launches as a standalone subprocess (see `netbbs.doors.runtime`), not an
ordinarily-imported library module, so testing it by path exercises precisely
what ships. It is loaded once here and shared by every module in this package
(issue #422); `test_backup.py` reaches the same file through the package, which
is a second load under a second name and is deliberate -- that test is about the
installed package, this suite is about the shipped script.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_VOIDRUNNER_PATH = (
    Path(__file__).resolve().parent.parent.parent / "src" / "netbbs" / "doors" / "bundled" / "voidrunner.py"
)


def _load_voidrunner():
    spec = importlib.util.spec_from_file_location("voidrunner_domain_under_test", _VOIDRUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # `dataclasses` (voidrunner.py uses `from __future__ import annotations`,
    # so field types are strings) resolves them via
    # `sys.modules[cls.__module__].__dict__` -- the module must already be
    # registered under its own name in sys.modules *before* exec_module
    # runs, or that lookup returns None and every @dataclass in the file
    # raises AttributeError at import time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vr = _load_voidrunner()


def _load_voidrunner():
    spec = importlib.util.spec_from_file_location("voidrunner_domain_under_test", _VOIDRUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # `dataclasses` (voidrunner.py uses `from __future__ import annotations`,
    # so field types are strings) resolves them via
    # `sys.modules[cls.__module__].__dict__` -- the module must already be
    # registered under its own name in sys.modules *before* exec_module
    # runs, or that lookup returns None and every @dataclass in the file
    # raises AttributeError at import time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _set_cargo(world, hold: dict, *, unit_cost: int | None = None) -> None:
    """Replace the hold with costed goods.

    Every unit in a schema-2 hold carries an acquisition-cost lot (issue #421), so
    a test that just needs goods aboard says so here instead of writing the cargo
    dict and leaving the basis behind. The lots are written directly rather than
    through `_acquire_cargo`, which would also open the trading ledger.
    """
    world.save.cargo, world.save.cargo_basis = {}, {}
    for commodity, quantity in hold.items():
        _add_cargo(world, commodity, quantity, unit_cost=unit_cost)


def _add_cargo(world, commodity: str, quantity, *, unit_cost: int | None = None) -> None:
    """Hold exactly `quantity` costed units of one commodity; see `_set_cargo`."""
    world.save.cargo.pop(commodity, None)
    world.save.cargo_basis.pop(commodity, None)
    if quantity <= 0:
        world.save.cargo[commodity] = quantity  # a zero entry is still a valid hold
        return
    unit = vr.price_for(world, world.save.current_system, commodity) if unit_cost is None else unit_cost
    world.save.cargo[commodity] = quantity
    world.save.cargo_basis[commodity] = [[quantity, quantity * unit]]
    if world.save.trading_ledger.since_day is None:
        # A recorded lot is recorded activity; the validator requires both.
        world.save.trading_ledger.since_day = world.save.turn



_BORDER = "╭╮╰╯│─═║╔╗╚╝├┤╠╣+-=|"


def plain(text: str) -> str:
    """One screen's raw output with its styling taken off.

    Page bodies are coloured now (issue #493), and colour lands *between*
    tokens: a row reading `Promoted to Void Baron` carries an escape before
    `Promoted` and another after it, so the sentence is no longer a substring
    of what was written. A test that looks for words the caller read reads them
    through here -- or through `page_text`, which already does this and takes
    the frame off as well.
    """
    stripped = vr._ANSI_RE.sub("", text)
    # The zero-width marks a screen uses to say what a row *is* -- a rule
    # across the frame, a table heading, a row of that table -- never reach a
    # terminal either; `draw_page` takes them off as it prints.
    for mark in (vr.SECTION_MARK, vr.STICKY_MARK, vr.MEMBER_MARK):
        stripped = stripped.replace(mark, "")
    return stripped


def page_source(lines) -> str:
    """A screen's own rows as `page_text` will show them back.

    A screen names its groups with rules drawn across the page frame, and
    `page_rows` drops border rows -- so a test comparing what a builder returned
    against what the caller read has to drop them here too.
    """
    return " ".join(plain(" ".join(
        line for line in lines if not line.startswith(vr.SECTION_MARK))).split())


_ANSI_BYTES = re.compile(rb"\x1b\[[0-9;]*[a-zA-Z]")


def plain_bytes(data: bytes) -> bytes:
    """`plain` for what a door process actually wrote down its pipe."""
    return _ANSI_BYTES.sub(b"", data)


def shows_page(data, title: str) -> bool:
    """Whether a door drew a page under `title`, its counter and all.

    A framed page puts the title in its top border and the counter in the
    far corner, with the border's own fill between them (issue #493), so a
    literal `"Title 1/"` is no longer in the bytes a door wrote.
    """
    text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else data
    pattern = re.compile(re.escape(title) + r"[\s─═+\-]*\d+/\d+")
    return any(pattern.search(row) for row in plain(text).splitlines())


def page_rows(frame: str, *, keep_indent: bool = False) -> list[str]:
    """The body rows of a drawn page, with the HUD frame taken off.

    Screens draw inside their frame again (issue #486), so a test that reads a
    page has to see past it: the border rows carry no content, the side borders
    are not part of a row's text, and a sentence that wraps has a border between
    its halves. Works the same on an unframed page, where there is nothing to
    take off, and on the `plain` display style, where the frame is +, - and |.
    """
    rows: list[str] = []
    for row in vr._ANSI_RE.sub("", frame).replace("\r\n", "\n").split("\n"):
        row = row.rstrip() if keep_indent else row.strip()
        if not row.strip():
            continue
        if row.lstrip()[0] in "╭╰╔╚├╠" or re.match(r"\+[-=]", row.lstrip()):
            continue  # a border row, titled or not
        if all(character in _BORDER or character == " " for character in row):
            continue  # what is left of one after a test cuts the title out
        if re.fullmatch(r"[A-Za-z0-9<>?]", row):
            continue  # the keypress a screen echoes when it ends a bar's row
        if row[0] in "│║|":
            row = row[1:]
        if row[-1:] in "│║|":
            row = row[:-1]
        rows.append(row.rstrip() if keep_indent else row.strip())
    return rows


def page_title(frame: str) -> str:
    """The title a page shows, wherever it is drawn.

    A framed screen draws its title into the top border, which `page_rows`
    drops; an unframed one prints it as the first row. Tests that look for the
    page counter, or for the credits a screen puts in its title, read it here.

    The border carries more than the title now (issue #493): the brand opens
    it, the page counter is pushed to the right-hand corner, and a run of the
    border's own fill sits between them. That fill is chrome, not text, so it
    collapses to a single space here and a caller's `Title n/m` still reads as
    `Title n/m`.
    """
    parts: list[str] = []
    for row in vr._ANSI_RE.sub("", frame).replace("\r\n", "\n").split("\n"):
        row = row.strip()
        if not row or re.fullmatch(r"[A-Za-z0-9<>?]", row):
            continue
        if row[0] in "╭╔" or re.match(r"\+[-=]", row):
            row = row.strip("╭╮╔╗+ ").strip("─═- ")
            # One box-drawing dash is already fill. An ASCII one is fill when
            # it stands alone between spaces or runs; a hyphen inside a
            # word (`Long-Range`) is part of the title.
            title = " ".join(re.sub(r"[─═]+|(?<=\s)-+(?=\s)|-{2,}", " ", row).split())
            if title:
                return title
            continue  # a plain top border: the header is the page's first row
        if all(character in _BORDER or character == " " for character in row):
            continue
        # Unframed, a long title wraps: it runs to the row the counter lands on,
        # and no further -- a page whose title has already been cut out must not
        # have its body read as one.
        parts.append(row)
        if re.search(r"\d+/\d+", " ".join(parts)) or len(parts) == 2:
            break
    return " ".join(parts)


def page_text(frame: str | list[str]) -> str:
    """`page_rows` for one frame or a run of them, as one whitespace-normalised
    string: what the caller read, in the order they read it."""
    frames = [frame] if isinstance(frame, str) else list(frame)
    return " ".join(" ".join(row for text in frames for row in page_rows(text)).split())


def _world_with_seed(seed: int) -> "vr.World":
    save = vr._new_career("Tester")
    save.seed = seed
    return vr.World(save)


def _post_and_accept_test_mission(world, mission):
    """Post authored test terms before exercising the real acceptance command."""
    board = world.save.mission_boards.setdefault(world.save.current_system, {
        "refresh_turn": world.save.turn + vr.MISSION_BOARD_DAYS, "offers": [],
    })
    board["offers"].append(mission.to_dict())
    vr.accept_mission(world, mission)


class _Sys:
    def __init__(self, x, y, discovered=True):
        self.x = x
        self.y = y
        self.discovered = discovered


def _drain_until(stream, output: bytearray, markers, events, stop=None) -> None:
    """Read a door's stdout in chunks, setting each event as its marker appears.

    `read(1)` is the slowest possible drain and the subprocess tests dominate the
    suite's runtime; `read1` returns whatever has already arrived, so a marker is
    still seen as soon as the door writes it (issue #422). Markers are matched in
    order, which lets a caller wait for a prompt before it writes, instead of
    racing the door's startup (issue #416 review).

    Markers are matched against the output with its styling taken off: page
    bodies are coloured now (issue #493), so a phrase the caller reads as one
    sentence is several runs of bytes with escapes between them, and a marker
    that had to be contiguous in the raw stream would pin the game's colours in
    place rather than its behaviour.

    Draining continues past the last marker, to the cap or the end of the
    stream. Stopping at the marker left the door writing into a pipe nobody was
    emptying, which was harmless only while a screen was smaller than the pipe
    buffer: a coloured page is several times the bytes of a plain one, and the
    door then blocked mid-screen and never reached its next keypress.

    `stop` is what makes "stopped at" mean it. A caller that wants the door
    frozen where the marker appeared passes its killer here, so the door is
    stopped *in this thread*, the instant the bytes land -- not whenever the
    waiting thread is next scheduled. The full pipe used to be an accidental
    brake on that gap; draining removed it, and a door that ran on could finish
    the journey the test meant to catch mid-flight.
    """
    if isinstance(markers, bytes):
        markers, events = (markers,), (events,)
    pending = list(zip(markers, events))
    while len(output) < 128_000:
        chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(1)
        if not chunk:
            return
        output.extend(chunk)
        if pending:
            seen = plain_bytes(bytes(output))
            while pending and pending[0][0] in seen:
                pending.pop(0)[1].set()
            if not pending and stop is not None:
                stop()


@contextlib.contextmanager
def _door_stopped_at(tmp_path, commands, acknowledgement: bytes, ready: bytes | None = None):
    """Run the shipped script, then force-kill while stdin is still open."""
    import json
    import os
    import subprocess
    import threading

    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}), encoding="utf-8")
    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info))
    proc = subprocess.Popen(
        [sys.executable, str(_VOIDRUNNER_PATH)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    reached = threading.Event()
    output = bytearray()

    prompt = threading.Event()
    markers = (ready, acknowledgement) if ready else (acknowledgement,)
    events = (prompt, reached) if ready else (reached,)
    # The door is stopped by the reader, the moment its last marker lands: this
    # helper exists to photograph a career mid-action, and a door left running
    # until the waiting thread woke up could finish the action instead.
    reader = threading.Thread(target=_drain_until,
                              args=(proc.stdout, output, markers, events, proc.kill))
    reader.start()
    try:
        # `ready` waits for the door's own prompt before writing, so the test does
        # not race process startup; a list is then written with a real gap between
        # parts, because a contiguous `ESC X` is an Alt or control-string sequence
        # by design and a *lone* Escape only exists on the wire once the decoder's
        # timeout has passed (issue #416 review).
        if ready:
            assert prompt.wait(15), bytes(output).decode("utf-8", errors="replace")
        parts = commands if isinstance(commands, list) else [commands]
        for index, part in enumerate(parts):
            if index:
                time.sleep(max(0.25, vr._INPUT_TIMEOUT * 5))
            proc.stdin.write(part)
            proc.stdin.flush()
        assert reached.wait(10), bytes(output).decode("utf-8", errors="replace")
        proc.kill()
        proc.wait(timeout=5)
        yield bytes(output)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        reader.join(timeout=5)
        assert not reader.is_alive()
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()


def _mission_details_world(kind="delivery"):
    world = _world_with_seed(42)
    target = next(s.id for s in world.galaxy if not s.discovered)
    mission = vr.Mission(1, kind, "Complete and unabridged contract objective", 500, 0, target,
                         commodity="food" if kind == "delivery" else None,
                         quantity=3 if kind == "delivery" else None, deadline_turn=10, pirate_tier=2)
    world.save.mission_boards[0] = {"refresh_turn": 3, "offers": [mission.to_dict()]}
    return world, mission


@contextlib.contextmanager
def _live_voidrunner(tmp_path, user_id=77, commands=b"", acknowledgement=b"STATION SERVICES"):
    """Own a real door process until the test exits, draining its output."""
    import json
    import os
    import subprocess
    import threading

    tmp_path.mkdir(parents=True, exist_ok=True)
    info = tmp_path / f"info-{user_id}.json"
    info.write_text(json.dumps({"user_id": user_id, "handle": "Tester"}), encoding="utf-8")
    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info))
    proc = subprocess.Popen([sys.executable, str(_VOIDRUNNER_PATH)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    output = bytearray()
    reached = threading.Event()

    reader = threading.Thread(target=_drain_until, args=(proc.stdout, output, acknowledgement, reached))
    reader.start()
    try:
        proc.stdin.write(commands)
        proc.stdin.flush()
        assert reached.wait(10), output.decode("utf-8", errors="replace")
        yield proc, env
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        reader.join(timeout=5)
        assert not reader.is_alive()
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()


def _world_with_pending_fight(*, tactics=None):
    world = _world_with_seed(42)
    destination = sorted(world.here.connections)[0]
    mission = vr.Mission(1, "bounty", "Intercept raider", 500, 0, destination, pirate_tier=2)
    world.save.active_missions = [mission]
    pirate = vr.Pirate("Bounty raider", 2, 50, 50)
    # Every saved fight carries its ruleset and the hull it opened with (#421).
    combat = {"pirate": vr.dataclasses.asdict(pirate), "outcome": None, "lines": [],
              "tactics": vr.new_tactics(pirate) if tactics is None else tactics,
              "hull_before": world.save.ship.hull_hp}
    world.save.turn = 1
    world.save.pending_travel = {"version": 1, "origin": 0, "destination": destination,
        "was_discovered": True, "destroyed": False, "phase": "primary", "primary": "bounty",
        "bounty": mission.to_dict(), "escorts": [], "escort_index": 0, "encounter": {"combat": combat}}
    return world, pirate


def _world_with_exploration_choice(kind):
    world, _ = _world_with_pending_fight()
    travel = world.save.pending_travel
    travel.update(primary="random", bounty=None, encounter={"kind": kind})
    world.save.active_missions = []
    world.by_id[travel["destination"]].discovered = True
    return world


def _world_with_named_crew(role, paid=0):
    world = _world_with_seed(42); world.save.pilot.credits = 100_000
    world.save.pilot.highest_rank_seen = len(vr.RANKS) - 1
    vr.hire_crew(world, role)
    world.save.ship.crew_records[role]["paid_jumps"] = paid
    world.save.turn = paid
    return world


# Endings archive the old run and launch a distinct, ordinary-module New Game+.
def _finale_world(finale="legend"):
    world=_world_with_seed(42)
    if finale=="legend": world.save.pilot.highest_rank_seen=4
    elif finale=="trader":
        world.save.trading_ledger.sales_revenue=60000; world.save.trading_ledger.sales_cost=10000; world.save.trading_ledger.since_day=0
    elif finale=="explorer":
        for system in world.galaxy: system.discovered=True
        world.sync_discovered()
    elif finale=="combat": world.save.pilot.kills=50
    return world


def _world_at_food_producer(seed: int = 42) -> "vr.World":
    world = _world_with_seed(seed)
    world.save.current_system = next(s.id for s in world.galaxy if "food" in vr.ECONOMY_PRODUCES[s.economy])
    world.save.pilot.credits = 1_000_000
    return world


def _escort_world(outcome: str):
    world = _world_with_seed(42)
    destination = sorted(world.here.connections)[0]
    mission = vr.Mission(7, "escort", "Escort a convoy", 800, 0, destination, pirate_tier=1)
    world.save.active_missions = [mission]
    world.save.turn = 1
    world.save.pending_travel = {"version": 1, "origin": 0, "destination": destination, "was_discovered": True,
        "destroyed": False, "phase": "escorts", "primary": "random", "bounty": None,
        "escorts": [mission.to_dict()], "escort_index": 0, "encounter": {}}
    return world, mission


def _box_rows(text: str) -> list[str]:
    rows = [vr._ANSI_RE.sub("", line) for line in text.split("\r\n")]
    return [row for row in rows if row and row[0] in "╔║╠╚╭│├╰+|"]
