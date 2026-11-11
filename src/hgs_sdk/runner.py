"""Starts a game logic class as an HGS worker.

    python -m hgs_sdk.runner --root <game folder> --entry server/logic.py:Game

The core sets HGS_WORKER_URL, HGS_WORKER_TOKEN and HGS_GAME_ID. The worker exits by
itself when its connection to the core closes, so it never outlives HGS.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

from .connection import Connection
from .logic import GameLogic


def load_logic(root: str, entry: str) -> GameLogic:
    if ":" in entry:
        file_part, class_name = entry.rsplit(":", 1)
    else:
        file_part, class_name = entry, "Game"
    root_path = Path(root).resolve()
    path = (root_path / file_part).resolve()
    try:
        path.relative_to(root_path)
    except ValueError:
        raise SystemExit(f"hgs_sdk: entry {file_part} is outside the game folder")
    if not path.is_file():
        raise SystemExit(f"hgs_sdk: entry file not found: {path}")
    sys.path.insert(0, str(root_path))  # the game may import its own modules
    spec = importlib.util.spec_from_file_location("hgs_game_logic", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hgs_game_logic"] = module
    spec.loader.exec_module(module)
    cls = getattr(module, class_name, None)
    if not (isinstance(cls, type) and issubclass(cls, GameLogic)):
        raise SystemExit(f"hgs_sdk: {class_name} in {file_part} must be a subclass of hgs_sdk.GameLogic")
    return cls()


def run_worker(root: str | None, entry: str | None) -> int:
    url = os.environ.get("HGS_WORKER_URL")
    token = os.environ.get("HGS_WORKER_TOKEN")
    game_id = os.environ.get("HGS_GAME_ID")
    if not (url and token and game_id and root and entry):
        print("hgs_sdk: this program is started by HGS, not directly", file=sys.stderr)
        return 2
    os.environ.pop("HGS_WORKER_TOKEN", None)  # the game code does not need the token
    logic = load_logic(root, entry)
    return asyncio.run(Connection(logic, url, token, game_id).run())


def main() -> int:
    p = argparse.ArgumentParser(prog="hgs_sdk.runner")
    p.add_argument("--root", required=True)
    p.add_argument("--entry", required=True)
    a = p.parse_args()
    return run_worker(a.root, a.entry)


if __name__ == "__main__":
    sys.exit(main())
