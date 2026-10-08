"""Turn selectors preserve baseline behavior and only inject into the current turn."""
from types import SimpleNamespace

from agent import turn_selection
from agent.memory_provider import MemoryProvider


def test_shadow_and_enforced_selection_keep_legacy_provider_and_system_prompt(monkeypatch):
    from agent.memory_manager import MemoryManager

    class Legacy(MemoryProvider):
        name = 'legacy'
        def is_available(self): return True
        def initialize(self, *args, **kwargs): pass
        def get_tool_schemas(self): return []
        def prefetch(self, query, **kwargs): return 'legacy recall'

    manager = MemoryManager()
    manager.add_provider(Legacy())
    agent = SimpleNamespace(_memory_manager=manager, session_id='s', _user_turn_count=1,
                            platform='cli', _parent_session_id='', _persist_disabled=False)
    history = [{'role': 'user', 'content': 'current task'}]
    original = list(history)
    calls = []
    plan = {'apply': False, 'feed': {'query': 'chosen query'}, 'context': 'skill pointer',
            'select_results': lambda batches: calls.append(batches) or 'selected'}
    monkeypatch.setattr(turn_selection, '_plans', lambda **kwargs: [plan])
    baseline = lambda: 'baseline'
    assert turn_selection.select_turn_context(agent, history, 'current task', baseline) == ('baseline', '')
    assert len(calls) == 1
    plan['apply'] = True
    notified = []
    # Unsupported providers keep their legacy output; only structured candidates are filtered.
    assert turn_selection.select_turn_context(agent, history, 'current task', baseline,
        notify_turn=lambda: notified.append('once')) == ('legacy recall', 'skill pointer')
    assert notified == ['once']
    assert history == original


def test_selector_failure_discards_candidates_without_hiding_legacy(monkeypatch):
    def broken(batches): raise ValueError('invalid decision')
    batches = [{'provider': 'records', 'candidates': [{'id': 'one', 'text': 'record'}], 'legacy_text': ''},
               {'provider': 'legacy', 'candidates': [], 'legacy_text': 'legacy text'}]
    manager = SimpleNamespace(prefetch_structured=lambda *args, **kwargs: batches)
    agent = SimpleNamespace(_memory_manager=manager, session_id='s', platform='cli',
                            _parent_session_id='', _user_turn_count=1, _persist_disabled=False)
    monkeypatch.setattr(turn_selection, '_plans', lambda **kwargs: [
        {'apply': True, 'feed': {'query': 'task'}, 'context': '', 'select_results': broken}])
    assert turn_selection.select_turn_context(agent, [], 'task', lambda: 'baseline') == ('legacy text', '')
