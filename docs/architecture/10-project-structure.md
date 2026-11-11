# 10. Структура репозиторію

```
hgs/
├── README.md
├── pyproject.toml                  метадані, залежності, налаштування pytest/ruff
├── requirements.txt                зафіксовані версії для збірки
├── requirements-dev.txt            pytest, pytest-asyncio, ruff
├── hgs.spec                        конфіг PyInstaller
│
├── src/
│   ├── hgs/
│   │   ├── __init__.py             версія
│   │   ├── __main__.py             точка входу: GUI | --core | --worker <id>
│   │   ├── common/                 спільне для GUI і ядра
│   │   │   ├── paths.py            розташування даних (%LOCALAPPDATA%, портативний режим)
│   │   │   ├── models.py           pydantic-моделі DTO Admin API
│   │   │   └── platform/           windows.py (Job Objects, DPAPI, сигнали) + fallback.py (Linux/macOS для розробки)
│   │   │
│   │   ├── core/
│   │   │   ├── app.py              збирання модулів, запуск трьох ASGI-слухачів, коректне завершення
│   │   │   ├── events.py           Event Bus
│   │   │   ├── logging_setup.py
│   │   │   ├── server_manager.py
│   │   │   ├── process_supervisor.py
│   │   │   ├── sessions.py
│   │   │   ├── rooms.py
│   │   │   ├── players.py
│   │   │   ├── auth.py
│   │   │   ├── router.py
│   │   │   ├── ratelimit.py
│   │   │   ├── saves.py
│   │   │   ├── resources.py
│   │   │   ├── secrets.py
│   │   │   ├── config/             схеми settings.json, маніфесту гри; завантаження/валідація
│   │   │   ├── db/                 database.py, migrations/0001_init.sql, repositories
│   │   │   ├── protocol/           client.py, worker.py — pydantic-схеми конвертів
│   │   │   ├── network/
│   │   │   │   ├── gateway.py      публічний ASGI-застосунок
│   │   │   │   ├── worker_bus.py   внутрішній ASGI-застосунок для воркерів
│   │   │   │   ├── reachability.py
│   │   │   │   └── tunnels/        base.py, cloudflare_quick.py, cloudflare_named.py
│   │   │   └── admin_api/          app.py, routes_*.py, events_ws.py, security.py
│   │   │
│   │   ├── adapters/
│   │   │   ├── base.py             BaseAdapter, AdapterContext, моделі стану
│   │   │   ├── registry.py
│   │   │   ├── html/               adapter.py, relay.py, schema.json
│   │   │   ├── python/             adapter.py, schema.json
│   │   │   └── process/            adapter.py, schema.json
│   │   │
│   │   └── gui/
│   │       ├── app.py              QApplication, тема, трей
│   │       ├── core_client.py      Admin API клієнт (QNetworkAccessManager + QWebSocket)
│   │       ├── core_launcher.py    запуск/нагляд процесу ядра
│   │       ├── theme/              dark.qss, палітра, іконки (SVG)
│   │       ├── widgets/            картка гри, індикатор стану, графік, діалоги
│   │       └── pages/              dashboard.py, games.py, players.py, rooms.py, network.py, logs.py, settings.py
│   │
│   └── hgs_sdk/                    Python SDK для воркерів (окремий пакет, без залежності від hgs.core)
│       ├── __init__.py
│       ├── logic.py                GameLogic, Room, Player
│       ├── connection.py           клієнт Worker Protocol
│       └── runner.py               python -m hgs_sdk.runner <module:Class>
│
├── sdk/js/
│   ├── hgs.js                      JavaScript SDK
│   ├── hgs.d.ts                    типи для редакторів
│   └── README.md
│
├── examples/
│   ├── demo-arena/                 Етап 3: тестова HTML-гра (позиції гравців), relay + logic
│   ├── python-worker/              Етап 5: Python-гра на hgs_sdk
│   ├── python-standalone/          Етап 5: окремий Python WS-сервер
│   └── node-worker/                приклад Custom/process-адаптера
│
├── tests/
│   ├── unit/                       протокол, ліміти, auth, кімнати, конфіги
│   ├── integration/                ядро + реальні WS-клієнти, кілька ігор, падіння воркера
│   └── gui/                        smoke-тести вікна (offscreen)
│
├── docs/
│   ├── architecture/               ці документи
│   └── guides/
│       ├── install.md              встановлення і запуск
│       ├── internet-access.md      налаштування інтернет-доступу
│       ├── add-new-game.md         підключення нової гри
│       ├── integrate-existing-game.md
│       └── write-adapter.md        власний адаптер
│
└── .github/workflows/
    └── windows.yml                 тести і збірка на windows-latest (GitHub Actions)
```

Правила:

* `core` не імпортує `gui`; `gui` не імпортує `core` (лише `common`). Зв'язок — тільки Admin API.
* `hgs_sdk` не імпортує `hgs` — його можна встановити окремо у venv гри.
* Платформна специфіка — лише в `common/platform/`; решта коду не містить `if sys.platform`.
