"""Worker Bus: local-only WebSocket endpoint where game worker processes connect.

Each worker gets a one-time token (random, passed via environment). The token is
bound to one game; it is consumed by the first connection and cannot be reused.
Protocol: docs/architecture/06-protocol.md, §6.4.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
import logging
import secrets
from typing import Any, Awaitable, Callable

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from hgs import PROTOCOL_VERSION

log = logging.getLogger("hgs.workerbus")

HELLO_TIMEOUT_S = 10.0


class WorkerLinkClosed(Exception):
    pass


class WorkerLink:
    """Core side of one worker connection: send, request/reply, incoming handler."""

    def __init__(self, ws: WebSocket, game_id: str) -> None:
        self.ws = ws
        self.game_id = game_id
        self.handler: Callable[[str, Any], Awaitable[None]] | None = None
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}
        self.closed = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self.info: dict[str, Any] = {}

    async def send(self, t: str, d: Any = None, **extra: Any) -> None:
        if self.closed.is_set():
            raise WorkerLinkClosed("worker is not connected")
        frame = {"v": PROTOCOL_VERSION, "t": t, **extra}
        if d is not None:
            frame["d"] = d
        text = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        async with self._send_lock:
            try:
                await self.ws.send_text(text)
            except Exception as exc:
                self.closed.set()
                raise WorkerLinkClosed(str(exc)) from exc

    def send_nowait(self, t: str, d: Any = None) -> None:
        """Fire-and-forget send (errors are logged, not raised)."""
        async def _go() -> None:
            try:
                await self.send(t, d)
            except WorkerLinkClosed:
                pass
        asyncio.get_running_loop().create_task(_go())

    async def request(self, t: str, d: Any = None, timeout: float = 5.0) -> Any:
        rid = f"c{next(self._ids)}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self.send(t, d, id=rid)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def run(self) -> None:
        try:
            while True:
                message = await self.ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                text = message.get("text")
                if text is None:
                    continue
                try:
                    frame = json.loads(text)
                    t = frame["t"]
                    if not isinstance(t, str) or not isinstance(frame, dict):
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    log.warning("game %s: invalid frame from worker ignored", self.game_id)
                    continue
                if t == "w.reply":
                    fut = self._pending.get(str(frame.get("re")))
                    if fut is not None and not fut.done():
                        fut.set_result(frame.get("d"))
                    continue
                if self.handler is not None:
                    try:
                        await self.handler(t, frame.get("d"))
                    except Exception:
                        log.exception("game %s: error handling worker message %s", self.game_id, t)
        except WebSocketDisconnect:
            pass
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(WorkerLinkClosed("worker disconnected"))

    async def close(self) -> None:
        self.closed.set()
        if self.ws.application_state == WebSocketState.CONNECTED:
            try:
                await self.ws.close(1000)
            except Exception:
                pass


class WorkerBus:
    def __init__(self) -> None:
        self.port = 0
        self._expected: dict[bytes, tuple[str, asyncio.Future]] = {}
        self.app = Starlette(routes=[WebSocketRoute("/worker", self.endpoint)])

    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/worker"

    def expect(self, game_id: str) -> tuple[str, asyncio.Future]:
        token = secrets.token_urlsafe(32)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._expected[hashlib.sha256(token.encode()).digest()] = (game_id, fut)
        return token, fut

    def cancel(self, fut: asyncio.Future) -> None:
        for key, (_, f) in list(self._expected.items()):
            if f is fut:
                del self._expected[key]
        if not fut.done():
            fut.cancel()

    async def endpoint(self, ws: WebSocket) -> None:
        await ws.accept()
        try:
            message = await asyncio.wait_for(ws.receive(), HELLO_TIMEOUT_S)
            frame = json.loads(message.get("text") or "{}")
            d = frame.get("d") or {}
            token = str(d.get("token", ""))
            key = hashlib.sha256(token.encode()).digest()
        except Exception:
            await ws.close(4401)
            return
        match = None
        for k in list(self._expected):
            if hmac.compare_digest(k, key):
                match = self._expected.pop(k)  # one-time token
                break
        if frame.get("t") != "w.hello" or match is None or d.get("game_id") != match[0]:
            log.warning("rejected worker connection (bad token or game id)")
            await ws.close(4401)
            return
        game_id, fut = match
        if d.get("api") != 1:
            await ws.close(4400)
            if not fut.done():
                fut.set_exception(RuntimeError(f"worker uses unsupported API version {d.get('api')}"))
            return
        link = WorkerLink(ws, game_id)
        link.info = {"sdk": d.get("sdk")}
        if fut.done():  # adapter gave up waiting
            await ws.close(4408)
            return
        fut.set_result(link)
        await link.run()
