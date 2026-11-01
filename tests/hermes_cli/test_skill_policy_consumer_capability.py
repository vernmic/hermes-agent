"""Host-capability probe for an external skill-policy consumer (runs on any Hermes host).

On a host that advertises ``pre_skill_batch`` the configured required policy is enforced
through the real dispatch. On a stock host without the capability, the same consumer
registration and ``skill_policy.required`` setting are silently ignored, so a consumer
that needs enforcement must detect the capability itself and report incompatibility.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from hermes_cli import plugins as plugins_mod
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest, VALID_HOOKS


def test_required_consumer_capability_on_this_host(tmp_path, monkeypatch):
    from tools import skill_manager_tool as smt

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    if hasattr(manager, "_discovered"):
        manager._discovered = True
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
    monkeypatch.setattr(plugins_mod, "_plugin_managers_by_home", {})
    from hermes_cli.config import get_config_path
    get_config_path().write_text("skill_policy:\n  required: pre_skill_batch\n", encoding="utf-8")

    calls = []

    def policy(*, operations, payload_digest, **_kw):
        calls.append(len(operations))
        return {"action": "approve", "payload_digest": payload_digest}

    PluginContext(PluginManifest(name="w01-capability-consumer", source="user"),
                  manager).register_hook("pre_skill_batch", policy)

    root = tmp_path / "skills"
    root.mkdir()
    content = "---\nname: cap-skill\ndescription: Capability probe.\n---\n\n# cap\n"
    with (
        patch("tools.skill_manager_tool.SKILLS_DIR", root),
        patch("agent.skill_utils.get_all_skills_dirs", return_value=[root]),
        patch("tools.skill_manager_tool._run_write_gate", return_value=None),
        patch("tools.skill_manager_tool._record_success"),
    ):
        result = json.loads(smt.skill_manage(action="create", name="cap-skill", content=content))

    advertised = "pre_skill_batch" in VALID_HOOKS
    print("W01-CAPABILITY", json.dumps({
        "advertises_pre_skill_batch": advertised,
        "has_gate_skill_batch": hasattr(smt, "_gate_skill_batch"),
        "create_success": result.get("success"),
        "policy_calls": calls,
        "skill_md_written": (root / "cap-skill" / "SKILL.md").is_file(),
    }, sort_keys=True))
    assert result.get("success") is True, result
    if advertised:
        assert calls == [1]  # required policy consulted exactly once
    else:
        # Stock host: no dispatch at all - required setting and consumer are inert.
        assert calls == []
