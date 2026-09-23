# Maintainer Notes

## Version bump

Kikakuka, FreekiCAD, and the KiCad addons use the same version.
Update all of these files:

1. `common.py`: `VERSION`
2. `FreekiCAD/package.xml`: `<version>` and the release `<date>`
3. `FreekiCAD/pyproject.toml`: `project.version`
4. `kicad-library/metadata.json`: the single entry under `versions`
5. `kicad-plugin/metadata.json`: the single entry under `versions`
6. `CHANGELOG.md`: add a section for the new version containing only changes
   since the previous release

`make archive` clones `git@gitlab.com:buganini/metadata.git` into
`workdir/metadata/` when that checkout does not exist. On later runs it pulls
the checkout with `--ff-only`, preserves the package's older versions, adds or
replaces the current version, updates the package-level fields from the local
metadata, and copies the current icon. If the package does not exist in the
metadata checkout yet, it creates a new single-version entry.

The addon metadata generators assume that the GitHub release tag is exactly
the version string, without a `v` prefix. For example, version `7.5` produces:

```text
https://github.com/buganini/Kikakuka/releases/download/7.5/kikakuka-library.zip
```

## Pre-release checks

Confirm that the five version sources match, then run:

```sh
python3 -m json.tool kicad-library/metadata.json >/dev/null
python3 -m json.tool kicad-plugin/metadata.json >/dev/null
env/bin/python -m unittest discover -s tests
make archive
unzip -t kikakuka-library.zip
unzip -t kikakuka-plugin.zip
```

`make archive` creates both package archives and edits the metadata fork in
place:

```text
kikakuka-library.zip
kikakuka-plugin.zip
workdir/metadata/
└── packages/
    ├── com.github.buganini.kikakuka-footprints/
    │   ├── metadata.json
    │   └── icon.png
    └── com.github.buganini.kikakuka-plugin/
        └── metadata.json
```

Check that each package archive contains `metadata.json` at its root and that
its metadata has exactly one version without any `download_*` fields. Each
publish metadata file must contain `download_url`, `download_sha256`,
`download_size`, and `install_size`.

If any packaged file or package metadata changes after this step, run
`make archive` again. Upload exactly the generated archive whose hash appears
in the publish metadata; never replace the archive for an already published
version.

## Release

1. Commit the version bump and release notes.
2. Create and push a tag whose name exactly matches the version.
3. Create the GitHub release for that tag.
4. Upload `kikakuka-library.zip` and `kikakuka-plugin.zip` as release assets.
   The existing GitHub release workflow creates the source archive but does
   not upload these addon archives.
5. If updating the separate FreekiCAD release mirror, review the target and
   run `make sync`; this command uses `rsync --delete` on `../FreekiCAD/`.

## KiCad official addon metadata

Review the changes under `workdir/metadata/` and confirm that all previous
versions remain in the package metadata. Run that checkout's official
packaging tool, commit and push its update branch, verify the GitLab CI
pipeline and temporary PCM repository, and then open a merge request against
`kicad/addons/metadata:main`.
