"""Smoke test of the BUILT application (dist/HGS). Used by CI after PyInstaller.

    python packaging/smoke_test.py dist/HGS/HGS[.exe]

Checks: the core starts from the executable, the admin API answers, a game with server
logic runs in a worker started from the same executable, the SDK is served, a player can
join and receives game state, a Python game in worker mode gets hgs_sdk, the GUI starts,
and the core stops cleanly when its control pipe closes.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def admin(port: int, token: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/admin/v1{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=60) as r:
        return json.loads(r.read() or b"null")


async def player(gateway: int, game: str) -> dict:
    import websockets

    async with websockets.connect(f"ws://127.0.0.1:{gateway}/hgs/v1/ws", proxy=None) as ws:
        await ws.send(json.dumps({"v": 1, "t": "hello", "seq": 1, "d": {"game": game, "auth": {"type": "open", "name": "Smoke"}}}))
        assert json.loads(await ws.recv())["t"] == "welcome"
        await ws.send(json.dumps({"v": 1, "t": "room.join", "seq": 2, "id": "j", "d": {"room_id": "lobby"}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if m["t"] == "event":
                return m["d"]


def main(exe: str) -> int:
    exe = str(Path(exe).resolve())
    tmp = Path(tempfile.mkdtemp())
    data = tmp / "data"
    data.mkdir()
    (data / "settings.json").write_text(json.dumps({"admin": {"port": 0}, "gateway": {"port": 0}, "worker_bus": {"port": 0}}))
    token = secrets.token_urlsafe(32)
    core = subprocess.Popen([exe, "--core", "--token-stdin", "--data-dir", str(data)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    core.stdin.write(token + "\n")
    core.stdin.flush()
    line = core.stdout.readline()
    print("core:", line.strip())
    ready = json.loads(line)
    assert ready["event"] == "ready", ready
    a = ready["admin_port"]
    try:
        st = admin(a, token, "GET", "/status")
        print("status ok, version", st["version"])
        types = [t["type"] for t in admin(a, token, "GET", "/adapters")]
        assert {"html", "python", "process"} <= set(types), types

        # HTML game with server logic: the worker is the same executable (--worker)
        admin(a, token, "POST", "/games", {"path": str(ROOT / "examples" / "demo-arena" / "logic")})
        view = admin(a, token, "POST", "/games/demo-arena-logic/start")
        assert view["state"] == "running", view
        ev = asyncio.run(player(ready["gateway_port"], "demo-arena-logic"))
        assert ev["e"] == "game_state", ev
        print("logic worker ok, pid", view["metrics"].get("pid"))

        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                f"http://127.0.0.1:{ready['gateway_port']}/hgs/sdk/hgs.js", timeout=10) as r:
            assert b"HOME GAME SERVER" in r.read()
        print("sdk served ok")

        # Python game in worker mode with a system Python: needs approval, gets hgs_sdk via PYTHONPATH
        py = shutil.which("python") or shutil.which("python3")
        if py and "WindowsApps" not in py:
            game = tmp / "py-counter"
            shutil.copytree(ROOT / "examples" / "python-worker", game)
            admin(a, token, "POST", "/games", {"path": str(game)})
            view = admin(a, token, "POST", "/games/py-counter/start")
            if view["state"] == "error" and view["last_error"].startswith("program is not in the allowlist: "):
                admin(a, token, "POST", "/allowlist", {"path": view["last_error"].split(": ", 1)[1]})
                view = admin(a, token, "POST", "/games/py-counter/start")
            assert view["state"] == "running", view
            print("python worker ok")
    finally:
        core.stdin.close()  # graceful stop, as when the GUI exits
        code = core.wait(60)
        err = core.stderr.read()
        if err.strip():
            print("core stderr:\n" + err[-3000:])
    assert code == 0, f"core exit code {code}"
    assert json.loads((data / "run_state.json").read_text())["clean"] is True
    print("graceful stop ok")

    # the GUI starts and launches its own core
    env = dict(os.environ, HGS_DATA_DIR=str(tmp / "gui-data"), QT_QPA_PLATFORM=os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    (tmp / "gui-data").mkdir()
    (tmp / "gui-data" / "settings.json").write_text(json.dumps({"admin": {"port": 0}, "gateway": {"port": 0},
                                                                "worker_bus": {"port": 0}}))
    gui = subprocess.Popen([exe], env=env)
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            if (tmp / "gui-data" / "hgs.db").exists() and gui.poll() is None:
                break
            time.sleep(0.5)
        assert gui.poll() is None, f"GUI exited with code {gui.returncode}"
        assert (tmp / "gui-data" / "hgs.db").exists(), "GUI did not start the core"
        print("gui ok")
    finally:
        gui.terminate()
        try:
            gui.wait(20)
        except subprocess.TimeoutExpired:
            gui.kill()
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
