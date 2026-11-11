"""HTML Adapter — for browser games hosted on GitHub Pages or any static hosting.

Stage 2 implements backend mode "relay": no server-side game code. The core forwards
game events between players of the same room and keeps an optional shared room state.
Mode "logic" (Python game logic in a worker process) arrives in Stage 3.

Honest limitation of relay: the server does not check game rules, so fairness relies on clients.
"""

from __future__ import annotations

import time
from typing import Any

from hgs.adapters.base import (
    AdapterMetrics,
    AdapterState,
    AdapterStatus,
    BaseAdapter,
    Capability,
    JoinDecision,
    PlayerRef,
    RoomRef,
    ValidationResult,
)
from hgs.core.config.manifest import GameManifest

SUPPORTED_MODES = ("relay",)


class HtmlAdapter(BaseAdapter):
    adapter_type = "html"
    description = "Browser (HTML/JS) games using the hgs.js SDK"

    def __init__(self, ctx, manifest: GameManifest) -> None:
        super().__init__(ctx, manifest)
        self.started_at: float | None = None
        self.events_relayed = 0

    @classmethod
    def validate_config(cls, manifest: GameManifest) -> ValidationResult:
        backend = manifest.backend or {}
        mode = backend.get("mode", "relay")
        errors = []
        if mode not in SUPPORTED_MODES:
            if mode == "logic":
                errors.append("backend.mode 'logic' is not available yet (planned for Stage 3); use 'relay'")
            else:
                errors.append(f"backend.mode must be one of {SUPPORTED_MODES}, got {mode!r}")
        unknown = set(backend) - {"mode"}
        if unknown:
            errors.append(f"unknown backend fields for relay mode: {sorted(unknown)}")
        return ValidationResult(not errors, errors)

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.ROOMS, Capability.ROOM_STATE})

    async def initialize(self) -> None:
        self.ctx.paths.ensure()

    async def start(self) -> None:
        self.state = AdapterState.RUNNING
        self.started_at = time.time()
        self.ctx.log.info("relay backend ready")

    async def stop(self, reason: str, grace_s: float) -> None:
        self.state = AdapterState.STOPPED
        self.ctx.log.info("relay backend stopped (%s)", reason)

    def get_status(self) -> AdapterStatus:
        return AdapterStatus(self.state, details={"mode": "relay"})

    def get_metrics(self) -> AdapterMetrics:
        return AdapterMetrics(custom={"events_relayed": self.events_relayed})

    async def handle_connection(self, player: PlayerRef, room: RoomRef) -> JoinDecision:
        return JoinDecision(True)

    async def handle_message(self, player: PlayerRef, room: RoomRef, event: str, data: Any, to: str | None) -> None:
        self.events_relayed += 1
        if to is not None:
            self.ctx.send_to_player(room.room_id, to, event, data, sender=player.player_id)
        else:
            self.ctx.send_to_room(room.room_id, event, data, sender=player.player_id, exclude=player.player_id)

    async def handle_disconnect(self, player: PlayerRef, room: RoomRef, reason: str) -> None:
        pass

    async def shutdown(self) -> None:
        self.state = AdapterState.STOPPED
