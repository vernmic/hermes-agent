#!/usr/bin/env python3
"""Targeted controls: public_names anchor support in the graph mapper.

Backward-compatible format proposal (grader-graph/5): requirements may
anchor implementations via anchor.kind=public_names in addition to the
legacy static.class. Positives: public class + function anchors, a
package-layout factory alternative, alias/re-export anchors with correct
source identities and line spans. Negatives: wrong-file, wrong-line,
name-prefix lookalike, docs and test nodes never count; a wholly missing
graph uses the declared per-capability fallback (never automatic
corroboration); a requirement with no resolvable anchor and no static
evidence stays unresolved (unqualified), never blanket credit.
ASCII only.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evals" / "comparator"))

import graph_compare as gc
from compare_products import integrate_graph_evidence

PRODUCT = '''\
"""module doc"""
import threading


class Widget:
    def spin(self):
        return 1


def make_widget(name):
    return Widget()


FastWidget = Widget
slow_widget = make_widget
'''


def make_product(tmp_path, text=PRODUCT, pkg=False):
    root = tmp_path / ("pkgprod" if pkg else "flatprod")
    if pkg:
        (root / "pkgmod").mkdir(parents=True)
        (root / "pkgmod" / "__init__.py").write_text("")
        (root / "pkgmod" / "impl.py").write_text(text)
        (root / "tests").mkdir()
        (root / "tests" / "test_impl.py").write_text(
            "from pkgmod.impl import Widget  # test file\n")
        (root / "README.md").write_text("Widget and make_widget docs\n")
    else:
        root.mkdir(parents=True)
        (root / "impl.py").write_text(text)
        (root / "tests").mkdir()
        (root / "tests" / "test_impl.py").write_text(
            "from impl import Widget  # test file\n")
        (root / "README.md").write_text("Widget and make_widget docs\n")
    return root


def node(nid, label, rel_file, line, kind="code", conf="OBSERVED", ftype="code"):
    return {
        "id": nid,
        "label": label,
        "source_file": rel_file,
        "source_location": f"L{line}",
        "file_type": ftype,
        "confidence": conf,
    }


def graph(nodes, edges=()):
    return {"nodes": list(nodes), "links": [
        {"source": s, "target": t, "relation": r} for s, t, r in edges]}


def req(rid, names=None, static_class=None, probes=("p1",)):
    r = {"id": rid, "description": rid, "probes": list(probes)}
    if names is not None:
        r["anchor"] = {"kind": "public_names", "names": names}
    if static_class is not None:
        r["static"] = {"class": static_class}
    return r


def compare(tmp_path, root, nodes, requirement, edges=(), probes=None):
    g = graph(nodes, edges)
    gpath = tmp_path / "g.json"
    gpath.write_text(json.dumps(g))
    rpath = tmp_path / "r.json"
    rpath.write_text(json.dumps({"requirements": [requirement]}))
    return gc.compare(str(gpath), root, str(rpath), probes or {"p1": True})


# WIDGET=5 (class), MAKE_WIDGET=10 (function), FASTWIDGET=14 (alias),
# SLOW_WIDGET=15 (alias), WID=18 in PRODUCT above.


def test_public_class_and_function_anchors_corroborate(tmp_path):
    root = make_product(tmp_path)
    res = compare(
        tmp_path, root,
        [node("n1", "Widget", "impl.py", 5), node("n2", "make_widget", "impl.py", 10)],
        req("R1", names=["Widget", "make_widget"]),
        edges=[("n1", "n2", "creates")],
    )
    assert res["qualified"] is True
    r1 = res["requirements"]["R1"]
    assert r1["verdict"] == "corroborated", r1
    assert r1["matched_symbols"] == ["Widget", "make_widget"]
    hits = {h["anchor"]["name"]: h for h in r1["code_evidence"]}
    assert hits["Widget"]["anchor"]["line"] == 5
    assert hits["make_widget"]["anchor"]["line"] == 10
    assert r1["relationships"], "meaningful relationships expected"
    assert r1["relationships"][0]["relation"] == "creates"


def test_package_layout_factory_alternative(tmp_path):
    root = make_product(tmp_path, pkg=True)
    res = compare(
        tmp_path, root,
        [node("n1", "make_widget factory", "pkgmod/impl.py", 10)],
        req("R1", names=["make_widget"]),
    )
    r1 = res["requirements"]["R1"]
    assert r1["verdict"] == "corroborated", r1
    assert r1["code_evidence"][0]["anchor"]["file"] == "pkgmod/impl.py"
    assert r1["code_evidence"][0]["anchor"]["line"] == 10


def test_alias_and_reexport_anchors_with_source_identity(tmp_path):
    root = make_product(tmp_path)
    res = compare(
        tmp_path, root,
        [node("n1", "FastWidget", "impl.py", 14),
         node("n2", "slow_widget", "impl.py", 15)],
        req("R1", names=["Widget", "make_widget"]),
    )
    r1 = res["requirements"]["R1"]
    assert r1["verdict"] == "corroborated", r1
    hits = {h["anchor"]["name"]: h for h in r1["code_evidence"]}
    # aliases resolve to their OWN definition lines with alias relation
    assert hits["FastWidget"]["anchor"]["line"] == 14
    assert hits["slow_widget"]["anchor"]["line"] == 15
    assert all(h["anchor"]["kind"] == "definition_or_alias_or_reexport"
               for h in r1["code_evidence"])


def test_wrong_file_and_wrong_line_do_not_count(tmp_path):
    root = make_product(tmp_path)
    res = compare(
        tmp_path, root,
        [node("n1", "Widget", "README.md", 5, ftype="doc"),
         node("n2", "Widget", "tests/test_impl.py", 5),
         node("n3", "Widget", "impl.py", 3)],  # wrong line: line 3 is a comment
        req("R1", names=["Widget"]),
    )
    r1 = res["requirements"]["R1"]
    assert r1["verdict"] != "corroborated", r1
    assert r1["code_evidence"] == []
    reasons = {h["reason"] for h in r1["context_evidence"]}
    assert "span_mismatch" in reasons


def test_name_prefix_lookalike_never_matches(tmp_path):
    text = PRODUCT + "\n\n Wid = 1\n"
    root = make_product(tmp_path, text=text)
    res = compare(
        tmp_path, root,
        [node("n1", "Wid", "impl.py", 18)],
        req("R1", names=["Widget"]),
    )
    r1 = res["requirements"]["R1"]
    assert r1["verdict"] == "no_evidence", r1
    assert r1["code_evidence"] == []


def test_missing_graph_uses_declared_fallback_not_corroboration(tmp_path):
    root = make_product(tmp_path)
    # graph has NO node for Widget at all
    res = compare(
        tmp_path, root,
        [node("n1", "unrelated", "impl.py", 12)],
        req("R1", names=["Widget"]),
    )
    r1 = res["requirements"]["R1"]
    assert r1["verdict"] == "no_evidence", r1
    # structural policy: supported + real static citation -> explicit
    # source+probe fallback, never automatic corroboration
    struct_label = {"requirements": {"R1": {
        "status": "supported",
        "citation": {"static": {"file": "impl.py", "names": ["Widget"],
                                "lines": {"Widget": 5},
                                "kind": "public_names_anchor"}},
    }}}
    unresolved = integrate_graph_evidence(struct_label, {"R1": r1})
    assert struct_label["requirements"]["R1"]["status"] == \
        "supported_source_fallback"
    assert "source anchor" in struct_label["requirements"]["R1"]["fallback_reason"]
    assert unresolved == []


def test_unresolved_mapping_is_unqualified_not_blanket_credit(tmp_path):
    root = make_product(tmp_path)
    res = compare(
        tmp_path, root,
        [node("n1", "unrelated", "impl.py", 12)],
        req("R1", names=["Widget"]),
    )
    r1 = res["requirements"]["R1"]
    # supported WITHOUT a real static citation and without graph evidence
    # stays capability_evidence_unresolved
    struct_label = {"requirements": {"R1": {"status": "supported"}}}
    unresolved = integrate_graph_evidence(struct_label, {"R1": r1})
    assert struct_label["requirements"]["R1"]["status"] == \
        "capability_evidence_unresolved"
    assert unresolved == ["R1"]


def test_legacy_static_class_still_works(tmp_path):
    root = make_product(tmp_path)
    res = compare(
        tmp_path, root,
        [node("n1", "Widget", "impl.py", 5)],
        req("R1", static_class="Widget"),
    )
    r1 = res["requirements"]["R1"]
    assert r1["verdict"] == "corroborated", r1
    assert r1["symbol"] == "Widget"
    assert r1["symbols"] == ["Widget"]


def test_mixed_legacy_and_public_names_merge(tmp_path):
    root = make_product(tmp_path)
    res = compare(
        tmp_path, root,
        [node("n1", "Widget", "impl.py", 5), node("n2", "make_widget", "impl.py", 10)],
        {"id": "R1", "description": "R1", "probes": ["p1"],
         "static": {"class": "Widget"},
         "anchor": {"kind": "public_names", "names": ["make_widget"]}},
        edges=[("n1", "n2", "uses")],
    )
    r1 = res["requirements"]["R1"]
    assert r1["verdict"] == "corroborated", r1
    assert set(r1["matched_symbols"]) == {"Widget", "make_widget"}


def test_no_anchor_at_all_is_no_evidence(tmp_path):
    root = make_product(tmp_path)
    res = compare(
        tmp_path, root,
        [node("n1", "Widget", "impl.py", 5)],
        req("R1"),
    )
    r1 = res["requirements"]["R1"]
    assert r1["verdict"] == "no_evidence", r1
    assert r1["symbols"] == []
    assert r1["code_evidence"] == []


def test_malformed_public_names_anchor_rejected_cleanly(tmp_path):
    root = make_product(tmp_path)
    res = compare(
        tmp_path, root,
        [node("n1", "Widget", "impl.py", 5)],
        req("R1", names=["Widget", 7, "not a name!"]),
    )
    r1 = res["requirements"]["R1"]
    # valid entries still anchor; invalid entries ignored, no crash
    assert r1["verdict"] == "corroborated", r1
    assert r1["symbols"] == ["Widget"]
