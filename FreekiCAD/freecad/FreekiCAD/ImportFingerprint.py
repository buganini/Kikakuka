"""Read-only fingerprints of live KiCad import inputs (never persisted)."""

import hashlib
import json
import os

from .kicad_paths import path_variables, resolve_model_path


def file_state(path):
    """Include replacement/deletion as well as ordinary model edits."""
    path = os.path.realpath(path)
    try:
        stat = os.stat(path)
        return (path, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    except FileNotFoundError:
        return (path, None)


def read_fingerprint(kicad, board, filename, settings, fallback_color):
    """Hash unsaved PCB contents and external inputs without building geometry.

    Exceptions deliberately propagate: an incomplete snapshot must not authorize
    skipping a reload. SaveDocumentToString only serializes; it does not save.
    """
    contents = board.get_as_string()
    if not contents:
        raise ValueError("KiCad returned an empty board snapshot")
    variables = path_variables(kicad, board, source_path=__file__)
    models = {}
    for footprint in board.get_footprints():
        for model in footprint.definition.models:
            if not model.visible or model.filename in models:
                continue
            path = resolve_model_path(
                model.filename, board, variables, prefer_step=True)
            # Resolve even missing references on every check, so installing a
            # previously missing model (or a preferred STEP) invalidates cache.
            models[model.filename] = file_state(path) if path else None
    metadata = {
        'filename': os.path.realpath(filename),
        'settings': settings,
        'path_variables': variables,
        'models': models,
        'text_variables': board.get_project().get_text_variables().variables,
        'fallback_color': fallback_color,
        # Project settings can affect mask expansion and rendered text.
        'project_file': file_state(os.path.splitext(filename)[0] + '.kicad_pro'),
    }
    digest = hashlib.sha256(contents.encode('utf-8'))
    digest.update(json.dumps(metadata, sort_keys=True).encode('utf-8'))
    return digest.hexdigest()
