#!/usr/bin/env python3
"""Freeze an OpenThoughts-TBLite fast-subset proposal before any run.

Qualified mode (default) is fail-closed: the dataset must be a clean git
checkout whose OBSERVED HEAD equals the expected pin commit; the corpus
must contain exactly the pinned task count; every task must map to a
canonical category; and the deterministic selection must fill every
category allocation with zero shortfall. Any violation exits nonzero
with a machine-readable reason and writes no qualified artifacts.

Every task is bound by a complete content-addressed tree hash (all task
files, excluding only documented caches), so mutating any environment
asset consumed by Docker COPY changes the manifest.

Generic mode (--generic) accepts arbitrary fixture folders: it writes an
explicitly UNQUALIFIED manifest (qualified=false, observed commit or
null) and refuses to emit a selection, so fixtures can never be
presented as the verified pinned corpus or a valid fast subset.

--expect-selection freezes continuity: the newly selected ids must
equal the prior selection's ids or the run fails. Selection is frozen
BEFORE outcomes; tasks are never replaced after results exist.

ASCII only.
"""
import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

# Frozen preflight pins and protocol constants.
EXPECTED_COMMIT = "5c37b41f00ce04719a4453061076ae9f46b74b7d"
EXPECTED_TASK_COUNT = 100
SEED_DEFAULT = "hermes-tblite-preflight-20261110-v1"

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

# Raw task.toml category -> canonical category. PROJECT-PROPOSED mapping,
# not an official dataset taxonomy: the dataset publishes category
# aggregates only, never per-task assignments. Ambiguous raw labels are
# resolved by the documented reasons in MAPPING_REASONS.
CATEGORY_TABLE = {
    "data_science": "data-processing-scripting",
    "data-science": "data-processing-scripting",
    "data_engineering": "data-processing-scripting",
    "data-processing": "data-processing-scripting",
    "file-operations": "data-processing-scripting",
    "file_operations": "data-processing-scripting",
    "parsing": "data-processing-scripting",
    "sql": "data-processing-scripting",
    "databases": "data-processing-scripting",
    "bash_scripting": "data-processing-scripting",
    "net-normalization": "data-processing-scripting",
    "market_analysis": "data-processing-scripting",
    "security": "security-cryptography",
    "cybersecurity": "security-cryptography",
    "forensics": "security-cryptography",
    "sql_injection_forensics": "security-cryptography",
    "systems": "security-cryptography",
    "software-engineering": "software-engineering",
    "software_testing": "software-engineering",
    "backend": "software-engineering",
    "backend-engineering": "software-engineering",
    "machine-learning": "machine-learning-ai",
    "image_analysis": "machine-learning-ai",
    "CP": "interactive-games",
    "chess_analysis": "interactive-games",
    "geometry": "interactive-games",
    "debugging": "debugging-troubleshooting",
    "bug_fix": "debugging-troubleshooting",
    "scientific-computing": "scientific-computing",
    "programming": "scientific-computing",
    "system-administration": "system-setup-configuration",
    "system_administration": "system-setup-configuration",
    "networking": "system-setup-configuration",
    "build-and-dependency-management": "build-dependency-management",
}

# The raw label "general" carries no signal; these explicit per-task
# overrides (and only these) assign the eleven "general" tasks.
GENERAL_OVERRIDES = {
    "game-of-stones": "interactive-games",
    "grid-pathfinding": "interactive-games",
    "parking-lot-pathfinding": "interactive-games",
    "permutation-construction-100k": "interactive-games",
    "bracket-sequence-restoration": "interactive-games",
    "anomaly-detection-ranking": "scientific-computing",
    "battery-charging-optimization": "system-setup-configuration",
    "html-index-analysis": "data-processing-scripting",
    "multi-server-configuration": "system-setup-configuration",
    "sales-data-csv-analysis": "data-processing-scripting",
    "server-log-analysis": "debugging-troubleshooting",
}

# Raw labels with no defensible canonical bucket, explicitly left
# UNMAPPED (task stays ineligible). Fail-closed continuity with the
# frozen v1 protocol: v1 already excluded these tasks; a new mapping
# for them would change the selected ids and is therefore forbidden.
FROZEN_UNMAPPED = {"algorithms"}

# Documented reasons for ambiguous judgment calls (audit trail for the
# project-proposed mapping; unlisted labels follow their evident names).
MAPPING_REASONS = {
    "systems": "symlink-chain-traversal is a filesystem-attack task; grouped with security",
    "networking": "iot-device-registration-server is service/TLS setup; grouped with system setup",
    "image_analysis": "image-tile-identification and playing-card-recognition are perception tasks; grouped with ML/AI",
    "backend": "implement-a-service tasks; grouped with software engineering",
    "backend-engineering": "all four are fix-the-bug service tasks; grouped with software engineering (debugging stays reserved for raw 'debugging'/'bug_fix')",
    "programming": "protein-sequence and smiles-data-lab are scientific data formats; grouped with scientific computing",
    "market_analysis": "publisher-market analyses are data-processing over tabular data",
    "general": "raw label carries no signal; per-task overrides above assign all eleven tasks",
    "algorithms": "schedule-vacation is a generic constraint-satisfaction task with no defensible bucket; left ineligible by the frozen v1 protocol (mapped category counts in v1 excluded it), and re-mapping now would change the frozen selection",
}

CANONICAL_CATEGORIES = [
    "data-processing-scripting",
    "security-cryptography",
    "software-engineering",
    "machine-learning-ai",
    "interactive-games",
    "debugging-troubleshooting",
    "scientific-computing",
    "system-setup-configuration",
    "build-dependency-management",
]

# Fixed allocation: 3 Data + 3 Security + 2 x each of the other seven = 20.
ALLOCATION = {
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

# Eligibility rules, declared before outcomes.
MAX_TIMEOUT_SEC = 3600.0
ELIGIBLE_DIFFICULTIES = {"easy", "medium"}
GPU_MARKERS = ("nvidia", "cuda")

# Documented irrelevant caches excluded from every content hash.
HASH_EXCLUDE_PARTS = {"__pycache__", ".git", ".pytest_cache"}


def utc_now():
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def fail(reason, detail=""):
    print(json.dumps({"decision": "fail", "reason": reason, "detail": detail}))
    sys.exit(1)


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_sha(path):
    path = Path(path)
    if not path.exists():
        return None
    if path.is_file():
        return file_sha(path)
    h = hashlib.sha256()
    files = sorted(
        p for p in path.rglob("*")
        if p.is_file() and not any(part in HASH_EXCLUDE_PARTS for part in p.parts)
    )
    for p in files:
        h.update(p.relative_to(path).as_posix().encode())
        h.update(b"\0")
        h.update(file_sha(p).encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def observe_checkout(dataset_root):
    """Return (observed_head, clean) for the dataset checkout.

    Never a constant: qualified mode compares the OBSERVED head against
    the expected pin. clean=False when any tracked file is modified.
    """
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(dataset_root), *args],
            capture_output=True, text=True, check=False, timeout=30,
        )
    head = git("rev-parse", "HEAD")
    if head.returncode != 0:
        return None, False
    status = git("status", "--porcelain")
    clean = status.returncode == 0 and not status.stdout.strip()
    return head.stdout.strip(), clean


def canonical_category(task_id, raw):
    if raw == "general":
        return GENERAL_OVERRIDES.get(task_id)
    return CATEGORY_TABLE.get(raw)


def build_task_entry(d):
    tf = d / "task.toml"
    cfg = tomllib.loads(tf.read_text())
    md = cfg.get("metadata", {})
    raw = md.get("category")
    canonical = canonical_category(d.name, raw)
    dockerfile = d / "environment" / "Dockerfile"
    gpu = False
    if dockerfile.is_file():
        text = dockerfile.read_text(errors="replace").lower()
        gpu = any(m in text for m in GPU_MARKERS)
    agent_timeout = cfg.get("agent", {}).get("timeout_sec")
    verifier_timeout = cfg.get("verifier", {}).get("timeout_sec")
    difficulty = (md.get("difficulty") or "").lower()
    eligible = (
        canonical in CANONICAL_CATEGORIES
        and difficulty in ELIGIBLE_DIFFICULTIES
        and dockerfile.is_file()
        and not gpu
        and (agent_timeout is None or agent_timeout <= MAX_TIMEOUT_SEC)
        and (verifier_timeout is None or verifier_timeout <= MAX_TIMEOUT_SEC)
    )
    return {
        "id": d.name,
        "raw_category": raw,
        "canonical_category": canonical,
        "difficulty": difficulty,
        "agent_timeout_sec": agent_timeout,
        "verifier_timeout_sec": verifier_timeout,
        "gpu": gpu,
        "eligible": eligible,
        # Complete content-addressed binding of everything Harbor
        # uploads/builds from this task, not only the Dockerfile.
        "task_tree_sha256": tree_sha(d),
        "sha256": {
            "task_toml": file_sha(tf),
            "dockerfile": tree_sha(dockerfile),
            "tests": tree_sha(d / "tests"),
            "solution": tree_sha(d / "solution"),
            "instruction": tree_sha(d / "instruction.md"),
        },
    }


def build_manifest(dataset_root):
    tasks = []
    for d in sorted(p for p in dataset_root.iterdir() if p.is_dir()):
        if (d / "task.toml").is_file():
            tasks.append(build_task_entry(d))
    return tasks


def select_subset(manifest, seed):
    pools = {}
    for cat in CANONICAL_CATEGORIES:
        eligible = [t["id"] for t in manifest if t["eligible"] and t["canonical_category"] == cat]
        ordered = sorted(
            eligible,
            key=lambda tid: hashlib.sha256(f"{seed}|{tid}".encode()).hexdigest(),
        )
        take = min(ALLOCATION[cat], len(ordered))
        pools[cat] = {
            "eligible_count": len(ordered),
            "allocated": ALLOCATION[cat],
            "selected_count": take,
            "hash_order": ordered,
            "selected": ordered[:take],
        }
    shortfall = sum(ALLOCATION[c] - len(pools[c]["selected"]) for c in CANONICAL_CATEGORIES)
    return {
        "seed": seed,
        "allocation": ALLOCATION,
        "shortfall_tasks": shortfall,
        "total_selected": sum(len(p["selected"]) for p in pools.values()),
        "pools": pools,
        "selected_ids": sorted(i for p in pools.values() for i in p["selected"]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--selection", default="")
    ap.add_argument("--expected-commit", default=EXPECTED_COMMIT)
    ap.add_argument("--seed", default=SEED_DEFAULT)
    ap.add_argument("--generic", action="store_true",
                    help="UNQUALIFIED mode for fixtures: manifest only, never a selection")
    ap.add_argument("--expect-selection", default="",
                    help="prior frozen selection JSON; new ids must match it exactly")
    args = ap.parse_args()

    root = Path(args.dataset).resolve()
    if not root.is_dir():
        fail("dataset_missing", str(root))

    observed_head, clean = observe_checkout(root)

    if args.generic:
        if args.selection:
            fail("generic_mode_cannot_freeze_selection")
        manifest_doc = {
            "mode": "generic-unqualified",
            "qualified": False,
            "dataset": "unversioned-generic-input",
            "observed_dataset_commit": observed_head,
            "expected_dataset_commit": args.expected_commit,
            "declared_note": "fixture manifest; explicitly NOT the verified pinned corpus; no fast-subset selection is valid from this input",
            "frozen_at_utc": utc_now(),
            "tasks": build_manifest(root),
        }
        Path(args.manifest).write_text(json.dumps(manifest_doc, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"decision": "ok", "mode": "generic-unqualified",
                          "qualified": False, "tasks": len(manifest_doc["tasks"])}))
        return

    # Qualified mode: fail closed on provenance, completeness, mapping
    # and allocation before writing anything.
    if observed_head is None:
        fail("dataset_not_a_git_checkout",
             "qualified freeze requires a git checkout; use --generic for fixtures")
    if observed_head != args.expected_commit:
        fail("dataset_commit_mismatch",
             f"observed {observed_head} != expected {args.expected_commit}")
    if not clean:
        fail("dataset_dirty", "git status is not clean")
    if not COMMIT_RE.match(args.expected_commit):
        fail("invalid_expected_commit", args.expected_commit)

    manifest = build_manifest(root)
    if len(manifest) != EXPECTED_TASK_COUNT:
        fail("task_count_mismatch",
             f"{len(manifest)} tasks != pinned corpus size {EXPECTED_TASK_COUNT}")
    unmapped = [
        t["id"] for t in manifest
        if t["canonical_category"] not in CANONICAL_CATEGORIES
        and (t["raw_category"] not in FROZEN_UNMAPPED or t["canonical_category"] is not None)
    ]
    if unmapped:
        fail("uncanonical_category", ", ".join(unmapped))

    selection = select_subset(manifest, args.seed)
    if selection["shortfall_tasks"] != 0 or selection["total_selected"] != 20:
        fail("allocation_shortfall",
             f"shortfall={selection['shortfall_tasks']} selected={selection['total_selected']}")

    if args.expect_selection:
        prior = json.loads(Path(args.expect_selection).read_text())
        prior_ids = sorted(prior.get("selected_ids") or
                           [i for p in (prior.get("pools") or {}).values() for i in p.get("selected", [])])
        if prior_ids and prior_ids != selection["selected_ids"]:
            fail("selection_continuity_violation",
                 "new selection differs from the prior frozen selection; "
                 "selected tasks are never replaced after outcomes")

    manifest_doc = {
        "mode": "qualified",
        "qualified": True,
        "dataset": "open-thoughts/OpenThoughts-TBLite",
        "observed_dataset_commit": observed_head,
        "expected_dataset_commit": args.expected_commit,
        "checkout_clean": clean,
        "task_count": len(manifest),
        "tasks": manifest,
    }
    Path(args.manifest).write_text(json.dumps(manifest_doc, indent=2, sort_keys=True) + "\n")

    selection_doc = {
        "mode": "qualified",
        "qualified": True,
        "dataset_commit": observed_head,
        "seed": args.seed,
        "category_mapping": {
            "project_proposed": True,
            "official_taxonomy": False,
            "note": "coarse 9-bucket mapping proposed by this project; the dataset publishes aggregates only",
            "reasons": MAPPING_REASONS,
            "general_overrides": GENERAL_OVERRIDES,
        },
        "rules": {
            "canonical_categories": CANONICAL_CATEGORIES,
            "category_table": CATEGORY_TABLE,
            "eligibility": {
                "difficulty_in": sorted(ELIGIBLE_DIFFICULTIES),
                "max_timeout_sec": MAX_TIMEOUT_SEC,
                "dockerfile_required": True,
                "gpu_forbidden": True,
            },
            "ordering": "ascending sha256hex of utf8 '<seed>|<task_id>'",
            "no_replacement_after_outcomes": True,
            "task_binding": "task_tree_sha256 (complete task tree, caches excluded)",
        },
        **{k: v for k, v in selection.items() if k != "pools"},
        "pools": selection["pools"],
    }
    if args.selection:
        Path(args.selection).write_text(json.dumps(selection_doc, indent=2, sort_keys=True) + "\n")

    print(json.dumps({
        "decision": "ok", "mode": "qualified", "qualified": True,
        "observed_dataset_commit": observed_head,
        "tasks": len(manifest), "eligible": sum(1 for t in manifest if t["eligible"]),
        "selected": selection["total_selected"], "shortfall": 0,
    }))


if __name__ == "__main__":
    main()
