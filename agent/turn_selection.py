"""Optional plugin selection at the current-turn recall/assembly boundary."""
import logging

logger = logging.getLogger(__name__)


def _plans(**kwargs):
    from hermes_cli.plugins import invoke_hook
    return invoke_hook('pre_memory_recall', **kwargs)


def select_turn_context(agent, messages, user_message, legacy_recall, notify_turn=None):
    """Selectors return a feed, a result callback and ephemeral context, never prompt edits."""
    if getattr(agent, '_persist_disabled', False):
        return legacy_recall(), ''
    try:
        plans = _plans(session_id=agent.session_id, conversation_history=list(messages),
                       user_message=user_message, turn_id=getattr(agent, '_current_turn_id', ''),
                       platform=getattr(agent, 'platform', ''),
                       parent_session_id=getattr(agent, '_parent_session_id', ''),
                       turn_number=getattr(agent, '_user_turn_count', 0))
        plan = next((p for p in plans if isinstance(p, dict) and 'feed' in p), None)
        if plan is None:
            return legacy_recall(), ''
        manager = getattr(agent, '_memory_manager', None)
        baseline = legacy_recall() if not plan.get('apply') else ''
        if plan.get('apply') and notify_turn:
            notify_turn()
        feed = plan.get('feed')
        batches = manager.prefetch_structured(feed, session_id=agent.session_id,
            deadline=plan.get('recall_deadline'), timeout=plan.get('recall_timeout_seconds')) if manager and feed else []
        legacy = '\n\n'.join(b.get('legacy_text', '') for b in batches if b.get('legacy_text'))
        try:
            selected = plan['select_results'](batches) or ''
            if not any(batch.get('candidates') for batch in batches):
                selected = ''
        except Exception:
            logger.warning('Turn recall selector failed; structured candidates withheld', exc_info=True)
            selected = ''
        if not plan.get('apply'):
            return baseline, ''
        return '\n\n'.join(p for p in (legacy, selected) if p), str(plan.get('context') or '')
    except Exception:
        logger.warning('Turn selector unavailable; retaining baseline recall', exc_info=True)
        return legacy_recall(), ''
