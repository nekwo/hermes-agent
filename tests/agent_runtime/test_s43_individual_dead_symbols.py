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

* ``paths.packet_artifacts_dir`` was live at S43 through checkpoint discovery;
  S64 retired that final reader and inverted the old keep pin.
* ``redaction.BasicRedactionScanner`` — retired with its test-only status enum;
  the shared runtime redaction patterns remain the production authority.
* ``skill_publishability.REASON_*`` — only the ``PUBLISHABLE_REASONS`` frozenset
  over them is dead.
* ``cli_format.emit_json`` — the module's whole live surface.
* ``repo_context``'s worktree trio (``isolated_repo_context_for_run`` /
  ``_worktree_token`` / ``_ensure_isolated_worktree``) — deliberate
  regression-test infrastructure (Wave 3 ruling); only the three unused ``_DIFF_*``
  regexes go.
* ``role_envelopes``'s store family — untouched; its event-count ruling is still
  pending, so ONLY ``role_envelope_summary`` is cut here.
  **SUPERSEDED 2026-07-31 (S44):** the ruling came back CUT. ``role_envelopes``
  was deleted whole, taking the store family and six event contracts with it.

No event contract moves *at S43*: ``event_catalog()`` stayed at 88 through this
wave. S44 then moved it to 82. The absolute count has one owner either way —
``tests/agent_runtime/test_s15_event_contract_pruning.SURVIVING_EVENT_COUNT``.

RETARGETED 2026-07-31 (S44/S45): three modules this file pinned symbols on no
longer exist. ``role_envelopes`` (S44) and ``budget_approval`` / ``stage_intent``
(S45, test-only whole modules) were deleted, so their ``REMOVED`` / ``KEPT``
entries moved to the ``RETIRED_WHOLE_MODULES`` table — which the 2026-08-01
migration below preserves verbatim as prose, its assertion having become a
registry ``Form.MODULE`` row. They are NOT dropped: an
individual-symbol witness that silently loses its module hides the bigger cut
that swallowed it, and the KEPT half is the more interesting record — those names
were correctly alive at S43 and died for an unrelated reason two waves later.

RETARGETED AGAIN 2026-08-01 (S57): ``repo_bundles`` joins that table. S43 cut one
symbol from it (``find_best_bundle_for_action``) and left the store alone; S52
took its write lane, S56 the four status projections that were its last
customers, and S57 the module. Four waves, four different reasons, one module.

=============================================================================
MIGRATED to ``tests/agent_runtime/test_tombstone_registry.py`` (2026-08-01)
=============================================================================

The ``REMOVED`` table and the ``RETIRED_WHOLE_MODULES`` absence assertion moved.
Both were pure absence:

* every one of the 24 names across the 18 live modules is now a ``Form.ATTR``
  row scoped to its own module (``agent_runtime.board_models``,
  ``.decision_contract_registry``, ``.decision_schema``, ``.event_rotation``,
  ``.locks``, ``.packets``, ``.paths``, ``.profile_artifact_sync``,
  ``.realm_sync``, ``.redaction``, ``.repo_context``, ``.runtime_instances``,
  ``.self_test_evidence``, ``.skill_install``, ``.skill_promotion``,
  ``.skill_publishability``, ``.tool_visibility``, and ``.cli_format``'s
  ``human_task_line`` / ``task_summary``, which the registry carries under s41
  because that is the wave that unbound them).
* the four whole-module absences are ``Form.MODULE`` rows —
  ``agent_runtime.role_envelopes`` (s44), ``agent_runtime.repo_bundles`` (s57),
  ``agent_runtime.budget_approval`` and ``agent_runtime.stage_intent`` (s45).
  ``find_spec is None`` there is the same assertion this file made, so the
  parametrized ``test_the_later_whole_module_cuts_took_both_halves`` went with
  its table.

THE RECORD THAT TABLE CARRIED IS PRESERVED HERE, because it is the interesting
half and a registry row cannot hold it. At S43 each KEPT name below had a
verified caller and each REMOVED name did not; a later wave then took the whole
module for a reason that had nothing to do with these individual symbols:

===================================== ======================================== ==========================================================
module (later cut)                    removed at S43                           deliberately KEPT at S43, then outlived
===================================== ======================================== ==========================================================
``role_envelopes`` (S44)              ``role_envelope_summary``                ``RoleEnvelopeStore`` (kept pending the operator ruling)
``repo_bundles`` (S57)                ``find_best_bundle_for_action``          — (nothing; the store was left whole)
``budget_approval`` (S45)             ``eligible_budget_approval_incidents``   ``budget_incident_can_continue``,
                                                                               ``budget_incident_needs_scope_recovery``, ``_safe_int``
``stage_intent`` (S45)                ``first_incomplete_product_edit_stage``, ``stage_requires_product_edit``,
                                      ``stage_is_committed_verification_gate``,``no_product_edit_recipe_id``,
                                      ``extract_single_known_stage_reference``,``no_product_edit_recipe_for_stage``,
                                      ``_stage_has_command_gate``,             ``no_product_edit_recipe_conflicts_with_stage``
                                      ``_looks_like_command``
===================================== ======================================== ==========================================================

WHAT STAYED, AND WHY NONE OF IT IS A ROW: the ``KEPT`` table and its
parametrized pin (live neighbours on the same line, often the same import, as a
cut name), the ``packet_artifacts`` EntityClass resolution (a fact about
``checkpoint.ENTITY_CLASS_NAMES``, not about a name's absence), the
and the still-imports negative gate.
"""

from __future__ import annotations

import importlib

import pytest


#: module -> names on the SAME module (often the same line) that must survive.
KEPT = {
    "agent_runtime.board_models": ("default_board_id",),
    "agent_runtime.cli_format": ("emit_json",),
    "agent_runtime.repo_context": (
        "isolated_repo_context_for_run",
        "_worktree_token",
        "_ensure_isolated_worktree",
    ),
    "agent_runtime.skill_publishability": (
        "REASON_SHARED_ROOT",
        "REASON_PROFILE_LOCAL_ONLY",
        "REASON_EXTERNAL_DIR_ONLY",
        "REASON_UNKNOWN_ROOT",
    ),
    "agent_runtime.profile_artifact_sync": (
        "KIND_PROFILE_MEMORY",
        "KIND_CORE_CONTEXT",
        "KIND_PERSONA_PROMPT",
    ),
}


@pytest.mark.parametrize("dotted", sorted(KEPT))
def test_the_live_neighbours_survive(dotted: str):
    module = importlib.import_module(dotted)
    assert [name for name in KEPT[dotted] if not hasattr(module, name)] == []


def test_every_touched_module_still_imports():
    for dotted in sorted(KEPT):
        importlib.import_module(dotted)
