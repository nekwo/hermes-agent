# Agent delete semantics + roster identity — staged implementation plan

Status: PLANNED (2026-08-27). Authority: operator rulings of 2026-08-27 ("i messaged
the alice you placed not mine, so investigate the fix for that", "why retire it
should just be delete"). Plan authored by the coordinating session; built by
dispatched implementation agents (one per repo), verified and landed by the
coordinator.

Two lanes, both born from one live incident on 2026-08-27:

1. **Roster identity** — a CLI placement with a hand-typed `--placement-id known_alice`
   minted `personainst_known_alice`, which does NOT match the launcher's
   deliberate-placement discriminator (`_agent_(\d+|[0-9a-f]{8})$`,
   `mission_agent_identity.dart:121`). It therefore entered the operator-channel
   dedupe (`mission_agent_roster_policy.dart` `_dedupeConversationalInstances`),
   shared the channel key `(profile:alice, "Alice Agent")` with the operator's
   pre-existing `personainst_profile_alice`, and — newer-wins — silently EVICTED the
   operator's own channel from the roster. The operator then @-messaged the
   impostor, whose inherited template model default (`opencode-zen`/`big-pickle`)
   500'd three times upstream (`profiles/base/logs/errors.log` 18:41:45–52). The
   drop was log-only (`MissionRosterDrop(... reason=conversationalChannelCollision
   kept=personainst_known_alice)`); no UI surface warned.

2. **Delete semantics** — `harness persona instance retire` acked
   `archived_actor_keys=[...]` / `office_archive_failures=[]`, but a launcher booted
   19s earlier re-pushed the archived actors (`state: active`,
   `updated_by: operator`). `OfficeStore.upsert_actor`
   (`agent_runtime/office_store.py:854`) treats ANY upsert of an archived key as
   "operator intent to re-add": it clears the resurrection-guard ledger entry AND
   `archived_path.unlink()`s the archive copy. Because the archive copy is gone,
   the retire REPLAY (`agent_runtime/agent_retire.py::_already_retired_ack`, which
   re-reads `archived_actor_keys_for_instance`) answers
   `already_retired: true, archived_actor_keys: []` and can never re-archive — a
   permanent wedge cleared only by `harness office actor-remove`. Measured live.

Operator rulings applied here:
- Gone means gone: the operator-facing action is **Delete**, and a delete must
  survive a running launcher.
- The @ roster should show which persona template an instance came from, in
  parentheses.
- Collisions must warn visibly, not only in a debug log.

Deliberately NOT in scope:
- Changing the dedupe's newer-wins choice. With R1 in place, deliberate placements
  can never enter the dedupe again; newer-wins remains the designed cure for the
  legacy-vs-canonical alias case it was built for. Recorded as a decision.
- The provider 500 itself (`opencode-zen` upstream outage — not ours).
- Template-default model inheritance (same class of gap as the skills lane;
  separate ruling if wanted).

## Wire contract (pinned up front — both agents build against THIS)

New guard refusal on `runtime.office.upsert`: JSON-RPC error **code 4090** (the
existing "a guard refused this write" family, `agent_runtime/serve_rpc.py:32`),
`data.reason = "actor_archived"`, data also carrying `actor_key`, `workspace_id`,
and `persona_instance_id` when known. Cure semantics: NON-RETRYABLE; the client
must drop its local row (the actor was deleted on the authority) — re-placing is a
NEW create with a fresh minted id, never a resurrection. The launcher maps it in
`mission_office_rpc.dart`'s reason parser as a terminal outcome whose handling is
delete-local, following the `class_key_collision` precedent for shape.

New CLI refusal on placement verbs: `data.reason = "placement_id_not_discriminable"`
(shape family as existing typed refusals in the create path), message naming both
cures: omit `--placement-id` to let the server mint, or supply the discriminable
shape.

## Stage R1 (hermes) — placement-id discriminability fence

`hermes_cli/harness.py:1104,1120,1536` accept `--placement-id` free-form;
`agent_runtime/agent_create.py::mint_placement_id` (line 338) mints the proper
shape only when it is omitted. The derived instance id is
`persona_instance_id_for_placement(placement_id)`
(`agent_runtime/persona_assignments.py:2666`) — prefix + token — so an
undisciplined placement id yields an undisciplined instance id and breaks BOTH
repos' discriminators (`is_canonical_persona_channel` here,
`missionInstanceIdIsDeliberatePlacement` there).

Change: at the service boundary (where caller-supplied placement ids are first
validated in `agent_create.py`), REFUSE a supplied placement id whose token does
not end in the deliberate-placement shape (`_agent_` + digits-or-8-hex tail —
mirror the launcher regex exactly; write it once, with a comment naming
`mission_agent_identity.dart:121` as the peer). Refusal reason
`placement_id_not_discriminable`. Do NOT silently canonicalize: `agent create
--placement-id` exists to PREDICT the actor key, and a rewritten id breaks the
prediction.

Acceptance:
- Tests: refusal for `known_alice`-shaped ids on `persona instance create`,
  `persona instance open`, and `agent create`; acceptance for launcher-minted
  shapes and for omitted (server-minted) ids.
- `scripts/changed_line_mutation_check.py --base <ref>` green with claims added to
  `tests/mutation_claims.json` for the new guard lines.
- No argv change → committed CLI contract dump untouched.

## Stage R2 (launcher) — persona template beside the @ handle

The ON LEVEL roster (`MissionAgentHudRosterPolicy` output, rendered from
`mission_agent_chat_panel.dart:161`) and the mention/instance picker
(`mission_agent_instance_picker.dart`) show the display name/handle with no hint
of the backing template. Render the persona id in parentheses after the name —
`Alice Agent (profile:alice)` — in both the picker rows and the ON LEVEL rows.
Plain persona ids render as-is (`(qa)`). Keep it display-only: no key changes, no
policy changes. Widget/golden tests updated where labels are asserted.

## Stage R3 (launcher) — roster drops become visible warnings

`_dedupeConversationalInstances` and `_foldAliasCollisions` record
`MissionRosterDrop`s that only reach `_logDrops`
(`mission_agent_roster_policy.dart:711`). Surface drops with reasons
`conversationalChannelCollision` and `aliasCollision` on the operator-visible
warning surface, following the retire-residue precedent
(`mission_office_layout_controller.dart` — `kMissionAgentRetireReceiptLabel` and
the residue strip machinery around line 484–792): a warning naming the dropped
instance, the kept instance, and the persona. Lifecycle-noise reasons
(`pendingReconciled`, `pendingExpired`, `personaInstanceIdentity`) stay log-only.
Test: policy fold with a colliding pair produces the visible warning state.

## Stage D1 (hermes) — upsert tombstone fence

`OfficeStore.upsert_actor` (`office_store.py:747`, resurrection arm ~805–865):
gate the archived-key re-add behind an explicit `resurrect` intent parameter
(default False) threaded from the RPC/CLI surface. Without it, an upsert whose
`actor_key` has an archive copy (or a live resurrection-guard ledger entry)
raises the typed refusal → wire `4090 / actor_archived` per the pinned contract.

First: AUDIT existing callers of the resurrection arm (grep `upsert_actor` and
the local-lane fallback spawn of `harness office actor-upsert`). If a designed
deliberate re-add gesture exists, thread `resurrect=True` through exactly that
gesture and no other. If none exists, the parameter still lands (the door stays,
with a key) — document which callers were audited in the field notes.

Acceptance: test that a mechanical upsert of an archived key refuses with
`actor_archived` and leaves the archive copy + ledger entry INTACT; test that the
explicit-resurrect path still re-adds and clears both. Mutation claims for the
guard.

## Stage D2 (hermes) — retire replay self-heals

`agent_retire.py::_already_retired_ack` (line 161): before answering, scan LIVE
actors still bound to the instance (`OfficeStore` list by `persona_instance_id`,
same binding the fresh arm uses) and archive them, reporting per-actor failures
in `office_archive_failures` exactly as the fresh arm does. The replay's
`archived_actor_keys` stays re-read-from-archive (now including what the sweep
just archived). `already_retired` stays `true`. Update the module docstring's
"a replay archives nothing, so it can fail at nothing" claim — it is the wedge's
own words. Respect the existing race guard (line ~284).

Acceptance: test reproducing the live wedge — retire, resurrect an actor by
store-level write, retire again → replay ack lists the re-archived key and the
level is clean. Mutation claims. `tests/agent_runtime/test_agent_retire_service.py`
and `test_serve_rpc_agent_retire.py` extended, not rewritten.

## Stage D3 (launcher) — stop resurrecting locally

First MEASURE, then fix (house discipline: re-measure the mechanism before
implementing the fix): locate the actual boot-window re-push. Known anchors: the
page-open re-upserts documented at `mission_office_layout_controller.dart:983`
("12 of 12 field REVISION MISSes were page-open re-upserts"), the
SharedPreferences→hermes migration seed (header, line ~14), and the offline read
cache flush. The live incident: launcher booted, and ~19s later pushed archived
actors back as `state: active, updated_by: operator`.

Then:
- On a clean retire ack AND on folding `archived_actor_keys` deltas from the
  baseline/patch lane, purge the actor from the local offline cache so no later
  flush can carry it.
- Order any boot-time flush AFTER the baseline projection fold, so the archive
  state wins the race.
- Map the new `4090 / actor_archived` refusal in `mission_office_rpc.dart` as a
  terminal reason whose cure is delete-local (drop the row from layout + cache,
  no retry, no sync-failed spiral); wire that handling through the write
  chokepoint.

Acceptance: controller test — cache holds an actor the projection says is
archived → after fold, no upsert is attempted and the cache row is gone; RPC
parser test for the new reason; the `actor_archived` outcome surfaces on the sync
strip as an informative receipt, not a failure loop.

## Stage D4 (both) — the operator's verb is Delete

- hermes: `harness persona instance delete` as a full alias of `retire`
  (argparse alias on the subcommand — same handler, same flags). Regenerate the
  committed CLI contract dump (`hermes_cli_contract.json`, via the launcher's
  `dump_hermes_cli_contract.dart --hermes-root=X:/Eternia/hermes-agent
  --python=C:/Python312/python.exe`) — coordinate: the hermes agent makes the
  argv change; the LAUNCHER agent regenerates and commits the dump in the
  launcher repo, plus the byte-pinned copy in hermes if the fixture is mirrored.
- launcher: operator-facing labels for the gesture say Delete ("Delete agent",
  "the agent was deleted but N placements are still on the level" — keep the
  receipt-label constants' single-definition discipline,
  `kMissionAgentRetireReceiptLabel` stays one place). RPC method names, capability
  ids (`persona.instance.retire`), and internal type names DO NOT move — this is
  surface language only.

## Field notes

Each agent keeps a running-record field-notes file in the repo it stands in
(house rule): hermes
`docs/agent-runtime-harness/planned/agent-delete-field-notes.md`, launcher
`Launcher_Brain/20 — Active Initiatives/agent-delete-field-notes.md`. Every
falsified assumption goes there at the moment it falsifies.

## Landing

Agents build and commit on isolated worktree branches; the coordinator verifies
(tests re-run against the branch, cross-repo contract check, mutation gate) and
lands onto the mainlines with one-breath explicit-path commits (shared-index
rule). The launcher push lane runs only against a quiet tree.
