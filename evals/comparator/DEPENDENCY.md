# Dependency: evals/benchmark_gates gate (T4, published)

The comparator's oracle phase depends on the reviewed T4 benchmark gate:

- Source: `evals/benchmark_gates/gate1_pytest.py` (+ `tblite_gate.py`
  placeholder, `tests/evals/test_benchmark_gates.py` regression controls)
- Fork: vernmic/hermes-agent, public branch evals/benchmark-gates
- Commit: 00231bee0815d881b3a2de5de0eaacb24d113c34
- Parent base lineage: public origin/main

Why: the R13 first port shipped a weaker `gate_oracle.py` that executed
external suites in place and guessed import origins statically. The
independent paired control (r13-independent/oracle-origin-control.json)
showed it PROMOTED a broken candidate whose tests actually imported the
reference module, and recorded the unexecuted broken module as good
provenance. The published gate blocks the identical fixture
(test_failure, import_origin_ok=false). Reusing the reviewed dependency
instead of duplicating it is deliberate: an output-format-compatible
wrapper is not behaviorally equivalent.

Consumption: set `gate_script` in the comparison config to
`evals/benchmark_gates/gate1_pytest.py` and `pinned_python` to the
explicit test interpreter. The composer drives it via its documented CLI
(`run --candidate-id --candidate --tests --python --pytest-args --timeout
--label --log --logs-dir`) and consumes the JUnit XML plus the JSONL
record's `import_origins` under the echoed `run_id`.

Validation base for this package: public origin/main
3d4dd108cf52e26cf0d7fb218461e53623a8ec2d plus this dependency commit,
checked out in an isolated scratch checkout. The comparator branch should
stack on (or cherry-pick) the dependency commit before merging.
