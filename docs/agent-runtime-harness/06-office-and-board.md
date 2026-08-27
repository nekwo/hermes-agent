# 06 — The office surface: how Mission Control's level writes and reads scene state

The Mission Office is the canvas where a persona placement is a desk, a drag is a
write, and a palette drop mints an agent. This doc is the CURRENT truth of that
surface: which verbs the level may call, how a write comes back as a fold instead
of a 5-second rebuild, and what the operator sees between letting go of a chip and
the runtime agreeing the actor exists. Every claim below was re-checked against
code, receipts, or the live diag log on 2026-08-22 — anything that could not be
is quarantined under `## Unverified carry-forward`, and anything not yet built
lives in `planned/`, not here. The goal/task mission lane was removed 2026-07-30
and appears nowhere on this surface.

---

## The write verbs — the level's mutations, one RPC lane

Every office mutation is one of these verbs, and all of them are registered
JSON-RPC methods on the serve child today (`agent_runtime/serve_rpc.py`,
`@method(...)`):

| Gesture | Method | Handler (all in `agent_runtime/serve_rpc.py`) | Ack |
|---|---|---|---|
| place / move | `runtime.office.upsert` | `_runtime_office_upsert` | `{actor_key, revision}` |
| delete | `runtime.office.remove` | `_runtime_office_remove` | `{actor_key, revision, state}` |
| folder taxonomy | `runtime.office.surface.update` | `_runtime_office_surface_update` | `{workspace_id, folders, revision}` |
| realm-sync resolve | `runtime.office.resolve_conflict` | `_runtime_office_resolve_conflict` | `{actor_key, take, state, revision?}` |
| place an AGENT (roster row + chat root + actor) | `runtime.agent.create` | `_runtime_agent_create` | `{persona_instance_id, actor_key, revision, position, actor, actor_fresh, skills, phases, …}` |
| retire an agent (row + every actor bound to it) | `runtime.agent.retire` | `_runtime_agent_retire` | `{persona_instance_id, archive_path, archived_actor_keys, office_archive_failures, already_retired, correlation_id?, …}` |

**Handlers are named, never `file:line`, and the reason is this table's own
history.** The retire row carried `serve_rpc.py:2055` from the day S5 landed it,
and by the time S10 read it back the function was at `:2079` — four days, one
intervening slice, and the citation went on reading as verified (invariant 13).
S8b had already had to strip that one number by hand; this change strips the
other five for the same reason rather than waiting for each to rot in turn.
`@method(...)` is the only registration site, so a symbol is grep-findable and a
line number is a guess about who edited above it.

The last two are the only ones whose write crosses BOTH stores — the roster and
the office — and they are each other's inverse. The four above them are office-only
and remain the verbs for what a placement verb cannot reach: an authored desk, a
class-keyed actor, a folder taxonomy, a realm-sync sidecar.

Three ack shapes are load-bearing and the handler docstrings say why. The remove
returns the **post**-archive revision, because `_archive_actor_locked` bumps on
the way out and an archived key carries the number forward through a restore — a
pre-archive number would hand the client a guard token already one behind. The
remove also carries a constant `state` word so a decoder cannot mistake a deletion
ack for a placement ack (`{actor_key, revision}` means the opposite thing).
The surface update echoes the folder list **as the store normalized it**, and
`folders` is a LIST on the wire: the capability lane's comma-join is an argv
artifact that splits `"Design, Ops"` into two folders, and a typed lane does not
copy an encoding's accidents. The resolve echoes the STORE's key and a normalized
`take`, because `take=remote` writes the key the peer's record carries and a
sidecar's filename may disagree with its record.

### The fences a placement write passes

`runtime.office.upsert` and `harness office actor-upsert` are two doors onto one
store method, and every guard lives in that method rather than in either door
(`OfficeStore.upsert_actor`, all of it inside `office_lock`). In order:
`_guard_class_keyed_write` (the class→instance re-key fence, hoisted out of four
callers by EG-6.6), `_guard_no_conflict` (an unresolved realm-sync sidecar),
`_guard_duplicate_desk`, then `_check_revision`. Each door keeps only a
TRANSLATION of the typed refusal into its own taxonomy — never a copy of the
predicate — so deleting a fence does not leave either door guarded.

**`_guard_duplicate_desk` is the newest, and it is a store fence for a rule that
used to be the launcher's alone.** One persona holds ONE live desk per level.
The launcher has always guarded the authoring gesture
(`MissionOfficeLayout.hasAuthoredDeskForPersona`) and warned at render time when
it found two (`MissionOfficeRenderResolver._scanDeskInvariants`), but neither
stands in front of `harness office actor-upsert` — which is exactly the door the
2026-08-24 incident authored a second `qa` desk through. The predicate
(`office_store._duplicate_desk_collision`) asks about the POST-WRITE state,
because an upsert REPLACES the target actor's items: after the write a persona
holds the desks in this payload plus the desks every OTHER live actor holds for
it. Three consequences fall out of that one sentence rather than three branches
— moving your own desk is accepted, a second actor desking the same persona is
refused, and a desk whose only holder is ARCHIVED is accepted, because
`scan_actors` reads the live directory and an archive is not a holding. Desks
are keyed on the ITEM's persona, not the actor's, matching the launcher's guard.

**The persona keying is PROVISIONAL, and D6 was ruled against it 2026-08-27.**
The operator's ruling is that duplicate desks are fine and only a duplicate on
the SAME INSTANCE is not — "it's an instantiated system", so the persona is a
template and keying a placement invariant to it is a category error. The ruling
was NOT implemented, deliberately: the same conversation established that desks
are a **placeholder for artifacts**, that they do not connect to agents yet, and
that artifacts "should be stand-alone things" — which the launcher already
half-records as an operator ruling of 2026-08-20 ("desks shouldn't be part of the
agent — they are separate things", `mission_office_layout.dart`).

Re-keying persona → instance would therefore harden a coupling the design is
moving AWAY from. If artifacts are standalone scene objects, this fence should
key on neither persona nor instance, and the duplicate-desk invariant stops
existing rather than moving. **Do not "fix" this fence toward instance keying.**
Revisit it when artifacts are actually built; until then the persona-keyed fence
is scaffolding that refuses a shape nothing yet produces. The field both sides
would need already exists (`OfficeItem.persona_instance_id`, the launcher's
nullable `personaInstanceId`), so no migration is being deferred — only a
decision that should not be taken before the feature it guards exists.
Like the class-key fence it refuses rather than answering from a directory it
could only partly read (`ActorsUnreadable`), and it fires on `--dry-run`.

**A desk's identity is its `item_id`, and the count is of DISTINCT ids** — the
same narrowing `office_class_key_guard` records for its own predicate, and for
the same reason. One desk claimed by two actor rows is a duplicate PLACEMENT
(`duplicate_item_placement`), a different fault with a different cure, and it is
a state the class→instance re-key migration deliberately passes through:
`scripts/office_actor_rekey_to_instance.py::_apply` mints the instance-keyed
actor with the class-keyed actor's items **copied verbatim** and only then
archives the old key, so both rows briefly claim the same desk. Counting rows
instead of ids would refuse the one operator script whose whole job is to move a
placement, while catching nothing the fence is for — what it is for is a SECOND
desk, a different id, which is what the incident authored and what the
launcher's detector counts. Pinned both ways:
`test_office_store.py::test_one_desk_claimed_by_two_rows_is_not_two_desks` and
`::test_a_second_actor_desking_one_persona_is_refused_naming_the_holder` are a
boundary, not one assertion twice.

**Known residual, stated rather than implied.** Between the two fences, an
INSTANCE-keyed write that claims a desk id another live actor already holds
passes both: the class-key fence guards only class-keyed payloads (an
instance-keyed write "IS the migration's shape"), and this one counts distinct
ids. That is the migration's transient made permanent if nobody finishes the
migration, and the launcher's render-time `duplicate_desk` warning is what sees
it. Nothing reads it today. `placement_census` (shipped in `bfdaf735c3`, D8) is not
that reader: it joins the two stores on `persona_instance_id` and never opens
`actor.items`, so two live actors holding one desk id are both counted as
`placed` and the section reports `ok`. Closing it needs either an items-aware
census row or a third predicate; until one exists the launcher's render-time
`duplicate_desk` warning is the only detector.

It has **no override on either lane**, which is the deliberate asymmetry with
`--allow-class-key`: that flag exists because an operator can legitimately want
the pre-migration shape back, whereas the render layer draws the implicit desk
only while a persona has no authored one — so a second authored desk is not a
placement anyone can mean, it is two desks one of which is unreachable. The way
past it is to move or remove the desk already there, and the refusal names it.

Refusal wire: `ERR_CONFLICT` 4090, `data.reason = "duplicate_desk"`, with
`data.persona_id`, `data.holding_actor_key`, `data.holding_item_id`; CLI exit
code `duplicate_desk` (family 4, beside `duplicate_conflict`). **Realm pull is
deliberately outside the fence** — `office_sync.apply_office_pull` writes actor
files directly and never reaches `upsert_actor`, so a workspace pulled from a
peer can still arrive holding two desks for one persona. That is the correct
boundary (a pulled duplicate is a conflict-lane fact about what a peer
published, not a local write), and it is why the launcher's render-time
`duplicate_desk` warning stays: it is the only thing that can see data
predating or bypassing this fence. The placement verb authors no desk at all
(`agent_create.placement_actor_payload` writes one `kind: "agent"` item), so no
`agent create` and no canvas drop pays for the fence's directory scan — pinned
by `test_agent_create_service.py::test_verb_authors_no_desk`.

Launcher side, all four are RPC-first through one writer
(`office/mission_office_rpc_writer.dart`: `upsertActor:76`, `removeActor:167`,
`updateSurface:257`, `resolveConflict:352`), each gated per-method on the serve
manifest. The argv capability lane survives on exactly one arm — `Unavailable` —
and a **refusal is terminal and never falls back**
(`mission_office_layout_controller.dart:351`). The per-flush receipt is
`kMissionOfficeWriteLaneReceiptLabel = 'write lane:'` (`:434`), and the live log
reads `[MissionOfficeWrite] ws_codex-test-workspace_28d285 write lane: 1 rpc, 0 cli`
on every 2026-08-22 flush — in the ISO-stamped log era the argv arms are fielded
and unexercised (the undated era carries 4 `0 rpc, 5 cli (fallback: laneAbsent)`
flushes; see the `laneAbsent` carry-forward), which is the evidence R#42's
deletion criterion asks for.

The layout mutation path itself mints one correlation id per gesture
(`office/mission_office_correlation.dart`), stamped **before** the await, so a
call whose reply never lands is still nameable. That token exists because on
2026-08-16 a timestamp inference read "deletes take 3.8 s" for writes that took
280–368 ms — the launcher's flush receipt lags the RPC by 250–650 ms — and a
wrong prioritisation followed from it.

### The placement verb — where an unaimed create lands, and what it hands back

`runtime.agent.create` and `harness agent create` are two doors onto
`agent_create.perform_agent_create`, the one function that writes the roster row,
the durable chat root and the office actor together. Since plan S2 its
`position` is **optional on both doors** (`--pos` is no longer `required`), and
its ack returns what was written.

**Absent means "I did not aim", and hermes answers it.** With no `position`, the
service resolves the slot through `agent_runtime/office_layout_policy.py` — a
deterministic lattice scan over the workspace's live actors in the request's
folder, returning the first free slot. **That scan starts at the WORLD ORIGIN
and climbs** (operator ruling 2026-08-27, D10(iii)): the first unaimed placement
lands on `(0, 0)`, and a blocked slot sends the next one one full grid step UP
(`ROW_SPACING`), not one step across. It wraps sideways to the next column only
after `ROWS_PER_COLUMN` (8) are stacked. The old lattice began at `(-5.0, 6.4)`
— the canvas's own unaimed-drop band — and filled left to right, which walked a
run of placements off the side of the floor instead of keeping them where the
operator was looking. With a `position`, it is written
verbatim, exactly as it always was; a malformed one (a one-element list, a
string, a bool pair, an infinity) still refuses `position_invalid`, because a
transport that mangled an aim is not the same thing as an operator who had none.
An explicit JSON `null` is read as ABSENT, deliberately: a client spelling "no
opinion" as `null` means what one omitting the key means, and refusing one while
accepting the other would make the wire's meaning depend on a serializer's
omit-none setting.

**The policy is hermes', and the launcher's copy is a prediction.** The launcher
needs a world position for the pending chip and the staged scene node before the
ack lands, so `MissionOfficePlacementPolicy` stays — with the same seven
constants and the same scan order. Two policies that can disagree are the defect
D2 exists for, so the agreement is pinned by a case file committed
byte-identical in both repos (`tests/fixtures/office_layout/cases.json` and the
launcher's `test/fixtures/harness_office_layout/cases.json`, each under a
`MANIFEST.sha256` its own side hashes). Read that directory's README before
touching any constant: it lands in both repos or in neither. Two cases exist
only because the repos are asymmetric — `hidden` is launcher-only view state
this store has never had, and a blank `folder` is hermes-only (`_normalize_item`
persists `""` where the launcher's decoder substitutes the kind's default), so
the policy resolves that fallback when it scans.

**The verb authors no desk**, so the lane it scans is always the AGENT lattice
whatever the folder is called; the desk lane's diagonal nudge exists to keep
unaimed desks off the agent lattice and there are no unaimed desks on this lane.

**Where the policy READ sits, and the window that leaves.** Outside
`office_lock`, immediately before `OfficeStore.upsert_actor`, which takes that
lock itself. That is forced rather than preferred: `locks._file_lock` is a real
file lock (`msvcrt.locking` / `flock`) acquired through a fresh handle, so it is
**not reentrant** (on Windows the second acquisition retries against a deadline
and refuses `HarnessLockUnavailable` after the configured 15 s — measured; on
POSIX `_file_lock` takes a bare blocking `fcntl.flock(…, LOCK_EX)` that never
reads the deadline, so it blocks indefinitely and that refusal is unreachable —
either way the read cannot live inside the lock) — wrapping the read and the
write in one `office_lock` would
deadlock the write it is meant to protect, and moving the policy INSIDE
`upsert_actor` is the honest close and a store change S2 does not carry. The
window: two creates that both omit a position and race between the read and
their writes can compute the same slot, and the second lands on top of the
first. Bounded to one slot, visible on the canvas, fixed by a drag — and no
worse than the prediction the launcher has always sent, which reads a snapshot
already a round trip stale. `resolve_placement_position`'s docstring is the
long form; the row is filed in the launcher's Mission Control queue.

**The ack gains `position` and `actor`, both additive.** `position` is what was
written — policy or verbatim — so a client that sent none learns where its agent
went without a second read. `actor` is the row **as stored**, in the same item
shape `runtime.office.get` renders: `office_models.office_actor_wire_row`, which
that method's own projection now flattens through, so the two cannot drift into
disagreement. It is taken off the store's return value and never rebuilt from
the request, which is the whole point — the client stops trusting its predicted
key, position and revision and adopts the server's.
`RPC_CONTRACT_VERSION` does not move and no name joins the manifest's `methods`
list; an old client ignores both keys, and an old serve still works for a client
that always sends a position.

**The verb assigns skills, and refuses to hand an agent a stale copy** (plan
S4). `skills: [id, ...]` on the RPC and `--skill <id>` (repeatable) on the CLI
are byte-parallel doors onto one phase that runs AFTER the placement. Absent,
the new instance's `skill_overrides` stays `None` and it inherits its persona's
skills live; a list — including an empty one, which is an explicit "override
with nothing" — is written at the INSTANCE tier and never at the persona
template. The mechanism, the gate order and why the hash check is a gate here
and a report everywhere else are in
[05 — chat turn lane §9](05-chat-turn-lane.md#9-the-agent-create-path); what
belongs to the office is the consequence for the placement.

**A skills refusal never costs the agent its desk.** The reservation vocabulary
gains `placed` between `instance_minted` and `done`: both writes landed, the
skills phase is owed. Every skills refusal carries `phase: "skills"` and
`rolled_back: false` — the literal truth, not a hedge — plus the
`persona_instance_id` of the agent that IS standing, and `next_expected` names
the cure: retry with the **SAME** `idempotency_key`, which re-enters at the
skills phase alone and neither re-mints the roster row nor bumps the actor's
revision. A new key would mint a second agent beside the first. The retry takes
the CURRENT request's skill list, not the receipt's, so an operator who mistyped
an id fixes it and retries rather than being answered with the old typo forever.

The migration is one added state and three rules: a `done` receipt with **no**
`skills` field is pre-plan and is never re-entered (its skills cannot have been
requested, so there is nothing to resume); a `placed` receipt carries the
normalised request list; and an unknown state stays `reservation_corrupt`, so an
OLD serve reading a NEW `placed` receipt refuses loudly rather than re-minting —
the safe direction for a downgrade.

**Two new refusal reasons ride the wire, and two more are shape refusals.**
`skill_unresolved` (`ERR_INVALID_PARAMS`, with `data.skill` and `data.status` in
`missing | collision | invalid_source`) and `skill_install_diverged`
(`ERR_HANDLER_FAILED`, with `data.source_hash` and `data.installed_hash`, both
explicitly null when a copy fault stopped the install before either could be
established) are the phase's own. Beside them: `skills_invalid` refuses a
malformed `skills` param before any write, stamped `rolled_back: true` like
every other `AgentCreateInvalid` arm, and `skill_assign_failed`
(`ERR_HANDLER_FAILED`, phase `skills`) is the store fault that would otherwise
have escaped as an untyped `-32000` with no `data` at all. The plan named the
first two; the second two are named here because an unnamed fault renders as a
`handlerRaised` the operator cannot act on. **Launcher note for S7:** it reads
`data.persona_instance_id` off any refusal with `rolled_back != true` and
publishes it as `orphanInstanceId` — a skills-phase instance is NOT an orphan,
so that decoder must branch on `phase == "skills"`. No live gesture reaches it
today, because the launcher sends no `skills` yet.

**The ack gains `skills` and `phases.skills_ms`,** both additive and both always
present: `skills.assigned` is read back off the row (never echoed from the
request) and `skills.installed` lists `{skill, changed, installed_hash}` per
canonical id, so a create that installed nothing because the copy was already
hash-equal says so. `skills_ms` is what makes the cold-machine `copytree` billed
rather than hidden, and `total_ms` is re-stamped after the phase so it is not
short by exactly that cost.

### The inverse — one call takes an agent off the level, and says what left

`runtime.agent.retire` / `harness agent retire <persona_instance_id>` are two
doors onto `agent_retire.perform_agent_retire`, and `harness persona instance
retire` is a third onto the SAME function (its own envelope preserved, its ack
now identical). All of them wrap `PersonaInstanceStore.retire`, which has always
archived both halves: the roster row into `persona_instances_archive/<ts>_retire/`,
and every office actor bound to the instance through
`OfficeStore.archive_actors_for_instance`.

**What S5 added is not the archive — it is the door and the receipt.** Before it,
the launcher removed a deliberate placement through two unjoined lanes (a
`persona.instance.retire` argv capability AND a `runtime.office.remove`), so a
half-state — actor archived with the row still live, or the reverse — was
representable and nothing detected it. And the office half was best-effort AND
silent: its outcome was discarded inside `_archive_office_placements`, so "the
desk is still on the canvas after the retire said it was gone" was a fact no
caller could be told.

Now the store's prune returns per-actor outcomes and the ack carries them:
`archived_actor_keys` names every actor that left, `office_archive_failures`
names every one that did not (`{actor_key, workspace_id, error}`; a fault in the
office projection ITSELF carries `actor_key: null`, because it is not one actor's).
An EMPTY failures list is the positive claim that every bound actor is off the
level. The roster archive stays authoritative either way — a locked desk file
must never make a placement un-retirable — so a failure is a report, never a
refusal.

**The gesture token rides this verb too (S8b).** `correlation_id` is optional,
normalised exactly as `agent create`'s is (`safe_assignment_text(limit=200)`),
and it is threaded `perform_agent_retire` → `PersonaInstanceStore.retire` →
`_archive_office_placements` → `OfficeStore.archive_actors_for_instance` →
`remove_actor`, so the `office.actor.removed` event AND the `state.patched`
remove row carry it — then echoed on the ack (present only when sent, so a call
without one is byte-identical to before the key existed). Until S8b this was the
ONLY level-mutating verb with no token, which meant one operator gesture's
create half and delete half lived in two correlation spaces that no single grep
joined. **Both argv doors publish the flag**: `harness agent retire
--correlation-id <token>` since S8b, and `harness persona instance retire
--correlation-id <token>` since S8b-b (2026-08-27). S8b withheld it from the
second on the reasoning that "no gesture behind it" was the truth for that door.
It was not: the launcher's `persona.instance.retire` argv capability IS that
door, fired from `MissionOfficeLayoutController.retireAgent`'s `Unavailable` arm,
and that method takes `correlationId` as a REQUIRED parameter. So the token
existed on every launcher retire and was dropped by exactly the arm that runs
when the RPC lane is degraded — the lane on which a grep over the event log is
the only join an operator has left.

**Refusals are `PersonaInstanceRetireError`'s codes one-to-one**: `not_found` →
`ERR_NOT_FOUND` (4001); `canonical_persona_channel` / `instance_active` /
`assignment_active` / `assignments_unknowable` → `ERR_CONFLICT` (4090) with
`data.reason` carrying the code verbatim, because the launcher decodes
`data.reason` first and the numeric code second.

**Retiring the same id twice is an ANSWER, not an error.** The second call
replays the ack with `already_retired: true` — the same `archive_path`, and the
same `archived_actor_keys`, re-read from the office archive rather than
reconstructed — because a remote client that lost the first ack must be able to
ask again. An id that never existed still refuses `not_found`: the replay reads a
TOMBSTONE, and absence alone is not one.

Adding the name grew the manifest's method SET without moving
`RPC_CONTRACT_VERSION`, and that presence is also the launcher's D12 rollout
marker for "this serve accepts an absent `position`" — see
[03 §2](03-transport-and-wire.md#2-capability-advertisement--rpc-and-ops) for
why a set-plus-integer manifest makes that legal, and the launcher's
`EterniaLauncher/docs/mission_control/04-office-scene.md` for what it gates over
there.

**Authorization has an enforcement point, and the policy behind it is empty on
purpose.** Both methods declare `console` on the wire (`rpc.tiers`, see
[03 §2](03-transport-and-wire.md#2-capability-advertisement--rpc-and-ops)) and
`serve_rpc.handle_request` evaluates that declaration against what the TRANSPORT
proved before any handler runs — the stdio owner, or a socket peer that passed
the HMAC. Both are allowed every tier today, so nothing an existing caller
observes moved; a caller kind nothing yet mints is refused with a typed
`data.reason: "scope_denied"`. The CLI mirrors it with a `local_console`
identity minted in `_agent_retire_outcome`, which is the one retire both CLI
doors reach.

**What that fixed, and what it deliberately did not.** The asymmetry this
section used to record — `harness persona instance retire` consults a gate and
`harness agent retire` does not, on the same `perform_agent_retire` — is gone,
and the 2026-08-27 survey found it had been the smaller half of the problem:
the consulted gate never ran on real traffic either, because
`_coordinator_actor_id` recognises only `--requested-by coordinator[:id]` while
the CLI defaults to `cli` and the launcher hardcodes `launcher`. The fix was not
to give the second door that gate. `authorize_coordinator_action` was renamed
`review_coordinator_budget` because it is a coordinator persona declaring its
own budget so the runtime can ask a HUMAN to confirm — it escalates and never
denies, and every input it reads is one the caller supplied. The service
functions still take no scope parameter: this doc's earlier "the fix is a scope
parameter on the two service functions" is superseded, and that parameter is the
optional non-bypassable BACKSTOP (plan Stage A6), not the gate. Per-device
scopes are the gateway's Stage 1 / R11, and they are now a policy edit to one
predicate rather than an architecture change.
Receipts: `8d69f8858b` (A1), `4d60060dc3` (A2), `dba7ed19b6` (A3),
`290f6f461b` (A4); launcher `2cf887b47`.

### What a remote connector inherits (the gateway check, folded from plan §A.11)

The placement verb was designed against the unbuilt Hermes Gateway
(`hermes-agent/docs/agent-runtime-harness/planned/remote-gateway.md`, launcher
`EterniaLauncher/docs/mission_control/planned/universal-remote-gateway.md`) so that
no part of
it would have to be redesigned when a remote connector arrives. The obligations
below are stated as what the SHIPPED verb already is; the gateway itself is not
built and nothing here describes it as if it were.

| Gateway surface | What the shipped verb gives it |
|---|---|
| `call` — a remote device cannot run the install's CLI | `runtime.agent.create` and `runtime.agent.retire` are the whole wire; the two `harness agent …` verbs are argv twins of the same service functions, and `skills` rides the RPC params rather than being a CLI-only flag |
| Additive-only wire (manifest = set + integer) | one new method name joined `methods`; new params are optional; new ack keys are additive; `RPC_CONTRACT_VERSION` never moved; observability is `phases.skills_ms` in the ack and log receipts, never a new key on the parity envelope |
| Exactly-once over a lossy link | the create's idempotency-key reservation already replays its ack (`idempotent_replay: true`) and the retire answers `already_retired: true` — a per-install client outbox can carry both verbs with no runtime-side addition |
| Per-device scopes | both are level mutations and declare the `console` tier, **checked at the front door before the handler runs** (see above). What is still absent is a device whose credential carries a tier — that is gateway Stage 1 / A5, and it is a policy edit to one predicate. If an `admin` tier ever carves out the skills INSTALL sub-phase, the phase boundary is where it sits, so it is still one predicate |
| Peer tier (an agent on install A addressing install B) | deliberately excluded: agents never mint or retire agents on another install; a remote OPERATOR does |
| `subscribe` | a placement is noticed through the fold, not a poll — one `patch` batch carrying the `persona_instance` and `office_actor` creates, pinned by `patch_agent_create.json` in both repos |

**The one hazard, and it is not this verb's to fix.**
`patch_coverage.accepted_fold_entities` is the INTERSECTION across every
subscriber in the room (03 §4). A deliberately narrow mobile fold profile — the
office canvas is out of scope on a phone — therefore drops
`office_actor`/`office_actor_lifecycle` from the room's accepted set the moment
one such client subscribes, and every placement demotes to a full core for the
desktop too. Correctness holds; the drop-latency numbers below do not. The
answer is per-subscriber promotion at the hub, which is a hub change owned by
the gateway plan's R10, and the demote arm is already pinned here by the second
golden `delta_agent_create_narrow_profile.json` so nobody discovers it on a
phone.

## The fold model — what a fold is, and who promotes a batch

> The generic patch-frame contract and the office push lane's re-envelope live in
> [03 — Transport and wire](03-transport-and-wire.md) §4 and §6. This section is
> the office's own half: the `office_surface` entity, the two-lane fence, and what
> the level declares.

A hermes write emits a domain event AND, inside the same lock, a `state.patched`
row. When every event in a coalesced batch is *coverable*, the batch ships as a
patch frame the launcher merges into its held core; when even one event is not,
the whole batch demotes to a full core — a `build_snapshot()` the client waits
on. `patch_coverage.py` is conservative by construction: one uncovered event
demotes everything (`patch_coverage.py:25-40`).

Promotion is **negotiated per client** by capability tokens, not decided by the
server alone. The office surface's own fold is `office_surface`
(`state_patches.py:1019`, `emit_office_surface_patch` at `:1089`), gated on
`OFFICE_SURFACE_FOLD_CAPABILITY = "office_surface_fold"`
(`patch_coverage.py:179`), which is what lets `office.surface.updated` join the
covered set (`patch_coverage.py:331`). It is a **subset merge** of
`{folders, revision, updated_at}`, not a row replace, because the office row also
carries actor lists, counts and ledger keys this write does not move. The launcher
declares both strings in one authority list — `kMissionFoldDeclaredEntities`
(`data/mission_read_model.dart:161`) — used verbatim by the argv stream child and
the `runtime.office.subscribe` request, so the two lanes cannot drift.

Where a surface write is genuinely unfoldable, hermes emits an accounted
**refresh** instead of pretending: `emit_office_surface_refresh`
(`state_patches.py:1156`) exists because archiving an orphaned surface removes the
office row and every actor under it in one move, and a covered event with no patch
beside it would ship an EMPTY patch list — advancing the client's watermark having
folded nothing, keeping the archived surface and its chip forever.

### The fold fence, and why it stopped firing

Two subscribers carry the same rows into one `MissionReadModel` — the NDJSON
`harness stream` child and the `runtime.office.subscribe` push lane — on purpose,
with the stale gate as the intended dedup. The gate ran at `prepareFold` while the
fold was prepare → off-isolate re-projection → commit, so two deliveries inside
one window both passed against the same base: a check-then-act race whose outcomes
were a fenced resubscribe, or the same batch committed twice.

Both halves are fixed and both fixes are live. `_foldChain`
(`data/mission_control_bridge.dart:2420`, `_enqueueFold` `:2429`) serializes every
fold from both lanes, so the loser stale-drops at prepare having paid no
projection; `MissionReadModel` records `coreRevisionWriter` (`:430`) at each of
its bump sites so a surviving fence NAMES the writer that moved the base
(`mission_control_bridge.dart:2459`, `:2650`) instead of leaving it to adjacency
inference. Live evidence, 2026-08-22 diag log: **zero** `fold:fenced`, **zero**
`REFUSED fenced`, **zero** `push:full_core` resubscribes in the ISO-timestamped
era; the only residual causes are `fold:no_base` (6) and `fold:gap` (2).

`fold:no_base` is itself a fix, not a defect. A full-core apply that supplies no
raw core now NULLS the retained fold base (`mission_read_model.dart:563-577`), so
the next patch takes the typed `patch_without_base` refusal → `no_base` resync
(`:1309`) → a forced hydrate on the one lane that provably supplies a core. The
alternative — folding onto a stale base and publishing it as truth — was
silently discarding whatever the poll's core carried, invisibly to the fence.
The rejected repair is recorded too: threading a raw core through the CLI poll
would make a fallback lane MORE capable, against rulings #42/#60.

The subscribe carries an optional `reason` so the server log can join the client's
resubscribe ladder — boundary-validated (≤64 chars, `[a-z0-9_:.-]`, refused
`-32602 reason_invalid` before any store, hub or producer call) and stamped on the
receipt, with absence printed as `-`. The docstring states the rule out loud: *a
cause the client chose is evidence, never authority* — a server that branched on
it would be taking dispatch orders from an untrusted string
(`serve_rpc.py:725-740`).

## Optimistic rendering vs snapshot truth

The page used to hold a whole-layout override, and on 2026-08-15 a predicate
redefined in a different file for a correct and unrelated reason caused the paint
path to discard that override on every frame of a drag — the office's optimistic
paint died for every gesture at once, with no test to say so.

That class is now unrepresentable. The office runs an **intent ledger**
(`office/mission_office_intent.dart`), keyed by surface id, and the paint path
reads the layout provider UNCONDITIONALLY and exactly once, then applies
`withPendingIntents(surfaceId:, server:)` on top
(`mission_control_page.dart:2881-2894`). `_officeLayoutOverride`,
`_hasPendingOfficeSave`, the override branch and the paint-path `isSettled` caller
are deleted, each with a row in `mission_control_tombstone_registry_test.dart`
(`:2980`, `:2995`) so they cannot come back. The ledger is keyed by SURFACE rather
than workspace deliberately: not every surface is harness-backed, and a ledger
hanging off `_WorkspaceSync` would have left SharedPreferences-backed surfaces
flickering on every gesture.

The ledger deliberately drops two of the five fields UP-1 sketched. `move` vs
`create` is not a kind — the distinction is `knownToServer`, which already lives
in `serverKeys`; and `predictedRevision` is not on the record, because the
revision authority sits at SEND time and a number stamped at STAGE time would be
the staler of two answers to one question.

**A drag is now one write, not hundreds.** `_handlePanUpdate` no longer calls
`onMoveSceneItem`; it records the commit position and asks the game to echo the
node at the cursor with no write at all
(`office/mission_office_mount.dart:362-373`, `office/mission_office_game.dart:284-305`).
`_handlePanEnd` emits the move and the commit **together or not at all** — firing
the commit alone would ask the write lane to flush somebody else's staged edit
early, which a camera pan or an unmoved node press would otherwise do (`:374-391`).
The `'Moved '` display-string gate that used to route the debounce — control flow
on operator-facing text — is deleted (`mission_control_page.dart:3713`).

**Snapshot truth is joined separately, and the type is the guard.** The
`roster_confirmed` mark reads `MissionControlSnapshot.offices[].actors[].actorKey`
— the producer's own folded state — never the page's overlaid layout
(`mission_control_page.dart:2310-2340`). Joining on the overlaid layout would find
the actor in the very turn that placed it and report near-zero on every drop
forever. The parameter TYPE is a `MissionControlSnapshot`, so rewiring it to the
overlay does not type-check.

**The drop pipeline** is instrumented end to end by `MissionDropTimeline`
(`data/mission_drop_timeline.dart`, 954 lines) with five phases —
`drop_started` (`:76`), `layout_mutated` (`:83`), `rpc_settled` (`:87`),
`roster_confirmed` (`:118`), `first_paint` (`:126`) — emitted as one
`[MissionDropTiming]` line at settle. Four honesty rules are enforced by the file:
an unresolved phase is ABSENT, never a fake `0`; marks come off a monotonic
`Stopwatch`; first mark wins within a drop; and the line ships in RELEASE, unlike
the chat timings that sit inside `assert` and compiled out of the 2026-08-09
analysis entirely. `rpc_instance_ms` is hermes' own `phases.instance_ms` ECHOED,
never derived by subtracting a launcher stamp from a hermes stamp — the diag log
is local-with-no-dates and hermes records are UTC, and that boundary produced two
misreads in one day.

**The pending-chip lane** (`office/mission_drop_pending_chips.dart`,
`office/mission_drop_pending_chip_layer.dart`) draws one chip per in-flight drop,
anchored to the placement's WORLD position and pushed through the scene's own
camera projector, because office actors are Flame components with no render box.
It says *placed, waiting for the runtime to confirm* and it may **never** say
"ready" — readiness is the roster's verdict, rendered where the roster is. With
no projector it degrades to a corner rail that still names the persona: "no
anchor" degrades to "wrong place", never to "gone".

## Drop latency — current numbers

The receipt's field list is catalogued in
[07 — Observability](07-observability.md), and the headline numbers are carried in
[08 — Performance and debt](08-performance-and-debt-ledger.md); this table is the
office's own read of them, boot by boot. From the live diag log
(`%TEMP%\eternia_launcher_diag.log`), one boot per row:

| Boot | Drop | `layout_mutate_ms` | `rpc_ms` | `rpc_instance_ms` | `roster_confirmed_ms` |
|---|---|---|---|---|---|
| 14:50Z | drop-1 (cold) | 19 | 2086 | 2030 | 8309 |
| 14:50Z | drop-2 (warm) | 16 | 121 | 78 | 4506 |
| 17:43Z | drop-1 (cold) | 20 | 1093 | 1046 | 4645 |
| 17:43Z | drop-2 | 15 | 360 | 78 | 841 |
| 17:43Z | drop-3 | 13 | 224 | 186 | 506 |

The node lands in ~20 ms every time; what the operator felt was the roster window.
The 14:50Z `roster_confirmed=8309` decomposed server-side into THREE full demote
builds at the SAME offset; the 17:43Z row is the same gesture after same-offset
demote reuse landed, and the tail is down by ~44%.

**The cold-create attribution is closed.** The 14:50Z open row said the 2,030 ms
first create was not a prewarm miss and needed one live drop on the new build to
name its owner. That drop happened. `agent_create_phases persona=qa
instance_ms=1046 phases=… create_patch_ms:984, chat_lane_scope_ms:859,
tool_visibility_ms:125 …` (serve `agent.log`, 13:43:53 local = the 17:43Z drop-1):
the cold cost is the **chat-lane tool-scope application**, 859 of 1,046 ms, and
the warm drop-2 pays `chat_lane_scope_ms:0` for a 78 ms create. `sprite_source=absent`
on every row — the `qa` persona binds no sheet, so `first_paint` is correctly
absent and the sprite lane owns none of this.

## HUD chips — what the investigation concluded, what shipped

Three header chips read from one parity envelope. The 2026-08-17 investigation's
verdict was that two were true alarms caused by *that week's own tooling*, and the
third was an observability artifact over a real 24 s window.

- **`projection drops N`** counted the residue of the operator's own first-class
  retires as anomalies. Fixed at the emission site: a session whose binding
  resolves in the persona-instance archive now drops as `instance_retired` with
  `by_design=True` (`persona_chat_history.py:401`), so the count means *lost*
  data. The chip also gained the disclosure the other two had —
  `anomalousDropSummaries` (`data/mission_control_snapshot.dart:345`, parsed
  `:263`, passed to the alert `:1070`).
- **`parity warnings N`** was a live-store pollution alarm with a smoking gun: a
  test called `monkeypatch.undo()` mid-body, which unwound the package's autouse
  root pin and let the next line write the operator's real store. Both halves
  landed — a structural gate (`tests/agent_runtime/test_no_midtest_monkeypatch_undo.py`)
  and a teardown tripwire, hoisted 2026-08-18 to `tests/conftest.py:545`
  (`_shared_monkeypatch_pin_tripwire`; the incident narrative stays in
  `tests/agent_runtime/conftest.py`'s docstring) — plus a
  first-class `archive_orphaned_surface` verb so the operator can clear the
  leaked surface without hand surgery.
- **`snapshot build Nms`** was ONE build wearing three log lines: the line is
  emitted per *hydrate caller* and measures that caller's WAIT. The vocabulary
  now says so — `led` / `rode` / `shared_next` (`snapshot.py:283-285`) plus a
  fourth, `cache`, added when the persisted-core fingerprint hit — printed on
  `snapshot_build_core role=… caller=… generation=… build_ms=… offset=…`
  (`snapshot.py:403`). **A boot's build count is the count of `led` lines, never
  the count of lines.** The provider prewarm was moved behind the read-model
  build on one thread (`serve.py:3290-3300`, injected rather than hardcoded) so
  its SDK import stops contending with the boot-critical build.

## The agent console — the dead lane and the limits chip

The 2026-08-16 investigation found the two defects did not share a cause but
shared an unnamed aggravator: **the console and the operator's shell interrogate
two different Hermes homes**, so `hermes auth list` in a shell was never a
diagnostic for the console. It also *corrected* the proposed mechanism — a failing
lane is NOT filtered out of the catalog; its 91 models load and the lane renders
with its verbatim 401 under "Not connected". What was genuinely silent was the
search gesture, the all-lanes-down collapse, and home provenance.

All four surfacings shipped: `searchMissionAgentUnavailableModels`
(`mission_agent_model_switcher_view_model.dart:726`, rendered
`agent_model_menu.dart:467`) so typing a model name on a dead lane no longer
renders copy identical to a typo; the `catalogUnavailable` collapse carries the
lane reasons (`agent_chat/mission_agent_model_switcher_view_model.dart:497-505`);
`probedHomeCaption` puts the probed home on screen
(`agent_model_menu.dart:235`); and the swallowed 401 became a stated status —
`usage {phase} failed (HTTP {status} — re-auth may be required)`
(`hermes_cli/harness.py:3840-3843`), with class-name-only discipline preserved for
everything that is not an HTTP status, because a bare status code leaks nothing.

## The board — still capability-only

The board is the office's sibling surface and it has NOT made the same journey.
All six board writes go out as argv capabilities — `board.card.add`, `.move`,
`.edit`, `.archive`, `.restore`, `board.resolve_conflict`
(`board/mission_board_write.dart:133-219`) — with no `runtime.board.*` RPC method
registered in `serve_rpc.py`. Board writes are named in the uncovered list
(`patch_coverage.py:33`), so a board batch demotes to a full core by design.
One asymmetry worth knowing: the board DOES send `expect_revision`
(`mission_board_write.dart:166,192`, sourced from `card.revision` at
`mission_board_card_panel.dart:233`), while the office's argv arms deliberately
omit it — the office's revision guard lives on the RPC lane only.

## Invariants

1. **A refusal is terminal; only `Unavailable` falls back.** The argv lane is not
   a second chance at a guard the store already refused.
2. **The canvas renders `server state + pending intents`.** A gesture is visible
   because it is in the ledger, never because a caller remembered to poke an
   override. There is no retire decision on the paint path to get wrong.
3. **The paint path always reads.** A predicate that suppressed the only read
   which could clear it shut the office lane for a whole process once
   (`mission_office_lane_reattach_test.dart`); a path that always reads cannot.
4. **An unresolved phase is absent, never zero.** `sprite_ms=0` would claim the
   sheet arrived instantly, which is the opposite of what happened.
5. **Never cross the wire with a clock.** Server-measured spans are echoed, never
   derived from a launcher stamp minus a hermes stamp.
6. **A client's declared cause is evidence, never authority.** No server dispatch
   branches on a `reason` string.
7. **One uncovered event demotes the whole batch**, and a covered event with no
   patch beside it is data loss — hence the accounted `refresh`.
8. **A boot's build count is the count of `led` lines.**
9. **Fold coverage is negotiated per client by capability token**, so an
   undeclared client keeps today's full cores and never receives a patch it
   cannot fold.
10. **A staged office change is never silently dropped.** A terminal hold keeps
    the intent alive, bills what it masks (`hold: N staged, lane=terminal`), and
    waits for delivery or a loud refusal with the repair surface on screen.
11. **The office fences pass byte-unchanged or the stage stops.**
    `mission_office_optimistic_paint_test.dart`,
    `mission_office_lane_reattach_test.dart`,
    `mission_office_mass_archive_incident_repro_test.dart` — an edit to any of
    them is a stage-stopping event, not a test update.
12. **One persona, one live desk per level, refused at the STORE.** The
    launcher's gesture guard and render warning are the client's half; the
    fence that a raw `actor-upsert` cannot walk around is
    `OfficeStore._guard_duplicate_desk`. Realm pull is outside it by design.
13. **Dead-symbol claims are repo-scoped or they are nothing.** A file-scoped grep
    answers "is it used here", not "is it dead"; and a `file:line` citation goes
    on reading as verified long after the code at it has moved.
14. **One create writes both stores or neither — and the skills phase is
    deliberately OUTSIDE that join.** The reservation compensates a failed
    placement by retiring the row; it never retires a PLACED agent to undo a
    file copy, so a skills refusal stamps `rolled_back: false` and names the
    agent that is standing.
15. **A placement is noticed through the fold, never through a counter.** The
    office surface's `revision` does not move on an actor write and must not be
    made to: bumping it would turn every placement into a `stale_revision`
    hazard for a concurrent folder edit, which is the guard that counter exists
    for. Do not re-propose it as "so clients notice".

## Unverified carry-forward

- **The `~4.3 s laneAbsent` window on page open.** The degrade path is live in
  code (`office/mission_office_rpc.dart:153,640,720`) and the plans of 2026-08-16
  size the window at ~4.3 s, but the current diag log's ISO-timestamped era
  contains **zero** `laneAbsent` lines, so the number is neither confirmed nor
  refuted today. Source: `OFFICE_WRITE_VERBS_RPC_PLAN_2026-08-16.md` §4.
- **The page-open write storm ("item 9").** Named unowned by three separate
  2026-08-16 plans; not re-derived from a live boot this pass. Source:
  `OFFICE_WRITE_VERBS_RPC_PLAN_2026-08-16.md` §4 / §5 D-W4.
- ~~**`office.actor.restore` is fully dead.**~~ VERIFIED AND EXECUTED
  2026-08-22: the launcher delete audit re-proved zero submit sites plus
  `localOnly` exposure, and the shell (registry row + argv lowering) was
  deleted in launcher `e38bb108c` with w23 tombstone rows in `379e70d5c`. The
  hermes CLI verb `actor-restore` deliberately survives pending the launcher
  docket's ruling 2 (kept product feature vs deleted with its store arm).
- **The office controller's incident-repro test exercises the argv fallback, not
  the RPC path**, because it never overrides `missionOfficeRpcWriterProvider` —
  so a regression making the RPC arm always `Unavailable` would leave it green.
  Flagged 2026-08-17; the override list was not re-audited this pass. Source:
  `SCOUT_LAUNCHER_LANE_MAP_2026-08-17.md` §4.

## Open rows

- **The placement verb SHIPPED end to end** (S0–S9, both repos, 2026-08-26/27)
  and its plan file was deleted by the commit that folded it into this doc,
  01, 03, 05 and 07. Nothing about it is planned any more; the landing record —
  slice, sha, review verdict — is the launcher's
  `EterniaLauncher/Launcher_Brain/20 — Active Initiatives/agent-placement-verb-handoff.md`,
  and every escalation it raised is a row in that brain's
  `mission-control-queue.md`. What it left open here:
  - **Authorization is at the chokepoint — CLOSED 2026-08-27.** Stages A1–A4 of
    [planned/authorization-chokepoint.md](planned/authorization-chokepoint.md)
    landed the tier declaration, the proven caller, the front-door gate and the
    CLI mirror (`8d69f8858b`, `4d60060dc3`, `dba7ed19b6`, `290f6f461b`;
    launcher `2cf887b47`). The gate is at the RPC dispatch layer with a
    `local_console` mirror at CLI entry, per Ruling A option (b) — the scope
    parameter this row used to propose is the optional BACKSTOP (Stage A6, not
    built, and only reachable under rulings (a)/(c) which were not taken). What
    is still open belongs to the gateway, not here: Stage A5 — the policy the
    gate evaluates stays "allow every caller that exists" until
    `gateway/devices.json` mints a device credential with a tier.
  - **Realm pull writes actor files with no office event**
    (`office_sync.apply_office_pull`), so a `WRITE_REMOTE` never becomes a
    patch. A real gap in the notification story and a realm-sync one, not a
    placement one — recorded here because this is where the notification model
    lives.
  - **D6 — RULED 2026-08-27, deliberately not implemented.** Duplicate desks are
    allowed; only a duplicate on the same INSTANCE is not. Superseded in the same
    breath by the direction it was ruled inside: desks are a placeholder for
    artifacts, artifacts are standalone, and the fence should then key on neither
    persona nor instance. See the fence section above for why re-keying now would
    be the wrong move.
  - **D10(iii) — RULED and SHIPPED 2026-08-27.** Un-aimed adds still omit
    `position` (the server decides), and the server's answer is now the world
    origin with a full-grid-step climb on collision. Both repos and the
    byte-pinned `cases.json` moved together; see the placement section above.
  - **D10(ii) — REOPENED 2026-08-27, and it is a real gap, not a scope call.**
    The operator's expectation is that skills set on the context panel apply to
    the next agent placed from that persona. They do not: the panel writes
    `--skill` to the INSTANCE (`persona.instance.update_profile` →
    `skill_overrides`), while a new placement inherits `persona.skills` — the
    template — read LIVE. Nothing writes `persona.skills`: it comes from config
    (`personas.py` / `config.py` overrides) and there is no operator verb and no
    launcher capability for it. "Out of scope" was the wrong call.
  - **Owner decisions still standing on their defaults**, none blocking: D10(iv) `console`
    scope for both methods is no longer prose — it is declared on `rpc.tiers`
    and evaluated at the front door (A1/A3 above); WHICH tiers exist beyond
    `read`/`console` is still the gateway's R11.
  - **No Stage C visual proof** of a CLI placement appearing on a live level.
    No slice's done-when carried one; S0's frame receipt is the lane's evidence
    and it is a hand-recorded capture, not a gated harness.
- Gesture prediction's ONE remaining stage — an unpinned create-refusal
  retraction. Its second row (adoption trusting the client's own content key)
  was discharged 2026-08-27 by the placement verb's S7 →
  [planned/office-gesture-prediction-remainder.md](planned/office-gesture-prediction-remainder.md)
- Collapsing the office write lane to one transport (argv-arm deletion, the
  guarded remove over a population the placement verb shrank, the unbuilt
  restore verb) →
  [planned/office-write-lane-collapse.md](planned/office-write-lane-collapse.md)
- The page-open write storm and the `laneAbsent` window on cold open →
  [planned/office-page-open-write-storm.md](planned/office-page-open-write-storm.md)
- The board surface's missing RPC lane and fold coverage →
  [planned/board-surface-rpc-lane.md](planned/board-surface-rpc-lane.md)
- Remaining console lane-ambiguity: browsable catalog under a failing lane, the
  bare-token paste path, catalog empties →
  [planned/agent-console-lane-honesty.md](planned/agent-console-lane-honesty.md)

## Supersedes

- `planned/agent-placement-verb.md` — **deleted 2026-08-27 by the S10 fold-in
  commit** (`git log --diff-filter=D --oneline -- docs/agent-runtime-harness/planned/agent-placement-verb.md`
  is how you get the sha and the file back). Its §0 verdict, §A decisions D1–D12
  and §A.11's gateway table are the shipped truth stated above; the launcher
  half is `EterniaLauncher/docs/mission_control/04-office-scene.md`; its §C
  killing-mutation table lives on in `tests/mutation_claims.json` and the
  launcher's group docstrings; its §B slice strip and §D risk list are history
  in the commit and in the brain's handoff note.
- [OFFICE_WRITE_VERBS_RPC_PLAN_2026-08-16.md](archive/2026-08-22-pre-consolidation/OFFICE_WRITE_VERBS_RPC_PLAN_2026-08-16.md) — all four verbs shipped; its remaining deferrals are in `planned/`.
- [OFFICE_FOLD_FENCE_CONTENTION_PLAN_2026-08-16.md](archive/2026-08-22-pre-consolidation/OFFICE_FOLD_FENCE_CONTENTION_PLAN_2026-08-16.md) — FC-0/FC-H1/FC-L2 all shipped; the fence class is zero on live receipts.
- [OFFICE_GESTURE_FOLD_PROMOTION_PLAN_2026-08-16.md](archive/2026-08-22-pre-consolidation/OFFICE_GESTURE_FOLD_PROMOTION_PLAN_2026-08-16.md) — the fold model and its `R#nn` register; the mechanism it designed is the one described above.
- [OFFICE_OPTIMISTIC_RENDER_REGRESSION_PLAN_2026-08-16.md](archive/2026-08-22-pre-consolidation/OFFICE_OPTIMISTIC_RENDER_REGRESSION_PLAN_2026-08-16.md) — OR-1/OR-2 shipped and were then SUBSUMED by the intent ledger, exactly as OR-4 required. OR-0 and OR-3 have no separately recorded disposition — the shipped surface is the intent ledger described in the body; consult the archived plan before citing either stage.
- [UNIFIED_GESTURE_PREDICTION_PLAN_2026-08-16.md](archive/2026-08-22-pre-consolidation/UNIFIED_GESTURE_PREDICTION_PLAN_2026-08-16.md) — UP-0/1/2 shipped and UP-4 struck at source; UP-3/5 in `planned/office-gesture-prediction-remainder.md`.
- [HUD_CHIPS_INVESTIGATION_PLAN_2026-08-17.md](archive/2026-08-22-pre-consolidation/HUD_CHIPS_INVESTIGATION_PLAN_2026-08-17.md) — all five stages shipped.
- [AGENT_CONSOLE_DEAD_LANE_AND_LIMITS_PLAN_2026-08-16.md](archive/2026-08-22-pre-consolidation/AGENT_CONSOLE_DEAD_LANE_AND_LIMITS_PLAN_2026-08-16.md) — S1–S4 shipped; the two-homes split is the standing finding.
- [SCOUT_LAUNCHER_LANE_MAP_2026-08-17.md](archive/2026-08-22-pre-consolidation/SCOUT_LAUNCHER_LANE_MAP_2026-08-17.md) — its Hazard A and Hazard B are both CLOSED (see the fold section and `onReconnected`'s production caller at `mission_office_subscribe_lane.dart:787`); do not re-quote them as open.
