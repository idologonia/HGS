"""Rooms page: rooms of all games — create, lock/unlock, change the player limit, close."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from hgs.gui.widgets.common import Banner
from hgs.gui.widgets.table import ago, fill, make_table, selected_key

if TYPE_CHECKING:
    from hgs.gui.core_client import CoreClient

STATUS = {"open": "відкрита", "locked": "закрита для входу", "closed": "закрита"}


class RoomsPage(QWidget):
    def __init__(self, client: "CoreClient", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self.rooms: list[dict] = []
        self.games: list[dict] = []
        self.setObjectName("Page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Rooms")
        title.setObjectName("PageTitle")
        lay.addWidget(title)
        self.banner = Banner()
        lay.addWidget(self.banner)
        bar = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.addItem("Усі ігри", None)
        self.filter.currentIndexChanged.connect(self.refresh)
        bar.addWidget(QLabel("Гра:"))
        bar.addWidget(self.filter)
        bar.addStretch(1)
        self.create_btn = QPushButton("+ Створити кімнату")
        self.create_btn.setObjectName("Primary")
        self.create_btn.clicked.connect(self.create)
        bar.addWidget(self.create_btn)
        lay.addLayout(bar)
        body = QHBoxLayout()
        self.table = make_table(["Кімната", "Гра", "ID", "Гравці", "Стан", "Хост", "Створено"])
        self.table.itemSelectionChanged.connect(self._show_players)
        body.addWidget(self.table, 3)
        side = QVBoxLayout()
        side.addWidget(QLabel("Гравці в кімнаті:"))
        self.players = QListWidget()
        side.addWidget(self.players, 1)
        body.addLayout(side, 1)
        lay.addLayout(body, 1)
        row = QHBoxLayout()
        self.lock_btn = QPushButton("Закрити / відкрити вхід")
        self.lock_btn.clicked.connect(self.toggle_lock)
        self.limit_btn = QPushButton("Ліміт гравців…")
        self.limit_btn.clicked.connect(self.set_limit)
        self.close_btn = QPushButton("Закрити кімнату")
        self.close_btn.setObjectName("Danger")
        self.close_btn.clicked.connect(self.close_room)
        for b in (self.lock_btn, self.limit_btn, self.close_btn):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

    def refresh(self) -> None:
        self.client.call("GET", "/games", None, self._on_games)
        gid = self.filter.currentData()
        self.client.call("GET", "/rooms" + (f"?game_id={gid}" if gid else ""), None, self._on_rooms)

    def _on_games(self, ok: bool, data: Any) -> None:
        if not ok:
            return
        self.games = data
        current = self.filter.currentData()
        self.filter.blockSignals(True)
        self.filter.clear()
        self.filter.addItem("Усі ігри", None)
        for g in data:
            self.filter.addItem(g["name"], g["id"])
        self.filter.setCurrentIndex(max(0, self.filter.findData(current)))
        self.filter.blockSignals(False)
        self.create_btn.setEnabled(any(g["state"] == "running" for g in data))

    def _on_rooms(self, ok: bool, data: Any) -> None:
        if not ok:
            self.banner.show_message(f"Не вдалося отримати кімнати: {data}")
            return
        self.rooms = data
        names = {g["id"]: g["name"] for g in self.games}
        fill(self.table, [[r["name"], names.get(r["game_id"], r["game_id"]), r["id"], f"{r['players']}/{r['max_players']}",
                           STATUS.get(r["status"], r["status"]),
                           next((p["name"] for p in r["player_list"] if p["id"] == r["host"]), "—"),
                           ago(r["created_at"])] for r in data],
             keys=[(r["game_id"], r["id"]) for r in data])
        self._show_players()

    def _selected(self) -> dict | None:
        key = selected_key(self.table)
        return next((r for r in self.rooms if (r["game_id"], r["id"]) == key), None)

    def _show_players(self) -> None:
        r = self._selected()
        self.players.clear()
        for p in (r["player_list"] if r else []):
            self.players.addItem(p["name"] + (" (хост)" if r and p["id"] == r["host"] else ""))
        for b in (self.lock_btn, self.limit_btn, self.close_btn):
            b.setEnabled(r is not None)

    def _done(self, ok: bool, data: Any) -> None:
        if ok:
            self.banner.hide()
        else:
            self.banner.show_message(str(data))
        self.refresh()

    def create(self) -> None:
        running = [g for g in self.games if g["state"] == "running"]
        dlg = QDialog(self)
        dlg.setWindowTitle("Нова кімната")
        form = QFormLayout(dlg)
        game = QComboBox()
        for g in running:
            game.addItem(g["name"], g["id"])
        name = QLineEdit()
        name.setMaxLength(24)
        limit = QSpinBox()
        limit.setRange(1, 1000)
        limit.setValue(8)
        form.addRow("Гра:", game)
        form.addRow("Назва:", name)
        form.addRow("Гравців (не більше ліміту гри):", limit)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(dlg.accept)
        box.rejected.connect(dlg.reject)
        form.addRow(box)
        if dlg.exec() == QDialog.Accepted and name.text().strip():
            self.client.call("POST", "/rooms", {"game_id": game.currentData(), "name": name.text().strip(),
                                                "max_players": limit.value()}, self._done)

    def toggle_lock(self) -> None:
        r = self._selected()
        if r:
            status = "open" if r["status"] == "locked" else "locked"
            self.client.call("POST", f"/rooms/{r['game_id']}/{r['id']}/update", {"status": status}, self._done)

    def set_limit(self) -> None:
        r = self._selected()
        if r:
            value, ok = QInputDialog.getInt(self, "Ліміт гравців", f"Кімната «{r['name']}»:", r["max_players"], 1, 1000)
            if ok:
                self.client.call("POST", f"/rooms/{r['game_id']}/{r['id']}/update", {"max_players": value}, self._done)

    def close_room(self) -> None:
        r = self._selected()
        if not r:
            return
        extra = f"\n\nУ кімнаті {r['players']} гравців — вони вийдуть з кімнати." if r["players"] else ""
        if QMessageBox.question(self, "Закрити кімнату", f"Закрити кімнату «{r['name']}»?{extra}") == QMessageBox.Yes:
            self.client.call("DELETE", f"/rooms/{r['game_id']}/{r['id']}", None, self._done)
