"""Real tmux/HTTP lifecycle checks, isolated from the operator's Hermes home."""

import importlib.util
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not shutil.which("tmux"),
    reason="WSL/Linux tmux supervisor",
)
SCRIPT = Path(__file__).resolve().parents[2] / "scripts/dashboard_watchdog.py"


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("Lifecycle condition did not become true")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def load_watchdog():
    spec = importlib.util.spec_from_file_location("dashboard_watchdog_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Watchdog


def test_supervisor_and_dashboard_crashes_preserve_single_instance(tmp_path):
    port = free_port()
    session = "dashboard-test-" + uuid.uuid4().hex
    pid_file = tmp_path / "pid"
    child = tmp_path / "server.py"
    child.write_text(
        "import http.server, json, os, pathlib\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "class Handler(http.server.BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200); self.end_headers()\n"
        "  data = {'overall':'degraded', 'version':'test',\n"
        f"          'hermes_home':{str(tmp_path)!r}}}\n"
        "  self.wfile.write(json.dumps(data).encode())\n"
        f"http.server.HTTPServer(('127.0.0.1', {port}), Handler).serve_forever()\n",
        encoding="utf-8",
    )
    command = [
        sys.executable,
        str(SCRIPT),
        "--home",
        str(tmp_path),
        "--port",
        str(port),
        "--session",
        session,
        "--interval",
        "0.1",
        "--cooldown",
        "0.2",
        "--grace",
        "2",
        "--",
        sys.executable,
        str(child),
    ]
    guard = None
    try:
        guard = subprocess.Popen(command)
        watchdog = load_watchdog()(
            tmp_path, [sys.executable, str(child)], port, session
        )
        wait_for(lambda: pid_file.exists() and watchdog.healthy())
        first = pid_file.read_text()
        duplicate = subprocess.run(command, capture_output=True, timeout=5)
        assert duplicate.returncode == 0
        assert b"already running" in duplicate.stdout
        guard.terminate()
        guard.wait(timeout=5)
        guard = subprocess.Popen(command)
        time.sleep(0.4)
        assert pid_file.read_text() == first
        watchdog.tmux("kill-session", "-t", "=" + session)
        wait_for(lambda: pid_file.read_text() != first and watchdog.healthy())
        second = pid_file.read_text()
        pause = tmp_path / "state/dashboard-watchdog/paused"
        pause.touch()
        time.sleep(0.3)
        watchdog.tmux("kill-session", "-t", "=" + session)
        time.sleep(0.5)
        assert pid_file.read_text() == second
        pause.unlink()
        wait_for(lambda: pid_file.read_text() != second and watchdog.healthy())
    finally:
        if guard is not None and guard.poll() is None:
            guard.terminate()
            guard.wait(timeout=5)
        subprocess.run(
            ["tmux", "kill-session", "-t", "=" + session],
            capture_output=True,
            check=False,
        )


def test_foreign_listener_startup_grace_and_failed_start_backoff(tmp_path):
    watchdog = load_watchdog()(
        tmp_path,
        ["/nonexistent/dashboard"],
        free_port(),
        "dashboard-test-" + uuid.uuid4().hex,
        grace=1,
        cooldown=2,
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", watchdog.port))
        listener.listen()
        assert watchdog.step() == "foreign_listener"
        assert not watchdog.state_file.exists()
    assert watchdog.step() in {"started", "start_failed"}
    target = "=" + watchdog.session
    wait_for(lambda: watchdog.tmux("has-session", "-t", target).returncode != 0)
    attempt = json.loads(watchdog.state_file.read_text())
    assert watchdog.step() == "backoff"
    assert json.loads(watchdog.state_file.read_text()) == attempt
    watchdog.command = [sys.executable, "-c", "import time; time.sleep(60)"]
    watchdog.state_file.unlink()
    try:
        assert watchdog.step() == "started"
        assert watchdog.step() == "starting"
        time.sleep(1.1)
        assert watchdog.step() == "backoff"
        assert watchdog.tmux("has-session", "-t", target).returncode != 0
    finally:
        watchdog.tmux("kill-session", "-t", "=" + watchdog.session)
