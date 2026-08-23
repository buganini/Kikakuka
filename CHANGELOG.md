# 6.9:
* Panelizer
    * Copy first PCB constraints to panel
    * Integrate KiKit copperfill function #32
    * Add fiducial layer selector #32
    * Add panel cell source area tolerance #30
    * Move warnings and errors to a standalone panel #32
    * Show a crosshair when clicking messages with coordinates
    * Work around KiCad project settings save crash
* FreekiCAD
    * Wait for KiCad board API readiness
    * Avoid reusing unverified KiCad sockets
* Add release workflow with submodule support
* Update KiKit to 1.8.1
* Update PUI
* Add hard discontinued sample board

# 6.8
* Panelizer
    * Display build exceptions as warnings
    * Display a warning for unsupported Shapely versions
    * Warn on mouse bites along curved edges
    * Patch KiKit outline chaining tolerance
* Add all-samples test runner
* Add discontinued sample board
* Update documentation

# 6.7
* Gerber: fix KiCad 10 compatibility
* Add memory appendBoard test
* Update documentation

# 6.6
* Panelizer
    * Reuse loaded PCB to reduce memory footprint

# 6.5
* Panelizer
    * Restore board layer/thickness check
    * Avoid appendBoard in non-exporting build
    * Speed up non-exporting build
    * Process footprint/silkscreen only in exporting build
    * Reduce render trigger

# 6.4
* Panelizer
    * Run garbage collection after build
    * Prevent silent build errors
    * Early return when there is no PCB
* Update Shapely to 2.0.7
* Update PUI
* Fix Windows build missing DLLs

# 6.3
* Differ: make Windows KiCad CLI path version-agnostic
* Update Windows instructions

# 6.2
* Workspace Manager
    * Deduplicate files
    * Set directory for workspace-to-new-panelization flow
* Panelizer: omit tooling holes and fiducials when frame is not used
* Update PUI

# 6.1
* FreekiCAD
    * Add flex PCB bending support via User.4 layer bend lines
    * Support bend angle/radius from User.4 text annotations
    * Improve bend cutting, wedge reconstruction, adjacency handling, and debug views
    * Move components with bent board geometry
    * Improve KiCad IPC readiness handling and retry logging
    * Add FPC/maze/radius bending samples and screenshot
* Fix KiCad 10 BUILDEXPR compatibility #28
* Gerber: exclude NPTH in string matching in find_PTH() #29
* Update KiKit

# 6.0
* Add FreekiCAD addon for linking KiCad boards into FreeCAD
    * Render board outlines, drill holes, colors, and 3D models
    * Support outline editing through FreeCAD sketches
    * Add auto-reload and per-object reload
    * Add KiCad/FreeCAD workspace bus integration
    * Support KiCad IPC socket discovery on macOS, Windows, and Linux/POSIX
    * Improve STEP model loading, color extraction, reuse, and reload performance
* Update README with FreekiCAD setup notes
* Add .gitignore files and remove committed pyc files

# 5.9
* Panelizer
    * Support frame PCB
    * Add tolerance for v-cuts building #27
    * Fix auto tabbing bug
    * Avoid loading empty frame PCB
    * Display memory usage
* Add rounded 8x8 panel sample
* Update pypdfium2 to 5.4.0
* Update PUI for Canvas.drawShapely()

# 5.8
* Panelizer
    * Implement build options
    * Fix build variants for boards not renamed
    * Display PCB loading errors
* Differ: enumerate layers in kicad_pcb
* Workspace Manager: save .kkkk with ensure_ascii=False
* Update README with git submodule instructions
* Update PUI

# 5.7
* Panelizer: add buttons for adding gerber folder & gerber zip

# 5.6:
* Gerber: write footprint name to the converted .kicad_pcb

# 5.5:
* Experimental Gerber support
    * Allow attaching BOM/CPL (converted to reference-only footprints)
* Add tooling holes
* Add fiducials
* Fix opening differ

# 5.4
* trim flags

# 5.3
* fix extracting flags from BUILDEXPR containing parentesis

# 5.2
* Panelizer
    * Fix island removal

# 5.1
* Workspace Manager:
    * Convert to relative path: don't convert non-existent path

# 5.0
* Panelizer
    * Support spacing=0 #22
    * Add frame size "Fit" button #22
    * Add pcb clearance setting #22
    * Add "move by distance" function
    * Wrap rotation to 360 deg to work around numerical errors #22
    * Update the render function to improve the display of the substrate area.
    * Add vcut_or_skip and hidden vcut_unsafe cut methods
    * Add "Generate Holes" for user to determine which area should be holes/fills in tight frame + zero-spacing scenario #22
    * Fix mousebites offset by preserving cut line direction #23
    * Add configurable angle setting for manual tabs #24
    * Tabs: better handling of non-perpendicular approaching angle #24

* Built variants

* Workspace Manager:
    * Display libraries
    * "Convert to relative path" button (related to KIPRJMOD)

# 4.6.1
* Fix compatibility with pypdfmium2 v5.0.0 #21

# 4.6
* Workspace display project with folder name
* Differ display file path with folder name
* Fix error in remove_tab() when the button is clicked again before the UI finishes refreshing #19
* Allow specifying tab width individually #20
* Implement dragging for manual tabs #20

# 4.5.1
* Fix memeory leak (in PUI) #16
* Remove non-existed workspace file

# 4.5
* Bugfixes
* UI/UX improvements
* Preserve silkscreen text regardless of reference renaming

# 4.4.1
* Fix updating canvas when highlighting manual tab

# 4.4
* Panelizer
    * Remove isolated substrates
* Fix false conflicts in frames area when both X/Y frames are used
* Speedup drawing board substrate
* Workspace Manager
    * Fix updating files list after creating new panelization

# 4.3.2
* Fix recalling project window
* Fix bug in tabs management (PUI 0.19)

# 4.3.1
* Fix open panelizer in no-workspace context
* Fix opening differ in no-workspace context

# 4.3
* Workspace Manager Allow Opening multiple workspaces
* Differ Update rendering
* Panelizer Update auto tabbing

# 4.2.1
* Fix kicad-cli pcb export pdf with KiCad v9.0.2

# 4.2
* Implement window recalling on windows
* Rework macos window recalling improve PID finding, handle closed PID

# 4.1
* Fix subprocess.run on non-windows
* Hide console when running kicad-cli on windows

# 4.0
* Rename to Kikakuka
* Update kikit to 1.7.1
* Update instruction with kicad v9
* Workspace manager with window recalling on macOS
* Differ with git support

# 3.5
* Add edge.cuts to v-cut output layer options #7
* Implement v-cuts merging #8
* Improve scrolling to zoom #9
* Bugfixes

# 3.4
* Fix numerical error in V-Cut check
* Add "None" cut method
* Display components' version info
* Codesign + notarize for macOS build

# 3.3
* Fix numerical error in oblique mousebites
* Add a checkbox to toggle pcb display
* Add a checkbox to toggle hole display
* Add icon

# 3.2
* Support mousebites offset #5
* Fix outward manual tab direction
* Fix numerical error in making tabs

# 3.1
* Fix coordinate check for hide_outside_reference_value

# 3.0
* Manual tab
* Autotabs find tabs to the interiors of substrates
* Add net/ref renamer configs

# 2.2
* Add macOS build script
* Add option for exporting mill fillets
* Bundle files required for mousebites #1

# 2.1
* Add windows build script
* Fix types arg for files dialogs

# 2.0
* Arbitrary rotation
* Compact alignment by collision detection
* Implement "Hide Out-of-Board References/Values"
* Add vc+mb cut method

# 1.0
* Free-form arrangement
* 90x rotation
* Alignment by bounds
* Hole
