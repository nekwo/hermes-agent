"""S43 removes 30 individually-dead symbols from modules that stay LIVE.

Nothing here is a module cut. Each module below keeps real callers; what goes is
one name (or one exclusive chain) inside it whose last reader left with the
mission lane, the stage graph, the proof store, or the packet emitter. Every
entry was re-verified with a whole-repo word search immediately before the cut,
not inherited from an earlier wave's list.

The three patterns worth naming, because each one hides from ``flutter
analyze``-style checks and from a casual grep:

* **Constant-by-construction bodies.** ``stage_intent``'s
  ``first_incomplete_product_edit_stage`` and
  ``extract_single_known_stage_reference`` iterate ``for stage in []`` — a
  literal empty list left behind when the stage graph went. They cannot return
  anything but ``None``. A caller would not fail; it would silently get the
  answer "no".
* **Exclusive private chains.** ``stage_is_committed_verification_gate`` was the
  only caller of ``_stage_has_command_gate``, which was the only caller of
  ``_looks_like_command``; ``packet_raw_artifact_path`` was the only caller of
  ``packet_artifacts_task_dir``. Cutting the head alone would leave the tail
  looking live to the next reachability pass.
* **Self-recursive orphans.** ``packets._truncate_free_fields`` references
  itself three times, so a naive reference count reports it as used.

Deliberate KEEPS on the same lines, each pinned by the negative gate below:

* ``paths.packet_artifacts_dir`` — LIVE via ``checkpoint``'s ``packet_artifacts``
  EntityClass, while its ``_task_dir`` / ``_raw_artifact_path`` leaves are dead.
* ``redaction.BasicRedactionScanner`` — the ``RedactionScanner`` Protocol it
  structurally satisfies is dead; the class itself is test-anchored and stays.
* ``skill_publishability.REASON_*`` — only the ``PUBLISHABLE_REASONS`` frozenset
  over them is dead.
* ``cli_format.emit_json`` — the module's whole live surface.
* ``repo_context``'s worktree trio (``isolated_repo_context_for_run`` /
  ``_worktree_token`` / ``_ensure_isolated_worktree``) — deliberate
  regression-test infrastructure (Wave 3 ruling); only the three unused ``_DIFF_*``
  regexes go.
* ``role_envelopes``'s store family — untouched; its event-count ruling is still
  pending, so ONLY ``role_envelope_summary`` is cut here.

No event contract moves: ``event_catalog()`` stays at 88.
"""

from __future__ import annotations

import importlib

import pytest


#: module -> the names that must no longer exist on it.
REMOVED = {
    "agent_runtime.board_models": ("is_default_board_id",),
    "agent_runtime.budget_approval": ("eligible_budget_approval_incidents",),
    "agent_runtime.cli_format": ("task_summary", "human_task_line"),
    "agent_runtime.decision_contract_registry": ("FieldContract",),
    "agent_runtime.decision_schema": ("_extract_first_json_blob",),
    "agent_runtime.event_rotation": ("rotate_if_needed",),
    "agent_runtime.locks": ("repo_land_lock",),
    "agent_runtime.packets": ("_truncate_free_fields",),
    "agent_runtime.paths": (
        "stagec_artifacts_task_dir",
        "packet_artifacts_task_dir",
        "packet_raw_artifact_path",
    ),
    "agent_runtime.profile_artifact_sync": ("MEMBER_STATE_KINDS", "_LEGACY_SEGMENTS"),
    "agent_runtime.realm_sync": ("SYNC_STATES",),
    "agent_runtime.redaction": ("RedactionScanner",),
    "agent_runtime.repo_bundles": ("find_best_bundle_for_action",),
    "agent_runtime.repo_context": (
        "_DIFF_TEST_FILE_RE",
        "_DIFF_REMOVED_ASSERT_RE",
        "_DIFF_ADDED_SKIP_RE",
    ),
    "agent_runtime.role_envelopes": ("role_envelope_summary",),
    "agent_runtime.runtime_instances": ("BACKGROUND_LANE",),
    "agent_runtime.self_test_evidence": ("_relative_runtime_path",),
    "agent_runtime.skill_install": ("harness_skill_installed_ok",),
    "agent_runtime.skill_promotion": ("RESULT_ACTIONS",),
    "agent_runtime.skill_publishability": ("PUBLISHABLE_REASONS",),
    "agent_runtime.stage_intent": (
        "first_incomplete_product_edit_stage",
        "stage_is_committed_verification_gate",
        "extract_single_known_stage_reference",
        "_stage_has_command_gate",
        "_looks_like_command",
    ),
    "agent_runtime.tool_visibility": ("_profile_readiness_cache_clear",),
}

#: module -> names on the SAME module (often the same line) that must survive.
KEPT = {
    "agent_runtime.board_models": ("default_board_id",),
    "agent_runtime.budget_approval": (
        "budget_incident_can_continue",
        "budget_incident_needs_scope_recovery",
        "_safe_int",
    ),
    "agent_runtime.cli_format": ("emit_json",),
    "agent_runtime.paths": ("packet_artifacts_dir", "stagec_artifacts_dir"),
    "agent_runtime.redaction": ("BasicRedactionScanner", "RedactionStatus"),
    "agent_runtime.repo_context": (
        "isolated_repo_context_for_run",
        "_worktree_token",
        "_ensure_isolated_worktree",
    ),
    "agent_runtime.role_envelopes": ("RoleEnvelopeStore",),
    "agent_runtime.skill_publishability": (
        "REASON_SHARED_ROOT",
        "REASON_PROFILE_LOCAL_ONLY",
        "REASON_EXTERNAL_DIR_ONLY",
        "REASON_UNKNOWN_ROOT",
    ),
    "agent_runtime.stage_intent": (
        "stage_requires_product_edit",
        "no_product_edit_recipe_id",
        "no_product_edit_recipe_for_stage",
        "no_product_edit_recipe_conflicts_with_stage",
    ),
    "agent_runtime.profile_artifact_sync": (
        "KIND_PROFILE_MEMORY",
        "KIND_CORE_CONTEXT",
        "KIND_PERSONA_PROMPT",
    ),
}


@pytest.mark.parametrize("dotted", sorted(REMOVED))
def test_the_dead_symbols_are_gone(dotted: str):
    module = importlib.import_module(dotted)
    assert [name for name in REMOVED[dotted] if hasattr(module, name)] == []


@pytest.mark.parametrize("dotted", sorted(KEPT))
def test_the_live_neighbours_survive(dotted: str):
    module = importlib.import_module(dotted)
    assert [name for name in KEPT[dotted] if not hasattr(module, name)] == []


def test_the_packet_artifacts_class_still_resolves_its_directory():
    """``packet_artifacts_dir`` is the KEEP in the paths trio: ``checkpoint``
    registers it as an EntityClass, so cutting it by name-similarity would break
    a live checkpoint class."""

    from agent_runtime import checkpoint

    assert "packet_artifacts" in checkpoint.ENTITY_CLASS_NAMES


def test_the_basic_redaction_scanner_still_scans(tmp_path):
    """The Protocol went; the concrete scanner is live (it takes a PATH)."""

    from agent_runtime.redaction import BasicRedactionScanner, RedactionStatus

    path = tmp_path / "artifact.txt"
    path.write_text("API_KEY='abcdefghijklmnop'", encoding="utf-8")
    assert BasicRedactionScanner().scan_text(path) == RedactionStatus.UNSAFE


def test_every_touched_module_still_imports():
    for dotted in sorted(set(REMOVED) | set(KEPT)):
        importlib.import_module(dotted)
