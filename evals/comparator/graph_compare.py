#!/usr/bin/env python3
"""Graphify output comparison v4 (grader-graph/4).

R12 correction: node identity and source span bind to the actual AST
definition. A node's qualified identity (plain names, Class.member,
aliases, re-exports) is resolved against the product's real definitions
with token boundaries - substrings like BoxUnused never match Box. When
the node supplies a source_location span (Graphify 'L<line>'), the claimed
line must equal the resolved definition's line; a mismatched span or an
unresolvable qualified name demotes the node to context with an explicit
reason. Nodes without a span are context (span_absent) - never falsely
verified node evidence; the requirement then uses the explicit
source-verified fallback path. Public factories, private descriptors and
re-exports remain valid anchors. All R10/R11 guarantees (typed envelope,
unique ids, endpoint checks, deterministic sorted joins, containment)
carry over unchanged.

Usage:
  graph_compare.py --graph <g.json> --product <dir> \
      --requirements <req.json> --probes <probe_results.json>
ASCII only.
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

SCHEMA = "grader-graph/4"


class Unqualified(Exception):
    pass


def _links_field(g):
    present = [k for k in ("links", "edges") if k in g]
    if not present:
        return []
    for k in present:
        if not isinstance(g[k], list):
            raise Unqualified(f"graph_{k}_not_list")
    return g[present[0]]


def qualify_envelope(g):
    if not isinstance(g, dict):
        raise Unqualified("graph_not_object")
    nodes = g.get("nodes")
    if not isinstance(nodes, list):
        raise Unqualified("graph_nodes_not_list")
    if not nodes:
        raise Unqualified("graph_no_nodes")
    links = _links_field(g)
    ids = set()
    for n in nodes:
        if not isinstance(n, dict):
            raise Unqualified("node_not_object")
        nid = n.get("id")
        if not isinstance(nid, str) or not nid.strip():
            raise Unqualified("node_id_missing")
        if nid in ids:
            raise Unqualified(f"node_id_duplicate:{nid[:40]}")
        ids.add(nid)
    for e in links:
        if not isinstance(e, dict):
            raise Unqualified("edge_not_object")
        for end in ("source", "target"):
            v = e.get(end)
            if not isinstance(v, str) or v not in ids:
                raise Unqualified(f"edge_endpoint_dangling:{str(v)[:40]}")
    return nodes, links


def is_test_path(path, product_root):
    try:
        rel_parts = path.relative_to(product_root).parts
    except ValueError:
        rel_parts = path.parts
    return (
        any(p in ("tests", "test") for p in rel_parts)
        or path.name.startswith("test_")
        or path.name.endswith("_test.py")
    )


def classify_node(node, product_root):
    src = node.get("source_file")
    conf = str(node.get("confidence") or "")
    ftype = str(node.get("file_type") or "")
    if isinstance(src, str) and src.strip():
        candidate = Path(src)
        if not candidate.is_absolute():
            candidate = product_root / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(product_root.resolve())
        except (ValueError, OSError):
            return "external", None
        if not resolved.exists():
            return "missing", str(resolved)
        if is_test_path(resolved, product_root):
            return "test", str(resolved)
        if conf in ("INFERRED", "AMBIGUOUS"):
            return "inferred", str(resolved)
        if ftype == "code" or resolved.suffix == ".py":
            return "code", str(resolved)
        return "doc", str(resolved)
    return "missing", None


def parse_span(loc):
    if loc is None:
        return None
    m = re.search(r"L(\d+)", str(loc))
    return int(m.group(1)) if m else None


IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


class AnchorIndex:
    """Real definitions per file: names, qualified class members, aliases
    and re-exports, each with its definition line."""

    def __init__(self, product_root):
        self.root = Path(product_root)
        self.defs = {}  # rel file -> {name: line}
        self.by_name = {}  # name -> [(rel file, line)]
        self.aliases = {}  # rel file -> {alias name: target name}
        for p in sorted(self.root.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            rel = str(p.relative_to(self.root))
            names = {}
            file_aliases = {}
            try:
                tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                self.defs[rel] = names
                continue
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    names.setdefault(node.name, node.lineno)
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            names.setdefault(f"{node.name}.{item.name}", item.lineno)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.setdefault(node.name, node.lineno)
                elif isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            names.setdefault(t.id, node.lineno)
                    if (
                        len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)
                        and isinstance(node.value, ast.Name)
                    ):
                        file_aliases.setdefault(node.targets[0].id, node.value.id)
                elif isinstance(node, ast.AnnAssign) and isinstance(
                    node.target, ast.Name
                ):
                    names.setdefault(node.target.id, node.lineno)
                elif isinstance(node, ast.ImportFrom):
                    for a in node.names:
                        names.setdefault(a.asname or a.name, node.lineno)
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if a.asname:
                            names.setdefault(a.asname, node.lineno)
            self.defs[rel] = names
            self.aliases[rel] = file_aliases
            for n, line in names.items():
                self.by_name.setdefault(n, []).append((rel, line))

    def resolve(self, rel_file, token):
        """Resolve an identifier token to (name, line) in this file."""
        names = self.defs.get(rel_file, {})
        if token in names:
            return token, names[token]
        return None, None

    def identity_candidates(label, node_id):
        """Identifier tokens from the node's label and id (token-bounded)."""
        tokens = []
        for text in (str(label or ""), str(node_id or "")):
            for chunk in re.split(r"[^A-Za-z0-9_.]+", text):
                if IDENT_RE.match(chunk):
                    tokens.append(chunk)
        seen, ordered = set(), []
        for t in tokens:
            if t not in seen:
                seen.add(t)
                ordered.append(t)
        return ordered

    def relevant_to(self, rel_file, token, symbol):
        """(name, line, relation) when token's resolved definition is the
        requirement symbol, a qualified member of it, or an alias/re-export
        bound to it. Token boundaries only."""
        name, line = self.resolve(rel_file, token)
        if name is None:
            return None
        if name == symbol:
            return name, line, "definition_or_alias_or_reexport"
        if self.aliases.get(rel_file, {}).get(name) == symbol:
            return name, line, "definition_or_alias_or_reexport"
        if symbol in name and name != symbol:
            head = name.split(".")[0]
            if head == symbol:
                return name, line, "qualified_member"
        return None


def context_hit(meta, reason, resolved=None):
    """A demoted node: explicit reason, never verified node evidence."""
    hit = {
        "id": meta["id"],
        "label": meta["label"],
        "source_file": meta["source_file"],
        "source_location": meta["source_location"],
        "evidence_kind": meta["evidence_kind"],
        "confidence": meta["confidence"],
        "reason": reason,
    }
    if resolved is not None:
        hit["resolved"] = resolved
    return hit


def node_relevance(meta, rel, anchors, symbol):
    """Resolve a node's qualified identity against the real definitions.
    Returns (relevant, unresolved_claim) - token boundaries, qualified
    names, aliases and re-exports all considered."""
    relevant = None
    unresolved_claim = None
    for token in AnchorIndex.identity_candidates(meta["label"], meta["id"]):
        hit = anchors.relevant_to(rel, token, symbol)
        if hit:
            relevant = (token,) + hit
            break
        nm, _ln = anchors.resolve(rel, token)
        if nm is not None:
            # token resolves to a different definition
            unresolved_claim = f"different_definition:{nm}"
            break
    return relevant, unresolved_claim


def execution_verdict(req, probe_values):
    """Probe alignment for this requirement's declared keys."""
    vals = [(probe_values or {}).get(k) for k in (req.get("probes") or [])]
    if any(v is False for v in vals):
        return "contradicted"
    if vals and all(v is True for v in vals):
        return "aligned"
    return "untested"


def map_requirement(req, node_by_id, anchors, edges_by_id, product_root, probe_values):
    """Map one requirement against span-bound node evidence."""
    symbol = (req.get("static") or {}).get("class")
    code_hits, context_hits = [], []
    anchored_ids = set()
    if symbol:
        for meta in sorted(node_by_id.values(), key=lambda m: (m["id"], m["label"])):
            resolved_path = meta["resolved"]
            if not resolved_path or meta["evidence_kind"] != "code":
                continue
            try:
                rel = str(Path(resolved_path).relative_to(product_root))
            except ValueError:
                continue
            relevant, unresolved_claim = node_relevance(meta, rel, anchors, symbol)
            if not relevant:
                if unresolved_claim:
                    context_hits.append(context_hit(meta, unresolved_claim))
                elif any(
                    symbol in t
                    for t in AnchorIndex.identity_candidates(meta["label"], meta["id"])
                ):
                    context_hits.append(
                        context_hit(meta, "unresolved_qualified_name_or_boundary")
                    )
                continue
            _token, name, def_line, relation = relevant
            claimed = parse_span(meta["source_location"])
            resolved = {"name": name, "line": def_line}
            if claimed is None:
                context_hits.append(context_hit(meta, "span_absent", resolved))
                continue
            if claimed != def_line:
                context_hits.append(context_hit(meta, "span_mismatch", resolved))
                continue
            anchored_ids.add(meta["id"])
            code_hits.append(
                {
                    "id": meta["id"],
                    "label": meta["label"],
                    "source_file": meta["source_file"],
                    "source_location": meta["source_location"],
                    "evidence_kind": meta["evidence_kind"],
                    "confidence": meta["confidence"],
                    "anchor": {
                        "file": rel,
                        "name": name,
                        "line": def_line,
                        "kind": relation,
                        "span_agreement": True,
                    },
                    "execution": execution_verdict(req, probe_values),
                }
            )
    relationships = set()
    for nid in sorted(anchored_ids):
        for e in edges_by_id.get(nid, []):
            relationships.add((e["source"], e["target"], str(e.get("relation"))))
    rel_list = []
    for src, tgt, rel in sorted(relationships)[:10]:
        sm, tm = node_by_id.get(src, {}), node_by_id.get(tgt, {})
        rel_list.append(
            {
                "relation": rel,
                "from_id": src,
                "from_label": sm.get("label", ""),
                "to_id": tgt,
                "to_label": tm.get("label", ""),
                "to_evidence_kind": tm.get("evidence_kind"),
            }
        )
    if not code_hits and not context_hits:
        verdict = "no_evidence"
    elif any(h["execution"] == "contradicted" for h in code_hits):
        verdict = "contradicted"
    elif code_hits and any(
        h["anchor"] and h["execution"] == "aligned" for h in code_hits
    ):
        verdict = "corroborated"
    else:
        verdict = "unverified_claim"
    return {
        "symbol": symbol,
        "code_evidence": code_hits[:6],
        "context_evidence": context_hits[:6],
        "relationships": rel_list,
        "mapped_relationship_count": len(relationships),
        "verdict": verdict,
    }


def compare(
    graph_path, product_root, requirements_path, probe_values, control_graph=False
):
    product_root = Path(product_root)
    try:
        raw = json.loads(Path(graph_path).read_text())
    except (OSError, ValueError):
        return {
            "schema": SCHEMA,
            "qualified": False,
            "error": "graph_not_readable_json",
        }
    try:
        nodes, links = qualify_envelope(raw)
    except Unqualified as exc:
        return {"schema": SCHEMA, "qualified": False, "error": str(exc)}

    anchors = AnchorIndex(product_root)
    node_by_id = {}
    for n in nodes:
        kind, resolved = classify_node(n, product_root)
        node_by_id[n["id"]] = {
            "id": n["id"],
            "label": str(n.get("label") or ""),
            "source_file": n.get("source_file"),
            "source_location": n.get("source_location"),
            "file_type": n.get("file_type"),
            "confidence": n.get("confidence"),
            "evidence_kind": kind,
            "resolved": resolved,
        }
    kind_counts = {}
    for meta in node_by_id.values():
        kind_counts[meta["evidence_kind"]] = (
            kind_counts.get(meta["evidence_kind"], 0) + 1
        )

    edges_by_id = {}
    for e in links:
        edges_by_id.setdefault(e["source"], []).append(e)
        edges_by_id.setdefault(e["target"], []).append(e)

    spec = json.loads(Path(requirements_path).read_text())
    result = {
        "schema": SCHEMA,
        "qualified": True,
        "control_graph": control_graph,
        "graph_path": str(graph_path),
        "node_count": len(nodes),
        "edge_count": len(links),
        "node_evidence_kinds": kind_counts,
        "coverage_note": "counts are context, never quality",
        "requirements": {},
    }
    for req in spec.get("requirements", []):
        result["requirements"][req["id"]] = map_requirement(
            req, node_by_id, anchors, edges_by_id, product_root, probe_values
        )
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--product", required=True)
    ap.add_argument("--requirements", required=True)
    ap.add_argument("--probes", default="")
    args = ap.parse_args()
    probes = json.loads(Path(args.probes).read_text()) if args.probes else {}
    print(
        json.dumps(
            compare(args.graph, args.product, args.requirements, probes), indent=2
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
