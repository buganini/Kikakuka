"""Persistent per-user Kikakuka settings."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional


CONFIG_FILENAME = ".kikakuka"
_EXECUTABLE_KEYS = {
    "kicad": "kicad_executable",
    "freecad": "freecad_executable",
}


def config_path() -> Path:
    return Path.home() / CONFIG_FILENAME


def load_config(path: Optional[Path] = None) -> dict[str, Any]:
    target = path or config_path()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def update_config(
    updates: dict[str, Any],
    path: Optional[Path] = None,
) -> dict[str, Any]:
    """Merge settings without discarding settings owned by another UI area."""
    target = path or config_path()
    config = load_config(target)
    for key, value in updates.items():
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(config, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return config


def custom_executable(application: str) -> Optional[Path]:
    key = _EXECUTABLE_KEYS[application]
    value = load_config().get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser()


def set_custom_executable(application: str, executable: Optional[Path]) -> None:
    key = _EXECUTABLE_KEYS[application]
    value = str(executable) if executable is not None else None
    update_config({key: value})
