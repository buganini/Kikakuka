# Kikakuka KiCad Addons

<img src="library/resources/icon.png" alt="Logo" width="64" height="64">

This directory contains the two KiCad 10 addon packages used by Kikakuka:

* [`library`](library) provides the Kikakuka footprints and their supporting
  STEP models.
* [`plugin`](plugin) provides four IPC actions for the PCB Editor.

## Library

The KiCad library is stored under [`library`](library). Add
[`library/footprints/Kikakuka.pretty`](library/footprints/Kikakuka.pretty) to
KiCad's footprint library table (for example, as `Kikakuka`). Its
[`Kikakuka.3dshapes`](library/3dmodels/Kikakuka.3dshapes) directory contains
the unit-cube placeholder and optional coupler helper models.

* [`Variable`](library/footprints/Kikakuka.pretty/Variable.kicad_mod) displays
  its `Value` on the board. In the Kikakuka Fabrication Planner, use
  build-variant fields such as `Value#Flag` or `Value#Option=Choice` to show a
  value selected by the active build flags or options.
* [`StringTemplate`](library/footprints/Kikakuka.pretty/StringTemplate.kicad_mod)
  formats its `Value` during Kikakuka Fabrication Planner export. Braced
  placeholders are replaced with matching footprint properties or build
  options after build-variant fields have been applied.
* [`CouplerFixed`](library/footprints/Kikakuka.pretty/CouplerFixed.kicad_mod)
  and
  [`CouplerMoving`](library/footprints/Kikakuka.pretty/CouplerMoving.kicad_mod)
  define matching planes for
  [coupler-based PCB alignment](../README.md#coupler-based-pcb-alignment). Give a pair
  the same reference and use their `Z`, `Offset`, and `Tilt` properties as
  needed.
* [`CouplerAt`](library/footprints/Kikakuka.pretty/CouplerAt.kicad_mod) aligns
  its PCB to absolute FreeCAD world `TargetX`, `TargetY`, and
  `TargetZ` coordinates, all defaulting to zero. Place it on B.Cu for the
  usual behavior, where the PCB bottom surface is positioned at `TargetZ`;
  use F.Cu only to reference the top surface.

For coupler length properties (`Z`, `Offset`, `TargetX`, `TargetY`, and
`TargetZ`), unitless values are millimetres; supported suffixes are `mm`,
`in`, `mil`, and `um`/`µm`. `Tilt` is in degrees and may optionally use
`deg` or `°`.

All three coupler footprints use the `Unspecified` type, placing their helper
models under **Virtual Models** in KiCad's 3D Viewer.

The model actions require the `com.github.buganini.kikakuka-footprints`
library package to be installed when they run because it supplies the
unit-cube and coupler helper models.

## Actions

### Populate Placeholder 3D Models

<img src="plugin/plugins/placeholder-48.png"
     alt="Populate Placeholder 3D Models icon" width="48">

Scans every footprint on the active board. When a footprint has no valid 3D
model and defines non-empty `SizeX`, `SizeY`, and `SizeZ` properties, the
action assigns the library's 1 mm unit cube and scales it to those dimensions.
Invalid model entries are replaced. Dimensions may use `mm`, `in`, or `mil`;
values without a unit are interpreted as millimetres. The action opens or
focuses KiCad's 3D Viewer when it finishes.

[![Kikakuka - Placeholder 3D Models](https://img.youtube.com/vi/fXU9RkxEFV0/0.jpg)](https://www.youtube.com/watch?v=fXU9RkxEFV0)

### Coupler 3D Viewer

<img src="plugin/plugins/coupler-3d-viewer-48.png"
     alt="Coupler 3D Viewer icon" width="48">

Adds or updates the colored helper model for every `CouplerFixed` and
`CouplerMoving` footprint, makes the helpers visible, and opens or focuses the
3D Viewer. The model transform follows the footprint properties:

* `Z` offsets the helper along the PCB surface normal.
* `Tilt` rotates the helper around footprint-local X.
* `Offset` moves the helper along the footprint triangle direction.

Lengths may use `mm`, `in`, `mil`, or `um`/`µm`; unitless lengths are
millimetres. `Tilt` is in degrees and may optionally use `deg` or `°`.

### Hide Couplers

<img src="plugin/plugins/disable-coupler-helpers-48.png"
     alt="Hide Couplers icon" width="48">

Hides Kikakuka coupler helper models without removing their model settings,
then opens or focuses the 3D Viewer. Running **Coupler 3D Viewer** makes the
helpers visible again.

Coupler footprints have the `Unspecified` type, so their helpers appear under
**Virtual Models** in the 3D Viewer. They can be omitted from STEP export with
**Ignore 'Unspecified' components**.

### Open in FreeCAD

<img src="plugin/plugins/open-in-freecad-48.png"
     alt="Open in FreeCAD icon" width="48">

Opens the current PCB in FreeCAD, or updates an existing linked PCB using the
current KiCad editor contents. The PCB must have been saved once to establish
its file path. Later unsaved edits are read through the KiCad API; the action
does not save or revert the board.

The action chooses its target before bringing any existing FreeCAD window
forward:

1. Search the **active document of every connected FreeCAD instance** for a
   link to this PCB. Instances are searched by ascending PID; the first match
   wins, regardless of which window was previously in the foreground.
2. If no active document matches, search all documents in the same instance
   order and use the first matching document. Links are compared by normalized
   absolute paths, including symlink resolution, rather than filenames alone.
3. If no document matches, select the first connected instance for a new
   document. If FreeCAD is not running, start it and wait for FreekiCAD to
   connect. An already running instance without a responsive FreekiCAD node
   produces an error after the connection wait instead of launching a duplicate.
4. Bring the selected instance to the foreground and restore its window if
   minimized. Update the matching PCB objects in the selected document, or
   create a document and add the PCB. After loading, make that document and
its tab active. A new document's view is fitted to the imported geometry.

FreekiCAD compares the live PCB contents, import settings, text variables, and
external model file state with the last successful Open in FreeCAD import.
When unchanged, it skips geometry rebuilding and still activates the document
and brings the window forward. This cache stays in memory and is not saved in
FCStd; the first request after reopening a document imports normally. If the
comparison is unavailable, FreekiCAD reloads normally. **Reload KiCad PCB** in
the PCB object's context menu always forces a reload.

Searching does not reload PCBs, change active tabs, or bring unrelated windows
forward. The selected document remains the target even if the user changes
tabs while the request is in progress. If that document is closed or its PCB
link is removed before the update, the action reports an error.

Repeated clicks for the same PCB are ignored while its request is running.
Failed new imports close the document created by the action; existing
documents are not closed on failure. Errors are reported through the plugin's
error output.

This action requires FreeCAD with an updated FreekiCAD that supports PCB
selection and opening. The original FreekiCAD **8.1.1 release does not support
this action**; update FreekiCAD and restart FreeCAD before using it. On Linux,
`FreeCAD` or `freecad` must be available in `PATH` to launch a new instance.

## Finding the actions

The actions appear as buttons on the PCB Editor toolbar and can be configured
under **Preferences → Preferences… → Action Plugins**. KiCad 10 IPC actions do
not appear in the legacy **Tools → External Plugins** submenu.
