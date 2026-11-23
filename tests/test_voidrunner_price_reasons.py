"""A commodity's page says why its price is what it is (review).

A price is a good's usual price moved by up to four things, and the market
showed only their product. These pin the row that names them: that the reasons
multiply to the quote, that each is named only when it is moving the price, in
the order the quote applies them, and that the row is on the page and fits.
"""

from __future__ import annotations

import contextlib
import io
import math

import pytest

from .support import _world_with_seed, plain, vr


@pytest.mark.parametrize("seed", range(40, 46))
def test_the_reasons_multiply_to_the_quote_everywhere(seed):
    world = _world_with_seed(seed)
    for day in (0, 7, 19):
        world.save.turn = day
        for system in world.galaxy[::5]:
            for commodity in vr.COMMODITIES:
                reasons = vr.price_reasons(world, system.id, commodity)
                product = math.prod(factor for _words, factor in reasons)
                quote = vr.price_for(world, system.id, commodity)
                # Factors within a thousandth of one are left out, which is under half a credit on any good.
                assert abs(vr.COMMODITIES[commodity]["base"] * product - quote) <= 1.0, (system.name, commodity, reasons)
                assert all(round(factor, 3) != 1.0 for _words, factor in reasons)


def test_each_reason_is_named_when_it_moves_the_price_and_in_the_quotes_order():
    world = _world_with_seed(42)
    here = world.here
    made = next(c for c in vr.LEGAL_COMMODITIES if vr.economy_price_factor(here.economy, c) < 1)
    assert vr.price_reasons(world, here.id, made)[0][0] == f"{here.economy} stations make it"
    wanted = next(c for c in vr.LEGAL_COMMODITIES if vr.economy_price_factor(here.economy, c) > 1)
    assert vr.price_reasons(world, here.id, wanted)[0][0] == f"{here.economy} stations want it"
    # Day zero is level: no season. Later there is one, after the economy and the trait.
    assert not any(words == "the season" for words, _f in vr.price_reasons(world, here.id, made))
    world.save.turn = 7
    assert vr.price_reasons(world, here.id, made)[-1][0] == "the season"
    # A system with a market of its own names it, between the economy and the season.
    sid, trait = next((sid, trait) for sid, trait in world.traits.items())
    words = [w for w, _f in vr.price_reasons(world, sid, trait["commodity"])]
    assert f"a standing {'glut' if trait['kind'] == 'surplus' else 'shortage'} here" in words
    assert words.index(words[-1]) == len(words) - 1 and (words[-1] == "the season" or "standing" in words[-1])
    # The pilot's own buying is last, and only on the asking side.
    world.save.market_drift = {here.id: {made: 1.2}}
    assert vr.price_reasons(world, here.id, made)[-1] == ("your own buying", 1.2)
    world.save.market_drift = {here.id: {made: 0.8}}  # sold into it: that moves what it pays, not what it asks
    assert not any(words == "your own buying" for words, _f in vr.price_reasons(world, here.id, made))


def test_the_row_reads_as_a_sentence_and_says_so_when_nothing_moves_the_price(monkeypatch):
    world = _world_with_seed(42)
    here = world.here
    made = next(c for c in vr.LEGAL_COMMODITIES if vr.economy_price_factor(here.economy, c) < 1)
    off = round((vr.economy_price_factor(here.economy, made) - 1) * 100)
    row = plain(vr.price_reason_line(world, here.id, made))
    assert row == (f"Why {vr.price_for(world, here.id, made)}cr: usually {vr.COMMODITIES[made]['base']}cr; "
                   f"{here.economy} stations make it {off:+d}%.")
    monkeypatch.setattr(vr, "price_reasons", lambda w, s, c: [])
    assert plain(vr.price_reason_line(world, here.id, made)).endswith("its usual price; nothing here is moving it.")


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_the_row_is_on_the_commoditys_page_and_fits(monkeypatch, width, height):
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", width)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", height)
    world = _world_with_seed(42)
    world.save.turn = 7
    world.save.market_drift = {world.here.id: {"food": 1.2}}
    keys = iter(["B"])
    monkeypatch.setattr(vr, "read_key", lambda: next(keys, "B"))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        vr._trade_commodity(vr.Palette(False), world, "food")
    text = plain(output.getvalue())
    assert "Why " in text and "usually" in text and "your own buying +20%" in " ".join(text.split())
    assert all(vr._visible_width(line) <= width for line in text.splitlines())
