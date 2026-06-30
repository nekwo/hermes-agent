"""Stage 62I replay scenario registry.

Every live contract failure is auto-captured as a replay scenario candidate: a
pointer-sized record holding the failing decision payload, the validation error,
and enough attribution to re-run the same payload against the *current* contracts
without touching any task state. This turns incidents into a growing regression
corpus instead of dead evidence ("playground, not cage").

Scenario records live under ``<runtime root>/replay_scenarios/`` with the same
local-evidence sensitivity as raw packet artifacts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType
from .serde import to_jsonable


SCENARIO_SCHEMA_VERSION = 1
MAX_ERROR_TEXT = 500
MAX_SCENARIOS = 500


def replay_scenarios_dir():
    path = paths.store_root() / "replay_scenarios"
    path.mkdir(parents=True, exist_ok=True)
    return path


_REFERENCE_PATTERN = re.compile(r"\b(?:proof|test|stage|bundle|packet)_[A-Za-z0-9_]{3,}\b")
# Structural field-name tokens that match the id pattern but are not entity ids.
_STRUCTURAL_TOKENS = frozenset({
    "proof_id", "proof_ids", "stage_id", "stage_ids", "bundle_id", "bundle_ids",
    "packet_id", "packet_ids", "test_plan", "test_run", "proof_gate", "proof_refs",
    "proof_reviewed", "proof_requirements", "stage_intent", "bundle_locks",
})


def estimate_context_relevance(task=None, payload: dict[str, Any] | None = None, error_message: str = "") -> dict[str, Any]:
    """Relevance, not size: did the projected context contain what the decision needed.

    Context size is a poor signal - a large context can omit the one entity the
    decision needed. This computes *grounding coverage*: of the load-bearing
    entity ids the decision referenced, how many were in the set the context
    builder actually projects (task.proof_ids, plan stage ids and their proof ids,
    repo bundle ids) versus only resolvable in the broader store (present but not
    projected = starvation) versus nowhere (invented).
    """
    raw_refs = _REFERENCE_PATTERN.findall(f"{error_message} {json.dumps(to_jsonable(payload or {}), default=str)[:4000]}")
    referenced = [ref for ref in dict.fromkeys(raw_refs) if ref not in _STRUCTURAL_TOKENS][:16]
    projected: set[str] = set()
    store_only: set[str] = set()
    if task is not None:
        projected.update(str(p) for p in (getattr(task, "proof_ids", None) or []))
        plan = getattr(task, "mission_plan", None)
        for stage in (getattr(plan, "stages", None) or []):
            sid = getattr(stage, "id", None)
            if sid:
                projected.add(str(sid))
            projected.update(str(p) for p in (getattr(stage, "proof_ids", None) or []))
    missing_but_in_store: list[str] = []
    grounded = 0
    for ref in referenced:
        if ref in projected:
            grounded += 1
            continue
        if ref.startswith(("proof_", "test_")):
            try:
                from .store import ProofStore

                ProofStore().get(ref)
                missing_but_in_store.append(ref)
                store_only.add(ref)
                continue
            except Exception:
                pass
    needed = len(referenced)
    grounding_coverage = 1.0 if needed == 0 else round(grounded / needed, 3)
    return {
        "referenced_count": needed,
        "grounded_in_projection": grounded,
        "grounding_coverage": grounding_coverage,
        "missing_but_in_store": missing_but_in_store[:6],
    }


def classify_failure_origin(
    *,
    task=None,
    run=None,
    payload: dict[str, Any] | None = None,
    error_message: str = "",
) -> dict[str, Any]:
    """Distinguish whose fault an invalid decision actually was, by relevance not size.

    - ``contract_violation``: the load-bearing entities were projected (or were
      invented and resolve nowhere) - the persona had what it needed and still
      produced an invalid shape.
    - ``context_starvation``: a referenced entity resolves in harness stores but
      was NOT in the projected context, or the task has unfulfilled context
      requests. Size-independent: a huge context that omits the needed entity is
      starvation, not overload. The harness owes the projection.
    - ``context_overload``: grounding was complete (everything needed was
      projected) yet the run still failed on a large context - relevant data was
      present but diluted. Fix compression/projection ordering, not the persona.
    """
    relevance = estimate_context_relevance(task=task, payload=payload, error_message=error_message)
    evidence: list[str] = []
    if relevance["missing_but_in_store"]:
        evidence.append(f"referenced_id_in_store_not_projected:{relevance['missing_but_in_store'][0][:64]}")
    if task is not None:
        unfulfilled = [
            req
            for req in (getattr(task, "context_requests", []) or [])
            if isinstance(req, dict) and req.get("status") in {"unsupported", "open", "fulfilled_partial"}
        ]
        if unfulfilled:
            evidence.append(f"unfulfilled_context_requests:{len(unfulfilled)}")
    if evidence:
        return {"failure_origin": "context_starvation", "origin_evidence": evidence, "context_relevance": relevance}
    # Overload is only credible when grounding was complete: the persona HAD every
    # referenced entity in context and still failed on a large prompt (dilution).
    llm = getattr(run, "llm", None) if run is not None else None
    input_tokens = int((llm or {}).get("input_tokens") or 0) if isinstance(llm, dict) else 0
    progress = getattr(run, "progress", None) if run is not None else None
    context_size = int((progress or {}).get("context_size_estimate") or 0) if isinstance(progress, dict) else 0
    fully_grounded = relevance["referenced_count"] > 0 and relevance["grounding_coverage"] >= 1.0
    large_context = input_tokens > 60_000 or context_size > 50_000
    if large_context and (fully_grounded or relevance["referenced_count"] == 0):
        return {
            "failure_origin": "context_overload",
            "origin_evidence": [f"input_tokens:{input_tokens}", f"context_size_estimate:{context_size}", f"grounding_coverage:{relevance['grounding_coverage']}"],
            "context_relevance": relevance,
        }
    return {"failure_origin": "contract_violation", "origin_evidence": [], "context_relevance": relevance}


def record_scenario_candidate(
    *,
    task_id: str,
    persona_id: str,
    decision_type: str | None,
    payload: dict[str, Any] | None,
    error_class: str,
    error_message: str,
    source: str = "live_contract_failure",
    run_id: str | None = None,
    failure_origin: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Best-effort capture of a failed decision as a replay scenario candidate.

    Returns the stored record, or ``None`` when the failure carries no payload
    (nothing to replay) or an identical scenario already exists (dedupe).
    """
    if not isinstance(payload, dict) or not payload:
        return None
    body = to_jsonable(payload)
    digest = hashlib.sha256(
        json.dumps({"t": decision_type, "p": body, "e": _safe_error(error_message)}, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    scenario_id = f"scen_{digest}"
    path = replay_scenarios_dir() / f"{scenario_id}.json"
    if path.exists():
        return None
    if len(list(replay_scenarios_dir().glob("scen_*.json"))) >= MAX_SCENARIOS:
        return None
    record = {
        "schema_version": SCENARIO_SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "status": "candidate",
        "source": source,
        "task_id": str(task_id or ""),
        "run_id": str(run_id or "") or None,
        "persona_id": str(persona_id or ""),
        "decision_type": str(decision_type or "") or None,
        "error_class": str(error_class or "")[:120],
        "error_message": _safe_error(error_message),
        "failure_origin": (failure_origin or {}).get("failure_origin", "unknown"),
        "origin_evidence": [(str(item) or "")[:120] for item in ((failure_origin or {}).get("origin_evidence") or [])[:6]],
        "context_relevance": (failure_origin or {}).get("context_relevance", {}),
        "payload": body,
        "captured_at": now(),
    }
    atomic_json_write(path, to_jsonable(record))
    return record


def list_scenarios() -> list[dict[str, Any]]:
    records = []
    for path in sorted(replay_scenarios_dir().glob("scen_*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return records


def get_scenario(scenario_id: str) -> dict[str, Any] | None:
    path = replay_scenarios_dir() / f"{_safe_id(scenario_id)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def replay_scenario(scenario_id: str) -> dict[str, Any]:
    """Re-run a captured payload against the current contracts.

    Pure validation: no task, run, or store state is read or mutated. The
    verdict distinguishes a payload that now intakes cleanly (the contract or
    normalizer was fixed) from one that still fails (regression still open).
    """
    record = get_scenario(scenario_id)
    if record is None:
        return {"ok": False, "scenario_id": scenario_id, "error": "scenario_not_found"}
    decision_type = record.get("decision_type")
    payload = copy.deepcopy(record.get("payload"))
    if not isinstance(payload, dict) or not decision_type:
        return {"ok": False, "scenario_id": scenario_id, "error": "scenario_not_replayable"}
    try:
        dtype = DecisionType(decision_type)
    except ValueError:
        return {"ok": False, "scenario_id": scenario_id, "error": f"unknown_decision_type:{decision_type}"}
    decision = AgentDecision(type=dtype, summary="replay", rationale="replay", payload=payload)
    from .decision_contract_registry import validate_payload_keys
    from .packets import validate_decision_packets

    try:
        validate_payload_keys(decision)
        validate_decision_packets(decision)
        _replay_mission_plan_validation(record, payload)
    except DecisionPayloadInvalid as exc:
        return {
            "ok": True,
            "scenario_id": scenario_id,
            "verdict": "still_failing",
            "original_error": record.get("error_message"),
            "current_error": _safe_error(str(exc)),
            "error_changed": _safe_error(str(exc)) != record.get("error_message"),
        }
    normalization = _normalization_summary(payload, record.get("payload"))
    return {
        "ok": True,
        "scenario_id": scenario_id,
        "verdict": "passes_current_contract",
        "original_error": record.get("error_message"),
        **normalization,
    }


def replay_all() -> dict[str, Any]:
    results = [replay_scenario(record.get("scenario_id", "")) for record in list_scenarios()]
    passing = [r["scenario_id"] for r in results if r.get("verdict") == "passes_current_contract"]
    failing = [r["scenario_id"] for r in results if r.get("verdict") == "still_failing"]
    broken = [r["scenario_id"] for r in results if not r.get("ok")]
    return {
        "total": len(results),
        "passes_current_contract": passing,
        "still_failing": failing,
        "not_replayable": broken,
        "results": results,
    }


def _replay_mission_plan_validation(record: dict[str, Any], payload: dict[str, Any]) -> None:
    """Run the mission_plan shape validators the live apply path would run.

    Uses a throwaway synthetic task, so task-derived enrichment (e.g. launcher
    auto-stages keyed off the real title/description) may differ from the live
    run; the duplicate-id/dependency/shape validations are faithful.
    """
    if not isinstance(payload.get("mission_plan"), dict) and not isinstance(payload.get("mission_plan_patch"), dict):
        return
    from .mission_plan import ensure_mission_plan
    from .models import Task
    from .states import TaskState

    ts = now()
    task = Task(
        id=str(record.get("task_id") or "task_replay"),
        title="replay scenario",
        description="replay scenario validation",
        state=TaskState.CREATED,
        created_at=ts,
        updated_at=ts,
        requested_by="replay",
    )
    ensure_mission_plan(task, payload)


def _normalization_summary(after: dict[str, Any], before: Any) -> dict[str, Any]:
    if not isinstance(before, dict):
        return {}
    dropped = sorted(set(before.keys()) - set(after.keys()))
    summary: dict[str, Any] = {}
    if dropped:
        summary["normalized_dropped_keys"] = dropped
    return summary


def _safe_error(text: Any) -> str:
    return " ".join(str(text or "").split())[:MAX_ERROR_TEXT]


def _safe_id(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isalnum() or ch == "_")[:64]
