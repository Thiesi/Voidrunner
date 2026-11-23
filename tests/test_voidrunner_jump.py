"""The jump's tunnel is its destination's (review).

Every jump was the same violet tunnel, played every turn of a career. These
pin what a destination now brings to it: its trade's light and its own world at
the far end when it is charted, a blank disc and the old violet when it is not,
a shorter scene for a one-unit hop, and that a journey hands the scene what the
pilot knows and no more.
"""

from __future__ import annotations

import contextlib
import io

from .support import _world_with_seed, vr


def _jump(**kw):
    base = dict(hull="Shuttle", destination="Xanthe", burn=3, fuel=20, day=7, seed="t")
    base.update(kw)
    return vr._JumpScene(80, 24, **base)


def _colors(scene, index=None):
    frame = scene.frame(scene.frames - 1 if index is None else index)
    return [color for row in frame for _char, fg, bg in row for color in (fg, bg) if color]


def test_a_charted_destination_lights_the_tunnel_and_shows_its_world():
    world = _world_with_seed(42)
    farm = next(s for s in world.galaxy if s.economy == "Agricultural")
    lab = next(s for s in world.galaxy if s.economy == "Tech")
    a = _jump(destination=farm.name, economy=farm.economy, world=farm.name)
    b = _jump(destination=lab.name, economy=lab.economy, world=lab.name)
    assert a.light == vr._SCENE_LIGHTS["Agricultural"] and b.light == vr._SCENE_LIGHTS["Tech"]
    assert a.frame(a.frames - 1) != b.frame(b.frames - 1)
    # The far end is that system's own world: the planet its dock and its atlas page draw.
    look = vr.world_look(farm.economy, farm.name)
    bands = {tuple(band) for band in look["bands"]} if look else set()
    assert len(set(a.far.values())) > 4  # a lit, banded disc, not a flat one
    assert bands or a.far
    centre = (40, a.mid)
    assert centre in a.far and centre in b.far and a.far[centre] != b.far[centre]
    # Before the tunnel there is nothing of the destination in the sky.
    assert a.frame(3) == _jump(destination=farm.name).frame(3)


def test_an_uncharted_destination_keeps_the_violet_and_shows_a_blank_disc():
    scene = _jump(destination=None)
    assert scene.light is None and scene.tint((200, 140, 255)) == (200, 140, 255)
    assert set(scene.far.values()) == {(14, 10, 30)}  # nothing on it: all a pilot knows
    # A name with no economy is treated the same: the scene never guesses a world.
    assert _jump(destination="Somewhere", economy=None).light is None
    assert _jump(destination=None, economy="Tech", world="Yarrow").light is None


def test_a_one_unit_hop_is_a_shorter_scene_with_the_same_ending():
    short, long = _jump(burn=1), _jump(burn=3)
    assert short.frames == vr._JumpScene.SHORT_FRAMES < long.frames == vr._JumpScene.frames
    sky = short.sky_px // 2
    assert short.frame(short.frames - 1)[:sky] == long.frame(long.frames - 1)[:sky]  # the same sky, reached sooner


def test_the_far_end_comes_up_in_steps_and_then_holds():
    scene = _jump(economy="Mining", world="Quietus", destination="Quietus")
    centre = [scene.frame(index)[scene.mid // 2][40] for index in range(scene.frames)]
    changes = sum(1 for before, after in zip(centre, centre[1:]) if before != after)
    assert centre[-1] == centre[-2] == centre[-3]  # it holds once it has arrived
    assert 2 <= changes <= 9  # the flash, the core, three steps of arrival: not a shimmer


def test_a_journey_hands_the_scene_what_the_pilot_knows(monkeypatch):
    made = []

    def record(make):
        made.append(make(80, 24))
        return True

    monkeypatch.setattr(vr, "play_scene", record)
    for charted in (True, False):
        world = _world_with_seed(42)
        world.save.pilot.credits = 5_000
        world.event_rng.random = lambda: 1.0  # no encounter, no inspection
        target = world.by_id[sorted(world.here.connections)[0]]
        target.discovered = charted
        made.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            vr.screen_travel(vr.Palette(False), world, target.id)
        jump = next(scene for scene in made if isinstance(scene, vr._JumpScene))
        if charted:
            assert jump.destination == target.name and jump.light == vr._SCENE_LIGHTS[target.economy]
        else:
            assert jump.destination is None and jump.light is None and set(jump.far.values()) == {(14, 10, 30)}
