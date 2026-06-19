from __future__ import annotations

import re
from typing import Any


KNOWN_RISK_FLAGS = frozenset(
    {
        "backend_contract_first",
        "backend_contract_packet_missing_repair",
        "bounded_complex_burn_in",
        "bounded_test_fix_pass_used",
        "command_proof_repo_mismatch",
        "command_proof_stage_mismatch",
        "cross_stack_backend_proof_missing_before_launcher_release",
        "cross_stack_contract_handoff",
        "cross_stack_contract_join",
        "cross_stack_launcher_release_missing",
        "cross_stack_qa_coordination_release_missing",
        "cross_stack_routing",
        "cross_stack_sequential_handoff",
        "cross_stack_sequential_join_required",
        "forked_from_issue_discovery",
        "frontend_backend_contract_handoff",
        "keep_me",
        "launcher_contract_released_by_neko",
        "launcher_contract_second",
        "launcher_visual_proof_required_after_join_gate",
        "neko_block_recovery_attempted",
        "neko_qa_coordination_released",
        "neko_scoped_dev_handoff_stage",
        "no_product_edits",
        "no_progress_escalated_to_neko",
        "persona_operation",
        "post_scope_wait_coerced_to_handoff",
        "proof_ids_required_before_qa",
        "qa_blocked_verdict_needs_dev_recovery",
        "real_token_smoke",
        "requires_visual_proof",
        "routing_burn_in_only",
        "sequential_specialist_handoff",
        "sequential_specialists_required",
        "stagec_mcp",
        "worker_session_receipts_required",
    }
)

PARAMETERIZED_RISK_FLAG_PREFIXES = frozenset(
    {
        "diagnostic_persona",
        "final_gap_report",
        "max_child_depth",
        "persona_assignment_id",
        "priority",
        "severity",
    }
)


def normalize_task_risk_flags(raw_flags: Any, raw_notes: Any = None) -> tuple[list[str], list[str]]:
    flags: list[str] = []
    notes: list[str] = [str(item).strip() for item in (raw_notes or []) if str(item).strip()] if isinstance(raw_notes, list) else []
    seen_flags: set[str] = set()
    seen_notes = set(notes)
    values = raw_flags if isinstance(raw_flags, list) else []
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        if is_known_risk_flag(text):
            if text not in seen_flags:
                flags.append(text)
                seen_flags.add(text)
            continue
        note = f"migrated legacy risk_flag: {text}"[:500]
        if note not in seen_notes:
            notes.append(note)
            seen_notes.add(note)
    return flags, notes


def is_known_risk_flag(value: str) -> bool:
    text = str(value or "").strip()
    if text in KNOWN_RISK_FLAGS:
        return True
    if ":" in text:
        prefix, suffix = text.split(":", 1)
        if prefix in PARAMETERIZED_RISK_FLAG_PREFIXES and _SAFE_PARAM_RE.fullmatch(suffix):
            return True
    return False


_SAFE_PARAM_RE = re.compile(r"[A-Za-z0-9_.-]{1,120}")
