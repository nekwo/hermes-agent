from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from datetime import timedelta
from pathlib import Path
from typing import TypeVar

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .errors import AlreadyExists, NotFound, WorkspaceDeleteBlocked
from .events import EventLog, archive_task_events
from .locks import archive_lock, task_lock, run_lock
from .models import AgentPersona, AgentRun, Event, Goal, GoalRuntimeInstance, Incident, Proof, Realm, Task, Workspace
from .persona_assignments import PersonaAssignmentStore, PersonaInstanceStore
from .recovery_flags import mark_incident_closed_for_recovery
from .serde import from_jsonable, to_jsonable
from .state_patches import emit_incident_remove, emit_task_refresh
from .simplified_contract import public_decision_type_value
from .states import RunState, TaskState

T = TypeVar("T")

TERMINAL_TASK_STATES = frozenset({TaskState.DONE, TaskState.CANCELLED})
# States that must release persona assignments on entry. FAILED is included even
# though it is not in TERMINAL_TASK_STATES (a failed task may still be updated):
# a failed goal must not keep starving its personas' slots either.
RELEASE_ASSIGNMENT_TASK_STATES = frozenset({TaskState.DONE, TaskState.CANCELLED, TaskState.FAILED})
TERMINAL_RUN_STATES = frozenset({RunState.COMPLETED, RunState.FAILED, RunState.STALE, RunState.CANCELLED})
ACTIVE_RUN_STATES = frozenset({RunState.QUEUED, RunState.STARTING, RunState.RUNNING, RunState.WAITING_ON_TOOL, RunState.WAITING_ON_APPROVAL})
ARCHIVABLE_TASK_STATES = frozenset({TaskState.DONE, TaskState.CANCELLED})
# Bound on Realm.deleted_workspace_ids — the workspace-delete resurrection
# guard. Oldest entries fall off first; by then every member has long since
# pulled the tombstone (the bounded-ledger idiom shared with the board/office
# archived ledgers).
DELETED_WORKSPACE_LEDGER_CAP = 500
_ANY_STAGE = object()


def _safe_operator_reason(reason: str) -> str:
    return "operator requested cancellation"


def _safe_session_id(value) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        lowered = value.lower()
        if not any(marker in lowered for marker in ("secret", "token", "password", "credential", "cookie", "key")):
            return value
    return None


def _safe_model_id(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    cleaned = "".join(ch if ch.isalnum() or ch in "_.:-" else "_" for ch in text)
    return cleaned.strip("._:-")[:120] or None


def _safe_display_name(value) -> str:
    return " ".join(str(value or "").split())[:160]


def _slugify(value) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug[:80] or "item"


def _dedupe_ids(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        clean = _safe_model_id(value)
        if clean and clean not in result:
            result.append(clean)
    return result


def _read_json(path: Path) -> dict:
    if not path.exists():
        raise NotFound(str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def _read_model(cls: type[T], path: Path) -> T:
    return from_jsonable(cls, _read_json(path))


def _write_model(path: Path, model) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, to_jsonable(model), indent=2, sort_keys=True)


def _append_store_event(event_log: EventLog, event_type: str, **payload) -> None:
    """Advance the EventLog watermark after a store mutation (Stage 12).

    The stream/read-model pipeline is watermark-gated: a store write with no
    event is invisible to every consumer (launcher snapshot, serve read model)
    until an unrelated event advances the offset. Emission lives HERE, at the
    store chokepoint, so programmatic callers are covered — not just CLI verbs.
    Payload values of None are dropped. Best effort: a broken event log must
    not fail the write, but the failure is logged, never silent.
    """
    try:
        body = {key: value for key, value in payload.items() if value is not None}
        event_log.append(Event(now(), event_type, None, None, None, body))
    except Exception:
        logging.getLogger(__name__).warning(
            "store event append failed: %s", event_type, exc_info=True
        )


def _parse_intent_basis(value):
    """Parse an ISO-8601 UTC intent basis into a datetime; None when absent or
    unparseable (fail-open — a malformed basis must never block a scope
    switch, it just loses supersede protection for that one write)."""
    if not value:
        return None
    try:
        from datetime import datetime

        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _resolve_activation_write(pointer_path: Path, key: str, value: str | None, issued_at: str | None) -> tuple[str, str | None, str]:
    """Compare-and-set decision for an active-pointer write.

    Mutation intents carry the wall-clock instant the operator issued them
    (``issued_at``). Transport can deliver an intent twice (serve timeout →
    CLI fallback re-runs the same argv) or late (a wedged serve child drains
    an abandoned request minutes later) — the intent's basis, not its arrival
    order, decides who wins. Returns ``(decision, current_value, basis)``:

    - ``apply``      — write the pointer and emit the activation event.
    - ``superseded`` — a strictly newer intent already owns the pointer; do
      not write, do not emit.
    - ``duplicate``  — the exact same intent (same basis, same target) was
      already applied; do not write, do not emit (exact-once event feed).

    A caller with no basis (human at a terminal, legacy callers) is stamped
    ``now()`` so the basis timeline always advances and manual actions win.
    """
    basis = issued_at or now()
    try:
        current = _read_json(pointer_path)
    except Exception:
        return "apply", None, basis
    current_value = _safe_model_id(current.get(key))
    incoming = _parse_intent_basis(basis)
    stored = _parse_intent_basis(current.get("intent_issued_at"))
    if incoming is None or stored is None:
        return "apply", current_value, basis
    if incoming < stored:
        return "superseded", current_value, basis
    if incoming == stored and value == current_value:
        return "duplicate", current_value, basis
    return "apply", current_value, basis


def _list_models(cls: type[T], directory: Path) -> list[T]:
    if not directory.exists():
        return []
    items: list[T] = []
    for path in directory.glob("*.json"):
        try:
            items.append(_read_model(cls, path))
        except NotFound:
            # Archive moves are evidence-preserving but not invisible to UI polls:
            # a file can disappear after glob() and before read_text().
            continue
    return sorted(items, key=lambda item: item.id)


class TaskStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def create(self, task: Task) -> Task:
        path = paths.goal_path(task.id)
        with task_lock(task.id):
            if any(candidate.exists() for candidate in paths.task_storage_candidates(task.id)):
                raise AlreadyExists(task.id)
            _write_model(path, task)
            self.event_log.append(
                Event(
                    ts=now(),
                    type="task.created",
                    task_id=task.id,
                    run_id=None,
                    persona_id=None,
                    # title is a contract summary field (Stage 12 D caught the drift)
                    payload={"title": task.title, "state": str(task.state), "actor": "harness"},
                )
            )
        return self.get(task.id)

    def get(self, task_id: str) -> Task:
        task = _read_model(Goal, paths.existing_task_path(task_id))
        return self._canonicalize_current_stage(task)

    def get_goal(self, goal_or_task_id: str) -> Task:
        value = str(goal_or_task_id or "").strip()
        try:
            return self.get(value)
        except NotFound:
            pass
        for task in self.list_all():
            if getattr(task, "goal_id", None) == value:
                return task
        raise NotFound(value)

    def update(self, task: Task, *, actor: str = "harness", reason: str = "") -> None:
        path = paths.existing_task_path(task.id)
        reached_terminal = False
        with task_lock(task.id):
            previous = self.get(task.id)
            if previous.state in TERMINAL_TASK_STATES:
                return
            _write_model(path, task)
            if previous.state != task.state:
                reached_terminal = task.state in RELEASE_ASSIGNMENT_TASK_STATES
                event_type = "task.transition"
                if task.state == TaskState.BLOCKED:
                    event_type = "task.blocked"
                elif previous.state == TaskState.BLOCKED:
                    event_type = "task.unblocked"
                elif task.state == TaskState.CANCELLED:
                    event_type = "task.cancelled"
                self.event_log.append(
                    Event(
                        ts=now(),
                        type=event_type,
                        task_id=task.id,
                        run_id=None,
                        persona_id=actor if actor != "harness" else None,
                        payload={
                            "from": str(previous.state),
                            "to": str(task.state),
                            "actor": actor,
                            "reason": reason,
                        },
                    )
                )
                # S7-A producer: a task/goal wire row (~80 KB: role_streams,
                # role_envelopes, mission_level_state, derived actor/stage labels)
                # cannot fold as a sub-4 KB op, so a transition emits an accounted
                # ``refresh`` — its coalesced batch falls back to a full core
                # (patch_coverage), which also carries the terminal-transition
                # fan-out (persona-instance removes, assignment closes). Dark by
                # default (read_model.delta_patches off).
                emit_task_refresh(
                    self.event_log,
                    task.id,
                    persona_id=actor if actor != "harness" else None,
                )
        if reached_terminal:
            # Terminal-transition chokepoint: every writer that lands a task in a
            # terminal state persists through this method (ticker COMPLETE_TASK,
            # cancel, planning post-QA guards, persona diagnostics' direct DONE),
            # so releasing persona assignments here closes every bypass path at
            # once. Writers that also release explicitly stay correct — releasing
            # is idempotent. Without this, a done-but-unarchived goal keeps its
            # slots active and every new goal logs agent_already_assigned.
            PersonaAssignmentStore(event_log=self.event_log).close_for_task(
                task.id,
                state="completed" if task.state == TaskState.DONE else "cancelled",
                reason=f"task reached {getattr(task.state, 'value', task.state)}",
            )
            PersonaInstanceStore(event_log=self.event_log).close_for_task(
                task.id,
                goal_id=getattr(task, "goal_id", None),
                reason=f"task reached {getattr(task.state, 'value', task.state)}",
            )

    def list_open(self) -> list[Task]:
        return [task for task in self.list_all() if task.state not in TERMINAL_TASK_STATES]

    def cancel(self, task_id: str, *, reason: str, actor: str = "cli") -> Task:
        task = self.get(task_id)
        if task.state in TERMINAL_TASK_STATES:
            return task
        task.state = TaskState.CANCELLED
        task.updated_at = now()
        self.update(task, actor=actor, reason=_safe_operator_reason(reason))
        PersonaAssignmentStore(event_log=self.event_log).close_for_task(
            task_id, state="cancelled", reason=_safe_operator_reason(reason)
        )
        return self.get(task_id)

    def list_by_state(self, *states: TaskState) -> list[Task]:
        wanted = {state if isinstance(state, TaskState) else TaskState(state) for state in states}
        return [task for task in self.list_all() if task.state in wanted]

    def list_all(self) -> list[Task]:
        by_id: dict[str, Task] = {}
        for task in _list_models(Goal, paths.tasks_dir()):
            by_id.setdefault(task.id, task)
        for task in _list_models(Goal, paths.goals_dir()):
            by_id[task.id] = task
        return [self._canonicalize_current_stage(task) for task in sorted(by_id.values(), key=lambda item: item.id)]

    def list_for_workspace(self, workspace_id: str | None) -> list[Task]:
        normalized = _safe_model_id(workspace_id)
        return [task for task in self.list_all() if _safe_model_id(getattr(task, "workspace_id", None)) == normalized]

    def _canonicalize_current_stage(self, task: Task) -> Task:
        plan = getattr(task, "mission_plan", None)
        if plan is not None:
            current = getattr(plan, "current_stage_id", None)
            if current != getattr(task, "current_stage_id", None):
                task.current_stage_id = current
        if not getattr(task, "goal_id", None):
            task.goal_id = task.id
        return task

    def archive_ready(self, *, actor: str = "cli", reason: str = "archive ready terminal tasks") -> dict:
        return ArchiveStore(event_log=self.event_log).archive_ready(actor=actor, reason=reason)

    def archive(self, task_id: str, *, actor: str = "cli", reason: str = "archive terminal task") -> dict:
        return ArchiveStore(event_log=self.event_log).archive_tasks([task_id], actor=actor, reason=reason)


class ArchiveStore:
    """Evidence-preserving archive for terminal Harness tasks."""

    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def archive_ready(self, *, actor: str = "cli", reason: str = "archive ready terminal tasks") -> dict:
        task_ids = [task.id for task in TaskStore(event_log=self.event_log).list_all() if task.state in ARCHIVABLE_TASK_STATES]
        return self.archive_tasks(task_ids, actor=actor, reason=reason)

    def archive_tasks(self, task_ids: list[str], *, actor: str = "cli", reason: str = "archive terminal task") -> dict:
        normalized_task_ids = [str(task_id).strip() for task_id in task_ids if str(task_id).strip()]
        batch = _archive_batch_name()
        archive_dir = paths.deleted_archive_dir() / batch
        archived: list[dict] = []
        skipped: list[dict] = []
        task_store = TaskStore(event_log=self.event_log)
        run_store = RunStore(event_log=self.event_log)

        with archive_lock():
            ready: list[Task] = []
            for task_id in normalized_task_ids:
                try:
                    task = task_store.get(task_id)
                except NotFound:
                    skipped.append({"task_id": task_id, "reason": "not_found", "message": "Task was not found; no archive batch was created for this id."})
                    continue
                active_runs = [run for run in run_store.list_for_task(task.id) if run.state in ACTIVE_RUN_STATES]
                active_workers = _active_worker_sessions_for_task(task.id)
                if task.state not in ARCHIVABLE_TASK_STATES:
                    skipped.append({"task_id": task.id, "state": str(task.state), "reason": "not_terminal", "message": f"Task is {task.state.value}; only done/cancelled tasks can be archived."})
                    continue
                if active_runs:
                    skipped.append({"task_id": task.id, "state": str(task.state), "reason": "active_runs", "run_ids": [run.id for run in active_runs], "message": "Task still has active runs; stop or settle them before archiving."})
                    continue
                if active_workers:
                    skipped.append({"task_id": task.id, "state": str(task.state), "reason": "active_worker_sessions", "worker_session_ids": [worker.id for worker in active_workers], "message": "Task still has active or possessed worker sessions; close or release them before archiving."})
                    continue
                ready.append(task)

            if not ready:
                return _archive_result(
                    batch=None,
                    archive_dir=None,
                    archived=[],
                    skipped=skipped,
                    manifest_path=None,
                )

            archive_dir.mkdir(parents=True, exist_ok=False)
            prepare_manifest = {
                "schema_version": 1,
                "archive_batch": batch,
                "prepared_at_utc": now().isoformat(),
                "reason": _safe_archive_reason(reason),
                "actor": _safe_archive_actor(actor),
                "ready_task_ids": [task.id for task in ready],
                "skipped_tasks": skipped,
                "state": "prepared",
            }
            atomic_json_write(archive_dir / "manifest.prepare.json", prepare_manifest, indent=2, sort_keys=True)

            delivery_outcomes = self._execute_delivery_directives(ready)

            for task in ready:
                entry = self._archive_one(task, archive_dir, run_store)
                if task.id in delivery_outcomes:
                    entry["delivery_directive_outcomes"] = delivery_outcomes[task.id]
                archived.append(entry)

            manifest = {
                "schema_version": 1,
                "archive_batch": batch,
                "created_at_utc": now().isoformat(),
                "reason": _safe_archive_reason(reason),
                "actor": _safe_archive_actor(actor),
                "archived_task_ids": [item["task_id"] for item in archived],
                "skipped_task_ids": [item["task_id"] for item in skipped],
                "archived_tasks": archived,
                "skipped_tasks": skipped,
                "prepare_manifest_path": "manifest.prepare.json",
            }
            atomic_json_write(archive_dir / "manifest.json", manifest, indent=2, sort_keys=True)
            for item in archived:
                self._append_archive_event(item, archive_dir=archive_dir, actor=actor, reason=reason)
            return _archive_result(
                batch=batch,
                archive_dir=archive_dir,
                archived=archived,
                skipped=skipped,
                manifest_path=archive_dir / "manifest.json",
            )

    def _execute_delivery_directives(self, ready: list[Task]) -> dict[str, list[dict]]:
        """Run each terminal task's declared delivery directive before its
        evidence moves: promote delivered bundles, then reap their worktrees.

        This is the single choke point both the daemon auto-archive and manual
        ``task archive`` route through, so ``done`` always means the directive
        ran — never a bundle stranded in a disposable worktree.
        """

        from .delivery_directive import execute_task_delivery_directives, execute_task_worktree_delivery_directives
        from .repo_bundles import RepoBundleStore

        outcomes: dict[str, list[dict]] = {}
        bundle_store = RepoBundleStore()
        run_store = RunStore(event_log=self.event_log)
        for task in ready:
            try:
                bundles = bundle_store.list_for_task(task.id)
            except Exception:
                bundles = []
            results: list[dict] = []
            if bundles:
                results = execute_task_delivery_directives(
                    task,
                    bundles,
                    event_log=self.event_log,
                    incident_store=IncidentStore(event_log=self.event_log),
                )
            # A terminal task owns nothing anymore: capture-then-reap every
            # remaining run worktree (non-delivered bundles, failed runs).
            # Dirty worktrees still obey the delivery directive before any
            # cleanup, so ``apply_to_repo`` cannot be bypassed by a bundleless
            # run.
            try:
                repos = list(dict.fromkeys(list(task.affected_repos or []) + [b.repo for b in bundles]))
                worktree_results = execute_task_worktree_delivery_directives(
                    task,
                    run_ids=[run.id for run in run_store.list_for_task(task.id)],
                    repos=repos,
                    event_log=self.event_log,
                    incident_store=IncidentStore(event_log=self.event_log),
                )
                results.extend(worktree_results)
            except Exception:
                pass
            if results:
                outcomes[task.id] = results
        return outcomes

    def _archive_one(self, task: Task, archive_dir: Path, run_store: "RunStore") -> dict:
        incident_store = IncidentStore(event_log=self.event_log)
        for incident in incident_store.list_open():
            if incident.task_id == task.id:
                incident_store.close(incident.id, reason="task_archived")

        task_dest = archive_dir / "tasks" / f"{task.id}.json"
        task_dest.parent.mkdir(parents=True, exist_ok=True)
        _move_if_exists(paths.existing_task_path(task.id), task_dest)

        archived_runs: list[str] = []
        for run in run_store.list_for_task(task.id):
            run_dest = archive_dir / "runs" / f"{run.id}.json"
            run_dest.parent.mkdir(parents=True, exist_ok=True)
            if _move_if_exists(paths.run_path(run.id), run_dest):
                archived_runs.append(run.id)

        archived_proofs: list[str] = []
        proof_dir = paths.proofs_dir() / task.id
        if proof_dir.exists():
            proof_dest = archive_dir / "proofs" / task.id
            proof_dest.parent.mkdir(parents=True, exist_ok=True)
            if proof_dest.exists():
                shutil.rmtree(proof_dest)
            shutil.move(str(proof_dir), str(proof_dest))
            archived_proofs = [path.stem.removeprefix("proof_") for path in sorted(proof_dest.glob("proof_*.json"))]

        archived_incidents: list[str] = []
        for incident in incident_store.list_all():
            if incident.task_id != task.id:
                continue
            incident_dest = archive_dir / "incidents" / f"{incident.id}.json"
            incident_dest.parent.mkdir(parents=True, exist_ok=True)
            if _move_if_exists(paths.incident_path(incident.id), incident_dest):
                archived_incidents.append(incident.id)
            detail_dest = archive_dir / "incident_details" / f"{incident.id}.txt"
            if _move_if_exists(paths.incident_detail_path(incident.id), detail_dest):
                detail_dest.parent.mkdir(parents=True, exist_ok=True)

        archived_workers = _archive_worker_evidence(task.id, archive_dir)
        archived_assignments = _archive_persona_assignment_evidence(task.id, archive_dir)
        archived_persona_instances = _archive_persona_instance_evidence(task, archive_dir)
        archived_repo_bundles = _archive_repo_bundle_evidence(task.id, archive_dir)
        archived_runtime_instances = _archive_runtime_instance_evidence(task.id, archive_dir)
        archived_packet_artifacts = _archive_packet_artifacts(task.id, archive_dir)
        archived_self_tests = _archive_self_test_evidence(task.id, archive_dir)
        archived_role_state = _archive_role_envelope_evidence(task.id, archive_dir)
        archived_events = archive_task_events(task.id, archive_dir)

        return {
            "task_id": task.id,
            "title": task.title,
            "state": str(task.state),
            "task_path": str(task_dest.relative_to(archive_dir)),
            "events_path": archived_events["events_path"],
            "event_count": archived_events["event_count"],
            "event_bytes": archived_events["event_bytes"],
            "event_compaction_eligible": archived_events["compaction_eligible"],
            "run_ids": archived_runs,
            "proof_ids": archived_proofs,
            "incident_ids": archived_incidents,
            "worker_session_ids": archived_workers.get("worker_session_ids", []),
            "persona_assignment_ids": archived_assignments.get("persona_assignment_ids", []),
            "persona_assignments_archived": archived_assignments.get("persona_assignments_archived", False),
            "persona_instance_ids": archived_persona_instances.get("persona_instance_ids", []),
            "persona_instances_archived": archived_persona_instances.get("persona_instances_archived", False),
            "repo_bundle_ids": archived_repo_bundles.get("repo_bundle_ids", []),
            "repo_bundles_archived": archived_repo_bundles.get("repo_bundles_archived", False),
            "runtime_instance_ids": archived_runtime_instances.get("runtime_instance_ids", []),
            "runtime_instances_archived": archived_runtime_instances.get("runtime_instances_archived", False),
            "packet_artifact_ids": archived_packet_artifacts.get("packet_artifact_ids", []),
            "packet_artifacts_archived": archived_packet_artifacts.get("packet_artifacts_archived", False),
            "worker_context_archived": archived_workers.get("context_archived", False),
            "proof_sandbox_archived": archived_workers.get("proof_sandbox_archived", False),
            "self_test_evidence_ids": archived_self_tests.get("self_test_evidence_ids", []),
            "self_test_evidence_archived": archived_self_tests.get("self_test_evidence_archived", False),
            "role_envelope_ids": archived_role_state.get("role_envelope_ids", []),
            "role_checklist_ids": archived_role_state.get("role_checklist_ids", []),
            "role_state_archived": archived_role_state.get("role_state_archived", False),
        }

    def _append_archive_event(self, item: dict, *, archive_dir: Path, actor: str, reason: str) -> None:
        self.event_log.append(
            Event(
                ts=now(),
                type="task.archived",
                task_id=item["task_id"],
                run_id=None,
                persona_id=_safe_archive_persona(actor),
                payload={
                    "archive_batch": archive_dir.name,
                    "manifest_path": "manifest.json",
                    "state": item["state"],
                    "reason": _safe_archive_reason(reason),
                    "run_count": len(item.get("run_ids") or []),
                    "proof_count": len(item.get("proof_ids") or []),
                    "incident_count": len(item.get("incident_ids") or []),
                    "worker_session_count": len(item.get("worker_session_ids") or []),
                    "persona_instance_count": len(item.get("persona_instance_ids") or []),
                    "runtime_instance_count": len(item.get("runtime_instance_ids") or []),
                    "self_test_evidence_count": len(item.get("self_test_evidence_ids") or []),
                    "event_count": int(item.get("event_count") or 0),
                },
            )
        )


class AgentStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def save(self, persona: AgentPersona) -> AgentPersona:
        _write_model(paths.agent_path(persona.id), persona)
        _append_store_event(
            self.event_log,
            "persona.updated",
            persona_id=persona.id,
            display_name=persona.display_name,
        )
        return persona

    def get(self, persona_id: str) -> AgentPersona:
        return _read_model(AgentPersona, paths.agent_path(persona_id))

    def list_all(self) -> list[AgentPersona]:
        return _list_models(AgentPersona, paths.agents_dir())


class WorkspaceStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def create(
        self,
        *,
        name: str,
        agent_ids: list[str] | None = None,
        default_blueprint_id: str | None = None,
        isolation: str = "soft",
        max_concurrent_lanes: int | None = None,
        realm_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Workspace:
        clean_name = _safe_display_name(name)
        if not clean_name:
            raise ValueError("workspace name is required")
        clean_isolation = str(isolation or "soft").strip().lower()
        if clean_isolation not in {"soft", "hard"}:
            raise ValueError("invalid_isolation")
        ts = now()
        slug = _slugify(clean_name)
        item = Workspace(
            id=_safe_model_id(workspace_id) or f"ws_{slug}_{uuid.uuid4().hex[:6]}",
            slug=slug,
            name=clean_name,
            agent_ids=_dedupe_ids(agent_ids or []),
            default_blueprint_id=_safe_model_id(default_blueprint_id),
            isolation=clean_isolation,
            max_concurrent_lanes=max_concurrent_lanes if max_concurrent_lanes is None else max(1, int(max_concurrent_lanes)),
            realm_id=_safe_model_id(realm_id),
            created_at=ts,
            updated_at=ts,
        )
        path = paths.workspace_path(item.id)
        if path.exists():
            raise AlreadyExists(item.id)
        _write_model(path, item)
        _append_store_event(
            self.event_log,
            "workspace.created",
            workspace_id=item.id,
            name=item.name,
            realm_id=item.realm_id,
        )
        return self.get(item.id)

    def get(self, workspace_id: str) -> Workspace:
        return _read_model(Workspace, paths.workspace_path(workspace_id))

    def list_all(self, *, include_archived: bool = False) -> list[Workspace]:
        items = _list_models(Workspace, paths.workspaces_dir())
        if not include_archived:
            items = [item for item in items if not item.archived]
        return sorted(items, key=lambda item: (item.name.lower(), item.id))

    def save(self, item: Workspace, *, emit_event: bool = True) -> Workspace:
        """``emit_event=False`` is for named mutators (rename/archive/…) that
        append their own, more specific event — never for skipping emission."""
        item.updated_at = now()
        _write_model(paths.workspace_path(item.id), item)
        if emit_event:
            _append_store_event(
                self.event_log, "workspace.updated", workspace_id=item.id, change="saved", name=item.name
            )
        return self.get(item.id)

    def set_active(self, workspace_id: str | None, *, issued_at: str | None = None) -> dict:
        value = _safe_model_id(workspace_id)
        name = self.get(value).name if value else None
        decision, current_value, basis = _resolve_activation_write(
            paths.active_workspace_path(), "workspace_id", value, issued_at
        )
        if decision != "apply":
            return {"workspace_id": current_value, "applied": False, "reason": decision, "requested_workspace_id": value}
        _write_model(
            paths.active_workspace_path(),
            {"workspace_id": value, "updated_at": now(), "intent_issued_at": basis},
        )
        if value:
            _append_store_event(self.event_log, "workspace.activated", workspace_id=value, name=name)
        else:
            _append_store_event(self.event_log, "workspace.activated", cleared=True)
        return {"workspace_id": value, "applied": True}

    def active_id(self) -> str | None:
        try:
            raw = _read_json(paths.active_workspace_path())
        except Exception:
            return None
        return _safe_model_id(raw.get("workspace_id"))

    def add_agent(self, workspace_id: str, persona_id: str) -> Workspace:
        item = self.get(workspace_id)
        persona = _safe_model_id(persona_id)
        if persona and persona not in item.agent_ids:
            item.agent_ids.append(persona)
        item = self.save(item, emit_event=False)
        _append_store_event(
            self.event_log, "workspace.updated", workspace_id=item.id, change="agent_added", persona_id=persona
        )
        return item

    def remove_agent(self, workspace_id: str, persona_id: str) -> Workspace:
        item = self.get(workspace_id)
        persona = _safe_model_id(persona_id)
        item.agent_ids = [value for value in item.agent_ids if value != persona]
        item = self.save(item, emit_event=False)
        _append_store_event(
            self.event_log, "workspace.updated", workspace_id=item.id, change="agent_removed", persona_id=persona
        )
        return item

    def rename(self, workspace_id: str, name: str) -> Workspace:
        item = self.get(workspace_id)
        item.name = _safe_display_name(name)
        item.slug = _slugify(item.name)
        item = self.save(item, emit_event=False)
        _append_store_event(
            self.event_log, "workspace.updated", workspace_id=item.id, change="renamed", name=item.name
        )
        return item

    def archive(self, workspace_id: str) -> Workspace:
        item = self.get(workspace_id)
        item.archived = True
        item = self.save(item, emit_event=False)
        _append_store_event(self.event_log, "workspace.archived", workspace_id=item.id, name=item.name)
        return item

    def delete(self, workspace_id: str, *, reason: str = "operator_delete") -> dict:
        """Hard-delete a workspace and cascade its scoped content stores.

        The single write chokepoint for workspace deletion (archive stays the
        reversible path). Guards, in order:

        - ``workspace_has_goals`` — the workspace still owns live-store goals.
          Evidence is never destroyed implicitly; the operator archives goals
          first (``harness task archive-ready``), then deletes.
        - ``realm_default_workspace`` — a SERVER-bound realm's default pointer
          is backend-adoption authority; promote another default first. A
          local realm's default pointer is local truth and is cleared here.

        Cascade: the workspace JSON, its Mission Office subtree, and every
        board owned by the workspace. A realm-bound delete also rewrites realm
        membership and records the id in the realm's ``deleted_workspace_ids``
        resurrection-guard ledger, so realm sync propagates the removal
        instead of letting another member's surviving copy republish it.
        Emits ``workspace.deleted`` (Stage 12: the mutation must ride its own
        event or stay invisible to the watermark-gated consumers).
        """
        item = self.get(workspace_id)
        tasks = TaskStore(event_log=self.event_log).list_for_workspace(item.id)
        if tasks:
            live = [task for task in tasks if task.state not in RELEASE_ASSIGNMENT_TASK_STATES]
            raise WorkspaceDeleteBlocked(
                "workspace_has_goals",
                "Workspace still owns goals; archive them first (harness task archive-ready), then delete.",
                safe_details={"goal_count": len(tasks), "live_goal_count": len(live)},
            )
        realm: Realm | None = None
        if item.realm_id:
            try:
                realm = RealmStore(event_log=self.event_log).get(item.realm_id)
            except NotFound:
                realm = None
        if realm is not None and realm.server_id and realm.default_workspace_id == item.id:
            raise WorkspaceDeleteBlocked(
                "realm_default_workspace",
                "This workspace is the realm's default; promote another default workspace first.",
                safe_details={"realm_id": realm.id},
            )

        # Cascade content stores under their own write locks so a concurrent
        # office/board write cannot interleave with the removal.
        from .board_store import BoardStore
        from .locks import board_lock, office_lock

        with office_lock(item.id):
            shutil.rmtree(paths.office_dir(item.id), ignore_errors=True)
        for board in BoardStore(event_log=self.event_log).list_all():
            if board.workspace_id != item.id:
                continue
            with board_lock(board.board_id):
                shutil.rmtree(paths.board_dir(board.board_id), ignore_errors=True)

        if realm is not None:
            realm.workspace_ids = [wid for wid in (realm.workspace_ids or []) if wid != item.id]
            ledger = [wid for wid in (realm.deleted_workspace_ids or []) if wid != item.id]
            ledger.append(item.id)
            realm.deleted_workspace_ids = ledger[-DELETED_WORKSPACE_LEDGER_CAP:]
            if realm.default_workspace_id == item.id:
                # Only reachable for local realms — the server-bound case is
                # guarded above.
                realm.default_workspace_id = None
            RealmStore(event_log=self.event_log).save(realm, emit_event=False)
            _append_store_event(
                self.event_log, "realm.updated", realm_id=realm.id, change="workspace_deleted"
            )

        paths.workspace_path(item.id).unlink(missing_ok=True)
        if self.active_id() == item.id:
            # Clear the dangling pointer; verb-layer callers may re-reconcile
            # to the realm's default afterwards.
            self.set_active(None)
        _append_store_event(
            self.event_log,
            "workspace.deleted",
            workspace_id=item.id,
            name=item.name,
            realm_id=item.realm_id,
            reason=reason,
        )
        return {
            "id": item.id,
            "name": item.name,
            "realm_id": item.realm_id,
            "deleted": True,
        }


def _normalize_skill_selection(selection: list[str] | None) -> list[str]:
    """Validate (shape only), dedupe, and sort skill selection slugs.

    Shape rules (per REALM_SKILL_SELECTION_DESIGN §2): non-empty, no leading
    dot, no path separator, and identical to their ``_safe_token`` form — the
    same tokenizer the realm publisher uses for skill directory names, so a
    valid slug round-trips to its published path. Every malformed slug is
    collected and reported in ONE ``ValueError`` (mapped to a typed
    ``invalid_request`` at the CLI seam) so a batch save names all offenders
    instead of failing on the first. Slugs unknown to the local catalog are
    NOT filtered here — that is realm truth another member may own.
    """
    # Function-local import breaks the module cycle (realm_sync imports store).
    from agent_runtime.realm_sync import _safe_token

    cleaned: set[str] = set()
    rejected: list[str] = []
    for raw in selection or []:
        slug = str(raw).strip()
        if (
            not slug
            or slug.startswith(".")
            or "/" in slug
            or "\\" in slug
            or slug != _safe_token(slug)
        ):
            rejected.append(slug or repr(raw))
            continue
        cleaned.add(slug)
    if rejected:
        raise ValueError(
            "malformed skill selection slug(s): " + ", ".join(repr(slug) for slug in sorted(set(rejected)))
        )
    return sorted(cleaned)


def _normalize_agent_selection(selection: list[str] | None) -> list[str]:
    """Validate, dedupe, and sort Realm persona-definition ids.

    Persona ids use the store's canonical model-id grammar (including ``:``
    for profile-backed personas). Unknown ids are deliberately preserved: a
    different Realm member may own the definition locally, so filtering an
    unrelated save through this machine's catalog would corrupt Realm truth.
    Every malformed id is reported together and no partial write occurs.
    """
    cleaned: set[str] = set()
    rejected: list[str] = []
    for raw in selection or []:
        value = str(raw).strip()
        normalized = _safe_model_id(value)
        if not value or normalized is None or value != normalized:
            rejected.append(value or repr(raw))
            continue
        cleaned.add(value)
    if rejected:
        raise ValueError(
            "malformed agent selection id(s): "
            + ", ".join(repr(value) for value in sorted(set(rejected)))
        )
    return sorted(cleaned)


class RealmStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def create(
        self,
        *,
        name: str,
        server_id: str | None = None,
        realm_id: str | None = None,
        default_workspace_id: str | None = None,
        default_workspace_name: str = "Default",
        default_workspace_version: int = 0,
    ) -> Realm:
        clean_name = _safe_display_name(name)
        if not clean_name:
            raise ValueError("realm name is required")
        ts = now()
        slug = _slugify(clean_name)
        item = Realm(
            id=_safe_model_id(realm_id) or f"realm_{slug}_{uuid.uuid4().hex[:6]}",
            slug=slug,
            name=clean_name,
            server_id=_safe_model_id(server_id),
            default_workspace_id=_safe_model_id(default_workspace_id),
            default_workspace_name=_safe_display_name(default_workspace_name) or "Default",
            default_workspace_version=max(0, int(default_workspace_version)),
            created_at=ts,
            updated_at=ts,
        )
        path = paths.realm_path(item.id)
        if path.exists():
            raise AlreadyExists(item.id)
        _write_model(path, item)
        _append_store_event(
            self.event_log, "realm.created", realm_id=item.id, name=item.name, server_id=item.server_id
        )
        return self.get(item.id)

    def get(self, realm_id: str) -> Realm:
        return _read_model(Realm, paths.realm_path(realm_id))

    def list_all(self, *, include_archived: bool = False) -> list[Realm]:
        items = _list_models(Realm, paths.realms_dir())
        if not include_archived:
            items = [item for item in items if not item.archived]
        return sorted(items, key=lambda item: (item.name.lower(), item.id))

    def save(self, item: Realm, *, emit_event: bool = True) -> Realm:
        """``emit_event=False`` is for callers that append their own, more
        specific event in the same mutation (bind_server, realm adopt)."""
        item.updated_at = now()
        _write_model(paths.realm_path(item.id), item)
        if emit_event:
            _append_store_event(self.event_log, "realm.updated", realm_id=item.id, change="saved")
        return self.get(item.id)

    def archive(self, realm_id: str) -> Realm:
        """Recoverably remove a Realm from live selectors and projections."""

        item = self.get(realm_id)
        if item.archived:
            return item
        item.archived = True
        item = self.save(item, emit_event=False)
        _append_store_event(
            self.event_log,
            "realm.archived",
            realm_id=item.id,
            name=item.name,
        )
        return item

    def bind_server(self, realm_id: str, server_id: str) -> Realm:
        item = self.get(realm_id)
        item.server_id = _safe_model_id(server_id)
        item = self.save(item, emit_event=False)
        _append_store_event(
            self.event_log, "realm.updated", realm_id=item.id, change="server_bound", server_id=item.server_id
        )
        return item

    def set_skill_selection(
        self, realm_id: str, *, mode: str, selection: list[str], dry_run: bool = False
    ) -> Realm:
        """Single write chokepoint for a realm's shared-skill publish selection.

        ``mode == "all"`` publishes every shared skill and PRESERVES the stored
        ``skill_selection`` intact (switching back to "selected" restores it, so
        the passed ``selection`` is ignored in this mode). ``mode == "selected"``
        replaces the selection with the validated/deduped/sorted slugs (an empty
        list means "publish none").

        Slugs are validated for shape only (non-empty, no leading dot, no path
        separators, must equal their ``_safe_token`` form). Slugs unknown to
        this machine's catalog are NOT filtered here — another member may hold
        the skill locally, and dropping it on an unrelated save would corrupt
        realm truth; unknown slugs are reported (``missing``) by the CLI, never
        stripped. Emits ``realm.updated``/``skill_selection`` so the read-model
        pipeline sees the mutation (Stage 12 watermark discipline).

        ``dry_run`` runs the full validation and returns the WOULD-BE realm
        (in-memory only) without saving and without emitting the store event.
        """
        if mode not in {"all", "selected"}:
            raise ValueError(f"invalid skill_publish_mode: {mode!r}")
        item = self.get(realm_id)
        if mode == "selected":
            item.skill_selection = _normalize_skill_selection(selection)
        item.skill_publish_mode = mode
        if dry_run:
            return item
        item = self.save(item, emit_event=False)
        _append_store_event(
            self.event_log,
            "realm.updated",
            realm_id=item.id,
            change="skill_selection",
            mode=mode,
            selection_count=len(item.skill_selection),
        )
        return item

    def set_agent_selection(
        self, realm_id: str, *, mode: str, selection: list[str], dry_run: bool = False
    ) -> Realm:
        """Single write chokepoint for Realm persona-definition selection.

        ``workspace`` keeps the explicit list intact but publishes only the
        definitions required by synced workspace/Office references.
        ``selected`` publishes the explicit set plus those required references.
        Unknown persona ids are preserved and reported by the CLI envelope.
        """
        if mode not in {"workspace", "selected"}:
            raise ValueError(f"invalid agent_publish_mode: {mode!r}")
        item = self.get(realm_id)
        if mode == "selected":
            item.agent_selection = _normalize_agent_selection(selection)
        item.agent_publish_mode = mode
        if dry_run:
            return item
        item = self.save(item, emit_event=False)
        _append_store_event(
            self.event_log,
            "realm.updated",
            realm_id=item.id,
            change="agent_selection",
            mode=mode,
            selection_count=len(item.agent_selection),
        )
        return item

    def set_active(self, realm_id: str | None, *, issued_at: str | None = None) -> dict:
        value = _safe_model_id(realm_id)
        name = self.get(value).name if value else None
        decision, current_value, basis = _resolve_activation_write(
            paths.active_realm_path(), "realm_id", value, issued_at
        )
        if decision != "apply":
            return {"realm_id": current_value, "applied": False, "reason": decision, "requested_realm_id": value}
        _write_model(
            paths.active_realm_path(),
            {"realm_id": value, "updated_at": now(), "intent_issued_at": basis},
        )
        if value:
            _append_store_event(self.event_log, "realm.activated", realm_id=value, name=name)
        else:
            _append_store_event(self.event_log, "realm.activated", cleared=True)
        return {"realm_id": value, "applied": True}

    def active_id(self) -> str | None:
        try:
            raw = _read_json(paths.active_realm_path())
        except Exception:
            return None
        return _safe_model_id(raw.get("realm_id"))


class RunStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def open_run(
        self,
        persona_id: str,
        task_id: str,
        stage_id: str | None = None,
        *,
        iteration_budget: int = 90,
        max_wall_seconds: float | None = None,
        max_api_calls: int | None = None,
        max_total_tokens: int | None = None,
        session_id: str | None = None,
        tick_id: str | None = None,
    ) -> AgentRun:
        ts = now()
        existing = self.find_active(task_id=task_id, persona_id=persona_id, stage_id=stage_id)
        if existing:
            raise AlreadyExists(existing[0].id)
        run = AgentRun(
            id=f"run_{uuid.uuid4().hex[:12]}",
            persona_id=persona_id,
            task_id=task_id,
            stage_id=stage_id,
            state=RunState.RUNNING,
            started_at=ts,
            last_heartbeat_at=ts,
            iteration_budget=iteration_budget,
            max_wall_seconds=max_wall_seconds,
            max_api_calls=max_api_calls,
            max_total_tokens=max_total_tokens,
            session_id=_safe_session_id(session_id),
        )
        _write_model(paths.run_path(run.id), run)
        self.event_log.append(
            Event(
                ts=now(),
                type="run.opened",
                task_id=task_id,
                run_id=run.id,
                persona_id=persona_id,
                payload={
                    "stage_id": stage_id,
                    "iteration_budget": iteration_budget,
                    "max_wall_seconds": max_wall_seconds,
                    "max_api_calls": max_api_calls,
                    "max_total_tokens": max_total_tokens,
                    "session_id": _safe_session_id(session_id),
                    "tick_id": tick_id,
                },
            )
        )
        return run

    def get(self, run_id: str) -> AgentRun:
        return _read_model(AgentRun, paths.run_path(run_id))

    def update(self, run: AgentRun) -> bool:
        with run_lock(run.id):
            run.session_id = _safe_session_id(run.session_id)
            if isinstance(run.llm, dict):
                safe_llm_session_id = _safe_session_id(run.llm.get("session_id"))
                if safe_llm_session_id:
                    run.llm["session_id"] = safe_llm_session_id
                else:
                    run.llm.pop("session_id", None)
            try:
                previous = self.get(run.id)
            except NotFound:
                previous = None
            if previous is not None and previous.state in TERMINAL_RUN_STATES:
                return False
            _write_model(paths.run_path(run.id), run)
            return True

    def heartbeat(self, run_id: str) -> AgentRun:
        run = self.get(run_id)
        if run.state in TERMINAL_RUN_STATES:
            return run
        run.last_heartbeat_at = now()
        run.state = RunState.RUNNING
        if not self.update(run):
            return self.get(run_id)
        self.event_log.append(
            Event(
                ts=now(),
                type="run.heartbeat",
                task_id=run.task_id,
                run_id=run.id,
                persona_id=run.persona_id,
                # contract summary fields; run_id is duplicated from the envelope
                # column because consumers validate/render from the payload
                payload={"run_id": run.id, "state": str(run.state)},
            )
        )
        return run

    def close_run(
        self,
        run_id: str,
        *,
        state: RunState,
        final_decision=None,
        error=None,
    ) -> AgentRun:
        with run_lock(run_id):
            run = self.get(run_id)
            run.session_id = _safe_session_id(run.session_id)
            if isinstance(run.llm, dict):
                safe_llm_session_id = _safe_session_id(run.llm.get("session_id"))
                if safe_llm_session_id:
                    run.llm["session_id"] = safe_llm_session_id
                else:
                    run.llm.pop("session_id", None)
            if run.state in TERMINAL_RUN_STATES:
                return run
            run.state = state if isinstance(state, RunState) else RunState(state)
            run.finished_at = now()
            if final_decision and isinstance(final_decision, dict):
                public_type = public_decision_type_value(final_decision.get("type"))
                if public_type and public_type != final_decision.get("type"):
                    raw_decision_type = final_decision.get("type")
                    final_decision = {**final_decision, "type": public_type}
                    final_decision.setdefault("execution_type", raw_decision_type)
            run.final_decision = final_decision
            run.error = error
            if isinstance(run.llm, dict):
                if final_decision and isinstance(final_decision, dict):
                    public_decision_type = public_decision_type_value(final_decision.get("type")) or final_decision.get("type")
                    prior_decision_type = run.llm.get("decision_type")
                    if prior_decision_type and public_decision_type and prior_decision_type != public_decision_type:
                        run.llm.setdefault("raw_decision_type", prior_decision_type)
                    if public_decision_type:
                        run.llm["decision_type"] = public_decision_type
                        run.llm.setdefault("public_decision_type", public_decision_type)
                    run.llm.setdefault("validation_status", "valid")
                elif error:
                    run.llm.setdefault("validation_status", "invalid")
            _write_model(paths.run_path(run.id), run)
            payload = {"state": str(run.state)}
            if isinstance(final_decision, dict):
                payload["decision_type"] = final_decision.get("type")
                payload["validation_status"] = "valid"
            if run.session_id:
                payload["session_id"] = run.session_id
            if isinstance(run.llm, dict):
                for key in ("total_tokens", "input_tokens", "output_tokens", "api_calls", "tool_turns"):
                    if run.llm.get(key) is not None:
                        payload[key] = run.llm.get(key)
                if run.llm.get("validation_status"):
                    payload["validation_status"] = run.llm.get("validation_status")
            self.event_log.append(
                Event(
                    ts=now(),
                    type="run.closed",
                    task_id=run.task_id,
                    run_id=run.id,
                    persona_id=run.persona_id,
                    payload=payload,
                )
            )
            return run

    def cancel(self, run_id: str, *, reason: str) -> AgentRun:
        return self.close_run(
            run_id,
            state=RunState.CANCELLED,
            error={"type": "operator_cancelled", "summary": _safe_operator_reason(reason)},
        )

    def approve_continuation(self, run_id: str) -> AgentRun:
        with run_lock(run_id):
            run = self.get(run_id)
            if run.state != RunState.WAITING_ON_APPROVAL:
                raise ValueError("run is not waiting on approval")
            if not _safe_session_id(run.session_id):
                raise ValueError("same_session_not_safe: missing session_id")
            error = run.error or {}
            if error.get("type") != "run_budget_exceeded":
                raise ValueError("run approval is only supported for budget-limited runs")
            run.state = RunState.FAILED
            run.finished_at = now()
            run.progress = {**(run.progress or {}), "approved_for_continuation": True, "continuation_session_id": run.session_id}
            run.error = {**error, "approved_for_continuation": True}
            _write_model(paths.run_path(run.id), run)
            self.event_log.append(
                Event(
                    ts=now(),
                    type="run.approved",
                    task_id=run.task_id,
                    run_id=run.id,
                    persona_id=run.persona_id,
                    payload={
                        "approval_type": "budget_continuation",
                        "session_id": run.session_id,
                        "next_expected": "continue_same_session",
                    },
                )
            )
            return run

    def latest_session_id(self, *, task_id: str, persona_id: str, stage_id: object = _ANY_STAGE) -> str | None:
        runs = [
            run for run in self.list_all()
            if run.task_id == task_id
            and run.persona_id == persona_id
            and (stage_id is _ANY_STAGE or run.stage_id == stage_id)
            and _safe_session_id(run.session_id)
        ]
        if runs and _latest_run_is_invalid(runs):
            return None
        runs = [run for run in runs if _run_session_is_reusable(run)]
        if not runs and stage_id is not _ANY_STAGE:
            fallback_runs = [
                run for run in self.list_all()
                if run.task_id == task_id
                and run.persona_id == persona_id
                and _safe_session_id(run.session_id)
            ]
            if fallback_runs and _latest_run_is_invalid(fallback_runs):
                return None
            runs = [run for run in fallback_runs if _run_session_is_reusable(run)]
        if not runs:
            return None
        latest = max(runs, key=lambda run: run.finished_at or run.last_heartbeat_at or run.started_at)
        return latest.session_id

    def find_stale(self, *, heartbeat_ttl_seconds: int) -> list[AgentRun]:
        cutoff = now() - timedelta(seconds=heartbeat_ttl_seconds)
        return [
            run
            for run in self.list_all()
            if run.state not in TERMINAL_RUN_STATES and run.state != RunState.WAITING_ON_APPROVAL and run.last_heartbeat_at < cutoff
        ]

    def list_for_task(self, task_id: str) -> list[AgentRun]:
        return [run for run in self.list_all() if run.task_id == task_id]

    def find_active(self, *, task_id: str | None = None, persona_id: str | None = None, stage_id: object = _ANY_STAGE) -> list[AgentRun]:
        return [
            run
            for run in self.list_all()
            if run.state in ACTIVE_RUN_STATES
            and (task_id is None or run.task_id == task_id)
            and (persona_id is None or run.persona_id == persona_id)
            and (stage_id is _ANY_STAGE or run.stage_id == stage_id)
        ]

    def list_all(self) -> list[AgentRun]:
        return _list_models(AgentRun, paths.runs_dir())


def _latest_run_is_invalid(runs: list[AgentRun]) -> bool:
    latest = max(runs, key=lambda run: run.finished_at or run.last_heartbeat_at or run.started_at)
    if _run_has_approved_continuation(latest):
        return False
    llm = latest.llm if isinstance(latest.llm, dict) else {}
    return latest.state == RunState.FAILED and llm.get("validation_status") == "invalid"


def _run_session_is_reusable(run: AgentRun) -> bool:
    if not _safe_session_id(run.session_id):
        return False
    llm = run.llm if isinstance(run.llm, dict) else {}
    if run.state == RunState.FAILED and llm.get("validation_status") == "invalid" and not _run_has_approved_continuation(run):
        return False
    return run.state in {RunState.COMPLETED, RunState.FAILED, RunState.WAITING_ON_APPROVAL}


def _run_has_approved_continuation(run: AgentRun) -> bool:
    progress = run.progress if isinstance(run.progress, dict) else {}
    error = run.error if isinstance(run.error, dict) else {}
    return progress.get("approved_for_continuation") is True or error.get("approved_for_continuation") is True


def _enrich_proof_lane_attribution(proof: Proof) -> None:
    """Default lane attribution onto proof metadata when the caller omitted it.

    Foreground/targeted-daemon goals run in a goal runtime instance even outside
    swarm mode; proofs should carry that lane identity so swarm-mode and
    single-lane evidence read the same way.
    """
    try:
        metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
        if not metadata.get("lane_id"):
            from .runtime_instances import GoalRuntimeInstanceStore

            instance_store = GoalRuntimeInstanceStore()
            instance = instance_store.active_for_task(proof.task_id) or instance_store.latest_for_task(proof.task_id)
            if instance is not None:
                metadata["lane_id"] = instance.id
        if not metadata.get("persona_instance_id"):
            run_id = str(metadata.get("run_id") or "").strip()
            if run_id:
                try:
                    run = RunStore().get(run_id)
                    if getattr(run, "persona_id", None):
                        metadata["persona_instance_id"] = f"personainst_{run.persona_id}"
                except Exception:
                    pass
        if not metadata.get("repo_bundle_ids"):
            from .repo_bundles import RepoBundleStore

            stage_id = str(getattr(proof, "stage_id", "") or "")
            bundle_ids = [
                bundle.id
                for bundle in RepoBundleStore().list_for_task(proof.task_id)
                if not stage_id or stage_id in (getattr(bundle, "stage_ids", None) or [])
            ]
            if bundle_ids:
                metadata["repo_bundle_ids"] = bundle_ids[:8]
        proof.metadata = metadata
    except Exception:
        # Attribution is observability; never fail proof attachment for it.
        pass


def _safe_proof_actor(value, *, fallback: str) -> str:
    if isinstance(value, str) and value in {"pm", "dev", "qa", "neko_supervisor", "supervisor", "harness"}:
        return value
    return fallback if fallback in {"pm", "dev", "qa", "neko_supervisor", "supervisor", "harness"} else "harness"


def _safe_run_id(value) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"run_[A-Za-z0-9_-]{1,64}", value):
        return value
    return None


def _safe_event_token(value, *, fallback: str | None) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        return value
    return fallback


def _safe_proof_status(value) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"passed", "failed", "timeout", "attached"}:
            return normalized
    return "attached"


def _safe_archive_actor(value: str) -> str:
    return value if value in {"cli", "harness", "dev", "qa", "neko_supervisor", "supervisor"} else "cli"


def _safe_archive_persona(value: str) -> str | None:
    return value if value in {"dev", "qa", "neko_supervisor", "supervisor"} else None


def _safe_archive_reason(value: str) -> str:
    lowered = str(value or "").lower()
    if any(marker in lowered for marker in ("secret", "token", "password", "credential", "authorization", "cookie", "key=")):
        return "operator archive command"
    return str(value or "archive terminal task")[:200]


def _archive_result(*, batch: str | None, archive_dir: Path | None, archived: list[dict], skipped: list[dict], manifest_path: Path | None) -> dict:
    return {
        "archive_batch": batch,
        "archive_dir": str(archive_dir) if archive_dir is not None else None,
        "archived_task_ids": [item["task_id"] for item in archived],
        "skipped_task_ids": [item["task_id"] for item in skipped],
        "archived_tasks": archived,
        "skipped_tasks": skipped,
        "archived_count": len(archived),
        "skipped_count": len(skipped),
        "manifest_path": str(manifest_path) if manifest_path is not None else None,
    }


def _archive_batch_name() -> str:
    stamp = now().strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}_archive_ready"


def _move_if_exists(source: Path, dest: Path) -> bool:
    if not source.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    shutil.move(str(source), str(dest))
    return True


def _active_worker_sessions_for_task(task_id: str):
    try:
        from .worker_sessions import WorkerSessionStore

        return WorkerSessionStore(event_log=EventLog()).find_active(task_id=task_id)
    except Exception:
        return []


def _archive_worker_evidence(task_id: str, archive_dir: Path) -> dict:
    result = {
        "worker_session_ids": [],
        "context_archived": False,
        "proof_sandbox_archived": False,
    }
    try:
        from .worker_sessions import WorkerSessionStore

        workers = WorkerSessionStore(event_log=EventLog()).list_for_task(task_id)
    except Exception:
        workers = []
    for worker in workers:
        dest = archive_dir / "worker_sessions" / f"{worker.id}.json"
        if _move_if_exists(paths.worker_session_path(worker.id), dest):
            result["worker_session_ids"].append(worker.id)
    context_src = paths.context_dir() / task_id
    context_dest = archive_dir / "context" / task_id
    if context_src.exists():
        _move_if_exists(context_src, context_dest)
        result["context_archived"] = True
    sandbox_src = paths.proof_sandbox_task_dir(task_id)
    sandbox_dest = archive_dir / "proof_sandbox" / task_id
    if sandbox_src.exists():
        _move_if_exists(sandbox_src, sandbox_dest)
        result["proof_sandbox_archived"] = True
    return result


def _archive_persona_assignment_evidence(task_id: str, archive_dir: Path) -> dict:
    result = {
        "persona_assignment_ids": [],
        "persona_assignments_archived": False,
    }
    try:
        assignments = PersonaAssignmentStore(event_log=EventLog()).list_for_task(task_id)
    except Exception:
        assignments = []
    for assignment in assignments:
        dest = archive_dir / "persona_assignments" / f"{assignment.id}.json"
        if _move_if_exists(paths.persona_assignment_path(assignment.id), dest):
            result["persona_assignment_ids"].append(assignment.id)
    result["persona_assignments_archived"] = bool(result["persona_assignment_ids"])
    return result


def _archive_persona_instance_evidence(task: Task, archive_dir: Path) -> dict:
    result = {
        "persona_instance_ids": [],
        "persona_instances_archived": False,
    }
    try:
        instances = PersonaInstanceStore(event_log=EventLog()).list_for_task(
            task.id,
            goal_id=getattr(task, "goal_id", None),
        )
    except Exception:
        instances = []
    for instance in instances:
        dest = archive_dir / "persona_instances" / f"{instance.id}.json"
        if _move_if_exists(paths.persona_instance_path(instance.id), dest):
            result["persona_instance_ids"].append(instance.id)
    result["persona_instances_archived"] = bool(result["persona_instance_ids"])
    return result


def _archive_repo_bundle_evidence(task_id: str, archive_dir: Path) -> dict:
    result = {
        "repo_bundle_ids": [],
        "repo_bundles_archived": False,
    }
    source = paths.repo_bundles_task_dir(task_id)
    if not source.exists():
        return result
    dest = archive_dir / "repo_bundles" / task_id
    if _move_if_exists(source, dest):
        result["repo_bundles_archived"] = True
        result["repo_bundle_ids"] = [
            path.stem
            for path in sorted(dest.glob("*.json"))
            if not path.name.endswith(".promotion.json")
        ]
    return result


def _archive_runtime_instance_evidence(task_id: str, archive_dir: Path) -> dict:
    result = {
        "runtime_instance_ids": [],
        "runtime_instances_archived": False,
    }
    source_dir = paths.runtime_instances_dir()
    if not source_dir.exists():
        return result
    for path in sorted(source_dir.glob("*.json")):
        try:
            instance = _read_model(GoalRuntimeInstance, path)
        except Exception:
            continue
        if instance.task_id != task_id:
            continue
        dest = archive_dir / "runtime_instances" / f"{instance.id}.json"
        if _move_if_exists(path, dest):
            result["runtime_instance_ids"].append(instance.id)
    result["runtime_instances_archived"] = bool(result["runtime_instance_ids"])
    return result


def _archive_packet_artifacts(task_id: str, archive_dir: Path) -> dict:
    result = {
        "packet_artifact_ids": [],
        "packet_artifacts_archived": False,
    }
    source = paths.packet_artifacts_task_dir(task_id)
    if not source.exists():
        return result
    dest = archive_dir / "packet_artifacts" / task_id
    if _move_if_exists(source, dest):
        result["packet_artifacts_archived"] = True
        result["packet_artifact_ids"] = [path.stem for path in sorted(dest.glob("*.json"))]
    return result




def _archive_role_envelope_evidence(task_id: str, archive_dir: Path) -> dict:
    result = {
        "role_envelope_ids": [],
        "role_checklist_ids": [],
        "role_state_archived": False,
    }
    for source, dest_name, pattern, key in (
        (paths.role_envelopes_task_dir(task_id), "role_envelopes", "*.json", "role_envelope_ids"),
        (paths.role_checklists_task_dir(task_id), "role_checklists", "*.json", "role_checklist_ids"),
    ):
        if not source.exists():
            continue
        dest = archive_dir / dest_name / task_id
        if _move_if_exists(source, dest):
            result["role_state_archived"] = True
            result[key] = [path.stem for path in sorted(dest.glob(pattern))]
    return result

def _archive_self_test_evidence(task_id: str, archive_dir: Path) -> dict:
    result = {
        "self_test_evidence_ids": [],
        "self_test_evidence_archived": False,
    }
    source = paths.self_test_task_dir(task_id)
    if not source.exists():
        return result
    dest = archive_dir / "self_tests" / task_id
    if _move_if_exists(source, dest):
        result["self_test_evidence_archived"] = True
        result["self_test_evidence_ids"] = [path.stem for path in sorted(dest.glob("selftest_*.json"))]
    return result


class IncidentStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def open(self, incident: Incident) -> Incident:
        _write_model(paths.incident_path(incident.id), incident)
        # NOTE: intentionally does NOT auto-link the incident into
        # task.open_incident_ids. The settle boundary (ticker._settled_boundary)
        # treats a linked incident as "Neko owns recovery" vs an unlinked one as
        # a hard stop, so centralized linking here changes stop-vs-recover
        # semantics. Linking must be done deliberately at the call sites that
        # want recovery routing, paired with a recovery-semantics review.
        metadata = incident.metadata if isinstance(getattr(incident, "metadata", None), dict) else {}
        payload = {"incident_id": incident.id, "kind": incident.kind}
        for key in ("proof_id", "check_id", "environment_fingerprint", "blocking_event_id", "stage_id", "persona_target", "lane_id", "lane_state_at_open"):
            value = metadata.get(key)
            if isinstance(value, str) and _safe_event_token(value, fallback=None):
                payload[key] = value
        if isinstance(metadata.get("budget_state"), dict):
            payload["budget_state"] = metadata["budget_state"]
        if isinstance(metadata.get("repo_lock_state"), dict):
            payload["repo_lock_state"] = metadata["repo_lock_state"]
        self.event_log.append(
            Event(
                ts=now(),
                type="incident.opened",
                task_id=incident.task_id,
                run_id=incident.run_id,
                persona_id=None,
                payload=payload,
            )
        )
        return incident

    def get(self, incident_id: str) -> Incident:
        return _read_model(Incident, paths.incident_path(incident_id))

    def close(self, incident_id: str, *, reason: str | None = None) -> Incident:
        from .locks import incident_lock

        with incident_lock(incident_id):
            incident = self.get(incident_id)
            was_open = incident.closed_at is None
            incident.closed_at = now()
            _write_model(paths.incident_path(incident_id), incident)
            if incident.task_id:
                try:
                    task_store = TaskStore()
                    task = task_store.get(incident.task_id)
                    changed = False
                    if incident.id in (task.open_incident_ids or []):
                        task.open_incident_ids = [item for item in task.open_incident_ids if item != incident.id]
                        changed = True
                    if was_open and task.state == TaskState.BLOCKED:
                        mark_incident_closed_for_recovery(task, incident_id=incident.id)
                        if not task.open_incident_ids:
                            task.state = TaskState.RUNNING
                        changed = True
                    if changed:
                        task.updated_at = now()
                        task_store.update(task, actor="harness", reason="incident closed")
                except NotFound:
                    pass
            payload = {"incident_id": incident.id}
            if reason:
                payload["reason"] = reason
            self.event_log.append(
                Event(
                    ts=now(),
                    type="incident.closed",
                    task_id=incident.task_id,
                    run_id=incident.run_id,
                    persona_id=None,
                    payload=payload,
                )
            )
            # S7-A producer: incidents ship open-only in the frame (settled), so a
            # close is a ``remove`` op — the launcher deletes the keyed row. Dark
            # by default (read_model.delta_patches off).
            emit_incident_remove(
                self.event_log,
                incident,
            )
            return incident

    def list_open(self) -> list[Incident]:
        incidents, _ = self.list_open_with_closed_count()
        return incidents

    def list_open_with_closed_count(self) -> tuple[list[Incident], int]:
        """Return live incidents without deserializing the closed-history tail.

        Mission Control keeps only open incidents in its steady-state frame;
        closed rows are represented by a count and fetched on demand.  Large
        long-lived runtimes can have thousands of closed incident files, and
        coercing every one into the recursive ``Incident`` dataclass graph on
        every snapshot made history cost as much as live state.

        We still JSON-decode every file so corrupt store rows fail exactly as
        ``list_all`` does.  Only closed rows skip the substantially more
        expensive ``from_jsonable`` pass.
        """

        directory = paths.incidents_dir()
        if not directory.exists():
            return [], 0
        open_incidents: list[Incident] = []
        closed_count = 0
        for path in directory.glob("*.json"):
            try:
                raw = _read_json(path)
            except NotFound:
                # Preserve the archive-race tolerance of ``_list_models``.
                continue
            if raw.get("closed_at") is not None:
                closed_count += 1
                continue
            open_incidents.append(from_jsonable(Incident, raw))
        return sorted(open_incidents, key=lambda item: item.id), closed_count

    def list_all(self) -> list[Incident]:
        return _list_models(Incident, paths.incidents_dir())
