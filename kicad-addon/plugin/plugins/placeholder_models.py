"""Add scaled placeholder models to footprints through KiCad's IPC API."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional


PLACEHOLDER_MODEL = (
    "${KICAD10_3RD_PARTY}/3dmodels/"
    "com_github_buganini_kikakuka-footprints/"
    "Kikakuka.3dshapes/unit-cube.step"
)

_LENGTH_RE = re.compile(
    r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
    r"(?:\s*(mm|in|mil))?\s*",
    re.IGNORECASE,
)
_VARIABLE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class ScanResult:
    scanned: int = 0
    already_valid: int = 0
    added: int = 0
    missing_size: int = 0
    invalid_size: int = 0
    details: list[str] = field(default_factory=list)


def parse_length_mm(value, quantity_name="length") -> float:
    """Parse a positive mm/in/mil value, treating a missing suffix as mm."""
    if value is None:
        raise ValueError(
            f"missing {quantity_name}; expected mm, in, or mil")

    match = _LENGTH_RE.fullmatch(str(value))
    if match is None:
        raise ValueError(
            f"invalid {quantity_name} value {value!r}; "
            "expected mm, in, or mil"
        )

    result = float(match.group(1))
    unit = (match.group(2) or "mm").lower()
    if unit == "in":
        result *= 25.4
    elif unit == "mil":
        result *= 0.0254

    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{quantity_name} must be greater than zero")
    return result


def _field_text(field) -> Optional[str]:
    try:
        return field.text.value
    except Exception:
        try:
            return field.text.text.value
        except Exception:
            return None


def footprint_properties(footprint) -> dict[str, str]:
    """Return the footprint's named fields as a property dictionary."""
    try:
        fields = footprint.texts_and_fields
    except Exception:
        fields = footprint.definition.texts

    result = {}
    for item in fields:
        try:
            name = item.name
        except Exception:
            continue
        value = _field_text(item)
        if name and value is not None:
            result[name] = value
    return result


def footprint_label(footprint) -> str:
    try:
        reference = footprint.reference_field.text.value.strip()
        if reference:
            return reference
    except Exception:
        pass
    try:
        name = footprint.definition.id.name.strip()
        if name:
            return name
    except Exception:
        pass
    return "<unnamed footprint>"


def _config_directories() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Preferences" / "kicad"]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        return [Path(appdata) / "kicad"] if appdata else []
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return [xdg_config / "kicad"]


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


def _third_party_roots() -> dict[int, Path]:
    """Find the usual PCM roots, preferring the root containing this plugin."""
    roots = {}
    source_paths = [Path(__file__).absolute(), Path(__file__).resolve()]
    for source in source_paths:
        for parent in source.parents:
            if parent.name.lower() == "3rdparty":
                version = parent.parent.name.split(".", 1)[0]
                if version.isdigit():
                    roots[int(version)] = parent
                break

    home = Path.home()
    if sys.platform == "darwin" or os.name == "nt":
        data_root = home / "Documents" / "KiCad"
    else:
        data_root = Path(
            os.environ.get("XDG_DATA_HOME", home / ".local" / "share")
        ) / "KiCad"

    for major in range(6, 12):
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


def path_variables(kicad, board) -> dict[str, str]:
    variables = _load_user_path_variables()
    variables.update({key: value for key, value in os.environ.items()})

    try:
        project_path = board.document.project.path
    except Exception:
        try:
            project_path = board.get_project().path
        except Exception:
            project_path = ""
    if project_path:
        variables["KIPRJMOD"] = str(project_path)

    for major, root in _third_party_roots().items():
        variables.setdefault(f"KICAD{major}_3RD_PARTY", str(root))

    model_dir = _installed_3d_model_directory(kicad)
    if model_dir is not None:
        for major in range(6, 12):
            variables.setdefault(f"KICAD{major}_3DMODEL_DIR", str(model_dir))
    return variables


def _expand_variables(value: str, variables: Mapping[str, str]) -> str:
    result = value
    for _ in range(10):
        expanded = _VARIABLE_RE.sub(
            lambda match: variables.get(match.group(1), match.group(0)),
            result,
        )
        if expanded == result:
            break
        result = expanded
    return result


def resolve_model_path(
    filename: str,
    board,
    variables: Mapping[str, str],
) -> Optional[Path]:
    """Resolve a model reference and return it only when the file exists."""
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

    value = _expand_variables(value, variables)
    if _VARIABLE_RE.search(value):
        return None

    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        project_dir = variables.get("KIPRJMOD")
        if project_dir:
            path = Path(project_dir) / path
    return path if path.is_file() else None


def has_valid_model(footprint, board, variables: Mapping[str, str]) -> bool:
    for model in footprint.definition.models:
        filename = str(model.filename or "").strip()
        # KiCad owns and resolves embedded file references.  The 0.8 bindings
        # do not expose their payload, so a non-empty embedded reference is the
        # strongest validation available without parsing the board file.
        if filename.lower().startswith("kicad-embed://"):
            return True
        if resolve_model_path(filename, board, variables) is not None:
            return True
    return False


def _new_placeholder_model(size_x: float, size_y: float, size_z: float):
    from kipy.board_types import Footprint3DModel
    from kipy.geometry import Vector3D

    model = Footprint3DModel()
    model.filename = PLACEHOLDER_MODEL
    model.scale = Vector3D.from_xyz(size_x, size_y, size_z)
    model.visible = True
    # kicad-python 0.8 exposes opacity as read-only, although the protobuf
    # field itself is writable.
    model._proto.opacity = 1.0
    return model


def _replace_models_with_placeholder(
    footprint,
    size_x: float,
    size_y: float,
    size_z: float,
):
    from kipy.board_types import Footprint3DModel

    footprint.definition.items = [
        item for item in footprint.definition.items
        if not isinstance(item, Footprint3DModel)
    ]
    footprint.definition.add_item(
        _new_placeholder_model(size_x, size_y, size_z))


def add_placeholder_models(kicad, board) -> ScanResult:
    """Scan the board and add scaled unit cubes in one undoable operation."""
    variables = path_variables(kicad, board)
    placeholder = resolve_model_path(PLACEHOLDER_MODEL, board, variables)
    if placeholder is None:
        raise FileNotFoundError(
            "Kikakuka unit-cube.step was not found. Install the "
            "com.github.buganini.kikakuka-footprints library package first."
        )

    result = ScanResult()
    updates = []
    for footprint in board.get_footprints():
        result.scanned += 1
        label = footprint_label(footprint)
        if has_valid_model(footprint, board, variables):
            result.already_valid += 1
            continue

        properties = footprint_properties(footprint)
        missing = [
            name for name in ("SizeX", "SizeY", "SizeZ")
            if not str(properties.get(name, "")).strip()
        ]
        if missing:
            result.missing_size += 1
            continue

        try:
            size_x = parse_length_mm(properties["SizeX"], "SizeX")
            size_y = parse_length_mm(properties["SizeY"], "SizeY")
            size_z = parse_length_mm(properties["SizeZ"], "SizeZ")
        except ValueError as error:
            result.invalid_size += 1
            result.details.append(f"{label}: {error}")
            continue

        _replace_models_with_placeholder(
            footprint, size_x, size_y, size_z)
        updates.append(footprint)

    if not updates:
        return result

    commit = board.begin_commit()
    try:
        updated = board.update_items(updates)
        if len(updated) != len(updates):
            raise RuntimeError(
                f"KiCad updated {len(updated)} of {len(updates)} footprints")
        board.push_commit(commit, "Add placeholder 3D models")
    except Exception:
        board.drop_commit(commit)
        raise

    result.added = len(updates)
    return result
