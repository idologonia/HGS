# 03. Система адаптерів

## 3.1. Ідея

Адаптер — це «перекладач» між ядром і конкретним типом ігрового сервера. Ядро викликає однаковий інтерфейс
для будь-якої гри; адаптер знає, **як** саме цю гру запустити, як передати їй події й як дізнатися її стан.

Важливо розділити два поняття:

* **Тип адаптера** (`html`, `python`, `process`, у майбутньому інші) — код у HGS.
* **Гра** — конкретний запис у реєстрі з власним `game_id`, конфігом, даними, що використовує один тип адаптера.

## 3.2. BaseAdapter (контракт, версія API 1)

```python
class AdapterState(StrEnum):
    STOPPED = "stopped"; STARTING = "starting"; RUNNING = "running"
    STOPPING = "stopping"; CRASHED = "crashed"; FAILED = "failed"; ERROR = "error"

class Capability(StrEnum):
    ROOMS = "rooms"                  # гра працює з кімнатами
    SERVER_LOGIC = "server_logic"    # є серверна логіка (не чистий relay)
    SAVE_STATE = "save_state"        # вміє зберігати/відновлювати стан
    HOT_RESTART = "hot_restart"      # перезапуск без розриву сесій
    PROXY = "proxy"                  # проксування до власного порту гри

@dataclass(frozen=True)
class AdapterStatus:
    state: AdapterState
    message: str | None
    players_online: int
    rooms_open: int
    started_at: datetime | None
    last_error: str | None

@dataclass(frozen=True)
class AdapterMetrics:
    cpu_percent: float | None
    memory_bytes: int | None
    messages_in_per_s: float
    messages_out_per_s: float
    restarts: int

class BaseAdapter(ABC):
    adapter_type: ClassVar[str]          # "html", "python", ...
    api_version: ClassVar[int] = 1       # версія контракту адаптера

    def __init__(self, ctx: AdapterContext, config: GameConfig): ...

    @classmethod
    @abstractmethod
    def validate_config(cls, raw: dict) -> ValidationResult:
        """Перевірка конфігу БЕЗ побічних ефектів. Викликається при додаванні гри,
        зміні налаштувань і перед кожним start()."""

    @abstractmethod
    async def initialize(self) -> None:
        """Підготовка: директорії, перевірка файлів/інтерпретатора, відновлення стану."""

    @abstractmethod
    async def start(self) -> None:
        """Запуск сервера гри. Повертає керування, коли гра готова приймати гравців
        або кидає AdapterStartError."""

    @abstractmethod
    async def stop(self, reason: StopReason, grace_s: float) -> None:
        """Коректна зупинка: збереження, повідомлення гравцям, завершення процесу."""

    async def restart(self) -> None:
        """Типова реалізація: stop() + start(). Можна перевизначити."""

    @abstractmethod
    def get_status(self) -> AdapterStatus: ...

    @abstractmethod
    def get_metrics(self) -> AdapterMetrics: ...

    def capabilities(self) -> frozenset[Capability]: ...

    # --- події гравців (викликає Router; гравець уже автентифікований ядром) ---
    @abstractmethod
    async def handle_connection(self, player: PlayerRef, room: RoomRef) -> JoinDecision:
        """Гравець приєднується до кімнати гри. Гра може прийняти чи відхилити (наприклад, кімната заповнена
        за правилами гри) і повернути початковий стан."""

    @abstractmethod
    async def handle_message(self, player: PlayerRef, room: RoomRef, event: str, data: JsonValue) -> None: ...

    @abstractmethod
    async def handle_disconnect(self, player: PlayerRef, room: RoomRef, reason: DisconnectReason) -> None: ...

    async def handle_room_event(self, room: RoomRef, event: RoomLifecycle) -> None:
        """created / closed / host_changed — за замовчуванням нічого."""

    async def save(self, reason: SaveReason) -> SaveResult | None:
        """Попросити гру зберегти стан. None — гра не підтримує збереження."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Остаточне звільнення ресурсів при виході з HGS (після stop або аварійно)."""
```

### AdapterContext — що адаптер може робити

Адаптер отримує **обмежений** контекст, прив'язаний до своєї гри. Він не має доступу до інших ігор, БД цілком чи
мережевих сокетів ядра.

```python
class AdapterContext(Protocol):
    game_id: str
    paths: GamePaths                # root, data, saves, backups, logs, runtime (лише цієї гри)
    log: logging.Logger             # логер гри
    def emit(self, event: CoreEvent) -> None          # повідомити ядро про стан/помилку
    rooms: ScopedRoomApi            # кімнати ЛИШЕ цієї гри
    send: ScopedSendApi             # to_player / to_room / to_room_except — лише цієї гри
    processes: ScopedProcessApi     # запуск процесів за LaunchSpec з перевіркою allowlist
    saves: ScopedSaveApi            # запис/читання знімків цієї гри
    worker_bus: WorkerBusApi        # отримати токен воркера, чекати на його підключення
```

### Правила для всіх адаптерів

1. Методи не блокують цикл подій (усе важке — у воркерах або `to_thread`).
2. Будь-яке виключення перехоплює Server Manager → стан `ERROR`/`CRASHED` лише для цієї гри.
3. Кожен виклик має таймаут (`start` 30 с, `stop` = grace + 10 с, обробники подій 5 с) — завислий адаптер
   не зупиняє ядро.
4. Сумісність: ядро перевіряє `api_version`. Нові необов'язкові методи додаються з типовою реалізацією;
   зміна обов'язкових — лише з новою мажорною версією API і підтримкою старої щонайменше одну версію.

## 3.3. HTML Adapter (`adapters/html/`)

Для HTML/JS-ігор, клієнт яких лежить на GitHub Pages чи іншому статичному хостингу. **Гру не переносимо на ПК** —
з HGS спілкується лише SDK `hgs.js` у браузері.

Два режими серверної частини (`backend.mode`):

### `relay` — без серверного коду

* Ядро пересилає подію гравця всім іншим гравцям **тієї ж кімнати** (або конкретному гравцю, якщо вказано `to`).
* Спільний стан кімнати `room_state` (JSON до 64 КБ, налаштовується): `HGS.setRoomState` / `patchRoomState`,
  новий гравець одразу отримує поточний стан.
* «Хост кімнати» (`host_player_id`) — для ігор, де один клієнт веде симуляцію; при його виході хост передається.
* Підходить для наявних ігор з клієнтською логікою та для прототипів.
* Обмеження (чесно): сервер не перевіряє правила гри, тож чесність залежить від клієнтів. Для змагальних ігор
  потрібен режим `logic`.

### `logic` — серверна логіка гри на Python

* Розробник гри пише модуль (наприклад, `server/logic.py`) з класом на базі `hgs_sdk.GameLogic`:

```python
from hgs_sdk import GameLogic, Room, Player

class Game(GameLogic):
    tick_rate = 20                       # опційно: виклики on_tick 20 разів/с

    async def on_room_created(self, room: Room): room.state = {"players": {}}
    async def on_join(self, room: Room, player: Player): ...
    async def on_event(self, room: Room, player: Player, event: str, data): ...
    async def on_leave(self, room: Room, player: Player, reason: str): ...
    async def on_tick(self, room: Room, dt: float): await room.broadcast("game_state", room.state)
    async def on_save(self) -> dict: ...         # формат визначає гра
    async def on_load(self, data: dict): ...
```

* Модуль виконується **в окремому процесі-воркері** (`hgs.exe --worker <game_id>` або `python -m hgs_sdk.runner`),
  який підключається до Worker Bus. Ядро в цьому процесі код гри не імпортує.
* Кожна кімната — окремий об'єкт `Room` зі своїм станом; SDK не дає звертатися до інших кімнат через мережу.

## 3.4. Python Adapter (`adapters/python/`)

Для окремих Python-серверів і Python-ігор.

Налаштування: директорія проєкту, точка входу (`main.py` або `пакет:функція`), інтерпретатор
(системний Python / `venv` у директорії гри / шлях, доданий користувачем у список дозволених), аргументи,
змінні середовища, робоча директорія, ліміти ресурсів.

Режими (`backend.mode`):

* **`worker`** — процес використовує `hgs_sdk` і підключається до Worker Bus. HGS бере на себе мережу, вхід,
  кімнати, безпеку. Рекомендований режим для нових ігор.
* **`standalone`** — наявний Python-сервер зі своїм портом і своїм протоколом. HGS керує процесом
  (start/stop/restart, логи, ресурси, падіння). Для доступу ззовні Gateway може проксувати WebSocket/HTTP
  `/hgs/v1/proxy/{game_id}/...` → `127.0.0.1:<port гри>`, щоб працювати через той самий тунель.
  У цьому режимі вхід і правила — відповідальність гри; HGS додає ліміти розміру/частоти та опційну
  перевірку запрошення перед проксуванням.

Що робить адаптер: запуск процесу через Process Supervisor, stdout/stderr → окремий лог гри, стан процесу (PID,
uptime, код завершення), коректна зупинка, контроль «зависання» (немає heartbeat від воркера 30 с → перезапуск
за політикою).

Python Adapter **ніколи** не виконує код, отриманий від клієнта: запускається лише файл, який користувач вказав у
GUI, із директорії, яку він зареєстрував.

## 3.5. Custom Adapter

Два рівні розширення, від простого до складного:

1. **`process` adapter (вбудований)** — запуск програми будь-якою мовою (Node.js, Go, C#, Rust…) з allowlist.
   Програма спілкується з HGS через Worker Protocol ([06-protocol.md](06-protocol.md), §6.4) — простий JSON
   через локальний WebSocket. Ядро міняти не потрібно. Режим `standalone` (проксі) так само доступний.
2. **Плагін-адаптер (Python-клас)** — новий тип адаптера, коли потрібна інша поведінка запуску (наприклад,
   Docker, Java-сервер із RCON). Розміщується у `%LOCALAPPDATA%\HGS\adapters\<name>\` з маніфестом:

```json
{
  "name": "java-server",
  "adapter_api": 1,
  "version": "0.1.0",
  "entry": "adapter.py:JavaServerAdapter",
  "config_schema": "schema.json"
}
```

Плагіни виконуються в процесі ядра, тому це **довірений код власника ПК**: GUI показує попередження, плагін
вмикається вручну, і за замовчуванням вимкнений. Інструкція «Як написати адаптер» буде в `docs/guides/`.

## 3.6. Реєстр адаптерів

`AdapterRegistry` при старті ядра реєструє вбудовані типи (`html`, `python`, `process`) і увімкнені плагіни,
перевіряє `adapter_api`, і надає GUI JSON-схему конфігу кожного типу — з неї GUI будує форму налаштувань.
