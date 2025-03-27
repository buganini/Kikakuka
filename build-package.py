import PyInstaller.__main__
import platform
import subprocess
import os
import itertools
import glob

import kikit
kikit_base = os.path.dirname(kikit.__file__)

create_dmg = False
codesign_identity = None

pyinstaller_args = []
create_dmg_args = []
if platform.system()=="Darwin":
    pyinstaller_args.extend(["--add-binary", f"/Applications/KiCad/KiCad.app/Contents/Frameworks/*.dylib:."])
    pyinstaller_args.extend(["-i", 'resources/icon.icns'])
    subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"])
    codesign_identity = input("Enter the codesign identity \"Developer ID Application: XXXXXX (XXXXXXXXXX)\" (leave empty for no signing): ").strip()
    if codesign_identity:
        create_dmg_args.extend(["--codesign", codesign_identity])
        create_dmg_args.extend(["--notarize", "notarytool-creds"])
    create_dmg = True
else:
    pyinstaller_args.extend(["-i", 'resources/icon.ico'])

pyinstaller_args.extend(["--add-data", f"{os.path.join(kikit_base, 'resources', 'kikit.pretty')}:kikit.pretty"])

print(pyinstaller_args)

PyInstaller.__main__.run([
    'kikit-ui.py',
    "--onedir",
    "--noconfirm",
    "--windowed",
    "--add-data=resources/icon.ico:.",
    *pyinstaller_args
])

if codesign_identity:
    for path in itertools.chain(
        glob.glob("dist/kikit-ui.app/**/*.so", recursive=True),
        glob.glob("dist/kikit-ui.app/**/*.dylib", recursive=True),
        glob.glob("dist/kikit-ui.app/**/Python3", recursive=True),
        ["dist/kikit-ui.app"],
    ):
        print("codesign", path)
        subprocess.run(["codesign",
            "--sign", codesign_identity,
            "--entitlements", "resources/entitlements.plist",
            "--timestamp",
            "--deep",
            str(path),
            "--force",
            "--options", "runtime"
        ])

if create_dmg:
    if os.path.exists("kikit-ui.dmg"):
        os.unlink("kikit-ui.dmg")
    subprocess.run([
        "create-dmg",
        "--volname", "KiKit-UI",
        "--volicon", "resources/icon.icns",
        "--app-drop-link", "0", "0",
        *create_dmg_args,
        "kikit-ui.dmg", "dist/kikit-ui.app"
    ])
    if codesign_identity:
        subprocess.run(["spctl", "-a", "-t", "open", "--context", "context:primary-signature", "-v", "kikit-ui.dmg"])
