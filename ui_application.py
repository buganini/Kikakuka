"""Shared desktop setup before PUI creates and shows Qt windows."""

import sys

from PUI.PySide6 import Application
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication

from linux_desktop import DESKTOP_ID, register_linux_desktop


class DesktopApplication(Application):
    desktop_id = DESKTOP_ID
    desktop_name = "Kikakuka"
    desktop_icon = "icon.png"
    desktop_arguments = ()

    def update(self, prev=None):
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
