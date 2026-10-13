"""Accolades: the achievements a pilot's account earns across every career.

They are measured from what the save already records plus a few counters of
their own, unlocked in `World.commit` by the save that earned them, announced
only once that save succeeded, kept through retirement, and projected into the
pilot's score record so a board can say how rare each one is.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json

import pytest

from .support import (_add_cargo, _finale_world, _world_with_exploration_choice, _world_with_pending_fight,
                      _world_with_seed, plain, vr)


def unlocked(save) -> set[str]:
    return set((save.accolades or {}).get("unlocked", {}))


# --- The catalog --------------------------------------------------------------

def test_the_catalog_is_well_formed():
    ids = [accolade.id for accolade in vr.ACCOLADES]
    assert len(ids) == len(set(ids))
    assert all(vr._ACCOLADE_ID.fullmatch(accolade_id) for accolade_id in ids)
    metrics = vr.accolade_metrics(vr._new_career("Tester"))
    for accolade in vr.ACCOLADES:
        assert accolade.group in vr.ACCOLADE_GROUPS
        assert accolade.metric in metrics, accolade.id
        assert accolade.target >= 1 and accolade.tier in (1, 2, 3)
    assert {accolade.group for accolade in vr.ACCOLADES} == set(vr.ACCOLADE_GROUPS)
    # Every counter is measured by something, and every rank above the first and
    # every career ending has an achievement of its own.
    assert set(vr.ACCOLADE_COUNTERS) <= {accolade.metric for accolade in vr.ACCOLADES}
    assert {f"rank_{index}" for index in range(1, len(vr.RANKS))} <= set(ids)
    assert {f"finale_{key}" for key in vr.CAREER_FINALES} <= set(ids)


def test_an_unknown_counter_is_refused():
    with pytest.raises(ValueError):
        vr.bump_accolade(vr._new_career("Tester"), "typo")


# --- Unlocking ----------------------------------------------------------------

def test_a_fresh_career_earns_nothing_and_writes_no_record():
    world = _world_with_seed(42)
    world.commit()
    assert world.save.accolades == {} and "accolades" not in world.save.to_dict()
    assert world.pending_accolades == []


def test_a_career_that_already_did_it_unlocks_at_its_first_commit():
    world = _world_with_seed(42)
    world.save.pilot.kills = 12
    world.save.turn = 15
    world.commit()
    assert {"kills_1", "kills_10", "hops_10"} <= unlocked(world.save)
    assert "kills_50" not in unlocked(world.save)
    assert world.save.accolades["unlocked"]["kills_10"] == 1  # the career that earned it
    assert {a.id for a in world.pending_accolades} >= {"kills_1", "kills_10", "hops_10"}
    # Earned once: the next commit announces nothing again.
    world.pending_accolades.clear()
    world.commit()
    assert world.pending_accolades == []


def test_an_accolade_is_announced_only_once_its_save_succeeded():
    world = _world_with_seed(42)
    world.save.pilot.kills = 1

    def fail(_world):
        raise OSError("disk full")

    world._checkpoint = fail
    with pytest.raises(vr.SaveError):
        world.commit()
    assert world.pending_accolades == []


def test_a_starting_shuttle_is_implied_and_other_hulls_are_remembered():
    world = _world_with_seed(42)
    world.commit()
    assert world.save.accolades == {}
    world.save.ship.hull_class = "Freighter"
    world.commit()
    assert world.save.accolades["hulls"] == ["Freighter"]
    assert vr.accolade_metrics(world.save)["hulls"] == 2


# --- The account, across retirement --------------------------------------------

def test_accolades_and_counters_survive_retirement_and_totals_span_careers():
    world = _finale_world("legend")
    world.save.turn = 600
    world.save.pilot.kills = 30
    vr.bump_accolade(world.save, "distress", 4)
    world.commit()
    before = unlocked(world.save)
    fresh = vr.World(vr.finish_career(world.save, "legend"))
    fresh.save.turn = 450
    fresh.save.pilot.kills = 25
    fresh.commit()
    assert before <= unlocked(fresh.save)
    assert fresh.save.accolades["counters"]["distress"] == 4
    metrics = vr.accolade_metrics(fresh.save)
    assert metrics["hops"] == 1050 and metrics["kills"] == 55
    assert {"hops_1000", "kills_50", "retire_1", "finale_legend"} <= unlocked(fresh.save)
    # Unlocked in the new career, and recorded as such.
    assert fresh.save.accolades["unlocked"]["finale_legend"] == 2
    # The retiring save and its successor share nothing.
    vr.bump_accolade(fresh.save, "distress")
    assert world.save.accolades["counters"]["distress"] == 4


def test_a_retirement_announces_what_the_new_career_earned(monkeypatch):
    world = _finale_world("legend")
    keys = iter(["S", "x"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_career_finale(vr.Palette(False), world)
    assert {"retire_1", "finale_legend"} <= {a.id for a in world.pending_accolades}


# --- The save -------------------------------------------------------------------

def _saved(save) -> dict:
    return json.loads(json.dumps(save.to_dict()))


def test_the_record_round_trips_and_keeps_ids_from_a_newer_build():
    world = _world_with_seed(42)
    world.save.pilot.kills = 1
    world.commit()
    document = _saved(world.save)
    document["accolades"]["unlocked"]["future_feat"] = 1
    document["accolades"]["counters"]["future_counter"] = 3
    loaded = vr.SaveData.from_dict(document)
    assert loaded.accolades == document["accolades"]
    assert _saved(loaded)["accolades"] == document["accolades"]


@pytest.mark.parametrize("mutate", [
    lambda a: a["counters"].__setitem__("distress", -1),
    lambda a: a["counters"].__setitem__("distress", True),
    lambda a: a["unlocked"].__setitem__("Bad-Id", 1),
    lambda a: a["unlocked"].__setitem__("kills_1", 2),   # a career that has not happened
    lambda a: a["unlocked"].__setitem__("kills_1", 0),
    lambda a: a.__setitem__("hulls", ["Freighter", "Freighter"]),
    lambda a: a.__setitem__("hulls", ["Dreadnought"]),
    lambda a: a.__setitem__("extra", 1),
    lambda a: a.__delitem__("counters"),
])
def test_a_malformed_record_is_a_broken_save(mutate):
    world = _world_with_seed(42)
    vr.bump_accolade(world.save, "distress")
    document = _saved(world.save)
    mutate(document["accolades"])
    with pytest.raises(vr.ResumeError):
        vr.SaveData.from_dict(document)


def test_a_newer_record_version_belongs_to_a_newer_build():
    world = _world_with_seed(42)
    vr.bump_accolade(world.save, "distress")
    document = _saved(world.save)
    document["accolades"]["version"] = 2
    with pytest.raises(vr.ResumeError):
        vr.SaveData.from_dict(document)


# --- What only a counter can know -------------------------------------------------

def _fight_won_in_one_shot(monkeypatch, world, pirate):
    world.save.pending_travel["encounter"]["combat"]["pirate"]["hp"] = 1
    monkeypatch.setattr(vr, "read_key", lambda: "F")
    with contextlib.redirect_stdout(io.StringIO()):
        return vr.screen_combat(vr.Palette(False), world, pirate)


def test_a_clean_kill_counts_once_even_when_the_fight_is_resumed(monkeypatch):
    world, pirate = _world_with_pending_fight()
    assert _fight_won_in_one_shot(monkeypatch, world, pirate) == "won"
    counters = world.save.accolades["counters"]
    assert counters["flawless"] == 1 and "close_calls" not in counters and "patrol_kills" not in counters
    assert "flawless" in unlocked(world.save)
    # The kept outcome is returned again; nothing is counted twice.
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.screen_combat(vr.Palette(False), world, pirate) == "won"
    assert world.save.accolades["counters"]["flawless"] == 1


def test_a_win_at_the_edge_of_destruction_is_a_close_call(monkeypatch):
    world, pirate = _world_with_pending_fight()
    world.save.ship.hull_hp = 1
    world.save.pending_travel["encounter"]["combat"]["hull_before"] = 1
    _fight_won_in_one_shot(monkeypatch, world, pirate)
    assert world.save.accolades["counters"]["close_calls"] == 1


def test_an_escape_and_a_bribe_are_counted(monkeypatch):
    world, pirate = _world_with_pending_fight()
    world.event_rng.random = lambda: 0.0
    monkeypatch.setattr(vr, "read_key", lambda: "E")
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.screen_combat(vr.Palette(False), world, pirate) == "escaped"
    assert world.save.accolades["counters"]["escapes"] == 1
    world, pirate = _world_with_pending_fight()
    world.save.pilot.credits = 100_000
    world.event_rng.random = lambda: 0.0
    monkeypatch.setattr(vr, "read_key", lambda: "P")
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.screen_combat(vr.Palette(False), world, pirate) == "escaped"
    assert world.save.accolades["counters"]["raider_bribes"] == 1
    assert "raider_bribe" in unlocked(world.save)


def test_answering_a_call_and_salvaging_a_wreck_are_counted(monkeypatch):
    world = _world_with_exploration_choice("distress")
    monkeypatch.setattr(vr, "read_key", lambda: "H")
    with contextlib.redirect_stdout(io.StringIO()):
        vr._encounter_distress_call(vr.Palette(False), world)
    assert world.save.accolades["counters"]["distress"] == 1 and "distress_1" in unlocked(world.save)
    world = _world_with_exploration_choice("derelict")
    world.event_rng.random = lambda: 0.0
    monkeypatch.setattr(vr, "read_key", lambda: "S")
    with contextlib.redirect_stdout(io.StringIO()):
        vr._encounter_derelict(vr.Palette(False), world)
    assert world.save.accolades["counters"]["derelicts"] == 1 and "derelicts_1" in unlocked(world.save)


def test_misfortune_is_counted_where_it_happens():
    world = _world_with_seed(42)
    vr.destroy_ship(world)
    world.save.current_system = world.here.connections[0]
    world.save.ship.fuel = 0
    world.save.pilot.credits = 0
    vr.rescue_stranded_pilot(world)
    world.commit()
    assert world.save.accolades["counters"]["ships_lost"] == 1
    assert world.save.accolades["counters"]["tows"] == 1
    assert {"lost_1", "towed"} <= unlocked(world.save)


@pytest.mark.parametrize("draw,counter", [(0.0, "customs_bribes"), (0.99, "busts")])
def test_customs_counts_a_bribe_taken_and_a_bribe_refused(draw, counter):
    world = _world_with_seed(42)
    _add_cargo(world, vr.CONTRABAND_COMMODITIES[0], 2)
    world.save.pilot.credits = 100_000
    world.event_rng.random = lambda: draw
    vr.resolve_customs(world, "P")
    assert world.save.accolades["counters"] == {counter: 1}


def test_a_delivery_counts_as_a_delivery():
    world = _world_with_seed(42)
    dest_id = world.here.id
    world.save.active_missions = [vr.Mission(7, "delivery", "Haul ore", 420, 0, dest_id, commodity="ore", quantity=2)]
    _add_cargo(world, "ore", 2)
    vr.check_mission_completions(world)
    assert world.save.accolades["counters"] == {"deliveries": 1}


# --- The board -------------------------------------------------------------------

def test_the_score_record_lists_what_was_unlocked_and_rarity_reads_it(tmp_path):
    veteran = _world_with_seed(42)
    veteran.save.pilot.kills = 12
    veteran._checkpoint = lambda w: vr.persist(w, tmp_path, 1)
    veteran.commit()
    newcomer = _world_with_seed(43)
    newcomer._checkpoint = lambda w: vr.persist(w, tmp_path, 2)
    newcomer.commit()
    record = json.loads((tmp_path / "scores" / "1.json").read_text(encoding="utf-8"))
    assert record["accolades"] == sorted(unlocked(veteran.save))
    # A record from before achievements existed is left out of rarity entirely.
    legacy = dict(record, user_id=3)
    del legacy["accolades"]
    (tmp_path / "scores" / "3.json").write_text(json.dumps(legacy), encoding="utf-8")
    holders, pilots = vr.accolade_rarity(tmp_path)
    assert pilots == 2 and holders["kills_10"] == 1


@pytest.mark.parametrize("bad", [["kills_1", "kills_1"], ["b", "a"], ["Bad"], "kills_1", [1]])
def test_a_malformed_score_list_is_ignored_not_trusted(bad):
    assert vr._score_accolades(bad) is None


def test_an_unchanged_score_record_is_not_rewritten(tmp_path):
    world = _world_with_seed(42)
    world.save.pilot.kills = 1
    world._checkpoint = lambda w: vr.persist(w, tmp_path, 1)
    world.commit()
    path = tmp_path / "scores" / "1.json"
    stamp = path.stat().st_mtime_ns
    vr.update_hall_of_fame(tmp_path, 1, world.save)
    assert path.stat().st_mtime_ns == stamp


# --- What the pilot sees -------------------------------------------------------------

def test_a_few_are_named_on_the_deck_and_many_are_counted():
    few = [vr.ACCOLADES_BY_ID[key] for key in ("kills_1", "hops_10")]
    assert vr.accolade_announcements(few) == [
        "Achievement unlocked: First Blood -- Win your first fight.",
        "Achievement unlocked: First Light -- Make 10 jumps."]
    many = list(vr.ACCOLADES[:5])
    assert vr.accolade_announcements(many) == [
        "5 achievements unlocked. [S] Status, [A] Achievements lists them."]


def test_the_deck_announces_and_plays_one_medal(monkeypatch):
    seen = []
    monkeypatch.setattr(vr, "play_scene", lambda make: seen.append(make(80, 24)) or True)
    world = _world_with_seed(42)
    world.save.pilot.kills = 1
    world.commit()
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_station_menu(vr.Palette(False), world)
    assert "Achievement unlocked: First Blood" in plain(buf.getvalue())
    assert [scene.kind for scene in seen] == ["medal"] and "FIRST BLOOD" in seen[0].title
    assert world.pending_accolades == []


def test_the_list_hides_what_is_hidden_and_shows_progress():
    world = _world_with_seed(42)
    world.save.pilot.kills = 12
    vr.destroy_ship(world)
    world.commit()
    before = copy.deepcopy(world.save.to_dict())
    text = plain("\n".join(vr.accolade_lines(world.save, ({"kills_10": 1}, 4))))
    assert "Scrap Metal" in text                          # hidden, but earned
    assert "Tow Truck Regular" not in text                 # hidden and not
    assert "hidden achievements still to find" in text
    assert "Warden of the Lanes Win 50 fights. 12/50." in text
    assert "Raider Bane Win 10 fights. Career #1. 25% of pilots." in text
    assert world.save.to_dict() == before                  # looking writes nothing


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_the_achievements_view_fits_the_terminal(monkeypatch, terminal, width, height):
    terminal(width, height)
    world = _world_with_seed(42)
    world.save.pilot.kills = 12
    world.commit()
    keys = iter(["A", ">", "B"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_status(vr.Palette(False), world)
    text = plain(buf.getvalue())
    # The first two pages: the count, then the Travel group's ladder.
    assert "Pilot Record: Achievements" in text and "TRAVEL" in text and "First Light" in text
    assert all(vr._visible_width(row) <= width for row in text.splitlines())


@pytest.mark.parametrize("width,height,label", [(80, 24, "[A] Awards"), (132, 50, "[A] Achievements")])
@pytest.mark.parametrize("view", ["O", "A"])
def test_the_pilot_record_action_bar_is_one_row(monkeypatch, terminal, width, height, label, view):
    """`[A] Achievements` wrapped the bar after `[R]` at 80 columns (issue #20)."""
    terminal(width, height)
    world = _world_with_seed(42)
    world.commit()
    keys = iter([view, "B"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_status(vr.Palette(False), world)
    bars = [row for row in plain(buf.getvalue()).splitlines() if "[B] Back" in row]
    assert bars and all("[O] Pilot" in row and label in row and "[R] Finale" in row for row in bars)
    # The bar ends at its colon; the key echoed after it sits in the last column.
    assert all(vr._visible_width(row[:row.index("Back:") + 5]) <= width - 1 for row in bars)


@pytest.mark.parametrize("tier", [1, 2, 3])
def test_progress_blends_toward_its_medals_color_as_it_closes_in(monkeypatch, tier):
    """The goal wears its medal's color; the count so far starts at plasma and
    takes a step toward that color every tenth of the way (play-test, v1.2.2)."""
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=True))
    goal = vr.ACCOLADE_TIER_RGB[tier]
    p = vr.pal()
    distances = []
    for current in range(0, 100, 10):
        drawn = vr.accolade_progress(current, 100, tier)
        assert drawn.endswith(f"{p.rgb(goal)}/100{vr.RESET}")
        color = tuple(int(part) for part in drawn[len("\x1b[38;2;"):drawn.index("m")].split(";"))
        distances.append(sum(abs(a - b) for a, b in zip(color, goal)))
    assert vr.accolade_progress(0, 100, tier).startswith(p.rgb(vr.ACCOLADE_PROGRESS_START))
    assert distances == sorted(distances, reverse=True) and len(set(distances)) == 10


@pytest.mark.parametrize("style", ["mono", "plain"])
def test_progress_has_no_color_where_the_preset_has_none(monkeypatch, style):
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
    # No color of its own; the resets every component writes are `out`'s to strip.
    assert vr.accolade_progress(4, 10, 3).replace(vr.RESET, "") == "4/10"


def test_numbers_in_a_description_are_values_and_credits_are_gold(monkeypatch):
    """`Complete 10 delivery contracts.` hid its 10 in the prose (play-test, v1.2.2).
    Bold, because ink beside prose is too close in brightness to stand out."""
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=True))
    p = vr.pal()
    drawn = vr.accolade_description("Complete 10 delivery contracts.")
    assert f"{p.text}Complete " in drawn and f"{p.ink}{vr.BOLD}10{vr.RESET}" in drawn
    assert f"{p.text} delivery contracts." in drawn
    assert f"{p.ink}{vr.BOLD}5%{vr.RESET}" in vr.accolade_description("Win a fight with under 5% of your hull left.")
    assert f"{p.gold}{vr.BOLD}10,000cr{vr.RESET}" in vr.accolade_description("Hold 10,000cr at once.")
    assert vr._ANSI_RE.sub("", vr.accolade_description("Make 1,000 jumps.")) == "Make 1,000 jumps."
