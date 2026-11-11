"""CoreApp: assembles all core modules and runs the three listeners in one asyncio loop.

Listeners:
* Admin API  — 127.0.0.1 only (GUI);
* Gateway    — players (127.0.0.1 in "local" mode, 0.0.0.0 in "lan" mode);
* Worker Bus — 127.0.0.1 only, game worker processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any

import uvicorn

from hgs import PROTOCOL_VERSION, __version__
from hgs.adapters.registry import AdapterRegistry, builtin_registry
from hgs.common.fileutil import atomic_write_json
from hgs.common.paths import DataPaths
from hgs.common.platform import InstanceLock
from hgs.core.admin_api.app import create_admin_app
from hgs.core.auth import AuthManager
from hgs.core.config.settings import Settings, load_settings
from hgs.core.db.database import Database
from hgs.core.events import EventBus
from hgs.core.logging_setup import LogManager, register_secret
from hgs.core.network.gateway import TRANSPORT_MAX_BYTES, Gateway
from hgs.core.network.manager import NetworkError, NetworkManager
from hgs.core.network.worker_bus import WorkerBus
from hgs.core.secret_store import SecretStore
from hgs.core.process_supervisor import AllowList
from hgs.core.saves import SaveManager
from hgs.core.resources import ResourceMonitor
from hgs.core.rooms import RoomManager
from hgs.core.security_log import SecurityJournal
from hgs.core.server_manager import ServerManager
from hgs.core.sessions import SessionManager

log = logging.getLogger("hgs.core")


class _EmbeddedServer(uvicorn.Server):
    """uvicorn server that does not take over process signals (the core handles them)."""

    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[override]
        yield


def _bind(host: str, port: int) -> socket.socket:
    try:
        sock = socket.create_server((host, port), backlog=128)
    except OSError as exc:
        raise CoreStartError(f"cannot listen on {host}:{port} — the port is busy or not allowed ({exc.strerror})") from exc
    sock.setblocking(False)
    return sock


class CoreStartError(RuntimeError):
    pass


class CoreApp:
    def __init__(self, data_dir: Path, admin_token: str, settings: Settings | None = None,
                 registry: AdapterRegistry | None = None) -> None:
        self.paths = DataPaths(data_dir)
        self.admin_token = admin_token
        register_secret(admin_token)
        self._settings_override = settings
        self.registry = registry or builtin_registry()
        self.bus = EventBus()
        self.started_at = time.time()
        self.previous_unclean = False
        self.admin_port = 0
        self.gateway_port = 0
        self._lock = InstanceLock(self.paths.lock)
        self._servers: dict[str, tuple[_EmbeddedServer, asyncio.Task]] = {}
        self.gateway_tls = False
        self.startup_warnings: list[str] = []
        self.settings_is_override = settings is not None
        self._bg: list[asyncio.Task] = []
        self._shutdown = asyncio.Event()
        self.shutdown_reason: str | None = None
        self._stopped = False

    # == startup ===================================================================
    async def start(self) -> None:
        self.paths.ensure()
        self._lock.acquire()
        try:
            await self._start()
        except BaseException:
            await self.stop()
            raise

    async def _start(self) -> None:
        self.settings = self._settings_override or load_settings(self.paths.settings)
        self.logs = LogManager(self.paths.logs, self.settings.logs.max_file_mb * 1024 * 1024, self.settings.logs.keep_files)
        self.logs.setup_core()
        self.logs.set_listener(self._on_log_line)
        log.info("HGS core %s starting (data: %s)", __version__, self.paths.root)

        self._check_previous_run()
        self.db = Database(self.paths.db)
        await self.db.open()
        self.journal = SecurityJournal(self.db, self.bus)
        self.secrets = SecretStore(self.db)
        self.auth = AuthManager(self.db, self.journal)
        self.sessions = SessionManager()
        self.rooms = RoomManager(self.bus)
        self.worker_bus = WorkerBus()
        self.saves = SaveManager(self.db, self.paths)
        self.allowlist = AllowList()
        self.servers = ServerManager(data=self.paths, db=self.db, bus=self.bus, registry=self.registry,
                                     rooms=self.rooms, sessions=self.sessions, logs=self.logs,
                                     worker_bus=self.worker_bus, saves=self.saves, allowlist=self.allowlist)
        await self.servers.load_registry()
        self.gateway = Gateway(self)
        self.servers.kick = self.gateway.kick
        self.resources = ResourceMonitor(self.bus, self.settings.resources.metrics_interval_s)
        self.network = NetworkManager(self)

        if self.settings.network.mode == "direct":
            d = self.settings.network.direct
            try:
                NetworkManager.validate_tls(d.tls_cert, d.tls_key)
            except NetworkError as exc:
                msg = f"Direct mode disabled at startup: {exc}. The gateway runs in Local mode."
                log.error(msg)
                self.startup_warnings.append(msg)
                self.settings.network.mode = "local"

        admin_sock = _bind("127.0.0.1", self.settings.admin.port)
        wb_sock = _bind("127.0.0.1", self.settings.worker_bus.port)
        self.admin_port = admin_sock.getsockname()[1]
        self.worker_bus.port = wb_sock.getsockname()[1]
        await self._serve(create_admin_app(self), admin_sock, "admin")
        await self._serve(self.worker_bus.app, wb_sock, "worker-bus")
        await self._start_gateway()
        await self.network.start()

        self.resources.start()
        self._bg.append(asyncio.get_running_loop().create_task(self._reaper(), name="room-reaper"))
        atomic_write_json(self.paths.run_state, {"clean": False, "pid": os.getpid(), "started_at": self.started_at})
        log.info("core ready: admin 127.0.0.1:%d, gateway %s:%d", self.admin_port, self.settings.bind_host(), self.gateway_port)
        self.bus.publish("core.ready", admin_port=self.admin_port, gateway_port=self.gateway_port)

    def _check_previous_run(self) -> None:
        try:
            state = json.loads(self.paths.run_state.read_text(encoding="utf-8"))
            self.previous_unclean = not state.get("clean", True)
        except FileNotFoundError:
            pass
        except Exception:
            self.previous_unclean = True
        if self.previous_unclean:
            log.warning("previous HGS run did not shut down cleanly")

    async def _start_gateway(self) -> None:
        sock = _bind(self.settings.bind_host(), self.settings.gateway.port)
        tls: dict[str, Any] = {}
        if self.settings.network.mode == "direct":
            d = self.settings.network.direct
            tls = {"ssl_certfile": d.tls_cert, "ssl_keyfile": d.tls_key}
        self.gateway_port = sock.getsockname()[1]
        await self._serve(self.gateway.app, sock, "gateway", **tls)
        self.gateway_tls = bool(tls)

    async def restart_gateway(self) -> None:
        """Rebind the public gateway (network mode/port changed). Connected players are
        disconnected and their SDKs reconnect (sessions can be resumed)."""
        entry = self._servers.pop("gateway", None)
        if entry is not None:
            server, task = entry
            server.should_exit = True
            try:
                await asyncio.wait_for(task, 10)
            except asyncio.TimeoutError:
                server.force_exit = True
                await asyncio.wait([task], timeout=5)
        await self._start_gateway()

    async def _serve(self, app: Any, sock: socket.socket, name: str, **extra: Any) -> None:
        config = uvicorn.Config(
            app, lifespan="off", log_config=None, access_log=False, server_header=False,
            ws_max_size=TRANSPORT_MAX_BYTES, ws_ping_interval=15.0, ws_ping_timeout=20.0,
            proxy_headers=False, timeout_graceful_shutdown=3, **extra,
        )
        server = _EmbeddedServer(config)
        task = asyncio.get_running_loop().create_task(server.serve(sockets=[sock]), name=f"uvicorn-{name}")
        self._servers[name] = (server, task)
        for _ in range(200):  # wait until it is accepting connections
            if server.started or task.done():
                break
            await asyncio.sleep(0.01)
        if task.done():
            exc = task.exception()
            raise CoreStartError(f"{name} listener failed to start: {exc}")

    async def _reaper(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                ttl = {gid: g.manifest.rooms.empty_ttl_s for gid, g in self.servers.games.items()}
                self.rooms.reap_empty(ttl)
                self.gateway.expire_sessions()
            except Exception:
                log.exception("room reaper failed")

    def _on_log_line(self, source: str, entry: dict) -> None:
        # Logging can happen from other threads (e.g. the DB thread); the bus is loop-only.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if loop is not None:
            self.bus.publish("server.log", source=source, entry=entry)

    # == shutdown ==================================================================
    def request_shutdown(self, reason: str) -> None:
        if not self._shutdown.is_set():
            self.shutdown_reason = reason
            self._shutdown.set()

    async def wait_for_shutdown(self) -> None:
        await self._shutdown.wait()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        log.info("core stopping: %s", self.shutdown_reason or "stop requested")
        if hasattr(self, "servers"):
            await self.servers.stop_all(grace_s=2.0)
        if hasattr(self, "network"):
            await self.network.stop()
        for t in self._bg:
            t.cancel()
        if hasattr(self, "resources"):
            await self.resources.stop()
        for server, _ in self._servers.values():
            server.should_exit = True
        if self._servers:
            await asyncio.wait([t for _, t in self._servers.values()], timeout=8)
        if hasattr(self, "db"):
            await self.db.close()
        try:
            atomic_write_json(self.paths.run_state, {"clean": True, "pid": os.getpid(), "stopped_at": time.time()})
        except Exception:
            pass
        log.info("core stopped")
        if hasattr(self, "logs"):
            self.logs.close()
        self._lock.release()

    # == views =====================================================================
    def admin_allowed_hosts(self) -> set[str]:
        p = self.admin_port
        return {f"127.0.0.1:{p}", f"localhost:{p}"}

    def status(self) -> dict[str, Any]:
        games = self.servers.all_views()
        return {
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "server_name": self.settings.server_name,
            "uptime_s": round(time.time() - self.started_at),
            "previous_unclean_shutdown": self.previous_unclean,
            "data_dir": str(self.paths.root),
            "network": {"mode": self.settings.network.mode, "bind": self.settings.bind_host(),
                        "gateway_port": self.gateway_port, "tls": self.gateway_tls,
                        "local_url": f"{'wss' if self.gateway_tls else 'ws'}://127.0.0.1:{self.gateway_port}",
                        "player_url": self.network.player_url(),
                        "tunnel_state": self.network.tunnel.state if self.network.tunnel else "stopped"},
            "warnings": self.startup_warnings,
            "games_total": len(games),
            "games_running": sum(1 for g in games if g["state"] == "running"),
            "players_online": self.sessions.count(),
            "rooms_open": len(self.rooms.list()),
            "resources": self.resources.snapshot,
        }
