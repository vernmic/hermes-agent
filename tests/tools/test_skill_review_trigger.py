"""Tests for tools/skill_review_trigger.py -- the automatic kanban review trigger
for fallback-path skill writes (spec: knowledge/design/fallback-skill-review-trigger-spec.md).

Covered: disarmed inertness, dry-run, landed fallback -> card, gate_pass -> no card,
attended/no-evidence -> no card, blocked proposal with NO write-ledger row, unavailable
proposal, retry dedup (one card + comment), distinct change -> new card, epoch recurrence,
board-offline retry/recovery, reconciler backfill + idempotency, and the no-authority
invariant (card creation writes nothing under the skills tree).
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _cfg(tmp_path, **over):
    cfg = {
        "enabled": True,
        "dry_run": False,
        "board": "default",
        "assignee": "default",
        "created_by": "fallback-review-trigger",
        "priority": 0,
        "completion_contract": "local-only",
        "see_also": "t_c5276d65",
        "outbox_path": str(tmp_path / "outbox.jsonl"),
        "watermark_path": str(tmp_path / "watermark.json"),
    }
    cfg.update(over)
    return cfg


def _entry(**over):
    entry = {
        "id": "abc123",
        "ts": "2026-10-05T12:00:00+00:00",
        "actor": "curator",
        "action": "patch",
        "skill": "data-science/foo",
        "evidence": {
            "session_id": "s1",
            "gate": {
                "disposition": "gate_bypassed",
                "reason": "no gate",
                "actor_unattended": True,
                "turn_ref": "turn-1",
                "turn_msg_id": None,
            },
        },
        "before": [{"path": "SKILL.md", "sha256": "a" * 64}],
        "after": [{"path": "SKILL.md", "sha256": "b" * 64}],
    }
    entry.update(over)
    return entry


def _count(conn, table="tasks"):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


@pytest.fixture
def board(tmp_path, monkeypatch):
    """Isolated kanban DB; the trigger resolves connections through it."""
    from hermes_cli import kanban_db, kanban_db_connect
    from tools import skill_review_trigger as trig

    db = tmp_path / "kanban.db"
    kanban_db.init_db(db)
    conn = kanban_db_connect.connect(db)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kbhome"))
    monkeypatch.setattr(trig, "_kanban_conn", lambda board=None: kanban_db_connect.connect(db))
    yield conn
    conn.close()


# --- arming / inertness ------------------------------------------------------


def test_disabled_is_inert(tmp_path, board):
    from tools import skill_review_trigger as trig

    c = _cfg(tmp_path, enabled=False)
    assert trig.trigger_armed(c) is False
    assert trig.on_landed_mutation(_entry(), c) == "disarmed"
    assert trig.enqueue_proposal_outcome(skill_id="x", action="patch", outcome="unavailable", cfg=c) == "disarmed"
    assert not Path(c["outbox_path"]).exists()
    assert _count(board) == 0


def test_dry_run_records_without_card(tmp_path, board):
    from tools import skill_review_trigger as trig

    c = _cfg(tmp_path, dry_run=True)
    assert trig.trigger_armed(c) is False and trig.trigger_enabled(c) is True
    assert trig.on_landed_mutation(_entry(), c) == "dry-run"
    rows = trig._read_outbox(c)
    assert len(rows) == 1 and rows[0]["status"] == "dry-run"
    assert trig.drain_outbox(c)["created"] == 0
    assert _count(board) == 0


# --- classification ----------------------------------------------------------


def test_gate_pass_creates_no_card(tmp_path, board):
    from tools import skill_review_trigger as trig

    entry = _entry(evidence={"session_id": "s1", "gate": {"disposition": "gate_pass"}})
    assert trig.on_landed_mutation(entry, _cfg(tmp_path)) == "out-of-scope"
    assert _count(board) == 0


def test_attended_entry_without_gate_is_not_carded(tmp_path, board):
    from tools import skill_review_trigger as trig

    entry = _entry(actor="agent", evidence={"session_id": "s1"})
    assert trig.on_landed_mutation(entry, _cfg(tmp_path)) == "out-of-scope"
    assert _count(board) == 0


def test_landed_fallback_creates_card(tmp_path, board):
    from tools import skill_review_trigger as trig

    c = _cfg(tmp_path)
    assert trig.on_landed_mutation(_entry(), c) == "enqueued"
    stats = trig.drain_outbox(c)
    assert stats["created"] == 1

    row = board.execute("SELECT id, title, status, created_by, idempotency_key FROM tasks").fetchone()
    assert row["status"] == "triage"
    assert row["created_by"] == "fallback-review-trigger"
    assert "data-science/foo" in row["title"]
    assert row["idempotency_key"].startswith("fallback-skill-review:v1:")

    body = board.execute("SELECT body FROM tasks").fetchone()["body"]
    payload = trig._extract_payload(body)
    assert payload["skill_id"] == "data-science/foo"
    assert payload["action"] == "patch"
    assert payload["failure"]["disposition"] == "gate_bypassed"
    assert payload["skill_files"][0]["after_sha256"] == "b" * 64
    assert payload["ledger_entry_id"] == "abc123"


# --- dedup (spec 5) ----------------------------------------------------------


def test_retry_dedup_single_card_with_comment(tmp_path, board):
    from tools import skill_review_trigger as trig

    c = _cfg(tmp_path)
    trig.on_landed_mutation(_entry(id="e1"), c)
    trig.drain_outbox(c)

    # Same logical change, fresh ledger id/ts: collapses onto the open card.
    assert trig.on_landed_mutation(_entry(id="e2", ts="2026-10-05T13:00:00+00:00"), c) == "enqueued"
    stats = trig.drain_outbox(c)
    assert stats["created"] == 0 and stats["deduped"] == 1
    assert _count(board) == 1
    assert _count(board, "task_comments") == 1


def test_distinct_change_creates_new_card(tmp_path, board):
    from tools import skill_review_trigger as trig

    c = _cfg(tmp_path)
    trig.on_landed_mutation(_entry(id="e1"), c)
    trig.drain_outbox(c)
    changed = _entry(id="e2")
    changed["after"] = [{"path": "SKILL.md", "sha256": "c" * 64}]
    trig.on_landed_mutation(changed, c)
    trig.drain_outbox(c)
    assert _count(board) == 2


def test_epoch_recurrence_after_terminal_card(tmp_path, board):
    from tools import skill_review_trigger as trig

    c = _cfg(tmp_path)
    trig.on_landed_mutation(_entry(id="e1"), c)
    trig.drain_outbox(c)
    tid = board.execute("SELECT id FROM tasks").fetchone()["id"]
    board.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (int(time.time()) - 100, tid))
    board.commit()

    # A genuinely new occurrence after the card closed gets its own card.
    ts = datetime.now(timezone.utc).isoformat()
    assert trig.on_landed_mutation(_entry(id="e2", ts=ts), c) == "enqueued"
    trig.drain_outbox(c)
    assert _count(board) == 2
    keys = [r[0] for r in board.execute("SELECT idempotency_key FROM tasks")]
    assert any(k.count(":") >= 3 for k in keys)  # base key + epoch salt


# --- proposal-outcome adapter (operator scope amendment) ---------------------


def test_blocked_proposal_preserves_content_without_ledger_row(tmp_path, board):
    from tools import skill_review_trigger as trig

    c = _cfg(tmp_path)
    status = trig.enqueue_proposal_outcome(
        skill_id="code/foo", action="patch", outcome="blocked", reason="evidence refused",
        proposed="NEW SKILL BODY", base_sha256="a" * 64, session_id="s9", turn_ref="t9", cfg=c)
    assert status == "enqueued"

    rows = trig._read_outbox(c)
    assert rows[0]["proposed_content"] == "NEW SKILL BODY"
    assert rows[0]["ledger_entry_id"] is None

    trig.drain_outbox(c)
    body = board.execute("SELECT body FROM tasks").fetchone()["body"]
    payload = trig._extract_payload(body)
    assert payload["failure"]["disposition"] == "gate_refuse_evidence"
    assert payload["proposal"]["sha256"] is not None
    assert payload["ledger_entry_id"] is None


def test_unavailable_proposal_maps_to_disposition(tmp_path, board):
    from tools import skill_review_trigger as trig

    c = _cfg(tmp_path)
    assert trig.enqueue_proposal_outcome(
        skill_id="code/foo", action="create", outcome="unavailable_timeout",
        proposed="BODY", cfg=c) == "enqueued"
    rows = trig._read_outbox(c)
    assert rows[0]["payload"]["failure"]["disposition"] == "judge_unavailable_timeout"


def test_proposal_dedup_by_proposal_identity(tmp_path, board):
    from tools import skill_review_trigger as trig

    c = _cfg(tmp_path)
    for _ in range(3):
        trig.enqueue_proposal_outcome(skill_id="code/foo", action="patch", outcome="blocked",
                                      proposed="SAME BODY", base_sha256="a" * 64, cfg=c)
    assert len(trig._read_outbox(c)) == 1


# --- recovery (spec 7.1, 7.3) ------------------------------------------------


def test_board_offline_retains_outbox_and_recovers(tmp_path, monkeypatch):
    from hermes_cli import kanban_db, kanban_db_connect
    from tools import skill_review_trigger as trig

    c = _cfg(tmp_path)

    def _boom(board=None):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(trig, "_kanban_conn", _boom)
    assert trig.on_landed_mutation(_entry(), c) == "enqueued"
    stats = trig.drain_outbox(c)
    assert stats["failed"] == 1
    assert trig._read_outbox(c)[0]["status"] == "pending"

    # Board back: the durable record still creates the card.
    db = tmp_path / "kb.db"
    kanban_db.init_db(db)
    conn = kanban_db_connect.connect(db)
    monkeypatch.setattr(trig, "_kanban_conn", lambda board=None: kanban_db_connect.connect(db))
    stats = trig.drain_outbox(c)
    assert stats["created"] == 1
    done = [r for r in trig._read_outbox(c) if r["status"] == "done"]
    assert len(done) == 1 and done[0]["task_id"]
    conn.close()


def test_reconcile_backfills_missing_and_is_idempotent(tmp_path, board, monkeypatch):
    from tools import skill_ledger
    from tools import skill_review_trigger as trig

    c = _cfg(tmp_path, include_historical=True)
    entries = [
        _entry(id="e1"),
        _entry(id="e2", actor="agent", evidence={"session_id": "s"}),  # not carded
    ]
    monkeypatch.setattr(skill_ledger, "list_entries", lambda skill=None, limit=None: list(reversed(entries)))

    stats = trig.reconcile(c)
    assert stats["enqueued"] == 1
    trig.drain_outbox(c)
    assert _count(board) == 1

    # Second pass: watermark is honoured, nothing re-enqueued.
    assert trig.reconcile(c)["enqueued"] == 0
    assert _count(board) == 1


# --- no-authority invariant (spec 6) -----------------------------------------


def test_card_creation_writes_nothing_under_skills(tmp_path, board, monkeypatch):
    from tools import skill_review_trigger as trig

    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setattr(trig, "_skills_root", lambda: skills)
    before = sorted(str(p) for p in skills.rglob("*"))

    c = _cfg(tmp_path)
    trig.on_landed_mutation(_entry(), c)
    trig.drain_outbox(c)
    trig.enqueue_proposal_outcome(skill_id="code/foo", action="patch", outcome="blocked",
                                  proposed="BODY", cfg=c)

    assert sorted(str(p) for p in skills.rglob("*")) == before
    assert _count(board) >= 1


def test_card_failure_never_raises(tmp_path, monkeypatch):
    from tools import skill_review_trigger as trig

    c = _cfg(tmp_path)

    def _boom(board=None):
        raise RuntimeError("nope")

    monkeypatch.setattr(trig, "_kanban_conn", _boom)
    # enqueue is durable-first and must not raise even when the board is gone.
    assert trig.on_landed_mutation(_entry(), c) == "enqueued"
    assert trig.drain_outbox(c) == {"created": 0, "deduped": 0, "failed": 1}


# --- gate evidence for the record_mutation call site (spec 3.5) --------------


def test_gate_evidence_unattended_is_bypassed(tmp_path):
    from tools import skill_review_trigger as trig

    ev = trig.gate_evidence_for_write(unattended=True, session_id="s1")
    assert ev["disposition"] == "gate_bypassed"
    assert ev["actor_unattended"] is True


def test_gate_evidence_attended_is_none(tmp_path):
    from tools import skill_review_trigger as trig

    assert trig.gate_evidence_for_write(unattended=False) is None


def test_gate_evidence_uses_inflight_context():
    from tools import skill_review_trigger as trig

    token = trig.set_gate_context({"disposition": "judge_unavailable_timeout",
                                   "reason": "3.0s timeout", "turn_ref": "turn-7"})
    try:
        ev = trig.gate_evidence_for_write(unattended=True)
        assert ev["disposition"] == "judge_unavailable_timeout"
        assert ev["reason"] == "3.0s timeout"
        assert ev["turn_ref"] == "turn-7"
    finally:
        trig.reset_gate_context(token)


def test_dedup_key_excludes_ledger_id_and_ts():
    from tools import skill_review_trigger as trig

    k1 = trig.dedup_key("x", "patch", "b1", "b2")
    k2 = trig.dedup_key("x", "patch", "b1", "b2")
    assert k1 == k2
    assert trig.dedup_key("x", "patch", "b1", "b2", "epoch") == k1 + ":epoch"
