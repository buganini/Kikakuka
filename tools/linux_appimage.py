"""Build a Linux AppImage from a PyInstaller onedir using KiCad's toolchain."""

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from contextlib import contextmanager


ROOT = Path(__file__).resolve().parents[1]
TOOLS = {
    "sharun": {
        "version": "v0.8.1",
        "url": "https://github.com/VHSgunzo/sharun/releases/download/v0.8.1/sharun-x86_64",
        "sha256": "18d970f56eca2c527ffd3993b161b6bc340055129db14b394a77cb67d8bbfff9",
    },
    "uruntime": {
        "version": "v0.5.6",
        "url": "https://github.com/VHSgunzo/uruntime/releases/download/v0.5.6/uruntime-appimage-dwarfs-x86_64",
        "sha256": "6416a112fac1e9983b1c0738cd140f17dc1205f515b9bdb36b4607ef98ee2a70",
    },
    "dwarfs": {
        "version": "v0.14.1",
        "url": "https://github.com/mhx/dwarfs/releases/download/v0.14.1/dwarfs-universal-0.14.1-Linux-x86_64",
        "sha256": "f3a117fd6d5b7304944b199af7fdb8086a48c509ea2e9832255d8f9a54c98587",
    },
}
ADDONS = ("freekicad.zip", "kicad-library.zip", "kicad-plugin.zip")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_tool(name, cache):
    spec = TOOLS[name]
    cache = cache / f"{name}-{spec['version']}-x86_64"
    path = cache / name
    cache.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            try:
                with urllib.request.urlopen(spec["url"], timeout=60) as response:
                    shutil.copyfileobj(response, temporary)
                temporary.flush()
                if sha256(temporary_path) != spec["sha256"]:
                    raise ValueError(f"Checksum mismatch downloading {name}")
                temporary_path.replace(path)
            finally:
                temporary_path.unlink(missing_ok=True)
    if sha256(path) != spec["sha256"]:
        raise ValueError(f"Checksum mismatch for cached tool {path}; remove it and retry")
    path.chmod(0o755)
    return path


def version():
    tree = ast.parse((ROOT / "common.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "VERSION"
                for target in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError("VERSION not found in common.py")


def elf_files(directory):
    files = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as source:
                if source.read(4) == b"\x7fELF":
                    files.append(path)
    return files


def validate_addons(internal):
    hashes = {}
    for name in ADDONS:
        archive = internal / "addons" / name
        with zipfile.ZipFile(archive) as bundle:
            if bundle.testzip() is not None:
                raise ValueError(f"Corrupt addon archive: {archive}")
        hashes[name] = sha256(archive)
    return hashes


def external_dependencies(files, internal, appdir, environment):
    """Collect dependencies not already carried by onedir or the CLI bundle."""
    collected_names = {path.name for path in (appdir / "shared/lib").rglob("*") if path.is_file()}
    dependencies = set()
    for start in range(0, len(files), 100):
        result = subprocess.run(["ldd", *(str(path) for path in files[start:start + 100])],
                                env=environment, capture_output=True, text=True)
        if "=> not found" in result.stdout:
            missing = [line.strip() for line in result.stdout.splitlines() if "=> not found" in line]
            raise RuntimeError("Unresolved native dependencies: " + "; ".join(missing))
        if result.returncode and "not a dynamic executable" not in result.stderr:
            raise RuntimeError(f"ldd dependency collection failed: {result.stderr}")
        for line in result.stdout.splitlines():
            match = re.match(r"\s*(?:\S+\s+=>\s+)?(/.*?)\s+\(0x[0-9a-f]+\)", line)
            if match:
                path = Path(match.group(1)).resolve()
                if not path.is_relative_to(internal.resolve()) and path.name not in collected_names:
                    dependencies.add(path)
    return sorted(dependencies)


def run(command, **kwargs):
    subprocess.run([str(part) for part in command], check=True, **kwargs)


@contextmanager
def build_directory():
    directory = Path(tempfile.mkdtemp(prefix="kikakuka-appimage-", dir=ROOT / "build"))
    try:
        yield directory
    except Exception:
        print(f"Failed build staging retained for diagnosis: {directory}", flush=True)
        raise
    else:
        shutil.rmtree(directory)


def build_appimage(onedir, cli, output_dir, cache):
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError("Linux AppImage packaging currently supports native x86_64 builds")
    for command in ("bash", "file", "readelf", "patchelf", "ldd", "env", "base64", "readlink"):
        if not shutil.which(command):
            raise RuntimeError(f"Missing build tool: {command}")
    onedir, cli = onedir.resolve(), cli.resolve()
    if not (onedir / "Kikakuka").is_file() or not cli.is_file():
        raise FileNotFoundError("Expected PyInstaller Kikakuka executable and KiCad CLI")
    addon_hashes = validate_addons(onedir / "_internal")
    cli_version = subprocess.check_output([str(cli), "--version"], text=True).strip()
    tools = {name: download_tool(name, cache.resolve()) for name in TOOLS}
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir.resolve() / f"Kikakuka-{version()}-x86_64.AppImage"
    (ROOT / "build").mkdir(exist_ok=True)

    # Work on a private copy; never let dependency tools modify dist/onedir.
    with build_directory() as directory:
        staging = Path(directory)
        source = staging / "source"
        shutil.copytree(onedir, source, symlinks=True)
        appdir = staging / "AppDir"
        environment = os.environ.copy()
        environment["SHARUN"] = str(tools["sharun"])
        libraries = elf_files(source / "_internal")
        search_dirs = sorted({str(path.parent) for path in libraries})
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(
            search_dirs + ([environment["LD_LIBRARY_PATH"]] if environment.get("LD_LIBRARY_PATH") else []))
        plugins = [cli.with_name(name) for name in ("_eeschema.kiface", "_pcbnew.kiface")]
        plugins = [path for path in plugins if path.is_file()]
        run([tools["sharun"], "lib4bin", "--hard-links", "--with-sharun",
             "--with-hooks", "--dst-dir", appdir, cli, *plugins], env=environment)
        # Keep the PyInstaller bootloader and its resource directory together.
        # Its automatic lib4bin conversion flattens _internal and unnecessarily
        # patches every bundled module's RPATH; libs-only avoids that conversion.
        shutil.copy2(source / "Kikakuka", appdir / "shared/bin/Kikakuka")
        shutil.copytree(source / "_internal", appdir / "shared/bin/_internal", symlinks=True)
        os.link(appdir / "sharun", appdir / "bin/Kikakuka")
        # Explicitly include dlopen dependencies (Qt plugins, pcbnew, PDFium, etc.).
        # Use source paths even after lib4bin moved its private copy of _internal.
        dependencies = external_dependencies([source / "Kikakuka", *libraries], source / "_internal", appdir, environment)
        print(f"Collecting {len(dependencies)} additional native dependencies", flush=True)
        for start in range(0, len(dependencies), 100):
            run([tools["sharun"], "lib4bin", "--libs-only", "--with-hooks",
                 "--dst-dir", appdir, *dependencies[start:start + 100]], env=environment)

        # KiCad dlopen() must receive the plugin ELF, not a sharun launcher.
        for plugin in plugins:
            link = appdir / "bin" / plugin.name
            link.unlink(missing_ok=True)
            link.symlink_to(Path("../shared/bin") / plugin.name)
        (appdir / "usr").mkdir(exist_ok=True)
        (appdir / "usr/bin").symlink_to("../bin")
        (appdir / "usr/share").symlink_to("../share")

        internal = appdir / "shared" / "bin" / "_internal"
        if not internal.is_dir():
            raise RuntimeError("sharun did not preserve PyInstaller's _internal resources")
        if validate_addons(internal) != addon_hashes:
            raise RuntimeError("Addon payload changed during dependency collection")
        bundled_cli = internal / "KiCad" / "bin"
        bundled_cli.mkdir(parents=True, exist_ok=True)
        # Resolve relative to this script so the CLI uses sharun's bundled loader.
        cli_relative = os.path.relpath(appdir / "bin/kicad-cli", bundled_cli.resolve())
        (bundled_cli / "kicad-cli").write_text(
            '#!/bin/sh\nroot="$(dirname -- "$(readlink -f "$0")")"\n'
            f'exec "$root/{cli_relative}" "$@"\n')
        (bundled_cli / "kicad-cli").chmod(0o755)
        # Differ uses embedded design data; stock footprints/3D libraries would
        # add several GB and are not needed for its exports.
        for data in ("kicad/resources", "kicad/schemas", "kicad/internat", "kicad/plugins", "fonts/fontconfig"):
            path = Path("/usr/share") / data
            if path.is_dir():
                shutil.copytree(path, appdir / "share" / data, symlinks=False, dirs_exist_ok=True)
        (appdir / "AppRun").write_text(
            '#!/bin/sh\nset -eu\n'
            'root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
            'unset KIKAKUKA_HOST_ENV\n'
            'KIKAKUKA_HOST_ENV="$(env -0 | base64 -w0)"\n'
            'export KIKAKUKA_HOST_ENV\n'
            'if [ "${1:-}" = "kicad-cli" ]; then shift; exec "$root/bin/kicad-cli" "$@"; fi\n'
            'exec "$root/bin/Kikakuka" "$@"\n')
        (appdir / "AppRun").chmod(0o755)
        shutil.copy(ROOT / "resources/icon.png", appdir / "Kikakuka.png")
        (appdir / ".DirIcon").symlink_to("Kikakuka.png")
        (appdir / "Kikakuka.desktop").write_text(
            '[Desktop Entry]\nType=Application\nName=Kikakuka\nExec=Kikakuka %F\n'
            'Icon=Kikakuka\nTerminal=false\nCategories=Development;Electronics;\n')
        (appdir / ".env").write_text(
            'APPDIR=${SHARUN_DIR}\nKICAD_STOCK_DATA_HOME=${SHARUN_DIR}/share/kicad\n'
            'QT_PLUGIN_PATH=${SHARUN_DIR}/shared/bin/_internal/PySide6/Qt/plugins\n')
        runtime = staging / "uruntime"
        runtime_data = tools["uruntime"].read_bytes()
        if b"URUNTIME_EXTRACT=3" not in runtime_data:
            raise RuntimeError("Unexpected uruntime extraction configuration")
        runtime.write_bytes(runtime_data.replace(b"URUNTIME_EXTRACT=3", b"URUNTIME_EXTRACT=2"))
        runtime.chmod(0o755)
        run([appdir / "AppRun", "--version"], env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        run([bundled_cli / "kicad-cli", "--version"])
        # Write atomically only after mkdwarfs completes successfully.
        temporary_output = staging / "Kikakuka.AppImage"
        # The universal binary selects the tool by argv[0], as in KiCad's build.
        mkdwarfs = staging / "mkdwarfs"
        shutil.copy2(tools["dwarfs"], mkdwarfs)
        run([mkdwarfs, "--force", "--set-owner", "0",
             "--set-group", "0", "--no-history", "--no-create-timestamp",
             "--header", runtime, "--input", appdir, "-C", "zstd:level=5",
             "--output", temporary_output])
        temporary_output.chmod(0o755)
        # The runtime's extraction path is also a useful headless smoke test.
        run([temporary_output, "--version"], env={**os.environ,
            "APPIMAGE_EXTRACT_AND_RUN": "1", "QT_QPA_PLATFORM": "offscreen"})
        # KiCad can write .kicad_prl beside its input, even for CLI exports.
        # Keep fixture projects in staging so builds never modify samples/.
        pcb_fixture = staging / "board.kicad_pcb"
        shutil.copy2(ROOT / "samples/25x12.kicad_pcb", pcb_fixture)
        schematic_fixture = staging / "schematic"
        shutil.copytree(ROOT / "samples/gerber", schematic_fixture)
        run([temporary_output, "kicad-cli", "pcb", "export", "pdf", "--layers", "F.Cu,Edge.Cuts",
             "-o", staging / "smoke.pdf", pcb_fixture],
            env={**os.environ, "APPIMAGE_EXTRACT_AND_RUN": "1"})
        if not (staging / "smoke.pdf").is_file():
            raise RuntimeError("Bundled KiCad CLI did not create the PCB PDF")
        run([temporary_output, "kicad-cli", "sch", "export", "pdf",
             "-o", staging / "schematic.pdf", schematic_fixture / "gerber.kicad_sch"],
            env={**os.environ, "APPIMAGE_EXTRACT_AND_RUN": "1"})
        for name in ("smoke.pdf", "schematic.pdf"):
            with (staging / name).open("rb") as pdf:
                if pdf.read(5) != b"%PDF-":
                    raise RuntimeError(f"Bundled KiCad CLI produced an invalid {name}")
        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as destination:
            destination_path = Path(destination.name)
        try:
            shutil.copy2(temporary_output, destination_path)
            destination_path.replace(output)
        finally:
            destination_path.unlink(missing_ok=True)

    digest = sha256(output)
    output.with_suffix(".AppImage.sha256").write_text(f"{digest}  {output.name}\n")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT))
    output.with_suffix(".build-info.json").write_text(json.dumps({
        "version": version(), "architecture": "x86_64", "commit": commit,
        "tracked_changes": dirty, "python": platform.python_version(),
        "pyinstaller": importlib.metadata.version("PyInstaller"),
        "extraction_fallback": True, "compression": "zstd:level=5",
        "kicad_cli": cli_version, "tools": TOOLS, "addons": addon_hashes,
        "appimage_sha256": digest,
    }, indent=2) + "\n")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onedir", type=Path, default=ROOT / "dist/Kikakuka")
    parser.add_argument("--kicad-cli", type=Path, default=Path(shutil.which("kicad-cli") or "/usr/bin/kicad-cli"))
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--tool-cache", type=Path, default=ROOT / "build/linux-tools")
    args = parser.parse_args()
    print(build_appimage(args.onedir, args.kicad_cli, args.output_dir, args.tool_cache))


if __name__ == "__main__":
    main()
