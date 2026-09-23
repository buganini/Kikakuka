#!/usr/bin/env python3
"""KiCad action entrypoint for hiding coupler helper models."""

import sys

from kipy import KiCad

from coupler_helpers import hide_coupler_helpers


def main() -> int:
    try:
        kicad = KiCad()
        board = kicad.get_board()
        result = hide_coupler_helpers(kicad, board)
        kicad.run_action("common.Control.show3DViewer")
    except Exception as error:
        print(
            f"Kikakuka: failed to hide couplers: {error}",
            file=sys.stderr,
        )
        return 1

    print(
        "Kikakuka: scanned {scanned} footprints; hid {hidden} helper "
        "models on {changed} footprints.".format(
            scanned=result.scanned,
            hidden=result.hidden,
            changed=result.changed,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
