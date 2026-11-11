#!/usr/bin/env python3
"""pre_evolution_snapshot.py -- Phase 6.4 pre-evolution snapshot and safe restore wrapper.

W02 revision of the staged W01 wrapper (review CHANGES REQUIRED implemented).
Wraps the INSTALLED native Hermes backup/restore backend; it does not
re-implement it.

Native backend (hermes_cli/backup.py in the installed checkout, default
<$ROOT>/hermes-agent):
  - full home backup:  run_backup(Namespace(output=<zip>, keep=0))
  - full home restore: run_import(Namespace(zipfile=<zip>, force=True))
  - quick state snapshots (hermes backup --quick) cover critical state files
    only -- NOT skills -- and are therefore never used for skill rollback.

W02 changes (see PACKET.md section 2):
  - the "skills" scope now covers profiles/<name>/skills/ trees dynamically
    (no per-agent name list), for restore, added-file detection, pruning,
    coverage counts and parity;
  - runtime/default paths resolve from HERMES_HOME / platform default and
    explicit flags (generic source; operator-specific paths live in the
    deployment manifest, not in this file);
  - the Hindsight hot-tar option was REMOVED: a raw live tar does not
    establish a consistent restorable database. The memory-recovery point is
    now fail-closed: a snapshot receipt records gate "open" unless a verified
    logical-dump proof receipt is supplied (--hindsight-proof). The verified
    route is the HS2-validated pg_dump -Fc / pg_restore-into-scratch-pg0
    procedure (tools/hindsight_logical_backup.sh in this package).

The application launcher is NOT used for foreign HERMES_HOME values (it
starts a mutating dependency bootstrap that can hang). The backend module is
imported in-process by the PM-managed interpreter resolved from the installed
runtime's installs/*/facts.json, verified by a real import probe; override
with --native-python. Only the launcher bootstrap is bypassed -- all native
validation, locking and exclusion policy runs unchanged.

Receipts contain path/size/sha256/status only. ASCII only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, NoReturn, Optional

# --------------------------------------------------------------------------
# Path resolution (generic; operator-specific values come from flags/env)
# --------------------------------------------------------------------------

def platform_default_root() -> Path:
    """Platform-default Hermes root: HERMES_HOME env, else ~/.hermes."""
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(os.path.abspath(os.path.expanduser(env)))
    return Path.home() / ".hermes"


def _resolve(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _home_root(home: Path) -> Path:
    """Root home for a given home (profiles map up one level, mirroring the
    native get_default_hermes_root behavior for custom roots)."""
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Native backup excludes SQLite sidecars; *.db members are page images taken
# through sqlite3.backup(), so a torn WAL/SHM pairing cannot occur.
SECRET_NAMES = {".env", "auth.json", "vault.key", "vault.json.enc", "state.db"}

ARTIFACT_PREFIX = "pre-evolution-"

# profiles/<name>/skills/... (profile-local skill trees, W02)
_PROFILE_SKILLS_RE = re.compile(r"^profiles/[^/]+/skills(/|$)")

# Bridge source executed by the resolved native interpreter: imports the
# native backend and calls the same entry points the CLI dispatches to,
# with identical namespace semantics.
BRIDGE_SOURCE = r"""
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
from types import SimpleNamespace
import hermes_cli.backup as B
mode = sys.argv[2]
if mode == "backup":
    ok = B.run_backup(SimpleNamespace(output=sys.argv[3], keep=0))
    sys.exit(0 if ok else 1)
elif mode == "import":
    rc = B.run_import(SimpleNamespace(zipfile=sys.argv[3], force=True))
    sys.exit(int(rc) if rc else 0)
elif mode == "probe":
    print("BACKEND_OK %s" % hasattr(B, "run_backup"))
    sys.exit(0)
sys.exit(2)
"""


def _probe_interpreter(python_bin: Path, repo: Path, timeout: int = 600) -> bool:
    """True when python_bin can import the native backup backend from repo
    (verified by really doing it, not by assuming). The probe imports the
    full hermes_cli package with bytecode writing disabled, which can take
    well over a minute on a cold filesystem cache -- hence the generous
    default timeout."""
    env = os.environ.copy()
    probe_home = tempfile.mkdtemp(prefix="w02-probe-")
    env["HERMES_HOME"] = probe_home
    try:
        proc = subprocess.run(
            [str(python_bin), "-I", "-c", BRIDGE_SOURCE, str(repo), "probe"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=timeout, check=False)
        return proc.returncode == 0 and "BACKEND_OK True" in (proc.stdout or "")
    except (subprocess.TimeoutExpired, OSError):
        return False
    finally:
        shutil.rmtree(probe_home, ignore_errors=True)


def resolve_native_runtime(repo: Path, installs_root: Path,
                           override: Optional[str] = None) -> dict:
    """Resolve {python, repo, resolved_via} able to import the native backup
    backend. Order: explicit override -> PM environments declared in
    installs/*/facts.json -> any installs/*/environments/*/venv. Every
    candidate is verified with a real import probe."""
    candidates: list[tuple[str, str]] = []
    if override:
        candidates.append(("override", override))
    if installs_root.is_dir():
        for facts in sorted(installs_root.glob("*/facts.json")):
            try:
                doc = json.loads(facts.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            venv = (((doc.get("packages") or {}).get("venv")) or {}).get("environment")
            if venv:
                candidates.append(("facts.json:" + facts.parent.name, str(venv)))
        for venv in sorted(installs_root.glob("*/environments/*/venv")):
            candidates.append(("environments-scan", str(venv)))
    for via, venv in candidates:
        # a candidate may be a venv root OR a direct interpreter path
        if Path(venv).is_file():
            python_bin = Path(venv)
        else:
            python_bin = Path(venv) / "bin" / "python3"
        if not python_bin.is_file():
            continue
        if _probe_interpreter(python_bin, repo):
            return {"python": str(python_bin), "repo": str(repo), "resolved_via": via}
    print("ERROR: no interpreter with the native backup backend found "
          "(installs_root={}; tried: {})".format(installs_root, ", ".join("{}={}".format(*c) for c in candidates)))
    raise SystemExit(3)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def live_root_paths(explicit: list[str]) -> list[Path]:
    """Protected live roots: platform default root, the root of any home this
    process operates on (added by callers), plus operator --live-root values.
    A destination IS, is INSIDE, or CONTAINS any of these -> live write."""
    roots = [platform_default_root()]
    for value in explicit:
        if value:
            roots.append(_resolve(value))
    out = []
    for root in roots:
        try:
            resolved = root.resolve(strict=False)
        except OSError:
            continue
        if resolved not in out:
            out.append(resolved)
    return out


def _is_unsafe_destination(dest: Path, live_roots: list[Path]) -> Optional[str]:
    try:
        resolved = dest.resolve(strict=False)
    except OSError:
        return "cannot resolve destination"
    for live in live_roots:
        if resolved == live:
            return f"destination is the live home {live}"
        if live in resolved.parents:
            return f"destination is inside the live home {live}"
        if resolved in live.parents:
            return f"destination contains the live home {live}"
    return None


def _valid_label(label: str) -> bool:
    return bool(LABEL_RE.match(label)) and ".." not in label


def _valid_member_rel(rel: str) -> bool:
    if not rel or rel.startswith("/") or "\\" in rel:
        return False
    if len(rel) > 2 and rel[1] == ":":  # drive letter (win-style)
        return False
    parts = rel.split("/")
    return all(p not in ("", ".", "..") for p in parts)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    with open(tmp, "w", encoding="ascii") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _fail(msg: str, code: int = 2) -> NoReturn:
    print(f"ERROR: {msg}")
    raise SystemExit(code)


def _git_identity(repo: Path) -> dict:
    def git(*args: str) -> str:
        try:
            proc = subprocess.run(["git", "-C", str(repo), *args],
                                  capture_output=True, text=True, timeout=30,
                                  check=False)
            return proc.stdout.strip() if proc.returncode == 0 else ""
        except (subprocess.TimeoutExpired, OSError):
            return ""
    return {"git_head": git("rev-parse", "HEAD"),
            "git_describe": git("describe", "--tags", "--always"),
            "git_branch": git("rev-parse", "--abbrev-ref", "HEAD")}


# --------------------------------------------------------------------------
# Artifact inspection
# --------------------------------------------------------------------------

def _strip_common_prefix(names: list[str]) -> tuple[list[str], Optional[str]]:
    if not names or "/" not in names[0]:
        return names, None
    first = names[0].split("/")[0]
    if all(n.startswith(first + "/") for n in names):
        stripped = [n.split("/", 1)[1] for n in names]
        if all(_valid_member_rel(n) for n in stripped):
            return stripped, first + "/"
    return names, None


def _artifact_members(zf: zipfile.ZipFile) -> tuple[dict[str, zipfile.ZipInfo], Optional[str], list[str]]:
    raw = [n for n in zf.namelist() if not n.endswith("/")]
    stripped, prefix = _strip_common_prefix(raw)
    members: dict[str, zipfile.ZipInfo] = {}
    rejected: list[str] = []
    for rel, orig in zip(stripped, raw):
        if not _valid_member_rel(rel):
            rejected.append(orig)
            continue
        if rel.startswith("_external/"):
            continue  # outside-HERMES_HOME provider state: never in scoped rollback
        members[rel] = zf.getinfo(orig)
    return members, prefix, rejected


def _is_skills_path(rel: str) -> bool:
    """Root skills tree OR any profile-local skills tree (dynamic; no
    per-agent name list)."""
    return rel == "skills" or rel.startswith("skills/") or bool(_PROFILE_SKILLS_RE.match(rel))


def _is_config_path(rel: str) -> bool:
    if rel in ("config.yaml", "SOUL.md") or rel.startswith("agent-hooks/"):
        return True
    parts = rel.split("/")
    return len(parts) >= 3 and parts[0] == "profiles" and parts[2] in ("config.yaml", "SOUL.md")


def _scope_predicates() -> dict[str, Callable[[str], bool]]:
    return {"skills": _is_skills_path, "config": _is_config_path}


SCOPE_CHOICES = ("skills", "config")


def _scope_members(members: dict[str, zipfile.ZipInfo], scopes: list[str]) -> dict[str, zipfile.ZipInfo]:
    preds = _scope_predicates()
    out: dict[str, zipfile.ZipInfo] = {}
    for scope in scopes:
        pred = preds[scope]
        for rel, info in members.items():
            if pred(rel):
                out[rel] = info
    return out


def _in_any_scope(rel: str, scopes: list[str]) -> bool:
    preds = _scope_predicates()
    return any(preds[s](rel) for s in scopes)


def _coverage_summary(members: dict[str, zipfile.ZipInfo]) -> dict:
    def count(pred: Callable[[str], bool]) -> int:
        return sum(1 for rel in members if pred(rel))

    profile_roots = {rel.split("/")[1] for rel in members
                     if rel.startswith("profiles/") and len(rel.split("/")) > 2}
    profiles_with_skills = {rel.split("/")[1] for rel in members
                            if _PROFILE_SKILLS_RE.match(rel)}
    return {
        "total_members": len(members),
        "skills_files": count(_is_skills_path),
        "root_skills_files": count(lambda r: r.startswith("skills/")),
        "root_skills_archive_files": count(lambda r: r.startswith("skills/.archive/")),
        "profile_skills_files": count(lambda r: bool(_PROFILE_SKILLS_RE.match(r))),
        "profiles_with_skills": sorted(profiles_with_skills),
        "profile_skills_archive_files": count(
            lambda r: bool(re.match(r"^profiles/[^/]+/skills/\.archive/", r))),
        "config_yaml": count(lambda r: r == "config.yaml" or r.endswith("/config.yaml")),
        "soul_md": count(lambda r: r == "SOUL.md" or r.endswith("/SOUL.md")),
        "agent_hooks_files": count(lambda r: r.startswith("agent-hooks/")),
        "cron_files": count(lambda r: r.startswith("cron/")),
        "databases": count(lambda r: r.endswith(".db")),
        "state_db_present": "state.db" in members,
        "env_present": ".env" in members,
        "profiles_present": sorted(profile_roots),
        "external_provider_files": count(lambda r: r.startswith("_external/")),
    }


# --------------------------------------------------------------------------
# Native backend invocation
# --------------------------------------------------------------------------

def _run_bridge(native: dict, mode: str, arg: str, env_home: str,
                timeout: int) -> tuple[int, str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = env_home
    try:
        proc = subprocess.run(
            [native["python"], "-I", "-c", BRIDGE_SOURCE, native["repo"], mode, arg],
            env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        return 124, f"timeout after {timeout}s: {exc}"
    except OSError as exc:
        return 127, str(exc)


# --------------------------------------------------------------------------
# Memory-recovery gate (W03: fail closed, typed/versioned, fast-fail)
# --------------------------------------------------------------------------

PROOF_SCHEMA_VERSION = 2
REQUIRED_DIGEST_TABLES = ("banks", "documents", "memory_units",
                          "memory_links", "unit_entities", "entities",
                          "alembic_version")
_SNAPSHOT_ID_RE = re.compile(r"^[0-9A-F]+-[0-9A-F]+-[0-9A-F]+$")


def _parse_iso(ts: str):
    try:
        from datetime import datetime
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def _check_digest_table(bad, tables: dict, which: str, table: str) -> None:
    entry = tables.get(table)
    if not isinstance(entry, dict):
        bad(f"digest set {which} is missing table: {table}")
        return
    rows = entry.get("rows")
    if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
        bad(f"digest set {which} table {table} has invalid rows {rows!r}")
    for field in ("sum_hash", "xor_hash"):
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            bad(f"digest set {which} table {table} has invalid {field}")


def _proof_load(bad: Callable[[str], NoReturn], proof_path: Path) -> dict:
    """Load and type-check the proof receipt file itself."""
    if not proof_path.is_file():
        bad("proof receipt file not found")
    try:
        proof = json.loads(proof_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        bad(f"proof receipt unreadable ({exc})")
    if not isinstance(proof, dict):
        bad("proof receipt is not an object")
    return proof


def _proof_contract(bad: Callable[[str], NoReturn], proof: dict) -> None:
    """Typed/versioned proof contract: schema, kind, verdict."""
    if proof.get("proof_schema") != PROOF_SCHEMA_VERSION:
        bad(f"unsupported proof_schema (want {PROOF_SCHEMA_VERSION})")
    if proof.get("kind") != "hindsight-logical-backup-proof":
        bad("wrong proof kind {!r}".format(proof.get("kind")))
    if proof.get("verdict") != "verified":
        bad("verdict is {!r}, not 'verified'".format(proof.get("verdict")))


def _proof_dump_identity(bad: Callable[[str], NoReturn],
                         proof: dict) -> tuple[Path, str, int]:
    """The dump artifact must exist with matching size and digest."""
    dump = proof.get("dump")
    if not isinstance(dump, dict):
        bad("missing dump section")
    dump_path = dump.get("path")
    dump_bytes = dump.get("bytes")
    dump_sha = dump.get("sha256")
    if not dump_path or not isinstance(dump_bytes, int) or dump_bytes <= 0 \
            or not (isinstance(dump_sha, str) and len(dump_sha) == 64):
        bad("dump identity malformed (need path, positive bytes, sha256)")
    dpath = _resolve(str(dump_path))
    if not dpath.is_file():
        bad(f"dump artifact does not exist: {dpath}")
    if dpath.stat().st_size != dump_bytes:
        bad(f"dump size mismatch: receipt {dump_bytes}, actual {dpath.stat().st_size}")
    actual_sha = _sha256_file(dpath)
    if actual_sha != dump_sha.lower():
        bad(f"dump sha256 mismatch: receipt {dump_sha}, actual {actual_sha}")
    return dpath, actual_sha, dump_bytes


def _proof_restore_evidence(bad: Callable[[str], NoReturn], proof: dict) -> dict:
    """Explicit successful restore evidence."""
    restore = proof.get("restore")
    if not isinstance(restore, dict):
        bad("missing restore section")
    if restore.get("outcome") != "success":
        bad("restore outcome is {!r}, not 'success'".format(restore.get("outcome")))
    if restore.get("pg_restore_rc") != 0:
        bad("pg_restore rc is {!r}".format(restore.get("pg_restore_rc")))
    if restore.get("digests_match") is not True:
        bad("restored content digests do not match the exported snapshot")
    return restore


def _proof_digest_consistency(bad: Callable[[str], NoReturn], proof: dict) -> dict:
    """Internal consistency (W04): never trust the summary flags alone --
    typed source and restored digest sets must both be present, complete
    and EQUAL, mismatch/missing sets must be present and empty, and the
    schema digest evidence must match."""
    digests = proof.get("digests")
    if not isinstance(digests, dict):
        bad("missing content digests")
    tables = digests.get("tables")
    restored_tables = digests.get("restored_tables")
    if not isinstance(tables, dict) or not isinstance(restored_tables, dict):
        bad("digest section must carry typed source AND restored table sets")
    for table in REQUIRED_DIGEST_TABLES:
        _check_digest_table(bad, tables, "source", table)
        _check_digest_table(bad, restored_tables, "restored", table)
    if set(tables) != set(restored_tables):
        bad(f"source and restored digest table sets differ ({sorted(tables)} vs {sorted(restored_tables)})")
    for table in sorted(tables):
        if tables[table] != restored_tables[table]:
            bad("restored digest differs from source digest for table {} "
                "(rows {!r} vs {!r})".format(table, tables[table].get("rows"),
                                     restored_tables[table].get("rows")))

    schema = digests.get("schema")
    restored_schema = digests.get("restored_schema")
    if not isinstance(schema, dict) or not isinstance(restored_schema, dict):
        bad("schema digest and restored schema digest are both required")
    if schema != restored_schema:
        bad("restored schema digest differs from the source schema digest")

    missing = digests.get("missing")
    mismatch = digests.get("mismatch")
    if not isinstance(missing, list) or not isinstance(mismatch, list):
        bad("digest missing/mismatch sets must be present lists")
    if missing:
        bad(f"digest missing set is not empty: {missing[:5]}")
    if mismatch:
        bad(f"digest mismatch set is not empty: {mismatch[:5]}")
    return tables


def _proof_export_binding(bad: Callable[[str], NoReturn],
                          proof: dict) -> tuple[str, str]:
    """Explicit export-snapshot binding."""
    export = proof.get("export")
    if not isinstance(export, dict):
        bad("export (snapshot binding) section is absent")
    snap_id = export.get("snapshot_id")
    if not isinstance(snap_id, str) or not _SNAPSHOT_ID_RE.match(snap_id):
        bad("export.snapshot_id missing/malformed (the digests and the dump "
            "must be bound to one exported snapshot; historical receipts "
            "without this binding are mechanism evidence only)")
    method = str(export.get("method", ""))
    if "exported snapshot" not in method:
        bad("export.method does not describe an exported-snapshot binding")
    return snap_id, method


def _proof_instance_binding(bad: Callable[[str], NoReturn], proof: dict,
                            restore: dict,
                            expected_container: str) -> tuple[str, str, str, str]:
    """Source INSTANCE + image identity binding (not merely image/db name:
    HS1 and HS2 share image tag and database name)."""
    source = proof.get("source")
    if not isinstance(source, dict):
        bad("missing source section")
    if source.get("db") != "hindsight":
        bad("proof is for database {!r}, not the Hindsight server".format(source.get("db")))
    src_container = source.get("container")
    if not isinstance(src_container, str) or not src_container.strip():
        bad("proof does not name its source container instance")
    if src_container != expected_container:
        bad(f"proof source instance {src_container!r} is not the intended container {expected_container!r} "
            "(same image/db name is not sufficient: HS1 and HS2 share them)")
    src_image = source.get("container_image")
    tgt_image = (restore or {}).get("image")
    if not src_image or tgt_image != src_image:
        bad(f"restore image {tgt_image!r} does not match source image {src_image!r}")
    src_id = source.get("image_id")
    tgt_id = (restore or {}).get("image_id")
    if not src_id or tgt_id != src_id:
        bad(f"immutable image identity mismatch ({src_id!r} vs {tgt_id!r})")
    return src_container, src_image, src_id, tgt_image


def _proof_run_binding(bad: Callable[[str], NoReturn], proof: dict,
                       run_label: Optional[str]) -> object:
    """Explicit run binding: WHEN a run label is requested, the proof MUST
    carry a present, nonempty, exactly matching run_label. An unbound
    (null/missing/empty) proof is acceptable ONLY for mechanism/home
    snapshot use where NO run was requested (W05 fix: the previous
    both-present check silently accepted unbound proofs for a named run)."""
    proof_run = proof.get("run_label")
    if run_label:
        if not isinstance(proof_run, str) or not proof_run.strip():
            bad(f"a run label ({run_label!r}) was requested but the proof carries no "
                "run_label; an unbound proof cannot authorize a named "
                "learning run")
        if proof_run != run_label:
            bad(f"proof was minted for run {proof_run!r}, not this run {run_label!r}")
    return proof_run


def _proof_freshness(bad: Callable[[str], NoReturn], proof: dict,
                     max_age_hours: float) -> tuple[object, float]:
    """Freshness binding."""
    created = _parse_iso(str(proof.get("created_at", "")))
    if created is None:
        bad("created_at missing/malformed")
    age_h = (datetime.now(UTC) - created.replace(tzinfo=UTC)
             ).total_seconds() / 3600.0
    if age_h < -1:
        bad("created_at is in the future")
    if age_h > max_age_hours:
        bad(f"proof is stale: {age_h:.1f}h old (max {max_age_hours:.1f}h)")
    return created, age_h


def _load_memory_proof(proof_path: Path, max_age_hours: float,
                       expected_container: str,
                       run_label: Optional[str]) -> dict:
    """Strict, INTERNALLY CONSISTENT validation of a proof_schema-2 Hindsight
    logical-backup proof (W04/W05); every failure is fatal (exit 3) BEFORE
    any expensive capture. Phase checks live in the _proof_* helpers;
    receipt fields used here are metadata/hashes/counts only -- memory
    contents and credentials never enter this code path."""
    import math

    def bad(reason: str) -> NoReturn:
        _fail(f"memory-recovery proof rejected: {reason} ({proof_path})", code=3)

    if not isinstance(max_age_hours, (int, float)) or isinstance(max_age_hours, bool) \
            or not math.isfinite(float(max_age_hours)) or max_age_hours <= 0:
        _fail("--max-proof-age-hours must be a finite positive number", code=3)
    if not expected_container or not str(expected_container).strip():
        _fail("intended Hindsight container is not configured: pass "
              "--hindsight-container (explicit CLI; behavior env vars are "
              "not accepted for non-secret configuration)", code=3)

    proof = _proof_load(bad, proof_path)
    _proof_contract(bad, proof)
    dpath, actual_sha, dump_bytes = _proof_dump_identity(bad, proof)
    restore = _proof_restore_evidence(bad, proof)
    tables = _proof_digest_consistency(bad, proof)
    snap_id, method = _proof_export_binding(bad, proof)
    src_container, src_image, src_id, tgt_image = _proof_instance_binding(
        bad, proof, restore, expected_container)
    proof_run = _proof_run_binding(bad, proof, run_label)
    _created, age_h = _proof_freshness(bad, proof, max_age_hours)

    return {
        "proof_receipt": str(proof_path),
        "proof_schema": PROOF_SCHEMA_VERSION,
        "dump": {"path": str(dpath), "sha256": actual_sha, "bytes": dump_bytes},
        "source": {"container": src_container,
                   "container_image": src_image,
                   "image_id": src_id,
                   "postgres_version": proof.get("source", {}).get("postgres_version")},
        "export": {"snapshot_id": snap_id, "method": method},
        "restore": {"image": tgt_image, "outcome": "success",
                    "digests_match": True,
                    "consistency_checked": True},
        "run_label": proof_run,
        "created_at": proof.get("created_at"),
        "age_hours": round(age_h, 3),
        "covered_tables": sorted(tables),
    }


def _memory_gate(args: argparse.Namespace) -> tuple[dict, bool]:
    """Resolve the full-memory precondition. Returns (field, ok).
    ok=False -> caller must fail BEFORE any expensive capture. The only
    proof-free success is the explicit home-only diagnostic mode, which
    records that it CANNOT authorize evolution."""
    if args.hindsight_proof:
        return _load_memory_proof(
            _resolve(args.hindsight_proof),
            args.max_proof_age_hours,
            (args.hindsight_container or "").strip(),
            args.evolution_run), True
    if args.allow_open_memory_gate:
        return {
            "gate": "open",
            "covered_by_home_artifact": False,
            "mode": "home-only-diagnostic",
            "cannot_authorize_evolution": True,
            "note": "No verified Hindsight logical-backup proof was "
                    "supplied. This snapshot is home-only diagnostic "
                    "evidence and must not authorize any evolution run.",
        }, True
    return {
        "gate": "open",
        "covered_by_home_artifact": False,
        "note": "No verified memory-recovery proof supplied. The "
                "pre-evolution entry fails closed: pass --hindsight-proof "
                "<receipt> (verified logical dump) or the explicit "
                "--allow-open-memory-gate home-only diagnostic flag.",
    }, False


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------

def cmd_snapshot(args: argparse.Namespace) -> int:
    home = _resolve(args.home)
    root = _home_root(home)
    backups_dir = _resolve(args.backups_dir)
    repo = _resolve(args.hermes_repo)
    installs_root = _resolve(args.installs_root)

    if not _valid_label(args.label):
        _fail("invalid label (allowed: [A-Za-z0-9][A-Za-z0-9._-]{0,63}, no '..')")
    if not home.is_dir():
        _fail(f"home directory not found: {home}")
    if not repo.is_dir():
        _fail(f"installed checkout not found: {repo} (set --hermes-repo)")
    if not installs_root.is_dir():
        _fail(f"installs root not found: {installs_root} (set --installs-root)")
    backups_dir.mkdir(parents=True, exist_ok=True)

    artifact = backups_dir / (f"{ARTIFACT_PREFIX}{args.label}.zip")
    if artifact.exists():
        _fail(f"artifact already exists (refusing to overwrite): {artifact}")

    # Full-memory precondition validated FIRST: absent/invalid/stale/
    # wrong-target proof fails nonzero before any expensive capture.
    memory_recovery, gate_ok = _memory_gate(args)
    if not gate_ok:
        print("ERROR: full-memory precondition not satisfied; refusing to "
              "run the pre-evolution snapshot")
        print("  {}".format(memory_recovery["note"]))
        raise SystemExit(3)
    if memory_recovery.get("gate") == "open":
        print("[snapshot] WARNING: home-only diagnostic mode "
              "(cannot authorize evolution)")

    live_roots = live_root_paths(list(args.live_root) + [str(root)])
    print(f"[snapshot] label={args.label} home={home}")
    native = resolve_native_runtime(repo, installs_root, args.native_python)
    print("[snapshot] native backend: {} (via {})".format(native["python"], native["resolved_via"]))

    if args.dry_run:
        print("[snapshot] dry-run: no backup executed")
        return 0

    print(f"[snapshot] native full backup -> {artifact}")
    t0 = time.monotonic()
    rc, out = _run_bridge(native, "backup", str(artifact), str(home),
                          args.backup_timeout)
    elapsed = round(time.monotonic() - t0, 1)
    if rc != 0 or not artifact.is_file():
        tail = "\n".join(out.strip().splitlines()[-15:])
        if tail:
            print(tail)
        _fail(f"native backup failed rc={rc} (artifact published: {artifact.is_file()})", code=1)
    if not zipfile.is_zipfile(artifact):
        _fail(f"native backup rc=0 but output is not a zip: {artifact}", code=1)

    print(f"[snapshot] native backup complete in {elapsed}s; hashing artifact")
    artifact_sha = _sha256_file(artifact)
    artifact_bytes = artifact.stat().st_size

    coverage: dict = {}
    rejected: list[str] = []
    prefix: Optional[str] = None
    with zipfile.ZipFile(artifact) as zf:
        members, prefix, rejected = _artifact_members(zf)
        coverage = _coverage_summary(members)
        if coverage["total_members"] == 0:
            _fail("artifact contains no restorable members", code=1)
        if not coverage["state_db_present"] and not args.allow_no_state_db:
            _fail("artifact lacks state.db -- full-home coverage is broken; "
                  "refusing to certify (use --allow-no-state-db only for "
                  "synthetic fixtures)", code=1)

    memory_recovery["gate"] = "verified" if args.hindsight_proof else "open"

    receipt = {
        "kind": "pre-evolution-snapshot",
        "label": args.label,
        "evolution_run": args.evolution_run,
        "created_at": _utc_now_iso(),
        "home": str(home),
        "is_live_home": _is_unsafe_destination(home, live_roots) is not None,
        "artifact": {
            "path": str(artifact),
            "sha256": artifact_sha,
            "bytes": artifact_bytes,
            "prefix": prefix,
        },
        "backend_identity": {
            **_git_identity(repo),
            "repo": str(repo),
            "python": native["python"],
            "resolved_via": native["resolved_via"],
            "code_repo_in_artifact": False,
        },
        "coverage": coverage,
        "rejected_member_names": rejected,
        "native_call": "hermes_cli.backup.run_backup(output=<artifact>, keep=0)",
        "native_elapsed_s": elapsed,
        "memory_recovery": memory_recovery,
        "notes": [
            "Native backup policy excludes: hermes-agent code repo, caches, "
            "venvs, models/runtimes/node roots, backups/, state-snapshots/, "
            "SQLite sidecars; skills/.archive IS included (root and "
            "profile-local trees alike).",
            "run_backup targets the ROOT home (a HERMES_HOME under the "
            "platform root maps back to the root); profile data is included "
            "via profiles/.",
            "Scoped rollback covers root skills/ AND profiles/*/skills/ plus "
            "config.yaml, SOUL.md, agent-hooks/ and profile config/SOUL.",
            "Quick snapshots (state-snapshots/) do NOT cover skills.",
        ],
    }
    receipt_path = backups_dir / (f"{ARTIFACT_PREFIX}{args.label}.receipt.json")
    _write_json(receipt_path, receipt)
    if args.receipt_out:
        _write_json(_resolve(args.receipt_out), receipt)

    print(f"[snapshot] artifact sha256={artifact_sha} bytes={artifact_bytes}"
          f" members={coverage['total_members']}")
    print(f"[snapshot] skills members: root={coverage['root_skills_files']}"
          f" profile={coverage['profile_skills_files']}"
          f" ({','.join(coverage['profiles_with_skills']) or 'none'})")
    print("[snapshot] memory-recovery gate: {}".format(memory_recovery["gate"]))
    print(f"[snapshot] receipt: {receipt_path}")
    print(f"[snapshot] scoped rollback: {sys.argv[0]} rollback --artifact {artifact} --home <DEST>")
    print(f"[snapshot] full restore (disposable dest only): {sys.argv[0]} restore-full "
          f"--artifact {artifact} --home <DEST>")
    return 0


# --------------------------------------------------------------------------
# rollback / verify / restore-full
# --------------------------------------------------------------------------

def _open_artifact(artifact: Path) -> zipfile.ZipFile:
    if not artifact.is_file():
        _fail(f"artifact not found: {artifact}", code=1)
    if not zipfile.is_zipfile(artifact):
        _fail(f"not a valid zip artifact: {artifact}", code=1)
    try:
        return zipfile.ZipFile(artifact)
    except (zipfile.BadZipFile, OSError) as exc:
        _fail(f"cannot open artifact: {exc}", code=1)


def _artifact_from_args(args: argparse.Namespace) -> Path:
    if args.label:
        return _resolve(args.backups_dir) / (f"{ARTIFACT_PREFIX}{args.label}.zip")
    if args.artifact:
        return _resolve(args.artifact)
    _fail("need --artifact or --label")


def _prescan(zf: zipfile.ZipFile, scoped: dict[str, zipfile.ZipInfo]) -> dict[str, str]:
    """Full CRC-forcing read of every scoped member BEFORE any write."""
    hashes: dict[str, str] = {}
    for rel, info in sorted(scoped.items()):
        try:
            h = hashlib.sha256()
            with zf.open(info) as src:
                while True:
                    block = src.read(1 << 20)
                    if not block:
                        break
                    h.update(block)
            hashes[rel] = h.hexdigest()
        except (OSError, zipfile.BadZipFile, EOFError, ValueError) as exc:
            _fail(f"artifact member damaged, aborting before any write: {rel} ({exc})", code=1)
    return hashes


def _safe_parent(home: Path, dest: Path) -> Optional[Path]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        resolved = dest.parent.resolve(strict=True)
        home_resolved = home.resolve(strict=False)
        resolved.relative_to(home_resolved)
    except (OSError, ValueError):
        return None
    return dest.parent


def _restore_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, rel: str,
                    home: Path) -> tuple[str, str, int]:
    dest = home / rel
    parent = _safe_parent(home, dest)
    if parent is None:
        return ("blocked-parent-escape", "", 0)
    if dest.is_symlink() or (dest.exists() and not stat.S_ISREG(dest.lstat().st_mode)):
        dest.unlink()  # never write through a link
    data = zf.read(info)
    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".w02-restore-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600 if dest.name in SECRET_NAMES else 0o644)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return ("restored", _sha256_bytes(data), len(data))


def cmd_rollback(args: argparse.Namespace) -> int:
    return _rollback_or_verify(args, mutate=True)


def cmd_verify(args: argparse.Namespace) -> int:
    return _rollback_or_verify(args, mutate=False)


def _classify_scope(hashes: dict, scoped: dict,
                    home: Path) -> tuple[dict, list[str]]:
    """Split scoped members into identical (hash-equal on disk) and
    to_restore (missing or differing)."""
    to_restore: dict[str, zipfile.ZipInfo] = {}
    identical: list[str] = []
    for rel in sorted(scoped):
        dest = home / rel
        if (dest.is_file() and not dest.is_symlink()
                and _sha256_file(dest) == hashes[rel]):
            identical.append(rel)
        else:
            to_restore[rel] = scoped[rel]
    return to_restore, identical


def _collect_added(home: Path, scoped: dict,
                   scopes: list[str]) -> list[str]:
    """Added files: dest files matching a scope predicate but absent from
    the artifact. Predicate-driven, so profile-local skill additions
    are detected without naming profiles, and unrelated profile data
    never floods the report."""
    member_set = set(scoped)
    added: list[str] = []
    for dirpath, dirnames, filenames in os.walk(home, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if not (Path(dirpath) / d).is_symlink()]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            rel = fpath.relative_to(home).as_posix()
            if rel in member_set or not _in_any_scope(rel, scopes):
                continue
            if fpath.is_file() and not fpath.is_symlink():
                added.append(rel)
    added.sort()
    return added


def _rollback_receipt(artifact: Path, home: Path, scopes: list[str],
                      mutate: bool, args: argparse.Namespace,
                      unsafe: object) -> dict:
    mode = "ROLLBACK" if mutate else "VERIFY"
    return {
        "kind": "pre-evolution-rollback" if mutate else "pre-evolution-verify",
        "mode": mode,
        "executed_at": _utc_now_iso(),
        "artifact": {
            "path": str(artifact),
            "sha256": _sha256_file(artifact),
            "bytes": artifact.stat().st_size,
        },
        "home": str(home),
        "is_live_home": unsafe is not None,
        "scopes": scopes,
        "dry_run": bool(mutate and getattr(args, "dry_run", False)),
        "prune_added": bool(mutate and getattr(args, "prune_added", False)),
        "summary": {},
        "files": [],
    }


def _verify_only(receipt: dict, mode: str, to_restore: dict, added: list,
                 identical: list[str], hashes: dict,
                 args: argparse.Namespace, mutate: bool) -> int:
    for rel in sorted(to_restore):
        receipt["files"].append({"path": rel, "status": "would-restore",
                                 "sha256_artifact": hashes[rel]})
    for rel in added:
        receipt["files"].append({"path": rel, "status": "added-in-dest"})
    receipt["summary"] = {
        "identical": len(identical),
        "to_restore": len(to_restore),
        "added_in_dest": len(added),
    }
    if args.receipt_out:
        _write_json(_resolve(args.receipt_out), receipt)
    drift = bool(to_restore or added)
    print("[{}] {}".format(mode, "DRIFT DETECTED" if drift else "IN PARITY"))
    for rel in sorted(to_restore)[:10]:
        print(f"[{mode}]   to_restore: {rel}")
    for rel in added[:10]:
        print(f"[{mode}]   added_in_dest: {rel}")
    if drift and (len(to_restore) + len(added)) > 20:
        print(f"[{mode}]   ... and {len(to_restore) + len(added) - 20} more"
              f" (see receipt)")
    return 1 if (drift and not mutate) else 0


def _apply_mutate(receipt: dict, zf: zipfile.ZipFile, to_restore: dict,
                  added: list, identical: list[str], hashes: dict,
                  args: argparse.Namespace) -> int:
    failures: list[str] = []
    for rel, info in sorted(to_restore.items()):
        status, digest, nbytes = _restore_member(zf, info, rel, home=Path(receipt["home"]))
        entry = {"path": rel, "status": status,
                 "sha256_artifact": hashes[rel],
                 "sha256_after": digest, "bytes": nbytes}
        if status == "restored":
            after = _sha256_file(Path(receipt["home"]) / rel)
            entry["sha256_after"] = after
            if after != hashes[rel]:
                entry["status"] = "VERIFY_FAILED"
                failures.append(rel)
        else:
            failures.append(rel)
        receipt["files"].append(entry)

    pruned: list[str] = []
    if getattr(args, "prune_added", False):
        for rel in added:
            target = Path(receipt["home"]) / rel
            try:
                if target.is_symlink() or target.is_file():
                    target.unlink()
                    pruned.append(rel)
            except OSError as exc:
                failures.append(rel)
                print(f"ERROR: prune failed {rel}: {exc}")

    # Principled parity: EVERY restored scope member re-hashes to its
    # artifact hash. No filename whitelist, no non-emptiness demand --
    # valid empty files (lock markers, package markers) hash-match too.
    all_match = all(f["status"] == "restored" and
                    f.get("sha256_after") == f.get("sha256_artifact")
                    for f in receipt["files"])
    receipt["summary"] = {
        "identical": len(identical),
        "restored": sum(1 for f in receipt["files"] if f["status"] == "restored"),
        "added_in_dest": len(added),
        "pruned_added": len(pruned),
        "failures": len(failures),
        "all_restored_match_artifact": all_match,
    }
    if args.receipt_out:
        _write_json(_resolve(args.receipt_out), receipt)

    if failures or not all_match:
        print(f"[rollback] FAILURES: {len(failures)} parity_ok={all_match}"
              f" ({','.join(failures[:5])})")
        return 1
    print(f"[rollback] complete: restored={receipt['summary']['restored']}"
          f" identical={len(identical)} pruned={len(pruned)}"
          f" added_left={len(added) - len(pruned)}")
    return 0


def _rollback_or_verify(args: argparse.Namespace, mutate: bool) -> int:
    home = _resolve(args.home)
    scopes = [s.strip() for s in args.scope.split(",") if s.strip()]
    for scope in scopes:
        if scope not in SCOPE_CHOICES:
            _fail("unknown scope {!r} (choices: {})".format(scope, ",".join(SCOPE_CHOICES)))

    # Protected roots for a restore destination are the platform default and
    # explicit --live-root values. The home being restored INTO is not itself
    # added: rolling a home back against its own snapshot is the normal use.
    live_roots = live_root_paths(list(args.live_root))
    unsafe = _is_unsafe_destination(home, live_roots)
    if mutate and unsafe and not args.allow_live:
        _fail(f"refusing rollback: {unsafe} (live-home protection; --allow-live is "
              "reserved for the operator after supervisor review)")

    artifact = _artifact_from_args(args)
    zf = _open_artifact(artifact)
    with zf:
        members, _prefix, rejected = _artifact_members(zf)
        if rejected:
            _fail("artifact contains unsafe member names; rejecting whole "
                  f"artifact: {rejected[:5]}", code=1)
        scoped = _scope_members(members, scopes)
        if not scoped:
            _fail(f"no members match scope {args.scope} in artifact", code=1)
        hashes = _prescan(zf, scoped)

        to_restore, identical = _classify_scope(hashes, scoped, home)
        added = _collect_added(home, scoped, scopes)

        mode = "ROLLBACK" if mutate else "VERIFY"
        print(f"[{mode}] artifact={artifact}")
        print("[{}] home={} scopes={}".format(mode, home, ",".join(scopes)))
        print(f"[{mode}] identical={len(identical)} to_restore={len(to_restore)}"
              f" added_in_dest={len(added)}")

        receipt = _rollback_receipt(artifact, home, scopes, mutate, args, unsafe)

        if not mutate or getattr(args, "dry_run", False):
            return _verify_only(receipt, mode, to_restore, added, identical,
                                hashes, args, mutate)
        return _apply_mutate(receipt, zf, to_restore, added, identical,
                             hashes, args)


def cmd_restore_full(args: argparse.Namespace) -> int:
    """Native full-home import (hermes import equivalent) into an explicit
    DISPOSABLE destination. Live homes are always rejected here."""
    home = _resolve(args.home)
    repo = _resolve(args.hermes_repo)
    installs_root = _resolve(args.installs_root)
    live_roots = live_root_paths(list(args.live_root))
    unsafe = _is_unsafe_destination(home, live_roots)
    if unsafe:
        _fail(f"refusing full restore: {unsafe}")
    artifact = _artifact_from_args(args)
    if not artifact.is_file():
        _fail(f"artifact not found: {artifact}", code=1)

    native = resolve_native_runtime(repo, installs_root, args.native_python)
    print(f"[restore-full] artifact={artifact}")
    print(f"[restore-full] destination={home} (disposable)")
    print("[restore-full] native backend: {} (via {})".format(native["python"], native["resolved_via"]))

    if args.dry_run:
        print("[restore-full] dry-run: no import executed")
        return 0

    home.mkdir(parents=True, exist_ok=True)
    rc, out = _run_bridge(native, "import", str(artifact), str(home),
                          args.import_timeout)
    tail = "\n".join(out.strip().splitlines()[-15:])
    if tail:
        print(tail)
    ok = rc == 0
    receipt = {
        "kind": "pre-evolution-restore-full",
        "executed_at": _utc_now_iso(),
        "artifact": {"path": str(artifact),
                     "sha256": _sha256_file(artifact),
                     "bytes": artifact.stat().st_size},
        "home": str(home),
        "native_call": "hermes_cli.backup.run_import(zipfile=<artifact>, force=True)",
        "rc": rc,
    }
    if args.receipt_out:
        _write_json(_resolve(args.receipt_out), receipt)
    if not ok:
        _fail(f"native import failed rc={rc}", code=1)
    print("[restore-full] complete")
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Phase 6.4 pre-evolution snapshot and safe restore wrapper")
    sub = parser.add_subparsers(dest="command", required=True)

    def path_flags(p, with_home_root_default: bool):
        root = platform_default_root()
        p.add_argument("--backups-dir", default=str(root / "backups"))
        p.add_argument("--hermes-repo", default=str(root / "hermes-agent"))
        p.add_argument("--installs-root", default=str(root / "installs"))
        p.add_argument("--live-root", action="append", default=[],
                       help="additional protected live root (repeatable); "
                            "the platform default root is always protected")
        p.add_argument("--native-python",
                       default=os.environ.get("W02_NATIVE_PYTHON"))

    snap = sub.add_parser("snapshot", help="full native backup + receipt")
    snap.add_argument("--label", required=True)
    snap.add_argument("--home", default=str(platform_default_root()))
    path_flags(snap, True)
    snap.add_argument("--backup-timeout", type=int, default=3600)
    snap.add_argument("--receipt-out", default=None)
    snap.add_argument("--hindsight-proof", default=None,
                      help="verified logical-dump proof receipt "
                           "(proof_schema 2; required unless "
                           "--allow-open-memory-gate)")
    snap.add_argument("--hindsight-container", default="",
                      help="intended live Hindsight container instance "
                           "(source binding; explicit CLI/config only -- no "
                           "environment-variable fallback; HS1/HS2 share "
                           "image and db name, so the instance must be "
                           "explicit)")
    snap.add_argument("--evolution-run", default=None,
                      help="optional explicit learning-run label bound into "
                           "the snapshot receipt and cross-checked against "
                           "the proof's run_label")
    snap.add_argument("--max-proof-age-hours", type=float, default=24.0,
                      help="reject proofs older than this (default 24; must "
                           "be finite and positive)")
    snap.add_argument("--allow-open-memory-gate", action="store_true",
                      help="EXPLICIT home-only diagnostic: record the "
                           "memory gate as open; the snapshot receipt is "
                           "marked cannot_authorize_evolution and must not "
                           "authorize any evolution run")
    snap.add_argument("--allow-no-state-db", action="store_true",
                      help="synthetic fixtures only")
    snap.add_argument("--dry-run", action="store_true")
    snap.set_defaults(func=cmd_snapshot)

    rb = sub.add_parser("rollback", help="scoped restore of skills/config")
    rb.add_argument("--artifact", default=None)
    rb.add_argument("--label", default=None)
    rb.add_argument("--home", required=True)
    rb.add_argument("--scope", default="skills,config")
    rb.add_argument("--dry-run", action="store_true")
    rb.add_argument("--prune-added", action="store_true")
    rb.add_argument("--allow-live", action="store_true")
    rb.add_argument("--receipt-out", default=None)
    path_flags(rb, False)
    rb.set_defaults(func=cmd_rollback)

    vf = sub.add_parser("verify", help="no-write parity report")
    vf.add_argument("--artifact", default=None)
    vf.add_argument("--label", default=None)
    vf.add_argument("--home", required=True)
    vf.add_argument("--scope", default="skills,config")
    vf.add_argument("--receipt-out", default=None)
    path_flags(vf, False)
    vf.set_defaults(func=cmd_verify)

    rf = sub.add_parser("restore-full",
                        help="native full-home import into a disposable destination")
    rf.add_argument("--artifact", default=None)
    rf.add_argument("--label", default=None)
    rf.add_argument("--home", required=True)
    rf.add_argument("--import-timeout", type=int, default=3600)
    rf.add_argument("--dry-run", action="store_true")
    rf.add_argument("--receipt-out", default=None)
    path_flags(rf, False)
    rf.set_defaults(func=cmd_restore_full)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
