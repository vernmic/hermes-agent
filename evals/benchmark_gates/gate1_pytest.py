#!/usr/bin/env python3
"""Gate 1 (pytest) benchmark gate. Phase 7.2 / Wave 4 (T4).

The gate builds the pytest command from a declared interpreter. A caller
cannot supply a no-op, help, or collect-only command and have it promote.
Promotion requires executed passing tests and a record bound to the
candidate tree and the test contract. Production check ignores control
records and rejects a stale or mutated identity.

Scope limit: this gate certifies bounded pytest execution with staged
import-origin evidence only. It is NOT a semantic-quality certification
of the candidate. Staging supports top-level Python modules and packages
(implementation first, tests second, staged tree as import root); other
layouts (fixture/src/CLI trees) are not qualified and must fail honestly.

ASCII only.

Usage:
  gate1_pytest.py run --candidate-id ID --candidate PATH --tests PATH \
      [--python PATH] [--pytest-args JSON] [--timeout S] [--label TEXT] \
      [--control] [--log PATH] [--logs-dir PATH]
  gate1_pytest.py reject --candidate-id ID --command JSON \
      [--python PATH] [--log PATH]
  gate1_pytest.py check --candidate-id ID --candidate PATH --tests PATH \
      [--pytest-args JSON] [--inspect-control] [--log PATH]

The interpreter is '--python' when given (the pinned-interpreter option)
and otherwise the interpreter running this gate. Log destinations are
'--log'/'--logs-dir' when given, else alongside this script. Operator
configuration is explicit CLI only; no environment variables are read
for behaviour (GATE1_* variables below are internal parent/child IPC).
Exit 0 only for a qualifying promotion or a matching production check.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BENCH = Path(__file__).resolve().parent
GATE = "gate1_pytest"
SAFE_DEFAULT_ARGS = ["-q", "--no-header", "-p", "no:cacheprovider"]
BANNED_FLAGS = {
    "--collect-only", "--co", "--help", "-h", "--setup-only",
    "--setup-plan", "--fixtures", "--fixtures-per-test",
}
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git"}


def now_utc():
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def resolve_python(value):
    # abspath only, never symlink resolution: a venv's bin/python is a
    # symlink to a base interpreter without the venv's site-packages.
    python = Path(value) if value else Path(sys.executable)
    return os.path.abspath(str(python))


def default_log():
    return BENCH / "gate-log.jsonl"


def default_logs_dir():
    return BENCH / "logs"


def parse_pytest_counts(output):
    counts = {}
    for key, pat in (("passed", r"(\d+) passed"),
                     ("failed", r"(\d+) failed"),
                     ("errors", r"(\d+) error"),
                     ("skipped", r"(\d+) skipped")):
        m = re.search(pat, output)
        if m:
            counts[key] = int(m.group(1))
    return counts or None


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_sha(path):
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(str(path))
    h = hashlib.sha256()
    if path.is_file():
        h.update(b"file\0")
        h.update(file_sha(path).encode("ascii"))
        return h.hexdigest()
    files = []
    for p in path.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix == ".pyc":
            continue
        files.append(p)
    for p in sorted(files, key=lambda x: x.relative_to(path).as_posix()):
        rel = p.relative_to(path).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(file_sha(p).encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def contract_sha(tests, pytest_args):
    blob = json.dumps({
        "tests_sha256": tree_sha(tests),
        "pytest_args": list(pytest_args),
        "tests_path": str(Path(tests).resolve()),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def pinned_ok(python):
    return Path(python).is_file() and os.access(python, os.X_OK)


def build_command(python, tests, pytest_args):
    return [python, "-m", "pytest", *list(pytest_args), str(Path(tests).resolve())]


def _under(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def stage_for_run(candidate, tests):
    """Copy the candidate, then copy only trusted test files into that copy.

    External suites are not executed in place. Sibling reference modules
    next to those tests are not copied. Layout matches the oracle runner:
    implementation first, tests second, import root is the staged tree.
    """
    cand = Path(candidate).resolve()
    tests = Path(tests).resolve()
    work = Path(tempfile.mkdtemp(prefix="gate1-stage-"))
    stage = work / "stage"
    shutil.copytree(
        cand, stage,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
    )
    if _under(tests, cand):
        return work, stage, stage / tests.relative_to(cand), None
    dest = stage / "_gate_oracle"
    dest.mkdir()
    if tests.is_file():
        shutil.copy2(tests, dest / tests.name)
        return work, stage, dest / tests.name, tests.parent
    copied = 0
    for src in tests.rglob("*"):
        if not src.is_file():
            continue
        if src.suffix != ".py" and src.name not in {"pytest.ini", "tox.ini", "setup.cfg"}:
            continue
        if src.name.startswith("test_") or src.name.endswith("_test.py") or src.name in {
            "conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "test.py", "tests.py",
        }:
            rel = src.relative_to(tests)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            copied += 1
    if copied == 0:
        raise FileNotFoundError(f"external suite has no test files: {tests}")
    return work, stage, dest, tests


DRIVER = r"""
import json, os, sys
from pathlib import Path
root = Path(os.environ["GATE1_STAGE_ROOT"]).resolve()
external = os.environ.get("GATE1_EXTERNAL_ROOT") or ""
tests = os.environ["GATE1_STAGED_TESTS"]
sys.path = [str(root)] + [p for p in sys.path if p and (p.startswith(sys.prefix) or p.startswith(sys.base_prefix))]
os.chdir(root)
os.environ["PYTHONPATH"] = str(root)
import pytest
args = json.loads(os.environ["GATE1_PYTEST_ARGS"])
rc = pytest.main([*args, tests])
if hasattr(rc, "value"):
    rc = int(rc.value)
names = set()
for p in root.iterdir():
    if p.name.startswith("_gate") or p.name in {"tests", "test", "__pycache__"}:
        continue
    if p.suffix == ".py":
        names.add(p.stem)
    elif p.is_dir() and (p / "__init__.py").is_file():
        names.add(p.name)
bad, good = [], []
ext = Path(external).resolve() if external else None
for name, mod in list(sys.modules.items()):
    f = getattr(mod, "__file__", None)
    if not f:
        continue
    path = Path(f).resolve()
    top = name.split(".")[0]
    if ext is not None and str(path).startswith(str(ext)):
        bad.append({"module": name, "path": str(path), "reason": "external_reference"})
        continue
    if top in names and not str(path).startswith(str(root)):
        bad.append({"module": name, "path": str(path), "reason": "upstream_shadow"})
        continue
    if top in names and str(path).startswith(str(root)):
        good.append({"module": name, "path": str(path)})
print("GATE_RESULT " + json.dumps({"exit_code": rc, "bad": bad, "good": good}))
"""


def run_staged(python, stage, staged_tests, external, pytest_args, timeout):
    driver = stage.parent / "gate_driver.py"
    driver.write_text(DRIVER, encoding="ascii")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(stage)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GATE1_STAGE_ROOT"] = str(stage)
    env["GATE1_STAGED_TESTS"] = str(staged_tests)
    env["GATE1_EXTERNAL_ROOT"] = str(external) if external else ""
    env["GATE1_PYTEST_ARGS"] = json.dumps(list(pytest_args))
    for key in ("PYTHONHOME",):
        env.pop(key, None)
    return subprocess.run(
        [python, str(driver)], cwd=str(stage), capture_output=True,
        text=True, timeout=timeout, errors="replace", env=env, check=False,
    )


def banned_reason(python, argv):
    flags = set(argv)
    hit = sorted(flags & BANNED_FLAGS)
    if hit:
        if any(x in hit for x in ("--collect-only", "--co")):
            return "collect_only"
        if any(x in hit for x in ("--help", "-h")):
            return "help"
        return "nonqualifying_flag"
    if "-c" in argv:
        return "no_op"
    if len(argv) >= 3 and argv[1] == "-m" and argv[2] != "pytest":
        return "not_pytest"
    if argv and argv[0] != python:
        return "interpreter_mismatch"
    if argv[:3] != [python, "-m", "pytest"]:
        return "not_pytest"
    return None


def append_record(log, record):
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="ascii", errors="replace") as fh:
        fh.write(json.dumps(record, ensure_ascii=True) + "\n")


def base_record(args, command, cwd, outcome, decision, **extra):
    rec = {
        "gate": GATE,
        "run_id": extra.pop("run_id"),
        "timestamp_utc": now_utc(),
        "candidate_id": args.candidate_id,
        "label": getattr(args, "label", "") or "",
        "command": command,
        "cwd": cwd,
        "timeout_s": getattr(args, "timeout", None),
        "outcome": outcome,
        "exit_code": extra.pop("exit_code", None),
        "timed_out": extra.pop("timed_out", False),
        "pytest": extra.pop("pytest", None),
        "log_path": extra.pop("log_path", None),
        "decision": decision,
        "control": bool(getattr(args, "control", False)),
        "interpreter": extra.pop("interpreter", None),
        "candidate_sha256": extra.pop("candidate_sha256", None),
        "test_contract_sha256": extra.pop("test_contract_sha256", None),
        "qualifying": extra.pop("qualifying", False),
    }
    rec.update(extra)
    return rec


def classify(exit_code, timed_out, counts, argv_reason):
    if argv_reason:
        return argv_reason, False
    if timed_out:
        return "timeout", False
    if exit_code is None:
        return "command_error", False
    if exit_code == 4:
        return "usage_error", False
    if exit_code not in (0, 1, 5):
        return "command_error", False
    passed = (counts or {}).get("passed", 0)
    failed = (counts or {}).get("failed", 0)
    errors = (counts or {}).get("errors", 0)
    skipped = (counts or {}).get("skipped", 0)
    executed = passed + failed + errors
    if exit_code == 5 or counts is None or executed == 0 and skipped == 0:
        return "empty_suite", False
    if executed == 0 and skipped > 0:
        return "all_skipped", False
    if failed or errors or exit_code == 1:
        return "test_failure", False
    if exit_code == 0 and passed > 0 and failed == 0 and errors == 0:
        return "pass", True
    return "command_error", False


def cmd_reject(args, python, log):
    command = json.loads(args.command)
    reason = banned_reason(python, command) or "not_pytest"
    interpreter = command[0] if command else None
    run_id = hashlib.sha256(json.dumps(command).encode("utf-8")).hexdigest()[:12]
    record = base_record(
        args, command, str(BENCH), reason, "block",
        run_id=run_id, interpreter=interpreter, qualifying=False,
    )
    append_record(log, record)
    print(json.dumps({"decision": "block", "outcome": reason,
                      "run_id": run_id, "pytest": None,
                      "interpreter": interpreter}))
    return 1


def cmd_run(args, python, log, logs_dir):
    if not pinned_ok(python):
        print(json.dumps({"decision": "block", "outcome": "pinned_interpreter_missing"}))
        return 1
    pytest_args = json.loads(args.pytest_args) if args.pytest_args else list(SAFE_DEFAULT_ARGS)
    if not isinstance(pytest_args, list) or not all(isinstance(x, str) for x in pytest_args):
        print(json.dumps({"decision": "block", "outcome": "usage_error"}))
        return 1
    command = build_command(python, args.tests, pytest_args)
    reason = banned_reason(python, command)
    if reason:
        args.command = json.dumps(command)
        return cmd_reject(args, python, log)
    try:
        candidate_sha = tree_sha(args.candidate)
        contract = contract_sha(args.tests, pytest_args)
        work, stage, staged_tests, external = stage_for_run(args.candidate, args.tests)
    except FileNotFoundError as exc:
        print(json.dumps({"decision": "block", "outcome": "missing_path",
                          "detail": str(exc)}))
        return 1
    cwd = str(stage)
    command = [python, str(stage.parent / "gate_driver.py")]
    run_id = hashlib.sha256((candidate_sha + contract + now_utc()).encode("ascii")).hexdigest()[:12]
    timed_out = False
    origin = {"bad": [], "good": []}
    try:
        proc = run_staged(python, stage, staged_tests, external, pytest_args, args.timeout)
        exit_code = proc.returncode
        output = (proc.stdout or "") + (proc.stderr or "")
        saw_result = False
        for line in output.splitlines():
            if line.startswith("GATE_RESULT "):
                origin = json.loads(line[len("GATE_RESULT "):])
                exit_code = origin.get("exit_code", exit_code)
                saw_result = True
        if not saw_result:
            exit_code = 1 if exit_code in (0, None) else exit_code
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        output = out + err
    except OSError as exc:
        record = base_record(
            args, command, cwd, "command_error", "block",
            run_id=run_id, interpreter=python, spawn_error=str(exc),
            candidate_sha256=candidate_sha, test_contract_sha256=contract,
            import_origin_ok=False,
        )
        append_record(log, record)
        shutil.rmtree(work, ignore_errors=True)
        print(json.dumps({"decision": "block", "outcome": "command_error",
                          "run_id": run_id}))
        return 1
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / (f"{GATE}-{run_id}.log")
    log_path.write_text(output, encoding="utf-8", errors="replace")
    counts = parse_pytest_counts(output)
    outcome, qualifying = classify(exit_code, timed_out, counts, None)
    bad = origin.get("bad") or []
    good = origin.get("good") or []
    if bad:
        outcome, qualifying = "import_shadow", False
    elif qualifying and not good:
        outcome, qualifying = "origin_unproven", False
    decision = "promote" if qualifying else "block"
    record = base_record(
        args, command, cwd, outcome, decision,
        run_id=run_id, exit_code=exit_code, timed_out=timed_out,
        pytest=counts, log_path=str(log_path), interpreter=python,
        candidate_sha256=candidate_sha, test_contract_sha256=contract,
        qualifying=qualifying, import_origin_ok=bool(qualifying and good and not bad),
        import_origins={"bad": bad, "good": good},
        staged_tests=str(staged_tests),
        external_root=str(external) if external else None,
    )
    append_record(log, record)
    shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({"decision": decision, "outcome": outcome,
                      "exit_code": exit_code, "run_id": run_id,
                      "pytest": counts, "interpreter": python,
                      "qualifying": qualifying,
                      "import_origin_ok": record["import_origin_ok"]}))
    return 0 if decision == "promote" else 1


def iter_records(log):
    if not log.exists():
        return
    for line in log.read_text(encoding="ascii", errors="replace").splitlines():
        if line.strip():
            yield json.loads(line)


def cmd_check(args, log):
    pytest_args = json.loads(args.pytest_args) if args.pytest_args else list(SAFE_DEFAULT_ARGS)
    try:
        candidate_sha = tree_sha(args.candidate)
        contract = contract_sha(args.tests, pytest_args)
    except FileNotFoundError as exc:
        print(json.dumps({"decision": "block", "reason": "missing_path",
                          "detail": str(exc)}))
        return 1
    latest = None
    for rec in iter_records(log) or []:
        if rec.get("gate") != GATE or rec.get("candidate_id") != args.candidate_id:
            continue
        if rec.get("control") and not args.inspect_control:
            continue
        latest = rec
    if latest is None:
        reason = "control_only" if any(
            r.get("candidate_id") == args.candidate_id and r.get("control")
            for r in iter_records(log) or []
        ) else "no_record"
        print(json.dumps({"candidate_id": args.candidate_id,
                          "decision": "block", "reason": reason}))
        return 1
    if args.inspect_control:
        print(json.dumps({"candidate_id": args.candidate_id,
                          "decision": latest.get("decision"),
                          "outcome": latest.get("outcome"),
                          "run_id": latest.get("run_id"),
                          "inspect_control": True,
                          "production": False}))
        return 0 if latest.get("decision") == "promote" else 1
    if latest.get("control"):
        print(json.dumps({"decision": "block", "reason": "control_only"}))
        return 1
    if not latest.get("qualifying") or latest.get("decision") != "promote" or not latest.get("import_origin_ok"):
        print(json.dumps({"decision": "block", "reason": latest.get("outcome") or "origin_unproven",
                          "run_id": latest.get("run_id")}))
        return 1
    if latest.get("candidate_sha256") != candidate_sha or latest.get("test_contract_sha256") != contract:
        print(json.dumps({"decision": "block", "reason": "identity_mismatch",
                          "run_id": latest.get("run_id")}))
        return 1
    print(json.dumps({"candidate_id": args.candidate_id, "decision": "promote",
                      "outcome": latest.get("outcome"), "run_id": latest.get("run_id"),
                      "production": True}))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--candidate-id", required=True)
    r.add_argument("--candidate", required=True)
    r.add_argument("--tests", required=True)
    r.add_argument("--python", default="")
    r.add_argument("--pytest-args", default="")
    r.add_argument("--timeout", type=int, default=300)
    r.add_argument("--label", default="")
    r.add_argument("--control", action="store_true")
    r.add_argument("--log", default="")
    r.add_argument("--logs-dir", default="")
    j = sub.add_parser("reject")
    j.add_argument("--candidate-id", required=True)
    j.add_argument("--command", required=True)
    j.add_argument("--python", default="")
    j.add_argument("--label", default="")
    j.add_argument("--control", action="store_true")
    j.add_argument("--log", default="")
    c = sub.add_parser("check")
    c.add_argument("--candidate-id", required=True)
    c.add_argument("--candidate", required=True)
    c.add_argument("--tests", required=True)
    c.add_argument("--python", default="")
    c.add_argument("--pytest-args", default="")
    c.add_argument("--inspect-control", action="store_true")
    c.add_argument("--log", default="")
    args = ap.parse_args()
    python = resolve_python(args.python)
    log = Path(args.log) if args.log else default_log()
    if args.cmd == "run":
        logs_dir = Path(args.logs_dir) if args.logs_dir else default_logs_dir()
        sys.exit(cmd_run(args, python, log, logs_dir))
    if args.cmd == "reject":
        sys.exit(cmd_reject(args, python, log))
    sys.exit(cmd_check(args, log))


if __name__ == "__main__":
    main()
