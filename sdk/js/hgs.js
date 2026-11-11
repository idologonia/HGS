/*!
 * hgs.js - JavaScript SDK for HOME GAME SERVER (HGS)
 * Version 1.0.0 - Protocol v1 - No dependencies.
 *
 * Usage (classic script):  <script src="hgs.js"></script>  -> global `HGS`
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
  // Close codes after which reconnecting makes no sense (docs 06, section 6.2)
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
      // "github:owner/repo[/file.json]" = find the current address in a file the HGS owner publishes
      this._discover = typeof o.serverUrl === "string" && o.serverUrl.startsWith("github:") ? o.serverUrl.slice(7) : null;
      this.url = this._discover ? null : normalizeUrl(o.serverUrl);
      this.serverName = null;
      this.gameId = o.gameId;
      this.options = {
        reconnect: Object.assign({ enabled: true, maxAttempts: 10, baseDelayMs: 500, maxDelayMs: 10000 }, o.reconnect || {}),
        requestTimeoutMs: o.requestTimeoutMs || 10000,
        queueWhileReconnecting: o.queueWhileReconnecting || 0,
        debug: !!o.debug,
      };
      this._storage = storageFor(o.storage || "localStorage");
      // the saved login does not depend on the (changing) tunnel address
      this._tokenKey = "hgs:" + this.gameId + ":" + (this._discover || this.url) + ":token";
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
        this._openResolved();
      }
      return p;
    }

    /** Find the server address (discovery mode), then open the connection. */
    _openResolved() {
      if (!this._discover || (this.url && this._attempt % 3 !== 2)) {
        this._open();
        return;
      }
      this._setState(this._attempt > 0 ? "reconnecting" : "connecting");
      findServer(this._discover)
        .then((info) => {
          this.serverName = info.name || null;
          if (!info.online || !info.server) throw new HGSError("server_offline", "the game server is offline right now", { retryable: true });
          this.url = normalizeUrl(info.server);
          this._open();
        })
        .catch((e) => {
          const err = e instanceof HGSError ? e : new HGSError("network", "cannot find the server: " + e.message, { retryable: true });
          if (this.url) return this._open(); // keep trying the last known address
          this._fail(err);
        });
    }

    _authPayload() {
      // 0) resume the same session right after a dropped connection (one-time token, short window)
      const r = this._resume;
      if (r && r.until && Date.now() < r.until) return { type: "resume", session_id: r.sessionId, resume_token: r.token };
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
          // keep the one-time resume token in memory only (never in storage)
          this._resume = d.resume_token ? { sessionId: d.session_id, token: d.resume_token, until: null,
            windowMs: ((d.server && d.server.resume_window_s) || 30) * 1000 } : null;
          const reconnected = this._attempt > 0;
          this._attempt = 0;
          this._setState("connected");
          this._startPing();
          for (const w of this._connectWaiters.splice(0)) w.resolve(this.player);
          this._emit("hgs:connected", { player: this.player, game: this.game, resumed: !!d.resumed, reconnected });
          if (d.resumed && d.room_id && d.room_id === this._lastRoomId) {
            // still in the room: the server sends room.joined with the current state
          } else if (this._lastRoomId) {
            const rid = this._lastRoomId;
            this.joinRoom(rid).catch((e) => this._emit("hgs:error", e));
          }
          this._flushQueue();
          break;
        }
        case "auth_error": {
          const d = msg.d || {};
          if (d.code === "resume_failed") {
            this._resume = null; // session expired: log in again normally
            this._retryWithAuth = true;
          } else if (usedAuth && usedAuth.type === "token" && d.code === "unauthorized" && !(this._auth && this._auth.type === "token")) {
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
      if (wasConnected && this._resume && !this._manualClose) {
        this._resume.until = Date.now() + this._resume.windowMs - 2000;
      }
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
      this._timer = setTimeout(() => this._openResolved(), delay);
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
      this._resume = null;
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

  // ------------------------------------------------------------------ server discovery
  /**
   * Reads the address file the HGS owner publishes on GitHub ("owner/repo" or "owner/repo/path.json").
   * Resolves to { server, online, name, updated }.
   */
  function findServer(spec) {
    const parts = String(spec).split("/");
    if (parts.length < 2) return Promise.reject(new HGSError("bad_config", "use github:owner/repo"));
    const repo = parts[0] + "/" + parts[1];
    const path = parts.length > 2 ? parts.slice(2).join("/") : "hgs-server.json";
    const api = "https://api.github.com/repos/" + repo + "/contents/" + path + "?t=" + Date.now();
    const raw = "https://raw.githubusercontent.com/" + repo + "/HEAD/" + path + "?t=" + Date.now();
    const get = (url, headers) => fetch(url, { headers, cache: "no-store" }).then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
    // the API is always fresh; raw.githubusercontent.com is the fallback (it may be cached for a few minutes)
    return get(HGS_DISCOVERY.api || api, { Accept: "application/vnd.github.raw+json" })
      .catch(() => get(HGS_DISCOVERY.raw || raw, {}));
  }
  const HGS_DISCOVERY = { api: null, raw: null }; // tests can point these elsewhere

  // ------------------------------------------------------------------ built-in login window
  const UI_TEXT = {
    uk: { login: "\u0412\u0445\u0456\u0434", register: "\u0420\u0435\u0454\u0441\u0442\u0440\u0430\u0446\u0456\u044f", name: "\u0406\u043c'\u044f", password: "\u041f\u0430\u0440\u043e\u043b\u044c", go: "\u0413\u0440\u0430\u0442\u0438", offline: "\u0421\u0435\u0440\u0432\u0435\u0440 \u0433\u0440\u0438 \u0437\u0430\u0440\u0430\u0437 \u0432\u0438\u043c\u043a\u043d\u0435\u043d\u043e. \u0421\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u043f\u0456\u0437\u043d\u0456\u0448\u0435.", retry: "\u0421\u043f\u0440\u043e\u0431\u0443\u0432\u0430\u0442\u0438 \u0449\u0435 \u0440\u0430\u0437", connecting: "\u041f\u0456\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u043d\u044f\u2026", short: "\u041f\u0430\u0440\u043e\u043b\u044c \u2014 \u0449\u043e\u043d\u0430\u0439\u043c\u0435\u043d\u0448\u0435 6 \u0441\u0438\u043c\u0432\u043e\u043b\u0456\u0432" },
    en: { login: "Log in", register: "Sign up", name: "Name", password: "Password", go: "Play", offline: "The game server is offline right now. Try again later.", retry: "Try again", connecting: "Connecting\u2026", short: "Password: at least 6 characters" },
  };
  const ERR_TEXT = {
    uk: { unauthorized: "\u041d\u0435\u043f\u0440\u0430\u0432\u0438\u043b\u044c\u043d\u0435 \u0456\u043c'\u044f \u0430\u0431\u043e \u043f\u0430\u0440\u043e\u043b\u044c", name_taken: "\u0426\u0435 \u0456\u043c'\u044f \u0432\u0436\u0435 \u0437\u0430\u0439\u043d\u044f\u0442\u0435", rate_limited: "\u0417\u0430\u0431\u0430\u0433\u0430\u0442\u043e \u0441\u043f\u0440\u043e\u0431, \u0437\u0430\u0447\u0435\u043a\u0430\u0439\u0442\u0435 \u043a\u0456\u043b\u044c\u043a\u0430 \u0445\u0432\u0438\u043b\u0438\u043d", banned: "\u0414\u043e\u0441\u0442\u0443\u043f \u0437\u0430\u0431\u043b\u043e\u043a\u043e\u0432\u0430\u043d\u043e", forbidden: "\u0420\u0435\u0454\u0441\u0442\u0440\u0430\u0446\u0456\u044e \u0432\u0438\u043c\u043a\u043d\u0435\u043d\u043e \u0432\u043b\u0430\u0441\u043d\u0438\u043a\u043e\u043c \u0441\u0435\u0440\u0432\u0435\u0440\u0430" },
    en: { unauthorized: "Wrong name or password", name_taken: "This name is taken", rate_limited: "Too many attempts, wait a few minutes", banned: "Access blocked", forbidden: "Registration is disabled" },
  };

  function overlay(lang) {
    const t = UI_TEXT[lang] || UI_TEXT.en;
    const box = document.createElement("div");
    box.setAttribute("data-hgs-login", "");
    box.style.cssText = "position:fixed;inset:0;z-index:2147483000;display:flex;align-items:center;justify-content:center;background:rgba(10,12,16,.82);font:15px system-ui,sans-serif;color:#e6e8ee";
    box.innerHTML =
      '<form style="background:#171a21;border:1px solid #2a2f3a;border-radius:12px;padding:22px;width:min(320px,90vw);display:flex;flex-direction:column;gap:10px">' +
      '<div style="display:flex;gap:6px"><button type="button" data-tab="login"></button><button type="button" data-tab="register"></button></div>' +
      '<input name="u" autocomplete="username" maxlength="24" required>' +
      '<input name="p" type="password" autocomplete="current-password" maxlength="128" required>' +
      '<div data-msg style="min-height:1.2em;color:#ff5c5c;font-size:13px"></div>' +
      '<button type="submit" data-go></button></form>';
    const style = "padding:9px 10px;border-radius:7px;border:1px solid #2a2f3a;background:#0f1115;color:#e6e8ee;font:inherit";
    box.querySelectorAll("input").forEach((i) => (i.style.cssText = style));
    box.querySelector('[name="u"]').placeholder = t.name;
    box.querySelector('[name="p"]').placeholder = t.password;
    box.querySelector("[data-go]").textContent = t.go;
    box.querySelector("[data-go]").style.cssText = style + ";background:#4f8cff;border-color:#4f8cff;color:#fff;font-weight:600;cursor:pointer";
    box.querySelector('[data-tab="login"]').textContent = t.login;
    box.querySelector('[data-tab="register"]').textContent = t.register;
    return box;
  }

  /**
   * One call for a game: finds the server, shows a login/sign-up window only when needed
   * (the login is remembered), joins the room and resolves with a connected client.
   *
   *   const hgs = await HGS.start({ gameId: "my-game", server: "github:me/hgs-address", room: "lobby" });
   *
   * A ?hgs=ws://... parameter in the page address overrides `server` (handy for local tests).
   */
  function start(options) {
    const o = Object.assign({ room: "lobby", lang: (typeof navigator !== "undefined" && /^uk|^ru/.test(navigator.language)) ? "uk" : "en" }, options || {});
    const t = UI_TEXT[o.lang] || UI_TEXT.en;
    const e = ERR_TEXT[o.lang] || ERR_TEXT.en;
    const params = fromUrlParams();
    const client = new Client({ serverUrl: params.serverUrl || o.server, gameId: o.gameId, storage: o.storage,
      auth: params.invite ? { type: "invite", code: params.invite, name: params.name || "Player" } : null, debug: o.debug });
    const room = params.room || o.room;
    return new Promise((resolve) => {
      let box = null;
      const done = () => {
        if (box) box.remove();
        client.joinRoom(room).then(() => resolve(client), () => resolve(client));
      };
      const showOffline = () => {
        if (!box) {
          box = overlay(o.lang);
          document.body.appendChild(box);
        }
        box.querySelector("form").innerHTML = '<div style="text-align:center;line-height:1.5">' + t.offline + '</div><button type="button" data-retry style="padding:9px;border-radius:7px;border:0;background:#4f8cff;color:#fff;font:inherit;cursor:pointer">' + t.retry + "</button>";
        box.querySelector("[data-retry]").onclick = () => {
          box.remove();
          box = null;
          attempt();
        };
      };
      const showLogin = (message) => {
        if (!box) {
          box = overlay(o.lang);
          document.body.appendChild(box);
        }
        let mode = "login";
        const tabs = box.querySelectorAll("[data-tab]");
        const paint = () => tabs.forEach((b) => {
          const on = b.getAttribute("data-tab") === mode;
          b.style.cssText = "flex:1;padding:8px;border-radius:7px;border:1px solid " + (on ? "#4f8cff" : "#2a2f3a") + ";background:" + (on ? "#1e2a44" : "transparent") + ";color:#e6e8ee;font:inherit;cursor:pointer";
        });
        tabs.forEach((b) => (b.onclick = () => { mode = b.getAttribute("data-tab"); paint(); }));
        paint();
        const msg = box.querySelector("[data-msg]");
        msg.textContent = message || "";
        box.querySelector("form").onsubmit = (ev) => {
          ev.preventDefault();
          const username = box.querySelector('[name="u"]').value.trim();
          const password = box.querySelector('[name="p"]').value;
          if (mode === "register" && password.length < 6) { msg.textContent = t.short; return; }
          msg.style.color = "#8a91a0";
          msg.textContent = t.connecting;
          client._auth = { type: "account", username, password, register: mode === "register" };
          client.forgetToken();
          client.connect().then(done, (err) => {
            msg.style.color = "#ff5c5c";
            if (err.code === "server_offline") return showOffline();
            msg.textContent = e[err.code] || err.message;
          });
        };
      };
      const attempt = () => {
        if (!client.savedToken && !client._auth) {
          // first visit: make sure the server is online before asking to log in
          if (!client._discover) return showLogin();
          return findServer(client._discover).then(
            (info) => (info.online && info.server ? showLogin() : showOffline()),
            () => showOffline());
        }
        client.connect().then(done, (err) => {
          if (err.code === "server_offline") return showOffline();
          client.forgetToken();
          client._auth = null;
          showLogin(err.code === "unauthorized" ? "" : (e[err.code] || err.message));
        });
      };
      attempt();
    });
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
    findServer,
    start,
    _discovery: HGS_DISCOVERY,
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
