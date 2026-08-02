from __future__ import annotations

import hashlib
import json
import logging
import shutil
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .errors import AgentRuntimeError
from .events import EventLog
from .models import (
    PERSONA_INSTANCE_ID_PREFIX,
    AgentPersona,
    Event,
    PersonaAssignment,
    PersonaInstance,
    looks_like_persona_instance_id,
)
from .personas import profile_chat_toolsets
from .serde import from_jsonable, to_jsonable
from .state_patches import emit_persona_instance_patch, emit_persona_instance_remove
from .states import WorkerSessionState
from .tool_visibility import (
    permission_state_for_persona,
    resolve_tool_visibility,
    turn_tool_context_for_persona,
)
from .tool_permissions import permission_options_for_chat

TERMINAL_ASSIGNMENT_STATES = frozenset({"completed", "blocked", "cancelled"})
_RELEASABLE_OWNER_TASK_STATES = frozenset({"done", "cancelled", "failed"})

# Modes that only exist because the instance is holding a chat open; once its
# last chat pointer is cleared the row demotes back to a plain configured agent.
_CHAT_MODES = frozenset({"chat", "free_floating"})

# Typed reasons carried on ``persona_instance.chat_binding_cleared``.
_BINDING_REPAIR_REASON = "session_missing_from_session_db"
CHAT_BINDING_CLEARED_REASON_DELETED = "chat_deleted"


def _session_presence_probe(session_db: Any | None = None) -> tuple[Any | None, str | None]:
    """Return ``(probe, skip_reason)`` for chat-session existence checks.

    The probe answers ``"present" | "absent" | "unknown"`` — tri-state on
    purpose. ``get_session`` swallowing an error and returning ``None`` would
    make an unreadable database indistinguishable from a deleted chat, and a
    repair built on that would reap live pointers on a transient failure.

    Three preconditions must hold before ANY binding may be called stale, and
    all fail closed (probe ``None`` + a typed skip reason):

    * when the database is self-resolved, the head home must be EXPLICITLY
      named by this process — relay context or ``HERMES_HEAD_HOME``
      (``head_home_not_authoritative``). ``chat_session_scope`` otherwise falls
      back to the shared runtime root's recorded head pointer and, failing
      that, to the ambient ``HERMES_HOME``; both are fine for reading or
      minting a transcript and neither may decide that a live binding is
      stale. Without that rule a maintenance verb run under a profile home
      probes that profile's database and reads every operator chat as absent —
      a POPULATED wrong database sails straight past the empty-DB guard (live
      2026-07-25: a reconcile under the alice profile home cleared 10 live
      chat bindings on a false ``session_missing_from_session_db`` verdict).
      A caller that passes ``session_db`` explicitly owns its own routing;
    * a database must resolve at all (``session_db_unavailable``);
    * it must positively enumerate at least one session (``session_db_empty``).
      A zero-row database is indistinguishable from a fresh or misrouted
      ``HERMES_HOME``, and "the home moved" must never present as "every chat
      was deleted".
    """

    db = session_db
    if db is None:
        try:
            from .chat_session_scope import resolve_chat_session_scope

            from .persona_chat_history import _default_session_db

            # DESTRUCTIVE posture: a head RECORDED for the shared runtime root
            # is enough to read or mint a transcript, and deliberately NOT
            # enough to clear a live binding. This lane still requires that THIS
            # process named the head — byte-identical to the shipped 8c3942a21
            # guard. The acquisition itself stays on the shared
            # ``_default_session_db`` delegate, so there is still exactly one.
            if not resolve_chat_session_scope().explicitly_named:
                return None, "head_home_not_authoritative"
            db = _default_session_db()
        except Exception:
            db = None
    if db is None:
        return None, "session_db_unavailable"
    try:
        sample = db.list_sessions_rich(limit=1, include_archived=True)
    except Exception:
        return None, "session_db_unavailable"
    if not sample:
        return None, "session_db_empty"

    def probe(session_id: str) -> str:
        try:
            row = db.get_session(session_id)
        except Exception:
            return "unknown"
        return "present" if row else "absent"

    return probe, None


def _owning_task_release_state(task_id: str) -> str | None:
    """Terminal state name of the owning task, ``"archived"`` when the task file
    has left the live store, or None when the task is live (or unreadable)."""
    try:
        path = paths.task_path(task_id)
        if not path.exists():
            return "archived"
        state = str(json.loads(path.read_text(encoding="utf-8")).get("state") or "").strip().lower()
    except Exception:
        return None
    return state if state in _RELEASABLE_OWNER_TASK_STATES else None


def _persona_instance_owner_release_state(instance: PersonaInstance) -> str | None:
    owners = {
        owner
        for owner in (
            safe_optional_token(instance.current_task_id),
            safe_optional_token(instance.goal_id),
        )
        if owner
    }
    if not owners:
        return "taskless"
    states: list[str] = []
    for owner in owners:
        state = _owning_task_release_state(owner)
        if state is None:
            return None
        states.append(state)
    if "done" in states:
        return "done"
    if "cancelled" in states:
        return "cancelled"
    if "failed" in states:
        return "failed"
    return "archived"
ACTIVE_ASSIGNMENT_STATES = frozenset({"queued", "assigned", "running", "waiting_on_tool", "waiting_on_proof", "needs_input"})
# S56 removed ``_worker_carries_live_binding``. It decided whether a WORKER row
# could stamp its ``task_bound`` binding onto a persona instance during
# derivation. Both of its inputs are gone: the worker session store was deleted
# (nothing can write a worker row, and the live runtime root carries no
# ``worker_sessions/`` directory at all), and ``build_snapshot`` had already
# been passing a ``workers = []`` literal into the derivation for two waves — so
# on the live tree the "carries" branch could never be taken and every persona
# fell through to the configured/idle reset. That reset is now unconditional in
# ``PersonaInstanceStore.ensure_for_personas``; the 2026-07-08 regression this
# predicate was written to fix (dead workers re-stamping a settled mission onto
# the instance) cannot recur, because there are no worker rows to re-stamp from.


class StaleModelOverrideWrite(AgentRuntimeError):
    """A model-override write carried an ``issued_at`` older than (or equal to)
    the last applied model write for the instance. The newer value wins; the
    stale intent must never be applied silently (supersede guard, mirroring the
    Stage 13 scope-flip fix)."""

    def __init__(self, instance: PersonaInstance, *, issued_at: datetime, applied_issued_at: datetime):
        super().__init__("model_override_write_superseded")
        self.instance = instance
        self.issued_at = issued_at
        self.applied_issued_at = applied_issued_at


class PersonaInstanceRetireError(AgentRuntimeError):
    """A persona-instance retire (end-of-life) was refused by a state guard.

    ``code`` is the machine-readable typed reason every surface (CLI JSON,
    launcher bridge) keys on — never a bare string:

    - ``not_found`` — no such live row.
    - ``canonical_persona_channel`` — the row IS the persona/profile's canonical
      operator channel (the global singleton ``persona_instance_id_for(persona)``).
      Its retirement is the queued workspace-scoping redesign, not this verb.
    - ``instance_active`` — a live run/worker still resolves for the instance;
      never archive a working agent.
    - ``assignment_active`` — an active persona assignment is bound to the
      instance; complete/close it first.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        persona_instance_id: str,
        detail: dict[str, Any] | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.message = message
        self.persona_instance_id = persona_instance_id
        self.detail = detail or {}


class RetiredPersonaInstanceError(AgentRuntimeError):
    """A saved chat tried to recreate an instance whose placement ended.

    Retirement preserves the row under ``persona_instances_archive`` so chat
    history remains inspectable, but the archived row is also the durable
    end-of-life marker. Reopening that old session must never mint the row back
    into the live roster.
    """

    def __init__(self, persona_instance_id: str, *, archive_path: Path):
        super().__init__("retired_persona_instance")
        self.code = "retired_persona_instance"
        self.persona_instance_id = persona_instance_id
        self.archive_path = archive_path


def _retired_persona_instance_archive_path(
    persona_instance_id: str,
) -> Path | None:
    """Newest explicit-retire archive row for ``persona_instance_id``.

    Only ``*_retire`` batches are tombstones. Reconcile/prune archives answer
    different lifecycle questions and must not make a future legitimate mint
    impossible. Exact child paths avoid treating the instance id as a glob.
    """
    archive_root = paths.persona_instances_archive_dir()
    if not archive_root.exists():
        return None
    archive_dirs = sorted(
        (
            candidate
            for candidate in archive_root.iterdir()
            if candidate.is_dir() and candidate.name.endswith("_retire")
        ),
        key=lambda candidate: candidate.name,
        reverse=True,
    )
    for archive_dir in archive_dirs:
        candidate = archive_dir / f"{persona_instance_id}.json"
        if candidate.is_file():
            return candidate
    return None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _safe_model_override_text(value: str, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must not be empty")
    if len(text) > 200:
        raise ValueError(f"{field_name} exceeds 200 characters")
    if any(ord(ch) < 0x20 for ch in text):
        raise ValueError(f"{field_name} contains control characters")
    return text


def _model_supports_reasoning_effort(model_id: str | None) -> bool:
    """True when the model exposes reasoning-effort control (offline id-heuristic).

    Reuses the canonical Copilot/GPT-5/o-series id heuristic with no catalog or
    api_key so this stays a cheap, network-free check safe to run for every
    instance in a snapshot. A resolution failure degrades to ``False`` (control
    hidden) rather than raising inside the projection.
    """
    if not str(model_id or "").strip():
        return False
    try:
        from hermes_cli.models import github_model_reasoning_efforts

        return bool(github_model_reasoning_efforts(model_id))
    except Exception:
        return False


def _normalize_reasoning_effort_override(value: str) -> str | None:
    """Normalize a reasoning-effort override to a stored value or ``None``.

    Empty clears the override (inherit the runtime default). ``"none"`` (thinking
    off) and every level in ``hermes_constants.VALID_REASONING_EFFORTS`` are
    accepted; anything else raises ``ValueError``.
    """
    from hermes_constants import VALID_REASONING_EFFORTS

    text = str(value or "").strip().lower()
    if not text:
        return None
    if text == "none" or text in VALID_REASONING_EFFORTS:
        return text
    raise ValueError(
        f"invalid reasoning_effort: {value!r} (expected one of none, "
        f"{', '.join(VALID_REASONING_EFFORTS)})"
    )


@dataclass(slots=True)
class PersonaAssignmentSpec:
    persona_id: str
    kind: str
    title: str
    message: str
    created_by: str = "harness"
    persona_instance_id: str | None = None
    state: str = "queued"
    goal_id: str | None = None
    stage_id: str | None = None
    operation_id: str | None = None
    repo_bundle_id: str | None = None
    repo: str | None = None
    affected_paths: list[str] = field(default_factory=list)
    proof_targets: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    allowed_decisions: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    evidence_kind: str | None = None
    production_proof_eligible: bool | None = None
    archive_scope: str | None = None
    client_message_id: str | None = None


class PersonaInstanceStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def create_free_floating(self, persona_or_template: AgentPersona | str) -> PersonaInstance:
        persona_id, role, display_name, profile_id = _free_floating_identity(persona_or_template)
        instance_id = persona_instance_id_for(persona_id)
        try:
            instance = self.get(instance_id)
        except Exception:
            instance = PersonaInstance(
                id=instance_id,
                persona_id=persona_id,
                role=role,
                display_name=display_name,
                profile_id=profile_id,
                runtime_root=str(paths.store_root()),
                state=WorkerSessionState.IDLE,
                mode="free_floating",
                updated_at=now(),
            )
            self._write(instance)
            self._event("persona_instance.created", instance, {"mode": "free_floating"})
            return self.get(instance_id)
        changed = False
        for attr, value in {
            "persona_id": persona_id,
            "role": role,
            "display_name": display_name,
            "profile_id": profile_id,
            "runtime_root": str(paths.store_root()),
            "mode": "free_floating",
        }.items():
            if getattr(instance, attr) != value:
                setattr(instance, attr, value)
                changed = True
        if changed:
            instance.updated_at = now()
            self._write(instance)
        return self.get(instance_id)

    def ensure_for_persona(self, persona: AgentPersona) -> PersonaInstance:
        instance_id = persona_instance_id_for(persona.id)
        try:
            existing = self.get(instance_id)
        except Exception:
            ts = now()
            instance = PersonaInstance(
                id=instance_id,
                persona_id=persona.id,
                role=str(persona.role),
                display_name=persona.display_name,
                profile_id=persona.hermes_profile,
                runtime_root=str(paths.store_root()),
                state=WorkerSessionState.IDLE,
                updated_at=ts,
            )
            self._write(instance)
            self._event("persona_instance.created", instance, {})
            return instance
        changed = False
        if existing.display_name != persona.display_name:
            existing.display_name = persona.display_name
            changed = True
        if existing.profile_id != persona.hermes_profile:
            existing.profile_id = persona.hermes_profile
            changed = True
        if changed:
            existing.updated_at = now()
            self._write(existing)
        return self.get(instance_id)

    def ensure_for_goal(self, persona: AgentPersona, *, goal_id: str, spawned_by: str | None, placement_id: str | None = None) -> PersonaInstance:
        normalized_goal = safe_optional_token(goal_id)
        placement = safe_assignment_token(placement_id) or safe_assignment_token(f"{normalized_goal}:{persona.id}")
        instance_id = persona_instance_id_for_placement(placement)
        try:
            instance = self.get(instance_id)
        except Exception:
            ts = now()
            instance = PersonaInstance(
                id=instance_id,
                persona_id=persona.id,
                role=str(persona.role),
                display_name=persona.display_name,
                profile_id=persona.hermes_profile,
                runtime_root=str(paths.store_root()),
                state=WorkerSessionState.IDLE,
                updated_at=ts,
            )
            self._write(instance)
            self._event("persona_instance.created", instance, {"mode": "task_bound", "placement_id": placement})
        changed = False
        normalized_spawned_by = safe_optional_token(spawned_by)
        # ``spawned_by`` is retained as the provenance/primary-parent scalar and
        # can legitimately be a non-instance principal (the operator adds an
        # instance ⇒ spawned_by="operator"). The authoritative parent SET is
        # STEERING, so it is seeded from the spawn parent ONLY when that parent is
        # instance-shaped; a principal is never a steering parent. Seeding a
        # principal here was the writer that made the HUD render "steered by
        # operator" for the add-instance/occupied-chat mint.
        seed_parents = (
            [normalized_spawned_by]
            if looks_like_persona_instance_id(normalized_spawned_by)
            else []
        )
        updates = {
            "mode": "task_bound",
            "current_task_id": normalized_goal,
            "goal_id": normalized_goal,
            "spawned_by": normalized_spawned_by,
            # Kept in sync with the (instance-shaped) spawn parent at creation, so
            # the on-disk record is self-consistent — not only healed by the
            # read-time __post_init__ backfill (which now applies the same guard).
            "steered_by": seed_parents,
        }
        for attr, value in updates.items():
            if getattr(instance, attr) != value:
                setattr(instance, attr, value)
                changed = True
        if changed:
            instance.updated_at = now()
            self._write(instance)
            self._event(
                "persona_instance.attributed",
                instance,
                {
                    "goal_id": instance.goal_id,
                    "spawned_by": instance.spawned_by,
                    # Preserve the raw provenance in the durable log even when it
                    # is a principal that does not enter the steering set.
                    "steered_by": list(instance.steered_by),
                },
            )
        return self.get(instance.id)

    def steer(
        self,
        persona_instance_id: str,
        *,
        parent_instance_id: str | None,
        goal_id: str | None = None,
        detach: bool = False,
    ) -> PersonaInstance:
        """Back-compat single-parent re-route (Stage 77).

        Preserves the original ``--parent`` semantics EXACTLY: replace the whole
        steering set with the one given parent, or clear it on ``detach``. New
        multi-parent callers use :meth:`set_parents` / :meth:`add_parent` /
        :meth:`remove_parent` / :meth:`detach_parents`. This is a re-route (a
        STEER verb, ungated per 76D.3), never a create/kill.
        """
        if detach:
            return self.detach_parents(persona_instance_id)
        normalized_parent = safe_optional_token(parent_instance_id)
        if not normalized_parent:
            raise ValueError("parent_instance_id is required unless detach is set")
        return self.set_parents(persona_instance_id, [normalized_parent], goal_id=goal_id)

    def set_parents(
        self,
        persona_instance_id: str,
        parent_instance_ids: list[str],
        *,
        goal_id: str | None = None,
    ) -> PersonaInstance:
        """Declaratively REPLACE a child's steering-parent set (fan-in).

        The Launcher graph-save reconciler asserts the desired set per child
        with this; it is idempotent (re-asserting the same set is a no-op) and
        the single write path for the persisted living-graph wiring. An empty
        set detaches the child (standalone owner).
        """
        normalized = _dedupe_tokens(parent_instance_ids)
        if not normalized:
            return self.detach_parents(persona_instance_id)
        return self._apply_steer_edges(persona_instance_id, normalized, goal_id=goal_id)

    def add_parent(
        self,
        persona_instance_id: str,
        parent_instance_id: str,
        *,
        goal_id: str | None = None,
    ) -> PersonaInstance:
        """Idempotently ADD one parent to a child's steering set (set-union)."""
        normalized_parent = safe_optional_token(parent_instance_id)
        if not normalized_parent:
            raise ValueError("parent_instance_id is required")
        instance = self.get(persona_instance_id)
        desired = list(instance.steered_by)
        if normalized_parent not in desired:
            desired.append(normalized_parent)
        return self._apply_steer_edges(persona_instance_id, desired, goal_id=goal_id, _instance=instance)

    def remove_parent(self, persona_instance_id: str, parent_instance_id: str) -> PersonaInstance:
        """Remove ONE parent from a child's steering set (detach-one).

        Removing the last parent detaches the child (standalone owner).
        """
        normalized_parent = safe_optional_token(parent_instance_id)
        instance = self.get(persona_instance_id)
        desired = [pid for pid in instance.steered_by if pid != normalized_parent]
        if not desired:
            return self.detach_parents(persona_instance_id)
        return self._apply_steer_edges(persona_instance_id, desired, goal_id=instance.goal_id, _instance=instance)

    def detach_parents(self, persona_instance_id: str) -> PersonaInstance:
        """Clear a child's entire steering set (detach-all) → standalone owner."""
        instance = self.get(persona_instance_id)
        removed = list(instance.steered_by)
        updates: dict[str, Any] = {
            "steered_by": [],
            "spawned_by": None,
            "goal_id": None,
            "current_task_id": None,
        }
        if instance.mode == "task_bound":
            updates["mode"] = "configured"
        self._commit_steer(instance, updates, added=[], removed=removed, detached=True)
        return self.get(instance.id)

    def clear_parents(self, persona_instance_id: str) -> PersonaInstance:
        """Clear a child's steering set WITHOUT leaving its mission.

        The flow-doc reconcile's "drawn standalone" verb: an operator's chart
        states who steers whom — never goal membership. [detach_parents] above
        is a different statement ("leave the mission": it also strips goal_id /
        current_task_id and task-bound mode), and using it for a chart ingest
        would unbind a root agent from its live goal. Same single write path
        ([_commit_steer]) and event as every other steer mutation."""

        instance = self.get(persona_instance_id)
        removed = list(instance.steered_by)
        self._commit_steer(
            instance,
            {"steered_by": [], "spawned_by": None},
            added=[],
            removed=removed,
            detached=True,
        )
        return self.get(instance.id)

    def repair_non_instance_steering(
        self,
        persona_instance_id: str | None = None,
        *,
        apply: bool = True,
    ) -> dict[str, Any]:
        """Strip non-instance principals out of steering fields (evented, dry-run aware).

        A steering-parent SET (``steered_by``) and its mirror (``spawned_by``)
        may hold ONLY persona-instance ids. A legacy mint seeded the operator
        principal into them (``steered_by=["operator"]`` / ``spawned_by=
        "operator"``), which the HUD then rendered as a phantom "steered by
        operator" edge. This removes the non-instance entries surgically:

        - ``steered_by`` keeps only its instance-shaped parents;
        - a NON-instance ``spawned_by`` is re-pointed at the surviving primary
          parent (``steered_by[0]``) when one remains, else cleared — restoring
          the mirror invariant. An already instance-shaped ``spawned_by`` is left
          untouched (the ``__post_init__`` backfill self-heals it into a bare
          ``steered_by``), so a valid parent is never destroyed.

        Everything else on the row (mode, goal, session) is untouched — this is a
        steering repair, not a detach. Honors dry-run: with ``apply=False``
        nothing is written and no event is emitted (the on-disk row stays
        byte-identical); with ``apply=True`` each repaired row goes through the
        single steer write path (``_commit_steer`` → one
        ``persona_instance.steered`` event + state patch).
        ``persona_instance_id`` targets one row; ``None`` scans every row.
        """
        targets = (
            [self.get(persona_instance_id)]
            if persona_instance_id
            else self.list_all()
        )
        repairs: list[dict[str, Any]] = []
        for instance in targets:
            steered_before = list(instance.steered_by)
            kept = [p for p in steered_before if looks_like_persona_instance_id(p)]
            bogus_steered = [p for p in steered_before if not looks_like_persona_instance_id(p)]
            spawned = instance.spawned_by
            bogus_spawn = (
                spawned
                if (spawned and not looks_like_persona_instance_id(spawned))
                else None
            )
            if not bogus_steered and bogus_spawn is None:
                continue
            updates: dict[str, Any] = {"steered_by": kept}
            # Only rewrite the mirror when it is itself bogus; re-point it at the
            # surviving primary, or clear it when no instance parent remains.
            desired_spawn = spawned
            if bogus_spawn is not None:
                desired_spawn = kept[0] if kept else None
                updates["spawned_by"] = desired_spawn
            record = {
                "persona_instance_id": instance.id,
                "steered_by_before": steered_before,
                "steered_by_after": kept,
                "spawned_by_before": spawned,
                "spawned_by_after": desired_spawn,
                "removed_steered_by": bogus_steered,
                "removed_spawned_by": bogus_spawn,
            }
            repairs.append(record)
            if not apply:
                continue
            self._commit_steer(
                instance,
                updates,
                added=[],
                removed=bogus_steered,
                detached=not kept,
            )
        return {
            "applied": bool(apply),
            "dry_run": not apply,
            "repaired": repairs,
            "repaired_count": len(repairs),
        }

    def repair_missing_steering_references(
        self,
        *,
        apply: bool = True,
    ) -> dict[str, Any]:
        """Remove steering parents that no longer resolve to a live instance.

        JSON shape validation cannot catch a syntactically valid id whose row
        has been retired or reaped. This is the referential-integrity repair:
        it preserves every live parent and all child context, rewrites the
        ``spawned_by`` mirror, and emits the ordinary steering event when
        applied. Dry-run is side-effect free.
        """
        instances = self.list_all()
        live_ids = {instance.id for instance in instances}
        repairs: list[dict[str, Any]] = []
        for instance in instances:
            before = list(instance.steered_by)
            kept = [parent for parent in before if parent in live_ids]
            missing = [parent for parent in before if parent not in live_ids]
            spawned_before = instance.spawned_by
            spawned_missing = bool(
                spawned_before
                and looks_like_persona_instance_id(spawned_before)
                and spawned_before not in live_ids
            )
            if not missing and not spawned_missing:
                continue
            spawned_after = kept[0] if kept else None
            repairs.append(
                {
                    "persona_instance_id": instance.id,
                    "steered_by_before": before,
                    "steered_by_after": kept,
                    "spawned_by_before": spawned_before,
                    "spawned_by_after": spawned_after,
                    "missing_parent_ids": sorted(set(missing + ([spawned_before] if spawned_missing else []))),
                }
            )
            if not apply:
                continue
            self._commit_steer(
                instance,
                {"steered_by": kept, "spawned_by": spawned_after},
                added=[],
                removed=missing,
                detached=not kept,
            )
        return {
            "applied": bool(apply),
            "dry_run": not apply,
            "repaired": repairs,
            "repaired_count": len(repairs),
        }

    def clear_chat_session_binding(
        self,
        instance: PersonaInstance,
        *,
        session_id: str,
        reason: str,
    ) -> dict[str, Any] | None:
        """THE write path that unbinds one instance from a chat session.

        Nulls only the pointers that actually reference ``session_id``, demotes a
        conversational mode back to ``configured`` once the instance is left with
        no chat, persists once, and emits ``persona_instance.chat_binding_cleared``
        (store mutations always emit an event). Returns the repair record, or
        ``None`` when the instance never pointed at that session.

        Every unbind — the operator ``persona chat delete`` verb and the
        ``repair_missing_chat_session_bindings`` reconcile sweep — goes through
        here so a stale binding can never be cleared silently by one path and
        loudly by another.
        """

        target = safe_assignment_text(session_id, limit=200)
        if not target:
            return None
        cleared: list[str] = []
        if safe_assignment_text(instance.default_chat_session_id, limit=200) == target:
            instance.default_chat_session_id = None
            cleared.append("default_chat_session_id")
        if safe_assignment_text(instance.session_id, limit=200) == target:
            instance.session_id = None
            cleared.append("session_id")
        if not cleared:
            return None
        mode_before = instance.mode
        if (
            not instance.default_chat_session_id
            and not instance.session_id
            and (instance.mode or "").lower() in _CHAT_MODES
        ):
            instance.mode = "configured"
        updated = self.update(instance)
        payload = {
            "persona_id": updated.persona_id,
            "session_id": target,
            "cleared_fields": cleared,
            "mode_before": mode_before,
            "mode_after": updated.mode,
            "reason": reason,
        }
        self._event("persona_instance.chat_binding_cleared", updated, payload)
        return {"persona_instance_id": updated.id, **payload}

    def repair_missing_chat_session_bindings(
        self,
        *,
        apply: bool = True,
        session_db: Any | None = None,
    ) -> dict[str, Any]:
        """Clear chat-session bindings whose session SessionDB no longer has.

        A persona instance can outlive its chat: the operator deletes the
        conversation through a path that does not own the instance store (the
        generic ``hermes sessions delete``, a gateway/web delete, a scrub), and
        the pointer is left dangling. The snapshot's persona-chat projection is
        READ-ONLY, so it can only hide the row and account a ``session_not_in_db``
        drop — one permanent parity anomaly per orphan, forever. This is the
        write-path repair that retires them.

        Fail-safe by construction:

        * a binding is cleared ONLY on a positive "absent" answer from a
          positively-enumerating SessionDB; an unavailable, unreadable or empty
          database repairs nothing at all (see :func:`_session_presence_probe`),
          because a blind read must never reap a live pointer;
        * ``task_bound`` instances are skipped entirely — a mission turn runs in
          a session that lives in the run/event stream and is legitimately absent
          from the operator SessionDB;
        * an instance with a live worker/run binding is held;
        * ``apply=False`` is a pure report: no writes, no events.
        """

        probe, skip_reason = _session_presence_probe(session_db)
        if probe is None:
            return {
                "applied": False,
                "dry_run": not apply,
                "skipped": skip_reason,
                "repaired": [],
                "repaired_count": 0,
                "held": [],
                "held_count": 0,
            }

        repairs: list[dict[str, Any]] = []
        held: list[dict[str, Any]] = []
        for instance in self.list_all():
            pointers = {
                safe_assignment_text(instance.default_chat_session_id, limit=200),
                safe_assignment_text(instance.session_id, limit=200),
            }
            pointers.discard(None)
            if not pointers:
                continue
            if (instance.mode or "").lower() == "task_bound" or safe_optional_token(
                instance.current_task_id
            ):
                # Mission sessions live in the run/event stream, not SessionDB.
                continue
            missing = sorted(session_id for session_id in pointers if probe(session_id) == "absent")
            if not missing:
                continue
            if self._has_live_binding(instance):
                held.extend(
                    {
                        "persona_instance_id": instance.id,
                        "persona_id": instance.persona_id,
                        "session_id": session_id,
                        "reason": "active-binding",
                    }
                    for session_id in missing
                )
                continue
            for session_id in missing:
                if not apply:
                    repairs.append(
                        {
                            "persona_instance_id": instance.id,
                            "persona_id": instance.persona_id,
                            "session_id": session_id,
                            "cleared_fields": [
                                field_name
                                for field_name, value in (
                                    ("default_chat_session_id", instance.default_chat_session_id),
                                    ("session_id", instance.session_id),
                                )
                                if safe_assignment_text(value, limit=200) == session_id
                            ],
                            "reason": _BINDING_REPAIR_REASON,
                        }
                    )
                    continue
                record = self.clear_chat_session_binding(
                    instance,
                    session_id=session_id,
                    reason=_BINDING_REPAIR_REASON,
                )
                if record is not None:
                    repairs.append(record)
        return {
            "applied": bool(apply),
            "dry_run": not apply,
            "repaired": repairs,
            "repaired_count": len(repairs),
            "held": held,
            "held_count": len(held),
        }

    def _release_parent_references(self, parent_instance_id: str) -> list[str]:
        """Transactionally release every child backlink before owner removal."""
        released: list[str] = []
        for child in self.list_all():
            if child.id == parent_instance_id or parent_instance_id not in child.steered_by:
                continue
            kept = [parent for parent in child.steered_by if parent != parent_instance_id]
            if kept:
                self._apply_steer_edges(
                    child.id,
                    kept,
                    goal_id=child.goal_id,
                    _instance=child,
                )
            else:
                # Owner retirement changes graph topology, not the child's
                # mission membership. Preserve goal/task/mode context.
                self.clear_parents(child.id)
            released.append(child.id)
        return released

    def _apply_steer_edges(
        self,
        persona_instance_id: str,
        parent_instance_ids: list[str],
        *,
        goal_id: str | None,
        _instance: PersonaInstance | None = None,
    ) -> PersonaInstance:
        instance = _instance or self.get(persona_instance_id)
        # Resolve every parent to the id of the row actually on disk BEFORE
        # persisting, so id-scheme drift (e.g. persona_personainst_x) can never
        # enter the stored steering set. Storing a drifted parent id would make
        # the next snapshot re-emit the drift, and the Launcher graph edge (which
        # matches parents by canonical instance id) would silently fail to
        # resolve. Dedupe on the CANONICAL id so a drifted and a canonical
        # spelling of one parent collapse to a single edge. The child id is
        # already canonical here — `instance.id` is the resolved row.
        normalized: list[str] = []
        seen: set[str] = set()
        for parent in parent_instance_ids or []:
            token = safe_optional_token(parent)
            if not token:
                continue
            # Defense in depth for the steering invariant: a steering parent is a
            # persona-INSTANCE id, never a principal. Reject a non-instance-shaped
            # token loudly with the reason, BEFORE the store lookup, so no future
            # caller (a replayed spec, a mangled graph edge) can reintroduce the
            # "steered by operator" class of bug — and so the failure names the
            # category error ("not an instance id") rather than a misleading
            # "not found". Actor-token drift (persona_personainst_x) is first
            # collapsed to its canonical instance shape, which the store lookup
            # below then resolves to the real row.
            shaped = canonical_persona_instance_id(token) or token
            if not looks_like_persona_instance_id(shaped):
                raise ValueError(
                    "steering parent must be a persona-instance id "
                    f"({PERSONA_INSTANCE_ID_PREFIX}*), not a non-instance principal: {token!r}"
                )
            try:
                parent_instance = self.get(token)
            except Exception as exc:
                raise ValueError(f"parent persona instance not found: {token}") from exc
            canonical_parent = parent_instance.id
            if canonical_parent == instance.id:
                raise ValueError("a persona instance cannot steer itself")
            if canonical_parent in seen:
                continue
            seen.add(canonical_parent)
            self._validate_no_steering_cycle(instance.id, canonical_parent)
            normalized.append(canonical_parent)
        if not normalized:
            return self.detach_parents(instance.id)
        before = list(instance.steered_by)
        resolved_goal = safe_optional_token(goal_id) if goal_id is not None else instance.goal_id
        updates: dict[str, Any] = {
            "steered_by": normalized,
            # Denormalized legacy mirror: the primary (first) parent, for old
            # readers still keyed on the scalar. Single writer, single source.
            "spawned_by": normalized[0],
            "goal_id": resolved_goal,
        }
        if resolved_goal:
            updates["mode"] = "task_bound"
            updates["current_task_id"] = resolved_goal
        added = [pid for pid in normalized if pid not in before]
        removed = [pid for pid in before if pid not in normalized]
        self._commit_steer(instance, updates, added=added, removed=removed, detached=False)
        return self.get(instance.id)

    def _commit_steer(
        self,
        instance: PersonaInstance,
        updates: dict[str, Any],
        *,
        added: list[str],
        removed: list[str],
        detached: bool,
    ) -> None:
        changed_fields: dict[str, Any] = {}
        for attr, value in updates.items():
            if getattr(instance, attr) != value:
                setattr(instance, attr, value)
                changed_fields[attr] = value
        if not changed_fields:
            return
        instance.updated_at = now()
        self._write(instance)
        self._event(
            "persona_instance.steered",
            instance,
            {
                "goal_id": instance.goal_id,
                "spawned_by": instance.spawned_by,
                "steered_by": list(instance.steered_by),
                "added": added,
                "removed": removed,
                "detached": bool(detached),
            },
        )
        # S6 producer: the flagship field-patch case. ``changed_fields`` is the
        # exact set this steer mutation wrote (steered_by/spawned_by, plus
        # goal_id/mode/current_task_id when the re-route changed them). Dark by
        # default (read_model.delta_patches off).
        self._emit_state_patch(instance, changed_fields)

    def update_profile(
        self,
        persona_instance_id: str,
        *,
        display_name: str | None = None,
        current_chat_goal: str | None = None,
        goal_id: str | None = None,
        skills: list[str] | None = None,
        clear_skills: bool = False,
        provider: str | None = None,
        model: str | None = None,
        api_mode: str | None = None,
        reasoning_effort: str | None = None,
        clear_model_override: bool = False,
        model_issued_at: datetime | None = None,
        requested_by: str | None = None,
    ) -> PersonaInstance:
        """Persist operator-editable runtime profile overrides.

        These fields belong to the durable persona instance, not the backing
        Hermes profile template. Editing ``Alice Agent`` therefore updates the
        live ``personainst_*`` record while leaving the lower ``alice`` profile
        untouched for future default instances.

        ``provider``/``model``/``api_mode`` form the instance model-override
        tier (None = inherit the backing persona live; see
        ``models.apply_instance_model_overrides``). ``clear_model_override``
        resets all three. A ``model_issued_at`` older than the last applied
        model write raises :class:`StaleModelOverrideWrite` instead of
        clobbering the newer value.
        """
        instance = self.get(persona_instance_id)
        before_patch_fields = self._profile_patch_snapshot(instance)
        changed = False
        model_lane_touched = clear_model_override or any(
            value is not None for value in (provider, model, api_mode, reasoning_effort)
        )
        if clear_model_override and any(value is not None for value in (provider, model, api_mode, reasoning_effort)):
            raise ValueError("clear_model_override conflicts with provider/model/api_mode/reasoning_effort values")
        if model_lane_touched:
            if model_issued_at is not None and instance.model_override_issued_at is not None:
                issued = _as_utc(model_issued_at)
                applied = _as_utc(instance.model_override_issued_at)
                if issued <= applied:
                    raise StaleModelOverrideWrite(instance, issued_at=issued, applied_issued_at=applied)
            if clear_model_override:
                if (
                    instance.model is not None
                    or instance.provider is not None
                    or instance.api_mode is not None
                    or instance.reasoning_effort is not None
                ):
                    instance.model = None
                    instance.provider = None
                    instance.api_mode = None
                    instance.reasoning_effort = None
                    changed = True
            else:
                for field_name, raw in (("provider", provider), ("model", model), ("api_mode", api_mode)):
                    if raw is None:
                        continue
                    value = _safe_model_override_text(raw, field_name=field_name)
                    if getattr(instance, field_name) != value:
                        setattr(instance, field_name, value)
                        changed = True
                # Reasoning effort rides the model lane but is a validated enum
                # (or "none"/empty-clear), not free text — normalize separately.
                if reasoning_effort is not None:
                    new_reasoning = _normalize_reasoning_effort_override(reasoning_effort)
                    if instance.reasoning_effort != new_reasoning:
                        instance.reasoning_effort = new_reasoning
                        changed = True
            if changed:
                instance.model_override_issued_at = _as_utc(model_issued_at) if model_issued_at is not None else now()
        if display_name is not None:
            value = safe_assignment_text(display_name, limit=120)
            if not value:
                raise ValueError("display_name must not be empty")
            if instance.display_name != value:
                instance.display_name = value
                changed = True
        if current_chat_goal is not None:
            value = safe_assignment_text(current_chat_goal, limit=500) or None
            if instance.current_chat_goal != value:
                instance.current_chat_goal = value
                changed = True
        if goal_id is not None:
            value = safe_optional_token(goal_id)
            if instance.goal_id != value:
                instance.goal_id = value
                changed = True
            if value and instance.current_task_id != value:
                instance.current_task_id = value
                changed = True
        if skills is not None or clear_skills:
            value = [] if clear_skills else _safe_skill_overrides(skills or [])
            if instance.skill_overrides != value:
                instance.skill_overrides = value
                changed = True
        if changed:
            instance.updated_at = now()
            self._write(instance)
            payload: dict[str, Any] = {
                "display_name": instance.display_name,
                "current_chat_goal": instance.current_chat_goal,
                "goal_id": instance.goal_id,
                "skill_overrides": list(instance.skill_overrides or []),
                "provider": instance.provider,
                "model": instance.model,
                "api_mode": instance.api_mode,
                "reasoning_effort": instance.reasoning_effort,
            }
            if requested_by:
                payload["requested_by"] = str(requested_by)[:80]
            self._event("persona_instance.profile_updated", instance, payload)
            # S6 producer: the persona-instance profile/model write funnel. Emit
            # only the operator-editable fields this call actually changed. Dark
            # by default (read_model.delta_patches off).
            after_patch_fields = self._profile_patch_snapshot(instance)
            self._emit_state_patch(
                instance,
                {
                    field_name: after_patch_fields[field_name]
                    for field_name in after_patch_fields
                    if after_patch_fields[field_name] != before_patch_fields.get(field_name)
                },
            )
        return self.get(instance.id)

    def set_backing_profile(self, persona_instance_id: str, profile_id: str | None) -> PersonaInstance:
        """Re-point one instance's ``profile_id`` at a new backing Hermes profile.

        Deliberately NOT part of :meth:`update_profile`: everything that method
        writes is an operator-editable RUNTIME override that belongs to the
        instance. ``profile_id`` is not — it is a PROJECTION of the owning
        persona's ``hermes_profile``. Folding it into ``update_profile`` would
        create a second, instance-local rebind authority competing with the
        persona record.

        The ONE sanctioned caller is
        :func:`agent_runtime.persona_profile_binding.rebind_persona_profile`,
        which moves the persona authority and cascades every instance row in the
        same operation, refuses while any instance is in flight, and emits the
        single typed ``persona.profile_rebound`` event that accounts for every
        row this method touched. That is why no event is appended here: the
        operation's event names each moved row, and it is deliberately an
        UNCOVERED type (see ``patch_coverage``) so the batch degrades to a full
        core rather than shipping a patch frame that folds nothing.
        """

        instance = self.get(persona_instance_id)
        value = safe_optional_token(profile_id)
        if instance.profile_id == value:
            return instance
        instance.profile_id = value
        instance.updated_at = now()
        self._write(instance)
        return self.get(instance.id)

    def _validate_no_steering_cycle(self, persona_instance_id: str, parent_instance_id: str) -> None:
        # Multi-parent DAG walk: adding parent → child must not let the child
        # reach itself through the steering graph. We only reject when the child
        # is reachable from the new parent (a real cycle); a diamond (two parents
        # sharing an ancestor) is fine, so we track a visited set separately from
        # the cycle test rather than raising on any re-visit.
        visited: set[str] = set()
        frontier = _dedupe_tokens([parent_instance_id])
        while frontier:
            cursor = frontier.pop()
            if cursor == persona_instance_id:
                raise ValueError("steering edge would create a cycle")
            if cursor in visited:
                continue
            visited.add(cursor)
            try:
                parent = self.get(cursor)
            except Exception:
                continue
            frontier.extend(_dedupe_tokens(list(parent.steered_by)))

    def get(self, persona_instance_id: str) -> PersonaInstance:
        path = paths.persona_instance_path(persona_instance_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # The literal file wins (canonical ids are read verbatim — zero
            # behaviour change). Only when it is missing do we resolve the same
            # id-scheme drift the identity module already defines, so a caller
            # that hands us a legacy/actor-token id (the Launcher graph save, a
            # replayed spec, a CLI verb) reaches the real row instead of a hard
            # "not found". Re-raise the original error for a genuinely absent id.
            resolved = self._resolve_stored_instance_id(persona_instance_id)
            if resolved is None:
                raise
            raw = json.loads(paths.persona_instance_path(resolved).read_text(encoding="utf-8"))
        return from_jsonable(PersonaInstance, raw)

    def _resolve_stored_instance_id(self, raw_id: str) -> str | None:
        """Resolve a caller-supplied id to the id of the row actually on disk.

        Tolerates exactly the drift :mod:`persona_instance_identity` already
        knows how to collapse: structural actor-token drift
        (``persona_personainst_x`` -> ``personainst_x`` /
        ``persona:<persona>`` -> canonical channel, via
        :func:`canonical_persona_instance_id`) and the durable
        legacy->canonical alias registry (the operator-hash schemes the store
        reconciler records). Returns a stored id whose file exists, or ``None``
        so the caller raises on a genuinely missing row. Read-only: it never
        mints or rewrites a row — the reconciler remains the durable cleanup.
        """
        candidates: list[str] = []

        def _add(candidate: str | None) -> None:
            if candidate and candidate != raw_id and candidate not in candidates:
                candidates.append(candidate)

        structural = canonical_persona_instance_id(raw_id)
        _add(structural)
        try:
            from .persona_instance_identity import load_persona_instance_aliases

            aliases = load_persona_instance_aliases()
        except Exception:
            aliases = {}
        _add(aliases.get(raw_id))
        if structural:
            _add(aliases.get(structural))
        for candidate in candidates:
            if paths.persona_instance_path(candidate).exists():
                return candidate
        return None

    def _archive_office_placements(self, instance) -> None:
        """Mission Office prune-lane hook (office plan §4.3): a reaped instance
        must not leave a phantom desk file that re-materializes the agent —
        and the fix lives HERE, never in a launcher-side filter (the
        orphan-tombstone precedent). Best-effort: office archival never fails
        the reap."""
        try:
            from .office_store import OfficeStore

            OfficeStore(event_log=self.event_log).archive_actors_for_instance(instance.id, reason="instance_reaped")
        except Exception:
            pass

    def sweep_orphaned_task_bound_instances(self, *, reason: str = "persona instance janitor") -> dict[str, Any]:
        """Reap stale task-bound instances whose owner is terminal or gone.

        Free-floating/profile chat instances are preserved. Instances with a
        genuinely live active worker or run are reported but never touched.
        """
        before = [instance for instance in self.list_all() if instance.mode == "task_bound"]
        reaped: list[str] = []
        skipped_active: list[str] = []
        preserved_live_owner: list[str] = []
        for instance in before:
            if self._has_live_binding(instance):
                skipped_active.append(instance.id)
                continue
            owner_state = _persona_instance_owner_release_state(instance)
            if owner_state is None:
                preserved_live_owner.append(instance.id)
                continue
            if self._delete(instance):
                reaped.append(instance.id)
                self._event(
                    "persona_instance.reaped",
                    instance,
                    {
                        "task_id": safe_optional_token(instance.current_task_id),
                        "goal_id": safe_optional_token(instance.goal_id),
                        "reason": safe_assignment_text(reason, limit=240),
                        "owner_state": owner_state,
                    },
                )
                # S7-A producer: janitor reap also removes the keyed row.
                emit_persona_instance_remove(self.event_log, instance, reason=reason)
                self._archive_office_placements(instance)
        after = [instance for instance in self.list_all() if instance.mode == "task_bound"]
        return {
            "before_task_bound_count": len(before),
            "after_task_bound_count": len(after),
            "reaped_persona_instance_ids": reaped,
            "skipped_active_persona_instance_ids": skipped_active,
            "preserved_live_owner_persona_instance_ids": preserved_live_owner,
            "remaining_task_bound_persona_instance_ids": [instance.id for instance in after],
            "reaped_count": len(reaped),
            "skipped_active_count": len(skipped_active),
            "preserved_live_owner_count": len(preserved_live_owner),
            "remaining_count": len(after),
        }

    def retire(
        self,
        persona_instance_id: str,
        *,
        reason: str = "placement removed",
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        """Instance end-of-life: archive a placement-backed (or otherwise
        deliberate) persona-instance ROW and emit an EventLog event.

        The operator ruling is that a deliberate placement IS the instance:
        deleting the placement ends the instance's life. This is the sanctioned
        verb for that transition — the row file MOVES to
        ``persona_instances_archive/<ts>_retire/`` (archive-never-delete), an
        evented mutation (no silent row move). Chat sessions and turn stores
        stay untouched on disk (history is never destroyed); the projection
        simply stops listing the row because ``list_all`` only globs the live
        dir, so every instance-fed surface (snapshot, roster, chat history,
        flow node dropdown) drops it on the next frame.

        Refuses with a typed :class:`PersonaInstanceRetireError` (never a silent
        no-op) when the row is the canonical persona/profile channel
        (``canonical_persona_channel`` — the global-singleton retirement is the
        queued workspace-scoping redesign), when a live run/worker still
        resolves (``instance_active`` — never archive a working agent), or when
        an active persona assignment is bound (``assignment_active``)."""
        try:
            instance = self.get(persona_instance_id)
        except Exception as exc:
            raise PersonaInstanceRetireError(
                "not_found",
                f"persona instance not found: {persona_instance_id}",
                persona_instance_id=safe_assignment_token(persona_instance_id) or str(persona_instance_id),
            ) from exc

        if is_canonical_persona_channel(instance):
            raise PersonaInstanceRetireError(
                "canonical_persona_channel",
                (
                    f"{instance.id} is the canonical persona channel for "
                    f"{instance.persona_id!r}; the global-singleton channel cannot be "
                    "retired here (that is the queued workspace-scoping redesign) — "
                    "retire a placement-backed instance instead"
                ),
                persona_instance_id=instance.id,
                detail={"persona_id": instance.persona_id},
            )

        if self._has_live_binding(instance):
            raise PersonaInstanceRetireError(
                "instance_active",
                f"{instance.id} has a live run binding; never retire a working agent",
                persona_instance_id=instance.id,
                detail={
                    "active_run_id": safe_optional_token(instance.active_run_id),
                },
            )

        active_assignment_ids = self._active_assignment_ids_for_instance(instance.id)
        if active_assignment_ids:
            raise PersonaInstanceRetireError(
                "assignment_active",
                (
                    f"{instance.id} has {len(active_assignment_ids)} active assignment(s); "
                    "complete or close them before retiring"
                ),
                persona_instance_id=instance.id,
                detail={"active_assignment_ids": active_assignment_ids},
            )

        archive_dir = paths.persona_instances_archive_dir() / f"{now().strftime('%Y%m%dT%H%M%SZ')}_retire"
        archived_path = self._archive_instance_row(instance, archive_dir)
        if archived_path is None:
            raise PersonaInstanceRetireError(
                "not_found",
                f"persona instance row is not on disk: {instance.id}",
                persona_instance_id=instance.id,
            )
        safe_reason = safe_assignment_text(reason, limit=240) or "placement removed"
        normalized_requested_by = str(requested_by)[:80] if requested_by else None
        payload: dict[str, Any] = {
            "reason": safe_reason,
            "persona_id": instance.persona_id,
            "mode": instance.mode,
            "archive_dir": str(archive_dir),
        }
        if normalized_requested_by:
            payload["requested_by"] = normalized_requested_by
        self._event("persona_instance.retired", instance, payload)
        # S7-A producer: the retired row leaves the active frame, so the launcher
        # deletes the keyed row (never renders it as a live idle agent). Dark by
        # default (read_model.delta_patches off).
        emit_persona_instance_remove(self.event_log, instance, reason=safe_reason)
        # Prune-lane hook (mirrors close_for_task / the janitor): a retired
        # instance must not leave a phantom office desk. Best-effort; office
        # archival never fails the retire.
        self._archive_office_placements(instance)
        return {
            "persona_instance_id": instance.id,
            "persona_id": instance.persona_id,
            "display_name": instance.display_name,
            "mode": instance.mode,
            "reason": safe_reason,
            "requested_by": normalized_requested_by,
            "archive_path": str(archived_path),
            "archive_dir": str(archive_dir),
        }

    def _active_assignment_ids_for_instance(self, instance_id: str) -> list[str]:
        """Ids of active persona assignments bound to this instance (guard input
        for :meth:`retire`). A retire must never orphan a live assignment."""
        try:
            assignments = PersonaAssignmentStore(event_log=self.event_log).list_all()
        except Exception:
            return []
        return [
            assignment.id
            for assignment in assignments
            if assignment.persona_instance_id == instance_id
            and assignment.state in ACTIVE_ASSIGNMENT_STATES
        ]

    def _archive_instance_row(self, instance: PersonaInstance, archive_dir) -> Any:
        """Move the instance's row file into ``archive_dir`` (archive-never-delete).
        Returns the archived path, or ``None`` when the live row is already gone."""
        source = paths.persona_instance_path(instance.id)
        if not source.exists():
            return None
        self._release_parent_references(instance.id)
        archive_dir.mkdir(parents=True, exist_ok=True)
        target = archive_dir / source.name
        shutil.move(str(source), str(target))
        return target

    def update(self, instance: PersonaInstance) -> PersonaInstance:
        instance.updated_at = now()
        self._write(instance)
        return self.get(instance.id)

    def retired_instance_archive_path(
        self,
        persona_instance_id: str | None,
        *,
        persona_id: str | None = None,
    ) -> Path | None:
        """The retirement tombstone for *persona_instance_id*, or ``None``.

        THE read-only retirement predicate. Retirement is not a flag on a row —
        it is the ABSENCE of a live row PLUS the presence of a ``*_retire``
        archive — so every caller that needs the answer has to compose those two
        facts. Composing them inline at each site is how a second, subtly
        different retirement rule gets born (one that reads a reconcile/prune
        archive as a tombstone, say, and makes a legitimate future mint
        impossible), so the composition lives here and :meth:`open_chat` — the
        write chokepoint that refuses a retired placement — asks this same
        method instead of re-deriving it.

        It exists because ``open_chat`` answers the question only by RAISING,
        and by then a caller like ``PersonaChatMintReceiptStore.mint`` has
        already created a titled session row. A refusal decidable without
        writing anything must be decidable WITHOUT writing anything.

        Never creates, mutates, or resurrects a row. Pass ``persona_id`` to
        resolve a caller-supplied (or omitted) instance id through the same
        :func:`canonical_chat_instance_id` derivation ``open_chat`` uses;
        without it the id is taken as already canonical.

        NEVER RAISES for a storage failure. Both callers ask this before their
        first durable write, and the mint's caller handles exactly one typed
        error (:class:`RetiredPersonaInstanceError`) — so an ``OSError`` from a
        flaky/UNC store root escaping here would reach the operator as the
        untyped traceback this predicate exists to retire. A probe that cannot
        read the archive cannot PROVE retirement, so it reports ``None`` (the
        pre-flight's posture, now shared by construction) and logs; the write
        chokepoint ``open_chat`` still refuses a retired target, so failing open
        costs the litter, never the guarantee.
        """
        instance_id = (
            canonical_chat_instance_id(persona_id, persona_instance_id)
            if persona_id
            else safe_assignment_token(persona_instance_id)
        )
        if not instance_id:
            return None
        try:
            self.get(instance_id)
        except Exception:
            pass
        else:
            # A live row always wins: the archive is history, and an id carried by a
            # live placement is live — never a tombstone.
            return None
        try:
            return _retired_persona_instance_archive_path(instance_id)
        except OSError:
            # The tombstone probe is filesystem I/O (``exists`` / ``iterdir`` /
            # ``is_file``) over the archive root. Loud in the log, quiet in the
            # answer: a caller must not be handed a refusal the store never
            # actually proved, nor a traceback from a lane that has a typed
            # refusal contract.
            logging.getLogger(__name__).warning(
                "retirement tombstone probe failed for %s; "
                "treating the target as NOT retired",
                instance_id,
                exc_info=True,
            )
            return None

    def assert_bindable(
        self,
        *,
        persona_id: str,
        session_id: str | None = None,
        persona_instance_id: str | None = None,
    ) -> str:
        """Everything :meth:`open_chat` refuses, asserted WITHOUT writing anything.

        Returns the canonical instance id the bind would target, so a caller that
        needs to act before the bind derives that id ONCE, here, rather than
        re-deriving it and drifting.

        This exists because ``open_chat`` answers "may this bind happen?" only by
        RAISING at the end of whatever the caller already did. For
        :meth:`PersonaChatMintReceiptStore.mint` that end came after a session row
        had been created, meta written and a title set — so a dispatch to a target
        that could never be served left a permanent titled thread in Mission
        Control, and the refusal arrived one durable write too late. A refusal
        decidable without writing must be decidable WITHOUT writing, and it must be
        the SAME refusal: one derivation, one spelling of the target id, one
        retirement rule (:meth:`retired_instance_archive_path`).

        ``session_id`` is optional so a caller can ask "is this instance bindable
        at all?" before it has minted a root; when present it is checked for the
        sibling-steal the bind refuses. ``open_chat`` calls this first and is the
        write chokepoint, so this costs one extra row read on the bind path and
        buys the pre-flight callers an answer they can trust.
        """

        normalized_persona = _normalize_instance_source_persona(persona_id)
        if not normalized_persona:
            raise ValueError("persona_id is required")
        normalized_instance = (
            canonical_persona_instance_id(persona_instance_id, persona_id=normalized_persona)
            if persona_instance_id
            else None
        )
        instance_id = normalized_instance or persona_instance_id_for(normalized_persona)
        # A chat session encodes the instance it was minted for; binding one
        # instance's session onto ANOTHER instance's pointer is the sibling steal
        # that overwrote ``personainst_qa``'s default-chat pointer with a
        # placement sibling's session (live 2026-07-18: the console's open-chat of
        # a sibling bound its session onto the canonical primary, then a
        # bare-persona relay adopted the poisoned pointer — both instances folded
        # onto ONE operator channel). Refuse loudly at the write chokepoint every
        # send/open flows through — the existing ``_session_owned_by_other_instance``
        # guard only covered ``add_instance``. Legacy/opaque ``persona_chat_*``
        # sessions (no encoded owner) and the instance's own sessions bind freely.
        normalized_session = safe_assignment_text(session_id, limit=200)
        if normalized_session and chat_session_is_foreign_to_instance(
            normalized_session, instance_id
        ):
            owner = chat_session_owner_instance_id(normalized_session)
            raise ValueError(
                f"chat session {normalized_session!r} belongs to instance {owner!r}; "
                f"it cannot be bound onto {instance_id!r} — open that instance's own "
                "chat lane instead of adopting a sibling's session"
            )
        # ONE retirement rule, composed in one place (absence of a live row PLUS a
        # ``*_retire`` tombstone) and asked here by every caller that needs it.
        retired_archive = self.retired_instance_archive_path(instance_id)
        if retired_archive is not None:
            raise RetiredPersonaInstanceError(instance_id, archive_path=retired_archive)
        return instance_id

    def open_chat(
        self,
        *,
        persona_id: str,
        session_id: str,
        persona_instance_id: str | None = None,
        display_name: str | None = None,
        default_display_name: str | None = None,
        profile_id: str | None = None,
        kill_active: bool = False,
        workspace_id: str | None = None,
        realm_id: str | None = None,
    ) -> PersonaInstance:
        """Bind a persona instance to a durable chat session without running a turn.

        Persona instances are intentionally chat-shaped: selecting an old chat can
        re-open the same live persona instance history by rebinding the instance
        to the stored session id, while the normal send/resume path owns the actual
        LLM execution. A placement retired through :meth:`retire` is the explicit
        exception: its archived row is an end-of-life tombstone, so the preserved
        chat stays history-only and cannot recreate a live roster row. This helper
        is a state transition only; it never fabricates a task, worker, run, or
        transcript.
        """
        normalized_persona = _normalize_instance_source_persona(persona_id)
        normalized_session = safe_assignment_text(session_id, limit=200)
        if not normalized_persona:
            raise ValueError("persona_id is required")
        if not normalized_session:
            raise ValueError("session_id is required")

        # The bind's refusals — sibling steal and retirement — live in ONE
        # read-only seam so a pre-flight caller and the bind itself cannot
        # disagree about who this is or whether it may be bound.
        instance_id = self.assert_bindable(
            persona_id=persona_id,
            session_id=normalized_session,
            persona_instance_id=persona_instance_id,
        )
        safe_display_name = safe_assignment_text(display_name, limit=120) if display_name is not None else None
        safe_default_display_name = (
            safe_assignment_text(default_display_name, limit=120) if default_display_name is not None else None
        )
        safe_profile_id = safe_assignment_token(profile_id) if profile_id is not None else None
        created = False
        try:
            instance = self.get(instance_id)
        except Exception:
            ts = now()
            role = "profile" if normalized_persona.startswith("profile:") else normalized_persona
            instance = PersonaInstance(
                id=instance_id,
                persona_id=normalized_persona,
                role=role,
                display_name=safe_display_name or safe_default_display_name or _display_name_for_template(normalized_persona.split(":", 1)[1] if normalized_persona.startswith("profile:") else normalized_persona),
                profile_id=safe_profile_id or (normalized_persona.split(":", 1)[1] if normalized_persona.startswith("profile:") else None),
                runtime_root=str(paths.store_root()),
                state=WorkerSessionState.IDLE,
                updated_at=ts,
            )
            created = True
        else:
            # Worker/run ownership is orthogonal to operator chat ownership.
            # Opening another chat root must not cancel or rebind live work.
            pass

        before = None if created else (
            instance.display_name,
            instance.profile_id,
            instance.workspace_id,
            instance.realm_id,
            instance.mode,
            instance.default_chat_session_id,
            instance.session_id,
        )

        # An explicit ``display_name`` is AUTHORITATIVE — an operator naming this
        # chat (create_operator_chat) or a deliberate placement (add_instance,
        # "QA Agent (2)"); it always applies. A ``default_display_name`` is the
        # persona DEFAULT the SEND PATH stamps and must NEVER rename an existing
        # instance: applying it unconditionally clobbered a placement name —
        # ``personainst_qa_agent_2`` read "QA Agent" instead of "QA Agent (2)"
        # (the "(2)" is LOAD-BEARING: the launcher conversational fold keys on
        # persona+displayName, so the clobber folds a sibling onto the primary's
        # channel). Stamp the default only when the instance has NO name yet.
        # The one rename path stays ``persona.instance.update_profile``.
        if safe_display_name:
            instance.display_name = safe_display_name
        elif safe_default_display_name and not safe_assignment_text(
            getattr(instance, "display_name", None), limit=120
        ):
            instance.display_name = safe_default_display_name
        if safe_profile_id:
            instance.profile_id = safe_profile_id
        elif normalized_persona.startswith("profile:") and not instance.profile_id:
            instance.profile_id = normalized_persona.split(":", 1)[1]
        # Scope-provenance pointers: a provided workspace/realm is the caller's
        # authoritative placement-scope claim (the launcher stamps its active
        # scope when minting a placement) and applies on create AND re-open; an
        # omitted one never clears an existing pointer (plain chat re-opens
        # don't know scope and must not erase it).
        safe_workspace_id = safe_assignment_token(workspace_id) if workspace_id is not None else None
        safe_realm_id = safe_assignment_token(realm_id) if realm_id is not None else None
        if safe_workspace_id:
            instance.workspace_id = safe_workspace_id
        if safe_realm_id:
            instance.realm_id = safe_realm_id
        instance.mode = "chat"
        instance.default_chat_session_id = normalized_session
        # Read-compatible mirror for v1 consumers. Worker writers never touch
        # this field; default_chat_session_id is the sole new authority.
        instance.session_id = normalized_session
        after = (
            instance.display_name,
            instance.profile_id,
            instance.workspace_id,
            instance.realm_id,
            instance.mode,
            instance.default_chat_session_id,
            instance.session_id,
        )
        if not created and before == after:
            # Idempotent re-open is an observation, not a mutation. Rewriting the
            # row would advance directory fingerprints and emitting
            # persona_instance.chat_opened would force a full-core stream delta.
            # One first-turn path legitimately reaches this chokepoint multiple
            # times; no-op calls must stay invisible to the event/read model.
            return instance
        updated = self.update(instance)
        self._event("persona_instance.chat_opened", updated, {"session_id": normalized_session})
        return updated

    def create_operator_chat(
        self,
        *,
        persona_id: str,
        display_name: str,
        session_id: str | None = None,
        kill_active: bool = False,
    ) -> PersonaInstance:
        normalized_persona = _normalize_instance_source_persona(persona_id)
        instance_id = persona_instance_id_for(normalized_persona)
        return self.open_chat(
            persona_id=normalized_persona,
            persona_instance_id=instance_id,
            session_id=session_id or persona_chat_session_id_for(instance_id),
            display_name=display_name,
            profile_id=_profile_id_for_persona_or_template(normalized_persona),
            kill_active=kill_active,
        )

    def add_instance(
        self,
        *,
        persona_id: str,
        placement_id: str,
        display_name: str | None = None,
        default_display_name: str | None = None,
        session_id: str | None = None,
        workspace_id: str | None = None,
        realm_id: str | None = None,
    ) -> PersonaInstance:
        normalized_persona = _normalize_instance_source_persona(persona_id)
        normalized_placement = safe_assignment_token(placement_id)
        if not normalized_placement:
            raise ValueError("placement_id is required for an additional persona instance")
        instance_id = persona_instance_id_for_placement(normalized_placement)
        try:
            existing = self.get(instance_id)
        except Exception:
            existing = None
        if existing is not None and existing.persona_id != normalized_persona:
            raise ValueError(f"placement already belongs to {existing.persona_id}: {normalized_placement}")
        normalized_session = safe_assignment_text(session_id, limit=200) if session_id is not None else None
        if normalized_session and self._session_owned_by_other_instance(normalized_session, instance_id):
            normalized_session = None
        # ``display_name`` is the operator's AUTHORITATIVE placement name and
        # always wins; ``default_display_name`` is the persona's honest default,
        # stamped only when the instance has no name yet — never enough to clobber
        # an existing distinct placement name on a re-open (open_chat enforces).
        return self.open_chat(
            persona_id=normalized_persona,
            persona_instance_id=instance_id,
            session_id=normalized_session or persona_chat_session_id_for(instance_id),
            display_name=display_name,
            default_display_name=default_display_name,
            profile_id=_profile_id_for_persona_or_template(normalized_persona),
            kill_active=False,
            workspace_id=workspace_id,
            realm_id=realm_id,
        )

    def _session_owned_by_other_instance(self, session_id: str, instance_id: str) -> bool:
        for instance in self.list_all():
            if instance.id != instance_id and instance.default_chat_session_id == session_id:
                return True
        return False

    # S56 removed ``update_from_worker`` and ``_goal_id_for_worker`` with the
    # worker session store. They were the only way a persona instance could ever
    # be stamped ``mode="task_bound"`` from a worker row, and the only writer of
    # ``PersonaInstance.active_worker_session_id`` (a field that went with them).

    def list_all(self) -> list[PersonaInstance]:
        directory = paths.persona_instances_dir()
        if not directory.exists():
            return []
        instances: list[PersonaInstance] = []
        for path in sorted(directory.glob("*.json")):
            try:
                instances.append(from_jsonable(PersonaInstance, json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(instances, key=lambda item: item.id)

    def ensure_for_personas(self, personas: list[AgentPersona]) -> list[PersonaInstance]:
        """Materialize an instance for every configured persona and settle any
        instance still carrying a stale execution binding.

        S56 renamed this from ``derive_from_workers(personas, workers)``. The
        ``workers`` half is gone: ``build_snapshot`` had been passing a
        ``workers = []`` literal for two waves, so the "a live worker carries
        this persona's binding" branch could not be taken on the live tree, and
        the worker session store it read has since been deleted. What remains —
        the ensure pass plus the configured/idle reset — is exactly what ran
        before, now unconditionally rather than for "every persona with no live
        worker" (which was every persona).

        ``chat`` / ``free_floating`` instances are still skipped: an operator
        chat binding is not stale execution state.
        """
        for persona in personas:
            self.ensure_for_persona(persona)
        for persona in personas:
            instance = self.ensure_for_persona(persona)
            if instance.mode in {"chat", "free_floating"}:
                continue
            if (
                instance.state != WorkerSessionState.IDLE
                or instance.current_assignment_id
                or instance.current_task_id
                or instance.active_run_id
                or instance.context_receipt_id
                or instance.compression_receipt_id
            ):
                instance.state = WorkerSessionState.IDLE
                instance.mode = "configured"
                instance.current_assignment_id = None
                instance.current_task_id = None
                instance.goal_id = None
                instance.spawned_by = None
                instance.steered_by = []
                instance.active_run_id = None
                instance.context_receipt_id = None
                instance.compression_receipt_id = None
                instance.token_budget_used = 0
                instance.tool_budget_used = 0
                instance.watchdog_warning_count = 0
                instance.last_heartbeat_at = None
                self.update(instance)
        return self.list_all()

    def _delete(self, instance: PersonaInstance) -> bool:
        path = paths.persona_instance_path(instance.id)
        if not path.exists():
            return False
        self._release_parent_references(instance.id)
        path.unlink()
        return True

    def _has_live_binding(self, instance: PersonaInstance) -> bool:
        # S56 removed the worker arm: the store it read is gone and no instance
        # can carry ``active_worker_session_id`` any more. The run arm is the
        # whole check.
        run_id = safe_optional_token(instance.active_run_id)
        if run_id:
            try:
                from .store import ACTIVE_RUN_STATES, RunStore

                run = RunStore(event_log=self.event_log).get(run_id)
                if run.state in ACTIVE_RUN_STATES:
                    return True
            except Exception:
                pass
        return False

    def _write(self, instance: PersonaInstance) -> None:
        path = paths.persona_instance_path(instance.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, to_jsonable(instance), indent=2, sort_keys=True)

    def _event(self, event_type: str, instance: PersonaInstance, payload: dict[str, Any]) -> None:
        self.event_log.append(Event(ts=now(), type=event_type, task_id=instance.current_task_id, run_id=instance.active_run_id, persona_id=instance.persona_id, payload={**payload, "persona_instance_id": instance.id}))

    @staticmethod
    def _profile_patch_snapshot(instance: PersonaInstance) -> dict[str, Any]:
        """Operator-editable runtime fields watched for S6 field-patch diffs.

        Lists are copied so a before/after comparison is not fooled by in-place
        mutation of the same underlying object."""

        return {
            "display_name": instance.display_name,
            "current_chat_goal": instance.current_chat_goal,
            "goal_id": instance.goal_id,
            "current_task_id": instance.current_task_id,
            "skill_overrides": list(instance.skill_overrides or []),
            "provider": instance.provider,
            "model": instance.model,
            "api_mode": instance.api_mode,
            "reasoning_effort": instance.reasoning_effort,
        }

    def _emit_state_patch(self, instance: PersonaInstance, changed: dict[str, Any]) -> None:
        """Emit an ``upsert`` ``state.patched`` entry for a persona-instance
        field change (S7-A producer; dark unless ``read_model.delta_patches`` is
        on). The store field NAMES that changed drive a WIRE-LEVEL projection
        (see :func:`emit_persona_instance_patch`) so the derived wire fields the
        launcher reads (``effective_model`` / ``skills`` / the display-name
        mirror / …) ship recomputed, not stale."""

        emit_persona_instance_patch(self.event_log, instance, list(changed.keys()))


class PersonaAssignmentStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def create(self, assignment: PersonaAssignment) -> PersonaAssignment:
        path = paths.persona_assignment_path(assignment.id)
        if path.exists():
            raise ValueError(f"persona assignment already exists: {assignment.id}")
        assignment.signal_hash = assignment.signal_hash or assignment_signal_hash(assignment)
        self._write(assignment)
        self._event("persona_assignment.created", assignment, {"state": assignment.state})
        return self.get(assignment.id)

    def create_or_resume(self, spec: PersonaAssignmentSpec) -> PersonaAssignment:
        persona_id = safe_assignment_token(spec.persona_id)
        goal_id = safe_optional_token(spec.goal_id)
        instance_id = canonical_persona_instance_id(spec.persona_instance_id, persona_id=persona_id) or (
            persona_instance_id_for_placement(f"{goal_id}:{persona_id}") if goal_id else persona_instance_id_for(persona_id)
        )
        signal_hash = assignment_signal_hash_from_parts(
            persona_id=persona_id,
            goal_id=goal_id,
            stage_id=spec.stage_id,
            kind=spec.kind,
            repo_bundle_id=spec.repo_bundle_id,
            repo=spec.repo,
            affected_paths=spec.affected_paths,
            proof_targets=spec.proof_targets,
            message=spec.message,
        )
        client_message_id = safe_assignment_text(spec.client_message_id, limit=200)
        if client_message_id:
            for existing in self.list_all():
                if (
                    existing.persona_instance_id == instance_id
                    and existing.kind == (safe_assignment_token(spec.kind) or "task_stage")
                    and getattr(existing, "client_message_id", None) == client_message_id
                ):
                    return existing
        for existing in self.find_active(
            persona_id=persona_id,
            goal_id=goal_id,
            stage_id=spec.stage_id,
            kind=spec.kind,
        ):
            if existing.signal_hash == signal_hash:
                return existing
        ts = now()
        evidence_kind = assignment_evidence_kind(spec.kind)
        archive_scope = assignment_archive_scope(spec.kind)
        production_proof_eligible = (
            bool(spec.production_proof_eligible)
            if spec.production_proof_eligible is not None
            else evidence_kind == "task_bound"
        )
        return self.create(
            PersonaAssignment(
                id=f"assign_{uuid.uuid4().hex[:12]}",
                persona_instance_id=instance_id,
                persona_id=persona_id,
                kind=safe_assignment_token(spec.kind) or "task_stage",
                state=safe_assignment_state(spec.state),
                title=safe_assignment_text(spec.title, limit=200),
                message=safe_assignment_text(spec.message, limit=4000),
                created_by=safe_assignment_token(spec.created_by) or "harness",
                created_at=ts,
                updated_at=ts,
                goal_id=goal_id,
                stage_id=safe_optional_token(spec.stage_id),
                operation_id=safe_optional_token(spec.operation_id),
                repo_bundle_id=safe_optional_token(spec.repo_bundle_id),
                repo=safe_assignment_text(spec.repo or "", limit=160) or None,
                affected_paths=[safe_assignment_text(item, limit=240) for item in spec.affected_paths if safe_assignment_text(item, limit=240)],
                proof_targets=[safe_assignment_text(item, limit=240) for item in spec.proof_targets if safe_assignment_text(item, limit=240)],
                acceptance=[safe_assignment_text(item, limit=500) for item in spec.acceptance if safe_assignment_text(item, limit=500)],
                non_goals=[safe_assignment_text(item, limit=500) for item in spec.non_goals if safe_assignment_text(item, limit=500)],
                allowed_decisions=[safe_assignment_token(item) for item in spec.allowed_decisions if safe_assignment_token(item)],
                allowed_tools=[safe_assignment_token(item) for item in spec.allowed_tools if safe_assignment_token(item)],
                evidence_kind=safe_assignment_token(spec.evidence_kind or evidence_kind) or evidence_kind,
                production_proof_eligible=production_proof_eligible,
                archive_scope=safe_assignment_token(spec.archive_scope or archive_scope) or archive_scope,
                client_message_id=client_message_id or None,
                signal_hash=signal_hash,
            )
        )

    def get(self, assignment_id: str) -> PersonaAssignment:
        raw = json.loads(paths.persona_assignment_path(assignment_id).read_text(encoding="utf-8"))
        return from_jsonable(PersonaAssignment, raw)

    def update(self, assignment: PersonaAssignment) -> PersonaAssignment:
        assignment.updated_at = now()
        self._write(assignment)
        return self.get(assignment.id)

    def list_all(self) -> list[PersonaAssignment]:
        directory = paths.persona_assignments_dir()
        if not directory.exists():
            return []
        assignments: list[PersonaAssignment] = []
        for path in sorted(directory.glob("*.json")):
            try:
                assignments.append(from_jsonable(PersonaAssignment, json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(assignments, key=lambda item: item.created_at)

    def list_for_persona(self, persona_id: str) -> list[PersonaAssignment]:
        normalized = safe_assignment_token(persona_id)
        return [assignment for assignment in self.list_all() if assignment.persona_id == normalized]

    def list_for_goal(self, goal_id: str) -> list[PersonaAssignment]:
        """Group assignments by the canonical goal id.

        Matches on ``goal_id`` (the Stage 39 grouping key); for legacy records
        where ``goal_id`` was never stamped, falls back to ``task_id`` so the
        old ``goal_id == task.id`` records still resolve.
        """
        normalized = safe_optional_token(goal_id)
        return [
            assignment
            for assignment in self.list_all()
            if (assignment.goal_id or assignment.task_id) == normalized
        ]

    def find_active(self, *, persona_id: str | None = None, goal_id: str | None = None, stage_id: str | None = None, kind: str | None = None) -> list[PersonaAssignment]:
        wanted_persona = safe_assignment_token(persona_id) if persona_id else None
        wanted_goal = safe_optional_token(goal_id) if goal_id else None
        wanted_stage = safe_optional_token(stage_id) if stage_id else None
        wanted_kind = safe_assignment_token(kind) if kind else None
        return [
            assignment
            for assignment in self.list_all()
            if assignment.state in ACTIVE_ASSIGNMENT_STATES
            and (wanted_persona is None or assignment.persona_id == wanted_persona)
            and (wanted_goal is None or assignment.goal_id == wanted_goal)
            and (wanted_stage is None or assignment.stage_id == wanted_stage)
            and (wanted_kind is None or assignment.kind == wanted_kind)
        ]

    def attach_run(self, assignment_id: str, run_id: str) -> PersonaAssignment:
        assignment = self.get(assignment_id)
        safe_run_id = safe_optional_token(run_id)
        if safe_run_id and safe_run_id not in assignment.run_ids:
            assignment.run_ids.append(safe_run_id)
        if assignment.state not in TERMINAL_ASSIGNMENT_STATES:
            assignment.state = "running"
        return self.update(assignment)

    def attach_proof(self, assignment_id: str, proof_id: str) -> PersonaAssignment:
        assignment = self.get(assignment_id)
        safe_proof_id = safe_optional_token(proof_id)
        if safe_proof_id and safe_proof_id not in assignment.proof_ids:
            assignment.proof_ids.append(safe_proof_id)
        return self.update(assignment)

    def record_context(self, assignment_id: str, context_receipt_id: str | None) -> PersonaAssignment:
        assignment = self.get(assignment_id)
        safe_receipt = safe_optional_token(context_receipt_id)
        if safe_receipt and safe_receipt not in assignment.context_receipt_ids:
            assignment.context_receipt_ids.append(safe_receipt)
        return self.update(assignment)

    def complete(self, assignment_id: str, *, state: str = "completed", error: str | None = None) -> PersonaAssignment:
        assignment = self.get(assignment_id)
        target_state = state if state in TERMINAL_ASSIGNMENT_STATES else "completed"
        target_error = safe_assignment_text(error or "", limit=500) or None
        if assignment.state in TERMINAL_ASSIGNMENT_STATES and assignment.state == target_state and assignment.last_error == target_error:
            return assignment
        assignment.state = target_state
        assignment.last_error = target_error
        assignment.completed_at = now()
        updated = self.update(assignment)
        self._event("persona_assignment.closed", updated, {"state": updated.state})
        return updated

    def _write(self, assignment: PersonaAssignment) -> None:
        path = paths.persona_assignment_path(assignment.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, to_jsonable(assignment), indent=2, sort_keys=True)

    def _event(self, event_type: str, assignment: PersonaAssignment, payload: dict[str, Any]) -> None:
        self.event_log.append(
            Event(
                ts=now(),
                type=event_type,
                task_id=assignment.task_id,
                run_id=assignment.run_ids[-1] if assignment.run_ids else None,
                persona_id=assignment.persona_id,
                payload={
                    **payload,
                    "assignment_id": assignment.id,
                    "persona_instance_id": assignment.persona_instance_id,
                    "kind": assignment.kind,
                    "client_message_id": assignment.client_message_id,
                    "title": assignment.title,
                    "message": assignment.message,
                    "stage_id": assignment.stage_id,
                    "goal_id": assignment.goal_id,
                    "repo": assignment.repo,
                    "affected_paths": list(assignment.affected_paths or []),
                    "proof_targets": list(assignment.proof_targets or []),
                    "acceptance": list(assignment.acceptance or []),
                    "non_goals": list(assignment.non_goals or []),
                    "allowed_decisions": list(assignment.allowed_decisions or []),
                },
            )
        )


def migrate_retired_persona_assignment_task_ids(*, dry_run: bool) -> dict[str, Any]:
    """Archive pre-retirement mission-lane assignment rows.

    A non-null raw ``task_id`` is the migration discriminator. Rows move out of
    the live store intact; no record is rewritten or deleted. Repeating the
    apply is inert because migrated rows are no longer present in the live
    directory.
    """

    live_dir = paths.persona_assignments_dir()
    archive_dir = paths.persona_assignments_archive_dir() / "retired_task_id"
    sources = sorted(live_dir.glob("*.json")) if live_dir.exists() else []
    eligible: list[tuple[Path, str]] = []
    held: list[dict[str, str]] = []
    for source in sources:
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except Exception as exc:
            held.append({"path": source.name, "reason": f"unreadable:{type(exc).__name__}"})
            continue
        if not isinstance(raw, dict) or raw.get("task_id") is None:
            continue
        assignment_id = safe_assignment_token(raw.get("id")) or source.stem
        eligible.append((source, assignment_id))

    archived = 0
    if not dry_run and eligible:
        archive_dir.mkdir(parents=True, exist_ok=True)
        for source, assignment_id in eligible:
            target = archive_dir / source.name
            if target.exists():
                held.append({"assignment_id": assignment_id, "reason": "archive_target_exists"})
                continue
            shutil.move(str(source), str(target))
            archived += 1

    return {
        "ok": not held,
        "migration": "retired_persona_assignment_task_id",
        "dry_run": bool(dry_run),
        "scanned": len(sources),
        "eligible": len(eligible),
        "archived": archived,
        "assignment_ids": [assignment_id for _, assignment_id in eligible],
        "archive_dir": str(archive_dir),
        "held": held,
    }


# ``PERSONA_INSTANCE_ID_PREFIX`` / ``looks_like_persona_instance_id`` are the
# single id-shape authority, defined in ``models`` (the low layer the store row
# lives in) and imported at the top of this module. Re-exported here verbatim so
# existing ``from .persona_assignments import PERSONA_INSTANCE_ID_PREFIX``
# callers (harness.py, persona_commands.py) keep resolving through one home.

# An operator-channel actor token ('persona_' + instance id) that leaked into
# a store row id. Live evidence 2026-07-10: persona_personainst_neko_supervisor
# persisted beside personainst_neko_supervisor for the same channel.
_ACTOR_TOKEN_DRIFT_PREFIX = f"persona_{PERSONA_INSTANCE_ID_PREFIX}"


def persona_instance_id_for(persona_id: str) -> str:
    return f"{PERSONA_INSTANCE_ID_PREFIX}{safe_assignment_token(persona_id) or 'persona'}"


def persona_instance_id_for_placement(placement_id: str) -> str:
    return f"{PERSONA_INSTANCE_ID_PREFIX}{safe_assignment_token(placement_id) or 'persona'}"


def is_canonical_persona_channel(instance: PersonaInstance) -> bool:
    """True when a row IS the persona/profile's canonical operator channel.

    The canonical channel is the global-singleton id
    ``persona_instance_id_for(persona_id)`` (e.g. ``personainst_qa`` for persona
    ``qa``, ``personainst_profile_alice`` for ``profile:alice``). A
    placement-backed or otherwise deliberate instance carries a distinct
    placement-derived id (``personainst_qa_agent_2``) whose tail is the scene
    itemId, so it never collapses onto the canonical id — that is exactly the
    discriminator the retire verb uses to protect the queued global-singleton
    redesign while ending placement-backed rows."""
    if not safe_assignment_token(instance.persona_id):
        return False
    return instance.id == persona_instance_id_for(instance.persona_id)


def canonical_persona_instance_id(raw_id: Any, *, persona_id: str | None = None) -> str | None:
    """SINGLE derivation authority for a caller-supplied persona-instance id.

    Every path that accepts an instance id from outside the store (operator
    chat opens, assignment specs, CLI verbs) must resolve it through here
    before minting or joining a row. The store historically persisted the
    same logical channel under four id schemes because callers' tokens were
    written through verbatim; this collapses the two schemes that are
    structurally recognizable:

    - ``persona_personainst_x`` (actor-token drift) -> ``personainst_x``
    - ``persona:<persona_id>`` selector tokens (launcher idle-row ids,
      mangled to ``persona_<persona_id>``) -> the persona's canonical
      operator-channel id.

    The legacy ``personainst_operator_<hash>`` scheme is not structurally
    derivable; the store reconciler (persona_instance_identity.py) retires
    those rows and records their aliases.
    """
    token = safe_assignment_token(raw_id)
    if not token:
        return None
    while token.startswith(_ACTOR_TOKEN_DRIFT_PREFIX):
        token = token[len("persona_") :]
    if persona_id:
        persona_token = safe_assignment_token(persona_id)
        if persona_token and token == f"persona_{persona_token}":
            return persona_instance_id_for(persona_id)
    return token


def persona_chat_session_id_for(persona_instance_id: str) -> str:
    normalized = safe_assignment_token(persona_instance_id) or "persona"
    return f"persona_chat_{normalized}_{uuid.uuid4().hex[:12]}"


# A chat session id is, by construction, ``persona_chat_<instance>_<hex>`` (see
# ``persona_chat_session_id_for``) or a legacy ``persona_chat_*`` id. Reusing a
# chat lane must never thread onto a task/worker session id, so the reuse guard
# keys on this prefix.
_PERSONA_CHAT_SESSION_PREFIX = "persona_chat_"

# A minted chat session ends in a bare 12-hex suffix (``uuid4().hex[:12]``); the
# body between the prefix and that suffix is the OWNING instance id. This is the
# same exact-mint discrimination ``agent_chat_open`` uses to keep a sibling's
# session (``persona_chat_<inst>_agent_2_<hex>``) from being swallowed by the
# primary's ``persona_chat_<inst>_`` prefix.
_CHAT_SESSION_HEX_SUFFIX_LEN = 12


def chat_session_owner_instance_id(session_id: str | None) -> str | None:
    """The persona-instance id a minted chat session belongs to, or ``None``.

    ``persona_chat_<instance>_<12 hex>`` → ``<instance>``. A legacy/opaque
    ``persona_chat_*`` id whose tail is not a bare 12-hex block has no derivable
    owner and returns ``None`` (treated as un-owned, never foreign)."""
    token = safe_assignment_text(session_id, limit=200)
    if not token or not token.startswith(_PERSONA_CHAT_SESSION_PREFIX):
        return None
    body = token[len(_PERSONA_CHAT_SESSION_PREFIX) :]
    owner, sep, tail = body.rpartition("_")
    if (
        sep
        and owner
        and len(tail) == _CHAT_SESSION_HEX_SUFFIX_LEN
        and all(ch in "0123456789abcdef" for ch in tail.lower())
    ):
        return owner
    return None


def sender_scope_workspace_id(
    session_id: str | None,
    *,
    instance_store: "PersonaInstanceStore | None" = None,
    active_workspace_id: str | None = None,
) -> str | None:
    """The workspace scope a chat SENDER addresses a target from.

    The single impure derivation shared by every addressable-roster surface that
    resolves a target for a SENDER (mission-chat target guard, ``agent_chat``
    threads/open): session → the owning instance → that instance's own workspace
    pointer (falling back to the active workspace for a runtime-global sender).
    A session with no derivable owner, or no session at all (a bare operator/CLI
    invocation), scopes to the active workspace — so the resolver degrades to the
    active scene rather than hiding the whole roster.

    Pairs with the pure :mod:`agent_runtime.workspace_scope` filters: this
    answers "which workspace am I addressing FROM"; those answer "which rows are
    addressable from that workspace". ``active_workspace_id`` /
    ``instance_store`` are injectable for tests and to reuse a caller's store.
    """

    from .workspace_scope import effective_workspace_id

    if active_workspace_id is None:
        from .store import WorkspaceStore

        active_workspace_id = WorkspaceStore().active_id()
    scope_workspace_id = active_workspace_id
    sender_session = safe_assignment_text(session_id, limit=200)
    if not sender_session:
        return scope_workspace_id
    sender_instance_id = chat_session_owner_instance_id(sender_session)
    if not sender_instance_id:
        return scope_workspace_id
    if instance_store is None:
        instance_store = PersonaInstanceStore()
    try:
        sender_instance = instance_store.get(sender_instance_id)
    except Exception:
        sender_instance = None
    if sender_instance is None:
        return scope_workspace_id
    return effective_workspace_id(sender_instance, active_workspace_id=active_workspace_id)


def chat_session_is_foreign_to_instance(session_id: str | None, instance_id: str | None) -> bool:
    """True when ``session_id`` is a chat session MINTED FOR a DIFFERENT instance.

    Chat sessions encode their owning instance, so a session whose exact-mint
    owner resolves to some OTHER real ``personainst_*`` instance must never be
    adopted as ``instance_id``'s default chat lane nor bound onto its pointer —
    that sibling-session steal is what folded two live QA instances onto one
    operator channel and overwrote the canonical instance's default-chat pointer
    with a placement sibling's session (live 2026-07-18).

    Deliberately narrow to avoid false positives: only an owner that is itself a
    canonical ``personainst_*`` handle counts. Legacy/seed/opaque sessions whose
    middle token is a bare persona id (``persona_chat_qa_<hex>``) or a synthetic
    seed (``persona_chat_seed_<hex>``) have no real sibling to steal from and bind
    freely, as does a session this instance already owns."""
    owner = chat_session_owner_instance_id(session_id)
    if owner is None or not owner.startswith(PERSONA_INSTANCE_ID_PREFIX):
        return False
    target = safe_assignment_token(instance_id)
    return bool(target) and owner != target


def canonical_chat_instance_id(persona_id: str, persona_instance_id: str | None = None) -> str:
    """Canonical persona-instance id a chat lane threads onto.

    One derivation shared by every chat-session resolver here (default resolve,
    non-minting resolve, mint) so an instance-shaped target and a bare persona id
    always land on the SAME instance row — no variant rows, no parallel scheme.
    """
    return (
        canonical_persona_instance_id(persona_instance_id, persona_id=persona_id)
        if persona_instance_id
        else None
    ) or persona_instance_id_for(persona_id)


def resolve_default_chat_session_id_for_instance(
    store: "PersonaInstanceStore",
    *,
    persona_id: str,
    persona_instance_id: str | None = None,
) -> str | None:
    """Return the target's EXISTING default chat session id WITHOUT minting.

    Read the canonical instance pointer and return its bound session ONLY when it
    is a chat-shaped ``persona_chat_*`` session. Returns ``None`` when the target
    has never chatted (or its pointer is a task/worker session) — the honest
    "no thread yet" answer the read verbs (``agent_chat_threads`` /
    ``agent_chat_open``) surface instead of fabricating a session. Never writes.
    """
    instance_id = canonical_chat_instance_id(persona_id, persona_instance_id)
    try:
        existing = store.get(instance_id)
    except Exception:
        existing = None
    if existing is not None:
        existing_session = safe_assignment_text(
            getattr(existing, "default_chat_session_id", None), limit=200
        )
        # Reuse only a chat-shaped session: a task/worker session on the pointer
        # (task_bound mode) is not the persona's chat lane and must never absorb
        # a chat relay's transcript.
        #
        # AND only when it is THIS instance's own session. A pointer poisoned with
        # a SIBLING's session (``persona_chat_<other-instance>_<hex>``) must not be
        # adopted as this instance's default — that adoption is the sibling steal
        # that folded ``personainst_qa`` onto ``personainst_qa_agent_2``'s chat
        # lane (2026-07-18). A foreign pointer falls through to a fresh own mint,
        # self-healing the corrupted pointer on the next send.
        if (
            existing_session
            and existing_session.startswith(_PERSONA_CHAT_SESSION_PREFIX)
            and not chat_session_is_foreign_to_instance(existing_session, instance_id)
        ):
            return existing_session
    return None


def _free_floating_identity(persona_or_template: AgentPersona | str) -> tuple[str, str, str, str | None]:
    if isinstance(persona_or_template, AgentPersona):
        return (
            persona_or_template.id,
            str(persona_or_template.role),
            persona_or_template.display_name,
            persona_or_template.hermes_profile,
        )
    raw = str(persona_or_template or "").strip()
    if raw.lower().startswith("profile:"):
        profile = safe_assignment_token(raw.split(":", 1)[1])
        persona_id = f"profile:{profile}" if profile else "profile:persona"
        return (persona_id, "profile", _display_name_for_template(profile), profile or None)
    persona_id = safe_assignment_token(raw) or "persona"
    return (persona_id, persona_id, persona_id, None)


def _display_name_for_template(profile: str) -> str:
    return " ".join(part.capitalize() for part in profile.replace("_", "-").split("-") if part) or "Profile"


def _normalize_instance_source_persona(persona_or_template_id: str) -> str:
    raw = str(persona_or_template_id or "").strip()
    if raw.lower().startswith("profile:"):
        profile = safe_assignment_token(raw.split(":", 1)[1])
        return f"profile:{profile}" if profile else "profile:persona"
    return safe_assignment_token(raw) or "persona"


def _profile_id_for_persona_or_template(persona_or_template_id: str) -> str | None:
    raw = str(persona_or_template_id or "").strip()
    if raw.lower().startswith("profile:"):
        return safe_assignment_token(raw.split(":", 1)[1]) or None
    return None


# S56 removed ``persona_instance_runtime_enabled`` and
# ``persona_assignment_store_enabled``. Both read
# ``enterprise_worker_sessions``, a config block named for a lane that no longer
# exists, and they gated the persona-instance ROSTER — the identity substrate
# every Mission Control surface keys on. The enabled shape has been the only
# shape for months (the live alice config sets all three fields true), and there
# is no disable consumer: nothing in either repo branches on a false verdict
# except the CLI's own "runtime is disabled" print. Both sections are now
# unconditional; the ``persona_instance_runtime`` WIRE block survives and
# reports the truth. See tests/agent_runtime/test_s56_roster_gate_removal.py.


def persona_instance_summary(
    instance: PersonaInstance,
    persona: AgentPersona | None = None,
    *,
    profile_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = instance.state.value if hasattr(instance.state, "value") else str(instance.state)
    visibility_persona = persona or _profile_visibility_persona(instance)
    profile_id = instance.profile_id or getattr(visibility_persona, "hermes_profile", None)
    skills = (
        list(instance.skill_overrides)
        if instance.skill_overrides is not None
        else list(getattr(visibility_persona, "skills", []) or [])
    )
    tool_options = None
    if visibility_persona is not None:
        tool_options = permission_options_for_chat(
            visibility_persona,
            session_id=instance.default_chat_session_id,
            task_id=instance.current_task_id,
            goal_id=instance.goal_id,
            runtime_root=instance.runtime_root,
        )
        # T9b: this preview is the persona instance's operator CHAT lane, so it
        # must reflect the chat-lane scoping (augmentation + cost cuts + restore
        # knob + registry hygiene) — not the raw effective_toolsets. Lazy import
        # avoids a module-load cycle; the chat-lane authority stays single.
        from .persona_runtime import apply_chat_lane_tool_scope

        apply_chat_lane_tool_scope(
            visibility_persona, tool_options, session_id=instance.default_chat_session_id
        )
    summary = {
        "agent_profile_id": instance.id,
        "agent_profile_display_name": instance.display_name,
        "source_persona_id": instance.persona_id,
        "source_profile_id": profile_id,
        "persona_instance_id": instance.id,
        "persona_id": instance.persona_id,
        "role": instance.role,
        "display_name": instance.display_name,
        "profile_id": profile_id,
        "backing_profile": profile_id,
        "repo_scope_label": getattr(persona, "repo_scope_label", None),
        "skills": skills,
        "skill_overrides": list(instance.skill_overrides) if instance.skill_overrides is not None else None,
        # Instance model-override tier (None = inherit persona live). The
        # effective_* pair is the agent-level value (override or persona
        # default); the config-default tier below persona resolves at runtime.
        "model": instance.model,
        "provider": instance.provider,
        "api_mode": instance.api_mode,
        "model_is_override": bool(instance.model or instance.provider or instance.reasoning_effort),
        "effective_model": instance.model or getattr(visibility_persona, "model", None),
        "effective_provider": instance.provider or getattr(visibility_persona, "provider", None),
        # Per-instance reasoning-effort override (None = inherit runtime default)
        # plus whether the effective model supports reasoning effort at all, so
        # the Launcher only offers the effort control for reasoning-capable
        # models (no fake affordance). Computed offline from the model id.
        "reasoning_effort": instance.reasoning_effort,
        "reasoning_supported": _model_supports_reasoning_effort(
            instance.model or getattr(visibility_persona, "model", None)
        ),
        "toolsets": list(getattr(visibility_persona, "toolsets", []) or []),
        "runtime_root": instance.runtime_root,
        "state": state,
        "lifecycle_mode": instance.mode,
        "mode": instance.mode,
        "goal_id": instance.goal_id,
        "workspace_id": instance.workspace_id,
        "realm_id": instance.realm_id,
        "spawned_by": instance.spawned_by,
        "steered_by": list(instance.steered_by),
        "returned_to": instance.returned_to,
        "current_chat_goal": instance.current_chat_goal,
        "current_work_assignment_id": instance.current_assignment_id,
        "current_assignment_id": instance.current_assignment_id,
        "attached_task_id": instance.current_task_id,
        "current_task_id": instance.current_task_id,
        # S56 removed ``active_worker_session_id`` from this row (contract 47).
        # Its only writer was ``update_from_worker``, which went with the worker
        # session store; the field could never be non-null again.
        "active_run_id": instance.active_run_id,
        "default_chat_session_id": instance.default_chat_session_id,
        "chat_session_id": instance.default_chat_session_id,
        "session_id": instance.default_chat_session_id,
        "context_receipt_id": instance.context_receipt_id,
        "compression_receipt_id": instance.compression_receipt_id,
        "prompt_contract_hash": instance.prompt_contract_hash,
        "skill_manifest_hash": instance.skill_manifest_hash,
        "token_budget_used": instance.token_budget_used,
        "tool_budget_used": instance.tool_budget_used,
        "watchdog_warning_count": instance.watchdog_warning_count,
        "last_heartbeat_at": instance.last_heartbeat_at,
        "updated_at": instance.updated_at,
    }
    if visibility_persona is not None:
        # Residue-slim R2: the heavy tool-detail payloads
        # (``turn_tool_context`` / ``tool_resolution`` / ``permission_state`` /
        # ``blocked_tools`` — ~97% of this row's bytes) leave the wire row behind
        # a typed ``visibility_ref`` pointer and are rebuilt on demand by
        # ``harness persona-instance detail <id> --json``. ``agent_hud_state`` is
        # RETIRED outright (the situational-HUD lane in ``runtime_hud.py`` is the
        # single HUD authority now). The always-visible agents drawer renders only
        # the head SCALARS below, derived at emit from the same tool-visibility
        # resolution (never from the retired hud state).
        tool_resolution = resolve_tool_visibility(
            visibility_persona,
            tool_options,
            profile_readiness=profile_readiness,
        )
        summary["permission_mode"] = tool_resolution.get("permission_mode") or "profile_default"
        summary["mutation_boundary"] = tool_resolution["mutation_boundary"]
        summary["tool_count"] = tool_resolution["final_tool_count"]
        summary["blocked_tools_count"] = len(tool_resolution["blocked_tools"])
        summary["effective_toolsets"] = tool_resolution["effective_toolsets"]
        summary["visibility_ref"] = persona_instance_visibility_ref(instance.id)
    return summary


#: The tool-detail fields R2 evicts from ``persona_instance_summary`` /
#: ``_agent_summary`` and serves on demand. ``agent_hud_state`` is deliberately
#: absent — retired, not evicted.
PERSONA_INSTANCE_VISIBILITY_FIELDS = (
    "tool_resolution",
    "turn_tool_context",
    "permission_state",
    "blocked_tools",
)


def persona_instance_visibility_ref(entity_id: str) -> dict[str, Any]:
    """Typed pointer replacing the evicted tool-detail payloads on a wire row.

    Mirrors the S8 ``detail_ref`` grammar (``evicted`` / id / evicted ``fields`` /
    ``fetch`` verb). The launcher renders an honest fetch affordance and pulls the
    full payloads via the fetch verb when the visibility dialog opens. Shared by
    ``persona_instance_summary`` and ``_agent_summary`` — both evict the same four
    fields and both fetch through ``harness persona-instance detail`` (which
    resolves a persona-instance id OR a persona id)."""

    return {
        "evicted": True,
        "id": entity_id,
        "fields": list(PERSONA_INSTANCE_VISIBILITY_FIELDS),
        "fetch": "harness persona-instance detail <id> --json",
    }


def persona_instance_tool_detail(
    instance: PersonaInstance, persona: AgentPersona | None = None
) -> dict[str, Any] | None:
    """The evicted tool-detail payloads for one persona instance, rebuilt from the
    same tool-visibility resolution ``persona_instance_summary`` used before R2.

    Served by ``harness persona-instance detail`` — the on-demand fetch behind the
    ``visibility_ref`` pointer. Returns ``None`` when no backing persona resolves
    (an honest "unavailable" the launcher surfaces, never a fake-empty payload).
    ``agent_hud_state`` is intentionally NOT rebuilt here (retired)."""

    visibility_persona = persona or _profile_visibility_persona(instance)
    if visibility_persona is None:
        return None
    tool_options = permission_options_for_chat(
        visibility_persona,
        session_id=instance.default_chat_session_id,
        task_id=instance.current_task_id,
        goal_id=instance.goal_id,
        runtime_root=instance.runtime_root,
    )
    # T9b: this on-demand tool detail is the persona instance's operator CHAT
    # lane — scope the preview to it (see apply_chat_lane_tool_scope).
    from .persona_runtime import apply_chat_lane_tool_scope

    apply_chat_lane_tool_scope(
        visibility_persona, tool_options, session_id=instance.default_chat_session_id
    )
    tool_resolution = resolve_tool_visibility(visibility_persona, tool_options)
    return {
        "persona_instance_id": instance.id,
        "persona_id": instance.persona_id,
        "display_name": instance.display_name,
        "tool_resolution": tool_resolution,
        "turn_tool_context": turn_tool_context_for_persona(
            visibility_persona, tool_options, visibility=tool_resolution
        ),
        "permission_state": permission_state_for_persona(
            visibility_persona, tool_options, visibility=tool_resolution
        ),
        "blocked_tools": tool_resolution["blocked_tools"],
    }


def active_persona_instance_agent_summaries(
    instances: list[PersonaInstance],
    personas_by_id: dict[str, AgentPersona] | None = None,
    readiness_by_persona_id: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    personas_by_id = personas_by_id or {}
    readiness_by_persona_id = readiness_by_persona_id or {}
    for instance in instances:
        instance_id = safe_assignment_token(getattr(instance, "id", None))
        if not instance_id or instance_id in seen:
            continue
        if not _persona_instance_is_active_lane(instance):
            continue
        persona_id = safe_assignment_token(getattr(instance, "persona_id", None)) or instance_id
        row = persona_instance_summary(
            instance,
            personas_by_id.get(persona_id),
            profile_readiness=readiness_by_persona_id.get(persona_id),
        )
        row["runtime_agent_kind"] = "persona_instance"
        row["source_persona_id"] = persona_id
        row["persona_id"] = instance_id
        row["agent_profile_id"] = instance_id
        row["persona_instance_id"] = instance_id
        row["base_persona_id"] = persona_id
        row["display_name"] = row.get("display_name") or instance_id
        seen.add(instance_id)
        rows.append(row)
    return rows


def _persona_instance_is_active_lane(instance: PersonaInstance) -> bool:
    state = getattr(instance, "state", None)
    state_text = state.value if hasattr(state, "value") else str(state or "")
    if state_text in {"running", "assigned", "waiting_on_tool", "waiting_on_proof", "self_healing", "waiting_on_human", "possessed"}:
        return True
    return any(
        bool(getattr(instance, attr, None))
        for attr in ("current_task_id", "goal_id", "current_assignment_id", "active_run_id")
    )


def _profile_visibility_persona(instance: PersonaInstance) -> AgentPersona | None:
    profile_id = (instance.profile_id or "").strip()
    persona_id = (instance.persona_id or "").strip()
    if not profile_id and not persona_id.lower().startswith("profile:"):
        return None
    if not profile_id and persona_id.lower().startswith("profile:"):
        profile_id = persona_id.split(":", 1)[1].strip()
    resolved_persona_id = persona_id or (f"profile:{profile_id}" if profile_id else "profile:unknown")
    display_name = instance.display_name or _display_name_for_template(profile_id or resolved_persona_id)
    try:
        from .config import ensure_persisted_personas, load_agent_runtime_config

        persisted_personas = list(ensure_persisted_personas(load_agent_runtime_config()))
    except Exception:
        persisted_personas = []
    configured = next(
        (
            candidate
            for candidate in persisted_personas
            if safe_assignment_token(getattr(candidate, "id", None))
            == safe_assignment_token(resolved_persona_id)
            or (
                profile_id
                and safe_assignment_token(getattr(candidate, "hermes_profile", None))
                == safe_assignment_token(profile_id)
            )
        ),
        None,
    )
    if configured is not None:
        return replace(
            configured,
            id=resolved_persona_id,
            display_name=display_name,
            hermes_profile=profile_id or configured.hermes_profile,
            skills=(
                list(instance.skill_overrides)
                if instance.skill_overrides is not None
                else list(configured.skills)
            ),
        )
    return AgentPersona(
        id=resolved_persona_id,
        display_name=display_name,
        role=instance.role,
        model=instance.model,
        provider=instance.provider,
        api_mode=instance.api_mode,
        toolsets=profile_chat_toolsets(profile_id, persisted_personas),
        system_prompt_path="",
        hermes_profile=profile_id or None,
        skills=list(instance.skill_overrides or []),
    )


def persona_assignment_summary(assignment: PersonaAssignment) -> dict[str, Any]:
    return {
        "agent_profile_id": assignment.persona_instance_id,
        "assignment_id": assignment.id,
        "persona_instance_id": assignment.persona_instance_id,
        "persona_id": assignment.persona_id,
        "kind": assignment.kind,
        "state": assignment.state,
        "title": assignment.title,
        "message": assignment.message,
        "task_id": assignment.task_id,
        "goal_id": assignment.goal_id,
        "stage_id": assignment.stage_id,
        "operation_id": assignment.operation_id,
        "repo_bundle_id": assignment.repo_bundle_id,
        "repo": assignment.repo,
        "affected_paths": list(assignment.affected_paths or []),
        "proof_targets": list(assignment.proof_targets or []),
        "acceptance": list(assignment.acceptance or []),
        "non_goals": list(assignment.non_goals or []),
        "allowed_decisions": list(assignment.allowed_decisions or []),
        "allowed_tools": list(assignment.allowed_tools or []),
        "run_ids": list(assignment.run_ids or []),
        "proof_ids": list(assignment.proof_ids or []),
        "context_receipt_ids": list(assignment.context_receipt_ids or []),
        "evidence_kind": assignment.evidence_kind,
        "production_proof_eligible": bool(assignment.production_proof_eligible),
        "archive_scope": assignment.archive_scope,
        "client_message_id": assignment.client_message_id,
        "created_by": assignment.created_by,
        "created_at": assignment.created_at,
        "updated_at": assignment.updated_at,
        "completed_at": assignment.completed_at,
        "last_error": assignment.last_error,
        "signal_hash": assignment.signal_hash,
    }



def assignment_evidence_kind(kind: str | None) -> str:
    normalized = safe_assignment_token(kind)
    if normalized == "diagnostic":
        return "diagnostic"
    if normalized.startswith("free_floating"):
        return "free_floating"
    return "task_bound"


def assignment_archive_scope(kind: str | None) -> str:
    evidence_kind = assignment_evidence_kind(kind)
    if evidence_kind == "free_floating":
        return "assignment"
    return "task"


def assignment_signal_hash(assignment: PersonaAssignment) -> str:
    return assignment_signal_hash_from_parts(
        persona_id=assignment.persona_id,
        goal_id=assignment.goal_id,
        stage_id=assignment.stage_id,
        kind=assignment.kind,
        repo_bundle_id=assignment.repo_bundle_id,
        repo=assignment.repo,
        affected_paths=assignment.affected_paths,
        proof_targets=assignment.proof_targets,
        message=assignment.message,
    )


def assignment_signal_hash_from_parts(*, persona_id: str | None, goal_id: str | None, stage_id: str | None, kind: str | None, repo: str | None, affected_paths: list[str] | None, proof_targets: list[str] | None, message: str | None, repo_bundle_id: str | None = None) -> str:
    payload = {
        "persona_id": safe_assignment_token(persona_id),
        "goal_id": safe_optional_token(goal_id),
        "stage_id": safe_optional_token(stage_id),
        "kind": safe_assignment_token(kind),
        "repo_bundle_id": safe_optional_token(repo_bundle_id),
        "repo": safe_assignment_text(repo or "", limit=160),
        "affected_paths": sorted(safe_assignment_text(item, limit=240) for item in (affected_paths or []) if safe_assignment_text(item, limit=240)),
        "proof_targets": sorted(safe_assignment_text(item, limit=240) for item in (proof_targets or []) if safe_assignment_text(item, limit=240)),
        "message": safe_assignment_text(message or "", limit=4000),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def safe_assignment_token(value: Any) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in str(value or "").strip())
    return text.strip("._-")[:120]


def safe_optional_token(value: Any) -> str | None:
    token = safe_assignment_token(value)
    return token or None


def _dedupe_tokens(values: list[str] | None) -> list[str]:
    """Normalize + de-duplicate a parent-id list, preserving first-seen order.

    The first surviving token is treated as the PRIMARY parent everywhere
    (the ``spawned_by`` mirror, the projection's home owner), so order matters.
    """
    seen: set[str] = set()
    result: list[str] = []
    for value in values or []:
        token = safe_optional_token(value)
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return result


def safe_assignment_state(value: str) -> str:
    state = safe_assignment_token(value)
    if state in ACTIVE_ASSIGNMENT_STATES or state in TERMINAL_ASSIGNMENT_STATES:
        return state
    return "queued"


def _safe_skill_overrides(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        skill = safe_assignment_token(value)
        if not skill or skill in seen:
            continue
        seen.add(skill)
        result.append(skill)
    return result[:40]


def safe_assignment_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    return text[:limit]
