# 07. Конфігурації, база даних, збереження

## 7.1. Розташування даних

```
%LOCALAPPDATA%\HGS\                 (або папка data\ поруч із hgs.exe у портативному режимі)
├── hgs.db                          SQLite: метадані, гравці, кімнати, журнали
├── settings.json                   глобальні налаштування (людиночитні, валідуються)
├── hgs.lock                        замок single-instance + прапорець «чисте завершення»
├── bin\cloudflared.exe             (якщо завантажено через HGS)
├── adapters\<plugin>\              плагіни-адаптери (вимкнені за замовчуванням)
├── certs\                          сертифікати для Direct-режиму
├── logs\core.log, security.log
└── games\<game_id>\
    ├── game.json                   конфіг гри в HGS (копія/посилання на маніфест + локальні налаштування)
    ├── saves\                      знімки стану (формат визначає гра)
    ├── backups\                    zip-архіви
    ├── logs\server.log, stdout.log, stderr.log
    └── runtime\                    тимчасові файли воркера (очищається при старті)
```

Файли гри (код, клієнт) **не копіюються** в HGS: гра лишається у своїй директорії (наприклад, клон
репозиторію GitHub). HGS зберігає лише шлях до неї.

## 7.2. Маніфест гри `hgs-game.json`

Лежить у корені репозиторію гри (його готує розробник гри/Claude). При додаванні гри HGS читає маніфест,
валідує через схему адаптера і створює `games\<game_id>\game.json`, де власник може змінювати локальні налаштування
(ліміти, режим входу), не чіпаючи репозиторій гри.

```json
{
  "$schema": "https://…/hgs-game.schema.json",
  "manifest_version": 1,
  "id": "tank-arena",
  "name": "Tank Arena",
  "version": "1.4.2",
  "adapter": "html",
  "adapter_api": 1,

  "client": {
    "url": "https://myname.github.io/tank-arena/",
    "allowed_origins": ["https://myname.github.io", "http://localhost:5500"]
  },

  "backend": {
    "mode": "logic",
    "module": "server/logic.py",
    "class": "Game",
    "python": "auto"
  },

  "auth":  { "mode": "invite" },
  "rooms": { "max_rooms": 10, "max_players": 8, "allow_player_create": true, "empty_ttl_s": 300 },
  "limits": { "max_message_bytes": 16384, "messages_per_s": 30, "memory_mb": 512, "cpu_percent": 50 },
  "saves": { "enabled": true, "interval_s": 300, "keep": 10, "on_stop": true },
  "restart": { "policy": "on_crash", "max_restarts": 3, "window_s": 600 }
}
```

Приклад для Python-сервера (`adapter: "python"`):

```json
{
  "manifest_version": 1, "id": "chess-py", "name": "Chess", "adapter": "python", "adapter_api": 1,
  "backend": {
    "mode": "standalone",
    "entry": "server/main.py",
    "args": ["--port", "{port}"],
    "python": "venv",
    "port": "auto",
    "ready_check": { "type": "tcp", "timeout_s": 20 }
  },
  "auth": { "mode": "invite" }
}
```

Плейсхолдери в `args` (`{port}`, `{data_dir}`, `{game_id}`) підставляє ядро; довільні змінні не дозволені.
Кожен тип адаптера публікує свою JSON-схему; невідомі поля → помилка валідації з точним шляхом поля.

## 7.3. Глобальні налаштування `settings.json`

```json
{
  "settings_version": 1,
  "server_name": "Oleh's HGS",
  "admin":   { "port": 8764 },
  "gateway": { "port": 8765, "bind": "local" },
  "worker_bus": { "port": 8766 },
  "network": {
    "mode": "tunnel",
    "tunnel": { "provider": "cloudflare_quick", "cloudflared_path": null, "autostart_with_server": false },
    "direct": { "public_host": null, "tls_cert": null, "tls_key": null, "duckdns_domain": null },
    "public_ip_lookup": true
  },
  "resources": { "metrics_interval_s": 2 },
  "logs": { "max_file_mb": 10, "keep_files": 5 },
  "ui": { "minimize_to_tray": true, "language": "uk" }
}
```

Секрети (токени тунелю, DuckDNS) **не** зберігаються в `settings.json` — лише в БД, зашифровані DPAPI.

## 7.4. Схема SQLite (версія 1)

```sql
CREATE TABLE schema_version (version INTEGER NOT NULL);

CREATE TABLE games (
  game_id TEXT PRIMARY KEY,          -- ^[a-z0-9][a-z0-9-]{1,47}$
  name TEXT NOT NULL,
  adapter TEXT NOT NULL,
  root_path TEXT NOT NULL,           -- зареєстрована директорія гри (allowlist)
  enabled INTEGER NOT NULL DEFAULT 1,
  added_at TEXT NOT NULL,
  last_started_at TEXT,
  last_state TEXT                    -- для діагностики після аварії
);

CREATE TABLE players (
  game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
  player_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  auth_method TEXT NOT NULL,
  password_hash BLOB,                -- лише для режиму account
  created_at TEXT NOT NULL,
  last_seen_at TEXT,
  banned INTEGER NOT NULL DEFAULT 0,
  ban_reason TEXT,
  PRIMARY KEY (game_id, player_id),
  UNIQUE (game_id, display_name)
);

CREATE TABLE player_tokens (
  token_hash BLOB PRIMARY KEY,       -- SHA-256
  game_id TEXT NOT NULL, player_id TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, last_used_at TEXT,
  revoked INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY (game_id, player_id) REFERENCES players ON DELETE CASCADE
);

CREATE TABLE invites (
  code_hash BLOB PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
  room_id TEXT,
  label TEXT,
  max_uses INTEGER NOT NULL DEFAULT 1, uses INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE rooms (                 -- метадані; живий стан — у пам'яті ядра/воркера
  game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
  room_id TEXT NOT NULL,
  name TEXT NOT NULL, max_players INTEGER NOT NULL,
  private INTEGER NOT NULL DEFAULT 0, join_code_hash BLOB,
  params TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL,
  persistent INTEGER NOT NULL DEFAULT 0,   -- створена адміністратором і переживає перезапуск
  created_at TEXT NOT NULL, closed_at TEXT,
  PRIMARY KEY (game_id, room_id)
);

CREATE TABLE saves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id TEXT NOT NULL, kind TEXT NOT NULL,       -- snapshot | backup
  reason TEXT NOT NULL,                            -- interval | stop | manual | crash_recovery
  path TEXT, size_bytes INTEGER, sha256 TEXT,
  ok INTEGER NOT NULL, error TEXT, created_at TEXT NOT NULL
);

CREATE TABLE security_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
  game_id TEXT, ip TEXT, player_id TEXT, details TEXT
);

CREATE TABLE secrets (
  name TEXT PRIMARY KEY, blob BLOB NOT NULL, updated_at TEXT NOT NULL   -- DPAPI-зашифровано
);
```

Таблиці журналів очищаються за віком (типово 30 днів).

## 7.5. Збереження і відновлення

* **Знімок стану гри** — ядро просить воркер `w.save`, отримує дані у форматі гри і пише
  `saves\<timestamp>_<reason>.json|bin` атомарно. Ротація: зберігати `keep` останніх.
* **Ігри, що зберігають самі** (наприклад, `standalone` Python-сервер зі своєю БД у `{data_dir}`) —
  HGS не втручається у формат, лише робить zip-бекап директорії даних гри при Stop/за розкладом/вручну
  (бекап робиться, коли процес зупинений або після `w.save`, щоб не копіювати файл посеред запису).
* **Аварійне завершення HGS** — у `hgs.lock` прапорець `clean_shutdown=false` під час роботи. Якщо при старті
  прапорець лишився `false`: GUI показує «HGS завершився некоректно», список ігор, що працювали, і пропонує
  відновити з останнього знімка під час наступного Start. Автозапуску немає.
* **Відновлення** (GUI → Saves → Restore) лише для зупиненої гри; поточний стан спершу автоматично
  архівується, щоб відновлення можна було скасувати.
