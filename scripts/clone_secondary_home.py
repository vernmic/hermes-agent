"""Clone Hermes configuration and skills into a fresh secondary runtime home.

Run with the source Hermes interpreter. Session stores, queues and production
schedules are deliberately fresh; credentials are copied locally, never emitted.
"""
import argparse
import datetime
import json
import re
from itertools import chain
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_cli.config import atomic_config_write
from utils import load_yaml_file_readonly

FILES = ('config.yaml', '.env', 'SOUL.md', 'profile.yaml', 'auth.json',
         'MEMORY.md', 'shell-hooks-allowlist.json')
DIRS = ('memories', 'skills', 'plugins', 'hindsight', 'hooks', 'agent-hooks',
        'scripts', 'credentials', 'skins')
TEXT_SUFFIXES = {'.py', '.sh', '.ps1', '.json', '.yaml', '.yml', '.toml', '.md', '.txt'}
MESSAGING_PREFIXES = ('TELEGRAM_', 'EMAIL_', 'DISCORD_', 'SLACK_', 'WHATSAPP_',
                      'MATRIX_', 'SIGNAL_', 'IRC_', 'FEISHU_', 'DINGTALK_')


def clone_home(source, target, api_url, ui_url, *, resume=False):
    if (target / 'config.yaml').exists() and not resume:
        raise RuntimeError(f'Refusing to overwrite configured home: {target}')
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        if (source / name).is_file():
            shutil.copy2(source / name, target / name)
    for name in DIRS:
        if (source / name).is_dir():
            shutil.copytree(source / name, target / name,
                            ignore=shutil.ignore_patterns('__pycache__', '.git', '*.lock', '*.bak*'),
                            dirs_exist_ok=True)
    replacements = ((str(source.parent.parent) if source.parent.name == 'profiles' else str(source),
                     str(target.parent.parent) if target.parent.name == 'profiles' else str(target)),
                    ('http://host.docker.internal:8888', api_url),
                    ('http://localhost:8888', api_url),
                    ('http://host.docker.internal:9999', ui_url),
                    ('http://localhost:9999', ui_url),
                    ('hindsight-hindsight-1', 'hindsight2-hindsight-1'),
                    ('hindsight-postgres-1', 'hindsight2-postgres-1'))
    changed = []
    copied = chain((target / name for name in FILES),
                   *( (target / name).rglob('*') for name in DIRS ))
    for p in copied:
        if not p.is_file() or p.suffix not in TEXT_SUFFIXES and p.name != '.env':
            continue
        try:
            original = p.read_bytes(); text = original.decode('utf-8')
        except (OSError, UnicodeError):
            continue
        for before, after in replacements:
            text = re.sub(re.escape(before) + r"(?![A-Za-z0-9_.-])", lambda m: after, text)
        if p.name == '.env':
            text = '\n'.join(l for l in text.splitlines()
                             if not l.split('=', 1)[0].startswith(MESSAGING_PREFIXES)) + '\n'
        if text.encode('utf-8') != original:
            p.write_bytes(text.encode('utf-8')); changed.append(str(p.relative_to(target)))
    config = load_yaml_file_readonly(target / 'config.yaml')
    config.setdefault('memory', {})['url'] = api_url
    config.setdefault('kanban', {})['dispatch_in_gateway'] = False
    # CLI/dashboard are the selected surfaces; platform definitions remain local
    # in the source, and no secondary gateway is started by this installer.
    for platform in config.get('platforms', {}).values():
        if isinstance(platform, dict): platform['enabled'] = False
    atomic_config_write(target / 'config.yaml', config)
    # Preserve job definitions for review; never carry their running state.
    jobs_path = source / 'cron/jobs.json'
    paused = 0
    if jobs_path.exists():
        jobs = json.loads(jobs_path.read_text())
        for job in jobs.get('jobs', []):
            job.update(enabled=False, state='paused', paused_reason='Hermes2 dashboard/CLI deployment; avoid duplicate production jobs',
                       paused_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
            paused += 1
        (target / 'cron').mkdir(exist_ok=True)
        (target / 'cron/jobs.json').write_text(json.dumps(jobs, indent=2))
    for directory in ('logs', 'sessions', 'workspace', 'state'):
        (target / directory).mkdir(exist_ok=True)
    for secret in (target / '.env', target / 'auth.json'):
        if secret.exists(): secret.chmod(0o600)
    source_skills = {str(p.relative_to(source / 'skills')) for p in (source / 'skills').rglob('SKILL.md')}
    copied_skills = {str(p.relative_to(target / 'skills')) for p in (target / 'skills').rglob('SKILL.md')}
    assert copied_skills == source_skills
    return {'source': str(source), 'target': str(target), 'skills': len(copied_skills),
            'model': config.get('model'), 'paused_jobs': paused, 'retargeted_files': changed,
            'memory_url': config['memory']['url']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--api-url', default='http://host.docker.internal:8889')
    parser.add_argument('--ui-url', default='http://host.docker.internal:9998')
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--resume', action='store_true', help='Resume this helper after a partial copy; overwrites only copied configuration.')
    args = parser.parse_args()
    source, target = args.source.resolve(), args.target.resolve()
    if source == target or source in target.parents or target in source.parents:
        parser.error('Source and target must be separate Hermes homes.')
    records = [clone_home(source, target, args.api_url, args.ui_url, resume=args.resume)]
    for profile in sorted((source / 'profiles').iterdir()):
        if profile.name.startswith('.') or not (profile / 'config.yaml').is_file(): continue
        records.append(clone_home(profile, target / 'profiles' / profile.name, args.api_url, args.ui_url, resume=args.resume))
    args.receipt.write_text(json.dumps({'profiles': records}, indent=2))
    print(json.dumps({'profiles': len(records), 'skills': sum(r['skills'] for r in records),
                      'paused_jobs': sum(r['paused_jobs'] for r in records)}))


if __name__ == '__main__':
    main()
