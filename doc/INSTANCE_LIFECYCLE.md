# Instance lifecycle

This compares the lifecycle operations available to an external integration:
enumerating open file paths, opening a requested path, and activating the
corresponding document or editor. "Get current status" means a programmatic
report, not information visible only in a GUI.

## Document model

A PID is not a document key. FreeCAD's MDI permits multiple documents per
process. In KiCad 10 and 11, Project Manager-launched schematic and PCB editor
frames share a process and KIWAY, while standalone `eeschema` and `pcbnew`
processes do not share that KIWAY.

## Capabilities

| Scope | Get current status | Open a file | Gain focus |
| --- | --- | --- | --- |
| FreeCAD | `FreeCAD.listDocuments()` returns the process's documents. A saved document has a `FileName`; imports and unsaved documents may not. | `FreeCAD.openDocument()` opens `.FCStd` in the current process, and importers can add documents there. A file can also be opened in a newly launched FreeCAD process; multiple instances may run concurrently. | Use `FreeCAD.setActiveDocument()` to change the active tab. |
| KiCad 10 | `pcbnew` IPC `GetOpenDocuments` returns the board filename and project directory. `eeschema` returns the schematic filename but omits the project path, so it cannot uniquely verify a full path. The Project Manager has no `GetOpenDocuments` handler of its own to report its open project or editor files. [Windows known issue](#kicad-on-windows-known-issue). | The Project Manager opens `.kicad_pro`, but cannot be asked to open a specified `.kicad_pcb` or `.kicad_sch`. To open a specified board or schematic path, launch standalone `pcbnew` or `eeschema`; these processes do not share the Project Manager's KIWAY. | Can focus `eeschema` and `pcbnew` windows by PID, but not the Project Manager. The generic `api.sock` does not identify its owner PID; `api-<PID>.sock` does. |
| KiCad 11 (development) | `pcbnew` `GetOpenDocuments` returns the board filename and project directory. `eeschema` returns project information and the current sheet path, but does not include the schematic filename. The Project Manager has no `GetOpenDocuments` handler of its own to report its open project or editor files. | The Project Manager opens `.kicad_pro` and can open the current project's schematic and PCB in editor windows within the same process. `KiCad.open_document()` is headless-only; opening an arbitrary `.kicad_pcb` or `.kicad_sch` path from an external request still requires a standalone `pcbnew` or `eeschema` process, which does not share the Project Manager's KIWAY. | Can focus `eeschema` and `pcbnew` windows by PID, but not the Project Manager. The generic `api.sock` does not identify its owner PID; `api-<PID>.sock` does. |
| Kikakuka's Instance Manager | Discovers same-user nodes through `/tmp/kikakuka-<UID>/<PID>-<process-start-ms>.sock` on POSIX systems or `\\.\pipe\kikakuka-<SID-hash>-<PID>-<process-start-ms>` on Windows. Replicated mapping events carry a canonical path, PID, timestamp, and an optional KiCad PCB socket. PCB paths are verified through KiCad IPC; GUI FreeCAD nodes report all open documents. | Elects the lowest-PID capable node, serializes each canonical path and editor launch, reuses a verified open PCB or live FreeCAD document, and otherwise opens it through the platform association or a responding FreeCAD GUI node. | Owner-checks the mapped PID before using platform focus APIs. GUI FreeCAD also activates the matching document and MDI tab. |

## Identity and permissions

Instance Manager never treats a bare PID as sufficient identity. Mesh endpoint
names include PID and process start time, and `hello` must return the same
identity before a node is eligible. Process enumeration reads only PID and the
OS owner first; name, creation time, command line, working directory, and
socket data are requested only for current-user processes. Stored and
WinAPI-returned PIDs are owner-checked again before inspection, reuse, or
focus. Inaccessible processes are skipped without requesting elevation.

For generic KiCad `api.sock` endpoints, macOS and Linux inspect
`Process(pid).net_connections(kind="unix")` only on same-user KiCad editor
candidates, retrying briefly while the socket table catches up. Windows opens
the named pipe and uses `GetNamedPipeServerProcessId()` from Kernel32, then
validates that PID as a same-user KiCad editor. No additional Windows Python
dependency or system-wide privileged connection scan is required.

### KiCad on Windows: known issue

[KiCad issue #23994](https://gitlab.com/kicad/code/kicad/-/work_items/23994)
reports that KiCad 10.0.1 on Windows 11 exposed only one named IPC pipe when
two KiCad instances were running. [KiCad's IPC documentation](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/index.html#connecting-to-kicad)
says additional instances should use PID-suffixed pipe names. If only one pipe is available,
an external integration cannot discover every instance's open PCB through IPC
or reliably associate each IPC endpoint with its editor PID. The current
upstream HEAD remains affected. On affected
builds, Instance Manager creates an ordinary `%TEMP%\kicad\api.sock` sentinel
before launching KiCad. The filesystem check then selects PID-specific names;
NNG creates the corresponding named pipes in a separate namespace. Close all
running KiCad applications before activating the workaround for the first
time. The sentinel then remains on disk for subsequent KiCad launches.

## API references

- [FreeCAD application document APIs](https://freecad.github.io/SourceDoc/da/dbf/classApp_1_1Application.html) and [Python document-observer callbacks](https://github.com/FreeCAD/FreeCAD/blob/main/src/App/DocumentObserverPython.h)
- [KiCad 10 project workflow](https://docs.kicad.org/10.0/en/getting_started_in_kicad/getting_started_in_kicad.html), [KIWAY process model](https://docs.kicad.org/doxygen/kiway_8h.html), [KiCad 10 schematic](https://gitlab.com/kicad/code/kicad/-/raw/10.0/eeschema/api/api_handler_sch.cpp) and [PCB](https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/api/api_handler_pcb.cpp) IPC handlers, [KiCad 11 development schematic](https://gitlab.com/kicad/code/kicad/-/raw/master/eeschema/api/api_handler_sch.cpp) and [PCB](https://gitlab.com/kicad/code/kicad/-/raw/master/pcbnew/api/api_handler_pcb.cpp) handlers, and [kicad-python `KiCad` client](https://docs.kicad.org/kicad-python/kicad.html)
- [KiCad 10](https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/api/api_handler_common.cpp) and [KiCad 11 development](https://gitlab.com/kicad/code/kicad/-/raw/master/common/api/api_handler_common.cpp) common IPC handlers (neither registers `GetOpenDocuments`)
