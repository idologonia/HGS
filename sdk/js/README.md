# hgs.js — JavaScript SDK для HOME GAME SERVER

Один файл без залежностей. Скопіюйте `hgs.js` у репозиторій своєї гри поруч з `index.html`
і підключіть звичайним тегом:

```html
<script src="hgs.js"></script>
```

HGS також роздає SDK за адресою `http(s)://<сервер>/hgs/sdk/hgs.js`, але копія в репозиторії надійніша:
гра завантажиться, навіть якщо сервер вимкнено.

## Найпростіше: `HGS.start`

```js
const hgs = await HGS.start({ gameId: "my-game", server: "github:me/hgs-address" });
hgs.on("game_state", (state) => draw(state));
hgs.send("input", { dx: 1 });
```

Сам знаходить сервер (адресу публікує HGS у файл `hgs-server.json` вашого репозиторію), показує вікно
реєстрації/входу лише першого разу, пам'ятає гравця, заходить у кімнату, показує «сервер вимкнено», якщо HGS
не працює. Параметри: `gameId`, `server` (`"github:owner/repo[/file.json]"` або `ws(s)://...`), `room`, `lang`.
`?hgs=ws://127.0.0.1:8765` в адресі сторінки має пріоритет (для локальних тестів). Гра має використовувати
`"auth": {"mode": "account"}`.

## Швидкий старт (ручне керування)

```js
const params = HGS.fromUrlParams();            // ?hgs=<адреса сервера>&invite=<код>&room=<id>&name=<ім'я>
const hgs = HGS.createClient({
  serverUrl: params.serverUrl,                 // "wss://xxx.trycloudflare.com" або "ws://127.0.0.1:8765"
  gameId: "my-game",                           // id з hgs-game.json
  auth: params.invite
    ? { type: "invite", code: params.invite, name: "Oleh" }
    : { type: "open", name: "Oleh" },          // "open" — лише для локальних тестів
});

hgs.on("hgs:state", ({ state }) => console.log("стан:", state));
hgs.on("game_state", (data, meta) => draw(data));   // подія від сервера гри або іншого гравця

await hgs.connect();
await hgs.joinRoom(params.room || "lobby");
hgs.send("player_action", { action: "move", dx: 1 });
```

Після першого входу за кодом запрошення SDK зберігає токен гравця в `localStorage`.
Наступного разу (і під час автоматичного перепідключення) код уже не потрібен.

## API

| Виклик | Що робить |
|--------|-----------|
| `HGS.createClient(options)` | новий клієнт (див. параметри нижче) |
| `client.connect()` → `Promise<player>` | підключитися й увійти |
| `client.disconnect()` | вийти без перепідключення |
| `client.listRooms()` → `Promise<room[]>` | відкриті кімнати гри |
| `client.createRoom({ name, maxPlayers, private, params })` | створити кімнату (якщо гра дозволяє) |
| `client.joinRoom(id)` → `Promise<room>` | увійти в кімнату |
| `client.leaveRoom()` | вийти з кімнати |
| `client.send(event, data)` | ігрова подія всім у кімнаті (relay) або серверу гри (logic) |
| `client.sendTo(playerId, event, data)` | подія одному гравцю (лише relay) |
| `client.setRoomState(state)` / `patchRoomState(patch)` | спільний стан кімнати (relay, лише хост) |
| `client.on(event, fn)` / `once` / `off` | підписка на події; `on` повертає функцію відписки |
| `client.state` | `idle`, `connecting`, `authenticating`, `connected`, `reconnecting`, `disconnected`, `failed` |
| `client.player`, `client.room`, `client.isHost`, `client.game` | поточні дані |
| `HGS.fromUrlParams()` | читає `hgs`, `invite`, `room`, `name` з адреси; прибирає код запрошення з адресного рядка |
| `HGS.connect(options)`, `HGS.send(...)`, `HGS.on(...)`, … | скорочення з одним глобальним клієнтом |

Параметри `createClient`: `serverUrl`, `gameId`, `auth` (`{type:"invite",code,name}`, `{type:"open",name}`,
`{type:"token",token}`), `storage` (`"localStorage"` або `"memory"`), `reconnect`
(`{enabled, maxAttempts, baseDelayMs, maxDelayMs}`), `requestTimeoutMs`, `queueWhileReconnecting` (скільки подій
тримати в черзі під час перепідключення; типово 0 — застарілі дії відкидаються), `debug`.

## Системні події

`hgs:state`, `hgs:connected`, `hgs:reconnecting`, `hgs:disconnected`, `hgs:room_joined`, `hgs:player_joined`,
`hgs:player_left`, `hgs:host_changed`, `hgs:room_state`, `hgs:room_closed`, `hgs:kicked`, `hgs:server_shutdown`,
`hgs:error`. Ігрові події мають власні назви гри (без префікса `hgs:`).

## Помилки

Усі помилки — `HGS.HGSError` з полями `code` (`unauthorized`, `room_full`, `room_not_found`, `rate_limited`,
`too_large`, `timeout`, `network`, `bad_argument`, …) і `retryable`. Некоректна назва події чи завелике
повідомлення відхиляються ще до відправлення.

## Перепідключення

Після розриву SDK сам перепідключається з паузами 0.5 → 1 → 2 → … ≤ 10 с, входить знову за токеном і повертається
в ту саму кімнату. Не перепідключається, якщо гравця вилучили (kick), увійшли тим самим гравцем в іншому місці
або вхід відхилено.
