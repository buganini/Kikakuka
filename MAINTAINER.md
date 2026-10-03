# Maintainer Notes

## Version bump

Kikakuka, FreekiCAD, the KiCad library, and the KiCad plugin each own their
version. Update the files for every module being released:

1. Kikakuka: `common.py` (`VERSION`)
2. FreekiCAD: `FreekiCAD/package.xml` (`<version>` and the release `<date>`)
   and `FreekiCAD/pyproject.toml` (`project.version`)
3. KiCad library: the single entry under `versions` in
   `kicad-addon/library/metadata.json`
4. KiCad plugin: the single entry under `versions` in
   `kicad-addon/plugin/metadata.json`
5. `CHANGELOG.md`: add a section containing only changes since the previous
   release

`make archive` clones `git@gitlab.com:buganini/metadata.git` into
`workdir/metadata/` when that checkout does not exist. On later runs it pulls
the checkout with `--ff-only`, preserves the package's older versions, adds or
replaces the current version, updates the package-level fields from the local
metadata, and copies the current icon. If the package does not exist in the
metadata checkout yet, it creates a new single-version entry.

The addon metadata generators assume that the GitHub release tag is exactly
the version string, without a `v` prefix. For example, version `7.5` produces:

```text
https://github.com/buganini/Kikakuka/releases/download/7.5/kikakuka-library-7.5.zip
```

## Pre-release checks

Confirm the intended module versions and ensure the two FreekiCAD version
sources match, then run:

```sh
python3 -m json.tool kicad-addon/library/metadata.json >/dev/null
python3 -m json.tool kicad-addon/plugin/metadata.json >/dev/null
env/bin/python -m unittest discover -s tests
make archive
tar -tzf Kikakuka-8.0.tar.gz >/dev/null
unzip -t kikakuka-library-8.0.zip
unzip -t kikakuka-plugin-8.0.zip
unzip -t freekicad-8.0.zip
```

Replace `8.0` above with each module's current version. `make archive` creates
the versioned source and package archives and edits the metadata fork in place:

```text
Kikakuka-{Kikakuka version}.tar.gz
kikakuka-library-{library version}.zip
kikakuka-plugin-{plugin version}.zip
freekicad-{FreekiCAD version}.zip
workdir/metadata/
└── packages/
    ├── com.github.buganini.kikakuka-footprints/
    │   ├── metadata.json
    │   └── icon.png
    └── com.github.buganini.kikakuka-plugin/
        ├── metadata.json
        └── icon.png
```

The source archive contains only tracked files. Checked-out submodules are
expanded recursively, and symlinks are dereferenced so archive consumers get
regular files.

## Shared Instance Manager modules

FreekiCAD is a separate deployment and cannot import modules from the
Kikakuka repository root. During development,
`FreekiCAD/freecad/FreekiCAD/{im_mesh,im_transport,instance_backend,kicad_api_retry}.py`
are relative symlinks into `im/`, and `kicad_paths.py` links to the
repository-root implementation. Edit those targets rather than creating
divergent copies under FreekiCAD. Release archives and `make sync` must
dereference all five links so the standalone addon contains ordinary
package-local files.

Keep imports between these shared modules package-relative. In particular,
they must not depend on `workspace.py`, `workspace_monitor.py`, or other
Kikakuka-only modules. The isolated import regression can be run directly with:

```sh
env/bin/python -m unittest tests.test_freekicad_entrypoint
```

Check that each package archive contains `metadata.json` at its root and that
its metadata has exactly one version without any `download_*` fields. Each
publish metadata file must contain `download_url`, `download_sha256`,
`download_size`, and `install_size`.

If any packaged file or package metadata changes after this step, run
`make archive` again. Upload exactly the generated archive whose hash appears
in the publish metadata; never replace the archive for an already published
version.

## Linux AppImage

The native x86_64 Linux build keeps PyInstaller's onedir payload and packages it
with sharun, uruntime and DwarFS, using the version pins and SHA256 checksums
from KiCad's AppImage toolchain. Install the build requirements on Ubuntu:

```sh
sudo apt install bash file binutils patchelf
env/bin/python -m pip install PyInstaller
env/bin/python build-package.py --check-dependencies
env/bin/python build-package.py
```

Use a Python environment that can import the installed KiCad `pcbnew` bindings
(including its matching Python ABI), as described in README.md. The installed
`kicad-cli` must be from the same KiCad release. The build downloads and verifies
the three pinned tools into `build/linux-tools/`; a verified cache permits
offline packaging. Nothing is installed into system directories.

The output is `dist/Kikakuka-{version}-x86_64.AppImage`, its SHA256 file and a
build-info JSON containing the source commit, tool versions and addon hashes.
The image includes KiCad CLI and all three offline addon ZIPs. KiCad and FreeCAD
GUI applications remain external. AppRun captures the original desktop
environment before sharun changes library and resource paths; editor launches
restore it and keep the user's normal HOME, settings and IPC directories.

To rebuild only the AppImage from an existing onedir:

```sh
env/bin/python tools/linux_appimage.py
```

The build checks the staged application, bundled CLI, addon archive integrity,
AppImage extraction launch and PCB/schematic PDF exports. For diagnostics, invoke the
bundled CLI with `./Kikakuka-{version}-x86_64.AppImage kicad-cli --version`.
Before release, also test normal FUSE launch,
Differ exports and Open in FreeCAD on a clean desktop VM. Extraction fallback
is enabled for images of any size; users need no PyInstaller or packaging tools.

## Release

1. Commit the version bump and release notes.
2. Create and push a tag whose name exactly matches the version.
3. Create the GitHub release for that tag.
4. Upload `Kikakuka-{version}.tar.gz`,
   `kikakuka-library-{version}.zip`, `kikakuka-plugin-{version}.zip`, and
   `freekicad-{version}.zip` as the applicable release assets, using each
   module's own version in its filename. `build-package.py` embeds the three
   addon ZIPs so Instance Manager can install the matching release offline.
5. If updating the separate FreekiCAD release mirror, review the target and
   run `make sync`; this command uses `rsync --delete` on `../FreekiCAD/`.

## KiCad official addon metadata

Review the changes under `workdir/metadata/` and confirm that all previous
versions remain in the package metadata. Run that checkout's official
packaging tool, commit and push its update branch, verify the GitLab CI
pipeline and temporary PCM repository, and then open a merge request against
`kicad/addons/metadata:main`.
