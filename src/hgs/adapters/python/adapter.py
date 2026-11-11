"""Python adapter — Python games and separate Python server programs.

backend:
  mode:   "worker"     — entry "server/logic.py:Game", a class based on hgs_sdk.GameLogic
          "standalone" — entry "server/main.py", an existing server with its own port
  python: "auto"   — the game's virtualenv (.venv / venv / env in the game folder) if present,
                     otherwise the Python HGS runs on (development) or the system Python
          "venv"   — require the game's virtualenv
          "system" — python from PATH (must be approved by the owner)
          "<path>" — a specific python.exe (must be approved by the owner)

hgs_sdk uses only the standard library, so HGS makes it importable in ANY environment by
putting a fresh copy on PYTHONPATH — the game does not need to install anything.
Code sent by remote players is never executed: only the entry file chosen by the owner runs.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import hgs_sdk
from hgs.adapters.base import AdapterError
from hgs.adapters.process.adapter import ProcessAdapter

VENV_DIRS = (".venv", "venv", "env")


def venv_python(root: Path) -> Path | None:
    for d in VENV_DIRS:
        exe = root / d / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        if exe.is_file():
            return exe
    return None


def system_python() -> str | None:
    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if found and "WindowsApps" not in found:  # skip the Microsoft Store stub
            return found
    return None


class PythonAdapter(ProcessAdapter):
    adapter_type = "python"
    description = "Python games and Python server programs (own virtualenv supported)"
    EXTRA_FIELDS = {"entry", "python"}

    @classmethod
    def validate_specific(cls, backend: dict[str, Any], errors: list[str]) -> None:
        entry = backend.get("entry")
        if not isinstance(entry, str) or not entry:
            errors.append("backend.entry is required (e.g. 'server/main.py' or 'server/logic.py:Game')")
            return
        file_part = entry.split(":", 1)[0] if backend.get("mode") == "worker" else entry
        if not file_part.endswith(".py") or Path(file_part).is_absolute() or ".." in Path(file_part).parts:
            errors.append("backend.entry must be a relative path to a .py file inside the game folder")
        if backend.get("mode") == "standalone" and ":" in entry:
            errors.append("standalone entry is a file path (no ':Class')")
        py = backend.get("python", "auto")
        if not isinstance(py, str) or not py:
            errors.append("backend.python must be 'auto', 'venv', 'system' or a path to python")

    def interpreter(self) -> str:
        choice = self.backend_cfg.get("python", "auto")
        root = self.ctx.game_root
        if choice in ("auto", "venv"):
            exe = venv_python(root)
            if exe is not None:
                return str(exe)
            if choice == "venv":
                raise AdapterError("no virtual environment (.venv, venv or env) found in the game folder")
            if not getattr(sys, "frozen", False):
                return sys.executable
            choice = "system"
        if choice == "system":
            found = system_python()
            if found is None:
                raise AdapterError("Python is not installed or not in PATH")
            return str(Path(found).resolve())
        return str(Path(choice).expanduser())

    def resolve_program(self) -> tuple[str, list[str], Path | None]:
        python = self.interpreter()
        entry = self.backend_cfg["entry"]
        root = self.ctx.game_root
        if self.mode == "worker":
            file_part = entry.split(":", 1)[0]
            # -s: ignore the user site-packages; PYTHONSAFEPATH (env) keeps the cwd off sys.path
            return python, ["-s", "-m", "hgs_sdk.runner", "--root", str(root), "--entry", entry], (root / file_part).resolve()
        return python, ["-s", str((root / entry).resolve())], (root / entry).resolve()

    def extra_env(self) -> dict[str, str]:
        if self.mode != "worker":
            return {}
        # a fresh copy of hgs_sdk in the game's runtime folder, importable from any environment
        target = self.ctx.paths.runtime / "sdk"
        src = Path(hgs_sdk.__file__).resolve().parent
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(src, target / "hgs_sdk", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return {"PYTHONPATH": str(target), "PYTHONSAFEPATH": "1"}
