"""Games page: a card per game with Start/Stop/Restart/Settings/Logs/Players/Status/Invite."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from hgs.gui.theme.dark import RED
from hgs.gui.widgets.common import Banner, Card, StateBadge, fmt_duration

if TYPE_CHECKING:
    from hgs.gui.core_client import CoreClient

ACTIVE = ("running", "starting", "stopping")


class GameCard(Card):
    action = Signal(str, str)  # game_id, action

    def __init__(self, game: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.game_id = game["id"]
        self.busy = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(6)
        top = QHBoxLayout()
        self.title = QLabel()
        self.title.setObjectName("CardTitle")
        self.badge = StateBadge()
        top.addWidget(self.title, 1)
        top.addWidget(self.badge)
        lay.addLayout(top)
        self.meta = QLabel()
        self.meta.setObjectName("Muted")
        self.stats = QLabel()
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {RED};")
        self.error.setTextInteractionFlags(Qt.TextSelectableByMouse)
        for w in (self.meta, self.stats, self.error):
            lay.addWidget(w)

        self.buttons: dict[str, QPushButton] = {}
        grid = QGridLayout()
        grid.setSpacing(6)
        spec = [("start", "Start"), ("stop", "Stop"), ("restart", "Restart"),
                ("settings", "Settings"), ("logs", "Logs"), ("players", "Players"),
                ("status", "Status"), ("invite", "Invite"), ("remove", "Видалити")]
        for i, (key, text) in enumerate(spec):
            b = QPushButton(text)
            if key == "start":
                b.setObjectName("Primary")
            if key == "remove":
                b.setObjectName("Danger")
            b.clicked.connect(lambda _=False, k=key: self.action.emit(self.game_id, k))
            self.buttons[key] = b
            grid.addWidget(b, i // 3, i % 3)
        lay.addLayout(grid)
        self.update_game(game)

    def update_game(self, g: dict) -> None:
        self.game = g
        self.title.setText(g["name"])
        self.badge.set_state(g["state"])
        self.meta.setText(f"{g['id']} · {g['adapter']} · {g['mode']} · v{g['version']} · вхід: {g['auth_mode']}")
        self.stats.setText(f"👥 гравців: {g['players_online']} · 🚪 кімнат: {g['rooms_open']} · "
                           f"⏱ {fmt_duration(g['uptime_s'])} · перезапусків: {g['restarts']}")
        self.error.setText(g["last_error"] or "")
        self.error.setVisible(bool(g["last_error"]))
        self._refresh_buttons()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        state = self.game["state"]
        running = state == "running"
        b = self.buttons
        b["start"].setEnabled(not self.busy and state not in ACTIVE)
        b["stop"].setEnabled(not self.busy and state not in ("stopped",))
        b["restart"].setEnabled(not self.busy and running)
        b["players"].setEnabled(running)
        b["invite"].setEnabled(self.game["auth_mode"] == "invite")
        b["invite"].setToolTip("" if self.game["auth_mode"] == "invite"
                               else "Гра використовує відкритий вхід (open) — запрошення не потрібні")
        b["remove"].setEnabled(not self.busy and state not in ACTIVE)
        for k in ("start", "stop", "restart"):
            b[k].setToolTip("Виконується операція…" if self.busy else "")


class GamesPage(QWidget):
    open_logs = Signal(str)  # log source

    def __init__(self, client: "CoreClient", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self.setObjectName("Page")
        self.cards: dict[str, GameCard] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)
        head = QHBoxLayout()
        title = QLabel("Games")
        title.setObjectName("PageTitle")
        head.addWidget(title, 1)
        self.add_btn = QPushButton("+ Додати гру")
        self.add_btn.setObjectName("Primary")
        self.add_btn.clicked.connect(self.add_game)
        head.addWidget(self.add_btn)
        root.addLayout(head)
        self.banner = Banner()
        root.addWidget(self.banner)
        self.empty = QLabel("Ще немає жодної гри. Натисніть «+ Додати гру» і виберіть папку гри, "
                            "у якій є файл hgs-game.json.")
        self.empty.setObjectName("Muted")
        self.empty.setWordWrap(True)
        root.addWidget(self.empty)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        holder.setObjectName("Page")
        self.grid = QGridLayout(holder)
        self.grid.setSpacing(12)
        self.grid.setAlignment(Qt.AlignTop)
        scroll.setWidget(holder)
        root.addWidget(scroll, 1)

    # -- data -----------------------------------------------------------------------
    def refresh(self) -> None:
        self.client.call("GET", "/games", callback=self._on_games)

    def _on_games(self, ok: bool, data) -> None:
        if not ok:
            self.banner.show_message(f"Не вдалося отримати список ігор: {data}")
            return
        ids = [g["id"] for g in data]
        for gid in list(self.cards):
            if gid not in ids:
                self.cards.pop(gid).deleteLater()
        for g in data:
            card = self.cards.get(g["id"])
            if card is None:
                card = GameCard(g)
                card.action.connect(self._on_action)
                self.cards[g["id"]] = card
            else:
                card.update_game(g)
        for i, gid in enumerate(ids):
            self.grid.addWidget(self.cards[gid], i // 2, i % 2)
        self.empty.setVisible(not ids)

    def game(self, game_id: str) -> dict | None:
        card = self.cards.get(game_id)
        return card.game if card else None

    # -- actions --------------------------------------------------------------------
    def add_game(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Виберіть папку гри (з файлом hgs-game.json)")
        if not path:
            return
        self.client.call("POST", "/games", {"path": path}, self._after("Не вдалося додати гру"))

    def _after(self, error_title: str, game_id: str | None = None) -> Callable[[bool, object], None]:
        def done(ok: bool, data) -> None:
            if game_id and game_id in self.cards:
                self.cards[game_id].set_busy(False)
            if ok:
                self.banner.hide()
            else:
                self.banner.show_message(f"{error_title}: {data}")
            self.refresh()
        return done

    def _on_action(self, game_id: str, action: str) -> None:
        g = self.game(game_id)
        if g is None:
            return
        card = self.cards[game_id]
        if action == "start":
            card.set_busy(True)
            self.client.call("POST", f"/games/{game_id}/start", None, self._after("Помилка запуску", game_id))
        elif action in ("stop", "restart"):
            grace = 5.0
            if g["players_online"] > 0:
                grace = ConfirmStopDialog.ask(self, g, action)
                if grace is None:
                    return
            elif action == "stop" and g["state"] == "running":
                if QMessageBox.question(self, "Зупинити сервер", f"Зупинити сервер «{g['name']}»?") != QMessageBox.Yes:
                    return
            card.set_busy(True)
            self.client.call("POST", f"/games/{game_id}/{action}", {"grace_s": grace},
                             self._after("Помилка зупинки" if action == "stop" else "Помилка перезапуску", game_id))
        elif action == "remove":
            if QMessageBox.question(
                self, "Видалити гру з реєстру",
                f"Прибрати «{g['name']}» з HGS?\n\nФайли гри в папці {g['root_path']} НЕ видаляються. "
                "Логи й дані HGS для цієї гри залишаться в папці даних HGS.") != QMessageBox.Yes:
                return
            self.client.call("DELETE", f"/games/{game_id}", None, self._after("Не вдалося видалити гру"))
        elif action == "logs":
            self.open_logs.emit(f"game:{game_id}")
        elif action == "settings":
            SettingsDialog(self, self.client, g, self.refresh).exec()
        elif action == "status":
            StatusDialog(self, g, self.client).exec()
        elif action == "players":
            PlayersDialog(self, self.client, g).exec()
        elif action == "invite":
            InviteDialog(self, self.client, g).exec()


# ======================================================================== dialogs
class ConfirmStopDialog(QDialog):
    @staticmethod
    def ask(parent: QWidget, g: dict, action: str) -> float | None:
        d = ConfirmStopDialog(parent, g, action)
        return d.grace.value() if d.exec() == QDialog.Accepted else None

    def __init__(self, parent: QWidget, g: dict, action: str) -> None:
        super().__init__(parent)
        verb = "Зупинити" if action == "stop" else "Перезапустити"
        self.setWindowTitle(f"{verb} сервер")
        lay = QVBoxLayout(self)
        text = QLabel(f"На сервері «{g['name']}» зараз {g['players_online']} гравців.\n"
                      "Вони отримають попередження і будуть відключені після паузи.")
        text.setWordWrap(True)
        lay.addWidget(text)
        form = QFormLayout()
        self.grace = QDoubleSpinBox()
        self.grace.setRange(0, 300)
        self.grace.setValue(10)
        self.grace.setSuffix(" с")
        form.addRow("Пауза перед відключенням:", self.grace)
        lay.addLayout(form)
        box = QDialogButtonBox(QDialogButtonBox.Cancel)
        ok = box.addButton(verb, QDialogButtonBox.AcceptRole)
        ok.setObjectName("Primary")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)


class SettingsDialog(QDialog):
    """Current configuration of the game (read from hgs-game.json) + reload / open folder."""

    def __init__(self, parent: QWidget, client: "CoreClient", g: dict, on_change: Callable[[], None]) -> None:
        super().__init__(parent)
        self.client, self.g, self.on_change = client, g, on_change
        self.setWindowTitle(f"Налаштування — {g['name']}")
        self.resize(620, 520)
        lay = QVBoxLayout(self)
        info = QLabel("Конфігурація гри береться з файлу hgs-game.json у папці гри. Змініть файл і натисніть "
                      "«Перечитати hgs-game.json» (сервер має бути зупинений). Редактор налаштувань у вікні — Етап 6.")
        info.setWordWrap(True)
        info.setObjectName("Muted")
        lay.addWidget(info)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        lay.addWidget(self.view, 1)
        self.msg = QLabel()
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)
        row = QHBoxLayout()
        folder = QPushButton("Відкрити папку гри")
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(g["root_path"])))
        self.reload = QPushButton("Перечитати hgs-game.json")
        self.reload.setEnabled(g["state"] not in ACTIVE)
        self.reload.setToolTip("" if self.reload.isEnabled() else "Спочатку зупиніть сервер")
        self.reload.clicked.connect(self._reload)
        close = QPushButton("Закрити")
        close.clicked.connect(self.accept)
        row.addWidget(folder)
        row.addWidget(self.reload)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        self._load()

    def _load(self) -> None:
        path = f"{self.g['root_path']}/hgs-game.json"
        try:
            with open(path, encoding="utf-8") as f:
                self.view.setPlainText(json.dumps(json.load(f), indent=2, ensure_ascii=False))
        except Exception as exc:
            self.view.setPlainText(f"Не вдалося прочитати {path}: {exc}")

    def _reload(self) -> None:
        def done(ok: bool, data) -> None:
            self.msg.setText("✓ Конфігурацію оновлено" if ok else f"Помилка: {data}")
            self.msg.setStyleSheet("" if ok else f"color: {RED};")
            self._load()
            self.on_change()
        self.client.call("POST", f"/games/{self.g['id']}/reload", None, done)


class StatusDialog(QDialog):
    def __init__(self, parent: QWidget, g: dict, client: "CoreClient") -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Стан — {g['name']}")
        self.resize(560, 420)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        rows = [
            ("ID гри", g["id"]), ("Стан", g["state"]), ("Адаптер", f"{g['adapter']} ({g['mode']})"),
            ("Версія гри", g["version"]), ("Режим входу", g["auth_mode"]),
            ("Гравців онлайн", str(g["players_online"])), ("Кімнат", str(g["rooms_open"])),
            ("Працює", fmt_duration(g["uptime_s"])), ("Автоперезапусків", str(g["restarts"])),
            ("Остання помилка", g["last_error"] or "—"), ("Папка гри", g["root_path"]),
            ("Адреса клієнта", g["client_url"] or "—"),
            ("Метрики адаптера", json.dumps(g["metrics"], ensure_ascii=False) if g["metrics"] else "—"),
        ]
        for k, v in rows:
            lbl = QLabel(v)
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            form.addRow(k + ":", lbl)
        self.url = QLabel("…")
        self.url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Адреса для SDK:", self.url)
        lay.addLayout(form)
        close = QPushButton("Закрити")
        close.clicked.connect(self.accept)
        lay.addWidget(close, 0, Qt.AlignRight)
        client.call("GET", "/status", None, lambda ok, st: self.url.setText(
            st["network"]["local_url"] if ok else "—"))


class PlayersDialog(QDialog):
    def __init__(self, parent: QWidget, client: "CoreClient", g: dict) -> None:
        super().__init__(parent)
        self.client, self.g = client, g
        self.setWindowTitle(f"Гравці — {g['name']}")
        self.resize(640, 380)
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Ім'я", "ID", "Кімната", "IP"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self.table, 1)
        self.msg = QLabel()
        lay.addWidget(self.msg)
        row = QHBoxLayout()
        refresh = QPushButton("Оновити")
        refresh.clicked.connect(self.load)
        self.kick = QPushButton("Відключити вибраного")
        self.kick.setObjectName("Danger")
        self.kick.clicked.connect(self._kick)
        close = QPushButton("Закрити")
        close.clicked.connect(self.accept)
        row.addWidget(refresh)
        row.addWidget(self.kick)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        self.load()

    def load(self) -> None:
        def done(ok: bool, data) -> None:
            if not ok:
                self.msg.setText(f"Помилка: {data}")
                return
            self.table.setRowCount(len(data))
            for r, p in enumerate(data):
                for c, v in enumerate((p["name"], p["player_id"], p["room_id"] or "—", p["ip"])):
                    self.table.setItem(r, c, QTableWidgetItem(v))
            self.msg.setText(f"Онлайн: {len(data)}")
            self.kick.setEnabled(bool(data))
        self.client.call("GET", f"/players?game_id={self.g['id']}", None, done)

    def _kick(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            self.msg.setText("Виберіть гравця в таблиці")
            return
        name = self.table.item(row, 0).text()
        pid = self.table.item(row, 1).text()
        if QMessageBox.question(self, "Відключити гравця", f"Відключити гравця {name}?") != QMessageBox.Yes:
            return
        self.client.call("POST", f"/players/{self.g['id']}/{pid}/kick", {"reason": "відключено адміністратором"},
                         lambda ok, d: (self.msg.setText("Гравця відключено" if ok else f"Помилка: {d}"), self.load()))


class InviteDialog(QDialog):
    def __init__(self, parent: QWidget, client: "CoreClient", g: dict) -> None:
        super().__init__(parent)
        self.client, self.g = client, g
        self.setWindowTitle(f"Запрошення — {g['name']}")
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.uses = QSpinBox()
        self.uses.setRange(1, 1000)
        self.uses.setValue(1)
        self.hours = QSpinBox()
        self.hours.setRange(1, 720)
        self.hours.setValue(168)
        self.hours.setSuffix(" год")
        self.label = QLineEdit()
        self.label.setMaxLength(64)
        self.label.setPlaceholderText("наприклад: для Олега")
        form.addRow("Кількість використань:", self.uses)
        form.addRow("Діє протягом:", self.hours)
        form.addRow("Мітка:", self.label)
        lay.addLayout(form)
        self.code = QLineEdit()
        self.code.setReadOnly(True)
        self.code.setPlaceholderText("Код з'явиться тут")
        self.code.setStyleSheet("font-size: 16pt; font-family: Consolas, monospace;")
        lay.addWidget(self.code)
        note = QLabel("Код показується лише один раз — HGS зберігає тільки його хеш. Передайте код гравцю; "
                      "посилання для гри через інтернет з'явиться на Етапі 4.")
        note.setWordWrap(True)
        note.setObjectName("Muted")
        lay.addWidget(note)
        row = QHBoxLayout()
        create = QPushButton("Створити код")
        create.setObjectName("Primary")
        create.clicked.connect(self._create)
        self.copy = QPushButton("Копіювати")
        self.copy.setEnabled(False)
        self.copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.code.text()))
        close = QPushButton("Закрити")
        close.clicked.connect(self.accept)
        row.addWidget(create)
        row.addWidget(self.copy)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)

    def _create(self) -> None:
        body = {"game_id": self.g["id"], "max_uses": self.uses.value(), "ttl_s": self.hours.value() * 3600,
                "label": self.label.text() or None}

        def done(ok: bool, data) -> None:
            if ok:
                self.code.setText(data["code"])
                self.copy.setEnabled(True)
            else:
                QMessageBox.critical(self, "Помилка", str(data))
        self.client.call("POST", "/invites", body, done)
