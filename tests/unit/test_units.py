import asyncio

import pytest

from hgs.core.auth import new_invite_code, normalize_invite
from hgs.core.config.manifest import ManifestError, parse_manifest
from hgs.core.events import EventBus
from hgs.core.protocol.client import ProtocolError, parse_frame, parse_hello
from hgs.core.ratelimit import LoginLimiter, TokenBucket


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_token_bucket_burst_and_refill():
    c = Clock()
    b = TokenBucket(rate=10, burst=5, clock=c)
    assert all(b.allow() for _ in range(5))
    assert not b.allow()
    c.t += 0.5  # +5 tokens
    assert all(b.allow() for _ in range(5))
    assert not b.allow()


def test_login_limiter_blocks_after_5_failures_per_minute():
    c = Clock()
    lim = LoginLimiter(clock=c)
    for i in range(4):
        assert lim.failure("1.2.3.4") is False
    assert lim.failure("1.2.3.4") is True
    assert lim.blocked_for("1.2.3.4") > 0
    assert lim.blocked_for("5.6.7.8") == 0
    c.t += 301
    assert lim.blocked_for("1.2.3.4") == 0


@pytest.mark.parametrize(
    "raw,limit,code",
    [
        ("not json", 1000, "bad_request"),
        ("[1,2]", 1000, "bad_request"),
        ('{"v":1}', 1000, "bad_request"),
        ('{"v":2,"t":"ping","seq":1}', 1000, "version_unsupported"),
        ('{"v":1,"t":"Bad Type","seq":1}', 1000, "bad_request"),
        ('{"v":1,"t":"ping","seq":1,"extra":1}', 1000, "bad_request"),
        (b'{"v":1,"t":"ping","seq":1}', 1000, "bad_request"),
        ("x" * 200, 150, "too_large"),
        ("[" * 100000 + "]" * 100000, 1000, "too_large"),
    ],
)
def test_parse_frame_rejects_bad_input(raw, limit, code):
    with pytest.raises(ProtocolError) as e:
        parse_frame(raw, limit)
    assert e.value.code == code


def test_parse_frame_accepts_valid():
    env = parse_frame('{"v":1,"t":"game","seq":3,"id":"a","d":{"e":"move","d":[1,2]}}', 1000)
    assert env.t == "game" and env.seq == 3 and env.d["e"] == "move"


def test_deeply_nested_json_is_rejected_not_crash():
    raw = '{"v":1,"t":"game","seq":1,"d":' + "[" * 5000 + "]" * 5000 + "}"
    with pytest.raises(ProtocolError):
        parse_frame(raw, 10**6)


def test_hello_validation():
    h = parse_hello({"game": "my-game", "auth": {"type": "open", "name": "Ann"}})
    assert h.auth.name == "Ann"
    for bad in [
        {"game": "My Game", "auth": {"type": "open", "name": "Ann"}},
        {"game": "g1", "auth": {"type": "root", "name": "x"}},
        {"game": "g1", "auth": {"type": "open", "name": "<script>"}},
        {"game": "g1", "auth": {"type": "open", "name": ""}},
    ]:
        with pytest.raises(ProtocolError):
            parse_hello(bad)


def test_invite_code_format_and_normalization():
    code = new_invite_code()
    assert len(code) == 12 and code.count("-") == 2
    assert normalize_invite(code.lower().replace("-", " ")) == code


def test_manifest_errors_point_to_field():
    with pytest.raises(ManifestError) as e:
        parse_manifest({"id": "Bad Id", "name": "x", "adapter": "html", "unknown": 1})
    fields = {err["field"] for err in e.value.errors}
    assert "id" in fields and "unknown" in fields
    m = parse_manifest({"id": "ok-game", "name": "Ok", "adapter": "html",
                        "client": {"allowed_origins": ["https://Name.github.io/"]}})
    assert m.client.allowed_origins == ["https://name.github.io"]
    with pytest.raises(ManifestError):
        parse_manifest({"id": "ok-game", "name": "Ok", "adapter": "html",
                        "client": {"allowed_origins": ["https://name.github.io/game"]}})


async def test_event_bus_slow_subscriber_drops_oldest():
    bus = EventBus()
    sub = bus.subscribe(maxsize=3)
    other = bus.subscribe("room.")
    for i in range(5):
        bus.publish("x.y", i=i)
    bus.publish("room.created", i=99)
    assert sub.dropped == 3
    got = [await sub.get() for _ in range(3)]
    assert [e.data["i"] for e in got] == [3, 4, 99]
    assert (await asyncio.wait_for(other.get(), 1)).data["i"] == 99


def test_js_sdk_is_pure_ascii():
    """hgs.js must work even on pages that do not declare their encoding."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "sdk" / "js" / "hgs.js").read_bytes()
    assert all(b < 128 for b in text)
