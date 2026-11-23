"""A faction posts work for its own (review, v1.3).

Membership gave a perk and a story and then nothing. These pin the work a
faction now posts: who sees it and where, what a warrant and a run are, what
each pays in standing and what it does not, that the board's ordinary offers
are untouched, and that a faction's contract is an ordinary one in a save.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from .support import _world_with_seed, vr


def _member(world, faction):
    setattr(world.save.pilot, vr.FACTION_MEMBERSHIPS[faction]["field"], True)
    world.save.pilot.reputation[faction] = 60
    return world


def _charted(seed=42):
    world = _world_with_seed(seed)
    for station in world.galaxy:
        station.discovered = True
    return world


def _offers(world, faction, days=range(0, 60)):
    """Every offer the faction posts at this station over `days`."""
    found = []
    for day in days:
        world.save.turn = day
        offer = vr.faction_offer(world, world.here.id)
        if offer is not None:
            assert vr.faction_contract(offer) == faction
            found.append(offer)
    return found


def _at_haven(world):
    haven = next(s for s in world.galaxy if s.economy == "Haven")
    world.save.current_system = haven.id
    return haven


def test_only_a_member_in_good_standing_sees_the_work_and_about_one_board_in_two():
    world = _charted()
    assert _offers(world, vr.FACTION_CONCORD) == []  # no commission, no warrants
    _member(world, vr.FACTION_CONCORD)
    posted = _offers(world, vr.FACTION_CONCORD, range(0, 200))
    assert 70 <= len(posted) <= 130
    world.save.pilot.reputation[vr.FACTION_CONCORD] = vr.FACTION_PERK_SUSPEND_AT  # standing lost: the work stops with the perk
    assert _offers(world, vr.FACTION_CONCORD) == []
    # The Cartel's standing buys nothing from the Navy, and the other way about.
    world = _member(_charted(), vr.FACTION_BLACKWAKE)
    assert _offers(world, vr.FACTION_CONCORD) == []
    _at_haven(world)
    assert _offers(world, vr.FACTION_BLACKWAKE)
    assert _offers(_member_at_haven(vr.FACTION_CONCORD), vr.FACTION_BLACKWAKE) == []


def _member_at_haven(faction):
    world = _member(_charted(), faction)
    _at_haven(world)
    return world


def test_a_warrant_is_a_bounty_on_a_real_raider_near_a_charted_system():
    world = _member(_charted(), vr.FACTION_CONCORD)
    hops = vr.bfs_hops(world.by_id, world.here.id)
    for offer in _offers(world, vr.FACTION_CONCORD):
        target = world.by_id[offer.target_system]
        assert offer.kind == "bounty" and offer.description == f"Concord warrant: hunt down a raider reported near {target.name}"
        assert 1 <= hops[target.id] <= 3 and target.discovered and offer.origin_system == world.here.id
        assert 1 <= offer.pirate_tier <= 4 and offer.pirate_tier in (max(1, min(4, target.danger)), max(1, min(4, target.danger + 1)))
        assert offer.reward == vr.BOUNTY_PAY[0] + offer.pirate_tier * vr.BOUNTY_PAY[1]
        vr._validate_combat_mission_snapshot(offer.to_dict(), "bounty")


def test_a_run_is_contraband_from_a_haven_to_somewhere_that_is_not_one():
    world = _member_at_haven(vr.FACTION_BLACKWAKE)
    hops = vr.bfs_hops(world.by_id, world.here.id)
    at_origin, per_jump, flat = vr.DELIVERY_PAY
    for offer in _offers(world, vr.FACTION_BLACKWAKE):
        target = world.by_id[offer.target_system]
        assert offer.kind == "delivery" and not vr.COMMODITIES[offer.commodity]["legal"]
        assert target.economy != "Haven" and 2 <= hops[target.id] <= 4  # never the next jump out
        assert 3 <= offer.quantity <= vr.delivery_contract_ceiling(world.save.ship)
        ordinary = offer.quantity * vr.COMMODITIES[offer.commodity]["base"] * (at_origin + per_jump * hops[target.id]) + flat
        assert offer.reward == round(ordinary * vr.CARTEL_RUN_PREMIUM)
        assert offer.description.startswith("Cartel run: deliver ") and target.name in offer.description
        assert not vr.chain_continues(world, offer)  # the Cartel's work is not a client's chain


def test_the_boards_ordinary_offers_are_what_they_would_have_been():
    for day in range(0, 12):
        member, stranger = _member(_charted(), vr.FACTION_CONCORD), _charted()
        member.save.turn = stranger.save.turn = day
        posted = vr.generate_mission_board(member)
        plain = vr.generate_mission_board(stranger)
        ordinary = [(m.kind, m.description, m.reward) for m in posted if vr.faction_contract(m) is None]
        assert ordinary == [(m.kind, m.description, m.reward) for m in plain][:len(ordinary)]
        assert len(posted) <= 4 and sum(1 for m in posted if vr.faction_contract(m)) <= 1
        assert len(plain) - len(ordinary) in (0, 1)  # a full board gave up its last place, at most


def test_a_run_pays_blackwake_standing_and_none_with_concord():
    world = _member_at_haven(vr.FACTION_BLACKWAKE)
    run = _offers(world, vr.FACTION_BLACKWAKE)[0]
    world.save.turn = run.deadline_turn - 1  # in time
    world.save.active_missions = [run]
    vr._acquire_cargo(world, run.commodity, run.quantity, 100 * run.quantity)
    world.save.current_system = run.target_system
    before = dict(world.save.pilot.reputation)
    credits = world.save.pilot.credits
    messages = vr.check_mission_completions(world)
    assert world.save.pilot.credits == credits + vr.mission_reward_for(world.save, "delivery", run.reward)
    assert world.save.pilot.reputation[vr.FACTION_BLACKWAKE] == before[vr.FACTION_BLACKWAKE] + vr.FACTION_CONTRACT_STANDING
    assert world.save.pilot.reputation[vr.FACTION_CONCORD] == before[vr.FACTION_CONCORD]
    assert f"+{vr.FACTION_CONTRACT_STANDING} Blackwake standing)" in messages[-1] and "Concord" not in messages[-1]
    # An ordinary delivery is still Concord's one point.
    world = _charted()
    plain = vr.Mission(id=3, kind="delivery", description="Deliver 2x Food to X", reward=200, origin_system=0,
                       target_system=1, commodity="food", quantity=2)
    world.save.active_missions = [plain]
    vr._acquire_cargo(world, "food", 2, 20)
    world.save.current_system = 1
    assert "+1 Concord standing)" in vr.check_mission_completions(world)[-1]
    assert world.save.pilot.reputation[vr.FACTION_CONCORD] == vr.CONCORD_STANDING_PER_CONTRACT


@pytest.mark.parametrize("warrant,matches,gain", [(True, True, 3), (True, False, -3), (False, True, 0)])
def test_a_warrant_served_on_the_right_ship_is_worth_standing(monkeypatch, warrant, matches, gain):
    world = _member(_charted(), vr.FACTION_CONCORD)
    target = sorted(world.here.connections)[0]
    tag = vr.FACTION_CONTRACT_TAGS[vr.FACTION_CONCORD] if warrant else ""
    bounty = vr.Mission(id=5, kind="bounty", description=f"{tag}hunt down a raider reported near X", reward=1100,
                        origin_system=0, target_system=target, pirate_tier=1, deadline_turn=40)
    world.save.active_missions = [bounty]
    world.save.pilot.credits = 5_000
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "won")
    monkeypatch.setattr(vr, "new_bounty_warrant", lambda w, b: {"matches": matches})
    monkeypatch.setattr(vr, "play_scene", lambda make: True)
    monkeypatch.setattr(vr, "read_key", lambda: " ")
    world.event_rng.random = lambda: 1.0
    before = world.save.pilot.reputation[vr.FACTION_CONCORD]
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, target)
    assert world.save.pilot.missions_completed == 1
    assert world.save.pilot.reputation[vr.FACTION_CONCORD] - before == gain
    served = any("Concord warrant served" in line for line in world.hop_report)
    assert served == (warrant and matches)


def test_the_contracts_sheet_and_the_factions_screen_say_what_the_work_is_worth():
    world = _member_at_haven(vr.FACTION_BLACKWAKE)
    assert f"+{vr.FACTION_CONTRACT_STANDING} for each run the Cartel posts at a Haven" in vr.faction_standing_route(vr.FACTION_BLACKWAKE)
    assert f"+{vr.FACTION_CONTRACT_STANDING} for a warrant the Navy posts for its own" in vr.faction_standing_route(vr.FACTION_CONCORD)


def test_a_factions_contract_is_an_ordinary_one_in_a_save_that_still_loads():
    world = _member(_charted(), vr.FACTION_CONCORD)
    day = next(day for day in range(0, 40) if _offers(world, vr.FACTION_CONCORD, [day]))
    world.save.turn = day
    posted = vr.generate_mission_board(world)
    warrant = next(m for m in posted if vr.faction_contract(m) == vr.FACTION_CONCORD)
    vr.accept_mission(world, warrant)
    again = vr.SaveData.from_dict(world.save.to_dict())
    assert [m.description for m in again.active_missions] == [warrant.description]
    assert set(warrant.to_dict()) <= {field.name for field in vr.dataclasses.fields(vr.Mission)}
