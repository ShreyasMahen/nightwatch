"""Trusted moderator, with private crash-recovery checkpoints."""

import argparse
import asyncio
import signal

from checkpoint import Checkpoint, CheckpointError
from comms import Hub
from game import Game


async def main(args):
    store = Checkpoint(args.checkpoint)
    store.acquire()
    hub = game = None
    waiters = []
    try:
        record = store.load()
        roster = args.players.split(',')
        if record is not None:
            game = Game.restore(record, store)
            if game.roster != roster:
                raise CheckpointError('Saved roster differs; resume the original roster or start a new session')
        else:
            game = Game(roster, args.night, args.discussion, args.vote, args.seed, store)
        hub = Hub(args.socket, game.roster, game.joined, game.message, game.left, recover_stale=True)
        game.hub = hub
        await hub.start()
        if record is not None:
            game.resume()
        else:
            with game.atomic():
                pass
        print(f'NIGHTWATCH moderator ready at {args.socket}', flush=True)
        print('Roster: ' + ', '.join(game.roster), flush=True)
        print('Voting requires a strict majority of living players.', flush=True)
        print('Private checkpoint: ' + args.checkpoint, flush=True)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(game.failed.wait())]
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if game.failed.is_set():
            raise CheckpointError('A state transaction failed; moderator stopped without acknowledging it')
    finally:
        for task in waiters:
            task.cancel()
        if waiters:
            await asyncio.gather(*waiters, return_exceptions=True)
        if game:
            await game.close()
        if hub:
            await hub.close()
        store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', default='/run/nightwatch/game.sock')
    parser.add_argument('--checkpoint', default='/var/lib/nightwatch/checkpoint.json')
    parser.add_argument('--players', default='p1,p2,p3,p4,p5')
    parser.add_argument('--night', type=float, default=60)
    parser.add_argument('--discussion', type=float, default=45)
    parser.add_argument('--vote', type=float, default=30)
    parser.add_argument('--seed', type=int, help='Reproducible demonstrations only')
    arguments = parser.parse_args()
    if min(arguments.night, arguments.discussion, arguments.vote) <= 0:
        parser.error('Phase lengths must be positive')
    try:
        asyncio.run(main(arguments))
    except (CheckpointError, OSError, ValueError) as exc:
        parser.exit(1, str(exc) + '\n')
