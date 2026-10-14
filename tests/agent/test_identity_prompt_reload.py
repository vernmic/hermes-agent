"""Identity-file stamp: a MEMORY.md / USER.md / SOUL.md edit rebuilds once."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.conversation_loop import _restore_or_build_system_prompt
from agent.system_prompt import identity_files_digest, identity_files_prompt_stale, identity_files_stamp


def test_digest_changes_when_memory_changes(tmp_path):
    (tmp_path / "memories").mkdir()
    (tmp_path / "memories" / "MEMORY.md").write_text("old\n", encoding="utf-8")
    before = identity_files_digest(tmp_path)
    (tmp_path / "memories" / "MEMORY.md").write_text("new\n", encoding="utf-8")
    assert identity_files_digest(tmp_path) != before


def test_unstamped_prompt_for_a_real_home_is_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent.system_prompt._agent_home",
        lambda agent: tmp_path,
    )
    agent = SimpleNamespace()
    assert identity_files_stamp(agent).startswith("Identity-files: ")
    assert identity_files_prompt_stale(agent, "stored prompt without a stamp")


def test_matching_stamp_is_not_stale(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.system_prompt._agent_home", lambda agent: tmp_path)
    agent = SimpleNamespace()
    stored = f"body\n\n{identity_files_stamp(agent)}"
    assert identity_files_prompt_stale(agent, stored) is False


def test_restore_rebuilds_once_when_memory_changes(tmp_path, monkeypatch):
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "MEMORY.md").write_text("version-one\n", encoding="utf-8")
    monkeypatch.setattr("agent.system_prompt._agent_home", lambda agent: tmp_path)

    db = MagicMock()
    db.get_session.return_value = {"system_prompt": "OLD PROMPT"}
    agent = MagicMock()
    agent._cached_system_prompt = None
    agent.session_id = "s1"
    agent.model = "m"
    agent.provider = "p"
    agent.platform = "cli"
    agent._session_db = db
    agent._use_prompt_caching = False
    agent._memory_store = MagicMock()
    agent._build_system_prompt = MagicMock(return_value="REBUILT")
    agent.enabled_toolsets = agent.disabled_toolsets = None

    monkeypatch.setattr(
        "agent.conversation_loop._stored_prompt_matches_runtime",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "agent.conversation_loop._bot_chat_prompt_stale",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "agent.conversation_loop.stage_surface_switch_note",
        lambda *a, **k: None,
    )

    _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])

    agent._memory_store.load_from_disk.assert_called_once()
    agent._build_system_prompt.assert_called_once()
    assert agent._cached_system_prompt == "REBUILT"
    db.update_system_prompt.assert_called_once()
