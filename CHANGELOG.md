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