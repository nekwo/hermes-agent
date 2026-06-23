from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .decision_schema import DecisionType
from .mission_plan import current_plan_stage, has_typed_plan, mission_plan_hud_enabled
from .models import AgentRun, Task, TaskStage
from .profile_context import active_profile_name
from .runtime_config import RuntimeConfig
from .stage_intent import no_product_edit_recipe_for_stage, no_product_edit_recipe_id, stage_requires_product_edit
from .stage_intent import stage_is_committed_verification_gate
from .states import StageStatus, TaskState


@dataclass(frozen=True, slots=True)
class WorkerAction:
    action_id: str
    decision_type: DecisionType
    shape_id: str
    label: str
    visible: bool = True
    primary: bool = False
    reason: str = ""
    payload_template: dict[str, Any] = field(default_factory=dict)
    not_allowed_reason: str | None = None

    def manifest(self) -> dict[str, Any]:
        payload = {
            "action_id": self.action_id,
            "decision_type": self.decision_type.value,
            "shape_id": self.shape_id,
            "label": self.label,
            "visible": self.visible,
            "primary": self.primary,
            "reason": self.reason,
            "payload_template": self.payload_template,
            "not_allowed_reason": self.not_allowed_reason,
        }
        return {key: value for key, value in payload.items() if value not in (None, "", {}, [])}


def normal_worker_flow_enabled(config: RuntimeConfig | None) -> bool:
    flow = getattr(config, "normal_worker_flow", None)
    return bool(getattr(flow, "enabled", False))


def worker_actions_for_role(
    role: str,
    task: Task,
    run: AgentRun,
    *,
    config: RuntimeConfig | None = None,
    proof_store=None,
) -> list[WorkerAction]:
    if not normal_worker_flow_enabled(config):
        return []
    resolved = _worker_role(role, run)
    if mission_plan_hud_enabled(config) and has_typed_plan(task):
        return _typed_actions(resolved, task, run, proof_store=proof_store)
    if resolved == "dev":
        return _dev_actions(task, run, proof_store=proof_store)
    if resolved == "qa":
        return _qa_actions(task, run, proof_store=proof_store)
    if resolved == "alice_supervisor":
        return _neko_actions(task, run)
    return [_block_action(primary=True, reason="Unknown worker role; block with evidence instead of guessing.")]


def _typed_actions(role: str, task: Task, run: AgentRun, *, proof_store=None) -> list[WorkerAction]:
    stage = current_plan_stage(task)
    if stage is None:
        if role == "alice_supervisor":
            return [
                WorkerAction(
                    "set_or_repair_plan",
                    DecisionType.PROPOSE_ACCEPTANCE,
                    "neko.scoped_handoff",
                    "Set Plan",
                    primary=True,
                    reason="No typed stage is active; create or repair the typed mission plan.",
                ),
                _block_action(reason="Use only if a typed plan cannot be safely created with current evidence."),
            ]
        return [
            WorkerAction(
                "request_context",
                DecisionType.REQUEST_FILE_READS,
                "common.request_file_reads",
                "Request Context",
                primary=True,
                reason="No typed executable stage is active; request the missing bounded context.",
            ),
            _block_action(reason="Cannot proceed without a typed current stage."),
        ]
    if role == "alice_supervisor":
        return [
            WorkerAction(
                "release_next_stage",
                DecisionType.PROPOSE_ACCEPTANCE,
                "neko.scoped_handoff",
                "Release Stage",
                primary=True,
                reason=f"Release or repair typed stage {stage.id}; preserve the parent mission intent.",
                payload_template={
                    "objective": "<release or repair summary>",
                    "acceptance_criteria": ["<unchanged parent criterion or typed stage criterion>"],
                    "release_stage_id": stage.id,
                },
            ),
            WorkerAction("route_recovery", DecisionType.TRIAGE_ISSUE_DISCOVERY, "neko.triage_issue_discovery", "Route Recovery"),
            _block_action(reason="Use only for true external/human/environment blockers."),
        ]
    stage_owner_role = _stage_owner_role(stage.owner)
    if role == "qa" or stage_owner_role == "qa":
        if _typed_visual_missing(task, proof_store=proof_store):
            return [
                WorkerAction(
                    "request_missing_proof",
                    DecisionType.REQUEST_SCREENSHOT,
                    "qa.request_screenshot",
                    "Request Visual Proof",
                    primary=True,
                    reason="Typed plan requires visual proof that is not attached yet.",
                    payload_template={
                        "stage_id": stage.id,
                        "target": "mission_control",
                        "proof_requirement": "fullscreen visual proof for the typed mission stage",
                        "mcp_server": "launcher_qa",
                        "required_launch_pins": {"hermes_profile": active_profile_name(), "runtime_root_id": "agent-runtime"},
                    },
                ),
                WorkerAction("report_verdict", DecisionType.REPORT_QA_VERDICT, "qa.report_qa_verdict", "QA Verdict"),
                _block_action(reason="Use when proof cannot be collected or verified."),
            ]
        return [
            WorkerAction(
                "report_verdict",
                DecisionType.REPORT_QA_VERDICT,
                "qa.report_qa_verdict",
                "QA Verdict",
                primary=True,
                reason="All typed blocking stage proof appears present; report an evidence-backed verdict.",
                payload_template={
                    "review_scope": "implementation",
                    "verdict": "approved",
                    "proof_ids": ["<proof_id if command/visual proof was required>"],
                    "delivery_packets_reviewed": ["<latest delivery packet id if no command proof was required>"],
                    "findings": [],
                },
            ),
            WorkerAction(
                "request_missing_proof",
                DecisionType.REQUEST_TEST_RUN,
                "qa.request_test_run",
                "Request Command Proof",
                reason="Use only when a typed command proof is missing or stale.",
            ),
            _block_action(reason="Use when proof cannot be collected or verified."),
        ]
    if role == "dev" and stage_owner_role == "dev":
        inferred_no_edit_recipe_id = _typed_no_edit_recipe_id(stage)
        no_edit_context_stage = _is_no_edit_context_stage(task, stage)
        if no_edit_context_stage and not _context_stage_should_request_gate(stage):
            if _fulfilled_context_request_count(task, actor=stage.owner, stage_id=stage.id) >= 2:
                return [
                    WorkerAction(
                        "deliver_findings",
                        DecisionType.PROPOSE_PATCH,
                        "dev.propose_patch",
                        "Deliver Findings",
                        primary=True,
                        reason=(
                            f"Typed no-edit investigation stage {stage.id} already has repeated context bundles; "
                            "deliver the findings and hardening plan from existing context now."
                        ),
                        payload_template={
                            "summary": "<investigation findings and recommended staged hardening plan>",
                            "changed_files": [],
                            "tests": ["no product edits; findings synthesized from fulfilled Harness context bundles"],
                            "delivery": {
                                "work_status": "patch_proposed",
                                "summary": "<one paragraph no-edit investigation conclusion>",
                                "findings": ["<grounded leakage cause or verified risk>"],
                                "recommendations": ["<staged hardening action>"],
                                "questions": ["<Tony decision needed, or omit if none>"],
                                "known_gaps": ["<remaining unknowns or none>"],
                            },
                        },
                    ),
                    _block_action(reason="Use only if the fulfilled context bundles are insufficient to produce a truthful report."),
                ]
            return [
                WorkerAction(
                    "request_context",
                    DecisionType.REQUEST_FILE_READS,
                    "common.request_file_reads",
                    "Request Context",
                    primary=True,
                    reason=f"Typed no-edit context stage {stage.id}; inspect the smallest relevant file/log set before delivering findings.",
                    payload_template={
                        "paths": ["<repo-relative file or directory to inspect>"],
                        "reason": "<why this context is required for the investigation>",
                    },
                ),
                _block_action(reason="Use when the needed investigation context is unavailable or outside this repo."),
            ]
        if stage.kind == "proof_only" or inferred_no_edit_recipe_id or (no_edit_context_stage and _context_stage_should_request_gate(stage)):
            payload: dict[str, Any] = {"stage_id": stage.id}
            recipe_id = stage.proof_recipe_id or inferred_no_edit_recipe_id
            if recipe_id:
                payload["recipe_id"] = recipe_id
            elif getattr(stage, "test_plan", None):
                payload["commands"] = list(stage.test_plan or [])[:3]
            return [
                WorkerAction(
                    "request_gate",
                    DecisionType.REQUEST_TEST_RUN,
                    "dev.request_test_run",
                    "Request Gate",
                    primary=True,
                    reason=f"Typed no-edit stage {stage.id}; ask Harness for the deterministic proof gate.",
                    payload_template=payload,
                ),
                WorkerAction("request_context", DecisionType.REQUEST_FILE_READS, "common.request_file_reads", "Request Context"),
                _block_action(reason="Use when the typed proof gate cannot run with current evidence."),
            ]
        if stage.kind == "implementation":
            delivered = bool(stage.packet_ids) or stage.status == StageStatus.READY_FOR_QA
            if delivered and stage.proof_recipe_id and not stage.proof_ids:
                return [
                    WorkerAction(
                        "request_gate",
                        DecisionType.REQUEST_TEST_RUN,
                        "dev.request_test_run",
                        "Request Gate",
                        primary=True,
                        reason=f"Typed implementation stage {stage.id} has delivery but is missing final command proof.",
                        payload_template={"stage_id": stage.id, "recipe_id": stage.proof_recipe_id},
                    ),
                    WorkerAction("request_context", DecisionType.REQUEST_FILE_READS, "common.request_file_reads", "Request Context"),
                    _block_action(),
                ]
            return [
                WorkerAction(
                    "deliver_patch",
                    DecisionType.PROPOSE_PATCH,
                    "dev.propose_patch",
                    "Deliver Patch",
                    primary=True,
                    reason=f"Typed implementation stage {stage.id}; edit, self-test in-session, then deliver.",
                    payload_template={
                        "summary": "<patch summary>",
                        "changed_files": ["<relative path>"],
                        "tests": ["<self-test command and status or not-run reason>"],
                        "delivery": {
                            "work_status": "patch_proposed",
                            "self_test_evidence_ids": ["<selftest_id>"],
                        },
                    },
                ),
                WorkerAction("request_context", DecisionType.REQUEST_FILE_READS, "common.request_file_reads", "Request Context"),
                _block_action(reason="Use when implementation is blocked by exact evidence."),
                WorkerAction(
                    "request_gate",
                    DecisionType.REQUEST_TEST_RUN,
                    "dev.request_test_run",
                    "Request Gate",
                    visible=False,
                    reason="Harness final gate runs after delivery.",
                    not_allowed_reason="Typed implementation stage has no accepted delivery yet; deliver_patch first.",
                ),
            ]
    return [_block_action(primary=True, reason=f"Typed stage {stage.id} is owned by {stage.owner}; current role cannot safely act.")]


def _context_stage_should_request_gate(stage) -> bool:
    text = " ".join([str(getattr(stage, "id", "") or ""), str(getattr(stage, "title", "") or ""), str(getattr(stage, "objective", "") or "")]).lower()
    if getattr(stage, "proof_recipe_id", None):
        return True
    test_plan = [str(item).strip() for item in (getattr(stage, "test_plan", None) or []) if str(item).strip()]
    return bool(test_plan) and ("do not inspect" in text or "efficiency smoke" in text or "diagnostic" in text)


def _is_no_edit_context_stage(task: Task, stage) -> bool:
    kind = str(getattr(stage, "kind", "") or "").strip().lower()
    if kind not in {"context", "investigation", "audit"}:
        return False
    return not stage_requires_product_edit(task, stage)


def _fulfilled_context_request_count(task: Task, *, actor: str | None = None, stage_id: str | None = None) -> int:
    owner = str(actor or "").strip()
    sid = str(stage_id or "").strip()
    count = 0
    for req in getattr(task, "context_requests", []) or []:
        if not isinstance(req, dict):
            continue
        if req.get("status") not in {"fulfilled", "fulfilled_partial", "superseded"}:
            continue
        req_actor = str(req.get("actor") or "").strip()
        if owner and req_actor and req_actor not in {owner, "dev", "backend_dev"}:
            continue
        req_stage_id = str(req.get("stage_id") or "").strip()
        reason = str(req.get("reason") or "")
        if sid and req_stage_id and req_stage_id != sid:
            continue
        if sid and not req_stage_id and sid not in reason:
            # Legacy requests did not always persist stage_id. If the task is
            # still on this stage, same-owner requests are current-stage
            # evidence; otherwise require an explicit reason match.
            if str(getattr(task, "current_stage_id", "") or "") != sid:
                continue
        count += 1
    return count


def _stage_owner_role(owner: str | None) -> str:
    value = str(owner or "").strip()
    if value == "backend_dev" or value.endswith("_dev") or value == "dev":
        return "dev"
    if value == "neko_supervisor":
        return "alice_supervisor"
    return value


def primary_worker_action(actions: list[WorkerAction]) -> WorkerAction | None:
    for action in actions:
        if action.visible and action.primary:
            return action
    for action in actions:
        if action.visible:
            return action
    return None


def project_worker_action_to_decision_shape(action: WorkerAction | None) -> str | None:
    return action.shape_id if action is not None else None


def _dev_actions(task: Task, run: AgentRun, *, proof_store=None) -> list[WorkerAction]:
    stage = _current_stage(task, run)
    stage_id = str(getattr(stage, "id", None) or getattr(run, "stage_id", None) or getattr(task, "current_stage_id", None) or "").strip()
    if stage is None:
        return [
            WorkerAction(
                "request_context",
                DecisionType.REQUEST_FILE_READS,
                "common.request_file_reads",
                "Request Context",
                primary=True,
                reason="No executable current stage is available; request the missing bounded stage context.",
            ),
            _block_action(reason="Cannot implement without a current stage."),
        ]
    failed_proof_ids = _failed_proof_ids(task)
    product_edit = stage_requires_product_edit(task, stage)
    recipe_id = no_product_edit_recipe_for_stage(stage)
    no_edit_stage = not product_edit and (_stage_has_command_gate(stage) or _stage_mentions_no_edit(stage) or bool(recipe_id))
    committed_verification_gate = product_edit and not failed_proof_ids and stage_is_committed_verification_gate(task, stage)
    if committed_verification_gate:
        payload_template: dict[str, Any] = {"stage_id": stage_id}
        if getattr(stage, "test_plan", None):
            payload_template["commands"] = list(stage.test_plan or [])[:3]
        return [
            WorkerAction(
                "request_gate",
                DecisionType.REQUEST_TEST_RUN,
                "dev.request_test_run",
                "Request Gate",
                primary=True,
                reason="Committed implementation verification stage with exact commands; ask Harness for proof instead of rediscovering the repo.",
                payload_template=payload_template,
            ),
            WorkerAction(
                "request_context",
                DecisionType.REQUEST_FILE_READS,
                "common.request_file_reads",
                "Request Context",
                reason="Use only if the named proof command is ambiguous or unsafe.",
            ),
            _block_action(reason="Use when the committed verification proof cannot run with current environment evidence."),
        ]
    if product_edit:
        actions = [
            WorkerAction(
                "deliver_patch",
                DecisionType.PROPOSE_PATCH,
                "dev.propose_patch",
                "Deliver Patch",
                primary=True,
                reason=_dev_delivery_reason(failed_proof_ids),
                payload_template={
                    "summary": "<patch summary>",
                    "changed_files": ["<relative path>"],
                    "tests": ["<self-test command and status or not-run reason>"],
                    "delivery": {
                        "work_status": "patch_proposed",
                        "self_test_evidence_ids": ["<selftest_id>"],
                    },
                },
            ),
            WorkerAction(
                "request_context",
                DecisionType.REQUEST_FILE_READS,
                "common.request_file_reads",
                "Request Context",
                reason="Use only for one bounded missing file/log/context item.",
            ),
            _block_action(reason="Use when environment/provider/dependency evidence prevents implementation."),
            WorkerAction(
                "repair_stage",
                DecisionType.CORRECT_STAGE,
                "dev.correct_stage",
                "Repair Stage",
                reason="Use only when the current stage is demonstrably stale or wrong.",
            ),
            WorkerAction(
                "request_gate",
                DecisionType.REQUEST_TEST_RUN,
                "dev.request_test_run",
                "Request Gate",
                visible=False,
                reason="Harness final gate runs after delivery.",
                not_allowed_reason="Product-edit stage has no accepted delivery yet; self-test locally and deliver_patch first.",
            ),
        ]
        return actions
    if no_edit_stage:
        payload_template: dict[str, Any] = {"stage_id": stage_id}
        if recipe_id:
            payload_template["recipe_id"] = recipe_id
        elif no_product_edit_recipe_id(stage_id):
            payload_template["recipe_id"] = stage_id
        elif getattr(stage, "test_plan", None):
            payload_template["commands"] = list(stage.test_plan or [])[:3]
        return [
            WorkerAction(
                "request_gate",
                DecisionType.REQUEST_TEST_RUN,
                "dev.request_test_run",
                "Request Gate",
                primary=True,
                reason="No-product-edit or explicit proof stage; ask Harness for the deterministic gate.",
                payload_template=payload_template,
            ),
            WorkerAction("request_context", DecisionType.REQUEST_FILE_READS, "common.request_file_reads", "Request Context"),
            _block_action(reason="Use when the no-edit proof cannot run with current environment evidence."),
        ]
    return [
        WorkerAction(
            "deliver_patch",
            DecisionType.PROPOSE_PATCH,
            "dev.propose_patch",
            "Deliver Patch",
            primary=True,
            reason="Implement or document the concrete stage work, self-test locally, then deliver.",
        ),
        WorkerAction("request_context", DecisionType.REQUEST_FILE_READS, "common.request_file_reads", "Request Context"),
        _block_action(),
    ]


def _qa_actions(task: Task, run: AgentRun, *, proof_store=None) -> list[WorkerAction]:
    stage = _current_stage(task, run)
    stage_id = str(getattr(stage, "id", None) or getattr(run, "stage_id", None) or getattr(task, "current_stage_id", None) or "").strip()
    if _visual_required(task, stage) and not _has_visual_proof(task, proof_store=proof_store):
        return [
            WorkerAction(
                "request_missing_visual_gate",
                DecisionType.REQUEST_SCREENSHOT,
                "qa.request_screenshot",
                "Request Visual Gate",
                primary=True,
                reason="Visual proof is required but no usable screenshot/video proof is attached.",
                payload_template={
                    "stage_id": stage_id,
                    "target": "mission_control",
                    "proof_requirement": "fullscreen visual proof for the current stage",
                    "mcp_server": "launcher_qa",
                    "required_launch_pins": {"hermes_profile": active_profile_name(), "runtime_root_id": "agent-runtime"},
                },
            ),
            WorkerAction("qa_verdict", DecisionType.REPORT_QA_VERDICT, "qa.report_qa_verdict", "QA Verdict"),
            _block_action(reason="Use when proof/environment is blocked and cannot be independently verified."),
        ]
    return [
        WorkerAction(
            "qa_verdict",
            DecisionType.REPORT_QA_VERDICT,
            "qa.report_qa_verdict",
            "QA Verdict",
            primary=True,
            reason="Review final gate proof and issue an evidence-backed verdict.",
        ),
        WorkerAction(
            "request_missing_command_gate",
            DecisionType.REQUEST_TEST_RUN,
            "qa.request_test_run",
            "Request Command Gate",
            reason="Use only when a required final command proof is missing or stale.",
        ),
        _block_action(reason="Use when proof/environment is blocked and cannot be independently verified."),
    ]


def _neko_actions(task: Task, run: AgentRun) -> list[WorkerAction]:
    state = task.state if isinstance(task.state, TaskState) else TaskState(task.state)
    if state == TaskState.READY_FOR_REVIEW:
        return [
            WorkerAction(
                "release_handoff",
                DecisionType.PROPOSE_ACCEPTANCE,
                "neko.qa_coordination_release",
                "Release Handoff",
                primary=True,
                reason="Dev proof is attached; release QA with joined proof IDs.",
            ),
            WorkerAction("route_repair", DecisionType.TRIAGE_ISSUE_DISCOVERY, "neko.triage_issue_discovery", "Route Repair"),
            _block_action(),
        ]
    if state == TaskState.BLOCKED and getattr(task, "open_incident_ids", None):
        return [
            WorkerAction(
                "route_repair",
                DecisionType.RESOLVE_INCIDENT,
                "neko.resolve_incident",
                "Route Repair",
                primary=True,
                reason="Task is blocked; resolve a specific incident only with evidence.",
            ),
            _block_action(),
        ]
    return [
        WorkerAction(
            "assign_scope",
            DecisionType.PROPOSE_ACCEPTANCE,
            "neko.scoped_handoff",
            "Assign Scope",
            primary=True,
            reason="Scope the next bounded owner and proof gate.",
        ),
        WorkerAction("route_repair", DecisionType.TRIAGE_ISSUE_DISCOVERY, "neko.triage_issue_discovery", "Route Repair"),
        _block_action(reason="Use only for true human/safety/environment blockers."),
    ]


def _block_action(*, primary: bool = False, reason: str = "Use when the next safe action is impossible with current evidence.") -> WorkerAction:
    return WorkerAction("report_blocker", DecisionType.BLOCK, "common.block", "Report Blocker", primary=primary, reason=reason)


def _typed_visual_missing(task: Task, *, proof_store=None) -> bool:
    stage = current_plan_stage(task)
    if stage is None:
        return False
    if not any(getattr(item, "requires_visual_proof", False) for item in (getattr(getattr(task, "mission_plan", None), "stages", []) or [])):
        return False
    if proof_store is None:
        return True
    for proof_id in list(getattr(task, "proof_ids", []) or []):
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            continue
        proof_type = proof.type.value if hasattr(proof.type, "value") else str(proof.type)
        if proof_type in {"screenshot", "video"} and getattr(proof, "path_or_value", None):
            return False
    return True


def _current_stage(task: Task, run: AgentRun) -> TaskStage | None:
    stage_id = str(getattr(run, "stage_id", None) or getattr(task, "current_stage_id", None) or "").strip()
    for stage in getattr(task, "stages", []) or []:
        if stage.id == stage_id:
            return stage
    return None


def _typed_no_edit_recipe_id(stage) -> str | None:
    recipe_id = str(getattr(stage, "proof_recipe_id", "") or "").strip()
    if no_product_edit_recipe_id(recipe_id):
        return recipe_id
    text = " ".join(
        [
            str(getattr(stage, "id", "") or ""),
            str(getattr(stage, "title", "") or ""),
            str(getattr(stage, "objective", "") or ""),
        ]
    ).lower().replace("_", " ").replace("-", " ")
    if (
        str(getattr(stage, "repo", "") or "").strip() == "hermes-agent"
        and "harness" in text
        and any(marker in text for marker in ("status", "snapshot", "log", "logs", "thinking", "observability", "smoke"))
        and any(marker in text for marker in ("no product edit", "without product edit", "no edit", "proof"))
    ):
        return "harness_runtime_status_snapshot"
    return None


def _worker_role(role: str, run: AgentRun) -> str:
    if role == "backend_dev" or role.endswith("_dev"):
        return "dev"
    if role in {"alice_supervisor", "dev", "qa"}:
        return role
    persona_id = str(getattr(run, "persona_id", "") or "")
    if persona_id == "neko_supervisor":
        return "alice_supervisor"
    if persona_id == "qa":
        return "qa"
    if persona_id == "backend_dev" or persona_id.endswith("_dev") or persona_id == "dev":
        return "dev"
    return role


def _stage_has_command_gate(stage: TaskStage) -> bool:
    return any(_looks_like_command(item) for item in getattr(stage, "test_plan", []) or [])


def _stage_mentions_no_edit(stage: TaskStage) -> bool:
    text = " ".join(
        [
            stage.id,
            stage.title,
            stage.objective,
            *list(stage.audit_notes or []),
            *list(stage.acceptance_criteria or []),
            *list(stage.test_plan or []),
        ]
    ).lower()
    return "no-edit" in text or "no product edit" in text or bool(no_product_edit_recipe_id(stage.id)) or bool(no_product_edit_recipe_for_stage(stage))


def _looks_like_command(value: object) -> bool:
    text = str(value or "").strip().lower()
    return bool(text) and any(marker in text for marker in ("pytest", "flutter ", "dart ", "python ", "manage.py", "npm ", "pnpm "))


def _failed_proof_ids(task: Task) -> list[str]:
    stage_state = (getattr(task, "harness_self_heal", None) or {}).get("stage_state")
    if isinstance(stage_state, dict):
        values = stage_state.get("last_failed_proof_ids")
        if isinstance(values, list):
            return [str(item) for item in values if str(item).strip()][:5]
    return []


def _dev_delivery_reason(failed_proof_ids: list[str]) -> str:
    if failed_proof_ids:
        return "A final gate failed; repair in the same worker session, self-test, and deliver the patched fix."
    return "Product-edit stage is implementing; edit, self-test in-session, then deliver_patch. Harness runs final gate after delivery."


def _visual_required(task: Task, stage: TaskStage | None) -> bool:
    if bool(getattr(task, "requires_visual_proof", False)):
        return True
    if stage is not None and getattr(stage, "requires_visual_proof", None) is True:
        return True
    text = " ".join(
        [
            str(getattr(task, "title", "")),
            str(getattr(task, "description", "")),
            str(getattr(stage, "title", "")),
            str(getattr(stage, "objective", "")),
        ]
    ).lower()
    return any(marker in text for marker in ("ui", "visual", "screenshot", "mission control", "launcher", "widget"))


def _has_visual_proof(task: Task, *, proof_store=None) -> bool:
    if proof_store is None:
        return False
    for proof_id in list(getattr(task, "proof_ids", []) or []):
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            continue
        proof_type = proof.type.value if hasattr(proof.type, "value") else str(proof.type)
        if proof_type in {"screenshot", "video"} and getattr(proof, "path_or_value", None):
            return True
    return False
