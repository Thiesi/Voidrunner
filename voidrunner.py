#!/usr/bin/env python3
"""
Voidrunner -- a persistent single-player space trading/exploration door
for NetBBS (issue #172 vertical: door-managed private save).

Same v1 door contract as `netbbs.doors.bundled.retro_trivia`: reads the
drop-file NetBBS hands it via `NETBBS_DOOR_INFO` for handle/user_id/color
depth, then owns raw stdin/stdout for the whole session (single keystroke
reads; this module decodes UTF-8 and whole terminal keys, with a line editor for
numeric quantities and the callsign prompt, since NetBBS gives a door no
line-editing help). Runnable completely standalone outside NetBBS too.
Zero external dependencies -- stdlib only.

**Persistence**: The native door API provides no mediated database access
and deletes its scratch working directory after every session (see
`netbbs.doors.runtime`'s own docstring) -- a door manages any save data
entirely itself. This door keeps one JSON save file per caller, keyed by
the drop-file's stable numeric `user_id` (never the handle, which can
change), under `VOIDRUNNER_SAVE_DIR` if set, else `~/.netbbs/
voidrunner_saves/`. Completed station actions commit before their success
message, including actions inside nested menus. Each completed auto-route
hop also commits without requiring the player to leave the chart. One process
holds the pilot session lock from load through its final checkpoint. Writes
use a flushed private temporary file plus `os.replace`: a door can be
killed at any moment without a graceful-shutdown guarantee. Interrupted
journeys resume before station access, with the same opponent HP, random
state, and pending mission resolution. Each combat decision commits its
complete effects before narration; consumed rewards are never replayed.

The default save location is deliberately *not* relative to this
script's own path: this module now ships as real installed package data
(`netbbs.doors.bundled`, resolved via `importlib.resources` -- see
`netbbs.doors.bundled.resolve_bundled_door_path`), and an installed
package's own directory is routinely read-only and/or wiped clean on
every upgrade, neither of which a save file can tolerate. A production
node with an unusual layout should set `VOIDRUNNER_SAVE_DIR` explicitly
rather than rely on the home-directory default holding for its own
service account. NetBBS forwards this explicit directory override; different
installations sharing an OS account need distinct directories.

**Architecture** (deliberate, for a reason beyond this door): the rules
of the game -- galaxy generation, pricing, combat resolution, mission
logic -- live in plain functions/dataclasses that only ever take a
`World` and return a new one plus narrative text (the "domain layer"
below); the storage layer owns career files, session leases and score records.
Everything that
touches `sys.stdin`/`sys.stdout` is confined to the "UI layer" at the
bottom. Today the storage layer is "read/write a local JSON file." If a
future NetBBS revision ever grows a mediated way for a door to talk to a
persistent background service (a real shared galaxy), *that* swap only
ever has to replace the storage layer -- the domain rules and the UI
loop are not coupled to "state lives in a local file." This does **not**
by itself make Voidrunner multiplayer, and nothing here assumes it ever
will be; it just avoids closing that door (see the design discussion in
issue #172 -- doors are locked as single-player/session-scoped in v1,
and this stays strictly inside that: one save, one player, no shared
galaxy state, no networking). Independent score records provide a shared ranking.

**Load-bearing invariant**: `generate_galaxy()` is a pure function of
the save's `seed` -- only the seed is persisted, not the galaxy itself,
to keep save files small. That only works if the *exact sequence* of
`random.Random` calls inside `generate_galaxy()` never changes; changing
call order/count for an existing release would silently regenerate a
different galaxy for every existing save (systems shifting id/name/
economy under a player's feet, `discovered` ids pointing at the wrong
system). Add new randomness only at the end of that function, never
threaded into the middle of its existing call sequence.
"""

from __future__ import annotations

import collections
import contextlib
import dataclasses
import errno
import json
import math
import os
import random
import re
import sys
import tempfile
import time
import unicodedata
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ESC = "\x1b"
RESET = f"{ESC}[0m"
BOLD = f"{ESC}[1m"
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\([AB0-2]|\x1b[78HDM]")
_OUTPUT_WIDTH = 80
_OUTPUT_HEIGHT = 24


# ---------------------------------------------------------------------------
# Drop-file + raw terminal I/O. Kept local so the bundled game remains a
# self-contained executable a SysOp can point straight at.
# ---------------------------------------------------------------------------


def _load_door_info() -> dict:
    default = {
        "handle": "Guest",
        "user_id": 0,
        "terminal_width": 80,
        "terminal_height": 24,
        "color_depth": "256",
        "node_name": "NetBBS",
    }
    path = os.environ.get("NETBBS_DOOR_INFO")
    if not path:
        return default
    try:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return default
    default.update(info)
    return default


class Palette:
    """Same six-color palette as retro_trivia.py's own -- see that
    module's class docstring for why a real nearest-256 algorithm is
    overkill here too."""

    def __init__(self, truecolor: bool):
        self._truecolor = truecolor

    def _sgr(self, rgb: tuple[int, int, int], idx256: int) -> str:
        if self._truecolor:
            r, g, b = rgb
            return f"{ESC}[38;2;{r};{g};{b}m"
        return f"{ESC}[38;5;{idx256}m"

    @property
    def title(self) -> str:
        return self._sgr((255, 90, 190), 205)

    @property
    def accent(self) -> str:
        return self._sgr((100, 220, 255), 51)

    @property
    def correct(self) -> str:
        return self._sgr((110, 255, 130), 46)

    @property
    def wrong(self) -> str:
        return self._sgr((255, 100, 100), 203)

    @property
    def muted(self) -> str:
        return self._sgr((150, 150, 160), 244)

    @property
    def gold(self) -> str:
        return self._sgr((255, 200, 60), 220)


def out(text: str = "") -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def out_line(text: str = "") -> None:
    out(_wrap_output(text, _OUTPUT_WIDTH) + "\r\n")


def out_prompt(text: str) -> None:
    """Write a prompt without relying on the terminal's soft wrapping."""
    out(_wrap_output(text, max(1, _OUTPUT_WIDTH - 1)))


def _wrap_output(text: str, width: int) -> str:
    """ANSI-aware, display-column-bounded wrapping for this standalone door."""
    text = text.replace("\t", " ")
    atoms: list[tuple[str, str, int]] = []
    pending_escape = ""
    position = 0
    for match in ANSI_ESCAPE_RE.finditer(text):
        for ch in text[position : match.start()]:
            atoms.append((pending_escape + ch, ch, _char_width(ch)))
            pending_escape = ""
        pending_escape += match.group(0)
        position = match.end()
    for ch in text[position:]:
        atoms.append((pending_escape + ch, ch, _char_width(ch)))
        pending_escape = ""
    if not atoms:
        return pending_escape

    if (
        width >= 2
        and atoms[0][1] in ("│", "║")
        and atoms[-1][1] == atoms[0][1]
        and sum(atom_width for _, _, atom_width in atoms) > width
    ):
        left = atoms[0][0]
        right = atoms[-1][0] + pending_escape
        content = "".join(raw for raw, _, _ in atoms[1:-1]).rstrip()
        rows = _wrap_output(content, width - 2).split("\r\n")
        rendered: list[str] = []
        active_style = ""
        for row in rows:
            continued = active_style + row if active_style else row
            active_style = _active_sgr_after(row, active_style)
            rendered.append(
                f"{left}{continued}{' ' * max(0, width - 2 - _visible_width(continued))}{right}"
            )
        return "\r\n".join(rendered)

    lines: list[str] = []
    start = 0
    while start < len(atoms):
        used = 0
        overflow = len(atoms)
        for index in range(start, len(atoms)):
            if used + atoms[index][2] > width:
                overflow = index
                break
            used += atoms[index][2]
        if overflow == len(atoms):
            lines.append("".join(raw for raw, _, _ in atoms[start:]) + pending_escape)
            pending_escape = ""
            break

        whitespace = overflow if atoms[overflow][1].isspace() else None
        if whitespace is None:
            whitespace = next(
                (
                    index
                    for index in range(overflow - 1, start - 1, -1)
                    if atoms[index][1].isspace()
                ),
                None,
            )
        whitespace_start = whitespace
        if whitespace is not None:
            while whitespace_start > start and atoms[whitespace_start - 1][1].isspace():
                whitespace_start -= 1
            if whitespace_start == start:
                whitespace = None
        if whitespace is None:
            end = max(start + 1, overflow)
            lines.append("".join(raw for raw, _, _ in atoms[start:end]))
            start = end
            continue

        whitespace_end = whitespace
        while whitespace_end < len(atoms) and atoms[whitespace_end][1].isspace():
            whitespace_end += 1
        boundary_escapes = "".join(
            raw[: -len(ch)] if ch else raw
            for raw, ch, _ in atoms[whitespace_start:whitespace_end]
        )
        lines.append(
            "".join(raw for raw, _, _ in atoms[start:whitespace_start])
            + boundary_escapes
        )
        start = whitespace_end

    if pending_escape:
        lines[-1] += pending_escape
    return "\r\n".join(lines)


def _char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    if unicodedata.category(ch).startswith("C"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _visible_width(text: str) -> int:
    return sum(_char_width(ch) for ch in ANSI_ESCAPE_RE.sub("", text))


def _active_sgr_after(text: str, active: str) -> str:
    for match in ANSI_ESCAPE_RE.finditer(text):
        sequence = match.group(0)
        if not (sequence.startswith(f"{ESC}[") and sequence.endswith("m")):
            continue
        params = sequence[2:-1].split(";") if sequence[2:-1] else ["0"]
        if "0" in params:
            active = ""
        if any(param and param != "0" for param in params):
            active += sequence
    return active


# Unsupported terminal keys are deliberately non-command strings, never an
# empty string (which is a substring of every menu's key alphabet).
IGNORED_KEY = "<key>"
ESCAPE_KEY = "<esc>"
_INPUT_TIMEOUT = 0.15


class _StdioBytes:
    """Unbuffered byte reads with a short timeout only for partial keys.

    POSIX select supports door pipes. Windows select does not; PeekNamedPipe
    checks the inherited stdin pipe without a background reader/thread.
    """

    def __init__(self, stream):
        self.stream = stream
        try:
            self.fd = stream.fileno()
        except (AttributeError, OSError):
            self.fd = None  # in-memory scripted terminal
        self.kernel = None
        if os.name == "nt" and self.fd is not None:
            import ctypes
            import msvcrt
            from ctypes import wintypes

            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.GetFileType.argtypes = [wintypes.HANDLE]
            self.kernel.GetFileType.restype = wintypes.DWORD
            self.kernel.PeekNamedPipe.argtypes = [
                wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
            ]
            self.kernel.PeekNamedPipe.restype = wintypes.BOOL
            self.handle = msvcrt.get_osfhandle(self.fd)

    def __call__(self, timeout: float | None) -> bytes | None:
        if self.fd is None:
            return self.stream.read(1)
        if timeout is not None:
            if self.kernel is None:
                import select

                if not select.select([self.fd], [], [], timeout)[0]:
                    return None
            else:
                import ctypes
                import msvcrt
                from ctypes import wintypes

                deadline = time.monotonic() + timeout
                kind = self.kernel.GetFileType(self.handle)
                while True:
                    if kind == 1:  # disk: read/EOF never waits for a key
                        break
                    if kind == 2 and msvcrt.kbhit():  # console
                        break
                    if kind == 3:  # inherited anonymous/named pipe
                        available = wintypes.DWORD()
                        if not self.kernel.PeekNamedPipe(self.handle, None, 0, None,
                                                        ctypes.byref(available), None):
                            error = ctypes.get_last_error()
                            if error in (109, 232):  # broken/closed pipe
                                return b""
                            raise ctypes.WinError(error)
                        if available.value:
                            break
                    if time.monotonic() >= deadline:
                        return None
                    time.sleep(min(0.005, max(0, deadline - time.monotonic())))
        return os.read(self.fd, 1)


class _DoorInput:
    """A bounded UTF-8/terminal-key decoder shared by every input screen.

    Partial sequences survive timeouts, so a delayed arrow suffix cannot become
    a menu command. Control strings and bracketed paste are discarded through
    their terminator, retaining only enough bytes to recognize that terminator.
    """

    def __init__(self, read_byte):
        self.read_byte = read_byte
        self.mode = "text"
        self.sequence = bytearray()
        self.utf8 = bytearray()
        self.utf8_size = 0
        self.pending = None
        self.skip_lf = False
        self.after_timeout = False
        self.mouse_remaining = 0
        self.string_bel = False

    def read_key(self) -> str:
        for _ in range(256):
            if self.pending is not None:
                byte, self.pending = self.pending, None
            else:
                timeout = _INPUT_TIMEOUT if not self.after_timeout and (self.mode != "text" or self.utf8) else None
                byte = self.read_byte(timeout)
            if byte is None:
                self.after_timeout = True
                if self.mode == "escape":
                    self.mode = "late_escape"
                    return ESCAPE_KEY
                return IGNORED_KEY
            if not byte:
                raise EOFError("stdin closed")
            self.after_timeout = False
            n = byte[0]
            if self.mouse_remaining:
                self.mouse_remaining -= 1
                if not self.mouse_remaining:
                    self.mode = "text"
                    return IGNORED_KEY
                continue
            if self.mode == "paste":
                self.sequence.extend(byte)
                del self.sequence[:-6]
                if self.sequence.endswith((b"\x1b[201~", b"\x9b201~")):
                    self.mode = "text"
                    self.sequence.clear()
                    return IGNORED_KEY
                continue
            if self.mode in ("string", "string_escape"):
                if (self.mode == "string_escape" and byte == b"\\") or n == 0x9c or (n == 7 and self.string_bel):
                    self.mode = "text"
                    return IGNORED_KEY
                self.mode = "string_escape" if n == 27 else "string"
                continue
            if self.mode in ("escape", "late_escape"):
                late = self.mode == "late_escape"
                self.mode = "text"
                if byte in (b"[", b"O"):
                    self.mode = "csi" if byte == b"[" else "ss3"
                    self.sequence.clear()
                    continue
                if not late and byte in (b"]", b"P", b"X", b"^", b"_"):
                    self.mode = "string"
                    self.string_bel = byte == b"]"
                    continue
                if 0x20 <= n <= 0x2f:
                    self.mode = "intermediate"
                    continue
                # After Escape was reported as a standalone key, only the
                # CSI/SS3 suffixes above remain reserved for delayed arrows.
                # P/X must be independent hotkeys, not open-ended strings.
                if not late:
                    # Alt+printable is a single unsupported key, not a command.
                    # Decode the entire UTF-8 character before discarding it.
                    self.mode = "alt"
            if self.mode in ("csi", "ss3", "intermediate"):
                if n == 27:
                    self.mode = "escape"
                    continue
                if self.mode == "csi" and len(self.sequence) < 8:
                    self.sequence.extend(byte)
                if self.mode == "csi" and self.sequence == b"[":
                    self.mode = "ss3"  # Linux-console ESC [[ A through E
                    continue
                if self.mode == "csi" and self.sequence == b"M":
                    self.mode = "mouse"
                    self.mouse_remaining = 3  # legacy X10 mouse coordinates
                    self.sequence.clear()
                    continue
                if 0x40 <= n <= 0x7e or (self.mode == "intermediate" and 0x30 <= n <= 0x7e):
                    self.mode = "paste" if self.mode == "csi" and self.sequence == b"200~" else "text"
                    self.sequence.clear()
                    if self.mode == "text":
                        return IGNORED_KEY
                elif not 0x20 <= n <= 0x3f:
                    self.mode = "text"
                    return IGNORED_KEY
                continue
            if self.utf8:
                if not 0x80 <= n <= 0xbf:
                    self.pending = byte
                    self.utf8.clear()
                    self.mode = "text"
                    return IGNORED_KEY
                self.utf8.extend(byte)
                if len(self.utf8) < self.utf8_size:
                    continue
                try:
                    char = self.utf8.decode("utf-8")
                except UnicodeDecodeError:
                    char = IGNORED_KEY
                self.utf8.clear()
            elif n == 27:
                self.mode = "escape"
                continue
            elif n in (0x9b, 0x8f):
                self.mode = "csi" if n == 0x9b else "ss3"
                self.sequence.clear()
                continue
            elif n in (0x90, 0x98, 0x9d, 0x9e, 0x9f):
                self.mode = "string"
                self.string_bel = n == 0x9d
                continue
            elif n >= 0x80:
                self.utf8_size = 2 if 0xc2 <= n <= 0xdf else 3 if 0xe0 <= n <= 0xef else 4 if 0xf0 <= n <= 0xf4 else 0
                if not self.utf8_size:
                    self.mode = "text"
                    return IGNORED_KEY
                self.utf8.extend(byte)
                continue
            else:
                char = chr(n)
            if self.mode == "alt":
                self.mode = "text"
                return IGNORED_KEY
            if self.skip_lf:
                self.skip_lf = False
                if char == "\n":
                    continue
            self.skip_lf = char == "\r"
            if char in ("\r", "\n", "\x7f", "\x08") or char.isprintable():
                return char
            return IGNORED_KEY
        return IGNORED_KEY


_INPUT_READER = None
_INPUT_STREAM = None


def read_key() -> str:
    """Read one decoded character or one harmless unsupported terminal key."""
    global _INPUT_READER, _INPUT_STREAM
    stream = sys.stdin.buffer
    if _INPUT_STREAM is not stream:
        _INPUT_STREAM = stream
        _INPUT_READER = _DoorInput(_StdioBytes(stream))
    return _INPUT_READER.read_key()


def read_command() -> str:
    """Match displayed ASCII hotkeys without Unicode case-fold aliases."""
    key = read_key()
    return key.upper() if len(key) == 1 and key.isascii() else IGNORED_KEY


def read_line_raw(max_len: int = 20, allowed=lambda c: "0" <= c <= "9") -> str:
    """Edit bounded Unicode text; numeric fields accept ASCII decimal digits.

    Both codepoint count and display width are bounded. Backspace removes a
    base character with its combining marks, erasing its actual screen columns.
    """
    buf: list[str] = []
    while True:
        ch = read_key()
        if ch in ("\r", "\n"):
            out_line()
            return unicodedata.normalize("NFC", "".join(buf))
        if ch in ("\x7f", "\x08"):
            if buf:
                removed = buf.pop()
                while unicodedata.combining(removed[0]) and buf:
                    removed = buf.pop() + removed
                out("\x08 \x08" * sum(_char_width(c) for c in removed))
            continue
        if len(ch) != 1 or not ch.isprintable() or not allowed(ch):
            continue
        if unicodedata.combining(ch) and not buf:
            continue
        if len(buf) >= max_len or sum(_char_width(c) for c in buf) + _char_width(ch) > max_len:
            continue
        buf.append(ch)
        out(ch)


def confirm(prompt: str, p: Palette) -> bool:
    out_prompt(f"{p.muted}{prompt} [Y/N] {RESET}")
    while True:
        key = read_command()
        if key == "Y":
            out_line("Y")
            return True
        if key == "N":
            out_line("N")
            return False


def pause(p: Palette, msg: str = "Press any key to continue...") -> None:
    out_prompt(f"{p.muted}{msg}{RESET}")
    while read_key() in (IGNORED_KEY, ESCAPE_KEY):
        pass
    out_line()


# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

GALAXY_SYSTEM_COUNT = 48

# Cap for Pilot.highlights (see Pilot.highlight) -- bounds an extremely
# long career's save file size without ever summarizing the record down
# to "recent," which is `log`'s own job.
MAX_HIGHLIGHTS = 40

ECONOMIES = ["Agricultural", "Industrial", "Mining", "Tech", "Haven"]
ECONOMY_WEIGHTS = [30, 25, 25, 15, 5]
ECONOMY_BASE_DANGER = {"Agricultural": 1, "Industrial": 1, "Mining": 2, "Tech": 1, "Haven": 3}

FACTION_CONCORD = "concord"
FACTION_BLACKWAKE = "blackwake"
FACTIONS = (FACTION_CONCORD, FACTION_BLACKWAKE)
FACTION_LABEL = {FACTION_CONCORD: "Concord Navy", FACTION_BLACKWAKE: "Blackwake Cartel"}

# (label, base price, legal)
COMMODITIES: dict[str, dict] = {
    "food": {"label": "Food", "base": 12, "legal": True},
    "textiles": {"label": "Textiles", "base": 18, "legal": True},
    "machinery": {"label": "Machinery", "base": 65, "legal": True},
    "electronics": {"label": "Electronics", "base": 110, "legal": True},
    "ore": {"label": "Raw Ore", "base": 30, "legal": True},
    "metals": {"label": "Refined Metals", "base": 75, "legal": True},
    "medicine": {"label": "Medicine", "base": 95, "legal": True},
    "weapons": {"label": "Weapons", "base": 140, "legal": False},
    "narcotics": {"label": "Narcotics", "base": 200, "legal": False},
}
LEGAL_COMMODITIES = [c for c, v in COMMODITIES.items() if v["legal"]]
CONTRABAND_COMMODITIES = [c for c, v in COMMODITIES.items() if not v["legal"]]

ECONOMY_PRODUCES = {
    "Agricultural": ["food", "textiles"],
    "Industrial": ["machinery", "metals"],
    "Mining": ["ore", "metals"],
    "Tech": ["electronics", "medicine"],
    "Haven": ["weapons", "narcotics"],
}
ECONOMY_DEMANDS = {
    "Agricultural": ["machinery", "electronics"],
    "Industrial": ["food", "electronics"],
    "Mining": ["food", "machinery"],
    "Tech": ["ore", "metals"],
    "Haven": ["food", "medicine"],
}
SELL_SPREAD = 0.92  # selling always pays a shade under the buy price

# upgrade key -> (label, max tier, cost(current_tier)->credits, effect blurb)
UPGRADES: dict[str, dict] = {
    "cargo": {"label": "Cargo Bay Expansion", "max_tier": 5, "cost": lambda t: 800 + t * 600,
              "effect": "+8 cargo capacity"},
    "engine": {"label": "Engine Tuning", "max_tier": 3, "cost": lambda t: 1200 + t * 1000,
               "effect": "+8 fuel, +evasion"},
    "weapon": {"label": "Weapon Systems", "max_tier": 4, "cost": lambda t: 1000 + t * 900,
               "effect": "+combat damage"},
    "shield": {"label": "Deflector Shields", "max_tier": 3, "cost": lambda t: 1100 + t * 950,
               "effect": "-incoming damage"},
    "scanner": {"label": "Long-Range Scanner", "max_tier": 2, "cost": lambda t: 900 + t * 700,
                "effect": "+scan range"},
    "hull": {"label": "Hull Reinforcement", "max_tier": 4, "cost": lambda t: 700 + t * 550,
             "effect": "+35 max hull"},
}
# Hired NPC crew (see screen_crew) -- unlike every entry in UPGRADES
# above, this is an ongoing per-jump wage instead of a one-time
# purchase, and each role is a simple binary hired/not-hired switch
# rather than a tier ladder. Keyed to match Ship's own `has_<role>`
# fields exactly.
CREW_ROLES: dict[str, dict] = {
    "gunner": {"label": "Gunner", "hire_cost": 800, "wage": 15, "effect": "+3 combat damage per hit"},
    "engineer": {"label": "Engineer", "hire_cost": 200, "wage": 2, "effect": "-25% fuel, round up (min 1)"},
    "navigator": {"label": "Navigator", "hire_cost": 600, "wage": 10, "effect": "+1 scan range"},
}
# hull class -> base cargo/fuel/hull, before any tier upgrades are added
# on top (cargo_capacity/fuel_capacity/hull_hp_max below still add
# +8/+8/+35 per tier regardless of class -- only the base changes).
# Carrier's own base exceeds both Freighter's and Cutter's in every
# stat on purpose -- it's the unified endgame hull, not a third
# competing tradeoff.
HULL_CLASSES: dict[str, dict] = {
    "Shuttle": {"cargo_base": 24, "fuel_base": 24, "hull_base": 60},
    "Freighter": {"cargo_base": 100, "fuel_base": 40, "hull_base": 140},
    "Cutter": {"cargo_base": 40, "fuel_base": 50, "hull_base": 180},
    "Carrier": {"cargo_base": 160, "fuel_base": 60, "hull_base": 260},
}

# current hull class -> [(target class, refit cost), ...] refits available
# from here. A branching ladder, not a strict sequence: Shuttle refits
# into *either* Freighter (cargo-focused trader build) *or* Cutter
# (hull/fuel-focused fighter build) -- a one-time playstyle commitment,
# not a step everyone takes in the same order -- and either then refits
# into Carrier later, unifying back into one endgame hull. No refit ever
# goes backward, matching the existing "flagship refit" tone this
# generalizes (a permanent commissioning, not a reversible choice).
HULL_REFITS: dict[str, list[tuple[str, int]]] = {
    "Shuttle": [("Freighter", 15_000), ("Cutter", 15_000)],
    "Freighter": [("Carrier", 45_000)],
    "Cutter": [("Carrier", 45_000)],
    "Carrier": [],
}

RANKS = [
    (0, "Rookie Hauler"),
    (5_000, "Independent Trader"),
    (20_000, "Merchant Captain"),
    (75_000, "Void Baron"),
    (250_000, "Legend of the Frontier"),
]

SYSTEM_NAMES = [
    "Aldrin's Reach", "Bastion", "Calyx", "Draven's Drift", "Erebus Point",
    "Farango", "Greywater", "Halcyon", "Ithaca Deep", "Junction",
    "Kestrel", "Lorne", "Meridian", "Nashira", "Obsidian Gate",
    "Perrin's Folly", "Quietus", "Ravensbourne", "Sable Hollow", "Tanager",
    "Umbra", "Verity", "Wraithmoor", "Xanthe", "Yellowstone Deep",
    "Zephyrine", "Ashfall", "Briar Cross", "Coldharbor", "Dustwake",
    "Ember Reach", "Fenwick", "Gallowglass", "Highmarch", "Ironvale",
    "Jettison", "Kelburn", "Lowlight", "Mirrorfall", "Nightgate",
    "Outreach", "Palewell", "Quarrytown", "Redshift", "Stillwater",
    "Threnody", "Undertow", "Vantage Point", "Whitfield", "Yarrow",
    "Zenith Deep", "Aphelion", "Blackglass", "Cindergate", "Driftwood",
]
STATION_SUFFIXES = [
    "Anchorage", "Station", "Yard", "Gate", "Reach", "Terminal",
    "Point", "Freeport", "Outpost", "Exchange",
]
PIRATE_NAMES = [
    "Rust Wraith", "Ashclaw", "Blacktide", "Hollow Fang", "Grimwire",
    "Static Ghost", "Void Jackal", "Cinder Raider", "Nullshade", "Ravage",
]

# What kind of random travel encounter fires, once the overall "does
# anything happen at all" roll already succeeds (screen_travel's own
# 0.08 + danger*0.05 chance, unchanged) -- diversifying travel without
# changing how *often* something happens. Pirate stays the dominant
# outcome on purpose, matching the difficulty this chance was originally
# tuned around; derelict/distress/tip share the remainder roughly evenly.
TRAVEL_ENCOUNTER_WEIGHTS: dict[str, int] = {"pirate": 55, "derelict": 15, "distress": 15, "tip": 15}

# Player notoriety: a "wanted" counter, independent of the two faction
# reputation tracks -- rises from a caught (bribe-refused) customs bust
# or a bounty kill that turns out to be mistaken identity, and gates how
# often a Concord Patrol travel encounter fires. Continuous in the raw
# notoriety count (not a small tier bucket) so the very first bust
# already carries some real risk rather than a dead zone before a
# threshold. Patrol *difficulty* (the intercepting ship's own combat
# tier) still buckets into the same 0-4 range every other tier stat in
# this file uses, for stat-generation consistency with generate_pirate.
NOTORIETY_PER_CUSTOMS_BUST = 2
NOTORIETY_PER_WRONG_BOUNTY_KILL = 2
WRONG_BOUNTY_KILL_CHANCE = 0.12
NOTORIETY_PATROL_CHANCE_PER_POINT = 0.03
NOTORIETY_PATROL_MAX_CHANCE = 0.40
CONCORD_PATROL_NAMES = ["CNS Vigilant", "CNS Warden", "CNS Sentinel", "CNS Bastion", "CNS Arbiter"]


def notoriety_patrol_chance(notoriety: int) -> float:
    return min(NOTORIETY_PATROL_MAX_CHANCE, notoriety * NOTORIETY_PATROL_CHANCE_PER_POINT)


def notoriety_fine_cost(notoriety: int) -> int:
    return 100 + notoriety * 40


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------


@dataclass
class GalaxySystem:
    id: int
    name: str
    x: int
    y: int
    economy: str
    danger: int
    station_name: str
    connections: list[int] = field(default_factory=list)
    discovered: bool = False


@dataclass
class Pilot:
    handle: str
    credits: int
    reputation: dict[str, int]
    missions_completed: int = 0
    kills: int = 0
    career_started: str = ""
    log: list[str] = field(default_factory=list)
    # How "wanted" the pilot currently is with Concord -- rises from a
    # caught (bribe-refused) customs bust or a bounty kill that turns out
    # to have been a mistaken identity, and is the only thing that gates
    # Concord Patrol travel encounters (see notoriety_patrol_chance).
    # Additive field, safe default for every pre-existing save via
    # from_dict's own .get() below -- no SCHEMA_VERSION bump needed.
    notoriety: int = 0
    # How many times this pilot has retired (New Game+, see
    # retire_pilot) -- the one field a retirement carries forward into
    # the otherwise brand-new career that replaces it. Additive field,
    # safe default for every pre-existing save via from_dict's own
    # .get() below -- no SCHEMA_VERSION bump needed.
    retirements: int = 0
    # A permanent milestone record -- first kill, first mission, rank
    # promotions, ship upgrades, landmark finds, retirements -- distinct
    # from `log`'s own rolling 8-entry buffer, which can't answer "what
    # did I ever actually do" once anything scrolls off it. Capped (see
    # `highlight()`) only to bound an extremely long career's save file
    # size, not to summarize down to "recent." Additive field, safe
    # default via from_dict's own .get() below -- no SCHEMA_VERSION
    # bump needed.
    highlights: list[str] = field(default_factory=list)
    # The highest RANKS index this pilot has already been credited a
    # promotion highlight for -- see check_rank_up. Additive field, safe
    # default via from_dict's own .get() below -- no SCHEMA_VERSION
    # bump needed.
    highest_rank_seen: int = 0
    # Faction endgame arcs (see concord_commission_available/
    # blackwake_made_available) -- each a one-time-unlockable, lifelong
    # perk at high standing with its faction, not a repeatable mission.
    # Additive fields, safe default via from_dict's own .get() below --
    # no SCHEMA_VERSION bump needed.
    has_concord_commission: bool = False
    has_blackwake_made: bool = False

    def note(self, msg: str) -> None:
        self.log.append(msg)
        del self.log[:-8]

    def highlight(self, msg: str) -> None:
        self.highlights.append(msg)
        del self.highlights[:-MAX_HIGHLIGHTS]

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Pilot":
        return cls(
            handle=d["handle"], credits=d["credits"], reputation=dict(d["reputation"]),
            missions_completed=d.get("missions_completed", 0), kills=d.get("kills", 0),
            career_started=d.get("career_started", ""), log=list(d.get("log", [])),
            notoriety=d.get("notoriety", 0), retirements=d.get("retirements", 0),
            highlights=list(d.get("highlights", [])), highest_rank_seen=d.get("highest_rank_seen", 0),
            has_concord_commission=d.get("has_concord_commission", False),
            has_blackwake_made=d.get("has_blackwake_made", False),
        )


@dataclass
class Ship:
    hull_class: str
    hull_hp: int
    fuel: int
    cargo_tier: int = 0
    engine_tier: int = 0
    weapon_tier: int = 0
    shield_tier: int = 0
    scanner_tier: int = 0
    hull_tier: int = 0
    # Hired NPC crew (see CREW_ROLES/screen_crew) -- an ongoing per-jump
    # wage instead of a one-time upgrade purchase, unlike every other
    # field on this dataclass. Additive fields; `Ship.from_dict`'s own
    # generic reconstruction (only passes keys present in the loaded
    # dict) already defaults every pre-existing save to False with no
    # further change needed there.
    has_gunner: bool = False
    has_engineer: bool = False
    has_navigator: bool = False

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Ship":
        return cls(**{f.name: d[f.name] for f in dataclasses.fields(cls) if f.name in d})


def cargo_capacity(ship: Ship) -> int:
    return HULL_CLASSES[ship.hull_class]["cargo_base"] + ship.cargo_tier * 8


def fuel_capacity(ship: Ship) -> int:
    return HULL_CLASSES[ship.hull_class]["fuel_base"] + ship.engine_tier * 8


def hull_hp_max(ship: Ship) -> int:
    return HULL_CLASSES[ship.hull_class]["hull_base"] + ship.hull_tier * 35


@dataclass
class Mission:
    id: int
    kind: str  # "delivery" | "bounty" | "scan"
    description: str
    reward: int
    origin_system: int
    target_system: int
    commodity: str | None = None
    quantity: int | None = None
    deadline_turn: int | None = None
    pirate_tier: int | None = None
    opening_assignment: bool = False

    def to_dict(self) -> dict:
        data = dataclasses.asdict(self)
        if self.opening_assignment is False:
            del data["opening_assignment"]
        return data

    @classmethod
    def from_dict(cls, d: dict) -> "Mission":
        return cls(**{f.name: d[f.name] for f in dataclasses.fields(cls) if f.name in d})


@dataclass
class FuturesContract:
    """Prepaid goods for station pickup after maturity.

    Missing pickup metadata identifies old remote-delivery orders; preserve
    their original settlement terms until consumed.
    """
    id: int
    commodity: str
    quantity: int
    locked_price: int  # total paid up front, including brokerage fee
    settle_turn: int
    origin_system: int | None = None  # None: preserve legacy remote settlement.
    principal: int | None = None

    def to_dict(self) -> dict:
        data = dataclasses.asdict(self)
        if self.origin_system is None and self.principal is None:
            # Legacy orders retain absent metadata when checkpointed again.
            del data["origin_system"]
            del data["principal"]
        return data

    @classmethod
    def from_dict(cls, d: dict) -> "FuturesContract":
        contract = cls(**{f.name: d[f.name] for f in dataclasses.fields(cls) if f.name in d})
        if "origin_system" in d or "principal" in d:
            if (type(contract.origin_system) is not int or not 0 <= contract.origin_system < GALAXY_SYSTEM_COUNT
                    or type(contract.principal) is not int or type(contract.locked_price) is not int
                    or not 0 <= contract.principal <= contract.locked_price
                    or type(contract.quantity) is not int or contract.quantity < 1
                    or type(contract.settle_turn) is not int or contract.settle_turn < 0
                    or not isinstance(contract.commodity, str) or contract.commodity not in COMMODITIES):
                raise ResumeError("The saved futures pickup terms cannot be read.")
        return contract


class ResumeError(Exception):
    """An interrupted career must be preserved, never reset automatically."""


def _reject_unknown_save_fields(value, fields: set[str], label: str) -> None:
    if isinstance(value, dict) and set(value) - fields:
        raise UnsupportedSave(f"The saved {label} contains unsupported fields.")


def _validate_combat_mission_snapshot(data: dict, kind: str) -> None:
    """Validate every value a resumed fight, payout, or removal consumes."""
    if not isinstance(data, dict):
        raise ValueError("invalid mission snapshot")
    _reject_unknown_save_fields(data, {f.name for f in dataclasses.fields(Mission)}, "mission snapshot")
    mission = Mission.from_dict(data)
    if type(mission.opening_assignment) is not bool or (mission.opening_assignment and kind != "delivery"):
        raise ValueError("invalid opening assignment")
    if (mission.kind != kind or not isinstance(mission.description, str)
            or type(mission.id) is not int or mission.id < 1
            or type(mission.reward) is not int or mission.reward < 0):
        raise ValueError("invalid mission identity or reward")
    for system in (mission.origin_system, mission.target_system):
        if type(system) is not int or not 0 <= system < GALAXY_SYSTEM_COUNT:
            raise ValueError("invalid mission system")
    if mission.pirate_tier is not None and (
            type(mission.pirate_tier) is not int or not 0 <= mission.pirate_tier <= 4):
        raise ValueError("invalid mission pirate tier")
    if mission.commodity is not None and (
            not isinstance(mission.commodity, str) or mission.commodity not in COMMODITIES):
        raise ValueError("invalid mission commodity")
    for number in (mission.quantity, mission.deadline_turn):
        if number is not None and (type(number) is not int or number < 0):
            raise ValueError("invalid mission quantity or deadline")


def _load_pending_travel(value: dict | None) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        raise ResumeError("This interrupted journey has an invalid format version.")
    if value["version"] != 1:
        raise UnsupportedSave("This interrupted journey uses an unsupported format.")
    _reject_unknown_save_fields(value, {"version", "origin", "destination", "escort_index", "phase", "primary",
                                       "encounter", "escorts", "destroyed", "was_discovered", "bounty"}, "journey")
    try:
        if any(type(value[key]) is not int for key in ("origin", "destination", "escort_index")):
            raise ValueError("invalid journey position")
        if (value["phase"] not in ("primary", "escorts", "arrival", "customs")
                or value["primary"] not in ("bounty", "patrol", "random")
                or not isinstance(value["encounter"], dict)
                or not isinstance(value["escorts"], list)
                or not isinstance(value["destroyed"], bool)
                or not isinstance(value["was_discovered"], bool)
                or not 0 <= value["escort_index"] <= len(value["escorts"])
                or not 0 <= value["origin"] < GALAXY_SYSTEM_COUNT
                or not 0 <= value["destination"] < GALAXY_SYSTEM_COUNT):
            raise ValueError("invalid journey")
        for mission in value["escorts"]:
            _validate_combat_mission_snapshot(mission, "escort")
        if value["primary"] == "bounty":
            _validate_combat_mission_snapshot(value["bounty"], "bounty")
        state = value["encounter"]
        _reject_unknown_save_fields(state, {"inspect", "done", "kind", "pirates", "index", "pirate", "ambush",
                                           "combat", "result"}, "encounter")
        if value["phase"] == "customs" and not isinstance(state["inspect"], bool):
            raise ValueError("invalid inspection")
        if "done" in state and not isinstance(state["done"], bool):
            raise ValueError("invalid completion")
        if "kind" in state and state["kind"] not in ("none", "pirate", "derelict", "distress", "tip"):
            raise ValueError("invalid encounter")
        pirates = []
        if "pirates" in state:
            if not isinstance(state["pirates"], list) or not 1 <= len(state["pirates"]) <= SQUADRON_SIZE:
                raise ValueError("invalid squadron")
            if type(state["index"]) is not int or not 0 <= state["index"] <= len(state["pirates"]):
                raise ValueError("invalid squadron position")
            pirates.extend(state["pirates"])
        elif state.get("kind") == "pirate":
            raise ValueError("missing squadron")
        for key in ("pirate", "ambush"):
            if key in state:
                pirates.append(state[key])
        if "combat" in state:
            combat = state["combat"]
            _reject_unknown_save_fields(combat, {"pirate", "outcome", "lines"}, "combat")
            pirates.append(combat["pirate"])
            if (combat["outcome"] not in (None, "won", "escaped", "destroyed")
                    or not isinstance(combat["lines"], list)
                    or not all(isinstance(line, str) for line in combat["lines"])):
                raise ValueError("invalid combat")
        for data in pirates:
            _reject_unknown_save_fields(data, {f.name for f in dataclasses.fields(Pirate)}, "opponent")
            pirate = Pirate(**data)
            if (any(type(data[key]) is not int for key in ("tier", "hp", "hp_max"))
                    or not isinstance(pirate.name, str) or not 0 <= pirate.tier <= 4
                    or not 0 <= pirate.hp <= pirate.hp_max or pirate.hp_max <= 0):
                raise ValueError("invalid opponent")
        if "result" in state and (not isinstance(state["result"], list)
                                  or not all(isinstance(line, str) for line in state["result"])):
            raise ValueError("invalid encounter result")
    except (KeyError, TypeError, ValueError) as exc:
        raise ResumeError("The interrupted journey cannot be read.") from exc
    return value


@dataclass
class TradingLedger:
    since_day: int | None = None
    sales_revenue: int = 0
    sales_cost: int = 0
    uncosted_sales: int = 0
    delivery_revenue: int = 0
    delivery_cost: int = 0
    uncosted_deliveries: int = 0
    cargo_loss_cost: int = 0
    uncosted_losses: int = 0
    fuel_spend: int = 0
    wages: int = 0
    cancelled_fees: int = 0


@dataclass
class SaveData:
    schema_version: int
    seed: int
    pilot: Pilot
    ship: Ship
    current_system: int
    turn: int
    cargo: dict[str, int]
    discovered: list[int]
    market_drift: dict[int, dict[str, float]]
    active_missions: list[Mission]
    next_mission_id: int
    flags: dict[str, bool]
    # At most one galaxy-wide economy event active at a time (see
    # tick_economy_event) -- a plain dict rather than its own dataclass,
    # matching `flags`'s own precedent for simple additive save state.
    # Additive field, safe default via from_dict's own .get() below --
    # no SCHEMA_VERSION bump needed.
    active_event: dict | None = None
    # Outstanding futures contracts (see FuturesContract/settle_futures_
    # contracts) -- additive field, safe default via from_dict's own
    # .get() below -- no SCHEMA_VERSION bump needed.
    active_futures: list[FuturesContract] = field(default_factory=list)
    next_futures_id: int = 1
    # Additive state: old careers start docked, with a fresh event RNG.
    # The galaxy generator has its own independent, unchanged RNG.
    pending_travel: dict | None = None
    event_rng_state: list | tuple | None = None
    mission_boards: dict[int, dict] = field(default_factory=dict)
    tracked_mission_id: int | None = None
    contraband_trade_balance: int = 0
    contraband_trade_milestones: int = 0
    best_credits: int = 0
    galaxy_version: int = 1
    # Each known FIFO lot is [remaining quantity, remaining total paid cost].
    # Any hold quantity without a lot is older cargo of unknown acquisition cost.
    cargo_basis: dict[str, list[list[int]]] = field(default_factory=dict)
    trading_ledger: TradingLedger = field(default_factory=TradingLedger)
    market_memory: dict[int, dict[str, dict]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "galaxy_version": self.galaxy_version,
            "seed": self.seed,
            "pilot": self.pilot.to_dict(),
            "ship": self.ship.to_dict(),
            "current_system": self.current_system,
            "turn": self.turn,
            "cargo": self.cargo,
            "discovered": self.discovered,
            "market_drift": {str(k): v for k, v in self.market_drift.items()},
            "active_missions": [m.to_dict() for m in self.active_missions],
            "next_mission_id": self.next_mission_id,
            "flags": self.flags,
            "active_event": self.active_event,
            "active_futures": [f.to_dict() for f in self.active_futures],
            "next_futures_id": self.next_futures_id,
            "pending_travel": self.pending_travel,
            "event_rng_state": self.event_rng_state,
            "mission_boards": {str(k): v for k, v in self.mission_boards.items()},
            "tracked_mission_id": self.tracked_mission_id,
            "contraband_trade_balance": self.contraband_trade_balance,
            "contraband_trade_milestones": self.contraband_trade_milestones,
            "best_credits": self.best_credits,
            "cargo_basis": self.cargo_basis,
            "trading_ledger": dataclasses.asdict(self.trading_ledger),
            "market_memory": {str(sid): quotes for sid, quotes in self.market_memory.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SaveData":
        try:
            _validate_save_document(d)
        except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ResumeError("The saved career contains invalid fields.") from exc
        return cls(
            schema_version=d["schema_version"],
            galaxy_version=d.get("galaxy_version", 1),
            seed=d["seed"],
            pilot=Pilot.from_dict(d["pilot"]),
            ship=Ship.from_dict(d["ship"]),
            current_system=d["current_system"],
            turn=d["turn"],
            cargo=dict(d["cargo"]),
            discovered=list(d["discovered"]),
            market_drift={int(k): v for k, v in d.get("market_drift", {}).items()},
            active_missions=[Mission.from_dict(m) for m in d.get("active_missions", [])],
            next_mission_id=d.get("next_mission_id", 1),
            flags=dict(d.get("flags", {})),
            active_event=d.get("active_event"),
            active_futures=[FuturesContract.from_dict(f) for f in d.get("active_futures", [])],
            next_futures_id=d.get("next_futures_id", 1),
            pending_travel=_load_pending_travel(d.get("pending_travel")),
            event_rng_state=d.get("event_rng_state"),
            mission_boards=_load_mission_boards(d.get("mission_boards", {})),
            tracked_mission_id=_load_tracked_mission_id(d.get("tracked_mission_id")),
            best_credits=_load_trade_total(d.get("best_credits", 0), nonnegative=True, label="credit high-water mark"),
            contraband_trade_balance=_load_trade_total(d.get("contraband_trade_balance", 0)),
            contraband_trade_milestones=_load_trade_total(d.get("contraband_trade_milestones", 0), nonnegative=True),
            cargo_basis={c: [list(lot) for lot in lots] for c, lots in d.get("cargo_basis", {}).items()},
            trading_ledger=TradingLedger(**d.get("trading_ledger", {})),
            market_memory={int(sid): {c: dict(q) for c, q in quotes.items()}
                           for sid, quotes in d.get("market_memory", {}).items()},
        )


class UnsupportedSave(ResumeError):
    """Use a compatible game build; do not offer a potentially destructive downgrade."""


def _validate_save_document(data: dict) -> None:
    """Validate before coercion can hide wrong types or discard future fields."""
    def require(ok, field):
        if not ok:
            raise ResumeError(f"The saved {field} is invalid.")

    def integer(value, field, minimum=0, maximum=2**63 - 1):
        require(type(value) is int and minimum <= value <= maximum, field)

    def record(value, cls, field):
        require(isinstance(value, dict), field)
        known = {f.name for f in dataclasses.fields(cls)}
        if set(value) - known:
            raise UnsupportedSave(f"The saved {field} contains unsupported fields.")
        for f in dataclasses.fields(cls):
            legacy_defaults = {"market_drift", "active_missions", "next_mission_id", "flags"} if cls is SaveData else set()
            if (f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
                    and f.name not in legacy_defaults):
                require(f.name in value, field)

    def text(value, field):
        require(isinstance(value, str) and len(value) <= 4096, field)
        require(all(ord(c) >= 32 and not 127 <= ord(c) <= 159 for c in value), field)

    def system(value, field):
        integer(value, field, maximum=GALAXY_SYSTEM_COUNT - 1)

    record(data, SaveData, "career")
    for key, expected in (("schema_version", SCHEMA_VERSION), ("galaxy_version", 1)):
        if type(data.get(key, expected)) is not int or data.get(key, expected) != expected:
            raise UnsupportedSave("This career uses an unsupported save or galaxy version.")
    integer(data["seed"], "galaxy seed", minimum=-(2**63))
    system(data["current_system"], "current system")
    integer(data["turn"], "day")
    for key in ("next_mission_id", "next_futures_id"):
        integer(data.get(key, 1), key, minimum=1)
    for key in ("best_credits", "contraband_trade_milestones"):
        integer(data.get(key, 0), key)
    integer(data.get("contraband_trade_balance", 0), "trade balance", minimum=-(2**63))
    pilot, ship = data["pilot"], data["ship"]
    record(pilot, Pilot, "pilot")
    text(pilot["handle"], "callsign")
    text(pilot.get("career_started", ""), "career date")
    for key in ("credits", "missions_completed", "kills", "retirements"):
        integer(pilot.get(key, 0), key)
    integer(pilot.get("notoriety", 0), "notoriety")
    integer(pilot.get("highest_rank_seen", 0), "rank", maximum=len(RANKS) - 1)
    for key in ("has_concord_commission", "has_blackwake_made"):
        require(type(pilot.get(key, False)) is bool, key)
    for key in ("log", "highlights"):
        require(isinstance(pilot.get(key, []), list), key)
        for line in pilot.get(key, []):
            text(line, key)
    reputation = pilot["reputation"]
    require(isinstance(reputation, dict) and set(reputation) <= set(FACTIONS), "faction reputation")
    for value in reputation.values():
        integer(value, "faction reputation", minimum=-100, maximum=100)
    record(ship, Ship, "ship")
    require(isinstance(ship["hull_class"], str) and ship["hull_class"] in HULL_CLASSES, "hull class")
    for key, upgrade in UPGRADES.items():
        integer(ship.get(key + "_tier", 0), key + " tier", maximum=upgrade["max_tier"])
    for key in ("has_gunner", "has_engineer", "has_navigator"):
        require(type(ship.get(key, False)) is bool, key)
    vessel = Ship.from_dict(ship)
    integer(ship["fuel"], "fuel", maximum=fuel_capacity(vessel))
    integer(ship["hull_hp"], "hull health", maximum=hull_hp_max(vessel))
    cargo = data["cargo"]
    require(isinstance(cargo, dict) and set(cargo) <= set(COMMODITIES), "cargo")
    for value in cargo.values():
        integer(value, "cargo quantity")
    require(sum(cargo.values()) <= cargo_capacity(vessel), "cargo capacity")
    basis = data.get("cargo_basis", {})
    require(isinstance(basis, dict) and set(basis) <= set(cargo), "cargo acquisition costs")
    for commodity, lots in basis.items():
        require(isinstance(lots, list) and 0 < len(lots) <= cargo[commodity], "cargo lots")
        for lot in lots:
            require(isinstance(lot, list) and len(lot) == 2, "cargo lot")
            integer(lot[0], "costed quantity", minimum=1)
            integer(lot[1], "acquisition cost")
        require(sum(lot[0] for lot in lots) <= cargo[commodity], "costed cargo quantity")
    ledger = data.get("trading_ledger", {})
    record(ledger, TradingLedger, "trading ledger")
    for key, value in ledger.items():
        if key == "since_day":
            if value is not None:
                integer(value, "ledger start day", maximum=data["turn"])
        else:
            integer(value, "ledger " + key)
    if basis or any(value for key, value in ledger.items() if key != "since_day"):
        require(ledger.get("since_day") is not None, "ledger start day")
    memory = data.get("market_memory", {})
    require(isinstance(memory, dict) and len(memory) <= GALAXY_SYSTEM_COUNT, "market memory")
    memory_ids = set()
    for key, quotes in memory.items():
        require(isinstance(key, str) and key.isascii() and key.isdecimal(), "remembered station")
        sid = int(key)
        system(sid, "remembered station")
        require(sid not in memory_ids, "duplicate remembered station")
        memory_ids.add(sid)
        require(isinstance(quotes, dict) and set(quotes) <= set(COMMODITIES), "remembered commodities")
        for quote in quotes.values():
            require(isinstance(quote, dict), "remembered quote")
            _reject_unknown_save_fields(quote, {"day", "buy", "sell"}, "remembered quote")
            require(set(quote) == {"day", "buy", "sell"}, "remembered quote")
            integer(quote["day"], "quote observation day", maximum=data["turn"])
            if quote["buy"] is not None:
                integer(quote["buy"], "remembered buy price", minimum=1)
            integer(quote["sell"], "remembered sale price", minimum=1)
    discovered = data["discovered"]
    require(isinstance(discovered, list) and len(discovered) <= GALAXY_SYSTEM_COUNT, "chart")
    for sid in discovered:
        system(sid, "chart system")
    require(len(discovered) == len(set(discovered)), "chart systems")
    require(memory_ids <= set(discovered), "market observations outside the chart")
    drift = data.get("market_drift", {})
    require(isinstance(drift, dict), "market drift")
    seen = set()
    for key, table in drift.items():
        require(isinstance(key, str) and key.isascii() and key.isdecimal(), "market system")
        sid = int(key)
        system(sid, "market system")
        require(sid not in seen, "duplicate market system")
        seen.add(sid)
        require(isinstance(table, dict) and set(table) <= set(COMMODITIES), "market commodities")
        for value in table.values():
            require(type(value) in (int, float) and math.isfinite(value) and 0 < value <= 10, "market price")
    flags = data.get("flags", {})
    require(isinstance(flags, dict) and all(isinstance(k, str) and type(v) is bool for k, v in flags.items()), "flags")
    boards = data.get("mission_boards", {})
    _load_mission_boards(boards)
    posted = []
    for board in boards.values():
        if set(board) != {"refresh_turn", "offers"}:
            raise UnsupportedSave("The saved contract board contains unsupported fields.")
        posted.extend(board["offers"])
    for key, cls in (("active_missions", Mission), ("active_futures", FuturesContract), ("posted", Mission)):
        records = posted if key == "posted" else data.get(key, [])
        require(isinstance(records, list), key)
        for item in records:
            record(item, cls, key)
            integer(item["id"], "contract ID", minimum=1)
            if cls is Mission:
                require(not item.get("opening_assignment") or key == "active_missions", "opening assignment placement")
                require(item["kind"] in ("delivery", "scan", "bounty", "escort"), "contract kind")
                text(item["description"], "contract description")
                integer(item["reward"], "contract reward")
                if item.get("deadline_turn") is not None:
                    integer(item["deadline_turn"], "contract deadline")
                _validate_combat_mission_snapshot(item, item["kind"])
                if item["kind"] == "delivery":
                    require(item.get("commodity") in COMMODITIES, "delivery goods")
                    integer(item.get("quantity"), "delivery quantity", minimum=1)
            else:
                require(isinstance(item["commodity"], str) and item["commodity"] in COMMODITIES, "order goods")
                integer(item["quantity"], "order quantity", minimum=1)
                integer(item["locked_price"], "order payment")
                integer(item["settle_turn"], "order maturity")
    opening_jobs = [item for item in data.get("active_missions", []) if item.get("opening_assignment") is True]
    require(len(opening_jobs) <= 1, "opening assignment count")
    if opening_jobs:
        require(flags.get("opening_assignment_taken") is True and not flags.get("opening_assignment_completed"),
                "opening assignment progress")
    if flags.get("opening_assignment_completed"):
        require(flags.get("opening_assignment_taken") is True, "opening assignment progress")
    event = data.get("active_event")
    if event is not None:
        event_fields = {"economy", "commodity", "direction", "turns_remaining", "description"}
        require(isinstance(event, dict), "economy event")
        if set(event) - event_fields:
            raise UnsupportedSave("The saved economy event contains unsupported fields.")
        require(set(event) == event_fields, "economy event")
        require(event["economy"] in ECONOMIES and event["commodity"] in COMMODITIES and
                event["direction"] in ("boom", "crash"), "economy event")
        integer(event["turns_remaining"], "event duration", minimum=1, maximum=ECONOMY_EVENT_MAX_TURNS)
        text(event["description"], "economy news")


class World:
    """The whole live game session: the regenerated (never persisted)
    galaxy, plus the persisted `SaveData`. Every domain function below
    takes a `World` and mutates it in place, returning narrative lines --
    see this module's own docstring for why that boundary matters."""

    def __init__(self, save: SaveData, *, checkpoint: Callable[[World], None] | None = None):
        self._checkpoint = checkpoint
        self.reset(save)

    def checkpoint(self) -> None:
        """Commit a completed action before acknowledging it to the caller.

        The executable binds storage; domain-only worlds need no filesystem.
        A failed commit stops the UI rather than acknowledging unsaved progress.
        Resetting a career preserves this binding.
        """
        self.sync_discovered()
        if self.save.pending_travel is None:
            expire_missions(self)
            _normalize_mission_ids(self.save)
            generate_mission_board(self)
            remember_local_market(self)
        if tracked_mission(self) is None:
            self.save.tracked_mission_id = None
        self.save.event_rng_state = self.event_rng.getstate()
        if self.save.pending_travel is not None:
            self.save.pending_travel["destroyed"] = self.ship_destroyed_this_hop
        if self._checkpoint is not None:
            try:
                self._checkpoint(self)
            except OSError as exc:
                raise SaveError("The completed action could not be saved.") from exc

    def reset(self, save: SaveData) -> None:
        """Re-derives every galaxy-shaped attribute from `save` in
        place -- the same work `__init__` itself does (and now
        delegates to), factored out so `retire_pilot`'s own New Game+
        reset can reuse it exactly rather than duplicating "how to
        rebuild a World from a SaveData" a second time. Never disturbs
        an existing `World` object's identity, only its contents -- a
        retiring pilot's `world` variable stays the same object, just
        pointed at a fresh galaxy/save underneath."""
        _validate_pending_travel_consistency(save)
        self.save = save
        if save.pending_travel is None:
            _normalize_mission_ids(save)
        self.galaxy: list[GalaxySystem] = generate_galaxy(save.seed)
        self.by_id: dict[int, GalaxySystem] = {s.id: s for s in self.galaxy}
        for sid in save.discovered:
            if sid in self.by_id:
                self.by_id[sid].discovered = True
        self.landmark: dict = generate_landmark(save.seed, self.galaxy)
        self.event_rng = random.Random()
        if save.event_rng_state is not None:
            try:
                version, state, gaussian = save.event_rng_state
                self.event_rng.setstate((version, tuple(state), gaussian))
            except (ValueError, TypeError, OverflowError) as exc:
                raise ResumeError("The saved random state cannot be restored.") from exc
        elif save.pending_travel is not None:
            raise ResumeError("The interrupted journey has no saved random state.")
        self.ship_destroyed_this_hop = bool(
            save.pending_travel and save.pending_travel["destroyed"]
        )

    @property
    def here(self) -> GalaxySystem:
        return self.by_id[self.save.current_system]

    def sync_discovered(self) -> None:
        self.save.discovered = [s.id for s in self.galaxy if s.discovered]


def _validate_pending_travel_consistency(save: SaveData) -> None:
    """Reject contradictory checkpoints before startup can rewrite the save."""
    travel = save.pending_travel
    if travel is None:
        return
    try:
        combat = travel["encounter"].get("combat")
        if combat is not None:
            outcome = combat["outcome"]
            if (combat["pirate"]["hp"] == 0) != (outcome == "won"):
                raise ValueError("opponent damage contradicts outcome")
            if travel["destroyed"] != (outcome == "destroyed"):
                raise ValueError("destruction contradicts outcome")
        expected_system = 0 if travel["destroyed"] else (
            travel["destination"] if travel["phase"] == "customs" else travel["origin"]
        )
        if save.current_system != expected_system:
            raise ValueError("position contradicts journey phase")
        pending = []
        if travel["phase"] == "primary" and travel["primary"] == "bounty":
            pending.append(Mission.from_dict(travel["bounty"]))
        if travel["phase"] in ("primary", "escorts"):
            pending.extend(Mission.from_dict(m) for m in travel["escorts"][travel["escort_index"]:])
        # Consume a copy as a multiset: legacy contracts can share both IDs and
        # identical terms. A single active job cannot satisfy two pending jobs.
        active = list(save.active_missions)
        for mission in pending:
            active.remove(mission)
    except (KeyError, TypeError, ValueError) as exc:
        raise ResumeError("The interrupted journey conflicts with the saved career.") from exc


# ---------------------------------------------------------------------------
# Galaxy generation (see the module docstring's call-order invariant)
# ---------------------------------------------------------------------------


def _distance(a: GalaxySystem, b: GalaxySystem) -> float:
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


def generate_galaxy(seed: int) -> list[GalaxySystem]:
    rng = random.Random(seed)
    names = SYSTEM_NAMES[:]
    rng.shuffle(names)
    systems: list[GalaxySystem] = []
    positions: set[tuple[int, int]] = set()
    for i in range(GALAXY_SYSTEM_COUNT):
        while True:
            pos = (rng.randint(0, 99), rng.randint(0, 49))
            if pos not in positions:
                positions.add(pos)
                break
        economy = rng.choices(ECONOMIES, weights=ECONOMY_WEIGHTS)[0]
        danger = max(0, min(5, ECONOMY_BASE_DANGER[economy] + rng.randint(-1, 2)))
        name = names[i % len(names)]
        station = f"{name.split()[0]} {rng.choice(STATION_SUFFIXES)}"
        systems.append(GalaxySystem(
            id=i, name=name, x=pos[0], y=pos[1], economy=economy, danger=danger,
            station_name=station,
        ))
    _connect_systems(systems, rng)
    home = systems[0]
    home.economy = "Industrial"
    home.danger = 1
    home.name = "Freeport"
    home.station_name = "Freeport Anchorage"
    home.discovered = True
    for nid in home.connections:
        neighbor = systems[nid]
        neighbor.discovered = True
        # Dogfood-caught: every system's danger/tier is drawn from the
        # same distribution regardless of distance from home, so purely
        # by seed luck a brand-new character could find a near-
        # unwinnable tier-4 raider one jump from Freeport, before ever
        # having a chance to earn a single upgrade. Capping *only* the
        # systems immediately reachable from home -- not the whole
        # galaxy, which would flatten the intended "push further, get
        # stronger" difficulty curve -- guarantees every new career gets
        # a short, real ramp before the tougher tiers start showing up
        # further out. No new `rng` calls here, so this doesn't disturb
        # this function's own seed-determinism invariant (see this
        # module's docstring).
        neighbor.danger = min(neighbor.danger, 2)
    return systems


def _connect_systems(systems: list[GalaxySystem], rng: random.Random) -> None:
    n = len(systems)
    in_tree = {0}
    while len(in_tree) < n:
        best: tuple[float, int, int] | None = None
        for i in in_tree:
            for j in range(n):
                if j in in_tree:
                    continue
                d = _distance(systems[i], systems[j])
                if best is None or d < best[0]:
                    best = (d, i, j)
        assert best is not None
        _, i, j = best
        systems[i].connections.append(j)
        systems[j].connections.append(i)
        in_tree.add(j)
    extra = max(6, n // 5)
    attempts = 0
    while extra > 0 and attempts < n * 20:
        attempts += 1
        i, j = rng.randrange(n), rng.randrange(n)
        if i == j or j in systems[i].connections:
            continue
        if _distance(systems[i], systems[j]) > 22:
            continue
        systems[i].connections.append(j)
        systems[j].connections.append(i)
        extra -= 1


# A distinguishing offset, not a real magic constant -- just keeps this
# module's own separate landmark `random.Random` instance from ever
# producing the same sequence as anything else seeded from `save.seed`
# directly (`event_rng`'s own reseeding, notably).
_LANDMARK_SEED_OFFSET = 0x4C414E44  # ASCII "LAND"

LANDMARK_FLAVORS = [
    {
        "label": "the Derelict Ark",
        "flavor": "A colony ship, generations old, drifting silent -- its hull scarred "
                   "by something that met it partway. The cargo bay's cryo-pods are long "
                   "since empty, but the ship's strongroom never was.",
        "reward_credits": 3000,
    },
    {
        "label": "the Silent Cathedral",
        "flavor": "A pre-Concord religious station, abandoned mid-service. The console "
                   "logs stop the same day, mid-sentence. Whatever the congregation left "
                   "behind, no one ever came back for it.",
        "reward_credits": 3000,
    },
    {
        "label": "the Shattered Yard",
        "flavor": "A shipyard that lost containment on something it was building. Half "
                   "the hull frames are still in their cradles, fused to the deck plating. "
                   "The other half is scattered across a debris field worth picking through.",
        "reward_credits": 3000,
    },
    {
        "label": "the Long Watch",
        "flavor": "An automated listening post, decades past its decommission date, still "
                   "quietly logging every ship that passes. Its archive is worth more to the "
                   "right buyer than the station's actual hardware ever was.",
        "reward_credits": 3000,
    },
]


def generate_landmark(seed: int, galaxy: list[GalaxySystem]) -> dict:
    """Picks one fixed system per galaxy to be a landmark -- a ruin or
    derelict station carrying a one-time lore payoff, per the #179
    backlog. Uses its own `random.Random` instance seeded from (but
    distinct from) the save's own seed, so it is fully reproducible for
    a given save without ever touching `generate_galaxy`'s own call
    sequence -- the seed-determinism invariant in that function's
    docstring only governs *its own* `random.Random` calls, not an
    unrelated, independently-seeded instance derived elsewhere. Never
    picks system 0 (Freeport) -- the landmark should be a destination
    worth traveling to, not home."""
    rng = random.Random(seed ^ _LANDMARK_SEED_OFFSET)
    candidates = [s.id for s in galaxy if s.id != 0]
    system_id = rng.choice(candidates)
    flavor = rng.choice(LANDMARK_FLAVORS)
    return {"system_id": system_id, **flavor}


# A purely positional grouping of the galaxy's own 100x50 coordinate
# grid (see `generate_galaxy`'s own `rng.randint(0, 99), rng.randint(0,
# 49)`) into a small number of named sectors, for a more readable chart
# once a career has explored more than a handful of systems. No RNG
# involved at all -- unlike `generate_landmark`, this needs none, so it
# can never even theoretically interact with `generate_galaxy`'s own
# seed-determinism invariant -- and stays perfectly stable for a given
# galaxy without needing to be stored anywhere.
SECTOR_COLS = 3
SECTOR_ROWS = 2
SECTOR_NAMES = [
    "Coreward Verge", "Auroral Span", "Farrider's Edge",
    "Hollow Reach", "The Long Dark", "Outer Fringe",
]  # row-major over the SECTOR_ROWS x SECTOR_COLS grid below


def sector_for(system: GalaxySystem) -> str:
    col = min(SECTOR_COLS - 1, system.x * SECTOR_COLS // 100)
    row = min(SECTOR_ROWS - 1, system.y * SECTOR_ROWS // 50)
    return SECTOR_NAMES[row * SECTOR_COLS + col]


def bfs_hops(by_id: dict[int, GalaxySystem], start_id: int) -> dict[int, int]:
    dist = {start_id: 0}
    q = collections.deque([start_id])
    while q:
        cur = q.popleft()
        for nxt in by_id[cur].connections:
            if nxt not in dist:
                dist[nxt] = dist[cur] + 1
                q.append(nxt)
    return dist


def fuel_cost_for_jump(a: GalaxySystem, b: GalaxySystem, ship: Ship | None = None) -> int:
    cost = max(1, round(_distance(a, b) / 6))
    if ship is not None and ship.has_engineer:
        cost = max(1, cost - (cost + 3) // 4)
    return cost


def bfs_path(by_id: dict[int, GalaxySystem], start_id: int, dest_id: int) -> list[int]:
    """Shortest hop-by-hop path from `start_id` to `dest_id`, as the
    list of system ids to jump through in order (excludes `start_id`,
    ends with `dest_id`; empty if they're the same system). The galaxy
    graph is always fully connected -- `_connect_systems` builds it from
    a spanning tree before adding any extra edges -- so a path always
    exists between any two system ids from the same galaxy."""
    if start_id == dest_id:
        return []
    came_from: dict[int, int] = {}
    seen = {start_id}
    q = collections.deque([start_id])
    while q:
        cur = q.popleft()
        if cur == dest_id:
            break
        for nxt in by_id[cur].connections:
            if nxt not in seen:
                seen.add(nxt)
                came_from[nxt] = cur
                q.append(nxt)
    path = [dest_id]
    while path[-1] != start_id:
        path.append(came_from[path[-1]])
    path.pop()
    path.reverse()
    return path


# ---------------------------------------------------------------------------
# Economy
# ---------------------------------------------------------------------------


def price_for(world: World, system_id: int, commodity: str) -> int:
    system = world.by_id[system_id]
    base = COMMODITIES[commodity]["base"]
    mult = 1.0
    if commodity in ECONOMY_PRODUCES[system.economy]:
        mult *= 0.6
    if commodity in ECONOMY_DEMANDS[system.economy]:
        mult *= 1.6
    drift = world.save.market_drift.get(system_id, {}).get(commodity, 1.0)
    return max(1, round(base * mult * drift))


def _nudge_drift(world: World, system_id: int, commodity: str, delta: float) -> None:
    table = world.save.market_drift.setdefault(system_id, {})
    current = table.get(commodity, 1.0)
    table[commodity] = max(0.6, min(1.6, current + delta))


def remember_local_market(world: World) -> None:
    """Observe only docked, locally visible quotes, without consuming RNG."""
    if world.save.pending_travel is not None:
        return
    for commodity in COMMODITIES:
        legal = COMMODITIES[commodity]["legal"]
        if not legal and world.here.economy != "Haven" and not world.save.cargo.get(commodity, 0):
            continue
        unit = price_for(world, world.here.id, commodity)
        _remember_market_quote(world, world.here.id, commodity, unit)


def _remember_market_quote(world: World, system_id: int, commodity: str, unit: int) -> dict:
    quote = {"day": world.save.turn, "sell": round(unit * SELL_SPREAD),
             "buy": unit if COMMODITIES[commodity]["legal"] or world.by_id[system_id].economy == "Haven" else None}
    world.save.market_memory.setdefault(system_id, {})[commodity] = quote
    return quote


def cargo_cost_preview(world: World, commodity: str, quantity: int) -> tuple[int, int]:
    """Read-only FIFO allocation matching disposal, including unknown older stock."""
    lots = world.save.cargo_basis.get(commodity, [])
    unknown = min(quantity, world.save.cargo.get(commodity, 0) - sum(lot[0] for lot in lots))
    remaining = quantity - unknown
    cost = 0
    for units, paid in lots:
        taken = min(remaining, units)
        cost += paid * taken // units
        remaining -= taken
        if not remaining:
            break
    return cost, unknown


def trade_route_quote(world: World, destination: int, commodity: str, quantity: int, *, use_hold: bool = False) -> dict:
    """Estimate against remembered sale data; never query a live remote market."""
    if world.save.pending_travel is not None:
        raise TradeError("Finish the journey before estimating a new trade.")
    if (type(destination) is not int or destination == world.here.id or destination not in world.by_id
            or not isinstance(commodity, str) or commodity not in COMMODITIES
            or type(quantity) is not int or quantity < 1 or type(use_hold) is not bool):
        raise TradeError("Choose another station, a commodity and a positive whole quantity.")
    remembered = world.save.market_memory.get(destination, {}).get(commodity)
    if remembered is None:
        raise TradeError("No observed sale quote for this commodity at that station. Visit its market first.")
    if use_hold:
        if quantity > world.save.cargo.get(commodity, 0):
            raise TradeError("That quantity is not in your hold.")
        cost, unknown = cargo_cost_preview(world, commodity, quantity)
        procurement = 0
    else:
        if not COMMODITIES[commodity]["legal"] and world.here.economy != "Haven":
            raise TradeError("This station does not openly sell that commodity.")
        if quantity + sum(world.save.cargo.values()) > cargo_capacity(world.save.ship):
            raise TradeError("That purchase would exceed your free hold space.")
        cost = procurement = quantity * price_for(world, world.here.id, commodity)
        unknown = 0
    path = bfs_path(world.by_id, world.here.id, destination)
    legs = []
    origin = world.here.id
    for sid in path:
        burn = fuel_cost_for_jump(world.by_id[origin], world.by_id[sid], world.save.ship)
        legs.append((sid, burn))
        origin = sid
    fuel = sum(burn for _, burn in legs)
    wage = sum(info["wage"] for role, info in CREW_ROLES.items() if getattr(world.save.ship, f"has_{role}"))
    wages = wage * len(path)
    fuel_cash = max(0, fuel - world.save.ship.fuel) * 6
    receipts = quantity * remembered["sell"]
    conflicts = []
    available = world.save.cargo.get(commodity, 0) + (0 if use_hold else quantity)
    older_cargo = 0 if use_hold else world.save.cargo.get(commodity, 0)
    for hop, sid in enumerate(path, 1):
        for mission in world.save.active_missions:
            if (mission.kind == "delivery" and mission.commodity == commodity
                    and mission.target_system == sid and mission.quantity <= available
                    and (mission.deadline_turn is None or world.save.turn + hop <= mission.deadline_turn)):
                if mission.quantity > older_cargo:
                    conflicts.append(mission.description)
                older_cargo = max(0, older_cargo - mission.quantity)
                available -= mission.quantity
    return {"destination": destination, "commodity": commodity, "quantity": quantity, "use_hold": use_hold,
            "observed_day": remembered["day"], "unit_sale": remembered["sell"], "receipts": receipts,
            "cargo_cost": cost, "unknown_units": unknown, "procurement": procurement, "legs": legs,
            "fuel": fuel, "fuel_cash": fuel_cash, "wages": wages,
            "cash_needed": procurement + fuel_cash + wages,
            "conflicts": conflicts,
            "margin": None if unknown or conflicts else receipts - cost - fuel * 6 - wages,
            "feasible": all(burn <= fuel_capacity(world.save.ship) for _, burn in legs)}


def tick_price_reversion(world: World) -> None:
    """Called once per turn (each jump) -- slowly pulls every drift entry
    that exists back toward 1.0 with a little noise, so a market you
    depressed/inflated recovers over time instead of staying broken
    forever. Only visited-and-traded systems ever have entries, so this
    stays cheap regardless of galaxy size."""
    for table in world.save.market_drift.values():
        for commodity, value in list(table.items()):
            reverted = value + (1.0 - value) * 0.08
            reverted += world.event_rng.uniform(-0.01, 0.01)
            table[commodity] = max(0.6, min(1.6, reverted))


# At most one galaxy-wide economy event active at a time. Deliberately
# a fixed drift *level* re-asserted every turn while active (see
# tick_economy_event), not a one-time nudge -- a one-time nudge would
# just be erased by tick_price_reversion's own per-turn pull toward 1.0
# within a couple of turns, defeating "temporary but real for a while."
ECONOMY_EVENT_CHANCE_PER_TURN = 0.05
ECONOMY_EVENT_MIN_TURNS = 8
ECONOMY_EVENT_MAX_TURNS = 15
ECONOMY_EVENT_CRASH_LEVEL = 0.7
ECONOMY_EVENT_BOOM_LEVEL = 1.3


def tick_economy_event(world: World) -> str | None:
    """Called once per turn, right after `tick_price_reversion` -- ages
    and ends an already-active event, or (only when none is active)
    rolls a small chance to start a new one. Returns a narrative line
    on the turn an event starts or ends, else None (most turns, most
    careers -- this is meant to be a rare, notable happening, not
    background noise)."""
    event = world.save.active_event
    if event is not None:
        level = ECONOMY_EVENT_CRASH_LEVEL if event["direction"] == "crash" else ECONOMY_EVENT_BOOM_LEVEL
        for system in world.galaxy:
            if system.economy == event["economy"]:
                table = world.save.market_drift.setdefault(system.id, {})
                table[event["commodity"]] = level
        event["turns_remaining"] -= 1
        if event["turns_remaining"] <= 0:
            world.save.active_event = None
            return f"Galaxy news: the {event['description']} has ended -- prices normalize."
        return None

    if world.event_rng.random() >= ECONOMY_EVENT_CHANCE_PER_TURN:
        return None
    economy = world.event_rng.choice(ECONOMIES)
    commodities = sorted(set(ECONOMY_PRODUCES[economy]) | set(ECONOMY_DEMANDS[economy]))
    if not commodities:
        return None
    commodity = world.event_rng.choice(commodities)
    direction = world.event_rng.choice(["crash", "boom"])
    turns = world.event_rng.randint(ECONOMY_EVENT_MIN_TURNS, ECONOMY_EVENT_MAX_TURNS)
    label = COMMODITIES[commodity]["label"]
    verb = "crash" if direction == "crash" else "spike"
    description = f"{label} prices {verb} across every {economy} system"
    world.save.active_event = {
        "economy": economy, "commodity": commodity, "direction": direction,
        "turns_remaining": turns, "description": description,
    }
    level = ECONOMY_EVENT_CRASH_LEVEL if direction == "crash" else ECONOMY_EVENT_BOOM_LEVEL
    for system in world.galaxy:
        if system.economy == economy:
            table = world.save.market_drift.setdefault(system.id, {})
            table[commodity] = level
    return f"Galaxy news: {description} (roughly {turns} turns)."


# New orders preserve a goods principal separately from their nonrefundable fee.
FUTURES_PREMIUM = 1.08  # Legacy price multiplier; new fees use integer rounding.
FUTURES_FEE_PERCENT = 8
FUTURES_DURATIONS = (5, 10, 20)
MAX_FUTURES_CONTRACTS = 8


class TradeError(ValueError):
    """Rejected economy actions leave the career unchanged."""


def _ledger(world: World) -> TradingLedger:
    ledger = world.save.trading_ledger
    if ledger.since_day is None:
        ledger.since_day = world.save.turn
    return ledger


def _acquire_cargo(world: World, commodity: str, quantity: int, cost: int) -> None:
    """Add purchased goods and their exact basis in the same action."""
    _ledger(world)
    lots = world.save.cargo_basis.setdefault(commodity, [])
    lots.append([quantity, cost])
    world.save.cargo[commodity] = world.save.cargo.get(commodity, 0) + quantity


def _dispose_cargo(world: World, commodity: str, quantity: int, *,
                   proceeds: int = 0, kind: str = "loss") -> tuple[int, int]:
    """Consume unknown legacy stock first, then FIFO lots; return cost/unknown units."""
    if quantity == 0:
        if world.save.cargo.get(commodity) == 0:
            world.save.cargo.pop(commodity)
        return 0, 0
    have = world.save.cargo[commodity]
    lots = world.save.cargo_basis.get(commodity, [])
    unknown = min(quantity, have - sum(lot[0] for lot in lots))
    remaining = quantity - unknown
    cost = 0
    while remaining:
        lot = lots[0]
        taken = min(remaining, lot[0])
        allocated = lot[1] * taken // lot[0]
        cost += allocated
        lot[0] -= taken
        lot[1] -= allocated
        remaining -= taken
        if lot[0] == 0:
            lots.pop(0)
    if not lots:
        world.save.cargo_basis.pop(commodity, None)
    if quantity == have:
        del world.save.cargo[commodity]
    else:
        world.save.cargo[commodity] = have - quantity
    ledger = _ledger(world)
    unknown_receipts = proceeds * unknown // quantity
    if kind == "sale":
        ledger.sales_revenue += proceeds - unknown_receipts
        ledger.sales_cost += cost
        ledger.uncosted_sales += unknown_receipts
    elif kind == "delivery":
        ledger.delivery_revenue += proceeds - unknown_receipts
        ledger.delivery_cost += cost
        ledger.uncosted_deliveries += unknown_receipts
    else:
        ledger.cargo_loss_cost += cost
        ledger.uncosted_losses += unknown
    return cost, unknown


def trade_cargo(world: World, commodity: str, quantity: int, *, buying: bool) -> str:
    """Validate a market command before changing credits, cargo, basis or prices."""
    if world.save.pending_travel is not None:
        raise TradeError("Finish the journey before trading.")
    if (not isinstance(commodity, str) or commodity not in COMMODITIES
            or type(quantity) is not int or quantity < 1 or type(buying) is not bool):
        raise TradeError("Choose a valid commodity and positive whole quantity.")
    unit = price_for(world, world.here.id, commodity)
    label = COMMODITIES[commodity]["label"]
    if buying:
        if not COMMODITIES[commodity]["legal"] and world.here.economy != "Haven":
            raise TradeError("Station authorities prohibit the open purchase of contraband.")
        if sum(world.save.cargo.values()) + quantity > cargo_capacity(world.save.ship):
            raise TradeError("Not enough cargo space.")
        total = quantity * unit
        if total > world.save.pilot.credits:
            raise TradeError(f"Need {total}cr for this purchase.")
        world.save.pilot.credits -= total
        _acquire_cargo(world, commodity, quantity, total)
        _nudge_drift(world, world.here.id, commodity, min(0.05, quantity * 0.01))
        record_contraband_trade(world, commodity, -total)
        return f"Bought {quantity}x {label} for {total}cr."
    if quantity > world.save.cargo.get(commodity, 0):
        raise TradeError("You do not have that much cargo.")
    total = quantity * round(unit * SELL_SPREAD)
    world.save.pilot.credits += total
    _dispose_cargo(world, commodity, quantity, proceeds=total, kind="sale")
    _nudge_drift(world, world.here.id, commodity, -min(0.05, quantity * 0.01))
    record_contraband_trade(world, commodity, total)
    return f"Sold {quantity}x {label} for {total}cr."


def futures_quote(world: World, commodity: str, quantity: int) -> tuple[int, int]:
    if commodity not in COMMODITIES or type(quantity) is not int or quantity < 1:
        raise TradeError("Choose a valid commodity and positive whole quantity.")
    unit = price_for(world, world.save.current_system, commodity)
    return unit * quantity, max(1, (unit * FUTURES_FEE_PERCENT + 99) // 100) * quantity


def buy_futures_contract(world: World, commodity: str, quantity: int, duration: int) -> str:
    if world.save.pending_travel is not None:
        raise TradeError("Finish the journey before placing an order.")
    if type(duration) is not int or duration not in FUTURES_DURATIONS:
        raise TradeError("Choose a 5, 10 or 20 day term.")
    principal, fee = futures_quote(world, commodity, quantity)
    if not COMMODITIES[commodity]["legal"] and world.here.economy != "Haven":
        raise TradeError("This station does not sell contraband futures.")
    if quantity > cargo_capacity(world.save.ship):
        raise TradeError("Order quantity exceeds your ship's cargo capacity.")
    if len(world.save.active_futures) >= MAX_FUTURES_CONTRACTS:
        raise TradeError(f"At most {MAX_FUTURES_CONTRACTS} outstanding orders; collect or cancel one first.")
    total = principal + fee
    if total > world.save.pilot.credits:
        raise TradeError(f"Need {total}cr including the nonrefundable {fee}cr fee.")
    contract = FuturesContract(
        id=world.save.next_futures_id, commodity=commodity, quantity=quantity,
        locked_price=total, settle_turn=world.save.turn + duration,
        origin_system=world.save.current_system, principal=principal,
    )
    world.save.pilot.credits -= total
    record_contraband_trade(world, commodity, -total)
    world.save.active_futures.append(contract)
    world.save.next_futures_id += 1
    label = COMMODITIES[commodity]["label"]
    msg = (f"Futures contract: {quantity}x {label}, goods {principal}cr + fee {fee}cr. "
           f"Pickup at {world.here.name} from day {contract.settle_turn}.")
    world.save.pilot.note(msg)
    return msg


def cancel_futures_contract(world: World, contract_id: int) -> str:
    if world.save.pending_travel is not None:
        raise TradeError("Finish the journey before cancelling an order.")
    contract = next((c for c in world.save.active_futures if c.id == contract_id), None)
    if contract is None or contract.origin_system is None:
        raise TradeError("That pickup order is no longer active.")
    world.save.active_futures.remove(contract)
    world.save.pilot.credits += contract.principal
    record_contraband_trade(world, contract.commodity, contract.principal)
    fee = contract.locked_price - contract.principal
    _ledger(world).cancelled_fees += fee
    msg = f"Order cancelled: {contract.principal}cr refunded; {fee}cr brokerage fee retained."
    world.save.pilot.note(msg)
    return msg


def settle_futures_contracts(world: World, *, legacy_only: bool = False) -> list[str]:
    """Deliver ready pickup orders at their station; honor legacy terms once."""
    messages = []
    for contract in list(world.save.active_futures):
        if world.save.turn < contract.settle_turn:
            continue
        legacy = contract.origin_system is None
        if not legacy and (legacy_only or world.save.current_system != contract.origin_system):
            continue
        room = cargo_capacity(world.save.ship) - sum(world.save.cargo.values())
        if room < contract.quantity and not legacy:
            continue  # Ready goods wait; no automatic refund or lost fee.
        world.save.active_futures.remove(contract)
        label = COMMODITIES[contract.commodity]["label"]
        if room < contract.quantity:
            world.save.pilot.credits += contract.locked_price
            msg = f"Legacy futures: no cargo room -- refunded {contract.locked_price}cr under original terms."
        else:
            _acquire_cargo(world, contract.commodity, contract.quantity, contract.locked_price)
            msg = f"Futures contract settled: {contract.quantity}x {label} delivered to your hold."
        world.save.pilot.note(msg)
        messages.append(msg)
    return messages


# ---------------------------------------------------------------------------
# Missions
# ---------------------------------------------------------------------------


MISSION_BOARD_DAYS = 3
MAX_ACTIVE_MISSIONS = 3


class MissionError(ValueError):
    """A rejected contract action makes no changes."""


def _load_mission_boards(data: dict) -> dict[int, dict]:
    try:
        if not isinstance(data, dict) or len(data) > GALAXY_SYSTEM_COUNT:
            raise ValueError("invalid boards")
        result = {}
        for key, board in data.items():
            sid = int(key)
            if not 0 <= sid < GALAXY_SYSTEM_COUNT or sid in result or not isinstance(board, dict):
                raise ValueError("invalid station")
            if type(board["refresh_turn"]) is not int or board["refresh_turn"] < 0:
                raise ValueError("invalid refresh day")
            offers = board["offers"]
            if not isinstance(offers, list) or len(offers) > 4:
                raise ValueError("invalid offers")
            for data in offers:
                mission = Mission.from_dict(data)
                if mission.kind not in ("delivery", "scan", "bounty", "escort") or mission.origin_system != sid:
                    raise ValueError("invalid offer")
                _validate_combat_mission_snapshot(data, mission.kind)
                if mission.kind == "delivery" and (mission.commodity is None or mission.quantity is None or mission.quantity < 1):
                    raise ValueError("invalid delivery")
            result[sid] = board
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise ResumeError("The saved contract boards cannot be read.") from exc


def _normalize_mission_ids(save: SaveData) -> None:
    """Repair legacy duplicate IDs while docked; never alter pending snapshots."""
    records = [m.to_dict() for m in save.active_missions]
    for board in save.mission_boards.values():
        records.extend(board["offers"])
    next_id = max([save.next_mission_id, 1] + [r["id"] + 1 for r in records])
    seen = set()
    for record in records:
        if record["id"] in seen or record["id"] < 1:
            record["id"] = next_id
            next_id += 1
        seen.add(record["id"])
    for mission, record in zip(save.active_missions, records):
        mission.id = record["id"]
    save.next_mission_id = next_id


def generate_mission_board(world: World) -> list[Mission]:
    """Stable posted offers: browsing/acceptance never replenishes the board."""
    sid = world.save.current_system
    cached = world.save.mission_boards.get(sid)
    if cached is None or world.save.turn >= cached["refresh_turn"]:
        _normalize_mission_ids(world.save)
        rng = random.Random(f"voidrunner-board-v1:{world.save.seed}:{sid}:{world.save.turn}")
        hops = bfs_hops(world.by_id, sid)
        offers = []
        for kind in rng.sample(["delivery", "delivery", "bounty", "scan", "escort"], k=rng.randint(3, 4)):
            mission = _generate_mission(world, kind, hops, rng=rng)
            if mission is not None:
                offers.append(mission.to_dict())
                world.save.next_mission_id += 1
        cached = {"refresh_turn": world.save.turn + MISSION_BOARD_DAYS, "offers": offers}
        world.save.mission_boards[sid] = cached
    return posted_mission_offers(world)


def posted_mission_offers(world: World) -> list[Mission]:
    """Read-only view of offers prepared at the preceding station checkpoint."""
    cached = world.save.mission_boards.get(world.save.current_system)
    if cached is None or world.save.turn >= cached["refresh_turn"]:
        return []
    # Return copies: a UI list or stale caller must not mutate the posted terms.
    return [Mission.from_dict(m) for m in cached["offers"]
            if not mission_expired(world, Mission.from_dict(m))
            and not (m["kind"] == "scan" and world.by_id[m["target_system"]].discovered)]


def _generate_mission(world: World, kind: str, hops: dict[int, int], *, rng=None) -> Mission | None:
    rng = world.event_rng if rng is None else rng
    origin = world.save.current_system
    if kind == "delivery":
        candidates = [sid for sid, h in hops.items() if 1 <= h <= 5 and sid != origin]
        if not candidates:
            return None
        target = rng.choice(candidates)
        commodity = rng.choice(LEGAL_COMMODITIES)
        qty = rng.randint(3, 10)
        reward = round(qty * COMMODITIES[commodity]["base"] * (0.9 + 0.15 * hops[target])) + 50
        desc = f"Deliver {qty}x {COMMODITIES[commodity]['label']} to {world.by_id[target].name}"
        return Mission(id=world.save.next_mission_id, kind=kind, description=desc, reward=reward,
                        origin_system=origin, target_system=target, commodity=commodity, quantity=qty,
                        deadline_turn=world.save.turn + rng.randint(15, 30))
    if kind == "bounty":
        candidates = [sid for sid, h in hops.items() if 1 <= h <= 3 and sid != origin and world.by_id[sid].discovered]
        if not candidates:
            return None
        target = rng.choice(candidates)
        tier = max(0, min(4, world.by_id[target].danger + rng.randint(-1, 1)))
        reward = 300 + tier * 250
        desc = f"Hunt down a raider reported near {world.by_id[target].name}"
        return Mission(id=world.save.next_mission_id, kind=kind, description=desc, reward=reward,
                        origin_system=origin, target_system=target, pirate_tier=tier,
                        deadline_turn=world.save.turn + rng.randint(15, 30))
    if kind == "scan":
        candidates = [sid for sid, h in hops.items() if 1 <= h <= 4 and not world.by_id[sid].discovered]
        if not candidates:
            return None
        target = rng.choice(candidates)
        reward = 200 + hops[target] * 80
        desc = f"Survey the uncharted system {hops[target]} jump(s) out (bearing logged)"
        return Mission(id=world.save.next_mission_id, kind=kind, description=desc, reward=reward,
                        origin_system=origin, target_system=target, deadline_turn=None)
    if kind == "escort":
        # A minimum of 2 hops, unlike bounty's single-system framing --
        # the whole point is "several jumps" of scripted waves, not one
        # fight at a fixed point. No `discovered` filter on the target,
        # matching delivery's own precedent of naming an uncharted
        # destination in the description.
        candidates = [sid for sid, h in hops.items() if 2 <= h <= 5 and sid != origin]
        if not candidates:
            return None
        target = rng.choice(candidates)
        tier = max(0, min(4, world.by_id[target].danger + rng.randint(-1, 1)))
        reward = 250 + hops[target] * 150 + tier * 150
        desc = (f"Escort a supply convoy to {world.by_id[target].name} "
                 f"({hops[target]} jump(s), raider activity expected)")
        return Mission(id=world.save.next_mission_id, kind=kind, description=desc, reward=reward,
                        origin_system=origin, target_system=target, pirate_tier=tier,
                        deadline_turn=world.save.turn + hops[target] * 6 + 10)
    return None


def accept_mission(world: World, mission: Mission) -> None:
    if mission.opening_assignment:
        raise MissionError("Accept First Flight from the Pilot Guide.")
    if len(world.save.active_missions) >= MAX_ACTIVE_MISSIONS:
        raise MissionError(f"You can carry at most {MAX_ACTIVE_MISSIONS} active contracts. Finish a contract first.")
    if mission_expired(world, mission):
        raise MissionError("That contract has expired.")
    if any(m.id == mission.id for m in world.save.active_missions):
        raise MissionError("That contract is already active.")
    if mission.origin_system != world.save.current_system:
        raise MissionError("Accept this contract at its originating station.")
    posted = world.save.mission_boards.get(world.save.current_system)
    if posted is None or world.save.turn >= posted["refresh_turn"] or mission.to_dict() not in posted["offers"]:
        raise MissionError("That offer is no longer posted. Reopen the contract board.")
    if mission.kind == "scan" and world.by_id[mission.target_system].discovered:
        raise MissionError("That survey target is already charted.")
    posted["offers"].remove(mission.to_dict())
    world.save.active_missions.append(Mission.from_dict(mission.to_dict()))
    world.save.next_mission_id = max(world.save.next_mission_id, mission.id + 1)


def mission_expired(world: World, mission: Mission) -> bool:
    return mission.deadline_turn is not None and world.save.turn > mission.deadline_turn


def opening_assignment_offer(world: World) -> Mission | None:
    """Quote an affordable first delivery without touching saves or RNG state."""
    save = world.save
    if (save.pending_travel is not None or save.current_system != 0 or save.turn != 0
            or save.flags.get("opening_assignment_taken")):
        return None
    ship = save.ship
    room = cargo_capacity(ship) - sum(save.cargo.values())
    wage = sum(info["wage"] for role, info in CREW_ROLES.items() if getattr(ship, f"has_{role}"))
    candidates = []
    for sid in world.here.connections:
        target = world.by_id[sid]
        fuel = fuel_cost_for_jump(world.here, target, ship)
        if 2 * fuel > fuel_capacity(ship):
            continue
        for commodity in ECONOMY_DEMANDS[target.economy]:
            if not COMMODITIES[commodity]["legal"]:
                continue
            if any(m.kind == "delivery" and m.target_system == sid and m.commodity == commodity
                   and not mission_expired(world, m) for m in save.active_missions):
                continue  # Earlier deliveries would consume this introductory load first.
            missing = max(0, 3 - save.cargo.get(commodity, 0))
            price = price_for(world, 0, commodity)
            budget = missing * price + max(0, 2 * fuel - ship.fuel) * 6 + 2 * wage
            if missing > room or budget > save.pilot.credits:
                continue
            preference = (target.danger, fuel, commodity not in ECONOMY_PRODUCES[world.here.economy], price, sid, commodity)
            reward = 3 * price + 2 * fuel * 6 + 2 * wage + 200
            candidates.append((preference, Mission(
                id=save.next_mission_id, kind="delivery", origin_system=0, target_system=sid,
                description=f"First Flight: deliver 3 {COMMODITIES[commodity]['label']} to {target.name}",
                commodity=commodity, quantity=3, reward=reward, opening_assignment=True,
            )))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def accept_opening_assignment(world: World, expected: Mission) -> None:
    """Validate the complete displayed offer before committing any rule changes."""
    offer = opening_assignment_offer(world)
    if offer is None or offer.to_dict() != expected.to_dict():
        raise MissionError("The opening assignment changed or is unavailable. Reopen the guide.")
    if len(world.save.active_missions) >= MAX_ACTIVE_MISSIONS:
        raise MissionError(f"You can carry at most {MAX_ACTIVE_MISSIONS} active contracts.")
    world.save.active_missions.append(offer)
    world.save.next_mission_id = offer.id + 1
    world.save.flags["opening_assignment_taken"] = True
    world.save.tracked_mission_id = offer.id


def expire_missions(world: World) -> list[str]:
    messages = []
    active = []
    for mission in world.save.active_missions:
        if mission_expired(world, mission):
            message = f"Mission expired: {mission.description}"
            world.save.pilot.note(message)
            messages.append(message)
        else:
            active.append(mission)
    world.save.active_missions = active
    if not any(m.id == world.save.tracked_mission_id for m in active):
        world.save.tracked_mission_id = None
    return messages


def check_mission_completions(world: World, *, just_discovered: int | None = None) -> list[str]:
    msgs = expire_missions(world)
    still_active: list[Mission] = []
    for m in world.save.active_missions:
        done = False
        if m.kind == "delivery" and world.save.current_system == m.target_system:
            have = world.save.cargo.get(m.commodity, 0)
            if have >= m.quantity:
                _dispose_cargo(world, m.commodity, m.quantity, proceeds=m.reward, kind="delivery")
                done = True
        elif m.kind == "scan" and just_discovered == m.target_system:
            done = True
        if done:
            world.save.pilot.credits += m.reward
            if m.opening_assignment:
                world.save.flags["opening_assignment_completed"] = True
            if world.save.pilot.missions_completed == 0:
                world.save.pilot.highlight(f"First mission complete: {m.description}.")
            world.save.pilot.missions_completed += 1
            msg = f"Mission complete: {m.description} (+{m.reward}cr)"
            world.save.pilot.note(msg)
            msgs.append(msg)
        else:
            still_active.append(m)
    world.save.active_missions = still_active
    if not any(m.id == world.save.tracked_mission_id for m in still_active):
        world.save.tracked_mission_id = None
    return msgs


# ---------------------------------------------------------------------------
# Combat / travel encounters
# ---------------------------------------------------------------------------


@dataclass
class Pirate:
    name: str
    tier: int
    hp: int
    hp_max: int


def generate_pirate(world: World, tier: int | None = None) -> Pirate:
    rng = world.event_rng
    t = tier if tier is not None else max(0, min(4, world.here.danger + rng.randint(-1, 1)))
    hp = 20 + t * 15
    return Pirate(name=rng.choice(PIRATE_NAMES), tier=t, hp=hp, hp_max=hp)


# Squadron fights: only at the two highest danger tiers, and even then
# not guaranteed -- most raider encounters stay a single ship. Scoped
# to the ordinary random "pirate" travel encounter only, not a bounty
# target (a mission's own singular "hunt down A raider" framing/reward
# doesn't fit a multi-ship fight) or a derelict's trap pirate (that
# encounter already compounds one risk -- boarding -- with combat;
# adding squadron risk on top would stack two escalations onto a single
# choice).
SQUADRON_MIN_DANGER = 4
SQUADRON_CHANCE = 0.35
SQUADRON_SIZE = 2


def generate_pirate_squadron(world: World, dest: GalaxySystem) -> list[Pirate]:
    """One ship most of the time; two at the highest danger tiers, with
    `SQUADRON_CHANCE` still deciding whether this particular encounter
    actually is one. Ships fight in sequence (the caller runs
    `screen_combat` once per ship, itself completely unmodified) with no
    auto-heal between them -- a squadron is meaningfully scarier because
    damage from the first ship carries into the fight against the
    second, not because the underlying combat math changes at all. Uses
    `dest.danger` for the spawn decision, matching `_resolve_random_
    travel_encounter`'s own "does anything happen at all" roll -- each
    individual ship's own tier still comes from `generate_pirate`'s
    existing (unrelated, origin-based) tier logic, unchanged."""
    if dest.danger >= SQUADRON_MIN_DANGER and world.event_rng.random() < SQUADRON_CHANCE:
        return [generate_pirate(world) for _ in range(SQUADRON_SIZE)]
    return [generate_pirate(world)]


def generate_concord_patrol(world: World) -> Pirate:
    """A Concord Patrol "hostile ship" for `screen_notoriety_patrol` --
    reuses the `Pirate` dataclass shape as-is (name/tier/hp/hp_max is all
    `fight_round`/`evade_chance` actually need structurally; nothing
    about those functions is pirate-specific) rather than introducing a
    second, parallel combatant type for one field's worth of
    difference. Difficulty scales with the pilot's own notoriety, not
    the system's danger rating -- a patrol is hunting *this pilot*
    specifically, unlike an ordinary raider encounter."""
    rng = world.event_rng
    tier = min(4, world.save.pilot.notoriety // 4)
    hp = 20 + tier * 15
    return Pirate(name=rng.choice(CONCORD_PATROL_NAMES), tier=tier, hp=hp, hp_max=hp)


def cargo_load_fraction(world: World) -> float:
    total = sum(world.save.cargo.values())
    cap = cargo_capacity(world.save.ship)
    return 0.0 if cap == 0 else min(1.0, total / cap)


def fight_round(world: World, pirate: Pirate) -> tuple[int, int, list[str]]:
    """One exchange of fire. Returns (damage_to_pirate, damage_to_player,
    narrative lines) -- pure aside from consuming `world.event_rng`, so
    the UI loop just prints and checks hp afterward."""
    rng = world.event_rng
    ship = world.save.ship
    lines = []
    dmg_to_pirate = rng.randint(5, 10) + ship.weapon_tier * 4 + (3 if ship.has_gunner else 0)
    pirate.hp = max(0, pirate.hp - dmg_to_pirate)
    lines.append(f"You hit the {pirate.name} for {dmg_to_pirate} damage.")
    if pirate.hp > 0:
        raw = rng.randint(4, 9) + pirate.tier * 4
        dmg_to_player = max(1, raw - ship.shield_tier * 3)
        world.save.ship.hull_hp = max(0, world.save.ship.hull_hp - dmg_to_player)
        lines.append(f"The {pirate.name} hits you for {dmg_to_player} damage.")
    else:
        dmg_to_player = 0
        lines.append(f"The {pirate.name} is destroyed!")
    return dmg_to_pirate, dmg_to_player, lines


def evade_chance(world: World, pirate: Pirate, *, dumped_cargo: bool) -> float:
    ship = world.save.ship
    chance = 0.5 + ship.engine_tier * 0.08 - pirate.tier * 0.07 - cargo_load_fraction(world) * 0.15
    if dumped_cargo:
        chance += 0.20
    return max(0.05, min(0.90, chance))


def bribe_cost(pirate: Pirate) -> int:
    return 150 + pirate.tier * 120


def bribe_chance(world: World, pirate: Pirate) -> float:
    rep = world.save.pilot.reputation.get(FACTION_BLACKWAKE, 0)
    chance = 0.30 + min(0.25, max(0, rep) * 0.01) - pirate.tier * 0.05
    return max(0.05, min(0.85, chance))


CONTRABAND_STANDING_STEP = 500


def _load_trade_total(value, *, nonnegative=False, label="contraband trading record") -> int:
    if type(value) is not int or (nonnegative and value < 0):
        raise ResumeError(f"The saved {label} cannot be read.")
    return value


def record_contraband_trade(world: World, commodity: str, cash_delta: int) -> None:
    """Reward only new lifetime cash-surplus milestones, never action count."""
    if COMMODITIES[commodity]["legal"]:
        return
    world.save.contraband_trade_balance += cash_delta
    milestones = max(0, world.save.contraband_trade_balance) // CONTRABAND_STANDING_STEP
    earned = milestones - world.save.contraband_trade_milestones
    if earned > 0:
        world.save.contraband_trade_milestones = milestones
        adjust_reputation(world, FACTION_BLACKWAKE, earned)
        world.save.pilot.note(f"Contraband trading milestone: +{earned} Blackwake standing.")


def adjust_reputation(world: World, faction: str, delta: int) -> None:
    rep = world.save.pilot.reputation
    rep[faction] = max(-100, min(100, rep.get(faction, 0) + delta))


# Faction endgame arcs: each faction's own reputation, previously only
# ever affecting bribe odds and customs outcomes in the moment, now has
# one exclusive, one-time-unlockable, lifelong reward at high standing.
# Deliberately a single permanent perk plus flavor per faction, not a
# repeatable mission chain -- matching the scope every other #179
# feature in this file settled on (retirement, landmark, etc.), not an
# open-ended new content system.
CONCORD_COMMISSION_THRESHOLD = 75
BLACKWAKE_MADE_THRESHOLD = 75
CONCORD_COMMISSION_BOUNTY_BONUS = 0.25  # +25% bounty/escort mission rewards, for life
BLACKWAKE_MADE_CUSTOMS_REDUCTION = 0.5  # halves customs check chance, for life
CONCORD_COMMISSION_BONUS_CREDITS = 2000
BLACKWAKE_MADE_BONUS_CREDITS = 2000


def concord_commission_available(world: World) -> bool:
    return (not world.save.pilot.has_concord_commission
            and world.save.pilot.reputation.get(FACTION_CONCORD, 0) >= CONCORD_COMMISSION_THRESHOLD)


def blackwake_made_available(world: World) -> bool:
    return (not world.save.pilot.has_blackwake_made
            and world.save.pilot.reputation.get(FACTION_BLACKWAKE, 0) >= BLACKWAKE_MADE_THRESHOLD)


def bounty_reward_for(world: World, base_reward: int) -> int:
    """Applies the Concord Privateer Commission's own bounty/escort
    reward bonus -- a permanent, one-time-unlocked perk (see
    screen_concord_commission), not a per-mission roll."""
    if world.save.pilot.has_concord_commission:
        return round(base_reward * (1 + CONCORD_COMMISSION_BOUNTY_BONUS))
    return base_reward


def screen_concord_commission(p: Palette, world: World) -> None:
    out_line()
    out_line(_box_title(p, "Concord Privateer Commission"))
    intro = [
        f"  {p.muted}Naval Command has taken notice of your combat record.{RESET}",
        f"  {p.muted}A formal privateer's commission is on offer -- official sanction to hunt{RESET}",
        f"  {p.muted}raiders under Concord colors and enhance every bounty and escort payout.{RESET}",
    ]
    for line in intro:
        pad_len = max(0, 77 - _vis_len(line))
        out_line(f"{p.accent}│{RESET}{line}{' ' * pad_len}{p.accent}│{RESET}")
    out_line(_box_bottom(p))
    if not confirm(f"Accept the commission ({CONCORD_COMMISSION_BONUS_CREDITS}cr signing bonus)?", p):
        return
    world.save.pilot.has_concord_commission = True
    world.save.pilot.credits += CONCORD_COMMISSION_BONUS_CREDITS
    world.save.pilot.note("Accepted a Concord privateer commission.")
    world.save.pilot.highlight("Commissioned as a Concord privateer -- bounty/escort rewards enhanced for life.")
    world.checkpoint()
    out_line(f"{p.correct}Commission accepted. Bounty and escort rewards are enhanced from here on.{RESET}")


def screen_blackwake_made(p: Palette, world: World) -> None:
    out_line()
    out_line(_box_title(p, "Blackwake Cartel"))
    intro = [
        f"  {p.muted}You have proven yourself to the Cartel's satisfaction.{RESET}",
        f"  {p.muted}Full membership is on offer -- its underground network of contacts eases{RESET}",
        f"  {p.muted}your way through customs inspections for as long as you fly.{RESET}",
    ]
    for line in intro:
        pad_len = max(0, 77 - _vis_len(line))
        out_line(f"{p.accent}│{RESET}{line}{' ' * pad_len}{p.accent}│{RESET}")
    out_line(_box_bottom(p))
    if not confirm(f"Accept full membership ({BLACKWAKE_MADE_BONUS_CREDITS}cr welcome gift)?", p):
        return
    world.save.pilot.has_blackwake_made = True
    world.save.pilot.credits += BLACKWAKE_MADE_BONUS_CREDITS
    world.save.pilot.note("Made a full member of the Blackwake Cartel.")
    world.save.pilot.highlight("Made a full member of the Blackwake Cartel -- customs risk reduced for life.")
    world.checkpoint()
    out_line(f"{p.correct}Welcome to the family. Customs checks are less likely to find you now.{RESET}")


def destroy_ship(world: World) -> str:
    """Ship destruction has real consequences -- lost cargo, a credit
    penalty, and a tow back home -- but is never a dead end. A door
    game with no way back from one bad fight is a needlessly hostile
    interaction, not a difficulty setting.

    Also wipes notoriety unconditionally, for any cause of destruction
    (an ordinary pirate as much as a Concord Patrol) -- a generic "near-
    death wipes your wanted status, fresh start" rule is simpler and
    easier to explain than a patrol-specific special case, and reads
    fine narratively either way: word doesn't travel from a wreck."""
    lost_cargo = sum(world.save.cargo.values())
    for commodity, quantity in list(world.save.cargo.items()):
        _dispose_cargo(world, commodity, quantity)
    penalty = min(world.save.pilot.credits, 200 + world.save.ship.hull_tier * 50)
    world.save.pilot.credits -= penalty
    world.save.ship.hull_hp = hull_hp_max(world.save.ship)
    world.save.current_system = 0
    world.save.pilot.notoriety = 0
    world.ship_destroyed_this_hop = True
    world.save.pilot.note("Ship destroyed -- salvage tug towed you back to Freeport.")
    return (f"Your ship is destroyed! {lost_cargo} units of cargo lost, "
            f"a {penalty}cr salvage fee charged. You wake up at Freeport Anchorage.")


def customs_check_chance(system: GalaxySystem) -> float:
    return max(0.0, 0.15 + (5 - system.danger) * 0.03)


def has_contraband(world: World) -> bool:
    return any(not COMMODITIES[c]["legal"] for c in world.save.cargo)


def dump_all_contraband(world: World) -> str:
    """Jettisons every illegal commodity in cargo at once, for zero
    credit -- a proactive alternative to `screen_customs`'s own
    "surrender contraband" outcome, without waiting to actually get
    stopped for it (and without risking a refused bribe's fine and
    notoriety if a customs check does fire). Strictly a QoL shortcut for
    a choice the player could already make one commodity at a time via
    the market, at any system with a market for it in the first place --
    dumping needs no market at all, since nothing is being sold."""
    dumped = {c: q for c, q in world.save.cargo.items() if not COMMODITIES[c]["legal"]}
    for c in dumped:
        _dispose_cargo(world, c, dumped[c])
    total = sum(dumped.values())
    msg = f"Jettisoned {total} units of contraband before a customs risk."
    world.save.pilot.note(msg)
    return msg


def is_stranded(world: World) -> bool:
    """True when the pilot has no way to leave the current system under
    their own power: no cargo to sell for cash, not enough fuel for even
    the cheapest reachable jump, and not enough credits to buy the
    shortfall at 6cr/unit either.

    Deliberately narrower than "ship destroyed" -- `destroy_ship` already
    has its own tow-home recovery, and a destroyed ship can never reach
    this check (hull_hp <= 0 always routes through that path first,
    resetting location/fuel/hull as a side effect). This instead catches
    a pilot who quietly spent down to nothing -- one refuel or repair too
    many, or a jump that used the last unit of fuel with no encounter
    along the way -- without ever losing a fight. `screen_station_menu`
    checks this on every single redraw (the outer loop's own home base,
    reached after every action), so a pilot can never linger in this
    state unnoticed."""
    if world.save.cargo:
        return False
    here = world.here
    if not here.connections:
        return False  # defensive: _connect_systems never leaves a system isolated
    cheapest = min(fuel_cost_for_jump(here, world.by_id[nid], world.save.ship) for nid in here.connections)
    if world.save.ship.fuel >= cheapest:
        return False
    shortfall = cheapest - world.save.ship.fuel
    return world.save.pilot.credits < shortfall * 6


def rescue_stranded_pilot(world: World) -> str:
    """Recovery for `is_stranded`, mirroring `destroy_ship`'s own "never
    a dead end" shape: tops the tank up to just enough for one more jump
    out of Freeport, towing the pilot there first if they aren't already
    home. No credit charge -- the whole point is a pilot who has nothing
    left to charge, unlike `destroy_ship`'s own salvage fee (capped at
    whatever the pilot can actually afford, which here is nothing)."""
    home = world.by_id[0]
    towed = world.save.current_system != 0
    if towed:
        world.save.current_system = 0
    cheapest = min(fuel_cost_for_jump(home, world.by_id[nid], world.save.ship) for nid in home.connections)
    world.save.ship.fuel = max(world.save.ship.fuel, cheapest)
    if towed:
        msg = ("Stranded with an empty tank and empty pockets, a passing salvage tug answers "
               "your beacon and tows you back to Freeport Anchorage, no charge.")
    else:
        msg = "The dockmaster spots your empty tank and tops you off enough to get moving again, no charge."
    world.save.pilot.note(msg)
    return msg


def pay_crew_wages(world: World) -> list[str]:
    """Deducts each hired crew member's per-turn wage -- called once per
    hop in `screen_travel`, the same cadence as the turn counter itself.
    A crew member whose wage can't be afforded resigns automatically
    (never drives credits negative, matching this file's own "never a
    dead end" consequence philosophy -- see `destroy_ship`/
    `rescue_stranded_pilot`) rather than being carried forward as debt."""
    ship = world.save.ship
    messages: list[str] = []
    for role, info in CREW_ROLES.items():
        if not getattr(ship, f"has_{role}"):
            continue
        wage = info["wage"]
        if world.save.pilot.credits >= wage:
            world.save.pilot.credits -= wage
            _ledger(world).wages += wage
        else:
            setattr(ship, f"has_{role}", False)
            msg = f"Your {info['label']} resigns -- you can't cover their wages."
            world.save.pilot.note(msg)
            messages.append(msg)
    return messages


# ---------------------------------------------------------------------------
# Ranks / status
# ---------------------------------------------------------------------------


def rank_for(credits: int) -> str:
    title = RANKS[0][1]
    for threshold, name in RANKS:
        if credits >= threshold:
            title = name
    return title


def check_rank_up(world: World) -> str | None:
    """Checked once per station-menu draw (the same "catches every
    path" reasoning `is_stranded`'s own check there already relies on)
    rather than wrapped around every credit-earning call site
    individually -- credits change in enough places (trading, missions,
    bounties, landmarks, patrol fines) that hooking each one would be
    far more invasive than noticing the promotion the next time the
    pilot is back at a menu. Returns the new rank's title if this call
    just crossed into it, else None -- fires at most once per rank,
    tracked by `Pilot.highest_rank_seen`."""
    pilot = world.save.pilot
    idx = 0
    for i, (threshold, _) in enumerate(RANKS):
        if pilot.credits >= threshold:
            idx = i
    if idx > pilot.highest_rank_seen:
        pilot.highest_rank_seen = idx
        title = RANKS[idx][1]
        pilot.highlight(f"Promoted to {title}.")
        return title
    return None


# ---------------------------------------------------------------------------
# Storage layer -- the only code in this file that touches a filesystem
# path for game state (see this module's own docstring).
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1


class SaveError(OSError):
    """A gameplay checkpoint failed; no further actions may be accepted."""


def _default_save_dir() -> Path:
    override = os.environ.get("VOIDRUNNER_SAVE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    # Not `Path(__file__).resolve().parent` -- this module ships as real
    # installed package data now (see this module's own docstring), and
    # an installed package's own directory is routinely read-only and/or
    # wiped on upgrade. `Path.home()` resolves to whatever OS user is
    # actually running NetBBS (the door sandbox's own same-OS-user
    # model), the same account that already owns the node's other state.
    try:
        home = Path.home()
    except RuntimeError:
        # Standalone callers can still remove every platform home locator.
        # NetBBS's runtime supplies one explicitly in its otherwise minimal
        # child environment, so a launched door does not take this fallback
        # merely because its scratch working directory is isolated.
        home = Path(tempfile.gettempdir())
    return home / ".netbbs" / "voidrunner_saves"


def _save_path(save_dir: Path, user_id: int) -> Path:
    return save_dir / f"{user_id}.json"


def _new_career(handle: str) -> SaveData:
    seed = random.randrange(1, 2**31 - 1)
    return SaveData(
        schema_version=SCHEMA_VERSION,
        seed=seed,
        pilot=Pilot(handle=handle, credits=1200, reputation={f: 0 for f in FACTIONS},
                    career_started=time.strftime("%Y-%m-%d")),
        ship=Ship(hull_class="Shuttle", hull_hp=60, fuel=24),
        current_system=0,
        turn=0,
        cargo={},
        discovered=[0],
        market_drift={},
        active_missions=[],
        next_mission_id=1,
        flags={},
    )


# New Game+ credit head start, per retirement, cumulative -- modest
# relative to the 250,000cr top-rank threshold that unlocks retirement
# in the first place, so it's a nice edge on the next run rather than a
# shortcut that trivializes it.
RETIREMENT_STARTING_CREDITS_BONUS = 500


def retire_pilot(old_save: SaveData) -> SaveData:
    """New Game+, available once a pilot reaches the top rank
    (`RANKS`'s own last entry -- see `screen_status`'s own eligibility
    check). Reuses `_new_career` almost entirely -- a genuinely fresh
    run: new seed (a different galaxy to explore, not the same map
    memorized), fresh ship/credits/reputation/notoriety/kills/missions,
    an empty log. Deliberately not a "New Game+ carries most things
    forward" design -- `retirements` (incremented) and its own small,
    cumulative starting-credit bonus are the *only* things that survive
    the reset, the "legacy" this feature is actually about; everything
    else restarting is what makes it a real new run rather than the same
    character continuing under a different name."""
    retirements = old_save.pilot.retirements + 1
    new_save = _new_career(old_save.pilot.handle)
    new_save.best_credits = max(old_save.best_credits, old_save.pilot.credits)
    new_save.pilot.retirements = retirements
    new_save.pilot.credits += retirements * RETIREMENT_STARTING_CREDITS_BONUS
    new_save.pilot.note(f"Retired as a {RANKS[-1][1]} (retirement #{retirements}) -- a new career begins.")
    new_save.pilot.highlight(f"Retired as a {RANKS[-1][1]} (retirement #{retirements}).")
    return new_save


MAX_SAVE_BYTES = 4 * 1024 * 1024
MAX_RECOVERY_COPIES = 8


def _previous_save_path(save_dir: Path, user_id: int) -> Path:
    return save_dir / f"{user_id}.previous.json"


def _read_save_bytes(path: Path) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(MAX_SAVE_BYTES + 1)
    if len(data) > MAX_SAVE_BYTES:
        raise ResumeError("The saved career exceeds the supported file size.")
    return data


def _unique_save_object(pairs) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ResumeError("The saved career contains duplicate fields.")
        result[key] = value
    return result


def _decode_career(raw: bytes) -> SaveData:
    """Verify a restart without committing or normalizing the stored document."""
    try:
        data = json.loads(raw, object_pairs_hook=_unique_save_object)
        save = SaveData.from_dict(data)
        # Validate journey relationships and RNG without normalizing contract IDs.
        _validate_pending_travel_consistency(save)
        if save.event_rng_state is not None:
            version, state, gaussian = save.event_rng_state
            if type(version) is int and version > random.Random.VERSION:
                raise UnsupportedSave("The saved random state requires a newer runtime.")
            if gaussian is not None and (type(gaussian) not in (int, float) or not math.isfinite(gaussian)):
                raise ValueError("invalid random state")
            random.Random().setstate((version, tuple(state), gaussian))
        elif save.pending_travel is not None:
            raise ResumeError("The interrupted journey has no saved random state.")
        return save
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError) as exc:
        raise ResumeError("The saved career cannot be decoded safely.") from exc


def load_or_create_save(save_dir: Path, user_id: int, handle: str) -> tuple[SaveData, bool, str | None]:
    """Only an absent career with no previous checkpoint may start fresh."""
    path = _save_path(save_dir, user_id)
    try:
        raw = _read_save_bytes(path)
    except FileNotFoundError:
        try:
            _previous_save_path(save_dir, user_id).stat()
        except FileNotFoundError:
            return _new_career(handle), True, None
        except OSError as exc:
            raise ResumeError("The previous checkpoint cannot be inspected.") from exc
        raise ResumeError("The career file is missing; a previous checkpoint exists.")
    except OSError as exc:
        raise ResumeError("The career file could not be read.") from exc
    return _decode_career(raw), False, None


def _recovery_original(save_dir: Path, user_id: int) -> bytes | None:
    """Read-only preservation preflight, repeated immediately before rollback."""
    try:
        original = _read_save_bytes(_save_path(save_dir, user_id))
    except FileNotFoundError:
        return None
    # A newer build's career must be opened with that build, never downgraded.
    try:
        _decode_career(original)
    except UnsupportedSave:
        raise
    except ResumeError:
        pass
    if len(list(save_dir.glob(f"{user_id}.recovery-*.json"))) >= MAX_RECOVERY_COPIES:
        raise ResumeError("Recovery copies are full; ask your SysOp to archive them.")
    return original


def restore_previous_career(save_dir: Path, user_id: int, expected: bytes) -> SaveData:
    """Under the session lease, preserve the current file before explicit rollback."""
    previous = _read_save_bytes(_previous_save_path(save_dir, user_id))
    if previous != expected:
        raise ResumeError("The previous checkpoint changed; reopen recovery to inspect it.")
    restored = _decode_career(previous)
    path = _save_path(save_dir, user_id)
    original = _recovery_original(save_dir, user_id)
    if original is not None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=save_dir, prefix=f".{user_id}.recovery-",
                                             suffix=".tmp", delete=False) as archive:
                temporary = Path(archive.name)
                archive.write(original)
                archive.flush()
                os.fsync(archive.fileno())
            # Only complete, closed archives enter the retained recovery namespace.
            retained = temporary.with_name(temporary.name[1:]).with_suffix(".json")
            os.replace(temporary, retained)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    _write_bytes_atomic(path, previous)
    return restored


class PilotBusy(Exception):
    """Another process owns this pilot's complete read/play/write session."""


@contextlib.contextmanager
def _file_lease(path: Path, *, wait: float = 0):
    """Hold a stable OS lock; never unlink its inode while another opener exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            # Windows permits a byte-range lock beyond EOF. Writing a dummy
            # byte first races with another opener that already owns the lock.
            handle.seek(0)
            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            acquire = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        deadline = time.monotonic() + wait
        while True:
            try:
                acquire()
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise PilotBusy from exc
                time.sleep(0.02)
        # Closing the descriptor releases the lock on every exit, including a
        # killed process. Keep the file itself: unlinking it could split owners.
        yield


@contextlib.contextmanager
def _maintenance_gate(save_dir: Path):
    # Restore preserves this directory and its lock inodes, switching only data.
    # Existing service-owned directories need no write access to their parent.
    with _file_lease(save_dir / ".maintenance.lock", wait=1):
        yield


@contextlib.contextmanager
def pilot_session(save_dir: Path, user_id: int):
    save_dir = save_dir.resolve()
    with contextlib.ExitStack() as lease:
        with _maintenance_gate(save_dir):
            lease.enter_context(_file_lease(save_dir / f".{user_id}.lock"))
        yield


@contextlib.contextmanager
def maintenance_session(save_dir: Path):
    """Exclude new launches and refuse maintenance while any pilot is active."""
    save_dir = save_dir.resolve()
    with _maintenance_gate(save_dir):
        # Probe every stable session lock while the gate prevents new owners.
        # The gate prevents new owners, so each probe can close immediately.
        # Permanent pilot files must not consume one descriptor per past caller.
        for path in save_dir.glob(".*.lock"):
            if re.fullmatch(r"\.[0-9]+\.lock", path.name):
                with _file_lease(path):
                    pass
        yield


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent,
            prefix=f".{path.stem}-", suffix=".tmp", delete=False,
        ) as handle:
            tmp = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)


def _write_json_atomic(path: Path, data: dict) -> None:
    _write_bytes_atomic(path, json.dumps(data).encode("utf-8"))


def write_save(save_dir: Path, user_id: int, save: SaveData) -> None:
    """Retain the preceding readable checkpoint under the pilot session lease."""
    path = _save_path(save_dir, user_id)
    new = json.dumps(save.to_dict()).encode("utf-8")
    try:
        if len(new) > MAX_SAVE_BYTES:
            raise ResumeError("The career exceeds the supported file size.")
        _decode_career(new)
    except ResumeError as exc:
        raise SaveError("Refusing to write an invalid career checkpoint.") from exc
    try:
        old = _read_save_bytes(path)
    except FileNotFoundError:
        old = None
    if old == new:
        return
    if old is not None:
        try:
            _decode_career(old)
        except ResumeError as exc:
            raise SaveError("Refusing to replace an unreadable saved career.") from exc
        _write_bytes_atomic(_previous_save_path(save_dir, user_id), old)
    _write_bytes_atomic(path, new)


HALL_OF_FAME_SIZE = 20


def _score_entry(data, user_id: int | None = None) -> dict | None:
    """Discard malformed flavor data before sorting or rendering it."""
    if not isinstance(data, dict) or not isinstance(data.get("handle"), str):
        return None
    entry = {key: data.get(key, 0) for key in
             ("user_id", "best_credits", "retirements", "kills", "missions_completed")}
    if "user_id" not in data or any(type(value) is not int or value < 0 for value in entry.values()):
        return None
    if user_id is not None and entry["user_id"] != user_id:
        return None
    entry["handle"] = data["handle"]
    entry["rank"] = rank_for(entry["best_credits"])
    return entry


def _read_score_json(path: Path, limit: int = 65536):
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
        if len(raw) <= limit:
            return json.loads(raw)
    except (OSError, ValueError):
        pass
    return None


def _legacy_scores(save_dir: Path) -> dict[int, dict]:
    data = _read_score_json(save_dir / "leaderboard.json", 2 * 1024 * 1024)
    entries = {}
    for item in data if isinstance(data, list) else []:
        entry = _score_entry(item)
        if entry is not None:
            prior = entries.get(entry["user_id"])
            if prior is None or entry["best_credits"] > prior["best_credits"]:
                entries[entry["user_id"]] = entry
    return entries


def _pilot_score(save_dir: Path, user_id: int) -> dict | None:
    return _score_entry(_read_score_json(save_dir / "scores" / f"{user_id}.json"), user_id)


def load_hall_of_fame(save_dir: Path) -> list[dict]:
    """Display the top 20 without discarding any independent pilot record."""
    entries = _legacy_scores(save_dir)
    try:
        for path in (save_dir / "scores").glob("*.json"):
            if not path.stem.isascii() or not path.stem.isdecimal():
                continue
            entry = _score_entry(_read_score_json(path), int(path.stem))
            if entry is not None:
                prior = entries.get(entry["user_id"])
                if prior:
                    entry["best_credits"] = max(entry["best_credits"], prior["best_credits"])
                    entry["rank"] = rank_for(entry["best_credits"])
                entries[entry["user_id"]] = entry
    except OSError:
        pass
    return sorted(entries.values(), key=lambda e: (-e["best_credits"], e["user_id"]))[:HALL_OF_FAME_SIZE]


def update_hall_of_fame(save_dir: Path, user_id: int, save: SaveData) -> None:
    """Under the pilot session lock, replace only this pilot's optional record."""
    legacy = _legacy_scores(save_dir).get(user_id, {})
    prior = _pilot_score(save_dir, user_id) or {}
    pilot = save.pilot
    best = max(save.best_credits, pilot.credits, prior.get("best_credits", 0), legacy.get("best_credits", 0))
    entry = {"user_id": user_id, "handle": pilot.handle, "best_credits": best,
             "rank": rank_for(best), "retirements": pilot.retirements,
             "kills": pilot.kills, "missions_completed": pilot.missions_completed}
    try:
        _write_json_atomic(save_dir / "scores" / f"{user_id}.json", entry)
    except OSError:
        pass  # A later checkpoint repairs this optional projection of the save.


def persist(world: World, save_dir: Path, user_id: int) -> None:
    world.sync_discovered()
    world.save.event_rng_state = world.event_rng.getstate()
    prior = _pilot_score(save_dir, user_id) or {}
    legacy = _legacy_scores(save_dir).get(user_id, {})
    world.save.best_credits = max(world.save.best_credits, world.save.pilot.credits,
                                  prior.get("best_credits", 0), legacy.get("best_credits", 0))
    write_save(save_dir, user_id, world.save)
    update_hall_of_fame(save_dir, user_id, world.save)


# ---------------------------------------------------------------------------
# UI layer -- everything below here is the only code allowed to touch
# sys.stdin/sys.stdout directly.
# ---------------------------------------------------------------------------

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _vis_len(text: str) -> int:
    """Measure display width by stripping ANSI SGR escape sequences."""
    return len(_ANSI_RE.sub("", text))


def _box_inner_width() -> int:
    return max(1, min(77, _OUTPUT_WIDTH - 2))


def _box_outer_width() -> int:
    return _box_inner_width() + 2


def _box_title(p: "Palette", text: str, width: int | None = None, border_color: str | None = None) -> str:
    """A `╭── {text} ───...───╮` title border sized so its right corner
    always lands on the same column as every other row in the same box
    (see the module-wide 77-visible-column interior convention) --
    `text` routinely embeds a variable-length station/system name or
    pilot handle, so a fixed literal dash count (this box family's
    original, hand-typed approach) drifts out of alignment with the
    box's other rows for any name that isn't exactly the length the
    dashes were originally counted for."""
    width = _box_inner_width() if width is None else min(width, _box_inner_width())
    head = f"── {text} "
    dashes = "─" * max(1, width - _vis_len(head))
    col = border_color if border_color is not None else p.accent
    return f"{col}{BOLD}╭{head}{dashes}╮{RESET}"


def _box_divider(p: "Palette", width: int | None = None, border_color: str | None = None) -> str:
    width = _box_inner_width() if width is None else min(width, _box_inner_width())
    col = border_color if border_color is not None else p.accent
    return f"{col}├{'─' * width}┤{RESET}"


def _box_bottom(p: "Palette", width: int | None = None, border_color: str | None = None) -> str:
    width = _box_inner_width() if width is None else min(width, _box_inner_width())
    col = border_color if border_color is not None else p.accent
    return f"{col}╰{'─' * width}╯{RESET}"


def _pad(text: str, width: int, align: str = "left") -> str:
    """Pad text containing ANSI codes to a target visual column width."""
    diff = max(0, width - _vis_len(text))
    if align == "right":
        return (" " * diff) + text
    if align == "center":
        left = diff // 2
        return (" " * left) + text + (" " * (diff - left))
    return text + (" " * diff)


def _gauge_bar(val: int, max_val: int, width: int = 10, p: Palette | None = None) -> str:
    """Render a tactical CRT-style gauge bar with dynamic threshold colors."""
    if max_val <= 0:
        pct = 0.0
    else:
        pct = max(0.0, min(1.0, val / max_val))
    filled = round(pct * width)
    empty = width - filled
    if p:
        col = p.correct if pct > 0.5 else (p.gold if pct > 0.2 else p.wrong)
        return f"{col}{'■' * filled}{p.muted}{'░' * empty}{RESET}"
    return f"[{'■' * filled}{'░' * empty}]"


def draw_status_bar(p: Palette, world: World) -> None:
    ship = world.save.ship
    pilot = world.save.pilot
    danger = world.here.danger
    danger_badge = (
        f"{p.correct}[SECURE]{RESET}" if danger == 0
        else (f"{p.gold}[CAUTION 1]{RESET}" if danger == 1 else f"{p.wrong}[DANGER {danger}]{RESET}")
    )
    cap = cargo_capacity(ship)
    used = sum(world.save.cargo.values())
    w = _box_outer_width()

    line1 = (
        f"{p.accent}{BOLD}{world.here.station_name}{RESET} "
        f"{p.muted}({world.here.economy} │ Sector: {sector_for(world.here)}){RESET}  {danger_badge}"
    )
    out_line(line1)
    fields = [
        f"{p.gold}{BOLD}{pilot.credits:,} cr{RESET}",
        f"{p.accent}Hull{RESET} {_gauge_bar(ship.hull_hp, hull_hp_max(ship), 8, p)} {ship.hull_hp}/{hull_hp_max(ship)}",
        f"{p.accent}Fuel{RESET} {_gauge_bar(ship.fuel, fuel_capacity(ship), 8, p)} {ship.fuel}/{fuel_capacity(ship)}",
        f"{p.accent}Hold{RESET} {_gauge_bar(used, cap, 6, p)} {used}/{cap}",
        f"{p.muted}Day {world.save.turn}{RESET}",
    ]
    row = "  "
    for field in fields:
        candidate = field if row == "  " else f"{row}  │  {field}"
        if row != "  " and _visible_width(candidate) > w:
            out_line(row)
            row = f"  {field}"
        else:
            row = f"  {field}" if row == "  " else candidate
    out_line(row)
    out_line(f"{p.muted}{'─' * w}{RESET}")


def screen_title(p: Palette, info: dict) -> None:
    inner_w = _box_inner_width()
    tagline = "[ a NetBBS door game ]"
    top_dashes = max(1, inner_w - len(tagline) - 2)
    top_border = (
        f"{p.title}{BOLD}╔"
        + "═" * top_dashes
        + f"{p.muted}[ {p.accent}a NetBBS door game{p.muted} ]"
        + f"{p.title}{BOLD}══╗{RESET}"
    )

    logo_l1 = "█░░█ █▀▀█ ▀█▀ █▀▀▄   █▀▀▄ █░░█ █▄░█ █▄░█ █▀▀ █▀▀▄"
    logo_l2 = "░▀▄▀ █▄▄█ ░█░ █▄▄▀   █░▀▄ █▄▄█ █░▀█ █░▀█ ██▄ █░▀▄"
    sub = f"{p.accent}Tactical Deep-Space Trading & Exploration{RESET}"

    node = info.get("node_name", "NetBBS")
    handle = info.get("handle", "Pilot")
    meta = (
        f"  {p.muted}NODE:{RESET} {p.title}{node}{RESET}  │  "
        f"{p.muted}PILOT:{RESET} {p.gold}{handle}{RESET}  │  "
        f"{p.muted}GALAXY:{RESET} {p.title}48 Star Systems{RESET}"
    )
    if _vis_len(meta) > inner_w:
        meta = (
            f"  {p.muted}NODE:{RESET} {p.title}{node[:16]}{RESET}  │  "
            f"{p.muted}PILOT:{RESET} {p.gold}{handle[:16]}{RESET}  │  "
            f"{p.muted}GALAXY:{RESET} {p.title}48 Systems{RESET}"
        )

    out_line()
    out_line(top_border)
    out_line(f"{p.title}{BOLD}║{RESET}{' ' * inner_w}{p.title}{BOLD}║{RESET}")
    out_line(f"{p.title}{BOLD}║{RESET}{_pad(f'{p.gold}{BOLD}{logo_l1}{RESET}', inner_w, 'center')}{p.title}{BOLD}║{RESET}")
    out_line(f"{p.title}{BOLD}║{RESET}{_pad(f'{p.gold}{BOLD}{logo_l2}{RESET}', inner_w, 'center')}{p.title}{BOLD}║{RESET}")
    out_line(f"{p.title}{BOLD}║{RESET}{_pad(f'{p.gold}{BOLD}V O I D R U N N E R{RESET}', inner_w, 'center')}{p.title}{BOLD}║{RESET}")
    out_line(f"{p.title}{BOLD}║{RESET}{' ' * inner_w}{p.title}{BOLD}║{RESET}")
    out_line(f"{p.title}{BOLD}║{RESET}{_pad(sub, inner_w, 'center')}{p.title}{BOLD}║{RESET}")
    out_line(f"{p.title}{BOLD}║{RESET}{' ' * inner_w}{p.title}{BOLD}║{RESET}")
    out_line(f"{p.title}{BOLD}╠{'═' * inner_w}╣{RESET}")
    out_line(f"{p.title}{BOLD}║{RESET}{_pad(meta, inner_w, 'left')}{p.title}{BOLD}║{RESET}")
    out_line(f"{p.title}{BOLD}╚{'═' * inner_w}╝{RESET}")
    out_line(f"{p.muted}A {info.get('node_name', 'NetBBS')} space trading door.{RESET}")
    out_line()


def create_career(p: Palette, info: dict) -> str | None:
    out_line()
    out_line(_box_title(p, "Pilot Commission Registration"))
    welcome = f"  {p.gold}Welcome to the void, pilot.{RESET} No career dossier found for {info['handle']}."
    pad_len = max(0, 77 - _vis_len(welcome))
    out_line(f"{p.accent}│{RESET}{welcome}{' ' * pad_len}{p.accent}│{RESET}")
    out_line(_box_bottom(p))
    out_prompt(f"  {p.muted}Pilot callsign [{info['handle']}]: {RESET}")
    entered = read_line_raw(max_len=16, allowed=lambda c: c.isalnum() or bool(unicodedata.combining(c)) or c == " ").strip()
    callsign = entered or info["handle"]
    out_line()
    out_line(f"{p.muted}  Starting deployment: Freeport Anchorage{RESET}")
    out_line(f"{p.muted}  Vessel: Battered Shuttle  │  Starting Bank: 1,200 cr  │  Cargo: Empty Hold{RESET}")
    if confirm(f"Launch {callsign}'s career?", p):
        return callsign
    out_line(f"{p.muted}Career launch cancelled. No career was saved.{RESET}")
    return None


def screen_station_menu(p: Palette, world: World) -> str:
    completed = settle_futures_contracts(world)
    completed += check_mission_completions(world)
    if completed:
        world.checkpoint()
    for msg in completed:
        out_line(f"{p.gold}{msg}{RESET}")
    if is_stranded(world):
        # Checked here, not only right after the action that could cause
        # it -- this is the outer loop's own home base, reached after
        # every single action, so it catches every path into the stuck
        # state (a refuel/repair that spent the last credits, a jump
        # that burned the last fuel with no encounter) in one place.
        out_line()
        rescued = rescue_stranded_pilot(world)
        world.checkpoint()
        out_line(f"{p.wrong}{rescued}{RESET}")
        pause(p)
    promoted = check_rank_up(world)
    if promoted:
        world.checkpoint()
        out_line()
        out_line(f"{p.gold}{BOLD}★ ★ ★ Promoted to {promoted}! ★ ★ ★{RESET}")
        pause(p)
    out_line()
    draw_status_bar(p, world)
    _show_tracked_mission(p, world)

    out_line(_box_title(p, f"Station Services: {world.here.station_name}"))
    menu_rows = [
        f"   {p.gold}[M]{RESET} Commodity Market     {p.gold}[Y]{RESET} Engineering Yard     {p.gold}[B]{RESET} Mission Board",
        f"   {p.gold}[C]{RESET} Navigation Chart     {p.gold}[S]{RESET} Pilot Status         {p.gold}[H]{RESET} Hall of Fame",
        f"   {p.gold}[G]{RESET} Pilot Guide          {p.gold}[T]{RESET} Trading Ledger       {p.gold}[Q]{RESET} Disembark & Save",
    ]
    for row in menu_rows:
        pad_len = max(0, 77 - _vis_len(row))
        out_line(f"{p.accent}│{RESET}{row}{' ' * pad_len}{p.accent}│{RESET}")

    special_ops = []
    if landmark_available_here(world):
        special_ops.append(f"{p.gold}[L]{RESET} {world.landmark['label']}")
    if has_contraband(world):
        special_ops.append(f"{p.wrong}[D]{RESET} Dump Contraband")
    if concord_commission_available(world):
        special_ops.append(f"{p.accent}[P]{RESET} Privateer Commission")
    if blackwake_made_available(world):
        special_ops.append(f"{p.accent}[W]{RESET} Welcome to the Wake")

    if special_ops:
        out_line(_box_divider(p))
        for op in special_ops:
            row = f"   {op}"
            pad_len = max(0, 77 - _vis_len(row))
            out_line(f"{p.accent}│{RESET}{row}{' ' * pad_len}{p.accent}│{RESET}")
    out_line(_box_bottom(p))
    out_prompt(f"  {p.gold}Command Deck{RESET} {p.muted}> {RESET}")
    choice = read_command()
    out_line(choice)
    return choice


def landmark_available_here(world: World) -> bool:
    return (world.here.id == world.landmark["system_id"]
            and not world.save.flags.get("landmark_investigated"))


def screen_dump_contraband(p: Palette, world: World) -> None:
    items = {c: q for c, q in world.save.cargo.items() if not COMMODITIES[c]["legal"]}
    if not items:
        return
    listing = ", ".join(f"{q} {COMMODITIES[c]['label']}" for c, q in items.items())
    out_line(f"{p.wrong}This forfeits {listing} for good -- no sale, no refund.{RESET}")
    if not confirm("Dump it all now?", p):
        return
    result = dump_all_contraband(world)
    world.checkpoint()
    out_line(f"{p.muted}{result}{RESET}")


def screen_landmark(p: Palette, world: World) -> None:
    landmark = world.landmark
    out_line()
    out_line(f"{p.accent}{BOLD}{landmark['label']}{RESET}")
    out_line(f"  {landmark['flavor']}")
    world.save.flags["landmark_investigated"] = True
    world.save.pilot.credits += landmark["reward_credits"]
    world.save.pilot.note(f"Investigated {landmark['label']} (+{landmark['reward_credits']}cr)")
    world.save.pilot.highlight(f"Investigated {landmark['label']} (+{landmark['reward_credits']}cr).")
    world.checkpoint()
    out_line(f"{p.gold}Salvage recovered: +{landmark['reward_credits']}cr{RESET}")
    pause(p)


def screen_market(p: Palette, world: World) -> None:
    system = world.here
    while True:
        out_line()
        out_line(_box_title(p, f"Interstellar Commodity Exchange: {system.station_name}"))
        header = f" {p.gold}KEY  COMMODITY            BUY/UNIT    SELL/UNIT    SPREAD   IN HOLD  STATUS{RESET}"
        out_line(f"{p.accent}│{RESET}{header}{' ' * max(0, 77 - _vis_len(header))}{p.accent}│{RESET}")
        out_line(f"{p.accent}├─────────────────────────────────────────────────────────────────────────────┤{RESET}")
        rows: list[tuple[str, str]] = []
        goods = LEGAL_COMMODITIES + [c for c in CONTRABAND_COMMODITIES if system.economy == "Haven" or world.save.cargo.get(c, 0) > 0]
        for commodity in goods:
            buy = price_for(world, system.id, commodity)
            sell = round(buy * SELL_SPREAD)
            spread = sell - buy
            have = world.save.cargo.get(commodity, 0)
            label = COMMODITIES[commodity]["label"]

            tag = ""
            if not COMMODITIES[commodity]["legal"]:
                tag = f"{p.wrong}Illegal{RESET}"
            event = world.save.active_event
            if event and event["commodity"] == commodity and event["economy"] == system.economy:
                tag = (f"{p.wrong}[CRASH]{RESET}" if event["direction"] == "crash"
                        else f"{p.correct}[BOOM]{RESET}")
            if not tag:
                tag = f"{p.muted}Normal{RESET}"

            key_char = LETTERS[len(rows)]
            row_str = (
                f"  {p.gold}[{key_char}]{RESET}  {label:<18} "
                f"{buy:>6} cr   {sell:>6} cr   {spread:>5} cr   "
                f"{have:>5}   {tag}"
            )
            rows.append((commodity, row_str))
            pad_len = max(0, 77 - _vis_len(row_str))
            out_line(f"{p.accent}│{RESET}{row_str}{' ' * pad_len}{p.accent}│{RESET}")

        out_line(f"{p.accent}╰─────────────────────────────────────────────────────────────────────────────╯{RESET}")
        cap = cargo_capacity(world.save.ship)
        used = sum(world.save.cargo.values())
        hold_bar = _gauge_bar(used, cap, 10, p)
        out_line(f"  {p.accent}Cargo Hold:{RESET} {hold_bar} {used}/{cap} units   │   {p.gold}[X]{RESET} Futures Exchange")
        if any(not COMMODITIES[c]["legal"] for c in goods):
            out_line("Blackwake: +1 standing per new 500cr net contraband trading gain; purchases count against gains.")
        out_prompt(f"  {p.muted}Trade which [A-{LETTERS[len(rows)-1]}], or [Q] back? {RESET}")
        key = read_command()
        out_line(key)
        if key == "Q":
            return
        if key == "X":
            futures_goods = [c for c in goods if COMMODITIES[c]["legal"] or world.here.economy == "Haven"]
            screen_futures(p, world, futures_goods)
            continue
        idx = LETTERS.index(key) if key in LETTERS else -1
        if idx < 0 or idx >= len(rows):
            continue
        commodity, _ = rows[idx]
        _trade_commodity(p, world, commodity)


def screen_futures(p: Palette, world: World, goods: list[str]) -> None:
    page = 0
    while True:
        entries = [("goods", c) for c in goods] + [("order", c) for c in world.save.active_futures]
        count = max(1, (len(entries) + 3) // 4)
        page = min(page, count - 1)
        visible = entries[page * 4:page * 4 + 4]
        out_line()
        out_line(f"{p.gold}Futures Exchange - {page + 1}/{count}{RESET}")
        out_line("New orders: station pickup, 8% nonrefundable fee rounded up per unit.")
        for index, (kind, item) in enumerate(visible, 1):
            if kind == "goods":
                principal, fee = futures_quote(world, item, 1)
                out_line(f"[{index}] Order {COMMODITIES[item]['label']}: {principal}+{fee}cr/unit")
            else:
                status = "ready" if world.save.turn >= item.settle_turn else f"day {item.settle_turn}"
                place = "legacy remote delivery" if item.origin_system is None else world.by_id[item.origin_system].name
                out_line(_mission_plain(f"[{index}] #{item.id}: {item.quantity} {COMMODITIES[item.commodity]['label']}, {status}; {place}"))
        out_line(f"Outstanding orders: {len(world.save.active_futures)}/{MAX_FUTURES_CONTRACTS}")
        out_prompt("[1-4] Details [N]ext [P]rev [B]ack > ")
        key = read_command()
        if key in ("B", "Q"):
            return
        if key == "N":
            page = min(page + 1, count - 1)
        elif key == "P":
            page = max(0, page - 1)
        elif len(key) == 1 and "1" <= key <= "4" and int(key) <= len(visible):
            kind, item = visible[int(key) - 1]
            if kind == "goods":
                _screen_buy_futures(p, world, item)
            else:
                _screen_futures_order(p, world, item)


def _screen_futures_order(p: Palette, world: World, contract: FuturesContract) -> None:
    while True:
        out_line()
        out_line(f"Order #{contract.id}: {contract.quantity} {COMMODITIES[contract.commodity]['label']}")
        if contract.origin_system is None:
            out_line("Legacy order: original remote delivery/full-refund terms apply.")
            out_line(f"Settles on day {contract.settle_turn}; paid {contract.locked_price}cr.")
            out_prompt("[B]ack > ")
        else:
            fee = contract.locked_price - contract.principal
            out_line(_mission_plain(f"Pickup: {world.by_id[contract.origin_system].name}, from day {contract.settle_turn}."))
            out_line(f"Paid {contract.principal}cr for goods + {fee}cr nonrefundable fee.")
            out_line("Collected on arrival/station entry when the full order fits; otherwise it waits.")
            out_line(f"[X] Cancel: refund {contract.principal}cr, forfeit {fee}cr fee.")
            out_prompt("[B]ack > ")
        key = read_command()
        if key in ("B", "Q"):
            return
        if key == "X" and contract.origin_system is not None:
            if confirm(f"Cancel order #{contract.id} for {contract.principal}cr? Fee is not refunded.", p):
                try:
                    message = cancel_futures_contract(world, contract.id)
                except TradeError as exc:
                    out_line(str(exc))
                    pause(p)
                    return
                world.checkpoint()
                out_line(message)
                pause(p)
                return


def _screen_buy_futures(p: Palette, world: World, commodity: str) -> None:
    quantity, duration = 1, FUTURES_DURATIONS[0]
    while True:
        principal, fee = futures_quote(world, commodity, quantity)
        out_line()
        out_line(f"Order {COMMODITIES[commodity]['label']}")
        out_line(f"Quantity: {quantity}; term: {duration} days.")
        out_line(_mission_plain(f"Pickup: {world.here.name}, from day {world.save.turn + duration}."))
        out_line(f"Goods {principal}cr + nonrefundable fee {fee}cr = {principal + fee}cr.")
        out_line(f"Credits: {world.save.pilot.credits}cr. Full cargo space needed only at pickup.")
        out_line("Cancellation refunds goods principal only; full holds leave orders waiting.")
        out_prompt("[Q] Quantity [T] Term [S] Sign [B] Back > ")
        key = read_command()
        if key == "B":
            return
        if key == "Q":
            out_prompt(f"Quantity (1-{cargo_capacity(world.save.ship)}, Enter keeps {quantity}): ")
            raw = read_line_raw(max_len=5)
            if raw:
                chosen = int(raw)
                if 1 <= chosen <= cargo_capacity(world.save.ship):
                    quantity = chosen
                else:
                    out_line("Quantity must fit your ship's cargo capacity.")
                    pause(p)
        elif key == "T":
            duration = FUTURES_DURATIONS[(FUTURES_DURATIONS.index(duration) + 1) % len(FUTURES_DURATIONS)]
        elif key == "S":
            if confirm(f"Pay {principal + fee}cr now, including the nonrefundable {fee}cr fee?", p):
                try:
                    message = buy_futures_contract(world, commodity, quantity, duration)
                except TradeError as exc:
                    out_line(str(exc))
                    pause(p)
                    continue
                world.checkpoint()
                out_line(f"{p.correct}{message}{RESET}")
                pause(p)
                return


def trading_ledger_lines(world: World) -> list[str]:
    ledger = world.save.trading_ledger
    lines = [
        "[B]ack returns to the deck. This ledger is read-only.",
        f"Credits available: {world.save.pilot.credits:,}cr.",
        (f"Recorded activity since day {ledger.since_day}; today is day {world.save.turn}."
         if ledger.since_day is not None else "No activity recorded yet. Earlier career costs are unknown."),
        f"Market sales with known cost: receipts {ledger.sales_revenue:,}cr - cargo {ledger.sales_cost:,}cr = margin {ledger.sales_revenue - ledger.sales_cost:+,}cr.",
        f"Sales with unknown cargo cost: {ledger.uncosted_sales:,}cr receipts; profit unknown.",
        f"Deliveries with known cost: payment {ledger.delivery_revenue:,}cr - cargo {ledger.delivery_cost:,}cr = margin {ledger.delivery_revenue - ledger.delivery_cost:+,}cr.",
        f"Deliveries with unknown cargo cost: {ledger.uncosted_deliveries:,}cr receipts; profit unknown.",
        "Mixed delivery payments are divided by cargo quantity. Margins exclude operating costs and other career income or spending.",
        f"Cargo lost or surrendered: {ledger.cargo_loss_cost:,}cr recorded cost, plus {ledger.uncosted_losses} units of unknown cost.",
        f"Fuel purchases: {ledger.fuel_spend:,}cr. Crew wages paid: {ledger.wages:,}cr. Cancelled-order fees: {ledger.cancelled_fees:,}cr.",
        "These totals begin when recorded, exclude earlier activity, repairs, fines and crew hiring, and are not total career profit.",
        "HOLD - older unknown cargo is consumed first, then recorded purchases in order. Futures costs include brokerage.",
    ]
    for commodity, quantity in world.save.cargo.items():
        lots = world.save.cargo_basis.get(commodity, [])
        known = sum(lot[0] for lot in lots)
        cost = sum(lot[1] for lot in lots)
        lines.append(f"{COMMODITIES[commodity]['label']}: {quantity} units; {known} costed at {cost:,}cr total; {quantity - known} with unknown cost.")
    if not world.save.cargo:
        lines.append("Hold empty.")
    wage = sum(info["wage"] for role, info in CREW_ROLES.items() if getattr(world.save.ship, f"has_{role}"))
    lines.extend([
        f"TRAVEL - tank {world.save.ship.fuel}/{fuel_capacity(world.save.ship)} units. Replacement fuel costs 6cr/unit; current crew wages {wage}cr/jump.",
        f"LOCAL MARKET - {world.here.name}, {world.here.economy}.",
        "Produces: " + ", ".join(COMMODITIES[c]["label"] for c in ECONOMY_PRODUCES[world.here.economy]) + ".",
        "Demands: " + ", ".join(COMMODITIES[c]["label"] for c in ECONOMY_DEMANDS[world.here.economy]) + ".",
    ])
    return lines


def _trade_pages(lines: list[str], title: str, footer: str) -> list[list[str]]:
    width = max(1, _OUTPUT_WIDTH - 1)
    overhead = (len(_wrap_output(footer, width).split("\r\n"))
                + len(_wrap_output(title + " 999/999", width).split("\r\n")) + 3)
    return _mission_text_pages(lines, overhead=overhead)


def remembered_market_lines(world: World) -> list[str]:
    lines = ["Last observed prices, not live remote data. Jumps advance the day; prices can change."]
    for sid, quotes in sorted(world.save.market_memory.items(), key=lambda item: world.by_id[item[0]].name):
        system = world.by_id[sid]
        lines.append(f"{system.name} - {system.economy}.")
        for commodity, quote in quotes.items():
            buy = f"{quote['buy']}cr" if quote["buy"] is not None else "prohibited"
            lines.append(f"{COMMODITIES[commodity]['label']}: buy {buy}, sell {quote['sell']}cr. Day {quote['day']} ({world.save.turn - quote['day']} days old).")
    if not world.save.market_memory:
        lines.append("No market observations yet. Older discoveries have no recorded quotes.")
    return lines


def trade_route_lines(world: World, destination: int | None, commodity: str, quantity: int, use_hold: bool) -> list[str]:
    name = world.by_id[destination].name if destination is not None else "not selected"
    lines = [f"Destination: {name}. Cargo: {COMMODITIES[commodity]['label']} x{quantity}.",
             f"Source: {'existing hold cargo' if use_hold else 'buy new cargo here'}. Credits: {world.save.pilot.credits:,}cr.",
             "Use [E]dit draft to change the route. Back makes no changes; this estimate never buys cargo or launches a route."]
    if destination is None:
        return lines + ["No other station has remembered prices yet. Visit another station or receive a trader's market report first."]
    try:
        quote = trade_route_quote(world, destination, commodity, quantity, use_hold=use_hold)
    except TradeError as exc:
        return lines + [str(exc)]
    age = world.save.turn - quote["observed_day"]
    lines.extend([
        f"Destination sell quote: {quote['unit_sale']}cr/unit, observed day {quote['observed_day']} ({age} days old).",
        f"Sale receipts if the full load arrives: {quote['receipts']:,}cr.",
        f"Cargo acquisition cost: {quote['cargo_cost']:,}cr recorded; {quote['unknown_units']} units with unknown cost.",
        f"Buy now: {quote['procurement']:,}cr. Credits after buying: {world.save.pilot.credits - quote['procurement']:,}cr.",
        f"Fuel: {quote['fuel']} units, replacement value {quote['fuel'] * 6}cr. Additional fuel cash: {quote['fuel_cash']}cr with your current tank.",
        f"Crew wages: {quote['wages']}cr for {len(quote['legs'])} jumps. Arrival day {world.save.turn + len(quote['legs'])} if uninterrupted.",
        f"Cash needed before sale: {quote['cash_needed']:,}cr. Budget {'covered' if quote['cash_needed'] <= world.save.pilot.credits else 'SHORT by ' + str(quote['cash_needed'] - world.save.pilot.credits) + 'cr'}.",
    ])
    if not quote["feasible"]:
        lines.append("INFEASIBLE: a route leg exceeds your tank capacity. Upgrade or plan a different route.")
    elif quote["margin"] is not None:
        lines.append(f"Estimated margin after cargo, replacement fuel and wages: {quote['margin']:+,}cr.")
    else:
        lines.append("Total margin unavailable: unknown cargo costs or delivery commitments affect this load.")
    if quote["conflicts"]:
        lines.append("DELIVERY CONFLICT: these contracts may consume this commodity on the route: " + "; ".join(quote["conflicts"]) + ".")
    lines.append("ROUTE - shortest by jumps; fuel top-ups at stations are budgeted at 6cr/unit.")
    tank = world.save.ship.fuel
    previous = world.here
    for sid, burn in quote["legs"]:
        system = world.by_id[sid]
        if tank < burn and burn <= fuel_capacity(world.save.ship):
            station = previous.name if previous.discovered else "the uncharted intermediate station"
            lines.append(f"Refuel {burn - tank} units at {station} before the next leg.")
            tank = burn
        label = system.name if system.discovered else "Uncharted system"
        danger = str(system.danger) if system.discovered else "unknown"
        blocked = " BLOCKED by tank capacity." if burn > fuel_capacity(world.save.ship) else ""
        lines.append(f"Jump: {label}; {burn} fuel; danger {danger}.{blocked}")
        tank = max(0, tank - burn)
        previous = system
    lines.append("Estimate assumes the remembered sale price and intact cargo. Market changes, encounters, repairs, fines, detours and other income/spending are excluded.")
    return lines


def _pick_trade_field(title: str, options: list[tuple[object, str]]) -> object | None:
    footer = "[1-9] Select [N]ext [P]rev [B]ack: "
    budget = len(_trade_pages(["row"] * _OUTPUT_HEIGHT, title, footer)[0])
    pages = [([], [])]
    for value, label in options:
        choice = None
        for row in _wrap_output(_mission_plain(label), max(1, _OUTPUT_WIDTH - 5)).split("\r\n"):
            rows, choices = pages[-1]
            if len(rows) >= budget or (choice is None and len(choices) >= 9):
                pages.append(([], []))
                rows, choices = pages[-1]
                choice = None
            if choice is None:
                choices.append(value)
                choice = len(choices)
            rows.append(f"[{choice}] {row}")
    page = 0
    while True:
        out_line()
        out_line(f"{title} {page + 1}/{len(pages)}")
        for row in pages[page][0]:
            out_line(row)
        if not options:
            out_line("No observed destinations yet.")
        out_prompt(footer)
        key = read_command()
        out_line(key)
        if key == "B":
            return None
        if key == "N":
            page = min(page + 1, len(pages) - 1)
        elif key == "P":
            page = max(0, page - 1)
        elif len(key) == 1 and "1" <= key <= "9" and int(key) <= len(pages[page][1]):
            return pages[page][1][int(key) - 1]


def edit_door_draft(*, title: str, initial: dict, fields: list[tuple],
                    apply: Callable[[dict], object], error_type: type[Exception]) -> object | None:
    """Standalone synchronous draft editor: scalar fields, apply/back, retained errors.

    Mirrors the host resource-editor contract without importing Session/DatabaseLane
    into the self-contained door. Field edits change only the copied draft.
    """
    draft = dict(initial)
    page, error = 0, None
    footer = " ".join(f"[{key}]{label}" for key, label, _, _ in fields) + " [S]Apply [N]ext [P]rev [B]ack: "
    while True:
        lines = [f"[{key}] {label}: {display(draft)}" for key, label, display, _ in fields]
        lines += ["Apply uses these draft values. Back discards edits; no career data is written."]
        if error:
            lines.insert(0, "Cannot apply: " + error)
        pages = _trade_pages(lines, title, footer)
        page = min(page, len(pages) - 1)
        out_line()
        out_line(f"{title} {page + 1}/{len(pages)}")
        for line in pages[page]:
            out_line(line)
        out_prompt(footer)
        key = read_command()
        out_line(key)
        if key == "B":
            return None
        if key == "N":
            page = min(page + 1, len(pages) - 1)
        elif key == "P":
            page = max(0, page - 1)
        elif key == "S":
            try:
                return apply(draft)
            except error_type as exc:
                error, page = str(exc), 0
        else:
            for hotkey, _, _, edit in fields:
                if key == hotkey:
                    try:
                        edit(draft)
                        error = None
                    except error_type as exc:
                        error = str(exc)
                    page = 0
                    break


def _edit_trade_route(world: World, initial: dict) -> dict | None:
    destinations = [(sid, world.by_id[sid].name) for sid in world.save.market_memory if sid != world.here.id]
    destinations.sort(key=lambda item: item[1])

    def pick(draft, field, title, choices):
        selected = _pick_trade_field(title, choices)
        if selected is not None:
            draft[field] = selected

    def quantity(draft):
        out_prompt(f"Quantity 1-{cargo_capacity(world.save.ship)} (Enter keeps {draft['quantity']}): ")
        raw = read_line_raw(max_len=5)
        if not raw:
            return
        if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= cargo_capacity(world.save.ship):
            raise TradeError("Choose a quantity within your cargo capacity.")
        draft["quantity"] = int(raw)

    def apply(draft):
        trade_route_quote(world, **draft)
        return dict(draft)

    fields = [
        ("D", "Destination", lambda d: world.by_id[d['destination']].name if d['destination'] is not None else "not selected",
         lambda d: pick(d, "destination", "Destination", destinations)),
        ("C", "Cargo", lambda d: COMMODITIES[d['commodity']]['label'],
         lambda d: pick(d, "commodity", "Cargo", [(c, v['label']) for c, v in COMMODITIES.items()])),
        ("Q", "Quantity", lambda d: str(d['quantity']), quantity),
        ("H", "Hold source", lambda d: "existing hold cargo" if d['use_hold'] else "buy new cargo here",
         lambda d: d.update(use_hold=not d['use_hold'])),
    ]
    return edit_door_draft(title="Route Draft", initial=initial, fields=fields, apply=apply, error_type=TradeError)


def screen_trade_route(p: Palette, world: World) -> None:
    destinations = sorted((sid for sid in world.save.market_memory if sid != world.here.id), key=lambda sid: world.by_id[sid].name)
    parameters = {"destination": destinations[0] if destinations else None,
                  "commodity": "food", "quantity": 1, "use_hold": False}
    page = 0
    footer = "[E]dit draft [N]ext [P]rev [B]ack: "
    while True:
        pages = _trade_pages(trade_route_lines(world, **parameters), "Trade Route", footer)
        page = min(page, len(pages) - 1)
        out_line()
        out_line(f"Trade Route {page + 1}/{len(pages)}")
        for line in pages[page]:
            out_line(line)
        out_prompt(footer)
        key = read_command()
        out_line(key)
        if key == "B":
            return
        if key == "N":
            page = min(page + 1, len(pages) - 1)
        elif key == "P":
            page = max(0, page - 1)
        elif key == "E":
            edited = _edit_trade_route(world, parameters)
            if edited is not None:
                parameters, page = edited, 0


def screen_remembered_markets(p: Palette, world: World) -> None:
    footer = "[N]ext [P]rev [B]ack: "
    pages = _trade_pages(remembered_market_lines(world), "Market Memory", footer)
    page = 0
    while True:
        out_line()
        out_line(f"Market Memory {page + 1}/{len(pages)}")
        for line in pages[page]:
            out_line(line)
        out_prompt(footer)
        key = read_command()
        out_line(key)
        if key in ("B", "Q"):
            return
        if key == "N":
            page = min(page + 1, len(pages) - 1)
        elif key == "P":
            page = max(0, page - 1)


def screen_trading_ledger(p: Palette, world: World) -> None:
    footer = "[M]arkets [R]oute [N]ext [P]rev [B]ack: "
    pages = _trade_pages(trading_ledger_lines(world), "Trading Ledger", footer)
    page = 0
    while True:
        out_line()
        out_line(f"Trading Ledger {page + 1}/{len(pages)}")
        for line in pages[page]:
            out_line(line)
        out_prompt(footer)
        action = read_command()
        out_line(action)
        if action in ("B", "Q"):
            return
        if action == "N":
            page = min(page + 1, len(pages) - 1)
        elif action == "P":
            page = max(0, page - 1)
        elif action == "M":
            screen_remembered_markets(p, world)
        elif action == "R":
            screen_trade_route(p, world)


def _trade_commodity(p: Palette, world: World, commodity: str) -> None:
    label = COMMODITIES[commodity]["label"]
    buy = price_for(world, world.here.id, commodity)
    sell = round(buy * SELL_SPREAD)
    out_line(f"{p.accent}{label}{RESET} -- Buy {buy}cr  Sell {sell}cr")
    out_line(f"Credits: {world.save.pilot.credits}cr. Each unit bought costs {buy}cr; each sold returns {sell}cr.")
    out_prompt(f"{p.muted}[B]uy [S]ell [Q]cancel: {RESET}")
    action = read_command()
    out_line(action)
    if action == "B":
        if not COMMODITIES[commodity]["legal"] and world.here.economy != "Haven":
            out_line(f"{p.wrong}Station authorities prohibit the open purchase of contraband.{RESET}")
            return
        room = cargo_capacity(world.save.ship) - sum(world.save.cargo.values())
        affordable = world.save.pilot.credits // buy if buy else room
        max_qty = max(0, min(room, affordable))
        if max_qty <= 0:
            out_line(f"{p.wrong}No room or no credits.{RESET}")
            return
        out_prompt(f"{p.muted}Quantity (max {max_qty}, Enter to cancel): {RESET}")
        raw = read_line_raw(max_len=5)
        qty = int(raw) if raw.isdigit() else 0
        qty = min(qty, max_qty)
        if qty <= 0:
            return
        result = trade_cargo(world, commodity, qty, buying=True)
        world.checkpoint()
        out_line(f"{p.correct}{result}{RESET}")
        out_line(f"Credits remaining: {world.save.pilot.credits}cr. Cost recorded in [T] Trading Ledger.")
    elif action == "S":
        have = world.save.cargo.get(commodity, 0)
        if have <= 0:
            out_line(f"{p.wrong}You have none to sell.{RESET}")
            return
        out_prompt(f"{p.muted}Quantity (have {have}, Enter to cancel): {RESET}")
        raw = read_line_raw(max_len=5)
        qty = int(raw) if raw.isdigit() else 0
        qty = min(qty, have)
        if qty <= 0:
            return
        result = trade_cargo(world, commodity, qty, buying=False)
        world.checkpoint()
        out_line(f"{p.correct}{result}{RESET}")
        out_line(f"Credits now: {world.save.pilot.credits}cr. Margin recorded in [T] Trading Ledger.")



def screen_shipyard(p: Palette, world: World) -> None:
    ship = world.save.ship
    while True:
        out_line()
        out_line(_box_title(p, f"Engineering Shipyard: {world.here.station_name}"))
        header = f" {p.gold}KEY  SUBSYSTEM UPGRADE       CURRENT STATUS   NEXT TIER COST  UPGRADE EFFECT{RESET}"
        out_line(f"{p.accent}│{RESET}{header}{' ' * max(0, 77 - _vis_len(header))}{p.accent}│{RESET}")
        out_line(f"{p.accent}├─────────────────────────────────────────────────────────────────────────────┤{RESET}")
        keys = list(UPGRADES.keys())
        for i, key in enumerate(keys):
            u = UPGRADES[key]
            tier = getattr(ship, f"{key}_tier")
            max_t = u["max_tier"]
            bar = _gauge_bar(tier, max_t, 5, p)
            if tier >= max_t:
                status_str = f"MAXED {bar}"
                cost_str = f"{'--':>6}   "
            else:
                cost = u["cost"](tier)
                status_str = f"Tier {tier}->{tier + 1} {bar}"
                cost_str = f"{cost:>6} cr"
            row_str = f"  {p.gold}[{LETTERS[i]}]{RESET}  {u['label']:<20} {_pad(status_str, 18)} {cost_str}  {p.muted}({u['effect']}){RESET}"
            pad_len = max(0, 77 - _vis_len(row_str))
            out_line(f"{p.accent}│{RESET}{row_str}{' ' * pad_len}{p.accent}│{RESET}")

        refit_options = HULL_REFITS[ship.hull_class]
        refit_keys = LETTERS[len(keys) : len(keys) + len(refit_options)]
        out_line(f"{p.accent}├─────────────────────────────────────────────────────────────────────────────┤{RESET}")
        if not refit_options:
            refit_msg = f"  {p.muted}Hull: already flying our best available class ({ship.hull_class}){RESET}"
            pad_len = max(0, 77 - _vis_len(refit_msg))
            out_line(f"{p.accent}│{RESET}{refit_msg}{' ' * pad_len}{p.accent}│{RESET}")
        else:
            for refit_key, (target_class, cost) in zip(refit_keys, refit_options):
                refit_msg = f"  {p.gold}[{refit_key}]{RESET}  {target_class}-Class Refit -- {cost:,} cr"
                pad_len = max(0, 77 - _vis_len(refit_msg))
                out_line(f"{p.accent}│{RESET}{refit_msg}{' ' * pad_len}{p.accent}│{RESET}")

        out_line(_box_bottom(p))
        out_line(f"  {p.accent}Fuel:{RESET} {ship.fuel}/{fuel_capacity(ship)} (6 cr/unit)  │  {p.accent}Hull:{RESET} {ship.hull_hp}/{hull_hp_max(ship)} (4 cr/HP)")
        out_prompt(f"  {p.gold}[U]{RESET}pgrade  {p.gold}[R]{RESET}efuel  {p.gold}[P]{RESET} Repair hull  "
            f"{p.gold}[K] Crew{RESET}  {p.gold}[Q]{RESET} Return: ")
        action = read_command()
        out_line(action)
        if action == "Q":
            return
        if action in refit_keys:
            target_class, cost = refit_options[refit_keys.index(action)]
            _hull_refit_screen(p, world, target_class, cost)
            continue
        if action in LETTERS[:len(keys)]:
            _buy_upgrade(p, world, keys[LETTERS.index(action)])
            continue
        if action == "U":
            out_prompt(f"  {p.muted}Which upgrade [A-{LETTERS[len(keys)-1]}]? {RESET}")
            key_choice = read_command()
            out_line(key_choice)
            if key_choice not in LETTERS[: len(keys)]:
                continue
            _buy_upgrade(p, world, keys[LETTERS.index(key_choice)])
        elif action == "R":
            _refuel(p, world)
        elif action == "P":
            _repair(p, world)
        elif action == "K":
            screen_crew(p, world)


def screen_crew(p: Palette, world: World) -> None:
    ship = world.save.ship
    while True:
        out_line()
        out_line(_box_title(p, "Crew Quarters & Specialist Roster"))
        header = f" {p.gold}KEY  ROLE           STATUS      WAGE RATE      SPECIALTY / BENEFIT{RESET}"
        out_line(f"{p.accent}│{RESET}{header}{' ' * max(0, 77 - _vis_len(header))}{p.accent}│{RESET}")
        out_line(_box_divider(p))
        keys = list(CREW_ROLES.keys())
        for i, role in enumerate(keys):
            info = CREW_ROLES[role]
            letter = LETTERS[i]
            if getattr(ship, f"has_{role}"):
                status = f"{p.correct}HIRED{RESET}    "
                wage = f"{info['wage']} cr/jump"
            else:
                status = f"{p.muted}Available{RESET}"
                wage = f"{info['hire_cost']}cr + {info['wage']}cr/j"
            row_str = f"  {p.gold}[{letter}]{RESET}  {info['label']:<14} {status} {wage:<16} {info['effect']}"
            pad_len = max(0, 77 - _vis_len(row_str))
            out_line(f"{p.accent}│{RESET}{row_str}{' ' * pad_len}{p.accent}│{RESET}")
        out_line(_box_bottom(p))
        out_prompt(f"  {p.muted}Hire/dismiss which [A-{LETTERS[len(keys)-1]}], or [Q] back? {RESET}")
        key = read_command()
        out_line(key)
        if key == "Q":
            return
        idx = LETTERS.index(key) if key in LETTERS else -1
        if idx < 0 or idx >= len(keys):
            continue
        _toggle_crew(p, world, keys[idx])


def _toggle_crew(p: Palette, world: World, role: str) -> None:
    ship = world.save.ship
    info = CREW_ROLES[role]
    if getattr(ship, f"has_{role}"):
        if confirm(f"Dismiss your {info['label']}?", p):
            setattr(ship, f"has_{role}", False)
            world.checkpoint()
            out_line(f"{p.muted}{info['label']} dismissed.{RESET}")
        return
    if world.save.pilot.credits < info["hire_cost"]:
        out_line(f"{p.wrong}Need {info['hire_cost']}cr to hire a {info['label']}.{RESET}")
        return
    if not confirm(f"Hire a {info['label']} for {info['hire_cost']}cr "
                    f"(+{info['wage']}cr/jump ongoing wage)?", p):
        return
    world.save.pilot.credits -= info["hire_cost"]
    setattr(ship, f"has_{role}", True)
    world.checkpoint()
    out_line(f"{p.correct}{info['label']} hired.{RESET}")


def _buy_upgrade(p: Palette, world: World, key: str) -> None:
    ship = world.save.ship
    u = UPGRADES[key]
    tier = getattr(ship, f"{key}_tier")
    if tier >= u["max_tier"]:
        out_line(f"{p.wrong}Already maxed.{RESET}")
        return
    cost = u["cost"](tier)
    if world.save.pilot.credits < cost:
        out_line(f"{p.wrong}Not enough credits ({cost}cr needed).{RESET}")
        return
    if not confirm(f"Buy {u['label']} tier {tier + 1} for {cost}cr?", p):
        return
    world.save.pilot.credits -= cost
    setattr(ship, f"{key}_tier", tier + 1)
    world.checkpoint()
    out_line(f"{p.correct}{u['label']} upgraded to tier {tier + 1}.{RESET}")


def _refuel(p: Palette, world: World) -> None:
    ship = world.save.ship
    room = fuel_capacity(ship) - ship.fuel
    if room <= 0:
        out_line(f"{p.muted}Tanks are already full.{RESET}")
        return
    affordable = world.save.pilot.credits // 6
    max_qty = max(0, min(room, affordable))
    if max_qty <= 0:
        out_line(f"{p.wrong}Not enough credits to buy fuel (6cr/unit).{RESET}")
        return
    out_prompt(f"{p.muted}Fuel to buy (max {max_qty}, 6cr/unit): {RESET}")
    raw = read_line_raw(max_len=4)
    qty = int(raw) if raw.isdigit() else 0
    qty = min(qty, max_qty)
    if qty <= 0:
        return
    cost = qty * 6
    world.save.pilot.credits -= cost
    _ledger(world).fuel_spend += cost
    ship.fuel += qty
    world.checkpoint()
    out_line(f"{p.correct}Refueled {qty} units for {cost}cr.{RESET}")


def _repair(p: Palette, world: World) -> None:
    ship = world.save.ship
    missing = hull_hp_max(ship) - ship.hull_hp
    if missing <= 0:
        out_line(f"{p.muted}Hull is already at full integrity.{RESET}")
        return
    cost = missing * 4
    if world.save.pilot.credits < cost:
        affordable_hp = world.save.pilot.credits // 4
        if affordable_hp <= 0:
            out_line(f"{p.wrong}Can't afford any repairs right now.{RESET}")
            return
        missing = affordable_hp
        cost = missing * 4
    if not confirm(f"Repair {missing} hull for {cost}cr?", p):
        return
    world.save.pilot.credits -= cost
    ship.hull_hp += missing
    world.checkpoint()
    out_line(f"{p.correct}Hull repaired to {ship.hull_hp}/{hull_hp_max(ship)}.{RESET}")


def _hull_refit_screen(p: Palette, world: World, target_class: str, cost: int) -> None:
    """One hull-class refit, generalized over `HULL_REFITS`'s own
    branching ladder -- Shuttle owners see two independent calls of this
    (Freighter or Cutter), Freighter/Cutter owners see one (Carrier),
    Carrier owners see none. Never reversible, matching this refit's own
    "permanent commissioning" tone -- there is no downgrade path."""
    ship = world.save.ship
    if world.save.pilot.credits < cost:
        out_line(f"{p.wrong}Need {cost}cr for the {target_class} refit.{RESET}")
        return
    if not confirm(f"Commission a {target_class}-class refit for {cost}cr? "
                    f"This is a permanent hull upgrade.", p):
        return
    previous_class = ship.hull_class
    world.save.pilot.credits -= cost
    ship.hull_class = target_class
    ship.hull_hp = hull_hp_max(ship)
    world.save.pilot.note(f"Commissioned a {target_class}-class hull refit.")
    world.save.pilot.highlight(f"Commissioned a {target_class}-class hull refit.")
    world.checkpoint()
    out_line(f"{p.gold}{BOLD}Your {previous_class} is towed into drydock and emerges a {target_class}.{RESET}")
    out_line(f"{p.gold}Cargo, hull, and fuel capacity all jump considerably.{RESET}")


def _mission_plain(text) -> str:
    return "".join(c if c.isprintable() else " " for c in _ANSI_RE.sub("", str(text)))


def _load_tracked_mission_id(value) -> int | None:
    if value is not None and (type(value) is not int or value < 1):
        raise ResumeError("The tracked contract cannot be read.")
    return value


def tracked_mission(world: World) -> Mission | None:
    return next((m for m in world.save.active_missions
                 if m.id == world.save.tracked_mission_id and not mission_expired(world, m)), None)


def track_mission(world: World, mission_id: int | None) -> None:
    if world.save.pending_travel is not None:
        raise MissionError("Finish the interrupted journey before changing contracts.")
    if mission_id is not None and not any(m.id == mission_id and not mission_expired(world, m)
                                          for m in world.save.active_missions):
        raise MissionError("That contract is no longer active.")
    world.save.tracked_mission_id = mission_id


def abandon_mission(world: World, mission_id: int) -> str:
    if world.save.pending_travel is not None:
        raise MissionError("Finish the interrupted journey before changing contracts.")
    mission = next((m for m in world.save.active_missions if m.id == mission_id), None)
    if mission is None:
        raise MissionError("That contract is no longer active.")
    world.save.active_missions.remove(mission)
    if world.save.tracked_mission_id == mission.id:
        world.save.tracked_mission_id = None
    message = f"Abandoned: {mission.description}. Cargo retained; no reward or fee."
    world.save.pilot.note(message)
    return message


def preceding_bounties(world: World, mission: Mission) -> int:
    count = 0
    if mission.kind == "bounty":
        for active in world.save.active_missions:
            if active.id == mission.id:
                break
            if active.kind == "bounty" and active.target_system == mission.target_system and not mission_expired(world, active):
                count += 1
    return count


def mission_route(world: World, mission: Mission) -> list[int]:
    path = bfs_path(world.by_id, world.save.current_system, mission.target_system)
    if mission.kind == "bounty":
        target = world.by_id[mission.target_system]
        neighbor = min(target.connections,
                       key=lambda sid: (fuel_cost_for_jump(target, world.by_id[sid], world.save.ship), sid))
        retries = preceding_bounties(world, mission) + (1 if not path else 0)
        path += [neighbor, mission.target_system] * retries
    return path


def mission_bearing(world: World, mission: Mission) -> str:
    target = world.by_id[mission.target_system]
    path = mission_route(world, mission)
    location = f"{target.name} ({target.x},{target.y})"
    if not path:
        return f"{location}: at this station"
    first = world.by_id[path[0]]
    return f"{location}: {len(path)} jump(s), next bearing ({first.x},{first.y})"


def mission_details(world: World, mission: Mission) -> list[str]:
    """Read-only terms and explicit estimates; never reveal remote market state."""
    path = mission_route(world, mission)
    reward = bounty_reward_for(world, mission.reward) if mission.kind in ("bounty", "escort") else mission.reward
    target = world.by_id[mission.target_system]
    lines = [mission.description, f"Destination: {mission_bearing(world, mission)}",
             f"Target danger: {target.danger}" if target.discovered else "Target danger: uncharted"]
    ahead = preceding_bounties(world, mission)
    if ahead:
        lines.append(f"Queued bounties: {ahead} earlier contract(s) at this target resolve first. Budget includes re-entry after each, assuming they remain active and you win.")
    if mission.kind == "bounty" and path and path[-1] == world.save.current_system:
        lines.append("Retry: leave this system and jump back to re-engage the bounty; budget includes both jumps.")
    if mission.deadline_turn is None:
        lines.append("Deadline: none. Jumps advance the day.")
    else:
        remaining = mission.deadline_turn - world.save.turn
        lines.append(f"Deadline: day {mission.deadline_turn} inclusive; today {world.save.turn}, {max(0, remaining)} jump(s) left.")
        if remaining < 0:
            lines.append("EXPIRED: this contract cannot pay.")
        elif len(path) > remaining and mission.kind != "scan":
            lines.append("WARNING: the shortest route misses the deadline.")
    lines.append(f"Gross payout: {reward:,} cr. Credits available: {world.save.pilot.credits:,} cr.")
    procurement = 0
    if mission.kind == "delivery":
        quantity = mission.quantity or 0
        have = world.save.cargo.get(mission.commodity, 0)
        missing = max(0, quantity - have)
        price = price_for(world, world.save.current_system, mission.commodity)
        procurement = missing * price
        free = cargo_capacity(world.save.ship) - sum(world.save.cargo.values())
        lines.extend([
            f"Cargo: deliver {quantity} {COMMODITIES[mission.commodity]['label']}; {have} aboard, buy {missing} more.",
            f"Procurement at this station: {missing} x {price} = {procurement:,} cr; prices can move after purchases.",
            f"Hold: need {missing} free units; {free} available. Delivery consumes the cargo.",
        ])
        if missing > free:
            lines.append("WARNING: make cargo space or upgrade before procuring the full load.")
    elif mission.kind == "scan":
        lines.append("Survey: chart this target by arriving or discovering it with your scanner. No cargo required.")
        lines.append("Accepting or tracking the bearing does not chart the system.")
    elif mission.kind == "bounty":
        lines.append(f"Combat: intercept a tier {mission.pirate_tier} raider at the target. Escape leaves the bounty active; destruction fails it.")
        lines.append("Bounty kills can trigger a mistaken-identity inquiry and notoriety.")
    elif mission.kind == "escort":
        lines.append(f"Combat: one tier {mission.pirate_tier} raider fight on EVERY jump while active, including detours.")
        lines.append("Other escort contracts add their own fights. Escape or destruction fails this convoy.")
        lines.append("Payment follows a won convoy fight on arrival at the target; no cargo space is needed.")
    fuel = 0
    max_leg = 0
    previous = world.save.current_system
    for sid in path:
        leg = fuel_cost_for_jump(world.by_id[previous], world.by_id[sid], world.save.ship)
        max_leg = max(max_leg, leg)
        fuel += leg
        previous = sid
    fuel_cash = max(0, fuel - world.save.ship.fuel) * 6
    wage = sum(info["wage"] for role, info in CREW_ROLES.items() if getattr(world.save.ship, f"has_{role}"))
    wages = wage * len(path)
    outlay = procurement + fuel_cash + wages
    lines.extend([
        f"Shortest-route budget: {fuel} fuel ({world.save.ship.fuel} aboard), {fuel_cash} cr top-ups; wages {wage} cr/jump, {wages} cr total.",
        f"Estimated remaining cash outlay: {outlay:,} cr; payout less this outlay: {reward - outlay:+,} cr.",
        "Estimate excludes cargo already paid for, repairs, detours, combat gains/losses and changing prices; it is not total profit.",
        "Budget assumes refuelling stops and retained crew. Survey scanning may avoid travel. Remote danger remains unknown until charted.",
    ])
    if max_leg > fuel_capacity(world.save.ship):
        lines.append("WARNING: a shortest-route jump exceeds tank capacity; upgrade or find another route.")
    if outlay > world.save.pilot.credits:
        lines.append("WARNING: current credits do not cover the estimated remaining outlay.")
    return [_mission_plain(line) for line in lines]


def _mission_text_pages(lines: list[str], *, overhead: int = 7) -> list[list[str]]:
    rows = [row for line in lines for row in _wrap_output(_mission_plain(line), max(1, _OUTPUT_WIDTH - 1)).split("\r\n")]
    size = max(1, _OUTPUT_HEIGHT - overhead)
    return [rows[i:i + size] for i in range(0, len(rows), size)] or [[]]


def _show_tracked_mission(p: Palette, world: World) -> None:
    mission = tracked_mission(world)
    if mission is not None:
        kind = "SURVEY" if mission.kind == "scan" else mission.kind.upper()
        out_line(f"{p.gold}Tracked {_mission_plain(kind)}: {_mission_plain(mission_bearing(world, mission))}{RESET}")


def pilot_recap(world: World) -> list[str]:
    save = world.save
    ready = sum(1 for order in save.active_futures if save.turn >= order.settle_turn)
    lines = [f"Docked: {world.here.name}. Day {save.turn}; {save.pilot.credits:,} cr.",
             f"Commitments: {len(save.active_missions)} contract(s), {len(save.active_futures)} futures order(s), {ready} mature."]
    mission = tracked_mission(world)
    if mission is not None:
        deadline = "no deadline" if mission.deadline_turn is None else f"due day {mission.deadline_turn}"
        lines.append(f"Plan: {mission.description}; {deadline}.")
    else:
        lines.append("No contract tracked. Use the Mission Board to inspect and track your jobs.")
    return [_mission_plain(line) for line in lines]


def pilot_guide_lines(world: World) -> list[str]:
    lines = ["Use [B]ack to return to the station deck before using its market, yard or chart commands.",
             "Your ship is your livelihood. Supply outlying stations, build capital, and choose what kind of pilot to become."]
    lines += pilot_recap(world)
    active = next((m for m in world.save.active_missions if m.opening_assignment), None)
    if active is not None:
        have = world.save.cargo.get(active.commodity, 0)
        lines += [f"First Flight: {have}/{active.quantity} {COMMODITIES[active.commodity]['label']} aboard.",
                  f"Deliver at {world.by_id[active.target_system].name}. Docking with the full load completes it automatically; delivery consumes those goods."]
    elif world.save.flags.get("opening_assignment_completed"):
        lines.append("First Flight complete. Your next goal: a first ship upgrade, then choose a regular trade or contract route.")
    elif world.save.flags.get("opening_assignment_taken"):
        lines.append("First Flight is closed. An abandoned introductory job cannot be taken again this career.")
    elif opening_assignment_offer(world) is not None:
        lines.append("Optional First Flight: Freeport merchants will sponsor one local delivery. [O]ffer shows all terms before acceptance.")
    else:
        lines.append("First Flight needs an affordable cargo and return-fuel budget while still at Freeport on day zero. The guide remains available anywhere.")
    lines += [
        "1. Buy cargo: [M]arket, choose the commodity's letter, [B]uy, enter a quantity. Buying spends credits and needs free hold space. Enter with no quantity cancels.",
        "2. Keep fuel: [Y]ard, [R]efuel. Each fuel unit costs 6 cr. Reserve enough for the outward and return jumps; keep credits for repairs and crew wages too.",
        "3. Depart: [C]hart, select the destination's letter. A jump advances one day, uses fuel and charges wages. Browsing, trading and upgrades do not advance the day.",
        "4. Deliver a contract by docking with its full cargo. Ordinary trading instead uses [M]arket, commodity letter, [S]ell. Sales pay less than the local buy quote; distant prices can change.",
        "5. First upgrade: [Y]ard, [A] Cargo Bay Expansion adds 8 cargo spaces. [F] Hull Reinforcement adds 35 maximum hull. Keep travel money before investing.",
        f"Your next cargo tier costs {UPGRADES['cargo']['cost'](world.save.ship.cargo_tier):,} cr." if world.save.ship.cargo_tier < UPGRADES['cargo']['max_tier'] else "Your cargo upgrades are complete.",
        "Danger is a risk rating, not a guarantee you can win a fight. Evasion can fail; bribes cost credits and can be refused. Read the encounter choices before acting.",
        "[B]oard shows full contract terms, tracking and abandonment. [G]uide keeps this recap available. [Q] on the station deck saves and leaves the game.",
    ]
    return lines


def _screen_opening_offer(p: Palette, world: World, offer: Mission) -> None:
    fuel = fuel_cost_for_jump(world.here, world.by_id[offer.target_system], world.save.ship)
    wage = sum(info["wage"] for role, info in CREW_ROLES.items() if getattr(world.save.ship, f"has_{role}"))
    lines = ["Freeport merchants need a reliable new pilot. First Flight sponsors one delivery; acceptance also tracks it."]
    lines += mission_details(world, offer)
    lines += [f"Reserve {2 * fuel} fuel for delivery and return; {world.save.ship.fuel} aboard. Fuel replacement costs {12 * fuel} cr for both jumps.",
              f"Current crew: {wage} cr per jump, {2 * wage} cr for delivery and return.",
              "Payment covers the quoted three units, round-trip fuel and current crew wages plus 200 cr. Detours, repairs, encounters and later prices can change your result.",
              "No deadline. This uses one active-contract slot. Abandonment closes First Flight for this career; the guide stays available.",
              "After acceptance, use [M]arket to buy the goods, [Y]ard to refuel if needed, then [C]hart to jump to the named station."]
    pages = _mission_text_pages(lines, overhead=5)
    page = 0
    while True:
        out_line()
        out_line(f"First Flight {page + 1}/{len(pages)}")
        for line in pages[page]:
            out_line(line)
        out_prompt(("[A]ccept " if page == len(pages) - 1 else "") + "[N]ext [P]rev [B]ack: ")
        key = read_command()
        if key in ("B", "Q"):
            return
        if key == "N" and page < len(pages) - 1:
            page += 1
        elif key == "P" and page:
            page -= 1
        elif key == "A" and page == len(pages) - 1:
            try:
                accept_opening_assignment(world, offer)
            except MissionError as exc:
                out_line(str(exc))
            else:
                world.checkpoint()
                out_line("First Flight accepted and tracked. Return with [B]ack, then use [M]arket to buy your cargo.")
            pause(p)
            return


def screen_pilot_guide(p: Palette, world: World) -> None:
    page = 0
    while True:
        pages = _mission_text_pages(pilot_guide_lines(world), overhead=5)
        page = min(page, len(pages) - 1)
        offer = opening_assignment_offer(world)
        out_line()
        out_line(f"Pilot Guide {page + 1}/{len(pages)}")
        for line in pages[page]:
            out_line(line)
        out_prompt(("[O]ffer " if offer is not None else "") + "[N]ext [P]rev [B]ack: ")
        key = read_command()
        if key in ("B", "Q"):
            return
        if key == "N" and page < len(pages) - 1:
            page += 1
        elif key == "P" and page:
            page -= 1
        elif key == "O" and offer is not None:
            _screen_opening_offer(p, world, offer)


def screen_mission_details(p: Palette, world: World, mission: Mission, *, active: bool) -> None:
    page = 0
    while True:
        lines = mission_details(world, mission)
        max_pages = sum(len(_wrap_output(line, max(1, _OUTPUT_WIDTH - 1)).split("\r\n")) for line in lines)
        title = f"Contract #{mission.id} {max_pages}/{max_pages}"
        footer = "[N]ext [P]rev [B]ack > "
        overhead = max(7, 1 + len(_wrap_output(title, _OUTPUT_WIDTH).split("\r\n"))
                       + len(_wrap_output(footer, max(1, _OUTPUT_WIDTH - 1)).split("\r\n"))
                       + (2 if active else 1))
        pages = _mission_text_pages(lines, overhead=overhead)
        page = min(page, len(pages) - 1)
        out_line()
        out_line(f"{p.gold}Contract #{mission.id} {page + 1}/{len(pages)}{RESET}")
        for row in pages[page]:
            out_line(row)
        actions = "[N]ext [P]rev [B]ack"
        if active:
            toggle = "Untrack" if world.save.tracked_mission_id == mission.id else "Track"
            out_line(f"[T] {toggle}")
            out_line("[D] Abandon")
        elif page == len(pages) - 1:
            out_line("[A]ccept contract")
        out_prompt(actions + " > ")
        key = read_command()
        if key in ("B", "Q"):
            return
        if key == "N":
            page = min(page + 1, len(pages) - 1)
        elif key == "P":
            page = max(0, page - 1)
        else:
            try:
                if not active and key == "A" and page == len(pages) - 1:
                    accept_mission(world, mission)
                    world.checkpoint()
                    out_line(f"{p.correct}Accepted: {_mission_plain(mission.description)}{RESET}")
                    pause(p)
                    return
                if active and key == "T":
                    track_mission(world, None if world.save.tracked_mission_id == mission.id else mission.id)
                    world.checkpoint()
                    out_line("Tracking updated.")
                    pause(p)
                elif active and key == "D":
                    if confirm("Abandon this contract? Forfeit its reward; keep cargo, no fee.", p):
                        message = abandon_mission(world, mission.id)
                        world.checkpoint()
                        out_line(_mission_plain(message))
                        pause(p)
                        return
            except MissionError as exc:
                out_line(f"{p.wrong}{exc}{RESET}")
                pause(p)


def screen_missions(p: Palette, world: World) -> None:
    page = 0
    while True:
        entries = [(m, False) for m in posted_mission_offers(world)] + [(m, True) for m in world.save.active_missions]
        wrapped = []
        for mission, active in entries:
            kind = "SURVEY" if mission.kind == "scan" else mission.kind.upper()
            state = "TRACKED" if active and world.save.tracked_mission_id == mission.id else "ACTIVE" if active else "OFFER"
            label = _mission_plain(f"{state} {kind}: {world.by_id[mission.target_system].name} (+{mission.reward:,}cr)")
            wrapped.append((mission, active, _wrap_output(label, max(1, _OUTPUT_WIDTH - 5)).split("\r\n")))
        summary = [f"Active: {len(world.save.active_missions)}/{MAX_ACTIVE_MISSIONS}"]
        posted = world.save.mission_boards.get(world.save.current_system)
        if posted:
            summary.append(f"Refresh day {posted['refresh_turn']}")
        footer = "[1-9] Details [N]ext [P]rev [B]ack > "
        max_pages = max(1, sum(len(rows) for _, _, rows in wrapped))
        overhead = 1 + len(_wrap_output(f"Contracts {max_pages}/{max_pages}", _OUTPUT_WIDTH).split("\r\n"))
        overhead += sum(len(_wrap_output(line, _OUTPUT_WIDTH).split("\r\n")) for line in summary)
        overhead += len(_wrap_output(footer, max(1, _OUTPUT_WIDTH - 1)).split("\r\n"))
        capacity = max(1, _OUTPUT_HEIGHT - overhead)
        pages = [([], [])]  # rows, selectable contracts; continued rows stay selectable
        for mission, active, rows in wrapped:
            for row in rows:
                body, choices = pages[-1]
                choice = next((i for i, (m, _) in enumerate(choices) if m is mission), None)
                if len(body) >= capacity or (choice is None and len(choices) >= 9):
                    pages.append(([], []))
                    body, choices = pages[-1]
                    choice = None
                if choice is None:
                    choice = len(choices)
                    choices.append((mission, active))
                body.append(f"[{choice + 1}] {row}")
        page = min(page, len(pages) - 1)
        out_line()
        out_line(f"{p.gold}Contracts {page + 1}/{len(pages)}{RESET}")
        for row in pages[page][0]:
            out_line(row)
        if not entries:
            out_line("No contracts currently available.")
        for line in summary:
            out_line(line)
        out_prompt(footer)
        key = read_command()
        if key in ("B", "Q"):
            return
        if key == "N":
            page = min(page + 1, len(pages) - 1)
        elif key == "P":
            page = max(0, page - 1)
        elif len(key) == 1 and "1" <= key <= "9" and int(key) <= len(pages[page][1]):
            mission, active = pages[page][1][int(key) - 1]
            screen_mission_details(p, world, mission, active=active)


def screen_status(p: Palette, world: World) -> None:
    pilot, ship = world.save.pilot, world.save.ship
    out_line()
    rank_name = rank_for(pilot.credits)
    out_line(_box_title(p, f"Pilot Record: {pilot.handle} ── [ Rank: {rank_name} ]"))
    creds_line = f"  {p.gold}Credits:{RESET} {pilot.credits:,} cr"
    pad_len = max(0, 77 - _vis_len(creds_line))
    out_line(f"{p.accent}│{RESET}{creds_line}{' ' * pad_len}{p.accent}│{RESET}")
    for faction in FACTIONS:
        rep = pilot.reputation.get(faction, 0)
        rep_label = "Allied" if rep >= 10 else ("Friendly" if rep >= 4 else ("Hostile" if rep <= -5 else "Neutral"))
        rep_col = p.correct if rep > 0 else (p.wrong if rep < 0 else p.muted)
        f_line = f"  {p.gold}{FACTION_LABEL[faction]} standing:{RESET} {rep_col}{rep:+d} ({rep_label}){RESET}"
        pad_len = max(0, 77 - _vis_len(f_line))
        out_line(f"{p.accent}│{RESET}{f_line}{' ' * pad_len}{p.accent}│{RESET}")
    out_line(_box_divider(p))
    ship_line = (
        f"  {p.gold}Ship:{RESET} {ship.hull_class:<12} │ "
        f"{p.accent}Hull:{RESET} {_gauge_bar(ship.hull_hp, hull_hp_max(ship), 6, p)} {ship.hull_hp}/{hull_hp_max(ship)} │ "
        f"{p.accent}Fuel:{RESET} {_gauge_bar(ship.fuel, fuel_capacity(ship), 6, p)} {ship.fuel}/{fuel_capacity(ship)} │ "
        f"{p.accent}Cargo:{RESET} {cargo_capacity(ship)}"
    )
    pad_len = max(0, 77 - _vis_len(ship_line))
    out_line(f"{p.accent}│{RESET}{ship_line}{' ' * pad_len}{p.accent}│{RESET}")

    crew = [info["label"] for role, info in CREW_ROLES.items() if getattr(ship, f"has_{role}")]
    if crew:
        c_line = f"  {p.gold}Crew:{RESET} {', '.join(crew)}"
        pad_len = max(0, 77 - _vis_len(c_line))
        out_line(f"{p.accent}│{RESET}{c_line}{' ' * pad_len}{p.accent}│{RESET}")

    disc_count = sum(1 for s in world.galaxy if s.discovered)
    pct = round((disc_count / len(world.galaxy)) * 100)
    chart_line = f"  {p.gold}Systems charted:{RESET} {disc_count}/{len(world.galaxy)} ({pct}%)   │  {p.gold}Raiders defeated:{RESET} {pilot.kills}"
    pad_len = max(0, 77 - _vis_len(chart_line))
    out_line(f"{p.accent}│{RESET}{chart_line}{' ' * pad_len}{p.accent}│{RESET}")

    m_line = f"  {p.gold}Missions completed:{RESET} {pilot.missions_completed}"
    if pilot.retirements:
        m_line += f"   │  {p.gold}Retirements:{RESET} {pilot.retirements}"
    pad_len = max(0, 77 - _vis_len(m_line))
    out_line(f"{p.accent}│{RESET}{m_line}{' ' * pad_len}{p.accent}│{RESET}")

    if pilot.has_concord_commission:
        st_line = f"  {p.gold}Standing:{RESET} Concord Privateer"
        pad_len = max(0, 77 - _vis_len(st_line))
        out_line(f"{p.accent}│{RESET}{st_line}{' ' * pad_len}{p.accent}│{RESET}")
    if pilot.has_blackwake_made:
        bw_line = f"  {p.gold}Standing:{RESET} Made (Blackwake Cartel)"
        pad_len = max(0, 77 - _vis_len(bw_line))
        out_line(f"{p.accent}│{RESET}{bw_line}{' ' * pad_len}{p.accent}│{RESET}")

    event = world.save.active_event
    if event:
        ev_line = f"  {p.gold}Economy event:{RESET} {event['description']} ({event['turns_remaining']} turn(s) left)"
        pad_len = max(0, 77 - _vis_len(ev_line))
        out_line(f"{p.accent}│{RESET}{ev_line}{' ' * pad_len}{p.accent}│{RESET}")

    if world.save.active_missions:
        out_line(_box_divider(p))
        head_str = f"  {p.gold}Active Contracts & Missions:{RESET}"
        pad_len = max(0, 77 - _vis_len(head_str))
        out_line(f"{p.accent}│{RESET}{head_str}{' ' * pad_len}{p.accent}│{RESET}")
        for m in world.save.active_missions:
            if m.deadline_turn is not None:
                rem = max(0, m.deadline_turn - world.save.turn)
                time_str = f"{rem} turn(s) left"
            else:
                time_str = "no deadline"
            desc = m.description
            if _vis_len(desc) > 40:
                desc = desc[:37] + "..."
            m_str = f"    • {desc:<40} {time_str:>14}  +{m.reward}cr"
            pad_len = max(0, 77 - _vis_len(m_str))
            out_line(f"{p.accent}│{RESET}{m_str}{' ' * pad_len}{p.accent}│{RESET}")

    out_line(_box_bottom(p))

    if pilot.highlights:
        out_line(f"{p.gold}Career highlights:{RESET}")
        for entry in pilot.highlights[-15:]:
            out_line(f"  {p.gold}* {entry}{RESET}")
    if pilot.log:
        out_line(f"{p.muted}Recent log:{RESET}")
        for entry in pilot.log[-8:]:
            out_line(f"  {p.muted}- {entry}{RESET}")

    if rank_for(pilot.credits) == RANKS[-1][1]:
        out_prompt(f"{p.muted}[R]etire and start a new career, or any other key to continue... {RESET}")
        key = read_command()
        out_line(key)
        if key == "R":
            if confirm("This ends your current career for good and begins a new one. Retire?", p):
                world.reset(retire_pilot(world.save))
                world.checkpoint()
                out_line()
                out_line(f"{p.accent}{BOLD}A new career begins.{RESET}")
        return
    pause(p)


def screen_hall_of_fame(p: Palette, world: World, save_dir: Path, user_id: int) -> None:
    entries = load_hall_of_fame(save_dir)
    out_line()
    out_line(_box_title(p, "Interstellar Pilot Hall of Fame"))
    if not entries:
        empty_line = f"  {p.muted}No pilots recorded yet -- be the first.{RESET}"
        out_line(f"{p.accent}│{RESET}{empty_line}{' ' * max(0, 77 - _vis_len(empty_line))}{p.accent}│{RESET}")
        out_line(_box_bottom(p))
    else:
        header = f" {p.gold}RK  PILOT CALLSIGN   RANK TITLE                 CREDITS  KILLS  MISSIONS RET{RESET}"
        out_line(f"{p.accent}│{RESET}{header}{' ' * max(0, 77 - _vis_len(header))}{p.accent}│{RESET}")
        out_line(_box_divider(p))
        for i, e in enumerate(entries, start=1):
            marker = f"{p.gold}*{RESET}" if e.get("user_id") == user_id else " "
            col = p.gold if e.get("user_id") == user_id else (p.accent if i <= 3 else p.muted)
            row_str = (
                f" {marker}{col}{i:>2}.{RESET} {e.get('handle', '?'):<16} "
                f"{e.get('rank', '?'):<22} {e.get('best_credits', 0):>8,}cr "
                f"{e.get('kills', 0):>6} {e.get('missions_completed', 0):>8} {e.get('retirements', 0):>4}"
            )
            pad_len = max(0, 77 - _vis_len(row_str))
            out_line(f"{p.accent}│{RESET}{row_str}{' ' * pad_len}{p.accent}│{RESET}")
        out_line(_box_bottom(p))
    pause(p)


# [S]can, [G]o to, [V]iew are fixed control keys on this same prompt,
# not per-connection row letters -- never assigned to a connection.
# Dogfood-caught: `_connect_systems`'s own extra-edge pass can give a
# single system up to ~7 connections (seen across a few thousand random
# seeds), and "G" is only the *7th* letter -- a plain `LETTERS[index]`
# assignment would silently make that 7th connection's own row letter
# collide with (and be permanently shadowed by) the "[G]o to" hotkey,
# unlike "S"/"V" which sit late enough in the alphabet to never
# realistically collide with any observed degree.
CHART_RESERVED_LETTERS = "SGV"
CHART_CONNECTION_LETTERS = [c for c in LETTERS if c not in CHART_RESERVED_LETTERS]


def screen_chart(p: Palette, world: World) -> str | None:
    """Returns a destination system id to travel to, or None if the
    player backed out."""
    while True:
        here = world.here
        out_line()
        _show_tracked_mission(p, world)
        out_line(_box_title(p, f"Navigation Star Chart: {here.name}"))
        header = f" {p.gold}KEY  BEARING DESTINATION       SECTOR      ECONOMY       DANGER   JUMP COST{RESET}"
        out_line(f"{p.accent}│{RESET}{header}{' ' * max(0, 77 - _vis_len(header))}{p.accent}│{RESET}")
        out_line(f"{p.accent}├─────────────────────────────────────────────────────────────────────────────┤{RESET}")
        options: list[int] = []
        for sid in sorted(here.connections):
            dest = world.by_id[sid]
            cost = fuel_cost_for_jump(here, dest, world.save.ship)
            letter = CHART_CONNECTION_LETTERS[len(options)]
            options.append(sid)

            in_range = world.save.ship.fuel >= cost
            fuel_label = f"{cost} fuel" if in_range else f"{p.wrong}{cost} fuel (LOW){RESET}"

            if dest.discovered:
                danger_str = (
                    f"{p.correct}Safe (0){RESET}" if dest.danger == 0
                    else (f"{p.gold}Danger {dest.danger}{RESET}" if dest.danger == 1 else f"{p.wrong}Danger {dest.danger}{RESET}")
                )
                row_str = f"  {p.gold}[{letter}]{RESET}  {dest.name:<17} {sector_for(dest):<15} {dest.economy:<13} {_pad(danger_str, 9)} {_pad(fuel_label, 12)}"
            else:
                row_str = (
                    f"  {p.gold}[{letter}]{RESET}  {p.muted}??? (Uncharted Bearing)  "
                    f"Unknown   Uncharted   Unknown   {fuel_label}{RESET}"
                )
            pad_len = max(0, 77 - _vis_len(row_str))
            out_line(f"{p.accent}│{RESET}{row_str}{' ' * pad_len}{p.accent}│{RESET}")

        out_line(_box_bottom(p))
        mission = tracked_mission(world)
        route = mission_route(world, mission) if mission is not None else []
        if route and route[0] in options:
            letter = CHART_CONNECTION_LETTERS[options.index(route[0])]
            next_system = world.by_id[route[0]]
            out_line(f"{p.gold}Tracked route: [{letter}] next bearing ({next_system.x},{next_system.y}){RESET}")
        scan_available = world.save.ship.scanner_tier > 0
        actions = []
        if scan_available:
            actions.append(f"{p.gold}[S]{RESET}can distant contacts")
        actions.append(f"{p.gold}[G]{RESET}o to system by name")
        actions.append(f"{p.gold}[V]{RESET}iew full chart by sector")
        out_line(f"  {'   '.join(actions)}")
        out_prompt(f"  {p.muted}Jump to which, or [Q] back? {RESET}")
        key = read_command()
        out_line(key)
        if key == "Q":
            return None
        if key == "S" and scan_available:
            _do_scan(p, world)
            continue
        if key == "G":
            _screen_auto_route(p, world)
            continue
        if key == "V":
            screen_galaxy_map(p, world)
            continue
        idx = CHART_CONNECTION_LETTERS.index(key) if key in CHART_CONNECTION_LETTERS else -1
        if idx < 0 or idx >= len(options):
            continue
        dest_id = options[idx]
        cost = fuel_cost_for_jump(here, world.by_id[dest_id], world.save.ship)
        if world.save.ship.fuel < cost:
            out_line(f"{p.wrong}Not enough fuel ({cost} needed, have {world.save.ship.fuel}).{RESET}")
            continue
        return dest_id


def _do_scan(p: Palette, world: World) -> None:
    range_hops = 2 + world.save.ship.scanner_tier + (1 if world.save.ship.has_navigator else 0)
    hops = bfs_hops(world.by_id, world.save.current_system)
    candidates = [sid for sid, h in hops.items() if h <= range_hops and not world.by_id[sid].discovered]
    if not candidates:
        out_line(f"{p.muted}Long-range sensors find nothing new nearby.{RESET}")
        return
    target = world.event_rng.choice(candidates)
    world.by_id[target].discovered = True
    world.sync_discovered()
    completed = check_mission_completions(world, just_discovered=target)
    world.checkpoint()
    out_line(f"{p.correct}Sensor contact! {world.by_id[target].name} is now on your chart.{RESET}")
    for msg in completed:
        out_line(f"{p.gold}{msg}{RESET}")


def screen_galaxy_map(p: Palette, world: World) -> None:
    """Every discovered system, grouped by named sector -- the "readable
    chart at scale" the star chart's own short direct-neighbor list
    can't provide once a career has charted more than a handful of
    systems. Read-only: jumping still happens via the star chart's own
    neighbor list or the [G]o to auto-route, not here."""
    hops = bfs_hops(world.by_id, world.save.current_system)
    out_line()
    out_line(_box_title(p, "Charted Systems"))
    by_sector: dict[str, list[GalaxySystem]] = {}
    for system in world.galaxy:
        if system.discovered:
            by_sector.setdefault(sector_for(system), []).append(system)
    if not by_sector:
        empty_str = f"  {p.muted}Nothing charted yet.{RESET}"
        pad_len = max(0, 77 - _vis_len(empty_str))
        out_line(f"{p.accent}│{RESET}{empty_str}{' ' * pad_len}{p.accent}│{RESET}")
    for sector in sorted(by_sector):
        sec_str = f"  {p.gold}{BOLD}▼ Sector: {sector}{RESET}"
        pad_len = max(0, 77 - _vis_len(sec_str))
        out_line(f"{p.accent}│{RESET}{sec_str}{' ' * pad_len}{p.accent}│{RESET}")
        for system in sorted(by_sector[sector], key=lambda s: s.name):
            marker = f"{p.accent}*{RESET}" if system.id == world.save.current_system else " "
            hop = hops.get(system.id)
            hop_label = "here" if hop == 0 else (f"{hop} jump(s)" if hop is not None else "unreachable")
            row_str = f"  {marker} {system.name:<18} {system.economy:<12} danger {system.danger}   {p.muted}{hop_label}{RESET}"
            pad_len = max(0, 77 - _vis_len(row_str))
            out_line(f"{p.accent}│{RESET}{row_str}{' ' * pad_len}{p.accent}│{RESET}")
    out_line(_box_bottom(p))
    pause(p)


def _screen_auto_route(p: Palette, world: World) -> None:
    """"[G]o to" from the star chart: type a (partial) system name
    instead of jumping one connection at a time. Only matches already-
    discovered systems -- an uncharted system's name isn't known to the
    player to type in the first place -- but the shortest path found
    can still pass *through* undiscovered systems along the way, same
    as a manual one-hop jump onto an uncharted neighbor already can.

    Executes every hop via the ordinary `screen_travel`, completely
    unmodified, so combat, customs, and travel encounters all still
    trigger exactly as they would one hop at a time. Stops early if a
    hop can't be afforded or diverts the plan (e.g. the ship is
    destroyed and towed back to Freeport mid-route) -- never force-
    marches the player through a route they can no longer see the cost
    of one hop ahead."""
    out_prompt(f"  {p.muted}Go to (system name, Enter to cancel): {RESET}")
    typed = read_line_raw(max_len=20, allowed=lambda c: c.isalnum() or c in " '").strip()
    if not typed:
        return
    here_id = world.save.current_system
    candidates = [s for s in world.galaxy
                  if s.discovered and s.id != here_id and typed.lower() in s.name.lower()]
    if not candidates:
        out_line(f"{p.wrong}No charted system matches '{typed}'.{RESET}")
        return
    if len(candidates) == 1:
        target = candidates[0]
    else:
        # Dogfood-caught: a short, common substring (a single vowel,
        # say) can match 30+ of the 48 galaxy systems at once -- with
        # plain `LETTERS`, that many matches reaches "Q" as a real row
        # letter (the 17th), making it ambiguous with this same
        # prompt's own "[Q] cancel". Reuses the same reserved-letters
        # pattern as the chart screen's own CHART_RESERVED_LETTERS fix.
        pick_letters = [c for c in LETTERS if c != "Q"]
        out_line(f"{p.muted}Multiple matches:{RESET}")
        shown = candidates[:len(pick_letters)]
        for i, s in enumerate(shown):
            out_line(f"  {p.gold}[{pick_letters[i]}]{RESET} {s.name}")
        out_prompt(f"  {p.muted}Which one, or [Q] cancel? {RESET}")
        key = read_command()
        out_line(key)
        if key == "Q":
            return
        idx = pick_letters.index(key) if key in pick_letters else -1
        if idx < 0 or idx >= len(shown):
            return
        target = shown[idx]

    path = bfs_path(world.by_id, here_id, target.id)
    total_fuel = 0
    cur = world.by_id[here_id]
    for hop_id in path:
        total_fuel += fuel_cost_for_jump(cur, world.by_id[hop_id], world.save.ship)
        cur = world.by_id[hop_id]
    out_line(f"{p.muted}Route to {target.name}: {len(path)} jump(s), {total_fuel} fuel total.{RESET}")
    if world.save.ship.fuel < total_fuel:
        out_line(f"{p.wrong}Not enough fuel for the full route "
                  f"({total_fuel} needed, have {world.save.ship.fuel}).{RESET}")
        return
    if not confirm("Engage auto-route?", p):
        return

    for hop_id in path:
        cost = fuel_cost_for_jump(world.here, world.by_id[hop_id], world.save.ship)
        if world.save.ship.fuel < cost:
            out_line(f"{p.wrong}Route interrupted -- not enough fuel to continue "
                      f"({cost} needed, have {world.save.ship.fuel}).{RESET}")
            return
        screen_travel(p, world, hop_id)
        # A restart finishes the saved hop, then returns control to the pilot;
        # the remaining auto-route is intentionally not resumed unattended.
        world.checkpoint()
        if world.save.current_system != hop_id:
            out_line(f"{p.wrong}Route interrupted.{RESET}")
            return


def _travel_encounter(world: World) -> dict:
    """One bounded encounter slot; standalone domain/UI calls need no journey."""
    travel = world.save.pending_travel
    return travel["encounter"] if travel is not None else {}


def _encounter_result(p: Palette, world: World, state: dict, lines: list[str]) -> None:
    """Commit both effects and completion before revealing their result."""
    state.update(done=True, result=lines)
    world.checkpoint()
    for line in lines:
        out_line(f"{p.gold}{line}{RESET}")


def _resolve_random_travel_encounter(p: Palette, world: World, dest: GalaxySystem) -> None:
    state = _travel_encounter(world)
    if state.get("done"):
        for line in state.get("result", []):
            out_line(f"{p.gold}{line}{RESET}")
        return
    if "kind" not in state:
        kind = "none"
        if world.event_rng.random() < 0.08 + dest.danger * 0.05:
            kind = world.event_rng.choices(
                list(TRAVEL_ENCOUNTER_WEIGHTS), weights=list(TRAVEL_ENCOUNTER_WEIGHTS.values())
            )[0]
        state["kind"] = kind
        if kind == "pirate":
            state["pirates"] = [dataclasses.asdict(ship) for ship in generate_pirate_squadron(world, dest)]
            state["index"] = 0
        world.checkpoint()
    kind = state["kind"]
    if kind == "pirate":
        pirates = state["pirates"]
        if len(pirates) > 1:
            out_line(f"{p.wrong}Raider squadron contact: {len(pirates)} ships incoming!{RESET}")
        while state["index"] < len(pirates):
            pirate = Pirate(**pirates[state["index"]])
            out_line(f"{p.wrong}Raider contact: the {pirate.name}!{RESET}")
            outcome = screen_combat(p, world, pirate)
            state["index"] += 1
            state.pop("combat", None)
            if outcome != "won":
                state["index"] = len(pirates)
            world.checkpoint()
        _encounter_result(p, world, state, [])
    elif kind == "derelict":
        _encounter_derelict(p, world)
    elif kind == "distress":
        _encounter_distress_call(p, world)
    elif kind == "tip":
        _encounter_market_tip(p, world, dest)
    else:
        _encounter_result(p, world, state, [])


def _encounter_derelict(p: Palette, world: World) -> None:
    state = _travel_encounter(world)
    if state.get("done"):
        return
    if "ambush" not in state:
        out_line(f"{p.muted}Sensors pick up a derelict hulk drifting nearby.{RESET}")
        while True:
            out_prompt(f"{p.muted}[B]oard for salvage or [I]gnore and continue? {RESET}")
            action = read_command()
            out_line(action)
            if action in ("B", "I"):
                break
        if action == "I":
            _encounter_result(p, world, state, ["You leave the derelict behind."])
            return
        if world.event_rng.random() < 0.70:
            reward = world.event_rng.randint(60, 100 + max(0, world.here.danger) * 120)
            world.save.pilot.credits += reward
            world.save.pilot.note(f"Salvaged a derelict hulk (+{reward}cr).")
            _encounter_result(p, world, state, [f"Salvage recovered: {reward}cr."])
            return
        state["ambush"] = dataclasses.asdict(generate_pirate(world))
        world.checkpoint()
    out_line(f"{p.wrong}The wreck's defenses weren't as dead as they looked!{RESET}")
    screen_combat(p, world, Pirate(**state["ambush"]))
    _encounter_result(p, world, state, [])


def _encounter_distress_call(p: Palette, world: World) -> None:
    state = _travel_encounter(world)
    if state.get("done"):
        return
    out_line(f"{p.muted}A garbled distress signal reaches your comms.{RESET}")
    while True:
        out_prompt(f"{p.muted}[H]elp (costs fuel) or [I]gnore and continue? {RESET}")
        action = read_command()
        out_line(action)
        if action in ("H", "I"):
            break
    if action == "I":
        _encounter_result(p, world, state, ["You continue past the distress signal."])
        return
    fuel_cost = min(world.save.ship.fuel, world.event_rng.randint(2, 4))
    world.save.ship.fuel -= fuel_cost
    reward = world.event_rng.randint(60, 180)
    world.save.pilot.credits += reward
    adjust_reputation(world, FACTION_CONCORD, 3)
    world.save.pilot.note(f"Answered a distress call (+{reward}cr, Concord standing up).")
    _encounter_result(p, world, state, [
        f"You divert to help -- {fuel_cost} fuel spent. Grateful survivors "
        f"pay {reward}cr, and Concord takes note.",
    ])


def _encounter_market_tip(p: Palette, world: World, dest: GalaxySystem) -> None:
    """Remember the revealed quote with the encounter result; RNG order is unchanged."""
    state = _travel_encounter(world)
    if state.get("done"):
        for line in state.get("result", []):
            out_line(f"{p.gold}{line}{RESET}")
        return
    hops = bfs_hops(world.by_id, dest.id)
    candidates = [sid for sid, h in hops.items() if 1 <= h <= 4 and world.by_id[sid].discovered]
    if not candidates:
        _encounter_result(p, world, state, ["You intercept a garbled data burst -- nothing usable in it."])
        return
    sid = world.event_rng.choice(candidates)
    system = world.by_id[sid]
    commodity = world.event_rng.choice(list(COMMODITIES))
    price = price_for(world, sid, commodity)
    quote = _remember_market_quote(world, sid, commodity, price)
    label = COMMODITIES[commodity]["label"]
    buy = f"{quote['buy']}cr" if quote["buy"] is not None else "prohibited"
    _encounter_result(p, world, state, [
        f"You intercept a trader's data burst: {label} at {system.name}, buy {buy}, sell {quote['sell']}cr. Recorded on day {world.save.turn}.",
    ])


def _resolve_escort_missions(p: Palette, world: World, dest_id: int) -> None:
    """Resolve each contract once per hop, including after a saved combat win.

    Full mission snapshots avoid confusing legacy contracts that share an ID.
    Advancing the index and awarding/failing a contract are one checkpoint.
    """
    travel = world.save.pending_travel
    missions = (travel["escorts"] if travel is not None else
                [m.to_dict() for m in world.save.active_missions if m.kind == "escort"])
    index = travel["escort_index"] if travel is not None else 0
    dest = world.by_id[dest_id]
    while index < len(missions):
        mission = Mission.from_dict(missions[index])
        if mission_expired(world, mission):
            world.save.active_missions.remove(mission)
            message = f"Mission expired: {mission.description}"
            world.save.pilot.note(message)
            index += 1
            if travel is not None:
                travel["escort_index"] = index
                travel["encounter"] = {}
            world.checkpoint()
            out_line(f"{p.wrong}{message}{RESET}")
            if world.ship_destroyed_this_hop:
                break
            continue
        state = _travel_encounter(world)
        if "pirate" not in state:
            state["pirate"] = dataclasses.asdict(generate_pirate(world, tier=mission.pirate_tier))
            world.checkpoint()
        pirate = Pirate(**state["pirate"])
        out_line(f"{p.wrong}Raiders ambush the convoy you're escorting -- the {pirate.name} closes in.{RESET}")
        outcome = screen_combat(p, world, pirate)
        lines = []
        if outcome == "won":
            if dest_id == mission.target_system:
                world.save.active_missions.remove(mission)
                reward = bounty_reward_for(world, mission.reward)
                world.save.pilot.credits += reward
                if world.save.pilot.missions_completed == 0:
                    world.save.pilot.highlight(f"First mission complete: {mission.description}.")
                world.save.pilot.missions_completed += 1
                world.save.pilot.note(f"Escort complete: {mission.description} (+{reward}cr)")
                world.save.pilot.highlight(f"Escorted a convoy safely to {dest.name}.")
                lines.append(f"Convoy delivered safely! +{reward}cr")
            else:
                lines.append("The convoy presses on.")
        else:
            world.save.active_missions.remove(mission)
            world.save.pilot.note(f"Escort failed: {mission.description}")
            lines.append("You disengage -- the convoy is left defenseless. Escort contract failed."
                         if outcome == "escaped" else "Escort contract failed -- the convoy was lost.")
        index += 1
        if travel is not None:
            travel["escort_index"] = index
            travel["encounter"] = {}
        world.checkpoint()
        for line in lines:
            out_line(f"{p.gold}{line}{RESET}")
        if world.ship_destroyed_this_hop:
            break


def _resolve_bounty(p: Palette, world: World, travel: dict) -> None:
    bounty = Mission.from_dict(travel["bounty"])
    if mission_expired(world, bounty):
        world.save.active_missions.remove(bounty)
        message = f"Mission expired: {bounty.description}"
        world.save.pilot.note(message)
        travel["phase"] = "escorts"
        travel["encounter"] = {}
        world.checkpoint()
        out_line(f"{p.wrong}{message}{RESET}")
        return
    state = _travel_encounter(world)
    if "pirate" not in state:
        state["pirate"] = dataclasses.asdict(generate_pirate(world, tier=bounty.pirate_tier))
        world.checkpoint()
    pirate = Pirate(**state["pirate"])
    out_line(f"{p.wrong}Your bounty target, the {pirate.name}, is waiting.{RESET}")
    outcome = screen_combat(p, world, pirate)
    lines = []
    if outcome == "won":
        world.save.active_missions.remove(bounty)
        reward = bounty_reward_for(world, bounty.reward)
        world.save.pilot.credits += reward
        if world.save.pilot.missions_completed == 0:
            world.save.pilot.highlight(f"First mission complete: {bounty.description}.")
        world.save.pilot.missions_completed += 1
        world.save.pilot.note(f"Bounty complete: {bounty.description} (+{reward}cr)")
        lines.append(f"Bounty complete! +{reward}cr")
        if world.event_rng.random() < WRONG_BOUNTY_KILL_CHANCE:
            world.save.pilot.notoriety += NOTORIETY_PER_WRONG_BOUNTY_KILL
            adjust_reputation(world, FACTION_CONCORD, -3)
            world.save.pilot.note("Concord inquiry: that bounty kill was mistaken identity -- notoriety rises.")
            lines.append("Later, a Concord inquiry flags an irregularity: that 'raider' matches an "
                         "informant's registered ship. Notoriety rises.")
    elif outcome == "destroyed":
        world.save.active_missions.remove(bounty)
        world.save.pilot.note(f"Bounty failed: {bounty.description}")
        lines.append(f"Bounty failed -- the {pirate.name} was too much this time.")
    # A cached terminal combat result remains until its parent commits rewards
    # and advances phase. A restart in between cannot repeat loot or the fight.
    travel["phase"] = "escorts"
    travel["encounter"] = {}
    world.checkpoint()
    for line in lines:
        out_line(f"{p.gold}{line}{RESET}")


def screen_travel(p: Palette, world: World, dest_id: int) -> None:
    """Finish one durable hop. A pending hop always wins over a new request.

    Departure, primary encounter, escort waves, docking, and customs each
    advance monotonically. Effects and their phase/index commit together;
    combat keeps its own terminal result until its parent consumes it.
    """
    travel = world.save.pending_travel
    if travel is None:
        origin = world.here
        dest = world.by_id[dest_id]
        world.save.ship.fuel -= fuel_cost_for_jump(origin, dest, world.save.ship)
        world.save.turn += 1
        world.ship_destroyed_this_hop = False
        lines = [f"Jumping to {'the unknown' if not dest.discovered else dest.name}..."]
        lines.extend(expire_missions(world))
        tick_price_reversion(world)
        event_msg = tick_economy_event(world)
        if event_msg:
            lines.append(event_msg)
        lines.extend(pay_crew_wages(world))
        lines.extend(settle_futures_contracts(world, legacy_only=True))
        was_discovered = dest.discovered
        dest.discovered = True
        if not was_discovered:
            lines.append(f"New system charted: {dest.name}.")
        bounty = next((m for m in world.save.active_missions
                       if m.kind == "bounty" and m.target_system == dest_id), None)
        primary = "bounty" if bounty is not None else (
            "patrol" if world.event_rng.random() < notoriety_patrol_chance(world.save.pilot.notoriety)
            else "random"
        )
        travel = world.save.pending_travel = {
            "version": 1, "origin": origin.id, "destination": dest_id,
            "was_discovered": was_discovered, "destroyed": False,
            "phase": "primary", "primary": primary,
            "bounty": bounty.to_dict() if bounty is not None else None,
            "escorts": [m.to_dict() for m in world.save.active_missions if m.kind == "escort"],
            "escort_index": 0, "encounter": {},
        }
        world.checkpoint()
        for line in lines:
            out_line(f"{p.gold}{line}{RESET}")
    dest_id = travel["destination"]
    dest = world.by_id[dest_id]
    if travel["phase"] == "primary":
        if travel["primary"] == "bounty":
            _resolve_bounty(p, world, travel)
        else:
            if travel["primary"] == "patrol":
                screen_notoriety_patrol(p, world)
            else:
                _resolve_random_travel_encounter(p, world, dest)
            travel["phase"] = "escorts"
            travel["encounter"] = {}
            world.checkpoint()
    if travel["phase"] == "escorts":
        # If an escort fight destroyed the ship, its cached result still needs
        # consuming (contract failure). If a prior encounter did, skip escorts.
        if not world.ship_destroyed_this_hop or travel["encounter"].get("combat"):
            _resolve_escort_missions(p, world, dest_id)
        travel["phase"] = "arrival"
        travel["encounter"] = {}
        world.checkpoint()
    if travel["phase"] == "arrival":
        lines = []
        inspect = False
        if not world.ship_destroyed_this_hop:
            world.save.current_system = dest_id
            lines = settle_futures_contracts(world)
            lines += check_mission_completions(
                world, just_discovered=None if travel["was_discovered"] else dest_id,
            )
            if has_contraband(world) and dest.economy != "Haven":
                chance = customs_check_chance(dest)
                if world.save.pilot.has_blackwake_made:
                    chance *= (1 - BLACKWAKE_MADE_CUSTOMS_REDUCTION)
                inspect = world.event_rng.random() < chance
        travel["phase"] = "customs"
        travel["encounter"] = {"inspect": inspect}
        world.checkpoint()
        for line in lines:
            out_line(f"{p.gold}{line}{RESET}")
    if travel["encounter"].get("inspect"):
        screen_customs(p, world)
    world.save.pending_travel = None
    world.checkpoint()
    out_line()


def screen_combat(p: Palette, world: World, pirate: Pirate) -> str:
    """Return won/escaped/destroyed; preserve a terminal result until consumed."""
    return _screen_combat_session(p, world, pirate, patrol=False)


def screen_notoriety_patrol(p: Palette, world: World) -> None:
    state = _travel_encounter(world)
    if "pirate" not in state:
        state["pirate"] = dataclasses.asdict(generate_concord_patrol(world))
        world.checkpoint()
    patrol = Pirate(**state["pirate"])
    out_line(f"{p.wrong}A Concord patrol vessel, the {patrol.name}, intercepts you -- "
              f"your transponder flags as wanted.{RESET}")
    _screen_combat_session(p, world, patrol, patrol=True)


def _screen_combat_session(p: Palette, world: World, pirate: Pirate, *, patrol: bool) -> str:
    """Commit each decision's full effects and opponent HP before narration.

    Patrols share turn handling but retain their separate law-enforcement
    consequences: no salvage, hostile Concord reputation, and surrender fines.
    Only the last exchange is retained, so a long fight cannot grow the save.
    """
    encounter = _travel_encounter(world)
    combat = encounter.get("combat")
    if combat is None:
        combat = encounter["combat"] = {
            "pirate": dataclasses.asdict(pirate), "outcome": None, "lines": [],
        }
        world.checkpoint()
    else:
        pirate = Pirate(**combat["pirate"])
        for line in combat["lines"]:
            out_line(f"  {line}")
        if combat["outcome"] is not None:
            return combat["outcome"]
    ship = world.save.ship
    fine = notoriety_fine_cost(world.save.pilot.notoriety)
    while True:
        pirate_bar = _gauge_bar(pirate.hp, pirate.hp_max, 8, p)
        hull_bar = _gauge_bar(ship.hull_hp, hull_hp_max(ship), 8, p)
        out_line(
            f"  {p.wrong}{BOLD}{pirate.name}{RESET} (tier {pirate.tier})  "
            f"HP {pirate_bar} {pirate.hp}/{pirate.hp_max}   |   "
            f"{p.accent}Your hull{RESET} {hull_bar} {ship.hull_hp}/{hull_hp_max(ship)}"
        )
        can_pay = world.save.pilot.credits >= (fine if patrol else bribe_cost(pirate))
        actions = "[F]ight [E]vade"
        if patrol:
            if can_pay:
                actions += f" [S]urrender & pay {fine}cr"
        else:
            actions += " [D]ump&evade"
            if can_pay:
                actions += " [B]ribe"
        out_prompt(f"{p.muted}{actions} [Q]uick status: {RESET}")
        action = read_command()
        out_line(action)
        lines = []
        outcome = None
        if action == "Q":
            out_line(
                f"  {p.accent}Tactical Systems:{RESET} Hull {ship.hull_hp}/{hull_hp_max(ship)} | "
                f"Shields Tier {ship.shield_tier} (-{ship.shield_tier * 3} dmg) | "
                f"Weapons Tier {ship.weapon_tier} (+{ship.weapon_tier * 4} dmg) | "
                f"Bank {world.save.pilot.credits:,} cr | Notoriety {world.save.pilot.notoriety}"
            )
            continue
        if action == "F":
            _, _, lines = fight_round(world, pirate)
            if pirate.hp <= 0:
                if world.save.pilot.kills == 0:
                    label = "Concord patrol vessel " if patrol else ""
                    world.save.pilot.highlight(f"First kill: destroyed the {label}{pirate.name}.")
                world.save.pilot.kills += 1
                if patrol:
                    world.save.pilot.notoriety += 3
                    adjust_reputation(world, FACTION_CONCORD, -10)
                    adjust_reputation(world, FACTION_BLACKWAKE, 3)
                    world.save.pilot.note("Destroyed a Concord patrol vessel -- notoriety rises further.")
                    lines.append(f"The {pirate.name} is destroyed -- Concord will not forget this.")
                else:
                    loot = 40 + pirate.tier * 60
                    world.save.pilot.credits += loot
                    adjust_reputation(world, FACTION_CONCORD, 2)
                    adjust_reputation(world, FACTION_BLACKWAKE, -1)
                    lines.append(f"Salvage recovered: {loot}cr.")
                outcome = "won"
        elif action == "E" or (action == "D" and not patrol):
            dumped = False
            available_cargo = [c for c, quantity in world.save.cargo.items() if quantity > 0]
            if action == "D" and available_cargo:
                commodity = world.event_rng.choice(available_cargo)
                _dispose_cargo(world, commodity, 1)
                dumped = True
                lines.append("You dump cargo to lighten the ship.")
            if world.event_rng.random() < evade_chance(world, pirate, dumped_cargo=dumped):
                lines.append("You break contact and escape.")
                outcome = "escaped"
            else:
                lines.append("Evasion failed -- they're still on you.")
                raw = world.event_rng.randint(4, 9) + pirate.tier * 4
                dmg = max(1, raw - ship.shield_tier * 3)
                ship.hull_hp = max(0, ship.hull_hp - dmg)
                lines.append(f"The {pirate.name} hits you for {dmg} damage.")
        elif action == "B" and not patrol and can_pay:
            cost = bribe_cost(pirate)
            if world.event_rng.random() < bribe_chance(world, pirate):
                world.save.pilot.credits -= cost
                adjust_reputation(world, FACTION_BLACKWAKE, 2)
                lines.append(f"The {pirate.name} takes {cost}cr and peels off.")
                outcome = "escaped"
            else:
                lines.append("They refuse the bribe and press the attack!")
                raw = world.event_rng.randint(4, 9) + pirate.tier * 4
                dmg = max(1, raw - ship.shield_tier * 3)
                ship.hull_hp = max(0, ship.hull_hp - dmg)
                lines.append(f"The {pirate.name} hits you for {dmg} damage.")
        elif action == "S" and patrol and can_pay:
            world.save.pilot.credits -= fine
            world.save.pilot.notoriety = 0
            adjust_reputation(world, FACTION_CONCORD, 2)
            world.save.pilot.note(f"Paid a {fine}cr fine to Concord -- notoriety cleared.")
            lines.append(f"You power down and pay the {fine}cr fine. Notoriety cleared.")
            outcome = "escaped"
        else:
            continue
        if ship.hull_hp <= 0 and outcome != "won":
            lines.append(destroy_ship(world))
            outcome = "destroyed"
        combat.update(pirate=dataclasses.asdict(pirate), outcome=outcome, lines=lines)
        world.checkpoint()
        for line in lines:
            out_line(f"  {line}")
        if outcome is not None:
            return outcome


def screen_customs(p: Palette, world: World) -> None:
    state = _travel_encounter(world)
    if state.get("done"):
        for line in state.get("result", []):
            out_line(f"{p.gold}{line}{RESET}")
        return
    contraband_qty = sum(q for c, q in world.save.cargo.items() if not COMMODITIES[c]["legal"])
    out_line()
    out_line(_box_title(p, "CONCORD CUSTOMS INSPECTION CHECKPOINT", border_color=p.wrong))
    hail_line = f"  {p.wrong}Concord customs hails you for a cargo inspection.{RESET}"
    out_line(f"{p.wrong}│{RESET}{hail_line}{' ' * max(0, 77 - _vis_len(hail_line))}{p.wrong}│{RESET}")
    scan_line = f"  {p.muted}Scanners detect {contraband_qty} units of unauthorized contraband in your cargo hold.{RESET}"
    out_line(f"{p.wrong}│{RESET}{scan_line}{' ' * max(0, 77 - _vis_len(scan_line))}{p.wrong}│{RESET}")
    out_line(_box_bottom(p, border_color=p.wrong))
    value = sum(q * COMMODITIES[c]["base"] for c, q in world.save.cargo.items() if not COMMODITIES[c]["legal"])
    while True:
        out_prompt(f"  {p.muted}[S]urrender contraband [B]ribe the inspector: {RESET}")
        action = read_command()
        out_line(action)
        if action in ("S", "B"):
            break
        out_line(f"{p.muted}Choose S or B. No cargo has been surrendered.{RESET}")
    if action == "B":
        cost = 100 + value // 2
        if world.save.pilot.credits >= cost and world.event_rng.random() < 0.6:
            world.save.pilot.credits -= cost
            _encounter_result(p, world, state, [f"{cost}cr changes hands quietly. Move along."])
            return
        fine = 150 + value
        world.save.pilot.credits = max(0, world.save.pilot.credits - fine)
        for c in CONTRABAND_COMMODITIES:
            _dispose_cargo(world, c, world.save.cargo.get(c, 0))
        adjust_reputation(world, FACTION_CONCORD, -5)
        # Notoriety only rises here, not on the cooperative "surrender
        # outright" path below -- a caught, refused bribe is a repeat-
        # offender bust; volunteering the contraband before a fight ever
        # starts is already the game's own "played fair, small rep
        # bonus" outcome (see the +1 just below), not a wanted-status
        # event on top of that.
        world.save.pilot.notoriety += NOTORIETY_PER_CUSTOMS_BUST
        _encounter_result(p, world, state, [f"Bribe refused -- contraband confiscated and a {fine}cr fine levied."])
        return
    for c in CONTRABAND_COMMODITIES:
        _dispose_cargo(world, c, world.save.cargo.get(c, 0))
    adjust_reputation(world, FACTION_CONCORD, 1)
    _encounter_result(p, world, state, [f"You surrender {contraband_qty} units without a fight."])


@dataclass(frozen=True)
class RecoveryResult:
    save: SaveData | None
    exit_code: int


def screen_save_recovery(p: Palette, save_dir: Path, user_id: int, error: ResumeError) -> RecoveryResult:
    """A failed load is not authorization to reset or roll back a career."""
    candidate = None
    previous = None
    preservation_problem = None
    if not isinstance(error, UnsupportedSave):
        try:
            previous = _read_save_bytes(_previous_save_path(save_dir, user_id))
            candidate = _decode_career(previous)
        except (OSError, ResumeError):
            pass
    if candidate is not None:
        try:
            _recovery_original(save_dir, user_id)
        except ResumeError as exc:
            preservation_problem = str(exc)
        except OSError:
            preservation_problem = "The current career or recovery storage cannot be read."
    lines = ["Career recovery", str(error),
             "Play has stopped; your saved career is unchanged."]
    if candidate is None:
        lines += ["No supported previous checkpoint is available.", "Please contact your SysOp."]
    else:
        lines += [f"Previous: {candidate.pilot.handle}",
                  f"Day {candidate.turn}; {candidate.pilot.credits:,} credits.",
                  "Journey pending: " + ("yes" if candidate.pending_travel else "no"),
                  "Restoring rolls back progress to this checkpoint.",
                  "Your current file will be kept as a recovery copy."]
        if preservation_problem is not None:
            lines += ["Automatic restoration is unavailable: " + preservation_problem,
                      "Please contact your SysOp for manual recovery."]
    pages = _mission_text_pages(lines, overhead=4)
    page = 0
    while True:
        out_line()
        for line in pages[page]:
            out_line(line)
        can_restore = candidate is not None and preservation_problem is None and page == len(pages) - 1
        action = "[R]estore  " if can_restore else ""
        out_prompt(action + "[N]ext [P]revious [B]ack: ")
        try:
            key = read_command()
        except EOFError:
            return RecoveryResult(None, 1)
        if key in ("B", "Q"):
            return RecoveryResult(None, 0)
        if key == "N" and page < len(pages) - 1:
            page += 1
        elif key == "P" and page:
            page -= 1
        elif key == "R" and can_restore:
            try:
                confirmed = confirm("Restore this previous checkpoint?", p)
            except EOFError:
                return RecoveryResult(None, 1)
            if confirmed:
                try:
                    restored = restore_previous_career(save_dir, user_id, previous)
                except (OSError, ResumeError):
                    out_line("Recovery failed. No replacement was completed. Please contact your SysOp.")
                    try:
                        pause(p)
                    except EOFError:
                        pass
                    return RecoveryResult(None, 1)
                out_line("Previous checkpoint restored. Resuming this career.")
                return RecoveryResult(restored, 0)


def main() -> int:
    global _OUTPUT_WIDTH, _OUTPUT_HEIGHT

    sys.stdout.reconfigure(encoding="utf-8")
    info = _load_door_info()
    try:
        _OUTPUT_WIDTH = max(1, int(info.get("terminal_width", 80)))
    except (TypeError, ValueError):
        _OUTPUT_WIDTH = 80
    try:
        _OUTPUT_HEIGHT = max(10, min(200, int(info.get("terminal_height", 24))))
    except (TypeError, ValueError):
        _OUTPUT_HEIGHT = 24
    p = Palette(truecolor=info.get("color_depth") == "truecolor")
    save_dir = _default_save_dir()
    # Real NetBBS launches always carry a real positive user_id from the
    # drop-file; this fallback only fires for standalone tinkering
    # (`python3 voidrunner.py` with no NETBBS_DOOR_INFO) and must be
    # deterministic per handle -- Python's built-in hash() is randomized
    # per process (PYTHONHASHSEED), which would silently start a fresh
    # career file on every standalone run.
    user_id = int(info.get("user_id", 0)) or zlib.crc32(info["handle"].encode())

    world = None
    lease = contextlib.ExitStack()
    try:
        try:
            lease.enter_context(pilot_session(save_dir, user_id))
        except OSError as exc:
            raise SaveError from exc
        screen_title(p, info)
        try:
            save, is_new, notice = load_or_create_save(save_dir, user_id, info["handle"])
        except ResumeError as exc:
            recovery = screen_save_recovery(p, save_dir, user_id, exc)
            save = recovery.save
            if save is None:
                return recovery.exit_code
            is_new, notice = False, None
        if notice:
            out_line(f"{p.wrong}{notice}{RESET}")
        if is_new:
            callsign = create_career(p, info)
            if callsign is None:
                return 0
            save.pilot.handle = callsign
        else:
            out_line(f"{p.muted}Welcome back, {save.pilot.handle}. Day {save.turn}.{RESET}")
        world = World(save, checkpoint=lambda current: persist(current, save_dir, user_id))
        world.checkpoint()
        if is_new:
            out_line("Start at [G] Pilot Guide for an optional first delivery and flight instructions.")
        elif world.save.pending_travel is None:
            for line in pilot_recap(world):
                out_line(line)
        if world.save.pending_travel is not None:
            out_line(f"{p.gold}Resuming your interrupted journey. Station access follows its resolution.{RESET}")
            screen_travel(p, world, world.save.pending_travel["destination"])
            pause(p)

        while True:
            choice = screen_station_menu(p, world)
            if choice == "T":
                screen_trading_ledger(p, world)
                continue
            if choice == "M":
                screen_market(p, world)
            elif choice == "Y":
                screen_shipyard(p, world)
            elif choice == "B":
                screen_missions(p, world)
                continue  # Browsing is read-only; acceptance checkpoints itself.
            elif choice == "G":
                screen_pilot_guide(p, world)
                continue  # Guide browsing is read-only; acceptance checkpoints itself.
            elif choice == "C":
                dest = screen_chart(p, world)
                if dest is not None:
                    screen_travel(p, world, dest)
            elif choice == "S":
                screen_status(p, world)
            elif choice == "H":
                screen_hall_of_fame(p, world, save_dir, user_id)
            elif choice == "L" and landmark_available_here(world):
                screen_landmark(p, world)
            elif choice == "D" and has_contraband(world):
                screen_dump_contraband(p, world)
            elif choice == "P" and concord_commission_available(world):
                screen_concord_commission(p, world)
            elif choice == "W" and blackwake_made_available(world):
                screen_blackwake_made(p, world)
            elif choice == "Q":
                world.checkpoint()
                out_line(f"{p.muted}Docking clamps engaged. Fly safe, {world.save.pilot.handle}.{RESET}")
                return 0
            else:
                continue
            world.checkpoint()
    except PilotBusy:
        out_line(f"{p.gold}This pilot already has an active Voidrunner session, or save maintenance is in progress. "
                 f"Close that session or wait for maintenance to finish, then try again.{RESET}")
        try:
            pause(p)
        except EOFError:
            pass
        return 0
    except ResumeError as exc:
        out_line(f"{p.wrong}{exc} Play has stopped; your saved career is unchanged. "
                 f"Please contact your SysOp.{RESET}")
        try:
            pause(p)
        except EOFError:
            pass
        return 1
    except SaveError:
        out_line(f"{p.wrong}Save failed. Play has stopped to protect your last saved career. "
                 f"Please contact your SysOp before playing again.{RESET}")
        try:
            pause(p)
        except EOFError:
            pass
        return 1
    except EOFError:
        try:
            if world is not None:
                world.checkpoint()
        except SaveError:
            return 1
        return 0
    finally:
        lease.close()
        out(RESET)


if __name__ == "__main__":
    sys.exit(main())
