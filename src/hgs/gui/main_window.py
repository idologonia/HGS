"""Main window: sidebar navigation, pages, core state banner, status bar."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QMainWindow, QMessageBox, QStackedWidget, QVBoxLayout, QWidget,
)

from hgs import __version__
from hgs.gui.core_client import CoreClient
from hgs.gui.core_launcher import CoreLauncher
from hgs.gui.pages.dashboard import DashboardPage
from hgs.gui.pages.games import GamesPage
from hgs.gui.pages.logs import LogsPage
from hgs.gui.pages.network import NetworkPage
from hgs.gui.widgets.common import Banner

# Events after which the game list is re-read (debounced).
GAME_EVENTS = ("server.state_changed", "game.", "player.", "room.", "network.")


class MainWindow(QMainWindow):
    def __init__(self, data_dir: Path) -> None:
        super().__init__()
        self.setWindowTitle("HOME GAME SERVER")
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
        self.banner.action.clicked.connect(self.restart_core)
        rl.addWidget(self.banner)
        self.info_banner = Banner("info")
        rl.addWidget(self.info_banner)
        self.stack = QStackedWidget()
        rl.addWidget(self.stack, 1)
        outer.addWidget(right, 1)
        self.setCentralWidget(central)

        self.dashboard = DashboardPage()
        self.games = GamesPage(self.client)
        self.logs = LogsPage(self.client, str(data_dir / "logs"))
        self.network = NetworkPage(self.client)
        self.games.open_logs.connect(self.show_logs)
        for name, page in (("Dashboard", self.dashboard), ("Games", self.games), ("Network", self.network),
                           ("Logs", self.logs)):
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

        self.launcher.start()

    # -- core lifecycle -------------------------------------------------------------
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
        self.banner.show_message(text, "Перезапустити ядро", closable=False)
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
        if self.stack.currentWidget() is self.network:
            self.network.refresh()

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
        if self.stack.currentWidget() is self.network and self._core_ready:
            self.network.refresh()

    def show_logs(self, source: str) -> None:
        self.nav.setCurrentRow(self.stack.indexOf(self.logs))
        self.logs.show_source(source)

    # -- closing --------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if not self.launcher.is_running():
            event.accept()
            return
        if not self._quitting:
            running = [c.game["name"] for c in self.games.cards.values() if c.game["state"] == "running"]
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
