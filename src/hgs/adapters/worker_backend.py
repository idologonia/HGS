"""WorkerBackend: runs a game's server logic in a separate process and talks to it
over the Worker Bus. Used by the HTML adapter (mode "logic"); the Python adapter
(Stage 5) reuses it.

Why a separate process: a bug, a hang or a memory leak in one game cannot affect the
core or other games, and the OS can limit/kill the whole process tree.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from hgs.adapters.base import AdapterError, JoinDecision, PlayerRef, RoomRef
from hgs.core.network.worker_bus import WorkerLink, WorkerLinkClosed
from hgs.core.process_supervisor import LaunchError, LaunchSpec, ManagedProcess

START_TIMEOUT_S = 25.0
HEARTBEAT_S = 10.0
HEARTBEAT_FAILS = 3
EVENT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


class WorkerBackend:
    def __init__(self, ctx, manifest, spec: LaunchSpec, config: dict[str, Any]) -> None:
        self.ctx = ctx
        self.manifest = manifest
        self.spec = spec
        self.config = config
        self.process: ManagedProcess | None = None
        self.link: WorkerLink | None = None
        self.stopping = False
        self.custom_status: dict[str, Any] = {}
        self.events_in = 0
        self._tasks: list[asyncio.Task] = []

    # == lifecycle =================================================================
    async def start(self) -> None:
        ctx = self.ctx
        self.stopping = False
        token, fut = ctx.worker_bus.expect(ctx.game_id)
        env = dict(self.spec.env)
        env.update({"HGS_WORKER_URL": ctx.worker_bus.url(), "HGS_WORKER_TOKEN": token,
                    "HGS_GAME_ID": ctx.game_id, "HGS_DATA_DIR": str(ctx.paths.root)})
        spec = LaunchSpec(program=self.spec.program, args=self.spec.args, cwd=self.spec.cwd,
                          game_root=self.spec.game_root, entry=self.spec.entry, env=env,
                          memory_mb=self.spec.memory_mb)
        try:
            ctx.allowlist.validate(spec)
        except LaunchError as exc:
            ctx.worker_bus.cancel(fut)
            raise AdapterError(str(exc)) from exc
        self.process = ManagedProcess(spec, ctx.log)
        try:
            await self.process.start()
        except LaunchError as exc:
            ctx.worker_bus.cancel(fut)
            raise AdapterError(str(exc)) from exc

        exit_wait = asyncio.create_task(self.process.wait())
        done, _ = await asyncio.wait({fut, exit_wait}, timeout=START_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED)
        if fut not in done:
            ctx.worker_bus.cancel(fut)
            if exit_wait in done:
                raise AdapterError(f"game worker exited during start (code {exit_wait.result()}): {self._last_err()}")
            exit_wait.cancel()
            await self.process.terminate(2)
            raise AdapterError("game worker did not connect in time")
        self.link = fut.result()
        self.link.handler = self._on_message

        welcome: dict[str, Any] = {"game_id": ctx.game_id, "config": self.config,
                                   "rooms": [self._room_dict(r) for r in ctx.list_rooms()], "save": None}
        saves = self.manifest.saves
        if saves.enabled and saves.load_on_start:
            snap = ctx.saves.latest(ctx.game_id)
            if snap is not None:
                welcome["save"] = {"format": snap[0], "data": snap[1]}
        try:
            reply = await self.link.request("w.welcome", welcome, timeout=START_TIMEOUT_S)
        except (asyncio.TimeoutError, WorkerLinkClosed) as exc:
            exit_wait.cancel()
            await self.process.terminate(2)
            raise AdapterError(f"game worker failed to initialize: {self._last_err() or type(exc).__name__}")
        if not isinstance(reply, dict) or not reply.get("ready"):
            exit_wait.cancel()
            await self.process.terminate(2)
            error = reply.get("error") if isinstance(reply, dict) else None
            raise AdapterError(f"game worker reported an error during start: {error or self._last_err()}")
        if welcome["save"]:
            ctx.log.info("state restored from the latest snapshot")
        self._tasks = [asyncio.create_task(self._watch(exit_wait)), asyncio.create_task(self._heartbeat())]
        if saves.enabled and saves.interval_s > 0:
            self._tasks.append(asyncio.create_task(self._autosave(saves.interval_s)))

    def _last_err(self) -> str:
        if self.process and self.process.stderr_tail:
            return self.process.stderr_tail[-1]
        return ""

    async def _watch(self, exit_wait: asyncio.Task) -> None:
        assert self.link is not None
        link_closed = asyncio.create_task(self.link.closed.wait())
        done, _ = await asyncio.wait({exit_wait, link_closed}, return_when=asyncio.FIRST_COMPLETED)
        if self.stopping:
            return
        if exit_wait in done:
            reason = f"game worker exited unexpectedly (code {exit_wait.result()})"
            if self._last_err():
                reason += f": {self._last_err()}"
        else:
            reason = "game worker lost its connection to the core"
            link_closed.cancel()
        self.ctx.report_crash(reason)

    async def _heartbeat(self) -> None:
        fails = 0
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            try:
                await self.link.request("w.ping", {}, timeout=HEARTBEAT_S)
                fails = 0
            except asyncio.TimeoutError:
                fails += 1
                self.ctx.log.warning("game worker did not answer ping (%d/%d)", fails, HEARTBEAT_FAILS)
                if fails >= HEARTBEAT_FAILS:
                    self.ctx.log.error("game worker is not responding, killing it")
                    if self.process and self.process.pid:
                        self.process.container.kill_tree(self.process.pid)
                    return
            except WorkerLinkClosed:
                return

    async def _autosave(self, interval: int) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.save("interval")
            except Exception as exc:
                self.ctx.log.error("periodic save failed: %s", exc)

    async def stop(self, reason: str, grace_s: float) -> None:
        self.stopping = True
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        if self.link is not None and not self.link.closed.is_set():
            try:
                await self.link.send("w.shutdown", {"reason": reason, "grace_s": grace_s})
            except WorkerLinkClosed:
                pass
        if self.process is not None and self.process.proc is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.process.proc.wait()), max(3.0, min(grace_s, 10.0) + 2))
            except asyncio.TimeoutError:
                pass
            await self.process.terminate(3)
        if self.link is not None:
            await self.link.close()
        self.link = None

    # == saves =====================================================================
    async def save(self, reason: str) -> dict | None:
        saves = self.manifest.saves
        if not saves.enabled or self.link is None or self.link.closed.is_set():
            return None
        if reason == "stop" and not saves.on_stop:
            return None
        try:
            reply = await self.link.request("w.save", {"reason": reason}, timeout=10)
        except (asyncio.TimeoutError, WorkerLinkClosed) as exc:
            self.ctx.log.error("save request failed: %s", type(exc).__name__)
            return None
        if not isinstance(reply, dict) or not reply.get("supported") or "data" not in reply:
            return None
        path = await self.ctx.saves.write_snapshot(self.ctx.game_id, reason, reply.get("format", "json"),
                                                   reply["data"], saves.keep)
        if path is None:
            return None
        self.ctx.log.info("state saved (%s)", reason)
        return {"path": str(path)}

    # == player events =============================================================
    @staticmethod
    def _room_dict(room: Any) -> dict:
        if isinstance(room, RoomRef):
            return {"id": room.room_id, "name": room.name, "params": room.params}
        return {"id": room.room_id, "name": room.name, "params": room.params, "max_players": room.max_players}

    async def handle_connection(self, player: PlayerRef, room: RoomRef) -> JoinDecision:
        if self.link is None:
            return JoinDecision(False, "game server is not ready")
        reply = await self.link.request("w.player.join", {"room": self._room_dict(room),
                                                          "player": {"id": player.player_id, "name": player.name}},
                                        timeout=4)
        if not isinstance(reply, dict):
            return JoinDecision(False, "invalid answer from the game")
        return JoinDecision(bool(reply.get("accept", True)), reply.get("reason"))

    async def handle_message(self, player: PlayerRef, room: RoomRef, event: str, data: Any, to: str | None) -> None:
        if self.link is not None:
            self.events_in += 1
            await self.link.send("w.player.event", {"room_id": room.room_id, "player_id": player.player_id,
                                                    "e": event, "d": data})

    async def handle_disconnect(self, player: PlayerRef, room: RoomRef, reason: str) -> None:
        if self.link is not None:
            try:
                await self.link.send("w.player.leave", {"room_id": room.room_id, "player_id": player.player_id,
                                                        "reason": reason})
            except WorkerLinkClosed:
                pass

    async def handle_room_event(self, room: RoomRef, event: str) -> None:
        if self.link is not None:
            try:
                await self.link.send(f"w.room.{event}", {"room": self._room_dict(room)})
            except WorkerLinkClosed:
                pass

    # == messages from the worker ==================================================
    async def _on_message(self, t: str, d: Any) -> None:
        ctx = self.ctx
        d = d if isinstance(d, dict) else {}
        if t == "w.send":
            room_id, event = d.get("room_id"), d.get("e")
            if not isinstance(room_id, str) or not isinstance(event, str) or not EVENT_RE.match(event):
                ctx.log.warning("worker sent an invalid w.send (ignored)")
                return
            to, exclude = d.get("to"), d.get("except")
            if isinstance(to, list):
                for pid in to:
                    if isinstance(pid, str):
                        ctx.send_to_player(room_id, pid, event, d.get("d"))
            else:
                ex = {p for p in exclude if isinstance(p, str)} if isinstance(exclude, list) else None
                ctx.send_to_room(room_id, event, d.get("d"), exclude=ex)
        elif t == "w.room.update":
            ctx.update_room(str(d.get("room_id")), status=d.get("status"), params=d.get("params"),
                            max_players=d.get("max_players"))
        elif t == "w.room.close":
            ctx.close_room(str(d.get("room_id")), str(d.get("reason") or "closed by game")[:120])
        elif t == "w.kick":
            ctx.kick(str(d.get("room_id")), str(d.get("player_id")), str(d.get("reason") or "kicked by game")[:120])
        elif t == "w.log":
            level = LOG_LEVELS.get(str(d.get("level", "info")).lower(), logging.INFO)
            ctx.log.log(level, "[game] %s", str(d.get("msg", ""))[:4000])
        elif t == "w.status":
            custom = d.get("custom")
            if isinstance(custom, dict) and len(str(custom)) < 4000:
                self.custom_status = custom
        else:
            ctx.log.warning("unknown message from worker: %s", t[:64])

    def metrics(self) -> dict[str, Any]:
        m = self.process.metrics() if self.process else {}
        return {**m, "events_in": self.events_in, **({"game": self.custom_status} if self.custom_status else {})}
