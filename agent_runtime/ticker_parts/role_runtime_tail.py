# Loaded by agent_runtime.ticker via _load_ticker_parts(); executed in ticker.py globals.
# Role-session, routing, repair, proof-workdir, and text-intent helpers live here.

def _close_role_session(
    envelope: RoleSessionEnvelope | None,
    *,
    run,
    close_reason: str,
    next_action_before: str | None = None,
    next_action_after: str | None = None,
    proof_ids_added: list[str] | None = None,
    incident_ids_opened: list[str] | None = None,
    would_continue: bool | None = None,
) -> None:
    if envelope is None:
        return
    if envelope.close_reason:
        return
    envelope.close_reason = close_reason
    try:
        EventLog().append(
            Event(
                now(),
                "role_session.closed",
                envelope.task_id,
                run.id,
                envelope.persona_id,
                role_session_payload(
                    envelope,
                    run=run,
                    close_reason=close_reason,
                    next_action_before=next_action_before,
                    next_action_after=next_action_after,
                    proof_ids_added=proof_ids_added,
                    incident_ids_opened=incident_ids_opened,
                    would_continue=would_continue,
                ),
            )
        )
    except Exception:
        return


def _safe_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _runtime_budget_block(task: Task, *, persona_id: str, run_store: RunStore, config: RuntimeConfig) -> dict[str, Any] | None:
    mission_limit = _safe_int(getattr(config, "mission_max_total_tokens", None))
    mission_total = _run_token_total(run_store.list_for_task(task.id))
    stage_id = getattr(task, "current_stage_id", None)
    if mission_limit is not None and mission_limit > 0 and mission_total >= mission_limit:
        return {
            "kind": "mission",
            "event_type": "mission_budget_exceeded",
            "summary": f"Mission token budget exceeded: total_tokens={mission_total}/{mission_limit}",
            "task_id": task.id,
            "persona_id": persona_id,
            "stage_id": stage_id,
            "total_tokens": mission_total,
            "limit": mission_limit,
        }

    swarm = getattr(config, "swarm", None)
    supervision = getattr(config, "supervision", None)
    hierarchical_budget = bool(getattr(supervision, "hierarchical_budget_enabled", False))
    if not bool(getattr(swarm, "enabled", False)) and not hierarchical_budget:
        return None
    swarm_limit = _safe_int(getattr(swarm, "global_token_hard_limit", None))
    swarm_total = _run_token_total(run_store.list_all())
    if swarm_limit is not None and swarm_limit > 0 and swarm_total >= swarm_limit:
        return {
            "kind": "swarm",
            "event_type": "swarm_budget_exceeded",
            "summary": f"Swarm token budget exceeded: total_tokens={swarm_total}/{swarm_limit}",
            "task_id": task.id,
            "persona_id": persona_id,
            "stage_id": stage_id,
            "total_tokens": swarm_total,
            "limit": swarm_limit,
        }
    if hierarchical_budget:
        child_limit = _safe_int(getattr(swarm, "per_lane_token_limit", None)) or swarm_limit
        if child_limit is not None and child_limit > 0:
            child_total = _child_token_total(run_store.list_for_task(task.id), persona_id=persona_id)
            if child_total >= child_limit:
                return {
                    "kind": "swarm",
                    "event_type": "swarm_budget_exceeded",
                    "summary": f"Child token budget exceeded: total_tokens={child_total}/{child_limit}",
                    "task_id": task.id,
                    "persona_id": persona_id,
                    "stage_id": stage_id,
                    "total_tokens": child_total,
                    "limit": child_limit,
                }
    return None


def _deploy_verification_enabled(config: RuntimeConfig) -> bool:
    supervision = getattr(config, "supervision", None)
    return bool(getattr(supervision, "deploy_verification_enabled", False))


def _commit_child_event_offset(action: HarnessAction, *, persona_store: PersonaInstanceStore | None = None) -> bool:
    parent_node_id = str(getattr(action, "parent_node_id", "") or "").strip()
    if not parent_node_id:
        return False
    try:
        offset = int(getattr(action, "child_events_offset", None) or 0)
    except (TypeError, ValueError):
        return False
    if offset <= 0:
        return False
    store = persona_store or PersonaInstanceStore()
    try:
        parent = store.get(parent_node_id)
    except Exception:
        return False
    parent.child_events_offset = max(int(getattr(parent, "child_events_offset", 0) or 0), offset)
    store.update(parent)
    return True


def _first_deploy_contention_warning(store: PersonaAssignmentStore, *, persona_id: str, goal_id: str, enabled: bool) -> dict[str, Any] | None:
    if not enabled:
        return None
    warnings = store.contention_warnings(persona_id=persona_id, goal_id=goal_id)
    return warnings[0] if warnings else None


def _verify_child_deploy_started(*, config: RuntimeConfig, run, worker, assignment, child_instance_id: str | None) -> str | None:
    if not _deploy_verification_enabled(config):
        return None
    if run is None or getattr(run, "state", None) != RunState.RUNNING:
        return "child run did not enter running state"
    if getattr(run, "last_heartbeat_at", None) is None:
        return "child run has no initial heartbeat"
    if assignment is not None and run.id not in list(getattr(assignment, "run_ids", []) or []):
        return "child assignment did not attach the opened run"
    if worker is not None:
        if getattr(worker, "active_run_id", None) != run.id:
            return "child worker did not attach the opened run"
        if getattr(worker, "last_heartbeat_at", None) is None:
            return "child worker has no initial heartbeat"
    if not child_instance_id:
        return "child persona instance was not created"
    return None


def _child_deploy_failed_result(
    action: HarnessAction,
    *,
    task: Task,
    persona_id: str,
    child_instance_id: str | None,
    reason: str,
    assignment_id: str | None = None,
    run_id: str | None = None,
    retryable: bool = False,
    event_log: EventLog | None = None,
) -> HarnessActionResult:
    if child_instance_id:
        emit_child_deploy_failed(
            child_instance_id=child_instance_id,
            reason=reason,
            task_id=task.id,
            assignment_id=assignment_id,
            stage_id=getattr(action, "stage_id", None) or getattr(task, "current_stage_id", None),
            persona_id=persona_id,
            retryable=retryable,
            summary=reason,
            event_log=event_log or EventLog(),
        )
    return HarnessActionResult(
        action,
        False,
        f"child deploy failed: {reason}",
        {
            "reason": reason,
            "assignment_id": assignment_id,
            "run_id": run_id,
            "persona_id": persona_id,
            "stage_id": getattr(action, "stage_id", None) or getattr(task, "current_stage_id", None),
            "retryable": bool(retryable),
        },
    )


def _task_for_action(task: Task, action: HarnessAction) -> Task:
    action_task = copy.deepcopy(task)
    stage_id = str(getattr(action, "stage_id", "") or "").strip()
    if not stage_id:
        return action_task
    action_task.current_stage_id = stage_id
    plan = getattr(action_task, "mission_plan", None)
    if plan is not None:
        known = {stage.id for stage in list(getattr(plan, "stages", None) or [])}
        if stage_id in known:
            plan.current_stage_id = stage_id
    return action_task


def _swarm_lane_concurrency_enabled(config: RuntimeConfig) -> bool:
    swarm = getattr(config, "swarm", None)
    if not bool(getattr(swarm, "enabled", False)):
        return False
    try:
        from .burn_in import swarm_certification_allows_production

        allowed, _summary = swarm_certification_allows_production(
            allow_uncertified_dev_swarm=bool(getattr(swarm, "allow_uncertified_dev_swarm", False)),
            requires_certification=bool(getattr(swarm, "requires_certification", True)),
        )
        return bool(allowed)
    except Exception:
        return not bool(getattr(swarm, "requires_certification", True))


def _max_active_lanes(config: RuntimeConfig) -> int:
    swarm = getattr(config, "swarm", None)
    try:
        return max(1, int(getattr(swarm, "max_active_lanes", 1) or 1))
    except (TypeError, ValueError):
        return 1


def _run_token_total(runs) -> int:
    total = 0
    for run in runs:
        llm = getattr(run, "llm", None)
        if not isinstance(llm, dict):
            continue
        tokens = _safe_int(llm.get("total_tokens"))
        if tokens is not None and tokens > 0:
            total += tokens
    return total


def _child_token_total(runs, *, persona_id: str) -> int:
    total = 0
    for run in runs:
        if getattr(run, "persona_id", None) != persona_id:
            continue
        llm = getattr(run, "llm", None)
        if not isinstance(llm, dict):
            continue
        tokens = _safe_int(llm.get("total_tokens"))
        if tokens is not None and tokens > 0:
            total += tokens
    return total


def _persona_value(persona, field: str, default):
    value = getattr(persona, field, None)
    return value if value is not None else default


def _persona_int(persona, field: str, default: int) -> int:
    value = _safe_int(getattr(persona, field, None))
    return max(1, value) if value is not None else default


def _persona_with_instance_model_overrides(persona, *, child_instance=None, assignment=None):
    """Overlay the persona-instance model override tier for run/tick model
    resolution (see models.apply_instance_model_overrides).

    Resolves the instance from the already-materialized goal child instance
    when present, else from the assignment's persona_instance_id pointer.
    Failure-tolerant by contract: an unreadable/missing instance record falls
    through to the persona's own values — a model switch must never be able to
    crash a tick.
    """
    instance = child_instance
    if instance is None and assignment is not None:
        instance_id = str(getattr(assignment, "persona_instance_id", "") or "").strip()
        if instance_id:
            try:
                instance = PersonaInstanceStore().get(instance_id)
            except Exception:
                instance = None
    try:
        return apply_instance_model_overrides(persona, instance)
    except Exception:
        return persona


def _apply_deterministic_proof_handoff(task: Task, proof_ids: list[str], decision, *, proof_store: ProofStore, actor: str, run_id: str) -> bool:
    if not proof_ids:
        return False
    proofs = []
    for proof_id in proof_ids:
        try:
            proofs.append(proof_store.get(proof_id))
        except Exception:
            return False
    if any(str(proof.type.value if hasattr(proof.type, "value") else proof.type) != "test_run" for proof in proofs):
        return False

    stage_id = str(decision.payload.get("stage_id") or task.current_stage_id or "").strip()
    acceptable_statuses = {"passed"}
    if _is_red_stage(task, stage_id):
        acceptable_statuses.add("failed")
    if any(str((proof.metadata or {}).get("status", "")).strip() not in acceptable_statuses for proof in proofs):
        return False

    mismatched_labels = _proof_repo_mismatch_labels(task, proofs, actor=actor, stage_id=stage_id)
    if mismatched_labels:
        task.proof_ids = _dedupe(list(getattr(task, "proof_ids", None) or []), proof_ids)
        if stage_id:
            _set_current_stage_id(task, stage_id)
            _set_stage_status(task, stage_id, StageStatus.BLOCKED)
        if "command_proof_repo_mismatch" not in task.risk_flags:
            task.risk_flags.append("command_proof_repo_mismatch")
        task.state = TaskState.RUNNING
        task.updated_at = now()
        EventLog().append(
            Event(
                ts=now(),
                type="run.progress",
                task_id=task.id,
                run_id=run_id,
                persona_id=actor,
                payload={
                    "type": "run.progress",
                    "source": "deterministic_proof_handoff",
                    "phase": "proof",
                    "step": "proof_repo_mismatch",
                    "stage_id": stage_id,
                    "proof_ids": proof_ids,
                    "workdir_labels": mismatched_labels,
                    "status": "blocked",
                    "summary": "Passing command proof came from a workdir that does not match the current stage repo intent.",
                    "next_expected": "neko_self_heal_or_corrected_dev_proof",
                },
            )
        )
        return False

    command_mismatches = _proof_command_stage_mismatch_labels(task, proofs, stage_id=stage_id)
    if command_mismatches:
        task.proof_ids = _dedupe(list(getattr(task, "proof_ids", None) or []), proof_ids)
        target_stage_id = _proof_command_stage_mismatch_target_stage_id(task, proofs, stage_id=stage_id) or stage_id
        if target_stage_id:
            _set_current_stage_id(task, target_stage_id)
            _set_stage_status(task, target_stage_id, StageStatus.IMPLEMENTING)
            if stage_id and stage_id != target_stage_id:
                stage = _stage_for_command_proof(task, stage_id)
                if stage is not None and stage.status == StageStatus.IMPLEMENTING:
                    _set_stage_status(task, stage_id, StageStatus.BLOCKED)
        if "command_proof_stage_mismatch" not in task.risk_flags:
            task.risk_flags.append("command_proof_stage_mismatch")
        task.state = TaskState.RUNNING
        task.updated_at = now()
        EventLog().append(
            Event(
                ts=now(),
                type="run.progress",
                task_id=task.id,
                run_id=run_id,
                persona_id=actor,
                payload={
                    "type": "run.progress",
                    "source": "deterministic_proof_handoff",
                    "phase": "proof",
                    "step": "proof_command_stage_mismatch",
                    "stage_id": stage_id,
                    "corrected_current_stage_id": target_stage_id,
                    "proof_ids": proof_ids,
                    "commands": command_mismatches,
                    "status": "needs_corrected_proof",
                    "summary": "Passing command proof did not satisfy the current stage proof contract.",
                    "next_expected": "corrected_stage_proof",
                },
            )
        )
        return False

    if stage_id:
        _set_current_stage_id(task, stage_id)
        _set_stage_status(task, stage_id, StageStatus.READY_FOR_QA)
    task.risk_flags = [
        flag
        for flag in (getattr(task, "risk_flags", None) or [])
        if flag
        not in {
            "command_proof_stage_mismatch",
            "command_proof_repo_mismatch",
        }
    ]
    if not _all_stages_dev_complete(task):
        if _needs_sequential_specialist_join(task):
            task.state = TaskState.RUNNING
        else:
            _advance_to_next_dev_stage(task)
            task.state = TaskState.RUNNING
    else:
        task.state = TaskState.RUNNING
    waits_for_launcher_join = task.state == TaskState.RUNNING and _needs_cross_stack_launcher_completion(task, proof_store=proof_store)
    contract_packet_id = None
    if waits_for_launcher_join:
        contract_packet_id = _ensure_backend_contract_packet_for_handoff(
            task,
            proof_ids,
            decision,
            actor=actor,
            run_id=run_id,
            stage_id=stage_id,
        )
        handoff_status = "backend_join_ready"
        handoff_summary = "Passing backend command proof attached; routed to Neko for Launcher join release without another Backend Dev tick."
        next_expected = "neko_cross_stack_launcher_release"
    elif task.state == TaskState.RUNNING:
        handoff_status = "ready_for_qa"
        handoff_summary = "Passing command proof attached; routed to QA without another Dev model tick."
        next_expected = "qa_verification"
    else:
        handoff_status = "next_stage_ready"
        handoff_summary = "Passing command proof attached; advanced to the next implementation stage."
        next_expected = "dev_next_stage"
    EventLog().append(
        Event(
            ts=now(),
            type="task.transition",
            task_id=task.id,
            run_id=run_id,
            persona_id=actor,
            payload={
                "source": "deterministic_proof_handoff",
                "phase": "handoff",
                "step": "deterministic_proof_handoff",
                "status": handoff_status,
                "summary": handoff_summary,
                "proof_count": len(proof_ids),
                "stage_id": stage_id,
                "next_expected": next_expected,
                "contract_packet_id": contract_packet_id,
                "to": task.state.value,
            },
        )
    )
    return True


def _ensure_backend_contract_packet_for_handoff(task: Task, proof_ids: list[str], decision, *, actor: str, run_id: str, stage_id: str) -> str | None:
    if actor != "backend_dev" or not proof_ids:
        return None
    if _has_backend_contract_delivery_packet(task):
        return None
    proof_id = str(proof_ids[0]).strip()
    if not proof_id:
        return None
    safe_stage = (stage_id or "backend_contract").replace(" ", "_")
    contract_packet_id = f"backend_contract_packet_{task.id}_{safe_stage}"
    source_packet = latest_packet(task.id, "handoff_packet")
    source_packet_id = str((source_packet or {}).get("packet_id") or "").strip()
    body = {
        "source_handoff_packet_id": source_packet_id,
        "consumed_contract_packet_ids": [],
        "consumed_proof_ids": [],
        "produced_contract_packet_id": contract_packet_id,
        "contract_packet": {
            "contract_packet_id": contract_packet_id,
            "surface": "Harness-owned backend_contract_smoke handoff",
            "contract_status": "tested",
            "request_shape": {
                "repo_scope": "EterniaBackend",
                "required_recipe_id": "backend_contract_smoke",
                "mode": "no_product_edit",
            },
            "response_shape": {
                "required_backend_proof_id": proof_id,
                "required_backend_proof_status": "passed",
                "next_handoff_packet_kind": "contract_join",
                "next_target_repo": "EterniaLauncher",
            },
            "error_shape": {
                "missing_backend_proof": "block Launcher release until backend_contract_smoke proof passes",
                "premature_qa": "block until backend and Launcher proof IDs are both attached",
            },
            "example_response": {
                "proof_id": proof_id,
                "recipe_id": "backend_contract_smoke",
                "status": "passed",
            },
        },
        "proof_ids": [proof_id],
        "proof_summary": "backend_contract_smoke passed; no product edits certified by Harness proof recipe",
        "command_summary": "Harness-owned backend_contract_smoke proof command passed",
        "known_gaps": [],
        "next_owner": "neko_supervisor",
        "operator_note": "Synthesized by Harness after backend proof to avoid a second Backend Dev packet-only turn.",
    }
    log = EventLog()
    packet = make_packet(task=task, decision=decision, packet_type="delivery", body=body, actor=actor, run_id=run_id, stage_id=stage_id)
    if record_packet(packet, event_log=log):
        log.append(
            Event(
                ts=now(),
                type="run.progress",
                task_id=task.id,
                run_id=run_id,
                persona_id=actor,
                payload={
                    "type": "run.progress",
                    "source": "deterministic_proof_handoff",
                    "phase": "handoff",
                    "step": "backend_contract_packet_synthesized",
                    "status": "recorded",
                    "stage_id": stage_id,
                    "proof_id": proof_id,
                    "contract_packet_id": contract_packet_id,
                    "summary": "Harness synthesized backend delivery packet from passed backend_contract_smoke proof before Neko Launcher join.",
                    "next_expected": "neko_cross_stack_launcher_release",
                },
            )
        )
    return contract_packet_id


def _record_command_proof_self_heal(
    task: Task,
    proof_ids: list[str],
    *,
    proof_store: ProofStore,
    stage_id: str | None,
    actor: str,
    run_id: str,
) -> None:
    if not proof_ids:
        return
    failed_ids: list[str] = []
    passed_ids: list[str] = []
    environment_status: str | None = None
    for proof_id in proof_ids:
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            continue
        metadata = proof.metadata or {}
        status = str(metadata.get("status") or "").strip().lower()
        if status == "failed":
            failed_ids.append(proof_id)
        elif status == "passed":
            passed_ids.append(proof_id)
        if metadata.get("environment_fingerprint_status"):
            environment_status = str(metadata.get("environment_fingerprint_status")).strip()[:80]
    if not failed_ids and not passed_ids:
        return

    root = dict(getattr(task, "harness_self_heal", {}) or {})
    stages = dict(root.get("stages") or {})
    key = stage_id or getattr(task, "current_stage_id", None) or "_mission"
    state = dict(stages.get(key) or {})
    counters = dict(state.get("counters") or {})

    if failed_ids:
        previous_failed = [str(item).strip() for item in (state.get("last_failed_proof_ids") or []) if str(item).strip()]
        new_failed = [proof_id for proof_id in failed_ids if proof_id not in previous_failed]
        if previous_failed and new_failed:
            counters["same_stage_retry_count"] = (_safe_int(counters.get("same_stage_retry_count")) or 0) + 1
        state["last_failed_proof_ids"] = _dedupe(previous_failed, failed_ids)[-20:]
        if environment_status:
            state["environment_fingerprint_status"] = environment_status
        if counters:
            state["counters"] = counters
        stages[key] = state
        current_key = getattr(task, "current_stage_id", None)
        if current_key and current_key != key:
            stages[current_key] = dict(state)
        root["stages"] = stages
        task.harness_self_heal = root
        EventLog().append(
            Event(
                ts=now(),
                type="run.progress",
                task_id=task.id,
                run_id=run_id,
                persona_id=actor,
                payload={
                    "type": "run.progress",
                    "source": "command_proof_self_heal",
                    "phase": "proof",
                    "step": "failed_proof_recorded",
                    "stage_id": key,
                    "status": "failed",
                    "last_failed_proof_ids": list(state["last_failed_proof_ids"]),
                    "same_stage_retry_count": counters.get("same_stage_retry_count", 0),
                    "summary": "Failed command proof recorded for bounded retry or Neko self-heal.",
                    "next_expected": "dev_bounded_retry" if not counters.get("same_stage_retry_count") else "neko_self_heal",
                },
            )
        )
        return

    if passed_ids:
        changed = False
        if state.get("last_failed_proof_ids"):
            state.pop("last_failed_proof_ids", None)
            changed = True
        if counters.get("same_stage_retry_count"):
            counters.pop("same_stage_retry_count", None)
            changed = True
        if environment_status:
            state["environment_fingerprint_status"] = environment_status
            changed = True
        if counters:
            state["counters"] = counters
        elif "counters" in state:
            state.pop("counters", None)
        if changed:
            stages[key] = state
            root["stages"] = stages
            task.harness_self_heal = root


def _record_failed_proof_block_after_reuse(task: Task, decision, *, actor: str, run_id: str) -> bool:
    if decision.type != DecisionType.BLOCK:
        return False
    payload = decision.payload if isinstance(getattr(decision, "payload", None), dict) else {}
    proof_ids = [str(item).strip() for item in (payload.get("failed_proof_ids") or []) if str(item).strip()] if isinstance(payload.get("failed_proof_ids"), list) else []
    if not proof_ids:
        root = getattr(task, "harness_self_heal", {}) or {}
        stages = root.get("stages") if isinstance(root, dict) else {}
        state = stages.get(getattr(task, "current_stage_id", None) or "_mission") if isinstance(stages, dict) else {}
        proof_ids = [str(item).strip() for item in (state.get("last_failed_proof_ids") or []) if str(item).strip()] if isinstance(state, dict) else []
    if not proof_ids:
        return False

    root = dict(getattr(task, "harness_self_heal", {}) or {})
    stages = dict(root.get("stages") or {})
    key = getattr(task, "current_stage_id", None) or "_mission"
    state = dict(stages.get(key) or {})
    counters = dict(state.get("counters") or {})
    counters["same_stage_retry_count"] = max(1, (_safe_int(counters.get("same_stage_retry_count")) or 0) + 1)
    state["last_failed_proof_ids"] = _dedupe([str(item).strip() for item in (state.get("last_failed_proof_ids") or []) if str(item).strip()], proof_ids)[-20:]
    if "environment_fingerprint_status" not in state:
        state["environment_fingerprint_status"] = "unchanged"
    state["counters"] = counters
    stages[key] = state
    root["stages"] = stages
    task.harness_self_heal = root
    EventLog().append(
        Event(
            ts=now(),
            type="run.progress",
            task_id=task.id,
            run_id=run_id,
            persona_id=actor,
            payload={
                "type": "run.progress",
                "source": "command_proof_self_heal",
                "phase": "self_heal",
                "step": "failed_proof_block_recorded",
                "stage_id": key,
                "status": "blocked",
                "last_failed_proof_ids": list(state["last_failed_proof_ids"]),
                "same_stage_retry_count": counters["same_stage_retry_count"],
                "summary": "Dev blocked after reusing a failed proof without an environment change; route Neko self-heal before another same-stage Dev run.",
                "next_expected": "neko_self_heal",
            },
        )
    )
    return True


def _proof_repo_mismatch_labels(task: Task, proofs: list, *, actor: str, stage_id: str) -> list[str]:
    intent = _command_proof_repo_intent(task, actor=actor, stage_id=stage_id)
    if intent is None:
        return []
    mismatches: list[str] = []
    for proof in proofs:
        metadata = proof.metadata or {}
        label = str(metadata.get("workdir_label") or "").strip()
        if not label:
            continue
        if _workdir_label_conflicts_intent(label, intent):
            mismatches.append(label[:120])
    return mismatches


def _workdir_label_conflicts_intent(label: str, intent: str) -> bool:
    if _repo_text_matches_intent(label, intent):
        return False
    return any(
        other != intent and _repo_text_matches_intent(label, other)
        for other in ("backend", "launcher", "harness")
    )


def _proof_command_stage_mismatch_labels(task: Task, proofs: list, *, stage_id: str) -> list[str]:
    incomplete_product_stage = first_incomplete_product_edit_stage(task, excluding_stage_id=stage_id)
    current_stage = _stage_for_command_proof(task, stage_id)
    current_stage_id = str(getattr(current_stage, "id", "") or "").strip()
    downstream_depends_on_current = bool(
        current_stage_id
        and incomplete_product_stage is not None
        and current_stage_id in [str(item).strip() for item in (getattr(incomplete_product_stage, "depends_on", None) or [])]
    )
    if (
        (current_stage is not None and stage_requires_product_edit(task, current_stage))
        or (incomplete_product_stage is not None and not downstream_depends_on_current)
    ):
        mismatches = []
        for proof in proofs:
            if _proof_is_no_product_edit_smoke(proof):
                metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
                command = str(metadata.get("command") or "").strip()
                recipe = str(metadata.get("proof_recipe_recipe_id") or "").strip()
                mismatches.append(
                    f"{recipe or 'no_product_edit_smoke'}:{command[:220] or '<missing command>'}"
                )
        if mismatches:
            return mismatches
    if not _stage_requires_bridge_archive_regression(task, stage_id):
        return []
    mismatches: list[str] = []
    for proof in proofs:
        metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
        command = str(metadata.get("command") or "").strip()
        normalized = command.lower().replace("\\", "/")
        if (
            "mission_control_bridge_test.dart" in normalized
            and "mission_control_snapshot_test.dart" in normalized
        ):
            continue
        mismatches.append(command[:240] or "<missing command>")
    return mismatches


def _proof_command_stage_mismatch_target_stage_id(task: Task, proofs: list, *, stage_id: str) -> str | None:
    if not any(_proof_is_no_product_edit_smoke(proof) for proof in proofs):
        return None
    target = first_incomplete_product_edit_stage(task, excluding_stage_id=stage_id)
    return target.id if target is not None else None


def _proof_is_no_product_edit_smoke(proof) -> bool:
    metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
    recipe_id = str(metadata.get("proof_recipe_recipe_id") or "").strip()
    if not recipe_id:
        recipe = metadata.get("proof_recipe")
        if isinstance(recipe, dict):
            recipe_id = str(recipe.get("recipe_id") or "").strip()
    if no_product_edit_recipe_id(recipe_id):
        return True
    command = str(metadata.get("command") or "").strip().lower()
    stdout = str(metadata.get("stdout_excerpt") or metadata.get("stdout") or "").strip().lower()
    return any(
        marker in f"{command}\n{stdout}"
        for marker in (
            "launcher_contract_smoke",
            "backend_contract_smoke",
            "archive_button_cli_contract",
            "harness_runtime_status_snapshot",
            "qa_release_verdict_smoke",
        )
    )


def _stage_requires_bridge_archive_regression(task: Task, stage_id: str) -> bool:
    stage = _stage_for_command_proof(task, stage_id)
    if stage is None:
        return False
    text = " ".join(
        [
            str(stage.id or ""),
            str(stage.title or ""),
            str(stage.objective or ""),
            " ".join(str(item) for item in (stage.acceptance_criteria or [])),
            " ".join(str(item) for item in (stage.test_plan or [])),
        ]
    ).lower().replace("_", "-")
    return (
        "mission control" in text
        and ("bridge" in text or "snapshot" in text or "archive" in text)
        and ("regression" in text or "test" in text or "coverage" in text)
    )


def _is_red_stage(task: Task, stage_id: str) -> bool:
    for stage in _runtime_stage_records(task):
        if stage.id == stage_id:
            text = " ".join(str(value or "") for value in (stage.id, stage.title, stage.objective)).lower()
            return any(marker in text for marker in ("red", "failing test", "prove tests fail"))
    return False


def _is_retryable_provider_failure(kind: str, exc: Exception) -> bool:
    text = str(exc).lower()
    if kind in {"provider_auth_failure", "runtime_dependency_missing", "model_invalid_output", "tool_policy_violation"}:
        return False
    if kind == "provider_rate_limit":
        return True
    if kind == "provider_failure":
        return any(marker in text for marker in ("ttfb", "first byte", "no bytes", "timeout", "timed out", "temporarily", "connection reset", "server error", "http 5"))
    return False


def _latest_model_invalid_repair_error(
    run_store: RunStore,
    *,
    task_id: str,
    persona_id: str,
    stage_id: str | None,
) -> str | None:
    try:
        runs = run_store.list_for_task(task_id)
    except Exception:
        return None
    candidates = [
        run
        for run in runs
        if run.persona_id == persona_id
        and run.stage_id == stage_id
        and run.state == RunState.FAILED
    ]
    if not candidates and stage_id:
        candidates = [
            run
            for run in runs
            if run.persona_id == persona_id
            and run.stage_id is None
            and run.state == RunState.FAILED
        ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda run: run.finished_at or run.last_heartbeat_at or run.started_at)
    llm = latest.llm if isinstance(latest.llm, dict) else {}
    if llm.get("validation_status") != "invalid":
        return None
    progress = latest.progress if isinstance(latest.progress, dict) else {}
    error = latest.error if isinstance(latest.error, dict) else {}
    if progress.get("approved_for_continuation") is True or error.get("approved_for_continuation") is True:
        return None
    message = str(error.get("message") or error.get("summary") or "").strip()
    if not message:
        return None
    return _safe_repair_error(message)


_DECISION_REPAIR_MAX_ATTEMPTS = 1


def _should_retry_invalid_decision(exc: DecisionPayloadInvalid, *, repair_attempts: int) -> bool:
    if repair_attempts >= _DECISION_REPAIR_MAX_ATTEMPTS:
        return False
    text = str(exc).strip().lower()
    if not text:
        return False
    if "dev stage plan loop guard failed" in text:
        return False
    return True


def _is_dev_stage_plan_loop_guard(exc: Exception) -> bool:
    if not isinstance(exc, DecisionPayloadInvalid):
        return False
    return "dev stage plan loop guard failed" in str(exc).strip().lower()


def _decision_repair_feedback(
    exc: DecisionPayloadInvalid,
    *,
    decision,
    repair_attempt: int,
) -> str:
    message = _safe_repair_error(str(exc)) or "decision failed Harness contract validation"
    payload: dict[str, Any] = {
        "message": message,
        "decision_type": getattr(getattr(decision, "type", None), "value", None) or str(getattr(decision, "type", "")),
        "repair_attempt": repair_attempt,
        "max_repair_attempts": _DECISION_REPAIR_MAX_ATTEMPTS,
    }
    invalid_field = _invalid_field_for_repair_message(message)
    if invalid_field:
        payload["invalid_field"] = invalid_field
        invalid_value = _extract_invalid_decision_value(decision, invalid_field)
        if invalid_value is not None:
            payload["invalid_value"] = _safe_value_preview(invalid_value)
    return json.dumps({key: value for key, value in payload.items() if value is not None}, sort_keys=True)


def _record_decision_repair_request(
    run_store: RunStore,
    task: Task,
    run,
    *,
    persona_id: str,
    exc: DecisionPayloadInvalid,
    decision,
    repair_attempt: int,
    worker_store=None,
    worker=None,
):
    repair_error = _decision_repair_feedback(exc, decision=decision, repair_attempt=repair_attempt)
    _capture_replay_scenario(task, run, persona_id=persona_id, exc=exc, decision=decision)
    run = _refresh_run_for_update(run_store, run)
    repair_payload = _decision_repair_progress_payload(repair_error, repair_attempt=repair_attempt)
    run.progress = {**(run.progress or {}), **repair_payload}
    run.llm = {
        **(run.llm or {}),
        "validation_status": "repair_requested",
        "last_validation_error": _safe_repair_error(str(exc)),
        "schema_repair_attempts": repair_attempt,
    }
    run_store.update(run)
    EventLog().append(Event(now(), "run.progress", task.id, run.id, persona_id, repair_payload))
    if worker_store is not None and worker is not None:
        worker_store.heartbeat(worker.id)
    return run, repair_error


def _capture_replay_scenario(task, run, *, persona_id: str, exc: DecisionPayloadInvalid, decision) -> None:
    """Auto-capture every live contract failure as a replay scenario candidate."""
    try:
        from .replay_scenarios import classify_failure_origin, record_scenario_candidate

        payload = getattr(decision, "payload", None) if decision is not None else None
        record_scenario_candidate(
            task_id=getattr(task, "id", ""),
            run_id=getattr(run, "id", None),
            persona_id=persona_id,
            decision_type=getattr(getattr(decision, "type", None), "value", None),
            payload=payload,
            error_class=type(exc).__name__,
            error_message=_safe_repair_error(str(exc)) or str(exc),
            failure_origin=classify_failure_origin(task=task, run=run, payload=payload, error_message=str(exc)),
        )
    except Exception:
        # Scenario capture is observability, never a reason to fail the repair path.
        pass


def _decision_repair_progress_payload(repair_error: str | None, *, repair_attempt: int) -> dict[str, Any]:
    try:
        parsed = json.loads(repair_error or "{}")
    except Exception:
        parsed = {}
    parsed = parsed if isinstance(parsed, dict) else {}
    payload: dict[str, Any] = {
        "type": "run.progress",
        "source": "decision_contract_repair",
        "phase": "contract_repair",
        "step": "decision_validation_failed",
        "status": "repair_requested",
        "summary": str(parsed.get("message") or repair_error or "decision failed Harness contract validation")[:500],
        "repair_attempt": repair_attempt,
        "max_repair_attempts": _DECISION_REPAIR_MAX_ATTEMPTS,
        "next_expected": "corrected_agent_decision",
    }
    for key in ("decision_type", "invalid_field", "invalid_value"):
        if parsed.get(key) is not None:
            payload[key] = parsed[key]
    return payload


def _invalid_field_for_repair_message(message: str) -> str | None:
    text = str(message or "").lower()
    if "request_screenshot" in text or "request_video" in text or "mcp_server" in text or "required_launch_pins" in text:
        if "mcp_server" in text:
            return "payload.mcp_server"
        if "required_launch_pins.hermes_profile" in text or "hermes_profile" in text:
            return "payload.required_launch_pins.hermes_profile"
        if "required_launch_pins.runtime_root_id" in text or "runtime_root_id" in text:
            return "payload.required_launch_pins.runtime_root_id"
        if "required_launch_pins" in text:
            return "payload.required_launch_pins"
        if "proof_requirement" in text:
            return "payload.proof_requirement"
        if "target" in text and "target_repo" not in text:
            return "payload.target"
        if "stage_id" in text:
            return "payload.stage_id"
        return "payload"
    if "missing payload keys" in text and "target" in text and "target_repo" not in text:
        return "payload.target"
    if "delivery.next_owner" in text:
        return "payload.delivery.next_owner"
    if "delivery.work_status" in text:
        return "payload.delivery.work_status"
    if "qa_review.coverage" in text:
        return "payload.qa_review.coverage"
    if "qa_review.next_owner" in text:
        return "payload.qa_review.next_owner"
    if "handoff_packet." in text:
        for field in ("target_owner", "next_owner", "final_owner", "target_repo", "next_repo", "final_repo", "handoff_mode"):
            if f"handoff_packet.{field}" in text:
                return f"payload.handoff_packet.{field}"
        return "payload.handoff_packet"
    if "proof_ids" in text:
        return "payload.proof_ids"
    if "recipe_id" in text:
        return "payload.recipe_id"
    if "commands" in text or "proof command" in text or "proof policy" in text:
        return "payload.commands"
    return None


def _extract_invalid_decision_value(decision, invalid_field: str) -> Any:
    payload = getattr(decision, "payload", None)
    if not isinstance(payload, dict):
        return None
    path = invalid_field
    if path.startswith("payload."):
        path = path[len("payload.") :]
    current: Any = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _safe_value_preview(value: Any) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_safe_value_preview(item) for item in value[:8]]
    if isinstance(value, dict):
        return {str(key)[:80]: _safe_value_preview(val) for key, val in list(value.items())[:8]}
    text = " ".join(str(value).split())
    if not text:
        return ""
    lowered = text.lower()
    if ":/" in text or "\\" in text or "token" in lowered or "secret" in lowered or "password" in lowered:
        return "<redacted>"
    return text[:160]


def _safe_repair_error(message: str) -> str | None:
    text = " ".join(message.split())
    if not text:
        return None
    if ":/" in text or "\\" in text:
        return "previous decision failed contract validation with path-like content; return a redaction-safe AgentDecision"
    return text[:500]


def _current_stage(task: Task):
    if not getattr(task, "current_stage_id", None):
        return None
    return next((stage for stage in _runtime_stage_records(task) if stage.id == task.current_stage_id), None)


def _is_live_persona_runtime(runtime) -> bool:
    return runtime is not None and runtime.__class__.__name__ == "GPTPersonaRuntime"


def _enterprise_worker_sessions_enabled(config: RuntimeConfig) -> bool:
    enterprise = getattr(config, "enterprise_worker_sessions", None)
    if enterprise is None:
        return False
    return bool(getattr(enterprise, "enabled", False) and getattr(enterprise, "worker_session_store", True))


def _supported_runner_kwargs(callable_obj, optional: dict) -> dict:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return {key: value for key, value in optional.items() if value is not None}
    parameters = signature.parameters
    accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values())
    if accepts_kwargs:
        return {key: value for key, value in optional.items() if value is not None}
    return {key: value for key, value in optional.items() if value is not None and key in parameters}


def _proof_intent_from_decision(decision) -> str:
    payload = decision.payload if isinstance(getattr(decision, "payload", None), dict) else {}
    explicit = payload.get("proof_intent") or payload.get("intent")
    command_count = len(payload.get("commands") or []) if isinstance(payload.get("commands"), list) else 0
    stage_id = str(payload.get("stage_id") or "").strip()
    if explicit:
        return _safe_metadata_text(explicit)
    summary = str(getattr(decision, "summary", "") or "collect deterministic command proof").strip()
    return _safe_metadata_text(f"{summary}; command_count={command_count}; stage_id={stage_id or 'current'}")


def _environment_fingerprint_payload(task: Task, stage_id: str | None) -> dict:
    state = _task_stage_self_heal_state(task, stage_id)
    fingerprint = state.get("last_environment_fingerprint") or state.get("environment_fingerprint")
    status = state.get("environment_fingerprint_status")
    safe_status = _safe_metadata_token(status) or ("recorded" if fingerprint else "unknown")
    return {
        "environment_fingerprint": _safe_metadata_text(fingerprint or safe_status),
        "environment_fingerprint_status": safe_status,
    }


def _task_stage_self_heal_state(task: Task, stage_id: str | None) -> dict:
    root = getattr(task, "harness_self_heal", None)
    if not isinstance(root, dict):
        return {}
    stages = root.get("stages") if isinstance(root.get("stages"), dict) else root
    if not isinstance(stages, dict):
        return {}
    state = stages.get(stage_id or getattr(task, "current_stage_id", None) or "_mission")
    return state if isinstance(state, dict) else {}


def _safe_metadata_text(value) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "unknown"
    if ":/" in text or "\\" in text or text.startswith(("/", "~")):
        return "redacted_path_like_value"
    lowered = text.lower()
    if any(marker in lowered for marker in ("secret=", "token=", "password=", "api_key=", "apikey=", "bearer ")):
        return "redacted_sensitive_value"
    return text[:500]


def _safe_metadata_token(value) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"unknown", "recorded", "changed", "unchanged", "blocked", "missing"}:
        return text
    return None


def _refresh_run_for_update(run_store: RunStore, source) -> Any:
    try:
        run = run_store.get(source.id)
    except Exception:
        return source
    if isinstance(getattr(source, "llm", None), dict):
        timing = _safe_timing_map(run.llm)
        timing.update(_safe_timing_map(source.llm))
        run.llm = {**(run.llm or {}), **source.llm}
        if timing:
            run.llm["timing"] = timing
    safe_session_id = _safe_session_id(getattr(source, "session_id", None))
    if safe_session_id:
        run.session_id = safe_session_id
    return run


def _record_failed_proof_auto_attachment(run, task: Task, decision, *, actor: str) -> bool:
    payload = decision.payload if isinstance(getattr(decision, "payload", None), dict) else {}
    if payload.get("failed_proof_auto_attached") is not True:
        return False
    proof_ids = [str(item).strip() for item in (payload.get("failed_proof_ids") or []) if str(item).strip()] if isinstance(payload.get("failed_proof_ids"), list) else []
    if not proof_ids:
        return False
    progress = dict(getattr(run, "progress", None) or {})
    progress["failed_proof_reused"] = True
    progress["failed_proof_auto_attached"] = True
    progress["last_failed_proof_ids"] = proof_ids
    run.progress = progress
    EventLog().append(
        Event(
            ts=now(),
            type="run.progress",
            task_id=task.id,
            run_id=run.id,
            persona_id=actor,
            payload={
                "type": "run.progress",
                "source": "dev_progress_gate",
                "phase": "self_heal",
                "step": "failed_proof_auto_attached",
                "status": "ready",
                "stage_id": getattr(task, "current_stage_id", None) or getattr(run, "stage_id", None),
                "failed_proof_ids": proof_ids,
                "summary": "Attached known failed proof IDs to the Dev retry decision before proof execution.",
                "next_expected": "bounded_retry_or_neko_self_heal",
            },
        )
    )
    return True


def _attach_stage_self_heal_to_run_progress(run, task: Task) -> None:
    root = getattr(task, "harness_self_heal", {}) or {}
    stages = root.get("stages") if isinstance(root, dict) else {}
    if not isinstance(stages, dict):
        return
    state = stages.get(getattr(task, "current_stage_id", None) or "_mission")
    if not isinstance(state, dict):
        return
    progress = dict(run.progress or {})
    for key in ("last_failed_proof_ids", "environment_fingerprint_status"):
        if key in state:
            progress[key] = state[key]
    if isinstance(state.get("self_heal"), dict) and state["self_heal"].get("attempt_number"):
        progress["self_heal_applied"] = True
    run.progress = progress


def _budget_approval_incidents_for_task(incidents: list[Incident], run_store: RunStore, *, cap: int = 2) -> list[Incident]:
    """Return budget incidents that Neko can safely steer into same-session continuation."""
    return eligible_budget_approval_incidents(incidents, run_store, cap=cap)


def _budget_scope_recovery_incidents_for_task(incidents: list[Incident], run_store: RunStore) -> list[Incident]:
    """Return budget incidents where Neko should split scope instead of approving continuation."""
    return [incident for incident in incidents if budget_incident_needs_scope_recovery(incident, run_store)]


def _hard_environment_blocker_incidents(incidents: list[Incident]) -> list[Incident]:
    return [incident for incident in incidents if str(getattr(incident, "kind", "") or "") == "environment_blocker"]


def choose_next_action(task: Task) -> HarnessAction:
    return MissionStateMachine().next_action(task)


def _persona_id_for_harness_action(action: HarnessAction, *, task: Task | None = None, config: RuntimeConfig | None = None, run_store: RunStore | None = None) -> str | None:
    if action.type == HarnessActionType.RUN_SLOT:
        slot_id = str(action.slot_id or "").strip()
        plan = getattr(task, "mission_plan", None) if task is not None else None
        bindings = getattr(plan, "bindings", None) if plan is not None else None
        if isinstance(bindings, dict) and slot_id:
            resolved = str(bindings.get(slot_id) or "").strip()
            if resolved:
                return resolved
        if slot_id == "dev":
            return _dev_persona_id_for_task(task, config=config, run_store=run_store)
        return slot_id or None
    return None


def _spawned_by_for_harness_action(action: HarnessAction, *, task: Task | None = None) -> str | None:
    if action.type != HarnessActionType.RUN_SLOT:
        return None
    slot_id = str(action.slot_id or "").strip()
    plan = getattr(task, "mission_plan", None) if task is not None else None
    bindings = getattr(plan, "bindings", None) if plan is not None else None
    if slot_id in {"lead", "neko_supervisor"}:
        return "operator"
    if isinstance(bindings, dict):
        lead = str(bindings.get("lead") or bindings.get("coordinator") or "").strip()
        if lead:
            return lead
    return "neko_supervisor"


def _persona_id_for_action(action_type: HarnessActionType, *, task: Task | None = None, config: RuntimeConfig | None = None, run_store: RunStore | None = None) -> str | None:
    return None


def _action_targets(action: HarnessAction, *slot_ids: str) -> bool:
    return action.type == HarnessActionType.RUN_SLOT and str(action.slot_id or "").strip() in set(slot_ids)


def _dev_persona_id_for_task(task: Task | None, *, config: RuntimeConfig | None = None, run_store: RunStore | None = None) -> str:
    """Choose the narrowest configured Dev specialist for a task's repo scope."""
    if task is None:
        return "dev"
    continuation_persona_id = _approved_continuation_persona_id(task, run_store)
    if continuation_persona_id:
        return continuation_persona_id
    typed_stage = current_plan_stage(task)
    if typed_stage is not None and typed_stage.owner in {"dev", "backend_dev"}:
        return typed_stage.owner
    stage_persona_id = _dev_persona_id_from_current_stage(task)
    if stage_persona_id:
        return stage_persona_id
    handoff_persona_id = _dev_persona_id_from_latest_handoff(task)
    if handoff_persona_id:
        return handoff_persona_id
    labels = {str(label).strip().lower() for label in safe_affected_repo_labels(list(getattr(task, "affected_repos", []) or []))}
    raw_repos = {str(repo).strip().lower() for repo in (getattr(task, "affected_repos", []) or []) if str(repo).strip()}
    haystack = " ".join(sorted(labels | raw_repos))
    if "eterniabackend" in haystack or "eternia-backend" in haystack or "backend" in haystack:
        return "backend_dev"

    cfg = config if hasattr(config, "personas") else load_agent_runtime_config()
    try:
        personas = ensure_persisted_personas(cfg)
    except Exception:
        personas = []
    for persona in personas:
        if str(getattr(persona, "role", "")) != "dev" or persona.id == "dev":
            continue
        repo_label = str(getattr(persona, "repo_scope_label", "") or "").strip().lower()
        repo_scope = str(getattr(persona, "repo_scope", "") or "").strip().lower()
        if repo_label and repo_label in labels:
            return persona.id
        if repo_scope and any(repo_scope in raw or raw in repo_scope for raw in raw_repos):
            return persona.id
    return "dev"


def _dev_persona_id_from_current_stage(task: Task) -> str | None:
    stage_id = str(getattr(task, "current_stage_id", "") or "").strip().lower()
    stage = _current_stage(task)
    title = str(getattr(stage, "title", "") or "").strip().lower()
    objective = str(getattr(stage, "objective", "") or "").strip().lower()
    affected_paths = [str(item).lower() for item in (getattr(stage, "affected_paths", None) or [])]
    test_plan = [str(item).lower() for item in (getattr(stage, "test_plan", None) or [])]
    haystack = " ".join(
        [
            stage_id,
            title,
            objective,
            " ".join(affected_paths),
            " ".join(test_plan),
        ]
    )
    backend_identity = (
        "backend" in stage_id
        or title.startswith("backend")
        or "eterniabackend" in haystack
        or any("eterniabackend" in path or "eternia-backend" in path for path in affected_paths)
        or any("scripts/test.sh" in item or "manage.py" in item for item in test_plan)
    )
    if backend_identity:
        return "backend_dev"
    if any(marker in haystack for marker in ("launcher", "frontend", "front-end", "ui", "eternialauncher")):
        return "dev"
    return None


def _dev_persona_id_from_latest_handoff(task: Task) -> str | None:
    try:
        packet = latest_packet(task.id, "handoff_packet", stage_id=getattr(task, "current_stage_id", None))
    except Exception:
        return None
    body = packet.get("body") if isinstance(packet, dict) else None
    if not isinstance(body, dict):
        return None
    target_dev_persona = str(body.get("target_dev_persona") or "").strip()
    if target_dev_persona in {"dev", "backend_dev"}:
        return target_dev_persona
    target_owner = str(body.get("target_owner") or "").strip()
    target_repo = str(body.get("target_repo") or "").strip()
    if target_owner in {"dev", "backend_dev"}:
        if target_owner == "dev" and target_repo == "EterniaLauncher":
            return "dev"
        if target_owner == "backend_dev" or target_repo == "EterniaBackend":
            return "backend_dev"
    return None


def _approved_continuation_persona_id(task: Task, run_store: RunStore | None) -> str | None:
    if run_store is None:
        return None
    try:
        runs = run_store.list_for_task(task.id)
    except Exception:
        return None
    dev_runs = [
        run for run in runs
        if run.stage_id == task.current_stage_id
        and (run.persona_id == "dev" or str(run.persona_id).endswith("_dev"))
    ]
    dev_runs.sort(key=_run_order_key)
    for run in reversed(dev_runs):
        if (
            run.state == RunState.FAILED
            and isinstance(run.error, dict)
            and run.error.get("type") == RUN_BUDGET_EXCEEDED
            and run.error.get("approved_for_continuation")
            and _safe_session_id(run.session_id)
        ):
            if _has_later_persona_run(dev_runs, run):
                continue
            return run.persona_id
    return None


def _has_later_persona_run(runs: list, candidate) -> bool:
    candidate_key = _run_order_key(candidate)
    return any(
        run.persona_id == candidate.persona_id
        and _run_order_key(run) > candidate_key
        for run in runs
    )


def _run_order_key(run) -> tuple[str, str]:
    return (
        str(getattr(run, "started_at", "") or ""),
        str(getattr(run, "id", "") or ""),
    )


def _approved_continuation_count(task: Task, run_store: RunStore | None, persona_id: str) -> int:
    if run_store is None:
        return 0
    try:
        runs = run_store.list_for_task(task.id)
    except Exception:
        return 0
    return sum(
        1
        for run in runs
        if run.stage_id == task.current_stage_id
        and run.persona_id == persona_id
        and run.state == RunState.FAILED
        and isinstance(run.error, dict)
        and run.error.get("type") == RUN_BUDGET_EXCEEDED
        and run.error.get("approved_for_continuation")
        and _safe_session_id(run.session_id)
    )


def _continuation_token_budget(base_limit, approved_count: int):
    base = _safe_int(base_limit)
    if base is None or approved_count <= 0:
        return base_limit
    return base * (approved_count + 1)


def _prior_stage_run_progress_flags(task: Task, run_store: RunStore | None, persona_id: str, *, exclude_run_id: str | None = None) -> dict[str, bool]:
    if run_store is None:
        return {}
    try:
        runs = run_store.list_for_task(task.id)
    except Exception:
        return {}
    flags: dict[str, bool] = {}
    for run in runs:
        if exclude_run_id and run.id == exclude_run_id:
            continue
        if run.stage_id != task.current_stage_id or run.persona_id != persona_id:
            continue
        progress = run.progress if isinstance(run.progress, dict) else {}
        if progress.get("has_patch_progress") is True or (_safe_int(progress.get("patch_count")) or 0) > 0:
            flags["has_patch_progress"] = True
        if progress.get("has_test_progress") is True or (_safe_int(progress.get("test_count")) or 0) > 0:
            flags["has_test_progress"] = True
        if progress.get("has_proof_progress") is True or (_safe_int(progress.get("proof_count")) or 0) > 0:
            flags["has_proof_progress"] = True
    return flags


def _initial_run_llm_metadata(persona, config: RuntimeConfig, *, retry_attempt: int, retry_max_attempts: int) -> dict:
    metadata = {
        "provider": persona.provider or getattr(config, "default_provider", None),
        "model": persona.model or getattr(config, "default_model", None),
        "api_mode": persona.api_mode or getattr(config, "default_api_mode", None),
        "retry_attempt": retry_attempt,
        "retry_max_attempts": retry_max_attempts,
    }
    return {key: value for key, value in metadata.items() if value is not None}


def _safe_traceback_frames(exc: BaseException, *, limit: int = 8) -> list[dict[str, object]]:
    frames: list[dict[str, object]] = []
    for frame in traceback.extract_tb(exc.__traceback__)[-limit:]:
        path = Path(frame.filename)
        frames.append(
            {
                "file": path.name,
                "module_path_tail": "/".join(path.parts[-3:]),
                "line": frame.lineno,
                "function": frame.name,
            }
        )
    return frames


def _get_persona(agent_store: AgentStore, persona_id: str, config: RuntimeConfig | None = None):
    stored = {persona.id: persona for persona in agent_store.list_all()}
    if persona_id in stored:
        return stored[persona_id]
    cfg = config if hasattr(config, "personas") else load_agent_runtime_config()
    return get_persisted_persona(persona_id, cfg)


def _dedupe(existing: list[str], incoming: list[str]) -> list[str]:
    result = list(existing)
    seen = set(result)
    for item in incoming:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _command_workdir_for_task(task: Task, explicit_workdir=None, *, actor: str | None = None, stage_id: str | None = None) -> Path:
    try:
        if explicit_workdir is None:
            repo_scope = _command_proof_repo_scope(task, actor=actor, stage_id=stage_id)
            if repo_scope:
                scoped_task = type("TaskCommandProofRepoScope", (), {"affected_repos": [repo_scope]})()
                return command_workdir_for_task(scoped_task)
        return command_workdir_for_task(task, explicit_workdir=explicit_workdir)
    except ValueError as exc:
        safe_repos = safe_affected_repo_labels(list(getattr(task, "affected_repos", []) or []))
        raise ValueError(
            "request_test_run could not resolve a valid affected repo workdir; "
            f"affected_repos={safe_repos!r}"
        ) from exc


def _isolated_workdir_from_run_progress(run) -> Path | None:
    progress = getattr(run, "progress", None)
    if not isinstance(progress, dict):
        return None
    execution = progress.get("repo_execution")
    if not isinstance(execution, dict) or not execution.get("isolated"):
        return None
    raw_workdir = str(execution.get("workdir") or "").strip()
    if not raw_workdir:
        return None
    workdir = Path(raw_workdir).expanduser()
    try:
        resolved = workdir.resolve()
    except OSError:
        return None
    if not resolved.is_dir():
        return None
    parts = {part.lower() for part in resolved.parts}
    if "wt" not in parts and resolved.parent.name.lower() != "hermes-agent-wt":
        return None
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=resolved,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    if branch.returncode != 0 or (branch.stdout or "").strip() != "HEAD":
        return None
    return resolved


def _isolate_command_proof_workdir_if_git(workdir: Path, *, task_id: str, run_id: str, actor: str | None) -> Path:
    git_root = _git_root_for_command_workdir(workdir)
    if git_root is None:
        return workdir
    repo_ctx = RepoExecutionContext(workdir=git_root, repo_label=git_root.name, source=f"{actor or 'proof'}-proof")
    return isolated_repo_context_for_run(repo_ctx, task_id=task_id, run_id=f"{run_id}_proof").workdir


def _git_root_for_command_workdir(workdir: Path) -> Path | None:
    try:
        start = workdir.resolve()
    except OSError:
        return None
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _command_proof_repo_scope(task: Task, *, actor: str | None, stage_id: str | None) -> str | None:
    """Return the repo that must own a command proof for the current stage.

    Dev sessions can be correctly repo-scoped while command proof collection
    still sees the task's broader cross-stack repo list. For proof integrity, a
    stage-specific repo hint is authoritative over the first affected_repo.
    """

    intent = _command_proof_repo_intent(task, actor=actor, stage_id=stage_id)
    affected_repos = [str(repo).strip() for repo in (getattr(task, "affected_repos", []) or []) if str(repo).strip()]
    stage_repo = _command_proof_stage_repo(task, stage_id=stage_id)
    has_legacy_stage = _has_legacy_command_stage(task, stage_id=stage_id)
    if intent is not None and (stage_repo is None or has_legacy_stage):
        for repo in affected_repos:
            if _repo_text_matches_intent(repo, intent):
                return repo
    if stage_repo and not has_legacy_stage:
        return stage_repo
    if stage_repo and (intent is None or _repo_text_matches_intent(stage_repo, intent)):
        return stage_repo
    if intent is None:
        return None
    if affected_repos:
        return None
    return {
        "backend": "EterniaBackend",
        "launcher": "EterniaLauncher",
        "harness": "hermes-agent",
    }[intent]


def _command_proof_stage_repo(task: Task, *, stage_id: str | None) -> str | None:
    stage = _stage_for_command_proof(task, stage_id)
    repo = str(getattr(stage, "repo", "") or "").strip() if stage is not None else ""
    if repo in {"EterniaBackend", "EterniaLauncher", "hermes-agent"}:
        if _is_typed_plan_stage(task, stage_id=stage_id):
            return repo
        return default_blueprint_placeholder_repo_override(task, repo) or repo
    return None


def _is_typed_plan_stage(task: Task, *, stage_id: str | None) -> bool:
    target = str(stage_id or getattr(task, "current_stage_id", "") or "").strip()
    if not target:
        return False
    plan = getattr(task, "mission_plan", None)
    return any(str(getattr(stage, "id", "") or "") == target for stage in (getattr(plan, "stages", None) or []))


def _has_legacy_command_stage(task: Task, *, stage_id: str | None) -> bool:
    target = str(stage_id or getattr(task, "current_stage_id", "") or "").strip()
    if not target:
        return False
    return any(getattr(stage, "id", None) == target for stage in (getattr(task, "stages", None) or []))


def _command_proof_repo_intent(task: Task, *, actor: str | None, stage_id: str | None) -> str | None:
    stage = _stage_for_command_proof(task, stage_id)
    stage_scope_text = ""
    stage_objective_text = ""
    stage_command_text = ""
    if stage is not None:
        stage_scope_text = " ".join(
            [
                str(stage.id),
                str(stage.title),
                " ".join(str(item) for item in (stage.affected_paths or [])),
            ]
        ).lower()
        stage_objective_text = str(stage.objective or "").lower()
        stage_command_text = " ".join(
            [
                " ".join(str(item) for item in (stage.test_plan or [])),
            ]
        ).lower()
    if _text_mentions_launcher(stage_scope_text):
        return "launcher"
    if _text_mentions_backend(stage_scope_text):
        return "backend"
    if _text_mentions_harness(stage_scope_text):
        return "harness"
    if _text_mentions_launcher(stage_command_text):
        return "launcher"
    if _text_mentions_backend(stage_command_text):
        return "backend"
    if _text_mentions_harness(stage_command_text):
        return "harness"
    if _text_mentions_backend(stage_objective_text):
        return "backend"
    if _text_mentions_launcher(stage_objective_text):
        return "launcher"
    if _text_mentions_harness(stage_objective_text):
        return "harness"
    actor_id = str(actor or "").strip()
    if actor_id == "backend_dev":
        return "backend"
    return None


def _stage_for_command_proof(task: Task, stage_id: str | None):
    target = str(stage_id or getattr(task, "current_stage_id", "") or "").strip()
    if not target:
        return None
    stages = list(_runtime_stage_records(task))
    legacy_stages = list(getattr(task, "stages", None) or [])
    seen = {id(stage) for stage in stages}
    stages.extend(stage for stage in legacy_stages if id(stage) not in seen)
    for stage in stages:
        if stage.id == target:
            return stage
    return None


def _repo_text_matches_intent(repo: str, intent: str) -> bool:
    raw = str(repo or "").strip()
    if ":" in raw or "/" in raw or "\\" in raw:
        haystack = Path(raw).name.lower().replace("_", "-")
    else:
        name = Path(raw).name.lower().replace("_", "-")
        text = raw.lower().replace("_", "-")
        haystack = f"{text} {name}"
    if intent == "launcher":
        return _text_mentions_launcher(haystack)
    if intent == "backend":
        return _text_mentions_backend(haystack)
    if intent == "harness":
        return _text_mentions_harness(haystack)
    return False


def _text_mentions_launcher(text: str) -> bool:
    normalized = str(text or "").lower().replace("_", "-")
    return any(
        marker in normalized
        for marker in (
            "eternialauncher",
            "eternia-launcher",
            "launcher",
            "frontend",
            "front-end",
            "flutter ",
            "flutter-test",
            "flutter-analyze",
            "dart-test",
        )
    )


def _text_mentions_backend(text: str) -> bool:
    normalized = str(text or "").lower().replace("_", "-")
    return any(marker in normalized for marker in ("eterniabackend", "eternia-backend", "backend"))


def _text_mentions_harness(text: str) -> bool:
    normalized = str(text or "").lower().replace("_", "-")
    return any(marker in normalized for marker in ("hermes-agent", "agent-runtime-harness", "harness"))
