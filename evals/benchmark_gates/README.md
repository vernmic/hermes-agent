# Benchmark gates (Phase 7.2 / T4)

Standalone benchmark-gate tooling staged from the accepted W4/R03
bounded pytest gate (originally installed under a private Hermes home).
Generic and reusable: no machine-specific paths; every path is an
explicit CLI option or a default relative to this directory.

## Layout

- gate1_pytest.py   Gate 1 CLI. `run` BUILDS the pytest command itself
                    from the declared interpreter (`--python`, default
                    the interpreter running the gate); no arbitrary
                    command argv is accepted, so the interpreter can
                    never be misreported. Appends a structured record to
                    an append-only gate log (`--log`, default
                    `gate-log.jsonl` here), preserves full logs in
                    `--logs-dir` (default `logs/` here), and exits 0
                    only for a qualifying promote. `check` exits 0 only
                    for a production promote: the latest non-control
                    qualifying record for the candidate whose
                    candidate_sha256 and test_contract_sha256 match the
                    current tree. `reject` records a blocked
                    non-qualifying command with its true interpreter.
- tblite_gate.py    TBLite fast-subset placeholder. NOT RUN per plan:
                    the real fast subset is not implemented (no defined
                    task subset, no scoring harness, no cost controls,
                    no decision-rule integration), so the gate always
                    records not_run/block and can never return a
                    passing result. The pytest gate must never be
                    relabelled or substituted as TBLite evidence.

## Decision rule

A record qualifies only when: the gate constructed the command from the
declared interpreter and `-m pytest`; the run did not time out; exit
code is 0; parsed counts show at least one passed test and zero
failures or errors; and the staged import-origin check proved the tests
imported the candidate from the staged tree (no external reference or
upstream shadow). Non-qualifying outcomes: no_op, help, collect_only,
not_pytest, interpreter_mismatch, empty_suite (nothing collected,
exit 5), all_skipped (executed == 0 with skips), usage_error (exit 4),
command_error, timeout, test_failure, import_shadow, origin_unproven.
Timeouts are never conflated with test results.

## Identity binding

Every run record carries candidate_sha256 (content hash of the
candidate file/tree) and test_contract_sha256 (tests tree hash + pytest
args + tests path). Production check recomputes both and blocks with
identity_mismatch on any change, so a reused candidate id, a mutated
candidate, or changed tests cannot authorize advancement. Control
records (`run --control`) are excluded from production check; they
remain inspectable via `check --inspect-control` for validation tooling.

## Configuration

Operator configuration is explicit CLI only (`--python`, `--log`,
`--logs-dir`); behaviour is never read from environment variables. The
GATE1_* names inside gate1_pytest.py are internal parent/child IPC for
one staged run, not operator configuration. The `--python` option
retains the pinned-interpreter capability (an operator or scheduler
pins one interpreter for every gate run).

## Scope limits (honest)

- This gate certifies bounded pytest execution with staged
  import-origin evidence. It is NOT a semantic-quality certification of
  the candidate, and promoting a candidate must not be read as one.
- Staging supports top-level Python modules and packages
  (implementation first, tests second, staged tree as import root) --
  the layout independently verified in review W4/R03. Fixture/src/CLI
  project layouts are not qualified; they must fail honestly and be
  qualified before any broader claim.
- TBLite is NOT RUN; see tblite_gate.py for the interface/integration
  gap that a real implementation must close.

## Tests

Regression tests: `tests/evals/test_benchmark_gates.py` (run with
`scripts/run_tests.sh tests/evals/test_benchmark_gates.py`). They drive
the real gate CLI against disposable candidate/oracle trees under a
temp dir with the gate log redirected there; nothing writes a live or
in-repo gate log, and the installed runtime is never touched.
