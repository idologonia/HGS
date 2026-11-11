"""Players page: who is online (kick) and known players of a game (ban, unban, revoke tokens)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import QComboBox, QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from hgs.gui.widgets.common import Banner
from hgs.gui.widgets.table import ago, fill, make_table, selected_key

if TYPE_CHECKING:
    from hgs.gui.core_client import CoreClient

AUTH_NAMES = {"invite": "запрошення", "password": "пароль гри", "account": "акаунт", "open": "без пароля"}


class PlayersPage(QWidget):
    def __init__(self, client: "CoreClient", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self.setObjectName("Page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Players")
        title.setObjectName("PageTitle")
        lay.addWidget(title)
        self.banner = Banner()
        lay.addWidget(self.banner)

        head = QLabel("Онлайн зараз")
        head.setObjectName("CardTitle")
        lay.addWidget(head)
        self.online = make_table(["Ім'я", "Гра", "Кімната", "IP", "Підключився", "Активність"])
        lay.addWidget(self.online, 1)
        row = QHBoxLayout()
        self.kick_btn = QPushButton("Відключити")
        self.kick_btn.setObjectName("Danger")
        self.kick_btn.clicked.connect(self.kick)
        row.addWidget(self.kick_btn)
        row.addStretch(1)
        lay.addLayout(row)

        head2 = QHBoxLayout()
        h = QLabel("Усі гравці гри")
        h.setObjectName("CardTitle")
        self.game = QComboBox()
        self.game.setMinimumWidth(220)
        self.game.currentIndexChanged.connect(self.load_known)
        head2.addWidget(h)
        head2.addWidget(self.game)
        head2.addStretch(1)
        lay.addLayout(head2)
        self.known = make_table(["Ім'я", "Вхід", "Статус", "Остання активність", "Бан"])
        lay.addWidget(self.known, 1)
        row2 = QHBoxLayout()
        self.ban_btn = QPushButton("Заблокувати…")
        self.ban_btn.setObjectName("Danger")
        self.ban_btn.clicked.connect(self.ban)
        self.unban_btn = QPushButton("Розблокувати")
        self.unban_btn.clicked.connect(lambda: self._known_action("unban", "Розблокувати гравця?"))
        self.revoke_btn = QPushButton("Скасувати збережені входи")
        self.revoke_btn.setToolTip("Гравцю доведеться увійти знову (кодом, паролем або акаунтом)")
        self.revoke_btn.clicked.connect(lambda: self._known_action("revoke-tokens", "Скасувати збережені входи гравця?"))
        for b in (self.ban_btn, self.unban_btn, self.revoke_btn):
            row2.addWidget(b)
        row2.addStretch(1)
        lay.addLayout(row2)

    def refresh(self) -> None:
        self.client.call("GET", "/players", None, self._on_online)
        self.client.call("GET", "/games", None, self._on_games)

    def _on_online(self, ok: bool, data: Any) -> None:
        if not ok:
            self.banner.show_message(f"Не вдалося отримати гравців: {data}")
            return
        fill(self.online, [[p["name"], p["game_id"], p["room_id"] or "—", p["ip"], ago(p["connected_at"]),
                            ago(p["last_activity"])] for p in data],
             keys=[(p["game_id"], p["player_id"], p["name"]) for p in data])
        self.kick_btn.setEnabled(bool(data))

    def _on_games(self, ok: bool, data: Any) -> None:
        if not ok:
            return
        current = self.game.currentData()
        self.game.blockSignals(True)
        self.game.clear()
        for g in data:
            self.game.addItem(g["name"], g["id"])
        idx = self.game.findData(current)
        self.game.setCurrentIndex(idx if idx >= 0 else 0)
        self.game.blockSignals(False)
        self.load_known()

    def load_known(self) -> None:
        gid = self.game.currentData()
        if not gid:
            fill(self.known, [])
            return

        def done(ok: bool, data: Any) -> None:
            if not ok:
                self.banner.show_message(f"Не вдалося отримати гравців гри: {data}")
                return
            fill(self.known, [[p["name"], AUTH_NAMES.get(p["auth_method"], p["auth_method"]),
                               "онлайн" if p["online"] else "офлайн", ago(p["last_seen_at"]),
                               ("так: " + (p["ban_reason"] or "")) if p["banned"] else "—"] for p in data],
                 keys=[(gid, p["player_id"], p["name"]) for p in data])
        self.client.call("GET", f"/players/known?game_id={gid}", None, done)

    def _done(self, ok: bool, data: Any) -> None:
        if ok:
            self.banner.hide()
        else:
            self.banner.show_message(str(data))
        self.refresh()

    def kick(self) -> None:
        key = selected_key(self.online)
        if key is None:
            self.banner.show_message("Виберіть гравця в таблиці «Онлайн зараз»")
            return
        gid, pid, name = key
        reason, ok = QInputDialog.getText(self, "Відключити гравця", f"Причина для {name}:", text="відключено адміністратором")
        if ok:
            self.client.call("POST", f"/players/{gid}/{pid}/kick", {"reason": reason or "kicked"}, self._done)

    def ban(self) -> None:
        key = selected_key(self.known)
        if key is None:
            self.banner.show_message("Виберіть гравця в таблиці «Усі гравці гри»")
            return
        gid, pid, name = key
        reason, ok = QInputDialog.getText(self, "Заблокувати гравця",
                                          f"{name} буде відключено, збережені входи скасовано. Причина:")
        if ok:
            self.client.call("POST", f"/players/{gid}/{pid}/ban", {"reason": reason or "banned"}, self._done)

    def _known_action(self, action: str, question: str) -> None:
        key = selected_key(self.known)
        if key is None:
            self.banner.show_message("Виберіть гравця в таблиці «Усі гравці гри»")
            return
        gid, pid, name = key
        if QMessageBox.question(self, "Гравець", f"{question}\n\n{name}") == QMessageBox.Yes:
            self.client.call("POST", f"/players/{gid}/{pid}/{action}", None, self._done)
