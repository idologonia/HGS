"""Room Manager.

A room belongs to exactly one game: rooms are stored per game_id, and every lookup
goes through the game_id of the caller, so events can never cross games or rooms.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from .protocol.client import msg

if TYPE_CHECKING:
    from .events import EventBus
    from .sessions import Session


class RoomError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Room:
    game_id: str
    room_id: str
    name: str
    max_players: int
    params: dict[str, Any] = field(default_factory=dict)
    private: bool = False
    persistent: bool = False  # default/admin rooms are not auto-closed when empty
    status: str = "open"  # open | locked | closed
    created_by: str | None = None
    created_at: float = field(default_factory=time.time)
    empty_since: float | None = field(default_factory=time.time)
    players: dict[str, "Session"] = field(default_factory=dict)
    state: Any = None  # shared room state (relay mode)
    host: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.room_id,
            "name": self.name,
            "max_players": self.max_players,
            "players": len(self.players),
            "status": self.status,
            "params": self.params,
        }

    def admin_view(self) -> dict[str, Any]:
        d = self.public()
        d.update(
            game_id=self.game_id,
            private=self.private,
            persistent=self.persistent,
            host=self.host,
            created_at=self.created_at,
            created_by=self.created_by,
            player_list=[s.public() for s in self.players.values()],
        )
        return d

    def broadcast(self, text: str, exclude: "str | set[str] | None" = None) -> None:
        skip = {exclude} if isinstance(exclude, str) else (exclude or set())
        for pid, s in list(self.players.items()):
            if pid not in skip:
                s.send(text)


class RoomManager:
    def __init__(self, bus: "EventBus") -> None:
        self.bus = bus
        self._rooms: dict[str, dict[str, Room]] = {}
        # listener(kind, room) with kind "created" | "closed"; set by the Server Manager
        self.listener: Callable[[str, Room], None] | None = None

    def _notify(self, kind: str, room: Room) -> None:
        if self.listener is not None:
            try:
                self.listener(kind, room)
            except Exception:
                pass

    # -- queries --------------------------------------------------------------------
    def get(self, game_id: str, room_id: str) -> Room | None:
        return self._rooms.get(game_id, {}).get(room_id)

    def list(self, game_id: str | None = None) -> list[Room]:
        if game_id is not None:
            return list(self._rooms.get(game_id, {}).values())
        return [r for rooms in self._rooms.values() for r in rooms.values()]

    # -- lifecycle ------------------------------------------------------------------
    def create(
        self,
        game_id: str,
        name: str,
        *,
        max_players: int,
        max_rooms: int,
        room_id: str | None = None,
        params: dict[str, Any] | None = None,
        private: bool = False,
        persistent: bool = False,
        created_by: str | None = None,
    ) -> Room:
        rooms = self._rooms.setdefault(game_id, {})
        if len(rooms) >= max_rooms:
            raise RoomError("forbidden", f"room limit reached ({max_rooms})")
        if room_id is None:
            room_id = "r_" + secrets.token_hex(4)
            while room_id in rooms:
                room_id = "r_" + secrets.token_hex(4)
        elif room_id in rooms:
            raise RoomError("bad_request", f"room {room_id!r} already exists")
        room = Room(
            game_id=game_id,
            room_id=room_id,
            name=name,
            max_players=max_players,
            params=dict(params or {}),
            private=private,
            persistent=persistent,
            created_by=created_by,
        )
        rooms[room_id] = room
        self.bus.publish("room.created", game_id=game_id, room=room.public())
        self._notify("created", room)
        return room

    def join(self, session: "Session", room_id: str) -> Room:
        room = self.get(session.game_id, room_id)
        if room is None or room.status == "closed":
            raise RoomError("room_not_found", "room not found")
        if session.player_id in room.players:
            return room
        if room.status == "locked":
            raise RoomError("room_closed", "room is locked")
        if len(room.players) >= room.max_players:
            raise RoomError("room_full", "room is full")
        if session.room_id is not None:
            self.leave(session, "switched_room")
        room.players[session.player_id] = session
        room.empty_since = None
        session.room_id = room.room_id
        if room.host is None:
            room.host = session.player_id
        room.broadcast(msg("room.player_joined", {"player": session.public()}), exclude=session.player_id)
        self.bus.publish("room.updated", game_id=room.game_id, room=room.public())
        return room

    def joined_payload(self, room: Room) -> dict[str, Any]:
        return {
            "room": {"id": room.room_id, "name": room.name, "max_players": room.max_players, "params": room.params},
            "players": [s.public() for s in room.players.values()],
            "state": room.state,
            "host": room.host,
        }

    def leave(self, session: "Session", reason: str) -> Room | None:
        if session.room_id is None:
            return None
        room = self.get(session.game_id, session.room_id)
        session.room_id = None
        if room is None or room.players.get(session.player_id) is not session:
            return None
        del room.players[session.player_id]
        room.broadcast(msg("room.player_left", {"player": session.public(), "reason": reason}))
        if room.host == session.player_id:
            room.host = next(iter(room.players), None)
            if room.host is not None:
                room.broadcast(msg("room.host_changed", {"host": room.host}))
        if not room.players:
            room.empty_since = time.time()
        self.bus.publish("room.updated", game_id=room.game_id, room=room.public())
        return room

    def close(self, game_id: str, room_id: str, reason: str) -> Room | None:
        room = self._rooms.get(game_id, {}).pop(room_id, None)
        if room is None:
            return None
        room.status = "closed"
        room.broadcast(msg("room.closed", {"reason": reason}))
        for s in room.players.values():
            s.room_id = None
        room.players.clear()
        self.bus.publish("room.closed", game_id=game_id, room_id=room_id, reason=reason)
        self._notify("closed", room)
        return room

    def close_all(self, game_id: str, reason: str) -> None:
        # The game itself is stopping: no per-room notifications to its adapter.
        listener, self.listener = self.listener, None
        try:
            for room_id in list(self._rooms.get(game_id, {})):
                self.close(game_id, room_id, reason)
        finally:
            self.listener = listener
        self._rooms.pop(game_id, None)

    def update(self, game_id: str, room_id: str, *, status: str | None = None, params: dict | None = None,
               max_players: int | None = None) -> Room | None:
        room = self.get(game_id, room_id)
        if room is None:
            return None
        if status in ("open", "locked"):
            room.status = status
        if isinstance(params, dict) and json_size(params) <= 4096:
            room.params = params
        if isinstance(max_players, int) and 1 <= max_players <= 1000:
            room.max_players = max_players
        self.bus.publish("room.updated", game_id=game_id, room=room.public())
        return room

    def reap_empty(self, ttl_by_game: dict[str, int], now: float | None = None) -> list[Room]:
        """Close non-persistent rooms that stayed empty longer than the game's TTL."""
        now = time.time() if now is None else now
        closed = []
        for game_id, rooms in list(self._rooms.items()):
            ttl = ttl_by_game.get(game_id)
            if ttl is None:
                continue
            for room in list(rooms.values()):
                if not room.persistent and room.empty_since is not None and now - room.empty_since >= ttl:
                    self.close(game_id, room.room_id, "empty")
                    closed.append(room)
        return closed


def json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
