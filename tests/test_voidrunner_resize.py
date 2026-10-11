"""Following the caller's terminal mid-session (Thiesi/NetBBS#645).

A host that supports it rewrites the drop file and signals the door; the handler
only sets a flag, and the next action bar applies the new size. These two tests
lived in NetBBS's `test_doors_resize.py` until the move; NetBBS keeps the
end-to-end check that sends the real signal to the shipped file.
"""

from __future__ import annotations

import json

from .support import vr


def test_a_resize_redraws_at_the_new_size_when_it_lands_at_an_action_bar(monkeypatch, tmp_path):
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester", "terminal_width": 40, "terminal_height": 12}), encoding="utf-8")
    monkeypatch.setenv("NETBBS_DOOR_INFO", str(info))
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 24)
    monkeypatch.setattr(vr, "_RESIZE_PENDING", False)
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    # No signal yet: an action bar reads its key as it always did.
    assert vr.read_command_at_prompt() == "Q" and (vr._OUTPUT_WIDTH, vr._OUTPUT_HEIGHT) == (80, 24)
    vr._note_resize()   # all the handler does
    vr._note_resize()   # and it may run twice for one resize
    assert (vr._OUTPUT_WIDTH, vr._OUTPUT_HEIGHT) == (80, 24), "nothing changes inside the handler"
    assert vr.read_command_at_prompt() == vr.RESIZE_KEY
    assert (vr._OUTPUT_WIDTH, vr._OUTPUT_HEIGHT) == (40, 12)
    assert vr.read_command_at_prompt() == "Q", "one resize is one redraw"


def test_a_resize_below_the_floor_draws_for_the_floor_and_is_no_screens_key(monkeypatch, tmp_path):
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester", "terminal_width": 20, "terminal_height": 5}), encoding="utf-8")
    monkeypatch.setenv("NETBBS_DOOR_INFO", str(info))
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 24)
    monkeypatch.setattr(vr, "_RESIZE_PENDING", True)
    assert vr.take_resize() and (vr._OUTPUT_WIDTH, vr._OUTPUT_HEIGHT) == (vr.MINIMUM_WIDTH, vr.MINIMUM_HEIGHT)
    # A screen treats it like any key it does not know. It must never read as one it does.
    assert len(vr.RESIZE_KEY) > 1 and not vr.RESIZE_KEY.isspace()
    assert vr.RESIZE_KEY not in "MYBCSHGTQKLDPWONVX<>0123456789"
