# 33 — Dev Work Event Instrumentation for Mission Control

## Goal

Mission Control currently shows tool/proof/handoff events for Dev runs, but live runs can lack the actual code-work trail Tony expects. The Launcher now has a `Code / patches` terminal filter, but it only appears when the Harness emits redaction-safe dev-work metadata.

Implement Harness-side instrumentation so Dev implementation runs persist safe patch/code/write-file summaries into existing event streams.

## Product stance

- The Live Agent Terminal is a redaction-safe structured operator transcript.
- It must show actual Dev work signals when tools modify code.
- It must not persist raw diffs, raw tool output, absolute paths, secrets, or hidden provider chain-of-thought.
- The fix belongs in existing progress/tool event plumbing, not a new logger or database.

## Current audit evidence

Live run inspected:

- `task_e63f57b5`
- `run_d52bbb90db90`

Raw `events.jsonl` had 81 events for that run and zero matches for `patch`, `write_file`, `changed_files`, `code_summary`, `patch_summary`, `file.edit`, or `file.write`.

Current code audit:

- `agent_runtime/profile_runner.py` wires `AIAgent` callbacks into `_progress_adapter`.
- `_tool_finished_payload()` already creates generic `phase=tool`, `step=tool_finished` events.
- `_safe_tool_result_detail()` already summarizes patch `files_modified` as safe basenames, but leaves the event classified as generic tool work.
- `tests/agent_runtime/test_profile_runner.py` already covers generic tool lifecycle and patch raw-diff suppression.
- Launcher reads `changed_files` / `files_touched` / `patch_summary` / `code_summary` / `detail` and shows `Code / patches` when `phase=dev_work`, `step=patch`, `tool=patch/write_file`, or patch/code summaries exist.

## Fixed architecture decisions

1. Reuse existing `run.progress` / `run.tool.finished` event path.
2. Classify mutating code tools as dev-work events by metadata:
   - `phase: dev_work`
   - `step: patch`, `write_file`, or `code_edit`
3. Preserve safe detail only:
   - `changed_files`: basename-only labels, max bounded list.
   - `files_touched`: count after sanitization.
   - `patch_summary` / `code_summary` / `file_summary`: short safe operator text.
4. Preserve generic tool events for non-mutating tools such as `read_file`, `search_files`, and `terminal`.
5. Do not persist raw diffs, command args, absolute paths, or tool result blobs.

## Rejected alternatives

- **Raw tool output in events:** rejected because it can leak secrets, paths, diffs, or provider/private data.
- **Launcher scraping git diff:** rejected because Launcher should consume the Harness read-model only.
- **Separate Dev Work Log panel:** rejected because Mission Control guidance requires one unified terminal with filters.
- **Hidden chain-of-thought capture:** rejected; providers do not expose this safely and fabricating it is misleading.

## Stage A — RED tests

Affected files:

- `tests/agent_runtime/test_profile_runner.py`

Required tests:

1. Patch tool finished event becomes dev-work:
   - `phase == "dev_work"`
   - `step == "patch"`
   - `changed_files == ["mission_control_page.dart", ...]`
   - `files_touched == 2`
   - `patch_summary` / `detail` present
   - raw `diff`, secret/path fragments absent
2. Write-file tool finished event becomes dev-work:
   - `phase == "dev_work"`
   - `step == "write_file"`
   - safe basename from result or input metadata
   - no absolute path persisted
3. Non-mutating tools remain generic `phase=tool`.

RED command:

```bash
pytest tests/agent_runtime/test_profile_runner.py::test_progress_adapter_summarizes_patch_tool_result_without_raw_diff -q --timeout=0
```

## Stage B — GREEN implementation

Affected files:

- `agent_runtime/profile_runner.py`

Implementation details:

- Add a helper to identify dev-work tools: `patch`, `write_file`, and future-safe `edit_file` / `apply_patch` aliases.
- For patch results, reuse sanitized basename extraction.
- For write-file results, inspect safe candidate file fields from result and invocation metadata without persisting raw paths.
- Add `phase`, `step`, `changed_files`, `files_touched`, and summary fields only after sanitization.
- Keep event payload below the existing 4096-byte event limit.

## Stage C — read-model proof

Existing snapshot/observe surfaces already copy event payload fields. After GREEN tests, verify:

```bash
pytest tests/agent_runtime/test_profile_runner.py -q --timeout=0
python -m compileall agent_runtime/profile_runner.py tests/agent_runtime/test_profile_runner.py
git diff --check
```

Optional future live proof:

- Run a tiny temp-root Harness Dev smoke that invokes patch/write_file and confirm `hermes harness snapshot --json` contains `phase=dev_work` and safe `changed_files`.

## Acceptance criteria

- Dev patch/write-file tool completions are visible as redaction-safe dev-work events.
- Launcher can show the `Code / patches` terminal chip from live Harness logs.
- No raw diffs, absolute paths, secrets, or hidden CoT are persisted.
- Targeted tests and compile/diff hygiene pass.

## Implemented status

Completed in this stage:

- Patch and write-file tool completion callbacks now classify as `phase=dev_work`.
- Patch events now emit `step=patch`, `patch_summary`, `changed_files`, and `files_touched` when safe file labels are available.
- Write-file/code-edit events now emit `step=write_file`/`code_edit`, `file_summary`, `changed_files`, and `files_touched` when safe file labels are available.
- `RunProgressSink` now preserves sanitized dev-work file lists instead of dropping list-valued fields.
- `observability.recent_events` now preserves the same sanitized dev-work fields so Launcher can render the `Code / patches` terminal chip.
- Tests cover raw diff/path/secret dropping.

Verification run:

```bash
pytest tests/agent_runtime/test_profile_runner.py tests/agent_runtime/test_progress.py tests/agent_runtime/test_observability_dev_work.py tests/agent_runtime/test_snapshot.py -q --timeout=0
python -m compileall agent_runtime/profile_runner.py agent_runtime/progress.py agent_runtime/observability.py tests/agent_runtime/test_profile_runner.py tests/agent_runtime/test_progress.py tests/agent_runtime/test_observability_dev_work.py
git diff --check
```

## Remaining gap policy

If AIAgent callback result payloads do not include changed file information for some tool backends, record a follow-up stage to enrich tool callback result metadata at the tool implementation layer. Do not make Launcher infer or scrape code changes.
