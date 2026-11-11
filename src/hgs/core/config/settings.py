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
    # local = only this PC (127.0.0.1); lan = all interfaces (0.0.0.0)
    bind: Literal["local", "lan"] = "local"
    max_connections_per_ip: int = Field(8, ge=1, le=1000)
    hello_timeout_s: float = Field(5.0, gt=0, le=60)


class WorkerBusSettings(_Strict):
    port: int = Field(8766, ge=0, le=65535)


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
    logs: LogSettings = LogSettings()
    resources: ResourceSettings = ResourceSettings()

    def bind_host(self) -> str:
        return "0.0.0.0" if self.gateway.bind == "lan" else "127.0.0.1"


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
