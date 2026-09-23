"""Manage CouplerFixed/CouplerMoving helper 3D model visibility."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

from placeholder_models import (
    footprint_label,
    footprint_properties,
    path_variables,
    resolve_model_path,
)


_MODEL_ROOT = (
    "${KICAD10_3RD_PARTY}/3dmodels/"
    "com_github_buganini_kikakuka-footprints/"
    "Kikakuka.3dshapes"
)
COUPLER_MODELS = {
    "CouplerFixed": f"{_MODEL_ROOT}/coupler-fixed.step",
    "CouplerMoving": f"{_MODEL_ROOT}/coupler-moving.step",
}
_LENGTH_RE = re.compile(
    r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
    r"(?:\s*(mm|in|mil|um|µm))?\s*",
    re.IGNORECASE,
)
_ANGLE_RE = re.compile(
    r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
    r"(?:\s*(?:deg|°))?\s*",
    re.IGNORECASE,
)


@dataclass
class EnableResult:
    scanned: int = 0
    couplers: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    invalid: int = 0
    details: list[str] = field(default_factory=list)


@dataclass
class HideResult:
    scanned: int = 0
    changed: int = 0
    hidden: int = 0


def parse_signed_length_mm(value, quantity_name: str) -> float:
    """Parse a signed length, treating a missing suffix as millimetres."""
    if value is None or not str(value).strip():
        return 0.0
    match = _LENGTH_RE.fullmatch(str(value))
    if match is None:
        raise ValueError(
            f"invalid {quantity_name} value {value!r}; "
            "expected mm, in, mil, or um"
        )
    result = float(match.group(1))
    unit = (match.group(2) or "mm").lower()
    if unit == "in":
        result *= 25.4
    elif unit == "mil":
        result *= 0.0254
    elif unit in ("um", "µm"):
        result *= 0.001
    if not math.isfinite(result):
        raise ValueError(f"{quantity_name} must be finite")
    return result


def parse_tilt_degrees(value) -> float:
    """Parse a signed footprint-local X rotation in degrees."""
    if value is None or not str(value).strip():
        return 0.0
    match = _ANGLE_RE.fullmatch(str(value))
    if match is None:
        raise ValueError(f"invalid Tilt value {value!r}; expected degrees")
    result = float(match.group(1))
    if not math.isfinite(result):
        raise ValueError("Tilt must be finite")
    return result


def footprint_coupler_type(footprint) -> Optional[str]:
    """Return the coupler type, preferring the library entry name."""
    try:
        name = footprint.definition.id.name
        if name in COUPLER_MODELS:
            return name
    except Exception:
        pass
    try:
        name = footprint.value_field.text.value
        if name in COUPLER_MODELS:
            return name
    except Exception:
        pass
    return None


def _normalise_model_filename(filename) -> str:
    return str(filename or "").strip().replace("\\", "/")


def _helper_model_type(filename) -> Optional[str]:
    value = _normalise_model_filename(filename)
    for coupler_type, helper in COUPLER_MODELS.items():
        normalised = _normalise_model_filename(helper)
        if value == normalised:
            return coupler_type
        # Also recognise a reference expanded by KiCad, while requiring the
        # PCM package directory so an unrelated same-named model is retained.
        suffix = normalised.split("/3dmodels/", 1)[-1]
        if value.endswith(f"/3dmodels/{suffix}"):
            return coupler_type
    return None


def _new_helper_model(coupler_type: str, z: float, tilt: float, offset: float):
    from kipy.board_types import Footprint3DModel
    from kipy.geometry import Vector3D

    model = Footprint3DModel()
    model.filename = COUPLER_MODELS[coupler_type]
    model.scale = Vector3D.from_xyz(1.0, 1.0, 1.0)
    # Positive Offset follows the footprint triangle.  In KiCad's footprint
    # coordinates that is local -Y.  Z remains along the board normal and is
    # therefore applied independently of the local-X tilt.
    model.offset = Vector3D.from_xyz(0.0, -offset, z)
    model.rotation = Vector3D.from_xyz(tilt, 0.0, 0.0)
    model.visible = True
    model.opacity = 1.0
    return model


def _vector_matches(vector, expected, tolerance=1e-9) -> bool:
    return all(
        math.isclose(actual, wanted, abs_tol=tolerance)
        for actual, wanted in zip(
            (vector.x, vector.y, vector.z), expected)
    )


def _model_matches(model, expected) -> bool:
    return (
        _normalise_model_filename(model.filename)
        == _normalise_model_filename(expected.filename)
        and _vector_matches(model.scale, (1.0, 1.0, 1.0))
        and _vector_matches(
            model.offset,
            (expected.offset.x, expected.offset.y, expected.offset.z),
        )
        and _vector_matches(
            model.rotation,
            (expected.rotation.x, expected.rotation.y, expected.rotation.z),
        )
        and bool(model.visible)
        and math.isclose(model.opacity, 1.0, abs_tol=1e-9)
    )


def _replace_helper_models(footprint, helper) -> None:
    from kipy.board_types import Footprint3DModel

    footprint.definition.items = [
        item for item in footprint.definition.items
        if not (
            isinstance(item, Footprint3DModel)
            and _helper_model_type(item.filename) is not None
        )
    ]
    footprint.definition.add_item(helper)


def _hide_helper_models(footprint) -> int:
    hidden = 0
    for model in footprint.definition.models:
        if _helper_model_type(model.filename) is None or not model.visible:
            continue
        model.visible = False
        hidden += 1
    return hidden


def _push_updates(board, updates, message: str) -> None:
    if not updates:
        return
    commit = board.begin_commit()
    try:
        updated = board.update_items(updates)
        if len(updated) != len(updates):
            raise RuntimeError(
                f"KiCad updated {len(updated)} of {len(updates)} footprints")
        board.push_commit(commit, message)
    except Exception:
        board.drop_commit(commit)
        raise


def enable_update_coupler_helpers(kicad, board) -> EnableResult:
    """Add or update helper models using each coupler's transform fields."""
    footprints = list(board.get_footprints())
    result = EnableResult(scanned=len(footprints))
    couplers = []
    for footprint in footprints:
        coupler_type = footprint_coupler_type(footprint)
        if coupler_type is not None:
            couplers.append((footprint, coupler_type))
    result.couplers = len(couplers)

    variables = path_variables(kicad, board)
    missing = []
    for coupler_type in sorted({item[1] for item in couplers}):
        filename = COUPLER_MODELS[coupler_type]
        if resolve_model_path(filename, board, variables) is None:
            missing.append(filename.rsplit("/", 1)[-1])
    if missing:
        raise FileNotFoundError(
            f"Kikakuka {', '.join(missing)} was not found. Install the "
            "com.github.buganini.kikakuka-footprints library package first."
        )

    updates = []
    for footprint, coupler_type in couplers:
        properties = footprint_properties(footprint)
        try:
            z = parse_signed_length_mm(properties.get("Z"), "Z")
            offset = parse_signed_length_mm(
                properties.get("Offset"), "Offset")
            tilt = parse_tilt_degrees(properties.get("Tilt"))
        except ValueError as error:
            result.invalid += 1
            result.details.append(f"{footprint_label(footprint)}: {error}")
            continue

        expected = _new_helper_model(coupler_type, z, tilt, offset)
        existing = [
            model for model in footprint.definition.models
            if _helper_model_type(model.filename) is not None
        ]
        if len(existing) == 1 and _model_matches(existing[0], expected):
            result.unchanged += 1
            continue

        if existing:
            result.updated += 1
        else:
            result.added += 1
        _replace_helper_models(footprint, expected)
        updates.append(footprint)

    _push_updates(board, updates, "Update Coupler 3D Viewer helpers")
    return result


def hide_coupler_helpers(kicad, board) -> HideResult:
    """Hide Kikakuka coupler helper models without removing them."""
    del kicad  # Kept in the action API for symmetry with enable/update.
    footprints = list(board.get_footprints())
    result = HideResult(scanned=len(footprints))
    updates = []
    for footprint in footprints:
        hidden = _hide_helper_models(footprint)
        if hidden:
            result.changed += 1
            result.hidden += hidden
            updates.append(footprint)
    _push_updates(board, updates, "Hide coupler helper 3D models")
    return result
