"""Small, dependency-free parsers for KiCad annotation quantities."""

import re


_LENGTH_RE = re.compile(
    r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
    r"(?:\s*(mm|cm|in|mil|um|µm|μm))?\s*",
    re.IGNORECASE,
)


def parse_length_mm(value, quantity_name="length"):
    """Parse *value* as millimetres.

    Supported suffixes are ``mm``, ``cm``, ``in``, ``mil``, and ``um``.  One mil is
    0.001 inch.  The two common Unicode spellings of the micrometre symbol are
    accepted as aliases for ``um``.  A missing suffix means millimetres.
    """
    if value is None:
        raise ValueError(
            f"missing {quantity_name}; expected mm, cm, in, mil, or um")
    match = _LENGTH_RE.fullmatch(str(value))
    if match is None:
        raise ValueError(
            f"invalid {quantity_name} value {value!r}; "
            "expected mm, cm, in, mil, or um"
        )
    result = float(match.group(1))
    unit = (match.group(2) or "mm").lower()
    if unit == "cm":
        result *= 10.0
    elif unit == "in":
        result *= 25.4
    elif unit == "mil":
        result *= 0.0254
    elif unit in ("um", "µm", "μm"):
        result /= 1000.0
    return result
