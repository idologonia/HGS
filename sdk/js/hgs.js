/*!
 * hgs.js — JavaScript SDK for HOME GAME SERVER (HGS)
 * Version 1.0.0 · Protocol v1 · No dependencies.
 *
 * Usage (classic script):  <script src="hgs.js"></script>  → global `HGS`
 *
 *   const hgs = HGS.createClient({ serverUrl, gameId, auth: { type: "invite", code, name } });
 *   await hgs.connect();
 *   await hgs.joinRoom("lobby");
 *   hgs.on("game_state", (data, meta) => { ... });
 *   hgs.send("player_action", { action: "move" });
 *
 * Full documentation: docs/architecture/09-js-sdk.md and sdk/js/README.md
 */
(function (root, factory) {
  "use strict";
  const HGS = factory();
  if (typeof module === "object" && module.exports) module.exports = HGS;
  root.HGS = HGS;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const VERSION = "1.0.0";
  const PROTOCOL = 1;
  const EVENT_RE = /^[A-Za-z0-9_.:-]{1,64}$/;
  // Close codes after which reconnecting makes no sense (docs 06, §6.2)
  const FATAL_CLOSE = { 4400: "protocol", 4401: "unauthorized", 4403: "kicked", 4409: "replaced", 4413: "too_large" };

  class HGSError extends Error {
    constructor(code, message, { retryable = false, details = null } = {}) {
      super(message || code);
      this.name = "HGSError";
      this.code = code;
      this.retryable = retryable;
      this.details = details;
    }
  }

  function storageFor(kind) {
    if (kind === "memory") return memoryStorage();
    try {
      const s = window.localStorage;
      const k = "__hgs_test__";
      s.setItem(k, "1");
      s.removeItem(k);
      return s;
    } catch (_) {
      return memoryStorage(); // private mode, file://, blocked storage
    }
  }

  function memoryStorage() {
    const m = new Map();
    return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) };
  }

  function normalizeUrl(url) {
    if (typeof url !== "string" || !url) throw new HGSError("bad_config", "serverUrl is required");
    let u = url.trim().replace(/\/+$/, "");
    if (u.startsWith("https://")) u = "wss://" + u.slice(8);
    else if (u.startsWith("http://")) u = "ws://" + u.slice(7);
    else if (!/^wss?:\/\//.test(u)) u = "wss://" + u;
    if (!/\/hgs\/v1\/ws$/.test(u)) u += "/hgs/v1/ws";
    return u;
  }

  function byteLength(str) {
    return typeof TextEncoder !== "undefined" ? new TextEncoder().encode(str).length : unescape(encodeURIComponent(str)).length;
  }

  class Client {
    constructor(options) {
      const o = Object.assign({}, options || {});
      if (!o.gameId || !/^[a-z0-9][a-z0-9-]{1,47}$/.test(o.gameId)) throw new HGSError("bad_config", "gameId is missing or invalid");
      this.url = normalizeUrl(o.serverUrl);
      this.gameId = o.gameId;
      this.options = {
        reconnect: Object.assign({ enabled: true, maxAttempts: 10, baseDelayMs: 500, maxDelayMs: 10000 }, o.reconnect || {}),
        requestTimeoutMs: o.requestTimeoutMs || 10000,
        queueWhileReconnecting: o.queueWhileReconnecting || 0,
        debug: !!o.debug,
      };
      this._storage = storageFor(o.storage || "localStorage");
      this._tokenKey = "hgs:" + this.gameId + ":" + this.url + ":token";
      this._auth = o.auth || null;
      if (o.token) this._auth = { type: "token", token: o.token };
      this.state = "idle";
      this.player = null;
      this.game = null;
      this.server = null;
      this.room = null; // { id, name, maxPlayers, params, players: [], host, state }
      this._ws = null;
      this._seq = 0;
      this._reqId = 0;
      this._pending = new Map();
      this._handlers = new Map();
      this._attempt = 0;
      this._timer = null;
      this._ping = null;
      this._lastRoomId = null;
      this._queue = [];
      this._connectWaiters = [];
      this._manualClose = false;
      this._onOnline = () => {
        if (this.state === "reconnecting") this._scheduleReconnect(0);
      };
      if (typeof window !== "undefined" && window.addEventListener) window.addEventListener("online", this._onOnline);
    }

    get isHost() {
      return !!(this.room && this.player && this.room.host === this.player.id);
    }

    get savedToken() {
      return this._storage.getItem(this._tokenKey);
    }

    forgetToken() {
      this._storage.removeItem(this._tokenKey);
    }

    // ---------------------------------------------------------------- events
    on(event, fn) {
      if (typeof fn !== "function") throw new HGSError("bad_argument", "handler must be a function");
      if (!this._handlers.has(event)) this._handlers.set(event, new Set());
      this._handlers.get(event).add(fn);
      return () => this.off(event, fn);
    }

    once(event, fn) {
      const off = this.on(event, (...args) => {
        off();
        fn(...args);
      });
      return off;
    }

    off(event, fn) {
      const set = this._handlers.get(event);
      if (!set) return;
      if (fn) set.delete(fn);
      else set.clear();
    }

    _emit(event, ...args) {
      const set = this._handlers.get(event);
      if (!set) return;
      for (const fn of Array.from(set)) {
        try {
          fn(...args);
        } catch (e) {
          console.error("[HGS] error in handler for '" + event + "':", e);
        }
      }
    }

    _log(...args) {
      if (this.options.debug) console.log("[HGS]", ...args);
    }

    _setState(state) {
      if (state === this.state) return;
      const previous = this.state;
      this.state = state;
      this._emit("hgs:state", { state, previous });
    }

    // ---------------------------------------------------------------- connection
    connect() {
      if (this.state === "connected") return Promise.resolve(this.player);
      const p = new Promise((resolve, reject) => this._connectWaiters.push({ resolve, reject }));
      if (this.state === "idle" || this.state === "disconnected" || this.state === "failed") {
        this._manualClose = false;
        this._attempt = 0;
        this._open();
      }
      return p;
    }

    _authPayload() {
      // 1) an explicit token, 2) the token saved after an earlier login, 3) invite / open login
      if (this._auth && this._auth.type === "token" && this._auth.token) return this._auth;
      const saved = this.savedToken;
      if (saved) return { type: "token", token: saved };
      if (this._auth) return this._auth;
      throw new HGSError("bad_config", "no credentials: pass auth (invite/open) or a saved token");
    }

    _open() {
      clearTimeout(this._timer);
      let auth;
      try {
        auth = this._authPayload();
      } catch (e) {
        this._fail(e);
        return;
      }
      this._setState(this._attempt > 0 ? "reconnecting" : "connecting");
      let ws;
      try {
        ws = new WebSocket(this.url);
      } catch (e) {
        this._onClose(1006, "cannot open WebSocket: " + e.message);
        return;
      }
      this._ws = ws;
      this._seq = 0;
      ws.onopen = () => {
        if (ws !== this._ws) return;
        this._setState("authenticating");
        this._rawSend({ t: "hello", d: { game: this.gameId, auth, client: { sdk: "hgs.js/" + VERSION } } });
      };
      ws.onmessage = (ev) => {
        if (ws !== this._ws) return;
        let msg;
        try {
          msg = JSON.parse(ev.data);
        } catch (_) {
          return;
        }
        this._onMessage(msg, auth);
      };
      ws.onclose = (ev) => {
        if (ws !== this._ws) return;
        this._onClose(ev.code, ev.reason);
      };
      ws.onerror = () => {
        /* onclose follows */
      };
    }

    _rawSend(frame) {
      frame.v = PROTOCOL;
      frame.seq = ++this._seq;
      const text = JSON.stringify(frame);
      const limit = this.server && this.server.limits ? this.server.limits.max_message_bytes : null;
      if (limit && byteLength(text) > limit) {
        throw new HGSError("too_large", "message is larger than the server limit (" + limit + " bytes)");
      }
      this._ws.send(text);
    }

    _onMessage(msg, usedAuth) {
      switch (msg.t) {
        case "welcome": {
          const d = msg.d;
          this.player = d.player;
          this.game = d.game || null;
          this.server = d.server;
          if (d.player_token) this._storage.setItem(this._tokenKey, d.player_token);
          const resumed = this._attempt > 0;
          this._attempt = 0;
          this._setState("connected");
          this._startPing();
          for (const w of this._connectWaiters.splice(0)) w.resolve(this.player);
          this._emit("hgs:connected", { player: this.player, game: this.game, resumed });
          if (this._lastRoomId) {
            const rid = this._lastRoomId;
            this.joinRoom(rid).catch((e) => this._emit("hgs:error", e));
          }
          this._flushQueue();
          break;
        }
        case "auth_error": {
          const d = msg.d || {};
          if (usedAuth && usedAuth.type === "token" && d.code === "unauthorized" && !(this._auth && this._auth.type === "token")) {
            this.forgetToken(); // stale token: try the invite / open login once more
            if (this._auth) this._retryWithAuth = true;
          }
          this._authError = new HGSError(d.code || "unauthorized", d.message || "authentication failed", {
            retryable: d.code === "game_unavailable" || d.code === "rate_limited",
          });
          break;
        }
        case "reply": {
          const p = this._pending.get(msg.re);
          if (!p) return;
          this._pending.delete(msg.re);
          clearTimeout(p.timer);
          if (msg.ok) p.resolve(msg.d);
          else p.reject(new HGSError(msg.error.code, msg.error.message, { retryable: msg.error.code === "rate_limited" }));
          break;
        }
        case "room.joined": {
          const d = msg.d;
          this.room = { id: d.room.id, name: d.room.name, maxPlayers: d.room.max_players, params: d.room.params, players: d.players, host: d.host, state: d.state };
          this._lastRoomId = d.room.id;
          this._emit("hgs:room_joined", { room: this.room, players: d.players, state: d.state, host: d.host });
          break;
        }
        case "room.player_joined":
          if (this.room && !this.room.players.some((p) => p.id === msg.d.player.id)) this.room.players.push(msg.d.player);
          this._emit("hgs:player_joined", msg.d);
          break;
        case "room.player_left":
          if (this.room) this.room.players = this.room.players.filter((p) => p.id !== msg.d.player.id);
          this._emit("hgs:player_left", msg.d);
          break;
        case "room.host_changed":
          if (this.room) this.room.host = msg.d.host;
          this._emit("hgs:host_changed", msg.d);
          break;
        case "room.state":
          if (this.room) this.room.state = msg.d.state;
          this._emit("hgs:room_state", msg.d);
          break;
        case "room.closed":
          this.room = null;
          // Closed because the game server is stopping: try the same room again after reconnecting
          // (default rooms are recreated on start). Otherwise the room is gone for good.
          if (!(msg.d && msg.d.reason === "server_stopped")) this._lastRoomId = null;
          this._emit("hgs:room_closed", msg.d);
          break;
        case "event":
          this._emit(msg.d.e, msg.d.d, { from: msg.d.from, roomId: this.room ? this.room.id : null });
          break;
        case "kicked":
          this._lastRoomId = null;
          this._emit("hgs:kicked", msg.d);
          break;
        case "server_shutdown":
          this._emit("hgs:server_shutdown", { graceS: msg.d.grace_s, reason: msg.d.reason });
          break;
        case "error":
          this._emit("hgs:error", new HGSError(msg.d.code, msg.d.message));
          break;
        case "pong":
          break;
        default:
          this._log("unknown message", msg.t);
      }
    }

    _onClose(code, reason) {
      this._stopPing();
      this._ws = null;
      for (const [, p] of this._pending) {
        clearTimeout(p.timer);
        p.reject(new HGSError("network", "connection closed", { retryable: true }));
      }
      this._pending.clear();
      const authError = this._authError;
      this._authError = null;
      const wasConnected = this.state === "connected";
      if (this._retryWithAuth && !this._manualClose) {
        this._retryWithAuth = false;
        this._open();
        return;
      }
      if (this._manualClose) {
        this._setState("disconnected");
        this._emit("hgs:disconnected", { code, reason, willReconnect: false });
        return;
      }
      const fatal = FATAL_CLOSE[code] || (authError && !authError.retryable);
      const r = this.options.reconnect;
      const canRetry = !fatal && r.enabled && this._attempt < r.maxAttempts;
      if (wasConnected || this.state === "connecting" || this.state === "authenticating" || this.state === "reconnecting") {
        this._emit("hgs:disconnected", { code, reason, willReconnect: canRetry });
      }
      if (canRetry) {
        this._attempt += 1;
        const delay = Math.min(r.maxDelayMs, r.baseDelayMs * Math.pow(2, this._attempt - 1));
        this._setState("reconnecting");
        this._scheduleReconnect(delay * (0.5 + Math.random() * 0.5));
        return;
      }
      if (code === 4403) this._lastRoomId = null;
      const err = authError || new HGSError(FATAL_CLOSE[code] || "network", reason || "connection closed (" + code + ")");
      this._fail(err);
    }

    _scheduleReconnect(delay) {
      clearTimeout(this._timer);
      this._emit("hgs:reconnecting", { attempt: this._attempt, delayMs: Math.round(delay) });
      this._timer = setTimeout(() => this._open(), delay);
    }

    _fail(err) {
      this._setState("failed");
      for (const w of this._connectWaiters.splice(0)) w.reject(err);
      this._emit("hgs:error", err);
    }

    _startPing() {
      this._stopPing();
      const every = ((this.server && this.server.heartbeat_s) || 15) * 1000;
      this._ping = setInterval(() => {
        try {
          if (this.state === "connected") this._rawSend({ t: "ping" });
        } catch (_) {}
      }, every);
    }

    _stopPing() {
      clearInterval(this._ping);
      this._ping = null;
    }

    disconnect() {
      this._manualClose = true;
      this._lastRoomId = null;
      clearTimeout(this._timer);
      this._stopPing();
      if (this._ws) this._ws.close(1000, "bye");
      else this._setState("disconnected");
      this.room = null;
    }

    // ---------------------------------------------------------------- requests
    _request(t, d) {
      if (this.state !== "connected") return Promise.reject(new HGSError("not_connected", "not connected to the server", { retryable: true }));
      const id = "q" + ++this._reqId;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          this._pending.delete(id);
          reject(new HGSError("timeout", "server did not answer in time", { retryable: true }));
        }, this.options.requestTimeoutMs);
        this._pending.set(id, { resolve, reject, timer });
        try {
          this._rawSend({ t, id, d });
        } catch (e) {
          clearTimeout(timer);
          this._pending.delete(id);
          reject(e);
        }
      });
    }

    listRooms() {
      return this._request("room.list", {}).then((d) => d.rooms);
    }

    createRoom({ name, maxPlayers, private: isPrivate = false, params = {} } = {}) {
      return this._request("room.create", { name, max_players: maxPlayers, private: isPrivate, params }).then((d) => d.room);
    }

    joinRoom(room) {
      const roomId = typeof room === "string" ? room : room && room.id;
      if (!roomId) return Promise.reject(new HGSError("bad_argument", "room id is required"));
      return new Promise((resolve, reject) => {
        const off = this.on("hgs:room_joined", (info) => {
          if (info.room.id === roomId) {
            off();
            resolve(this.room);
          }
        });
        this._request("room.join", { room_id: roomId }).catch((e) => {
          off();
          if (e.code === "room_not_found" || e.code === "room_closed") this._lastRoomId = null;
          reject(e);
        });
      });
    }

    leaveRoom() {
      this._lastRoomId = null;
      return this._request("room.leave", {}).then(() => {
        this.room = null;
      });
    }

    setRoomState(state) {
      return this._request("room.state.set", { state }).then(() => {
        if (this.room) this.room.state = state;
      });
    }

    patchRoomState(patch) {
      return this._request("room.state.patch", { patch }).then(() => {
        if (this.room) this.room.state = Object.assign({}, this.room.state || {}, patch);
      });
    }

    // ---------------------------------------------------------------- game events
    send(event, data) {
      return this._sendGame(event, data, undefined);
    }

    sendTo(playerId, event, data) {
      if (typeof playerId !== "string") throw new HGSError("bad_argument", "playerId must be a string");
      return this._sendGame(event, data, playerId);
    }

    _sendGame(event, data, to) {
      if (typeof event !== "string" || !EVENT_RE.test(event)) {
        throw new HGSError("bad_argument", "event name must match " + EVENT_RE);
      }
      const d = { e: event };
      if (data !== undefined) d.d = data;
      if (to !== undefined) d.to = to;
      if (this.state !== "connected") {
        if (this.state === "reconnecting" && this.options.queueWhileReconnecting > this._queue.length) {
          this._queue.push(d);
          return false;
        }
        console.warn("[HGS] not connected; event '" + event + "' dropped");
        return false;
      }
      this._rawSend({ t: "game", d });
      return true;
    }

    _flushQueue() {
      const q = this._queue.splice(0);
      for (const d of q) {
        try {
          this._rawSend({ t: "game", d });
        } catch (e) {
          this._emit("hgs:error", e);
        }
      }
    }
  }

  // ------------------------------------------------------------------ helpers
  function fromUrlParams(search) {
    const src = search !== undefined ? search : typeof location !== "undefined" ? location.search : "";
    const p = new URLSearchParams(src);
    const out = {
      serverUrl: p.get("hgs") || null,
      invite: p.get("invite") || null,
      room: p.get("room") || null,
      name: p.get("name") || null,
    };
    if (out.invite && search === undefined && typeof history !== "undefined" && history.replaceState) {
      p.delete("invite"); // keep the code out of bookmarks and screenshots
      const qs = p.toString();
      history.replaceState(null, "", location.pathname + (qs ? "?" + qs : "") + location.hash);
    }
    return out;
  }

  let defaultClient = null;
  function requireDefault() {
    if (!defaultClient) throw new HGSError("not_connected", "call HGS.connect() first");
    return defaultClient;
  }

  return {
    version: VERSION,
    protocol: PROTOCOL,
    HGSError,
    Client,
    createClient: (options) => new Client(options),
    fromUrlParams,
    /** Shortcut for simple games: one global client. */
    connect(options) {
      if (defaultClient) defaultClient.disconnect();
      defaultClient = new Client(options);
      return defaultClient.connect();
    },
    get default() {
      return defaultClient;
    },
    joinRoom: (room) => requireDefault().joinRoom(room),
    leaveRoom: () => requireDefault().leaveRoom(),
    listRooms: () => requireDefault().listRooms(),
    createRoom: (o) => requireDefault().createRoom(o),
    send: (e, d) => requireDefault().send(e, d),
    sendTo: (p, e, d) => requireDefault().sendTo(p, e, d),
    setRoomState: (s) => requireDefault().setRoomState(s),
    patchRoomState: (p) => requireDefault().patchRoomState(p),
    on: (e, fn) => requireDefault().on(e, fn),
    once: (e, fn) => requireDefault().once(e, fn),
    off: (e, fn) => requireDefault().off(e, fn),
    disconnect: () => requireDefault().disconnect(),
  };
});
