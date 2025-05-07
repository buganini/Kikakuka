import PyInstaller.__main__
import platform
import subprocess
import os
import itertools
import glob
import shutil

import kikit
kikit_base = os.path.dirname(kikit.__file__)

from differ import kicad_cli

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

# kikit's footprint for mousebites driils
pyinstaller_args.extend(["--add-data", f"{os.path.join(kikit_base, 'resources', 'kikit.pretty')}:kikit.pretty"])

# pypdfium2 for differ
pyinstaller_args.extend(["--collect-all=pypdfium2_raw", "--collect-all=pypdfium2"])

print(pyinstaller_args)

PyInstaller.__main__.run([
    'kikakuka.py',
    "--name", "Kikakuka",
    "--onedir",
    "--noconfirm",
    "--windowed",
    "--add-data=resources/icon.ico:.",
    *pyinstaller_args
])

# kicad-cli for differ
if platform.system() == "Darwin":
    if os.path.exists(kicad_cli):
        shutil.copy(kicad_cli, "dist/Kikakuka.app/Contents/MacOS")
    else:
        print("KiCad CLI not found at", kicad_cli)
        exit(1)
    os.makedirs("dist/Kikakuka.app/Contents/PlugIns", exist_ok=True)
    shutil.copy("/Applications/KiCad/KiCad.app/Contents/PlugIns/_eeschema.kiface", "dist/Kikakuka.app/Contents/PlugIns")
    shutil.copy("/Applications/KiCad/KiCad.app/Contents/PlugIns/_pcbnew.kiface", "dist/Kikakuka.app/Contents/PlugIns")
    shutil.copytree("/Applications/KiCad/KiCad.app/Contents/PlugIns/sim", "dist/Kikakuka.app/Contents/PlugIns/sim")
elif platform.system() == "Windows":
    if os.path.exists(kicad_cli):
        os.makedirs("dist/Kikakuka/_internal/KiCad/bin", exist_ok=True)
        shutil.copy(kicad_cli, "dist/Kikakuka/_internal/KiCad/bin")
        shutil.copy(os.path.join(os.path.dirname(kicad_cli), "_pcbnew.dll"), "dist/Kikakuka/_internal/KiCad/bin")
        shutil.copy(os.path.join(os.path.dirname(kicad_cli), "_eeschema.dll"), "dist/Kikakuka/_internal/KiCad/bin")
    else:
        print("KiCad CLI not found at", kicad_cli)
        exit(1)

if codesign_identity:
    for path in itertools.chain(
        glob.glob("dist/Kikakuka.app/**/*.so", recursive=True),
        glob.glob("dist/Kikakuka.app/**/*.kiface", recursive=True),
        glob.glob("dist/Kikakuka.app/**/*.dylib", recursive=True),
        glob.glob("dist/Kikakuka.app/**/Python3", recursive=True),
        ["dist/Kikakuka.app"],
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
    if os.path.exists("Kikakuka.dmg"):
        os.unlink("Kikakuka.dmg")
    subprocess.run([
        "create-dmg",
        "--volname", "Kikakuka",
        "--volicon", "resources/icon.icns",
        "--app-drop-link", "0", "0",
        *create_dmg_args,
        "Kikakuka.dmg", "dist/Kikakuka.app"
    ])
    if codesign_identity:
        subprocess.run(["spctl", "-a", "-t", "open", "--context", "context:primary-signature", "-v", "Kikakuka.dmg"])
