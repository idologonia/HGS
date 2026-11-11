"""Resource Monitor: CPU, RAM and network of the PC and of the HGS core process."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import psutil

if TYPE_CHECKING:
    from .events import EventBus

log = logging.getLogger("hgs.resources")


class ResourceMonitor:
    def __init__(self, bus: "EventBus", interval_s: float = 2.0) -> None:
        self.bus = bus
        self.interval_s = interval_s
        self.snapshot: dict[str, Any] = {}
        self._proc = psutil.Process()
        self._last_net = psutil.net_io_counters()
        self._last_t = time.monotonic()
        psutil.cpu_percent(None)  # first call primes the counter
        self._proc.cpu_percent(None)
        self._task: asyncio.Task | None = None

    def sample(self) -> dict[str, Any]:
        now = time.monotonic()
        net = psutil.net_io_counters()
        dt = max(now - self._last_t, 1e-6)
        vm = psutil.virtual_memory()
        snap = {
            "cpu_percent": psutil.cpu_percent(None),
            "ram_percent": vm.percent,
            "ram_used_bytes": vm.used,
            "ram_total_bytes": vm.total,
            "net_recv_bps": max(0.0, (net.bytes_recv - self._last_net.bytes_recv) / dt),
            "net_sent_bps": max(0.0, (net.bytes_sent - self._last_net.bytes_sent) / dt),
            "core_cpu_percent": self._proc.cpu_percent(None),
            "core_memory_bytes": self._proc.memory_info().rss,
        }
        self._last_net, self._last_t = net, now
        self.snapshot = snap
        return snap

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run(), name="resource-monitor")

    async def _run(self) -> None:
        while True:
            try:
                self.bus.publish("metrics.tick", **self.sample())
            except Exception:
                log.exception("resource sampling failed")
            await asyncio.sleep(self.interval_s)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
