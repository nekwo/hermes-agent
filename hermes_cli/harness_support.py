"""Harness-local presentation, envelope, and error-taxonomy helpers.

Split out of ``hermes_cli/harness.py`` (P0 step 2). These are the pieces the
exec'd command parts under ``hermes_cli/harness_parts/`` reach for by free
name — the Stage-42 envelope builders, the printer, the row sorter, the
request-JSON loader, and the error taxonomy. Housing them in a real,
importable module lets each part declare an explicit import header instead of
inheriting them from whatever harness.py happened to define, which is what
makes the parts analysable at all (see
``tests/hermes_cli/test_harness_parts_namespace.py``).

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
    DefaultScopeReconciliationRequired,
    EventPayloadTooLarge,
    InvalidTransition,
    NotFound,
    ProofMissing,
    RuntimeRootMismatch,
    StaleRevision,
    StaleRun,
    StoreCorrupt,
    SyncConflict,
    WorkspaceDeleteBlocked,
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
    "realm_not_found": 3,
    "workspace_not_found": 3,
    "goal_not_found": 3,
    "run_not_found": 3,
    "lane_not_found": 3,
    "worker_not_found": 3,
    "persona_not_found": 3,
    "blueprint_not_found": 3,
    "invalid_request": 2,
    "invalid_payload": 2,
    "blueprint_invalid": 2,
    "repo_scope_invalid": 2,
    "invalid_binding": 2,
    "unbound_required_slot": 2,
    "invalid_isolation": 2,
    "duplicate_conflict": 4,
    "already_exists": 4,
    "stale_revision": 4,
    "agent_busy": 4,
    "agent_already_assigned": 4,
    "lane_budget_exceeded": 4,
    "repo_locked": 4,
    "spawn_scope_exhausted": 4,
    "kill_scope_denied": 4,
    "sync_conflict": 4,
    "sync_behind": 4,
    "sync_secret_excluded": 4,
    # Permission / auth (5)
    "permission_denied": 5,
    "membership_denied": 5,
    "role_insufficient": 5,
    "provider_auth_missing": 5,
    "provider_auth_expired": 5,
    "sync_auth_failed": 5,
    # State / precondition (6)
    "goal_blocked": 6,
    "goal_terminal": 6,
    "invalid_transition": 6,
    "stale_run": 6,
    "planning_locked": 6,
    "proof_missing": 6,
    "proof_gate_failed": 6,
    "needs_operator_confirm": 6,
    "default_scope_reconciliation_required": 6,
    # Skills / readiness (6)
    "skill_hash_mismatch": 6,
    "missing_skill": 6,
    "skill_install_failed": 6,
    "profile_not_ready": 6,
    "confirmation_required": 8,
    # Runtime / infra (7)
    "runtime_unavailable": 7,
    "daemon_offline": 7,
    "wrong_runtime_root": 7,
    "profile_mismatch": 7,
    "snapshot_stale": 7,
    "contract_version_mismatch": 7,
    "context_bundle_too_large": 7,
    "budget_exhausted": 7,
    "stagec_visual_failed": 7,
    "sync_remote_unreachable": 7,
    "install_clone_failed": 7,
    "install_venv_failed": 7,
    "install_postinstall_failed": 7,
    "install_dependency_missing": 7,
    # Data integrity (1)
    "store_corrupt": 1,
    "event_payload_too_large": 1,
    "internal_error": 1,
    "timeout": 124,
}


def emit_harness_error(exc: BaseException, *, args=None, code: str | None = None, message: str | None = None) -> int:
    error_code = code or _error_code_for_exception(exc)
    safe_details = {"error_class": type(exc).__name__}
    if isinstance(exc, RealmSyncError):
        safe_details.update(exc.safe_details)
    if isinstance(exc, WorkspaceDeleteBlocked):
        safe_details.update(exc.safe_details)
    if isinstance(exc, DefaultScopeReconciliationRequired):
        safe_details.update(exc.safe_details)
    envelope = _error_envelope(
        error_code,
        message or _safe_error_message(exc),
        retryable=getattr(exc, "retryable", False) or error_code in {"runtime_unavailable", "daemon_offline", "timeout"},
        safe_details=safe_details,
    )
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
    # Typed AgentRuntimeError subclasses map to their precondition/integrity codes.
    for exc_type, code in (
        (InvalidTransition, "invalid_transition"),
        (StaleRun, "stale_run"),
        (ProofMissing, "proof_missing"),
        (StoreCorrupt, "store_corrupt"),
        (DefaultScopeReconciliationRequired, "default_scope_reconciliation_required"),
        (EventPayloadTooLarge, "event_payload_too_large"),
        (RuntimeRootMismatch, "wrong_runtime_root"),
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
        "goal_not_found": "Run `hermes harness goal list --json` and retry with a listed id.",
        "workspace_not_found": "Run `hermes harness workspace list --json` and retry with a listed id.",
        "realm_not_found": "Run `hermes harness realm list --json` and retry with a listed id.",
        "default_scope_reconciliation_required": "Run `hermes harness realm default-scope --dry-run --json`; no identities will change without explicit approval.",
        "sync_conflict": "Resolve conflicts in the realm sync git repo, then retry.",
        "sync_behind": "Run `hermes harness realm sync pull <realm> --json` before publishing.",
        "sync_secret_excluded": "Remove secrets/state from the realm sync allowlist source before retrying.",
        "sync_remote_unreachable": "Check network/git remote availability and retry.",
        "sync_auth_failed": "Provide a fresh launcher-brokered credential via --credential-file or HERMES_REALM_SYNC_CREDENTIAL.",
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


def _require_yes(args, code: str = "confirmation_required") -> bool:
    if getattr(args, "yes", False) or getattr(args, "dry_run", False):
        return True
    _print_stage42(
        _error_envelope(code, "This destructive operation requires --yes.", retryable=False),
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
