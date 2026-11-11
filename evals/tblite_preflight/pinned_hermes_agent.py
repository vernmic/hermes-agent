"""Harbor custom agent: Hermes pinned to an exact fork commit.

Minimal override of Harbor's built-in Hermes adapter
(harbor.agents.installed.hermes.Hermes, harbor-framework/harbor
500af91374dcc43b59fac54cf90295646617b14d). Deltas from the built-in
adapter, and nothing else:

- ``install`` pins BOTH the installer script and the install target to
  the exact fork commit via the installer's own supported flags
  (HERMES_REPO_URL + --branch + --commit + --hermes-home). All
  configurable identity arguments are validated against strict patterns
  AND shell-quoted; nothing is smuggled through any flag value.
- ``install`` persists OBSERVED identity (git rev-parse HEAD +
  hermes --version, teed to /logs/agent/observed-identity.txt and
  /tmp) and is idempotent (a checkout already at the pinned HEAD is
  re-verified and reused).
- ``exec_as_agent`` (run-time, before the chat command): a blocking
  RUNTIME IDENTITY GATE on EVERY supported native chat launch (also
  when the command already carries --skills) - observed HEAD, preloaded
  skill FILE hashes (sha256 of the complete SKILL.md file including its
  YAML frontmatter, NOT a normalized body digest) and the model
  endpoint are measured in the child, persisted to
  /logs/agent/runtime-identity.txt, and COMPARED against the expected
  values. A launched --skills selection must match the configured
  preload exactly. Any mismatch or missing observation raises, failing
  the trial as an infrastructure error instead of letting an
  unverified run qualify.
- The chat command gains the pinned core's NATIVE preload flag
  (``hermes chat --skills NAME``).

Configuration (all optional; absent values skip their check):
  preload_skills / TBLITE_HERMES_PRELOAD_SKILLS        comma list
  expected_skill_sha256 / TBLITE_HERMES_SKILL_SHA256   name:sha256 pairs
  expected_base_url / TBLITE_HERMES_EXPECT_BASE_URL    exact child
    GLM_BASE_URL (e.g. the z.ai coding-plan endpoint); the pinned core
    returns an explicit GLM_BASE_URL without probing any endpoint.

Non-Hermes preflight knobs consumed only by this glue.

ASCII only.
"""
import os
import re
import shlex
from typing import Any

from harbor.agents.installed.hermes import Hermes
from typing_extensions import override

# Frozen pins for the TBLite preflight (declared before any run).
DEFAULT_FORK_SLUG = "vernmic/hermes-agent"
DEFAULT_BRANCH = "codex/hermes2-v0.21.7"
DEFAULT_COMMIT = "93faf1387ce4bb4769de506d47a3c82afb9d0326"

# Strict identity patterns: validated AND quoted, never trusted raw.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(/[A-Za-z0-9][A-Za-z0-9_.-]*)?$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_RUN_MARKER = "hermes --yolo chat "
_HERMES_HOME = "/tmp/hermes"
_LOGS_AGENT = "/logs/agent"

# Skills-flag forms accepted by the pinned chat parser (action=append).
_SKILLS_SEPARATE = {"--skills", "-s"}
_PROMPT_VALUE_FLAGS = {"-q", "--query", "--query-file"}


def _skills_flag_value(token: str) -> str | None:
    """Return the skills value carried by a token in one of the parser's
    append forms (--skills V and -s V handled by the caller; here:
    --skills=V, -s=V, attached -sV), or None when the token is not a
    skills flag. A skills-shaped token in a non-value position must be
    treated as ambiguous by the caller, never ignored."""
    if token.startswith("--skills="):
        return token.split("=", 1)[1]
    if token.startswith("-s=") and len(token) > 3:
        return token[3:]
    if token.startswith("-s") and len(token) > 2 and not token.startswith("--"):
        return token[2:]
    return None


def _is_skills_flag_shaped(token: str) -> bool:
    return token in _SKILLS_SEPARATE or _skills_flag_value(token) is not None


def _launched_skill_selection(command: str) -> set[str] | None:
    """Parse the COMPLETE --skills selection from a supported adapter
    chat command; None when no skills flag is present. Ambiguity and
    out-of-contract shapes raise RuntimeError (fail closed)."""
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise RuntimeError(
            f"runtime identity gate: unparseable quoting in chat command ({exc})"
        ) from exc
    chat_index = None
    for i, tok in enumerate(tokens):
        if tok == "chat":
            chat_index = i
            break
        if _is_skills_flag_shaped(tok):
            raise RuntimeError(
                "runtime identity gate: skills flag before the chat subcommand "
                "is outside the supported adapter command shape"
            )
    if chat_index is None:
        return None
    values: list[str] = []
    i = chat_index + 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            break
        if tok in _PROMPT_VALUE_FLAGS:
            if i + 1 >= len(tokens):
                break
            prompt_token = tokens[i + 1]
            if _is_skills_flag_shaped(prompt_token):
                raise RuntimeError(
                    "runtime identity gate: skills-shaped token in a prompt "
                    "value position is ambiguous; refusing to certify"
                )
            i += 2
            continue
        if tok in _SKILLS_SEPARATE:
            if i + 1 >= len(tokens):
                raise RuntimeError(
                    "runtime identity gate: dangling skills flag without a value"
                )
            values.append(tokens[i + 1])
            i += 2
            continue
        inline = _skills_flag_value(tok)
        if inline is not None:
            values.append(inline)
        i += 1
    if not values:
        return None
    return {s for value in values for s in value.split(",") if s}


def _validated(value: str, pattern: re.Pattern, what: str) -> str:
    if not pattern.match(value or ""):
        raise ValueError(f"invalid {what}: {value!r}")
    return value


def _parse_skill_shas(raw: str) -> dict[str, str]:
    """Parse 'name:sha256,...' into a validated mapping."""
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        name, sep, sha = pair.partition(":")
        if not sep:
            raise ValueError(f"invalid skill sha pair (want name:sha256): {pair!r}")
        out[_validated(name.strip(), _SKILL_NAME_RE, "skill name")] = _validated(
            sha.strip(), _SHA256_RE, "skill sha256"
        )
    return out


class PinnedHermes(Hermes):
    """Hermes agent installed from an exact fork commit, gated on
    measured runtime identity."""

    def __init__(
        self,
        *args: Any,
        fork_slug: str | None = None,
        branch: str | None = None,
        commit: str | None = None,
        preload_skills: list[str] | None = None,
        expected_skill_sha256: str | None = None,
        expected_base_url: str | None = None,
        **kwargs: Any,
    ):
        self._fork_slug = _validated(
            fork_slug or os.environ.get("TBLITE_HERMES_FORK", DEFAULT_FORK_SLUG), _SLUG_RE, "fork slug"
        )
        self._branch = _validated(
            branch or os.environ.get("TBLITE_HERMES_BRANCH", DEFAULT_BRANCH), _BRANCH_RE, "branch"
        )
        self._commit = _validated(
            commit or os.environ.get("TBLITE_HERMES_COMMIT", DEFAULT_COMMIT), _COMMIT_RE, "commit"
        )
        if preload_skills is not None:
            raw_preload = ",".join(preload_skills)
        else:
            raw_preload = os.environ.get("TBLITE_HERMES_PRELOAD_SKILLS", "")
        self._preload_skills = [
            _validated(s.strip(), _SKILL_NAME_RE, "skill name")
            for s in raw_preload.split(",") if s.strip()
        ]
        self._expected_skill_shas = _parse_skill_shas(
            expected_skill_sha256
            if expected_skill_sha256 is not None
            else os.environ.get("TBLITE_HERMES_SKILL_SHA256", "")
        )
        self._expected_base_url = (
            expected_base_url or os.environ.get("TBLITE_HERMES_EXPECT_BASE_URL", "") or None
        )
        super().__init__(*args, **kwargs)

    @property
    def _installer_url(self) -> str:
        return (
            f"https://raw.githubusercontent.com/{self._fork_slug}/"
            f"{self._commit}/scripts/install.sh"
        )

    @property
    def _repo_url(self) -> str:
        return f"https://github.com/{self._fork_slug}.git"

    def _observed_identity_command(self) -> str:
        return (
            'export PATH="$HOME/.local/bin:$PATH" && '
            f'export HERMES_HOME="{_HERMES_HOME}" && '
            f'git -C "{_HERMES_HOME}/hermes-agent" rev-parse HEAD && '
            "hermes --version"
        )

    async def install(self, environment) -> None:  # type: ignore[override]
        await self.ensure_system_dependencies(
            environment, ("curl", "git", "ripgrep", "xz")
        )
        # Idempotent bootstrap: a checkout already at the pinned HEAD is
        # re-verified (observed, not requested) and reused. A missing or
        # older install makes the probe exec fail (RuntimeError from the
        # base adapter on nonzero exit); anything else - timeout, transport
        # failure - propagates and fails setup loudly.
        try:
            result = await self.exec_as_agent(
                environment, command=self._observed_identity_command(), timeout_sec=120
            )
            observed = (result.stdout or "").strip().splitlines()
            if any(line.strip() == self._commit for line in observed):
                return
        except RuntimeError:
            pass
        command = (
            "set -euo pipefail; "
            f"curl -fsSL {shlex.quote(self._installer_url)} -o /tmp/hermes-install.sh && "
            f"HERMES_REPO_URL={shlex.quote(self._repo_url)} "
            f"HERMES_HOME={shlex.quote(_HERMES_HOME)} "
            "bash /tmp/hermes-install.sh "
            f"--skip-setup --hermes-home {shlex.quote(_HERMES_HOME)} "
            f"--branch {shlex.quote(self._branch)} "
            f"--commit {shlex.quote(self._commit)} && "
            f"mkdir -p {_LOGS_AGENT} && "
            f"({self._observed_identity_command()}) | tee /tmp/hermes-observed-identity.txt | tee {_LOGS_AGENT}/observed-identity.txt && "
            f'mkdir -p "{_HERMES_HOME}" "{_HERMES_HOME}/sessions" "{_HERMES_HOME}/skills" "{_HERMES_HOME}/memories"'
        )
        await self.exec_as_agent(environment, command=command)

    @override
    def get_version_command(self) -> str | None:
        # The pinned core (93faf1387...) exposes --version, not a
        # `version` subcommand; the built-in adapter's `hermes version`
        # assumes a newer CLI.
        return 'export PATH="$HOME/.local/bin:$PATH"; hermes --version'

    def _runtime_gate_command(self) -> str:
        parts = [
            f'sha256sum "{_HERMES_HOME}/skills/{shlex.quote(name)}/SKILL.md"'
            for name in self._preload_skills
        ]
        parts.append('printf "GLM_BASE_URL=%s\\n" "${GLM_BASE_URL:-}"')
        # Query the ACTUAL checkout at launch time. Never the cached
        # install receipt: a checkout whose HEAD moved after install
        # must still be caught here. A missing repo or failing git
        # makes the whole gate command exit nonzero, which blocks the
        # launch; there is no fallback to any cached value.
        parts.append(
            f'git -C "{_HERMES_HOME}/hermes-agent" rev-parse HEAD'
        )
        return (
            f"mkdir -p {_LOGS_AGENT} && set -o pipefail && ( "
            + "; ".join(parts)
            + f" ) | tee {_LOGS_AGENT}/runtime-identity.txt"
        )

    def _enforce_runtime_identity(self, stdout: str) -> None:
        """Block the trial when measured identity misses expectations."""
        observed = (stdout or "").strip().splitlines()
        heads = [ln.strip() for ln in observed if _COMMIT_RE.match(ln.strip())]
        if not heads:
            raise RuntimeError(
                "runtime identity gate: observed git HEAD missing from measurement"
            )
        if heads[0] != self._commit:
            raise RuntimeError(
                f"runtime identity gate: observed HEAD {heads[0]} != pinned {self._commit}"
            )
        for name, expected_sha in self._expected_skill_shas.items():
            hit = None
            for ln in observed:
                m = re.match(r"^([0-9a-f]{64})\s+\S*$", ln.strip())
                if m and f"/skills/{name}/SKILL.md" in ln:
                    hit = m.group(1)
                    break
            if hit is None:
                raise RuntimeError(
                    f"runtime identity gate: skill body hash missing for {name}"
                )
            if hit != expected_sha:
                raise RuntimeError(
                    f"runtime identity gate: skill {name} sha {hit} != expected {expected_sha}"
                )
        if self._expected_base_url:
            urls = [ln.strip() for ln in observed if ln.strip().startswith("GLM_BASE_URL=")]
            if not urls or urls[0] != f"GLM_BASE_URL={self._expected_base_url}":
                raise RuntimeError(
                    "runtime identity gate: child GLM_BASE_URL missing or != expected "
                    f"endpoint {self._expected_base_url} (measured: {urls[0] if urls else 'none'})"
                )

    @override
    async def exec_as_agent(self, environment, command: str, *args: Any, **kwargs: Any):
        # Runtime identity gate + native skill preload on EVERY supported
        # native chat launch. The gate runs even when the command already
        # carries --skills: an already-preloaded chat must not skip the
        # observed-HEAD / skill-file-hash / endpoint checks, and its
        # launched skill selection must match the configured preload
        # exactly (fail closed rather than certify one file while
        # launching another). The preload flag itself is inserted only
        # when absent, never duplicated.
        if command and _RUN_MARKER in command:
            gate = await super().exec_as_agent(
                environment, command=self._runtime_gate_command(),
                env=kwargs.get("env"), timeout_sec=60,
            )
            self._enforce_runtime_identity(getattr(gate, "stdout", "") or "")
            launched = self._enforce_preload_selection(command)
            if self._preload_skills and launched is None:
                preload_flag = "--skills " + shlex.quote(",".join(self._preload_skills))
                command = command.replace(
                    _RUN_MARKER, f"{_RUN_MARKER}{preload_flag} ", 1
                )
        return await super().exec_as_agent(environment, command, *args, **kwargs)

    def _enforce_preload_selection(self, command: str) -> set[str] | None:
        """Return the COMPLETE launched --skills selection after
        verifying it against the configured preload; None when the
        command carries no skills flag in any form.

        The pinned CLI parser (hermes_cli/_parser.py: chat inherits
        '-s/--skills' with action='append') LOADS EVERY occurrence -
        separate token, '--skills=V', '-s V', '-s=V' and attached '-sV'
        all accumulate, comma lists split. Parsing therefore collects
        the COMPLETE selection from every flag form after the 'chat'
        subcommand. Prompt values of -q/--query/--query-file are skipped
        (a skills-shaped token in a prompt position is treated as
        ambiguity and blocks). Skills flags before the 'chat' subcommand
        are outside the adapter's supported shape and block. The same
        configured skill repeated across flags normalizes into one
        entry; any EXTRA skill blocks - one file is never certified
        while another loads.
        """
        launched = _launched_skill_selection(command)
        if not self._preload_skills:
            if launched:
                raise RuntimeError(
                    "runtime identity gate: chat launched with --skills while no preload is configured"
                )
            return None
        if launched is not None and launched != set(self._preload_skills):
            raise RuntimeError(
                "runtime identity gate: launched --skills "
                f"{sorted(launched)} != configured preload {sorted(self._preload_skills)}"
            )
        return launched
