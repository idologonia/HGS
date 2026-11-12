"""Cloudflare Tunnel provider (cloudflared).

quick — `cloudflared tunnel --url http://127.0.0.1:<port>`: free, no account, a random
        https://*.trycloudflare.com address that CHANGES on every start; no uptime guarantee.
named — `cloudflared tunnel run` with a tunnel token (free Cloudflare account + your own domain);
        fixed address. The token is passed through the TUNNEL_TOKEN environment variable,
        never on the command line (command lines are visible in Task Manager).

The gateway stays on 127.0.0.1; cloudflared connects to it locally.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
import time
from collections import deque
from pathlib import Path
from typing import Callable

from hgs.common.platform.process import ProcessContainer, popen_kwargs

log = logging.getLogger("hgs.tunnel")

QUICK_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
CONNECTED_RE = re.compile(r"Registered tunnel connection|Connection [0-9a-f-]+ registered", re.I)
MAX_RESTARTS = 3
RESTART_WINDOW_S = 600
BACKOFF_S = (2.0, 8.0, 30.0)

LIMITATIONS = {
    "cloudflare_quick": [
        "Безкоштовно, без акаунта і домену.",
        "Адреса випадкова і змінюється при кожному запуску тунелю — після перезапуску надішліть друзям нове посилання.",
        "Призначено для тестів і розробки: Cloudflare не гарантує доступність і може змінити умови.",
        "Є обмеження на кількість одночасних запитів; для невеликої компанії друзів цього зазвичай достатньо.",
    ],
    "tailscale_funnel": [
        "Безкоштовно. Постійна адреса https://<назва-пк>.<ваша-мережа>.ts.net — не змінюється між запусками.",
        "Домен ts.net рідко блокують антивіруси й провайдери (на відміну від trycloudflare.com).",
        "Потрібно один раз встановити Tailscale (tailscale.com/download) і увійти в акаунт; Funnel вмикається "
        "одним натисканням у панелі Tailscale (HGS покаже посилання).",
        "Tailscale обмежує пропускну здатність Funnel; для ігор з друзями зазвичай вистачає. Доступність не гарантована.",
    ],
    "cloudflare_named": [
        "Постійна адреса на вашому домені (наприклад, https://play.example.com).",
        "Потрібні безкоштовний акаунт Cloudflare і власний домен, підключений до Cloudflare (домен зазвичай платний).",
        "Тунель і адресу налаштовують у панелі Cloudflare Zero Trust; HGS потрібен лише токен тунелю.",
        "Доступність залежить від Cloudflare та вашого інтернету — HGS не може її гарантувати.",
    ],
}


def binary_name() -> str:
    return "cloudflared.exe" if sys.platform == "win32" else "cloudflared"


def find_cloudflared(configured: str | None, bin_dir: Path) -> Path | None:
    for candidate in (configured, str(bin_dir / binary_name()), shutil.which("cloudflared")):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


class CloudflareTunnel:
    def __init__(self, *, kind: str, command: list[str], local_port: int, token: str | None,
                 named_url: str | None, on_change: Callable[[], None]) -> None:
        self.kind = kind  # "cloudflare_quick" | "cloudflare_named"
        self.command = command  # path to cloudflared (tests may use a fake)
        self.local_port = local_port
        self.token = token
        self.named_url = named_url.rstrip("/") if named_url else None
        self.on_change = on_change
        self.state = "stopped"  # stopped | starting | connected | reconnecting | failed
        self.public_url: str | None = None
        self.error: str | None = None
        self.started_at: float | None = None
        self.lines: deque[str] = deque(maxlen=50)
        self._proc: asyncio.subprocess.Process | None = None
        self._container: ProcessContainer | None = None
        self._task: asyncio.Task | None = None
        self._crashes: deque[float] = deque()
        self._stopping = False

    def limitations(self) -> list[str]:
        return LIMITATIONS[self.kind]

    async def _precheck(self) -> bool:
        return True

    def _set(self, state: str, error: str | None = None) -> None:
        self.state = state
        self.error = error
        self.on_change()

    def _args(self) -> list[str]:
        if self.kind == "cloudflare_quick":
            return ["tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.local_port}"]
        return ["tunnel", "--no-autoupdate", "run"]

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        if not await self._precheck():
            return
        if self.kind == "cloudflare_named" and (not self.token or not self.named_url):
            self._set("failed", "для постійного тунелю потрібні токен тунелю і публічна адреса")
            return
        self._stopping = False
        self._crashes.clear()
        self._task = asyncio.get_running_loop().create_task(self._run(), name="tunnel")

    async def _run(self) -> None:
        while not self._stopping:
            self.public_url = self.named_url if self.kind == "cloudflare_named" else None
            self._set("starting" if not self._crashes else "reconnecting")
            code = await self._run_once()
            if self._stopping:
                break
            now = time.monotonic()
            self._crashes.append(now)
            while self._crashes and now - self._crashes[0] > RESTART_WINDOW_S:
                self._crashes.popleft()
            last = self.lines[-1] if self.lines else ""
            if len(self._crashes) > MAX_RESTARTS:
                self.public_url = None
                self._set("failed", f"cloudflared завершився (код {code}) кілька разів поспіль: {last}")
                return
            delay = BACKOFF_S[min(len(self._crashes) - 1, len(BACKOFF_S) - 1)]
            log.warning("cloudflared exited (code %s), restarting in %.0f s", code, delay)
            self._set("reconnecting", f"cloudflared завершився (код {code}), повторна спроба…")
            await asyncio.sleep(delay)
        self.public_url = None
        self._set("stopped")

    async def _run_once(self) -> int | None:
        env = dict(os.environ)
        if self.token:
            env["TUNNEL_TOKEN"] = self.token
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.command, *self._args(), env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **popen_kwargs())
        except OSError as exc:
            self.lines.append(str(exc))
            return None
        self._container = ProcessContainer()
        try:
            self._container.attach(self._proc.pid)
        except Exception as exc:
            log.warning("cannot contain cloudflared: %s", exc)
        self.started_at = time.time()
        assert self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if not text:
                continue
            self.lines.append(text[:500])
            log.info("[cloudflared] %s", text[:500])
            self._on_line(text)
        code = await self._proc.wait()
        self._container.close()
        return code

    def _on_line(self, text: str) -> None:
        """React to one line of the tunnel program's output."""
        if self.kind == "cloudflare_quick" and self.public_url is None:
            m = QUICK_URL_RE.search(text)
            if m:
                self.public_url = m.group(0)
                self._set("connected")
        elif self.kind == "cloudflare_named" and self.state != "connected" and CONNECTED_RE.search(text):
            self._set("connected")

    async def stop(self) -> None:
        self._stopping = True
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), 5)
            except (ProcessLookupError, asyncio.TimeoutError):
                if self._container:
                    self._container.kill_tree(self._proc.pid)
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self.public_url = None
        if self.state != "stopped":
            self._set("stopped")

    def view(self) -> dict:
        return {"provider": self.kind, "state": self.state, "url": self.public_url, "error": self.error,
                "log_tail": list(self.lines)[-8:], "limitations": self.limitations()}
