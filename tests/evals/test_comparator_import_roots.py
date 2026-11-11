"""Execution import-root contract tests for the comparator (public).

All cases run the PUBLISHED composer (evals/comparator/compare_products.py)
through the PUBLISHED T4 gate (evals/benchmark_gates/gate1_pytest.py) with
real staged candidates and observed import origins -- no mocks of gate
execution, no AST-guessed provenance. Covers: src-layout reference vs flat
candidate (and the reverse) on the same behavioral contract; selected-source
mutation measured as product failure; an installed/inherited decoy outside
the selected root cannot substitute; missing or escaping roots fail closed;
full-project drift still blocks; and an execution-policy-only change
invalidates a stale judge binding even when easy behavior scores coincide.
ASCII only.
"""

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
    def get(self, k, v=None):
        return self._d[k]
    def count(self):
        return len(self._d)
    def clear(self):
        self._d = {}
"""

BROKEN_BOX = GOOD_BOX.replace("return len(self._d)", "return len(self._d) + 1")


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


PATH_AWARE_PROBE = """
import json
import os
import sys

sys.path.insert(0, sys.argv[1])
root = os.path.realpath(sys.argv[1])
r = {}
try:
    import box as boxmod
    from box import Box
    origin_ok = os.path.realpath(getattr(boxmod, "__file__", "")).startswith(root)
    b = Box()
    b.put("a", 1)
    r["put_get"] = (b.get("a") == 1) and origin_ok
    b.put("b", 2)
    r["count"] = (b.count() == 2) and origin_ok
    b.clear()
    r["clear"] = (b.count() == 0) and origin_ok
except BaseException as exc:  # health: allow BLE001 -- probe records failure data
    r["harness_error"] = "error:" + type(exc).__name__
print("PROBE_RESULT " + json.dumps(r))
"""


def write_product(root, source, layout):
    """layout 'flat' -> box.py at root; 'src' -> box.py under src/."""
    d = root / "src" if layout == "src" else root
    d.mkdir(parents=True, exist_ok=True)
    (d / "box.py").write_text(source)
    (root / "README.md").write_text("product readme\n")
    return d


def make_cfg(tmp_path, name, orig, cand, orig_root=".", cand_root=".",
             probe=None):
    og = tmp_path / (name + "-og.json")
    og.write_text(json.dumps(box_graph()))
    ident_keys = {
        "original_dir": orig,
        "candidate_dir": cand,
        "oracle_tests": FIXTURE / "oracle",
        "probe_script": probe,
        "requirements": FIXTURE / "requirements-box.json",
        "original_graph": og,
        "candidate_graph": og,
    }
    import compare_products as cp
    ident = {k: cp.tree_sha(v) for k, v in ident_keys.items()}
    cfg = {
        "original_dir": str(orig),
        "candidate_dir": str(cand),
        "oracle_tests": str(FIXTURE / "oracle"),
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
    }
    p = tmp_path / (name + ".json")
    p.write_text(json.dumps(cfg))
    return p


def run_composer(config, out, judge=None):
    cmd = [sys.executable, str(COMPARATOR / "compare_products.py"),
           "--config", str(config), "--out", str(out)]
    if judge:
        cmd += ["--judge-response", str(judge)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    try:
        composed = json.loads(proc.stdout)
    except ValueError:
        composed = {"parse_error": proc.stdout[-300:], "stderr": proc.stderr[-300:]}
    receipt = out / "comparison-receipt.json"
    detail = json.loads(receipt.read_text()) if receipt.exists() else {}
    return proc.returncode, composed, detail


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


@pytest.fixture()
def probe(tmp_path):
    p = tmp_path / "probe_path.py"
    p.write_text(PATH_AWARE_PROBE)
    return p


def test_src_reference_vs_flat_candidate_composes(tmp_path, probe):
    original = tmp_path / "original"
    candidate = tmp_path / "candidate"
    write_product(original, GOOD_BOX, "src")
    write_product(candidate, GOOD_BOX, "flat")
    cfg = make_cfg(tmp_path, "fwd", original, candidate,
                   orig_root="src", cand_root=".", probe=probe)
    _rc, _comp, det = run_composer(cfg, tmp_path / "p1")
    judge = tmp_path / "judge.json"
    judge.write_text(json.dumps(valid_judge(det.get("input_bundle_sha256"))))
    rc, comp, det = run_composer(cfg, tmp_path / "p2", judge)
    assert rc == 0 and comp["grade"]["candidate"]["test"] == 1.0
    proj = det["execution_projection"]
    assert proj["original"]["import_root"] == "src"
    assert proj["original"]["execution_dir"].endswith("/original/src")
    assert proj["candidate"]["import_root"] == "."
    assert proj["candidate"]["execution_dir"].endswith("/candidate")
    # path-aware probes imported the SELECTED code, not something else
    for label in ("original", "candidate"):
        results = det["probes"][label]["results"]
        assert results == {"put_get": True, "count": True, "clear": True}
    # the gate staged the selected source dirs (origin proven, both green)
    for label in ("reference", "candidate"):
        assert det["oracle"][label]["outcome"] == "pass"
        assert det["oracle"][label]["import_origins"]["good"]


def test_flat_reference_vs_src_candidate_composes(tmp_path, probe):
    original = tmp_path / "original"
    candidate = tmp_path / "candidate"
    write_product(original, GOOD_BOX, "flat")
    write_product(candidate, GOOD_BOX, "src")
    cfg = make_cfg(tmp_path, "rev", original, candidate,
                   orig_root=".", cand_root="src", probe=probe)
    _rc, _comp, det = run_composer(cfg, tmp_path / "p1")
    judge = tmp_path / "judge.json"
    judge.write_text(json.dumps(valid_judge(det.get("input_bundle_sha256"))))
    rc, comp, _det = run_composer(cfg, tmp_path / "p2", judge)
    assert rc == 0 and comp["grade"]["candidate"]["test"] == 1.0


def test_selected_source_mutation_is_measured_failure(tmp_path, probe):
    original = tmp_path / "original"
    candidate = tmp_path / "candidate"
    write_product(original, GOOD_BOX, "src")
    write_product(candidate, BROKEN_BOX, "src")
    cfg = make_cfg(tmp_path, "mut", original, candidate,
                   orig_root="src", cand_root="src", probe=probe)
    _rc, comp, det = run_composer(cfg, tmp_path / "p1")
    assert det["oracle"]["reference"]["outcome"] == "pass"
    assert det["oracle"]["candidate"]["outcome"] == "test_failure"
    assert comp.get("grade") is None or \
        comp["grade"]["candidate"]["test"] < 1.0
    # genuine partial failures stay measured: only the broken behavior fails
    cand_ids = det["test_identities"]["candidate"]
    by_case = {k.rsplit("::", 1)[1]: s for k, s in cand_ids.items()}
    assert by_case["test_put_get"] == "pass"
    assert by_case["test_count"] == "fail"
    assert by_case["test_clear"] == "fail"


def test_decoy_outside_selected_root_cannot_substitute(tmp_path, probe):
    """A GOOD box.py at the project root cannot answer for a BROKEN one
    inside the selected src/ root: gate and probes both execute the
    selected source directory only."""
    original = tmp_path / "original"
    candidate = tmp_path / "candidate"
    write_product(original, GOOD_BOX, "src")
    write_product(candidate, BROKEN_BOX, "src")
    # installed/inherited-style decoy: good copy at the project root
    (candidate / "box.py").write_text(GOOD_BOX)
    cfg = make_cfg(tmp_path, "decoy", original, candidate,
                   orig_root="src", cand_root="src", probe=probe)
    _rc, comp, det = run_composer(cfg, tmp_path / "out")
    assert det["oracle"]["candidate"]["outcome"] == "test_failure"
    assert comp.get("grade") is None
    # the path-aware probe measured the broken selected code
    assert det["probes"]["candidate"]["results"]["count"] is False


@pytest.mark.parametrize("bad_root", ["../original", "src/..", "/etc",
                                      "no_such_dir", "~/src"])
def test_missing_or_escaping_roots_fail_closed(tmp_path, probe, bad_root):
    original = tmp_path / "original"
    candidate = tmp_path / "candidate"
    write_product(original, GOOD_BOX, "src")
    write_product(candidate, GOOD_BOX, "src")
    cfg = make_cfg(tmp_path, "bad" + str(len(bad_root)), original, candidate,
                   orig_root="src", cand_root=bad_root, probe=probe)
    rc, comp, det = run_composer(cfg, tmp_path / "out")
    assert rc != 0 and comp.get("grade") is None
    if not det:
        return  # config rejected before any receipt (SystemExit)
    proj = det.get("execution_projection", {})
    assert proj.get("candidate", {}).get("import_root") != bad_root or rc == 2


def test_root_escape_via_symlink_fails_closed(tmp_path, probe):
    original = tmp_path / "original"
    candidate = tmp_path / "candidate"
    write_product(original, GOOD_BOX, "src")
    write_product(candidate, GOOD_BOX, "src")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "box.py").write_text(GOOD_BOX)
    (candidate / "link").symlink_to(outside, target_is_directory=True)
    cfg = make_cfg(tmp_path, "sym", original, candidate,
                   orig_root="src", cand_root="link", probe=probe)
    rc, comp, _det = run_composer(cfg, tmp_path / "out")
    assert rc != 0 and comp.get("grade") is None


def test_full_project_drift_still_blocks(tmp_path, probe):
    mut = tmp_path / "probe_drift.py"
    mut.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from box import Box\n"
        "b = Box(); b.put('a', 1); b.clear()\n"
        "p = Path(sys.argv[1]) / 'box.py'\n"
        "p.write_text(p.read_text() + '\\n# mutated\\n')\n"
        'print("PROBE_RESULT " + json.dumps('
        '{"put_get": True, "count": True, "clear": True}))\n'
    )
    original = tmp_path / "original"
    candidate = tmp_path / "candidate"
    write_product(original, GOOD_BOX, "src")
    write_product(candidate, GOOD_BOX, "src")
    cfg = make_cfg(tmp_path, "drift", original, candidate,
                   orig_root="src", cand_root="src", probe=mut)
    rc, comp, _det = run_composer(cfg, tmp_path / "out")
    assert rc == 2
    assert any("identity_drift_during_run" in x
               for x in comp.get("block_reasons", []))


def test_execution_policy_change_invalidates_stale_judge(tmp_path, probe):
    """Identical products and easy scores, different execution policy: the
    bundle sha changes, so a judge bound to the old root is rejected."""
    original = tmp_path / "original"
    candidate = tmp_path / "candidate"
    write_product(original, GOOD_BOX, "src")
    # candidate has BOTH: selected src/ code and an equally-good root copy,
    # so behavior scores coincide under either root
    write_product(candidate, GOOD_BOX, "src")
    (candidate / "box.py").write_text(GOOD_BOX)
    cfg_src = make_cfg(tmp_path, "polA", original, candidate,
                       orig_root="src", cand_root="src", probe=probe)
    cfg_root = make_cfg(tmp_path, "polB", original, candidate,
                        orig_root="src", cand_root=".", probe=probe)
    _rc, _comp, det_a = run_composer(cfg_src, tmp_path / "pa")
    sha_src = det_a.get("input_bundle_sha256")
    _rc, _comp, det_b = run_composer(cfg_root, tmp_path / "pb")
    sha_root = det_b.get("input_bundle_sha256")
    assert sha_src and sha_root and sha_src != sha_root
    # a judge minted for the src-root run cannot certify the root run
    judge = tmp_path / "judge.json"
    judge.write_text(json.dumps(valid_judge(sha_src)))
    rc, comp, _det = run_composer(cfg_root, tmp_path / "pc", judge)
    assert rc == 2
    assert any("judge_bundle_mismatch" in x
               for x in comp.get("block_reasons", []))
