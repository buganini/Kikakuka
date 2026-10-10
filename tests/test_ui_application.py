import os
from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux desktop setup")
class DesktopDialogTests(unittest.TestCase):
    def test_appimage_uses_qt_file_chooser_and_source_keeps_native_dialogs(self):
        script = """
from unittest.mock import patch
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog
from kikakuka.ui_application import DesktopApplication
import os

application = DesktopApplication()
with patch('kikakuka.ui_application.register_linux_desktop'), patch('kikakuka.ui_application.Application.update'):
    application.update()
assert QApplication.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs) == bool(os.environ.get('APPDIR'))
if os.environ.get('APPDIR'):
    def cancel():
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, QFileDialog):
                widget.reject()
    QTimer.singleShot(100, cancel)
    assert QFileDialog.getOpenFileName(None, 'Select KiCad installation')[0] == ''
"""
        for appdir in (None, "/tmp/test-appdir"):
            with self.subTest(appdir=appdir):
                environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
                environment.pop("APPDIR", None)
                if appdir:
                    environment["APPDIR"] = appdir
                result = subprocess.run(
                    [sys.executable, "-c", script], env=environment,
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
