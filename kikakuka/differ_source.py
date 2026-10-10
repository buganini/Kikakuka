"""Resolve the source and export paths for a differ file selection."""

import hashlib
import os


def source_paths(filepath, repo_root, revision, temp_dir):
    """Return (export directory, selected source path, display path).

    Historical files live in the same checkout directory used by the diff
    exporter. The display path is relative to the repository for revisions.
    """
    key = hashlib.sha256(filepath.encode("utf-8")).hexdigest()
    if not revision:
        return os.path.join(temp_dir, key), filepath, filepath

    if not repo_root:
        raise ValueError("repository not found for selected revision")
    relative_path = os.path.relpath(filepath, repo_root)
    export_dir = os.path.join(temp_dir, f"{key}_{revision}")
    return (
        export_dir,
        os.path.join(export_dir, "workdir", relative_path),
        relative_path.replace("\\", "/"),
    )
