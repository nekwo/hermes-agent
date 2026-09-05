"""S15 de-registers the event contracts that no surviving code can emit.

The mission lane took its producers with it (planning, the ticker, the ``Task``
half of ``store.py``, goal hygiene, steering/supervision, liveness, burn-in,
mission goals, the proof runner, and parts of ``child_events``), and S13/S14 took
the last few (``preflight`` → ``task.preflight``, ``missing_input`` →
``missing_input.requested``, ``worklog`` → ``persona.worklog``).

An ``EventContract`` with no producer is not harmless: it is a shape the
manifest publishes and ``EventLog.append`` accepts —
so a test (or an agent) can mint an event no reader will ever see in production.

``ALLOWED_EVENT_TYPES`` is checked **on append only**, so historical log rows
carrying a de-registered type still read back fine. What does change is
``contract_hash()`` and the event-only snapshot/status contract fingerprint.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_runtime.decision_contract_registry import (
    contract_hash,
    contract_manifest,
    event_catalog,
)
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
        # S52 (2026-08-01). The last seven repo_bundle.* types, de-registered
        # with the RepoBundleStore WRITE lane that was their only emitter.
        # ``repo_bundle.delivered`` reached this set at S25 for the same reason,
        # one writer earlier.
        "repo_bundle.created",
        "repo_bundle.updated",
        "repo_bundle.assigned",
        "repo_bundle.running",
        "repo_bundle.verified",
        "repo_bundle.rejected",
        "repo_bundle.woke",
        # S53 (2026-08-01). The lane family, de-registered with the
        # GoalRuntimeInstanceStore WRITE lane that was their only emitter.
        # ``foreground_runtime.closed`` is the LAST of the foreground_runtime.*
        # family: S15 above already took the other six when the mission lane
        # went, and this one outlived them only because
        # ``mark_terminal_for_task`` was still standing.
        "lane.created",
        "lane.transitioned",
        "lane.transition_rejected",
        "foreground_runtime.closed",
        # Round 4 contract ruling: all three writers were caller-less after the
        # mission/task lane removal. Historical rows remain readable/renderable.
        "run.closed",
        "self_test.recorded",
        "self_test.loop_detected",
        # Final dead-code closeout: their only writers were retired with the
        # goal-instance sweep and IncidentStore mutation surface.
        "persona_instance.reaped",
        "persona_instance.attributed",
        "incident.opened",
        "incident.closed",
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
# Then -7 at S52: the remaining repo_bundle.* family, de-registered with the
# RepoBundleStore write lane (their only emitter). Two of the seven were also
# operator-summary types, so events.OPERATOR_SUMMARY_EVENT_TYPES and their
# formatter arm went with them; owned by
# tests/agent_runtime/test_s52_repo_bundle_write_lane_removal.py.
# Then -4 at S53: lane.created / .transitioned / .transition_rejected and
# foreground_runtime.closed, de-registered with the GoalRuntimeInstanceStore
# write lane (their only emitter); owned by
# tests/agent_runtime/test_s53_lane_write_lane_removal.py.
# Then -10 at S56: the whole worker_session.* family (opened / assigned /
# resumed / heartbeat / context_absorbed / steered / possessed / released /
# watchdog_warning / closed), de-registered in the same commit that deleted
# agent_runtime/worker_sessions.py whole -- their only emitter; owned by
# tests/agent_runtime/test_s56_worker_session_lane_removal.py.
# Then -4 in the final closeout: persona_instance.reaped/.attributed and
# incident.opened/.closed lost their last writers. This stays an absolute count
# on purpose: it is the one assertion that catches
# a contract silently appearing or disappearing.
# Then +4 at WP-H2 (2026-08-03): dispatch.recorded / .completed / .delivered /
# .dropped, registered in the SAME commit as their emitter
# (``agent_runtime.dispatch_store``) — the S55 rule running in the additive
# direction. This is the first ADDITION this counter has recorded, and it
# behaved exactly as designed: it went red on a legitimate new lane and had to
# be moved deliberately, rather than letting a contract slip in unnoticed.
#
# Then +1 at the WP-L2 review fixes (2026-08-03): dispatch.delivery_backlog.
# The dispatch store stopped pruning UNDELIVERED completions — that path was
# deleting answers nobody had ever received — which means the only remaining
# bound on them is that senders eventually drain. When they do not, the backlog
# has to be sayable rather than merely observable in a table nobody opens, so
# the exemption ships with an event that reports it. Registered in the same
# commit as its emitter, per S55.
#
# Then +1 again (final review wave, 2026-08-03): dispatch.outcome_superseded.
# The re-arm guard makes a second writer landing a different outcome on an
# already-delivered row harmless to the sender — which is precisely why it would
# otherwise be invisible. It is the observable symptom of the supervised-id
# registry being process-local, so it gets a name instead of being absorbed.
#
# Then +1 (R0, 2026-08-09): persona_chat.send_refused. A mission-chat send
# refused before the chat-root lease was acquired wrote NOTHING — every durable
# write the lane performs lives inside that lease — so the 2026-08-09 incident
# investigation could not find an operator message that had definitely been
# sent in any persistence surface. The refusal now leaves a fact about itself
# (root, client_message_id, error_kind, ts) with the text deliberately excluded.
# Registered in the same commit as its emitter, per S55; this counter went red
# on it and was moved deliberately, which is the counter working.
#
# Then +1 (instance replication H3, 2026-08-31): persona_instance.replicated. A
# realm pull that delivers a desk now mints the agent behind it, and that write
# is a THIRD intent class — neither an authored create nor a diagnostic repair,
# but the arrival of a peer's authored fact (plan §3.5). Reusing
# ``persona_instance.created`` was the alternative and was refused: that type
# means "this machine authored an agent" to every consumer that reads it, so one
# pull would read as N creates in the log an operator greps. Registered in the
# SAME commit as its emitter (``PersonaInstanceStore.replicate_instance`` /
# ``apply_replicated_steering``), per S55; this counter went red on it and was
# moved deliberately, which is again the counter working.
# S2c registered the five ``gateway.peer.*`` types (59 -> 64) in the SAME commit
# as their emitters, per S55: ``gateway_peers`` writes ``recorded`` from both
# ceremonies, ``revoked`` from the revoke door, ``updated`` from every cache
# writer and from the external-write memo, ``roster`` from the roster cache, and
# ``reachability`` from the dial result on a change of word. This counter went
# red on that commit and was moved deliberately — which is the counter working.
# C1h-bis registered ``persona_chat.turn_started`` / ``persona_chat.turn_ended``
# (64 -> 66) in the SAME commit as their emitter
# (``agent_runtime.chat_turn_presence.ChatTurnPresence``, called from the
# chat-turn core both the method lane and the CLI lane share), per S55. They
# exist because the stream hub publishes only when the event log moves and a
# chat turn running a model appended nothing of its own between its write-ahead
# record and its projection commit — so the ``running_work`` chat_turn row was in
# the projection and no subscriber was ever handed a frame carrying it. Two types
# rather than one with a ``change_kind``, on the ``dispatch.recorded`` /
# ``dispatch.completed`` precedent: the row appears on one and disappears on the
# other. This counter went red on that commit and was moved deliberately — which
# is, again, the counter working.
SURVIVING_EVENT_COUNT = 66


def test_the_unemittable_event_types_are_no_longer_registered():
    assert REMOVED_EVENT_TYPES & ALLOWED_EVENT_TYPES == frozenset()
    assert REMOVED_EVENT_TYPES & set(event_catalog()) == frozenset()


def test_the_registry_publishes_exactly_the_surviving_event_count():
    assert len(event_catalog()) == SURVIVING_EVENT_COUNT
    assert set(event_catalog()) == ALLOWED_EVENT_TYPES
    # S66 dropped the third assertion here. It read
    # ``verify_registry()["event_count"]`` — a test-only wrapper whose whole
    # importer set was this line, restating the count the line above already
    # asserts off the live catalog. Its removal loses no coverage; the manifest
    # (which DOES have a production reader, ``harness contracts show``) is
    # asserted below.
    assert contract_manifest()["events"] == event_catalog()
    assert contract_manifest()["contract_hash"] == contract_hash()


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
        # the chat/board/office/realm keep set.
        "persona_instance.created",
        "board.card.created",
        "office.actor.upserted",
        "realm.sync.published",
        "state.reconciled",
    }
    assert survivors <= ALLOWED_EVENT_TYPES


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
