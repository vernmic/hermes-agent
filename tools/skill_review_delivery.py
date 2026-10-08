"""Cross-process outbox transactions, grouped cards and bounded background delivery."""
import hashlib
import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)
_workers = {}
_workers_lock = threading.Lock()


def transaction(cfg):
    from tools.skill_usage import skill_file_lock
    from tools.skill_review_trigger import outbox_path
    return skill_file_lock(Path(str(outbox_path(cfg)) + '.lock'))


def read_checked(cfg):
    from tools.skill_review_trigger import outbox_path
    path = outbox_path(cfg)
    return [json.loads(line) for line in path.read_text(encoding='utf-8').split('\n') if line.strip()] if path.exists() else []


def upsert(record, cfg):
    from tools import skill_review_trigger as trigger
    with transaction(cfg):
        rows = read_checked(cfg)
        for index, prior in enumerate(rows):
            if prior.get('dedup_key') != record['dedup_key'] or prior.get('status') not in ('pending', 'dry-run'):
                continue
            # Keep the originating session and full first event stable. Retries
            # are additional evidence, including retries from worker sessions.
            prior.setdefault('attempts', []).append({k: v for k, v in record.items() if k != 'attempts'})
            if prior['status'] == 'dry-run' and record['status'] == 'pending':
                # A fresh capture after arming promotes this logical change.
                # Existing shadow records are never bulk-promoted on activation.
                prior['status'] = 'pending'
            rows[index] = prior
            break
        else:
            rows.append(record)
        okay = trigger._write_outbox(rows, cfg)
    if okay and cfg.get('automatic_delivery') and trigger.trigger_armed(cfg):
        schedule(cfg)
    return okay


def change_key(payload):
    return hashlib.sha256('\n'.join(str(payload.get(key, '')) for key in ('skill_id', 'before_digest', 'after_digest')).encode()).hexdigest()


def create_grouped(record, cfg):
    from tools import skill_review_trigger as trigger
    payload = record['payload']
    if cfg.get('dry_run'):
        return None, True
    conn = trigger._kanban_conn(cfg.get('board'))
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS fallback_review_links (change_key TEXT PRIMARY KEY, task_id TEXT NOT NULL)')
        conn.commit()
        logical = change_key(payload)
        previous = conn.execute('SELECT t.id,t.status,t.completed_at FROM fallback_review_links l JOIN tasks t ON t.id=l.task_id WHERE l.change_key=?', (logical,)).fetchone()
        if previous and (previous['status'] not in ('done', 'archived') or
            (previous['completed_at'] is not None and trigger._iso_to_epoch(record['ts']) <= previous['completed_at'])):
            trigger._append_redundant_comment(conn, previous['id'], record)
            conn.commit()
            return previous['id'], False
        session_id = payload.get('session_id')
        grouped = dict(record)
        if session_id:
            grouped['card_key'] = 'fallback-skill-review:session:v1:' + hashlib.sha256(
                (payload['skill_id'] + '\n' + session_id).encode()).hexdigest()[:32]
        task_id, created = trigger._create_ungrouped_card(grouped, cfg)
        if task_id:
            conn.execute('INSERT INTO fallback_review_links VALUES(?,?) ON CONFLICT(change_key) DO UPDATE SET task_id=excluded.task_id', (logical, task_id))
            conn.commit()
        return task_id, created
    finally:
        conn.close()


def correlate_landed(record, cfg):
    """An approved landing annotates an existing review; it never creates one."""
    from tools import skill_review_trigger as trigger
    conn = trigger._kanban_conn(cfg.get('board'))
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fallback_review_links'").fetchone():
            return None
        row = conn.execute('SELECT task_id FROM fallback_review_links WHERE change_key=?',
                           (change_key(record['payload']),)).fetchone()
        if row:
            trigger._append_redundant_comment(conn, row['task_id'], record)
            conn.commit()
            return row['task_id']
        return None
    finally:
        conn.close()


def drain(cfg, limit=None):
    from tools import skill_review_trigger as trigger
    stats = {'created': 0, 'deduped': 0, 'failed': 0}
    if not trigger.trigger_armed(cfg):
        return stats
    # Hold the same cross-process lock as capture across read/update/replace. A new
    # proposal cannot be overwritten by the consumer's older snapshot.
    with transaction(cfg):
        rows = read_checked(cfg)
        processed = 0
        for row in rows:
            if row['status'] != 'pending' or (limit is not None and processed >= limit):
                continue
            processed += 1
            if row.get('source') == 'landed_correlation':
                try:
                    row.update(status='done', task_id=correlate_landed(row, cfg))
                    stats['deduped'] += bool(row['task_id'])
                except Exception:
                    stats['failed'] += 1
                continue
            task_id, created = trigger.create_card(row, cfg)
            if task_id:
                for attempt in row.get('attempts', []):
                    conn = trigger._kanban_conn(cfg.get('board'))
                    try:
                        trigger._append_redundant_comment(conn, task_id, attempt)
                        conn.commit()
                    finally:
                        conn.close()
                row.update(status='done', task_id=task_id)
                stats['created' if created else 'deduped'] += 1
            else:
                stats['failed'] += 1
        if not trigger._write_outbox(rows, cfg):
            stats['failed'] += 1
    return stats


def service(cfg=None):
    from tools import skill_review_trigger as trigger
    cfg = cfg or trigger.load_trigger_config()
    if not trigger.trigger_enabled(cfg):
        return {'disarmed': True}
    replay = trigger.reconcile(cfg)
    delivery = drain(cfg, limit=10)
    return {'reconcile': replay, 'delivery': delivery, 'outbox': trigger.outbox_status(cfg)}


def schedule(cfg):
    from agent.memory_provider import spawn_context_thread
    from hermes_constants import hermes_home_key
    home = hermes_home_key()
    with _workers_lock:
        if home in _workers and _workers[home].is_alive():
            return
        def run():
            try:
                for delay in (0, 1, 2, 4, 8, 16):
                    if delay:
                        time.sleep(delay)
                    status = service(cfg)
                    if not status.get('outbox', {}).get('pending', 0):
                        return
            except Exception:
                logger.warning('Skill review delivery failed; durable events await next service pass', exc_info=True)
        worker = spawn_context_thread(run, name='skill-review-delivery')
        _workers[home] = worker
        worker.start()
