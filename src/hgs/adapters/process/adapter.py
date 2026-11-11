"""Process adapter — run a game server written in any language.

backend.mode:
* "worker"     — the program speaks the Worker Protocol (docs 06, §6.4) using HGS_WORKER_URL,
                 HGS_WORKER_TOKEN and HGS_GAME_ID from its environment; HGS does login, rooms, security.
* "standalone" — an existing server with its own port/protocol; HGS manages the process and
                 proxies WebSocket connections to it.

The program must be inside the game folder or approved by the owner (allowlist).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from hgs.adapters.base import (
    AdapterError,
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
from hgs.adapters.standalone_backend import StandaloneBackend, free_port
from hgs.adapters.worker_backend import WorkerBackend
from hgs.core.config.manifest import GameManifest
from hgs.core.process_supervisor import LaunchSpec

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
ALLOWED_PLACEHOLDERS = {"port", "data_dir", "game_id", "game_dir"}
COMMON_FIELDS = {"mode", "args", "env", "port", "ready", "proxy", "config"}


def validate_common(backend: dict[str, Any], errors: list[str]) -> None:
    mode = backend.get("mode")
    if mode not in ("worker", "standalone"):
        errors.append("backend.mode must be 'worker' or 'standalone'")
    args = backend.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) and len(a) < 1000 for a in args) or len(args) > 64:
        errors.append("backend.args must be a list of strings")
    else:
        for a in args:
            for name in PLACEHOLDER.findall(a):
                if name not in ALLOWED_PLACEHOLDERS:
                    errors.append(f"unknown placeholder {{{name}}} in backend.args")
    env = backend.get("env", {})
    if not isinstance(env, dict) or len(env) > 32 or not all(
            isinstance(k, str) and ENV_NAME.match(k) and not k.upper().startswith("HGS_") and isinstance(v, str)
            and len(v) < 4000 for k, v in env.items()):
        errors.append("backend.env must map NAME -> string (names starting with HGS_ are reserved)")
    port = backend.get("port", "auto")
    if not (port == "auto" or (isinstance(port, int) and 1024 <= port <= 65535)):
        errors.append("backend.port must be 'auto' or a number 1024..65535")
    ready = backend.get("ready", {"type": "tcp"})
    if not isinstance(ready, dict) or ready.get("type", "tcp") not in ("tcp", "none") or not (
            1 <= float(ready.get("timeout_s", 20)) <= 300):
        errors.append("backend.ready must be {type: 'tcp'|'none', timeout_s: 1..300}")
    proxy = backend.get("proxy", {})
    if not isinstance(proxy, dict) or set(proxy) - {"require_login"}:
        errors.append("backend.proxy may only contain require_login")
    if not isinstance(backend.get("config", {}), dict):
        errors.append("backend.config must be an object")


class ProcessAdapter(BaseAdapter):
    adapter_type = "process"
    description = "Any program (Node.js, Go, C#, ...): Worker Protocol or standalone server"
    EXTRA_FIELDS = {"command"}

    def __init__(self, ctx, manifest: GameManifest) -> None:
        super().__init__(ctx, manifest)
        self.backend_cfg = manifest.backend or {}
        self.mode = self.backend_cfg.get("mode")
        self.worker: WorkerBackend | None = None
        self.standalone: StandaloneBackend | None = None
        self.port: int | None = None

    # -- configuration --------------------------------------------------------------
    @classmethod
    def validate_config(cls, manifest: GameManifest) -> ValidationResult:
        backend = manifest.backend or {}
        errors: list[str] = []
        validate_common(backend, errors)
        unknown = set(backend) - COMMON_FIELDS - cls.EXTRA_FIELDS
        if unknown:
            errors.append(f"unknown backend fields: {sorted(unknown)}")
        cls.validate_specific(backend, errors)
        return ValidationResult(not errors, errors)

    @classmethod
    def validate_specific(cls, backend: dict[str, Any], errors: list[str]) -> None:
        cmd = backend.get("command")
        if not isinstance(cmd, str) or not cmd or len(cmd) > 500:
            errors.append("backend.command must be a program name or path (e.g. 'node' or 'bin/server.exe')")

    def capabilities(self) -> frozenset[Capability]:
        if self.mode == "standalone":
            return frozenset({Capability.PROXY})
        return frozenset({Capability.ROOMS, Capability.SERVER_LOGIC, Capability.SAVE_STATE})

    # -- launch spec ----------------------------------------------------------------
    def resolve_program(self) -> tuple[str, list[str], Path | None]:
        """Returns (program, leading args, entry file to check)."""
        cmd = self.backend_cfg["command"]
        root = self.ctx.game_root
        candidate = (root / cmd)
        if ("/" in cmd or "\\" in cmd) and candidate.is_file():
            return str(candidate.resolve()), [], None
        found = shutil.which(cmd)
        if found is None:
            raise AdapterError(f"program not found: {cmd}")
        return str(Path(found).resolve()), [], None

    def _expand(self, args: list[str]) -> list[str]:
        values = {"port": str(self.port or ""), "data_dir": str(self.ctx.paths.root), "game_id": self.ctx.game_id,
                  "game_dir": str(self.ctx.game_root)}
        return [PLACEHOLDER.sub(lambda m: values[m.group(1)], a) for a in args]

    def extra_env(self) -> dict[str, str]:
        return {}

    async def initialize(self) -> None:
        self.ctx.paths.ensure()
        program, lead, entry = self.resolve_program()
        if self.mode == "standalone":
            port = self.backend_cfg.get("port", "auto")
            self.port = free_port() if port == "auto" else int(port)
        env = {**self.backend_cfg.get("env", {}), **self.extra_env()}
        if self.port:
            env["PORT"] = str(self.port)
        spec = LaunchSpec(program=program, args=[*lead, *self._expand(self.backend_cfg.get("args", []))],
                          cwd=self.ctx.game_root, game_root=self.ctx.game_root, entry=entry, env=env,
                          memory_mb=self.manifest.limits.memory_mb)
        if self.mode == "standalone":
            self.standalone = StandaloneBackend(self.ctx, spec, self.port, self.backend_cfg.get("ready", {"type": "tcp"}))
        else:
            self.worker = WorkerBackend(self.ctx, self.manifest, spec, self.backend_cfg.get("config", {}))

    # -- lifecycle ------------------------------------------------------------------
    async def start(self) -> None:
        self.state = AdapterState.STARTING
        await (self.standalone or self.worker).start()
        self.state = AdapterState.RUNNING

    async def stop(self, reason: str, grace_s: float) -> None:
        self.state = AdapterState.STOPPING
        backend = self.standalone or self.worker
        if backend is not None:
            await backend.stop(reason, grace_s)
        self.state = AdapterState.STOPPED

    def get_status(self) -> AdapterStatus:
        return AdapterStatus(self.state, details={"mode": self.mode, "port": self.port})

    def get_metrics(self) -> AdapterMetrics:
        backend = self.standalone or self.worker
        m = backend.metrics() if backend else {}
        cpu, mem = m.pop("cpu_percent", None), m.pop("memory_bytes", None)
        return AdapterMetrics(cpu_percent=cpu, memory_bytes=mem, custom=m)

    def proxy_target(self) -> tuple[int, bool] | None:
        """(port, require_login) for the gateway proxy, or None."""
        if self.mode != "standalone" or self.port is None or self.state != AdapterState.RUNNING:
            return None
        return self.port, bool(self.backend_cfg.get("proxy", {}).get("require_login", False))

    async def save(self, reason: str) -> dict | None:
        return await self.worker.save(reason) if self.worker else None

    # -- players (worker mode) ------------------------------------------------------
    async def handle_connection(self, player: PlayerRef, room: RoomRef) -> JoinDecision:
        if self.worker is None:
            return JoinDecision(False, "this game uses its own protocol: connect through /hgs/v1/proxy/")
        return await self.worker.handle_connection(player, room)

    async def handle_message(self, player: PlayerRef, room: RoomRef, event: str, data: Any, to: str | None) -> None:
        if self.worker is not None:
            await self.worker.handle_message(player, room, event, data, to)

    async def handle_disconnect(self, player: PlayerRef, room: RoomRef, reason: str) -> None:
        if self.worker is not None:
            await self.worker.handle_disconnect(player, room, reason)

    async def handle_room_event(self, room: RoomRef, event: str) -> None:
        if self.worker is not None:
            await self.worker.handle_room_event(room, event)

    async def shutdown(self) -> None:
        backend = self.standalone or self.worker
        if backend is not None and not backend.stopping:
            await backend.stop("shutdown", 0)
        self.state = AdapterState.STOPPED
