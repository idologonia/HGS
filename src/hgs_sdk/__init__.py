"""hgs_sdk — write the server side of a game for HOME GAME SERVER.

    from hgs_sdk import GameLogic, Room, Player

    class Game(GameLogic):
        tick_rate = 20
        async def on_join(self, room, player): ...
        async def on_event(self, room, player, event, data): ...
        async def on_tick(self, room, dt): room.broadcast("game_state", room.state)

HGS handles networking, login, rooms and security; the game only implements its rules.
"""

from .logic import GameLogic, Player, Room

__version__ = "1.0.0"
__all__ = ["GameLogic", "Room", "Player", "__version__"]
