"""Client Protocol v1 (browser/game client <-> Gateway). See docs/architecture/06-protocol.md."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from hgs import PROTOCOL_VERSION

TYPE_PATTERN = r"^[a-z][a-z0-9_.]{0,63}$"
EVENT_PATTERN = r"^[A-Za-z0-9_.:-]{1,64}$"
MAX_JSON_DEPTH = 64
NAME_PATTERN = r"^[^\x00-\x1f\x7f<>]{1,24}$"


class ProtocolError(Exception):
    """Invalid frame from a client. `code` is sent back to the client."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    v: int
    t: str = Field(pattern=TYPE_PATTERN)
    id: str | None = Field(None, max_length=64)
    seq: int | None = Field(None, ge=0)
    d: Any = None


# --- hello -----------------------------------------------------------------------
class _A(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthOpen(_A):
    type: Literal["open"]
    name: str = Field(pattern=NAME_PATTERN)


class AuthInvite(_A):
    type: Literal["invite"]
    code: str = Field(min_length=4, max_length=32)
    name: str = Field(pattern=NAME_PATTERN)


class AuthToken(_A):
    type: Literal["token"]
    token: str = Field(min_length=16, max_length=128)


AuthPayload = Annotated[Union[AuthOpen, AuthInvite, AuthToken], Field(discriminator="type")]


class ClientInfo(_A):
    sdk: str | None = Field(None, max_length=64)
    game_version: str | None = Field(None, max_length=32)


class Hello(_A):
    game: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,47}$")
    auth: AuthPayload
    client: ClientInfo = ClientInfo()


# --- requests after hello --------------------------------------------------------
class RoomCreate(_A):
    name: str = Field(pattern=NAME_PATTERN)
    max_players: int | None = Field(None, ge=1, le=1000)
    private: bool = False
    params: dict[str, Any] = {}


class RoomJoin(_A):
    room_id: str = Field(min_length=1, max_length=64)


class GameEvent(_A):
    e: str = Field(pattern=EVENT_PATTERN)
    d: Any = None
    to: str | None = Field(None, max_length=64)


class RoomStateSet(_A):
    state: Any


class RoomStatePatch(_A):
    patch: dict[str, Any]


_hello_adapter = TypeAdapter(Hello)


def parse_frame(raw: str | bytes, max_bytes: int) -> Envelope:
    if isinstance(raw, bytes):
        raise ProtocolError("bad_request", "binary frames are not supported")
    if len(raw.encode("utf-8")) > max_bytes:
        raise ProtocolError("too_large", f"message exceeds {max_bytes} bytes")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        raise ProtocolError("bad_request", "message is not valid JSON")
    if not isinstance(data, dict):
        raise ProtocolError("bad_request", "message must be a JSON object")
    if _too_deep(data, MAX_JSON_DEPTH):
        raise ProtocolError("bad_request", f"message is nested deeper than {MAX_JSON_DEPTH} levels")
    try:
        env = Envelope.model_validate(data)
    except ValidationError as exc:
        raise ProtocolError("bad_request", _short(exc))
    if env.v != PROTOCOL_VERSION:
        raise ProtocolError("version_unsupported", f"protocol version {env.v} is not supported")
    return env


def _too_deep(value: Any, limit: int) -> bool:
    """Iterative depth check (no recursion, so hostile input cannot overflow the stack)."""
    stack = [(value, 1)]
    while stack:
        v, depth = stack.pop()
        if depth > limit:
            return True
        if isinstance(v, dict):
            stack.extend((x, depth + 1) for x in v.values())
        elif isinstance(v, list):
            stack.extend((x, depth + 1) for x in v)
    return False


def parse_payload(model: type[BaseModel], data: Any) -> Any:
    try:
        return model.model_validate(data if data is not None else {})
    except ValidationError as exc:
        raise ProtocolError("bad_request", _short(exc))


def parse_hello(data: Any) -> Hello:
    try:
        return _hello_adapter.validate_python(data if data is not None else {})
    except ValidationError as exc:
        raise ProtocolError("bad_request", _short(exc))


def _short(exc: ValidationError) -> str:
    e = exc.errors()[0]
    loc = ".".join(str(p) for p in e["loc"]) or "message"
    return f"{loc}: {e['msg']}"


def msg(t: str, d: Any = None, **extra: Any) -> str:
    """Build a server -> client frame."""
    out: dict[str, Any] = {"v": PROTOCOL_VERSION, "t": t}
    out.update(extra)
    if d is not None:
        out["d"] = d
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def reply_ok(req_id: str | None, d: Any = None) -> str:
    return msg("reply", d, re=req_id, ok=True)


def reply_err(req_id: str | None, code: str, message: str) -> str:
    return msg("reply", None, re=req_id, ok=False, error={"code": code, "message": message})


# WebSocket close codes (docs 06, §6.2)
CLOSE_PROTOCOL = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_KICKED = 4403
CLOSE_HELLO_TIMEOUT = 4408
CLOSE_REPLACED = 4409
CLOSE_TOO_LARGE = 4413
CLOSE_FLOOD = 4429
CLOSE_GAME_STOPPED = 4503
