"""Game-facing objects: GameLogic, Room, Player."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .connection import Connection


class Player:
    __slots__ = ("id", "name", "data")

    def __init__(self, id: str, name: str) -> None:
        self.id = id
        self.name = name
        self.data: dict[str, Any] = {}  # free storage for the game

    def __repr__(self) -> str:
        return f"Player({self.id!r}, {self.name!r})"


class Room:
    """One room of the game. Messages sent through a room reach only its players."""

    def __init__(self, conn: "Connection", id: str, name: str, params: dict[str, Any] | None = None,
                 max_players: int | None = None) -> None:
        self._conn = conn
        self.id = id
        self.name = name
        self.params: dict[str, Any] = dict(params or {})
        self.max_players = max_players
        self.players: dict[str, Player] = {}
        self.state: Any = None  # owned by the game; HGS never looks inside

    def __repr__(self) -> str:
        return f"Room({self.id!r}, players={len(self.players)})"

    def broadcast(self, event: str, data: Any = None, *, exclude: "Player | list[Player] | None" = None) -> None:
        ex = [exclude] if isinstance(exclude, Player) else (exclude or [])
        self._conn.post("w.send", {"room_id": self.id, "e": event, "d": data, "except": [p.id for p in ex]})

    def send(self, player: "Player | list[Player]", event: str, data: Any = None) -> None:
        to = player if isinstance(player, list) else [player]
        self._conn.post("w.send", {"room_id": self.id, "e": event, "d": data, "to": [p.id for p in to]})

    def kick(self, player: Player, reason: str = "kicked by game") -> None:
        self._conn.post("w.kick", {"room_id": self.id, "player_id": player.id, "reason": reason})

    def close(self, reason: str = "closed by game") -> None:
        self._conn.post("w.room.close", {"room_id": self.id, "reason": reason})

    def update(self, *, status: str | None = None, params: dict | None = None, max_players: int | None = None) -> None:
        """status: "open" or "locked" (no new players)."""
        if params is not None:
            self.params = dict(params)
        self._conn.post("w.room.update", {"room_id": self.id, "status": status, "params": params,
                                          "max_players": max_players})


class GameLogic:
    """Base class for a game's server logic. Override the hooks you need.

    All hooks are async. An exception in a hook is logged and does not stop the server.
    """

    #: how many times per second on_tick() is called for each room with players (0 = never)
    tick_rate: float = 0

    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}
        self.config: dict[str, Any] = {}  # backend.config from hgs-game.json
        self.log = logging.getLogger("game")
        self._conn: "Connection | None" = None

    def set_status(self, **custom: Any) -> None:
        """Show custom numbers in the HGS GUI (Status dialog), e.g. set_status(matches=3)."""
        if self._conn is not None:
            self._conn.post("w.status", {"custom": custom})

    # -- lifecycle ---------------------------------------------------------------
    async def on_load(self, data: Any) -> None:
        """Restore state saved by on_save() (called before on_start when a snapshot exists)."""

    async def on_start(self) -> None: ...

    async def on_save(self) -> Any:
        """Return JSON-compatible data (or bytes) to save, or None if the game has nothing to save."""
        return None

    async def on_shutdown(self) -> None: ...

    # -- rooms and players -------------------------------------------------------
    async def on_room_created(self, room: Room) -> None: ...

    async def on_room_closed(self, room: Room) -> None: ...

    async def on_join(self, room: Room, player: Player) -> "bool | str | None":
        """Return False or a reason string to refuse the player; anything else accepts."""
        return True

    async def on_event(self, room: Room, player: Player, event: str, data: Any) -> None: ...

    async def on_leave(self, room: Room, player: Player, reason: str) -> None: ...

    async def on_tick(self, room: Room, dt: float) -> None: ...
