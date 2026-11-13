"""Interactive text client or optional demonstration bot for Nightwatch."""

import argparse
import asyncio
import contextlib
import os
import sys

from comms import Client, ProtocolError

HELP = """Commands (player names are p1, p2, ...):
  status                        current phase and living players
  say your message              public day discussion (broadcast)
  whisper p2 your message       one-to-one private day message
  whisper p2,p3 your message    private message to a selected group
  team your message             wolves-only night chat
  strike p2                     wolf's night attack
  inspect p2                    seer's night investigation
  protect p2                    guard's night protection; self allowed
  vote p2                       day vote to eliminate another player
  pass                          abstain from this night action / vote
  help                          show commands
  quit                          disconnect (rejoin as the same Linux user)
Choices may change until the phase deadline. A strict majority of living players is required to eliminate someone.
"""


def parse_command(line, phase_id):
    parts = line.strip().split(maxsplit=1)
    if not parts:
        return None
    cmd, rest = parts[0].lower(), parts[1] if len(parts) == 2 else ""
    message = {"cmd": cmd, "phase_id": phase_id}
    if cmd in ("say", "team"):
        message["text"] = rest
    elif cmd == "whisper":
        names, _, text = rest.partition(" ")
        message.update(to=names.split(","), text=text)
    elif cmd in ("strike", "inspect", "protect", "vote"):
        message["target"] = rest
    return message


class Console:
    def __init__(self, client, bot=False):
        self.client, self.bot = client, bot
        self.user, self.role, self.team = "", "", []
        self.phase_id, self.alive = 0, []
        self.suspects, self.inspected = set(), set()
        self.last_protected = None
        self.done = asyncio.Event()

    def display(self, message):
        kind = message.get("kind")
        if kind == "welcome":
            self.user = message["user"]
            print(f"IDENTITY: {self.user} | Linux UID {message['uid']} | PID {message['pid']}", flush=True)
        elif kind == "role":
            self.role, self.team = message["role"], message["team"]
            print("[PRIVATE] " + message["text"] +
                  (" Team: " + ", ".join(self.team) if self.team else ""), flush=True)
        elif kind == "state":
            self.phase_id, self.alive = message["phase_id"], message["alive"]
            print(f"\n=== ROUND {message['round']} | {message['phase'].upper()} "
                  f"| {message['seconds']:g}s ===\n"
                  f"Alive: {', '.join(self.alive)}", flush=True)
        elif kind == "chat":
            print(f"[{message['scope']}] {message['sender']}: {message['text']}", flush=True)
        elif kind == "result":
            print("\n" + message["text"], flush=True)
            print("Final roles: " + ", ".join(f"{p}={r}" for p, r in message["roles"].items()), flush=True)
        else:
            print(f"[{kind.upper()}] {message.get('text', '')}", flush=True)

    async def command(self, line):
        print(f"> {line}", flush=True)
        await self.client.send(parse_command(line, self.phase_id))

    async def bot_action(self, message):
        if message["kind"] == "inspection":
            self.inspected.add(message["target"])
            if message["alignment"] == "wolf":
                self.suspects.add(message["target"])
        if message["kind"] == "chat" and message["text"].startswith("SEER: "):
            tokens = message["text"].split()
            if len(tokens) == 4 and tokens[2:] == ["is", "wolf"]:
                self.suspects.add(tokens[1])
        if message["kind"] != "state" or self.user not in self.alive:
            return
        phase = message["phase"]
        others = [p for p in self.alive if p != self.user]
        if phase == "night":
            if self.role == "wolf":
                await self.command("team Let's agree on a target before the deadline.")
                targets = [p for p in others if p not in self.team]
                await self.command("strike " + targets[-1])
            elif self.role == "seer":
                targets = [p for p in others if p not in self.inspected] or others
                await self.command("inspect " + targets[0])
            elif self.role == "guard":
                targets = [p for p in self.alive if p != self.last_protected]
                self.last_protected = targets[0]
                await self.command("protect " + targets[0])
        elif phase == "discussion":
            found = sorted(self.suspects & set(others))
            if self.role == "seer" and found:
                await self.command(f"say SEER: {found[0]} is wolf")
            else:
                await self.command("say Let's compare evidence before we vote.")
            if len(others) >= 2:
                await self.command("whisper " + ",".join(others[:2]) +
                                   " Can we compare our suspicions privately?")
        elif phase == "vote":
            found = sorted(self.suspects & set(others))
            targets = [p for p in others if p not in self.team] or others
            await self.command("vote " + (found[0] if found and self.role != "wolf" else targets[0]))

    async def receive(self):
        try:
            while True:
                message = await self.client.receive()
                self.display(message)
                if self.bot:
                    await self.bot_action(message)
                if message.get("kind") == "result":
                    break
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ProtocolError) as exc:
            print(f"Connection closed: {type(exc).__name__}. "
                  "Reconnect with the same Linux user if the game is still running.", flush=True)
        finally:
            self.done.set()

    async def keyboard(self):
        reader = asyncio.StreamReader()
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )
        try:
            while not self.done.is_set():
                line = (await reader.readline()).decode().strip()
                if not line or line == "quit":
                    if reader.at_eof() or line == "quit":
                        break
                    continue
                if line == "help":
                    print(HELP, flush=True)
                    continue
                await self.client.send(parse_command(line, self.phase_id))
        finally:
            transport.close()
            self.done.set()


async def main(args):
    client = await Client.connect(args.socket)
    console = Console(client, args.bot)
    print("NIGHTWATCH | Signal & Shadow", flush=True)
    print(f"Local process PID {os.getpid()} / UID {os.getuid()}", flush=True)
    print("PID namespace: " + os.readlink("/proc/self/ns/pid"), flush=True)
    print("Demonstration bot enabled." if args.bot else HELP, flush=True)
    tasks = [asyncio.create_task(console.receive())]
    if not args.bot:
        tasks.append(asyncio.create_task(console.keyboard()))
    await console.done.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/run/nightwatch/game.sock")
    parser.add_argument("--bot", action="store_true", help="Run a transparent, non-learning demo bot")
    try:
        asyncio.run(main(parser.parse_args()))
    except (ConnectionError, FileNotFoundError, PermissionError, ProtocolError) as exc:
        sys.exit(str(exc))
    except KeyboardInterrupt:
        pass
