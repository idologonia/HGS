"""Non-blocking Admin API client for the GUI (Qt network stack, no extra threads)."""

from __future__ import annotations

import json
from typing import Any, Callable

from PySide6.QtCore import QByteArray, QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkReply, QNetworkRequest
from PySide6.QtWebSockets import QWebSocket

Callback = Callable[[bool, Any], None]


class CoreClient(QObject):
    event = Signal(dict)
    connected_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.port = 0
        self.token = ""
        self._nam = QNetworkAccessManager(self)
        self._nam.setProxy(QNetworkProxy(QNetworkProxy.NoProxy))  # never send localhost traffic to a proxy
        self._ws: QWebSocket | None = None
        self._reconnect = QTimer(self)
        self._reconnect.setSingleShot(True)
        self._reconnect.setInterval(1500)
        self._reconnect.timeout.connect(self._open_events)
        self.events_connected = False
        self._active = False

    # -- configuration --------------------------------------------------------------
    def configure(self, port: int, token: str) -> None:
        self.port = port
        self.token = token
        self._active = True
        self._open_events()

    def disconnect_core(self) -> None:
        self._active = False
        self._reconnect.stop()
        if self._ws is not None:
            self._ws.abort()
            self._ws.deleteLater()
            self._ws = None
        self._set_connected(False)

    def _request(self, path: str) -> QNetworkRequest:
        req = QNetworkRequest(QUrl(f"http://127.0.0.1:{self.port}/admin/v1{path}"))
        req.setRawHeader(b"Authorization", f"Bearer {self.token}".encode())
        req.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        req.setTransferTimeout(30000)
        return req

    # -- HTTP -----------------------------------------------------------------------
    def call(self, method: str, path: str, body: Any = None, callback: Callback | None = None) -> None:
        req = self._request(path)
        data = QByteArray(json.dumps(body).encode()) if body is not None else QByteArray()
        if method == "GET":
            reply = self._nam.get(req)
        elif method == "POST":
            reply = self._nam.post(req, data)
        elif method == "DELETE":
            reply = self._nam.deleteResource(req)
        else:
            reply = self._nam.sendCustomRequest(req, method.encode(), data)
        reply.finished.connect(lambda r=reply: self._finished(r, callback))

    def _finished(self, reply: QNetworkReply, callback: Callback | None) -> None:
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll().data())
        reply.deleteLater()
        if callback is None:
            return
        if status is None:
            callback(False, f"Server core is not reachable ({reply.errorString()})")
            return
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw.decode("utf-8", "replace")
        if 200 <= int(status) < 300:
            callback(True, payload)
        else:
            message = payload.get("message") or payload.get("detail") if isinstance(payload, dict) else payload
            callback(False, str(message or f"HTTP {status}"))

    # -- event stream ---------------------------------------------------------------
    def _open_events(self) -> None:
        if not self._active:
            return
        if self._ws is not None:
            self._ws.deleteLater()
        ws = QWebSocket()
        ws.setParent(self)
        ws.setProxy(QNetworkProxy(QNetworkProxy.NoProxy))
        ws.connected.connect(lambda: self._set_connected(True))
        ws.disconnected.connect(self._on_ws_closed)
        ws.textMessageReceived.connect(self._on_text)
        self._ws = ws
        req = QNetworkRequest(QUrl(f"ws://127.0.0.1:{self.port}/admin/v1/events"))
        req.setRawHeader(b"Authorization", f"Bearer {self.token}".encode())
        ws.open(req)

    def _on_ws_closed(self) -> None:
        self._set_connected(False)
        if self._active:
            self._reconnect.start()

    def _set_connected(self, value: bool) -> None:
        if value != self.events_connected:
            self.events_connected = value
            self.connected_changed.emit(value)

    def _on_text(self, text: str) -> None:
        try:
            self.event.emit(json.loads(text))
        except json.JSONDecodeError:
            pass
