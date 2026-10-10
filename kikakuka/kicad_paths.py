"""Resolve KiCad path variables and 3D model references."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Mapping, Optional


_VARIABLE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_KICAD_MAJORS = range(6, 12)


def _config_directories() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Preferences" / "kicad"]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        return [Path(appdata) / "kicad"] if appdata else []
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return [xdg_config / "kicad"]


def _data_directories() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Documents" / "KiCad"]
    if os.name == "nt":
        user_profile = Path(os.environ.get("USERPROFILE", home))
        return [user_profile / "Documents" / "KiCad"]
    xdg_data = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return [xdg_data / "kicad", xdg_data / "KiCad"]


def _load_user_path_variables() -> dict[str, str]:
    result = {}
    for config_root in _config_directories():
        try:
            configs = config_root.glob("*/kicad_common.json")
            for config in configs:
                try:
                    data = json.loads(config.read_text(encoding="utf-8"))
                    variables = (data.get("environment", {}) or {}).get(
                        "vars", {}) or {}
                    result.update({
                        str(key): str(value)
                        for key, value in variables.items()
                        if value is not None
                    })
                except (OSError, ValueError, TypeError):
                    continue
        except OSError:
            continue
    return result


def _third_party_roots(source_path=None) -> dict[int, Path]:
    """Find PCM roots, preferring the root containing *source_path*."""
    roots = {}
    if source_path is not None:
        source = Path(source_path)
        for candidate in (source.absolute(), source.resolve()):
            for parent in candidate.parents:
                if parent.name.lower() != "3rdparty":
                    continue
                version = parent.parent.name.split(".", 1)[0]
                if version.isdigit():
                    roots[int(version)] = parent
                break

    for data_root in _data_directories():
        for major in _KICAD_MAJORS:
            candidate = data_root / f"{major}.0" / "3rdparty"
            if candidate.is_dir() and major not in roots:
                roots[major] = candidate
    return roots


def _installed_3d_model_directory(kicad) -> Optional[Path]:
    try:
        binary = Path(kicad.get_kicad_binary_path("kicad-cli")).resolve()
    except Exception:
        return None

    candidates = [
        binary.parent.parent / "SharedSupport" / "3dmodels",
        binary.parent.parent / "share" / "kicad" / "3dmodels",
        binary.parent / "3dmodels",
    ]
    return next((path for path in candidates if path.is_dir()), None)


def _project_directory(board) -> str:
    if board is None:
        return ""
    try:
        return str(board.document.project.path or "")
    except Exception:
        try:
            return str(board.get_project().path or "")
        except Exception:
            return ""


def path_variables(kicad, board=None, *, source_path=None) -> dict[str, str]:
    """Collect KiCad path variables needed to resolve model references."""
    variables = _load_user_path_variables()
    variables.update({key: value for key, value in os.environ.items()})

    project_path = _project_directory(board)
    if project_path:
        variables["KIPRJMOD"] = project_path

    for major, root in _third_party_roots(source_path).items():
        variables.setdefault(f"KICAD{major}_3RD_PARTY", str(root))

    model_dir = _installed_3d_model_directory(kicad)
    if model_dir is not None:
        for major in _KICAD_MAJORS:
            variables.setdefault(f"KICAD{major}_3DMODEL_DIR", str(model_dir))
    return variables


def expand_variables(value: str, variables: Mapping[str, str]) -> str:
    """Recursively expand ``${NAME}`` references from *variables*."""
    def replacement(match):
        replacement_value = variables.get(match.group(1))
        return (str(replacement_value)
                if replacement_value is not None else match.group(0))

    result = value
    for _ in range(10):
        expanded = _VARIABLE_RE.sub(replacement, result)
        if expanded == result:
            break
        result = expanded
    return result


def resolve_model_path(
    filename: str,
    board,
    variables: Mapping[str, str],
    *,
    prefer_step: bool = False,
) -> Optional[Path]:
    """Resolve a KiCad model reference and return it when the file exists."""
    value = str(filename or "").strip()
    if not value:
        return None

    try:
        value = board.expand_text_variables(value, expand_env_vars=True)
    except TypeError:
        try:
            value = board.expand_text_variables(value)
        except Exception:
            pass
    except Exception:
        pass

    value = expand_variables(str(value), variables)
    if _VARIABLE_RE.search(value):
        return None

    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        project_dir = variables.get("KIPRJMOD")
        if project_dir:
            path = Path(project_dir) / path

    if prefer_step and path.suffix.lower() == ".wrl":
        for suffix in (".step", ".stp", ".STEP", ".STP"):
            alternative = path.with_suffix(suffix)
            if alternative.is_file():
                return alternative
    return path if path.is_file() else None
