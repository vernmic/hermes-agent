#!/usr/bin/env python3
"""Generic schema-driven capability mapper (grader-requirements/1).

STAGED PROTECTED CANDIDATE - review copy. The installed grader-structural
skill and its scripts are NOT modified.

Requirements schema (JSON):
{
  "schema": "grader-requirements/1",
  "product_module": "cached_property",        # import name under the product dir
  "requirements": [
    {
      "id": "R1",
      "description": "instance memoization",
      "static": {"class": "cached_property", "method": "__get__"},
      "probes": ["r1_memoizes", "r1_per_instance", "r1_none_cached"],
      "probe_mode": "all"                       # all|any|none_optional
    },
    ...
  ]
}

A requirement is supported only when its static evidence is a non-stub
implementation (file:line cited) AND its declared probes all executed true.
Missing/untested/malformed probes make the requirement 'untested', never
supported. Probe false with static evidence = 'contradicted'. No static and
no probes = 'missing'. Graph citations are accepted optionally as context
only and never satisfy anything.

Structural score = supported / total, or null when any requirement errored
(qualification failure - never a fabricated number).
ASCII only.
"""

import ast
import json
from pathlib import Path

EXCLUDE_PARTS = {"__pycache__", "tests", "test", ".git", "build", "dist"}
NON_PUBLIC = {"setup.py", "conftest.py", "setup_cfg.py"}


def public_sources(root):
    root = Path(root)
    out = []
    for p in sorted(root.rglob("*.py")):
        if any(part in EXCLUDE_PARTS for part in p.parts):
            continue
        if (
            p.name in NON_PUBLIC
            or p.name.startswith("test_")
            or p.name.endswith("_test.py")
        ):
            continue
        out.append(p)
    return out


def is_stub_body(body):
    for node in body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if isinstance(node, ast.Pass):
            continue
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and node.value.value is Ellipsis
        ):
            continue
        if isinstance(node, ast.Raise):
            exc = node.exc
            name = None
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                name = exc.func.id
            elif isinstance(exc, ast.Name):
                name = exc.id
            if name and "NotImplementedError" in name:
                continue
        return False
    return True


def collect_classes(root):
    """(relative file, ClassDef) for non-stub classes in public source."""
    for p in public_sources(root):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and not is_stub_body(node.body):
                yield str(p.relative_to(root)), node


def static_lookup(root, spec):
    """Find the class named in spec; verify optional required method exists
    as an implemented (non-stub) method. Returns citation or None."""
    want_class = spec.get("class")
    want_method = spec.get("method")
    for rel, cls in collect_classes(root):
        if want_class and cls.name != want_class:
            continue
        if want_method:
            impl = None
            for item in cls.body:
                if isinstance(item, ast.FunctionDef) and item.name == want_method:
                    impl = "implemented" if not is_stub_body(item.body) else "stub"
                    break
            if impl != "implemented":
                continue
        return {
            "file": rel,
            "class": cls.name,
            "line": cls.lineno,
            "method": want_method,
        }
    return None


def public_names_anchor(root, product_module, names):
    """Verify each public name is defined/aliased/re-exported by the
    product module (single module or package __init__). Returns citation."""
    root = Path(root)
    candidates = [
        root / (f"{product_module}.py"),
        root / product_module / "__init__.py",
    ]
    for mod_path in candidates:
        if not mod_path.exists():
            continue
        rel = str(mod_path.relative_to(root))
        try:
            tree = ast.parse(mod_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        found = {}
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                found.setdefault(node.name, node.lineno)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        found.setdefault(t.id, node.lineno)
            elif isinstance(node, (ast.ImportFrom, ast.Import)):
                for a in node.names:
                    found.setdefault(a.asname or a.name, node.lineno)
        if all(n in found for n in names):
            return {
                "file": rel,
                "names": names,
                "lines": {n: found[n] for n in names},
                "kind": "public_names_anchor",
            }
    return None


def probe_status(probe_values, keys, mode):
    """probe_values: dict key -> true/false/'untested'/'malformed'/'error:*'.
    Returns (status, detail): pass|fail|untested."""
    if not keys:
        return "pass", "no_probes_declared_static_only"
    vals = [probe_values.get(k, "untested") for k in keys]
    if any(
        v in ("untested", "malformed")
        or (isinstance(v, str) and v.startswith("error:"))
        for v in vals
    ):
        return "untested", dict(zip(keys, vals))
    booleans = [v for v in vals if isinstance(v, bool)]
    if mode == "any":
        return ("pass" if any(booleans) else "fail"), dict(zip(keys, vals))
    ok = all(booleans) and len(booleans) == len(vals)
    return ("pass" if ok else "fail"), dict(zip(keys, vals))


def map_product(root, label, requirements_path, probe_values, product_module):
    root = Path(root)
    spec_doc = json.loads(Path(requirements_path).read_text())
    if spec_doc.get("schema") != "grader-requirements/1":
        return {
            "product": label,
            "error": "unsupported requirements schema",
            "structural_score": None,
            "qualification": "schema_mismatch",
        }
    out = {"product": label, "root": str(root), "requirements": {}}
    errored = False
    for req in spec_doc["requirements"]:
        rid = req["id"]
        try:
            anchor_spec = req.get("anchor")
            if anchor_spec and anchor_spec.get("kind") == "public_names":
                static_ev = public_names_anchor(
                    root,
                    spec_doc.get("product_module") or product_module,
                    anchor_spec.get("names", []),
                )
            else:
                static_ev = static_lookup(root, req.get("static") or {})
            status_p, detail = probe_status(
                probe_values or {},
                req.get("probes") or [],
                req.get("probe_mode", "all"),
            )
        except Exception as exc:  # health: allow BLE001 -- per-requirement error becomes a fail-closed error status, never swallowed
            out["requirements"][rid] = {
                "status": "error",
                "detail": f"{type(exc).__name__}: {exc}",
            }
            errored = True
            continue
        citation = {"static": static_ev, "probes": detail}
        if status_p == "untested":
            status = "untested"
        elif status_p == "fail":
            status = "contradicted"
        elif static_ev is not None:
            status = "supported"
        elif req.get("probes"):
            status = "supported_behavioral_only"
        else:
            status = "missing"
        out["requirements"][rid] = {"status": status, "citation": citation}
    if errored:
        out["structural_score"] = None
        out["qualification"] = "probe_error"
    else:
        supported = sum(
            1
            for v in out["requirements"].values()
            if v["status"].startswith("supported")
        )
        out["structural_score"] = round(supported / len(out["requirements"]), 4)
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--requirements", required=True)
    ap.add_argument("--impl", required=True)
    ap.add_argument("--probes", default="")
    ap.add_argument("--product", default="product")
    args = ap.parse_args()
    probes = json.loads(Path(args.probes).read_text()) if args.probes else {}
    print(
        json.dumps(
            map_product(args.impl, args.product, args.requirements, probes, None),
            indent=2,
        )
    )
