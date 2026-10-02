"""Locate KiCad CLI without importing the Differ UI or native bindings."""

import glob
import os
import platform
import shutil
import sys


def resolve_kicad_cli():
    system = platform.system()
    base_path = getattr(sys, "_MEIPASS", None)
    if base_path:
        if system == "Darwin":
            bundled = os.path.abspath(os.path.join(base_path, "..", "MacOS", "kicad-cli"))
        else:
            executable = "kicad-cli.exe" if system == "Windows" else "kicad-cli"
            bundled = os.path.join(base_path, "KiCad", "bin", executable)
        if os.path.isfile(bundled):
            return bundled, "Bundled"

    if system == "Darwin":
        system_path = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    elif system == "Windows":
        candidates = sorted(glob.glob("C:/Program Files/KiCad/*/bin/kicad-cli.exe"))
        system_path = candidates[0] if candidates else "C:/Program Files/KiCad/bin/kicad-cli.exe"
    else:
        system_path = shutil.which("kicad-cli") or "/usr/bin/kicad-cli"
    return system_path, "System"
