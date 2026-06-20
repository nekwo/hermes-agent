# Stage 75 - Persona Library and Console Interactibility

> Implementation plan that grounds Stage 74 (Open Persona Runtime Blueprint Graph)
> in the runtime as it actually exists today. Stage 74 reads as a greenfield
> blueprint; this stage is the audit-backed execution path: ship the one real
> contract gap and the concrete interactibility wins first, and explicitly defer
> the model surgery and the graph editor until they earn their way in.
>
> Repos:
> - Harness: `X:/Eternia/hermes-agent`
> - Launcher: `X:/Unreal Engine/Engine/Launcher/EterniaLauncher`
> - Runtime profiles: `X:/Eternia/.hermes/profiles`

## Audit: what already exists

Stage 74 describes a four-object model (Template -> Instance -> Runtime Loop ->
Work Binding) as if none of it exists. Most of it does. Verified against the
current checkout:

| Stage 74 object | Reality in code | Status |
|---|---|---|
| Persona/Profile Template | Only the four configured `agents` are emitted (`agent_runtime/snapshot.py` `build_snapshot`/`_agent_summary`). The 18 profiles under `.hermes/profiles` are invisible to Mission Control. `hermes_cli/profiles.py:list_profiles()` already returns name/model/provider/skill_count/description per profile. | Missing |
| Agent Instance | `PersonaInstance` already supports `mode in {"chat","free_floating"}` and `current_task_id=None` (`agent_runtime/persona_assignments.py`). But instances are derived from active workers (`derive_from_workers`), not created/persisted as idle objects. | Partial |
| Runtime Loop | `GoalRuntimeInstance` exists with lanes + a lifecycle state machine (`agent_runtime/runtime_instances.py`). But `create_foreground`/`create_lane` require a `task_id`; there is no taskless idle loop. | Task-coupled |
| Work Binding | `PersonaAssignment` already binds persona to task/stage/kind, including a `free_floating` kind. | Done |

Conclusion: `available_personas` (Stage 74A) is the only genuinely missing
contract piece, and it is the linchpin. Every visual behavior Stage 74 wants
(place any persona, open its console, glow sync) is gated behind the palette
being able to see the full library. Today it sees 4 of ~22 identities.

## Working stance

This stage honors the standing "remaster the cathedral" stance: chat-first
foundation, harness as escalation layer, stop over-building orchestration before
chat works. Operator chat is already live and verified
(`agent_runtime/persona_runtime.py:chat_reply`).

Therefore:

- The blueprint graph (Stage 74E) is **explicitly out of scope** here. It is a
  topology editor for objects we cannot yet usefully manipulate; building it now
  is the over-building the stance warns against.
- Taskless runtime loops (Stage 74D) are **deferred**. They require changing the
  task-coupled `GoalRuntimeInstance` model for a payoff that nothing consumes
  until the cheaper wins land.
- We ship the library contract and the concrete console/label/glow asks first,
  because they make Mission Control more interactible today and ride on the chat
  layer that already exists.

## Closed decisions (previously open in Stage 74)

- **Template id namespace:** use `profile:<id>`. Never reuse raw profile ids as
  template ids, so templates can never collide with a configured `persona_id` in
  the merged palette.
- **Alice vs Neko:** show the full profile library; do not collapse or duplicate.
  Add a `backs_persona_id` backref on each template so the UI can render
  "Alice (backing Neko Mission Lead)" instead of hiding the relationship.
- **Free-floating instance store:** reuse the existing `PersonaInstance`
  `free_floating` mode rather than inventing a separate operator-session store.
  Promotion into task-bound work already exists via `PersonaAssignment`.

## Slice 75A - Snapshot exposes the persona library

Additive only. The harness emits `available_personas` from the Hermes profile
library in addition to configured `agents`. No existing field changes.

Implementation anchors:

- Source data lives in `hermes_cli/profiles.py`, but **do not call `list_profiles()`
  from `build_snapshot`**. That function runs `_check_gateway_running()` (per-profile
  PID probe) and `_count_skills()` (per-profile recursive `rglob`) for every profile;
  at ~18 profiles on every snapshot build that is needless latency. Instead add a
  lightweight enumerator (e.g. `available_profile_templates()` in profiles.py, or a
  local helper) that iterates `_get_profiles_root()`, reads `config.yaml` model/provider
  via the existing `_read_config_model`, and `profile.yaml` description via
  `read_profile_meta` — skipping the gateway-PID and skill-count work.
- Emit in `agent_runtime/snapshot.py:build_snapshot` as a new top-level
  `available_personas` list, built by a new `_available_persona_summary` helper.
- Each entry: `persona_id = "profile:<name>"`, `display_name`, `role: "profile"`,
  `hermes_profile`, `source: "hermes_profile"`, `template_only: true`,
  `profile_readiness: "available"`, plus `description` when present.
- Cross-reference: when a configured agent's `hermes_profile` matches a profile,
  set that template's `backs_persona_id` to the configured `persona_id`
  (e.g. the `alice` template gets `backs_persona_id: "neko_supervisor"`).
- Guard cost: `list_profiles()` touches disk; build the list once per snapshot
  and tolerate a bad/missing profile dir by emitting `[]` (never raise).

Acceptance:

- Snapshot includes configured `agents` unchanged (byte-stable for existing keys).
- Snapshot includes every Hermes profile as an `available_personas` entry.
- Template entries carry `source`, `template_only`, and `backs_persona_id` where
  a configured agent is backed by that profile.
- Template ids are `profile:<name>` and never equal a configured `persona_id`.
- Existing consumers that only read `agents`/`persona_instances` keep working.

Verification:

```powershell
python -m pytest tests/agent_runtime/test_snapshot.py
python -m hermes_cli.main harness snapshot --json
```

## Slice 75B - Launcher console, label, and glow

The three concrete asks captured in Stage 74. Independent of any new model; they
ride on the existing chat path. Launcher-side, against the snapshot from 75A.

1. **Open console action.** Each placed agent row in Scene Palette gets an
   "Open Agent Console" action. It routes to an existing chat/free-floating
   instance for that persona, or opens a new free-floating instance via the
   existing chat path. A `template_only` library entry opens a fresh instance;
   a configured agent opens its live console.
2. **Label resolution.** The viewer floating label must resolve the exact placed
   persona display name, not a stale initial model or generic role label. Fixes
   `backend_dev` (Scene Palette) showing "Dev Agent" in the viewer.
3. **Glow sync.** Highlighting a palette row glows the matching viewer actor, and
   vice versa.

Acceptance:

- Scene Palette shows all personas/profiles (configured + library) with the
  configured/template distinction preserved.
- Placing `alice`, `reviewer`, `spark_docs`, or `claude_launcher` works visually.
- Every placed row exposes a console action that opens the right instance.
- Viewer label matches the placed persona display name exactly.
- Selecting a row glows the corresponding actor in the viewer and back.
- Profile-only templates do not claim active worker state.

Verification:

```powershell
flutter analyze lib/features/mission_control/data/mission_control_bridge.dart lib/features/mission_control/data/mission_control_snapshot.dart lib/features/mission_control/office
flutter test test/features/mission_control/mission_office_page_test.dart test/features/mission_control/mission_office_layout_test.dart
flutter build windows --debug --target lib/main_marionette.dart
powershell.exe -NoProfile -ExecutionPolicy Bypass -File docs/stages/qa-reboot/scripts/Test-StageCAppTabMcpE2E.ps1 -Tab mission-control -ArtifactRoot <artifact_dir> -ScreenshotScenarioLabel stage75_persona_library
```

## Slice 75C - Operator-created idle instances (only after 75A/75B feel good)

First piece that needs real model work, so it earns its way in last. Promote
instances from purely worker-derived to operator-created idle objects that
persist while not running.

Implementation anchors:

- Extend `PersonaInstanceStore` with an explicit `create_free_floating(persona_or_template)`
  that allocates a stable instance id independent of profile/persona id and
  persists without a worker session.
- `derive_from_workers` continues to reconcile live workers; idle operator
  instances survive the reconcile rather than being dropped when no worker is
  active.
- Snapshot `persona_instances` includes idle operator instances.

Acceptance:

- Operator can create a free-floating instance with no task.
- Operator can open the console for an existing instance.
- Multiple instances can be created from the same profile.
- Instances have stable ids separate from profile/persona ids and survive when
  no worker is running.

Verification:

```powershell
python -m pytest tests/agent_runtime/test_persona_assignments.py tests/agent_runtime/test_snapshot.py
```

## UI Remaster Stages

The current Mission Office (verified from a live screenshot 2026-06-20) works but
is visually raw, and the rawness is the interactibility problem:

- **No layout system.** Command Deck (top-left), Evidence Stack (top-right),
  Next Action menu (bottom-left, overlapping the canvas), and Select Agent
  (bottom-right) are free-floating overlays. They collide and leave a large dead
  void in the center.
- **Empty agent slots.** Placed agents render as a tiny figure plus an *empty*
  rectangle — no name-in-card, role, profile, state, readiness, or action. The
  viewer label floats above and can read stale/generic (the Stage 74 bug).
- **No library.** Only the 4 configured agents appear; the ~18 profiles have
  nowhere to live on screen.
- **Leaked diagnostics.** "Snapshot contains unsafe fields; details suppressed"
  banners render as primary UI instead of a quiet diagnostic.

Launcher anchors: `office/mission_office_layout.dart` +
`mission_office_layout_store.dart` (placement), `office/mission_office_scene_model.dart`
+ `mission_office_scene_adapter.dart` (snapshot -> scene), `office/mission_office_game.dart`
(viewport actors), `data/mission_control_snapshot.dart` (parse, where 75A's
`available_personas` is read), `persona_actions/mission_persona_action_registry.dart`
(row/actor actions), `rail/mission_control_rail_tile.dart` (left nav).

Design stance: this is a bespoke sci-fi command-deck aesthetic, not a generic
dashboard — lean into it (dark canvas, signal-colored state, glow as meaning).
Remaster in dependency order; each stage stands alone and ships with visual proof.

### Slice 75D - Dock layout and panel system

Replace free-floating overlays with a deterministic dock so panels never overlap
and the center canvas is the focus.

- Define dock regions: left rail (exists), top status strip (runtime/tick/proof),
  right inspector column (Evidence + selected-object detail), bottom action bar
  (Next Action / quick controls). Center is the Mission Office canvas.
- Panels mount into regions via the layout store; persist collapse/expand per
  region. No panel renders over the canvas except transient menus.
- Demote "unsafe fields suppressed" to a quiet inline diagnostic chip, not a
  full banner.

Acceptance:

- No panel overlaps the canvas at any window size in the verification build.
- Top strip, right inspector, and bottom bar are stable docked regions.
- Diagnostic suppression notices are inline chips, not primary panels.
- `flutter analyze` clean on touched files; layout test passes.

### Slice 75E - Mission Office viewport remaster

Turn empty boxes into legible agent cards and give the floor real structure.

- Each placed actor renders a card: display name (resolved from the placed
  persona — fixes the Stage 74 label bug, shares the 75B resolver), role,
  backing profile, live state (idle/running/waiting/blocked), readiness dot, and
  an inline "Open Console" affordance.
- Visual state vocabulary: idle = dim, running = pulse, waiting = amber,
  blocked = red, selected/hovered = glow (shares 75B glow sync). State derives
  from snapshot `persona_streams` / `role_streams`, never invented client-side.
- Floor zones: configured team, free-floating instances, and (after 75F) the
  library shelf are visually distinct regions rather than scattered figures.
- Selecting an actor populates the right inspector with its detail + chat entry.

Acceptance:

- No empty agent rectangles; every placed agent shows name/role/profile/state.
- Viewer label matches the placed persona exactly (Stage 74 bug closed).
- State colors/animations are driven by snapshot fields only.
- Selecting an actor opens its inspector and console entry point.

### Slice 75F - Persona library shelf (consumes 75A)

Surface the full identity library in the office so any profile can be placed.

- A "Library" shelf/drawer lists `available_personas` from 75A, grouped by
  configured-vs-template, showing display name, profile, description, and the
  `backs_persona_id` relationship (e.g. "Alice — backing Neko").
- Placing a library entry adds a scene actor (template_only) without starting a
  worker; opening its console creates a free-floating instance (ties to 75C).
- Search/filter by name/role/description (description comes from `profile.yaml`).

Acceptance:

- Shelf shows every profile from the snapshot library, template vs configured
  clearly marked, with backref shown where present.
- Placing a template adds a non-running actor; no worker auto-starts.
- Filtering by name/role/description works.
- Opening a template console routes through the free-floating instance path.

UI verification (applies to 75D-75F):

```powershell
flutter analyze lib/features/mission_control
flutter test test/features/mission_control/mission_office_page_test.dart test/features/mission_control/mission_office_layout_test.dart
flutter build windows --debug --target lib/main_marionette.dart
powershell.exe -NoProfile -ExecutionPolicy Bypass -File docs/stages/qa-reboot/scripts/Test-StageCAppTabMcpE2E.ps1 -Tab mission-control -ArtifactRoot <artifact_dir> -ScreenshotScenarioLabel stage75_ui_remaster
```

## Deferred (tracked, not built here)

- **Stage 74D - taskless runtime loops.** Requires decoupling
  `GoalRuntimeInstance` from `task_id`. Revisit only when an operator workflow
  needs an idle Harness loop that nothing has asked for yet.
- **Stage 74E - blueprint graph prototype.** Revisit only after 75A-75C give the
  graph real, manipulable objects and the snapshot contract can represent them
  without further additions.

## Non-goals

- Do not make every Hermes profile a configured Harness agent.
- Do not start workers automatically because a template appears in the UI.
- Do not require a task/goal before opening a persona console.
- Do not build the graph editor before instances and runtimes are operable.
- Do not change existing snapshot keys; 75A is purely additive.
