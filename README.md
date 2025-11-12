# KiCad Workspace Manager &  Panelizer & Differ
<img src="resources/icon.png" alt="Logo" width="64" height="64">

Kikakuka (企画課, きかくか, Planning Section) (formerly Kikit-UI) is mainly built on top of [KiKit](https://github.com/yaqwsx/KiKit), [Shapely](https://github.com/shapely/shapely), [OpenCV](https://github.com/opencv/opencv-python), [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) and [PUI](https://github.com/buganini/PUI).

# Features
* Workspace Manager
    * Easily navigate between projects
        * Automatically open multiple KiCad instances on macOS
    * Recall windows of previously opened files (macOS and Windows only)
* Differ
    * Highlight changed areas
    * [Schematic diff viewer](#schematics-differ)
    * [PCB diff viewer](#pcb-differ)
    * Git support
* Panelizer
    * Build Variants (in the Panelizer)
        * Can be used for single PCB with frameless setting
        * Each PCB can have its own flag settings
    * Interactive arrangement with real-time preview
    * Freeform placement not limited to M×N grid configurations
    * Support for multiple different PCBs in a single panel
    * [Automatic](#auto-tab) or [manual](#manual-tab) tab creation
    * Automatic V-cut/mousebites selection
    * Enable [hole](#substrate-hole) creation in panel substrate for extruded parts
    * No coding skills required

# Workspace Manager
The `.kkkk` file saves workspace information in JSON format.
![Workspace Manager](screenshots/workspace.png)

# Differ
## Schematics Differ
![Schematics Differ](screenshots/sch_differ.gif)

## PCB Differ
![PCB Differ](screenshots/pcb_differ.png)
* A diff sample of [cynthion-hardware](https://github.com/greatscottgadgets/cynthion-hardware)

# Build Variants
Set `BUILDEXPR` in footprints' properties. This can be done quickly with `Symbol Fields Table` using the current sheet only scope. Remember to sync them to PCB afterward.
## BUILDEXPR
A boolean expression with operators:
* `~` Not
* `&` And
* `|` Or

e.g. `(A | ~B) & C`
![BUILDEXPR-Prop](screenshots/buildexpr-prop.png)

## Per-PCB flags settings
![BUILDEXPR-Flags](screenshots/buildexpr-flags.png)

## Footprints with the BUILDEXPR evaluated as false will be marked as DNP
![BUILDEXPR-Flags](screenshots/buildexpr-dnp.png)



# Panelizer
The `.kikit_pnl` file saves panelization settings in JSON format, with PCB paths stored relative to the file's location.

## Global Alignment
![Global Alignment](screenshots/global_alignment.gif)

## Per-PCB Alignment
![Per-PCB Alignment](screenshots/single_alignment.gif)

## Substrate Hole
![Substrate Hole](screenshots/substrate_hole.gif)

## Tight Frame + Auto Tab + V-Cuts *or* Mousebites
![UI](screenshots/tight_frame_autotab_autocut.png)
### Output
![Output](screenshots/tight_frame_autotab_autocut_output.png)
### 3D Output
![3D Output](screenshots/tight_frame_autotab_autocut_output_3d.png)

## Tight Frame + Auto Tab + V-Cuts *and* Mousebites
![UI](screenshots/tight_frame_autotab_vcuts_and_mousebites.png)

## Loose Frame + Auto Tab + Mousebites
![UI](screenshots/loose_frame_autotab_mousebites.png)
### 3D Output
![3D Output](screenshots/loose_frame_autotab_mousebites_output_3d.png)

## Auto Tab
Tab position candidates are determined by the PCB edge and max_tab_spacing, prioritized by divided edge length (smaller first), and skipped if there is a nearby candidate (distance < max_tab_spacing/3) with higher priority.

In the image below with debug mode on, small red dots are tab position candidates, larger red circles are selected candidates, and the two rectangles represent the two half-bridge tabs.
![Auto Tab](screenshots/auto_tab.png)

## Manual Tab
Auto tab is off for PCB with manual tabs.
Drag inside the PCB for moving selected tab, drag outside the PCB for changing the direction for the selected tab.
![Manual Tab](screenshots/manual_tab.gif)

# Run from source (Linux/macOS)
Make sure your python can import `pcbnew`
```
> python3 -c "import pcbnew; print(pcbnew._pcbnew)"
<module '_pcbnew' from '/usr/lib/python3/dist-packages/_pcbnew.so'>
```
On macOS, I have to use the python interpreter bundled with KiCAD
```
PYTHON=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
```

On Linux, you should be able to just use the your default python3
```
PYTHON=python3
```

Create a virtual environment and install dependencies
```
${PYTHON} -m venv --system-site-packages env
./env/bin/pip3 install -r requirements.txt
```

Run
```
./env/bin/python3 kikakuka.py
```

# Run from source (Windows)
On Windows the Python interpreter is at `C:\Program Files\KiCad\9.0\bin\python.exe`.
But however in my Windows environment venv is not working properly, here is how I run it with everything installed in the KiCad's environment.
```
"C:\Program Files\KiCad\9.0\bin\python.exe" -m pip install -r requirements.txt
"C:\Program Files\KiCad\9.0\bin\python.exe" kikakuka.py
```

# CLI Usage
```
# Just open it
./env/bin/python3 kikakuka.py

# Start with PCB files
./env/bin/python3 kikakuka.py a.kicad_pcb b.kicad_pcb...

# Load file (.kkkk or .kikit_pnl)
./env/bin/python3 kikakuka.py a.kikit_pnl

# Headless export
./env/bin/python3 kikakuka.py a.kikit_pnl out.kicad_pcb

# Differ
./env/bin/python3 kikakuka.py --differ a.kicad_sch b.kicad_sch
```

# Contributors
* @buganini
* @dartrax
