"""Starts the core as a child process and watches it.

The admin token is generated here and given to the core through stdin.
stdin stays open: if the GUI dies, the pipe closes and the core shuts down by itself.
"""

from __future__ import annotations

import json
import secrets
import sys
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal


class CoreLauncher(QObject):
    ready = Signal(int, int)  # admin_port, gateway_port
    failed = Signal(str)      # could not start / exited unexpectedly (message)
    stopped = Signal()        # exited after a requested stop

    def __init__(self, data_dir: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.data_dir = data_dir
        self.token = ""
        self.admin_port = 0
        self.gateway_port = 0
        self._stderr: deque[str] = deque(maxlen=40)
        self._stopping = False
        self._error_msg: str | None = None
        self.proc: QProcess | None = None

    def command(self) -> tuple[str, list[str]]:
        args = ["--core", "--token-stdin", "--data-dir", str(self.data_dir)]
        if getattr(sys, "frozen", False):  # PyInstaller build: hgs.exe --core ...
            return sys.executable, args
        return sys.executable, ["-m", "hgs", *args]

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def start(self) -> None:
        if self.is_running():
            return
        self.token = secrets.token_urlsafe(32)
        self._stderr.clear()
        self._stopping = False
        self._error_msg = None
        proc = QProcess(self)
        program, args = self.command()
        proc.setProgram(program)
        proc.setArguments(args)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        proc.started.connect(lambda: proc.write((self.token + "\n").encode()))
        self.proc = proc
        proc.start()

    def stop(self, timeout_ms: int = 15000) -> None:
        """Graceful stop: close the control pipe; kill only if the core does not exit in time."""
        if not self.is_running():
            return
        self._stopping = True
        assert self.proc is not None
        self.proc.closeWriteChannel()
        proc = self.proc
        QTimer.singleShot(timeout_ms, lambda: proc.kill() if proc.state() != QProcess.NotRunning else None)

    def wait_stopped(self, timeout_ms: int) -> bool:
        return self.proc is None or self.proc.state() == QProcess.NotRunning or self.proc.waitForFinished(timeout_ms)

    def last_stderr(self) -> str:
        return "\n".join(self._stderr)

    # -- process signals ------------------------------------------------------------
    def _on_stdout(self) -> None:
        assert self.proc is not None
        while self.proc.canReadLine():
            line = bytes(self.proc.readLine().data()).decode("utf-8", "replace").strip()
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("event") == "ready":
                self.admin_port = int(msg["admin_port"])
                self.gateway_port = int(msg["gateway_port"])
                self.ready.emit(self.admin_port, self.gateway_port)
            elif msg.get("event") == "error":
                self._error_msg = msg.get("message") or "unknown error"

    def _on_stderr(self) -> None:
        assert self.proc is not None
        text = bytes(self.proc.readAllStandardError().data()).decode("utf-8", "replace")
        for line in text.splitlines():
            if line.strip():
                self._stderr.append(line)

    def _on_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        if self._stopping and code == 0:
            self.stopped.emit()
            return
        if self._stopping:
            self.stopped.emit()
            return
        detail = self._error_msg or (self._stderr[-1] if self._stderr else f"exit code {code}")
        self.failed.emit(f"Server core stopped unexpectedly: {detail}")

    def _on_error(self, err: QProcess.ProcessError) -> None:
        if err == QProcess.FailedToStart:
            self.failed.emit(f"Cannot start the server core: {self.proc.errorString() if self.proc else err}")
