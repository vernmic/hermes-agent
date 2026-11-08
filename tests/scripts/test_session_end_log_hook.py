"""Real-process contract tests for the opt-in session-end logging example."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

HOOK = Path(__file__).resolve().parents[2] / "scripts/agent_hooks/session_end_log.py"


def run_hook(tmp_path, payload, **env_values):
    env = dict(os.environ, HERMES_WORKSPACE_DIR=str(tmp_path),
               HERMES_HOOK_DISTILL="0", **env_values)
    return subprocess.run([sys.executable, str(HOOK)], input=payload,
                          text=True, capture_output=True, env=env, timeout=5, check=False)


def test_records_turn_fields_without_running_nested_agents(tmp_path):
    result = run_hook(tmp_path, json.dumps({"session_id": "one", "profile": "coder",
                                           "extra": {"turn_id": "t", "completed": True}}))
    assert result.returncode == 0 and result.stdout.strip() == "{}"
    record = json.loads((tmp_path / "execution-log.jsonl").read_text())
    assert (record["session_id"], record["profile"], record["turn_id"], record["completed"]) == (
        "one", "coder", "t", True)
    assert record["ts"].endswith("Z")
    # Even the retired opt-in may not start an agent or scan a growing log.
    env = dict(os.environ, HERMES_WORKSPACE_DIR=str(tmp_path), HERMES_HOOK_DISTILL="1",
               PATH=str(tmp_path))
    result = subprocess.run([sys.executable, str(HOOK)], input="{}", text=True,
                            capture_output=True, env=env, timeout=5, check=False)
    assert result.returncode == 0 and result.stdout.strip() == "{}"
    assert len((tmp_path / "execution-log.jsonl").read_text().splitlines()) == 2


@pytest.mark.parametrize("payload", ["not json", "[]", '{"extra":[]}'])
def test_malformed_input_fails_open_without_echoing_payload(tmp_path, payload):
    result = run_hook(tmp_path, payload)
    assert result.returncode == 0 and result.stdout.strip() == "{}"
    assert not (tmp_path / "execution-log.jsonl").exists()
    assert payload not in result.stderr


@pytest.mark.platforms("posix")
def test_busy_log_or_fifo_never_waits_for_a_reader_or_lock(tmp_path):
    import fcntl

    log = tmp_path / "execution-log.jsonl"
    os.mkfifo(log)
    result = run_hook(tmp_path, "{}")
    assert result.returncode == 0 and result.stdout.strip() == "{}"
    log.unlink()
    with log.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run_hook(tmp_path, "{}")
        assert result.returncode == 0 and result.stdout.strip() == "{}"
        assert log.read_text() == ""


def test_native_shell_hook_callback_serializes_and_finishes(tmp_path, monkeypatch):
    from agent.shell_hooks import ShellHookSpec, _make_callback

    monkeypatch.setenv("HERMES_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_HOOK_DISTILL", "0")
    callback = _make_callback(ShellHookSpec(
        event="on_session_end", command=f'"{sys.executable}" "{HOOK}"', timeout=5))
    assert callback(session_id="native-probe", completed=True, platform="telegram") is None
    record = json.loads((tmp_path / "execution-log.jsonl").read_text())
    assert record["session_id"] == "native-probe"
    assert record["platform"] == "telegram"
    assert record["completed"] is True
