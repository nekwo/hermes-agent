from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .events import EventLog
from .models import AgentPersona, Event, PersonaAssignment, PersonaInstance, WorkerSession
from .serde import from_jsonable, to_jsonable
from .states import RunState, WorkerSessionState

TERMINAL_ASSIGNMENT_STATES = frozenset({"completed", "blocked", "cancelled"})
ACTIVE_ASSIGNMENT_STATES = frozenset({"queued", "assigned", "running", "waiting_on_tool", "waiting_on_proof", "needs_input"})
LIVE_RUN_STATES = frozenset(
    {
        RunState.QUEUED,
        RunState.STARTING,
        RunState.RUNNING,
        RunState.WAITING_ON_TOOL,
        RunState.WAITING_ON_APPROVAL,
    }
)


class ChatBusyError(RuntimeError):
    def __init__(self, instance: PersonaInstance, *, active_run_id: str | None, active_worker_session_id: str | None):
        super().__init__("chat_busy")
        self.instance = instance
        self.active_run_id = active_run_id
        self.active_worker_session_id = active_worker_session_id


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

    def get(self, persona_instance_id: str) -> PersonaInstance:
        raw = json.loads(paths.persona_instance_path(persona_instance_id).read_text(encoding="utf-8"))
        return from_jsonable(PersonaInstance, raw)

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

        normalized_instance = safe_assignment_token(persona_instance_id) if persona_instance_id else None
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
        return self.open_chat(
            persona_id=normalized_persona,
            persona_instance_id=instance_id,
            session_id=session_id or persona_chat_session_id_for(instance_id),
            display_name=display_name,
            profile_id=_profile_id_for_persona_or_template(normalized_persona),
            kill_active=False,
        )

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
        instance.active_worker_session_id = worker.id
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

    def _write(self, instance: PersonaInstance) -> None:
        path = paths.persona_instance_path(instance.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, to_jsonable(instance), indent=2, sort_keys=True)

    def _event(self, event_type: str, instance: PersonaInstance, payload: dict[str, Any]) -> None:
        self.event_log.append(Event(ts=now(), type=event_type, task_id=instance.current_task_id, run_id=instance.active_run_id, persona_id=instance.persona_id, payload={**payload, "persona_instance_id": instance.id}))


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
        instance_id = spec.persona_instance_id or persona_instance_id_for(persona_id)
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
                goal_id=safe_optional_token(spec.goal_id),
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

    def list_for_task(self, task_id: str) -> list[PersonaAssignment]:
        normalized = safe_optional_token(task_id)
        return [assignment for assignment in self.list_all() if assignment.task_id == normalized]

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
                },
            )
        )


def persona_instance_id_for(persona_id: str) -> str:
    return f"personainst_{safe_assignment_token(persona_id) or 'persona'}"


def persona_instance_id_for_placement(placement_id: str) -> str:
    return f"personainst_{safe_assignment_token(placement_id) or 'persona'}"


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
    profile_id = instance.profile_id or getattr(persona, "hermes_profile", None)
    return {
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
        "skills": list(getattr(persona, "skills", []) or []),
        "toolsets": list(getattr(persona, "toolsets", []) or []),
        "runtime_root": instance.runtime_root,
        "state": state,
        "lifecycle_mode": instance.mode,
        "mode": instance.mode,
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


def safe_assignment_state(value: str) -> str:
    state = safe_assignment_token(value)
    if state in ACTIVE_ASSIGNMENT_STATES or state in TERMINAL_ASSIGNMENT_STATES:
        return state
    return "queued"


def safe_assignment_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    return text[:limit]
