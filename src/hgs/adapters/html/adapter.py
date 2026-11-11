"""HTML Adapter — for browser games hosted on GitHub Pages or any static hosting.

The game client stays where it is (e.g. GitHub Pages) and talks to HGS through hgs.js.
Backend modes:

* "relay" — no server code. The core forwards game events between players of the same
  room and keeps an optional shared room state. Limitation: the server does not check
  game rules, so fairness relies on the clients.
* "logic" — the game's server logic (a Python class based on hgs_sdk.GameLogic) runs in
  a separate worker process. Only the Python standard library + hgs_sdk are available
  to it (the Python adapter, Stage 5, supports projects with their own virtualenv).
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
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
from hgs.adapters.worker_backend import WorkerBackend
from hgs.core.config.manifest import GameManifest
from hgs.core.process_supervisor import LaunchSpec

SUPPORTED_MODES = ("relay", "logic")
CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def worker_command(*args: str) -> tuple[str, list[str]]:
    """Command line that starts an HGS worker with the same Python as the core.

    -I (isolated mode) keeps files in the game folder from shadowing HGS modules.
    """
    if getattr(sys, "frozen", False):  # PyInstaller build: hgs.exe --worker ...
        return sys.executable, ["--worker", *args]
    return sys.executable, ["-I", "-m", "hgs", "--worker", *args]


class HtmlAdapter(BaseAdapter):
    adapter_type = "html"
    description = "Browser (HTML/JS) games using the hgs.js SDK"

    def __init__(self, ctx, manifest: GameManifest) -> None:
        super().__init__(ctx, manifest)
        self.mode = (manifest.backend or {}).get("mode", "relay")
        self.started_at: float | None = None
        self.events_relayed = 0
        self.worker: WorkerBackend | None = None

    # -- configuration --------------------------------------------------------------
    @classmethod
    def validate_config(cls, manifest: GameManifest) -> ValidationResult:
        backend = manifest.backend or {}
        mode = backend.get("mode", "relay")
        errors: list[str] = []
        if mode not in SUPPORTED_MODES:
            return ValidationResult.fail(f"backend.mode must be one of {SUPPORTED_MODES}, got {mode!r}")
        allowed = {"mode"} if mode == "relay" else {"mode", "module", "class", "config"}
        unknown = set(backend) - allowed
        if unknown:
            errors.append(f"unknown backend fields for {mode} mode: {sorted(unknown)}")
        if mode == "logic":
            module = backend.get("module")
            if not isinstance(module, str) or not module.endswith(".py"):
                errors.append("backend.module must be a path to a .py file inside the game folder, e.g. 'server/logic.py'")
            elif Path(module).is_absolute() or ".." in Path(module).parts:
                errors.append("backend.module must be a relative path inside the game folder")
            if not isinstance(backend.get("class", "Game"), str) or not CLASS_RE.match(backend.get("class", "Game")):
                errors.append("backend.class must be a Python class name")
            if not isinstance(backend.get("config", {}), dict):
                errors.append("backend.config must be an object")
        return ValidationResult(not errors, errors)

    def capabilities(self) -> frozenset[Capability]:
        if self.mode == "logic":
            return frozenset({Capability.ROOMS, Capability.SERVER_LOGIC, Capability.SAVE_STATE})
        return frozenset({Capability.ROOMS, Capability.ROOM_STATE})

    # -- lifecycle ------------------------------------------------------------------
    async def initialize(self) -> None:
        self.ctx.paths.ensure()
        if self.mode == "logic":
            backend = self.manifest.backend
            root = self.ctx.game_root
            entry = (root / backend["module"]).resolve()
            program, args = worker_command("--root", str(root), "--entry", f"{backend['module']}:{backend.get('class', 'Game')}")
            spec = LaunchSpec(program=program, args=args, cwd=root, game_root=root, entry=entry,
                              memory_mb=self.manifest.limits.memory_mb)
            self.worker = WorkerBackend(self.ctx, self.manifest, spec, backend.get("config", {}))

    async def start(self) -> None:
        self.state = AdapterState.STARTING
        if self.worker is not None:
            await self.worker.start()
        self.state = AdapterState.RUNNING
        self.started_at = time.time()
        self.ctx.log.info("%s backend ready", self.mode)

    async def stop(self, reason: str, grace_s: float) -> None:
        self.state = AdapterState.STOPPING
        if self.worker is not None:
            await self.worker.stop(reason, grace_s)
        self.state = AdapterState.STOPPED
        self.ctx.log.info("%s backend stopped (%s)", self.mode, reason)

    def get_status(self) -> AdapterStatus:
        return AdapterStatus(self.state, details={"mode": self.mode})

    def get_metrics(self) -> AdapterMetrics:
        if self.worker is not None:
            m = self.worker.metrics()
            cpu, mem = m.pop("cpu_percent", None), m.pop("memory_bytes", None)
            return AdapterMetrics(cpu_percent=cpu, memory_bytes=mem, custom=m)
        return AdapterMetrics(custom={"events_relayed": self.events_relayed})

    async def save(self, reason: str) -> dict | None:
        return await self.worker.save(reason) if self.worker else None

    # -- players --------------------------------------------------------------------
    async def handle_connection(self, player: PlayerRef, room: RoomRef) -> JoinDecision:
        if self.worker is not None:
            return await self.worker.handle_connection(player, room)
        return JoinDecision(True)

    async def handle_message(self, player: PlayerRef, room: RoomRef, event: str, data: Any, to: str | None) -> None:
        if self.worker is not None:
            await self.worker.handle_message(player, room, event, data, to)
            return
        self.events_relayed += 1
        if to is not None:
            self.ctx.send_to_player(room.room_id, to, event, data, sender=player.player_id)
        else:
            self.ctx.send_to_room(room.room_id, event, data, sender=player.player_id, exclude=player.player_id)

    async def handle_disconnect(self, player: PlayerRef, room: RoomRef, reason: str) -> None:
        if self.worker is not None:
            await self.worker.handle_disconnect(player, room, reason)

    async def handle_room_event(self, room: RoomRef, event: str) -> None:
        if self.worker is not None:
            await self.worker.handle_room_event(room, event)

    async def shutdown(self) -> None:
        if self.worker is not None and not self.worker.stopping:
            await self.worker.stop("shutdown", 0)
        self.state = AdapterState.STOPPED
