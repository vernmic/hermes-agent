"""Offline tests for the TBLite paired-20 regression gate (W02).

Fixtures are sanitized generic job trees in the REAL Harbor-500af913
production nesting (derived from the accepted preflight receipts); they
are plumbing proofs only and NEVER evidence of actual 40-trial
performance. Fixture task checksums are deterministic synthetic digests
through the plan plumbing; canonical checksums and the pinned JobConfig
schema are verified through the REAL installed Harbor source in the
recorded receipt runs (see W02 packet evidence). No network, Docker,
model or provider is touched.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent.parent / "fixtures" / "tblite_paired"
spec = importlib.util.spec_from_file_location(
    "gate_fixtures", FIXTURES / "gate_fixtures.py")
gfx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gfx)

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from evals.tblite_paired_gate.paired_gate import (
    POLICY_NAME, PairedProtocol, cmd_plan, cmd_qualify, cmd_run,
    evaluate_policy, load_frozen_selection, measure_harbor_head,
    measure_python_version,
)


def setup_protocol(tmp_path, **kw):
    sel = gfx.frozen_selection_file(tmp_path / "selection.json")
    proto = gfx.protocol_file(tmp_path / "protocol.json", sel, **kw)
    return proto


def run_qualify(tmp_path):
    proto = setup_protocol(tmp_path)
    rc = cmd_qualify(type("A", (), {
        "protocol": str(proto), "jobs_root": str(tmp_path / "jobs")})())
    report = json.loads((tmp_path / "jobs" / "paired-report.json").read_text())
    return rc, report


def first_task(tmp_path) -> str:
    sel = json.loads((tmp_path / "selection.json").read_text())
    return sel["pools"]["fixture-pool"]["selected"][0]


# -- fixture-grade pinned-tool standins (receipts cover the real ones) ---------


def fake_dataset_repo(tmp_path) -> tuple[str, str]:
    """Real git repo standing in for the dataset checkout; returns
    (path, measured HEAD). Fixture plumbing only."""
    repo = tmp_path / "dataset-repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / ".gitkeep").write_text("")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=f", "-c", "user.email=f@f",
         "commit", "-qm", "fixture dataset"], check=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    return str(repo), head


def fake_harbor_source(tmp_path) -> str:
    """Deterministic stub harbor package standing in for the pinned
    500af913 source: the checksum snippet imports
    harbor.models.task.task.Task through this path. The canonical
    checksums through the REAL pinned source are recorded in receipts."""
    src = tmp_path / "harbor-src"
    pkg = src / "harbor" / "models" / "task"
    pkg.mkdir(parents=True)
    for d in (src / "harbor", src / "harbor" / "models", pkg):
        (d / "__init__.py").write_text("")
    (pkg / "task.py").write_text(
        "import hashlib\n\n\nclass Task:\n"
        "    def __init__(self, task_dir):\n"
        "        self.task_dir = str(task_dir)\n\n"
        "    @property\n"
        "    def checksum(self):\n"
        "        return hashlib.sha256(\n"
        "            ('stub-assets:' + self.task_dir).encode()).hexdigest()\n"
    )
    return str(src)


def plan_args(tmp_path, proto, jobs_root=None):
    return type("A", (), {
        "protocol": str(proto), "jobs_root": str(jobs_root or tmp_path / "jobs"),
        "label": "fixture",
        "checksum_python": sys.executable,
        "harbor_source": fake_harbor_source(tmp_path),
    })()


# -- complete 20x2 ------------------------------------------------------------


def test_complete_paired_tree_passes(tmp_path):
    gfx.build_complete_tree(tmp_path / "jobs")
    rc, report = run_qualify(tmp_path)
    assert rc == 0 and report["verdict"] == "PASS"
    assert report["n_pairs"] == 20
    assert len(report["paired_rows"]) == 20
    assert all(r["delta"] == 0.0 for r in report["paired_rows"])
    assert report["policy_evaluation"]["mean_candidate"] == 1.0
    assert report["policy"]["sha256"]  # predeclared policy hash-bound
    assert report["run_id"]  # W02: bound run identity


def test_measured_candidate_regression_fails_policy_not_unqualified(tmp_path):
    rewards = [1.0] * 19 + [0.0]  # genuine verified failure on one task
    gfx.build_complete_tree(tmp_path / "jobs", candidate_rewards=rewards)
    rc, report = run_qualify(tmp_path)
    assert rc == 1 and report["verdict"] == "FAIL"  # measured outcome, qualified set
    assert report["n_regressions"] == 1
    reg = [r for r in report["paired_rows"] if r["delta"] < 0]
    assert len(reg) == 1 and reg[0]["candidate_reward"] == 0.0


# -- set integrity: missing / duplicate / swapped / extra / orphan --------------


def test_missing_task_never_qualifies(tmp_path):
    import shutil

    gfx.build_complete_tree(tmp_path / "jobs")
    task = first_task(tmp_path)
    shutil.rmtree(gfx.trial_path(tmp_path / "jobs", "candidate", task).parent)
    (tmp_path / "jobs" / "candidate" / f"fixture-candidate-{task}.config.json").unlink()
    rc, report = run_qualify(tmp_path)
    assert rc == 3 and report["verdict"] == "UNQUALIFIED"


def test_swapped_task_checksums_never_qualify(tmp_path):
    gfx.build_complete_tree(tmp_path / "jobs")
    task = first_task(tmp_path)
    d1 = gfx.trial_path(tmp_path / "jobs", "candidate", task)
    other = json.loads((tmp_path / "selection.json").read_text())[
        "pools"]["fixture-pool"]["selected"][1]
    d2 = gfx.trial_path(tmp_path / "jobs", "candidate", other)
    r1 = json.loads((d1 / "result.json").read_text())
    r2 = json.loads((d2 / "result.json").read_text())
    # swap result identities wholesale: rename-masking attack (W02 f1)
    r1["task_name"], r2["task_name"] = r2["task_name"], r1["task_name"]
    r1["trial_name"], r2["trial_name"] = r2["trial_name"], r1["trial_name"]
    r1["task_checksum"], r2["task_checksum"] = r2["task_checksum"], r1["task_checksum"]
    (d1 / "result.json").write_text(json.dumps(r1))
    (d2 / "result.json").write_text(json.dumps(r2))
    rc, report = run_qualify(tmp_path)
    assert rc == 3 and report["verdict"] == "UNQUALIFIED"
    assert any("planned task" in f or "frozen canonical" in f
               for f in report["fatal"])


def test_same_wrong_checksum_on_both_arms_never_qualifies(tmp_path):
    """W02 finding 1 core: identical WRONG asset checksums across both
    arms are rejected -- the checksum is bound to the frozen canonical
    value measured through the pinned Harbor Task API at plan time."""
    gfx.build_complete_tree(tmp_path / "jobs")
    task = first_task(tmp_path)
    wrong = "f" * 64
    for arm in ("baseline", "candidate"):
        trial = gfx.trial_path(tmp_path / "jobs", arm, task)
        r = json.loads((trial / "result.json").read_text())
        r["task_checksum"] = wrong
        (trial / "result.json").write_text(json.dumps(r))
    rc, report = run_qualify(tmp_path)
    assert rc == 3 and "frozen canonical" in json.dumps(report["fatal"])


def test_orphan_job_directory_never_qualifies(tmp_path):
    gfx.build_complete_tree(tmp_path / "jobs")
    (tmp_path / "jobs" / "candidate" / "fixture-candidate-EXTRA").mkdir()
    rc, report = run_qualify(tmp_path)
    assert rc == 3 and "orphan job directory" in json.dumps(report["fatal"])


def test_extra_trial_attempt_never_qualifies(tmp_path):
    gfx.build_complete_tree(tmp_path / "jobs")
    task = first_task(tmp_path)
    job_dir = gfx.trial_path(tmp_path / "jobs", "baseline", task).parent
    (job_dir / f"{task}__extra99").mkdir()
    rc, report = run_qualify(tmp_path)
    assert rc == 3 and "multiple trial dirs" in json.dumps(report["fatal"])


# -- identity drift ------------------------------------------------------------


def test_skill_sha_drift_never_qualifies(tmp_path):
    gfx.build_complete_tree(tmp_path / "jobs")
    trial = gfx.trial_path(tmp_path / "jobs", "candidate", first_task(tmp_path))
    (trial / "agent" / "runtime-identity.txt").write_text(
        f"{'0' * 64}  /tmp/hermes/skills/{gfx.SKILL}/SKILL.md\n"
        f"GLM_BASE_URL={gfx.BASE_URL}\n{gfx.CORE}\n"
    )
    rc, report = run_qualify(tmp_path)
    assert rc == 3 and "skill file sha drift" in json.dumps(report["fatal"])


def test_core_head_drift_between_arms_never_qualifies(tmp_path):
    gfx.build_complete_tree(tmp_path / "jobs")
    trial = gfx.trial_path(tmp_path / "jobs", "candidate", first_task(tmp_path))
    (trial / "agent" / "runtime-identity.txt").write_text(
        f"{gfx.CANDIDATE_SHA}  /tmp/hermes/skills/{gfx.SKILL}/SKILL.md\n"
        f"GLM_BASE_URL={gfx.BASE_URL}\n{'a' * 40}\n"
    )
    rc, report = run_qualify(tmp_path)
    assert rc == 3 and "core HEAD" in json.dumps(report["fatal"])


def test_general_endpoint_drift_never_qualifies(tmp_path):
    gfx.build_complete_tree(tmp_path / "jobs")
    trial = gfx.trial_path(tmp_path / "jobs", "candidate", first_task(tmp_path))
    (trial / "agent" / "runtime-identity.txt").write_text(
        f"{gfx.CANDIDATE_SHA}  /tmp/hermes/skills/{gfx.SKILL}/SKILL.md\n"
        "GLM_BASE_URL=https://api.z.ai/api/paas/v4\n"  # metered general endpoint
        f"{gfx.CORE}\n"
    )
    rc, report = run_qualify(tmp_path)
    assert rc == 3 and "endpoint drift" in json.dumps(report["fatal"])


def test_missing_identity_receipt_never_qualifies(tmp_path):
    gfx.build_complete_tree(tmp_path / "jobs")
    trial = gfx.trial_path(tmp_path / "jobs", "candidate", first_task(tmp_path))
    (trial / "agent" / "runtime-identity.txt").unlink()
    rc, report = run_qualify(tmp_path)
    assert rc == 3 and "runtime-identity receipt" in json.dumps(report["fatal"])


# -- measurement integrity -----------------------------------------------------


@pytest.mark.parametrize("rewards,match", [
    ({"reward": float("nan")}, "non-finite"),
    ({"reward": 1.5}, "outside"),
    ({"reward": "1.0"}, "malformed"),
    ({}, "malformed"),
])
def test_bad_rewards_never_qualify(tmp_path, rewards, match):
    gfx.build_complete_tree(tmp_path / "jobs")
    trial = gfx.trial_path(tmp_path / "jobs", "candidate", first_task(tmp_path))
    r = json.loads((trial / "result.json").read_text())
    r["verifier_result"]["rewards"] = rewards
    (trial / "result.json").write_text(json.dumps(r))
    rc, report = run_qualify(tmp_path)
    assert rc == 3 and match in json.dumps(report["fatal"])


def test_trial_exception_is_unqualified_never_zero(tmp_path):
    gfx.build_complete_tree(tmp_path / "jobs")
    trial = gfx.trial_path(tmp_path / "jobs", "candidate", first_task(tmp_path))
    r = json.loads((trial / "result.json").read_text())
    r["exception_info"] = {"type": "AgentTimeoutError", "message": "timed out"}
    (trial / "result.json").write_text(json.dumps(r))
    rc, report = run_qualify(tmp_path)
    assert rc == 3 and "exception" in json.dumps(report["fatal"])
    assert "never 0 or PASS" in report["note"]


def test_reward_valid_false_never_qualifies(tmp_path):
    gfx.build_complete_tree(tmp_path / "jobs")
    trial = gfx.trial_path(tmp_path / "jobs", "candidate", first_task(tmp_path))
    r = json.loads((trial / "result.json").read_text())
    r["reward_valid"] = False
    (trial / "result.json").write_text(json.dumps(r))
    rc, report = run_qualify(tmp_path)
    assert rc == 3 and "reward_valid" in json.dumps(report["fatal"])


def test_malformed_trial_result_never_qualifies(tmp_path):
    gfx.build_complete_tree(tmp_path / "jobs")
    trial = gfx.trial_path(tmp_path / "jobs", "candidate", first_task(tmp_path))
    (trial / "result.json").write_text("{ broken")
    rc, report = run_qualify(tmp_path)
    assert rc == 3 and "malformed" in json.dumps(report["fatal"])


# -- W02: plan / config / run-state binding ------------------------------------


def test_config_mutation_after_plan_never_qualifies(tmp_path):
    gfx.build_complete_tree(tmp_path / "jobs")
    task = first_task(tmp_path)
    cfg = tmp_path / "jobs" / "candidate" / f"fixture-candidate-{task}.config.json"
    body = json.loads(cfg.read_text())
    body["agent_timeout_multiplier"] = 0.01  # mutate the reviewed config
    cfg.write_text(json.dumps(body, indent=2))
    rc, report = run_qualify(tmp_path)
    assert rc == 3 and "drifted from the reviewed plan" in json.dumps(report["fatal"])


def test_stale_pre_run_trial_never_qualifies(tmp_path):
    """W02 finding 2: an old same-shape result from BEFORE the bound run
    cannot be qualified as current."""
    gfx.build_complete_tree(tmp_path / "jobs")
    task = first_task(tmp_path)
    job_dir = gfx.trial_path(tmp_path / "jobs", "baseline", task).parent
    summary = json.loads((job_dir / "result.json").read_text())
    summary["started_at"] = gfx.STALE_STARTED
    (job_dir / "result.json").write_text(json.dumps(summary))
    rc, report = run_qualify(tmp_path)
    assert rc == 3 and "stale trial" in json.dumps(report["fatal"])


def test_qualify_without_plan_refuses(tmp_path):
    proto = setup_protocol(tmp_path)
    (tmp_path / "jobs" / "baseline").mkdir(parents=True)
    (tmp_path / "jobs" / "candidate").mkdir(parents=True)
    with pytest.raises(SystemExit, match="no plan.json"):
        cmd_qualify(type("A", (), {
            "protocol": str(proto), "jobs_root": str(tmp_path / "jobs")})())


def test_qualify_without_run_state_refuses(tmp_path):
    """W02: a tree without a bound run receipt (e.g. dry-run only) is
    not qualifiable."""
    gfx.build_complete_tree(tmp_path / "jobs")
    (tmp_path / "jobs" / "run-state.json").unlink()
    with pytest.raises(SystemExit, match="run-state"):
        run_qualify(tmp_path)


def test_dry_run_progress_never_counts_as_attempt(tmp_path):
    gfx.build_complete_tree(tmp_path / "jobs")
    task = first_task(tmp_path)
    job = f"fixture-candidate-{task}"
    progress = tmp_path / "jobs" / "progress.jsonl"
    records = [json.loads(x) for x in progress.read_text().splitlines()]
    for r in records:
        if r["job"] == job:
            r["status"] = "dry-run"
    progress.write_text("".join(json.dumps(r) + "\n" for r in records))
    rc, report = run_qualify(tmp_path)
    assert rc == 3 and "no executed-attempt receipt" in json.dumps(report["fatal"])


def test_foreign_progress_run_id_never_qualifies(tmp_path):
    """Progress receipts from a DIFFERENT run cannot vouch for this
    tree's trials."""
    gfx.build_complete_tree(tmp_path / "jobs")
    progress = tmp_path / "jobs" / "progress.jsonl"
    records = [json.loads(x) for x in progress.read_text().splitlines()]
    for r in records:
        r["run_id"] = "someother run"
    progress.write_text("".join(json.dumps(r) + "\n" for r in records))
    rc, report = run_qualify(tmp_path)
    assert rc == 3 and "no executed-attempt receipt" in json.dumps(report["fatal"])


# -- stale protocol / selection binding ---------------------------------------


def test_stale_selection_fails_closed(tmp_path):
    sel = gfx.frozen_selection_file(tmp_path / "selection.json")
    proto = gfx.protocol_file(tmp_path / "protocol.json", sel)
    sel.write_text(sel.read_text().replace("fixture-task-01", "fixture-task-XX"))
    protocol = PairedProtocol.load(proto)
    with pytest.raises(SystemExit, match="sha256 mismatch"):
        load_frozen_selection(protocol)


def test_undeclared_policy_refused(tmp_path):
    sel = gfx.frozen_selection_file(tmp_path / "selection.json")
    proto = gfx.protocol_file(tmp_path / "protocol.json", sel)
    body = json.loads(proto.read_text())
    body["policy"] = {"name": "maybe-good-enough", "version": 2}
    proto.write_text(json.dumps(body))
    with pytest.raises(SystemExit, match="refusing"):
        PairedProtocol.load(proto)


def test_noop_empty_jobs_root_refused(tmp_path):
    """W02 corrected contract: with no reviewed plan there is nothing to
    qualify against -- a clear refusal, not an UNQUALIFIED report that
    implies a bound run was examined."""
    (tmp_path / "jobs" / "baseline").mkdir(parents=True)
    (tmp_path / "jobs" / "candidate").mkdir(parents=True)
    with pytest.raises(SystemExit, match="no plan.json"):
        run_qualify(tmp_path)


# -- plan / run plumbing (explicitly labelled plumbing) ------------------------


def plan_test_env(tmp_path):
    """Plan against a fixture dataset git repo + stub harbor source;
    fixture-grade plumbing, canonical receipts live in the packet."""
    repo, head = fake_dataset_repo(tmp_path)
    sel = gfx.frozen_selection_file(tmp_path / "selection.json",
                                    dataset_commit=head)
    proto = gfx.protocol_file(tmp_path / "protocol.json", sel,
                              task_root=repo, dataset_commit=head)
    task_ids = json.loads(sel.read_text())["pools"]["fixture-pool"]["selected"]
    for task_id in task_ids:
        (Path(repo) / task_id).mkdir(exist_ok=True)
        (Path(repo) / task_id / "task.yaml").write_text("fixture: " + task_id + "\n")
    return proto, task_ids


def test_plan_measures_dataset_and_freezes_canonical_checksums(tmp_path):
    proto, _task_ids = plan_test_env(tmp_path)
    rc = cmd_plan(plan_args(tmp_path, proto))
    assert rc == 0
    plan = json.loads((tmp_path / "jobs" / "plan.json").read_text())
    assert plan["measured_dataset_commit"] == json.loads(proto.read_text())["dataset_commit"]
    for arm in ("baseline", "candidate"):
        jobs = plan["arms"][arm]["jobs"]
        assert len(jobs) == 20
        names = [j["job_name"] for j in jobs]
        assert len(set(names)) == 20
        for j in jobs:
            cfg = json.loads(Path(j["config"]).read_text())
            assert cfg["n_concurrent_trials"] == 1
            assert cfg["environment"]["override_cpus"] == 2
            assert cfg["environment"]["override_memory_mb"] == 4096
            assert cfg["agents"][0]["base_url"] == gfx.BASE_URL
            assert j["config_sha256"]
            assert len(j["expected_task_checksum"]) == 64  # frozen per task
        ident = plan["arms"][arm]["expected_identity"]
        assert ident["hermes_core_commit"] == gfx.CORE


def test_plan_refuses_wrong_dataset_checkout(tmp_path):
    repo, _head = fake_dataset_repo(tmp_path)
    sel = gfx.frozen_selection_file(tmp_path / "selection.json",
                                    dataset_commit="0" * 40)
    proto = gfx.protocol_file(tmp_path / "protocol.json", sel,
                              task_root=repo, dataset_commit="0" * 40)
    with pytest.raises(SystemExit, match="dataset checkout HEAD"):
        cmd_plan(plan_args(tmp_path, proto))


def test_run_requires_explicit_harbor_command(tmp_path):
    with pytest.raises(SystemExit, match="explicit --harbor-command"):
        cmd_run(type("A", (), {
            "protocol": "p", "jobs_root": str(tmp_path), "harbor_command": None,
            "harbor_python": sys.executable,
            "stop_after": 0, "timeout": 10, "dry_run": True})())


def test_run_requires_finite_positive_timeout(tmp_path):
    with pytest.raises(SystemExit, match="finite positive"):
        cmd_run(type("A", (), {
            "protocol": "p", "jobs_root": str(tmp_path),
            "harbor_command": "echo {config}",
            "allow_fixture_command": True,
            "harbor_python": sys.executable,
            "stop_after": 0, "timeout": 0, "dry_run": True})())


def run_test_env(tmp_path, *, harbor_commit=None, python_version=None):
    """Env for cmd_run plumbing: synthetic plan bound to a fixture git
    'harbor source' and the real test interpreter (measured values by
    default; explicit mismatches for the refusal controls)."""
    import hashlib

    repo, head = fake_dataset_repo(tmp_path)
    measured_py = measure_python_version(sys.executable)
    sel = gfx.frozen_selection_file(tmp_path / "selection.json")
    proto = gfx.protocol_file(
        tmp_path / "protocol.json", sel,
        harbor_commit=harbor_commit if harbor_commit is not None else head,
        python_version=python_version if python_version is not None else measured_py)
    jobs_root = tmp_path / "jobs"
    gfx.write_plan(jobs_root,
                   json.loads(sel.read_text())["pools"]["fixture-pool"]["selected"],
                   harbor_source=repo)
    body = json.loads((jobs_root / "plan.json").read_text())
    body["protocol_sha256"] = hashlib.sha256(
        proto.read_bytes()).hexdigest()
    (jobs_root / "plan.json").write_text(json.dumps(body, indent=2))
    return proto, jobs_root


def test_run_measured_tool_identities_bound(tmp_path):
    proto, jobs_root = run_test_env(tmp_path)
    rc = cmd_run(type("A", (), {
        "protocol": str(proto), "jobs_root": str(jobs_root),
        "harbor_command": "echo HARBOR {config}",
            "allow_fixture_command": True,
        "harbor_python": sys.executable,
        "stop_after": 1, "timeout": 30, "dry_run": True})())
    assert rc == 0
    state = json.loads((jobs_root / "run-state.json").read_text())
    body = json.loads(proto.read_text())
    assert state["measured_harbor_head"] == body["harbor_commit"]
    assert state["measured_python_version"] == body["python_version"]
    assert state["plan_sha256"]


def test_run_refuses_harbor_head_mismatch(tmp_path):
    proto, jobs_root = run_test_env(tmp_path, harbor_commit="1" * 40)
    with pytest.raises(SystemExit, match="framework version"):
        cmd_run(type("A", (), {
            "protocol": str(proto), "jobs_root": str(jobs_root),
            "harbor_command": "echo {config}",
            "allow_fixture_command": True,
            "harbor_python": sys.executable,
            "stop_after": 0, "timeout": 30, "dry_run": True})())


def test_run_refuses_python_version_mismatch(tmp_path):
    proto, jobs_root = run_test_env(tmp_path, python_version="0.0.0")
    with pytest.raises(SystemExit, match="python"):
        cmd_run(type("A", (), {
            "protocol": str(proto), "jobs_root": str(jobs_root),
            "harbor_command": "echo {config}",
            "allow_fixture_command": True,
            "harbor_python": sys.executable,
            "stop_after": 0, "timeout": 30, "dry_run": True})())


def test_run_refuses_mutated_config_before_dispatch(tmp_path):
    proto, jobs_root = run_test_env(tmp_path)
    plan = json.loads((jobs_root / "plan.json").read_text())
    entry = plan["arms"]["baseline"]["jobs"][0]
    cfg = Path(entry["config"])
    body = json.loads(cfg.read_text())
    body["agent_timeout_multiplier"] = 0.01  # mutate after review
    cfg.write_text(json.dumps(body))
    with pytest.raises(SystemExit, match="drifted from the reviewed plan"):
        cmd_run(type("A", (), {
            "protocol": str(proto), "jobs_root": str(jobs_root),
            "harbor_command": "echo {config}",
            "allow_fixture_command": True,
            "harbor_python": sys.executable,
            "stop_after": 0, "timeout": 30, "dry_run": True})())


def test_run_nonzero_command_is_terminal_infra_failure(tmp_path):
    """W02: a nonzero harbor command is an infra FAILURE (exit 4) with a
    terminal receipt -- never ordinary success."""
    proto, jobs_root = run_test_env(tmp_path)
    rc = cmd_run(type("A", (), {
        "protocol": str(proto), "jobs_root": str(jobs_root),
        "harbor_command": "exit 7 # {config}",
            "allow_fixture_command": True,
        "harbor_python": sys.executable,
        "stop_after": 1, "timeout": 30, "dry_run": False})())
    assert rc == 4
    records = [json.loads(x) for x in
               (jobs_root / "progress.jsonl").read_text().splitlines()]
    statuses = [r["status"] for r in records]
    assert statuses[0] == "started" and "dispatched" in statuses  # W03 pre-spawn receipts
    assert records[-1]["status"] == "exit-7"  # terminal receipt retained


def test_run_timeout_kills_owned_children_and_records_terminal(tmp_path):
    """W02: REAL process control -- a harbor command that backgrounds
    its own child must be terminated with its whole owned group and
    leave a terminal timeout receipt."""
    hang = 'import subprocess,time; subprocess.Popen(["sleep","300"]); time.sleep(300)'
    proto, jobs_root = run_test_env(tmp_path)
    rc = cmd_run(type("A", (), {
        "protocol": str(proto), "jobs_root": str(jobs_root),
        "harbor_command": f'"{sys.executable}" -c \'{hang}\' # {{config}}',
            "allow_fixture_command": True,
        "harbor_python": sys.executable,
        "stop_after": 1, "timeout": 2, "dry_run": False})())
    assert rc == 4
    records = [json.loads(x) for x in
               (jobs_root / "progress.jsonl").read_text().splitlines()]
    assert records[-1]["status"] == "timeout"
    assert records[-1]["timed_out"] is True
    assert records[0]["status"] == "started"  # W03: persisted before spawn
    alive = subprocess.run(["pgrep", "-f", "sleep 300"],
                           capture_output=True, text=True, check=False).stdout.strip()
    assert not alive, f"orphaned owned children survived: {alive}"


def test_run_resume_never_duplicates_completed_jobs(tmp_path):
    """W02: no-duplicate resume -- completed terminal jobs are skipped
    without rerun; nothing is replayed or replaced."""
    proto, jobs_root = run_test_env(tmp_path)
    args = type("A", (), {
        "protocol": str(proto), "jobs_root": str(jobs_root),
        "harbor_command": "echo HARBOR {config}",
            "allow_fixture_command": True,
        "harbor_python": sys.executable,
        "stop_after": 2, "timeout": 30, "dry_run": False})()
    assert cmd_run(args) == 0
    progress = jobs_root / "progress.jsonl"
    first = [json.loads(x) for x in progress.read_text().splitlines()]
    assert len(first) == 6  # stop-after bound: 2 jobs x started+dispatched+terminal
    assert cmd_run(args) == 0  # resume continues with the NEXT jobs
    second = [json.loads(x) for x in progress.read_text().splitlines()]
    jobs = [r["job"] for r in second if r["status"] == "exit-ok"]
    assert sorted(jobs) == sorted(set(jobs)), "a completed job was re-dispatched"
    assert len(second) == 12  # 4 jobs x 3 records; none repeated
    assert all(r["status"] in ("started", "dispatched", "exit-ok") for r in second)


def test_run_refuses_interrupted_job_rather_than_rerunning(tmp_path):
    """W02: a started-but-not-terminal record is a clear refusal, never
    a silent rerun that could replace an unfavorable attempt."""
    proto, jobs_root = run_test_env(tmp_path)
    args = type("A", (), {
        "protocol": str(proto), "jobs_root": str(jobs_root),
        "harbor_command": "echo HARBOR {config}",
            "allow_fixture_command": True,
        "harbor_python": sys.executable,
        "stop_after": 1, "timeout": 30, "dry_run": False})()
    assert cmd_run(args) == 0
    progress = jobs_root / "progress.jsonl"
    records = [json.loads(x) for x in progress.read_text().splitlines()]
    # simulate a REAL hard interruption: the started/dispatched receipts
    # exist but no terminal record was ever written (see the W03 real
    # SIGKILL entrypoint test for the genuine path)
    records = [r for r in records if r["status"] != "exit-ok"]
    progress.write_text("".join(json.dumps(r) + "\n" for r in records))
    with pytest.raises(SystemExit, match="refusing to rerun"):
        cmd_run(args)


def test_dry_run_never_stands_in_for_completed_attempts(tmp_path):
    proto, jobs_root = run_test_env(tmp_path)
    rc = cmd_run(type("A", (), {
        "protocol": str(proto), "jobs_root": str(jobs_root),
        "harbor_command": "echo HARBOR {config}",
            "allow_fixture_command": True,
        "harbor_python": sys.executable,
        "stop_after": 40, "timeout": 30, "dry_run": True})())
    assert rc == 0
    records = [json.loads(x) for x in
               (jobs_root / "progress.jsonl").read_text().splitlines()]
    assert len(records) == 40
    assert all(r["status"] == "dry-run" for r in records)
    # W02: qualification refuses a dry-run-only tree
    gfx.write_run_state(jobs_root, "unused")
    sel = json.loads((tmp_path / "selection.json").read_text())
    for arm in ("baseline", "candidate"):
        for task_id in sel["pools"]["fixture-pool"]["selected"]:
            gfx.write_trial(jobs_root, arm, task_id)
    with pytest.raises(SystemExit, match="run-state"):
        cmd_qualify(type("A", (), {
            "protocol": str(proto), "jobs_root": str(jobs_root)})())


def test_default_gate2_placeholder_stays_not_run():
    """The T4 placeholder remains fail-closed; this package does not
    rewrite it, and a paired report never rewrites its exit contract."""
    placeholder = REPO / "evals" / "benchmark_gates" / "tblite_gate.py"
    proc = subprocess.run([sys.executable, str(placeholder), "plumbing-id",
                           "--log", "/dev/null"], capture_output=True, text=True,
                          check=False)
    assert proc.returncode != 0  # always blocks: NOT_RUN
    assert "NOT_RUN" in proc.stdout


def test_policy_evaluation_rule_is_predeclared_v1():
    rows = [{"task_id": f"t{i}", "baseline_reward": 1.0,
             "candidate_reward": 1.0, "delta": 0.0} for i in range(20)]
    ok = evaluate_policy({"name": POLICY_NAME, "version": 1}, rows)
    assert ok["pass"] is True
    rows[7]["candidate_reward"] = 0.0
    rows[7]["delta"] = -1.0
    bad = evaluate_policy({"name": POLICY_NAME, "version": 1}, rows)
    assert bad["pass"] is False and bad["regressed_tasks"] == ["t7"]
    unknown = evaluate_policy({"name": "other", "version": 9}, rows)
    assert unknown["pass"] is False  # no discretionary rule is ever chosen


# -- W02: pinned Harbor schema / canonical API (receipt-graded) -----------------


HARBOR_PY = os.environ.get("TBLITE_HARBOR_PYTHON", "")


def test_planned_configs_parse_through_pinned_harbor_jobconfig(tmp_path):
    """When the pinned Harbor env is provided (TBLITE_HARBOR_PYTHON),
    every planned config parses through the REAL JobConfig model of the
    installed primary source -- schema/nesting verified, no Docker."""
    if not HARBOR_PY:
        pytest.skip("TBLITE_HARBOR_PYTHON not set; canonical parse is "
                    "recorded in the W02 receipt runs")
    proto, _ = plan_test_env(tmp_path)
    assert cmd_plan(plan_args(tmp_path, proto)) == 0
    plan = json.loads((tmp_path / "jobs" / "plan.json").read_text())
    snippet = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from harbor.models.job.config import JobConfig\n"
        "for p in sys.argv[2:]:\n"
        "    JobConfig(**json.loads(open(p).read()))\n"
        "print('ok', len(sys.argv) - 2)\n"
    )
    src = os.environ.get("TBLITE_HARBOR_SRC", "")
    assert src, "TBLITE_HARBOR_SRC (pinned harbor checkout) required"
    configs = [j["config"] for arm in ("baseline", "candidate")
               for j in plan["arms"][arm]["jobs"]]
    proc = subprocess.run([HARBOR_PY, "-c", snippet, src, *configs],
                          capture_output=True, text=True, timeout=600, check=False)
    assert proc.returncode == 0, proc.stderr[-400:]
    assert proc.stdout.startswith("ok 40")


def test_canonical_checksum_through_pinned_harbor_task_api(tmp_path):
    """The canonical task checksum measured through the REAL pinned
    Task API (same digest the framework writes into trial results)."""
    if not (HARBOR_PY and os.environ.get("TBLITE_HARBOR_SRC")):
        pytest.skip("pinned Harbor env not provided; canonical checksum "
                    "equivalence is recorded in the W02 receipt runs")
    from evals.tblite_paired_gate.paired_gate import measure_task_checksum
    task_dir = Path(
        "/home/vern/.cache/hermes-tblite-preflight/repos/OpenThoughts-TBLite/jsonl-aggregator")
    if not task_dir.is_dir():
        pytest.skip("pinned dataset checkout not present on this host")
    digest = measure_task_checksum(
        HARBOR_PY, os.environ["TBLITE_HARBOR_SRC"], task_dir)
    # equals the checksum recorded in the accepted W03 production trial
    assert digest == "96e1b6e2141983e07462dafbeb54a477d32151922ad61e61979da961458d1f05"
