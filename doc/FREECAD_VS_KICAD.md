# FreeCAD vs KiCad document lifecycle

This compares the APIs available to Kikakuka/FreekiCAD and the behavior we
currently implement. It is not a claim that either application's other APIs or
future releases lack a feature. KiCad details below refer to the project's
`kicad-python>=0.8,<0.9` dependency and the GUI editors, not headless KiCad.

| Operation | FreeCAD GUI / FreekiCAD | KiCad GUI / current Kikakuka integration |
| --- | --- | --- |
| Document model | One FreeCAD process can own several documents and MDI tabs. One PID can therefore map to several file paths. Multiple FreeCAD processes can also coexist, each with its own mesh node. | PCB Editor and Schematic Editor are separate editor processes. Kikakuka treats each editor PID as one active target file; several editor instances can be open. A schematic's hierarchy of sheets is not a set of independent editor tabs. |
| List open documents | `FreeCAD.listDocuments()` enumerates documents in that process. FreekiCAD exposes `freecad-list-documents` through its mesh node and runs the scan on FreeCAD's GUI thread. | `KiCad.get_open_documents(doc_type)` exists in `kicad-python 0.8` and returns type-filtered document specifiers. Kikakuka does **not** currently use it for a full editor inventory: it discovers KiCad API endpoints and uses `get_board()` to verify the PCB in each responding editor. |
| Identify a file | A saved document has `Document.FileName`. STEP and `.kkkk_asm` imports can have an empty `FileName`; FreekiCAD records their source path when it handles the open, including a file passed to a newly launched FreeCAD. | For PCBs, Kikakuka derives the canonical path from the API-reported board name and project path. Schematic/project paths have no equivalent verified-document probe in this integration; their PID mapping is best-effort. |
| Activate an already-open file | FreekiCAD finds the matching document, selects its `QMdiSubWindow`, calls `FreeCAD.setActiveDocument(name)`, then asks the OS to foreground that PID. Selecting the application alone would not select the right tab. | There is no `set_active_document` operation in the pinned `KiCad` Python client. Kikakuka finds the matching PCB editor instance and foregrounds its PID; it does not switch a document inside that process. |
| Open another file | `freecad-open-document` asks a responding GUI node to open `.FCStd`, STEP, or `.kkkk_asm` in the **same process**. Only if no GUI FreeCAD node is available does Kikakuka launch a process; it then waits for the opened document to be identified. | The pinned client does not expose a GUI `open_document` operation. Kikakuka launches the requested file through the OS/editor command, then waits for the PCB API to report the target board. Schematic/project launches cannot be verified the same way. |
| Observe lifecycle | FreekiCAD's document observer receives create, activate, change, save, and delete callbacks. It broadcasts path-to-PID changes; an on-demand scan repairs missed events. FreeCADCmd hosts a mesh node but does not publish GUI document state. | Kikakuka currently has no KiCad document-observer feed. It discovers processes/sockets and re-probes the PCB via IPC on demand. A stale schematic/project association can survive until process exit or another update. |
| Close or rename | A FreeCAD close removes that document's path while leaving other paths for the same PID intact. Saving under a new name replaces its mapping. | For PCBs, the next IPC probe detects that the editor no longer reports the old board. Without a corresponding schematic probe, Kikakuka cannot reliably detect a live schematic editor changing files. |

## Practical lifecycle

For FreeCAD, the source of truth is the responding GUI node's live document
list. On open, Kikakuka first asks every GUI node to activate an existing
matching document. If none matches, it asks a node to create the document in
the same process. The document observer publishes changes; Monitor's manual
**Refresh** re-queries each GUI node and reconciles all paths belonging to its
PID. Imported files require FreekiCAD's source-path association because an
unsaved FreeCAD document may not have a native filename. An unrelated unsaved
document with no known source cannot be assigned a path; Monitor shows
`unknown` when that process has no other identified file.

For KiCad PCBs, the source of truth is the board path reported over that
editor's IPC endpoint, not a cached `file path -> PID` entry. The endpoint is
associated with an editor PID, and a request rechecks the board before reusing
it. For schematics and projects, the current integration relies on the
best-effort PID association because it has not implemented an equivalent
live-path check. Monitor also lists running editor processes whose file path
cannot be identified, as `unknown`.

The [Instance Manager](INSTANCE_MANAGER.md) documents mesh discovery,
election, state propagation, and platform-specific transport.

## API references

- [FreeCAD application document APIs](https://freecad.github.io/SourceDoc/da/dbf/classApp_1_1Application.html) and [Python document-observer callbacks](https://github.com/FreeCAD/FreeCAD/blob/main/src/App/DocumentObserverPython.h)
- [KiCad IPC API overview](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/) and [kicad-python `KiCad` client](https://docs.kicad.org/kicad-python/kicad.html)
