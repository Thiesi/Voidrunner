"""A raider that got the better of the pilot comes back.

A raider was a name, a tier and a hull, and the next one owed nothing to the
last. These pin the score a raider keeps: what opens one, when and how it
returns, what settling it pays, how many a career holds and for how long, that
the encounter's own draws are untouched, and that the scores are a field of
the save which loads and which a new career starts without.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from .support import _world_with_seed, plain, vr


def _pirate(name="Ashclaw", tier=1):
    hull = 20 + tier * 15
    return vr.Pirate(name, tier, hull, hull)


def _return_day(world, start=3, limit=60):
    """A day on which a rested rival is the raider met."""
    return next(day for day in range(start, limit)
                if vr.zlib.crc32(f"rival:{world.save.seed}:{day}".encode()) % vr.RIVAL_RETURN_ONE_IN == 0)


def test_what_a_raider_did_opens_a_score_and_the_career_keeps_three():
    world = _world_with_seed(42)
    world.save.turn = 5
    vr.remember_rival(world, _pirate("Ashclaw", 1), "ran")
    assert world.save.rivals == [{"name": "Ashclaw", "tier": 1, "day": 5, "how": "ran"}]
    assert vr.rival(world.save, "Ashclaw")["how"] == "ran" and vr.rival(world.save, "Grimwire") is None
    assert "Ashclaw will know this ship again" in world.save.pilot.log[-1]
    # The same raider again: one score, its highest tier, the latest thing it did.
    world.save.turn = 9
    vr.remember_rival(world, _pirate("Ashclaw", 3), "wreck")
    vr.remember_rival(world, _pirate("Ashclaw", 2), "paid")
    assert world.save.rivals == [{"name": "Ashclaw", "tier": 3, "day": 9, "how": "paid"}]
    for name in ("Grimwire", "Ravage", "Nullshade"):
        vr.remember_rival(world, _pirate(name), "ran")
    assert [item["name"] for item in world.save.rivals] == ["Grimwire", "Ravage", "Nullshade"]  # the newest three
    assert len(world.save.rivals) == vr.RIVAL_MAX


def test_a_rival_returns_rested_within_its_memory_and_one_contact_in_two():
    world = _world_with_seed(42)
    world.save.turn = 2
    vr.remember_rival(world, _pirate("Ashclaw", 2), "wreck")
    back = {}
    for day in range(2, 2 + vr.RIVAL_MEMORY_DAYS + 6):
        world.save.rivals = [{"name": "Ashclaw", "tier": 2, "day": 2, "how": "wreck"}]
        world.save.turn = day
        back[day] = vr.returning_rival(world)
    assert back[2] is None and back[3] is None  # not the day after
    days = [day for day, item in back.items() if item]
    assert days and min(days) >= 2 + vr.RIVAL_REST_DAYS and max(days) <= 2 + vr.RIVAL_MEMORY_DAYS
    assert 0.3 <= len(days) / (vr.RIVAL_MEMORY_DAYS - vr.RIVAL_REST_DAYS + 1) <= 0.7
    # Past its memory the score is dropped, not merely dormant.
    world.save.rivals = [{"name": "Ashclaw", "tier": 2, "day": 2, "how": "wreck"}]
    world.save.turn = 2 + vr.RIVAL_MEMORY_DAYS + 1
    assert vr.returning_rival(world) is None and world.save.rivals == []


@pytest.mark.parametrize("tier,back", [(0, 1), (2, 3), (3, 4), (4, 4)])
def test_it_comes_back_a_tier_stronger_up_to_the_strongest(tier, back):
    ship = vr.rival_ship({"name": "Ashclaw", "tier": tier, "day": 1, "how": "ran"})
    assert (ship.name, ship.tier, ship.hp, ship.hp_max) == ("Ashclaw", back, 20 + back * 15, 20 + back * 15)


def test_settling_a_score_pays_its_salvage_again_and_closes_it():
    world = _world_with_seed(42)
    vr.remember_rival(world, _pirate("Ashclaw", 2), "paid")
    credits = world.save.pilot.credits
    lines = vr.settle_rival(world, _pirate("Ashclaw", 3))
    assert world.save.pilot.credits == credits + 40 + 3 * 60 and world.save.rivals == []
    assert lines == ["A score settled: the Ashclaw will not be back. Its hold was full of other people's cargo: +220cr."]
    assert "score" in {accolade.id for accolade in vr.unlock_accolades(world.save)}
    assert "Settled a score with the Ashclaw." in world.save.pilot.highlights


def _travel(world, target, name, tier, hull=None):
    world.save.pending_travel = {
        "version": 1, "origin": world.here.id, "destination": target, "was_discovered": True, "destroyed": False,
        "phase": "primary", "primary": "random", "bounty": None, "escorts": [], "escort_index": 0,
        "encounter": {"kind": "pirate", "pirates": [{"name": name, "tier": tier, "hp": hull or 20 + tier * 15,
                                                      "hp_max": 20 + tier * 15}], "index": 0},
    }


def _play(world, keys, monkeypatch, pirate):
    typed = iter(keys)
    monkeypatch.setattr(vr, "read_key", lambda: next(typed))
    monkeypatch.setattr(vr, "play_scene", lambda make: True)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        outcome = vr.screen_combat(vr.Palette(False), world, pirate)
    return outcome, plain(output.getvalue())


def test_running_from_a_raider_opens_a_score_and_meeting_it_again_says_so(monkeypatch):
    world = _world_with_seed(42)
    target = sorted(world.here.connections)[0]
    world.save.turn = 4
    _travel(world, target, "Ashclaw", 1)
    world.event_rng.random = lambda: 0.0  # the evasion works
    outcome, _text = _play(world, ["E"], monkeypatch, _pirate("Ashclaw", 1))
    assert outcome == "escaped" and vr.rival(world.save, "Ashclaw") == {"name": "Ashclaw", "tier": 1, "day": 4, "how": "ran"}
    # Days later the same name is on the contact: the fight opens by saying so.
    world.save.turn = 9
    _travel(world, target, "Ashclaw", 2)
    world.save.ship.weapon_tier = 4
    world.save.ship.hull_hp = vr.hull_hp_max(world.save.ship)
    credits = world.save.pilot.credits
    outcome, text = _play(world, ["F", "F", "F", "F", "F", "F"], monkeypatch, _pirate("Ashclaw", 2))
    assert "The Ashclaw again. It has not forgotten the ship that ran." in " ".join(text.split())
    assert outcome == "won" and world.save.rivals == []
    assert world.save.pilot.credits == credits + 2 * (40 + 2 * 60)  # its salvage, and its salvage again
    assert any("A score settled" in line for line in world.hop_report)


def test_a_bribe_and_a_lost_ship_are_scores_and_a_patrol_is_not(monkeypatch):
    world = _world_with_seed(42)
    target = sorted(world.here.connections)[0]
    world.save.pilot.credits = 5_000
    _travel(world, target, "Grimwire", 1)
    world.event_rng.random = lambda: 0.0  # the bribe is taken
    outcome, _text = _play(world, ["P"], monkeypatch, _pirate("Grimwire", 1))
    assert outcome == "escaped" and vr.rival(world.save, "Grimwire")["how"] == "paid"
    world = _world_with_seed(42)
    world.save.ship.hull_hp = 1
    _travel(world, target, "Ravage", 4)
    outcome, _text = _play(world, ["F"], monkeypatch, _pirate("Ravage", 4))
    assert outcome == "destroyed" and vr.rival(world.save, "Ravage") == {"name": "Ravage", "tier": 4, "day": 0, "how": "wreck"}
    # A raider destroyed with no score against it opens none.
    world = _world_with_seed(42)
    world.save.ship.weapon_tier = 4
    _travel(world, target, "Nullshade", 0)
    outcome, _text = _play(world, ["F", "F", "F"], monkeypatch, _pirate("Nullshade", 0))
    assert outcome == "won" and world.save.rivals == []


def test_the_contact_a_pilot_meets_can_turn_out_to_be_the_rival_and_the_draws_are_untouched(monkeypatch):
    plain_world, scored = _world_with_seed(42), _world_with_seed(42)
    day = _return_day(scored)
    for world in (plain_world, scored):
        world.save.turn = day
        world.event_rng.seed(7)
    scored.save.rivals = [{"name": "Static Ghost", "tier": 2, "day": day - 3, "how": "wreck"}]
    dest = plain_world.by_id[sorted(plain_world.here.connections)[0]]
    met = {}
    for label, world in (("plain", plain_world), ("scored", scored)):
        state = {}
        monkeypatch.setattr(vr, "_travel_encounter", lambda w, state=state: state)
        monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate, label=label: met.setdefault(label, pirate) and "won")
        monkeypatch.setattr(world.event_rng, "choices", lambda *a, **k: ["pirate"])
        world.event_rng.random = lambda: 0.0  # an encounter, and no squadron roll changes it
        world.save.pending_travel = {"destination": dest.id}
        world.commit = lambda: None
        with contextlib.redirect_stdout(io.StringIO()):
            vr._resolve_random_travel_encounter(vr.Palette(False), world, world.by_id[dest.id])
    assert (met["scored"].name, met["scored"].tier) == ("Static Ghost", 3)  # the rival, a tier stronger
    assert met["plain"].name != "Static Ghost" or met["plain"].tier != 3
    assert plain_world.event_rng.getstate() == scored.event_rng.getstate()  # the same draws either way


def test_status_lists_the_scores_still_open():
    world = _world_with_seed(42)
    assert not any("SCORES TO SETTLE" in plain(line) for line in vr.pilot_record_lines(world))
    world.save.turn = 12
    vr.remember_rival(world, _pirate("Ashclaw", 2), "wreck")
    world.save.turn = 20
    text = " ".join(plain(line) for line in vr.pilot_record_lines(world))
    assert "SCORES TO SETTLE" in text
    assert f"Ashclaw tier 2; it broke your ship on day 12; it remembers for {vr.RIVAL_MEMORY_DAYS - 8} more days" in " ".join(text.split())


def test_the_scores_are_saved_and_a_new_career_starts_without_them():
    world = _world_with_seed(42)
    assert "rivals" not in world.save.to_dict()  # absent until there is one
    world.save.turn = 3
    vr.remember_rival(world, _pirate("Ashclaw", 2), "ran")
    again = vr.SaveData.from_dict(world.save.to_dict())
    assert again.rivals == world.save.rivals and again.rivals is not world.save.rivals
    for broken in ([{"name": "Ashclaw", "tier": 9, "day": 1, "how": "ran"}], [{"name": "Ashclaw", "tier": 1, "day": 99, "how": "ran"}],
                   [{"name": "Ashclaw", "tier": 1, "day": 1, "how": "liked it"}], [{"name": "Ashclaw", "tier": 1, "day": 1}],
                   [{"name": "A", "tier": 1, "day": 1, "how": "ran"}] * 2, "Ashclaw"):
        data = world.save.to_dict()
        data["rivals"] = broken
        with pytest.raises(vr.ResumeError):
            vr.SaveData.from_dict(data)
    assert vr.retire_pilot(world.save).rivals == []
