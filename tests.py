"""Rule and real-socket tests; run with python3 -m unittest -v tests."""

import asyncio
import contextlib
import json
import os
import pwd
import struct
import tempfile
import unittest

from comms import Client, Hub, MAX_FRAME, ProtocolError, encode, read_frame
from game import Game, RuleError


class FakeHub:
    def __init__(self):
        self.peers = dict.fromkeys(["p1", "p2", "p3", "p4", "p5"])
        self.sent = []

    def send(self, name, message):
        self.sent.append((name, message))
        return name in self.peers

    def multicast(self, names, message):
        return sum(self.send(p, message) for p in set(names))

    def broadcast(self, message):
        return self.multicast(self.peers, message)


class RulesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.game = Game(["p1", "p2", "p3", "p4", "p5"], night=999, discussion=999, vote=999)
        self.game.hub = FakeHub()
        self.game.roles = {"p1": "wolf", "p2": "seer", "p3": "guard",
                           "p4": "villager", "p5": "villager"}
        self.game.phase, self.game.phase_id, self.game.round = "night", 1, 1

    async def asyncTearDown(self):
        await self.game.close()

    def command(self, name, cmd, **extra):
        return self.game.command(name, {"cmd": cmd, "phase_id": self.game.phase_id, **extra})

    async def test_role_authorization(self):
        with self.assertRaises(RuleError):
            self.command("p4", "strike", target="p2")

    async def test_stale_phase_rejected(self):
        with self.assertRaises(RuleError):
            self.game.command("p1", {"cmd": "strike", "phase_id": 0, "target": "p2"})

    async def test_guard_blocks_attack(self):
        self.command("p1", "strike", target="p2")
        self.command("p3", "protect", target="p2")
        self.game.resolve_night()
        self.assertIn("p2", self.game.alive)
        self.assertEqual(self.game.phase, "discussion")

    async def test_guard_cannot_repeat(self):
        self.game.last_protected = "p2"
        with self.assertRaises(RuleError):
            self.command("p3", "protect", target="p2")

    async def test_inspection_is_unicast(self):
        self.command("p2", "inspect", target="p1")
        self.game.resolve_night()
        private = [(p, m) for p, m in self.game.hub.sent if m["kind"] == "inspection"]
        self.assertEqual(len(private), 1)
        self.assertEqual(private[0][0], "p2")
        self.assertEqual(private[0][1]["alignment"], "wolf")

    async def test_whisper_selected_recipients_only(self):
        self.game.phase = "discussion"
        self.command("p4", "whisper", to=["p2", "p3"], text="Compare evidence")
        self.assertEqual({p for p, _ in self.game.hub.sent}, {"p2", "p3", "p4"})
        self.assertTrue(all(m["sender"] == "p4" for _, m in self.game.hub.sent))

    async def test_claimed_sender_is_ignored(self):
        self.game.phase = "discussion"
        self.command("p4", "say", text="hello", sender="p1")
        self.assertTrue(all(m["sender"] == "p4" for _, m in self.game.hub.sent))

    async def test_vote_tie_eliminates_nobody(self):
        self.game.phase = "vote"
        self.command("p1", "vote", target="p2")
        self.command("p2", "vote", target="p1")
        self.game.resolve_vote()
        self.assertEqual(len(self.game.alive), 5)

    async def test_dead_player_cannot_chat(self):
        self.game.phase = "discussion"
        self.game.alive.remove("p4")
        with self.assertRaises(RuleError):
            self.command("p4", "say", text="I saw the wolf")

    async def test_no_client_chosen_winner(self):
        with self.assertRaises(RuleError):
            self.command("p4", "win", target="p1")

    async def test_control_characters_rejected(self):
        self.game.phase = "discussion"
        with self.assertRaises(RuleError):
            self.command("p4", "say", text="\x1b[2J")

    async def test_malformed_command_type(self):
        with self.assertRaises(RuleError):
            self.game.command("p1", {"cmd": [], "phase_id": 1})

    async def test_village_and_wolf_win_conditions(self):
        self.game.alive.remove("p1")
        self.assertTrue(self.game.finished())
        self.assertEqual(self.game.phase, "finished")
        self.assertTrue(any(m.get("winner") == "VILLAGE" for _, m in self.game.hub.sent))
        self.game.alive = {"p1", "p2"}
        self.assertTrue(self.game.finished())
        self.assertTrue(any(m.get("winner") == "WOLVES" for _, m in self.game.hub.sent))


class FramingTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_frames_and_back_to_back_frames(self):
        reader = asyncio.StreamReader()
        frames = encode({"cmd": "status"}) + encode({"cmd": "pass"})
        async def feed():
            for byte in frames:
                reader.feed_data(bytes([byte]))
                await asyncio.sleep(0)
        task = asyncio.create_task(feed())
        self.assertEqual(await read_frame(reader), {"cmd": "status"})
        self.assertEqual(await read_frame(reader), {"cmd": "pass"})
        await task

    async def test_oversized_header_rejected_before_body(self):
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("!I", MAX_FRAME + 1))
        with self.assertRaises(ProtocolError):
            await read_frame(reader)

    async def test_invalid_json_and_non_object_rejected(self):
        for body in (b"not JSON", b"[]", b"null", b"\xff"):
            reader = asyncio.StreamReader()
            reader.feed_data(struct.pack("!I", len(body)) + body)
            with self.assertRaises(ProtocolError):
                await read_frame(reader)

    async def test_truncated_frame_is_disconnect(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b"\0\0")
        reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            await read_frame(reader)


@unittest.skipUnless(hasattr(__import__("socket"), "SO_PEERCRED"), "Linux required")
class RealSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nw-test-")
        self.path = os.path.join(self.temp.name, "game.sock")
        self.name = pwd.getpwuid(os.getuid()).pw_name
        self.messages, self.identities = [], []
        async def joined(name, pid, uid):
            self.identities.append((name, pid, uid))
        async def message(name, data):
            self.messages.append((name, data))
        async def left(name):
            pass
        self.hub = Hub(self.path, [self.name], joined, message, left)
        await self.hub.start()
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        await self.hub.close()
        self.temp.cleanup()

    async def connect(self):
        client = await Client.connect(self.path)
        self.clients.append(client)
        return client

    async def test_kernel_credentials_and_private_delivery(self):
        client = await self.connect()
        welcome = await client.receive()
        self.assertEqual(welcome["uid"], os.getuid())
        self.assertEqual(welcome["pid"], os.getpid())
        self.hub.send(self.name, {"kind": "private", "text": "secret"})
        self.assertEqual((await client.receive())["text"], "secret")

    async def test_duplicate_user_rejected(self):
        first = await self.connect()
        await first.receive()
        second = await self.connect()
        self.assertIn("already", (await second.receive())["text"])

    async def test_unknown_user_rejected(self):
        self.hub.allowed.clear()
        client = await self.connect()
        self.assertIn("roster", (await client.receive())["text"])

    async def test_eof_then_reconnect(self):
        client = await self.connect()
        await client.receive()
        await client.close()
        for _ in range(100):
            if self.name not in self.hub.peers:
                break
            await asyncio.sleep(0.01)
        second = await self.connect()
        self.assertEqual((await second.receive())["kind"], "welcome")

    async def test_socket_group_permissions(self):
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o660)


if __name__ == "__main__":
    unittest.main(verbosity=2)
