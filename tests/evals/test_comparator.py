"""Portable comparator contract tests (public test contract).

Covers qualification (fail-closed on malformed/dangling/duplicate graph
structure), span-bound node identity (the L999 / BoxUnused /
never-defined negatives and the legitimate nested/alias anchors),
determinism across PYTHONHASHSEED, cited source fallback versus
unresolved-evidence blocking, denominator integrity (candidate skips never
earn full credit), immutability (identity drift), judge bundle binding,
and measured partial failures - all through the PUBLISHED benchmark gate
(evals/benchmark_gates/gate1_pytest.py, reviewed dependency) with staged
candidates and observed import origins. The external-reference and
inherited-shadow controls reproduce the independent R13 oracle regression
proof: a broken candidate whose tests import the reference module must
NEVER be promoted, and statically-guessed origins are not accepted.
No network, no private paths, no secrets, no mocks of gate execution.
ASCII only.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
COMPARATOR = HERE.parent.parent / "evals" / "comparator"
GATE = HERE.parent.parent / "evals" / "benchmark_gates" / "gate1_pytest.py"
sys.path.insert(0, str(COMPARATOR))
import compare_products as cp
import graph_compare as gc

FIXTURE = COMPARATOR / "fixtures" / "box-fixture"

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
MENTION_ONLY = """
# someday we might have a Box here
def nothing():
    return None
"""
PROBES_OK = {"put_get": True, "count": True, "clear": True}


def box_graph(extra_nodes=(), extra_edges=(), dup_labels=False):
    lab = "Box" if dup_labels else None
    nodes = [
        {
            "id": "mod_box",
            "label": "box",
            "source_file": "box.py",
            "source_location": "L2",
            "file_type": "code",
            "confidence": "EXTRACTED",
        },
        {
            "id": "cls_box",
            "label": "Box",
            "source_file": "box.py",
            "source_location": "L2",
            "file_type": "code",
            "confidence": "EXTRACTED",
        },
        {
            "id": "m_put",
            "label": lab or "Box.put",
            "source_file": "box.py",
            "source_location": "L5",
            "file_type": "code",
            "confidence": "EXTRACTED",
        },
        {
            "id": "m_count",
            "label": lab or "Box.count",
            "source_file": "box.py",
            "source_location": "L9",
            "file_type": "code",
            "confidence": "EXTRACTED",
        },
        {
            "id": "m_clear",
            "label": lab or "Box.clear",
            "source_file": "box.py",
            "source_location": "L11",
            "file_type": "code",
            "confidence": "EXTRACTED",
        },
        *extra_nodes,
    ]
    edges = [
        {"source": "mod_box", "target": "cls_box", "relation": "contains"},
        {"source": "cls_box", "target": "m_put", "relation": "contains"},
        {"source": "m_put", "target": "m_count", "relation": "shares_data_with"},
        {"source": "cls_box", "target": "m_clear", "relation": "contains"},
        *extra_edges,
    ]
    return {"directed": True, "nodes": nodes, "links": edges}


@pytest.fixture()
def good_product(tmp_path):
    d = tmp_path / "good"
    d.mkdir()
    (d / "box.py").write_text(GOOD_BOX)
    return d


@pytest.fixture()
def graph_file(tmp_path):
    def _write(name, graph):
        p = tmp_path / (name + ".json")
        p.write_text(json.dumps(graph))
        return p

    return _write


def compare_graph(graph_path, product, probes=None):
    return gc.compare(
        graph_path,
        product,
        FIXTURE / "requirements-box.json",
        probes if probes is not None else PROBES_OK,
    )


# -- span-bound node identity -------------------------------------------


def test_real_definition_corroborated_with_span(good_product, graph_file):
    res = compare_graph(graph_file("g", box_graph()), good_product)
    b1 = res["requirements"]["B1"]
    assert b1["verdict"] == "corroborated"
    anchor = b1["code_evidence"][0]["anchor"]
    assert anchor["name"] == "Box"
    assert anchor["span_agreement"] is True


def test_impossible_location_is_span_mismatch(good_product, graph_file):
    nodes = [
        {
            "id": "class_Box",
            "label": "Box",
            "source_file": "box.py",
            "source_location": "L999",
            "file_type": "code",
            "confidence": "EXTRACTED",
        }
    ]
    res = compare_graph(graph_file("l999", {"nodes": nodes, "links": []}), good_product)
    ctx = res["requirements"]["B1"]["context_evidence"]
    assert res["requirements"]["B1"]["verdict"] == "unverified_claim"
    assert ctx[0]["reason"] == "span_mismatch"


def test_substring_name_is_a_different_definition(good_product, graph_file):
    nodes = [
        {
            "id": "class_BoxUnused",
            "label": "BoxUnused",
            "source_file": "box.py",
            "source_location": "L13",
            "file_type": "code",
            "confidence": "EXTRACTED",
        }
    ]
    extra = GOOD_BOX + "\nclass BoxUnused:\n    pass\n"
    prod = good_product.parent / "with-unused"
    prod.mkdir()
    (prod / "box.py").write_text(extra)
    res = compare_graph(graph_file("unused", {"nodes": nodes, "links": []}), prod)
    ctx = res["requirements"]["B1"]["context_evidence"]
    assert ctx and ctx[0]["reason"] == "different_definition:BoxUnused"
    assert not res["requirements"]["B1"]["code_evidence"]


def test_nonexistent_member_never_verifies(good_product, graph_file):
    nodes = [
        {
            "id": "method_Box_never_defined",
            "label": "Box.never_defined",
            "source_file": "box.py",
            "source_location": "L999",
            "file_type": "code",
            "confidence": "EXTRACTED",
        }
    ]
    res = compare_graph(graph_file("nd", {"nodes": nodes, "links": []}), good_product)
    ctx = res["requirements"]["B1"]["context_evidence"]
    assert ctx[0]["reason"] == "unresolved_qualified_name_or_boundary"


def test_legitimate_nested_member_and_missing_span(good_product, graph_file):
    nested = [
        {
            "id": "method_Box_put",
            "label": "Box.put",
            "source_file": "box.py",
            "source_location": "L5",
            "file_type": "code",
            "confidence": "EXTRACTED",
        },
        {
            "id": "class_nospan",
            "label": "Box",
            "source_file": "box.py",
            "file_type": "code",
            "confidence": "EXTRACTED",
        },
    ]
    res = compare_graph(
        graph_file("nested", {"nodes": nested, "links": []}), good_product
    )
    b1 = res["requirements"]["B1"]
    names = [h["anchor"]["name"] for h in b1["code_evidence"]]
    assert "Box.put" in names
    reasons = {h["reason"] for h in b1["context_evidence"]}
    assert "span_absent" in reasons


def test_alias_and_factory_architectures_anchor(tmp_path, graph_file):
    prod = tmp_path / "factory"
    prod.mkdir()
    source = (
        "def make_box():\n    return _BoxImpl()\n\n\n"
        "class _BoxImpl:\n"
        "    def __init__(self):\n        self._d = {}\n"
        "    def put(self, k, v):\n        self._d[k] = v\n"
        "    def get(self, k):\n        return self._d[k]\n"
        "    def count(self):\n        return len(self._d)\n"
        "    def clear(self):\n        self._d = {}\n\n\n"
        "class Box:\n"
        "    def __init__(self):\n        self._impl = make_box()\n"
        "    def put(self, k, v):\n        self._impl.put(k, v)\n"
        "    def get(self, k):\n        return self._impl.get(k)\n"
        "    def count(self):\n        return self._impl.count()\n"
        "    def clear(self):\n        self._impl.clear()\n\n\n"
        "BoxAlias = Box\n"
    )
    (prod / "box.py").write_text(source)
    nodes = [
        {
            "id": "f_make",
            "label": "make_box factory",
            "source_file": "box.py",
            "source_location": "L1",
            "file_type": "code",
            "confidence": "EXTRACTED",
        },
        {
            "id": "f_box",
            "label": "Box facade",
            "source_file": "box.py",
            "source_location": "L18",
            "file_type": "code",
            "confidence": "EXTRACTED",
        },
        {
            "id": "a_box",
            "label": "BoxAlias",
            "source_file": "box.py",
            "source_location": "L31",
            "file_type": "code",
            "confidence": "EXTRACTED",
        },
    ]
    res = compare_graph(graph_file("fac", {"nodes": nodes, "links": []}), prod)
    b1 = res["requirements"]["B1"]
    assert b1["verdict"] == "corroborated"
    names = {h["anchor"]["name"] for h in b1["code_evidence"]}
    assert {"Box", "BoxAlias"} <= names


# -- deterministic, fail-closed qualification ---------------------------


def test_output_identical_across_hash_seeds(good_product, tmp_path, graph_file):
    g = graph_file("det", box_graph())
    outputs = []
    for seed in ("0", "1", "42"):
        proc = subprocess.run(
            [
                sys.executable,
                str(COMPARATOR / "graph_compare.py"),
                "--graph",
                str(g),
                "--product",
                str(good_product),
                "--requirements",
                str(FIXTURE / "requirements-box.json"),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=dict(os.environ, PYTHONHASHSEED=seed),
        )
        outputs.append(proc.stdout)
    assert outputs[0] == outputs[1] == outputs[2]


@pytest.mark.parametrize(
    "graph,error",
    [
        (
            {"nodes": box_graph()["nodes"], "links": "not a list"},
            "graph_links_not_list",
        ),
        ("[]", "graph_not_object"),
        (
            {
                "nodes": box_graph()["nodes"],
                "links": [{"source": "mod_box", "target": "MISSING", "relation": "x"}],
            },
            None,
        ),
        (
            {
                "nodes": [dict(box_graph()["nodes"][0]), dict(box_graph()["nodes"][0])],
                "links": [],
            },
            None,
        ),
    ],
)
def test_malformed_graphs_fail_closed(good_product, graph_file, graph, error):
    body = graph if isinstance(graph, str) else json.dumps(graph)
    p = good_product.parent / "malformed.json"
    p.write_text(body)
    res = gc.compare(p, good_product, FIXTURE / "requirements-box.json", {})
    assert res.get("qualified") is False
    assert res.get("error")
    if error:
        assert res["error"].startswith(error)


def test_duplicate_labels_join_by_id(good_product, graph_file):
    res = compare_graph(graph_file("dup", box_graph(dup_labels=True)), good_product)
    b1 = res["requirements"]["B1"]
    assert b1["verdict"] == "corroborated"
    assert b1["mapped_relationship_count"] > 0


# -- end-to-end composition through the generic gate --------------------


def make_cfg(tmp_path, name, original, candidate, probe, ograph, cgraph):
    ident = {
        k: cp.tree_sha(v)
        for k, v in {
            "original_dir": original,
            "candidate_dir": candidate,
            "oracle_tests": FIXTURE / "oracle",
            "probe_script": probe,
            "requirements": FIXTURE / "requirements-box.json",
            "original_graph": ograph,
            "candidate_graph": cgraph,
        }.items()
    }
    cfg = {
        "original_dir": str(original),
        "candidate_dir": str(candidate),
        "oracle_tests": str(FIXTURE / "oracle"),
        "probe_script": str(probe),
        "requirements": str(FIXTURE / "requirements-box.json"),
        "product_module": "box",
        "original_graph": str(ograph),
        "candidate_graph": str(cgraph),
        "gate_script": str(GATE),
        "pinned_python": sys.executable,
        "expected_probe_keys": ["put_get", "count", "clear"],
        "expected_identities": ident,
    }
    p = tmp_path / (name + ".json")
    p.write_text(json.dumps(cfg))
    return p


def run_composer(config, out, judge=None):
    cmd = [
        sys.executable,
        str(COMPARATOR / "compare_products.py"),
        "--config",
        str(config),
        "--out",
        str(out),
    ]
    if judge:
        cmd += ["--judge-response", str(judge)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    try:
        composed = json.loads(proc.stdout)
    except ValueError:
        composed = {"parse_error": proc.stdout[-300:]}
    receipt = out / "comparison-receipt.json"
    detail = json.loads(receipt.read_text()) if receipt.exists() else {}
    return proc.returncode, composed, detail


def valid_judge(bundle_sha):
    return {
        "judge_score": 0.9,
        "original_score": 0.8,
        "rationale": "both products implement the frozen contract with"
        " cited files and executed probes",
        "mandatory_status": {"B1": "met", "B2": "met", "B3": "met"},
        "qualitative": "better",
        "confidence": "medium",
        "evidence": ["box.py"],
        "strengths": ["s"],
        "omissions": ["o"],
        "tradeoffs": ["t"],
        "contamination": "none",
        "input_bundle_sha256": bundle_sha,
    }


def e2e_setup(tmp_path, candidate_source, cgraph=None, probe=None):
    original = tmp_path / "original"
    original.mkdir()
    (original / "box.py").write_text(GOOD_BOX)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "box.py").write_text(candidate_source)
    og = tmp_path / "o.json"
    og.write_text(json.dumps(box_graph()))
    cg = tmp_path / "c.json"
    cg.write_text(json.dumps(cgraph or box_graph()))
    probe = probe or (FIXTURE / "probe_box.py")
    return make_cfg(tmp_path, "cfg", original, candidate, probe, og, cg), og


def test_doc_only_graph_composes_via_cited_fallback(tmp_path):
    doc = {
        "directed": True,
        "nodes": [
            {
                "id": "d1",
                "label": "Box.put stores",
                "source_file": "NOTES.md",
                "file_type": "document",
                "confidence": "INFERRED",
            },
            {
                "id": "d2",
                "label": "Box.count returns size",
                "source_file": "README.md",
                "file_type": "document",
                "confidence": "EXTRACTED",
            },
            {
                "id": "d3",
                "label": "Box.clear empties",
                "source_file": "README.md",
                "file_type": "document",
                "confidence": "EXTRACTED",
            },
        ],
        "links": [],
    }
    cfg, _ = e2e_setup(tmp_path, GOOD_BOX, cgraph=doc)
    rc, comp, det = run_composer(cfg, tmp_path / "pass1")
    judge = tmp_path / "judge.json"
    judge.write_text(json.dumps(valid_judge(det.get("input_bundle_sha256"))))
    rc, comp, det = run_composer(cfg, tmp_path / "pass2", judge)
    st = det["structural"]["candidate"]["requirements"]
    fallbacks = [
        v for v in st.values() if v.get("status") == "supported_source_fallback"
    ]
    assert rc == 0 and comp["grade"]["candidate"]["structural"] == 1.0
    assert len(fallbacks) == 3
    assert all("source anchor" in v["fallback_reason"] for v in fallbacks)


def test_mention_only_product_blocks_unresolved(tmp_path):
    lying = tmp_path / "probe_lying.py"
    lying.write_text(
        'import json\nprint("PROBE_RESULT " + json.dumps('
        '{"put_get": True, "count": True, "clear": True}))\n'
    )
    doc = {
        "directed": True,
        "nodes": [
            {
                "id": "d1",
                "label": "Box.put stores",
                "source_file": "NOTES.md",
                "file_type": "document",
                "confidence": "INFERRED",
            },
            {
                "id": "d2",
                "label": "Box.count returns size",
                "source_file": "README.md",
                "file_type": "document",
                "confidence": "EXTRACTED",
            },
            {
                "id": "d3",
                "label": "Box.clear empties",
                "source_file": "README.md",
                "file_type": "document",
                "confidence": "EXTRACTED",
            },
        ],
        "links": [],
    }
    cfg, _ = e2e_setup(tmp_path, MENTION_ONLY, cgraph=doc, probe=lying)
    rc, comp, _ = run_composer(cfg, tmp_path / "out")
    assert rc == 2 and comp.get("grade") is None
    assert any(
        "capability_evidence_unresolved" in x for x in comp.get("block_reasons", [])
    )


def test_candidate_skip_never_earns_full_credit(tmp_path):
    skip_src = GOOD_BOX.replace(
        "def count(self):",
        "def count(self):\n        raise NotImplementedError('skip')",
    ).replace("import unittest\n", "")
    cfg, _ = e2e_setup(tmp_path, skip_src)
    _rc, comp, _det = run_composer(cfg, tmp_path / "out")
    assert comp.get("grade") is None or comp["grade"]["candidate"]["test"] < 1.0


def test_probe_harness_failure_blocks(tmp_path):
    exit7 = tmp_path / "probe_exit7.py"
    exit7.write_text(
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from box import Box\n"
        "b = Box(); b.put('a', 1); b.clear()\n"
        'print("PROBE_RESULT " + json.dumps('
        '{"put_get": True, "count": True, "clear": True}))\n'
        "sys.exit(7)\n"
    )
    cfg, _ = e2e_setup(tmp_path, GOOD_BOX, probe=exit7)
    rc, comp, _ = run_composer(cfg, tmp_path / "out")
    assert rc == 2
    assert any("probe_exit_7" in x for x in comp.get("block_reasons", []))


def test_identity_drift_blocks(tmp_path):
    mut = tmp_path / "probe_mut.py"
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
    cfg, _ = e2e_setup(tmp_path, GOOD_BOX, probe=mut)
    rc, comp, _ = run_composer(cfg, tmp_path / "out")
    assert rc == 2
    assert any("identity_drift_during_run" in x for x in comp.get("block_reasons", []))


def test_judge_bundle_binding(tmp_path):
    cfg, _ = e2e_setup(tmp_path, GOOD_BOX)
    rc, comp, det = run_composer(cfg, tmp_path / "p1")
    good = tmp_path / "judge.json"
    good.write_text(json.dumps(valid_judge(det.get("input_bundle_sha256"))))
    rc, comp, _ = run_composer(cfg, tmp_path / "p2", good)
    assert rc == 0 and comp["grade"]["candidate"]["composite"] == 0.98
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps(valid_judge("0" * 64)))
    rc, comp, _ = run_composer(cfg, tmp_path / "p3", stale)
    assert rc == 2
    assert any("judge_bundle_mismatch" in x for x in comp.get("block_reasons", []))


def test_partial_failure_stays_measured(tmp_path):
    broken = GOOD_BOX.replace("return len(self._d)", "return len(self._d) + 1")
    cfg, _ = e2e_setup(tmp_path, broken)
    rc, comp, det = run_composer(cfg, tmp_path / "p1")
    judge = tmp_path / "judge.json"
    j = valid_judge(det.get("input_bundle_sha256"))
    j.update(judge_score=0.5, original_score=0.8)
    judge.write_text(json.dumps(j))
    rc, comp, _ = run_composer(cfg, tmp_path / "p2", judge)
    assert rc == 0
    assert comp["grade"]["candidate"]["test"] == pytest.approx(0.3333, abs=1e-4)


# -- real-gate provenance controls (R14) --------------------------------


def run_gate_cli(candidate, tests, work, label="ctl"):
    import subprocess

    log = work / f"gate-{label}.jsonl"
    proc = subprocess.run(
        [
            sys.executable,
            str(GATE),
            "run",
            "--candidate-id",
            f"ctl-{label}",
            "--candidate",
            str(candidate),
            "--tests",
            str(tests),
            "--python",
            sys.executable,
            "--timeout",
            "120",
            "--pytest-args",
            json.dumps(["-q", "--no-header", "-p", "no:cacheprovider"]),
            "--label",
            label,
            "--control",
            "--log",
            str(log),
            "--logs-dir",
            str(work / f"logs-{label}"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    record = None
    if log.exists():
        for line in log.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                if rec.get("run_id") == payload.get("run_id"):
                    record = rec
    return payload, record


def test_gate_external_reference_never_promotes_broken(tmp_path):
    """Exact R13 regression fixture: tests sit NEXT TO the reference
    module; a broken candidate must fail on its own staged code, and the
    record must never list the unexecuted candidate module as provenance
    for a passing run."""
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "targetlib.py").write_text("def combine(a, b):\n    return a + b\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_targetlib.py").write_text(
        "from targetlib import combine\n\n"
        "def test_combine():\n    assert combine(2, 3) == 5\n"
    )
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "targetlib.py").write_text("def combine(a, b):\n    return a * b\n")

    ref_payload, ref_record = run_gate_cli(reference, tests, tmp_path, "ref")
    assert ref_payload["decision"] == "promote"
    assert ref_payload["outcome"] == "pass"
    assert ref_payload["import_origin_ok"] is True
    good_paths = [g["path"] for g in ref_record["import_origins"]["good"]]
    # tests executed against the STAGED reference, not the original tree
    assert any("gate1-stage" in g for g in good_paths)

    bad_payload, bad_record = run_gate_cli(broken, tests, tmp_path, "bad")
    assert bad_payload["decision"] == "block"
    assert bad_payload["outcome"] == "test_failure"
    assert bad_record["qualifying"] is False
    # measured product failure: origins still observed, never invented
    assert bad_record["import_origins"]["bad"] == []


def test_composer_blocks_external_reference_promotion(tmp_path):
    """Through the composer, the same broken candidate gets a MEASURED
    zero test score (never promotion), proving staged-origin fidelity."""
    reference = tmp_path / "original"
    reference.mkdir()
    (reference / "targetlib.py").write_text("def combine(a, b):\n    return a + b\n")
    tests = tmp_path / "oracle"
    tests.mkdir()
    (tests / "test_targetlib.py").write_text(
        "from targetlib import combine\n\n"
        "def test_combine():\n    assert combine(2, 3) == 5\n"
    )
    broken = tmp_path / "candidate"
    broken.mkdir()
    (broken / "targetlib.py").write_text("def combine(a, b):\n    return a * b\n")
    req = tmp_path / "req.json"
    req.write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "T1", "static": {"class": "combine"}, "probes": ["t1"]}
                ]
            }
        )
    )
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, sys\nsys.path.insert(0, sys.argv[1])\n"
        "from targetlib import combine\n"
        'print("PROBE_RESULT " + json.dumps({"t1": combine(2, 3) == 5}))\n'
    )
    og = tmp_path / "o.json"
    og.write_text(json.dumps({"nodes": [], "links": []}))
    ident = {
        k: cp.tree_sha(v)
        for k, v in {
            "original_dir": reference,
            "candidate_dir": broken,
            "oracle_tests": tests,
            "probe_script": probe,
            "requirements": req,
            "original_graph": og,
            "candidate_graph": og,
        }.items()
    }
    cfg = tmp_path / "cfg.json"
    cfg.write_text(
        json.dumps(
            {
                "original_dir": str(reference),
                "candidate_dir": str(broken),
                "oracle_tests": str(tests),
                "probe_script": str(probe),
                "requirements": str(req),
                "product_module": "targetlib",
                "original_graph": str(og),
                "candidate_graph": str(og),
                "gate_script": str(GATE),
                "pinned_python": sys.executable,
                "expected_probe_keys": ["t1"],
                "expected_identities": ident,
            }
        )
    )
    _rc, _comp, det = run_composer(cfg, tmp_path / "out")
    # reference green against ITS staged copy; candidate measured failing
    ref_payload = det["oracle"]["reference"]
    cand_payload = det["oracle"]["candidate"]
    assert ref_payload.get("outcome") == "pass"
    assert ref_payload.get("import_origins", {}).get("good")
    assert cand_payload.get("outcome") == "test_failure"
    assert cand_payload.get("decision") == "block"
    ref_ids = det["test_identities"]["reference"]
    cand_ids = det["test_identities"]["candidate"]
    assert all(s == "pass" for s in ref_ids.values())
    assert all(s == "fail" for s in cand_ids.values())
    passed = sum(1 for t in ref_ids if cand_ids.get(t) == "pass")
    assert passed == 0
    assert "candidate_dropped_applicable_tests" not in _comp.get("block_reasons", [])


def test_composer_rejects_inherited_shadow_origin(tmp_path):
    """A staged module whose name resolves to an already-imported
    site-packages module (upstream_shadow) is BAD provenance: the composer
    must block on import origins instead of trusting the name."""
    original = tmp_path / "original"
    original.mkdir()
    (original / "box.py").write_text(GOOD_BOX)
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "box.py").write_text(GOOD_BOX)
    # pytest always imports pluggy from site-packages before collection
    (shadow / "pluggy.py").write_text("# staged namesake\n")
    shadow_tests = tmp_path / "oracle-shadow"
    shadow_tests.mkdir()
    (shadow_tests / "test_box.py").write_text(
        (FIXTURE / "oracle" / "test_box.py").read_text()
        + "\n\ndef test_shadow():\n    import pluggy\n"
        "    assert pluggy.__file__\n"
    )
    og = tmp_path / "o.json"
    og.write_text(json.dumps(box_graph()))
    ident = {
        k: cp.tree_sha(v)
        for k, v in {
            "original_dir": original,
            "candidate_dir": shadow,
            "oracle_tests": shadow_tests,
            "probe_script": FIXTURE / "probe_box.py",
            "requirements": FIXTURE / "requirements-box.json",
            "original_graph": og,
            "candidate_graph": og,
        }.items()
    }
    cfg = tmp_path / "cfg.json"
    cfg.write_text(
        json.dumps(
            {
                "original_dir": str(original),
                "candidate_dir": str(shadow),
                "oracle_tests": str(shadow_tests),
                "probe_script": str(FIXTURE / "probe_box.py"),
                "requirements": str(FIXTURE / "requirements-box.json"),
                "product_module": "box",
                "original_graph": str(og),
                "candidate_graph": str(og),
                "gate_script": str(GATE),
                "pinned_python": sys.executable,
                "expected_probe_keys": ["put_get", "count", "clear"],
                "expected_identities": ident,
            }
        )
    )
    rc, comp, det = run_composer(cfg, tmp_path / "out")
    assert rc == 2
    assert any(
        "import_origin_failed:candidate" in x for x in comp.get("block_reasons", [])
    )
    cand = det.get("oracle", {}).get("candidate", {})
    assert cand.get("import_origin_ok") is False


def test_composer_empty_oracle_suite_blocks(tmp_path):
    """Infrastructure distinction: an oracle suite with no tests is a
    qualification failure (empty_suite), not a product measurement."""
    original = tmp_path / "original"
    original.mkdir()
    (original / "box.py").write_text(GOOD_BOX)
    empty = tmp_path / "oracle-empty"
    empty.mkdir()
    (empty / "test_none.py").write_text(
        "def helper():\n    return 1\n"
    )  # collects zero tests
    og = tmp_path / "o.json"
    og.write_text(json.dumps(box_graph()))
    ident = {
        k: cp.tree_sha(v)
        for k, v in {
            "original_dir": original,
            "candidate_dir": original,
            "oracle_tests": empty,
            "probe_script": FIXTURE / "probe_box.py",
            "requirements": FIXTURE / "requirements-box.json",
            "original_graph": og,
            "candidate_graph": og,
        }.items()
    }
    cfg = tmp_path / "cfg.json"
    cfg.write_text(
        json.dumps(
            {
                "original_dir": str(original),
                "candidate_dir": str(original),
                "oracle_tests": str(empty),
                "probe_script": str(FIXTURE / "probe_box.py"),
                "requirements": str(FIXTURE / "requirements-box.json"),
                "product_module": "box",
                "original_graph": str(og),
                "candidate_graph": str(og),
                "gate_script": str(GATE),
                "pinned_python": sys.executable,
                "expected_probe_keys": ["put_get", "count", "clear"],
                "expected_identities": ident,
            }
        )
    )
    _rc, comp, det = run_composer(cfg, tmp_path / "out")
    assert comp.get("grade") is None
    assert det["oracle"]["reference"].get("outcome") == "empty_suite"
    assert any(
        x in comp.get("block_reasons", [])
        for x in ("junit_missing_or_invalid", "reference_not_green:empty_suite")
    )
