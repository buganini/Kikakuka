"""Launch the detected FreeCAD console command from Kikakuka's CLI."""

from __future__ import annotations

import subprocess
import sys
from typing import Sequence

from addon_manager import freecad_commands


def _freecad_environment(executable: str) -> dict[str, str]:
    from im.instance_backend import freecad_process_environment

    return freecad_process_environment(executable)


def run_freecadcmd(arguments: Sequence[str]) -> int:
    """Run FreeCADCmd with *arguments* and return its exit status."""
    commands = freecad_commands()
    if not commands:
        print("FreeCADCmd executable was not found", file=sys.stderr)
        return 127

    command = [*commands[0], *arguments]
    environment = _freecad_environment(commands[0][0])

    try:
        return subprocess.run(command, env=environment).returncode
    except OSError as exc:
        print(f"Could not run FreeCADCmd: {exc}", file=sys.stderr)
        return 126
