# Instance Manager

Kikakuka and each FreekiCAD process host an equivalent Instance Manager
node. FreekiCAD starts its node on package import, before its workbench is
selected, including in FreeCADCmd.
GUI FreeCAD processes also publish their open document state; FreeCADCmd does
not publish its own open documents.
For the differences between FreeCAD and KiCad document lifecycles, see
[FreeCAD vs KiCad document lifecycle](FREECAD_VS_KICAD.md).
The common coordination logic is in
[`im_mesh.py`](../FreekiCAD/freecad/FreekiCAD/im_mesh.py), local transport in
[`im_transport.py`](../FreekiCAD/freecad/FreekiCAD/im_transport.py), and editor-specific
operations are in
[`instance_backend.py`](../FreekiCAD/freecad/FreekiCAD/instance_backend.py).

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

Different-file launches of the same editor are serialized by a second,
per-program OS lock. This keeps process-list PID inference and KiCad's initial
socket setup from overlapping. For PCBs, the launch slot remains held until
KiCad's IPC reports the requested board (up to 30 seconds). Schematic/project
launches have no equivalent document probe, so they keep a short settling
period before the next KiCad launch. The outer request may wait up to 120
seconds to accommodate queued opens.

## State and refresh

Successful KiCad and FreeCAD opens broadcast file-to-editor-PID events directly
to every currently discovered node. In GUI FreeCAD, FreekiCAD observes document
create/activate/save/close events and broadcasts paths with a saved `FileName`.
Unsaved documents have no path to publish. Multiple GUI documents in one process
retain separate mappings; FreeCADCmd does not publish its own documents.
Deletions use timestamped tombstones so an old
snapshot cannot revive a removed mapping. A joining node and an executor
refresh snapshots from peers on demand; the Monitor does the same at startup
and on manual Refresh. The Monitor also sends `freecad-list-documents`
directly to every responding GUI FreeCAD node. Each node scans
`FreeCAD.listDocuments()` on its GUI thread and returns its saved file paths.
FreekiCAD's `.kkkk_asm` importer also records the source path, because its
generated FreeCAD document has no
`FileName`. The Monitor reconciles that PID's entries, including removals
missed by an earlier event. The scan also queues
corrective broadcasts to the other nodes. Nodes that cannot answer are left
unchanged until a later refresh. FreeCADCmd does not provide this action.
Before opening a FreeCAD file, the executor asks the responding GUI nodes to
find and activate that path. A match selects its FreeCAD document and MDI tab,
then brings that process to the foreground. If the document is not open but a
GUI FreeCAD node exists, the executor sends `freecad-open-document` to that
node and waits for FreekiCAD to create the document in the same process. STEP
files use FreeCAD's non-modal importer; imported files without a native
`FileName` are associated with their source path for later scans. A new
process is launched only when no GUI FreeCAD node can be reached. The live
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

Kikakuka and FreekiCAD require `psutil`. FreekiCAD's
optional KiCad integration additionally requires `kicad-python` and
`shapely`; install those in the Python environment **inside FreeCAD**, not only in Kikakuka's
environment. FreeCAD Addon Manager may not install `psutil`
automatically on builds whose allowed-package list excludes it.

The current backend can verify a PCB's open document through KiCad IPC. KiCad
schematics do not have an equivalent verified-document probe here, so a live
schematic PID association is best-effort. The 120-second request limit prevents
an indefinitely blocked GUI workflow; a modal dialog or unresponsive KiCad
may return a timeout and require a retry. No background heartbeat detects a
document changing in an otherwise-live editor; the next demand-triggered
operation rechecks the PCB mapping.
