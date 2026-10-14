import json
from pathlib import Path

import pytest

from scripts.clone_secondary_home import clone_home
from hermes_cli.profiles import _get_profiles_root, resolve_profile
from utils import load_yaml_file_readonly


def test_copy_retargets_memory_and_skills_without_production_work(tmp_path):
    source, target = tmp_path / 'first', tmp_path / 'second'
    source.mkdir()
    (source / 'config.yaml').write_text('model:\n  default: test-model\n  provider: test\nmemory:\n  url: http://host.docker.internal:8888\n')
    (source / '.env').write_text('DEEPSEEK_API_KEY=private-value\nTELEGRAM_BOT_TOKEN=bot-private\nHINDSIGHT_URL=http://host.docker.internal:8888\n')
    skill = source / 'skills/example/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: example\n---\nUse ' + str(source) + '/scripts/check.py\n')
    (source / 'cron').mkdir()
    (source / 'cron/jobs.json').write_text(json.dumps({'jobs': [{'id': 'j1', 'enabled': True, 'state': 'active'}]}))
    (source / 'state.db').write_bytes(b'not-a-session-copy')
    (target / 'installs').mkdir(parents=True)
    metadata = target / 'installs/state.json'
    metadata.write_text(json.dumps({'home': str(target)}))
    expected_metadata = metadata.read_bytes()
    original = (source / '.env').read_bytes()
    result = clone_home(source, target, 'http://host.docker.internal:8889', 'http://host.docker.internal:9998')
    cfg = load_yaml_file_readonly(target / 'config.yaml')
    assert cfg['model'] == {'default': 'test-model', 'provider': 'test'}
    assert cfg['memory']['url'].endswith(':8889')
    assert cfg['kanban']['dispatch_in_gateway'] is False
    env = (target / '.env').read_text()
    assert 'private-value' in env and 'bot-private' not in env
    assert '8888' not in env
    assert str(target) in (target / 'skills/example/SKILL.md').read_text()
    jobs = json.loads((target / 'cron/jobs.json').read_text())['jobs']
    assert jobs[0]['enabled'] is False and jobs[0]['state'] == 'paused'
    assert not (target / 'state.db').exists()
    assert (source / '.env').read_bytes() == original
    assert metadata.read_bytes() == expected_metadata
    assert result['skills'] == 1
    with pytest.raises(RuntimeError, match='Refusing to overwrite'):
        clone_home(source, target, 'different', 'different')


def test_secondary_profile_root_a_b_a(tmp_path, monkeypatch):
    roots = [tmp_path / 'one', tmp_path / 'two']
    for root in roots:
        home = root / 'profiles/coder'
        home.mkdir(parents=True)
        (root / 'config.yaml').write_text('model: {}\n')
        (home / 'config.yaml').write_text('model: {}\n')
    for root in [roots[0], roots[1], roots[0]]:
        monkeypatch.setenv('HERMES_HOME', str(root))
        assert _get_profiles_root() == root / 'profiles'
        assert resolve_profile('coder') == root / 'profiles/coder'
