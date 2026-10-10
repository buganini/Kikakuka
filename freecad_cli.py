"""Launch the detected FreeCAD console command from Kikakuka's CLI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Sequence

from addon_manager import freecad_commands


def kkkk_export_script_path() -> Path:
    """Return the source-tree or bundled FreekiCAD export entry point."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        script = Path(frozen_root) / "freekicad/scripts/kkkk_export.py"
    else:
        script = (
            Path(__file__).resolve().parent
            / "FreekiCAD/scripts/kkkk_export.py"
        )
    if not script.is_file():
        raise FileNotFoundError(f"Bundled kkkk_export.py was not found: {script}")
    return script


def _freecad_environment(executable: str) -> dict[str, str]:
    from cadhoc.instance_backend import freecad_process_environment

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


def run_freekicad_export(source: str, target: str) -> int:
    """Export an assembly through the bundled FreekiCAD entry point."""
    try:
        script = kkkk_export_script_path()
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 127
    return run_freecadcmd([str(script), source, target])
