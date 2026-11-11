# 02. Модулі серверного ядра

Ядро (`hgs.core`) — один asyncio-процес. Кожен модуль має чітку відповідальність і спілкується з іншими
через явні інтерфейси або Event Bus. Модулі не знають про GUI.

## 2.1. Server Manager (`core/server_manager.py`)

Відповідає за життєвий цикл ігрових серверів.

* Тримає реєстр `GameServer` (по одному на `game_id`): конфіг, адаптер, стан, лічильник падінь.
* Операції: `start(game_id)`, `stop(game_id, grace_s, force=False)`, `restart(game_id)`, `status(game_id)`.
* Серіалізує операції над однією грою (asyncio.Lock на гру) — подвійне натискання Start не запустить два процеси.
* Реалізує політику перезапуску (див. 01, §1.7).
* Будь-яке виключення адаптера перехоплюється, гра переходить у `ERROR`/`CRASHED`, решта ядра працює далі.
* При завершенні HGS зупиняє всі ігри паралельно з загальним таймаутом.

## 2.2. Process Supervisor (`core/process_supervisor.py`)

Запуск зовнішніх процесів **лише зі списку дозволених**.

* Приймає `LaunchSpec` (інтерпретатор, точка входу, аргументи, робоча директорія, env) — формується тільки з
  конфігу гри, ніколи з мережевих повідомлень.
* Перевіряє: інтерпретатор у списку дозволених; точка входу знаходиться всередині директорії гри
  (після `realpath`, без `..` і символьних посилань назовні); директорія гри зареєстрована користувачем.
* Запускає через `asyncio.create_subprocess_exec` (без `shell=True`), з `CREATE_NEW_PROCESS_GROUP`.
* Додає процес до Job Object гри: ліміт пам'яті, ліміт CPU (%), `KILL_ON_JOB_CLOSE`.
* Читає stdout/stderr построково → Logger (окремий файл гри) + Event Bus (для GUI).
* Коректне завершення: повідомлення `shutdown` через Worker Protocol → очікування → `CTRL_BREAK_EVENT`
  → очікування → `TerminateJobObject`.
* Звичайні права користувача; HGS ніколи не просить прав адміністратора.

## 2.3. Network Manager (`core/network/`)

* **Gateway** — публічний ASGI-застосунок (WebSocket `/hgs/v1/ws`, `/hgs/v1/health`, `/hgs/v1/info`,
  роздача `hgs.js`). Слухає `127.0.0.1` (режим Local), `0.0.0.0` (LAN/Direct).
* **Tunnel Manager** — керує провайдерами тунелю (`cloudflared` тощо), публікує зовнішню адресу.
* **Reachability Checker** — перевіряє, чи досяжний сервер ззовні.
* Деталі — у [04-network.md](04-network.md).

## 2.4. Session Manager (`core/sessions.py`)

Сесія = одне автентифіковане WebSocket-з'єднання гравця з конкретною грою.

* Створює сесію після успішного `hello`, видає `session_id` і одноразовий `resume_token`.
* Тримає сесію у стані `DETACHED` протягом `resume_window` (типово 30 с) після розриву з'єднання,
  щоб гравець міг повернутися в ту саму кімнату без повторного входу.
* Heartbeat: ping кожні 15 с, розрив після 2 пропущених.
* Для кожної сесії: token bucket (ліміт частоти), лічильник `seq` (захист від повторів), ліміт розміру.
* Примусове відключення (kick) адміністратором.

## 2.5. Room Manager (`core/rooms.py`)

* Кімната належить **рівно одній грі**: ключ `(game_id, room_id)`. Маршрутизатор фізично не може переслати
  подію в кімнату іншої гри чи іншу кімнату тієї ж гри.
* Поля: `room_id`, `name`, `game_id`, `max_players`, `players`, `status` (`open`/`locked`/`in_game`/`closed`),
  `params` (довільні параметри сесії, JSON), `created_by`, `created_at`, `private` + опційний код кімнати.
* Створення: адміністратором (GUI) або гравцем (якщо гра дозволяє `allow_player_create`).
* Закриття: адміністратором, грою або автоматично після `empty_ttl` без гравців.
* Для режиму `relay` — зберігає `room_state` (JSON, ліміт розміру) і `host_player_id` (перший гравець,
  передається наступному при виході).

## 2.6. Player Manager (`core/players.py`)

* Гравець прив'язаний до гри: `player_id` унікальний у межах `game_id` (немає глобального акаунта для всіх ігор).
* Поля: `player_id`, `display_name`, `game_id`, `auth_method`, `created_at`, `last_seen`, `banned`, статус онлайн
  (обчислюється з активних сесій), поточна кімната.
* Операції адміністратора: kick, ban/unban, відкликання токенів.

## 2.7. Authentication Manager (`core/auth.py`)

* Режими входу для гри: `invite` (типово), `password`, `account`, `open` (лише LAN/localhost).
* Видача та перевірка токенів, зберігання тільки хешів.
* Обмеження спроб входу (на IP і на ідентифікатор).
* Деталі — у [05-security.md](05-security.md).

## 2.8. Message Router (`core/router.py`)

Шлях кожного вхідного повідомлення гравця:

```
WebSocket frame
 → перевірка розміру → JSON-розбір → pydantic-схема конверта
 → перевірка seq → rate limit → перевірка прав (сесія в кімнаті?)
 → системне повідомлення (room.*, ping) → Room/Session Manager
 → ігрова подія → адаптер гри (relay в ядрі або Worker Bus → воркер)
```

Вихідні повідомлення від воркера проходять зворотну перевірку: воркер може адресувати лише кімнати й гравців
своєї гри.

## 2.9. Database Manager (`core/db/`)

* SQLite у режимі WAL, один потік-записувач, черга запитів з asyncio (`run_in_executor`).
* Версійовані міграції (`schema_version`), автоматичний бекап файлу БД перед міграцією.
* Схема — у [07-config-and-data.md](07-config-and-data.md).

## 2.10. Event Bus (`core/events.py`)

* Внутрішній асинхронний pub/sub з типізованими подіями (pydantic-моделі), без зовнішніх брокерів.
* Повільний підписник не блокує інших: окрема обмежена черга на підписника, переповнення → відкидання
  найстаріших подій + лічильник втрат.
* Admin API транслює події в GUI через `ws://127.0.0.1:8764/admin/v1/events`.

Основні події: `core.ready`, `server.state_changed`, `server.log`, `player.connected`, `player.disconnected`,
`room.created`, `room.updated`, `room.closed`, `security.alert`, `tunnel.state_changed`, `metrics.tick`,
`save.completed`, `save.failed`.

## 2.11. Logger (`core/logging_setup.py`)

* Стандартний `logging`, формат JSON Lines, ротація файлів (`RotatingFileHandler`).
* Окремі файли: `logs/core.log`, `logs/security.log`, `games/<id>/logs/server.log` (+ stdout/stderr воркера).
* Кільцевий буфер останніх N рядків для GUI.
* Токени й паролі маскуються фільтром перед записом.

## 2.12. Save Manager (`core/saves.py`)

* Окрема директорія для кожної гри; формат даних визначає адаптер (ядро зберігає байти/JSON як є).
* Атомарний запис: тимчасовий файл → `fsync` → `os.replace`.
* Знімки з ротацією (зберігати N останніх), резервні копії директорії гри (zip) за запитом/розкладом/при Stop.
* Журнал збережень у БД (`saves`): коли, причина, розмір, контрольна сума, успіх/помилка.
* Відновлення після аварії: при старті ядро бачить «брудний» прапорець завершення → GUI пропонує відновити
  останній знімок. Сервери все одно **не** запускаються автоматично.

## 2.13. Resource Monitor (`core/resources.py`)

* Кожні 2 с: CPU/RAM системи, мережа (байти/с), CPU/RAM кожного дерева процесів гри (psutil).
* Перевищення м'яких порогів → `security.alert`/`resource.warning`; жорсткі ліміти — через Job Object.

## 2.14. Admin API (`core/admin_api/`)

REST + WebSocket тільки на `127.0.0.1`. Кожен запит з заголовком `Authorization: Bearer <admin_token>`.
Додатково: перевірка `Host` (захист від DNS rebinding), відхилення запитів з браузерним `Origin` (захист від CSRF).

Основні маршрути (`/admin/v1/...`):

| Метод | Шлях | Дія |
|-------|------|-----|
| GET | `/status` | стан ядра, метрики |
| GET/POST/DELETE | `/games`, `/games/{id}` | реєстр ігор |
| PUT | `/games/{id}/config` | змінити конфіг (з валідацією) |
| POST | `/games/{id}/start` \| `stop` \| `restart` | керування |
| GET | `/games/{id}/logs?tail=N` | логи |
| GET | `/players?game_id=` | гравці |
| POST | `/players/{gid}/{pid}/kick` \| `ban` | модерація |
| GET/POST/DELETE | `/rooms`, `/rooms/{gid}/{rid}` | кімнати |
| GET/POST | `/invites` | коди запрошень |
| GET/PUT | `/network`, `/network/tunnel/start` \| `stop`, `/network/check` | мережа |
| GET/POST | `/games/{id}/saves`, `/games/{id}/backups`, `.../restore` | збереження |
| WS | `/events` | потік подій |
