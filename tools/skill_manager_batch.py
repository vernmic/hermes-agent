"""Atomic multi-op batch path for ``skill_manage``. Origin state
(``skill_manage``/``_find_skill``/``_skill_gate_bypass``) is reached lazily
through ``tools.skill_manager_tool`` so that module owns it."""

from contextlib import suppress
import json
import logging
import posixpath
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger("tools.skill_manager_tool")

_BATCH_OP_ACTIONS = {"create", "patch", "write_file", "remove_file"}
_BATCH_MAX_OPS = 20

# --- Per-op argument shape (checked before any effect) ---------------------------------
# action -> (arg, is_missing, error) checks run before the handler.
_MISSING, _IS_NONE = (lambda v: not v), (lambda v: v is None)
_REQUIRED_ARGS = {
    "create": [("content", _MISSING,
                "content is required for 'create'. Provide the full SKILL.md text (frontmatter + body).")],
    "edit": [("content", _MISSING,
              "content is required for a full rewrite. Provide the full updated SKILL.md text.")],
    "write_file": [
        ("file_path", _MISSING, "file_path is required for 'write_file'. Example: 'references/api-guide.md'"),
        ("file_content", _IS_NONE, "file_content is required for 'write_file'.")],
    "remove_file": [("file_path", _MISSING, "file_path is required for 'remove_file'.")]}
# A bare "required" error is a dead end: the model retries blindly and often escapes to
# action='write_file', clobbering the whole file.
_PATCH_NEEDS_OLD_STRING = (
    "old_string is required for 'patch' and must be the EXACT text currently in the file. "
    "Read the target file first (read_file on the skill's SKILL.md, or the file named by "
    "file_path) and copy the snippet verbatim, then retry 'patch'. Do NOT fall back to "
    "action='write_file' — that rewrites the entire file and destroys unrelated content.")
_PATCH_NEEDS_NEW_STRING = "new_string is required for 'patch'. Use an empty string to delete matched text."
_PATCH_EITHER_OR = ("Pass EITHER content (full SKILL.md rewrite) OR old_string/new_string "
                    "(targeted replacement), not both.")
# Text-slot keys a model confuses: key -> the action that reads it. A 27B model that just
# used write_file's file_content re-emits it on create/patch and then replays the identical
# payload when the error only says the right key is "required" — the hint has to name where
# the text actually landed so the retry can move it.
_TEXT_SLOT_OWNER = {"content": "create (and a full-rewrite patch)",
                    "new_string": "a targeted patch (with old_string)",
                    "file_content": "write_file"}
# action -> (text slots it reads, where misfiled text belongs)
_TEXT_SLOT_FOR = {
    "create": (("content",), "'content'"),
    "edit": (("content",), "'content'"),
    "patch": (("content", "new_string"), "old_string/new_string (targeted) or 'content' (full rewrite, last resort)"),
    "write_file": (("file_content",), "'file_content'")}


def _misplaced_text_hint(action: str, args: dict) -> str:
    """Sentence naming the text-slot key(s) this op carries that ``action`` never reads, or ''."""
    if action not in _TEXT_SLOT_FOR:
        return ""  # delete/remove_file/unknown: no text slot, so no destination to point at
    reads, destination = _TEXT_SLOT_FOR[action]
    stray = [k for k in _TEXT_SLOT_OWNER if k not in reads and args.get(k) is not None]
    if not stray:
        return ""
    carried = " and ".join(f"'{k}' (that key is for {_TEXT_SLOT_OWNER[k]})" for k in stray)
    return f" Note: this op carries {carried} — move that text to {destination}."


def _op_shape_error(action: str, args: dict):
    """Argument-shape error text for one op, or None. Only shape misses carry the misplaced-text
    hint: a patch whose real problem is an unmatched old_string must not be steered to a full
    rewrite. Pure function so the batch can reject a misfiled op BEFORE any sibling is applied."""
    for arg, missing, message in _REQUIRED_ARGS.get(action, ()):
        if missing(args.get(arg)):
            return message + _misplaced_text_hint(action, args)
    if action == "patch":
        # Every patch shape miss is decided here, not in the handler, so a batch never applies
        # op[0] only to roll it back over op[1]'s missing new_string or content+old_string mix.
        if args.get("content") and (args.get("old_string") or args.get("new_string") is not None):
            return _PATCH_EITHER_OR
        if not args.get("old_string") and not args.get("content"):
            return _PATCH_NEEDS_OLD_STRING + _misplaced_text_hint(action, args)
        if not args.get("content") and args.get("new_string") is None:
            return _PATCH_NEEDS_NEW_STRING
    return None


def _validate_batch_ops(operations, default_name, tool_error):
    """Shape checks with no side effects. Returns (names, None) or (None, error_json)."""
    from tools.skill_manager_guards import _background_review_preflight
    from tools.skill_manager_tool import _validate_category
    def fail(i, msg):
        return None, tool_error(f"operations[{i}]{msg}", success=False)
    names = []
    for i, op in enumerate(operations):
        if not isinstance(op, dict) or not op.get("action"):
            return fail(i, " needs an 'action'.")
        act = op["action"]
        if act not in _BATCH_OP_ACTIONS:
            return fail(i, f": unknown action '{act}'. Batchable: "
                           f"{', '.join(sorted(_BATCH_OP_ACTIONS))}; delete must be sole.")
        nm = op.get("name") or default_name
        if not nm:
            return fail(i, " needs a 'name' (the skill it targets).")
        # Reject a misfiled op here, before any sibling is applied: a runtime failure on
        # op[1] would first apply op[0] and then roll the whole batch back.
        if (shape_err := _op_shape_error(act, op)) is not None:
            return fail(i, f" ({act} on '{nm}'): {shape_err}")
        # create's category is resolved to a target dir before the snapshot: reject a bad one here
        # so it returns a JSON error (not a TypeError) and never leaks the snapshot tempdir.
        if act == "create" and (cat_err := _validate_category(op.get("category"))) is not None:
            return fail(i, f" ({act} on '{nm}'): {cat_err}")
        names.append(nm)
        if act == "create" and nm in names[:-1]:
            return fail(i, f": create for '{nm}' must precede that skill's other ops.")
        if (preflight := _background_review_preflight(act, nm)) is not None:
            return None, json.dumps(preflight, ensure_ascii=False)
    # Clobber guard: a DESTRUCTIVE op (create/write_file/remove_file/full rewrite) on
    # a file an earlier op touched would SILENTLY discard its work — reject it.
    # Additive patches are always legal. Paths are normalized against spelling variants.
    touched_files = set()
    for i, op in enumerate(operations):
        act, nm = op["action"], names[i]
        # create and full-rewrite patch (content) always hit SKILL.md.
        full_rewrite = act == "patch" and bool(op.get("content"))
        fp = (op.get("file_path") or "").strip()
        target = ("SKILL.md" if (act == "create" or full_rewrite or not fp)
                  else posixpath.normpath(fp.lstrip("/")))
        key = (nm, target)
        if (act in ("create", "write_file", "remove_file") or full_rewrite) and key in touched_files:
            return fail(i, f": {act} on '{target}' of skill '{nm}' — an earlier op in this "
                           f"batch already touched that file, and this op would silently discard its work. "
                           f"One destructive op (write_file/remove_file/full rewrite) per file per batch; put "
                           f"it first, or fold the change in. Patch chains are fine.")
        touched_files.add(key)
    return names, None


def _snapshot_skills(names, snap_root, find_skill, create_targets):
    """Copy every touched skill aside. Returns (snapshots, None) or (None, error_text).

    ``create_targets`` maps a name with no skill yet to the dir its ``create`` op will use.
    An EMPTY pre-existing dir there has no SKILL.md to snapshot, yet create adopts it (see
    ``_create_skill``): record that it pre-dated the batch so rollback never rmtree()s it."""
    snapshots = {}  # skill name -> (pre_dir or None, snapshot_dir or None, dir_pre_existed)
    for nm in dict.fromkeys(names):  # ordered unique
        pre = find_skill(nm)
        pre_dir = Path(pre["path"]) if pre else None
        snap = snap_root / nm if pre_dir is not None and pre_dir.is_dir() else None
        if snap is not None:
            try:
                shutil.copytree(pre_dir, snap)
            except Exception as exc:
                return None, f"Could not snapshot '{nm}' for atomic batch: {exc}"
        target = create_targets.get(nm) if pre is None else None
        snapshots[nm] = (pre_dir, snap, target is not None and target.is_dir())
    return snapshots, None


def _restore_snapshot(pre_dir, snap, post_dir, dir_pre_existed=False, written=()) -> None:
    post_exists = post_dir is not None and post_dir.is_dir()
    if snap is None:
        if not post_exists:
            return
        if not dir_pre_existed:  # Batch created this skill: remove the partial result.
            shutil.rmtree(post_dir)
            return
        # The dir predates the batch (adopted empty leftover): unlink exactly the files the
        # batch wrote there, then rmdir() the now-empty dirs up to and including the skill dir.
        # rmdir() fails on anything left, so a file that landed out-of-band survives.
        for rel in ("SKILL.md", *written):
            target = post_dir / rel
            with suppress(OSError):
                target.unlink()
            for parent in target.parents:
                if parent == post_dir or not parent.is_relative_to(post_dir):
                    break
                with suppress(OSError):
                    parent.rmdir()
        with suppress(OSError):
            post_dir.rmdir()
        return
    if not post_exists:
        shutil.copytree(snap, pre_dir)
        return
    # Move the broken state aside and delete it only after the snapshot is
    # back, so a failed copytree (disk full, locked file) can't mean total loss.
    aside = post_dir.with_name(post_dir.name + ".rollback-broken")
    shutil.rmtree(aside, ignore_errors=True)
    post_dir.rename(aside)
    try:
        shutil.copytree(snap, pre_dir)
    except Exception:
        # Restore failed: put the half-applied state back rather than nothing.
        shutil.rmtree(pre_dir, ignore_errors=True)
        aside.rename(pre_dir)
        raise
    shutil.rmtree(aside, ignore_errors=True)


def _rollback(snapshots, find_skill, results):
    """Restore every snapshot. ``results`` are the ops applied so far (their name/file_path
    tell an adopted dir's rollback which files were the batch's). Returns (note, failed)."""
    notes = []
    for nm, (pre_dir, snap, dir_pre_existed) in snapshots.items():
        written = [posixpath.normpath(r["file_path"].lstrip("/")) for r in results
                   if r["name"] == nm and r["action"] == "write_file" and r["file_path"]]
        try:
            post = find_skill(nm)
            _restore_snapshot(pre_dir, snap, Path(post["path"]) if post else None,
                              dir_pre_existed, written)
        except Exception as exc:
            notes.append(f"ROLLBACK FAILED for '{nm}' ({exc})"
                         + (f"; snapshot preserved at '{snap}'" if snap is not None else ""))
    return ("; ".join(notes) if notes else "all touched skills rolled back"), bool(notes)


_ADVISORY_KEYS = ("lint_warnings", "lint_hint")


def _skill_manage_batch(operations, default_name: str | None = None, task_id: str | None = None,
                        session_id: str | None = None, evidence=None) -> str:
