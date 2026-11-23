"""A beaten raider is a prize, and what becomes of it is the pilot's to decide (review).

Winning a fight ended it: the raider blew up and a sum of credits appeared.
These pin the choice a win now leaves: what each of the four pays and in what
coin, when each is offered, that the question is part of the saved fight and is
answered once, that a favour owed turns the next raiders away, what the page
and the window show while the question is open, and what a save keeps.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from .support import _add_cargo, _world_with_exploration_choice, _world_with_pending_fight, _world_with_seed, plain, vr


def _raider(name="Ashclaw", tier=2):
    hull = 20 + tier * 15
    return vr.Pirate(name, tier, hull, hull)


def _fight(world, pirate, hp=1):
    """A random raider met on a journey, one shot from beaten."""
    beaten = vr.dataclasses.replace(pirate, hp=hp)
    world.save.pending_travel["encounter"] = {
        "kind": "pirate", "pirates": [vr.dataclasses.asdict(pirate)], "index": 0,
        "combat": {"pirate": vr.dataclasses.asdict(beaten), "outcome": None, "lines": [],
                   "tactics": vr.new_tactics(pirate), "hull_before": world.save.ship.hull_hp}}
    return beaten


def _play(world, pirate, keys, monkeypatch):
    typed = iter(keys)
    monkeypatch.setattr(vr, "read_key", lambda: next(typed))
    monkeypatch.setattr(vr, "play_scene", lambda make: True)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        outcome = vr.screen_combat(vr.Palette(False), world, pirate)
    return outcome, plain(output.getvalue())


@pytest.fixture
def terminal(monkeypatch):
    def size(width: int, height: int, style: str = "auto"):
        monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
        monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
        monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
        monkeypatch.setattr(vr, "_OUTPUT_ENCODING", "utf-8")
    return size


# --- what a wreck is worth ----------------------------------------------------------------------


def test_the_scrap_is_what_a_kill_always_paid_and_the_hold_is_worth_two_and_a_half_times_it():
    world = _world_with_seed(42)
    assert [vr.prize_loot(_raider(tier=tier)) for tier in range(5)] == [40, 100, 160, 220, 280]
    seen = set()
    state = world.event_rng.getstate()
    for day in range(60):
        world.save.turn = day
        for tier in range(5):
            pirate = _raider(tier=tier)
            commodity, quantity = vr.prize_cargo(world, pirate)
            assert (commodity, quantity) == vr.prize_cargo(world, pirate)  # the same hold every time it is asked
            base = vr.COMMODITIES[commodity]["base"]
            target = vr.prize_loot(pirate) * vr.PRIZE_CARGO_WORTH
            assert quantity >= 1 and abs(quantity * base - target) <= base / 2 + 1
            seen.add(commodity)
    assert seen == set(vr.COMMODITIES)  # any good, contraband among them
    assert world.event_rng.getstate() == state


def test_which_choices_a_wreck_leaves():
    world = _world_with_seed(42)
    pirate = _raider()
    assert vr.prize_actions(world, pirate) == "S/C/H/L"
    assert vr.prize_actions(world, pirate, bounty=True) == "S/C"  # a warrant's target is the Navy's already
    _add_cargo(world, "food", vr.cargo_capacity(world.save.ship), unit_cost=5)
    assert vr.prize_actions(world, pirate) == "S/H/L"  # no room, no cargo
    assert vr.prize_actions(world, pirate, bounty=True) == "S"
    assert set(vr.PRIZE_LABELS) == set(vr.PRIZE_KEYS)


# --- the four coins ------------------------------------------------------------------------------


def _standing(world):
    return world.save.pilot.reputation[vr.FACTION_CONCORD], world.save.pilot.reputation[vr.FACTION_BLACKWAKE]


def test_stripping_it_is_the_kill_as_it_always_was():
    world = _world_with_seed(42)
    credits, (concord, blackwake) = world.save.pilot.credits, _standing(world)
    assert vr.take_prize(world, _raider(tier=2), "S") == ["Salvage recovered: 160cr."]
    assert world.save.pilot.credits == credits + 160 and _standing(world) == (concord + 2, blackwake - 1)
    assert world.save.cargo == {} and world.save.favours == []


def test_taking_its_cargo_is_goods_for_nothing_as_far_as_the_hold_has_room():
    world = _world_with_seed(42)
    pirate = _raider(tier=2)
    commodity, quantity = vr.prize_cargo(world, pirate)
    label = vr.COMMODITIES[commodity]["label"]
    credits, (concord, blackwake) = world.save.pilot.credits, _standing(world)
    free = vr.cargo_capacity(world.save.ship)
    taken = min(quantity, free)
    lines = vr.take_prize(world, pirate, "C")
    assert world.save.cargo == {commodity: taken} and world.save.cargo_basis[commodity] == [[taken, 0]]
    assert world.save.pilot.credits == credits and _standing(world) == (concord + 2, blackwake - 1)
    assert lines[0].startswith(f"Taken from the Ashclaw's hold: {taken} {label}")
    # A hold with less room takes what fits and says what it left.
    tight = _world_with_seed(42)
    other = next(c for c in vr.LEGAL_COMMODITIES if c != commodity)
    _add_cargo(tight, other, vr.cargo_capacity(tight.save.ship) - 1, unit_cost=5)
    lines = vr.take_prize(tight, pirate, "C")
    assert tight.save.cargo[commodity] == 1
    assert lines == ([f"Taken from the Ashclaw's hold: 1 {label} of {quantity}; the rest would not fit."] if quantity > 1
                     else [f"Taken from the Ashclaw's hold: 1 {label}."])
    full = _world_with_seed(42)
    _add_cargo(full, other, vr.cargo_capacity(full.save.ship), unit_cost=5)
    with pytest.raises(ValueError):
        vr.take_prize(full, pirate, "C")


def test_handing_it_over_is_standing_and_a_point_of_notoriety_and_no_credits():
    world = _world_with_seed(42)
    credits, (concord, blackwake) = world.save.pilot.credits, _standing(world)
    assert vr.take_prize(world, _raider(), "H") == [
        f"The wreck is marked for the Navy: Concord standing +{vr.PRIZE_CONCORD_STANDING}."]
    assert world.save.pilot.credits == credits and world.save.pilot.notoriety == 0
    assert _standing(world) == (concord + vr.PRIZE_CONCORD_STANDING, blackwake - 1)
    world.save.pilot.notoriety = 4
    assert vr.take_prize(world, _raider("Grimwire"), "H")[0].endswith(", notoriety -1.")
    assert world.save.pilot.notoriety == 3
    assert vr.PRIZE_CONCORD_STANDING > 2  # more than the kill's own token


def test_letting_it_go_is_a_favour_owed_and_the_cartels_regard_and_nothing_from_the_navy():
    world = _world_with_seed(42)
    world.save.turn = 7
    credits, (concord, blackwake) = world.save.pilot.credits, _standing(world)
    lines = vr.take_prize(world, _raider(), "L")
    assert lines == ["The Ashclaw limps home. It owes you one: the next raiders you meet will be waved off."]
    assert world.save.favours == [{"name": "Ashclaw", "day": 7}]
    assert world.save.pilot.credits == credits
    assert _standing(world) == (concord, blackwake + vr.PRIZE_BLACKWAKE_STANDING)
    assert world.save.accolades["counters"]["spared"] == 1
    assert "mercy" in {accolade.id for accolade in vr.unlock_accolades(world.save)}
    # The same ship again is one favour, the newest; a career keeps three.
    world.save.turn = 9
    vr.take_prize(world, _raider(), "L")
    assert world.save.favours == [{"name": "Ashclaw", "day": 9}]
    for name in ("Grimwire", "Ravage", "Nullshade"):
        vr.take_prize(world, _raider(name), "L")
    assert [item["name"] for item in world.save.favours] == ["Grimwire", "Ravage", "Nullshade"]
    assert len(world.save.favours) == vr.FAVOUR_MAX
    with pytest.raises(ValueError):
        vr.take_prize(world, _raider(), "X")


# --- the favour, called in ------------------------------------------------------------------------


def test_a_favour_is_kept_forty_days_and_the_oldest_is_called_in_first():
    world = _world_with_seed(42)
    world.save.favours = [{"name": "Ravage", "day": 12}, {"name": "Ashclaw", "day": 5}]
    world.save.turn = 20
    assert vr.favour_owed(world)["name"] == "Ashclaw"
    world.save.turn = 5 + vr.FAVOUR_MEMORY_DAYS + 1
    assert vr.favour_owed(world)["name"] == "Ravage" and len(world.save.favours) == 1  # the old one is forgotten
    world.save.turn = 12 + vr.FAVOUR_MEMORY_DAYS + 1
    assert vr.favour_owed(world) is None and world.save.favours == []


def _meet_raiders(world, monkeypatch, *, fight="escaped"):
    """Fly into a raider contact through the door's own encounter resolution."""
    world = world
    world.event_rng.random = lambda: 0.0  # an encounter, whatever the danger
    monkeypatch.setattr(world.event_rng, "choices", lambda population, weights: ["pirate"])
    fought = []
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: fought.append(pirate.name) or fight)
    monkeypatch.setattr(vr, "play_scene", lambda make: True)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr._resolve_random_travel_encounter(vr.Palette(False), world, world.by_id[world.save.pending_travel["destination"]])
    return fought, plain(output.getvalue())


def test_the_next_raiders_are_waved_off_by_the_ship_that_was_let_go(monkeypatch):
    world = _world_with_exploration_choice("pirate")
    world.save.pending_travel["encounter"] = {}
    world.save.favours = [{"name": "Ashclaw", "day": 0}]
    hull = world.save.ship.hull_hp
    fought, text = _meet_raiders(world, monkeypatch)
    state = world.save.pending_travel["encounter"]
    assert fought == [] and world.save.ship.hull_hp == hull
    assert state["kind"] == "waved" and state["done"] is True and "pirates" not in state
    assert "the Ashclaw is with it. It knows this ship. They peel away without a shot: a favour repaid." in " ".join(text.split())
    assert world.save.favours == [] and world.save.accolades["counters"]["favours"] == 1
    assert "favour" in {accolade.id for accolade in world.pending_accolades}  # earned in the commit that kept it
    vr.SaveData.from_dict(world.save.to_dict())  # the waved-off encounter is a state a save may hold
    # The favour is spent: the raiders after that are raiders.
    world.save.pending_travel["encounter"] = {}
    fought, _text = _meet_raiders(world, monkeypatch)
    assert len(fought) >= 1


def test_a_rival_with_a_score_to_settle_is_not_turned_by_anyones_debt(monkeypatch):
    world = _world_with_exploration_choice("pirate")
    world.save.pending_travel["encounter"] = {}
    world.save.favours = [{"name": "Ashclaw", "day": 0}]
    monkeypatch.setattr(vr, "returning_rival", lambda w: {"name": "Grimwire", "tier": 1, "day": 0, "how": "ran"})
    fought, _text = _meet_raiders(world, monkeypatch)
    assert fought[0] == "Grimwire" and world.save.favours == [{"name": "Ashclaw", "day": 0}]


# --- the fight: asked once, kept with the win ------------------------------------------------------


def test_a_won_fight_asks_and_the_answer_is_kept_with_it(monkeypatch):
    world = _world_with_exploration_choice("pirate")
    pirate = _fight(world, _raider())
    credits = world.save.pilot.credits
    outcome, text = _play(world, pirate, ["F", "L"], monkeypatch)
    combat = world.save.pending_travel["encounter"]["combat"]
    assert outcome == "won" and combat["prize"] == "L" and world.save.pilot.kills == 1
    assert world.save.pilot.credits == credits and world.save.favours == [{"name": "Ashclaw", "day": world.save.turn}]
    assert "The Ashclaw is dead in space." in combat["lines"] and combat["lines"][-1].startswith("The Ashclaw limps home.")
    assert not any("is destroyed!" in line for line in combat["lines"])
    assert "[S] Strip it [C] Take cargo [H] Hand over [L] Let go" in " ".join(text.split())
    # Returned again -- a resumed journey -- the fight asks nothing and pays nothing twice.
    monkeypatch.setattr(vr, "prize_choice", lambda *a: pytest.fail("asked twice"))
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.screen_combat(vr.Palette(False), world, pirate) == "won"
    assert len(world.save.favours) == 1


def test_a_connection_lost_at_the_question_comes_back_to_the_question(monkeypatch):
    world = _world_with_exploration_choice("pirate")
    pirate = _fight(world, _raider())

    class Dropped(Exception):
        pass

    def drop(*args):
        raise Dropped

    monkeypatch.setattr(vr, "prize_choice", drop)
    with pytest.raises(Dropped):
        _play(world, pirate, ["F"], monkeypatch)
    record = world.save.to_dict()
    assert record["pending_travel"]["encounter"]["combat"]["prize"] == "open"
    assert record["pilot"]["kills"] == 1  # the win is kept; only the wreck is undecided
    resumed = vr.World(vr.SaveData.from_dict(record))
    credits = resumed.save.pilot.credits
    monkeypatch.setattr(vr, "prize_choice", lambda p, w, target, actions: "S")
    monkeypatch.setattr(vr, "play_scene", lambda make: pytest.fail("a kept win replays nothing"))
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.screen_combat(vr.Palette(False), resumed, pirate) == "won"
    assert resumed.save.pilot.credits == credits + vr.prize_loot(pirate) and resumed.save.pilot.kills == 1
    assert resumed.save.pending_travel["encounter"]["combat"]["prize"] == "S"


def test_a_bountys_target_is_stripped_or_emptied_and_a_patrol_is_no_prize(monkeypatch):
    world, pirate = _world_with_pending_fight()  # a warrant's target, on the way to it
    world.save.pending_travel["encounter"]["combat"]["pirate"]["hp"] = 1
    offered = []
    monkeypatch.setattr(vr, "prize_choice", lambda p, w, target, actions: offered.append(actions) or "S")
    monkeypatch.setattr(vr, "play_scene", lambda make: True)
    monkeypatch.setattr(vr, "read_key", lambda: "F")
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.screen_combat(vr.Palette(False), world, pirate) == "won"
    assert offered == ["S/C"]
    # A patrol destroyed is no prize: nothing is asked, and nothing is salvaged.
    world = _world_with_exploration_choice("none")
    world.save.pending_travel["primary"] = "patrol"
    patrol = _fight(world, _raider("Concord Lance", 1))
    world.save.pending_travel["encounter"].pop("kind"), world.save.pending_travel["encounter"].pop("pirates")
    world.save.pending_travel["encounter"].pop("index")
    credits = world.save.pilot.credits
    monkeypatch.setattr(vr, "prize_choice", lambda *a: pytest.fail("a patrol is no prize"))
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr._screen_combat_session(vr.Palette(False), world, patrol, patrol=True) == "won"
    assert world.save.pilot.credits == credits and "prize" not in world.save.pending_travel["encounter"]["combat"]


def test_a_squadrons_first_ship_is_stripped_while_its_wingman_still_shoots(monkeypatch):
    world = _world_with_exploration_choice("pirate")
    lead, wing = _raider("Hollow Fang", 2), _raider("Rust Wraith", 1)
    world.save.pending_travel["encounter"] = {
        "kind": "pirate", "pirates": [vr.dataclasses.asdict(lead), vr.dataclasses.asdict(wing)], "index": 0,
        "formation": {"version": 1, "engaged": True},
        "combat": {"pirate": vr.dataclasses.asdict(vr.dataclasses.replace(lead, hp=1)), "outcome": None, "lines": [],
                   "tactics": vr.new_tactics(lead), "hull_before": world.save.ship.hull_hp}}
    credits = world.save.pilot.credits
    monkeypatch.setattr(vr, "prize_choice", lambda *a: pytest.fail("the wingman is still shooting"))
    outcome, _text = _play(world, vr.dataclasses.replace(lead, hp=1), ["F"], monkeypatch)
    combat = world.save.pending_travel["encounter"]["combat"]
    assert outcome == "won" and "prize" not in combat
    assert world.save.pilot.credits == credits + vr.prize_loot(lead) and "Salvage recovered: 160cr." in combat["lines"]
    # The last of them is a prize like any other.
    world.save.pending_travel["encounter"]["index"] = 1
    world.save.pending_travel["encounter"]["combat"] = {
        "pirate": vr.dataclasses.asdict(vr.dataclasses.replace(wing, hp=1)), "outcome": None, "lines": [],
        "tactics": vr.new_tactics(wing), "hull_before": world.save.ship.hull_hp}
    asked = []
    monkeypatch.setattr(vr, "prize_choice", lambda p, w, target, actions: asked.append(target.name) or "S")
    _play(world, vr.dataclasses.replace(wing, hp=1), ["F"], monkeypatch)
    assert asked == ["Rust Wraith"]


# --- what a save keeps ----------------------------------------------------------------------------------


def test_favours_and_an_open_question_are_fields_a_save_keeps_and_checks():
    world = _world_with_exploration_choice("none")
    assert "favours" not in world.save.to_dict() and vr._new_career("Sim").favours == []
    world.save.favours = [{"name": "Ashclaw", "day": 1}]
    record = world.save.to_dict()
    assert vr.SaveData.from_dict(record).favours == [{"name": "Ashclaw", "day": 1}]
    for bad in ([{"name": "Ashclaw"}], [{"name": "", "day": 1}], [{"name": "Ashclaw", "day": 99}],
                [{"name": "A", "day": 0}] * 4, {"name": "Ashclaw"}):
        with pytest.raises(vr.ResumeError):
            vr.SaveData.from_dict({**record, "favours": bad})
    # The question is only ever open on a fight that was won.
    pirate = _fight(world, _raider(), hp=0)
    combat = world.save.pending_travel["encounter"]["combat"]
    combat.update(outcome="won", prize="open")
    vr.SaveData.from_dict(world.save.to_dict())
    for outcome, prize in (("won", "X"), ("escaped", "open")):
        combat.update(outcome=outcome, prize=prize)
        if outcome != "won":
            combat["pirate"]["hp"] = pirate.hp_max
        with pytest.raises(Exception):
            vr.SaveData.from_dict(world.save.to_dict())


# --- the page and the window ----------------------------------------------------------------------------


@pytest.mark.parametrize("width,height", [(40, 12), (64, 20), (80, 24), (160, 48)])
def test_the_page_is_a_row_a_choice_and_fits(terminal, width, height):
    terminal(width, height)
    world = _world_with_seed(42)
    world.save.pilot.notoriety = 2
    world.save.favours = [{"name": "Ravage", "day": 0}]
    pirate = _raider(tier=4)
    lines = vr.prize_terms(world, pirate, vr.prize_actions(world, pirate))
    text = [plain(line) for line in lines]
    assert text[0].startswith("The Ashclaw is dead in space") and "1 favour owed to you" in text[0]
    rows = [row for row in text if row.startswith("[")]
    assert [row[:3] for row in rows] == ["[S]", "[C]", "[H]", "[L]"]
    assert "+280cr" in rows[0] and f"Concord +{vr.PRIZE_CONCORD_STANDING}" in rows[2] and "notoriety -1" in rows[2]
    assert "a favour owed" in rows[3] and f"Blackwake +{vr.PRIZE_BLACKWAKE_STANDING}" in rows[3]
    if width >= 80:
        assert all(vr._visible_width(line) <= vr._page_content_width() for line in lines)
        # Three aligned columns: what each pays starts in one column.
        assert len({row.index(pay) for row, pay in zip(rows, ("+280cr", None, "Concord", "a favour owed")) if pay}) == 1
    assert vr.prize_action_bar("S/C/H/L") == "[S] Strip it [C] Take cargo [H] Hand over [L] Let go [<>] Page: "


def test_the_page_says_when_there_is_no_room_what_is_illegal_and_what_a_warrant_allows():
    world = _world_with_seed(42)
    pirate = _raider()
    commodity, quantity = vr.prize_cargo(world, pirate)
    other = next(c for c in vr.LEGAL_COMMODITIES if c != commodity)
    _add_cargo(world, other, vr.cargo_capacity(world.save.ship), unit_cost=5)
    text = " ".join(plain(line) for line in vr.prize_terms(world, pirate, vr.prize_actions(world, pirate)))
    assert "no room in the hold" in text and "[C]" not in text
    text = " ".join(plain(line) for line in vr.prize_terms(_world_with_seed(42), pirate, "S/C"))
    assert "A warrant's target is the Navy's already" in text and "[H]" not in text and "[L]" not in text
    # A hold of contraband says so before it is taken.
    world = _world_with_seed(42)
    day = next(day for day in range(200) if not vr.COMMODITIES[
        vr.prize_cargo(_at(world, day), pirate)[0]]["legal"])
    world.save.turn = day
    assert "Illegal" in " ".join(plain(line) for line in vr.prize_terms(world, pirate, "S/C/H/L"))


def _at(world, day):
    world.save.turn = day
    return world


@pytest.mark.parametrize("width,height", [(40, 12), (64, 20), (80, 24), (100, 30), (132, 43), (160, 48), (200, 60)])
def test_the_window_shows_the_wreck_while_the_question_is_open(terminal, width, height):
    """The contact dark and adrift, no lock, at every layout -- an ember that fell
    outside a shallow band once took the door down the moment a fight was won."""
    terminal(width, height)
    world = _world_with_exploration_choice("pirate")
    for name in ("Ashclaw", "Hollow Fang", "Grimwire", "Rust Wraith", "Nullshade", "Ravage"):
        pirate = _fight(world, _raider(name), hp=0)
        world.save.pending_travel["encounter"]["combat"].update(outcome="won", prize="open")
        scene = vr.canopy_scene(world)
        assert scene["kind"] == "combat" and scene["wreck"] is True and scene["sky"] != "alert"
        cells = vr.canopy_cells(scene, width, height)
        assert len(cells) == height and all(len(row) == width for row in cells)
        if width >= 132:
            assert "DEAD IN SPACE" in "".join(cell[0] for cell in cells[1])
    # Decided, the journey goes on: the window is the lane again.
    world.save.pending_travel["encounter"]["combat"]["prize"] = "S"
    assert vr.canopy_scene(world)["kind"] == "transit"


def test_the_scene_of_a_prize_is_a_ship_disabled_and_still_there():
    prize = vr._KillScene(80, 24, hull="Shuttle", patrol=False, name="Ashclaw", loot=0, seed="x")
    kill = vr._KillScene(80, 24, hull="Shuttle", patrol=False, name="Ashclaw", loot=160, seed="x")
    assert prize.title == "TARGET DISABLED" and kill.title == "TARGET DESTROYED"
    assert prize.detail[-1][0] == "dead in space" and kill.detail[-1][0] == "salvage +160cr"

    def lit(scene):
        """Cells of the last frame, right of the pilot's own hull, that carry anything."""
        return sum(1 for row in scene.frame(scene.frames - 1)[:12] for cell in row[40:] if cell[0] != " ")

    assert lit(prize) > lit(kill) + 20  # the hull is still there when the light goes


def test_status_lists_favours_owed_and_the_guide_says_what_a_wreck_is():
    world = _world_with_seed(42)
    world.save.turn = 10
    assert "FAVOURS OWED" not in " ".join(plain(line) for line in vr.pilot_record_lines(world, "O"))
    world.save.favours = [{"name": "Ashclaw", "day": 4}]
    text = " ".join(" ".join(plain(line) for line in vr.pilot_record_lines(world, "O")).split())
    assert "FAVOURS OWED TO YOU" in text
    assert f"Ashclaw let go on day 4; it will turn raiders away once, within {vr.FAVOUR_MEMORY_DAYS - 6} more days" in text
    assert any("A beaten raider is yours to decide about." in line for line in vr.pilot_guide_lines(world))
