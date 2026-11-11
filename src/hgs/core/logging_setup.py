"""Logging: JSON Lines files with rotation, per-game logs, an in-memory tail for the GUI."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import threading
from collections import deque
from pathlib import Path
from typing import Callable

LOG_FORMAT_FIELDS = ("ts", "level", "logger", "msg")

_secrets: set[str] = set()
_secrets_lock = threading.Lock()


def register_secret(value: str) -> None:
    """Values registered here are masked in every log record."""
    if value and len(value) >= 8:
        with _secrets_lock:
            _secrets.add(value)


_TOKEN_RE = re.compile(r'("?(?:token|password|code|resume_token|player_token)"?\s*[:=]\s*"?)([^",\s}]+)', re.I)


def redact(text: str) -> str:
    with _secrets_lock:
        for s in _secrets:
            if s in text:
                text = text.replace(s, "***")
    return _TOKEN_RE.sub(r"\1***", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        if record.exc_info:
            entry["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False)


class TailBuffer(logging.Handler):
    """Keeps the last N formatted records per source and notifies a listener."""

    def __init__(self, source: str, maxlen: int = 2000) -> None:
        super().__init__()
        self.source = source
        self.lines: deque[dict] = deque(maxlen=maxlen)
        self.listener: Callable[[str, dict], None] | None = None
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = json.loads(self.format(record))
        except Exception:  # pragma: no cover - formatting must never break the app
            return
        self.lines.append(entry)
        if self.listener is not None:
            try:
                self.listener(self.source, entry)
            except Exception:
                pass

    def tail(self, n: int) -> list[dict]:
        if n <= 0:
            return []
        return list(self.lines)[-n:]


class LogManager:
    """Creates loggers and keeps tails for: core, security, game:<id>."""

    def __init__(self, logs_dir: Path, max_bytes: int = 10 * 1024 * 1024, backups: int = 5) -> None:
        self.logs_dir = logs_dir
        self.max_bytes = max_bytes
        self.backups = backups
        self.tails: dict[str, TailBuffer] = {}
        self._listener: Callable[[str, dict], None] | None = None
        self._configured: set[str] = set()

    def set_listener(self, listener: Callable[[str, dict], None] | None) -> None:
        self._listener = listener
        for t in self.tails.values():
            t.listener = listener

    def _file_handler(self, path: Path) -> logging.Handler:
        path.parent.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(
            path, maxBytes=self.max_bytes, backupCount=self.backups, encoding="utf-8"
        )
        h.setFormatter(JsonFormatter())
        return h

    def _setup(self, source: str, logger_name: str, file: Path, propagate: bool) -> logging.Logger:
        logger = logging.getLogger(logger_name)
        if source in self._configured:
            return logger
        logger.setLevel(logging.INFO)
        logger.propagate = propagate
        logger.addHandler(self._file_handler(file))
        tail = TailBuffer(source)
        tail.listener = self._listener
        logger.addHandler(tail)
        self.tails[source] = tail
        self._configured.add(source)
        return logger

    def setup_core(self) -> logging.Logger:
        core = self._setup("core", "hgs", self.logs_dir / "core.log", propagate=False)
        self._setup("security", "hgs.security", self.logs_dir / "security.log", propagate=True)
        # uvicorn errors go to the core log too
        for name in ("uvicorn.error",):
            lg = logging.getLogger(name)
            lg.handlers = []
            lg.propagate = False
            for h in core.handlers:
                lg.addHandler(h)
        logging.getLogger("uvicorn.access").disabled = True
        return core

    def game_logger(self, game_id: str, logs_dir: Path) -> logging.Logger:
        # Separate hierarchy so game records do not end up in core.log.
        return self._setup(f"game:{game_id}", f"hgsgame.{game_id}", logs_dir / "server.log", propagate=False)

    def tail(self, source: str, n: int) -> list[dict]:
        t = self.tails.get(source)
        return t.tail(n) if t else []

    def close(self) -> None:
        for source in list(self._configured):
            name = "hgs" if source == "core" else "hgs.security" if source == "security" else "hgsgame." + source[5:]
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)
        self._configured.clear()
        self.tails.clear()
