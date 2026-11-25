"""Fly whole careers with simple pilots and report how they went.

    python tools/career_sim.py                      # every pilot, 12 seeds, 100 days
    python tools/career_sim.py --pilots trader --days 60 --seeds 20
    python tools/career_sim.py --set TRAIT_CHANCE=0 --set CONTRABAND_STREET_PREMIUM=1.0
    python tools/career_sim.py --door path/to/an/older/voidrunner.py

A balance question about one rule can be answered by calling that rule. A
question about all of them together cannot: what a trait is worth depends on
the hold that carries it, which depends on what the contracts paid, which
depends on the rank. This flies the door's own journeys (`screen_travel`, with
its encounters, fights, customs and contract completions) and its own market,
board, yard and stake functions, with a pilot that presses the keys.

The first four pilots are deliberately plain. They see one jump ahead, and
they see a neighbouring market's prices whether or not they have docked there,
so a trader here is a little better informed than a caller. None of them is a
ceiling: they say how the game's parts weigh against each other.

The fifth, `careful`, is nearer to a caller who thinks. It trades as the
trader does, but it weighs the leg after next -- only between stations it has
docked at, which is what a caller's ledger knows -- and when the lane it is on
stops paying it leaves for the nearest station it has not seen lately, where a
plain pilot shuttles between two markets it has drained for the rest of its
career. It is still no ceiling, but it is the better bound on one.

The summary ends with when each thing a career works toward is first reached
-- each hull, every upgrade, each rank, the first stake and the last -- because
"how long does the game last" is asked in those days and not in credits.
`--jumps-per-day` says how many jumps a caller flies in a real day: it turns
the daily dispatch over and translates the days into real ones.

`--factions` has the pilots work toward a faction and join it the day the door
lets them: a contractor and the traders hand beaten raiders to the Navy for
Concord's commission, a smuggler lets them go for the Cartel's, and each takes
the work its faction then posts. It is off by default, so the plain pilots
stay what they were.

`--futures` is for an older build flown with `--door`: the futures exchange
was removed after these pilots showed that using it cost a career money, and
on a build without one the switch does nothing. There, a pilot holds today's
price, at a station it has been to before, on a good the season will make
dearer.

`--set NAME=VALUE` changes a module constant before each career, which is how
one rule is switched off to see what it was contributing. `--prize` is what
every pilot does with a beaten raider (strip it, by default, which is what a
kill paid before there was a choice). `--door` flies
another build with the same pilots, where the functions a pilot uses exist.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import multiprocessing
import pathlib
import re
import statistics
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
PILOTS = ("trader", "smuggler", "contractor", "explorer", "careful")
#: A hold that clears less than this a unit is a lane run dry: time to look elsewhere.
DRAINED_PER_UNIT = 8
#: What the careful pilot thinks the leg after next is worth, against the leg in hand.
SECOND_LEG_WEIGHT = 0.5
CHECKPOINTS = (10, 20, 40, 60, 80, 100, 150, 200, 300)
#: What a career works toward, in the order the summary lists them: the day each is first reached.
MILESTONES = ("Freighter", "Cutter", "Carrier", "fitted", "rank 2", "rank 3", "rank 4", "stake", "all stakes",
              "Concord", "Blackwake")
#: A futures order is placed when the season will carry the price up by more than this by the day it is collected.
FUTURES_WORTH_IT = 0.14
#: Orders a pilot keeps open at once, each at a different station.
FUTURES_OPEN = 2
#: Careers are flown to a real day: the dispatch changes once for every this many jumps.
JUMPS_PER_REAL_DAY = 12
#: More keys than any one journey needs; past it the pilot has met a prompt it cannot read.
KEYS_PER_JOURNEY = 600
#: Below this a pilot cannot fill a Shuttle's hold with anything worth carrying.
BROKE = 600
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_KEYS = re.compile(r"\[([A-Z])\] ([A-Za-z]+)")
_INTENT = re.compile(r"([A-Za-z]+) > ([A-Za-z]+) > ([A-Za-z]+)")
_TIER = re.compile(r"tier (\d)")
_ACT = re.compile(r"\[([A-Z])/[A-Z]\] Act: $")


class Stuck(RuntimeError):
    """A journey asked for more keys than any journey needs."""


def load_door(path: str):
    spec = importlib.util.spec_from_file_location("voidrunner_sim", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def apply_settings(vr, settings: dict[str, str]) -> None:
    for name, text in settings.items():
        if not hasattr(vr, name):
            raise SystemExit(f"{name}: this build has no such constant")
        value = json.loads(text)
        setattr(vr, name, tuple(value) if isinstance(getattr(vr, name), tuple) else value)


class Pilot:
    """One career: the keys a journey asks for, and what to do at a station."""

    def __init__(self, vr, kind: str, seed: int, *, dispatch: bool = True, stakes: bool = True, prize: str = "S",
                 jumps_per_day: int = JUMPS_PER_REAL_DAY, factions: bool = False, futures: bool = False,
                 nerve: int | None = None, charter: str = "standard"):
        self.vr, self.kind, self.buy_stakes = vr, kind, stakes
        self.fixed_nerve = nerve    # the most dangerous harbor flown to by choice, whatever the ship; None is by the ship
        self.factions, self.futures = factions, futures
        self.joined = {}            # the day each faction was joined
        self.orders = Counter()     # futures orders placed and collected, and what collecting saved
        self.keep = Counter()       # goods a journey put aboard that are for the next market, not this one
        self.jumps_per_day = max(1, jumps_per_day)
        self.prize = prize          # what to do with a beaten raider, where that is offered
        self.prizes = Counter()     # what was done with each
        vr.apply_display_style("plain")
        save = vr._new_career("Sim")
        save.seed = seed
        if charter != "standard" and hasattr(save, "charter"):
            save.charter = charter  # raiders a tier stronger, no dock work, a doubled tug fee
        self.world = vr.World(save)
        self.world.event_rng.seed(seed)  # a career's luck is its own; here it follows the seed, to fly it again
        if hasattr(self.world, "dispatch_clock"):
            self.world.dispatch_clock = (
                (lambda: 740_000 + self.world.save.turn // self.jumps_per_day) if dispatch else (lambda: None))
        self.palette = vr.Palette(False)
        self.screen = io.StringIO()
        self.keys = 0
        self.spent = Counter()      # credits put into the ship, the crew and stations
        self.earned = Counter()     # credits by where they came from
        self.lost = 0               # ships
        self.towed = 0              # times a tug brought a dry, broke ship home
        self.shifts = 0             # days worked on Freeport's docks
        self.docked = {}            # the day each station was last left
        self.roam = None            # the station a careful pilot has set out for, off a lane run dry
        self.roam_steps = 0         # jumps it will still spend getting there
        self.gave_up = {}           # the day it last stopped making for each station
        self.roamed = 0             # times it did
        self.fights = Counter()     # keys pressed in a fight, by key
        vr.read_key = self.key

    # --- the keys a journey asks for ------------------------------------------

    def key(self) -> str:
        self.keys += 1
        text = _ANSI.sub("", self.screen.getvalue())
        if self.keys > KEYS_PER_JOURNEY:
            raise Stuck(text[-400:])
        self.screen.seek(0)
        self.screen.truncate()
        offered = dict(_KEYS.findall(text[-260:]))
        if offered.get("F") == "Fire":
            choice = self.fight(text, offered)
            self.fights[choice] += 1
            return choice
        if offered.get("S") == "Strip":
            # A beaten raider: this pilot's way with one, or its scrap where that is not offered.
            want = self.courting() or self.prize
            choice = want if want in offered else "S"
            self.prizes[choice] += 1
            return choice
        act = _ACT.search(text[-40:])
        if act:
            return act.group(1)  # a derelict or a distress call: salvage it, answer it
        for key, label in (("P", "Pay"), ("S", "Surrender"), ("C", "Continue")):
            if offered.get(key) == label:
                return key
        if "Y/N" in text[-120:]:
            return "Y"
        if "B" in offered:
            return "B"
        return ("\r", " ", "B", "N")[self.keys % 4]

    def fight(self, text: str, offered: dict[str, str]) -> str:
        ship = self.world.save.ship
        hull = ship.hull_hp / self.vr.hull_hp_max(ship)
        tier = _TIER.findall(text)
        outclassed = bool(tier) and self.vr.raider_outclasses(ship, int(tier[-1]))
        intent = _INTENT.findall(text)
        if (outclassed or hull < 0.25) and "E" in offered:
            return "E"
        # The pattern row names all three moves and capitalises the one coming.
        move = next((step for step in intent[-1] if step.isupper()), "") if intent else ""
        # The crew's stations, each once a fight: slip the volley, patch a hull
        # that is half gone while nothing heavy is coming, call the shot into a recovery.
        if move == "VOLLEY" and offered.get("N") == "Slip":
            return "N"
        if hull < 0.5 and move != "VOLLEY" and offered.get("M") == "Mend":
            return "M"
        if move == "RECOVER" and offered.get("C") == "Called":
            return "C"
        if move == "VOLLEY" and "G" in offered:
            return "G"
        return "F"

    # --- a station --------------------------------------------------------------

    def sell_price(self, system_id: int, commodity: str) -> int:
        vr = self.vr
        if hasattr(vr, "sell_price_for"):
            return vr.sell_price_for(self.world, system_id, commodity)
        return round(vr.price_for(self.world, system_id, commodity) * vr.SELL_SPREAD)

    def reserved(self) -> Counter:
        held = Counter()
        for mission in self.world.save.active_missions:
            if mission.kind == "delivery":
                held[mission.commodity] += mission.quantity
        return held

    def trade(self, commodity: str, quantity: int, *, buying: bool, account: str = "trade") -> bool:
        credits = self.world.save.pilot.credits
        try:
            self.vr.trade_cargo(self.world, commodity, quantity, buying=buying)
        except self.vr.TradeError:
            return False
        self.earned[account] += self.world.save.pilot.credits - credits
        return True

    def sell_hold(self) -> None:
        world, vr = self.world, self.vr
        reserved = self.reserved() + self.keep
        for commodity, held in list(world.save.cargo.items()):
            free = held - reserved[commodity]
            quantity = min(free, vr.market_depth_quote(world, world.here.id, commodity)["demand"])
            if quantity > 0 and self.sell_price(world.here.id, commodity) > 0:
                self.trade(commodity, quantity, buying=False)
        world.commit()

    def maintain(self) -> None:
        world, vr = self.world, self.vr
        ship, pilot = world.save.ship, world.save.pilot
        fuel = min(vr.fuel_capacity(ship) - ship.fuel, pilot.credits // vr.FUEL_PRICE)
        if fuel > 0:
            pilot.credits -= fuel * vr.FUEL_PRICE
            ship.fuel += fuel
            self.spent["fuel"] += fuel * vr.FUEL_PRICE
        missing = vr.hull_hp_max(ship) - ship.hull_hp
        if missing * 4 >= vr.hull_hp_max(ship):  # a quarter down: see to it
            unit = vr.repair_cost_per_hp(ship)
            mend = min(missing, pilot.credits // max(1, unit))
            pilot.credits -= mend * unit
            ship.hull_hp += mend
            self.spent["repairs"] += mend * unit

    def invest(self) -> None:
        """Spend what is beyond a hold's worth of working capital: the hold and
        the next hull first, then the rest of the ship and a crew, then stakes;
        cheapest first within each, half the next hull's price kept back while
        there is one to save for, and a Shuttle fitted no further than one tier
        of anything but its hold."""
        world, vr = self.world, self.vr
        ship, pilot = world.save.ship, world.save.pilot
        while True:
            reserve = vr.cargo_capacity(ship) * 60
            offers = []
            for key, upgrade in vr.UPGRADES.items():
                tier = getattr(ship, f"{key}_tier")
                if tier < upgrade["max_tier"] and (key == "cargo" or tier < 1 or ship.hull_class != "Shuttle"):
                    offers.append((0 if key == "cargo" else 1, upgrade["cost"](tier), "upgrade", key))
            for target, cost in vr.HULL_REFITS[ship.hull_class]:
                if target in ("Carrier", "Cutter" if self.kind == "contractor" else "Freighter"):
                    offers.append((0, cost, "refit", target))
            for role, info in vr.CREW_ROLES.items():
                if not getattr(ship, f"has_{role}"):
                    offers.append((1, info["hire_cost"] + info["wage"] * 40, "crew", role))
            if self.buy_stakes and hasattr(vr, "stake_blocker") and ship.hull_class != "Shuttle":
                level = vr.stake_level(world.save, world.here.id)
                if level < len(vr.STAKE_COSTS) and (level or len(vr.stakes(world.save)) < vr.STAKE_MAX_STATIONS):
                    offers.append((2, vr.STAKE_COSTS[level], "stake", ""))
            saving = min((cost for first, cost, what, _key in offers if what == "refit"), default=0) // 2
            affordable = [offer for offer in sorted(offers)
                          if pilot.credits - offer[1] >= reserve + (saving if offer[0] else 0)]
            if not affordable:
                return
            _first, cost, what, key = affordable[0]
            before = pilot.credits
            if what == "upgrade":
                pilot.credits -= cost
                setattr(ship, f"{key}_tier", getattr(ship, f"{key}_tier") + 1)
            elif what == "refit":
                pilot.credits -= cost
                ship.hull_class = key
                ship.hull_hp = vr.hull_hp_max(ship)
            elif what == "crew":
                vr.hire_crew(world, key)
            else:
                vr.buy_stake(world)
            self.spent[what] += before - pilot.credits
            world.commit()

    def faction(self) -> str:
        """The faction this pilot works toward: the Cartel for a smuggler, the Navy for the rest."""
        return self.vr.FACTION_BLACKWAKE if self.kind == "smuggler" else self.vr.FACTION_CONCORD

    def courting(self) -> str | None:
        """What to do with a beaten raider while a membership is still to be
        earned: hand it to the Navy, or let it go for the Cartel's regard."""
        if not self.factions or self.faction() in self.joined:
            return None
        return "L" if self.faction() == self.vr.FACTION_BLACKWAKE else "H"

    def join(self) -> None:
        """Take a membership the day the door offers it."""
        world, vr = self.world, self.vr
        if not self.factions:
            return
        for faction in (vr.FACTION_CONCORD, vr.FACTION_BLACKWAKE):
            if faction not in self.joined and vr.faction_join_blocker(world, faction) is None:
                credits = world.save.pilot.credits
                vr.join_faction(world, faction)
                self.earned["grants"] += world.save.pilot.credits - credits
                self.joined[faction] = world.save.turn
                world.commit()

    def order_futures(self) -> None:
        """Hold today's price, at a station the pilot has been to before, on the
        good the season will carry furthest up by the day it is collected."""
        world, vr = self.world, self.vr
        here, ship, save = world.here, world.save.ship, world.save
        if not self.futures or not hasattr(vr, "buy_futures_contract"):
            return  # the exchange closed after v1.7.2; with --door, an older build still has one
        if (here.id not in self.docked or len(save.active_futures) >= FUTURES_OPEN
                or any(order.origin_system == here.id for order in save.active_futures)):
            return
        spare = save.pilot.credits - vr.cargo_capacity(ship) * 60
        best = None
        for commodity in vr.LEGAL_COMMODITIES:
            price = vr.price_for(world, here.id, commodity)
            for days in vr.FUTURES_DURATIONS:
                rise = vr.season_forecast(world, here.id, commodity, days) / price - 1
                if rise > FUTURES_WORTH_IT and (best is None or rise > best[0]):
                    best = (rise, commodity, days, price)
        if best is None:
            return
        _rise, commodity, days, price = best
        stock = vr.market_depth_quote(world, here.id, commodity)["stock"]
        quantity = min(vr.cargo_capacity(ship), stock, spare * 100 // max(1, price * (100 + vr.FUTURES_FEE_PERCENT)))
        if quantity < max(4, vr.cargo_capacity(ship) // 4):
            return
        try:
            vr.buy_futures_contract(world, commodity, quantity, days)
        except vr.TradeError:
            return
        self.orders["placed"] += 1
        world.commit()

    def take_contracts(self) -> None:
        world, vr = self.world, self.vr
        mine = self.factions and self.faction() in self.joined
        if self.kind != "contractor" and world.save.pilot.credits >= BROKE and not mine:
            return  # the others turn to the board only when there is nothing to trade with
        slots = vr.contract_slots(world.save) if hasattr(vr, "contract_slots") else vr.MAX_ACTIVE_MISSIONS
        hops = vr.bfs_hops(world.by_id, world.here.id)
        ship = world.save.ship
        for mission in vr.generate_mission_board(world):
            if len(world.save.active_missions) >= slots:
                break
            if (self.kind != "contractor" and world.save.pilot.credits >= BROKE
                    and vr.faction_contract(mission) != self.faction()):
                continue  # a member with a hold to fill takes its faction's work and leaves the rest
            away = hops.get(mission.target_system, 99)
            if world.by_id[mission.target_system].danger > self.nerve():
                continue
            if away > 5 or (mission.deadline_turn is not None and world.save.turn + away + 1 > mission.deadline_turn):
                continue
            if mission.kind == "delivery":
                cost = vr.price_for(world, world.here.id, mission.commodity) * mission.quantity
                room = vr.cargo_capacity(ship) - sum(world.save.cargo.values())
                stock = vr.market_depth_quote(world, world.here.id, mission.commodity)["stock"]
                if cost >= mission.reward or mission.quantity > min(room, stock) or cost > world.save.pilot.credits:
                    continue
                if not self.trade(mission.commodity, mission.quantity, buying=True, account="contract goods"):
                    continue
            elif mission.kind == "scan":
                if world.by_id[mission.target_system].discovered:
                    continue
            elif mission.kind in ("bounty", "escort") and ship.weapon_tier < 2:
                continue
            try:
                vr.accept_mission(world, mission)
            except vr.MissionError:
                continue
        world.commit()

    def drop_lost_causes(self) -> None:
        """A delivery whose goods went down with a ship is bought again here if
        that is possible, and given up if not: a pilot does not fly to a
        client empty-handed for the rest of a career."""
        world, vr = self.world, self.vr
        for mission in list(world.save.active_missions):
            if mission.kind != "delivery":
                continue
            short = mission.quantity - world.save.cargo.get(mission.commodity, 0)
            if short <= 0 or self.trade(mission.commodity, short, buying=True, account="contract goods"):
                continue
            try:
                vr.abandon_mission(world, mission.id)
            except (vr.MissionError, ValueError):
                pass
        world.commit()

    def nerve(self) -> int:
        """The most dangerous harbor this ship is taken to by choice: a caller
        reads the danger column, and a bare or battered Shuttle keeps to calm space."""
        ship, vr = self.world.save.ship, self.vr
        if self.fixed_nerve is not None:
            return self.fixed_nerve
        if ship.hull_hp * 2 < vr.hull_hp_max(ship):
            return 2
        if ship.hull_class == "Shuttle":
            return 3 if ship.weapon_tier + ship.hull_tier + ship.shield_tier < 2 else 4
        return 5

    def plan(self, dest) -> tuple[int, list[tuple[str, int]]]:
        """What a hold bought here would clear at `dest`, and what to buy."""
        world, ship = self.world, self.world.save.ship
        room = self.vr.cargo_capacity(ship) - sum(world.save.cargo.values())
        return self.leg(world.here, dest, room, world.save.pilot.credits)

    def leg(self, here, dest, room: int, credits: int) -> tuple[int, list[tuple[str, int]]]:
        """What `room` units bought at `here` with `credits` would clear at `dest`, and what to buy."""
        world, vr = self.world, self.vr
        ship = world.save.ship
        goods = list(vr.COMMODITIES) if self.kind == "smuggler" else list(vr.LEGAL_COMMODITIES)
        margins = sorted(((self.sell_price(dest.id, c) - vr.price_for(world, here.id, c), c) for c in goods), reverse=True)
        profit, buys = 0, []
        for margin, commodity in margins:
            if margin <= 0 or room <= 0:
                break
            if not vr.COMMODITIES[commodity]["legal"]:
                if here.economy != "Haven":
                    continue
                if dest.economy != "Haven":
                    margin = int(margin * (1 - vr.customs_risk_for(world, dest))) - 5
                    if margin <= 0:
                        continue
            price = vr.price_for(world, here.id, commodity)
            quantity = min(room, vr.market_depth_quote(world, here.id, commodity)["stock"],
                           vr.market_depth_quote(world, dest.id, commodity)["demand"], credits // max(1, price))
            if quantity > 0:
                buys.append((commodity, quantity))
                profit += margin * quantity
                room -= quantity
                credits -= price * quantity
        return profit - vr.fuel_cost_for_jump(here, dest, ship) * vr.FUEL_PRICE, buys

    def next_stop(self):
        world, vr = self.world, self.vr
        here, ship = world.here, world.save.ship
        reach = [world.by_id[sid] for sid in here.connections
                 if vr.fuel_cost_for_jump(here, world.by_id[sid], ship) <= ship.fuel]
        if not reach:
            return None, []
        calm = [dest for dest in reach if dest.danger <= self.nerve()]
        reach = calm or [min(reach, key=lambda dest: (dest.danger, dest.id))]
        targets = [m.target_system for m in world.save.active_missions if m.target_system != here.id]
        if targets:
            best = None
            for dest in reach:
                hops = vr.bfs_hops(world.by_id, dest.id)
                away = min(hops.get(target, 99) for target in targets)
                if best is None or away < best[0]:
                    best = (away, dest)
            return best[1], self.plan(best[1])[1]
        plans = [(self.plan(dest), dest) for dest in reach]
        if self.kind == "careful":
            return self.careful_stop(reach, plans)
        if self.kind == "explorer":
            new = [(plan, dest) for plan, dest in plans if not dest.discovered]
            if new:
                plans = new
        # A lane worked every day runs dry: a market left alone for a while is
        # worth a little more than its price today says, up to ten days' rest.
        hold, today = vr.cargo_capacity(ship), world.save.turn
        (_profit, buys), dest = max(plans, key=lambda item: (
            item[0][0] + hold * 2 * min(10, today - self.docked.get(item[1].id, today - 10)), -item[1].id))
        return dest, buys

    def careful_stop(self, reach, plans):
        """The careful pilot's choice: the leg in hand plus half the best leg
        after it, and off the lane altogether once a full hold stops paying."""
        world, vr = self.world, self.vr
        here, ship = world.here, world.save.ship
        hold, today = vr.cargo_capacity(ship), world.save.turn
        best_now = max(profit for (profit, _buys), _dest in plans)
        if self.roam is not None:
            self.roam_steps -= 1
            if self.roam == here.id or best_now >= hold * DRAINED_PER_UNIT * 3 or self.roam_steps < 0:
                # Arrived, or the lane pays again, or the way there is shut (a
                # dangerous harbor between, or more fuel than the tank holds):
                # either way that station is not set out for again soon.
                self.gave_up[self.roam] = today
                self.roam = None
        if self.roam is None and best_now < hold * DRAINED_PER_UNIT and world.save.pilot.credits >= BROKE:
            # Nothing here is worth a hold: make for the nearest station not seen
            # lately, a never-docked one for choice, inside the pilot's nerve.
            hops = vr.bfs_hops(world.by_id, here.id)
            fresh = [(sid in self.docked, away, sid) for sid, away in hops.items()
                     if sid != here.id and away <= 4 and world.by_id[sid].danger <= self.nerve()
                     and self.docked.get(sid, -99) <= today - 20 and self.gave_up.get(sid, -99) <= today - 30]
            if fresh:
                _seen, away, self.roam = min(fresh)
                self.roam_steps = away + 2
                self.roamed += 1
        if self.roam is not None:
            toward = min(reach, key=lambda dest: (vr.bfs_hops(world.by_id, dest.id).get(self.roam, 99), dest.id))
            return toward, self.plan(toward)[1]

        def after(dest, credits: int) -> int:
            """The best leg out of `dest` that the ledger could plan: both ends docked at before."""
            if not vr.system_visited(world, dest.id):
                return 0
            onward = [world.by_id[sid] for sid in dest.connections
                      if sid != here.id and vr.system_visited(world, sid) and world.by_id[sid].danger <= self.nerve()]
            return max((self.leg(dest, far, hold, credits)[0] for far in onward), default=0)

        credits = world.save.pilot.credits
        (_profit, buys), dest = max(plans, key=lambda item: (
            item[0][0] + SECOND_LEG_WEIGHT * after(item[1], credits + item[0][0])
            + hold * 2 * min(10, today - self.docked.get(item[1].id, today - 10)), -item[1].id))
        return dest, buys

    # --- the career --------------------------------------------------------------

    def reached(self) -> list[str]:
        """The milestones beyond hull and rank that this career stands at now."""
        world, vr = self.world, self.vr
        ship, labels = world.save.ship, []
        if all(getattr(ship, f"{key}_tier") >= upgrade["max_tier"] for key, upgrade in vr.UPGRADES.items()):
            labels.append("fitted")
        if hasattr(vr, "stakes"):
            held = sum(vr.stakes(world.save).values())
            if held:
                labels.append("stake")
            if held >= vr.STAKE_MAX_STATIONS * len(vr.STAKE_COSTS):
                labels.append("all stakes")
        return labels

    def worth(self) -> int:
        world = self.world
        cargo = sum(paid for lots in world.save.cargo_basis.values() for _units, paid in lots)
        # Goods paid for and not yet collected, on a build that still sells futures.
        cargo += sum(order.principal for order in getattr(world.save, "active_futures", []))
        sunk = sum(self.spent[what] for what in ("upgrade", "refit", "stake"))
        return world.save.pilot.credits + cargo + sunk

    def fly(self, days: int) -> dict:
        world, vr = self.world, self.vr
        marks, firsts = {}, {}
        outcome = "flew"
        while world.save.turn < days:
            day = world.save.turn
            self.sell_hold()
            while hasattr(vr, "dock_work_open") and vr.dock_work_open(world) and world.save.turn < days:
                vr.work_dock_shift(world)  # nothing to trade with: a day on Freeport's docks
                self.shifts += 1
                self.earned["shifts"] += vr.DOCK_WORK_PAY
                world.commit()
            self.maintain()
            self.join()
            self.invest()
            self.order_futures()
            self.drop_lost_causes()
            self.take_contracts()
            dest, buys = self.next_stop()
            if dest is None and vr.is_stranded(world):
                vr.rescue_stranded_pilot(world)  # the station menu's own tug
                self.towed += 1
                world.commit()
                dest, buys = self.next_stop()
            if dest is None:
                outcome = "stranded"
                break
            for commodity, quantity in buys:
                self.trade(commodity, quantity, buying=True)
            world.commit()
            self.docked[world.here.id] = world.save.turn
            credits, self.keys = world.save.pilot.credits, 0
            aboard = Counter(world.save.cargo)
            open_orders = {order.id: order for order in getattr(world.save, "active_futures", [])}
            self.keep = Counter()
            try:
                with contextlib.redirect_stdout(self.screen):
                    vr.screen_travel(self.palette, world, dest.id)
            except Stuck as stuck:
                outcome = f"stuck: {str(stuck)[-160:]!r}"
                break
            self.earned["journeys"] += world.save.pilot.credits - credits
            self.lost += bool(world.ship_destroyed_this_hop)
            # An order collected on arrival was bought at an old price to be
            # carried on: it is the next leg's cargo, not something to sell here.
            still = {order.id for order in getattr(world.save, "active_futures", [])}
            for order in (order for key, order in open_orders.items() if key not in still):
                gained = world.save.cargo.get(order.commodity, 0) - aboard[order.commodity]
                if gained > 0:
                    self.keep[order.commodity] += min(gained, order.quantity)
                    self.orders["collected"] += 1
                    today = vr.price_for(world, world.here.id, order.commodity) * order.quantity
                    self.orders["saved"] += today - order.locked_price
            rank = vr.career_rank_index(world.save.pilot)
            for label in (f"rank {rank}", world.save.ship.hull_class, *self.reached()):
                firsts.setdefault(label, world.save.turn)
            for faction, joined_on in self.joined.items():
                firsts.setdefault("Concord" if faction == vr.FACTION_CONCORD else "Blackwake", joined_on)
            for mark in CHECKPOINTS:
                if day < mark <= world.save.turn:
                    marks[mark] = {"credits": world.save.pilot.credits, "worth": self.worth()}
        save = world.save
        return {
            "outcome": outcome, "days": save.turn, "marks": marks, "firsts": firsts, "lost": self.lost, "towed": self.towed,
            "shifts": self.shifts, "prizes": dict(self.prizes), "roamed": self.roamed, "orders": dict(self.orders),
            "standing": dict(save.pilot.reputation),
            "kills": save.pilot.kills, "contracts": save.pilot.missions_completed,
            "failed": save.pilot.missions_failed + save.pilot.missions_expired,
            "charted": sum(1 for system in world.galaxy if system.discovered),
            "stakes": sum(vr.stakes(save).values()) if hasattr(vr, "stakes") else 0,
            "dispatches": vr.dispatch_record(save)["total"] if hasattr(vr, "dispatch_record") else 0,
            "earned": dict(self.earned), "spent": dict(self.spent), "fights": dict(self.fights),
            "hull": save.ship.hull_class, "rank": vr.career_rank_index(save.pilot),
        }


def fly_one(job: tuple) -> dict:
    door, kind, seed, days, settings, dispatch, stakes, *more = job
    prize = more[0] if more else "S"
    jumps_per_day = more[1] if len(more) > 1 else JUMPS_PER_REAL_DAY
    ways = more[2] if len(more) > 2 else {}
    vr = load_door(door)
    apply_settings(vr, settings)
    result = Pilot(vr, kind, seed, dispatch=dispatch, stakes=stakes, prize=prize, jumps_per_day=jumps_per_day,
                   factions=bool(ways.get("factions")), futures=bool(ways.get("futures")),
                   nerve=ways.get("nerve"), charter=ways.get("charter") or "standard").fly(days)
    result.update(pilot=kind, seed=seed, jumps_per_day=jumps_per_day)
    return result


def median(values) -> float:
    values = [value for value in values if value is not None]
    return statistics.median(values) if values else float("nan")


def milestone_rows(results: list[dict]) -> list[str]:
    """When each milestone is first reached: the median day among the careers
    that reached it, and how many did. A pacing question is asked in these."""
    reached = [name for name in MILESTONES if any(name in r["firsts"] for r in results)]
    if not reached:
        return []
    lines = [f"{'pilot':<11}" + "".join(f"{name:>13}" for name in reached)]
    for kind in PILOTS:
        flown = [r for r in results if r["pilot"] == kind]
        if not flown:
            continue
        row = f"{kind:<11}"
        for name in reached:
            days = [r["firsts"][name] for r in flown if name in r["firsts"]]
            row += f"{f'{statistics.median(days):.0f} ({len(days)})' if days else '-':>13}"
        lines.append(row)
    return lines


def summarize(results: list[dict], days: int) -> list[str]:
    lines = []
    marks = [mark for mark in CHECKPOINTS if mark <= days]
    head = f"{'pilot':<11}" + "".join(f"{'day ' + str(mark):>10}" for mark in marks)
    lines.append(head + f"{'rank':>6}{'ships':>7}{'kills':>7}{'jobs':>6}{'chart':>7}{'stake':>7}{'disp':>6}  hulls at the end")
    for kind in PILOTS:
        flown = [r for r in results if r["pilot"] == kind]
        if not flown:
            continue
        row = f"{kind:<11}" + "".join(
            f"{median(r['marks'].get(mark, {}).get('worth') for r in flown):>10,.0f}" for mark in marks)
        row += (f"{median(r['rank'] for r in flown):>6.0f}{statistics.mean(r['lost'] for r in flown):>7.1f}"
                f"{median(r['kills'] for r in flown):>7.0f}{median(r['contracts'] for r in flown):>6.0f}"
                f"{median(r['charted'] for r in flown):>7.0f}{median(r['stakes'] for r in flown):>7.0f}"
                f"{median(r['dispatches'] for r in flown):>6.0f}  ")
        row += " ".join(f"{hull} {count}" for hull, count in Counter(r["hull"] for r in flown).most_common())
        lines.append(row)
        ended = Counter(r["outcome"].split(":")[0] for r in flown if r["outcome"] != "flew")
        if ended:
            lines.append(f"{'':<11}did not finish: " + ", ".join(f"{count} {what}" for what, count in ended.items()))
        ordered = [r["orders"] for r in flown if r.get("orders", {}).get("placed")]
        if ordered:
            lines.append(f"{'':<11}futures: {sum(o['placed'] for o in ordered)} orders in {len(ordered)} of {len(flown)} "
                         f"careers, {sum(o.get('collected', 0) for o in ordered)} collected, "
                         f"{sum(o.get('saved', 0) for o in ordered):,}cr under the day's price in all")
        worked = [r["shifts"] for r in flown if r.get("shifts")]
        if worked:
            lines.append(f"{'':<11}dock work: {len(worked)} of {len(flown)} careers, {sum(worked)} shifts in all, "
                         f"{max(worked)} at most")
    lines.append("")
    lines.append("Net worth (credits, cargo at cost, and what went into upgrades, hulls and stakes), median by day.")
    lines.append("ships: lost per career, mean.  jobs: contracts completed.  chart: systems charted.  disp: dispatches flown.")
    milestones = milestone_rows(results)
    if milestones:
        jumps = results[0].get("jumps_per_day", JUMPS_PER_REAL_DAY)
        lines.append("")
        lines += milestones
        lines.append("")
        lines.append("The day each is first reached, median, and (how many careers reached it). fitted: every upgrade bought.")
        lines.append(f"A day is one jump. At {jumps} jumps a real day these {days} days are {days / jumps:.1f} real days.")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--door", default=str(ROOT / "voidrunner.py"))
    parser.add_argument("--pilots", nargs="+", choices=PILOTS, default=list(PILOTS))
    parser.add_argument("--seeds", type=int, default=12, help="careers per pilot")
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                        help="a module constant to change first, as JSON (repeatable)")
    parser.add_argument("--no-dispatch", action="store_true")
    parser.add_argument("--no-stakes", action="store_true")
    parser.add_argument("--prize", choices=("S", "C", "H", "L"), default="S",
                        help="what the pilots do with a beaten raider: strip it (the default), take its "
                             "cargo, hand it to the Navy or let it go")
    parser.add_argument("--factions", action="store_true",
                        help="the pilots work toward a faction, join it and take its work")
    parser.add_argument("--futures", action="store_true",
                        help="with --door and a build that still has a futures exchange: the pilots use it")
    parser.add_argument("--nerve", type=int, choices=range(0, 6), metavar="0-5",
                        help="the most dangerous harbor every pilot flies to by choice, whatever its ship: "
                             "2 is a cautious caller, 5 a bold one; left out, a pilot's nerve follows its ship")
    parser.add_argument("--charter", choices=("standard", "hard"), default="standard",
                        help="the charter every career is flown on")
    parser.add_argument("--jumps-per-day", type=int, default=JUMPS_PER_REAL_DAY,
                        help="jumps flown in one real day: it turns the dispatch over, and the summary's "
                             "days into real days")
    parser.add_argument("--json", type=pathlib.Path, help="also write every career's record here")
    parser.add_argument("--jobs", type=int, default=max(1, multiprocessing.cpu_count() - 1))
    args = parser.parse_args()
    settings = dict(item.split("=", 1) for item in args.set)
    jobs = [(args.door, kind, 1000 + seed, args.days, settings, not args.no_dispatch, not args.no_stakes, args.prize,
             args.jumps_per_day, {"factions": args.factions, "futures": args.futures, "nerve": args.nerve,
                                  "charter": args.charter})
            for kind in args.pilots for seed in range(args.seeds)]
    if args.jobs > 1:
        with multiprocessing.Pool(args.jobs) as pool:
            results = pool.map(fly_one, jobs)
    else:
        results = [fly_one(job) for job in jobs]
    print("\n".join(summarize(results, args.days)))
    if args.json:
        args.json.write_text(json.dumps(results, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
