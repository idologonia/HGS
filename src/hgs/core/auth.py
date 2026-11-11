"""Authentication Manager: invites, player tokens, login rate limiting.

Only hashes of codes and tokens are stored (SHA-256); comparison happens by hash lookup.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db.database import Database, utcnow
from .ratelimit import LoginLimiter
from .security_log import SecurityJournal

# No 0/O/1/I/L to avoid confusion when a code is dictated.
INVITE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
PLAYER_TOKEN_TTL = timedelta(days=30)


class AuthError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PlayerInfo:
    game_id: str
    player_id: str
    name: str


def sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def new_invite_code() -> str:
    raw = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(10))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def normalize_invite(code: str) -> str:
    raw = "".join(ch for ch in code.upper() if ch.isalnum())
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
MIN_PASSWORD = 6


def hash_password(password: str) -> bytes:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            maxmem=64 * 1024 * 1024, dklen=32)
    return b"scrypt$%d$%d$%d$" % (SCRYPT_N, SCRYPT_R, SCRYPT_P) + salt.hex().encode() + b"$" + digest.hex().encode()


def verify_password(password: str, stored: bytes | None) -> bool:
    if not stored:
        return False
    try:
        _, n, r, p, salt, digest = stored.split(b"$")
        got = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt.decode()), n=int(n), r=int(r),
                             p=int(p), maxmem=64 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(got, bytes.fromhex(digest.decode()))
    except (ValueError, TypeError):
        return False


def new_player_id() -> str:
    return "p_" + secrets.token_hex(6)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class AuthManager:
    def __init__(self, db: Database, journal: SecurityJournal) -> None:
        self.db = db
        self.journal = journal
        self.limiter = LoginLimiter()

    # -- invites --------------------------------------------------------------------
    async def create_invite(
        self, game_id: str, *, max_uses: int = 1, ttl_s: int = 7 * 86400, label: str | None = None
    ) -> dict:
        if not 1 <= max_uses <= 1000:
            raise ValueError("max_uses must be 1..1000")
        if not 60 <= ttl_s <= 30 * 86400:
            raise ValueError("ttl_s must be between 60 seconds and 30 days")
        code = new_invite_code()
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_s)
        await self.db.execute(
            "INSERT INTO invites(code_hash, game_id, label, max_uses, created_at, expires_at) VALUES (?,?,?,?,?,?)",
            (sha256(code), game_id, label, max_uses, _iso(now), _iso(expires)),
        )
        return {"code": code, "game_id": game_id, "max_uses": max_uses, "expires_at": _iso(expires), "label": label}

    async def list_invites(self, game_id: str) -> list[dict]:
        return await self.db.fetchall(
            "SELECT label, max_uses, uses, created_at, expires_at, revoked FROM invites WHERE game_id=? ORDER BY created_at DESC",
            (game_id,),
        )

    # -- game password (mode "password") -------------------------------------------
    async def set_game_password(self, game_id: str, password: str) -> None:
        if len(password) < MIN_PASSWORD or len(password) > 128:
            raise ValueError(f"password must be {MIN_PASSWORD}..128 characters")
        h = await asyncio.to_thread(hash_password, password)  # scrypt is slow on purpose
        await self.db.execute(
            "INSERT INTO game_passwords(game_id, password_hash, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(game_id) DO UPDATE SET password_hash=excluded.password_hash, updated_at=excluded.updated_at",
            (game_id, h, utcnow()),
        )

    async def has_game_password(self, game_id: str) -> bool:
        return await self.db.fetchone("SELECT 1 FROM game_passwords WHERE game_id=?", (game_id,)) is not None

    def _issue_token(self, c: sqlite3.Connection, game_id: str, pid: str, now: datetime) -> str:
        token = secrets.token_urlsafe(32)
        c.execute(
            "INSERT INTO player_tokens(token_hash, game_id, player_id, created_at, expires_at, last_used_at) VALUES (?,?,?,?,?,?)",
            (sha256(token), game_id, pid, _iso(now), _iso(now + PLAYER_TOKEN_TTL), _iso(now)),
        )
        return token

    async def revoke_player_tokens(self, game_id: str, player_id: str) -> int:
        return await self.db.execute("UPDATE player_tokens SET revoked=1 WHERE game_id=? AND player_id=?",
                                     (game_id, player_id))

    async def set_banned(self, game_id: str, player_id: str, banned: bool, reason: str | None = None) -> bool:
        n = await self.db.execute("UPDATE players SET banned=?, ban_reason=? WHERE game_id=? AND player_id=?",
                                  (int(banned), reason if banned else None, game_id, player_id))
        return n > 0

    async def login_password(self, game_id: str, name: str, password: str, ip: str) -> tuple[PlayerInfo, str]:
        self.check_blocked(ip)
        row = await self.db.fetchone("SELECT password_hash FROM game_passwords WHERE game_id=?", (game_id,))
        if row is None:
            raise AuthError("unauthorized", "the owner has not set a password for this game yet")
        if not await asyncio.to_thread(verify_password, password, row["password_hash"]):
            raise await self._fail(ip, game_id, "auth_failed_password", AuthError("unauthorized", "wrong password"))
        now = datetime.now(timezone.utc)

        def tx(c: sqlite3.Connection) -> tuple[str, str]:
            p = c.execute("SELECT player_id, auth_method, banned FROM players WHERE game_id=? AND display_name=?",
                          (game_id, name)).fetchone()
            if p is not None:
                if p["auth_method"] != "password":
                    raise AuthError("name_taken", "this name is already used by another player")
                if p["banned"]:
                    raise AuthError("banned", "player is banned")
                pid = p["player_id"]
                c.execute("UPDATE players SET last_seen_at=? WHERE game_id=? AND player_id=?", (_iso(now), game_id, pid))
            else:
                pid = new_player_id()
                c.execute("INSERT INTO players(game_id, player_id, display_name, auth_method, created_at, last_seen_at) "
                          "VALUES (?,?,?,?,?,?)", (game_id, pid, name, "password", _iso(now), _iso(now)))
            return pid, self._issue_token(c, game_id, pid, now)

        pid, token = await self.db.transaction(tx)
        self.limiter.success(ip)
        return PlayerInfo(game_id, pid, name), token

    async def login_account(self, game_id: str, username: str, password: str, register: bool, allow_register: bool,
                            ip: str) -> tuple[PlayerInfo, str]:
        self.check_blocked(ip)
        now = datetime.now(timezone.utc)
        if register:
            if not allow_register:
                raise AuthError("forbidden", "registration is disabled for this game")
            if len(password) < MIN_PASSWORD:
                raise AuthError("bad_request", f"password must be at least {MIN_PASSWORD} characters")
            h = await asyncio.to_thread(hash_password, password)

            def tx(c: sqlite3.Connection) -> tuple[str, str]:
                if c.execute("SELECT 1 FROM players WHERE game_id=? AND display_name=?", (game_id, username)).fetchone():
                    raise AuthError("name_taken", "this name is already taken")
                pid = new_player_id()
                c.execute("INSERT INTO players(game_id, player_id, display_name, auth_method, password_hash, created_at, "
                          "last_seen_at) VALUES (?,?,?,?,?,?,?)", (game_id, pid, username, "account", h, _iso(now), _iso(now)))
                return pid, self._issue_token(c, game_id, pid, now)

            pid, token = await self.db.transaction(tx)
            return PlayerInfo(game_id, pid, username), token

        row = await self.db.fetchone(
            "SELECT player_id, password_hash, banned FROM players WHERE game_id=? AND display_name=? AND auth_method='account'",
            (game_id, username))
        # verify even for unknown users so timing does not reveal which names exist
        ok = await asyncio.to_thread(verify_password, password, row["password_hash"] if row else _DUMMY_HASH)
        if row is None or not ok:
            raise await self._fail(ip, game_id, "auth_failed_account", AuthError("unauthorized", "wrong name or password"))
        if row["banned"]:
            raise AuthError("banned", "player is banned")
        pid = row["player_id"]

        def tx2(c: sqlite3.Connection) -> str:
            c.execute("UPDATE players SET last_seen_at=? WHERE game_id=? AND player_id=?", (_iso(now), game_id, pid))
            return self._issue_token(c, game_id, pid, now)

        token = await self.db.transaction(tx2)
        self.limiter.success(ip)
        return PlayerInfo(game_id, pid, username), token

    # -- login ----------------------------------------------------------------------
    def check_blocked(self, ip: str) -> None:
        left = self.limiter.blocked_for(ip)
        if left > 0:
            raise AuthError("rate_limited", f"too many failed attempts, try again in {int(left) + 1} s")

    async def _fail(self, ip: str, game_id: str, kind: str, err: AuthError) -> AuthError:
        just_blocked = self.limiter.failure(ip)
        await self.journal.record(kind, game_id=game_id, ip=ip, reason=err.code)
        if just_blocked:
            await self.journal.record("ip_blocked", "high", game_id=game_id, ip=ip)
        return err

    async def login_open(self, game_id: str, name: str) -> PlayerInfo:
        """Open mode (LAN/local testing only): the name is the identity."""
        row = await self.db.fetchone(
            "SELECT player_id, display_name, banned, auth_method FROM players WHERE game_id=? AND display_name=?",
            (game_id, name),
        )
        if row:
            if row["banned"]:
                raise AuthError("banned", "player is banned")
            if row["auth_method"] != "open":
                raise AuthError("name_taken", "this name is already used by another player")
            pid = row["player_id"]
            await self.db.execute(
                "UPDATE players SET last_seen_at=? WHERE game_id=? AND player_id=?", (utcnow(), game_id, pid)
            )
            return PlayerInfo(game_id, pid, name)
        pid = new_player_id()
        await self.db.execute(
            "INSERT INTO players(game_id, player_id, display_name, auth_method, created_at, last_seen_at) VALUES (?,?,?,?,?,?)",
            (game_id, pid, name, "open", utcnow(), utcnow()),
        )
        return PlayerInfo(game_id, pid, name)

    async def redeem_invite(self, game_id: str, code: str, name: str, ip: str) -> tuple[PlayerInfo, str]:
        self.check_blocked(ip)
        code_hash = sha256(normalize_invite(code))
        pid = new_player_id()
        now = datetime.now(timezone.utc)

        def tx(c: sqlite3.Connection) -> str:
            inv = c.execute(
                "SELECT max_uses, uses, expires_at, revoked FROM invites WHERE code_hash=? AND game_id=?",
                (code_hash, game_id),
            ).fetchone()
            if inv is None or inv["revoked"]:
                raise AuthError("unauthorized", "invalid invite code")
            if datetime.fromisoformat(inv["expires_at"]) <= now:
                raise AuthError("unauthorized", "invite code has expired")
            if inv["uses"] >= inv["max_uses"]:
                raise AuthError("unauthorized", "invite code has already been used")
            if c.execute(
                "SELECT 1 FROM players WHERE game_id=? AND display_name=?", (game_id, name)
            ).fetchone():
                raise AuthError("name_taken", "this name is already used by another player")
            c.execute("UPDATE invites SET uses = uses + 1 WHERE code_hash=?", (code_hash,))
            c.execute(
                "INSERT INTO players(game_id, player_id, display_name, auth_method, created_at, last_seen_at) VALUES (?,?,?,?,?,?)",
                (game_id, pid, name, "invite", _iso(now), _iso(now)),
            )
            return self._issue_token(c, game_id, pid, now)

        try:
            token = await self.db.transaction(tx)
        except AuthError as err:
            if err.code == "name_taken":
                raise  # not a guessing attempt
            raise await self._fail(ip, game_id, "auth_failed_invite", err)
        self.limiter.success(ip)
        return PlayerInfo(game_id, pid, name), token

    async def login_token(self, game_id: str, token: str, ip: str) -> PlayerInfo:
        self.check_blocked(ip)
        now = datetime.now(timezone.utc)
        row = await self.db.fetchone(
            """SELECT t.player_id, t.expires_at, t.revoked, p.display_name, p.banned
               FROM player_tokens t JOIN players p ON p.game_id=t.game_id AND p.player_id=t.player_id
               WHERE t.token_hash=? AND t.game_id=?""",
            (sha256(token), game_id),
        )
        if row is None or row["revoked"] or datetime.fromisoformat(row["expires_at"]) <= now:
            raise await self._fail(ip, game_id, "auth_failed_token", AuthError("unauthorized", "invalid or expired token"))
        if row["banned"]:
            raise AuthError("banned", "player is banned")
        # sliding expiry
        await self.db.execute(
            "UPDATE player_tokens SET last_used_at=?, expires_at=? WHERE token_hash=?",
            (_iso(now), _iso(now + PLAYER_TOKEN_TTL), sha256(token)),
        )
        await self.db.execute(
            "UPDATE players SET last_seen_at=? WHERE game_id=? AND player_id=?", (_iso(now), game_id, row["player_id"])
        )
        self.limiter.success(ip)
        return PlayerInfo(game_id, row["player_id"], row["display_name"])


_DUMMY_HASH = hash_password("dummy-password-for-timing")
