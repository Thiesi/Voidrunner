![Voidrunner: tactical deep-space trading and exploration](art/banner.png)

# Voidrunner

A persistent, single-player space trading and exploration door game for BBSes,
in the tradition of Elite and Trade Wars. Trade across a 48-system galaxy, take
contracts, fight or talk your way past raiders, hire a crew, join factions,
and retire a career into the Hall of Fame.

Voidrunner is **one Python file** (`voidrunner.py`) with no dependencies beyond
the standard library. It needs Python 3.11 or newer and a terminal of at least
40×12. Saves are per-caller JSON files that the door manages itself.

## Where it runs

- **NetBBS:** bundled. NetBBS ships a tagged release of this file, so a NetBBS
  SysOp has nothing to install.
- **Other BBS software** (Synchronet, Mystic, ENiGMA½, …): planned. It needs
  DOOR32.SYS/DOOR.SYS support, CP437 output and, on Windows, socket-handle
  mode. The plan is in Thiesi/NetBBS#1214 and the work is in this repository's
  issues.
- **Standalone**, for trying it out: `python3 voidrunner.py`.

## Saves

Careers live in the directory named by `VOIDRUNNER_SAVE_DIR`. NetBBS always
sets it, to `<db path>.doors/voidrunner/` unless the SysOp chose another
place. Without it, the door uses `~/.netbbs/voidrunner_saves/`. Each pilot is
one JSON file, keyed by the caller's numeric user id. The directory also holds
the session locks and the Hall of Fame records, so back it up as a whole and
don't delete lock files while a BBS is running.

## Development

```sh
python -m pip install pytest pytest-xdist
python -m pytest -n auto              # the suite, about half a minute
python tools/check_stdlib_only.py     # the door imports only the standard library
python tools/door_gallery.py --open   # every screen at 80/64/40 columns, every preset
```

A change to a screen comes with gallery pictures: the tests can prove that a
screen fits, never that it looks right. The game's design (rules, saves,
presentation) is in [docs/design.md](docs/design.md).

Voidrunner was developed inside NetBBS until October 2026. Its history came
along, but issue numbers in older commits, comments and the design text refer
to [Thiesi/NetBBS](https://github.com/Thiesi/NetBBS) issues.

## Releases

Pushing a tag such as `v1.0.0` runs the suite and publishes that commit's
`voidrunner.py`, byte-for-byte as stored in git, with a `SHA256SUMS` file.
NetBBS vendors these releases.

The banner is drawn from the game's own launch splash by `art/render_banner.py`.

## License

BSD 2-Clause, see [LICENSE](LICENSE).
