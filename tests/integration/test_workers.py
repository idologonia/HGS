"""Game logic in worker processes: lifecycle, isolation, saves, crashes, hangs, launch safety."""

import asyncio
import json
import os
import textwrap
from pathlib import Path

import psutil
import pytest
import websockets

from tests.conftest import make_game_dir

LOGIC = '''
import os, time
from hgs_sdk import GameLogic

class Game(GameLogic):
    tick_rate = 20

    def __init__(self):
        super().__init__()
        self.counter = 0

    async def on_load(self, data):
        self.counter = data["counter"]

    async def on_save(self):
        return {"counter": self.counter}

    async def on_start(self):
        self.set_status(env_has_secret="HGS_TEST_SECRET" in os.environ, pid=os.getpid())

    async def on_join(self, room, player):
        if player.name == "Nope":
            return "you are not welcome"
        self.counter += 1
        room.state = room.state or {"names": []}
        room.state["names"].append(player.name)

    async def on_event(self, room, player, event, data):
        if event == "die":
            os._exit(7)
        if event == "freeze":
            time.sleep(60)  # blocks the worker completely
        if event == "boom":
            raise ValueError("bug in game")
        if event == "echo":
            room.send(player, "echo", {"data": data, "counter": self.counter})
        if event == "kick_me":
            room.kick(player, "game said so")

    async def on_tick(self, room, dt):
        room.broadcast("state", room.state)
'''


def logic_game(tmp: Path, game_id: str, code: str = LOGIC, **overrides) -> Path:
    d = make_game_dir(tmp, game_id, backend={"mode": "logic", "module": "server/logic.py", "class": "Game"}, **overrides)
    (d / "server").mkdir(exist_ok=True)
    (d / "server" / "logic.py").write_text(textwrap.dedent(code), encoding="utf-8")
    return d


async def add_start(h, path: Path, game_id: str, expect: str = "running") -> dict:
    r = await h.admin.post("/games", json={"path": str(path)})
    assert r.status_code == 201, r.text
    view = (await h.admin.post(f"/games/{game_id}/start")).json()
    assert view["state"] == expect, view
    return view


async def test_logic_game_state_and_room_isolation(hgs, tmp_path):
    await add_start(hgs, logic_game(tmp_path, "logic"), "logic")
    a = await hgs.client("logic", "Ann")
    b = await hgs.client("logic", "Bob")
    c = await hgs.client("logic", "Cid")
    await a.join()
    await b.join()
    room2 = (await c.request("room.create", {"name": "Other"}))["d"]["room"]["id"]
    await c.join(room2)
    while True:
        st = await b.recv_type("event")
        if st["d"]["e"] == "state" and len(st["d"]["d"]["names"]) == 2:
            break
    assert sorted(st["d"]["d"]["names"]) == ["Ann", "Bob"]
    st_c = await c.recv_type("event")
    assert st_c["d"]["d"]["names"] == ["Cid"]  # room 2 sees only its own state
    # reply to one player only
    await a.send("game", {"e": "echo", "d": 5})
    while (m := await a.recv_type("event"))["d"]["e"] != "echo":
        pass
    assert m["d"]["d"]["data"] == 5 and m["d"]["from"] == "server"
    # refusal from on_join
    n = await hgs.client("logic", "Nope")
    r = await n.request("room.join", {"room_id": "lobby"})
    assert r["ok"] is False and "not welcome" in r["error"]["message"]
    # game asks HGS to kick a player
    await b.send("game", {"e": "kick_me"})
    assert (await b.recv_type("kicked"))["d"]["reason"] == "game said so"
    # an exception in a handler is logged, the worker keeps running
    await a.send("game", {"e": "boom"})
    await a.send("game", {"e": "echo", "d": 6})
    while (m := await a.recv_type("event"))["d"]["e"] != "echo":
        pass
    logs = (await hgs.admin.get("/logs/game:logic")).json()
    assert any("ValueError: bug in game" in l["msg"] for l in logs)


async def test_saves_survive_restart(hgs, tmp_path):
    await add_start(hgs, logic_game(tmp_path, "saver"), "saver")
    for name in ("Ann", "Bob"):
        c = await hgs.client("saver", name)
        await c.join()
        await c.close()
    assert (await hgs.admin.post("/games/saver/stop", json={"grace_s": 0})).json()["state"] == "stopped"
    saves = list((tmp_path / "data" / "games" / "saver" / "saves").glob("*_stop.json"))
    assert len(saves) == 1 and json.loads(saves[0].read_text()) == {"counter": 2}
    await hgs.admin.post("/games/saver/start")
    c = await hgs.client("saver", "Cid")
    await c.join()
    await c.send("game", {"e": "echo"})
    while (m := await c.recv_type("event"))["d"]["e"] != "echo":
        pass
    assert m["d"]["d"]["counter"] == 3  # 2 restored + 1
    rows = await hgs.core.db.fetchall("SELECT reason, ok FROM saves WHERE game_id='saver'")
    assert rows and rows[0]["ok"] == 1


async def test_worker_crash_restarts_and_isolation(core_factory, tmp_path, monkeypatch):
    from hgs.core import server_manager

    monkeypatch.setattr(server_manager, "BACKOFF_S", (0.1, 0.1, 0.1))
    h = await core_factory()
    await add_start(h, logic_game(tmp_path, "crashy", restart={"max_restarts": 1}), "crashy")
    await add_start(h, logic_game(tmp_path, "steady"), "steady")
    steady = await h.client("steady", "Zed")
    await steady.join()
    states = h.core.bus.subscribe("server.state_changed")
    for attempt, expected in ((1, "crashed"), (2, "failed")):
        a = await h.client("crashy", f"P{attempt}")
        assert a.welcome["t"] == "welcome", a.welcome
        await a.join()
        await a.send("game", {"e": "die"})
        assert await a.closed_code() == 4503
        while True:
            ev = await asyncio.wait_for(states.get(), 10)
            if ev.data["game_id"] == "crashy" and ev.data["state"] in ("crashed", "failed"):
                assert ev.data["state"] == expected
                assert "code 7" in ev.data["error"]
                break
        if expected == "crashed":
            while True:
                ev = await asyncio.wait_for(states.get(), 20)
                if ev.data["game_id"] == "crashy" and ev.data["state"] == "running":
                    break
    # the other game's worker and player are untouched
    await steady.send("game", {"e": "echo", "d": 1})
    while (m := await steady.recv_type("event"))["d"]["e"] != "echo":
        pass
    assert (await h.admin.get("/status")).status_code == 200


async def test_frozen_worker_is_killed_by_heartbeat(core_factory, tmp_path, monkeypatch):
    from hgs.adapters import worker_backend
    from hgs.core import server_manager

    monkeypatch.setattr(worker_backend, "HEARTBEAT_S", 0.3)
    monkeypatch.setattr(server_manager, "BACKOFF_S", (0.1, 0.1, 0.1))
    h = await core_factory()
    await add_start(h, logic_game(tmp_path, "frozen"), "frozen")
    pid = (await h.admin.get("/games/frozen")).json()["metrics"]["pid"]
    a = await h.client("frozen", "Ann")
    await a.join()
    states = h.core.bus.subscribe("server.state_changed")
    await a.send("game", {"e": "freeze"})
    while True:
        ev = await asyncio.wait_for(states.get(), 15)
        if ev.data["game_id"] == "frozen" and ev.data["state"] == "crashed":
            break
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


async def test_worker_stops_with_server_and_secrets_do_not_leak(core_factory, tmp_path, monkeypatch):
    monkeypatch.setenv("HGS_TEST_SECRET", "x")
    h = await core_factory()
    await add_start(h, logic_game(tmp_path, "clean"), "clean")
    m = (await h.admin.get("/games/clean")).json()["metrics"]
    assert m["game"]["env_has_secret"] is False
    pid = m["pid"]
    assert psutil.pid_exists(pid)
    await h.admin.post("/games/clean/stop", json={"grace_s": 0})
    await asyncio.sleep(0.2)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


async def test_worker_exits_when_core_goes_away(core_factory, tmp_path):
    h = await core_factory()
    await add_start(h, logic_game(tmp_path, "orphan"), "orphan")
    pid = (await h.admin.get("/games/orphan")).json()["metrics"]["pid"]
    # simulate an abrupt loss of the core: drop the worker's connection without a shutdown message
    link = h.core.servers.games["orphan"].adapter.worker.link
    h.core.servers.games["orphan"].adapter.worker.stopping = True
    await link.ws.close(1011)
    proc = psutil.Process(pid)
    await asyncio.get_running_loop().run_in_executor(None, lambda: proc.wait(10))
    assert not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE


@pytest.mark.parametrize(
    "code,module,expect",
    [
        ("def broken(:\n  pass", "server/logic.py", "SyntaxError"),
        ("class Game: pass", "server/logic.py", "subclass of hgs_sdk.GameLogic"),
        ("from hgs_sdk import GameLogic\nclass Game(GameLogic):\n  async def on_start(self): raise RuntimeError('no db')",
         "server/logic.py", "failed"),
    ],
)
async def test_broken_logic_reports_error(hgs, tmp_path, code, module, expect):
    gid = "broken-" + str(abs(hash(code)) % 10000)
    view = await add_start(hgs, logic_game(tmp_path, gid, code=code), gid, expect="error")
    assert expect in view["last_error"], view["last_error"]


async def test_entry_outside_game_folder_is_rejected(hgs, tmp_path):
    d = make_game_dir(tmp_path, "escape", backend={"mode": "logic", "module": "../evil.py"})
    r = await hgs.admin.post("/games", json={"path": str(d)})
    assert r.status_code == 400 and "inside the game folder" in r.json()["message"]
    # a symlink pointing outside is caught at start time
    outside = tmp_path / "outside.py"
    outside.write_text("print('pwned')")
    d2 = logic_game(tmp_path, "symlinked")
    (d2 / "server" / "logic.py").unlink()
    try:
        (d2 / "server" / "logic.py").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not available")
    r = await hgs.admin.post("/games", json={"path": str(d2)})
    view = (await hgs.admin.post("/games/symlinked/start")).json()
    assert view["state"] == "error" and "inside the game folder" in view["last_error"]


async def test_worker_bus_rejects_unknown_tokens(hgs):
    url = hgs.core.worker_bus.url()
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"v": 1, "t": "w.hello", "d": {"token": "guess", "game_id": "x", "api": 1}}))
        with pytest.raises(websockets.ConnectionClosed) as e:
            await ws.recv()
        assert e.value.rcvd.code == 4401


async def test_sdk_is_served_by_gateway(hgs):
    import httpx

    async with httpx.AsyncClient() as c:
        r = await c.get(f"http://127.0.0.1:{hgs.core.gateway_port}/hgs/sdk/hgs.js")
    assert r.status_code == 200 and "HOME GAME SERVER" in r.text
    assert r.headers["content-type"].startswith("text/javascript")
