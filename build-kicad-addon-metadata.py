#!/usr/bin/env python3

import hashlib
import json
import shutil
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "kikakuka-addon.zip"
SOURCE_METADATA = ROOT / "kicad-addon" / "metadata.json"
SOURCE_ICON = ROOT / "kicad-addon" / "resources" / "icon.png"
PUBLISH_ROOT = ROOT / "kicad-addon-publish"
DOWNLOAD_BASE = "https://github.com/buganini/Kikakuka/releases/download"


def main():
    metadata = json.loads(SOURCE_METADATA.read_text(encoding="utf-8"))
    versions = metadata.get("versions", [])
    if len(versions) != 1:
        raise ValueError(
            "package metadata must contain exactly one version")

    identifier = metadata["identifier"]
    version = versions[0]["version"]
    archive_data = ARCHIVE.read_bytes()
    with ZipFile(ARCHIVE) as package:
        install_size = sum(
            entry.file_size for entry in package.infolist()
            if not entry.is_dir())

    versions[0].update({
        "download_url":
            f"{DOWNLOAD_BASE}/{version}/{ARCHIVE.name}",
        "download_sha256": hashlib.sha256(archive_data).hexdigest(),
        "download_size": len(archive_data),
        "install_size": install_size,
    })

    package_dir = PUBLISH_ROOT / "packages" / identifier
    package_dir.mkdir(parents=True, exist_ok=True)
    publish_metadata = package_dir / "metadata.json"
    publish_metadata.write_text(
        json.dumps(metadata, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8")
    shutil.copyfile(SOURCE_ICON, package_dir / "icon.png")
    print(f"Wrote {package_dir.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
