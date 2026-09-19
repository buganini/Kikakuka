#!/usr/bin/env python3
import argparse
import glob
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from legacy_pcb_diff import PcbLegacyRenderer
from pcb_diff_tiles import (
    PcbTileRenderer,
    build_pair_metadata,
    choose_render_scale,
    visible_tile_indices,
)


def find_kicad_cli():
    if platform.system() == "Darwin":
        return "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    if platform.system() == "Windows":
        candidates = glob.glob("C:/Program Files/KiCad/*/bin/kicad-cli.exe")
        return candidates[0] if candidates else "kicad-cli.exe"
    return "/usr/bin/kicad-cli"


def board_layers(path):
    layers = []
    in_layers = False
    with open(path, "r", encoding="utf-8") as board_file:
        for line in board_file:
            stripped = line.strip()
            if not in_layers:
                if stripped == "(layers":
                    in_layers = True
                continue
            if stripped == ")":
                break
            if not re.match(r"^\(\d+\s+", stripped):
                continue
            names = re.findall(r'"((?:[^"\\]|\\.)*)"', stripped)
            if names:
                layers.append(names[-1])
    if not layers:
        raise RuntimeError(f"Could not read PCB layer table from {path}")
    return layers


def export_layer_pdfs(kicad_cli, board_path, layers, cache_dir):
    output_dir = os.path.join(cache_dir, "pcb_pdf")
    started = time.perf_counter()
    subprocess.run([
        kicad_cli,
        "pcb", "export", "pdf",
        "--mode-separate",
        "--layers", ",".join(layers),
        "-o", output_dir,
        str(board_path),
    ], check=True, stdout=subprocess.DEVNULL)
    return time.perf_counter() - started


def directory_size(path):
    return sum(
        os.path.getsize(os.path.join(root, filename))
        for root, _directories, filenames in os.walk(path)
        for filename in filenames
    )


def format_size(size):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024


def benchmark(args, root):
    cache_a = os.path.join(root, "cache_a")
    cache_b = os.path.join(root, "cache_b")
    layers_a = board_layers(args.board_a)
    layers_b = board_layers(args.board_b)
    layers = list(layers_a)
    layers.extend(layer for layer in layers_b if layer not in layers)

    kicad_cli = args.kicad_cli or find_kicad_cli()
    export_a = export_layer_pdfs(
        kicad_cli, args.board_a, layers_a, cache_a
    )
    export_b = export_layer_pdfs(
        kicad_cli, args.board_b, layers_b, cache_b
    )
    metadata = build_pair_metadata(cache_a, cache_b, layers)

    legacy_output = os.path.join(root, "legacy")
    legacy = PcbLegacyRenderer()
    started = time.perf_counter()
    legacy.render_full_page(
        metadata,
        layers,
        legacy_output,
        render_scale=args.legacy_scale,
    )
    legacy_elapsed = time.perf_counter() - started
    legacy.close()

    width, height = metadata["canvas_size"]
    view_scale = min(
        args.viewport_width / width,
        args.viewport_height / height,
    ) * 0.75
    render_scale = choose_render_scale(
        view_scale, pixel_density=args.pixel_density
    )
    transform = (
        (args.viewport_width - width * view_scale) / 2.0,
        (args.viewport_height - height * view_scale) / 2.0,
        view_scale,
    )
    tiles = visible_tile_indices(
        metadata["canvas_size"],
        (args.viewport_width, args.viewport_height),
        transform,
        render_scale,
    )

    viewport_output = os.path.join(root, "viewport")
    viewport = PcbTileRenderer()
    tile_times = []
    started = time.perf_counter()
    for tile_x, tile_y in tiles:
        tile_started = time.perf_counter()
        viewport.render_tile(
            metadata,
            layers,
            render_scale,
            tile_x,
            tile_y,
            os.path.join(viewport_output, f"{tile_x}_{tile_y}"),
        )
        tile_times.append(time.perf_counter() - tile_started)
    viewport_elapsed = time.perf_counter() - started
    viewport.close()

    print(f"Boards: {args.board_a} / {args.board_b}")
    print(f"Layers: {len(layers)}")
    print(f"PDF export (excluded): {export_a + export_b:.3f}s")
    print(
        f"Legacy full page ({args.legacy_scale:g}x): "
        f"{legacy_elapsed:.3f}s, {format_size(directory_size(legacy_output))}"
    )
    print(
        f"Viewport {args.viewport_width}x{args.viewport_height} "
        f"(density {args.pixel_density:g}x, {render_scale:g}x LOD, "
        f"{len(tiles)} tiles): "
        f"{viewport_elapsed:.3f}s, "
        f"{format_size(directory_size(viewport_output))}"
    )
    if tile_times:
        print(f"First viewport tile: {tile_times[0]:.3f}s")
    if viewport_elapsed:
        print(f"Visible-area speedup: {legacy_elapsed / viewport_elapsed:.2f}x")


def main():
    parser = argparse.ArgumentParser(
        description="Compare legacy and viewport PCB differ renderers."
    )
    parser.add_argument("board_a", type=Path)
    parser.add_argument("board_b", type=Path)
    parser.add_argument("--kicad-cli")
    parser.add_argument("--legacy-scale", type=float, default=7.0)
    parser.add_argument("--viewport-width", type=int, default=1200)
    parser.add_argument("--viewport-height", type=int, default=800)
    parser.add_argument("--pixel-density", type=float, default=1.0)
    parser.add_argument(
        "--output",
        type=Path,
        help="Keep PDFs and rendered artifacts in this directory.",
    )
    args = parser.parse_args()

    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        benchmark(args, str(args.output))
    else:
        with tempfile.TemporaryDirectory(
                prefix="kikakuka_differ_benchmark_") as root:
            benchmark(args, root)


if __name__ == "__main__":
    main()
