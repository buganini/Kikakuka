"""Shared desktop setup before PUI creates and shows Qt windows."""

import os
import sys
from pathlib import Path

from PUI.PySide6 import Application
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication

from linux_desktop import DESKTOP_ID, register_linux_desktop


def clear_opencv_qt_paths():
    """Keep older GUI OpenCV wheels from redirecting PySide's Qt runtime."""
    cv2 = sys.modules.get("cv2")
    if not sys.platform.startswith("linux") or not getattr(cv2, "__file__", None):
        return
    qt_dir = Path(cv2.__file__).resolve().parent / "qt"
    for variable, subdirectory in (
            ("QT_QPA_PLATFORM_PLUGIN_PATH", "plugins"),
            ("QT_QPA_FONTDIR", "fonts")):
        value = os.environ.get(variable)
        if value and Path(value).resolve() == qt_dir / subdirectory:
            del os.environ[variable]


class DesktopApplication(Application):
    desktop_id = DESKTOP_ID
    desktop_name = "Kikakuka"
    desktop_icon = "icon.png"
    desktop_arguments = ()

    def update(self, prev=None):
        if not self.ui:
            clear_opencv_qt_paths()
        if not self.ui and sys.platform.startswith("linux"):
            QGuiApplication.setApplicationName(self.desktop_id)
            QGuiApplication.setDesktopFileName(self.desktop_id)
            try:
                register_linux_desktop(self.desktop_id, self.desktop_name,
                                       self.desktop_icon, self.desktop_arguments)
            except OSError as error:
                print(f"Could not register Kikakuka desktop icon: {error}", file=sys.stderr)
            # PUI constructs QApplication([]), which PySide fills with PUI's
            # application.py path. X11 uses argv[0] as the WM_CLASS instance.
            # Supply our identity explicitly before any native window exists.
            self.ui = QApplication([self.desktop_id])
            if self.icon:
                self.ui.setWindowIcon(QIcon(self.icon))
        return super().update(prev)
