"""Every sector has a story its docks tell (review).

A sector was a name on the star map. These pin the six stories: that each is
written whole and fits the deck, when each part is heard and in what order,
what the whole of one brings, that the Archive keeps what was heard and says
how the rest is come by, and that a career's progress is a field of the save.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from .support import _world_with_seed, plain, vr


def _dock(world, system_id: int) -> list[str]:
    """Arrive at a station as a hop does: the tale first, then the market is remembered."""
    world.save.current_system = system_id
    world.by_id[system_id].discovered = True
    world.sync_discovered()
    lines = vr.sector_tale(world)
    vr.remember_local_market(world)
    return lines


def _sector_with(world, least: int) -> str:
    """A sector other than Freeport's with at least this many stations."""
    home = vr.sector_for(world.by_id[0])
    return next(name for name in vr.SECTOR_NAMES if name != home and len(vr.sector_stations(world, name)) >= least)


def test_six_stories_each_written_whole_and_fit_for_the_deck():
    assert set(vr.SECTOR_STORIES) == set(vr.SECTOR_NAMES) and len(vr.SECTOR_STORY_AT) == len(vr.SECTOR_NUMERALS) == 3
    room = 2 * (80 - 5) - len("Result: ")
    texts = []
    for sector, (character, parts) in vr.SECTOR_STORIES.items():
        assert len(parts) == 3 and 20 <= len(character) <= 72
        for numeral, part in zip(vr.SECTOR_NUMERALS, parts):
            assert len(f"{sector}, {numeral}: {part}") <= room, part
            assert '"' not in part and part[-1] == "."
        texts += [character, *parts]
    assert len(set(texts)) == len(texts)


@pytest.mark.parametrize("seed", range(40, 46))
def test_a_part_is_heard_at_the_first_third_and_fifth_station_or_as_many_as_there_are(seed):
    world = _world_with_seed(seed)
    for sector in vr.SECTOR_NAMES:
        size = len(vr.sector_stations(world, sector))
        assert vr.sector_story_marks(world, sector) == (min(1, size), min(3, size), min(5, size))


def test_the_parts_come_in_order_one_a_docking_as_the_sector_is_learned():
    world = _world_with_seed(42)
    sector = _sector_with(world, 6)
    stations = [system.id for system in vr.sector_stations(world, sector)]
    parts = vr.SECTOR_STORIES[sector][1]
    heard = [_dock(world, sid) for sid in stations[:6]]
    assert heard[0] == [f"{sector}, I: {parts[0]}"]
    assert heard[1] == []
    assert heard[2] == [f"{sector}, II: {parts[1]}"]
    assert heard[3] == []
    assert heard[4][0] == f"{sector}, III: {parts[2]}"
    assert heard[5] == [] and world.save.tales == {sector: 3}
    # Docking again where the pilot has been tells nothing twice.
    assert _dock(world, stations[0]) == []


def test_a_small_sector_tells_its_story_over_as_many_dockings_as_it_takes():
    """Two stations: the marks are 1, 2, 2 and a docking tells one part, so the
    last waits for the next docking there."""
    world = _world_with_seed(42)
    sector = _sector_with(world, 2)
    keep = [system.id for system in vr.sector_stations(world, sector)][:2]
    for system in vr.sector_stations(world, sector):
        if system.id not in keep:
            system.x, system.y = world.by_id[0].x, world.by_id[0].y  # moved out of the sector
    assert vr.sector_story_marks(world, sector) == (1, 2, 2)
    assert len(_dock(world, keep[0])) == 1 and len(_dock(world, keep[1])) == 1
    assert _dock(world, keep[0])[0].startswith(f"{sector}, III: ")
    assert world.save.tales[sector] == 3


def test_freeports_own_sector_begins_its_story_at_the_first_arrival_there():
    world = _world_with_seed(42)
    vr.remember_local_market(world)  # a career begins docked at Freeport
    home = vr.sector_for(world.by_id[0])
    assert world.save.tales == {}
    neighbour = next(system.id for system in vr.sector_stations(world, home) if system.id)
    lines = _dock(world, neighbour)
    assert lines == [f"{home}, I: {vr.SECTOR_STORIES[home][1][0]}"]
    # And coming home is a docking like any other.
    third = next(system.id for system in vr.sector_stations(world, home) if system.id not in (0, neighbour))
    _dock(world, third)
    assert world.save.tales[home] == 2


def test_the_whole_of_a_story_is_an_achievement_and_puts_the_sectors_markets_on_the_chart():
    world = _world_with_seed(42)
    sector = _sector_with(world, 6)
    stations = [system.id for system in vr.sector_stations(world, sector)]
    for sid in stations[:4]:
        _dock(world, sid)
    assert "tale" not in {accolade.id for accolade in vr.unlock_accolades(world.save)}
    unseen = [sid for sid in stations[5:] if sid in world.traits]
    lines = _dock(world, stations[4])
    assert lines[1].startswith(f"{sector}: that is its story whole, and it is in the Archive.")
    assert all(world.save.flags.get(f"rumour_{sid}") for sid in unseen)
    assert not world.save.flags.get(f"rumour_{stations[4]}")  # the station just docked at needs no rumour
    if unseen:
        assert f"{vr.plural(len(unseen), 'market')} marked on your chart by reputation." in lines[1]
        assert vr.rumoured_trait(world, unseen[0]) == world.traits[unseen[0]]["kind"]
    assert world.save.accolades["counters"]["sector_tales"] == 1
    assert "tale" in {accolade.id for accolade in vr.unlock_accolades(world.save)}
    assert f"Heard the whole story of {sector}." in world.save.pilot.highlights[-1]
    everything = next(a for a in vr.ACCOLADES if a.id == "tales_all")
    assert everything.target == len(vr.SECTOR_NAMES) == 6


def test_a_hop_carries_the_part_to_the_deck():
    world = _world_with_seed(42)
    vr.remember_local_market(world)
    world.save.pilot.credits = 5_000
    world.event_rng.random = lambda: 1.0  # no encounter, no inspection
    target = next(iter(sorted(world.here.connections)))
    with contextlib.redirect_stdout(io.StringIO()):
        vr.screen_travel(vr.Palette(False), world, target)
    sector = vr.sector_for(world.by_id[target])
    assert f"{sector}, I: {vr.SECTOR_STORIES[sector][1][0]}" in world.hop_report
    assert world.save.tales == {sector: 1}


def test_what_was_heard_is_a_field_of_the_save_that_a_new_career_starts_without():
    world = _world_with_seed(42)
    assert "tales" not in world.save.to_dict()
    sector = _sector_with(world, 3)
    _dock(world, vr.sector_stations(world, sector)[0].id)
    record = world.save.to_dict()
    assert record["tales"] == {sector: 1}
    again = vr.SaveData.from_dict(record)
    assert again.tales == {sector: 1} and again.to_dict()["tales"] == {sector: 1}
    for bad in ({"Nowhere": 1}, {sector: 0}, {sector: 4}, {sector: True}, [sector]):
        with pytest.raises(vr.ResumeError):
            vr.SaveData.from_dict({**record, "tales": bad})
    assert vr._new_career("Sim").tales == {}


def test_the_archive_keeps_what_was_heard_and_says_how_the_rest_is_come_by():
    world = _world_with_seed(42)
    sector = _sector_with(world, 6)
    stations = [system.id for system in vr.sector_stations(world, sector)]
    text = " ".join(plain(line) for line in vr.sector_file_lines(world))
    assert text.count("No dock in this sector yet.") == sum(
        1 for name in vr.SECTOR_NAMES if vr.sector_stations(world, name))
    _dock(world, stations[0])
    lines = [plain(line) for line in vr.sector_file_lines(world)]
    character, parts = vr.SECTOR_STORIES[sector]
    assert character in lines and f"I. {parts[0]}" in lines
    assert parts[1] not in " ".join(lines) and parts[2] not in " ".join(lines)
    size = len(stations)
    assert f"II. Heard once you have docked at 2 more stations here (1 of {size} so far)." in lines
    _dock(world, stations[1])
    assert f"II. Heard once you have docked at 1 more station here (2 of {size} so far)." in [
        plain(line) for line in vr.sector_file_lines(world)]
    for sid in stations[2:5]:
        _dock(world, sid)
    lines = [plain(line) for line in vr.sector_file_lines(world)]
    assert all(f"{numeral}. {part}" in lines for numeral, part in zip(vr.SECTOR_NUMERALS, parts))
    assert not any("Heard" in line and sector.upper() in line for line in lines)
    assert any(f"{sector.upper()}  3/3" in line for line in lines)


def test_the_archive_opens_the_files_at_every_stage(monkeypatch):
    world = _world_with_seed(42)
    opened = []
    keys = iter(["F", "B"])
    footers = []

    def page(p, title, lines, footer, number, **_):
        footers.append(footer)
        return next(keys), 0, 1

    monkeypatch.setattr(vr, "_draw_service_page", page)
    monkeypatch.setattr(vr, "screen_sector_files", lambda p, w: opened.append(True))
    vr.screen_archive(vr.Palette(False), world)
    assert opened == [True] and all("[F] Files" in footer for footer in footers)
    assert any("[F] Files" in plain(line) for line in vr.archive_lines(world))
    assert any("[F] Files" in line for line in vr.pilot_guide_lines(world))
