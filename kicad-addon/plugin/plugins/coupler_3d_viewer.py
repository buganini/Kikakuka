#!/usr/bin/env python3
"""KiCad action entrypoint for viewing transformed coupler helpers."""

import sys

from kipy import KiCad

from coupler_helpers import enable_update_coupler_helpers


def main() -> int:
    try:
        kicad = KiCad()
        board = kicad.get_board()
        result = enable_update_coupler_helpers(kicad, board)
        kicad.run_action("common.Control.show3DViewer")
    except Exception as error:
        print(f"Kikakuka Coupler 3D Viewer: {error}", file=sys.stderr)
        return 1

    print(
        "Kikakuka: scanned {scanned} footprints and found {couplers} "
        "couplers; added {added}, updated {updated}, left {unchanged} "
        "unchanged, and skipped {invalid} with invalid properties.".format(
            scanned=result.scanned,
            couplers=result.couplers,
            added=result.added,
            updated=result.updated,
            unchanged=result.unchanged,
            invalid=result.invalid,
        )
    )
    for detail in result.details:
        print(f"Kikakuka: skipped {detail}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
