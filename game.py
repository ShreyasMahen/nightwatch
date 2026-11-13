"""Nightwatch game rules. All inter-user delivery goes through comms.Hub."""

import asyncio
import random
import math
import time
from contextlib import contextmanager

from checkpoint import CheckpointError
from collections import Counter


class RuleError(Exception):
    pass


class Game:
    def __init__(self, roster, night=60, discussion=45, vote=30, seed=None, checkpoint=None):
        if not 5 <= len(roster) <= 7 or len(set(roster)) != len(roster):
            raise ValueError("Choose 5 to 7 distinct Linux users")
        self.roster = list(roster)
        self.durations = {"night": night, "discussion": discussion, "vote": vote}
        self.random = random.Random(seed) if seed is not None else random.SystemRandom()
        self.hub = None
        self.lock = asyncio.Lock()
        self.phase, self.phase_id, self.round = "lobby", 0, 0
        self.roles, self.alive, self.choices = {}, set(roster), {}
        self.last_protected = None
        self.timer = None
        self.checkpoint = checkpoint
        self.deadline_at = None
        self.saved_remaining = 0
        self.inspections = {}
        self.winner = None
        self.failed = asyncio.Event()
        self._outbox = None

    def deliver(self, method, *args):
        if self._outbox is None:
            return getattr(self.hub, method)(*args)
        self._outbox.append((method, args))

    @contextmanager
    def atomic(self):
        if self._outbox is not None:
            raise RuntimeError("Nested game transaction")
        self._outbox = []
        try:
            yield
            if self.checkpoint:
                self.checkpoint.save(self.record())
            events = self._outbox
        except BaseException:
            self.failed.set()
            if self.timer:
                self.timer.cancel()
            raise
        finally:
            self._outbox = None
        for method, args in events:
            if method == "print":
                print(*args, flush=True)
            else:
                getattr(self.hub, method)(*args)

    def record(self):
        return {"schema": 1, "roster": self.roster, "durations": self.durations,
                "phase": self.phase, "phase_id": self.phase_id, "round": self.round,
                "roles": self.roles, "alive": sorted(self.alive), "choices": self.choices,
                "last_protected": self.last_protected, "inspections": self.inspections,
                "winner": self.winner,
                "remaining": max(0, self.deadline_at-time.monotonic()) if self.deadline_at else 0}

    @classmethod
    def restore(cls, record, checkpoint=None):
        try:
            if record.get("schema") != 1:
                raise ValueError("unknown checkpoint schema")
            roster, durations = record["roster"], record["durations"]
            if not isinstance(roster, list) or not all(isinstance(p, str) for p in roster):
                raise ValueError("invalid roster")
            if set(durations) != {"night", "discussion", "vote"} or not all(
                    type(v) in (int, float) and math.isfinite(v) and v > 0 for v in durations.values()):
                raise ValueError("invalid durations")
            game = cls(roster, **durations, checkpoint=checkpoint)
            phase = record["phase"]
            if phase not in ("lobby", "night", "discussion", "vote", "finished"):
                raise ValueError("invalid phase")
            roles, alive, choices = record["roles"], record["alive"], record["choices"]
            if not isinstance(roles, dict) or not isinstance(choices, dict) or not isinstance(alive, list):
                raise ValueError("invalid state types")
            if not set(alive) <= set(roster) or len(alive) != len(set(alive)):
                raise ValueError("invalid living players")
            if phase != "lobby":
                expected = Counter({"wolf": 2 if len(roster) == 7 else 1, "seer": 1, "guard": 1,
                                    "villager": len(roster) - (4 if len(roster) == 7 else 3)})
                if set(roles) != set(roster) or Counter(roles.values()) != expected:
                    raise ValueError("invalid roles")
            elif roles or choices:
                raise ValueError("lobby cannot contain dealt roles or actions")
            if not set(choices) <= set(alive) or any(v is not None and v not in alive for v in choices.values()):
                raise ValueError("invalid actions")
            remaining = record["remaining"]
            if type(remaining) not in (int, float) or not math.isfinite(remaining) or remaining < 0:
                raise ValueError("invalid timer")
            for key in ["phase_id", "round"]:
                if type(record[key]) is not int or record[key] < 0:
                    raise ValueError("invalid phase number")
            if record["last_protected"] is not None and record["last_protected"] not in roster:
                raise ValueError("invalid guard history")
            inspections = record["inspections"]
            if not isinstance(inspections, dict) or any(
                name not in roles or roles[name] != "seer" or not isinstance(v, dict)
                or v.get("target") not in roster or v.get("alignment") not in ("wolf", "village")
                or not isinstance(v.get("text"), str) for name, v in inspections.items()):
                raise ValueError("invalid inspection history")
            winner = record["winner"]
            if (phase == "finished") != (winner in ("VILLAGE", "WOLVES")):
                raise ValueError("invalid winner")
            game.phase, game.phase_id, game.round = phase, record["phase_id"], record["round"]
            game.roles, game.alive, game.choices = roles.copy(), set(alive), choices.copy()
            game.last_protected, game.inspections, game.winner = record["last_protected"], inspections.copy(), winner
            game.saved_remaining = min(remaining, durations.get(phase, remaining))
            return game
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CheckpointError("Invalid checkpoint; refusing to start a replacement match") from exc

    def resume(self):
        with self.atomic():
            if self.phase in self.durations:
                self.deadline_at = time.monotonic() + self.saved_remaining
                self.timer = asyncio.create_task(self.deadline(self.phase_id, self.saved_remaining))
            self.announce(f"RECOVERED round {self.round} / {self.phase.upper()}; roles and accepted actions restored.")

    def tell(self, user, text, kind="notice", **extra):
        self.deliver("send", user, {"kind": kind, "text": text, **extra})

    def announce(self, text):
        self.deliver("broadcast", {"kind": "notice", "text": text})
        if self._outbox is None:
            print(text, flush=True)
        else:
            self._outbox.append(("print", (text,)))

    def snapshot(self):
        return {"kind": "state", "phase": self.phase, "phase_id": self.phase_id,
                "round": self.round, "alive": sorted(self.alive),
                "connected": sorted(self.hub.peers),
                "seconds": max(0, self.deadline_at - time.monotonic()) if self.deadline_at else 0,
                "majority_needed": len(self.alive) // 2 + 1}

    def reveal_private(self, user):
        role = self.roles[user]
        team = sorted(p for p, r in self.roles.items() if r == "wolf") if role == "wolf" else []
        self.tell(user, f"Your secret role is {role.upper()}.", kind="role",
                  role=role, team=team)

    async def joined(self, user, pid, uid):
        async with self.lock:
            with self.atomic():
                self.announce(f"{user} connected (Linux UID {uid}, PID {pid}).")
                if self.roles:
                    self.reveal_private(user)
                    if user in self.inspections:
                        self.tell(user, self.inspections[user]["text"], "inspection", **{
                            k: v for k, v in self.inspections[user].items() if k != "text"})
                    if user in self.choices:
                        self.tell(user, "Restored action: " + str(self.choices[user]), "ack")
                if self.phase == "lobby" and set(self.roster) <= set(self.hub.peers):
                    wolves = 2 if len(self.roster) == 7 else 1
                    roles = ["wolf"] * wolves + ["seer", "guard"]
                    roles += ["villager"] * (len(self.roster) - len(roles))
                    self.random.shuffle(roles)
                    self.roles = dict(zip(self.roster, roles))
                    self.announce("All players joined. Secret roles have been dealt.")
                    for player in self.roster:
                        self.reveal_private(player)
                    self.round = 1
                    self.transition("night")
                else:
                    self.deliver("send", user, self.snapshot())
                    if self.phase == "finished":
                        self.tell(user, f"{self.winner} WIN! All roles are now revealed.",
                                  "result", winner=self.winner, roles=self.roles)

    async def left(self, user):
        async with self.lock:
            if self.phase != "finished":
                self.announce(f"{user} disconnected; reconnect with the same Linux user. "
                              "Missing actions become abstentions when time expires.")

    def transition(self, phase):
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
        self.phase, self.phase_id, self.choices = phase, self.phase_id + 1, {}
        self.deadline_at = time.monotonic() + self.durations[phase] if phase in self.durations else None
        self.announce(f"ROUND {self.round} / {phase.upper()}")
        self.deliver("broadcast", self.snapshot())
        if phase in self.durations:
            self.timer = asyncio.create_task(self.deadline(self.phase_id, self.durations[phase]))

    async def deadline(self, phase_id, seconds):
        await asyncio.sleep(seconds)
        async with self.lock:
            with self.atomic():
                if self.phase_id == phase_id:
                    if self.phase == "night":
                        self.resolve_night()
                    elif self.phase == "discussion":
                        self.transition("vote")
                    elif self.phase == "vote":
                        self.resolve_vote()

    @staticmethod
    def clean_text(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 240:
            raise RuleError("Message must contain 1 to 240 characters")
        if any(not char.isprintable() for char in value):
            raise RuleError("Control characters are not allowed")
        return value

    async def message(self, user, message):
        async with self.lock:
            with self.atomic():
                try:
                    self.command(user, message)
                except RuleError as exc:
                    self.tell(user, str(exc), "error")

    def command(self, user, message):
        cmd = message.get("cmd")
        if not isinstance(cmd, str):
            raise RuleError("Command must be a string")
        if cmd == "status":
            self.deliver("send", user, self.snapshot())
            return
        if self.phase in ("lobby", "finished"):
            raise RuleError("Wait for the game to start, or start a new game")
        if user not in self.alive:
            raise RuleError("Eliminated players can watch but cannot act or chat")
        if type(message.get("phase_id")) is not int or message["phase_id"] != self.phase_id:
            raise RuleError("Stale phase: wait for the current phase announcement")
        if cmd in ("say", "whisper", "team"):
            text = self.clean_text(message.get("text"))
            if cmd == "team":
                if self.phase != "night" or self.roles[user] != "wolf":
                    raise RuleError("Only living wolves can use team chat at night")
                recipients = [p for p in self.alive if self.roles[p] == "wolf"]
                scope = "WOLF TEAM"
            else:
                if self.phase not in ("discussion", "vote"):
                    raise RuleError("Public and private discussion is available during the day")
                if cmd == "say":
                    recipients, scope = list(self.hub.peers), "PUBLIC"
                else:
                    recipients = message.get("to")
                    if (not isinstance(recipients, list) or not recipients or
                            any(not isinstance(p, str) or p not in self.alive for p in recipients)):
                        raise RuleError("Whisper recipients must be living player names")
                    recipients = sorted(set(recipients + [user]))
                    scope = "PRIVATE to " + ",".join(recipients)
            self.deliver("multicast", recipients, {"kind": "chat", "sender": user,
                                         "scope": scope, "text": text})
            return
        required = {"strike": "wolf", "inspect": "seer", "protect": "guard"}
        if cmd == "pass":
            if self.phase not in ("night", "vote"):
                raise RuleError("There is no action to pass during discussion")
            self.choices[user] = None
        else:
            target = message.get("target")
            if not isinstance(target, str) or target not in self.alive:
                raise RuleError("Target must be a living player")
            if cmd == "vote":
                if self.phase != "vote" or target == user:
                    raise RuleError("Vote for another living player during the voting phase")
            elif cmd in required:
                if self.phase != "night" or self.roles[user] != required[cmd]:
                    raise RuleError("That action is not available for your role in this phase")
                if cmd in ("strike", "inspect") and target == user:
                    raise RuleError("You cannot target yourself with that action")
                if cmd == "strike" and self.roles[target] == "wolf":
                    raise RuleError("Wolves cannot attack a teammate")
                if cmd == "protect" and target == self.last_protected:
                    raise RuleError("The guard cannot protect the same player on consecutive nights")
            else:
                raise RuleError("Unknown command; type help for available commands")
            self.choices[user] = target
        self.tell(user, f"Action saved: {cmd} " + str(self.choices[user]) + ". "
                  "You can change it before the deadline.", "ack")

    @staticmethod
    def unique_plurality(targets):
        counts = Counter(t for t in targets if t is not None)
        if not counts:
            return None, counts
        highest = max(counts.values())
        winners = [name for name, count in counts.items() if count == highest]
        return (winners[0] if len(winners) == 1 else None), counts

    def eliminate(self, target, reason):
        self.alive.remove(target)
        self.announce(f"{target} was eliminated by {reason}. Role: {self.roles[target]}.")

    def finished(self):
        wolves = sum(self.roles[p] == "wolf" for p in self.alive)
        villagers = len(self.alive) - wolves
        winner = "VILLAGE" if wolves == 0 else "WOLVES" if wolves >= villagers else None
        if winner:
            self.winner = winner
            self.transition("finished")
            self.deliver("broadcast", {"kind": "result", "winner": winner, "roles": self.roles,
                                "text": f"{winner} WIN! All roles are now revealed."})
            self.announce(f"GAME OVER: {winner} WIN")
            return True
        return False

    def resolve_night(self):
        for player in sorted(self.alive):
            if self.roles[player] == "seer" and self.choices.get(player) is not None:
                target = self.choices[player]
                alignment = "wolf" if self.roles[target] == "wolf" else "village"
                self.inspections[player] = {
                    "text": f"Your inspection: {target} is {alignment.upper()}.",
                    "target": target, "alignment": alignment}
                self.tell(player, self.inspections[player]["text"], "inspection",
                          target=target, alignment=alignment)
        protected = next((self.choices.get(p) for p in self.alive if self.roles[p] == "guard"), None)
        self.last_protected = protected
        victim, _ = self.unique_plurality(self.choices.get(p) for p in self.alive
                                         if self.roles[p] == "wolf")
        if victim and victim != protected:
            self.eliminate(victim, "the night attack")
        elif victim:
            self.announce("The guard stopped the night attack. Nobody was eliminated.")
        else:
            self.announce("No decisive night attack. Nobody was eliminated.")
        if not self.finished():
            self.transition("discussion")

    def resolve_vote(self):
        target, counts = self.unique_plurality(self.choices.get(p) for p in self.alive)
        self.announce("Vote totals: " + (", ".join(f"{p}={n}" for p, n in sorted(counts.items()))
                                        or "everyone abstained"))
        threshold = len(self.alive) // 2 + 1
        if target and counts[target] >= threshold:
            self.eliminate(target, f"a majority vote ({counts[target]}/{len(self.alive)})")
        else:
            self.announce(f"No strict majority ({threshold} votes required): nobody was eliminated.")
        if not self.finished():
            self.round += 1
            self.transition("night")

    async def close(self):
        if self.timer:
            self.timer.cancel()
            try:
                await self.timer
            except asyncio.CancelledError:
                pass
