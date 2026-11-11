"""Stage 5: Python adapter (worker / standalone), process adapter, proxy, allowlist."""

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import psutil
import pytest
import websockets

from tests.conftest import make_game_dir

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def copy_example(tmp: Path, name: str) -> Path:
    dest = tmp / name
    shutil.copytree(EXAMPLES / name, dest)
    return dest


async def add_start(h, path: Path, game_id: str, expect: str = "running", manifest: dict | None = None) -> dict:
    body = {"path": str(path)}
    if manifest is not None:
        body["manifest"] = manifest
    r = await h.admin.post("/games", json=body)
    assert r.status_code == 201, r.text
    view = (await h.admin.post(f"/games/{game_id}/start")).json()
    assert view["state"] == expect, view
    return view


# ---------------------------------------------------------------- worker mode
async def test_python_worker_in_games_own_venv_without_extra_packages(hgs, tmp_path):
    game = copy_example(tmp_path, "python-worker")
    # a bare virtualenv: no pip, no websockets — hgs_sdk must still work (standard library only)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(game / ".venv")], check=True)
    (game / "server" / "logic.py").write_text((game / "server" / "logic.py").read_text() + '''
    async def on_start(self):
        import sys
        self.set_status(prefix=sys.prefix)

    async def on_room_created(self, room):
        pass
''')
    logic = (game / "server" / "logic.py").read_text().replace(
        '        if event == "click":', '        if event == "big":\n            room.send(player, "big", "x" * 300000)\n'
                                       '        if event == "click":')
    (game / "server" / "logic.py").write_text(logic)
    view = await add_start(hgs, game, "py-counter")
    assert ".venv" in view["metrics"]["game"]["prefix"]  # really runs in the game's own environment
    a = await hgs.client("py-counter", "Ann")
    await a.join()
    assert (await a.recv_type("event"))["d"]["d"]["count"] == 0
    for _ in range(3):
        await a.send("game", {"e": "click"})
    while (m := await a.recv_type("event"))["d"]["d"]["count"] < 3:
        pass
    await a.send("game", {"e": "big"})
    big = await a.recv_type("event")
    assert big["d"]["e"] == "big" and len(big["d"]["d"]) == 300000  # large frames through the stdlib client
    await hgs.admin.post("/games/py-counter/stop", json={"grace_s": 0})
    await hgs.admin.post("/games/py-counter/start")
    b = await hgs.client("py-counter", "Bob")
    await b.join()
    assert (await b.recv_type("event"))["d"]["d"]["best"] == 3  # restored from the snapshot


async def test_python_venv_required_but_missing(hgs, tmp_path):
    d = make_game_dir(tmp_path, "novenv", adapter="python",
                      backend={"mode": "worker", "entry": "server/logic.py:Game", "python": "venv"})
    (d / "server").mkdir()
    (d / "server" / "logic.py").write_text("from hgs_sdk import GameLogic\nclass Game(GameLogic): pass\n")
    view = await add_start(hgs, d, "novenv", expect="error")
    assert "no virtual environment" in view["last_error"]


@pytest.mark.parametrize("backend,needle", [
    ({"mode": "worker", "entry": "../x.py:Game"}, "inside the game folder"),
    ({"mode": "standalone", "entry": "server/main.py", "env": {"HGS_WORKER_TOKEN": "x"}}, "reserved"),
    ({"mode": "standalone", "entry": "server/main.py", "args": ["{secret}"]}, "placeholder"),
    ({"mode": "standalone", "entry": "server/main.py", "shell": "rm -rf /"}, "unknown backend fields"),
    ({"mode": "fork-bomb", "entry": "a.py"}, "backend.mode"),
])
async def test_python_backend_validation(hgs, tmp_path, backend, needle):
    d = make_game_dir(tmp_path, "badpy", adapter="python", backend=backend)
    r = await hgs.admin.post("/games", json={"path": str(d)})
    assert r.status_code == 400 and needle in r.json()["message"], r.text


# ---------------------------------------------------------------- standalone + proxy
async def test_standalone_server_through_proxy(hgs, tmp_path):
    game = copy_example(tmp_path, "python-standalone")
    manifest = json.loads((game / "hgs-game.json").read_text())
    manifest["limits"] = {"max_message_bytes": 1024}
    (game / "hgs-game.json").write_text(json.dumps(manifest))
    view = await add_start(hgs, game, "py-chat")
    assert view["proxy_path"] == "/hgs/v1/proxy/py-chat/"
    port = view["metrics"]["port"]
    url = f"ws://127.0.0.1:{hgs.core.gateway_port}/hgs/v1/proxy/py-chat/"
    a = await websockets.connect(url)
    b = await websockets.connect(url)
    await a.send("hello world")
    assert json.loads(await asyncio.wait_for(b.recv(), 3)) == {"chat": "hello world"}
    assert json.loads(await asyncio.wait_for(a.recv(), 3)) == {"chat": "hello world"}  # echo to the sender
    players = (await hgs.admin.get("/players", params={"game_id": "py-chat"})).json()
    assert len(players) == 2 and players[0]["name"] == "guest"
    assert (await hgs.admin.get("/games/py-chat")).json()["players_online"] == 2
    # HGS limits still apply to proxied traffic
    await a.send("x" * 5000)
    with pytest.raises(websockets.ConnectionClosed) as e:
        await asyncio.wait_for(a.recv(), 3)
    assert e.value.rcvd.code == 4413
    # admin kick works for proxied players
    for _ in range(50):  # wait until the closed connection is cleaned up
        players = (await hgs.admin.get("/players")).json()
        if len(players) == 1:
            break
        await asyncio.sleep(0.05)
    pid = players[0]["player_id"]
    await hgs.admin.post(f"/players/py-chat/{pid}/kick", json={"reason": "bye"})
    with pytest.raises(websockets.ConnectionClosed) as e:
        await asyncio.wait_for(b.recv(), 3)
    assert e.value.rcvd.code == 4403
    # the game port itself is only on localhost; crash of the server process -> crash policy
    proc = next(p for p in psutil.process_iter(["cmdline"]) if p.info["cmdline"] and str(port) in " ".join(p.info["cmdline"]))
    states = hgs.core.bus.subscribe("server.state_changed")
    proc.kill()
    while True:
        ev = await asyncio.wait_for(states.get(), 10)
        if ev.data["game_id"] == "py-chat" and ev.data["state"] == "crashed":
            assert "exited unexpectedly" in ev.data["error"]
            break


async def test_proxy_with_hgs_login(hgs, tmp_path):
    game = copy_example(tmp_path, "python-standalone")
    manifest = json.loads((game / "hgs-game.json").read_text())
    manifest["backend"]["proxy"] = {"require_login": True}
    (game / "hgs-game.json").write_text(json.dumps(manifest))
    await add_start(hgs, game, "py-chat")
    url = f"ws://127.0.0.1:{hgs.core.gateway_port}/hgs/v1/proxy/py-chat"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"v": 1, "t": "hello", "seq": 1, "d": {"game": "py-chat",
                                  "auth": {"type": "invite", "code": "AAAA-BBBB-CC", "name": "Eve"}}}))
        assert json.loads(await ws.recv())["t"] == "auth_error"
    code = (await hgs.admin.post("/invites", json={"game_id": "py-chat"})).json()["code"]
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"v": 1, "t": "hello", "seq": 1, "d": {"game": "py-chat",
                                  "auth": {"type": "invite", "code": code, "name": "Ann"}}}))
        welcome = json.loads(await ws.recv())
        assert welcome["t"] == "welcome" and welcome["d"]["proxied"] is True
        await ws.send("hi")
        assert json.loads(await asyncio.wait_for(ws.recv(), 3)) == {"chat": "hi"}
        players = (await hgs.admin.get("/players")).json()
        assert players[0]["name"] == "Ann"


async def test_stopping_standalone_closes_proxied_players(hgs, tmp_path):
    game = copy_example(tmp_path, "python-standalone")
    await add_start(hgs, game, "py-chat")
    ws = await websockets.connect(f"ws://127.0.0.1:{hgs.core.gateway_port}/hgs/v1/proxy/py-chat/")
    await hgs.admin.post("/games/py-chat/stop", json={"grace_s": 0})
    with pytest.raises(websockets.ConnectionClosed):
        await asyncio.wait_for(ws.recv(), 5)
    async with websockets.connect(f"ws://127.0.0.1:{hgs.core.gateway_port}/hgs/v1/proxy/py-chat/") as ws2:
        with pytest.raises(websockets.ConnectionClosed) as e:
            await asyncio.wait_for(ws2.recv(), 3)
        assert e.value.rcvd.code == 4503


# ---------------------------------------------------------------- process adapter (another language)
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
async def test_node_worker_needs_approval_then_works(hgs, tmp_path):
    game = copy_example(tmp_path, "node-worker")
    view = await add_start(hgs, game, "node-chat", expect="error")
    assert view["last_error"].startswith("program is not in the allowlist: ")
    node = view["last_error"].split(": ", 1)[1]
    assert (await hgs.admin.post("/allowlist", json={"path": node})).json() == [node]
    assert (await hgs.admin.post("/games/node-chat/start")).json()["state"] == "running"
    a = await hgs.client("node-chat", "Ann")
    b = await hgs.client("node-chat", "Bob")
    await a.join()
    await b.join()
    await a.send("game", {"e": "chat", "d": "привіт"})
    ev = await b.recv_type("event")
    assert ev["d"]["d"] == {"from": "Ann", "text": "привіт"}
    await hgs.admin.post("/games/node-chat/stop", json={"grace_s": 0})
    await hgs.admin.post("/allowlist/remove", json={"path": node})
    view = (await hgs.admin.post("/games/node-chat/start")).json()
    assert view["state"] == "error"  # approval was withdrawn


# ---------------------------------------------------------------- description kept in HGS
async def test_game_without_manifest_file(hgs, tmp_path):
    folder = tmp_path / "plain-folder"
    folder.mkdir()
    manifest = {"id": "plain", "name": "Plain", "adapter": "html", "backend": {"mode": "relay"}, "auth": {"mode": "open"}}
    await add_start(hgs, folder, "plain", manifest=manifest)
    assert not (folder / "hgs-game.json").exists()  # the game folder was not modified
    r = await hgs.admin.post("/games/plain/reload")
    assert r.status_code == 409  # running
    manifest["name"] = "Renamed"
    assert (await hgs.admin.put("/games/plain/manifest", json={"manifest": manifest})).status_code == 409
    await hgs.admin.post("/games/plain/stop", json={"grace_s": 0})
    r = await hgs.admin.put("/games/plain/manifest", json={"manifest": manifest})
    assert r.status_code == 200 and r.json()["name"] == "Renamed" and r.json()["manifest_source"] == "hgs"
    bad = dict(manifest, rooms={"max_players": 0})
    r = await hgs.admin.put("/games/plain/manifest", json={"manifest": bad})
    assert r.status_code == 400 and "rooms.max_players" in r.json()["message"]
    r = await hgs.admin.post("/games/plain/reload")
    assert r.status_code == 400 and "kept in HGS" in r.json()["message"]
