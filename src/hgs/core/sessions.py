"""Session Manager: one authenticated WebSocket connection of a player to one game."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import Counter
from typing import Awaitable, Callable

from .ratelimit import TokenBucket

log = logging.getLogger("hgs.sessions")

_CLOSE = object()


class Session:
    """Outgoing messages go through a bounded queue drained by one writer task,
    so a slow client can never block the game or other players."""

    OUTBOX_LIMIT = 512

    def __init__(
        self,
        *,
        game_id: str,
        player_id: str,
        name: str,
        ip: str,
        send_text: Callable[[str], Awaitable[None]],
        close_ws: Callable[[int, str], Awaitable[None]],
        rate: float,
        burst: int,
        max_message_bytes: int,
    ) -> None:
        self.id = "s_" + secrets.token_urlsafe(12)
        self.game_id = game_id
        self.player_id = player_id
        self.name = name
        self.ip = ip
        self.room_id: str | None = None
        self.connected_at = time.time()
        self.last_activity = self.connected_at
        self.last_seq = 0
        self.bad_messages = 0
        self.bucket = TokenBucket(rate, burst)
        self.max_message_bytes = max_message_bytes
        self.messages_in = 0
        self.messages_out = 0
        self.closed = False
        self.close_code: int | None = None
        self._send_text = send_text
        self._close_ws = close_ws
        self._outbox: asyncio.Queue = asyncio.Queue()
        self._writer = asyncio.create_task(self._write_loop(), name=f"ws-writer-{self.id}")

    def public(self) -> dict:
        return {"id": self.player_id, "name": self.name}

    def send(self, text: str) -> None:
        if self.closed:
            return
        if self._outbox.qsize() >= self.OUTBOX_LIMIT:
            log.warning("session %s outbox overflow, disconnecting slow client", self.id)
            self.close(1013, "client too slow")
            return
        self._outbox.put_nowait(text)

    def close(self, code: int, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        self.close_code = code
        self._outbox.put_nowait((_CLOSE, code, reason))

    async def _write_loop(self) -> None:
        try:
            while True:
                item = await self._outbox.get()
                if isinstance(item, tuple) and item and item[0] is _CLOSE:
                    _, code, reason = item
                    try:
                        await self._close_ws(code, reason[:120])
                    except Exception:
                        pass
                    return
                try:
                    await self._send_text(item)
                    self.messages_out += 1
                except Exception:
                    self.closed = True
                    return
        except asyncio.CancelledError:
            pass

    async def wait_closed(self, timeout: float = 5.0) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(self._writer), timeout)
        except (asyncio.TimeoutError, Exception):
            self._writer.cancel()


class SessionManager:
    def __init__(self) -> None:
        self._by_id: dict[str, Session] = {}
        self._by_player: dict[tuple[str, str], Session] = {}
        self._per_ip: Counter[str] = Counter()

    def ip_connections(self, ip: str) -> int:
        return self._per_ip[ip]

    def ip_acquire(self, ip: str) -> None:
        self._per_ip[ip] += 1

    def ip_release(self, ip: str) -> None:
        self._per_ip[ip] -= 1
        if self._per_ip[ip] <= 0:
            del self._per_ip[ip]

    def add(self, s: Session) -> Session | None:
        """Register a session. Returns the previous session of the same player (to be replaced)."""
        old = self._by_player.get((s.game_id, s.player_id))
        self._by_id[s.id] = s
        self._by_player[(s.game_id, s.player_id)] = s
        return old

    def remove(self, s: Session) -> None:
        self._by_id.pop(s.id, None)
        if self._by_player.get((s.game_id, s.player_id)) is s:
            del self._by_player[(s.game_id, s.player_id)]

    def get_player(self, game_id: str, player_id: str) -> Session | None:
        return self._by_player.get((game_id, player_id))

    def for_game(self, game_id: str) -> list[Session]:
        return [s for s in self._by_id.values() if s.game_id == game_id]

    def all(self) -> list[Session]:
        return list(self._by_id.values())

    def count(self, game_id: str | None = None) -> int:
        if game_id is None:
            return len(self._by_id)
        return sum(1 for s in self._by_id.values() if s.game_id == game_id)
