# Stage 76 - Unified Template / Instance / Chat / Goal Model

> The confirmed entity architecture for Mission Control, locked with Tony
> 2026-06-22, made implementation-ready by an audit of the current checkout.
> Supersedes the provisional framings in Stage 74 (four-object model) and the
> "node-as-pointer, instance↔chat 1:1" resolution recorded mid-Stage-75.
>
> Repos:
> - Harness: `X:/Eternia/hermes-agent`
> - Launcher: `X:/Unreal Engine/Engine/Launcher/EterniaLauncher`
> - Runtime profiles / stores: `X:/Eternia/.hermes/profiles`, `X:/Eternia/.hermes/agent-runtime`

## Why this stage exists

The "double Alice" bug (two `alice` cards in the Agent Profiles library) was a
category error in the data model, not a view glitch. The library rendered
**instances** (chats/placements) as **templates**, and two parallel creation
paths each minted a fresh `personainst_operator_<uuid>` per call, so operator
instances accumulated with no idempotency. The Launcher card-dedup added in
`_runtimeLibraryEntries` (`mission_office_host.dart`) is a deliberate **stopgap**;
this stage removes the cause and folds that stopgap away.

## The confirmed model — four entities, one chain

1. **Agent / Template** — the persona definition (`alice`, `dev`, `qa`, …).
   Defined once, reusable. This is the **Library**. One template → one card.
   Template id namespace stays `profile:<id>` (Stage 75 closed decision).

2. **Level agent-instance** — a *placement* of a template onto the level (Mission
   Office). **Durable**: stable identity, persists on the level across chats.
   The **Placed** tab. A persona has **one primary placement** by default; more can
   be added — by the operator ("add new instance") or by an agent spawning a
   sub-agent. Multiplicity on the level is **a feature, not the double-Alice bug**
   (that was a *Library* problem; see "Two surfaces" below). What's forbidden is
   *orphaned/unattributed* instances, not multiple attributed ones. (Placement key
   = scene `itemId`; see the closed decision below.)

3. **Chat** — the session a placed instance is currently running. One active at a
   time, **swappable**: a placement can shut its current chat down and open a
   fresh chat of the same persona while staying on the level. The instance
   outlives any one chat.

4. **Goal / Task** — owned by the **chat**, not the placement. The Neko operator
   chat *owns a goal*; a worker chat *carries a task*.

### Cardinalities

```
Agent/Template        1 ───< N   Level agent-instance     (one placement per persona on the level)
Level agent-instance  1 ───< N   Chat                     (over its life; one active, cyclable)
Chat                  0 ───  1    Goal    ── if it is the Neko operator (conductor) chat
Chat                  0 ───  1    Task    ── if it is a worker (slot) chat
Goal                  1 ───< N    worker Chats             (staffs blueprint slots; each carries a Task)
```

### Two load-bearing facts

- **Goal/task lives on the chat.** Swap a chat → the goal/task does *not* follow;
  it belonged to that conversation. The placement persists, goal-less, until it
  runs a new goal/task chat. This answers "what owns the harness goal/task": the
  chat does (the Neko operator chat owns the goal).
- **A goal is just a chat that owns it** → **N goals = N Neko operator chats**
  (peers). No singleton orchestrator. Closes the previously-open `chat→goal`
  cardinality knob (`0..1` vs `0..N`): the cockpit's many-goals view is many Neko
  operator chats. Closes the cycle: `Goal → staffs → worker instances → which are
  placements → running worker chats → carrying tasks`.

### Two surfaces — don't conflate them

The double-Alice bug and level-instance multiplicity live on **different surfaces**:

- **Library (templates).** One card per template, always. The double-Alice bug was
  *here* — the palette rendered instances as templates. Fixed by "Library shows
  templates only" (76B). Once true, **the level's instance count never affects the
  Library** — five Alice placements still show one Alice card.
- **Level / Placed (runtime instances).** Runtime placements live here, and
  multiplicity is **expected and desirable**. It is *not* a dedupe target.

So the add-instance / fork / chat-swap rules are *level* rules; the dedupe rule is
a *Library* rule. They never need to "reconcile" — they're orthogonal.

### Sub-agent spawn = a level instance (visualize + steer)

A spawned sub-agent **is** a level instance. When a goal fans out into
dev/qa/backend — or an agent spawns parallel workers of one role — each becomes a
placement on the level running its own chat and carrying its task. This turns the
`Goal → staffs → worker instances` cycle into a **visible, steerable agent tree**
rather than a black box: the operator sees each spawned sub-agent on the level and
can open/steer its chat. Level instances therefore have **two attributed sources** —
operator placement ("add new instance") and **agent sub-agent spawn** (goal
staffing / swarm). Both are intentional and carry provenance (placing operator, or
parent agent + goal/task); only *orphaned/unattributed* instances are forbidden.

### `mode` is derived, not stored

`PersonaInstance.mode` (`configured` / `free_floating` / `chat` / `task_bound`)
stops being a stored kind. Instance state is **derived** from whether its active
chat carries a goal/task: none → operator chat; task → task-bound; goal → goal
room.

## Tasks are a HUD, not a gate

Second locked principle: **a task / task-list is an advisory HUD self-check, not a
strict gating state machine.** It reflects what a chat is doing and flags gaps —
mutable, soft, **crash-proof**. A stuck or malformed task is a stale HUD line,
never a dead-end. This is how a typical agent harness treats its todo list.

What this de-fangs (current crash/stall sources, audited below):
- The strict `TaskState` machine — especially **`BLOCKED` as a terminal
  dead-end** (`states.py:21`). Under the HUD model "blocked" is a warning chip,
  not a state the harness cannot escape.
- The **proof / verdict gates** — the *Evidence Stack* ("missing approved QA
  verdict", "missing passed test proof") is literally `GateResult.missing` from
  `task_verdict_proof_satisfied` (`proof_gates.py:127`). As a HUD these become
  advisory evidence rows that *inform*.

### Integrity does not disappear — it moves up a layer

"Soft so it can't crash" must not become "claims false success." Resolution:
- **Proof stays as evidence, moves up to the goal owner.** Surfaced in the HUD and
  read by the **Neko operator chat** (conductor) at the escalation point, instead
  of an automatic per-task control-flow gate.
- Integrity lives with the conductor, not in brittle per-task gating ("harness as
  escalation layer"). Preserves one-shot-autonomy's "don't lie about success"
  while removing the crash surface.
- This **refines, does not repeal** the AAA non-negotiable "the harness, not the
  model, owns proof validation": the harness still owns and surfaces the proof
  *evidence* (`proof_gates.py` is untouched as a *computation*); what changes is
  that the **goal owner**, not a per-task gate, adjudicates, and an unmet check
  **escalates** instead of dead-ending.

---

## Audit: current state vs. target

| Concern | Reality in code today | Gap |
|---|---|---|
| Template (Library source) | `available_personas` from `_available_persona_summary` (`snapshot.py:1313`), 18 `profile:<id>` templates + `backs_persona_id`. Already correct. | None — keep. |
| Instance creation | **Two** capabilities (`persona.profile.instantiate` "Create Agent Profile" required `display_name`, `capabilities.py:40`; `persona.instance.create` WIP "Open Agent Chat" optional `display_name`, `:51`) both route to CLI `persona instance create` → `_cmd_persona_instance_create` (`harness.py:929`), which branches on `display_name`: present → `create_operator_chat`, absent → `_queue_free_floating_assignment`. | Both paths mint a per-call UUID — see next row. |
| Canonical id | `create_operator_chat` (`persona_assignments.py:195`) and `create_free_floating` (`:53`) both use `unique_operator_persona_instance_id()` (`:495`) → `personainst_operator_<uuid>`. **`open_chat` (`:136`) already defaults to canonical `persona_instance_id_for(persona_id)` (`:491`)** — the unique override is the anomaly. | Accumulation source. Fix in 76A. |
| Placement key | Launcher office layout keys placements by **`personaId`**: `placementFor(personaId)`, primary item where `itemId == personaId` (`mission_office_layout.dart`). One placement per persona on the level. | Decides the canonical id — see closed decision. |
| Library projection | `_runtimeLibraryEntries` (`mission_office_host.dart:1462`) merges `available_personas` + instance-derived entries; `_isRuntimeLibraryInstance` (`:1514`) admits `freeFloating`/`chatHistory`/`configuredIdle`/`diagnostic` instances as cards. The 2026-06-22 stopgap dedup is at `:1469`. | Instances leak into the template palette. Fix in 76B. |
| Task gating | `TaskState.BLOCKED` (`states.py:21`) is set as a near-terminal state in ~10 sites: `planning.py:178,227,247,599,604,1148,1627`, `blueprints/routing.py:226,230`, `no_freeze_monitor.py:163`, `preflight.py:170`; consumed as a stop in `goal_runner.py:243,333`. | Hard dead-end. Soften in 76C. |
| Proof gating | `proof_gates.py` returns `GateResult(allowed, missing, warnings)` (already has a `warnings` lane). `.allowed` gates transitions in `planning.py:514`, `blueprints/routing.py:45`. | Hard gate. Re-route to advisory + escalation in 76C. |
| HUD surface | A Mission HUD already exists: `context_builder.py` builds `mission_hud`/`agent_hud` with `recommended_action`/`skill_ref` (Stage 59); `persona_runtime.py:558` tells personas to treat it as a closed-choice contract. | The task-HUD **extends** this, not a new surface. |
| Role skills | `docs/agent-runtime-harness/stage46-skills/{harness-mission-lead,harness-qa-verdict,harness-dev-delivery,launcher-analyze-proof}/SKILL.md` encode proof-gate / blocker rules. `harness-mission-lead` is already the conductor (`assign`, `report_blocker`, `request_missing_input`, release through gates). | Skills must reflect proof-as-evidence + Neko-adjudicates. Update in 76C. |

## Closed decisions

- **Placement key = scene `itemId`; instance id ⟷ placement (1:1).** The Launcher
  layout keys items by `itemId`, with a **primary** placement where
  `itemId == personaId` (`mission_office_layout.dart:116` `hasPrimaryAgentForPersona`)
  and **additional** placements minted as `<persona>_agent_2`, `_3`, … by `addItem`
  (`:213-243`). So:
  - The **primary** placement's instance is the canonical
    **`persona_instance_id_for(persona_id)`** (`personainst_profile_alice`) — the
    default target for place/open-chat, idempotent.
  - **Additional** placements (operator "add new instance", or an agent spawning a
    sub-agent) get an instance id derived from their scene `itemId` (e.g.
    `personainst_<itemId>`). These are deliberate, placement-backed, and
    **attributed** — unlike the old orphaned per-call UUIDs that accumulated
    unattributed level instances. (Those orphans are a *level-hygiene* problem; the
    duplicate *cards* the operator saw were the Library rendering instances as
    templates — that's the double-Alice bug, fixed separately on the Library surface
    in 76B.)
  This reframes the grounding-commit "multiple agents per template" idea: it's
  allowed, but **attributed and placement-backed**, not auto-minted-orphaned. A
  `display_name` labels a placement; it never forks one on its own.
- **Keep `profile:<id>` template namespace and `backs_persona_id`** (Stage 75).
- **`open_chat` is already the correct primitive** — 76A makes the create paths
  delegate to its canonical-id behavior rather than overriding it.

## What this supersedes

- **Stage 74's four-object model** (`Template → Instance → Runtime Loop → Work
  Binding`): "Runtime Loop"/"Work Binding" collapse into `Chat → Goal/Task`. Keep
  Stage 74's `available_personas` contract + `profile:<id>` namespace.
- **The mid-Stage-75 "node-as-pointer, instance↔chat 1:1, unique-session" note**:
  reversed. The level node **is** the instance (a durable placement), and
  **instance↔chat is 1:N over time** (swappable), not 1:1.
- **`unique_operator_persona_instance_id` and the two parallel create paths**:
  unified (76A).

---

## Stage 76A — Unify instance creation on the canonical placement id (harness)

**Goal:** one durable instance per persona/placement; opening/cycling chats reuses
it; no more orphan `personainst_operator_<uuid>` accumulation.

### Two real verbs (place vs. open-chat), not one

The two capabilities are not redundant once the instance is canonical — they map
to the two real verbs of the model. Keep both, give them distinct behavior:

- **`persona.profile.instantiate` ("Create Agent Profile") = Place / ensure.**
  Ensure the canonical placement exists for this persona (idempotent — one per
  persona), set/update its display label, and open its first chat. Calling it
  twice for `profile:alice` returns the *same* placement.
- **`persona.instance.create` ("Open Agent Chat") = Open a new chat.** Open a fresh
  chat *session* on an already-placed instance. The placement is unchanged; only a
  new active session is bound.

> Today both look redundant only because both wrongly mint a per-call UUID
> instance. After the canonical-id change below, "place" becomes idempotent and
> "open chat" just adds a session — the two-button UI becomes honest.
> Launcher call sites that must align: `_createAgentInstanceIntent`
> (`mission_control_page.dart:947`, → instantiate) and `_createPersonaChatIntent`
> (`:929`, → instance.create). Both currently map to one CLI in
> `mission_control_bridge.dart:677`.

### Changes
- `agent_runtime/persona_assignments.py`
  - `create_operator_chat` (`:195`) / `create_free_floating` (`:53`): replace
    `unique_operator_persona_instance_id()` with
    `persona_instance_id_for(normalized_persona)` so the **instance** id is the
    canonical placement. Delete `unique_operator_persona_instance_id` (`:495`) and
    `free_floating_persona_instance_id_for` (`:504`) once callers/tests are off
    them.
  - **Per-chat session id must stay resolvable to the instance.** The roster
    recovers a session's persona by parsing the session-id prefix (`_infer_persona_id`,
    `persona_chat_history.py:258`). So a fresh per-chat id derived purely from a
    random token would break grouping, and one derived purely from the (now
    canonical) instance id would collapse every chat onto one session and lose
    history. Use a composite: **`persona_chat_<canonical_instance_id>_<chat_token>`**
    — replace `persona_chat_session_id_for` (`:499`) with a form that appends a
    fresh token while keeping the instance-id segment the roster can parse.
  - **Chat-swap safety — guard on the live run; resolve via operator prompt.**
    `open_chat` (`:136`) today blanks `current_task_id` / `active_worker_session_id`
    / `active_run_id` *unconditionally* (`:188-190`), which orphans a running
    worker/run and races the tick loop. New-chat **and** select-old-chat must, when
    the target placement already has a current chat, surface the two-way operator
    prompt (76B) rather than overwriting:
    1. Load the instance's `active_run_id` / `active_worker_session_id`.
    2. **Add new instance to level** → mint an *additional* placement
       (`personainst_<new_itemId>`) and run the new/selected chat there; the current
       placement and its (possibly live) chat are **untouched** — nothing killed.
    3. **Replace this instance and kill chat** → bind the new/selected chat to this
       placement. If its current chat is **live** (`RunState ∈ {QUEUED, STARTING,
       RUNNING, WAITING_ON_TOOL, WAITING_ON_APPROVAL}` or an active worker), first
       cancel the run (`run.cancel`) and close the worker (`persona.instance.close`
       / worker close), *then* swap.
    4. Never zero `active_run_id` / `active_worker_session_id` unless the underlying
       run/worker was actually terminated. The harness still hard-enforces this:
       a replace against a live run **requires** the kill flag; without it the call
       returns `chat_busy` (the prompt is the UI for that contract, not a substitute
       for it).
  - **Add-instance path:** a new `add_instance` flag/verb mints an additional
    placement-backed instance for an already-placed persona. This is the *only*
    sanctioned way to get a second instance of one persona — explicit, named, and
    tied to a scene `itemId`. (Default place/open-chat still targets the canonical
    primary instance.)
- `hermes_cli/harness.py`
  - `_cmd_persona_instance_create` (`:929`): both branches resolve the canonical
    placement instance; `display_name` updates the placement label, it does not
    fork a new instance. Add a `--kill-active` (force) flag that maps to the
    chat-swap kill path; default is wait/refuse on `chat_busy`. Keep the JSON
    contract keys (`agent_profile_id` == canonical instance id now); add a
    `chat_busy` / `killed_previous` status field.
  - Capability surface: keep both ids but make them the two verbs above —
    `persona.profile.instantiate` (place, requires `display_name`),
    `persona.instance.create` (open-chat, `display_name` optional). Add the
    kill/force arg to both. Update `capabilities.py:40,51` and the Launcher
    registry (`harness_capability_registry.dart:52,76`,
    `mission_control_bridge.dart:677`) so the two ids carry the two distinct
    intents rather than identical args.

### Tests (rewrite to the new invariant)
- `tests/agent_runtime/test_persona_assignments.py`
  - `:164` `test_create_free_floating_instance_is_unique_...` → assert **reuse**:
    `first.id == second.id == persona_instance_id_for("profile:reviewer")`, still
    idle + worker-independent.
  - `:190` `test_create_operator_chat_from_same_template_gets_distinct_sessions` →
    assert `first.id == second.id` (one placement) **and**
    `first.session_id != second.session_id` (distinct chats).
  - `:215` `test_operator_chat_history_binds_by_unique_session` stays green: chats
    still bind by unique session under one instance.
  - Remove `free_floating_persona_instance_id_for` assertions at `:172,:455,:974`.
  - `:731` `test_free_floating_chat_session_binding_reuses_instance_session` →
    **reconcile**: the instance is canonical, but each *new chat* gets a fresh
    session (reuse applies to *resuming* a chat, not to opening a new one). Update to
    assert resume-reuses / new-opens-fresh.
  - `:412` `test_snapshot_exposes_operator_created_idle_persona_instance` and
    `:431` `test_persona_instance_create_cli_creates_free_floating_assignment_without_ticking`
    → ids are now canonical (`personainst_profile_<…>`), not `personainst_operator_<uuid>`.
  - **New:** session ids are `persona_chat_<canonical_instance_id>_<token>` and
    `_infer_persona_id` (`persona_chat_history.py:258`) still recovers the persona —
    history grouping survives the canonical-id change.
  - **New (chat-swap safety):** opening a new chat on an instance with a live
    `active_run_id` returns `chat_busy` and does **not** blank the run fields; with
    the kill flag it cancels the run + closes the worker, then swaps; the orphaned
    run never survives the swap.
  - **New (add-instance):** the `add_instance` path mints a *distinct* placement-backed
    instance (`personainst_<itemId>`) for an already-placed persona; default
    open/place stays canonical (no second instance).
- `tests/agent_runtime/test_persona_chat_history_curation.py` — must stay green
  end-to-end: it exercises session→persona inference + curation that the new
  session-id format feeds. Treat green here as the gate that 76A didn't break history.
- `tests/agent_runtime/test_snapshot.py:510` (`available_personas`) — unchanged
  (templates); add an assertion that `persona_instances` carry canonical ids.
- `tests/agent_runtime/test_capabilities.py:34-36`: assert the two ids carry the
  two verbs (instantiate requires `display_name`; instance.create optional; both
  expose the kill/force arg).

### Acceptance
- Placing `profile:alice` twice → **one** instance (`personainst_profile_alice`);
  opening N chats on it → N distinct, instance-resolvable sessions.
- No new `personainst_operator_<uuid>` files appear under
  `.hermes/agent-runtime/persona_instances/`.
- Swapping the active chat while a run is live is **refused** (or kills first) —
  never silently orphans a worker/run; the chat pointer and the tick loop stay
  consistent.

## Stage 76B — Library shows templates, Placed shows instances (Launcher)

**Goal:** the three layers map to UI; no instance ever renders as a template card.

### Changes (`lib/features/mission_control/office/mission_office_host.dart`)
- `_runtimeLibraryEntries` (`:1462`): Library renders **templates only**
  (`available_personas` where `_isProfileSourceTemplate`). Drop the
  instance-iteration branch and the 2026-06-22 stopgap dedup (`:1469`) — it
  becomes unnecessary once the source is clean.
- `_isRuntimeLibraryInstance` (`:1514`) / `_personaForLibraryInstance` (`:1533`):
  no longer feed the Library; instance presentation lives in the **Placed** tab
  and the chat panel.
- Confirm the **Placed** tab (`_PalettePlacedItemsTab`) and chat panel already
  read instances; add the goal/task badge there from
  `persona_instances[].attached_task_id` / goal id.
- **Conflict prompt (new):** when the operator triggers *new chat* or *select old
  chat* on a placement that already has a current chat, show a two-option dialog:
  - **Add new instance to level** → `addItem` (`mission_office_layout.dart:213`) a
    new placement + the harness `add_instance` path; runs the chat on the new
    instance, leaves the current one alone.
  - **Replace this instance and kill chat** → the kill-then-swap path (sends the
    kill flag; harness cancels run + closes worker, then binds the chat here).
  The dialog is the UI for the harness `chat_busy` contract — if the current chat
  is idle, "replace" is a clean swap; if live, "replace" carries the kill.

### Acceptance
- Library lists 18 templates, one card each; Placed lists current placements
  (including multiple deliberate placements of one persona); neither leaks the
  other. `flutter analyze <changed paths>` clean.
- New-chat / select-old-chat on an occupied placement shows the add-vs-replace
  prompt; "add" forks a new instance, "replace" kills-then-swaps; neither orphans a
  live run.

## Stage 76C — Soften the task layer to a HUD (harness + skills)

**Goal:** tasks/task-lists are advisory; the harness cannot crash or dead-end on
task/proof state. **This is the largest and riskiest stage — phase it.**

### Phase 1 — `BLOCKED` is non-terminal
- Make `TaskState.BLOCKED` a *recoverable, escalation-raising* state rather than a
  stop. Audit each setter (`planning.py:178,227,247,599,604,1148,1627`,
  `blueprints/routing.py:226,230`, `no_freeze_monitor.py:163`, `preflight.py:170`)
  and the consumers (`goal_runner.py:243,333`): on block, emit an escalation
  signal to the **goal owner chat** and keep the run loop alive (route to the
  conductor) instead of terminating the tick.
- Prefer routing through the **blueprint** path (`blueprints/routing.py`) which is
  meant to replace the legacy role-encoded `planning.py` machine — soften there
  first, leave `planning.py` legacy paths for last.

### Phase 2 — proof becomes advisory evidence read by the goal owner
- `proof_gates.py` stays as the *computation* (untouched). Change the **consumers**:
  where `GateResult.allowed` currently hard-blocks a transition
  (`planning.py:514`, `blueprints/routing.py:45`), instead surface
  `GateResult.missing` as **HUD evidence on the goal-owner chat** and let the
  conductor (Neko) adjudicate/escalate. Reuse the existing `warnings` lane on
  `GateResult` and the Mission HUD (`context_builder.py` `agent_hud`) rather than
  inventing a surface.
- Keep proof *evidence* first-class and redaction-safe; only the *gating* behavior
  moves from per-task control flow to conductor-read escalation.

### Phase 3 — skills reflect the new posture
- `stage46-skills/harness-mission-lead/SKILL.md`: Neko, as goal owner, *reads the
  Evidence Stack / HUD and adjudicates*; blocked is an escalation to handle, not a
  dead-end. (It already owns `assign`/`report_blocker`/`request_missing_input`.)
- `stage46-skills/harness-qa-verdict/SKILL.md` + `harness-dev-delivery/SKILL.md` +
  `launcher-analyze-proof/SKILL.md`: reword "proof gate blocks release" to "proof
  is evidence the goal owner adjudicates"; missing proof is a HUD warning, not a
  terminal block.

### Acceptance
- A goal with a missing proof / blocked task surfaces HUD evidence + an escalation
  to the goal owner; the harness keeps running rather than dead-ending.
- `python -m pytest tests/agent_runtime -q` green (existing proof-gate *computation*
  tests unchanged; gating-behavior tests updated).

## Stage 76D — Direct flow: bind the blueprint engine to the entity model (TO-DO, next stage)

**Why:** scripting the harness flow is ambiguous today because **two orchestrators
coexist** and entry points silently pick different ones (audit: `goal run`
→ bare Task → *legacy* `TaskState` ladder; `blueprint run` → MissionPlan → *graph*;
chosen by `has_typed_plan`). The blueprint engine (blueprint flow Stages 0–9) is
built but the legacy ladder is not deleted (Stage 10 pending), and nobody has
written down that the blueprint engine's `slot/RUN_SLOT` vocabulary **is** this
doc's `instance/chat/goal` model. Two docs describe one flow at two altitudes and
aren't connected. This stage connects them and collapses the flow to one path.

**Depends on:** blueprint-flow [Stage 10 — legacy deletion](../../agent_runtime/docs/blueprint_goal_flow_stages.md)
(delete the `TaskState` ladder, `retired_*_action`, `retired_owner_allowlist`, the
`has_typed_plan` fork). 76D is the entity-model binding *on top of* that retirement.

### The mapping (the missing translation table)

| Blueprint-flow term | Entity-model term (this doc) |
|---|---|
| Goal / MissionPlan owner | **Neko operator chat** that owns the goal |
| `slot` binding → persona | a **worker instance** = a **level placement** (visible/steerable) |
| `RUN_SLOT(slot_id)` | spawn-or-resume that worker's **level instance + chat** |
| `StageStatus` / proof gate | the worker chat's **soft task HUD** (76C) — advisory, conductor-read |
| stage `intervention` outcome | **escalation to the goal owner** (Neko chat), not a dead-end |
| blueprint (template) | the flow the goal-owner chat runs; slots staff sub-agent instances |

### 76D.1 — Collapse entry points onto the graph (one orchestrator)

**Depends on** blueprint Stage 10 deleting the legacy ladder + the
`has_typed_plan` / `mission_plan_routing_enabled` fork in
[`state_machine.py`](../../agent_runtime/state_machine.py) `next_action` (`:57-130`,
which today still returns `_run_slot` with hardcoded role ids `neko_supervisor`/
`dev`/`qa`). 76D rides on that — do not re-spec it here.

- `agent_runtime/goal_runner.py` — `GoalRunOptions` (`:20`) gains
  `blueprint_id: str | None = None` and `bindings: dict[str, str] = {}`.
  `MissionRuntimeController._create_task` (`:168`) — after creating the `Task`,
  **instantiate the blueprint onto `task.mission_plan`** (`blueprints/instantiate.py`
  `instantiate_blueprint`) so `has_typed_plan(task)` is true → graph-routed from
  birth. Default blueprint id from config (e.g. `neko_dev_qa_basic`).
- `hermes_cli/harness.py` — `_cmd_goal_run` (`:781`) thread `--blueprint` (default)
  and `--bind` into `GoalRunOptions`. `goal run` becomes `blueprint run` with an
  implicit/default blueprint; `tick` (stepped) and `daemon` (background) call the
  same `TickEngine` → one flow, three depths, never two engines.
- **Tests:** `tests/agent_runtime/test_goal_runner.py:116` — `goal run` produces a
  graph-routed task (`has_typed_plan` true, first action `run_slot`), not a
  legacy-ladder task; `:270` blocker-summary path still holds via graph
  `intervention`.

### 76D.2 — `RUN_SLOT` spawns an attributed level instance

The seam where a slot becomes a running worker is
[`ticker.py`](../../agent_runtime/ticker.py) `:415` (resolve `persona` →
assignment → worker), with slot→persona resolution in
`_persona_id_for_harness_action` (`:2917`, already reads `plan.bindings[slot_id]`).

- `agent_runtime/models.py` — `PersonaInstance` gains `spawned_by: str | None`
  (provenance: `operator` vs the parent coordinator slot/instance) and a `goal_id`
  linkage. This is the attribution the level-instance model (76A) and "sub-agent
  spawn = a level instance" require.
- `agent_runtime/ticker.py:415` — when RUN_SLOT spins up the worker, **ensure the
  canonical `PersonaInstance` placement** for that persona and set
  `goal_id` + `spawned_by = <coordinator>`, so `derive_from_workers` /
  `persona_instances` renders it as an attributed **level node**.
- `agent_runtime/snapshot.py` — `persona_instance_summary` surfaces `spawned_by` /
  `goal_id` so the Launcher draws the steerable sub-agent tree.
- **Tests:** a RUN_SLOT tick yields a `persona_instance` with `spawned_by` = the
  coordinator and `goal_id` set; snapshot exposes it as a level placement.

### 76D.3 — Living-steer verbs + permission scope

Steer verbs already exist as `normal`-danger capabilities (`worker.nudge`,
`worker.resume`, `persona.instance.message`); create/kill are `warning`/`destructive`
(`run.cancel`, `persona.instance.close`, the 76A `add_instance`). 76D.3 gates the
latter for a *coordinator* (not the operator).

- **Permission-scope schema (resolves the prior open knob).** Add to the goal (the
  Neko chat owns the goal; the scope lives on the goal/assignment):
  ```python
  @dataclass(slots=True)
  class CoordinatorPermissionScope:
      max_spawns: int = 0            # create (spawn) actions allowed in-scope
      spawns_used: int = 0
      may_kill_own: bool = True      # kill instances THIS coordinator spawned (spawned_by == self)
      may_kill_others: bool = False  # kill operator-placed/other → always operator grant
      # time/action budget reuses runtime_config max_actions_per_tick / run_lease_seconds
  ```
  Default rides `AgentPersona.autonomy` (`models.py:152`, default `"review"`):
  `review` → confirm each create/kill; higher autonomy → in-scope auto. The
  own-vs-others test uses 76D.2's `spawned_by`.
- **Gate seam.** A central `authorize_coordinator_action(action, scope, target_instance)
  -> ok | needs_operator_confirm` checked in the capability/CLI handlers for
  `warning`/`destructive` verbs when the actor is a coordinator. In-scope → proceed
  and increment `spawns_used`; out-of-scope → return `needs_operator_confirm` (the
  Launcher surfaces a confirm, same pattern as the 76A `chat_busy` prompt). Operator
  actions bypass the gate.
- **Tests:** steer verbs run with no scope; `create` beyond `max_spawns` →
  `needs_operator_confirm`; `kill` of an own-spawned (`spawned_by == self`) child
  within `may_kill_own` → ok; `kill` of an operator-placed instance → always
  `needs_operator_confirm`.
- **Skills:** `harness-mission-lead` documents the bounded verb set + scope (76C/76D
  skill map); re-install bumps its manifest hash.

> **Residual decision (small):** where the scope is *granted* — a per-goal default in
> `runtime_config` vs a field the operator sets when capturing the goal. Default to a
> conservative `runtime_config` value (`max_spawns` small, `may_kill_others=False`);
> the operator can raise it per goal. Not a blocker for 76D.1/76D.2.

### 76D.4 — Agent contract: first message + HUD (the node sockets' content)

What a node's agent actually receives. **Three layers, kept separate** — the common
mistake is folding them into one template:

1. **System prompt — per *persona*, stable.** The participation contract ("read the
   `## Mission HUD`, reply with a structured `AgentDecision`"). Lives on the persona
   (`persona_runtime.py:558`); **not** templated per node. Unchanged.
2. **First message = templated objective — per *node*.** Today the objective is raw
   `ctx.current_stage.objective` / `task.description`
   ([`context_builder.py:221,837`](../../agent_runtime/context_builder.py)). Replace
   with a **`(owner_slot.role × output_type) → objective` template registry** that
   renders: *what* + *deliver-what* + *acceptance*, with the **goal** (first node) or
   the **upstream node's output** (downstream node) filled into the input slot. Keep
   it thin (Stage 59) — it states the objective, never re-states the HUD/behavior.
3. **HUD — live per-turn contract — per *node*, every turn.** `_mission_hud`
   (`context_builder.py:672`) already builds `agent_hud` / `next_required_move`
   (`:712`) / `recommended_action` (`:691`) / `decision_menu` / `payload_skeleton` /
   `skill_ref`. Make it **stage-shaped, not role-shaped**: replace the role keying
   (`_hud_role` `:967`, `hud_shape_index_for_role` `:1101`, `role_checklist_hud`
   `:721`) with the **current stage's** `owner_slot` + `objective` + `proof_gate` +
   outgoing edges. The HUD's `recommended_action` = the edge the stage must satisfy;
   its required proof = the stage `proof_gate`. (This is blueprint-flow **Stage 7** +
   the retirement ledger's "role-shaped HUD → slot/stage-shaped" — do it there, wire
   it here.)

**Relationship:** layer 2 opens the node once (prose objective); layer 3 steers every
turn after, including turn one (machine contract). The template never carries HUD
shape; the HUD never carries prose behavior.

**Output choice drives both, from one place:** the node's **output socket** sets the
stage `proof_gate`, which feeds (a) the template's *deliver-X* clause and (b) the
HUD's `required_proof_types` / `recommended_action`. `code feature` →
`proof_gate.required_proof_types:[test_run]` + commit/diff; `design document` → a doc
artifact gate, no test. One choice, two surfaces, kept consistent because both read
the same `proof_gate`.

**Files touched**
- `agent_runtime/context_builder.py` — `build_context` (`:50`) renders the templated
  first message at the objective injection (`:221`); `_mission_hud` (`:672`) +
  `_simplified_agent_hud` (`:825`) become stage-derived; drop the role-shaped
  `_hud_role`/`hud_shape_index_for_role`/`role_checklist_hud` keying.
- `agent_runtime/objective_templates.py` (new) — the `(role × output_type)` registry
  + `render_objective(stage, *, goal, input_artifact)`.
- `agent_runtime/blueprints/schema.py` / `MissionPlanStage` — carry the node
  `output_type` (or derive it from `proof_gate.required_proof_types`); wire
  output_type → `proof_gate` at `instantiate_blueprint`.
- `agent_runtime/proof_gates.py` — unchanged (the gate the output selects).

**Tests**
- `tests/agent_runtime/test_context_builder.py` — first message is the rendered
  `(role × output_type)` objective (not raw `task.description`); a downstream node's
  template uses the upstream output as input; `_mission_hud` derives
  `recommended_action`/required proof from the current stage's `proof_gate` + edges,
  **not** from role (update role-shaped assertions to stage-shaped).
- `output_type=code feature → required_proof_types:[test_run]`;
  `output_type=design document → doc artifact gate` (no test).

**Skills**
- The role skills reference the HUD's **stage-shaped** fields; Stage 59 split
  preserved (compact options in `agent_hud`, full rules in the skill).

### The graph is living — continuous steering, bounded by permission

The graph is **not a one-shot DAG** (data in → transform → out → done). The first
node (the coordinator, e.g. Neko) keeps steering the downstream nodes for the life
of the mission. This is the property that justifies the graph existing at all — and
it is the through-line of the whole model (durable instances, swappable live chats,
soft-HUD escalation made continuous).

- **A node is a running chat, not a function call** — live, observable, interruptible.
  "Output" is the live trace/HUD updating as the agent works, not only a terminal
  artifact.
- **An edge carries two things:** a **dataflow** wire (artifact: output → next input)
  *and* a **steering** wire (the coordinator's live authority over what it spawned).
- **Two steering authorities:** the coordinator steers programmatically; the
  **operator** steers manually by dropping into any node's chat (chat-first). The
  graph is a shared control surface, not a script that runs away.

**Bounded live-steer verb set (the safety line):**

| Class | Verbs | Danger | Permission |
|---|---|---|---|
| **Steer** (shape unchanged) | use/read output, message/re-prompt, re-scope, re-route along existing edges | `normal` | **ungated** — coordinator does these freely |
| **Restructure** (shape changes) | **create** (spawn instance), **kill** (cancel run + close worker) | `warning`/`destructive` | **gated** — requires permission |

This rides the existing capability **danger levels** (`worker.nudge`/`resume`/
`persona.instance.message` = `normal`; `run.cancel`/`persona.instance.close`/the 76A
`add_instance` = `warning`/`destructive`) — no new machinery. **Kill is
provenance-sensitive:** killing an **agent-spawned own child** is lighter than
killing an **operator-placed** instance (the latter always needs explicit operator
grant), using the attribution from the level-instance model.

> **Permission model — specified in 76D.3.** `CoordinatorPermissionScope`
> (`max_spawns`, `may_kill_own`, `may_kill_others`, riding `autonomy` +
> `runtime_config` budget) with a central `authorize_coordinator_action` gate that
> returns `needs_operator_confirm` out of scope. Only the *grant location* (per-goal
> config default vs operator-set-at-capture) is a small residual decision (76D.3).

### Acceptance
- `goal run` and `blueprint run` execute the **same** graph engine; no path reaches
  the legacy ladder (grep gates from blueprint Stage 10 pass).
- A running goal shows its slots as **level instances** (placements) the operator can
  open/steer; sub-agent spawns appear as attributed placements.
- The coordinator can **steer** (use output, re-prompt, re-route) without permission,
  but **create/kill** is refused without an in-scope grant or live operator confirm;
  killing an operator-placed instance always requires explicit operator grant.
- The mapping table above is reflected in both docs; the single-line flow is the
  canonical description of "scripting the harness flow."

---

## Recommended build order

1. **76A** — unify creation (small, high-value, kills the recurrence at the
   source). Land with its test rewrites.
2. **76B** — Library=templates / Placed=instances; remove the stopgap dedup.
3. **76C Phase 1** — `BLOCKED` non-terminal (biggest crash-surface win).
4. **76C Phase 2–3** — proof-as-evidence + skill rewording.
5. **76D** — direct flow: land blueprint Stage 10 (one orchestrator), collapse the
   entry points, and bind `slot/RUN_SLOT` to `instance/chat/goal` (next stage).

## Hard invariants

- One Library card per template, always. Instances never render as templates.
- Goal/task is owned by the **chat**; swapping a chat drops it; the placement
  persists.
- **Chat swap is guarded, never silent.** New-chat / select-old-chat on an occupied
  placement resolves through the operator prompt — **add a new instance** (fork a
  placement, kill nothing) or **replace + kill** (cancel run + close worker, then
  swap). The active-chat pointer cannot move over a live run without the kill flag;
  the harness returns `chat_busy` otherwise. Run/worker fields are only zeroed when
  the underlying run/worker was actually terminated.
- **Level multiplicity is attributed, never orphaned.** A second placement of one
  persona exists only via an attributed source — operator "add new instance" or an
  agent sub-agent spawn; instance id ⟷ scene `itemId`. No code path silently mints
  orphaned/unattributed level instances. (The double-Alice *cards* were a separate
  **Library** bug — instances rendered as templates — fixed by 76B; level instance
  count never affects the Library.)
- Past chats are **preserved and resumable** — a new chat advances the active slot;
  the old session stays in SessionDB, grouped under the placement via an
  instance-resolvable session id (`persona_chat_<instance_id>_<token>`).
- The canonical instance id is `persona_instance_id_for(persona_id)` and matches
  the Launcher placement key (`personaId`) — never reintroduce per-call UUID
  instances.
- Goals scale by being chats (N goals = N Neko operator chats) — never reintroduce
  a singleton goal manager or the hardcoded role-encoded `TaskState` machine.
- Tasks are advisory HUD; proof is evidence read by the goal owner at escalation —
  integrity stays, gating-by-crash goes. `proof_gates.py` computation is preserved;
  only its *consumers'* gating behavior changes.
- Launcher is on a shared WIP branch: stage only changed files by path; "done" =
  `pytest` + `flutter analyze` pass, plus the operator click-list to confirm.

---

## Test discipline

Tests are part of each stage, not a follow-up. Three rules:

1. **Tests land in the same change as the code.** No stage is "done" with red or
   stale tests. A behavior this doc *changes* (e.g. unique→canonical instance id)
   must have its pinning test *rewritten in the same diff* — never deleted to go
   green, never left asserting the old behavior.
2. **Every hard invariant has a guarding test.** The invariants above are only real
   if a test fails when they're violated (chat-swap orphan, instance-as-template
   leak, silent duplicate, proof-gate crash, legacy-ladder reachable).
3. **Headless-first; deletions are grep-gated.** Each stage validates from the repo
   shell (`pytest` / `flutter analyze`) with no MCP unless visual proof is the
   point. Where a stage *deletes* a symbol (76C/76D), a `rg` returning zero hits
   outside tests/migrations is part of the gate (mirrors blueprint Stage 10).

### Test map (per stage)

| Stage | Existing tests to update | New tests (guard the invariant) |
|---|---|---|
| **76A** | `test_persona_assignments.py` — the ~10 tests pinning unique-id/session behavior (`:164,:190,:215,:412,:431,:455,:731,:870,:974` …); `test_capabilities.py:34-36`; `test_snapshot.py:510`; `test_persona_chat_history_curation.py` stays green | canonical reuse + distinct sessions; instance-resolvable session-id format; **chat-swap `chat_busy`/kill** no-orphan; **add-instance** distinct placement |
| **76B** (Launcher) | `flutter analyze` on `mission_office_host.dart` + layout; existing office/palette widget tests | Library renders **templates only** (no instance cards); Placed shows multiple deliberate placements; conflict prompt → add vs replace+kill |
| **76C** | `test_worker_actions_blocked_menu.py` (BLOCKED menu → escalation, not terminal); `test_planning.py` / `test_state_machine.py` / `test_transitions.py` / `test_ticker.py` (BLOCKED routing); `test_goal_runner.py:270` (blocker summary); `test_proof_gates.py` **stays green** (computation unchanged) | `BLOCKED` is recoverable/escalates (run loop survives); proof `missing` surfaces as HUD evidence on the goal-owner chat, does **not** hard-block a transition |
| **76D** | `test_goal_runner.py` (`:116` goal run now graph-routed, not legacy); `test_state_machine.py`; `blueprints/test_blueprint_runtime.py`; `test_context_builder.py` (role-shaped HUD → stage-shaped); blueprint-flow Stage 10 grep gates | `goal run` == `blueprint run` engine; `RUN_SLOT` spawns an attributed level instance; first message = `(role × output_type)` template, HUD stage-shaped, output→proof-gate; steer ungated / create+kill permission-gated (in-scope vs confirm vs operator-grant) |

Headless gate (all stages): `python -m pytest tests/agent_runtime -q` green; for
76B/76D UI, `flutter analyze <changed paths>` clean. 76C/76D add the deletion greps
from blueprint Stage 10 (`retired_*_action`, role-named `TaskState` members, etc.).

---

## Skill discipline

Skills are the agents' operating manual — if a skill tells an agent to behave the old
way after the contract changes, the agent behaves wrong even with correct code. So
skills update in lockstep, exactly like tests.

1. **A skill lands with the contract change it describes.** If 76C makes proof
   advisory, `harness-qa-verdict` cannot still say "block release until proof
   passes" — that's contract drift; the agent runs off the stale manual.
2. **Skill changes are versioned + readiness-gated.** Editing a skill bumps its
   `skill_manifest_hash`; `_agent_summary` surfaces `skill_hash_mismatches` /
   `missing_skills`. "Done" includes **re-installing** the updated skills into the
   bound profiles (`hermes harness install-stage46-skills`) so live personas aren't
   left on stale manifests (`agent_runtime/skill_install.py`).
3. **Keep the HUD/skill split (Stage 59).** Live closed-choice options stay in the
   `agent_hud`; the full rules — proof-adjudication, steering verbs, permission model
   — live in the skill. Don't push prose contract into the HUD or shape rules into
   the skill.

### Skill map (per stage)

Role→skill binding today (`config.py:16` `_STAGE46_REQUIRED_SKILLS`):
`neko_supervisor → harness-mission-lead`; `dev → harness-dev-delivery,
launcher-analyze-proof`; `backend_dev → harness-dev-delivery`; `qa → harness-qa-verdict`.

| Stage | Skills to update | What changes |
|---|---|---|
| **76A / 76B** | none | creation + Library are operator/Launcher-facing, not agent-contract |
| **76C** | `harness-qa-verdict`, `harness-dev-delivery`, `launcher-analyze-proof`, `harness-mission-lead` | proof = **evidence the goal owner adjudicates**, not a hard release gate; missing proof = HUD warning, not terminal; **`BLOCKED` = an escalation to handle, not a dead-end**; mission-lead reads the Evidence Stack / `agent_hud` and adjudicates |
| **76D** | `harness-mission-lead` (primary) **+ the install mechanism** | Neko = **living coordinator**: the bounded verb set (steer = use-output/re-prompt/re-route, ungated; **create/kill = permission-gated, provenance-sensitive**); `slot`/`RUN_SLOT` = a **level instance**, sub-agent spawn; **retire the role-keyed `_STAGE46_REQUIRED_SKILLS` map → persona `skills` + per-stage `required_skills`**, and **role-shaped HUD → slot/stage-shaped** (blueprint Stage 10 retirement ledger) |

> 76D's skill *mechanism* change (role-map → persona/stage skills, role→slot HUD) is
> the blueprint-flow Stage 10 retirement, not new work — do it there, and re-key the
> four skills off slots/stages instead of hardcoded roles.
