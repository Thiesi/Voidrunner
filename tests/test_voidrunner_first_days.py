"""A new career's first days, played by keys against the real door (#93).

Two regressions in the review showed only on a fresh career's first day and
were found by playing it: Disembark took a row of the deck to itself on some
days (#69), and the market's season mark pushed the SPREAD column off at 80
columns on a day a contract's NEED was showing (#82). Unit tests build a
`World` and call one screen's builder; nothing drove the door from the
commission screen onward.

This does. It starts the door with an empty save directory and plays the way
a new caller is told to: commission, the day-zero deck, the Guide and its
First Flight offer, the Market to buy the delivery, the Chart to fly it, the
arrival deck, the Market to sell what is left, the Board and the Yard, every
service on the deck and back, and a second jump home. At every stop:

- nothing on the screen runs past the terminal's last column, every box is
  square and nothing failed to decode (`tools/frame_check.py`);
- the key pressed next is one the screen advertises, and it is answered;
- at 80x24 the deck is one page on day zero and day one.

It is run at 80x24 and at 160x48, where the deck has its own wide layout (#91).
The galaxy is a new one every run, as a caller's is, so the walk reads the
screens for its keys -- the commodity and the station First Flight names, the
letters the market and the chart give them -- rather than knowing them.

That makes it the one test here whose luck is not fixed, on purpose: each run
is a different first day, which is how #69 and #82 were found. So it answers
whatever a journey raises, the way the career simulator's pilot does, and
sells only what the journey left in the hold.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
#: Keys a journey may take before the arrival deck: a fight, its prize, a
#: customs stop and their result pages. Past it the walk met a prompt it cannot read.
JOURNEY_KEYS = 40
#: What this walk found on `main` that is filed as an issue of its own. A
#: finding listed here is noted and the walk goes on; a walk that met one ends
#: as an expected failure naming the issue, and passes once it is fixed. The
#: galaxy is new every run, so whether a run meets one is chance: these are
#: never strict.
KNOWN = {
    "day-one deck": "#95",  # First Flight's arrival results push the day-1 deck to two pages at 80x24
}


@pytest.fixture(scope="module")
def gallery():
    """`tools/door_gallery.py`, whose `Door` types at a real door one key at a
    time and waits for each screen to finish drawing."""
    sys.path.insert(0, str(ROOT / "tools"))
    spec = importlib.util.spec_from_file_location("door_gallery_first_days", ROOT / "tools" / "door_gallery.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _advertised(rows: list[str]) -> set[str]:
    """Every key the screen offers: `[K]` entries, and the bar's `[A,C-H]`,
    `[1-9]` and `[Y/N, Esc=No]` spellings."""
    keys = set()
    for inside in re.findall(r"\[([^\]]{1,24})\]", " ".join(rows)):
        for part in re.split(r"[,/]", inside):
            part = part.strip()
            span = re.fullmatch(r"([A-Z0-9])-([A-Z0-9])", part)
            if span:
                keys.update(chr(code) for code in range(ord(span.group(1)), ord(span.group(2)) + 1))
            elif re.fullmatch(r"[A-Z0-9<>]", part):
                keys.add(part)
    return keys


def _journey_key(text: str) -> str:
    """The key for a screen met between departure and arrival.

    The career simulator's pilot (`Pilot.key`), in the same order: fight;
    strip a beaten raider; answer a derelict or a distress call; pay, give in
    or carry on; say yes; go back; and Enter for a page that only wants a key.
    """
    offered = dict(re.findall(r"\[([A-Z])\] ([A-Za-z]+)", text))
    act = re.search(r"\[([A-Z])/[A-Z]\] Act:", text)
    if offered.get("F") == "Fire":
        return "F"
    if offered.get("S") == "Strip":
        return "S"
    if act:
        return act.group(1)
    for key, label in (("P", "Pay"), ("S", "Surrender"), ("C", "Continue")):
        if offered.get(key) == label:
            return key
    if "Y/N" in text:
        return "Y"
    return "B" if "B" in offered else "\r"


class Walk:
    """One career, typed at; every screen it stops at is checked."""

    def __init__(self, gallery, state: pathlib.Path, width: int, height: int):
        self.g, self.width, self.height = gallery, width, height
        self.door = gallery.Door(ROOT / "voidrunner.py", state, width, height, {})
        self.stops: list[str] = []
        self.known: list[str] = []

    # --- reading the screen ----------------------------------------------------

    def screen(self) -> str:
        return self.g.last_screen(self.door.read())

    def rows(self) -> list[str]:
        canvas = self.g.term.Screen(self.width, self.height)
        canvas.feed(self.screen())
        return ["".join(char for char, _ in row).rstrip() for row in canvas.cells]

    def text(self) -> str:
        return "\n".join(self.rows())

    def prose(self) -> str:
        """The words inside the page's frame as one line, so a sentence that
        wraps inside it reads as one. Only inside it: on a wide terminal the
        canopy and its slate stand beside the frame, row for row."""
        rows = self.rows()
        top = self.title()
        left = len(top) - len(top.lstrip())
        right = next((col for col in range(left + 1, len(top))
                      if top[col] in "╮+" and (col + 1 == len(top) or top[col + 1] not in "-=")), len(top))
        return " ".join(" ".join(row[left + 1:right] for row in rows).split())

    def title(self) -> str:
        """The frame's top row, which carries the page's title and counter."""
        return next((row for row in self.rows() if re.match(r"\s*(╭|\+-)", row)), "")

    def page(self) -> tuple[int, int] | None:
        found = re.search(r"(\d+)/(\d+)\s*[╮+]", self.title())
        return (int(found.group(1)), int(found.group(2))) if found else None

    def fail(self, why: str) -> None:
        pytest.fail(f"{why} at {self.width}x{self.height}, after "
                    f"{' > '.join(self.stops[-6:])}:\n{self.text()}", pytrace=False)

    # --- checking and pressing -------------------------------------------------

    def check(self, stop: str) -> None:
        self.stops.append(stop)
        problems = self.g.frame_problems(self.screen(), self.width, self.height)
        if problems:
            self.fail(f"{stop}: " + "; ".join(problems))

    def start(self) -> None:
        try:
            self.door.settle()
        except SystemExit as exc:
            self.fail(str(exc))
        self.check("launch")

    def press(self, key: str, stop: str, *, advertised: bool = True) -> None:
        """Press `key`, which the screen must offer, and check what it draws."""
        if advertised and key not in _advertised(self.rows()):
            # A deck #95 pushed onto two pages shows its services on page two;
            # the door takes their keys on either page, as a caller finds.
            if not (self.known and "Command Deck" in self.title() and (self.page() or (1, 1))[1] > 1):
                self.fail(f"{key!r} is not offered for {stop}")
        try:
            self.door.press(key.encode())
        except SystemExit as exc:
            self.fail(f"{key!r} for {stop} was not answered: {exc}")
        self.check(stop)

    def type(self, text: str, stop: str) -> None:
        """A quantity, one keystroke at a time: a burst is discarded as paste."""
        for character in text:
            self.press(character, stop, advertised=False)
        self.press("\r", stop, advertised=False)

    def expect(self, title: str) -> None:
        if title not in self.title():
            self.fail(f"expected {title!r} in the title")

    def deck_is_one_page(self, day: int) -> None:
        self.expect("Command Deck")
        if f"Day {day}" not in self.text():
            self.fail(f"expected the day-{day} deck")
        if (self.width, self.height) == (80, 24) and self.page() != (1, 1):
            finding = f"the day-{day} deck is page {self.page()} at 80x24, not 1/1"
            if day == 1:
                self.known.append(f"{finding} ({KNOWN['day-one deck']})")
            else:
                self.fail(finding)

    def entry(self, name: str) -> str:
        """The `[K]` of the listed entry that names `name`."""
        for row in self.rows():
            found = re.search(r"\[([A-Z0-9])\]\s+" + re.escape(name) + r"\b", row)
            if found:
                return found.group(1)
        self.fail(f"no entry names {name!r}")

    # --- a journey ---------------------------------------------------------------

    def fly(self, destination: str, day: int, *, or_first: bool = False) -> None:
        """Chart, the destination's letter, yes, and a key per page until the
        next day's deck, wherever the journey ended."""
        self.press("C", "Chart")
        self.expect("Navigation")
        if or_first and not re.search(r"\[[A-Z]\]\s+" + re.escape(destination) + r"\b", self.text()):
            first = re.search(r"\[([A-Z])\]\s+([A-Z][\w' ]*?)\s{2}", self.text().split("DEPARTURES", 1)[-1])
            if not first:
                self.fail("the chart lists no departure")
            destination = first.group(2)
        self.press(self.entry(destination), f"Chart: {destination}")
        if "Y/N" not in self.text():
            self.fail(f"no departure question for {destination}")
        self.press("Y", f"depart for {destination}")
        for _ in range(JOURNEY_KEYS):
            if "Command Deck" in self.title() and f"Day {day}" in self.text():
                return
            key = _journey_key(self.text())
            self.press(key, f"journey to {destination}", advertised=key != "\r")
        self.fail(f"the journey to {destination} did not reach the day-{day} deck in {JOURNEY_KEYS} keys")

    def every_service_and_back(self) -> None:
        """Every key the deck advertises opens a screen of its own, and Back
        comes home from it."""
        services, turned = _advertised(self.rows()), 0
        while (self.page() or (1, 1))[0] < (self.page() or (1, 1))[1]:
            self.press(">", "the deck's next page")
            services |= _advertised(self.rows())
            turned += 1
        for _ in range(turned):
            self.press("<", "the deck's previous page")
        for key in sorted(services - {"Q", "X", "<", ">"}):
            self.press(key, f"deck [{key}]")
            if "Command Deck" in self.title():
                self.fail(f"the deck's [{key}] drew the deck again")
            self.press("B", f"back from [{key}]", advertised=False)
            self.expect("Command Deck")


@pytest.mark.parametrize("size", [(80, 24), (160, 48)], ids=["80x24", "160x48"])
def test_a_new_career_plays_its_first_days_on_square_screens(gallery, tmp_path, size):
    walk = Walk(gallery, tmp_path, *size)
    try:
        walk.start()
        walk.expect("Pilot Commission")
        walk.press("\r", "callsign", advertised=False)  # the drop file's handle
        walk.press("Y", "launch")
        walk.deck_is_one_page(0)

        # The Guide and its First Flight: accept it, and read what it asks for.
        walk.press("G", "Guide")
        walk.expect("Pilot Guide")
        walk.press("O", "First Flight offer")
        walk.expect("First Flight")
        for _ in range(4):
            if "[A] Accept" in walk.text():
                break
            walk.press("N", "First Flight terms")
        walk.press("A", "accept First Flight")
        walk.deck_is_one_page(0)
        asked = re.search(r"buy (\d+) units of (.+?), then \[C\] Chart to (.+?)\.", walk.prose())
        if not asked:
            walk.fail("the deck does not say what First Flight wants")
        units, commodity, destination = int(asked.group(1)), asked.group(2), asked.group(3)

        # The Market: the delivery and three more to sell on arrival.
        walk.press("M", "Market")
        walk.expect("Market")
        walk.press(walk.entry(commodity), f"Market: {commodity}")
        walk.press("P", "Purchase")
        walk.type(str(units + 3), "quantity")
        walk.expect("Market")
        walk.press("B", "back to the deck", advertised=False)
        walk.deck_is_one_page(0)

        # Day one, at First Flight's station.
        walk.fly(destination, 1)
        walk.deck_is_one_page(1)
        walk.press("M", "Market")
        # What is left to sell: three units, unless the journey's luck took
        # the hold (a raider, a lost ship and a tow home all happen on day one).
        held = re.search(r"\[[A-Z]\]\s+" + re.escape(commodity) + r"(?:\s+\d+){4}\s+(\d+)", walk.text())
        if held and int(held.group(1)):
            walk.press(walk.entry(commodity), f"Market: {commodity}")
            walk.press("S", "Sell")
            walk.type(held.group(1), "quantity")
            walk.expect("Market")
        walk.press("B", "back to the deck", advertised=False)
        walk.press("B", "Board")
        walk.expect("Contracts")
        walk.press("B", "back to the deck")
        walk.press("Y", "Yard")
        walk.expect("Yard")
        walk.press("B", "back to the deck")
        walk.deck_is_one_page(1)
        walk.every_service_and_back()

        # Day two, home to Freeport -- or, for a career towed there already,
        # to the first station the chart lists.
        walk.fly("Freeport", 2, or_first=True)
        walk.expect("Command Deck")
    finally:
        walk.door.kill()
    if walk.known:
        pytest.xfail("; ".join(walk.known))


@pytest.mark.parametrize(("bar", "key"), [
    ("[F] Fire [G] Guard [E] Evade [P] Pay bribe:", "F"),           # a fight
    ("[S] Strip it [C] Take cargo [H] Hand over [L] Let go:", "S"),  # its prize (#94)
    ("[H/I] Act: ", "H"),                                            # a distress call
    ("[S] Surrender [P] Pay bribe [<>] Page: ", "P"),                # customs
    ("[<>] Page [C] Continue: ", "C"),
    ("Depart for Freeport? [Y/N, Esc=No]", "Y"),
    ("[B] Back:", "B"),
    ("Press any key", "\r"),
])
def test_the_walk_answers_every_prompt_a_journey_raises(bar, key):
    """Which of these a run meets is chance, so each is pinned here."""
    assert _journey_key(f"╭─ Page ─ 1/1 ╮\n│ text │\n╰──────╯\n{bar}") == key
