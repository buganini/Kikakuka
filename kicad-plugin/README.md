# Kikakuka KiCad plugin

This KiCad 10 IPC plugin provides three PCB Editor actions:

* **Populate Placeholder 3D Models** scans every footprint on the active board. If a footprint has no 3D model whose resolved file exists, and it has non-empty `SizeX`, `SizeY`, and `SizeZ` properties, the action replaces its invalid model entries with Kikakuka's 1 mm unit cube and scales it to those dimensions, then opens or focuses KiCad's 3D Viewer. Values may use `mm`, `in`, or `mil`; values without a suffix are millimetres.
* **Coupler 3D Viewer** adds or updates the colored helper STEP model on every `CouplerFixed` and `CouplerMoving` footprint, then opens or focuses KiCad's 3D Viewer. Its model transform follows `Z`, `Tilt`, and `Offset`: `Z` is along the PCB surface normal, `Tilt` rotates around footprint-local X, and positive `Offset` follows the footprint triangle. Lengths may use `mm`, `in`, `mil`, or `um`/`µm`; unitless lengths are millimetres. Tilt is in degrees and may optionally use `deg` or `°`.
* **Hide Couplers** hides the Kikakuka coupler helper models without removing them, then opens or focuses KiCad's 3D Viewer. **Coupler 3D Viewer** makes them visible again. The coupler footprints are `Unspecified`, so their helpers appear under **Virtual Models** in KiCad's 3D Viewer and can be excluded from STEP export with **Ignore 'Unspecified' components**.

The actions are shown as PCB Editor toolbar buttons with the Kikakuka icon. Their button visibility can be changed under **Preferences → Preferences… → Action Plugins**. KiCad 10 does not list IPC actions in the legacy **Tools → External Plugins** submenu.

Install the separate `com.github.buganini.kikakuka-footprints` library package before using these model actions, because it supplies `unit-cube.step`, `coupler-fixed.step`, and `coupler-moving.step`.
