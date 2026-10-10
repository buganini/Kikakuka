# CADhoc

CADhoc is the local coordination library for instance management and
cross-application integration. Kikakuka and each FreekiCAD process host an
equivalent peer CADhoc node,
while the KiCad add-on uses a client-only node. FreekiCAD starts its node
on package import, before its workbench is selected, including in FreeCADCmd.
The client-only node shares CADhoc discovery, authentication, locking, and
request implementation, but does not listen on a mesh endpoint, publish
mappings, or execute requests for peers.
Standalone Kikakuka tools start their local node on demand before opening a
KiCad file, so the Fabrication Planner and Differ can reuse an existing editor
without requiring the Workspace Manager to be running.
GUI Fabrication Planner processes also publish their open FabPlan path. The
Workspace Manager uses that mapping to activate the owning process instead of
opening the same FabPlan in another process. Headless FabPlan builds do not
start an instance node or publish files.
GUI FreeCAD processes also publish their open document state; FreeCADCmd does
not publish its own open documents.
For the differences between FreeCAD and KiCad document lifecycles, see
[Instance lifecycle](../doc/INSTANCE_LIFECYCLE.md).
The common coordination logic is in
[`mesh.py`](mesh.py), local transport in
[`transport.py`](transport.py), and editor-specific
operations are in
[`instance_backend.py`](instance_backend.py).
FreekiCAD exposes these shared modules through relative symlinks in its Python
package. Standalone FreekiCAD synchronization and release archives dereference
the links so distributed packages contain regular files.

## Python API

The `cadhoc` package root does not re-export names. Import application-facing APIs
from the module that defines them. Names beginning with `_`, including
`mesh._exchange()` and `mesh._file_lock()`, are implementation details;
`transport` is the low-level socket/named-pipe transport rather than an
application API. Use the functions below so token handling, discovery,
election, retries, and result polling remain centralized.

Paths passed to CADhoc should be absolute. Public file APIs canonicalize paths with
`abspath()`, `realpath()`, and `normcase()` before comparing or publishing
them. Calls that may discover, launch, or wait for an editor are blocking and
should run outside a GUI thread.

### Client and coordination API

| API | Result and intended use |
| --- | --- |
| `mesh.request(message, timeout=120.0)` | Send one dispatched editor request. `message` contains the nested editor action, not `mesh_action`, token, or request ID. CADhoc discovers a capable node, waits for its final reply, and returns the reply dictionary. No node raises `ConnectionError`; exhausted communication/result waits raise `TimeoutError`; an editor failure normally returns `status: "error"`. |
| `mesh.discover()` | Probe live nodes and return their `hello` data plus `endpoint`, ordered by PID and node ID. This is a read-only capability query, not a cached registry. |
| `mesh.scan_freecad_documents()` | Return `(pid, paths)` pairs from responding FreeCAD GUI nodes. Unavailable or temporarily unresponsive nodes are omitted. |
| `mesh.activate_open_freecad_document(filepath, target_pid=None, before_activate=None)` | Select an already-open FreeCAD document and return its PID, or `None`. It never opens a missing document. `before_activate(pid)` can foreground the process before its MDI tab changes. |
| `mesh.open_in_freecad_node(filepath, before_open=None)` | Queue a file in an existing FreeCAD GUI node and return that PID, or `None` when no suitable node exists. Acceptance means the GUI owns the job; it does not mean a long import has finished. |
| `mesh.bind_freecad_source(pid, filepath, timeout=45)` | After launching FreeCAD with an importable file, wait for that node and associate the imported document with its source path. Returns a boolean. |
| `freecad_open.open_board(filepath, socket_path)` | KiCad add-on entry point for **Open in FreeCAD**. It probes GUI nodes, chooses the target document/process, and queues an update or creation. Returns the selected FreeCAD PID, or `None` when a duplicate click is coalesced. |

`mesh.request()` accepts the editor actions listed in
[Dispatched editor actions](#dispatched-editor-actions). It returns a completed
reply, so callers do not send `dispatch` or poll `result` themselves. Direct
mesh commands are documented for protocol implementations and diagnostics;
normal Python callers should not construct them.

### Hosting a node

`mesh.start_node(handle, on_change=None)` starts one node for the current
process and returns its `InstanceNode`. Repeated calls return the same local
node until `close()` is called; they do not replace the original callbacks.
`handle(request)` executes a dispatched editor request in a worker thread and
returns a reply dictionary. `on_change(filepath, pid, socket_path)` is called
when a newer mapping event is applied. Kikakuka and FreekiCAD normally use
`instance_backend.handle` as the standard editor handler.

The returned node exposes these lifecycle and mapping methods:

| API | Behavior |
| --- | --- |
| `node.publish(filepath, pid, socket_path=None)` | Publish or replace a file-to-process mapping. Pass `pid=None` to publish a tombstone when the file closes. |
| `node.refresh()` | Merge peer snapshots on demand and tombstone mappings whose process has exited. |
| `node.snapshot()` | Refresh and return the current live `{canonical_path: pid}` mapping. Tombstones and socket details are omitted. |
| `node.close()` | Stop the listener, remove its Unix socket when applicable, and clear the process-local singleton. Safe to call more than once. |
| `mesh.local_node()` | Return the process-local node, or `None` if this process is client-only or its node has closed. |

A FreeCAD GUI host can additionally register document providers on its node:

| Registration | Callback contract |
| --- | --- |
| `set_document_provider(provider)` | `provider() -> iterable[str]` of open canonical document/source paths. |
| `set_document_activator(activator)` | `activator(filepath) -> bool`; select an existing document and tab without opening it. |
| `set_document_opener(opener)` | `opener(filepath) -> bool`; open/import a general FreeCAD-supported file. |
| `set_pcb_opener(opener)` | `opener(filepath, socket_path, *, create, active_only, probe, document_name)`; probe or update/create a linked PCB document. |
| `set_source_registrar(registrar)` | `registrar(filepath) -> bool`; bind a launch-imported document to its source path. |

Provider callbacks can be invoked by listener or worker threads. A GUI host is
responsible for dispatching GUI-only work to its main thread. Registering the
document provider advertises FreeCAD GUI document capability in `hello`;
registering the PCB opener advertises the current Open in FreeCAD capability.

### Supporting APIs

These helpers are shared by Kikakuka and FreekiCAD but are more specialized
than the normal request/node interface:

| Module | Public helpers |
| --- | --- |
| `mesh` | `activate_or_open_file()` serializes reuse versus launch for an application-owned file; `launch_lock()` serializes editor startup; `runtime_dir()` returns the private transport/lock directory. `owned_process_iter()`, `owned_process()`, `owned_pid_exists()`, and `owned_pids()` perform same-user process inspection; `is_kicad_editor_process()` classifies editor process names. |
| `instance_backend` | `handle()` is the standard dispatched editor backend; `scan_open_kicad_boards()` reports verified KiCad boards; `external_process_environment()` and `freecad_process_environment()` construct sanitized child environments; `freecad_launch_log_path()` returns the fallback launch log; `ensure_windows_kicad_api_sentinel()` and `launch_linux_kicad()` implement platform-specific launch support. |
| `kicad_api_retry` | `retry_kicad_call()`, `get_ready_kicad_board()`, `probe_kicad_board_ready()`, and `is_kicad_retryable_error()` centralize retries for KiCad busy/not-ready responses. |
| `linux_window` | `bring_pid_to_front()` performs best-effort X11/EWMH activation; `x11_display_available()` and `xwayland_available()` report availability; activation failures raise `WindowActivationError`. |

### Examples

#### Send a client-only request

A process does not need to host a node to send a request. This opens or reuses
the matching editor and brings it to the foreground:

```python
import pathlib

import cadhoc.mesh

filepath = str(pathlib.Path("boards/demo.kicad_pcb").resolve())
reply = cadhoc.mesh.request({"action": "open-file", "filepath": filepath})
if reply.get("status") != "ok":
    raise RuntimeError(reply.get("message", "editor request failed"))

print(reply.get("pid"), reply.get("socket"))
```

For a PCB freshly exported to disk, add `"ensure_fresh": True`. This reverts
an already-open board through KiCad IPC; it is intentionally valid only for an
`open-file` `.kicad_pcb` request.

#### Embed the standard editor node

Use the shared backend when the host process should both send and execute
editor requests:

```python
import cadhoc.instance_backend
import cadhoc.mesh


def mapping_changed(filepath, pid, socket_path):
    print("mapping", filepath, pid, socket_path)


node = cadhoc.mesh.start_node(
    cadhoc.instance_backend.handle,
    mapping_changed,
)
try:
    # Run the host application's event loop here.
    run_application()
finally:
    node.close()
```

The listener is background-threaded, but `cadhoc.mesh.request()` itself blocks
while the selected node works and should not be called directly from a GUI
event handler.

#### Publish an application-owned document

Applications such as Fabrication Planner can publish a file owned by their own
process so another process activates it instead of opening a duplicate:

```python
import os
import pathlib

import cadhoc.instance_backend
import cadhoc.mesh

node = cadhoc.mesh.start_node(cadhoc.instance_backend.handle)
filepath = str(pathlib.Path("jobs/demo.kkkk_fab").resolve())
node.publish(filepath, os.getpid())

try:
    run_document_window()
finally:
    node.publish(filepath, None)
    node.close()
```

Publishing records ownership only; it does not open, focus, or validate the
document. The host must publish a tombstone when the document closes or moves.

## Standalone node for debugging

Run a foreground mesh node from the repository root with either command:

```sh
python3 -m cadhoc
python3 cadhoc
```

Both forms run the same entry point. The process logs its PID, local endpoint,
KiCad API availability, dispatched editor requests and replies, mapping
changes, and shutdown. Press Ctrl-C (or send SIGTERM) to close the listener and
remove its Unix socket. Use `--log-level` to change the default `info` level.

Compare the two KiCad PID/socket discovery paths without starting a mesh node:

```sh
python3 -m cadhoc test
python3 cadhoc test
```

The `enumerate` section scans the Unix socket directory or Windows named-pipe
namespace and reads PID-specific endpoint ownership only from endpoint names.
A generic `api.sock` is shown with `?` because enumeration alone cannot identify
its owner. The second section obtains ownership independently and directly:
`psutil.Process(pid).net_connections(kind="unix")` on macOS/Linux, and
`GetNamedPipeServerProcessId()` on Windows. Both process paths are restricted
to same-user KiCad editor processes and ignore inaccessible processes. These
probes are implemented separately from the production backend so the test does
not reuse or alter CADhoc's normal discovery path.

This entry point runs the same editor backend as nodes embedded in Kikakuka and
FreekiCAD. It is useful for diagnostics or for keeping a visible executor in
the foreground; it is not a permanent leader, and normal applications do not
require it to be running.

## Shared-package boundary

FreekiCAD is deployed independently from Kikakuka. In this repository its
`FreekiCAD.cadhoc` subpackage is a relative directory symlink to `cadhoc/`,
while `kicad_paths.py` links to the repository-root implementation. Release
and Addon Manager packaging dereference both links recursively into regular
files and directories inside the FreekiCAD Python package. Shared CADhoc
modules must therefore use only the Python
standard library, declared FreekiCAD dependencies, and package-relative CADhoc
imports. They must not import Kikakuka-root modules such as
`workspace_monitor.py`. A standalone-package import test enforces this
boundary without placing the Kikakuka repository root on `PYTHONPATH`.

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

The token is created under a file lock when `mesh-token` is missing and is
reused across application restarts; there is no scheduled rotation. Senders
and receivers read the current token file on each request, so replacing or
recreating the file does not leave long-running nodes using an old cached
value. Invalid token files fail validation instead of falling back to an old
secret. A replacement between sending and receiving can reject that in-flight
request; later requests use the current token. Older nodes that cache the
token at startup must be updated and restarted once to gain this behavior.
The token authenticates local requests; it does not encrypt traffic or exclude
programs running as the same OS user that can read the file.

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

## File-opening and foreground policy

CADhoc separates explicit user navigation from background integration
work. A user navigation request brings its target editor to the foreground;
an integration request normally preserves the user's current foreground
application. Document discovery and read-only probes never change tabs, reload
documents, launch applications, or focus windows.

"Foreground" below means that CADhoc makes a best-effort OS activation request
after selecting the target process and document. "Background" means that CADhoc
does not request activation and, where supported, launches the editor without
activating it. Window-manager restrictions can still prevent or override the
requested behavior; see [Process launching and access](#process-launching-and-access).

| Entry point | Reuse or open behavior | Foreground behavior |
| --- | --- | --- |
| Workspace Manager, Differ, Fabrication Planner, or `kikakuka.py --open` | Send `open-file`; reuse the matching editor when possible, otherwise open one. | Foreground the reused or newly opened KiCad or FreeCAD process. |
| Instance Manager **Go to** | Navigate to the selected already-open instance; for FreeCAD, also select its matching document/tab. It never opens a file or launches a new process. | Foreground the selected running process. |
| Fabrication Planner after a successful export | Send PCB `open-file` with `ensure_fresh: true`; revert an already-open board from disk, or open the newly exported board. | Foreground KiCad after the board is ready. |
| FreekiCAD **Open in KiCad** | Send PCB `open-file` without `ensure_fresh`; reuse or open the board without reverting unsaved KiCad content. | Foreground KiCad. |
| FreekiCAD `reload`, `open-sketch`, `move-component`, or `update-coupler` | Reuse or open the PCB Editor only to obtain a verified KiCad IPC socket; FreekiCAD performs the subsequent operation. | Keep KiCad in the background and preserve the requesting FreeCAD process in front when possible. A possible **Open Anyway** prompt is the exception described below. |
| FreekiCAD `monitor-couplers` | Resolve only an already-open PCB; never launch KiCad. | No focus change. |
| KiCad add-on **Open in FreeCAD**, read-only probe phase | Search active documents first, then other documents, for an existing linked PCB. | No tab activation, reload, process launch, or focus change. |
| KiCad add-on **Open in FreeCAD**, selected update/create phase | Update the selected document; create one in a responding FreeCAD GUI node when no match exists; launch FreeCAD only when no GUI instance is running. | Restore and foreground the selected or newly launched FreeCAD process, then activate the selected document. |
| General `.FCStd`, `.step`, `.stp`, `.stl`, or `.kkkk_asm` `open-file` | Activate an already-open document; otherwise queue the open in a responding FreeCAD GUI node; launch a new process only when no FreeCAD GUI is running. | Foreground FreeCAD and select the matching document/tab when available. |
| Shared opener with no reachable CADhoc node | Fall back to the platform-specific launcher. An explicit error from a responding node does not fall back, because that could open a duplicate. | Controlled by the fallback launcher rather than CADhoc. |

### Reuse and launch order

- For `.kicad_pcb`, KiCad IPC must report the requested board path before CADhoc
  reuses the process or considers a new launch successful. A stored mapping by
  itself is not sufficient.
- For `.kicad_sch` and `.kicad_pro`, KiCad exposes no equivalent document
  probe. CADhoc reuses a live file-to-PID mapping when available; otherwise it
  launches the matching editor and infers its PID on a best-effort basis.
- For FreeCAD files, live GUI document queries take precedence over stored
  mappings. CADhoc first activates an existing document, then asks a responding GUI
  node to open it, and launches a new FreeCAD process only when no GUI process
  is running. If FreeCAD is running but its FreekiCAD node is unreachable, the
  request fails instead of opening a duplicate process.
- Per-file locks serialize requests for the same canonical path. A separate
  per-program launch lock prevents two different files from racing through
  editor startup and PID/socket discovery.

### Background KiCad integration

FreekiCAD integration requests launch a missing KiCad editor without activation
on macOS (`open -g`) and Windows (`SW_SHOWNOACTIVATE`). On Linux, Instance
Manager does not issue an explicit focus request. Linux launches the matching
KiCad executable directly; in a Wayland session with a usable XWayland display,
only the child KiCad process receives `GDK_BACKEND=x11` so later EWMH activation
can find its window. Because macOS applications may activate themselves despite
`open -g`, FreekiCAD includes its PID in integration requests and Instance
Manager restores that process after launch and again when PCB IPC becomes
ready.

The exception is a KiCad lock that may produce an **Open Anyway** prompt.
Before launching, the backend checks KiCad's sibling lock path
(`~<filename>.<ext>.lck`) and reads its `username` and `hostname` fields. A lock
owned by the current user and host with no other same-user KiCad editor is
treated as stale and can be reclaimed without a prompt. A foreign lock, or a
current-user lock while another same-user KiCad editor is running, may require
input; the new process is focused before CADhoc waits for PCB IPC so the modal
prompt remains visible. On macOS, CADhoc does not restore FreeCAD over an unresolved
prompt.

The lock check must happen before process launch because KiCad creates its own
lock file during every successful open. Checking afterwards would incorrectly
classify every new editor as requiring foreground attention.

## Protocol command reference

The current mesh protocol version is **3**, reported by `hello`. Requests and
replies are JSON objects. Every request requires the shared `token` and a
`mesh_action`; `_exchange()` supplies the token automatically. Unix sockets
frame UTF-8 JSON with a four-byte big-endian byte length. Windows named pipes
use the transport's message framing. The maximum JSON message size is 16 MiB.

There are two command levels: `mesh_action` selects a node operation;
`dispatch` carries an editor request whose `action` is handled by
`instance_backend.py`. The two action fields are not interchangeable.

### Program roles and message direction

**CADhoc is a library and a peer node embedded in several programs, not a single
central server.** A sender is the process issuing the request; the receiver is
the process hosting the addressed CADhoc endpoint. A process can be both. Replies
return to the requesting process over the same request/reply connection.

| Program | Role in the CADhoc protocol |
| --- | --- |
| Kikakuka Workspace Manager and standalone tools such as Differ / Fabrication Planner | Can host a CADhoc node and send requests. Their node can execute editor requests for other callers. |
| `python -m cadhoc` | Standalone CADhoc node using the same editor backend; can receive requests and query other nodes while executing them. |
| FreeCAD GUI with FreekiCAD | Hosts a CADhoc node, sends integration requests, and receives FreeCAD document commands. It can also execute generic editor requests for other callers. |
| FreeCADCmd with FreekiCAD | Hosts a CADhoc node and can send/execute editor requests, but does not offer GUI document commands. |
| KiCad add-on's Open in FreeCAD Python process | Client-only CADhoc node. Runs `cadhoc.freecad_open.open_board()` and sends requests directly to FreeCAD nodes, but does not listen on an endpoint or receive peer requests. |
| KiCad PCB Editor (`pcbnew`) | Server for **KiCad's own IPC API**, not a CADhoc mesh receiver. CADhoc nodes and FreekiCAD talk to its KiCad socket using kipy. |

An **executor node** below means a selected node hosted by Kikakuka, FreekiCAD,
or standalone `python -m cadhoc`. `mesh.request()` tries discovered nodes in
ascending PID/node-ID order; for a PCB request it requires `kicad_api: true`.
The receiver may be the sender's own embedded node. It need not be the
Workspace Manager, the process owning the requested document, or a separate
background daemon.

| `mesh_action` | Sender program / role | Receiver program / role |
| --- | --- | --- |
| `hello` | Any discovering CADhoc client or node, including the KiCad add-on action | Each candidate CADhoc node: Kikakuka, FreekiCAD, or standalone CADhoc |
| `snapshot` | A node refreshing its mappings, including Workspace Manager refresh or an executor before handling a request | Other discovered CADhoc nodes |
| `event` | The node publishing a file-to-process mapping change | Other discovered CADhoc nodes |
| `dispatch` | Kikakuka file-opening tools, or FreekiCAD requesting a KiCad operation | Selected executor node; it resolves/opens the editor through `instance_backend.py` |
| `result` | The client that received `accepted` | The **same node** that accepted that request, whether an executor or a FreeCAD GUI node |
| `freecad-list-documents` | Kikakuka's instance refresh, or an executor searching for an open FreeCAD file | FreeCAD GUI process with FreekiCAD document support |
| `freecad-activate-document` | Executor handling a FreeCAD `open-file` request | The FreeCAD GUI node containing the matching document |
| `freecad-open-document` | Executor handling a FreeCAD `open-file` request | Selected existing FreeCAD GUI node |
| `freecad-open-pcb` | KiCad add-on's Open in FreeCAD Python process | FreeCAD GUI nodes during probing, then the selected target node for updating/creating the PCB |
| `freecad-bind-source` | Executor that launched FreeCAD to open a file | The newly launched FreeCAD GUI node |

These are the current callers and intended roles, not sender-specific access
rules. Nodes authenticate the shared token and check their capabilities;
commands are not restricted by the sender application's name.

### Common action flows

**Open in FreeCAD** starts in the KiCad add-on process:

```text
KiCad add-on action process
  -> FreeCAD CADhoc nodes: hello, freecad-open-pcb(probe=true), result
  -> selected FreeCAD CADhoc node: freecad-open-pcb(document=..., defer=true), accepted
     (or create=true when no document matches)
  -> KiCad add-on action process exits
  -> FreekiCAD GUI callback: compare inputs, reload if needed, activate document
     -> invoking pcbnew's KiCad IPC socket: read live PCB contents
```

The add-on process coordinates instance selection and foregrounding through
`cadhoc.freecad_open`. It already knows the invoking KiCad socket, so it does not
send `dispatch(action=reload)` to resolve one. The final `result` is returned
after FreekiCAD finishes a probe. The actual update is accepted and deferred so
the KiCad API action can exit before FreeCAD connects back to pcbnew. Later
import errors are shown in FreeCAD's Report View. CADhoc probes never address
pcbnew.

**Reload KiCad PCB** starts in the FreeCAD process containing the PCB object:

```text
FreekiCAD -> executor CADhoc node: dispatch(request.action=reload), result
executor -> pcbnew's KiCad IPC socket: verify matching PCB; launch if needed
executor -> FreekiCAD: result containing the verified KiCad socket
FreekiCAD GUI callback -> pcbnew's KiCad IPC socket: read PCB and force rebuild
```

Here the executor may itself be hosted by that same FreeCAD process, another
FreeCAD process, Kikakuka, or standalone CADhoc. It returns the socket to the
original requester; it does not choose another FreeCAD document or rebuild
geometry in the executor's process.

**Open in KiCad** also starts in the FreeCAD process containing the PCB object:

```text
FreekiCAD -> executor CADhoc node: dispatch(request.action=open-file), result
executor -> pcbnew's KiCad IPC socket: verify matching PCB; launch if needed
executor -> operating system: foreground the selected KiCad process
executor -> FreekiCAD: completion result; no FreeCAD geometry update
```

Open in KiCad does not set `ensure_fresh`, so it does not revert unsaved KiCad
contents. It uses the same editor action as Kikakuka's ordinary file opener.

### Mesh commands

Fields below are in addition to `token` and `mesh_action`. Fields are required
unless marked optional. Success replies have `status: "ok"` unless the table
specifies an asynchronous acknowledgement. FreeCAD commands require the
corresponding provider on the receiving GUI node; an unavailable provider
returns `status: "error"` with a `message`.

| `mesh_action` | Request fields | Reply and behavior |
| --- | --- | --- |
| `hello` | None | `version`, `pid`, `started_ms`, node `id`, `kicad_api`, `freecad_documents`, `freecad_pcb`. The last field is `2` for the current PCB selection/open capability, otherwise `0`. |
| `snapshot` | None | `mappings`: canonical file path to complete mapping event, including tombstones. Reads local state without refreshing peers. |
| `event` | `event` object | Applies a newer mapping event; ignores an equal or older stamp. Returns `ok` either way. |
| `dispatch` | Nonempty unique `id`, `request` object | Queues an editor request on a worker thread; returns `accepted` and `id`. Poll `result` on this node for completion. |
| `result` | `id` | Returns the cached final reply, `pending`, or `unknown` for an unrecognized/expired ID. |
| `freecad-list-documents` | None | `pid`, `documents`: list of normalized absolute document/source paths from the GUI thread, not document metadata objects. |
| `freecad-activate-document` | Absolute `filepath` | `pid`, `found` boolean. Selects the matching document and MDI tab; the caller handles process foregrounding. Does not open a missing file. |
| `freecad-open-document` | Absolute `filepath`, nonempty unique `id` | Queues GUI opening/importing; returns `accepted` and `id`. Repeated requests for the same in-flight path receive the existing ID. Final reply is `ok` with `pid`, or `error`. |
| `freecad-open-pcb` | Absolute `.kicad_pcb` `filepath`, nonempty KiCad `socket`, nonempty unique `id`; optional fields below | Queues PCB probing or opening; returns `accepted` and `id`. Final reply includes `pid` and `found`; probes also return `document`. |
| `freecad-bind-source` | Absolute `filepath` | `pid`, `bound` boolean. Associates a launch-imported document without a native filename with its source path. Returns false if a unique suitable unbound document cannot be identified. Does not import the file itself. |

A mapping `event` contains `filepath`, `pid` (or `null` for a tombstone), and
`stamp: [time_ns, publishing_node_pid, publishing_node_id]`. A live PCB mapping
may also contain `socket`. Stamps are compared lexicographically. Use
`publish()` to canonicalize paths and broadcast events.

`freecad-open-document` supports `.FCStd`, `.step`, `.stp`, `.stl`, and
`.kkkk_asm`.
It activates an already-open document when possible. Its asynchronous result
can be polled, but the current general file opener proceeds after acceptance
instead of waiting for a potentially long import to finish.

Optional `freecad-open-pcb` fields:

| Field | Default | Meaning |
| --- | --- | --- |
| `create` | `false` | Allow a new document if no linked PCB is found. |
| `active_only` | `false` | Search only this instance's active document; prevents creation even when `create` is true. |
| `probe` | `false` | Only search; do not reload, create, activate tabs, or foreground the window. Returns the matching internal document name in `document`, or `null`. |
| `document` | Omitted / `null` | Restrict the request to this internal FreeCAD document name. This overrides candidate selection by `active_only`; a non-probe request fails if the document or PCB link disappeared. |
| `defer` | `false` | For a non-probe job, wait for `caller_pid` to exit after acceptance before FreeCAD connects to KiCad's API socket. The caller may return after `accepted` instead of polling the result. |
| `caller_pid` | Omitted / `null` | Positive PID of the invoking action process. Used with `defer`; the node waits up to 30 seconds for this process to exit. |

The `socket` identifies the invoking KiCad editor. The normal caller supplies
its endpoint without the `ipc://` prefix. `freecad-open-pcb` addresses **one**
FreeCAD node; cross-instance active-document priority and process foregrounding
are coordinated by `freecad_open.open_board()`. A non-probe request updates all
matching PCB objects in the selected document, then activates it. FreekiCAD
may skip unchanged geometry using its in-memory import fingerprint. A probe
still uses the asynchronous acknowledgement/result exchange.

### Dispatched editor actions

The nested `request` contains `action` and, except for `list` and `log`, an
existing `filepath`. The worker normalizes the path before handling it. Optional
`object` (default empty string) and `component` label fields route the eventual
reply back to FreekiCAD; they do not identify a CADhoc node or FreeCAD document.

| `request.action` | Additional fields | Executor behavior and final reply |
| --- | --- | --- |
| `open-file` | Optional `ensure_fresh` (default `false`, PCB only) | Reuse or open an editor and foreground it. Returns `filepath`, `pid`, and `socket` when available. For an already-open PCB, `ensure_fresh: true` reverts it from disk through KiCad IPC before focusing; ordinary opens do not revert. |
| `reload` | Optional `object` | Find or open KiCad for this PCB and return its verified socket. FreekiCAD subsequently reads the PCB and rebuilds geometry. |
| `open-sketch` | Optional `object` | Resolve the PCB socket; FreekiCAD establishes the outline-edit connection after receiving it. |
| `move-component` | Optional `object`, `component` | Resolve the PCB socket; FreekiCAD performs the component change afterward. |
| `update-coupler` | Optional `object`, `component` | Resolve the PCB socket; FreekiCAD performs the coupler update afterward. |
| `monitor-couplers` | Optional `object` | Resolve an already-open PCB socket. Never launches KiCad; returns an error if the board is not open. |
| `list` | None | `instances`: refreshed canonical file-path-to-PID map, excluding tombstones. This differs from the mesh `snapshot` reply. |
| `log` | None | Compatibility no-op; returns `ok`. Does not write a log message. |

`open-file` accepts `.kicad_pcb`, `.kicad_sch`, `.kicad_pro`, `.fcstd`, `.step`,
`.stp`, `.stl`, and `.kkkk_asm`. The five integration actions from `reload` through
`monitor-couplers` are for `.kicad_pcb` files and normally do not foreground
KiCad; see [File-opening and foreground policy](#file-opening-and-foreground-policy) for the lock-prompt
exception. Their successful replies contain `action`, `object`, `socket`,
`pid`, and `component` when supplied, in addition to `status: "ok"`.
**An integration action's CADhoc success means the socket is ready, not that the
subsequent FreekiCAD import or edit has finished.**

### Asynchronous replies and retries

| `status` | Meaning |
| --- | --- |
| `accepted` | Work was queued or its request ID was already known. Poll the returned `id` on the same node. |
| `pending` | Work has not completed. |
| `ok` | The addressed operation completed; inspect fields such as `found` or `bound` where applicable. |
| `error` | Operation failed; `message` explains why. |
| `unknown` | This node has no cached result for the ID. It is not proof that work never ran. |

Example editor request and result polling (the token is illustrative):

```json
{"token":"<shared-token>","mesh_action":"dispatch","id":"<unique-request-id>","request":{"action":"reload","filepath":"/boards/demo.kicad_pcb","object":"Demo"}}
{"status":"accepted","id":"<unique-request-id>"}
{"token":"<shared-token>","mesh_action":"result","id":"<unique-request-id>"}
{"status":"ok","action":"reload","object":"Demo","socket":"/tmp/kicad/api.sock","pid":1234}
```

The standard mesh request client polls every 0.2 seconds and waits up to 120
seconds by default. Open in FreeCAD waits up to 300 seconds for each read-only
document probe, but returns after the selected update/create job is accepted.
A timeout does not cancel queued or running work. Completed results are
eligible for cleanup after 300 seconds when a subsequent `dispatch` or
`freecad-open-pcb` request performs cleanup; they are not durable records.
IDs deduplicate work within one node's retained results, not across nodes.
The general request client can try another node after communication failure;
per-file locks and live editor re-probing protect editor reuse. Open in FreeCAD
never switches to another node after its PCB request has been accepted, because
that import may still be running.

## Finding a pcbnew PID

KiCad's PCB IPC sockets are separate from the CADhoc mesh sockets.
On Unix, the backend scans `/tmp/kicad` for `api-<PID>.sock` and `api.sock`;
on Windows, it also enumerates the corresponding named pipes. It reads the
ordinary process list on demand to find KiCad editor PIDs and creation times:

- For `api-<PID>.sock`, the PID is in the socket name, but the backend still
  requires a matching live KiCad editor process before using it.
- `api.sock` contains no PID. On Unix, the backend first checks the Unix-domain
  sockets of same-user KiCad processes that have not already been matched to a
  PID-specific socket. Because a newly created socket may take a moment to
  appear in the process socket table, this ownership check makes one initial
  attempt plus four retries at 0.1-second intervals before falling back. On
  Windows, if the matching generic named pipe exists,
  the backend opens it and asks `GetNamedPipeServerProcessId()` for its server
  PID. The returned PID must still match a live, same-user KiCad process and its
  recorded creation time. If exact socket ownership is unavailable, it assigns
  `api.sock` to the oldest unmatched KiCad editor process as a best-effort
  fallback. This fallback is a process-order heuristic, not a PID supplied by
  KiCad IPC.
- The Workspace Manager keeps its PID-to-socket lookup in reactive state. Its
  own PCB-open worker refreshes immediately, then retries up to 12 times at
  0.5-second intervals until the matching socket appears. PCB operations
  executed by any CADhoc node publish the resolved socket directly, so a KiCad
  instance opened for FreekiCAD updates a running Instance Manager row without
  waiting for this scan or a manual refresh.

### Windows named-pipe workaround

[KiCad issue #23994](https://gitlab.com/kicad/code/kicad/-/work_items/23994)
causes affected Windows builds to expose only one IPC endpoint when several
instances run on Windows. NNG implements an `ipc://` endpoint as a Windows
named pipe, but the affected KiCad code checks only the ordinary filesystem
for `%TEMP%\kicad\api.sock`. Because those namespaces are independent, a
second instance does not see the first instance's named pipe and tries to bind
the same name instead of falling back to `api-<PID>.sock`.

CADhoc ensures that an ordinary sentinel file exists at
`%TEMP%\kicad\api.sock` before launching KiCad. On affected builds,
the filesystem check sees the sentinel and every newly launched instance
selects `%TEMP%\kicad\api-<PID>.sock`; NNG then creates a named pipe with that
path without conflicting with the sentinel in the separate filesystem
namespace. The sentinel must remain in place while the workaround is active.
If the path already exists but is not a regular file, it must not be replaced.
CADhoc leaves the sentinel on disk permanently and recreates it the
next time Kikakuka or FreekiCAD starts if the temporary directory was cleaned.
Close all running KiCad applications before activating the workaround for the
first time so every instance starts after the sentinel exists.

Discovery must not treat the sentinel itself as a live IPC endpoint. It probes
`api.sock` to cover an instance started before the sentinel was created, and
enumerates `\\.\pipe` for the actual named pipes. When a generic pipe is found,
the standard-library `ctypes` module calls Kernel32's
`GetNamedPipeServerProcessId()`; no additional Python dependency is required.
A candidate is retained only when its PID belongs to a live KiCad editor and
its IPC API responds with a usable document.
The current upstream HEAD remains affected. A build with the named-pipe
collision fix applied uses `WaitNamedPipeW()` instead of the filesystem check;
such a build ignores the sentinel and handles the fallback itself, so the file
is harmless but unnecessary.

For each candidate socket, the backend asks KiCad's API for the open board
name (and project path if the name is relative). Only a path matching the
requested PCB is accepted for reuse, focus, or PCB IPC operations; a saved
file-to-PID mapping alone is not proof that the board remains open. The
Instance Manager tab uses the same socket-and-board probe to rebuild its PCB
rows on manual refresh. KiCad rows display the basename of the matched IPC
socket, such as `api.sock` or `api-1234.sock`; the full socket path remains
internal.

When launching a new KiCad editor, the current launcher also compares editor
process lists before and after launch to infer the new PID. For a PCB, that
inference is not the final answer: the backend waits up to 30 seconds for the
requested board to appear through KiCad IPC and uses the PID assigned to that
socket. Process-list enumeration is still needed to validate PID-specific
sockets and to provide the fallback PID for `api.sock`; it is not a background
monitor.

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
`pcbnew`. Matching normalizes the basename and extension, then requires a known
GUI executable name, so helper processes such as `kicad-api` and `kicad-cli`
are excluded. It does not verify that the chosen process opened the requested
schematic. Schematic/project launches then retain the per-program launch lock
for another three seconds to let process startup settle.

The Instance Manager tab also enumerates editor processes on refresh. If no
file-to-PID mapping is known, it may infer a `.kicad_sch` or `.kicad_pro`
path from the process command line (resolving relative paths against its
working directory). If neither source supplies a path, the row shows an
unknown file rather than claiming a verified document.

## FreeCAD document APIs and PID

The KiCad plugin's **Open in FreeCAD** action uses `cadhoc.freecad_open` and the
authenticated `freecad-open-pcb` operation. It first searches active documents
across GUI nodes, then searches the remaining documents for matching normalized
PCB links. Read-only probes return the matching document's internal name without
changing tabs or reloading geometry. Only after selecting the target does CADhoc
focus its process and request an update of that specific document from the
invoking KiCad socket. FreekiCAD restores a minimized window before updating
and may skip rebuilding when its session-local fingerprint of the live
import inputs matches the last successful import. CADhoc does not store or compare
these fingerprints. Only when no match exists is a document created.
The `freecad_pcb: 2` capability
distinguishes this selection protocol from the earlier combined search/update
operation. Requests acknowledge quickly
and report completion through the mesh result API so import failures reach the
plugin. A separate nonblocking per-PCB lock coalesces repeated clicks, and the
FreeCAD launch lock prevents simultaneous launches. This operation does not
publish PCB-to-FreeCAD PID mappings: the PCB path still belongs to its KiCad
editor in the general instance map.

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

Successful KiCad and FreeCAD operations broadcast mapping events directly to
every currently discovered node. Each event contains a canonical `filepath`,
an editor `pid`, and a total-order `stamp`; successful KiCad PCB replies also
add the full `socket` path. A node snapshot retains the complete event, while
the public `snapshot()` view remains a file-to-PID map. Workspace callbacks
receive `(filepath, pid, socket)` and write the optional socket directly into
the reactive PID-to-socket state. The `socket` field is additive JSON data, so
older peers that do not use it can still process the path/PID event.
Replacing a PID or applying a tombstone removes an unreferenced old socket.

Deletions use timestamped tombstones with `pid: null` and no socket, so an old
snapshot cannot revive a removed mapping or socket. A joining node and an
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

FreekiCAD's [`cadhoc_client.py`](../FreekiCAD/freecad/FreekiCAD/cadhoc_client.py)
provides asynchronous and synchronous KiCad requests for linked PCB objects.
The Workspace Manager's KiCad and FreeCAD open actions and `pcb_open.py` use
the same route.
The old `/tmp/kikakuka.sock` / Windows port 19780 daemon and its tests have
been removed. KiCad's own
`api.sock` and `api-<PID>.sock` are unrelated and remain in use.

## Platform and permissions

### FreeCAD launch environment isolation

FreeCAD launches on macOS, Windows, and Linux use an isolated child environment:
the launcher removes inherited Python/Qt/QML search paths, dynamic-library
overrides, and `KICAD_API_TOKEN` / `KICAD_API_SOCKET`. It disables Python user
site packages while preserving ordinary user settings and KiCad model-path
variables. Linux launches FreeCAD explicitly for both file opens and empty
documents; macOS applies the environment to `open -a FreeCAD`. This does not
modify the parent process or repair an already-running contaminated FreeCAD.

`freecad_process_environment()` applies the following policy:

| Variable group | Child-process policy |
| --- | --- |
| `PYTHONHOME`, `PYTHONPATH`, `PYTHONUSERBASE`, `PYTHONSTARTUP`, `PYTHONEXECUTABLE`, `VIRTUAL_ENV`, `VIRTUAL_ENV_PROMPT`, `__PYVENV_LAUNCHER__` | Remove inherited values and KiCad's per-plugin virtual-environment markers so FreeCAD selects its own Python runtime. FreeCAD's launcher may then set its own `PYTHONHOME`. |
| `QT_PLUGIN_PATH`, `QT_QPA_PLATFORM_PLUGIN_PATH`, `QT_QPA_FONTDIR`, `QML_IMPORT_PATH`, `QML2_IMPORT_PATH` | Remove paths pointing to the caller's Qt plugins, fonts, or QML modules. |
| `DYLD_LIBRARY_PATH`, `DYLD_FRAMEWORK_PATH`, `DYLD_FALLBACK_LIBRARY_PATH`, `DYLD_FALLBACK_FRAMEWORK_PATH`, `DYLD_INSERT_LIBRARIES`, `LD_LIBRARY_PATH`, `LD_PRELOAD` | Remove inherited library search/injection overrides. |
| `KICAD_API_TOKEN`, `KICAD_API_SOCKET` | Remove credentials and endpoint selection belonging to the invoking KiCad instance. |
| `PYTHONNOUSERSITE` | Set to `1`; FreeCAD still adds its own AdditionalPythonPackages directory. |
| `PATH` | Prepend the FreeCAD executable directory for direct launches on Windows/Linux; preserve it for macOS's `open` launcher. |
| Other variables | Preserve user settings and model paths such as `KICAD10_3DMODEL_DIR`. |

This applies when CADhoc starts a new FreeCAD process, including Open in FreeCAD
and general FreeCAD file opening. Reusing a running FreeCAD does not relaunch it
or change its environment. The cleanup currently targets FreeCAD launches;
it is not a global environment change or a claim that KiCad's file-association
launchers apply the same policy.

On Linux, the launcher prefers a transient `systemd-run --user` service so the
FreeCAD/AppImage process is owned by the desktop user manager instead of the
short-lived KiCad action process. The service receives only the sanitized
desktop/session, locale, and graphics variables needed to open a GUI. If no
user manager is available or submission fails, the launcher falls back to a
new process session with standard input disconnected. The transient service
explicitly disables `PrivateTmp` so FreeCAD and KiCad see the same instance
mesh and KiCad API sockets under `/tmp`. Both paths use the user's home
directory as the working directory. Standard output and error are redirected
to `/tmp/kikakuka-<UID>/freecad-startup.log`, which is truncated for each
launch, retaining startup diagnostics without attaching a console. PCB-open
failures are also written to FreeCAD's Report View and this redirected output.

KiCad API tokens are separate from the CADhoc mesh token. CADhoc endpoint probes and
FreekiCAD connections to a selected KiCad socket explicitly initialize kipy
with `kicad_token=""`, allowing the response to supply that instance's token.
They must not use a `KICAD_API_TOKEN` inherited from another instance. The KiCad
add-on's initial connection to its invoking editor still uses the environment
provided by KiCad. FreekiCAD does not print API credentials in path-variable
diagnostics.

Open in KiCad uses `dispatch(action=open-file)` and Open in FreeCAD uses
`freecad-open-pcb`; neither action writes a new token into an existing process's
environment or overwrites an existing `mesh-token` file. A KiCad API client
learning a token from a response stores it in that client, not in the mesh
credential file. Therefore, an inherited `KICAD_API_TOKEN` can be stale even
when the process's own live KiCad endpoint is working.

### Process launching and access

macOS starts a new FreeCAD with `open -a FreeCAD -n -W --args <file>` and
uses AppleScript for best-effort focus; other new editors use `open -n`.
Windows starts FreeCAD directly and uses file associations for KiCad, with
Win32 foreground APIs. Linux starts FreeCAD directly and launches `pcbnew`,
`eeschema`, or `kicad` according to the KiCad file suffix. X11 and XWayland
focus uses EWMH through the system `libX11`; native Wayland has no generic
PID-based focus operation.
File-to-PID discovery uses ordinary process enumeration and KiCad's IPC. On
all platforms, process enumeration first reads only the PID and OS owner field;
name, creation time, command line, current directory, and socket details are
read only after the process is confirmed to belong to the current user. On
Unix it may call `psutil.Process.net_connections(kind="unix")` for unmatched
same-user KiCad processes. Stored or WinAPI-returned PIDs are owner-checked
again before they are inspected, reused, or focused. It does not use
system-wide `psutil.net_connections()`,
`Process.open_files()`, `Process.environ()`, or privileged process/socket
inspection. An inaccessible process is treated as unavailable, never as a
reason to request elevation.

The private runtime directory and request token provide best-effort local-user
isolation, not an authentication boundary against malicious processes running
as the same desktop user (which can read the token). Keep the per-user
temporary directory private.

## Dependencies and limitations

CADhoc requires `psutil>=7.2.2` on macOS, Linux, and Windows. FreeCAD
Addon Manager may not install it automatically on builds whose allowed-package
list excludes it.

The current backend can verify a PCB's open document through KiCad IPC. KiCad
schematics do not have an equivalent verified-document probe here, so a live
schematic PID association is best-effort. The 120-second request limit prevents
an indefinitely blocked GUI workflow; a modal dialog or unresponsive KiCad
may return a timeout and require a retry. No background heartbeat detects a
document changing in an otherwise-live editor; the next demand-triggered
operation rechecks the PCB mapping.
