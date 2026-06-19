# Stage 21 — Block Decisions Require Log Evidence

Status: completed 2026-05-30

## Goal

Agents must not mark a mission `blocked` with only prose. A `block` decision must include a brief reason plus a redaction-safe pointer to the agent/runtime log evidence line that justifies the block.

Tony requirement:

> For an agent to call blocked they need to grep or glob their own logs and return a brief reason and point at their log line number.

## Product stance

Mission Control is useful only if a blocked task immediately answers "why" and "where is the evidence?" in the Launcher right panel. Blocking without a log reference is below AAA quality because it forces Tony/Alice to manually dig through run/event logs.

## Contract

`DecisionType.BLOCK` payload must include:

```json
{
  "reason": "brief operator-readable reason",
  "log_ref": {
    "path": "events.jsonl",
    "line": 1234,
    "summary": "brief evidence summary"
  }
}
```

Rules:

- `reason` is required in the payload and non-empty; rationale fallback is not accepted.
- `log_ref` is required and must be an object.
- `log_ref.path` is required and must be a redaction-safe relative/log handle, not an absolute path, drive-qualified path, traversal path, dotfile path, or secret-like path component.
- `log_ref.line` is required and must be an integer >= 1.
- `log_ref.summary` is required and non-empty.

## Implementation stages

### Stage 1 — TDD contract validation

Add tests that:

- legacy `block` payloads with only `reason` fail validation;
- valid `block` payloads with `log_ref` pass;
- invalid/missing line/path/summary fail.

### Stage 2 — Persona prompt contract

Update `build_system_prompt` block payload contract so live personas know they must inspect their own logs and include `log_ref`.

### Stage 3 — Verification

Run focused decision/persona tests, compileall, and diff check.

## Verification

- RED: focused block-contract tests failed before implementation because legacy block payloads with only `reason` were accepted and the prompt still documented only `{reason}`.
- GREEN: `venv/Scripts/python.exe -m pytest -o addopts='' tests/agent_runtime/test_planning.py::test_block_decision_requires_log_ref_evidence tests/agent_runtime/test_planning.py::test_block_decision_accepts_log_ref_evidence tests/agent_runtime/test_planning.py::test_block_decision_rejects_invalid_log_ref_line tests/agent_runtime/test_persona_prompts.py::test_build_system_prompt_appends_payload_contracts_after_schema -q` passed `4 passed`.
- Targeted: `venv/Scripts/python.exe -m pytest -o addopts='' tests/agent_runtime/test_planning.py tests/agent_runtime/test_persona_prompts.py -q` passed `20 passed`.
- Review fix: require payload `reason` without rationale fallback and reject unsafe `log_ref.path` values such as absolute, drive-qualified, traversal, dotfile, auth/token, credentials, and secret-like paths.
- Hygiene: `venv/Scripts/python.exe -m compileall agent_runtime tests/agent_runtime && git diff --check` passed.

## Future UI use

Launcher can render `payload.log_ref` in the right-side Agent Detail panel:

- show blocked reason;
- show `events.jsonl:<line>` / safe source handle;
- make it clickable when a local operator view can resolve it;
- show the log summary next to the final decision.
