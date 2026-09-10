"""What the caller actually sees: boxes, widths, pagination, action
bars and the input decoder.

Split out of `test_voidrunner_domain.py` (issue #422).
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

import pytest

from .support import _VOIDRUNNER_PATH, _door_stopped_at, _escort_world, _finale_world, _mission_details_world, _set_cargo, _world_with_named_crew, _world_with_pending_fight, _world_with_seed, vr


#
# The tactical-HUD box rendering #190 introduced draws every screen as a
# "|...content...|" box whose outer border (the "|--- ... ---|" separator
# rows) is always exactly 79 columns wide. Content rows are supposed to
# right-pad to match that same width, but several screens either hand-
# typed a header row's spacing without counting it precisely, or let an
# unbounded piece of text (a mission description, a sector name, an
# upgrade's effect blurb) run past its column budget -- both silently
# push that one row's right-hand border past (or short of) where every
# other row's border sits, breaking the box. These tests pin the fixed
# cases directly rather than re-deriving the whole checker, since the
# box style itself (a fixed 79-column border) is what every one of these
# regressions would otherwise quietly reappear against.


def _assert_box_rows_match_border(text: str, label: str) -> None:
    stripped = [vr._ANSI_RE.sub("", line) for line in text.split("\r\n")]
    border_widths = {len(l) for l in stripped if l.strip().startswith(("╭", "├", "╰"))}
    assert len(border_widths) <= 1, f"{label}: inconsistent border widths {border_widths}"
    if not border_widths:
        return
    (border_width,) = border_widths
    for line in stripped:
        if line.strip().startswith("│"):
            assert line.rstrip().endswith("│"), f"{label}: right border missing: {line!r}"
            assert len(line) == border_width, (
                f"{label}: content row width {len(line)} != border width {border_width}: {line!r}"
            )


@pytest.mark.parametrize("width,height",[(20,10),(40,12),(80,24)])
@pytest.mark.parametrize("section",["C","H"])
def test_pilot_record_pages_expose_every_retained_entry_once_without_rebuilding(monkeypatch, terminal,width,height,section):
    import re
    terminal(width, height)
    world=_world_with_seed(42)
    world.save.active_missions=[vr.Mission(i+1,"escort",f"Full contract explanation with a distinctive ending END{i:03}",100,0,1,deadline_turn=100) for i in range(35)]
    world.save.pilot.highlights=[f"Highlight-{i:03}-END" for i in range(35)]
    world.save.pilot.log=[f"Log-{i:03}-END" for i in range(80)]
    before=world.save.to_dict();rng=world.event_rng.getstate()
    world._checkpoint=lambda _:pytest.fail("Record browsing checkpointed")
    output=io.StringIO();frames=[];phase=0;builds=[]
    original=vr._service_pages
    def build(lines,title,footer):
        builds.append(title)
        return original(lines,title,footer)
    monkeypatch.setattr(vr,"_service_pages",build)
    def choose():
        nonlocal phase
        frame=vr._ANSI_RE.sub("",output.getvalue());output.seek(0);output.truncate(0)
        assert len(frame.splitlines())<=height
        assert all(vr._visible_width(line)<=width for line in frame.splitlines())
        assert "[B] Back:" in " ".join(frame.split())
        if phase==0:phase=1;return section
        if phase==2:return "B"
        frames.append(frame)
        page,count=map(int,re.search(r"(\d+)/(\d+)",frame).groups())
        if page==count:phase=2;return "O"
        return ">"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):vr.screen_status(vr.Palette(False),world)
    text=" ".join(" ".join(frames).split())
    markers=[f"END{i:03}" for i in range(35)] if section=="C" else [f"Highlight-{i:03}-END" for i in range(35)]+[f"Log-{i:03}-END" for i in range(80)]
    for marker in markers:assert text.count(marker)==1
    assert len(builds)==2
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng


@pytest.mark.parametrize("commands",[b"SCH>OBQ",b"SCH>",b"SRSNBBQ",b"SR"])
def test_real_pilot_record_browsing_cancel_and_eof_preserve_career(tmp_path,commands):
    import json,os,subprocess
    world=_world_with_seed(42)
    world.save.pilot.credits=vr.RANKS[-1][0]
    world.save.pilot.highest_rank_seen=len(vr.RANKS)-1
    world.save.pilot.highlights=["A retained early accomplishment"]*20
    world._checkpoint=lambda current:vr.persist(current,tmp_path,77);world.checkpoint()
    original=(tmp_path/"77.json").read_bytes()
    info=tmp_path/"door_info.json"
    info.write_text(json.dumps({"user_id":77,"handle":"Tester","terminal_width":40,"terminal_height":12}),encoding="utf-8")
    result=subprocess.run([sys.executable,str(_VOIDRUNNER_PATH)],input=commands,capture_output=True,
        env=dict(os.environ,VOIDRUNNER_SAVE_DIR=str(tmp_path),NETBBS_DOOR_INFO=str(info)),timeout=10)
    assert result.returncode==0 and not result.stderr and b"Pilot Record:" in result.stdout
    if b"N" in commands:assert b"Retirement cancelled" in result.stdout
    assert (tmp_path/"77.json").read_bytes()==original


def test_pilot_record_retirement_acknowledges_a_saved_new_career(tmp_path):
    world=_world_with_seed(42)
    world.save.pilot.credits=vr.RANKS[-1][0]
    world.save.pilot.highest_rank_seen=len(vr.RANKS)-1
    world._checkpoint=lambda current:vr.persist(current,tmp_path,77);world.checkpoint()
    with _door_stopped_at(tmp_path,b"SHRSY",b"A new career begins."):
        saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
        assert saved.pilot.retirements==1 and saved.seed!=world.save.seed
        assert saved.pilot.credits==1200+vr.RETIREMENT_STARTING_CREDITS_BONUS


def test_screen_status_preserves_complete_mission_description(monkeypatch):
    world = _world_with_seed(300)
    world.save.pilot.credits = 15_000
    world.save.active_missions = [
        vr.Mission(id=1, kind="escort",
                   description="Escort a supply convoy to Perrin's Folly (4 jump(s), raider activity expected)",
                   reward=900, origin_system=0, target_system=5, pirate_tier=3, deadline_turn=40),
    ]
    keys = iter(["C", "B"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_status(vr.Palette(truecolor=False), world)
    text = " ".join(vr._ANSI_RE.sub("", buf.getvalue()).split())
    assert world.save.active_missions[0].description in text
    assert all(vr._visible_width(line) <= 80 for line in buf.getvalue().splitlines())


def test_screen_status_credits_remain_complete_and_width_safe(monkeypatch):
    world = _world_with_seed(301)
    world.save.pilot.credits = 1_234_567
    monkeypatch.setattr(vr, "read_key", lambda: "B")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_status(vr.Palette(truecolor=False), world)
    assert "1,234,567 cr" in buf.getvalue()
    assert all(vr._visible_width(line) <= 80 for line in buf.getvalue().splitlines())


@pytest.mark.parametrize("width,height", [(20,10), (40,12), (80,24)])
@pytest.mark.parametrize("screen", ["yard", "crew"])
@pytest.mark.parametrize("maxed", [False, True])
def test_service_pages_retain_all_terms_and_fit_terminal(monkeypatch, terminal,width,height,screen,maxed):
    import copy,re
    world=_world_with_seed(302); world.save.pilot.credits=100_000
    if maxed:
        for key,upgrade in vr.UPGRADES.items(): setattr(world.save.ship,f"{key}_tier",upgrade["max_tier"])
        for role in vr.CREW_ROLES: setattr(world.save.ship,f"has_{role}",True)
    before=copy.deepcopy(world.save.to_dict()); rng=world.event_rng.getstate()
    monkeypatch.setattr(world,"checkpoint",lambda:pytest.fail("Paging saved"))
    terminal(width, height)
    output=io.StringIO(); frames=[]
    def choose():
        frame=output.getvalue(); frames.append(frame); output.seek(0); output.truncate(0)
        plain=" ".join(vr._ANSI_RE.sub("",frame).split())
        match=re.search(r"(?:Engineering Yard|Crew Roster): 100,000cr (\d+)/(\d+)",plain)
        assert match and len(frames)<200
        assert "[B] Back" in plain or "[B] Back" in plain
        return "Q" if match[1]==match[2] else ">"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):
        (vr.screen_shipyard if screen=="yard" else vr.screen_crew)(vr.Palette(False),world)
    assert all(len(frame.splitlines())<=height for frame in frames)
    assert all(vr._visible_width(line)<=width for frame in frames for line in frame.splitlines())
    # Check full terms without mixing page controls into wrapped phrases.
    plain=" ".join(vr._ANSI_RE.sub(""," ".join(frames)).split())
    for info in (vr.UPGRADES if screen=="yard" else vr.CREW_ROLES).values():
        for word in info["label"].split(): assert word in plain
    if screen=="yard":
        assert "Freighter-Class" in plain and "Cutter-Class" in plain
        assert ("MAXED" if maxed else "Tier 0") in plain
    else: assert ("HIRED" if maxed else "Available") in plain and "cr/jump" in plain
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng


@pytest.mark.parametrize("screen", ["yard","crew"])
def test_service_choices_keep_price_and_benefit_with_label_at_40_columns(monkeypatch, terminal,screen):
    import re
    world=_world_with_seed(42)
    terminal(40, 12)
    output=io.StringIO();frames=[]
    def choose():
        frame=output.getvalue();frames.append(" ".join(vr._ANSI_RE.sub("",frame).split()));output.seek(0);output.truncate(0)
        match=re.search(r"1,200cr (\d+)/(\d+)",frames[-1]);assert match
        return "Q" if match[1]==match[2] else ">"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):
        (vr.screen_shipyard if screen=="yard" else vr.screen_crew)(vr.Palette(False),world)
    for key,info in (vr.UPGRADES if screen=="yard" else vr.CREW_ROLES).items():
        matches=[frame for frame in frames if info["label"] in frame]
        assert len(matches)==1
        effect = info["effect"] if screen == "yard" else vr.crew_effect(key, vr.crew_level(world.save.ship, key))
        assert effect in matches[0]
        cost=info["cost"](getattr(world.save.ship,f"{key}_tier")) if screen=="yard" else info["hire_cost"]
        assert (f"{cost:,}cr" if screen=="yard" else f"{cost}cr") in matches[0]


@pytest.mark.parametrize("commands", [b"Y><QQ",b"Y",b"YK><QQQ",b"YK",b"YANQQ",b"YKANQQQ",b"YR\rQQ",b"YPNQQ"])
def test_real_responsive_service_browsing_and_cancellation_preserve_career(tmp_path,commands):
    import json,os,subprocess
    world=_world_with_seed(42); world.save.ship.fuel=20; world.save.ship.hull_hp=50
    world._checkpoint=lambda current:vr.persist(current,tmp_path,77); world.checkpoint()
    original=(tmp_path/"77.json").read_bytes()
    info=tmp_path/"door_info.json"
    info.write_text(json.dumps({"user_id":77,"handle":"Tester","terminal_width":40,"terminal_height":12}),encoding="utf-8")
    result=subprocess.run([sys.executable,str(_VOIDRUNNER_PATH)],input=commands,capture_output=True,
        env=dict(os.environ,VOIDRUNNER_SAVE_DIR=str(tmp_path),NETBBS_DOOR_INFO=str(info)),timeout=10)
    assert result.returncode==0 and not result.stderr and b"Engineering Yard" in result.stdout
    if b"K" in commands: assert b"Crew Roster" in result.stdout
    assert (tmp_path/"77.json").read_bytes()==original


def test_service_retained_upgrade_result_is_durable_without_menu_exit(tmp_path):
    world=_world_with_seed(42)
    world._checkpoint=lambda current:vr.persist(current,tmp_path,77); world.checkpoint()
    cost=vr.UPGRADES["cargo"]["cost"](world.save.ship.cargo_tier)
    before=world.save.pilot.credits
    with _door_stopped_at(tmp_path,b"YAY",b"Result: Cargo Bay Expansion upgraded"):
        saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
        assert saved.ship.cargo_tier==1 and saved.pilot.credits==before-cost


@pytest.mark.parametrize("width,height",[(20,10),(40,12),(80,24)])
@pytest.mark.parametrize("haven",[False,True])
def test_market_catalog_pages_preserve_goods_quotes_and_telemetry(monkeypatch, terminal,width,height,haven):
    import re
    terminal(width, height)
    world=_world_with_seed(42)
    if haven:world.save.current_system=next(station.id for station in world.galaxy if station.economy=="Haven")
    _set_cargo(world, {"weapons":1})
    before=world.save.to_dict();rng=world.event_rng.getstate()
    output=io.StringIO();frames=[]
    def choose():
        frame=vr._ANSI_RE.sub("",output.getvalue());output.seek(0);output.truncate(0);frames.append(frame)
        assert len(frame.splitlines())<=height
        assert all(vr._visible_width(line)<=width for line in frame.splitlines())
        plain=" ".join(frame.split())
        assert "[B] Back:" in plain and "[X] Futures" in plain and "1,200cr" in plain
        page,count=map(int,re.search(r"(\d+)/(\d+)",frame).groups())
        return "Q" if page==count else ">"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):vr.screen_market(vr.Palette(False),world)
    text=" ".join(" ".join(frames).split())
    for commodity in vr.LEGAL_COMMODITIES+["weapons"]:assert vr.COMMODITIES[commodity]["label"] in text
    assert "prohibited" in text or haven
    assert "Illegal" in text and "Stock" in text and "demand" in text and "hold" in text
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng


@pytest.mark.parametrize("key",list("ACDEF"))
def test_market_commodity_keys_keep_identity_after_paging(monkeypatch, terminal,key):
    terminal(40, 12)
    world=_world_with_seed(42);selected=[]
    commands=iter([">",">",key,"Q"])
    monkeypatch.setattr(vr,"read_key",lambda:next(commands))
    monkeypatch.setattr(vr,"_trade_commodity",lambda p,w,c:selected.append(c))
    with contextlib.redirect_stdout(io.StringIO()):vr.screen_market(vr.Palette(False),world)
    assert selected==[vr.LEGAL_COMMODITIES[vr.MARKET_LETTERS.index(key)]]


def test_market_retains_trade_result_through_cancelled_quantity(monkeypatch):
    world=_world_with_seed(42);saved=[]
    world._checkpoint=lambda current:saved.append(current.save.to_dict())
    commands=iter(["A","P","A","P","Q"]);quantities=iter(["1",""])
    monkeypatch.setattr(vr,"read_key",lambda:next(commands))
    monkeypatch.setattr(vr,"read_line_raw",lambda **kw:next(quantities))
    with contextlib.redirect_stdout(io.StringIO()) as output:vr.screen_market(vr.Palette(False),world)
    assert output.getvalue().count("Result: Bought 1x Food")==2
    assert world.save.cargo=={"food":1} and len(saved)==1
    assert f"Market: {world.save.pilot.credits:,}cr" in output.getvalue()


def test_prohibited_commodity_details_hide_buy_and_reject_unadvertised_purchase(monkeypatch):
    world=_world_with_seed(42);_set_cargo(world, {"weapons":1});before=world.save.to_dict()
    monkeypatch.setattr(vr,"read_key",lambda:"P")
    with contextlib.redirect_stdout(io.StringIO()) as output:result=vr._trade_commodity(vr.Palette(False),world,"weapons")
    assert "Buy prohibited" in output.getvalue() and "[P] Purchase" not in output.getvalue()
    assert "prohibit" in result and world.save.to_dict()==before


@pytest.mark.parametrize("commands",[b"M><AQ Q".replace(b" ",b""),b"M><",b"MAP\rQ",b"MXBQ"])
def test_real_market_catalog_browsing_cancel_and_eof_preserve_career(tmp_path,commands):
    import json,os,subprocess
    world=_world_with_seed(42)
    world._checkpoint=lambda current:vr.persist(current,tmp_path,77);world.checkpoint()
    original=(tmp_path/"77.json").read_bytes()
    info=tmp_path/"door_info.json";info.write_text(json.dumps({"user_id":77,"handle":"Tester","terminal_width":40,"terminal_height":12}),encoding="utf-8")
    result=subprocess.run([sys.executable,str(_VOIDRUNNER_PATH)],input=commands,capture_output=True,
        env=dict(os.environ,VOIDRUNNER_SAVE_DIR=str(tmp_path),NETBBS_DOOR_INFO=str(info)),timeout=10)
    assert result.returncode==0 and not result.stderr and b"Market: 1,200cr" in result.stdout
    assert (tmp_path/"77.json").read_bytes()==original


@pytest.mark.parametrize("action",["buy","sell"])
def test_market_retained_trade_result_is_durable_before_disconnect(tmp_path,action):
    world=_world_with_seed(42)
    if action=="sell":_set_cargo(world, {"food":2})
    world._checkpoint=lambda current:vr.persist(current,tmp_path,77);world.checkpoint()
    command=b"MAP1\r" if action=="buy" else b"MAS1\r"
    marker=b"Result: Bought 1x Food" if action=="buy" else b"Result: Sold 1x Food"
    with _door_stopped_at(tmp_path,command,marker):
        saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
        assert saved.cargo=={"food":1}
        assert saved.pilot.credits!=world.save.pilot.credits


def test_screen_market_contraband_catalog_keeps_labels_and_bounds(monkeypatch):
    world = _world_with_seed(303)
    world.save.pilot.credits = 50_000
    haven = next(s for s in world.galaxy if s.economy == "Haven")
    world.save.current_system = haven.id
    haven.discovered = True
    _set_cargo(world, {"weapons": 5, "narcotics": 3})
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_market(vr.Palette(truecolor=False), world)
    text = " ".join(vr._ANSI_RE.sub("", buf.getvalue()).split())
    # The market row now reads "Stock N; demand N; hold N" so it fits an 80-column page (#412).
    assert "Illegal" in text and "demand" in text and "Cargo Hold: 8/" in text
    assert all(vr._visible_width(line) <= 80 for line in buf.getvalue().splitlines())


@pytest.mark.parametrize("width,height", [(20,10),(40,12),(80,24)])
@pytest.mark.parametrize("expanded", [False,True])
def test_station_deck_pages_keep_telemetry_actions_and_exit_visible(monkeypatch, terminal,without_action_bar,width,height,expanded):
    import re
    terminal(width, height)
    world=_world_with_seed(42)
    world.save.ship.has_gunner=True
    world.save.ship.hull_hp=1
    world.save.pilot.credits=5
    _set_cargo(world, {"weapons":2})
    world.save.active_event={"description":"Regional supply disruption", "turns_remaining":3}
    before=world.save.to_dict()
    world._checkpoint=lambda _:pytest.fail("Browsing checkpointed")
    output=io.StringIO();frames=[];toggled=False
    def choose():
        nonlocal toggled
        frame=vr._ANSI_RE.sub("",output.getvalue());output.seek(0);output.truncate(0)
        assert len(frame.splitlines())<=height
        assert all(vr._visible_width(line)<=width for line in frame.splitlines())
        plain=" ".join(frame.split())
        assert "[Q] Exit:" in plain
        assert "5cr" in plain
        if expanded and not toggled:
            toggled=True
            return "X"
        frames.append(re.sub(r"^[\s>]*Command Deck:\s*[\d,]+cr\s+\d+/\d+","",without_action_bar(frame)))
        page,count=map(int,re.search(r"(\d+)/(\d+)",frame).groups())
        return "Q" if page==count else ">"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):assert vr.screen_station_menu(vr.Palette(False),world)=="Q"
    text=" ".join(" ".join(frames).split())
    for phrase in ("Station Services", "CRITICAL HULL", "Contraband aboard", "Regional supply disruption", "LOW CASH", "Cargo 2/", "[M]", "[Y]", "[B]", "[C]", "[S]", "[H]", "[G]", "[T]"):
        assert phrase in text
    if expanded:assert "systems charted" in text and "Crew:" in text
    assert world.save.to_dict()==before


@pytest.mark.parametrize("key",list("MYBCSHGTQ"))
def test_station_deck_service_keys_work_after_paging_and_expansion(monkeypatch, terminal,key):
    terminal(20, 10)
    world=_world_with_seed(42)
    before=world.save.to_dict()
    commands=iter([">","X",">","<",key])
    monkeypatch.setattr(vr,"read_key",lambda:next(commands))
    with contextlib.redirect_stdout(io.StringIO()):assert vr.screen_station_menu(vr.Palette(False),world)==key
    assert world.save.to_dict()==before


@pytest.mark.parametrize("commands", [b">X><XQ", b">X><", b">XMQYQQ"])
def test_real_cockpit_paging_toggle_and_exit_preserve_career(tmp_path,commands):
    import json,os,subprocess
    world=_world_with_seed(42)
    world._checkpoint=lambda current:vr.persist(current,tmp_path,77);world.checkpoint()
    original=(tmp_path/"77.json").read_bytes()
    info=tmp_path/"door_info.json"
    info.write_text(json.dumps({"user_id":77,"handle":"Tester","terminal_width":40,"terminal_height":12}),encoding="utf-8")
    result=subprocess.run([sys.executable,str(_VOIDRUNNER_PATH)],input=commands,capture_output=True,
        env=dict(os.environ,VOIDRUNNER_SAVE_DIR=str(tmp_path),NETBBS_DOOR_INFO=str(info)),timeout=10)
    assert result.returncode==0 and not result.stderr
    assert b"Command Deck:" in result.stdout and b"[X] Compact" in result.stdout
    if b"M" in commands:
        assert b"Commodity Market" in result.stdout and b"Engineering Yard:" in result.stdout
    assert (tmp_path/"77.json").read_bytes()==original


def test_station_retained_settlement_result_is_durable_before_disconnect(tmp_path):
    world=_world_with_seed(42)
    vr.buy_futures_contract(world,"food",2,5)
    world.save.turn=5
    world._checkpoint=lambda current:vr.persist(current,tmp_path,77);world.checkpoint()
    with _door_stopped_at(tmp_path,b"",b"Result:"):
        saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
        assert not saved.active_futures
        assert saved.cargo=={"food":2}


def test_screen_station_menu_special_ops_fit_the_standard_page(monkeypatch):
    world = _world_with_seed(304)
    world.save.current_system = world.landmark["system_id"]
    world.by_id[world.save.current_system].discovered = True
    _set_cargo(world, {"weapons": 2})
    world.save.pilot.reputation[vr.FACTION_CONCORD] = vr.CONCORD_COMMISSION_THRESHOLD
    world.save.pilot.reputation[vr.FACTION_BLACKWAKE] = vr.BLACKWAKE_MADE_THRESHOLD
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_station_menu(vr.Palette(truecolor=False), world)
    text = vr._ANSI_RE.sub("", buf.getvalue())
    assert all(vr._visible_width(line) <= 80 for line in text.splitlines())
    assert len(text.splitlines()) <= 24
    for key in ("[L]", "[D]", "[P]", "[W]"):
        assert key in text


@pytest.mark.parametrize("width,height",[(20,10),(40,12),(80,24)])
@pytest.mark.parametrize("discovered",[False,True])
def test_navigation_chart_pages_preserve_all_connections_and_career(monkeypatch, terminal,width,height,discovered):
    import copy,re
    world=_world_with_seed(305);world.here.connections=list(range(1,len(world.galaxy)))
    for station in world.galaxy[1:]:station.discovered=discovered
    before=copy.deepcopy(world.save.to_dict());rng=world.event_rng.getstate()
    monkeypatch.setattr(world,"checkpoint",lambda:pytest.fail("Chart browsing saved"))
    terminal(width, height)
    output=io.StringIO();frames=[]
    def choose():
        frame=output.getvalue();frames.append(frame);output.seek(0);output.truncate(0)
        plain=" ".join(vr._ANSI_RE.sub("",frame).split())
        match=re.search(r"Navigation: Fuel 24/24 (\d+)/(\d+)",plain);assert match and len(frames)<300
        assert "[B] Back" in plain
        return "Q" if match[1]==match[2] else ">"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output): assert vr.screen_chart(vr.Palette(False),world) is None
    assert all(len(frame.splitlines())<=height for frame in frames)
    assert all(vr._visible_width(line)<=width for frame in frames for line in frame.splitlines())
    # Remove repeated row keys before joining wrapped entry text.
    text=" ".join(re.sub(r"\[[A-Z]\] ","",vr._ANSI_RE.sub(""," ".join(frames))).split())
    for station in world.galaxy[1:]:
        if discovered: assert station.name in text and vr.sector_for(station) in text
        else: assert station.name not in text
        assert f"({station.x},{station.y})" in text
    if not discovered: assert "danger unknown" in text
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng


@pytest.mark.parametrize("width,height",[(40,12),(80,24)])
def test_navigation_chart_selects_last_connection_beyond_one_alphabet(monkeypatch, terminal,width,height):
    import re
    world=_world_with_seed(42);world.here.connections=list(range(1,len(world.galaxy)))
    for station in world.galaxy:station.discovered=True
    target=world.galaxy[-1];target.name="FINAL";world.save.ship.fuel=999
    terminal(width, height)
    output=io.StringIO();seen=[]
    def choose():
        frame=vr._ANSI_RE.sub("",output.getvalue());seen.append(frame);output.seek(0);output.truncate(0)
        if "Depart for FINAL?" in frame: return "Y"
        match=re.search(r"\[([A-Z])\] FINAL",frame)
        if match:
            assert match[1] not in vr.CHART_RESERVED_LETTERS
            return match[1]
        assert len(seen)<100
        return ">"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):selected=vr.screen_chart(vr.Palette(False),world)
    assert selected==target.id and len(seen)>1 and world.here.id==0


@pytest.mark.parametrize("commands",[b"C><QQ",b"C",b"CAQQ",b"CA"])
def test_real_responsive_chart_back_eof_and_rejected_jump_preserve_career(tmp_path,commands):
    import json,os,subprocess
    world=_world_with_seed(42);world.save.ship.fuel=0;_set_cargo(world, {"food":1})
    world._checkpoint=lambda current:vr.persist(current,tmp_path,77);world.checkpoint()
    original=(tmp_path/"77.json").read_bytes()
    info=tmp_path/"door_info.json";info.write_text(json.dumps({"user_id":77,"handle":"Tester","terminal_width":40,"terminal_height":12}),encoding="utf-8")
    result=subprocess.run([sys.executable,str(_VOIDRUNNER_PATH)],input=commands,capture_output=True,
        env=dict(os.environ,VOIDRUNNER_SAVE_DIR=str(tmp_path),NETBBS_DOOR_INFO=str(info)),timeout=10)
    assert result.returncode==0 and not result.stderr and b"Navigation: Fuel" in result.stdout
    if b"A" in commands: assert b"Result: Not enough fuel" in result.stdout
    assert (tmp_path/"77.json").read_bytes()==original


def test_chart_retained_scan_result_is_checkpointed_before_disconnect(tmp_path):
    world=_world_with_seed(42);world.save.ship.scanner_tier=1
    world._checkpoint=lambda current:vr.persist(current,tmp_path,77);world.checkpoint()
    before=set(world.save.discovered)
    expected=set(vr.survey_candidates(world))
    with _door_stopped_at(tmp_path,b"CSS",b"Result: Survey complete"):
        saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
        assert set(saved.discovered)-before==expected and saved.turn==0
        assert saved.ship.fuel == world.save.ship.fuel - 2


@pytest.mark.parametrize("width,height",[(20,10),(40,12),(80,24)])
def test_score_pages_retain_all_twenty_pilots_and_fields_without_reloading(tmp_path,monkeypatch, terminal,width,height):
    import json,re
    terminal(width, height)
    records=[{"user_id":i,"handle":f"Pilot-{i:02}","best_credits":1_000_000-i,"rank":vr.RANKS[-1][1],"kills":100+i,"missions_completed":200+i,"retirements":i} for i in range(1,26)]
    path=tmp_path/"leaderboard.json";path.write_text(json.dumps(records),encoding="utf-8");original_bytes=path.read_bytes()
    vr.import_hall_of_fame(tmp_path)  # the old file reaches the rankings through `scores/` (#421)
    world=_world_with_seed(42);before=world.save.to_dict()
    output=io.StringIO();frames=[];loads=[];builds=[]
    original_load=vr._load_score_records;original_pages=vr._service_pages
    def load(directory):loads.append(directory);return original_load(directory)
    def pages(*args):builds.append(1);return original_pages(*args)
    monkeypatch.setattr(vr,"_load_score_records",load);monkeypatch.setattr(vr,"_service_pages",pages)
    def choose():
        frame=vr._ANSI_RE.sub("",output.getvalue());output.seek(0);output.truncate(0);frames.append(frame)
        assert len(frame.splitlines())<=height
        assert all(vr._visible_width(line)<=width for line in frame.splitlines())
        assert "[B] Back:" in " ".join(frame.split())
        page,count=map(int,re.search(r"(\d+)/(\d+)",frame).groups())
        return "B" if page==count else "N"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):vr.screen_hall_of_fame(vr.Palette(False),world,tmp_path,20)
    text=" ".join(" ".join(frames).split())
    for i in range(1,21):
        assert text.count(f"Pilot-{i:02}")==1
        assert f"{1_000_000-i:,}cr" in text
        assert str(100+i) in text and str(200+i) in text
    assert "Pilot-21" not in text and text.count("[YOU]")==2
    assert len(loads)==len(builds)==1
    assert path.read_bytes()==original_bytes and world.save.to_dict()==before


@pytest.mark.parametrize("commands",[b"HNPNBQ",b"HN",b"H"])
def test_real_score_paging_back_and_eof_preserve_career_and_scores(tmp_path,commands):
    import json,os,subprocess
    world=_world_with_seed(42)
    world._checkpoint=lambda current:vr.persist(current,tmp_path,77);world.checkpoint()
    for uid in range(1,26):
        save=vr._new_career(f"Pilot-{uid:02}");save.pilot.credits=uid*1000
        vr.update_hall_of_fame(tmp_path,uid,save)
    paths=[tmp_path/"77.json",*(tmp_path/"scores").glob("*.json")]
    before={path:path.read_bytes() for path in paths}
    info=tmp_path/"door_info.json";info.write_text(json.dumps({"user_id":77,"handle":"Tester","terminal_width":40,"terminal_height":12}),encoding="utf-8")
    result=subprocess.run([sys.executable,str(_VOIDRUNNER_PATH)],input=commands,capture_output=True,
        env=dict(os.environ,VOIDRUNNER_SAVE_DIR=str(tmp_path),NETBBS_DOOR_INFO=str(info)),timeout=10)
    assert result.returncode==0 and not result.stderr and b"Hall of Fame" in result.stdout
    assert {path:path.read_bytes() for path in paths}==before


def test_screen_hall_of_fame_records_are_complete_and_width_safe(monkeypatch):
    world = _world_with_seed(307)
    import json
    import tempfile
    from pathlib import Path

    entries = [{
        "user_id": 1, "handle": "SixteenCharHandl", "best_credits": 999_999,
        "rank": vr.RANKS[-1][1], "retirements": 3, "kills": 120, "missions_completed": 88,
    }]
    save_dir = Path(tempfile.mkdtemp())
    (save_dir / "leaderboard.json").write_text(json.dumps(entries), encoding="utf-8")
    vr.import_hall_of_fame(save_dir)  # the old file reaches the screen through `scores/` (#421)
    monkeypatch.setattr(vr, "read_key", lambda: "B")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_hall_of_fame(vr.Palette(truecolor=False), world, save_dir, 1)
    text = " ".join(vr._ANSI_RE.sub("", buf.getvalue()).split())
    assert "SixteenCharHandl" in text and "999,999cr" in text
    assert "[YOU]" in text and "combat victories 120" in text
    assert "missions 88" in text and "retirements 3" in text
    assert all(vr._visible_width(line) <= 80 for line in buf.getvalue().splitlines())


def test_screen_customs_large_contraband_stash_has_complete_width_safe_terms(monkeypatch):
    world = _world_with_seed(308)
    _set_cargo(world, {"weapons": 20, "narcotics": 15})
    world.save.pilot.credits = 50_000
    monkeypatch.setattr(vr, "read_key", lambda: "S")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_customs(vr.Palette(truecolor=False), world)
    text = " ".join(vr._ANSI_RE.sub("", buf.getvalue()).split())
    assert "35 units" in text and "60% acceptance" in text and "no debt" in text
    assert all(vr._visible_width(line) <= 80 for line in buf.getvalue().splitlines())


# The box-border checks above only pin the *right edge* of each row -- they
# can't catch a column that starts or ends in a different place from row to
# row, because a naive `f"{ansi_colored_value:<N}"` format spec counts the
# invisible escape bytes as part of N. Since every colored value in this
# module (`_gauge_bar`'s output, the market/chart/shipyard status strings)
# happens to share the same fixed-length ANSI overhead across rows, the
# resulting under-padding is *constant* and the box border still lands in
# the right place -- but the column itself silently drifts out of alignment
# with its neighbors. These tests pin the actual column position of the
# text immediately after each fixed-width colored field.


@pytest.mark.parametrize("action", ["upgrade", "fuel", "repair", "crew", "refit", "rejected"])
def test_service_actions_retain_result_and_updated_credit_heading(monkeypatch,action):
    world=_world_with_seed(309); world.save.pilot.credits=20_000
    world.save.ship.fuel=0; world.save.ship.hull_hp-=1
    sequence={"upgrade":"AYQ","fuel":"RQ","repair":"PYQ","crew":"AYQ","refit":"GCYQ","rejected":"AQ"}[action]
    if action=="rejected": world.save.pilot.credits=0
    keys=iter(sequence); monkeypatch.setattr(vr,"read_key",lambda:next(keys))
    monkeypatch.setattr(vr,"read_line_raw",lambda **kw:"1")
    saved=[]; world._checkpoint=lambda current:saved.append(current.save.pilot.credits)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        (vr.screen_crew if action=="crew" else vr.screen_shipyard)(vr.Palette(False),world)
    plain=vr._ANSI_RE.sub("",output.getvalue())
    assert "Result:" in plain and f"{world.save.pilot.credits:,}cr" in plain.split("Result:")[0]
    assert len(saved)==(0 if action=="rejected" else 1)
    if saved: assert saved[-1]==world.save.pilot.credits


def test_screen_missions_preserves_rewards_in_compact_entries(monkeypatch):
    world = _world_with_seed(310)
    world.save.pilot.credits = 5_000
    monkeypatch.setattr(
        vr, "posted_mission_offers",
        lambda world: [
            vr.Mission(id=1, kind="bounty", description="Short", reward=5,
                       origin_system=0, target_system=1, pirate_tier=1, deadline_turn=None),
            vr.Mission(id=2, kind="cargo", description="Also short", reward=123_456,
                       origin_system=0, target_system=2, pirate_tier=0, deadline_turn=None),
        ],
    )
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_missions(vr.Palette(truecolor=False), world)
    output = buf.getvalue()
    assert "+5cr" in output and "+123,456cr" in output
    assert "[1]" in output and "[2]" in output
    assert "Details" in output and "[B] Back" in output


def test_navigation_chart_keeps_danger_and_fuel_distinct_with_retained_rejection(monkeypatch):
    world=_world_with_seed(311)
    safe,danger=world.here.connections[:2]
    world.by_id[safe].danger=0;world.by_id[danger].danger=3
    for sid in (safe,danger):world.by_id[sid].discovered=True
    world.save.ship.fuel=0
    key=vr.CHART_CONNECTION_LETTERS[sorted(world.here.connections).index(danger)]
    keys=iter([key,"Q"]);monkeypatch.setattr(vr,"read_key",lambda:next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:assert vr.screen_chart(vr.Palette(False),world) is None
    plain=vr._ANSI_RE.sub("",output.getvalue())
    assert "Danger 0" in plain and "Danger 3" in plain and "LOW FUEL" in plain
    assert "Result: Not enough fuel" in plain and world.save.turn==0


def test_screen_title_renders_full_splash_with_tagline_and_exact_box_width():
    p = vr.Palette(truecolor=False)
    for node, handle in [("Central BBS", "Alice"), ("A Very Long BBS Node Name Here", "SixteenCharHandl")]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            vr.screen_title(p, {"node_name": node, "handle": handle})
        output = buf.getvalue()
        assert "a NetBBS door game" in output
        assert "V O I D R U N N E R" in output
        stripped = [vr._ANSI_RE.sub("", line) for line in output.split("\r\n") if line.strip()]
        box_lines = [line for line in stripped if line.startswith(("╔", "║", "╠", "╚"))]
        assert len(box_lines) in (11, 13)  # 13 when the meta fields stack rather than clip (#404)
        assert handle in " ".join(box_lines)
        widths = {len(line) for line in box_lines}
        assert widths == {79}, f"expected all splash box rows to be 79 visible chars, got {widths}"


def test_create_career_box_rows_fit_79_column_border(monkeypatch):
    p = vr.Palette(truecolor=False)
    monkeypatch.setattr(vr, "read_line_raw", lambda max_len=16, allowed=None: "TestPilot")
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        callsign = vr.create_career(p, {"handle": "SixteenCharHandl"})
    assert callsign == "TestPilot"
    _assert_box_rows_match_border(buf.getvalue(), "create_career@long-handle")
    stripped = [vr._ANSI_RE.sub("", line) for line in buf.getvalue().split("\r\n") if line.strip()]
    border_lines = [line for line in stripped if line.startswith(("╭", "╰"))]
    assert len(border_lines) == 2
    assert {len(line) for line in border_lines} == {79}


def test_screen_crew_and_galaxy_map_boxes_match_79_columns(monkeypatch):
    world = _world_with_seed(312)
    p = vr.Palette(truecolor=False)

    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    with contextlib.redirect_stdout(io.StringIO()) as output: vr.screen_crew(p, world)
    assert len(output.getvalue().splitlines()) <= 24
    assert all(vr._visible_width(line) <= 80 for line in output.getvalue().splitlines())

    # Spatial map uses ASCII borders bounded to the negotiated width.
    monkeypatch.setattr(vr, "read_key", lambda: "B")
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_galaxy_map(p, world)
    borders = [line for line in output.getvalue().splitlines() if line.startswith("+-")]
    assert len(borders) == 2 and {len(line) for line in borders} == {79}


def test_empty_mission_board_renders_clean_notice_and_back(monkeypatch):
    world = _world_with_seed(313)
    p = vr.Palette(truecolor=False)
    monkeypatch.setattr(vr, "posted_mission_offers", lambda w: [])
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.screen_missions(p, world)
    output = buf.getvalue()
    assert "No contracts currently available" in output
    assert "[B] Back" in output


def test_commission_and_cartel_screens_fit_standard_terminal(monkeypatch, terminal):
    world = _world_with_seed(314)
    terminal(80, 24)
    monkeypatch.setattr(vr, "read_key", lambda: "B")
    monkeypatch.setattr(vr, "confirm", lambda *args: pytest.fail("Browsing opened a confirmation"))
    for screen in (vr.screen_concord_commission, vr.screen_blackwake_made):
        output = io.StringIO()
        with contextlib.redirect_stdout(output): screen(vr.Palette(False), world)
        assert len(output.getvalue().splitlines()) <= 24
        assert all(vr._visible_width(row) <= 80 for row in output.getvalue().splitlines())
        assert "Back" in output.getvalue() and "Standing:" in output.getvalue()


def test_status_bar_separator_is_79_columns():
    world = _world_with_seed(315)
    p = vr.Palette(truecolor=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vr.draw_status_bar(p, world)
    stripped = [vr._ANSI_RE.sub("", line) for line in buf.getvalue().split("\r\n") if line.strip()]
    rule_line = [line for line in stripped if line.startswith("─") and set(line) == {"─"}]
    assert len(rule_line) == 1
    assert len(rule_line[0]) == 79


def _use_decoded_input(monkeypatch, data):
    stream = io.BytesIO(data)
    reader = vr._DoorInput(lambda timeout: stream.read(1))
    monkeypatch.setattr(vr, "read_key", reader.read_key)


def test_unicode_line_editing_erases_wide_and_combining_characters(monkeypatch):
    _use_decoded_input(monkeypatch, "界e\u0301\x7f\x08Jörg\r\n7\r".encode("utf-8"))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        name = vr.read_line_raw(16, allowed=lambda c: c.isalnum() or bool(vr.unicodedata.combining(c)))
        quantity = vr.read_line_raw(5)
    assert name == "Jörg"
    assert quantity == "7"  # CRLF submits once, not an empty next field
    assert "\x08 \x08" * 3 in output.getvalue()


def test_numeric_input_rejects_unicode_digits_that_int_cannot_parse(monkeypatch):
    _use_decoded_input(monkeypatch, "²Ⅳ١３5\r".encode("utf-8"))
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.read_line_raw(5) == "5"


def test_text_input_bounds_columns_and_normalizes_name(monkeypatch):
    _use_decoded_input(monkeypatch, "界界界\re\u0301\r".encode("utf-8"))
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.read_line_raw(4, allowed=str.isalnum) == "界界"
        assert vr.read_line_raw(4, allowed=lambda c: True) == "é"


def test_escape_and_paste_cannot_confirm_action(monkeypatch):
    _use_decoded_input(monkeypatch, b"\x1bY\x1b[200~Y\x1b[201~\x1b[1;5YN")
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.confirm("Launch?", vr.Palette(False)) is False


def test_arrow_keys_cannot_accept_missions(monkeypatch):
    world = _world_with_seed(42)
    _use_decoded_input(monkeypatch, b"\x1b[A\x1bOBQ")
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_missions(vr.Palette(False), world)
    assert not world.save.active_missions


def test_unsupported_keys_do_not_acknowledge_result_pause(monkeypatch):
    _use_decoded_input(monkeypatch, b"\x1b[A\x1b[200~Y\x1b[201~KN")
    with contextlib.redirect_stdout(io.StringIO()):
        vr.pause(vr.Palette(False))
    assert vr.read_key() == "N"


@pytest.mark.parametrize("text", ["\u017f", "\u0131", "\u00df"])
def test_unicode_cannot_alias_ascii_commands(monkeypatch, text):
    _use_decoded_input(monkeypatch, (text + "s").encode("utf-8"))
    assert vr.read_command() == vr.IGNORED_KEY
    assert vr.read_command() == "S"


@pytest.mark.parametrize("command", [b"P", b"X", b"p", b"x"])
def test_standalone_escape_does_not_capture_later_hotkeys(command):
    events = iter([b"\x1b", None, command, b"Q"])
    reader = vr._DoorInput(lambda timeout: next(events))
    assert reader.read_key() == vr.ESCAPE_KEY
    assert reader.read_key() == command.decode("ascii")
    assert reader.read_key() == "Q"


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
@pytest.mark.parametrize("screen", ["guide", "offer"])
def test_opening_guide_and_offer_pages_fit_and_browsing_is_read_only(monkeypatch, terminal, width, height, screen):
    import copy
    world = _world_with_seed(42)
    world.checkpoint()
    before = copy.deepcopy(world.save.to_dict())
    terminal(width, height)
    output = io.StringIO()
    pages = []
    guide_count = len(vr._mission_text_pages(vr.pilot_guide_lines(world), overhead=5))

    def choose():
        value = output.getvalue()
        pages.append(value)
        output.seek(0)
        output.truncate(0)
        assert world.save.to_dict() == before
        assert len(pages) <= 100
        return "B" if ("[A] Accept" in value if screen == "offer" else len(pages) == guide_count) else "N"

    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output):
        if screen == "guide":
            vr.screen_pilot_guide(vr.Palette(False), world)
        else:
            vr._screen_opening_offer(vr.Palette(False), world, vr.opening_assignment_offer(world))
    assert world.save.to_dict() == before
    for page in pages:
        rows = page.splitlines()
        assert len(rows) <= height, (width, height, rows)
        assert all(vr._visible_width(row) <= width for row in rows)
    if screen == "offer":
        assert all("[A] Accept" not in page for page in pages[:-1])


@pytest.mark.parametrize("style", [None, True, [], {}, "unknown"])
def test_invalid_display_preference_preserves_career(tmp_path, style):
    import json
    document = _world_with_seed(42).save.to_dict()
    document["display_style"] = style
    path = tmp_path / "77.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(vr.ResumeError):
        vr.load_or_create_save(tmp_path, 77, "Tester")
    assert path.read_bytes() == before


@pytest.mark.parametrize("style", list(vr.DISPLAY_STYLES))
@pytest.mark.parametrize("truecolor", [True, False])
def test_display_output_color_and_art_keep_unicode_letters(monkeypatch, style, truecolor):
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", style)
    p = vr.Palette(truecolor)
    artwork = "".join(map(chr, vr._ASCII_ART_TRANSLATION))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.out(p.accent + artwork + " Caf\u00e9 \u754c " + vr.BOLD + "LOW FUEL" + vr.RESET)
    text = output.getvalue()
    assert "Caf\u00e9 \u754c" in text and "LOW FUEL" in text
    assert vr._visible_width(text) == vr._visible_width(artwork + " Caf\u00e9 \u754c LOW FUEL")
    if style in ("mono", "plain"):
        assert "\x1b" not in text
    elif style == "basic":
        assert "\x1b[" in text and "38;" not in text
    else:
        assert ("38;2;" if truecolor else "38;5;") in text
    if style == "plain":
        assert text.startswith(artwork.translate(vr._ASCII_ART_TRANSLATION))
        assert artwork.translate(vr._ASCII_ART_TRANSLATION).isascii()
    else:
        assert artwork in text


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
def test_display_options_paging_and_back_write_nothing(monkeypatch, terminal, width, height):
    import re
    terminal(width, height)
    world = _world_with_seed(42)
    before = world.save.to_dict()
    world._checkpoint = lambda current: pytest.fail("Browsing saved a preference")
    output = io.StringIO()
    frames = []
    def choose():
        frame = output.getvalue()
        output.seek(0); output.truncate(0)
        frames.append(frame)
        assert len(frame.splitlines()) <= height
        assert all(vr._visible_width(line) <= width for line in frame.splitlines())
        assert "[B] Back:" in " ".join(frame.split())
        page, count = map(int, re.search(r"(\d+)/(\d+)", frame).groups())
        return "B" if page == count else ">"
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(output):
        vr.screen_display_options(vr.Palette(False), world)
    combined = " ".join(" ".join(frames).split())
    for word in ("Full palette", "16-color", "Monochrome", "Plain", "UTF-8"):
        assert word in combined
    assert world.save.to_dict() == before


def test_plain_display_keeps_utf8_input_and_wide_backspace(monkeypatch):
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "plain")
    _use_decoded_input(monkeypatch, "Caf\u00e9\u754c\x7f\r".encode("utf-8"))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        assert vr.read_line_raw(20, allowed=str.isalnum) == "Caf\u00e9"
    assert "Caf\u00e9\u754c" in output.getvalue()
    assert "\b \b\b \b" in output.getvalue()


@pytest.mark.parametrize("width,height", [(20,10),(40,12),(80,24)])
@pytest.mark.parametrize("style", list(vr.DISPLAY_STYLES))
def test_viewport_visits_every_view_and_page_without_writes(monkeypatch, terminal,width,height,style):
    import copy,re
    world=_world_with_seed(42); world.save.current_system=world.landmark["system_id"]
    before=copy.deepcopy(world.save.to_dict()); rng=world.event_rng.getstate()
    world._checkpoint=lambda w:pytest.fail("Viewport wrote a save")
    terminal(width, height, style)
    output=io.StringIO(); frames=[]; view=1
    def choose():
        nonlocal view
        frame=output.getvalue(); output.seek(0); output.truncate(0); frames.append(frame)
        assert len(frame.splitlines())<=height and all(vr._visible_width(row)<=width for row in frame.splitlines())
        page,count=map(int,re.search(r"(\d+)/(\d+)",vr._ANSI_RE.sub("",frame)).groups())
        if page<count:return ">"
        view+=1
        return str(view) if view<=3 else "B"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):vr.screen_viewport(vr.Palette(False),world)
    text=" ".join(vr._ANSI_RE.sub(""," ".join(frames)).split())
    assert "Views:" in text and world.here.station_name in text and "Discovery" in text
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng


@pytest.mark.parametrize("width,height", [(20,10),(40,12),(80,24)])
def test_refit_portrait_preview_pages_keep_terms_and_back_changes_nothing(monkeypatch, terminal,width,height):
    import copy,re
    world=_world_with_seed(42); world.save.pilot.credits=20000; world.save.ship.fuel=7
    before=copy.deepcopy(world.save.to_dict()); rng=world.event_rng.getstate()
    terminal(width, height)
    world._checkpoint=lambda w:pytest.fail("Preview checkpointed")
    monkeypatch.setattr(vr,"confirm",lambda *args:pytest.fail("Preview asked for confirmation"))
    output=io.StringIO(); frames=[]
    def choose():
        frame=output.getvalue(); output.seek(0); output.truncate(0); frames.append(frame)
        assert len(frame.splitlines())<=height and all(vr._visible_width(row)<=width for row in frame.splitlines())
        page,count=map(int,re.search(r"(\d+)/(\d+)",vr._ANSI_RE.sub("",frame)).groups())
        return ">" if page<count else "B"
    monkeypatch.setattr(vr,"read_key",choose)
    with contextlib.redirect_stdout(output):assert vr._hull_refit_screen(vr.Palette(False),world,"Freighter",15000) is None
    text=" ".join(vr._ANSI_RE.sub(""," ".join(frames)).split())
    for term in ("15,000cr", "20,000cr", "Fuel stays at 7", "does not fill", "cannot be reversed"):
        assert term in text
    assert world.save.to_dict()==before and world.event_rng.getstate()==rng


@pytest.mark.parametrize("seed", [-1,-(2**63)])
@pytest.mark.parametrize("retired", [False,True])
def test_achievement_signed_seed_survives_ranking_restart_and_unchanged_checkpoint(tmp_path,monkeypatch,seed,retired):
    save=_finale_world("combat").save;save.seed=seed
    save.trading_ledger.sales_revenue=5000;save.trading_ledger.since_day=0
    world=vr.World(save);world._checkpoint=lambda w:vr.persist(w,tmp_path,77);world.checkpoint()
    if retired:world.reset(vr.finish_career(world.save,"combat"));world.checkpoint()
    entries=vr._load_score_records(tmp_path)
    for category in ("trading","exploration","combat"):
        assert any(row["seed"]==seed for row in vr.achievement_ranking(entries,category))
    saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
    restored=vr.World(saved);restored._checkpoint=lambda w:vr.persist(w,tmp_path,77)
    monkeypatch.setattr(vr.os,"replace",lambda *args:pytest.fail("Unchanged signed-seed score rewritten"))
    restored.checkpoint()


@pytest.mark.parametrize("version", [None,False,True,"1",1.0,0,-1,"missing"])
def test_achievement_malformed_version_repairs_from_authoritative_save(tmp_path,version):
    import json
    world=_world_with_seed(42);world._checkpoint=lambda w:vr.persist(w,tmp_path,77);world.checkpoint()
    path=tmp_path/"scores"/"77.json";data=json.loads(path.read_text(encoding="utf-8"))
    if version=="missing":data["achievements"].pop("version")
    else:data["achievements"]["version"]=version
    path.write_text(json.dumps(data),encoding="utf-8")
    assert "achievements" not in vr._load_score_records(tmp_path)[0]
    world.checkpoint()
    assert vr._load_score_records(tmp_path)[0]["achievements"]==vr.score_achievements(world.save)
    assert type(json.loads(path.read_text(encoding="utf-8"))["achievements"]["version"]) is int


@pytest.mark.parametrize("source,total,current", [("imported",9,0),("score",9,0),("imported",9,12)])
def test_achievement_retirement_totals_survive_a_projection_restart_and_the_next_finale(tmp_path,source,total,current):
    import json
    record={"user_id":77,"handle":"Older","best_credits":10000,"retirements":total,"kills":80}
    path=tmp_path/"leaderboard.json" if source=="imported" else tmp_path/"scores"/"77.json"
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps([record] if source=="imported" else record),encoding="utf-8")
    if source=="imported": vr.import_hall_of_fame(tmp_path)  # the old file gets there once, at launch (#421)
    world=_finale_world();world.save.pilot.retirements=current;world._checkpoint=lambda w:vr.persist(w,tmp_path,77);world.checkpoint()
    expected=max(total,current);saved,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
    assert saved.pilot.retirements==expected and saved.retired_careers==[] and saved.pilot.credits==1200
    entries=vr._load_score_records(tmp_path);assert vr.achievement_ranking(entries,"careers")[0]["retirements"]==expected
    assert entries[0]["achievements"]["careers"][0]["number"]==expected+1
    assert vr.achievement_ranking(entries,"combat")==[]
    world=vr.World(saved,checkpoint=lambda w:vr.persist(w,tmp_path,77));world.reset(vr.finish_career(world.save,"legend"));world.checkpoint()
    again,_,_=vr.load_or_create_save(tmp_path,77,"Tester")
    assert again.pilot.retirements==expected+1 and again.retired_careers[0]["number"]==expected+1
    assert vr._load_score_records(tmp_path)[0]["achievements"]["careers"][-1]["number"]==expected+2


def test_achievement_readonly_snapshot_preserves_an_imported_total_before_projection_repair(tmp_path):
    import json
    world=_world_with_seed(42);world._checkpoint=lambda w:vr.persist(w,tmp_path,77);world.checkpoint()
    older=tmp_path/"leaderboard.json";older.write_text(json.dumps([{"user_id":77,"handle":"Older","best_credits":10000,"retirements":9}]),encoding="utf-8")
    vr.import_hall_of_fame(tmp_path)
    paths=[older,tmp_path/"77.json",tmp_path/"scores"/"77.json"];before={p:p.read_bytes() for p in paths}
    entries=vr._load_score_records(tmp_path)
    assert vr.achievement_ranking(entries,"careers")[0]["retirements"]==9 and "achievements" not in entries[0]
    assert {p:p.read_bytes() for p in paths}==before
    world.checkpoint();entry=vr._load_score_records(tmp_path)[0]
    assert entry["retirements"]==9 and entry["achievements"]["careers"][-1]["number"]==10


def test_retirement_forfeits_the_commissioned_reward_the_contract_showed():
    """The dossier quotes what the contract was worth, commission included."""
    world = _finale_world("legend")
    target = sorted(world.here.connections)[0]
    world.save.active_missions = [vr.Mission(4, "bounty", "Intercept raider", 1000, 0, target, pirate_tier=1)]
    world.save.pilot.has_concord_commission = True
    world.save.pilot.reputation[vr.FACTION_CONCORD] = 100
    quoted = vr.bounty_reward_for(world, 1000)
    assert quoted == 1250  # the commission the contract screen and every loss path show
    assert f"forfeited {quoted:,}cr" in " ".join(vr.finish_career(world.save, "legend").pilot.log)
    world.save.pilot.has_concord_commission = False
    assert "forfeited 1,000cr" in " ".join(vr.finish_career(world.save, "legend").pilot.log)
    world.save.active_missions = [vr.Mission(5, "delivery", "Deliver goods", 1000, 0, target, commodity="food", quantity=2)]
    world.save.pilot.has_concord_commission = True
    assert "forfeited 1,000cr" in " ".join(vr.finish_career(world.save, "legend").pilot.log)  # deliveries carry no commission


def test_absent_loss_counters_default_to_zero_and_the_record_shows_them():
    world = _world_with_seed(42)
    data = world.save.to_dict()
    for key in ("missions_failed", "missions_expired"): data["pilot"].pop(key)
    restored = vr.SaveData.from_dict(data)
    assert restored.pilot.missions_failed == 0 and restored.pilot.missions_expired == 0
    data["pilot"]["missions_failed"] = -1
    with pytest.raises(vr.ResumeError): vr.SaveData.from_dict(data)
    world.save.pilot.missions_completed, world.save.pilot.missions_failed, world.save.pilot.missions_expired = 4, 2, 1
    text = " ".join(vr.pilot_record_lines(world))
    assert "Missions completed: 4; failed or abandoned: 2; expired: 1." in text
    assert vr.career_accomplishments(world.save)["failed"] == 2


def test_dossiers_record_losses_and_older_dossiers_still_load():
    world = _world_with_seed(42)
    world.save.pilot.missions_completed, world.save.pilot.missions_failed, world.save.pilot.missions_expired = 3, 2, 1
    world.save.pilot.kills = 50
    fresh = vr.finish_career(world.save, "combat")
    dossier = fresh.retired_careers[-1]
    assert dossier["failed"] == 2 and dossier["expired"] == 1
    assert "3 missions completed, 2 failed, 1 expired." in " ".join(vr.career_dossier_lines(fresh))
    data = fresh.to_dict()
    for key in ("failed", "expired"): data["retired_careers"][0].pop(key)
    old = vr.SaveData.from_dict(data)
    assert "missions completed." in " ".join(vr.career_dossier_lines(old))
    data["retired_careers"][0]["failed"] = 2  # half of the pair is malformed
    with pytest.raises(vr.ResumeError): vr.SaveData.from_dict(data)


@pytest.mark.parametrize("width,labelled", [(20, False), (39, False), (40, True), (80, True)])
def test_the_combat_bar_keeps_its_labels_until_the_page_cannot_afford_them(monkeypatch, width, labelled):
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
    bar = vr.combat_action_bar("F/E/D/P")
    assert ("[F] Fire" in bar) == labelled
    assert ("[F/E/D/P] Act" in bar) != labelled
    assert "[I] Info" in bar and "[<>] Page: " in bar


def test_combat_lines_name_the_escort_at_stake_only_during_escort_fights():
    world = _world_with_seed(42); pirate = vr.Pirate("Opponent", 1, 50, 50)
    world.save.pilot.credits = 10_000
    plain = " ".join(vr.combat_display_lines(world, pirate, [], patrol=False, tactics=vr.new_tactics(pirate)))
    assert "fails the escort contract" not in plain
    world, mission = _escort_world("escaped"); world.save.pilot.credits = 10_000
    _set_cargo(world, {"food": 1})  # Dump is only offered with something to dump (#414)
    lines = vr.combat_display_lines(world, pirate, [], patrol=False, tactics=vr.new_tactics(pirate))
    evade = next(row for row in lines if row.startswith("[E]")); dump = next(row for row in lines if row.startswith("[D]"))
    bribe = next(row for row in lines if row.startswith("[P]"))
    assert evade.endswith("Escaping fails the escort contract.") and dump.endswith("Escaping fails the escort contract.")
    assert bribe.endswith("An accepted bribe fails the escort contract.")


@pytest.mark.parametrize("paid_jumps,expected", [(0, 3), (5, 3), (15, 2), (30, 2)])
def test_engineer_discounts_repairs_by_service_level(paid_jumps, expected):
    world = _world_with_named_crew("engineer", paid_jumps)
    assert vr.repair_cost_per_hp(world.save.ship) == expected
    world.save.ship.has_engineer = False
    assert vr.repair_cost_per_hp(world.save.ship) == 4


def test_repair_screen_charges_the_discounted_rate_and_yard_shows_it(monkeypatch):
    world = _world_with_named_crew("engineer", 15)
    world.save.ship.hull_hp = vr.hull_hp_max(world.save.ship) - 10; world.save.pilot.credits = 1_000
    monkeypatch.setattr(vr, "confirm", lambda prompt, p: "for 20cr" in prompt)
    with contextlib.redirect_stdout(io.StringIO()):
        assert "for 20cr" in vr._repair(vr.Palette(False), world)
    assert world.save.pilot.credits == 980 and world.save.ship.hull_hp == vr.hull_hp_max(world.save.ship)
    assert "at 2cr/HP" in " ".join(vr.shipyard_lines(world)) and "discounts repairs" in " ".join(vr.shipyard_lines(world))
    assert "repairs 2cr/HP instead of 4" in vr.crew_effect("engineer", 2)


def _deck_page(world, monkeypatch, terminal, width=80, height=24):
    terminal(width, height)
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    with contextlib.redirect_stdout(io.StringIO()) as output:
        assert vr.screen_station_menu(vr.Palette(False), world) == "Q"
    return vr._ANSI_RE.sub("", output.getvalue())


def test_arrival_narration_is_retained_as_deck_results_then_cleared(monkeypatch, terminal):
    world = _world_with_seed(42); world.save.ship.fuel = 99
    dest = sorted(world.here.connections)[0]; world.by_id[dest].discovered = False; world.sync_discovered()
    monkeypatch.setattr(world.event_rng, "random", lambda: 0.99)  # no encounter, no inspection
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, dest)
    assert f"New system charted: {world.by_id[dest].name}." in world.hop_report
    page = _deck_page(world, monkeypatch, terminal)
    assert f"Result: Jumping to the unknown..." in page and f"Result: New system charted: {world.by_id[dest].name}." in page
    assert world.hop_report == [] and "Result:" not in _deck_page(world, monkeypatch, terminal)


def test_encounter_and_combat_outcomes_join_the_hop_report(monkeypatch):
    world, mission = _escort_world("won")
    monkeypatch.setattr(vr, "screen_combat", lambda p, w, pirate: "won")
    with contextlib.redirect_stdout(io.StringIO()):
        vr._resolve_escort_missions(vr.Palette(False), world, mission.target_system)
    assert any(line.startswith("Convoy delivered safely! +") for line in world.hop_report)
    world = _world_with_seed(42); state = {}
    world.save.pending_travel = {"version": 1, "origin": 0, "destination": 1, "was_discovered": True, "destroyed": False,
        "phase": "primary", "primary": "random", "bounty": None, "escorts": [], "escort_index": 0, "encounter": state}
    with contextlib.redirect_stdout(io.StringIO()):
        vr._encounter_result(vr.Palette(False), world, state, ["Salvage recovered: 90cr."])
    assert world.hop_report == ["Salvage recovered: 90cr."]
    with contextlib.redirect_stdout(io.StringIO()):
        vr._resolve_random_travel_encounter(vr.Palette(False), world, world.by_id[1])  # replay after a restart
    assert world.hop_report == ["Salvage recovered: 90cr."] * 2


def test_hop_report_is_bounded_and_the_deck_still_fits(monkeypatch, terminal):
    world = _world_with_seed(42)
    vr.report_hop(world, [f"line {i}" for i in range(20)])
    assert world.hop_report == [f"line {i}" for i in range(12, 20)]
    vr.report_hop(world, [f"line {i}" for i in range(20)])
    for width, height in ((80, 24), (40, 24)):
        page = _deck_page(world, monkeypatch, terminal, width, height)
        rows = [row for row in page.split("\r\n") if row]
        assert len(rows) <= height and all(vr._visible_width(row) <= width for row in rows)
        vr.report_hop(world, [f"line {i}" for i in range(20)])


def test_keyed_rows_prefix_only_the_first_row():
    assert vr.keyed_rows("B", ["Mirrorfall (76,4); Industrial;", "Danger 1; 1 fuel TRACKED NEXT."]) == \
        ["[B] Mirrorfall (76,4); Industrial;", "    Danger 1; 1 fuel TRACKED NEXT."]
    assert vr.keyed_rows("3", ["single"]) == ["[3] single"] and vr.keyed_rows("A", []) == []


def test_chart_continuations_are_indented_and_still_selectable(monkeypatch, terminal):
    terminal(40, 24)
    world = _world_with_seed(42); world.save.ship.fuel = 99
    for sid in world.here.connections: world.by_id[sid].discovered = True
    pages = vr._chart_pages(world, "Navigation", "[B] Back: ", None)
    rows = [row for page in pages for row in page[0]]
    keyed = [row for row in rows if row.startswith("[") and row[1] in vr.CHART_CONNECTION_LETTERS]
    assert len(keyed) == len(world.here.connections)  # one key per destination, however many rows it wraps to
    assert any(row.startswith("    ") for row in rows)  # at least one entry wrapped at 40 columns
    key = vr.CHART_CONNECTION_LETTERS[0]
    keys = iter([key, "Y"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()):
        assert vr.screen_chart(vr.Palette(False), world) == sorted(world.here.connections)[0]


def test_mission_board_and_picker_continuations_carry_one_key(monkeypatch, terminal):
    terminal(30, 24)
    world, mission = _mission_details_world()
    monkeypatch.setattr(vr, "read_key", lambda: "B")
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_missions(vr.Palette(False), world)
    rows = vr._ANSI_RE.sub("", output.getvalue()).split("\r\n")
    assert sum(row.startswith("[1] OFFER") for row in rows) == 1 and any(row.startswith("    ") for row in rows)
    keys = iter(["B"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr._pick_trade_field("Pick", [("x", "a very long option label that certainly wraps at thirty columns wide")])
    rows = vr._ANSI_RE.sub("", output.getvalue()).split("\r\n")
    assert sum(row.startswith("[1] a very long") for row in rows) == 1 and any(row.startswith("    ") for row in rows)


def test_single_page_footers_drop_paging_tokens_but_keep_the_counter(monkeypatch, terminal):
    assert vr.single_page_footer("[<] Prev [>] Next [R] Refuel [B] Back: ", 1) == "[R] Refuel [B] Back: "
    assert vr.single_page_footer("[<>] Page [B] Back: ", 1) == "[B] Back: " and vr.single_page_footer("[<>] Page [B] Back: ", 2) == "[<>] Page [B] Back: "
    world = _world_with_seed(42)
    terminal(80, 60)
    monkeypatch.setattr(vr, "read_key", lambda: "B")
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_shipyard(vr.Palette(False), world)
    plain = " ".join(vr._ANSI_RE.sub("", output.getvalue()).split())
    assert " 1/1 " in plain and "[<] Prev" not in plain and "[R] Refuel [P] Repair" in plain


def test_dump_is_absent_and_harmless_with_an_empty_hold(monkeypatch):
    world, pirate = _world_with_pending_fight()
    world.save.pilot.credits = 0
    lines = vr.combat_display_lines(world, pirate, [], patrol=False, tactics=vr.new_tactics(pirate))
    assert not any(line.startswith("[D]") for line in lines)
    before = world.save.to_dict(); rng = world.event_rng.getstate()
    keys = iter(["D"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    frames = []
    def choose():
        try: return next(keys)
        except StopIteration:
            assert world.save.to_dict() == before and world.event_rng.getstate() == rng
            raise EOFError
    monkeypatch.setattr(vr, "read_key", choose)
    with contextlib.redirect_stdout(io.StringIO()) as output, pytest.raises(EOFError):
        vr._screen_combat_session(vr.Palette(False), world, pirate, patrol=False)
    assert "[D] Dump" not in output.getvalue()


def test_plural_reads_as_prose():
    assert vr.plural(1, "jump") == "1 jump" and vr.plural(2, "jump") == "2 jumps" and vr.plural(0, "pilot") == "0 pilots"


def test_accepting_first_flight_returns_to_the_deck_with_the_next_step(monkeypatch, terminal):
    world = _world_with_seed(42)
    offer = vr.opening_assignment_offer(world); assert offer is not None
    pages = len(vr._mission_text_pages(["x"] * 40, overhead=5))  # plenty of N presses reach the last page
    keys = iter(["O"] + ["N"] * 40 + ["A"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    monkeypatch.setattr(vr, "pause", lambda p: pytest.fail("no acknowledgement pause on acceptance"))
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_pilot_guide(vr.Palette(False), world)  # returns on its own after acceptance
    assert world.save.active_missions and world.save.active_missions[0].opening_assignment
    assert any(line.startswith("First Flight accepted and tracked. Next: [M] Market") for line in world.hop_report)
    page = _deck_page(world, monkeypatch, terminal)
    assert "Result: First Flight accepted and tracked." in page


def test_chart_route_planner_opens_the_destination_picker_first(monkeypatch):
    world = _world_with_seed(42)
    picked = []
    monkeypatch.setattr(vr, "_pick_trade_field", lambda title, choices, **kw: picked.append(title) or None)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_auto_route(vr.Palette(False), world)
    assert picked == ["Charted Destination"] and "Route Planner" not in output.getvalue()
    dest = sorted(world.here.connections)[0]; world.by_id[dest].discovered = True
    monkeypatch.setattr(vr, "_pick_trade_field", lambda title, choices, **kw: dest)
    keys = iter(["B"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_auto_route(vr.Palette(False), world)
    assert "Route Planner" in output.getvalue() and "[J] Jump next" in output.getvalue()


def _world_with_a_trade_lead():
    """Remember every neighbour, not just one: Freeport's first connection shares its
    economy, so a two-station fixture has identical prices and no lead to plan from."""
    world = _world_with_seed(42)
    for sid in world.by_id[0].connections:
        world.by_id[sid].discovered = True
        world.save.current_system = sid
        vr.remember_local_market(world)
    world.save.current_system = 0
    vr.remember_local_market(world)
    world.save.discovered = [system.id for system in world.galaxy if system.discovered]
    return world


def test_ledger_route_draft_starts_from_the_best_lead(monkeypatch):
    world = _world_with_a_trade_lead()
    leads = vr.trade_opportunities(world)
    assert leads, "a differing neighbour economy should offer a lead"
    keys = iter(["B"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_trade_route(vr.Palette(False), world)
    plain = " ".join(vr._ANSI_RE.sub("", output.getvalue()).split())
    assert f"{vr.COMMODITIES[leads[0]['commodity']]['label']} x{leads[0]['quantity']}" in plain
    assert "Food x1" not in plain or leads[0]["commodity"] == "food" and leads[0]["quantity"] == 1
    keys = iter(["B"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_trading_ledger(vr.Palette(False), world)
    assert "[O] Opportunities [R] Route [M] Markets" in output.getvalue()


@pytest.mark.parametrize("screen,footer", [("screen_chart", "[G] Route planner"),
                                           ("screen_missions", "[B] Back"),
                                           ("screen_trading_ledger", "[O] Opportunities")])
def test_idle_keys_are_absorbed_on_hand_rolled_action_bars_too(monkeypatch, terminal, screen, footer):
    """The rule holds on every action bar, not only the service pages (#416)."""
    world = _world_with_seed(42)
    keys = iter([" ", vr.IGNORED_KEY, "\t", "B"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    terminal(80, 24)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        getattr(vr, screen)(vr.Palette(False), world)
    assert vr._ANSI_RE.sub("", output.getvalue()).count(footer) == 1


def test_idle_keys_at_an_action_bar_do_not_reprint_the_page(monkeypatch, terminal):
    world = _world_with_seed(42)
    keys = iter([" ", vr.IGNORED_KEY, "\t", "Q"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    terminal(80, 24)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        assert vr.screen_station_menu(vr.Palette(False), world) == "Q"
    text = vr._ANSI_RE.sub("", output.getvalue())
    assert text.count("Command Deck:") == 1 and text.count("[Q] Exit:") == 1
    keys = iter(["?", "Q"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_station_menu(vr.Palette(False), world)
    assert vr._ANSI_RE.sub("", output.getvalue()).count("Command Deck:") == 2  # an unknown hotkey still redraws


def test_wording_uses_singular_forms_and_names_the_offer_refresh():
    assert vr.hall_of_fame_lines([{"user_id": 1, "handle": "A"}], 1)[0].startswith("Top 1 pilot by")
    assert vr.achievement_lines([], "combat", 1)[0].startswith("Top 0 local careers by")
    world, mission = _mission_details_world()
    world.checkpoint()
    posted = world.save.mission_boards.get(world.save.current_system)
    assert posted is not None
    lines = []
    import re
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.read_key = lambda: "B"
        vr.screen_missions(vr.Palette(False), world)
    assert re.search(r"New offers on day \d+", output.getvalue()) and "Refresh day" not in output.getvalue()


@pytest.mark.parametrize("footer,expected", [
    ("[<] Prev [>] Next [X] Expand [Q] Exit: ", "[X] Expand [Q] Exit: "),
    ("[S] Story [B] Back [<>] Page: ", "[S] Story [B] Back: "),
    ("[<>] Page [O/C/H/D] View [R] Finale [B] Back: ", "[O/C/H/D] View [R] Finale [B] Back: "),
    ("[F] Fire [E] Evade [I] Info [<>] Page: ", "[F] Fire [E] Evade [I] Info: "),
    ("[R] Route [N] Next [P] Prev [B] Back", "[R] Route [B] Back"),
    ("[1-9] Details [N] Next [P] Prev [B] Back > ", "[1-9] Details [B] Back > "),
    ("[A] Accept [N] Next [P] Prev [B] Back: ", "[A] Accept [B] Back: "),
    ("[1-5] View [N] Next [P] Prev [B] Back: ", "[1-5] View [B] Back: "),  # the Hall of Fame spelling
    ("[E] Edit draft [B] Back: ", "[E] Edit draft [B] Back: "),
])
def test_single_page_footers_drop_every_spelling_of_the_paging_tokens(footer, expected):
    """The literal table missed every colon-terminated bar, which is most of them."""
    assert vr.single_page_footer(footer, 1) == expected
    assert vr.single_page_footer(footer, 2) == footer
    assert "[<" not in vr.single_page_footer(footer, 1)
    assert "  " not in vr.single_page_footer(footer, 1) and " :" not in vr.single_page_footer(footer, 1)


def test_a_screen_that_fits_without_paging_tokens_is_one_page(monkeypatch, terminal):
    """Counting pages against the longer footer split screens that fit (#412 review):
    at 40 columns the market's own bar wraps to two rows and its shortened form to
    one, so the page that was two rows short of fitting now fits."""
    terminal(40, 24)
    footer = "[<] Prev [>] Next [A,C-J] Trade [X] Futures [B] Back: "
    rows = lambda text: len(vr._wrap_output(text, 39).split("\r\n"))
    assert rows(footer) > rows(vr.single_page_footer(footer, 1))
    capacity = len(vr._service_pages(["row"] * 200, "Title", footer)[0])
    assert len(vr._service_pages(["row"] * (capacity + 1), "Title", footer)) == 1
    assert len(vr._service_pages(["row"] * (capacity + 4), "Title", footer)) > 1


def test_contract_details_stop_advertising_paging_on_a_single_page(monkeypatch, terminal):
    terminal(80, 200)
    world, mission = _mission_details_world()
    keys = iter(["B"]); monkeypatch.setattr(vr, "read_key", lambda: next(keys))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr.screen_mission_details(vr.Palette(False), world, mission, active=False)
    text = vr._ANSI_RE.sub("", output.getvalue())
    assert "Contract #" in text and "1/1" in text
    assert "[N] Next" not in text and "[P] Prev" not in text
    assert "[A] Accept contract" in text and "[R] Route" in text


def test_a_commit_does_not_reroll_the_board_or_repair_ids():
    """A nested menu saving its own action must not run turn processing (#417)."""
    import copy
    world = _world_with_seed(42)
    world.checkpoint()
    board = copy.deepcopy(world.save.mission_boards)
    world.save.mission_boards[world.save.current_system]["offers"] = []
    world.save.turn += 3  # the board is stale: only a turn boundary may replace it
    world.commit()
    assert world.save.mission_boards[world.save.current_system]["offers"] == []
    world.advance_station_state()
    assert world.save.mission_boards[world.save.current_system]["offers"]
    assert world.save.mission_boards.keys() == board.keys()


def test_turn_processing_writes_nothing_and_committing_writes_once():
    world = _world_with_seed(42)
    writes = []
    world._checkpoint = lambda current: writes.append(current.save.turn)
    world.advance_station_state()
    assert writes == []  # pure domain work
    world.commit()
    assert writes == [world.save.turn]
    world.checkpoint()
    assert len(writes) == 2  # a turn boundary is exactly one of each


def test_a_rank_reached_by_an_action_is_captured_by_its_own_commit():
    """Rank capture belongs to the action, not to the turn: the credits that
    earned it can be spent before the next station tick (#417)."""
    world = _world_with_seed(42)
    world.save.pilot.credits = RANK_THRESHOLD = vr.RANKS[1][0]
    world.commit()
    assert world.save.pilot.highest_rank_seen == 1 and world.pending_promotions
    world.pending_promotions.clear()
    world.save.pilot.credits = 0
    world.commit()
    assert world.save.pilot.highest_rank_seen == 1 and not world.pending_promotions
    assert RANK_THRESHOLD > 0


def test_a_failed_commit_never_announces_the_rank_it_did_not_save():
    world = _world_with_seed(42)
    def refuse(current):
        raise OSError("disk full")
    world._checkpoint = refuse
    world.save.pilot.credits = vr.RANKS[1][0]
    with pytest.raises(vr.SaveError):
        world.commit()
    assert world.pending_promotions == []


def test_a_docked_commit_re_observes_the_market_it_just_traded_in():
    """Buying moves this station's stock and price; the caller watched it move."""
    world = _world_with_seed(42)
    world.checkpoint()
    before = dict(world.save.market_memory[0]["food"])
    vr._consume_market_depth(world, "food", 10, buying=True)  # a purchase moves the pool
    world.commit()
    assert world.save.market_memory[0]["food"]["stock"] == before["stock"] - 10
    world.save.pending_travel = {"version": 1, "origin": 0, "destination": 1, "was_discovered": True,
                                 "destroyed": False, "phase": "primary", "primary": "random",
                                 "bounty": None, "escorts": [], "escort_index": 0, "encounter": {}}
    vr._consume_market_depth(world, "food", 5, buying=True)
    world.commit()  # mid-journey there is no local market to observe
    assert world.save.market_memory[0]["food"]["stock"] == before["stock"] - 10


@pytest.mark.parametrize("key,page,count,expected", [
    (">", 0, 3, 1), (">", 2, 3, 2), (">", 0, 1, 0), (">", 5, 0, 0),
    ("<", 2, 3, 1), ("<", 0, 3, 0), ("B", 1, 3, None), ("", 1, 3, None),
])
def test_page_step_clamps_at_both_ends_and_declines_other_keys(key, page, count, expected):
    assert vr.page_step(key, page, count) == expected


def test_no_screen_lives_above_the_ui_marker():
    """The marker is a boundary: nothing above it may read a key (issue #420)."""
    source = _VOIDRUNNER_PATH.read_text(encoding="utf-8").split("\n")
    marker = next(index for index, line in enumerate(source) if line.startswith("# UI layer"))
    above = [line for line in source[:marker] if line.startswith(("def screen_", "def _screen_"))]
    assert above == []


def test_screen_naming_says_what_a_caller_can_navigate_to():
    """`_screen_*` is a shared body or a sub-step, never a destination (#420)."""
    import inspect
    helpers = {name for name, value in vars(vr).items()
               if name.startswith("_screen_") and inspect.isfunction(value)}
    assert helpers == {"_screen_combat_session", "_screen_faction_contact", "_screen_buy_futures",
                       "_screen_futures_order", "_screen_map_info", "_screen_opening_offer"}
    # Each of those is called only from another screen, never from the outer loop.
    outer = inspect.getsource(vr.main) if hasattr(vr, "main") else ""
    assert not any(name in outer for name in helpers)


@pytest.mark.parametrize("style,keeps_colour,keeps_unicode", [
    ("auto", True, True), ("basic", True, True), ("mono", False, True), ("plain", False, False),
])
def test_apply_display_style_selects_what_reaches_the_terminal(monkeypatch, terminal, style, keeps_colour, keeps_unicode):
    """The presets were covered only indirectly, through one output test (#423)."""
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    vr.apply_display_style(style)
    assert vr._OUTPUT_STYLE == style
    written = io.StringIO()
    with contextlib.redirect_stdout(written):
        vr.out("\x1b[38;5;220mHull \u2588\u2591\u2588\x1b[0m")
    shown = written.getvalue()
    assert ("\x1b[" in shown) == keeps_colour
    assert any(ord(ch) > 127 for ch in shown) == keeps_unicode
    assert "Hull" in shown  # the text itself survives every preset


def test_apply_display_style_refuses_an_unknown_preset(monkeypatch):
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    with pytest.raises(ValueError):
        vr.apply_display_style("sepia")
    assert vr._OUTPUT_STYLE == "auto"


def test_select_display_style_rejects_unknown_presets_and_reports_a_change():
    world = _world_with_seed(42)
    assert vr.select_display_style(world, "mono") is True
    assert vr.select_display_style(world, "mono") is False  # already applied
    assert world.save.display_style == "mono"
    with pytest.raises(ValueError):
        vr.select_display_style(world, "sepia")
    assert world.save.display_style == "mono"
