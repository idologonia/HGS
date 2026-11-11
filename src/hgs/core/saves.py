"""Save Manager: per-game snapshots, written atomically, rotated, journaled.

The format of the data belongs to the game: JSON is stored as .json, bytes as .bin.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import shutil
import time
import zipfile
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


    # == listing ======================================================================
    def list_snapshots(self, game_id: str) -> list[dict]:
        out = []
        for p in reversed(self._snapshots(self.paths.game(game_id).saves)):
            st = p.stat()
            out.append({"file": p.name, "size_bytes": st.st_size, "modified": st.st_mtime})
        return out

    def list_backups(self, game_id: str) -> list[dict]:
        folder = self.paths.game(game_id).backups
        if not folder.is_dir():
            return []
        return [{"file": p.name, "size_bytes": p.stat().st_size, "modified": p.stat().st_mtime}
                for p in sorted(folder.glob("*.zip"), reverse=True)]

    @staticmethod
    def _safe_name(name: str) -> str:
        if not name or "/" in name or "\\" in name or name.startswith(".") or ".." in name:
            raise ValueError("invalid file name")
        return name

    # == backups ======================================================================
    def make_backup(self, game_id: str, reason: str, keep: int = 10) -> Path:
        """Zip the game's HGS data (snapshots, game data written by the game, config) — not the backups."""
        gp = self.paths.game(game_id)
        gp.backups.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        target = gp.backups / f"{stamp}_{reason}.zip"
        tmp = target.with_suffix(".zip.part")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for path in gp.root.rglob("*"):
                rel = path.relative_to(gp.root)
                if not path.is_file() or rel.parts[0] in ("backups", "runtime", "logs"):
                    continue
                z.write(path, rel.as_posix())
        tmp.replace(target)
        backups = sorted(gp.backups.glob("*.zip"))
        for old in backups[:-keep] if keep > 0 else []:
            old.unlink(missing_ok=True)
        log.info("backup of %s created: %s", game_id, target.name)
        return target

    async def backup(self, game_id: str, reason: str) -> dict:
        import asyncio

        try:
            path = await asyncio.to_thread(self.make_backup, game_id, reason)
        except Exception as exc:
            await self.db.execute(
                "INSERT INTO saves(game_id, kind, reason, ok, error, created_at) VALUES (?,?,?,?,?,?)",
                (game_id, "backup", reason, 0, str(exc), utcnow()))
            raise
        data = path.read_bytes()
        await self.db.execute(
            "INSERT INTO saves(game_id, kind, reason, path, size_bytes, sha256, ok, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (game_id, "backup", reason, str(path), len(data), hashlib.sha256(data).hexdigest(), 1, utcnow()))
        return {"file": path.name, "size_bytes": len(data)}

    # == restore ======================================================================
    async def restore_snapshot(self, game_id: str, file: str) -> dict:
        """Make the chosen snapshot the newest one; it is loaded on the next start.
        The current state is backed up first, so a restore can be undone."""
        gp = self.paths.game(game_id)
        src = gp.saves / self._safe_name(file)
        if not src.is_file():
            raise ValueError("snapshot not found")
        await self.backup(game_id, "before_restore")
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        dest = gp.saves / f"{stamp}_restore{src.suffix}"
        atomic_write_bytes(dest, src.read_bytes())
        await self._journal(game_id, "restore", str(dest), dest.stat().st_size, None, True, None)
        return {"file": dest.name}

    async def restore_backup(self, game_id: str, file: str) -> dict:
        import asyncio

        gp = self.paths.game(game_id)
        src = gp.backups / self._safe_name(file)
        if not src.is_file():
            raise ValueError("backup not found")
        await self.backup(game_id, "before_restore")

        def _extract() -> None:
            with zipfile.ZipFile(src) as z:
                names = z.namelist()
                for n in names:  # refuse archives that try to escape the folder
                    target = (gp.root / n).resolve()
                    if not str(target).startswith(str(gp.root.resolve())) or n.split("/")[0] in ("backups", "runtime"):
                        raise ValueError(f"unsafe path in backup: {n}")
                shutil.rmtree(gp.saves, ignore_errors=True)
                gp.saves.mkdir(parents=True, exist_ok=True)
                for n in names:
                    if n != "game.json":  # the configuration is not rolled back, only the data
                        z.extract(n, gp.root)

        await asyncio.to_thread(_extract)
        await self._journal(game_id, "restore_backup", str(src), src.stat().st_size, None, True, None)
        return {"file": file}
