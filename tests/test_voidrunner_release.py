"""What a release ships beside the game: the NetBBS door profile (#29).

A SysOp imports `netbbs.json` with NetBBS's [J] Import JSON to run a release
before NetBBS bundles it. These pin the rules NetBBS's profile validator
applies, and the ones that keep that copy's saves where the node backs them up.
"""

from __future__ import annotations

import json
import re

from .support import _VOIDRUNNER_PATH

PROFILE = _VOIDRUNNER_PATH.parent / "profiles" / "netbbs.json"


def _template() -> dict:
    return json.loads(PROFILE.read_text(encoding="utf-8"))


def test_the_profile_launches_this_script_natively_over_stdio():
    template = _template()
    assert set(template) == {"name", "executable_path", "args", "profile"}
    assert template["args"] == ["{install_dir}/voidrunner.py"]
    profile = template["profile"]
    assert profile["version"] == 1
    assert (profile["adapter"], profile["endpoint"], profile["encoding"]) == ("native", "stdio", "utf-8")
    assert profile["install_dir"].startswith("/")


def test_the_profile_follows_the_callers_terminal_and_its_resizes():
    profile = _template()["profile"]
    # Zero for both is NetBBS's "follow the caller"; it refuses one without the other.
    assert (profile["width"], profile["height"]) == (0, 0)
    assert profile["resize_signal"] is True
    assert "signal.SIGUSR1" in _VOIDRUNNER_PATH.read_text(encoding="utf-8")


def test_several_pilots_may_fly_but_the_game_keeps_one_session_per_pilot():
    profile = _template()["profile"]
    # NetBBS refuses more than one session without the certification; the
    # game's own per-pilot lock turns away a second session for the same pilot.
    assert 1 < profile["max_sessions"] <= 36
    assert profile["multinode_certified"] is True


def test_the_profile_never_moves_the_saves_out_of_the_node_backup():
    environment = _template()["profile"]["environment"]
    # NetBBS passes the node's own save directory to every launch; a profile
    # value would put the careers somewhere its backups do not look.
    assert "VOIDRUNNER_SAVE_DIR" not in environment
    assert all(key in ("TERM", "LANG", "LC_ALL", "TZ", "PATH") or re.fullmatch(r"DOOR_[A-Z0-9_]{1,48}", key)
               for key in environment)
