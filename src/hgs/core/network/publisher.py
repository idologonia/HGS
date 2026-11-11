"""Address publisher: keeps the current public server address in a file on GitHub, so games on
GitHub Pages find the server by themselves (no links for players).

The file (default hgs-server.json in a PUBLIC repository of the owner) looks like:
    {"server": "wss://xxx.trycloudflare.com", "online": true, "updated": 1700000000, "name": "My HGS"}
The address is not a secret: players still have to log in. Only a GitHub token with write access
to that one repository is needed; it is stored encrypted.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hgs.core.app import CoreApp

log = logging.getLogger("hgs.publisher")

API = "https://api.github.com"  # tests point this at a local fake
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
PATH_RE = re.compile(r"^[A-Za-z0-9_./-]{1,200}\.json$")


class PublishError(RuntimeError):
    pass


def _request(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, Any]:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "User-Agent": "HGS-address-publisher", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"{}").get("message", "")
        except Exception:
            detail = ""
        return exc.code, {"message": detail}


class AddressPublisher:
    def __init__(self, core: "CoreApp") -> None:
        self.core = core
        self.status: str | None = None
        self.error: str | None = None
        self.last_published: dict | None = None
        self._lock = asyncio.Lock()
        self._pending: asyncio.Task | None = None
        self._sha: dict[str, str] = {}  # last known version of the file (GitHub's GET can be cached)

    @property
    def cfg(self):
        return self.core.settings.network.publish

    def view(self) -> dict:
        return {"enabled": self.cfg.enabled, "repo": self.cfg.repo, "path": self.cfg.path, "status": self.status,
                "error": self.error, "last": self.last_published,
                "read_url": f"https://api.github.com/repos/{self.cfg.repo}/contents/{self.cfg.path}" if self.cfg.repo else None}

    def schedule(self) -> None:
        """Publish the current state soon (debounced; never blocks the caller)."""
        if not self.cfg.enabled or not self.cfg.repo:
            return
        if self._pending and not self._pending.done():
            self._pending.cancel()
        self._pending = asyncio.get_running_loop().create_task(self._delayed())

    async def _delayed(self) -> None:
        await asyncio.sleep(0.5)
        try:
            await self.publish()
        except PublishError:
            pass

    def _document(self) -> dict:
        url = self.core.network.player_url()
        exposed = self.core.settings.internet_exposed
        return {"server": url if exposed else None, "online": bool(url and exposed),
                "updated": int(time.time()), "name": self.core.settings.server_name}

    async def publish(self, doc: dict | None = None) -> dict:
        if self._pending and not self._pending.done() and asyncio.current_task() is not self._pending:
            self._pending.cancel()  # an explicit publish replaces the scheduled one
        async with self._lock:
            cfg = self.cfg
            if not cfg.repo or not REPO_RE.match(cfg.repo) or not PATH_RE.match(cfg.path):
                raise self._fail("вкажіть репозиторій у форматі owner/name і файл *.json")
            token = await self.core.secrets.get("github_token")
            if not token:
                raise self._fail("не вказано токен GitHub")
            doc = doc or self._document()
            try:
                result = await asyncio.to_thread(self._put, cfg.repo, cfg.path, token, doc, self._sha)
            except PublishError as exc:
                raise self._fail(str(exc))
            self.error = None
            self.status = "опубліковано: " + ("онлайн " + doc["server"] if doc["online"] else "офлайн")
            self.last_published = {**doc, "commit": result}
            log.info("server address published to %s/%s (%s)", cfg.repo, cfg.path, "online" if doc["online"] else "offline")
            self.core.bus.publish("network.changed", publish=self.view())
            return doc

    def _fail(self, message: str) -> PublishError:
        self.error = message
        self.status = "помилка публікації"
        log.warning("address publishing failed: %s", message)
        try:
            self.core.bus.publish("network.changed", publish=self.view())
        except RuntimeError:
            pass
        return PublishError(message)

    @staticmethod
    def _put(repo: str, path: str, token: str, doc: dict, known: dict[str, str]) -> str | None:
        url = f"{API}/repos/{repo}/contents/{path}"
        key = f"{repo}/{path}"
        body = {"message": f"HGS: server {'online' if doc['online'] else 'offline'}",
                "content": base64.b64encode(json.dumps(doc, indent=2).encode()).decode()}
        sha = known.get(key)
        for attempt in range(4):
            if sha is None or attempt > 0:
                # ask for the current version; the query string avoids a cached answer
                code, current = _request("GET", f"{url}?nocache={time.time_ns()}", token)
                if code not in (200, 404):
                    raise PublishError(_explain(code, current))
                sha = current.get("sha") if code == 200 and isinstance(current, dict) else None
            if sha:
                body["sha"] = sha
            else:
                body.pop("sha", None)
            code, res = _request("PUT", url, token, body)
            if code in (200, 201):
                new_sha = ((res or {}).get("content") or {}).get("sha") if isinstance(res, dict) else None
                if new_sha:
                    known[key] = new_sha
                else:
                    known.pop(key, None)
                return (res.get("commit") or {}).get("sha") if isinstance(res, dict) else None
            if code not in (409, 422):  # 409/422 = the file changed in between: read it again and retry
                raise PublishError(_explain(code, res))
            known.pop(key, None)
            time.sleep(1.0 + attempt)
        raise PublishError(_explain(code, res))


def _explain(code: int, res: Any) -> str:
    msg = res.get("message", "") if isinstance(res, dict) else ""
    hints = {401: "токен недійсний або прострочений", 403: "токену бракує дозволу Contents: Read and write",
             404: "репозиторій не знайдено (перевірте назву і доступ токена до нього)",
             409: "конфлікт версій файлу, спробуйте ще раз", 422: "GitHub відхилив запит"}
    return f"GitHub {code}: {hints.get(code, msg or 'помилка')}"
