# Як підключити нову гру до HGS

Ця інструкція — для гри, яку ви створюєте з Claude і публікуєте на GitHub Pages.
(Наявну гру підключаємо за окремим процесом: [12-game-integration.md](../architecture/12-game-integration.md).)

## 1. Оберіть режим сервера

| Режим | Коли | Що пише розробник |
|-------|------|-------------------|
| `relay` | кооператив, казуальні ігри, прототипи; логіку рахують браузери | лише клієнт (HTML/JS) |
| `logic` | змагальні ігри, де сервер має бути «суддею» | клієнт + `server/logic.py` |

У `relay` сервер не перевіряє правила, тож гравець, який змінить код у браузері, може змахлювати.

## 2. Структура репозиторію гри

```
my-game/
├── index.html          гра (GitHub Pages)
├── hgs.js              копія sdk/js/hgs.js з репозиторію HGS
├── hgs-game.json       опис гри для HGS
└── server/logic.py     лише для режиму logic
```

## 3. `hgs-game.json`

```json
{
  "id": "my-game",
  "name": "My Game",
  "version": "1.0.0",
  "adapter": "html",
  "backend": { "mode": "logic", "module": "server/logic.py", "class": "Game" },
  "auth": { "mode": "invite" },
  "client": { "url": "https://YOUR-NAME.github.io/my-game/",
              "allowed_origins": ["https://YOUR-NAME.github.io", "http://localhost:5500"] },
  "rooms": { "max_players": 8, "default_rooms": ["lobby"] },
  "saves": { "enabled": true, "interval_s": 300, "keep": 10 }
}
```

Для `relay`: `"backend": { "mode": "relay" }`. Для тестів на своєму ПК без кодів: `"auth": { "mode": "open" }`.
Повний опис полів — [07-config-and-data.md](../architecture/07-config-and-data.md).

## 4. Клієнт (index.html)

```html
<script src="hgs.js"></script>
<script>
  const params = HGS.fromUrlParams();
  const hgs = HGS.createClient({
    serverUrl: params.serverUrl, gameId: "my-game",
    auth: { type: "invite", code: params.invite, name: prompt("Ваше ім'я") },
  });
  hgs.on("game_state", (state) => render(state));
  hgs.connect().then(() => hgs.joinRoom(params.room || "lobby"));
  // при дії гравця:
  hgs.send("input", { dx: 1, dy: 0 });
</script>
```

Правило для одиночного режиму: якщо в адресі немає `?hgs=`, гра може працювати офлайн як раніше.
Довідка по SDK — [sdk/js/README.md](../../sdk/js/README.md).

## 5. Сервер (режим logic): `server/logic.py`

```python
from hgs_sdk import GameLogic

class Game(GameLogic):
    tick_rate = 20                                   # on_tick 20 разів на секунду

    async def on_join(self, room, player):
        room.state = room.state or {}
        room.state[player.id] = {"x": 100, "y": 100}

    async def on_event(self, room, player, event, data):
        if event == "input":
            room.state[player.id]["x"] += data.get("dx", 0)

    async def on_leave(self, room, player, reason):
        room.state.pop(player.id, None)

    async def on_tick(self, room, dt):
        room.broadcast("game_state", room.state)

    async def on_save(self):                         # опційно: що зберегти між запусками
        return {"version": 1}

    async def on_load(self, data): ...
```

Код логіки працює в окремому процесі. Доступні стандартна бібліотека Python і `hgs_sdk`.
Помилка в обробнику записується в лог гри і не зупиняє сервер.
`room.state` належить грі — HGS не заглядає всередину.

Корисне: `room.broadcast(event, data, exclude=player)`, `room.send(player, event, data)`, `room.kick(player)`,
`room.close()`, `room.update(status="locked")`, `self.set_status(matches=3)` (показується в HGS),
`self.config` (поле `backend.config` з маніфесту), `self.log.info(...)`.
Повернути `False` або рядок з `on_join` — відмовити гравцю.

## 6. Запуск

1. Клонуйте репозиторій гри на ПК.
2. HGS → **Games** → **+ Додати гру** → виберіть папку гри → **Start**.
3. Перевірка на одному ПК: відкрийте гру з параметрами `?hgs=ws://127.0.0.1:8765&name=Ann`
   (для режиму `invite` — створіть код кнопкою **Invite** і додайте `&invite=КОД`).
4. Гра з друзями через інтернет — після Етапу 4 (тунель + посилання-запрошення).

Готовий приклад: [examples/demo-arena](../../examples/demo-arena/README.md).

## Підказка для Claude

> Зроби гру мультиплеєрною через HGS (HOME GAME SERVER): підключи `hgs.js`, адресу сервера бери з
> `HGS.fromUrlParams()`, вхід — за кодом запрошення з параметра `invite`. Додай `hgs-game.json`
> (adapter "html", mode "logic") і серверну логіку `server/logic.py` на основі `hgs_sdk.GameLogic`.
> Сервер — авторитетний: клієнт шле тільки дії гравця, сервер розсилає стан. Без ?hgs= гра працює офлайн.
