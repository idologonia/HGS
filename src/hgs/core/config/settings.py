"""Global HGS settings (settings.json). Secrets are never stored here."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hgs.common.fileutil import atomic_write_json

log = logging.getLogger("hgs.settings")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdminSettings(_Strict):
    port: int = Field(8764, ge=0, le=65535)


class GatewaySettings(_Strict):
    port: int = Field(8765, ge=0, le=65535)
    # Deprecated (Stage 2): use network.mode. Kept so old settings files still load.
    bind: Literal["local", "lan"] | None = None
    max_connections_per_ip: int = Field(8, ge=1, le=1000)
    hello_timeout_s: float = Field(5.0, gt=0, le=60)


class WorkerBusSettings(_Strict):
    port: int = Field(8766, ge=0, le=65535)


class TunnelSettings(_Strict):
    # cloudflare_quick: free, no account, random https://*.trycloudflare.com address that changes on every start
    # cloudflare_named: free Cloudflare account + your own domain; fixed address; token stored encrypted
    # tailscale_funnel: free Tailscale account; fixed https://<pc>.<tailnet>.ts.net address, rarely blocked
    provider: Literal["cloudflare_quick", "cloudflare_named", "tailscale_funnel"] = "cloudflare_quick"
    cloudflared_path: str | None = None
    tailscale_path: str | None = None
    named_public_url: str | None = Field(None, max_length=200)  # e.g. https://play.example.com


class DirectSettings(_Strict):
    public_host: str | None = Field(None, max_length=253)  # domain or IP players use
    public_port: int | None = Field(None, ge=1, le=65535)  # external port if the router maps a different one
    tls_cert: str | None = None  # PEM certificate (full chain)
    tls_key: str | None = None   # PEM private key
    duckdns_domain: str | None = Field(None, max_length=100, pattern=r"^[a-z0-9-]+$")  # "myhgs" for myhgs.duckdns.org


class PublishSettings(_Strict):
    # Publish the current server address to a file in a GitHub repository (games find the server by it).
    enabled: bool = False
    repo: str | None = Field(None, max_length=201, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    path: str = Field("hgs-server.json", max_length=200, pattern=r"^[A-Za-z0-9_./-]+\.json$")


class NetworkSettings(_Strict):
    # local  = only this PC (127.0.0.1)
    # lan    = home network (0.0.0.0), no TLS
    # direct = internet through your router (0.0.0.0) with TLS (wss://)
    # tunnel = internet through a tunnel; the gateway stays on 127.0.0.1
    mode: Literal["local", "lan", "direct", "tunnel"] = "local"
    tunnel: TunnelSettings = TunnelSettings()
    direct: DirectSettings = DirectSettings()
    public_ip_lookup: bool = True  # ask api.ipify.org for the public IP (Network page)
    publish: PublishSettings = PublishSettings()


class LogSettings(_Strict):
    max_file_mb: int = Field(10, ge=1, le=1000)
    keep_files: int = Field(5, ge=1, le=100)


class ResourceSettings(_Strict):
    metrics_interval_s: float = Field(2.0, ge=0.5, le=60)


class Settings(_Strict):
    settings_version: int = 1
    server_name: str = Field("Home Game Server", min_length=1, max_length=64)
    admin: AdminSettings = AdminSettings()
    gateway: GatewaySettings = GatewaySettings()
    worker_bus: WorkerBusSettings = WorkerBusSettings()
    network: NetworkSettings = NetworkSettings()
    # Programs outside game folders that the owner approved for starting game servers.
    allowed_programs: list[str] = Field(default_factory=list, max_length=100)
    logs: LogSettings = LogSettings()
    resources: ResourceSettings = ResourceSettings()

    def model_post_init(self, _ctx) -> None:
        # migrate the Stage 2 field
        if self.gateway.bind == "lan" and self.network.mode == "local":
            self.network.mode = "lan"
        self.gateway.bind = None

    def bind_host(self) -> str:
        return "0.0.0.0" if self.network.mode in ("lan", "direct") else "127.0.0.1"

    @property
    def internet_exposed(self) -> bool:
        return self.network.mode in ("direct", "tunnel")


class SettingsError(RuntimeError):
    pass


def load_settings(path: Path) -> Settings:
    """Load settings; create the file with defaults if it does not exist.

    A broken file is NOT silently replaced: the user gets a clear error.
    """
    if not path.exists():
        s = Settings()
        atomic_write_json(path, s.model_dump())
        return s
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Settings.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise SettingsError(f"Invalid settings file {path}: {exc}") from exc


def save_settings(path: Path, settings: Settings) -> None:
    atomic_write_json(path, settings.model_dump())
