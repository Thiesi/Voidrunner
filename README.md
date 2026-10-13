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
- **Other BBS software** (Synchronet, Mystic, ENiGMA½, …): accepts DOOR32.SYS
  and DOOR.SYS, with CP437 or UTF-8 terminals and stdio or inherited DOOR32
  sockets; see the SysOp setup below. Host time-limit handling remains planned (#5).
- **Standalone**, for trying it out: `python3 voidrunner.py`.

## Saves

Careers live in the directory named by `VOIDRUNNER_SAVE_DIR`. NetBBS always
sets it, to `<db path>.doors/voidrunner/` unless the SysOp chose another
place. Outside NetBBS, the default is `~/.voidrunner/`. If that directory does
not exist and `~/.netbbs/voidrunner_saves/` does, the legacy directory is used
in place, without moving files. NetBBS's fallback remains the legacy directory,
including backup imports without a door environment when both directories exist.
Each pilot is one JSON file, keyed by the caller's numeric user id. The directory also holds
the session locks and the Hall of Fame records, so back it up as a whole and
don't delete lock files while a BBS is running.

## SysOp setup

Install Python 3.11+ and copy `voidrunner.py` to a persistent door directory.
Pass the caller's drop-file **filename**, quoted if it contains spaces:

```sh
python3 /opt/doors/voidrunner/voidrunner.py /path/to/node/DOOR.SYS
```

`DOOR32.SYS` also works. Filenames are matched without case sensitivity;
renamed files are recognized by their contents. Both CRLF and LF line endings
are accepted. The caller's numeric record number owns the save, so changing
an alias does not start a new career. `NETBBS_DOOR_INFO`, when set, takes
precedence over the command-line file, even if the JSON file cannot be read.
Malformed classic files emit a warning to stderr and use Guest defaults.

Set `VOIDRUNNER_SAVE_DIR` to an absolute, writable directory shared by every
node of **one BBS**, outside per-session temporary directories. Different BBSes
need separate save directories because their user numbers can overlap. A Unix
wrapper at `/opt/doors/voidrunner/launch.sh` can set it for every launch:

```sh
#!/bin/sh
export VOIDRUNNER_SAVE_DIR=/var/lib/voidrunner
exec /usr/bin/python3 /opt/doors/voidrunner/voidrunner.py "$1"
```

Make the wrapper executable and create the save directory with ownership for
the BBS account. Configure the host as follows (adjust the example paths):

| Host | Stdio launch configuration |
| --- | --- |
| Synchronet on Linux | In SCFG's Online Programs, select Native Executable, Standard I/O, DOOR.SYS, and drop files in the Node Directory. Command line: `/opt/doors/voidrunner/launch.sh "%f"`. |
| Mystic on Linux/macOS | Use the `DD` external-program menu command with `/opt/doors/voidrunner/launch.sh "%PDOOR.SYS"`. `%P` includes the trailing slash. |
| ENiGMA½ | Use the `abracadabra` menu module with the configuration below. |

```hjson
config: {
    name: Voidrunner
    dropFileType: DOOR
    cmd: /usr/bin/python3
    args: [ "/opt/doors/voidrunner/voidrunner.py", "{dropFilePath}" ]
    io: stdio
    encoding: cp437
    env: { VOIDRUNNER_SAVE_DIR: "/var/lib/voidrunner" }
}
```

These are setup recipes awaiting the real-host checks in #6. Classic drop files select the
16-color `basic` preset for new ANSI careers, or `plain` for ASCII callers;
existing careers keep their saved Display Options choice. Encoding defaults to
CP437 for classic drop files and UTF-8 for standalone launches. In Display
Options, `[E]` cycles Automatic, UTF-8 and CP437 independently of the color
preset. Match the host's bridge encoding to that choice (the ENiGMA½ example
uses CP437). NetBBS always receives UTF-8 and handles its caller's encoding
itself, so its Display Options show a managed encoding. Saved names remain
Unicode; characters outside CP437 are transliterated for display only.
For a native DOOR32 socket launch on Windows or Unix, pass the same command-line
filename and configure the host to inherit the caller's connected socket.
DOOR32 line 1 must be `2` and line 2 must contain that inherited handle. The door
then sends and receives terminal traffic through the socket; it does not use
redirected stdin/stdout. A stale or invalid socket handle stops the launch with
a diagnostic on stderr. Comm type `0` and DOOR.SYS launches use stdio as before.

The socket transport filters telnet commands, escapes literal IAC output bytes,
and accepts CR-NUL or CR-LF as a single Enter. The host retains responsibility
for option negotiation. A disconnected caller follows the normal EOF checkpoint
path. Returning from the door closes its inherited handle without shutting down
the host's connection. Real Windows Synchronet validation remains in #6.
Known host time limits appear in each page's bottom border. The door warns at
three minutes remaining and saves before returning to the BBS, reserving up to
one second before the host cutoff. This also works while a prompt is unanswered.
The allowance comes from classic drop-file minutes or NetBBS's
`session_limit_seconds`, measured with a monotonic clock from door launch.
Absent or invalid limits leave sessions unbounded; an explicit zero expires
immediately. The limit belongs to this launch and is never stored in a career.

In the animated presets (`auto` and `basic`) the door opens on a short title
splash and plays brief full-screen scenes during play: the jump, a contact, a
kill or the loss of the ship, an escape, a customs sweep, docking, salvage,
a rescue, a data burst, a tow, a hull refit, promotions, joining a faction and
retirement. Any key skips one. Callers who want none choose `fast` in Display Options. A SysOp can
switch them off for everyone with `DOOR_SPLASH=0` (the title splash) and
`DOOR_SCENES=0` (the in-flight scenes) in the door's environment. A scene sends
under 90 KB at 80x24 in truecolor, less at lower color depths.

Host references: [Synchronet DOOR.SYS](https://wiki.synchro.net/ref:door.sys),
[Mystic external programs](https://wiki.mysticbbs.com/doku.php?id=menu_commands),
[ENiGMA½ scripts and native binaries](https://enigma-bbs.github.io/doors/scripts-and-binaries/).

## Development

```sh
python -m pip install pytest pytest-xdist
python -m pytest -n auto              # the suite, about half a minute
python tools/check_stdlib_only.py     # the door imports only the standard library
python tools/door_gallery.py --open   # every screen at 80/64/40 columns, every preset
python tools/door_gallery.py --encoding cp437 --out build/gallery-cp437
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
