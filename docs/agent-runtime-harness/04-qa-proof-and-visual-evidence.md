# Stage 4 — QA Proof + Visual Evidence Rules

## Goal

Make proof explicit, typed, redaction-aware, and role-appropriate.

Tony's proof rule:

- Dev claims can be proven by commits/diffs plus harness-captured test output.
- QA approval for UI/game/Launcher/visual/media work requires passed tests plus screenshot or video evidence.
- The harness, not the model, captures and validates proof.

Stage 4 does **not** introduce daemon scheduling. It adds proof gates and artifact capture contracts so Stage 5 can execute them reliably.

## Deep audit findings from current repo

### Existing proof-capable surfaces

Hermes already has several proof producers that Stage 4 should wrap, not reinvent:

- `tools/terminal_tool.py` / terminal backend tools can produce command, exit code, stdout/stderr, duration, and cwd.
- `tools/file_tool.py` and `utils.atomic_json_write()` provide safe file writes for proof records.
- Browser tools can produce screenshots and page state snapshots.
- Launcher QA MCP tools in Tony's environment return screenshot/artifact paths and redaction-safe widget state.
- `hermes_state.py::SessionDB` persists model/tool transcript history with WAL and FTS5.
- `hermes_logging.py` has redacting formatters and session-context correlation.
- Messaging/gateway code supports `MEDIA:<path>` delivery, but Stage 4 should store proof first; sending is a later/user action.
- `agent_runtime/proof_rules.py` currently has only a minimal `ProofType` and `ProofRequirement`. It needs real gate logic.
- `agent_runtime/store.py::ProofStore` can persist proof records under `proofs/<task_id>/proof_<id>.json`.

### Current gaps

- `ProofType` currently has `DIFF`, `COMMIT`, `TEST_RUN`, `SCREENSHOT`, `VIDEO`, `LOG`, `URL`, `TEXT`, `ARTIFACT`; Stage 4 must decide whether to add `QA_VERDICT`, `PM_APPROVAL`, and `REDaction_SCAN` or model those as metadata on existing proof types.
- `ProofRequirement` only checks type presence. Stage 4 needs gate functions that explain *why* evidence is missing.
- Events include `proof.attached` and `proof.scanned`, but there is no redaction scanner hook or proof validator yet.
- Stage 2 personas can request tests/screenshots/video, but the runtime does not execute those actions.

## Package additions

```text
agent_runtime/
  proof_gates.py           # can_dev_ready_for_qa, can_qa_approve, can_pm_integrate
  proof_capture.py         # data structures for harness-captured commands/screenshots/videos
  redaction.py             # redaction status enum + scanner interface/stub
  qa_verdict.py            # QA verdict payload validation/application
```

Avoid adding product-specific Launcher MCP imports into core code. Use a provider interface for visual capture so Launcher, browser, and future Unreal capture can plug in.

## Expanded proof model

Stage 1 `Proof` remains the persisted record. Stage 4 should standardize metadata by proof type.

### Test-run proof metadata

```json
{
  "command": "python -m pytest tests/agent_runtime/ -q",
  "cwd": "<repo-root>",
  "exit_code": 0,
  "duration_ms": 4312,
  "stdout_path": "proofs/task_abc/test-runs/run_1.stdout.txt",
  "stderr_path": "proofs/task_abc/test-runs/run_1.stderr.txt",
  "started_at": "...",
  "finished_at": "..."
}
```

Rules:

- Full stdout/stderr go into files, not event payloads or task JSON.
- `exit_code == 0` is required unless PM/Tony waived tests.
- Test commands must be harness-executed; model statements like "tests passed" are not proof.

### Commit/diff proof metadata

```json
{
  "repo": "<repo-root>",
  "commit": "abc123",
  "branch": "docs/agent-runtime-harness",
  "diff_stat": "20 files changed, ...",
  "dirty": false,
  "no_commit_reason": null
}
```

Rules:

- Dev-ready proof needs either commit hash or diff stat plus explicit no-commit reason.
- Dirty working tree can be allowed only if task is not ready for QA/integration.

### Visual proof metadata

```json
{
  "capture_provider": "launcher_mcp | browser | manual | unreal_future",
  "path": "proofs/task_abc/screenshots/screen_1.png",
  "scenario": "library-details-open",
  "window_or_url": "redaction-safe target label",
  "width": 1920,
  "height": 1080,
  "redaction_status": "safe | needs_scan | unsafe"
}
```

Rules:

- Binary screenshots/videos stay as files under proof root.
- Events contain only proof ID and redaction status.
- If `redaction_status != safe`, proof can satisfy internal QA gate but must not be auto-sent externally.

### QA verdict proof

Prefer a `ProofType.TEXT` or new `ProofType.QA_VERDICT` with metadata:

```json
{
  "verdict": "approved | needs_fixes | blocked",
  "proof_ids": ["proof_test", "proof_screenshot"],
  "findings": [],
  "reviewed_stage_ids": ["stage_1"],
  "visual_requirement_satisfied": true
}
```

## Proof gates

### `can_dev_ready_for_qa(task, proofs)`

Passes when:

1. The task has implementation changes represented by one of:
   - `ProofType.COMMIT`
   - `ProofType.DIFF`
   - explicit no-code/docs-only waiver.
2. Each implemented stage has at least one relevant test-run proof or a test waiver.
3. No open blocker/critical incidents exist.

Returns:

```python
@dataclass(slots=True)
class GateResult:
    allowed: bool
    missing: list[str]
    warnings: list[str] = field(default_factory=list)
```

### `can_qa_approve(task, proofs)`

Always requires:

- QA verdict proof with `verdict == approved`.
- At least one passed `TEST_RUN` proof, unless PM/Tony waived tests.

If task or any stage requires visual proof, also requires:

- at least one `SCREENSHOT` or `VIDEO` proof with usable artifact path.

### `can_pm_integrate(task, proofs, incidents)`

Requires:

- `task.state == QA_APPROVED` or QA approval proof.
- Dev proof present.
- QA proof gate passes.
- No open critical incidents.
- Any required human approval gate is satisfied.

## Visual proof classification

`requires_visual_proof` is true by default when task/stage touches:

- Launcher UI / Flutter widgets / shell navigation
- Unreal, gameplay, animation, rendering, cinematics
- media playback, screenshots, video, visual layout
- browser user-facing flow
- generated image/video quality checks

Default false for:

- backend-only refactor
- docs-only change
- CLI-only behavior, unless terminal UX output matters
- internal schema/store changes with no visible UI

QA can escalate from false to true during plan review. De-escalation requires PM/Tony waiver with reason.

## Capture interfaces

Stage 4 should define interfaces, not concrete product imports:

```python
class TestRunner(Protocol):
    def run(self, command: str, *, cwd: Path, timeout_s: int) -> CapturedTestRun: ...

class VisualCaptureProvider(Protocol):
    def capture_screenshot(self, request: ScreenshotRequest) -> CapturedArtifact: ...
    def capture_video(self, request: VideoRequest) -> CapturedArtifact: ...

class RedactionScanner(Protocol):
    def scan_text(self, path: Path) -> RedactionStatus: ...
    def scan_image(self, path: Path) -> RedactionStatus: ...
```

Concrete providers can be added later:

- `BrowserVisualCaptureProvider`
- `LauncherMcpVisualCaptureProvider`
- `ManualArtifactProvider`
- future `UnrealVisualCaptureProvider`

## Event requirements

Use existing event types:

- `proof.attached`
- `proof.scanned`

Add if useful:

- `proof.gate_checked`
- `qa.verdict_recorded`

Event payloads may contain:

- proof IDs
- proof type
- exit code
- redaction status
- missing proof keys

Event payloads must not contain:

- full stdout/stderr
- screenshot bytes
- raw secrets
- full file diffs

## Implementation tasks

1. Add failing tests for `can_qa_approve()` requiring test proof.
2. Add failing tests for visual tasks requiring screenshot/video.
3. Add failing tests for non-visual tasks passing with test + QA verdict.
4. Add failing tests for `can_pm_integrate()` blocking on open incidents.
5. Add metadata validators for test-run, commit/diff, and visual proof records.
6. Add redaction status helpers and proof scan event tests.
7. Add capture request/result dataclasses without concrete tool execution.
8. Add QA verdict application helper that records verdict proof and transitions only when gates pass.
9. Run `python -m pytest tests/agent_runtime/ -q`.

## Tests

Required test files:

```text
tests/agent_runtime/test_proof_gates.py
tests/agent_runtime/test_proof_capture.py
tests/agent_runtime/test_redaction.py
tests/agent_runtime/test_qa_verdict.py
```

Test matrix:

- Visual task without screenshot/video cannot be QA-approved.
- Visual task with screenshot but failed tests cannot be QA-approved.
- Non-visual task with passed tests + QA approved verdict passes QA gate.
- Test-run proof with nonzero exit code is not passing proof.
- Missing artifact path fails visual gate.
- Unsafe redaction status blocks external delivery flag but can remain attached internally.
- PM integration gate fails when critical incident is open.
- Model-authored "tests passed" text proof does not satisfy test proof gate.
- Full stdout/stderr is written to artifact path and not event payload.

## Acceptance criteria

- Proof gates are deterministic and return human-readable missing evidence.
- QA cannot approve a visual task without screenshot/video proof and passed test proof.
- PM cannot integrate without QA-approved proof bundle.
- Visual evidence paths are relative under proof root when possible.
- No binary evidence or large logs are in task JSON/events.
- Redaction status is explicit on every artifact proof.

## Risks / interventions

- **Secret leakage:** screenshots/logs can contain secrets. Default to `needs_scan`; never auto-send unsafe proof.
- **Self-reported proof:** model statements do not count. Harness must capture command/exit code/artifact path.
- **Huge logs:** full output lives in artifacts; event payloads stay < 4 KB.
- **Provider coupling:** keep Launcher/Browser/Unreal capture behind interfaces.
- **False visual negatives:** QA can escalate `requires_visual_proof`; PM/Tony must waive de-escalation.
