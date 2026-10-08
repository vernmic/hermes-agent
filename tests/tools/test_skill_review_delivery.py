"""Grouped intake preserves distinct evidence and concurrent capture survives draining."""
import threading


def test_grouped_cards_keep_events_and_deduplicate_across_sessions(tmp_path, monkeypatch):
    from hermes_cli import kanban_db, kanban_db_connect
    from tools import skill_review_trigger as trigger
    kanban_db.init_db(tmp_path / 'kanban.db')
    conn = kanban_db_connect.connect(tmp_path / 'kanban.db')
    monkeypatch.setattr(trigger, '_kanban_conn', lambda *args: kanban_db_connect.connect(tmp_path / 'kanban.db'))
    cfg = {'enabled': True, 'dry_run': False, 'grouping': 'skill_session',
           'outbox_path': str(tmp_path / 'outbox.jsonl'), 'automatic_delivery': False}
    def entry(identity, after, session='s'):
        return {'id': identity, 'ts': '2026-10-07T00:00:00+00:00', 'actor': 'curator',
                'skill': 'skill', 'action': 'patch', 'before': [{'path': 'skill/SKILL.md', 'sha256': 'a'}],
                'after': [{'path': 'skill/SKILL.md', 'sha256': after}], 'evidence': {'session_id': session}}
    for row in (entry('one', 'b'), entry('two', 'c'), entry('retry', 'b', 'worker-session')):
        assert trigger.on_landed_mutation(row, cfg) == 'enqueued'
    assert trigger.drain_outbox(cfg)['created'] == 1
    # create_grouped uses a connection context owned by its caller; test's shared connection stays open.
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM task_comments').fetchone()[0] == 2


def test_capture_during_drain_survives(tmp_path, monkeypatch):
    from tools import skill_review_trigger as trigger
    cfg = {'enabled': True, 'dry_run': False, 'outbox_path': str(tmp_path / 'outbox.jsonl')}
    def record(key):
        return {'dedup_key': key, 'ts': '2026-10-07T00:00:00+00:00', 'status': 'pending', 'payload': {}}
    assert trigger._outbox_upsert(record('first'), cfg)
    entered, release = threading.Event(), threading.Event()
    def create(row, cfg):
        entered.set()
        assert release.wait(10)
        return 'task', True
    monkeypatch.setattr(trigger, 'create_card', create)
    drain = threading.Thread(target=trigger.drain_outbox, args=(cfg,))
    drain.start()
    assert entered.wait(10)
    capture = threading.Thread(target=trigger._outbox_upsert, args=(record('second'), cfg))
    capture.start()
    release.set()
    drain.join(10)
    capture.join(10)
    assert not drain.is_alive() and not capture.is_alive()
    rows = trigger._read_outbox(cfg)
    assert {row['dedup_key'] for row in rows} == {'first', 'second'}
    assert next(row for row in rows if row['dedup_key'] == 'second')['status'] == 'pending'


def test_approved_landing_annotates_refused_proposal_without_new_card(tmp_path, monkeypatch):
    from hermes_cli import kanban_db, kanban_db_connect
    from tools import skill_review_trigger as trigger
    kanban_db.init_db(tmp_path / 'kanban.db')
    monkeypatch.setattr(trigger, '_kanban_conn', lambda *args: kanban_db_connect.connect(tmp_path / 'kanban.db'))
    cfg = {'enabled': True, 'dry_run': False, 'grouping': 'skill_session', 'automatic_delivery': False,
           'outbox_path': str(tmp_path / 'outbox.jsonl')}
    entry = {'id': 'landed', 'actor': 'curator', 'action': 'patch', 'skill': 'skill',
             'before': [{'path': 'skill/SKILL.md', 'sha256': 'a'}],
             'after': [{'path': 'skill/SKILL.md', 'sha256': 'b'}],
             'evidence': {'session_id': 'origin', 'gate': {'disposition': 'gate_refuse_evidence'}}}
    proposal = trigger.landed_record_from_entry(entry, cfg)
    proposal['source'] = 'proposal'
    trigger._outbox_upsert(proposal, cfg)
    assert trigger.drain_outbox(cfg)['created'] == 1
    entry['evidence'] = {'session_id': 'worker', 'gate': {'disposition': 'gate_pass'}}
    assert trigger.on_landed_mutation(entry, cfg) == 'correlation-enqueued'
    assert trigger.drain_outbox(cfg)['deduped'] == 1
    conn = kanban_db_connect.connect(tmp_path / 'kanban.db')
    try:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
        note = conn.execute('SELECT body FROM task_comments').fetchone()[0]
        assert 'gate_pass' in note and 'landed' in note
    finally:
        conn.close()
