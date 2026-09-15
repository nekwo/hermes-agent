# 02 — Blueprint Goal-Flow Engine

> **2026-07-30 — describes a removed subsystem.** The Blueprint stage-graph engine
> specified here (blueprints, `MissionPlan`, slots/edges/proof gates, `ticker.py`,
> `goal_runner.py`) was removed by
> [16 — Mission Lane Removal](16-mission-lane-removal.md). Retained for
> archaeology. Still-live content: the §Identity profile→persona substrate —
> `promote_profile_to_persona` now lives in `agent_runtime/personas.py` behind the
> permanent `agent_runtime/blueprints/resolve.py` shim — and the AAA
> non-negotiables in spirit. The implementation reference this doc links
> (`agent_runtime/docs/blueprint_goal_flow_stages.md`) was deleted with the engine
> (S7), and companion doc 03 was deleted 2026-07-30.

> **Status (as written 2026-07; NO LONGER TRUE — see the removal note above):
> "the live engine spec + remaining build."** How a Mission Control goal
> actually runs: a stable **graph** of swappable **agent bindings**, 1 to N agents, not
> a forced 3–4. This folds the blueprint goal-flow staged plan and the simplified-state
> work that preceded it into one source of truth.
>
> Companions: [01 — Architecture](01-architecture.md) (the entity model this engine
> served) and `03-retirement-ledger.md` (deleted 2026-07-30; the legacy execution
> path this engine deleted).
>
> The canonical engine spec also lives at
> `agent_runtime/docs/blueprint_goal_flow_stages.md` (kept beside the code, with full
> per-stage schemas and CLI examples). **This doc is the product-level summary +
> entity-model binding; that file is the implementation reference.** Keep them in sync.

---

## Core principle — replacement, not a new layer

The harness historically had **two orchestrators** side by side in
`agent_runtime/state_machine.py`:

1. **Legacy role-encoded `TaskState` ladder** — `if state == TaskState.X` chain in
   `next_action`, with the dev→qa→pm→neko shape welded into the enum. This is the
   "3–4 forced" problem: you cannot express a 1-agent or 5-agent flow because the graph
   *is* the enum.
2. **The typed `mission_plan` path** — `_typed_next_action`, gated by
   `has_typed_plan(task)`, walking a real stage DAG (`MissionPlan` / `MissionPlanStage`,
   `mission_plan.py`).

**The blueprint engine is the generalization of #2 and the retirement of #1** — a
*promotion of an existing subsystem*, not a greenfield build. The single largest risk
is running both orchestrators at once and having them disagree. The coexistence rule:

> **A mission is owned by exactly one orchestrator, decided once at creation and never
> mixed.** `has_typed_plan` true → graph routing only; false → legacy ladder unchanged.
> No per-turn negotiation, no dual-write. [03](03-retirement-ledger.md) tracks deleting
> the legacy side and the `has_typed_plan` fork itself.

### What the typed path already gives us (do not rebuild)

| Blueprint concept | Already exists as | Location |
|---|---|---|
| Stage DAG | `MissionPlan.stages` | `models.py` |
| Generic per-stage status | `StageStatus` (`draft/ready/implementing/ready_for_qa/passed/needs_fixes/blocked`) | `states.py:53` |
| Stage ownership | `MissionPlanStage.owner` / `owner_slot` | `models.py` |
| Edges (implicit) | `MissionPlanStage.depends_on` DAG | `models.py` |
| Routing engine | `next_unblocked_stage`, `release_next_stage` | `mission_plan.py` |
| Validation + cycle detection | `validate_mission_plan` | `mission_plan.py` |
| `RUN_SLOT` action | `HarnessActionType.RUN_SLOT` | `actions.py` |
| Graph routing | outcome derivation, edge lookup, attempt bounds | `blueprints/routing.py` |
| Blueprint schema/store/instantiate/resolve | `agent_runtime/blueprints/` | shipped |

---

## Identity: slot / role / persona / profile

Two real identities with a substrate/overlay relationship, plus two blueprint-local
concepts (slot, binding).

- **Tier 1 — Hermes profile (substrate).** On-disk profile dirs (`hermes_cli/profiles.py`).
  Name, model, provider, bundled skills — **no role, toolsets, autonomy, or
  orchestration contract.** A profile alone is `template_only`, **not bindable**.
- **Tier 2 — Persona (participation layer).** `AgentPersona` (`models.py`), persisted via
  `AgentStore` (`store.py`). Adds role, toolsets, autonomy, system prompt, skills, MCP,
  and points at a profile via `hermes_profile`. **The persona is what lets an agent
  participate in the graph at all** — it injects the HUD, exposes skills/toolsets, and
  carries the `AgentDecision` protocol. **A binding always targets a persona, never a
  bare profile.**
- **Slot** — logical owner inside one blueprint (`builder`, `verifier`, `lead`). Declares
  a `role` (∈ `AgentRole`) + a default proof expectation. Stages own an `owner_slot`,
  never a profile/persona directly.
- **Binding** — `persona:<id>` (direct) or `profile:<name>` (resolve the wrapping
  persona, or **auto-promote** the profile into a persisted persona from the slot's
  role). Unprefixed bindings are invalid. Persisted runtime binding is always
  `slot_id → persona_id`; requested strings kept in `binding_sources` for replay.

**Resolution chain:** `stage.owner_slot → binding[slot] (a persona id) → persona →
persona.hermes_profile → on-disk profile`.

**Dynamic provisioning.** Binding a profile with no persona **auto-provisions** a real,
persisted persona from the slot's `role` (default prompt + mandatory orchestration
skills + role toolsets). This is a *promote*, not a throwaway. Capabilities:
`persona.profile.create` (new template), `persona.profile.promote` (template → bindable
persona), `persona.profile.delete` (rejects live-bound profiles; marks dependents
`orphaned`). `ROLE_PROMOTION_DEFAULTS` holds the per-role defaults; `ROLE_ALIASES =
{"neko": "alice_supervisor"}` normalizes during migration.

---

## The blueprint schema (closed)

`agent_runtime/blueprints/schema.py`:

```python
class StageOutcome(StrEnum):                       # closed edge vocabulary
    PASSED = "passed"; FAILED = "failed"; BLOCKED = "blocked"
    READY = "ready"; MISSING_INPUT = "missing_input"; REWORK = "needs_fixes"

TERMINAL_TARGETS = frozenset({"done", "intervention"})

@dataclass(frozen=True, slots=True)
class Slot:      id: str; role: str                # role ∈ AgentRole
class ProofGate: required=True; minimum_status="passed"; required_proof_types=(); proof_recipe_id=None; commands=()
class Edge:      from_stage: str; on: StageOutcome; to: str
class BlueprintLimits: max_total_stages: int; max_attempts_per_stage: int
class BlueprintStage:  id; owner_slot; objective; repo="none"; kind="implementation"; proof_gate=ProofGate(); required_skills=(); depends_on=(); on_unhandled=None
class Blueprint: id; version; name; slots; stages; edges; limits; on_unhandled="intervention"
```

- **`StageOutcome` (edge routing) is distinct from `StageStatus` (persisted state).**
  Mapped via `OUTCOME_TO_STAGE_STATUS` (`FAILED`/`REWORK → REWORK`, `BLOCKED → BLOCKED`,
  etc.).
- **Outcomes are derived, not declared by the agent.** `derive_stage_outcome(stage,
  decision, proofs)` maps a finished run (block / QA verdict / unresolved context / proof
  satisfied) to one closed outcome. Routing is then a table lookup
  (`next_target(plan, stage, outcome)`), replacing the `if state == TaskState.X` ladder.
- **Loop limits.** `MissionPlan.stage_attempts[stage_id]` increments on entry; on breach
  of `max_attempts_per_stage` / `max_total_stages` the engine routes to `intervention`.
- **`intervention`** is a reserved target: sets the mission `BLOCKED`, links an incident,
  routes to the `lead` slot if bound else surfaces to the operator; re-entering for the
  same unresolved signal is a `NOOP` (preserves one-shot autonomy).

Storage/instantiation/resolution live in `blueprints/{store,instantiate,resolve}.py`.
`validate_blueprint` rejects: undeclared slot, role ∉ `AgentRole`, edge to unknown
target, uncovered outcome (no silent dead-end), missing limits, dependency cycle.

---

## Make HUD, skills, and proof dynamic (slot/stage-shaped, not role-shaped)

Three subsystems are still role-shaped or hardcoded; each must be re-keyed off the graph
so it scales 1-to-N. None is thrown away.

**HUD (per-turn agent contract).** Built by `_mission_hud` (`context_builder.py`),
injected as a `## Mission HUD` JSON block. Today role-shaped (`hud_shape_index_for_role`,
`role_checklist_hud`). *Dynamic form:* derive from the **current stage** — its
`owner_slot`, objective, proof gate, and **outgoing edges**. `recommended_action` = the
edge the stage must satisfy; required proof = the stage `proof_gate`. One HUD builder,
parameterized by stage.

**Skills (per-agent capability).** Live on `AgentPersona.skills` but force-augmented by a
hardcoded `_STAGE46_REQUIRED_SKILLS` map (`config.py`) via the `stage46_*` install path.
*Dynamic form:* two sources, no hardcoded map — **persona-level skills** (overlay,
seeded on `create_profile`) + **stage-level `required_skills`** (checked at
binding/promotion, surfaced as a readiness block). Drop `_STAGE46_REQUIRED_SKILLS` and
the `stage46` naming entirely.

**Proof gates.** `ProofRecipe` registry + `MissionPlanStage.proof_recipe_id` already
attach proof. The gap was role-named gate functions (`*_proof_satisfied`), now collapsed
into one generic `stage_proof_satisfied(stage, proofs)` driven by the stage's
`proof_gate`. The recipe registry stays curated/allowlisted.

### The agent contract — three layers, kept separate

The common mistake is folding these into one template:

1. **System prompt — per *persona*, stable.** The participation contract ("read the
   `## Mission HUD`, reply with a structured `AgentDecision`"). Lives on the persona
   (`persona_runtime.py`); not templated per node. Unchanged.
2. **First message = templated objective — per *node*.** *(Removed 2026-07-31.)*
   This proposed replacing raw `ctx.current_stage.objective` / `task.description`
   with a `(owner_slot.role × output_type) → objective` registry that rendered
   *what* + *deliver-what* + *acceptance*. The stage graph it templated went with
   S7, so the renderer module had no caller and was deleted; the sentence is kept
   only so the three-layer framing below still reads.
3. **HUD — live per-turn contract — per *node*, every turn.** Stage-shaped (above).

Layer 2 opens the node once (prose objective); layer 3 steers every turn after (machine
contract). **Output choice drives both from one place:** the node's output socket sets
the stage `proof_gate`, which feeds the template's *deliver-X* clause **and** the HUD's
`required_proof_types`. `code feature → required_proof_types:[test_run]` + commit/diff;
`design document → doc artifact gate, no test`.

---

## Permission scope (living-steer safety line)

Steer verbs (`worker.nudge`, `worker.resume`, `persona.instance.message`) are `normal`
and **ungated**. Create/kill (`run.cancel`, `persona.instance.close`, `add_instance`)
are `warning`/`destructive` and **gated** for a *coordinator* (not the operator):

```python
@dataclass(slots=True)
class CoordinatorPermissionScope:
    max_spawns: int = 0            # create actions allowed in-scope
    spawns_used: int = 0
    may_kill_own: bool = True      # kill instances THIS coordinator spawned (spawned_by == self)
    may_kill_others: bool = False  # kill operator-placed/other → always operator grant
```

The scope lives on the goal (the Neko chat owns it). Default rides
`AgentPersona.autonomy` (default `"review"` → confirm each create/kill). A central
`authorize_coordinator_action(action, scope, target_instance)` is checked in the
capability/CLI handlers: in-scope → proceed + increment; out-of-scope →
`needs_operator_confirm` (Launcher surfaces a confirm, same pattern as `chat_busy`).
Operator actions bypass. Own-vs-others uses the instance's `spawned_by` provenance.

---

## Binding to the entity model (the missing translation table)

| Blueprint-flow term | Entity-model term ([01](01-architecture.md)) |
|---|---|
| Goal / `MissionPlan` owner | the **Neko operator chat** that owns the goal |
| `slot` binding → persona | a **worker instance** = a **level placement** (visible/steerable) |
| `RUN_SLOT(slot_id)` | spawn-or-resume that worker's **level instance + chat** |
| `StageStatus` / proof gate | the worker chat's **soft task HUD** — advisory, conductor-read |
| stage `intervention` outcome | **escalation to the goal owner** (Neko chat), not a dead-end |
| blueprint (template) | the flow the goal-owner chat runs; slots staff sub-agent instances |

When `RUN_SLOT` spins up a worker (`ticker.py`), it **ensures the canonical
`PersonaInstance` placement** for that persona and sets `goal_id` + `spawned_by =
<coordinator>`, so the snapshot renders it as an **attributed level node** — the
steerable sub-agent tree, not a black box. `PersonaInstance` gains `spawned_by` +
`goal_id`; `persona_instance_summary` surfaces them.

---

## Build order (remaining)

The schema, store, instantiate, resolve, routing, the bundled blueprints
(`one_agent_smoke`, `two_agent_build_verify`, `neko_dev_qa_basic`,
`frontend_backend_join`, `visual_ui_qa`, `full_production_flow`), `matrix-run`, run
records, and the snapshot projection are **shipped**. What remains is the collapse and
deletion ([03](03-retirement-ledger.md)):

1. **Collapse entry points onto the graph.** `MissionRuntimeController._create_task`
   (`goal_runner.py`) instantiates the default blueprint onto `task.mission_plan` so
   `has_typed_plan` is true from birth; `goal run` becomes `blueprint run` with a
   default blueprint; `tick`/`daemon` call the same `TickEngine` — one flow, three
   depths.
2. **`RUN_SLOT` spawns an attributed level instance** (provenance above).
3. **Living-steer verbs + permission scope** (`CoordinatorPermissionScope` + gate).
4. **Stage-shaped agent contract** (templated first message + stage-shaped HUD).
5. **Legacy deletion** — delete the `TaskState` ladder, the `has_typed_plan` fork, the
   launcher cross-stack special cases, role-shaped HUD, and `_STAGE46_REQUIRED_SKILLS`.
   Tracked in [03](03-retirement-ledger.md).

---

## Test & skill discipline

- **Tests land in the same change as the code.** No stage is "done" with red or stale
  tests; a behavior this doc changes must have its pinning test *rewritten in the same
  diff* — never deleted to go green.
- **Every hard invariant has a guarding test** (chat-swap orphan, instance-as-template
  leak, legacy-ladder reachable, proof-gate crash, silent duplicate).
- **Headless-first; deletions are grep-gated.** Each stage validates from the shell
  (`python -m pytest tests/agent_runtime -q`; `flutter analyze <changed paths>` for UI).
  Where a stage deletes a symbol, an `rg` returning zero hits outside tests/migrations is
  part of the gate. MCP is required **only** when visual/native proof is the explicit
  acceptance (Mission Control pixels, Launcher navigation).
- **Skills update in lockstep.** A skill lands with the contract change it describes
  (e.g. proof becomes advisory → `harness-qa-verdict` cannot still say "block release").
  Editing a skill bumps `skill_manifest_hash`; "done" includes re-installing into bound
  profiles (`hermes harness install-stage46-skills`). Keep the HUD/skill split: closed-
  choice options in `agent_hud`, full rules in the skill.

---

## AAA non-negotiables (retained, refined)

- State transitions are explicit and tested.
- The harness, not the model, owns proof **validation** — but the **goal owner**
  (Neko chat), not a per-task gate, *adjudicates*, and an unmet check **escalates**
  instead of dead-ending. (`proof_gates.py` computation preserved.)
- Dev claims are backed by commits/diff/test output; UI/visual QA requires passed tests
  plus screenshot/video evidence.
- Process failures are incidents/retryable run failures, not product task failures.
- No duplicate recovery spam; recover in place where possible.
- No Claude/CLI wrappers in the core design — GPT personas are first-class Hermes actors
  invoked through Hermes' model/tool runtime.
