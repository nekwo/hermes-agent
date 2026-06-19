# Stage 34 — Safe Agent Thinking Process Observability

## Goal

Mission Control should show the agent's useful thinking/process trail without exposing hidden provider chain-of-thought, raw model output, secrets, paths, or unsafe tool payloads.

Tony-facing wording may say **Thinking process**, but the underlying contract is a redaction-safe structured summary only.

## Product contract

The Live Agent Terminal may render:

- `Decision process:` — the validated `AgentDecision.rationale` or equivalent safe decision rationale.
- `Thinking process:` — a sanitized `reasoning_summary` progress field emitted by Harness callbacks or derived from a validated run decision.
- Tool/work/proof goodies already present: tool labels, skill labels, toolsets, code/patch summaries, changed-file basenames, proof IDs/images, token breakdown, provider/model/session labels, validation status, next expected action, duration/exit metadata.

The Live Agent Terminal must not render:

- hidden provider chain-of-thought;
- raw `_thinking` / raw model content;
- raw diffs;
- absolute local paths;
- token/credential/API-key/cookie/Authorization strings;
- raw tool result blobs.

## Implementation notes

Harness:

- `ProfileAgentRunner` callback adapter converts `reasoning.available` / `_thinking` callbacks into `phase=thinking_process`, `step=reasoning_summary` events.
- It only forwards a compact sanitized `reasoning_summary` string when it is safe.
- `RunProgressSink` and observability payload sanitizers whitelist `reasoning_summary` but still reject unsafe/pathish strings.
- Snapshot archived run summaries expose `reasoning_summary`, falling back to validated decision rationale when no dedicated reasoning summary exists.

Launcher:

- `MissionAgentLogEvent` parses `reasoning_summary`.
- The CLI bridge maps `reasoning_summary` from Harness recent events and run summaries.
- The terminal transcript renders `Thinking process: <summary>` and includes thinking-process events in the decision/handoff filter.

## Verification

Harness targeted tests:

```bash
venv/Scripts/python.exe -m pytest \
  tests/agent_runtime/test_profile_runner.py::test_progress_adapter_summarizes_reasoning_progress_without_raw_private_text \
  tests/agent_runtime/test_progress.py::test_safe_progress_payload_preserves_agent_thinking_summary_only \
  tests/agent_runtime/test_observability_dev_work.py::test_observability_preserves_safe_agent_thinking_summary_only \
  tests/agent_runtime/test_snapshot.py::test_snapshot_archived_tasks_include_run_proof_and_decision_transcript \
  -q --timeout=0
```

Launcher targeted tests:

```bash
flutter test \
  test/features/mission_control/mission_control_bridge_test.dart \
  test/features/mission_control/mission_control_page_test.dart \
  --plain-name "Live Agent Terminal expands ids into readable names and opens proof image inspector"
```

## Acceptance criteria

- Future Harness Dev/QA/Neko runs can surface safe thinking/process summaries in Mission Control.
- Archive playback retains the same safe process summary for archived runs.
- Existing terminal goodies remain consolidated in the Live Agent Terminal instead of split across disconnected cards.
- Tests prove unsafe private text is dropped.
