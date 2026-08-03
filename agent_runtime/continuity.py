from __future__ import annotations

import hashlib
from typing import Any

from hermes_time import now

from .chat_live_log import mirrored_persona_chat_append
from .events import EventLog
from .models import Event
from .child_events import emit_child_returned
from .config import load_root_runtime_config
from .persona_assignments import PersonaInstanceStore, safe_assignment_text

SUMMARY_LIMIT = 1200
REF_LIMIT = 8
REF_TEXT_LIMIT = 120


def return_summary_to_parent_session(
    persona_instance_id: str,
    *,
    parent_session_id: str,
    summary: str,
    proof_ids: list[str] | None = None,
    artifact_refs: list[str] | None = None,
    event_log: EventLog | None = None,
    child_events_enabled: bool | None = None,
) -> dict[str, Any]:
    """Post a bounded child summary into the parent's chat session.

    This is the R3 continuity primitive: return a distilled summary + refs, not
    the child transcript. The SessionDB row is an assistant message because it is
    information produced by the runtime/child for the parent to consume.
    """

    safe_instance = safe_assignment_text(persona_instance_id, limit=160)
    safe_session = safe_assignment_text(parent_session_id, limit=200)
    safe_summary = safe_assignment_text(summary, limit=SUMMARY_LIMIT)
    if not safe_instance:
        raise ValueError("persona_instance_id is required")
    if not safe_session:
        raise ValueError("parent_session_id is required")
    if not safe_summary:
        raise ValueError("summary is required")

    proofs = _bounded_refs(proof_ids or [])
    artifacts = _bounded_refs(artifact_refs or [])
    message = _format_parent_message(safe_instance, safe_summary, proof_ids=proofs, artifact_refs=artifacts)
    client_message_id = _client_message_id(safe_instance, safe_session, safe_summary)

    session_db = _session_db()
    session_db.ensure_session(safe_session, source="agent_runtime_persona_chat")
    # Wrapped in the live-log seam like every other explicit persona-chat
    # append: "the child finished, here is the distilled result" is exactly the
    # row a head agent grepping the parent thread's log is looking for, and this
    # lane writing straight to SessionDB is how it went missing from the mirror
    # in the first place.
    with mirrored_persona_chat_append(
        session_db=session_db,
        session_id=safe_session,
        role="assistant",
        text=message,
        client_message_id=client_message_id,
    ):
        message_id = session_db.append_message(
            session_id=safe_session,
            role="assistant",
            content=message,
            platform_message_id=client_message_id,
        )

    store = PersonaInstanceStore(event_log=event_log)
    instance = store.get(safe_instance)
    instance.returned_to = safe_session
    store.update(instance)

    log = event_log or EventLog()
    log.append(
        Event(
            ts=now(),
            type="steer.returned",
            task_id=None,
            run_id=None,
            persona_id=instance.persona_id,
            session_id=safe_session,
            payload={
                "action_id": f"steer:{safe_instance}:{safe_session}:return",
                "verb": "return",
                "source_node_id": safe_instance,
                "target_node_id": safe_session,
                "result": "summary_returned",
                "persona_instance_id": safe_instance,
                "proof_ids": proofs,
                "artifact_refs": artifacts,
            },
        )
    )
    if child_events_enabled is None:
        try:
            supervision = getattr(load_root_runtime_config(), "supervision", None)
            child_events_enabled = bool(getattr(supervision, "child_events_enabled", False))
        except Exception:
            child_events_enabled = False
    if child_events_enabled:
        emit_child_returned(
            child=instance,
            summary=safe_summary,
            proof_ids=proofs,
            artifact_refs=artifacts,
            event_log=log,
        )

    return {
        "ok": True,
        "capability_id": "persona.instance.return_summary",
        "persona_instance_id": safe_instance,
        "parent_session_id": safe_session,
        "returned_to": safe_session,
        "message_id": message_id,
        "summary_chars": len(safe_summary),
        "proof_ids": proofs,
        "artifact_refs": artifacts,
        "bounded": True,
    }


def _session_db():
    """The OPERATOR-visible chat database — never the ambient ``HERMES_HOME`` one.

    A child returning a distilled summary to its parent runs precisely under a
    persona profile-home override, so a bare ``SessionDB()`` wrote the summary
    into the CHILD's profile database (where the projection never reads it) and
    minted a phantom parent-session row there. A phantom row is worse than a
    missing one: a misrouted presence probe reading that database answers
    "present" for a session the operator cannot see. (Defect D2 in
    ``docs/agent-runtime-harness/chat-session-presence-authority.md``.)
    """

    from .chat_session_scope import open_chat_session_db

    db = open_chat_session_db()
    if db is None:
        raise RuntimeError("chat session database unavailable")
    return db


def _bounded_refs(values: list[str]) -> list[str]:
    refs: list[str] = []
    for value in values:
        safe = safe_assignment_text(value, limit=REF_TEXT_LIMIT)
        if safe and safe not in refs:
            refs.append(safe)
        if len(refs) >= REF_LIMIT:
            break
    return refs


def _format_parent_message(child_id: str, summary: str, *, proof_ids: list[str], artifact_refs: list[str]) -> str:
    lines = [
        f"Continuity return from {child_id}:",
        summary,
    ]
    if proof_ids:
        lines.append("Proof refs: " + ", ".join(proof_ids))
    if artifact_refs:
        lines.append("Artifact refs: " + ", ".join(artifact_refs))
    return "\n".join(lines)


def _client_message_id(child_id: str, session_id: str, summary: str) -> str:
    digest = hashlib.sha256(f"{child_id}\0{session_id}\0{summary}".encode("utf-8")).hexdigest()[:16]
    return f"continuity_return_{digest}"
