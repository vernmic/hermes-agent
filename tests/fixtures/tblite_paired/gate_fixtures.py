"""Shared fixture builder for the paired-gate offline tests (W02).

Builds temporary Harbor-500af913-shaped BOUND job trees in the REAL
production nesting:

    jobs/<arm>/<job_name>.config.json          (bytes hash-bound in plan)
    jobs/<arm>/<job_name>/result.json          (job summary, n_total_trials)
    jobs/<arm>/<job_name>/<task>__<suffix>/    (trial dir: result.json +
                                                agent/runtime-identity.txt)

plus the reviewed plan.json (per-task config sha256 + canonical
task_checksum) and the runner's run-state.json + progress.jsonl. The
schema is derived from the ACTUAL preflight production receipts
(hermes-gated-control-w03) with generic task ids -- sanitized public
fixtures, explicitly NOT evidence of real trials. Fixture checksums are
deterministic synthetic digests measured through the plan plumbing;
canonical checksums are measured through the real pinned Harbor Task
API in the recorded receipt runs (see the W02 packet).
"""

import hashlib
import json
from pathlib import Path

CORE = "93faf1387ce4bb4769de506d47a3c82afb9d0326"
BASE_URL = "https://api.z.ai/api/coding/paas/v4"
MODEL = "glm-5.3"
PROVIDER = "zai"
SKILL = "test-driven-development"
BASELINE_SHA = "b" * 64
CANDIDATE_SHA = "c" * 64
RUN_STARTED = "2026-01-01T00:00:00+00:00"
JOB_STARTED = "2026-01-02T00:00:00+00:00"
STALE_STARTED = "2025-01-01T00:00:00+00:00"
TASK_ROOT = "repos/OpenThoughts-TBLite"


def fixture_checksum(task_id: str) -> str:
    """Deterministic fixture-grade digest (NOT the canonical Harbor
    checksum; the canonical one is measured through the pinned Task API
    by the real plan path and recorded in receipts)."""
    return hashlib.sha256(("fixture-assets:" + task_id).encode()).hexdigest()


def frozen_selection_file(path: Path, task_ids=None,
                          dataset_commit="5c37b41f00ce04719a4453061076ae9f46b74b7d") -> Path:
    task_ids = task_ids or [f"fixture-task-{i:02d}" for i in range(1, 21)]
    sel = {
        "dataset_commit": dataset_commit,
        "pools": {
            "fixture-pool": {
                "allocated": len(task_ids), "eligible_count": len(task_ids),
                "hash_order": task_ids, "selected": task_ids,
                "selected_count": len(task_ids),
            }
        },
    }
    path.write_text(json.dumps(sel, indent=2))
    return path


def protocol_file(path: Path, selection_path: Path, *,
                  task_root=TASK_ROOT, harbor_commit="500af91374dcc43b59fac54cf90295646617b14d",
                  python_version="3.14.7", dataset_commit="5c37b41f00ce04719a4453061076ae9f46b74b7d") -> Path:
    body = {
        "dataset_commit": dataset_commit,
        "harbor_commit": harbor_commit,
        "hermes_core_commit": CORE,
        "model": MODEL, "provider": PROVIDER, "base_url": BASE_URL,
        "agent_module": "evals.tblite_preflight.pinned_hermes_agent:PinnedHermes",
        "python_version": python_version,
        "task_root": task_root,
        "frozen_selection": {
            "path": str(selection_path),
            "sha256": hashlib.sha256(selection_path.read_bytes()).hexdigest(),
        },
        "baseline_skill": {"path": "/skills/baseline", "sha256": BASELINE_SHA,
                           "preload_name": SKILL},
        "candidate_skill": {"path": "/skills/candidate", "sha256": CANDIDATE_SHA,
                            "preload_name": SKILL},
        "policy": {"name": "frozen20-paired-no-regression", "version": 1,
                   "declared_before_outcomes": True},
    }
    path.write_text(json.dumps(body, indent=2))
    return path


def _job_name(arm: str, task_id: str) -> str:
    return f"fixture-{arm}-{task_id}"


def write_plan(jobs_root: Path, task_ids: list[str], *,
               checksums=None, harbor_source="/tmp/fixture-harbor-src") -> dict:
    """Reviewed synthetic plan in the exact shape cmd_plan writes
    (per-task config bytes + frozen canonical checksum bindings)."""
    checksums = checksums or {}
    plan = {
        "created_at": RUN_STARTED,
        "protocol_sha256": "bound-by-test",
        "measured_dataset_commit": "5c37b41f00ce04719a4453061076ae9f46b74b7d",
        "checksum_python": sys_python(),
        "harbor_source": str(harbor_source),
        "arms": {},
    }
    for arm in ("baseline", "candidate"):
        arm_dir = jobs_root / arm
        arm_dir.mkdir(parents=True, exist_ok=True)
        entries = []
        for task_id in task_ids:
            job_name = _job_name(arm, task_id)
            cfg = {
                "job_name": job_name, "jobs_dir": str(arm_dir),
                "agent_timeout_multiplier": 1.0,
                "agent_setup_timeout_multiplier": 1.5,
                "n_concurrent_trials": 1,
                "environment": {"type": "docker", "override_cpus": 2,
                                "override_memory_mb": 4096},
                "agents": [{
                    "name": "evals.tblite_preflight.pinned_hermes_agent:PinnedHermes",
                    "model_name": f"{PROVIDER}/{MODEL}", "base_url": BASE_URL,
                    "skills": ["/skills/" + arm],
                }],
                "tasks": [{"path": f"{TASK_ROOT}/{task_id}"}],
            }
            cfg_path = arm_dir / f"{job_name}.config.json"
            cfg_path.write_text(json.dumps(cfg, indent=2))
            entries.append({
                "task_id": task_id, "config": str(cfg_path),
                "config_sha256": hashlib.sha256(cfg_path.read_bytes()).hexdigest(),
                "job_name": job_name,
                "expected_task_checksum": checksums.get(task_id, fixture_checksum(task_id)),
            })
        plan["arms"][arm] = {"jobs": entries}
    (jobs_root / "plan.json").write_text(json.dumps(plan, indent=2))
    return plan


def write_run_state(jobs_root: Path, plan_sha: str, *, run_id="fixrun0001",
                    started_at=RUN_STARTED, harbor_head=None,
                    python_version=None) -> dict:
    state = {
        "run_id": run_id, "started_at": started_at,
        "protocol_sha256": "bound-by-test", "plan_sha256": plan_sha,
        "measured_harbor_head": harbor_head or "500af91374dcc43b59fac54cf90295646617b14d",
        "measured_python_version": python_version or "3.14.7",
        # W03: qualified runs must be toolchain-pinned (fixture trees that
        # mean to qualify simulate a pinned binding explicitly)
        "toolchain_binding": {"binding": "pinned"},
    }
    (jobs_root / "run-state.json").write_text(json.dumps(state, indent=2))
    return state


def write_progress(jobs_root: Path, task_ids: list[str], *, run_id="fixrun0001",
                   statuses=None) -> list[dict]:
    """Executed-attempt receipts: one terminal record per planned job."""
    statuses = statuses or {}
    records = []
    for arm in ("baseline", "candidate"):
        for task_id in task_ids:
            job = _job_name(arm, task_id)
            records.append({"run_id": run_id, "arm": arm, "task_id": task_id,
                            "job": job, "status": statuses.get(job, "exit-ok"),
                            "started_at": JOB_STARTED, "finished_at": JOB_STARTED})
    path = jobs_root / "progress.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return records


def write_trial(jobs_root: Path, arm: str, task_id: str, *, reward=1.0,
                skill_sha=None, head=CORE, base_url=BASE_URL, model=MODEL,
                provider=PROVIDER, exception=None, reward_valid=True,
                verifier_rewards=None, checksum=None, suffix="fixpl1",
                omit_receipt=False, malformed_result=False,
                task_name=None, trial_name=None, task_path=None,
                job_started=JOB_STARTED, n_total_trials=1) -> Path:
    """Write one production-nested trial: <arm>/<job_name>/<task>__<sfx>/
    with the job-level summary beside it."""
    sha = skill_sha if skill_sha is not None else (
        BASELINE_SHA if arm == "baseline" else CANDIDATE_SHA)
    arm_dir = jobs_root / arm
    job_name = _job_name(arm, task_id)
    job_dir = arm_dir / job_name
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "result.json").write_text(json.dumps({
        "id": f"job-{job_name}", "started_at": job_started,
        "finished_at": job_started, "n_total_trials": n_total_trials,
        "stats": {"n_completed_trials": n_total_trials},
    }))
    trial_dir = job_dir / f"{task_id}__{suffix}"
    (trial_dir / "agent").mkdir(parents=True, exist_ok=True)
    if not omit_receipt:
        (trial_dir / "agent" / "runtime-identity.txt").write_text(
            f"{sha}  /tmp/hermes/skills/{SKILL}/SKILL.md\n"
            f"GLM_BASE_URL={base_url}\n"
            f"{head}\n"
        )
    if malformed_result:
        (trial_dir / "result.json").write_text("{ not json")
        return trial_dir
    rewards = verifier_rewards if verifier_rewards is not None else {"reward": reward}
    (trial_dir / "result.json").write_text(json.dumps({
        "task_name": task_name if task_name is not None else task_id,
        "trial_name": trial_name if trial_name is not None else f"{task_id}__{suffix}",
        "task_id": {"path": task_path if task_path is not None
                    else f"{TASK_ROOT}/{task_id}"},
        "task_checksum": checksum if checksum is not None else fixture_checksum(task_id),
        "agent_info": {"name": "hermes",
                       "model_info": {"name": model, "provider": provider}},
        "verifier_result": {"rewards": rewards},
        "exception_info": exception,
        "reward_valid": reward_valid,
    }))
    return trial_dir


def build_complete_tree(jobs_root: Path, *, candidate_rewards=None) -> Path:
    """20x2 complete qualified BOUND tree (plan + run-state + progress +
    real nesting); candidate rewards default 1.0."""
    jobs_root = Path(jobs_root)
    jobs_root.mkdir(parents=True, exist_ok=True)
    sel = frozen_selection_file(jobs_root.parent / "selection.json")
    proto = protocol_file(jobs_root.parent / "protocol.json", sel)
    task_ids = json.loads(sel.read_text())["pools"]["fixture-pool"]["selected"]
    write_plan(jobs_root, task_ids)
    plan_sha = hashlib.sha256((jobs_root / "plan.json").read_bytes()).hexdigest()
    write_run_state(jobs_root, plan_sha)
    write_progress(jobs_root, task_ids)
    for i, task_id in enumerate(task_ids):
        write_trial(jobs_root, "baseline", task_id, reward=1.0)
        reward = 1.0 if candidate_rewards is None else candidate_rewards[i]
        write_trial(jobs_root, "candidate", task_id, reward=reward)
    # bind the plan to the actual protocol file for qualification
    body = json.loads((jobs_root / "plan.json").read_text())
    body["protocol_sha256"] = hashlib.sha256(proto.read_bytes()).hexdigest()
    (jobs_root / "plan.json").write_text(json.dumps(body, indent=2))
    state = json.loads((jobs_root / "run-state.json").read_text())
    state["plan_sha256"] = hashlib.sha256(
        (jobs_root / "plan.json").read_bytes()).hexdigest()
    state["protocol_sha256"] = body["protocol_sha256"]
    (jobs_root / "run-state.json").write_text(json.dumps(state, indent=2))
    return jobs_root


def trial_path(jobs_root: Path, arm: str, task_id: str, suffix="fixpl1") -> Path:
    return Path(jobs_root) / arm / _job_name(arm, task_id) / f"{task_id}__{suffix}"


def sys_python() -> str:
    import sys
    return sys.executable
