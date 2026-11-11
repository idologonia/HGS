"""Admin API — local only (127.0.0.1), used by the GUI.

Protection: separate port bound to 127.0.0.1, random bearer token per HGS run,
Host header check (DNS rebinding), requests with a browser Origin are rejected (CSRF).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import TYPE_CHECKING, Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from hgs import __version__
from hgs.core.config.manifest import GAME_ID_PATTERN
from hgs.core.config.settings import NetworkSettings
from hgs.core.network.manager import NetworkError
from hgs.core.rooms import RoomError
from hgs.core.server_manager import GameError

if TYPE_CHECKING:
    from hgs.core.app import CoreApp

log = logging.getLogger("hgs.admin")

_STATUS = {"not_found": 404, "conflict": 409, "bad_request": 400, "forbidden": 403}


class AddGame(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    manifest: dict | None = None  # when the folder has no hgs-game.json


class ManifestBody(BaseModel):
    manifest: dict


class FileName(BaseModel):
    file: str = Field(min_length=1, max_length=200)


class RoomUpdate(BaseModel):
    status: str | None = Field(None, pattern="^(open|locked)$")
    max_players: int | None = Field(None, ge=1, le=1000)


class GeneralSettings(BaseModel):
    server_name: str = Field(min_length=1, max_length=64)
    max_connections_per_ip: int = Field(ge=1, le=1000)
    hello_timeout_s: float = Field(gt=0, le=60)
    log_max_file_mb: int = Field(ge=1, le=1000)
    log_keep_files: int = Field(ge=1, le=100)
    metrics_interval_s: float = Field(ge=0.5, le=60)


class ProgramPath(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class StopGame(BaseModel):
    grace_s: float = Field(5.0, ge=0, le=300)


class CreateInvite(BaseModel):
    game_id: str = Field(pattern=GAME_ID_PATTERN)
    max_uses: int = Field(1, ge=1, le=1000)
    ttl_s: int = Field(7 * 86400, ge=60, le=30 * 86400)
    label: str | None = Field(None, max_length=64)


class CreateRoom(BaseModel):
    game_id: str = Field(pattern=GAME_ID_PATTERN)
    name: str = Field(min_length=1, max_length=24)
    max_players: int | None = Field(None, ge=1, le=1000)


class Kick(BaseModel):
    reason: str = Field("kicked by admin", max_length=120)


class Ban(BaseModel):
    reason: str = Field("banned by admin", max_length=120)


class NetworkUpdate(BaseModel):
    network: NetworkSettings
    gateway_port: int | None = Field(None, ge=1, le=65535)


class SecretValue(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


class GamePassword(BaseModel):
    password: str = Field(min_length=6, max_length=128)


def _check_host_origin(core: "CoreApp", headers: Any) -> None:
    host = (headers.get("host") or "").lower()
    if host not in core.admin_allowed_hosts():
        raise HTTPException(403, "invalid host")
    if headers.get("origin"):
        raise HTTPException(403, "browser requests are not allowed")


def _check_token(core: "CoreApp", value: str | None) -> None:
    expected = "Bearer " + core.admin_token
    if not value or not hmac.compare_digest(value.encode(), expected.encode()):
        raise HTTPException(401, "invalid admin token")


def create_admin_app(core: "CoreApp") -> FastAPI:
    async def guard(request: Request) -> None:
        _check_host_origin(core, request.headers)
        _check_token(core, request.headers.get("authorization"))

    app = FastAPI(title="HGS Admin API", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    api = "/admin/v1"
    deps = [Depends(guard)]

    @app.exception_handler(GameError)
    async def _game_error(request: Request, exc: GameError) -> JSONResponse:
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=_STATUS.get(exc.code, 400))

    @app.exception_handler(RoomError)
    async def _room_error(request: Request, exc: RoomError) -> JSONResponse:
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=_STATUS.get(exc.code, 400))

    @app.exception_handler(NetworkError)
    async def _network_error(request: Request, exc: NetworkError) -> JSONResponse:
        return JSONResponse({"error": "bad_request", "message": str(exc)}, status_code=400)

    @app.exception_handler(ValueError)
    async def _value_error(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"error": "bad_request", "message": str(exc)}, status_code=400)

    # -- status -----------------------------------------------------------------
    @app.get(f"{api}/status", dependencies=deps)
    async def status() -> dict:
        return core.status()

    @app.get(f"{api}/adapters", dependencies=deps)
    async def adapters() -> list:
        return core.registry.types()

    @app.get(f"{api}/events/recent", dependencies=deps)
    async def recent_events(limit: int = Query(50, ge=1, le=200)) -> list:
        return [e.to_dict() for e in list(core.bus.recent)[-limit:]]

    @app.post(f"{api}/shutdown", dependencies=deps)
    async def shutdown() -> dict:
        core.request_shutdown("requested by GUI")
        return {"ok": True}

    # -- games ------------------------------------------------------------------
    @app.get(f"{api}/games", dependencies=deps)
    async def games() -> list:
        return core.servers.all_views()

    @app.post(f"{api}/games", dependencies=deps, status_code=201)
    async def add_game(body: AddGame) -> dict:
        game = await core.servers.add_game(body.path, body.manifest)
        return core.servers.game_view(game)

    @app.get(f"{api}/games/{{game_id}}/manifest", dependencies=deps)
    async def get_manifest(game_id: str) -> dict:
        g = core.servers._get(game_id)
        return {"manifest": g.manifest.model_dump(), "source": g.manifest_source}

    @app.put(f"{api}/games/{{game_id}}/manifest", dependencies=deps)
    async def put_manifest(game_id: str, body: ManifestBody) -> dict:
        return core.servers.game_view(await core.servers.update_manifest(game_id, body.manifest))

    # -- saves and backups --------------------------------------------------------------
    @app.get(f"{api}/games/{{game_id}}/saves", dependencies=deps)
    async def saves(game_id: str) -> dict:
        core.servers._get(game_id)
        return {"snapshots": core.saves.list_snapshots(game_id), "backups": core.saves.list_backups(game_id),
                "journal": await core.saves.history(game_id, 50)}

    @app.post(f"{api}/games/{{game_id}}/save", dependencies=deps)
    async def save_now(game_id: str) -> dict:
        g = core.servers._get(game_id)
        if not core.servers.is_running(game_id):
            raise GameError("conflict", "the server is not running")
        result = await g.adapter.save("manual")
        if result is None:
            raise GameError("bad_request", "this game does not save its state (or saving is disabled in its settings)")
        return result

    @app.post(f"{api}/games/{{game_id}}/backup", dependencies=deps)
    async def backup(game_id: str) -> dict:
        core.servers._get(game_id)
        return await core.saves.backup(game_id, "manual")

    def _require_stopped(game_id: str) -> None:
        g = core.servers._get(game_id)
        if g.state.value not in ("stopped", "error", "failed"):
            raise GameError("conflict", "stop the server before restoring")

    @app.post(f"{api}/games/{{game_id}}/restore-snapshot", dependencies=deps)
    async def restore_snapshot(game_id: str, body: FileName) -> dict:
        _require_stopped(game_id)
        return await core.saves.restore_snapshot(game_id, body.file)

    @app.post(f"{api}/games/{{game_id}}/restore-backup", dependencies=deps)
    async def restore_backup(game_id: str, body: FileName) -> dict:
        _require_stopped(game_id)
        return await core.saves.restore_backup(game_id, body.file)

    # -- general settings ---------------------------------------------------------------
    @app.get(f"{api}/settings", dependencies=deps)
    async def get_settings() -> dict:
        st = core.settings
        return {"server_name": st.server_name, "max_connections_per_ip": st.gateway.max_connections_per_ip,
                "hello_timeout_s": st.gateway.hello_timeout_s, "log_max_file_mb": st.logs.max_file_mb,
                "log_keep_files": st.logs.keep_files, "metrics_interval_s": st.resources.metrics_interval_s,
                "admin_port": st.admin.port, "worker_bus_port": st.worker_bus.port,
                "allowed_programs": st.allowed_programs, "data_dir": str(core.paths.root),
                "secrets_protected": core.secrets.protected}

    @app.put(f"{api}/settings", dependencies=deps)
    async def put_settings(body: GeneralSettings) -> dict:
        st = core.settings
        st.server_name = body.server_name
        st.gateway.max_connections_per_ip = body.max_connections_per_ip
        st.gateway.hello_timeout_s = body.hello_timeout_s
        st.logs.max_file_mb = body.log_max_file_mb
        st.logs.keep_files = body.log_keep_files
        st.resources.metrics_interval_s = body.metrics_interval_s
        core.resources.interval_s = body.metrics_interval_s
        core._save_settings()
        core.bus.publish("settings.changed")
        return await get_settings()

    # -- allowlist of programs ---------------------------------------------------------
    @app.get(f"{api}/allowlist", dependencies=deps)
    async def allowlist() -> list:
        return core.settings.allowed_programs

    @app.post(f"{api}/allowlist", dependencies=deps)
    async def allow_program(body: ProgramPath) -> list:
        return await core.allow_program(body.path)

    @app.post(f"{api}/allowlist/remove", dependencies=deps)
    async def disallow_program(body: ProgramPath) -> list:
        return await core.disallow_program(body.path)

    @app.get(f"{api}/games/{{game_id}}", dependencies=deps)
    async def game(game_id: str) -> dict:
        return core.servers.game_view(core.servers._get(game_id))

    @app.delete(f"{api}/games/{{game_id}}", dependencies=deps)
    async def remove_game(game_id: str) -> dict:
        await core.servers.remove_game(game_id)
        return {"ok": True}

    @app.post(f"{api}/games/{{game_id}}/start", dependencies=deps)
    async def start(game_id: str) -> dict:
        await core.servers.start(game_id)
        return core.servers.game_view(core.servers._get(game_id))

    @app.post(f"{api}/games/{{game_id}}/stop", dependencies=deps)
    async def stop(game_id: str, body: StopGame = Body(default_factory=StopGame)) -> dict:
        await core.servers.stop(game_id, body.grace_s)
        return core.servers.game_view(core.servers._get(game_id))

    @app.post(f"{api}/games/{{game_id}}/restart", dependencies=deps)
    async def restart(game_id: str, body: StopGame = Body(default_factory=StopGame)) -> dict:
        await core.servers.restart(game_id, body.grace_s)
        return core.servers.game_view(core.servers._get(game_id))

    @app.post(f"{api}/games/{{game_id}}/reload", dependencies=deps)
    async def reload(game_id: str) -> dict:
        return core.servers.game_view(await core.servers.reload_manifest(game_id))

    @app.get(f"{api}/games/{{game_id}}/invites", dependencies=deps)
    async def invites(game_id: str) -> list:
        core.servers._get(game_id)
        return await core.auth.list_invites(game_id)

    # -- logs -------------------------------------------------------------------
    @app.get(f"{api}/logs", dependencies=deps)
    async def log_sources() -> list:
        return sorted(core.logs.tails)

    @app.get(f"{api}/logs/{{source}}", dependencies=deps)
    async def logs(source: str, tail: int = Query(200, ge=1, le=2000)) -> list:
        if source.startswith("game:") and source[5:] not in core.servers.games:
            raise GameError("not_found", "unknown log source")
        return core.logs.tail(source, tail)

    # -- invites, rooms, players --------------------------------------------------
    @app.post(f"{api}/invites", dependencies=deps, status_code=201)
    async def create_invite(body: CreateInvite) -> dict:
        game = core.servers._get(body.game_id)
        inv = await core.auth.create_invite(body.game_id, max_uses=body.max_uses, ttl_s=body.ttl_s, label=body.label)
        inv["server_url"] = core.network.player_url()
        inv["link"] = core.network.invite_link(game.manifest.client.url, inv["code"])
        return inv

    @app.post(f"{api}/games/{{game_id}}/password", dependencies=deps)
    async def set_password(game_id: str, body: GamePassword) -> dict:
        core.servers._get(game_id)
        await core.auth.set_game_password(game_id, body.password)
        log.info("game password changed for %s", game_id)
        return {"ok": True}

    @app.get(f"{api}/games/{{game_id}}/auth", dependencies=deps)
    async def game_auth(game_id: str) -> dict:
        g = core.servers._get(game_id)
        return {"mode": g.manifest.auth.mode, "password_set": await core.auth.has_game_password(game_id),
                "allow_register": g.manifest.auth.allow_register}

    # -- network --------------------------------------------------------------------
    @app.get(f"{api}/network", dependencies=deps)
    async def network() -> dict:
        view = core.network.view()
        view["secrets_set"] = await core.secrets.names()
        return view

    @app.put(f"{api}/network", dependencies=deps)
    async def update_network(body: NetworkUpdate) -> dict:
        await core.network.apply(body.network, body.gateway_port)
        return core.network.view()

    @app.post(f"{api}/network/tunnel/start", dependencies=deps)
    async def tunnel_start() -> dict:
        await core.network.start_tunnel()
        return core.network.view()

    @app.post(f"{api}/network/tunnel/stop", dependencies=deps)
    async def tunnel_stop() -> dict:
        await core.network.stop_tunnel()
        return core.network.view()

    @app.post(f"{api}/network/check", dependencies=deps)
    async def network_check() -> dict:
        return await core.network.check_reachability()

    @app.post(f"{api}/network/public-ip", dependencies=deps)
    async def public_ip() -> dict:
        ip = await core.network.lookup_public_ip()
        return {"public_ip": ip, "error": core.network.public_ip_error}

    @app.post(f"{api}/network/cloudflared/download", dependencies=deps)
    async def download_cloudflared() -> dict:
        from hgs.core.network.cloudflared_download import DownloadError, download

        try:
            result = await asyncio.to_thread(download, core.paths.root / "bin")
        except DownloadError as exc:
            raise GameError("bad_request", str(exc))
        log.info("cloudflared %s downloaded and verified (sha256 %s)", result["version"], result["sha256"])
        return result

    @app.post(f"{api}/network/publish", dependencies=deps)
    async def publish_now() -> dict:
        from hgs.core.network.publisher import PublishError

        try:
            return await core.network.publisher.publish()
        except PublishError as exc:
            raise GameError("bad_request", str(exc))

    @app.put(f"{api}/secrets/{{name}}", dependencies=deps)
    async def set_secret(name: str, body: SecretValue) -> dict:
        await core.secrets.set(name, body.value.strip())
        return {"ok": True, "protected": core.secrets.protected}

    @app.delete(f"{api}/secrets/{{name}}", dependencies=deps)
    async def delete_secret(name: str) -> dict:
        await core.secrets.delete(name)
        return {"ok": True}

    @app.get(f"{api}/rooms", dependencies=deps)
    async def rooms(game_id: str | None = None) -> list:
        return [r.admin_view() for r in core.rooms.list(game_id)]

    @app.post(f"{api}/rooms", dependencies=deps, status_code=201)
    async def create_room(body: CreateRoom) -> dict:
        game = core.servers._get(body.game_id)
        if not core.servers.is_running(body.game_id):
            raise GameError("conflict", "start the server first")
        r = game.manifest.rooms
        room = core.rooms.create(body.game_id, body.name, max_players=min(body.max_players or r.max_players, r.max_players),
                                 max_rooms=r.max_rooms, persistent=True, created_by="admin")
        return room.admin_view()

    @app.post(f"{api}/rooms/{{game_id}}/{{room_id}}/update", dependencies=deps)
    async def update_room(game_id: str, room_id: str, body: RoomUpdate) -> dict:
        room = core.rooms.update(game_id, room_id, status=body.status, max_players=body.max_players)
        if room is None:
            raise GameError("not_found", "room not found")
        return room.admin_view()

    @app.delete(f"{api}/rooms/{{game_id}}/{{room_id}}", dependencies=deps)
    async def close_room(game_id: str, room_id: str) -> dict:
        if core.rooms.close(game_id, room_id, "closed by admin") is None:
            raise GameError("not_found", "room not found")
        return {"ok": True}

    @app.get(f"{api}/players", dependencies=deps)
    async def players(game_id: str | None = None) -> list:
        return [
            {"game_id": s.game_id, "player_id": s.player_id, "name": s.name, "room_id": s.room_id, "ip": s.ip,
             "connected_at": s.connected_at, "last_activity": s.last_activity}
            for s in core.sessions.all() if game_id is None or s.game_id == game_id
        ]

    @app.get(f"{api}/players/known", dependencies=deps)
    async def known_players(game_id: str) -> list:
        core.servers._get(game_id)
        rows = await core.db.fetchall(
            "SELECT player_id, display_name AS name, auth_method, created_at, last_seen_at, banned, ban_reason "
            "FROM players WHERE game_id=? ORDER BY last_seen_at DESC LIMIT 500", (game_id,))
        online = {s.player_id for s in core.sessions.for_game(game_id)}
        for r in rows:
            r["online"] = r["player_id"] in online
        return rows

    @app.post(f"{api}/players/{{game_id}}/{{player_id}}/ban", dependencies=deps)
    async def ban(game_id: str, player_id: str, body: Ban = Body(default_factory=Ban)) -> dict:
        if not await core.auth.set_banned(game_id, player_id, True, body.reason):
            raise GameError("not_found", "player not found")
        await core.auth.revoke_player_tokens(game_id, player_id)
        core.gateway.kick(game_id, player_id, body.reason)
        log.info("player %s banned in %s: %s", player_id, game_id, body.reason)
        return {"ok": True}

    @app.post(f"{api}/players/{{game_id}}/{{player_id}}/unban", dependencies=deps)
    async def unban(game_id: str, player_id: str) -> dict:
        if not await core.auth.set_banned(game_id, player_id, False):
            raise GameError("not_found", "player not found")
        return {"ok": True}

    @app.post(f"{api}/players/{{game_id}}/{{player_id}}/revoke-tokens", dependencies=deps)
    async def revoke(game_id: str, player_id: str) -> dict:
        n = await core.auth.revoke_player_tokens(game_id, player_id)
        return {"ok": True, "revoked": n}

    @app.post(f"{api}/players/{{game_id}}/{{player_id}}/kick", dependencies=deps)
    async def kick(game_id: str, player_id: str, body: Kick = Body(default_factory=Kick)) -> dict:
        if not core.gateway.kick(game_id, player_id, body.reason):
            raise GameError("not_found", "player is not online")
        log.info("player %s kicked from %s: %s", player_id, game_id, body.reason)
        return {"ok": True}

    # -- event stream -------------------------------------------------------------
    @app.websocket(f"{api}/events")
    async def events(ws: WebSocket) -> None:
        try:
            _check_host_origin(core, ws.headers)
            _check_token(core, ws.headers.get("authorization"))
        except HTTPException:
            await ws.close(code=4401)
            return
        await ws.accept()
        sub = core.bus.subscribe()
        try:
            await ws.send_json({"type": "core.hello", "ts": 0, "data": core.status()})
            receiver = asyncio.create_task(ws.receive())
            while True:
                getter = asyncio.create_task(sub.get())
                done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
                if receiver in done:
                    getter.cancel()
                    message = receiver.result()
                    if message["type"] == "websocket.disconnect":
                        return
                    receiver = asyncio.create_task(ws.receive())
                    continue
                await ws.send_json(getter.result().to_dict())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            sub.close()
            for t in (locals().get("receiver"), locals().get("getter")):
                if t is not None and not t.done():
                    t.cancel()

    return app
