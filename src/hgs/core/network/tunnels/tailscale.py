"""Tailscale Funnel provider: a free, permanent https://<machine>.<tailnet>.ts.net address.

`tailscale funnel <port>` (foreground) publishes the local gateway on the internet for as long as
the command runs; stopping it removes the publication. Needs the Tailscale app installed and logged
in, and Funnel allowed for the tailnet (the CLI prints a link to allow it; HGS shows that link).
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from pathlib import Path

from hgs.common.platform.process import popen_kwargs

from .cloudflare import CloudflareTunnel

TS_URL_RE = re.compile(r"https://[a-z0-9-]+(\.[a-z0-9-]+)*\.ts\.net")
ENABLE_RE = re.compile(r"https://login\.tailscale\.com/\S+")


def find_tailscale(configured: str | None) -> Path | None:
    candidates = [configured, shutil.which("tailscale")]
    if sys.platform == "win32":
        candidates.append(r"C:\Program Files\Tailscale\tailscale.exe")
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return None


class TailscaleFunnel(CloudflareTunnel):
    def __init__(self, *, command: list[str], local_port: int, on_change) -> None:
        super().__init__(kind="tailscale_funnel", command=command, local_port=local_port, token=None,
                         named_url=None, on_change=on_change)
        self.dns_name: str | None = None

    def _args(self) -> list[str]:
        # an explicit IPv4 target: "localhost" may resolve to ::1 on Windows, where HGS does not listen
        return ["funnel", f"http://127.0.0.1:{self.local_port}"]

    async def _status(self) -> dict:
        proc = await asyncio.create_subprocess_exec(*self.command, "status", "--json", stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                    **popen_kwargs())
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 15)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("Tailscale не відповідає")
        try:
            return json.loads(out or b"{}")
        except json.JSONDecodeError:
            raise RuntimeError((err or out).decode("utf-8", "replace").strip()[:300] or "невідома відповідь Tailscale")

    async def _precheck(self) -> bool:
        try:
            st = await self._status()
        except (OSError, RuntimeError) as exc:
            self._set("failed", f"не вдалося запитати стан Tailscale: {exc}")
            return False
        state = st.get("BackendState")
        if state != "Running":
            hint = {"NeedsLogin": "увійдіть в акаунт у програмі Tailscale (значок біля годинника → Log in)",
                    "Stopped": "увімкніть Tailscale (значок біля годинника → Connect)"}.get(state, f"стан: {state}")
            self._set("failed", f"Tailscale не підключено — {hint}")
            return False
        dns = ((st.get("Self") or {}).get("DNSName") or "").rstrip(".")
        self.dns_name = dns or None
        return True

    def _on_line(self, text: str) -> None:
        m = ENABLE_RE.search(text)
        if m and self.state != "connected":
            self._set("starting", f"Увімкніть Funnel для цього ПК, відкривши посилання (один раз): {m.group(0)}")
            return
        m = TS_URL_RE.search(text)
        if m and self.state != "connected":
            self.public_url = m.group(0)
            self._set("connected")
