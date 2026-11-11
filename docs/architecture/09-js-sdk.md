# 09. JavaScript SDK (`hgs.js`)

## 9.1. Вимоги до SDK

* Один файл без залежностей і без збирача (`<script src="hgs.js">` або ES-модуль `import { HGS } from "./hgs.js"`).
* Працює в сучасних браузерах (Chrome, Edge, Firefox, Safari останніх 2 років).
* Не нав'язує структуру ігрового стану.
* Повний життєвий цикл з'єднання, автоматичне перепідключення, черга запитів, зрозумілі помилки.
* Версія SDK пишеться у `hello` — сервер може попередити про несумісність.
* Поставляється разом з HGS (`/hgs/sdk/hgs.js`) і як файл для копіювання в репозиторій гри
  (рекомендовано: копія в репозиторії, щоб гра не залежала від доступності сервера для завантаження скрипта).

## 9.2. Публічний API

```js
// Створення клієнта. Можна мати кілька незалежних клієнтів, але зазвичай достатньо одного.
const hgs = HGS.createClient({
  serverUrl: "wss://abc.trycloudflare.com",  // або HGS.fromUrlParams().serverUrl
  gameId: "tank-arena",
  auth: { type: "invite", code: "K7Q2-M9XA-PD", name: "Oleh" }, // або {type:"token"} — взяти збережений
  storage: "localStorage",       // де тримати player_token; "memory" — не зберігати
  reconnect: { enabled: true, maxAttempts: 10, baseDelayMs: 500, maxDelayMs: 10000 },
  debug: false,
});

await hgs.connect();                       // Promise: resolve після welcome, reject з HGSError
const room = await hgs.joinRoom("lobby");  // або hgs.joinRoom({ code: "ABCD" })
const rooms = await hgs.listRooms();
const newRoom = await hgs.createRoom({ name: "Друзі", maxPlayers: 4, params: { map: "desert" } });

hgs.send("player_action", { action: "move", dx: 1 });   // ігрова подія (без підтвердження)
hgs.sendTo(playerId, "whisper", { text: "hi" });        // лише relay-режим

hgs.on("game_state", (data, meta) => { /* meta: { from, roomId } */ });
hgs.once("match_start", fn);
hgs.off("game_state", fn);

// Спільний стан кімнати (relay-режим)
await hgs.setRoomState({ turn: 1 });
await hgs.patchRoomState({ score: { p1: 3 } });
hgs.room.state;          // поточний відомий стан
hgs.room.players;        // [{ id, name }]
hgs.room.host;           // id хоста
hgs.isHost;              // boolean

await hgs.leaveRoom();
hgs.disconnect();        // явний вихід — без перепідключення

hgs.player;              // { id, name }
hgs.state;               // "idle" | "connecting" | "authenticating" | "connected"
                         // | "reconnecting" | "disconnected" | "failed"
```

Скорочення для простих ігор (як у технічному завданні) — глобальний клієнт за замовчуванням:

```js
HGS.connect({ serverUrl, gameId, token });  // = HGS.default = createClient(...).connect()
HGS.joinRoom("room-id");
HGS.send("player_action", { action: "move" });
HGS.on("game_state", (state) => { ... });
```

## 9.3. Системні події SDK

Системні події мають префікс `hgs:`, щоб не конфліктувати з подіями гри:

| Подія | Дані |
|-------|------|
| `hgs:state` | `{ state, previous }` — будь-яка зміна стану з'єднання |
| `hgs:connected` | `{ player, resumed }` |
| `hgs:reconnecting` | `{ attempt, delayMs, reason }` |
| `hgs:disconnected` | `{ code, reason, willReconnect }` |
| `hgs:room_joined` | `{ room, players, state, host }` |
| `hgs:player_joined` / `hgs:player_left` | `{ player, reason? }` |
| `hgs:host_changed` | `{ host }` |
| `hgs:room_state` | `{ state }` |
| `hgs:room_closed` | `{ reason }` |
| `hgs:kicked` | `{ reason }` |
| `hgs:server_shutdown` | `{ graceS, reason }` |
| `hgs:error` | `HGSError` |

## 9.4. Помилки

```js
class HGSError extends Error {
  code;        // "unauthorized" | "room_full" | "rate_limited" | "network" | "timeout" | "protocol" | ...
  retryable;   // чи має сенс повторити
  details;
}
```

* Усі `async`-методи відхиляють Promise з `HGSError`; таймаут запиту — 10 с (налаштовується).
* Виклик `send()` без з'єднання: у стані `reconnecting` подія **відкидається** з попередженням у консолі
  (за замовчуванням) — бо застарілі ігрові дії небезпечніші, ніж втрачені. Опція `queueWhileReconnecting`
  дозволяє буферизувати до N подій.
* Перевірка аргументів: назва події (рядок, `^[a-zA-Z0-9_.:-]{1,64}$`), дані серіалізуються в JSON і
  перевіряються на ліміт розміру, отриманий у `welcome`, **до** відправлення.

## 9.5. Перепідключення

1. Розрив (не 4401/4403/явний `disconnect()`) → `reconnecting`.
2. Затримка: експоненційна з випадковим «jitter» (0.5 с → 1 → 2 → … ≤ 10 с).
3. Якщо є `resume_token` і не минуло `resume_window_s` → `hello{auth:{type:"resume"}}`: повернення в ту ж кімнату,
   отримання актуального стану кімнати.
4. Інакше → вхід за збереженим `player_token` → повторне приєднання до останньої кімнати.
5. Після `maxAttempts` → `failed`, гра показує власне повідомлення (SDK UI не малює).
6. Події `online`/`offline` браузера та повернення вкладки з фону прискорюють/відкладають спроби.

## 9.6. Інтеграційні допоміжні функції

```js
HGS.fromUrlParams();
// Читає ?hgs=<serverUrl>&invite=<code>&room=<id> з адреси сторінки,
// після читання прибирає invite з адресного рядка (history.replaceState), щоб код не потрапив у закладки/скріншоти.

HGS.version;  // "1.0.0"
```

Так гравець отримує одне посилання від власника HGS і відкриває гру на GitHub Pages — SDK сам знає, куди
підключатися. Адресу сервера не треба «зашивати» в код гри (адреса Quick Tunnel змінюється).

## 9.7. Python SDK (`hgs_sdk`) — для серверної логіки

Пакет для воркерів (режими `html/logic` і `python/worker`): клас `GameLogic`, об'єкти `Room`, `Player`,
автоматичне підключення до Worker Bus за змінними середовища, heartbeat, збереження/завантаження, тики.
Також буде мінімальний референсний клієнт Worker Protocol на Node.js як приклад для Custom-адаптерів.
