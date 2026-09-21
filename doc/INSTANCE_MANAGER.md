# Instance Manager

## Component boundary

The Instance Manager is a separate component from the Workspace Manager. It
owns process identity, file-to-PID mappings, editor launch/reuse/focus, and
KiCad IPC socket resolution. Its lifetime and behavior must not depend on
whether any workspace tab is open. This is a component boundary, not a
requirement to run in a separate OS process.

The Workspace Manager owns workspace files, project trees, and tabs. It is a
client of the Instance Manager when a user opens a file. FreekiCAD, the shared
file opener, and the Monitor UI are clients as well. In particular, the
Monitor displays an instance snapshot; it must not own or repair PID state.

**Implementation status:** this boundary is the intended design. Today the
PID map and launch code still live in `MainUI`/`WorkspaceUI` in `workspace.py`,
while `WorkspaceBus` in `workspace_bus.py` handles KiCad IPC. Moving those
responsibilities into an independent component is not yet implemented.

## PID state and lifecycle

The current implementation keeps a best-effort, in-memory map from an open
file to the process handling it:

```text
pidmap[absolute_file_path] = process_id
pidmap[":differ"] = differ_process_id
```

The Instance Manager should own this state rather than a workspace UI. It is
not persisted with the workspace list. A PID identifies a process, not
necessarily its currently open document; KiCad board mappings are checked
against the KiCad IPC API before an IPC socket is returned. Closing a
workspace removes its tab, but does not terminate editor processes or clear
instance state.

## Recording and reusing processes

- Opening a KiCad PCB or schematic uses the OS file association. On macOS and
  Linux, `posix_open_file()` compares process snapshots before and after the
  open command (with a three-second wait) to infer the editor PID; Windows
  uses the same approach after `os.startfile()`. If no PID can be identified,
  the file still opens but cannot immediately be added to `pidmap`.
- Opening FreeCAD records the launched or discovered PID. Windows and Linux
  remove that mapping when the launched process exits; macOS does not have an
  exit waiter for its discovered PID. Panelizer and Differ run as separate
  Kikakuka processes; their paths, or `:differ`, are mapped to their PIDs and
  removed when those subprocesses exit.
- An open request first tries an existing mapping. It may focus that process
  instead of opening another copy. Dead PIDs are discarded when checked with
  `psutil.pid_exists()`; a stale entry is not a guarantee that the editor
  still has the same file open.

The window-focus implementation is platform-specific: macOS uses
`osascript`, Windows uses window APIs, and Linux currently has no
`bringToFront()` implementation.

## KiCad IPC and PID recovery

The current IPC endpoint, `WorkspaceBus` in `workspace_bus.py`, serves
requests from FreekiCAD and the shared file opener. It is hosted by `MainUI`
today, but belongs to the Instance Manager boundary. It listens on
`/tmp/kikakuka.sock` on Unix and localhost TCP port 19780 on Windows, and
maintains a temporary `_pending_open_pids` map for KiCad launches whose
board is not ready yet.

At startup, the bus scans existing KiCad IPC sockets and attempts to rebuild
board-path-to-PID mappings. It repeats that scan immediately before opening a
KiCad file through the bus, so a board manually opened since startup can be
reused. For `api-<PID>.sock`, the filename supplies the candidate PID. For the
first instance's generic `api.sock`, the bus uses the oldest KiCad editor PID
without a PID-named socket. This association is an inference; the subsequent
KiCad IPC probe checks which board the socket actually serves.

The bus probes a candidate socket through `kipy` to read the board filename
and project path. A verified board path updates `pidmap`, removing another
path previously mapped to the same PID. A busy editor remains pending and is
retried during startup recovery. A stale socket with no live KiCad owner is
removed only if it is a Unix socket and cannot be connected to.

For a FreekiCAD action that needs a PCB IPC socket, the bus waits for an
ongoing open or startup recovery, then opens KiCad if necessary. It checks
that the PID is alive, waits for a responsive socket, and compares the board
path reported by KiCad with the requested path. A mismatch clears or repairs
the mapping and retries resolution; a pending launch is not considered ready
merely because another instance's generic `api.sock` responds. The passive
`monitor-couplers` action does not launch KiCad. The separate `open-file`
action opens or focuses a PCB/schematic and returns without waiting for its
IPC API to become ready. The `list` action reports the current `pidmap`
grouped by PID; it does not enumerate every OS process.

## Monitor client

The Monitor tab is a manually refreshed presentation of instance state, not
the source of `pidmap`. Currently `workspace_monitor.py` enumerates running
KiCad and FreeCAD GUI processes with `psutil.process_iter()`. For each PID, it
uses a known `pidmap` path first, then tries the process command line and
working directory. When the file cannot be determined, the path is shown as
`Unknown`. It does not query open files or KiCad's active document, and it
does not automatically refresh on process-open or process-close events. The
process snapshot logic should move behind the Instance Manager interface;
the tab should only request and display that snapshot.

Process inspection is best-effort across platforms: another user's process
may deny access to its command line or working directory. A process can also
exit between enumeration and inspection. These cases are skipped or shown
without a path; the manager does not request elevated privileges.

## Privilege policy

Instance management must work as a regular desktop user. Do not depend on
APIs that require root or administrator privileges, including system-wide
socket-owner queries such as `psutil.net_connections(kind="unix")` on macOS.
Use PID-named KiCad sockets, ordinary process enumeration, and KiCad's own
IPC response instead. Treat inaccessible process details as unavailable and
keep a non-privileged fallback; do not prompt for elevation just to populate
or repair `pidmap`.

## Current implementation references

- [`workspace.py`](../workspace.py): transitional home of `MainUI.pidmap`,
  editor launch/focus, Panelizer/Differ subprocesses, and the Monitor tab.
- [`workspace_bus.py`](../workspace_bus.py): transitional KiCad IPC endpoint,
  socket discovery, mapping recovery, and pending launches.
- [`workspace_monitor.py`](../workspace_monitor.py): process snapshot and
  best-effort file-path inference, currently called directly by the UI.
- [`pcb_open.py`](../pcb_open.py): socket request to the manager with an OS
  file-association fallback when the request cannot be fulfilled.
