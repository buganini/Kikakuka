"""Prepare KiCad and Gerber inputs for Differ."""

import os
import tempfile

from gerber import convert_to_kicad, is_gerber_dir, is_gerber_zip


def prepare_differ_source(source, side, temp_dir):
    source = os.path.abspath(source)
    if not (is_gerber_dir(source) or is_gerber_zip(source)):
        return source, []

    output_dir = tempfile.mkdtemp(prefix=f"gerber-{side}-", dir=temp_dir)
    source_name = os.path.basename(os.path.normpath(source))
    if is_gerber_zip(source):
        source_name = os.path.splitext(source_name)[0]
    output = os.path.join(
        output_dir, f"{source_name or 'gerber'}.kicad_pcb")
    errors = convert_to_kicad(
        source, output, required_edge_cuts=False, differ_mode=True)
    return output, errors
