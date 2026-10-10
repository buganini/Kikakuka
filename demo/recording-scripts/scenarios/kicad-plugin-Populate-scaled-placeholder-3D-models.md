# Populate Scaled Placeholder 3D Models

## Setup

- `[Sub shell]` Run `killall pcbnew` to close all PCB Editor instances and
  release the sample board's file lock.

  ```sh
  killall pcbnew
  ```

- `[Sub shell]` Restore `samples/fpc2.kicad_pcb` from Git.

  ```sh
  git checkout -- samples/fpc2.kicad_pcb
  ```

- `[Sub shell]` In `samples/fpc2.kicad_pcb`, find the footprint whose
  reference is `missing-3d-model`. Remove its `SizeX`, `SizeY`, and `SizeZ`
  properties, then save the board.

  ```sh
  python3 demo/recording-scripts/scripts/prepare-kicad-plugin-Populate-scaled-placeholder-3D-models.py
  ```
- `[Sub shell]` Validate that KiCad can read the prepared board before opening
  it.

  ```sh
  /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli pcb export pos \
    --output /tmp/kikakuka-fpc2.pos samples/fpc2.kicad_pcb
  ```

- `[Sub shell]` Open `samples/fpc2.kicad_pcb` directly through Kikakuka's
  instance-manager open path.

  ```sh
  source env/bin/activate
  python3 -m kikakuka --open samples/fpc2.kicad_pcb
  ```

  (wait for ready)
- `[GUI automation]` Zoom to all objects in the PCB Editor.
  Operator cue: use **View > Zoom to Objects** so the target footprint has a
  reproducible screen position before recording pointer actions.
- `[GUI automation]` Open the 3D Viewer.
  Operator cue: use **View > 3D Viewer**.
- `[GUI automation]` Arrange the PCB Editor across the left four-fifths of the
  screen and the 3D Viewer across the right one-third. Both windows should fill
  the available work-area height without overlapping the macOS menu bar or Dock.
  Operator cue: make the PCB Editor the main foreground window before setting
  its frame, then set the 3D Viewer frame and bring it forward.
- `[GUI automation]` Focus the 3D Viewer and zoom out three times. (within 2 s)
  Operator cue: click the toolbar button with the magnifying glass and minus
  sign three times.
- `[GUI automation]` Return focus to the PCB Editor.
  Operator cue: bring the window titled `fpc2 — PCB Editor` forward explicitly.
- `[GUI automation]` In the Selection Filter, disable **All items**, then enable
  only **Footprints**. This prevents overlapping pads from intercepting the
  footprint click during the recording.

## Script
- `[GUI automation]` Display this caption for 5 s:

  `No Model`

- `[GUI automation]` Open the properties of the `missing-3d-model` footprint.
  (within 5 s)
  Operator cue: click the target footprint identified during the pilot run;
  do not use **Edit > Find** during recording. Use the QFN footprint shown in
  `pilot-run/kicad-plugin-Populate-scaled-placeholder-3D-models-target-footprint.png`.
  Click on its magenta footprint outline after the Selection Filter has been
  limited to **Footprints**, then use the pointer to open **Footprint
  Properties** from the context menu or another visible UI control.

- `[GUI automation]` Show its invalid 3D model entry while displaying this
  caption:

  `Invalid 3D model`

  (within 1 s; display for 5 s)
  Operator cue: use the pointer to select the **3D Models** tab in
  **Footprint Properties**, then leave the pointer still while the unresolved
  model entry is visible.

- `[GUI automation]` Set the following footprint properties:

  - `SizeX`: `3 mm`
  - `SizeY`: `3 mm`
  - `SizeZ`: `0.5 mm`

  (within 5 s)
  Operator cue: return to the **General** tab, use the plus button below the
  Fields table for each field, then edit the new row's Name and Value cells.

- `[GUI automation]` Close the footprint properties dialog. (within 0.5 s)
- `[GUI automation]` Run the **Populate Placeholder 3D Models** plugin action.
  (within 3 s)
  Operator cue: click the Kikakuka toolbar icon with the cube overlay.
- `[GUI automation]` Click **Rotate Y CCW** three times. (within 1 s)
  Operator cue: in the 3D Viewer toolbar, use the right-hand one of the two
  rotation buttons labelled `Y`.
- `[GUI automation]` Keep the populated model visible while displaying this
  caption:

  `Model is populated`

  (display for 3 s)

## Teardown

- `[Terminal.app]` Stop FFmpeg and save the finished recording as
  `out/kicad-plugin-Populate-scaled-placeholder-3D-models.mp4`.
- `[Sub shell]` Restore the sample board:

  ```sh
  git checkout -- samples/fpc2.kicad_pcb
  ```
