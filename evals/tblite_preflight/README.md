# TBLite harness preflight (evals/tblite_preflight) - W02

Bounded preflight glue for running the OpenThoughts-TBLite benchmark
through the existing Harbor framework with a fork-pinned Hermes agent.
Mechanism preflight ONLY: not the 20-task regression gate, not
calibration, not GEPA, not a TBLite PASS claim.

## freeze_fast_subset.py (v2, fail-closed)

- Qualified mode (default): the dataset must be a CLEAN git checkout
  whose OBSERVED HEAD equals the expected pin (5c37b41f...); the corpus
  must contain exactly 100 tasks; every raw category label must be
  mapped or explicitly declared FROZEN_UNMAPPED (with reason); and the
  deterministic selection must fill every category allocation with zero
  shortfall. Any violation exits nonzero before writing artifacts.
- Every task is bound by a complete task-tree sha256 (all files,
  excluding only documented caches), so mutating ANY task asset
  (including environment inputs consumed by Docker COPY) changes the
  manifest.
- --expect-selection enforces selection continuity: newly selected ids
  must equal the prior frozen selection; selected tasks are never
  replaced after outcomes.
- --generic accepts fixtures and writes an explicitly UNQUALIFIED
  manifest (qualified=false) and refuses to write any selection.
- The coarse 9-bucket mapping is PROJECT-PROPOSED, not an official
  taxonomy; ambiguous raw labels carry documented reasons
  (MAPPING_REASONS) and all eleven "general" tasks carry explicit
  per-task overrides.

## pinned_hermes_agent.py (v2)

Harbor custom agent (-a evals.tblite_preflight.pinned_hermes_agent:PinnedHermes)
subclassing Harbor's built-in Hermes adapter. Deltas:

- install(): installer script AND install target pinned to the exact
  fork commit via the installer's own flags; HERMES_HOME=/tmp/hermes for
  installer and runtime (the pinned core commits its dependency
  environment there); observed identity (git rev-parse HEAD +
  hermes --version) recorded at the end; idempotent - a checkout
  already at the pinned HEAD is re-verified and reused (verified
  preinstalled pin, usable from a prepared image).
- All identity arguments (fork slug, branch, commit, skill names) are
  validated against strict patterns AND shell-quoted; nothing is
  smuggled through any flag value.
- get_version_command(): the pinned core's actual --version flag.
- exec_as_agent(): blocking runtime identity gate on every supported
  native chat launch (including commands that already carry --skills):
  observed git HEAD, the sha256 of each preloaded skill's COMPLETE
  SKILL.md file (frontmatter included - not a normalized body digest;
  the W02 preload-block reproduction of the verbatim body remains
  independent evidence), and the child model endpoint are measured,
  persisted and enforced; a launched --skills selection must match the
  configured preload exactly. The native `chat --skills` preload flag
  is inserted only when absent, never duplicated.

TBLITE_HERMES_FORK / TBLITE_HERMES_BRANCH / TBLITE_HERMES_COMMIT /
TBLITE_HERMES_PRELOAD_SKILLS / TBLITE_HERMES_SKILL_SHA256 /
TBLITE_HERMES_EXPECT_BASE_URL are non-Hermes preflight knobs consumed by
this glue only, not agent behavior variables.

## Scope and limits

- Skill injection uses Harbor's --skill with an UNCHANGED public
  bundled skill from the measured fork commit; it is a preflight-only
  control skill, not the final GEPA target or learning acceptance.
- Local bounded execution only: concurrency 1, bounded CPU/memory,
  existing trusted Docker daemon, unique compose project names,
  cleanup of own project IDs only. Published cloud concurrency (32
  Daytona sandboxes) is not a forecast for local capacity.
- `harbor run` exposes no prebuilt task-env image override (that path
  is hosted-only), so every fresh trial re-runs the pinned install
  (~13.5 min measured at 2 CPUs). The glue's install-skip makes a
  prepared image usable, but no compatibility claim is made until
  tested end-to-end.
- No new core tool, no default-on behavior, no new HERMES_* env vars
  (the TBLITE_* names above are non-Hermes preflight knobs consumed by
  this glue, not agent behavior variables).

## Tests

Offline controls: tests/evals/test_tblite_preflight_freeze.py (no
Docker, no network, no model calls; includes the public-entry
wrong-commit / unversioned / dirty / asset-drift controls and the
generic-mode and continuity contracts).
