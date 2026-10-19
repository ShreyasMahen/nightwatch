# Nightwatch: Signal & Shadow

A text-based social deduction game for 5-7 players. The village must identify
the hidden wolves before the wolves equal or outnumber the other survivors.
Players exchange public arguments, private whispers, and secret role actions.

**Communications library: [`comms.py`](comms.py), in the project root.**
All communications between player users and the moderator pass through this
single source file. It uses asynchronous UNIX-domain stream sockets, framed
JSON, and Linux `SO_PEERCRED` authentication. There are no named pipes or
third-party Python dependencies.

## Requirements

- Docker Desktop on macOS/Windows, or Docker Engine on Linux.
- Python 3.10 or newer on the host for the convenient `play.py` wrapper.
- Internet access for the initial official `python:3.13-slim` image download.
- The game itself runs on Python 3.13 inside Linux.

The host-side helper is intended for macOS, Windows, and Linux. It has been
verified on macOS with Linux containers; Windows has not been separately tested.
On Windows, use `py play.py ...` in place of `python3 play.py ...` if your Python
installation provides the `py` launcher. Use Linux containers in Docker Desktop.

## Quick demonstration

Open Terminal in this extracted project folder and run:

```sh
python3 play.py build
python3 play.py test
python3 play.py demo --players 7
```

The demonstration runs seven independent clients under Linux users `p1` to
`p7`, with UIDs 10001 to 10007 and different PIDs. It uses two-second phases
and seed 7 for reproducibility. The bots use only their own roles and messages
received through the same IPC library as human players. The moderator remains
the authority for all game rules. The demo prints each player's verified UID
and PID and saves complete transcripts in `evidence/demo/`. Its temporary
container is removed after the run. These are simulated players, not a claim
that seven humans participated.

## Play with humans in five terminals

First build the image and start a fresh moderator:

```sh
python3 play.py build
python3 play.py start
```

Open five Terminal tabs/windows in this folder. Run one of these commands in
each tab, so every player has a separate client process and Linux identity:

```sh
python3 play.py join p1
python3 play.py join p2
python3 play.py join p3
python3 play.py join p4
python3 play.py join p5
```

The first command belongs in the first tab, the second in the second tab, and
so on. The game starts automatically when all five users connect. The moderator
runs as Linux user `moderator` (UID 10000), never as a player.

For seven humans, use `python3 play.py start --players 7` and add p6 and p7.
Five or six players have one wolf; seven players have two wolves. There is
always one seer, one guard, and enough ordinary villagers to fill the roster.

For example, the helper's p1 command executes:

```sh
docker exec -it --user p1 nightwatch-game python3 client.py
```

The direct `docker exec` command works from any host folder. To avoid a
"can't open play.py" error when a new terminal starts in your home directory,
use these direct commands in five separate terminals after the moderator is
started:

| Player terminal | Command |
| --- | --- |
| p1 | `docker exec -it --user p1 nightwatch-game python3 client.py` |
| p2 | `docker exec -it --user p2 nightwatch-game python3 client.py` |
| p3 | `docker exec -it --user p3 nightwatch-game python3 client.py` |
| p4 | `docker exec -it --user p4 nightwatch-game python3 client.py` |
| p5 | `docker exec -it --user p5 nightwatch-game python3 client.py` |

All five are manual players; the other terminals do not make decisions for
themselves. For automated players, use the separate `demo` command. Type game
commands fresh; this minimal client does not implement Up-arrow command history.
To stop a running game from any host terminal, use `docker stop nightwatch-game`.
To discard it before creating a new match with `play.py start`, use `play.py stop`
from the project folder.

To leave a player terminal, type `quit` or press Ctrl-C. Rejoin with the same
`play.py join pN` command to retain that user's role and living/dead status.
To finish and discard the current game container:

```sh
python3 play.py stop
```

This removes only the helper's `nightwatch-game` container. Start again for a
new game. Do not start a second moderator with the same socket pathname.

## Rules and strategy

1. **Night (60 s):** Each living wolf uses `strike pN`. With two wolves, a
   unique plurality of their choices decides the target; a tie means no attack.
   Wolves can privately coordinate using `team message`. The seer uses
   `inspect pN` and receives a private wolf/village result at dawn. The guard
   uses `protect pN`, including themselves, but cannot protect the same player
   on consecutive nights. A protected target survives an attack.
2. **Discussion (45 s):** Living players can `say message` publicly or
   `whisper p2,p3 message` to a selected group. Evidence, alliances, and false
   claims are part of the strategy. A claim to be the seer is ordinary chat;
   the server does not certify it or reveal the sender's actual secret role.
3. **Vote (30 s):** Use `vote pN` to select another living player. Players may
   change their choices before the deadline or use `pass`. A unique plurality
   eliminates a player. Ties and unanimous abstention eliminate nobody.
4. Eliminated roles are revealed. Eliminated players can watch public events
   but cannot act or chat. No wolves alive means a village win. Wolves win when
   their count is at least the count of the other survivors. All roles are
   revealed at the end.

All users can issue `status` or `help`. Actions that arrive with an old phase
ID are rejected. A missed night action or vote counts as abstaining, so one
disconnected client does not freeze the match.

## Single-file communications API

The public API is in `comms.py`:

| API | Meaning |
| --- | --- |
| `Hub(path, allowed_users, on_join, on_message, on_leave)` | Configure the broker and async callbacks. |
| `await hub.start()` | Bind a Linux UNIX stream socket; set mode 0660. |
| `hub.send(user, message) -> bool` | Queue one JSON object for one user. |
| `hub.multicast(users, message) -> int` | Queue one copy per unique selected user. |
| `hub.broadcast(message) -> int` | Queue a copy for every connected user. |
| `await hub.close()` | Stop accepting, close peers, and clean up the socket. |
| `await Client.connect(path) -> Client` | Open one user's IPC endpoint. |
| `await client.send(message)` | Write one framed JSON object. |
| `await client.receive() -> dict` | Receive one complete framed JSON object. |
| `await client.close()` | Close the endpoint. |
| `ProtocolError` | Malformed, oversized, or non-object message. |

Callback signatures are `async on_join(user, pid, uid)`,
`async on_message(authenticated_user, message)`, and `async on_leave(user)`.
`game.py` handles game-level authorization, phase IDs, and selected recipients.
Transport framing and delivery stay entirely in `comms.py`.

The wire representation is a four-byte unsigned big-endian body length followed
by a UTF-8 JSON object, at most 16,384 bytes. It does not assume one socket read
equals one message. A client action looks like:

```json
{"cmd": "whisper", "phase_id": 2, "to": ["p2", "p3"], "text": "Compare evidence?"}
```

Sender names in delivered messages are inserted by the server from the kernel
identity; a client-supplied `sender` field has no authority.

## Concurrency, security, and crash behavior

- A separate client process runs under each player's UID. The moderator has
  its own UID. The Dockerfile creates the accounts, and the demo records them.
- `asyncio` schedules independent connection readers and outbound queue writers.
  An application lock serializes game-state changes with phase deadlines.
- The primitive is a reliable, ordered byte stream. Selective multicast and
  broadcast are application-level fan-out over independent connections; UNIX
  stream sockets do not natively broadcast to all players.
- The broker reads `(pid, uid, gid)` using Linux `SO_PEERCRED` and resolves the
  UID through the account database. A roster allowlist and one active session
  per username reject outsiders and duplicate sessions.
- `/run/nightwatch` is owned by `moderator:nightwatch` with mode 0750. Players
  can reach the socket but cannot replace files in that directory. The socket
  has mode 0660. Game source files are readable but not writable by players.
- Every peer has a 64-frame outbound queue and a three-second write timeout.
  Slow or overflowing peers are disconnected. Incoming commands are limited
  to 30 per five seconds, and a started message body has a three-second timeout.
- EOF removes only that user's transport session. The user can reconnect;
  roles remain in moderator memory and deadlines continue without them.
- Invalid roles, targets, phase IDs, command types, and control characters are
  rejected. Only the moderator can eliminate a player or declare a winner.

**Trust boundary:** the moderator, Linux root, and anyone with control of the
Docker daemon are trusted administrators. This design does not hide secrets
from them. It has no remote networking, encryption, persistent match recovery,
or protection from an administrator killing/replacing the server. If the
moderator crashes, the match must be restarted. An idle connection from a
legitimate player can occupy that player's session until it disconnects; it
cannot stop the game's timed phases. These are useful topics for peer review.

## Files

| File | Purpose |
| --- | --- |
| `comms.py` | **Entire inter-user communications library.** |
| `game.py` | Authoritative role, phase, action, and win rules. |
| `server.py` | Moderator CLI and graceful signal handling. |
| `client.py` | Interactive text client and optional demo bot. |
| `play.py` | Host-side Docker setup/play/test/demo helper. |
| `tests.py` | Rule, frame, authentication, and socket integration tests. |
| `Dockerfile`, `.dockerignore` | Reproducible Linux setup and distinct users. |
| `COMMUNICATIONS_GUIDE.md` | Explanation and questions to check understanding. |

## References

- Python 3.13 asyncio streams: https://docs.python.org/3.13/library/asyncio-stream.html
- Linux UNIX sockets and SO_PEERCRED: https://man7.org/linux/man-pages/man7/unix.7.html
- Docker exec user selection: https://docs.docker.com/reference/cli/docker/container/exec/


## Sharing

The public repository contains the game source, Docker setup, tests, and
communications documentation. Running a demo creates local transcripts under
`evidence/demo/`; those generated files are ignored by Git. Assignment reports,
feedback notes, and screenshots are maintained separately in the local project.
