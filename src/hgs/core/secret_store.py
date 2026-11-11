"""Secret storage (tunnel tokens, DuckDNS token, ...).

Windows: values are encrypted with DPAPI, bound to the current Windows user account,
so the database file alone is useless on another PC or for another user.
Other systems (development only): stored base64-encoded and marked as unprotected.
"""

from __future__ import annotations

import base64
import logging
import sys

from hgs.core.db.database import Database, utcnow
from hgs.core.logging_setup import register_secret

log = logging.getLogger("hgs.secrets")

_PREFIX_DPAPI = b"dpapi:"
_PREFIX_PLAIN = b"plain:"


def _protect(value: str) -> bytes:
    data = value.encode("utf-8")
    if sys.platform == "win32":
        import win32crypt

        return _PREFIX_DPAPI + win32crypt.CryptProtectData(data, "HGS secret", None, None, None, 0)
    return _PREFIX_PLAIN + base64.b64encode(data)


def _unprotect(blob: bytes) -> str:
    if blob.startswith(_PREFIX_DPAPI):
        import win32crypt

        return win32crypt.CryptUnprotectData(blob[len(_PREFIX_DPAPI):], None, None, None, 0)[1].decode("utf-8")
    if blob.startswith(_PREFIX_PLAIN):
        return base64.b64decode(blob[len(_PREFIX_PLAIN):]).decode("utf-8")
    raise ValueError("unknown secret format")


class SecretStore:
    NAMES = ("cloudflare_tunnel_token", "duckdns_token", "github_token")

    def __init__(self, db: Database) -> None:
        self.db = db
        self.protected = sys.platform == "win32"
        if not self.protected:
            log.warning("secrets are NOT encrypted on this platform (development mode)")

    async def set(self, name: str, value: str) -> None:
        if name not in self.NAMES:
            raise ValueError(f"unknown secret {name!r}")
        if not value or len(value) > 4096:
            raise ValueError("secret value is empty or too long")
        register_secret(value)
        await self.db.execute(
            "INSERT INTO secrets(name, blob, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET blob=excluded.blob, updated_at=excluded.updated_at",
            (name, _protect(value), utcnow()),
        )

    async def get(self, name: str) -> str | None:
        row = await self.db.fetchone("SELECT blob FROM secrets WHERE name=?", (name,))
        if row is None:
            return None
        try:
            value = _unprotect(row["blob"])
        except Exception as exc:
            log.error("cannot decrypt secret %s (created by another Windows user?): %s", name, exc)
            return None
        register_secret(value)
        return value

    async def delete(self, name: str) -> None:
        await self.db.execute("DELETE FROM secrets WHERE name=?", (name,))

    async def names(self) -> list[str]:
        return [r["name"] for r in await self.db.fetchall("SELECT name FROM secrets ORDER BY name")]
