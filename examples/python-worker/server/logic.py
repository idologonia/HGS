"""Python adapter, worker mode: a shared click counter per room, saved between restarts.

Runs with the game's own virtualenv (.venv) if there is one; hgs_sdk is provided by HGS.
"""

from hgs_sdk import GameLogic


class Game(GameLogic):
    def __init__(self):
        super().__init__()
        self.best = 0

    async def on_load(self, data):
        self.best = data.get("best", 0)

    async def on_save(self):
        return {"best": self.best}

    async def on_join(self, room, player):
        room.state = room.state or {"count": 0}
        room.send(player, "count", {"count": room.state["count"], "best": self.best, "goal": self.config.get("goal")})

    async def on_event(self, room, player, event, data):
        if event == "click":
            room.state["count"] += 1
            self.best = max(self.best, room.state["count"])
            room.broadcast("count", {"count": room.state["count"], "best": self.best, "by": player.name})
