"""Plugin hook / middleware / event-bus / system-prompt-section dispatch.

Mixed into :class:`hermes_cli.plugins.PluginManager`. ``_resolve_hook_callback_timeout`` stays on
the origin (tests patch it there) and is looked up lazily.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import inspect
import logging
import queue
import re
import threading
import time
import types
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Union

from hermes_cli.middleware import OBSERVER_SCHEMA_VERSION

logger = logging.getLogger("hermes_cli.plugins")

# Allowlist of agent-turn hot-path hooks bounded by plugins.hook_callback_timeout (fail-open:
# abandon without join — joining reintroduced a shutdown hang). Unlisted hooks run synchronously.
# Intentionally unbounded: on_session_finalize/reset (last-chance flush — abandon can lose state);
# subagent_start (observer); pre_gateway_dispatch (policy gate — neither fail mode is acceptable);
# pre/post_approval_* (approval UX has its own timeout); kanban_* (own heartbeat/stale reclaim).
# The goal is to stop a hung Python plugin callback from wedging the conversation loop (#76821) without
# joining the worker (avoids the #6622 ThreadPoolExecutor shutdown hang). Hooks not listed below run
# synchronously to completion. (on_session_start/end stay bounded — they sit on the common session-boundary
# path.) - subagent_start — observer only; blocking delegation belongs in pre_tool_call. Lower frequency
# than tool/LLM hooks. Abandoning is unsafe either way (fail-open skips auth-like checks; fail-closed can
# drop legitimate messages). Prefer finish-or-exception fallthrough. - pre_approval_request /
# post_approval_response — observers only (cannot veto); the approval UX already has its own timeout; not on
# the tool loop hot path. - kanban_task_* — fire after the board DB commit, observers only, in
# dispatcher/worker processes; kanban has its own heartbeat/stale reclaim. Abandon-without-join also leaves
# a daemon thread that may still mutate shared state — safer for value-returning observers than for
# gates/flushes.
_HOOK_TIMEOUT_BOUNDED_HOOKS: set[str] = {
    "post_tool_call", "transform_terminal_output", "transform_tool_result", "transform_llm_output",
    "pre_llm_call", "post_llm_call", "pre_memory_recall", "pre_api_request", "post_api_request", "api_request_error",
    "pre_auxiliary_call", "post_auxiliary_call", "pre_verify", "on_session_start", "on_session_end",
    # Fail-open consumer on every inbound gateway message: a hung plugin must not stall the profile.
    "post_gateway_admission",
}

# Policy hooks: timeout / still-running must fail closed (block the tool).
_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS: set[str] = {"pre_tool_call", "pre_skill_write"}
