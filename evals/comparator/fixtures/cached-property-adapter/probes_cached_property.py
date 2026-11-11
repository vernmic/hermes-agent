#!/usr/bin/env python3
"""Pilot probe adapter: cached-property frozen requirements (declared).

This adapter is EXPLICITLY pilot-specific to the cached-property frozen
requirements (W2/R05). It is NOT a generic prober for arbitrary
repositories; other products declare their own adapters in their
comparison config.

Strengthened per R07 review:
  - the None case counts computations, not just values;
  - TTL decoration forms are exercised on BOTH plain and threaded TTL
    decorators;
  - thread races use a start barrier so threads genuinely contend, and
    capture per-thread exceptions and results.

Contract: prints one line 'PROBE_RESULT <json>' mapping keys to
true/false/'error:<Type>'/null. Keys match the comparison config's
expected_probe_keys exactly.

Usage: probes_cached_property.py <product_dir>
ASCII only.
"""

import json
import sys
import threading
import time
from pathlib import Path


class Barrier:
    def __init__(self, n):
        self.n = n
        self.count = 0
        self.mutex = threading.Lock()
        self.turn = threading.Event()

    def wait(self):
        with self.mutex:
            self.count += 1
            if self.count == self.n:
                self.turn.set()
                return
        self.turn.wait()


def race(fn, threads_n):
    """Run fn in threads_n threads behind a start barrier.

    Returns (single_compute, exceptions, results) where single_compute means
    every thread observed the same value and fn executed exactly once."""
    errors = []
    results = []
    calls = {"n": 0}
    calls_lock = threading.Lock()
    barrier = Barrier(threads_n)

    def target():
        try:
            barrier.wait()
            results.append(fn())
        except BaseException as exc:  # health: allow BLE001 -- captured, never silently dropped; probe error outcomes are measured data
            errors.append(f"{type(exc).__name__}: {exc}")

    def counted():
        with calls_lock:
            calls["n"] += 1
        return fn()

    # The product decorator counts via the wrapped function; we wrap the
    # underlying computation by calling the property, which invokes it.
    threads = [threading.Thread(target=target) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return calls, errors, results


def main():
    product = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(product))
    r = {}
    try:
        import cached_property as cp

        names = [
            "cached_property",
            "threaded_cached_property",
            "cached_property_with_ttl",
            "threaded_cached_property_with_ttl",
        ]
        r["import_surface"] = all(hasattr(cp, n) for n in names)

        # R1: memoization, per-instance, None counts computations
        class A:
            calls = 0

            @cp.cached_property
            def v(self):
                type(self).calls += 1
                return type(self).calls

        a = A()
        r["r1_memoizes"] = a.v == 1 and a.v == 1 and A.calls == 1
        b = A()
        r["r1_per_instance"] = b.v == 2 and b.v == 2 and A.calls == 2

        class N:
            calls = 0

            @cp.cached_property
            def v(self):
                type(self).calls += 1

        n = N()
        _ = n.v
        _ = n.v
        r["r1_none_computes_once"] = n.v is None and N.calls == 1

        # R2 class access returns the decorator/descriptor object, not a value
        r["r2_class_access"] = hasattr(A.v, "__get__") and not isinstance(A.v, type(a))

        # R3 delete resets
        c = A()
        before = A.calls
        c.v
        del c.v
        r["r3_delete_resets"] = c.v == before + 2 and A.calls == before + 2

        # R4 assignment overrides without computing
        d = A()
        before = A.calls
        d.v = 99
        r["r4_assignment"] = d.v == 99 and A.calls == before

        # R5 coroutine shared await
        import asyncio

        class P:
            calls = 0

            @cp.cached_property
            async def v(self):
                type(self).calls += 1
                await asyncio.sleep(0)
                return type(self).calls

        async def drive():
            p = P()
            x = await p.v
            y = await p.v
            return x, y, P.calls

        x, y, pcalls = asyncio.run(drive())
        r["r5_coroutine_shared"] = x == 1 and y == 1 and pcalls == 1

        # R6: barrier race on threaded decorator
        class M:
            def __init__(self):
                self.calls = 0
                self.lock = threading.Lock()

            @cp.threaded_cached_property
            def v(self):
                time.sleep(0.05)
                with self.lock:
                    self.calls += 1
                return self.calls

        raced, errors, results = _race_prop(M, 5)
        r["r6_single_compute"] = (
            raced.calls == 1 and not errors and all(x == 1 for x in results)
        )

        # R7 expiry recomputes
        class E:
            calls = 0

            @cp.cached_property_with_ttl(ttl=0.05)
            def v(self):
                type(self).calls += 1
                return type(self).calls

        e = E()
        f1 = e.v
        time.sleep(0.15)
        f2 = e.v
        r["r7_expiry_recomputes"] = f1 == 1 and f2 == 2

        # R8: barrier race after expiry on threaded TTL
        class W:
            def __init__(self):
                self.calls = 0
                self.lock = threading.Lock()

            @cp.threaded_cached_property_with_ttl(ttl=0.05)
            def v(self):
                with self.lock:
                    self.calls += 1
                return self.calls

        w = W()
        w.v  # first compute: calls == 1
        time.sleep(0.15)  # expire
        _, errors2, results2 = _race_prop(w, 5)
        r["r8_race_after_expiry"] = (
            w.calls == 2 and not errors2 and all(x == 2 for x in results2)
        )

        # R9: decoration forms on BOTH TTL decorators
        def make(deco, kind):
            src = (
                "class T:\n"
                "    def __init__(self): self.calls = 0\n"
                f"    @cp.{deco}{kind}\n"
                "    def v(self):\n"
                "        self.calls += 1\n"
                "        return self.calls\n"
            )
            ns = {"cp": cp}
            exec(compile(src, "<probe>", "exec"), ns)
            return ns["T"]()

        forms = {}
        for deco_label, deco in (
            ("plain", "cached_property_with_ttl"),
            ("threaded", "threaded_cached_property_with_ttl"),
        ):
            for label, kind in (
                ("bare", ""),
                ("empty", "()"),
                ("positional", "(5)"),
                ("kw", "(ttl=5)"),
            ):
                key = f"r9_{deco_label}_{label}"
                try:
                    t = make(deco, kind)
                    v1, v2 = t.v, t.v
                    forms[key] = v1 == 1 and v2 == 1 and t.calls == 1
                except Exception as exc:  # health: allow BLE001 -- probe counts error outcomes as measured data
                    forms[key] = f"error:{type(exc).__name__}"
        r.update(forms)
    except Exception as exc:  # health: allow BLE001 -- probe records error data
        r["harness_error"] = f"error:{type(exc).__name__}"
    print("PROBE_RESULT " + json.dumps(r))


def _race_prop(cls_or_obj, threads_n):
    """Race an object's .v property behind a barrier.

    Accepts a class (instantiates fresh) or an existing instance (races it,
    e.g. after expiry). Returns (obj, errors, results)."""
    obj = cls_or_obj() if isinstance(cls_or_obj, type) else cls_or_obj
    errors = []
    results = []
    barrier = Barrier(threads_n)

    def target():
        try:
            barrier.wait()
            results.append(obj.v)
        except BaseException as exc:  # health: allow BLE001 -- probe counts error outcomes as measured data
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=target) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return obj, errors, results


if __name__ == "__main__":
    main()
