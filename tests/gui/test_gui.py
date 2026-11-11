"""GUI smoke tests: the real window drives a real core process (offscreen Qt)."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QElapsedTimer, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from tests.conftest import make_game_dir  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    from hgs.gui.theme.dark import apply_theme

    apply_theme(app)
    yield app


def wait_until(app, cond, timeout=20.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


class StallMeter:
    """Measures the longest gap between event-loop ticks (a frozen GUI = long gap)."""

    def __init__(self) -> None:
        self.max_gap_ms = 0
        self._t = QElapsedTimer()
        self._t.start()
        self._timer = QTimer()
        self._timer.setInterval(10)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        self.max_gap_ms = max(self.max_gap_ms, self._t.restart())

    def stop(self) -> None:
        self._timer.stop()


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    from hgs.gui.main_window import MainWindow

    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text(json.dumps({"admin": {"port": 0}, "gateway": {"port": 0}}))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    w = MainWindow(data)
    w.show()
    wait_until(qapp, lambda: w._core_ready and w.client.events_connected, what="core ready")
    yield w
    w.close()  # goes through the normal exit path (confirmation auto-accepted)
    wait_until(qapp, lambda: not w.launcher.is_running(), timeout=20, what="core stop")
    w.deleteLater()


def test_full_flow_add_start_player_stop(qapp, window, tmp_path):
    w = window
    meter = StallMeter()
    w.client.call("POST", "/games", {"path": str(make_game_dir(tmp_path, "demo"))})
    wait_until(qapp, lambda: "demo" in w.games.cards, what="game card")
    card = w.games.cards["demo"]
    assert card.buttons["start"].isEnabled() and not card.buttons["stop"].isEnabled()

    card.buttons["start"].click()
    wait_until(qapp, lambda: card.game["state"] == "running", what="running state")
    assert card.badge.text().endswith("ПРАЦЮЄ")
    assert not card.buttons["start"].isEnabled() and card.buttons["stop"].isEnabled()

    # a real player connects from another thread; the card must update via the event stream
    from websockets.sync.client import connect

    port = w.launcher.gateway_port
    stop_player = threading.Event()

    def player():
        with connect(f"ws://127.0.0.1:{port}/hgs/v1/ws") as ws:
            ws.send(json.dumps({"v": 1, "t": "hello", "seq": 1,
                                "d": {"game": "demo", "auth": {"type": "open", "name": "Ann"}}}))
            ws.recv()
            stop_player.wait(15)

    t = threading.Thread(target=player, daemon=True)
    t.start()
    wait_until(qapp, lambda: card.game["players_online"] == 1, what="player count on card")
    wait_until(qapp, lambda: w.dashboard.players.value.text() == "1", what="dashboard players")
    assert any("Ann" in w.dashboard.events.item(i).text() for i in range(w.dashboard.events.count()))
    stop_player.set()
    t.join(5)
    wait_until(qapp, lambda: card.game["players_online"] == 0, what="player left")

    card.buttons["stop"].click()  # confirmation is auto-accepted in this test
    wait_until(qapp, lambda: card.game["state"] == "stopped", what="stopped state")

    # logs page shows the game's log
    card.buttons["logs"].click()
    wait_until(qapp, lambda: "relay backend" in w.logs.view.toPlainText(), what="game log")

    meter.stop()
    assert meter.max_gap_ms < 1000, f"GUI event loop stalled for {meter.max_gap_ms} ms"

    if os.environ.get("HGS_SCREENSHOT_DIR"):
        w.resize(1180, 760)
        w.nav.setCurrentRow(1)
        card.buttons["start"].click()
        wait_until(qapp, lambda: card.game["state"] == "running", what="running again")
        for i, name in ((0, "dashboard"), (1, "games"), (2, "logs")):
            w.nav.setCurrentRow(i)
            for _ in range(30):
                qapp.processEvents()
                time.sleep(0.02)
            w.grab().save(os.path.join(os.environ["HGS_SCREENSHOT_DIR"], f"{name}.png"))


def test_core_crash_shows_banner_and_restart_works(qapp, window):
    w = window
    w.launcher.proc.kill()
    wait_until(qapp, lambda: w.banner.isVisible(), what="error banner")
    assert "неочікувано" in w.banner.label.text() or "unexpectedly" in w.banner.label.text()
    assert w.dashboard.core.value.text() == "Зупинено"
    w.banner.action.click()
    wait_until(qapp, lambda: w._core_ready and w.client.events_connected, what="core restarted")
    assert not w.banner.isVisible()
