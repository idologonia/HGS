"""Publishing the server address to GitHub, so games find the server without links."""

import asyncio

import pytest

from tests.fake_github import FakeGitHub
from tests.integration.test_internet import fake_cloudflared, net  # noqa: F401 (fixture)

KEY = "me/hgs-address/hgs-server.json"


@pytest.fixture
def github(monkeypatch):
    from hgs.core.network import publisher

    fake = FakeGitHub()
    monkeypatch.setattr(publisher, "API", fake.url)
    yield fake
    fake.close()


async def wait_doc(github, pred, timeout=10):
    for _ in range(int(timeout / 0.05)):
        d = github.doc(KEY)
        if d is not None and pred(d):
            return d
        await asyncio.sleep(0.05)
    raise AssertionError(f"published document not as expected: {github.doc(KEY)}")


async def test_address_follows_the_tunnel(hgs, github, fake_cloudflared):  # noqa: F811
    r = await hgs.admin.post("/network/publish")
    assert r.status_code == 400  # nothing configured yet
    await hgs.admin.put("/secrets/github_token", json={"value": "ghp_test"})
    await net(hgs, mode="tunnel", publish={"enabled": True, "repo": "me/hgs-address"})
    d = await wait_doc(github, lambda d: d["online"] is False)  # tunnel not started yet
    await hgs.admin.post("/network/tunnel/start")
    d = await wait_doc(github, lambda d: d["online"])
    assert d["server"] == "wss://brave-test-words.trycloudflare.com"
    view = (await hgs.admin.get("/network")).json()["publish"]
    assert view["error"] is None and "онлайн" in view["status"]
    await hgs.admin.post("/network/tunnel/stop")
    await wait_doc(github, lambda d: d["online"] is False and d["server"] is None)
    assert github.puts >= 3  # updates reuse the file's sha


async def test_wrong_token_is_reported(hgs, github):
    await hgs.admin.put("/secrets/github_token", json={"value": "wrong"})
    await net(hgs, mode="tunnel", publish={"enabled": True, "repo": "me/hgs-address"})
    r = await hgs.admin.post("/network/publish")
    assert r.status_code == 400 and "401" in r.json()["message"]
    assert "токен" in (await hgs.admin.get("/network")).json()["publish"]["error"]


async def test_core_stop_publishes_offline(core_factory, github, tmp_path):
    h = await core_factory()
    await h.admin.put("/secrets/github_token", json={"value": "ghp_test"})
    await net(h, mode="tunnel", publish={"enabled": True, "repo": "me/hgs-address"})
    github.files[KEY] = b'{"server": "wss://old.trycloudflare.com", "online": true}'
    await h.admin.post("/network/publish")
    await h.core.stop()
    assert github.doc(KEY)["online"] is False


async def test_stale_github_cache_and_parallel_publishes(hgs, github):
    await hgs.admin.put("/secrets/github_token", json={"value": "ghp_test"})
    await net(hgs, mode="tunnel", publish={"enabled": True, "repo": "me/hgs-address"})
    github.files[KEY] = b'{"online": true, "server": "wss://old"}'
    github._old[KEY] = b'{"older": 1}'
    github.stale_gets = 1  # the first read returns an outdated version -> 409 -> re-read -> success
    hgs.core.network.publisher._sha.clear()
    r = await hgs.admin.post("/network/publish")
    assert r.status_code == 200, r.text
    # the button right after saving: several publishes at once must not conflict
    results = await asyncio.gather(*(hgs.admin.post("/network/publish") for _ in range(3)))
    assert all(x.status_code == 200 for x in results), [x.text for x in results]
    assert (await hgs.admin.get("/network")).json()["publish"]["error"] is None
