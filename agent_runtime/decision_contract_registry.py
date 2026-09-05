"""Canonical Mission Control event contracts.

The old AgentDecision/object/HUD registry shared this module until the runtime
stopped consuming typed model decisions.  Only the live append-time event
contract remains: historical de-registered rows are still readable because the
catalog gates writes, never reads.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


CONTRACT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class EventContract:
    event_type: str
    display_label: str
    summary_fields: tuple[str, ...] = ()
    detail_fields: tuple[str, ...] = ()
    redacted_fields: tuple[str, ...] = ()

    def manifest(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "event_type": self.event_type,
                "display_label": self.display_label,
                "summary_fields": list(self.summary_fields),
                "detail_fields": list(self.detail_fields),
                "redacted_fields": list(self.redacted_fields),
            }.items()
            if value not in (None, "", [], {}, ())
        }


def event_catalog() -> dict[str, dict[str, Any]]:
    return {
        event_type: contract.manifest()
        for event_type, contract in _EVENT_CONTRACTS.items()
    }


def allowed_event_types() -> frozenset[str]:
    return frozenset(_EVENT_CONTRACTS)


def validate_event_payload(event_type: str, payload: object) -> tuple[str, ...]:
    """Return required summary fields missing from a registered event payload."""

    contract = _EVENT_CONTRACTS.get(str(event_type))
    if contract is None:
        return ()
    body = payload if isinstance(payload, dict) else {}
    return tuple(
        field for field in contract.summary_fields if field not in body
    )


def contract_manifest() -> dict[str, Any]:
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "events": event_catalog(),
        "contract_hash": contract_hash(),
    }


def contract_hash() -> str:
    payload = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "events": {
            key: value.manifest()
            for key, value in _EVENT_CONTRACTS.items()
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# S66 removed ``verify_registry``. It was the report body of the retired
# ``hermes harness contracts verify`` lane; S65 took the last production entry
# point with the decision-contract island, leaving one test caller
# (``test_s15_event_contract_pruning``) asserting ``event_count`` — a fact the
# line above it already asserted directly off ``event_catalog()``. That is the
# ledger's settled closed-loop rule (item 2): a symbol whose whole importer set
# is the test written to exercise it is not covered code. ``contract_manifest``
# stays — it has a production reader.
#
# 2026-08-19: that production reader is ``hermes harness contracts dump``, and
# the dead-code audit proposed cutting the verb (0 invocations in either repo
# or the shared skills root) while its own ruling 5 said to KEEP
# ``contract_manifest``. Both together are not a coherent position: with the
# verb gone the constant's whole importer set is ``test_s15_event_contract_
# pruning``, which asserts the manifest is a faithful projection of
# ``event_catalog()`` / ``contract_hash()`` — the closed loop the rule above
# names. So it is one decision, not two, and it was resolved KEEP-BOTH:
#
#   * "no committed invocation" is weak evidence for a read-only DIAGNOSTIC.
#     Operators run `observe`, `migrate`, `snapshot` and this by hand; none of
#     those appears in a committed script either, and nobody proposes cutting
#     them.
#   * this is the ONLY door onto the canonical contract registry. A registry
#     you cannot dump is a registry you cannot audit, and 12 lines is a poor
#     trade for that.
#
# The principle the two H-CLI-3 verbs that DID go were judged on: cut a verb
# whose data has another door (`workspace actors` — `persona list` reads the
# same store through the same summary), keep a verb that IS the only door.


_EVENT_CONTRACTS: dict[str, EventContract] = {
    # S53 (2026-08-01) de-registered foreground_runtime.closed, lane.created,
    # lane.transitioned and lane.transition_rejected with their sole emitter:
    # the GoalRuntimeInstanceStore WRITE lane (create_lane / transition /
    # park_lane / resume_lane / park_open_task / mark_terminal_for_task and the
    # `save` chokepoint they funnelled through). None had a production caller.
    # Cut ORDER mattered: park_lane's one non-test caller was operator_control.py,
    # deleted at S49 earlier in this same wave, so it only became callerless once
    # that landed. S15 already de-registered the other six foreground_runtime.*
    # types when the mission lane went; this is the last of that family. The
    # store's READ side stays live -- status.py projects lanes off list_all. See
    # tests/agent_runtime/test_s53_lane_write_lane_removal.py.
    # S17 de-registered run.heartbeat (RunStore.heartbeat) and run.approved
    # (RunStore.approve_continuation) with their writers; S25 finished the set
    # with run.opened once the two filler appends that were its last minters
    # (tests/agent_runtime/test_events.py) were retargeted onto live types — see
    # tests/agent_runtime/test_s25_run_opened_retirement.py, which owns that
    # delta. Round 4 removed the last production caller of RunStore.cancel ->
    # close_run and de-registered run.closed; historical rows remain readable.
    "run.progress": EventContract("run.progress", "Run progress", ("phase", "step", "status", "summary"), ("next_expected", "proof_id")),
    "child.returned": EventContract("child.returned", "Child returned", ("parent_node_id", "child_node_id", "summary"), ("proof_ids", "artifact_refs", "stage_id", "persona_instance_id")),
    "run.tool.started": EventContract("run.tool.started", "Tool started", ("tool_name",), ("run_id",)),
    "run.tool.finished": EventContract("run.tool.finished", "Tool finished", ("tool_name", "status"), ("duration_ms",)),
    # S32 de-registered decision_contract.parity behind its emitter: S27
    # (5c16417f6) cut the simplified-contract projection half out of
    # simplified_contract.py, and _record_parity — which only that half called —
    # was the only writer that ever produced the type. It compared the public
    # and execution decision types for the deterministic executor deleted in S5.
    # Unlike run.opened / repo_bundle.delivered there is no operator-summary row
    # or formatter arm to retire with it. See
    # tests/agent_runtime/test_s32_decision_contract_parity_retirement.py.
    # NOT the same thing: agent_runtime/parity.py is the read-model
    # ProjectionAccountant and never emitted this (or any) event type.
    # S44 de-registered the six role_envelope.* / role_checklist.* contracts that
    # sat on this line. Their ONLY emitters were RoleEnvelopeStore.save and
    # RoleChecklistStore.save, both deleted with the store family; the two runtime
    # directories had already been archived aside as writer-less on 2026-07-30.
    # Same rule as S25/S36/S37: a contract with no writer is a shape
    # EventLog.append accepts and no reader will ever see. Historical rows are
    # unaffected (append type-checks on WRITE only), and none of the six was in
    # events.OPERATOR_SUMMARY_EVENT_TYPES, so no summary arm went with them. See
    # tests/agent_runtime/test_s44_role_envelope_family_removal.py.
    "persona_instance.created": EventContract("persona_instance.created", "Persona instance created", ("persona_instance_id",), ("persona_id",)),
    # A replication mint is a THIRD intent class, neither authored create nor
    # diagnostic repair (instance-replication plan §3.5), so it gets its own
    # type rather than reusing ``persona_instance.created`` — which means "this
    # machine authored an agent" to every consumer that reads it, and would make
    # one realm pull look like N creates in the log an operator greps.
    # ``action`` distinguishes the three writes the one door takes (a fresh
    # ``replicated``, an ``adopted`` travelling surface, a phase-two
    # ``steered``); ``source`` is always ``realm_sync``, the ``updated_by``
    # precedent. Emitter: ``PersonaInstanceStore.replicate_instance`` /
    # ``apply_replicated_steering``.
    "persona_instance.replicated": EventContract("persona_instance.replicated", "Persona instance replicated from a realm peer", ("persona_instance_id", "source", "realm_id", "action"), ("persona_id", "steered_by")),
    "persona_instance.steered": EventContract("persona_instance.steered", "Persona instance steering edge changed", ("persona_instance_id", "goal_id"), ("persona_id", "spawned_by", "steered_by", "added", "removed", "detached")),
    "persona_instance.reconciled": EventContract("persona_instance.reconciled", "Legacy-id persona instance row folded onto its canonical channel", ("persona_instance_id", "from_id", "to_id", "action"), ("persona_id",)),
    "persona_instance.pruned": EventContract("persona_instance.pruned", "Orphaned/legacy-role persona instance archived from the live graph", ("persona_instance_id", "reason"), ("persona_id", "role", "profile_id", "updated_at")),
    "persona_instance.chat_binding_cleared": EventContract("persona_instance.chat_binding_cleared", "Persona instance unbound from a chat session (operator delete, or a binding whose session SessionDB no longer has)", ("persona_instance_id", "session_id", "reason"), ("persona_id", "cleared_fields", "mode_before", "mode_after")),
    "persona_instance.retired": EventContract("persona_instance.retired", "Placement-backed persona instance retired (end-of-life) to the archive on placement removal", ("persona_instance_id", "reason"), ("persona_id", "mode", "requested_by", "archive_dir")),
    # The persona-instance reconciler's phase-5 graph reap
    # (persona_instance_identity._prune_owner_less_flow_graphs). A runtime flow
    # graph is keyed on its OWNER instance (``runtime:<owner>``), so reaping a
    # row strands the canvas it owned; the phase archives the doc into
    # ``flow_graphs_stale/<ts>_graph_prune/`` and rides this event. The three
    # summary fields are what every emission carries — ``reason`` is the typed
    # constant from flow_graph.py, never free text. No persona rides the Event
    # envelope: by construction the owner no longer resolves to a row.
    "flow_graph.pruned": EventContract("flow_graph.pruned", "Owner-less runtime flow graph archived", ("graph_id", "owner_instance_id", "reason"), ("drawn_agent_count", "archived_to")),
    "steer.returned": EventContract("steer.returned", "Steer returned", ("action_id", "verb", "source_node_id", "target_node_id"), ("result", "stage_id", "persona_instance_id")),
    # S49 de-registered the three operator.takeover.* contracts with their SOLE
    # emitter, agent_runtime/operator_control.py. That module's only importers
    # were two tests and an S41 binding pin; no production caller ever reached
    # `operator_takeover_worker`, and "worker.takeover" was never a registered
    # capability id — it was a string in the function's own return dict. Same
    # rule as S25/S36/S37/S44: a contract with no writer is a shape
    # EventLog.append accepts and no reader will ever see. See
    # tests/agent_runtime/test_s49_operator_control_removal.py.
    "persona_instance.chat_opened": EventContract("persona_instance.chat_opened", "Persona instance chat opened", ("persona_instance_id", "session_id"), ("persona_id",)),
    "persona_chat.projected": EventContract("persona_chat.projected", "Persona chat turn projection committed", ("persona_instance_id", "root_chat_session_id", "client_message_id", "turn_id", "change_kind"), ("active_session_id", "native_revision")),
    "persona_chat.metadata_updated": EventContract("persona_chat.metadata_updated", "Persona chat session metadata updated", ("persona_instance_id", "root_chat_session_id", "change_kind"), ()),
    # C1h-bis (2026-09-05). The turn's own two publishes, and the reason they are
    # a PAIR of types rather than one type with a ``change_kind``: the
    # ``running_work`` chat_turn row APPEARS on one and DISAPPEARS on the other,
    # so "how many turns began here" and "which of them settled" are different
    # questions about different rows and a log an operator greps should answer
    # them separately. The precedent is one lane over in the SAME projection —
    # ``dispatch.recorded`` / ``dispatch.completed`` — which is exactly why that
    # lane's rows reach a subscriber's screen and, until these landed, the chat
    # lane's did not: the hub publishes when the event log moves, and a turn
    # running a model appends nothing of its own between its write-ahead record
    # and its projection commit. No new FRAME kind rides with them — the
    # running_work delta the existing pipeline already builds IS the frame.
    # Emitter: ``agent_runtime.chat_turn_presence.ChatTurnPresence``, called from
    # the chat-turn core that the method lane (``runtime.chat.message``) and the
    # CLI lane (``harness mission-chat message``) share. ``state`` on the end row
    # is the journal's own word, read at publish time, so it cannot drift from
    # what the projection will report.
    "persona_chat.turn_started": EventContract("persona_chat.turn_started", "Persona chat turn started", ("persona_instance_id", "root_chat_session_id", "client_message_id", "turn_id"), ("active_session_id",)),
    "persona_chat.turn_ended": EventContract("persona_chat.turn_ended", "Persona chat turn left the in-flight set", ("persona_instance_id", "root_chat_session_id", "client_message_id", "turn_id", "state"), ("active_session_id",)),
    # The refusal counter-record (2026-08-09). Every durable write a mission-chat
    # turn performs lives inside the chat-root lease, so a send REFUSED on the
    # way to that lease wrote nothing at all — the investigation went looking for
    # a message the operator had definitely sent and found it in zero persistence
    # surfaces (history, live log, turn journal). Summary fields are the three
    # the emitter always passes and the three a loss is diagnosed from. The
    # message TEXT is deliberately absent and must stay absent: its sanitising
    # chokepoints are inside the lease this branch never took, and events.jsonl
    # feeds the snapshot/read-model pipeline. Emitter:
    # ``_publish_persona_chat_send_refused_event`` in persona_commands.py.
    "persona_chat.send_refused": EventContract("persona_chat.send_refused", "Persona chat send refused", ("root_chat_session_id", "client_message_id", "error_kind"), ("persona_instance_id", "lease_owner_pid", "lease_owner_kind", "lease_acquired_at")),
    # The operator chat-delete CLI verb (_cmd_persona_chat_delete). It was
    # emitted but never registered, so the append raised inside its own
    # try/except and the delete left no durable record while the per-instance
    # unbind (persona_instance.chat_binding_cleared) did land — a split trail.
    # Summary fields are the two keys the verb always passes; the three list/
    # actor fields are details. The persona rides the Event envelope's
    # persona_id column, not the payload.
    "persona_chat.deleted": EventContract("persona_chat.deleted", "Persona chat session deleted", ("session_id", "deleted_session"), ("cleared_bindings", "closed_assignment_ids", "requested_by")),
    "persona_instance.profile_updated": EventContract("persona_instance.profile_updated", "Persona instance runtime profile updated", ("persona_instance_id",), ("persona_id", "display_name", "current_chat_goal", "goal_id", "skill_overrides", "provider", "model", "api_mode", "requested_by")),
    "persona_assignment.created": EventContract("persona_assignment.created", "Persona assignment created", ("assignment_id", "persona_instance_id", "kind"), ("state",)),
    "persona_assignment.closed": EventContract("persona_assignment.closed", "Persona assignment closed", ("assignment_id", "state"), ("kind",)),
    # S25 de-registered repo_bundle.delivered one commit behind its emitter: S24
    # (354d7555a) deleted RepoBundleStore.mark_delivered with the
    # delivery-capture path, and it was the only writer that ever produced the
    # type. At the time, every OTHER repo_bundle.* still rode a live
    # RepoBundleStore.update call.
    #
    # S49-54 wave (2026-08-01): that is no longer true. S52 deleted the
    # RepoBundleStore WRITE lane whole -- update, create_or_update_from_task,
    # attach_assignment, mark_running, mark_verified, mark_rejected,
    # wake_ready_dependencies -- because not one had a production caller. The
    # remaining SEVEN repo_bundle.* contracts followed their emitters out:
    # created / updated / assigned / running / verified / rejected / woke.
    # Unlike S44, two of them (updated, assigned) WERE operator-summary types,
    # so events.OPERATOR_SUMMARY_EVENT_TYPES and their shared formatter arm in
    # operator_event_summary went with them -- the S21/S25 rule that the
    # frozenset may not name a de-registered type. The store's READ side stays
    # live; status.py still projects bundles off list_all. See
    # tests/agent_runtime/test_s52_repo_bundle_write_lane_removal.py.
    # S56 (2026-08-01) de-registered ALL TEN worker_session.* contracts --
    # opened / assigned / resumed / heartbeat / context_absorbed / steered /
    # possessed / released / watchdog_warning / closed -- in the SAME commit
    # that deleted their emitters. `agent_runtime/worker_sessions.py` went
    # whole: the write lane had no production caller, and once it was gone the
    # module's entire remaining importer set was its own tests plus the
    # worker-derived persona-instance surface, which went with it.
    # Emitter and registration move together on purpose: S55's gate asserts
    # every registered type HAS an emitter, so splitting the two across commits
    # would turn this cut red rather than let it land silently.
    # See tests/agent_runtime/test_s56_worker_session_lane_removal.py. The final
    # dead-code pass also retired the callerless IncidentStore writers, so
    # incident.opened/closed are read-compatible historical types only.
    # Realm store mutations. Every RealmStore write MUST ride one of
    # these: the stream/read-model pipeline is watermark-gated on the
    # EventLog, so an event-less mutation is invisible to every consumer
    # (launcher snapshot, serve read model) until an unrelated event
    # happens to advance the offset.
    "realm.adopted": EventContract("realm.adopted", "Realm adopted", ("realm_id", "name"), ("server_id",)),
    "realm.created": EventContract("realm.created", "Realm created", ("realm_id", "name"), ("server_id",)),
    "realm.updated": EventContract("realm.updated", "Realm updated", ("realm_id", "change"), ("server_id",)),
    # RealmStore.archive — the one realm mutation whose event was emitted but
    # never registered, so its append raised and was swallowed by
    # _append_store_event's best-effort wrapper and the archive stayed invisible
    # to watermark-gated consumers. ``name`` rides in detail_fields because
    # _append_store_event drops None values. Mirrors workspace.archived.
    "realm.archived": EventContract("realm.archived", "Realm archived", ("realm_id",), ("name",)),
    # Activation events carry realm_id/workspace_id when a scope is activated
    # and {"cleared": true} when the active pointer is cleared — so the ids
    # live in detail_fields, not summary_fields (Stage 12 slice D validates
    # summary_fields on append).
    "realm.activated": EventContract("realm.activated", "Active realm changed", (), ("realm_id", "name", "cleared")),
    "workspace.created": EventContract("workspace.created", "Workspace created", ("workspace_id", "name"), ("realm_id",)),
    "workspace.updated": EventContract("workspace.updated", "Workspace updated", ("workspace_id", "change"), ("name", "persona_id")),
    "workspace.archived": EventContract("workspace.archived", "Workspace archived", ("workspace_id",), ("name",)),
    # Hard delete (archive stays the reversible path). ``reason`` is
    # "operator_delete" or "realm_sync_tombstone" — the resurrection-guard
    # ledger application on pull rides the same store chokepoint.
    "workspace.deleted": EventContract("workspace.deleted", "Workspace deleted", ("workspace_id",), ("name", "realm_id", "reason")),
    "workspace.activated": EventContract("workspace.activated", "Active workspace changed", (), ("workspace_id", "name", "cleared")),
    "realm.sync.pulled": EventContract("realm.sync.pulled", "Realm pulled", ("realm_id", "changed"), ("artifacts",)),
    "realm.sync.published": EventContract("realm.sync.published", "Realm published", ("realm_id", "changed"), ("artifacts", "commit")),
    # Mission Board store mutations. Every BoardStore write MUST ride one of
    # these (standing store rule): the stream/read-model pipeline is
    # watermark-gated on the EventLog, so an event-less board write is invisible
    # to every consumer until an unrelated event advances the offset. Cards
    # NEVER mutate goal state — these are planning-domain events only.
    "board.created": EventContract("board.created", "Board created", ("board_id", "workspace_id"), ("title",)),
    "board.updated": EventContract("board.updated", "Board updated", ("board_id", "change"), ("title", "revision")),
    "board.card.created": EventContract("board.card.created", "Board card created", ("board_id", "card_id"), ("title", "column_id", "created_by")),
    "board.card.moved": EventContract("board.card.moved", "Board card moved", ("board_id", "card_id", "column_id"), ("from_column_id", "order_key")),
    "board.card.edited": EventContract("board.card.edited", "Board card edited", ("board_id", "card_id"), ("fields", "revision")),
    "board.card.archived": EventContract("board.card.archived", "Board card archived", ("board_id", "card_id", "reason"), ("column_id",)),
    "board.card.restored": EventContract("board.card.restored", "Board card restored", ("board_id", "card_id"), ("column_id",)),
    "board.card.conflict_resolved": EventContract("board.card.conflict_resolved", "Board card sync conflict resolved", ("board_id", "card_id", "take"), ("revision",)),
    "board.rebalanced": EventContract("board.rebalanced", "Board column order keys rebalanced", ("board_id", "column_id"), ("card_count",)),
    # Mission Office events — the OfficeStore chokepoint emits one on EVERY
    # mutation (same standing store rule as boards; realm-synced the same way).
    # See EterniaLauncher docs/mission_control/OFFICE_LAYOUT_REALM_SYNC_PLAN_2026-07-17.md.
    "office.surface.created": EventContract("office.surface.created", "Office surface created", ("workspace_id",), ()),
    "office.surface.updated": EventContract("office.surface.updated", "Office surface updated", ("workspace_id", "change"), ("revision",)),
    "office.actor.upserted": EventContract("office.actor.upserted", "Office actor placement upserted", ("workspace_id", "actor_key"), ("persona_id", "items", "revision")),
    "office.actor.removed": EventContract("office.actor.removed", "Office actor placement archived", ("workspace_id", "actor_key", "reason"), ()),
    "office.actor.restored": EventContract("office.actor.restored", "Office actor placement restored", ("workspace_id", "actor_key"), ()),
    "office.actor.conflict_resolved": EventContract("office.actor.conflict_resolved", "Office actor sync conflict resolved", ("workspace_id", "actor_key", "take"), ("revision",)),
    "persona.updated": EventContract("persona.updated", "Persona updated", ("persona_id",), ("display_name",)),
    # The persona ⇄ Hermes-profile rebind chokepoint
    # (``persona_profile_binding.rebind_persona_profile``). ONE event per
    # operation: it moves the persona authority through ``AgentStore.save`` AND
    # cascades every ``persona_instance.profile_id`` projection, so the payload
    # names each moved row (bounded; the overflow is accounted in
    # ``instances_truncated``, never dropped silently). Deliberately NOT added to
    # ``patch_coverage.COVERED_DOMAIN_EVENT_TYPES`` — a rebind batch must degrade
    # to a full core, not ship a patch frame that folds nothing.
    "persona.profile_rebound": EventContract(
        "persona.profile_rebound",
        "Persona rebound to a different Hermes profile",
        ("persona_id", "from_profile", "to_profile"),
        (
            "actor",
            "instance_count",
            "instances",
            "instances_truncated",
            # Partial-apply accounting: the persona authority moved but these
            # projection rows did not. Emitted ON the run that stranded them —
            # the `_agent_*` placement rows have no self-heal, so this event is
            # the only durable record that they need a retry.
            "status",
            "failed_count",
            "failed",
            "failed_truncated",
        ),
    ),
    # Synthetic watchdog event: appended by stream_frames when the scope/catalog
    # fingerprint changed while the EventLog offset did not — an event-less write
    # slipped the Stage 12 rule. Advances the watermark so gated consumers
    # converge; every occurrence names a producer bug to fix at the source.
    "state.reconciled": EventContract("state.reconciled", "Read model reconciled after event-less write", ("fingerprint",), ("source",)),
    # S7-A read-model producer: an op-based, WIRE-LEVEL state-patch entry.
    # Payload is ``{entity, id, op, changed?}`` where op ∈ {upsert, remove,
    # refresh}. ``upsert`` carries ``changed`` — the projected wire fields the
    # mutation affected (derived dependents recomputed), sized to the 4KB cap
    # (oversize values become accounted {oversize,bytes} markers; an unavoidable
    # overflow degrades the op to ``refresh``). ``remove``/``refresh`` carry no
    # ``changed``. ``entity``/``id``/``op`` are the summary fields every emission
    # carries; ``changed`` is optional (detail). ``seq``/``ts`` ride the EventLog
    # envelope, not the payload. Emitted only when ``read_model.delta_patches``
    # is on — which it now SHIPS as (runtime_config.SHIPPED_DELTA_PATCHES); an
    # operator's explicit root-config ``false`` is what makes this type absent.
    #
    # ``created`` (2026-08-16, office fold-promotion plan §V4) is the second
    # optional key: a boolean on an ``upsert`` meaning the row was ABSENT before
    # the write, so a fold must insert rather than merge. It exists so
    # ``patch_coverage`` can gate the widened ops behind a capability token, and
    # it now rides TWO entities:
    #
    # * ``office_actor`` (§V4) — the fold inserts on absent unconditionally
    #   there, because every office upsert already carries the complete row, so
    #   the stamp is purely the coverage gate and the launcher ignores it.
    # * ``persona_instance`` (D3, §10.3) — here the launcher READS it: persona
    #   upserts are subsets, so insert-on-absent must happen only for the
    #   complete-row create, and ``patch_without_target`` must survive for
    #   everything else. Same key, one additional reader.
    #
    # Optional rather than summary because most patches are not create rows and
    # a required key would make every existing emission invalid.
    "state.patched": EventContract("state.patched", "State patched", ("entity", "id", "op"), ("changed", "created")),
    # The LIVE orphan-worktree janitor (delivery_directive.reap_orphan_worktrees,
    # two production callers: harness_doctor and the `worktree reap` CLI verb).
    # Emitted but never registered, so every destructive reap appended nothing —
    # the operator lost the only durable record of what was removed. Emitted on
    # the destructive path only (dry_run previews are write-free), so both counts
    # are always present; `captured` lists the reap-captured patch filenames and
    # is bounded to 20 at the emitter. NOT registered alongside it:
    # worktree.task_reaped and bundle.worktree_reaped, whose emitters sat in the
    # Task-declared-directive residue half of that module — S24 (354d7555a)
    # deleted them, so those two stay unregistered permanently rather than
    # provisionally, and no unemittable-contract debt was created.
    "worktree.orphans_reaped": EventContract("worktree.orphans_reaped", "Orphan worktrees reaped", ("reaped_count", "kept_count"), ("captured",)),
    # Detached agent-to-agent dispatch (``agent_chat_send(wait=false)``). Every
    # ``agent_runtime.dispatch_store`` mutation rides one of these — the same
    # standing store rule realms/boards/offices follow: the stream and
    # read-model pipeline are watermark-gated on the EventLog, so an event-less
    # dispatch write is invisible to the Activity projection and the serve cache
    # until an unrelated event happens to advance the offset. For a lane whose
    # entire purpose is telling an operator that background work is in flight,
    # that is precisely the silent failure it exists to retire.
    #
    # Payloads are SUMMARIES, never transcripts: the 4096-byte cap is a hard
    # append-time refusal, and the reply itself already lives in the target's
    # chat thread that ``target_session_id`` names. ``reply_chars`` carries the
    # size so a consumer can see that a large answer arrived without carrying it.
    "dispatch.recorded": EventContract("dispatch.recorded", "Detached dispatch recorded", ("dispatch_id", "target_persona"), ("sender_session_id", "notify_operator", "title", "remote_install_id")),
    "dispatch.completed": EventContract("dispatch.completed", "Detached dispatch completed", ("dispatch_id", "status"), ("reply_chars", "error", "target_session_id", "remote_install_id", "remote_reason")),
    "dispatch.delivered": EventContract("dispatch.delivered", "Detached dispatch delivered to its sender", ("dispatch_id",), ()),
    "dispatch.dropped": EventContract("dispatch.dropped", "Detached dispatch delivery abandoned", ("dispatch_id", "reason"), ("attempts",)),
    "dispatch.delivery_backlog": EventContract("dispatch.delivery_backlog", "Undelivered dispatch completions exceed the retention cap", ("pending", "cap"), ()),
    "dispatch.outcome_superseded": EventContract("dispatch.outcome_superseded", "A different outcome landed on an already-delivered dispatch", ("dispatch_id", "settled"), ("previous",)),
    # Peer-store mutations (S2c, R-IP12). Every write door in
    # ``gateway_peers`` rides one of these, for the standing store reason: the
    # stream/read-model pipeline is watermark-gated on the EventLog, so an
    # event-less peer write is invisible to every consumer until an unrelated
    # event happens to advance the offset — and a CLI ``peers join`` beside a
    # running serve is exactly that write. Emitting from the WRITING process
    # (the ``realm.sync.*`` precedent) is what makes the join visible with no
    # restart.
    #
    # **Ids and counts only.** No secret, no endpoint list, no roster body, no
    # display name — the 4096-byte cap is a hard append-time refusal, and these
    # rows describe a credential store that a person who was not there will
    # read later. What a row holds is in the row; what happened to it is here.
    # ``grant_id`` rides detail on all five (R-IP17: one errand, one token, every
    # party writes it), and ``store_revision`` so an operator correlating an
    # external edit with a mtime has the number in the same place.
    "gateway.peer.recorded": EventContract("gateway.peer.recorded", "Peer install recorded", ("peer_install_id", "source"), ("grant_id", "store_revision")),
    "gateway.peer.revoked": EventContract("gateway.peer.revoked", "Peer install revoked", ("peer_install_id", "announced"), ("grant_id", "store_revision")),
    # ``store`` is "trust" or "cache" and ``change`` names WHICH fact moved, so
    # a reader can tell an announced rename from a fingerprint rotation from an
    # external write without opening either file.
    "gateway.peer.updated": EventContract("gateway.peer.updated", "Peer row updated", ("store", "change"), ("peer_install_id", "grant_id", "store_revision")),
    "gateway.peer.roster": EventContract("gateway.peer.roster", "Peer roster cached", ("peer_install_id", "count"), ("workspace_id", "fetched_at", "grant_id", "store_revision")),
    "gateway.peer.reachability": EventContract("gateway.peer.reachability", "Peer reachability changed", ("peer_install_id", "reachability"), ("unreachable_since", "error", "grant_id", "store_revision")),
}
