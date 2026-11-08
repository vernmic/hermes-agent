# Opt-in session-end logging hook

Install session-end.sh and session_end_log.py together in the operator's agent-hooks directory. Configure hooks.on_session_end with command pointing to session-end.sh and timeout: 5. No hook is enabled by adding these example files to a checkout.

This observer writes bounded field data to execution-log.jsonl under HERMES_WORKSPACE_DIR, or the routed HERMES_HOME/workspace. It always returns the no-op JSON object, including malformed-input and unavailable-log errors. A busy advisory lock or FIFO is skipped without waiting. The shell hook runner's process-group timeout remains the outer deadline for slow filesystems/process startup; the logger alone cannot cancel an OS-level I/O stall.

There are no jq/awk/grep calls, full-history scans, network calls, model calls or nested Hermes sessions. The old disabled HERMES_HOOK_DISTILL experiment stays disabled even if that legacy variable is set: per-turn hooks are not the GEPA learning scheduler. Use the existing reviewed GEPA workflow. Optional reported cost above 2 is recorded in expensive-sessions.txt, without interpreting it as a provider invoice or enforcing a model budget. The existing expense-text format is preserved.

This hardens a logging observer; it does not prove that a hook caused an event-loop crash. The gateway executor liveness repair is the separately merged upstream #142061 backport. Thread reuse must include per-turn state cleanup.
