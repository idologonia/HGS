"""Browser tests: real Chromium runs the demo game and the hgs.js SDK against a real core."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.async_api")

REPO = Path(__file__).resolve().parents[2]
CLIENT = (REPO / "examples" / "demo-arena" / "client" / "index.html").as_uri()


def _chromium_path() -> str | None:
    p = os.environ.get("HGS_CHROMIUM") or "/opt/pw-browsers/chromium"
    return p if os.path.exists(p) else None


@pytest.fixture
async def browser():
    async with pw.async_playwright() as p:
        try:
            b = await p.chromium.launch(executable_path=_chromium_path())
        except Exception as exc:  # browser not installed
            pytest.skip(f"Chromium not available: {exc}")
        yield b
        await b.close()


async def open_player(browser, hgs, game: str, name: str, extra: str = "", storage: str = "memory"):
    page = await (await browser.new_context()).new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    url = f"{CLIENT}?hgs=ws://127.0.0.1:{hgs.core.gateway_port}&game={game}&name={name}&storage={storage}{extra}"
    await page.goto(url)
    await page.wait_for_function("window.demo && demo.hgs.state === 'connected' && demo.hgs.room", timeout=10000)
    page.errors = errors
    return page


async def player_id(page) -> str:
    return await page.evaluate("demo.hgs.player.id")


async def hold_key(page, key: str, ms: int) -> None:
    await page.keyboard.down(key)
    await asyncio.sleep(ms / 1000)
    await page.keyboard.up(key)


async def start_demo(hgs, folder: str, game_id: str) -> None:
    r = await hgs.admin.post("/games", json={"path": str(REPO / "examples" / "demo-arena" / folder)})
    assert r.status_code == 201, r.text
    assert (await hgs.admin.post(f"/games/{game_id}/start")).json()["state"] == "running"


@pytest.mark.parametrize("folder,game_id,mode", [("relay", "demo-arena", "relay"),
                                                  ("logic", "demo-arena-logic", "logic")])
async def test_two_browsers_see_each_other_move(hgs, browser, folder, game_id, mode):
    await start_demo(hgs, folder, game_id)
    a = await open_player(browser, hgs, game_id, "Ann")
    b = await open_player(browser, hgs, game_id, "Bob")
    assert await a.evaluate("demo.mode") == mode
    aid = await player_id(a)
    await b.wait_for_function(f"demo.positions()['{aid}'] !== undefined", timeout=5000)
    x0 = await b.evaluate(f"demo.positions()['{aid}'].x")
    start_x_own = await a.evaluate(f"demo.positions()['{aid}'].x")
    await hold_key(a, "ArrowRight", 400)
    await asyncio.sleep(0.4)
    x_seen_by_b = await b.evaluate(f"demo.positions()['{aid}'].x")
    x_own = await a.evaluate(f"demo.positions()['{aid}'].x")
    if start_x_own < 700:  # not already at the right wall
        assert x_seen_by_b > x0 + 30, (x0, x_seen_by_b)
    assert abs(x_seen_by_b - x_own) <= 2
    names = await b.evaluate("demo.hgs.room.players.map(p => p.name).sort()")
    assert names == ["Ann", "Bob"]
    assert not a.errors and not b.errors


async def test_sdk_reconnects_and_rejoins_after_server_restart(hgs, browser):
    await start_demo(hgs, "relay", "demo-arena")
    a = await open_player(browser, hgs, "demo-arena", "Ann")
    await a.evaluate("window.events = []; ['hgs:disconnected','hgs:reconnecting','hgs:connected','hgs:server_shutdown']"
                     ".forEach(e => demo.hgs.on(e, () => events.push(e))); null")
    await hgs.admin.post("/games/demo-arena/stop", json={"grace_s": 0.2})
    await a.wait_for_function("demo.hgs.state === 'reconnecting'", timeout=5000)
    await hgs.admin.post("/games/demo-arena/start")
    await a.wait_for_function("demo.hgs.state === 'connected' && demo.hgs.room && demo.hgs.room.id === 'lobby'",
                              timeout=15000)
    events = await a.evaluate("events")
    assert events[0] == "hgs:server_shutdown" and "hgs:reconnecting" in events and events[-1] == "hgs:connected"


async def test_kicked_player_does_not_reconnect(hgs, browser):
    await start_demo(hgs, "relay", "demo-arena")
    a = await open_player(browser, hgs, "demo-arena", "Ann")
    await a.evaluate("window.kicked = null; demo.hgs.on('hgs:kicked', d => kicked = d.reason); null")
    await hgs.admin.post(f"/players/demo-arena/{await player_id(a)}/kick", json={"reason": "bye"})
    await a.wait_for_function("demo.hgs.state === 'failed'", timeout=5000)
    assert await a.evaluate("kicked") == "bye"
    await asyncio.sleep(1.2)
    assert await a.evaluate("demo.hgs.state") == "failed"


async def test_invite_login_saves_token_and_hides_code(hgs, browser, tmp_path):
    from tests.conftest import make_game_dir

    d = make_game_dir(tmp_path, "inv-game", auth={"mode": "invite"})
    await hgs.admin.post("/games", json={"path": str(d)})
    await hgs.admin.post("/games/inv-game/start")
    code = (await hgs.admin.post("/invites", json={"game_id": "inv-game"})).json()["code"]
    page = await (await browser.new_context()).new_page()
    base = f"{CLIENT}?hgs=ws://127.0.0.1:{hgs.core.gateway_port}&game=inv-game&name=Ann"
    await page.goto(base + f"&invite={code}")
    await page.wait_for_function("demo.hgs.state === 'connected'", timeout=10000)
    first_id = await player_id(page)
    assert code not in page.url  # removed from the address bar
    assert await page.evaluate("demo.hgs.savedToken") is not None
    await page.goto(base)  # no invite any more: the saved token is used
    await page.wait_for_function("demo.hgs.state === 'connected'", timeout=10000)
    assert await player_id(page) == first_id


async def test_sdk_validates_arguments(hgs, browser):
    await start_demo(hgs, "relay", "demo-arena")
    a = await open_player(browser, hgs, "demo-arena", "Ann")
    result = await a.evaluate("""() => {
        const out = {};
        try { HGS.createClient({ serverUrl: 'ws://x', gameId: 'Bad Id' }); } catch (e) { out.badId = e.code; }
        try { demo.hgs.send('bad name!', 1); } catch (e) { out.badEvent = e.code; }
        try { demo.hgs.send('big', 'x'.repeat(20000)); } catch (e) { out.tooBig = e.code; }
        out.isError = (() => { try { demo.hgs.send(''); } catch (e) { return e instanceof HGS.HGSError; } })();
        return out;
    }""")
    assert result == {"badId": "bad_config", "badEvent": "bad_argument", "tooBig": "too_large", "isError": True}
    # the connection is still fine after rejected calls
    rooms = await a.evaluate("demo.hgs.listRooms()")
    assert rooms[0]["id"] == "lobby"
    err = await a.evaluate("demo.hgs.joinRoom('missing').catch(e => e.code)")
    assert err == "room_not_found"
