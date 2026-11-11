#!/usr/bin/env python3
"""Public staged comparison entry point v2 (grader-comparison/2).

Fixes over v1 per the R08 independent review:
  R08-1 consumes both Graphify graphs (reference + candidate) with schema,
      coverage, provenance and requirement alignment; graph evidence is
      corroborated only against source content and executed probes; missing/
      malformed graph JSON blocks composition.
  R08-2 probes must return the complete declared key set with boolean values
      AND exit 0. Missing/malformed/errored keys or nonzero exit are harness
      failures that block; measured false results remain valid partials.
  R08-3 per-test identities via pytest JUnit XML through the accepted gate's
      --pytest-args. Test score = candidate passes over the reference-
      qualified test-id set. Candidate-only skips or dropped cases can never
      yield full credit; genuine partial failures stay measured.
  R08-4 expected frozen identities verified before AND after the run; any
      drift (e.g. product mutated during probes) blocks. The judge response
      must echo input_bundle_sha256 binding it to this exact evidence.
  R08-5 the judge mandatory_status must match the declared requirement ID
      set exactly (no missing, no extra, correct types).

Gate script and interpreter are explicit config keys (native-resolved), not
hardcoded user paths. Exit codes: 0 grade composed, 2 blocked.
ASCII only.
"""

import argparse
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

SCHEMA = "grader-comparison/4"
WEIGHTS = {"structural": 0.4, "test": 0.4, "judge": 0.2}


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_sha(path):
    path = Path(path).resolve()
    h = hashlib.sha256()
    if path.is_file():
        files, prefix = [path], path.parent
    else:
        files = sorted(
            p
            for p in path.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        )
        prefix = path
    if not files:
        return None
    for p in files:
        h.update(str(p.relative_to(prefix)).encode("utf-8"))
        h.update(b"\0")
        h.update(sha_file(p).encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def load_config(path):
    cfg = json.loads(Path(path).read_text())
    required = [
        "original_dir",
        "candidate_dir",
        "oracle_tests",
        "probe_script",
        "expected_probe_keys",
        "requirements",
        "product_module",
        "original_graph",
        "candidate_graph",
        "gate_script",
        "pinned_python",
        "expected_identities",
    ]
    missing = [k for k in required if k not in cfg]
    if missing:
        raise SystemExit(f"config missing keys: {missing}")
    for key in (
        "gate_script",
        "pinned_python",
        "original_graph",
        "candidate_graph",
        "original_dir",
        "candidate_dir",
        "oracle_tests",
        "probe_script",
        "requirements",
    ):
        if not Path(cfg[key]).exists():
            raise SystemExit(f"config path does not exist: {key}={cfg[key]}")
    if not cfg["expected_probe_keys"]:
        raise SystemExit("expected_probe_keys must be non-empty")
    return cfg


def run_gate(cfg, candidate, tests, label, work, junit_path):
    """Run the T4 benchmark gate (evals/benchmark_gates/gate1_pytest.py)
    with its explicit CLI: --python interpreter, --log JSONL and --logs-dir.
    The gate stages the candidate, copies only trusted test files, rejects
    inherited/external import origins, and emits a JSON decision plus a
    JSONL record keyed by run_id carrying import_origins."""
    log_file = work / (f"gate-{label}.jsonl")
    logs_dir = work / (f"logs-{label}")
    pytest_args = json.dumps(
        ["-q", "--no-header", "-p", "no:cacheprovider", f"--junitxml={junit_path}"]
    )
    cmd = [
        sys.executable,
        str(Path(cfg["gate_script"]).resolve()),
        "run",
        "--candidate-id",
        f"cmp-{label}",
        "--candidate",
        str(candidate),
        "--tests",
        str(tests),
        "--python",
        str(cfg["pinned_python"]),
        "--timeout",
        "300",
        "--pytest-args",
        pytest_args,
        "--label",
        label,
        "--control",
        "--log",
        str(log_file),
        "--logs-dir",
        str(logs_dir),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(work),
        timeout=300,
        check=False,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {
            "decision": "block",
            "outcome": "gate_output_invalid",
            "raw": (proc.stdout + proc.stderr)[-800:],
        }
    payload["gate_exit"] = proc.returncode
    if log_file.exists():
        record = None
        for line in log_file.read_text(errors="replace").splitlines():
            if line.strip():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("run_id") == payload.get("run_id"):
                    record = rec
        if record:
            payload["import_origins"] = record.get("import_origins")
    return payload


def normalize_test_id(classname, name):
    """Layout-independent id: last two classname components + case name.

    The same oracle staged in-tree ('tests.test_x.TestY') and externally
    ('_gate_oracle.test_x.TestY') must map to the same id."""
    parts = (classname or "").split(".")
    tail = ".".join(parts[-2:]) if len(parts) >= 2 else (classname or "")
    return f"{tail}::{name}"


def parse_junit(path):
    """Return {normalized_test_id: status} from JUnit XML."""
    if not Path(path).exists():
        return None
    statuses = {}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None
    for case in root.iter("testcase"):
        cid = normalize_test_id(case.get("classname", ""), case.get("name", ""))
        if case.find("failure") is not None:
            statuses[cid] = "fail"
        elif case.find("error") is not None:
            statuses[cid] = "error"
        elif case.find("skip") is not None or case.find("skipped") is not None:
            statuses[cid] = "skip"
        else:
            statuses[cid] = "pass"
    return statuses


def origins_proven(gate_payload):
    origins = gate_payload.get("import_origins") or {}
    good = origins.get("good") or []
    bad = origins.get("bad") or []
    return bool(good) and not bad


def run_probes(probe_script, product, expected_keys, work, label):
    script = Path(probe_script).resolve()
    log = work / (f"probes-{label}.log")
    try:
        proc = subprocess.run(
            [sys.executable, str(script), str(product)],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        out, rc = proc.stdout + proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or b""
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "replace")
        out, rc = raw.decode("utf-8", "replace") + "PROBE_TIMEOUT", None
    log.write_text(out, encoding="utf-8", errors="replace")
    result = None
    for line in out.splitlines():
        if line.startswith("PROBE_RESULT "):
            try:
                result = json.loads(line[len("PROBE_RESULT ") :])
            except ValueError:
                result = None
    if not isinstance(result, dict):
        return {
            "qualified": False,
            "harness_errors": ["probe_output_invalid"],
            "exit_code": rc,
            "results": None,
            "log": log.name,
        }
    harness_errors = []
    values = {}
    for key in expected_keys:
        if key not in result:
            harness_errors.append(f"probe_key_missing:{key}")
            values[key] = "untested"
            continue
        v = result[key]
        if v is True or v is False:
            values[key] = v
        elif v is None:
            harness_errors.append(f"probe_key_null:{key}")
            values[key] = "untested"
        else:
            harness_errors.append(f"probe_key_malformed:{key}")
            values[key] = "malformed"
    extra = [k for k in result if k not in expected_keys and k != "harness_error"]
    if extra:
        harness_errors.append(
            "probe_keys_unexpected:{}".format(",".join(sorted(extra)[:5]))
        )
    if rc not in (0, None) and not harness_errors:
        harness_errors.append(f"probe_exit_{rc}")
    if rc is None:
        harness_errors.append("probe_timeout")
    return {
        "qualified": not harness_errors,
        "harness_errors": harness_errors,
        "exit_code": rc,
        "results": values,
        "log": log.name,
    }


def score_finite(v):
    return (
        not isinstance(v, bool)
        and isinstance(v, (int, float))
        and math.isfinite(float(v))
        and 0.0 <= float(v) <= 1.0
    )


def validate_judge(text, expected_ids, bundle_sha):
    issues = []
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None, ["judge_output_not_json"]
    if not isinstance(data, dict):
        return None, ["judge_output_not_object"]
    for field in ("judge_score", "original_score"):
        if not score_finite(data.get(field)):
            issues.append(f"{field}_invalid")
    mand = data.get("mandatory_status")
    if not isinstance(mand, dict):
        issues.append("mandatory_status_invalid")
    else:
        got = set(mand)
        want = set(expected_ids)
        if got != want:
            for k in sorted(want - got):
                issues.append(f"mandatory_missing:{k}")
            for k in sorted(got - want):
                issues.append(f"mandatory_extra:{k}")
        for k, v in mand.items():
            if type(v) is not str or v not in ("met", "unmet", "untested"):
                issues.append(f"mandatory_bad_value:{k}")
    if data.get("qualitative") not in (
        "better",
        "roughly_equivalent",
        "worse",
        "uncertain",
    ):
        issues.append("qualitative_invalid")
    if data.get("confidence") not in ("low", "medium", "high"):
        issues.append("confidence_invalid")
    rationale = data.get("rationale")
    if not isinstance(rationale, str) or len(rationale.strip()) < 20:
        issues.append("rationale_invalid")
    evidence = data.get("evidence")
    if (
        not isinstance(evidence, list)
        or not evidence
        or not all(isinstance(x, str) and x.strip() for x in evidence)
    ):
        issues.append("evidence_invalid")
    for field in ("strengths", "omissions", "tradeoffs"):
        v = data.get(field)
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            issues.append(f"{field}_invalid")
    if str(data.get("contamination", "")).strip().lower() != "none":
        issues.append("contamination_declared_or_missing")
    if data.get("input_bundle_sha256") != bundle_sha:
        issues.append("judge_bundle_mismatch")
    return data, issues


FROZEN_KEYS = (
    "original_dir",
    "candidate_dir",
    "oracle_tests",
    "probe_script",
    "requirements",
    "original_graph",
    "candidate_graph",
)


def verify_identities(cfg):
    """Stage 1: hash every frozen input before anything executes."""
    reasons, ident_before = [], {}
    for key in FROZEN_KEYS:
        ident_before[key] = tree_sha(cfg[key])
        if ident_before[key] is None:
            reasons.append(f"identity_unmeasured:{key}")
    for key, expected in cfg.get("expected_identities", {}).items():
        if ident_before.get(key) != expected:
            reasons.append(f"identity_mismatch:{key}")
    return ident_before, reasons


def run_oracle_phase(cfg, work):
    """Stage 2: real gate execution for both products (explicit
    interpreter, --log/--logs-dir, JUnit identities)."""
    ref_junit = work / "junit-ref.xml"
    cand_junit = work / "junit-cand.xml"
    ref = run_gate(
        cfg, cfg["original_dir"], cfg["oracle_tests"], "ref", work, ref_junit
    )
    cand = run_gate(
        cfg, cfg["candidate_dir"], cfg["oracle_tests"], "cand", work, cand_junit
    )
    ref_tests, cand_tests = parse_junit(ref_junit), parse_junit(cand_junit)
    reasons = []
    if ref_tests is None or cand_tests is None:
        reasons.append("junit_missing_or_invalid")
    ref_green = (
        ref.get("outcome") == "pass"
        and origins_proven(ref)
        and ref_tests is not None
        and all(s == "pass" for s in ref_tests.values())
    )
    if not ref_green and "junit_missing_or_invalid" not in reasons:
        reasons.append(f"reference_not_green:{ref.get('outcome')}")
    if not origins_proven(ref):
        reasons.append("import_origin_failed:original")
    if not origins_proven(cand):
        reasons.append("import_origin_failed:candidate")
    return ref, cand, ref_tests, cand_tests, ref_green, reasons


def score_tests(ref_tests, cand_tests):
    """Stage 3: reference-qualified denominator; skips/drops never earn
    credit, genuine failures stay measured partials."""
    if not (ref_tests and cand_tests):
        return (
            {"original": None, "candidate": None},
            {"policy": "no qualified per-test evidence"},
            [],
        )
    ref_set = {tid for tid, s in ref_tests.items() if s == "pass"}
    cand_pass = {tid for tid, s in cand_tests.items() if s == "pass"}
    cand_skip = {tid for tid, s in cand_tests.items() if s == "skip"}
    missing = sorted(ref_set - set(cand_tests))
    candidate_only = sorted(set(cand_tests) - set(ref_tests))
    passed_in_set = len(ref_set & cand_pass)
    scores = {
        "original": 1.0,
        "candidate": round(passed_in_set / len(ref_set), 4) if ref_set else None,
    }
    detail = {
        "reference_qualified_count": len(ref_set),
        "candidate_passed_in_set": passed_in_set,
        "candidate_skips_in_set": sorted(ref_set & cand_skip),
        "candidate_missing_tests": missing,
        "candidate_only_tests": candidate_only,
        "policy": "score = passes over reference-qualified ids; skips and "
        "dropped cases never yield credit; genuine failures stay measured",
    }
    reasons = []
    if missing or (ref_set & cand_skip):
        reasons.append("candidate_dropped_applicable_tests")
    return scores, detail, reasons


def run_probe_phase(cfg, work):
    """Stage 4: probes with complete schema + exit qualification."""
    probes, reasons = {}, []
    for label in ("original", "candidate"):
        probes[label] = run_probes(
            cfg["probe_script"],
            cfg[f"{label}_dir"],
            cfg["expected_probe_keys"],
            work,
            label,
        )
        if not probes[label]["qualified"]:
            reasons.append(
                "probes_harness_failed:{}:{}".format(
                    label, ",".join(probes[label]["harness_errors"][:3])
                )
            )
    return probes, reasons


def run_graph_phase(cfg, probes):
    """Stage 5: span-bound graph mapping for both products."""
    sys.path.insert(0, str(Path(__file__).parent))
    import graph_compare as gcmod

    graphs, reasons = {}, []
    for label, gkey, pkey in (
        ("original", "original_graph", "original_dir"),
        ("candidate", "candidate_graph", "candidate_dir"),
    ):
        graphs[label] = gcmod.compare(
            cfg[gkey],
            cfg[pkey],
            cfg["requirements"],
            probes[label].get("results"),
            control_graph=False,
        )
        if not graphs[label].get("qualified"):
            reasons.append(
                "graph_unqualified:{}:{}".format(label, graphs[label].get("error"))
            )
    return graphs, reasons


def integrate_graph_evidence(struct_label, graph_reqs):
    """Stage 6a (per product): explicit per-capability policy.
    corroborated -> supported; unverified/no_evidence -> cited source
    fallback only with a real anchor, else capability_evidence_unresolved;
    contradicted -> loses support. Extraction gaps are never numeric
    product faults, and never blanket qualified credit."""
    statuses = struct_label["requirements"]
    unresolved = []
    for rid, gres in graph_reqs.items():
        entry = statuses.get(rid)
        if not entry:
            continue
        verdict = gres.get("verdict")
        entry["graph_verdict"] = verdict
        entry["graph_relationships"] = gres.get("mapped_relationship_count", 0)
        static_ev = (entry.get("citation") or {}).get("static")
        if verdict == "contradicted" and entry["status"].startswith("supported"):
            entry["status"] = "contradicted_by_graph"
        elif verdict in ("unverified_claim", "no_evidence"):
            if entry["status"].startswith("supported") and static_ev:
                entry["status"] = "supported_source_fallback"
                entry["fallback_reason"] = (
                    "no qualified graph evidence for this capability; "
                    f"source anchor + executed probes verified: "
                    f"{json.dumps(static_ev, sort_keys=True)}"
                )
            elif entry["status"].startswith("supported"):
                entry["status"] = "capability_evidence_unresolved"
                unresolved.append(rid)
    return unresolved


def build_structural(cfg, graphs, probes):
    """Stage 6: capability mapping + graph integration."""
    import check_capabilities as cc

    struct, reasons = {}, {}
    for label in ("original", "candidate"):
        struct[label] = cc.map_product(
            cfg[f"{label}_dir"],
            label,
            cfg["requirements"],
            probes[label].get("results"),
            cfg["product_module"],
        )
        if "requirements" not in struct[label]:
            # mapper rejected the requirements schema: fail closed, never
            # crash and never fabricate a structural score
            struct[label]["structural_score"] = None
            struct[label].setdefault(
                "qualification", struct[label].get("error") or "mapper_error"
            )
            reasons[label] = [struct[label]["qualification"]]
            continue
        unresolved = integrate_graph_evidence(
            struct[label], graphs[label].get("requirements", {})
        )
        statuses = struct[label]["requirements"]
        label_reasons = []
        if any(v.get("status") == "error" for v in statuses.values()):
            struct[label]["structural_score"] = None
            struct[label]["qualification"] = "probe_error"
            label_reasons.append("probe_error")
        elif unresolved:
            struct[label]["structural_score"] = None
            struct[label]["qualification"] = (
                "capability_evidence_unresolved:" + ",".join(sorted(unresolved))
            )
            label_reasons.append(struct[label]["qualification"])
        else:
            supported = sum(
                1 for v in statuses.values() if v["status"].startswith("supported")
            )
            struct[label]["structural_score"] = (
                round(supported / len(statuses), 4) if statuses else None
            )
        reasons[label] = label_reasons
    return struct, reasons


def build_bundle(ident_before, test_detail, probes, graphs, struct_scores):
    """Stage 8a: the frozen evidence bundle hash binding the judge."""
    bundle_material = json.dumps(
        {
            "identity": ident_before,
            "test_detail": test_detail,
            "probes": {k: v.get("results") for k, v in probes.items()},
            "structural_scores": struct_scores,
            "graph_verdicts": {
                k: {
                    r: v.get("verdict")
                    for r, v in graphs[k].get("requirements", {}).items()
                }
                for k in graphs
            },
            "graph_relationships": {
                k: {
                    r: v.get("mapped_relationship_count", 0)
                    for r, v in graphs[k].get("requirements", {}).items()
                }
                for k in graphs
            },
        },
        sort_keys=True,
    )
    return hashlib.sha256(bundle_material.encode("utf-8")).hexdigest()


def compose(
    reasons, struct_scores, test_scores, test_detail, judge_parsed, judge_issues
):
    """Stage 9: compose or block. Never a numeric grade on unqualified,
    missing or malformed evidence."""
    composed = {"schema": SCHEMA, "weights": WEIGHTS, "test_detail": test_detail}
    if judge_issues:
        reasons.append("judge_invalid:" + ",".join(judge_issues[:5]))
    components_null = any(
        v is None for v in list(struct_scores.values()) + list(test_scores.values())
    )
    if reasons or components_null:
        composed["grade"] = None
        composed["block_reasons"] = reasons or ["score_component_null"]
        composed["diagnostic"] = {
            "structural": struct_scores,
            "test": test_scores,
            "judge": None,
        }
        return composed
    composed["grade"] = {}
    for label, jscore in (
        ("candidate", judge_parsed["judge_score"]),
        ("original", judge_parsed["original_score"]),
    ):
        composed["grade"][label] = {
            "structural": struct_scores[label],
            "test": test_scores[label],
            "judge": jscore,
            "composite": round(
                WEIGHTS["structural"] * struct_scores[label]
                + WEIGHTS["test"] * test_scores[label]
                + WEIGHTS["judge"] * jscore,
                4,
            ),
        }
    return composed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--judge-response", default="")
    args = ap.parse_args()
    cfg = load_config(args.config)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="cmp2-", dir=str(out)))
    receipts = {
        "schema": SCHEMA,
        "started_utc": now(),
        "config_paths": {k: cfg[k] for k in cfg if k != "expected_identities"},
    }
    reasons = []

    ident_before, ident_reasons = verify_identities(cfg)
    reasons += ident_reasons
    receipts["identity_before"] = ident_before

    ref, cand, ref_tests, cand_tests, ref_green, oracle_reasons = run_oracle_phase(
        cfg, work
    )
    reasons += oracle_reasons
    receipts["oracle"] = {"reference": ref, "candidate": cand}
    receipts["test_identities"] = {"reference": ref_tests, "candidate": cand_tests}

    test_scores, test_detail, test_reasons = score_tests(ref_tests, cand_tests)
    reasons += test_reasons

    probes, probe_reasons = run_probe_phase(cfg, work)
    reasons += probe_reasons
    receipts["probes"] = probes

    graphs, graph_reasons = run_graph_phase(cfg, probes)
    reasons += graph_reasons
    receipts["graphs"] = graphs

    struct, struct_reason_map = build_structural(cfg, graphs, probes)
    for label, label_reasons in struct_reason_map.items():
        for qual in label_reasons:
            reasons.append(f"structural_unqualified:{label}:{qual}")
    receipts["structural"] = struct
    struct_scores = {k: struct[k].get("structural_score") for k in struct}

    ident_after = {key: tree_sha(cfg[key]) for key in FROZEN_KEYS}
    receipts["identity_after"] = ident_after
    for key in FROZEN_KEYS:
        if ident_before[key] != ident_after[key]:
            reasons.append(f"identity_drift_during_run:{key}")

    receipts["qualification"] = {"reference_green": ref_green}
    receipts["test_detail"] = test_detail

    req_doc = json.loads(Path(cfg["requirements"]).read_text())
    expected_ids = [r["id"] for r in req_doc["requirements"]]
    bundle_sha = build_bundle(ident_before, test_detail, probes, graphs, struct_scores)
    receipts["input_bundle_sha256"] = bundle_sha
    if args.judge_response and Path(args.judge_response).exists():
        judge_parsed, judge_issues = validate_judge(
            Path(args.judge_response).read_text(), expected_ids, bundle_sha
        )
    else:
        judge_parsed, judge_issues = None, ["judge_response_missing"]

    composed = compose(
        reasons, struct_scores, test_scores, test_detail, judge_parsed, judge_issues
    )
    receipts["composed"] = composed
    receipts["finished_utc"] = now()
    (out / "comparison-receipt.json").write_text(json.dumps(receipts, indent=2) + "\n")
    print(json.dumps(composed, indent=2))
    return 0 if composed.get("grade") else 2


if __name__ == "__main__":
    sys.exit(main())
