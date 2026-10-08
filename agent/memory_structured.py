"""Bounded structured recall with an explicit legacy-provider fallback."""
import time
from agent.memory_provider import spawn_context_thread


def prefetch_batch(manager, provider, feed, session_id, *, deadline=None, timeout=None):
    result = {'provider': provider.name, 'candidates': [], 'legacy_text': '', 'feed': feed, 'error': None}
    timeout = manager._external_prefetch_timeout if timeout is None else timeout
    if deadline is not None:
        timeout = min(timeout, max(0, deadline - time.monotonic()))
    if timeout <= 0:
        result['error'] = 'recall_deadline_exhausted'
        return result
    structured = getattr(provider, 'structured_recall_api_version', 0) == 1
    box = {}
    def run():
        try:
            box['value'] = (provider.prefetch_candidates(dict(feed), session_id=session_id) if structured
                            else provider.prefetch(feed['query'], session_id=session_id))
        except Exception as exc:
            box['error'] = type(exc).__name__
    with manager._external_prefetch_lock:
        existing = manager._external_prefetch_threads.get(provider.name)
        if existing and existing.is_alive():
            result['error'] = 'previous_recall_still_running'
            return result
        worker = spawn_context_thread(run, name=f'{provider.name}-structured-recall')
        manager._external_prefetch_threads[provider.name] = worker
        worker.start()
    worker.join(min(timeout, max(0, deadline - time.monotonic())) if deadline is not None else timeout)
    if worker.is_alive():
        result['error'] = 'recall_timeout'
        return result
    with manager._external_prefetch_lock:
        if manager._external_prefetch_threads.get(provider.name) is worker:
            manager._external_prefetch_threads.pop(provider.name, None)
    value = box.get('value')
    if not structured:
        from agent.memory_manager import spill_if_oversized
        if isinstance(value, str):
            result['legacy_text'] = spill_if_oversized(value, session_id=session_id,
                source=f'{provider.name} memory prefetch', config=manager._external_prefetch_spill_config) if value else ''
        else:
            result['error'] = box.get('error') or 'invalid_legacy_response'
        return result
    if not isinstance(value, dict) or not isinstance(value.get('candidates'), list):
        result['error'] = box.get('error') or 'invalid_structured_response'
        return result
    if any(not isinstance(r, dict) or not isinstance(r.get('id'), str)
           or not isinstance(r.get('text'), str) for r in value['candidates']):
        result['error'] = 'invalid_candidate'
        return result
    result.update(candidates=value['candidates'], error=value.get('error'),
                  feed_hash=value.get('feed_hash'), query_provenance=value.get('query_provenance'),
                  query_errors=value.get('query_errors', []))
    return result
