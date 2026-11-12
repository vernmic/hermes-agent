# TBLite paired-20 regression gate (Phase 7.2) -- source/offline only

Explicit runner + qualification for the paired baseline-vs-candidate
regression gate on the frozen TBLite 20. Integrates EXISTING pieces
only; no new core tool, service or execution framework.

## Dependencies (both from public origin/main 3d4dd108cf52)

- TBLite preflight source, fork branch evals/tblite-preflight at
  d679d62ea3d08945d3bf3a1342ede132e84b1d2c (5 files):
  freeze_fast_subset.py, pinned_hermes_agent.py, README.md, __init__.py,
  tests/evals/test_tblite_preflight_freeze.py (38 canonical tests).
- T4 benchmark gates, fork branch evals/benchmark-gates at
  00231bee0815d881b3a2de5de0eaacb24d113c34 (4 files): gate1_pytest.py,
  tblite_gate.py (gate2 placeholder), README.md,
  tests/evals/test_benchmark_gates.py (19 canonical tests).

gate2 (tblite_gate.py) STAYS fail-closed NOT RUN by default. This
package is invoked explicitly and never rewrites the placeholder's
exit contract; wiring a PASS into gate2 is the root reviewer's call
after a qualified report exists.

## Concrete protocol (all fields hash-bound in the protocol JSON)

Same for both arms; ONLY the skill revision differs:

- dataset pin 5c37b41f00ce04719a4453061076ae9f46b74b7d, frozen 20
  selection from preflight W01/W02 (selection-v1.json) bound by sha256;
  --expect-selection continuity; never resampled after outcomes.
- Harbor framework pin 500af91374dcc43b59fac54cf90295646617b14d.
- observed Hermes core 93faf1387ce4bb4769de506d47a3c82afb9d0326.
- model glm-5.3 / provider zai via the explicit monthly coding
  endpoint https://api.z.ai/api/coding/paas/v4 only; no general-endpoint
  autodetection, no model fallback.
- max concurrency 1, 2 CPUs, 4096 MB per trial environment.
- baseline vs candidate FULL SKILL.md file sha256 + native --skills
  preload, enforced per trial by the published PinnedHermes adapter
  (measured runtime-identity receipts: live git HEAD, full skill file
  hash, endpoint).

## Production job-schema mapping

`plan` writes one Harbor config per task in the exact schema of the
accepted preflight controls (hermes-gated-control-w03/config.json):
job_name, jobs_dir, n_concurrent_trials=1, docker environment
override_cpus=2 / override_memory_mb=4096, agents[0] = the published
PinnedHermes module with model/base_url/skills, tasks[0].path =
repos/OpenThoughts-TBLite/<task>. `qualify` consumes the per-trial
result.json (task_name, task_checksum, agent_info.model_info,
verifier_result.rewards.reward, exception_info, reward_valid) and the
per-trial agent/runtime-identity.txt receipt -- the REAL Harbor
500af913 schemas, not invented fields.

## Fail-closed qualification

UNQUALIFIED (never 0, never PASS) for: missing/duplicated/swapped/
extra task IDs, partial 20/40 sets, skipped/dropped trials, trial
exceptions/timeouts (infra, not product outcomes), reward_valid=false,
malformed/non-finite/out-of-range rewards, missing identity receipts,
observed core HEAD / skill file sha / endpoint / model drift, task
checksum drift between arms, stale selection (sha mismatch), or an
undeclared policy. A genuine verified task failure is a measured
outcome. All 20 paired rows, deltas, counts and identities land in
paired-report.json.

## PREDECLARED nonregression policy (proposal for root review)

frozen20-paired-no-regression v1, hash-bound in the protocol BEFORE
any outcomes: PASS iff (1) all 40 trials qualified, (2) candidate
reward >= baseline reward on EVERY task, (3) mean candidate >= mean
baseline. Anything else FAILs; no discretionary judgment. Source
support: install-plan 7.2 "Frozen TBLite20 paired tasks must show no
regression". This is a PROPOSAL -- the live gate runs only after root
accepts the policy text into the protocol file; missing/unbound policy
is unqualified.

## Usage

    python evals/tblite_paired_gate/paired_gate.py plan \
        --protocol protocol.json --jobs-root jobs/
    python evals/tblite_paired_gate/paired_gate.py run \
        --protocol protocol.json --jobs-root jobs/ \
        --harbor-command '<explicit pinned harbor invocation> {config}' \
        --stop-after N --timeout S
    python evals/tblite_paired_gate/paired_gate.py qualify \
        --protocol protocol.json --jobs-root jobs/

`run` executes sequentially (max concurrency 1), appends per-job
progress records (no poll loops), supports bounded --stop-after for
background operation, and REQUIRES an explicit harbor command template
-- the gate never guesses a live invocation. The 40-trial run may take
hours and is NOT executed by this source checkpoint; no models,
Docker, providers or live operations run before the reviewed protocol
and candidate prerequisites pass.

## W03 corrections (review 01a10c72, 2026-11-12)

- Production dispatch is TOOLCHAIN-BOUND: the --harbor-command must
  invoke the pinned venv's REAL harbor console entrypoint
  (<venv>/bin/harbor, shebang = the measured interpreter); the venv's
  harbor package must be content-identical to the pinned checkout at
  the JobConfig schema; the checkout HEAD must equal the protocol pin.
  An unrelated executable is rejected before dispatch even when the
  correct --harbor-python/--harbor-source are supplied for measurement.
  `--allow-fixture-command` explicitly labels NON-PRODUCTION fixture
  commands; such runs are stamped fixture-only in run-state.json and
  can NEVER qualify for the live gate.
- STARTED receipts are persisted BEFORE spawning each job (with the
  owned pid/pgid appended at dispatch). A hard parent SIGKILL cannot
  run Python cleanup -- the persisted in-flight receipt is what makes
  any later resume REFUSE that job; owned pid/pgid are recorded for
  explicit operator reconciliation. Catchable SIGINT/SIGTERM boundedly
  terminate the OWNED group, append an interrupted-* terminal receipt,
  and exit 128+signal. Existing run-state/protocol/plan identities are
  re-verified before ANY resume/dispatch.
- Owned-group termination tracks the WHOLE group (never a leader-poll
  short-circuit): a SIGTERM-ignoring child in the group keeps the
  group alive past the leader's exit and is reached by the SIGKILL
  escalation. This is a PROCESS-group guarantee only -- Docker
  container/daemon cleanup for benchmark containers is a SEPARATE
  scope (owned job artifacts, Harbor's own container lifecycle) and is
  NOT claimed here.
- Output wording corrected: RETAINED log artifacts are capped at
  200KB; the TRANSIENT capture during communicate() is not
  memory-bounded. Retained cap != transient capture bound.

## W02 corrections (review 01a10c72, 2026-11-12)

The CLI gained required explicit inputs; previous invocations are
intentionally refused until they supply them:

- `plan --checksum-python PINNED_HARBOR_PYTHON --harbor-source
  PINNED_HARBOR_CHECKOUT`: canonical per-task checksums are MEASURED
  through the pinned Harbor 500af913 Task API and frozen into
  plan.json together with per-config byte hashes and the measured
  dataset checkout HEAD (a mismatched checkout is refused).
- `run --harbor-python INTERPRETER --timeout N` (finite positive
  required): Harbor source HEAD and interpreter version are MEASURED
  before any dispatch and bound into run-state.json; each job runs in
  its own process group with terminal receipts (ok / exit-N /
  timeout) and owned-group-only cleanup; completed jobs resume without
  rerun, interrupted jobs are clearly refused, dry runs never count as
  attempts, and nonzero/timeout jobs make the run exit 4 (infra).
- `qualify` requires the reviewed plan AND the run-state of the run
  being qualified; trials must live at the real pinned-framework
  nesting `<arm>/<job_name>/<task>__<suffix>/`, echo the planned task
  identity (task_name / trial_name / task path / frozen canonical
  checksum), belong to the bound run (no stale, orphan, dry-run or
  foreign-run receipts), and configs must hash to their reviewed plan
  bytes.
