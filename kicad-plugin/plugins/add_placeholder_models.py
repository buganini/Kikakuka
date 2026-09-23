#!/usr/bin/env python3
"""KiCad action entrypoint for adding placeholder 3D models."""

import sys

from kipy import KiCad

from placeholder_models import add_placeholder_models


def main() -> int:
    try:
        kicad = KiCad()
        board = kicad.get_board()
        result = add_placeholder_models(kicad, board)
        kicad.run_action("common.Control.show3DViewer")
    except Exception as error:
        print(f"Kikakuka: failed to add placeholder models: {error}", file=sys.stderr)
        return 1

    print(
        "Kikakuka: scanned {scanned} footprints; added {added} placeholder "
        "models; {valid} already had a valid model; {missing} lacked size "
        "properties; {invalid} had invalid sizes.".format(
            scanned=result.scanned,
            added=result.added,
            valid=result.already_valid,
            missing=result.missing_size,
            invalid=result.invalid_size,
        )
    )
    for detail in result.details:
        print(f"Kikakuka: skipped {detail}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
