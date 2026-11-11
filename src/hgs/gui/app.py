"""GUI entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from hgs.gui.main_window import MainWindow
from hgs.gui.theme.dark import apply_theme


def run_gui(data_dir: Path) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("HOME GAME SERVER")
    app.setOrganizationName("HGS")
    apply_theme(app)
    window = MainWindow(data_dir)

    def _session_end(manager) -> None:
        # Windows is logging off / shutting down: stop servers gracefully.
        window._quitting = True
        window.launcher.stop(timeout_ms=8000)
        window.launcher.wait_stopped(8000)

    app.commitDataRequest.connect(_session_end)
    window.show()
    return app.exec()
