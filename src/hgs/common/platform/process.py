"""Process containment for game workers.

Windows: every worker is put into a Job Object with KILL_ON_JOB_CLOSE (if HGS dies,
Windows kills its game processes — no orphans) and an optional memory limit.
Other systems (development): the worker gets its own process group so the whole
tree can be stopped; the worker also exits by itself when its link to the core closes.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from typing import Any

log = logging.getLogger("hgs.process")

IS_WINDOWS = sys.platform == "win32"


def popen_kwargs() -> dict[str, Any]:
    """Extra arguments for asyncio.create_subprocess_exec."""
    if IS_WINDOWS:
        # no console window pops up; own process group
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class ProcessContainer:
    """Holds one worker process tree and enforces limits where the OS allows it."""

    def __init__(self, memory_mb: int | None = None) -> None:
        self.memory_mb = memory_mb
        self._job = None
        self.limits_applied: list[str] = []

    def attach(self, pid: int) -> None:
        if IS_WINDOWS:
            self._attach_windows(pid)
        self._lower_priority(pid)

    def _attach_windows(self, pid: int) -> None:
        try:
            import win32api
            import win32con
            import win32job
        except ImportError:  # pragma: no cover - pywin32 is a dependency on Windows
            log.warning("pywin32 is not installed: worker limits and kill-on-exit are not available")
            return
        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        flags = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if self.memory_mb:
            flags |= win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info["ProcessMemoryLimit"] = self.memory_mb * 1024 * 1024
        info["BasicLimitInformation"]["LimitFlags"] |= flags
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            win32api.CloseHandle(handle)
        self._job = job  # keep the handle alive: closing it kills the worker tree
        self.limits_applied.append("job_object")
        if self.memory_mb:
            self.limits_applied.append(f"memory<={self.memory_mb}MB")

    def _lower_priority(self, pid: int) -> None:
        """Game servers run below normal priority so the PC stays responsive."""
        try:
            import psutil

            p = psutil.Process(pid)
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if IS_WINDOWS else 5)
            self.limits_applied.append("below_normal_priority")
        except Exception as exc:
            log.debug("cannot lower priority of %s: %s", pid, exc)

    def kill_tree(self, pid: int) -> None:
        if IS_WINDOWS and self._job is not None:
            import win32job

            try:
                win32job.TerminateJobObject(self._job, 1)
                return
            except Exception as exc:
                log.warning("TerminateJobObject failed: %s", exc)
        if not IS_WINDOWS:
            try:
                os.killpg(pid, signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError):
                pass
        try:
            import psutil

            parent = psutil.Process(pid)
            for child in parent.children(recursive=True):
                child.kill()
            parent.kill()
        except Exception:
            pass

    def close(self) -> None:
        if self._job is not None:
            try:
                import win32api

                win32api.CloseHandle(self._job)
            except Exception:
                pass
            self._job = None
