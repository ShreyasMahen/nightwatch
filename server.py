"""Run the trusted moderator. Clients must have distinct Linux UIDs."""

import argparse
import asyncio
import signal

from comms import Hub
from game import Game


async def main(args):
    game = Game(args.players.split(","), args.night, args.discussion, args.vote, args.seed)
    hub = Hub(args.socket, game.roster, game.joined, game.message, game.left)
    game.hub = hub
    await hub.start()
    print(f"NIGHTWATCH moderator ready at {args.socket}", flush=True)
    print("Roster: " + ", ".join(game.roster), flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await game.close()
        await hub.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/run/nightwatch/game.sock")
    parser.add_argument("--players", default="p1,p2,p3,p4,p5")
    parser.add_argument("--night", type=float, default=60)
    parser.add_argument("--discussion", type=float, default=45)
    parser.add_argument("--vote", type=float, default=30)
    parser.add_argument("--seed", type=int, help="Reproducible demonstrations only; omit for real games")
    arguments = parser.parse_args()
    if min(arguments.night, arguments.discussion, arguments.vote) <= 0:
        parser.error("Phase lengths must be positive")
    asyncio.run(main(arguments))
