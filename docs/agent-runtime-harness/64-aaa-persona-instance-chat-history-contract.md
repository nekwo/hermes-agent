# Stage 64 — AAA Persona Instance Chat History Contract

> **For Hermes:** This is the runtime/source-of-truth companion to Launcher `docs/mission_control/26-stage-21-aaa-persona-instance-chat-history.md`. Use `staged-deep-audit-delivery` before implementation.

**Goal:** Provide a frozen, redaction-safe Agent Runtime Harness contract for persona instance chat history so Launcher Mission Control can list old chats, open a selected chat, and continue the correct persona instance without fabricating tasks/runs or spending tokens.

**Architecture:** Harness owns persona instances, assignments, old-chat session binding, and snapshot read models. Launcher consumes `harness snapshot --json` and calls declared capabilities. `open-chat` is a state-only operation; execution remains explicit through `message`, `run-once`, `diagnose`, task ticks, or run-until-settled.

**Audit date:** 2026-06-18

**2026-06-19 follow-up audit:** Stage 64A/64B are now partially implemented (`agent_runtime/persona_chat_history.py` and snapshot `persona_chat_history` exist), but this contract only covers old SessionDB-bound chat history. It does **not** solve free-floating `persona.instance.create/message --auto-run` final answer readback. For the current Agent Console stuck-at-refresh gap, use `71-mission-control-agent-chat-streaming-audit.md` as the superseding implementation audit.

---

## Current runtime audit evidence

### Implemented runtime pieces

- `agent_runtime/models.py`
  - `PersonaInstance` includes `id`, `persona_id`, `role`, `display_name`, `profile_id`, `runtime_root`, `state`, `mode`, `current_assignment_id`, `current_task_id`, `active_worker_session_id`, `active_run_id`, `session_id`, receipts, budget counters, heartbeat, and schema version.
  - `PersonaAssignment` includes instance/persona identity, kind/state/title/message, task/goal/stage links, proof/context fields, evidence kind, production proof eligibility, archive scope, and schema version.
- `agent_runtime/persona_assignments.py`
  - `PersonaInstanceStore.open_chat(persona_id, session_id)` exists and is explicitly state-only.
  - It sets `mode = "chat"`, stores the sanitized `session_id`, clears task/worker/run links, updates the instance, and emits `persona_instance.chat_opened`.
  - It does not create `PersonaAssignment` or `AgentRun`.
- `agent_runtime/capabilities.py`
  - declares `persona.instance.open_chat` with required args `persona_id`, `session_id`.
- `hermes_cli/harness.py`
  - parses `harness persona instance open-chat --persona <persona> --session-id <session> --json`.
  - rejects when persona assignment store is disabled.
- `tests/agent_runtime/test_capabilities.py`
  - asserts `persona.instance.open_chat` is present and requires `persona_id`, `session_id`.
- `tests/agent_runtime/test_persona_assignments.py`
  - `test_persona_instance_open_chat_binds_old_chat_without_ticking` proves open-chat sets mode/session and creates no assignments or runs.

### Missing runtime pieces for AAA

- No dedicated `persona_chat_history` snapshot read model exists yet.
- No redaction-safe chat-preview schema is frozen.
- No runtime test proves `harness snapshot --json` includes old chat history rows.
- No contract currently defines how SessionDB session ids map to persona ids/instance ids for old chats selected from Launcher.
- No snapshot-level test proves chat/opened mode is emitted in a Launcher-friendly shape.

---

## Frozen capability contract

### `persona.instance.open_chat`

**Semantics:** Bind/open an old chat session for a persona instance without model execution.

**CLI:**

```bash
python -m hermes_cli.main harness persona instance open-chat \
  --persona <persona-id-or-alias> \
  --session-id <safe-session-id> \
  --json
```

**Capability descriptor:**

```json
{
  "id": "persona.instance.open_chat",
  "target_kind": "persona_instance",
  "label": "Open Chat",
  "group": "lifecycle",
  "execution_semantics": "control_state_change",
  "required_args": ["persona_id", "session_id"],
  "danger": "normal"
}
```

**Success response minimum:**

```json
{
  "ok": true,
  "persona_instance_id": "personainst_dev",
  "persona_id": "dev",
  "session_id": "chat_old_123",
  "mode": "chat"
}
```

**Failure response minimum:**

```json
{
  "ok": false,
  "feature_enabled": true,
  "assignment_store_enabled": false,
  "error": "persona assignment store is disabled"
}
```

**Non-negotiables:**

- Must not create a task.
- Must not create a run.
- Must not create a persona assignment.
- Must not invoke a provider/model.
- Must emit `persona_instance.chat_opened` with `persona_instance_id` and `session_id`.

---

## Snapshot contract to add

Add an optional top-level field to `harness snapshot --json`:

```json
{
  "persona_chat_history": [
    {
      "session_id": "chat_old_123",
      "persona_id": "dev",
      "persona_instance_id": "personainst_dev",
      "title": "Launcher Dev operator channel",
      "last_message_preview": "Please inspect the latest proof.",
      "message_count": 12,
      "created_at": "2026-06-18T11:00:00Z",
      "updated_at": "2026-06-18T12:00:00Z",
      "state": "open",
      "redaction_status": "safe"
    }
  ]
}
```

### Field rules

- `session_id`: required, stable, redaction-safe session identifier; never a provider token or auth token.
- `persona_id`: required when known; aliases must be normalized to canonical persona ids.
- `persona_instance_id`: required when known; if absent during migration, compute `personainst_<persona>` consistently with `persona_instance_id_for`.
- `title`: safe display string. If unsafe or unknown, use `Untitled persona chat`.
- `last_message_preview`: optional safe preview. If unsafe, omit or set to `Preview hidden by redaction boundary` and set `redaction_status = redacted`.
- `message_count`: integer count of safe+redacted messages known for the session.
- `created_at` / `updated_at`: optional ISO timestamps.
- `state`: `open | archived` for first rollout.
- `redaction_status`: `safe | redacted` for first rollout.

### Source-of-truth rule

The history read model may read SessionDB and runtime state, but only Harness emits the final redaction-safe snapshot. Launcher must not crawl local Hermes state files or SessionDB directly.

---

## Stage 64A — Implement redaction-safe history builder

**Objective:** Build a pure runtime helper that returns persona chat-history rows without mutating state or invoking a provider.

**Files:**

- Create: `agent_runtime/persona_chat_history.py`
- Modify: `agent_runtime/snapshot.py`
- Tests: `tests/agent_runtime/test_persona_chat_history.py` or `tests/agent_runtime/test_snapshot.py`

**Implementation tasks:**

1. Create a `PersonaChatHistoryEntry` dataclass or plain dict builder with the snapshot fields above.
2. Read from SessionDB/runtime state through a narrow adapter; do not expose raw DB rows to Launcher.
3. Normalize persona aliases through existing persona alias helpers where possible.
4. Sanitize title/preview using existing redaction utilities or add focused sanitizer tests.
5. Limit rows for first rollout (for example newest 50) and document ordering newest-first.

**Tests:**

- Empty SessionDB/runtime returns `[]`.
- A safe session produces one row with safe preview and count.
- An unsafe-looking preview/title is redacted.
- A session linked by `PersonaInstanceStore.open_chat()` maps to the correct `persona_instance_id`.

---

## Stage 64B — Emit history in snapshot

**Objective:** Include `persona_chat_history` in `harness snapshot --json` behind the same enterprise persona-instance feature boundary.

**Files:**

- Modify: `agent_runtime/snapshot.py`
- Tests: `tests/agent_runtime/test_snapshot.py`

**Implementation tasks:**

1. Add `persona_chat_history` to the snapshot envelope when persona instance runtime / assignment store is enabled.
2. Preserve backward compatibility when disabled: either omit field or emit `[]`; choose one and document it. Preferred: emit `[]` for enabled-but-empty, omit for disabled.
3. Add a test that disabled config does not leak local chat/session state.
4. Add a test that enabled config emits redaction-safe rows.

**Acceptance:**

- `harness snapshot --json` can drive Launcher’s history UI without raw DB reads.
- Disabled/default Hermes profiles are unchanged.

---

## Stage 64C — Strengthen open-chat readback

**Objective:** Make `open-chat` response and subsequent snapshot readback stable enough for Launcher tests and visual proof.

**Files:**

- Modify: `hermes_cli/harness.py` if response lacks fields.
- Modify: `agent_runtime/persona_assignments.py` only if readback event/persistence needs strengthening.
- Tests: `tests/agent_runtime/test_persona_assignments.py`

**Tasks:**

1. Ensure success JSON includes `ok`, `persona_instance_id`, `persona_id`, `session_id`, and `mode`.
2. Ensure following `build_snapshot()` includes the instance with mode `chat` and same session id.
3. Ensure no assignments/runs are created by open-chat.

**Acceptance:**

- Launcher can dispatch open-chat and wait for snapshot readback without guessing.

---

## Stage 64D — Archive/history semantics

**Objective:** Preserve old chat history while supporting archive/close without evidence loss.

**Files:**

- Existing close/archive command implementation in `hermes_cli/harness.py` and `agent_runtime/persona_assignments.py`.
- New/updated tests in `tests/agent_runtime/test_persona_assignments.py` and history tests.

**Tasks:**

1. Define whether archived chat sessions stay in `persona_chat_history` with `state=archived`.
2. Ensure `close` affects active free-floating assignments, not read-only archived history rows.
3. Ensure `archive` does not remove SessionDB message records.
4. Add tests for archived history visibility.

---

## AAA runtime acceptance gates

```bash
python -m pytest tests/agent_runtime/test_capabilities.py \
  tests/agent_runtime/test_persona_assignments.py \
  tests/agent_runtime/test_snapshot.py

python -m hermes_cli.main harness persona instance open-chat \
  --persona launcher-dev \
  --session-id chat_old_123 \
  --json

python -m hermes_cli.main harness snapshot --json
```

Required proof claims:

- Open-chat returns ok.
- Open-chat creates no task/run/assignment.
- Snapshot includes the bound persona instance with `mode=chat`, `session_id=chat_old_123`.
- Snapshot includes redaction-safe chat history when enabled.
- Disabled/default configs do not expose enterprise history state.

---

## Gaps / interventions

1. **High:** Missing `persona_chat_history` snapshot model. Launcher cannot become AAA history UI without this contract.
2. **High:** Launcher does not yet dispatch `persona.instance.open_chat`.
3. **Medium:** `mode="chat"` is runtime-valid but not yet a first-class Launcher mode label.
4. **Medium:** Visual proof is missing after the latest Launcher auto-start fix.
5. **Medium:** Existing Stage 63/Stage 20 docs are stale in places and must be reconciled before delegating implementation.

Do not mark the persona instance/chat-history product AAA until these gaps are implemented or explicitly deferred with Tony’s acceptance.
