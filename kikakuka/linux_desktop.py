"""Persistent desktop identity for source runs and portable Linux builds."""

import os
from pathlib import Path
import sys

from .common import resource_path


DESKTOP_ID = "Kikakuka"
GENERATED_MARKER = "X-Kikakuka-Generated=true"


def desktop_exec_argument(value):
    # Desktop-entry string escaping is applied before Exec quoting.
    value = os.fspath(value).replace("%", "%%")
    for character in ("\\", '"', "`", "$"):
        value = value.replace(character, "\\" + character)
    value = value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
    return '"' + value + '"'


def register_linux_desktop(desktop_id=DESKTOP_ID, name="Kikakuka",
                           icon_resource="icon.png", arguments=()):
    """Give Wayland a launcher and icon outside the disposable AppImage mount.

    Refresh only launchers we created; leave manually installed entries alone.
    """
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    desktop = data_home / "applications" / f"{desktop_id}.desktop"
    if desktop.exists() and GENERATED_MARKER not in desktop.read_text():
        return
    if not desktop.exists():
        for directory in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"):
            if directory and (Path(directory) / "applications" / desktop.name).exists():
                return

    if os.environ.get("APPIMAGE"):
        command = [os.environ["APPIMAGE"]]
    elif getattr(sys, "frozen", False):
        command = [sys.executable]
    else:
        command = [
            sys.executable,
            str(Path(__file__).resolve().parent),
        ]

    command.extend(arguments)
    icon = data_home / "icons/hicolor/512x512/apps" / f"{desktop_id}.png"
    icon_data = Path(resource_path(icon_resource)).read_bytes()
    icon.parent.mkdir(parents=True, exist_ok=True)
    if not icon.exists() or icon.read_bytes() != icon_data:
        icon.write_bytes(icon_data)
    contents = (
        f"[Desktop Entry]\nType=Application\nName={name}\n"
        f"Exec={' '.join(desktop_exec_argument(argument) for argument in command)} %F\n"
        f"Icon={icon}\nTerminal=false\nCategories=Development;Electronics;\n"
        f"StartupWMClass={desktop_id}\n{GENERATED_MARKER}\n"
    )
    desktop.parent.mkdir(parents=True, exist_ok=True)
    if not desktop.exists() or desktop.read_text() != contents:
        desktop.write_text(contents)
