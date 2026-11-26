"""Epilogues for the faction cases, and the last site (review).

A case ended on its closing line and a second site's record led nowhere.
These pin the word that now comes of a closed case, when and how often, and
the third site: where it is, that only the second site's record leads to it,
what its record says, what a save keeps, and that it has every picture a site
is drawn in.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from .support import _world_with_seed, plain, vr


def _closed(faction, choice, day=3, turn=3):
    world = _world_with_seed(42)
    world.save.turn = turn
    world.save.faction_stories[faction] = {"version": 1, "stage": "complete", "choice": choice, "day": day}
    return world


# --- epilogues ----------------------------------------------------------------------

def test_every_ending_of_every_case_has_word_of_what_came_of_it():
    assert vr.EPILOGUE_DAYS == 12 and set(vr.FACTION_EPILOGUES) == set(vr.FACTIONS)
    lines = []
    for faction in vr.FACTIONS:
        told = vr.FACTION_EPILOGUES[faction]
        assert set(told) == {"who", "hardline", "aid"} and told["who"] in vr.FACTION_STORIES[faction]["intro"]
        for choice in ("hardline", "aid"):
            line = vr.faction_epilogue(faction, choice)
            assert line == f"Word from {told['who']}: {told[choice]}" and line.endswith(".") and line.isascii()
            assert 150 < len(line) < 260, line
            assert told[choice] != vr.FACTION_STORIES[faction][choice]["closing"]
            lines.append(line)
    assert len(set(lines)) == 4


@pytest.mark.parametrize("faction", vr.FACTIONS)
@pytest.mark.parametrize("choice", ["hardline", "aid"])
def test_word_comes_twelve_days_after_a_case_closes_and_comes_once(faction, choice):
    world = _closed(faction, choice, day=3, turn=14)
    assert vr.faction_epilogues_due(world) == []                       # eleven days: not yet
    assert "epilogue" not in world.save.faction_stories[faction]
    world.save.turn = 15
    assert vr.faction_epilogues_due(world) == [vr.faction_epilogue(faction, choice)]
    assert world.save.faction_stories[faction]["epilogue"] is True
    assert vr.faction_epilogues_due(world) == []                       # once
    world.save.turn = 80
    assert vr.faction_epilogues_due(world) == []
    page = [plain(line) for line in vr.faction_story_lines(world, faction)]
    assert vr.faction_epilogue(faction, choice) in page and any("SINCE THEN" in line for line in page)
    vr.SaveData.from_dict(world.save.to_dict())


def test_a_case_that_is_open_or_closed_before_this_was_built_is_handled():
    world = _world_with_seed(42)
    world.save.turn = 50
    world.save.faction_stories[vr.FACTION_CONCORD] = {"version": 1, "stage": "committed", "choice": "aid"}
    assert vr.faction_epilogues_due(world) == []                       # not closed: nothing has come of it yet
    # A case closed by an older build has no day: word of it is simply due.
    world.save.faction_stories[vr.FACTION_CONCORD] = {"version": 1, "stage": "complete", "choice": "aid"}
    vr.SaveData.from_dict(world.save.to_dict())
    assert vr.faction_epilogues_due(world) == [vr.faction_epilogue(vr.FACTION_CONCORD, "aid")]
    page = [plain(line) for line in vr.faction_story_lines(_closed(vr.FACTION_CONCORD, "aid"), vr.FACTION_CONCORD)]
    assert not any("SINCE THEN" in line for line in page)              # before word comes, the page is as it was


def test_closing_a_case_notes_the_day_and_a_save_refuses_a_day_on_an_open_one():
    world = _closed(vr.FACTION_BLACKWAKE, "aid", day=3, turn=20)
    record = world.save.to_dict()
    vr.SaveData.from_dict(record)
    for bad in ({"day": 99}, {"day": -1}, {"epilogue": False}, {"epilogue": 1}):
        broken = {**record, "faction_stories": {vr.FACTION_BLACKWAKE: {**record["faction_stories"][vr.FACTION_BLACKWAKE], **bad}}}
        with pytest.raises((vr.ResumeError, vr.UnsupportedSave)):
            vr.SaveData.from_dict(broken)
    opened = {**record, "faction_stories": {vr.FACTION_BLACKWAKE: {"version": 1, "stage": "accepted", "day": 3}}}
    with pytest.raises((vr.ResumeError, vr.UnsupportedSave)):
        vr.SaveData.from_dict(opened)


def test_word_reaches_the_pilot_at_the_next_dock_and_the_dock_itself_stays_quiet(monkeypatch):
    world = _closed(vr.FACTION_CONCORD, "hardline", day=0, turn=30)
    target = sorted(world.here.connections)[0]
    world.save.ship.fuel = vr.fuel_capacity(world.save.ship)
    world.event_rng.random = lambda: 1.0
    monkeypatch.setattr(vr, "play_scene", lambda make: True)
    monkeypatch.setattr(vr, "crew_voice", lambda *a, **k: None)
    monkeypatch.setattr(vr, "dock_rumour", lambda *a, **k: None)
    monkeypatch.setattr(vr, "sector_tale", lambda w: [])
    monkeypatch.setattr(vr, "dock_voice", lambda w: "The dock says its piece.")
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_travel(vr.Palette(False), world, target)
    report = " ".join(plain(output.getvalue()).split()) + " " + " ".join(world.hop_report)
    assert "Word from Edda Ro: The official took a plea" in report and "The dock says its piece." not in report
    assert world.save.faction_stories[vr.FACTION_CONCORD]["epilogue"] is True


# --- the last site ---------------------------------------------------------------------

def test_the_last_site_is_a_place_of_its_own_in_the_most_dangerous_system_left():
    assert vr.LAST_SITE["label"] == "the Last Beacon" and vr.LAST_SITE["reward_credits"] == 15_000
    assert vr.LAST_SITE["label"] not in {flavor["label"] for flavor in vr.LANDMARK_FLAVORS}
    for seed in range(40, 80):
        world = _world_with_seed(seed)
        first, far, last = world.landmark, world.far_site, world.last_site
        assert last["label"] == "the Last Beacon" and len({0, first["system_id"], far["system_id"], last["system_id"]}) == 4
        taken = {0, first["system_id"], far["system_id"]}
        sectors = {vr.sector_for(world.by_id[first["system_id"]]), vr.sector_for(world.by_id[far["system_id"]])}
        open_systems = [s for s in world.galaxy if s.id not in taken]
        candidates = [s for s in open_systems if vr.sector_for(s) not in sectors] or open_systems
        assert world.by_id[last["system_id"]].danger == max(s.danger for s in candidates)
        assert vr.generate_last_site(seed, world.galaxy, first, far) == last     # the same every time for a seed


def test_only_the_second_sites_record_leads_to_it():
    world = _world_with_seed(42)
    last = world.last_site
    world.by_id[last["system_id"]].discovered = True
    world.save.current_system = last["system_id"]
    assert vr.landmark_here(world) is None and vr.next_site(world) is None       # nothing read yet: nothing here
    world.save.flags["landmark_investigated"] = True
    assert vr.next_site(world) is world.far_site and vr.landmark_here(world) is None
    assert "a second bearing" in vr.far_site_lead(world)
    world.save.flags["landmark2_investigated"] = True
    assert vr.next_site(world) is last and vr.landmark_here(world) is last
    assert vr.far_site_bearing(world) == last["system_id"] and "one more bearing, the last" in vr.far_site_lead(world)


def test_investigating_it_pays_reads_its_whole_record_and_ends_the_trail():
    world = _world_with_seed(42)
    last = world.last_site
    world.save.flags.update(landmark_investigated=True, landmark2_investigated=True)
    world.by_id[last["system_id"]].discovered = True
    world.save.current_system = last["system_id"]
    before = world.save.pilot.credits
    lines = vr.investigate_landmark(world)
    assert world.save.pilot.credits == before + 15_000 and world.save.flags["landmark3_investigated"] is True
    assert lines == ["Salvage recovered: +15000cr.", *vr.ARCHIVE_RECORDS["the Last Beacon"]]
    assert "light kept, all well" in lines[1] and "pen on a cord" in lines[2]
    assert vr.next_site(world) is None and vr.far_site_bearing(world) is None and vr.far_site_lead(world) is None
    with pytest.raises(ValueError):
        vr.investigate_landmark(world)
    assert vr.discovery_site(world) == (last, True)
    assert vr.accolade_metrics(world.save)["last_site"] == 1
    assert "last_site" in {accolade.id for accolade in vr.unlock_accolades(world.save)}
    vr.SaveData.from_dict(world.save.to_dict())


def test_the_route_planner_takes_its_bearing_uncharted():
    world = _world_with_seed(42)
    world.save.flags.update(landmark_investigated=True, landmark2_investigated=True)
    last = world.by_id[world.last_site["system_id"]]
    last.discovered = False
    assert (last.id, f"Site bearing ({last.x},{last.y})") in vr.route_choices(world)


def test_it_has_every_picture_a_site_is_drawn_in(terminal):
    terminal(80, 24)
    art = vr.PORTRAITS["site"]["the Last Beacon"]
    assert set(art) == {"large", "compact"} and len(art["compact"]) == 3 and 5 <= len(art["large"]) <= 7
    assert all(row.isascii() and len(row) <= 38 for row in art["large"]) and all(len(row) <= 18 for row in art["compact"])
    assert set(vr.PORTRAITS["site"]) == {flavor["label"] for flavor in vr.LANDMARK_FLAVORS} | {"the Last Beacon"}
    assert set(vr.ARCHIVE_RECORDS) == set(vr.PORTRAITS["site"])
    # The Viewport paints it: a scene with something in it, and a lamp that turns.
    world = _world_with_seed(42)
    world.save.flags.update(landmark_investigated=True, landmark2_investigated=True, landmark3_investigated=True)
    _large, _compact, details, title = vr.viewport_content(world, "3")
    assert title == "Discovery" and "the Last Beacon" in details and "Salvage already claimed." in details
    blank = vr._ViewportSite(80, 24, site=None, items=[], prose="", seed="t")
    beacon = vr._ViewportSite(80, 24, site="the Last Beacon", items=[], prose="", seed="t")
    stills = {repr(beacon.frame(beacon.still + offset)) for offset in range(0, 12, 3)}
    assert len(stills) >= 3                                        # the lamp turns: the picture is not one frame
    assert beacon.frame(beacon.still) != blank.frame(blank.still)  # and it is not the empty scanner sweep
