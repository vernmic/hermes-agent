"""Plugin policy decisions bound to the complete pre-mutation operation payload."""
from contextlib import contextmanager
from contextvars import ContextVar

_checked = ContextVar('skill_policy_checked', default=False)


def unattended():
    from tools.skill_provenance import is_unattended_review
    from tools.skill_ledger import derive_actor
    return is_unattended_review() or derive_actor() == 'curator'


def required():
    if not unattended():
        return False
    from hermes_cli.config import load_config
    return bool((load_config().get('skills') or {}).get('unattended_write_policy_required', False))


def evaluate(payload, *, session_id=None):
    if _checked.get():
        return {'allowed': True, 'gate': None}
    from hermes_cli.plugins import has_hook, invoke_hook
    try:
        if not has_hook('pre_skill_write'):
            if not required():
                return {'allowed': True, 'gate': None}
            return {'allowed': False, 'reason': 'Required unattended skill policy is unavailable',
                    'gate': {'disposition': 'gate_error', 'actor_unattended': unattended()}, 'capture_required': True}
        results = invoke_hook('pre_skill_write', proposal=payload, session_id=session_id)
    except Exception:
        return {'allowed': False, 'reason': 'Skill policy could not be evaluated',
                'gate': {'disposition': 'gate_error', 'actor_unattended': unattended()}, 'capture_required': True}
    if not results:
        return {'allowed': False, 'reason': 'Skill policy returned no decision',
                'gate': {'disposition': 'gate_error', 'actor_unattended': unattended()}, 'capture_required': True}
    gate = None
    for decision in results:
        if not isinstance(decision, dict) or decision.get('action') != 'allow':
            return {'allowed': False, 'reason': decision.get('message', 'Skill policy refused')
                    if isinstance(decision, dict) else 'Invalid skill policy decision',
                    'gate': decision.get('gate', {'disposition': 'gate_error', 'actor_unattended': unattended()})
                    if isinstance(decision, dict) else {'disposition': 'gate_error', 'actor_unattended': unattended()},
                    'capture_required': not isinstance(decision, dict) or not decision.get('gate')}
        gate = decision.get('gate') or gate
    return {'allowed': True, 'gate': gate}


@contextmanager
def write_scope(payload, *, session_id=None):
    from tools.skill_review_trigger import reset_gate_context, set_gate_context
    decision = evaluate(payload, session_id=session_id)
    if decision.get('capture_required'):
        try:
            from tools.skill_review_events import preserve_proposal
            preserve_proposal(payload, session_id=session_id, gate={**decision['gate'], 'reason': decision['reason']})
        except Exception:
            # Review capture cannot authorize a blocked mutation.
            pass
    token = set_gate_context(decision['gate']) if decision.get('gate') else None
    checked = _checked.set(True) if decision['allowed'] else None
    try:
        yield decision
    finally:
        if checked is not None:
            _checked.reset(checked)
        if token is not None:
            reset_gate_context(token)
