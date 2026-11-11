"""Server logic of the demo arena: the server owns every position (clients send only input).

This is an infrastructure test for HGS, not a template other games must follow.
"""

import random

from hgs_sdk import GameLogic

WIDTH, HEIGHT, RADIUS = 800, 500, 14
COLORS = ["#4f8cff", "#3ecf8e", "#f2c94c", "#ff5c5c", "#b67dff", "#ff9f43", "#2dd4bf", "#f472b6"]


class Game(GameLogic):
    tick_rate = 20

    def __init__(self):
        super().__init__()
        self.total_joins = 0  # survives restarts through on_save / on_load

    async def on_load(self, data):
        self.total_joins = int(data.get("total_joins", 0))

    async def on_save(self):
        return {"total_joins": self.total_joins}

    async def on_join(self, room, player):
        if room.state is None:
            room.state = {}
        self.total_joins += 1
        room.state[player.id] = {
            "id": player.id, "name": player.name,
            "x": random.randint(RADIUS, WIDTH - RADIUS), "y": random.randint(RADIUS, HEIGHT - RADIUS),
            "dx": 0, "dy": 0, "color": COLORS[self.total_joins % len(COLORS)],
        }
        self.set_status(total_joins=self.total_joins)
        self.log.info("%s joined %s", player.name, room.name)

    async def on_event(self, room, player, event, data):
        if event == "input" and isinstance(data, dict):
            me = room.state.get(player.id)
            if me is not None:
                me["dx"] = max(-1, min(1, int(data.get("dx", 0))))
                me["dy"] = max(-1, min(1, int(data.get("dy", 0))))

    async def on_leave(self, room, player, reason):
        if room.state:
            room.state.pop(player.id, None)

    async def on_tick(self, room, dt):
        speed = float(self.config.get("speed", 200))
        for p in room.state.values():
            p["x"] = max(RADIUS, min(WIDTH - RADIUS, p["x"] + p["dx"] * speed * dt))
            p["y"] = max(RADIUS, min(HEIGHT - RADIUS, p["y"] + p["dy"] * speed * dt))
        room.broadcast("game_state", {
            "players": [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in p.items() if k not in ("dx", "dy")}
                        for p in room.state.values()],
            "total_joins": self.total_joins,
        })
