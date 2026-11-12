"""GUI entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from hgs.gui.main_window import MainWindow
from hgs.gui.theme.dark import apply_theme


def run_gui(data_dir: Path) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("HOME GAME SERVER")
    app.setOrganizationName("HGS")
    if os.environ.get("HGS_DATA_DIR") is None and _another_instance_running():
        return 0  # HGS is already open: its window was brought to the front
    apply_theme(app)
    window = MainWindow(data_dir)

    def _session_end(manager) -> None:
        # Windows is logging off / shutting down: stop servers gracefully.
        window._quitting = True
        window.launcher.stop(timeout_ms=8000)
        window.launcher.wait_stopped(8000)

    app.commitDataRequest.connect(_session_end)
    server = _single_instance_server(window)
    window.show()
    code = app.exec()
    if server is not None:
        server.close()
    return code


def _instance_name() -> str:
    import getpass
    import hashlib

    return "hgs-gui-" + hashlib.sha1(getpass.getuser().encode()).hexdigest()[:12]


def _another_instance_running() -> bool:
    """If HGS is already open, ask it to show its window and return True."""
    from PySide6.QtNetwork import QLocalSocket

    sock = QLocalSocket()
    sock.connectToServer(_instance_name())
    if not sock.waitForConnected(500):
        return False
    sock.write(b"show")
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    return True


def _single_instance_server(window):
    from PySide6.QtNetwork import QLocalServer

    server = QLocalServer()
    QLocalServer.removeServer(_instance_name())  # a stale name after a crash
    if not server.listen(_instance_name()):
        return None

    def on_connection() -> None:
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.readyRead.connect(lambda: (conn.readAll(), window.show_from_tray()))
    server.newConnection.connect(on_connection)
    return server
