"""Entry point of the core process (`hgs --core`).

The GUI starts the core as a child process and writes the admin token as the first
line of stdin (not a command-line argument: those are visible in Task Manager).
When stdin closes (the GUI exited or crashed) the core shuts down gracefully.
The core reports readiness as one JSON line on stdout.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import signal
import sys
import threading
from pathlib import Path

from hgs.common.platform import InstanceLockError
from hgs.core.app import CoreApp, CoreStartError
from hgs.core.config.settings import SettingsError


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


async def _main(data_dir: Path, token: str, watch_stdin: bool) -> int:
    core = CoreApp(data_dir, token)
    try:
        await core.start()
    except (CoreStartError, InstanceLockError, SettingsError) as exc:
        _emit({"event": "error", "message": str(exc)})
        return 2
    loop = asyncio.get_running_loop()

    def _signal(*_: object) -> None:
        loop.call_soon_threadsafe(core.request_shutdown, "signal")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _signal)
        except (ValueError, OSError):
            pass
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal)

    if watch_stdin:
        def _watch() -> None:
            try:
                while sys.stdin.readline():
                    pass
            except Exception:
                pass
            loop.call_soon_threadsafe(core.request_shutdown, "control pipe closed")

        threading.Thread(target=_watch, name="stdin-watch", daemon=True).start()

    _emit({"event": "ready", "admin_port": core.admin_port, "gateway_port": core.gateway_port})
    await core.wait_for_shutdown()
    await core.stop()
    _emit({"event": "stopped"})
    return 0


def run_core(data_dir: Path, token_from_stdin: bool) -> int:
    if token_from_stdin:
        token = sys.stdin.readline().strip()
        if len(token) < 32:
            _emit({"event": "error", "message": "admin token missing"})
            return 2
    else:
        token = secrets.token_urlsafe(32)
        print(f"Admin token for this run: {token}", file=sys.stderr)
    return asyncio.run(_main(data_dir, token, watch_stdin=token_from_stdin))
