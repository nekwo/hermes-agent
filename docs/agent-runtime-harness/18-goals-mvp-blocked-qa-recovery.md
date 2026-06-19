# Stage 18 — Goals MVP Blocked QA Recovery

## Goal

Complete the smallest Goals MVP recovery gap exposed by Tony's original Launcher drawer-drag goal: a mission that receives a structured QA implementation verdict of `blocked` with proof attached must not become inert just because no separate incident is open.

The MVP loop must keep moving:

```text
Goal → PM scopes → Dev implements/proves → QA verifies
  → if QA blocks with exact proof gaps → Dev recovery/fix pass
  → QA approves → PM closes done
```

## Product stance

This is Mission Control brainstem work, not UI polish. A blocked QA verdict is already a structured agent decision and proof artifact. The harness must treat it as actionable workflow state, not as a terminal/manual-only state.

## Audit evidence

Tony's active original test goal:

- Task: `task_768cb054`
- Title: `Recover and complete Launcher drawer drag QA from clean Harness goal`
- Repo: `C:/repo/product`
- Current state before this stage: `blocked`
- Open incidents: `0`
- Latest tick behavior: `actions_taken=[]`, `skipped=["task_768cb054"]`
- QA verdict proof: `proof_qa_b06261f6`, metadata verdict `blocked`
- QA findings required a proof manifest and broad/analyzer proof clarity.
- Older command proofs failed with Windows shell/path syntax (`The filename, directory name, or volume label syntax is incorrect.`), which Stage 16's bash proof runner addresses for future proof runs.

Root cause:

- `MissionStateMachine.next_action()` returns NOOP for `TaskState.BLOCKED` with no `open_incident_ids`.
- `_apply_implementation_review(... verdict=blocked ...)` sets the task to `blocked` but does not create a recoverable routing marker or incident.
- `context_builder._safe_proof_metadata()` hides QA verdict `findings`, so Dev/Neko cannot see exact proof-fix requirements from the QA proof context.

## Stage 18.1 — Regression coverage

Add failing tests for:

1. A blocked task with QA blocked verdict proof and no open incident routes to Dev recovery instead of NOOP/skipped.
2. A ticker run actually executes Dev for that case.
3. Rendered context exposes safe QA findings (`issue`, `required_fix`, `severity`) without arbitrary metadata leaks.
4. A fresh QA implementation blocked verdict records a recoverable marker on the task.

## Stage 18.2 — Recovery routing

Implementation:

- Add QA-blocked verdict detection helper in the state machine.
- Detection sources:
  - risk flag `qa_blocked_verdict_needs_dev_recovery`; or
  - proof store contains a task proof with type `qa_verdict` and metadata `verdict=blocked`.
- If task is `blocked` with no open incidents and detection is true, return `RUN_DEV` with reason `needs dev recovery from QA blocked verdict`.
- Preserve existing NOOP for other blocked/no-incident states such as unsupported context requests.

## Stage 18.3 — Context, close, and future verdict hardening

Implementation:

- Include safe QA findings in rendered proof records.
- When QA blocks implementation, add a deduped risk flag `qa_blocked_verdict_needs_dev_recovery` and mark the current stage blocked when present.
- Harden AgentDecision extraction for live-model outputs that include prose, trailing braces, or non-decision evidence JSON before the real structured decision.
- Once QA approval proof exists, close `qa_approved` / PM proof-review states with a deterministic `complete_task` Harness action instead of spending another PM model tick that can re-open scope or emit a wrong-role decision.
- Do not treat child proof as parent proof.
- Do not auto-approve or fake proof; route blocked work back to Dev and close only after QA approval proof exists.

## Verification

Targeted commands:

```bash
venv/Scripts/python.exe -m pytest tests/agent_runtime/test_ticker.py tests/agent_runtime/test_context_builder.py tests/agent_runtime/test_aaa_gap_fixes.py -q
```

Full harness command:

```bash
venv/Scripts/python.exe -m pytest --timeout-method=thread tests/agent_runtime tests/hermes_cli/test_harness_cli.py -q
```

Hygiene:

```bash
venv/Scripts/python.exe -m compileall agent_runtime hermes_cli tests/agent_runtime
venv/Scripts/python.exe -m ruff check agent_runtime tests/agent_runtime tests/hermes_cli/test_harness_cli.py
PYTHONPATH=. venv/Scripts/python.exe -m pytest --timeout-method=thread tests/agent_runtime tests/hermes_cli/test_harness_cli.py -q
git diff --check
```

Live proof:

- First live tick against `task_768cb054` after blocked-verdict routing must take one action rather than skipping, routed to Dev with QA findings visible in context.
- Final live tick after QA approval proof must take deterministic `complete_task` and persist `state=done` without invoking another PM model tick.
- If product-repo proof failures remain before QA approval, the harness must report that as exact proof/product intervention, not inert skip.

## Acceptance criteria

- Blocked QA verdict tasks do not silently skip forever.
- Dev sees QA findings and can request corrected deterministic proof.
- Existing blocked context-request/manual-intervention tasks remain NOOP unless an incident routes Neko.
- Tests and full harness gate pass.
- Existing original Launcher goal is usable as the MVP recovery test surface and reaches `done` once QA approval proof is attached.
