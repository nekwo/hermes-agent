# Workspace LEVEL CLI tier: `hermes harness level …`.
#
# This module is exec'd into hermes_cli/harness.py's globals (see
# _load_command_parts) and shares the Stage-42 envelope/printer/error helpers
# with every other tier — imported from hermes_cli.harness_support below, not
# inherited. Both writes go through the LevelStore chokepoint, the same door
# realm sync's pull applier uses.
#
# THE CONTRACT, and the whole reason these two verbs exist: the launcher owns
# the level document's format and hermes owns its transport. `set` takes the
# launcher's `SceneSerializer` output and stores it VERBATIM; `show --full`
# hands the same bytes back. hermes validates exactly two facts about them (it
# is UTF-8 JSON, the object carries a `version`) and reformats nothing, because
# the day the backend's level routes land the transport is meant to be
# swappable without a format change.
#
# Design contract: EterniaLauncher
# docs/spatial/planned/one-engine-one-catalogue-levels-per-workspace.md (R15).

# Explicit import header — its rationale lives ONCE, in
# ``hermes_cli/harness_support.py``'s module docstring, which also names the
# two gates that hold it: ruff's F821 for the header being complete, and
# tests/hermes_cli/test_harness_parts_namespace.py for the load-order namespace.

from __future__ import annotations

import hashlib
from pathlib import Path

from agent_runtime.root_observability import attach_root_observability
from agent_runtime.store import WorkspaceStore
from hermes_cli.harness_support import (
    _object_envelope,
    _print_stage42,
    emit_harness_error,
)


def _level_store():
    from agent_runtime.level_sync import LevelStore

    return LevelStore()


def _level_workspace_for(args) -> str | None:
    return getattr(args, "workspace", None) or WorkspaceStore().active_id()


def _load_level_bytes(raw: str) -> bytes:
    """Resolve a ``--document`` value to the EXACT bytes to store.

    Deliberately NOT ``_load_request_json``: that helper parses, and a parsed
    document re-serialized on the way to disk is the one thing this family
    promises never to do. A path is read as bytes; anything else is taken as the
    document text itself and encoded UTF-8.

    **A path is the right call for anything real.** Windows caps a command line
    at ~32 KB and a level is up to 1 MB, so the launcher writes a temp file and
    passes its path; the inline form is for a hand-typed probe.
    """

    candidate = (raw or "").strip()
    if candidate[:1] not in {"{", "["}:
        try:
            path = Path(candidate)
            if path.is_file():
                return path.read_bytes()
        except OSError:
            # Not a usable path — fall through and take the literal as the
            # document, which is what the caller meant if it was not a filename.
            pass
    return candidate.encode("utf-8")


def _level_row(workspace_id: str, token: str, raw: bytes | None, *, full: bool) -> dict:
    """One level row. ``present: false`` is an honest empty, never an error.

    ``sha256`` is over the STORED BYTES, not the semantic hash realm sync merges
    on, and the two are named differently on purpose: this one answers "did the
    round trip preserve my document", which is the property the launcher's
    bridge checks, and a semantic hash cannot answer it.
    """

    row = {
        "workspace_id": workspace_id,
        "workspace_token": token,
        "present": raw is not None,
        "bytes": len(raw) if raw is not None else 0,
        "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
        "version": None,
    }
    if raw is not None:
        from agent_runtime.level_sync import validate_level_document

        try:
            row["version"] = validate_level_document(raw).get("version")
        except Exception:  # noqa: BLE001 — a stored document that will not read
            # is a FACT about the store, not a failure of the read verb. The row
            # stays, ``version`` stays null, and ``--full`` still hands the bytes
            # over — which is the only way an operator repairs one.
            row["version"] = None
    if full:
        row["document"] = raw.decode("utf-8", errors="replace") if raw is not None else None
    return row


def _cmd_level_show(args) -> int:
    """`harness level show` — read one workspace's level back.

    ``--full`` carries the document itself, and the metadata-only default is the
    office family's (`office show --full`) rather than an opinion of its own: a
    1 MB JSON string on a table print is not an answer anyone reads, and the one
    caller that wants the bytes always knows it wants them.
    """

    store = _level_store()
    workspace = _level_workspace_for(args)
    if not workspace:
        return emit_harness_error(
            ValueError("no workspace selected; pass --workspace"), args=args, code="invalid_request"
        )
    from agent_runtime import paths

    token = paths.safe_path_token(workspace)
    row = _level_row(workspace, token, store.read(workspace), full=bool(getattr(args, "full", False)))
    _print_stage42(attach_root_observability(_object_envelope("level", row)), args=args)
    return 0


def _cmd_level_set(args) -> int:
    """`harness level set` — store one workspace's level VERBATIM.

    The refusal taxonomy is the store door's own (`LevelDocumentError.code`), not
    a second reading of the document here: `invalid_payload` for a document this
    runtime will not accept, with the typed word the store used carried through
    as ``reason`` so the launcher can say WHICH of the four it was rather than
    "the level was rejected".

    ``--dry-run`` validates and reports what WOULD change without writing, which
    is what makes "will hermes take this document" answerable before a publish
    carries it to every member of a realm.
    """

    from agent_runtime.level_sync import LevelDocumentError, validate_level_document

    store = _level_store()
    workspace = _level_workspace_for(args)
    if not workspace:
        return emit_harness_error(
            ValueError("no workspace selected; pass --workspace"), args=args, code="invalid_request"
        )
    try:
        raw = _load_level_bytes(args.document)
    except OSError as exc:
        return emit_harness_error(exc, args=args, code="invalid_payload")
    try:
        validate_level_document(raw)
    except LevelDocumentError as exc:
        return emit_harness_error(exc, args=args, code="invalid_payload", reason=exc.code)
    from agent_runtime import paths

    token = paths.safe_path_token(workspace)
    if getattr(args, "dry_run", False):
        row = _level_row(workspace, token, raw, full=False)
        # ``changed`` is answered against what is on disk RIGHT NOW, so a dry run
        # over an identical document says "nothing would change" instead of
        # implying a write.
        row["changed"] = store.read(workspace) != raw
        row["dry_run"] = True
        _print_stage42(attach_root_observability(_object_envelope("level", row)), args=args)
        return 0
    try:
        outcome = store.write(workspace, raw)
    except OSError as exc:
        return emit_harness_error(exc, args=args, code="runtime_unavailable")
    row = _level_row(workspace, token, raw, full=False)
    row["changed"] = bool(outcome["changed"])
    _print_stage42(attach_root_observability(_object_envelope("level", row)), args=args)
    return 0
