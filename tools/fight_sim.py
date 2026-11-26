#!/usr/bin/env python3
"""Fly fights with the door's own functions and say how they go.

    python tools/fight_sim.py                      # every ship against tiers 3 to 5
    python tools/fight_sim.py --fights 900 --tiers 4 5 --ships finished
    python tools/fight_sim.py --set ELITE_HULL=30  # try a figure before changing it

A career simulator cannot say whether a fight is a fight: its pilots meet a
few dozen raiders in a hundred days, mostly weak ones. This one stages the
fight itself -- a ship, a crew, one raider of a tier, hull full at the start
-- and flies it some hundreds of times with a sensible pilot at the keys:

- slip the move when a volley is likelier than not;
- mend at half hull while the contact is not about to volley, or at a third
  whatever it is doing;
- call the shot into a recovery, or when only a called shot finishes it;
- guard when a volley is at least one chance in four and the guard is ready;
- otherwise fire.

It prints, for each ship, tier and crew, how many fights are won, the hull a
win costs and how many turns a fight takes. Nothing here is the game's rule:
`tactical_round` and its neighbours decide every exchange.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys

DOOR = pathlib.Path(__file__).resolve().parent.parent / "voidrunner.py"
#: A raider of each pattern, by a name that draws it.
PATTERN_NAMES = {"Raider": "Hollow Fang", "Bulwark": "Grimwire", "Skirmisher": "Rust Wraith"}
CREWS = {"none": (), "gunner": ("gunner",), "engineer": ("engineer",), "navigator": ("navigator",),
         "all": ("gunner", "engineer", "navigator")}
TURN_LIMIT = 80


def load_door(path: pathlib.Path = DOOR):
    spec = importlib.util.spec_from_file_location("voidrunner_fight_sim", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def ships(vr) -> dict[str, dict]:
    """The ships fights are flown in: where a career starts, where it is in its
    middle, and a finished one -- the biggest hull with every module at its top."""
    finished = {f"{key}_tier": upgrade["max_tier"] for key, upgrade in vr.UPGRADES.items()}
    fleet = {"starter": {"hull_class": "Shuttle"},
             "mid": {"hull_class": "Freighter", "weapon_tier": 2, "shield_tier": 1, "hull_tier": 2},
             "finished": {"hull_class": "Carrier", **finished}}
    # The other flagships, finished the same way, where the door has them.
    for hull in getattr(vr, "FLAGSHIPS", ()):
        if hull != "Carrier":
            fleet[hull.lower()] = {"hull_class": hull, **finished}
    return fleet


def volley_chance(vr, world, pirate, tactics) -> float:
    """How likely the contact's next move is a volley, as the panel lets a pilot work out."""
    read = vr.tactical_read(world.save.ship, pirate.tier, tactics)
    shown, jump = vr.tactical_intent(tactics), vr.tactical_jump(tactics)
    return (read if shown == "volley" else 0.0) + ((1 - read) if jump == "volley" else 0.0)


def staged(vr, fittings: dict, crew: tuple[str, ...]):
    """A world whose ship is fitted and crewed as asked."""
    save = vr._new_career("Sim")
    save.seed = 1
    world = vr.World(save)
    for name, value in fittings.items():
        setattr(world.save.ship, name, value)
    for role in crew:
        setattr(world.save.ship, f"has_{role}", True)
    return world


def fight(vr, world, name: str, tier: int, seed: int) -> tuple[bool, int, int]:
    """One fight to its end: (won, hull lost, turns)."""
    world.event_rng.seed(seed)
    ship = world.save.ship
    ship.hull_hp = start = vr.hull_hp_max(ship)
    hull = vr.raider_hull(tier) if hasattr(vr, "raider_hull") else 20 + 15 * tier
    pirate = vr.Pirate(name, tier, hull, hull)
    tactics = vr.new_tactics(pirate)
    turns = 0
    while pirate.hp > 0 and ship.hull_hp > 0 and turns < TURN_LIMIT:
        turns += 1
        stations = vr.crew_stations(world, pirate, tactics)
        shown = vr.tactical_intent(tactics)
        finishes = vr._tactical_outgoing_damage(ship, shown, 9) >= pirate.hp
        volley = volley_chance(vr, world, pirate, tactics)
        if "N" in stations and not finishes and volley >= 0.5:
            vr.tactical_round(world, pirate, tactics, "N")
            continue
        half, third = ship.hull_hp * 2 <= start, ship.hull_hp * 3 <= start
        if "M" in stations and not finishes and (half and shown != "volley" or third):
            vr.tactical_mend(world, pirate, tactics)
            continue
        called = vr._tactical_outgoing_damage(ship, shown, 9, called=True)
        if "C" in stations and (shown == "recover" or called >= pirate.hp > vr._tactical_outgoing_damage(ship, shown, 9)):
            vr.tactical_round(world, pirate, tactics, "C")
            continue
        guard = tactics["brace_ready"] and not finishes and volley >= 0.25
        vr.tactical_round(world, pirate, tactics, "G" if guard else "F")
    return pirate.hp <= 0 and ship.hull_hp > 0, start - ship.hull_hp, turns


def names_for(vr, tier: int) -> list[str]:
    """The raiders a tier is flown by: one of each pattern, or an elite."""
    if tier >= getattr(vr, "ELITE_TIER", 99):
        return [vr.ELITE_NAMES[0]]
    return list(PATTERN_NAMES.values())


def run(vr, ship: str, crew: str, tier: int, fights: int) -> dict:
    world = staged(vr, ships(vr)[ship], CREWS[crew])
    names = names_for(vr, tier)
    each = max(1, fights // len(names))
    results = [fight(vr, world, name, tier, seed) for name in names for seed in range(each)]
    won = [hull for ok, hull, _turns in results if ok]
    start = vr.hull_hp_max(world.save.ship)
    return {"ship": ship, "crew": crew, "tier": tier, "fights": len(results), "won": len(won),
            "hull": start, "cost": sum(won) / len(won) if won else 0.0,
            "turns": sum(turns for _ok, _hull, turns in results) / len(results),
            "outclassed": vr.raider_outclasses(world.save.ship, tier)}


def table(rows: list[dict]) -> list[str]:
    lines = [f"{'ship':9} {'crew':10} {'tier':>4}  {'won':>5}  {'hull a win costs':>17}  {'turns':>5}  note"]
    for row in rows:
        share = row["won"] / row["fights"]
        cost = f"{row['cost']:.0f} of {row['hull']} ({row['cost'] / row['hull']:.0%})" if row["won"] else "-"
        lines.append(f"{row['ship']:9} {row['crew']:10} {row['tier']:>4}  {share:>5.0%}  {cost:>17}  {row['turns']:>5.1f}  "
                     + ("outclassed" if row["outclassed"] else ""))
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--door", type=pathlib.Path, default=DOOR)
    parser.add_argument("--fights", type=int, default=600, help="fights flown for each row")
    parser.add_argument("--tiers", type=int, nargs="+", default=[3, 4, 5])
    parser.add_argument("--ships", nargs="+", default=["starter", "mid", "finished"],
                        help="starter, mid, finished (a Carrier), argosy, frigate")
    parser.add_argument("--crews", nargs="+", default=["none", "all"], choices=list(CREWS))
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                        help="override one of the door's figures for this run, e.g. ELITE_HULL=30")
    args = parser.parse_args()
    vr = load_door(args.door)
    for item in args.set:
        name, value = item.split("=", 1)
        setattr(vr, name, type(getattr(vr, name))(value))
    most = getattr(vr, "RAIDER_TIER_MOST", 4)
    rows = [run(vr, ship, crew, tier, args.fights)
            for ship in args.ships for tier in args.tiers if tier <= most for crew in args.crews]
    print("\n".join(table(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
