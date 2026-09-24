# Instance Manager

Kikakuka and each FreekiCAD process host an equivalent Instance Manager
node. FreekiCAD starts its node on package import, before its workbench is
selected, including in FreeCADCmd.
Standalone Kikakuka tools start their local node on demand before opening a
KiCad file, so the Fabrication Planner and Differ can reuse an existing editor
without requiring the Workspace Manager to be running.
GUI FreeCAD processes also publish their open document state; FreeCADCmd does
not publish its own open documents.
For the differences between FreeCAD and KiCad document lifecycles, see
[Instance lifecycle](INSTANCE_LIFECYCLE.md).
The common coordination logic is in
[`im_mesh.py`](../im/im_mesh.py), local transport in
[`im_transport.py`](../im/im_transport.py), and editor-specific
operations are in
[`instance_backend.py`](../im/instance_backend.py).
FreekiCAD exposes these shared modules through relative symlinks in its Python
package. Standalone FreekiCAD synchronization and release archives dereference
the links so distributed packages contain regular files.

## Discovery and election

On macOS and Linux, each node listens at
`/tmp/kikakuka-<UID>/<PID>-<process-start-ms>.sock`; the per-user directory is
mode 0700 and socket files are mode 0600. Windows uses a named pipe of the form
`\\.\pipe\kikakuka-<SID-hash>-<PID>-<process-start-ms>`; the short hash comes
from the current process's Windows account SID, not its username or runtime
directory path. A short node-ID suffix is possible when tests host multiple
nodes in one process. There is no per-node JSON registration file. A shared
random token is stored in the private runtime directory
(`%LOCALAPPDATA%\Kikakuka\instances` on Windows); nodes
reject requests without it. This directory also holds the cross-process lock
files.

Discovery scans socket files or the Windows pipe namespace **on demand**. If
Windows pipe enumeration is unavailable, it derives names from the process
list. A `hello` request checks protocol version, PID, creation time, and
capabilities before election; `psutil` validates the PID and creation time.
Unix socket files are removed only when confirmed stale. Windows named pipes
disappear when their final handle closes and need no stale-file cleanup. Live
nodes are sorted by PID and node ID. There is no heartbeat, shared memory,
privileged socket-owner inspection, or elevated-privilege requirement.

A caller first sends a `dispatch` request to the lowest-PID capable node; PCB
requests skip nodes without `kicad-python`. The receiver
acknowledges within the 1-second request timeout and performs the operation in
a worker thread. The caller polls that node for the result, up to 120 seconds
by default. If a node fails before acknowledging or while being polled, the
caller tries the next candidate. A request ID prevents repeated execution on
one node. An OS file lock keyed by canonical path serializes opens across
different nodes after a failover or concurrent elections. The executor always
re-probes KiCad's IPC before opening, so a second executor can reuse an editor
opened by the first. If no node is running, the shared file opener falls back
to the system file association; a node's explicit error does **not** fall back
and risk opening a duplicate.

An `open-file` PCB request may set `ensure_fresh`. The executor first waits
for and verifies the matching board through KiCad IPC, retrying transient
busy/not-ready responses before considering a new launch. If that board was
already open, it then calls `RevertDocument` through the same ready board proxy
before focusing the editor; a board opened by the request already came from
disk and is not reverted again. The Fabrication Planner uses this after each
successful export so an existing PCB Editor displays the newly exported file.

Different-file launches of the same editor are serialized by a second,
per-program OS lock. This keeps process-list PID inference and KiCad's initial
socket setup from overlapping. For PCBs, the launch slot remains held until
KiCad's IPC reports the requested board (up to 30 seconds). Schematic/project
launches have no equivalent document probe, so they keep a short settling
period before the next KiCad launch. The outer request may wait up to 120
seconds to accommodate queued opens.

## Finding a pcbnew PID

KiCad's PCB IPC sockets are separate from the Instance Manager mesh sockets.
On Unix, the backend scans `/tmp/kicad` for `api-<PID>.sock` and `api.sock`;
on Windows, it also enumerates the corresponding named pipes. It reads the
ordinary process list on demand to find KiCad editor PIDs and creation times:

- For `api-<PID>.sock`, the PID is in the socket name, but the backend still
  requires a matching live KiCad editor process before using it.
- `api.sock` contains no PID. The backend assigns it to the oldest KiCad
  editor process that has not already been matched to a PID-specific socket.
  This is a hacky process-order heuristic, not a PID supplied by KiCad IPC.

For each candidate socket, the backend asks KiCad's API for the open board
name (and project path if the name is relative). Only a path matching the
requested PCB is accepted for reuse, focus, or PCB IPC operations; a saved
file-to-PID mapping alone is not proof that the board remains open. The
Instance Manager tab uses the same socket-and-board probe to rebuild its PCB
rows on manual refresh.

When launching a new KiCad editor, the current launcher also compares editor
process lists before and after launch to infer the new PID. For a PCB, that
inference is not the final answer: the backend waits up to 30 seconds for the
requested board to appear through KiCad IPC and uses the PID assigned to that
socket. Process-list enumeration is still needed to validate PID-specific
sockets and to assign a PID to `api.sock`; it is not a background monitor.

## Finding an eeschema PID (KiCad 10)

For `.kicad_sch` (and `.kicad_pro`) there is no PCB-style IPC probe that
reports which document an editor currently has open. If the mesh already has
a file-to-PID mapping and that PID still exists, the executor reuses it. This
is only a best-effort hint: a live process may have switched or closed its
document without the mapping being updated.

Otherwise the backend opens the file through its system association. It scans
KiCad editor processes before and after launch, waits up to eight seconds for
a new PID, and selects the newly appeared process with the latest creation
time. The scan includes process names such as `eeschema`, `kicad`, and
`pcbnew`; it does not verify that the chosen process opened the requested
schematic. Schematic/project launches then retain the per-program launch lock
for another three seconds to let process startup settle.

The Instance Manager tab also enumerates editor processes on refresh. If no
file-to-PID mapping is known, it may infer a `.kicad_sch` or `.kicad_pro`
path from the process command line (resolving relative paths against its
working directory). If neither source supplies a path, the row shows an
unknown file rather than claiming a verified document.

## FreeCAD document APIs and PID

Each GUI FreeCAD process runs its own FreekiCAD mesh node. The node's `hello`
response identifies its PID and whether GUI document actions are available,
so FreeCAD documents do not need KiCad-style socket-to-PID inference.
`freecad-list-documents` runs `FreeCAD.listDocuments()` on that process's GUI
thread. Each document's `FileName` supplies its path; for imported files with
an empty `FileName`, FreekiCAD remembers the source path separately. An
unsaved document without a known source path has no file path to report.

FreekiCAD registers a `FreeCAD.addDocumentObserver()` observer. Its create,
activate, change, save, and delete callbacks update the file-to-PID mapping
without polling. A manual Instance Manager refresh calls
`freecad-list-documents` again to reconcile missed events and show one row per
open file, even when several files share the same FreeCAD PID. FreeCADCmd runs
a mesh node but does not provide these GUI document actions.

For an already-open file, `freecad-activate-document` finds it with
`FreeCAD.listDocuments()`, selects it with `FreeCAD.setActiveDocument()`, and
uses FreeCAD's Qt MDI area to select the matching visible tab. For a new file,
`freecad-open-document` queues work on the GUI thread: `.FCStd` uses
`FreeCAD.openDocument()`, STEP uses `Import.open()` (without a modal import
dialog), and `.kkkk_asm` uses FreekiCAD's assembly importer. The mesh ACK
confirms the request was queued, not that document loading has finished.

## State and refresh

Successful KiCad and FreeCAD opens broadcast file-to-editor-PID events directly
to every currently discovered node. Deletions use timestamped tombstones so
an old snapshot cannot revive a removed mapping. A joining node and an
executor refresh snapshots from peers on demand; the Instance Manager tab does
the same at startup and on manual Refresh. The tab probes KiCad PCB IPC
endpoints to rebuild paths for boards opened outside Kikakuka and requests
`freecad-list-documents` from responding GUI FreeCAD nodes. It reconciles each
FreeCAD PID's entries, including removals missed by earlier events, and queues
corrective broadcasts to the other nodes. Nodes that cannot answer are left
unchanged until a later refresh.
Before opening a FreeCAD file, the executor asks the responding GUI nodes to
find and activate that path. A match selects its FreeCAD document and MDI tab,
then brings that process to the foreground. If the document is not open but a
GUI FreeCAD node exists, the executor sends `freecad-open-document` to that
node. Its acknowledgement means the GUI open was queued; the caller does not
wait for a potentially slow import or launch another process. STEP
files use FreeCAD's non-modal importer; imported files without a native
`FileName` are associated with their source path for later scans. A new
process is launched only when no FreeCAD GUI process is running. If a GUI
process is running but its node cannot be reached, opening fails rather than
launching a duplicate. The live
document check takes precedence over an old PID mapping. After a new process
starts, the launcher waits for its FreekiCAD node and binds the requested path
to its imported document; process creation alone is not reported as a
successful file open.
When refreshing, dead editor PIDs are removed and broadcast. There is no
periodic poll. A mapping is only a hint: for PCBs, the KiCad API-reported board
path is checked before focus or socket resolution. `monitor-couplers` remains
passive and never launches an editor.

FreekiCAD's [`im_client.py`](../FreekiCAD/freecad/FreekiCAD/im_client.py)
provides asynchronous and synchronous KiCad requests for linked PCB objects.
The Workspace Manager's KiCad and FreeCAD open actions and `pcb_open.py` use
the same route.
The old `/tmp/kikakuka.sock` / Windows port 19780 daemon and its tests have
been removed. KiCad's own
`api.sock` and `api-<PID>.sock` are unrelated and remain in use.

## Platform and permissions

macOS starts a new FreeCAD with `open -a FreeCAD -n -W --args <file>` and
uses AppleScript for best-effort focus; other new editors use `open -n`.
Windows uses file associations and Win32 foreground APIs; Linux uses
`xdg-open` and currently has no reliable cross-desktop focus operation.
File-to-PID discovery uses ordinary process enumeration and KiCad's IPC. No
`psutil.net_connections(kind="unix")` or other privileged process/socket
inspection is used. An inaccessible process is treated as unavailable, never
as a reason to request elevation.

The private runtime directory and request token provide best-effort local-user
isolation, not an authentication boundary against malicious processes running
as the same desktop user (which can read the token). Keep the per-user
temporary directory private.

## Dependencies and limitations

Instance Manager requires `psutil`. FreeCAD Addon Manager may not install it
automatically on builds whose allowed-package list excludes it.

The current backend can verify a PCB's open document through KiCad IPC. KiCad
schematics do not have an equivalent verified-document probe here, so a live
schematic PID association is best-effort. The 120-second request limit prevents
an indefinitely blocked GUI workflow; a modal dialog or unresponsive KiCad
may return a timeout and require a retry. No background heartbeat detects a
document changing in an otherwise-live editor; the next demand-triggered
operation rechecks the PCB mapping.
