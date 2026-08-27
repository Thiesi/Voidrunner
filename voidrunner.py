#!/usr/bin/env python3
"""
Voidrunner -- a persistent single-player space trading/exploration door
for NetBBS (issue #172 vertical: door-managed private save).

Same v1 door contract as `netbbs.doors.bundled.retro_trivia`: reads the
drop-file NetBBS hands it via `NETBBS_DOOR_INFO` for handle/user_id/color
depth, then owns raw stdin/stdout for the whole session (single keystroke
reads; this module adds its own small raw line-reader on top, for
numeric quantities and the callsign prompt, since NetBBS gives a door no
line-editing help). Runnable completely standalone outside NetBBS too.
Zero external dependencies -- stdlib only.

**Persistence**: NetBBS's door sandbox gives a door no database access
and deletes its scratch working directory after every session (see
`netbbs.doors.runtime`'s own docstring) -- a door manages any save data
entirely itself. This door keeps one JSON save file per caller, keyed by
the drop-file's stable numeric `user_id` (never the handle, which can
change), under `VOIDRUNNER_SAVE_DIR` if set, else `~/.netbbs/
voidrunner_saves/`. State is written after every state-changing action
(atomically, via a temp file + `os.replace`), not just on explicit quit
-- a door can be killed at any moment (caller disconnect, wall-clock
timeout) with no graceful-shutdown guarantee, so "save on quit only"
would lose real progress.

The default save location is deliberately *not* relative to this
script's own path: this module now ships as real installed package data
(`netbbs.doors.bundled`, resolved via `importlib.resources` -- see
`netbbs.doors.bundled.resolve_bundled_door_path`), and an installed
package's own directory is routinely read-only and/or wiped clean on
every upgrade, neither of which a save file can tolerate. A production
node with an unusual layout should set `VOIDRUNNER_SAVE_DIR` explicitly
rather than rely on the home-directory default holding for its own
service account.

**Architecture** (deliberate, for a reason beyond this door): the rules
of the game -- galaxy generation, pricing, combat resolution, mission
logic -- live in plain functions/dataclasses that only ever take a
`World` and return a new one plus narrative text (the "domain layer"
below); a small `load_or_create_save`/`write_save` pair is the only thing
that touches a filesystem path (the "storage layer"); everything that
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
state, no networking).

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
import dataclasses
import json
import os
import random
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

ESC = "\x1b"
RESET = f"{ESC}[0m"
BOLD = f"{ESC}[1m"


# ---------------------------------------------------------------------------
# Drop-file + raw terminal I/O (mirrors retro_trivia.py's own conventions;
# duplicated rather than imported so this remains one self-contained file a
# SysOp can point straight at -- see this module's own docstring).
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
    out(text + "\r\n")


def read_key() -> str:
    """One raw byte -- see this module's own docstring. A door owns the
    raw terminal stream; there is no line-editing help from NetBBS."""
    data = sys.stdin.buffer.read(1)
    if not data:
        raise EOFError("stdin closed")
    return data.decode("ascii", errors="replace")


def read_line_raw(max_len: int = 20, allowed=str.isdigit) -> str:
    """A minimal raw-mode line reader: backspace (BS/DEL) edits, Enter
    submits, a leading `ESC [ <letter>` CSI sequence (arrow keys and
    friends) is swallowed whole rather than leaking stray bytes into the
    buffer. `allowed` filters which characters are accepted at all --
    `str.isdigit` for quantity prompts, alnum+space for the callsign
    prompt."""
    buf: list[str] = []
    while True:
        ch = read_key()
        if ch in ("\r", "\n"):
            out_line()
            return "".join(buf)
        if ch in ("\x7f", "\x08"):
            if buf:
                buf.pop()
                out("\x08 \x08")
            continue
        if ch == ESC:
            nxt = read_key()
            if nxt == "[":
                while True:
                    b = read_key()
                    if b.isalpha() or b == "~":
                        break
            continue
        if not allowed(ch):
            continue
        if len(buf) >= max_len:
            continue
        buf.append(ch)
        out(ch)


def confirm(prompt: str, p: Palette) -> bool:
    out(f"{p.muted}{prompt} [Y/N] {RESET}")
    while True:
        key = read_key().upper()
        if key == "Y":
            out_line("Y")
            return True
        if key == "N":
            out_line("N")
            return False


def pause(p: Palette, msg: str = "Press any key to continue...") -> None:
    out(f"{p.muted}{msg}{RESET}")
    read_key()
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
               "effect": "+8 fuel capacity, cheaper/safer jumps"},
    "weapon": {"label": "Weapon Systems", "max_tier": 4, "cost": lambda t: 1000 + t * 900,
               "effect": "+combat damage"},
    "shield": {"label": "Deflector Shields", "max_tier": 3, "cost": lambda t: 1100 + t * 950,
               "effect": "-incoming damage"},
    "scanner": {"label": "Long-Range Scanner", "max_tier": 2, "cost": lambda t: 900 + t * 700,
                "effect": "+scan range/quality"},
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
    "engineer": {"label": "Engineer", "hire_cost": 700, "wage": 12, "effect": "-1 fuel cost per jump (min 1)"},
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

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Mission":
        return cls(**{f.name: d[f.name] for f in dataclasses.fields(cls) if f.name in d})


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

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
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
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SaveData":
        return cls(
            schema_version=d["schema_version"],
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
        )


class World:
    """The whole live game session: the regenerated (never persisted)
    galaxy, plus the persisted `SaveData`. Every domain function below
    takes a `World` and mutates it in place, returning narrative lines --
    see this module's own docstring for why that boundary matters."""

    def __init__(self, save: SaveData):
        self.reset(save)

    def reset(self, save: SaveData) -> None:
        """Re-derives every galaxy-shaped attribute from `save` in
        place -- the same work `__init__` itself does (and now
        delegates to), factored out so `retire_pilot`'s own New Game+
        reset can reuse it exactly rather than duplicating "how to
        rebuild a World from a SaveData" a second time. Never disturbs
        an existing `World` object's identity, only its contents -- a
        retiring pilot's `world` variable stays the same object, just
        pointed at a fresh galaxy/save underneath."""
        self.save = save
        self.galaxy: list[GalaxySystem] = generate_galaxy(save.seed)
        self.by_id: dict[int, GalaxySystem] = {s.id: s for s in self.galaxy}
        for sid in save.discovered:
            if sid in self.by_id:
                self.by_id[sid].discovered = True
        self.landmark: dict = generate_landmark(save.seed, self.galaxy)
        self.event_rng = random.Random()

    @property
    def here(self) -> GalaxySystem:
        return self.by_id[self.save.current_system]

    def sync_discovered(self) -> None:
        self.save.discovered = [s.id for s in self.galaxy if s.discovered]


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
        cost = max(1, cost - 1)
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


# ---------------------------------------------------------------------------
# Missions
# ---------------------------------------------------------------------------


def generate_mission_board(world: World) -> list[Mission]:
    rng = world.event_rng
    hops = bfs_hops(world.by_id, world.save.current_system)
    board: list[Mission] = []
    for kind in rng.sample(["delivery", "delivery", "bounty", "scan", "escort"], k=rng.randint(3, 4)):
        mission = _generate_mission(world, kind, hops)
        if mission is not None:
            board.append(mission)
    return board


def _generate_mission(world: World, kind: str, hops: dict[int, int]) -> Mission | None:
    rng = world.event_rng
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
    world.save.active_missions.append(mission)
    world.save.next_mission_id += 1


def check_mission_completions(world: World, *, just_discovered: int | None = None) -> list[str]:
    msgs: list[str] = []
    still_active: list[Mission] = []
    for m in world.save.active_missions:
        done = False
        if m.kind == "delivery" and world.save.current_system == m.target_system:
            have = world.save.cargo.get(m.commodity, 0)
            if have >= m.quantity:
                world.save.cargo[m.commodity] = have - m.quantity
                if world.save.cargo[m.commodity] <= 0:
                    del world.save.cargo[m.commodity]
                done = True
        elif m.kind == "scan" and just_discovered == m.target_system:
            done = True
        if done:
            world.save.pilot.credits += m.reward
            if world.save.pilot.missions_completed == 0:
                world.save.pilot.highlight(f"First mission complete: {m.description}.")
            world.save.pilot.missions_completed += 1
            msg = f"Mission complete: {m.description} (+{m.reward}cr)"
            world.save.pilot.note(msg)
            msgs.append(msg)
        elif m.deadline_turn is not None and world.save.turn > m.deadline_turn:
            msg = f"Mission expired: {m.description}"
            world.save.pilot.note(msg)
            msgs.append(msg)
        else:
            still_active.append(m)
    world.save.active_missions = still_active
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


def adjust_reputation(world: World, faction: str, delta: int) -> None:
    rep = world.save.pilot.reputation
    rep[faction] = max(-100, min(100, rep.get(faction, 0) + delta))


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
    world.save.cargo.clear()
    penalty = min(world.save.pilot.credits, 200 + world.save.ship.hull_tier * 50)
    world.save.pilot.credits -= penalty
    world.save.ship.hull_hp = hull_hp_max(world.save.ship)
    world.save.current_system = 0
    world.save.pilot.notoriety = 0
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
        del world.save.cargo[c]
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


def _default_save_dir() -> Path:
    override = os.environ.get("VOIDRUNNER_SAVE_DIR")
    if override:
        return Path(override)
    # Not `Path(__file__).resolve().parent` -- this module ships as real
    # installed package data now (see this module's own docstring), and
    # an installed package's own directory is routinely read-only and/or
    # wiped on upgrade. `Path.home()` resolves to whatever OS user is
    # actually running NetBBS (the door sandbox's own same-OS-user
    # model), the same account that already owns the node's other state.
    try:
        home = Path.home()
    except RuntimeError:
        # `netbbs.doors.runtime.run_door` launches every door with its
        # environment replaced outright -- only `NETBBS_DOOR_INFO`
        # survives (see that module's own docstring) -- so `HOME`/
        # `USERPROFILE` never reach this process either. POSIX's
        # `Path.home()` falls back to the OS password database
        # regardless of env vars and still resolves correctly; Windows
        # has no such fallback and raises exactly this. A production
        # Windows deployment (not NetBBS's primary NetBSD target) should
        # set `VOIDRUNNER_SAVE_DIR` explicitly rather than rely on this
        # -- functional, but not as durable a location as a real home
        # directory would be.
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
    new_save.pilot.retirements = retirements
    new_save.pilot.credits += retirements * RETIREMENT_STARTING_CREDITS_BONUS
    new_save.pilot.note(f"Retired as a {RANKS[-1][1]} (retirement #{retirements}) -- a new career begins.")
    new_save.pilot.highlight(f"Retired as a {RANKS[-1][1]} (retirement #{retirements}).")
    return new_save


def load_or_create_save(save_dir: Path, user_id: int, handle: str) -> tuple[SaveData, bool, str | None]:
    """Returns (save, is_new_career, notice). `notice`, if not None, is a
    message the UI should show the player once (e.g. a corrupt save was
    preserved rather than silently discarded)."""
    path = _save_path(save_dir, user_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            save = SaveData.from_dict(data)
            # `handle` (the caller's current login name) is deliberately
            # never written back onto `save.pilot.handle` here -- a
            # dogfood playtest caught this line unconditionally
            # clobbering a player's chosen in-game callsign back to
            # their BBS handle on *every* login, silently discarding the
            # whole point of `create_career`'s own callsign prompt. The
            # save is already keyed by the stable numeric `user_id`
            # (never the handle -- see this module's own docstring), so
            # nothing here actually depends on the two staying in sync.
            return save, False, None
        except (OSError, ValueError, KeyError, TypeError):
            backup = path.with_suffix(f".corrupt-{int(time.time())}")
            try:
                path.rename(backup)
                notice = f"Your previous save could not be read; it was preserved as {backup.name}."
            except OSError:
                notice = "Your previous save could not be read and a new career was started."
    else:
        notice = None
    return _new_career(handle), True, notice


def write_save(save_dir: Path, user_id: int, save: SaveData) -> None:
    save_dir.mkdir(parents=True, exist_ok=True)
    path = _save_path(save_dir, user_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(save.to_dict()), encoding="utf-8")
    os.replace(tmp, path)


# Cross-save Hall of Fame: a single shared leaderboard file living
# alongside every individual `{user_id}.json` save in the same sandbox
# directory -- no DB access needed, since the door's own save directory
# is already a real per-door filesystem sandbox (see this module's own
# docstring). "leaderboard.json" can never collide with a save file,
# since every save file's own name is a bare integer user_id.
HALL_OF_FAME_SIZE = 20


def _hall_of_fame_path(save_dir: Path) -> Path:
    return save_dir / "leaderboard.json"


def load_hall_of_fame(save_dir: Path) -> list[dict]:
    """Best-effort read of the shared leaderboard -- a missing or
    corrupt file just means an empty leaderboard, never a hard failure.
    This file is pure flavor for every individual save; nothing else in
    the game may ever depend on its contents."""
    path = _hall_of_fame_path(save_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def update_hall_of_fame(save_dir: Path, user_id: int, save: SaveData) -> None:
    """Refreshes this pilot's entry on the shared leaderboard -- called
    from `persist()`, so it happens automatically after every state-
    changing action, the same cadence as `write_save` itself. Every
    field except `best_credits` always reflects the pilot's current,
    latest-known state (so retirements/kills/missions_completed never
    go stale); `best_credits` alone only ever ratchets upward, since a
    losing streak -- or a deliberate retirement's own credits reset --
    shouldn't erase a prior high-water mark. Entirely best-effort and
    additive: any failure here must never interrupt or corrupt the real
    per-pilot save file written right next to it."""
    try:
        all_entries = load_hall_of_fame(save_dir)
        prior_best = next((e.get("best_credits", 0) for e in all_entries if e.get("user_id") == user_id), 0)
        entries = [e for e in all_entries if e.get("user_id") != user_id]
        pilot = save.pilot
        best_credits = max(pilot.credits, prior_best)
        entries.append({
            "user_id": user_id,
            "handle": pilot.handle,
            "best_credits": best_credits,
            "rank": rank_for(best_credits),
            "retirements": pilot.retirements,
            "kills": pilot.kills,
            "missions_completed": pilot.missions_completed,
        })
        entries.sort(key=lambda e: e.get("best_credits", 0), reverse=True)
        entries = entries[:HALL_OF_FAME_SIZE]
        save_dir.mkdir(parents=True, exist_ok=True)
        path = _hall_of_fame_path(save_dir)
        tmp = path.with_suffix(".hof.tmp")
        tmp.write_text(json.dumps(entries), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def persist(world: World, save_dir: Path, user_id: int) -> None:
    world.sync_discovered()
    write_save(save_dir, user_id, world.save)
    update_hall_of_fame(save_dir, user_id, world.save)


# ---------------------------------------------------------------------------
# UI layer -- everything below here is the only code allowed to touch
# sys.stdin/sys.stdout directly.
# ---------------------------------------------------------------------------

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def draw_status_bar(p: Palette, world: World) -> None:
    ship = world.save.ship
    pilot = world.save.pilot
    out_line(
        f"{p.muted}{world.here.station_name} ({world.here.economy}, danger {world.here.danger}){RESET}  "
        f"{p.gold}{pilot.credits}cr{RESET}  "
        f"{p.accent}Hull {ship.hull_hp}/{hull_hp_max(ship)}{RESET}  "
        f"{p.accent}Fuel {ship.fuel}/{fuel_capacity(ship)}{RESET}  "
        f"{p.muted}Day {world.save.turn}{RESET}"
    )


def screen_title(p: Palette, info: dict) -> None:
    out_line()
    out_line(f"{p.title}{BOLD}+==============================================+{RESET}")
    out_line(f"{p.title}{BOLD}|{RESET}          {p.gold}{BOLD}V O I D R U N N E R{RESET}          {p.title}{BOLD}|{RESET}")
    out_line(f"{p.title}{BOLD}+==============================================+{RESET}")
    out_line(f"{p.muted}A {info['node_name']} space trading door.{RESET}")
    out_line()


def create_career(p: Palette, info: dict) -> str:
    out_line(f"{p.accent}No career on file for {info['handle']} -- let's fix that.{RESET}")
    out(f"{p.muted}Pilot callsign [{info['handle']}]: {RESET}")
    entered = read_line_raw(max_len=16, allowed=lambda c: c.isalnum() or c == " ").strip()
    callsign = entered or info["handle"]
    out_line()
    out_line(f"{p.muted}You'll start at Freeport Anchorage with a battered Shuttle, "
              f"1200 credits, and an empty hold.{RESET}")
    if confirm(f"Launch {callsign}'s career?", p):
        return callsign
    out_line(f"{p.muted}Understood -- sticking with {info['handle']}.{RESET}")
    return info["handle"]


def screen_station_menu(p: Palette, world: World) -> str:
    if is_stranded(world):
        # Checked here, not only right after the action that could cause
        # it -- this is the outer loop's own home base, reached after
        # every single action, so it catches every path into the stuck
        # state (a refuel/repair that spent the last credits, a jump
        # that burned the last fuel with no encounter) in one place.
        out_line()
        out_line(f"{p.wrong}{rescue_stranded_pilot(world)}{RESET}")
        pause(p)
    promoted = check_rank_up(world)
    if promoted:
        out_line()
        out_line(f"{p.gold}{BOLD}Promoted to {promoted}!{RESET}")
        pause(p)
    out_line()
    draw_status_bar(p, world)
    out_line(f"{p.accent}{BOLD}{world.here.station_name}{RESET}")
    menu = (f"  {p.gold}[M]{RESET}arket   {p.gold}[Y]{RESET}ard   {p.gold}[B]{RESET}oard   "
            f"{p.gold}[C]{RESET}hart   {p.gold}[S]{RESET}tatus   {p.gold}[H]{RESET}all of Fame   "
            f"{p.gold}[Q]{RESET}uit & save")
    if landmark_available_here(world):
        menu += f"   {p.gold}[L]{RESET} {world.landmark['label']}"
    if has_contraband(world):
        menu += f"   {p.gold}[D]{RESET}ump contraband"
    out_line(menu)
    out(f"{p.muted}> {RESET}")
    return read_key().upper()


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
    out_line(f"{p.muted}{dump_all_contraband(world)}{RESET}")


def screen_landmark(p: Palette, world: World) -> None:
    landmark = world.landmark
    out_line()
    out_line(f"{p.accent}{BOLD}{landmark['label']}{RESET}")
    out_line(f"  {landmark['flavor']}")
    world.save.flags["landmark_investigated"] = True
    world.save.pilot.credits += landmark["reward_credits"]
    world.save.pilot.note(f"Investigated {landmark['label']} (+{landmark['reward_credits']}cr)")
    world.save.pilot.highlight(f"Investigated {landmark['label']} (+{landmark['reward_credits']}cr).")
    out_line(f"{p.gold}Salvage recovered: +{landmark['reward_credits']}cr{RESET}")
    pause(p)


def screen_market(p: Palette, world: World) -> None:
    system = world.here
    while True:
        out_line()
        out_line(f"{p.accent}{BOLD}Market -- {system.station_name}{RESET}")
        rows: list[str] = []
        goods = LEGAL_COMMODITIES + (CONTRABAND_COMMODITIES if system.economy == "Haven" else [])
        for commodity in goods:
            buy = price_for(world, system.id, commodity)
            sell = round(buy * SELL_SPREAD)
            have = world.save.cargo.get(commodity, 0)
            label = COMMODITIES[commodity]["label"]
            tag = f"{p.wrong}[contraband]{RESET} " if not COMMODITIES[commodity]["legal"] else ""
            rows.append((commodity, f"  {p.gold}[{LETTERS[len(rows)]}]{RESET} {tag}{label:<16} "
                                     f"Buy {buy:>5}  Sell {sell:>5}   (have {have})"))
        for _, text in rows:
            out_line(text)
        cap = cargo_capacity(world.save.ship)
        used = sum(world.save.cargo.values())
        out_line(f"{p.muted}Cargo hold: {used}/{cap}{RESET}")
        out(f"{p.muted}Trade which, or [Q] back? {RESET}")
        key = read_key().upper()
        out_line(key)
        if key == "Q":
            return
        idx = LETTERS.index(key) if key in LETTERS else -1
        if idx < 0 or idx >= len(rows):
            continue
        commodity, _ = rows[idx]
        _trade_commodity(p, world, commodity)


def _trade_commodity(p: Palette, world: World, commodity: str) -> None:
    label = COMMODITIES[commodity]["label"]
    buy = price_for(world, world.here.id, commodity)
    sell = round(buy * SELL_SPREAD)
    out_line(f"{p.accent}{label}{RESET} -- Buy {buy}cr  Sell {sell}cr")
    out(f"{p.muted}[B]uy [S]ell [Q]cancel: {RESET}")
    action = read_key().upper()
    out_line(action)
    if action == "B":
        room = cargo_capacity(world.save.ship) - sum(world.save.cargo.values())
        affordable = world.save.pilot.credits // buy if buy else room
        max_qty = max(0, min(room, affordable))
        if max_qty <= 0:
            out_line(f"{p.wrong}No room or no credits.{RESET}")
            return
        out(f"{p.muted}Quantity (max {max_qty}, Enter to cancel): {RESET}")
        raw = read_line_raw(max_len=5)
        qty = int(raw) if raw.isdigit() else 0
        qty = min(qty, max_qty)
        if qty <= 0:
            return
        cost = qty * buy
        world.save.pilot.credits -= cost
        world.save.cargo[commodity] = world.save.cargo.get(commodity, 0) + qty
        _nudge_drift(world, world.here.id, commodity, min(0.05, qty * 0.01))
        out_line(f"{p.correct}Bought {qty}x {label} for {cost}cr.{RESET}")
        if not COMMODITIES[commodity]["legal"]:
            adjust_reputation(world, FACTION_BLACKWAKE, 1)
    elif action == "S":
        have = world.save.cargo.get(commodity, 0)
        if have <= 0:
            out_line(f"{p.wrong}You have none to sell.{RESET}")
            return
        out(f"{p.muted}Quantity (have {have}, Enter to cancel): {RESET}")
        raw = read_line_raw(max_len=5)
        qty = int(raw) if raw.isdigit() else 0
        qty = min(qty, have)
        if qty <= 0:
            return
        proceeds = qty * sell
        world.save.pilot.credits += proceeds
        world.save.cargo[commodity] -= qty
        if world.save.cargo[commodity] <= 0:
            del world.save.cargo[commodity]
        _nudge_drift(world, world.here.id, commodity, -min(0.05, qty * 0.01))
        out_line(f"{p.correct}Sold {qty}x {label} for {proceeds}cr.{RESET}")


def screen_shipyard(p: Palette, world: World) -> None:
    ship = world.save.ship
    while True:
        out_line()
        out_line(f"{p.accent}{BOLD}Shipyard -- {world.here.station_name}{RESET}")
        keys = list(UPGRADES.keys())
        for i, key in enumerate(keys):
            u = UPGRADES[key]
            tier = getattr(ship, f"{key}_tier")
            if tier >= u["max_tier"]:
                out_line(f"  {p.muted}[{LETTERS[i]}] {u['label']:<24} MAXED ({u['effect']}){RESET}")
            else:
                cost = u["cost"](tier)
                out_line(f"  {p.gold}[{LETTERS[i]}]{RESET} {u['label']:<24} "
                          f"tier {tier}->{tier + 1}  {cost}cr  ({u['effect']})")
        # Distinct letters, one past the last upgrade slot -- not a
        # hardcoded "[F]", which silently collided with Hull
        # Reinforcement's own slot letter (also "F", the 6th of 6
        # upgrades) the instant a 6th upgrade category existed. Two rows
        # both showing "[F]" on the same screen was never actually
        # ambiguous to the code (this is a different prompt from
        # [U]pgrade's own follow-up "Which upgrade?" letter), but it was
        # a real dogfood-caught display bug regardless -- confusing to
        # read even when not to press. `HULL_REFITS`'s own branching
        # ladder means this is 0, 1, or 2 rows depending on the current
        # hull class -- Shuttle owners see two independent refit choices
        # (Freighter or Cutter), not a hardcoded single "Flagship Refit"
        # row.
        refit_options = HULL_REFITS[ship.hull_class]
        refit_keys = LETTERS[len(keys) : len(keys) + len(refit_options)]
        if not refit_options:
            out_line(f"  {p.muted}Hull: already flying our best available class ({ship.hull_class}){RESET}")
        else:
            for refit_key, (target_class, cost) in zip(refit_keys, refit_options):
                out_line(f"  {p.gold}[{refit_key}]{RESET} {target_class}-Class Refit -- {cost}cr")
        out(f"{p.muted}Fuel: {ship.fuel}cr@ 6cr/unit  [U]pgrade [R]efuel [P]air hull "
            f"[C]rew [Q]back: {RESET}")
        action = read_key().upper()
        out_line(action)
        if action == "Q":
            return
        if action in refit_keys:
            target_class, cost = refit_options[refit_keys.index(action)]
            _hull_refit_screen(p, world, target_class, cost)
            continue
        if action == "U":
            out(f"{p.muted}Which upgrade? {RESET}")
            key_choice = read_key().upper()
            out_line(key_choice)
            if key_choice not in LETTERS[: len(keys)]:
                continue
            _buy_upgrade(p, world, keys[LETTERS.index(key_choice)])
        elif action == "R":
            _refuel(p, world)
        elif action == "P":
            _repair(p, world)
        elif action == "C":
            screen_crew(p, world)


def screen_crew(p: Palette, world: World) -> None:
    ship = world.save.ship
    while True:
        out_line()
        out_line(f"{p.accent}{BOLD}Crew Quarters{RESET}")
        keys = list(CREW_ROLES.keys())
        for i, role in enumerate(keys):
            info = CREW_ROLES[role]
            letter = LETTERS[i]
            if getattr(ship, f"has_{role}"):
                out_line(f"  {p.gold}[{letter}]{RESET} {info['label']:<12} "
                          f"HIRED -- {info['wage']}cr/jump  ({info['effect']})")
            else:
                out_line(f"  {p.gold}[{letter}]{RESET} {info['label']:<12} "
                          f"Hire for {info['hire_cost']}cr + {info['wage']}cr/jump  ({info['effect']})")
        out(f"{p.muted}Hire/dismiss which, or [Q] back? {RESET}")
        key = read_key().upper()
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
    out_line(f"{p.correct}{u['label']} upgraded to tier {tier + 1}.{RESET}")


def _refuel(p: Palette, world: World) -> None:
    ship = world.save.ship
    room = fuel_capacity(ship) - ship.fuel
    if room <= 0:
        out_line(f"{p.muted}Tanks are already full.{RESET}")
        return
    affordable = world.save.pilot.credits // 6
    max_qty = max(0, min(room, affordable))
    out(f"{p.muted}Fuel to buy (max {max_qty}, 6cr/unit): {RESET}")
    raw = read_line_raw(max_len=4)
    qty = int(raw) if raw.isdigit() else 0
    qty = min(qty, max_qty)
    if qty <= 0:
        return
    cost = qty * 6
    world.save.pilot.credits -= cost
    ship.fuel += qty
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
    out_line(f"{p.gold}{BOLD}Your {previous_class} is towed into drydock and emerges a {target_class}.{RESET}")
    out_line(f"{p.gold}Cargo, hull, and fuel capacity all jump considerably.{RESET}")


def screen_missions(p: Palette, world: World) -> None:
    board = generate_mission_board(world)
    while True:
        out_line()
        out_line(f"{p.accent}{BOLD}Mission Board -- {world.here.station_name}{RESET}")
        for i, m in enumerate(board):
            out_line(f"  {p.gold}[{LETTERS[i]}]{RESET} ({m.kind}) {m.description}  {p.gold}+{m.reward}cr{RESET}")
        if world.save.active_missions:
            out_line(f"{p.muted}Active missions:{RESET}")
            for m in world.save.active_missions:
                out_line(f"  {p.muted}- {m.description} (+{m.reward}cr){RESET}")
        out(f"{p.muted}Accept which, or [Q] back? {RESET}")
        key = read_key().upper()
        out_line(key)
        if key == "Q":
            return
        idx = LETTERS.index(key) if key in LETTERS else -1
        if idx < 0 or idx >= len(board):
            continue
        mission = board.pop(idx)
        accept_mission(world, mission)
        out_line(f"{p.correct}Accepted: {mission.description}{RESET}")


def screen_status(p: Palette, world: World) -> None:
    pilot, ship = world.save.pilot, world.save.ship
    out_line()
    out_line(f"{p.accent}{BOLD}{pilot.handle} -- {rank_for(pilot.credits)}{RESET}")
    out_line(f"  {p.gold}Credits:{RESET} {pilot.credits}")
    for faction in FACTIONS:
        out_line(f"  {p.gold}{FACTION_LABEL[faction]} standing:{RESET} {pilot.reputation.get(faction, 0)}")
    out_line(f"  {p.gold}Ship:{RESET} {ship.hull_class}  Hull {ship.hull_hp}/{hull_hp_max(ship)}  "
              f"Fuel {ship.fuel}/{fuel_capacity(ship)}  Cargo {cargo_capacity(ship)}")
    crew = [info["label"] for role, info in CREW_ROLES.items() if getattr(ship, f"has_{role}")]
    if crew:
        out_line(f"  {p.gold}Crew:{RESET} {', '.join(crew)}")
    out_line(f"  {p.gold}Systems charted:{RESET} {sum(1 for s in world.galaxy if s.discovered)}/{len(world.galaxy)}")
    out_line(f"  {p.gold}Missions completed:{RESET} {pilot.missions_completed}   "
              f"{p.gold}Raiders defeated:{RESET} {pilot.kills}")
    if pilot.retirements:
        out_line(f"  {p.gold}Retirements:{RESET} {pilot.retirements}")
    if pilot.highlights:
        out_line(f"{p.gold}Career highlights:{RESET}")
        for entry in pilot.highlights[-15:]:
            out_line(f"  {p.gold}* {entry}{RESET}")
    if pilot.log:
        out_line(f"{p.muted}Recent log:{RESET}")
        for entry in pilot.log[-8:]:
            out_line(f"  {p.muted}- {entry}{RESET}")
    if rank_for(pilot.credits) == RANKS[-1][1]:
        out(f"{p.muted}[R]etire and start a new career, or any other key to continue... {RESET}")
        key = read_key().upper()
        out_line(key)
        if key == "R":
            if confirm("This ends your current career for good and begins a new one. Retire?", p):
                world.reset(retire_pilot(world.save))
                out_line()
                out_line(f"{p.accent}{BOLD}A new career begins.{RESET}")
        return
    pause(p)


def screen_hall_of_fame(p: Palette, world: World, save_dir: Path, user_id: int) -> None:
    entries = load_hall_of_fame(save_dir)
    out_line()
    out_line(f"{p.accent}{BOLD}Hall of Fame{RESET}")
    if not entries:
        out_line(f"{p.muted}No pilots recorded yet -- be the first.{RESET}")
    else:
        for i, e in enumerate(entries, start=1):
            marker = f"{p.gold}*{RESET}" if e.get("user_id") == user_id else " "
            out_line(f"{marker}{i:>2}. {e.get('handle', '?'):<16} {e.get('rank', '?'):<24} "
                      f"{e.get('best_credits', 0):>8}cr   "
                      f"{p.muted}Retirements {e.get('retirements', 0)}   "
                      f"Kills {e.get('kills', 0)}   "
                      f"Missions {e.get('missions_completed', 0)}{RESET}")
    pause(p)


def screen_chart(p: Palette, world: World) -> str | None:
    """Returns a destination system id to travel to, or None if the
    player backed out."""
    while True:
        here = world.here
        out_line()
        out_line(f"{p.accent}{BOLD}Star Chart -- {here.name}{RESET}")
        options: list[int] = []
        for sid in sorted(here.connections):
            dest = world.by_id[sid]
            cost = fuel_cost_for_jump(here, dest, world.save.ship)
            letter = LETTERS[len(options)]
            if dest.discovered:
                out_line(f"  {p.gold}[{letter}]{RESET} {dest.name} ({dest.economy}, danger {dest.danger}) "
                          f"-- {cost} fuel")
            else:
                out_line(f"  {p.gold}[{letter}]{RESET} ??? (uncharted bearing) -- {cost} fuel")
            options.append(sid)
        scan_available = world.save.ship.scanner_tier > 0
        if scan_available:
            out_line(f"  {p.gold}[S]{RESET}can for distant contacts")
        out_line(f"  {p.gold}[G]{RESET}o to a charted system by name")
        out_line(f"  {p.gold}[V]{RESET}iew full chart by sector")
        out(f"{p.muted}Jump to which, or [Q] back? {RESET}")
        key = read_key().upper()
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
        idx = LETTERS.index(key) if key in LETTERS else -1
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
    out_line(f"{p.correct}Sensor contact! {world.by_id[target].name} is now on your chart.{RESET}")
    for msg in check_mission_completions(world, just_discovered=target):
        out_line(f"{p.gold}{msg}{RESET}")


def screen_galaxy_map(p: Palette, world: World) -> None:
    """Every discovered system, grouped by named sector -- the "readable
    chart at scale" the star chart's own short direct-neighbor list
    can't provide once a career has charted more than a handful of
    systems. Read-only: jumping still happens via the star chart's own
    neighbor list or the [G]o to auto-route, not here."""
    hops = bfs_hops(world.by_id, world.save.current_system)
    out_line()
    out_line(f"{p.accent}{BOLD}Charted Systems{RESET}")
    by_sector: dict[str, list[GalaxySystem]] = {}
    for system in world.galaxy:
        if system.discovered:
            by_sector.setdefault(sector_for(system), []).append(system)
    if not by_sector:
        out_line(f"{p.muted}Nothing charted yet.{RESET}")
    for sector in sorted(by_sector):
        out_line(f"{p.gold}{BOLD}{sector}{RESET}")
        for system in sorted(by_sector[sector], key=lambda s: s.name):
            marker = f"{p.accent}*{RESET}" if system.id == world.save.current_system else " "
            hop = hops.get(system.id)
            hop_label = "here" if hop == 0 else (f"{hop} jump(s)" if hop is not None else "unreachable")
            out_line(f"{marker} {system.name:<18} {system.economy:<12} danger {system.danger}   "
                      f"{p.muted}{hop_label}{RESET}")
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
    out(f"{p.muted}Go to (system name, Enter to cancel): {RESET}")
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
        out_line(f"{p.muted}Multiple matches:{RESET}")
        shown = candidates[:len(LETTERS)]
        for i, s in enumerate(shown):
            out_line(f"  {p.gold}[{LETTERS[i]}]{RESET} {s.name}")
        out(f"{p.muted}Which one, or [Q] cancel? {RESET}")
        key = read_key().upper()
        out_line(key)
        idx = LETTERS.index(key) if key in LETTERS else -1
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
        if world.save.current_system != hop_id:
            out_line(f"{p.wrong}Route interrupted.{RESET}")
            return


def _resolve_random_travel_encounter(p: Palette, world: World, dest: GalaxySystem) -> None:
    """Only reached when no bounty target is waiting at `dest`
    (`screen_travel`'s own bounty branch takes unconditional priority
    and never calls this). Rolls once for whether anything happens at
    all -- the same `0.08 + danger*0.05` chance that used to gate a
    single pirate check directly, unchanged, so overall encounter
    frequency/difficulty is unaffected -- then, only if so, rolls a
    second time for *what kind*, via `TRAVEL_ENCOUNTER_WEIGHTS`. This is
    what actually diversifies travel, not a higher overall chance of
    something happening."""
    if world.event_rng.random() >= 0.08 + dest.danger * 0.05:
        return
    kind = world.event_rng.choices(
        list(TRAVEL_ENCOUNTER_WEIGHTS), weights=list(TRAVEL_ENCOUNTER_WEIGHTS.values())
    )[0]
    if kind == "pirate":
        pirates = generate_pirate_squadron(world, dest)
        if len(pirates) > 1:
            out_line(f"{p.wrong}Raider squadron contact: {len(pirates)} ships incoming!{RESET}")
        else:
            out_line(f"{p.wrong}Raider contact: the {pirates[0].name}!{RESET}")
        for pirate in pirates:
            # Only a genuine kill lets the fight continue to the next
            # ship -- an evade/bribe ("escaped") or a destroyed ship
            # ends the whole encounter, not just this one ship. Bribing
            # or evading pirate #1 and then being ambushed by pirate #2
            # anyway would read as a bait-and-switch, not a squadron.
            if screen_combat(p, world, pirate) != "won":
                break
    elif kind == "derelict":
        _encounter_derelict(p, world)
    elif kind == "distress":
        _encounter_distress_call(p, world)
    elif kind == "tip":
        _encounter_market_tip(p, world, dest)


def _encounter_derelict(p: Palette, world: World) -> None:
    """A passive salvage opportunity, not a fight -- boarding is a real
    risk/reward choice (a genuine haul most of the time, a hidden
    ambush occasionally); declining is free, matching every other
    optional-encounter convention already in this file (bribe, evade)
    where the safe choice alone is never punished."""
    out_line(f"{p.muted}Sensors pick up a derelict hulk drifting nearby.{RESET}")
    out(f"{p.muted}[B]oard for salvage or [I]gnore and continue? {RESET}")
    action = read_key().upper()
    out_line(action)
    if action != "B":
        return
    if world.event_rng.random() < 0.70:
        reward = world.event_rng.randint(80, 60 + world.here.danger * 120)
        world.save.pilot.credits += reward
        world.save.pilot.note(f"Salvaged a derelict hulk (+{reward}cr).")
        out_line(f"{p.correct}Salvage recovered: {reward}cr.{RESET}")
    else:
        out_line(f"{p.wrong}The wreck's defenses weren't as dead as they looked!{RESET}")
        pirate = generate_pirate(world)
        screen_combat(p, world, pirate)


def _encounter_distress_call(p: Palette, world: World) -> None:
    """Helping costs a few fuel units (diverting off the direct route)
    for a credit reward and Concord standing; ignoring is free, same
    "declining costs nothing" convention as `_encounter_derelict`."""
    out_line(f"{p.muted}A garbled distress signal reaches your comms.{RESET}")
    out(f"{p.muted}[H]elp (costs fuel) or [I]gnore and continue? {RESET}")
    action = read_key().upper()
    out_line(action)
    if action != "H":
        return
    fuel_cost = min(world.save.ship.fuel, world.event_rng.randint(2, 4))
    world.save.ship.fuel -= fuel_cost
    reward = world.event_rng.randint(60, 180)
    world.save.pilot.credits += reward
    adjust_reputation(world, FACTION_CONCORD, 3)
    world.save.pilot.note(f"Answered a distress call (+{reward}cr, Concord standing up).")
    out_line(f"{p.correct}You divert to help -- {fuel_cost} fuel spent. Grateful survivors "
              f"pay {reward}cr, and Concord takes note.{RESET}")


def _encounter_market_tip(p: Palette, world: World, dest: GalaxySystem) -> None:
    """Pure flavor/utility -- reveals a real, already-computed price
    (`price_for`) at a nearby, already-discovered system. No new state,
    no choice to make, and no `random.Random` calls that could affect
    anything but which system/commodity the tip names."""
    hops = bfs_hops(world.by_id, dest.id)
    candidates = [sid for sid, h in hops.items() if 1 <= h <= 4 and world.by_id[sid].discovered]
    if not candidates:
        out_line(f"{p.muted}You intercept a garbled data burst -- nothing usable in it.{RESET}")
        return
    sid = world.event_rng.choice(candidates)
    system = world.by_id[sid]
    commodity = world.event_rng.choice(list(COMMODITIES))
    price = price_for(world, sid, commodity)
    label = COMMODITIES[commodity]["label"]
    out_line(f"{p.muted}You intercept a trader's data burst: "
              f"{label} is going for {price}cr at {system.name}.{RESET}")


def _resolve_escort_missions(p: Palette, world: World, dest_id: int) -> None:
    """Runs once per hop, after `screen_travel`'s own bounty/patrol/
    random-encounter chain above -- an escort contract means a scripted
    pirate wave finds the convoy on *every* leg of the trip, unlike an
    ordinary encounter's own probabilistic roll, since the whole point
    of the mission is guaranteed danger across several jumps. Evading
    (or being bribed away from) the fight fails the contract exactly
    like losing it does -- the convoy is left to the raiders either
    way -- only a genuine win lets it continue, and pays out the moment
    it reaches its destination. Iterates a snapshot of the list since
    a completed or failed mission removes itself from `active_missions`
    mid-loop."""
    dest = world.by_id[dest_id]
    for mission in [m for m in world.save.active_missions if m.kind == "escort"]:
        pirate = generate_pirate(world, tier=mission.pirate_tier)
        out_line(f"{p.wrong}Raiders ambush the convoy you're escorting -- the {pirate.name} closes in.{RESET}")
        outcome = screen_combat(p, world, pirate)
        if outcome == "won":
            if dest_id == mission.target_system:
                world.save.active_missions.remove(mission)
                world.save.pilot.credits += mission.reward
                if world.save.pilot.missions_completed == 0:
                    world.save.pilot.highlight(f"First mission complete: {mission.description}.")
                world.save.pilot.missions_completed += 1
                world.save.pilot.note(f"Escort complete: {mission.description} (+{mission.reward}cr)")
                world.save.pilot.highlight(f"Escorted a convoy safely to {dest.name}.")
                out_line(f"{p.gold}Convoy delivered safely! +{mission.reward}cr{RESET}")
            else:
                out_line(f"{p.correct}The convoy presses on.{RESET}")
        else:
            world.save.active_missions.remove(mission)
            world.save.pilot.note(f"Escort failed: {mission.description}")
            if outcome == "escaped":
                out_line(f"{p.wrong}You disengage -- the convoy is left defenseless. Escort contract failed.{RESET}")
            else:
                out_line(f"{p.wrong}Escort contract failed -- the convoy was lost.{RESET}")


def screen_travel(p: Palette, world: World, dest_id: int) -> None:
    origin = world.here
    dest = world.by_id[dest_id]
    cost = fuel_cost_for_jump(origin, dest, world.save.ship)
    world.save.ship.fuel -= cost
    world.save.turn += 1
    tick_price_reversion(world)
    for msg in pay_crew_wages(world):
        out_line(f"{p.wrong}{msg}{RESET}")
    out_line(f"{p.muted}Jumping to {'the unknown' if not dest.discovered else dest.name}...{RESET}")

    bounty = next((m for m in world.save.active_missions
                    if m.kind == "bounty" and m.target_system == dest_id), None)
    if bounty is not None:
        pirate = generate_pirate(world, tier=bounty.pirate_tier)
        out_line(f"{p.wrong}Your bounty target, the {pirate.name}, is waiting.{RESET}")
        outcome = screen_combat(p, world, pirate)
        if outcome == "won":
            world.save.active_missions.remove(bounty)
            world.save.pilot.credits += bounty.reward
            if world.save.pilot.missions_completed == 0:
                world.save.pilot.highlight(f"First mission complete: {bounty.description}.")
            world.save.pilot.missions_completed += 1
            world.save.pilot.note(f"Bounty complete: {bounty.description} (+{bounty.reward}cr)")
            out_line(f"{p.gold}Bounty complete! +{bounty.reward}cr{RESET}")
            # A flat, tier-independent chance the kill turns out to have
            # been mistaken identity -- discovered only after the fact,
            # since a bounty target always *looks* like a legitimate
            # raider going in (there is no way for the player to have
            # known beforehand, matching the "not a difficulty setting"
            # philosophy the rest of this file's own consequence design
            # already follows).
            if world.event_rng.random() < WRONG_BOUNTY_KILL_CHANCE:
                world.save.pilot.notoriety += NOTORIETY_PER_WRONG_BOUNTY_KILL
                adjust_reputation(world, FACTION_CONCORD, -3)
                world.save.pilot.note("Concord inquiry: that bounty kill was mistaken identity -- notoriety rises.")
                out_line(f"{p.wrong}Later, a Concord inquiry flags an irregularity: that 'raider' matches an "
                          f"informant's registered ship. Notoriety rises.{RESET}")
        elif outcome == "destroyed":
            # Losing must also clear the bounty -- otherwise it stays
            # active forever and re-triggers this same guaranteed fight
            # on every future visit to this system (see screen_combat's
            # own docstring for the real playtest that found this).
            world.save.active_missions.remove(bounty)
            world.save.pilot.note(f"Bounty failed: {bounty.description}")
            out_line(f"{p.wrong}Bounty failed -- the {pirate.name} was too much this time.{RESET}")
        # outcome == "escaped": left active on purpose -- a deliberate
        # retreat to come back stronger later isn't a failure.
    elif world.event_rng.random() < notoriety_patrol_chance(world.save.pilot.notoriety):
        # Same unconditional-priority slot as a bounty target -- checked
        # before the ordinary random-encounter roll, not folded into
        # TRAVEL_ENCOUNTER_WEIGHTS's own slice, since a wanted pilot
        # should face a meaningfully higher (and continuously scaling)
        # interception chance than one more flavor-encounter option
        # would give it. `notoriety_patrol_chance` is 0.0 at notoriety 0,
        # so an unwanted pilot never reaches this branch at all.
        screen_notoriety_patrol(p, world)
    else:
        _resolve_random_travel_encounter(p, world, dest)

    _resolve_escort_missions(p, world, dest_id)

    was_discovered = dest.discovered
    dest.discovered = True
    world.save.current_system = dest_id
    world.sync_discovered()
    if not was_discovered:
        out_line(f"{p.gold}New system charted: {dest.name}.{RESET}")
    for msg in check_mission_completions(world, just_discovered=None if was_discovered else dest_id):
        out_line(f"{p.gold}{msg}{RESET}")

    if world.save.cargo and any(not COMMODITIES[c]["legal"] for c in world.save.cargo) and dest.economy != "Haven":
        if world.event_rng.random() < customs_check_chance(dest):
            screen_customs(p, world)
    out_line()


def screen_combat(p: Palette, world: World, pirate: Pirate) -> str:
    """Returns "won" (pirate destroyed), "escaped" (evaded or bribed
    away, ship intact), or "destroyed" (the player's own ship was lost)
    -- a bounty's own caller needs to tell all three apart: "won"
    completes it, "escaped" leaves it active to retry later (a real
    tactical retreat-and-come-back-stronger choice), and "destroyed"
    must also clear it, or a bounty the player has already lost respawns
    as a mandatory, unwinnable-at-current-gear ambush on every future
    visit to that system forever (dogfood-caught: a real playtest got
    stuck fighting the same tier-2 bounty target to the death four times
    in a row, since nothing ever removed it)."""
    ship = world.save.ship
    while True:
        out_line(f"{p.wrong}{pirate.name}{RESET} (tier {pirate.tier})  HP {pirate.hp}/{pirate.hp_max}   "
                  f"{p.accent}Your hull{RESET} {ship.hull_hp}/{hull_hp_max(ship)}")
        can_bribe = world.save.pilot.credits >= bribe_cost(pirate)
        out(f"{p.muted}[F]ight [E]vade [D]ump&evade" + (" [B]ribe" if can_bribe else "") + " [Q]uick status: "
            f"{RESET}")
        action = read_key().upper()
        out_line(action)
        if action == "F":
            _, _, lines = fight_round(world, pirate)
            for line in lines:
                out_line(f"  {line}")
            if pirate.hp <= 0:
                loot = 40 + pirate.tier * 60
                world.save.pilot.credits += loot
                if world.save.pilot.kills == 0:
                    world.save.pilot.highlight(f"First kill: destroyed the {pirate.name}.")
                world.save.pilot.kills += 1
                adjust_reputation(world, FACTION_CONCORD, 2)
                adjust_reputation(world, FACTION_BLACKWAKE, -1)
                out_line(f"{p.correct}Salvage recovered: {loot}cr.{RESET}")
                return "won"
            if ship.hull_hp <= 0:
                out_line(f"{p.wrong}{destroy_ship(world)}{RESET}")
                return "destroyed"
        elif action in ("E", "D"):
            dumped = False
            if action == "D" and world.save.cargo:
                commodity = world.event_rng.choice(list(world.save.cargo.keys()))
                world.save.cargo[commodity] -= 1
                if world.save.cargo[commodity] <= 0:
                    del world.save.cargo[commodity]
                dumped = True
                out_line(f"{p.muted}You dump cargo to lighten the ship.{RESET}")
            if world.event_rng.random() < evade_chance(world, pirate, dumped_cargo=dumped):
                out_line(f"{p.correct}You break contact and escape.{RESET}")
                return "escaped"
            out_line(f"{p.wrong}Evasion failed -- they're still on you.{RESET}")
            raw = world.event_rng.randint(4, 9) + pirate.tier * 4
            dmg = max(1, raw - ship.shield_tier * 3)
            ship.hull_hp = max(0, ship.hull_hp - dmg)
            out_line(f"  The {pirate.name} hits you for {dmg} damage.")
            if ship.hull_hp <= 0:
                out_line(f"{p.wrong}{destroy_ship(world)}{RESET}")
                return "destroyed"
        elif action == "B" and can_bribe:
            cost = bribe_cost(pirate)
            if world.event_rng.random() < bribe_chance(world, pirate):
                world.save.pilot.credits -= cost
                adjust_reputation(world, FACTION_BLACKWAKE, 2)
                out_line(f"{p.correct}The {pirate.name} takes {cost}cr and peels off.{RESET}")
                return "escaped"
            out_line(f"{p.wrong}They refuse the bribe and press the attack!{RESET}")


def screen_notoriety_patrol(p: Palette, world: World) -> None:
    """A Concord Patrol intercepting a wanted pilot -- structurally
    similar to `screen_combat`'s own fight/evade loop (built on the same
    `fight_round`/`evade_chance` pure functions), but a deliberately
    separate function rather than a reuse of `screen_combat` itself:
    this is law enforcement, not pirates, and the consequences genuinely
    differ. `screen_combat`'s own "won" branch pays salvage loot and
    *raises* Concord standing -- exactly backward here, where winning
    means a wanted pilot just killed a Concord officer. `[S]urrender`
    replaces `[B]ribe` as the peaceful resolution, and is the *only* way
    notoriety ever goes back down outside of `destroy_ship`'s own
    unconditional wipe -- there is no passive decay."""
    ship = world.save.ship
    patrol = generate_concord_patrol(world)
    fine = notoriety_fine_cost(world.save.pilot.notoriety)
    out_line(f"{p.wrong}A Concord patrol vessel, the {patrol.name}, intercepts you -- "
              f"your transponder flags as wanted.{RESET}")
    while True:
        out_line(f"{p.wrong}{patrol.name}{RESET} (tier {patrol.tier})  HP {patrol.hp}/{patrol.hp_max}   "
                  f"{p.accent}Your hull{RESET} {ship.hull_hp}/{hull_hp_max(ship)}")
        can_surrender = world.save.pilot.credits >= fine
        out(f"{p.muted}[F]ight [E]vade" + (f" [S]urrender & pay {fine}cr" if can_surrender else "") +
            f" [Q]uick status: {RESET}")
        action = read_key().upper()
        out_line(action)
        if action == "F":
            _, _, lines = fight_round(world, patrol)
            for line in lines:
                out_line(f"  {line}")
            if patrol.hp <= 0:
                if world.save.pilot.kills == 0:
                    world.save.pilot.highlight(f"First kill: destroyed the Concord patrol vessel {patrol.name}.")
                world.save.pilot.kills += 1
                world.save.pilot.notoriety += 3
                adjust_reputation(world, FACTION_CONCORD, -10)
                adjust_reputation(world, FACTION_BLACKWAKE, 3)
                world.save.pilot.note("Destroyed a Concord patrol vessel -- notoriety rises further.")
                out_line(f"{p.wrong}The {patrol.name} is destroyed -- Concord will not forget this.{RESET}")
                return
            if ship.hull_hp <= 0:
                out_line(f"{p.wrong}{destroy_ship(world)}{RESET}")
                return
        elif action == "E":
            if world.event_rng.random() < evade_chance(world, patrol, dumped_cargo=False):
                out_line(f"{p.correct}You break contact and escape.{RESET}")
                return
            out_line(f"{p.wrong}Evasion failed -- they're still on you.{RESET}")
            raw = world.event_rng.randint(4, 9) + patrol.tier * 4
            dmg = max(1, raw - ship.shield_tier * 3)
            ship.hull_hp = max(0, ship.hull_hp - dmg)
            out_line(f"  The {patrol.name} hits you for {dmg} damage.")
            if ship.hull_hp <= 0:
                out_line(f"{p.wrong}{destroy_ship(world)}{RESET}")
                return
        elif action == "S" and can_surrender:
            world.save.pilot.credits -= fine
            world.save.pilot.notoriety = 0
            adjust_reputation(world, FACTION_CONCORD, 2)
            world.save.pilot.note(f"Paid a {fine}cr fine to Concord -- notoriety cleared.")
            out_line(f"{p.correct}You power down and pay the {fine}cr fine. Notoriety cleared.{RESET}")
            return


def screen_customs(p: Palette, world: World) -> None:
    contraband_qty = sum(q for c, q in world.save.cargo.items() if not COMMODITIES[c]["legal"])
    out_line(f"{p.wrong}Concord customs hails you for a cargo inspection.{RESET}")
    value = sum(q * COMMODITIES[c]["base"] for c, q in world.save.cargo.items() if not COMMODITIES[c]["legal"])
    out(f"{p.muted}[S]urrender contraband [B]ribe the inspector: {RESET}")
    action = read_key().upper()
    out_line(action)
    if action == "B":
        cost = 100 + value // 2
        if world.save.pilot.credits >= cost and world.event_rng.random() < 0.6:
            world.save.pilot.credits -= cost
            out_line(f"{p.correct}{cost}cr changes hands quietly. Move along.{RESET}")
            return
        fine = 150 + value
        world.save.pilot.credits = max(0, world.save.pilot.credits - fine)
        for c in CONTRABAND_COMMODITIES:
            world.save.cargo.pop(c, None)
        adjust_reputation(world, FACTION_CONCORD, -5)
        # Notoriety only rises here, not on the cooperative "surrender
        # outright" path below -- a caught, refused bribe is a repeat-
        # offender bust; volunteering the contraband before a fight ever
        # starts is already the game's own "played fair, small rep
        # bonus" outcome (see the +1 just below), not a wanted-status
        # event on top of that.
        world.save.pilot.notoriety += NOTORIETY_PER_CUSTOMS_BUST
        out_line(f"{p.wrong}Bribe refused -- contraband confiscated and a {fine}cr fine levied.{RESET}")
        return
    for c in CONTRABAND_COMMODITIES:
        world.save.cargo.pop(c, None)
    adjust_reputation(world, FACTION_CONCORD, 1)
    out_line(f"{p.muted}You surrender {contraband_qty} units without a fight.{RESET}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    info = _load_door_info()
    p = Palette(truecolor=info.get("color_depth") == "truecolor")
    save_dir = _default_save_dir()
    # Real NetBBS launches always carry a real positive user_id from the
    # drop-file; this fallback only fires for standalone tinkering
    # (`python3 voidrunner.py` with no NETBBS_DOOR_INFO) and must be
    # deterministic per handle -- Python's built-in hash() is randomized
    # per process (PYTHONHASHSEED), which would silently start a fresh
    # career file on every standalone run.
    user_id = int(info.get("user_id", 0)) or zlib.crc32(info["handle"].encode())

    try:
        screen_title(p, info)
        save, is_new, notice = load_or_create_save(save_dir, user_id, info["handle"])
        if notice:
            out_line(f"{p.wrong}{notice}{RESET}")
        if is_new:
            save.pilot.handle = create_career(p, info)
        else:
            out_line(f"{p.muted}Welcome back, {save.pilot.handle}. Day {save.turn}.{RESET}")
        world = World(save)
        persist(world, save_dir, user_id)

        while True:
            choice = screen_station_menu(p, world)
            if choice == "M":
                screen_market(p, world)
            elif choice == "Y":
                screen_shipyard(p, world)
            elif choice == "B":
                screen_missions(p, world)
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
            elif choice == "Q":
                out_line(f"{p.muted}Docking clamps engaged. Fly safe, {world.save.pilot.handle}.{RESET}")
                persist(world, save_dir, user_id)
                return 0
            else:
                continue
            persist(world, save_dir, user_id)
    except EOFError:
        try:
            persist(world, save_dir, user_id)  # type: ignore[possibly-undefined]
        except Exception:
            pass
        return 0
    finally:
        out(RESET)


if __name__ == "__main__":
    sys.exit(main())
