"""Process Supervisor: starts game worker processes, ONLY from an allowlist.

* the program must be an allowed interpreter (HGS's own Python is always allowed);
* the entry file and working directory must be inside the game's registered folder;
* no shell is ever used; arguments are a list;
* stdout/stderr go to the game's log; the process tree is contained (see common.platform.process).
Network messages can never influence what is started: specs come only from the game config.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from hgs.common.platform.process import IS_WINDOWS, ProcessContainer, popen_kwargs

MAX_LINE = 4000

# Environment variables passed through to workers (everything else is dropped, so
# secrets in the core's environment do not leak into game processes).
_ENV_KEEP = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "PATHEXT", "COMSPEC", "LANG", "LC_ALL",
             "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PYTHONIOENCODING", "TZ")


class LaunchError(Exception):
    pass


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class LaunchSpec:
    program: str
    args: list[str]
    cwd: Path
    game_root: Path
    entry: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    memory_mb: int | None = None


class AllowList:
    def __init__(self) -> None:
        self._programs: set[str] = set()
        self.add(sys.executable)

    @staticmethod
    def _norm(p: str) -> str:
        return os.path.normcase(str(Path(p).resolve()))

    def add(self, program: str) -> None:
        self._programs.add(self._norm(program))

    def allows(self, program: str) -> bool:
        return self._norm(program) in self._programs

    def validate(self, spec: LaunchSpec) -> None:
        if not self.allows(spec.program):
            raise LaunchError(f"program is not in the allowlist: {spec.program}")
        if not spec.game_root.is_dir():
            raise LaunchError(f"game folder does not exist: {spec.game_root}")
        if not is_within(spec.cwd, spec.game_root):
            raise LaunchError("working directory must be inside the game folder")
        if spec.entry is not None:
            if not is_within(spec.entry, spec.game_root):
                raise LaunchError(f"entry point must be inside the game folder: {spec.entry}")
            if not spec.entry.is_file():
                raise LaunchError(f"entry point not found: {spec.entry}")


class ManagedProcess:
    def __init__(self, spec: LaunchSpec, logger: logging.Logger) -> None:
        self.spec = spec
        self.log = logger
        self.proc: asyncio.subprocess.Process | None = None
        self.container = ProcessContainer(spec.memory_mb)
        self.started_at: float | None = None
        self.stderr_tail: deque[str] = deque(maxlen=30)
        self._readers: list[asyncio.Task] = []
        self._ps: psutil.Process | None = None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode if self.proc else None

    async def start(self) -> None:
        env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_KEEP}
        env.update(self.spec.env)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            self.proc = await asyncio.create_subprocess_exec(
                self.spec.program, *self.spec.args, cwd=str(self.spec.cwd), env=env,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                **popen_kwargs(),
            )
        except OSError as exc:
            raise LaunchError(f"cannot start process: {exc}") from exc
        self.started_at = time.time()
        try:
            self.container.attach(self.proc.pid)
        except Exception as exc:
            self.log.warning("could not apply process limits: %s", exc)
        self.log.info("worker process started (pid %s, limits: %s)", self.proc.pid,
                      ", ".join(self.container.limits_applied) or "none")
        self._readers = [
            asyncio.create_task(self._read(self.proc.stdout, "stdout")),
            asyncio.create_task(self._read(self.proc.stderr, "stderr")),
        ]

    async def _read(self, stream: asyncio.StreamReader | None, name: str) -> None:
        if stream is None:
            return
        while True:
            try:
                line = await stream.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()[:MAX_LINE]
            if not text:
                continue
            if name == "stderr":
                self.stderr_tail.append(text)
                self.log.warning("[stderr] %s", text)
            else:
                self.log.info("[stdout] %s", text)

    async def wait(self) -> int:
        assert self.proc is not None
        code = await self.proc.wait()
        for r in self._readers:
            try:
                await asyncio.wait_for(r, 2)
            except (asyncio.TimeoutError, Exception):
                r.cancel()
        return code

    async def terminate(self, timeout: float = 5.0) -> None:
        """Ask the process to exit, then kill the whole tree if it does not."""
        if self.proc is None or self.proc.returncode is not None:
            self.container.close()
            return
        try:
            if IS_WINDOWS:
                self.proc.terminate()  # TerminateProcess; graceful exit happens via the worker protocol first
            else:
                os.killpg(self.proc.pid, 15)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), timeout)
        except asyncio.TimeoutError:
            self.log.warning("worker did not exit in %.0f s, killing the process tree", timeout)
            self.container.kill_tree(self.proc.pid)
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.log.error("worker process %s could not be killed", self.proc.pid)
        self.container.kill_tree(self.proc.pid)  # leftover children, if any
        self.container.close()

    def metrics(self) -> dict:
        if self.proc is None or self.proc.returncode is not None:
            return {}
        try:
            if self._ps is None or self._ps.pid != self.proc.pid:
                self._ps = psutil.Process(self.proc.pid)
                self._ps.cpu_percent(None)
            procs = [self._ps, *self._ps.children(recursive=True)]
            mem = sum(p.memory_info().rss for p in procs)
            return {"pid": self.proc.pid, "cpu_percent": self._ps.cpu_percent(None), "memory_bytes": mem,
                    "uptime_s": round(time.time() - (self.started_at or time.time()))}
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return {"pid": self.proc.pid}
