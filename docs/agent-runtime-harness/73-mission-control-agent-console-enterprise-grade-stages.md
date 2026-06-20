# Stage 73 - Mission Control Agent Console enterprise-grade upgrade

> Deep-audited implementation plan for making Mission Control's Agent Console
> and persona chat history feel AAA/enterprise-grade. This stage follows Stage
> 72 streaming operator chat and treats the operator channel as the primary work
> surface. Chat history and agent diagnostics remain available, but they should
> not dominate the first viewport.
>
> Repos:
> - Launcher: `X:/Unreal Engine/Engine/Launcher/EterniaLauncher`
> - Harness: `X:/Eternia/hermes-agent`

## Implementation status

Stages 73A through 73F are implemented in Launcher. Stage 73G has widget and
analyzer proof, but still requires a real Stage C screenshot/proof capture after
the next debug build because command proof cannot validate the final visual
hierarchy by itself.

Implemented Launcher changes:

- `MissionAgentInstancePicker` is now a compatibility wrapper over split widgets.
- `MissionAgentSelectorStrip` owns compact persona/agent selection.
- `MissionPersonaChatHistorySection` renders collapsed by default and expands to
  search/filter/open saved sessions.
- `MissionHarnessAgentConsoleList` renders compact agent rows with diagnostics
  collapsed.
- `_AgentsDrawerContent` now composes Agent Console as chat-first, with a responsive
  widened-drawer two-zone layout.
- Legacy/fallback `MissionAgentInstancePicker` call sites inherit the split wrapper
  and collapsed-history behavior.

## Audit verdict

The stages are viable, but they must be implemented in a stricter order than the
first sketch implied.

The current Launcher UI has one key architectural issue: `MissionAgentInstancePicker`
owns three concerns at once:

- persona instance selection chips;
- saved persona chat history;
- live Harness agent status cards with diagnostics.

`_AgentsDrawerContent` then places that full picker above `_MissionAgentOperatorChannel`
inside one vertical scroll. This is why Chat History can visually outrank the active
chat. The implementation-ready path is to first split the picker into smaller
surfaces, then promote chat, then collapse history and diagnostics.

Do not start by only styling cards. That would keep the wrong information
architecture and make the UI look more polished while still feeling operationally
backwards.

## Current code map

Launcher files that define the current behavior:

- `lib/features/mission_control/mission_control_page.dart`
  - `_MissionControlPageState` owns `_openDrawer`, selected goal, selected agent, and
    drawer width.
  - `_MissionCanvasShell` mounts `_MissionDrawerOverlay`.
  - `_MissionDrawerOverlay` and `_DrawerPanel` own the desktop drawer frame.
  - `_AgentsDrawerContent` stacks `MissionAgentInstancePicker` above
    `_MissionAgentOperatorChannel`.
  - `_MissionAgentOperatorChannel` wraps `MissionAgentChatPanel` in
    `SelectionContainer.disabled`; keep that protection.
- `lib/features/mission_control/agent_chat/mission_agent_instance_picker.dart`
  - `MissionAgentInstancePicker` is currently a combined picker, history list, and
    agent console.
  - `_PersonaChatHistoryList` renders history expanded by default.
  - `_AgentConsoleInstanceList` renders all agent cards.
  - `_HarnessAgentCard` already collapses diagnostics with `ExpansionTile`.
- `lib/features/mission_control/agent_chat/mission_agent_chat_panel.dart`
  - The active operator channel and streaming reply surface.
  - This should become visually dominant inside Agent Console, but its message and
    streaming logic should not be rewritten in Stage 73 unless a layout bug forces it.

Relevant tests:

- `test/features/mission_control/mission_control_canvas_drawer_test.dart`
- `test/features/mission_control/mission_agent_instance_picker_test.dart`
- `test/features/mission_control/mission_agent_chat_panel_test.dart`
- `test/features/mission_control/mission_control_page_test.dart`

## Non-goals

- Do not change Harness snapshot JSON unless the Launcher cannot derive a required
  display field from existing safe data.
- Do not change persona chat persistence, redaction, or streaming transport.
- Do not expose raw ids by default. Raw ids stay behind diagnostics/expanded detail.
- Do not make a new landing page, marketing hero, or decorative dashboard.
- Do not put cards inside cards. Use compact bands, rows, tabs, rails, or accordions.
- Do not remove Stage 72 streaming behavior or the stream/projected-message dedupe.

## Required design outcome

Default Agent Console viewport on desktop should read in this priority order:

1. Selected agent identity and current readiness.
2. Active operator chat with visible composer.
3. Compact agent/team navigation.
4. Collapsed Chat History.
5. Collapsed diagnostics/details.

Saved chat history is navigation, not the primary workspace. Agent diagnostics are
operational evidence, not the default conversation.

## Stage 73A - Resizable Agent Console drawer

Status: implemented in Launcher.

Purpose:

Give operators enough room to use the chat and console before the deeper layout
upgrade lands.

Implemented behavior:

- Desktop side drawers have a left-edge resize handle.
- Dragging left widens the drawer; dragging right narrows it.
- Width clamps to a readable minimum and viewport-safe maximum.
- Compact/mobile drawers remain full-width.
- Bottom terminal drawer keeps its bottom-sheet behavior.

Implemented Launcher touchpoints:

- `lib/features/mission_control/mission_control_page.dart`
  - `_drawerWidth`
  - `_setDrawerWidth`
  - `_MissionDrawerOverlay`
  - `_DrawerResizeHandle`
- `test/features/mission_control/mission_control_canvas_drawer_test.dart`
  - Drag proof for `mission_drawer_resize_handle`.

Proof already run during implementation:

```powershell
flutter test test/features/mission_control/mission_control_canvas_drawer_test.dart
flutter test test/features/mission_control
flutter analyze lib/features/mission_control test/features/mission_control
```

Follow-up caveat:

The width is currently session-local page state. Persisting it across Launcher restarts
is optional and should be a separate preference task if desired.

## Stage 73B - Split Agent Console surfaces

Status: implemented in Launcher.

Purpose:

Separate the current all-in-one `MissionAgentInstancePicker` into implementation
units that can be rearranged cleanly.

Why this stage must come before chat-first layout:

`MissionAgentInstancePicker` currently renders the title, pipeline spine, instance
chips, Chat History, and Harness Agents as one widget. If Stage 73C tries to collapse
history inside that widget before the container split, the drawer will still put
the whole utility surface above chat.

Implementation tasks:

- Extract a compact instance selector widget from `MissionAgentInstancePicker`.
  Suggested name: `MissionAgentSelectorStrip`.
- Extract Chat History into a standalone widget.
  Suggested name: `MissionPersonaChatHistorySection`.
- Extract live agent cards into a standalone widget.
  Suggested name: `MissionHarnessAgentConsoleList`.
- Keep `MissionAgentInstancePicker` as a compatibility wrapper only if older wide
  or compact layouts still call it.
- Add keys for testable regions:
  - `mission_agent_selector_strip`
  - `mission_chat_history_section`
  - `mission_agent_console_list`

Required connection points:

- `_AgentsDrawerContent` must be able to place chat and utility sections in a new
  order without duplicating picker internals.
- Existing callbacks must remain intact:
  - `onSelectInstance`
  - `onOpenChat`
  - `onIntent(_openPersonaChatIntent(entry))`
- Existing data inputs must remain unchanged:
  - `agentInstances`
  - `snapshot.goals`
  - `snapshot.personaChatHistory`
  - `selectedAgentInstance?.instanceId`

Acceptance:

- Existing picker tests still pass or are migrated to the extracted widgets.
- Selecting an agent still opens the same operator channel.
- Opening a saved chat still calls `_openPersonaChatIntent(entry)`.
- Raw ids remain hidden until diagnostics are expanded.
- No user-visible copy regresses to raw implementation names.

Focused proof:

```powershell
flutter test test/features/mission_control/mission_agent_instance_picker_test.dart
flutter test test/features/mission_control/mission_control_canvas_drawer_test.dart
flutter analyze lib/features/mission_control/agent_chat/mission_agent_instance_picker.dart lib/features/mission_control/mission_control_page.dart
```

## Stage 73C - Chat-first Agent Console drawer

Status: implemented in Launcher.

Purpose:

Make the selected agent's operator channel the dominant Agent Console surface.

Dependency:

Start only after Stage 73B splits the picker into independently placeable sections.

Implementation tasks:

- Rewrite `_AgentsDrawerContent` layout around the active chat.
- For wide drawer widths, use a two-zone layout:
  - primary body: selected agent header plus `_MissionAgentOperatorChannel`;
  - utility rail: compact selector, collapsed history, compact agent list.
- For narrow drawer widths, use a vertical layout:
  - compact selector;
  - selected agent header;
  - `_MissionAgentOperatorChannel`;
  - collapsed history;
  - compact agent list.
- Avoid wrapping the entire drawer in one `SingleChildScrollView` when chat needs
  stable height. Use a `Column` with an `Expanded` chat area and scroll only the
  utility regions.
- Preserve `_MissionAgentOperatorChannel`'s `SelectionContainer.disabled` wrapper.
- Give the chat area a stable min height so the composer remains reachable.

Suggested layout thresholds:

- Wide utility rail when drawer width is at least `860`.
- Single-column chat-first layout below `860`.
- Chat minimum height: `520` on desktop side drawer.
- Utility rail width: `280` to `340`, clamped to available width.

Acceptance:

- Opening Agent Console with a selected agent shows the operator chat in the first
  viewport.
- The composer is visible without scrolling at 1400x900 and 1728x1117 desktop
  test sizes.
- Chat History is not expanded above chat.
- The selected agent is obvious from a compact header.
- No `RenderFlex` overflow at 700px drawer width or after widening the drawer.

Focused tests:

- Add/adjust a drawer test that opens Agent Console and asserts:
  - `MissionAgentChatPanel` is present;
  - `mission_chat_history_section` is collapsed;
  - the composer is visible before scrolling.
- Add a narrow-width test using the default 700px drawer.
- Add a widened-drawer test using the Stage 73A handle.

Proof:

```powershell
flutter test test/features/mission_control/mission_control_canvas_drawer_test.dart
flutter test test/features/mission_control/mission_agent_chat_panel_test.dart
flutter analyze lib/features/mission_control test/features/mission_control
```

## Stage 73D - Collapsed-by-default Chat History

Status: implemented in Launcher.

Purpose:

Keep saved persona sessions available, searchable, and safe without letting them
dominate the active work surface.

Dependency:

Start after Stage 73B. This can land before or after 73C, but it must not be the
only change shipped for the "chat more prominent" goal.

Implementation tasks:

- Convert the extracted chat history widget into a collapsed-by-default section.
- Use session-local expansion state at first.
- Header should show:
  - `Chat History`;
  - total session count;
  - redacted session count if any;
  - latest update label when `updatedAt` exists.
- Expanded body should show:
  - persona filter chips or segmented control;
  - search input if there are more than five sessions;
  - saved session rows;
  - empty state for no matching sessions.
- Keep row tap/open behavior identical to current `onOpenChat`.
- Redacted previews remain low-emphasis and never appear in the collapsed header
  except as a count/status.

Acceptance:

- Default render shows the history header but no saved-chat rows.
- Expanding history reveals saved-chat rows.
- Opening a saved chat selects the persona instance and dispatches the open-chat
  intent.
- Redacted sessions show safe placeholder/status only.
- Collapsed state does not hide the selected active chat.

Focused tests:

- Update `mission_agent_instance_picker_test.dart` or add a new widget test for
  `MissionPersonaChatHistorySection`.
- Test default collapsed state.
- Test expand/collapse.
- Test persona filter/search if implemented.
- Test redacted preview handling.

Proof:

```powershell
flutter test test/features/mission_control/mission_agent_instance_picker_test.dart
flutter test test/features/mission_control/mission_control_page_test.dart
flutter analyze lib/features/mission_control/agent_chat test/features/mission_control/mission_agent_instance_picker_test.dart
```

## Stage 73E - Enterprise-grade Agent Console cards

Status: implemented in Launcher.

Purpose:

Turn live Harness agent state into a compact operations summary that scales past
four specialists.

Dependency:

Start after Stage 73B. Best after 73C so the cards can be tuned for the final
rail/section position.

Implementation tasks:

- Convert full-height `_HarnessAgentCard` content into compact rows by default.
- Keep selected-agent treatment clear but not oversized.
- Show the highest-signal fields by default:
  - role/display name;
  - readiness/status;
  - current assignment or latest run;
  - token count if available;
  - production-proof eligibility.
- Keep diagnostics collapsed by default.
- Move raw ids, worker session ids, context receipt ids, and compression receipt ids
  into diagnostics only.
- Make proof/run badges actionable only if there is an existing drawer or inspector
  route. If no route exists, keep them non-clickable and do not fake navigation.

Acceptance:

- Four agents fit in the utility rail without forcing the chat below the fold.
- Selected agent row remains visually distinct.
- Diagnostics expansion reveals the same safe detail as today.
- Unsafe/redacted snapshot state is precise and does not leak hidden content.
- No nested cards.

Focused tests:

- Default agent rows hide diagnostics/raw ids.
- Expanding diagnostics reveals safe ids/details.
- Selecting a row changes the selected agent.
- Status/proof chips render truthfully for ready/running/blocked/failed agents.

Proof:

```powershell
flutter test test/features/mission_control/mission_agent_instance_picker_test.dart
flutter test test/features/mission_control/mission_control_canvas_drawer_test.dart
flutter analyze lib/features/mission_control test/features/mission_control
```

## Stage 73F - Legacy wide/compact path cleanup

Status: implemented by compatibility wrapper inheritance; remaining direct
`MissionAgentInstancePicker` call sites are fallback/legacy surfaces and no longer
render Chat History expanded by default.

Purpose:

Keep inactive or fallback Mission Control layouts from drifting away from the new
Agent Console contract.

Why this exists:

`mission_control_page.dart` still contains older wide/compact body classes that
reference `MissionAgentInstancePicker`. Even if the canvas drawer is primary, these
paths can still be compiled, tested, or restored. They should not silently retain
the old history-first layout.

Implementation tasks:

- Audit references to `MissionAgentInstancePicker` in:
  - `_WideBody`
  - `_CompactBody`
  - archived/detail surfaces near the bottom of `mission_control_page.dart`
- Either migrate those call sites to the new split widgets or mark them explicitly
  as compatibility-only if they are unreachable.
- Keep analyzer ignores minimal and local.

Acceptance:

- `rg "MissionAgentInstancePicker" lib/features/mission_control` has only expected
  call sites.
- Any remaining compatibility wrapper renders chat-first or collapsed-history
  behavior when visible.
- No stale test expects `Persona Instances & Chat History` to be the dominant
  Agent Console title.

Proof:

```powershell
rg "MissionAgentInstancePicker" lib/features/mission_control
flutter test test/features/mission_control
flutter analyze lib/features/mission_control test/features/mission_control
```

## Stage 73G - Integrated visual proof and QA

Status: widget/analyzer proof passed in this implementation pass; Stage C visual
proof still required.

Purpose:

Prove the product-quality UX, not just the code paths.

Dependency:

Run after 73C, 73D, and 73E are implemented.

Required widget proof:

```powershell
flutter test test/features/mission_control
flutter analyze lib/features/mission_control test/features/mission_control
```

Required Stage C visual proof:

```powershell
flutter build windows --debug --target lib/main_marionette.dart
powershell.exe -NoProfile -ExecutionPolicy Bypass -File docs\stages\qa-reboot\scripts\Test-StageCAppTabMcpE2E.ps1 -Tab <tab> -ArtifactRoot <artifact_dir> -ScreenshotScenarioLabel <safe_label>
```

Screenshot scenarios:

- Default Agent Console at 1400x900: chat prominent, history collapsed.
- Widened Agent Console: two-zone layout visible, chat still primary.
- Expanded Chat History: saved sessions visible and safe.
- Expanded diagnostics for one selected agent: raw ids/details visible only there.
- Streaming reply active or simulated: live bubble visible with correct agent name.

Visual proof requirements:

- Screenshot is desktop-sized, nonblank, and uses the intended Launcher debug build.
- First viewport shows chat as the primary work surface.
- No text overlap in drawer header, resize handle, chips, rows, or composer.
- No hidden provider chain-of-thought appears anywhere.
- Redacted data shows safe placeholders only.

## Cross-stage data contract

Use existing safe fields wherever possible:

- `MissionAgentInstance`
  - `instanceId`
  - `personaId`
  - `displayName`
  - `role`
  - `status`
  - `taskId`
  - `taskTitle`
  - `runId`
  - `workerSessionId`
  - `sessionIdPresent`
  - `providerLabel`
  - `tokenCountLabel`
  - `assignmentKind`
  - `assignmentState`
  - `assignmentTitle`
  - `productionProofEligible`
- `MissionPersonaChatHistoryEntry`
  - `sessionId`
  - `personaId`
  - `personaInstanceId`
  - `title`
  - `lastMessagePreview`
  - `messageCount`
  - `updatedAt`
  - `state`
  - `redactionStatus`
  - `messages`
- `HarnessAgentCardView`
  - `title`
  - `roleLabel`
  - `repoScopeLabel`
  - `statusLabel`
  - `activityLabel`
  - `assignmentLabel`
  - `chips`
  - `diagnostics`
  - `accentHex`

Only add Harness fields if the Launcher cannot derive an enterprise summary from
these safe projections.

## Final implementation order

1. 73A - Resizable drawer. Done.
2. 73B - Split picker/history/console widgets.
3. 73C - Chat-first drawer layout.
4. 73D - Collapsed-by-default Chat History.
5. 73E - Compact enterprise-grade agent rows.
6. 73F - Legacy wide/compact path cleanup.
7. 73G - Integrated widget, analyzer, and Stage C visual proof.

This order keeps each stage shippable and prevents the common failure mode where
history gets collapsed but the picker remains above chat, or cards get polished
while the chat still feels secondary.
