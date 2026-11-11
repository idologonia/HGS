"""An existing Python game server with its own port and protocol (a tiny chat).

HGS starts/stops it, collects its logs and forwards players to it through
ws(s)://<hgs address>/hgs/v1/proxy/py-chat/ — the server itself knows nothing about HGS.
Needs the `websockets` package in the game's virtualenv (see requirements.txt).
"""

import argparse
import asyncio
import json

import websockets

clients = set()


async def handler(ws):
    clients.add(ws)
    try:
        async for message in ws:
            text = json.dumps({"chat": str(message)[:500]})
            for c in list(clients):
                try:
                    await c.send(text)
                except websockets.ConnectionClosed:
                    pass
    finally:
        clients.discard(ws)


async def main(port: int) -> None:
    async with websockets.serve(handler, "127.0.0.1", port):
        print(f"chat server listening on {port}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    asyncio.run(main(parser.parse_args().port))
