"""FreeCAD-console helper used by Kikakuka to install FreekiCAD."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile


RESULT_PREFIX = "KIKAKUKA_ADDON_RESULT="


def emit(**result):
    print(RESULT_PREFIX + json.dumps(result, ensure_ascii=False), flush=True)


def package_version(path: Path):
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    value = root.findtext("{*}version")
    return value.strip() if value else None


def dependency_specs(package_xml: bytes):
    """Convert required Python package.xml dependencies to pip specs."""
    root = ET.fromstring(package_xml)
    operators = (
        ("version_eq", "=="),
        ("version_gte", ">="),
        ("version_gt", ">"),
        ("version_lte", "<="),
        ("version_lt", "<"),
    )
    specs = []
    for dependency in root.findall("{*}depend"):
        if dependency.get("type") != "python":
            continue
        if dependency.get("optional", "false").casefold() == "true":
            continue
        package = (dependency.text or "").strip()
        if not package:
            continue
        constraints = [
            operator + dependency.attrib[attribute]
            for attribute, operator in operators
            if dependency.get(attribute)
        ]
        specs.append(package + ",".join(constraints))
    return specs


def installed_package_xml() -> Path:
    import FreeCAD

    return Path(FreeCAD.ConfigGet("UserAppData")) / "Mod" / "FreekiCAD" / "package.xml"


def install_dependencies(specs):
    if not specs:
        return
    import addonmanager_utilities as utils

    target = Path(utils.get_pip_target_directory())
    target.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        command = utils.create_pip_call(
            ["install", "--upgrade", "--target", str(target), spec]
        )
        completed = subprocess.run(command, text=True, capture_output=True)
        if completed.stdout:
            print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")
        if completed.returncode:
            raise RuntimeError(
                f"Could not install required FreeCAD dependency {spec}"
            )


def install(archive_path: Path):
    archive_path = archive_path.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        package_xml = archive.read("package.xml")
    specs = dependency_specs(package_xml)
    install_dependencies(specs)

    from Addon import Addon
    from addonmanager_installer import AddonInstaller, InstallationMethod

    addon = Addon("FreekiCAD", str(archive_path), branch="main")
    allowed = [spec.split("<", 1)[0].split(">", 1)[0].split("=", 1)[0]
               for spec in specs]
    installer = AddonInstaller(addon, allow_list=allowed)
    if not installer.run(InstallationMethod.ZIP):
        raise RuntimeError("FreeCAD Addon Manager could not install FreekiCAD")

    version = package_version(installed_package_xml())
    if version is None:
        raise RuntimeError("FreekiCAD package.xml was not installed")
    emit(ok=True, version=version, dependencies=specs)


def uninstall():
    from Addon import Addon
    from addonmanager_uninstaller import AddonUninstaller

    path = installed_package_xml().parent
    if not path.exists() and not path.is_symlink():
        emit(ok=True, removed=False)
        return
    addon = Addon("FreekiCAD")
    if not AddonUninstaller(addon).run():
        raise RuntimeError("FreeCAD Addon Manager could not uninstall FreekiCAD")
    emit(ok=True, removed=True)


def main(argv=None):
    if argv is None:
        action = os.environ.get("KIKAKUKA_ADDON_ACTION")
        if action:
            argv = [action]
            archive = os.environ.get("KIKAKUKA_ADDON_ARCHIVE")
            if archive:
                argv.append(archive)
        else:
            argv = sys.argv[1:]
    argv = list(argv)
    try:
        if not argv:
            raise ValueError("missing action")
        action = argv[0]
        if action == "status":
            path = installed_package_xml()
            emit(ok=True, version=package_version(path), path=str(path))
            return 0
        if action == "install":
            if len(argv) != 2:
                raise ValueError("install requires an archive path")
            install(Path(argv[1]))
            return 0
        if action == "uninstall":
            if len(argv) != 1:
                raise ValueError("uninstall takes no archive path")
            uninstall()
            return 0
        raise ValueError(f"unknown action: {action}")
    except Exception as exc:
        emit(ok=False, error=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
elif "FreeCAD" in sys.modules:
    # FreeCAD 1.1 imports command-line scripts instead of executing them as
    # __main__, so the conventional guard above does not run there.
    main()
