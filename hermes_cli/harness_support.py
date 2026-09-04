"""Harness-local presentation, envelope, and error-taxonomy helpers.

Split out of ``hermes_cli/harness.py`` (P0 step 2). These are the pieces the
exec'd command parts under ``hermes_cli/harness_parts/`` reach for by free
name — the Stage-42 envelope builders, the printer, the row sorter, the
request-JSON loader, and the error taxonomy. Housing them in a real,
importable module lets each part declare an explicit import header instead of
inheriting them from whatever harness.py happened to define, which is what
makes the parts analysable at all (see
``tests/hermes_cli/test_harness_parts_namespace.py``).

WHY EACH PART CARRIES AN EXPLICIT IMPORT HEADER
-----------------------------------------------
This paragraph lived, verbatim, at the top of all six parts until 2026-08-19.
It is here once now, and each part points at it.

The parts are still ``exec``'d into ``harness.py``'s globals by
``_load_command_parts`` — that mechanism is unchanged — but they are no longer
DEPENDENT on it. These names used to arrive implicitly from whatever
``harness.py`` happened to import, so a wrong one surfaced as a ``NameError``
only when an operator ran the one verb that touched it: a latent break with an
arbitrarily long fuse, discovered in production by whoever reached for the
least-used command. Re-importing a name ``harness.py`` also imports rebinds it
to the identical object, so the explicit header costs nothing at runtime. Both
halves — that the header is present, and that it rebinds identically — are
checked by ``tests/hermes_cli/test_harness_parts_namespace.py``.

Nothing here knows about argparse wiring or any specific command: harness.py
keeps its 50 local command bodies and re-imports these names, so
``hermes_cli.harness.emit_harness_error`` (imported by ``harness_parts/serve.py``
and ``hermes_cli/main.py``) keeps resolving exactly as before.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path

from agent_runtime.cli_format import emit_json
from agent_runtime.errors import (
    AgentRuntimeError,
    AlreadyExists,
    ArchiveUnreadable,
    DefaultScopeReconciliationRequired,
    EventPayloadTooLarge,
    IdempotencyKeyVerbMismatch,
    IdempotentReplayUnresolved,
    NotFound,
    StaleRevision,
    StoreCorrupt,
    SyncConflict,
    WorkspaceDeleteBlocked,
    WorkspaceUnresolved,
)
from agent_runtime.persona_chat_continuity import PERSONA_CHAT_SESSION_SOURCE
from agent_runtime.realm_sync import RealmSyncError

__all__ = [
    "ERROR_EXIT_CODES",
    "PERSONA_CHAT_SESSION_SOURCE",
    "STAGE42_SCHEMA_VERSION",
    "emit_harness_error",
    "harness_repo_root",
    "_apply_fields",
    "_error_code_for_exception",
    "_error_envelope",
    "_error_hint",
    "_list_envelope",
    "_load_request_json",
    "_object_envelope",
    "_print_stage42",
    "_quiet_output",
    "_redact_paths",
    "_require_yes",
    "_safe_error_message",
    "_sort_rows",
    "_table_output",
]


def harness_repo_root() -> Path:
    """The hermes-agent checkout root, anchored to this file.

    The command parts are exec'd into harness.py's globals, so a part reading
    ``__file__`` sees *harness.py's* path, not its own — ``parents[1]`` there
    means the repo root only by accident of harness.py living one level down.
    Anchoring here removes that coupling: this module's ``__file__`` is
    ``<repo>/hermes_cli/harness_support.py`` whether it is imported or a caller
    is exec'd.
    """

    return Path(__file__).resolve().parents[1]


STAGE42_SCHEMA_VERSION = 1
ERROR_EXIT_CODES = {
    "not_found": 3,
    "workspace_not_found": 3,
    # An office write named a workspace no RECORD resolves (OfficeStore
    # .ensure_surface refusing to author a surface for it). Family 3 beside
    # workspace_not_found on purpose: same operator cure, same exit, one
    # spelling per lane rather than a second vocabulary for one contract.
    "workspace_unresolved": 3,
    "run_not_found": 3,
    "persona_not_found": 3,
    "blueprint_not_found": 3,
    "invalid_request": 2,
    "invalid_payload": 2,
    "invalid_isolation": 2,
    # `harness skills delete` refusals (SkillTombstoneRefused). Family 2 because
    # the fault is in the REQUEST and the operator's next move is to change what
    # they typed: a malformed slug is retyped, and an installer-owned id is
    # deleted through the other lane entirely (the constant plus the repo source
    # under docs/agent-runtime-harness/harness-skills/) rather than through a
    # realm ledger the next pull's installer would overrule. Two codes and one
    # family, on this table's standing rule: the family is the next MOVE, the
    # code is WHICH correction.
    "skill_slug_invalid": 2,
    "skill_installer_owned": 2,
    # One board idempotency key presented to two card verbs
    # (``IdempotencyKeyVerbMismatch``). Family 2 on this table's standing rule —
    # the family is the next MOVE and the code is WHICH correction: the caller
    # retypes the gesture with a key of its own. Deliberately NOT beside
    # ``idempotent_replay_unresolved`` (family 7, retryable): that one is a
    # damaged file an operator repairs before the identical call succeeds, and
    # this one is a request that will be refused identically forever.
    "idempotency_key_verb_mismatch": 2,
    "duplicate_conflict": 4,
    # The office desk fence (D6): this persona already holds a live desk on this
    # level. Family 4 beside ``duplicate_conflict`` because the operator's next
    # MOVE is the same (something is already placed — move or remove it); its own
    # CODE because WHICH thing differs, and because the word has to match the
    # wire's ``data.reason`` and the launcher's render-time detector or one
    # refusal ends up with three names.
    "duplicate_desk": 4,
    "already_exists": 4,
    "stale_revision": 4,
    # ``agent_already_assigned`` left this table with AX2 (2026-08-31). It was
    # the snapshot ``warnings`` lane's only code, that lane's only input was the
    # writerless assignment store, and the launcher had already tombstoned the
    # spelling with the retired bridge-error vocabulary — so no producer on
    # either side of the wire could reach this row.
    "spawn_scope_exhausted": 4,
    "sync_conflict": 4,
    "sync_behind": 4,
    "sync_secret_excluded": 4,
    # Permission / auth (5)
    "permission_denied": 5,
    "membership_denied": 5,
    "role_insufficient": 5,
    "provider_auth_expired": 5,
    "sync_auth_failed": 5,
    # State / precondition (6)
    "proof_missing": 6,
    "needs_operator_confirm": 6,
    "default_scope_reconciliation_required": 6,
    # `harness realm sync revert` with no local clone (or no subtree for this
    # realm inside one). Family 6 and not 4: nothing conflicts and nothing is
    # broken — the local picture of upstream simply is not there yet, and the
    # operator's next MOVE is `realm sync pull`. It refuses rather than reading
    # the absence as "upstream has nothing", which is the one misreading that
    # would archive an operator's whole local office.
    "sync_repo_missing": 6,
    # Skills / readiness (6)
    "skill_hash_mismatch": 6,
    "missing_skill": 6,
    "profile_not_ready": 6,
    # `harness work cancel` refusals. Split on WHY, because the operator's next
    # move differs: `cancel_unsupported` means this kind of work has no
    # interrupt seam at all (nothing to retry — a precondition, 6), while
    # `cancel_unavailable` / `cancel_failed` mean the owning subsystem is not
    # reachable from this process or refused the interrupt (an infra condition,
    # 7, retryable from the lane that owns the work).
    "cancel_unsupported": 6,
    # `harness gateway pair` refusals (remote-gateway Stage 1). The operator's
    # next MOVE is to WAIT: for one of three outstanding codes to be redeemed or
    # to expire, or for a brute-force lockout to lapse. Family 6 because both are
    # PRECONDITIONS on the pairing store's state rather than faults — nothing is
    # broken, nothing needs repairing, and the identical command succeeds later
    # unchanged. Two codes and one family, on the rule this table already
    # follows: the family is the next move, the code is which precondition.
    "pairing_codes_pending": 6,
    "pairing_locked_out": 6,
    "confirmation_required": 8,
    # Runtime / infra (7)
    "runtime_unavailable": 7,
    "wrong_runtime_root": 7,
    "budget_exhausted": 7,
    "sync_remote_unreachable": 7,
    "install_clone_failed": 7,
    "install_venv_failed": 7,
    "install_postinstall_failed": 7,
    "install_dependency_missing": 7,
    "cancel_unavailable": 7,
    "cancel_failed": 7,
    # A file this write DEPENDS ON would not decode, so the runtime declined to
    # answer from a guess (EG-1.5 `archive_unreadable` — the archived copy holds
    # the revision token an ``upsert_actor`` must bump; EG-6.6
    # `actors_unreadable` — the class-key fence cannot see the whole actor
    # directory it must consult). 7 and not 1, deliberately:
    #
    # * the 1-family is a TERMINAL verdict on the data and it shares its number
    #   with ``internal_error``. Spending it here would name the fault correctly
    #   in ``error.code`` and then report the exact wrong story in the exit
    #   status — a corrupt server file still reading as "the harness crashed",
    #   which is the half of EG-1.5's defect the CLI lane never got;
    # * the condition is TRANSIENT and retryable in the way this family already
    #   means (an AV hold releases; an operator repairs one file and the same
    #   call succeeds unchanged) — the same reasoning the wire records for
    #   spending ``-32600`` "cannot serve this right now" rather than a 4090
    #   refusal, and the same reasoning ``cancel_unavailable`` sits here on.
    #
    # Two codes, one family: the family is the operator's next MOVE (retry once
    # the file is readable), the code is WHICH file to repair.
    "archive_unreadable": 7,
    "actors_unreadable": 7,
    # The other two ``ArchiveUnreadable`` subclasses, added 2026-09-04. They
    # were raised (``board_store.py`` card reads, ``persona_assignments.py``
    # instance reads) and mapped -- ``_error_code_for_exception`` returns
    # ``exc.code`` for the whole family -- but they had no ROW here, so
    # ``ERROR_EXIT_CODES.get(code, 1)`` handed them 1: the number the two rows
    # above are spending an entire comment to avoid, and the one that shares
    # its family with ``internal_error``. The comment beside that mapping arm
    # says a subclass "inherits the exit family"; it did not. Same family, same
    # cure, one code per file, pinned now by
    # ``tests/hermes_cli/test_error_exit_code_producers.py``
    # ``::test_every_archive_unreadable_subclass_inherits_the_exit_family_it_claims``.
    "cards_unreadable": 7,
    "persona_instances_unreadable": 7,
    # A recorded idempotency receipt that cannot be resolved to the row it
    # names. Family 7 beside the two rows above because the operator cure is
    # theirs exactly — repair or remove one unreadable file and run the same
    # command again — and because the alternative is what it did before this
    # row existed: fall through to the AgentRuntimeError catch-all and exit 1
    # as ``internal_error``, reporting a refusal as a harness crash.
    "idempotent_replay_unresolved": 7,
    # Data integrity (1)
    "store_corrupt": 1,
    # This machine's own store could not be WRITTEN (R-D14). Family 1 beside
    # ``store_corrupt`` and deliberately NOT 7, which is where every other I/O
    # condition on a root sits, because the two differ on the only thing an exit
    # family encodes — whether the identical command succeeds later:
    #
    # * a 7 is transient. An AV hold releases, a listener comes up, and the same
    #   call works unchanged. Retrying is the cure.
    # * this is a standing verdict about a directory's permissions. D3 run #1
    #   retried it once a minute for four minutes and got the identical
    #   [WinError 5] every time, and the launcher — reading ``runtime_unavailable``
    #   and mapping it to ``no_route`` — told the operator the OTHER MACHINE was
    #   unreachable. It was a local disk permission, and no amount of network is
    #   going to fix it.
    #
    # So it is terminal until a human changes something here, and the message
    # names the file so they know which something. ``retryable`` is false for the
    # same reason: a client that retries this burns a pairing code per attempt.
    "store_unwritable": 1,
    "event_payload_too_large": 1,
    "internal_error": 1,
    "timeout": 124,
}


def emit_harness_error(exc: BaseException, *, args=None, code: str | None = None, message: str | None = None, reason: str | None = None) -> int:
    """Render one refusal. ``reason`` is the RAW word the layer below used.

    R-D6, and it exists because ``code`` is a FAMILY. Nine store refusals map
    onto ``runtime_unavailable`` and three onto ``invalid_payload``, so a caller
    that only has the code knows what to do next and cannot say what happened —
    which is exactly what S4's 12:00:40 receipt hit: the launcher wrote
    ``no_route``, hermes had said something specific, and the specific thing was
    unrecoverable from the other machine.

    Omitted rather than ``null`` when a caller passes nothing, so every envelope
    this harness has ever emitted keeps its exact byte shape (the response
    fixtures and their Launcher mirrors pin those bytes) and only the verbs that
    have a reason to give grow the key.
    """

    error_code = code or _error_code_for_exception(exc)
    safe_details = {"error_class": type(exc).__name__}
    if isinstance(exc, RealmSyncError):
        safe_details.update(exc.safe_details)
    if isinstance(exc, WorkspaceDeleteBlocked):
        safe_details.update(exc.safe_details)
    if isinstance(exc, WorkspaceUnresolved):
        safe_details.update(exc.safe_details)
    if isinstance(exc, DefaultScopeReconciliationRequired):
        safe_details.update(exc.safe_details)
    envelope = _error_envelope(
        error_code,
        message or _safe_error_message(exc),
        # ``archive_unreadable`` / ``actors_unreadable`` are retryable for the
        # reason their exit family is 7: the file becomes readable again when an
        # AV hold releases or an operator repairs it, and the identical call then
        # succeeds. Saying ``retryable: false`` beside a 7 would have the two
        # halves of one envelope disagree about the same fault.
        retryable=getattr(exc, "retryable", False) or error_code in {"runtime_unavailable", "daemon_offline", "timeout", "archive_unreadable", "actors_unreadable", "cards_unreadable", "persona_instances_unreadable", "idempotent_replay_unresolved"},
        safe_details=safe_details,
    )
    cleaned = str(reason or "").strip()
    if cleaned:
        envelope["error"]["reason"] = cleaned
    _print_stage42(envelope, args=args, default_output="json")
    return ERROR_EXIT_CODES.get(error_code, 1)


def _error_code_for_exception(exc: BaseException) -> str:
    if isinstance(exc, NotFound):
        return "not_found"
    if isinstance(exc, AlreadyExists):
        return "already_exists"
    if isinstance(exc, RealmSyncError):
        return exc.code
    # A persisted-entity file that does not exist on disk is a lookup miss,
    # not an internal error — map it to the not-found taxonomy (exit 3).
    if isinstance(exc, FileNotFoundError):
        return "not_found"
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_payload"
    # A file the write DEPENDS ON will not decode. Its own ``code``, not a
    # constant and not a row in the tuple below: ``ActorsUnreadable`` subclasses
    # ``ArchiveUnreadable`` so it inherits the exit family and the cure SHAPE
    # (repair one file, retry the same call) while naming a DIFFERENT file —
    # a fixed string here would tell an operator holding an undecodable ACTOR
    # file to go and fix the archive copy. This is the same
    # ``exc.code``-not-the-class-constant rule the three RPC write arms record
    # (``serve_rpc.py`` — upsert, archive, resolve), so one condition keeps one
    # name per lane instead of one name per verb.
    #
    # Placed ahead of the ``AgentRuntimeError`` catch-all it exists to escape:
    # without this row ``harness office actor-remove`` / ``actor-upsert`` over an
    # undecodable archived copy exited 1 as ``internal_error`` — a corrupt file
    # on the server reported as a harness crash, which is precisely the
    # names-the-wrong-party failure EG-1.5 fixed on the wire and left standing
    # here. Verbs that catch the condition themselves (``harness_parts/office.py``
    # actor-upsert, EG-6.6) pass ``code=`` explicitly and never reach this; this
    # row is what covers every OTHER office write verb.
    if isinstance(exc, ArchiveUnreadable):
        return exc.code
    # An office write naming a workspace no record resolves. Its own ``exc.code``
    # under the same rule as the row above, and placed AHEAD of the
    # ``AgentRuntimeError`` catch-all for the same reason that row was: without
    # it, an operator refusal exits 1 as ``internal_error``, and a refusal is not
    # a harness crash. Exit family 3 (``ERROR_EXIT_CODES``) so it lands beside
    # ``workspace_not_found`` — the RPC office arms' spelling for the neighbouring
    # condition — and no existing client branch has to learn a new exit.
    if isinstance(exc, WorkspaceUnresolved):
        return exc.code
    # A recorded idempotency receipt naming a row that will not resolve.
    # Ahead of the catch-all for the third time and the same reason: the
    # write REFUSED rather than crashed, and re-running it is the one thing
    # that must not happen (``add_card`` mints a new id per call, so the key
    # that exists to prevent a duplicate would create one).
    if isinstance(exc, IdempotentReplayUnresolved):
        return exc.code
    # One board idempotency key presented to two different card verbs. Ahead of
    # the catch-all for the fourth time and the same reason — a refusal is not a
    # crash — and its own row rather than a share of the one above because the
    # two disagree about ``retryable``: repairing nothing will ever make this
    # call succeed, so it must not join the retryable set that row is in.
    if isinstance(exc, IdempotencyKeyVerbMismatch):
        return exc.code
    # Typed AgentRuntimeError subclasses map to their precondition/integrity
    # codes. Four rows left this tuple on 2026-08-19 — InvalidTransition,
    # StaleRun, ProofMissing and RuntimeRootMismatch — because an AST Raise
    # walk over all thirteen production packages AND `tests/` found zero raise
    # sites for any of them: each class existed to be mapped here and nowhere
    # else. A mapping row for an exception nothing throws is not defensive, it
    # is a claim about the runtime that is false.
    for exc_type, code in (
        (StoreCorrupt, "store_corrupt"),
        (DefaultScopeReconciliationRequired, "default_scope_reconciliation_required"),
        (EventPayloadTooLarge, "event_payload_too_large"),
        (StaleRevision, "stale_revision"),
        (SyncConflict, "sync_conflict"),
    ):
        if isinstance(exc, exc_type):
            return code
    if isinstance(exc, ValueError):
        text = str(exc)
        if text in ERROR_EXIT_CODES:
            return text
        return "invalid_request"
    # THE CATCH-ALL, AND WHAT IT COSTS (surveyed 2026-09-04).
    #
    # Four typed conditions above are hand-placed AHEAD of this row, each with
    # its own comment saying the same sentence: without the row, a REFUSAL or a
    # damaged server file exits 1 as ``internal_error``, which names the wrong
    # party. Four of one shape is a pattern, not four bugs -- the default here
    # is "the harness crashed", and it is applied to every typed subclass
    # nobody remembered to escape.
    #
    # Measured over every ``AgentRuntimeError`` subclass on this date, six
    # still land here: ``ActorArchived`` (which DECLARES ``code =
    # "actor_archived"`` and never gets to spend it through this lane -- only
    # the two arms that catch it by hand, ``harness_parts/office.py`` and
    # ``serve_rpc.py``, name it), ``SkillTombstoneRefused``,
    # ``WorkspaceDeleteBlocked``, ``ProbeIsolationViolation``,
    # ``PersonaInstanceRetireError`` and ``StaleModelOverrideWrite``. The first
    # four are refusals.
    #
    # Whether the default should instead be DECLARED per error class -- read
    # ``exc.code`` when the class carries one, and let this row cover only the
    # classes that genuinely have no name for themselves -- is an open operator
    # ruling, not a drive-by: it moves the exit status of live conditions, and
    # ``actor_archived`` has no ``ERROR_EXIT_CODES`` row to move to yet.
    if isinstance(exc, AgentRuntimeError):
        return "internal_error"
    return "internal_error"


def _error_envelope(code: str, message: str, *, retryable: bool = False, safe_details: dict | None = None, hint: str | None = None, correlation_id: str | None = None) -> dict:
    return {
        "schema_version": STAGE42_SCHEMA_VERSION,
        "kind": "error",
        "error": {
            "code": code,
            "message": message,
            "hint": hint or _error_hint(code),
            "retryable": bool(retryable),
            "error_id": f"err_{uuid.uuid4().hex[:8]}",
            "correlation_id": correlation_id,
            "safe_details": safe_details or {},
        },
    }


def _error_hint(code: str) -> str:
    return {
        "confirmation_required": "Re-run with --yes after confirming the destructive operation.",
        "not_found": "Check the id with the matching list command.",
        "workspace_not_found": "Run `hermes harness workspace list --json` and retry with a listed id.",
        "default_scope_reconciliation_required": "Run `hermes harness realm default-scope --dry-run --json`; no identities will change without explicit approval.",
        "sync_conflict": "Resolve conflicts in the realm sync git repo, then retry.",
        "sync_behind": "Run `hermes harness realm sync pull <realm> --json` before publishing.",
        "sync_repo_missing": "Run `hermes harness realm sync pull <realm> --json` first — a revert reconciles against the last-pulled subtree.",
        "sync_secret_excluded": "Remove secrets/state from the realm sync allowlist source before retrying.",
        "sync_remote_unreachable": "Check network/git remote availability and retry.",
        "sync_auth_failed": "Provide a fresh launcher-brokered credential via --credential-file or HERMES_REALM_SYNC_CREDENTIAL.",
        # The default hint says "retry after correcting the request", which is
        # the one thing that cannot help here: nothing about the request is
        # wrong. These two name the FILE instead — and they are two hints for the
        # same reason they are two codes (errors.py: the operator's cure must not
        # be mislabelled), because repairing the archive copy does nothing for an
        # undecodable live actor file and vice versa.
        #
        # CORRECTED 2026-08-19 (MCF-47(iii)). This said ``store/office_archive/``
        # and sent the operator to the WRONG TREE. ``store_root()/office_archive``
        # is the archived office SURFACE graveyard written by ``harness office
        # archive-surface``; the error is raised from reads of
        # ``paths.office_archived_actor_path`` — ``office/<ws>/archive/<actor>.json``
        # — which is a different directory reached by a near-identical name. An
        # operator following the old hint would have repaired nothing and found
        # nothing, on the one taxonomy row whose whole purpose is to name the file.
        # ``core_cache.py``'s exclusion note already spells out that these two
        # trees are distinct; this hint is where that distinction was lost.
        "archive_unreadable": "Repair or remove the undecodable archived actor copy under office/<workspace-id>/archive/ in the runtime store, then retry the same command.",
        "idempotent_replay_unresolved": "The idempotency receipt named in the message records a write whose row cannot be found. Repair or remove that receipt file under boards/<board-id>/idempotency/, or retry with a fresh --idempotency-key, then run the same command again.",
        # Names the two verbs' collision and the ONE cure. It must not say
        # "retry the same command": the same command is exactly what is refused,
        # and the neighbouring hint's "repair the file" is wrong here — the
        # receipt is intact and correct about the gesture that wrote it.
        "idempotency_key_verb_mismatch": "That --idempotency-key already belongs to a different board verb on this board (the message names both). One key names one gesture: re-run with a key of its own. Retrying this command unchanged will be refused identically.",
        "actors_unreadable": "Repair or remove the undecodable actor file named in the message, or pass --persona-instance-id to place the instance, then retry.",
        # Its own hint rather than the default: the default sends the operator to
        # ``safe_details``, and this refusal's facts ride the MESSAGE (
        # ``emit_harness_error`` merges details for three named types and this is
        # not one of them), so the default would point at an empty object.
        "duplicate_desk": "Move the desk this persona already holds — named in the message — or remove it with `harness office actor-remove`, then retry.",
        # Both name the operator's actual next move rather than the default's
        # "inspect safe_details": the details carry only the slug, which is the
        # one thing the operator already typed.
        "skill_slug_invalid": "Use a bare slug or <category>/<name>; see `hermes harness skills inventory --json` for the catalog spelling.",
        "skill_installer_owned": "This id is reinstalled from repo source on every pull. Remove it from hermes_constants.CANONICAL_SHARED_SKILL_IDS and docs/agent-runtime-harness/harness-skills/ instead.",
    }.get(code, "Inspect safe_details and retry after correcting the request.")


_ABS_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|[\\/])[^\s\"']*[\\/][^\s\"']*")


def _redact_paths(text: str) -> str:
    """Replace absolute filesystem paths with their basename.

    The error contract forbids absolute paths in messages (they leak the
    runtime root). A bare `realm_nope.json` is enough for the operator.
    """

    def _basename(match: "re.Match[str]") -> str:
        token = match.group(0)
        return re.split(r"[\\/]", token)[-1] or token

    return _ABS_PATH_RE.sub(_basename, text)


def _safe_error_message(exc: BaseException) -> str:
    text = " ".join(str(exc or type(exc).__name__).split())
    text = _redact_paths(text)
    return text[:300] or type(exc).__name__


def _load_request_json(raw: str) -> dict:
    """Resolve a ``--request-json`` value to a parsed object.

    Accepts either a path to a JSON file or an inline JSON document. Inline
    JSON (or a malformed file) that fails to parse raises ``json.JSONDecodeError``
    which the CLI maps to ``invalid_payload`` (exit 2) — never the file-not-found
    ``internal_error`` the bare ``Path(...).read_text()`` produced.
    """
    candidate = (raw or "").strip()
    looks_inline = candidate[:1] in {"{", "["}
    if not looks_inline:
        try:
            path = Path(candidate)
            if path.is_file():
                candidate = path.read_text(encoding="utf-8")
        except OSError:
            # Not a usable path — fall through and parse the literal as JSON.
            pass
    return json.loads(candidate)


def _list_envelope(item_kind: str, items: list[dict], *, cursor: str | None = None, truncated: bool = False) -> dict:
    return {
        "schema_version": STAGE42_SCHEMA_VERSION,
        "kind": "list",
        "item_kind": item_kind,
        "count": len(items),
        "items": items,
        "cursor": cursor,
        "truncated": bool(truncated),
    }


def _object_envelope(kind: str, item: dict, *, warnings: list[dict] | None = None) -> dict:
    data = {"schema_version": STAGE42_SCHEMA_VERSION, "kind": kind, **item}
    if warnings:
        data["warnings"] = warnings
    return data


def _print_stage42(data: dict, *, args, default_output: str | None = None) -> None:
    output = "json" if getattr(args, "json", False) else (getattr(args, "output", None) or default_output or ("table" if sys.stdout.isatty() else "json"))
    data = _apply_fields(data, getattr(args, "fields", None))
    if getattr(args, "quiet", False):
        print(_quiet_output(data))
        return
    if output == "json":
        print(emit_json(data))
    elif output == "yaml":
        import yaml

        print(yaml.safe_dump(json.loads(emit_json(data)), sort_keys=False, allow_unicode=True))
    else:
        print(_table_output(data, wide=output == "wide"))


def _apply_fields(data: dict, fields_text: str | None) -> dict:
    if not fields_text:
        return data
    fields = [field.strip() for field in fields_text.split(",") if field.strip()]
    if data.get("kind") == "list":
        kept = []
        for item in data.get("items") or []:
            kept.append({key: item.get(key) for key in fields if key in item})
        return {**data, "items": kept}
    return {key: data.get(key) for key in ["schema_version", "kind", *fields] if key in data}


def _quiet_output(data: dict) -> str:
    if data.get("kind") == "list":
        return "\n".join(str(item.get("id") or item.get("task_id") or "") for item in data.get("items") or [] if item)
    return str(data.get("id") or data.get("task_id") or "")


def _table_output(data: dict, *, wide: bool = False) -> str:
    if data.get("kind") == "error":
        err = data.get("error") or {}
        return f"{err.get('code')}: {err.get('message')}"
    if data.get("kind") == "list":
        items = list(data.get("items") or [])
        if not items:
            return f"no {data.get('item_kind', 'items')}"
        keys = list(items[0].keys()) if wide else [key for key in ("id", "title", "name", "state", "workspace_id", "realm_id", "updated_at") if key in items[0]]
        return "\n".join("  ".join(str(item.get(key, "")) for key in keys) for item in items)
    keys = [key for key in ("id", "title", "name", "state", "workspace_id", "realm_id", "updated_at") if key in data]
    return "  ".join(str(data.get(key, "")) for key in keys) if keys else emit_json(data)


def _require_yes(
    args,
    code: str = "confirmation_required",
    *,
    message: str | None = None,
    safe_details: dict | None = None,
) -> bool:
    """The ONE confirmation chokepoint for destructive harness verbs.

    ``message`` / ``safe_details`` exist so a verb can name WHAT it is about to
    destroy. A caller told to re-run with ``--yes`` against a bare "this
    destructive operation" string is confirming a SENTENCE, not a target — and
    on a lane where ids are opaque (``terminal:sess-8f2c``) that is precisely
    how the wrong thing gets killed. Both are optional and default to the
    historical text, so every existing caller stays byte-identical; a verb that
    CAN identify its target passes it here rather than printing a competing
    confirmation envelope of its own.
    """

    if getattr(args, "yes", False) or getattr(args, "dry_run", False):
        return True
    _print_stage42(
        _error_envelope(
            code,
            message or "This destructive operation requires --yes.",
            retryable=False,
            safe_details=safe_details,
        ),
        args=args,
        default_output="json",
    )
    return False


def _sort_rows(rows: list[dict], sort_key: str | None) -> list[dict]:
    key = str(sort_key or "").strip()
    if not key:
        return rows
    reverse = key.startswith("-")
    if reverse:
        key = key[1:]
    return sorted(rows, key=lambda item: str(item.get(key, "")), reverse=reverse)
