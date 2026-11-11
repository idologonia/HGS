"""Game manifest (hgs-game.json) — describes a game for HGS.

Common fields are validated here; the `backend` section is validated by the adapter.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

MANIFEST_FILE = "hgs-game.json"
GAME_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,47}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClientSection(_Strict):
    url: str | None = None
    # Browser origins allowed to connect (e.g. "https://name.github.io").
    # Empty list = do not check Origin (only acceptable for local testing).
    allowed_origins: list[str] = []

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, v: list[str]) -> list[str]:
        out = []
        for o in v:
            o = o.rstrip("/")
            if not (o.startswith("http://") or o.startswith("https://")) or "/" in o.split("://", 1)[1]:
                raise ValueError(f"origin must look like https://host[:port], got {o!r}")
            out.append(o.lower())
        return out


class AuthSection(_Strict):
    # Stage 2 implements "invite" and "open". "password"/"account" arrive in Stage 4.
    mode: Literal["invite", "open"] = "invite"


class RoomsSection(_Strict):
    max_rooms: int = Field(10, ge=1, le=1000)
    max_players: int = Field(8, ge=1, le=1000)
    allow_player_create: bool = True
    empty_ttl_s: int = Field(300, ge=0, le=86400)
    # Rooms created when the server starts; they stay open while the server runs.
    default_rooms: list[str] = Field(default_factory=lambda: ["lobby"], max_length=50)

    @field_validator("default_rooms")
    @classmethod
    def _room_ids(cls, v: list[str]) -> list[str]:
        for r in v:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", r):
                raise ValueError(f"invalid room id {r!r}")
        return v


class LimitsSection(_Strict):
    max_message_bytes: int = Field(16384, ge=256, le=1024 * 1024)
    messages_per_s: float = Field(30, gt=0, le=1000)
    burst: int = Field(60, ge=1, le=10000)
    room_state_bytes: int = Field(65536, ge=1024, le=1024 * 1024)


class RestartSection(_Strict):
    policy: Literal["on_crash", "never"] = "on_crash"
    max_restarts: int = Field(3, ge=0, le=100)
    window_s: int = Field(600, ge=10, le=86400)


class GameManifest(_Strict):
    manifest_version: Literal[1] = 1
    id: str = Field(pattern=GAME_ID_PATTERN)
    name: str = Field(min_length=1, max_length=64)
    version: str = Field("0.0.0", max_length=32)
    adapter: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    adapter_api: int = 1
    client: ClientSection = ClientSection()
    backend: dict[str, Any] = {}
    auth: AuthSection = AuthSection()
    rooms: RoomsSection = RoomsSection()
    limits: LimitsSection = LimitsSection()
    restart: RestartSection = RestartSection()


class ManifestError(ValueError):
    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


def parse_manifest(raw: Any) -> GameManifest:
    try:
        return GameManifest.model_validate(raw)
    except ValidationError as exc:
        errors = [
            {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()
        ]
        summary = "; ".join(f"{e['field']}: {e['message']}" for e in errors)
        raise ManifestError(f"Invalid game manifest: {summary}", errors) from exc


def load_manifest_file(path: Path) -> GameManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"{path.name} not found in {path.parent}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path.name} is not valid JSON: {exc}") from exc
    return parse_manifest(raw)
