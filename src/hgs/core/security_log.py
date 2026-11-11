"""Suspicious-activity journal: security.log + security_events table + GUI alert."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from .db.database import utcnow

if TYPE_CHECKING:
    from .db.database import Database
    from .events import EventBus

log = logging.getLogger("hgs.security")


class SecurityJournal:
    def __init__(self, db: "Database", bus: "EventBus") -> None:
        self.db = db
        self.bus = bus

    async def record(
        self,
        kind: str,
        severity: str = "warning",
        *,
        game_id: str | None = None,
        ip: str | None = None,
        player_id: str | None = None,
        **details: Any,
    ) -> None:
        text = json.dumps(details, ensure_ascii=False) if details else None
        log.warning("%s game=%s ip=%s player=%s %s", kind, game_id, ip, player_id, text or "")
        self.bus.publish("security.alert", kind=kind, severity=severity, game_id=game_id, ip=ip, player_id=player_id, details=details)
        try:
            await self.db.execute(
                "INSERT INTO security_events(at, kind, severity, game_id, ip, player_id, details) VALUES (?,?,?,?,?,?,?)",
                (utcnow(), kind, severity, game_id, ip, player_id, text),
            )
        except Exception:  # journaling must never break request handling
            logging.getLogger("hgs").exception("failed to store security event")
