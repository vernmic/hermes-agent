"""Portable proof-validator tests for scripts/pre_evolution_snapshot.py.

Runs WITHOUT any installed Hermes tree: the wrapper is stdlib-only, so this
file imports it directly from the scripts/ directory next to the tests root
and exercises the memory-proof validator (in-process and through the public
CLI entry with a tiny synthetic home). All fixtures are synthetic; nothing
here touches a live home, the Hindsight server, Docker, or any database.

The native-backend integration lives in
tests/scripts/test_pre_evolution_snapshot_native.py and needs an explicit
compatible interpreter against a real checkout.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "scripts" / "pre_evolution_snapshot.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("pre_evolution_snapshot", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def w():
    return _load_module()


def make_dump(tmp: Path) -> tuple[Path, str]:
    import hashlib
    tmp.mkdir(parents=True, exist_ok=True)
    dump = tmp / "proof-dump.dump"
    dump.write_bytes(b"PGDMP synthetic proof payload" * 50)
    return dump, hashlib.sha256(dump.read_bytes()).hexdigest()


def make_proof(tmp: Path, **overrides) -> Path:
    import hashlib
    from datetime import UTC, datetime, timedelta
    dump, sha = make_dump(tmp)
    # fresh timestamp: hardcoded dates go stale against the 24h default limit
    created = (datetime.now(UTC) - timedelta(minutes=10)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    tables = {t: {"rows": 1, "sum_hash": "123", "xor_hash": "456"}
              for t in ("banks", "documents", "memory_units", "memory_links",
                        "unit_entities", "entities", "alembic_version")}
    schema = {"rows": 40, "sum_hash": "789", "xor_hash": "012"}
    doc = {
        "kind": "hindsight-logical-backup-proof",
        "proof_schema": 2,
        "verdict": "verified",
        "created_at": created,
        "run_label": None,
        "source": {"container": "hindsight-hindsight-1",
                   "container_image": "hindsight-fork:v1.1.0-7827db7fc",
                   "image_id": "sha256:" + "a" * 64, "db": "hindsight",
                   "postgres_version": "PostgreSQL 18.1"},
        "dump": {"path": str(dump), "bytes": dump.stat().st_size,
                 "sha256": sha, "format": "postgres custom (-Fc)"},
        "export": {"snapshot_id": "00000003-0000001B-1",
                   "method": "repeatable-read exported snapshot shared by "
                             "source digests and pg_dump"},
        "digests": {"tables": dict(tables), "schema": dict(schema),
                    "restored_tables": {k: dict(v) for k, v in tables.items()},
                    "restored_schema": dict(schema),
                    "missing": [], "mismatch": []},
        "restore": {"container": "scratch",
                    "image": "hindsight-fork:v1.1.0-7827db7fc",
                    "image_id": "sha256:" + "a" * 64,
                    "outcome": "success", "pg_restore_rc": 0,
                    "digests_match": True},
    }
    doc.update(overrides)
    path = tmp / "proof.json"
    path.write_text(json.dumps(doc), encoding="ascii")
    return path


def _rejects(w, proof: Path, *, container="hindsight-hindsight-1",
             run=None, max_age=24.0) -> str:
    with pytest.raises(SystemExit) as exc:
        w._load_memory_proof(proof, max_age, container, run)
    assert exc.value.code == 3
    return "rejected"


def test_accepts_fully_bound_proof(w, tmp_path):
    proof = make_proof(tmp_path)
    field = w._load_memory_proof(proof, 24.0, "hindsight-hindsight-1", None)
    assert field["export"]["snapshot_id"] == "00000003-0000001B-1"
    assert field["source"]["container"] == "hindsight-hindsight-1"


def test_rejects_inconsistent_digests(w, tmp_path):
    # restored documents rows differ while digests_match stays true
    proof = make_proof(tmp_path)
    doc = json.loads(proof.read_text())
    doc["digests"]["restored_tables"]["documents"]["rows"] = 999
    proof.write_text(json.dumps(doc))
    _rejects(w, proof)
    # nonempty mismatch list
    proof = make_proof(tmp_path)
    doc = json.loads(proof.read_text())
    doc["digests"]["mismatch"] = ["memory_links"]
    proof.write_text(json.dumps(doc))
    _rejects(w, proof)
    # differing restored schema
    proof = make_proof(tmp_path)
    doc = json.loads(proof.read_text())
    doc["digests"]["restored_schema"]["sum_hash"] = "x"
    proof.write_text(json.dumps(doc))
    _rejects(w, proof)


def test_rejects_missing_or_malformed_binding(w, tmp_path):
    proof = make_proof(tmp_path)
    doc = json.loads(proof.read_text())
    doc.pop("export")
    proof.write_text(json.dumps(doc))
    _rejects(w, proof)
    proof = make_proof(tmp_path)
    doc = json.loads(proof.read_text())
    doc["export"]["snapshot_id"] = "bogus"
    proof.write_text(json.dumps(doc))
    _rejects(w, proof)


def test_rejects_wrong_instance_and_bad_freshness(w, tmp_path):
    proof = make_proof(tmp_path)
    _rejects(w, proof, container="hindsight2-hindsight-1")  # HS2, same image/db
    _rejects(w, proof, container="")                        # unconfigured target
    with pytest.raises(SystemExit):
        w._load_memory_proof(proof, 0, "hindsight-hindsight-1", None)
    with pytest.raises(SystemExit):
        w._load_memory_proof(proof, -1, "hindsight-hindsight-1", None)


def test_requested_run_requires_matching_bound_label(w, tmp_path):
    """A requested run label must be present, nonempty and exactly matched
    on the proof (unbound proofs are mechanism-only)."""
    # null label (make_proof default) -> rejected for a named run
    proof = make_proof(tmp_path)
    _rejects(w, proof, run="required-learning-run")
    # empty label -> rejected
    proof = make_proof(tmp_path, run_label="")
    _rejects(w, proof, run="required-learning-run")
    # wrong label -> rejected
    proof = make_proof(tmp_path, run_label="other-run")
    _rejects(w, proof, run="required-learning-run")
    # exactly matching label -> accepted
    proof = make_proof(tmp_path, run_label="required-learning-run")
    field = w._load_memory_proof(proof, 24.0, "hindsight-hindsight-1",
                                 "required-learning-run")
    assert field["run_label"] == "required-learning-run"
    # unbound proof with NO run requested -> accepted (mechanism use)
    proof = make_proof(tmp_path)
    w._load_memory_proof(proof, 24.0, "hindsight-hindsight-1", None)


def test_public_entry_requested_run_rejects_unbound_proof(tmp_path):
    """Public-entry control: the CLI rejects an unbound proof when
    --evolution-run is supplied, before any capture."""
    home = tmp_path / "home"
    (home / "skills" / "demo").mkdir(parents=True)
    home.joinpath("config.yaml").write_text("model:\n  provider: x\n")
    home.joinpath(".env").write_text("DUMMY=1\n")
    home.joinpath("skills/demo/SKILL.md").write_text("# demo\n")
    import sqlite3
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    proof = make_proof(tmp_path / "p")
    backups = tmp_path / "backups"
    backups.mkdir()
    # explicit stand-in repo/installs dirs keep this test portable (no
    # installed tree needed): the gate rejection happens before any
    # interpreter resolution, and the canonical runner's conftest points
    # HERMES_HOME at a temp dir whose default repo path does not exist.
    proc = subprocess.run(
        [sys.executable, str(TOOL), "snapshot", "--label", "runbound",
         "--home", str(home), "--backups-dir", str(backups),
         "--hermes-repo", str(tmp_path), "--installs-root", str(tmp_path),
         "--hindsight-proof", str(proof),
         "--hindsight-container", "hindsight-hindsight-1",
         "--evolution-run", "required-learning-run"],
        capture_output=True, text=True, timeout=300, check=False)
    assert proc.returncode == 3
    assert "run_label" in proc.stdout
    assert not (backups / "pre-evolution-runbound.zip").exists()
