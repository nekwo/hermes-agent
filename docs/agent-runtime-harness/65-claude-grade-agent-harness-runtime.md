# Stage 65 — Claude-Grade Agent Harness Runtime

> **For Hermes:** This is the runtime/source-of-truth companion to Launcher `docs/mission_control/31-stage-23-claude-grade-agent-harness.md`. Use `staged-deep-audit-delivery`, `agent-runtime-harness`, and proof-first tests before implementation.

**Goal:** Upgrade Agent Runtime Harness from persona-instance/session plumbing into a Claude-grade agent runtime: typed readiness, durable chats, live turn execution, transcript/event read models, approvals, cancellation, archive, and enterprise-safe observability.

**Architecture:** Harness owns runtime truth. Launcher must be able to answer: can this persona chat run now, what happened after send, what is the current turn doing, what is the transcript, what action is required, and what proof/event backs that state. `open-chat` remains state-only; message/run capabilities drive execution explicitly and return stable readback.

**Audit date:** 2026-06-18

---

## Why Stage 64 was not enough

Stage 64 added/defined persona chat history and `open-chat` state binding. The screenshot still fails because the product can create/queue a persona instance but does not expose a Claude-like live agent loop.

Failure artifact symptoms:

- Launcher displays `Harness state: not_ready` as a chat bubble.
- First operator message yields `Persona instance started and operator message queued. Waiting for the next Harness snapshot…`.
- No assistant turn starts or produces transcript output in the visible panel.
- Disabled run/control buttons have unclear runtime reasons.

Runtime conclusion: chat/session history exists, but readiness, execution scheduling, transcript readback, and event-state projection are incomplete for enterprise UX.

---

## Product/runtime non-negotiables

1. **Readiness is typed.** Harness must emit why a persona chat can or cannot execute: ready, sandbox, feature disabled, provider/auth unavailable, runtime root mismatch, no worker, capability unavailable, or unknown.
2. **Message append and turn execution are explicit.** Appending a message is durable; starting a model turn is separately visible but may be automatic in chat mode.
3. **No indefinite queued states.** Every queued message has a next state: scheduled, running, waiting approval, blocked, completed, cancelled, or timed out.
4. **Transcript is durable and redaction-safe.** Launcher reads the selected session transcript through Harness, not by crawling raw local DB files.
5. **Events power UI.** Harness emits/snapshots redaction-safe events for queued/running/tool/approval/completed/blocked states.
6. **Controls are capabilities.** Stop/pause/resume/approve/archive/new-chat/run-one-turn are declared capabilities with required args and disabled reasons.
7. **No proof fabrication.** Persona chat is not production proof unless explicitly promoted through a proof gate.
8. **Enterprise safety.** Redaction, archive/evidence retention, runtime-root/profile isolation, cancellation races, and budget/status surfaces are tested.

---

## Current runtime anchors to audit/modify

- `agent_runtime/capabilities.py`
  - Add/extend descriptors for readiness, transcript/history, run turn, stop/cancel, archive, approval as needed.
- `agent_runtime/persona_assignments.py`
  - Owns persona instance lifecycle, `open_chat`, and current assignment links.
- `agent_runtime/persona_chat_history.py`
  - Current safe history read model; extend or pair with transcript selected-session read model.
- `agent_runtime/snapshot.py`
  - Snapshot envelope must include readiness, history, selected transcript/turn/event summaries.
- `agent_runtime/models.py`
  - Add typed models only if existing models cannot represent chat turn/runtime state safely.
- `hermes_cli/harness.py`
  - CLI/capability entrypoints for message, run, open-chat, archive/close, readiness diagnostics, transcript smoke.
- Tests:
  - `tests/agent_runtime/test_capabilities.py`
  - `tests/agent_runtime/test_persona_assignments.py`
  - `tests/agent_runtime/test_snapshot.py`
  - Add `tests/agent_runtime/test_persona_chat_runtime.py` if this becomes too broad.

---

## Stage 65A — Typed runtime readiness

**Objective:** Emit a stable readiness envelope so Launcher never has to infer `not_ready` from fixture defaults or raw text.

**Snapshot contract:**

```json
"persona_runtime_readiness": {
  "state": "ready|sandbox|disconnected|feature_disabled|provider_unavailable|auth_unavailable|runtime_root_mismatch|capability_unavailable|unknown",
  "summary": "Runtime ready",
  "operator_action": "none|re_authenticate|select_runtime_root|enable_feature|start_worker|configure_provider|open_diagnostics",
  "can_execute_persona_turn": true,
  "can_append_message": true,
  "reason_code": "ready"
}
```

**Tasks:**

1. Audit existing config/profile/runtime-root checks.
2. Add a pure readiness builder with no side effects.
3. Include readiness in `build_snapshot()` when Harness persona runtime is enabled.
4. Add tests for ready, disabled, and disconnected/sandbox states.

**Proof:**

```bash
python -m pytest tests/agent_runtime/test_snapshot.py -q
```

**Acceptance:**

- Launcher can render runtime blocker copy without hardcoding `not_ready`.
- Readiness contains no secrets or local absolute paths.

---

## Stage 65B — Message append + auto-turn scheduling contract

**Objective:** Ensure first send in chat mode produces a durable operator message and either schedules/starts a turn or returns an actionable blocker.

**Capability semantics:**

- `persona.instance.create`
  - Creates or opens persona chat instance.
  - Appends initial user message.
  - If `auto_run=true` and readiness allows, creates/schedules a chat turn.
  - Returns `session_id`, `persona_instance_id`, `message_id`, `turn_id` or blocker.
- `persona.instance.message`
  - Appends user message to existing session.
  - If `auto_run=true`, schedules/starts next turn.
  - Returns same stable readback.
- `persona.instance.run_once`
  - Explicit manual one-turn execution for queued context.

**Response contract:**

```json
{
  "ok": true,
  "persona_id": "neko",
  "persona_instance_id": "personainst_neko",
  "session_id": "...",
  "message_id": "...",
  "turn_id": "...",
  "execution_state": "queued|running|blocked|completed",
  "blocker": null
}
```

**Tasks:**

1. Audit current `create`, `message`, and `run_once` CLI paths.
2. Add `auto_run` / scheduling semantics only where safe; default may be profile-controlled.
3. Guarantee readback contains enough IDs for Launcher reconciliation.
4. Tests prove no model/provider is called when readiness blocks execution.
5. Tests prove ready runtime schedules/starts a turn using a fake/deterministic runtime.

**Proof:**

```bash
python -m pytest tests/agent_runtime/test_persona_assignments.py tests/agent_runtime/test_persona_chat_runtime.py -q
```

**Acceptance:**

- No successful send can disappear into an invisible queue.
- Blocked sends return typed blocker state.

---

## Stage 65C — Transcript read model

**Objective:** Expose the selected persona chat transcript through Harness in redaction-safe form.

**Snapshot/CLI contract options:**

Option A — selected transcript in snapshot:

```json
"persona_chat_transcript": {
  "session_id": "...",
  "persona_id": "neko",
  "messages": [
    {
      "id": "...",
      "role": "user|assistant|system|tool",
      "text": "safe text or redaction placeholder",
      "created_at": "...",
      "redaction_status": "safe|redacted"
    }
  ]
}
```

Option B — dedicated CLI:

```bash
python -m hermes_cli.main harness persona instance transcript --session-id <id> --json
```

**Decision:** Start with snapshot selected-session transcript if row count is bounded; add CLI if snapshots become heavy.

**Tasks:**

1. Build transcript rows from SessionDB through a sanitizer adapter.
2. Bound row count and order deterministically.
3. Add redaction tests for paths/tokens/credential-like strings.
4. Include transcript for the selected/open chat only, not all history.

**Acceptance:**

- Old chat selection can render prior messages.
- Snapshot cannot leak raw tokens, provider payloads, or local private paths.

---

## Stage 65D — Turn/event state read model

**Objective:** Provide the UI with a Claude-like live status timeline.

**Contract:**

```json
"persona_turns": [
  {
    "turn_id": "...",
    "session_id": "...",
    "persona_id": "neko",
    "state": "queued|running|waiting_approval|blocked|completed|cancelled|failed",
    "summary": "Neko is thinking…",
    "started_at": "...",
    "updated_at": "...",
    "requires_approval": false,
    "approval_id": null,
    "last_event": {
      "kind": "tool_started|tool_completed|assistant_message|blocker|approval_required",
      "safe_label": "Reading files"
    }
  }
]
```

**Tasks:**

1. Map existing runs/events/proofs into persona turn summaries.
2. Add event sanitizer.
3. Ensure event rows are append-only enough for audit/proof review.
4. Expose selected session’s active/latest turn in snapshot.

**Acceptance:**

- Launcher can display queued/running/tool/approval/completed states without guessing.
- Turn state survives refresh.

---

## Stage 65E — Control capabilities and disabled reasons

**Objective:** Make every Mission Control control a declared capability with stable enablement and reason.

**Capabilities to audit/add:**

- `persona.instance.run_once`
- `persona.instance.cancel_turn`
- `persona.instance.pause`
- `persona.instance.resume`
- `persona.instance.approve`
- `persona.instance.archive_chat`
- `persona.instance.new_chat`
- `persona.instance.open_chat`
- `persona.diagnose`

**Descriptor extension:**

```json
{
  "id": "persona.instance.cancel_turn",
  "enabled": true,
  "disabled_reason": null,
  "required_args": ["persona_id", "session_id", "turn_id"],
  "execution_semantics": "control_state_change"
}
```

**Acceptance:**

- Launcher can render enabled/disabled controls with exact reasons from Harness state.
- Disabled controls are not mystery grey buttons.

---

## Stage 65F — Fast refresh / event stream

**Objective:** Reduce post-send latency and eliminate stale “waiting for snapshot” UX.

**Options:**

1. Snapshot polling while any persona turn is queued/running.
2. NDJSON event stream from Harness runtime.
3. Local file/watch-based event feed for Windows Launcher.
4. Hybrid: immediate response envelope + bounded polling + event stream later.

**First rollout decision:** Hybrid is safest:

- Command response returns `execution_state` and IDs immediately.
- Launcher polls snapshot at a fast bounded cadence while state is active.
- Later stage can add streaming deltas.

**Acceptance:**

- UI updates status within a bounded interval after send.
- Timeout becomes `blocked: no runtime update after <N>s`, not indefinite waiting.

---

## Stage 65G — Enterprise security and operations hardening

**Objective:** Make the harness safe enough for long-lived enterprise use.

**Lanes:**

- Redaction for transcript/history/events.
- Runtime-root/profile mismatch detection.
- Auth/provider readiness checks.
- Budget/token accounting in persona turns.
- Archive/evidence retention.
- Cancellation race tests.
- Snapshot compatibility/versioning.
- Diagnostics export with raw logs behind explicit operator action.

**Acceptance:**

- Tests cover redaction and disabled/default profile non-leak behavior.
- Runtime blockers are actionable and classified.
- Evidence is preserved when chats/runs are archived.

---

## Implementation order

1. Stage 65A readiness envelope.
2. Stage 65B message append + auto-turn scheduling/readback.
3. Stage 65C selected transcript read model.
4. Stage 65D turn/event read model.
5. Stage 65E control capabilities/disabled reasons.
6. Stage 65F fast refresh/event stream.
7. Stage 65G enterprise hardening.

---

## Runtime definition of done

A real Harness smoke must prove one of these states after operator sends `hi`:

1. **Ready:** response includes `execution_state=queued|running|completed`, a `turn_id`, snapshot shows the turn, and transcript eventually includes assistant output.
2. **Blocked:** response includes `ok=false` or `execution_state=blocked` with typed `reason_code` and operator action.
3. **Sandbox:** response explicitly says sandbox and never masquerades as production runtime.

A command that returns success but leaves Launcher at indefinite `waiting for the next Harness snapshot` fails this stage.

---

## Implementation proof — 2026-06-18

Implemented the first live-turn slice for persona instance chat sends:

- `persona instance create/message` accept `--auto-run`, `--max-actions`, and `--max-seconds`.
- JSON readback now includes `execution_state`, `run_ids`, `turn_id`, `auto_run`, bounds, validation/token metadata, and a safe `next_expected` operator hint.
- `persona.instance.message` capability metadata is `bounded_execution` rather than `queues_only`.
- A ready profile can complete a bounded turn synchronously enough for Launcher readback.

Verified direct runtime proof:

```bash
HERMES_AGENT_RUNTIME_ROOT='X:/Eternia/.hermes/profiles/alice/agent_runtime' \nHERMES_HOME='X:/Eternia/.hermes' HERMES_PROFILE='alice' \nhermes harness persona instance message personainst_neko_supervisor \n  --message 'final structured auto-run smoke: reply READY only.' \n  --title 'final structured auto-run smoke' \n  --requested-by launcher --auto-run --max-actions 1 --max-seconds 45 --json
```

Observed result: `ok=true`, `execution_state=completed`, `turn_id=run_a4b10c139632`, `latest_validation_status=valid`, `latest_decision_type=propose_acceptance`.

Targeted gate:

```bash
python -m py_compile agent_runtime/capabilities.py hermes_cli/harness.py
pytest -q tests/agent_runtime/test_capabilities.py tests/agent_runtime/test_persona_assignments.py
# 20 passed
```

Remaining intervention:

- Dev/Backend/QA worker profiles are currently blocked by expired Codex OAuth. The runtime now returns structured blocked JSON instead of a traceback, but those personas cannot produce an assistant turn until their profile auth is repaired.
- Launcher Mission Control still reconciles the selected Operator Channel to the mission-scoped QA proof agent when the selected mission is a QA/proof task. Neko completion is proven at Hermes runtime level; full Neko-through-Launcher proof needs a mission scope where Neko is the active selected persona, or a product decision to let the Agent Console override mission-scoped selection.

