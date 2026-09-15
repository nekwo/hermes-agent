"""Downstream explicit process completion delivery and mission-chat wait policy."""
from __future__ import annotations
import logging
import os
import threading
from typing import Optional, TYPE_CHECKING
if TYPE_CHECKING:
    from tools.process_registry import ProcessSession
logger = logging.getLogger("tools.process_registry")
MISSION_CHAT_WAIT_MAX_SECONDS = 600


def checkpoint_path():
    """Path to the crash-recovery checkpoint, resolved at call time."""

    from hermes_constants import get_hermes_background_work_home

    return get_hermes_background_work_home() / "processes.json"


def _configured_wait_ceiling() -> int:
    """``TERMINAL_TIMEOUT`` as an int, or the historical 180 s default."""

    try:
        return int(os.getenv("TERMINAL_TIMEOUT", "180"))
    except (ValueError, TypeError):
        return 180


def wait_ceiling_seconds() -> int:
    """The longest ``process wait`` this lane may block for.

    Two inputs, and the mission-chat one can only ever RAISE the answer:

    * ``TERMINAL_TIMEOUT`` — the deployment-wide configuration, unchanged, and
      the only input on every lane that is not mission-chat.
    * :data:`MISSION_CHAT_WAIT_MAX_SECONDS` — a FLOOR for the governed
      mission-chat lane, applied with ``max()`` so an operator who configured a
      longer ``TERMINAL_TIMEOUT`` keeps it and nobody's window is shortened.

    Lane identity comes from ``agent_runtime.terminal_envelope``'s run scope —
    the same ContextVar the terminal tool already resolves its envelope from, so
    a wait issued inside a mission-chat turn is recognised without a second
    notion of "which lane am I on". Any failure to resolve it (the import
    missing on a lean install, an odd scope object) degrades to the configured
    ceiling, i.e. to exactly today's behaviour.
    """

    ceiling = _configured_wait_ceiling()
    try:
        from agent_runtime.terminal_envelope import (
            LANE_MISSION_CHAT,
            current_terminal_envelope_scope,
        )

        scope = current_terminal_envelope_scope()
        on_lane = scope is not None and str(getattr(scope, "lane", "")) == LANE_MISSION_CHAT
    except Exception:  # pragma: no cover - defensive; a clamp must never fail a wait
        logger.debug("terminal envelope lane unavailable for the wait ceiling", exc_info=True)
        return ceiling
    return max(ceiling, MISSION_CHAT_WAIT_MAX_SECONDS) if on_lane else ceiling


class ProcessNotificationMixin:
    def restore_durable_completions(self) -> int:
        """Rehydrate durable pending delegation completions into the queue.

        Called explicitly, once, by the entry points that OWN a completion
        drain (the gateway, the interactive CLI, the TUI gateway, harness
        serve) — the same explicit-at-startup contract MCP discovery moved to
        for #16856. It used to run inside ``__init__``, which made it an
        IMPORT side effect of the module-scope singleton: any module that
        touched the tool tree — including read-only projections — opened (and
        created) ``state.db`` and ran ``recover_abandoned_delegations()``, a
        real mutation, before a single verb executed. See
        ``docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/eager-tool-discovery-audit-2026-08-09.md``.

        Idempotent per process: a second call returns 0 without touching the
        store, because re-running the restore would re-enqueue every pending
        completion (delivery attempts are only deduped at claim time). The
        guard is set before the restore runs, so a failed restore is warned
        about and NOT retried — the same once-at-startup semantics the
        constructor provided.

        Returns the number of completions enqueued (0 on the guarded or
        failed path).
        """
        with self._durable_restore_lock:
            if self._durable_completions_restored:
                return 0
            self._durable_completions_restored = True
        try:
            from tools.async_delegation import restore_undelivered_completions
            return restore_undelivered_completions(self.completion_queue)
        except Exception as exc:
            logger.warning("Could not restore async delegation completions: %s", exc)
            return 0

    def _completion_event_payload(self, session) -> dict:
        from tools.process_registry import _output_tail, _redact_process_result
        result = {
            "type": "completion", "session_id": session.id,
            "session_key": session.session_key, "task_id": session.task_id,
            "owner_task_id": session.owner_task_id or session.task_id,
            "command": session.command, **self._exit_fields(session),
            "output": _output_tail(session, 2000), "started_at": session.started_at,
            **({"handoff_note": session.handoff_note} if session.handoff_note else {}),
        }
        return _redact_process_result(result)

    @staticmethod
    def _stamp_notify_routing(event: dict, row: dict) -> None:
        """Carry the request's identity onto the completion it answers.

        ``origin_ui_session_id`` is the key the serve delivery drain resolves a
        chat root from FIRST (``dispatch_delivery._chat_root_of_completion``),
        so stamping it is what makes an exit reach the thread that asked —
        positive knowledge recorded at request time, never drain-time guessing.
        The turn and instance ride along as provenance for the same reason the
        dispatch lane's ``requested_by`` carries its dispatch id: this row is
        the last place they are knowable.
        """

        root = str(row.get("chat_session_id") or "")
        event["notify_requested"] = True
        if root:
            event["origin_ui_session_id"] = root
        turn_id = str(row.get("turn_id") or "")
        if turn_id:
            event["notify_turn_id"] = turn_id
        instance = str(row.get("persona_instance_id") or "")
        if instance:
            event["notify_persona_instance_id"] = instance

    @staticmethod
    def _notify_request_row(session_id: str) -> Optional[dict]:
        """The pending notify row for a session, or None. Never raises.

        Called from the reader thread at every process exit, so a store fault
        degrades to "nobody asked" rather than killing the reaper mid-move.
        """

        try:
            import tools.process_notify_store as notify_store

            return notify_store.pending_notify_request(session_id)
        except Exception:
            logger.debug("process notify lookup failed for %s", session_id, exc_info=True)
            return None

    @staticmethod
    def _settle_notify_request(session_id: str, *, fired: bool, detail: str = "") -> None:
        try:
            import tools.process_notify_store as notify_store

            notify_store.settle_notify_request(
                session_id,
                state=notify_store.STATE_FIRED if fired else notify_store.STATE_DROPPED,
                detail=detail,
            )
        except Exception:
            logger.debug("process notify settle failed for %s", session_id, exc_info=True)

    @staticmethod
    def _notify_owner(chat_session_id: str) -> Optional[tuple]:
        """``(persona_id, persona_instance_id)`` owning a chat root, or None.

        Resolved through the runtime's ONE answer to "whose work is this"
        (``persona_assignments.chat_session_owner_persona``) — the same call the
        dispatch delivery drain proves ownership with, never a second
        derivation.
        """

        try:
            from agent_runtime.persona_assignments import chat_session_owner_persona

            owner = chat_session_owner_persona(chat_session_id)
        except Exception:
            logger.debug("persona owner lookup failed for %s", chat_session_id, exc_info=True)
            return None
        return owner

    @classmethod
    def _notify_target_is_live(cls, row: dict) -> bool:
        """Whether the instance that asked still owns the root it named.

        Fail OPEN on an unresolvable lookup and CLOSED on a resolved absence,
        which is the same asymmetry ``dispatch_store``'s restore sweep holds: an
        unreadable probe is absence of proof, a store that answers "no owner" is
        proof of absence. Only the second drops the row.
        """

        root = str(row.get("chat_session_id") or "")
        if not root:
            return True
        try:
            owner = cls._notify_owner(root)
        except Exception:  # pragma: no cover - defensive; the reaper must survive
            return True
        if owner is None:
            return False
        recorded = str(row.get("persona_instance_id") or "")
        if recorded and str(owner[1]) != recorded:
            # The root resolves, but to somebody else — the requester was
            # retired and its thread re-owned. Same verdict, different cause.
            return False
        return True

    def _notify_chat_root(self, session: ProcessSession) -> str:
        """The persona chat root a notify request would be delivered into.

        Three sources, most authoritative first, and every one of them is
        POSITIVE knowledge rather than a guess:

        * the run's bound session key (``persona_chat_continuity.
          chat_root_session_key_scope`` puts the chat root into
          ``tools.approval``'s ContextVar for the whole persona run);
        * the terminal envelope scope's session id, for a run that bound the
          envelope but not the key;
        * the session's own ``session_key``, which the terminal tool stamped at
          spawn from the first of those.

        Empty when none of them names a persona chat root — which is how a
        gateway, CLI or worker-lane caller is refused instead of delivered into
        somebody else's thread.
        """

        candidates = []
        try:
            from tools.approval_context import get_current_session_key

            candidates.append(get_current_session_key(default="") or "")
        except Exception:
            pass
        try:
            from agent_runtime.terminal_envelope import current_terminal_envelope_scope

            scope = current_terminal_envelope_scope()
            candidates.append(str(getattr(scope, "session_id", "") or ""))
        except Exception:
            pass
        candidates.append(str(session.session_key or ""))
        for candidate in candidates:
            if candidate.startswith("persona_chat_"):
                return candidate
        return ""

    @staticmethod
    def _notify_turn_id(chat_session_id: str) -> str:
        """The in-flight turn id on a chat root, or "". Provenance only.

        Read from the turn journal the same way the delivery drain's idle probe
        reads it, so "which turn asked" is answered by the record rather than by
        a value this lane would have to invent and thread through.
        """

        try:
            from agent_runtime.mission_chat_turns import (
                INFLIGHT_TURN_STATES,
                mission_chat_turn_records,
            )

            for record in mission_chat_turn_records(session_id=chat_session_id) or []:
                if str((record or {}).get("state") or "") in INFLIGHT_TURN_STATES:
                    return str((record or {}).get("turn_id") or "")
        except Exception:
            logger.debug("notify turn lookup failed for %s", chat_session_id, exc_info=True)
        return ""

    def notify_on_exit(self, session_id: str) -> dict:
        """Arm a process-exit delivery for a background session.

        The agent contract this exists for: fire a long verb in the background,
        call this, and END THE TURN. On exit the completion is published to the
        queue the serve delivery drain already consumes, and the agent's next
        turn arrives carrying the receipt — the same road a detached
        ``agent_chat_send`` reply travels back.

        Idempotent, and immediate when the process has ALREADY exited: an exit
        that happened between the verb and this call must not become a lost
        wake-up, so the completion is published right here instead of waiting
        for a reaper that has already run.

        A process that never exits delivers nothing. That is deliberate — the
        turn wall/budget system is the guard, and an agent that would rather
        block is bounded by :data:`MISSION_CHAT_WAIT_MAX_SECONDS`.
        """

        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}

        root = self._notify_chat_root(session)
        if not root:
            return {
                "status": "unavailable",
                "error": (
                    "process-exit delivery needs a persona chat thread to deliver "
                    "into, and this run is not on one. Use process wait with a "
                    "timeout instead."
                ),
            }
        owner = self._notify_owner(root)
        if owner is None:
            return {
                "status": "unavailable",
                "error": (
                    f"chat root {root} does not resolve to a live persona instance, "
                    "so a delivery turn would have nowhere to land. Use process "
                    "wait with a timeout instead."
                ),
            }
        persona_id, instance_id = str(owner[0]), str(owner[1])

        import tools.process_notify_store as notify_store

        row, created = notify_store.record_notify_request(
            session_id=session.id,
            chat_session_id=root,
            persona_instance_id=instance_id,
            persona_id=persona_id,
            turn_id=self._notify_turn_id(root),
            command=session.command,
        )
        result = {
            "session_id": session.id,
            "command": session.command,
            "chat_session_id": root,
            "persona_id": persona_id,
            "persona_instance_id": instance_id,
            "turn_id": row.get("turn_id", ""),
            "already_armed": not created,
        }
        if not session.exited:
            result["status"] = "armed"
            result["delivery"] = "on_exit"
            result["note"] = (
                "End your turn. When this process exits you will receive a new turn "
                "carrying its exit code and output tail."
            )
            return result

        # Already finished. Deliver now unless this row already did.
        if row.get("state") != notify_store.STATE_PENDING:
            result["status"] = "exited"
            result["delivery"] = "already_queued"
            result["exit_code"] = session.exit_code
            return result
        event = self._completion_event_payload(session)
        self._stamp_notify_routing(event, row)
        self.completion_queue.put(event)
        self._settle_notify_request(session.id, fired=True)
        result["status"] = "exited"
        result["delivery"] = "queued"
        result["exit_code"] = session.exit_code
        result["note"] = (
            "The process had already exited; its completion is queued for delivery."
        )
        return result

