"""StandaloneBackend: runs an existing game server that has its own port and protocol.

HGS manages the process (start/stop/restart, logs, resources, crashes) and can proxy
WebSocket connections to it through the gateway (/hgs/v1/proxy/<game_id>/...), so the same
tunnel works for every game. The game's own rules and login stay the game's responsibility.
"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any

from hgs.adapters.base import AdapterError
from hgs.core.process_supervisor import LaunchError, LaunchSpec, ManagedProcess


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def tcp_ready(port: int) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 1.0)
        w.close()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


class StandaloneBackend:
    def __init__(self, ctx, spec: LaunchSpec, port: int, ready: dict[str, Any]) -> None:
        self.ctx = ctx
        self.spec = spec
        self.port = port
        self.ready = ready
        self.process: ManagedProcess | None = None
        self.stopping = False
        self._watch: asyncio.Task | None = None

    async def start(self) -> None:
        self.stopping = False
        try:
            self.ctx.allowlist.validate(self.spec)
        except LaunchError as exc:
            raise AdapterError(str(exc)) from exc
        self.process = ManagedProcess(self.spec, self.ctx.log)
        try:
            await self.process.start()
        except LaunchError as exc:
            raise AdapterError(str(exc)) from exc
        exit_wait = asyncio.create_task(self.process.wait())
        if self.ready.get("type", "tcp") == "tcp":
            deadline = time.monotonic() + float(self.ready.get("timeout_s", 20))
            while True:
                if exit_wait.done():
                    raise AdapterError(f"game server exited during start (code {exit_wait.result()}): {self._last_err()}")
                if await tcp_ready(self.port):
                    break
                if time.monotonic() > deadline:
                    exit_wait.cancel()
                    await self.process.terminate(3)
                    raise AdapterError(f"game server did not open port {self.port} in time")
                await asyncio.sleep(0.2)
        self.ctx.log.info("standalone server is listening on 127.0.0.1:%d", self.port)
        self._watch = asyncio.create_task(self._watcher(exit_wait))

    def _last_err(self) -> str:
        return self.process.stderr_tail[-1] if self.process and self.process.stderr_tail else ""

    async def _watcher(self, exit_wait: asyncio.Task) -> None:
        code = await exit_wait
        if not self.stopping:
            reason = f"game server exited unexpectedly (code {code})"
            if self._last_err():
                reason += f": {self._last_err()}"
            self.ctx.report_crash(reason)

    async def stop(self, reason: str, grace_s: float) -> None:
        self.stopping = True
        if self._watch:
            self._watch.cancel()
        if self.process is not None:
            await self.process.terminate(max(3.0, min(grace_s, 10.0)))

    def metrics(self) -> dict[str, Any]:
        m = self.process.metrics() if self.process else {}
        return {**m, "port": self.port}
