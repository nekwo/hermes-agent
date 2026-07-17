from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .errors import AgentRuntimeError
from .events import EventLog
from .models import AgentPersona, Event, PersonaAssignment, PersonaInstance, WorkerSession
from .personas import profile_chat_toolsets
from .serde import from_jsonable, to_jsonable
from .state_patches import emit_persona_instance_patch, emit_persona_instance_remove
from .states import RunState, WorkerSessionState
from .tool_visibility import (
    agent_hud_state_for_persona,
    permission_state_for_persona,
    resolve_tool_visibility,
    turn_tool_context_for_persona,
)
from .tool_permissions import permission_options_for_chat

TERMINAL_ASSIGNMENT_STATES = frozenset({"completed", "blocked", "cancelled"})
_RELEASABLE_OWNER_TASK_STATES = frozenset({"done", "cancelled", "failed"})


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
ACTIVE_PERSONA_WORKER_STATES = frozenset(
    {
        WorkerSessionState.ASSIGNED,
        WorkerSessionState.RUNNING,
        WorkerSessionState.WAITING_ON_TOOL,
        WorkerSessionState.WAITING_ON_PROOF,
        WorkerSessionState.SELF_HEALING,
        WorkerSessionState.WAITING_ON_HUMAN,
        WorkerSessionState.POSSESSED,
    }
)
LIVE_RUN_STATES = frozenset(
    {
        RunState.QUEUED,
        RunState.STARTING,
        RunState.RUNNING,
        RunState.WAITING_ON_TOOL,
        RunState.WAITING_ON_APPROVAL,
    }
)


def _worker_carries_live_binding(worker: WorkerSession) -> bool:
    """Whether a worker may stamp its ``task_bound`` binding onto the persona
    instance during snapshot derivation.

    The binding follows the TASK's life, not the worker's: an idle worker
    between ticks of a live task keeps the persona attached (agents must not
    flicker off the goal topology between runs), but once the owning task is
    terminal or archived the worker is history — it must not resurrect the
    binding. Dead workers used to be picked as ``latest_by_persona`` and
    re-stamp a settled mission's task/session onto the instance on every
    snapshot build (undoing the terminal-task reaper and orphan sweep), so
    Mission Control opened the persona's console on an empty dead mission
    session instead of the latest chat (2026-07-08). A persona whose latest
    worker is dead now falls through to the configured/idle reset below
    instead. Precedence mirrors ``sweep_orphaned_task_bound_instances``: an
    actively working session always carries (even mid-setup before its task
    file lands); a non-active worker carries only while its task is live."""
    if worker.state in ACTIVE_PERSONA_WORKER_STATES:
        return True
    task_id = safe_optional_token(worker.task_id)
    return bool(task_id) and _owning_task_release_state(task_id) is None


class ChatBusyError(AgentRuntimeError):
    def __init__(self, instance: PersonaInstance, *, active_run_id: str | None, active_worker_session_id: str | None):
        super().__init__("chat_busy")
        self.instance = instance
        self.active_run_id = active_run_id
        self.active_worker_session_id = active_worker_session_id


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
    task_id: str | None = None
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
        updates = {
            "mode": "task_bound",
            "current_task_id": normalized_goal,
            "goal_id": normalized_goal,
            "spawned_by": normalized_spawned_by,
            # Keep the authoritative parent set in sync with the spawn parent at
            # creation, so the on-disk record is self-consistent (not only healed
            # by the read-time __post_init__ backfill).
            "steered_by": [normalized_spawned_by] if normalized_spawned_by else [],
        }
        for attr, value in updates.items():
            if getattr(instance, attr) != value:
                setattr(instance, attr, value)
                changed = True
        if changed:
            instance.updated_at = now()
            self._write(instance)
            self._event("persona_instance.attributed", instance, {"goal_id": instance.goal_id, "spawned_by": instance.spawned_by})
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

    def list_for_task(self, task_id: str, *, goal_id: str | None = None) -> list[PersonaInstance]:
        normalized = safe_optional_token(task_id)
        normalized_goal = safe_optional_token(goal_id)
        wanted = {item for item in (normalized, normalized_goal) if item}
        if not wanted:
            return []
        return [
            instance
            for instance in self.list_all()
            if instance.mode == "task_bound"
            and (
                safe_optional_token(instance.current_task_id) in wanted
                or safe_optional_token(instance.goal_id) in wanted
            )
        ]

    def close_for_task(
        self,
        task_id: str,
        *,
        goal_id: str | None = None,
        reason: str = "task terminal",
    ) -> dict[str, Any]:
        """Remove task-bound persona instances from the live graph.

        Task-bound persona instances are a live projection of worker/goal
        membership, not an archive of completed work. Leaving them in the live
        store after terminal transition makes snapshots and the Launcher graph
        render old workers as live agents. Never reap an instance while its
        active run or worker still resolves to a live record.
        """
        reaped: list[str] = []
        skipped_active: list[str] = []
        for instance in self.list_for_task(task_id, goal_id=goal_id):
            if self._has_live_binding(instance):
                skipped_active.append(instance.id)
                continue
            if self._delete(instance):
                reaped.append(instance.id)
                self._event(
                    "persona_instance.reaped",
                    instance,
                    {
                        "task_id": safe_optional_token(task_id),
                        "goal_id": safe_optional_token(goal_id),
                        "reason": safe_assignment_text(reason, limit=240),
                    },
                )
                # S7-A producer: the instance left the active frame, so its keyed
                # row is a REMOVE the launcher deletes (never a stale live agent).
                # Dark by default (read_model.delta_patches off).
                emit_persona_instance_remove(self.event_log, instance, reason=reason)
        remaining = self.list_for_task(task_id, goal_id=goal_id)
        return {
            "task_id": safe_optional_token(task_id),
            "goal_id": safe_optional_token(goal_id),
            "reaped_persona_instance_ids": reaped,
            "skipped_active_persona_instance_ids": skipped_active,
            "remaining_task_bound_persona_instance_ids": [instance.id for instance in remaining],
            "reaped_count": len(reaped),
            "skipped_active_count": len(skipped_active),
            "remaining_count": len(remaining),
        }

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

    def update(self, instance: PersonaInstance) -> PersonaInstance:
        instance.updated_at = now()
        self._write(instance)
        return self.get(instance.id)

    def open_chat(
        self,
        *,
        persona_id: str,
        session_id: str,
        persona_instance_id: str | None = None,
        display_name: str | None = None,
        profile_id: str | None = None,
        kill_active: bool = False,
    ) -> PersonaInstance:
        """Bind a persona instance to a durable chat session without running a turn.

        Persona instances are intentionally chat-shaped: selecting an old chat can
        re-open the same persona instance history by rebinding the instance to the
        stored session id, while the normal send/resume path owns the actual LLM
        execution. This helper is a state transition only; it never fabricates a
        task, worker, run, or transcript.
        """
        normalized_persona = _normalize_instance_source_persona(persona_id)
        normalized_session = safe_assignment_text(session_id, limit=200)
        if not normalized_persona:
            raise ValueError("persona_id is required")
        if not normalized_session:
            raise ValueError("session_id is required")

        normalized_instance = (
            canonical_persona_instance_id(persona_instance_id, persona_id=normalized_persona)
            if persona_instance_id
            else None
        )
        instance_id = normalized_instance or persona_instance_id_for(normalized_persona)
        safe_display_name = safe_assignment_text(display_name, limit=120) if display_name is not None else None
        safe_profile_id = safe_assignment_token(profile_id) if profile_id is not None else None
        try:
            instance = self.get(instance_id)
        except Exception:
            ts = now()
            role = "profile" if normalized_persona.startswith("profile:") else normalized_persona
            instance = PersonaInstance(
                id=instance_id,
                persona_id=normalized_persona,
                role=role,
                display_name=safe_display_name or _display_name_for_template(normalized_persona.split(":", 1)[1] if normalized_persona.startswith("profile:") else normalized_persona),
                profile_id=safe_profile_id or (normalized_persona.split(":", 1)[1] if normalized_persona.startswith("profile:") else None),
                runtime_root=str(paths.store_root()),
                state=WorkerSessionState.IDLE,
                updated_at=ts,
            )
        else:
            self._guard_or_replace_chat(instance, kill_active=kill_active)

        if safe_display_name:
            instance.display_name = safe_display_name
        if safe_profile_id:
            instance.profile_id = safe_profile_id
        elif normalized_persona.startswith("profile:") and not instance.profile_id:
            instance.profile_id = normalized_persona.split(":", 1)[1]
        instance.mode = "chat"
        instance.session_id = normalized_session
        instance.current_assignment_id = None
        instance.current_task_id = None
        instance.active_worker_session_id = None
        instance.active_run_id = None
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
        session_id: str | None = None,
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
        return self.open_chat(
            persona_id=normalized_persona,
            persona_instance_id=instance_id,
            session_id=normalized_session or persona_chat_session_id_for(instance_id),
            display_name=display_name,
            profile_id=_profile_id_for_persona_or_template(normalized_persona),
            kill_active=False,
        )

    def _session_owned_by_other_instance(self, session_id: str, instance_id: str) -> bool:
        for instance in self.list_all():
            if instance.id != instance_id and instance.session_id == session_id:
                return True
        return False

    def _guard_or_replace_chat(self, instance: PersonaInstance, *, kill_active: bool) -> None:
        active_run_id, active_worker_session_id = _live_chat_bindings(instance)
        if not active_run_id and not active_worker_session_id:
            return
        if not kill_active:
            raise ChatBusyError(
                instance,
                active_run_id=active_run_id,
                active_worker_session_id=active_worker_session_id,
            )
        _terminate_live_chat_bindings(
            active_run_id=active_run_id,
            active_worker_session_id=active_worker_session_id,
        )

    def update_from_worker(self, worker: WorkerSession) -> PersonaInstance:
        instance_id = persona_instance_id_for(worker.persona_id)
        try:
            instance = self.get(instance_id)
        except Exception:
            instance = PersonaInstance(
                id=instance_id,
                persona_id=worker.persona_id,
                role=worker.role,
                display_name=worker.display_name,
                profile_id=None,
                runtime_root=str(paths.store_root()),
                state=worker.state,
            )
        instance.role = worker.role
        instance.display_name = worker.display_name
        instance.state = worker.state
        instance.mode = "task_bound"
        instance.current_assignment_id = worker.current_assignment_id
        instance.current_task_id = worker.task_id
        instance.goal_id = self._goal_id_for_worker(worker) or worker.task_id
        instance.active_worker_session_id = worker.id if worker.state in ACTIVE_PERSONA_WORKER_STATES else None
        instance.active_run_id = worker.active_run_id
        instance.session_id = worker.session_id
        instance.context_receipt_id = worker.context_receipt_id
        instance.compression_receipt_id = worker.compression_receipt_id
        instance.prompt_contract_hash = worker.prompt_contract_hash
        instance.skill_manifest_hash = worker.skill_manifest_hash
        instance.token_budget_used = worker.token_budget_used
        instance.tool_budget_used = worker.tool_budget_used
        instance.watchdog_warning_count = worker.watchdog_warning_count
        instance.last_heartbeat_at = worker.last_heartbeat_at
        return self.update(instance)

    def _goal_id_for_worker(self, worker: WorkerSession) -> str | None:
        assignment_id = safe_optional_token(worker.current_assignment_id)
        if not assignment_id:
            return None
        try:
            assignment = PersonaAssignmentStore().get(assignment_id)
        except Exception:
            return None
        return safe_optional_token(assignment.goal_id or assignment.task_id)

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

    def derive_from_workers(self, personas: list[AgentPersona], workers: list[WorkerSession]) -> list[PersonaInstance]:
        for persona in personas:
            self.ensure_for_persona(persona)
        latest_by_persona: dict[str, WorkerSession] = {}
        for worker in workers:
            if not _worker_carries_live_binding(worker):
                continue
            existing = latest_by_persona.get(worker.persona_id)
            if existing is None or (worker.last_heartbeat_at or worker.opened_at) >= (existing.last_heartbeat_at or existing.opened_at):
                latest_by_persona[worker.persona_id] = worker
        for worker in latest_by_persona.values():
            self.update_from_worker(worker)
        for persona in personas:
            if persona.id in latest_by_persona:
                continue
            instance = self.ensure_for_persona(persona)
            if instance.mode in {"chat", "free_floating"}:
                continue
            if (
                instance.state != WorkerSessionState.IDLE
                or instance.current_assignment_id
                or instance.current_task_id
                or instance.active_worker_session_id
                or instance.active_run_id
                or instance.session_id
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
                instance.active_worker_session_id = None
                instance.active_run_id = None
                instance.session_id = None
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
        path.unlink()
        return True

    def _has_live_binding(self, instance: PersonaInstance) -> bool:
        worker_id = safe_optional_token(instance.active_worker_session_id)
        if worker_id:
            try:
                from .worker_sessions import ACTIVE_WORKER_STATES, WorkerSessionStore

                worker = WorkerSessionStore(event_log=self.event_log).get(worker_id)
                if worker.state in ACTIVE_WORKER_STATES:
                    return True
            except Exception:
                pass
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
        goal_id = safe_optional_token(spec.goal_id or spec.task_id)
        instance_id = canonical_persona_instance_id(spec.persona_instance_id, persona_id=persona_id) or (
            persona_instance_id_for_placement(f"{goal_id}:{persona_id}") if goal_id else persona_instance_id_for(persona_id)
        )
        signal_hash = assignment_signal_hash_from_parts(
            persona_id=persona_id,
            task_id=spec.task_id,
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
        for existing in self.find_active(persona_id=persona_id, task_id=spec.task_id, stage_id=spec.stage_id, kind=spec.kind):
            if existing.signal_hash == signal_hash:
                return existing
        ts = now()
        evidence_kind = assignment_evidence_kind(spec.kind)
        archive_scope = assignment_archive_scope(spec.kind, task_id=spec.task_id)
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
                task_id=safe_optional_token(spec.task_id),
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

    def contention_warnings(self, *, persona_id: str | None = None, goal_id: str | None = None, task_id: str | None = None) -> list[dict[str, Any]]:
        wanted_persona = safe_assignment_token(persona_id) if persona_id else None
        wanted_goal = safe_optional_token(goal_id or task_id)
        warnings: list[dict[str, Any]] = []
        for assignment in self.list_all():
            if assignment.state not in ACTIVE_ASSIGNMENT_STATES:
                continue
            if wanted_persona and assignment.persona_id != wanted_persona:
                continue
            existing_goal = safe_optional_token(assignment.goal_id or assignment.task_id)
            if wanted_goal and existing_goal == wanted_goal:
                continue
            if self._release_if_owning_goal_terminal(assignment):
                # Self-heal instead of warn: an assignment held by a goal that is
                # already terminal (or archived out of the live store) is stale
                # state, not real contention. Release it so the warning stays an
                # honest signal of genuinely concurrent goals.
                continue
            warnings.append(
                {
                    "code": "agent_already_assigned",
                    "message": f"{assignment.persona_id} already has an active assignment on another goal.",
                    "persona_id": assignment.persona_id,
                    "persona_instance_id": assignment.persona_instance_id,
                    "assignment_id": assignment.id,
                    "goal_id": existing_goal,
                    "retryable": False,
                }
            )
        return warnings

    def _release_if_owning_goal_terminal(self, assignment: PersonaAssignment) -> bool:
        """Release an active assignment whose owning goal is terminal/archived.

        Returns True when the assignment was released. Free-floating assignments
        (no owning task/goal) are never touched — their staleness cannot be
        inferred from task state.
        """
        owner = safe_optional_token(assignment.task_id or assignment.goal_id)
        if not owner:
            return False
        owner_state = _owning_task_release_state(owner)
        if owner_state is None:
            return False
        self.complete(
            assignment.id,
            state="completed" if owner_state in {"done", "archived"} else "cancelled",
            error=f"released stale assignment; owning goal is {owner_state}",
        )
        return True

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

    def list_for_task(self, task_id: str) -> list[PersonaAssignment]:
        normalized = safe_optional_token(task_id)
        return [assignment for assignment in self.list_all() if assignment.task_id == normalized]

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

    def find_active(self, *, persona_id: str | None = None, task_id: str | None = None, stage_id: str | None = None, kind: str | None = None) -> list[PersonaAssignment]:
        wanted_persona = safe_assignment_token(persona_id) if persona_id else None
        wanted_task = safe_optional_token(task_id) if task_id else None
        wanted_stage = safe_optional_token(stage_id) if stage_id else None
        wanted_kind = safe_assignment_token(kind) if kind else None
        return [
            assignment
            for assignment in self.list_all()
            if assignment.state in ACTIVE_ASSIGNMENT_STATES
            and (wanted_persona is None or assignment.persona_id == wanted_persona)
            and (wanted_task is None or assignment.task_id == wanted_task)
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

    def close_for_task(self, task_id: str, *, state: str = "completed", reason: str | None = None) -> list[str]:
        """Close every still-active assignment bound to a task/goal.

        Persona-instance assignments are otherwise only released on *archival*
        (the files are moved out of the live dir). A task that reaches a
        terminal state but is not archived keeps its slots ``active``, so
        ``find_active``/``contention_warnings`` keep emitting
        ``agent_already_assigned`` and a fresh goal can never claim the persona
        — the "graveyard starvation" that wedges new goals at finalization.
        Closing on the terminal transition prevents that.
        """
        normalized = safe_optional_token(task_id)
        if not normalized:
            return []
        closed: list[str] = []
        for assignment in self.list_all():
            if assignment.state in TERMINAL_ASSIGNMENT_STATES:
                continue
            if safe_optional_token(assignment.task_id) != normalized and safe_optional_token(assignment.goal_id) != normalized:
                continue
            self.complete(assignment.id, state=state, error=reason)
            closed.append(assignment.id)
        return closed

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


PERSONA_INSTANCE_ID_PREFIX = "personainst_"

# An operator-channel actor token ('persona_' + instance id) that leaked into
# a store row id. Live evidence 2026-07-10: persona_personainst_neko_supervisor
# persisted beside personainst_neko_supervisor for the same channel.
_ACTOR_TOKEN_DRIFT_PREFIX = f"persona_{PERSONA_INSTANCE_ID_PREFIX}"


def persona_instance_id_for(persona_id: str) -> str:
    return f"{PERSONA_INSTANCE_ID_PREFIX}{safe_assignment_token(persona_id) or 'persona'}"


def persona_instance_id_for_placement(placement_id: str) -> str:
    return f"{PERSONA_INSTANCE_ID_PREFIX}{safe_assignment_token(placement_id) or 'persona'}"


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


def _live_chat_bindings(instance: PersonaInstance) -> tuple[str | None, str | None]:
    active_run_id = None
    active_worker_session_id = None
    if instance.active_run_id:
        try:
            from .store import RunStore

            run = RunStore().get(instance.active_run_id)
            if run.state in LIVE_RUN_STATES:
                active_run_id = run.id
        except Exception:
            active_run_id = instance.active_run_id
    if instance.active_worker_session_id:
        try:
            from .worker_sessions import ACTIVE_WORKER_STATES, WorkerSessionStore

            worker = WorkerSessionStore().get(instance.active_worker_session_id)
            if worker.state in ACTIVE_WORKER_STATES:
                active_worker_session_id = worker.id
        except Exception:
            active_worker_session_id = instance.active_worker_session_id
    return active_run_id, active_worker_session_id


def _terminate_live_chat_bindings(*, active_run_id: str | None, active_worker_session_id: str | None) -> None:
    if active_run_id:
        from .store import RunStore

        RunStore().cancel(active_run_id, reason="operator replaced active persona chat")
    if active_worker_session_id:
        from .worker_sessions import WorkerSessionState, WorkerSessionStore

        WorkerSessionStore().close(
            active_worker_session_id,
            reason="operator replaced active persona chat",
            state=WorkerSessionState.CLOSED,
        )


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


def persona_instance_runtime_enabled(config) -> bool:
    enterprise = getattr(config, "enterprise_worker_sessions", None)
    return bool(getattr(enterprise, "enabled", False) and getattr(enterprise, "persona_instance_runtime", False))


def persona_assignment_store_enabled(config) -> bool:
    enterprise = getattr(config, "enterprise_worker_sessions", None)
    return bool(getattr(enterprise, "enabled", False) and getattr(enterprise, "persona_assignment_store", False))


def persona_instance_summary(instance: PersonaInstance, persona: AgentPersona | None = None) -> dict[str, Any]:
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
            session_id=instance.session_id,
            task_id=instance.current_task_id,
            goal_id=instance.goal_id,
            runtime_root=instance.runtime_root,
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
        "spawned_by": instance.spawned_by,
        "steered_by": list(instance.steered_by),
        "returned_to": instance.returned_to,
        "current_chat_goal": instance.current_chat_goal,
        "current_work_assignment_id": instance.current_assignment_id,
        "current_assignment_id": instance.current_assignment_id,
        "attached_task_id": instance.current_task_id,
        "current_task_id": instance.current_task_id,
        "active_worker_session_id": instance.active_worker_session_id,
        "active_run_id": instance.active_run_id,
        "chat_session_id": instance.session_id,
        "session_id": instance.session_id,
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
        summary["tool_resolution"] = resolve_tool_visibility(visibility_persona, tool_options)
        summary["turn_tool_context"] = turn_tool_context_for_persona(visibility_persona, tool_options)
        summary["permission_state"] = permission_state_for_persona(visibility_persona, tool_options)
        summary["agent_hud_state"] = agent_hud_state_for_persona(visibility_persona, tool_options)
        summary["blocked_tools"] = summary["tool_resolution"]["blocked_tools"]
        summary["blocked_tools_count"] = len(summary["blocked_tools"])
        summary["effective_toolsets"] = summary["tool_resolution"]["effective_toolsets"]
    return summary


def active_persona_instance_agent_summaries(
    instances: list[PersonaInstance],
    personas_by_id: dict[str, AgentPersona] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    personas_by_id = personas_by_id or {}
    for instance in instances:
        instance_id = safe_assignment_token(getattr(instance, "id", None))
        if not instance_id or instance_id in seen:
            continue
        if not _persona_instance_is_active_lane(instance):
            continue
        persona_id = safe_assignment_token(getattr(instance, "persona_id", None)) or instance_id
        row = persona_instance_summary(instance, personas_by_id.get(persona_id))
        row["runtime_agent_kind"] = "persona_instance"
        row["source_persona_id"] = persona_id
        row["persona_id"] = instance_id
        row["agent_profile_id"] = instance_id
        row["persona_instance_id"] = instance_id
        row["base_persona_id"] = persona_id
        row["display_name"] = row.get("display_name") or instance_id
        row["agent_hud_state"] = row.get("agent_hud_state") or {}
        row["tool_resolution"] = row.get("tool_resolution") or {}
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
        for attr in ("current_task_id", "goal_id", "current_assignment_id", "active_worker_session_id", "active_run_id")
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
    return AgentPersona(
        id=resolved_persona_id,
        display_name=display_name,
        role="alice_supervisor",
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=profile_chat_toolsets(profile_id, persisted_personas),
        system_prompt_path="",
        autonomy="propose_only",
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


def assignment_archive_scope(kind: str | None, *, task_id: str | None) -> str:
    evidence_kind = assignment_evidence_kind(kind)
    if evidence_kind == "free_floating" and not safe_optional_token(task_id):
        return "assignment"
    if evidence_kind == "diagnostic":
        return "task"
    return "task" if safe_optional_token(task_id) else "assignment"


def assignment_signal_hash(assignment: PersonaAssignment) -> str:
    return assignment_signal_hash_from_parts(
        persona_id=assignment.persona_id,
        task_id=assignment.task_id,
        stage_id=assignment.stage_id,
        kind=assignment.kind,
        repo_bundle_id=assignment.repo_bundle_id,
        repo=assignment.repo,
        affected_paths=assignment.affected_paths,
        proof_targets=assignment.proof_targets,
        message=assignment.message,
    )


def assignment_signal_hash_from_parts(*, persona_id: str | None, task_id: str | None, stage_id: str | None, kind: str | None, repo: str | None, affected_paths: list[str] | None, proof_targets: list[str] | None, message: str | None, repo_bundle_id: str | None = None) -> str:
    payload = {
        "persona_id": safe_assignment_token(persona_id),
        "task_id": safe_optional_token(task_id),
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
