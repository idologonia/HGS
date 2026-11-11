CREATE TABLE game_passwords (
  game_id TEXT PRIMARY KEY REFERENCES games(game_id) ON DELETE CASCADE,
  password_hash BLOB NOT NULL,
  updated_at TEXT NOT NULL
);
