"""Options no pilot should ever have chosen, made into choices (review, v1.3).

Evade was never right with cargo aboard, the customs bribe always lost to
surrendering, contraband earned less than legal Electronics with risk on top,
Textiles had no buyer, and a stock Freighter missed the outclassed rule by two
hull points. These pin the five corrections and what each leaves alone.
"""

from __future__ import annotations

import pytest

from .support import _set_cargo, _world_with_seed, plain, vr


def _tactics(version: int = vr.TACTICAL_RULESET_VERSION) -> dict:
    return {"version": version, "profile": "Raider", "step": 0, "brace_ready": True}


# --- Evade and Dump ----------------------------------------------------------------

@pytest.mark.parametrize("held,units", [(1, 1), (8, 1), (9, 2), (24, 3), (100, 13), (200, 25)])
def test_a_dump_costs_an_eighth_of_the_hold_so_evade_is_the_free_one(held, units):
    world = _world_with_seed(42)
    world.save.ship.hull_class = "Carrier"
    world.save.ship.cargo_tier = 5
    _set_cargo(world, {"food": held})
    pirate = vr.Pirate("Probe", 1, 35, 35)
    assert vr.dump_units(world, pirate, _tactics()) == units
    assert vr.dump_units(world, pirate, _tactics(3)) == 1  # a fight begun before ruleset 4 keeps its unit
    row = next(plain(r) for r in vr.combat_display_lines(world, pirate, [], patrol=False, tactics=_tactics())
               if plain(r).startswith("[D]"))
    assert f"{vr.plural(units, 'unit')} overboard" in row
    # Dump is still the better odds, by twenty points: what it costs is the choice.
    evade = vr.combat_evade_chance(world, pirate, dumped_cargo=False, tactics=_tactics())
    dump = vr.combat_evade_chance(world, pirate, dumped_cargo=True, tactics=_tactics(), cargo_units=held - units)
    assert dump > evade


def test_an_empty_hold_has_nothing_to_dump():
    world = _world_with_seed(42)
    assert vr.dump_units(world, vr.Pirate("Probe", 1, 35, 35), _tactics()) == 0


# --- the outclassed rule -------------------------------------------------------------

def test_a_stock_freighter_is_outclassed_by_tier_four_and_a_shuttle_at_tier_three_is_not():
    world = _world_with_seed(42)
    ship = world.save.ship
    ship.hull_class = "Freighter"
    worst = vr._tactical_incoming_damage(ship, 4, "volley", 9, _tactics())
    assert (worst * 2, vr.hull_hp_max(ship)) == (138, 140)   # two points the wrong side of the old rule
    assert vr.raider_outclasses(ship, 4) and not vr.raider_outclasses(ship, 4, 3)
    assert not vr.raider_outclasses(ship, 3)
    ship.hull_tier = 1                                        # 175 hull: it can take two and fight on
    assert not vr.raider_outclasses(ship, 4)
    ship.hull_class, ship.hull_tier = "Shuttle", 0
    assert vr.raider_outclasses(ship, 4) and not vr.raider_outclasses(ship, 3)


# --- the customs bribe ----------------------------------------------------------------

def _customs(units: int, accepted: bool):
    world = _world_with_seed(42)
    world.save.pilot.credits = 50_000
    _set_cargo(world, {"narcotics": units})
    world.event_rng.random = lambda: 0.0 if accepted else 0.99
    return world


def test_the_bribe_is_taken_three_times_in_four_and_says_so(terminal):
    terminal(80, 24)
    assert vr.CUSTOMS_BRIBE_CHANCE == 0.75
    world = _customs(4, True)
    assert "75% acceptance" in " ".join(plain(row) for row in vr.customs_display_lines(world))
    for roll, taken in ((0.74, True), (0.75, False)):
        world = _customs(4, True)
        world.event_rng.random = lambda roll=roll: roll
        vr.resolve_customs(world, "P")
        assert bool(world.save.cargo.get("narcotics")) == taken


@pytest.mark.parametrize("units,bribe_wins", [(2, False), (4, False), (5, True), (20, True)])
def test_the_bribe_beats_surrendering_a_big_hold_and_loses_on_a_small_one(units, bribe_wins):
    """Expected credits, counting the cargo at what customs values it."""
    value = units * vr.COMMODITIES["narcotics"]["base"]
    outcomes = []
    for accepted in (True, False):
        world = _customs(units, accepted)
        before = world.save.pilot.credits
        vr.resolve_customs(world, "P")
        kept = value if world.save.cargo.get("narcotics") else 0
        outcomes.append(world.save.pilot.credits - before + kept - value)
    bribe = vr.CUSTOMS_BRIBE_CHANCE * outcomes[0] + (1 - vr.CUSTOMS_BRIBE_CHANCE) * outcomes[1]
    assert (bribe > -value) == bribe_wins, (bribe, -value)


# --- contraband and Textiles ------------------------------------------------------------

def test_contraband_fetches_half_as_much_again_away_from_a_haven():
    for commodity in vr.CONTRABAND_COMMODITIES:
        base = vr.COMMODITIES[commodity]["base"]
        assert vr.economy_price_factor("Haven", commodity) == pytest.approx(0.6)   # where it is made: unchanged
        for economy in vr.ECONOMIES:
            if economy != "Haven":
                assert vr.economy_price_factor(economy, commodity) == vr.CONTRABAND_STREET_PREMIUM
        assert vr.price_ladder(commodity) == sorted([round(base * 0.6)] + [round(base * 1.5)] * 4)
    for commodity in vr.LEGAL_COMMODITIES:
        for economy in vr.ECONOMIES:
            assert vr.economy_price_factor(economy, commodity) in (0.6, 1.0, 1.6)


def test_smuggling_is_the_best_margin_in_the_galaxy_and_legal_goods_are_what_they_were():
    def margin(commodity: str) -> int:
        ladder = vr.price_ladder(commodity)
        return round(ladder[-1] * vr.SELL_SPREAD) - ladder[0]
    legal = max(margin(commodity) for commodity in vr.LEGAL_COMMODITIES)
    assert legal == 96  # Electronics, as before
    assert margin("narcotics") == 156 and margin("weapons") == 109
    assert min(margin(commodity) for commodity in vr.CONTRABAND_COMMODITIES) > legal


def test_a_quote_and_the_ladder_it_is_judged_against_use_one_rule():
    world = _world_with_seed(42)
    for system in world.galaxy:
        if system.id in world.traits:
            continue
        for commodity in vr.COMMODITIES:
            assert vr.price_for(world, system.id, commodity) in vr.price_ladder(commodity)


def test_textiles_have_a_buyer():
    assert "textiles" in vr.ECONOMY_DEMANDS["Tech"] and "textiles" in vr.ECONOMY_PRODUCES["Agricultural"]
    assert vr.price_ladder("textiles") == [11, 18, 18, 18, 29]
    assert ("textiles", "Tech") in vr._dispatch_hauls()
    world = _world_with_seed(42)
    tech = next(system for system in world.galaxy if system.economy == "Tech" and system.id not in world.traits)
    assert vr.market_depth_quote(world, tech.id, "textiles")["demand"] == 96  # a wanted good's deeper pool
