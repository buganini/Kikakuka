# KiCad Workspace Manager & Interactive GUI Panelizer
<img src="resources/icon.png" alt="Logo" width="64" height="64">

Kikakuka (企画課, きかくか, Planning Section) (formerly Kikit-UI) is mainly built on top of [KiKit](https://github.com/yaqwsx/KiKit), [Shapely](https://github.com/shapely/shapely) and [PUI](https://github.com/buganini/PUI).

# Features
* Workspace Manager
* Panelizer
    * Interactive arrangement, what you see is what you get
    * Freeform arrangement, not limited to M×N configuration
    * Multiple different PCB panelization
    * Auto/Manual tab creation
    * Auto V-cut/mousebites selection
    * Enable hole creation in panel substrate for extruded parts
    * Does not require coding skill

# kkkk file
The .kkkk file saves workspace information in JSON format.

# kikit_pnl file
The .kikit_pnl file saves panelization settings in JSON format, with PCB paths stored relative to the file's location.

# Global Alignment
![Global Alignment](screenshots/global_alignment.gif)

# Per-PCB Alignment
![Per-PCB Alignment](screenshots/single_alignment.gif)

# Substrate Hole
![Substrate Hole](screenshots/substrate_hole.gif)

# Tight Frame + Auto Tab + V-Cuts *or* Mousebites
![UI](screenshots/tight_frame_autotab_autocut.png)
## Output
![Output](screenshots/tight_frame_autotab_autocut_output.png)
## 3D Output
![3D Output](screenshots/tight_frame_autotab_autocut_output_3d.png)

# Tight Frame + Auto Tab + V-Cuts *and* Mousebites
![UI](screenshots/tight_frame_autotab_vcuts_and_mousebites.png)

# Loose Frame + Auto Tab + Mousebites
![UI](screenshots/loose_frame_autotab_mousebites.png)
## 3D Output
![3D Output](screenshots/loose_frame_autotab_mousebites_output_3d.png)

# Auto Tab
Tab position candidates are determined by the PCB edge and max_tab_spacing, prioritized by divided edge length (smaller first), and skipped if there is a nearby candidate (distance < max_tab_spacing/3) with higher priority.

In the image below with debug mode on, small red dots are tab position candidates, larger red circles are selected candidates, and the two rectangles represent the two half-bridge tabs.
![Auto Tab](screenshots/auto_tab.png)

# Manual Tab
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

# Load file
./env/bin/python3 kikakuka.py a.kikit_pnl

# Headless export
./env/bin/python3 kikakuka.py a.kikit_pnl out.kicad_pcb
```

# Contributors
* @buganini
* @dartrax
