"""A landmark's record leads to a second site (review, v1.3).

A galaxy had one landmark, and exploration had nothing left that was a place
once it was claimed. These pin the second: where it may be, that it cannot be
found before the first site's record has given its bearing, that the bearing is
said, kept in view and flyable, what the second pays and says, and that all of
it is one more flag in a save an older build still loads.
"""

from __future__ import annotations

import pytest

from .support import _world_with_seed, plain, vr


def _at(world, system_id: int):
    world.save.current_system = system_id
    return world


@pytest.mark.parametrize("seed", range(40, 70))
def test_the_far_site_is_another_kind_of_place_somewhere_else(seed):
    world = _world_with_seed(seed)
    first, far = world.landmark, world.far_site
    assert far["system_id"] not in (0, first["system_id"])
    assert far["label"] != first["label"] and far["label"] in {flavor["label"] for flavor in vr.LANDMARK_FLAVORS}
    assert vr.sector_for(world.by_id[far["system_id"]]) != vr.sector_for(world.by_id[first["system_id"]])
    assert far["reward_credits"] == vr.FAR_SITE_REWARD > first["reward_credits"]
    # Its own generator: the same for a seed, and the first site is where it always was.
    assert vr.World(vr.SaveData.from_dict(world.save.to_dict())).far_site == far
    assert first == vr.generate_landmark(seed, world.galaxy)


def test_the_far_site_is_not_there_until_the_first_record_gives_its_bearing():
    world = _at(_world_with_seed(42), 0)
    far = world.far_site["system_id"]
    assert vr.far_site_bearing(world) is None and vr.far_site_lead(world) is None
    _at(world, far)
    assert vr.landmark_here(world) is None and not vr.landmark_available_here(world)
    assert ("L", world.far_site["label"].removeprefix("the ")) not in vr.deck_service_entries(world)
    with pytest.raises(ValueError):
        vr.investigate_landmark(world)
    assert (far, f"Site bearing ({world.by_id[far].x},{world.by_id[far].y})") not in vr.route_choices(_at(world, 0))


def test_investigating_the_first_site_says_where_the_second_is():
    world = _world_with_seed(42)
    _at(world, world.landmark["system_id"])
    before = world.save.pilot.credits
    lines = vr.investigate_landmark(world)
    far = world.by_id[world.far_site["system_id"]]
    assert world.save.pilot.credits == before + world.landmark["reward_credits"]
    lead = f"Its record carries a second bearing: {far.x},{far.y} in {vr.sector_for(far)}. The route planner will take it."
    assert lead in lines and vr.far_site_lead(world) == lead
    assert vr.far_site_bearing(world) == far.id
    assert far.name not in " ".join(lines)  # a bearing, never a name
    # The Discovery view keeps the lead in sight until it has been followed.
    _large, _compact, details, _title = vr.viewport_content(world, "3")
    assert lead in details and world.landmark["label"] in details


def test_the_bearing_is_a_destination_the_route_planner_takes():
    world = _world_with_seed(42)
    world.save.flags["landmark_investigated"] = True
    far = world.by_id[world.far_site["system_id"]]
    far.discovered = False
    _at(world, 0)
    assert vr.route_choices(world)[-1] == (far.id, f"Site bearing ({far.x},{far.y})")
    first_leg = vr.bfs_path(world.by_id, 0, far.id)[0]
    world.save.ship.fuel = vr.fuel_capacity(world.save.ship)
    assert vr.prepare_route_jump(world, far.id) == first_leg
    # Once charted it is in the list by its own name, like any other system.
    far.discovered = True
    assert (far.id, far.name) in vr.route_choices(world)
    assert not any(label.startswith("Site bearing") for _sid, label in vr.route_choices(world))


def test_the_second_site_pays_more_and_gives_its_own_record():
    world = _world_with_seed(42)
    world.save.flags["landmark_investigated"] = True
    far = world.far_site
    _at(world, far["system_id"])
    assert vr.landmark_here(world) is far
    assert vr.deck_service_entries(world)[-1][0] == "L"
    assert vr.deck_service_entries(world)[-1][1].lower() == far["label"].removeprefix("the ").lower()
    before = world.save.pilot.credits
    lines = vr.investigate_landmark(world)
    assert world.save.pilot.credits == before + vr.FAR_SITE_REWARD
    assert lines == [f"Salvage recovered: +{vr.FAR_SITE_REWARD}cr.", vr.ARCHIVE_RECORDS[far["label"]][0]]
    assert world.save.flags["landmark2_investigated"] is True
    # Claimed once, and the bearing is spent.
    assert vr.landmark_here(world) is None and vr.far_site_bearing(world) is None and vr.far_site_lead(world) is None
    assert not any(label.startswith("Site bearing") for _sid, label in vr.route_choices(world))
    with pytest.raises(ValueError):
        vr.investigate_landmark(world)
    # The Discovery view now shows the second site, claimed.
    _large, _compact, details, _title = vr.viewport_content(world, "3")
    assert far["label"] in details and "Salvage already claimed." in details


def test_mara_s_errand_is_the_first_site_s_and_is_not_disturbed():
    world = _world_with_seed(42)
    world.save.flags["archive_v1_started"] = True
    _at(world, world.landmark["system_id"])
    lines = vr.investigate_landmark(world)
    assert world.save.flags["archive_v1_recovered"] is True
    assert "Archive record recovered. Return to Mara at Freeport." in lines
    assert vr.far_site_lead(world) in lines


def test_following_the_record_is_an_achievement_and_the_save_still_loads():
    world = _world_with_seed(42)
    assert "far_site" in {accolade.id for accolade in vr.ACCOLADES}
    world.save.flags["landmark_investigated"] = True
    _at(world, world.far_site["system_id"])
    assert "far_site" not in {accolade.id for accolade in vr.unlock_accolades(world.save)}
    vr.investigate_landmark(world)
    assert "far_site" in {accolade.id for accolade in vr.unlock_accolades(world.save)}
    again = vr.SaveData.from_dict(world.save.to_dict())
    assert again.flags["landmark2_investigated"] is True


def test_the_picture_names_the_bearing_while_it_is_open(monkeypatch):
    world = _world_with_seed(42)
    _at(world, world.landmark["system_id"])
    vr.investigate_landmark(world)
    far = world.by_id[world.far_site["system_id"]]
    scene = vr.viewport_scene(world, "3", 80, 24)
    caption = "".join(piece for row in scene.lines for piece, _color in row)
    assert f"Second bearing: {far.x},{far.y}" in caption
    world.save.flags["landmark2_investigated"] = True
    scene = vr.viewport_scene(world, "3", 80, 24)
    assert "Second bearing" not in "".join(piece for row in scene.lines for piece, _color in row)
    assert scene.site == world.far_site["label"]
