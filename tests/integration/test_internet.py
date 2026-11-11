"""Stage 4: login modes, session resume, network modes, TLS, tunnel supervision, bans, secrets."""

import asyncio
import json
import shutil
import ssl
import subprocess
import sys
import textwrap

import httpx
import pytest
import websockets

from tests.conftest import Client, make_game_dir


async def net(h, **changes) -> httpx.Response:
    current = (await h.admin.get("/network")).json()["settings"]
    for k, v in changes.items():
        if isinstance(v, dict):
            current[k] = {**current[k], **v}
        else:
            current[k] = v
    return await h.admin.put("/network", json={"network": current})


# ---------------------------------------------------------------- login modes
async def test_password_mode(hgs):
    await hgs.add_and_start("pw", auth={"mode": "password"})
    c = await hgs.client("pw", {"type": "password", "name": "Ann", "password": "whatever"})
    assert c.welcome["d"]["code"] == "unauthorized" and "not set a password" in c.welcome["d"]["message"]
    assert (await hgs.admin.post("/games/pw/password", json={"password": "123"})).status_code == 422
    assert (await hgs.admin.post("/games/pw/password", json={"password": "friends-only"})).status_code == 200
    ok = await hgs.client("pw", {"type": "password", "name": "Ann", "password": "friends-only"})
    assert ok.welcome["t"] == "welcome" and ok.welcome["d"]["player_token"]
    bad = await hgs.client("pw", {"type": "password", "name": "Bob", "password": "nope"})
    assert bad.welcome["d"]["code"] == "unauthorized"
    # the same name logs in again as the same player
    again = await hgs.client("pw", {"type": "password", "name": "Ann", "password": "friends-only"})
    assert again.welcome["d"]["player"]["id"] == ok.welcome["d"]["player"]["id"]
    assert (await hgs.admin.get("/games/pw/auth")).json()["password_set"] is True


async def test_account_mode(hgs, tmp_path):
    await hgs.add_and_start("acc", auth={"mode": "account"})
    reg = await hgs.client("acc", {"type": "account", "username": "Ann", "password": "secret1", "register": True})
    assert reg.welcome["t"] == "welcome"
    dup = await hgs.client("acc", {"type": "account", "username": "Ann", "password": "other12", "register": True})
    assert dup.welcome["d"]["code"] == "name_taken"
    short = await hgs.client("acc", {"type": "account", "username": "Bob", "password": "123", "register": True})
    assert short.welcome["d"]["code"] == "bad_request"
    login = await hgs.client("acc", {"type": "account", "username": "Ann", "password": "secret1"})
    assert login.welcome["d"]["player"]["id"] == reg.welcome["d"]["player"]["id"]
    wrong = await hgs.client("acc", {"type": "account", "username": "Ann", "password": "wrong!!"})
    unknown = await hgs.client("acc", {"type": "account", "username": "Ghost", "password": "wrong!!"})
    assert wrong.welcome["d"]["message"] == unknown.welcome["d"]["message"]  # does not reveal which names exist
    # registration can be disabled
    await hgs.add_and_start("closed", auth={"mode": "account", "allow_register": False})
    no = await hgs.client("closed", {"type": "account", "username": "Cid", "password": "secret1", "register": True})
    assert no.welcome["d"]["code"] == "forbidden"


# ---------------------------------------------------------------- resume
async def resume(h, game: str, welcome: dict) -> Client:
    return await h.client(game, {"type": "resume", "session_id": welcome["d"]["session_id"],
                                 "resume_token": welcome["d"]["resume_token"]})


async def test_resume_after_dropped_connection(hgs):
    await hgs.add_and_start("arena")
    a = await hgs.client("arena", "Ann")
    b = await hgs.client("arena", "Bob")
    await a.join()
    await b.join()
    await a.recv_type("room.player_joined")
    first = a.welcome
    await a.ws.close(code=4000)  # abnormal close: the session waits for a resume
    await asyncio.sleep(0.2)
    await b.send("game", {"e": "while_away", "d": 1})
    a2 = await resume(hgs, "arena", first)
    assert a2.welcome["t"] == "welcome" and a2.welcome["d"]["resumed"] is True
    assert a2.welcome["d"]["player"]["id"] == first["d"]["player"]["id"]
    assert a2.welcome["d"]["room_id"] == "lobby"
    joined = await a2.recv_type("room.joined")
    assert {p["name"] for p in joined["d"]["players"]} == {"Ann", "Bob"}
    await b.expect_nothing()  # Bob never saw Ann leave
    await b.send("game", {"e": "hi", "d": 2})
    assert (await a2.recv_type("event"))["d"]["e"] == "hi"
    # the old resume token was single-use
    await a2.ws.close(code=4000)
    await asyncio.sleep(0.2)
    replay = await resume(hgs, "arena", first)
    assert replay.welcome["d"]["code"] == "resume_failed"
    kinds = [r["kind"] for r in await hgs.core.db.fetchall("SELECT kind FROM security_events")]
    assert "resume_token_rejected" in kinds


async def test_detached_session_expires(hgs, monkeypatch):
    from hgs.core.network import gateway

    monkeypatch.setattr(gateway, "RESUME_WINDOW_S", 0.2)
    await hgs.add_and_start("arena")
    a = await hgs.client("arena", "Ann")
    b = await hgs.client("arena", "Bob")
    await a.join()
    await b.join()
    await a.ws.close(code=4000)
    await asyncio.sleep(0.4)
    hgs.core.gateway.expire_sessions()
    left = await b.recv_type("room.player_left")
    assert left["d"]["reason"] == "timeout"
    late = await resume(hgs, "arena", a.welcome)
    assert late.welcome["d"]["code"] == "resume_failed"


async def test_intentional_close_leaves_immediately(hgs):
    await hgs.add_and_start("arena")
    a = await hgs.client("arena", "Ann")
    b = await hgs.client("arena", "Bob")
    await a.join()
    await b.join()
    await a.close()  # code 1000
    assert (await b.recv_type("room.player_left"))["d"]["reason"] == "left"


# ---------------------------------------------------------------- network modes
async def test_mode_switch_rebinds_gateway_and_open_login_is_blocked_online(hgs):
    await hgs.add_and_start("arena")
    a = await hgs.client("arena", "Ann")
    r = await net(hgs, mode="lan")
    assert r.status_code == 200, r.text
    assert r.json()["bind"] == "0.0.0.0"
    assert await a.closed_code() in (1001, 1012, 1006)  # players were disconnected by the rebind
    hgs.ws_url = f"ws://127.0.0.1:{hgs.core.gateway_port}/hgs/v1/ws"
    assert (await hgs.client("arena", "Bob")).welcome["t"] == "welcome"

    r = await net(hgs, mode="tunnel")
    assert r.json()["bind"] == "127.0.0.1"
    hgs.ws_url = f"ws://127.0.0.1:{hgs.core.gateway_port}/hgs/v1/ws"
    c = await hgs.client("arena", "Cid")
    assert c.welcome["d"]["code"] == "forbidden"  # open login is off while reachable from the internet
    st = (await hgs.admin.get("/status")).json()
    assert st["network"]["mode"] == "tunnel" and st["network"]["player_url"] is None


async def test_cf_connecting_ip_trusted_only_in_tunnel_mode(hgs):
    await hgs.add_and_start("pages", auth={"mode": "invite"})
    code1 = (await hgs.admin.post("/invites", json={"game_id": "pages", "max_uses": 5})).json()["code"]

    async def connect(name):
        ws = await websockets.connect(hgs.ws_url, additional_headers={"CF-Connecting-IP": "203.0.113.7"})
        c = Client(ws)
        await c.send("hello", {"game": "pages", "auth": {"type": "invite", "code": code1, "name": name}})
        c.welcome = await c.recv()
        return c

    await connect("Ann")
    ips = {p["name"]: p["ip"] for p in (await hgs.admin.get("/players")).json()}
    assert ips["Ann"] == "127.0.0.1"  # header ignored outside tunnel mode
    await net(hgs, mode="tunnel")
    hgs.ws_url = f"ws://127.0.0.1:{hgs.core.gateway_port}/hgs/v1/ws"
    await connect("Bob")
    ips = {p["name"]: p["ip"] for p in (await hgs.admin.get("/players")).json()}
    assert ips["Bob"] == "203.0.113.7"


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI not available")
async def test_direct_mode_requires_tls_and_serves_wss(hgs, tmp_path):
    r = await net(hgs, mode="direct")
    assert r.status_code == 400 and "certificate" in r.json()["message"]
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    r = await net(hgs, mode="direct", direct={"public_host": "play.example.org", "tls_cert": str(cert), "tls_key": str(key)})
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["tls"] is True and view["player_url"] == f"wss://play.example.org:{hgs.core.gateway_port}"
    await hgs.add_and_start("secure", auth={"mode": "invite"})
    code = (await hgs.admin.post("/invites", json={"game_id": "secure"})).json()["code"]
    ctx = ssl.create_default_context(cafile=str(cert))
    async with websockets.connect(f"wss://127.0.0.1:{hgs.core.gateway_port}/hgs/v1/ws", ssl=ctx) as ws:
        c = Client(ws)
        await c.send("hello", {"game": "secure", "auth": {"type": "invite", "code": code, "name": "Ann"}})
        assert (await c.recv())["t"] == "welcome"
    # plain ws:// is not accepted any more
    with pytest.raises(Exception):
        async with websockets.connect(f"ws://127.0.0.1:{hgs.core.gateway_port}/hgs/v1/ws", open_timeout=3):
            pass


# ---------------------------------------------------------------- tunnel supervision (fake cloudflared)
FAKE_CLOUDFLARED = '''
import sys, time, os
mode = os.environ.get("FAKE_MODE", "ok")
print("INF Requesting new quick Tunnel on trycloudflare.com...", flush=True)
if mode == "crash":
    time.sleep(0.2)
    print("ERR failed to connect", flush=True)
    sys.exit(1)
print("INF |  https://brave-test-words.trycloudflare.com  |", flush=True)
print("INF args=" + " ".join(sys.argv[1:]), flush=True)
time.sleep(60)
'''


@pytest.fixture
def fake_cloudflared(tmp_path, monkeypatch):
    from hgs.core.network import manager
    from hgs.core.network.tunnels import cloudflare

    script = tmp_path / "fake_cloudflared.py"
    script.write_text(textwrap.dedent(FAKE_CLOUDFLARED))
    monkeypatch.setattr(manager.NetworkManager, "tunnel_command", lambda self: [sys.executable, str(script)])
    monkeypatch.setattr(cloudflare, "BACKOFF_S", (0.05, 0.05, 0.05))
    return script


async def test_quick_tunnel_url_invite_link_and_reachability(hgs, fake_cloudflared, monkeypatch):
    r = await hgs.admin.post("/network/tunnel/start")
    assert r.status_code == 400  # not in tunnel mode yet
    await net(hgs, mode="tunnel")
    await hgs.add_and_start("pages", auth={"mode": "invite"}, client={"url": "https://me.github.io/pages/"})
    await hgs.admin.post("/network/tunnel/start")
    for _ in range(100):
        view = (await hgs.admin.get("/network")).json()
        if view["tunnel"]["state"] == "connected":
            break
        await asyncio.sleep(0.05)
    assert view["tunnel"]["url"] == "https://brave-test-words.trycloudflare.com"
    assert view["player_url"] == "wss://brave-test-words.trycloudflare.com"
    assert any("--url" in line and f"127.0.0.1:{hgs.core.gateway_port}" in line for line in view["tunnel"]["log_tail"])
    inv = (await hgs.admin.post("/invites", json={"game_id": "pages"})).json()
    assert inv["link"].startswith("https://me.github.io/pages/?hgs=wss%3A%2F%2Fbrave-test-words.trycloudflare.com&invite=")
    # reachability: point the "public" URL at our own gateway -> the instance nonce must match
    local = f"ws://127.0.0.1:{hgs.core.gateway_port}"
    monkeypatch.setattr(type(hgs.core.network), "player_url", lambda self: local)
    check = (await hgs.admin.post("/network/check")).json()
    assert check["ok"] is True, check
    local = "ws://127.0.0.1:9"  # nothing listens there
    check = (await hgs.admin.post("/network/check")).json()
    assert check["ok"] is False and "не вдалося" in check["detail"]
    await hgs.admin.post("/network/tunnel/stop")
    assert (await hgs.admin.get("/network")).json()["tunnel"]["state"] == "stopped"


async def test_crashing_tunnel_is_restarted_then_marked_failed(hgs, fake_cloudflared, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "crash")
    await net(hgs, mode="tunnel")
    events = hgs.core.bus.subscribe("network.changed")
    await hgs.admin.post("/network/tunnel/start")
    states = []
    while True:
        ev = await asyncio.wait_for(events.get(), 10)
        st = (ev.data.get("tunnel") or {}).get("state")
        if st and (not states or states[-1] != st):
            states.append(st)
        if st == "failed":
            break
    assert "reconnecting" in states
    view = (await hgs.admin.get("/network")).json()["tunnel"]
    assert "failed to connect" in view["error"]


async def test_named_tunnel_needs_token(hgs, fake_cloudflared):
    await net(hgs, mode="tunnel", tunnel={"provider": "cloudflare_named", "named_public_url": "https://play.example.com"})
    await hgs.admin.post("/network/tunnel/start")
    assert (await hgs.admin.get("/network")).json()["tunnel"]["state"] == "failed"
    r = await hgs.admin.put("/secrets/cloudflare_tunnel_token", json={"value": "eyJhIjoiZmFrZSJ9"})
    assert r.status_code == 200
    view = (await hgs.admin.get("/network")).json()
    assert "cloudflare_tunnel_token" in view["secrets_set"]
    assert "eyJhIjoiZmFrZSJ9" not in json.dumps(view)  # secrets are never returned
    await hgs.admin.post("/network/tunnel/start")
    for _ in range(100):
        view = (await hgs.admin.get("/network")).json()
        if "args=tunnel --no-autoupdate run" in " ".join(view["tunnel"]["log_tail"]):
            break
        await asyncio.sleep(0.05)
    assert "eyJhIjoiZmFrZSJ9" not in " ".join(view["tunnel"]["log_tail"])  # token not on the command line
    assert view["player_url"] is None or view["player_url"] == "wss://play.example.com"
    assert (await hgs.admin.put("/secrets/unknown", json={"value": "x"})).status_code == 400


# ---------------------------------------------------------------- moderation
async def test_ban_revokes_tokens_and_kicks(hgs):
    await hgs.add_and_start("mod", auth={"mode": "invite"})
    code = (await hgs.admin.post("/invites", json={"game_id": "mod"})).json()["code"]
    a = await hgs.client("mod", {"type": "invite", "code": code, "name": "Ann"})
    token, pid = a.welcome["d"]["player_token"], a.welcome["d"]["player"]["id"]
    r = await hgs.admin.post(f"/players/mod/{pid}/ban", json={"reason": "cheating"})
    assert r.status_code == 200
    assert (await a.recv_type("kicked"))["d"]["reason"] == "cheating"
    again = await hgs.client("mod", {"type": "token", "token": token})
    assert again.welcome["t"] == "auth_error"
    known = (await hgs.admin.get("/players/known", params={"game_id": "mod"})).json()
    assert known[0]["banned"] == 1 and known[0]["ban_reason"] == "cheating"
    assert (await hgs.admin.post(f"/players/mod/{pid}/unban")).status_code == 200
