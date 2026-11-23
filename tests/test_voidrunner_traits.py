"""A system's own market: standing surpluses and shortages (review).

Forty-eight systems were five price tables. These pin that a trait is drawn
from the seed and the system alone and saves nothing, that it is only ever a
second source or a second buyer and never beats the trade an economy defines,
that it moves both sides of the counter by what it says, and that a pilot
learns it by docking and not before.
"""

from __future__ import annotations

import collections
import contextlib
import io
import json

import pytest

from .support import _set_cargo, _world_with_seed, plain, vr


def _trait_world(kind: str, seed_from: int = 1):
    """A world and a system in it with a trait of `kind`."""
    for seed in range(seed_from, seed_from + 50):
        world = _world_with_seed(seed)
        for system_id, trait in sorted(world.traits.items()):
            if trait["kind"] == kind:
                return world, world.by_id[system_id], trait
    raise AssertionError(f"no {kind} in 50 galaxies")


# --- what a trait is ---------------------------------------------------------------

def test_traits_come_from_the_seed_alone_and_leave_the_galaxy_and_the_save_alone():
    world = _world_with_seed(42)
    assert vr.system_traits(42, world.galaxy) == world.traits == vr.system_traits(42, vr.generate_galaxy(42))
    assert world.traits != _world_with_seed(43).traits
    assert 0 not in world.traits  # every career starts from the same plain Freeport
    # Nothing is saved for it, and the galaxy is the one its seed always made.
    assert "traits" not in world.save.to_dict() and world.save.galaxy_version == 1
    before = [(s.id, s.name, s.x, s.y, s.economy, s.danger, tuple(s.connections)) for s in vr.generate_galaxy(42)]
    vr.system_traits(42, vr.generate_galaxy(42))
    assert before == [(s.id, s.name, s.x, s.y, s.economy, s.danger, tuple(s.connections)) for s in vr.generate_galaxy(42)]


def test_about_three_systems_in_five_have_one_and_it_is_never_the_economys_own_trade():
    kinds = collections.Counter()
    for seed in range(60):
        world = _world_with_seed(seed)
        for system_id, trait in world.traits.items():
            economy = world.by_id[system_id].economy
            commodity = trait["commodity"]
            assert vr.COMMODITIES[commodity]["legal"]
            assert commodity not in vr.ECONOMY_PRODUCES[economy] and commodity not in vr.ECONOMY_DEMANDS[economy]
            kinds[trait["kind"]] += 1
        kinds["none"] += len(world.galaxy) - 1 - len(world.traits)
    total = sum(kinds.values())
    assert 0.25 <= kinds["surplus"] / total <= 0.35 and 0.25 <= kinds["shortage"] / total <= 0.35
    assert set(kinds) == {"surplus", "shortage", "none"}


@pytest.mark.parametrize("kind", ["surplus", "shortage"])
def test_a_trait_moves_both_sides_of_the_counter_and_only_its_own_good(kind):
    world, system, trait = _trait_world(kind)
    commodity = trait["commodity"]
    base = vr.COMMODITIES[commodity]["base"]
    assert vr.price_for(world, system.id, commodity) == round(base * vr.TRAIT_PRICE[kind])
    assert vr.sell_price_for(world, system.id, commodity) == round(round(base * vr.TRAIT_PRICE[kind]) * vr.SELL_SPREAD)
    plain_twin = next(s for s in world.galaxy if s.economy == system.economy and s.id not in world.traits)
    for other in vr.LEGAL_COMMODITIES:
        if other != commodity:
            assert vr.price_for(world, system.id, other) == vr.price_for(world, plain_twin.id, other)
    assert (vr.price_for(world, system.id, commodity) < base) == (kind == "surplus")


def test_a_trait_is_a_second_source_or_buyer_and_never_the_best_deal_in_the_galaxy():
    for commodity in vr.LEGAL_COMMODITIES:
        base = vr.COMMODITIES[commodity]["base"]
        ladder = vr.price_ladder(commodity)
        produced = any(commodity in goods for goods in vr.ECONOMY_PRODUCES.values())
        demanded = any(commodity in goods for economy, goods in vr.ECONOMY_DEMANDS.items() if economy != "Haven")
        if produced:
            assert round(base * vr.TRAIT_PRICE["surplus"]) > ladder[0], commodity   # dearer than where it is made
        if demanded:
            assert round(base * vr.TRAIT_PRICE["shortage"]) < ladder[-1], commodity  # cheaper than where it is wanted
    # Every legal good has a maker and a buyer among the economies (Medicine's
    # only buyer is a Haven, which the shortage check above leaves out).
    for commodity in vr.LEGAL_COMMODITIES:
        assert any(commodity in goods for goods in vr.ECONOMY_PRODUCES.values()), commodity
        assert any(commodity in goods for goods in vr.ECONOMY_DEMANDS.values()), commodity


# --- found by docking ---------------------------------------------------------------

def test_a_market_is_a_question_until_the_pilot_has_docked_there():
    world, system, trait = _trait_world("surplus")
    system.discovered = True  # charted or scanned: its economy and danger are known
    assert not vr.system_visited(world, system.id) and vr.known_trait(world, system.id) is None
    # A passing trader's data burst gives a quote, not a visit.
    vr._remember_market_quote(world, system.id, "food", vr.price_for(world, system.id, "food"))
    assert not vr.system_visited(world, system.id) and vr.known_trait(world, system.id) is None
    world.save.current_system = system.id
    world.commit()
    assert vr.system_visited(world, system.id) and vr.known_trait(world, system.id) == trait


def test_the_first_docking_says_what_was_found_and_later_ones_do_not():
    world, system, trait = _trait_world("shortage")
    world.save.current_system = system.id
    world.commit()
    label = vr.COMMODITIES[trait["commodity"]]["label"]
    found = [line for line in world.hop_report if line.startswith("Local market:")]
    assert found == [f"Local market: {system.name} has a standing shortage of {label}, bought about 40% over its usual price."]
    world.hop_report.clear()
    world.commit()
    assert not world.hop_report
    # A system without one says nothing.
    world = _world_with_seed(42)
    quiet = next(s for s in world.galaxy if s.id and s.id not in world.traits)
    world.save.current_system = quiet.id
    world.commit()
    assert not any(line.startswith("Local market:") for line in world.hop_report)


def test_a_career_loaded_again_remembers_where_it_has_been():
    world, system, trait = _trait_world("surplus")
    world.save.current_system = system.id
    system.discovered = True  # as arriving charts it
    world.commit()
    again = vr.World(vr.SaveData.from_dict(json.loads(json.dumps(world.save.to_dict()))))
    assert again.traits == world.traits and vr.known_trait(again, system.id) == trait


# --- on screen ----------------------------------------------------------------------

def test_the_market_wears_its_trait_and_the_scale_reads_it(terminal):
    terminal(80, 24)
    for kind, word in (("surplus", "cheap"), ("shortage", "dear")):
        world, system, trait = _trait_world(kind)
        world.save.current_system = system.id
        label = vr.COMMODITIES[trait["commodity"]]["label"]
        goods = list(vr.LEGAL_COMMODITIES)
        rows = [plain(row) for row in vr.market_catalog_lines(world, goods)]
        assert f"{label.upper()} {word.upper()}" in rows[0]
        assert vr.trait_label(trait) == f"{label} {word}"
        row = next(r for r in rows if label in r and r.startswith("["))
        assert row.split()[-2] == word  # the scale's word, then the season's mark after it


@pytest.mark.parametrize("width,height", [(80, 24), (40, 12)])
def test_the_chart_names_a_visited_markets_trait_and_asks_about_the_rest(terminal, width, height):
    terminal(width, height)
    world, system, trait = _trait_world("surplus")
    neighbour = world.by_id[system.connections[0]]
    world.save.current_system = neighbour.id
    system.discovered = neighbour.discovered = True
    entries = dict(vr.chart_entries(world))
    if width == 80:
        assert "?" in plain(entries[system.id]).split()      # charted, never docked
    world.save.current_system = system.id
    world.commit()
    world.save.current_system = neighbour.id
    world.commit()
    row = " ".join(plain(dict(vr.chart_entries(world))[system.id]).split())
    heading = next(plain(text) for key, text in vr.chart_entries(world) if key is None and "ECONOMY" in plain(text))
    if width == 80:
        assert vr.trait_label(trait) in row and "MARKET" in heading
    else:
        # Forty columns keeps each destination to two rows and gives the column
        # up; the trait is in the Ledger's remembered markets.
        assert len(plain(dict(vr.chart_entries(world))[system.id]).splitlines()) == 2
        assert any(vr.trait_label(trait) in plain(line) for line in vr.remembered_market_lines(world))
    for page in vr._service_pages([text for _, text in vr.chart_entries(world)], "Navigation: Fuel 24/24", "[B] Back: "):
        assert len(page) + 3 <= height


def test_the_remembered_market_and_the_viewport_carry_it(terminal):
    terminal(80, 24)
    world, system, trait = _trait_world("shortage")
    world.save.current_system = system.id
    world.commit()
    headings = [plain(row) for row in vr.remembered_market_lines(world) if system.name in plain(row)]
    assert headings and vr.trait_label(trait) in headings[0]
    large, compact, details, title = vr.viewport_content(world, "2")
    assert any(vr.trait_label(trait) in plain(row) for row in large + compact + details)


def test_selling_into_a_shortage_pays_what_it_says():
    world, system, trait = _trait_world("shortage")
    world.save.current_system = system.id
    commodity = trait["commodity"]
    _set_cargo(world, {commodity: 10}, unit_cost=1)
    before = world.save.pilot.credits
    paid = vr.sell_price_for(world, system.id, commodity)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.trade_cargo(world, commodity, 10, buying=False)
    assert world.save.pilot.credits == before + 10 * paid
    assert paid > round(vr.COMMODITIES[commodity]["base"] * vr.SELL_SPREAD)
