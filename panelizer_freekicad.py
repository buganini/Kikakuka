"""Normalize FreekiCAD bend drawings while assembling KiCad panels."""

import os

from FreekiCAD.FreekiCAD.constants import FREEKICAD_LAYER_NAME


def find_freekicad_layer(board, all_layers,
                          expected_name=FREEKICAD_LAYER_NAME):
    """Return the one layer named *expected_name* on *board*, if present."""
    expected = str(expected_name).strip().lower()
    matches = [
        layer for layer in all_layers
        if str(board.GetLayerName(layer)).strip().lower() == expected
    ]
    if len(matches) > 1:
        names = ", ".join(str(int(layer)) for layer in matches)
        raise ValueError(
            f"multiple layers are named {expected_name!r}: {names}")
    return matches[0] if matches else None


def _drawing_layer_ids(board):
    return {int(item.GetLayer()) for item in board.GetDrawings()}


def choose_panel_freekicad_layer(boards, all_layers, user_layers,
                                 expected_name=FREEKICAD_LAYER_NAME,
                                 reserved_layers=()):
    """Choose one collision-free panel layer for all FreekiCAD drawings.

    A candidate is unsafe when a source board uses that numeric layer for
    ordinary drawings while naming a different layer FreekiCAD.
    """
    source_layers = [
        find_freekicad_layer(board, all_layers, expected_name)
        for board in boards
    ]
    if not any(layer is not None for layer in source_layers):
        return None, source_layers

    candidates = []
    seen = set()
    reserved_ids = {int(layer) for layer in reserved_layers}
    user_layer_ids = {int(layer) for layer in user_layers}
    source_user_layers = [
        layer for layer in source_layers
        if layer is not None and int(layer) in user_layer_ids
    ]
    for layer in [*source_user_layers, *user_layers]:
        if (layer is None
                or int(layer) in seen
                or int(layer) in reserved_ids):
            continue
        seen.add(int(layer))
        candidates.append(layer)

    used_layers = [_drawing_layer_ids(board) for board in boards]
    for candidate in candidates:
        candidate_id = int(candidate)
        if all(
                source is not None and int(source) == candidate_id
                or candidate_id not in used
                for source, used in zip(source_layers, used_layers)):
            return candidate, source_layers

    raise ValueError(
        "no collision-free User layer is available for FreekiCAD bends")


def remap_freekicad_drawings(board, source_layer, target_layer,
                              standard_layer_name,
                              expected_name=FREEKICAD_LAYER_NAME):
    """Move one board's bend drawings onto the panel canonical layer."""
    if source_layer is None:
        return 0

    source_id = int(source_layer)
    target_id = int(target_layer)
    moved = 0
    for drawing in board.GetDrawings():
        if int(drawing.GetLayer()) != source_id:
            continue
        if source_id != target_id:
            drawing.SetLayer(target_layer)
        moved += 1

    if source_id != target_id:
        board.SetLayerName(
            source_layer, standard_layer_name(source_layer))
    board.SetLayerName(target_layer, expected_name)
    return moved


def prepare_panel_freekicad_sources(source_paths, temp_dir, pcbnew_module,
                                    expected_name=FREEKICAD_LAYER_NAME,
                                    reserved_layers=()):
    """Return normalized source paths and the selected canonical layer."""
    boards = []
    for path in source_paths:
        board = pcbnew_module.LoadBoard(str(path))
        if board is None:
            raise ValueError(f"could not load source board: {path}")
        boards.append(board)
    all_layers = list(pcbnew_module.LSET.AllLayersMask().Seq())
    user_layers = list(pcbnew_module.LSET.UserDefinedLayersMask().Seq())
    canonical, source_layers = choose_panel_freekicad_layer(
        boards, all_layers, user_layers, expected_name, reserved_layers)
    if canonical is None:
        return list(source_paths), None, []

    normalized_paths = []
    moved_counts = []
    for index, (source_path, board, source_layer) in enumerate(
            zip(source_paths, boards, source_layers)):
        moved = remap_freekicad_drawings(
            board,
            source_layer,
            canonical,
            pcbnew_module.BOARD.GetStandardLayerName,
            expected_name)
        moved_counts.append(moved)
        if source_layer is None:
            normalized_paths.append(source_path)
            continue

        enabled = board.GetEnabledLayers()
        enabled.addLayer(canonical)
        board.SetEnabledLayers(enabled)
        normalized_path = os.path.join(
            temp_dir, f"freekicad-source-{index}.kicad_pcb")
        if not pcbnew_module.SaveBoard(normalized_path, board, True):
            raise OSError(
                f"could not save normalized source board: {source_path}")
        normalized_paths.append(normalized_path)

    return normalized_paths, canonical, moved_counts
