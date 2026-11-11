CREATE TABLE games (
  game_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  adapter TEXT NOT NULL,
  root_path TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  added_at TEXT NOT NULL,
  last_started_at TEXT,
  last_state TEXT
);

CREATE TABLE players (
  game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
  player_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  auth_method TEXT NOT NULL,
  password_hash BLOB,
  created_at TEXT NOT NULL,
  last_seen_at TEXT,
  banned INTEGER NOT NULL DEFAULT 0,
  ban_reason TEXT,
  PRIMARY KEY (game_id, player_id),
  UNIQUE (game_id, display_name)
);

CREATE TABLE player_tokens (
  token_hash BLOB PRIMARY KEY,
  game_id TEXT NOT NULL,
  player_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_used_at TEXT,
  revoked INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY (game_id, player_id) REFERENCES players(game_id, player_id) ON DELETE CASCADE
);

CREATE TABLE invites (
  code_hash BLOB PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
  room_id TEXT,
  label TEXT,
  max_uses INTEGER NOT NULL DEFAULT 1,
  uses INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE rooms (
  game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
  room_id TEXT NOT NULL,
  name TEXT NOT NULL,
  max_players INTEGER NOT NULL,
  private INTEGER NOT NULL DEFAULT 0,
  join_code_hash BLOB,
  params TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,
  persistent INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  closed_at TEXT,
  PRIMARY KEY (game_id, room_id)
);

CREATE TABLE saves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  reason TEXT NOT NULL,
  path TEXT,
  size_bytes INTEGER,
  sha256 TEXT,
  ok INTEGER NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE security_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,
  severity TEXT NOT NULL,
  game_id TEXT,
  ip TEXT,
  player_id TEXT,
  details TEXT
);
CREATE INDEX security_events_at ON security_events(at);

CREATE TABLE secrets (
  name TEXT PRIMARY KEY,
  blob BLOB NOT NULL,
  updated_at TEXT NOT NULL
);
