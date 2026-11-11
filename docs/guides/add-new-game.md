# Як підключити гру до HGS (без посилань для друзів)

Підсумок: друзі відкривають **звичайну адресу гри** на GitHub Pages, **один раз реєструються** (ім'я + пароль)
і далі просто грають. Усе інше — на вашому ПК в HGS.

## Один раз: репозиторій для адреси сервера

Адреса вашого сервера змінюється (безкоштовний тунель), тому HGS сам записує актуальну адресу у файл на GitHub,
а ігри читають його.

1. На GitHub створіть **публічний** репозиторій, наприклад `hgs-address` (можна з README, більше нічого не треба).
2. Створіть токен: GitHub → аватар → **Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token**.
   * Repository access: **Only select repositories** → `hgs-address`.
   * Permissions → Repository permissions → **Contents: Read and write**. Більше нічого.
   * Expiration: наприклад, 1 рік. Скопіюйте токен.
3. HGS → **Network** → блок «Автоматична адреса для ігор»:
   * поставте «Публікувати адресу», репозиторій `ваш-логін/hgs-address`;
   * **Вказати токен GitHub…** → вставте токен (зберігається зашифрованим);
   * **Застосувати**.
4. Там само: режим **Tunnel** → **Запустити тунель**. HGS сам оновить адресу на GitHub
   (і позначить «офлайн», коли ви зупините тунель або закриєте HGS).

Адреса сервера в цьому файлі не є секретом: щоб грати, все одно потрібен акаунт.

## Для кожної гри: адаптація (в чаті, де робиться гра)

Вставте Claude в тому чаті (замініть `ВАШ-ЛОГІН`, `назва-гри`):

> Зроби цю гру мультиплеєрною через HGS (HOME GAME SERVER, https://github.com/idologonia/HGS).
> 1. Скопіюй у репозиторій гри файл `sdk/js/hgs.js` з репозиторію HGS і підключи його `<script src="hgs.js">`.
> 2. Підключення — одним викликом:
>    `const hgs = await HGS.start({ gameId: "назва-гри", server: "github:ВАШ-ЛОГІН/hgs-address" });`
>    SDK сам знайде сервер, сам покаже вікно реєстрації/входу (лише першого разу) і зайде в кімнату "lobby".
>    Далі: `hgs.send("подія", дані)` і `hgs.on("подія", (дані, meta) => ...)`. Довідка: `sdk/js/README.md` у HGS.
> 3. Додай у корінь `hgs-game.json`: `"id": "назва-гри"`, `"adapter": "html"`,
>    `"backend": {"mode": "logic", "module": "server/logic.py", "class": "Game"}`, `"auth": {"mode": "account"}`,
>    `"client": {"url": "https://ВАШ-ЛОГІН.github.io/назва-гри/", "allowed_origins": ["https://ВАШ-ЛОГІН.github.io"]}`.
> 4. Серверну логіку напиши в `server/logic.py` на основі `hgs_sdk.GameLogic` (див. `docs/guides/add-new-game.md`
>    у HGS). Сервер — головний: клієнт надсилає лише дії гравця, сервер розсилає стан гри.
> 5. Не змінюй механіки, баланс, дизайн і збереження. Якщо сервер недоступний — гра працює офлайн, як зараз.
>    Спершу покажи план змін і перелік файлів і дочекайся мого погодження.

Або дайте назву репозиторію гри мені в чаті HGS — я проаналізую гру і запропоную план.

## Для кожної гри: додати в HGS

1. Скачайте репозиторій гри: **Code → Download ZIP**, розпакуйте (наприклад, `Документи\Ігри\назва-гри`).
   Після оновлення гри скачайте ZIP знову і замініть папку (HGS → картка гри → **Stop**, замінити, **Start**).
2. HGS → **Games → + Додати гру** → виберіть цю папку → **Start**.
3. Готово. Друзям надсилаєте просто адресу гри: `https://ВАШ-ЛОГІН.github.io/назва-гри/`.

Перевірка на своєму ПК без тунелю: відкрийте гру з `?hgs=ws://127.0.0.1:8765` в кінці адреси.

## Як виглядає для друга

1. Відкриває `https://ВАШ-ЛОГІН.github.io/назва-гри/`.
2. Першого разу — вікно «Реєстрація»: ім'я + пароль (від 6 символів) → **Грати**.
3. Наступного разу — одразу гра (браузер пам'ятає вхід).
4. Якщо HGS у вас вимкнений — «Сервер гри зараз вимкнено» з кнопкою «Спробувати ще раз».

Керування гравцями (відключити, заблокувати) — HGS → **Players**.

## Довідка для розробника гри

### Клієнт

```js
const hgs = await HGS.start({ gameId: "my-game", server: "github:me/hgs-address" });
hgs.on("game_state", (state) => render(state));
hgs.send("input", { dx: 1, dy: 0 });
```

`HGS.start` приймає також `room` (типово `"lobby"`) і `lang` (`"uk"`/`"en"`).

### Сервер: `server/logic.py`

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

Код логіки працює в окремому процесі; доступні стандартна бібліотека Python і `hgs_sdk`. Помилка в обробнику
записується в лог гри і не зупиняє сервер. Корисне: `room.broadcast(event, data, exclude=player)`,
`room.send(player, event, data)`, `room.kick(player)`, `room.close()`, `self.set_status(...)`, `self.config`,
`self.log.info(...)`. Повернути `False` або рядок з `on_join` — відмовити гравцю.

Варіант без серверного коду (`"backend": {"mode": "relay"}`): сервер лише пересилає події між гравцями кімнати;
простіше, але сервер не перевіряє правила.

Готовий приклад: [examples/demo-arena](../../examples/demo-arena/README.md).
