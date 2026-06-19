# Stage 42 — Mission Control archive button live bridge

## Summary

Stage 41 implemented evidence-preserving Harness archive support at the CLI/runtime layer, but follow-up audit found the Launcher Mission Control archive button was still routed through the unsupported live-intent path.

The UI button existed and emitted `MissionControlIntent.archiveReadyGoal`, but `CliMissionControlActionRepository._argsForIntent` mapped `archiveReadyGoal` to `null`, causing the live Launcher action to report:

```text
Archive Ready Goal is not supported by the live harness CLI yet.
```

Stage 42 closed that gap by wiring the existing Launcher archive intent to the live Harness archive CLI command.

## Root cause

Frontend bridge support lagged behind the Harness CLI implementation.

Affected file:

```text
X:/Unreal Engine/Engine/Launcher/EterniaLauncher/lib/features/mission_control/data/mission_control_bridge.dart
```

Bad path:

```dart
MissionControlIntentType.pauseGoal ||
MissionControlIntentType.requestQa ||
MissionControlIntentType.archiveReadyGoal => null,
```

Fixed path:

```dart
MissionControlIntentType.archiveReadyGoal => <String>[
  'harness',
  'task',
  'archive',
  intent.goalId!,
  '--json',
],
MissionControlIntentType.pauseGoal ||
MissionControlIntentType.requestQa => null,
```

## Live goal

```text
task_f5171546
Stage 42: Wire Mission Control archive button to live Harness archive CLI
```

Final state:

```text
done
```

Settle result:

```text
stop_reason=task_terminal
ticks=5
open_incidents=0
proof_ids=4
```

## Implementation commit

Launcher commit:

```text
26f38f9b fix(mission-control): wire archive button to harness CLI
```

Changed files:

```text
lib/features/mission_control/data/mission_control_bridge.dart
test/features/mission_control/mission_control_bridge_test.dart
test/features/mission_control/mission_control_page_test.dart
```

## Verification

Tony/Alice reran targeted Launcher tests after agent completion:

```bash
flutter test test/features/mission_control/mission_control_bridge_test.dart test/features/mission_control/mission_control_page_test.dart
```

Result:

```text
All tests passed!
```

Harness status after commit:

```text
open_tasks=0
active_runs=0
waiting_runs=0
open_incidents=0
runtime_health.ok=true
next_actions=[]
```

## Known remaining non-blocker

Mission Control visual/MCP screenshot proof remains separately blocked by the stale Launcher debug EXE target issue discovered in Stage 41:

```text
launch_wrong_debug_target_missing_marionette
Debug EXE built against lib/main.dart, not lib/main_marionette.dart
```

That does not block Stage 42 code/test acceptance because the archive button bridge path is covered by focused Flutter/Dart tests and the live Harness goal completed with QA approval.
