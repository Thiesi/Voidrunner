"""Render Voidrunner's screens at every supported size and preset, as a page.

    python tools/door_gallery.py --out build/gallery --open
    python tools/door_gallery.py --widths 80 64 40 --fresh

Ported from NetBBS's `scripts/door_gallery.py`, which renders its other bundled
door, War Dialer, too; a few comments below still use War Dialer's screens as
the example that motivated a rule.

Why this exists: a door's presentation cannot be reviewed by reading a diff, and
the test suite can only ever assert that a screen *fits* -- never that it looks
like anything. Both bundled games lost their entire visual design across an
overhaul in which every individual slice passed review and every test stayed
green (issues #493, #494). The countermeasure is that a change to a screen comes
with pictures: this drives the real door in a subprocess, keeps the bytes it
writes, and paints them with a small ANSI emulator (`tools/ansi_to_html.py`,
from the netbbs.org screenshots), on a canvas the size of the terminal being simulated -- so a panel is the
final screen a caller sees, scrolling included, not a transcript of everything
that was ever printed.

**A door is typed at, not piped into.** Both doors treat bytes that arrive
together as an unframed paste and discard them (War Dialer's burst window is
20ms), and a screen is only on the terminal until the next keystroke redraws
over it. So `Door` writes one key at a time, waits for the output to go quiet
after each, and photographs the screen *before* anything is sent to leave it --
a walk that piped its keys in at once photographed the switchboard nine times.

**Panels must be comparable.** A door generates its world from a random seed, so
a gallery built from fresh state would show different systems, names and missions
in every panel and again on every run, and a before/after review would be
meaningless. Each door therefore gets one *fixture* -- a single career or world
created once, cached under the output directory and copied for every capture,
with the display preset applied to the copy -- so only the size and the preset
vary. `--fresh` rebuilds them; delete the directory and the next run makes new
ones.

A scripted walk is deliberately shallow: it opens a screen and comes back. Add a
walk to `WALKS` when a door grows a screen the current keys do not reach.
"""
from __future__ import annotations

import argparse
import html
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor

SCRIPTS = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import ansi_to_html as term  # noqa: E402

SPAN = re.compile(r'<span style="([^"]*)">(.*?)</span>')
ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
CLEAR = "\x1b[2J\x1b[H"
# A row's frame edge, in either spelling. The plain preset draws the box in ASCII
# (`|` where Unicode has `┃`), and stripping only the Unicode one left every entry
# row beginning with a character before its `[K]` marker -- so under that preset
# no walk that names an entry by what it is could find one.
# Both weights of vertical: War Dialer moved from heavy to light in issue
# #517, Voidrunner has always drawn light, and the ASCII presets use "|".
FRAME_EDGE = re.compile(r"^[\s┃││|]*")
#: A screen's own statement of which page this is, which it prints for this.
PAGE_NOTE = re.compile(r"page (\d+)/(\d+)")

# A keystroke is isolated by the silence around it: comfortably more than War
# Dialer's 20ms burst window, and enough quiet afterwards for the door to have
# finished drawing what the key asked for.
QUIET = 0.15
SETTLE = 0.75
ANSWER = 6.0  # a keypress can take a second to redraw; past this it did nothing.
PATIENCE = 30.0  # a door still drawing after this is hung, not slow.
WORKERS = 4  # panels are independent subprocesses; a gallery is 150+ of them.

# Each walk is (label, keys), pressed in order against a door that already has a
# career or world: registration and the first-visit guide belong to the fixture,
# not to every panel. The key is the one the door's own dispatch uses -- check it
# there, not in the action bar, before adding a walk.
#
# The suffixes exist because how much fits a page depends on the terminal, so a
# walk can name a place or an entry but never a page number:
#
#   `*`  press this key until the screen stops changing, then photograph that.
#        War Dialer's switchboard is two pages at 80x24 and eleven at 40x12, so a
#        fixed number of Next presses photographed page two twice at one size and
#        never reached the feed, the orders or the season card at another.
#   `?`  page forward until the screen offers this key, then press it. The scene
#        hub shows all seven entries at 80x24 and four at 40x12, so `I5` reached
#        "Your season reports" at one size and a key the picker was ignoring at
#        another.
#   `#`  press whichever key the screen offers first, for a walk that wants *an*
#        entry rather than a named one. The root picker marks the caller's own
#        holdings `[-]`, so which digit it accepts depends on the fixture's own
#        history -- naming one there broke the build twice as the fixture grew.
#   `%`  photograph this screen and every page after it, turning them with this
#        key. Must end the walk. `*` fast-forwards, and a card stack's pages are
#        *different cards*: at forty columns the switchboard is fourteen pages,
#        and keeping only the first, the second and the last published three of
#        them -- the scene, the feed, the orders and most of the season card were
#        in no panel at any size. Use it where paging changes what kind of
#        information is on the screen (a card stack, the rules, a preview's
#        stakes and then its terms), not where it merely shows the next rows of
#        the same table: page two of the scene table is the same drawing with
#        different data in it, and reviewing it again costs a panel and teaches
#        nothing.
#  `{X}` press the key of the entry whose own rows say X, paging to find it. The
#        one selector that survives a changed world: which digit a Public PBX is
#        depends on what the caller happens to hold, and the three owner services
#        are three different screens -- a PBX removes Heat, a Warez Hub adds it
#        and rolls for a bust, a Carrier Switch does neither -- so `G1?$` reviewed
#        one of the three and nothing said so.
#   `$`  page to the last page and press the last key offered there, for an entry
#        a picker appends after a variable list. The exchange control screen puts
#        the owner service after however many crew transfers the holding happens
#        to offer, so `3` meant the service only for as long as the fixture kept
#        exactly two of them -- and a digit that lands on a transfer instead
#        publishes a garrison preview under the service's caption.
#
#   `~`  the launch splash, as its last frame leaves it. Every other walk runs
#        with `DOOR_SPLASH=0`, so a panel is the screen behind the splash
#        and a launch costs no animation; this one turns the splash back on and
#        keeps what was drawn between the splash's own clear and its handover.
#        A preset without motion has no splash, so `SPLASH_PRESETS` names the
#        presets this walk is taken in.
#
# A walk photographs one screen: the one it is looking at when its keys run out.
# Passing *through* a picker on the way somewhere else therefore reviews nothing
# of it, so a screen on the way to another screen needs a walk of its own that
# stops there. `SHOWS` names what each walk's screen must say, and the build
# refuses to publish a panel that does not say it.
WALKS: dict[str, list[tuple[str, bytes]]] = {
    "voidrunner": [
        # (label, keys) or (label, keys, fixture). The default fixture is "base".
        # The animated launch splash, in the presets that animate.
        ("Launch splash", b"~"),
        # A career saved mid-fight opens on the notice that says so, and waits
        # for a key before it draws the fight (issue #641).
        ("Journey Resumed", b"", "combat"),
        ("Combat", b" ", "combat"),
        # The same fight against a raider the ship cannot beat (issue #647): the
        # OUTCLASSED row, and a Dump priced at half the hold.
        ("Combat, outclassed", b" ", "outclassed"),
        # The first deck of a session carries a returning pilot's welcome and
        # recap above its gauges (issue #641), which at forty columns is most of
        # page one. It is reviewed as itself; the deck a pilot sees for the rest
        # of the session is reached by leaving a screen and coming back.
        ("Command Deck, first of a session", b""),
        ("Command Deck", b"OB"),
        ("Command Deck, expanded", b"OBX"),
        ("Commodity Market", b"M"),
        ("Engineering Yard", b"Y"),
        # The crew roster is behind the yard, not the deck: `K` from `Y`.
        ("Crew Roster", b"YK"),
        ("Mission Board", b"B"),
        ("Contract Details", b"B1"),
        ("Navigation Chart", b"C"),
        ("Pilot Record", b"S", "played"),
        # The record's other three views, and the finale, are each one more key.
        ("Pilot Record, jobs", b"SC", "played"),
        ("Pilot Record, log", b"SH", "played"),
        ("Career Dossiers", b"SD", "played"),
        ("Career Finale", b"SR", "played"),
        # The ending a retirement is shown (issue #644): Status, Finale, Retire,
        # yes. It needs a career that *may* retire, which is its own fixture.
        ("Career Complete", b"SRSY", "retiring"),
        ("Hall of Fame", b"H", "played"),
        # The Hall's five views are five different tables, not one paged table.
        ("Hall of Fame, trading", b"H2", "played"),
        ("Hall of Fame, exploration", b"H3", "played"),
        ("Hall of Fame, combat", b"H4", "played"),
        ("Hall of Fame, careers", b"H5", "played"),
        ("Pilot Guide", b"G"),
        ("Trading Ledger", b"T"),
        ("Viewport", b"V"),
        ("Display Options", b"O"),
        ("Archive Contacts", b"N"),
        ("Concord Contacts", b"P"),
        ("Blackwake Contacts", b"W"),
        # The chart opens as a list; its own [V] is the spatial map, which is a
        # different drawing and the one the 40x12 floor was argued over.
        ("Navigation Chart, map", b"CV"),
        # The list picker is a screen of its own and had no panel: it is the
        # one drawing in the game that is neither framed nor full-bleed, and it
        # used to be printed under whatever opened it (issue #643). Two ways in,
        # because the played career is the one with more than a row to pick.
        ("Route planner, destination picker", b"CG", "played"),
        ("Star map, station picker", b"CVI", "played"),
    ],
}

# Keys that take a brand-new career through everything a first launch asks, so a
# panel never opens on registration or the first-visit guide.
ONBOARDING: dict[str, bytes] = {"voidrunner": b"\rY"}


def load_door(door: pathlib.Path):
    """The door as a module, for seeding a world through its own functions."""
    spec = importlib.util.spec_from_file_location(f"gallery_{door.stem}", door)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered before it runs: `@dataclass` resolves a field's type through
    # `sys.modules[cls.__module__]`, which is not there yet otherwise.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# Per-walk hooks, keyed by door and walk label. Voidrunner needs none of them
# today; NetBBS's War Dialer walks show what they are for:
#   PREPARED  a function run against the panel's own copy of the fixture;
#   SETUP     keys played in a throwaway launch before the photographed one.
PREPARED: dict[str, dict[str, object]] = {}
SETUP: dict[str, dict[str, bytes]] = {}


# What has to be legible on the screen a walk stops at, checked against the
# painted panel rather than the door's bytes. A caption is a claim about a
# picture, and a walk drives a picker whose entries move as the fixture grows:
# without this the gallery could publish the exchange control screen's transfer
# preview under "Owner service preview" and read as a complete review. Every walk
# of a door listed here must name its screen, so a new walk cannot be added
# unchecked.
SHOWS: dict[str, dict[str, str]] = {}

# The presets in which a door plays its launch splash: the rest turn motion off,
# and with it the splash, so a `~` walk there would photograph nothing.
SPLASH_PRESETS: dict[str, tuple[str, ...]] = {
    "voidrunner": ("auto", "basic"),
}

# Every preset a caller can choose, applied to the fixture's copy rather than
# passed as a flag: Voidrunner keeps its display style in the career (every one
# of `DISPLAY_STYLES`).
PRESETS: dict[str, dict[str, dict]] = {
    "voidrunner": {style: {"display_style": style} for style in
                   ("auto", "fast", "basic", "mono", "plain")},
}


DROP_USER_ID = 1  # the caller every panel is drawn for; display rows are keyed by it


def remove(path: pathlib.Path) -> None:
    """Delete a capture's directory, once the door has let go of it.

    Windows keeps a deleted-but-open file alive, and the door's SQLite handle
    outlives its process by a moment, so a single `rmtree(ignore_errors=True)`
    quietly left the world file -- and its directory -- behind on every panel.
    """
    for _ in range(30):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.1)
    print(f"could not remove {path}", file=sys.stderr)


def drop_file(work: pathlib.Path, width: int, height: int, info_extra: dict) -> pathlib.Path:
    info = {
        "handle": "Thiesi", "user_id": DROP_USER_ID, "terminal_width": width, "terminal_height": height,
        "color_depth": "truecolor", "node_name": "ReLink",
    }
    info.update(info_extra)
    path = work / "info.json"
    path.write_text(json.dumps(info), encoding="utf-8")
    return path


class Door:
    """A door in a subprocess, typed at one key at a time.

    `read()` returns everything written so far, so a caller can photograph a
    screen while the door is still sitting on it.
    """

    def __init__(self, door: pathlib.Path, state: pathlib.Path, width: int, height: int,
                 info_extra: dict, *, splash: bool = False) -> None:
        self.door = door
        self.size = f"{width}x{height}"
        env = dict(os.environ)
        env.update(NETBBS_DOOR_INFO=str(drop_file(state, width, height, info_extra)),
                   VOIDRUNNER_SAVE_DIR=str(state / "saves"),
                   DOOR_SPLASH="1" if splash else "0",
                   PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, str(door)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env)
        self.lock = threading.Lock()
        self.out, self.err = bytearray(), bytearray()
        self.spoke = time.monotonic()
        self.readers = [self._reader(self.proc.stdout, self.out, True),
                        self._reader(self.proc.stderr, self.err, False)]

    def _reader(self, stream, sink: bytearray, is_stdout: bool) -> threading.Thread:
        def pump() -> None:
            while True:
                chunk = stream.read(1)
                if not chunk:
                    return
                with self.lock:
                    sink.extend(chunk)
                    if is_stdout:
                        self.spoke = time.monotonic()
        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        return thread

    def settle(self, since: int = 0, what: str = "startup", *, expect: bool = True) -> None:
        """Wait for the door to answer and then stop drawing.

        Silence alone does not mean the screen is ready: a door that has been
        sitting at a prompt has been silent for as long as the caller took to
        press a key, so a settle that only measured quiet returned before the
        keypress had drawn anything and photographed the previous screen.
        `since` is the output length before the key went in; the wait is over
        only once the door has written past it and then gone quiet.

        A screen is owed at startup and after every walk key, because a walk's
        keys come from the door's own dispatch: silence means the key stopped
        being accepted, and publishing the screen before it under the next
        screen's name is the exact failure this script exists to catch. Only
        `expect=False` tolerates silence, for the one case that earns it --
        first-launch keys, where a door that asks one question fewer than the
        next door leaves a key with nothing to answer.
        """
        answer = time.monotonic() + ANSWER
        deadline = time.monotonic() + PATIENCE
        while time.monotonic() < deadline:
            with self.lock:
                quiet = time.monotonic() - self.spoke
                answered = len(self.out) > since
            if answered and quiet >= SETTLE:
                return
            if not answered and time.monotonic() >= answer:
                if not expect:
                    return
                break  # nothing is coming; say so now rather than at the deadline
            time.sleep(0.05)
        # Either the door printed nothing where a screen was owed, or it never
        # stopped printing. Neither is what a caller would be looking at.
        raise SystemExit(
            f"{self.door.name} {'never stopped drawing' if answered else 'printed nothing'} "
            f"after {what} at {self.size}:\n"
            f"{bytes(self.err).decode('utf-8', 'replace')[-800:]}")

    def press(self, key: bytes, *, expect: bool = True) -> None:
        """Press one key and wait for the screen it is supposed to open.

        A walk's keys are chosen from the door's dispatch, so a key that draws
        nothing has stopped being accepted -- and the panel would then publish
        the screen before it under the next screen's name. That is the failure
        this whole script exists to catch, so it is an error, not a shrug.
        """
        time.sleep(QUIET)  # every key arrives alone; a burst is discarded as paste
        with self.lock:
            before = len(self.out)
        if self.proc.poll() is not None:
            # Writing to a closed pipe raises `OSError: [Errno 22]` on Windows,
            # which says nothing about the walk that ran out of door.
            raise SystemExit(f"{self.door.name} had already exited (code "
                             f"{self.proc.poll()}) when {key!r} was pressed at "
                             f"{self.size}: a key before it left the game")
        self.proc.stdin.write(key)
        self.proc.stdin.flush()
        self.settle(before, f"key {key!r}", expect=expect)

    #: How a screen says which keys it will accept right now. Matched on the hint
    #: row rather than on the frame, because Fast mode has no frame -- and an
    #: entry's own `[2]` marker can be on the screen while the entry's last row,
    #: and therefore its key, is on the next page.
    OFFERS = ("pick ", "no choice on this page", "inspect ")

    def offered(self) -> str:
        """The screen's own list of the keys it will take, or "" if it offers none."""
        for row in ANSI.sub("", last_screen(self.read())).split("\r\n"):
            row = row.strip()
            if row.startswith(self.OFFERS):
                return row
        return ""

    def press_first_offered(self, *, limit: int = 24) -> None:
        """Press the first key the screen offers, paging forward to find one.

        The root picker's first page can legitimately offer nothing at forty
        columns: the caller's own holdings are marked `[-]`, and one unselectable
        entry is most of a twelve-row page, so the choices are on page two.
        """
        for _ in range(limit):
            keys = re.findall(r"\[(\w)\]", self.offered())
            if keys:
                self.press(keys[0].encode())
                return
            before = last_screen(self.read())
            self.press(b"N")
            if last_screen(self.read()) == before:
                break
        raise SystemExit(f"{self.door.name} offered no key at {self.size}:\n"
                         f"{self.offered()!r}")

    def press_matching(self, text: str, *, limit: int = 24) -> None:
        """Press the key of the entry whose own rows contain `text`.

        A picker draws `[K]` against the first row of an entry and indents the
        rest, so the entry owning a row is the nearest key above it. Which key
        that is and whether it can be pressed yet are two different pages: at
        forty columns the garrison list shows `[2] Bay  WAREZ HUB` at the foot of
        page one while offering only `[1]`, and page two offers `[2]` with the
        name nowhere on it. So remember the key the text belongs to, and keep
        paging until the screen will take it.
        """
        wanted = None
        for _ in range(limit):
            screen = ANSI.sub("", last_screen(self.read()))
            key = None
            for row in screen.split("\r\n"):
                stripped = FRAME_EDGE.sub("", row)
                found = re.match(r"\[(\w)\]", stripped)
                if found:
                    key = found.group(1)
                if wanted is None and key and text in stripped:
                    wanted = key
            if wanted and f"[{wanted}]" in self.offered():
                self.press(wanted.encode())
                return
            before = last_screen(self.read())
            self.press(b"N")
            if last_screen(self.read()) == before:
                break
        raise SystemExit(f"{self.door.name} offered no entry saying {text!r} at "
                         f"{self.size}"
                         + (f" (found it as [{wanted}], never offered)" if wanted else ""))

    def press_pages(self, key: bytes, *, limit: int = 32) -> list[str]:
        """This screen and every page after it, turned with `key`.

        War Dialer prints `page i/n` in the border for exactly this -- its own
        `page_note` calls the counter "the handle a scripted walk uses to know
        whether there is another page to turn" -- so stop on page n rather than
        on "the screen stopped changing". For a screen that is merely paged those
        are the same end; for one that *leaves* when its last page is
        acknowledged they are not, and the first-visit guide takes any key and
        then hands the caller the switchboard -- which "until it stops changing"
        photographed twice under the guide's name. A screen that prints no
        counter has exactly one page: `page_note` writes one whenever there is
        more than one, and Fast mode, which has no border to write it into, puts
        it in the title row for the same reason.
        """
        pages = [last_screen(self.read())]
        for _ in range(limit):
            note = PAGE_NOTE.search(ANSI.sub("", pages[-1]))
            if not note or note.group(1) == note.group(2):
                break
            self.press(key)
            page = last_screen(self.read())
            if page == pages[-1]:
                break
            pages.append(page)
        return pages

    def press_last_offered(self, *, limit: int = 24) -> None:
        """Page to the last page and press the last key offered there.

        The only stable way to name an entry a picker appends after a list whose
        length is the fixture's business: the exchange control screen's owner
        service sits after one crew transfer per offer the holding can make.
        """
        self.press_to_end(b"N", limit=limit)
        keys = re.findall(r"\[(\w)\]", self.offered())
        if not keys:
            raise SystemExit(f"{self.door.name} offered no key on its last page "
                             f"at {self.size}:\n{self.offered()!r}")
        self.press(keys[-1].encode())

    def press_when_offered(self, key: bytes, *, limit: int = 24) -> None:
        """Page forward until the screen offers `key`, then press it.

        How many entries fit a page depends on the terminal, so a walk can name
        the entry it wants but never the page the entry is on: the scene hub
        offers all seven at 80x24 and four at 40x12.
        """
        wanted = f"[{key.decode()}]"
        for _ in range(limit):
            if wanted in self.offered():
                self.press(key)
                return
            before = last_screen(self.read())
            self.press(b"N")
            if last_screen(self.read()) == before:
                break
        raise SystemExit(f"{self.door.name} never offered {wanted} at {self.size}:\n"
                         f"{self.offered()!r}")

    def press_to_end(self, key: bytes, *, limit: int = 24) -> None:
        """Press `key` until the screen it redraws stops changing.

        A paging key on the last page still redraws -- the door clears and draws
        the same page again -- so "stopped changing" is the only signal for "this
        is the end", and it is the same end at every terminal size.
        """
        for _ in range(limit):
            before = last_screen(self.read())
            self.press(key)
            if last_screen(self.read()) == before:
                return

    def read(self) -> str:
        with self.lock:
            return bytes(self.out).decode("utf-8", "replace")

    def kill(self) -> None:
        """Leave nothing running behind a failed capture."""
        self.proc.kill()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        for reader in self.readers:
            reader.join(timeout=5)

    def finish(self) -> None:
        """Close stdin, let the door exit, and refuse to publish a crash.

        A gallery that certifies a broken walk is worse than no gallery: a door
        that exits nonzero has crashed, whatever it managed to print first.
        """
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            code = self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.kill()  # reap it, rather than leaving one child per panel behind
            raise SystemExit(f"{self.door.name} never exited after its keys were pressed")
        for reader in self.readers:
            reader.join(timeout=5)
        if code != 0:
            raise SystemExit(f"{self.door.name} exited {code}:\n"
                             f"{bytes(self.err).decode('utf-8', 'replace')[-800:]}")


def capture(door: pathlib.Path, state: pathlib.Path, keys: bytes, width: int, height: int,
            info_extra: dict, *, expect: bool = True) -> list[str]:
    """Drive a walk and return the screen -- or pages -- it ends on."""
    running = Door(door, state, width, height, info_extra, splash=keys == b"~")
    pages: list[str] | None = None
    try:
        running.settle()
        if keys == b"~":
            # The splash clears once to start and once to hand over, and draws
            # every frame in between by cursor address; the emulator replays
            # them into the last frame. It keeps drawing until it hands over,
            # so the settle above has already waited the whole animation out.
            running.finish()
            parts = running.read().split(CLEAR)
            return [parts[1] if len(parts) > 2 else ""]
        if keys == b"^":
            # The masthead, and anything else a door draws before its first
            # screen clears. `last_screen` keeps what follows the clear, so
            # these were unreachable by any walk: War Dialer's scanline was
            # rebuilt in issue #517 with no panel to review it on.
            #
            # Finished like any other walk, not killed. Killing looked necessary
            # because the door is waiting for input here -- but `finish` closes
            # stdin first, and reaching EOF is exactly how every other walk gets
            # its door to exit. Going through it keeps the guarantee that a door
            # which exits nonzero never publishes a panel, including one that
            # crashes *after* the settle window while preparing its first
            # interactive screen, which a `poll()` taken at this instant cannot
            # see.
            running.finish()
            return [running.read().split(CLEAR)[0]]
        index = 0
        while index < len(keys):
            key, suffix = keys[index:index + 1], keys[index + 1:index + 2]
            if suffix == b"%":
                if index + 2 != len(keys):
                    raise SystemExit(f"`%` photographs where a walk stops, so it has "
                                     f"to end one: {keys!r}")
                pages = running.press_pages(key)
                index += 2
                continue
            if suffix == b"*":
                running.press_to_end(key)
                index += 2
                continue
            if suffix == b"?":
                running.press_when_offered(key)
                index += 2
                continue
            if key == b"#":
                running.press_first_offered()
                index += 1
                continue
            if key == b"$":
                running.press_last_offered()
                index += 1
                continue
            if key == b"{":
                end = keys.index(b"}", index)
                running.press_matching(keys[index + 1:end].decode())
                index = end + 1
                continue
            running.press(key, expect=expect)
            index += 1
        if pages is None:
            pages = [last_screen(running.read())]
    except BaseException:
        # A door that hung or crashed the build must not outlive it, or a failed
        # gallery leaves a process per panel behind.
        running.kill()
        raise
    running.finish()
    return pages


def apply_preset(state: pathlib.Path, extra: dict) -> dict:
    """Put the preset where the door reads it; return what the drop file needs."""
    style = extra.get("display_style")
    if style:
        for save in (state / "saves").glob("*.json"):
            if save.name.endswith(".previous") or "recovery" in save.name:
                continue
            data = json.loads(save.read_text(encoding="utf-8"))
            data["display_style"] = style
            save.write_text(json.dumps(data), encoding="utf-8")
    return {key: value for key, value in extra.items()
            if key != "display_style"}


def _screen_rows(running: "Door") -> list[str]:
    """The page the door is looking at: unstyled, unframed, one row per line.

    Only the last page it drew. A door does not clear between screens, so the
    whole conversation is still on the wire, and an entry read out of an earlier
    screen would send the walk somewhere else entirely.
    """
    rows = [re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", row).strip()
            for row in last_screen(running.read()).replace("\r\n", "\n").split("\n")]
    opened = max((index for index, row in enumerate(rows) if row[:1] in ("\u256d", "+")), default=0)
    return [row.strip("\u2502\u2551|").strip() for row in rows[opened:]]


def _entry_key(rows: list[str], pattern: str, follower: str | None = None) -> str | None:
    """The `[K]` of the first listed entry matching `pattern`.

    `follower` is matched against the entry's second row, which is where a
    contract card puts the facts its first row does not have room for.
    """
    for index, row in enumerate(rows):
        match = re.match(r"^\[([0-9A-Z])\]\s+(.*)$", row)
        if not match or not re.search(pattern, match.group(2)):
            continue
        if follower is not None and not re.search(follower, rows[index + 1] if index + 1 < len(rows) else ""):
            continue
        return match.group(1)
    return None


def build_voidrunner_outclassed(door_name: str, door: pathlib.Path, state: pathlib.Path) -> None:
    """The combat fixture, with cargo aboard, against a raider two tiers too strong.

    The fight is played into like the combat fixture's, with a hold bought first so
    that Dump is on offer. What play cannot arrange is the opponent: an opening
    board never posts a tier-4 bounty next door, and a tier-4 random contact needs
    a danger-4 system the opening neighbourhood may not have. So the opponent of
    the fight the door saved is raised to tier 4 in the save, and nothing else --
    the hull, the hold, the credits and the ruleset are what play left.
    """
    build_voidrunner_combat(door_name, door, state, stock_hold=True)
    for save in (state / "saves").glob("*.json"):
        if ".previous" in save.name or "recovery" in save.name:
            continue
        data = json.loads(save.read_text(encoding="utf-8"))
        travel = data.get("pending_travel") or {}
        combat = (travel.get("encounter") or {}).get("combat")
        if combat is None:
            raise SystemExit("the combat fixture did not leave a fight to raise")
        # The opponent of the saved fight, and only that. The journey's copy of the
        # bounty has to stay equal to the contract on the books, or the door refuses
        # the career as inconsistent -- which it did, the first time this was built.
        combat["pirate"].update(tier=4, hp=80, hp_max=80)
        save.write_text(json.dumps(data), encoding="utf-8")


def build_voidrunner_combat(door_name: str, door: pathlib.Path, state: pathlib.Path, *, stock_hold: bool = False) -> None:
    """A career left in the middle of a fight, so combat gets a panel too.

    Combat is only reachable through a jump, and a random encounter is a chance
    roll -- a dozen consecutive hops against the cached career drew none, which
    is why the screen the old presentation was thinnest on had no picture. The
    deterministic route is a bounty whose target is the next system along: that
    forces the fight on arrival rather than rolling for one. The career is left
    mid-fight, because the door commits the fight before narrating it, so every
    later launch resumes straight into the combat screen.

    An opening board carries such a bounty about three times in four, so this
    retries with a fresh career rather than hoping; each attempt is one short
    subprocess. It reads the door's own screens for the keys it presses -- the
    board's letters, the offer's Accept page, the chart's destination -- so the
    save format lives in the door and nowhere else.
    """
    for attempt in range(16):
        remove(state)
        _onboard(door_name, door, state)
        running = Door(door, state, 80, 24, {})
        try:
            running.settle()
            if stock_hold:
                # Market, the first commodity, Purchase, a dozen units, back out.
                for key in (b"M", b"A", b"P", b"1", b"2", b"\r", b"B"):
                    running.press(key, expect=False)
            running.press(b"B")  # the contract board
            rows = _screen_rows(running)
            # A bounty card says BOUNTY on its first row and "1 jumps" on its
            # second; one jump is what makes the fight happen on arrival.
            key = _entry_key(rows, r"BOUNTY", r"\b1 jumps\b")
            target = None
            if key is not None:
                entry = next(row for row in rows if row.startswith(f"[{key}] "))
                target = re.sub(r"^\[.\]\s+(?:\S*\s)?", "", entry).split("  ")[0].strip()
            if not key or not target:
                running.kill()
                continue
            running.press(key.encode())  # the contract's own terms
            # Accept lives on the page that offers it; the first page says which.
            for _ in range(8):
                if "[A] Accept" in " ".join(_screen_rows(running)):
                    break
                running.press(b">")
            running.press(b"A")
            running.press(b"B")  # back to the board
            running.press(b"B")  # back to the deck
            running.press(b"C")  # the chart
            letter = _entry_key(_screen_rows(running), re.escape(target))
            if letter is None:
                running.kill()
                continue
            running.press(letter.encode())
            running.press(b"Y")  # depart; the bounty is waiting on arrival
            if "Combat" not in " ".join(_screen_rows(running)):
                running.kill()
                continue
        except BaseException:
            running.kill()
            raise
        # Deliberately killed, not closed: the career is meant to stay in the
        # fight, and a clean exit would be a career that had left one.
        running.kill()
        return
    raise SystemExit("no adjacent bounty appeared on 16 opening boards; "
                     "the combat fixture could not be built")


def build_voidrunner_played(door_name: str, door: pathlib.Path, state: pathlib.Path) -> None:
    """A career with a fight behind it, for the screens that show a record.

    A brand-new career has no combat victories, one charted system and no
    trading margin, and `achievement_ranking` filters exactly those -- so the
    Hall of Fame's four achievement views photographed their empty-state
    message rather than the tables #493 rebuilt. This takes the combat fixture
    one step further and finishes the fight, which is the shortest walk the door
    itself offers to a career with something on its record.
    """
    build_voidrunner_combat(door_name, door, state)
    running = Door(door, state, 80, 24, {})
    try:
        running.settle()
        for _ in range(20):
            rows = _screen_rows(running)
            if any("STATION SERVICES" in row for row in rows):
                break  # the fight is over, one way or the other, and we are docked
            running.press(b"F" if "[F] Fire" in " ".join(rows) else b"\r", expect=False)
    except BaseException:
        running.kill()
        raise
    running.kill()  # the career on disk is what the panels copy


def build_voidrunner_retiring(door_name: str, door: pathlib.Path, state: pathlib.Path) -> None:
    """The played career, allowed to retire.

    Every other fixture here is built by playing the door and nothing else, and
    that stays the rule. This is the one state play cannot reach inside a build:
    the cheapest ending asks for the top career rank, which is 150,000 credits of
    trading. So the career is played as far as `played` goes -- a fight, a kill, a
    record -- and then one field is raised in its save: the rank it has *retained*.
    Credits, ship, log and everything the ending screen prints are what play left
    there, which is the point of photographing it.
    """
    build_voidrunner_played(door_name, door, state)
    game = load_door(door)
    for save in (state / "saves").glob("*.json"):
        if ".previous" in save.name or "recovery" in save.name or save.parent.name == "scores":
            continue
        data = json.loads(save.read_text(encoding="utf-8"))
        data["pilot"]["highest_rank_seen"] = len(game.RANKS) - 1
        save.write_text(json.dumps(data), encoding="utf-8")


FIXTURE_BUILDERS = {
    ("voidrunner", "outclassed"): build_voidrunner_outclassed,
    ("voidrunner", "combat"): build_voidrunner_combat,
    ("voidrunner", "played"): build_voidrunner_played,
    ("voidrunner", "retiring"): build_voidrunner_retiring,
}


def _onboard(door_name: str, door: pathlib.Path, state: pathlib.Path) -> None:
    """A career or world at its first ordinary screen."""
    state.mkdir(parents=True, exist_ok=True)
    # First-launch keys are the one place a key may find nothing to answer.
    capture(door, state, ONBOARDING[door_name], 80, 24, {}, expect=False)


def base_fixture(door_name: str, door: pathlib.Path, root_dir: pathlib.Path,
                 fresh: bool = False, name: str = "base") -> pathlib.Path:
    """One career or world per (door, fixture), created once and copied by every panel.

    Built in a temporary directory and moved into place only once the door has
    exited cleanly, so an interrupted run never leaves a half-made fixture that
    the next run would accept simply because the directory exists.
    """
    fixture = root_dir / "fixtures" / (door_name if name == "base" else f"{door_name}-{name}")
    if fresh:
        remove(fixture)
    # A directory is not a fixture; a world file or a saved career inside one is.
    # `--fresh` deletes through a tree the door's SQLite handle may still be
    # holding, and on Windows that can take the files and leave the directory
    # behind -- after which a bare existence check accepted a world with no
    # schema in it and failed every panel with `no such table: meta`.
    #
    # `saves` was still being tested for existence rather than for contents,
    # which is the same hazard one level down: `remove` gives up after three
    # seconds, leaving `saves/` and `saves/scores/` behind with no career in
    # them, and every later run then accepted that empty directory as a
    # fixture. Every Voidrunner panel silently rendered the *registration*
    # screen instead of the screen it was named after -- a gallery that looks
    # like it built, and reviews nothing.
    if any(fixture.glob("saves/*.json")):
        return fixture
    remove(fixture)
    build = FIXTURE_BUILDERS.get((door_name, name), _onboard)
    staging = pathlib.Path(tempfile.mkdtemp(prefix="gallery-fixture-"))
    try:
        state = staging / "state"
        build(door_name, door, state)
        # Move the state *into* the fixture directory entry by entry rather than
        # moving the directory itself: if Windows would not let the old fixture
        # go, `shutil.move` treats it as a destination and nests the staging
        # directory inside it, and every panel then opens a world with no schema.
        fixture.mkdir(parents=True, exist_ok=True)
        for entry in sorted(state.iterdir()):
            shutil.move(str(entry), str(fixture / entry.name))
    finally:
        remove(staging)
    return fixture


def last_screen(raw: str) -> str:
    """What is left on the terminal: doors clear before each screen."""
    return raw.split(CLEAR)[-1]


def to_html(screen: str, width: int, height: int, styles: dict[str, str]) -> str:
    """Paint the screen on a canvas the size of the terminal it was drawn for."""
    fragment = term.render(screen, width=width, height=height)
    out = []
    for row in fragment.split("\n"):
        cells, run_name, run_length = [], None, 0
        for style, char in SPAN.findall(row):
            keep = ";".join(part for part in style.split(";")
                            if part.startswith(("color:", "background:", "font-weight")))
            name = styles.setdefault(keep, f"s{len(styles)}")
            if char == " " and run_name in (None, name):
                run_name, run_length = name, run_length + 1
                continue
            if run_length:
                cells.append(f'<i class={run_name} style="width:{run_length}ch"></i>')
            run_name, run_length = (name, 1) if char == " " else (None, 0)
            if char != " ":
                cells.append(f"<i class={name}>{char}</i>")
        if run_length:
            cells.append(f'<i class={run_name} style="width:{run_length}ch"></i>')
        out.append("".join(cells))
    return "\n".join(out) or "&nbsp;"


def shown(mark: str | dict[str, str], preset: str) -> str:
    """What a walk's screen must say under this preset.

    Usually one string for every preset. A dict is for the screen a preset
    deliberately draws differently: Fast mode omits optional art, so War
    Dialer's masthead is `WAR DIALER - Season n` there and the letter-spaced
    wordmark everywhere else, and no single marker covers both.
    """
    return mark.get(preset, mark["*"]) if isinstance(mark, dict) else mark


def painted(screen: str, width: int, height: int) -> str:
    """The panel's text as one line, read back through the emulator that paints it.

    Checking the door's bytes would not do: a heading is styled, so in the byte
    stream its words are separated by the sequences that color them, and a
    screen drawn with cursor moves says nothing in the order it was written.
    """
    canvas = term.Screen(width, height)
    canvas.feed(screen)
    rows = ["".join(char for char, _ in row) for row in canvas.cells]
    return " ".join(" ".join(rows).split())


def build(door_name: str, widths: list[int], heights: dict[int, int],
          out_dir: pathlib.Path, fresh: bool) -> pathlib.Path:
    door = ROOT / f"{door_name}.py"
    if not door.exists():
        raise SystemExit(f"no such door: {door}")
    out_dir.mkdir(parents=True, exist_ok=True)
    # A walk may name the fixture it needs; most want the plain one.
    walks = [(walk + ("base",))[:3] for walk in WALKS[door_name]]
    fixtures = {name: base_fixture(door_name, door, out_dir, fresh, name)
                for name in dict.fromkeys(name for _, _, name in walks)}

    shows = SHOWS.get(door_name, {})
    unnamed = [label for label, _, _ in walks if label not in shows]
    if shows and unnamed:
        raise SystemExit("every walk must say what its screen shows; "
                         f"{door_name} does not for: {', '.join(unnamed)}")

    shots = [(label, keys, preset, extra, width, heights.get(width, 24), fixture)
             for label, keys, fixture in walks
             for preset, extra in PRESETS[door_name].items()
             if keys != b"~" or preset in SPLASH_PRESETS.get(door_name, ())
             for width in widths]

    run_dir = pathlib.Path(tempfile.mkdtemp(prefix="gallery-run-"))

    def shoot(shot) -> list[str]:
        label, keys, _, extra, width, height, fixture = shot
        work = pathlib.Path(tempfile.mkdtemp(dir=run_dir))
        try:
            state = work / "state"
            shutil.copytree(fixtures[fixture], state)
            info_extra = apply_preset(state, extra)
            prepare = PREPARED.get(door_name, {}).get(label)
            if prepare:
                prepare(door, state)
            setup = SETUP.get(door_name, {}).get(label)
            if setup:
                # A launch of its own, at one size, so the setup keys never have
                # to work at every terminal; what it draws is thrown away.
                capture(door, state, setup, 80, 24, info_extra)
            return capture(door, state, keys, width, height, info_extra)
        except BaseException as exc:
            # A build of nine hundred panels that dies must say which walk died:
            # the bare exception names a key and a size, and never the screen.
            raise SystemExit(f"{label} at {width}x{height} {shot[2]} "
                             f"({keys!r}): {type(exc).__name__}: {exc}") from exc
        finally:
            remove(work)

    # Each panel is its own door in its own copy of the fixture, so they can be
    # taken at once; the page is assembled from them in order afterwards. They
    # all live under one directory, so a build that dies takes them with it
    # rather than leaving a career per panel in the system temp directory.
    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            screens = list(pool.map(shoot, shots))
    finally:
        remove(run_dir)

    # A panel that is not the screen its caption names is worse than a missing
    # one: it reads as a review of a screen nobody has looked at. The mark has to
    # be on one of the walk's pages rather than on all of them -- a card stack
    # carries each heading on the page holding that card, and `DEFENCE` is on page
    # one of the exchange card while page two is its terms -- and every page has
    # to have something on it.
    wrong = [f"  {label} at {width}x{height} {preset}: no {shown(shows[label], preset)!r} "
             f"on any of {len(pages)} page(s)"
             for (label, _, preset, _, width, height, _fixture), pages in zip(shots, screens)
             if label in shows
             and not any(shown(shows[label], preset) in painted(screen, width, height)
                         for screen in pages)]
    wrong += [f"  {label} at {width}x{height} {preset} page {number} is blank"
              for (label, _, preset, _, width, height, _fixture), pages in zip(shots, screens)
              for number, screen in enumerate(pages, 1)
              if not painted(screen, width, height).strip()]
    if wrong:
        raise SystemExit(f"{door_name}: a walk did not end on the screen it names:\n"
                         + "\n".join(wrong))

    styles: dict[str, str] = {}
    sections, panels, current = [], [], shots[0][0]
    for (label, _, preset, _, width, height, _fixture), pages in zip(shots, screens):
        if label != current:
            sections.append(f'<section><h2>{html.escape(current)}</h2>'
                            f'<div class="row">{"".join(panels)}</div></section>')
            panels, current = [], label
        for number, screen in enumerate(pages, 1):
            caption = f"{width}x{height} · {preset}"
            if len(pages) > 1:
                caption += f" · page {number}/{len(pages)}"
            panels.append(
                f'<figure><figcaption>{html.escape(caption)}</figcaption>'
                f'<div class="screen"><pre>{to_html(screen, width, height, styles)}</pre>'
                f'</div></figure>')
    sections.append(f'<section><h2>{html.escape(current)}</h2>'
                    f'<div class="row">{"".join(panels)}</div></section>')

    css = "\n".join(f"i.{name}{{{style}}}" for style, name in styles.items() if style)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(door_name)} gallery</title>
<style>
 body{{margin:0;padding:32px 20px 64px;background:#080b12;color:#c7d2e2;
   font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}}
 h1{{margin:0 0 8px;font-size:28px;color:#eef3fa}}
 h2{{margin:0 0 12px;font-size:18px;color:#eef3fa}}
 p{{margin:0 0 24px;max-width:74ch}}
 section{{margin-bottom:32px;padding:16px;border:1px solid #232c3d;border-radius:6px;background:#111725}}
 .row{{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-start}}
 figure{{margin:0;display:flex;flex-direction:column;gap:6px;min-width:0}}
 figcaption{{font:11px/1.4 ui-monospace,Consolas,monospace;letter-spacing:.07em;color:#8794a8}}
 .screen{{background:#05070b;border:1px solid #232c3d;border-radius:4px;padding:10px 12px;overflow-x:auto}}
 pre{{margin:0;white-space:pre;font-family:"DejaVu Sans Mono",Consolas,monospace;
   font-size:12px;line-height:1.32;color:#d5dde9}}
 pre i{{display:inline-block;width:1ch;font-style:normal}}
 code{{font-family:ui-monospace,Consolas,monospace;font-size:.92em}}
{css}
</style></head><body>
<h1>{html.escape(door_name)} — every screen, every size</h1>
<p>Rendered from the real door in a subprocess, typed at one key at a time and
painted by <code>tools/ansi_to_html.py</code> on a canvas the size of
the terminal, from one cached career so that panels differ only by size and
preset. Attach this page to any pull request that changes a screen.</p>
{"".join(sections)}
</body></html>
"""
    target = out_dir / f"{door_name}-gallery.html"
    target.write_text(page, encoding="utf-8")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--widths", type=int, nargs="+", default=[80, 64, 40],
                        help="terminal widths to render (the supported floor is 40)")
    parser.add_argument("--out", type=pathlib.Path, default=ROOT / "build" / "gallery")
    parser.add_argument("--fresh", action="store_true", help="rebuild the cached fixture first")
    parser.add_argument("--open", action="store_true", help="open the page when it is written")
    args = parser.parse_args()

    heights = {80: 24, 64: 20, 40: 12}
    out_dir = args.out.expanduser().resolve()
    target = build("voidrunner", args.widths, heights, out_dir, args.fresh)
    print(f"wrote {target}")
    if args.open:
        webbrowser.open(target.as_uri())


if __name__ == "__main__":
    main()
