"""The hard charter (review).

A career can be launched on a hard charter: raiders a tier stronger, no dock
work, a doubled tug fee, a Hall of Fame table and three achievements of its
own. These pin the rules, that it is chosen when a career is launched and at
no other time, what a save and a score record keep of it, and that a career
on the standard charter is exactly what it was.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys

import pytest

from .support import _VOIDRUNNER_PATH, _finale_world, _world_with_seed, plain, plain_bytes, vr


def _hard(seed=42):
    world = _world_with_seed(seed)
    world.save.charter = "hard"
    return world


# --- the rules ------------------------------------------------------------------

def test_the_charters_and_what_the_hard_one_says_of_itself():
    assert vr.CHARTERS == ("standard", "hard") and (vr.HARD_RAIDER_TIERS, vr.HARD_TUG_FACTOR) == (1, 2)
    assert not vr.is_hard(_world_with_seed(42).save) and vr.is_hard(_hard().save)
    terms = " ".join(vr.HARD_TERMS)
    for said in ("a tier stronger", "no shifts", "charges double", "Hall of Fame", "cannot be changed"):
        assert said in terms


def test_a_raider_is_a_tier_stronger_and_the_luck_is_the_same():
    for danger in range(5):  # danger 5 also sends elites, whose tier is their own; see the elite tests
        for seed in range(40):
            standard, hard = _world_with_seed(42), _hard()
            standard.event_rng.seed(seed)
            hard.event_rng.seed(seed)
            usual = vr.generate_pirate(standard, danger=danger)
            harder = vr.generate_pirate(hard, danger=danger)
            assert harder.tier == min(4, usual.tier + 1) and harder.name == usual.name
            assert harder.hp == harder.hp_max == 20 + harder.tier * 15
            assert standard.event_rng.random() == hard.event_rng.random()  # the same draws were taken
    # A raider whose tier is given -- a bounty's, a rival's -- is the tier it was given.
    assert vr.generate_pirate(_hard(), 2).tier == 2


@pytest.mark.usefixtures("plain_sectors")
def test_the_chart_and_the_hulk_speak_of_the_raiders_this_pilot_will_meet():
    standard, hard = _world_with_seed(42), _hard()
    assert [vr.raider_tier_span(standard, danger) for danger in range(6)] == [(0, 1), (0, 2), (1, 3), (2, 4), (3, 4), (4, 5)]
    assert [vr.raider_tier_span(hard, danger) for danger in range(6)] == [(1, 2), (1, 3), (2, 4), (3, 4), (4, 4), (4, 5)]
    # A Shuttle is warned off a danger-1 lane on the hard charter where the standard one says nothing.
    for world in (standard, hard):
        target = sorted(world.here.connections)[0]
        world.by_id[target].discovered, world.by_id[target].danger = True, 1
    target = sorted(standard.here.connections)[0]
    worst = vr.raider_tier_span(hard, 1)[1]
    warned = "Raiders there can outclass this hull." in vr.departure_terms(hard, target)
    assert warned == vr.raider_outclasses(hard.save.ship, worst)
    assert ("Raiders there can outclass this hull." in vr.departure_terms(standard, target)) == vr.raider_outclasses(
        standard.save.ship, 2)


def test_the_foreman_has_no_shifts_and_a_stranded_pilot_is_still_rescued():
    standard, hard = _world_with_seed(42), _hard()
    for world in (standard, hard):
        world.save.pilot.credits = 0
    assert vr.dock_work_open(standard) and not vr.dock_work_open(hard)
    assert not any("Dock work" in plain(row) for row in vr.deck_alert_rows(hard))
    hard.save.ship.fuel = 0
    assert vr.is_stranded(hard)
    assert "no charge" in vr.rescue_stranded_pilot(hard) and not vr.is_stranded(hard)


def test_the_tug_charges_double_and_says_so_before_it_is_owed():
    standard, hard = _world_with_seed(42), _hard()
    usual = vr.salvage_fee(standard.save.ship)
    assert vr.tug_fee(standard) == usual and vr.tug_fee(hard) == 2 * usual
    hard.save.ship.hull_hp = 5
    assert f"destruction costs {2 * usual}cr salvage" in " ".join(plain(row) for row in vr.deck_alert_rows(hard))
    for world in (standard, hard):
        world.save.pilot.credits = 10_000
        message = vr.destroy_ship(world)
        assert f"a {vr.tug_fee(world)}cr salvage fee charged" in message
        assert world.save.pilot.credits == 10_000 - vr.tug_fee(world)
        assert world.save.ship.hull_hp == vr.hull_hp_max(world.save.ship)  # paid in full, patched in full
    # A pilot who cannot pay the doubled fee gets the hull the doubled fee's share buys: less than the same credits bought.
    ship = vr.starting_ship()
    assert vr.salvage_hull(ship, usual, None, 2 * usual) < vr.salvage_hull(ship, usual) == vr.hull_hp_max(ship)
    broke = _hard()
    broke.save.pilot.credits = 0
    assert "Freeport's docks have work" not in vr.destroy_ship(broke)


def test_a_standard_career_is_exactly_what_it_was():
    world = _world_with_seed(42)
    assert world.save.charter == "standard" and "charter" not in world.save.to_dict()
    assert vr.tug_fee(world) == vr.salvage_fee(world.save.ship)
    world.save.pilot.credits = 0
    assert vr.dock_work_open(world)
    assert "HARD CHARTER" not in " ".join(plain(line) for line in vr.pilot_record_lines(world))
    assert not any("charter" in key for key in vr.score_achievements(world.save)["careers"][-1])


# --- choosing it ------------------------------------------------------------------

def _launch(monkeypatch, keys):
    stream = iter(keys)
    monkeypatch.setattr(vr, "read_key", lambda: next(stream))
    shown = []
    with contextlib.redirect_stdout(io.StringIO()) as output:
        choice = vr.launch_choice(vr.Palette(False), "Nova", lambda: shown.append("terms"))
    return choice, shown, " ".join(plain(output.getvalue()).split())


def test_the_launch_question_takes_h_reads_the_terms_out_and_asks_again(monkeypatch):
    choice, shown, text = _launch(monkeypatch, ["Y"])
    assert choice == "standard" and shown == []
    assert "Launch Nova's career, or [H] for the hard charter? [Y/N, Esc=No]" in text
    choice, shown, text = _launch(monkeypatch, ["h", "Y"])
    assert choice == "hard" and shown == ["terms"] and "Launch Nova's career on the hard charter? [Y/N, Esc=No]" in text
    # Declining the hard charter is not declining the career: the first question is asked again.
    choice, shown, text = _launch(monkeypatch, ["H", "N", "Y"])
    assert choice == "standard" and shown == ["terms"] and text.count("or [H] for the hard charter?") == 2
    assert _launch(monkeypatch, ["H", vr.ESCAPE_KEY, "N"])[0] is None
    assert _launch(monkeypatch, [vr.ESCAPE_KEY])[0] is None


def test_an_ordinary_question_does_not_take_the_third_key(monkeypatch):
    keys = iter(["H", "x", "Y"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.confirm("Depart?", vr.Palette(False)) is True  # H and x are not answers here


def test_the_commission_leaves_the_charter_with_the_door(monkeypatch, terminal):
    terminal(80, 24)
    for keys, charter in ((["\r", "Y"], "standard"), (["\r", "H", "Y"], "hard")):
        stream = iter(keys)
        monkeypatch.setattr(vr, "read_key", lambda: next(stream))
        info = {"handle": "Nib"}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            assert vr.create_career(vr.Palette(False), info) == "Nib"
        assert info["charter"] == charter
        text = " ".join(plain(output.getvalue()).split())
        assert ("The hard charter:" in text and all(term in text for term in vr.HARD_TERMS)) == (charter == "hard")
    stream = iter(["\r", "N"])
    monkeypatch.setattr(vr, "read_key", lambda: next(stream))
    info = {"handle": "Nib"}
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.create_career(vr.Palette(False), info) is None and "charter" not in info


def test_the_finale_page_chooses_the_next_careers_charter(monkeypatch):
    world = _finale_world("legend")
    page = " ".join(plain(line) for line in vr.career_finale_lines(world.save, "legend"))
    assert "Next career's charter: standard. [H] changes it." in page and "Hard:" not in page
    page = " ".join(plain(line) for line in vr.career_finale_lines(world.save, "legend", "hard"))
    assert "Next career's charter: hard. [H] changes it. Hard: raiders a tier stronger" in page
    # The toggle is a key on the page; the next career is launched on what it shows.
    asked = []
    monkeypatch.setattr(vr, "confirm", lambda prompt, p, **kw: asked.append(prompt) or False)
    keys = iter(["H", "S", "B"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    seen = []
    real = vr.finish_career
    monkeypatch.setattr(vr, "finish_career", lambda save, finale, charter="standard": seen.append(charter) or real(save, finale, charter))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_career_finale(vr.Palette(False), world)
    assert seen == ["hard"] and len(asked) == 1 and "[H] Charter" in plain(output.getvalue())
    assert world.save.charter == "standard" and world.save.pilot.retirements == 0  # declined: nothing changed


def test_retiring_archives_the_charter_flown_and_launches_the_one_chosen():
    world = _finale_world("legend")
    world.save.charter = "hard"
    fresh = vr.finish_career(world.save, "legend")            # the next one standard
    assert fresh.charter == "standard" and fresh.retired_careers[-1]["charter"] == "hard"
    again = vr.finish_career(world.save, "legend", "hard")    # the next one hard too
    assert again.charter == "hard"
    standard = _finale_world("legend")
    onward = vr.finish_career(standard.save, "legend", "hard")
    assert onward.charter == "hard" and "charter" not in onward.retired_careers[-1]
    assert vr.finish_career(standard.save, "legend", "nonsense").charter == "standard"
    # A hard pilot retiring is offered a hard charter again unless they say otherwise.
    assert vr.retire_pilot(world.save).charter == "standard"  # the bare constructor promises nothing


# --- what is kept -----------------------------------------------------------------

def test_a_save_keeps_the_charter_and_refuses_one_it_does_not_know():
    world = _hard()
    record = world.save.to_dict()
    assert record["charter"] == "hard" and vr.SaveData.from_dict(record).charter == "hard"
    plain_record = _world_with_seed(42).save.to_dict()
    assert "charter" not in plain_record and vr.SaveData.from_dict(plain_record).charter == "standard"
    for bad in ("iron", "", 3, None):
        with pytest.raises((vr.ResumeError, vr.UnsupportedSave)):
            vr.SaveData.from_dict({**record, "charter": bad})
    # The dossier of a hard career rides through a save; one that claims a charter it cannot have is refused.
    retired = vr.finish_career(_finale_hard().save, "legend", "hard").to_dict()
    assert retired["retired_careers"][-1]["charter"] == "hard"
    assert vr.SaveData.from_dict(retired).retired_careers[-1]["charter"] == "hard"
    retired["retired_careers"][-1]["charter"] = "standard"
    with pytest.raises((vr.ResumeError, vr.UnsupportedSave)):
        vr.SaveData.from_dict(retired)


def _finale_hard():
    world = _finale_world("legend")
    world.save.charter = "hard"
    return world


def test_the_score_record_carries_it_and_older_records_still_read():
    hard = vr.finish_career(_finale_hard().save, "legend", "hard")
    summary = vr.score_achievements(hard)
    assert [career.get("charter") for career in summary["careers"]] == ["hard", "hard"]
    assert vr._score_achievements(summary, hard.pilot.retirements) == summary
    assert not vr._future_score_achievements({"achievements": summary})
    plain_summary = vr.score_achievements(_world_with_seed(42).save)
    assert vr._score_achievements(plain_summary, 0) == plain_summary  # a record without the key is a standard career
    wrong = json.loads(json.dumps(summary))
    wrong["careers"][0]["charter"] = "standard"
    assert vr._score_achievements(wrong, hard.pilot.retirements) is None
    newer = json.loads(json.dumps(summary))
    newer["careers"][0]["gravity"] = 2
    assert vr._future_score_achievements({"achievements": newer})  # a field this build does not know is still a newer build's


def test_the_hall_has_a_table_for_hard_careers_alone():
    assert list(vr.SCORE_CATEGORIES)[-1] == "hard" and vr.SCORE_CATEGORIES["hard"] == "Hard charter"

    def entry(user_id, handle, credits, charter):
        career = {"number": 1, "seed": 7, "started": "2026-11-01", "ended": None, "finale": None, "days": 30,
                  "credits": credits, "market_margin": 10, "kills": 2, "missions": 1, "charted": 9}
        if charter:
            career["charter"] = "hard"
        return {"user_id": user_id, "handle": handle, "best_credits": credits, "retirements": 0,
                "achievements": {"version": 1, "careers": [career]}}

    entries = [entry(1, "Easy", 900_000, False), entry(2, "Iron", 40_000, True), entry(3, "Steel", 75_000, True),
               entry(4, "Broke", 0, True)]
    ranked = vr.achievement_ranking(entries, "hard")
    assert [record["handle"] for record in ranked] == ["Steel", "Iron"]  # by credits; the standard career is not here
    lines = [plain(line) for line in vr.achievement_lines(entries, "hard", 2)]
    assert lines[0] == "Top 2 local careers flown on a hard charter, by credits. [YOU] marks your pilot."
    assert lines[1].startswith("#1 Steel, career 1: 75,000cr. Current career;")
    assert lines[2].startswith("#2 [YOU] Iron, career 1: 40,000cr.")
    assert "raiders a tier stronger, no dock work, a doubled tug fee" in lines[-1] and "[H]" in lines[-1]
    # The other tables rank every career, hard or not.
    assert [record["handle"] for record in vr.achievement_ranking(entries, "combat")][:1] != []
    assert len(vr.achievement_ranking(entries, "combat")) == 4
    assert "No qualifying achievements recorded yet." in " ".join(plain(line) for line in vr.achievement_lines([], "hard", 1))


def test_its_achievements_are_earned_on_a_hard_charter_and_nowhere_else():
    ids = {accolade.id: accolade for accolade in vr.ACCOLADES}
    assert {"hard_rank_2", "hard_rank_4", "hard_retired"} <= set(ids)
    assert [ids[key].tier for key in ("hard_rank_2", "hard_rank_4", "hard_retired")] == [1, 2, 3]
    standard = _world_with_seed(42)
    standard.save.pilot.credits = 400_000
    vr.check_rank_up(standard)
    assert vr.accolade_metrics(standard.save)["hard_rank"] == 0
    assert not {"hard_rank_2", "hard_rank_4"} & {a.id for a in vr.unlock_accolades(standard.save)}
    hard = _hard()
    hard.save.pilot.credits = 20_000
    vr.check_rank_up(hard)
    assert vr.accolade_metrics(hard.save)["hard_rank"] == 2
    assert "hard_rank_2" in {a.id for a in vr.unlock_accolades(hard.save)}
    hard.save.pilot.credits = 400_000
    vr.check_rank_up(hard)
    assert "hard_rank_4" in {a.id for a in vr.unlock_accolades(hard.save)}
    retired = vr.finish_career(hard.save, "legend")
    assert vr.accolade_metrics(retired)["hard_retired"] == 1 and vr.accolade_metrics(retired)["hard_rank"] == 4
    assert "hard_retired" in {a.id for a in vr.unlock_accolades(retired)}


def test_a_dossier_says_a_career_was_flown_hard():
    retired = vr.finish_career(_finale_hard().save, "legend")
    assert any(line.endswith("; hard charter.") for line in vr.career_dossier_lines(retired))
    standard = vr.finish_career(_finale_world("legend").save, "legend")
    assert not any("hard charter" in line for line in vr.career_dossier_lines(standard))


def test_the_pilot_record_says_which_charter_this_is():
    lines = [plain(line) for line in vr.pilot_record_lines(_hard())]
    assert "HARD CHARTER" in lines[1]
    assert lines[2] == "charter raiders a tier stronger, no dock work, a doubled tug fee"


# --- the real door ------------------------------------------------------------------

def test_the_real_door_launches_a_hard_career_and_a_standard_one(tmp_path):
    for keys, charter in ((b"\rHYQ", "hard"), (b"\rYQ", None)):
        home = tmp_path / (charter or "standard")
        home.mkdir()
        info = home / "door_info.json"
        info.write_text(json.dumps({"user_id": 77, "handle": "Tester", "terminal_width": 80, "terminal_height": 24}),
                        encoding="utf-8")
        env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(home), NETBBS_DOOR_INFO=str(info))
        result = subprocess.run([sys.executable, str(_VOIDRUNNER_PATH)], input=keys, capture_output=True, env=env, timeout=60)
        shown = b" ".join(plain_bytes(result.stdout).split()).decode("utf-8", "replace")
        assert result.returncode == 0 and not result.stderr
        assert "or [H] for the hard charter? [Y/N, Esc=No]" in shown and "Command Deck" in shown
        assert ("on the hard charter? [Y/N, Esc=No]" in shown) == (charter == "hard")
        saved = json.loads((home / "77.json").read_text(encoding="utf-8"))
        assert saved.get("charter") == charter
        score = json.loads((home / "scores" / "77.json").read_text(encoding="utf-8"))
        assert score["achievements"]["careers"][-1].get("charter") == charter
