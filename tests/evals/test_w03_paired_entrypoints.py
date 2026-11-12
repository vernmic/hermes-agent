"""W03 entrypoint tests: the three peer-confirmed counterexamples
reproduced as red-on-W02 / green-on-W03 contracts, plus the corrected
capture wording. Real processes and real imported entrypoints; every
owned process is cleaned up; no Docker/models/live trials.
"""

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

FIX_SPEC = importlib.util.spec_from_file_location(
    "gfx", REPO / "tests/fixtures/tblite_paired/gate_fixtures.py")
gfx = importlib.util.module_from_spec(FIX_SPEC)
FIX_SPEC.loader.exec_module(gfx)

from evals.tblite_paired_gate import paired_gate as pg

HARBOR_SRC = "/home/vern/.cache/hermes-tblite-preflight/repos/harbor"
HARBOR_PY = "/home/vern/.cache/hermes-tblite-preflight/venv-harbor/bin/python"
HARBOR_BIN = "/home/vern/.cache/hermes-tblite-preflight/venv-harbor/bin/harbor"
MARK = "w03pair-marker"


def run_env(tmp_path, label="w03"):
    """Fixture plan env: synthetic git 'harbor source' bound to the
    protocol pin via gfx.write_plan (plumbing-only plan; fixture
    commands must use --allow-fixture-command)."""
    import hashlib

    hsrc = tmp_path / "harbor-src"
    hsrc.mkdir()
    subprocess.run(["git", "init", "-q", str(hsrc)], check=True)
    subprocess.run(["git", "-C", str(hsrc), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(hsrc), "config", "user.name", "t"], check=True)
    (hsrc / "x").write_text("x\n")
    subprocess.run(["git", "-C", str(hsrc), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(hsrc), "commit", "-qm", "h"], check=True)
    head = subprocess.run(["git", "-C", str(hsrc), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    sel = gfx.frozen_selection_file(tmp_path / "selection.json")
    proto = gfx.protocol_file(tmp_path / "protocol.json", sel,
                              harbor_commit=head, python_version=pyver)
    jobs_root = tmp_path / "jobs"
    gfx.write_plan(jobs_root,
                   json.loads(sel.read_text())["pools"]["fixture-pool"]["selected"],
                   harbor_source=str(hsrc))
    body = json.loads((jobs_root / "plan.json").read_text())
    body["protocol_sha256"] = hashlib.sha256(proto.read_bytes()).hexdigest()
    (jobs_root / "plan.json").write_text(json.dumps(body, indent=2))
    return proto, jobs_root, str(hsrc)


def run_args(proto, jobs, harbor_command, timeout=30, stop_after=0, fixture=True):
    return type("A", (), {
        "protocol": str(proto), "jobs_root": str(jobs),
        "harbor_command": harbor_command, "timeout": timeout,
        "stop_after": stop_after, "dry_run": False,
        "harbor_python": sys.executable, "harbor_source": "",
        "allow_fixture_command": fixture})()


# -- R1: dispatch bound to the measured pinned toolchain ------------------------


def test_r1_unbound_command_rejected_in_production_mode(tmp_path):
    proto, jobs, hsrc = run_env(tmp_path)
    args = run_args(proto, jobs, "echo WRONG-FRAMEWORK {config}", fixture=False)
    args.harbor_source = hsrc
    args.harbor_python = HARBOR_PY  # real venv: entrypoint exists, command still unbound
    with pytest.raises(SystemExit, match="entrypoint"):
        pg.cmd_run(args)


def test_r1_real_pinned_command_accepted_and_bound(tmp_path):
    """Production binding ACCEPTS the real pinned venv CLI invocation
    (verified pre-dispatch; dry-run executes nothing)."""
    proto, jobs, _hsrc = run_env(tmp_path)
    # protocol pin must equal the REAL pinned checkout for binding to hold
    import hashlib

    sel = gfx.frozen_selection_file(tmp_path / "selection.json")
    proto = gfx.protocol_file(tmp_path / "protocol.json", sel,
                              harbor_commit="500af91374dcc43b59fac54cf90295646617b14d",
                              python_version="3.14.7")
    gfx.write_plan(jobs,
                   json.loads(sel.read_text())["pools"]["fixture-pool"]["selected"],
                   harbor_source=HARBOR_SRC)
    body = json.loads((jobs / "plan.json").read_text())
    body["protocol_sha256"] = hashlib.sha256(proto.read_bytes()).hexdigest()
    (jobs / "plan.json").write_text(json.dumps(body, indent=2))
    args = run_args(proto, jobs, f'"{HARBOR_BIN}" run --config {{config}}', fixture=False)
    args.harbor_python = HARBOR_PY
    args.harbor_source = HARBOR_SRC
    args.dry_run = True
    rc = pg.cmd_run(args)
    assert rc == 0
    state = json.loads((jobs / "run-state.json").read_text())
    assert state["toolchain_binding"]["binding"] == "pinned"
    assert state["toolchain_binding"]["jobconfig_sha256"]


def test_r1_fixture_command_stamped_and_never_qualifies(tmp_path):
    proto, jobs, _hsrc = run_env(tmp_path)
    args = run_args(proto, jobs, f"echo FIXTURE {MARK} {{config}}", stop_after=1)
    assert pg.cmd_run(args) == 0
    state = json.loads((jobs / "run-state.json").read_text())
    assert state["toolchain_binding"]["binding"] == "fixture-only"
    with pytest.raises(SystemExit, match="NEVER qualify"):
        pg.cmd_qualify(type("A", (), {"protocol": str(proto), "jobs_root": str(jobs)})())


# -- R2: STARTED receipt before spawn; real interruption refuses resume ----------


def test_r2_real_parent_sigkill_leaves_inflight_receipt_resume_refuses(tmp_path):
    proto, jobs, _hsrc = run_env(tmp_path, label="r2")
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import sys; sys.path.insert(0, " + repr(str(REPO)) + ")\n"
        "from evals.tblite_paired_gate import paired_gate as pg\n"
        "pg.cmd_run(type('A', (), {'protocol': " + repr(str(proto)) + ", 'jobs_root': "
        + repr(str(jobs)) + ", 'harbor_command': " + repr(f"sleep 240 # {{config}} {MARK}")
        + ", 'timeout': 240, 'stop_after': 0, 'dry_run': False, 'harbor_python': "
        + repr(sys.executable) + ", 'harbor_source': '', 'allow_fixture_command': True})())\n"
    )
    parent = subprocess.Popen([sys.executable, str(driver)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)  # health: allow HX013 -- the in-flight window IS the interruption behaviour under test
    try:
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=10)
        progress = jobs / "progress.jsonl"
        records = ([json.loads(x) for x in progress.read_text().splitlines()]
                   if progress.is_file() else [])
        inflight = [r for r in records if r["status"] in ("started", "dispatched")]
        terminal = [r for r in records if str(r["status"]).startswith(("exit-", "timeout"))]
        assert inflight, "real SIGKILL left no in-flight receipt"
        assert not terminal
        # resume must REFUSE the interrupted job
        with pytest.raises(SystemExit, match="refusing to rerun"):
            pg.cmd_run(run_args(proto, jobs, f"echo redone {{config}} {MARK}"))
    finally:
        for pid in subprocess.run(["pgrep", "-f", MARK.replace("[", "[")],
                                  capture_output=True, text=True, check=False).stdout.split():
            subprocess.run(["kill", "-9", pid], check=False)


def test_r2_catchable_sigterm_leaves_terminal_receipt_and_cleans(tmp_path):
    proto, jobs, _hsrc = run_env(tmp_path, label="r2s")
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import sys; sys.path.insert(0, " + repr(str(REPO)) + ")\n"
        "from evals.tblite_paired_gate import paired_gate as pg\n"
        "try:\n"
        "    pg.cmd_run(type('A', (), {'protocol': " + repr(str(proto)) + ", 'jobs_root': "
        + repr(str(jobs)) + ", 'harbor_command': " + repr(f"sleep 240 # {{config}} {MARK}")
        + ", 'timeout': 240, 'stop_after': 0, 'dry_run': False, 'harbor_python': "
        + repr(sys.executable) + ", 'harbor_source': '', 'allow_fixture_command': True})())\n"
        "except SystemExit as e:\n"
        "    sys.exit(int(e.code or 0))\n"
    )
    parent = subprocess.Popen([sys.executable, str(driver)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)  # health: allow HX013 -- in-flight window before the catchable SIGTERM under test
    try:
        parent.send_signal(signal.SIGTERM)  # catchable interruption
        rc = parent.wait(timeout=30)
        records = [json.loads(x) for x in
                   (jobs / "progress.jsonl").read_text().splitlines()]
        statuses = [r["status"] for r in records]
        assert any(s.startswith("interrupted-") for s in statuses), statuses
        assert rc == 128 + signal.SIGTERM
        time.sleep(1)  # health: allow HX013 -- settle before owned-child check
        alive = subprocess.run(["pgrep", "-f", "sleep 240 #"],
                               capture_output=True, text=True, check=False).stdout.split()
        owned_alive = [p for p in alive if MARK in
                       subprocess.run(["cat", f"/proc/{p}/cmdline"],
                                      capture_output=True, text=True, check=False).stdout]
        assert not owned_alive, f"catchable SIGTERM left owned children: {owned_alive}"
    finally:
        for pid in subprocess.run(["pgrep", "-f", MARK],
                                  capture_output=True, text=True, check=False).stdout.split():
            subprocess.run(["kill", "-9", pid], check=False)


# -- R3: owned-group escalation past a SIGTERM-immune child ---------------------


def test_r3_sigterm_immune_grandchild_killed_at_timeout(tmp_path):
    script = tmp_path / "immune.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"(trap '' TERM; exec -a {MARK} sleep 300) &\n"
        f"exec -a {MARK}b sleep 300\n")
    script.chmod(0o755)
    started = {"run_id": "t", "arm": "baseline", "task_id": "t", "job": "j",
               "command": str(script), "config_sha256": "0", "protocol_sha256": "0",
               "status": "started", "started_at": "now"}
    try:
        rec = pg._run_job_receipted(str(script), 2, tmp_path / "job.log",
                                    tmp_path / "progress.jsonl", started)
        assert rec["status"] == "timeout" and rec["timed_out"] is True
        time.sleep(1)  # health: allow HX013 -- settle time before survivor check
        survivors = subprocess.run(["pgrep", "-f", MARK],
                                   capture_output=True, text=True, check=False).stdout.split()
        assert not survivors, f"owned survivors after terminal timeout: {survivors}"
    finally:
        for pid in subprocess.run(["pgrep", "-f", MARK],
                                  capture_output=True, text=True, check=False).stdout.split():
            subprocess.run(["kill", "-9", pid], check=False)


# -- R4: retained artifact cap wording/behavior ---------------------------------


def test_r4_retained_artifact_cap_and_honest_wording(tmp_path):
    rec = pg._run_job("python3 -c \"print('x'*300000)\"", 30, tmp_path / "big.log")
    assert rec["status"] == "exit-ok"
    log = (tmp_path / "big.log").read_text()
    assert len(log) < 300_000  # retained artifact IS capped...
    assert "retained artifact, capped" in log  # ...and says so honestly
