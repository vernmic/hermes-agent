"""TBLite paired-20 regression gate: explicit runner + qualification
(Phase 7.2). SOURCE/OFFLINE ONLY until the reviewed protocol and
candidate prerequisites pass.

W02 corrections (per the W01 review):

- Task/result binding: every trial must echo the PLANNED task --
  task_name, trial_name prefix, task_id path and the canonical
  task_checksum FROZEN IN THE PLAN (measured offline through the
  pinned Harbor 500af913 Task API by the operator-supplied
  --checksum-python interpreter). The same wrong checksum on both
  arms no longer qualifies; a renamed trial directory cannot mask
  another task's result.
- Bound plans/configs: `qualify` refuses to run without the plan that
  was built from the current protocol (protocol sha + per-config byte
  hashes re-verified) and without the run-state receipt of the run
  being qualified. Old, stale (pre-run), orphan or mutated trees are
  UNQUALIFIED; nothing is silently overwritten.
- Real Harbor nesting: jobs live at <jobs_root>/<arm>/<job_name>/ and
  trials at <job_name>/<task_name>__<suffix>/ exactly as the pinned
  framework lays them out (observed: hermes-gated-control-w03). Planned
  configs parse against the pinned JobConfig schema (verified in tests
  and receipts through the installed primary source).
- Bounded runner: finite positive --timeout required for live dispatch;
  each job runs in its OWN process group with terminal receipts on
  ok/nonzero/timeout, owned-group-only cleanup after timeout/interrupt,
  infra status distinguished from measured outcomes, no-duplicate
  resume (completed jobs skipped; interrupted jobs clearly refused,
  never silently rerun to replace unfavorable results), and dry runs
  never counted as attempts.
- Tool identities measured, not declared: --harbor-source git HEAD and
  --harbor-python version are MEASURED before dispatch, bound into the
  run state and re-verified at qualification; the dataset checkout
  commit is verified at plan time and each task's canonical checksum
  is measured through the pinned Harbor Task API.

Fail-closed rules are unchanged: a missing, duplicated, dropped,
exceptional, timeout, malformed, non-finite, out-of-range,
identity-drifted or otherwise unmeasurable trial makes the whole
qualification UNQUALIFIED -- never scored 0, never a pass. The
nonregression policy stays PREDECLARED and hash-bound before any
outcomes; gate2 (tblite_gate.py) stays fail-closed NOT RUN.

ASCII only.
"""

import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

ARMS = ("baseline", "candidate")
_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA_LINE_RE = re.compile(r"^([0-9a-f]{64})\s+\S+/skills/([^/]+)/SKILL\.md$")
POLICY_NAME = "frozen20-paired-no-regression"
POLICY_VERSION = 1

# Offline snippet run by the operator-supplied pinned-Harbor interpreter
# (--checksum-python): measures the canonical task checksum through the
# PRIMARY installed Task implementation -- the gate never reimplements
# the digest algorithm.
_CHECKSUM_SNIPPET = (
    "import sys, warnings;"
    "sys.path.insert(0, sys.argv[1]);"
    "from harbor.models.task.task import Task;"
    "warnings.simplefilter('ignore');"
    "print(Task(sys.argv[2]).checksum)"
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_positive(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SystemExit(f"{name} must be a finite positive integer (got {value!r})")
    return value


@dataclass(frozen=True)
class PairedProtocol:
    """Everything that must be identical across both arms except the
    skill revision. Hash-bound: the protocol file's sha256 is stamped
    into every receipt and report."""

    path: Path
    body: dict

    @classmethod
    def load(cls, path: Path) -> "PairedProtocol":
        body = json.loads(Path(path).read_text())
        required = {
            "dataset_commit", "harbor_commit", "hermes_core_commit",
            "model", "provider", "base_url", "agent_module",
            "python_version", "frozen_selection", "baseline_skill",
            "candidate_skill", "policy", "task_root",
        }
        missing = required - set(body)
        if missing:
            raise SystemExit(f"protocol missing fields: {sorted(missing)}")
        if body["policy"].get("name") != POLICY_NAME or body["policy"].get("version") != POLICY_VERSION:
            raise SystemExit(
                f"unrecognized/undeclared policy {body['policy']!r}; refusing "
                "to choose a nonregression rule from outcomes"
            )
        return cls(path=Path(path), body=body)

    @property
    def sha256(self) -> str:
        return _sha256_file(self.path)

    def skill(self, arm: str) -> dict:
        return self.body[f"{arm}_skill"]

    def identity(self, arm: str) -> dict:
        return {
            "skill_file_sha256": self.skill(arm)["sha256"],
            "skill_name": self.skill(arm)["preload_name"],
            "hermes_core_commit": self.body["hermes_core_commit"],
            "base_url": self.body["base_url"],
            "model": self.body["model"],
            "provider": self.body["provider"],
        }


def load_frozen_selection(protocol: PairedProtocol) -> list[str]:
    """Load the pre-outcome frozen 20 selection and bind it by hash."""
    sel_path = Path(protocol.body["frozen_selection"]["path"])
    expected_sha = protocol.body["frozen_selection"]["sha256"]
    actual = _sha256_file(sel_path)
    if actual != expected_sha:
        raise SystemExit(
            f"frozen selection sha256 mismatch: protocol bound {expected_sha}, "
            f"file is {actual} -- stale or replaced selection; fail closed"
        )
    sel = json.loads(sel_path.read_text())
    if sel.get("dataset_commit") != protocol.body["dataset_commit"]:
        raise SystemExit("selection dataset pin differs from protocol dataset pin")
    ids = sorted(
        task_id
        for pool in sel["pools"].values()
        for task_id in pool["selected"]
    )
    if len(ids) != 20 or len(set(ids)) != 20:
        raise SystemExit(f"selection is not a unique frozen-20 set ({len(ids)} ids)")
    return ids


# -- measured tool identities (W02 finding 5) ----------------------------------


def measure_harbor_head(harbor_source: str) -> str:
    """MEASURE the pinned Harbor checkout's git HEAD (read-only)."""
    proc = subprocess.run(
        ["git", "-C", str(harbor_source), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    head = proc.stdout.strip()
    if proc.returncode != 0 or not _HEAD_RE.match(head):
        raise SystemExit(
            f"cannot measure harbor source HEAD at {harbor_source}: "
            f"{(proc.stderr or proc.stdout).strip()[:160]}"
        )
    return head


def measure_python_version(python: str) -> str:
    """MEASURE the actual interpreter version string."""
    proc = subprocess.run(
        [str(python), "-c", "import sys; print(sys.version.split()[0])"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    version = proc.stdout.strip()
    if proc.returncode != 0 or not version:
        raise SystemExit(
            f"cannot measure python version of {python}: "
            f"{(proc.stderr or proc.stdout).strip()[:160]}"
        )
    return version


def measure_dataset_commit(task_root: str) -> str:
    """MEASURE the dataset checkout commit at plan time."""
    proc = subprocess.run(
        ["git", "-C", str(task_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    head = proc.stdout.strip()
    if proc.returncode != 0 or not _HEAD_RE.match(head):
        raise SystemExit(
            f"cannot measure dataset checkout HEAD at {task_root}: "
            f"{(proc.stderr or proc.stdout).strip()[:160]}"
        )
    return head


def measure_task_checksum(checksum_python: str, harbor_src: str, task_dir: Path) -> str:
    """Canonical task checksum THROUGH the pinned Harbor Task API run by
    the operator-supplied interpreter (primary source, not a
    reimplementation)."""
    proc = subprocess.run(
        [str(checksum_python), "-c", _CHECKSUM_SNIPPET, str(harbor_src), str(task_dir)],
        capture_output=True, text=True, timeout=300, check=False,
    )
    digest = proc.stdout.strip()
    if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
        raise SystemExit(
            "canonical task checksum measurement failed through the pinned "
            f"Harbor API for {task_dir}: {(proc.stderr or proc.stdout).strip()[:200]}"
        )
    return digest


# -- job planning (production Harbor 500af913 schema) --------------------------


def job_config(protocol: PairedProtocol, arm: str, task_id: str, job_name: str,
               jobs_dir: str) -> dict:
    """One Harbor job config per task, in the exact schema the pinned
    framework consumed in the accepted preflight controls (source:
    hermes-gated-control-w03/config.json)."""
    return {
        "job_name": job_name,
        "jobs_dir": jobs_dir,
        "agent_timeout_multiplier": 1.0,
        "agent_setup_timeout_multiplier": 1.5,
        "n_concurrent_trials": 1,
        "environment": {
            "type": "docker",
            "override_cpus": 2,
            "override_memory_mb": 4096,
        },
        "agents": [
            {
                "name": protocol.body["agent_module"],
                "model_name": f"{protocol.body['provider']}/{protocol.body['model']}",
                "base_url": protocol.body["base_url"],
                "skills": [protocol.skill(arm)["path"]],
            }
        ],
        "tasks": [{"path": f"{protocol.body['task_root']}/{task_id}"}],
    }


def cmd_plan(args: argparse.Namespace) -> int:
    protocol = PairedProtocol.load(Path(args.protocol))
    task_ids = load_frozen_selection(protocol)
    # W02: the dataset checkout is MEASURED, never trusted from the file
    measured_dataset = measure_dataset_commit(protocol.body["task_root"])
    if measured_dataset != protocol.body["dataset_commit"]:
        raise SystemExit(
            f"dataset checkout HEAD {measured_dataset} != protocol pin "
            f"{protocol.body['dataset_commit']}; replan against the pinned checkout"
        )
    jobs_root = Path(args.jobs_root)
    jobs_root.mkdir(parents=True, exist_ok=True)
    plan = {
        "created_at": _utc_now(),
        "protocol_sha256": protocol.sha256,
        "measured_dataset_commit": measured_dataset,
        "checksum_python": str(Path(args.checksum_python)),
        "harbor_source": str(Path(args.harbor_source)),
        "arms": {},
    }
    for arm in ARMS:
        arm_dir = jobs_root / arm
        arm_dir.mkdir(exist_ok=True)
        entries = []
        for task_id in task_ids:
            job_name = f"{args.label}-{arm}-{task_id}"
            cfg = job_config(protocol, arm, task_id, job_name, str(arm_dir))
            cfg_path = arm_dir / f"{job_name}.config.json"
            cfg_path.write_text(json.dumps(cfg, indent=2))
            task_dir = Path(protocol.body["task_root"]) / task_id
            entries.append({
                "task_id": task_id,
                "config": str(cfg_path),
                "config_sha256": _sha256_file(cfg_path),
                "job_name": job_name,
                "expected_task_checksum": measure_task_checksum(
                    args.checksum_python, args.harbor_source, task_dir
                ),
            })
        plan["arms"][arm] = {"expected_identity": protocol.identity(arm),
                             "jobs": entries}
    plan_path = jobs_root / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2))
    print(f"[plan] 2 arms x {len(task_ids)} tasks; canonical checksums frozen; "
          f"plan={plan_path} protocol_sha256={protocol.sha256}")
    return 0


# -- bounded runner (W02 finding 4) --------------------------------------------


def _terminate_owned_group(proc: subprocess.Popen) -> None:
    """Bounded termination of ONLY the process group this runner created
    (start_new_session makes the child its own group leader).

    W03: escalation tracks the WHOLE owned group, not the leader. A
    SIGTERM-ignoring child in the group keeps the group alive past the
    leader's exit, so the grace loop checks the GROUP (killpg(pgid, 0)),
    never a leader poll short-circuit, and escalates to SIGKILL for the
    group while any member remains. Only this runner's own pgid is ever
    signalled."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 5.0
    group_alive = True
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            group_alive = False
            break
        time.sleep(0.1)
    if group_alive:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    # Reap the leader so it cannot linger as a zombie.
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _run_job(command: str, timeout: int, log_path: Path) -> dict:
    """Run one job in its OWN process group; always returns a TERMINAL
    status record (ok / exit-N / timeout).

    Retained log artifacts are capped at 200KB; the TRANSIENT capture
    via communicate() is NOT memory-bounded (W03 wording corrected)."""
    proc = subprocess.Popen(
        command, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_owned_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
    # Retained artifact cap (transient capture above is unbounded).
    stdout = (stdout or "")[:200_000]
    stderr = (stderr or "")[:200_000]
    log_path.write_text(
        f"command={command}\nexit={proc.returncode}\ntimed_out={timed_out}\n"
        f"--- stdout (retained artifact, capped) ---\n{stdout}\n"
        f"--- stderr (retained artifact, capped) ---\n{stderr}",
        encoding="utf-8", errors="replace",
    )
    if timed_out:
        status = "timeout"
    elif proc.returncode == 0:
        status = "exit-ok"
    else:
        status = f"exit-{proc.returncode}"
    return {"status": status, "returncode": proc.returncode, "timed_out": timed_out}


def _run_job_receipted(command: str, timeout: int, log_path: Path,
                       progress: Path, started: dict) -> dict:
    """Run one job with catchable-signal hygiene: the owned process is
    spawned, its pid/pgid are appended to the in-flight receipt, and on
    SIGINT/SIGTERM (catchable) the OWNED group is boundedly terminated
    and an 'interrupted' terminal receipt is appended before the signal
    propagates. A hard SIGKILL cannot run Python cleanup -- the already
    persisted STARTED receipt is what makes resume refuse afterwards;
    owned pid/pgid are recorded for explicit operator reconciliation."""
    proc = subprocess.Popen(
        command, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    inflight = dict(started)
    inflight["status"] = "dispatched"
    inflight["owned_pid"] = proc.pid
    try:
        inflight["owned_pgid"] = os.getpgid(proc.pid)
    except ProcessLookupError:
        inflight["owned_pgid"] = proc.pid
    with open(progress, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(inflight) + "\n")

    def _terminal_for(status: str, timed_out: bool) -> dict:
        rec = dict(inflight)
        rec.update({"status": status, "returncode": proc.returncode,
                    "timed_out": timed_out})
        return rec

    old_int = old_term = None
    interrupted = {"signum": None}

    def _on_signal(signum, _frame):  # pragma: no cover - timing dependent
        interrupted["signum"] = signum
        raise KeyboardInterrupt

    try:
        old_int = signal.signal(signal.SIGINT, _on_signal)
        old_term = signal.signal(signal.SIGTERM, _on_signal)
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_owned_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
        stdout = (stdout or "")[:200_000]
        stderr = (stderr or "")[:200_000]
        log_path.write_text(
            f"command={command}\nexit={proc.returncode}\ntimed_out={timed_out}\n"
            f"--- stdout (retained artifact, capped) ---\n{stdout}\n"
            f"--- stderr (retained artifact, capped) ---\n{stderr}",
            encoding="utf-8", errors="replace",
        )
        if timed_out:
            return _terminal_for("timeout", True)
        return _terminal_for(
            "exit-ok" if proc.returncode == 0 else f"exit-{proc.returncode}",
            False,
        )
    except KeyboardInterrupt:
        # Catchable SIGINT/SIGTERM: boundedly clean the OWNED group and
        # append a terminal receipt; then propagate.
        _terminate_owned_group(proc)
        rec = _terminal_for(
            f"interrupted-{interrupted['signum'] or signal.SIGINT}", False)
        with open(progress, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        raise SystemExit(128 + (interrupted["signum"] or signal.SIGINT))
    finally:
        if old_int is not None:
            signal.signal(signal.SIGINT, old_int)
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)


def _load_progress(progress: Path) -> list[dict]:
    if not progress.is_file():
        return []
    return [json.loads(line) for line in progress.read_text().splitlines() if line.strip()]


def cmd_run(args: argparse.Namespace) -> int:
    """Execute the plan sequentially (max concurrency 1) with bounded,
    receipted, resumable dispatch. The harbor command is an explicit
    operator template ({config} substituted); nothing is invented here.
    NOT part of offline validation."""
    if not args.harbor_command or "{config}" not in args.harbor_command:
        raise SystemExit(
            "run requires an explicit --harbor-command template containing "
            "{config}; the paired gate never guesses a live invocation"
        )
    _require_positive(args.timeout, "--timeout (finite positive bound required "
                     "for live dispatch; unlimited is refused)")
    plan_path = Path(args.jobs_root) / "plan.json"
    if not plan_path.is_file():
        raise SystemExit(f"no plan.json under {args.jobs_root}; run 'plan' first")
    plan = json.loads(plan_path.read_text())
    protocol = PairedProtocol.load(Path(args.protocol))
    if plan.get("protocol_sha256") != protocol.sha256:
        raise SystemExit("plan was built against a different protocol; replan")
    # W02: measured tool identities, bound into the run state BEFORE dispatch
    measured_head = measure_harbor_head(plan.get("harbor_source") or args.harbor_source)
    if measured_head != protocol.body["harbor_commit"]:
        raise SystemExit(
            f"measured harbor HEAD {measured_head} != protocol pin "
            f"{protocol.body['harbor_commit']}; refusing to dispatch another "
            "framework version"
        )
    measured_py = measure_python_version(args.harbor_python)
    if measured_py != protocol.body["python_version"]:
        raise SystemExit(
            f"measured harbor python {measured_py} != protocol pin "
            f"{protocol.body['python_version']}; refusing to dispatch"
        )
    # W03: bind the ACTUAL dispatch to the measured pinned toolchain.
    # Production commands must invoke the pinned venv's real harbor
    # console entrypoint; fixture-only commands are accepted ONLY under
    # an explicit label and can never qualify for the live gate.
    harbor_source = plan.get("harbor_source") or args.harbor_source
    if getattr(args, "allow_fixture_command", False):
        toolchain_binding = {
            "binding": "fixture-only",
            "note": "fixture command explicitly allowed; run is NOT "
                    "qualified for the live gate",
        }
    else:
        toolchain_binding = verify_toolchain_binding(
            args.harbor_command, args.harbor_python, harbor_source,
            protocol.body["harbor_commit"],
        )
    progress = Path(args.jobs_root) / "progress.jsonl"
    prior = _load_progress(progress)
    by_job: dict[str, list[dict]] = {}
    for record in prior:
        by_job.setdefault(record.get("job", ""), []).append(record)
    run_state_path = Path(args.jobs_root) / "run-state.json"
    if run_state_path.is_file():
        run_state = json.loads(run_state_path.read_text())
        # W03: an existing run state must still bind THIS protocol+plan
        # before ANY resume/dispatch.
        if run_state.get("protocol_sha256") != protocol.sha256 or \
                run_state.get("plan_sha256") != _sha256_file(plan_path):
            raise SystemExit(
                "existing run-state does not bind this protocol+plan; "
                "fail closed -- inspect and archive the old run explicitly"
            )
    else:
        run_state = {
            "run_id": hashlib.sha256(
                (protocol.sha256 + plan_path.name + _utc_now()).encode("ascii")
            ).hexdigest()[:16],
            "started_at": _utc_now(),
            "protocol_sha256": protocol.sha256,
            "plan_sha256": _sha256_file(plan_path),
            "measured_harbor_head": measured_head,
            "measured_python_version": measured_py,
            "toolchain_binding": toolchain_binding,
        }
        run_state_path.write_text(json.dumps(run_state, indent=2))
    done = 0
    infra_failures = []
    for arm in ARMS:
        for entry in plan["arms"][arm]["jobs"]:
            records = by_job.get(entry["job_name"], [])
            terminals = [r for r in records if r.get("status") in
                         ("exit-ok", "timeout") or str(r.get("status", "")).startswith("exit-")]
            if terminals:
                print(f"[run] {entry['job_name']}: already terminal "
                      f"({terminals[-1]['status']}); resumed without rerun")
                continue
            if records:
                raise SystemExit(
                    f"job {entry['job_name']} has a non-terminal progress record "
                    "(interrupted run); refusing to rerun -- inspect the attempt "
                    "and archive it explicitly; attempts are never silently "
                    "replaced"
                )
            # W02: config bytes verified against the reviewed plan before dispatch
            actual_cfg = _sha256_file(Path(entry["config"]))
            if actual_cfg != entry["config_sha256"]:
                raise SystemExit(
                    f"config {entry['config']} drifted from the reviewed plan "
                    f"({actual_cfg[:16]}... != {entry['config_sha256'][:16]}...); "
                    "replan; refusing to dispatch a mutated configuration"
                )
            if args.stop_after and done >= args.stop_after:
                print(f"[run] stop-after {args.stop_after} reached; state persists")
                if infra_failures:
                    return 4
                return 0
            cmd = args.harbor_command.format(config=entry["config"])
            # W03: persist the STARTED identity BEFORE spawning, so a hard
            # parent kill mid-job leaves an in-flight receipt and any later
            # resume REFUSES (records below append owned pid + terminal).
            started = {"run_id": run_state["run_id"], "arm": arm,
                       "task_id": entry["task_id"], "job": entry["job_name"],
                       "command": cmd, "config_sha256": entry["config_sha256"],
                       "protocol_sha256": protocol.sha256,
                       "status": "started", "started_at": _utc_now()}
            print(f"[run] {arm}/{entry['task_id']}: {cmd}")
            if args.dry_run:
                record = dict(started)
                record["status"] = "dry-run"
            else:
                with open(progress, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(started) + "\n")
                log_dir = Path(args.jobs_root) / "run-logs"
                log_dir.mkdir(exist_ok=True)
                record = _run_job_receipted(
                    cmd, args.timeout, log_dir / f"{entry['job_name']}.log",
                    progress, started)
                if record["status"] != "exit-ok":
                    infra_failures.append((entry["job_name"], record["status"]))
            record["finished_at"] = _utc_now()
            with open(progress, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            done += 1
    if infra_failures:
        print(f"[run] {len(infra_failures)} job(s) ended in infra failure "
              f"(nonzero/timeout): {[j for j, _ in infra_failures]}; "
              "qualification of this run is not possible")
        return 4
    print(f"[run] {done} jobs issued; qualify with 'qualify'")
    return 0


# -- W03: measured toolchain binding for production dispatch -------------------


def _venv_harbor_config_path(harbor_python: str) -> str:
    """MEASURE the harbor package file backing the pinned interpreter's
    JobConfig schema (real import; nothing declared by the operator)."""
    proc = subprocess.run(
        [str(harbor_python), "-c",
         "import harbor.models.job.config as m; print(m.__file__)"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    path = proc.stdout.strip()
    if proc.returncode != 0 or not path:
        raise SystemExit(
            "cannot measure the harbor package behind --harbor-python: "
            f"{(proc.stderr or proc.stdout).strip()[:160]}"
        )
    return path


def verify_toolchain_binding(
    harbor_command: str, harbor_python: str, harbor_source: str,
    expected_head: str,
) -> dict:
    """Bind the ACTUAL dispatch to the measured pinned toolchain.

    The executed --harbor-command must invoke the pinned venv's REAL
    console entrypoint (``<venv>/bin/harbor``, whose shebang is the
    measured interpreter), the venv's harbor package must be
    content-identical to the pinned checkout at the JobConfig schema,
    and the checkout HEAD must equal the protocol pin. An unrelated
    executable (or a different framework binary) is rejected BEFORE
    production dispatch even when the correct --harbor-python/--harbor-
    source are supplied for measurement."""
    measured_head = measure_harbor_head(harbor_source)
    if measured_head != expected_head:
        raise SystemExit(
            f"measured harbor HEAD {measured_head} != protocol pin "
            f"{expected_head}; refusing to dispatch another framework version"
        )
    # The venv python is typically a SYMLINK to the base interpreter:
    # the console entrypoint lives next to the VENV path (unresolved),
    # and its shebang names the venv python path itself.
    python_path = Path(harbor_python).absolute()
    entrypoint = python_path.parent / "harbor"
    if not entrypoint.is_file():
        raise SystemExit(
            f"pinned harbor console entrypoint missing: {entrypoint} "
            "(production dispatch must invoke the pinned venv's harbor CLI)"
        )
    shebang = entrypoint.read_text(errors="replace").splitlines()[0] if entrypoint else ""
    if str(python_path) not in shebang:
        raise SystemExit(
            f"harbor entrypoint {entrypoint} is not backed by the measured "
            f"interpreter {python_path} (shebang: {shebang.strip()!r})"
        )
    if str(entrypoint) not in harbor_command:
        raise SystemExit(
            f"--harbor-command does not invoke the pinned harbor entrypoint "
            f"{entrypoint}; production dispatch must run the pinned venv CLI "
            "(fixture-only commands require --allow-fixture-command and can "
            "never qualify for the live gate)"
        )
    venv_cfg = Path(_venv_harbor_config_path(harbor_python))
    src_cfg = Path(harbor_source) / "src/harbor/models/job/config.py"
    if not src_cfg.is_file() or _sha256_file(venv_cfg) != _sha256_file(src_cfg):
        raise SystemExit(
            "pinned interpreter's harbor package is not content-identical to "
            f"the pinned checkout at {src_cfg}; refusing unbound dispatch"
        )
    return {
        "harbor_entrypoint": str(entrypoint),
        "harbor_head": measured_head,
        "jobconfig_sha256": _sha256_file(src_cfg),
        "binding": "pinned",
    }


# -- qualification ------------------------------------------------------------


def _unqualified(reason: str) -> dict:
    return {"qualified": False, "reason": reason}


def _parse_identity_receipt(text: str) -> dict:
    """Parse the published adapter's measured runtime-identity receipt
    (source: agent/runtime-identity.txt from the accepted controls):
    one sha256 line per preloaded skill, the GLM_BASE_URL line, then
    the live-queried git HEAD line."""
    out: dict[str, Any] = {"skills": {}, "base_url": None, "head": None}
    for line in text.splitlines():
        m = _SHA_LINE_RE.match(line.strip())
        if m:
            out["skills"][m.group(2)] = m.group(1)
            continue
        if line.startswith("GLM_BASE_URL="):
            out["base_url"] = line.split("=", 1)[1]
            continue
        if _HEAD_RE.match(line.strip()):
            out["head"] = line.strip()
    return out


def _reward_of(trial: dict) -> tuple[Optional[float], str]:
    """Extract and range-check the verified reward. Returns
    (reward, reason); reason empty on success. Malformed / non-finite /
    out-of-range / missing reward never becomes a score."""
    if not trial.get("reward_valid", False):
        return None, "reward_valid is false"
    verdict = trial.get("verifier_result") or {}
    rewards = verdict.get("rewards") if isinstance(verdict, dict) else None
    reward = (rewards or {}).get("reward") if isinstance(rewards, dict) else None
    if isinstance(reward, bool) or not isinstance(reward, (int, float)):
        return None, f"malformed reward {reward!r}"
    reward = float(reward)
    if math.isnan(reward) or math.isinf(reward):
        return None, "non-finite reward"
    if not 0.0 <= reward <= 1.0:
        return None, f"reward {reward} outside [0,1]"
    return reward, ""


def qualify_trial(trial_dir: Path, expected: dict, planned: dict) -> dict:
    """Qualify one trial directory against the arm's expected identity
    AND the planned task binding (task_name / trial_name prefix /
    task_id path / frozen canonical task_checksum). Genuine verified
    failures stay measured outcomes; anything wrong with the
    MEASUREMENT is unqualified."""
    result_path = trial_dir / "result.json"
    if not result_path.is_file():
        return _unqualified("missing trial result.json")
    try:
        trial = json.loads(result_path.read_text())
    except ValueError as exc:
        return _unqualified(f"malformed trial result.json: {exc}")
    if trial.get("exception_info"):
        return _unqualified(
            f"trial exception (infra/timeout, not a product outcome): "
            f"{str(trial['exception_info'])[:160]}"
        )
    # W02 finding 1: the result must be THIS planned task's result
    if trial.get("task_name") != planned["task_id"]:
        return _unqualified(
            f"result task_name {trial.get('task_name')!r} != planned task "
            f"{planned['task_id']!r} (renamed or substituted trial)"
        )
    trial_name = trial.get("trial_name") or ""
    if not trial_name.startswith(f"{planned['task_id']}__"):
        return _unqualified(
            f"trial_name {trial_name!r} is not a trial of planned task "
            f"{planned['task_id']!r}"
        )
    planned_path = f"{expected['task_root']}/{planned['task_id']}"
    if (trial.get("task_id") or {}).get("path") != planned_path:
        return _unqualified(
            f"result task path {(trial.get('task_id') or {}).get('path')!r} != "
            f"planned {planned_path!r}"
        )
    reward, reason = _reward_of(trial)
    if reason:
        return _unqualified(reason)
    info = trial.get("agent_info") or {}
    model_info = info.get("model_info") or {}
    if model_info.get("name") != expected["model"] or model_info.get("provider") != expected["provider"]:
        return _unqualified(f"agent/model identity drift: {model_info!r}")
    checksum = trial.get("task_checksum")
    # W02 finding 1: checksum bound to the FROZEN canonical value, not
    # merely equal across arms -- the same wrong checksum on both arms
    # no longer qualifies
    if not isinstance(checksum, str) or not checksum:
        return _unqualified("missing task_checksum (task asset binding lost)")
    if checksum != planned["expected_task_checksum"]:
        return _unqualified(
            f"task_checksum {checksum[:16]}... != frozen canonical "
            f"{planned['expected_task_checksum'][:16]}... for task "
            f"{planned['task_id']!r} (wrong or drifted task assets)"
        )
    receipt = trial_dir / "agent" / "runtime-identity.txt"
    if not receipt.is_file():
        return _unqualified("missing measured runtime-identity receipt")
    ident = _parse_identity_receipt(receipt.read_text())
    if ident.get("head") != expected["hermes_core_commit"]:
        return _unqualified(
            f"observed core HEAD {ident.get('head')} != expected {expected['hermes_core_commit']}"
        )
    skill_sha = (ident.get("skills") or {}).get(expected["skill_name"])
    if skill_sha != expected["skill_file_sha256"]:
        return _unqualified(
            f"preloaded skill file sha drift: observed {skill_sha} != "
            f"expected {expected['skill_file_sha256']}"
        )
    if ident.get("base_url") != expected["base_url"]:
        return _unqualified(
            f"model endpoint drift: {ident.get('base_url')!r} != expected {expected['base_url']!r}"
        )
    return {"qualified": True, "reward": reward, "task_checksum": checksum,
            "task_name": trial.get("task_name"), "trial_name": trial_name}


def _collect_arm(arm_dir: Path, plan_arm: dict, task_ids: list[str],
                 expected: dict, run_state: dict, executed_jobs: set[str]) -> dict:
    """Collect and qualify one arm's 20 trials at the REAL Harbor
    nesting (<arm>/<job_name>/<task>__<suffix>/). Fail closed on
    missing, duplicate, swapped, extra, stale, orphan or partial sets."""
    trials: dict[str, dict] = {}
    problems: list[str] = []
    if not arm_dir.is_dir():
        return {"trials": trials, "problems": [f"arm directory {arm_dir} missing"]}
    planned_jobs = {e["job_name"]: e for e in plan_arm["jobs"]}
    for job in sorted(arm_dir.glob("*.config.json")):
        cfg_bytes_sha = _sha256_file(job)
        stem = job.name[: -len(".config.json")] if job.name.endswith(".config.json") else job.stem
        entry = planned_jobs.get(stem)
        if entry is None:
            problems.append(f"config {job.name} is not in the reviewed plan")
            continue
        # W02 finding 2: config bytes re-verified against the plan
        if cfg_bytes_sha != entry["config_sha256"]:
            problems.append(f"config {job.name} drifted from the reviewed plan")
            continue
        job_dir = arm_dir / entry["job_name"]
        if not job_dir.is_dir():
            problems.append(f"no job directory {job_dir.name} (never ran)")
            continue
        job_result = job_dir / "result.json"
        if not job_result.is_file():
            problems.append(f"job {entry['job_name']} has no job-level result.json")
            continue
        try:
            job_summary = json.loads(job_result.read_text())
        except ValueError:
            problems.append(f"job {entry['job_name']} result.json malformed")
            continue
        if job_summary.get("n_total_trials") != 1:
            problems.append(
                f"job {entry['job_name']} n_total_trials="
                f"{job_summary.get('n_total_trials')} (orphan/extra attempts)"
            )
            continue
        # W02 finding 2: the trial must belong to the bound run (not an
        # old same-shape result from before the run started)
        if str(job_summary.get("started_at", "")) < run_state["started_at"]:
            problems.append(
                f"job {entry['job_name']} started {job_summary.get('started_at')} "
                f"pre-dates the bound run {run_state['started_at']} (stale trial)"
            )
            continue
        if entry["job_name"] not in executed_jobs:
            problems.append(
                f"job {entry['job_name']} has no executed-attempt receipt in "
                "the bound run (dry-run or foreign progress)"
            )
            continue
        trial_dirs = sorted(
            d for d in job_dir.iterdir()
            if d.is_dir() and d.name.startswith(f"{entry['task_id']}__")
        )
        if not trial_dirs:
            problems.append(f"no trial directory for task {entry['task_id']!r}")
            continue
        if len(trial_dirs) > 1:
            problems.append(f"multiple trial dirs for task {entry['task_id']!r}")
            continue
        verdict = qualify_trial(trial_dirs[0], expected, entry)
        verdict["job_name"] = entry["job_name"]
        if entry["task_id"] in trials:
            problems.append(f"duplicate qualified entry for task {entry['task_id']!r}")
            continue
        trials[entry["task_id"]] = verdict
    for task_id in task_ids:
        if task_id not in trials:
            problems.append(f"missing task {task_id!r} in arm")
    extra = sorted(set(trials) - set(task_ids))
    if extra:
        problems.append(f"tasks outside the frozen selection: {extra}")
    # W02 finding 2: orphan job directories beyond the reviewed plan
    for d in sorted(arm_dir.iterdir()):
        if d.is_dir() and d.name not in planned_jobs:
            problems.append(f"orphan job directory {d.name} (extra attempt)")
    return {"trials": trials, "problems": problems}


def evaluate_policy(policy: dict, rows: list[dict]) -> dict:
    """PREDECLARED frozen20-paired-no-regression v1 (hash-bound in the
    protocol BEFORE outcomes): all 40 trials qualified, no per-task
    regression (candidate reward >= baseline reward on every task), and
    mean candidate >= mean baseline. Everything else fails the gate.
    No discretionary judgment is consulted."""
    if policy.get("name") != POLICY_NAME or policy.get("version") != POLICY_VERSION:
        return {"pass": False, "reason": "policy not the declared v1 rule"}
    regressions = [r["task_id"] for r in rows if r["delta"] < 0]
    mean_base = sum(r["baseline_reward"] for r in rows) / len(rows)
    mean_cand = sum(r["candidate_reward"] for r in rows) / len(rows)
    reasons = []
    if regressions:
        reasons.append(f"per-task regressions on {regressions}")
    if mean_cand < mean_base:
        reasons.append(f"mean regression {mean_cand:.4f} < {mean_base:.4f}")
    return {"pass": not reasons, "reasons": reasons,
            "mean_baseline": mean_base, "mean_candidate": mean_cand,
            "regressed_tasks": regressions}


def cmd_qualify(args: argparse.Namespace) -> int:
    protocol = PairedProtocol.load(Path(args.protocol))
    task_ids = load_frozen_selection(protocol)
    jobs_root = Path(args.jobs_root)
    # W02 finding 2: qualification is bound to the reviewed plan AND the
    # run-state receipt of the run being qualified
    plan_path = jobs_root / "plan.json"
    if not plan_path.is_file():
        raise SystemExit(f"no plan.json under {jobs_root}; nothing reviewed to qualify against")
    plan = json.loads(plan_path.read_text())
    if plan.get("protocol_sha256") != protocol.sha256:
        raise SystemExit("plan was built against a different protocol; replan")
    run_state_path = jobs_root / "run-state.json"
    if not run_state_path.is_file():
        raise SystemExit(
            "no run-state.json: a dry run or unbound tree cannot be qualified; "
            "execute the reviewed plan through 'run' first"
        )
    run_state = json.loads(run_state_path.read_text())
    if run_state.get("protocol_sha256") != protocol.sha256 or \
            run_state.get("plan_sha256") != _sha256_file(plan_path):
        raise SystemExit("run-state does not bind this protocol+plan; fail closed")
    if run_state.get("measured_harbor_head") != protocol.body["harbor_commit"]:
        raise SystemExit("run-state measured harbor HEAD drift; fail closed")
    if run_state.get("measured_python_version") != protocol.body["python_version"]:
        raise SystemExit("run-state measured python version drift; fail closed")
    binding = (run_state.get("toolchain_binding") or {}).get("binding")
    if binding != "pinned":
        raise SystemExit(
            "run-state toolchain binding is "
            f"{binding!r}; fixture-only/unbound runs can NEVER qualify for "
            "the live gate"
        )
    progress = _load_progress(jobs_root / "progress.jsonl")
    executed = {
        r["job"] for r in progress
        if r.get("run_id") == run_state["run_id"]
        and str(r.get("status", "")).startswith("exit-")
    }
    expected_base = dict(protocol.identity("baseline"),
                         task_root=protocol.body["task_root"])
    expected_cand = dict(protocol.identity("candidate"),
                         task_root=protocol.body["task_root"])
    arms: dict[str, dict] = {}
    fatal: list[str] = []
    for arm, expected in (("baseline", expected_base), ("candidate", expected_cand)):
        collected = _collect_arm(jobs_root / arm, plan["arms"][arm], task_ids,
                                 expected, run_state, executed)
        unqualified = {t: v["reason"] for t, v in collected["trials"].items()
                       if not v.get("qualified")}
        if collected["problems"] or unqualified:
            fatal.append(f"{arm}: {collected['problems']} unqualified={unqualified}")
        arms[arm] = collected
    if fatal:
        report = {
            "verdict": "UNQUALIFIED", "created_at": _utc_now(),
            "protocol_sha256": protocol.sha256, "fatal": fatal,
            "note": "missing/failed measurements are UNQUALIFIED, never 0 or PASS",
        }
        _write_report(jobs_root, report)
        print("[qualify] UNQUALIFIED; see report")
        return 3
    rows = []
    for task_id in task_ids:
        b = arms["baseline"]["trials"][task_id]
        c = arms["candidate"]["trials"][task_id]
        rows.append({
            "task_id": task_id,
            "baseline_reward": b["reward"], "candidate_reward": c["reward"],
            "delta": c["reward"] - b["reward"],
            "task_checksum": b["task_checksum"],
        })
    policy = evaluate_policy(protocol.body["policy"], rows)
    report = {
        "verdict": "PASS" if policy["pass"] else "FAIL",
        "created_at": _utc_now(), "protocol_sha256": protocol.sha256,
        "run_id": run_state["run_id"],
        "policy": {"name": POLICY_NAME, "version": POLICY_VERSION,
                   "sha256": _sha256_text(json.dumps(protocol.body["policy"], sort_keys=True))},
        "paired_rows": rows,
        "n_pairs": len(rows),
        "n_candidate_wins": sum(1 for r in rows if r["delta"] > 0),
        "n_regressions": len(policy["regressed_tasks"]),
        "policy_evaluation": policy,
        "identity": {arm: protocol.identity(arm) for arm in ARMS},
    }
    _write_report(jobs_root, report)
    print(f"[qualify] {report['verdict']} "
          f"(regressions={report['n_regressions']}/{len(rows)}); report written")
    return 0 if report["verdict"] == "PASS" else 1


def _write_report(jobs_root: Path, report: dict) -> Path:
    path = jobs_root / "paired-report.json"
    path.write_text(json.dumps(report, indent=2))
    return path


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="build 2x20 job configs from the frozen selection")
    p.add_argument("--protocol", required=True)
    p.add_argument("--jobs-root", required=True)
    p.add_argument("--label", default="tblite-paired")
    p.add_argument("--checksum-python", required=True,
                   help="interpreter of the pinned Harbor env used to measure "
                        "canonical task checksums through the primary Task API")
    p.add_argument("--harbor-source", required=True,
                   help="pinned Harbor source checkout (read-only HEAD + import path)")
    p.set_defaults(func=cmd_plan)
    p = sub.add_parser("run", help="execute the plan sequentially (explicit harbor command)")
    p.add_argument("--protocol", required=True)
    p.add_argument("--jobs-root", required=True)
    p.add_argument("--harbor-command", default=None)
    p.add_argument("--harbor-python", required=True,
                   help="interpreter the harbor command actually runs under "
                        "(its version is MEASURED and bound)")
    p.add_argument("--stop-after", type=int, default=0)
    p.add_argument("--timeout", type=int, default=0)
    p.add_argument("--allow-fixture-command", action="store_true",
                   help="NON-PRODUCTION: allow an unbound fixture command; the "
                        "run is stamped fixture-only and can never qualify")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("qualify", help="qualify a completed jobs tree and evaluate the predeclared policy")
    p.add_argument("--protocol", required=True)
    p.add_argument("--jobs-root", required=True)
    p.set_defaults(func=cmd_qualify)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
