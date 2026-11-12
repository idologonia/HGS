"""Network Manager: how players reach the gateway (local / LAN / direct / tunnel).

Adapters never see any of this: switching the access mode only rebinds the gateway and
starts/stops the tunnel. Nothing here guarantees permanent availability — a tunnel or a
home connection can drop, and the GUI says so.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from hgs.common.fileutil import atomic_write_json
from hgs.core.config.settings import NetworkSettings, Settings

from .tunnels.cloudflare import LIMITATIONS, CloudflareTunnel, find_cloudflared
from .tunnels.tailscale import TailscaleFunnel, find_tailscale

if TYPE_CHECKING:
    from hgs.core.app import CoreApp

log = logging.getLogger("hgs.network")

DUCKDNS_INTERVAL_S = 300


class NetworkError(ValueError):
    pass


def lan_addresses() -> list[str]:
    out = []
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family.name == "AF_INET":
                ip = ipaddress.ip_address(a.address)
                if ip.is_private and not ip.is_loopback and not ip.is_link_local:
                    out.append(a.address)
    return sorted(set(out))


def is_cgnat(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def _http_get(url: str, timeout: float = 10.0, context: ssl.SSLContext | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "HGS-network-check"})
    with urllib.request.urlopen(req, timeout=timeout, context=context) as r:  # noqa: S310
        return r.status, r.read(65536)


class NetworkManager:
    def __init__(self, core: "CoreApp") -> None:
        self.core = core
        self.tunnel: CloudflareTunnel | None = None
        self.public_ip: str | None = None
        self.public_ip_error: str | None = None
        self.last_check: dict[str, Any] | None = None
        self.duckdns_status: str | None = None
        self._duckdns_task: asyncio.Task | None = None
        from .publisher import AddressPublisher

        self.publisher = AddressPublisher(core)
        self._published_url: str | None = "unset"

    @property
    def settings(self) -> NetworkSettings:
        return self.core.settings.network

    # == addresses =================================================================
    def player_url(self) -> str | None:
        """The address players put into the SDK (serverUrl). None if not available yet."""
        n = self.settings
        port = self.core.gateway_port
        if n.mode == "local":
            return f"ws://127.0.0.1:{port}"
        if n.mode == "lan":
            ips = lan_addresses()
            return f"ws://{ips[0]}:{port}" if ips else None
        if n.mode == "direct":
            host = n.direct.public_host or (f"{n.direct.duckdns_domain}.duckdns.org" if n.direct.duckdns_domain else None)
            if not host:
                return None
            ext = n.direct.public_port or port
            return f"wss://{host}" + ("" if ext == 443 else f":{ext}")
        if self.tunnel and self.tunnel.public_url and self.tunnel.state == "connected":
            return "wss://" + self.tunnel.public_url.split("://", 1)[1]
        return None

    def invite_link(self, client_url: str | None, code: str) -> str | None:
        server = self.player_url()
        if not client_url or not server:
            return None
        sep = "&" if "?" in client_url else "?"
        return f"{client_url}{sep}{urllib.parse.urlencode({'hgs': server, 'invite': code})}"

    def warnings(self) -> list[str]:
        n = self.settings
        out = []
        if n.mode == "lan":
            out.append("LAN-режим без шифрування: ігри з GitHub Pages (https) не зможуть підключитися — "
                       "використовуйте тунель або відкривайте гру локально.")
        if n.mode == "direct" and not (n.direct.public_host or n.direct.duckdns_domain):
            out.append("Вкажіть публічну адресу (домен) — інакше гравці не знатимуть, куди підключатися.")
        if n.mode == "tunnel" and (self.tunnel is None or self.tunnel.state != "connected"):
            out.append("Тунель не запущено: гравці з інтернету поки не можуть підключитися.")
        if not self.core.secrets.protected and self.core.settings.internet_exposed:
            out.append("Секрети на цій ОС не шифруються (режим розробки).")
        return out

    def view(self) -> dict[str, Any]:
        n = self.settings
        binary = self._binary()
        tunnel = self.tunnel.view() if self.tunnel else {
            "provider": n.tunnel.provider, "state": "stopped", "url": None, "error": None, "log_tail": [],
            "limitations": LIMITATIONS[n.tunnel.provider]}
        return {
            "settings": n.model_dump(),
            "gateway_port": self.core.gateway_port,
            "bind": self.core.settings.bind_host(),
            "tls": self.core.gateway_tls,
            "player_url": self.player_url(),
            "lan_ips": lan_addresses(),
            "public_ip": self.public_ip,
            "public_ip_error": self.public_ip_error,
            "cgnat_suspected": bool(self.public_ip and is_cgnat(self.public_ip)),
            "tunnel": {**tunnel, "binary": str(binary) if binary else None},
            "duckdns": self.duckdns_status,
            "last_check": self.last_check,
            "warnings": self.warnings(),
            "secrets_protected": self.core.secrets.protected,
            "publish": self.publisher.view(),
        }

    def _changed(self) -> None:
        url = self.player_url()
        self.core.bus.publish("network.changed", mode=self.settings.mode, player_url=url,
                              tunnel=self.tunnel.view() if self.tunnel else None)
        if url != self._published_url:  # tell games where the server is now
            self._published_url = url
            self.publisher.schedule()

    # == settings ==================================================================
    @staticmethod
    def validate_tls(cert: str | None, key: str | None) -> None:
        if not cert or not key:
            raise NetworkError("Direct mode needs a TLS certificate and key (wss://). Without them browsers "
                               "on https pages cannot connect. Use the tunnel mode if you have no certificate.")
        for f in (cert, key):
            if not Path(f).is_file():
                raise NetworkError(f"file not found: {f}")
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
        except (ssl.SSLError, OSError) as exc:
            raise NetworkError(f"cannot load the certificate/key: {exc}") from exc

    async def apply(self, new: NetworkSettings, gateway_port: int | None = None) -> None:
        if new.mode == "direct":
            self.validate_tls(new.direct.tls_cert, new.direct.tls_key)
        if new.tunnel.provider == "cloudflare_named" and new.tunnel.named_public_url:
            if not new.tunnel.named_public_url.startswith("https://"):
                raise NetworkError("the public address of a named tunnel must start with https://")
        old_settings = self.core.settings
        updated = old_settings.model_copy(deep=True)
        updated.network = new
        if gateway_port is not None:
            updated.gateway.port = gateway_port
        rebind = (updated.bind_host() != old_settings.bind_host()
                  or updated.gateway.port != old_settings.gateway.port
                  or new.mode == "direct" or old_settings.network.mode == "direct")
        if new.mode != "tunnel" or new.tunnel != old_settings.network.tunnel:
            await self.stop_tunnel()
        self.core.settings = updated
        try:
            if rebind:
                await self.core.restart_gateway()
        except Exception:
            self.core.settings = old_settings
            await self.core.restart_gateway()
            raise
        if not self.core.settings_is_override:
            atomic_write_json(self.core.paths.settings, updated.model_dump(exclude_none=True))
        self._restart_duckdns()
        self._published_url = "unset"  # republish with the new settings
        log.info("network mode: %s (gateway %s:%d)", new.mode, updated.bind_host(), self.core.gateway_port)
        self._changed()

    # == tunnel ====================================================================
    def _binary(self) -> Path | None:
        t = self.settings.tunnel
        if t.provider == "tailscale_funnel":
            return find_tailscale(t.tailscale_path)
        return find_cloudflared(t.cloudflared_path, self.core.paths.root / "bin")

    def tunnel_command(self) -> list[str]:
        binary = self._binary()
        if binary is None:
            if self.settings.tunnel.provider == "tailscale_funnel":
                raise NetworkError("Tailscale не знайдено: встановіть його з tailscale.com/download і увійдіть в акаунт")
            raise NetworkError("cloudflared not found: download it on the Network page or set its path")
        return [str(binary)]

    async def start_tunnel(self) -> None:
        if self.settings.mode != "tunnel":
            raise NetworkError("switch the network mode to Tunnel first")
        if self.tunnel and self.tunnel.state in ("starting", "connected", "reconnecting"):
            return
        t = self.settings.tunnel
        token = await self.core.secrets.get("cloudflare_tunnel_token") if t.provider == "cloudflare_named" else None
        if t.provider == "tailscale_funnel":
            self.tunnel = TailscaleFunnel(command=self.tunnel_command(), local_port=self.core.gateway_port,
                                          on_change=self._changed)
            await self.tunnel.start()
            return
        self.tunnel = CloudflareTunnel(kind=t.provider, command=self.tunnel_command(), local_port=self.core.gateway_port,
                                       token=token, named_url=t.named_public_url, on_change=self._changed)
        await self.tunnel.start()

    async def stop_tunnel(self) -> None:
        if self.tunnel is not None:
            await self.tunnel.stop()

    # == diagnostics ===============================================================
    async def lookup_public_ip(self) -> str | None:
        if not self.settings.public_ip_lookup:
            self.public_ip_error = "disabled in settings"
            return None
        try:
            status, body = await asyncio.to_thread(_http_get, "https://api.ipify.org?format=json", 10)
            self.public_ip = json.loads(body)["ip"]
            self.public_ip_error = None
        except Exception as exc:
            self.public_ip_error = f"{type(exc).__name__}: {exc}"
        self._changed()
        return self.public_ip

    async def check_reachability(self) -> dict[str, Any]:
        """Fetch /hgs/v1/health through the public address and check it is THIS HGS instance."""
        url = self.player_url()
        result: dict[str, Any] = {"ok": False, "url": url, "at": time.time()}
        if url is None:
            result["detail"] = "немає публічної адреси (запустіть тунель або вкажіть домен)"
        else:
            http = url.replace("wss://", "https://").replace("ws://", "http://") + "/hgs/v1/health"
            result["url"] = http
            t0 = time.monotonic()
            try:
                status, body = await asyncio.to_thread(_http_get, http, 15)
                data = json.loads(body)
                result["latency_ms"] = round((time.monotonic() - t0) * 1000)
                if data.get("instance") == self.core.gateway.instance_nonce:
                    result["ok"] = True
                    result["detail"] = "сервер доступний за цією адресою"
                else:
                    result["detail"] = "за адресою відповідає інший сервер, а не цей HGS"
            except Exception as exc:
                result["detail"] = f"не вдалося підключитися: {type(exc).__name__}: {exc}"
                if self.settings.mode == "direct":
                    result["hint"] = ("Перевірте проброс порту на роутері. Якщо роутер не підтримує «hairpin NAT», "
                                      "перевірка з цього ПК може не пройти — відкрийте адресу з телефону через "
                                      "мобільний інтернет. Якщо у провайдера CGNAT, пряме підключення неможливе — "
                                      "використовуйте тунель.")
        self.last_check = result
        self._changed()
        return result

    # == DuckDNS (dynamic IP for direct mode) ======================================
    def _restart_duckdns(self) -> None:
        if self._duckdns_task:
            self._duckdns_task.cancel()
            self._duckdns_task = None
        d = self.settings.direct
        if self.settings.mode == "direct" and d.duckdns_domain:
            self._duckdns_task = asyncio.get_running_loop().create_task(self._duckdns_loop(d.duckdns_domain))

    async def _duckdns_loop(self, domain: str) -> None:
        while True:
            token = await self.core.secrets.get("duckdns_token")
            if not token:
                self.duckdns_status = "немає токена DuckDNS"
            else:
                url = "https://www.duckdns.org/update?" + urllib.parse.urlencode({"domains": domain, "token": token, "ip": ""})
                try:
                    _, body = await asyncio.to_thread(_http_get, url, 15)
                    self.duckdns_status = "IP оновлено" if body.strip() == b"OK" else "DuckDNS відхилив запит (перевірте домен і токен)"
                except Exception as exc:
                    self.duckdns_status = f"помилка оновлення: {type(exc).__name__}"
            await asyncio.sleep(DUCKDNS_INTERVAL_S)

    async def start(self) -> None:
        self._restart_duckdns()

    async def stop(self) -> None:
        if self._duckdns_task:
            self._duckdns_task.cancel()
        await self.stop_tunnel()
        if self.publisher.cfg.enabled and self.publisher.cfg.repo and self._published_url not in (None, "unset"):
            try:  # games show "server offline" instead of trying a dead address
                await asyncio.wait_for(self.publisher.publish({"server": None, "online": False, "updated": int(time.time()),
                                                               "name": self.core.settings.server_name}), 15)
            except Exception:
                pass
