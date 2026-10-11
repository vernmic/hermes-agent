#!/usr/bin/env python3
"""Keep a loopback dashboard alive on WSL hosts without systemd.

Run in the foreground under Windows Task Scheduler. tmux keeps the dashboard
alive when this supervisor restarts; flock prevents concurrent supervisors.
"""

import argparse
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request


class Watchdog:
    def __init__(
        self,
        home,
        command,
        port=9119,
        session="hermes-dashboard",
        grace=300,
        cooldown=30,
    ):
        self.home = Path(home).resolve()
        self.command = command
        self.port = port
        self.session = session
        self.grace = grace
        self.cooldown = cooldown
        self.state_dir = self.home / "state" / "dashboard-watchdog"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.pause = self.state_dir / "paused"
        self.state_file = self.state_dir / "attempt.json"
        self.owner = hashlib.sha256(
            json.dumps([str(self.home), command, port]).encode()
        ).hexdigest()
        self.logs = self.home / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger("dashboard-watchdog." + session)
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        if not self.log.handlers:
            handler = RotatingFileHandler(
                self.logs / "dashboard-watchdog.log", maxBytes=1_000_000, backupCount=3
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self.log.addHandler(handler)
        self.last_event = None
        self.unhealthy_since = None

    def tmux(self, *args):
        return subprocess.run(
            ["tmux", *args], capture_output=True, text=True, timeout=10, check=False
        )

    def healthy(self):
        # The gateway may be degraded while the dashboard itself works.
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/status")
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=3) as response:
                data = json.load(response)
                return (
                    isinstance(data, dict)
                    and "overall" in data
                    and isinstance(data.get("version"), str)
                    and data.get("hermes_home") == str(self.home)
                )
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def listening(self):
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                return True
        except OSError:
            return False

    def event(self, name, detail=""):
        if (name, detail) != self.last_event:
            self.log.info("%s %s", name, detail)
            self.last_event = (name, detail)
        return name

    def step(self):
        if self.pause.exists():
            return self.event("paused")
        if self.healthy():
            self.unhealthy_since = None
            return self.event("healthy")
        now = time.time()
        if self.unhealthy_since is None:
            self.unhealthy_since = now
        exists = self.tmux("has-session", "-t", "=" + self.session).returncode == 0
        if exists:
            # Option commands do not accept the '=' exact-target prefix on
            # older tmux versions. has-session above already proved existence.
            marker = self.tmux(
                "show-options", "-qv", "-t", self.session, "@dashboard-watchdog"
            ).stdout.strip()
            try:
                owner, started = marker.split(":")
                age = now - float(started)
            except ValueError:
                return self.event("unmanaged_session", "preserving existing session")
            if owner != self.owner:
                return self.event("unmanaged_session", "owner differs")
            if min(age, now - self.unhealthy_since) < self.grace:
                return self.event("starting")
            self.tmux("kill-session", "-t", "=" + self.session)
            self.event("unhealthy_restart", "startup/health grace expired")
        elif self.listening():
            # Never kill an unrelated process occupying the configured port.
            return self.event("foreign_listener", "status response not recognized")
        boot = (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        )
        try:
            attempt = json.loads(self.state_file.read_text(encoding="utf-8"))
            if attempt.get("boot") == boot:
                if now - attempt["at"] < self.cooldown:
                    return self.event("backoff")
        except (OSError, ValueError, KeyError, TypeError):
            pass
        attempt = {"at": now, "boot": boot}
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(attempt), encoding="utf-8")
        temporary.replace(self.state_file)
        output = self.logs / "dashboard-service.log"
        if output.exists() and output.stat().st_size > 5_000_000:
            output.replace(output.with_suffix(".log.previous"))
        command = (
            f"exec env HERMES_HOME={shlex.quote(str(self.home))} "
            f"{shlex.join(self.command)} >> {shlex.quote(str(output))} 2>&1"
        )
        # Submit creation and ownership marking together to the tmux server;
        # a supervisor crash between client calls must not orphan ownership.
        result = self.tmux(
            "new-session",
            "-d",
            "-s",
            self.session,
            command,
            ";",
            "set-option",
            "-t",
            self.session,
            "@dashboard-watchdog",
            f"{self.owner}:{now}",
        )
        if result.returncode:
            return self.event("start_failed", result.stderr.strip())
        return self.event("started", "waiting for HTTP readiness")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--port", type=int, default=9119)
    parser.add_argument("--session", default="hermes-dashboard")
    parser.add_argument("--interval", type=float, default=15)
    parser.add_argument("--grace", type=float, default=300)
    parser.add_argument("--cooldown", type=float, default=30)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if min(args.interval, args.grace, args.cooldown) <= 0:
        parser.error("interval, grace and cooldown must be positive")
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        command = [
            str(Path.home() / ".local/bin/hermes"),
            "dashboard",
            "--no-open",
            "--skip-build",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
        ]
    watchdog = Watchdog(
        args.home, command, args.port, args.session, args.grace, args.cooldown
    )
    with (watchdog.state_dir / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Dashboard watchdog already running.")
            return 0
        while True:
            try:
                result = watchdog.step()
            except Exception:
                # Keep supervising after transient tmux/IO failures; log traceback.
                watchdog.log.exception("check_failed")
                result = "check_failed"
            if args.once:
                print(result)
                failed = {"check_failed", "start_failed", "foreign_listener"}
                return int(result in failed)
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
