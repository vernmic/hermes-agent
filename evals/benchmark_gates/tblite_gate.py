#!/usr/bin/env python3
"""TBLite fast-subset gate placeholder. Phase 7.2 / Wave 4 (T4).

The real TBLite fast subset is not implemented. Per the Wave 4 plan this
gate stays explicitly NOT RUN: an unimplemented check can never return a
passing result. Every invocation records NOT_RUN and blocks.

Interface/integration gap (deliberate, unresolved): a real TBLite gate
needs (1) a defined fast subset of the TBLite benchmark tasks, (2) a
model-backed scoring harness with its own cost/budget controls, and (3)
a decision rule wired into the same gate-log/identity contract as
gate1_pytest.py. None of those exist yet. Until they do, this placeholder
must remain fail-closed; the pytest gate above must never be relabelled
or substituted as TBLite evidence.

ASCII only.

Usage:
  tblite_gate.py CANDIDATE_ID [--log PATH]

Exit is always nonzero (block).
"""
import datetime as dt
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent


def default_log():
    return BENCH / "gate-log.jsonl"


def main():
    log = default_log()
    args = sys.argv[1:]
    if "--log" in args:
        i = args.index("--log")
        if i + 1 >= len(args):
            print(json.dumps({"gate": "tblite_fast_subset",
                              "decision": "block", "status": "NOT_RUN",
                              "reason": "--log requires a path"}))
            return 1
        log = Path(args[i + 1])
        del args[i:i + 2]
    record = {
        "gate": "tblite_fast_subset",
        "run_id": "tblite",
        "timestamp_utc": dt.datetime.now(dt.UTC)
        .isoformat(timespec="seconds"),
        "candidate_id": args[0] if args else "(unspecified)",
        "outcome": "not_run",
        "reason": "real TBLite fast subset not implemented; "
                  "unimplemented check cannot pass",
        "decision": "block",
    }
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="ascii", errors="replace") as fh:
        fh.write(json.dumps(record, ensure_ascii=True) + "\n")
    print(json.dumps({"gate": "tblite_fast_subset", "decision": "block",
                      "status": "NOT_RUN",
                      "reason": record["reason"]}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
