"""Worker side of the Worker Protocol (docs/architecture/06-protocol.md, §6.4)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
import time
import traceback
from typing import Any

import websockets

from .logic import GameLogic, Player, Room

API_VERSION = 1


class _BusLogHandler(logging.Handler):
    def __init__(self, conn: "Connection") -> None:
        super().__init__()
        self.conn = conn

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + "".join(traceback.format_exception(*record.exc_info))
            self.conn.post("w.log", {"level": record.levelname.lower(), "msg": msg})
        except Exception:
            pass


class Connection:
    def __init__(self, logic: GameLogic, url: str, token: str, game_id: str) -> None:
        self.logic = logic
        self.url = url
        self.token = token
        self.game_id = game_id
        self._out: asyncio.Queue = asyncio.Queue()
        self._ws: Any = None
        self._tick_task: asyncio.Task | None = None
        self._last_tick_error = 0.0
        self.exit_code = 0
        logic._conn = self
        handler = _BusLogHandler(self)
        logic.log.addHandler(handler)
        logic.log.setLevel(logging.INFO)
        logic.log.propagate = False

    # -- sending -----------------------------------------------------------------
    def post(self, t: str, d: Any = None, **extra: Any) -> None:
        frame = {"v": 1, "t": t, **extra}
        if d is not None:
            frame["d"] = d
        self._out.put_nowait(frame)

    async def _writer(self) -> None:
        while True:
            frame = await self._out.get()
            try:
                text = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError) as exc:
                print(f"hgs_sdk: cannot send {frame.get('t')}: data is not JSON-serializable ({exc})", file=sys.stderr)
                continue
            await self._ws.send(text)

    def _reply(self, rid: Any, d: Any) -> None:
        self.post("w.reply", d if d is not None else {}, re=rid)

    # -- main loop ---------------------------------------------------------------
    async def run(self) -> int:
        async with websockets.connect(self.url, max_size=2 * 1024 * 1024, ping_interval=20, ping_timeout=20,
                                      proxy=None) as ws:
            self._ws = ws
            await ws.send(json.dumps({"v": 1, "t": "w.hello", "d": {"token": self.token, "game_id": self.game_id,
                                                                     "api": API_VERSION, "sdk": "hgs_sdk-py/1.0.0"}}))
            writer = asyncio.create_task(self._writer())
            try:
                async for raw in ws:
                    try:
                        frame = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    stop = await self._handle(frame.get("t"), frame.get("d") or {}, frame.get("id"))
                    if stop:
                        break
                # flush pending messages before closing
                while not self._out.empty():
                    await asyncio.sleep(0.01)
                    if writer.done():
                        break
            except websockets.ConnectionClosed:
                if self.exit_code == 0:
                    print("hgs_sdk: connection to HGS core lost, exiting", file=sys.stderr)
                    self.exit_code = 3
            finally:
                writer.cancel()
                if self._tick_task:
                    self._tick_task.cancel()
        return self.exit_code

    async def _call(self, name: str, *args: Any) -> Any:
        try:
            return await getattr(self.logic, name)(*args)
        except Exception:
            self.logic.log.error("error in %s:\n%s", name, traceback.format_exc())
            return _FAILED

    def _room(self, d: dict) -> Room:
        rid = str(d.get("id"))
        room = self.logic.rooms.get(rid)
        if room is None:
            room = Room(self, rid, str(d.get("name", rid)), d.get("params") or {}, d.get("max_players"))
            self.logic.rooms[rid] = room
        return room

    async def _handle(self, t: str, d: dict, rid: Any) -> bool:
        logic = self.logic
        if t == "w.ping":
            self._reply(rid, {})
        elif t == "w.welcome":
            logic.config = d.get("config") or {}
            for r in d.get("rooms") or []:
                self._room(r)
            save = d.get("save")
            ok = True
            if save:
                data = save.get("data")
                if save.get("format") == "b64":
                    data = base64.b64decode(data)
                ok = await self._call("on_load", data) is not _FAILED
            if ok:
                ok = await self._call("on_start") is not _FAILED
            if not ok:
                self._reply(rid, {"ready": False, "error": "on_load/on_start failed (see game log)"})
                await asyncio.sleep(0.2)
                self.exit_code = 1
                return True
            if logic.tick_rate and logic.tick_rate > 0:
                self._tick_task = asyncio.create_task(self._ticker(float(logic.tick_rate)))
            self._reply(rid, {"ready": True})
        elif t == "w.room.created":
            room = self._room(d.get("room") or {})
            await self._call("on_room_created", room)
        elif t == "w.room.closed":
            room = logic.rooms.pop(str((d.get("room") or {}).get("id")), None)
            if room is not None:
                await self._call("on_room_closed", room)
        elif t == "w.player.join":
            room = self._room(d.get("room") or {})
            p = d.get("player") or {}
            player = Player(str(p.get("id")), str(p.get("name")))
            result = await self._call("on_join", room, player)
            if result is _FAILED:
                self._reply(rid, {"accept": False, "reason": "game error"})
            elif result is False or isinstance(result, str):
                self._reply(rid, {"accept": False, "reason": result if isinstance(result, str) else None})
            else:
                room.players[player.id] = player
                self._reply(rid, {"accept": True})
        elif t == "w.player.event":
            room = logic.rooms.get(str(d.get("room_id")))
            player = room.players.get(str(d.get("player_id"))) if room else None
            if room is not None and player is not None:
                await self._call("on_event", room, player, str(d.get("e")), d.get("d"))
        elif t == "w.player.leave":
            room = logic.rooms.get(str(d.get("room_id")))
            player = room.players.pop(str(d.get("player_id")), None) if room else None
            if room is not None and player is not None:
                await self._call("on_leave", room, player, str(d.get("reason", "")))
        elif t == "w.save":
            data = await self._call("on_save")
            if data is _FAILED:
                self._reply(rid, {"supported": False, "error": "on_save failed"})
            elif data is None:
                self._reply(rid, {"supported": False})
            elif isinstance(data, (bytes, bytearray)):
                self._reply(rid, {"supported": True, "format": "b64", "data": base64.b64encode(data).decode()})
            else:
                self._reply(rid, {"supported": True, "format": "json", "data": data})
        elif t == "w.shutdown":
            await self._call("on_shutdown")
            return True
        return False

    async def _ticker(self, rate: float) -> None:
        interval = 1.0 / rate
        last = time.monotonic()
        while True:
            await asyncio.sleep(max(0.0, last + interval - time.monotonic()))
            now = time.monotonic()
            dt, last = now - last, now
            for room in list(self.logic.rooms.values()):
                if not room.players:
                    continue
                try:
                    await self.logic.on_tick(room, dt)
                except Exception:
                    if now - self._last_tick_error > 5:  # do not flood the log 20 times per second
                        self._last_tick_error = now
                        self.logic.log.error("error in on_tick:\n%s", traceback.format_exc())


class _Failed:
    pass


_FAILED = _Failed()
