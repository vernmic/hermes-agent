"""Trusted oracle-manifest staging contract tests (public).

All cases execute the PUBLISHED gate (evals/benchmark_gates/
gate1_pytest.py) with real staged candidates, observed runtime module
origins and verified allowlisted oracle resources -- no mocks, no
AST-guessed provenance, no private machine paths. Covers the
oracle-staging/1 mode: helper packages and out-of-tree data preserved;
production target deliberately absent from the oracle copy; missing/
mismatched/unsafe manifest assets fail closed BEFORE tests run; a changed
manifest policy invalidates check authorization; a copied reference
target cannot rescue a broken or missing candidate implementation; an
unrelated imported helper cannot certify a missing declared target;
src/flat execution-root variants compose with an external oracle; and
the historical broken-candidate / reference-shadow negatives still block
under the new mode. ASCII only.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
COMPARATOR = HERE.parent.parent / "evals" / "comparator"
GATE = HERE.parent.parent / "evals" / "benchmark_gates" / "gate1_pytest.py"
FIXTURE = COMPARATOR / "fixtures" / "box-fixture"
sys.path.insert(0, str(COMPARATOR))

GOOD_BOX = """
class Box:
    def __init__(self):
        self._d = {}
    def put(self, k, v):
        self._d[k] = v
    def get(self, k):
        return self._d[k]
    def count(self):
        return len(self._d)
    def clear(self):
        self._d = {}
"""

BROKEN_BOX = GOOD_BOX.replace("return len(self._d)", "return len(self._d) + 1")

# get() is broken: every oracle assertion touching the target really fails
RESCUE_BROKEN_BOX = GOOD_BOX.replace("return self._d[k]", "return None")

# oracle suite: a PACKAGE (needs __init__.py) whose tests also read an
# out-of-tree data file relative to the oracle project root
ORACLE_INIT = "VALUE = 42\n"

ORACLE_TEST = """
import json
from pathlib import Path

import box

from . import VALUE


def test_mixin_value():
    assert VALUE == 42


def test_data_file():
    data = json.loads(
        (Path(__file__).parent.parent / "benchmark" / "data.json").read_text()
    )
    assert data["shape"] == "box"


def test_box():
    b = box.Box()
    b.put("a", 1)
    assert b.get("a") == 1
"""

DATA_JSON = '{"shape": "box"}\n'

PROBE = """
import json
import sys

sys.path.insert(0, sys.argv[1])
r = {}
try:
    from box import Box

    b = Box()
    b.put("a", 1)
    r["put_get"] = b.get("a") == 1
    r["count"] = b.count() == 1
    b.clear()
    r["clear"] = b.count() == 0
except BaseException as exc:  # health: allow BLE001 -- recorded for blocking
    r["harness_error"] = "error:" + type(exc).__name__
print("PROBE_RESULT " + json.dumps(r))
"""


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def box_graph():
    def node(nid, label, loc):
        return {"id": nid, "label": label, "source_file": "box.py",
                "source_location": loc, "file_type": "code",
                "confidence": "EXTRACTED"}
    nodes = [node("mod_box", "box", "L2"), node("cls_box", "Box", "L2"),
             node("m_put", "Box.put", "L5"), node("m_count", "Box.count", "L9"),
             node("m_clear", "Box.clear", "L11")]
    edges = [{"source": "mod_box", "target": "cls_box", "relation": "contains"},
             {"source": "cls_box", "target": "m_put", "relation": "contains"},
             {"source": "m_put", "target": "m_count",
              "relation": "shares_data_with"},
             {"source": "cls_box", "target": "m_clear", "relation": "contains"}]
    return {"directed": True, "nodes": nodes, "links": edges}


def make_oracle(tmp_path, test_body=ORACLE_TEST, data=DATA_JSON):
    oracle = tmp_path / "oracle"
    (oracle / "tests").mkdir(parents=True)
    (oracle / "benchmark").mkdir()
    (oracle / "tests" / "__init__.py").write_text(ORACLE_INIT)
    (oracle / "tests" / "test_box.py").write_text(test_body)
    (oracle / "benchmark" / "data.json").write_text(data)
    return oracle


def make_manifest(tmp_path, oracle, files=None, targets=("box",),
                  name="manifest"):
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    files = files or {
        "tests/__init__.py": sha(oracle / "tests" / "__init__.py"),
        "tests/test_box.py": sha(oracle / "tests" / "test_box.py"),
        "benchmark/data.json": sha(oracle / "benchmark" / "data.json"),
    }
    man = tmp_path / (name + ".json")
    man.write_text(json.dumps({
        "schema": "oracle-staging/1",
        "oracle_project_root": str(oracle),
        "tests_path": "tests",
        "files": files,
        "target_modules": list(targets),
    }))
    return man


def run_gate(candidate, tests, manifest, work, cid="ctl", python=None):
    log = work / "gate.jsonl"
    cmd = [sys.executable, str(GATE), "run", "--candidate-id", cid,
           "--candidate", str(candidate), "--tests", str(tests),
           "--python", str(python or sys.executable),
           "--log", str(log), "--logs-dir", str(work)]
    if manifest:
        cmd += ["--oracle-manifest", str(manifest)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                          check=False)
    payload = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") \
        else {"raw": proc.stdout[-300:]}
    records = []
    if log.exists():
        records = [json.loads(l) for l in log.read_text().splitlines()
                   if l.strip()]
    return proc.returncode, payload, records


def run_composer(config, out, judge=None):
    cmd = [sys.executable, str(COMPARATOR / "compare_products.py"),
           "--config", str(config), "--out", str(out)]
    if judge:
        cmd += ["--judge-response", str(judge)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    try:
        composed = json.loads(proc.stdout)
    except ValueError:
        composed = {"parse_error": proc.stdout[-300:],
                    "stderr": proc.stderr[-300:]}
    receipt = out / "comparison-receipt.json"
    detail = json.loads(receipt.read_text()) if receipt.exists() else {}
    return proc.returncode, composed, detail


def make_cfg(tmp_path, name, original, candidate, manifest, probe=None,
             orig_root=".", cand_root=".", oracle_tests=None):
    """In manifest mode, oracle_tests must BE the manifest's oracle
    project tests directory (the gate enforces the equality; the composer
    identity freezes that exact source)."""
    if oracle_tests is None:
        oracle_tests = Path(json.loads(manifest.read_text())
                            ["oracle_project_root"]) / "tests"
    og = tmp_path / (name + "-graph.json")
    og.write_text(json.dumps(box_graph()))
    probe = probe or (tmp_path / "probe.py")
    if not probe.exists():
        probe.write_text(PROBE)
    import compare_products as cp
    ident_keys = {
        "original_dir": original,
        "candidate_dir": candidate,
        "oracle_tests": oracle_tests,
        "probe_script": probe,
        "requirements": FIXTURE / "requirements-box.json",
        "original_graph": og,
        "candidate_graph": og,
        "oracle_manifest": manifest,
    }
    ident = {k: cp.tree_sha(v) for k, v in ident_keys.items()}
    cfg = {
        "original_dir": str(original),
        "candidate_dir": str(candidate),
        "oracle_tests": str(oracle_tests),
        "probe_script": str(probe),
        "requirements": str(FIXTURE / "requirements-box.json"),
        "product_module": "box",
        "original_graph": str(og),
        "candidate_graph": str(og),
        "gate_script": str(GATE),
        "pinned_python": sys.executable,
        "expected_probe_keys": ["put_get", "count", "clear"],
        "expected_identities": ident,
        "original_import_root": orig_root,
        "candidate_import_root": cand_root,
        "oracle_manifest": str(manifest),
    }
    p = tmp_path / (name + "-cfg.json")
    p.write_text(json.dumps(cfg))
    return p


def valid_judge(bundle_sha):
    return {
        "judge_score": 0.9, "original_score": 0.8,
        "rationale": "both products implement the frozen contract with"
                     " cited files and executed probes",
        "mandatory_status": {"B1": "met", "B2": "met", "B3": "met"},
        "qualitative": "better", "confidence": "medium",
        "evidence": ["box.py"], "strengths": ["s"], "omissions": ["o"],
        "tradeoffs": ["t"], "contamination": "none",
        "input_bundle_sha256": bundle_sha,
    }


def flat_product(root, source):
    root.mkdir(parents=True, exist_ok=True)
    (root / "box.py").write_text(source)
    return root


def src_product(root, source):
    d = root / "src"
    d.mkdir(parents=True, exist_ok=True)
    (d / "box.py").write_text(source)
    return root


# -- staging fidelity ----------------------------------------------------


def test_helper_package_and_data_preserved_flat(tmp_path):
    oracle = make_oracle(tmp_path)
    cand = flat_product(tmp_path / "cand", GOOD_BOX)
    man = make_manifest(tmp_path, oracle)
    _rc, payload, recs = run_gate(cand, oracle / "tests", man,
                                  tmp_path, "pkgdata")
    assert payload["decision"] == "promote", payload
    assert payload["pytest"]["passed"] == 3
    rec = [r for r in recs if r.get("candidate_id") == "pkgdata"][-1]
    assert rec["oracle_manifest"]["files_count"] == 3
    targets = rec["import_origins"]["targets"]
    assert targets and all(t["origin"] == "candidate" for t in targets)


def test_helper_package_and_data_preserved_src(tmp_path):
    oracle = make_oracle(tmp_path)
    cand = src_product(tmp_path / "cand", GOOD_BOX)
    man = make_manifest(tmp_path, oracle)
    _rc, payload, _ = run_gate(cand / "src", oracle / "tests", man,
                               tmp_path, "srcroot")
    assert payload["decision"] == "promote", payload


def test_old_mode_still_fails_without_helpers(tmp_path):
    """The exact historical gap: name-filtered staging drops the package
    __init__/helpers, so the SAME suite fails collection in the default
    mode -- the manifest mode is what makes it faithful, not a weaker
    import proof."""
    oracle = make_oracle(tmp_path)
    cand = flat_product(tmp_path / "cand", GOOD_BOX)
    _rc, payload, _ = run_gate(cand, oracle / "tests", None,
                               tmp_path, "oldmode")
    assert payload["decision"] == "block"
    assert payload["outcome"] in ("test_failure", "empty_suite",
                                  "command_error", "usage_error")


def test_production_target_omitted_from_oracle_copy(tmp_path):
    """The oracle copy contains ONLY tests+data: an implementation file
    placed in the oracle project but NOT allowlisted is never staged."""
    oracle = make_oracle(tmp_path)
    (oracle / "box.py").write_text(GOOD_BOX)  # NOT in the allowlist
    cand = flat_product(tmp_path / "cand", RESCUE_BROKEN_BOX)
    man = make_manifest(tmp_path, oracle)
    _rc, payload, _ = run_gate(cand, oracle / "tests", man,
                               tmp_path, "omit-prod")
    assert payload["decision"] == "block"
    assert payload["outcome"] == "test_failure"


# -- fail-closed manifest validation ------------------------------------


def _invalid_manifest_cases(tmp_path):
    oracle = make_oracle(tmp_path)
    cand = flat_product(tmp_path / "cand", GOOD_BOX)
    make_manifest(tmp_path / "valid", oracle, name="m0")
    # missing file
    m1 = make_manifest(tmp_path / "c1", oracle, name="m1")
    doc = json.loads(m1.read_text())
    doc["files"]["tests/not_there.py"] = "0" * 64
    m1.write_text(json.dumps(doc))
    # hash mismatch: helper changed after manifest minted
    m2 = make_manifest(tmp_path / "c2", oracle, name="m2")
    doc = json.loads(m2.read_text())
    doc["files"]["tests/__init__.py"] = "1" * 64
    m2.write_text(json.dumps(doc))
    # traversal
    m3 = make_manifest(tmp_path / "c3", oracle, name="m3")
    doc = json.loads(m3.read_text())
    doc["files"]["../escape.py"] = sha(cand / "box.py")
    m3.write_text(json.dumps(doc))
    # absolute path
    m4 = make_manifest(tmp_path / "c4", oracle, name="m4")
    doc = json.loads(m4.read_text())
    doc["files"][str(cand / "box.py")] = sha(cand / "box.py")
    m4.write_text(json.dumps(doc))
    # escaping symlink
    m5 = make_manifest(tmp_path / "c5", oracle, name="m5")
    (oracle / "link.py").symlink_to(cand / "box.py")
    doc = json.loads(m5.read_text())
    doc["files"]["link.py"] = sha(cand / "box.py")
    m5.write_text(json.dumps(doc))
    # duplicate keys in raw JSON
    m6 = tmp_path / "c6.json"
    m6.write_text(
        '{"schema": "oracle-staging/1", "schema": "oracle-staging/1",'
        ' "oracle_project_root": "' + str(oracle) + '",'
        ' "tests_path": "tests", "files": {}, "target_modules": ["box"]}'
    )
    return oracle, cand, [(m1, "missing"), (m2, "mismatch"), (m3, "traversal"),
                          (m4, "absolute"), (m5, "symlink"), (m6, "duplicate")]


@pytest.mark.parametrize("which", ["missing", "mismatch", "traversal",
                                   "absolute", "symlink", "duplicate"])
def test_invalid_manifests_block_before_execution(tmp_path, which):
    oracle, cand, cases = _invalid_manifest_cases(tmp_path)
    man = next(m for m, w in cases if w == which)
    _rc, payload, recs = run_gate(cand, oracle / "tests", man,
                                 tmp_path / ("run-" + which),
                                 "inv-" + which)
    assert payload["decision"] == "block"
    assert payload["outcome"] == "oracle_manifest_invalid"
    # fail-closed BEFORE tests: no pytest was executed under this id
    assert not [r for r in recs if r.get("candidate_id") == "inv-" + which
                and r.get("pytest")]


# -- authorization binding -----------------------------------------------


def test_changed_policy_invalidates_check(tmp_path):
    oracle = make_oracle(tmp_path)
    cand = flat_product(tmp_path / "cand", GOOD_BOX)
    man = make_manifest(tmp_path, oracle, name="auth1")
    _rc, payload, _ = run_gate(cand, oracle / "tests", man,
                              tmp_path / "run", "auth")
    assert payload["decision"] == "promote"
    # mutate a helper inside the oracle project and mint a NEW manifest:
    # the prior authorization cannot certify the changed policy
    (oracle / "tests" / "__init__.py").write_text("VALUE = 43\n")
    man2 = make_manifest(tmp_path, oracle, name="auth2")
    proc = subprocess.run(
        [sys.executable, str(GATE), "check", "--candidate-id", "auth",
         "--candidate", str(cand), "--tests", str(oracle / "tests"),
         "--oracle-manifest", str(man2), "--log",
         str(tmp_path / "run" / "gate.jsonl")],
        capture_output=True, text=True, timeout=120, check=False)
    checked = json.loads(proc.stdout)
    assert checked["decision"] == "block"
    assert checked["reason"] == "identity_mismatch"


def test_changed_manifest_rejects_judge_in_composer(tmp_path):
    oracle = make_oracle(tmp_path)
    original = flat_product(tmp_path / "original", GOOD_BOX)
    candidate = flat_product(tmp_path / "candidate", GOOD_BOX)
    man = make_manifest(tmp_path, oracle, name="judge1")
    cfg = make_cfg(tmp_path, "jm", original, candidate, man)
    _rc, _comp, det = run_composer(cfg, tmp_path / "p1")
    judge = tmp_path / "judge.json"
    judge.write_text(json.dumps(valid_judge(det.get("input_bundle_sha256"))))
    rc, comp, _ = run_composer(cfg, tmp_path / "p2", judge)
    assert rc == 0 and comp["grade"]["candidate"]["test"] == 1.0
    # change the oracle helper + mint a new manifest: bundle changes, the
    # old judge authorization is stale even though behavior scores coincide
    (oracle / "tests" / "__init__.py").write_text("VALUE = 43\n")
    oracle.parent.mkdir(parents=True, exist_ok=True)
    man2 = make_manifest(tmp_path, oracle, name="judge2")
    cfg2 = make_cfg(tmp_path, "jm2", original, candidate, man2)
    _rc, _comp, det2 = run_composer(cfg2, tmp_path / "p3")
    assert det2["input_bundle_sha256"] != det["input_bundle_sha256"]
    rc, comp, _ = run_composer(cfg2, tmp_path / "p4", judge)
    assert rc == 2
    assert any("judge_bundle_mismatch" in x
               for x in comp.get("block_reasons", []))


# -- provenance negatives ------------------------------------------------


def test_reference_target_cannot_rescue_missing_candidate(tmp_path):
    """A reference implementation allowlisted into the oracle namespace
    may satisfy the tests' imports, but the declared target then resolves
    from the ORACLE namespace: block target_from_oracle."""
    oracle = make_oracle(tmp_path)
    (oracle / "box.py").write_text(GOOD_BOX)
    cand = tmp_path / "cand"           # NO implementation at all
    cand.mkdir()
    (cand / "README.md").write_text("candidate without implementation\n")
    man = make_manifest(tmp_path, oracle, files={
        "tests/__init__.py": sha(oracle / "tests" / "__init__.py"),
        "tests/test_box.py": sha(oracle / "tests" / "test_box.py"),
        "benchmark/data.json": sha(oracle / "benchmark" / "data.json"),
        "box.py": sha(oracle / "box.py"),
    })
    _rc, payload, recs = run_gate(cand, oracle / "tests", man,
                                  tmp_path, "rescue")
    rec = [r for r in recs if r.get("candidate_id") == "rescue"][-1]
    assert payload["decision"] == "block"
    assert any(x.startswith("target_from_oracle:box")
               for x in rec["import_origins"]["target_problems"])


def test_broken_candidate_measured_even_with_reference_present(tmp_path):
    oracle = make_oracle(tmp_path)
    (oracle / "box.py").write_text(GOOD_BOX)
    cand = flat_product(tmp_path / "cand", RESCUE_BROKEN_BOX)
    man = make_manifest(tmp_path, oracle, files={
        "tests/__init__.py": sha(oracle / "tests" / "__init__.py"),
        "tests/test_box.py": sha(oracle / "tests" / "test_box.py"),
        "benchmark/data.json": sha(oracle / "benchmark" / "data.json"),
        "box.py": sha(oracle / "box.py"),
    })
    _rc, payload, recs = run_gate(cand, oracle / "tests", man,
                                 tmp_path, "rescue2")
    assert payload["decision"] == "block"
    # the oracle copy shadows the broken candidate import: the target is
    # observed from the oracle namespace and can never certify the run
    assert any(x.startswith("target_from_oracle:box")
               for x in recs[-1]["import_origins"]["target_problems"])


def test_unrelated_helper_cannot_certify_missing_target(tmp_path):
    """Candidate-side helper import succeeds (good origins non-empty) but
    the declared target is never imported: target_missing blocks even
    though the old origin logic alone would have passed."""
    oracle = make_oracle(tmp_path, test_body="""
from . import VALUE


def test_mixin_value():
    assert VALUE == 42
""")
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "helper.py").write_text("X = 1\n")
    (cand / "conftest.py").write_text("import helper  # noqa: F401\n")
    man = make_manifest(tmp_path, oracle, files={
        "tests/__init__.py": sha(oracle / "tests" / "__init__.py"),
        "tests/test_box.py": sha(oracle / "tests" / "test_box.py"),
    })
    _rc, payload, recs = run_gate(cand, oracle / "tests", man,
                                 tmp_path, "unrelated")
    rec = [r for r in recs if r.get("candidate_id") == "unrelated"][-1]
    assert payload["decision"] == "block"
    assert "target_missing:box" in rec["import_origins"]["target_problems"]
    # the unrelated helper WAS observed as good candidate code
    assert any(o["module"] == "helper"
               for o in rec["import_origins"]["good"])


def test_shadow_regression_still_blocks_with_manifest(tmp_path):
    """The historical reference-shadow negative, re-run under the new
    manifest mode: a staged namesake of an already-imported site-packages
    module (pluggy) is upstream_shadow and blocks."""
    oracle = make_oracle(tmp_path, test_body="""
from . import VALUE


def test_shadow():
    import pluggy

    assert pluggy.__file__
""")
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "pluggy.py").write_text("# staged namesake\n")
    man = make_manifest(tmp_path, oracle, files={
        "tests/__init__.py": sha(oracle / "tests" / "__init__.py"),
        "tests/test_box.py": sha(oracle / "tests" / "test_box.py"),
    })
    _rc, payload, _ = run_gate(cand, oracle / "tests", man,
                               tmp_path, "shadow")
    assert payload["decision"] == "block"
    assert payload["import_origin_ok"] is False


def test_external_reference_never_promotes_broken(tmp_path):
    """The historical external-reference negative under the manifest
    mode: tests importing a module from the ORIGINAL oracle project (a
    file NOT in the allowlisted staged copy) is external_reference and
    blocks."""
    oracle = make_oracle(tmp_path)
    # trusted-looking sibling in the oracle project, NOT allowlisted
    (oracle / "sibling_ref.py").write_text("X = 1\n")
    body = (
        "import sys\n"
        "from . import VALUE  # noqa: E402\n"
        f"sys.path.insert(0, {str(oracle)!r})\n"
        "import sibling_ref  # noqa: E402\n"
        "\n\n"
        "def test_value():\n"
        "    assert VALUE == 42\n"
    )
    (oracle / "tests" / "test_box.py").write_text(body)
    cand = flat_product(tmp_path / "cand", GOOD_BOX)
    man = make_manifest(tmp_path, oracle, files={
        "tests/__init__.py": sha(oracle / "tests" / "__init__.py"),
        "tests/test_box.py": sha(oracle / "tests" / "test_box.py"),
        "benchmark/data.json": sha(oracle / "benchmark" / "data.json"),
    })
    _rc, payload, recs = run_gate(cand, oracle / "tests", man,
                                 tmp_path, "externref")
    assert payload["decision"] == "block"
    assert any(b["reason"] == "external_reference"
               for b in recs[-1]["import_origins"]["bad"])


# -- composer integration ------------------------------------------------


def test_composer_manifest_mode_end_to_end(tmp_path):
    oracle = make_oracle(tmp_path)
    original = src_product(tmp_path / "original", GOOD_BOX)
    candidate = flat_product(tmp_path / "candidate", GOOD_BOX)
    man = make_manifest(tmp_path, oracle)
    cfg = make_cfg(tmp_path, "e2e", original, candidate, man,
                   orig_root="src", cand_root=".")
    _rc, _comp, det = run_composer(cfg, tmp_path / "p1")
    judge = tmp_path / "judge.json"
    judge.write_text(json.dumps(valid_judge(det.get("input_bundle_sha256"))))
    rc, comp, det = run_composer(cfg, tmp_path / "p2", judge)
    assert rc == 0 and comp["grade"]["candidate"]["test"] == 1.0
    # manifest provenance recorded and frozen
    assert det["oracle_manifest"]["schema"] == "oracle-staging/1"
    assert det["identity_before"]["oracle_manifest"]
    assert det["identity_after"]["oracle_manifest"]
    # probes ran under the configured pinned interpreter, recorded
    for label in ("original", "candidate"):
        assert det["probes"][label]["interpreter"] == sys.executable
    # gate saw the manifest on both sides
    for label in ("reference", "candidate"):
        assert det["oracle"][label]["import_origin_ok"]


def test_composer_missing_manifest_file_fails_closed(tmp_path):
    oracle = make_oracle(tmp_path)
    original = flat_product(tmp_path / "original", GOOD_BOX)
    candidate = flat_product(tmp_path / "candidate", GOOD_BOX)
    man = make_manifest(tmp_path, oracle)
    cfg = make_cfg(tmp_path, "miss", original, candidate, man)
    man.unlink()
    rc, comp, _ = run_composer(cfg, tmp_path / "out")
    assert rc != 0 and comp.get("grade") is None


def test_composer_manifest_drift_blocks(tmp_path):
    oracle = make_oracle(tmp_path)
    original = flat_product(tmp_path / "original", GOOD_BOX)
    candidate = flat_product(tmp_path / "candidate", GOOD_BOX)
    man = make_manifest(tmp_path, oracle)
    cfg = make_cfg(tmp_path, "drift", original, candidate, man)
    # mutate the allowlisted helper AFTER identity freeze
    man_chg = json.loads(man.read_text())
    (oracle / "tests" / "__init__.py").write_text("VALUE = 45\n")
    man.write_text(json.dumps(man_chg))  # hashes now stale -> manifest invalid
    rc, comp, _ = run_composer(cfg, tmp_path / "out")
    assert rc != 0 and comp.get("grade") is None


# -- W02: strict per-namespace origin qualification ----------------------


def test_mixed_oracle_submodule_blocks(tmp_path):
    """Exact independent-review reproduction: a candidate package
    initializer extends __path__ into the allowlisted oracle namespace;
    the missing API is borrowed from oracle box/api.py. The run must
    BLOCK with a clear mixed-origin provenance reason, and the oracle
    submodule must not appear as candidate-good proof."""
    oracle = tmp_path / "oracle"
    (oracle / "box").mkdir(parents=True)
    (oracle / "tests").mkdir()
    (oracle / "box" / "api.py").write_text("def value():\n    return 42\n")
    (oracle / "tests" / "test_api.py").write_text(
        "from box.api import value\n\n\ndef test_value():\n"
        "    assert value() == 42\n")
    cand = tmp_path / "cand"
    (cand / "box").mkdir(parents=True)
    (cand / "box" / "__init__.py").write_text(
        "from pathlib import Path\n"
        "__path__.append(str(Path(__file__).parent.parent /"
        " \"_gate_oracle\" / \"box\"))\n")
    man = make_manifest(tmp_path, oracle, files={
        "box/api.py": sha(oracle / "box" / "api.py"),
        "tests/test_api.py": sha(oracle / "tests" / "test_api.py"),
    })
    _rc, payload, recs = run_gate(cand, oracle / "tests", man,
                                 tmp_path, "mixedoracle")
    rec = [r for r in recs if r.get("candidate_id") == "mixedoracle"][-1]
    assert payload["decision"] == "block"
    assert payload["import_origin_ok"] is False
    assert any(x.startswith("target_mixed_origin:box:box.api@oracle")
               for x in rec["import_origins"]["target_problems"])
    # the oracle implementation is never candidate-good proof
    assert "box.api" not in [g["module"]
                             for g in rec["import_origins"]["good"]]


def test_mixed_external_submodule_blocks(tmp_path):
    """Same shape, borrowing from OUTSIDE the staged tree entirely: the
    candidate initializer extends __path__ at an external directory. The
    external target submodule blocks qualification too."""
    oracle = tmp_path / "oracle"
    (oracle / "box").mkdir(parents=True)
    (oracle / "tests").mkdir()
    (oracle / "box" / "api.py").write_text("def value():\n    return 7\n")
    (oracle / "tests" / "test_api.py").write_text(
        "from box.api import value\n\n\ndef test_value():\n"
        "    assert value() == 7\n")
    extern = tmp_path / "external"
    (extern / "box").mkdir(parents=True)
    (extern / "box" / "api.py").write_text("def value():\n    return 7\n")
    cand = tmp_path / "cand"
    (cand / "box").mkdir(parents=True)
    (cand / "box" / "__init__.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "__path__.append(os.environ.get(\"W02_EXTERNAL_PKG\", \"\"))\n")
    man = make_manifest(tmp_path, oracle, files={
        "box/api.py": sha(oracle / "box" / "api.py"),
        "tests/test_api.py": sha(oracle / "tests" / "test_api.py"),
    })
    import os as _os
    env = dict(_os.environ, W02_EXTERNAL_PKG=str(extern / "box"))
    log = tmp_path / "gate.jsonl"
    cmd = [sys.executable, str(GATE), "run", "--candidate-id",
           "mixedext", "--candidate", str(cand),
           "--tests", str(oracle / "tests"), "--python", sys.executable,
           "--log", str(log), "--logs-dir", str(tmp_path),
           "--oracle-manifest", str(man)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                          check=False, env=env)
    payload = json.loads(proc.stdout)
    recs = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    rec = [r for r in recs if r.get("candidate_id") == "mixedext"][-1]
    assert payload["decision"] == "block"
    assert any("box.api" in x for x in rec["import_origins"]["target_problems"])


def test_complete_candidate_package_qualifies(tmp_path):
    """Positive control for the strict rule: the FULL candidate package
    (initializer + submodule, no borrowing) qualifies against the same
    oracle suite."""
    oracle = tmp_path / "oracle"
    (oracle / "box").mkdir(parents=True)
    (oracle / "tests").mkdir()
    (oracle / "box" / "api.py").write_text("def value():\n    return 42\n")
    (oracle / "tests" / "test_api.py").write_text(
        "from box.api import value\n\n\ndef test_value():\n"
        "    assert value() == 42\n")
    cand = tmp_path / "cand"
    (cand / "box").mkdir(parents=True)
    (cand / "box" / "__init__.py").write_text("")
    (cand / "box" / "api.py").write_text("def value():\n    return 42\n")
    man = make_manifest(tmp_path, oracle, files={
        "box/api.py": sha(oracle / "box" / "api.py"),
        "tests/test_api.py": sha(oracle / "tests" / "test_api.py"),
    })
    _rc, payload, recs = run_gate(cand, oracle / "tests", man,
                                 tmp_path, "fullpkg")
    rec = [r for r in recs if r.get("candidate_id") == "fullpkg"][-1]
    assert payload["decision"] == "promote", payload
    assert rec["import_origins"]["target_problems"] == []
    origins = {o["module"]: o["origin"] for o in rec["import_origins"]["targets"]}
    assert origins.get("box") == "candidate"
    assert origins.get("box.api") == "candidate"


def test_trusted_helper_package_still_allowed(tmp_path):
    """Trusted test/helper namespaces under the oracle namespace stay
    allowed outside the declared product: helper imports are neither
    target proof nor provenance failures."""
    oracle = tmp_path / "oracle"
    (oracle / "tests" / "helpers").mkdir(parents=True)
    (oracle / "tests" / "__init__.py").write_text("")
    (oracle / "tests" / "helpers" / "__init__.py").write_text("Z = 5\n")
    (oracle / "tests" / "test_x.py").write_text(
        "from .helpers import Z\n\nimport box\n\n\n"
        "def test_helper():\n    assert Z == 5\n\n\n"
        "def test_box():\n    assert box.Box is not None\n")
    cand = flat_product(tmp_path / "cand", GOOD_BOX)
    man = make_manifest(tmp_path, oracle, files={
        "tests/__init__.py": sha(oracle / "tests" / "__init__.py"),
        "tests/helpers/__init__.py": sha(oracle / "tests" / "helpers" / "__init__.py"),
        "tests/test_x.py": sha(oracle / "tests" / "test_x.py"),
    })
    _rc, payload, recs = run_gate(cand, oracle / "tests", man,
                                 tmp_path, "helperpkg")
    rec = [r for r in recs if r.get("candidate_id") == "helperpkg"][-1]
    assert payload["decision"] == "promote", payload
    assert rec["import_origins"]["target_problems"] == []
    helper_mods = [o for o in rec["import_origins"]["targets"]
                   if o["module"].startswith("tests")]
    assert not helper_mods  # helpers are not target namespaces


def _ns_oracle(tmp_path):
    """Oracle whose suite is a package (so the oracle root is importable)
    and whose product namespace nspkg has a fileless top package."""
    oracle = tmp_path / "oracle"
    (oracle / "nspkg").mkdir(parents=True)
    (oracle / "tests").mkdir()
    (oracle / "tests" / "__init__.py").write_text("")
    (oracle / "nspkg" / "impl.py").write_text("V = 1\n")
    (oracle / "tests" / "test_ns.py").write_text(
        "from nspkg.impl import V\n\n\ndef test_v():\n    assert V == 1\n")
    return oracle


def test_namespace_package_borrowed_impl_blocks(tmp_path):
    """A fileless namespace package can qualify ONLY through observed
    CANDIDATE submodules. With no candidate portion at all, the impl is
    borrowed from the oracle namespace and blocks."""
    oracle = _ns_oracle(tmp_path)
    cand = tmp_path / "cand"          # no nspkg portion of its own
    cand.mkdir()
    man = make_manifest(tmp_path, oracle, files={
        "nspkg/impl.py": sha(oracle / "nspkg" / "impl.py"),
        "tests/__init__.py": sha(oracle / "tests" / "__init__.py"),
        "tests/test_ns.py": sha(oracle / "tests" / "test_ns.py"),
    }, targets=("nspkg",))
    _rc, payload, recs = run_gate(cand, oracle / "tests", man,
                                 tmp_path, "nsborrow")
    rec = [r for r in recs if r.get("candidate_id") == "nsborrow"][-1]
    assert payload["decision"] == "block"
    assert any(x.startswith("target_from_oracle:nspkg")
               for x in rec["import_origins"]["target_problems"])


def test_namespace_package_candidate_submodule_qualifies(tmp_path):
    """Positive control: the same fileless-namespace oracle suite with a
    REAL candidate submodule qualifies through the observed candidate
    observation alone."""
    oracle = _ns_oracle(tmp_path)
    cand = tmp_path / "cand"
    (cand / "nspkg").mkdir(parents=True)   # fileless namespace portion
    (cand / "nspkg" / "impl.py").write_text("V = 1\n")
    # the oracle project still CONTAINS nspkg/impl.py, but a correct
    # manifest allowlists only the trusted suite, never the product --
    # the candidate submodule is then the only importable portion
    man = make_manifest(tmp_path, oracle, files={
        "tests/__init__.py": sha(oracle / "tests" / "__init__.py"),
        "tests/test_ns.py": sha(oracle / "tests" / "test_ns.py"),
    }, targets=("nspkg",))
    _rc, payload, recs = run_gate(cand, oracle / "tests", man,
                                 tmp_path, "nscand")
    rec = [r for r in recs if r.get("candidate_id") == "nscand"][-1]
    assert payload["decision"] == "promote", payload
    assert rec["import_origins"]["target_problems"] == []
    origins = {o["module"]: o["origin"]
               for o in rec["import_origins"]["targets"]}
    assert origins.get("nspkg.impl") == "candidate"
