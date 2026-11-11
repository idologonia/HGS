"""Logs page: core, security and per-game logs with live updates."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

if TYPE_CHECKING:
    from hgs.gui.core_client import CoreClient

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
SOURCE_NAMES = {"core": "Ядро", "security": "Безпека"}


def format_entry(e: dict) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.get("ts", 0)))
    line = f"{ts}  {e.get('level', ''):<8} {e.get('logger', '')}: {e.get('msg', '')}"
    if e.get("exc"):
        line += "\n" + e["exc"]
    return line


class LogsPage(QWidget):
    def __init__(self, client: "CoreClient", data_dir: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self.data_dir = data_dir
        self.setObjectName("Page")
        self.paused = False
        self._pending: list[str] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Logs")
        title.setObjectName("PageTitle")
        root.addWidget(title)
        bar = QHBoxLayout()
        self.source = QComboBox()
        self.source.setMinimumWidth(200)
        self.source.currentIndexChanged.connect(self.reload)
        self.level = QComboBox()
        self.level.addItems(["INFO", "WARNING", "ERROR", "DEBUG"])
        self.level.currentIndexChanged.connect(self.reload)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Пошук…")
        self.search.textChanged.connect(lambda _: self._search_timer.start())
        self.pause_btn = QPushButton("Пауза")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._toggle_pause)
        folder = QPushButton("Відкрити папку логів")
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.data_dir)))
        for w in (QLabel("Джерело:"), self.source, QLabel("Рівень від:"), self.level, self.search, self.pause_btn, folder):
            bar.addWidget(w)
        root.addLayout(bar)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(5000)
        self.view.setStyleSheet("font-family: Consolas, 'DejaVu Sans Mono', monospace; font-size: 9pt;")
        root.addWidget(self.view, 1)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self.reload)
        # batch live lines: at most 10 UI updates per second
        self._flush = QTimer(self)
        self._flush.setInterval(100)
        self._flush.timeout.connect(self._flush_pending)
        self._flush.start()

    def current_source(self) -> str | None:
        return self.source.currentData()

    def load_sources(self, select: str | None = None) -> None:
        def done(ok: bool, data) -> None:
            if not ok:
                return
            want = select or self.current_source() or "core"
            self.source.blockSignals(True)
            self.source.clear()
            for s in data:
                self.source.addItem(SOURCE_NAMES.get(s, s.replace("game:", "Гра: ")), s)
            if select and select not in data:
                self.source.addItem(select.replace("game:", "Гра: "), select)
            idx = self.source.findData(want)
            self.source.setCurrentIndex(max(idx, 0))
            self.source.blockSignals(False)
            self.reload()
        self.client.call("GET", "/logs", None, done)

    def show_source(self, source: str) -> None:
        self.load_sources(select=source)

    def _accept(self, e: dict) -> bool:
        if LEVELS.get(e.get("level", "INFO"), 20) < LEVELS[self.level.currentText()]:
            return False
        q = self.search.text().strip().lower()
        return not q or q in e.get("msg", "").lower()

    def reload(self) -> None:
        src = self.current_source()
        if not src:
            return

        def done(ok: bool, data) -> None:
            self.view.clear()
            self._pending.clear()
            if not ok:
                self.view.setPlainText(f"Не вдалося завантажити лог: {data}")
                return
            if not data:
                self.view.setPlainText("(лог порожній)")
                return
            self.view.setPlainText("\n".join(format_entry(e) for e in data if self._accept(e)))
            self.view.moveCursor(QTextCursor.End)
        self.client.call("GET", f"/logs/{src}?tail=2000", None, done)

    def on_log_event(self, data: dict) -> None:
        if data.get("source") == self.current_source() and self._accept(data.get("entry", {})):
            self._pending.append(format_entry(data["entry"]))

    def _toggle_pause(self, on: bool) -> None:
        self.paused = on
        self.pause_btn.setText("Продовжити" if on else "Пауза")

    def _flush_pending(self) -> None:
        if self.paused or not self._pending:
            return
        lines, self._pending = self._pending, []
        self.view.appendPlainText("\n".join(lines))
