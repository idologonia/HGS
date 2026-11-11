"""HGS entry point.

  hgs                 -> desktop GUI (starts the core automatically)
  hgs --core          -> server core only (used by the GUI; prints a ready line)
  hgs --worker ...    -> game logic worker (started by the core, not by users)
"""

from __future__ import annotations

import argparse
import multiprocessing
import sys
from pathlib import Path

from hgs.common.paths import default_data_dir


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(prog="hgs", description="HOME GAME SERVER")
    parser.add_argument("--core", action="store_true", help="run the server core without GUI")
    parser.add_argument("--token-stdin", action="store_true", help="read the admin token from stdin (used by the GUI)")
    parser.add_argument("--data-dir", type=Path, default=None, help="data directory (default: %%LOCALAPPDATA%%\\HGS)")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)  # game worker (started by the core)
    parser.add_argument("--root", help=argparse.SUPPRESS)
    parser.add_argument("--entry", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker:
        from hgs_sdk.runner import run_worker

        return run_worker(args.root, args.entry)
    data_dir = args.data_dir or default_data_dir()

    if args.core:
        from hgs.core.runner import run_core

        return run_core(data_dir, args.token_stdin)

    from hgs.gui.app import run_gui

    return run_gui(data_dir)


if __name__ == "__main__":
    sys.exit(main())
