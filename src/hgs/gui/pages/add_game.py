"""Add a game folder that has no hgs-game.json: describe it in a form.

The description is kept inside HGS; the game's folder is not modified.
"""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

KINDS = [
    ("python-standalone", "Python — наявний сервер зі своїм портом (standalone)"),
    ("python-worker", "Python — логіка гри на hgs_sdk.GameLogic (worker)"),
    ("html-relay", "HTML/JS — без серверного коду (relay)"),
    ("html-logic", "HTML/JS — серверна логіка на Python (logic)"),
]


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    return (s or "game")[:48].rjust(2, "g")


class AddGameDialog(QDialog):
    def __init__(self, parent: QWidget, folder: str) -> None:
        super().__init__(parent)
        self.folder = Path(folder)
        self.setWindowTitle("Нова гра")
        self.resize(620, 0)
        lay = QVBoxLayout(self)
        info = QLabel(f"У папці {folder} немає файлу hgs-game.json. Опишіть гру — опис зберігається в HGS, "
                      "файли гри не змінюються.")
        info.setWordWrap(True)
        lay.addWidget(info)
        form = QFormLayout()
        self.name = QLineEdit(self.folder.name)
        self.gid = QLineEdit(slug(self.folder.name))
        self.kind = QComboBox()
        for key, label in KINDS:
            self.kind.addItem(label, key)
        self.kind.currentIndexChanged.connect(self._update)
        self.entry = QLineEdit()
        browse = QPushButton("Огляд…")
        browse.clicked.connect(self._browse_entry)
        entry_row = QWidget()
        h = QHBoxLayout(entry_row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.entry)
        h.addWidget(browse)
        self.cls = QLineEdit("Game")
        self.python = QComboBox()
        self.python.addItem("Автоматично (.venv гри, якщо є)", "auto")
        self.python.addItem("Лише віртуальне середовище гри (.venv)", "venv")
        self.python.addItem("Системний Python (PATH)", "system")
        self.args = QLineEdit("--port {port}")
        self.port = QSpinBox()
        self.port.setRange(0, 65535)
        self.port.setSpecialValueText("автоматично")
        self.auth = QComboBox()
        for key, label in (("invite", "Коди запрошень"), ("password", "Пароль гри"), ("account", "Акаунти гравців"),
                           ("open", "Без пароля (лише локально)")):
            self.auth.addItem(label, key)
        self.max_players = QSpinBox()
        self.max_players.setRange(1, 1000)
        self.max_players.setValue(8)
        rows = [("Назва:", self.name), ("ID гри:", self.gid), ("Тип:", self.kind), ("Точка входу (.py):", entry_row),
                ("Клас логіки:", self.cls), ("Python:", self.python), ("Аргументи:", self.args),
                ("Порт сервера:", self.port), ("Вхід гравців:", self.auth), ("Гравців у кімнаті:", self.max_players)]
        self._rows = {}
        for label, w in rows:
            form.addRow(label, w)
            self._rows[label] = w
        lay.addLayout(form)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setObjectName("Muted")
        lay.addWidget(self.hint)
        box = QDialogButtonBox(QDialogButtonBox.Cancel)
        ok = box.addButton("Додати", QDialogButtonBox.AcceptRole)
        ok.setObjectName("Primary")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        guess = next((p for p in ("server/main.py", "main.py", "server.py", "app.py") if (self.folder / p).is_file()), "")
        self.entry.setText(guess)
        self._update()

    def _set_row_visible(self, label: str, visible: bool) -> None:
        w = self._rows[label]
        form: QFormLayout = self.layout().itemAt(1).layout()
        form.setRowVisible(w, visible)

    def _update(self) -> None:
        kind = self.kind.currentData()
        py = kind.startswith("python")
        self._set_row_visible("Точка входу (.py):", kind != "html-relay")
        self._set_row_visible("Клас логіки:", kind in ("python-worker", "html-logic"))
        self._set_row_visible("Python:", py)
        self._set_row_visible("Аргументи:", kind == "python-standalone")
        self._set_row_visible("Порт сервера:", kind == "python-standalone")
        hints = {
            "python-standalone": "HGS запускає ваш сервер, передає йому вільний порт ({port} в аргументах і змінна PORT) "
                                 "і пропускає до нього гравців через /hgs/v1/proxy/<id>/. Сервер має слухати 127.0.0.1.",
            "python-worker": "Клас на основі hgs_sdk.GameLogic. HGS бере на себе вхід, кімнати й безпеку.",
            "html-relay": "Браузерна гра з hgs.js; сервер лише пересилає події між гравцями кімнати.",
            "html-logic": "Браузерна гра з hgs.js + серверна логіка (hgs_sdk.GameLogic) у файлі .py.",
        }
        self.hint.setText(hints[kind])

    def _browse_entry(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Точка входу", str(self.folder), "Python (*.py)")
        if path:
            try:
                self.entry.setText(Path(path).resolve().relative_to(self.folder.resolve()).as_posix())
            except ValueError:
                self.hint.setText("Файл має бути всередині папки гри.")

    def manifest(self) -> dict:
        kind = self.kind.currentData()
        entry = self.entry.text().strip()
        cls = self.cls.text().strip() or "Game"
        if kind == "python-standalone":
            backend = {"mode": "standalone", "entry": entry, "python": self.python.currentData(),
                       "args": self.args.text().split(), "port": self.port.value() or "auto"}
            adapter = "python"
        elif kind == "python-worker":
            backend = {"mode": "worker", "entry": f"{entry}:{cls}", "python": self.python.currentData()}
            adapter = "python"
        elif kind == "html-logic":
            backend = {"mode": "logic", "module": entry, "class": cls}
            adapter = "html"
        else:
            backend = {"mode": "relay"}
            adapter = "html"
        return {"id": self.gid.text().strip(), "name": self.name.text().strip(), "adapter": adapter,
                "backend": backend, "auth": {"mode": self.auth.currentData()},
                "rooms": {"max_players": self.max_players.value()}}
