"""Every system's own world, and the hulls in the picture (review).

Forty-eight systems shared five pictures, one per economy. A world is now made
from its economy and the system's name alone, the pilot's own hull is moored at
the station, and a raider flies the hull its pattern names. These tests pin
that a world is stable, distinct and still its economy's color, and that the
canopy shows it.
"""

from __future__ import annotations

from .support import _world_with_seed, vr

def test_a_systems_world_is_its_own_and_the_same_on_every_visit():
    vr.world_look.cache_clear()
    first = vr.world_look("Agricultural", "Ember Reach")
    vr.world_look.cache_clear()
    assert vr.world_look("Agricultural", "Ember Reach") == first
    looks = [vr.world_look("Agricultural", name) for name in vr.SYSTEM_NAMES]
    assert len({look["bands"] for look in looks}) == len(looks)
    # Variety is not optional: across the name pool there are iced worlds and
    # bare ones, ringed and unringed, moonless and mooned.
    assert {bool(look["cap"]) for look in looks} == {True, False}
    assert {bool(look["ring"]) for look in looks} == {True, False}
    assert {len(look["moons"]) for look in looks} >= {0, 1}
    assert vr.world_look("Haven", None) is None  # the title's world stays as drawn


def test_a_world_keeps_its_economys_family_of_color():
    """A farm world may be any green or blue; it may not turn rust."""
    import colorsys
    for economy, bands in vr._SCENE_WORLD_BANDS.items():
        if economy == "Mining":
            continue  # greys: a hue is not what a mining world is read by
        for name in vr.SYSTEM_NAMES:
            look = vr.world_look(economy, name)
            hues = sorted(colorsys.rgb_to_hsv(*(c / 255 for c in color))[0] for color in bands)
            for color in look["bands"]:
                hue = colorsys.rgb_to_hsv(*(c / 255 for c in color))[0]
                nearest = min(min(abs(hue - h), 1 - abs(hue - h)) for h in hues)
                assert nearest <= 0.08, (economy, name, color)


def test_the_canopy_shows_this_systems_world_and_this_pilots_hull(monkeypatch):
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 24)
    monkeypatch.setattr(vr, "_OUTPUT_STYLE", "auto")
    monkeypatch.setattr(vr, "_OUTPUT_ENCODING", "utf-8")
    monkeypatch.setattr(vr, "_PALETTE", vr.Palette(truecolor=True))
    world = _world_with_seed(42)
    scene = vr.canopy_scene(world)
    assert scene["world"] == world.here.name and scene["hull"] == "Shuttle"
    vr._CANOPY_CACHE.clear()
    shuttle = vr.canopy_cells(scene, 80, 24)
    carrier = vr.canopy_cells(dict(scene, hull="Carrier"), 80, 24)
    elsewhere = vr.canopy_cells(dict(scene, world="Bastion"), 80, 24)
    assert shuttle != carrier, "a refit does not show in the window"
    assert shuttle != elsewhere, "two systems of one economy share a picture"
    vr._CANOPY_CACHE.clear()
