# Demo Recording

This directory contains reusable instructions and scenarios for recording GUI
demonstrations.

Follow the selected file in `scenarios/` from a clean and predictable starting
state. Keep the application content readable, perform each action at a pace
that is easy to follow, and leave enough time for the result of each step to be
visible before continuing.

Mouse click events must be visually apparent in the recording.

Record video only; do not capture microphone or system audio.

## Pilot Run

Before the final recording, perform a **Pilot Run** stage. Use it to locate UI
controls, verify window focus and layout, and identify actions that need more
precise operator cues. Record the findings in
`pilot-run/<scenario-name>.md`, using the same basename as the scenario. Capture
hard-to-recognize icons during the pilot run, crop them into `pilot-run/`, and
link those images from the pilot-run notes. Name reusable icon references by
program and function, rather than by scenario; for example,
`pcbnew-3dviewer-rotate-y-ccw.png` can be reused by every scenario that uses
that control. Keep only the notes and reference images needed for the final
recording. Delete temporary full-window or full-screen captures after
cropping the useful controls from them.

The final recording should use the notes as an operator reference; do not use
the pilot-run footage as the finished demonstration. Maximize the window being
inspected during the pilot run so its controls are not obscured, then restore
the layout required by the scenario for the final recording. When identifying
an icon, leave the pointer over it until its tooltip appears, and use the
tooltip to verify the control's name before documenting or capturing it.

During the pilot run, complete any platform-specific screen-recording
permission setup before detecting devices. Use the platform CLI to detect the
available recording devices and record the current screen-device mapping in the
pilot-run notes. Do not assume a fixed index: camera and phone devices may
occupy different indices on another machine.

## Recording

Arrange application windows with `[GUI automation]` so that scenarios use
predictable positions and sizes. Use the platform's visible work area, not the
raw display bounds, so arranged windows do not overlap the menu bar, Dock,
taskbar, or system bars.

When a scenario says to open a file **directly**, use the platform's
command-line launcher or the project-provided opener named by the scenario
instead of navigating a graphical Open dialog. For example, use `open -n` on
macOS when the scenario has no more specific opener. Scenarios may still
explicitly use an Open dialog when showing that workflow is relevant.

If direct opening does not show the expected file, first suspect the scenario
input. Validate that the file still parses and that the setup edits did not
damage it before trying alternate launch methods.

Give every script action a completion time in parentheses to control the pace
of the recording. The time runs from the start of the action until the requested
UI state is visible. For example, `(within 1 s)` means that the action must
reach its visible result within one second; it is not an additional delay
afterward. Specify caption and result display durations separately.

Save the finished recording as `out/<scenario-name>.mp4`, where
`<scenario-name>` is the scenario filename without the `.md` extension. For
example, `scenarios/example.md` must produce `out/example.mp4`.

## Command Runners

Scenario commands must say where they run. Use these runner labels:

- `[Terminal.app]`: run only the foreground FFmpeg recording command when macOS
  Screen Recording permission is required.
- `[Sub shell]`: run directly in a non-sandboxed shell attached to the task,
  outside the Codex background sandbox. Do not route these commands through
  AppleScript or GUI terminal automation. A Codex background `exec` shell,
  shell subshell expression such as `(...)`, or PTY is not a substitute if it
  cannot create `.git/index.lock`.
- `[GUI automation]`: run through the automation driver that moves the pointer,
  clicks, types, arranges windows, or reads GUI state.
- `[Codex sandbox]`: run in the background Codex shell only for safe inspection
  that does not need GUI permissions, screen recording permissions, or writes to
  Git metadata.

Verify the command-runner environment before starting a pilot run or final
recording. The task must provide a working `[Sub shell]` that can update Git
metadata and launch GUI applications through the operating system. If that
runner is unavailable, or its privilege review cannot run, move the recording
to a task with the required environment before changing scenario state. Do not
substitute `[Codex sandbox]`, AppleScript, GUI terminal automation, Finder, an
Open dialog, or a pre-launched empty application for a documented `[Sub shell]`
direct-open command.

When a project supplies a Python virtual environment, activate it in
`[Sub shell]` before invoking `python3`. Do not assume that the system or
Homebrew Python contains the project's dependencies merely because the same
command works in an already-activated interactive shell. Each scenario must
show the activation command alongside the Python command that depends on it.

Do not move `[Sub shell]` or `[Terminal.app]` commands into `[Codex sandbox]`.
In particular, Git checkout, setup, teardown, direct file opening, and other
CLI preparation commands should run in `[Sub shell]`. Only the actual FFmpeg
screen capture needs `[Terminal.app]` on macOS, because that command depends on
Screen Recording permission.

## Scenario Files

Every scenario must contain these sections in order:

```markdown
# Scenario Title

## Setup

Describe the required files, application state, and recording preparation.

## Script

List the actions to perform and the subtitles to display.

## Teardown

Describe how to stop recording, close temporary state, and save the output.
```

## macOS

When arranging windows, base the frames on the Finder desktop window's
`bounds`, which represents the visible work area on the current display. Avoid
using `bounds of window of desktop`, raw display dimensions, or hard-coded
heights that can place windows under the menu bar or Dock.

Use `[Sub shell]` for macOS CLI setup and cleanup commands, including Git,
process cleanup, direct file opening, and non-recording probes. Do not route
these commands through AppleScript or GUI terminal automation.

Run Git commands such as `git checkout` from `[Sub shell]`, not from
`[Codex sandbox]`. The sub shell must be non-sandboxed so Git can create and
remove metadata such as `.git/index.lock`.

If a shell reports that Git cannot create `.git/index.lock`, do not replace
`git checkout` with manual file rewriting. Rerun the Git command from
`[Sub shell]` so the repository index is updated through Git itself.

Grant Terminal.app access under
**System Settings > Privacy & Security > Screen & System Audio Recording**,
then fully quit and relaunch it. Run only the FFmpeg recording command from
that foreground Terminal.app session, not from a background automation shell.

If the recording is driven by a separate GUI automation tool, grant
Accessibility permission to that tool. Screen Recording permission lets FFmpeg
capture the display, but Accessibility permission is still required for
synthetic pointer and keyboard events to reach GUI applications reliably.

List AVFoundation devices from the same Terminal.app session used for
recording:

```sh
ffmpeg -f avfoundation -list_devices true -i ""
```

Use the index named `Capture screen 0` as the video input. For example, if the
CLI reports it as device `2`, use `-i "2:none"`; the `:none` suffix disables
audio capture. Do not use a camera or phone-camera index.

An FFmpeg capture must use no audio input and disable the audio track:

```sh
ffmpeg -f avfoundation -video_size 1920x1080 -framerate 30 \
  -i "<screen-index>:none" -an out/<scenario-name>.mp4
```
