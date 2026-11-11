"""A tiny local imitation of the GitHub "contents" API (GET/PUT a file), for tests."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeGitHub:
    def __init__(self, token: str = "ghp_test") -> None:
        self.token = token
        self.files: dict[str, bytes] = {}  # "owner/repo/path" -> content
        self.puts = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _key(self) -> str | None:
                p = self.path.split("?")[0]
                if not p.startswith("/repos/") or "/contents/" not in p:
                    return None
                repo, path = p[len("/repos/"):].split("/contents/", 1)
                return f"{repo}/{path}"

            def do_GET(self):  # noqa: N802
                key = self._key()
                if key not in fake.files:
                    return self._send(404, b'{"message":"Not Found"}')
                data = fake.files[key]
                if "raw" in (self.headers.get("Accept") or ""):
                    return self._send(200, data)
                sha = hashlib.sha1(data).hexdigest()
                self._send(200, json.dumps({"sha": sha, "content": base64.b64encode(data).decode()}).encode())

            def do_PUT(self):  # noqa: N802
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    return self._send(401, b'{"message":"Bad credentials"}')
                key = self._key()
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if key in fake.files and body.get("sha") != hashlib.sha1(fake.files[key]).hexdigest():
                    return self._send(409, b'{"message":"sha mismatch"}')
                fake.files[key] = base64.b64decode(body["content"])
                fake.puts += 1
                self._send(200, json.dumps({"commit": {"sha": "abc"}}).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def doc(self, key: str) -> dict | None:
        return json.loads(self.files[key]) if key in self.files else None

    def close(self) -> None:
        self.server.shutdown()
