# Cross-platform behavior

Kikakuka supports macOS, Windows, and Linux, but program launching and window
activation use the native mechanism of each desktop. File processing, KiCad
IPC, FreekiCAD, and the Instance Manager protocol otherwise provide the same
workflow on every supported platform.

## Overview

| Behavior | macOS | Windows | Linux |
| --- | --- | --- | --- |
| Open a KiCad file | Launch Services through `open`; new applications are requested in the background. | The registered file association; the new window is requested without activation. | Launch `pcbnew`, `eeschema`, or `kicad` directly according to the file suffix. |
| Open a FreeCAD file | Launch Services through `open -a ... -n -W --args`, with the file path passed as an application argument. | Start the detected or selected FreeCAD executable directly with the file path. | Start the detected executable or AppImage with the file path in a detached user service or process session. |
| Bring a process to front | AppleScript selects the application process by PID. | Win32 enumerates visible windows by PID, restores a minimized window, and requests foreground activation. | EWMH `_NET_ACTIVE_WINDOW` for X11 or XWayland; unavailable for native Wayland windows. |
| Open a folder | `open` | `explorer` | `xdg-open` |
| Instance Manager transport | Per-user Unix domain sockets. | Per-user Windows named pipes. | Per-user Unix domain sockets. |
| Instance Manager location | `/tmp/kikakuka-<UID>` | `%LOCALAPPDATA%\Kikakuka\instances`, with a per-user temporary-directory fallback. | `/tmp/kikakuka-<UID>` |
| FreeCAD package | A detected or selected `.app` bundle or executable. | A detected or selected FreeCAD executable. | A distribution package or AppImage. Snap is not supported. |

Custom KiCad and FreeCAD installations can be selected in the Add-ons tab.
The selection is stored in `~/.kikakuka`; **Auto** removes the corresponding
manual selection and restores automatic detection.

## macOS

KiCad files are opened with Launch Services. Kikakuka uses `open -n -g` so a
new application instance is requested without immediately taking focus. A
manually selected KiCad `.app` bundle is launched with `open -a`.

FreeCAD is launched with a sanitized child environment so Python, Qt, QML,
and dynamic-library settings inherited from KiCad do not contaminate its own
runtime. The file path is passed after `--args` because relying on the Finder
open-file event is not reliable for FreeCAD. A selected FreeCAD `.app` bundle
is handled in the same way.

Bring-to-front finds the application process by PID and asks macOS `System
Events` to make it frontmost. This is a best-effort request and remains subject
to macOS focus and privacy policy. When FreekiCAD is running, it also selects
the requested FreeCAD document and MDI tab inside the application.

## Windows

> [!NOTE]
> Affected Windows builds expose only one IPC endpoint for multiple instances
> due to [KiCad issue #23994](https://gitlab.com/kicad/code/kicad/-/work_items/23994).
> Close all running KiCad applications before first using Kikakuka or
> FreekiCAD. Kikakuka creates a persistent `%TEMP%\kicad\api.sock` sentinel
> that makes subsequently launched KiCad instances use PID-specific named
> pipes.

KiCad files are opened with the Windows file association. Kikakuka requests
`SW_SHOWNOACTIVATE`, allowing background integration work without deliberately
stealing focus. A user-initiated navigation can subsequently bring the editor
to the front.

FreeCAD is launched directly from the detected or manually selected
executable with the same sanitized child-environment policy used on other
platforms.

Bring-to-front enumerates visible top-level windows belonging to the target
PID, restores the selected window if it is minimized, and calls the Win32
foreground API. Windows can reject foreground stealing, so activation remains
best effort.

## Linux

> [!NOTE]
> FreeCAD installed through Snap is not supported. Snap confinement isolates
> the filesystem and local sockets, preventing FreeCAD from communicating
> with KiCad's IPC endpoint. Selecting the Snap executable manually does not
> remove this isolation; use a non-Snap FreeCAD package or the official
> AppImage.

KiCad files do not use `xdg-open`. Kikakuka launches `pcbnew` for
`.kicad_pcb`, `eeschema` for `.kicad_sch`, and `kicad` for `.kicad_pro`
directly. A manually selected KiCad AppImage is launched directly for all
three file types. If the selected executable belongs to a normal KiCad
installation, Kikakuka resolves the matching editor beside it.

In a Wayland session, Kikakuka tests whether `DISPLAY` reaches a working
XWayland server. When it does, only the new KiCad child receives
`GDK_BACKEND=x11`; this makes its window visible to the same X11/EWMH
activation path used by Instance Manager. Native X11 sessions need no backend
override. If XWayland is unavailable, KiCad is launched without the override
and uses its normal native backend.

FreeCAD is launched directly from a detected executable or selected AppImage.
The launcher removes inherited KiCad Python, Qt, QML, library, and IPC
variables. Reliable operation requires launching FreeCAD as a transient
`systemd-run --user` service so it is not owned by the short-lived KiCad action
process.

`systemd-run --user` is required for reliable **Open in FreeCAD** operation.
The command starts inside a temporary KiCad action process. That action process
exits after dispatching the request, and Linux desktop or process-management
environments can also clean up its remaining process tree. A directly spawned
FreeCAD can therefore show its splash screen and then exit. The transient user
service transfers FreeCAD's lifetime to the logged-in user's systemd manager,
allowing it to continue after the KiCad action process has finished. The
launcher passes only the sanitized desktop, display, locale, and graphics
environment needed by FreeCAD.

The transient service uses `PrivateTmp=no`. FreeCAD must see the same
`/tmp/kikakuka-<UID>` Instance Manager sockets and KiCad IPC endpoints as
KiCad; a private temporary directory would isolate those endpoints and prevent
communication. If no user manager is available or the transient service
cannot be created, Kikakuka attempts a new detached process session with
standard input disconnected as a best-effort fallback. That fallback cannot
guarantee that FreeCAD survives after the KiCad action process exits. Startup
output from either launch path is written to:

```text
/tmp/kikakuka-<UID>/freecad-startup.log
```

When only a Snap installation is detected, the Add-ons tab reports
`Unsupported: Snap-installed FreeCAD cannot communicate with KiCad` and does
not offer to install FreekiCAD into it.

### Linux (X11 or XWayland)

Kikakuka finds a top-level X11 window through `_NET_WM_PID` and sends the EWMH
`_NET_ACTIVE_WINDOW` message. This works for native X11 windows and for
applications exposing an XWayland window. It uses the system `libX11` library
directly and requires a usable `DISPLAY`; no Python Xlib package is needed.

The window manager or Wayland compositor makes the final focus decision, so a
bring-to-front request may still be rejected. Under a Wayland desktop, an
application running natively rather than through XWayland cannot be found in
the X11 client list.

### Linux (native Wayland)

Wayland does not generally allow one unrelated process to enumerate or
activate another application's windows. Kikakuka therefore reports that
bring-to-front is unavailable when the target has no XWayland window instead
of reporting a false success.

The Wayland protocol for cooperative foreground activation is
[`xdg-activation-v1`](https://wayland.app/protocols/xdg-activation-v1); its
global interface is named `xdg_activation_v1`. It does not provide an
X11-style operation that raises an arbitrary window by PID. A native Wayland
implementation must instead perform these steps:

1. While handling a real user action, the initiating application requests an
   activation token from the compositor. The request should include its
   `wl_surface`, the input `wl_seat`, and the recent input-event serial.
2. The initiating application passes the token to the target. A newly launched
   application can receive it through `XDG_ACTIVATION_TOKEN`; an already
   running application needs an IPC or D-Bus method that accepts the token.
3. The target application consumes the token and calls
   `xdg_activation_v1.activate(token, target_surface)` for one of its own
   `wl_surface` objects.
4. The compositor validates the token and makes the final activation decision.
   It may reject an expired token, a token without a sufficiently recent user
   event, or any request prohibited by its focus-stealing policy.

FreekiCAD can still activate a document and its MDI tab from inside a running
FreeCAD process. That in-process action does not guarantee that the compositor
will raise the FreeCAD top-level window. KiCad's own native Wayland support
also has upstream limitations; X11 remains KiCad's supported Linux graphical
configuration.

Relevant KiCad documentation:

- [KiCad system requirements](https://www.kicad.org/help/system-requirements/#wayland)
- [KiCad Linux graphical backend policy](https://www.kicad.org/download/linux-distros/#about-graphical-backends-x11-wayland)
- [KiCad and Wayland support](https://www.kicad.org/blog/2025/06/KiCad-and-Wayland-Support/)
