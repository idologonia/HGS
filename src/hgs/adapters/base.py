"""Adapter contract (API version 1). See docs/architecture/03-adapters.md.

An adapter connects the core to one kind of game server. The core calls the same
interface for every game; the adapter knows how to run that game and pass events to it.

Rules for adapters:
* never block the event loop (heavy work belongs in worker processes or to_thread);
* raise exceptions freely — the Server Manager catches them and only this game is affected;
* use only the AdapterContext: it is scoped to this game.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol

from hgs.common.paths import GamePaths
from hgs.core.config.manifest import GameManifest

ADAPTER_API_VERSION = 1


class AdapterState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    CRASHED = "crashed"
    FAILED = "failed"
    ERROR = "error"


class Capability(StrEnum):
    ROOMS = "rooms"
    ROOM_STATE = "room_state"  # shared room state managed by the core (relay)
    SERVER_LOGIC = "server_logic"
    SAVE_STATE = "save_state"
    HOT_RESTART = "hot_restart"
    PROXY = "proxy"


@dataclass(frozen=True)
class AdapterStatus:
    state: AdapterState
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterMetrics:
    cpu_percent: float | None = None
    memory_bytes: int | None = None
    custom: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)

    @classmethod
    def success(cls) -> "ValidationResult":
        return cls(True)

    @classmethod
    def fail(cls, *errors: str) -> "ValidationResult":
        return cls(False, list(errors))


@dataclass(frozen=True)
class PlayerRef:
    player_id: str
    name: str


@dataclass(frozen=True)
class RoomRef:
    room_id: str
    name: str
    params: dict[str, Any]


@dataclass(frozen=True)
class JoinDecision:
    accept: bool = True
    reason: str | None = None


class AdapterContext(Protocol):
    """What an adapter may do. Every operation is limited to the adapter's own game."""

    game_id: str
    game_root: Path
    paths: GamePaths
    log: logging.Logger

    def send_to_room(self, room_id: str, event: str, data: Any, *, sender: str | None = None,
                     exclude: str | None = None) -> None: ...

    def send_to_player(self, room_id: str, player_id: str, event: str, data: Any, *,
                       sender: str | None = None) -> bool: ...

    def room_players(self, room_id: str) -> list[PlayerRef]: ...

    def report_crash(self, error: str) -> None:
        """Tell the core that the game server died unexpectedly (restart policy applies)."""


class BaseAdapter(ABC):
    adapter_type: ClassVar[str]
    api_version: ClassVar[int] = ADAPTER_API_VERSION
    description: ClassVar[str] = ""

    def __init__(self, ctx: AdapterContext, manifest: GameManifest) -> None:
        self.ctx = ctx
        self.manifest = manifest
        self.state = AdapterState.STOPPED

    # -- configuration --------------------------------------------------------------
    @classmethod
    @abstractmethod
    def validate_config(cls, manifest: GameManifest) -> ValidationResult:
        """Check the adapter-specific configuration without side effects."""

    # -- lifecycle ------------------------------------------------------------------
    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self, reason: str, grace_s: float) -> None: ...

    async def restart(self) -> None:
        await self.stop("restart", 5.0)
        await self.start()

    @abstractmethod
    def get_status(self) -> AdapterStatus: ...

    @abstractmethod
    def get_metrics(self) -> AdapterMetrics: ...

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.ROOMS})

    # -- player events --------------------------------------------------------------
    @abstractmethod
    async def handle_connection(self, player: PlayerRef, room: RoomRef) -> JoinDecision: ...

    @abstractmethod
    async def handle_message(self, player: PlayerRef, room: RoomRef, event: str, data: Any,
                             to: str | None) -> None: ...

    @abstractmethod
    async def handle_disconnect(self, player: PlayerRef, room: RoomRef, reason: str) -> None: ...

    async def handle_room_event(self, room: RoomRef, event: str) -> None:
        """created / closed — nothing by default."""

    async def save(self, reason: str) -> dict | None:
        """Ask the game to save its state. None = not supported."""
        return None

    @abstractmethod
    async def shutdown(self) -> None:
        """Release everything (called when HGS exits)."""


class AdapterError(Exception):
    """Expected adapter failure with a message that is safe to show to the user."""
