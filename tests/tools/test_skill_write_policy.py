"""Policy failure cannot authorize a mutation; scope evidence survives refusals."""
import pytest
from tools import skill_write_policy as policy


@pytest.mark.parametrize('result', [None, {}, {'action': 'block', 'message': 'refused'},
                                   {'action': 'allow'}])
def test_policy_contract_fails_closed_and_preserves_payload(monkeypatch, result):
    from hermes_cli import plugins
    monkeypatch.setattr(plugins, 'has_hook', lambda name: True)
    monkeypatch.setattr(plugins, 'invoke_hook', lambda *args, **kwargs: [result])
    proposed = {'action': 'batch', 'operations': [{'action': 'patch', 'name': 'skill',
                  'old_string': 'old', 'new_string': 'new'}]}
    assert policy.evaluate(proposed, session_id='s')['allowed'] is (result == {'action': 'allow'})
    assert proposed['operations'][0]['old_string'] == 'old'


def test_policy_scope_context_restores_after_write(monkeypatch):
    from tools.skill_review_trigger import current_gate_context
    from hermes_cli import plugins
    monkeypatch.setattr(plugins, 'has_hook', lambda name: True)
    monkeypatch.setattr(plugins, 'invoke_hook', lambda *args, **kwargs: [
        {'action': 'allow', 'gate': {'disposition': 'gate_pass', 'turn_ref': 's:t'}}])
    with policy.write_scope({'action': 'patch', 'name': 'skill'}, session_id='s') as decision:
        assert decision['allowed']
        assert current_gate_context()['disposition'] == 'gate_pass'
    assert current_gate_context() is None


def test_required_policy_missing_or_loader_failure_never_allows_unattended_write(monkeypatch):
    from hermes_cli import plugins
    monkeypatch.setattr(policy, 'required', lambda: True)
    monkeypatch.setattr(plugins, 'has_hook', lambda name: False)
    assert policy.evaluate({'action': 'patch', 'name': 'skill'})['allowed'] is False
    def unavailable(name):
        raise RuntimeError('plugin loader unavailable')
    monkeypatch.setattr(plugins, 'has_hook', unavailable)
    assert policy.evaluate({'action': 'patch', 'name': 'skill'})['allowed'] is False
