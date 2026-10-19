"""All inter-user IPC for Nightwatch: framed JSON over async UNIX sockets.

Linux only. Identity comes from SO_PEERCRED, never from a claimed username.
No game rules belong in this module. See README.md for the public API.
"""

import asyncio
import contextlib
import json
import os
import pwd
import socket
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

MAX_FRAME = 16_384
QUEUE_SIZE = 64
IO_TIMEOUT = 3


class ProtocolError(Exception):
    """A peer supplied an invalid frame or message."""


def encode(message):
    if not isinstance(message, dict):
        raise ProtocolError("Messages must be JSON objects")
    body = json.dumps(message, ensure_ascii=True, allow_nan=False).encode("utf-8")
    if not 0 < len(body) <= MAX_FRAME:
        raise ProtocolError("Frame exceeds 16384 bytes")
    return struct.pack("!I", len(body)) + body


async def read_frame(reader):
    """Read a length-prefixed object, including across partial socket reads."""
    header = await reader.readexactly(4)
    size = struct.unpack("!I", header)[0]
    if not 0 < size <= MAX_FRAME:
        raise ProtocolError("Invalid frame length")
    try:
        body = await asyncio.wait_for(reader.readexactly(size), IO_TIMEOUT)
        message = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError, asyncio.TimeoutError) as exc:
        raise ProtocolError("Malformed or incomplete JSON body") from exc
    if not isinstance(message, dict):
        raise ProtocolError("Messages must be JSON objects")
    return message


@dataclass
class Peer:
    name: str
    pid: int
    uid: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    outgoing: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))
    arrivals: deque = field(default_factory=deque)


class Hub:
    """Authenticated broker with unicast, multicast and broadcast delivery.

    Callbacks are async functions: on_join(name, pid, uid),
    on_message(name, message), and on_leave(name). Outgoing methods enqueue
    synchronously, so one slow reader cannot block game state transitions.
    """

    def __init__(self, path, allowed_users, on_join, on_message, on_leave):
        if not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("Nightwatch requires Linux SO_PEERCRED; use Docker on macOS")
        self.path = Path(path)
        self.allowed = set(allowed_users)
        self.on_join, self.on_message, self.on_leave = on_join, on_message, on_leave
        self.peers = {}
        self.server = None
        self.tasks = set()

    async def start(self):
        # Refuse an occupied path. Never unlink an arbitrary pre-existing file.
        if self.path.exists() or self.path.is_symlink():
            raise FileExistsError(f"Socket path already exists: {self.path}")
        self.server = await asyncio.start_unix_server(self._accept, path=str(self.path))
        os.chmod(self.path, 0o660)

    def send(self, recipient, message):
        """Enqueue one server-authored message; return False if unavailable."""
        peer = self.peers.get(recipient)
        if peer is None or peer.writer.is_closing():
            return False
        frame = encode(message)
        try:
            peer.outgoing.put_nowait(frame)
        except asyncio.QueueFull:
            peer.writer.close()
            return False
        return True

    def multicast(self, recipients, message):
        """Deliver once to each named recipient; return delivery count."""
        return sum(self.send(name, message) for name in set(recipients))

    def broadcast(self, message):
        """Deliver to every currently connected user."""
        return self.multicast(tuple(self.peers), message)

    async def _pump(self, peer):
        try:
            while True:
                peer.writer.write(await peer.outgoing.get())
                await asyncio.wait_for(peer.writer.drain(), IO_TIMEOUT)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            peer.writer.close()

    async def _reject(self, writer, reason):
        with contextlib.suppress(ConnectionError, OSError, asyncio.TimeoutError):
            writer.write(encode({"kind": "error", "text": reason}))
            await asyncio.wait_for(writer.drain(), IO_TIMEOUT)

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        peer = None
        pump = None
        try:
            raw = writer.get_extra_info("socket").getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            pid, uid, _gid = struct.unpack("3i", raw)
            try:
                name = pwd.getpwuid(uid).pw_name
            except KeyError:
                name = ""
            if name not in self.allowed:
                await self._reject(writer, "Linux user is not in this game's roster")
                return
            if name in self.peers:
                await self._reject(writer, "This Linux user already has a connected player")
                return
            peer = Peer(name, pid, uid, reader, writer)
            self.peers[name] = peer
            pump = asyncio.create_task(self._pump(peer))
            self.send(name, {"kind": "welcome", "user": name, "uid": uid, "pid": pid})
            await self.on_join(name, pid, uid)
            while True:
                message = await read_frame(reader)
                now = time.monotonic()
                while peer.arrivals and now - peer.arrivals[0] > 5:
                    peer.arrivals.popleft()
                if len(peer.arrivals) >= 30:
                    raise ProtocolError("Rate limit: 30 commands per 5 seconds")
                peer.arrivals.append(now)
                await self.on_message(name, message)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except ProtocolError as exc:
            await self._reject(writer, str(exc))
        finally:
            if peer is not None and self.peers.get(peer.name) is peer:
                del self.peers[peer.name]
                await self.on_leave(peer.name)
            if pump:
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump
            writer.close()
            with contextlib.suppress(ConnectionError, OSError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), IO_TIMEOUT)
            self.tasks.discard(task)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for peer in list(self.peers.values()):
            peer.writer.close()
        pending = list(self.tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class Client:
    """One user's IPC endpoint. The broker authenticates its process UID."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.lock = asyncio.Lock()

    @classmethod
    async def connect(cls, path):
        reader, writer = await asyncio.open_unix_connection(path)
        return cls(reader, writer)

    async def send(self, message):
        async with self.lock:
            self.writer.write(encode(message))
            await asyncio.wait_for(self.writer.drain(), IO_TIMEOUT)

    async def receive(self):
        return await read_frame(self.reader)

    async def close(self):
        self.writer.close()
        with contextlib.suppress(ConnectionError, OSError, asyncio.TimeoutError):
            await asyncio.wait_for(self.writer.wait_closed(), IO_TIMEOUT)
