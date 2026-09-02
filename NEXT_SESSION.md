# Next Session: Hermes Upgrade

Handoff notes from the 2026-09-01/09-02 OpenClaw/pi-mono/hindsight upgrade session,
written for whoever (which agent/session) picks up the equivalent Hermes upgrade next.
The goal is the same playbook, adapted to what's actually different about this repo.

---

## State as of 2026-09-02 (already checked, don't re-derive)

- **History is intact.** `main` IS an ancestor of `upstream/main` (`git merge-base
  --is-ancestor main upstream/main` returns 0). This is the *good* case — like Hindsight
  was for the OpenClaw-family upgrade, not the broken-history case OpenClaw/pi-mono were in.
  A normal rebase/fast-forward works here; you do **not** need the fresh-branch
  tag-reconstruction approach.
- **Zero local commits ahead of upstream.** `git rev-list --left-right --count
  main...upstream/main` returns `0  19124` — this fork carries no local patches at all,
  it's just very stale (19,124 commits behind). That simplifies things a lot: there's
  nothing to re-derive or decide whether to drop, just a straight update.
- **Latest upstream tag: `v2026.8.31`** (as of the fetch on 2026-09-02; check again,
  don't trust this is still current). Local release notes only go up to `RELEASE_v0.13.0.md`
  — that's a *different* versioning scheme (semantic pre-2026 releases) than the current
  upstream tags (`v2026.8.31`-style dated tags), so don't try to map one to the other
  directly; just check what the actual latest tag is when you start.
- **Runtime: Python primary, Node present too.** `./hermes` (the CLI entry point at repo
  root) is a Python launcher (`from hermes_cli.main import main`). `package.json` also
  exists with `"engines": { "node": ">=20.0.0" }` — there's a Node component somewhere
  (web UI or gateway service, similar to OpenClaw's split). Check what actually needs
  Node vs Python before assuming either build tool alone covers the whole upgrade.
- **WSL2 is a hard requirement, confirmed** — `I:\hermes\plan\install-plan.md` states
  outright: "Hermes does not run natively on Windows." All Hermes work runs inside WSL2
  Ubuntu (`I:\hermes\CLAUDE.md` confirms this too: workspace root is `I:\hermes\` on the
  Windows side, `/mnt/i/hermes/` from WSL2, Hermes home is `~/.hermes/` inside WSL2). Any
  upgrade work — install, build, `hermes doctor`, `hermes update` — needs to run from a WSL2
  shell, not PowerShell/Git Bash on the Windows side, even though this fork's files live on
  the `I:` drive and are editable from Windows tooling directly.
- **`I:\hermes\` (separate directory, sibling to this fork) already has its own `hermes
  doctor`-gated upgrade discipline documented** in `plan/install-plan.md` — "Each phase ends
  with a `hermes doctor` gate... Don't proceed if doctor is red," and a two-command backup
  requirement before every phase (`hermes backup` for config/sessions/skills, plus a separate
  tar backup for Hindsight's data — **Hindsight uses an embedded pg0 store under
  `/mnt/i/hindsight/data`, not the Postgres container; a `pg_dump` against the Postgres
  container silently captures nothing useful for Hindsight**, per that same file's callout).
  This mirrors the `openclaw doctor --lint` discipline from the OpenClaw pass closely enough
  that the existing Hermes-side process is probably already right — check it before
  inventing a new one.

---

## Methodology that worked well for OpenClaw/pi-mono/hindsight — apply the same discipline here

1. **Verify history state before picking an upgrade strategy** (done above for Hermes —
   it's the clean-rebase case). Don't assume; always run the ancestor check yourself if
   this doc is more than a day or two old by the time you read it.

2. **If there ARE local patches to carry forward, re-derive each one, don't blind-copy the
   diff.** For every patch: check current GitHub issues/PRs by keyword (query the web, don't
   guess) — is the underlying bug already fixed upstream? Superseded by an architecture
   change? Only keep what's still genuinely needed, and write a fresh minimal commit against
   current source rather than cherry-picking a diff that may not apply cleanly to code that's
   moved. (Not needed for Hermes right now since there are zero local patches, but if that
   changes before you get to this, this is the standard to hold new patches to.)

3. **Real backups before any real-data migration, never a "sandbox with hardcoded paths."**
   If Hermes has an equivalent to OpenClaw's state-dir/config migration (check for anything
   like a `doctor --fix`, session/memory migration, or config schema changes), do NOT try to
   test it against a copied "sandbox" whose own config might have absolute paths pointing
   back at the real install (that's exactly what leaked through and touched real production
   data during the OpenClaw pass — a copied config with hardcoded `agentDir`-style absolute
   paths bypassed the sandbox's env-var override for every agent except the one whose config
   left that field unset). If you need to test a migration safely, either fully back up the
   real state first (`robocopy ... /E /XJ` on Windows — the `/XJ` matters, see next point) and
   operate on the real thing, or make sure the sandbox copy's own config has every path
   rewritten to point inside the sandbox, not just the directory the copy lives in.

4. **`robocopy` follows junctions/symlinks by default — always pass `/XJ` for a backup.**
   Without it, backing up a directory that contains a junction (e.g., to a global CLI install
   that itself junctions to a huge monorepo) can balloon a multi-GB backup into hundreds of
   GB / millions of files before you notice. Happened once this session; caught and cleaned
   up, but check for junctions first (`Get-ChildItem -Force -Attributes ReparsePoint` doesn't
   itself recurse into them, so it's safe for detection) rather than discovering it mid-copy.

5. **Config/schema hygiene: run whatever Hermes's equivalent of `doctor --lint` is, fix real
   errors, don't chase every warning.** OpenClaw's `doctor --lint` distinguishes `error`
   (blocks startup, must fix) from `warning` (advisory). If Hermes has something similar,
   respect that distinction rather than treating every finding as equally urgent.

6. **If Hermes has an interactive-only CLI command for fixes/migrations** (OpenClaw's
   `doctor` needs a real TTY for anything beyond the safest auto-migrations, and hangs
   forever on stdin if run unattended/scripted) — check for the same failure mode here
   before scripting anything. Test with a short timeout first, don't assume it's safe to
   background a long-running interactive command.

7. **Model/provider assignments, if Hermes has its own model config separate from
   OpenClaw's**: don't trust vendor marketing framing for "best" model picks. If you're
   touching model assignments, cross-reference against a real, live task-based benchmark
   (e.g., OpenRouter's `tau2-bench-airline` leaderboard — https://openrouter.ai/benchmarks —
   real agentic tool-calling accuracy/cost/speed, not synthetic Q&A). It changed several
   picks in the OpenClaw pass in ways vendor pages alone would not have surfaced (a
   "flagship" model that was actually mediocre on real agentic tasks; a "fast" model variant
   that hung for 10+ minutes on a real open-ended task despite good benchmark numbers on a
   different task shape). Also test live in whatever chat/playground surface you have —
   synthetic benchmarks and hands-on behavior can diverge more than expected.

8. **Concurrent-agent git hygiene, if you parallelize this the way the OpenClaw session's
   knowledge-base audit did (6 parallel agents editing a shared corpus):** never `git add -A`
   or `git commit -a`. Stage explicit file paths per agent/task, and have each agent verify
   via `git diff`/fresh reads that its own target files still contain what it expects
   *before* committing — other parallel agents' edits can land on shared files (a taxonomy
   doc, a hub/index file) mid-task. This worked well: zero lost or duplicated edits across
   6 parallel passes doing real content changes, because every agent checked before writing
   and before committing.

9. **If a build/CLI command gets blocked by "Claude Code auto mode classifier" with no more
   specific reason:** don't try to route around it via another tool — that's an explicit
   instruction violation. Either hand the exact command to the user to run themselves, or
   (if you're a subagent and the parent session has different permissions) leave the change
   staged/ready and report back clearly what's blocked and why, with the exact command to run.

10. **Before treating something as "obviously a bug," check the actual planning docs first.**
    The OpenClaw session almost "fixed" an appendix cross-reference pattern as if it were
    arbitrary, until checking `plan.md`'s own "Pass 2 Cross-References" section confirmed the
    connections were explicitly planned and just never fully executed — a templating bug
    (wrong literal filename in a boilerplate block), not an intentional design choice. If
    Hermes has planning/design docs for whatever you're about to "fix," read them first.

---

## Specifically for this repo, next steps

1. Confirm latest upstream tag is still `v2026.8.31` (or whatever it's become).
2. Since history is clean and there are zero local commits ahead: this is very likely just
   `git merge upstream/main` (or `git reset --hard <latest-tag>` if you want to jump straight
   to a tagged release rather than upstream/main's tip — ask the user which they'd prefer,
   this session didn't get far enough to know Hermes's own release-cadence preference).
3. Check `package.json` engines (`node >=20.0.0`) against whatever Node is actually
   available — same portable-Node-if-needed approach used for OpenClaw
   (`I:\github\.tools\node-v24.20.0-win-x64\`, PATH-prepended only for the relevant commands,
   never touching the system-wide Node install) if the system default doesn't satisfy it.
4. Figure out the actual install/run mechanism before assuming it mirrors OpenClaw's junction
   setup — this repo's `./hermes` is a direct Python launcher script in the repo root, not
   obviously wired through a global npm link the way OpenClaw was. Check how the user
   actually invokes `hermes` day-to-day before assuming any particular "no copy step needed"
   shortcut applies here.
5. `I:\hermes\` (a separate, sibling directory — the knowledge-base/instance repo, not this
   fork) has its own handoff note for a related but distinct piece of work — see
   `I:\hermes\NEXT_SESSION.md` if that's also in scope for this session.
