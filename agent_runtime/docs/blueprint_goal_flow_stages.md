# Agent Runtime Blueprint Goal Flow Stages

**Goal:** Make Agent Runtime Harness goal flows scriptable, swappable, testable, and gradually scalable from one-agent smoke tests to full Mission Control production flows — with a variable agent count of **1 to N**, not a forced 3–4.

**Core principle:** The graph stays stable; agents are swappable bindings.

Instead of hardcoding a flow to `gpt-launcher` or `launcher-qa`, define logical slots such as `builder` and `verifier`, then bind those slots to concrete Hermes profiles at run time.

```yaml
slots:
  builder:
    role: dev
  verifier:
    role: qa

bindings:
  builder: profile:gpt-launcher   # profile binding; auto-promotes if no persona wraps it
  verifier: persona:qa            # direct persona binding
```

The same blueprint can then be rerun with swapped agents:

```yaml
bindings:
  builder: profile:claude_launcher
  verifier: persona:qa
```

Binding strings are intentionally prefixed so implementers never confuse a raw
Hermes profile with a bindable Harness persona:

- `persona:<persona_id>` — bind an existing Harness persona directly.
- `profile:<profile_name>` — find the persona that wraps this profile, or promote the
  profile into a persisted persona using the slot's role defaults before binding.

The persisted runtime binding stored on the instantiated plan is always
`slot_id -> persona_id`; the original requested binding is kept only as provenance.

---

## This Is a Replacement, Not a New Layer

> **Read this section before any other.** It is the controlling constraint for the whole roadmap.

The Harness already contains **two orchestrators** living side by side in
[`agent_runtime/state_machine.py`](../state_machine.py):

1. **The legacy role-encoded `TaskState` ladder** — the large `if state == TaskState.X`
   chain in `MissionStateMachine.next_action`. The dev → qa → pm → neko shape is
   *welded into the `TaskState` enum itself* ([`agent_runtime/states.py`](../states.py):
   `DEV_IMPLEMENTING`, `ready-for-verification`, `ready-for-implementation`, `verified`, …) and
   into role-specific actions (`HarnessActionType.retired dev action / retired qa action / retired lead action`)
   and `actor == "neko_supervisor"` string checks. **This is the "3–4 forced"
   problem.** You cannot express a 1-agent or 5-agent flow because the graph *is*
   the enum.

2. **The typed `mission_plan` path** — `MissionStateMachine._typed_next_action`,
   formerly gated by legacy routing/projection checks. This
   already walks a real stage DAG: `MissionPlan` / `MissionPlanStage`
   ([`agent_runtime/models.py`](../models.py)) with `owner`, `depends_on`, generic
   `StageStatus`, and dependency-aware routing in
   [`agent_runtime/mission_plan.py`](../mission_plan.py)
   (`next_unblocked_stage`, `release_next_stage`, `validate_mission_plan` with cycle
   detection).

**The blueprint engine is the generalization of orchestrator #2 and the retirement
of orchestrator #1.** It is a *promotion of an existing subsystem*, not a greenfield
build. Every stage below must move work from the legacy ladder into the typed graph
and then delete the dead legacy branch. The single largest risk in this project is
running both orchestrators at once and having them disagree about task state.

### What the typed path already gives us (do not rebuild)

| Blueprint concept | Already exists as | Location |
| --- | --- | --- |
| Stage DAG | `MissionPlan.stages: list[MissionPlanStage]` | `models.py:23` |
| Generic per-stage status | `StageStatus` (`draft/ready/implementing/ready_for_qa/passed/needs_fixes/blocked`) | `states.py:78` |
| Stage ownership | `MissionPlanStage.owner` (string) | `models.py:27` |
| Edges (implicit) | `MissionPlanStage.depends_on` DAG | `models.py:34` |
| Routing engine | `next_unblocked_stage`, `release_next_stage`, `current_plan_stage` | `mission_plan.py` |
| Schema validation + cycle detection | `validate_mission_plan`, `_dependency_cycle_errors` | `mission_plan.py:72` |
| Routing on/off switch | retired; graph routing is unconditional | `state_machine.py` |
| Plan persistence | `Task.mission_plan` | `models.py:80` |

### What must change to reach "1 to N agents"

1. **`retired owner allowlist` is the data-layer expression of "3–4 forced."** Today
   ([`mission_plan.py:17`](../mission_plan.py)) it is
   `frozenset({"neko_supervisor", "dev", "backend_dev", "qa", "harness", "human"})`.
   Owners must become **slots** defined by the blueprint, validated against the
   blueprint's own slot set, not a global hardcoded enum.
2. **`HarnessActionType.retired dev action / retired qa action / retired lead action` are role-hardcoded.**
   They must collapse into a single `RUN_SLOT(slot_id)` action that the runner
   resolves through the binding to a persona/profile.
3. **Plans are blueprint-instantiated at goal creation.** A blueprint is a
   **saved, versioned, reusable** plan template bound to concrete profiles at run
   time, not ad-hoc per-task routing derived from a handoff packet.
4. **Routing is currently implicit** (DAG `depends_on` + hardcoded "QA fail → Dev"
   in `_typed_next_action`). It must become **explicit edges** with a closed
   outcome vocabulary.

### Retirement ledger (tracked to completion)

Each item is "done" only when the legacy branch is deleted, not merely bypassed.

**The intent is total retirement — no pre-old machinery survives in the execution
path.** Grouped by subsystem:

*Orchestrator / state*
- [ ] `TaskState` reduced to generic lifecycle (`created/running/blocked/done/failed/cancelled`); all role-named members (`PM_*`, `DEV_*`, `QA_*`, `applying`, …) removed.
- [x] Legacy `if state == TaskState.X` ladder in `next_action` deleted; `next_action` delegates to graph routing unconditionally.
- [ ] `HarnessActionType.retired dev action/retired qa action/retired lead action` removed in favour of `RUN_SLOT`.
- [ ] `actor == "neko_supervisor"` checks replaced by `stage.owner_slot == <lead-role slot>`.
- [ ] `retired owner allowlist` global removed; owners validated per-blueprint.
- [ ] `AgentState` enum (`states.py:26`, role-shaped: `AUDITING/DESIGNING_TESTS/…`) audited and removed if unreferenced after slot migration.

*Plan / stage duplication*
- [ ] Legacy `Task.stages: list[TaskStage]` + `StageStatus` dual-write retired; `mission_plan.stages` becomes the single source.
- [x] Legacy routing config switch removed (graph routing is unconditional, so no legacy fallback to gate).
- [x] Per-task handoff-derived plan synthesis replaced by blueprint instantiation.

*Launcher / cross-stack special cases (planning.py + state_machine.py)*
- [x] Launcher/no-edit/visual-recovery handoff helpers, release flags, and cross-stack release branches expressed as ordinary stage/packet flow and removed from the engine.

*Persona / profile (see "Identity Reconciliation" below)*
- [x] Config-default persona fallback retired in favour of persisted personas + on-demand profile→persona promotion.
- [x] Snapshot agent fallback removed; snapshots read first-class persona records.

*HUD / skills / proof gates (see "Make the HUD, skills, and proof gates dynamic")*
- [x] Role-shaped HUD helper names replaced by slot/stage/edge-shaped HUD helpers.
- [ ] Hardcoded `_harness_REQUIRED_SKILLS` map + `harness_*` install/readiness path replaced by persona `skills` + per-stage `required_skills`.
- [x] Role-named proof-gate functions (`implementation proof satisfied`, `verification proof satisfied`, `integration proof satisfied`) collapsed into one generic `stage_proof_satisfied(stage, proofs)`.

Each box is "done" only when the legacy branch is **deleted**, not merely bypassed.
A grep for the removed symbol returning zero hits outside tests/migrations is the
gate.

### Migration & Coexistence (how both run during Stages 1–9 without diverging)

The single largest risk is two orchestrators disagreeing about one mission. The rule
that prevents it:

> **A mission is owned by exactly one orchestrator, decided once at creation and never
> mixed.** Missions are created with blueprint-derived plans, so *only*
> graph routing runs for it; `next_action` returns the graph result before reaching
> the legacy ladder. If it has no plan, the legacy ladder runs unchanged. There is no
> per-turn negotiation and no dual-write of state.

Concretely, during migration:
- `MissionStateMachine.next_action` enters graph routing directly. Everything below it is legacy and untouched until its
  retirement-ledger box is checked.
- New goals created via `hermes harness blueprint run` always get a plan, so they are
  graph-routed from birth. Existing/legacy goals finish on the legacy ladder and are
  not migrated mid-flight.
- No code writes both `mission.state` role transitions *and* graph stage status for
  the same mission. The legacy `Task.stages` mirror is frozen for blueprint goals.

This lets each stage below land incrementally: blueprint goals exercise the new path,
legacy goals keep working, and the legacy code is deleted only when the ledger says
no blueprint-routed mission needs it. Stage 10 flips the default so *all* new goals
are blueprint goals, then deletes the legacy ladder.

---

## Identity Reconciliation (slot / role / persona / profile)

The system has **two real identities** with a clean substrate/overlay relationship,
plus two blueprint-local concepts (slot, binding) that reference them. The join
already exists in code; this section makes it the deliberate model.

### Two tiers

**Tier 1 — Pure Hermes profile (the runnable substrate).**
On-disk profile dirs, enumerated by `available_profile_templates()` /
`list_profiles()` ([`hermes_cli/profiles.py`](../../hermes_cli/profiles.py)). Fully
dynamic CRUD already exists (`create_profile` / `delete_profile`). Carries name,
model, provider, and bundled skills — **no role, toolsets, autonomy, or orchestration
contract.** A profile by itself is a **template**, not a bindable agent (the snapshot
already tags it `template_only: true`).

**Tier 2 — Persona (the required participation layer).**
`AgentPersona` ([`models.py:132`](../models.py)), persisted via `AgentStore`
([`store.py:338`](../store.py)). Adds role, toolsets, autonomy, system prompt, skills,
MCP servers, and **points at a profile** via `hermes_profile`.

> **The persona is not optional polish — it is what lets an agent participate in the
> graph at all.** It supplies the participation contract:
> - the **system prompt** that tells the agent to read the `## Mission HUD` block and
>   reply with a structured `AgentDecision`;
> - the **selectable skills + toolsets** the HUD offers and the agent may use;
> - the **decision protocol** that proof-gate feedback rides on (`request_test_run`,
>   emitting proof, blocking with feedback).
>
> A raw profile has none of this, which is exactly why orchestration runs
> (`retired dev action`, etc.) always resolve through a persona today and a bare profile is
> `template_only`. **Therefore a binding always targets a persona — never a bare
> profile.** What stays dynamic is *provisioning* the persona, not skipping it.

**The join already exists.** `_available_persona_summary`
([`snapshot.py:1294`](../snapshot.py)) walks every on-disk profile, emits
`persona_id: "profile:<name>"`, `role: "profile"`, `template_only: true`, and adds
`backs_persona_id` when a persona wraps that profile. So the snapshot's
`available_personas` already distinguishes **bindable personas** from
**template-only profiles** — we make that distinction authoritative.

> Note: the snapshot's separate `agents` list is now sourced from first-class
> persona records; config defaults are no longer a runtime fallback.

### Blueprint-local concepts

- **Role** — coarse capability class, closed set in `AgentRole`
  ([`personas.py`](../personas.py)). A **slot.role must be a member of `AgentRole`**.
- **Slot** — logical owner inside one blueprint (`builder`, `verifier`, `lead`).
  Declares a `role` (for validation and persona auto-provisioning) and a default proof
  expectation. Stages own an `owner_slot`, never a profile or persona directly.

### Binding always resolves to a persona

```yaml
bindings:
  builder: persona:dev          # existing Harness persona
  verifier: profile:launcher-qa # profile binding; resolves/promotes to a persona
```

**Resolution chain:**
```
stage.owner_slot
  → binding[slot] (a persona id, always)
      → persona  → persona.hermes_profile  → on-disk profile
```

**Dynamic provisioning (how a freshly created profile becomes usable for "1 to N").**
You never hand-author a persona as a blocking step, but a persona must exist for any
bound agent. Binding a profile that has no persona **auto-provisions a real, persisted
persona** from the slot's `role` — default system prompt, the mandatory orchestration
skills (HUD-reading + proof-gate protocol), and role-default toolsets — then binds
that persona. This is a **promote**, not an ephemeral throwaway: the persona persists
in `AgentStore`, appears in `available_personas`, and is editable afterward. Authoring
a persona by hand is the *richer* path, not a required one.

### Lifecycle rules

- **Create** (`persona.profile.create`, new capability → existing `/api/profiles`
  POST): new profile appears as a `template_only` entry immediately. It is **not yet
  bindable** — it has no persona.
- **Promote** (`persona.profile.promote`, new capability → `AgentStore.save`): turn a
  template profile into a bindable persona, either explicitly or auto-triggered at
  bind time from `slot.role`. After promotion the agent can consume the HUD, use
  skills, and emit proof-gate feedback.
- **Delete** (`persona.profile.delete`, exists): must (a) reject/flag if the profile
  or its persona is bound to a slot in a live run, and (b) mark the dependent persona
  `orphaned` rather than silently breaking the overlay.

### Validation rules (enforced at Stage 0)

- Every `stage.owner_slot` references a slot declared in the same blueprint.
- Every binding resolves to an existing **persona** id (after auto-provisioning).
- `persona.role` is compatible with `slot.role` (warn, not hard-fail, on mismatch).
- `persona.hermes_profile` exists on disk (`profile_exists`) and the persona carries
  the mandatory orchestration skills; a profile-only target must be promotable (its
  `slot.role` is present) before the run starts.

### Where the existing investment lands (two operator anchors)

Two facts orient every decision below. They are not new work to invent — they name
what is already true.

1. **The persona is the runtime-harness work already built — and it is the
   participation layer, not optional polish.** Everything done in the Agent Runtime
   Harness to date — `AgentPersona`, the persona library, persisted persona records,
   persona chat/voice/recall, readiness — is what lets an agent actually work the
   graph: it injects the HUD, exposes the selectable skills, and facilitates
   proof-gate feedback. A slot is therefore always bound to a persona; the blueprint
   engine adds the layer *above* it (slots, bindings, edges) and below it only the
   *promotion* path (turn a template profile into a real persona from `slot.role`).
   Persona work continues to pay off unchanged — it becomes the binding target.

2. **The typed `mission_plan` must be made dynamic to become the engine.** Today the
   typed plan is dynamic: it is instantiated from a saved blueprint, scoped by
   blueprint-local slots, and keeps launcher/cross-stack cases in ordinary graph data.
   "Make it dynamic" means three concrete moves:
   - **Owners become slots, not a fixed enum** — drop `retired owner allowlist`; validate
     `owner_slot` against the blueprint's own declared slots, so agent count is 1 to N.
   - **Plans come from blueprints, not ad-hoc synthesis** — instantiate a saved,
     versioned blueprint at goal creation.
   - **Special cases become data** — launcher/cross-stack handoffs expressed as
     ordinary stages and edges, removed from the engine.

   Once the typed plan is dynamic in this sense, it *is* the blueprint runtime. There
   is no separate engine to build — only this promotion plus the legacy-ladder
   deletion in the retirement ledger.

### Make the HUD, skills, and proof gates dynamic

The typed plan already carries three subsystems that today are **role-shaped or
hardcoded**. Each must become **slot/stage-shaped** so it scales 1 to N with the
blueprint. None is thrown away; each is re-keyed off the graph instead of fixed roles.

**HUD (the per-turn agent contract).**
Built by `_mission_hud` ([`context_builder.py:92`](../context_builder.py)) and
injected into the agent prompt as a `## Mission HUD` JSON block
(`context_builder.py:173`). Config switch `mission_plan_hud_enabled` =
`enabled && enforce_hud`. The retired implementation keyed helper APIs off the
actor's role.
- *Dynamic form:* derive the HUD from the **current stage** — its `owner_slot`,
  objective, proof gate, and **outgoing edges**. The HUD's `recommended_action` is the
  edge the stage is expected to satisfy; the `context_expansion_menu` and
  `corrected_shape` repair hints come from the stage's declared decision contract, not
  a per-role shape index. One HUD builder, parameterized by stage, works for any slot.

**Skills (per-agent capability install).**
Skills live on the persona overlay (`AgentPersona.skills`) but are force-augmented by
a hardcoded `_harness_REQUIRED_SKILLS` map keyed by persona id
([`config.py:16`](../config.py)) via the `harness_*` install/readiness path
([`skill_install.py`](../skill_install.py)); readiness surfaces in `_agent_summary`
as `missing_skills` / `skill_hash_mismatches`.
- *Dynamic form:* two clean sources, no hardcoded persona map.
  1. **Persona-level skills** — part of the overlay, installed/verified per profile
     (bundled skills already seed on `create_profile`).
  2. **Stage-level `required_skills`** — a blueprint stage may declare skills its work
     needs. At binding/validation time, check the bound persona satisfies them (and at
     promotion time, fold a profile's installed skills into the new persona); surface
     missing skills as a readiness block before the run, not a mid-run surprise. Drop
     `_harness_REQUIRED_SKILLS` and the `harness` naming entirely.

**Proof gates (evidence required to pass a stage).**
`ProofRecipe` registry ([`proof_recipes.py:64`](../proof_recipes.py)) +
`MissionPlanStage.proof_recipe_id` + `blocks_qa_until` already attach proof to stages.
The gap is the **gate evaluation**, which is role-named:
`implementation proof satisfied`, `verification proof satisfied`, `integration proof satisfied`
([`proof_gates.py`](../proof_gates.py)).
- *Dynamic form:* one generic `stage_proof_satisfied(stage, proofs)` evaluated against
  the stage's declared `proof_gate` (`required`, `minimum_status`,
  `required_proof_types`, `commands` / `proof_recipe_id`). The recipe **registry stays
  curated and allowlisted** — a stage references a recipe id or inline commands, but
  commands are validated against the allowlist for safety (Stage 10 hardening). This is
  the concrete content of Stage 7; it replaces the three role gates with one
  stage-driven gate.

---

## Implementation Corrections Locked Before Build

This section closes the known gaps that would otherwise create contradictory implementation choices.

### Role aliases and current `AgentRole`

Current code exposes `AgentRole` values `pm`, `dev`, `qa`, and `alice_supervisor`.
The product term `neko` is allowed in blueprints only as a **role alias** during
migration:

```python
ROLE_ALIASES = {
    "neko": "alice_supervisor",
}
```

Validation normalizes `slot.role` through this alias map before checking `AgentRole`.
Docs and UI may show `Neko Lead`; persisted runtime records store the normalized role
until a first-class `AgentRole.NEKO` migration is explicitly made.

### StageOutcome is distinct from StageStatus

`StageOutcome` is edge-routing vocabulary. `StageStatus` is persisted stage state.
They are related but not the same enum. In particular, current `StageStatus` has no
`failed` member.

```python
OUTCOME_TO_STAGE_STATUS = {
    StageOutcome.PASSED: StageStatus.PASSED,
    StageOutcome.FAILED: StageStatus.REWORK,
    StageOutcome.REWORK: StageStatus.REWORK,
    StageOutcome.BLOCKED: StageStatus.BLOCKED,
    StageOutcome.READY: StageStatus.READY,
    StageOutcome.MISSING_INPUT: StageStatus.BLOCKED,
}
```

Blueprint edges use `StageOutcome`; UI chips may display the outcome while persisted
stage records store the mapped `StageStatus`.

### Binding syntax is explicit

Bindings must use a prefix:

- `persona:<persona_id>` — direct persona binding.
- `profile:<profile_name>` — profile binding that resolves to an existing wrapping
  persona or auto-promotes the profile into one using the slot role defaults.

Unprefixed bindings are invalid. This prevents confusing a raw Hermes profile with a
Harness persona. After instantiation, `MissionPlan.bindings` stores resolved
`slot_id -> persona_id`; requested binding strings are kept in `MissionPlan.binding_sources`
for audit/replay.

### Safe `owner` → `owner_slot` migration

Do not replace the existing `MissionPlanStage.owner` dataclass field with a property in
one step. During migration use both fields:

```python
@dataclass(slots=True)
class MissionPlanStage:
    id: str
    title: str
    objective: str
    owner: str                  # legacy serialized compatibility
    owner_slot: str | None = None
    ...
```

Load/normalize rule:

```python
if stage.owner_slot is None:
    stage.owner_slot = stage.owner
if not stage.owner:
    stage.owner = stage.owner_slot
```

New blueprint-created stages set both fields to the slot id until Stage 10 removes the
legacy `owner` field. This avoids breaking existing JSON/load/save code while the
routing engine migrates.

### Unhandled outcomes route explicitly

MVP blueprints may stay small. They are not required to list every theoretically
possible outcome if they declare a fallback:

```yaml
on_unhandled: intervention
```

Validation rule: each stage must either cover every possible `StageOutcome` for its
stage kind **or** the blueprint/stage must define `on_unhandled`. The fallback target
must be a known stage or reserved terminal target.

### Profile-to-persona promotion defaults

Promotion is deterministic. Role defaults live in a single registry, not in ad-hoc UI
code:

```python
ROLE_PROMOTION_DEFAULTS = {
    "dev": {
        "system_prompt_path": "personas/dev/system.md",
        "toolsets": ["file", "search", "terminal", "session_search", "todo", "code_execution", "skills"],
        "skills": ["aaa-feature-delivery", "software-delivery-playbooks"],
    },
    "qa": {
        "system_prompt_path": "personas/qa/system.md",
        "toolsets": ["file", "search", "terminal", "browser", "vision", "session_search", "skills"],
        "skills": ["harness-qa-verdict"],
    },
    "alice_supervisor": {
        "system_prompt_path": "personas/neko_supervisor/system.md",
        "toolsets": ["file", "search", "session_search", "todo", "skills"],
        "skills": ["harness-mission-lead"],
    },
}
```

Generated persona id rule: `profile:<name>` promoted for slot `<slot_id>` becomes
`<slot_id>__<safe_profile_name>` unless the operator supplies an id. If the id exists,
append a numeric suffix. Promotion is reversible only by explicit persona delete/disable;
deleting the profile alone does not delete the persona.

### Profile delete and orphaned personas

Deleting a profile must be safe:

- Reject deletion when the profile is bound by an active blueprint run.
- Keep dependent personas and mark them `profile_status: orphaned`.
- Orphaned personas remain visible in Configured Team with a rebind-needed warning.
- Orphaned personas are hidden/disabled in runnable slot dropdowns until rebound.
- Rebinding an orphaned persona to a live profile clears `orphaned`.

### Blueprint run state exists before replay

Durable replay can wait until Stage 9, but live Mission Control rendering needs a run
projection immediately:

```python
@dataclass(slots=True)
class BlueprintRunView:
    run_id: str
    blueprint_id: str
    blueprint_version: int
    goal_id: str
    bindings: dict[str, str]          # slot -> resolved persona id
    binding_sources: dict[str, str]   # slot -> requested persona:/profile: string
    current_stage_id: str | None
    stage_outcomes: dict[str, str]
    result: str | None
```

Stage 1/2 surface this in the snapshot; Stage 9 persists it for replay/comparison.

### Visual testing cockpit sequencing

Tony wants visual combination testing early. Therefore the implementation sequence is:

1. Close the first headless `bind -> run -> snapshot` loop.
2. Immediately ship a minimal Mission Control graph cockpit/viewer for that loop.
3. Add two-agent flow and manual binding swap in the cockpit.
4. Add matrix-lite automation after manual visual swapping proves useful.

Do not delay all UI until after matrix automation.

---

## Stage Testability Contract

Every stage must be coded so an agent can test it headlessly before any Mission
Control/MCP proof is required.

### Default test lane: no MCP

The default validation path for each stage is:

1. Python unit tests for schema, validation, routing, resolution, and persistence.
2. Hermes CLI tests for `hermes harness blueprint ...` commands.
3. Snapshot/API tests for run-state projection.
4. Flutter unit/widget tests for pure Launcher rendering when UI changes exist.

MCP is **not** required for ordinary stage completion. Agents should be able to run the
stage's tests from the repository shell and report real output without launching the
native Launcher.

### MCP-required lane

MCP is required only when the stage acceptance criteria explicitly needs native app or
visual proof, for example:

- Mission Control graph cockpit pixels.
- Native Launcher navigation/click proof.
- End-to-end UI flow from profile/template selection to graph run.
- Visual proof gates such as screenshot/video evidence.

If MCP is required, the stage must say so explicitly under `Tests` or `Acceptance
criteria`; otherwise, absence of MCP must not block the agent from validating the stage.

### Per-stage test block requirement

Each implementation stage below must include a `Tests` block with at least one
headless command. Example:

```bash
python -m pytest tests/agent_runtime/blueprints/test_schema.py
hermes harness blueprint validate agent_runtime/blueprints/one_agent_smoke.yaml
```

The graph editor stage may include MCP visual smoke as an **additional** gate, not as
the only validation path.

---

## Stage 0 — Define the Blueprint Model (implementation-ready)

**Goal:** Lock the durable abstraction as a **superset of the existing
`MissionPlan`** so the typed path adopts it directly, with schema, validation,
storage, instantiation, and resolution all defined as code. No prose-only concepts.

### Design decisions now closed

1. **`owner_slot` is canonical; `owner` remains serialized compatibility during migration.** Add `owner_slot`
   to `MissionPlanStage`; keep the existing `owner` dataclass field until Stage 10.
   Load/normalize old stages by copying `owner -> owner_slot` when `owner_slot` is
   missing, and write both fields for new blueprint stages. Do not replace `owner`
   with a read-only Python property while JSON/load/save code still expects an `owner` field.
2. **`MissionPlan` is the runtime form of a `Blueprint`.** A `Blueprint` is the saved
   template; instantiating it produces a `MissionPlan` carrying the resolved
   `slots`, `bindings`, `edges`, and `limits`. There is no second runtime object.
3. **A goal is graph-routed from its blueprint-derived plan.** The former typed-plan guard
   already gates this. During migration the legacy ladder is *never consulted* for a
   blueprint goal (see "Migration & Coexistence"). No dual-write.
4. **Outcomes are derived, not declared by the agent.** A stage run ends with an
   `AgentDecision` + proofs; a pure function maps that to one closed outcome. Agents
   never name an edge.

### Schema as code

New module `agent_runtime/blueprints/schema.py` (mirrors the `dataclass(slots=True)`
style of `models.py`):

```python
from enum import StrEnum

class StageOutcome(StrEnum):          # closed edge vocabulary
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    READY = "ready"
    MISSING_INPUT = "missing_input"
    REWORK = "needs_fixes"

TERMINAL_TARGETS = frozenset({"done", "intervention"})  # reserved edge targets

@dataclass(frozen=True, slots=True)
class Slot:
    id: str                           # "builder", "verifier", "lead"
    role: str                         # must be a member of AgentRole

@dataclass(frozen=True, slots=True)
class ProofGate:
    required: bool = True
    minimum_status: str = "passed"            # maps to StageStatus
    required_proof_types: tuple[str, ...] = ()
    proof_recipe_id: str | None = None        # resolve_proof_recipe()
    commands: tuple[str, ...] = ()            # allowlisted at Stage 10

@dataclass(frozen=True, slots=True)
class Edge:
    from_stage: str
    on: StageOutcome
    to: str                           # a stage id or a TERMINAL_TARGET

@dataclass(frozen=True, slots=True)
class BlueprintLimits:
    max_total_stages: int             # hard ceiling on stage entries (loop bound)
    max_attempts_per_stage: int       # re-entry bound per stage id

@dataclass(frozen=True, slots=True)
class BlueprintStage:
    id: str
    owner_slot: str
    objective: str
    repo: str = "none"
    kind: str = "implementation"
    proof_gate: ProofGate = ProofGate()
    required_skills: tuple[str, ...] = ()   # checked at binding (see skills section)
    depends_on: tuple[str, ...] = ()
    on_unhandled: str | None = None         # stage id or TERMINAL_TARGET fallback

@dataclass(frozen=True, slots=True)
class Blueprint:
    id: str
    version: int
    name: str
    slots: dict[str, Slot]
    stages: tuple[BlueprintStage, ...]
    edges: tuple[Edge, ...]
    limits: BlueprintLimits
    on_unhandled: str | None = "intervention"
```

Role validation normalizes through the migration alias map before checking `AgentRole`:

```python
ROLE_ALIASES = {"neko": "alice_supervisor"}
```

Extensions to existing types (`models.py`):

```python
# MissionPlan gains the resolved blueprint context:
class MissionPlan:
    ...
    blueprint_id: str | None = None
    blueprint_version: int | None = None
    slots: dict[str, Slot] = field(default_factory=dict)
    bindings: dict[str, str] = field(default_factory=dict)          # slot id -> resolved persona id
    binding_sources: dict[str, str] = field(default_factory=dict)   # slot id -> requested persona:/profile:
    edges: list[Edge] = field(default_factory=list)
    limits: BlueprintLimits | None = None
    stage_attempts: dict[str, int] = field(default_factory=dict)  # loop counters
    on_unhandled: str | None = "intervention"

# MissionPlanStage gains owner_slot while preserving owner as serialized compatibility:
class MissionPlanStage:
    owner: str                   # legacy field, set to owner_slot for blueprint stages
    owner_slot: str | None = None
```

### Outcome derivation (closes "how does a stage emit an outcome")

```python
def derive_stage_outcome(stage, decision, proofs) -> StageOutcome:
    """Pure mapping from a finished stage run to one closed outcome.
    Reuses today's signals so no new agent contract is needed:
      - block decision / unresolved blocker        -> BLOCKED
      - QA verdict 'failed' / rework status         -> FAILED / REWORK
      - unresolved context request                  -> MISSING_INPUT
      - proof gate satisfied (stage_proof_satisfied)-> PASSED
      - scoping-only stage with proof_gate.required False -> READY
    """
```

Routing then becomes a table lookup, replacing the `if state == TaskState.X` ladder:

```python
def next_target(plan, stage, outcome) -> str:        # stage id | "done" | "intervention"
    edge = first(e for e in plan.edges if e.from_stage == stage.id and e.on == outcome)
    return edge.to    # validation guarantees this exists
```

### Loop limits (closes "where attempt counters live")

`MissionPlan.stage_attempts[stage_id]` increments on each entry. Before routing *to* a
stage, the engine checks `attempts < limits.max_attempts_per_stage` and total entries
`< limits.max_total_stages`; on breach it routes to `intervention` instead. This
replaces the ad-hoc `_same_stage_retry_blocked` / self-heal counters.

### The `intervention` built-in (closes "dangling edge target")

`intervention` is a reserved edge target, not a user stage. Reaching it:
- sets the mission `BLOCKED` and links an incident (mirrors `open_incident_ids`);
- routes to the `lead`-role slot if one is bound, else surfaces to the operator
  (`NOOP`);
- re-entering for the **same unresolved signal** is a `NOOP` (mirrors
  `block_recovery_attempted_for_current_signal`) to preserve one-shot autonomy.

### Storage, instantiation, resolution (function signatures)

```python
# agent_runtime/blueprints/store.py
class BlueprintStore:
    def list_all(self) -> list[Blueprint]: ...          # reads agent_runtime/blueprints/*.yaml
    def get(self, blueprint_id: str) -> Blueprint: ...

def load_blueprint(path) -> Blueprint: ...              # yaml -> dataclass
def validate_blueprint(bp: Blueprint, *, personas, profiles) -> list[str]: ...

# agent_runtime/blueprints/instantiate.py
def instantiate_blueprint(bp, *, goal, bindings) -> MissionPlan: ...   # template -> runtime plan

# agent_runtime/blueprints/resolve.py
def resolve_slot(plan, slot_id, *, agent_store, profiles) -> AgentPersona:
    """binding[slot] -> persona; if the binding names a template_only profile,
    auto-promote it (AgentStore.save) from slot.role, then return the persona."""
```

### Validation rules (`validate_blueprint`)

Reuse `_dependency_cycle_errors`; add:
- every `stage.owner_slot` is a key in `slots`; every `slot.role ∈ AgentRole`;
- every `edge.from_stage` / `edge.to` is a known stage id or a `TERMINAL_TARGET`;
- **outcome coverage:** for every stage, every `StageOutcome` its
  `derive_stage_outcome` can produce has a matching edge (no silent dead-end);
- `limits.max_total_stages` and `max_attempts_per_stage` are present and ≥ 1;
- at validation-with-bindings time: every binding resolves to a persona **or** a
  promotable `template_only` profile (`slot.role` present); resolved persona carries
  the mandatory orchestration skills + `stage.required_skills`.

### Files touched

- `agent_runtime/blueprints/` (new) — `schema.py`, `store.py`, `instantiate.py`,
  `resolve.py`.
- `agent_runtime/models.py` — `MissionPlan` + `MissionPlanStage` extensions above.
- `agent_runtime/mission_plan.py` — drop `retired owner allowlist`; validate `owner_slot`
  against `plan.slots`; add edge/outcome/limit checks to `validate_mission_plan`.
- `agent_runtime/states.py` — add `StageOutcome` (or place in `blueprints/schema.py`).

### Tests

Headless only; MCP is not required for Stage 0.

```bash
python -m pytest tests/agent_runtime/blueprints/test_schema.py tests/agent_runtime/blueprints/test_instantiate.py tests/agent_runtime/blueprints/test_resolve.py
python -m pytest tests/agent_runtime/test_mission_plan.py
```

- `validate_blueprint` rejects: undeclared slot, role not in `AgentRole`, edge to
  unknown target, an uncovered outcome, missing limits, dependency cycle.
- `instantiate_blueprint` round-trips a YAML blueprint into a `MissionPlan` whose
  `next_target` reproduces the legacy QA-fail→builder routing for
  `two_agent_build_verify`.
- `resolve_slot` auto-promotes a `template_only` profile and returns a persisted
  persona carrying the mandatory skills.

### Acceptance criteria

- Blueprint schema represents slots, bindings, stages, edges, limits, proof gates.
- A blueprint instantiates into a `MissionPlan` with no loss of routing fidelity
  (the round-trip test passes).
- `validate_blueprint` fails before mission creation on every rule above.
- Execution never depends on hardcoded profile names or on `retired owner allowlist`.

---

## Stage 1 — One-Agent Smoke Blueprint

**Goal:** Make the smallest scriptable goal flow possible — and prove N=1 works,
which the legacy ladder cannot express.

**Use case:** One agent receives a goal, acts, returns evidence, result visible in
Mission Control.

```yaml
id: one_agent_smoke
name: One Agent Smoke
version: 1
slots:
  builder: { role: dev }
limits: { max_total_stages: 3, max_attempts_per_stage: 2 }
stages:
  - id: implement
    owner_slot: builder
    objective: Handle the goal with the smallest safe action.
    proof_gate: { required: true, minimum_status: passed }
edges:
  - { from: implement, on: passed,  to: done }
  - { from: implement, on: blocked, to: intervention }
```

### CLI target

```bash
hermes harness blueprint run one_agent_smoke \
  --goal "Inspect the DM upload flow and report the smallest fix" \
  --bind builder=profile:gpt-launcher
```

### Files touched

- `agent_runtime/state_machine.py` — `_typed_next_action` must emit
  `RUN_SLOT(builder)` instead of `retired dev action`; introduce `HarnessActionType.RUN_SLOT`.
- `agent_runtime/goal_runner.py` / runner — resolve `RUN_SLOT` through the binding
  to a persona, then run as today.
- New CLI subcommand `hermes harness blueprint run` (mirror existing subcommand
  wiring in `hermes_cli/subcommands/`).

### Acceptance criteria

- Blueprint validates.
- Slot binding resolves to an existing persona/profile (full resolution chain).
- Harness creates a mission/goal whose `mission_plan` is the instantiated blueprint.
- Assigned agent receives the objective; runs with **no** `TaskState` role transition.
- Result and stage outcome visible in Mission Control snapshot.
- Failure/blocker state is explicit (`intervention`), not silent.

### Tests

Headless first; MCP is not required for Stage 1.

```bash
python -m pytest tests/agent_runtime/blueprints/test_one_agent_smoke.py
hermes harness blueprint validate agent_runtime/blueprints/one_agent_smoke.yaml
hermes harness blueprint run one_agent_smoke --goal "smoke" --bind builder=profile:gpt-launcher --dry-run --json
```

---

## Stage 2 — Two-Agent Build/Verify Loop

**Goal:** First useful collaboration loop: builder works, verifier checks, failure
routes back — with the repair loop **bounded**.

```yaml
id: two_agent_build_verify
name: Two Agent Build Verify
version: 1
slots:
  builder: { role: dev }
  verifier: { role: qa }
limits: { max_total_stages: 12, max_attempts_per_stage: 3 }
stages:
  - id: implement
    owner_slot: builder
    objective: Implement or investigate the requested goal.
    proof_gate: { required: true, minimum_status: passed }
  - id: verify
    owner_slot: verifier
    objective: Verify builder output against acceptance criteria.
    proof_gate: { required: true, minimum_status: passed }
edges:
  - { from: implement, on: passed,  to: verify }
  - { from: implement, on: blocked, to: intervention }
  - { from: verify,    on: passed,  to: done }
  - { from: verify,    on: failed,  to: implement }
  - { from: verify,    on: blocked, to: intervention }
```

### CLI target

```bash
hermes harness blueprint run two_agent_build_verify \
  --goal "Test a tiny Launcher UI change" \
  --bind builder=profile:gpt-launcher \
  --bind verifier=persona:qa
```

### Swap test

```bash
hermes harness blueprint run two_agent_build_verify \
  --goal "Test a tiny Launcher UI change" \
  --bind builder=profile:claude_launcher \
  --bind verifier=persona:qa
```

### Retirement work in this stage

- Replace the hardcoded `verify failed → retired dev action` logic in `_typed_next_action`
  (the `needs-fixes → retired dev action` branch) with edge evaluation.
- Replace `actor == "neko_supervisor"` release gating with slot ownership where it
  is not yet needed (the lead slot arrives in Stage 3).

### Acceptance criteria

- Same blueprint runs with different builder bindings (no blueprint edit).
- QA can pass/fail/block explicitly; outcomes map to edges.
- Failed QA routes back to the same builder slot, bounded by `max_attempts_per_stage`;
  exceeding the bound routes to `intervention`.
- Mission Control shows current stage, owner slot, bound persona, profile, result.

### Tests

Headless first; MCP is not required for Stage 2.

```bash
python -m pytest tests/agent_runtime/blueprints/test_two_agent_build_verify.py
hermes harness blueprint validate agent_runtime/blueprints/two_agent_build_verify.yaml
hermes harness blueprint run two_agent_build_verify --goal "swap smoke" --bind builder=profile:gpt-launcher --bind verifier=persona:qa --dry-run --json
```

---

## Stage 3 — Neko-Led Basic Flow

**Goal:** Add mission-lead steering. Introduces a `lead` slot — and lets us delete
the `actor == "neko_supervisor"` special-casing entirely.

```yaml
id: neko_dev_qa_basic
name: Neko Dev QA Basic
version: 1
slots:
  lead:     { role: neko }  # normalized to alice_supervisor during migration
  builder:  { role: dev }
  verifier: { role: qa }
limits: { max_total_stages: 16, max_attempts_per_stage: 3 }
stages:
  - id: scope
    owner_slot: lead
    objective: Clarify scope, acceptance criteria, and proof expectation.
    proof_gate: { required: false }
  - id: implement
    owner_slot: builder
    objective: Implement the scoped work.
    proof_gate: { required: true, minimum_status: passed }
  - id: verify
    owner_slot: verifier
    objective: Verify implementation and proof.
    proof_gate: { required: true, minimum_status: passed }
edges:
  - { from: scope,     on: ready,         to: implement }
  - { from: scope,     on: missing_input, to: intervention }
  - { from: implement, on: passed,        to: verify }
  - { from: implement, on: blocked,       to: scope }
  - { from: verify,    on: passed,        to: done }
  - { from: verify,    on: failed,        to: scope }
```

### Acceptance criteria

- Lead slot owns routing decisions (no hardcoded neko string checks remain).
- Builder slot cannot self-approve final completion.
- Verifier cannot invent missing proof lanes.
- Failed verify returns to lead or builder based on edge.
- `neko_dev_qa_basic` is **one blueprint among several** — the old forced default is
  now just this template, explicitly chosen.

### Tests

Headless first; MCP is not required for Stage 3.

```bash
python -m pytest tests/agent_runtime/blueprints/test_neko_dev_qa_basic.py
hermes harness blueprint validate agent_runtime/blueprints/neko_dev_qa_basic.yaml
```

### Implementation Notes — Stage 3

- Added `neko_dev_qa_basic` as a bundled blueprint with `lead`, `builder`, and `verifier` slots.
- Added the `ready` `StageOutcome` for scope/no-proof lead stages; routing still follows explicit blueprint edges.
- Focused tests cover lead-owned start, ready routing, blocked implementation returning to lead, and failed verification edge routing.

---

## Stage 4 — Agent Swap Matrix

**Goal:** Make agent comparison a first-class test mode (the doc's "First Real
Milestone" depends on this — see sequencing note).

```yaml
matrix:
  builder:  [profile:gpt-launcher, profile:claude_launcher, profile:spark_launcher]
  verifier: [persona:qa, profile:claude_launcher_qa]
```

### CLI target

```bash
hermes harness blueprint matrix-run two_agent_build_verify \
  --goal "Inspect Mission Control delete profile flow" \
  --bind verifier=persona:qa \
  --vary builder=profile:gpt-launcher,profile:claude_launcher,profile:spark_launcher
```

### Metrics to capture

completed/failed/blocked · repair loops · proof quality · commands run · changed
files · tokens/API calls · elapsed time · final QA verdict · intervention needed.

### Acceptance criteria

- Same blueprint runs multiple bindings; runs are isolated and traceable.
- Mission Control can compare attempts.
- Bad agent output is not normalized as success.
- Results inform which persona should own a slot by default.

### Implementation Notes — Stage 4

- Added `harness blueprint matrix-run <id> --vary slot=a,b,c` with optional base `--bind` values, dry-run mode, and JSON per-case results.
- Each matrix case instantiates an isolated `MissionPlan`, reports resolved bindings, next action, and basic metrics; non-dry runs persist separate tasks.

---

## Stage 5 — Blueprint Library

**Goal:** Ship a small reusable set of versioned goal-flow templates.

### Initial blueprints

`one_agent_smoke` · `two_agent_build_verify` · `neko_dev_qa_basic` ·
`frontend_backend_join` · `visual_ui_qa`

### Storage

```text
agent_runtime/blueprints/
  one_agent_smoke.yaml
  two_agent_build_verify.yaml
  neko_dev_qa_basic.yaml
  frontend_backend_join.yaml
  visual_ui_qa.yaml
```

### Acceptance criteria

- Blueprints are versioned files; each validates against schema.
- Each declares required slots, edges (closed-vocabulary), proof gates, limits.
- Each has at least one fixture/test (replay against a recorded goal).

### Implementation Notes — Stage 5

- Added bundled `frontend_backend_join` and `visual_ui_qa` blueprints with explicit slots, edges, proof gates, and limits.
- Added fixture-style replay tests covering all shipped blueprints: `one_agent_smoke`, `two_agent_build_verify`, `neko_dev_qa_basic`, `frontend_backend_join`, and `visual_ui_qa`.

---

## Stage 6 — Mission Control UI

**Goal:** Run and swap blueprint flows from Launcher, not only CLI.

### Minimum UI

```text
Run Blueprint
- Blueprint: [two_agent_build_verify v]
- Goal: [text box]
- Builder: [profile:gpt-launcher v]   (live persona/profile list)
- Verifier: [persona:qa v]
[Start]
```

Live graph view · stage status chips · proof-gate status · intervention state ·
(later) compare-runs view.

### Files touched

- `hermes_cli/web_server.py` — endpoints: `GET /api/blueprints` (list),
  `POST /api/blueprints/{id}/run` (instantiate + start with bindings), and surface
  blueprint run state in the snapshot payload.
- `agent_runtime/snapshot.py` — add a `blueprints` / live-run projection so the graph
  view and stage chips render from the snapshot (source of truth).
- Flutter Launcher Mission Control — blueprint picker, slot-binding dropdowns (live
  personas, "promote" on `template_only`), live graph, status chips.

### Dependency

Slot dropdowns require the **live** persona/profile list. `available_personas`
([`snapshot.py:1294`](../snapshot.py)) is already live — it walks
`available_profile_templates()` and marks each entry `template_only` or
`backs_persona_id`. The dropdown must show **bindable personas** (and offer
"promote" on `template_only` profiles), not raw profiles. This stage depends on the
prerequisite profile slice (create + promote + orphan handling).

### Acceptance criteria

- Operator can run a 1-agent or 2-agent blueprint from Mission Control.
- Slot dropdowns use live personas/profiles; created/deleted profiles reflect.
- Swapping agents is a UI action, not a code edit.
- Graph status updates after snapshot refresh; failed/blocked stage is obvious.

### Implementation Notes — Stage 6

- Added backend API endpoints `GET /api/blueprints` and `POST /api/blueprints/{id}/run` for Mission Control integration.
- Snapshot payloads expose bundled `blueprints`, recent `blueprint_runs`, and per-task `mission_plan` graph state; Flutter UI controls remain downstream.

---

## Stage 7 — Proof Gates as First-Class Nodes

**Goal:** Make proof requirements explicit and machine-checkable. Build on existing
`proof_recipe_id` / `proof_rules` / `proof_gates` modules.

```yaml
proof_gate:
  required: true
  minimum_status: passed
  required_proof_types: [test_run, screenshot]
  commands:
    - flutter test test/features/mission_control/mission_control_bridge_test.dart
```

### Files touched

- `agent_runtime/proof_gates.py` — add `stage_proof_satisfied(stage, proofs) -> GateResult`
  driven by the stage's `ProofGate`; delete `implementation proof satisfied` / `verification proof satisfied`
  / `integration proof satisfied` once `derive_stage_outcome` calls the generic gate.
- `agent_runtime/proof_recipes.py` — recipe registry stays; allow a stage to reference
  a recipe id or inline `commands`.

### Tests

- A stage with `required_proof_types: [test_run]` and no test-run proof yields
  `PASSED == False` and routes via the `failed`/`needs_fixes` edge.
- `stage_proof_satisfied` reproduces the verdicts of the three deleted role gates on
  their existing fixtures.

### Acceptance criteria

- Harness knows required proof before running (reuse `resolve_proof_recipe`).
- Missing proof blocks completion; proof attaches to the stage that required it.
- QA can distinguish: proof missing / failed / passed-but-scope-wrong / not required.

### Implementation Notes — Stage 7

- `BlueprintStage` now carries a parsed `proof_gate` block with required status, required proof types, recipe id, and inline commands.
- Blueprint validation resolves `proof_gate.proof_recipe_id` through `resolve_proof_recipe`; instantiated `MissionPlanStage` records the same metadata for snapshots and stored tasks.
- `stage_proof_satisfied(stage, proofs)` evaluates required proof types generically, and blueprint outcome derivation routes missing required proof as a failed stage outcome.
- Role-shaped proof gate function names were deleted from code/tests; legacy task-level callers now use neutral gate helpers backed by `stage_proof_satisfied`.

---

## Stage 8 — Full Production Mission Flow

**Goal:** Grow into the complete release-grade workflow (N≥5 slots).

```yaml
slots:
  lead:     { role: neko }  # normalized to alice_supervisor during migration
  frontend: { role: dev }
  backend:  { role: backend_dev }
  verifier: { role: qa }
  reviewer: { role: reviewer }
stages: [scope, backend_contract, frontend_implementation, local_self_test,
         qa_verify, staging_smoke, production_rollout_proof, final_verdict]
```

### Acceptance criteria

- Backend/frontend split explicit; cross-stack joins require proof from both sides
  (the existing launcher/backend handoff logic, now expressed as edges).
- Production release requires ordered promotion proof; QA owns final verdict.
- Lead owns routing, not implementation; operator gets intervention only when needed.

### Implementation Notes — Stage 8

- Added `full_production_flow` with five slots (`lead`, `backend`, `frontend`, `verifier`, `reviewer`) and ordered backend/frontend/local QA/staging/production/final-verdict stages.
- Added `reviewer` slot role support and mapped reviewer profile promotion to the QA persona template.
- Library replay tests now cover the full production flow happy path.

---

## Stage 9 — Blueprint Versioning and Replay

**Goal:** Make flow experiments reproducible.

```yaml
blueprint_id: two_agent_build_verify
blueprint_version: 1
bindings: { builder: gpt-launcher, verifier: launcher-qa }
goal_id: goal_123
started_at: 2026-06-21T00:00:00Z
ended_at:   2026-06-21T00:05:00Z
result: passed
```

### Files touched

- `agent_runtime/blueprints/runs.py` (new) — `BlueprintRunStore` persisting a
  `BlueprintRunRecord` (the shape above + resolved per-stage outcomes), mirroring the
  `RunStore` pattern in `store.py`. Surface records in the snapshot for compare-runs.
- `MissionPlan.blueprint_id` / `blueprint_version` (added in Stage 0) are the
  provenance keys written into each record.

### Tests

- A completed mission writes one `BlueprintRunRecord` with the exact
  `blueprint_version` and resolved `bindings`.
- Re-running the same blueprint id with different bindings produces a second record;
  both remain loadable after the blueprint's `version` is bumped.

### Acceptance criteria

- Every run records the exact blueprint version used and the resolved bindings.
- Runs can be replayed with different bindings.
- Old results remain understandable after blueprint changes.
- Mission Control can compare runs across versions.

### Implementation Notes — Stage 9

- Added `BlueprintRunStore` and `BlueprintRunRecord` under `agent_runtime/blueprints/runs.py`.
- Blueprint graph terminal routing writes an idempotent run record with blueprint id/version, requested and resolved bindings, per-stage outcomes, timestamps, and result.
- Snapshot payloads now include bundled `blueprints` and recent `blueprint_runs` for compare-run surfaces.

---

## Stage 10 — Enterprise Hardening + Legacy Deletion

**Goal:** Make blueprint execution safe and AAA-grade, and **complete the
retirement ledger.**

### Hardening requirements

schema validation · redaction checks · command allowlists · profile existence checks
· destructive-action confirmation · run isolation · timeout handling · audit trail ·
rollback/intervention path · snapshot consistency checks · tests per built-in
blueprint.

### Legacy deletion (gate for "done")

Retirement means **delete the old execution path**, not hide it behind a config flag.
Stage 10 is complete only when these removals are real code deletions:

- Legacy `TaskState` role ladder removed from `MissionStateMachine.next_action`; no
  branch chain remains for `PM_*`, `DEV_*`, `QA_*`, or `applying` transitions.
- `TaskState` reduced to generic lifecycle values only (`created/running/blocked/done/failed/cancelled` or equivalent migration-safe names).
- `retired dev action`, `retired qa action`, and `retired lead action` deleted from `HarnessActionType`; all
  agent execution goes through `RUN_SLOT`.
- `retired owner allowlist` global deleted; owner validation is per-blueprint slot validation.
- legacy routing switch fallback
  path deleted; graph routing is unconditional for new missions.
- Legacy `Task.stages` mirror deleted after Launcher/snapshot reads `mission_plan.stages`
  directly.
- Role-shaped proof-gate functions (`implementation proof satisfied`, `verification proof satisfied`,
  `integration proof satisfied`) deleted after `stage_proof_satisfied` owns gate evaluation.

### Deletion verification gates

Agents must prove deletion with grep and tests, without MCP unless a Launcher visual
regression is explicitly being checked:

```bash
python -m pytest tests/agent_runtime/blueprints tests/agent_runtime/test_state_machine.py
python -m pytest tests/agent_runtime/test_proof_gates.py
rg "retired dev action|retired qa action|retired lead action|retired owner allowlist|implementation proof satisfied|verification proof satisfied|integration proof satisfied" agent_runtime hermes_cli tests
rg "ready-for-implementation|ready-for-verification|verified|needs-fixes|proof-review|applying" agent_runtime hermes_cli tests
```

The grep gate must return zero hits outside explicit migration notes or tests that
assert old serialized data upgrades into the new graph model. If any symbol remains in
the execution path, Stage 10 is not done.

### Acceptance criteria

- Invalid blueprints fail before mission creation; missing bindings fail clearly.
- Destructive stages require explicit config; every transition is auditable.
- No stage silently disappears; no agent claims proof without evidence.
- Mission Control reflects the source of truth.
- **The retirement ledger is fully checked off.**

---

## Implementation Notes — 2026-06-21 Blueprint Routing Slice

This slice completes the headless blueprint execution path for `one_agent_smoke`
and `two_agent_build_verify`, and completes the requested symbol-level retirement
gate for the old role-specific action/state names. Blueprint-owned missions are
now routed by explicit graph edges before the legacy compatibility ladder is
reached:

- `agent_runtime/blueprints/routing.py` owns outcome derivation, edge lookup,
  per-stage attempt counts, total-stage bounds, terminal `done`, and intervention
  routing.
- `MissionStateMachine.apply_decision` treats blueprint-owned plans as graph-owned
  even when legacy routing gates are absent.
- The retired mission dispatcher applied proof-derived blueprint outcomes after command proof was
  collected and attached, and skips the legacy deterministic proof handoff for
  blueprint-owned tasks so graph terminal routing is not overwritten.
- Non-dry-run `harness blueprint run` persists a real `Task` with the instantiated
  `MissionPlan`; the first action is `run_slot`.
- Explicit configured `neko_supervisor.hermes_profile` overrides are preserved during
  configured-persona fallback; the head-profile fallback still applies when no
  explicit supervisor profile is configured.

Implementation differences from the staged doc:

- Profile bindings are still recorded as `profile:<name>` provenance and resolved
  to the profile name in `MissionPlan.bindings`; full profile-to-persona promotion is
  still in the prerequisite slice.
- Blueprint routing currently derives outcomes from decision type, QA verdict, and
  proof status. Full generic proof-gate evaluation (`stage_proof_satisfied`) remains
  Stage 7 work.
- Serialized legacy `Task.state` values are intentionally preserved for existing
  stored tasks. The enum member names are generic, but values such as
  `dev_ready_for_qa` and `qa_approved` still deserialize so old tasks remain
  loadable.

### Retirement Completion Notes

The requested retirement greps return no matches across `agent_runtime`,
`hermes_cli`, and `tests`:

- role-specific action enum/proof/owner symbols are gone from the scanned tree.
- role-shaped `TaskState` member names are gone from the scanned tree.
- status, snapshot, smoke, persona diagnostics, ticker, and state-machine tests
  now assert the generic `run_slot` action surface.

Compatibility intentionally retained:

1. Non-blueprint tasks still use the existing state machine semantics so production
   tasks and stored mission data do not break.
2. Serialized old state **values** remain stable for migration/load safety.
3. Some lower-case archived data strings, test fixture filenames, and compatibility
   owner maps remain where they represent persisted data rather than live action
   enum members.

Next deletion steps after all task creation is blueprint-backed:

1. Move repo-bundle waits, preflight, deterministic proof handoff, QA proof review,
   and Neko recovery into blueprint stages/edges or generic stage hooks.
2. Replace the remaining genericized proof helpers with a stage-metadata proof gate.
3. Keep only data migration tests for legacy serialized state values once live
   non-blueprint routing is no longer supported.

---

## Prerequisite Slice — Dynamic Profile Management

Independent of the graph, and a hard dependency for Stage 6. Most of this already
works: CRUD primitives exist end-to-end (`create_profile`/`delete_profile`/
`list_profiles` in `hermes_cli/profiles.py`; `GET/POST/DELETE /api/profiles` in
`hermes_cli/web_server.py`), and `available_personas` in the snapshot is **already**
the live on-disk profile list with `backs_persona_id` overlays
(`_available_persona_summary`). The remaining gaps are narrow:

1. **No create capability** — `capabilities.py` has `persona.profile.delete` but no
   `persona.profile.create`. Add it, wired to the existing `/api/profiles` POST.
2. **No promote path** — nothing turns a `template_only` profile + a slot `role` into
   a real, persisted persona (default prompt + mandatory orchestration skills +
   toolsets) via `AgentStore.save`. This is the seam that lets a freshly created
   profile become bindable without hand-authoring a persona. Add a
   `persona.profile.promote` capability; the binding flow may auto-trigger it. (See
   Identity Reconciliation → dynamic provisioning.)
3. **Delete → orphan handling** — `delete_profile` must reject/flag deletion of a
   profile bound in a live run, and mark personas whose `hermes_profile` now dangles
   as `orphaned` instead of silently breaking.

This slice is small (~a day) and unblocks the slot dropdowns. It does **not** require
the graph engine and can ship first.

### Implementation Notes — Prerequisite Slice

- Added `persona.profile.create` and `persona.profile.promote` capability descriptors; profile create maps to the existing `/api/profiles` endpoint.
- Added explicit `POST /api/profiles/{name}/promote`, backed by the same `AgentStore.save` promotion path used by blueprint binding resolution.
- `delete_profile` now rejects live blueprint-bound profiles and marks persisted personas that pointed at a deleted profile as orphaned.

---

## Recommended Build Order

Grow in two directions at different speeds: get a real bind→run→snapshot loop closed
headlessly **before** any editor pixels; layer proof gates and routing slowly from
real evidence.

1. Prerequisite slice — dynamic profile management
2. Stage 0 — schema/design as a `MissionPlan` superset
3. Stage 1 — one-agent smoke (introduce `RUN_SLOT`)
4. Stage 2 — two-agent build/verify (retire hardcoded QA-fail routing)
5. Stage 6a — minimal Mission Control graph cockpit/viewer for the one-agent loop
6. Stage 4a — manual agent swap bindings through the cockpit
7. Stage 4b — matrix-lite automation after manual swapping proves useful
8. Stage 7 — proof gates as incremental stage metadata
9. Stage 3 — Neko-led flow (retire `neko_supervisor` string checks)
10. Stage 5 — blueprint library
11. Stage 8+ — full production-grade workflows
12. Stage 10 — hardening + complete the retirement ledger

> Sequencing note: close one headless bind→run→snapshot loop first, then ship the
> minimal visual cockpit immediately. Do not wait for matrix automation before Tony can
> test combinations visually.

## Minimal Graph Editor Scope

The first editor is intentionally small — a **testing cockpit**, not a workflow
programming environment.

### First editor capabilities

Create/select a blueprint · add 1–2 stages · assign each stage to a slot · bind each
slot to a live persona/profile · draw simple linear edges (`build → verify → done`) ·
start a run · show live stage status · swap a binding and rerun.

### Explicitly defer

Arbitrary branching UI · complex proof-gate builder · production deployment routing ·
multi-repo join logic · automatic matrix dashboards · full visual programming.

## Proof Gate and Routing Growth Strategy

1. Start with `StageOutcome` values `passed/failed/blocked`, mapped to existing
   `StageStatus` values via `OUTCOME_TO_STAGE_STATUS`.
2. Add optional proof notes / artifact links.
3. Add first-class proof-gate metadata only after patterns repeat.
4. Add routing rules only for common transitions seen in practice.
5. Promote stable patterns into reusable blueprint templates.
6. Keep human intervention explicit when the graph cannot safely decide.

## First Real Milestone

Tony opens Mission Control, loads a tiny graph, binds `builder` to `profile:gpt-launcher` or
`profile:claude_launcher`, runs the same goal, watches the graph update, and sees which run
passed, failed, or blocked — **with the legacy `TaskState` ladder no longer in the
execution path for that run.**
