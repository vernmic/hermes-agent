"""Native-backend integration test for scripts/pre_evolution_snapshot.py.

Runs against THIS test's actual checkout (REPO_ROOT) using the
runner-selected interpreter (sys.executable -- the same python -m pytest
process the canonical scripts/run_tests.sh spawns). Both are passed
EXPLICITLY to the wrapper as --hermes-repo/--native-python; there is no
environment gating and no fallback to any private installation. The
interpreter path is passed verbatim (symlink preserved, never resolved).

All backup/mutation/restore targets live under tmp_path.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "scripts" / "pre_evolution_snapshot.py"

# Runner-selected interpreter, verbatim (symlink NOT resolved) and the
# checkout this test file lives in -- no env knobs, no private fallbacks.
NATIVE_REPO = REPO_ROOT
NATIVE_PYTHON = Path(sys.executable)


@pytest.fixture()
def home(tmp_path: Path):
    h = tmp_path / "home"
    (h / "skills" / "demo").mkdir(parents=True)
    (h / "profiles" / "coder" / "skills" / "demo").mkdir(parents=True)
    h.joinpath("config.yaml").write_text("model:\n  provider: example\n")
    h.joinpath(".env").write_text("DUMMY=1\n")
    h.joinpath("skills/demo/SKILL.md").write_text("# demo skill\n")
    (h / "profiles/coder/skills/demo/SKILL.md").write_text("# coder skill\n")
    conn = sqlite3.connect(h / "state.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE sessions (id TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('s1')")
    conn.commit()
    conn.close()
    yield h
    shutil.rmtree(tmp_path, ignore_errors=True)


def _tool(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), *args],
                          capture_output=True, text=True, timeout=900,
                          check=False)


def test_native_backup_and_scoped_rollback_on_real_checkout(home, tmp_path):
    """Full loop against the real backend: snapshot (native run_backup via
    the runner-selected interpreter), mutate, scoped rollback incl.
    profile-local skills, parity verify."""
    backups = tmp_path / "backups"
    backups.mkdir()
    common = ["--home", str(home), "--backups-dir", str(backups),
              "--hermes-repo", str(NATIVE_REPO),
              "--installs-root", str(NATIVE_REPO),
              "--native-python", str(NATIVE_PYTHON),
              "--allow-open-memory-gate"]

    proc = _tool("snapshot", "--label", "native1", *common)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    artifact = backups / "pre-evolution-native1.zip"
    assert artifact.is_file()

    # mutate: root skill + profile-local skill changed, config broken
    (home / "skills/demo/SKILL.md").write_text("# mutated\n")
    (home / "profiles/coder/skills/demo/SKILL.md").write_text("# mutated\n")
    (home / "config.yaml").write_text("model:\n  provider: broken\n")
    proc = _tool("rollback", "--artifact", str(artifact), "--home", str(home),
                 "--hermes-repo", str(NATIVE_REPO),
                 "--installs-root", str(NATIVE_REPO))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (home / "skills/demo/SKILL.md").read_text() == "# demo skill\n"
    assert (home / "profiles/coder/skills/demo/SKILL.md").read_text() == \
        "# coder skill\n"
    assert (home / "config.yaml").read_text() == "model:\n  provider: example\n"

    proc = _tool("verify", "--artifact", str(artifact), "--home", str(home),
                 "--hermes-repo", str(NATIVE_REPO),
                 "--installs-root", str(NATIVE_REPO))
    assert proc.returncode == 0 and "IN PARITY" in proc.stdout

    # WAL-consistent capture: the archived state.db holds committed rows
    with zipfile.ZipFile(artifact) as zf:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            zf.extract("state.db", td)
            conn = sqlite3.connect(Path(td) / "state.db")
            rows = [r[0] for r in conn.execute("SELECT id FROM sessions")]
            conn.close()
    assert rows == ["s1"]


def test_live_root_protection_on_real_entry(home, tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    proc = _tool("snapshot", "--label", "native2",
                 "--home", str(home), "--backups-dir", str(backups),
                 "--hermes-repo", str(NATIVE_REPO),
                 "--installs-root", str(NATIVE_REPO),
                 "--native-python", str(NATIVE_PYTHON),
                 "--allow-open-memory-gate")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # the platform-default root and its aliases are refused as destinations
    default_root = Path(os.environ.get("HERMES_HOME",
                                       str(Path.home() / ".hermes")))
    for bad in (str(default_root), str(default_root.parent)):
        proc = _tool("rollback", "--label", "native2", "--home", bad,
                     "--backups-dir", str(backups),
                     "--hermes-repo", str(NATIVE_REPO),
                     "--installs-root", str(NATIVE_REPO))
        assert proc.returncode == 2
