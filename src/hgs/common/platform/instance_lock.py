"""Single-instance lock: prevents two HGS cores from using the same data directory."""

from __future__ import annotations

import os
import sys
from pathlib import Path


class InstanceLockError(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.pid_file = path.with_name("core.pid")
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise InstanceLockError(
                f"Another HGS instance is already using {self.path.parent}"
            ) from exc
        self._fh = fh
        # the lock file itself is locked on Windows, so the owner's PID goes to a separate file
        try:
            self.pid_file.write_text(str(os.getpid()), encoding="ascii")
        except OSError:
            pass

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self.pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                try:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


def stop_stale_core(data_dir: Path) -> str:
    """Terminate a leftover HGS core that still holds `data_dir` (only an HGS core process, nothing else)."""
    import psutil

    pid_file = data_dir / "core.pid"
    try:
        pid = int(pid_file.read_text(encoding="ascii").strip())
        proc = psutil.Process(pid)
        cmd = " ".join(proc.cmdline())
    except (OSError, ValueError, psutil.Error):
        return "no running core found"
    if "--core" not in cmd:
        return "the recorded process is not an HGS core; nothing was stopped"
    proc.terminate()
    try:
        proc.wait(10)
    except psutil.TimeoutExpired:
        for child in proc.children(recursive=True):
            child.kill()
        proc.kill()
    return f"stopped old core (PID {pid})"
