#!/usr/bin/env python3
import argparse
import subprocess
import sys
from pathlib import Path


PANEL_SUFFIXES = (".kikit_pnl", ".kiki_pnl")
GERBER_SAMPLE_DIR = Path("samples/gerber/export")
DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".claude",
    ".history",
    "__pycache__",
    "build",
    "dist",
    "env",
    "tmp",
}


def discover_panels(root, exclude_dirs):
    panels = []
    for path in root.rglob("*"):
        if any(part in exclude_dirs for part in path.relative_to(root).parts[:-1]):
            continue
        if path.is_file() and path.suffix in PANEL_SUFFIXES:
            panels.append(path)
    return sorted(panels)


def output_path_for(panel, root, out_dir):
    relative = panel.relative_to(root)
    stem = "__".join(relative.with_suffix("").parts)
    return out_dir / f"{stem}.kicad_pcb"


def run_kikakuka(python, kikakuka, inputs, output, timeout):
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [str(python), str(kikakuka), *[str(path) for path in inputs], str(output)]
    return subprocess.run(
        command,
        cwd=kikakuka.parent,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def check_output(output):
    if not output.exists() or output.stat().st_size == 0:
        return "did not create a non-empty .kicad_pcb"
    return None


def main():
    repo_root = Path(__file__).resolve().parents[1]

    parser = argparse.ArgumentParser(
        description="Run headless conversions for the repository's sample files."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=repo_root,
        help="Directory to search for panel files.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=repo_root / "tmp" / "all_samples",
        help="Directory for exported .kicad_pcb files.",
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="Python executable used to run kikakuka.py.",
    )
    parser.add_argument(
        "--kikakuka",
        type=Path,
        default=repo_root / "kikakuka.py",
        help="Path to kikakuka.py.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Per-file timeout in seconds.",
    )
    parser.add_argument(
        "--include-generated",
        action="store_true",
        help="Also search generated/cache directories such as build, dist, env, tmp, and .claude.",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    out_dir = args.out_dir.resolve()
    kikakuka = args.kikakuka.resolve()
    exclude_dirs = set() if args.include_generated else DEFAULT_EXCLUDE_DIRS

    panels = discover_panels(root, exclude_dirs)
    if not panels:
        raise SystemExit(f"No panel files found under {root}")

    print(f"root={root}")
    print(f"out_dir={out_dir}")
    print(f"panels={len(panels)}")

    failures = []
    for index, panel in enumerate(panels, start=1):
        output = output_path_for(panel, root, out_dir)
        print(f"[{index}/{len(panels)}] {panel.relative_to(root)} -> {output}")
        try:
            result = run_kikakuka(args.python, kikakuka, [panel.resolve()], output, args.timeout)
        except subprocess.TimeoutExpired as exc:
            failures.append((panel, f"timed out after {exc.timeout} seconds", "", exc.stderr or ""))
            print("  FAIL timeout")
            continue

        if result.returncode != 0:
            failures.append((panel, f"exit code {result.returncode}", result.stdout, result.stderr))
            print(f"  FAIL exit code {result.returncode}")
            continue

        output_error = check_output(output)
        if output_error:
            failures.append((panel, f"export {output_error}", result.stdout, result.stderr))
            print("  FAIL missing or empty output")
            continue

        print(f"  OK {output.stat().st_size} bytes")

    gerber_dir = root / GERBER_SAMPLE_DIR
    gerber_output = out_dir / "samples__gerber__export.kicad_pcb"
    print(f"[gerber] {GERBER_SAMPLE_DIR} -> {gerber_output}")
    if not gerber_dir.is_dir():
        failures.append((gerber_dir, "sample gerber export directory does not exist", "", ""))
        print("  FAIL missing input directory")
    else:
        try:
            result = run_kikakuka(args.python, kikakuka, [gerber_dir.resolve()], gerber_output, args.timeout)
        except subprocess.TimeoutExpired as exc:
            failures.append((gerber_dir, f"timed out after {exc.timeout} seconds", "", exc.stderr or ""))
            print("  FAIL timeout")
        else:
            if result.returncode != 0:
                failures.append((gerber_dir, f"exit code {result.returncode}", result.stdout, result.stderr))
                print(f"  FAIL exit code {result.returncode}")
            else:
                output_error = check_output(gerber_output)
                if output_error:
                    failures.append((gerber_dir, f"conversion {output_error}", result.stdout, result.stderr))
                    print("  FAIL missing or empty output")
                else:
                    print(f"  OK {gerber_output.stat().st_size} bytes")

    if failures:
        print()
        print("Failures:")
        for panel, reason, stdout, stderr in failures:
            print(f"- {panel}: {reason}")
            if stdout:
                print("  stdout:")
                print(indent(stdout.rstrip(), "    "))
            if stderr:
                print("  stderr:")
                print(indent(stderr.rstrip(), "    "))
        raise SystemExit(1)

    print()
    print("All sample conversions passed.")


def indent(text, prefix):
    return "\n".join(f"{prefix}{line}" for line in text.splitlines())


if __name__ == "__main__":
    main()
