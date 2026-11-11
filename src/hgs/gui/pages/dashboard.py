"""Dashboard: core state, servers, players, CPU/RAM/network, network access, recent events."""

from __future__ import annotations

import time

from PySide6.QtWidgets import QGridLayout, QLabel, QListWidget, QVBoxLayout, QWidget

from hgs.gui.theme.dark import GREEN, MUTED, RED, YELLOW
from hgs.gui.widgets.common import STATE_NAMES, Card, Sparkline, StatCard, fmt_bytes, fmt_duration

EVENT_TEXT = {
    "core.ready": "Ядро запущено",
    "game.added": "Гру додано: {game[name]}",
    "game.removed": "Гру видалено з реєстру: {game_id}",
    "game.updated": "Конфігурацію оновлено: {game[name]}",
    "server.state_changed": "{game_id}: стан → {state}",
    "player.connected": "{game_id}: гравець {player[name]} підключився",
    "player.disconnected": "{game_id}: гравець {player[name]} відключився",
    "room.created": "{game_id}: створено кімнату {room[name]}",
    "room.closed": "{game_id}: кімнату {room_id} закрито ({reason})",
    "security.alert": "⚠ Безпека: {kind} (ip {ip})",
}


def describe_event(ev: dict) -> str | None:
    tmpl = EVENT_TEXT.get(ev.get("type", ""))
    if tmpl is None:
        return None
    try:
        text = tmpl.format(**ev.get("data", {}))
    except (KeyError, IndexError, TypeError):
        text = ev["type"]
    if ev.get("type") == "server.state_changed":
        text = text.replace(f"→ {ev['data'].get('state')}", "→ " + STATE_NAMES.get(ev["data"].get("state", ""), "?").lower())
    if ev.get("type") == "server.state_changed" and ev["data"].get("error"):
        text += f" — {ev['data']['error']}"
    ts = time.strftime("%H:%M:%S", time.localtime(ev.get("ts") or time.time()))
    return f"{ts}  {text}"


class DashboardPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)
        title = QLabel("Dashboard")
        title.setObjectName("PageTitle")
        root.addWidget(title)

        grid = QGridLayout()
        grid.setSpacing(12)
        self.core = StatCard("Серверне ядро")
        self.servers = StatCard("Активні сервери")
        self.players = StatCard("Гравці онлайн")
        self.cpu = StatCard("Процесор (CPU)")
        self.ram = StatCard("Пам'ять (RAM)")
        self.access = StatCard("Мережевий доступ")
        for i, w in enumerate((self.core, self.servers, self.players, self.cpu, self.ram, self.access)):
            grid.addWidget(w, i // 3, i % 3)
        root.addLayout(grid)

        net = Card()
        nl = QVBoxLayout(net)
        nl.setContentsMargins(16, 12, 16, 12)
        self.net_label = QLabel("Мережа: ↓ —  ↑ —")
        self.net_label.setObjectName("Muted")
        self.spark = Sparkline()
        nl.addWidget(self.net_label)
        nl.addWidget(self.spark)
        root.addWidget(net)

        srv_title = QLabel("Ігрові сервери: ресурси")
        srv_title.setObjectName("CardTitle")
        root.addWidget(srv_title)
        from hgs.gui.widgets.table import make_table

        self.servers_table = make_table(["Гра", "Стан", "Гравці", "Кімнати", "CPU", "RAM", "Працює"])
        self.servers_table.setMaximumHeight(170)
        root.addWidget(self.servers_table)

        ev_title = QLabel("Останні події")
        ev_title.setObjectName("CardTitle")
        root.addWidget(ev_title)
        self.events = QListWidget()
        root.addWidget(self.events, 1)
        self.set_core_state("starting")

    # -- updates --------------------------------------------------------------------
    def set_core_state(self, state: str, detail: str = "") -> None:
        text, color = {"running": ("Працює", GREEN), "starting": ("Запуск…", YELLOW),
                       "stopped": ("Зупинено", RED)}.get(state, (state, MUTED))
        self.core.set(text, detail, color)

    def update_status(self, st: dict) -> None:
        self.set_core_state("running", f"v{st['version']} · працює {fmt_duration(st['uptime_s'])}")
        self.servers.set(f"{st['games_running']} / {st['games_total']}", "запущено / зареєстровано ігор")
        self.players.set(str(st["players_online"]), f"кімнат відкрито: {st['rooms_open']}")
        net = st["network"]
        names = {"local": ("Local", "лише цей ПК"), "lan": ("LAN", "домашня мережа"),
                 "tunnel": ("Tunnel", "інтернет через тунель"), "direct": ("Direct", "інтернет напряму")}
        title, mode = names.get(net["mode"], (net["mode"], ""))
        tunnel = {"connected": "тунель підключено", "starting": "тунель запускається",
                  "reconnecting": "тунель перепідключається", "failed": "помилка тунелю"}.get(net.get("tunnel_state", ""), "")
        detail = f"{mode}, порт {net['gateway_port']}" + (f" · {tunnel}" if net["mode"] == "tunnel" and tunnel else "")
        detail += "\n" + (net.get("player_url") or "адреса для гравців недоступна")
        color = None
        if net["mode"] == "tunnel":
            color = GREEN if net.get("tunnel_state") == "connected" else YELLOW
        self.access.set(title, detail, color)
        if st.get("resources"):
            self.update_metrics(st["resources"])

    def update_games(self, games: list[dict]) -> None:
        from hgs.gui.widgets.common import STATE_NAMES
        from hgs.gui.widgets.table import fill

        rows = []
        for g in games:
            m = g.get("metrics") or {}
            rows.append([g["name"], STATE_NAMES.get(g["state"], g["state"]).lower(), str(g["players_online"]),
                         str(g["rooms_open"]),
                         f"{m['cpu_percent']:.0f}%" if m.get("cpu_percent") is not None else "у ядрі HGS",
                         fmt_bytes(m["memory_bytes"]) if m.get("memory_bytes") is not None else "—",
                         fmt_duration(g["uptime_s"])])
        fill(self.servers_table, rows, keys=[g["id"] for g in games])

    def update_metrics(self, m: dict) -> None:
        self.cpu.set(f"{m['cpu_percent']:.0f}%", f"ядро HGS: {m['core_cpu_percent']:.1f}%")
        self.ram.set(f"{m['ram_percent']:.0f}%",
                     f"{fmt_bytes(m['ram_used_bytes'])} з {fmt_bytes(m['ram_total_bytes'])} · ядро HGS {fmt_bytes(m['core_memory_bytes'])}")
        self.net_label.setText(f"Мережа ПК: ↓ {fmt_bytes(m['net_recv_bps'])}/с   ↑ {fmt_bytes(m['net_sent_bps'])}/с")
        self.spark.push(m["net_recv_bps"], m["net_sent_bps"])

    def add_event(self, ev: dict) -> None:
        text = describe_event(ev)
        if text is None:
            return
        self.events.insertItem(0, text)
        while self.events.count() > 200:
            self.events.takeItem(self.events.count() - 1)
