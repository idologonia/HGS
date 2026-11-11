"""Server Manager: the game registry and the lifecycle of game servers.

* operations on one game are serialized by a per-game lock (double Start cannot start twice);
* any adapter exception is caught: only that game goes to ERROR/CRASHED, the core keeps running;
* every adapter call has a timeout, so a hung adapter cannot freeze the core;
* crash restarts are limited (default: 3 within 10 minutes, back-off 2/8/30 s), then FAILED.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hgs.adapters.base import (
    AdapterError,
    AdapterState,
    BaseAdapter,
    PlayerRef,
)
from hgs.adapters.registry import AdapterRegistry
from hgs.common.fileutil import atomic_write_json
from hgs.common.paths import DataPaths, GamePaths
from hgs.core.config.manifest import MANIFEST_FILE, GameManifest, ManifestError, load_manifest_file, parse_manifest
from hgs.core.db.database import Database, utcnow
from hgs.core.protocol.client import CLOSE_GAME_STOPPED, msg

if TYPE_CHECKING:
    from hgs.core.events import EventBus
    from hgs.core.logging_setup import LogManager
    from hgs.core.rooms import RoomManager
    from hgs.core.sessions import SessionManager

log = logging.getLogger("hgs.servers")

START_TIMEOUT_S = 30.0
CALL_TIMEOUT_S = 5.0
BACKOFF_S = (2.0, 8.0, 30.0)


class GameError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class GameContext:
    """AdapterContext implementation, scoped to one game."""

    def __init__(self, manager: "ServerManager", game_id: str, root: Path, paths: GamePaths, logger: logging.Logger):
        self._m = manager
        self.game_id = game_id
        self.game_root = root
        self.paths = paths
        self.log = logger

    # services (set by the Server Manager)
    @property
    def worker_bus(self):
        return self._m.worker_bus

    @property
    def saves(self):
        return self._m.saves

    @property
    def allowlist(self):
        return self._m.allowlist

    def list_rooms(self) -> list:
        return self._m.rooms.list(self.game_id)

    def update_room(self, room_id: str, **changes: Any) -> None:
        self._m.rooms.update(self.game_id, room_id, **changes)

    def close_room(self, room_id: str, reason: str) -> None:
        self._m.rooms.close(self.game_id, room_id, reason)

    def kick(self, room_id: str, player_id: str, reason: str) -> bool:
        room = self._m.rooms.get(self.game_id, room_id)
        if room is None or player_id not in room.players or self._m.kick is None:
            return False
        return self._m.kick(self.game_id, player_id, reason)

    def send_to_room(self, room_id: str, event: str, data: Any, *, sender: str | None = None,
                     exclude: "str | set[str] | None" = None) -> None:
        room = self._m.rooms.get(self.game_id, room_id)  # lookup is scoped to this game
        if room is None:
            return
        room.broadcast(msg("event", {"e": event, "d": data, "from": sender or "server"}), exclude=exclude)

    def send_to_player(self, room_id: str, player_id: str, event: str, data: Any, *,
                       sender: str | None = None) -> bool:
        room = self._m.rooms.get(self.game_id, room_id)
        if room is None:
            return False
        text = msg("event", {"e": event, "d": data, "from": sender or "server"})
        s = room.players.get(player_id)
        if s is None:
            queue = room.pending.get(player_id)
            if queue is not None and len(queue) < 100:
                queue.append(text)  # the player is joining right now
                return True
            return False
        s.send(text)
        return True

    def room_players(self, room_id: str) -> list[PlayerRef]:
        room = self._m.rooms.get(self.game_id, room_id)
        return [PlayerRef(s.player_id, s.name) for s in room.players.values()] if room else []

    def report_crash(self, error: str) -> None:
        asyncio.get_running_loop().create_task(self._m._on_crash(self.game_id, error))


@dataclass
class GameServer:
    manifest: GameManifest
    root: Path
    paths: GamePaths
    state: AdapterState = AdapterState.STOPPED
    adapter: BaseAdapter | None = None
    last_error: str | None = None
    started_at: float | None = None
    crashes: deque = field(default_factory=deque)
    restarts: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    restart_task: asyncio.Task | None = None
    manifest_source: str = "file"  # "file" = hgs-game.json in the game folder, "hgs" = kept inside HGS

    @property
    def game_id(self) -> str:
        return self.manifest.id


class ServerManager:
    def __init__(
        self,
        *,
        data: DataPaths,
        db: Database,
        bus: "EventBus",
        registry: AdapterRegistry,
        rooms: "RoomManager",
        sessions: "SessionManager",
        logs: "LogManager",
        worker_bus: Any = None,
        saves: Any = None,
        allowlist: Any = None,
    ) -> None:
        self.data = data
        self.db = db
        self.bus = bus
        self.registry = registry
        self.rooms = rooms
        self.sessions = sessions
        self.logs = logs
        self.worker_bus = worker_bus
        self.saves = saves
        self.allowlist = allowlist
        self.kick = None  # set by the core once the gateway exists
        self.games: dict[str, GameServer] = {}
        rooms.listener = self._on_room_event

    def _on_room_event(self, kind: str, room: Any) -> None:
        game = self.games.get(room.game_id)
        if game is None or game.adapter is None or game.state != AdapterState.RUNNING:
            return
        from hgs.adapters.base import RoomRef

        ref = RoomRef(room.room_id, room.name, dict(room.params))
        adapter = game.adapter

        async def _call() -> None:
            try:
                await asyncio.wait_for(adapter.handle_room_event(ref, kind), CALL_TIMEOUT_S)
            except Exception as exc:
                log.error("game %s: room event handler failed: %s", room.game_id, _describe(exc))
        asyncio.get_running_loop().create_task(_call())

    # == registry ==================================================================
    async def load_registry(self) -> None:
        """Load registered games. Servers are NOT started (manual start only)."""
        for row in await self.db.fetchall("SELECT game_id, root_path FROM games ORDER BY name"):
            gp = self.data.game(row["game_id"])
            try:
                stored = json.loads(gp.config.read_text(encoding="utf-8"))
                manifest = parse_manifest(stored["manifest"])
            except Exception as exc:  # a broken game must not prevent HGS from starting
                log.error("cannot load game %s: %s", row["game_id"], exc)
                continue
            self.games[manifest.id] = GameServer(manifest, Path(row["root_path"]), gp,
                                                 manifest_source=stored.get("manifest_source", "file"))

    def _validate(self, manifest: GameManifest) -> None:
        cls = self.registry.get(manifest.adapter)
        if cls is None:
            raise GameError("bad_request", f"unknown adapter type {manifest.adapter!r}")
        if manifest.adapter_api != cls.api_version:
            raise GameError("bad_request", f"game needs adapter API v{manifest.adapter_api}, HGS has v{cls.api_version}")
        result = cls.validate_config(manifest)
        if not result.ok:
            raise GameError("bad_request", "; ".join(result.errors))

    async def add_game(self, directory: str, manifest_data: dict | None = None) -> GameServer:
        """Register a game folder. Without manifest_data, hgs-game.json is read from the folder;
        with it, the description is kept only inside HGS (the game folder is not modified)."""
        root = Path(directory).expanduser()
        if not root.is_dir():
            raise GameError("bad_request", f"directory does not exist: {directory}")
        root = root.resolve()
        try:
            if manifest_data is not None:
                manifest = parse_manifest(manifest_data)
            else:
                manifest = load_manifest_file(root / MANIFEST_FILE)
        except ManifestError as exc:
            raise GameError("bad_request", str(exc))
        if manifest.id in self.games:
            raise GameError("conflict", f"game {manifest.id!r} is already registered")
        self._validate(manifest)
        gp = self.data.game(manifest.id)
        gp.ensure()
        source = "hgs" if manifest_data is not None else "file"
        atomic_write_json(gp.config, {"root_path": str(root), "manifest_source": source, "manifest": manifest.model_dump()})
        await self.db.execute(
            "INSERT INTO games(game_id, name, adapter, root_path, added_at) VALUES (?,?,?,?,?)",
            (manifest.id, manifest.name, manifest.adapter, str(root), utcnow()),
        )
        game = GameServer(manifest, root, gp, manifest_source=source)
        self.games[manifest.id] = game
        log.info("game added: %s (%s) from %s", manifest.id, manifest.adapter, root)
        self.bus.publish("game.added", game=self.game_view(game))
        return game

    async def reload_manifest(self, game_id: str) -> GameServer:
        game = self._get(game_id)
        async with game.lock:
            if game.state not in (AdapterState.STOPPED, AdapterState.ERROR, AdapterState.FAILED):
                raise GameError("conflict", "stop the server before reloading its configuration")
            if not (game.root / MANIFEST_FILE).is_file():
                raise GameError("bad_request", f"{MANIFEST_FILE} not found in the game folder; this game's description "
                                               "is kept in HGS — edit it in Settings")
            try:
                manifest = load_manifest_file(game.root / MANIFEST_FILE)
            except ManifestError as exc:
                raise GameError("bad_request", str(exc))
            if manifest.id != game_id:
                raise GameError("bad_request", f"manifest id changed to {manifest.id!r}; remove and add the game again")
            self._validate(manifest)
            atomic_write_json(game.paths.config, {"root_path": str(game.root), "manifest_source": "file",
                                                  "manifest": manifest.model_dump()})
            await self.db.execute("UPDATE games SET name=?, adapter=? WHERE game_id=?", (manifest.name, manifest.adapter, game_id))
            game.manifest = manifest
            game.manifest_source = "file"
        self.bus.publish("game.updated", game=self.game_view(game))
        return game

    async def update_manifest(self, game_id: str, data: dict) -> GameServer:
        """Change the game's configuration inside HGS (Settings editor). The game folder is not modified."""
        game = self._get(game_id)
        async with game.lock:
            if game.state not in (AdapterState.STOPPED, AdapterState.ERROR, AdapterState.FAILED):
                raise GameError("conflict", "stop the server before changing its configuration")
            try:
                manifest = parse_manifest(data)
            except ManifestError as exc:
                raise GameError("bad_request", str(exc))
            if manifest.id != game_id:
                raise GameError("bad_request", "the game id cannot be changed")
            self._validate(manifest)
            atomic_write_json(game.paths.config, {"root_path": str(game.root), "manifest_source": "hgs",
                                                  "manifest": manifest.model_dump()})
            await self.db.execute("UPDATE games SET name=?, adapter=? WHERE game_id=?", (manifest.name, manifest.adapter, game_id))
            game.manifest = manifest
            game.manifest_source = "hgs"
        self.bus.publish("game.updated", game=self.game_view(game))
        return game

    async def remove_game(self, game_id: str) -> None:
        game = self._get(game_id)
        async with game.lock:
            if game.state not in (AdapterState.STOPPED, AdapterState.ERROR, AdapterState.FAILED):
                raise GameError("conflict", "stop the server before removing the game")
            await self.db.execute("DELETE FROM games WHERE game_id=?", (game_id,))
            del self.games[game_id]
        # The game's own files are never touched; HGS data (saves, logs) stays in the data folder.
        log.info("game removed from registry: %s", game_id)
        self.bus.publish("game.removed", game_id=game_id)

    # == lifecycle =================================================================
    def _get(self, game_id: str) -> GameServer:
        game = self.games.get(game_id)
        if game is None:
            raise GameError("not_found", f"game {game_id!r} is not registered")
        return game

    def _set_state(self, game: GameServer, state: AdapterState, error: str | None = None) -> None:
        game.state = state
        if error is not None:
            game.last_error = error
        self.bus.publish("server.state_changed", game_id=game.game_id, state=state.value, error=error)

    async def start(self, game_id: str, *, _auto: bool = False) -> None:
        game = self._get(game_id)
        async with game.lock:
            if game.state in (AdapterState.RUNNING, AdapterState.STARTING):
                return
            if game.state == AdapterState.FAILED and _auto:
                return
            if not _auto:
                game.crashes.clear()
                if game.restart_task and not game.restart_task.done():
                    game.restart_task.cancel()
            await self._start_locked(game)

    async def _start_locked(self, game: GameServer) -> None:
        self._set_state(game, AdapterState.STARTING)
        game.last_error = None
        try:
            self._validate(game.manifest)
            cls = self.registry.get(game.manifest.adapter)
            assert cls is not None
            logger = self.logs.game_logger(game.game_id, game.paths.logs)
            ctx = GameContext(self, game.game_id, game.root, game.paths, logger)
            adapter = cls(ctx, game.manifest)
            game.adapter = adapter
            await asyncio.wait_for(adapter.initialize(), START_TIMEOUT_S)
            await asyncio.wait_for(adapter.start(), START_TIMEOUT_S)
        except Exception as exc:
            err = _describe(exc)
            log.error("game %s failed to start: %s", game.game_id, err)
            await self._safe_adapter_stop(game, "start_failed")
            game.adapter = None
            self._set_state(game, AdapterState.ERROR, err)
            return
        game.started_at = time.time()
        for room_id in game.manifest.rooms.default_rooms:
            if self.rooms.get(game.game_id, room_id) is None:
                self.rooms.create(
                    game.game_id, room_id, room_id=room_id, max_players=game.manifest.rooms.max_players,
                    max_rooms=game.manifest.rooms.max_rooms, persistent=True, created_by="server",
                )
        await self.db.execute("UPDATE games SET last_started_at=?, last_state=? WHERE game_id=?",
                              (utcnow(), "running", game.game_id))
        log.info("game %s started", game.game_id)
        self._set_state(game, AdapterState.RUNNING)

    async def stop(self, game_id: str, grace_s: float = 5.0, reason: str = "stopped by admin") -> None:
        game = self._get(game_id)
        if game.restart_task and not game.restart_task.done():
            game.restart_task.cancel()
        async with game.lock:
            if game.state in (AdapterState.STOPPED,):
                return
            if game.state in (AdapterState.ERROR, AdapterState.FAILED, AdapterState.CRASHED):
                self._disconnect_all(game, reason)
                self._set_state(game, AdapterState.STOPPED)
                return
            await self._stop_locked(game, grace_s, reason)

    async def _stop_locked(self, game: GameServer, grace_s: float, reason: str) -> None:
        self._set_state(game, AdapterState.STOPPING)
        players = self.sessions.for_game(game.game_id)
        if players and grace_s > 0:
            for s in players:
                s.send(msg("server_shutdown", {"grace_s": grace_s, "reason": reason}))
            await asyncio.sleep(grace_s)
        if game.adapter is not None:
            try:
                await asyncio.wait_for(game.adapter.save("stop"), CALL_TIMEOUT_S)
            except Exception as exc:
                log.error("game %s: save on stop failed: %s", game.game_id, _describe(exc))
        self._disconnect_all(game, reason)
        await self._safe_adapter_stop(game, reason, grace_s)
        game.adapter = None
        game.started_at = None
        await self.db.execute("UPDATE games SET last_state=? WHERE game_id=?", ("stopped", game.game_id))
        log.info("game %s stopped (%s)", game.game_id, reason)
        self._set_state(game, AdapterState.STOPPED)

    def _disconnect_all(self, game: GameServer, reason: str) -> None:
        self.rooms.close_all(game.game_id, "server_stopped")
        for s in self.sessions.for_game(game.game_id):
            s.close(CLOSE_GAME_STOPPED, reason)

    async def _safe_adapter_stop(self, game: GameServer, reason: str, grace_s: float = 0.0) -> None:
        if game.adapter is None:
            return
        try:
            await asyncio.wait_for(game.adapter.stop(reason, grace_s), grace_s + 10.0)
        except Exception as exc:
            log.error("game %s: adapter stop failed: %s", game.game_id, _describe(exc))
        try:
            await asyncio.wait_for(game.adapter.shutdown(), CALL_TIMEOUT_S)
        except Exception as exc:
            log.error("game %s: adapter shutdown failed: %s", game.game_id, _describe(exc))

    async def restart(self, game_id: str, grace_s: float = 5.0) -> None:
        game = self._get(game_id)
        await self.stop(game_id, grace_s, "restarting")
        await self.start(game.game_id)

    async def _on_crash(self, game_id: str, error: str) -> None:
        game = self.games.get(game_id)
        if game is None:
            return
        async with game.lock:
            if game.state not in (AdapterState.RUNNING, AdapterState.STARTING):
                return
            log.error("game %s crashed: %s", game_id, error)
            self._disconnect_all(game, "server crashed")
            await self._safe_adapter_stop(game, "crashed")
            game.adapter = None
            now = time.monotonic()
            policy = game.manifest.restart
            game.crashes.append(now)
            while game.crashes and now - game.crashes[0] > policy.window_s:
                game.crashes.popleft()
            if policy.policy == "never" or len(game.crashes) > policy.max_restarts:
                self._set_state(game, AdapterState.FAILED, f"{error} (automatic restart disabled after "
                                f"{len(game.crashes)} crash(es); press Start to try again)")
                return
            self._set_state(game, AdapterState.CRASHED, error)
            delay = BACKOFF_S[min(len(game.crashes) - 1, len(BACKOFF_S) - 1)]
            game.restart_task = asyncio.get_running_loop().create_task(self._delayed_restart(game_id, delay))

    async def _delayed_restart(self, game_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        game = self.games.get(game_id)
        if game is None or game.state != AdapterState.CRASHED:
            return
        game.restarts += 1
        log.info("game %s: automatic restart #%d", game_id, game.restarts)
        await self.start(game_id, _auto=True)

    async def stop_all(self, grace_s: float = 3.0) -> None:
        await asyncio.gather(
            *(self.stop(gid, grace_s, "HGS is shutting down") for gid in list(self.games)),
            return_exceptions=True,
        )

    # == views =====================================================================
    def is_running(self, game_id: str) -> bool:
        g = self.games.get(game_id)
        return g is not None and g.state == AdapterState.RUNNING and g.adapter is not None

    def game_view(self, game: GameServer) -> dict[str, Any]:
        m = game.manifest
        metrics: dict[str, Any] = {}
        if game.adapter is not None:
            try:
                am = game.adapter.get_metrics()
                metrics = dict(am.custom)
                if am.cpu_percent is not None:
                    metrics["cpu_percent"] = am.cpu_percent
                if am.memory_bytes is not None:
                    metrics["memory_bytes"] = am.memory_bytes
            except Exception:
                metrics = {}
        return {
            "id": m.id,
            "name": m.name,
            "version": m.version,
            "adapter": m.adapter,
            "mode": (m.backend or {}).get("mode", "relay"),
            "root_path": str(game.root),
            "state": game.state.value,
            "last_error": game.last_error,
            "started_at": game.started_at,
            "uptime_s": round(time.time() - game.started_at) if game.started_at else None,
            "players_online": self.sessions.count(m.id),
            "rooms_open": len(self.rooms.list(m.id)),
            "restarts": game.restarts,
            "auth_mode": m.auth.mode,
            "client_url": m.client.url,
            "manifest_source": game.manifest_source,
            "proxy_path": f"/hgs/v1/proxy/{m.id}/" if m.adapter in ("python", "process")
            and (m.backend or {}).get("mode") == "standalone" else None,
            "metrics": metrics,
        }

    def all_views(self) -> list[dict[str, Any]]:
        return [self.game_view(g) for g in self.games.values()]


def _describe(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "timed out"
    if isinstance(exc, (AdapterError, GameError)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"
