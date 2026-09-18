#!/usr/bin/env python3

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "kikakuka-addon.zip"
SOURCE_METADATA = ROOT / "kicad-addon" / "metadata.json"
SOURCE_ICON = ROOT / "kicad-addon" / "resources" / "icon.png"
DOWNLOAD_BASE = "https://github.com/buganini/Kikakuka/releases/download"


def load_published_metadata(metadata_path, identifier):
    if not metadata_path.exists():
        return {}
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("identifier") != identifier:
        raise ValueError("existing metadata has a different identifier")
    return metadata


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "metadata_repository", type=Path,
        help="checkout of the KiCad addon metadata repository")
    args = parser.parse_args()

    package_metadata = json.loads(
        SOURCE_METADATA.read_text(encoding="utf-8"))
    package_versions = package_metadata.get("versions", [])
    if len(package_versions) != 1:
        raise ValueError(
            "package metadata must contain exactly one version")

    identifier = package_metadata["identifier"]
    current_version = dict(package_versions[0])
    version = current_version["version"]
    archive_data = ARCHIVE.read_bytes()
    with ZipFile(ARCHIVE) as package:
        install_size = sum(
            entry.file_size for entry in package.infolist()
            if not entry.is_dir())

    current_version.update({
        "download_url":
            f"{DOWNLOAD_BASE}/{version}/{ARCHIVE.name}",
        "download_sha256": hashlib.sha256(archive_data).hexdigest(),
        "download_size": len(archive_data),
        "install_size": install_size,
    })

    package_dir = (
        args.metadata_repository.resolve() / "packages" / identifier)
    publish_metadata = package_dir / "metadata.json"
    metadata = load_published_metadata(publish_metadata, identifier)
    previous_versions = metadata.get("versions", [])
    for key, value in package_metadata.items():
        if key != "versions":
            metadata[key] = value
    metadata["versions"] = [current_version] + [
        item for item in previous_versions
        if item.get("version") != version
    ]

    package_dir.mkdir(parents=True, exist_ok=True)
    publish_metadata.write_text(
        json.dumps(metadata, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8")
    shutil.copyfile(SOURCE_ICON, package_dir / "icon.png")
    print(f"Wrote {package_dir}")


if __name__ == "__main__":
    main()
