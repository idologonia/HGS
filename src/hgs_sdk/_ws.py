"""Minimal WebSocket client (RFC 6455) on asyncio streams — standard library only.

hgs_sdk must run inside any Python environment a game uses (its own venv), so it cannot
depend on third-party packages. Only what the Worker Bus needs is implemented:
ws:// to 127.0.0.1, text frames, fragmentation, ping/pong, close.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct
import urllib.parse

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class ConnectionClosed(Exception):
    def __init__(self, code: int = 1006, reason: str = "") -> None:
        super().__init__(f"connection closed ({code}) {reason}")
        self.code = code
        self.reason = reason


class WebSocketClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, max_size: int) -> None:
        self._r = reader
        self._w = writer
        self.max_size = max_size
        self._send_lock = asyncio.Lock()
        self.closed = False

    @classmethod
    async def connect(cls, url: str, max_size: int = 2 * 1024 * 1024, timeout: float = 10.0) -> "WebSocketClient":
        u = urllib.parse.urlsplit(url)
        if u.scheme != "ws":
            raise ValueError("only ws:// URLs are supported")
        host, port = u.hostname or "127.0.0.1", u.port or 80
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, limit=max_size + 1024), timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        request = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        writer.write(request.encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            writer.close()
            raise ConnectionError(f"WebSocket handshake failed: {lines[0]}")
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
        expected = base64.b64encode(hashlib.sha1(key.encode() + _GUID).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            writer.close()
            raise ConnectionError("WebSocket handshake failed: bad accept key")
        return cls(reader, writer, max_size)

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if n < 4096 else _mask_fast(payload, mask)
        async with self._send_lock:
            if self.closed:
                raise ConnectionClosed(1006, "already closed")
            self._w.write(bytes(header) + masked)
            await self._w.drain()

    async def send(self, text: str) -> None:
        await self._send_frame(0x1, text.encode("utf-8"))

    async def recv(self) -> str:
        buf = bytearray()
        msg_opcode = None
        while True:
            try:
                b1, b2 = await self._r.readexactly(2)
            except (asyncio.IncompleteReadError, ConnectionError):
                self.closed = True
                raise ConnectionClosed(1006, "connection lost")
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                (n,) = struct.unpack("!H", await self._r.readexactly(2))
            elif n == 127:
                (n,) = struct.unpack("!Q", await self._r.readexactly(8))
            if n > self.max_size or len(buf) + n > self.max_size:
                await self.close(1009)
                raise ConnectionClosed(1009, "message too big")
            mask = await self._r.readexactly(4) if b2 & 0x80 else None
            data = await self._r.readexactly(n) if n else b""
            if mask:
                data = _mask_fast(data, mask)
            if opcode == 0x8:  # close
                code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 1005
                if not self.closed:
                    try:
                        await self._send_frame(0x8, data[:2])
                    except Exception:
                        pass
                self.closed = True
                self._w.close()
                raise ConnectionClosed(code, data[2:].decode("utf-8", "replace"))
            if opcode == 0x9:  # ping
                await self._send_frame(0xA, data)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x2):
                msg_opcode = opcode
                buf = bytearray(data)
            elif opcode == 0x0:
                buf += data
            if fin and msg_opcode is not None:
                return buf.decode("utf-8") if msg_opcode == 0x1 else bytes(buf).decode("utf-8", "replace")

    async def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        try:
            await self._send_frame(0x8, struct.pack("!H", code))
        except Exception:
            pass
        self.closed = True
        self._w.close()


def _mask_fast(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    key = int.from_bytes((mask * (n // 4 + 1))[:n], "big") if n else 0
    return (int.from_bytes(data, "big") ^ key).to_bytes(n, "big") if n else b""
