#!/usr/bin/env python3
"""Prepare the fpc2 sample for the placeholder 3D model recording scenario."""

from pathlib import Path


BOARD_PATH = Path("samples/fpc2.kicad_pcb")
TARGET_REFERENCE = "missing-3d-model"
REMOVED_PROPERTIES = ("SizeX", "SizeY", "SizeZ")
EXPECTED_PAD_COUNT = 17


def remove_property_block(lines, index):
    """Return the index after one footprint property block."""
    index += 1

    while index < len(lines) and lines[index] != "\t\t)\n":
        index += 1

    if index >= len(lines):
        raise RuntimeError("unterminated property block")

    return index + 1


def main():
    lines = BOARD_PATH.read_text().splitlines(True)
    output = []
    changed_reference = False
    removed = []

    index = 0
    while index < len(lines):
        line = lines[index]

        if (
            not changed_reference
            and line.startswith('\t\t(property "Reference" "REF**"')
        ):
            line = line.replace(
                '(property "Reference" "REF**"',
                f'(property "Reference" "{TARGET_REFERENCE}"',
            )
            changed_reference = True

        if any(
            line.startswith(f'\t\t(property "{name}"')
            for name in REMOVED_PROPERTIES
        ):
            removed.append(line.split('"')[1])
            index = remove_property_block(lines, index)
            continue

        output.append(line)
        index += 1

    text = "".join(output)
    pad_count = text.count("\n\t\t(pad ")

    if not changed_reference:
        raise RuntimeError("did not change REF** to missing-3d-model")
    if sorted(removed) != sorted(REMOVED_PROPERTIES):
        raise RuntimeError(f"unexpected removed properties: {removed}")
    if text.count(f'(property "Reference" "{TARGET_REFERENCE}"') != 1:
        raise RuntimeError("prepared board must contain one target reference")
    if any(f'(property "{name}"' in text for name in REMOVED_PROPERTIES):
        raise RuntimeError("prepared board still contains size properties")
    if pad_count != EXPECTED_PAD_COUNT:
        raise RuntimeError(
            f"prepared board has {pad_count} pads, expected {EXPECTED_PAD_COUNT}"
        )

    BOARD_PATH.write_text(text)
    print(f"prepared {BOARD_PATH}")
    print(f"removed: {', '.join(removed)}")
    print(f"pad count: {pad_count}")


if __name__ == "__main__":
    main()
