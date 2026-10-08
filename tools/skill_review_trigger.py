"""Automatic kanban review trigger for fallback-path skill writes.

Spec: ``knowledge/design/fallback-skill-review-trigger-spec.md`` (v2026-10-05,
including the operator scope amendment and October 6 decisions).
Grouping, safe delivery and historical cutoff policy follow those decisions.

INERT BY DEFAULT. The trigger is armed only when
``fallback_review.enabled`` is true AND ``fallback_review.dry_run`` is false
(spec 3.1). Activation is a separate, operator-gated deployment step: merely
having this module on disk enqueues and creates nothing.

Design rules enforced here (spec section 6):
  * Downstream consumer of the write ledger -- never runs inside
    ``skill_ledger.append_entry``, and never blocks or rolls back a mutation.
  * The proposal-outcome adapter captures refused/unavailable updates BEFORE any
    mutation and fabricates no ledger entry.
  * Queuing a card writes nothing under the skills tree, flips no disposition,
    and cannot unlock a refusal. Every failure is swallowed and logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# --- frozen taxonomy (spec 3.4) ----------------------------------------------

GATE_PASS = "gate_pass"
FALLBACK_DISPOSITIONS = frozenset({
    "gate_refuse_evidence",
    "gate_refuse_recurrence",
    "judge_unavailable_timeout",
    "judge_unavailable_transport",
    "judge_unavailable_auth",
    "judge_unavailable_parse",
    "gate_bypassed",
    "gate_error",
    "ledger_unavailable",
})

# Content deletion is included. Administrative lifecycle operations are excluded.
CONTENT_ACTIONS = frozenset({"create", "edit", "patch", "write_file", "remove_file", "delete"})

# Proposal-outcome tokens -> gate dispositions (spec 3.4). The proposal adapter
# rows carry no write-ledger anchor, so their disposition lives on the outbox
# record instead.
OUTCOME_TO_DISPOSITION = {
    "unavailable": "judge_unavailable_transport",
    "unavailable_timeout": "judge_unavailable_timeout",
    "unavailable_transport": "judge_unavailable_transport",
    "unavailable_auth": "judge_unavailable_auth",
    "unavailable_parse": "judge_unavailable_parse",
    "blocked": "gate_refuse_evidence",
    "refused": "gate_refuse_evidence",
    "gate_error": "gate_error",
    "bypassed": "gate_bypassed",
    "ledger_unavailable": "ledger_unavailable",
}

_CONFIG_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "dry_run": False,
    "board": "default",
    "assignee": "default",
    "created_by": "fallback-review-trigger",
    "priority": 0,
    "completion_contract": "local-only",
    "see_also": "t_c5276d65",
    "outbox_path": None,
    "watermark_path": None,
    "grouping": "skill_session",
    "automatic_delivery": True,
    "start_after": None,
    "include_historical": False,
}

_outbox_lock = threading.Lock()

# In-flight gate/turn context: the active write policy sets this around a mutation
# so the ledger row carries the disposition. Absent gate evidence + an
# unattended actor is recorded as ``gate_bypassed`` (spec 3.4).
_gate_ctx: ContextVar[Optional[Dict[str, Any]]] = ContextVar("skill_review_gate_ctx", default=None)


def set_gate_context(payload: Dict[str, Any]) -> Token:
    return _gate_ctx.set(dict(payload or {}))


def reset_gate_context(token: Token) -> None:
    _gate_ctx.reset(token)


def current_gate_context() -> Optional[Dict[str, Any]]:
    return _gate_ctx.get()


# --- config ------------------------------------------------------------------


def load_trigger_config() -> Dict[str, Any]:
    """``fallback_review`` config with defaults; never raises."""
    cfg = dict(_CONFIG_DEFAULTS)
    try:
        from hermes_cli.config import cfg_get, load_config

        raw = cfg_get(load_config(), "fallback_review", default=None)
        if isinstance(raw, dict):
            cfg.update({k: v for k, v in raw.items() if v is not None})
    except Exception as exc:  # pragma: no cover - config layer is best-effort
        logger.debug("skill_review_trigger: config unavailable (%s)", exc)
    return cfg


def trigger_enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """The queue capability is on (spec 3.1). ``dry_run`` still counts as on."""
    c = cfg or load_trigger_config()
    return bool(c.get("enabled"))


def trigger_armed(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """spec 3.1: enabled AND NOT dry_run. Nothing else arms the trigger; in
    particular judge availability / enforced mode is NOT an arming prerequisite."""
    c = cfg or load_trigger_config()
    return bool(c.get("enabled")) and not bool(c.get("dry_run"))


def _record_status(cfg: Dict[str, Any]) -> str:
    """Outbox status for a new record: ``pending`` when armed, else ``dry-run``
    (a recorded would-be task that is never inserted, spec 3.1)."""
    return "pending" if trigger_armed(cfg) else "dry-run"


# --- paths -------------------------------------------------------------------


def _skills_root() -> Path:
    try:
        from tools import skill_ledger as _ledger

        return _ledger._skills_dir()
    except Exception:
        home = get_hermes_home()
        # Mirror skill_ledger's resolution: <home>/skills.
        return Path(home) / "skills"


def outbox_path(cfg: Optional[Dict[str, Any]] = None) -> Path:
    c = cfg or load_trigger_config()
    override = c.get("outbox_path")
    if override:
        return Path(str(override)).expanduser()
    return _skills_root() / ".review_outbox.jsonl"


def watermark_path(cfg: Optional[Dict[str, Any]] = None) -> Path:
    c = cfg or load_trigger_config()
    override = c.get("watermark_path")
    if override:
        return Path(str(override)).expanduser()
    return _skills_root() / ".review_outbox.watermark"


# --- digests / dedup key (spec 5.1, 5.2) -------------------------------------


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canon_manifest(manifest: Optional[List[Dict[str, str]]]) -> str:
    rows = []
    for item in manifest or []:
        path = str(item.get("path", ""))
        sha = str(item.get("sha256", ""))
        rows.append(f"{path}\t{sha}")
    return "\n".join(sorted(rows))


def manifest_digest(manifest: Optional[List[Dict[str, str]]]) -> str:
    return _sha256_hex(canon_manifest(manifest))


def before_after_digests(entry: Dict[str, Any]) -> Tuple[str, str]:
    return (manifest_digest(entry.get("before") or []),
            manifest_digest(entry.get("after") or []))


def base_dedup_key(skill_id: str, action: str, before_digest: str, after_digest: str) -> str:
    tail = _sha256_hex("\n".join([skill_id, action, before_digest, after_digest]))[:32]
    return "fallback-skill-review:v1:" + tail


def dedup_key(skill_id: str, action: str, before_digest: str, after_digest: str,
              epoch_salt: Optional[str] = None) -> str:
    key = base_dedup_key(skill_id, action, before_digest, after_digest)
    return f"{key}:{epoch_salt}" if epoch_salt else key


# --- ledger-entry classification --------------------------------------------


def entry_disposition(entry: Dict[str, Any]) -> Optional[str]:
    gate = (entry.get("evidence") or {}).get("gate") or {}
    disp = gate.get("disposition")
    return str(disp) if disp else None


def entry_is_unattended(entry: Dict[str, Any]) -> bool:
    gate = (entry.get("evidence") or {}).get("gate") or {}
    if isinstance(gate.get("actor_unattended"), bool):
        return bool(gate["actor_unattended"])
    # Historical fallback entries predate the gate fields; the curator actor is
    # the only durable unattended marker on them.
    return entry.get("actor") == "curator"


def entry_should_card(entry: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """``(should_card, disposition)`` per spec 3.2/3.4.

    A landed entry is carded when its content-changing action carries a
    fallback disposition, or -- with no gate evidence at all -- when it is an
    unattended write (the gate was never consulted). ``gate_pass`` never cards.
    """
    if not entry_is_unattended(entry) or entry.get("action") not in CONTENT_ACTIONS:
        return False, entry_disposition(entry)
    disp = entry_disposition(entry)
    if disp == GATE_PASS:
        return False, disp
    if disp in FALLBACK_DISPOSITIONS:
        return True, disp
    if disp is None:
        return entry_is_unattended(entry), None
    return False, disp


# --- gate evidence for the record_mutation call site (spec 3.5) --------------


def build_gate_evidence(*, disposition: str, reason: str = "", gate_version: Optional[str] = None,
                        judge_model: Optional[str] = None, gate_call_ref: Optional[str] = None,
                        actor_unattended: bool = False, turn_ref: Optional[str] = None,
                        turn_msg_id: Optional[int] = None) -> Dict[str, Any]:
    return {
        "disposition": disposition,
        "reason": reason,
        "gate_version": gate_version,
        "judge_model": judge_model,
        "gate_call_ref": gate_call_ref,
        "actor_unattended": bool(actor_unattended),
        "turn_ref": turn_ref,
        "turn_msg_id": turn_msg_id,
    }


def gate_evidence_for_write(*, unattended: bool, session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The additive ``evidence['gate']`` sub-object for a landed mutation.

    Returns the in-flight gate context when a gate was consulted, a
    ``gate_bypassed`` record when the write is unattended, and ``None`` for an
    ordinary attended foreground write (spec Q3 default: not carded, unchanged
    ledger shape).
    """
    ctx = _gate_ctx.get()
    if ctx:
        merged = dict(ctx)
        if session_id and not merged.get("session_id"):
            merged["session_id"] = session_id
        return build_gate_evidence(
            disposition=str(merged.get("disposition") or "gate_bypassed"),
            reason=str(merged.get("reason") or ""),
            gate_version=merged.get("gate_version"),
            judge_model=merged.get("judge_model"),
            gate_call_ref=merged.get("gate_call_ref"),
            actor_unattended=bool(merged.get("actor_unattended", unattended)),
            turn_ref=merged.get("turn_ref"),
            turn_msg_id=merged.get("turn_msg_id"),
        )
    if unattended:
        return build_gate_evidence(
            disposition="gate_bypassed",
            reason="write gate not consulted (unattended fallback path)",
            actor_unattended=True,
        )
    return None


# --- durable outbox (spec 7.1) -----------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_outbox(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    path = outbox_path(cfg)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("skill_review_trigger: outbox unreadable (%s)", exc)
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("skill_review_trigger: skipping malformed outbox line")
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _write_outbox(records: List[Dict[str, Any]], cfg: Optional[Dict[str, Any]] = None) -> bool:
    path = outbox_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
        with tmp.open("w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("skill_review_trigger: outbox write failed (%s)", exc)
        return False


def _outbox_upsert(record: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Replace the row for ``record['dedup_key']`` or append. Durable-first."""
    from tools.skill_review_delivery import upsert
    return upsert(record, cfg or load_trigger_config())


def outbox_status(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    rows = _read_outbox(cfg)
    counts = {"pending": 0, "done": 0, "failed": 0}
    for row in rows:
        counts[row.get("status", "pending")] = counts.get(row.get("status", "pending"), 0) + 1
    return counts


# --- card payload / body (spec 4) --------------------------------------------


_PAYLOAD_FENCE = "```json"


def build_card_body(payload: Dict[str, Any]) -> str:
    summary = (
        f"Fallback skill write: {payload.get('skill_id')} ({payload.get('action')})\n\n"
        "The proper skill-change mechanism was unavailable or blocked this update, or the\n"
        "change landed through a fallback path. Review the evidence and the current skill,\n"
        "then implement through the proper mechanism if justified, or record why not.\n"
        "This card authorizes nothing: it does not bypass the write refusal.\n\n"
    )
    return summary + _PAYLOAD_FENCE + "\n" + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n```"


def _extract_payload(body: str) -> Optional[Dict[str, Any]]:
    start = body.find(_PAYLOAD_FENCE)
    if start < 0:
        return None
    start = body.find("\n", start)
    end = body.find("```", start + 1)
    if start < 0 or end < 0:
        return None
    try:
        parsed = json.loads(body[start:end].strip())
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def build_payload(*, skill_id: str, action: str, before_digest: str, after_digest: str,
                  files: Optional[List[Dict[str, Any]]] = None,
                  actor: Optional[str] = None, unattended: bool = False,
                  session_id: Optional[str] = None, turn_ref: Optional[str] = None,
                  turn_msg_id: Optional[int] = None, failure: Optional[Dict[str, Any]] = None,
                  ledger_entry_id: Optional[str] = None, cfg: Optional[Dict[str, Any]] = None,
                  ledger_path: Optional[str] = None, blobs: Optional[str] = None,
                  dedup_degraded: bool = False) -> Dict[str, Any]:
    c = cfg or load_trigger_config()
    ts = _now_iso()
    try:
        from tools import skill_ledger as _ledger

        ledger_file = ledger_path or str(_ledger.ledger_path())
        blobs_dir = blobs or str(_ledger.blobs_dir())
    except Exception:
        ledger_file = ledger_path
        blobs_dir = blobs
    return {
        "board": c.get("board", "default"),
        "skill_id": skill_id,
        "action": action,
        "skill_files": files or [],
        "actor": actor,
        "unattended": bool(unattended),
        "session_id": session_id,
        "turn_ref": turn_ref,
        "turn_msg_id": turn_msg_id,
        "failure": failure or {},
        "before_digest": before_digest,
        "after_digest": after_digest,
        "diff_ref": {"ledger": ledger_file, "entry_id": ledger_entry_id, "blobs": blobs_dir},
        "ledger_entry_id": ledger_entry_id,
        "first_seen_ts": ts,
        "last_seen_ts": ts,
        "see_also": c.get("see_also"),
        "created_by": c.get("created_by", "fallback-review-trigger"),
        "dedup_degraded": bool(dedup_degraded),
    }


def build_record(*, skill_id: str, action: str, before_digest: str, after_digest: str,
                 dedup_key_value: str, payload: Dict[str, Any], source: str,
                 ledger_entry_id: Optional[str] = None) -> Dict[str, Any]:
    return {
        "dedup_key": dedup_key_value,
        "ledger_entry_id": ledger_entry_id,
        "skill_id": skill_id,
        "action": action,
        "source": source,  # "ledger" | "proposal"
        "payload": payload,
        "status": "pending",
        "ts": _now_iso(),
    }


# --- kanban board access -----------------------------------------------------


def _kanban_conn(board: Optional[str] = None):
    from hermes_cli import kanban_db_connect

    return kanban_db_connect.connect(board=board or load_trigger_config().get("board", "default"))


def _find_task(conn, key: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT id, status, completed_at FROM tasks WHERE idempotency_key = ? "
        "AND status != 'archived' ORDER BY created_at DESC LIMIT 1",
        (key,),
    ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "status": row["status"], "completed_at": row["completed_at"]}


def _tasks_for_base(conn, base: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, status, completed_at FROM tasks WHERE idempotency_key = ? "
        "OR idempotency_key LIKE ?",
        (base, base + ":%"),
    ).fetchall()
    return [{"id": r["id"], "status": r["status"], "completed_at": r["completed_at"]} for r in rows]


def resolve_epoch_salt(conn, base: str, entry_ts: Optional[str]) -> Optional[str]:
    """spec 5.4: a new card only once every prior card is terminal and the write
    postdates it. While any card is open the change stays deduped onto it."""
    tasks = _tasks_for_base(conn, base)
    if not tasks:
        return None
    open_tasks = [t for t in tasks if t["status"] not in ("done", "archived")]
    if open_tasks:
        open_tasks.sort(key=lambda t: t.get("completed_at") or 0)
        return None  # no epoch: dedupe onto the open card
    terminal = [t for t in tasks if t["status"] in ("done", "archived")]
    if not terminal:
        return None
    terminal.sort(key=lambda t: t.get("completed_at") or 0)
    newest = terminal[-1]
    if entry_ts and newest.get("completed_at") is not None:
        try:
            if _iso_to_epoch(entry_ts) <= int(newest["completed_at"]):
                return None  # re-occurrence predates the close: still a retry
        except Exception:
            return None
    return str(newest["id"])


def _iso_to_epoch(ts: str) -> float:
    text = str(ts).strip().replace("Z", "+00:00")
    return datetime.fromisoformat(text).timestamp()


def _update_body_last_seen(conn, task_id: str, last_seen_ts: str) -> None:
    row = conn.execute("SELECT body FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or not row["body"]:
        return
    payload = _extract_payload(row["body"])
    if payload is None:
        return
    payload["last_seen_ts"] = last_seen_ts
    conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (build_card_body(payload), task_id))


def create_card(record, cfg=None):
    c = cfg or load_trigger_config()
    if c.get("grouping") == "skill_session":
        from tools.skill_review_delivery import create_grouped
        try:
            return create_grouped(record, c)
        except Exception as exc:
            logger.warning("Grouped review card failed (%s); retained in outbox", exc)
            return None, False
    return _create_ungrouped_card(record, c)


def _create_ungrouped_card(record: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], bool]:
    """Insert (or dedupe onto) the review card. Returns ``(task_id, created)``.

    Never raises. In dry-run, records the would-be card and creates nothing.
    """
    c = cfg or load_trigger_config()
    payload = record.get("payload") or {}
    base = record.get("card_key") or base_dedup_key(payload.get("skill_id", ""), payload.get("action", ""),
                          payload.get("before_digest", ""), payload.get("after_digest", ""))
    try:
        from hermes_cli import kanban_db

        conn = _kanban_conn(c.get("board"))
        epoch = resolve_epoch_salt(conn, base, record.get("ts") or payload.get("first_seen_ts"))
        key = (base + ":" + epoch) if epoch else base

        existing = _find_task(conn, key)
        if existing is not None:
            _append_redundant_comment(conn, existing["id"], record)
            _update_body_last_seen(conn, existing["id"], _now_iso())
            conn.commit()
            return existing["id"], False

        if c.get("dry_run"):
            return None, True  # would-be card; nothing written

        title = f"Fallback skill write: {payload.get('skill_id')} ({payload.get('action')})"
        body = build_card_body(payload)
        try:
            from hermes_cli.kanban_db import add_comment  # noqa: F401  (import check)

            task_id = kanban_db.create_task(
                conn, title=title, body=body, assignee=c.get("assignee"),
                created_by=c.get("created_by"), priority=int(c.get("priority") or 0),
                triage=True, idempotency_key=key, completion_contract=c.get("completion_contract"),
            )
        except Exception as exc:
            # Concurrent enqueue race or board write failure: re-read, else give up.
            logger.warning("skill_review_trigger: card create failed (%s)", exc)
            again = _find_task(conn, key)
            if again is not None:
                _append_redundant_comment(conn, again["id"], record)
                conn.commit()
                return again["id"], False
            raise
        conn.commit()
        return task_id, True
    except Exception as exc:
        logger.warning("skill_review_trigger: card enqueue failed (%s) -- proposal retained in outbox", exc)
        return None, False
    finally:
        if "conn" in locals():
            conn.close()


def _append_redundant_comment(conn, task_id: str, record: Dict[str, Any]) -> None:
    payload = record.get("payload") or {}
    gate = payload.get("failure") or {}
    note = {
        "ledger_entry_id": record.get("ledger_entry_id") or payload.get("ledger_entry_id"),
        "session_id": payload.get("session_id"),
        "turn_ref": payload.get("turn_ref"),
        "ts": record.get("ts"),
        "disposition": gate.get("disposition") if isinstance(gate, dict) else None,
        "event": payload,
        "outbox_key": record.get("dedup_key"),
    }
    try:
        from hermes_cli.kanban_db import add_comment

        add_comment(conn, task_id, "fallback-review-trigger",
                    "Additional provenance for the same logical change: "
                    + json.dumps(note, ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        logger.warning("skill_review_trigger: dedup comment failed (%s)", exc)


# --- the two adapters --------------------------------------------------------


def landed_record_from_entry(entry: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Build the durable record for one landed ledger entry, or None if out of scope."""
    should, _disp = entry_should_card(entry)
    if not should:
        return None
    skill_id = str(entry.get("skill") or "")
    for item in (entry.get("before") or []) + (entry.get("after") or []):
        try:
            parts = Path(item["path"]).relative_to(_skills_root()).parts
            if "SKILL.md" in parts:
                skill_id = "/".join(parts[:parts.index("SKILL.md")])
                break
        except (ValueError, KeyError):
            pass
    action = str(entry.get("action") or "")
    before_digest, after_digest = before_after_digests(entry)
    gate = (entry.get("evidence") or {}).get("gate") or {}
    files = []
    b_sha = {str(i.get("path")): i.get("sha256") for i in (entry.get("before") or [])}
    a_sha = {str(i.get("path")): i.get("sha256") for i in (entry.get("after") or [])}
    for path in sorted(set(b_sha) | set(a_sha)):
        files.append({"path": path, "before_sha256": b_sha.get(path), "after_sha256": a_sha.get(path)})
    payload = build_payload(
        skill_id=skill_id, action=action, before_digest=before_digest, after_digest=after_digest,
        files=files, actor=entry.get("actor"), unattended=entry_is_unattended(entry),
        session_id=(entry.get("evidence") or {}).get("session_id"),
        turn_ref=gate.get("turn_ref"), turn_msg_id=gate.get("turn_msg_id"),
        failure={"gate": "write-gate", "disposition": entry_disposition(entry),
                 "reason": gate.get("reason"), "gate_version": gate.get("gate_version"),
                 "judge_model": gate.get("judge_model"), "gate_call_ref": gate.get("gate_call_ref")},
        ledger_entry_id=entry.get("id"), cfg=cfg,
        dedup_degraded=entry_disposition(entry) == "ledger_unavailable",
    )
    return build_record(
        skill_id=skill_id, action=action, before_digest=before_digest, after_digest=after_digest,
        dedup_key_value=dedup_key(skill_id, action, before_digest, after_digest),
        payload=payload, source="ledger", ledger_entry_id=entry.get("id"),
    )


def on_landed_mutation(entry: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> str:
    """Post-write hook (spec 3.3 step 3). Call AFTER ``record_mutation`` returned.

    Reads the ledger entry (or the in-memory dict) by id, builds the durable
    outbox record, and returns a short status. Inert when disarmed. Never raises.
    """
    c = cfg or load_trigger_config()
    if not trigger_enabled(c):
        return "disarmed"
    try:
        coerced = _coerce_entry(entry)
        if coerced is None:
            return "missing-entry"
        entry = coerced
        if entry_disposition(entry) == GATE_PASS and entry_is_unattended(entry) and c.get("grouping") == "skill_session":
            copied = {**entry, "evidence": {**(entry.get("evidence") or {}), "gate": {
                **((entry.get("evidence") or {}).get("gate") or {}), "disposition": "gate_bypassed"}}}
            record = landed_record_from_entry(copied, c)
            if record is None:
                return "out-of-scope"
            record["source"] = "landed_correlation"
            record["payload"]["failure"]["disposition"] = GATE_PASS
            record["dedup_key"] += ":landed:" + str(entry.get("id"))
            record["status"] = _record_status(c)
            return "correlation-enqueued" if _outbox_upsert(record, c) else "outbox-write-failed"
        record = landed_record_from_entry(entry, c)
        if record is None:
            return "out-of-scope"
        record["status"] = _record_status(c)
        if not _outbox_upsert(record, c):
            return "outbox-write-failed"
        return "enqueued" if record["status"] == "pending" else "dry-run"
    except Exception as exc:
        logger.warning("skill_review_trigger: landed hook failed (%s) -- write unaffected", exc)
        return "error"


def _coerce_entry(entry: Any) -> Optional[Dict[str, Any]]:
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, str):
        try:
            from tools import skill_ledger as _ledger

            return _ledger.get_entry(entry)
        except Exception:
            return None
    return None


def on_landed_mutation_by_id(entry_id: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    """Read the ledger entry by its returned id, then enqueue. Never raises."""
    c = cfg or load_trigger_config()
    if not trigger_enabled(c):
        return "disarmed"
    try:
        from tools import skill_ledger as _ledger

        entry = _ledger.get_entry(entry_id)
        if entry is None:
            logger.warning("skill_review_trigger: no ledger entry %s; nothing enqueued", entry_id)
            return "missing-entry"
        return on_landed_mutation(entry, c)
    except Exception as exc:
        logger.warning("skill_review_trigger: landed hook failed (%s) -- write unaffected", exc)
        return "error"


def enqueue_proposal_outcome(
    *, skill_id: str, action: str, outcome: str, reason: str = "",
    proposed: Optional[str] = None, base_sha256: Optional[str] = None,
    proposed_sha256: Optional[str] = None, session_id: Optional[str] = None,
    turn_ref: Optional[str] = None, turn_msg_id: Optional[int] = None,
    unattended: bool = True, cfg: Optional[Dict[str, Any]] = None,
) -> str:
    """Proposal-outcome adapter (operator scope amendment): a refused or
    unavailable update with NO mutation and NO write-ledger row. Durable-first.

    Returns a short status string; never raises.
    """
    c = cfg or load_trigger_config()
    if not trigger_enabled(c):
        return "disarmed"
    if not unattended:
        return "out-of-scope"
    try:
        disposition = OUTCOME_TO_DISPOSITION.get(outcome, "gate_error")
        if proposed is not None and proposed_sha256 is None:
            proposed_sha256 = _sha256_hex(proposed)
        if proposed_sha256 is None and base_sha256 is None:
            # No digests at all: degrade honestly rather than fabricate an anchor.
            dedup_degraded = True
            b_digest = _sha256_hex(skill_id + "\n" + action + "\nproposal")
            a_digest = b_digest
        else:
            dedup_degraded = False
            b_digest = base_sha256 or _sha256_hex("")
            a_digest = proposed_sha256 or _sha256_hex("")
        key = dedup_key(skill_id, action, b_digest, a_digest)
        payload = build_payload(
            skill_id=skill_id, action=action, before_digest=b_digest, after_digest=a_digest,
            files=[], actor="curator" if unattended else None, unattended=unattended,
            session_id=session_id, turn_ref=turn_ref, turn_msg_id=turn_msg_id,
            failure={"gate": "write-gate", "disposition": disposition, "reason": reason},
            ledger_entry_id=None, cfg=c, dedup_degraded=dedup_degraded,
        )
        # Preserve the original proposal text/diff: the card references it, the
        # durable outbox holds the bytes.
        payload["proposal"] = {
            "sha256": proposed_sha256 or (base_sha256 or None),
            "len": len(proposed) if proposed is not None else None,
        }
        record = build_record(
            skill_id=skill_id, action=action, before_digest=b_digest, after_digest=a_digest,
            dedup_key_value=key, payload=payload, source="proposal", ledger_entry_id=None,
        )
        if proposed is not None:
            record["proposed_content"] = proposed
        record["status"] = _record_status(c)
        if not _outbox_upsert(record, c):
            return "outbox-write-failed"
        return "enqueued" if record["status"] == "pending" else "dry-run"
    except Exception as exc:
        logger.warning("skill_review_trigger: proposal enqueue failed (%s) -- refusal unaffected", exc)
        return "error"


# --- consumer + reconciler (spec 7.1, 7.3) -----------------------------------


def drain_outbox(cfg: Optional[Dict[str, Any]] = None, *, limit: Optional[int] = None) -> Dict[str, int]:
    """Create cards for pending outbox records, flip them to done with the id.

    Bounded backoff is the caller's concern; this is one pass and never raises.
    """
    from tools.skill_review_delivery import drain
    return drain(cfg or load_trigger_config(), limit)


def _read_watermark(cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
    try:
        data = json.loads(watermark_path(cfg).read_text(encoding="utf-8"))
        return data.get("last_entry_id")
    except Exception:
        return None


def _write_watermark(entry_id: Optional[str], cfg: Optional[Dict[str, Any]] = None) -> bool:
    try:
        path = watermark_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"last_entry_id": entry_id}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("skill_review_trigger: watermark write failed (%s)", exc)
        return False


def reconcile(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    from tools.skill_review_delivery import transaction
    c = cfg or load_trigger_config()
    if not trigger_enabled(c):
        return {"scanned": 0, "enqueued": 0, "outbox_write_failed": 0, "unresolved": 0}
    with transaction(c):
        return _reconcile_locked(c)


def _reconcile_locked(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """Scan the write ledger for un-enqueued fallback entries (spec 7.1).

    The watermark advances ONLY after the corresponding outbox write commits.
    If the ledger was trimmed so the watermark id is gone, the pass replays from
    the start; dedup (spec 5) makes that idempotent.
    """
    c = cfg or load_trigger_config()
    stats = {"scanned": 0, "enqueued": 0, "outbox_write_failed": 0, "unresolved": 0}
    if not trigger_enabled(c):
        return stats
    try:
        from tools import skill_ledger as _ledger

        entries = list(_ledger.list_entries())  # newest first
        if not entries:
            return stats
        ordered = list(reversed(entries))  # oldest first
        watermark = _read_watermark(c)
        start = 0
        if watermark:
            for i, entry in enumerate(ordered):
                if entry.get("id") == watermark:
                    start = i + 1
                    break
        if not watermark and not c.get("include_historical") and not c.get("start_after"):
            stats["unresolved"] = 1
            logger.warning("Review reconciliation needs an explicit start_after cutoff")
            return stats
        for entry in ordered[start:]:
            if c.get("start_after") and _iso_to_epoch(entry["ts"]) < _iso_to_epoch(c["start_after"]):
                continue
            stats["scanned"] += 1
            record = landed_record_from_entry(entry, c)
            if record is None:
                if not _write_watermark(entry.get("id"), c):
                    stats["outbox_write_failed"] += 1
                    break
                continue
            record["status"] = _record_status(c)
            # Exactly one durable record per logical change: present in the
            # outbox OR already a kanban card counts as enqueued.
            if _record_already_enqueued(record, c):
                if _write_watermark(entry.get("id"), c):
                    continue
                stats["outbox_write_failed"] += 1
                break
            if not _outbox_upsert(record, c):
                stats["outbox_write_failed"] += 1
                stats["unresolved"] += 1
                break  # never advance the watermark past an uncommitted record
            stats["enqueued"] += 1
            if not _write_watermark(entry.get("id"), c):
                stats["outbox_write_failed"] += 1
                break
    except Exception as exc:
        logger.warning("skill_review_trigger: reconcile failed (%s)", exc)
    return stats


def _record_already_enqueued(record: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    # Logical equality cannot prove a later ledger occurrence was captured. In
    # particular a recurrence after card completion must reach epoch resolution.
    identity = record.get("ledger_entry_id")
    for row in _read_outbox(cfg):
        events = [row] + row.get("attempts", [])
        if identity and any(event.get("ledger_entry_id") == identity for event in events):
            return True
    return False


# --- Approved policy (operator decision 2026-10-06) ---------------------------
# Q2: one card per skill/originating session; preserve events and retry links.
# Q3: attended changes excluded.
# Q4: cross-process locked JSONL; consumer and reconciler share the capture lock.
# Q5: no pre-write mutation journal. Mutation-to-ledger crash gap remains.
# Q6: disposition strings unchanged.
# Q7: explicit cutoff, dry-run, then pilot; no blanket historical activation.
# Q8: content delete included; administrative archive/restore/purge/rollback excluded.
# Q9: default board/default assignee/triage.
