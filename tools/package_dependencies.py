"""Fail early with actionable build dependency errors."""

import importlib.util
import os
import platform
import shutil
import subprocess
import sys


PYTHON_DEPENDENCIES = {
    "PyInstaller": "PyInstaller", "kikit": "KiKit", "pcbnew": "KiCad Python bindings",
    "PUI": "QPUIQ", "PySide6": "PySide6_Essentials", "shapely": "shapely",
    "pypdfium2": "pypdfium2", "cv2": "opencv-python-headless", "psutil": "psutil",
    "pygit2": "pygit2", "parsimonious": "parsimonious", "kipy": "kicad-python",
    "openpyxl": "openpyxl",
}
LINUX_TOOLS = ("bash", "file", "readelf", "patchelf", "ldd", "env", "base64", "readlink")


def check_dependencies(cli):
    errors = []
    for module, package in PYTHON_DEPENDENCIES.items():
        if importlib.util.find_spec(module) is None:
            errors.append(f"Missing Python module {module} ({package})")
    if not os.path.isfile(cli) or (platform.system() != "Windows" and not os.access(cli, os.X_OK)):
        errors.append(f"KiCad CLI not found or not executable: {cli}")
    if platform.system() == "Linux":
        if platform.machine() != "x86_64":
            errors.append("Linux AppImage currently requires a native x86_64 build")
        missing = [name for name in LINUX_TOOLS if not shutil.which(name)]
        if missing:
            errors.append("Missing Linux tools: " + ", ".join(missing))
    if not errors:
        try:
            cli_version = subprocess.check_output([cli, "--version"], text=True, timeout=30).strip()
            bindings_version = subprocess.check_output(
                [sys.executable, "-c", "import pcbnew; print(pcbnew.Version())"],
                text=True, timeout=30).strip()
            if cli_version != bindings_version:
                errors.append(f"KiCad version mismatch: CLI {cli_version}, pcbnew {bindings_version}")
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"Unable to load KiCad CLI or pcbnew (check native libraries/Python ABI): {error}")
    if errors:
        hints = [
            "Use this Python environment to install requirements.txt and PyInstaller.",
            "pcbnew must come from KiCad with a matching Python ABI; it is not a pip package.",
        ]
        if platform.system() == "Linux":
            hints.append("Ubuntu build tools: sudo apt install bash file binutils patchelf libc-bin coreutils")
        raise RuntimeError("Build dependency check failed:\n- " + "\n- ".join(errors) + "\n" + "\n".join(hints))
