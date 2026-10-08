"""Structured recall deadlines and profile propagation must not leak later results."""
import threading
import time
from pathlib import Path
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


class Records(MemoryProvider):
    name = 'records'
    structured_recall_api_version = 1
    def is_available(self): return True
    def initialize(self, **kwargs): pass
    def get_tool_schemas(self): return []


def test_per_request_allowance_does_not_change_baseline_or_override_deadline():
    class Delayed(Records):
        def prefetch_candidates(self, feed, **kwargs):
            time.sleep(0.06)
            return {'candidates': [{'id': 'one', 'text': 'useful'}]}
    manager = MemoryManager(external_prefetch_timeout=0.01)
    manager.add_provider(Delayed())
    assert manager.prefetch_structured({'query': 'longer'}, timeout=1)[0]['candidates']
    assert manager._external_prefetch_timeout == 0.01
    started = time.monotonic()
    result = manager.prefetch_structured({'query': 'bounded'}, timeout=1, deadline=started + 0.01)[0]
    assert result['error'] == 'recall_timeout' and not result['candidates']
    assert time.monotonic() - started < 0.5


def test_invalid_request_timeout_is_rejected_before_provider_call():
    import pytest
    manager = MemoryManager()
    for value in (0, -1, float('inf'), float('nan'), True, '30'):
        with pytest.raises(ValueError):
            manager.prefetch_structured({'query': 'test'}, timeout=value)


def test_caller_deadline_limits_both_structured_and_legacy_recall():
    release = threading.Event()
    class SlowRecords(Records):
        def prefetch_candidates(self, feed, **kwargs):
            release.wait(3)
            return {'candidates': [{'id': 'late', 'text': 'late'}]}
    class SlowLegacy(Records):
        structured_recall_api_version = 0
        def prefetch(self, query, **kwargs):
            release.wait(3)
            return 'late'
    try:
        for provider in (SlowRecords(), SlowLegacy()):
            manager = MemoryManager(external_prefetch_timeout=2)
            manager.add_provider(provider)
            started = time.monotonic()
            result = manager.prefetch_structured({'query': 'bounded'}, deadline=started + 0.03)[0]
            assert time.monotonic() - started < 0.5
            assert result['error'] == 'recall_timeout'
            assert result['candidates'] == [] and result['legacy_text'] == ''
    finally:
        release.set()


def test_expired_deadline_skips_providers_and_is_shared_across_fanout():
    calls = []
    class Delayed(Records):
        name = 'first'
        def prefetch_candidates(self, feed, **kwargs):
            calls.append(self.name)
            time.sleep(0.1)
            return {'candidates': []}
    class Next(Delayed):
        name = 'second'
    manager = MemoryManager(external_prefetch_timeout=2)
    manager.add_provider(Delayed())
    # The manager normally has at most one external provider; exercise generic fanout directly.
    manager._providers.append(Next())
    result = manager.prefetch_structured({'query': 'bounded'}, deadline=time.monotonic() + 0.03)
    assert [r['error'] for r in result] == ['recall_timeout', 'recall_deadline_exhausted']
    assert calls == ['first']


def test_timeout_drops_late_candidates_and_prevents_overlapping_calls():
    release = threading.Event()
    entered = threading.Event()
    class Slow(Records):
        def prefetch_candidates(self, feed, **kwargs):
            entered.set()
            release.wait(3)
            return {'candidates': [{'id': 'late', 'text': 'late record'}], 'feed_hash': 'old'}
    manager = MemoryManager(external_prefetch_timeout=0.01)
    manager.add_provider(Slow())
    try:
        first = manager.prefetch_structured({'query': 'first'})[0]
        assert entered.wait(1) and first['candidates'] == [] and first['error'] == 'recall_timeout'
        second = manager.prefetch_structured({'query': 'second'})[0]
        assert second['error'] == 'previous_recall_still_running'
        release.set()
        manager._external_prefetch_threads['records'].join(2)
        assert first['candidates'] == []
    finally:
        release.set()


def test_profile_a_b_a_and_provenance_follow_each_thread(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import get_secret, set_secret_scope, reset_secret_scope
    class Scoped(Records):
        def prefetch_candidates(self, feed, **kwargs):
            home = str(get_hermes_home())
            return {'candidates': [{'id': home, 'text': home}], 'feed_hash': feed['feed_hash'],
                    'query_provenance': [{'query': feed['query'], 'test_secret': get_secret('OPENROUTER_API_KEY')}], 'query_errors': []}
    manager = MemoryManager()
    manager.add_provider(Scoped())
    for profile in ('a', 'b', 'a'):
        home = tmp_path / profile
        home_token = set_hermes_home_override(home)
        secret_token = set_secret_scope({'OPENROUTER_API_KEY': 'test-' + profile}, profile_home=str(home))
        try:
            batch = manager.prefetch_structured({'query': profile, 'feed_hash': profile})[0]
            assert batch['candidates'][0]['id'] == str(home)
            assert batch['feed_hash'] == profile and batch['query_provenance'][0]['query'] == profile
            assert batch['query_provenance'][0]['test_secret'] == 'test-' + profile
        finally:
            reset_secret_scope(secret_token)
            reset_hermes_home_override(home_token)
