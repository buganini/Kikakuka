"""Add scaled placeholder models to footprints through KiCad's IPC API."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional

from kicad_compat import KICAD10_COMPAT, get_kicad_compat
from kicad_paths import (
    path_variables as _shared_path_variables,
    resolve_model_path,
)


# Retained as the public KiCad 10 constant used by existing tests and boards.
# Runtime code selects the value from the connected system KiCad version.
PLACEHOLDER_MODEL = KICAD10_COMPAT.placeholder_model_uri

_LENGTH_RE = re.compile(
    r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
    r"(?:\s*(mm|in|mil))?\s*",
    re.IGNORECASE,
)
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


def path_variables(kicad, board) -> dict[str, str]:
    return _shared_path_variables(kicad, board, source_path=__file__)


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


def _new_placeholder_model(
    size_x: float,
    size_y: float,
    size_z: float,
    compatibility=KICAD10_COMPAT,
):
    from kipy.board_types import Footprint3DModel
    from kipy.geometry import Vector3D

    model = Footprint3DModel()
    model.filename = compatibility.placeholder_model_uri
    model.scale = Vector3D.from_xyz(size_x, size_y, size_z)
    model.visible = True
    compatibility.set_model_opacity(model, 1.0)
    return model


def _replace_models_with_placeholder(
    footprint,
    size_x: float,
    size_y: float,
    size_z: float,
    compatibility=KICAD10_COMPAT,
):
    from kipy.board_types import Footprint3DModel

    footprint.definition.items = [
        item for item in footprint.definition.items
        if not isinstance(item, Footprint3DModel)
    ]
    footprint.definition.add_item(
        _new_placeholder_model(
            size_x, size_y, size_z, compatibility=compatibility))


def add_placeholder_models(kicad, board) -> ScanResult:
    """Scan the board and add scaled unit cubes in one undoable operation."""
    compatibility = get_kicad_compat(kicad.get_version())
    variables = path_variables(kicad, board)
    placeholder_model = compatibility.placeholder_model_uri
    placeholder = resolve_model_path(placeholder_model, board, variables)
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
            footprint,
            size_x,
            size_y,
            size_z,
            compatibility=compatibility,
        )
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
