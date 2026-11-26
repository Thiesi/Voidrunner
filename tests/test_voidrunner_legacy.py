"""A retiring career passes one thing on (review).

Retiring began a career that owed the old one a few hundred credits and a
module tier. These pin the legacy a retiring pilot now chooses: what can be
passed on and when, what the next career begins with for each, that it is one
thing and never credits, what the dossier keeps, and how the finale page asks.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from .support import _finale_world, plain, vr


def _retiring(**crew):
    world = _finale_world("legend")
    for role in crew:
        setattr(world.save.ship, f"has_{role}", True)
        world.save.ship.crew_records[role] = {"version": 1, "identity": crew[role][0], "paid_jumps": crew[role][1]}
    return world


# --- what can be passed on --------------------------------------------------------

def test_a_stake_can_always_be_passed_on_and_the_rest_only_where_there_is_something_to_pass():
    bare = _retiring()
    assert vr.legacy_options(bare.save) == ["none", "stake"]
    crewed = _retiring(gunner=(1, 16), navigator=(0, 3))
    assert vr.legacy_options(crewed.save) == ["none", "stake", "veteran:gunner", "veteran:navigator"]
    known = _retiring()
    known.save.pilot.reputation[vr.FACTION_CONCORD] = 19
    assert "name:concord" not in vr.legacy_options(known.save)     # hardly known: nothing to remember
    known.save.pilot.reputation[vr.FACTION_CONCORD] = 20
    assert vr.legacy_options(known.save)[-1] == "name:concord"
    known.save.pilot.reputation[vr.FACTION_BLACKWAKE] = 60         # the faction that thinks best of the pilot, and only it
    assert [o for o in vr.legacy_options(known.save) if o.startswith("name:")] == ["name:blackwake"]


def test_each_legacy_says_what_it_is():
    world = _retiring(gunner=(1, 16))
    world.save.pilot.reputation[vr.FACTION_CONCORD] = 70
    save = world.save
    assert vr.legacy_text(save, "none") == "nothing"
    assert vr.legacy_text(save, "stake") == "a stake: step one at Freeport Anchorage"
    name = vr.CREW_CANDIDATES["gunner"][1][0]
    assert vr.legacy_text(save, "veteran:gunner") == f"a veteran: {name}, gunner, Veteran"
    assert vr.legacy_text(save, "name:concord") == "a name: +35 standing with the Concord Navy"


# --- what the next career begins with ---------------------------------------------

def test_a_stake_is_the_first_step_at_freeport():
    world = _retiring()
    fresh = vr.finish_career(world.save, "legend", "standard", "stake")
    assert vr.stakes(fresh) == {0: 1} and fresh.flags == {"stake_0_1": True}
    assert "Passed on from your last career: a stake at Freeport Anchorage." in fresh.pilot.log
    assert fresh.retired_careers[-1]["legacy"] == "stake"


def test_a_veteran_signs_on_again_as_who_they_were_and_leaves_their_errand_behind():
    world = _retiring(gunner=(1, 16), engineer=(2, 40))
    world.save.ship.crew_records["gunner"]["assignment"] = {"stage": "accepted"}
    fresh = vr.finish_career(world.save, "legend", "standard", "veteran:gunner")
    assert fresh.ship.has_gunner and not fresh.ship.has_engineer and not fresh.ship.has_navigator   # one, not the crew
    assert fresh.ship.crew_records == {"gunner": {"version": 1, "identity": 1, "paid_jumps": 16}}
    assert vr.crew_level(fresh.ship, "gunner") == vr.crew_level(world.save.ship, "gunner") == 2      # a Veteran still
    name = vr.CREW_CANDIDATES["gunner"][1][0]
    assert f"Passed on from your last career: {name} signs on again." in fresh.pilot.log
    assert vr.World(fresh) and vr.crew_name(vr.World(fresh), "gunner") == name                       # in a new galaxy, the same person
    vr.SaveData.from_dict(fresh.to_dict())


def test_a_name_is_half_the_standing_up_to_forty_and_opens_no_membership():
    for standing, carried in ((20, 10), (51, 25), (80, 40), (100, 40)):
        world = _retiring()
        world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = standing
        world.save.pilot.has_blackwake_made = True
        fresh = vr.finish_career(world.save, "legend", "standard", "name:blackwake")
        assert fresh.pilot.reputation == {vr.FACTION_CONCORD: 0, vr.FACTION_BLACKWAKE: carried}
        assert not fresh.pilot.has_blackwake_made and not fresh.pilot.has_concord_commission
        assert f"the Blackwake Cartel remembers your name, +{carried} standing." in " ".join(fresh.pilot.log)
    assert (vr.LEGACY_NAME_SHARE, vr.LEGACY_NAME_MOST, vr.LEGACY_NAME_LEAST) == (2, 40, 20)


def test_it_is_one_thing_never_credits_and_nothing_is_the_default():
    world = _retiring(gunner=(1, 16))
    world.save.pilot.reputation[vr.FACTION_CONCORD] = 80
    plain_fresh = vr.finish_career(world.save, "legend")
    assert "legacy" not in plain_fresh.retired_careers[-1] and plain_fresh.flags == {} and not plain_fresh.ship.has_gunner
    assert plain_fresh.pilot.reputation == {vr.FACTION_CONCORD: 0, vr.FACTION_BLACKWAKE: 0}
    for legacy in vr.legacy_options(world.save):
        fresh = vr.finish_career(world.save, "legend", "standard", legacy)
        assert fresh.pilot.credits == plain_fresh.pilot.credits and fresh.ship.hull_class == "Shuttle"
        given = (bool(fresh.flags), fresh.ship.has_gunner, fresh.pilot.reputation[vr.FACTION_CONCORD] > 0)
        assert sum(given) == (0 if legacy == "none" else 1)
    # What cannot be passed on is not: a specialist who is not aboard, a faction that hardly knows the pilot.
    for impossible in ("veteran:navigator", "name:blackwake", "everything"):
        fresh = vr.finish_career(world.save, "legend", "standard", impossible)
        assert "legacy" not in fresh.retired_careers[-1] and fresh.flags == {} and not fresh.ship.has_navigator


# --- what is kept -----------------------------------------------------------------

def test_the_dossier_keeps_what_was_passed_on_and_a_save_refuses_what_could_not_be():
    world = _retiring(gunner=(1, 16))
    fresh = vr.finish_career(world.save, "legend", "standard", "veteran:gunner")
    record = fresh.to_dict()
    assert record["retired_careers"][-1]["legacy"] == "veteran:gunner"
    assert vr.SaveData.from_dict(record).retired_careers[-1]["legacy"] == "veteran:gunner"
    assert any(line.endswith("Passed on: a veteran.") for line in vr.career_dossier_lines(fresh))
    for bad in ("none", "veteran:cook", "name:pirates", "credits", 7):
        record["retired_careers"][-1]["legacy"] = bad
        with pytest.raises((vr.ResumeError, vr.UnsupportedSave)):
            vr.SaveData.from_dict(record)
    older = vr.finish_career(_retiring().save, "legend")           # a dossier from before this has no such field
    assert not any("Passed on" in line for line in vr.career_dossier_lines(older))
    vr.SaveData.from_dict(older.to_dict())


def test_the_score_record_is_what_it_was():
    fresh = vr.finish_career(_retiring().save, "legend", "standard", "stake")
    summary = vr.score_achievements(fresh)
    assert all("legacy" not in career for career in summary["careers"])
    assert vr._score_achievements(summary, fresh.pilot.retirements) == summary


# --- the finale page --------------------------------------------------------------

def test_the_finale_page_asks_and_l_goes_round_what_can_be_passed_on(monkeypatch):
    world = _retiring(gunner=(1, 16))
    page = " ".join(" ".join(plain(line).split()) for line in vr.career_finale_lines(world.save, "legend"))
    assert "You pass on nothing. [L] change." in page
    page = " ".join(" ".join(plain(line).split()) for line in vr.career_finale_lines(world.save, "legend", "standard", "stake"))
    assert "You pass on a stake: step one at Freeport Anchorage. [L] change." in page
    seen = []
    real = vr.finish_career
    monkeypatch.setattr(vr, "finish_career", lambda save, finale, charter="standard", legacy="none":
                        seen.append(legacy) or real(save, finale, charter, legacy))
    monkeypatch.setattr(vr, "confirm", lambda prompt, p, **kw: False)
    for presses, expected in ((0, "none"), (1, "stake"), (2, "veteran:gunner"), (3, "none")):
        keys = iter(["L"] * presses + ["S", "B"])
        monkeypatch.setattr(vr, "read_key", lambda: next(keys))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            vr.screen_career_finale(vr.Palette(False), world)
        assert seen[-1] == expected and "[L] Legacy" in plain(output.getvalue())
    assert world.save.pilot.retirements == 0   # every retirement above was declined: nothing changed


@pytest.mark.parametrize("legacy", ["none", "stake", "veteran:gunner", "name:blackwake"])
def test_the_finale_stays_two_pages_at_80x24_whatever_is_passed_on(terminal, legacy):
    """It was two pages before there was a legacy to choose; each choice is one row."""
    terminal(80, 24)
    world = _retiring(gunner=(1, 16))
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = 100
    lines = vr.career_finale_lines(world.save, "legend", "standard", legacy)
    row = next(line for line in lines if plain(line).startswith("You pass on "))
    assert vr._visible_width(row) <= vr._page_content_width()
    pages = vr._service_pages(lines, "Career Finale", "[1-4] Choose [S] Retire [L] Legacy [H] Charter [<>] Page [B] Back: ")
    assert len(pages) == 2
