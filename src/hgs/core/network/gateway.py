"""Public Gateway: the only entry point for players (WebSocket /hgs/v1/ws).

There are NO admin routes here: the admin API is a separate app on a separate,
local-only port. Path of every incoming frame (docs 02, §2.8):

  size check -> JSON -> envelope schema -> seq -> rate limit -> permissions -> handler
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

import websockets

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
    AuthAccount,
    AuthInvite,
    AuthOpen,
    AuthPassword,
    AuthResume,
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
RESUME_WINDOW_S = 30


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
                WebSocketRoute("/hgs/v1/proxy/{game_id}", self.proxy_endpoint),
                WebSocketRoute("/hgs/v1/proxy/{game_id}/{path:path}", self.proxy_endpoint),
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
        peer = ws.client.host if ws.client else "unknown"
        # Behind the tunnel every connection comes from the local cloudflared process; the real
        # address is in CF-Connecting-IP. The header is trusted ONLY in tunnel mode and only from
        # a local peer — anyone else could forge it.
        if self.core.settings.network.mode == "tunnel" and peer in ("127.0.0.1", "::1"):
            cf = ws.headers.get("cf-connecting-ip", "") or ws.headers.get("x-forwarded-for", "").split(",")[0]
            try:
                return str(ipaddress.ip_address(cf.strip()))
            except ValueError:
                pass
        return peer

    async def ws_endpoint(self, ws: WebSocket) -> None:
        ip = self._client_ip(ws)
        sessions = self.core.sessions
        if sessions.ip_connections(ip) >= self.core.settings.gateway.max_connections_per_ip:
            await self.core.journal.record("too_many_connections", ip=ip)
            await ws.close(code=1008)
            return
        sessions.ip_acquire(ip)
        session: Session | None = None
        generation = 0
        client_code: int | None = None
        try:
            await ws.accept()
            session = await self._handshake(ws, ip)
            if session is None:
                return
            generation = session.generation
            client_code = await self._receive_loop(ws, session)
        except WebSocketDisconnect as exc:
            client_code = exc.code
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
                await self._connection_lost(session, generation, client_code)

    async def _connection_lost(self, s: Session, generation: int, client_code: int | None) -> None:
        if s.generation != generation or s.cleaned:
            return  # the session already moved to a newer connection
        intentional = client_code == 1000
        if s.closed or intentional or not self.core.servers.is_running(s.game_id):
            await self._cleanup(s, "left" if intentional else "disconnected")
            return
        s.detach(RESUME_WINDOW_S)
        log.info("player %s (%s) lost connection to %s; can resume for %d s", s.name, s.player_id, s.game_id,
                 RESUME_WINDOW_S)
        self.core.bus.publish("player.detached", game_id=s.game_id, player=s.public())

    def expire_sessions(self) -> None:
        for s in self.core.sessions.expired():
            asyncio.get_running_loop().create_task(self._cleanup(s, "timeout"))

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

        async def _send(text: str) -> None:
            await ws.send_text(text)

        async def _close(code: int, reason: str) -> None:
            if ws.application_state == WebSocketState.CONNECTED:
                await ws.close(code=code, reason=reason)

        if isinstance(hello.auth, AuthResume):
            return await self._resume(ws, ip, hello, env.seq or 0, _send, _close)

        new_token: str | None = None
        try:
            player, new_token = await self._authenticate(hello.game, manifest, hello.auth, ip)
        except AuthError as exc:
            await self._send_and_close(ws, msg("auth_error", {"code": exc.code, "message": exc.message}),
                                       CLOSE_FLOOD if exc.code == "rate_limited" else CLOSE_UNAUTHORIZED)
            return None

        # The game could have been stopped while we were checking credentials.
        if not core.servers.is_running(hello.game):
            await self._send_and_close(ws, msg("auth_error", {"code": "game_unavailable",
                                                              "message": "game server is not running"}), CLOSE_GAME_STOPPED)
            return None

        limits = manifest.limits
        session = Session(
            game_id=hello.game, player_id=player.player_id, name=player.name, ip=ip,
            rate=limits.messages_per_s, burst=limits.burst, max_message_bytes=limits.max_message_bytes,
        )
        session.on_dead = lambda s: asyncio.get_running_loop().create_task(self._cleanup(s, "closed"))
        session.attach(_send, _close)
        session.last_seq = env.seq or 0
        old = core.sessions.add(session)
        if old is not None:
            await self._leave_room(old, "replaced")
            old.close(CLOSE_REPLACED, "connected from another place")
            if old.detached:
                await self._cleanup(old, "replaced")
        welcome = self._welcome(session, manifest, resumed=False)
        if new_token:
            welcome["player_token"] = new_token
        session.send(msg("welcome", welcome))
        log.info("player %s (%s) connected to %s from %s", player.name, player.player_id, hello.game, ip)
        core.bus.publish("player.connected", game_id=hello.game, player=session.public(), ip=ip)
        return session

    def _welcome(self, session: Session, manifest: Any, resumed: bool) -> dict[str, Any]:
        limits = manifest.limits
        return {
            "session_id": session.id,
            "resume_token": session.new_resume_token(),
            "resumed": resumed,
            "player": session.public(),
            "game": {"id": manifest.id, "name": manifest.name, "version": manifest.version,
                     "mode": (manifest.backend or {}).get("mode", "relay")},
            "server": {
                "protocol": PROTOCOL_VERSION,
                "heartbeat_s": 15,
                "resume_window_s": RESUME_WINDOW_S,
                "limits": {"max_message_bytes": limits.max_message_bytes, "messages_per_s": limits.messages_per_s,
                           "burst": limits.burst},
            },
        }

    async def _resume(self, ws: WebSocket, ip: str, hello: Any, seq: int, send: Any, close: Any) -> Session | None:
        core = self.core
        auth = hello.auth
        s = core.sessions.get(auth.session_id)
        valid = (s is not None and s.game_id == hello.game and s.detached and not s.closed
                 and s.check_resume_token(auth.resume_token))
        if not valid:
            if s is not None and s.game_id == hello.game:
                # a known session with a wrong/used token: possible replay
                await core.journal.record("resume_token_rejected", "high", game_id=hello.game, ip=ip,
                                          player_id=s.player_id)
            await self._send_and_close(ws, msg("auth_error", {"code": "resume_failed",
                                                              "message": "session expired, log in again"}),
                                       CLOSE_UNAUTHORIZED)
            return None
        manifest = core.servers.games[hello.game].manifest
        s.attach(send, close)
        s.generation += 1
        s.ip = ip
        s.last_seq = seq
        room = core.rooms.get(s.game_id, s.room_id) if s.room_id else None
        welcome = self._welcome(s, manifest, resumed=True)
        welcome["room_id"] = room.room_id if room is not None else None
        s.send(msg("welcome", welcome))
        if room is not None and room.players.get(s.player_id) is s:
            s.send(msg("room.joined", core.rooms.joined_payload(room)))
        log.info("player %s (%s) resumed session on %s", s.name, s.player_id, s.game_id)
        core.bus.publish("player.resumed", game_id=s.game_id, player=s.public())
        return s

    async def _authenticate(self, game_id: str, manifest: Any, auth: Any, ip: str) -> tuple[PlayerInfo, str | None]:
        a = self.core.auth
        mode = manifest.auth.mode
        if isinstance(auth, AuthToken) and mode != "open":
            return await a.login_token(game_id, auth.token, ip), None
        if mode == "open":
            if self.core.settings.internet_exposed:
                raise AuthError("forbidden", "open login is disabled while the server is reachable from the internet")
            if not isinstance(auth, AuthOpen):
                raise AuthError("unauthorized", "this game uses open login: send {type:'open', name}")
            return await a.login_open(game_id, auth.name), None
        if mode == "invite" and isinstance(auth, AuthInvite):
            return await a.redeem_invite(game_id, auth.code, auth.name, ip)
        if mode == "password" and isinstance(auth, AuthPassword):
            return await a.login_password(game_id, auth.name, auth.password, ip)
        if mode == "account" and isinstance(auth, AuthAccount):
            return await a.login_account(game_id, auth.username, auth.password, auth.register_,
                                         manifest.auth.allow_register, ip)
        raise AuthError("unauthorized", f"this game requires {mode} login")

    async def _receive_loop(self, ws: WebSocket, s: Session) -> int | None:
        """Returns the client's close code (None when the server ended the session)."""
        denied = 0
        while not s.closed:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return message.get("code")
            raw = message.get("text") if message.get("text") is not None else message.get("bytes", b"")
            s.messages_in += 1
            s.last_activity = time.time()
            if not s.bucket.allow():
                denied += 1
                if denied > s.bucket.burst:
                    await self.core.journal.record("flood", game_id=s.game_id, ip=s.ip, player_id=s.player_id)
                    s.close(CLOSE_FLOOD, "too many messages")
                    return None
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
                    return None
                s.bad_messages += 1
                s.send(reply_err(req_id, exc.code, exc.message) if req_id else
                       msg("error", {"code": exc.code, "message": exc.message}))
                if s.bad_messages >= MAX_BAD_MESSAGES:
                    await self.core.journal.record("malformed_messages", game_id=s.game_id, ip=s.ip, player_id=s.player_id)
                    s.close(CLOSE_PROTOCOL, "too many invalid messages")
                    return None
            except RoomError as exc:
                s.send(reply_err(req_id, exc.code, exc.message) if req_id else
                       msg("error", {"code": exc.code, "message": exc.message}))
        return None

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
            if s.player_id in room.players:
                s.send(reply_ok(req_id, {"room": room.public()}))
                s.send(msg("room.joined", core.rooms.joined_payload(room)))
                return
            room.pending[s.player_id] = []
            try:
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
                for text in room.pending.get(s.player_id, []):
                    s.send(text)
            finally:
                room.pending.pop(s.player_id, None)
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
        if s.cleaned:
            return
        s.cleaned = True
        s.close(1000, "bye")  # no-op if already closed; ends the writer task
        await self._leave_room(s, reason)
        self.core.sessions.remove(s)
        await s.wait_closed(2.0)
        try:
            await self.core.db.execute("UPDATE players SET last_seen_at=datetime('now') WHERE game_id=? AND player_id=?",
                                       (s.game_id, s.player_id))
        except Exception:
            pass
        log.info("player %s (%s) disconnected from %s", s.name, s.player_id, s.game_id)
        self.core.bus.publish("player.disconnected", game_id=s.game_id, player=s.public(), code=s.close_code)

    # == proxy to standalone game servers ===========================================
    async def proxy_endpoint(self, ws: WebSocket) -> None:
        """Forward a WebSocket to a game's own server (python/process adapters, mode "standalone").

        HGS applies its limits (connections per IP, message size and rate, allowed origins) and,
        if the game asks for it (backend.proxy.require_login), an HGS login as the first message.
        """
        core = self.core
        game_id = ws.path_params["game_id"]
        ip = self._client_ip(ws)
        if core.sessions.ip_connections(ip) >= core.settings.gateway.max_connections_per_ip:
            await ws.close(code=1008)
            return
        game = core.servers.games.get(game_id)
        adapter = game.adapter if game and core.servers.is_running(game_id) else None
        target = adapter.proxy_target() if adapter is not None and hasattr(adapter, "proxy_target") else None
        origin = ws.headers.get("origin")
        allowed = game.manifest.client.allowed_origins if game else []
        core.sessions.ip_acquire(ip)
        session: Session | None = None
        upstream = None
        try:
            await ws.accept()
            if target is None:
                await ws.close(code=CLOSE_GAME_STOPPED, reason="game server is not running")
                return
            if allowed and origin is not None and origin.lower().rstrip("/") not in allowed:
                await core.journal.record("origin_rejected", game_id=game_id, ip=ip, origin=origin[:200])
                await ws.close(code=CLOSE_KICKED, reason="origin not allowed")
                return
            port, require_login = target
            manifest = game.manifest
            if require_login:
                player = await self._proxy_login(ws, game_id, manifest, ip)
                if player is None:
                    return
                pid, name = player.player_id, player.name
            else:
                pid, name = "g_" + secrets.token_hex(5), "guest"
            limits = manifest.limits
            session = Session(game_id=game_id, player_id=pid, name=name, ip=ip, rate=limits.messages_per_s,
                              burst=limits.burst, max_message_bytes=limits.max_message_bytes)

            async def _close(code: int, reason: str) -> None:
                if ws.application_state == WebSocketState.CONNECTED:
                    await ws.close(code=code, reason=reason)

            session.proxied = True
            session.attach(ws.send_text, _close)
            old = core.sessions.add(session)
            if old is not None:
                old.close(CLOSE_REPLACED, "connected from another place")
            path = ws.path_params.get("path", "")
            query = ws.url.query
            url = f"ws://127.0.0.1:{port}/{path}" + (f"?{query}" if query else "")
            try:
                upstream = await websockets.connect(url, max_size=TRANSPORT_MAX_BYTES, open_timeout=5, proxy=None,
                                                    ping_interval=None)
            except Exception as exc:
                adapter.ctx.log.warning("proxy: cannot connect to the game server: %s", exc)
                await ws.close(code=1011, reason="game server is not answering")
                return
            core.bus.publish("player.connected", game_id=game_id, player=session.public(), ip=ip, proxied=True)
            await self._pump(ws, upstream, session)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("proxy error (game %s, ip %s)", game_id, ip)
        finally:
            core.sessions.ip_release(ip)
            if upstream is not None:
                await upstream.close()
            if session is not None:
                session.cleaned = True
                session.close(1000, "bye")
                core.sessions.remove(session)
                await session.wait_closed(2.0)
                core.bus.publish("player.disconnected", game_id=game_id, player=session.public(), code=session.close_code)

    async def _proxy_login(self, ws: WebSocket, game_id: str, manifest: Any, ip: str) -> PlayerInfo | None:
        try:
            message = await asyncio.wait_for(ws.receive(), self.core.settings.gateway.hello_timeout_s)
            env = parse_frame(message.get("text") or "", HELLO_MAX_BYTES)
            if env.t != "hello":
                raise ProtocolError("bad_request", "first message must be 'hello'")
            hello = parse_hello(env.d)
            if hello.game != game_id or isinstance(hello.auth, AuthResume):
                raise ProtocolError("bad_request", "wrong game or login type")
            player, token = await self._authenticate(game_id, manifest, hello.auth, ip)
        except asyncio.TimeoutError:
            await self._send_and_close(ws, msg("auth_error", {"code": "timeout", "message": "hello not received"}),
                                       CLOSE_HELLO_TIMEOUT)
            return None
        except (ProtocolError, AuthError) as exc:
            await self._send_and_close(ws, msg("auth_error", {"code": exc.code, "message": exc.message}), CLOSE_UNAUTHORIZED)
            return None
        welcome: dict[str, Any] = {"player": {"id": player.player_id, "name": player.name}, "proxied": True}
        if token:
            welcome["player_token"] = token
        await ws.send_text(msg("welcome", welcome))
        return player

    async def _pump(self, ws: WebSocket, upstream: Any, s: Session) -> None:
        async def client_to_game() -> None:
            denied = 0
            while not s.closed:
                m = await ws.receive()
                if m["type"] == "websocket.disconnect":
                    return
                data = m.get("text") if m.get("text") is not None else m.get("bytes")
                if data is None:
                    continue
                size = len(data.encode("utf-8")) if isinstance(data, str) else len(data)
                if size > s.max_message_bytes:
                    await self.core.journal.record("message_too_large", game_id=s.game_id, ip=s.ip)
                    s.close(CLOSE_TOO_LARGE, "message too large")
                    return
                if not s.bucket.allow():
                    denied += 1
                    if denied > s.bucket.burst:
                        await self.core.journal.record("flood", game_id=s.game_id, ip=s.ip)
                        s.close(CLOSE_FLOOD, "too many messages")
                        return
                    continue  # over the rate: dropped
                denied = max(0, denied - 1)
                s.messages_in += 1
                s.last_activity = time.time()
                await upstream.send(data)

        async def game_to_client() -> None:
            async for data in upstream:
                if s.closed:
                    return
                if isinstance(data, bytes):
                    await ws.send_bytes(data)
                else:
                    await ws.send_text(data)
                s.messages_out += 1
            s.close(1000, "game server closed the connection")

        tasks = [asyncio.create_task(client_to_game()), asyncio.create_task(game_to_client())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def kick(self, game_id: str, player_id: str, reason: str) -> bool:
        s = self.core.sessions.get_player(game_id, player_id)
        if s is None:
            return False
        if not s.proxied:
            s.send(msg("kicked", {"reason": reason}))
        s.close(CLOSE_KICKED, reason)  # a detached session is cleaned up through on_dead
        return True
