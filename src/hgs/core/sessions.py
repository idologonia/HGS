"""Session Manager: one authenticated connection of a player to one game.

A session can outlive a dropped connection for a short "resume window": the player stays
in the room and can come back with a one-time resume token (rotated on every use).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
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

    def __init__(self, *, game_id: str, player_id: str, name: str, ip: str, rate: float, burst: int,
                 max_message_bytes: int) -> None:
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
        self.closed = False  # permanently finished
        self.cleaned = False  # removed from rooms and the session list
        self.generation = 0
        self.proxied = False  # connected to a standalone game server (does not speak the HGS protocol)  # incremented on every resume (identifies the current connection)
        self.close_code: int | None = None
        self.detached_until: float | None = None
        self._resume_hash: bytes | None = None
        self._outbox: asyncio.Queue | None = None
        self._writer: asyncio.Task | None = None
        self._send_text: Callable[[str], Awaitable[None]] | None = None
        self._close_ws: Callable[[int, str], Awaitable[None]] | None = None
        # called when a detached session is closed/expired (nobody else will clean it up)
        self.on_dead: Callable[["Session"], None] | None = None

    def public(self) -> dict:
        return {"id": self.player_id, "name": self.name}

    @property
    def detached(self) -> bool:
        return self.detached_until is not None

    # -- connection attach/detach -------------------------------------------------
    def attach(self, send_text: Callable[[str], Awaitable[None]], close_ws: Callable[[int, str], Awaitable[None]]) -> None:
        self._send_text = send_text
        self._close_ws = close_ws
        self._outbox = asyncio.Queue()
        self.detached_until = None
        self.bad_messages = 0
        self._writer = asyncio.create_task(self._write_loop(self._outbox), name=f"ws-writer-{self.id}")

    def detach(self, window_s: float) -> None:
        """The connection dropped: keep the session for `window_s` seconds."""
        if self._writer is not None:
            self._writer.cancel()
        self._writer = None
        self._outbox = None
        self.detached_until = time.time() + window_s

    def new_resume_token(self) -> str:
        token = secrets.token_urlsafe(32)
        self._resume_hash = hashlib.sha256(token.encode()).digest()
        return token

    def check_resume_token(self, token: str) -> bool:
        if self._resume_hash is None:
            return False
        ok = hmac.compare_digest(self._resume_hash, hashlib.sha256(token.encode()).digest())
        if ok:
            self._resume_hash = None  # one-time
        return ok

    # -- sending ------------------------------------------------------------------
    def send(self, text: str) -> None:
        if self.closed or self._outbox is None:
            return  # detached: messages are dropped (the room state is re-sent on resume)
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
        if self._outbox is not None:
            self._outbox.put_nowait((_CLOSE, code, reason))
        elif self.on_dead is not None:
            self.on_dead(self)

    async def _write_loop(self, outbox: asyncio.Queue) -> None:
        try:
            while True:
                item = await outbox.get()
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
                    return
        except asyncio.CancelledError:
            pass

    async def wait_closed(self, timeout: float = 5.0) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(writer), timeout)
        except (asyncio.TimeoutError, Exception):
            writer.cancel()


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

    def get(self, session_id: str) -> Session | None:
        return self._by_id.get(session_id)

    def get_player(self, game_id: str, player_id: str) -> Session | None:
        return self._by_player.get((game_id, player_id))

    def for_game(self, game_id: str) -> list[Session]:
        return [s for s in self._by_id.values() if s.game_id == game_id]

    def all(self) -> list[Session]:
        return list(self._by_id.values())

    def expired(self, now: float | None = None) -> list[Session]:
        now = time.time() if now is None else now
        return [s for s in self._by_id.values() if s.detached_until is not None and s.detached_until <= now]

    def count(self, game_id: str | None = None) -> int:
        if game_id is None:
            return len(self._by_id)
        return sum(1 for s in self._by_id.values() if s.game_id == game_id)
