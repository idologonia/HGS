"""Where HGS keeps its data.

Windows: %LOCALAPPDATA%\\HGS. Other systems (development): ~/.local/share/hgs.
The HGS_DATA_DIR environment variable or --data-dir overrides it (portable mode, tests).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def default_data_dir() -> Path:
    override = os.environ.get("HGS_DATA_DIR")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        # portable mode: a file named portable.txt next to HGS.exe keeps all data in .\data
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "portable.txt").exists():
            return exe_dir / "data"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "HGS"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "hgs"


@dataclass(frozen=True)
class DataPaths:
    root: Path

    @property
    def db(self) -> Path:
        return self.root / "hgs.db"

    @property
    def settings(self) -> Path:
        return self.root / "settings.json"

    @property
    def lock(self) -> Path:
        return self.root / "hgs.lock"

    @property
    def run_state(self) -> Path:
        return self.root / "run_state.json"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def games(self) -> Path:
        return self.root / "games"

    def game(self, game_id: str) -> "GamePaths":
        return GamePaths(self.games / game_id)

    def ensure(self) -> None:
        for p in (self.root, self.logs, self.games):
            p.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class GamePaths:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / "game.json"

    @property
    def saves(self) -> Path:
        return self.root / "saves"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    def ensure(self) -> None:
        for p in (self.root, self.saves, self.backups, self.logs, self.runtime):
            p.mkdir(parents=True, exist_ok=True)
