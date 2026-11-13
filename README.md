# Nightwatch: Signal & Shadow

Projects 2 and 3, one combined continuation of the original Project 1 game.
Author: Shreyas Mahendra.

Nightwatch is a text-based social deduction game for 5 to 7 players. It keeps
Project 1's wolves, seer, guard, villagers, private messages, and timed phases.
The new mechanism is **per-player container isolation with Linux cgroup limits**.
Each player has a distinct Linux UID and a separate process/PID namespace.
The moderator has its own container and a private persistent checkpoint volume.

**All game communication between player users is in the single file
[`comms.py`](comms.py).** The original asynchronous UNIX stream socket primitive
is retained. This submission takes the assignment's new-isolation option.

## Quick start

Install and start Docker Desktop on macOS/Windows, or Docker Engine on Linux.
Use Python 3 on the host. No third-party Python package is required by the game.
On Windows, use WSL for the same commands, or replace `python3` with `py` if your
host Python uses that launcher. The verified environment is macOS with Docker's
Linux engine and cgroup v2; native Windows behavior has not been tested.

Download or extract the project, open a host terminal, and `cd` into the folder
containing play.py. Run:

```sh
python3 play.py build
python3 play.py test
python3 play.py demo --players 7
```

The demo uses scripted bots through the same client protocol. It starts one
moderator container and one container per player, plays to a winner, records
transcripts, and removes its uniquely named containers and volumes. A demo's
output normally goes under evidence/isolated-demo. Use `--evidence PATH` to
choose a different directory and `--keep` to retain containers for inspecting
logs. Retained demo containers can be removed with `stop --session NAME --purge`.

The container image is `nightwatch-isolated`. Project 1's old image/container
names are not used by this helper.

## Interactive game

Start the moderator from the project folder in your Mac/host terminal:

```sh
python3 play.py start --players 5
```

Open five separate host terminals in the same project folder. Run one command
in each terminal:

```sh
python3 play.py join p1
python3 play.py join p2
python3 play.py join p3
python3 play.py join p4
python3 play.py join p5
```

Each join command starts its own Linux container as the corresponding user.
Once all players join, the moderator deals secret roles and starts the night.
Type gameplay commands in the relevant player's terminal. The terminal used to
start the moderator remains a host shell. You can use a full path to play.py
instead of changing directories when opening a new terminal.

For seven players, start with `--players 7` and add p6 and p7. For a separate
match, add the same `--session my-match` to start, join, stop, and resume.

## Rules and commands

Five/six players have one wolf; seven players have two wolves. There is one
seer, one guard, and enough villagers to fill the roster.

- Night (60 seconds): wolves choose an attack and may use team chat; the seer
  inspects one player; the guard protects one player, including themselves.
  The guard cannot protect the same player on consecutive nights. A tied wolf
  attack eliminates nobody. Inspection reveals wolf/village alignment only.
- Discussion (45 seconds): public chat and selected-recipient whispers.
- Vote (30 seconds): the same chat remains available while players vote.
  Elimination requires **floor(living players / 2) + 1 votes for one target**.
  Abstainers and disconnected living players still count in the denominator.
  Two votes out of five, or two out of four, eliminate nobody. Three out of
  five, or three out of four, are sufficient. This replaces Project 1's unique
  plurality rule in response to Christopher Caperlac's feedback.
- Eliminated players can watch but cannot act or chat. The village wins when
  no wolves remain. Wolves win when they are at least as numerous as the rest.

```text
status                         current phase and living players
say Hello everyone             public day message
whisper p2 Compare evidence     private message to p2 (and sender)
whisper p2,p3 Compare evidence  selected-group private message
team Choose the same target    living wolves only, at night
strike p2                      wolf night attack
inspect p2                     seer night investigation
protect p2                     guard night protection
vote p2                        vote for another living player
pass                           abstain from night action or vote
help                           show commands
quit                           disconnect
```

Choices can change until the phase deadline. A disconnected player's saved
choice stays in place, otherwise the missing action is an abstention. The
minimal client does not implement arrow-key command history; type the command
again rather than pressing Up. There is no automated reconnection: rejoin with
the same Linux user after the connection closes.

## Isolation added for Projects 2 and 3

Project 1 put all players and the moderator in one container under distinct
UIDs. Now each player container has:

| Control | Player configuration |
| --- | --- |
| Linux user | p1 through p7, UIDs 10001 through 10007, shared group 2000 |
| PID/filesystem namespaces | Separate from moderator and other players |
| CPU | 0.25 of one CPU (`--cpus 0.25`) |
| Memory / swap | 64 MiB maximum, memory-swap also 64 MiB, so no extra swap |
| Processes/threads | cgroup maximum 32 |
| File descriptors | 128 soft and hard limit |
| Filesystem | Read-only root image; bounded 8 MiB /tmp for temporary files |
| Network | `--network none`; no published ports |
| Privilege | All capabilities dropped, no-new-privileges, default seccomp |
| Shared socket | Read-only volume mount; directory 0750 and socket 0660 |
| Match secrets | Moderator checkpoint volume is not mounted in players |

The moderator has a separate 128 MiB / one-CPU profile, its own UID 10000,
and the only writable mounts for the socket directory and private state.
A short trusted setup container uses CHOWN/FOWNER only to initialize those
Docker-managed volumes. These capabilities are absent from gameplay containers.
No container receives the Docker socket or a host filesystem bind mount.
Docker-provided runtime paths such as /dev/shm can also be writable; read-only
root does not mean that every kernel/runtime filesystem is read-only.

Linux authenticates a connecting user's UID with SO_PEERCRED. A peer in another
PID namespace may appear as PID 0 to the moderator; that value does not mean the
players share a process. Clients print their local PID and namespace identifier,
and the demo records distinct UID/namespace/container identities. Sender fields
inside player messages never override the authenticated identity.

This remains a **local-only, unencrypted UNIX socket design**. Encryption was a
concern in Christopher's README review. The response here is to keep traffic off
TCP networks, isolate processes, protect filesystem paths, and authenticate
kernel UIDs. These are access controls, not TLS or a claim of encrypted traffic.
The moderator, host root, and Docker administrator remain trusted and can inspect
or alter the match. Containers share a kernel and are not a complete security
boundary against kernel vulnerabilities or an administrator. A player can still
abstain, disconnect, lie in chat, or misuse their own account.

## Crash recovery

The moderator stores private state at /var/lib/nightwatch/checkpoint.json in its
own named volume. The directory is 0700 and the JSON file is 0600. Roles, living
players, accepted choices, phase IDs, round, guard history, latest seer result,
and remaining phase time are saved.

State-changing callbacks stage their outgoing messages, write a temporary file,
fsync it, atomically replace the checkpoint, and fsync the directory before
publishing acknowledgements or state events. A checkpoint failure stops the
moderator; it does not acknowledge an unsaved action. A private file lock rejects
a second moderator. Recovery only unlinks an owned, stale socket after checking
that it is not a live listener, symlink, or regular file.

After a crash, resume the same moderator:

```sh
python3 play.py resume
```

Rejoin players with the same join commands. Roles and accepted actions are
restored. The phase timer resumes from the remaining duration stored in the last
checkpoint. This pauses at the last saved clock value, rather than counting the
outage as gameplay time. Uncheckpointed timer progress and unsent acknowledgements
may be lost. Public/private chat history is not replayed; the latest seer result
is restored to the seer. This is crash recovery for one moderator, not replicated
storage, automatic failover, or protection against deleting/corrupting its volume.
Corrupt state fails closed rather than silently dealing a replacement match.
Checkpoint I/O is synchronous, so slow storage can delay the event loop; the
current design is a single moderator with small local snapshots.

Stop a match with:

```sh
python3 play.py stop
```

This stops containers and retains the moderator and volumes for resume. To delete
this session's saved match and start a new one, explicitly use:

```sh
python3 play.py stop --purge
python3 play.py start
```

The helper only removes its labeled containers and volumes for the named session.
It does not prune Docker or touch unrelated projects.

## Reproduce the new mechanism tests

```sh
python3 verify.py --evidence evidence/verification
```

This creates disposable sessions and tests bounded CPU load, a bounded process
creation attempt, a memory allocation that exceeds the player's cap, private
checkpoint access, socket-directory protection, read-only root, and distinct PID
namespaces. It checks another player and the moderator remain responsive. It
then force-kills the moderator mid-night, restarts from the saved volume,
reconnects the same users, verifies the same roles/actions/phase, exercises a
private group message, and finishes the match with a majority vote. Use `--keep`
to retain its uniquely named evidence containers. It needs cgroup v2 and takes
about a minute; exact timings depend on the machine. The tests deliberately
stress only their own bounded containers.

## Communications library API

The sole game IPC implementation is comms.py. No game rules are in that file.
It exposes:

| API | Meaning |
| --- | --- |
| `Hub(path, allowed_users, on_join, on_message, on_leave, recover_stale=False)` | Authenticated local broker; callbacks are async |
| `await Hub.start()` | Bind the socket and set mode 0660 |
| `Hub.send(user, message)` | Enqueue one JSON object for one connected player; returns bool |
| `Hub.multicast(users, message)` | Deduplicate selected users and enqueue copies; returns count |
| `Hub.broadcast(message)` | Enqueue to all connected players; returns count |
| `await Hub.close()` | Stop accepting and close peer sessions |
| `await Client.connect(path)` | Connect as the current Linux process user |
| `await Client.send(message)` | Write a framed JSON object |
| `await Client.receive()` | Read a complete JSON object |
| `await Client.close()` | Close the endpoint |

Frames have a four-byte big-endian length prefix and a UTF-8 JSON object body,
limited to 16 KiB. Readers handle partial reads. Each peer has an independent
64-frame outbound queue, three-second body/write timeouts, and a 30-command per
five-second limit. Queue overflow or a stalled write disconnects that peer.
Application fan-out is neither simultaneous nor atomic group delivery. Offline
players can miss chat. Game rules and phase deadlines share an asyncio lock;
per-peer I/O remains concurrent.

## Source map

- comms.py: all player/moderator IPC, framing, identity, routing, backpressure.
- game.py: original roles/phases plus strict-majority voting and transactional
  checkpoint/event ordering.
- checkpoint.py: moderator-private atomic snapshots and exclusive file lock.
- server.py: moderator startup, checkpoint restore, signal/failure handling.
- client.py: original interactive interface and optional demonstration bot.
- play.py: host-side isolated Docker launch, demo, stop, and resume helper.
- verify.py: reproducible host-side isolation/crash experiments using comms.py
  driver processes; not an alternative game IPC library.
- tests.py: rule, frame, real-socket, persistence, and recovery unit tests.
- Dockerfile: Linux accounts, source image, private directory permissions.
- COMMUNICATIONS_GUIDE.md: explanations and questions to check understanding.

The Canvas submission includes report.pdf, poster.pdf, fresh screenshots, and
actual test logs. Private assignment reports and evidence are excluded from Git.
The original Project 1 archive remains separately preserved.

References: [Docker resource limits](https://docs.docker.com/engine/containers/resource_constraints/),
[Docker default seccomp](https://docs.docker.com/engine/security/seccomp/),
[Linux UNIX sockets and credentials](https://man7.org/linux/man-pages/man7/unix.7.html).
