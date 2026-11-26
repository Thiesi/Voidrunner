"""The crew take stations in a fight (tactical ruleset 5).

A fight was one resource and two verbs, and a specialist was three points of
damage or nothing. These pin the three stations: who has one, that each is
used once a fight, what each does to the exchange and that the panel quotes
what the round then does, that a ship without crew and a fight begun under an
older ruleset are untouched, and that a fight's saved state carries what was
spent.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from .support import _world_with_seed, plain, vr


def _fight(*roles, tier=2, name="Hollow Fang", version=None, hull=None, **ship):
    world = _world_with_seed(42)
    for role in roles:
        setattr(world.save.ship, f"has_{role}", True)
    for key, value in ship.items():
        setattr(world.save.ship, key, value)
    world.save.ship.hull_hp = vr.hull_hp_max(world.save.ship) if hull is None else hull
    world.event_rng.seed(1)  # a career's luck is drawn fresh; a test's is fixed
    hp = 20 + 15 * tier
    pirate = vr.Pirate(name, tier, hp, hp)
    tactics = vr.new_tactics(pirate) if version is None else vr.new_tactics(pirate, version)
    return world, pirate, tactics


def test_a_new_fight_has_stations_and_starts_with_nothing_spent():
    world, pirate, tactics = _fight()
    assert vr.CREW_STATION_RULESET_VERSION == 5 and vr.TACTICAL_RULESET_VERSION == 7  # stations came with five
    assert tactics == {"version": 7, "profile": vr.tactical_profile(pirate.name), "step": 0, "brace_ready": True, "spent": ""}
    assert vr.crew_stations(world, pirate, tactics) == []  # nobody aboard, nothing to call on
    assert vr.TACTICAL_THREAT_BONUS_BY_VERSION[5] == vr.TACTICAL_THREAT_BONUS_BY_VERSION[4]  # the same damage


def test_each_specialist_aboard_has_a_station_once_a_fight():
    world, pirate, tactics = _fight("gunner", "engineer", "navigator", hull=30)
    assert vr.crew_stations(world, pirate, tactics) == ["C", "M", "N"]
    assert {key: role for key, (role, _label) in vr.CREW_STATIONS.items()} == {"C": "gunner", "M": "engineer", "N": "navigator"}
    vr.tactical_round(world, pirate, tactics, "C")
    assert vr.crew_stations(world, pirate, tactics) == ["M", "N"] and tactics["spent"] == "C"
    with pytest.raises(ValueError):
        vr.tactical_round(world, pirate, tactics, "C")
    vr.tactical_mend(world, pirate, tactics)
    vr.tactical_round(world, pirate, tactics, "N")
    assert vr.crew_stations(world, pirate, tactics) == [] and sorted(tactics["spent"]) == ["C", "M", "N"]
    with pytest.raises(ValueError):
        vr.tactical_mend(world, pirate, tactics)
    # The next contact is a new fight, and the crew are at their stations again.
    assert vr.new_tactics(vr.Pirate("Grimwire", 2, 50, 50))["spent"] == ""


def test_a_whole_hull_has_nothing_to_mend():
    world, pirate, tactics = _fight("engineer")
    assert vr.crew_stations(world, pirate, tactics) == []
    world.save.ship.hull_hp -= 1
    assert vr.crew_stations(world, pirate, tactics) == ["M"] and vr.mend_amount(world.save.ship) == 1


@pytest.mark.parametrize("name", ["Hollow Fang", "Grimwire", "Rust Wraith"])
def test_a_called_shot_is_half_as_much_again_and_cover_does_not_blunt_it(name):
    for seed in range(12):
        # Tier 1 is read perfectly, so both fights meet the pattern's first move.
        world, pirate, tactics = _fight("gunner", name=name, tier=1)
        twin, other, plain_tactics = _fight("gunner", name=name, tier=1)
        world.event_rng.seed(seed)
        twin.event_rng.seed(seed)
        move = vr.tactical_steps(tactics)[0]
        dealt, _taken, lines = vr.tactical_round(world, pirate, tactics, "C")
        fired, _, _ = vr.tactical_round(twin, other, plain_tactics, "F")
        ship = world.save.ship
        same_roll = {vr._tactical_outgoing_damage(ship, move, roll, called=True) for roll in range(9, 15)
                     if vr._tactical_outgoing_damage(ship, move, roll) == fired}
        assert dealt in same_roll and dealt > fired
        assert lines[0].startswith(vr.crew_name(world, "gunner") + " calls the shot")
        assert tactics["brace_ready"] is True  # it is a Fire: Guard is recharged
    ship = _fight("gunner")[0].save.ship
    for move, (percent, _incoming) in vr.TACTICAL_INTENTS.items():
        for roll in (9, 14):
            base = roll + ship.weapon_tier * 4 + vr.gunner_bonus(ship)
            assert vr._tactical_outgoing_damage(ship, move, roll, called=True) == base * (max(100, percent) * vr.CALLED_SHOT_PERCENT // 100) // 100
    assert vr.CALLED_SHOT_PERCENT == 150 and vr.SLIP_TAKEN_PERCENT == 25


def test_a_patch_gives_back_a_quarter_of_the_hull_for_the_turns_shot():
    # Tier 1 is read perfectly: the contact makes the move shown and its cycle moves on by one.
    world, pirate, tactics = _fight("engineer", hull=20, tier=1)
    ship = world.save.ship
    full = vr.hull_hp_max(ship)
    gain = vr.mend_amount(ship)
    assert gain == max(vr.MEND_LEAST, full // vr.MEND_SHARE)
    enemy = pirate.hp
    mended, received, lines = vr.tactical_mend(world, pirate, tactics)
    assert mended == gain and pirate.hp == enemy  # no shot was fired
    assert received > 0 and ship.hull_hp == 20 + gain - received  # and nothing guarded against its move
    assert lines[0] == f"{vr.crew_name(world, 'engineer')} patches the hull under fire: +{gain} hull."
    assert tactics["step"] == 1  # the contact made its move
    # Never past the maximum.
    world, pirate, tactics = _fight("engineer", hull=vr.hull_hp_max(_fight()[0].save.ship) - 3)
    assert vr.tactical_mend(world, pirate, tactics)[0] == 3


def test_a_slip_is_a_full_shot_that_the_contacts_move_only_grazes():
    for seed in range(12):
        world, pirate, tactics = _fight("navigator", tier=3)
        twin, other, plain_tactics = _fight("navigator", tier=3)
        world.event_rng.seed(seed)
        twin.event_rng.seed(seed)
        dealt, received, lines = vr.tactical_round(world, pirate, tactics, "N")
        fired, taken, _ = vr.tactical_round(twin, other, plain_tactics, "F")
        assert dealt == fired  # the same shot
        assert received == taken * vr.SLIP_TAKEN_PERCENT // 100 < taken  # the same move, a quarter of it
        assert world.event_rng.getstate() == twin.event_rng.getstate()  # and the fight stays on its sequence
        assert vr.crew_name(world, "navigator") + " had already moved the ship" in lines[-1]
        assert tactics["brace_ready"] is True and tactics["step"] == plain_tactics["step"]


def test_the_panel_quotes_what_each_station_then_does(monkeypatch):
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    world, pirate, tactics = _fight("gunner", "engineer", "navigator", hull=30)
    rows = [plain(row) for row in vr.combat_option_rows(world, pirate, patrol=False, tactics=tactics)]
    ship = world.save.ship
    intent = vr.tactical_intent(tactics)
    weak, strong = (vr._tactical_outgoing_damage(ship, intent, roll, called=True) for roll in (9, 14))
    low, high = (vr._tactical_incoming_damage(ship, pirate.tier, intent, roll, tactics) for roll in (4, 9))
    called = next(row for row in rows if row.startswith("[C] Called shot"))
    assert f"deal {weak}-{strong}" in called and f"take {low}-{high}; once" in called
    mend = next(row for row in rows if row.startswith("[M] Mend"))
    assert f"+{vr.mend_amount(ship)} hull" in mend and "once, no shot" in mend
    slip = next(row for row in rows if row.startswith("[N] Slip"))
    assert f"take {low * vr.SLIP_TAKEN_PERCENT // 100}-{high * vr.SLIP_TAKEN_PERCENT // 100}; once" in slip
    # Between Fire and Guard, which every ship has, and the ways out.
    keys = [row[1] for row in rows if row.startswith("[")]
    assert keys[:5] == ["F", "G", "C", "M", "N"] and keys[5] == "E"
    # Spent, a station leaves the panel.
    tactics["spent"] = "CN"
    rows = [plain(row) for row in vr.combat_option_rows(world, pirate, patrol=False, tactics=tactics)]
    assert not any(row.startswith(("[C]", "[N]")) for row in rows) and any(row.startswith("[M]") for row in rows)


def test_a_ship_without_crew_and_an_older_fight_are_what_they_were():
    # No crew: ruleset 5 plays exchange for exchange as ruleset 4 did.
    for seed in range(8):
        new, a, t5 = _fight(tier=3)
        old, b, t4 = _fight(tier=3, version=4)
        new.event_rng.seed(seed)
        old.event_rng.seed(seed)
        for action in ("F", "G", "F", "F"):
            if a.hp <= 0 or new.save.ship.hull_hp <= 0:
                break
            assert vr.tactical_round(new, a, t5, action) == vr.tactical_round(old, b, t4, action)
    # A fight begun before ruleset 5 has no stations, crew or not, and keeps its shape.
    world, pirate, tactics = _fight("gunner", "engineer", "navigator", version=4, hull=20)
    assert "spent" not in tactics and vr.crew_stations(world, pirate, tactics) == []
    with pytest.raises(ValueError):
        vr.tactical_round(world, pirate, tactics, "C")
    vr._validate_tactics(tactics)


def test_a_fights_saved_state_carries_what_was_spent():
    vr._validate_tactics({"version": 5, "profile": "Raider", "step": 1, "brace_ready": False, "spent": "NC"})
    for broken in ({"spent": "CC"}, {"spent": "X"}, {"spent": 3}, {}):
        state = {"version": 5, "profile": "Raider", "step": 0, "brace_ready": True, **broken}
        with pytest.raises(ValueError):
            vr._validate_tactics(state)
    with pytest.raises((ValueError, vr.UnsupportedSave)):  # an older fight may not carry it
        vr._validate_tactics({"version": 4, "profile": "Raider", "step": 0, "brace_ready": True, "spent": ""})


def test_the_roster_and_the_action_bar_name_the_stations():
    assert "one called shot a fight" in vr.CREW_ROLES["gunner"]["effect"]
    assert "one hull patch a fight" in vr.CREW_ROLES["engineer"]["effect"]
    assert "one slip a fight" in vr.CREW_ROLES["navigator"]["effect"]
    assert vr.combat_action_bar("F/G/C/M/N/E") == "[F] Fire [G] Guard [C] Called shot [M] Mend [N] Slip [E] Evade [I] Info [<>] Page: "


def test_a_real_fight_takes_the_keys_and_keeps_the_state(monkeypatch):
    world = _world_with_seed(42)
    for role in ("gunner", "engineer", "navigator"):
        setattr(world.save.ship, f"has_{role}", True)
    world.save.ship.hull_hp = 30
    target = sorted(world.here.connections)[0]
    world.save.pending_travel = {
        "version": 1, "origin": 0, "destination": target, "was_discovered": True, "destroyed": False,
        "phase": "primary", "primary": "random", "bounty": None, "escorts": [], "escort_index": 0,
        "encounter": {"kind": "pirate", "pirates": [{"name": "Hollow Fang", "tier": 1, "hp": 400, "hp_max": 400}], "index": 0},
    }
    typed = iter(["N", "C", "M", "W"])  # W is not a key here: the session ends when the keys run out

    def key():
        try:
            return next(typed)
        except StopIteration:
            raise KeyboardInterrupt

    monkeypatch.setattr(vr, "read_key", key)
    monkeypatch.setattr(vr, "play_scene", lambda make: True)
    with contextlib.redirect_stdout(io.StringIO()), pytest.raises(KeyboardInterrupt):
        vr.screen_combat(vr.Palette(False), world, vr.Pirate("Hollow Fang", 1, 400, 400))
    combat = world.save.pending_travel["encounter"]["combat"]
    assert sorted(combat["tactics"]["spent"]) == ["C", "M", "N"] and combat["tactics"]["version"] == vr.TACTICAL_RULESET_VERSION
    assert combat["pirate"]["hp"] < 400  # the slip and the called shot both landed
    vr.SaveData.from_dict(world.save.to_dict())  # and the fight is a save that loads
