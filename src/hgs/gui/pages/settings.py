"""Settings page: general server settings, approved programs, data folder, about."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QSettings, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from hgs import PROTOCOL_VERSION, __version__
from hgs.gui.widgets.common import Banner

if TYPE_CHECKING:
    from hgs.gui.core_client import CoreClient


def gui_prefs() -> QSettings:
    return QSettings("HGS", "HOME GAME SERVER")


class SettingsPage(QWidget):
    def __init__(self, client: "CoreClient", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self.data: dict[str, Any] | None = None
        self.setObjectName("Page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Settings")
        title.setObjectName("PageTitle")
        lay.addWidget(title)
        self.banner = Banner()
        lay.addWidget(self.banner)

        general = QGroupBox("Сервер")
        form = QFormLayout(general)
        self.name = QLineEdit()
        self.name.setMaxLength(64)
        self.max_conn = QSpinBox()
        self.max_conn.setRange(1, 1000)
        self.hello = QDoubleSpinBox()
        self.hello.setRange(0.5, 60)
        self.hello.setSuffix(" с")
        self.log_mb = QSpinBox()
        self.log_mb.setRange(1, 1000)
        self.log_mb.setSuffix(" МБ")
        self.log_keep = QSpinBox()
        self.log_keep.setRange(1, 100)
        self.metrics = QDoubleSpinBox()
        self.metrics.setRange(0.5, 60)
        self.metrics.setSuffix(" с")
        for label, w in (("Назва сервера:", self.name), ("З'єднань з однієї IP:", self.max_conn),
                         ("Час на вхід гравця:", self.hello), ("Розмір файлу логу:", self.log_mb),
                         ("Скільки файлів логу тримати:", self.log_keep), ("Оновлення метрик кожні:", self.metrics)):
            form.addRow(label, w)
        save = QPushButton("Зберегти")
        save.setObjectName("Primary")
        save.clicked.connect(self.save)
        form.addRow("", save)
        note = QLabel("Розмір логів застосовується після перезапуску HGS. Мережа — на сторінці Network.")
        note.setObjectName("Muted")
        form.addRow("", note)
        lay.addWidget(general)

        ui = QGroupBox("Вікно")
        ul = QVBoxLayout(ui)
        self.tray = QCheckBox("Згортати в трей під час закриття, якщо працюють сервери")
        self.tray.setChecked(gui_prefs().value("minimize_to_tray", True, type=bool))
        self.tray.toggled.connect(lambda v: gui_prefs().setValue("minimize_to_tray", v))
        ul.addWidget(self.tray)
        lay.addWidget(ui)

        progs = QGroupBox("Дозволені програми (поза папками ігор)")
        pl = QVBoxLayout(progs)
        self.programs = QListWidget()
        pl.addWidget(self.programs)
        remove = QPushButton("Скасувати дозвіл")
        remove.clicked.connect(self.remove_program)
        pl.addWidget(self._row(remove))
        lay.addWidget(progs)

        about = QGroupBox("Про програму")
        al = QFormLayout(about)
        self.data_dir = QLabel()
        open_data = QPushButton("Відкрити папку даних")
        open_data.clicked.connect(lambda: self.data and QDesktopServices.openUrl(QUrl.fromLocalFile(self.data["data_dir"])))
        al.addRow("Версія:", QLabel(f"HOME GAME SERVER {__version__} · протокол v{PROTOCOL_VERSION}"))
        al.addRow("Дані:", self._row(self.data_dir, open_data))
        self.ports = QLabel()
        al.addRow("Локальні порти:", self.ports)
        self.secrets = QLabel()
        al.addRow("Секрети:", self.secrets)
        lay.addWidget(about)
        lay.addStretch(1)

    @staticmethod
    def _row(*widgets: QWidget) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        for x in widgets:
            h.addWidget(x)
        h.addStretch(1)
        return w

    def refresh(self) -> None:
        self.client.call("GET", "/settings", None, self._on_data)

    def _on_data(self, ok: bool, data: Any) -> None:
        if not ok:
            self.banner.show_message(f"Не вдалося отримати налаштування: {data}")
            return
        self.data = data
        self.name.setText(data["server_name"])
        self.max_conn.setValue(data["max_connections_per_ip"])
        self.hello.setValue(data["hello_timeout_s"])
        self.log_mb.setValue(data["log_max_file_mb"])
        self.log_keep.setValue(data["log_keep_files"])
        self.metrics.setValue(data["metrics_interval_s"])
        self.programs.clear()
        self.programs.addItems(data["allowed_programs"])
        self.data_dir.setText(data["data_dir"])
        self.ports.setText(f"Admin API 127.0.0.1:{data['admin_port'] or 'auto'} · Worker Bus 127.0.0.1:"
                           f"{data['worker_bus_port'] or 'auto'}")
        self.secrets.setText("зашифровані Windows DPAPI" if data["secrets_protected"]
                             else "НЕ зашифровані (лише для розробки на не-Windows системах)")

    def save(self) -> None:
        body = {"server_name": self.name.text().strip() or "Home Game Server", "max_connections_per_ip": self.max_conn.value(),
                "hello_timeout_s": self.hello.value(), "log_max_file_mb": self.log_mb.value(),
                "log_keep_files": self.log_keep.value(), "metrics_interval_s": self.metrics.value()}

        def done(ok: bool, data: Any) -> None:
            if ok:
                self.banner.hide()
                self._on_data(True, data)
            else:
                self.banner.show_message(f"Не вдалося зберегти: {data}")
        self.client.call("PUT", "/settings", body, done)

    def remove_program(self) -> None:
        item = self.programs.currentItem()
        if item is None:
            return
        if QMessageBox.question(self, "Скасувати дозвіл", f"Ігри більше не зможуть запускати:\n{item.text()}") == QMessageBox.Yes:
            self.client.call("POST", "/allowlist/remove", {"path": item.text()}, lambda ok, d: self.refresh())
