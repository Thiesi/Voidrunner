"""Three flagships: the Carrier, and one for each line (review).

Both lines led to the Carrier, so the choice a pilot made at the first refit
was undone at the second. These pin the two specialised flagships: what they
are worth against the Carrier, where each can be commissioned and that nothing
follows them, their pictures in every form a hull is drawn in, what a save and
an achievement make of them, and that the tools can fly them.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from pathlib import Path

import pytest

from .support import _finale_world, _world_with_seed, plain, vr

ROOT = Path(__file__).resolve().parent.parent
OLD_HULLS = ("Shuttle", "Freighter", "Cutter", "Carrier")


def _tool(name):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test_flagships", ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- what they are ----------------------------------------------------------------

def test_each_line_has_a_flagship_of_its_own_beside_the_carrier():
    assert vr.FLAGSHIPS == ("Carrier", "Argosy", "Frigate") and list(vr.HULL_CLASSES) == [*OLD_HULLS, "Argosy", "Frigate"]
    assert vr.HULL_REFITS["Freighter"] == [("Carrier", 45_000), ("Argosy", 45_000)]
    assert vr.HULL_REFITS["Cutter"] == [("Carrier", 45_000), ("Frigate", 45_000)]
    assert vr.HULL_REFITS["Shuttle"] == [("Freighter", 15_000), ("Cutter", 15_000)]
    assert all(vr.HULL_REFITS[hull] == [] for hull in vr.FLAGSHIPS) and set(vr.HULL_REFITS) == set(vr.HULL_CLASSES)


def test_a_specialised_flagship_does_one_thing_better_than_the_carrier_and_the_other_worse():
    carrier, argosy, frigate = (vr.HULL_CLASSES[hull] for hull in vr.FLAGSHIPS)
    assert argosy == {"cargo_base": 240, "fuel_base": 50, "hull_base": 200}
    assert frigate == {"cargo_base": 96, "fuel_base": 72, "hull_base": 340}
    assert argosy["cargo_base"] == carrier["cargo_base"] * 3 // 2 and argosy["hull_base"] < carrier["hull_base"]
    assert frigate["hull_base"] > carrier["hull_base"] * 1.3 and frigate["cargo_base"] * 5 == carrier["cargo_base"] * 3
    # And each is its own line carried further, in the thing the line is for and in hull.
    freighter, cutter = vr.HULL_CLASSES["Freighter"], vr.HULL_CLASSES["Cutter"]
    assert all(argosy[key] > freighter[key] for key in argosy)
    assert all(frigate[key] > cutter[key] for key in frigate)
    # The four hulls there were are what they were.
    assert [vr.HULL_CLASSES[hull] for hull in OLD_HULLS] == [
        {"cargo_base": 24, "fuel_base": 24, "hull_base": 60}, {"cargo_base": 100, "fuel_base": 40, "hull_base": 140},
        {"cargo_base": 40, "fuel_base": 50, "hull_base": 180}, {"cargo_base": 160, "fuel_base": 60, "hull_base": 260}]


# --- the yard -----------------------------------------------------------------------

@pytest.mark.parametrize("line,flagship", [("Freighter", "Argosy"), ("Cutter", "Frigate")])
def test_the_yard_offers_both_and_commissions_the_one_chosen(monkeypatch, terminal, line, flagship):
    terminal(80, 24)
    world = _world_with_seed(42)
    ship = world.save.ship
    ship.hull_class = line
    ship.hull_hp = vr.hull_hp_max(ship)
    world.save.pilot.credits = 50_000
    yard = [" ".join(plain(row).split()) for row in vr.shipyard_lines(world)]
    rows = [row for row in yard if "-Class" in row]
    assert [row.split("-Class")[0][-len(name):] for row, name in zip(rows, ("Carrier", flagship))] == ["Carrier", flagship]
    target = vr.HULL_CLASSES[flagship]
    assert f"hull {vr.hull_hp_max(ship)} → {target['hull_base']}" in rows[1] or f"{target['hull_base']}" in rows[1]
    assert rows[1].endswith("45,000cr") and "A refit is a permanent hull change." in yard
    # Commission it: read the preview, press C, say yes.
    keys = iter(["C", "B"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    asked = []
    monkeypatch.setattr(vr, "confirm", lambda prompt, p, **kw: asked.append(plain(prompt)) or True)
    monkeypatch.setattr(vr, "play_scene", lambda make: True)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        result = vr._hull_refit_screen(vr.Palette(False), world, flagship, 45_000)
    article = "an" if flagship == "Argosy" else "a"
    assert asked == [f"Commission {article} {flagship}-class refit for 45000cr? This is a permanent hull upgrade."]
    assert result == f"Commissioned {article} {flagship} hull for 45000cr."
    assert f"emerges {article} {flagship}." in plain(output.getvalue())
    assert ship.hull_class == flagship and world.save.pilot.credits == 5_000 and ship.hull_hp == vr.hull_hp_max(ship)
    assert not any("-Class" in plain(row) for row in vr.shipyard_lines(world))  # nothing follows a flagship
    assert f"best available class ({flagship})" in " ".join(plain(row) for row in vr.shipyard_lines(world))


def test_a_name_takes_the_article_it_takes():
    assert [vr.a_or_an(word) for word in ("Carrier", "Argosy", "Frigate", "Independent Trader", "Void Baron")] == [
        "a Carrier", "an Argosy", "a Frigate", "an Independent Trader", "a Void Baron"]


# --- how they look ------------------------------------------------------------------

@pytest.mark.parametrize("hull", ["Argosy", "Frigate"])
def test_a_flagship_has_a_sprite_of_its_own_that_a_scene_has_room_for(hull):
    art = vr._SCENE_HULLS[hull]
    assert len(art) == 7 and len({len(row) for row in art}) == 1 and len(art[0]) <= len(vr._SCENE_HULLS["Carrier"][0])
    assert all(pixel == "." or pixel in vr._SPLASH_SHIP_COLORS for row in art for pixel in row)
    assert art not in [vr._SCENE_HULLS[other] for other in vr.HULL_CLASSES if other != hull]
    assert art[0] == art[6]                                   # the same outline above its spine as below
    assert art[3][-1] == "n" and art[3][0] == "f"              # nose right, engine left, like every pilot hull
    # It is wider than the hull it grows from, and the middle rows are its body.
    line = "Freighter" if hull == "Argosy" else "Cutter"
    assert len(art[0]) > len(vr._SCENE_HULLS[line][0])
    ship = vr.starting_ship()
    ship.hull_class = hull
    ship.hull_hp = vr.hull_hp_max(ship)
    assert len(vr.pixel_hull(ship)) == 4 and len(vr.pixel_hull(ship, zoom=2)) == 7
    ship.hull_hp = 1   # and it wears its damage like the others
    scorched = sum(color in (vr.HULL_SCORCH, vr.HULL_EMBER) for row in vr.hull_pixels(ship) for color in row)
    assert scorched == vr._HULL_DAMAGE_MARKS["Critical"]


@pytest.mark.parametrize("hull", ["Argosy", "Frigate"])
def test_a_flagship_has_a_portrait_for_a_terminal_without_pixels(terminal, hull):
    terminal(80, 24, "plain")
    art = vr.PORTRAITS["ship"][hull]
    assert set(art) == {"large", "compact"} and len(art["compact"]) == 3 and 5 <= len(art["large"]) <= 6
    assert all(row.isascii() and len(row) <= 38 for row in art["large"]) and all(len(row) <= 18 for row in art["compact"])
    assert hull.upper() in art["large"][2].replace(" ", "")
    ship = vr.starting_ship()
    ship.hull_class = hull
    ship.hull_hp = vr.hull_hp_max(ship)
    assert vr.ship_portrait(ship, "large") == art["large"] and vr.ship_portrait(ship, "compact") == art["compact"]
    ship.hull_hp = 1
    assert sum(row.count("x") for row in vr.ship_portrait(ship, "large")) == 6  # six marks on a critical hull


def test_every_hull_has_every_picture():
    assert set(vr._SCENE_HULLS) == set(vr.PORTRAITS["ship"]) == set(vr.HULL_CLASSES)


# --- what a save and the record make of them -----------------------------------------

@pytest.mark.parametrize("hull", ["Argosy", "Frigate"])
def test_a_career_in_a_flagship_saves_retires_and_is_remembered(hull):
    world = _finale_world("legend")
    world.save.ship.hull_class = hull
    world.save.ship.hull_hp = vr.hull_hp_max(world.save.ship)
    assert vr.SaveData.from_dict(world.save.to_dict()).ship.hull_class == hull
    retired = vr.finish_career(world.save, "legend")
    assert retired.retired_careers[-1]["ship"] == hull
    assert vr.SaveData.from_dict(retired.to_dict()).retired_careers[-1]["ship"] == hull
    assert any(f"; {hull}" in line for line in vr.career_dossier_lines(retired))
    with pytest.raises((vr.ResumeError, vr.UnsupportedSave)):
        vr.SaveData.from_dict({**world.save.to_dict(), "ship": {**world.save.to_dict()["ship"], "hull_class": "Dreadnought"}})


def test_flying_every_hull_now_means_six_and_one_already_earned_is_kept():
    award = next(accolade for accolade in vr.ACCOLADES if accolade.id == "all_hulls")
    assert award.target == len(vr.HULL_CLASSES) == 6
    world = _world_with_seed(42)
    vr._accolade_record(world.save)["hulls"].extend(["Freighter", "Cutter", "Carrier"])
    assert vr.accolade_metrics(world.save)["hulls"] == 4 and not vr.accolade_unlocked(world.save, "all_hulls")
    vr._accolade_record(world.save)["hulls"].extend(["Argosy", "Frigate"])
    assert "all_hulls" in {accolade.id for accolade in vr.unlock_accolades(world.save)}
    # A pilot who earned it when there were four hulls still has it.
    veteran = _world_with_seed(42)
    vr._accolade_record(veteran.save)["unlocked"]["all_hulls"] = 1
    vr.unlock_accolades(veteran.save)
    assert vr.accolade_unlocked(veteran.save, "all_hulls")


# --- the tools ---------------------------------------------------------------------

def test_the_simulators_can_fly_them():
    career = _tool("career_sim")
    game = career.load_door(ROOT / "voidrunner.py")
    assert {"Argosy", "Frigate"} <= set(career.MILESTONES)
    hauler = career.Pilot(game, "trader", 1005, flagship="line")
    fighter = career.Pilot(game, "contractor", 1005, flagship="line")
    usual = career.Pilot(game, "trader", 1005)
    assert (hauler.flagship, fighter.flagship, usual.flagship) == ("line", "line", "carrier")
    fights = _tool("fight_sim")
    fleet = fights.ships(fights.load_door())
    assert set(fleet) == {"starter", "mid", "finished", "argosy", "frigate"}
    assert fleet["argosy"]["hull_class"] == "Argosy" and fleet["frigate"]["hull_class"] == "Frigate"
    sim_vr = fights.load_door()
    carrier, frigate = (fights.run(sim_vr, ship, "none", 5, 90) for ship in ("finished", "frigate"))
    argosy = fights.run(sim_vr, "argosy", "none", 5, 90)
    assert frigate["won"] > carrier["won"] > argosy["won"]  # against an elite, alone: the warship, the Carrier, the hauler
