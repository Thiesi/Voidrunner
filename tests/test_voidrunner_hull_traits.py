"""A trait for each hull line (review).

A hull was three numbers, and a hold past what a station stocks could not be
filled. These pin what each line now does that the others cannot: a hauler is
dealt more of a station's stock and demand, a warship's crew can be called on
a second time in a fight; that the Shuttle and the Carrier do neither; what a
save may hold; and where a pilot is told.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from .support import _world_with_pending_fight, _world_with_seed, plain, vr


def _in(hull, seed=42):
    world = _world_with_seed(seed)
    ship = world.save.ship
    ship.hull_class = hull
    ship.hull_hp = vr.hull_hp_max(ship)
    world.save.pilot.credits = 500_000
    return world


def _crewed(hull):
    world = _in(hull)
    ship = world.save.ship
    ship.has_gunner = ship.has_engineer = ship.has_navigator = True
    ship.hull_hp = vr.hull_hp_max(ship) - 40      # something for the engineer to mend
    pirate = vr.Pirate("Hollow Fang", 3, 400, 400)
    return world, pirate, vr.new_tactics(pirate)


# --- which hull has what --------------------------------------------------------

def test_each_line_has_one_trait_and_the_shuttle_and_the_carrier_have_none():
    assert vr.HULL_REACH == {"Freighter": 110, "Argosy": 125} and vr.HULL_ENCORES == {"Cutter": 1, "Frigate": 2}
    assert set(vr.HULL_TRAITS) == set(vr.HULL_REACH) | set(vr.HULL_ENCORES)
    ship = vr.starting_ship()
    for hull in vr.HULL_CLASSES:
        ship.hull_class = hull
        assert vr.hull_reach(ship) == vr.HULL_REACH.get(hull, 100) and vr.station_encores(ship) == vr.HULL_ENCORES.get(hull, 0)
        assert (vr.hull_reach(ship) > 100) + (vr.station_encores(ship) > 0) == (hull in vr.HULL_TRAITS)
    assert all(text.startswith("Hauler: ") for hull, text in vr.HULL_TRAITS.items() if hull in vr.HULL_REACH)
    assert all(text.startswith("Warship: ") for hull, text in vr.HULL_TRAITS.items() if hull in vr.HULL_ENCORES)
    assert vr.HULL_REACH_MOST == 125


# --- the hauler's reach -----------------------------------------------------------

def test_a_hauler_is_dealt_a_deeper_pool_that_refills_at_the_stations_pace():
    public = vr.market_depth_limits("Industrial", "machinery")
    assert public == vr.market_depth_limits("Industrial", "machinery", 100)
    assert public == {"stock": 96, "demand": 48, "stock_rate": 6, "demand_rate": 3}   # what every ship was dealt
    freighter, argosy = (vr.market_depth_limits("Industrial", "machinery", reach) for reach in (110, 125))
    assert (freighter["stock"], freighter["demand"]) == (105, 52) and (argosy["stock"], argosy["demand"]) == (120, 60)
    assert all(limits["stock_rate"] == 6 and limits["demand_rate"] == 3 for limits in (freighter, argosy))


@pytest.mark.parametrize("hull,stock", [("Shuttle", 96), ("Cutter", 96), ("Frigate", 96), ("Carrier", 96),
                                        ("Freighter", 105), ("Argosy", 120)])
def test_the_stock_a_station_shows_and_sells_is_the_hulls(hull, stock):
    world = _in(hull)
    made = next(c for c in vr.LEGAL_COMMODITIES if c in vr.ECONOMY_PRODUCES[world.here.economy])
    quote = vr.market_depth_quote(world, world.here.id, made)
    assert quote["stock"] == stock and quote["stock_rate"] == 6
    room = vr.cargo_capacity(world.save.ship)
    if room > stock:
        vr.trade_cargo(world, made, stock, buying=True)                 # the whole pool, in one purchase
        assert vr.market_depth_quote(world, world.here.id, made)["stock"] == 0
        with pytest.raises(vr.TradeError, match="Only 0 units in stock"):
            vr.trade_cargo(world, made, 1, buying=True)
        world.save.turn += 2
        assert vr.market_depth_quote(world, world.here.id, made)["stock"] == 12  # six a day, hauler or not
        world.save.turn += 40
        assert vr.market_depth_quote(world, world.here.id, made)["stock"] == stock  # and back to its own ceiling


def test_a_freighter_that_becomes_a_carrier_gives_the_reach_up_and_its_save_still_loads():
    world = _in("Argosy")
    made = next(c for c in vr.LEGAL_COMMODITIES if c in vr.ECONOMY_PRODUCES[world.here.economy])
    vr.trade_cargo(world, made, 1, buying=True)
    assert world.save.market_depth[world.here.id][made]["stock"] == 119      # a pool only a hauler has
    world.save.ship.hull_class = "Carrier"
    vr.SaveData.from_dict(world.save.to_dict())                              # still a save this door reads
    assert vr.market_depth_quote(world, world.here.id, made)["stock"] == 96  # and the Carrier is dealt the floor
    with pytest.raises(vr.TradeError, match="Only 96 units in stock"):
        vr.trade_cargo(world, made, 97, buying=True)


def test_the_market_says_why_its_figures_are_larger(terminal):
    terminal(80, 24)
    for hull, badge in (("Argosy", "HAULER x1.25"), ("Freighter", "HAULER x1.1"), ("Carrier", None), ("Shuttle", None)):
        world = _in(hull)
        head = plain(vr.market_catalog_lines(world, list(vr.LEGAL_COMMODITIES))[0])
        assert (badge in head) if badge else ("HAULER" not in head)


# --- the warship's second call ------------------------------------------------------

def test_a_cutter_calls_on_one_station_twice_and_a_frigate_on_two():
    world, pirate, tactics = _crewed("Cutter")
    ship = world.save.ship
    assert tactics["version"] == vr.ENCORE_RULESET_VERSION == vr.TACTICAL_RULESET_VERSION == 7
    assert vr.crew_stations(world, pirate, tactics) == ["C", "M", "N"] and vr.encores_left(ship, tactics) == 1
    assert [vr.station_calls_left(ship, tactics, key) for key in "CMN"] == [2, 2, 2]   # any one of them, twice
    tactics["spent"] = "C"
    assert vr.crew_stations(world, pirate, tactics) == ["C", "M", "N"]
    assert [vr.station_calls_left(ship, tactics, key) for key in "CMN"] == [1, 2, 2]
    tactics["spent"] = "CC"                                            # the second call is made
    assert vr.crew_stations(world, pirate, tactics) == ["M", "N"] and vr.encores_left(ship, tactics) == 0
    assert [vr.station_calls_left(ship, tactics, key) for key in "CMN"] == [0, 1, 1]   # the others once, as ever
    tactics["spent"] = "CCMN"
    assert vr.crew_stations(world, pirate, tactics) == []
    world, pirate, tactics = _crewed("Frigate")
    tactics["spent"] = "CCNN"
    assert vr.crew_stations(world, pirate, tactics) == ["M"] and vr.encores_left(world.save.ship, tactics) == 0
    assert vr.station_calls_left(world.save.ship, tactics, "M") == 1  # the third station has no second call


@pytest.mark.parametrize("hull", ["Shuttle", "Freighter", "Carrier", "Argosy"])
def test_a_ship_that_is_not_a_warship_calls_on_each_station_once(hull):
    world, pirate, tactics = _crewed(hull)
    assert [vr.station_calls_left(world.save.ship, tactics, key) for key in "CMN"] == [1, 1, 1]
    tactics["spent"] = "C"
    assert vr.crew_stations(world, pirate, tactics) == ["M", "N"]


def test_a_fight_begun_before_ruleset_seven_keeps_one_call_each():
    world, pirate, _tactics = _crewed("Frigate")
    older = vr.new_tactics(pirate, 6)
    assert vr.encores_left(world.save.ship, older) == 0
    older["spent"] = "C"
    assert vr.crew_stations(world, pirate, older) == ["M", "N"]


def test_the_second_call_is_a_real_one():
    world, pirate, tactics = _crewed("Frigate")
    world.event_rng.seed(7)
    before = pirate.hp
    vr.tactical_round(world, pirate, tactics, "C")
    first = before - pirate.hp
    vr.tactical_round(world, pirate, tactics, "C")
    assert tactics["spent"] == "CC" and pirate.hp < before - first      # it landed again
    assert "C" not in vr.crew_stations(world, pirate, tactics)          # and there is no third
    world.save.ship.hull_hp = vr.hull_hp_max(world.save.ship) // 2
    vr.tactical_mend(world, pirate, tactics)
    assert tactics["spent"] == "CCM" and "M" in vr.crew_stations(world, pirate, tactics)  # the Frigate's other second call
    world.save.ship.hull_hp = vr.hull_hp_max(world.save.ship) // 2
    vr.tactical_mend(world, pirate, tactics)
    assert tactics["spent"] == "CCMM" and vr.crew_stations(world, pirate, tactics) == ["N"]
    assert vr.station_calls_left(world.save.ship, tactics, "N") == 1    # two second calls, both made


def test_the_panel_says_how_often_a_station_can_still_be_called(terminal):
    terminal(80, 24)
    world, pirate, tactics = _crewed("Cutter")
    ship = world.save.ship
    assert [vr.station_calls_text(ship, tactics, key) for key in "CMN"] == ["once or twice"] * 3
    rows = " ".join(" ".join(plain(row).split()) for row in vr.combat_option_rows(world, pirate, patrol=False, tactics=tactics))
    assert rows.count("once or twice") == 3
    tactics["spent"] = "N"
    assert vr.station_calls_text(ship, tactics, "N") == "once more" and vr.station_calls_text(ship, tactics, "C") == "once or twice"
    tactics["spent"] = "NN"
    assert vr.station_calls_text(ship, tactics, "C") == "once"
    info = " ".join(" ".join(plain(line).split()) for line in vr.combat_display_lines(
        world, pirate, [], patrol=False, tactics=tactics, details=True))
    assert "This Cutter can call on one of them a second time." in info
    world, pirate, tactics = _crewed("Carrier")
    info = " ".join(" ".join(plain(line).split()) for line in vr.combat_display_lines(
        world, pirate, [], patrol=False, tactics=tactics, details=True))
    assert "a second time" not in info and "; once" in " ".join(
        " ".join(plain(row).split()) for row in vr.combat_option_rows(world, pirate, patrol=False, tactics=tactics))


def test_a_save_may_hold_a_station_called_on_twice_and_no_more():
    world, _pirate = _world_with_pending_fight()
    combat = world.save.pending_travel["encounter"]["combat"]
    for spent in ("", "C", "CC", "CCNN", "CNCN", "MCMC"):
        combat["tactics"] = {"version": 7, "profile": "Raider", "step": 0, "brace_ready": True, "spent": spent}
        vr.SaveData.from_dict(world.save.to_dict())
    for spent in ("CCC", "CCMMNN", "CX", 5):
        combat["tactics"] = {"version": 7, "profile": "Raider", "step": 0, "brace_ready": True, "spent": spent}
        with pytest.raises((vr.ResumeError, vr.UnsupportedSave)):
            vr.SaveData.from_dict(world.save.to_dict())
    # Under ruleset 6 and 5 a station is listed once, as it always was.
    for version in (5, 6):
        combat["tactics"] = {"version": version, "profile": "Raider", "step": 0, "brace_ready": True, "spent": "CMN"}
        vr.SaveData.from_dict(world.save.to_dict())
        combat["tactics"]["spent"] = "CC"
        with pytest.raises((vr.ResumeError, vr.UnsupportedSave)):
            vr.SaveData.from_dict(world.save.to_dict())


# --- where a pilot is told -----------------------------------------------------------

def test_the_yard_names_the_trait_of_the_hull_and_the_refit_says_what_is_given_up(monkeypatch, terminal):
    terminal(80, 24)
    for hull in vr.HULL_CLASSES:
        yard = " ".join(" ".join(plain(row).split()) for row in vr.shipyard_lines(_in(hull)))
        assert (f"This {hull}: {vr.HULL_TRAITS[hull]}" in yard) if hull in vr.HULL_TRAITS else ("This " + hull + ":" not in yard)

    def preview(hull, target):
        world = _in(hull)
        monkeypatch.setattr(vr, "read_key", lambda: "B")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            vr._hull_refit_screen(vr.Palette(False), world, target, 45_000)
        return " ".join(plain(output.getvalue()).split())

    assert vr.HULL_TRAITS["Argosy"] in preview("Freighter", "Argosy")
    assert vr.HULL_TRAITS["Frigate"] in preview("Cutter", "Frigate")
    assert "A Carrier has no trait of its own: it gives up the hauler's." in preview("Freighter", "Carrier")
    assert "A Carrier has no trait of its own: it gives up the warship's." in preview("Cutter", "Carrier")
    assert vr.HULL_TRAITS["Freighter"] in preview("Shuttle", "Freighter")
