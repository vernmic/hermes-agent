"""Regression tests for the benchmark gates (Phase 7.2 / T4).

Staged from the independently accepted W4/R03 bounded pytest gate. These
tests pin the fail-closed contract: a candidate promotes only on real
executed passing tests with staged import-origin proof, bound to the
candidate/test identity; every degenerate shape (no-op, help,
collect-only, empty, all-skipped, timeout, control-only, mutated
identity, external-reference or shadowed imports) blocks. The TBLite
placeholder stays NOT RUN and can never pass.

All gate runs use disposable candidate/oracle trees under tmp_path and a
gate log redirected there; nothing writes a live or in-repo gate log.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATES = ROOT / "evals" / "benchmark_gates"
GATE = GATES / "gate1_pytest.py"
TBLITE = GATES / "tblite_gate.py"

CORRECT_ADDER = "def add(a, b):\n    return a + b\n"
BROKEN_ADDER = "def add(a, b):\n    return a * b\n"
ADDER_TESTS = "import adder\n\ndef test_add():\n    assert adder.add(1, 2) == 3\n"
HANG_TESTS = (
    "import threading\n"
    "import adder\n"
    "\n"
    "def test_add():\n"
    "    assert adder.add(1, 2) == 3\n"
    "\n"
    "def test_hang():\n"
    "    threading.Event().wait(60)\n"
)


def load_gate_module():
    spec = importlib.util.spec_from_file_location("gate1_pytest_under_test", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gate_mod():
    return load_gate_module()


@pytest.fixture(autouse=True)
def no_gate_log_in_source_tree():
    yield
    assert not (GATES / "gate-log.jsonl").exists()
    assert not (GATES / "logs").exists()


def make_candidate(root, impl=CORRECT_ADDER, tests=ADDER_TESTS):
    cand = Path(root) / "candidate"
    (cand / "tests").mkdir(parents=True)
    (cand / "adder.py").write_text(impl)
    (cand / "tests" / "test_adder.py").write_text(tests)
    return cand


def make_external_oracle(root, impl=CORRECT_ADDER):
    ref = Path(root) / "reference"
    ref.mkdir()
    (ref / "targetlib.py").write_text(impl)
    (ref / "test_targetlib.py").write_text(
        "import targetlib\n\ndef test_add():\n    assert targetlib.add(1, 2) == 3\n"
    )
    return ref


def gate_cli(*cli, log, logs_dir=None, env=None):
    cmd = [sys.executable, str(GATE), *cli, "--log", str(log)]
    if logs_dir is not None:
        cmd.extend(["--logs-dir", str(logs_dir)])
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"raw": proc.stdout, "err": proc.stderr}
    return proc.returncode, payload


def gate_run(tmp_path, candidate, tests, *extra, env=None, candidate_id="cand"):
    return gate_cli(
        "run",
        "--candidate-id", candidate_id,
        "--candidate", str(candidate),
        "--tests", str(tests),
        "--python", sys.executable,
        *extra,
        log=tmp_path / "gate-log.jsonl",
        logs_dir=tmp_path / "logs",
        env=env,
    )


def gate_check(tmp_path, candidate, tests, *extra, candidate_id="cand"):
    return gate_cli(
        "check",
        "--candidate-id", candidate_id,
        "--candidate", str(candidate),
        "--tests", str(tests),
        *extra,
        log=tmp_path / "gate-log.jsonl",
    )


# -- unit contracts on the gate's pure logic ------------------------------

def test_classify_requires_executed_passing_tests(gate_mod):
    ok = {"passed": 2}
    assert gate_mod.classify(0, False, ok, None) == ("pass", True)
    assert gate_mod.classify(0, False, {"passed": 2, "skipped": 1}, None) == ("pass", True)
    # zero executed, zero skipped -> empty; skips only -> all_skipped
    assert gate_mod.classify(0, False, {"skipped": 3}, None) == ("all_skipped", False)
    assert gate_mod.classify(5, False, None, None) == ("empty_suite", False)
    assert gate_mod.classify(0, False, None, None) == ("empty_suite", False)


def test_classify_failclosed_outcomes(gate_mod):
    assert gate_mod.classify(1, False, {"passed": 1, "failed": 1}, None) == ("test_failure", False)
    assert gate_mod.classify(0, False, {"passed": 1, "errors": 1}, None) == ("test_failure", False)
    assert gate_mod.classify(4, False, {"passed": 1}, None) == ("usage_error", False)
    assert gate_mod.classify(2, False, {"passed": 1}, None) == ("command_error", False)
    assert gate_mod.classify(None, False, None, None) == ("command_error", False)
    assert gate_mod.classify(0, True, {"passed": 1}, None) == ("timeout", False)
    # an argv-level rejection outranks everything else, even a timeout
    assert gate_mod.classify(0, True, {"passed": 1}, "no_op") == ("no_op", False)


def test_parse_pytest_counts(gate_mod):
    line = "2 passed, 1 failed, 4 errors, 3 skipped in 0.01s"
    assert gate_mod.parse_pytest_counts(line) == {
        "passed": 2, "failed": 1, "errors": 4, "skipped": 3,
    }
    assert gate_mod.parse_pytest_counts("no summary here") is None


def test_banned_reason_blocks_nonqualifying_commands(gate_mod):
    py = "/usr/bin/python3"
    base = [py, "-m", "pytest", "tests"]
    assert gate_mod.banned_reason(py, base) is None
    assert gate_mod.banned_reason(py, [py, "-m", "pytest", "--collect-only", "tests"]) == "collect_only"
    assert gate_mod.banned_reason(py, [py, "-m", "pytest", "--co", "tests"]) == "collect_only"
    assert gate_mod.banned_reason(py, [py, "-m", "pytest", "--help"]) == "help"
    assert gate_mod.banned_reason(py, [py, "-m", "pytest", "-h"]) == "help"
    assert gate_mod.banned_reason(py, [py, "-c", "pass"]) == "no_op"
    assert gate_mod.banned_reason(py, [py, "-m", "unittest", "tests"]) == "not_pytest"
    assert gate_mod.banned_reason(py, ["/other/python", "-m", "pytest", "tests"]) == "interpreter_mismatch"


def test_contract_sha_binds_tests_and_args(gate_mod, tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("def test_x():\n    assert True\n")
    a = gate_mod.contract_sha(tests, ["-q"])
    assert a == gate_mod.contract_sha(tests, ["-q"])
    assert a != gate_mod.contract_sha(tests, ["-x"])
    (tests / "test_x.py").write_text("def test_x():\n    assert 1 == 1\n")
    assert a != gate_mod.contract_sha(tests, ["-q"])


def test_tree_sha_ignores_caches_and_pyc(gate_mod, tmp_path):
    tree = tmp_path / "tree"
    (tree / "__pycache__").mkdir(parents=True)
    (tree / "__pycache__" / "m.pyc").write_text("junk")
    (tree / "m.py").write_text("VALUE = 1\n")
    before = gate_mod.tree_sha(tree)
    (tree / "__pycache__" / "m.pyc").write_text("other junk")
    assert gate_mod.tree_sha(tree) == before
    (tree / "m.py").write_text("VALUE = 2\n")
    assert gate_mod.tree_sha(tree) != before


# -- end-to-end: real gate processes on disposable trees ------------------

def test_run_promotes_passing_candidate(tmp_path):
    cand = make_candidate(tmp_path)
    rc, out = gate_run(tmp_path, cand, cand / "tests")
    assert rc == 0
    assert out["decision"] == "promote"
    assert out["outcome"] == "pass"
    assert out["qualifying"] is True
    assert out["import_origin_ok"] is True
    assert out["interpreter"] == sys.executable
    # promotion is bound to the identity: a matching check promotes
    rc, out = gate_check(tmp_path, cand, cand / "tests")
    assert rc == 0
    assert out["decision"] == "promote"
    assert out["production"] is True


def test_run_blocks_broken_candidate(tmp_path):
    cand = make_candidate(tmp_path, impl=BROKEN_ADDER)
    rc, out = gate_run(tmp_path, cand, cand / "tests")
    assert rc == 1
    assert out["decision"] == "block"
    assert out["outcome"] == "test_failure"
    rc, out = gate_check(tmp_path, cand, cand / "tests")
    assert rc == 1
    assert out["decision"] == "block"


def test_collect_only_and_help_never_promote(tmp_path):
    cand = make_candidate(tmp_path)
    rc, out = gate_run(tmp_path, cand, cand / "tests",
                       "--pytest-args", json.dumps(["-q", "--collect-only"]))
    assert rc == 1 and out["outcome"] == "collect_only"
    rc, out = gate_run(tmp_path, cand, cand / "tests",
                       "--pytest-args", json.dumps(["--help"]))
    assert rc == 1 and out["outcome"] == "help"


def test_empty_suite_blocks(tmp_path):
    cand = tmp_path / "candidate"
    (cand / "tests").mkdir(parents=True)
    (cand / "adder.py").write_text(CORRECT_ADDER)
    (cand / "tests" / "helper.py").write_text("# not a test file\n")
    rc, out = gate_run(tmp_path, cand, cand / "tests")
    assert rc == 1 and out["outcome"] == "empty_suite"


def test_all_skipped_suite_blocks(tmp_path):
    cand = tmp_path / "candidate"
    (cand / "tests").mkdir(parents=True)
    (cand / "adder.py").write_text(CORRECT_ADDER)
    (cand / "tests" / "test_skip.py").write_text(
        "import pytest\n\n@pytest.mark.skip(reason='not run')\n"
        "def test_skipped():\n    assert True\n"
    )
    rc, out = gate_run(tmp_path, cand, cand / "tests")
    assert rc == 1 and out["outcome"] == "all_skipped"


def test_timeout_blocks_and_is_not_a_test_result(tmp_path):
    cand = tmp_path / "candidate"
    (cand / "tests").mkdir(parents=True)
    (cand / "adder.py").write_text(CORRECT_ADDER)
    (cand / "tests" / "test_adder.py").write_text(HANG_TESTS)
    rc, out = gate_run(tmp_path, cand, cand / "tests", "--timeout", "5")
    assert rc == 1
    assert out["outcome"] == "timeout"
    assert out["pytest"] is None


def test_control_runs_cannot_authorize_production(tmp_path):
    cand = make_candidate(tmp_path)
    rc, out = gate_run(tmp_path, cand, cand / "tests", "--control")
    assert rc == 0 and out["decision"] == "promote"
    rc, out = gate_check(tmp_path, cand, cand / "tests")
    assert rc == 1 and out["reason"] == "control_only"
    rc, out = gate_check(tmp_path, cand, cand / "tests", "--inspect-control")
    assert rc == 0
    assert out["decision"] == "promote"
    assert out["production"] is False


def test_mutated_candidate_identity_mismatch(tmp_path):
    cand = make_candidate(tmp_path)
    rc, out = gate_run(tmp_path, cand, cand / "tests")
    assert rc == 0 and out["decision"] == "promote"
    (cand / "adder.py").write_text(CORRECT_ADDER + "# mutated\n")
    rc, out = gate_check(tmp_path, cand, cand / "tests")
    assert rc == 1 and out["reason"] == "identity_mismatch"


def test_external_oracle_is_staged_not_executed_in_place(tmp_path):
    ref = make_external_oracle(tmp_path, impl=CORRECT_ADDER)
    oracle = ref / "test_targetlib.py"

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "targetlib.py").write_text(BROKEN_ADDER)
    rc, out = gate_run(tmp_path, broken, oracle, candidate_id="ext-broken")
    assert rc != 0 and out["decision"] != "promote"
    rc, out = gate_check(tmp_path, broken, oracle, candidate_id="ext-broken")
    assert rc != 0

    correct = tmp_path / "correct"
    correct.mkdir()
    (correct / "targetlib.py").write_text(CORRECT_ADDER)
    rc, out = gate_run(tmp_path, correct, oracle, candidate_id="ext-correct")
    assert rc == 0
    assert out["decision"] == "promote"
    assert out["import_origin_ok"] is True


def test_inherited_pythonpath_cannot_shadow_the_candidate(tmp_path):
    ref = make_external_oracle(tmp_path, impl=CORRECT_ADDER)
    oracle = ref / "test_targetlib.py"
    cand = tmp_path / "candidate"
    cand.mkdir()
    (cand / "targetlib.py").write_text(BROKEN_ADDER)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ref)
    rc, out = gate_run(tmp_path, cand, oracle, env=env, candidate_id="shadow")
    assert rc != 0 and out["decision"] != "promote"


def test_reject_records_true_interpreter(tmp_path):
    rc, out = gate_cli(
        "reject",
        "--candidate-id", "noop",
        "--command", json.dumps(["/usr/bin/python3", "-c", "pass"]),
        "--python", sys.executable,
        log=tmp_path / "gate-log.jsonl",
    )
    assert rc == 1 and out["outcome"] == "no_op"
    assert out["interpreter"] == "/usr/bin/python3"
    rc, out = gate_cli(
        "reject",
        "--candidate-id", "mismatch",
        "--command", json.dumps(["/other/python", "-m", "pytest", "tests"]),
        "--python", sys.executable,
        log=tmp_path / "gate-log.jsonl",
    )
    assert rc == 1 and out["outcome"] == "interpreter_mismatch"


def test_missing_declared_interpreter_blocks(tmp_path):
    cand = make_candidate(tmp_path)
    rc, out = gate_run(tmp_path, cand, cand / "tests", "--python", "/nonexistent/python")
    assert rc == 1
    assert out["outcome"] == "pinned_interpreter_missing"


def test_tblite_placeholder_is_always_not_run_and_blocks(tmp_path):
    log = tmp_path / "gate-log.jsonl"
    for argv in ([], ["some-candidate"]):
        proc = subprocess.run(
            [sys.executable, str(TBLITE), *argv, "--log", str(log)],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode != 0
        payload = json.loads(proc.stdout)
        assert payload["decision"] == "block"
        assert payload["status"] == "NOT_RUN"
    records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert len(records) == 2
    assert all(r["outcome"] == "not_run" and r["decision"] == "block" for r in records)
