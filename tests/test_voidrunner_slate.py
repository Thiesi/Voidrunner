"""A wide deck has a purser's slate beside the cockpit (review).

On a wide terminal the deck's panel is 112 columns and its cockpit used sixty.
These pin what now stands in the rest: what the slate says and that every
figure on it is one the market, the hold or the ledger already gives; that it
shows whole items or none; that it stands where the silhouette stood and adds
no row; and that the deck at 80 columns is the deck it was.
"""

from __future__ import annotations

import pytest

from .support import _add_cargo, _world_with_seed, plain, vr


@pytest.fixture
def terminal(monkeypatch):
    def size(width: int, height: int, style: str = "auto"):
        monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
        monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
        monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
        monkeypatch.setattr(vr, "_OUTPUT_ENCODING", "utf-8")
    return size


def _slate(world, room=48) -> dict[str, str]:
    rows = [plain(row) for row in vr.deck_slate_rows(world, room)]
    return {row.split()[0]: " ".join(row.split()[1:]) for row in rows}


def test_the_slate_prices_the_hold_here_against_what_it_cost():
    world = _world_with_seed(42)
    assert _slate(world)["CARGO"] == "hold empty"
    _add_cargo(world, "food", 10, unit_cost=5)
    _add_cargo(world, "machinery", 2, unit_cost=9_000)
    here = world.here.id
    food = vr.sell_price_for(world, here, "food") * 10 - 50
    machinery = vr.sell_price_for(world, here, "machinery") * 2 - 18_000
    assert food > 0 > machinery
    cargo = _slate(world, 70)["CARGO"]
    assert f"10 {vr.COMMODITIES['food']['label']} {food:+,}" in cargo
    assert f"2 {vr.COMMODITIES['machinery']['label']} {machinery:+,}" in cargo
    # The dearer lot is named first.
    assert cargo.index("Machinery") < cargo.index("Food")


def test_cargo_promised_to_a_contract_and_cargo_that_cannot_be_sold_say_so():
    world = _world_with_seed(42)
    _add_cargo(world, "food", 5, unit_cost=5)
    target = sorted(world.here.connections)[0]
    world.save.active_missions = [vr.Mission(id=3, kind="delivery", description="deliver", reward=400, origin_system=0,
                                             target_system=target, commodity="food", quantity=5)]
    assert _slate(world)["CARGO"] == f"5 {vr.COMMODITIES['food']['label']} for a contract"
    # More aboard than is promised: the lot has a price again.
    _add_cargo(world, "food", 8, unit_cost=5)
    assert "for a contract" not in _slate(world)["CARGO"]
    contraband = next(c for c, info in vr.COMMODITIES.items() if not info["legal"])
    assert world.here.economy != "Haven"
    _add_cargo(world, contraband, 2, unit_cost=100)
    assert f"2 {vr.COMMODITIES[contraband]['label']} no sale" in _slate(world, 80)["CARGO"]


@pytest.mark.parametrize("seed", range(40, 46))
def test_cheap_and_pays_are_the_markets_own_words_for_its_own_prices(seed):
    world = _world_with_seed(seed)
    here = world.here.id
    slate = _slate(world, 200)
    for commodity in vr.LEGAL_COMMODITIES:
        label = vr.COMMODITIES[commodity]["label"]
        quote = vr.price_for(world, here, commodity)
        word = vr.price_standing(quote, vr.price_ladder(commodity))[0]
        assert (f"{label} {quote}" in slate["CHEAP"]) == (word == "cheap")
        assert (f"{label} {vr.sell_price_for(world, here, commodity)}" in slate["PAYS"]) == (word == "dear")
    # Contraband is not on a slate where it is not sold.
    assert world.here.economy == "Haven" or not any(
        vr.COMMODITIES[c]["label"] in slate["CHEAP"] + slate["PAYS"] for c in vr.CONTRABAND_COMMODITIES)


def test_the_lead_is_the_ledgers_first_and_says_so_when_it_has_none():
    world = _world_with_seed(42)
    assert _slate(world)["LEAD"] == "none the ledger knows yet"
    # A market seen one jump out, and the ledger has a run.
    home = world.save.current_system
    target = sorted(world.here.connections)[0]
    world.by_id[target].discovered = True
    world.save.current_system = target
    vr.remember_local_market(world)  # as docking there does
    world.save.current_system = home
    leads = vr.trade_opportunities(world)
    assert leads
    best = leads[0]
    lead = _slate(world, 70)["LEAD"]
    assert lead.endswith(f"+{best['margin']:,}")
    assert f"{best['quantity']} {vr.COMMODITIES[best['commodity']]['label']} to {world.by_id[best['destination']].name}" in lead
    # Narrower, it gives up the jumps and then the quantity, never the margin.
    assert _slate(world, 36)["LEAD"].endswith(f"+{best['margin']:,}")


def test_the_leads_are_ranked_once_until_something_they_depend_on_changes(monkeypatch):
    world = _world_with_seed(42)
    calls = []
    monkeypatch.setattr(vr, "trade_opportunities", lambda w: calls.append(1) or [])
    for _ in range(3):
        vr.deck_leads(world)
    assert len(calls) == 1
    world.save.pilot.credits += 1
    vr.deck_leads(world)
    world.save.turn += 1
    vr.deck_leads(world)
    assert len(calls) == 3


def test_the_slate_shows_whole_items_and_counts_the_rest():
    items = [("Food 12", "Food 12"), ("Textiles 31", "Textiles 31"), ("Machinery 148", "Machinery 148")]
    assert vr._slate_fit(items, 60, "none") == "Food 12   Textiles 31   Machinery 148"
    assert plain(vr._slate_fit(items, 26, "none")) == "Food 12   Textiles 31   +1"
    assert plain(vr._slate_fit(items, 12, "none")) == "Food 12   +2"
    assert plain(vr._slate_fit(items, 4, "none")) == "3 here"
    assert plain(vr._slate_fit([], 30, "hold empty")) == "hold empty"


def test_a_wide_deck_has_the_slate_where_the_silhouette_stood_and_is_no_taller(terminal):
    world = _world_with_seed(42)
    terminal(80, 24)
    narrow = [plain(row) for row in vr.station_deck_lines(world)]
    assert not any("CARGO" in row or "LEAD" in row for row in narrow)
    assert any("▀" in row or "▄" in row for row in narrow)  # the hull's silhouette
    terminal(160, 48)
    wide = [plain(row) for row in vr.station_deck_lines(world)]
    assert len(wide) == len(narrow)
    width = vr._page_content_width()
    assert all(vr._visible_width(row) <= width for row in vr.station_deck_lines(world))
    labels = [row.split("│")[1].split()[0] for row in wide if "│" in row]
    assert labels == ["CARGO", "CHEAP", "PAYS", "LEAD"]
    # Beside the career row and the three gauges, one column, the same on every row.
    marks = {row.index("│") for row in wide if "│" in row}
    assert len(marks) == 1
    assert [row.split()[0] for row in wide if "│" in row] == ["Day", "HULL", "FUEL", "HOLD"]
    assert not any("▀" in row or "▄" in row for row in wide)


def test_a_full_crew_is_all_aboard_beside_the_slate_and_named_without_it(terminal):
    world = _world_with_seed(42)
    for role in vr.CREW_ROLES:
        setattr(world.save.ship, f"has_{role}", True)
    terminal(160, 48)
    crew = next(plain(row) for row in vr.station_deck_lines(world) if plain(row).startswith("CREW"))
    assert crew.endswith("all aboard")
    world.save.ship.has_engineer = False
    crew = next(plain(row) for row in vr.station_deck_lines(world) if plain(row).startswith("CREW"))
    assert crew.endswith("Gunner, Navigator")
    world.save.ship.has_engineer = True
    terminal(80, 24)
    crew = next(plain(row) for row in vr.station_deck_lines(world) if plain(row).startswith("CREW"))
    assert crew.endswith("Gunner, Engineer, Navigator")


@pytest.mark.parametrize("style,rule", [("mono", "│"), ("plain", "|")])
def test_the_slate_is_drawn_in_every_preset(terminal, style, rule):
    world = _world_with_seed(42)
    terminal(160, 48, style)
    rows = vr.station_deck_lines(world)
    slate = [row for row in rows if f"{rule}  CARGO" in plain(row)]
    assert len(slate) == 1
    if style == "plain":
        assert all(ord(ch) < 128 for row in rows for ch in plain(row))
