"""Host-side Docker helper: build, start, join, demo, test, stop.

Run this on macOS, Linux or Windows with Docker installed. Game clients
themselves run inside Linux as p1 through p7, each with its own UID.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IMAGE = "nightwatch-local"
CONTAINER = "nightwatch-game"


def run(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, **kwargs)


def ready(name):
    for _ in range(100):
        result = subprocess.run(
            ["docker", "exec", name, "python3", "-c",
             "import os; raise SystemExit(not os.path.exists('/run/nightwatch/game.sock'))"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return
        time.sleep(0.2)
    raise RuntimeError("Moderator did not become ready; inspect docker logs " + name)


def start(name, players=5, fast=False, seed=None):
    command = ["run", "-d", "--name", name, IMAGE, "python3", "server.py",
               "--players", ",".join(f"p{i}" for i in range(1, players + 1))]
    if fast:
        command += ["--night", "2", "--discussion", "2", "--vote", "2"]
    if seed is not None:
        command += ["--seed", str(seed)]
    run(*command)
    ready(name)


def demo(players):
    name = "nightwatch-demo-" + str(time.time_ns())
    destination = ROOT / "evidence" / "demo"
    destination.mkdir(parents=True, exist_ok=True)
    processes, handles = [], []
    try:
        start(name, players, fast=True, seed=7)
        for number in range(1, players + 1):
            user = f"p{number}"
            handle = (destination / f"{user}.txt").open("w")
            handles.append(handle)
            process = subprocess.Popen(
                ["docker", "exec", "--user", user, name,
                 "python3", "client.py", "--bot"], stdout=handle, stderr=subprocess.STDOUT,
            )
            processes.append(process)
        deadline = time.monotonic() + 120
        for process in processes:
            process.wait(timeout=max(1, deadline - time.monotonic()))
            if process.returncode != 0:
                raise RuntimeError("A demo client failed; see evidence/demo")
        for handle in handles:
            handle.flush()
        with (destination / "moderator.txt").open("w") as log:
            run("logs", name, stdout=log, stderr=subprocess.STDOUT)
        records = []
        for number in range(1, players + 1):
            path = destination / f"p{number}.txt"
            lines = path.read_text().splitlines()
            identity = next(line for line in lines if line.startswith("IDENTITY:"))
            result = next((line for line in lines if "WIN!" in line), None)
            if result is None:
                raise RuntimeError(f"Demo did not finish for p{number}")
            records.append({"user": f"p{number}", "identity": identity, "result": result})
        (destination / "summary.json").write_text(json.dumps(records, indent=2) + "\n")
        print("\nCOMPLETED: each player ran under a different Linux UID and PID.")
        for record in records:
            print(record["identity"])
        print(records[0]["result"])
        print("Actual gameplay transcripts:", destination)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for handle in handles:
            handle.close()
        # This helper owns the uniquely named, disposable demo container.
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["build", "start", "join", "demo", "test", "stop"])
    parser.add_argument("user", nargs="?", help="p1 ... p7 for join")
    parser.add_argument("--players", type=int, choices=[5, 6, 7], default=5)
    args = parser.parse_args()
    if args.action == "build":
        run("build", "-t", IMAGE, str(ROOT))
    elif args.action == "start":
        start(CONTAINER, args.players)
        print("Open one terminal per player, then use: python3 play.py join p1 (and p2, ...)")
    elif args.action == "join":
        if args.user not in [f"p{i}" for i in range(1, 8)]:
            parser.error("Choose a Linux user p1 through p7")
        run("exec", "-it", "--user", args.user, CONTAINER, "python3", "client.py")
    elif args.action == "demo":
        demo(args.players)
    elif args.action == "test":
        run("run", "--rm", IMAGE, "python3", "-m", "unittest", "-v", "tests")
    elif args.action == "stop":
        # Only the fixed game container created by this helper is removed.
        run("rm", "-f", CONTAINER)


if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, RuntimeError, subprocess.TimeoutExpired) as exc:
        sys.exit(str(exc))
