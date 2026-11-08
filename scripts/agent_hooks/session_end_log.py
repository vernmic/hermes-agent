#!/usr/bin/env python3
"""A bounded-work, fail-open session-end logging observer. No agent/model calls."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
import stat
import sys

try:
    import fcntl
except ImportError:
    fcntl = None

MAX_PAYLOAD_BYTES = 1_048_576


def append_record(path: Path, record: dict | str) -> None:
    line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) if isinstance(record, dict) else record
    data = (line.rstrip("\n") + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("hook log must be a regular file")
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.write(fd, data) != len(data):
            raise OSError("incomplete hook log append")
    finally:
        os.close(fd)


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise ValueError("hook payload too large")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be an object")
        extra = payload.get("extra", {})
        if not isinstance(extra, dict):
            raise ValueError("hook extra must be an object")
        workspace_value = os.environ.get("HERMES_WORKSPACE_DIR")
        if not workspace_value:
            home = os.environ.get("HERMES_HOME")
            if not home:
                raise ValueError("missing routed HERMES_HOME")
            workspace_value = str(Path(home) / "workspace")
        workspace = Path(workspace_value)
        workspace.mkdir(parents=True, exist_ok=True)
        record = {"hook_event_name": "on_session_end",
                  "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z")}
        for key, source in [("session_id", payload), ("profile", payload),
                            ("turn_id", extra), ("task_id", extra),
                            ("model", extra), ("platform", extra),
                            ("turn_exit_reason", extra)]:
            record[key] = str(source.get(key) or "")[:512]
        for key in ("completed", "failed", "interrupted"):
            record[key] = bool(extra.get(key, False))
        append_record(workspace / "execution-log.jsonl", record)
        cost = extra.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 2:
            append_record(workspace / "expensive-sessions.txt",
                          f"{record['ts']}  {record['session_id']}  cost={cost}")
        # The old opt-in nested agent had no safe per-turn trigger and was disabled.
        # Learning belongs in the existing reviewed GEPA workflow, never this observer.
        if os.environ.get("HERMES_HOOK_DISTILL") == "1":
            print("session-end: inline distillation remains disabled; use the reviewed GEPA workflow",
                  file=sys.stderr)
    except (OSError, ValueError, TypeError) as exc:
        # Do not echo payloads (they can contain private conversation data).
        print(f"session-end: observation skipped ({type(exc).__name__})", file=sys.stderr)
    finally:
        print("{}")


if __name__ == "__main__":
    main()
