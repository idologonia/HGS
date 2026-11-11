"""Shared test fixtures: an in-process HGS core on random ports with a temporary data dir."""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets

from hgs.adapters.registry import builtin_registry
from hgs.core.app import CoreApp
from hgs.core.config.settings import Settings


def make_game_dir(base: Path, game_id: str, **overrides: Any) -> Path:
    d = base / f"src-{game_id}"
    d.mkdir(parents=True, exist_ok=True)
    manifest = {"id": game_id, "name": game_id.title(), "adapter": "html", "backend": {"mode": "relay"},
                "auth": {"mode": "open"}}
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(manifest.get(k), dict):
            manifest[k] = {**manifest[k], **v}
        else:
            manifest[k] = v
    (d / "hgs-game.json").write_text(json.dumps(manifest), encoding="utf-8")
    return d


class Harness:
    def __init__(self, core: CoreApp, tmp: Path) -> None:
        self.core = core
        self.tmp = tmp
        self.admin = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{core.admin_port}/admin/v1",
            headers={"Authorization": f"Bearer {core.admin_token}"},
            timeout=10,
        )
        self.ws_url = f"ws://127.0.0.1:{core.gateway_port}/hgs/v1/ws"

    async def add_and_start(self, game_id: str, **overrides: Any) -> None:
        r = await self.admin.post("/games", json={"path": str(make_game_dir(self.tmp, game_id, **overrides))})
        assert r.status_code == 201, r.text
        r = await self.admin.post(f"/games/{game_id}/start")
        assert r.json()["state"] == "running", r.text

    async def client(self, game_id: str, auth: dict | str, *, origin: str | None = None) -> "Client":
        if isinstance(auth, str):
            auth = {"type": "open", "name": auth}
        c = Client(await websockets.connect(self.ws_url, origin=origin, max_size=4 * 1024 * 1024))
        await c.send("hello", {"game": game_id, "auth": auth})
        c.welcome = await c.recv()
        return c


class Client:
    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.seq = 0
        self.welcome: dict | None = None
        self._n = 0

    async def send(self, t: str, d: Any = None, *, req: bool = False, seq: int | None = None) -> str | None:
        self.seq += 1
        env: dict[str, Any] = {"v": 1, "t": t, "seq": self.seq if seq is None else seq}
        rid = None
        if req:
            self._n += 1
            rid = f"r{self._n}"
            env["id"] = rid
        if d is not None:
            env["d"] = d
        await self.ws.send(json.dumps(env))
        return rid

    async def recv(self, timeout: float = 3.0) -> dict:
        return json.loads(await asyncio.wait_for(self.ws.recv(), timeout))

    async def recv_type(self, t: str, timeout: float = 3.0) -> dict:
        while True:
            m = await self.recv(timeout)
            if m["t"] == t:
                return m

    async def request(self, t: str, d: Any = None) -> dict:
        rid = await self.send(t, d, req=True)
        while True:
            m = await self.recv()
            if m["t"] == "reply" and m.get("re") == rid:
                return m

    async def join(self, room_id: str = "lobby") -> dict:
        r = await self.request("room.join", {"room_id": room_id})
        assert r["ok"], r
        return await self.recv_type("room.joined")

    async def expect_nothing(self, timeout: float = 0.3) -> None:
        try:
            m = await asyncio.wait_for(self.ws.recv(), timeout)
        except asyncio.TimeoutError:
            return
        raise AssertionError(f"unexpected message: {m}")

    async def closed_code(self, timeout: float = 5.0) -> int:
        try:
            while True:
                await asyncio.wait_for(self.ws.recv(), timeout)
        except websockets.ConnectionClosed as exc:
            return exc.rcvd.code if exc.rcvd else exc.code

    async def close(self) -> None:
        await self.ws.close()


def make_settings(**gateway: Any) -> Settings:
    s = Settings()
    s.admin.port = 0
    s.gateway.port = 0
    s.worker_bus.port = 0
    for k, v in gateway.items():
        setattr(s.gateway, k, v)
    return s


@pytest.fixture
async def core_factory(tmp_path: Path):
    cores: list[CoreApp] = []
    clients: list[httpx.AsyncClient] = []

    async def _make(settings: Settings | None = None, registry=None, data_dir: Path | None = None) -> Harness:
        core = CoreApp(data_dir or tmp_path / "data", secrets.token_urlsafe(32), settings or make_settings(),
                       registry=registry or builtin_registry())
        await core.start()
        cores.append(core)
        h = Harness(core, tmp_path)
        clients.append(h.admin)
        return h

    yield _make
    for c in clients:
        await c.aclose()
    for core in cores:
        await core.stop()


@pytest.fixture
async def hgs(core_factory) -> Harness:
    return await core_factory()
