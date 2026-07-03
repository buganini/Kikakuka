#!/usr/bin/env python3
import argparse
import gc
import time
from pathlib import Path

import pcbnew
import psutil


def rss_mb(process):
    return process.memory_info().rss / 1024 / 1024


def print_stats(iteration, current, baseline, previous, elapsed):
    print(
        "[STATS] "
        f"iter={iteration} "
        f"rss_mb={current:.2f} "
        f"delta_mb={current - baseline:.2f} "
        f"iter_delta_mb={current - previous:.2f} "
        f"seconds={elapsed:.4f}",
        flush=True,
    )


def duplicate_item(item):
    try:
        return item.Duplicate()
    except TypeError:
        return pcbnew.Cast_to_BOARD_ITEM(item).Duplicate().Cast()


def append_board_pcbnew(panel, source, translation):
    for footprint in source.GetFootprints():
        new_item = duplicate_item(footprint)
        new_item.Move(translation)
        panel.Add(new_item)

    for track in source.GetTracks():
        new_item = duplicate_item(track)
        new_item.Move(translation)
        panel.Add(new_item)

    for drawing in source.GetDrawings():
        new_item = duplicate_item(drawing)
        new_item.Move(translation)
        panel.Add(new_item)

    for zone in source.Zones():
        new_item = duplicate_item(zone)
        new_item.Move(translation)
        panel.Add(new_item)


def run_once(board_path, panel_path, copies, spacing, save):
    panel = pcbnew.NewBoard(str(panel_path))
    try:
        for i in range(copies):
            source = pcbnew.LoadBoard(str(board_path))
            try:
                append_board_pcbnew(panel, source, pcbnew.VECTOR2I(round(i * spacing), 0))
            finally:
                source = None
        if save:
            pcbnew.SaveBoard(str(panel_path), panel)
    finally:
        panel = None


def main():
    parser = argparse.ArgumentParser(
        description="Measure RSS while repeatedly loading boards and appending their items with pcbnew only."
    )
    parser.add_argument(
        "board",
        nargs="?",
        default="samples/L.kicad_pcb",
        help="KiCad board to append repeatedly.",
    )
    parser.add_argument(
        "-n",
        "--iterations",
        type=int,
        default=50,
        help="Number of panel builds to run.",
    )
    parser.add_argument(
        "-c",
        "--copies",
        type=int,
        default=1,
        help="Number of source boards to load and append per panel build.",
    )
    parser.add_argument(
        "--spacing",
        type=float,
        default=100.0,
        help="Horizontal spacing between copies, in mm.",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save the generated panel board. Disabled by default to isolate load/append work.",
    )
    parser.add_argument(
        "--keep-garbage",
        action="store_true",
        help="Do not call gc.collect() after each iteration.",
    )
    parser.add_argument(
        "--post-gc-sleep",
        type=float,
        default=10.0,
        help="Seconds to sleep after the final gc.collect() before sampling RSS.",
    )
    parser.add_argument(
        "--out-dir",
        default="tmp/memory_appendboard",
        help="Directory for temporary panel filenames.",
    )
    args = parser.parse_args()

    board_path = Path(args.board).resolve()
    if not board_path.exists():
        raise SystemExit(f"Board not found: {board_path}")

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    process = psutil.Process()
    baseline = rss_mb(process)

    print(f"board={board_path}")
    print(f"out_dir={out_dir}")
    print(f"iterations={args.iterations} copies={args.copies} save={args.save}")

    previous = baseline
    spacing = pcbnew.FromMM(args.spacing)
    for i in range(1, args.iterations + 1):
        panel_path = out_dir / f"panel_{i:04d}.kicad_pcb"
        start = time.perf_counter()
        run_once(board_path, panel_path, args.copies, spacing, args.save)
        if not args.keep_garbage:
            gc.collect()
        elapsed = time.perf_counter() - start
        current = rss_mb(process)
        print_stats(i, current, baseline, previous, elapsed)
        previous = current

    start = time.perf_counter()
    gc.collect()
    if args.post_gc_sleep > 0:
        time.sleep(args.post_gc_sleep)
    elapsed = time.perf_counter() - start
    current = rss_mb(process)
    print_stats("final", current, baseline, previous, elapsed)


if __name__ == "__main__":
    main()
