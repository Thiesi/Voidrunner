"""What outlasts a hop: crew, workshops, faction arcs, the archive,
ranks, retirement and the Hall of Fame.

Split out of `test_voidrunner_domain.py` (issue #422).
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

import pytest

from .support import _Sys, _VOIDRUNNER_PATH, _add_cargo, _box_rows, _door_stopped_at, _escort_world, _finale_world, _set_cargo, _world_with_named_crew, _world_with_pending_fight, _world_with_seed, vr


def test_retire_pilot_increments_retirements_and_grants_cumulative_bonus():
    old_save = vr._new_career("Vet")
    old_save.pilot.retirements = 2
    old_save.pilot.credits = 300_000

    new_save = vr.retire_pilot(old_save)

    assert new_save.pilot.retirements == 3
    assert new_save.pilot.credits == 1200 + 3 * vr.RETIREMENT_STARTING_CREDITS_BONUS


def test_retire_pilot_resets_career_progress_and_keeps_handle():
    old_save = vr._new_career("Vet")
    old_save.pilot.handle = "Vet"
    old_save.pilot.credits = 300_000
    old_save.pilot.kills = 40
    old_save.pilot.missions_completed = 12
    old_save.pilot.notoriety = 9
    old_save.pilot.reputation["pirates"] = 5
    old_save.pilot.log.append("did something memorable")
    old_save.discovered = [0, 1, 2, 3]

    new_save = vr.retire_pilot(old_save)

    assert new_save.pilot.handle == "Vet"
    assert new_save.pilot.kills == 0
    assert new_save.pilot.missions_completed == 0
    assert new_save.pilot.notoriety == 0
    assert all(v == 0 for v in new_save.pilot.reputation.values())
    assert new_save.discovered == [0]
    assert any("Retired" in entry for entry in new_save.pilot.log)


def test_retire_pilot_rerolls_the_galaxy_seed():
    old_save = vr._new_career("Vet")
    original_seed = old_save.seed

    new_save = vr.retire_pilot(old_save)

    assert new_save.seed != original_seed


def test_pilot_from_dict_defaults_retirements_to_zero_for_old_saves():
    d = vr._new_career("Legacy").pilot.to_dict()
    del d["retirements"]

    pilot = vr.Pilot.from_dict(d)

    assert pilot.retirements == 0


def test_screen_status_shows_finale_requirements_without_ineligible_confirmation(monkeypatch):
    world = _world_with_seed(95)
    world.save.pilot.credits = 100
    keys = iter(["R", "S", "B", "B"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(vr, "confirm", lambda *args: pytest.fail("Ineligible retirement prompt"))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_status(vr.Palette(True), world)
    assert "Requires Retained top career rank" in output.getvalue()


def test_screen_status_retires_on_confirmation_at_top_rank(monkeypatch):
    world = _world_with_seed(96)
    world.save.pilot.credits = vr.RANKS[-1][0]
    old_seed = world.save.seed

    keys = iter(["R", "S", "Y"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_status(vr.Palette(truecolor=False), world)

    assert world.save.pilot.retirements == 1
    assert world.save.seed != old_seed


def test_screen_status_declines_retirement_without_committing(monkeypatch):
    world = _world_with_seed(97)
    world.save.pilot.credits = vr.RANKS[-1][0]
    old_seed = world.save.seed

    keys = iter(["R", "S", "N", "B", "B"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))

    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_status(vr.Palette(truecolor=False), world)

    assert "Retirement cancelled" in output.getvalue()
    assert world.save.pilot.retirements == 0
    assert world.save.seed == old_seed


def test_pilot_highlight_appends_and_caps_at_max_highlights():
    save = vr._new_career("Recorder")
    for i in range(vr.MAX_HIGHLIGHTS + 5):
        save.pilot.highlight(f"Event {i}")
    assert len(save.pilot.highlights) == vr.MAX_HIGHLIGHTS
    assert save.pilot.highlights[-1] == f"Event {vr.MAX_HIGHLIGHTS + 4}"
    assert save.pilot.highlights[0] == "Event 5"


def test_pilot_from_dict_defaults_highlights_and_rank_seen_for_old_saves():
    d = vr._new_career("Legacy").pilot.to_dict()
    del d["highlights"]
    del d["highest_rank_seen"]

    pilot = vr.Pilot.from_dict(d)

    assert pilot.highlights == []
    assert pilot.highest_rank_seen == 0


def test_check_rank_up_fires_once_per_rank_and_records_a_highlight():
    world = _world_with_seed(115)
    world.save.pilot.credits = vr.RANKS[1][0]

    title = vr.check_rank_up(world)
    assert title == vr.RANKS[1][1]
    assert world.save.pilot.highest_rank_seen == 1
    assert any("Promoted" in h for h in world.save.pilot.highlights)

    # Same rank again -- must not re-fire.
    assert vr.check_rank_up(world) is None


def test_check_rank_up_does_not_fire_for_a_fresh_career():
    world = _world_with_seed(116)
    assert vr.check_rank_up(world) is None


def test_check_rank_up_skips_ahead_correctly_on_a_big_jump():
    world = _world_with_seed(117)
    world.save.pilot.credits = vr.RANKS[-1][0]

    title = vr.check_rank_up(world)

    assert title == vr.RANKS[-1][1]
    assert world.save.pilot.highest_rank_seen == len(vr.RANKS) - 1


def test_first_kill_records_a_highlight_but_not_the_second(monkeypatch):
    world = _world_with_seed(118)
    pirate = vr.generate_pirate(world, tier=1)

    def win_the_fight(w, target):
        target.hp = 0
        return (0, 0, [])

    monkeypatch.setattr(vr, "tactical_round", lambda w, p, t, a: win_the_fight(w, p))
    monkeypatch.setattr(vr, "read_key", lambda: "F")

    with contextlib.redirect_stdout(io.StringIO()):
        outcome = vr.screen_combat(vr.Palette(truecolor=False), world, pirate)
    assert outcome == "won"
    assert sum("First kill" in h for h in world.save.pilot.highlights) == 1

    pirate2 = vr.generate_pirate(world, tier=1)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_combat(vr.Palette(truecolor=False), world, pirate2)
    assert sum("First kill" in h for h in world.save.pilot.highlights) == 1


def test_first_mission_completion_records_a_highlight():
    world = _world_with_seed(119)
    mission = vr.Mission(id=1, kind="delivery", description="Haul food", reward=100,
                          origin_system=0, target_system=0, commodity="food", quantity=1)
    world.save.active_missions.append(mission)
    _add_cargo(world, "food", 1)

    msgs = vr.check_mission_completions(world)

    assert msgs
    assert any("First mission" in h for h in world.save.pilot.highlights)


def test_hull_refit_records_a_highlight(monkeypatch):
    world = _world_with_seed(120)
    world.save.pilot.credits = 100_000
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)

    monkeypatch.setattr(vr,"read_key",lambda:"C")
    with contextlib.redirect_stdout(io.StringIO()):
        vr._hull_refit_screen(vr.Palette(truecolor=False), world, "Freighter", 5000)

    assert any("Freighter-class hull refit" in h for h in world.save.pilot.highlights)


def test_landmark_investigation_records_a_highlight(monkeypatch):
    keys = iter("IB")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    world = _world_with_seed(121)
    world.save.current_system = world.landmark["system_id"]

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_landmark(vr.Palette(truecolor=False), world)

    assert any(world.landmark["label"] in h for h in world.save.pilot.highlights)


def test_retire_pilot_records_a_highlight_on_the_new_career():
    old_save = vr._new_career("Vet")
    old_save.pilot.credits = vr.RANKS[-1][0]

    new_save = vr.retire_pilot(old_save)

    assert any("Retired" in h for h in new_save.pilot.highlights)


def test_screen_status_shows_career_highlights(monkeypatch):
    world = _world_with_seed(122)
    world.save.pilot.highlight("Something notable happened.")
    keys = iter(["H", "B"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_status(vr.Palette(truecolor=False), world)

    assert "Career highlights" in buf.getvalue()
    assert "Something notable happened." in buf.getvalue()


def test_station_menu_announces_a_promotion(monkeypatch):
    world = _world_with_seed(123)
    world.save.pilot.credits = vr.RANKS[1][0]
    world.checkpoint()
    keys = iter([" ", "Q"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)

    assert "Promoted to" in buf.getvalue()


def test_load_hall_of_fame_returns_empty_list_when_missing(tmp_path):
    assert vr.load_hall_of_fame(tmp_path) == []


def test_load_hall_of_fame_returns_empty_list_on_corrupt_file(tmp_path):
    (tmp_path / "leaderboard.json").write_text("not json{{{", encoding="utf-8")
    assert vr.load_hall_of_fame(tmp_path) == []


def test_load_hall_of_fame_returns_empty_list_when_not_a_list(tmp_path):
    (tmp_path / "leaderboard.json").write_text('{"oops": true}', encoding="utf-8")
    assert vr.load_hall_of_fame(tmp_path) == []


def test_update_hall_of_fame_creates_an_entry_for_a_new_pilot(tmp_path):
    save = vr._new_career("Newcomer")
    save.pilot.credits = 5000

    vr.update_hall_of_fame(tmp_path, 42, save)

    entries = vr.load_hall_of_fame(tmp_path)
    assert len(entries) == 1
    assert entries[0]["user_id"] == 42
    assert entries[0]["handle"] == "Newcomer"
    assert entries[0]["best_credits"] == 5000
    assert entries[0]["rank"] == vr.rank_for(5000)


def test_update_hall_of_fame_never_lowers_best_credits(tmp_path):
    save = vr._new_career("Vet")
    save.pilot.credits = 10_000
    vr.update_hall_of_fame(tmp_path, 1, save)

    save.pilot.credits = 500  # a losing streak, or a retirement's own reset
    vr.update_hall_of_fame(tmp_path, 1, save)

    entries = vr.load_hall_of_fame(tmp_path)
    assert entries[0]["best_credits"] == 10_000


def test_update_hall_of_fame_refreshes_non_credit_fields_every_time(tmp_path):
    save = vr._new_career("Vet")
    save.pilot.credits = 10_000
    vr.update_hall_of_fame(tmp_path, 1, save)

    save.pilot.credits = 500
    save.pilot.retirements = 3
    save.pilot.kills = 7
    vr.update_hall_of_fame(tmp_path, 1, save)

    entries = vr.load_hall_of_fame(tmp_path)
    assert entries[0]["retirements"] == 3
    assert entries[0]["kills"] == 7
    assert entries[0]["best_credits"] == 10_000  # still the high-water mark


def test_update_hall_of_fame_keeps_separate_pilots_separate(tmp_path):
    save_a = vr._new_career("Alice")
    save_a.pilot.credits = 3000
    save_b = vr._new_career("Bob")
    save_b.pilot.credits = 7000

    vr.update_hall_of_fame(tmp_path, 1, save_a)
    vr.update_hall_of_fame(tmp_path, 2, save_b)

    entries = vr.load_hall_of_fame(tmp_path)
    assert len(entries) == 2
    assert entries[0]["handle"] == "Bob"  # sorted by best_credits, descending
    assert entries[1]["handle"] == "Alice"


def test_update_hall_of_fame_caps_at_hall_of_fame_size(tmp_path):
    for uid in range(vr.HALL_OF_FAME_SIZE + 5):
        save = vr._new_career(f"Pilot{uid}")
        save.pilot.credits = uid
        vr.update_hall_of_fame(tmp_path, uid, save)

    entries = vr.load_hall_of_fame(tmp_path)
    assert len(entries) == vr.HALL_OF_FAME_SIZE
    # The lowest-credit pilots are omitted from the view, but retained on disk.
    assert entries[-1]["best_credits"] == 5


def test_persist_updates_both_the_save_and_the_hall_of_fame(tmp_path):
    save = vr._new_career("Persisted")
    save.pilot.credits = 4200
    world = vr.World(save)

    vr.persist(world, tmp_path, 7)

    assert (tmp_path / "7.json").exists()
    entries = vr.load_hall_of_fame(tmp_path)
    assert entries and entries[0]["user_id"] == 7


def test_screen_hall_of_fame_shows_no_pilots_message_when_empty(tmp_path, monkeypatch):
    world = _world_with_seed(139)
    monkeypatch.setattr(vr, "read_key", lambda: "B")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_hall_of_fame(vr.Palette(truecolor=False), world, tmp_path, 1)

    assert "No pilots recorded" in buf.getvalue()


def test_screen_hall_of_fame_marks_the_current_pilot(tmp_path, monkeypatch):
    save = vr._new_career("Me")
    save.pilot.credits = 9000
    vr.update_hall_of_fame(tmp_path, 5, save)
    other = vr._new_career("Someone Else")
    other.pilot.credits = 200
    vr.update_hall_of_fame(tmp_path, 6, other)

    world = _world_with_seed(140)
    monkeypatch.setattr(vr, "read_key", lambda: "B")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_hall_of_fame(vr.Palette(truecolor=False), world, tmp_path, 5)

    text = buf.getvalue()
    assert "Me" in text and "Someone Else" in text
    me_line = next(line for line in text.splitlines() if "Me" in line and "Someone" not in line)
    assert "[YOU]" in me_line


def test_ship_from_dict_defaults_crew_fields_for_old_saves():
    save = vr._new_career("Legacy")
    d = save.ship.to_dict()
    del d["has_gunner"], d["has_engineer"], d["has_navigator"]

    ship = vr.Ship.from_dict(d)

    assert not ship.has_gunner and not ship.has_engineer and not ship.has_navigator


def test_gunner_adds_flat_combat_damage(monkeypatch):
    world = _world_with_seed(148)
    pirate = vr.Pirate(name="Target", tier=0, hp=999, hp_max=999)
    world.event_rng.randint = lambda a, b: a  # pin the random roll for a clean comparison

    dmg_without, _, _ = vr.tactical_round(world, pirate, vr.new_tactics(pirate), "F")

    world.save.ship.has_gunner = True
    pirate2 = vr.Pirate(name="Target", tier=0, hp=999, hp_max=999)
    dmg_with, _, _ = vr.tactical_round(world, pirate2, vr.new_tactics(pirate2), "F")

    assert dmg_with == dmg_without + 3


def test_engineer_discounts_fuel_cost_but_never_below_one():
    world = _world_with_seed(149)
    a, b = world.by_id[0], world.by_id[world.by_id[0].connections[0]]
    base = vr.fuel_cost_for_jump(a, b)

    world.save.ship.has_engineer = True
    discounted = vr.fuel_cost_for_jump(a, b, world.save.ship)

    assert discounted == max(1, base - (base + 3) // 4)


def test_engineer_discount_never_goes_below_one_even_on_the_cheapest_jump():
    class _Sys:
        x = 0
        y = 0

    ship = vr.Ship(hull_class="Shuttle", hull_hp=60, fuel=24, has_engineer=True)
    assert vr.fuel_cost_for_jump(_Sys(), _Sys(), ship) == 1


def test_navigator_extends_scan_range(monkeypatch):
    world = _world_with_seed(150)
    world.save.ship.scanner_tier = 1

    without = 2 + world.save.ship.scanner_tier
    world.save.ship.has_navigator = True
    with_nav = 2 + world.save.ship.scanner_tier + (1 if world.save.ship.has_navigator else 0)

    assert with_nav == without + 1


def test_pay_crew_wages_deducts_for_each_hired_role():
    world = _world_with_seed(151)
    world.save.pilot.credits = 1000
    world.save.ship.has_gunner = True
    world.save.ship.has_navigator = True

    messages = vr.pay_crew_wages(world)

    assert messages == []
    expected = 1000 - vr.CREW_ROLES["gunner"]["wage"] - vr.CREW_ROLES["navigator"]["wage"]
    assert world.save.pilot.credits == expected
    assert world.save.ship.has_gunner and world.save.ship.has_navigator


def test_pay_crew_wages_resigns_a_crew_member_who_cant_be_paid():
    world = _world_with_seed(152)
    world.save.pilot.credits = 1
    world.save.ship.has_engineer = True

    messages = vr.pay_crew_wages(world)

    assert len(messages) == 1
    assert "resigns" in messages[0]
    assert not world.save.ship.has_engineer
    assert world.save.pilot.credits == 1  # never driven negative


def test_pay_crew_wages_never_drives_credits_negative():
    world = _world_with_seed(153)
    world.save.pilot.credits = 0
    world.save.ship.has_gunner = True
    world.save.ship.has_engineer = True
    world.save.ship.has_navigator = True

    vr.pay_crew_wages(world)

    assert world.save.pilot.credits == 0


def test_toggle_crew_hires_when_affordable(monkeypatch):
    world = _world_with_seed(154)
    world.save.pilot.credits = 10_000
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)

    with contextlib.redirect_stdout(io.StringIO()):
        vr._toggle_crew(vr.Palette(truecolor=False), world, "gunner")

    assert world.save.ship.has_gunner
    assert world.save.pilot.credits == 10_000 - vr.CREW_ROLES["gunner"]["hire_cost"]


def test_toggle_crew_refuses_when_unaffordable(monkeypatch):
    world = _world_with_seed(155)
    world.save.pilot.credits = 10
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)

    with contextlib.redirect_stdout(io.StringIO()):
        vr._toggle_crew(vr.Palette(truecolor=False), world, "gunner")

    assert not world.save.ship.has_gunner
    assert world.save.pilot.credits == 10


def test_toggle_crew_dismisses_on_confirmation(monkeypatch):
    world = _world_with_seed(156)
    world.save.ship.has_navigator = True
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)

    with contextlib.redirect_stdout(io.StringIO()):
        vr._toggle_crew(vr.Palette(truecolor=False), world, "navigator")

    assert not world.save.ship.has_navigator


def test_toggle_crew_keeps_crew_without_dismissal_confirmation(monkeypatch):
    world = _world_with_seed(157)
    world.save.ship.has_navigator = True
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: False)

    with contextlib.redirect_stdout(io.StringIO()):
        vr._toggle_crew(vr.Palette(truecolor=False), world, "navigator")

    assert world.save.ship.has_navigator


def test_screen_crew_lists_all_roles(monkeypatch):
    world = _world_with_seed(158)
    monkeypatch.setattr(vr, "read_key", lambda: "Q")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_crew(vr.Palette(truecolor=False), world)

    text = buf.getvalue()
    for info in vr.CREW_ROLES.values():
        assert info["label"] in text


def test_shipyard_offers_crew_option(monkeypatch):
    world = _world_with_seed(159)
    monkeypatch.setattr(vr, "read_key", lambda: "Q")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_shipyard(vr.Palette(truecolor=False), world)

    assert "[K] Crew" in buf.getvalue()


def test_shipyard_crew_row_letter_never_collides_with_an_upgrade_row():
    """Regression guard for a real dogfood-caught bug: Crew's own footer
    hotkey used to be "[C]", which is also Weapon Systems' own row
    letter (the 3rd of 6 UPGRADES entries) shown just above it on the
    same screen -- two different things both labeled "[C]" on one
    prompt, the exact ambiguity this file's own `refit_keys` comment
    already documents fixing once before."""
    upgrade_row_letters = set(vr.YARD_LETTERS[: len(vr.UPGRADES)])
    assert "K" not in upgrade_row_letters


def test_shipyard_k_key_opens_crew_screen(monkeypatch):
    world = _world_with_seed(160)
    keys = iter(["K", "Q", "Q"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_shipyard(vr.Palette(truecolor=False), world)

    assert "Crew Roster" in buf.getvalue()


def test_chart_screen_reserves_sgv_and_never_assigns_them_to_a_connection(monkeypatch):
    """Regression guard for a real dogfood-caught bug: `_connect_systems`'s
    own extra-edge pass can give a single system up to ~7 connections
    (seen across a few thousand random seeds), and a plain `LETTERS[i]`
    assignment would silently give the 7th one the same row letter as
    the "[G] Go to" hotkey -- permanently shadowing that connection,
    since "G" was checked as a fixed control key before ever falling
    through to the row lookup. Forces a system with 8 connections
    (more than "G"'s own position, 7th letter) and confirms the 8th
    one is both drawn with, and selectable via, a non-reserved letter."""
    world = _world_with_seed(190)
    world.here.connections = list(range(1, 9))  # 8 synthetic neighbors
    for sid in world.here.connections:
        world.by_id[sid].discovered = True
    world.save.ship.fuel = 999

    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_chart(vr.Palette(truecolor=False), world)

    for letter in "SGV":
        assert letter not in vr.CHART_CONNECTION_LETTERS[:8]

    eighth_letter = vr.CHART_CONNECTION_LETTERS[7]
    keys = iter([eighth_letter, "Y"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()):
        dest = vr.screen_chart(vr.Palette(truecolor=False), world)
    assert dest == 8  # the 8th synthetic connection, reachable via its own real letter


def test_screen_status_shows_hired_crew(monkeypatch):
    world = _world_with_seed(161)
    world.save.ship.has_gunner = True
    monkeypatch.setattr(vr, "read_key", lambda: "B")  # whitespace is absorbed at the prompt (#416)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_status(vr.Palette(truecolor=False), world)

    assert "Gunner" in buf.getvalue()


def test_retiring_resets_crew():
    old_save = vr._new_career("Vet")
    old_save.ship.has_gunner = True
    old_save.ship.has_engineer = True

    new_save = vr.retire_pilot(old_save)

    assert not new_save.ship.has_gunner and not new_save.ship.has_engineer


def test_pilot_from_dict_defaults_faction_arc_fields_for_old_saves():
    save = vr._new_career("Legacy")
    d = save.pilot.to_dict()
    del d["has_concord_commission"], d["has_blackwake_made"]

    pilot = vr.Pilot.from_dict(d)

    assert not pilot.has_concord_commission
    assert not pilot.has_blackwake_made


def test_concord_commission_unavailable_below_threshold():
    world = _world_with_seed(180)
    world.save.pilot.reputation[vr.FACTION_CONCORD] = vr.CONCORD_COMMISSION_THRESHOLD - 1
    assert not vr.concord_commission_available(world)


def test_concord_commission_available_at_threshold():
    world = _world_with_seed(181)
    world.save.pilot.reputation[vr.FACTION_CONCORD] = vr.CONCORD_COMMISSION_THRESHOLD
    assert vr.concord_commission_available(world)


def test_concord_commission_unavailable_once_already_held():
    world = _world_with_seed(182)
    world.save.pilot.reputation[vr.FACTION_CONCORD] = vr.CONCORD_COMMISSION_THRESHOLD
    world.save.pilot.has_concord_commission = True
    assert not vr.concord_commission_available(world)


def test_blackwake_made_available_at_threshold():
    world = _world_with_seed(183)
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = vr.BLACKWAKE_MADE_THRESHOLD
    assert vr.blackwake_made_available(world)


def test_blackwake_made_unavailable_once_already_held():
    world = _world_with_seed(184)
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = vr.BLACKWAKE_MADE_THRESHOLD
    world.save.pilot.has_blackwake_made = True
    assert not vr.blackwake_made_available(world)


def test_bounty_reward_for_applies_the_commission_bonus():
    world = _world_with_seed(185)
    assert vr.bounty_reward_for(world, 100) == 100

    world.save.pilot.has_concord_commission = True
    assert vr.bounty_reward_for(world, 100) == round(100 * (1 + vr.CONCORD_COMMISSION_BOUNTY_BONUS))


def test_screen_concord_commission_grants_perk_and_bonus_on_confirmation(monkeypatch):
    world = _world_with_seed(186)
    before_credits = world.save.pilot.credits
    world.save.pilot.reputation[vr.FACTION_CONCORD] = 75
    keys = iter("JB"); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_concord_commission(vr.Palette(truecolor=False), world)

    assert world.save.pilot.has_concord_commission
    assert world.save.pilot.credits == before_credits + vr.CONCORD_COMMISSION_BONUS_CREDITS
    assert any("privateer" in h.lower() for h in world.save.pilot.highlights)


def test_screen_concord_commission_declines_without_confirmation(monkeypatch):
    world = _world_with_seed(187)
    world.save.pilot.reputation[vr.FACTION_CONCORD] = 75
    keys = iter("JB"); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: False)

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_concord_commission(vr.Palette(truecolor=False), world)

    assert not world.save.pilot.has_concord_commission


def test_screen_blackwake_made_grants_perk_and_bonus_on_confirmation(monkeypatch):
    world = _world_with_seed(188)
    before_credits = world.save.pilot.credits
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = 75
    keys = iter("JB"); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)

    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_blackwake_made(vr.Palette(truecolor=False), world)

    assert world.save.pilot.has_blackwake_made
    assert world.save.pilot.credits == before_credits + vr.BLACKWAKE_MADE_BONUS_CREDITS


def test_station_menu_always_offers_faction_contacts(monkeypatch):
    world = _world_with_seed(189)
    monkeypatch.setattr(vr, "read_key", lambda: "Q")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)
    assert "[P]" in buf.getvalue() and "[W]" in buf.getvalue()

    world.save.pilot.reputation[vr.FACTION_CONCORD] = vr.CONCORD_COMMISSION_THRESHOLD
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = vr.BLACKWAKE_MADE_THRESHOLD
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)
    assert "[P]" in buf2.getvalue() and "[W]" in buf2.getvalue()


def test_retiring_resets_faction_arcs():
    old_save = vr._new_career("Vet")
    old_save.pilot.has_concord_commission = True
    old_save.pilot.has_blackwake_made = True

    new_save = vr.retire_pilot(old_save)

    assert not new_save.pilot.has_concord_commission
    assert not new_save.pilot.has_blackwake_made


def test_retirement_keeps_checkpoint_binding(tmp_path):
    world = vr.World(vr._new_career("Retiring"),
                     checkpoint=lambda current: vr.persist(current, tmp_path, 77))
    world.reset(vr.retire_pilot(world.save))
    world.checkpoint()
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Retiring")
    assert saved.pilot.retirements == 1


def test_simultaneous_processes_retain_every_pilot_score(tmp_path):
    import subprocess

    script = """
import runpy, sys
from pathlib import Path
vr = runpy.run_path(sys.argv[1])
uid = int(sys.argv[3])
directory = Path(sys.argv[2])
print('ready', flush=True)
sys.stdin.read(1)
with vr['pilot_session'](directory, uid):
    world = vr['World'](vr['_new_career']('Pilot' + str(uid)))
    for count in range(12):
        world.save.pilot.credits = uid * 1000 + count
        vr['persist'](world, directory, uid)
"""
    processes = []
    try:
        for uid in range(1, 9):
            processes.append(subprocess.Popen(
                [sys.executable, "-c", script, str(_VOIDRUNNER_PATH), str(tmp_path), str(uid)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for proc in processes:
            proc.stdin.write(b"x")
            proc.stdin.flush()
        for proc in processes:
            _, errors = proc.communicate(timeout=15)
            assert proc.returncode == 0, errors
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                pipe.close()
    entries = vr.load_hall_of_fame(tmp_path)
    assert {e["user_id"]: e["best_credits"] for e in entries} == {uid: uid * 1000 + 11 for uid in range(1, 9)}
    assert not list(tmp_path.rglob("*.tmp"))


def test_score_outside_top_twenty_retains_its_peak_on_return(tmp_path):
    veteran = vr._new_career("Veteran")
    veteran.pilot.credits = 9000
    vr.update_hall_of_fame(tmp_path, 1, veteran)
    for uid in range(2, 23):
        save = vr._new_career(f"Pilot{uid}")
        save.pilot.credits = 10_000 + uid
        vr.update_hall_of_fame(tmp_path, uid, save)
    assert all(e["user_id"] != 1 for e in vr.load_hall_of_fame(tmp_path))
    veteran.pilot.credits = 100
    vr.update_hall_of_fame(tmp_path, 1, veteran)
    # Temporarily reduce only the display population, as in a restored subset.
    retained = tmp_path / "subset"
    retained.mkdir()
    (retained / "scores").mkdir()
    (retained / "scores" / "1.json").write_bytes((tmp_path / "scores" / "1.json").read_bytes())
    assert vr.load_hall_of_fame(retained)[0]["best_credits"] == 9000


def test_the_old_leaderboard_is_imported_in_full_and_kept(tmp_path):
    """Careers do not survive schema 2; Hall of Fame records do (issue #421)."""
    import json

    rows = [{"user_id": 77, "handle": "Old", "best_credits": 9000, "kills": 2},
            {"user_id": 91, "handle": "Retired", "best_credits": 40_000, "retirements": 3}]
    path = tmp_path / "leaderboard.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    original = path.read_bytes()

    vr.import_hall_of_fame(tmp_path)

    assert path.read_bytes() == original  # never rewritten, so a retry is free
    imported = {entry["user_id"]: entry for entry in vr.load_hall_of_fame(tmp_path)}
    assert imported[77]["best_credits"] == 9000 and imported[77]["kills"] == 2
    assert imported[91]["best_credits"] == 40_000 and imported[91]["retirements"] == 3

    # A pilot who does launch keeps the imported floor and their own newer counters.
    world = _world_with_seed(42)
    world.save.pilot.handle = "Renamed"
    world.save.pilot.kills = 10
    vr.persist(world, tmp_path, 77)
    entry = next(e for e in vr.load_hall_of_fame(tmp_path) if e["user_id"] == 77)
    assert entry["best_credits"] == world.save.best_credits == 9000
    assert entry["handle"] == "Renamed" and entry["kills"] == 10


def test_importing_the_old_leaderboard_twice_changes_nothing_the_second_time(tmp_path):
    import json

    path = tmp_path / "leaderboard.json"
    path.write_text(json.dumps([{"user_id": 77, "handle": "Old", "best_credits": 9000}]), encoding="utf-8")
    vr.import_hall_of_fame(tmp_path)
    stored = (tmp_path / "scores" / "77.json").read_bytes()
    vr.import_hall_of_fame(tmp_path)
    assert (tmp_path / "scores" / "77.json").read_bytes() == stored


def test_failed_score_write_is_repaired_from_saved_peak_after_spending_and_retirement(tmp_path, monkeypatch):
    world = _world_with_seed(42)
    world.save.pilot.credits = 12_000
    replace = vr.os.replace

    def fail_score(source, target):
        if target.parent.name == "scores":
            raise OSError("score directory temporarily unavailable")
        return replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(vr.os, "replace", fail_score)
        vr.persist(world, tmp_path, 77)
    loaded, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert loaded.best_credits == 12_000
    assert vr.load_hall_of_fame(tmp_path) == []
    loaded.pilot.credits = 10
    retired = vr.retire_pilot(loaded)
    vr.persist(vr.World(retired), tmp_path, 77)
    assert vr.load_hall_of_fame(tmp_path)[0]["best_credits"] == 12_000
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize("malformed", [None, [], {"user_id": 77, "handle": [], "best_credits": 9},
                                      {"user_id": 77, "handle": "Bad", "best_credits": "9"}])
def test_malformed_score_entries_do_not_break_career_checkpoint(tmp_path, malformed):
    import json

    (tmp_path / "leaderboard.json").write_text(json.dumps([malformed]), encoding="utf-8")
    (tmp_path / "scores").mkdir()
    (tmp_path / "scores" / "77.json").write_text(json.dumps(malformed), encoding="utf-8")
    world = _world_with_seed(42)
    vr.persist(world, tmp_path, 77)
    assert vr.load_hall_of_fame(tmp_path)[0]["best_credits"] == world.save.pilot.credits


@pytest.mark.parametrize("field", ["market_drift", "active_missions", "next_mission_id", "flags",
                                   "contraband_standing_step"])
def test_a_career_missing_a_field_only_a_retired_schema_omitted_is_refused(field):
    """Those four defaulted for pre-overhaul careers; schema 2 writes them (#421)."""
    import json

    data = _world_with_seed(42).save.to_dict()
    data.pop(field)
    with pytest.raises(vr.ResumeError):
        vr._decode_career(json.dumps(data).encode())


def test_opening_quote_budgets_crew_and_return_fuel_before_acceptance():
    world = _world_with_seed(42)
    world.save.ship.has_gunner = True
    world.save.ship.has_engineer = True
    world.save.ship.fuel = 0
    offer = vr.opening_assignment_offer(world)
    assert offer is not None
    fuel = vr.fuel_cost_for_jump(world.here, world.by_id[offer.target_system], world.save.ship)
    price = vr.price_for(world, 0, offer.commodity)
    assert offer.reward == 3 * price + 12 * fuel + 2 * (15 + 2) + 200
    # Enough for three cheapest units alone is insufficient for the reserve/wages.
    world.save.pilot.credits = 3 * min(vr.price_for(world, 0, c) for c in vr.LEGAL_COMMODITIES)
    assert vr.opening_assignment_offer(world) is None


@pytest.mark.parametrize("style", list(vr.DISPLAY_STYLES))
def test_display_preference_roundtrip_restart_and_retirement(tmp_path, style):
    world = _world_with_seed(42)
    world.save.display_style = style
    vr.persist(world, tmp_path, 77)
    saved, is_new, notice = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert not is_new and notice is None and saved.display_style == style
    assert vr.retire_pilot(saved).display_style == style
    legacy = saved.to_dict()
    legacy.pop("display_style")
    vr._validate_save_document(legacy)
    assert vr.SaveData.from_dict(legacy).display_style == "auto"


# Optional archive story uses the seeded landmark and existing career persistence.
def _archive_world(stage="idle", *, ending="P", investigated=False):
    world = _world_with_seed(42)
    if investigated: world.save.flags["landmark_investigated"] = True
    if stage != "idle": vr.archive_action(world, "A")
    if stage in ("recovered", "complete"):
        world.save.current_system = world.landmark["system_id"]
        vr.archive_action(world, "I")
        world.save.current_system = 0
    if stage == "complete": vr.archive_action(world, ending)
    return world


@pytest.mark.parametrize("ending,reward,concord,blackwake", [("P", 500, 5, 0), ("S", 1500, -2, 5)])
@pytest.mark.parametrize("investigated", [False, True])
def test_archive_round_trip_preserves_rng_and_existing_salvage(ending, reward, concord, blackwake, investigated):
    import copy
    world = _world_with_seed(42)
    if investigated: world.save.flags["landmark_investigated"] = True
    world.save.active_missions = [vr.Mission(i+1, "bounty", "Existing", 500, 0, 1, pirate_tier=1) for i in range(vr.MAX_ACTIVE_MISSIONS)]
    missions = copy.deepcopy(world.save.active_missions)
    credits, turn, fuel, rng = world.save.pilot.credits, world.save.turn, world.save.ship.fuel, world.event_rng.getstate()
    vr.archive_action(world, "A")
    world.save.current_system = world.landmark["system_id"]
    text = " ".join(vr.archive_action(world, "I"))
    assert all(record in text for record in vr.ARCHIVE_RECORDS[world.landmark["label"]])
    world.save.current_system = 0
    vr.archive_action(world, ending)
    assert world.save.pilot.credits == credits + reward + (0 if investigated else 3000)
    assert world.save.pilot.reputation[vr.FACTION_CONCORD] == concord
    assert world.save.pilot.reputation[vr.FACTION_BLACKWAKE] == blackwake
    assert world.save.pilot.missions_completed == 1 and vr.archive_finished(world)
    assert world.save.active_missions == missions
    assert (world.save.turn, world.save.ship.fuel, world.event_rng.getstate()) == (turn, fuel, rng)
    assert any("Archive:" in line for line in world.save.pilot.highlights)
    loaded = vr.World(vr.SaveData.from_dict(world.save.to_dict()))
    before = copy.deepcopy(loaded.save.to_dict())
    for action in ("A", "I", "P", "S"):
        with pytest.raises(ValueError, match="already complete"): vr.archive_action(loaded, action)
        assert loaded.save.to_dict() == before
    assert ("Families" if ending == "P" else "Kest Rel") in " ".join(vr.archive_lines(loaded))


@pytest.mark.parametrize("stage,location,action", [
    ("idle", "site", "A"), ("idle", "site", "I"), ("idle", "home", "P"),
    ("started", "home", "A"), ("started", "home", "I"), ("started", "home", "S"),
    ("recovered", "site", "P"), ("recovered", "site", "S"), ("recovered", "site", "I"),
    ("idle", "home", "?"),
])
def test_archive_invalid_decisions_are_atomic(stage, location, action):
    import copy
    world = _archive_world(stage)
    world.save.current_system = world.landmark["system_id"] if location == "site" else 0
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    with pytest.raises(ValueError): vr.archive_action(world, action)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


@pytest.mark.parametrize("flags", [
    {"archive_v1_recovered": True},
    {"archive_v1_started": True, "archive_v1_recovered": True},
    {"archive_v1_public": True}, {"archive_v1_private": True},
    {"archive_v1_started": True, "archive_v1_recovered": True, "landmark_investigated": True,
     "archive_v1_public": True, "archive_v1_private": True},
    {"archive_v1_started": "yes"},
])
def test_invalid_archive_progress_preserves_original_career(tmp_path, flags):
    import json
    data = _world_with_seed(42).save.to_dict(); data["flags"] = flags
    path = tmp_path / "77.json"; path.write_text(json.dumps(data), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(vr.ResumeError): vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == original


@pytest.mark.parametrize("stage", ["idle", "started", "recovered", "complete"])
@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
@pytest.mark.parametrize("style", ["auto", "plain"])
def test_archive_contact_pages_preserve_all_terms_without_writes(monkeypatch, terminal, without_action_bar, stage, width, height, style):
    import copy,re
    world = _archive_world(stage)
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    terminal(width, height, style)
    output, frames = io.StringIO(), []
    world._checkpoint = lambda w: pytest.fail("Browsing archive wrote a checkpoint")
    def choose():
        frame = output.getvalue(); output.seek(0); output.truncate(0); frames.append(frame)
        assert "[B] Back" in " ".join(vr._ANSI_RE.sub("",frame).split())
        assert len(frame.splitlines()) <= height
        assert all(vr._visible_width(row) <= width for row in frame.splitlines())
        assert world.save.to_dict() == before and world.event_rng.getstate() == rng
        page, count = map(int, re.search(r"Archive.*?(\d+)/(\d+)", frame, re.S).groups())
        return "B" if page == count else ">"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output): vr.screen_archive(vr.Palette(False), world)
    bodies=[]
    for frame in frames:
        plain=vr._ANSI_RE.sub("",frame)
        body=plain[re.search(r"Archive.*?\d+/\d+",plain,re.S).end():]
        bodies.append(without_action_bar(body))
    assert " ".join(" ".join(bodies).split())==" ".join(" ".join(vr.archive_lines(world)).split())


@pytest.mark.parametrize("commands,site", [(b"N", False), (b"N?><BQ", False), (b"NRBBQ", True), (b"L", True), (b"L><BQ", True)])
def test_real_archive_and_landmark_browsing_preserve_career_bytes(tmp_path, commands, site):
    import json,os,subprocess
    world = _world_with_seed(42)
    if site:
        world.save.current_system = world.landmark["system_id"]
        world.by_id[world.here.id].discovered = True
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    before = (tmp_path / "77.json").read_bytes()
    info = tmp_path / "door_info.json"; info.write_text(json.dumps({"user_id": 77, "handle": "Tester", "terminal_width": 40, "terminal_height": 12}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=commands, capture_output=True, timeout=10,
        env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)))
    assert result.returncode == 0 and not result.stderr
    assert (b"Archive" if commands.startswith(b"N") else b"Unclaimed salvage") in result.stdout
    assert (tmp_path / "77.json").read_bytes() == before


@pytest.mark.parametrize("stage,key,flag,ack", [
    ("idle", b"A", "archive_v1_started", b"assignment accepted."),
    ("started", b"I", "archive_v1_recovered", b"Salvage recovered:"),
    ("recovered", b"P", "archive_v1_public", b"Archive complete:"),
    ("recovered", b"S", "archive_v1_private", b"Archive complete:"),
])
def test_real_archive_actions_save_before_ack_and_cannot_replay(tmp_path, stage, key, flag, ack):
    import os,subprocess
    world = _archive_world(stage)
    if stage == "started":
        world.save.current_system = world.landmark["system_id"]
        world.by_id[world.here.id].discovered = True
    world.save.pilot.highest_rank_seen = len(vr.RANKS) - 1
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    with _door_stopped_at(tmp_path, b"N" + key, ack):
        saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert saved.flags[flag] is True
    before = (tmp_path / "77.json").read_bytes()
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=b"N" + key + b"BQ", capture_output=True, timeout=10,
        env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(tmp_path / "door_info.json")))
    assert result.returncode == 0 and not result.stderr
    assert (tmp_path / "77.json").read_bytes() == before


def test_archive_bearing_routes_only_accepted_site_without_charting_or_rng():
    import copy
    world = _world_with_seed(42); target = world.landmark["system_id"]
    world.by_id[target].discovered = False
    with pytest.raises(vr.MissionError, match="charted"): vr.prepare_route_jump(world, target)
    vr.archive_action(world, "A")
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    assert vr.prepare_route_jump(world, target) == vr.bfs_path(world.by_id, 0, target)[0]
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng
    assert not world.by_id[target].discovered
    other = next(sid for sid in world.by_id if sid not in (0, target)); world.by_id[other].discovered = False
    with pytest.raises(vr.MissionError, match="charted"): vr.prepare_route_jump(world, other)
    world.save.ship.fuel = 0
    with pytest.raises(vr.MissionError, match="Not enough fuel"): vr.prepare_route_jump(world, target)


def test_archive_record_depends_on_existing_landmark_and_never_awards_salvage_twice():
    seen = set()
    for seed in range(30):
        world = _world_with_seed(seed); label = world.landmark["label"]; seen.add(label)
        world.save.current_system = world.landmark["system_id"]
        vr.investigate_landmark(world)
        with pytest.raises(ValueError, match="no unclaimed"): vr.investigate_landmark(world)
        world.save.current_system = 0; vr.archive_action(world, "A")
        world.save.current_system = world.landmark["system_id"]
        result = vr.archive_action(world, "I")
        assert all(line in result for line in vr.ARCHIVE_RECORDS[label])
        assert world.save.pilot.credits == 4200
    assert seen == set(vr.ARCHIVE_RECORDS)


@pytest.mark.parametrize("ending", ["P", "S"])
def test_archive_story_ui_completes_real_round_trip_and_spends_normal_fuel(monkeypatch, tmp_path, ending):
    world = _world_with_seed(42)
    target = world.landmark["system_id"]
    outward = vr.bfs_path(world.by_id, 0, target); homeward = vr.bfs_path(world.by_id, target, 0)
    commands = iter("AR" + "J" * len(outward) + "BIR" + "J" * len(homeward) + "B" + ending + "B")
    monkeypatch.setattr(vr, "read_key", lambda: next(commands))
    # Fix only the chance draws; real departure, docking and costs still execute.
    monkeypatch.setattr(world.event_rng, "random", lambda: 0.99)
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    with contextlib.redirect_stdout(io.StringIO()): vr.screen_archive(vr.Palette(False), world)
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert saved.current_system == 0 and saved.turn == len(outward) + len(homeward) == 10
    assert saved.ship.fuel == 10 and saved.pilot.missions_completed == 1
    assert saved.pilot.credits == 1200 + 3000 + (500 if ending == "P" else 1500)
    assert saved.pending_travel is None and saved.flags["archive_v1_public" if ending == "P" else "archive_v1_private"]
    assert target in saved.discovered


@pytest.mark.parametrize("stage,key,ack", [("idle", "A", "assignment accepted"), ("started", "I", "Salvage recovered"), ("recovered", "P", "Archive complete")])
def test_archive_checkpoint_failure_stops_before_acknowledgement(monkeypatch, stage, key, ack):
    world = _archive_world(stage)
    if stage == "started": world.save.current_system = world.landmark["system_id"]
    output = io.StringIO()
    def fail(current):
        assert ack not in output.getvalue()
        raise vr.SaveError()
    world._checkpoint = fail
    monkeypatch.setattr(vr, "read_key", lambda: key)
    with contextlib.redirect_stdout(output), pytest.raises(vr.SaveError): vr.screen_archive(vr.Palette(False), world)
    assert ack not in output.getvalue()


def test_archive_unfinished_objective_appears_on_station_and_returning_recap():
    world = _archive_world("started")
    for stage in ("started", "recovered"):
        if stage == "recovered":
            world.save.current_system = world.landmark["system_id"]; vr.archive_action(world, "I"); world.save.current_system = 0
        for lines in (vr.station_deck_lines(world), vr.pilot_recap(world)):
            assert vr.archive_objective(world) in " ".join(lines)
    vr.archive_action(world, "P")
    assert not any(line.startswith("Archive:") for line in vr.pilot_recap(world))
    assert not any(key.startswith("archive_v1") for key in vr.retire_pilot(world.save).flags)


def test_archive_route_map_includes_uncharted_accepted_bearing(monkeypatch):
    world = _archive_world("started"); target = world.landmark["system_id"]
    assert not world.by_id[target].discovered
    keys = iter("VB"); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    calls = []
    monkeypatch.setattr(vr, "screen_galaxy_map", lambda p, w, **kwargs: calls.append(kwargs))
    with contextlib.redirect_stdout(io.StringIO()): vr.screen_auto_route(vr.Palette(False), world, destination=target)
    assert calls == [{"path": vr.bfs_path(world.by_id, 0, target), "public_target": target}]
    assert not world.by_id[target].discovered


# Specialist workshops are derived from existing stations and consume real cargo.
def _world_at_workshop(key, tier=0):
    world = _world_with_seed(42)
    world.save.current_system = vr.specialist_stations(world)[key]
    world.by_id[world.here.id].discovered = True
    world.save.pilot.credits = 100_000
    world.save.pilot.highest_rank_seen = len(vr.RANKS) - 1
    setattr(world.save.ship, key + "_tier", tier)
    quote = vr.workshop_quote(world, key)
    vr._acquire_cargo(world, quote["commodity"], quote["quantity"],
                      quote["quantity"] * vr.price_for(world, world.here.id, quote["commodity"]))
    return world


@pytest.mark.parametrize("key,tier", [("cargo", t) for t in range(5)] + [("engine", t) for t in range(3)] + [("scanner", t) for t in range(2)])
def test_workshop_installation_uses_materials_and_cash_for_one_normal_tier(key, tier):
    import copy
    world = _world_at_workshop(key, tier)
    quote = vr.workshop_quote(world, key)
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    message = vr.install_workshop_module(world, key)
    assert getattr(world.save.ship, key + "_tier") == tier + 1
    assert world.save.pilot.credits == before["pilot"]["credits"] - (vr.UPGRADES[key]["cost"](tier) * 65 + 99) // 100
    assert world.save.cargo.get(quote["commodity"], 0) == 0
    assert world.save.trading_ledger.workshop_spend == quote["credits"]
    assert world.save.trading_ledger.workshop_material_cost == before["cargo_basis"][quote["commodity"]][0][1]
    assert world.save.trading_ledger.cargo_loss_cost == 0
    assert world.save.pilot.missions_completed == 0 and world.save.pilot.reputation == before["pilot"]["reputation"]
    assert world.save.ship.fuel == before["ship"]["fuel"] and world.save.ship.hull_hp == before["ship"]["hull_hp"]
    assert world.save.turn == before["turn"] and world.event_rng.getstate() == rng
    assert vr.WORKSHOPS[key]["owner"] in message and message in world.save.pilot.highlights
    vr.SaveData.from_dict(world.save.to_dict())


@pytest.mark.parametrize("key", ["cargo", "engine", "scanner"])
@pytest.mark.parametrize("condition", ["remote", "credits", "materials", "maxed", "journey", "invalid"])
def test_workshop_rejected_installation_preserves_everything(key, condition):
    import copy
    world = _world_at_workshop(key)
    if condition == "remote": world.save.current_system = 0
    elif condition == "credits": world.save.pilot.credits = vr.workshop_quote(world, key)["credits"] - 1
    elif condition == "materials": world.save.cargo.clear()
    elif condition == "maxed": setattr(world.save.ship, key + "_tier", vr.UPGRADES[key]["max_tier"])
    elif condition == "journey": world.save.pending_travel = {"phase": "primary"}
    elif condition == "invalid": key = "weapon"
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    with pytest.raises(ValueError): vr.install_workshop_module(world, key)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


def test_workshop_material_accounting_consumes_fifo_without_fake_loss():
    world = _world_at_workshop("engine", 1)
    world.save.cargo = {"machinery": 5}; world.save.cargo_basis = {"machinery": [[5, 1250]]}
    vr.install_workshop_module(world, "engine")
    assert world.save.cargo == {"machinery": 1} and world.save.cargo_basis == {"machinery": [[1, 250]]}
    ledger = world.save.trading_ledger
    assert (ledger.workshop_material_cost, ledger.workshop_spend) == (1000, 1430)
    assert ledger.cargo_loss_cost == ledger.sales_cost == ledger.delivery_cost == 0
    assert "materials 1,000cr recorded cost" in " ".join(vr.trading_ledger_lines(world))
    restored = vr.World(vr.SaveData.from_dict(world.save.to_dict()))
    assert restored.save.trading_ledger == ledger


def test_retirement_clears_the_workshop_counters():
    fields = ("workshop_spend", "workshop_material_cost")
    upgraded = _world_at_workshop("cargo"); vr.install_workshop_module(upgraded, "cargo")
    assert any(getattr(upgraded.save.trading_ledger, field) for field in fields)
    retired = vr.retire_pilot(upgraded.save)
    assert retired.ship.cargo_tier == 0 and all(getattr(retired.trading_ledger, field) == 0 for field in fields)


@pytest.mark.parametrize("key", ["cargo", "engine", "scanner"])
@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
@pytest.mark.parametrize("style", ["auto", "plain"])
def test_workshop_detail_pages_keep_terms_and_leave_career_untouched(monkeypatch, terminal, without_action_bar, key, width, height, style):
    import copy,re
    world = _world_at_workshop(key)
    terminal(width, height, style)
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    output, contents = io.StringIO(), []
    world._checkpoint = lambda w: pytest.fail("Workshop browsing checkpointed")
    def choose():
        frame = output.getvalue(); output.seek(0); output.truncate(0)
        assert len(frame.splitlines()) <= height
        assert all(vr._visible_width(row) <= width for row in frame.splitlines())
        assert world.save.to_dict() == before and world.event_rng.getstate() == rng
        plain = vr._ANSI_RE.sub("", frame)
        match = re.search(r"Workshop\s+[\d,]+cr\s+(\d+)/(\d+)", plain)
        page, count = map(int, match.groups())
        payload = plain[match.end():]
        payload = without_action_bar(payload)
        contents.append(payload.strip().removeprefix(">").strip())
        return "B" if page == count else ">"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output): assert vr.screen_workshop(vr.Palette(False), world, key) is None
    text = " ".join(" ".join(contents).split())
    for line in vr.workshop_lines(world, key): assert " ".join(line.split()) in text


@pytest.mark.parametrize("key,number", [("cargo", b"1"), ("engine", b"2"), ("scanner", b"3")])
def test_real_workshop_installation_saves_costs_and_tier_before_ack(tmp_path, key, number):
    import os,subprocess
    world = _world_at_workshop(key)
    quote = vr.workshop_quote(world, key)
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    ack = (vr.WORKSHOPS[key]["owner"] + " installed").encode()
    with _door_stopped_at(tmp_path, b"YS" + number + b"IY", ack):
        saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert getattr(saved.ship, key + "_tier") == 1
        assert saved.pilot.credits == 100_000 - quote["credits"]
        assert not saved.cargo and saved.trading_ledger.workshop_spend == quote["credits"]
    before = (tmp_path / "77.json").read_bytes()
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=b"YS" + number + b"BBQQ", capture_output=True, timeout=10,
        env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(tmp_path / "door_info.json")))
    assert result.returncode == 0 and not result.stderr
    assert (tmp_path / "77.json").read_bytes() == before


@pytest.mark.parametrize("fail", [False, True])
def test_workshop_confirmation_cancel_or_save_failure_cannot_acknowledge_installation(monkeypatch, fail):
    import copy
    world = _world_at_workshop("cargo"); before = copy.deepcopy(world.save.to_dict())
    output = io.StringIO(); keys = iter("IB")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(vr, "confirm", lambda *args: fail)
    def checkpoint(w):
        assert "Iona Rusk installed" not in output.getvalue()
        raise vr.SaveError()
    world._checkpoint = checkpoint
    with contextlib.redirect_stdout(output):
        if fail:
            with pytest.raises(vr.SaveError): vr.screen_workshop(vr.Palette(False), world, "cargo")
        else:
            vr.screen_workshop(vr.Palette(False), world, "cargo")
            assert world.save.to_dict() == before
    assert "Iona Rusk installed" not in output.getvalue()


def test_workshop_discloses_and_consumes_materials_promised_to_a_delivery():
    world = _world_at_workshop("cargo")
    world.save.active_missions = [vr.Mission(1, "delivery", "Promised metal", 500, 0, 1, commodity="metals", quantity=2)]
    assert "cargo promised to delivery contracts" in " ".join(vr.workshop_lines(world, "cargo"))
    vr.install_workshop_module(world, "cargo")
    assert not world.save.cargo and len(world.save.active_missions) == 1
    assert world.save.pilot.missions_completed == 0


def test_crew_candidate_preview_is_stable_without_save_or_rng_changes():
    import copy,dataclasses
    for seed in range(24):
        world = _world_with_seed(seed)
        before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
        galaxy = [dataclasses.asdict(s) for s in world.galaxy]
        names = [vr.crew_name(world, role) for role in vr.CREW_ROLES]
        assert len(set(names)) == 3
        for _ in range(3): vr.crew_roster_lines(world)
        assert world.save.to_dict() == before and world.event_rng.getstate() == rng
        assert [dataclasses.asdict(s) for s in world.galaxy] == galaxy
        restored = vr.World(vr.SaveData.from_dict(before))
        assert [vr.crew_name(restored, role) for role in vr.CREW_ROLES] == names


@pytest.mark.parametrize("role", ["gunner", "engineer", "navigator"])
@pytest.mark.parametrize("paid,level", [(0, 0), (4, 0), (5, 1), (14, 1), (15, 2), (29, 2), (30, 3)])
def test_paid_service_promotes_only_at_threshold_and_stops_at_mastery(role, paid, level):
    world = _world_with_named_crew(role, paid)
    before, rng = world.save.pilot.credits, world.event_rng.getstate()
    assert vr.crew_level(world.save.ship, role) == level
    world.save.turn += 1
    messages = vr.pay_crew_wages(world)
    new_paid = min(30, paid + 1)
    assert world.save.ship.crew_records[role]["paid_jumps"] == new_paid
    assert world.save.pilot.credits == before - vr.CREW_ROLES[role]["wage"]
    assert world.event_rng.getstate() == rng
    promoted = paid in (4, 14, 29)
    assert bool(messages) == promoted
    if promoted:
        assert vr.crew_name(world, role) in messages[0]
        assert messages[0] in world.save.pilot.highlights
    restored = vr.World(vr.SaveData.from_dict(world.save.to_dict()))
    assert restored.save.ship.crew_records == world.save.ship.crew_records


@pytest.mark.parametrize("role", ["gunner", "engineer", "navigator"])
def test_dismissal_unpaid_resignation_and_rehire_preserve_named_service(role):
    import copy
    world = _world_with_named_crew(role, 15)
    record = copy.deepcopy(world.save.ship.crew_records[role]); name = vr.crew_name(world, role)
    before = world.save.pilot.credits
    vr.dismiss_crew(world, role)
    assert world.save.ship.crew_records[role] == record
    assert [vr.gunner_bonus, vr.engineer_discount, vr.navigator_bonus][list(vr.CREW_ROLES).index(role)](world.save.ship) == 0
    vr.hire_crew(world, role)
    assert world.save.pilot.credits == before - vr.CREW_ROLES[role]["hire_cost"]
    assert vr.crew_name(world, role) == name and world.save.ship.crew_records[role] == record
    world.save.pilot.credits = vr.CREW_ROLES[role]["wage"] - 1
    messages = vr.pay_crew_wages(world)
    assert not getattr(world.save.ship, "has_" + role)
    assert world.save.ship.crew_records[role] == record and name in messages[0]


@pytest.mark.parametrize("role", ["gunner", "engineer", "navigator"])
def test_crew_without_a_service_record_keeps_the_base_bonus_until_paid(role):
    world = _world_with_seed(42); setattr(world.save.ship, "has_" + role, True)
    data = world.save.to_dict(); data["ship"].pop("crew_records")
    restored = vr.World(vr.SaveData.from_dict(data))
    assert restored.save.ship.crew_records == {}
    vr.crew_roster_lines(restored)
    assert restored.save.ship.crew_records == {} and vr.crew_level(restored.save.ship, role) == 0
    restored.save.turn += 1
    vr.pay_crew_wages(restored)
    assert restored.save.ship.crew_records[role]["paid_jumps"] == 1
    assert vr.crew_level(restored.save.ship, role) == 0


@pytest.mark.parametrize("condition", ["unknown", "already", "money", "journey", "dismiss_absent"])
def test_invalid_crew_employment_decisions_are_atomic(condition):
    import copy
    world = _world_with_seed(42); role = "gunner"
    if condition == "unknown": role = "captain"
    elif condition == "already": vr.hire_crew(world, role)
    elif condition == "money": world.save.pilot.credits = 0
    elif condition == "journey": world.save.pending_travel = {"phase": "primary"}
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    with pytest.raises(ValueError):
        (vr.dismiss_crew if condition == "dismiss_absent" else vr.hire_crew)(world, role)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


@pytest.mark.parametrize("records", [None, [], {"captain": {}}, {"gunner": None},
    {"gunner": {"version": 2, "identity": 0, "paid_jumps": 0}},
    {"gunner": {"version": True, "identity": 0, "paid_jumps": 0}},
    {"gunner": {"version": 1, "identity": -1, "paid_jumps": 0}},
    {"gunner": {"version": 1, "identity": 3, "paid_jumps": 0}},
    {"gunner": {"version": 1, "identity": 0, "paid_jumps": 31}},
    {"gunner": {"version": 1, "identity": 0, "paid_jumps": True}},
    {"gunner": {"version": 1, "identity": 0}},
    {"gunner": {"version": 1, "identity": 0, "paid_jumps": 0, "future": True}},
])
def test_invalid_named_crew_records_preserve_original_career(tmp_path, records):
    import json
    data = _world_with_seed(42).save.to_dict(); data["ship"]["crew_records"] = records
    path = tmp_path / "77.json"; path.write_text(json.dumps(data), encoding="utf-8"); before = path.read_bytes()
    with pytest.raises(vr.ResumeError): vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == before


@pytest.mark.parametrize("paid,bonus", [(0, 3), (5, 4), (15, 5), (30, 6)])
def test_promoted_gunner_bonus_matches_actual_fire_and_combat_info(monkeypatch, paid, bonus):
    world = _world_with_named_crew("gunner", paid)
    pirate = vr.Pirate("Hollow Fang", 0, 80, 80)
    monkeypatch.setattr(world.event_rng, "randint", lambda low, high: low)
    damage, _, _ = vr.tactical_round(world, pirate, vr.new_tactics(pirate), "F")
    assert damage == 9 + bonus
    assert f"gunner bonus +{bonus}" in " ".join(vr.combat_display_lines(world, pirate, [], patrol=False, details=True, tactics=vr.new_tactics(pirate)))


@pytest.mark.parametrize("paid,percent", [(0, 25), (5, 30), (15, 35), (30, 40)])
def test_promoted_engineer_savings_match_declared_rounding_and_never_zero(paid, percent):
    world = _world_with_named_crew("engineer", paid)
    a, b = world.here, world.by_id[world.here.connections[0]]
    a.x = a.y = b.y = 0
    for base in range(1, 17):
        b.x = base * 6
        assert vr.fuel_cost_for_jump(a, b, world.save.ship) == max(1, base - (base * percent + 99) // 100)
    assert f"-{percent}%" in " ".join(vr.crew_roster_lines(world))


@pytest.mark.parametrize("paid,bonus", [(0, 1), (5, 2), (15, 3), (30, 4)])
def test_promoted_navigator_survey_range_matches_actual_contacts(paid, bonus):
    world = _world_with_named_crew("navigator", paid); world.save.ship.scanner_tier = 1
    hops = vr.bfs_hops(world.by_id, 0)
    expected = {sid for sid, distance in hops.items() if distance <= 3 + bonus and not world.by_id[sid].discovered}
    assert set(vr.survey_candidates(world)) == expected
    assert f"+{bonus} survey hops" in " ".join(vr.crew_roster_lines(world))


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
@pytest.mark.parametrize("style", ["auto", "plain"])
def test_named_crew_roster_keeps_personality_progress_and_costs_without_writes(monkeypatch, terminal, width, height, style):
    import copy,re
    world = _world_with_named_crew("gunner", 14)
    vr.hire_crew(world, "navigator"); world.save.ship.crew_records["navigator"]["paid_jumps"] = 5
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    terminal(width, height, style)
    output, frames = io.StringIO(), []
    world._checkpoint = lambda w: pytest.fail("Crew browsing checkpointed")
    def choose():
        frame = output.getvalue(); output.seek(0); output.truncate(0); frames.append(frame)
        assert "[1-3] Task" in " ".join(vr._ANSI_RE.sub("",frame).split())
        assert len(frame.splitlines()) <= height
        assert all(vr._visible_width(row) <= width for row in frame.splitlines())
        assert world.save.to_dict() == before and world.event_rng.getstate() == rng
        page, count = map(int, re.search(r"Crew Roster:.*?(\d+)/(\d+)", frame, re.S).groups())
        return "Q" if page == count else ">"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output): vr.screen_crew(vr.Palette(False), world)
    text = " ".join(" ".join(frames).split())
    for role in vr.CREW_ROLES: assert vr.crew_name(world, role) in text
    assert "14/15" in text and "5/15" in text and "cr/jump" in text


def test_engineer_promotion_starts_fuel_savings_on_following_departure(monkeypatch):
    world = _world_with_named_crew("engineer", 4)
    destination = world.here.connections[0]
    a, b = world.here, world.by_id[destination]; a.x = a.y = b.y = 0; b.x = 24
    assert vr.fuel_cost_for_jump(a, b, world.save.ship) == 3
    monkeypatch.setattr(world.event_rng, "random", lambda: 0.99)
    with contextlib.redirect_stdout(io.StringIO()): vr.screen_travel(vr.Palette(False), world, destination)
    assert world.save.ship.fuel == 21 and vr.crew_level(world.save.ship, "engineer") == 1
    assert vr.fuel_cost_for_jump(b, a, world.save.ship) == 2


@pytest.mark.parametrize("role", ["gunner", "engineer", "navigator"])
def test_real_paid_service_promotion_is_saved_before_ack_and_not_replayed(tmp_path, role):
    import os,subprocess
    world = _world_with_named_crew(role, 4)
    world.event_rng.seed(100)
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    with _door_stopped_at(tmp_path, b"CAY", b"is now Seasoned"):
        saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert saved.ship.crew_records[role]["paid_jumps"] == 5 and saved.turn == 5
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=b"IQQ", capture_output=True, timeout=10,
        env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(tmp_path / "door_info.json")))
    assert result.returncode == 0 and not result.stderr
    resumed, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert resumed.ship.crew_records[role]["paid_jumps"] == 5


def test_retirement_clears_named_crew_and_service():
    world = _world_with_named_crew("gunner", 30)
    retired = vr.retire_pilot(world.save)
    assert retired.ship.crew_records == {} and not retired.ship.has_gunner


@pytest.mark.parametrize("role", ["gunner", "engineer", "navigator"])
def test_crew_service_survives_every_serialized_departure_checkpoint(monkeypatch, role):
    import copy,json
    world = _world_with_named_crew(role, 4); destination = world.here.connections[0]
    monkeypatch.setattr(world.event_rng, "random", lambda: 0.99)
    snapshots = []; world._checkpoint = lambda w: snapshots.append(json.loads(json.dumps(w.save.to_dict())))
    with contextlib.redirect_stdout(io.StringIO()): vr.screen_travel(vr.Palette(False), world, destination)
    expected = copy.deepcopy(world.save.to_dict())
    assert len(snapshots) >= 4
    for snapshot in snapshots:
        if snapshot["pending_travel"] is None: continue
        restored = vr.World(vr.SaveData.from_dict(snapshot))
        monkeypatch.setattr(restored.event_rng, "random", lambda: 0.99)
        with contextlib.redirect_stdout(io.StringIO()): vr.screen_travel(vr.Palette(False), restored, destination)
        assert restored.save.to_dict() == expected
        assert restored.save.ship.crew_records[role]["paid_jumps"] == 5


def test_crew_promotion_save_failure_stops_before_narration(monkeypatch):
    world = _world_with_named_crew("engineer", 4)
    output = io.StringIO()
    def fail(current):
        assert current.save.ship.crew_records["engineer"]["paid_jumps"] == 5
        assert "is now Seasoned" not in output.getvalue()
        raise vr.SaveError()
    world._checkpoint = fail
    with contextlib.redirect_stdout(output), pytest.raises(vr.SaveError):
        vr.screen_travel(vr.Palette(False), world, world.here.connections[0])
    assert "is now Seasoned" not in output.getvalue()


@pytest.mark.parametrize("recovery", ["destroy_ship", "rescue_stranded_pilot"])
def test_named_crew_survive_existing_salvage_recovery(recovery):
    import copy
    world = _world_with_named_crew("engineer", 15)
    before = copy.deepcopy(world.save.ship.crew_records)
    getattr(vr, recovery)(world)
    assert world.save.ship.has_engineer and world.save.ship.crew_records == before


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
def test_crew_first_page_starts_with_available_specialist(monkeypatch, terminal, width, height):
    world = _world_with_seed(42)
    terminal(width, height)
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    output = io.StringIO()
    with contextlib.redirect_stdout(output): vr.screen_crew(vr.Palette(False), world)
    text = " ".join(vr._ANSI_RE.sub("", output.getvalue()).split())
    assert "Gunner: Available" in text
    if width >= 40:
        assert vr.crew_name(world, "gunner") in text
        assert "hire 800cr + 15cr/jump" in text and "+3 combat damage per hit" in text
    if "Promotions" in text: assert text.index("Gunner: Available") < text.index("Promotions")


@pytest.mark.parametrize("concord,blackwake", [(-100, 100), (-99, 99), (-98, 98), (0, 0), (95, 95), (96, 96), (99, 99), (100, 100)])
@pytest.mark.parametrize("ending", ["P", "S"])
def test_archive_preview_and_result_report_effective_standing(ending, concord, blackwake):
    import copy
    world = _archive_world("recovered")
    world.save.pilot.reputation = {vr.FACTION_CONCORD: concord, vr.FACTION_BLACKWAKE: blackwake}
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    concord_gain = min(100, concord + 5) - concord if ending == "P" else max(-100, concord - 2) - concord
    blackwake_gain = min(100, blackwake + 5) - blackwake
    terms = f"Concord {concord_gain:+d}" if ending == "P" else f"Blackwake {blackwake_gain:+d}; Concord {concord_gain:+d}"
    assert terms in " ".join(vr.archive_lines(world))
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng
    assert terms in " ".join(vr.archive_action(world, ending))
    assert world.save.pilot.reputation[vr.FACTION_CONCORD] - concord == concord_gain
    assert world.save.pilot.reputation[vr.FACTION_BLACKWAKE] - blackwake == (0 if ending == "P" else blackwake_gain)
    assert world.event_rng.getstate() == rng


# Personal assignments use existing combat/chart totals and delivery accounting.
def _world_with_crew_task(role, *, ready=False):
    world = _world_with_named_crew(role, 5)
    vr.accept_crew_assignment(world, role)
    if ready:
        if role == "gunner": world.save.pilot.kills += 1
        elif role == "engineer":
            world.save.cargo = {"machinery": 3}; world.save.cargo_basis = {"machinery": [[3, 300]]}
            world.save.trading_ledger.since_day = world.save.turn
        else:
            for system in [s for s in world.galaxy if not s.discovered][:3]: system.discovered = True
        world.save.current_system = vr.crew_assignment_destination(world, role)
        world.here.discovered = True
    world.save.discovered = [system.id for system in world.galaxy if system.discovered]
    return world


@pytest.mark.parametrize("role", list(vr.CREW_ROLES))
def test_personal_crew_task_preview_does_not_create_records_or_draw_rng(role):
    import copy
    world = _world_with_seed(42)
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    lines = " ".join(vr.crew_assignment_lines(world, role))
    assert vr.crew_name(world, role) in lines and str(vr.CREW_ASSIGNMENTS[role]["reward"]) in lines
    assert "five paid jumps" in lines and "no deposit" in lines and "wages" in lines
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


@pytest.mark.parametrize("role", list(vr.CREW_ROLES))
def test_personal_crew_task_completion_pays_once_and_retains_personal_memory(role):
    import copy
    world = _world_with_crew_task(role, ready=True)
    credits, missions = world.save.pilot.credits, world.save.pilot.missions_completed
    reputation, paid, rng = copy.deepcopy(world.save.pilot.reputation), world.save.ship.crew_records[role]["paid_jumps"], world.event_rng.getstate()
    result = vr.complete_crew_assignment(world, role)
    assert "Personal assignment complete" in result
    assert world.save.pilot.credits == credits + vr.CREW_ASSIGNMENTS[role]["reward"]
    assert world.save.pilot.missions_completed == missions + 1
    assert any(vr.crew_name(world, role) in line for line in world.save.pilot.highlights)
    assert world.save.pilot.reputation == reputation and world.save.ship.crew_records[role]["paid_jumps"] == paid
    assert world.event_rng.getstate() == rng
    assert vr.CREW_ASSIGNMENTS[role]["closing"] in vr.crew_assignment_lines(world, role)
    before = copy.deepcopy(world.save.to_dict())
    for operation in (vr.accept_crew_assignment, vr.complete_crew_assignment):
        with pytest.raises(ValueError, match="already complete"): operation(world, role)
        assert world.save.to_dict() == before
    assert not vr.crew_assignment_recap(world)


@pytest.mark.parametrize("role", list(vr.CREW_ROLES))
@pytest.mark.parametrize("fault", ["unhired", "recruit", "pending", "wrong_location", "unfinished"])
def test_personal_crew_task_invalid_handover_is_atomic(role, fault):
    import copy
    world = _world_with_crew_task(role, ready=True)
    if fault == "unhired": setattr(world.save.ship, f"has_{role}", False)
    elif fault == "recruit": world.save.ship.crew_records[role]["paid_jumps"] = 4
    elif fault == "pending": world.save.pending_travel = {"phase": "departed"}
    elif fault == "wrong_location": world.save.current_system = 0
    elif role == "gunner": world.save.pilot.kills = 0
    elif role == "engineer": _add_cargo(world, "machinery", 2)
    else:
        for system in world.galaxy: system.discovered = False
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    with pytest.raises(ValueError): vr.complete_crew_assignment(world, role)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


@pytest.mark.parametrize("role", list(vr.CREW_ROLES))
def test_personal_crew_task_rehire_keeps_baseline_and_acceptance_is_not_repeatable(role):
    import copy
    world = _world_with_crew_task(role)
    task = copy.deepcopy(vr.crew_assignment_record(world, role))
    vr.dismiss_crew(world, role)
    if role == "gunner": world.save.pilot.kills += 1
    elif role == "navigator":
        for system in [s for s in world.galaxy if not s.discovered][:3]: system.discovered = True
    else: _add_cargo(world, "machinery", 3)
    vr.hire_crew(world, role)
    assert vr.crew_assignment_record(world, role) == task
    before = copy.deepcopy(world.save.to_dict())
    with pytest.raises(ValueError, match="already active"): vr.accept_crew_assignment(world, role)
    assert world.save.to_dict() == before
    world.save.current_system = vr.crew_assignment_destination(world, role)
    vr.complete_crew_assignment(world, role)
    assert vr.crew_assignment_record(world, role)["state"] == "complete"


@pytest.mark.parametrize("remaining", [0, 1, 2, 3, 10])
def test_navigator_personal_task_acceptance_is_possible_for_mature_charts(remaining):
    world = _world_with_named_crew("navigator", 5)
    for system in world.galaxy: system.discovered = system.id < len(world.galaxy) - remaining
    vr.accept_crew_assignment(world, "navigator")
    task = vr.crew_assignment_record(world, "navigator")
    assert task["baseline"] == len(world.galaxy) - remaining and task["required"] == min(3, remaining)
    if remaining == 0: assert "atlas is complete" in " ".join(vr.crew_assignment_lines(world, "navigator"))
    for system in [s for s in world.galaxy if not s.discovered][:task["required"]]: system.discovered = True
    world.save.current_system = vr.crew_assignment_destination(world, "navigator")
    vr.complete_crew_assignment(world, "navigator")
    assert task["state"] == "complete"


@pytest.mark.parametrize("lots", [[[4, 400]], [[1, 100], [3, 300]], [[3, 300], [1, 100]]])
def test_engineer_personal_task_uses_delivery_fifo_and_gross_reward(lots):
    world = _world_with_crew_task("engineer", ready=True)
    _set_cargo(world, {"machinery": 4})
    world.save.cargo_basis = {"machinery": [list(lot) for lot in lots]}
    before = world.save.pilot.credits
    vr.complete_crew_assignment(world, "engineer")
    ledger = world.save.trading_ledger
    assert world.save.cargo == {"machinery": 1} and world.save.cargo_basis == {"machinery": [[1, 100]]}
    assert ledger.delivery_revenue == 900
    assert ledger.delivery_cost == 300 and ledger.cargo_loss_cost == 0
    assert world.save.pilot.credits == before + 900


@pytest.mark.parametrize("task", [None, [], {}, {"version": 2, "state": "active", "baseline": 0, "required": 1},
    {"version": True, "state": "active", "baseline": 0, "required": 1},
    {"version": 1, "state": "other", "baseline": 0, "required": 1},
    {"version": 1, "state": "active", "baseline": -1, "required": 1},
    {"version": 1, "state": "active", "baseline": 1, "required": 1},
    {"version": 1, "state": "active", "baseline": 0, "required": 2},
    {"version": 1, "state": "active", "baseline": 0, "required": True},
    {"version": 1, "state": "complete", "baseline": 0, "required": 1},
    {"version": 1, "state": "active", "baseline": 0, "required": 1, "extra": 1}])
def test_invalid_personal_crew_assignment_preserves_original_file(tmp_path, task):
    import json
    world = _world_with_crew_task("gunner")
    world.save.ship.crew_records["gunner"]["assignment"] = task
    path = tmp_path / "77.json"; path.write_text(json.dumps(world.save.to_dict()), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(vr.ResumeError): vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == before


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
@pytest.mark.parametrize("style", ["auto", "plain"])
@pytest.mark.parametrize("role", list(vr.CREW_ROLES))
def test_personal_crew_task_pages_keep_complete_terms_and_leave_no_writes(monkeypatch, terminal, width, height, style, role):
    import copy,re
    world = _world_with_crew_task(role)
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    terminal(width, height, style)
    output, frames, bodies = io.StringIO(), [], []
    world._checkpoint = lambda w: pytest.fail("Task browsing checkpointed")
    def choose():
        frame = output.getvalue(); output.seek(0); output.truncate(0); frames.append(frame)
        assert "[B] Back" in " ".join(vr._ANSI_RE.sub("",frame).split())
        assert len(frame.splitlines()) <= height and all(vr._visible_width(row) <= width for row in frame.splitlines())
        assert world.save.to_dict() == before and world.event_rng.getstate() == rng
        page, count = map(int, re.search(r"Crew task.*?(\d+)/(\d+)", frame, re.S).groups())
        plain = vr._ANSI_RE.sub("", frame)
        body = re.sub(r"^[\s>]*Crew task\s+[\d,]+cr\s+\d+/\d+\s*", "", plain).split("[C] Complete")[0]
        bodies.append(body)
        return "B" if page == count else ">"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output): assert vr.screen_crew_assignment(vr.Palette(False), world, role) is None
    text = " ".join(" ".join(bodies).split())
    assert text == " ".join(" ".join(vr.crew_assignment_lines(world, role)).split())
    assert vr.crew_name(world, role) in text and "Reward:" in text and "Ordinary travel fuel" in text
    assert "five paid jumps" in text and "Work while they are away still counts" in text


@pytest.mark.parametrize("role,index", [("gunner", 1), ("engineer", 2), ("navigator", 3)])
@pytest.mark.parametrize("completing", [False, True])
def test_real_personal_crew_task_saves_before_acknowledgement(tmp_path, role, index, completing):
    world = _world_with_crew_task(role, ready=True) if completing else _world_with_named_crew(role, 5)
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    before, paid = world.save.pilot.credits, world.save.ship.crew_records[role]["paid_jumps"]
    keys = f"YK{index}".encode() + (b"CY" if role == "engineer" else b"C") if completing else f"YK{index}A".encode()
    ack = b"Personal assignment complete:" if completing else b"Personal assignment accepted:"
    with _door_stopped_at(tmp_path, keys, ack):
        saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        task = saved.ship.crew_records[role]["assignment"]
        assert task["state"] == ("complete" if completing else "active")
        assert saved.pilot.credits == before + (vr.CREW_ASSIGNMENTS[role]["reward"] if completing else 0)
        assert saved.ship.crew_records[role]["paid_jumps"] == paid
        if completing and role == "engineer": assert not saved.cargo and saved.trading_ledger.delivery_cost == 300


@pytest.mark.parametrize("role", list(vr.CREW_ROLES))
def test_personal_crew_task_save_failure_stops_before_result(monkeypatch, role):
    world = _world_with_crew_task(role, ready=True); output = io.StringIO()
    keys = iter("CY" if role == "engineer" else "C"); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    def fail(current):
        assert "Personal assignment complete:" not in output.getvalue()
        raise vr.SaveError()
    world._checkpoint = fail
    with contextlib.redirect_stdout(output), pytest.raises(vr.SaveError): vr.screen_crew_assignment(vr.Palette(False), world, role)


def test_personal_crew_engineer_task_real_route_and_handover(monkeypatch, tmp_path):
    world = _world_with_named_crew("engineer", 5)
    world.save.cargo = {"machinery": 3}; world.save.cargo_basis = {"machinery": [[3, 300]]}; world.save.trading_ledger.since_day = world.save.turn
    destination = vr.crew_assignment_destination(world, "engineer"); path = vr.bfs_path(world.by_id, world.here.id, destination)
    start_turn, start_credits = world.save.turn, world.save.pilot.credits
    keys = iter("AR" + "J" * len(path) + "BCYB")
    monkeypatch.setattr(vr, "read_key", lambda: next(keys)); monkeypatch.setattr(world.event_rng, "random", lambda: 0.99)
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    with contextlib.redirect_stdout(io.StringIO()): vr.screen_crew_assignment(vr.Palette(False), world, "engineer")
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert saved.current_system == destination and saved.turn == start_turn + len(path)
    assert saved.pilot.credits == start_credits + 900 - len(path) * vr.CREW_ROLES["engineer"]["wage"]
    assert not saved.cargo and saved.trading_ledger.delivery_cost == 300
    assert saved.ship.crew_records["engineer"]["assignment"]["state"] == "complete"


def test_personal_navigator_task_progress_uses_real_area_survey():
    world = _world_with_crew_task("navigator"); world.save.ship.scanner_tier = 1
    baseline = vr.crew_assignment_record(world, "navigator")["baseline"]
    fuel, turn, rng = world.save.ship.fuel, world.save.turn, world.event_rng.getstate()
    vr.perform_survey(world)
    have, need = vr.crew_assignment_progress(world, "navigator")
    assert have == sum(system.discovered for system in world.galaxy) - baseline and have >= need
    assert world.save.ship.fuel == fuel - 2 and world.save.turn == turn and world.event_rng.getstate() == rng


def test_personal_crew_task_recap_and_retirement():
    world = _world_with_crew_task("gunner")
    for lines in (vr.pilot_recap(world), vr.station_deck_lines(world)):
        assert any("Crew task:" in line and vr.crew_name(world, "gunner") in line for line in lines)
    assert vr.retire_pilot(world.save).ship.crew_records == {}


@pytest.mark.parametrize("role,index", [("gunner", 1), ("engineer", 2), ("navigator", 3)])
@pytest.mark.parametrize("commands", [b"", b">?<BQQ", b"BQQ"])
def test_real_personal_crew_task_back_and_eof_preserve_career(tmp_path, role, index, commands):
    import json,os,subprocess
    world = _world_with_crew_task(role)
    (tmp_path / "door_info.json").write_text(json.dumps({"user_id": 77, "handle": "Tester", "terminal_width": 40, "terminal_height": 12}), encoding="utf-8")
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    path = tmp_path / "77.json"; original = path.read_bytes()
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=f"YK{index}".encode() + commands,
                            capture_output=True, timeout=10, env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path),
                            NETBBS_DOOR_INFO=str(tmp_path / "door_info.json")))
    assert result.returncode == 0 and not result.stderr
    assert b"Crew task" in result.stdout and path.read_bytes() == original


def test_personal_engineer_handover_refusal_keeps_career(monkeypatch, tmp_path):
    world = _world_with_crew_task("engineer", ready=True)
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    before = (tmp_path / "77.json").read_bytes()
    keys = iter("CNB"); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()): assert vr.screen_crew_assignment(vr.Palette(False), world, "engineer") is None
    assert (tmp_path / "77.json").read_bytes() == before and world.save.cargo == {"machinery": 3}


@pytest.mark.parametrize("role", list(vr.CREW_ROLES))
@pytest.mark.parametrize("fault", ["unhired", "recruit", "pending"])
def test_personal_crew_task_rejected_acceptance_is_atomic(role, fault):
    import copy
    world = _world_with_named_crew(role, 5)
    if fault == "unhired": vr.dismiss_crew(world, role)
    elif fault == "recruit": world.save.ship.crew_records[role]["paid_jumps"] = 4
    else: world.save.pending_travel = {"phase": "departed"}
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    with pytest.raises(ValueError): vr.accept_crew_assignment(world, role)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


@pytest.mark.parametrize("role", list(vr.CREW_ROLES))
@pytest.mark.parametrize("complete", [False, True])
def test_personal_crew_task_serialized_state_retains_exact_requirements(tmp_path, role, complete):
    import json
    world = _world_with_crew_task(role, ready=True)
    if complete: vr.complete_crew_assignment(world, role)
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    expected = json.loads(json.dumps(world.save.to_dict()))
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert saved.to_dict() == expected
    resumed = vr.World(saved)
    if complete:
        with pytest.raises(ValueError, match="already complete"): vr.complete_crew_assignment(resumed, role)
    else:
        vr.complete_crew_assignment(resumed, role)
        assert vr.crew_assignment_record(resumed, role)["state"] == "complete"


@pytest.mark.parametrize("role", list(vr.CREW_ROLES))
def test_personal_crew_task_has_no_contract_slot_or_deadline(role):
    world = _world_with_named_crew(role, 5)
    world.save.active_missions = [vr.Mission(index + 1, "bounty", "Existing", 500, 0, 1, pirate_tier=1) for index in range(vr.MAX_ACTIVE_MISSIONS)]
    vr.accept_crew_assignment(world, role)
    world.save.turn += 100
    vr.check_mission_completions(world)
    assert vr.crew_assignment_record(world, role)["state"] == "active"
    assert len(world.save.active_missions) == vr.MAX_ACTIVE_MISSIONS


@pytest.mark.parametrize("patch", [{"baseline": 49}, {"baseline": True}, {"required": 4}, {"required": 0}, {"state": "complete"}])
def test_invalid_personal_navigator_task_preserves_career(tmp_path, patch):
    import json
    world = _world_with_crew_task("navigator")
    world.save.ship.crew_records["navigator"]["assignment"].update(patch)
    path = tmp_path / "77.json"; path.write_text(json.dumps(world.save.to_dict()), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(vr.ResumeError): vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == original


@pytest.mark.parametrize("concord,blackwake", [(-100, 75), (-51, 75), (-50, 75), (-49, -50), (0, -51), (75, -49), (100, 100)])
def test_faction_perks_follow_each_memberships_current_standing(concord, blackwake):
    world = _world_with_seed(42)
    world.save.pilot.has_concord_commission = world.save.pilot.has_blackwake_made = True
    world.save.pilot.reputation = {vr.FACTION_CONCORD: concord, vr.FACTION_BLACKWAKE: blackwake}
    assert vr.bounty_reward_for(world, 1000) == (1250 if concord > -50 else 1000)
    assert vr.customs_risk_for(world, world.here) == pytest.approx(vr.customs_check_chance(world.here) * (0.5 if blackwake > -50 else 1))
    for faction, standing in world.save.pilot.reputation.items():
        status = "ACTIVE" if standing > -50 else "SUSPENDED"
        assert status in vr.faction_membership_status(world, faction)
        assert any(vr.FACTION_LABEL[faction] in line and status in line for line in vr.pilot_record_lines(world))


@pytest.mark.parametrize("faction", vr.FACTIONS)
def test_faction_suspension_restoration_keeps_credential_and_never_repays_grant(faction):
    import copy
    world = _world_with_seed(42); world.save.pilot.reputation[faction] = 75
    vr.join_faction(world, faction)
    credits = world.save.pilot.credits
    vr.adjust_reputation(world, faction, -125)
    assert not vr.faction_perk_active(world, faction)
    assert getattr(world.save.pilot, vr.FACTION_MEMBERSHIPS[faction]["field"])
    before = copy.deepcopy(world.save.to_dict())
    with pytest.raises(ValueError, match="already held"): vr.join_faction(world, faction)
    assert world.save.to_dict() == before
    vr.adjust_reputation(world, faction, 1)
    assert vr.faction_perk_active(world, faction) and world.save.pilot.credits == credits


@pytest.mark.parametrize("faction", vr.FACTIONS)
@pytest.mark.parametrize("fault", ["under_threshold", "already_joined", "pending"])
def test_faction_join_rejects_before_any_effect(faction, fault):
    import copy
    world = _world_with_seed(42); world.save.pilot.reputation[faction] = 75
    if fault == "under_threshold": world.save.pilot.reputation[faction] = 74
    elif fault == "already_joined": setattr(world.save.pilot, vr.FACTION_MEMBERSHIPS[faction]["field"], True)
    else: world.save.pending_travel = {"phase": "arrival"}
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    with pytest.raises(ValueError): vr.join_faction(world, faction)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


@pytest.mark.parametrize("order", [vr.FACTIONS, list(reversed(vr.FACTIONS))])
def test_both_faction_memberships_can_be_joined_without_revoking_the_other(order):
    world = _world_with_seed(42); world.save.pilot.reputation = {faction: 75 for faction in vr.FACTIONS}
    before, rng = world.save.pilot.credits, world.event_rng.getstate()
    for faction in order: vr.join_faction(world, faction)
    assert world.save.pilot.credits == before + 4000
    assert all(vr.faction_perk_active(world, faction) for faction in vr.FACTIONS)
    assert world.event_rng.getstate() == rng


@pytest.mark.parametrize("faction", vr.FACTIONS)
@pytest.mark.parametrize("state", ["visitor", "eligible", "active", "suspended"])
@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
@pytest.mark.parametrize("style", ["auto", "plain"])
def test_faction_contact_pages_preserve_all_terms_without_writes(monkeypatch, terminal, without_action_bar, faction, state, width, height, style):
    import copy,re
    world = _world_with_seed(42)
    world.save.pilot.reputation[faction] = -50 if state == "suspended" else 75 if state != "visitor" else 0
    setattr(world.save.pilot, vr.FACTION_MEMBERSHIPS[faction]["field"], state in ("active", "suspended"))
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    terminal(width, height, style)
    output, bodies = io.StringIO(), []
    world._checkpoint = lambda w: pytest.fail("Contact browsing checkpointed")
    monkeypatch.setattr(vr, "confirm", lambda *args: pytest.fail("Browsing opened a confirmation"))
    def choose():
        frame = output.getvalue(); output.seek(0); output.truncate(0)
        assert len(frame.splitlines()) <= height and all(vr._visible_width(row) <= width for row in frame.splitlines())
        plain = vr._ANSI_RE.sub("", frame)
        assert "[B] Back" in " ".join(plain.split())
        if state=="eligible":assert "[J] Join" in " ".join(plain.split())
        match = re.search(r"[\d,]+cr\s+(\d+)/(\d+)", plain); assert match
        page, count = map(int, match.groups())
        body = re.sub(r"^[\s>]*\w+\s+[\d,]+cr\s+\d+/\d+\s*", "", plain)
        bodies.append(without_action_bar(body))
        assert world.save.to_dict() == before and world.event_rng.getstate() == rng
        return "B" if page == count else ">"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output): vr._screen_faction_contact(vr.Palette(False), world, faction)
    assert " ".join(" ".join(bodies).split()) == " ".join(" ".join(vr.faction_contact_lines(world, faction)).split())


@pytest.mark.parametrize("key", ["P", "W"])
@pytest.mark.parametrize("commands", [b"", b">?<BQ", b"JNBQ"])
def test_real_faction_contact_back_eof_and_refusal_preserve_career(tmp_path, key, commands):
    import json,os,subprocess
    world = _world_with_seed(42); world.save.pilot.reputation = {faction: 75 for faction in vr.FACTIONS}
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    (tmp_path / "door_info.json").write_text(json.dumps({"user_id": 77, "handle": "Tester", "terminal_width": 40, "terminal_height": 12}), encoding="utf-8")
    original = (tmp_path / "77.json").read_bytes()
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=key.encode() + commands,
                            capture_output=True, timeout=10, env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(tmp_path / "door_info.json")))
    assert result.returncode == 0 and not result.stderr
    assert (b"Edda Ro" if key == "P" else b"Rook Talan") in result.stdout
    assert (tmp_path / "77.json").read_bytes() == original


@pytest.mark.parametrize("faction", vr.FACTIONS)
def test_faction_join_save_failure_stops_before_acknowledgement(monkeypatch, faction):
    world = _world_with_seed(42); world.save.pilot.reputation[faction] = 75
    output = io.StringIO(); keys = iter("JY"); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    greeting = "Commission accepted." if faction == vr.FACTION_CONCORD else "Welcome to the family."
    def fail(current):
        assert greeting not in output.getvalue()
        raise vr.SaveError()
    world._checkpoint = fail
    with contextlib.redirect_stdout(output), pytest.raises(vr.SaveError): vr._screen_faction_contact(vr.Palette(False), world, faction)
    assert greeting not in output.getvalue()


def _world_at_faction_customs_phase(phase):
    world, _ = _world_with_pending_fight()
    world.save.active_missions = []
    _set_cargo(world, {"weapons": 1})
    world.save.pilot.has_blackwake_made = True
    travel = world.save.pending_travel
    travel.update(phase=phase, primary="random", bounty=None, encounter={})
    destination = world.by_id[travel["destination"]]
    assert destination.economy != "Haven"
    if phase == "customs": world.save.current_system = destination.id
    destination.discovered = True
    return world, destination


@pytest.mark.parametrize("standing", [-51, -50, -49, 75])
def test_faction_customs_suspension_changes_real_inspection_dispatch(monkeypatch, tmp_path, standing):
    import copy
    world, destination = _world_at_faction_customs_phase("arrival")
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = standing
    decisions, visits = [], []
    def save(current):
        vr.persist(current, tmp_path, 77)
        if current.save.pending_travel is not None and current.save.pending_travel["phase"] == "customs":
            decisions.append(copy.deepcopy(current.save.pending_travel["encounter"]))
    world._checkpoint = save; world.checkpoint()
    monkeypatch.setattr(world.event_rng, "random", lambda: 0.20)
    monkeypatch.setattr(vr, "screen_customs", lambda p, w: visits.append(w.here.id))
    with contextlib.redirect_stdout(io.StringIO()): vr.screen_travel(vr.Palette(False), world, destination.id)
    assert decisions == [{"inspect": standing <= -50}]
    assert visits == ([destination.id] if standing <= -50 else [])


@pytest.mark.parametrize("inspect", [True, False])
def test_faction_customs_resume_keeps_resolved_inspection_after_standing_changes(monkeypatch, tmp_path, inspect):
    world, destination = _world_at_faction_customs_phase("customs")
    world.save.pending_travel["encounter"] = {"inspect": inspect}
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = 75 if inspect else -100
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester"); resumed = vr.World(saved)
    visits = []
    monkeypatch.setattr(resumed.event_rng, "random", lambda: pytest.fail("Resolved customs decision rerolled"))
    monkeypatch.setattr(vr, "screen_customs", lambda p, w: visits.append(w.here.id))
    with contextlib.redirect_stdout(io.StringIO()): vr.screen_travel(vr.Palette(False), resumed, destination.id)
    assert visits == ([destination.id] if inspect else []) and resumed.save.pending_travel is None


# Optional faction cases use real travel and existing cargo accounting.
def _faction_case_world(faction, stage="idle", choice="aid"):
    world = _world_with_seed(42)
    if stage != "idle": vr.faction_story_action(world, faction, "A")
    if stage in ("accepted", "evidence", "committed", "complete"):
        world.save.current_system = vr.faction_story_target(world, faction)
        world.here.discovered = True
    if stage in ("evidence", "committed", "complete"):
        vr.faction_story_action(world, faction, "I")
    if stage in ("committed", "complete"):
        vr.faction_story_action(world, faction, "H" if choice == "hardline" else "A")
        world.save.current_system = vr.faction_story_destination(world, faction)
        world.here.discovered = True
        ending = vr.FACTION_STORIES[faction][choice]
        if ending["commodity"]:
            _set_cargo(world, {ending["commodity"]: ending["quantity"]})
            world.save.cargo_basis = {ending["commodity"]: [[ending["quantity"], 100 * ending["quantity"]]]}
            world.save.trading_ledger.since_day = world.save.turn
    if stage == "complete": vr.faction_story_action(world, faction, "C")
    return world


@pytest.mark.parametrize("faction", vr.FACTIONS)
@pytest.mark.parametrize("choice", ["hardline", "aid"])
def test_faction_case_four_endings_pay_once_without_membership_or_slots(faction, choice):
    import copy
    world = _faction_case_world(faction, "committed", choice)
    world.save.active_missions = [vr.Mission(i+1, "bounty", "Existing", 500, 0, 1, pirate_tier=1) for i in range(vr.MAX_ACTIVE_MISSIONS)]
    missions = copy.deepcopy(world.save.active_missions)
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    ending = vr.FACTION_STORIES[faction][choice]
    result = vr.faction_story_action(world, faction, "C")
    assert "Case complete:" in result and f"{ending['reward']:,}cr" in result
    assert world.save.pilot.credits == before["pilot"]["credits"] + ending["reward"]
    assert world.save.pilot.missions_completed == 1
    assert world.save.pilot.reputation == ending["standing"]
    assert world.save.faction_stories[faction] == {"version": 1, "stage": "complete", "choice": choice}
    assert not world.save.cargo and not world.save.cargo_basis
    assert world.save.active_missions == missions and world.save.turn == before["turn"]
    assert world.save.ship.fuel == before["ship"]["fuel"] and world.event_rng.getstate() == rng
    assert not world.save.pilot.has_concord_commission and not world.save.pilot.has_blackwake_made
    assert vr.FACTION_STORIES[faction][choice]["closing"] in vr.faction_story_lines(world, faction)
    assert not vr.faction_story_recap(world)
    complete = copy.deepcopy(world.save.to_dict())
    for action in "ACHIR":
        with pytest.raises(ValueError): vr.faction_story_action(world, faction, action)
        assert world.save.to_dict() == complete


@pytest.mark.parametrize("faction", vr.FACTIONS)
@pytest.mark.parametrize("stage,action,fault", [("idle", "I", "none"), ("accepted", "I", "location"), ("evidence", "C", "none"), ("committed", "H", "none"), ("committed", "C", "location"), ("committed", "C", "cargo"), ("committed", "C", "travel")])
def test_faction_case_rejected_actions_preserve_every_effect(faction, stage, action, fault):
    import copy
    world = _faction_case_world(faction, stage)
    if fault == "location": world.save.current_system = (world.save.current_system + 1) % len(world.galaxy)
    if fault == "cargo": world.save.cargo.clear(); world.save.cargo_basis.clear()
    if fault == "travel": world.save.pending_travel = {"phase": "arrival"}
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    with pytest.raises(ValueError): vr.faction_story_action(world, faction, action)
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng


@pytest.mark.parametrize("faction", vr.FACTIONS)
@pytest.mark.parametrize("choice", ["hardline", "aid"])
@pytest.mark.parametrize("standing", [-100, 99, 100])
def test_faction_case_previews_show_effective_capped_standing(faction, choice, standing):
    world = _faction_case_world(faction, "committed", choice)
    world.save.pilot.reputation = {group: standing for group in vr.FACTIONS}
    expected = {group: max(-100, min(100, standing + amount)) for group, amount in vr.FACTION_STORIES[faction][choice]["standing"].items()}
    preview = " ".join(vr.faction_story_lines(world, faction))
    result = vr.faction_story_action(world, faction, "C")
    assert world.save.pilot.reputation == expected
    for group, after in expected.items():
        assert f"{vr.FACTION_LABEL[group]} {after-standing:+d}" in preview
        assert f"{vr.FACTION_LABEL[group]} {after-standing:+d}" in result


@pytest.mark.parametrize("lots", [[[5, 500]], [[2, 200], [3, 300]], [[3, 300], [2, 200]]])
def test_faction_case_material_handover_uses_fifo_costs(lots):
    world = _faction_case_world(vr.FACTION_BLACKWAKE, "committed")
    _set_cargo(world, {"electronics": 5})
    world.save.cargo_basis = {"electronics": [list(lot) for lot in lots]}
    vr.faction_story_action(world, vr.FACTION_BLACKWAKE, "C")
    ledger = world.save.trading_ledger
    assert world.save.cargo == {"electronics": 2}
    assert ledger.delivery_cost == 300
    assert ledger.delivery_revenue == 1500
    assert ledger.cargo_loss_cost == ledger.sales_cost == 0


def test_faction_case_no_haven_disables_only_armed_ending_without_rng_or_mutation():
    import copy
    world = _faction_case_world(vr.FACTION_BLACKWAKE, "evidence")
    for system in world.galaxy:
        if system.economy == "Haven": system.economy = "Industrial"
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    assert "no Haven" in " ".join(vr.faction_story_lines(world, vr.FACTION_BLACKWAKE))
    with pytest.raises(ValueError, match="No Haven"): vr.faction_story_action(world, vr.FACTION_BLACKWAKE, "H")
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng
    vr.faction_story_action(world, vr.FACTION_BLACKWAKE, "A")
    assert world.save.faction_stories[vr.FACTION_BLACKWAKE]["choice"] == "aid"


@pytest.mark.parametrize("faction", vr.FACTIONS)
@pytest.mark.parametrize("stage", ["idle", "accepted", "evidence", "committed", "complete"])
def test_faction_case_roundtrip_preserves_exact_state_and_legacy_absence(tmp_path, faction, stage):
    import json
    world = _faction_case_world(faction, stage)
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    expected = json.loads(json.dumps(world.save.to_dict()))
    saved, fresh, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert not fresh and saved.to_dict() == expected
    assert ("faction_stories" in expected) == (stage != "idle")


@pytest.mark.parametrize("record", [None, [], {"x": {}}, {"concord": {}}, {"concord": {"version": True, "stage": "accepted"}}, {"concord": {"version": 1, "stage": "bad"}}, {"concord": {"version": 1, "stage": "accepted", "choice": "aid"}}, {"concord": {"version": 1, "stage": "committed"}}, {"concord": {"version": 1, "stage": "complete", "choice": "bad"}}])
def test_faction_case_invalid_state_preserves_original_career(tmp_path, record):
    import json
    world = _world_with_seed(42); data = world.save.to_dict(); data["faction_stories"] = record
    path = tmp_path / "77.json"; raw = json.dumps(data).encode(); path.write_bytes(raw)
    with pytest.raises(vr.ResumeError): vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == raw


@pytest.mark.parametrize("record", [{"version": 2, "stage": "accepted"}, {"version": 1, "stage": "accepted", "future": True}])
def test_faction_case_future_state_is_not_downgraded(tmp_path, record):
    import json
    data = _world_with_seed(42).save.to_dict(); data["faction_stories"] = {"concord": record}
    path = tmp_path / "77.json"; raw = json.dumps(data).encode(); path.write_bytes(raw)
    with pytest.raises(vr.UnsupportedSave): vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == raw


@pytest.mark.parametrize("faction", vr.FACTIONS)
@pytest.mark.parametrize("stage", ["idle", "accepted", "evidence", "committed", "complete"])
@pytest.mark.parametrize("width,height,style", [(20, 10, "plain"), (40, 12, "auto"), (80, 24, "auto")])
def test_faction_case_pages_preserve_full_terms_without_writes(monkeypatch, terminal, without_action_bar, faction, stage, width, height, style):
    import copy,re
    world = _faction_case_world(faction, stage)
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    terminal(width, height, style)
    output, bodies = io.StringIO(), []
    world._checkpoint = lambda w: pytest.fail("Case browsing checkpointed")
    monkeypatch.setattr(vr, "confirm", lambda *args: pytest.fail("Browsing opened a confirmation"))
    def choose():
        frame = output.getvalue(); output.seek(0); output.truncate(0)
        assert "[B] Back" in " ".join(vr._ANSI_RE.sub("",frame).split())
        assert len(frame.splitlines()) <= height and all(vr._visible_width(row) <= width for row in frame.splitlines())
        plain = vr._ANSI_RE.sub("", frame)
        page, count = map(int, re.search(r"[\d,]+cr\s+(\d+)/(\d+)", plain).groups())
        body = re.sub(r"^[\s>]*Case\s+[\d,]+cr\s+\d+/\d+\s*", "", plain)
        bodies.append(without_action_bar(body))
        assert world.save.to_dict() == before and world.event_rng.getstate() == rng
        return "B" if page == count else ">"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output): assert vr.screen_faction_story(vr.Palette(False), world, faction) is None
    assert " ".join(" ".join(bodies).split()) == " ".join(" ".join(vr.faction_story_lines(world, faction)).split())


@pytest.mark.parametrize("faction", vr.FACTIONS)
@pytest.mark.parametrize("stage,keys,ack,next_stage", [("idle", "A", "Case accepted:", "accepted"), ("accepted", "I", "Evidence recovered:", "evidence"), ("evidence", "H", "Course chosen:", "committed"), ("committed", "CY", "Case complete:", "complete")])
def test_faction_case_real_process_kill_retains_each_acknowledged_stage(tmp_path, faction, stage, keys, ack, next_stage):
    world = _faction_case_world(faction, stage)
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    commands = ("P" if faction == vr.FACTION_CONCORD else "W") + "S" + keys
    with _door_stopped_at(tmp_path, commands.encode(), ack.encode()):
        saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
        assert saved.faction_stories[faction]["stage"] == next_stage
        assert saved.pilot.missions_completed == (1 if next_stage == "complete" else 0)
        if next_stage == "complete": assert not saved.cargo and saved.trading_ledger.delivery_cost > 0


@pytest.mark.parametrize("stage,keys,ack", [("idle", "A", "Case accepted:"), ("accepted", "I", "Evidence recovered:"), ("evidence", "H", "Course chosen:"), ("committed", "CY", "Case complete:")])
def test_faction_case_checkpoint_failure_prevents_acknowledgement(monkeypatch, stage, keys, ack):
    world = _faction_case_world(vr.FACTION_CONCORD, stage); output = io.StringIO()
    commands = iter(keys); monkeypatch.setattr(vr, "read_key", lambda: next(commands))
    def fail(current):
        assert ack not in output.getvalue()
        raise vr.SaveError()
    world._checkpoint = fail
    with contextlib.redirect_stdout(output), pytest.raises(vr.SaveError): vr.screen_faction_story(vr.Palette(False), world, vr.FACTION_CONCORD)


@pytest.mark.parametrize("faction", vr.FACTIONS)
@pytest.mark.parametrize("choice", ["hardline", "aid"])
def test_faction_case_real_route_completes_both_legs_with_ordinary_costs(monkeypatch, tmp_path, faction, choice):
    world = _world_with_seed(42); ending = vr.FACTION_STORIES[faction][choice]
    if ending["commodity"]: _set_cargo(world, {ending["commodity"]: ending["quantity"]})
    first, last = vr.faction_story_target(world, faction), vr.faction_story_target(world, faction, choice)
    outward, onward = vr.bfs_path(world.by_id, 0, first), vr.bfs_path(world.by_id, first, last)
    commands = iter("AR" + "J"*len(outward) + "BI" + ("H" if choice == "hardline" else "A") + "R" + "J"*len(onward) + "BC" + ("Y" if ending["commodity"] else "") + "B")
    monkeypatch.setattr(vr, "read_key", lambda: next(commands)); monkeypatch.setattr(world.event_rng, "random", lambda: 0.99)
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    with contextlib.redirect_stdout(io.StringIO()): vr.screen_faction_story(vr.Palette(False), world, faction)
    saved, _, _ = vr.load_or_create_save(tmp_path, 77, "Tester")
    assert saved.current_system == last and saved.turn == len(outward) + len(onward)
    expected_fuel = sum(vr.fuel_cost_for_jump(world.by_id[a], world.by_id[b], world.save.ship) for path in ([0] + outward, [first] + onward) for a, b in zip(path, path[1:]))
    assert saved.ship.fuel == 24 - expected_fuel and expected_fuel > 0
    assert saved.pilot.credits == 1200 + ending["reward"] and saved.pilot.missions_completed == 1
    assert saved.faction_stories[faction]["stage"] == "complete" and saved.pending_travel is None
    assert first in saved.discovered and last in saved.discovered


@pytest.mark.parametrize("faction", vr.FACTIONS)
@pytest.mark.parametrize("stage,commands", [("idle", b""), ("idle", b">?<BBQ"), ("committed", b""), ("committed", b"CNBBQ"), ("complete", b"BBQ")])
def test_faction_case_real_back_eof_and_refusal_preserve_career(tmp_path, faction, stage, commands):
    import json,os,subprocess
    world = _faction_case_world(faction, stage)
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 77); world.checkpoint()
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester", "terminal_width": 40, "terminal_height": 12}), encoding="utf-8")
    original = (tmp_path / "77.json").read_bytes()
    prefix = b"PS" if faction == vr.FACTION_CONCORD else b"WS"
    result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=prefix + commands,
                            capture_output=True, timeout=10, env=dict(os.environ, VOIDRUNNER_SAVE_DIR=str(tmp_path), NETBBS_DOOR_INFO=str(info)))
    assert result.returncode == 0 and not result.stderr
    assert b"Case " in result.stdout and (tmp_path / "77.json").read_bytes() == original


@pytest.mark.parametrize("faction", vr.FACTIONS)
def test_faction_case_hostile_ending_suspends_opponent_perk_without_revoking_membership(faction):
    world = _faction_case_world(faction, "committed", "hardline")
    other = next(group for group in vr.FACTIONS if group != faction)
    world.save.pilot.has_concord_commission = world.save.pilot.has_blackwake_made = True
    world.save.pilot.reputation = {faction: 0, other: -40}
    assert vr.faction_perk_active(world, other)
    before = world.save.pilot.credits
    vr.faction_story_action(world, faction, "C")
    assert world.save.pilot.reputation[other] == -52 and not vr.faction_perk_active(world, other)
    assert vr.faction_perk_active(world, faction)
    assert world.save.pilot.has_concord_commission and world.save.pilot.has_blackwake_made
    assert world.save.pilot.credits == before + vr.FACTION_STORIES[faction]["hardline"]["reward"]


def test_faction_case_bearing_allows_only_active_haven_route_without_charting():
    import copy
    world = _faction_case_world(vr.FACTION_BLACKWAKE, "evidence")
    target = vr.faction_story_target(world, vr.FACTION_BLACKWAKE, "hardline")
    world.by_id[target].discovered = False
    assert target not in vr.specialist_stations(world).values()
    with pytest.raises(vr.MissionError, match="charted"): vr.prepare_route_jump(world, target)
    vr.faction_story_action(world, vr.FACTION_BLACKWAKE, "H")
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    assert vr.prepare_route_jump(world, target) == vr.bfs_path(world.by_id, world.here.id, target)[0]
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng and not world.by_id[target].discovered
    for lines in (vr.station_deck_lines(world), vr.pilot_recap(world)):
        assert vr.FACTION_STORIES[vr.FACTION_BLACKWAKE]["title"] in " ".join(lines)
    world.save.faction_stories[vr.FACTION_BLACKWAKE]["stage"] = "complete"
    with pytest.raises(vr.MissionError, match="charted"): vr.prepare_route_jump(world, target)


@pytest.mark.parametrize("fault", ["location", "cargo"])
def test_faction_case_unavailable_handover_never_opens_confirmation(monkeypatch, fault):
    world = _faction_case_world(vr.FACTION_CONCORD, "committed")
    if fault == "location": world.save.current_system = 0
    else: world.save.cargo.clear(); world.save.cargo_basis.clear()
    keys = iter("CB"); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(vr, "confirm", lambda *args: pytest.fail("Unavailable handover opened confirmation"))
    world._checkpoint = lambda w: pytest.fail("Unavailable handover checkpointed")
    with contextlib.redirect_stdout(io.StringIO()): vr.screen_faction_story(vr.Palette(False), world, vr.FACTION_CONCORD)


@pytest.mark.parametrize("route_kind", ["general", "archive"])
def test_general_route_map_keeps_independent_tracked_objective_and_route_end(monkeypatch, route_kind):
    import copy
    world = _archive_world("started")
    destination = world.landmark["system_id"] if route_kind == "archive" else world.here.connections[0]
    path = vr.bfs_path(world.by_id, world.here.id, destination)
    target = next(system.id for system in world.galaxy if not system.discovered and system.id not in path)
    mission = vr.Mission(100, "scan", "Tracked survey", 1000, 0, target)
    world.save.active_missions = [mission]; world.save.tracked_mission_id = mission.id
    world.by_id[target].x = 99; world.by_id[target].y = 49
    world.by_id[destination].x = 0; world.by_id[destination].y = 49
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    lists = []; original = vr.map_list_lines
    def capture(current, supplied, public):
        lines = original(current, supplied, public); lists.extend(lines); return lines
    monkeypatch.setattr(vr, "map_list_lines", capture)
    keys = iter("VOBB"); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()): vr.screen_auto_route(vr.Palette(False), world, destination=destination)
    objective = [line for line in lists if world.by_id[target].name in line]
    endpoint = [line for line in lists if world.by_id[destination].name in line]
    assert len(objective) == 1 and objective[0].startswith("! ")
    assert len(endpoint) == 1 and "X" in endpoint[0].split()[0] and "!" not in endpoint[0].split()[0]
    grid = "".join(vr.spatial_map_grid(world, path, public_target=destination, sector=None, columns=119, rows=36))
    assert "!" in grid and "X" in grid
    assert "Tracked contract objective" in " ".join(vr.map_inspection_lines(world, target, path, destination))
    assert "Public route destination" in " ".join(vr.map_inspection_lines(world, destination, path, destination))
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng
    assert not world.by_id[target].discovered


@pytest.mark.parametrize("stage", ["idle", "started"])
@pytest.mark.parametrize("investigated", [False, True])
def test_archive_terms_disclose_only_remaining_landmark_salvage(stage, investigated):
    import copy
    world = _archive_world(stage, investigated=investigated)
    before, rng = copy.deepcopy(world.save.to_dict()), world.event_rng.getstate()
    terms = " ".join(vr.archive_lines(world))
    expected = "Landmark salvage already claimed; transcribing the record pays no additional salvage." if investigated else "Unclaimed landmark salvage: +3,000cr once when investigating."
    assert expected in terms
    assert "Unclaimed landmark salvage remains yours" not in terms
    assert world.save.to_dict() == before and world.event_rng.getstate() == rng
    if stage == "idle": vr.archive_action(world, "A")
    world.save.current_system = world.landmark["system_id"]
    credits = world.save.pilot.credits
    vr.archive_action(world, "I")
    assert world.save.pilot.credits - credits == (0 if investigated else 3000)


@pytest.mark.parametrize("index", [1, 2, 3, 4])
def test_career_rank_views_keep_earned_title_after_spending(index):
    import copy
    world = _world_with_seed(42); world.save.pilot.highest_rank_seen = index; world.save.pilot.credits = 10
    before = copy.deepcopy(world.save.to_dict())
    for lines in (vr.station_deck_lines(world, expanded=True), vr.pilot_record_lines(world, "O")):
        assert f"Rank: {vr.RANKS[index][1]}." in " ".join(lines)
    assert world.save.to_dict() == before


@pytest.mark.parametrize("threshold,index", [(5000,1),(20000,2),(75000,3),(150000,4)])
def test_career_rank_checkpoint_retains_reward_before_next_spend(tmp_path, threshold, index):
    world = _world_with_seed(42); world._checkpoint = lambda w: vr.persist(w,tmp_path,77)
    world.save.pilot.credits = threshold; world.checkpoint()
    saved, _, _ = vr.load_or_create_save(tmp_path,77,"Tester")
    assert saved.pilot.highest_rank_seen == index
    world.save.pilot.credits = 10; world.checkpoint(); world.checkpoint()
    saved, _, _ = vr.load_or_create_save(tmp_path,77,"Tester")
    assert vr.career_rank(saved.pilot) == vr.RANKS[index][1]
    assert sum("Promoted to" in entry for entry in saved.pilot.highlights) == 1
    assert vr.check_rank_up(world) is None


@pytest.mark.parametrize("commands,marker", [(b"MAS1\rAP1\r",b"Result: Bought 1x Food"), (b"MAS1\r",b"Result: Sold 1x Food")])
def test_career_rank_real_nested_market_peak_survives_kill(tmp_path, commands, marker):
    world = _world_with_seed(42); world.save.pilot.credits = 4999; _set_cargo(world, {"food":1})
    world._checkpoint=lambda w:vr.persist(w,tmp_path,77); world.checkpoint()
    assert world.save.pilot.highest_rank_seen == 0
    with _door_stopped_at(tmp_path,commands,marker):
        saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
        assert saved.pilot.highest_rank_seen == 1
        assert vr.career_rank(saved.pilot) == "Independent Trader"
        if b"AP" in commands: assert saved.pilot.credits < 5000
        else: assert saved.pilot.credits >= 5000


@pytest.mark.parametrize("credits,expected", [(0,0),(4999,0),(5000,1),(19999,1),(20000,2),(74999,2),(75000,3),(149999,3),(150000,4)])
def test_career_rank_current_funds_support_legacy_rank_without_writes(credits, expected):
    import copy
    world = _world_with_seed(42); world.save.pilot.credits = credits; world.save.best_credits = 1000000
    before=copy.deepcopy(world.save.to_dict())
    assert vr.career_rank_index(world.save.pilot) == expected
    terms=" ".join(vr.career_rank_terms(world.save.pilot))
    if expected<4: assert f"{vr.RANKS[expected+1][0]:,}cr balance" in terms
    else: assert "Top rank retained" in terms
    assert world.save.to_dict() == before


def test_career_rank_retirement_resets_rank_despite_lifetime_score():
    world = _world_with_seed(42); world.save.pilot.highest_rank_seen=4; world.save.pilot.credits=1; world.save.best_credits=1000000
    retired=vr.retire_pilot(world.save)
    assert retired.best_credits==1000000 and retired.pilot.highest_rank_seen==0
    assert vr.career_rank(retired.pilot)==vr.RANKS[0][1]


def test_career_rank_retained_top_rank_keeps_real_retirement_available(monkeypatch,tmp_path):
    world=_world_with_seed(42); world.save.pilot.highest_rank_seen=4; world.save.pilot.credits=100
    world._checkpoint=lambda w:vr.persist(w,tmp_path,77); world.checkpoint()
    keys=iter("RSY"); monkeypatch.setattr(vr,"read_key",lambda:next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output: vr.screen_status(vr.Palette(False),world)
    assert "A new career begins" in output.getvalue()
    saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
    assert saved.pilot.retirements==1 and saved.pilot.highest_rank_seen==0


def test_career_rank_save_failure_stops_reward_before_ack(monkeypatch):
    world=_world_with_seed(42); world.save.pilot.credits=4999; _set_cargo(world, {"food":1})
    output=io.StringIO(); keys=iter("AS"); monkeypatch.setattr(vr,"read_key",lambda:next(keys)); monkeypatch.setattr(vr,"read_line_raw",lambda **kw:"1")
    def fail(current):
        assert current.save.pilot.highest_rank_seen==1 and "Result: Sold" not in output.getvalue()
        raise vr.SaveError()
    world._checkpoint=fail
    with contextlib.redirect_stdout(output),pytest.raises(vr.SaveError): vr.screen_market(vr.Palette(False),world)


@pytest.mark.parametrize("width,height",[(20,10),(40,12),(80,24)])
def test_career_rank_full_terms_fit_record_pages_without_mutation(monkeypatch, terminal,width,height):
    import copy,re
    world=_world_with_seed(42); world.save.pilot.highest_rank_seen=2; world.save.pilot.credits=100
    before=copy.deepcopy(world.save.to_dict()); output=io.StringIO(); bodies=[]
    terminal(width, height)
    world._checkpoint=lambda w:pytest.fail("Rank browsing checkpointed")
    def choose():
        frame=output.getvalue(); output.seek(0); output.truncate(0)
        assert len(frame.splitlines())<=height and all(vr._visible_width(line)<=width for line in frame.splitlines())
        plain=vr._ANSI_RE.sub("",frame); match=re.search(r"Overview\s+(\d+)/(\d+)",plain); assert match
        page,count=map(int,match.groups())
        body=re.sub(r"^[\s>]*Pilot Record:\s*Overview\s+\d+/\d+\s*","",plain)
        bodies.append(body.split("[<")[0]); return "B" if page==count else ">"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):vr.screen_status(vr.Palette(False),world)
    text=" ".join(" ".join(bodies).split())
    assert "Rank is permanent for this career" in text and "75,000cr balance" in text
    assert world.save.to_dict()==before


@pytest.mark.parametrize("finale",list(vr.CAREER_FINALES))
def test_career_finale_archives_exact_run_and_grants_ordinary_module(tmp_path,finale):
    import copy,json
    world=_finale_world(finale); world.save.pilot.highlight("A distinct old achievement")
    world.save.pilot.retirements=3; world.save.best_credits=1000000; world.save.display_style="plain"
    before=copy.deepcopy(world.save.to_dict()); rng=world.event_rng.getstate()
    fresh=vr.finish_career(world.save,finale)
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng
    assert fresh.seed!=world.save.seed and fresh.pilot.retirements==4 and fresh.best_credits==1000000
    assert fresh.pilot.credits==3200 and fresh.pilot.highest_rank_seen==0
    assert fresh.display_style=="plain" and fresh.discovered==[0] and not fresh.cargo and not fresh.active_missions
    assert not fresh.faction_stories and not fresh.ship.crew_records
    expected={vr.CAREER_FINALES[finale]["tier"]} if finale!="legend" else set()
    for module in ("cargo","scanner","weapon","engine","shield","hull"):
        assert getattr(fresh.ship,module+"_tier")==int(module in expected)
    assert len(fresh.retired_careers)==1
    item=fresh.retired_careers[0]
    assert item["number"]==4 and item["seed"]==42 and item["finale"]==finale
    assert item["credits"]==1200 and item["highlights"]==["A distinct old achievement"]
    assert item["rank"]==vr.career_rank_index(world.save.pilot)
    assert item["market_margin"]==vr.career_accomplishments(world.save)["trader"]
    newworld=vr.World(fresh); newworld._checkpoint=lambda w:vr.persist(w,tmp_path,77); newworld.checkpoint()
    saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
    assert saved.to_dict()==json.loads(json.dumps(newworld.save.to_dict()))
    assert vr.CAREER_FINALES[finale]["closing"] in " ".join(vr.career_dossier_lines(saved))
    fresh.retired_careers[0]["highlights"].append("New container")
    assert world.save.pilot.highlights==["A distinct old achievement"]


@pytest.mark.parametrize("finale",list(vr.CAREER_FINALES))
@pytest.mark.parametrize("fault",["not_ready","travel","capacity"])
def test_career_finale_rejects_before_reset_rng_or_old_state_mutation(monkeypatch,finale,fault):
    import copy
    world=_world_with_seed(42) if fault=="not_ready" else _finale_world(finale)
    if fault=="travel": world.save.pending_travel={"phase":"arrival"}
    if fault=="capacity": world.save.retired_careers=[{} for _ in range(vr.MAX_RETIRED_CAREERS)]
    before=copy.deepcopy(world.save.to_dict()); rng=world.event_rng.getstate()
    monkeypatch.setattr(vr,"_new_career",lambda *args:pytest.fail("Invalid finale created a new run"))
    with pytest.raises(ValueError):vr.finish_career(world.save,finale)
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng


@pytest.mark.parametrize("margin,ready",[(49999,False),(50000,True),(50001,True),(-50000,False)])
def test_career_finale_trader_counts_only_recorded_market_margin(margin,ready):
    world=_world_with_seed(42); ledger=world.save.trading_ledger
    ledger.sales_cost=50000; ledger.sales_revenue=50000+margin
    ledger.delivery_revenue=1000000; ledger.uncosted_sales=1000000; ledger.uncosted_deliveries=1000000
    world.save.pilot.credits=1000000; world.save.best_credits=1000000
    assert vr.career_accomplishments(world.save)["trader"]==margin
    assert (vr.career_finale_blocker(world.save,"trader") is None)==ready


@pytest.mark.parametrize("finale,below,at",[("explorer",47,48),("combat",49,50)])
def test_career_finale_nontrader_boundaries_ignore_cash(finale,below,at):
    world=_world_with_seed(42); world.save.pilot.credits=0
    for value in (below,at):
        if finale=="explorer": world.save.discovered=list(range(value))
        else: world.save.pilot.kills=value
        assert (vr.career_finale_blocker(world.save,finale) is None)==(value==at)


def test_career_finale_repeated_endings_preserve_prior_dossiers_and_legacy_count(tmp_path):
    import copy
    world=_finale_world(); world.save.pilot.retirements=7
    first=vr.finish_career(world.save,"legend"); previous=copy.deepcopy(first.retired_careers)
    first.pilot.kills=50
    second=vr.finish_career(first,"combat")
    assert [item["number"] for item in second.retired_careers]==[8,9]
    assert second.retired_careers[:-1]==previous and first.retired_careers==previous
    second.retired_careers[0]["highlights"].append("Changed copy")
    assert first.retired_careers==previous
    second.retired_careers[0]["highlights"].pop()
    vr.write_save(tmp_path,77,second)
    saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
    assert saved.retired_careers==second.retired_careers and saved.pilot.retirements==9


@pytest.mark.parametrize("patch",[{"version":True},{"number":0},{"number":2},{"rank":0},{"finale":"unknown"},{"charted":49},{"market_margin":False},{"seed":"42"},{"highlights":["bad\x1b"]},{"ship":"imaginary"},{"credits":-1},{"days":1.5}])
def test_career_finale_malformed_dossier_preserves_original(tmp_path,patch):
    import json
    fresh=vr.finish_career(_finale_world().save,"legend"); fresh.retired_careers[0].update(patch)
    path=tmp_path/"77.json"; raw=json.dumps(fresh.to_dict()).encode(); path.write_bytes(raw)
    with pytest.raises(vr.ResumeError):vr.load_or_create_save(tmp_path,77,"Tester")
    assert path.read_bytes()==raw


@pytest.mark.parametrize("patch",[{"version":2},{"future":True}])
def test_career_finale_future_dossier_cannot_be_downgraded(tmp_path,patch):
    import json
    fresh=vr.finish_career(_finale_world().save,"legend"); fresh.retired_careers[0].update(patch)
    path=tmp_path/"77.json"; raw=json.dumps(fresh.to_dict()).encode(); path.write_bytes(raw)
    with pytest.raises(vr.UnsupportedSave):vr.load_or_create_save(tmp_path,77,"Tester")
    assert path.read_bytes()==raw


@pytest.mark.parametrize("records",[None,{},[{}]])
def test_career_finale_invalid_archive_container_is_preserved(tmp_path,records):
    import json
    data=_world_with_seed(42).save.to_dict(); data["retired_careers"]=records
    path=tmp_path/"77.json"; raw=json.dumps(data).encode(); path.write_bytes(raw)
    with pytest.raises(vr.ResumeError):vr.load_or_create_save(tmp_path,77,"Tester")
    assert path.read_bytes()==raw


def test_career_finale_full_archive_loads_but_never_evicts_to_retire(tmp_path):
    import copy
    fresh=vr.finish_career(_finale_world().save,"legend")
    fresh.retired_careers=[dict(fresh.retired_careers[0],number=i+1) for i in range(vr.MAX_RETIRED_CAREERS)]
    fresh.pilot.retirements=vr.MAX_RETIRED_CAREERS; fresh.pilot.highest_rank_seen=4
    vr.write_save(tmp_path,77,fresh); saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
    before=copy.deepcopy(saved.to_dict())
    with pytest.raises(ValueError,match="archive full"):vr.finish_career(saved,"legend")
    assert saved.to_dict()==before and len(saved.retired_careers)==128
    saved.retired_careers.append(dict(saved.retired_careers[-1],number=129)); saved.pilot.retirements=129
    with pytest.raises(vr.SaveError):vr.write_save(tmp_path,77,saved)


@pytest.mark.parametrize("finale,index",[("legend",1),("trader",2),("explorer",3),("combat",4)])
def test_career_finale_real_kill_ack_keeps_dossier_and_new_equipment(tmp_path,finale,index):
    world=_finale_world(finale); world.save.pilot.highlight("Preserved through disconnect")
    world._checkpoint=lambda w:vr.persist(w,tmp_path,77); world.checkpoint()
    with _door_stopped_at(tmp_path,f"SR{index}SY".encode(),b"A new career begins."):
        saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
        assert saved.pilot.retirements==1 and saved.retired_careers[0]["finale"]==finale
        assert "Preserved through disconnect" in saved.retired_careers[0]["highlights"]
        tier=vr.CAREER_FINALES[finale]["tier"]
        if tier:assert getattr(saved.ship,tier+"_tier")==1
        assert saved.pending_travel is None and not saved.faction_stories and saved.pilot.highest_rank_seen==0


@pytest.mark.parametrize("commands",[b"SR",b"SR2><BBQ",b"SR1SNBBQ",b"SD><BQ"])
def test_career_finale_real_browse_refusal_eof_write_nothing(tmp_path,commands):
    import json,os,subprocess
    world=_finale_world(); world._checkpoint=lambda w:vr.persist(w,tmp_path,77); world.checkpoint()
    info=tmp_path/"door_info.json"; info.write_text(json.dumps({"user_id":77,"handle":"Tester","terminal_width":40,"terminal_height":12}),encoding="utf-8")
    before=(tmp_path/"77.json").read_bytes()
    result=subprocess.run([sys.executable,str(_VOIDRUNNER_PATH)],input=commands,capture_output=True,timeout=10,
        env=dict(os.environ,VOIDRUNNER_SAVE_DIR=str(tmp_path),NETBBS_DOOR_INFO=str(info)))
    assert result.returncode==0 and not result.stderr
    assert (tmp_path/"77.json").read_bytes()==before
    if b"N" in commands:assert b"Retirement cancelled" in result.stdout


@pytest.mark.parametrize("finale",list(vr.CAREER_FINALES))
def test_career_finale_checkpoint_failure_stops_before_new_run_ack(monkeypatch,finale):
    world=_finale_world(finale); output=io.StringIO(); keys=iter("SY")
    monkeypatch.setattr(vr,"read_key",lambda:next(keys))
    def fail(current):
        assert len(current.save.retired_careers)==1 and "A new career begins." not in output.getvalue()
        raise vr.SaveError()
    world._checkpoint=fail
    with contextlib.redirect_stdout(output),pytest.raises(vr.SaveError):vr.screen_career_finale(vr.Palette(False),world)


@pytest.mark.parametrize("width,height",[(20,10),(40,12),(80,24)])
@pytest.mark.parametrize("finale",list(vr.CAREER_FINALES))
def test_career_finale_complete_terms_fit_every_page_without_writes(monkeypatch, terminal,width,height,finale):
    import copy,re
    world=_finale_world(finale); before=copy.deepcopy(world.save.to_dict()); rng=world.event_rng.getstate()
    terminal(width, height)
    output=io.StringIO(); bodies=[]; world._checkpoint=lambda w:pytest.fail("Finale browsing checkpointed")
    def choose():
        frame=output.getvalue(); output.seek(0); output.truncate(0)
        assert len(frame.splitlines())<=height and all(vr._visible_width(row)<=width for row in frame.splitlines())
        plain=vr._ANSI_RE.sub("",frame); page,count=map(int,re.search(r"Career Finale\s+(\d+)/(\d+)",plain).groups())
        body=re.sub(r"^[\s>]*Career Finale\s+\d+/\d+\s*","",plain)
        bodies.append(body.split("[1-4] Choose")[0]); return "B" if page==count else ">"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):assert vr.screen_career_finale(vr.Palette(False),world) is None
    assert " ".join(" ".join(bodies).split())==" ".join(" ".join(vr.career_finale_lines(world.save,finale)).split())
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng


@pytest.mark.parametrize("width,height",[(20,10),(40,12),(80,24)])
def test_career_dossier_pages_include_all_retained_highlights(monkeypatch, terminal,width,height):
    import copy,re
    world=_finale_world(); world.save.pilot.highlights=[f"Old highlight {i:02}" for i in range(vr.MAX_HIGHLIGHTS)]
    world.reset(vr.finish_career(world.save,"legend")); before=copy.deepcopy(world.save.to_dict())
    output=io.StringIO(); bodies=[]; first=True
    terminal(width, height)
    world._checkpoint=lambda w:pytest.fail("Dossier browsing checkpointed")
    def choose():
        nonlocal first
        frame=output.getvalue(); output.seek(0); output.truncate(0)
        assert len(frame.splitlines())<=height and all(vr._visible_width(row)<=width for row in frame.splitlines())
        if first:first=False;return "D"
        plain=vr._ANSI_RE.sub("",frame); page,count=map(int,re.search(r"Dossiers\s+(\d+)/(\d+)",plain).groups())
        body=re.sub(r"^[\s>D]*Pilot Record:\s*Dossiers\s+\d+/\d+\s*","",plain)
        bodies.append(body.split("[<")[0]); return "B" if page==count else ">"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):vr.screen_status(vr.Palette(False),world)
    text=" ".join(" ".join(bodies).split())
    assert " ".join(" ".join(vr.career_dossier_lines(world.save)).split())==text
    for i in range(vr.MAX_HIGHLIGHTS):assert text.count(f"Old highlight {i:02}")==1
    assert world.save.to_dict()==before


def test_career_finale_atomic_replace_failure_preserves_old_run_and_no_ack(monkeypatch,tmp_path):
    world=_finale_world(); world._checkpoint=lambda w:vr.persist(w,tmp_path,77);world.checkpoint()
    before=(tmp_path/"77.json").read_bytes(); output=io.StringIO(); original=vr._write_bytes_atomic
    def fail_current(path,data):
        if path.name=="77.json":raise OSError("replacement unavailable")
        original(path,data)
    monkeypatch.setattr(vr,"_write_bytes_atomic",fail_current)
    keys=iter("SY");monkeypatch.setattr(vr,"read_key",lambda:next(keys))
    with contextlib.redirect_stdout(output),pytest.raises(vr.SaveError):vr.screen_career_finale(vr.Palette(False),world)
    assert "A new career begins." not in output.getvalue() and (tmp_path/"77.json").read_bytes()==before
    saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
    assert saved.pilot.retirements==0 and not saved.retired_careers


@pytest.mark.parametrize("path,value,label",[("trader",0,"Beginning"),("trader",5000,"Broker"),("trader",20000,"Guild Merchant"),("trader",50000,"Founder"),("explorer",1,"Local"),("explorer",12,"Scout"),("explorer",30,"Pathfinder"),("explorer",48,"Cartographer"),("combat",0,"Unproven"),("combat",5,"Escort"),("combat",20,"Defender"),("combat",50,"Ace")])
def test_career_finale_path_titles_match_recorded_accomplishment(path,value,label):
    world=_world_with_seed(42)
    if path=="trader":world.save.trading_ledger.sales_revenue=value
    elif path=="explorer":world.save.discovered=list(range(value))
    else:world.save.pilot.kills=value
    assert f"{path.title()} ({label}):" in " ".join(vr.career_path_lines(world.save))


@pytest.mark.parametrize("paid,bonus",[(0,1),(5,2),(15,3),(30,4)])
@pytest.mark.parametrize("hired",[True,False])
def test_promoted_navigator_survey_terms_match_actual_range_without_writes(paid,bonus,hired):
    import copy
    world=_world_with_named_crew("navigator",paid); world.save.ship.scanner_tier=1; world.save.ship.has_navigator=hired
    before=copy.deepcopy(world.save.to_dict()); rng=world.event_rng.getstate()
    terms=" ".join(vr.survey_terms(world)); actual=bonus if hired else 0
    assert f"Navigator bonus: +{actual} connection hops." in terms
    assert f"Range: {3+actual} connection hops" in terms
    distances=vr.bfs_hops(world.by_id,world.here.id)
    assert set(vr.survey_candidates(world))=={sid for sid,hops in distances.items() if hops<=3+actual and not world.by_id[sid].discovered}
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng


@pytest.mark.parametrize("role", list(vr.CREW_ROLES))
@pytest.mark.parametrize("paid", [5,15,30])
def test_personal_crew_roster_retained_experience_unlocks_on_rehire(role,paid):
    import copy
    world=_world_with_named_crew(role,paid)
    vr.dismiss_crew(world,role)
    before=copy.deepcopy(world.save.to_dict()); rng=world.event_rng.getstate()
    assert "available after rehiring" in " ".join(vr.crew_roster_lines(world))
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng
    vr.hire_crew(world,role)
    assert vr.crew_assignment_blocker(world,role) is None
    vr.accept_crew_assignment(world,role)
    assert vr.crew_assignment_record(world,role)["state"]=="active"


@pytest.mark.parametrize("finale,category,metric", [("trader","trading","market_margin"),("explorer","exploration","charted"),("combat","combat","kills")])
def test_achievement_scores_retain_actual_career_after_retirement_and_restart(tmp_path,finale,category,metric):
    world=_finale_world(finale); world._checkpoint=lambda w:vr.persist(w,tmp_path,77); world.checkpoint()
    before=vr.achievement_ranking(vr._load_score_records(tmp_path),category)[0]
    world.reset(vr.finish_career(world.save,finale)); world.checkpoint()
    loaded,_,_=vr.load_or_create_save(tmp_path,77,"Tester"); vr.persist(vr.World(loaded),tmp_path,77)
    entries=vr._load_score_records(tmp_path); ranked=vr.achievement_ranking(entries,category)
    assert ranked[0][metric]==before[metric] and ranked[0]["number"]==1 and ranked[0]["seed"]==before["seed"]
    assert ranked[0]["finale"]==finale and len(entries[0]["achievements"]["careers"])==2
    assert entries[0]["achievements"]["careers"][-1]["number"]==2
    assert vr.achievement_ranking(entries,"careers")[0]["retirements"]==1


def test_achievement_scores_failed_retirement_projection_repairs_from_saved_dossiers(tmp_path,monkeypatch):
    world=_finale_world("combat"); world._checkpoint=lambda w:vr.persist(w,tmp_path,77); world.checkpoint()
    score=tmp_path/"scores"/"77.json"; original=score.read_bytes(); replace=vr.os.replace
    world.reset(vr.finish_career(world.save,"combat"))
    def fail(source,target):
        if target.parent.name=="scores":raise OSError("optional score write failed")
        return replace(source,target)
    with monkeypatch.context() as patch:
        patch.setattr(vr.os,"replace",fail); world.checkpoint()
    assert score.read_bytes()==original
    loaded,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
    assert loaded.retired_careers[0]["kills"]==50 and loaded.pilot.kills==0
    vr.persist(vr.World(loaded),tmp_path,77)
    rows=vr.achievement_ranking(vr._load_score_records(tmp_path),"combat")
    assert len(rows)==1 and rows[0]["kills"]==50 and rows[0]["finale"]=="combat"


def test_achievement_ranking_reads_pilots_outside_wealth_top_twenty(tmp_path):
    import json
    for uid in range(1,26):
        save=vr._new_career(f"Pilot-{uid}"); save.pilot.credits=uid*1000; save.pilot.kills=100 if uid==1 else uid
        vr.update_hall_of_fame(tmp_path,uid,save)
    paths=list((tmp_path/"scores").glob("*.json")); before={p:p.read_bytes() for p in paths}
    assert 1 not in {entry["user_id"] for entry in vr.load_hall_of_fame(tmp_path)}
    assert vr.achievement_ranking(vr._load_score_records(tmp_path),"combat")[0]["user_id"]==1
    assert len(vr.achievement_ranking(vr._load_score_records(tmp_path),"combat"))==20
    assert len(paths)==25 and {p:p.read_bytes() for p in paths}==before


def test_achievement_summary_retains_every_dossier_at_capacity_and_legacy_gaps(tmp_path):
    save=vr._new_career("Veteran"); save.pilot.retirements=7
    for _ in range(vr.MAX_RETIRED_CAREERS):
        save.pilot.kills=50; save=vr.finish_career(save,"combat")
    vr.write_save(tmp_path,77,save); vr.update_hall_of_fame(tmp_path,77,save)
    loaded,_,_=vr.load_or_create_save(tmp_path,77,"Veteran")
    record=vr._load_score_records(tmp_path)[0]; careers=record["achievements"]["careers"]
    assert len(careers)==129 and [c["number"] for c in careers]==list(range(8,137))
    assert len(loaded.retired_careers)==128 and record["retirements"]==135
    assert [row["number"] for row in vr.achievement_ranking([record],"combat")]==list(range(8,28))
    assert "135 completed careers; 128 recorded conclusions" in " ".join(vr.achievement_lines([record],"careers",77))


def test_achievement_imported_scores_never_fabricate_per_career_metrics(tmp_path):
    import json
    older={"user_id":77,"handle":"Older","best_credits":10000,"retirements":9,"kills":80}
    path=tmp_path/"leaderboard.json"; path.write_text(json.dumps([older]),encoding="utf-8"); original=path.read_bytes()
    vr.import_hall_of_fame(tmp_path)  # the old file reaches the rankings through `scores/` (#421)
    entries=vr._load_score_records(tmp_path)
    assert vr.achievement_ranking(entries,"wealth")[0]["best_credits"]==10000
    assert vr.achievement_ranking(entries,"careers")[0]["retirements"]==9
    for category in ("trading","exploration","combat"):
        assert vr.achievement_ranking(entries,category)==[]
        assert "Older score files" in " ".join(vr.achievement_lines(entries,category,77))
    assert path.read_bytes()==original


@pytest.mark.parametrize("fault", ["version","extra","record_extra"])
def test_achievement_future_score_summary_survives_current_checkpoint(tmp_path,fault):
    import json
    world=_world_with_seed(42); vr.persist(world,tmp_path,77); path=tmp_path/"scores"/"77.json"
    data=json.loads(path.read_text(encoding="utf-8"))
    if fault=="version":data["achievements"]["version"]=2
    elif fault=="extra":data["achievements"]["future"]=True
    else:data["achievements"]["careers"][0]["future"]=True
    path.write_text(json.dumps(data),encoding="utf-8"); before=path.read_bytes()
    world.save.pilot.credits+=10;vr.persist(world,tmp_path,77)
    assert path.read_bytes()==before and vr.achievement_ranking(vr._load_score_records(tmp_path),"combat")==[]
    loaded,_,_=vr.load_or_create_save(tmp_path,77,"Tester");assert loaded.pilot.credits==1210


@pytest.mark.parametrize("fault", ["summary_null","records_null","records_scalar","empty","many","record_null","boolean","negative","margin_bool","charted","sequence","finale","ended","started"])
def test_achievement_malformed_optional_summaries_do_not_break_scores_or_checkpoint(tmp_path,fault):
    import json
    world=_world_with_seed(42);vr.persist(world,tmp_path,77);path=tmp_path/"scores"/"77.json"
    data=json.loads(path.read_text(encoding="utf-8")); summary=data["achievements"]; record=summary["careers"][0]
    if fault=="summary_null":data["achievements"]=None
    elif fault=="records_null":summary["careers"]=None
    elif fault=="records_scalar":summary["careers"]=3
    elif fault=="empty":summary["careers"]=[]
    elif fault=="many":summary["careers"]=[record]*130
    elif fault=="record_null":summary["careers"]=[None]
    elif fault=="boolean":record["kills"]=True
    elif fault=="negative":record["kills"]=-1
    elif fault=="margin_bool":record["market_margin"]=False
    elif fault=="charted":record["charted"]=49
    elif fault=="sequence":record["number"]=3
    elif fault=="finale":record["finale"]="combat"
    elif fault=="ended":record["ended"]="yesterday"
    else:record["started"]=[]
    path.write_text(json.dumps(data),encoding="utf-8")
    assert "achievements" not in vr._load_score_records(tmp_path)[0]
    vr.persist(world,tmp_path,77)
    assert vr._load_score_records(tmp_path)[0]["achievements"]["careers"][0]["number"]==1


@pytest.mark.parametrize("width,height", [(20,10),(40,12),(80,24)])
@pytest.mark.parametrize("style", list(vr.DISPLAY_STYLES))
def test_achievement_category_pages_keep_snapshot_complete_terms_and_all_navigation(tmp_path,monkeypatch, terminal,width,height,style):
    import re
    world=_finale_world("combat");world.save.trading_ledger.sales_revenue=52000;world.save.discovered=list(range(48))
    world.save=vr.finish_career(world.save,"combat");vr.update_hall_of_fame(tmp_path,77,world.save)
    record_path=tmp_path/"scores"/"77.json"; before=record_path.read_bytes(); entries=vr._load_score_records(tmp_path)
    terminal(width, height, style)
    output=io.StringIO(); bodies={c:[] for c in vr.SCORE_CATEGORIES}; category=0; loads=[]
    original=vr._load_score_records
    def load(directory):loads.append(1);return original(directory)
    monkeypatch.setattr(vr,"_load_score_records",load)
    def choose():
        nonlocal category
        frame=output.getvalue();output.seek(0);output.truncate(0)
        assert len(frame.splitlines())<=height and all(vr._visible_width(row)<=width for row in frame.splitlines())
        plain=vr._ANSI_RE.sub("",frame)
        assert "[1-5] View" in " ".join(plain.split()) and "[B] Back:" in " ".join(plain.split())
        match=re.search(r"(\d+)/(\d+)",plain);page,count=map(int,match.groups())
        bodies[list(vr.SCORE_CATEGORIES)[category]].append(plain[match.end():].split("[1-5] View")[0])
        if page<count:return "N"
        category+=1;return str(category+1) if category<5 else "B"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):vr.screen_hall_of_fame(vr.Palette(False),world,tmp_path,77)
    assert loads==[1] and record_path.read_bytes()==before
    for category,parts in bodies.items():
        text=" ".join(" ".join(parts).split())
        for line in vr.achievement_lines(entries,category,77):assert " ".join(line.split()) in text


@pytest.mark.parametrize("commands", [b"H2345",b"H2N3NP4N5NBQ"])
def test_real_achievement_category_browsing_keeps_career_and_score_bytes(tmp_path,commands):
    import json,os,subprocess
    world=_finale_world("combat");world._checkpoint=lambda w:vr.persist(w,tmp_path,77);world.checkpoint()
    paths=[tmp_path/"77.json",tmp_path/"scores"/"77.json"];before={p:p.read_bytes() for p in paths}
    info=tmp_path/"door_info.json";info.write_text(json.dumps({"user_id":77,"handle":"Tester","terminal_width":40,"terminal_height":12}),encoding="utf-8")
    result=subprocess.run([sys.executable,str(_VOIDRUNNER_PATH)],input=commands,capture_output=True,timeout=10,
        env=dict(os.environ,VOIDRUNNER_SAVE_DIR=str(tmp_path),NETBBS_DOOR_INFO=str(info)))
    assert result.returncode==0 and not result.stderr and b"[1-5] View" in result.stdout
    assert {p:p.read_bytes() for p in paths}==before


@pytest.mark.parametrize("revenue,cost,expected", [(7000,2000,5000),(1000,2000,-1000),(2000,2000,0)])
def test_achievement_trading_uses_known_market_margin_without_delivery_or_operating_costs(tmp_path,revenue,cost,expected):
    world=_world_with_seed(42);ledger=world.save.trading_ledger
    ledger.sales_revenue=revenue;ledger.sales_cost=cost;ledger.uncosted_sales=80000
    ledger.delivery_revenue=60000;ledger.delivery_cost=9000;ledger.uncosted_deliveries=20000
    ledger.fuel_spend=300;ledger.wages=400;world.save.pilot.credits=250000
    vr.update_hall_of_fame(tmp_path,77,world.save); entries=vr._load_score_records(tmp_path)
    assert entries[0]["achievements"]["careers"][0]["market_margin"]==expected
    ranked=vr.achievement_ranking(entries,"trading")
    assert len(ranked)==int(expected>0)
    if ranked:assert ranked[0]["market_margin"]==5000


def test_achievement_score_summary_over_old_64k_limit_survives_restart(tmp_path):
    save=vr._new_career("Archivist")
    for _ in range(10):
        save.pilot.career_started="A"*4096;save.pilot.kills=50
        save=vr.finish_career(save,"combat");save.retired_careers[-1]["ended"]="B"*4096
    vr.write_save(tmp_path,77,save);vr.update_hall_of_fame(tmp_path,77,save)
    path=tmp_path/"scores"/"77.json"
    assert 65536<path.stat().st_size<vr.MAX_SAVE_BYTES
    records=vr._load_score_records(tmp_path)
    assert len(records[0]["achievements"]["careers"])==11
    assert len(vr.achievement_ranking(records,"combat"))==10


@pytest.mark.parametrize("width,height", [(40,12),(80,24)])
@pytest.mark.parametrize("category", ["trading","exploration","combat","careers"])
def test_achievement_first_page_shows_ranked_pilot_before_long_counting_rules(tmp_path,monkeypatch, terminal,width,height,category):
    world=_finale_world("combat");world.save.trading_ledger.sales_revenue=50000
    world.save=vr.finish_career(world.save,"combat");world.save.pilot.handle="VisiblePilot"
    vr.update_hall_of_fame(tmp_path,77,world.save)
    terminal(width, height)
    output=io.StringIO();keys=iter([str(list(vr.SCORE_CATEGORIES).index(category)+1),"B"])
    def choose():
        key=next(keys)
        if key=="B":assert "VisiblePilot" in output.getvalue()
        output.seek(0);output.truncate(0);return key
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):vr.screen_hall_of_fame(vr.Palette(False),world,tmp_path,77)


@pytest.mark.parametrize("retired", [False,True])
def test_achievement_unchanged_checkpoint_after_restart_never_replaces_files(tmp_path,monkeypatch,retired):
    world=_finale_world("combat")
    if retired:world.reset(vr.finish_career(world.save,"combat"))
    vr.persist(world,tmp_path,77)
    paths=[tmp_path/"77.json",tmp_path/"scores"/"77.json"]
    before={p:(p.read_bytes(),p.stat().st_mtime_ns,p.stat().st_ino) for p in paths}
    loaded,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
    def replaced(*args):pytest.fail("Unchanged checkpoint replaced a file")
    monkeypatch.setattr(vr.os,"replace",replaced)
    vr.persist(vr.World(loaded),tmp_path,77)
    assert {p:(p.read_bytes(),p.stat().st_mtime_ns,p.stat().st_ino) for p in paths}==before


@pytest.mark.parametrize("key", ["P","W"])
@pytest.mark.parametrize("commands", [b"",b"B",b">?<B",b"JNB"])
def test_real_faction_browsing_does_not_replace_score_after_input(tmp_path,key,commands):
    import json,os,subprocess
    world=_world_with_seed(42);world.save.pilot.reputation={f:75 for f in vr.FACTIONS}
    vr.persist(world,tmp_path,77)
    info=tmp_path/"door_info.json";info.write_text(json.dumps({"user_id":77,"handle":"Tester","terminal_width":40,"terminal_height":12}),encoding="utf-8")
    script="""
import runpy,sys,os
v=runpy.run_path(sys.argv[1]);g=v['main'].__globals__
original_key=g['read_key'];original_replace=os.replace
entered=False;replaced=[]
def read_key():
    global entered
    try:key=original_key()
    except EOFError:
        entered=False;raise
    entered=True;return key
def replace(source,target):
    result=original_replace(source,target)
    if entered and target.parent.name=='scores':replaced.append(target.name)
    return result
g['read_key']=read_key;os.replace=replace
code=g['main']()
print('REPLACED:'+str(len(replaced)),file=sys.stderr)
raise SystemExit(code)
"""
    result=subprocess.run([sys.executable,"-c",script,str(_VOIDRUNNER_PATH)],input=key.encode()+commands,capture_output=True,timeout=10,
        env=dict(os.environ,VOIDRUNNER_SAVE_DIR=str(tmp_path),NETBBS_DOOR_INFO=str(info)))
    assert result.returncode==0 and result.stderr.strip()==b"REPLACED:0"
    assert (b"Edda Ro" if key=="P" else b"Rook Talan") in result.stdout


@pytest.mark.parametrize("faction", vr.FACTIONS)
@pytest.mark.parametrize("stage", ["idle","accepted","evidence"])
def test_faction_case_ending_selectors_only_appear_when_choice_is_available(faction,stage):
    import copy
    world=_faction_case_world(faction,stage);before=copy.deepcopy(world.save.to_dict());rng=world.event_rng.getstate()
    lines=vr.faction_story_lines(world,faction)
    for choice,key in [("hardline","H"),("aid","A")]:
        label=vr.FACTION_STORIES[faction][choice]["label"]
        row=next(line for line in lines if label+":" in line)
        assert row.startswith(f"[{key}] ")==(stage=="evidence")
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng


@pytest.mark.parametrize("faction", vr.FACTIONS)
def test_faction_case_route_becomes_available_only_after_committing_ending(monkeypatch,faction):
    import copy
    world=_faction_case_world(faction,"evidence");before=copy.deepcopy(world.save.to_dict());rng=world.event_rng.getstate()
    routes=[];checkpoints=[];world._checkpoint=lambda w:checkpoints.append(w.save.faction_stories[faction]["stage"])
    output=io.StringIO();keys=iter("RARB")
    def choose():
        key=next(keys);frame=output.getvalue();output.seek(0);output.truncate(0)
        if world.save.faction_stories[faction]["stage"]=="evidence":
            assert "[R] Route" not in frame and world.save.to_dict()==before and world.event_rng.getstate()==rng
        else:assert "[R] Route" in frame
        return key
    monkeypatch.setattr(vr,"read_key",choose)
    monkeypatch.setattr(vr,"screen_auto_route",lambda p,w,*,destination:routes.append(destination))
    with contextlib.redirect_stdout(output):vr.screen_faction_story(vr.Palette(False),world,faction)
    assert routes==[vr.faction_story_target(world,faction,"aid")] and checkpoints==["committed"]


def test_faction_case_missing_haven_does_not_advertise_or_dispatch_hardline(monkeypatch):
    world=_faction_case_world(vr.FACTION_BLACKWAKE,"evidence")
    for system in world.galaxy:
        if system.economy=="Haven":system.economy="Industrial"
    keys=iter("HAB");output=io.StringIO();seen=[]
    def choose():
        key=next(keys);frame=output.getvalue();output.seek(0);output.truncate(0)
        assert "[H] Hardline" not in frame
        if key=="A":assert world.save.faction_stories[vr.FACTION_BLACKWAKE]["stage"]=="evidence"
        return key
    world._checkpoint=lambda w:seen.append(w.save.faction_stories[vr.FACTION_BLACKWAKE]["choice"])
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):vr.screen_faction_story(vr.Palette(False),world,vr.FACTION_BLACKWAKE)
    assert seen==["aid"]


@pytest.mark.parametrize("faction", vr.FACTIONS)
def test_faction_case_idle_route_is_unavailable_until_acceptance(monkeypatch,faction):
    import copy
    world=_faction_case_world(faction,"idle");before=copy.deepcopy(world.save.to_dict());rng=world.event_rng.getstate()
    routes=[];checkpoints=[];world._checkpoint=lambda w:checkpoints.append(w.save.faction_stories[faction]["stage"])
    output=io.StringIO();keys=iter("RARB")
    def choose():
        key=next(keys);frame=output.getvalue();output.seek(0);output.truncate(0)
        if faction not in world.save.faction_stories:
            assert "[R] Route" not in frame and world.save.to_dict()==before and world.event_rng.getstate()==rng
        else:assert "[R] Route" in frame
        return key
    monkeypatch.setattr(vr,"read_key",choose)
    monkeypatch.setattr(vr,"screen_auto_route",lambda p,w,*,destination:routes.append(destination))
    with contextlib.redirect_stdout(output):vr.screen_faction_story(vr.Palette(False),world,faction)
    assert routes==[vr.faction_story_target(world,faction)] and checkpoints==["accepted"]


@pytest.mark.parametrize("width,height", [(20,10),(40,12),(80,24)])
def test_career_rank_checkpoint_notice_survives_spending_until_deck(monkeypatch, terminal,tmp_path,width,height):
    import re
    world=_world_with_seed(42);world._checkpoint=lambda w:vr.persist(w,tmp_path,77)
    world.save.pilot.credits=4999;_set_cargo(world, {"food":1});world.checkpoint()
    keys=iter("ASQ");monkeypatch.setattr(vr,"read_key",lambda:next(keys))
    monkeypatch.setattr(vr,"read_line_raw",lambda **kw:"1")
    with contextlib.redirect_stdout(io.StringIO()):vr.screen_market(vr.Palette(False),world)
    world.save.pilot.credits=100;world.checkpoint();world.checkpoint()
    saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
    assert saved.pilot.highest_rank_seen==1 and saved.pilot.credits==100
    terminal(width, height)
    output=io.StringIO();bodies=[]
    def choose():
        frame=output.getvalue();output.seek(0);output.truncate(0)
        assert len(frame.splitlines())<=height and all(vr._visible_width(line)<=width for line in frame.splitlines())
        plain=vr._ANSI_RE.sub("",frame);match=re.search(r"Command Deck:.*?(\d+)/(\d+)",plain,re.S);assert match
        page,count=map(int,match.groups());bodies.append(plain[match.end():].split("[<] Prev")[0])
        return "Q" if page==count else ">"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):vr.screen_station_menu(vr.Palette(False),world)
    text=" ".join(" ".join(bodies).split())
    assert text.count("Promoted to Independent Trader; rank retained for this career.")==1
    monkeypatch.setattr(vr,"read_key",lambda:"Q")
    with contextlib.redirect_stdout(output):vr.screen_station_menu(vr.Palette(False),world)
    assert "Promoted to" not in output.getvalue()


def test_career_rank_new_career_does_not_repeat_old_promotion_notice(monkeypatch):
    world=_world_with_seed(42);world.save.pilot.credits=5000;world.checkpoint()
    world.reset(vr.retire_pilot(world.save));world.checkpoint()
    monkeypatch.setattr(vr,"read_key",lambda:"Q")
    with contextlib.redirect_stdout(io.StringIO()) as output:vr.screen_station_menu(vr.Palette(False),world)
    assert "Promoted to" not in output.getvalue()


@pytest.mark.parametrize("count", [41,128])
def test_career_finale_preserves_accepted_legacy_highlight_lists(monkeypatch,tmp_path,count):
    world=_finale_world();highlights=[f"Legacy achievement {i}" for i in range(count)]
    world.save.pilot.highlights=list(highlights);world._checkpoint=lambda w:vr.persist(w,tmp_path,77);world.checkpoint()
    old,_,_=vr.load_or_create_save(tmp_path,77,"Tester");assert old.pilot.highlights==highlights
    keys=iter("SY");monkeypatch.setattr(vr,"read_key",lambda:next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:vr.screen_career_finale(vr.Palette(False),world)
    assert "A new career begins." in output.getvalue()
    saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
    assert saved.pilot.retirements==1 and saved.retired_careers[0]["highlights"]==highlights
    lines=vr.career_dossier_lines(saved)
    assert [line[2:] for line in lines if line.startswith("* ")]==highlights


def test_career_finale_legacy_highlights_survive_real_retirement_disconnect(tmp_path):
    world=_finale_world();highlights=[f"Legacy event {i}" for i in range(41)]
    world.save.pilot.highlights=list(highlights);world._checkpoint=lambda w:vr.persist(w,tmp_path,77);world.checkpoint()
    with _door_stopped_at(tmp_path,b"SRSY",b"A new career begins."):
        saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
        assert saved.pilot.retirements==1 and saved.retired_careers[0]["highlights"]==highlights


@pytest.mark.parametrize("version", [2,99])
def test_career_finale_future_incomplete_dossier_disables_recovery(tmp_path,monkeypatch,version):
    import json
    fresh=vr.finish_career(_finale_world().save,"legend");vr.write_save(tmp_path,77,fresh)
    previous=(tmp_path/"77.json").read_bytes();(tmp_path/"77.previous.json").write_bytes(previous)
    data=fresh.to_dict();data["retired_careers"]=[{"version":version}]
    raw=json.dumps(data).encode();(tmp_path/"77.json").write_bytes(raw)
    with pytest.raises(vr.UnsupportedSave) as error:vr.load_or_create_save(tmp_path,77,"Tester")
    monkeypatch.setattr(vr,"read_key",lambda:"Q")
    with contextlib.redirect_stdout(io.StringIO()) as output:vr.screen_save_recovery(vr.Palette(False),tmp_path,77,error.value)
    assert "[R]" not in output.getvalue() and (tmp_path/"77.json").read_bytes()==raw
    assert (tmp_path/"77.previous.json").read_bytes()==previous


def test_career_finale_size_preflight_precedes_confirmation_and_keeps_legacy_career(tmp_path,monkeypatch):
    import copy,json
    world=_finale_world();world.checkpoint();vr.persist(world,tmp_path,77)
    highlights=["x"*4096 for _ in range(1010)]+[""]
    world.save.pilot.highlights=highlights
    remaining=vr.MAX_SAVE_BYTES-len(json.dumps(world.save.to_dict()).encode())
    while remaining>4096:
        highlights.insert(-1,"x"*4096);remaining=vr.MAX_SAVE_BYTES-len(json.dumps(world.save.to_dict()).encode())
    assert 0<=remaining<=4096
    highlights[-1]="x"*remaining;vr.persist(world,tmp_path,77)
    raw=(tmp_path/"77.json").read_bytes();assert len(raw)==vr.MAX_SAVE_BYTES
    before=copy.deepcopy(world.save.to_dict());rng=world.event_rng.getstate()
    monkeypatch.setattr(vr,"confirm",lambda *args:pytest.fail("Oversized retirement offered confirmation"))
    keys=iter("SB");monkeypatch.setattr(vr,"read_key",lambda:next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:result=vr.screen_career_finale(vr.Palette(False),world)
    assert result and "Retirement unavailable" in result and "Current career retained" in result
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng and (tmp_path/"77.json").read_bytes()==raw
    saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester");assert saved.pilot.retirements==0


def test_retiring_with_active_contracts_records_them_as_abandoned():
    world = _world_with_seed(42); world.save.pilot.kills = 50
    dest = sorted(world.here.connections)[0]
    world.save.active_missions = [vr.Mission(1, "delivery", "Deliver goods", 300, 0, dest, commodity="food", quantity=2),
                                  vr.Mission(2, "escort", "Escort a convoy", 800, 0, dest, pirate_tier=1)]
    assert "2 active contract(s) count as abandoned" in " ".join(vr.career_finale_lines(world.save, "combat"))
    import copy; before = copy.deepcopy(world.save.to_dict())
    fresh = vr.finish_career(world.save, "combat")
    assert fresh.retired_careers[-1]["failed"] == 2 and not fresh.active_missions
    assert any("Abandoned at retirement: Escort a convoy (forfeited 800cr)" in entry for entry in fresh.pilot.log)
    assert world.save.to_dict() == before  # a cancelled retirement leaves the live career untouched
    assert vr.finish_career(world.save, "combat").retired_careers[-1]["failed"] == 2  # and repeating does not inflate it


def test_long_wide_callsign_is_stacked_intact_rather_than_clipped(monkeypatch):
    handle = "船" * 16  # a valid 16-character callsign, 32 display columns
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    rows = vr.title_rows({"node_name": "A Very Long BBS Node Name Here", "handle": handle}, vr._box_inner_width())
    meta = [text for kind, text, _ in rows if kind == "meta"]
    assert len(meta) == 3 and f"  PILOT: {handle}" in meta
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_title(vr.Palette(False), {"node_name": "A Very Long BBS Node Name Here", "handle": handle})
    rows = _box_rows(output.getvalue())
    assert handle in " ".join(rows) and {vr._visible_width(row) for row in rows} == {vr._box_outer_width()}


def test_stacked_title_fields_and_wordmark_wrap_instead_of_trimming(monkeypatch):
    long_handle = "A" * 32
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 40)
    rows = vr.title_rows({"node_name": "ReLink", "handle": long_handle}, vr._box_inner_width())
    meta = "".join(text.strip() for kind, text, _ in rows if kind == "meta")
    assert f"PILOT: {long_handle}" in meta.replace("PILOT:", "PILOT: ").replace("  ", " ") or long_handle in meta
    assert all(vr._visible_width(text) <= vr._box_inner_width() for _, text, _ in rows)
    for width in (11, 8, 3):
        monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
        rows = vr.title_rows({"node_name": "N", "handle": "P"}, vr._box_inner_width())
        assert "".join(text for kind, text, _ in rows if kind == "wordmark") == "VOIDRUNNER"
        with contextlib.redirect_stdout(io.StringIO()) as output:
            vr.screen_title(vr.Palette(False), {"node_name": "N", "handle": "P"})
        box = _box_rows(output.getvalue())
        assert box and {vr._visible_width(row) for row in box} == {vr._box_outer_width()}


def test_legend_threshold_is_twice_void_baron_and_promotion_never_demotes():
    thresholds = dict((name, value) for value, name in vr.RANKS)
    assert thresholds["Legend of the Frontier"] == 2 * thresholds["Void Baron"] == 150_000
    world = _world_with_seed(42)
    world.save.pilot.credits = 160_000; world.save.pilot.highest_rank_seen = 3
    assert vr.check_rank_up(world) == "Legend of the Frontier"
    world.save.pilot.credits = 10
    assert vr.career_rank(world.save.pilot) == "Legend of the Frontier" and vr.check_rank_up(world) is None


def test_new_fights_use_ruleset_two_and_cached_fights_keep_their_curve():
    world = _world_with_seed(42); pirate = vr.Pirate("Probe", 3, 65, 65)
    tactics = vr.new_tactics(pirate)
    assert tactics["version"] == 2 and vr.tactical_threat_bonus(tactics) == (0, 3, 6, 8, 34)
    assert vr.tactical_threat_bonus({"version": 1}) == (0, 3, 6, 20, 55)
    ship = world.save.ship
    v1 = vr._tactical_incoming_damage(ship, 3, "volley", 9, {"version": 1})
    v2 = vr._tactical_incoming_damage(ship, 3, "volley", 9, {"version": 2})
    assert v1 == 47 and v2 == 28 and v2 < v1


def test_both_tactical_versions_load_and_others_are_unsupported():
    world, pirate = _world_with_pending_fight(tactics={"version": 1, "profile": "Raider", "step": 0, "brace_ready": True})
    vr.SaveData.from_dict(world.save.to_dict())
    world.save.pending_travel["encounter"]["combat"]["tactics"]["version"] = 2
    vr.SaveData.from_dict(world.save.to_dict())
    world.save.pending_travel["encounter"]["combat"]["tactics"]["version"] = 3
    with pytest.raises(vr.UnsupportedSave): vr.SaveData.from_dict(world.save.to_dict())


def test_threat_curve_has_no_cliff_between_adjacent_tiers():
    bonus = vr.TACTICAL_THREAT_BONUS_BY_VERSION[2]
    assert bonus[:3] == vr.TACTICAL_THREAT_BONUS_BY_VERSION[1][:3]  # tiers 0-2 unchanged
    assert bonus[3] - bonus[2] <= 3 and bonus[4] >= 4 * bonus[3]  # no cliff into tier 3; tier 4 still punishes


def test_starter_shuttle_with_brace_survives_tier_three_more_often_than_not():
    import random
    wins = 0
    for seed in range(200):
        world = _world_with_seed(1); world.event_rng.seed(seed)
        ship = world.save.ship; ship.hull_hp = vr.hull_hp_max(ship)
        pirate = vr.Pirate(vr.PIRATE_NAMES[seed % len(vr.PIRATE_NAMES)], 3, 65, 65)
        tactics = vr.new_tactics(pirate)
        while pirate.hp > 0 and ship.hull_hp > 0:
            action = "G" if vr.tactical_intent(tactics) == "volley" and tactics["brace_ready"] else "F"
            vr.tactical_round(world, pirate, tactics, action)
        wins += ship.hull_hp > 0
    assert 70 <= wins <= 160, wins  # about a coin flip with Brace: survivable, not a walkover
    assert 100 <= wins <= 195, wins  # survivable with Brace, not a walkover


def test_escort_completion_earns_concord_standing_and_a_lost_escort_does_not(monkeypatch):
    for outcome, delta in (("won", vr.CONCORD_STANDING_PER_CONTRACT), ("escaped", 0)):
        world, mission = _escort_world(outcome)
        monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: outcome)
        with contextlib.redirect_stdout(io.StringIO()):
            vr._resolve_escort_missions(vr.Palette(False), world, mission.target_system)
        assert world.save.pilot.reputation.get("concord", 0) == delta


def test_an_older_milestone_step_is_rescaled_rather_than_re_awarded():
    """The counter is standing already granted, so halving the step must not
    hand a loaded career free points for gains it was already paid for."""
    world = _world_with_seed(42)
    world.save.contraband_trade_balance = 1000
    world.save.contraband_trade_milestones = 2  # two points, awarded per 500cr
    data = world.save.to_dict()
    data["contraband_standing_step"] = 500  # awarded before the step was halved
    restored = vr.SaveData.from_dict(data)
    assert restored.contraband_trade_milestones == 4  # 1,000cr of gain, now four 250cr steps
    reloaded = vr.World(restored)
    reloaded.save.pilot.reputation["blackwake"] = 0
    vr.record_contraband_trade(reloaded, "weapons", 249)
    assert reloaded.save.pilot.reputation["blackwake"] == 0  # settled gains mint nothing
    vr.record_contraband_trade(reloaded, "weapons", 1)
    assert reloaded.save.pilot.reputation["blackwake"] == 1  # the next genuine step still pays
    assert vr.SaveData.from_dict(reloaded.save.to_dict()).contraband_trade_milestones == 5  # and reloads unchanged
    data["contraband_standing_step"] = 0
    with pytest.raises(vr.ResumeError): vr.SaveData.from_dict(data)
