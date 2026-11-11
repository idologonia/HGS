"""Saves dialog of a game: snapshots, backups, journal; save now, back up, restore."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QTabWidget, QVBoxLayout, QWidget

from hgs.gui.theme.dark import RED
from hgs.gui.widgets.common import fmt_bytes
from hgs.gui.widgets.table import ago, fill, make_table, selected_key

if TYPE_CHECKING:
    from hgs.gui.core_client import CoreClient


class SavesDialog(QDialog):
    def __init__(self, parent: QWidget, client: "CoreClient", g: dict, data_dir: str) -> None:
        super().__init__(parent)
        self.client, self.g = client, g
        self.setWindowTitle(f"Збереження — {g['name']}")
        self.resize(760, 520)
        lay = QVBoxLayout(self)
        info = QLabel("Знімок — стан гри, який вона сама віддає HGS (при зупинці, періодично, вручну); формат визначає "
                      "гра. Резервна копія — zip-архів даних гри в HGS. Відновлення — лише для зупиненого сервера; "
                      "поточний стан спершу автоматично архівується.")
        info.setWordWrap(True)
        info.setObjectName("Muted")
        lay.addWidget(info)
        self.tabs = QTabWidget()
        self.snapshots = make_table(["Файл", "Розмір", "Коли"])
        self.backups = make_table(["Файл", "Розмір", "Коли"])
        self.journal = make_table(["Коли", "Тип / причина", "Результат"])
        self.tabs.addTab(self.snapshots, "Знімки стану")
        self.tabs.addTab(self.backups, "Резервні копії")
        self.tabs.addTab(self.journal, "Журнал")
        lay.addWidget(self.tabs, 1)
        self.msg = QLabel()
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)
        row = QHBoxLayout()
        running = g["state"] == "running"
        self.save_btn = QPushButton("Зберегти зараз")
        self.save_btn.setEnabled(running)
        self.save_btn.setToolTip("" if running else "Сервер не працює")
        self.save_btn.clicked.connect(lambda: self._call("POST", f"/games/{g['id']}/save", None, "✓ Знімок збережено"))
        backup = QPushButton("Створити резервну копію")
        backup.clicked.connect(lambda: self._call("POST", f"/games/{g['id']}/backup", None, "✓ Резервну копію створено"))
        self.restore_btn = QPushButton("Відновити вибране…")
        self.restore_btn.setEnabled(not running and g["state"] not in ("starting", "stopping"))
        self.restore_btn.setToolTip("" if self.restore_btn.isEnabled() else "Спочатку зупиніть сервер")
        self.restore_btn.clicked.connect(self.restore)
        folder = QPushButton("Відкрити папку")
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(f"{data_dir}/games/{g['id']}")))
        close = QPushButton("Закрити")
        close.clicked.connect(self.accept)
        for b in (self.save_btn, backup, self.restore_btn, folder):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        self.load()

    def load(self) -> None:
        def done(ok: bool, data: Any) -> None:
            if not ok:
                self.msg.setText(f"Помилка: {data}")
                return
            fill(self.snapshots, [[s["file"], fmt_bytes(s["size_bytes"]), ago(s["modified"])] for s in data["snapshots"]],
                 keys=[s["file"] for s in data["snapshots"]])
            fill(self.backups, [[b["file"], fmt_bytes(b["size_bytes"]), ago(b["modified"])] for b in data["backups"]],
                 keys=[b["file"] for b in data["backups"]])
            fill(self.journal, [[j["created_at"], j["reason"], "✓" if j["ok"] else f"✗ {j['error'] or ''}"]
                                for j in data["journal"]])
        self.client.call("GET", f"/games/{self.g['id']}/saves", None, done)

    def _call(self, method: str, path: str, body: Any, success: str) -> None:
        def done(ok: bool, data: Any) -> None:
            self.msg.setText(success if ok else f"Помилка: {data}")
            self.msg.setStyleSheet("" if ok else f"color: {RED};")
            self.load()
        self.client.call(method, path, body, done)

    def restore(self) -> None:
        tab = self.tabs.currentIndex()
        if tab == 2:
            self.msg.setText("Виберіть знімок або резервну копію")
            return
        table = self.snapshots if tab == 0 else self.backups
        file = selected_key(table)
        if file is None:
            self.msg.setText("Виберіть файл у списку")
            return
        what = "знімок стану" if tab == 0 else "резервну копію"
        if QMessageBox.question(self, "Відновити", f"Відновити {what} {file}?\n\nПоточний стан буде збережено в резервну "
                                "копію «before_restore», тож відновлення можна скасувати.") != QMessageBox.Yes:
            return
        path = "restore-snapshot" if tab == 0 else "restore-backup"
        self._call("POST", f"/games/{self.g['id']}/{path}", {"file": file},
                   "✓ Відновлено. Дані буде використано під час наступного запуску сервера.")
