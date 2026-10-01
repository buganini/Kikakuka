# Linux support and limitations

Kikakuka's file processing, local Instance Manager mesh, process discovery,
and KiCad IPC support work on Linux. Linux desktop integration is less
complete than its macOS and Windows equivalents, especially around launching
and activating GUI applications.

This document distinguishes an X11 desktop session from a Wayland session
running X11 applications through XWayland. Setting an application's GTK
backend to X11 does not turn a Wayland desktop session into an X11 session.

## Current capabilities

| Capability | Linux status |
| --- | --- |
| Instance Manager mesh | Supported through private per-user Unix sockets. |
| Same-user KiCad and FreeCAD process discovery | Supported. |
| KiCad PCB discovery and refresh | Supported through KiCad IPC. Generic `api.sock` ownership is resolved with same-user process socket inspection. |
| Open KiCad files | Supported through `xdg-open` and the system file association. |
| Open FreeCAD files | Supported by launching `FreeCAD` or `freecad` directly with an isolated child environment. |
| Activate a document inside a running FreeCAD process | Supported by the FreekiCAD node. |
| Bring an external window to the front | Supported for X11 and XWayland windows through EWMH `_NET_ACTIVE_WINDOW`. Native Wayland windows are not supported. |
| Open a file's directory | Supported through `xdg-open`. |

## Missing or limited desktop integration

### External window activation

Kikakuka implements Linux window activation by PID for X11 and XWayland
windows. It finds the process's top-level X11 window through `_NET_WM_PID`
and asks the window manager to activate it with the EWMH
`_NET_ACTIVE_WINDOW` message. A window manager or Wayland compositor may
still reject focus stealing.

Consequences include:

- X11 and XWayland activation is a request; the window manager retains final
  control over whether the window receives focus.
- A native Wayland window cannot be found in the X11 client list. Kikakuka
  reports that external activation is unavailable instead of silently
  pretending that it raised the window. The Workspace GUI displays a dialog;
  a headless `python -m im` node returns the message in its IPC reply and
  writes it to the CLI log without importing Qt.
- Kikakuka cannot reliably make a possible KiCad **Open Anyway** dialog
  visible on Linux.
- Background integration requests cannot guarantee that a newly opened KiCad
  window remains in the background. Final activation is controlled by the
  desktop and `xdg-open` implementation.

On native Wayland, an unrelated process generally cannot enumerate or
activate another application's windows. Activation normally requires
compositor support and a user-interaction activation token, or cooperation
from the target process. There is no single cross-desktop replacement for
the macOS and Windows implementations.

FreeCAD is a partial exception: when a FreekiCAD node is already running in
the target process, it can activate the requested document and its MDI tab
from inside FreeCAD. Whether the compositor raises the top-level FreeCAD
window remains desktop-dependent.

### KiCad launch control

Linux currently opens KiCad files with `xdg-open`. Unlike launching `pcbnew`
or `eeschema` directly, this does not give Kikakuka deterministic control over:

- which executable handles the file;
- whether an existing process is reused;
- whether a new process starts activated or in the background; or
- which environment reaches the final KiCad process.

Desktop portals, D-Bus activation, Flatpak, and an already-running KiCad
process can cause the final process to use an environment other than the one
passed to `xdg-open`.

## X11, Wayland, and KiCad

KiCad's published system requirements do not support running KiCad on
Wayland. They require Wayland- or XWayland-specific bugs to be reproduced in
an actual X11 session before the KiCad project will investigate them. KiCad's
Wayland status statement lists known problems with focus, window placement,
multi-window coordination, input, OpenGL rendering, performance, clipboard,
freezes, and crashes.

Relevant upstream documentation:

- [KiCad system requirements](https://www.kicad.org/help/system-requirements/#wayland)
- [KiCad Linux graphical backend policy](https://www.kicad.org/download/linux-distros/#about-graphical-backends-x11-wayland)
- [KiCad and Wayland support](https://www.kicad.org/blog/2025/06/KiCad-and-Wayland-Support/)
- [KiCad native Wayland tracking issue](https://gitlab.com/kicad/code/kicad/-/issues/7207)

The practical configurations are:

| Desktop configuration | KiCad backend | Notes |
| --- | --- | --- |
| X11 session | X11 | KiCad's supported Linux configuration; Kikakuka requests focus through EWMH. |
| Wayland session with XWayland | X11 through XWayland | Often more usable than native Wayland. Kikakuka requests focus through EWMH, but the compositor may reject it; this configuration is not covered by KiCad's X11 support policy. |
| Wayland session | Native Wayland | Runs with known KiCad, wxWidgets, and compositor limitations; external focus by PID is not generally available. |
| Wayland session without XWayland | X11 requested | Cannot start because no X11 display is available. |

## `GDK_BACKEND`

`GDK_BACKEND` selects the GTK display backend used by a newly created GTK
process. For example:

```sh
GDK_BACKEND=x11 pcbnew board.kicad_pcb
```

Setting `GDK_BACKEND=x11` affects only a process launched with that child
environment and its descendants. It cannot change the backend of an existing
KiCad process.

Kikakuka does **not** currently set `GDK_BACKEND` when it invokes `xdg-open`.
If X11 is adopted as the default KiCad launch policy, the implementation
should follow these rules:

1. Set `GDK_BACKEND=x11` only in the child environment used to launch KiCad
   editors on Linux. Do not mutate Kikakuka's global environment.
2. Do not apply the setting to FreeCAD or unrelated applications. FreeCAD's
   Qt backend and its in-process activation behavior are separate concerns.
3. Launch `pcbnew` or `eeschema` directly when the backend must be guaranteed.
   Passing the variable to `xdg-open` is not a guarantee because activation
   may be delegated to another process.
4. Require a usable `DISPLAY`. A Wayland session normally supplies one when
   XWayland is installed; a pure Wayland session may not.
5. Treat XWayland as a compatibility path, not as proof that the application
   is running in a supported X11 desktop session.
6. Preserve a documented opt-out for testing native Wayland and for systems
   whose packaging or graphics driver fails under XWayland.
7. Report the selected launch path and backend in diagnostics so focus and
   rendering failures can be distinguished.

An always-on global assignment such as setting `os.environ["GDK_BACKEND"]`
inside Kikakuka is not appropriate. It would also affect child applications,
override an explicit user choice, fail on systems without XWayland, and still
would not affect an already-running or separately activated KiCad process.

## Direction for complete Linux support

Linux desktop integration should be split by capability rather than treated
as a single Wayland-versus-X11 switch:

1. Keep the existing POSIX mesh, process, and KiCad IPC implementation.
2. Use the X11/EWMH focus backend for X11 and XWayland processes.
3. Use direct KiCad editor launches when deterministic executable selection,
   child environment, or foreground policy is required.
4. Let FreekiCAD perform document activation inside FreeCAD whenever a live
   node is available.
5. On native Wayland, report external focus as unavailable unless a supported
   activation-token flow or target-process cooperation is present.

This design makes X11 functionality useful on both X11 desktops and XWayland
while keeping the remaining Wayland restrictions explicit.
