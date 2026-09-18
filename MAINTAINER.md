# Maintainer Notes

## Version bump

Kikakuka, FreekiCAD, and the KiCad footprint addon use the same version.
Update all of these files:

1. `common.py`: `VERSION`
2. `FreekiCAD/package.xml`: `<version>` and the release `<date>`
3. `FreekiCAD/pyproject.toml`: `project.version`
4. `kicad-addon/metadata.json`: the single entry under `versions`
5. `CHANGELOG.md`: add a section for the new version containing only changes
   since the previous release

`make archive` clones `git@gitlab.com:buganini/metadata.git` into
`workdir/metadata/` when that checkout does not exist. On later runs it pulls
the checkout with `--ff-only`, preserves the package's older versions, adds or
replaces the current version, updates the package-level fields from the local
metadata, and copies the current icon. If the package does not exist in the
metadata checkout yet, it creates a new single-version entry.

The addon metadata generator assumes that the GitHub release tag is exactly
the version string, without a `v` prefix. For example, version `7.5` produces:

```text
https://github.com/buganini/Kikakuka/releases/download/7.5/kikakuka-addon.zip
```

## Pre-release checks

Confirm that the four version sources match, then run:

```sh
python3 -m json.tool kicad-addon/metadata.json >/dev/null
env/bin/python -m unittest discover -s tests
make archive
unzip -t kikakuka-addon.zip
```

`make archive` creates the package archive and edits the metadata fork in
place:

```text
kikakuka-addon.zip
workdir/metadata/
└── packages/
    └── com.github.buganini.kikakuka-footprints/
        ├── metadata.json
        └── icon.png
```

Check that the package archive contains `metadata.json` at its root and that
its metadata has exactly one version without any `download_*` fields. The
publish metadata must contain `download_url`, `download_sha256`,
`download_size`, and `install_size`.

If any packaged file or package metadata changes after this step, run
`make archive` again. Upload exactly the generated archive whose hash appears
in the publish metadata; never replace the archive for an already published
version.

## Release

1. Commit the version bump and release notes.
2. Create and push a tag whose name exactly matches the version.
3. Create the GitHub release for that tag.
4. Upload `kikakuka-addon.zip` as a release asset. The existing GitHub release
   workflow creates the source archive but does not upload this addon archive.
5. If updating the separate FreekiCAD release mirror, review the target and
   run `make sync`; this command uses `rsync --delete` on `../FreekiCAD/`.

## KiCad official addon metadata

Review the changes under `workdir/metadata/` and confirm that all previous
versions remain in the package metadata. Run that checkout's official
packaging tool, commit and push its update branch, verify the GitLab CI
pipeline and temporary PCM repository, and then open a merge request against
`kicad/addons/metadata:main`.
