# 01 — System Architecture: the entities and how they relate

> **What this domain is.** The Agent Runtime Harness is a Hermes-native persona
> runtime for Mission Control. **Chat is the only execution lane**: an operator
> or an agent messages a placed persona instance's durable chat root, and the
> runtime owns identity, chat continuity, realms and workspaces, the Mission
> Office scene, the board, and an enforcement-free agent graph. This document
> is the entity model — what the things ARE and who owns what. How a turn
> executes, how state is stored, and how it syncs are other domains.

Every claim below was verified against code at HEAD on 2026-08-22; anchors are
`path:line` or a named function, and unverifiable carry-forwards are quarantined
in their own section at the bottom.

## The chat-only lane

There is one runtime execution surface. `GPTPersonaRuntime`
(`agent_runtime/persona_runtime.py:51`) exposes exactly **one** public method,
`mission_chat_reply` (`:72`) — there is no `run_persona`, no tick, no worker
loop. The entry point is `_cmd_mission_chat_message` — defined at
`hermes_cli/harness_parts/persona_commands.py:2637`, exec-loaded into
`harness.py` globals (`hermes_cli/harness.py:6213`) and wired to argparse at
`harness.py:1372`.

Turn ingress has one path. Asynchronous agent-to-agent delivery
(`agent_chat_send(wait=false)`) does not inject a message: a serve-hosted drain
forges a real turn through **the same handler an operator message goes through**
(`agent_runtime/dispatch_delivery.py:1072`, docstring `:12-19`), which is what
keeps transcript, live log, turn journal and projection consistent for free.
Two narrower append seams do exist and are deliberate, turn-less writes — the
bounded child-summary mirror (`agent_runtime/continuity.py:52-62`, posted by
`return_summary_to_parent_session`) and the explicit-append seam
(`persona_commands.py:7185`, whose own docstring records the open question of
declaring the persona-chat write path native-only). Doc 05 §8 owns their
contract; neither runs a turn or reaches the provider.

MCP tool admission is data-owned, not role-owned: a persona may admit only the
servers its backing profile declares, and "role names do not narrow or widen
that data-owned set" (`mcp_admission.py:26-29`). `R1_ADMISSIBLE_ROLES` returns
zero hits in production source (its only survivors are quotes in the archived
removal doc).

## The entity chain

Four things, in one chain, each with a distinct lifetime.

**1 — Persona template.** `AgentPersona` (`agent_runtime/models.py:364`) — the
definition: display name, role, model/provider/api_mode, toolsets, skills,
`hermes_profile`, budgets, readiness. Personas are **data**, from the config
block (`config.persona_records_from_config`, `agent_runtime/config.py:532`)
merged with persisted store rows (`ensure_persisted_personas`, `:578`, over
`store.AgentStore` at `store.py:152`). Nothing in code declares them — S11 left
`DEFAULT_PERSONA_IDS`, `BASE_PERSONA_ID`, `DEFAULT_SUPERVISOR_PERSONA_ID`,
`ALLOWED_TOOLSETS_BY_ROLE` and `PER_ROLE_TOOL_DENIES` as scoped tombstone rows
against `agent_runtime.personas` (`tests/agent_runtime/test_tombstone_registry.py`,
s11 rows). A definition can be withheld without being deleted:
`persona_lifecycle.py`'s `DISABLED_ROLE_TOKENS` / `MOTHBALLED_*` keep it in the
administrative catalog but never let it acquire a live instance
(`is_runtime_persona`). Since S0a (2026-09-03) the persona's `toolsets` field is
LEGACY DISPLAY on the harness lane: the capability declaration is the bound
PROFILE's top-level `toolsets:` key, read by `personas.declared_lane_toolsets`
and defaulting to `harness_core` (canon 05 §4c). The field is still carried,
still travels on realm publish, and admits nothing.

**2 — Durable persona instance (the placement).** `PersonaInstance`
(`models.py:310`). Stable identity, outlives any one chat. Ids are structurally
prefixed `personainst_` (`models.py:15`), and there are exactly two derivations:

- the **canonical operator channel** — `persona_instance_id_for(persona_id)`
  (in `persona_assignments.py`), e.g. `personainst_profile_alice`;
- a **placement-backed** row — `persona_instance_id_for_placement(placement_id)`
  (its neighbour there), whose tail is the scene `itemId`.

`is_canonical_persona_channel` is the live discriminator between them.
Every id arriving from outside the store passes the single derivation authority
`canonical_persona_instance_id` (both in the same file), which folds the two structurally
recognizable drift schemes; ids predating that chokepoint are folded by
`persona_instance_identity.py`, whose `identity_aliases_for_rows` ships as
`identity_map` on the snapshot. Per-call UUID instances cannot be minted —
`unique_operator_persona_instance_id` returns zero hits in production source
(one quote survives in the archived doc 01).

**3 — The chat root (swappable).** `PersonaInstance.default_chat_session_id`
(`models.py:360`) is a durable pointer, deliberately independent of everything
else on the row. Ids are minted `persona_chat_<instance>_<hex>` by
`persona_assignments.persona_chat_session_id_for`, and made
**durable at the bind argument**, not beside it, so a pointer cannot be stored
before the transcript row exists (`_durable_chat_root` beside it — the failure it
fixed was a dragged-in agent whose every send was refused with
`unknown_chat_session`). `chat_head_home` (`models.py:379`) records *where* that
pointer dereferences: which profile DB holds a transcript is a per-conversation
fact no machine-level pointer can answer.

**4 — The scene placement.** `OfficeActor` with its `OfficeItem`s (both in
`models.py`), one file per actor under `office/<workspace>/actors/`. The
`actor_key` is minted only by `OfficeStore` — `canonical_persona_instance_id`
for instance-bound actors, else the persona id
(`office_store._canonical_actor_key`). Actor granularity, not item granularity,
is the merge unit, so an agent and its coupled desk travel together. (Symbols
only: the three line cites this paragraph carried — `models.py:179` / `:157` and
`office_store.py:113` — had all drifted by 2026-08-31, the last of them onto an
unrelated constant, and a name is the cheapest thing in this repo to re-find.)

**One call creates all of it.** `agent_create.perform_agent_create` writes the
roster row, the durable chat root and the placement inside one function. The
order is instance-first and that is not arbitrary: a placement written first
would be a half-state naming an instance the runtime never minted, and the
launcher's codec refuses on principle to derive a binding for an actor that has
none — the function's own docstring is the long form. (This paragraph carried
`agent_create.py:692` from consolidation until 2026-08-27, when the correction
recorded that the function "was at `:1205`". By 2026-08-31 it was at `:1271`:
the correction rotted the same way the citation it corrected did, which is the
whole case for naming symbols instead of lines and is why this note no longer
states either number as current.)

**And it chooses WHERE, when the caller did not.** `position` is optional on
both doors (plan S2): absent, the slot comes from
`agent_runtime/office_layout_policy.py` — the same deterministic lattice the
launcher predicts with, pinned across the two repos by a byte-identical case
fixture; present, it is written verbatim. The ack returns `position` (what was
written) and `actor` (the row as stored, in `runtime.office.get`'s own item
shape), so a caller with no canvas — the CLI, a cron, a remote connector over
`call` — needs neither a guess going in nor a second read coming out. Where that
policy read sits relative to `office_lock` — INSIDE it since H-H10, which is
what retired the one-slot race it used to leave — is stated at
`placement_position_policy` and in
[06 — The office surface](06-office-and-board.md#the-placement-verb--where-an-unaimed-create-lands-and-what-it-hands-back).

**And it can hand the new agent its SKILLS — but that phase is outside the
join.** `skills: [id, …]` on the RPC (`--skill`, repeatable, on the CLI) runs
`agent_create.run_skills_phase` AFTER both writes are durable, writing
`skill_overrides` at the INSTANCE tier through
`PersonaInstanceStore.update_profile` and never `persona.skills`, which would
reconfigure every other instance of that persona. Absent, `skill_overrides`
stays `None` and the agent inherits its persona's skills live; the ack's
`skills.inherited` says which of those two an empty `assigned` list means. The
reservation gains `placed` between `instance_minted` and `done` so a skills
refusal keeps the agent — standing, messageable, resumable under the SAME
idempotency key — instead of retiring a working agent to undo a file copy. The
mechanism and its two gates are [05 §9](05-chat-turn-lane.md#9-the-agent-create-path);
the placement consequence is
[06 — the placement verb](06-office-and-board.md#the-placement-verb--where-an-unaimed-create-lands-and-what-it-hands-back).

**And ONE call takes it all away again.** `perform_agent_retire`
(`agent_runtime/agent_retire.py`) is the inverse, over the store method that
always did both halves (`PersonaInstanceStore.retire` archives the row AND, through
`OfficeStore.archive_actors_for_instance`, every actor bound to the instance).
What it adds is a DOOR (`runtime.agent.retire`, `harness agent retire`, and
`persona instance retire` delegating to the same function — with `persona
instance delete` as a full argparse alias since 2026-08-27, because the
operator's verb is delete) and a RECEIPT: the ack
NAMES every actor it archived (`archived_actor_keys`) beside every one it could
not (`office_archive_failures`). The office half is still best-effort — the roster
archive is authoritative with or without the office projection — but it is no
longer SILENT, which is what let a half-state (row archived, desk still on the
canvas) exist with nothing able to detect it. Retiring an already-archived id
replays the same ack with `already_retired: true` rather than refusing, so a
client that lost the ack can ask again — and the replay first SWEEPS any actor a
client mechanically resurrected back into the archive, because idempotent must
not mean inert (the boot-flush resurrection incident, [06
§inverse](06-office-and-board.md#the-inverse--one-call-takes-an-agent-off-the-level-and-says-what-left)).
The receipt is also PERSISTED beside the tombstone, so what the office half did
outlives the ack that carried it and a replay answers `first_attempt` instead of
an empty failure list it cannot stand behind (H-H5); the doctor's placement
census reads it, which is what gives "row archived, desk still live" a standing
detector rather than a one-shot ack (H-H4).

**"Placed" is the JOIN, and neither store is folded into the other** (placement
plan D1). Entity 2 answers *does this agent exist* — `persona instance create
--add-instance` is the roster-only recovery door and mints rows with no
placement, on purpose — while entity 4 answers *is it on this level*. So:

- **placed** — a live instance-keyed `OfficeActor` whose `persona_instance_id`
  names a live `PersonaInstance` row. Both halves, or it is not placed.
- **unplaced row** — a live placement-backed row (`is_canonical_persona_channel`
  is the discriminator; a canonical operator channel is not a placement and
  never was) that no live actor references. **Legal**, and the recovery door's
  normal output.
- **orphan actor** — a live instance-keyed actor whose instance is retired or
  missing. A **defect**: it renders as an agent nothing can message.

The join gets a **read**, never a merge: `harness doctor`'s `placement_census`
(`harness_doctor.py::_placement_census_report`) reports the three per workspace
— beside the two ITEM-level sweeps that ride the same section, desk litter and
duplicate placements — and repairs nothing, because both repairs are operator
gestures and a doctor
reconciling them would be picking which store was wrong from one snapshot. See
[07 — Observability](07-observability.md#the-doctors-report-roster).

The chain ends there. There is no Goal and no Task — `Chat → Goal/Task` is
gone; see [the removal section](#what-the-mission-lane-removal-deleted-and-why).

### `mode` is stored, not derived

The archived entity model called `PersonaInstance.mode` derived. It is not:
`PersonaInstanceStore.open_chat` writes `instance.mode = "chat"`,
`clear_chat_session_binding` reverts the last unbind to `"configured"`, and
`create_free_floating` minted `"free_floating"` (all in
`persona_assignments.py`). The vocabulary also
still contains `"task_bound"`, which is still written — see
[Open rows](#open-rows).

## The agent graph — enforcement-free

The graph is an **authored map that sets one field**. A launcher flow chart
arrives whole as one JSON document (`hermes harness flow set --graph`), is
stored verbatim, and its owner edges are applied to instances that **already
exist**: "Ingest never creates, starts, or deletes an instance, and it never
touches goal membership: a chart states who steers whom, nothing else"
(`agent_runtime/flow_graph.py:9-17`). A parsed doc carries only node id →
optional bound instance, plus an ordered edge list (`FlowGraphDoc`, `:82-93`).
There is no node type in the runtime — a grep for `node_type` across
`agent_runtime/` returns nothing.

**Graph identity IS the owner instance's id** (`graph_id: runtime:<owner>`), so
a map is that one instance's blueprint and may only set or clear *itself* as a
parent in each referenced child. Parents set by another lead's map are
preserved, so two leads' maps compose into fan-in instead of clobbering each
other (`flow_graph.py:19-31`). Non-owner edges drawn on a lead's canvas are
reported, never applied.

The persisted truth is `PersonaInstance.steered_by` — an ordered **set** of
parent instance ids (`models.PersonaInstance`), written through the single
declarative chokepoint `PersonaInstanceStore.set_parents` (in
`persona_assignments.py`; an empty set detaches the child).
`PersonaInstance.spawned_by` is PROVENANCE, not steering: two live writers
outside the store stamp it with a principal — `agent_create` sets `"operator"`,
and `_maybe_stamp_spawned_by` (`persona_commands.py`) stamps `coordinator_id or
"operator"`. Steering itself admits only instance-shaped tokens — read-side
filters apply `models.looks_like_persona_instance_id` (in the `spawned_by` arm
of `snapshot`'s graph projection and in `runtime_hud`) so a principal such as
"operator" is provenance, never a parent, which is what keeps the historical
"steered by operator" phantom edge unrepresentable in the graph.

(Symbols only, and this paragraph is why the rule exists. Every one of its seven
line cites had drifted by 2026-08-31 — and one of them was a CORRECTION that had
itself gone stale: it said the comment at `models.py:330-332` still called
`spawned_by` a store-written mirror of `steered_by[0]`, when that comment had
already been rewritten to say the opposite. A rotted citation misdirects a
reader; a rotted correction tells them the code is wrong when it is right.)

Because graph identity is the owner's id, a stored doc outlives its owner. The
persona-instance reconciler's last phase archives owner-less canvases into
`flow_graphs_stale/` — never deletes — strictly on owner liveness, since an
empty canvas whose owner is live is intended, not garbage
(`flow_graph.py:33-39`, `:446`).

What the graph does *not* do: schedule, gate, or execute. It feeds the agent's
`## Runtime Situation` block, whose declared field roster
(`runtime_hud.py:151` `HUD_FIELDS`) is `preview · scope · lane · mission ·
roster · steering · board` plus two volatile rows (`turn_budget`, capability).
Steering is the one block always emitted, because an explicit empty block is
the honest "standalone" answer (`runtime_hud.py:721-725`).

## Realms and workspaces

`Realm` (`models.py:49`) is the sync and publish boundary; `Workspace`
(`models.py:33`) is the scene and roster boundary. A realm holds
`workspace_ids` plus a `deleted_workspace_ids` resurrection-guard ledger that
travels with it, so a member holding a stale local copy neither republishes nor
re-adopts a deleted workspace. Since 2026-08-28 the same idea guards skill
packages: `skill_tombstones` (`SkillTombstone`, `models.py:104`), a per-realm
ledger capped at `SKILL_TOMBSTONE_LEDGER_CAP = 200` (`store.py:34`), serialized
additively at the existing schema version — the delete lane it powers is
documented under [Skills](#skills).

**Both of those ledgers are UNIONED on pull, not adopted** (RD-11, 2026-08-31,
`4a8d398268`). `_UNIONED_REALM_LEDGERS` (`realm_sync.py`) names them and
`_pulled_artifact_bytes` merges each by its own rule — set-union for
`deleted_workspace_ids`, per-slug newest-stamp-wins for `skill_tombstones` — so
a concurrent publish can no longer drop a delete another member recorded. It is
not gated on `server_id`: a local-only realm with a sync repo loses a guard
entry the same way. Every OTHER realm field keeps the last-writer-wins posture
`skill_selection` documents. The `skill_tombstones` half is what makes the
ledger a per-slug **state register** rather than a delete list: an entry
lifted by `skills restore` stays on the ledger carrying `restored_at`
(`models.py:119-135`), because an absence cannot be told apart from "this
member never heard about the delete" and a union would undo every restore on
the next pull. A stamped entry blocks nothing (`store.active_skill_tombstones`)
and is the settled history the cap prunes first (`store.prune_settled_ledger`),
so an inert restore can never evict a live block.

Realms own what publishes: `skill_publish_mode`
(`all` | `selected`) and `agent_publish_mode` (`workspace` | `selected`), with
personas required by a roster or an Office placement pinned regardless, so a
pulled workspace can never point at an absent persona definition. Stores:
`WorkspaceStore` (`store.py:173`), `RealmStore` (`:472`); active pointers are
single files (`paths.active_workspace_path()` / `active_realm_path()`).
Server-bound realms authorize every sync action against the Eternia backend and
**fail closed** (`realm_membership.py:1-12`).

**"Fail closed" is per-HALF for the READ verb, since 2026-09-02.** `publish` and
`pull` still refuse whole — every byte they touch is a realm-wide assertion, and
no half of either is a denied member's to run. `realm_sync_status` is the
exception, and it is a correction rather than a weakening: it called `_authorize`
FIRST and raised `sync_auth_failed` before answering a single local fact, so a
member whose credential expired lost the store drift, the held skill packages,
the held profile artifacts and the workspace publication rows — every one of them
a credential-free local read — at exactly the moment the diagnostic was worth
having. The authorization now gates the REMOTE half only (the clone, the fetch,
and therefore the freshness of `ahead`/`behind`), and a denial rides the same
additive honesty pair an unreachable remote already rides: `remote_checked: false`
with the typed code (`sync_auth_failed`, `role_insufficient`, …) in
`remote_check_error`. No new key, because from the launcher's side "hermes could
not reach the remote" and "hermes was not allowed to" are one fact — what follows
is the last known LOCAL picture — and the sheet already renders exactly that
(`realm_sync_detail_sheet.dart`'s `_RemoteUncheckedNote`). The FLOOR is
deliberate: a member who has never cloned this realm has no local repo to read,
and the only way to get one is the clone just refused, so the verb still raises
with the code it always raised — and attempts no network call on the way there.

**Unpublished local drift has two exits, not one, since 2026-08-31**
(`3e6d8c06f3`). Pull deliberately never clobbers local state, which used to
leave an operator holding drift they never meant to publish with Publish as the
only door. `_board_store_drift` / `_office_store_drift` now build per-item rows
(`StoreDriftItem`: family, container, item_key, kind — `realm_sync.py`) and
the four existing counts are DERIVED from those rows, so the count shapes the
launcher parses are byte-identical and `store_drift.items` is additive beside
them. `hermes harness realm sync revert <realm> [--item FAMILY:CONTAINER:KEY]…
[--all] [--dry-run]` (`harness.py:624-631`, `agent_runtime/realm_revert.py`)
realigns those exact rows to the last-pulled upstream already on disk:
`--yes`-gated like publish/resolve because it is destructive of LOCAL state,
archive-never-delete so it is recoverable, and **local-only** — no git, no
network, no `--credential-file`, and it never mints a realm-visible tombstone.

**A pull that delivers a desk now delivers the AGENT behind it**
(landed 2026-08-31, H1–H4, tip `a0c171af47`). Until that day the pull adopted
office actors and never instances, so a pulled placement was born with an actor
key and no `persona_instances/` row and the launcher badged it "Not linked here".
The lane that closes it is one more family applier, and nothing else:

- **The projection.** `agent_runtime/persona_instance_sync.py` splits the
  32-field `PersonaInstance` record into three disjoint sets whose union is every
  dataclass field — `PERSONA_INSTANCE_ALLOWED_KEYS` (14, travels),
  `PERSONA_INSTANCE_DERIVED_KEYS` (6, re-derived by the mint), and
  `PERSONA_INSTANCE_LOCAL_ONLY_KEYS` (12, neither) — because "does this leave the
  machine" and "is this re-derived on arrival" are different questions about one
  field and both have to be answerable. A test asserts the partition is TOTAL
  over `dataclasses.fields(PersonaInstance)`, so a field added tomorrow cannot
  compile green unclassified. The 14 publish as `store/persona_instances.yaml`
  (`kind: realm_persona_instances`), synthesized in the SAME gated walk that
  already resolves persona ids — `OfficePublishScan` grew `instance_ids` rather
  than a second `glob`, because a refusal gate cannot speak for a walk it did not
  take. Pruned to the ids the published desks reference; no artifact at all when
  a realm has no instance-backed desks.
- **The seam.** `apply_persona_instance_pull` runs AFTER
  `apply_profile_artifact_pull` and BEFORE `_apply_workspace_tombstones`. After,
  because the mint reads the pulled persona definition to derive `role` and
  `profile_id` and a mint from a definition that has not landed builds the wrong
  agent; before, so a replica is never minted into a workspace the same pull is
  about to archive.
- **The mint is a STORE DOOR, never a file write.**
  `PersonaInstanceStore.replicate_instance` — the delta patch, the local
  derivations, and the event-then-patch ordering are all store-level facts, and
  an applier writing `persona_instances/` directly would lose all three. It was
  the board lane's event-less-adopt defect, refused in advance — a defect the
  board lane itself no longer has (see [The board](#the-board)). The receipt is a
  THIRD intent class: not authored (nobody clicked here) and not diagnostic (this
  is a peer's authored fact arriving), so it is `persona_instance.replicated`
  with `source: "realm_sync"` — reusing `persona_instance.created` would make one
  pull read as N local creates in the log an operator greps.
- **The baseline is keyed off the REMOTE hash**, in its own sidecar
  (`paths.persona_instance_baseline_path`), through the shared
  `classify_three_way_pull`. That is what makes a fresh replica read as
  baseline-aligned rather than as unpublished local drift — without it the very
  next `realm sync status` offers to revert correct state. HOLD never clobbers,
  and an instance simply missing from the projection is `upstream_absent`, never
  a delete: absence is short-answer-shaped and this subsystem has already paid
  for inferring deletion from a short answer.
- **A dropped steering edge HEALS itself, since 2026-09-02.** Phase two drops an
  edge whose parent is absent here (refused, unpublished, or canonical) and
  accounts it on `steering_dropped`. That left the row's local body differing
  from the remote body while the baseline held the REMOTE hash — so every later
  pull classified it `kept_local`, and phase two does not re-run for those. The
  edge was gone for good even on a realm that published the parent one pull
  later; the H3 field notes filed it as a known non-convergence. Re-running
  phase two for `kept_local` rows was the obvious cure and stays REFUSED: it
  would clobber an operator's own re-steer, the one thing `kept_local` exists to
  protect. What closes it is a durable record of the drop —
  `paths.persona_instance_dropped_steering_path`, `{instance_id: {parents,
  remote_hash}}`, never synced and never published — plus one discriminator,
  `_healable_dropped_parents`: the row re-enters phase two only when the realm
  has not moved the body since the drop AND the local body is still EXACTLY
  "remote minus the dropped edge". An operator re-steer differs somewhere the
  dropped edge cannot account for and is left alone. The ledger is REBUILT by
  the pass that ran phase two rather than merged, so a healed edge, a HELD row
  and a row the realm stopped carrying all clear themselves with no expiry rule;
  the two arms that return before phase two (an older peer's absent projection,
  an unreadable one) deliberately do not write it. Only `parent_absent` is
  recorded — a self edge and a cycle are refusals of the remote GRAPH and no
  arriving parent can repair them. The ack gains one additive list,
  `steering_healed: [{key, parent}]`, and a healed row is counted in `adopted`
  because a travelling field did move forward onto an existing row.
- **Drift and revert reach these rows.** `DRIFT_FAMILY_PERSONA_INSTANCE`
  (`realm_sync.py`) with counts `store_drift.persona_instances` additive
  beside `boards` / `office`, items keyed `{family, container=workspace_id,
  item_key=instance_id, kind}`, and the revert selector
  `persona_instance:<workspace_id>:<instance_id>`. `classify_revert` needed the
  family's transition rows and no new table — it was already total over
  family × kind. The revert routes through the FAMILY's admission door, not just
  the shared scan, or a revert would admit a body its own pull would refuse.

Scope is placement-backed rows only. Canonical channel rows
(`is_canonical_persona_channel`) are derived identically on every machine
already; publish SKIPS them and the pull door REFUSES them
(`canonical_channel_not_replicable`), which is what keeps the lane independent of
the queued global-singleton redesign. The ack is
`result["persona_instance_sync"]`, emitted unconditionally with
`source: "projection" | "unreadable" | null` — an omitted key cannot tell an
older PEER apart from an older local hermes, and the launcher has to tell them
apart. **The two-machine live proof RAN and PASSED, 2026-09-01** (plan stage
L3, operator-run): Windows deleted and re-added the Neko agent — exercising
the tombstone and resurrection-guard path rather than dodging it — and
published; the Mac's freshly booted serve pulled and minted
`personainst_neko_supervisor_agent_2e94fab3` through the store door
(`persona_instance.replicated` at `2026-09-01T05:42:04Z`,
`source: "realm_sync"`), the launcher's "Not linked here" badge cleared, and
the operator's chat with the replica produced a real turn record. A realm pull
delivers a working agent, demonstrated live across two machines — the receipt
ledger is `docs/mission_control/planned/instance-replication.md` in the
EterniaLauncher repo.

Workspace scoping is its own authority, and it governs **advertising and
bare-persona resolution only** (`agent_runtime/workspace_scope.py`):

- `workspace_id` of `None` on an instance means runtime-global — visible and
  addressable in every workspace.
- A non-`None` pointer is a "belongs to THIS workspace" claim.
- A scope of `None` (no active workspace) degrades to unscoped rather than
  hiding the roster.
- `exclude_global_canonicals` (`:191`) — a persona's auto-derived canonical row
  is never advertised into a real workspace scope, because instance means
  in-level placement.
- `shadow_canonical_by_placement` (`:150`) — where an in-scope placement
  exists, a bare persona id lands on the deliberate placement, not the plumbing
  row.

Explicit `personainst_*` targeting stays legal cross-workspace, and identity
lookups always read the full unfiltered roster — a steering edge into another
workspace is a real graph fact even when it is not addressable.

`workspace_template.py` copies authored **structure** between workspaces (office
taxonomy and placements, the default board's active cards, roster, settings);
history is never copied.

## The board

Workspace-scoped kanban, and **planning state only**: "Cards are planning
state. They do not carry or mutate mission records"
(`agent_runtime/board_store.py:8-15`). `Board` / `BoardColumn` / `BoardCard`
are at `models.py:135` / `:92` / `:108`. `BoardStore` is the single write
chokepoint and emits a typed event on every mutation.

**That second clause only became true on 2026-09-02.** `board_sync
.apply_board_pull` wrote board defs and cards with a raw `atomic_json_write`,
past the store entirely, so a pull that ARCHIVED a card emitted
`board.card.archived` (it goes through `archive_card`) and reached every live
consumer, while a pull that GAVE you a card — or changed the column taxonomy
under your lanes — emitted nothing, advanced no watermark, and sat on disk
invisible until an unrelated write happened to wake the pipeline. The office
family closed the identical asymmetry in H1 (`f810bd2ac`); the board family now
has its twin verbs, `BoardStore.adopt_remote_board` / `adopt_remote_card`, and
both `apply_board_pull` and `realm_revert._adopt_from_upstream` route through
them. The bytes written are unchanged, so nothing about pull classification
moves: `board_content_hash` excludes `updated_by`, `revision` and the
timestamps, so stamping `updated_by="realm_sync"` (`"realm_sync_revert"` on the
revert lane) is hash-neutral and the baseline still keys off the REMOTE content.
Board events stay UNCOVERED on the patch lane by design, so what a pull now
reaches is the honest full-core delta it should always have reached.

Three properties make it converge across machines without a merge engine: the
default board id is deterministic (`board_default_<workspace_id>`,
`board_models.py:39`) with default columns on fixed ids and behaviour keyed on
`kind`, never `title` (`models.py:92-105`); archive-never-delete, with an
`archived_card_ids` ledger blocking a pulled remote copy from resurrecting a
locally archived card; and card position is a fractional `order_key` whose moves
allocate the midpoint between neighbours (`board_order`).

Agents reach it through the upstream-owned `tools/board_tool.py`, where
resolution is now **two rungs**, not three — explicit `board_id`, else the active
workspace's default board (`board_tool.py:79-102`) — and passively, as the
advisory `board` digest row of `HUD_FIELDS`.

## Skills

`docs/agent-runtime-harness/harness-skills/` is **installed source and stays
live in place**. It is the repo-side origin
(`skill_install.harness_skill_source_root`, `agent_runtime/skill_install.py:34`);
`harness install-harness-skills` (`hermes_cli/harness.py:1663`) copies each
package to the single shared canonical root, `get_shared_skills_dir()` —
root-relative, not per-profile, so every persona references one copy and realm
sync publishes it (`skill_install.py:38-43`). Never edit the installed copy.

The directories present match `hermes_constants.CANONICAL_SHARED_SKILL_IDS`
(`hermes_constants.py:19`) exactly — four since 2026-08-28:
`harness-dev-delivery`, `harness-qa-verdict`, `harness-runtime-model` and
`harness-charsheet-authoring`. `harness-continuity` folded into
`harness-runtime-model` the same day (lean-preload rule: the digest in
SKILL.md, the full recipe in `references/operations.md`). The 2026-08-28
skills consolidation deleted `harness-mission-lead` (the mission-lane tombstone)
outright, folded `launcher-analyze-proof` into `harness-qa-verdict`, and folded
the shared-root-only `mission-control-harness` into `harness-runtime-model` —
whose lean `SKILL.md` stays the required preload while the absorbed operating
manual lives in its on-demand `references/operations.md`. Each of those merges
kept the count at five, not raised it. Read
the constant for the count; this list is a gloss and went stale within a day of
`harness-charsheet-authoring` joining it.

**The two copies are joined by `install-harness-skills` and by nothing else,
and the join runs at the moments a machine ACQUIRES drift.** A turn loads the
INSTALLED package — for a canonical id the resolver rejects every other
candidate (`skill_utils._skill_resolution_status` → `invalid_source` for any
`source_kind` but `shared_core`) — so a commit that edits the repo copy changes
the documentation and changes nothing an agent reads. It happened: the
`harness-charsheet-authoring` package was installed at `5504706978` and edited
twice more the same hour; the installed copy stayed 449 B behind for two days,
and the live gate turn's `used_skills` row carried the stale package's hash.
Tests do not see this — they read the tree, which is correct by construction.
So the check runs where the guarantee lives, on the machine:
`scripts/verify_harness_skill_install.py` installs every canonical package and
then fails if `harness_skill_hash_mismatches` is non-empty (`--check` verifies
without writing). The runtime reports the same divergence passively as the
`skill_hash_mismatch` readiness code (`agent_runtime/profile_readiness.py`),
which is where to look when a persona is behaving like an older version of its
own skill.

**The same gate now measures SIZE, because two identical copies do not settle
how big they are.** A canonical package is `load_policy: required_preload`, so
its whole `SKILL.md` body is pasted into every turn of every persona that lists
it (`mission_chat_turn_context.py::_resolve_skill_preload` →
`skill_commands.py::build_preloaded_skills_prompt` →
`skill_commands.py::_build_skill_message`, which appends `content.strip()`
verbatim). Its LENGTH is therefore a standing per-turn cost, and hashes are
indifferent to length: the `harness-charsheet-authoring` rewrite took the
package from 26,042 B to 44,478 B — 70% growth, on every turn, forever — with
this gate green throughout, and the growth was filed as a missing measurement
rather than caught. `skill_install.py::SKILL_SIZE_CEILINGS` is that
measurement: one declared byte budget per canonical id, today's size rounded up
with its headroom and a one-line reason, read by
`skill_install.py::harness_skill_size_overages` and failed by
`verify_harness_skill_install.py::main` in the same verdict as the hash lane.
**A canonical id with no entry fails** — unmeasured is not the same as within
budget. Only `SKILL.md` counts: a package's `references/` are named by PATH in
the turn and fetched with `skill_view` on demand, so pricing their bytes here
would charge for context no turn carries and push authors to inline them, which
is backwards. Raising a ceiling is a one-line diff in the same change as the
growth; deleting a paragraph is the other answer and usually the better one.
The budget is deliberately NOT on the serve-boot path — a boot repairs drift it
did not cause and must not be blocked by an authoring decision the producer
owns. On the turn record the same number rides
`prompt_observability.py::_resolved_skill_receipt` as `skill_md_bytes`, beside
the `content_hash` that says which bytes: the hash answers WHICH, this answers
HOW MANY, and `None` (never `0`) when the skill did not resolve.

**Four triggers run the join, and the trigger set is the whole design**
(operator ruling 2026-08-30; `archive/skill-install-trigger-relocation.md`):

| Trigger | Where | Covers |
|---|---|---|
| explicit CLI | `harness install-harness-skills` | manual only |
| realm-sync pull | `agent_runtime/realm_sync.py:509-511` | realm members, on realm pull |
| `git pull` | `.githooks/post-merge` → the verify script | a consumer's merge pull |
| `harness serve` boot | `harness_parts/serve.py` `install_harness_skills_at_boot` | every boot, every pull shape |

The first three fire when a machine PUBLISHES or explicitly syncs. Boot is the
one that fires when a machine merely *consumes*, and it is why the pre-push
hook is gone: repairing at push fixed the machine whose repo copy was already
the newest thing in the realm, and left every puller unrepaired until it
happened to push. Boot is also the only site with an unambiguous home — the
verify script's exit-2 refuse-to-guess ladder exists because a git hook
inherits an arbitrary shell, whereas serve was spawned with `HERMES_HOME`
pinned. **One command per clone arms the hook** — `git config core.hooksPath
.githooks`, which git shares across every worktree of that clone — and it does
not cover `git pull --rebase`, which fires no post-merge; serve boot does.

Three adjacent lanes are live. `skill_promotion.py` is the one guarded door
through which downloaded or authored packages become canonical: downloads land
in a per-realm inbox the resolver never sees, displaced packages are archived
not deleted, promotion is an atomic `os.replace`, and provenance lives outside
skill dirs so it cannot change a package's content hash.
`external_skill_links.py` links the shared root into other harnesses on the
machine (Claude Code, Codex), idempotently and non-destructively.

**The third lane (2026-08-28) makes removal travel.** `hermes harness skills
delete <slug> [--realm …] [--dry-run]` (`hermes_cli/harness.py:2633`) archives
the local package — never deletes, `.archive/<timestamp>/` beside the shared
root — writes a `{slug, deleted_at, deleted_hash}` tombstone into every realm
that currently publishes the name (the R-E default: a mode-`all` realm
qualifies only through a covered local package, a mode-`selected` realm by
naming the slug even with no local copy), prunes the slug from
`skill_selection` (R-F), and unlinks the per-realm inbox mirror (a cache the
next pull rebuilds). The match rule is single and one-to-many: a bare `foo`
tombstone covers top-level `foo` AND categorized `<cat>/foo`
(`store.skill_tombstone_matches`, `store.py:451`) — which is why the delete
receipt's `archived` array is the truth and its scalar fields are only the
single-package convenience. Enforcement is entirely client-side, because a
GitHub-App push has no pre-receive hook, and it closes at three points in
`realm_sync.py`: pull applies the ledger (`_apply_skill_tombstones`,
archive-never-delete), pull's auto-adopt skips tombstoned slugs, and publish
filters them out of the artifact set (`_skill_artifacts`). Canonical
ids refuse with `skill_installer_owned` — every pull reinstalls the canonical
set, so a tombstone would fight the installer forever (the R-B ruling); a
malformed slug refuses with `skill_slug_invalid`; both are exit-code family 2.
An unknown slug is a warning (`skill_unknown`, exit 0), as is
`skill_no_local_package` ("nothing archived" and "nothing written at all" are
different answers). The only resurrection door is the explicit
`skills restore <slug> --realm <id>` (R-C), which STAMPS `restored_at` on the
ledger entry (RD-11, 2026-08-31 — it used to remove it; see
[Realms and workspaces](#realms-and-workspaces) for why an absence could not
survive the union) and hands back the archive path plus the promote command —
it does NOT re-add the slug to `skill_selection`. The launcher seam reads identical
`{slug, deleted_at, deleted_hash}` rows in three places: the sidecar,
`realm_sync_status`, and the additive `tombstones` array on
`realm skills show`. Both verbs route their success envelopes through
`attach_root_observability` — a delete aimed at the wrong shared root would
otherwise report a well-formed `skill_unknown` and read as "already gone."
**Conflict posture is UNION, since 2026-08-31** (`4a8d398268`, plan stamped
SHIPPED at `25dfcb897a`): R-D's last-writer-wins v1 — under which two members
publishing concurrently could silently drop a ledger entry — was upgraded and
built. The merge is described under
[Realms and workspaces](#realms-and-workspaces). Do not re-quote LWW for this
ledger; it now holds only for the realm's ordinary fields.

## Persona identity

Identity is layered, and each layer has an owner. The Mission Control chat
system message is composed by `_mission_chat_surface_message`
(`persona_runtime.py:525`) in this order:

1. **Runtime identity** — a first-person block naming the selected persona and
   making self-relay impossible (`_mission_chat_identity_prompt`, `:434`).
2. **Profile SOUL** — the profile-owned durable character and voice.
3. **Operator-channel rules** — tool, permission, clarification and
   anti-fabrication behaviour, always applied. It also carries the one standing
   routing posture the channel owns: **character / sprite-sheet authoring is a
   delegation.** An authoring ask goes by `agent_chat_send` to the teammate who
   carries the charsheet authoring skill — selected by capability from the HUD
   roster, never by a memorized instance id — and their `MEDIA:` /
   `CHARSHEET-QA:` lines are relayed verbatim; a persona that already holds the
   skill is exempt and does the work itself. Owner ruling R-1 = option **1b**,
   delegation, NOT "assign the skill to the supervisor"
   ([planned/charsheet-turn-efficiency-2026-08-29.md](planned/charsheet-turn-efficiency-2026-08-29.md)).
4. Optional workspace `AGENTS.md`, behind a fixed preamble that states the
   boundary: a repo doc describes the repo and never redefines how this channel
   handles confirmation (`MISSION_CHAT_WORKSPACE_AGENTS_PREAMBLE`, `:473`).
5. Optional operator per-session surface prompt.

SOUL resolution defaults to `profiles/<hermes_profile>/SOUL.md` with **no
fallthrough**: on a miss a bare `SOUL.md` must never resolve to the operator
profile's SOUL, which is the persona-identity-leak class
(`_mission_chat_soul_overlay`, `:949-960`). Profile memory and core context
files are persona-declared opt-ins (`models.py:266-267`). That whole string is
**byte-stable for the life of a conversation** by invariant, because the
transport keys its cross-turn prompt cache on `sha256(instructions + tools)`;
anything per-turn volatile — the Runtime Situation HUD, the queued-skill
preload — rides the operator's *user* turn instead (`:496-511`,
`_mission_chat_user_message` at `:533`).

Visual identity is a separate live lane: `agent/charsheet/` generates
directional character sheets behind `hermes harness` verbs
(`hermes_cli/harness.py:3001+`), and a placement carries its sprite as
`OfficeItem.pet_slug` (`models.py:174`). Since 2026-08-31 the interactive
per-verb lane has a one-shot sibling: `harness characters auto`
(`harness.py:4782`, `_cmd_characters_auto` at `:4781`, shipped `2321a2a9c3`,
plan stamped a ledger at `8e0617a458`) drives turnaround → approve → generate →
compose → install in ONE process, printing a receipt line per stage. It is for
an operator's explicit "drive it all the way" ask and nothing else, because it
auto-approves the turnaround — the last moment a reference can change. It never
overrides a handedness refusal (there is no `--accept-handedness` on it), it
writes the same per-attempt history the interactive verbs write so `reopen`
repair and the QA crops behave identically, and it RESUMES rather than restarts:
a stage whose work already exists is skipped with the reason on its summary
line.

## What the mission-lane removal deleted, and why

On 2026-07-30 the goal/task mission lane was removed whole. Deleted: goal and
task records and their stores; the dispatch loop and worker execution
(`goal_runner`, `ticker`, `supervision`, `worker_actions`, `root_node_engine`,
`node_tools`, `agent_runtime.reconciler`, `recovery`, `planning`, `liveness`);
the proof and gate machinery (`proof_gates`, `gates`, `final_gate`, `promotion_gates`,
`proof_runner`, `burn_in`, `smoke`, `replay_scenarios`); the stage graph
(`mission_plan`, `default_plan`, `state_machine`); and role gating of tools and
MCP admission. The receipt is data-driven and enforced:
`tests/agent_runtime/test_tombstone_registry.py` holds one row per banned name,
scanned against AST-reparsed source so a retirement comment can never satisfy
its own gate.

**Why**, in the words of the archived docs that argued it: the controlling
principle carried forward from the root-node design is *no judgment in Python;
the harness is substrate only*
([08](archive/2026-08-22-pre-consolidation/08-blueprint-as-script-collapse.md),
header note). That is incompatible with a per-task state machine whose `BLOCKED`
was a terminal dead-end and whose proof gates could hard-block a transition.
Judgment moved to the agent in the chat lane; the runtime keeps identity,
continuity, scope and evidence.

Two things were deliberately kept: `agent_runtime/blueprints/resolve.py`, a
permanent re-export of `promote_profile_to_persona` for the upstream
profile-promotion endpoint, and `task_store_stub.TaskStoreStub` (re-exported as
`TaskStore` at `store.py:149`) under ruling R-3 — though its stated cause has
since changed; see Open rows. Personas and profiles were **not** deleted:
nothing under `.hermes/profiles/` was touched, only the hardcoded logic that
declared them.

The full S0–S12 record, the six corrected premises, the five operator rulings
and the final acceptance are in
[16 — Mission Lane Removal](archive/2026-08-22-pre-consolidation/16-mission-lane-removal.md).

## Invariants

- **One template, one Library card; instances are never rendered as templates.**
  Level multiplicity is a feature; Library dedupe is a different surface. The
  "double Alice" defect was the two being conflated.
- **A persona-instance id is derived, never invented** — canonical from the
  persona id, or placement-derived from the scene `itemId`. Never a per-call
  UUID: that is what minted the duplicate rows the reconciler now folds, and it
  is why level multiplicity is attributed rather than orphaned.
- **The chat root is independent of everything else on the row.** Opening
  another chat must not cancel or rebind other work
  (`PersonaInstanceStore.open_chat`'s docstring), and a bind is refused for a
  sibling-steal or a retired instance (`PersonaInstanceStore.assert_bindable`).
- **A chat pointer is never stored before its transcript exists**
  (`persona_assignments._durable_chat_root`). The ordering is structural — the pointer
  cannot be bound without the durability call having returned.
- **Graph ingest never creates, starts, or deletes an instance**
  (`flow_graph.py:9-17`). A map states who steers whom, and only its own owner's
  edges, so two leads' maps compose into fan-in instead of fighting.
- **Only an instance can be a steering parent** (`models.py:18`). A principal is
  provenance, not a parent.
- **Archive, never delete** — board cards, office actors, persona-instance rows,
  owner-less flow graphs and displaced skill packages move aside and leave a
  resurrection-guard id in a bounded ledger. Deletion loses the evidence that a
  pulled remote copy is stale.
- **Personas and profiles are data.** No code declares a persona, a role
  ceiling, or an MCP admission floor. An unknown role stays active by default.
- **Advertising is scoped; identity is not.** Workspace scope narrows who an
  agent may be offered and who a bare persona id resolves to — never who
  something *is*.
- **Volatile facts never enter the system prompt.** They ride the user turn, or
  the ~13K-token stable prefix is re-billed every turn.
- **An idempotency receipt stores the KEY OF the row, never a picture of it.**
  A replay re-reads and rebuilds; where it cannot, it says so rather than
  presenting the recording as current. `agent_create` learned this the
  expensive way — its `STATE_DONE` arm echoed the whole recorded ack for an
  actor an operator drag, a realm pull or a `resolve_conflict` had since moved,
  and it now re-reads through `_live_actor` and stamps `actor_fresh: false`
  when it cannot. A 2026-09-02 audit of every other receipt ledger in the
  runtime — chat-turn reservations, both persona-chat mint stores, the board,
  the retire receipt, the mission-chat turn journal, the steer ack, the kanban
  key column, and the three in-memory caches — found **no second instance**:
  each is either id-only with a live re-read, or records a decision no live row
  can contradict. Three had reached the rule independently, and `agent_retire`
  goes further, sweeping live placements BEFORE the archived-keys read so a
  replay self-heals instead of merely reporting.
  **What that audit did not ask, and the answer, 2026-09-02**: it swept other
  LEDGERS and never re-read `agent_create`'s own excluded list. `_reply` kept
  `skills` beside `persona_instance_id` / `placement_id` / `actor_key` as "the
  recorded decision", and the block is a MIX — `inherited` is a statement about
  the request, but `assigned` mirrors `PersonaInstance.skill_overrides` (which
  `update_profile` mutates) and `installed[].installed_hash` names bytes the
  next install of the same id displaces. Both observations are now re-read on a
  replay (`_observed_skills`, `skill_install.installed_harness_skill_hash`)
  under a `skills_fresh` valve shaped exactly like `actor_fresh`; `inherited`
  and `installed[].changed` stay verbatim, because re-deriving them would make
  the ack a second authority for what the key decided. The lesson generalizes:
  the exclusion list on a re-reading builder is itself a claim, and it has to be
  audited field by field rather than block by block.
  **The corollary is the half that WAS broken**: "I cannot resolve this
  receipt" is not "there is no receipt". The board answered both with `None`,
  so the caller re-ran the write and `add_card` minted a twin under the key
  that existed to prevent one. Unresolvable now refuses
  (`errors.IdempotentReplayUnresolved`).
  **The second half, closed 2026-09-02**: the same namespace is per-BOARD and
  three verbs read it, so a key crossing from `add_card` to `move_card` replayed
  the other verb's card and the move silently did not happen. The receipt now
  records its `verb` and a crossing refuses
  (`errors.IdempotencyKeyVerbMismatch`) — see
  [06 §One idempotency key names ONE verb](06-office-and-board.md#one-idempotency-key-names-one-verb).

## Open rows

Each links to a `planned/` file carrying its evidence and the gate to open it.

- 2026-08-22 — **`--kill-active` is a no-op that reports success**: accepted by
  `open_chat`, never read; the CLI echoes the operator's own flag back as
  `killed_previous` →
  [planned/chat-swap-kill-active-guard.md](planned/chat-swap-kill-active-guard.md)
- 2026-08-22 — **`mode="task_bound"` is still written** by a flag documented as
  a correlation id, five readers still branch on it, and `TaskStoreStub` has
  lost the upstream caller its permanence rests on →
  [planned/task-bound-vocabulary-retirement.md](planned/task-bound-vocabulary-retirement.md)
- 2026-08-22 — **a writer-less lane store still projects onto `status`**
  (`foreground_runtime`, `runtime_instances`), and two live docstrings describe
  deleted execution lanes →
  [planned/writerless-goal-lane-residue.md](planned/writerless-goal-lane-residue.md)
- Deferred by design — **no node taxonomy, no sub-graph expansion**; the Node
  Charter's four questions are unanswered for every candidate →
  [planned/graph-node-taxonomy-and-subgraphs.md](planned/graph-node-taxonomy-and-subgraphs.md)
- Queued — **the global-singleton persona-instance redesign**, cited at
  `persona_assignments.persona_instance_id_for` →
  [planned/global-singleton-persona-instances.md](planned/global-singleton-persona-instances.md)
- 2026-08-24 — **a character's state vocabulary is fixed at `start`**: there is
  no `characters add-state`, so adding a strip to an installed sheet means
  re-authoring the character →
  [archive/charsheet-add-state.md](archive/charsheet-add-state.md)
- ~~2026-08-28 — the skill-tombstone ledger merges last-writer-wins~~ —
  **CLOSED 2026-08-31**: R-D was upgraded LWW → UNION and built
  (`4a8d398268`); the merge is canon under
  [Realms and workspaces](#realms-and-workspaces) and
  [Skills](#skills). The plan is stamped SHIPPED at `25dfcb897a`.

## Unverified carry-forward

- **Launcher-side entity rendering.** The archived model
  ([01-architecture.md](archive/2026-08-22-pre-consolidation/01-architecture.md),
  Part A/C) states Launcher rules — Library shows templates only; single-click
  an agent opens its home graph; single-click a node opens that agent's chat;
  double-click-to-expand is deferred. These are Flutter behaviours in
  `EterniaLauncher`, outside this repo, and were not verified here. The runtime
  half of the "home graph" resolution (`graph_id = runtime:<owner>`, one map per
  owner instance) IS verified above; the click semantics are not.
- **Neko prompt-layer ordering beyond the chat builder.** The eight-step order
  in
  [neko-persona-identity-deploy.md](archive/2026-08-22-pre-consolidation/neko-persona-identity-deploy.md)
  opens with "Hermes core" and closes with profile memory and conversation
  history. Steps 2–5 are verified above against
  `_mission_chat_surface_message`; the Hermes-core preamble and the
  memory/history tail assemble outside this builder and were not re-derived.

## Supersedes

It also replaces `planned/agent-placement-verb.md`, **deleted 2026-08-27 by the
S10 fold-in commit** (`git log --diff-filter=D --oneline -- docs/agent-runtime-harness/planned/agent-placement-verb.md`
recovers it): the entity half of that plan — the create's three phases, the
inverse, and "placed" as a JOIN rather than a merge — is the truth stated above.

Beyond that it replaces, for current truth, the entity/architecture content of
these files under `archive/2026-08-22-pre-consolidation/`:

- [01-architecture.md](archive/2026-08-22-pre-consolidation/01-architecture.md)
  — the locked entity model. Parts A/B's `Chat → Goal/Task` chain, `task_bound`
  mode, tasks-as-HUD and proof gates are history.
- [02-execution-engine.md](archive/2026-08-22-pre-consolidation/02-execution-engine.md)
  — the removed stage-graph engine. Only its profile→persona promotion
  substrate survives, via `agent_runtime/blueprints/resolve.py`.
- [08-blueprint-as-script-collapse.md](archive/2026-08-22-pre-consolidation/08-blueprint-as-script-collapse.md)
  — the root-node execution model; ancestor of "no judgment in Python", nothing
  more.
- [04-decision-hud-simplification-map.md](archive/2026-08-22-pre-consolidation/04-decision-hud-simplification-map.md)
  — its steering sections are the design origin of the kept `steered_by` edges.
- [00-index.md](archive/2026-08-22-pre-consolidation/00-index.md) — the previous
  live-truth ranking.
- [neko-persona-identity-deploy.md](archive/2026-08-22-pre-consolidation/neko-persona-identity-deploy.md)
  and [neko_SOUL_draft.md](archive/2026-08-22-pre-consolidation/neko_SOUL_draft.md)
  — persona identity ownership; the SOUL text remains a live reference copy.
- [CHARACTER_SHEET_8WAY_PLAN_2026-08-17.md](archive/2026-08-22-pre-consolidation/CHARACTER_SHEET_8WAY_PLAN_2026-08-17.md)
  — built and merged; `agent/charsheet/` is live, and the sheet contract itself
  is owned by the Launcher spec.
- [SKILL_INBOX_PROMOTION_DESIGN_2026-07-24.md](archive/2026-08-22-pre-consolidation/SKILL_INBOX_PROMOTION_DESIGN_2026-07-24.md)
  — implemented as `agent_runtime/skill_promotion.py`, which names it as its
  design authority.
- [16-mission-lane-removal.md](archive/2026-08-22-pre-consolidation/16-mission-lane-removal.md)
  — superseded only for the *summary* above. It remains the authority for the
  S0–S12 record and the operator rulings; linked, never reproduced.
