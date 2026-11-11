"""Public Gateway: the only entry point for players (WebSocket /hgs/v1/ws).

There are NO admin routes here: the admin API is a separate app on a separate,
local-only port. Path of every incoming frame (docs 02, §2.8):

  size check -> JSON -> envelope schema -> seq -> rate limit -> permissions -> handler
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from hgs import PROTOCOL_VERSION, __version__
from hgs.adapters.base import Capability, PlayerRef, RoomRef
from hgs.core.auth import AuthError, PlayerInfo
from hgs.core.protocol.client import (
    CLOSE_FLOOD,
    CLOSE_GAME_STOPPED,
    CLOSE_HELLO_TIMEOUT,
    CLOSE_KICKED,
    CLOSE_PROTOCOL,
    CLOSE_REPLACED,
    CLOSE_TOO_LARGE,
    CLOSE_UNAUTHORIZED,
    AuthInvite,
    AuthOpen,
    AuthToken,
    GameEvent,
    ProtocolError,
    RoomCreate,
    RoomJoin,
    RoomStatePatch,
    RoomStateSet,
    msg,
    parse_frame,
    parse_hello,
    parse_payload,
    reply_err,
    reply_ok,
)
from hgs.core.rooms import Room, RoomError, json_size
from hgs.core.sessions import Session

if TYPE_CHECKING:
    from hgs.core.app import CoreApp

log = logging.getLogger("hgs.gateway")

HELLO_MAX_BYTES = 4096
MAX_BAD_MESSAGES = 10
ADAPTER_CALL_TIMEOUT_S = 5.0
# Hard cap for the transport; per-game limits are smaller and checked in parse_frame.
TRANSPORT_MAX_BYTES = 1024 * 1024 + 1024


def sdk_path():
    """Location of hgs.js: bundled with the package (builds) or in the repository (development)."""
    from pathlib import Path

    here = Path(__file__).resolve()
    for candidate in (here.parents[2] / "static" / "hgs.js", here.parents[4] / "sdk" / "js" / "hgs.js"):
        if candidate.is_file():
            return candidate
    return None


class Gateway:
    def __init__(self, core: "CoreApp") -> None:
        self.core = core
        # Random value that lets a reachability check prove it reached THIS instance.
        self.instance_nonce = secrets.token_hex(8)
        self.app = Starlette(
            routes=[
                WebSocketRoute("/hgs/v1/ws", self.ws_endpoint),
                Route("/hgs/v1/health", self.health),
                Route("/hgs/v1/info", self.info),
                Route("/hgs/sdk/hgs.js", self.sdk),
            ]
        )

    # == HTTP ======================================================================
    async def sdk(self, request: Request) -> Response:
        path = sdk_path()
        if path is None:
            return JSONResponse({"error": "sdk_not_found"}, status_code=404)
        return Response(path.read_bytes(), media_type="text/javascript; charset=utf-8",
                        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})

    async def health(self, request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "service": "hgs", "instance": self.instance_nonce})

    async def info(self, request: Request) -> JSONResponse:
        game_id = request.query_params.get("game")
        out: dict[str, Any] = {
            "server_name": self.core.settings.server_name,
            "protocol": PROTOCOL_VERSION,
            "version": __version__,
        }
        if game_id:
            g = self.core.servers.games.get(game_id)
            if g is None:
                return JSONResponse({"error": "game_not_found"}, status_code=404)
            out["game"] = {"id": g.game_id, "name": g.manifest.name, "running": self.core.servers.is_running(game_id),
                           "auth_mode": g.manifest.auth.mode, "mode": (g.manifest.backend or {}).get("mode", "relay")}
        # public, non-sensitive information: browsers on any site may read it
        return JSONResponse(out, headers={"Access-Control-Allow-Origin": "*"})

    # == WebSocket =================================================================
    def _client_ip(self, ws: WebSocket) -> str:
        return ws.client.host if ws.client else "unknown"

    async def ws_endpoint(self, ws: WebSocket) -> None:
        ip = self._client_ip(ws)
        sessions = self.core.sessions
        if sessions.ip_connections(ip) >= self.core.settings.gateway.max_connections_per_ip:
            await self.core.journal.record("too_many_connections", ip=ip)
            await ws.close(code=1008)
            return
        sessions.ip_acquire(ip)
        session: Session | None = None
        try:
            await ws.accept()
            session = await self._handshake(ws, ip)
            if session is None:
                return
            await self._receive_loop(ws, session)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("unexpected gateway error (ip=%s)", ip)
            if ws.application_state == WebSocketState.CONNECTED:
                try:
                    await ws.close(code=1011)
                except Exception:
                    pass
        finally:
            sessions.ip_release(ip)
            if session is not None:
                await self._cleanup(session, "disconnected")

    async def _send_and_close(self, ws: WebSocket, text: str, code: int) -> None:
        try:
            await ws.send_text(text)
            await ws.close(code=code)
        except Exception:
            pass

    async def _handshake(self, ws: WebSocket, ip: str) -> Session | None:
        core = self.core
        try:
            message = await asyncio.wait_for(ws.receive(), core.settings.gateway.hello_timeout_s)
        except asyncio.TimeoutError:
            await self._send_and_close(ws, msg("auth_error", {"code": "timeout", "message": "hello not received in time"}),
                                       CLOSE_HELLO_TIMEOUT)
            return None
        if message["type"] == "websocket.disconnect":
            return None
        try:
            env = parse_frame(message.get("text") if message.get("text") is not None else message.get("bytes", b""),
                              HELLO_MAX_BYTES)
            if env.t != "hello":
                raise ProtocolError("bad_request", "first message must be 'hello'")
            hello = parse_hello(env.d)
        except ProtocolError as exc:
            await core.journal.record("bad_hello", ip=ip, reason=exc.message)
            await self._send_and_close(ws, msg("auth_error", {"code": exc.code, "message": exc.message}), CLOSE_PROTOCOL)
            return None

        game = core.servers.games.get(hello.game)
        if game is None or not core.servers.is_running(hello.game):
            await self._send_and_close(
                ws, msg("auth_error", {"code": "game_unavailable", "message": "game server is not running"}),
                CLOSE_GAME_STOPPED)
            return None

        manifest = game.manifest
        origin = ws.headers.get("origin")
        allowed = manifest.client.allowed_origins
        if allowed and origin is not None and origin.lower().rstrip("/") not in allowed:
            await core.journal.record("origin_rejected", game_id=hello.game, ip=ip, origin=origin[:200])
            await self._send_and_close(ws, msg("auth_error", {"code": "forbidden", "message": "origin not allowed"}),
                                       CLOSE_KICKED)
            return None

        new_token: str | None = None
        try:
            player, new_token = await self._authenticate(hello.game, manifest.auth.mode, hello.auth, ip)
        except AuthError as exc:
            await self._send_and_close(ws, msg("auth_error", {"code": exc.code, "message": exc.message}),
                                       CLOSE_FLOOD if exc.code == "rate_limited" else CLOSE_UNAUTHORIZED)
            return None

        # The game could have been stopped while we were checking credentials.
        if not core.servers.is_running(hello.game):
            await self._send_and_close(ws, msg("auth_error", {"code": "game_unavailable",
                                                              "message": "game server is not running"}), CLOSE_GAME_STOPPED)
            return None

        async def _send(text: str) -> None:
            await ws.send_text(text)

        async def _close(code: int, reason: str) -> None:
            if ws.application_state == WebSocketState.CONNECTED:
                await ws.close(code=code, reason=reason)

        limits = manifest.limits
        session = Session(
            game_id=hello.game, player_id=player.player_id, name=player.name, ip=ip,
            send_text=_send, close_ws=_close,
            rate=limits.messages_per_s, burst=limits.burst, max_message_bytes=limits.max_message_bytes,
        )
        session.last_seq = env.seq or 0
        old = core.sessions.add(session)
        if old is not None:
            await self._leave_room(old, "replaced")
            old.close(CLOSE_REPLACED, "connected from another place")
        welcome: dict[str, Any] = {
            "session_id": session.id,
            "resumed": False,
            "player": session.public(),
            "game": {"id": manifest.id, "name": manifest.name, "version": manifest.version,
                     "mode": (manifest.backend or {}).get("mode", "relay")},
            "server": {
                "protocol": PROTOCOL_VERSION,
                "heartbeat_s": 15,
                "limits": {"max_message_bytes": limits.max_message_bytes, "messages_per_s": limits.messages_per_s,
                           "burst": limits.burst},
            },
        }
        if new_token:
            welcome["player_token"] = new_token
        session.send(msg("welcome", welcome))
        log.info("player %s (%s) connected to %s from %s", player.name, player.player_id, hello.game, ip)
        core.bus.publish("player.connected", game_id=hello.game, player=session.public(), ip=ip)
        return session

    async def _authenticate(self, game_id: str, mode: str, auth: Any, ip: str) -> tuple[PlayerInfo, str | None]:
        a = self.core.auth
        if mode == "open":
            if not isinstance(auth, AuthOpen):
                raise AuthError("unauthorized", "this game uses open login: send {type:'open', name}")
            return await a.login_open(game_id, auth.name), None
        if isinstance(auth, AuthInvite):
            return await a.redeem_invite(game_id, auth.code, auth.name, ip)
        if isinstance(auth, AuthToken):
            return await a.login_token(game_id, auth.token, ip), None
        raise AuthError("unauthorized", "this game requires an invite code or a player token")

    async def _receive_loop(self, ws: WebSocket, s: Session) -> None:
        denied = 0
        while not s.closed:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            raw = message.get("text") if message.get("text") is not None else message.get("bytes", b"")
            s.messages_in += 1
            s.last_activity = time.time()
            if not s.bucket.allow():
                denied += 1
                if denied > s.bucket.burst:
                    await self.core.journal.record("flood", game_id=s.game_id, ip=s.ip, player_id=s.player_id)
                    s.close(CLOSE_FLOOD, "too many messages")
                    return
                s.send(msg("error", {"code": "rate_limited", "message": "slow down"}))
                continue
            denied = max(0, denied - 1)
            req_id = None
            try:
                env = parse_frame(raw, s.max_message_bytes)
                req_id = env.id
                if env.seq is None or env.seq <= s.last_seq:
                    raise ProtocolError("invalid_seq", "seq must increase with every message")
                s.last_seq = env.seq
                await self._dispatch(s, env.t, env.d, req_id)
                s.bad_messages = 0
            except ProtocolError as exc:
                if exc.code == "too_large":
                    await self.core.journal.record("message_too_large", game_id=s.game_id, ip=s.ip, player_id=s.player_id)
                    s.send(msg("error", {"code": exc.code, "message": exc.message}))
                    s.close(CLOSE_TOO_LARGE, "message too large")
                    return
                s.bad_messages += 1
                s.send(reply_err(req_id, exc.code, exc.message) if req_id else
                       msg("error", {"code": exc.code, "message": exc.message}))
                if s.bad_messages >= MAX_BAD_MESSAGES:
                    await self.core.journal.record("malformed_messages", game_id=s.game_id, ip=s.ip, player_id=s.player_id)
                    s.close(CLOSE_PROTOCOL, "too many invalid messages")
                    return
            except RoomError as exc:
                s.send(reply_err(req_id, exc.code, exc.message) if req_id else
                       msg("error", {"code": exc.code, "message": exc.message}))

    # == handlers ==================================================================
    async def _dispatch(self, s: Session, t: str, d: Any, req_id: str | None) -> None:
        core = self.core
        game = core.servers.games.get(s.game_id)
        if game is None or not core.servers.is_running(s.game_id):
            s.close(CLOSE_GAME_STOPPED, "game server is not running")
            return
        manifest = game.manifest

        if t == "ping":
            s.send(msg("pong", {"ts": time.time()}, re=req_id))
        elif t == "room.list":
            rooms = [r.public() for r in core.rooms.list(s.game_id) if not r.private]
            s.send(reply_ok(req_id, {"rooms": rooms}))
        elif t == "room.create":
            if not manifest.rooms.allow_player_create:
                raise RoomError("forbidden", "players cannot create rooms in this game")
            p = parse_payload(RoomCreate, d)
            max_players = min(p.max_players or manifest.rooms.max_players, manifest.rooms.max_players)
            if json_size(p.params) > 4096:
                raise ProtocolError("too_large", "room params are too large")
            room = core.rooms.create(s.game_id, p.name, max_players=max_players, max_rooms=manifest.rooms.max_rooms,
                                     params=p.params, private=p.private, created_by=s.player_id)
            s.send(reply_ok(req_id, {"room": room.public()}))
        elif t == "room.join":
            p = parse_payload(RoomJoin, d)
            room = core.rooms.get(s.game_id, p.room_id)
            if room is None:
                raise RoomError("room_not_found", "room not found")
            decision = await self._adapter_call(s, "handle_connection", self._pref(s), self._rref(room))
            if decision is None:
                raise RoomError("internal", "game server error")
            if not decision.accept:
                raise RoomError("forbidden", decision.reason or "join rejected by the game")
            if s.room_id and s.room_id != room.room_id:
                await self._leave_room(s, "switched_room")
            room = core.rooms.join(s, room.room_id)
            s.send(reply_ok(req_id, {"room": room.public()}))
            s.send(msg("room.joined", core.rooms.joined_payload(room)))
        elif t == "room.leave":
            await self._leave_room(s, "left")
            s.send(reply_ok(req_id))
        elif t in ("room.state.set", "room.state.patch"):
            room = self._require_room(s)
            adapter = game.adapter
            if adapter is None or Capability.ROOM_STATE not in adapter.capabilities():
                raise RoomError("forbidden", "this game does not use shared room state")
            if room.host != s.player_id:
                raise RoomError("forbidden", "only the room host can change the room state")
            if t == "room.state.set":
                new_state = parse_payload(RoomStateSet, d).state
            else:
                patch = parse_payload(RoomStatePatch, d).patch
                base = room.state if isinstance(room.state, dict) else {}
                new_state = {**base, **patch}
            if json_size(new_state) > manifest.limits.room_state_bytes:
                raise ProtocolError("too_large", "room state exceeds the limit")
            room.state = new_state
            room.broadcast(msg("room.state", {"state": new_state}), exclude=s.player_id)
            s.send(reply_ok(req_id))
        elif t == "game":
            p = parse_payload(GameEvent, d)
            room = self._require_room(s)
            if p.to is not None and p.to not in room.players:
                raise RoomError("bad_request", "target player is not in this room")
            await self._adapter_call(s, "handle_message", self._pref(s), self._rref(room), p.e, p.d, p.to)
            if req_id:
                s.send(reply_ok(req_id))
        elif t == "hello":
            raise ProtocolError("bad_request", "already authenticated")
        else:
            raise ProtocolError("bad_request", f"unknown message type {t!r}")

    def _require_room(self, s: Session) -> Room:
        room = self.core.rooms.get(s.game_id, s.room_id) if s.room_id else None
        if room is None:
            raise RoomError("forbidden", "join a room first")
        return room

    @staticmethod
    def _pref(s: Session) -> PlayerRef:
        return PlayerRef(s.player_id, s.name)

    @staticmethod
    def _rref(room: Room) -> RoomRef:
        return RoomRef(room.room_id, room.name, dict(room.params))

    async def _adapter_call(self, s: Session, method: str, *args: Any) -> Any:
        game = self.core.servers.games.get(s.game_id)
        adapter = game.adapter if game else None
        if adapter is None:
            return None
        try:
            return await asyncio.wait_for(getattr(adapter, method)(*args), ADAPTER_CALL_TIMEOUT_S)
        except Exception as exc:
            # A failing game handler must not break the connection or the core.
            adapter.ctx.log.error("%s failed: %s: %s", method, type(exc).__name__, exc)
            return None

    async def _leave_room(self, s: Session, reason: str) -> None:
        if not s.room_id:
            return
        room = self.core.rooms.leave(s, reason)
        if room is not None:
            await self._adapter_call(s, "handle_disconnect", self._pref(s), self._rref(room), reason)

    async def _cleanup(self, s: Session, reason: str) -> None:
        await self._leave_room(s, reason)
        self.core.sessions.remove(s)
        s.close(1000, "bye")
        await s.wait_closed(2.0)
        try:
            await self.core.db.execute("UPDATE players SET last_seen_at=datetime('now') WHERE game_id=? AND player_id=?",
                                       (s.game_id, s.player_id))
        except Exception:
            pass
        log.info("player %s (%s) disconnected from %s", s.name, s.player_id, s.game_id)
        self.core.bus.publish("player.disconnected", game_id=s.game_id, player=s.public(), code=s.close_code)

    def kick(self, game_id: str, player_id: str, reason: str) -> bool:
        s = self.core.sessions.get_player(game_id, player_id)
        if s is None:
            return False
        s.send(msg("kicked", {"reason": reason}))
        s.close(CLOSE_KICKED, reason)
        return True
