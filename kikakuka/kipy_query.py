#!/usr/bin/env python3
"""Print information about a KiCad IPC API socket."""

import argparse
import os
import sys


def _socket_endpoint(value):
    if value.startswith("ipc://"):
        return value
    return f"ipc://{value}"


def _open_file_path(board, project):
    board_name = getattr(board, "name", "") or getattr(
        getattr(board, "document", None), "board_filename", ""
    )
    if not board_name:
        return None
    if os.path.isabs(board_name):
        return os.path.abspath(board_name)

    project_path = getattr(project, "path", "")
    if project_path:
        return os.path.abspath(os.path.join(project_path, board_name))
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Query a KiCad IPC socket and print its open board path."
    )
    parser.add_argument("socket", help="socket path, with or without the ipc:// prefix")
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=2000,
        help="KiCad API timeout in milliseconds (default: 2000)",
    )
    args = parser.parse_args(argv)

    endpoint = _socket_endpoint(args.socket)

    try:
        from kipy.kicad import KiCad
    except ImportError as exc:
        print(f"error: cannot import kipy: {exc}", file=sys.stderr)
        return 2

    try:
        kicad = KiCad(socket_path=endpoint, timeout_ms=args.timeout_ms)
        kicad_version = kicad.get_version()
        api_version = kicad.get_api_version()
        board = kicad.get_board()
        project = board.get_project()
    except Exception as exc:
        print(f"error: KiCad API query failed: {exc}", file=sys.stderr)
        return 1

    print(f"Endpoint:    {endpoint}")
    print(f"KiCad:       {kicad_version}")
    print(f"API:         {api_version}")
    print(f"Board:       {getattr(board, 'name', '') or '(unnamed)'}")
    print(f"Project:     {getattr(project, 'name', '') or '(unnamed)'}")
    print(f"Project dir: {getattr(project, 'path', '') or '(unknown)'}")
    print(f"Open file:   {_open_file_path(board, project) or '(unknown)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
