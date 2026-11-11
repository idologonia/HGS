"""Save Manager: per-game snapshots, written atomically, rotated, journaled.

The format of the data belongs to the game: JSON is stored as .json, bytes as .bin.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from hgs.common.fileutil import atomic_write_bytes
from hgs.common.paths import DataPaths
from hgs.core.db.database import Database, utcnow

log = logging.getLogger("hgs.saves")


class SaveManager:
    def __init__(self, db: Database, paths: DataPaths) -> None:
        self.db = db
        self.paths = paths

    async def write_snapshot(self, game_id: str, reason: str, fmt: str, data: Any, keep: int) -> Path | None:
        saves = self.paths.game(game_id).saves
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        try:
            if fmt == "json":
                raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
                path = saves / f"{stamp}_{reason}.json"
            elif fmt == "b64":
                raw = base64.b64decode(data, validate=True)
                path = saves / f"{stamp}_{reason}.bin"
            else:
                raise ValueError(f"unknown save format {fmt!r}")
            atomic_write_bytes(path, raw)
        except Exception as exc:
            log.error("save of %s failed: %s", game_id, exc)
            await self._journal(game_id, reason, None, None, None, False, str(exc))
            return None
        await self._journal(game_id, reason, str(path), len(raw), hashlib.sha256(raw).hexdigest(), True, None)
        self._rotate(saves, keep)
        return path

    async def _journal(self, game_id, reason, path, size, sha, ok, error) -> None:
        await self.db.execute(
            "INSERT INTO saves(game_id, kind, reason, path, size_bytes, sha256, ok, error, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (game_id, "snapshot", reason, path, size, sha, int(ok), error, utcnow()),
        )

    @staticmethod
    def _snapshots(saves: Path) -> list[Path]:
        if not saves.is_dir():
            return []
        return sorted(p for p in saves.iterdir() if p.suffix in (".json", ".bin") and p.is_file())

    def _rotate(self, saves: Path, keep: int) -> None:
        files = self._snapshots(saves)
        for old in files[:-keep] if keep > 0 else []:
            try:
                old.unlink()
            except OSError:
                pass

    def latest(self, game_id: str) -> tuple[str, Any] | None:
        """Newest readable snapshot as (format, data); corrupt files are skipped."""
        for path in reversed(self._snapshots(self.paths.game(game_id).saves)):
            try:
                raw = path.read_bytes()
                if path.suffix == ".json":
                    return "json", json.loads(raw)
                return "b64", base64.b64encode(raw).decode()
            except Exception as exc:
                log.warning("skipping unreadable snapshot %s: %s", path, exc)
        return None

    async def history(self, game_id: str, limit: int = 50) -> list[dict]:
        return await self.db.fetchall(
            "SELECT reason, path, size_bytes, ok, error, created_at FROM saves WHERE game_id=? ORDER BY id DESC LIMIT ?",
            (game_id, limit),
        )
