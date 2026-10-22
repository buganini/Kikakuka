"""Kikakuka CLI adapters for CADhoc editor opening."""

import os
from cadhoc.editor_open import open_kicad_file, open_with_system

def open_pcb_file(filepath, ensure_fresh=False):
    """PCB-only entry point used by the panelizer."""
    if not os.fspath(filepath).lower().endswith(".kicad_pcb"):
        raise ValueError("expected a .kicad_pcb file")
    return open_kicad_file(filepath, ensure_fresh=ensure_fresh)


def parse_open_arguments(arguments):
    """Parse the top-level ``--open``/``--fresh`` command-line mode.

    Return ``None`` when this is not an open request, otherwise return a
    ``(filepaths, ensure_fresh)`` tuple.  ``--open`` is a mode selector and
    must therefore be the first argument.
    """
    arguments = list(arguments)
    if not arguments or arguments[0] != "--open":
        if "--open" in arguments:
            raise ValueError("--open must be the first argument")
        if "--fresh" in arguments:
            raise ValueError("--fresh requires --open")
        return None
    if "--open" in arguments[1:]:
        raise ValueError("--open may only be specified once")

    unknown = [
        argument for argument in arguments
        if argument.startswith("-")
        and argument not in {"--open", "--fresh"}
    ]
    if unknown:
        raise ValueError(
            f"unknown option for --open: {unknown[0]}")

    filepaths = [
        argument for argument in arguments
        if argument not in {"--open", "--fresh"}
    ]
    if not filepaths:
        raise ValueError("--open requires at least one KiCad file")
    return filepaths, "--fresh" in arguments


def open_requested_kicad_files(arguments):
    """Handle the CLI open mode, returning whether it was selected."""
    request = parse_open_arguments(arguments)
    if request is None:
        return False
    filepaths, ensure_fresh = request
    pcb_paths = {
        filepath for filepath in filepaths
        if os.fspath(filepath).lower().endswith(".kicad_pcb")
    }
    if ensure_fresh and not pcb_paths:
        raise ValueError("--fresh requires at least one .kicad_pcb file")
    for filepath in filepaths:
        open_kicad_file(
            filepath,
            ensure_fresh=ensure_fresh and filepath in pcb_paths,
        )
    return True
