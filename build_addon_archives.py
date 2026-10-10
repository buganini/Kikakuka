"""Build the release and embedded addon ZIP archives."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parent


def _git_symlinks() -> set[Path]:
    """Identify path-text placeholders without guessing from file contents."""
    if not (ROOT / ".git").exists():
        return set()
    result = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT.as_posix()}", "ls-files", "--stage", "-z"],
        cwd=ROOT, check=True, stdout=subprocess.PIPE,
    )
    links = set()
    for record in result.stdout.split(b"\0"):
        if record:
            metadata, name = record.split(b"\t", 1)
            if metadata.split()[0] == b"120000":
                links.add(ROOT / name.decode("utf-8"))
    return links


def _dereference(source: Path, links: set[Path]) -> Path:
    seen = set()
    while source.is_symlink() or source.absolute() in links:
        source = source.absolute()
        if source in seen:
            raise ValueError(f"Symlink cycle: {source}")
        seen.add(source)
        target = (source.readlink() if source.is_symlink()
                  else Path(source.read_text(encoding="utf-8")))
        source = Path(os.path.abspath(source.parent / target))
    if not source.exists():
        raise FileNotFoundError(f"Missing archive source or symlink target: {source}")
    return source


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
    links: set[Path] | None = None,
    overrides: set[Path] | None = None,
    ancestors: frozenset[Path] = frozenset(),
) -> None:
    """Store target contents, including Git's regular-file link placeholders."""
    links = _git_symlinks() if links is None else links
    source = _dereference(source, links)
    if source.is_dir():
        identity = source.resolve()
        if identity in ancestors:
            raise ValueError(f"Directory symlink cycle: {source}")
        for child in sorted(source.iterdir()):
            destination = archive_root / child.name
            if _excluded(destination) or destination in (overrides or set()):
                continue
            _add_tree(archive, child, destination, links, overrides,
                      ancestors | {identity})
    else:
        archive.write(source, archive_root.as_posix())


def _build_archive(
    output: Path,
    entries: list[tuple[Path, Path]],
    generated: dict[Path, str] | None = None,
) -> None:
    output.unlink(missing_ok=True)
    links = _git_symlinks()
    overrides = {destination for _, destination in entries}
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for source, archive_path in entries:
            _add_tree(archive, source, archive_path, links, overrides)
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
            (ROOT / "cadhoc", Path("plugins/cadhoc")),
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
