"""Main window: sidebar navigation, pages, core state banner, status bar."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QMainWindow, QMenu, QMessageBox, QStackedWidget, QSystemTrayIcon, QVBoxLayout,
    QWidget,
)

from hgs import __version__
from hgs.gui.core_client import CoreClient
from hgs.gui.core_launcher import CoreLauncher
from hgs.gui.pages.dashboard import DashboardPage
from hgs.gui.pages.games import GamesPage
from hgs.gui.pages.logs import LogsPage
from hgs.gui.pages.network import NetworkPage
from hgs.gui.pages.players import PlayersPage
from hgs.gui.pages.rooms import RoomsPage
from hgs.gui.pages.settings import SettingsPage, gui_prefs
from hgs.gui.widgets.common import Banner

# Events after which the game list is re-read (debounced).
GAME_EVENTS = ("server.state_changed", "game.", "player.", "room.", "network.")


def app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#4f8cff"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setPen(QColor("white"))
    f = QFont()
    f.setBold(True)
    f.setPixelSize(30)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "H")
    p.end()
    return QIcon(pm)


class MainWindow(QMainWindow):
    def __init__(self, data_dir: Path) -> None:
        super().__init__()
        self.setWindowTitle("HOME GAME SERVER")
        self.setWindowIcon(app_icon())
        self.resize(1180, 760)
        self.data_dir = data_dir
        self._quitting = False
        self._core_ready = False

        self.launcher = CoreLauncher(data_dir, self)
        self.client = CoreClient(self)
        self.launcher.ready.connect(self._on_core_ready)
        self.launcher.failed.connect(self._on_core_failed)
        self.launcher.stopped.connect(self._on_core_stopped)
        self.client.event.connect(self._on_event)
        self.client.connected_changed.connect(self._on_events_connection)

        central = QWidget()
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(200)
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(0, 0, 0, 12)
        app_title = QLabel("HOME GAME\nSERVER")
        app_title.setObjectName("AppTitle")
        sl.addWidget(app_title)
        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        sl.addWidget(self.nav, 1)
        self.core_label = QLabel("● Ядро: запуск…")
        self.core_label.setContentsMargins(14, 0, 14, 0)
        self.core_label.setWordWrap(True)
        sl.addWidget(self.core_label)
        version = QLabel(f"v{__version__}")
        version.setObjectName("Muted")
        version.setContentsMargins(14, 4, 14, 0)
        sl.addWidget(version)
        outer.addWidget(sidebar)

        right = QWidget()
        right.setObjectName("Page")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(16, 12, 16, 0)
        self.banner = Banner()
        self.banner.action.clicked.connect(self._banner_action)
        self._stale_core = False
        rl.addWidget(self.banner)
        self.info_banner = Banner("info")
        rl.addWidget(self.info_banner)
        self.stack = QStackedWidget()
        rl.addWidget(self.stack, 1)
        outer.addWidget(right, 1)
        self.setCentralWidget(central)

        self.dashboard = DashboardPage()
        self.games = GamesPage(self.client, data_dir=str(data_dir))
        self.players = PlayersPage(self.client)
        self.rooms = RoomsPage(self.client)
        self.network = NetworkPage(self.client)
        self.logs = LogsPage(self.client, str(data_dir / "logs"))
        self.settings_page = SettingsPage(self.client)
        self.games.open_logs.connect(self.show_logs)
        self.pages = (("Dashboard", self.dashboard), ("Games", self.games), ("Players", self.players),
                      ("Rooms", self.rooms), ("Network", self.network), ("Logs", self.logs),
                      ("Settings", self.settings_page))
        for name, page in self.pages:
            self.nav.addItem(name)
            self.stack.addWidget(page)
        self.nav.currentRowChanged.connect(self._on_nav)
        self.nav.setCurrentRow(0)

        self.status_text = QLabel("Запуск серверного ядра…")
        self.statusBar().addWidget(self.status_text, 1)

        self._games_refresh = QTimer(self)
        self._games_refresh.setSingleShot(True)
        self._games_refresh.setInterval(250)
        self._games_refresh.timeout.connect(self._refresh_all)
        self._uptime = QTimer(self)  # keeps uptime counters fresh
        self._uptime.setInterval(5000)
        self._uptime.timeout.connect(self._refresh_all)

        self.tray: QSystemTrayIcon | None = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(app_icon(), self)
            self.tray.setToolTip("HOME GAME SERVER")
            menu = QMenu(self)
            show = QAction("Показати", self)
            show.triggered.connect(self.show_from_tray)
            quit_ = QAction("Вийти", self)
            quit_.triggered.connect(self.quit_from_tray)
            menu.addAction(show)
            menu.addAction(quit_)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(lambda reason: self.show_from_tray()
                                        if reason == QSystemTrayIcon.Trigger else None)
            self.tray.show()
        self._force_quit = False

        self.launcher.start()

    # -- core lifecycle -------------------------------------------------------------
    def _banner_action(self) -> None:
        if self._stale_core:
            from hgs.common.platform import stop_stale_core

            stop_stale_core(self.data_dir)
            self._stale_core = False
        self.restart_core()

    def restart_core(self) -> None:
        self.banner.hide()
        self.core_label.setText("● Ядро: запуск…")
        self.dashboard.set_core_state("starting")
        self.launcher.start()

    def _on_core_ready(self, admin_port: int, gateway_port: int) -> None:
        self._core_ready = True
        self.client.configure(admin_port, self.launcher.token)
        self.core_label.setText("● Ядро: працює")
        self.core_label.setStyleSheet("color: #3ecf8e;")
        self._refresh_all()
        self._uptime.start()
        self.logs.load_sources()

    def _on_core_failed(self, message: str) -> None:
        self._core_ready = False
        self._uptime.stop()
        self.client.disconnect_core()
        self.core_label.setText("● Ядро: зупинено")
        self.core_label.setStyleSheet("color: #ff5c5c;")
        self.dashboard.set_core_state("stopped", message)
        details = self.launcher.last_stderr()
        text = message + (f"\n\nОстанні рядки:\n{details[-800:]}" if details and details not in message else "")
        text += f"\n\nЛог ядра: {self.data_dir / 'logs' / 'core.log'}"
        self._stale_core = "Another HGS instance" in message
        if self._stale_core:
            text = ("Попередня копія серверного ядра HGS ще працює у фоні (наприклад, після оновлення або якщо HGS "
                    "відкрили двічі). Її можна завершити — ігрові сервери в ній буде зупинено.")
        self.banner.show_message(text, "Завершити старе ядро і запустити знову" if self._stale_core
                                 else "Перезапустити ядро", closable=False)
        self.status_text.setText("Серверне ядро не працює")

    def _on_core_stopped(self) -> None:
        self._core_ready = False
        if self._quitting:
            self.close()

    def _on_events_connection(self, connected: bool) -> None:
        if connected:
            self._refresh_all()

    # -- events ---------------------------------------------------------------------
    def _on_event(self, ev: dict) -> None:
        t = ev.get("type", "")
        if t == "metrics.tick":
            self.dashboard.update_metrics(ev["data"])
            return
        if t == "server.log":
            self.logs.on_log_event(ev["data"])
            return
        if t == "core.hello":
            self.dashboard.update_status(ev["data"])
            for w in ev["data"].get("warnings") or []:
                self.banner.show_message(w)
            if ev["data"].get("previous_unclean_shutdown"):
                self.info_banner.show_message(
                    "Минулого разу HGS завершився некоректно (збій або вимкнення ПК). Ігрові сервери не запускаються "
                    "автоматично — перевірте ігри й запустіть потрібні вручну.")
            return
        self.dashboard.add_event(ev)
        if t.startswith(GAME_EVENTS):
            self._games_refresh.start()

    def _refresh_all(self) -> None:
        if not self._core_ready:
            return
        self.games.refresh()
        self.client.call("GET", "/status", None, self._on_status)
        self.client.call("GET", "/games", None, lambda ok, d: ok and self.dashboard.update_games(d))
        current = self.stack.currentWidget()
        if current in (self.network, self.players, self.rooms):
            current.refresh()

    def _on_status(self, ok: bool, st) -> None:
        if not ok:
            return
        self.dashboard.update_status(st)
        net = st["network"]
        self.status_text.setText(
            f"Серверів працює: {st['games_running']} · гравців онлайн: {st['players_online']} · "
            f"шлюз: {net['bind']}:{net['gateway_port']}")

    # -- navigation -----------------------------------------------------------------
    def _on_nav(self, row: int) -> None:
        self.stack.setCurrentIndex(row)
        if self.stack.currentWidget() is self.logs and self._core_ready and self.logs.source.count() == 0:
            self.logs.load_sources()
        if self.stack.currentWidget() in (self.network, self.players, self.rooms, self.settings_page) and self._core_ready:
            self.stack.currentWidget().refresh()

    def show_logs(self, source: str) -> None:
        self.nav.setCurrentRow(self.stack.indexOf(self.logs))
        self.logs.show_source(source)

    # -- closing --------------------------------------------------------------------
    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_from_tray(self) -> None:
        self._force_quit = True
        self.show_from_tray()
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if not self.launcher.is_running():
            if self.tray:
                self.tray.hide()
            event.accept()
            return
        running = [c.game["name"] for c in self.games.cards.values() if c.game["state"] == "running"]
        if (running and not self._quitting and not self._force_quit and self.tray is not None
                and gui_prefs().value("minimize_to_tray", True, type=bool)):
            self.hide()
            self.tray.showMessage("HOME GAME SERVER", "Сервери працюють далі. Вийти можна з меню значка в треї.",
                                  QSystemTrayIcon.Information, 4000)
            event.ignore()
            return
        if not self._quitting:
            if running:
                players = sum(c.game["players_online"] for c in self.games.cards.values())
                answer = QMessageBox.question(
                    self, "Вийти з HGS",
                    f"Працюють сервери: {', '.join(running)} (гравців онлайн: {players}).\n\n"
                    "Сервери буде коректно зупинено, а гравців відключено. Вийти?")
                if answer != QMessageBox.Yes:
                    event.ignore()
                    return
            self._quitting = True
            self.status_text.setText("Зупинка серверів і ядра…")
            self.setEnabled(False)
            self.client.disconnect_core()
            self.launcher.stop()
        event.ignore()  # the window closes when the core reports it has stopped
