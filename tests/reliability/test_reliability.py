"""Stage 7: reliability — hard crashes, malformed input, leaks, corrupted files, busy ports."""

from __future__ import annotations

import asyncio
import gc
import json
import os
import random
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil
import pytest
import websockets

from hgs.core.app import CoreApp, CoreStartError
from hgs.core.config.settings import SettingsError
from tests.conftest import make_game_dir, make_settings
from tests.integration.test_workers import logic_game


# ---------------------------------------------------------------- the core process is killed
def _start_core_process(data: Path) -> tuple[subprocess.Popen, dict, str]:
    data.mkdir(parents=True, exist_ok=True)
    (data / "settings.json").write_text(json.dumps({"admin": {"port": 0}, "gateway": {"port": 0},
                                                    "worker_bus": {"port": 0}}))
    token = secrets.token_urlsafe(32)
    proc = subprocess.Popen([sys.executable, "-m", "hgs", "--core", "--token-stdin", "--data-dir", str(data)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    proc.stdin.write(token + "\n")
    proc.stdin.flush()
    ready = json.loads(proc.stdout.readline())
    assert ready["event"] == "ready", ready
    return proc, ready, token


async def test_killing_the_core_leaves_no_game_processes(tmp_path):
    proc, ready, token = _start_core_process(tmp_path / "data")
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{ready['admin_port']}/admin/v1",
                                     headers={"Authorization": f"Bearer {token}"}) as admin:
            await admin.post("/games", json={"path": str(logic_game(tmp_path, "victim"))})
            view = (await admin.post("/games/victim/start")).json()
            assert view["state"] == "running", view
            worker_pid = view["metrics"]["pid"]
        assert psutil.pid_exists(worker_pid)
        proc.kill()  # like a crash or "End task" in Task Manager
        proc.wait(10)
        w = psutil.Process(worker_pid)
        await asyncio.get_running_loop().run_in_executor(None, lambda: w.wait(15))
        assert not w.is_running() or w.status() == psutil.STATUS_ZOMBIE
    finally:
        if proc.poll() is None:
            proc.kill()
    # the next start notices the crash and does NOT start servers automatically
    proc2, ready2, token2 = _start_core_process(tmp_path / "data")
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{ready2['admin_port']}/admin/v1",
                                     headers={"Authorization": f"Bearer {token2}"}) as admin:
            st = (await admin.get("/status")).json()
            assert st["previous_unclean_shutdown"] is True and st["games_running"] == 0
    finally:
        proc2.stdin.close()  # graceful: closing the control pipe stops the core
        assert proc2.wait(20) == 0


async def test_closing_control_pipe_stops_core_gracefully(tmp_path):
    proc, ready, token = _start_core_process(tmp_path / "data")
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{ready['admin_port']}/admin/v1",
                                 headers={"Authorization": f"Bearer {token}"}) as admin:
        await admin.post("/games", json={"path": str(logic_game(tmp_path, "graceful"))})
        worker_pid = (await admin.post("/games/graceful/start")).json()["metrics"]["pid"]
    proc.stdin.close()  # the GUI exited
    assert proc.wait(20) == 0
    assert json.loads((tmp_path / "data" / "run_state.json").read_text())["clean"] is True
    snaps = list((tmp_path / "data" / "games" / "graceful" / "saves").glob("*_stop.json"))
    assert snaps, "state was saved during the graceful shutdown"
    assert not psutil.pid_exists(worker_pid) or psutil.Process(worker_pid).status() == psutil.STATUS_ZOMBIE


# ---------------------------------------------------------------- malformed input (fuzz)
def _mutations(rng: random.Random) -> list[str | bytes]:
    valid = {"v": 1, "t": "game", "seq": 5, "d": {"e": "move", "d": {"x": 1}}}
    out: list[str | bytes] = []
    for _ in range(400):
        kind = rng.randrange(9)
        if kind == 0:
            out.append(bytes(rng.randrange(256) for _ in range(rng.randrange(1, 200))))
        elif kind == 1:
            s = json.dumps(valid)
            i = rng.randrange(len(s))
            out.append(s[:i] + chr(rng.randrange(32, 0x2FFF)) + s[i + 1:])
        elif kind == 2:
            out.append(json.dumps(valid)[: rng.randrange(1, 40)])
        elif kind == 3:
            out.append(json.dumps({**valid, "t": rng.choice(["room.join", "room.create", "room.state.set", "hello",
                                                             "x" * 70, "", None, 5])}))
        elif kind == 4:
            out.append(json.dumps({**valid, "d": rng.choice([None, [], "str", 1e308, {"e": None}, {"e": "a" * 100},
                                                             {"room_id": {"$ne": 1}}, {"name": "\u0000‮"}])}))
        elif kind == 5:
            out.append(json.dumps({**valid, "seq": rng.choice([-1, 0, 2**64, "1", None, 1.5])}))
        elif kind == 6:
            out.append("[" * rng.randrange(1, 3000))
        elif kind == 7:
            out.append(json.dumps({**valid, "v": rng.choice([0, 2, "1", None])}))
        else:
            out.append('{"v":1,"t":"game","seq":9,"d":' + '{"a":' * 80 + "1" + "}" * 80 + "}")
    return out


async def test_fuzzed_messages_do_not_break_the_server(hgs):
    await hgs.add_and_start("fuzz", limits={"messages_per_s": 1000, "burst": 1000})
    bystander = await hgs.client("fuzz", "Calm")
    await bystander.join()
    rng = random.Random(1234)
    frames = _mutations(rng)
    sent = 0
    while sent < len(frames):
        c = await hgs.client("fuzz", f"F{sent}")
        assert c.welcome["t"] == "welcome", c.welcome
        try:
            for frame in frames[sent:sent + 50]:
                await c.ws.send(frame)
                sent += 1
            await c.ws.close()
        except websockets.ConnectionClosed:
            pass  # the server may close a connection that misbehaves — that is expected
        sent = max(sent, (sent // 50 + 1) * 50) if sent % 50 else sent
    # the server is healthy: the bystander still works, nothing leaked
    await bystander.send("game", {"e": "still-alive", "d": 1})
    assert (await bystander.request("room.list"))["ok"]
    await asyncio.sleep(0.3)
    assert hgs.core.sessions.count() == 1
    st = (await hgs.admin.get("/status")).json()
    assert st["games_running"] == 1


# ---------------------------------------------------------------- leaks under churn
async def test_connection_churn_does_not_leak(core_factory):
    hgs = await core_factory(make_settings(max_connections_per_ip=100))
    await hgs.add_and_start("churn", rooms={"max_players": 1000})
    proc = psutil.Process()

    async def one(i: int) -> None:
        c = await hgs.client("churn", f"P{i % 40}")
        await c.join()
        await c.send("game", {"e": "hi", "d": i})
        if i % 3 == 0:
            await c.ws.close(code=4000)  # abnormal close -> detached session
        else:
            await c.close()

    for i in range(40):  # warm-up
        await one(i)
    gc.collect()
    rss_before = proc.memory_info().rss
    for batch in range(10):
        await asyncio.gather(*(one(batch * 40 + i) for i in range(40)))
    await asyncio.sleep(0.5)
    for s in list(hgs.core.sessions.all()):  # expire detached sessions now
        s.detached_until = time.time() - 1 if s.detached else s.detached_until
    hgs.core.gateway.expire_sessions()
    await asyncio.sleep(0.5)
    gc.collect()
    growth_mb = (proc.memory_info().rss - rss_before) / 1e6
    assert hgs.core.sessions.count() == 0
    assert hgs.core.sessions._per_ip == {}
    assert all(not r.players for r in hgs.core.rooms.list("churn"))
    assert growth_mb < 30, f"memory grew by {growth_mb:.1f} MB after 400 connections"


# ---------------------------------------------------------------- corrupted files, busy ports
async def test_corrupted_files_are_handled(core_factory, tmp_path):
    data = tmp_path / "data"
    h = await core_factory(data_dir=data)
    await h.add_and_start("good")
    await h.add_and_start("broken")
    await h.admin.post("/games/good/stop", json={"grace_s": 0})
    await h.admin.post("/games/broken/stop", json={"grace_s": 0})
    await h.core.stop()
    (data / "games" / "broken" / "game.json").write_text("{ not json")
    h2 = await core_factory(data_dir=data)
    ids = [g["id"] for g in (await h2.admin.get("/games")).json()]
    assert ids == ["good"]  # a broken game does not prevent HGS from starting
    logs = (await h2.admin.get("/logs/core")).json()
    assert any("cannot load game broken" in l["msg"] for l in logs)


async def test_corrupted_snapshot_falls_back_to_older(hgs, tmp_path):
    await hgs.add_and_start("snap", **{"backend": {"mode": "relay"}})
    saves = tmp_path / "data" / "games" / "snap" / "saves"
    (saves / "20200101-000000-000_stop.json").write_text('{"counter": 7}')
    (saves / "20990101-000000-000_stop.json").write_text("{ corrupted")
    assert hgs.core.saves.latest("snap") == ("json", {"counter": 7})


def test_invalid_settings_file_is_reported_not_replaced(tmp_path):
    from hgs.core.config.settings import load_settings

    f = tmp_path / "settings.json"
    f.write_text('{"gateway": {"port": "eighty"}}')
    with pytest.raises(SettingsError) as e:
        load_settings(f)
    assert "gateway.port" in str(e.value) or "port" in str(e.value)
    assert f.read_text() == '{"gateway": {"port": "eighty"}}'  # the user's file is left untouched


async def test_busy_port_gives_a_clear_error(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        settings = make_settings()
        settings.gateway.port = port
        core = CoreApp(tmp_path / "data", secrets.token_urlsafe(32), settings)
        with pytest.raises(CoreStartError) as e:
            await core.start()
        assert f"127.0.0.1:{port}" in str(e.value) and "busy" in str(e.value)
    # the lock was released: a corrected start works
    core2 = CoreApp(tmp_path / "data", secrets.token_urlsafe(32), make_settings())
    await core2.start()
    await core2.stop()


def test_core_process_reports_start_errors_to_the_gui(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text("{ broken json")
    proc = subprocess.run([sys.executable, "-m", "hgs", "--core", "--token-stdin", "--data-dir", str(data)],
                          input="x" * 43 + "\n", capture_output=True, text=True, timeout=30)
    assert proc.returncode == 2
    msg = json.loads(proc.stdout.strip().splitlines()[-1])
    assert msg["event"] == "error" and "settings" in msg["message"].lower()


async def test_round_trip_latency_is_low(hgs):
    """A reply followed by an event must not wait for a delayed TCP ACK (TCP_NODELAY)."""
    await hgs.add_and_start("lat")
    a = await hgs.client("lat", "Ann")
    b = await hgs.client("lat", "Bob")
    await a.join()
    await b.join()
    timings = []
    for i in range(20):
        t = time.perf_counter()
        await a.join("lobby")  # reply + room.joined: two frames in a row
        await a.send("game", {"e": "ping", "d": i})
        await b.recv_type("event")
        timings.append(time.perf_counter() - t)
    timings.sort()
    assert timings[len(timings) // 2] < 0.02, f"median round trip {timings[len(timings) // 2] * 1000:.1f} ms"
