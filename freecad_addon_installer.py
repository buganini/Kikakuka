"""FreeCAD-console helper used by Kikakuka to install FreekiCAD."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile


RESULT_PREFIX = "KIKAKUKA_ADDON_RESULT="


def emit(**result):
    # Keep the protocol ASCII even when FreeCAD's native logs use a Windows
    # code page. JSON escapes preserve Unicode paths and error messages.
    print(RESULT_PREFIX + json.dumps(result, ensure_ascii=True), flush=True)


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


def _missing_pyside(exc: ImportError) -> bool:
    text = str(exc).casefold()
    return "pyside" in text and (
        "no viable" in text
        or "no module named" in text
        or "cannot import" in text
    )


def _run_pip_specs(specs, target: Path, create_command):
    target.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        command = create_command([
            "install",
            "--upgrade",
            "--target",
            str(target),
            spec,
        ])
        completed = subprocess.run(
            command, text=True, capture_output=True,
            encoding="utf-8", errors="replace",
        )
        if completed.stdout:
            print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")
        if completed.returncode:
            output = "\n".join((completed.stderr or "", completed.stdout or ""))
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            errors = [line for line in lines if line.startswith("ERROR:")]
            detail = "\n".join(errors or lines[-8:])[-1500:]
            raise RuntimeError(
                f"Could not install required FreeCAD dependency {spec}"
                + (f": {detail}" if detail else f" (pip exit status {completed.returncode})")
            )


def _freecad_python_executable() -> Path:
    import FreeCAD

    home = Path(FreeCAD.getHomePath())
    names = (
        ("python.exe", "python3.exe", "python", "python3")
        if os.name == "nt"
        else ("python3", "python")
    )
    candidates = [home / "bin" / name for name in names]
    candidates.extend(Path(sys.executable).with_name(name) for name in names)
    for name in names:
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("Could not locate FreeCAD's Python executable")


def _headless_pip_target() -> Path:
    import FreeCAD

    return (
        Path(FreeCAD.ConfigGet("UserAppData"))
        / "AdditionalPythonPackages"
        / f"py{sys.version_info.major}{sys.version_info.minor}"
    )


def install_dependencies(specs):
    if not specs:
        return
    try:
        import addonmanager_utilities as utils

        target = Path(utils.get_pip_target_directory())
        create_command = utils.create_pip_call
    except ImportError as exc:
        if not _missing_pyside(exc):
            raise
        target = _headless_pip_target()
        command_prefix = [
            str(_freecad_python_executable()),
            "-m",
            "pip",
            "--disable-pip-version-check",
        ]
        create_command = lambda args: [*command_prefix, *args]
    _run_pip_specs(specs, target, create_command)


def _safe_archive_parts(name: str):
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or (
        len(normalized) >= 2 and normalized[1] == ":"
    ):
        raise ValueError(f"Unsafe archive path: {name}")
    parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"Unsafe archive path: {name}")
    return parts


def _remove_path(path: Path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _install_archive_direct(archive_path: Path, destination: Path):
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=".FreekiCAD-", dir=parent))
    staged = staging_root / "FreekiCAD"
    staged.mkdir()
    backup = destination.with_name(destination.name + ".kikakuka-old")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                parts = _safe_archive_parts(member.filename)
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(
                        f"Archive symlink is not allowed: {member.filename}"
                    )
                output = staged.joinpath(*parts)
                if member.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, output.open("wb") as target:
                    shutil.copyfileobj(source, target)

        if not (staged / "package.xml").is_file():
            raise ValueError("FreekiCAD archive has no package.xml")
        _remove_path(backup)
        if destination.exists() or destination.is_symlink():
            destination.rename(backup)
        try:
            staged.rename(destination)
        except Exception:
            if backup.exists() and not destination.exists():
                backup.rename(destination)
            raise
        _remove_path(backup)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def install(archive_path: Path):
    archive_path = archive_path.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        package_xml = archive.read("package.xml")
    specs = dependency_specs(package_xml)
    install_dependencies(specs)

    destination = installed_package_xml().parent
    if destination.is_symlink():
        # Never let the addon manager overwrite a linked source checkout.
        _install_archive_direct(archive_path, destination)
        emit(ok=True, version=package_version(destination / "package.xml"),
             dependencies=specs)
        return
    try:
        from Addon import Addon
        from addonmanager_installer import AddonInstaller, InstallationMethod

        addon = Addon("FreekiCAD", str(archive_path), branch="main")
        allowed = [spec.split("<", 1)[0].split(">", 1)[0].split("=", 1)[0]
                   for spec in specs]
        installer = AddonInstaller(addon, allow_list=allowed)
        if not installer.run(InstallationMethod.ZIP):
            raise RuntimeError(
                "FreeCAD Addon Manager could not install FreekiCAD"
            )
    except ImportError as exc:
        if not _missing_pyside(exc):
            raise
        _install_archive_direct(archive_path, destination)

    version = package_version(destination / "package.xml")
    if version is None:
        raise RuntimeError("FreekiCAD package.xml was not installed")
    emit(ok=True, version=version, dependencies=specs)


def install_symlink(source: Path):
    source = source.resolve(strict=True)
    version = package_version(source / "package.xml")
    if version is None or not (source / "freecad/FreekiCAD").is_dir():
        raise ValueError("Source is not a FreekiCAD addon directory")
    destination = installed_package_xml().parent
    if destination.resolve() == source and not destination.is_symlink():
        raise ValueError("Source cannot be the installation directory")
    if source in destination.resolve().parents or destination.resolve() in source.parents:
        raise ValueError("Source and installation directories must not contain each other")
    install_dependencies(dependency_specs((source / "package.xml").read_bytes()))
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".FreekiCAD-link-", dir=destination.parent))
    link = staging / "FreekiCAD"
    backup = staging / "previous"
    try:
        # Create first so permission failures leave the current install intact.
        link.symlink_to(source, target_is_directory=True)
        if destination.exists() or destination.is_symlink():
            destination.rename(backup)
        try:
            link.rename(destination)
        except Exception:
            if backup.exists() or backup.is_symlink():
                backup.rename(destination)
            raise
        _remove_path(backup)
    finally:
        # Retain the backup if rollback itself failed.
        if not backup.exists() and not backup.is_symlink():
            shutil.rmtree(staging)
    emit(ok=True, version=version, symlink_target=str(source))


def _remove_freekicad_directory(path: Path):
    """Remove only the known user-addon directory without following links."""
    if path.name.casefold() != "freekicad":
        raise RuntimeError(f"Refusing to remove unexpected addon path: {path}")
    _remove_path(path)


def uninstall():
    path = installed_package_xml().parent
    if path.is_symlink():
        _remove_freekicad_directory(path)
        emit(ok=True, removed=True)
        return
    if not path.exists() and not path.is_symlink():
        emit(ok=True, removed=False)
        return
    try:
        from Addon import Addon
        from addonmanager_uninstaller import AddonUninstaller

        addon = Addon("FreekiCAD")
        if not AddonUninstaller(addon).run():
            raise RuntimeError(
                "FreeCAD Addon Manager could not uninstall FreekiCAD"
            )
    except ImportError as exc:
        if not _missing_pyside(exc):
            raise
        # Some Windows FreeCADCmd distributions do not expose PySide, while
        # AddonUninstaller inherits QtCore.QObject even in its documented
        # non-GUI mode. All GUI instances are already required to be closed,
        # so removing this known addon directory is the equivalent safe action.
        _remove_freekicad_directory(path)
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
            result = dict(ok=True, version=package_version(path), path=str(path))
            if path.parent.is_symlink():
                result["symlink_target"] = str(path.parent.resolve())
            emit(**result)
            return 0
        if action == "install-symlink":
            if len(argv) != 2:
                raise ValueError("install-symlink requires a source directory")
            install_symlink(Path(argv[1]))
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
