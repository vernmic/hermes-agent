# Optional turn selection and skill policy

The fork branch `codex/jev-turn-gate` adds two optional plugin boundaries:
`pre_memory_recall` selects a recall feed and ephemeral current-turn context;
`pre_skill_write` judges the complete operation before a skill mutation.
Providers opt into `structured_recall_api_version = 1` to return identified
candidates and provenance. Providers without that capability retain their
legacy text recall. Selection does not rewrite the cached system prompt or
prior transcript messages. Calls inherit the active profile and secret scope.

A read selector can run off, in shadow, or enforced. The external policy owns
those modes. A missing selector keeps baseline recall; an invalid second-pass
selection withholds structured candidates while retaining legacy providers.
A timed-out provider cannot accumulate overlapping recall workers.

Unattended policy enforcement is explicitly configured. When
`skills.unattended_write_policy_required` is true, an unavailable policy refuses
unattended writes. Atomic batches are judged before mutation, and the judgment
is bound to all operations rather than to a later subset. Failed/refused proposals
can be durably captured and delivered to a review board with bounded retries.
The external Jev plugin and operator tools are maintained in
[vernmic/hindsight](https://github.com/vernmic/hindsight/tree/codex/hermes-memory-quality/hindsight-integrations/hermes/extras/hermes_ops).

Source baseline: `370ada99cc` (the checked-out fork baseline at publication).
The installed October 7 gateway uses `f97608f178` with adapted local changes.
These are separate receipts. Fetching upstream does not mean the host patch
has been ported to a newer upstream commit, and a source push does not install it.
Future changes belong in this fork, with tests and a reviewed update plan;
workspace patch bundles are deployment history, not canonical editing locations.
The source behavior checks are run with `scripts/run_tests.sh`.

The fork-only `customizations/hermes-memory` branch adds the deployed
workflow-builder evidence reminder to both skill-writing background review
prompts. It depends on `codex/jev-turn-gate`. This reminder does not replace
the optional deterministic policy hook and does not enable the write gate.
