"""
The interpreter to spawn a bundled door with when a test means to *kill* it.

On Windows, a venv's `python.exe` is a launcher that starts the real
interpreter as a second process. Killing the launcher takes the interpreter
down too, but only after `Popen.wait()` has already returned, and in that
gap the "killed" door keeps running: a crash test that stops a door
mid-action then finds the action finished and saved. Under a loaded
parallel run the gap is long enough to happen every time.

The bundled doors import only the standard library, so the base interpreter
runs them unchanged, and its process is the one `kill()` stops. Elsewhere
(no venv, or a POSIX venv whose `python` is a symlink) this is simply
`sys.executable`.
"""

from __future__ import annotations

import sys

DOOR_PYTHON = getattr(sys, "_base_executable", None) or sys.executable
