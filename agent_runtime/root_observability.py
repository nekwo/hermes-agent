"""Stamp WHICH runtime root a ``--json`` envelope was answered from.

The property that makes root bugs survive investigation: a wrong root returns
a well-formed EMPTY answer — ``ok: true, count: 0`` — indistinguishable from
genuinely-empty data. Any diagnostic that resolves its own root cannot detect
a root bug; it always confirms health (the 2026-08-12 ambient chat-history
incident: the platform-default SHADOW runtime answered every probe cleanly).

So the envelope itself must carry the answer's frame of reference. This module
is the one attach point:

* ``resolution`` — :func:`agent_runtime.resolution.resolution_payload`:
  the resolved store root, which ladder rung won, and the trace.
* ``chat_scope`` (chat lanes only) —
  :meth:`agent_runtime.chat_session_scope.ChatSessionScope.payload`: which
  chat ``state.db`` the answer read, and how that head was named.
  ``source: "ambient_home"`` beside an empty result is the tell the incident
  lacked entirely.

Both blocks are DIAGNOSTIC and additive: a consumer renders them, never
branches on them, and attaching must never be able to take a verb down — a
failed resolution is reported as a typed ``error_kind`` value, not raised.

Enforced structurally by
``tests/hermes_cli/test_harness_json_root_observability.py``: a new
``--json``-emitting ``_cmd_*`` handler that ships without this block (and
without a written ledger reason) fails CI.
"""

from __future__ import annotations

from typing import Any

__all__ = ["attach_root_observability"]


def attach_root_observability(
    payload: dict[str, Any],
    *,
    chat_scope: bool = False,
    resolution: Any = None,
) -> dict[str, Any]:
    """Additively stamp ``resolution`` (and optionally ``chat_scope``).

    Mutates and returns *payload*. Existing keys are never overwritten — a
    producer that already carries its own resolution block keeps it. Never
    raises: an unresolvable block is attached as a typed error marker, because
    "this envelope cannot say which root answered" is itself the finding.

    ``resolution`` lets a handler that already resolved the runtime (e.g.
    ``doctor``) stamp from its OWN resolution object instead of deriving a
    second one for the same envelope.
    """

    if not isinstance(payload, dict):
        return payload
    if "resolution" not in payload:
        try:
            from .resolution import resolution_payload

            payload["resolution"] = resolution_payload(resolution)
        except Exception as exc:  # accounted, never raised
            payload["resolution"] = {
                "error_kind": "resolution_unavailable",
                "error": type(exc).__name__,
            }
    if chat_scope and "chat_scope" not in payload:
        try:
            from .chat_session_scope import resolve_process_chat_scope

            # The PROCESS frame of reference. A per-conversation stamp
            # would be a different fact, and lanes that own one (e.g.
            # persona-chat history) stamp it themselves — this never
            # overwrites an existing block.
            payload["chat_scope"] = resolve_process_chat_scope().payload()
        except Exception as exc:  # accounted, never raised
            payload["chat_scope"] = {
                "error_kind": "chat_scope_unavailable",
                "error": type(exc).__name__,
            }
    return payload
