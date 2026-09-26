"""Build the release and embedded addon ZIP archives."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parent


def _json_version(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    return str(data["versions"][0]["version"])


def _freecad_version(path: Path) -> str:
    value = ET.parse(path).getroot().findtext("{*}version")
    if not value:
        raise ValueError(f"No version in {path}")
    return value.strip()


def _excluded(relative: Path) -> bool:
    return any(part in {".git", "__pycache__"} for part in relative.parts) or (
        relative.name == ".DS_Store" or relative.suffix == ".pyc"
    )


def _add_tree(
    archive: zipfile.ZipFile,
    source: Path,
    archive_root: Path = Path(),
) -> None:
    """Add a tree in lexical order, following links into regular ZIP files."""
    for current, directories, files in os.walk(source, followlinks=True):
        current_path = Path(current)
        relative_dir = current_path.relative_to(source)
        directories[:] = sorted(
            directory
            for directory in directories
            if not _excluded(relative_dir / directory)
        )
        for filename in sorted(files):
            relative = relative_dir / filename
            if _excluded(relative):
                continue
            # ZipFile.write() uses stat(), not lstat(), so file links are
            # dereferenced and consumers receive ordinary files.
            archive.write(current_path / filename, (archive_root / relative).as_posix())


def _build_archive(
    output: Path,
    entries: list[tuple[Path, Path]],
    generated: dict[Path, str] | None = None,
) -> None:
    output.unlink(missing_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for source, archive_path in entries:
            if source.is_dir():
                _add_tree(archive, source, archive_path)
            else:
                archive.write(source, archive_path.as_posix())
        for archive_path, content in (generated or {}).items():
            archive.writestr(archive_path.as_posix(), content + "\n")


def build() -> list[Path]:
    library_version = _json_version(ROOT / "kicad-addon/library/metadata.json")
    plugin_version = _json_version(ROOT / "kicad-addon/plugin/metadata.json")
    freecad_version = _freecad_version(ROOT / "FreekiCAD/package.xml")

    library = ROOT / f"kikakuka-library-{library_version}.zip"
    plugin = ROOT / f"kikakuka-plugin-{plugin_version}.zip"
    freecad = ROOT / f"freekicad-{freecad_version}.zip"

    library_root = ROOT / "kicad-addon/library"
    _build_archive(
        library,
        [
            (library_root / "metadata.json", Path("metadata.json")),
            (library_root / "footprints", Path("footprints")),
            (library_root / "3dmodels", Path("3dmodels")),
            (library_root / "resources", Path("resources")),
        ],
        {Path("footprints/.kikakuka-version"): library_version},
    )

    plugin_root = ROOT / "kicad-addon/plugin"
    _build_archive(
        plugin,
        [
            (plugin_root / "metadata.json", Path("metadata.json")),
            (plugin_root / "plugins", Path("plugins")),
            (plugin_root / "resources", Path("resources")),
        ],
        {Path("plugins/.kikakuka-version"): plugin_version},
    )

    _build_archive(freecad, [(ROOT / "FreekiCAD", Path())])

    embedded = ROOT / "build/addons"
    embedded.mkdir(parents=True, exist_ok=True)
    shutil.copy2(library, embedded / "kicad-library.zip")
    shutil.copy2(plugin, embedded / "kicad-plugin.zip")
    shutil.copy2(freecad, embedded / "freekicad.zip")
    return [library, plugin, freecad]


if __name__ == "__main__":
    for archive in build():
        print(archive)
