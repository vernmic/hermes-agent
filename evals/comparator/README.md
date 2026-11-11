# evals/comparator - reusable product-comparison harness

Compares a reference ("original") product and a candidate product against a
frozen requirements contract, using executed probes, oracle tests, source
anchors and optional Graphify-shaped graph evidence. Everything is
qualification-first: harness failures, missing evidence, stale identities
or malformed graphs BLOCK a numeric grade (exit 2); genuine measured
product failures stay in the score as partials.

## Dependency: benchmark gate (REQUIRED)

The oracle phase REUSES the published T4 gate and does not ship its own:
`evals/benchmark_gates/gate1_pytest.py` at fork commit
00231bee0815d881b3a2de5de0eaacb24d113c34 (branch evals/benchmark-gates).
The gate stages the candidate, copies only trusted test files, rejects
external-reference and inherited-shadow import origins, distinguishes
pass / test_failure / empty_suite / all_skipped / timeout / usage_error /
no-op / collect-only / help / interpreter-mismatch, and binds candidate
and test-contract SHA256 identities. Config `gate_script` points at it;
`pinned_python` names the test interpreter. See DEPENDENCY.md. A weaker
replacement gate is a regression, not a substitute.

## Layout

- `compare_products.py` - CLI composer (grader-comparison/6):
  `python compare_products.py --config cfg.json --out dir
  [--judge-response resp.json]`. Structural 0.4 / tests 0.4 / judge 0.2.
- `graph_compare.py` - Graphify-output mapper (grader-graph/4): qualified
  node identity (token boundaries, `Class.member`, aliases, re-exports)
  resolved against real AST definitions with line-span agreement; absent or
  mismatched spans demote to cited context, never verified node evidence.
- `check_capabilities.py` - requirements/probe/anchor mapper with an
  explicit public-names anchor kind.
- `fixtures/box-fixture/` - fully portable end-to-end example: a tiny Box
  product, oracle tests, probe script and requirements JSON.
- `fixtures/cached-property-adapter/` - OPTIONAL pilot adapter
  (requirements + probes) for a cached-property reconstruction study; it is
  an example of writing a project adapter, not a built-in schema.

## Contracts

- Config: JSON with explicit paths and interpreters
  (`gate_script`, `pinned_python`, product dirs, oracle tests, probe
  script, requirements, both graphs, `expected_probe_keys`,
  `expected_identities`). No environment magic; no user-specific defaults.
- Probes print one `PROBE_RESULT {json}` line with exactly the declared
  boolean keys and exit 0.
- Oracle tests run through the benchmark gate against the STAGED
  candidate under the explicit pinned interpreter; sibling reference
  modules are never copied; per-test identities come from JUnit XML,
  normalized layout-independently; import origins must resolve inside the
  staged candidate.
- Graphs follow the Graphify node/link shape; typed envelope, unique node
  ids, resolved endpoints and a present-list links field are all enforced.

Alternative valid architectures (function factories, private descriptors,
facade classes) anchor identically - no class layout is ever imposed, and
graph counts are context, never quality.

## Execution import roots (v5)

`original_dir`/`candidate_dir` remain FULL project roots: frozen
identities, graph/source mapping and judge material always cover the
whole project. Real projects differ in layout (a src-layout reference
vs a flat candidate, or the reverse); the optional config keys

- `original_import_root` (project-relative, default `"."`)
- `candidate_import_root` (project-relative, default `"."`)

explicitly select each product's Python import root for EXECUTION: the
benchmark gate's `--candidate` and the probe script argument both
receive that selected source directory, never a silently guessed root.
Roots are validated fail-closed before anything executes: no absolute,
tilde or `..` traversal paths; the directory must exist inside the
DECLARED product; symlinks may not escape it. There is no auto-install
and no heuristic global fallback - a missing root is a configuration
error, not a fallback trigger.

The receipt records `execution_projection` per product (import_root,
project_root, execution_dir), and the versioned bundle material
(revised bundle format, now grader-comparison/6) includes the
import-root policy, the oracle-manifest policy with every allowlisted
resource identity, the probe interpreters, and each
execution directory's content identity inside `input_bundle_sha256`:
a judge response minted for one execution root can never certify
another. Judge hashes from pre-v5 bundles are stale and must not be
reused as fresh proofs.

## Trusted oracle manifests (v5b)

Some upstream suites need trusted sibling resources the gate's
name-filtered staging drops (package `tests/__init__.py` mixins, helper
modules, out-of-tree data files). The optional frozen config key
`oracle_manifest` points at an `oracle-staging/1` manifest (see
evals/benchmark_gates/gate1_pytest.py): trusted oracle PROJECT root,
selected tests path, a concrete project-relative FILE allowlist with
REQUIRED per-file sha256, and declared target import module names. In
manifest mode `oracle_tests` must BE the manifest's oracle project
tests directory (the gate enforces the equality); only allowlisted
hash-verified files are staged under `_gate_oracle/` with
oracle-project-relative paths preserved (`tests/../x` resolves
faithfully), and declared targets must be observed at runtime from
staged CANDIDATE code (a copied reference target blocks as
target_from_oracle; a never-imported target blocks as target_missing).
The manifest file joins the frozen identity set (pre/post) and the
manifest policy plus every resource identity is hashed into
`input_bundle_sha256`: a changed helper, data file or staging policy
cannot reuse a prior judge authorization. Probes run under the
configured `pinned_python` (the same explicit interpreter the gate
uses), recorded per product and bound into the bundle -- probe and
test runtimes are coherent by construction.
