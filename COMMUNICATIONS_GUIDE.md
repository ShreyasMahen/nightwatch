# Understanding the communications library

## Follow one private message

Suppose p1 types `whisper p2,p3 Compare evidence?` during discussion. The client
parses it into an object with a command, phase ID, selected recipients, and
text. `Client.send` serializes it into JSON, prefixes its length with four
bytes, and writes the frame to p1's UNIX stream connection. The broker's
`read_frame` reconstructs the object even if the operating system splits the
bytes across multiple reads. The broker already knows this connection belongs
to p1 because `SO_PEERCRED` supplied p1's Linux UID when the connection opened.

`Game.message` takes the game-state lock. The game checks that p1 is alive,
the phase ID is current, the phase permits discussion, the recipients are
living players, and the text has no control characters. It sets `sender=p1`
itself and calls `Hub.multicast`. The library puts one encoded copy into the
queues for p1, p2, and p3. The sender receives an echo; other players get no
copy. Independent writer tasks send those queues while other users can act.

## Why a stream needs framing

A stream preserves byte order, not message boundaries. One `write()` may
require several reads, and two writes can arrive together. If a 25-byte JSON
body is sent, its header is the four bytes `00 00 00 19`. The receiver first
reads exactly four bytes, checks the decoded size, then reads exactly that
many body bytes. A malformed length is rejected before allocating/reading an
arbitrarily large body. A truncated frame is a failed connection, not a valid
short message. The tests feed frames one byte at a time to exercise this.

## Where concurrency happens

Each player has an operating-system process and a distinct Linux user. The
moderator uses an event loop to wait for multiple sockets without blocking on
one input. Each peer has a reader callback and an outbound writer task. Phase
timers are separate tasks. An `asyncio.Lock` prevents a deadline and a command
from changing game state simultaneously. Delivery methods enqueue without
waiting for a slow receiver, and writer timeouts bound stalled delivery.
The lock is in-process coordination, not a replacement for interprocess IPC.

## What authentication proves

On Linux, `SO_PEERCRED` returns the UID, GID, and PID associated with the peer
connection. It proves which local process account opened that connection;
it does not prove which human is at a keyboard. The broker maps the UID to
an allowed username. A JSON field such as `sender=p2` cannot turn a p1 process
into p2. Root or someone controlling Docker can launch processes as any user;
these administrators are outside the untrusted-player threat model.

## What multicast means here

There is no native one-to-many operation on the chosen stream socket. The
library implements multicast by enqueueing a separate copy for every unique
selected recipient. Broadcast uses the same mechanism for all active peers.
Each connection is ordered, but there is no global simultaneous-delivery or
all-or-nothing group-delivery guarantee. One disconnected recipient can miss
a group message while the others receive it. Reconnection supplies the latest
game state. Chat is not durably replayed; the continuation also restores the
latest private inspection result to the seer.

## Be ready to explain these questions

1. Why are four-byte lengths needed if JSON already has braces?
2. What prevents a client from claiming to be a different player?
3. Which code decides whether a player may inspect or attack?
4. Why can an eliminated player still receive public information?
5. What happens if a client disconnects halfway through a frame?
6. What happens if a receiver never reads its outbound messages?
7. Is group delivery atomic? What is lost when a player reconnects?
8. Why is the moderator a trusted single point of failure?
9. Which settings make demo runs reproducible, and why should a real match
   omit the predictable demo seed?

Try one run with human commands, send a whisper to only one teammate, and
disconnect/rejoin one client. Compare what appears in the other terminals.
Ask classmates to challenge these assumptions and record their actual results.


## Projects 2 and 3: what changed and why

The communication primitive is still the original local asyncio UNIX stream
socket. The new mechanism is isolation around each endpoint: one Linux user
and one container per player. The moderator has a separate container. CPU,
memory, process count, open files, writable storage, and network access are
bounded. A shared socket volume lets these isolated processes communicate.
The named-volume directory is owned by UID 10000, group 2000, mode 0750. The
socket is mode 0660. Player mounts are read-only, which prevents changes to the
directory while still allowing a connection to the socket. The private state
volume is mounted only in the moderator.

The kernel translates process identities across namespaces. Our cross-container
run reported distinct UIDs but PID 0 from the moderator's view. Each client
reported a distinct PID namespace with local PID 1. Authentication uses the UID,
not this namespace-relative PID and not a client-supplied sender field.

Christopher's README feedback prompted two other changes. A village vote now
requires floor(living/2)+1 votes, including disconnected living players in the
denominator. The moderator also saves match state. Its transaction queues
messages, writes/fsyncs a temporary private JSON file, replaces/fsyncs the final
checkpoint, then releases the queued messages. Therefore an acknowledgement
means the choice was saved. A storage failure stops the moderator. No new IPC
is hidden in checkpoint.py; its file operations are private moderator state.

On restart, roles, actions, phase IDs, living players, guard history and latest
seer result are restored. Time resumes from the last saved remaining duration,
so downtime does not consume that saved remainder. This policy may replay some
uncheckpointed clock time. Chat history is not recovered. The snapshot is not
a replicated log or automatic failover service.

Encryption and isolation solve different problems. This version keeps local
UNIX sockets rather than opening a TCP service. The local channel is not TLS
encrypted. Kernel identity and path permissions restrict ordinary users;
container profiles restrict a player's processes and resource use. Host root,
the Docker administrator and moderator remain trusted. Neither resource limits
nor checkpointing hides secrets from those trusted administrators.

The measured CPU check used 25000 microseconds of quota per 100000-microsecond
period. A five-second busy loop averaged about 25.05% of one CPU while another
player received nine status responses. The bounded process probe reached 32
tasks. A memory hog exited 137 with one cgroup OOM kill; the real client,
moderator and another player remained responsive. A SIGKILL/restart restored
the saved match and it ended in a village victory after a three-of-four vote.
Those are observations from the included run, not a claim of exhaustive safety.

Questions to check understanding:

1. Why can all clients have local PID 1 while being separate processes?
2. Why does a socket connection still need message framing and authorization?
3. How can one client's queue fill without blocking all other writers?
4. Why must the checkpoint be committed before the acknowledgement is sent?
5. What does fsync of the directory add after replacing the checkpoint file?
6. Why can two votes out of five no longer eliminate a player?
7. Which information is lost across a crash, and which state is restored?
8. What could host root or the Docker administrator still do?
9. Why did many separate interpreters trigger the memory cap before the PID cap?
