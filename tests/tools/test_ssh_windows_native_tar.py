"""Opt-in native Windows tar for SSH bulk upload to MSYS/Git Bash remotes."""

from types import SimpleNamespace

import pytest

from tools.environments import ssh as ssh_env
from tools.environments.ssh import SSHEnvironment

BASE = "/c/Users/tester/.hermes"
POSIX_EXTRACT = f"tar xf - --no-overwrite-dir -C {BASE}"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: "/usr/bin/ssh")
    monkeypatch.setattr(ssh_env.SSHEnvironment, "_establish_connection", lambda self: None)
    monkeypatch.setattr(ssh_env.SSHEnvironment, "_detect_remote_home", lambda self: "/c/Users/tester")
    monkeypatch.setattr(ssh_env.SSHEnvironment, "init_session", lambda self: None)
    monkeypatch.delenv("HERMES_SSH_WINDOWS_NATIVE_TAR", raising=False)
    return SSHEnvironment(host="example.com", user="tester", sync_files=False)


def _uname(env, monkeypatch, *, stdout="", returncode=0, exc=None):
    calls = []

    def fake_run_ssh(cmd, timeout=None, **_):
        calls.append(cmd)
        if exc is not None:
            raise exc
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(env, "_run_ssh", fake_run_ssh)
    return calls


def test_gate_off_keeps_posix_extract_without_probe(env, monkeypatch):
    calls = _uname(env, monkeypatch, stdout="MINGW64_NT-10.0-19045\n")
    assert env._remote_extract_command(BASE) == POSIX_EXTRACT
    assert calls == []


@pytest.mark.parametrize("uname", ["MINGW64_NT-10.0-19045", "MSYS_NT-10.0", "CYGWIN_NT-10.0"])
def test_gate_on_msys_remote_uses_native_tar(env, monkeypatch, uname):
    monkeypatch.setenv("HERMES_SSH_WINDOWS_NATIVE_TAR", "1")
    _uname(env, monkeypatch, stdout=uname + "\n")
    cmd = env._remote_extract_command(BASE)
    assert '"$(cygpath -u "$SYSTEMROOT")/System32/tar.exe" -xf - ' in cmd
    assert f'-C "$(cygpath -w {BASE})"' in cmd
    assert "--no-overwrite-dir" not in cmd


def test_gate_on_linux_remote_keeps_posix_extract(env, monkeypatch):
    monkeypatch.setenv("HERMES_SSH_WINDOWS_NATIVE_TAR", "true")
    _uname(env, monkeypatch, stdout="Linux\n")
    assert env._remote_extract_command(BASE) == POSIX_EXTRACT


def test_probe_failure_falls_back_and_is_cached(env, monkeypatch):
    monkeypatch.setenv("HERMES_SSH_WINDOWS_NATIVE_TAR", "on")
    calls = _uname(env, monkeypatch, exc=RuntimeError("connection lost"))
    assert env._remote_extract_command(BASE) == POSIX_EXTRACT
    assert env._remote_extract_command(BASE) == POSIX_EXTRACT
    assert calls == ["uname -s"]


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no"])
def test_gate_values_that_stay_off(env, monkeypatch, value):
    monkeypatch.setenv("HERMES_SSH_WINDOWS_NATIVE_TAR", value)
    calls = _uname(env, monkeypatch, stdout="MINGW64_NT-10.0\n")
    assert env._remote_extract_command(BASE) == POSIX_EXTRACT
    assert calls == []
