"""Full proposal evidence and ledger-unavailable recovery for the review outbox."""
import hashlib
import json


def preserve_proposal(proposal, *, session_id, gate, previews=None):
    from tools import skill_review_trigger as trigger
    cfg = trigger.load_trigger_config()
    if not trigger.trigger_enabled(cfg):
        return 'disarmed'
    if not gate.get('actor_unattended'):
        return 'out-of-scope'
    operations = proposal.get('operations') or [proposal]
    previews = previews or {}
    statuses = []
    names = dict.fromkeys(op.get('name') or proposal.get('name') or 'unknown' for op in operations)
    for name in names:
        preview = previews.get(name) or {}
        skill_id = preview.get('skill_id', name)
        local_ops = [op for op in operations if (op.get('name') or proposal.get('name') or 'unknown') == name]
        before = preview.get('before') or []
        after = preview.get('after') or []
        text = json.dumps({'operations': local_ops, 'evidence': proposal.get('evidence'),
                           'base_files': preview.get('existing_files'), 'skill_path': preview.get('path')},
                          ensure_ascii=True, sort_keys=True)
        before_digest = trigger.manifest_digest(before)
        after_digest = trigger.manifest_digest(after) if preview else hashlib.sha256(text.encode()).hexdigest()
        action = local_ops[0]['action'] if len(local_ops) == 1 else 'batch'
        payload = trigger.build_payload(skill_id=skill_id, action=action, before_digest=before_digest,
            after_digest=after_digest, files=[], actor='curator', unattended=True,
            session_id=session_id, turn_ref=gate.get('turn_ref'), turn_msg_id=None,
            failure={'gate': 'write-gate', **gate}, ledger_entry_id=None, cfg=cfg)
        payload['proposal'] = {'operations': local_ops, 'evidence': proposal.get('evidence'),
                               'skill_path': preview.get('path'), 'base_manifest': before, 'proposed_manifest': after}
        key = trigger.base_dedup_key(skill_id, action, before_digest, after_digest)
        payload['diff_ref']['proposal_outbox'] = {'path': str(trigger.outbox_path(cfg)), 'key': key}
        record = trigger.build_record(skill_id=skill_id, action=action, before_digest=before_digest,
            after_digest=after_digest, dedup_key_value=key, payload=payload, source='proposal')
        record['proposed_content'] = text
        record['status'] = trigger._record_status(cfg)
        statuses.append('enqueued' if trigger._outbox_upsert(record, cfg) else 'outbox-write-failed')
    return 'outbox-write-failed' if 'outbox-write-failed' in statuses else 'enqueued'


def on_missing_ledger(action, name, *, before, after_root, session_id):
    from tools import skill_review_trigger as trigger, skill_ledger
    from tools.skill_provenance import is_unattended_review
    cfg = trigger.load_trigger_config()
    if not trigger.trigger_enabled(cfg) or not (is_unattended_review() or skill_ledger.derive_actor() == 'curator'):
        return
    after = skill_ledger.capture_before(after_root) if after_root else []
    entry = {'id': None, 'skill': name, 'action': action, 'actor': 'curator',
             'before': before or [], 'after': after,
             'evidence': {'session_id': session_id, 'gate': {'disposition': 'ledger_unavailable', 'actor_unattended': True}}}
    trigger.on_landed_mutation(entry, cfg)
