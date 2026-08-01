"""S15 de-registers the event contracts that no surviving code can emit.

The mission lane took its producers with it (planning, the ticker, the ``Task``
half of ``store.py``, goal hygiene, steering/supervision, liveness, burn-in,
mission goals, the proof runner, and parts of ``child_events``), and S13/S14 took
the last few (``preflight`` → ``task.preflight``, ``missing_input`` →
``missing_input.requested``, ``worklog`` → ``persona.worklog``).

An ``EventContract`` with no producer is not harmless: it is a shape the prompt
contract advertises, the manifest publishes, and ``EventLog.append`` accepts —
so a test (or an agent) can mint an event no reader will ever see in production.

``ALLOWED_EVENT_TYPES`` is checked **on append only**, so historical log rows
carrying a de-registered type still read back fine. What does change is
``contract_hash()``: it is stamped as ``prompt_contract_hash`` on live persona
instances, which will read as contract-drifted after this stage. That is expected
and self-heals on the next turn.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_runtime.decision_contract_registry import (
    contract_manifest,
    event_catalog,
    verify_registry,
)
from agent_runtime.decision_schema import DecisionType
from agent_runtime.events import ALLOWED_EVENT_TYPES


REMOVED_EVENT_TYPES = frozenset(
    {
        "task.created",
        "task.transition",
        "task.cancelled",
        "task.blocked",
        "task.unblocked",
        "task.archived",
        "task.stage_added",
        "task.stage_updated",
        "task.stage_corrected",
        "task.pm_fleshed",
        "task.preflight",
        "foreground_runtime.prepared",
        "foreground_runtime.activated",
        "foreground_runtime.parked_task",
        "foreground_runtime.cancelled_stale_run",
        "foreground_runtime.waiting_on_fresh_run",
        "foreground_runtime.preempted_background_run",
        "plan.reviewed",
        "goal_create.field_dropped",
        "delivery.intent",
        "patch.proposed",
        "context.requested",
        "missing_input.requested",
        "cross_stack.backend_first_released",
        "cross_stack.backend_contract_packet_missing",
        "cross_stack.backend_proof_missing",
        "cross_stack.launcher_released",
        "cross_stack.launcher_release_missing",
        "cross_stack.qa_coordination_release_missing",
        "backend_release_gate_environment_failed",
        "issue.discovery_reported",
        "issue.discovery_triaged",
        "issue.child_mission_created",
        "run.liveness.warning",
        "liveness.poll",
        "child.progress",
        "child.blocked",
        "child.deploy_failed",
        "persona.worklog",
        "run.approval_required",
        "mission_budget_exceeded",
        "swarm_budget_exceeded",
        "run.model_call.started",
        "run.model_call.finished",
        "run.validation.started",
        "run.validation.failed",
        "role_session.opened",
        "role_session.continued",
        "role_session.watchdog_warning",
        "role_session.closed",
        "steer.requested",
        "steer.started",
        "steer.failed",
        "steer.cap_hit",
        "worker_session.compressed",
        "worker_session.possession_requested",
        "self_test.reused",
        "proof.attached",
        "proof.scanned",
        "proof.gate_checked",
        "handoff_request.deprecated_heuristic_agreement",
        "qa.verdict_recorded",
        "qa.coordination_released",
        "incident.resolved",
        "scope.override_recorded",
        "daemon.started",
        "daemon.stopped",
        # S44 (2026-07-31). These six were NOT unemittable at S15 — their
        # emitters were alive then. They joined this set when the
        # role_envelopes / role_checklists store family was deleted, which is
        # exactly why `role_envelope.paused` used to sit in the near-miss
        # survivor set below and has now been re-derived onto this side.
        "role_envelope.opened",
        "role_envelope.continued",
        "role_envelope.paused",
        "role_envelope.closed",
        "role_checklist.created",
        "role_checklist.item_updated",
        # S49 (2026-08-01). Same shape as the S44 six: live emitters at S15,
        # de-registered only once `agent_runtime/operator_control.py` — the sole
        # producer of all three — was deleted whole.
        "operator.takeover.requested",
        "operator.takeover.approval_required",
        "operator.takeover.applied",
    }
)

# 159 registered - 67 unemittable at S15, then -2 at S17 (run.heartbeat and
# run.approved went with their RunStore writers; see
# tests/agent_runtime/test_s17_run_store_residue_removal.py, which owns that
# delta), then +3 at S16b (realm.archived, persona_chat.deleted,
# worktree.orphans_reaped — live emitters whose appends were being refused and
# swallowed; see tests/agent_runtime/test_s16b_live_event_registration.py), then
# net -1 at S25: -1 run.opened (S17's third writer-less contract, held back only
# by two filler test appends until they were retargeted), -1 repo_bundle.delivered
# (S24 deleted RepoBundleStore.mark_delivered, its only emitter), +1
# flow_graph.pruned (the persona-instance reconciler's phase-5 graph reap). Those
# three deltas are owned by tests/agent_runtime/test_s25_run_opened_retirement.py,
# tests/agent_runtime/test_s25_repo_bundle_delivered_retirement.py, and
# tests/agent_runtime/test_s25_graph_prune_on_reap.py. Then -1 at S32:
# decision_contract.parity, whose sole emitter simplified_contract._record_parity
# was deleted at S27 (5c16417f6) — owned by
# tests/agent_runtime/test_s32_decision_contract_parity_retirement.py.
# Then -1 at S36: packet.recorded, whose writerless make/record API is retired;
# owned by tests/agent_runtime/test_s36_packet_emit_retirement.py.
# Then -2 at S37: packet.duplicate and packet.normalized, whose only literal
# writer was retired with the same S36 packet emit API; owned by
# tests/agent_runtime/test_s37_packet_contract_deregistration.py.
# Then -6 at S44: role_envelope.opened / .continued / .paused / .closed and
# role_checklist.created / .item_updated. Their only emitters were
# RoleEnvelopeStore.save and RoleChecklistStore.save, both deleted with the store
# family under the operator's 2026-07-31 ruling on deferred-debt item 1; owned by
# tests/agent_runtime/test_s44_role_envelope_family_removal.py.
# Then -3 at S49: operator.takeover.requested / .approval_required / .applied,
# whose sole emitter agent_runtime/operator_control.py was deleted whole under
# the operator's 2026-08-01 cut ruling; owned by
# tests/agent_runtime/test_s49_operator_control_removal.py.
# This stays an absolute count on purpose: it is the one assertion that catches
# a contract silently appearing or disappearing.
SURVIVING_EVENT_COUNT = 79


def test_the_unemittable_event_types_are_no_longer_registered():
    assert REMOVED_EVENT_TYPES & ALLOWED_EVENT_TYPES == frozenset()
    assert REMOVED_EVENT_TYPES & set(event_catalog()) == frozenset()


def test_the_registry_publishes_exactly_the_surviving_event_count():
    assert len(event_catalog()) == SURVIVING_EVENT_COUNT
    assert verify_registry()["event_count"] == SURVIVING_EVENT_COUNT
    assert set(event_catalog()) == ALLOWED_EVENT_TYPES


def test_appending_a_de_registered_event_type_is_refused():
    import pytest
    from hermes_time import now

    from agent_runtime.events import Event, EventLog

    with pytest.raises(ValueError):
        EventLog().append(
            Event(ts=now(), type="task.created", task_id="task_1", run_id=None, persona_id=None)
        )


def test_the_near_miss_survivors_stay_registered():
    """Types one careless grep away from the removal set — all still emittable."""

    survivors = {
        # child_events.py:32 is a live emit.
        "child.returned",
        # RE-DERIVED at S44: `role_envelope.paused` was pinned here because
        # `role_envelopes.py:150` emitted it on the else branch of a ternary —
        # the subtlest live emit in the registry, and the reason this survivor
        # set exists. That emitter is gone with the store family, so the type
        # moved to REMOVED_EVENT_TYPES above. Re-derived, not deleted: a
        # near-miss pin that silently loses its subject is how a survivor set
        # rots into decoration.
        # progress.py emits these with the label profile_runner hands it.
        "run.tool.started",
        "run.tool.finished",
        "run.progress",
        # RunStore.cancel -> close_run is LIVE, so run.closed stays emittable.
        # RE-DERIVED at S49: this note used to cite TWO callers (operator
        # takeover, persona-chat replacement). S49 deleted operator_control.py,
        # so only the persona-chat replacement in persona_assignments.py still
        # reaches it — one caller, still live, still emittable. run.heartbeat and
        # run.approved went with their writers at S17; run.opened followed at
        # S25 once its two filler minters were retargeted — all three are pinned
        # as removed by the tests that own those deltas.
        "run.closed",
        # self_test_evidence.py appends both directly.
        "self_test.recorded",
        "self_test.loop_detected",
        # the chat/board/office/realm keep set.
        "persona_instance.created",
        "board.card.created",
        "office.actor.upserted",
        "realm.sync.published",
        "state.reconciled",
    }
    assert survivors <= ALLOWED_EVENT_TYPES


def test_the_s11_role_filtering_shells_are_gone():
    from agent_runtime import decision_contract_registry as registry

    # A no-op that returned every decision for every role — zero callers.
    assert not hasattr(registry, "allowed_decisions_for_role")

    # An always-empty list reported as if it were a real check result.
    assert "role_shape_errors" not in verify_registry()

    # What it stood in for is still true and still pinned: no role filtering.
    assert {item.value for item in DecisionType} == set(contract_manifest()["decisions"])


def test_the_cross_repo_stream_fixtures_still_match_their_manifest():
    """The Launcher folds these exact bytes; pruning contracts must not move them."""

    import hashlib

    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "stream_frames"
    entries = dict(
        reversed(line.split("  ", 1))
        for line in (fixtures / "MANIFEST.sha256").read_text(encoding="utf-8").strip().splitlines()
    )
    name = "patch_coverage_manifest.json"
    assert hashlib.sha256((fixtures / name).read_bytes()).hexdigest() == entries[name]
    # The golden still names task.transition as a NOT-foldable chokepoint. That is
    # a classifier label, not an emission, so de-registering the type leaves the
    # cross-stack bytes untouched.
    manifest = json.loads((fixtures / name).read_text(encoding="utf-8"))
    assert any(case["chokepoint"] == "task.transition" for case in manifest["cases"])
