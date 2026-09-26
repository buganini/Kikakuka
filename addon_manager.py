"""Bundled KiCad and FreeCAD addon discovery and installation."""

from __future__ import annotations

from dataclasses import dataclass
import copy
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Iterable, Mapping, Optional
import zipfile


KICAD_PLUGIN = "kicad_plugin"
KICAD_LIBRARY = "kicad_library"
FREEKICAD = "freekicad"

ADDON_LABELS = {
    KICAD_PLUGIN: "KiCad Plugin",
    KICAD_LIBRARY: "KiCad Library",
    FREEKICAD: "FreekiCAD",
}

_BUNDLE_NAMES = {
    KICAD_PLUGIN: "kicad-plugin.zip",
    KICAD_LIBRARY: "kicad-library.zip",
    FREEKICAD: "freekicad.zip",
}

_SOURCE_METADATA = {
    KICAD_PLUGIN: Path("kicad-addon/plugin/metadata.json"),
    KICAD_LIBRARY: Path("kicad-addon/library/metadata.json"),
    FREEKICAD: Path("FreekiCAD/package.xml"),
}

_PCM_DIRECTORIES = {
    "plugins",
    "footprints",
    "3dmodels",
    "symbols",
    "resources",
    "colors",
    "templates",
    "scripts",
}

_RESULT_PREFIX = "KIKAKUKA_ADDON_RESULT="
_INSTALL_LOCK = threading.Lock()
_MIN_FREECAD_VERSION = (1, 0)
_MIN_FREECAD_VERSION_TEXT = "1.0"


@dataclass(frozen=True)
class AddonStatus:
    key: str
    label: str
    bundled_version: str
    installed_version: Optional[str]
    detail: str = ""
    available: bool = True

    @property
    def action(self) -> str:
        if not self.available:
            return ""
        if self.installed_version is None:
            return "Install"
        if self.installed_version != self.bundled_version:
            return "Update"
        return ""

    @property
    def uninstall_action(self) -> str:
        if self.available and self.installed_version is not None:
            return "Uninstall"
        return ""

    @property
    def status_text(self) -> str:
        if not self.available:
            return self.detail
        if self.installed_version is None:
            text = f"Not installed (bundled {self.bundled_version})"
        elif self.installed_version == self.bundled_version:
            text = f"Installed {self.installed_version}"
        else:
            text = (
                f"Installed {self.installed_version}; "
                f"bundled {self.bundled_version}"
            )
        return f"{text} — {self.detail}" if self.detail else text

    def as_row(self) -> dict[str, str]:
        return {
            "label": self.label,
            "status": self.status_text,
            "action": self.action,
            "uninstall": self.uninstall_action,
        }


@dataclass(frozen=True)
class KiCadPaths:
    version: str
    settings_dir: Path
    third_party_dir: Path

    @property
    def installed_packages(self) -> Path:
        return self.settings_dir / "installed_packages.json"

    @property
    def footprint_table(self) -> Path:
        return self.settings_dir / "fp-lib-table"


def _source_root() -> Path:
    return Path(__file__).resolve().parent


def _resource_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    # build_addon_archives.py places CLI/source-runtime resources here;
    # PyInstaller exposes the same relative layout under sys._MEIPASS.
    return _source_root() / "build"


def bundle_archive(key: str) -> Path:
    """Return an archive from the active runtime's resource directory."""
    name = _BUNDLE_NAMES[key]
    bundled = _resource_root() / "addons" / name
    if bundled.is_file():
        return bundled

    prefix = name[:-4]
    candidates = sorted(_source_root().glob(f"{prefix}-*.zip"))
    if candidates:
        return candidates[-1]
    raise FileNotFoundError(
        f"Bundled {ADDON_LABELS[key]} archive was not found; "
        "run `make archive-zips`"
    )


def freecad_helper_path() -> Path:
    bundled = _resource_root() / "addons" / "freecad_addon_installer.py"
    if bundled.is_file():
        return bundled
    return _source_root() / "freecad_addon_installer.py"


def _read_json_metadata(key: str) -> dict:
    source = _source_root() / _SOURCE_METADATA[key]
    if source.is_file() and not getattr(sys, "_MEIPASS", None):
        return json.loads(source.read_text(encoding="utf-8"))
    with zipfile.ZipFile(bundle_archive(key)) as archive:
        return json.loads(archive.read("metadata.json").decode("utf-8"))


def _read_freecad_package_xml() -> bytes:
    source = _source_root() / _SOURCE_METADATA[FREEKICAD]
    if source.is_file() and not getattr(sys, "_MEIPASS", None):
        return source.read_bytes()
    with zipfile.ZipFile(bundle_archive(FREEKICAD)) as archive:
        return archive.read("package.xml")


def bundled_version(key: str) -> str:
    if key == FREEKICAD:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(_read_freecad_package_xml())
        version = root.findtext("{*}version")
        if not version:
            raise ValueError("FreekiCAD package.xml has no version")
        return version.strip()
    metadata = _read_json_metadata(key)
    return str(metadata["versions"][0]["version"])


def _kicad_version(metadata: Mapping) -> str:
    return str(metadata["versions"][0]["kicad_version"])


def _windows_documents(home: Path, environ: Mapping[str, str]) -> Path:
    if platform.system() == "Windows":
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _kind = winreg.QueryValueEx(key, "Personal")
            return Path(os.path.expandvars(value)).expanduser()
        except (ImportError, OSError):
            pass
    profile = Path(environ.get("USERPROFILE", str(home)))
    return profile / "Documents"


def resolve_kicad_paths(
    version: str,
    *,
    system: Optional[str] = None,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> KiCadPaths:
    """Resolve the settings and PCM paths used by a KiCad major version."""
    system = system or platform.system()
    home = Path(home) if home is not None else Path.home()
    environ = dict(os.environ if environ is None else environ)

    config_override = environ.get("KICAD_CONFIG_HOME")
    if config_override:
        config_root = Path(config_override).expanduser()
    elif system == "Darwin":
        config_root = home / "Library" / "Preferences" / "kicad"
    elif system == "Windows":
        config_root = Path(environ.get("APPDATA", home / "AppData/Roaming")) / "kicad"
    else:
        config_root = Path(environ.get("XDG_CONFIG_HOME", home / ".config")) / "kicad"
    settings_dir = config_root / version

    variables = {}
    common_config = settings_dir / "kicad_common.json"
    try:
        common = json.loads(common_config.read_text(encoding="utf-8"))
        variables.update((common.get("environment") or {}).get("vars") or {})
    except (OSError, ValueError, TypeError):
        pass
    variables.update(environ)

    major = version.split(".", 1)[0]
    variable_name = f"KICAD{major}_3RD_PARTY"
    configured = variables.get(variable_name)
    if configured:
        third_party = Path(os.path.expandvars(str(configured))).expanduser()
    else:
        data_override = environ.get("KICAD_DOCUMENTS_HOME")
        if data_override:
            data_root = Path(data_override).expanduser()
        elif system == "Darwin":
            data_root = home / "Documents" / "KiCad"
        elif system == "Windows":
            data_root = _windows_documents(home, environ) / "KiCad"
        else:
            data_root = Path(environ.get("XDG_DATA_HOME", home / ".local/share")) / "kicad"
        third_party = data_root / version / "3rdparty"

    return KiCadPaths(version, settings_dir, third_party)


def _installed_pcm_entries(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    packages = data.get("packages", [])
    return packages if isinstance(packages, list) else []


def _pcm_installed_entry(path: Path, identifier: str) -> Optional[dict]:
    for entry in _installed_pcm_entries(path):
        package = entry.get("package") or {}
        if package.get("identifier") == identifier:
            return entry
    return None


def _pcm_installed_version(path: Path, identifier: str) -> Optional[str]:
    entry = _pcm_installed_entry(path, identifier)
    if entry is None:
        return None
    value = entry.get("current_version")
    return str(value) if value is not None else "unknown"


def _command_version(command: list[str]) -> Optional[str]:
    try:
        completed = subprocess.run(
            [*command, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = "\n".join((completed.stdout, completed.stderr))
    match = re.search(r"\b(\d+)\.(\d+)(?:\.\d+)?\b", output)
    return match.group(0) if match else None


def _version_parts(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def _command_version_matches(command: list[str], required_major: str) -> bool:
    version = _command_version(command)
    return bool(version and version.split(".", 1)[0] == required_major)


def _windows_associated_executable(extension: str) -> Optional[Path]:
    if platform.system() != "Windows":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        query = ctypes.windll.shlwapi.AssocQueryStringW
        query.argtypes = (
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        query.restype = wintypes.LONG
        length = wintypes.DWORD()
        # ASSOCSTR_EXECUTABLE = 2
        query(0, 2, extension, None, None, ctypes.byref(length))
        if not length.value:
            return None
        buffer = ctypes.create_unicode_buffer(length.value)
        if query(0, 2, extension, None, buffer, ctypes.byref(length)) != 0:
            return None
        return Path(buffer.value) if buffer.value else None
    except (AttributeError, OSError):
        return None


def _owned_kicad_cli_candidates() -> Iterable[Path]:
    try:
        from im.im_mesh import owned_pids, owned_process

        for pid in owned_pids():
            process = owned_process(pid)
            if process is None:
                continue
            try:
                name = Path(process.name()).stem.casefold()
                if name not in {"kicad", "pcbnew", "eeschema"}:
                    continue
                executable = Path(process.exe())
                yield executable.with_name(
                    "kicad-cli.exe" if executable.suffix.casefold() == ".exe" else "kicad-cli"
                )
            except Exception:
                continue
    except Exception:
        return


def _running_editor_pids(key: str) -> list[int]:
    """Return same-user application instances that can hold addon state."""
    try:
        from im.im_mesh import is_kicad_editor_process, owned_process_iter

        pids = []
        for process in owned_process_iter(["pid", "name"]):
            try:
                name = process.info.get("name") or ""
                stem = Path(name).stem.casefold()
                is_editor = (
                    stem.startswith("freecad")
                    if key == FREEKICAD
                    else is_kicad_editor_process(name)
                )
                if is_editor:
                    pids.append(process.pid)
            except Exception:
                continue
        return sorted(set(pids))
    except Exception:
        return []


def _require_no_running_instances(key: str, operation: str) -> None:
    pids = _running_editor_pids(key)
    if not pids:
        return
    application = "FreeCAD" if key == FREEKICAD else "KiCad"
    raise RuntimeError(
        f"Close all {application} instances before {operation.lower()}"
    )


def _kicad_cli_candidates() -> list[Path]:
    candidates: list[Path] = list(_owned_kicad_cli_candidates())
    for name in ("kicad-cli", "kicad-cli.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))

    system = platform.system()
    if system == "Darwin":
        candidates.extend(
            [
                Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
                Path("/Applications/KiCad.app/Contents/MacOS/kicad-cli"),
            ]
        )
    elif system == "Windows":
        associated = _windows_associated_executable(".kicad_pcb")
        if associated:
            candidates.append(associated.with_name("kicad-cli.exe"))
        for variable in ("ProgramFiles", "LOCALAPPDATA"):
            value = os.environ.get(variable)
            if value:
                candidates.extend(
                    sorted(
                        Path(value).glob("KiCad/*/bin/kicad-cli.exe"),
                        reverse=True,
                    )
                )
    else:
        candidates.append(Path("/usr/bin/kicad-cli"))

    return candidates


def _detected_kicad_installations() -> list[tuple[Path, str]]:
    candidates = _kicad_cli_candidates()
    installations = []

    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved in seen or not candidate.is_file():
            continue
        seen.add(resolved)
        # Do not mistake Kikakuka's private export helper for a KiCad install.
        if getattr(sys, "_MEIPASS", None):
            try:
                if candidate.is_relative_to(Path(sys._MEIPASS)):
                    continue
            except (AttributeError, ValueError):
                pass
        version = _command_version([str(candidate)])
        if version:
            installations.append((candidate, version))
    return installations


def find_kicad_installation(version: str) -> Optional[Path]:
    """Locate a KiCad installation matching the addon's required major."""
    required_major = version.split(".", 1)[0]
    for candidate, detected_version in _detected_kicad_installations():
        if detected_version.split(".", 1)[0] == required_major:
            return candidate
    return None


def find_any_kicad_version() -> Optional[str]:
    installations = _detected_kicad_installations()
    return installations[0][1] if installations else None


def _kicad_payload_directories(key: str) -> tuple[str, ...]:
    return ("plugins",) if key == KICAD_PLUGIN else ("footprints", "3dmodels")


def _installed_payload_version(
    paths: KiCadPaths,
    key: str,
    identifier: str,
) -> Optional[str]:
    clean_id = identifier.replace(".", "_")
    for directory in _kicad_payload_directories(key):
        marker = paths.third_party_dir / directory / clean_id / ".kikakuka-version"
        try:
            version = marker.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if version:
            return version
    return None


def _payload_matches_bundle(paths: KiCadPaths, key: str, identifier: str) -> bool:
    """Recognize archives built before the payload version marker existed."""
    clean_id = identifier.replace(".", "_")
    compared = False
    try:
        with zipfile.ZipFile(bundle_archive(key)) as archive:
            for member in archive.infolist():
                parts = _safe_member_parts(member.filename)
                if (
                    member.is_dir()
                    or len(parts) < 2
                    or parts[0] not in _kicad_payload_directories(key)
                    or parts[-1] == ".kikakuka-version"
                ):
                    continue
                installed = (
                    paths.third_party_dir
                    / parts[0]
                    / clean_id
                    / Path(*parts[1:])
                )
                if (
                    not installed.is_file()
                    or installed.read_bytes() != archive.read(member)
                ):
                    return False
                compared = True
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return False
    return compared


def _metadata_with_version(metadata: dict, version: str) -> dict:
    repaired = copy.deepcopy(metadata)
    versions = repaired.get("versions")
    if not isinstance(versions, list):
        versions = []
        repaired["versions"] = versions
    if not any(
        str(item.get("version")) == version
        for item in versions
        if isinstance(item, dict)
    ):
        template = copy.deepcopy(versions[0]) if versions else {}
        template["version"] = version
        versions.append(template)
    return repaired


def kicad_addon_status(key: str) -> AddonStatus:
    metadata = _read_json_metadata(key)
    version = _kicad_version(metadata)
    if find_kicad_installation(version) is None:
        detected_version = find_any_kicad_version()
        detail = (
            f"Unsupported KiCad version {detected_version}; "
            f"requires {version.split('.', 1)[0]}.x"
            if detected_version is not None
            else "Cannot find kicad installation"
        )
        return AddonStatus(
            key,
            ADDON_LABELS[key],
            str(metadata["versions"][0]["version"]),
            None,
            detail,
            False,
        )
    paths = resolve_kicad_paths(version)
    identifier = str(metadata["identifier"])
    entry = _pcm_installed_entry(paths.installed_packages, identifier)
    installed = _pcm_installed_version(paths.installed_packages, identifier)
    payload_version = _installed_payload_version(paths, key, identifier)
    if payload_version is None and _payload_matches_bundle(paths, key, identifier):
        payload_version = str(metadata["versions"][0]["version"])

    package = (entry or {}).get("package") or {}
    invalid_record = bool(
        entry
        and (
            installed in (None, "0.0", "unknown")
            or package.get("type") == "invalid"
        )
    )
    if payload_version and (entry is None or invalid_record):
        installed = payload_version
        # A running KiCad keeps its own package list in memory and can overwrite
        # the repaired file when it exits. Only repair metadata while all KiCad
        # processes are closed.
        if not _running_editor_pids(key):
            _record_pcm_install(
                paths.installed_packages,
                _metadata_with_version(metadata, payload_version),
                payload_version,
            )
    elif installed is None:
        clean_id = identifier.replace(".", "_")
        if any(
            (paths.third_party_dir / directory / clean_id).is_dir()
            for directory in _kicad_payload_directories(key)
        ):
            installed = "unknown"
    return AddonStatus(
        key,
        ADDON_LABELS[key],
        str(metadata["versions"][0]["version"]),
        installed,
    )


def _safe_member_parts(name: str) -> tuple[str, ...]:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"Unsafe archive path: {name}")
    parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"Unsafe archive path: {name}")
    return parts


def _replace_directory(staged: Path, destination: Path) -> None:
    def remove(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

    backup = destination.with_name(destination.name + ".kikakuka-old")
    remove(backup)
    if destination.exists() or destination.is_symlink():
        destination.rename(backup)
    try:
        staged.rename(destination)
    except Exception:
        if backup.exists() and not destination.exists():
            backup.rename(destination)
        raise
    remove(backup)


def _extract_pcm_archive(archive_path: Path, root: Path, identifier: str) -> set[str]:
    clean_id = identifier.replace(".", "_")
    root.mkdir(parents=True, exist_ok=True)
    # Keep staging on the destination filesystem so the final rename is atomic.
    staging_root = Path(tempfile.mkdtemp(prefix=".kikakuka-pcm-", dir=root))
    extracted_top_dirs: set[str] = set()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                parts = _safe_member_parts(member.filename)
                if parts[0] not in _PCM_DIRECTORIES or len(parts) < 2:
                    continue
                # Reject Unix symlinks in downloaded or bundled ZIP input.
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f"Archive symlink is not allowed: {member.filename}")
                top_dir = parts[0]
                relative = parts[1:]
                target = staging_root / top_dir / clean_id
                extracted_top_dirs.add(top_dir)
                if member.is_dir():
                    (target.joinpath(*relative)).mkdir(parents=True, exist_ok=True)
                    continue
                output = target.joinpath(*relative)
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, output.open("wb") as destination:
                    shutil.copyfileobj(source, destination)

        if not extracted_top_dirs:
            raise ValueError("PCM archive contains no installable directories")
        for top_dir in sorted(extracted_top_dirs):
            staged = staging_root / top_dir / clean_id
            destination = root / top_dir / clean_id
            destination.parent.mkdir(parents=True, exist_ok=True)
            _replace_directory(staged, destination)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return extracted_top_dirs


def _atomic_json_write(path: Path, data: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".kikakuka-new")
    temporary.write_text(
        json.dumps(data, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _record_pcm_install(path: Path, metadata: dict, version: str) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        data = {"packages": []}
    packages = data.get("packages")
    if not isinstance(packages, list):
        packages = []
    identifier = metadata["identifier"]
    previous = next(
        (
            entry
            for entry in packages
            if (entry.get("package") or {}).get("identifier") == identifier
        ),
        {},
    )
    packages = [
        entry
        for entry in packages
        if (entry.get("package") or {}).get("identifier") != identifier
    ]
    previous_package = previous.get("package") or {}
    invalid_previous = (
        previous.get("current_version") in (None, "0.0", "unknown")
        or previous_package.get("type") == "invalid"
    )
    packages.append(
        {
            "current_version": version,
            "install_timestamp": int(time.time()),
            "package": metadata,
            "pinned": bool(previous.get("pinned", False)),
            "repository_id": (
                "" if invalid_previous else previous.get("repository_id", "")
            ),
            "repository_name": (
                "Local file"
                if invalid_previous
                else previous.get("repository_name", "Local file")
            ),
        }
    )
    data["packages"] = packages
    _atomic_json_write(path, data)


def _remove_pcm_install(path: Path, identifier: str) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    packages = data.get("packages")
    if not isinstance(packages, list):
        return
    remaining = [
        entry
        for entry in packages
        if (entry.get("package") or {}).get("identifier") != identifier
    ]
    if len(remaining) == len(packages):
        return
    data["packages"] = remaining
    _atomic_json_write(path, data)


def _ensure_footprint_table(path: Path, major: str, clean_id: str) -> None:
    uri = (
        f"${{KICAD{major}_3RD_PARTY}}/footprints/"
        f"{clean_id}/Kikakuka.pretty"
    )
    entry = (
        f'  (lib (name "PCM_Kikakuka") (type "KiCad") '
        f'(uri "{uri}") (options "") '
        f'(descr "Added by Kikakuka"))'
    )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = "(fp_lib_table\n  (version 7)\n)\n"

    if uri in text:
        return
    name_pattern = re.compile(
        r'^\s*\(lib \(name "PCM_Kikakuka"\).*$', re.MULTILINE
    )
    if name_pattern.search(text):
        text = name_pattern.sub(entry, text)
    else:
        closing = text.rfind(")")
        if closing < 0:
            raise ValueError(f"Invalid KiCad footprint table: {path}")
        text = text[:closing].rstrip() + "\n" + entry + "\n" + text[closing:]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".kikakuka-new")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _remove_footprint_table_entry(path: Path, major: str, clean_id: str) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    uri = (
        f"${{KICAD{major}_3RD_PARTY}}/footprints/"
        f"{clean_id}/Kikakuka.pretty"
    )
    lines = text.splitlines(keepends=True)
    remaining = [
        line
        for line in lines
        if 'name "PCM_Kikakuka"' not in line and uri not in line
    ]
    if len(remaining) == len(lines):
        return
    temporary = path.with_name(path.name + ".kikakuka-new")
    temporary.write_text("".join(remaining), encoding="utf-8")
    os.replace(temporary, path)


def install_kicad_addon(key: str) -> AddonStatus:
    if key not in (KICAD_PLUGIN, KICAD_LIBRARY):
        raise ValueError(f"Not a KiCad addon: {key}")
    metadata = _read_json_metadata(key)
    version = str(metadata["versions"][0]["version"])
    kicad_version = _kicad_version(metadata)
    paths = resolve_kicad_paths(kicad_version)
    identifier = str(metadata["identifier"])
    _extract_pcm_archive(bundle_archive(key), paths.third_party_dir, identifier)
    _record_pcm_install(paths.installed_packages, metadata, version)
    if key == KICAD_LIBRARY:
        _ensure_footprint_table(
            paths.footprint_table,
            kicad_version.split(".", 1)[0],
            identifier.replace(".", "_"),
        )
    status = kicad_addon_status(key)
    return status


def uninstall_kicad_addon(key: str) -> AddonStatus:
    if key not in (KICAD_PLUGIN, KICAD_LIBRARY):
        raise ValueError(f"Not a KiCad addon: {key}")
    metadata = _read_json_metadata(key)
    kicad_version = _kicad_version(metadata)
    paths = resolve_kicad_paths(kicad_version)
    identifier = str(metadata["identifier"])
    clean_id = identifier.replace(".", "_")

    for directory in _PCM_DIRECTORIES:
        payload = paths.third_party_dir / directory / clean_id
        if payload.is_symlink() or payload.is_file():
            payload.unlink()
        elif payload.is_dir():
            shutil.rmtree(payload)
    _remove_pcm_install(paths.installed_packages, identifier)
    if key == KICAD_LIBRARY:
        _remove_footprint_table_entry(
            paths.footprint_table,
            kicad_version.split(".", 1)[0],
            clean_id,
        )
    return kicad_addon_status(key)


def _owned_freecad_executables() -> Iterable[Path]:
    try:
        from im.im_mesh import owned_pids, owned_process

        for pid in owned_pids():
            process = owned_process(pid)
            if process is None:
                continue
            try:
                name = process.name().casefold()
                if "freecad" in name:
                    yield Path(process.exe())
            except Exception:
                continue
    except Exception:
        return


def freecad_commands(system: Optional[str] = None) -> list[list[str]]:
    """Return supported FreeCADCmd invocations."""
    system = system or platform.system()
    cmd_candidates: list[Path] = []

    for executable in _owned_freecad_executables():
        if Path(executable).stem.casefold() == "freecadcmd":
            cmd_candidates.append(executable)
        if system == "Darwin" and ".app" in str(executable):
            app = next(
                (parent for parent in executable.parents if parent.suffix == ".app"),
                None,
            )
            if app:
                cmd_candidates.append(app / "Contents/Resources/bin/freecadcmd")
        cmd_candidates.extend(
            [executable.with_name("freecadcmd"), executable.with_name("FreeCADCmd.exe")]
        )

    for name in ("freecadcmd", "FreeCADCmd", "FreeCADCmd.exe"):
        found = shutil.which(name)
        if found:
            cmd_candidates.append(Path(found))

    if system == "Darwin":
        applications = [Path("/Applications"), Path.home() / "Applications"]
        for applications_dir in applications:
            for app in applications_dir.glob("FreeCAD*.app"):
                cmd_candidates.append(app / "Contents/Resources/bin/freecadcmd")
    elif system == "Windows":
        associated = _windows_associated_executable(".FCStd")
        if associated:
            cmd_candidates.append(associated.with_name("FreeCADCmd.exe"))
        roots = [os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")]
        for root in filter(None, roots):
            base = Path(root)
            cmd_candidates.extend(sorted(base.glob("FreeCAD*/bin/FreeCADCmd.exe"), reverse=True))

    commands: list[list[str]] = []
    seen = set()
    for candidate in cmd_candidates:
        try:
            key = str(candidate.resolve())
        except OSError:
            key = str(candidate)
        if key in seen or not candidate.is_file():
            continue
        seen.add(key)
        commands.append([str(candidate)])
    return commands


def _windows_executable_version(path: Path) -> Optional[str]:
    if platform.system() != "Windows":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(
            str(path), 0, size, buffer
        ):
            return None
        value = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(
            buffer, "\\", ctypes.byref(value), ctypes.byref(length)
        ):
            return None
        info = ctypes.cast(value, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        return ".".join(
            str(part)
            for part in (
                info.dwFileVersionMS >> 16,
                info.dwFileVersionMS & 0xFFFF,
                info.dwFileVersionLS >> 16,
            )
        )
    except (AttributeError, OSError, ValueError):
        return None


def _freecad_version_from_installation(path: Path) -> Optional[str]:
    if platform.system() == "Darwin":
        import plistlib

        app = next((parent for parent in path.parents if parent.suffix == ".app"), None)
        if app:
            try:
                with (app / "Contents/Info.plist").open("rb") as source:
                    info = plistlib.load(source)
                value = (
                    info.get("CFBundleShortVersionString")
                    or info.get("CFBundleVersion")
                )
                if value and re.fullmatch(r"\d+(?:\.\d+)+", str(value)):
                    return str(value)
            except (OSError, ValueError, TypeError):
                pass
    windows_version = _windows_executable_version(path)
    if windows_version:
        return windows_version
    for part in reversed(path.parts):
        match = re.search(r"FreeCAD[^\d]*(\d+(?:\.\d+)+)", part, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def freecad_installation_version(commands: list[list[str]]) -> Optional[str]:
    for command in commands:
        if command:
            version = _freecad_version_from_installation(Path(command[0]))
            if version:
                return version
    for command in commands:
        version = _command_version(command)
        if version:
            return version
    return None


def _parse_helper_result(output: str) -> Optional[dict]:
    for line in reversed(output.splitlines()):
        if line.startswith(_RESULT_PREFIX):
            try:
                value = json.loads(line[len(_RESULT_PREFIX):])
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


def run_freecad_helper(action: str, archive: Optional[Path] = None) -> dict:
    helper = freecad_helper_path()
    errors = []
    commands = freecad_commands()
    if not commands:
        raise FileNotFoundError("FreeCADCmd executable was not found")
    helper_env = os.environ.copy()
    helper_env["KIKAKUKA_ADDON_ACTION"] = action
    if archive is not None:
        helper_env["KIKAKUKA_ADDON_ARCHIVE"] = str(archive)
    else:
        helper_env.pop("KIKAKUKA_ADDON_ARCHIVE", None)
    for prefix in commands:
        # FreeCAD parses extra script arguments itself on some releases.
        # Pass helper inputs through the environment so only the script path
        # appears on FreeCAD's command line.
        command = [*prefix, str(helper)]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=300 if action == "install" else 30,
                env=helper_env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{' '.join(prefix)}: {exc}")
            continue
        output = "\n".join((completed.stdout, completed.stderr))
        result = _parse_helper_result(output)
        if result and result.get("ok"):
            return result
        message = (result or {}).get("error") or output.strip() or (
            f"exit status {completed.returncode}"
        )
        if len(message) > 1000:
            message = message[-1000:]
        errors.append(f"{' '.join(prefix)}: {message}")
    raise RuntimeError("; ".join(errors))


def _heuristic_freekicad_version() -> Optional[str]:
    import xml.etree.ElementTree as ET

    home = Path.home()
    if platform.system() == "Darwin":
        root = home / "Library/Application Support/FreeCAD"
    elif platform.system() == "Windows":
        root = Path(os.environ.get("APPDATA", home / "AppData/Roaming")) / "FreeCAD"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")) / "FreeCAD"
    candidates = list(root.glob("*/Mod/FreekiCAD/package.xml"))
    candidates.extend(root.glob("Mod/FreekiCAD/package.xml"))
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for package_xml in candidates:
        try:
            value = ET.parse(package_xml).getroot().findtext("{*}version")
            if value:
                return value.strip()
        except (OSError, ET.ParseError):
            continue
    return None


def freekicad_status(*, query_freecad: bool = False) -> AddonStatus:
    commands = freecad_commands()
    if not commands:
        return AddonStatus(
            FREEKICAD,
            ADDON_LABELS[FREEKICAD],
            bundled_version(FREEKICAD),
            None,
            "Cannot find freecad installation",
            False,
        )
    freecad_version = freecad_installation_version(commands)
    if freecad_version is None:
        return AddonStatus(
            FREEKICAD,
            ADDON_LABELS[FREEKICAD],
            bundled_version(FREEKICAD),
            None,
            "Cannot determine FreeCAD version",
            False,
        )
    if _version_parts(freecad_version) < _MIN_FREECAD_VERSION:
        return AddonStatus(
            FREEKICAD,
            ADDON_LABELS[FREEKICAD],
            bundled_version(FREEKICAD),
            None,
            f"Unsupported FreeCAD version {freecad_version}; "
            f"requires {_MIN_FREECAD_VERSION_TEXT} or newer",
            False,
        )
    installed = None
    detail = ""
    if query_freecad:
        try:
            # The helper itself is a short-lived FreeCADCmd process. Serialize
            # it with mutations so our own status probe cannot trip the
            # all-instances-closed guard.
            with _INSTALL_LOCK:
                result = run_freecad_helper("status")
            installed = result.get("version")
        except Exception as exc:
            print(f"Addon manager: FreeCAD status check failed: {exc}")
            detail = "Could not query FreeCAD addon status"
    if installed is None:
        installed = _heuristic_freekicad_version()
    if installed is not None:
        detail = ""
    return AddonStatus(
        FREEKICAD,
        ADDON_LABELS[FREEKICAD],
        bundled_version(FREEKICAD),
        str(installed) if installed is not None else None,
        detail,
    )


def install_freekicad() -> AddonStatus:
    result = run_freecad_helper("install", bundle_archive(FREEKICAD))
    installed = result.get("version")
    return AddonStatus(
        FREEKICAD,
        ADDON_LABELS[FREEKICAD],
        bundled_version(FREEKICAD),
        str(installed) if installed is not None else None,
    )


def uninstall_freekicad() -> AddonStatus:
    run_freecad_helper("uninstall")
    return AddonStatus(
        FREEKICAD,
        ADDON_LABELS[FREEKICAD],
        bundled_version(FREEKICAD),
        None,
    )


def addon_statuses(*, query_freecad: bool = False) -> list[AddonStatus]:
    return [
        kicad_addon_status(KICAD_PLUGIN),
        kicad_addon_status(KICAD_LIBRARY),
        freekicad_status(query_freecad=query_freecad),
    ]


def install_addon(key: str) -> AddonStatus:
    # KiCad plugin and library installs share installed_packages.json, so
    # serialize all button-triggered installs within this process.
    with _INSTALL_LOCK:
        _require_no_running_instances(key, "Install")
        if key == FREEKICAD:
            return install_freekicad()
        return install_kicad_addon(key)


def uninstall_addon(key: str) -> AddonStatus:
    with _INSTALL_LOCK:
        _require_no_running_instances(key, "Uninstall")
        if key == FREEKICAD:
            return uninstall_freekicad()
        return uninstall_kicad_addon(key)
