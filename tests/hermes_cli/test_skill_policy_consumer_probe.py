"""W01 consumer probe for the proposed ``pre_skill_batch`` / ``skill_policy.required`` host contract.

A deterministic synthetic stand-in for an external skill-policy consumer (modeled on the
Jev ``pre_skill_write`` judge) is registered through a real ``PluginContext`` and exercised
through the real ``skill_manage`` entry points against a temporary skill root:

* the consumer's "model" is a pure function (no provider, no network);
* host dispatch (``_gate_skill_batch`` -> ``invoke_policy_hook`` -> bounded callback worker),
  the real ``skill_policy.required`` config read and the real mutation handlers are NOT mocked;
* only the interactive write-approval staging (``_run_write_gate``) and post-success
  bookkeeping (``_record_success``: ledger/prompt cache) are stubbed, as in the existing
  lifecycle-hook suite.

Harness preconditions live in fixtures, so a setup problem is reported by pytest as an
ERROR, while a contract violation is a FAILED assertion.

Tests named ``test_gap_*`` characterize current behavior that a required policy does NOT
cover. They pass when the gap is still present and exist to keep the gap visible.
"""

from __future__ import annotations

import contextvars
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import plugins as plugins_mod
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest, VALID_HOOKS

POLICY_HOOK = "pre_skill_batch"
BLOCK_MARKER = "SYNTHETIC-POLICY-REFUSE"
PROBE_ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar("w01_probe_actor", default="unset")


def skill_md(name: str, body: str = "Step 1: Do the thing.") -> str:
    return f"---\nname: {name}\ndescription: Synthetic W01 probe skill.\n---\n\n# {name}\n\n{body}\n"


def tree(root: Path) -> dict[str, str]:
    """Relative path -> sha256 of every file under the temporary skill root."""
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and ".locks" not in p.parts}


class SyntheticConsumer:
    """External policy consumer ported to ``pre_skill_batch``.

    ``mode``: ``"model"`` (deterministic verdict: refuse when any operation text carries
    BLOCK_MARKER), ``"raise"`` (callback failure), ``"legacy_allow"`` (returns the old
    ``pre_skill_write`` ``{"action": "allow"}`` shape without a digest echo).
    Refusals are kept as review recommendations; recording never authorizes a write.
    """

    def __init__(self, root: Path, mode: str = "model"):
        self.root = root
        self.mode = mode
        self.calls: list[dict] = []
        self.recommendations: list[dict] = []

    @staticmethod
    def model(operations) -> str:
        text = json.dumps(operations, sort_keys=True)
        return "decline" if BLOCK_MARKER in text else "allow"

    def pre_skill_batch(self, *, operations, payload_digest, **kwargs):
        self.calls.append({
            "operations": copy.deepcopy(operations),
            "payload_digest": payload_digest,
            "tree_at_decision": tree(self.root),
            "actor": PROBE_ACTOR.get(),
            "extra_kwargs": sorted(kwargs),
        })
        if self.mode == "raise":
            raise RuntimeError("synthetic policy backend crashed")
        if self.mode == "legacy_allow":
            return {"action": "allow"}
        if self.model(operations) == "decline":
            self.recommendations.append({"payload_digest": payload_digest,
                                         "operations": copy.deepcopy(operations),
                                         "disposition": "gate_refuse_evidence"})
            return {"action": "block", "message": "synthetic policy declined the proposal",
                    "gate": {"disposition": "gate_refuse_evidence"}}
        return {"action": "approve", "payload_digest": payload_digest}

    def register(self, ctx) -> None:
        ctx.register_hook(POLICY_HOOK, self.pre_skill_batch)


# ---------------------------------------------------------------- harness fixtures


@pytest.fixture
def host(tmp_path, monkeypatch):
    if POLICY_HOOK not in VALID_HOOKS:
        pytest.fail("HARNESS: this host does not advertise pre_skill_batch; run the "
                    "capability test for stock-host behavior", pytrace=False)
    from tools import skill_manager_tool as smt

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    if hasattr(manager, "_discovered"):
        manager._discovered = True  # never discover real user/bundled plugins
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
    monkeypatch.setattr(plugins_mod, "_plugin_managers_by_home", {})
    root = tmp_path / "skills"
    root.mkdir()
    native = SimpleNamespace(write_gate=smt._run_write_gate, record_success=smt._record_success)
    with (
        patch("tools.skill_manager_tool.SKILLS_DIR", root),
        patch("agent.skill_utils.get_all_skills_dirs", return_value=[root]),
        patch("tools.skill_manager_tool._run_write_gate", return_value=None),
        patch("tools.skill_manager_tool._record_success"),
    ):
        from hermes_cli.lifecycle import has_hook

        assert not has_hook(POLICY_HOOK), "HARNESS: a policy hook leaked into the isolated host"
        assert smt._required_skill_policy_hook() is None, "HARNESS: skill_policy.required leaked in"

        def require_policy(extra: str = ""):
            from hermes_cli.config import get_config_path
            get_config_path().write_text(f"skill_policy:\n  required: {POLICY_HOOK}\n{extra}", encoding="utf-8")
            assert smt._required_skill_policy_hook() == POLICY_HOOK, \
                "HARNESS: real config read did not return skill_policy.required"

        def install(consumer: SyntheticConsumer) -> SyntheticConsumer:
            consumer.register(PluginContext(PluginManifest(name="w01-synthetic-consumer", source="user"),
                                            manager))
            assert has_hook(POLICY_HOOK), "HARNESS: PluginContext registration not visible to dispatch"
            return consumer

        def seed():
            created = smt._create_skill("probe-skill", skill_md("probe-skill"))
            assert created["success"], f"HARNESS: seeding failed: {created}"
            note = root / "probe-skill" / "references" / "note.md"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text("note", encoding="utf-8")

        yield SimpleNamespace(root=root, smt=smt, require_policy=require_policy,
                              install=install, seed=seed, native=native)


def run_batch(smt, ops):
    return json.loads(smt.skill_manage(action="", name="", operations=ops))


def batch_ops(text: str = "note"):
    return [
        {"name": "fresh-skill", "action": "create", "content": skill_md("fresh-skill")},
        {"name": "fresh-skill", "action": "write_file", "file_path": "references/n.md",
         "file_content": text},
    ]


FLAT = {
    "create": dict(name="fresh-skill", content=skill_md("fresh-skill")),
    "edit": dict(name="probe-skill", content=skill_md("probe-skill", "Step 1: Edited.")),
    "patch": dict(name="probe-skill", old_string="Do the thing.", new_string="Do it safely."),
    "write_file": dict(name="probe-skill", file_path="references/new.md", file_content="new"),
    "remove_file": dict(name="probe-skill", file_path="references/note.md"),
    "delete": dict(name="probe-skill", absorbed_into=""),
}


def effect_present(root: Path, action: str) -> bool:
    skill = root / "probe-skill"
    return {
        "create": lambda: (root / "fresh-skill" / "SKILL.md").is_file(),
        "edit": lambda: "Edited." in (skill / "SKILL.md").read_text(encoding="utf-8"),
        "patch": lambda: "Do it safely." in (skill / "SKILL.md").read_text(encoding="utf-8"),
        "write_file": lambda: (skill / "references" / "new.md").is_file(),
        "remove_file": lambda: not (skill / "references" / "note.md").exists(),
        "delete": lambda: not skill.exists(),
    }[action]()


def flat(smt, action):
    return json.loads(smt.skill_manage(action=action, **FLAT[action]))


# ---------------------------------------------------------------- batch path


def test_allow_batch_sees_full_payload_once_before_any_mutation(host):
    host.require_policy()
    consumer = host.install(SyntheticConsumer(host.root))
    ops = batch_ops()
    result = run_batch(host.smt, ops)
    assert result["success"] is True, result
    assert len(consumer.calls) == 1
    call = consumer.calls[0]
    assert call["operations"] == ops
    assert call["payload_digest"] == host.smt._skill_batch_payload_digest(ops)
    assert call["tree_at_decision"] == {}  # nothing existed when the policy decided
    assert (host.root / "fresh-skill" / "references" / "n.md").is_file()
    assert consumer.recommendations == []


def test_block_batch_refuses_whole_batch_and_keeps_recommendation(host):
    host.require_policy()
    consumer = host.install(SyntheticConsumer(host.root))
    result = run_batch(host.smt, batch_ops(text=BLOCK_MARKER))
    assert result["success"] is False
    assert result["error"].startswith("Skill batch policy denied:"), result
    assert tree(host.root) == {}
    assert len(consumer.calls) == 1 and len(consumer.recommendations) == 1
    assert consumer.recommendations[0]["payload_digest"] == consumer.calls[0]["payload_digest"]


def test_required_policy_absent_refuses_batch_as_unavailable(host):
    host.require_policy()  # no consumer installed
    result = run_batch(host.smt, batch_ops())
    assert result["success"] is False
    assert result["error"].startswith("Skill batch policy unavailable:"), result
    assert "not registered" in result["error"]
    assert tree(host.root) == {}


def test_required_policy_failed_callback_is_unavailable_not_denied(host):
    host.require_policy()
    consumer = host.install(SyntheticConsumer(host.root, mode="raise"))
    result = run_batch(host.smt, batch_ops())
    assert result["success"] is False
    assert result["error"].startswith("Skill batch policy unavailable:"), result
    assert len(consumer.calls) == 1
    assert tree(host.root) == {}


def test_optional_policy_failed_callback_fails_open(host):
    # No skill_policy.required: a crashing optional consumer never blocks (documented default).
    consumer = host.install(SyntheticConsumer(host.root, mode="raise"))
    result = run_batch(host.smt, batch_ops())
    assert result["success"] is True, result
    assert len(consumer.calls) == 1


def test_no_policy_selected_keeps_native_behavior(host):
    batch = run_batch(host.smt, batch_ops())
    assert batch["success"] is True, batch
    single = json.loads(host.smt.skill_manage(action="create", name="other-skill",
                                              content=skill_md("other-skill")))
    assert single["success"] is True, single


def test_legacy_allow_shape_is_not_a_required_decision(host):
    # The old pre_skill_write result {"action": "allow"} must be translated to an
    # approve directive echoing payload_digest; untranslated it is "no valid decision".
    consumer = host.install(SyntheticConsumer(host.root, mode="legacy_allow"))
    optional = run_batch(host.smt, batch_ops())
    assert optional["success"] is True, optional
    host.require_policy()
    required = json.loads(host.smt.skill_manage(action="create", name="later-skill",
                                                content=skill_md("later-skill")))
    assert required["success"] is False
    assert required["error"].startswith("Skill batch policy unavailable:"), required
    assert len(consumer.calls) == 2


def test_callback_runs_with_caller_context(host):
    host.require_policy()
    consumer = host.install(SyntheticConsumer(host.root))
    token = PROBE_ACTOR.set("unattended-review")
    try:
        result = run_batch(host.smt, batch_ops())
    finally:
        PROBE_ACTOR.reset(token)
    assert result["success"] is True, result
    assert consumer.calls[0]["actor"] == "unattended-review"


# ---------------------------------------------------------------- operations[] subset


def test_batch_rejects_edit_and_non_sole_delete_before_policy(host):
    host.seed()
    host.require_policy()
    consumer = host.install(SyntheticConsumer(host.root))
    before = tree(host.root)
    edit = run_batch(host.smt, [{"name": "probe-skill", "action": "edit",
                                 "content": skill_md("probe-skill", "x")}])
    mixed = run_batch(host.smt, [{"name": "probe-skill", "action": "write_file",
                                  "file_path": "references/a.md", "file_content": "a"},
                                 {"name": "probe-skill", "action": "delete"}])
    assert edit["success"] is False and "unknown action 'edit'" in edit["error"], edit
    assert mixed["success"] is False and "delete must be the SOLE op" in mixed["error"], mixed
    assert consumer.calls == []
    assert tree(host.root) == before


def test_batch_full_rewrite_and_remove_file_are_one_decision(host):
    """operations[] has no edit: a full rewrite is patch+content. Together with remove_file
    it is still one pre_skill_batch decision covering the whole payload."""
    host.seed()
    host.require_policy()
    consumer = host.install(SyntheticConsumer(host.root))
    rewritten = skill_md("probe-skill", "Step 1: Rewritten.")
    ops = [{"name": "probe-skill", "action": "patch", "content": rewritten},
           {"name": "probe-skill", "action": "remove_file", "file_path": "references/note.md"}]
    result = run_batch(host.smt, ops)
    assert result["success"] is True, result
    assert len(consumer.calls) == 1
    assert consumer.calls[0]["operations"] == ops
    assert consumer.calls[0]["payload_digest"] == host.smt._skill_batch_payload_digest(ops)
    assert (host.root / "probe-skill" / "SKILL.md").read_text(encoding="utf-8") == rewritten
    assert not (host.root / "probe-skill" / "references" / "note.md").exists()


SOLE_DELETE = [{"name": "probe-skill", "action": "delete", "absorbed_into": ""}]


def test_sole_delete_operations_shape_is_judged_once_on_the_flat_payload(host):
    """W02-H1: the sole-delete operations[] call is ONE judgment. The batch route hands the
    op to flat ``skill_manage(action="delete")``, whose single gate judges the flat-equivalent
    payload (``_flat_gate_operation``) - the same rule the staged record is re-hashed with."""
    host.seed()
    host.require_policy()
    consumer = host.install(SyntheticConsumer(host.root))
    result = run_batch(host.smt, SOLE_DELETE)
    assert result["success"] is True, result
    (call,) = consumer.calls
    flat_op = host.smt._flat_gate_operation(
        "delete", "probe-skill", {"absorbed_into": "", "replace_all": False})
    assert call["operations"] == [flat_op]
    assert call["payload_digest"] == host.smt._skill_batch_payload_digest([flat_op])
    assert not (host.root / "probe-skill").exists()


def _staged_skill_records():
    from tools import write_approval as wa
    return wa.list_pending(wa.SKILLS)


def test_sole_delete_native_staging_replay_and_tamper(host):
    """W02-H1 on the native path: real write-approval staging, real ``/skills approve``
    applier (``_apply_one`` -> ``apply_skill_pending``) and real success bookkeeping."""
    from hermes_cli.write_approval_commands import _apply_one
    from tools import write_approval as wa

    host.seed()
    host.require_policy("skills:\n  write_approval: true\n")
    consumer = host.install(SyntheticConsumer(host.root))
    with patch.object(host.smt, "_run_write_gate", host.native.write_gate), \
            patch.object(host.smt, "_record_success", host.native.record_success):
        staged = run_batch(host.smt, SOLE_DELETE)
        assert staged.get("staged") is True, staged
        (call,) = consumer.calls
        (rec,) = _staged_skill_records()
        assert rec["payload"]["payload_digest"] == call["payload_digest"]
        assert (host.root / "probe-skill" / "SKILL.md").is_file()  # nothing deleted yet

        tampered = dict(rec, payload=dict(rec["payload"], absorbed_into="other-skill"))
        ok, error, _ = _apply_one(wa.SKILLS, tampered, None)
        assert not ok and "payload binding" in error
        assert (host.root / "probe-skill" / "SKILL.md").is_file()

        ok, error, _ = _apply_one(wa.SKILLS, rec, None)
        assert ok, error
    assert len(consumer.calls) == 1  # replay never re-asks the policy
    assert not (host.root / "probe-skill").exists()


def test_sole_delete_without_policy_is_unchanged(host):
    host.seed()
    result = run_batch(host.smt, SOLE_DELETE)
    assert result["success"] is True, result
    assert not (host.root / "probe-skill").exists()


# ---------------------------------------------------------------- single-operation (flat) path


@pytest.mark.parametrize("action", list(FLAT))
def test_flat_action_required_allow_dispatches_one_single_op_decision(host, action):
    host.seed()
    host.require_policy()
    consumer = host.install(SyntheticConsumer(host.root))
    result = flat(host.smt, action)
    assert result["success"] is True, result
    assert len(consumer.calls) == 1
    (op,) = consumer.calls[0]["operations"]
    assert op["action"] == action and op["name"] == FLAT[action]["name"]
    assert effect_present(host.root, action)


@pytest.mark.parametrize("action", list(FLAT))
def test_flat_action_required_absent_is_refused_unavailable(host, action):
    host.seed()
    host.require_policy()
    before = tree(host.root)
    result = flat(host.smt, action)
    assert result["success"] is False
    assert result["error"].startswith("Skill batch policy unavailable:"), result
    assert tree(host.root) == before


@pytest.mark.parametrize("action", list(FLAT))
def test_flat_action_declined_is_refused_without_effect(host, action, monkeypatch):
    host.seed()
    host.require_policy()
    consumer = host.install(SyntheticConsumer(host.root))
    monkeypatch.setattr(SyntheticConsumer, "model", staticmethod(lambda ops: "decline"))
    before = tree(host.root)
    result = flat(host.smt, action)
    assert result["success"] is False
    assert result["error"].startswith("Skill batch policy denied:"), result
    assert tree(host.root) == before
    assert len(consumer.recommendations) == 1


# ---------------------------------------------------------------- uncovered entry points (gaps)


def test_gap_direct_handlers_bypass_required_policy(host):
    """Dashboard ``POST /api/skills`` / ``PUT /api/skills/content`` and
    ``agent.learning_mutations`` call ``_create_skill`` / ``_edit_skill`` directly.
    With a required policy configured and NO policy plugin present, those entries still
    mutate and ``pre_skill_batch`` is never dispatched."""
    host.require_policy()
    created = host.smt._create_skill("direct-skill", skill_md("direct-skill"))
    edited = host.smt._edit_skill("direct-skill", skill_md("direct-skill", "Step 1: Direct edit."))
    assert created["success"] is True and edited["success"] is True
    assert "Direct edit." in (host.root / "direct-skill" / "SKILL.md").read_text(encoding="utf-8")


def test_gap_legacy_staged_replay_without_digest_skips_required_policy(host):
    """A staged record without ``payload_digest`` (e.g. staged before the policy became
    required) replays through ``apply_skill_pending`` without any policy decision."""
    host.require_policy()
    result = json.loads(host.smt.apply_skill_pending(
        {"action": "create", "name": "replayed-skill", "content": skill_md("replayed-skill")}))
    assert result["success"] is True, result
    assert (host.root / "replayed-skill" / "SKILL.md").is_file()
