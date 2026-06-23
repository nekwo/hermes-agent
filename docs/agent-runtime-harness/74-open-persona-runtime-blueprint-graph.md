# Stage 74 - Open Persona Runtime Blueprint Graph

> **Superseded (entity model) by [Stage 76](76-unified-template-instance-chat-goal-model.md), locked 2026-06-22.**
> Stage 74's four-object model (Template → Instance → Runtime Loop → Work Binding)
> is replaced by Template → durable Level Instance (placement) → swappable Chat →
> Goal/Task owned by the chat. Keep Stage 74's `available_personas` library
> contract and `profile:<id>` namespace; read the object model here as historical.
>
> Capture of the Mission Control/Harness direction discussed on 2026-06-20.
> This stage moves Mission Control from a fixed four-agent cockpit toward an
> open runtime workspace where Hermes profiles, persona templates, agent
> instances, and Harness loops can be composed visually.
>
> Repos:
> - Harness: `X:/Eternia/hermes-agent`
> - Launcher: `X:/Unreal Engine/Engine/Launcher/EterniaLauncher`
> - Runtime profiles: `X:/Eternia/.hermes/profiles`

## Product intent

Mission Control should become a blueprint-style operations surface:

- Operators can see every Hermes persona/profile available on the machine.
- Operators can place personas into the Mission Office as templates.
- Operators can create one or more live instances from a persona/profile.
- Agents and Harness runtimes can exist without an active task or goal.
- Any instance can later be bound to a task, goal, lane, stage, or Harness loop.
- Multiple Harness loops can exist side by side, each with its own runtime state,
  worker sessions, agent instances, logs, proofs, and lifecycle controls.

The mental model is not "one hard-coded Neko, one Dev, one Backend Dev, one QA".
The model is:

```text
Persona/Profile Template -> Agent Instance -> Runtime Loop -> Work Binding
```

Each layer should be visible and controllable without forcing the next layer to
exist.

## Conversation capture

Requested immediate Mission Office behavior:

- Add an **Open Dev Agent Console** button on each placed agent row in Scene Palette.
- When a row or scene agent is highlighted, the matching agent should visibly glow
  in the viewer.
- Fix the mismatch where Scene Palette shows `backend_dev • agent`, but the viewer
  floating label only says `Dev Agent`.
- The viewer label must resolve the exact placed persona display name, not a stale
  initial model or generic role label.

Observed persona/runtime question:

- Alice and Neko are not currently two separate Harness agents.
- `neko_supervisor` is the configured Harness persona.
- That persona uses the Hermes profile `alice`.
- Therefore Neko is currently the live Harness role, while Alice is the backing
  profile/persona identity.

Current configured Harness agents observed from snapshot:

- `neko_supervisor` - display `Neko Mission Lead`, role `alice_supervisor`,
  profile `alice`.
- `dev` - display `Launcher Dev Agent`, role `dev`, profile `gpt-launcher`.
- `backend_dev` - display `Backend Dev Agent`, role `dev`, profile `backend-dev`.
- `qa` - display `QA Agent`, role `qa`, profile `launcher-qa`.

Hermes profiles observed under `X:/Eternia/.hermes/profiles`:

- `alice`
- `alice-img`
- `aliceimagecron`
- `backend-dev`
- `brain-writer`
- `claude_backend`
- `claude_launcher`
- `claude_launcher_qa`
- `gpt_backend`
- `gpt-launcher`
- `launcher-qa`
- `launcher-qa-direct`
- `pm`
- `reviewer`
- `spark_backend`
- `spark_docs`
- `spark_launcher`
- `spark_logreader`

Desired next behavior:

- Mission Control should show all personas/profiles, not only configured Harness
  agents.
- Profile-backed personas should be spawnable templates.
- Harness runtimes should also be instanceable.
- Agents can run independently in multiple instances.
- Tasks and goals become optional bindings, not prerequisites for existence.

## Key distinction

Do not overload "agent" to mean everything. The system needs four separate object
types.

### Persona/Profile Template

A reusable definition backed by a Hermes profile, config, role defaults, skills,
model/provider settings, and display metadata.

Examples:

- `alice`
- `gpt-launcher`
- `backend-dev`
- `launcher-qa`
- `spark_docs`
- `reviewer`

Templates are not inherently running.

### Agent Instance

A live or saved instantiation of a persona/profile template. Instances can be
free-floating, idle, chatting, assigned to a task, assigned to a runtime lane, or
closed/archived.

Examples:

- `alice#1` free-floating operator chat.
- `backend-dev#2` attached to a backend implementation stage.
- `reviewer#1` attached to a proof review lane.

Instances should not require a task or goal.

### Runtime Loop

A Harness-controlled execution loop with lifecycle, scheduler state, incidents,
proofs, worker sessions, event history, and lanes. A runtime loop may have zero,
one, or many agent instances attached.

Examples:

- A foreground goal runtime.
- A background investigation runtime.
- A sandbox/profile-test runtime with no task.
- A blueprint-created runtime loop containing a custom set of agents.

### Work Binding

A relationship that connects an instance or runtime to work. Work can be a task,
goal, stage, lane, proof request, operator chat thread, or future graph node.

Work bindings are optional and reversible. They should not define identity.

## Contract direction

Hermes snapshot should expose both configured live agents and available persona
templates.

Current shape:

```json
{
  "agents": [
    {
      "persona_id": "backend_dev",
      "display_name": "Backend Dev Agent",
      "role": "dev",
      "hermes_profile": "backend-dev",
      "profile_readiness": "ready"
    }
  ]
}
```

Desired additive shape:

```json
{
  "agents": [],
  "available_personas": [
    {
      "persona_id": "alice",
      "display_name": "Alice",
      "role": "profile",
      "hermes_profile": "alice",
      "source": "hermes_profile",
      "template_only": true,
      "profile_readiness": "available"
    }
  ],
  "persona_instances": [],
  "runtime_instances": []
}
```

Rules:

- `agents` remains the configured Harness team.
- `available_personas` is the broader library of profile-backed templates.
- `persona_instances` remains live/saved instance truth.
- `runtime_instances` should become the live/saved Harness loop truth.
- Launcher can merge `agents` and `available_personas` for the palette library,
  but should preserve the distinction in diagnostics and creation flows.

## Launcher direction

Mission Control should visually separate:

- **Library**: templates that can be placed or instanced.
- **Placed**: scene objects currently arranged in Mission Office.
- **Instances**: running or saved agent instances.
- **Runtimes**: Harness loops, lanes, and lifecycle controls.
- **Bindings**: links between instances, runtimes, and work.

Scene Palette should include all profile templates. Adding a template to the
scene should place a visual actor, not automatically imply that a Harness worker
is running.

Agent rows should show a direct console action when the placed persona can be
opened in the Agent Console. The action should route to either:

- an existing instance for that persona, or
- a create/open flow for a new free-floating instance.

Highlighted scene rows should glow both in the palette and in the viewer.

## Blueprint graph direction

The eventual graph editor should allow operators to compose runtime topology.

Expected node types:

- Persona/Profile Template
- Agent Instance
- Harness Runtime Loop
- Task/Goal
- Stage/Lane
- Proof Request
- Tool/Capability
- Memory/Context Bundle
- Human Approval Gate

Expected edges:

- instantiate template
- attach instance to runtime
- bind instance to task/goal
- route stage to persona/role
- require proof before QA
- handoff output to another persona
- gate execution on operator approval

The graph should be executable, but also inspectable while idle. Building the
graph must not require a task to already exist.

## Implementation stages

### Stage 74A - Snapshot exposes persona templates

Harness emits `available_personas` from Hermes profiles in addition to configured
`agents`.

Acceptance:

- Snapshot includes configured agents unchanged.
- Snapshot includes every Hermes profile as a template entry.
- Template entries are marked with `source` and `template_only`.
- Existing consumers that only read `agents` continue working.

### Stage 74B - Launcher consumes templates

Launcher bridge merges `agents` plus `available_personas` into the Mission Office
library while keeping configured agents distinct from profile-only templates.

Acceptance:

- Scene Palette shows all personas/profiles.
- Placing `alice`, `reviewer`, `spark_docs`, or `claude_launcher` works visually.
- Configured Harness agents still show readiness and runtime details.
- Profile-only templates do not falsely claim active worker state.

### Stage 74C - Instance creation flow

Mission Control adds an explicit create/open instance flow from a placed persona
or palette template.

Acceptance:

- Operator can create a free-floating instance with no task.
- Operator can open console for an existing instance.
- Multiple instances can be created from the same profile.
- Instances have stable ids separate from profile/persona ids.

### Stage 74D - Runtime loop templates

Harness exposes runtime loop creation separate from task creation.

Acceptance:

- Operator can create an idle Harness runtime loop.
- Runtime can exist with zero attached tasks.
- Runtime can attach one or more persona instances.
- Runtime can later bind to a goal or task.

### Stage 74E - Blueprint graph prototype

Mission Control adds a graph workspace for composing templates, instances,
runtimes, and work bindings.

Acceptance:

- Graph can place template and runtime nodes.
- Graph can create instance edges from templates.
- Graph can attach instances to runtime loops.
- Graph can bind or unbind a runtime from a task/goal.
- Snapshot remains the source of truth after every mutation.

## Non-goals for the first patch

- Do not make every Hermes profile a configured Harness agent.
- Do not start workers automatically just because a template appears in the UI.
- Do not require a task/goal before opening a persona console.
- Do not collapse Alice and Neko into one UI object without explaining the
  configured persona vs backing profile relationship.
- Do not make the blueprint graph before the snapshot contract can represent its
  objects cleanly.

## Verification plan

Harness:

```powershell
python -m pytest tests/agent_runtime/test_snapshot.py
python -m hermes_cli.main harness snapshot --json
```

Launcher:

```powershell
flutter analyze lib/features/mission_control/data/mission_control_bridge.dart lib/features/mission_control/data/mission_control_snapshot.dart lib/features/mission_control/office
flutter test test/features/mission_control/mission_office_page_test.dart test/features/mission_control/mission_office_layout_test.dart
```

Visual proof for UI changes:

```powershell
flutter build windows --debug --target lib/main_marionette.dart
powershell.exe -NoProfile -ExecutionPolicy Bypass -File docs/stages/qa-reboot/scripts/Test-StageCAppTabMcpE2E.ps1 -Tab mission-control -ArtifactRoot <artifact_dir> -ScreenshotScenarioLabel stage74_persona_templates
```

## Open decisions

- Should profile template ids use raw profile ids, `profile:<id>`, or a separate
  stable template id namespace?
- Should `alice` appear separately from `neko_supervisor` when Neko is currently
  backed by the Alice profile?
- Should free-floating instances live in the same store as task-bound persona
  instances, or in a separate operator-session store with promotion into runtime
  instances?
- Should runtime loop templates be versioned/saved like blueprints?
- Should graph execution create all nodes immediately, or compile into a plan that
  the operator reviews before instantiation?

## Working stance

The next narrow patch should expose persona templates first. That gives Mission
Control the full library of Hermes identities without changing runtime behavior.
Once the UI can see and place every persona/profile, follow-up stages can safely
add instance creation, runtime loop creation, and graph execution.
