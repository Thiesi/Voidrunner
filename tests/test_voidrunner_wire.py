"""The wire: what the pilots of one board have lately done (review).

Each pilot's last few events live in that pilot's own score record, written
only by their own session, and a reader merges every record it can read. These
pin what is news and what is not, that the record is validated like the rest of
the score data, that another pilot's text is sanitized before it is shown, and
that the deck gives it one row only when today's order is not asking for it.
"""

from __future__ import annotations

import datetime
import json
import time

import pytest

from .support import _finale_world, _world_with_seed, plain, vr

DAY = datetime.date(2026, 11, 22).toordinal()


def _live(world, user_id: int = 77):
    world.wire_live, world.wire_user = True, user_id
    return world


def _texts(world) -> list[str]:
    return [event["text"] for event in world.wire_events]


# --- what is news ---------------------------------------------------------------

def test_a_world_nobody_made_live_notes_nothing():
    world = _world_with_seed(42)
    world.save.pilot.credits = 30_000
    world.commit()
    world.wire("rank", "reached the top")
    assert world.wire_events == [] and world.board_wire() == [] and vr.wire_deck_row(world) is None


def test_a_promotion_and_a_silver_achievement_are_news_and_a_bronze_one_is_not():
    world = _live(_world_with_seed(42))
    world.save.pilot.credits = 5_000   # Independent Trader
    world.save.pilot.kills = 10        # First Blood is bronze, Raider Bane silver
    world.commit()
    texts = _texts(world)
    assert "reached Independent Trader" in texts and "earned Raider Bane" in texts
    assert "earned First Blood" not in texts
    # The rank's own achievement is the promotion, said once.
    assert sum("Independent Trader" in text for text in texts) == 1
    assert all(event["kind"] in vr.WIRE_KINDS and len(event["text"]) <= vr.WIRE_TEXT_MAX for event in world.wire_events)


def test_a_dispatch_is_news_only_at_the_runs_worth_saying():
    world = _live(_world_with_seed(42))
    today = [DAY]
    world.dispatch_clock = lambda: today[0]
    said = []
    for run in range(1, 15):
        vr.fly_dispatch(world, vr.dispatch_order(world))
        if any("daily dispatch" in text for text in _texts(world)):
            said.append(run)
        world.wire_events.clear()
        today[0] += 1
    assert said == [3, 7, 14]


def test_a_lost_ship_is_news():
    world = _live(_world_with_seed(42))
    world.save.ship.hull_class = "Freighter"
    vr.destroy_ship(world)
    assert _texts(world) == ["lost a Freighter to raiders"]
    vr.destroy_ship(world, patrol=True)
    assert _texts(world)[-1] == "lost a Freighter to a Concord patrol"


# --- the record -----------------------------------------------------------------

def test_events_go_into_the_pilots_own_record_and_the_newest_six_are_kept(tmp_path):
    world = _live(_world_with_seed(42))
    assert vr.update_hall_of_fame(tmp_path, 77, world.save) is True
    path = tmp_path / "scores" / "77.json"
    assert "wire" not in json.loads(path.read_text(encoding="utf-8"))
    for number in range(4):
        world.wire("award", f"earned thing {number}")
    assert vr.update_hall_of_fame(tmp_path, 77, world.save, world.wire_events) is True
    world.wire_events.clear()
    for number in range(4, 9):
        world.wire("award", f"earned thing {number}")
    vr.update_hall_of_fame(tmp_path, 77, world.save, world.wire_events)
    stored = json.loads(path.read_text(encoding="utf-8"))["wire"]
    assert [event["text"] for event in stored] == [f"earned thing {number}" for number in range(3, 9)]
    assert len(stored) == vr.WIRE_KEEP
    assert [item["text"] for item in vr.board_wire(vr._load_score_records(tmp_path))][0] in {event["text"] for event in stored}


def test_a_save_writes_the_events_once_and_a_failed_write_keeps_them(tmp_path, monkeypatch):
    world = _live(_world_with_seed(42))
    world._checkpoint = lambda current: vr.persist(current, tmp_path, 77)
    world.checkpoint()
    world.wire("award", "earned something")
    real = vr._write_json_atomic

    def fail_scores(path, data):
        if path.parent.name == "scores":
            raise OSError("disk full")
        return real(path, data)

    monkeypatch.setattr(vr, "_write_json_atomic", fail_scores)
    world.commit()  # the career saves; its optional score record does not
    assert _texts(world) == ["earned something"]
    monkeypatch.setattr(vr, "_write_json_atomic", real)
    world.commit()
    assert world.wire_events == []
    stored = json.loads((tmp_path / "scores" / "77.json").read_text(encoding="utf-8"))["wire"]
    assert [event["text"] for event in stored] == ["earned something"]
    world.commit()
    assert json.loads((tmp_path / "scores" / "77.json").read_text(encoding="utf-8"))["wire"] == stored


@pytest.mark.parametrize("broken", [
    "news", [], [{"at": 1, "kind": "rank"}], [{"at": 1, "kind": "gossip", "text": "x"}],
    [{"at": "1", "kind": "rank", "text": "x"}], [{"at": 1, "kind": "rank", "text": ""}],
    [{"at": 1, "kind": "rank", "text": "x" * 61}], [{"at": 1, "kind": "rank", "text": "x", "more": 1}],
    [{"at": 1, "kind": "rank", "text": "x"}] * 7,
])
def test_a_malformed_wire_is_left_out_and_the_rest_of_the_record_is_kept(tmp_path, broken):
    world = _world_with_seed(42)
    vr.update_hall_of_fame(tmp_path, 77, world.save)
    path = tmp_path / "scores" / "77.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**record, "wire": broken}), encoding="utf-8")
    entry = vr._load_score_records(tmp_path)[0]
    assert "wire" not in entry and entry["handle"] == record["handle"]


def test_another_pilots_text_is_sanitized_before_it_is_shown():
    item = {"at": 10, "kind": "rank", "text": "reached \x1b[31mthe top\x07", "user_id": 2, "handle": "N\x1b[2Jib"}
    line = vr.wire_line(item)
    assert "\x1b" not in line and "\x07" not in line and "ib" in line and "the top" in line


def test_the_wire_is_newest_first_across_the_board():
    entries = [{"user_id": 1, "handle": "Ada", "wire": [{"at": 100, "kind": "rank", "text": "a"},
                                                         {"at": 300, "kind": "award", "text": "c"}]},
               {"user_id": 2, "handle": "Nib", "wire": [{"at": 200, "kind": "lost", "text": "b"}]},
               {"user_id": 3, "handle": "Idle"}]
    assert [(item["handle"], item["text"]) for item in vr.board_wire(entries)] == [("Ada", "c"), ("Nib", "b"), ("Ada", "a")]


# --- the deck -------------------------------------------------------------------

def _board(*items) -> list[dict]:
    return [{"at": at, "kind": "rank", "text": text, "user_id": user_id, "handle": handle}
            for at, text, user_id, handle in items]


def test_the_deck_gives_the_wire_its_row_only_once_todays_order_is_flown(terminal):
    terminal(80, 24)
    world = _live(_world_with_seed(42))
    now = int(time.time())
    world.wire_source = lambda: _board((now - 50, "reached Void Baron", 2, "Nib"), (now - 10, "earned mine", 77, "Me"))
    world.dispatch_clock = lambda: DAY
    assert "DISPATCH" in plain(vr.deck_wire_row(world)) and "WIRE" not in plain(vr.deck_wire_row(world))
    vr.fly_dispatch(world, vr.dispatch_order(world))
    row = plain(vr.deck_wire_row(world))
    assert "WIRE" in row and "Nib reached Void Baron" in row and "mine" not in row  # own news is not news
    assert vr._visible_width(vr.deck_wire_row(world)) <= vr._page_content_width()
    assert row in [plain(line) for line in vr.station_deck_lines(world)]
    # With no dispatch on this board the wire has the row to itself.
    world.dispatch_clock = None
    assert "Nib reached Void Baron" in plain(vr.deck_wire_row(world))


def test_old_news_and_an_empty_board_leave_the_streak_on_the_deck(terminal):
    terminal(80, 24)
    world = _live(_world_with_seed(42))
    world.dispatch_clock = lambda: DAY
    vr.fly_dispatch(world, vr.dispatch_order(world))
    stale = int(time.time()) - (vr.WIRE_DECK_DAYS + 1) * 86400
    world.wire_source = lambda: _board((stale, "reached Void Baron", 2, "Nib"))
    assert "DISPATCH flown" in plain(vr.deck_wire_row(world))
    world._wire_read = None
    world.wire_source = lambda: []
    assert "DISPATCH flown" in plain(vr.deck_wire_row(world))
    world.dispatch_clock = None
    assert vr.deck_wire_row(world) is None


def test_the_deck_reads_the_board_at_most_once_a_minute(monkeypatch):
    world = _live(_world_with_seed(42))
    reads = []
    world.wire_source = lambda: reads.append(1) or []
    clock = [1000.0]
    monkeypatch.setattr(vr.time, "monotonic", lambda: clock[0])
    for _ in range(5):
        world.board_wire()
    assert len(reads) == 1
    clock[0] += vr.WIRE_REFRESH_SECONDS + 1
    world.board_wire()
    assert len(reads) == 2


# --- the page and retirement ------------------------------------------------------

@pytest.mark.parametrize("width,height", [(80, 24), (40, 12)])
def test_the_dispatch_page_lists_the_wire_and_fits(terminal, width, height):
    terminal(width, height)
    world = _world_with_seed(42)
    world.dispatch_clock = lambda: DAY
    entries = [{"user_id": 2, "handle": "Nib", "best_credits": 0, "retirements": 0, "kills": 0, "missions_completed": 0,
                "wire": [{"at": 1_795_000_000 + n, "kind": "award", "text": f"earned thing {n}"} for n in range(6)]}]
    lines = vr.dispatch_lines(world, entries, 77)
    rows = [plain(row) for row in lines]
    wire = rows[rows.index(next(row for row in rows if "THE WIRE" in row)) + 1:]
    assert len(wire) == 6 and all("Nib earned thing" in row for row in wire)
    assert "thing 5" in wire[0] and "thing 0" in wire[-1]
    for page in vr._service_pages(lines, "Daily Dispatch", "[<>] Page [B] Back: "):
        assert len(page) + 3 <= height
    assert "THE WIRE" not in " ".join(plain(row) for row in vr.dispatch_lines(world, [], 77))


def test_retiring_is_news(tmp_path):
    world = _live(_finale_world("legend"))
    label = vr.CAREER_FINALES["legend"]["label"]
    world.wire("retired", f"retired as {label}")
    assert _texts(world) == [f"retired as {label}"]
    assert vr._score_wire([{"at": 5, "kind": "retired", "text": f"retired as {label}"}]) is not None
