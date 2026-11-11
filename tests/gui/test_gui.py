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
    (data / "settings.json").write_text(json.dumps({"admin": {"port": 0}, "gateway": {"port": 0}, "worker_bus": {"port": 0}}))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    w = MainWindow(data)
    w.show()
    wait_until(qapp, lambda: w._core_ready and w.client.events_connected, what="core ready")
    yield w
    w._force_quit = True  # do not minimize to the tray (real desktops in CI have one)
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
        for i, (name, _page) in enumerate(w.pages):
            name = name.lower()
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


def test_gui_stays_responsive_under_load(qapp, window):
    """100+ game messages per second and a live log stream must not freeze the window."""
    from pathlib import Path

    from websockets.sync.client import connect

    w = window
    root = Path(__file__).resolve().parents[2] / "examples" / "demo-arena" / "logic"
    w.client.call("POST", "/games", {"path": str(root)})
    wait_until(qapp, lambda: "demo-arena-logic" in w.games.cards, what="game card")
    card = w.games.cards["demo-arena-logic"]
    card.buttons["start"].click()
    wait_until(qapp, lambda: card.game["state"] == "running", what="running")
    card.buttons["logs"].click()  # live log view of the game
    wait_until(qapp, lambda: "logic backend ready" in w.logs.view.toPlainText(), what="game log")

    port = w.launcher.gateway_port
    done = threading.Event()
    sent = [0]

    def spam(name: str) -> None:
        with connect(f"ws://127.0.0.1:{port}/hgs/v1/ws", max_size=None) as ws:
            seq = 1
            ws.send(json.dumps({"v": 1, "t": "hello", "seq": seq,
                                "d": {"game": "demo-arena-logic", "auth": {"type": "open", "name": name}}}))
            ws.recv()
            seq += 1
            ws.send(json.dumps({"v": 1, "t": "room.join", "seq": seq, "id": "j", "d": {"room_id": "lobby"}}))
            end = time.monotonic() + 3
            while time.monotonic() < end:
                seq += 1
                ws.send(json.dumps({"v": 1, "t": "game", "seq": seq,
                                    "d": {"e": "input", "d": {"dx": seq % 3 - 1, "dy": 0}}}))
                sent[0] += 1
                time.sleep(0.04)  # 25 msg/s per player, under the per-player limit
                try:
                    while True:
                        ws.recv(timeout=0)  # drain game_state updates
                except TimeoutError:
                    pass

    threads = [threading.Thread(target=spam, args=(f"Bot{i}",), daemon=True) for i in range(5)]
    meter = StallMeter()
    for t in threads:
        t.start()
    wait_until(qapp, lambda: card.game["players_online"] == 5, what="5 players")
    wait_until(qapp, lambda: not any(t.is_alive() for t in threads), timeout=30, what="load finished")
    meter.stop()
    done.set()
    assert sent[0] >= 300, sent[0]
    assert meter.max_gap_ms < 500, f"GUI event loop stalled for {meter.max_gap_ms} ms"
    wait_until(qapp, lambda: "Bot4 joined" in w.logs.view.toPlainText(), what="live log lines")
    wait_until(qapp, lambda: "процес гри" in card.stats.text(), what="worker metrics on the card")


def test_network_page_switches_mode(qapp, window):
    w = window
    w.nav.setCurrentRow(w.stack.indexOf(w.network))
    wait_until(qapp, lambda: w.network.view is not None, what="network view")
    assert w.network.mode_buttons["local"].isChecked()
    assert w.network.player_url.text().startswith("ws://127.0.0.1:")
    assert not w.network.tunnel_start.isEnabled()  # not in tunnel mode
    w.network.mode_buttons["lan"].setChecked(True)
    w.network.apply_btn.click()  # confirmations are auto-accepted in tests
    wait_until(qapp, lambda: w.network.view["bind"] == "0.0.0.0", what="LAN mode applied")
    assert w.network.warnings.isVisible() and "LAN" in w.network.warnings.text()
    w.network.mode_buttons["direct"].setChecked(True)
    w.network.apply_btn.click()
    wait_until(qapp, lambda: w.network.banner.isVisible(), what="error for direct mode without TLS")
    assert "certificate" in w.network.banner.label.text()
    assert w.network.view["settings"]["mode"] == "lan"  # unchanged after the refused change


def test_add_python_project_without_manifest_and_edit_settings(qapp, window, tmp_path):
    import shutil
    from pathlib import Path

    from hgs.gui.pages.add_game import AddGameDialog
    from hgs.gui.pages.games import SettingsDialog

    w = window
    folder = tmp_path / "My Chat"
    shutil.copytree(Path(__file__).resolve().parents[2] / "examples" / "python-standalone" / "server", folder / "server")
    dlg = AddGameDialog(w, str(folder))
    assert dlg.gid.text() == "my-chat" and dlg.entry.text() == "server/main.py"
    dlg.kind.setCurrentIndex(dlg.kind.findData("python-standalone"))
    dlg.auth.setCurrentIndex(dlg.auth.findData("open"))
    manifest = dlg.manifest()
    assert manifest["backend"] == {"mode": "standalone", "entry": "server/main.py", "python": "auto",
                                   "args": ["--port", "{port}"], "port": "auto"}
    w.client.call("POST", "/games", {"path": str(folder), "manifest": manifest})
    wait_until(qapp, lambda: "my-chat" in w.games.cards, what="card")
    card = w.games.cards["my-chat"]
    card.buttons["start"].click()
    wait_until(qapp, lambda: card.game["state"] == "running", what="standalone running")
    assert not (folder / "hgs-game.json").exists()
    assert "PID" in card.stats.text()

    from websockets.sync.client import connect

    with connect(f"ws://127.0.0.1:{w.launcher.gateway_port}{card.game['proxy_path']}") as ws:
        ws.send("hi from the GUI test")
        assert json.loads(ws.recv(timeout=5)) == {"chat": "hi from the GUI test"}

    card.buttons["stop"].click()
    wait_until(qapp, lambda: card.game["state"] == "stopped", what="stopped")
    sd = SettingsDialog(w, w.client, card.game, w.games.refresh)
    wait_until(qapp, lambda: '"my-chat"' in sd.view.toPlainText(), what="manifest loaded")
    sd.view.setPlainText(sd.view.toPlainText().replace('"name": "My Chat"', '"name": "Renamed Chat"'))
    sd.save.click()
    wait_until(qapp, lambda: sd.msg.text().startswith("✓"), what="saved")
    wait_until(qapp, lambda: card.game["name"] == "Renamed Chat", what="card renamed")
    sd.view.setPlainText("{ not json")
    sd.save.click()
    assert "некоректний JSON" in sd.msg.text()
    sd.close()


def _connect_player(port: int, game: str, name: str, room: str = "lobby"):
    from websockets.sync.client import connect

    ws = connect(f"ws://127.0.0.1:{port}/hgs/v1/ws").__enter__()
    ws.send(json.dumps({"v": 1, "t": "hello", "seq": 1, "d": {"game": game, "auth": {"type": "open", "name": name}}}))
    ws.recv()
    ws.send(json.dumps({"v": 1, "t": "room.join", "seq": 2, "id": "j", "d": {"room_id": room}}))
    return ws


def test_players_rooms_settings_and_saves_pages(qapp, window, tmp_path):
    from hgs.gui.pages.saves import SavesDialog
    from hgs.gui.widgets.table import selected_key

    w = window
    root = __import__("pathlib").Path(__file__).resolve().parents[2] / "examples" / "demo-arena" / "logic"
    w.client.call("POST", "/games", {"path": str(root)})
    wait_until(qapp, lambda: "demo-arena-logic" in w.games.cards, what="card")
    card = w.games.cards["demo-arena-logic"]
    card.buttons["start"].click()
    wait_until(qapp, lambda: card.game["state"] == "running", what="running")
    ann = _connect_player(w.launcher.gateway_port, "demo-arena-logic", "Ann")

    # Dashboard: per-game resources
    wait_until(qapp, lambda: w.dashboard.servers_table.rowCount() == 1
               and w.dashboard.servers_table.item(0, 2).text() == "1", what="dashboard servers table")
    assert w.dashboard.servers_table.item(0, 5).text() != "—"  # worker RAM is measured

    # Rooms: create, lock, close
    w.nav.setCurrentRow(w.stack.indexOf(w.rooms))
    wait_until(qapp, lambda: w.rooms.table.rowCount() == 1, what="lobby listed")
    w.client.call("POST", "/rooms", {"game_id": "demo-arena-logic", "name": "Arena 2", "max_players": 4})
    wait_until(qapp, lambda: w.rooms.table.rowCount() == 2, what="new room listed")
    row = next(r for r in range(2) if w.rooms.table.item(r, 0).text() == "Arena 2")
    w.rooms.table.selectRow(row)
    w.rooms.lock_btn.click()
    wait_until(qapp, lambda: any(r["status"] == "locked" for r in w.rooms.rooms), what="room locked")
    w.rooms.table.selectRow(next(r for r in range(2) if w.rooms.table.item(r, 0).text() == "Arena 2"))
    w.rooms.close_btn.click()
    wait_until(qapp, lambda: w.rooms.table.rowCount() == 1, what="room closed")
    w.rooms.table.selectRow(0)
    wait_until(qapp, lambda: w.rooms.players.count() == 1, what="players in lobby")
    assert "Ann" in w.rooms.players.item(0).text()

    # Players: online list and kick
    w.nav.setCurrentRow(w.stack.indexOf(w.players))
    wait_until(qapp, lambda: w.players.online.rowCount() == 1, what="online player")
    wait_until(qapp, lambda: w.players.known.rowCount() == 1, what="known player")
    w.players.online.selectRow(0)
    from PySide6.QtWidgets import QInputDialog
    orig = QInputDialog.getText
    QInputDialog.getText = staticmethod(lambda *a, **k: ("test kick", True))
    try:
        w.players.kick_btn.click()
    finally:
        QInputDialog.getText = orig
    wait_until(qapp, lambda: w.players.online.rowCount() == 0, what="player kicked")
    ann.close()

    # Settings: rename the server
    w.nav.setCurrentRow(w.stack.indexOf(w.settings_page))
    wait_until(qapp, lambda: w.settings_page.data is not None, what="settings loaded")
    w.settings_page.name.setText("Friday Games")
    w.settings_page.save()
    wait_until(qapp, lambda: w.settings_page.data["server_name"] == "Friday Games", what="settings saved")

    # Saves: snapshot now, backup, restore (stopped)
    dlg = SavesDialog(w, w.client, card.game, str(w.data_dir))
    dlg.save_btn.click()
    wait_until(qapp, lambda: dlg.snapshots.rowCount() >= 1, what="snapshot listed")
    card.buttons["stop"].click()
    wait_until(qapp, lambda: card.game["state"] == "stopped", what="stopped")
    dlg2 = SavesDialog(w, w.client, card.game, str(w.data_dir))
    wait_until(qapp, lambda: dlg2.snapshots.rowCount() >= 2, what="stop snapshot listed")
    dlg2.snapshots.selectRow(dlg2.snapshots.rowCount() - 1)
    oldest = selected_key(dlg2.snapshots)
    dlg2.restore_btn.click()
    wait_until(qapp, lambda: dlg2.msg.text().startswith("✓ Відновлено"), what="restored")
    wait_until(qapp, lambda: dlg2.backups.rowCount() == 1, what="before_restore backup")
    assert "before_restore" in dlg2.backups.item(0, 0).text()
    assert "restore" in dlg2.snapshots.item(0, 0).text() and oldest
    dlg.close()
    dlg2.close()
