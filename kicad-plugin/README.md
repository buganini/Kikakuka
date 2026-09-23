# Kikakuka KiCad plugin

This KiCad 10 IPC plugin provides one PCB Editor action: **Add Placeholder 3D Models**.

The action is shown as a PCB Editor toolbar button with the Kikakuka icon. Its button visibility can be changed under **Preferences → Preferences… → Action Plugins**. KiCad 10 does not list IPC actions in the legacy **Tools → External Plugins** submenu.

The action scans every footprint on the active board. If a footprint has no 3D model whose resolved file exists, and it has non-empty `SizeX`, `SizeY`, and `SizeZ` properties, the action replaces its invalid model entries with Kikakuka's 1 mm unit cube and scales it to those dimensions. Values may use `mm`, `in`, or `mil`; values without a suffix are millimetres.

Install the separate `com.github.buganini.kikakuka-footprints` library package before using the action, because that package supplies `unit-cube.step`.
