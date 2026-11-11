#!/usr/bin/env python3
"""Control probe adapter for the synthetic box mini-product (declared)."""

import json
import sys

sys.path.insert(0, sys.argv[1])
r = {}
try:
    from box import Box

    b = Box()
    b.put("a", 1)
    r["put_get"] = b.get("a") == 1
    b.put("b", 2)
    r["count"] = b.count() == 2
    b.clear()
    r["clear"] = b.count() == 0
except BaseException as exc:  # health: allow BLE001 -- probe records failure data for the comparator to block on
    r["harness_error"] = f"error:{type(exc).__name__}"
print("PROBE_RESULT " + json.dumps(r))
