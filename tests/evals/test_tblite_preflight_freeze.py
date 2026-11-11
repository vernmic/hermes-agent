"""Offline controls for the TBLite preflight freeze v2 and pinned agent glue.

No Docker, no network, no model calls. Covers:
- fail-closed qualified freeze: unversioned folder, wrong commit, dirty
  source, task-count mismatch, and generic mode's explicit
  unqualified/no-selection contract;
- complete task-tree binding: mutating an environment asset consumed by
  Docker COPY changes the manifest;
- selection continuity (--expect-selection) and determinism;
- pinned agent identity: validated/quoted pin arguments, observed-HEAD
  capture, native --skills preload interception with skill-body hashing,
  and verified-preinstalled skip.
"""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = ROOT / "evals" / "tblite_preflight"
FREEZE_CLI = PREFLIGHT / "freeze_fast_subset.py"

SEED = "hermes-tblite-preflight-20261110-v1"
FORK_SLUG = "vernmic/hermes-agent"
PINNED_BRANCH = "codex/hermes2-v0.21.7"
PINNED_COMMIT = "93faf1387ce4bb4769de506d47a3c82afb9d0326"

EXPECTED_ALLOCATION = {
    "data-processing-scripting": 3,
    "security-cryptography": 3,
    "software-engineering": 2,
    "machine-learning-ai": 2,
    "interactive-games": 2,
    "debugging-troubleshooting": 2,
    "scientific-computing": 2,
    "system-setup-configuration": 2,
    "build-dependency-management": 2,
}

TASK_TOML = """version = "1.0"

[metadata]
author_name = "fixture"
difficulty = "{difficulty}"
category = "{category}"

[verifier]
timeout_sec = 900.0

[agent]
timeout_sec = 900.0

[environment]
build_timeout_sec = 600.0
"""

DOCKERFILE = "FROM ubuntu:24.04\nCOPY asset.txt /app/asset.txt\n"


def load_freeze_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("tblite_freeze_under_test", FREEZE_CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def freeze():
    return load_freeze_module()


def make_task(root, name, category="data_science", difficulty="easy"):
    d = Path(root) / name
    (d / "environment").mkdir(parents=True)
    (d / "tests").mkdir()
    (d / "solution").mkdir()
    (d / "task.toml").write_text(TASK_TOML.format(difficulty=difficulty, category=category))
    (d / "environment" / "Dockerfile").write_text(DOCKERFILE)
    (d / "environment" / "asset.txt").write_text(f"asset-for-{name}\n")
    (d / "instruction.md").write_text("do the thing\n")
    (d / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    (d / "solution" / "solve.sh").write_text("#!/bin/sh\ntrue\n")
    return d


def git_repo_with_tasks(tmp_path, n_tasks=1, commit_message="fixture"):
    repo = tmp_path / "dataset"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    for i in range(n_tasks):
        make_task(repo, f"task-{i:03d}")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", commit_message], check=True)
    return repo


def run_freeze(*cli, cwd=None):
    proc = subprocess.run(
        [sys.executable, str(FREEZE_CLI), *cli],
        capture_output=True, text=True, cwd=cwd, check=False,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"raw": proc.stdout, "err": proc.stderr}
    return proc.returncode, payload


# -- qualified freeze is fail-closed ---------------------------------------

def test_unversioned_folder_fails_qualified_mode(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    make_task(folder, "task-000")
    rc, out = run_freeze(
        "--dataset", str(folder),
        "--manifest", str(tmp_path / "m.json"),
    )
    assert rc == 1
    assert out["reason"] == "dataset_not_a_git_checkout"


def test_wrong_commit_fails_qualified_mode(tmp_path):
    repo = git_repo_with_tasks(tmp_path)
    other = git_repo_with_tasks(tmp_path / "other", commit_message="other")
    other_head = subprocess.run(
        ["git", "-C", str(other), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    rc, out = run_freeze(
        "--dataset", str(repo),
        "--manifest", str(tmp_path / "m.json"),
        "--expected-commit", other_head,
    )
    assert rc == 1
    assert out["reason"] == "dataset_commit_mismatch"
    assert not (tmp_path / "m.json").exists()


def test_dirty_source_fails_qualified_mode(tmp_path):
    repo = git_repo_with_tasks(tmp_path)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    (repo / "task-000" / "environment" / "asset.txt").write_text("mutated\n")
    rc, out = run_freeze(
        "--dataset", str(repo),
        "--manifest", str(tmp_path / "m.json"),
        "--expected-commit", head,
    )
    assert rc == 1
    assert out["reason"] == "dataset_dirty"


def test_task_count_mismatch_fails_qualified_mode(tmp_path):
    repo = git_repo_with_tasks(tmp_path, n_tasks=1)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    rc, out = run_freeze(
        "--dataset", str(repo),
        "--manifest", str(tmp_path / "m.json"),
        "--expected-commit", head,
    )
    assert rc == 1
    assert out["reason"] == "task_count_mismatch"


def test_generic_mode_is_explicitly_unqualified_and_cannot_freeze_selection(tmp_path):
    folder = tmp_path / "fixture"
    folder.mkdir()
    make_task(folder, "task-000")
    rc, out = run_freeze(
        "--dataset", str(folder),
        "--manifest", str(tmp_path / "m.json"),
        "--selection", str(tmp_path / "s.json"),
        "--generic",
    )
    assert rc == 1
    assert out["reason"] == "generic_mode_cannot_freeze_selection"
    assert not (tmp_path / "s.json").exists()
    rc, out = run_freeze(
        "--dataset", str(folder),
        "--manifest", str(tmp_path / "m.json"),
        "--generic",
    )
    assert rc == 0
    doc = json.loads((tmp_path / "m.json").read_text())
    assert doc["qualified"] is False
    assert doc["mode"] == "generic-unqualified"
    assert "NOT the verified pinned corpus" in doc["declared_note"]
    assert doc["observed_dataset_commit"] is None


# -- complete task-tree binding --------------------------------------------

def test_environment_asset_drift_changes_task_binding(tmp_path):
    repo = git_repo_with_tasks(tmp_path)
    m1 = tmp_path / "m1.json"
    rc, _ = run_freeze("--dataset", str(repo), "--manifest", str(m1), "--generic")
    assert rc == 0
    before = json.loads(m1.read_text())["tasks"][0]["task_tree_sha256"]
    # Same repo, one environment asset consumed by Docker COPY mutated:
    # the complete task-tree binding must change.
    (repo / "task-000" / "environment" / "asset.txt").write_text("drifted\n")
    m2 = tmp_path / "m2.json"
    rc, _ = run_freeze("--dataset", str(repo), "--manifest", str(m2), "--generic")
    assert rc == 0
    after = json.loads(m2.read_text())["tasks"][0]["task_tree_sha256"]
    assert before != after


def test_tree_sha_excludes_only_documented_caches(freeze, tmp_path):
    d = tmp_path / "t"
    (d / "__pycache__").mkdir(parents=True)
    (d / "__pycache__" / "junk.pyc").write_text("junk")
    (d / "a.txt").write_text("x")
    first = freeze.tree_sha(d)
    (d / "__pycache__" / "junk.pyc").write_text("other junk")
    assert freeze.tree_sha(d) == first
    (d / "a.txt").write_text("y")
    assert freeze.tree_sha(d) != first


# -- selection contracts ----------------------------------------------------

def test_allocation_sums_to_twenty_across_nine_categories():
    assert sum(EXPECTED_ALLOCATION.values()) == 20
    assert len(EXPECTED_ALLOCATION) == 9


def test_selection_is_deterministic_and_seed_bound(freeze):
    tasks = [
        {"id": f"task-{i:03d}", "canonical_category": "data-processing-scripting",
         "eligible": True}
        for i in range(5)
    ]
    s1 = freeze.select_subset(json.loads(json.dumps(tasks)), SEED)
    s2 = freeze.select_subset(json.loads(json.dumps(tasks)), SEED)
    assert s1["selected_ids"] == s2["selected_ids"]
    other = freeze.select_subset(json.loads(json.dumps(tasks)), SEED + "-x")
    assert other["seed"] != s1["seed"]


def test_ineligible_and_unmapped_tasks_are_never_selected(freeze):
    tasks = [
        {"id": "ok-a", "canonical_category": "security-cryptography", "eligible": True},
        {"id": "hard", "canonical_category": "security-cryptography", "eligible": False},
        {"id": "unmapped", "canonical_category": None, "eligible": False},
    ]
    selection = freeze.select_subset(tasks, SEED)
    assert "hard" not in selection["selected_ids"]
    assert "unmapped" not in selection["selected_ids"]
    assert selection["pools"]["security-cryptography"]["selected"] == ["ok-a"]


def test_selection_continuity_guard_rejects_changed_ids(tmp_path):
    repo = git_repo_with_tasks(tmp_path)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    prior = tmp_path / "prior.json"
    prior.write_text(json.dumps({"selected_ids": ["different-task"]}))
    rc, out = run_freeze(
        "--dataset", str(repo),
        "--manifest", str(tmp_path / "m.json"),
        "--expected-commit", head,
        "--expect-selection", str(prior),
    )
    assert rc == 1
    # Continuity is checked after provenance: 1-task repo fails count first.
    assert out["reason"] in {"task_count_mismatch", "selection_continuity_violation"}


# -- pinned agent glue ------------------------------------------------------

def _stub_harbor():
    import sys, types

    hermes_mod = types.ModuleType("harbor.agents.installed.hermes")

    class _Hermes:
        def __init__(self, *a, **k):
            self.commands = []

        async def ensure_system_dependencies(self, env, deps):
            self.deps = deps

        async def exec_as_agent(self, env, command=None, *a, **k):
            self.commands.append(command)
            return types.SimpleNamespace(stdout="")

    hermes_mod.Hermes = _Hermes
    sys.modules.setdefault("harbor", types.ModuleType("harbor"))
    sys.modules["harbor.agents"] = types.ModuleType("harbor.agents")
    sys.modules["harbor.agents.installed"] = types.ModuleType("harbor.agents.installed")
    sys.modules["harbor.agents.installed.hermes"] = hermes_mod
    return _Hermes


def _load_agent_module(monkeypatch=None):
    import importlib.util

    stub = _stub_harbor()
    spec = importlib.util.spec_from_file_location(
        "pinned_hermes_under_test", PREFLIGHT / "pinned_hermes_agent.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, stub


def _make_agent(mod, **kw):
    agent = mod.PinnedHermes(logs_dir=None, **kw)
    return agent


@pytest.mark.asyncio
async def test_install_pins_installer_target_and_observes_head():
    mod, _ = _load_agent_module()
    import types

    agent = _make_agent(mod)
    calls = []

    async def two_phase(environment, command=None, *a, **k):
        calls.append(command)
        if len(calls) == 1:
            # preinstall probe: nothing installed yet
            raise RuntimeError("not installed yet")
        return types.SimpleNamespace(stdout="")

    agent.exec_as_agent = two_phase
    await mod.PinnedHermes.install(agent, types.SimpleNamespace())
    cmd = calls[1]
    assert f"/{PINNED_COMMIT}/scripts/install.sh" in cmd
    assert f"--branch {PINNED_BRANCH}" in cmd and f"--commit {PINNED_COMMIT}" in cmd
    assert "git -C \"/tmp/hermes/hermes-agent\" rev-parse HEAD" in cmd
    assert "tee /tmp/hermes-observed-identity.txt" in cmd
    assert "hermes --version" in cmd
    assert cmd.count("--branch") == 1
    # quoting present for repo url
    assert "HERMES_REPO_URL=https://github.com/vernmic/hermes-agent.git" in cmd


@pytest.mark.asyncio
async def test_preinstalled_pin_skips_installer():
    mod, _ = _load_agent_module()
    import types

    agent = _make_agent(mod)
    calls = []

    async def probe_ok(environment, command=None, *a, **k):
        calls.append(command)
        return types.SimpleNamespace(stdout=f"{PINNED_COMMIT}\nHermes Agent v0.21.7 fixture\n")

    agent.exec_as_agent = probe_ok
    await mod.PinnedHermes.install(agent, types.SimpleNamespace())
    assert len(calls) == 1  # probe only, no installer download
    assert "rev-parse HEAD" in calls[0]


@pytest.mark.asyncio
async def test_run_time_gate_runs_then_chat_gets_native_preload():
    mod, stub_cls = _load_agent_module()
    import types

    calls = []
    gate_stdout = (
        "93faf1387ce4bb4769de506d47a3c82afb9d0326\n"
        "Hermes Agent v0.21.7+21.g93faf13\n"
        "<sha-line>\n"
    ).replace("<sha-line>", "649e6ab89bd8aaad2f9b162c60321f227cff8a8cda56393f07a81d1c6825a692  /tmp/hermes/skills/test-driven-development/SKILL.md")

    async def capture(self, environment, command=None, *a, **k):
        calls.append(command)
        if "chat" in (command or ""):
            return types.SimpleNamespace(stdout="")
        return types.SimpleNamespace(stdout=gate_stdout)

    stub_cls.exec_as_agent = capture
    agent = _make_agent(mod, preload_skills=["test-driven-development"])
    base_cmd = (
        'export PATH="$HOME/.local/bin:$PATH" && hermes --yolo chat '
        '-q "$HERMES_INSTRUCTION" -Q --model glm-5.3 --provider zai '
        "2>&1 | stdbuf -oL tee /logs/agent/hermes.txt"
    )
    await mod.PinnedHermes.exec_as_agent(agent, types.SimpleNamespace(), base_cmd)
    # first call: the runtime identity gate; second: the chat command
    assert "runtime-identity.txt" in calls[0]
    assert calls[0].startswith("mkdir -p /logs/agent")
    assert "GLM_BASE_URL" in calls[0]
    cmd = calls[1]
    assert "--skills test-driven-development" in cmd
    assert cmd.count("--skills") == 1


@pytest.mark.asyncio
async def test_gate_blocks_wrong_observed_head():
    mod, stub_cls = _load_agent_module()
    import types

    calls = []

    async def capture(self, environment, command=None, *a, **k):
        calls.append(command)
        return types.SimpleNamespace(stdout="0000000000000000000000000000000000000000\nvX\n")

    stub_cls.exec_as_agent = capture
    agent = _make_agent(mod)
    with pytest.raises(RuntimeError, match="observed HEAD"):
        await mod.PinnedHermes.exec_as_agent(
            agent, types.SimpleNamespace(), "x && hermes --yolo chat -q y -Q"
        )
    assert len(calls) == 1  # chat command never ran


@pytest.mark.asyncio
async def test_gate_blocks_missing_head_measurement():
    mod, stub_cls = _load_agent_module()
    import types

    async def capture(self, environment, command=None, *a, **k):
        return types.SimpleNamespace(stdout="no head here\n")

    stub_cls.exec_as_agent = capture
    agent = _make_agent(mod)
    with pytest.raises(RuntimeError, match="missing"):
        await mod.PinnedHermes.exec_as_agent(
            agent, types.SimpleNamespace(), "hermes --yolo chat -q y -Q"
        )


def test_gate_blocks_wrong_or_missing_skill_hash():
    mod, _ = _load_agent_module()
    agent = _make_agent(
        mod,
        preload_skills=["test-driven-development"],
        expected_skill_sha256="test-driven-development:" + "649e6ab89bd8aaad2f9b162c60321f227cff8a8cda56393f07a81d1c6825a692",
    )
    good = (
        "93faf1387ce4bb4769de506d47a3c82afb9d0326\n"
        "649e6ab89bd8aaad2f9b162c60321f227cff8a8cda56393f07a81d1c6825a692  /tmp/hermes/skills/test-driven-development/SKILL.md\n"
    )
    agent._enforce_runtime_identity(good)  # passes
    wrong_sha = good.replace("649e6ab8", "a49e6ab8", 1)
    with pytest.raises(RuntimeError, match="sha .* != expected"):
        agent._enforce_runtime_identity(wrong_sha)
    with pytest.raises(RuntimeError, match="hash missing"):
        agent._enforce_runtime_identity("93faf1387ce4bb4769de506d47a3c82afb9d0326\n")


def test_gate_blocks_wrong_or_missing_base_url():
    mod, _ = _load_agent_module()
    agent = _make_agent(
        mod, expected_base_url="https://api.z.ai/api/coding/paas/v4"
    )
    head = "93faf1387ce4bb4769de506d47a3c82afb9d0326\n"
    agent._enforce_runtime_identity(
        head + "GLM_BASE_URL=https://api.z.ai/api/coding/paas/v4\n"
    )
    with pytest.raises(RuntimeError, match="GLM_BASE_URL"):
        agent._enforce_runtime_identity(head + "GLM_BASE_URL=https://api.z.ai/api/paas/v4\n")
    with pytest.raises(RuntimeError, match="GLM_BASE_URL"):
        agent._enforce_runtime_identity(head)


def test_skill_sha_config_is_validated():
    mod, _ = _load_agent_module()
    with pytest.raises(ValueError):
        _make_agent(mod, expected_skill_sha256="tdd:not-a-sha")
    with pytest.raises(ValueError):
        _make_agent(mod, expected_skill_sha256="bad name;" + "0" * 64)


# -- gate must run on EVERY native chat launch (W04 bypass fix) ---------------


def _good_gate_stdout():
    return (
        "93faf1387ce4bb4769de506d47a3c82afb9d0326\n"
        "649e6ab89bd8aaad2f9b162c60321f227cff8a8cda56393f07a81d1c6825a692  /tmp/hermes/skills/test-driven-development/SKILL.md\n"
        "GLM_BASE_URL=https://api.z.ai/api/coding/paas/v4\n"
    )


@pytest.mark.asyncio
async def test_preloaded_command_gated_exactly_once_no_duplicate_flag():
    mod, stub_cls = _load_agent_module()
    import types

    calls = []

    async def capture(self, environment, command=None, *a, **k):
        calls.append(command)
        if "chat" not in (command or ""):
            return types.SimpleNamespace(stdout=_good_gate_stdout())
        return types.SimpleNamespace(stdout="")

    stub_cls.exec_as_agent = capture
    agent = _make_agent(mod, preload_skills=["test-driven-development"])
    preloaded_cmd = (
        'export PATH="$HOME/.local/bin:$PATH" && hermes --yolo chat '
        "--skills test-driven-development "
        '-q "$HERMES_INSTRUCTION" -Q --model glm-5.3 --provider zai'
    )
    await mod.PinnedHermes.exec_as_agent(agent, types.SimpleNamespace(), preloaded_cmd)
    assert len(calls) == 2, "gate + chat exactly once each"
    assert "runtime-identity.txt" in calls[0]
    assert calls[1] == preloaded_cmd, "already-preloaded command must pass through unchanged"
    assert calls[1].count("--skills") == 1


@pytest.mark.asyncio
async def test_wrong_head_blocks_before_chat_even_with_preloaded_skills():
    mod, stub_cls = _load_agent_module()
    import types

    calls = []

    async def capture(self, environment, command=None, *a, **k):
        calls.append(command)
        return types.SimpleNamespace(stdout="0000000000000000000000000000000000000000\n")

    stub_cls.exec_as_agent = capture
    agent = _make_agent(mod, preload_skills=["test-driven-development"])
    preloaded_cmd = "hermes --yolo chat --skills test-driven-development -q x -Q"
    with pytest.raises(RuntimeError, match="observed HEAD"):
        await mod.PinnedHermes.exec_as_agent(agent, types.SimpleNamespace(), preloaded_cmd)
    assert len(calls) == 1, "chat must never launch when the gate fails"


@pytest.mark.asyncio
async def test_differing_launched_skill_selection_blocks():
    mod, stub_cls = _load_agent_module()
    import types

    calls = []

    async def capture(self, environment, command=None, *a, **k):
        calls.append(command)
        return types.SimpleNamespace(stdout=_good_gate_stdout())

    stub_cls.exec_as_agent = capture
    agent = _make_agent(mod, preload_skills=["test-driven-development"])
    other_cmd = "hermes --yolo chat --skills some-other-skill -q x -Q"
    with pytest.raises(RuntimeError, match="!= configured preload"):
        await mod.PinnedHermes.exec_as_agent(agent, types.SimpleNamespace(), other_cmd)
    assert len(calls) == 1, "differing selection must block before chat"


@pytest.mark.asyncio
async def test_unconfigured_skills_flag_blocks():
    mod, stub_cls = _load_agent_module()
    import types

    calls = []

    async def capture(self, environment, command=None, *a, **k):
        calls.append(command)
        return types.SimpleNamespace(stdout="93faf1387ce4bb4769de506d47a3c82afb9d0326\n")

    stub_cls.exec_as_agent = capture
    agent = _make_agent(mod)  # no preload configured
    with pytest.raises(RuntimeError, match="no preload is configured"):
        await mod.PinnedHermes.exec_as_agent(
            agent, types.SimpleNamespace(), "hermes --yolo chat --skills anything -q x -Q"
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_original_insertion_still_works_when_flag_absent():
    mod, stub_cls = _load_agent_module()
    import types

    calls = []

    async def capture(self, environment, command=None, *a, **k):
        calls.append(command)
        if "chat" in (command or ""):
            return types.SimpleNamespace(stdout="")
        return types.SimpleNamespace(stdout=_good_gate_stdout())

    stub_cls.exec_as_agent = capture
    agent = _make_agent(mod, preload_skills=["test-driven-development"])
    await mod.PinnedHermes.exec_as_agent(
        agent, types.SimpleNamespace(), "hermes --yolo chat -q x -Q"
    )
    assert calls[1].count("--skills") == 1
    assert "--skills test-driven-development" in calls[1]


# -- W05 boundary 1: gate queries the LIVE checkout HEAD ----------------------


def _gate_fixture(tmp_path, monkeypatch, mod):
    """Real temporary environment: git checkout + skill file + logs dir,
    wired into the module's configurable observation paths."""
    home = tmp_path / "home"
    repo = home / "hermes-agent"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "cli.py").write_text("# fixture\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    skill_dir = home / "skills" / "test-driven-development"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: test-driven-development\n---\nbody\n")
    skill_sha = hashlib.sha256((skill_dir / "SKILL.md").read_bytes()).hexdigest()
    logs = tmp_path / "logs"
    monkeypatch.setattr(mod, "_HERMES_HOME", str(home))
    monkeypatch.setattr(mod, "_LOGS_AGENT", str(logs))
    return repo, head, skill_sha


def _run_gate(mod, agent):
    proc = subprocess.run(
        ["bash", "-c", agent._runtime_gate_command()],
        capture_output=True, text=True, check=False,
    )
    return proc


def test_gate_reads_live_checkout_head_and_accepts_match(tmp_path, monkeypatch):
    mod, _ = _load_agent_module()
    _repo, head, skill_sha = _gate_fixture(tmp_path, monkeypatch, mod)
    agent = _make_agent(
        mod, commit=head,
        preload_skills=["test-driven-development"],
        expected_skill_sha256=f"test-driven-development:{skill_sha}",
    )
    proc = _run_gate(mod, agent)
    assert proc.returncode == 0, proc.stderr
    agent._enforce_runtime_identity(proc.stdout)  # live HEAD matches: passes
    assert (tmp_path / "logs" / "runtime-identity.txt").exists()


def test_gate_blocks_checkout_head_moved_after_install(tmp_path, monkeypatch):
    mod, _ = _load_agent_module()
    repo, head, _skill_sha = _gate_fixture(tmp_path, monkeypatch, mod)
    agent = _make_agent(mod, commit=head, preload_skills=["test-driven-development"])
    # the checkout moves AFTER the install receipt existed
    (repo / "cli.py").write_text("# mutated\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "moved"], check=True)
    proc = _run_gate(mod, agent)
    assert proc.returncode == 0
    with pytest.raises(RuntimeError, match="observed HEAD"):
        agent._enforce_runtime_identity(proc.stdout)


def test_gate_blocks_when_git_cannot_resolve_head(tmp_path, monkeypatch):
    import shutil

    mod, _ = _load_agent_module()
    repo, head, _skill_sha = _gate_fixture(tmp_path, monkeypatch, mod)
    agent = _make_agent(mod, commit=head, preload_skills=["test-driven-development"])
    shutil.rmtree(repo / ".git")
    proc = _run_gate(mod, agent)
    assert proc.returncode != 0, "missing repository must make the gate command fail"


# -- W05 boundary 2: COMPLETE --skills selection parsing ------------------------


def _sel(mod, cmd):
    return mod._launched_skill_selection(cmd)


def test_repeated_skills_flag_with_extra_blocks():
    mod, _ = _load_agent_module()
    agent = _make_agent(mod, preload_skills=["test-driven-development"])
    cmd = "hermes --yolo chat --skills test-driven-development --skills unverified-extra -q x -Q"
    with pytest.raises(RuntimeError, match="!= configured preload"):
        agent._enforce_preload_selection(cmd)


def test_short_form_repeats_with_extra_blocks():
    mod, _ = _load_agent_module()
    agent = _make_agent(mod, preload_skills=["test-driven-development"])
    cmd = "hermes --yolo chat -s test-driven-development -s extra -q x -Q"
    with pytest.raises(RuntimeError, match="!= configured preload"):
        agent._enforce_preload_selection(cmd)


def test_equals_form_and_attached_short_form_parse():
    mod, _ = _load_agent_module()
    assert _sel(mod, "hermes --yolo chat --skills=tdd -q x -Q") == {"tdd"}
    assert _sel(mod, "hermes --yolo chat -s=tdd -q x -Q") == {"tdd"}
    assert _sel(mod, "hermes --yolo chat -stdd -q x -Q") == {"tdd"}


def test_all_forms_together_accumulate_complete_selection():
    mod, _ = _load_agent_module()
    cmd = "hermes --yolo chat --skills a -s=b -sc --skills=d,e -q x -Q"
    assert _sel(mod, cmd) == {"a", "b", "c", "d", "e"}


def test_configured_same_repeats_normalize_to_pass():
    mod, _ = _load_agent_module()
    agent = _make_agent(mod, preload_skills=["test-driven-development"])
    cmd = "hermes --yolo chat --skills test-driven-development -s test-driven-development -q x -Q"
    assert agent._enforce_preload_selection(cmd) == {"test-driven-development"}


def test_quoted_prompt_mention_is_not_a_flag():
    mod, _ = _load_agent_module()
    cmd = 'hermes --yolo chat -q "please use --skills carefully in the answer" -Q'
    assert _sel(mod, cmd) is None


def test_skills_shaped_prompt_token_is_ambiguous_and_blocks():
    mod, _ = _load_agent_module()
    agent = _make_agent(mod, preload_skills=["test-driven-development"])
    with pytest.raises(RuntimeError, match="ambiguous"):
        agent._enforce_preload_selection("hermes --yolo chat -q --skills -Q")


def test_skills_flag_before_chat_subcommand_blocks():
    mod, _ = _load_agent_module()
    agent = _make_agent(mod, preload_skills=["test-driven-development"])
    with pytest.raises(RuntimeError, match="outside the supported adapter command shape"):
        agent._enforce_preload_selection("hermes --skills tdd --yolo chat -q x -Q")


def test_dangling_skills_flag_blocks():
    mod, _ = _load_agent_module()
    with pytest.raises(RuntimeError, match="dangling"):
        _sel(mod, "hermes --yolo chat --skills")


@pytest.mark.asyncio
async def test_prompt_mention_does_not_suppress_insertion():
    mod, stub_cls = _load_agent_module()
    import types

    calls = []

    async def capture(self, environment, command=None, *a, **k):
        calls.append(command)
        if "chat" in (command or ""):
            return types.SimpleNamespace(stdout="")
        return types.SimpleNamespace(stdout=_good_gate_stdout())

    stub_cls.exec_as_agent = capture
    agent = _make_agent(mod, preload_skills=["test-driven-development"])
    cmd = 'export PATH="$HOME/.local/bin:$PATH" && hermes --yolo chat -q "mention --skills here" -Q'
    await mod.PinnedHermes.exec_as_agent(agent, types.SimpleNamespace(), cmd)
    # exactly one inserted flag; the prompt mention passes through untouched
    assert calls[1].count("--skills test-driven-development") == 1
    assert '"mention --skills here"' in calls[1]


def test_invalid_identity_arguments_are_rejected():
    mod, _ = _load_agent_module()
    with pytest.raises(ValueError):
        _make_agent(mod, commit="main")  # branch name where a sha belongs
    with pytest.raises(ValueError):
        _make_agent(mod, branch="--evil")
    with pytest.raises(ValueError):
        _make_agent(mod, fork_slug="a/b; rm -rf /")
    with pytest.raises(ValueError):
        _make_agent(mod, preload_skills=["x; touch /tmp/pwned"])
