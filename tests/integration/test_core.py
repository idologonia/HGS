"""Integration tests: real core, real HTTP and WebSocket clients."""

import asyncio
import json

import httpx
import pytest
import websockets

from hgs.adapters.base import AdapterMetrics, AdapterState, AdapterStatus, JoinDecision
from hgs.adapters.html.adapter import HtmlAdapter
from hgs.adapters.registry import builtin_registry
from tests.conftest import make_game_dir, make_settings


# ---------------------------------------------------------------- admin API security
async def test_admin_requires_token_host_and_no_origin(hgs):
    base = f"http://127.0.0.1:{hgs.core.admin_port}/admin/v1/status"
    async with httpx.AsyncClient() as c:
        assert (await c.get(base)).status_code == 401
        assert (await c.get(base, headers={"Authorization": "Bearer wrong"})).status_code == 401
        good = {"Authorization": f"Bearer {hgs.core.admin_token}"}
        assert (await c.get(base, headers=good)).status_code == 200
        assert (await c.get(base, headers={**good, "Host": "evil.example:80"})).status_code == 403
        assert (await c.get(base, headers={**good, "Origin": "https://evil.example"})).status_code == 403


async def test_admin_api_not_served_on_gateway(hgs):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"http://127.0.0.1:{hgs.core.gateway_port}/admin/v1/status",
                        headers={"Authorization": f"Bearer {hgs.core.admin_token}"})
        assert r.status_code == 404
        health = await c.get(f"http://127.0.0.1:{hgs.core.gateway_port}/hgs/v1/health")
        assert health.json()["ok"] is True


# ---------------------------------------------------------------- lifecycle
async def test_add_start_stop_and_registry_persists(core_factory, tmp_path):
    h = await core_factory()
    await h.add_and_start("alpha")
    r = await h.admin.post("/games", json={"path": str(make_game_dir(tmp_path, "alpha"))})
    assert r.status_code == 409  # already registered
    r = await h.admin.post("/games", json={"path": str(tmp_path / "missing")})
    assert r.status_code == 400
    st = (await h.admin.get("/status")).json()
    assert st["games_running"] == 1 and st["previous_unclean_shutdown"] is False
    r = await h.admin.post("/games/alpha/stop", json={"grace_s": 0})
    assert r.json()["state"] == "stopped"
    await h.core.stop()

    h2 = await core_factory(data_dir=tmp_path / "data")
    games = (await h2.admin.get("/games")).json()
    assert [g["id"] for g in games] == ["alpha"]
    assert games[0]["state"] == "stopped"  # never auto-started


async def test_invalid_manifest_reports_field(hgs, tmp_path):
    d = make_game_dir(tmp_path, "bad", backend={"mode": "teleport"})
    r = await hgs.admin.post("/games", json={"path": str(d)})
    assert r.status_code == 400 and "backend.mode" in r.json()["message"]


async def test_unclean_shutdown_detected(core_factory, tmp_path):
    h = await core_factory()
    (tmp_path / "data" / "run_state.json").write_text('{"clean": false}')
    await h.core.stop()
    (tmp_path / "data" / "run_state.json").write_text('{"clean": false}')  # simulate a crash
    h2 = await core_factory(data_dir=tmp_path / "data")
    assert (await h2.admin.get("/status")).json()["previous_unclean_shutdown"] is True


async def test_second_core_on_same_data_dir_is_refused(core_factory, tmp_path):
    from hgs.common.platform import InstanceLockError

    await core_factory()
    with pytest.raises(InstanceLockError):
        await core_factory(data_dir=tmp_path / "data")


# ---------------------------------------------------------------- players and rooms
async def test_two_clients_relay_in_same_room(hgs):
    await hgs.add_and_start("arena")
    a = await hgs.client("arena", "Ann")
    b = await hgs.client("arena", "Bob")
    assert a.welcome["t"] == "welcome"
    await a.join()
    joined = await b.join()
    assert {p["name"] for p in joined["d"]["players"]} == {"Ann", "Bob"}
    assert (await a.recv_type("room.player_joined"))["d"]["player"]["name"] == "Bob"
    await a.send("game", {"e": "move", "d": {"x": 3}})
    ev = await b.recv_type("event")
    assert ev["d"] == {"e": "move", "d": {"x": 3}, "from": a.welcome["d"]["player"]["id"]}
    await a.expect_nothing()  # sender does not get its own event back
    # direct message
    await b.send("game", {"e": "whisper", "d": "hi", "to": a.welcome["d"]["player"]["id"]})
    assert (await a.recv_type("event"))["d"]["e"] == "whisper"


async def test_rooms_are_isolated(hgs):
    await hgs.add_and_start("arena")
    a = await hgs.client("arena", "Ann")
    b = await hgs.client("arena", "Bob")
    r = await a.request("room.create", {"name": "Second", "max_players": 2})
    assert r["ok"]
    await a.join(r["d"]["room"]["id"])
    await b.join("lobby")
    await a.send("game", {"e": "secret", "d": 1})
    await b.expect_nothing()
    # not in a room -> cannot send game events
    c = await hgs.client("arena", "Cid")
    reply = await c.request("game", {"e": "x"})
    assert reply["ok"] is False and reply["error"]["code"] == "forbidden"


async def test_games_are_isolated(hgs):
    await hgs.add_and_start("game-a")
    await hgs.add_and_start("game-b")
    a = await hgs.client("game-a", "Ann")
    b = await hgs.client("game-b", "Bob")
    await a.join("lobby")
    await b.join("lobby")  # same room id, different game
    await a.send("game", {"e": "x", "d": 1})
    await b.expect_nothing()
    # stopping one game does not affect the other
    await hgs.admin.post("/games/game-a/stop", json={"grace_s": 0})
    assert await a.closed_code() == 4503
    await b.send("game", {"e": "ping", "d": 1})
    assert (await b.request("room.list"))["ok"]


async def test_room_full_and_room_not_found(hgs):
    await hgs.add_and_start("tiny", rooms={"max_players": 1})
    a = await hgs.client("tiny", "Ann")
    b = await hgs.client("tiny", "Bob")
    await a.join()
    r = await b.request("room.join", {"room_id": "lobby"})
    assert r["error"]["code"] == "room_full"
    r = await b.request("room.join", {"room_id": "nope"})
    assert r["error"]["code"] == "room_not_found"


async def test_room_state_host_only_and_sent_to_late_joiner(hgs):
    await hgs.add_and_start("arena")
    a = await hgs.client("arena", "Ann")
    b = await hgs.client("arena", "Bob")
    await a.join()
    await b.join()
    assert (await a.request("room.state.set", {"state": {"turn": 1}}))["ok"]
    assert (await b.recv_type("room.state"))["d"]["state"] == {"turn": 1}
    r = await b.request("room.state.patch", {"patch": {"turn": 2}})
    assert r["error"]["code"] == "forbidden"
    assert (await a.request("room.state.patch", {"patch": {"score": 5}}))["ok"]
    c = await hgs.client("arena", "Cid")
    joined = await c.join()
    assert joined["d"]["state"] == {"turn": 1, "score": 5}
    # host leaves -> host passes on
    await a.close()
    changed = await b.recv_type("room.host_changed")
    assert changed["d"]["host"] == b.welcome["d"]["player"]["id"]


async def test_stop_with_players_sends_shutdown_notice(hgs):
    await hgs.add_and_start("arena")
    a = await hgs.client("arena", "Ann")
    await a.join()
    stop = asyncio.create_task(hgs.admin.post("/games/arena/stop", json={"grace_s": 0.3}))
    notice = await a.recv_type("server_shutdown")
    assert notice["d"]["grace_s"] == 0.3
    assert await a.closed_code() == 4503
    assert (await stop).json()["state"] == "stopped"
    # new connections are refused while stopped
    c = await hgs.client("arena", "Bob")
    assert c.welcome["t"] == "auth_error" and c.welcome["d"]["code"] == "game_unavailable"


async def test_kick(hgs):
    await hgs.add_and_start("arena")
    a = await hgs.client("arena", "Ann")
    pid = a.welcome["d"]["player"]["id"]
    r = await hgs.admin.post(f"/players/arena/{pid}/kick", json={"reason": "test"})
    assert r.status_code == 200
    assert (await a.recv_type("kicked"))["d"]["reason"] == "test"
    assert await a.closed_code() == 4403


async def test_same_player_second_connection_replaces_first(hgs):
    await hgs.add_and_start("arena")
    a1 = await hgs.client("arena", "Ann")
    a2 = await hgs.client("arena", "Ann")
    assert await a1.closed_code() == 4409
    assert (await a2.request("room.list"))["ok"]


# ---------------------------------------------------------------- authentication
async def test_invite_flow_token_reuse_and_bruteforce_block(hgs):
    await hgs.add_and_start("secure", auth={"mode": "invite"})
    inv = (await hgs.admin.post("/invites", json={"game_id": "secure", "max_uses": 1})).json()
    a = await hgs.client("secure", {"type": "invite", "code": inv["code"].lower(), "name": "Ann"})
    assert a.welcome["t"] == "welcome"
    token = a.welcome["d"]["player_token"]
    await a.close()
    # token login works, no new token issued
    a2 = await hgs.client("secure", {"type": "token", "token": token})
    assert a2.welcome["t"] == "welcome" and "player_token" not in a2.welcome["d"]
    assert a2.welcome["d"]["player"]["name"] == "Ann"
    await a2.close()
    # a used invite cannot be reused
    b = await hgs.client("secure", {"type": "invite", "code": inv["code"], "name": "Bob"})
    assert b.welcome["t"] == "auth_error" and await b.closed_code() == 4401
    # open login is refused for an invite game
    o = await hgs.client("secure", "Eve")
    assert o.welcome["t"] == "auth_error"
    # brute force: after 5 failures the IP is blocked even for a valid token
    for _ in range(4):
        x = await hgs.client("secure", {"type": "invite", "code": "AAAA-BBBB-CC", "name": "Eve"})
        assert x.welcome["d"]["code"] == "unauthorized"
    blocked = await hgs.client("secure", {"type": "token", "token": token})
    assert blocked.welcome["d"]["code"] == "rate_limited"
    events = await hgs.core.db.fetchall("SELECT kind FROM security_events")
    assert any(e["kind"] == "ip_blocked" for e in events)
    # the token itself is never stored in plain text
    rows = await hgs.core.db.fetchall("SELECT token_hash FROM player_tokens")
    assert all(token.encode() not in r["token_hash"] for r in rows)


async def test_origin_allowlist(hgs):
    await hgs.add_and_start("pages", client={"allowed_origins": ["https://friend.github.io"]})
    ok = await hgs.client("pages", "Ann", origin="https://friend.github.io")
    assert ok.welcome["t"] == "welcome"
    bad = await hgs.client("pages", "Bob", origin="https://evil.example")
    assert bad.welcome["t"] == "auth_error" and bad.welcome["d"]["code"] == "forbidden"


async def test_hello_timeout(core_factory):
    h = await core_factory(make_settings(hello_timeout_s=0.3))
    ws = await websockets.connect(h.ws_url)
    with pytest.raises(websockets.ConnectionClosed) as e:
        while True:
            await asyncio.wait_for(ws.recv(), 3)
    assert e.value.rcvd.code == 4408


# ---------------------------------------------------------------- abuse protection
async def test_oversized_message_closes_connection(hgs):
    await hgs.add_and_start("arena", limits={"max_message_bytes": 1024})
    a = await hgs.client("arena", "Ann")
    await a.join()
    await a.send("game", {"e": "big", "d": "x" * 5000})
    assert await a.closed_code() == 4413


async def test_replayed_seq_and_malformed_messages(hgs):
    await hgs.add_and_start("arena")
    a = await hgs.client("arena", "Ann")
    await a.send("ping", seq=1)  # seq 1 was used by hello
    err = await a.recv_type("error")
    assert err["d"]["code"] == "invalid_seq"
    for _ in range(12):
        await a.ws.send("{broken")
    assert await a.closed_code() == 4400


async def test_flood_is_limited(hgs):
    await hgs.add_and_start("arena", limits={"messages_per_s": 5, "burst": 5})
    a = await hgs.client("arena", "Ann")
    for _ in range(40):
        await a.send("ping")
    assert await a.closed_code() == 4429


async def test_too_many_connections_per_ip(core_factory):
    h = await core_factory(make_settings(max_connections_per_ip=2))
    await h.add_and_start("arena")
    a = await h.client("arena", "Ann")
    b = await h.client("arena", "Bob")
    with pytest.raises(websockets.InvalidStatus):
        await websockets.connect(h.ws_url)
    await a.close()
    await asyncio.sleep(0.2)
    c = await h.client("arena", "Cid")
    assert c.welcome["t"] == "welcome"


# ---------------------------------------------------------------- fault isolation
class BrokenHandlerAdapter(HtmlAdapter):
    adapter_type = "broken"

    @classmethod
    def validate_config(cls, manifest):
        from hgs.adapters.base import ValidationResult
        return ValidationResult.success()

    async def handle_message(self, player, room, event, data, to):
        if event == "boom":
            raise ZeroDivisionError("game bug")
        if event == "hang":
            await asyncio.sleep(60)
        if event == "crash":
            self.ctx.report_crash("process exited with code 1")
            return
        await super().handle_message(player, room, event, data, to)


class FailingStartAdapter(BrokenHandlerAdapter):
    adapter_type = "nostart"

    async def start(self):
        raise RuntimeError("cannot open port")


def _registry():
    reg = builtin_registry()
    reg.register(BrokenHandlerAdapter)
    reg.register(FailingStartAdapter)
    return reg


async def test_adapter_errors_do_not_break_core_or_connection(core_factory, monkeypatch):
    from hgs.core.network import gateway

    monkeypatch.setattr(gateway, "ADAPTER_CALL_TIMEOUT_S", 0.3)
    h = await core_factory(registry=_registry())
    await h.add_and_start("buggy", adapter="broken")
    await h.add_and_start("healthy")
    a = await h.client("buggy", "Ann")
    b = await h.client("buggy", "Bob")
    await a.join()
    await b.join()
    await a.send("game", {"e": "boom"})
    await a.send("game", {"e": "hang"})
    await a.send("game", {"e": "fine", "d": 1})
    assert (await b.recv_type("event", timeout=5))["d"]["e"] == "fine"
    logs = (await h.admin.get("/logs/game:buggy")).json()
    assert any("ZeroDivisionError" in l["msg"] for l in logs)
    h2 = await h.client("healthy", "Cid")
    assert h2.welcome["t"] == "welcome"


async def test_start_failure_sets_error_state(core_factory):
    h = await core_factory(registry=_registry())
    r = await h.admin.post("/games", json={"path": str(make_game_dir(h.tmp, "nostart", adapter="nostart"))})
    assert r.status_code == 201
    view = (await h.admin.post("/games/nostart/start")).json()
    assert view["state"] == "error" and "cannot open port" in view["last_error"]


async def test_crash_restart_policy_then_failed(core_factory, monkeypatch):
    from hgs.core import server_manager

    monkeypatch.setattr(server_manager, "BACKOFF_S", (0.05, 0.05, 0.05))
    h = await core_factory(registry=_registry())
    await h.add_and_start("crashy", adapter="broken", restart={"max_restarts": 2})
    await h.add_and_start("healthy")
    healthy = await h.client("healthy", "Zed")
    await healthy.join()
    states = h.core.bus.subscribe("server.state_changed")
    for i in range(3):
        a = await h.client("crashy", f"P{i}")
        assert a.welcome["t"] == "welcome", a.welcome
        await a.join()
        await a.send("game", {"e": "crash"})
        assert await a.closed_code() == 4503
        expected = "crashed" if i < 2 else "failed"
        while True:
            ev = await asyncio.wait_for(states.get(), 3)
            if ev.data["game_id"] == "crashy" and ev.data["state"] in ("crashed", "failed"):
                assert ev.data["state"] == expected
                break
        if expected == "crashed":
            while True:  # wait for automatic restart
                ev = await asyncio.wait_for(states.get(), 3)
                if ev.data["game_id"] == "crashy" and ev.data["state"] == "running":
                    break
    view = (await h.admin.get("/games/crashy")).json()
    assert view["state"] == "failed" and view["restarts"] == 2
    # the other game and its player were untouched
    assert (await healthy.request("room.list"))["ok"]
    # manual Start resets the failure counter
    assert (await h.admin.post("/games/crashy/start")).json()["state"] == "running"


async def test_event_stream_for_gui(hgs):
    url = f"ws://127.0.0.1:{hgs.core.admin_port}/admin/v1/events"
    with pytest.raises(websockets.InvalidStatus):
        async with websockets.connect(url):
            pass
    async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {hgs.core.admin_token}"}) as ws:
        hello = json.loads(await ws.recv())
        assert hello["type"] == "core.hello"
        await hgs.add_and_start("arena")
        seen = set()
        while "server.state_changed" not in seen or "game.added" not in seen:
            seen.add(json.loads(await asyncio.wait_for(ws.recv(), 3))["type"])
