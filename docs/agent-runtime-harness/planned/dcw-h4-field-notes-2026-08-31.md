# W2-H4 field notes — charsheet autopilot (Stage 5) + element pairing (Stage 6)

Running record of the decision-close wave's W2-H4 agent, hermes side. Branch
`feat/dcw-h4-charsheet-autopilot`, based on `origin/main` at `301946bc57`.
Build spec: `planned/charsheet-turn-efficiency-2026-08-29.md` Stages 5 and 6.
Ruling: RD-10 of the launcher's `decision-close-wave-2026-08-31.md` — R-3 YES.

## §1 Premises re-measured before writing a line

The stage text was written 2026-08-29 and six stages have shipped since. Every
premise Stage 5 and Stage 6 rest on was measured against the shipped code
first.

| Premise (plan) | Measured | Verdict |
|---|---|---|
| Stage 5 composes over the interactive verbs' per-attempt history | every verb handler calls exactly one `CharacterDraft` method and the history lives in `ImageRevisionStore` (`agent/charsheet/draft.py`) | HOLDS — the autopilot calls the same four methods, so `reopen` repair is inherited, not re-implemented |
| Stage 5 "consumes the machine `next` hints (4b)" | 4b shipped `_characters_next` at `hermes_cli/harness.py:4025` with three arms: `start`, `approve-direction` (when it advanced), failed `rows` | HOLDS, with one correction below |
| Stage 5 rides process-exit delivery (3b) | `process notify` is live (`tools/process_registry.py`, `notify_on_exit`); nothing about it is verb-specific | HOLDS — `auto` needs only to be ONE long process |
| "newline-delimited payloads" | `emit_json` (`agent_runtime/cli_format.py:8`) is `indent=2` — every existing `--json` payload is a multi-line block | **FAILS as written.** NDJSON needs a compact dump. Resolved by adding `emit_json_line` beside `emit_json` rather than by hand-rolling a second encoder |
| Stage 6 "key by tool-call id, not arrival order" | the pairing site is `_ChatProtocolV2Emitter._tool_finished` (`hermes_cli/harness_parts/persona_commands.py:6067`), which pairs by tool NAME and `stack.pop()` — LIFO | mechanism CONFIRMED, **but there is no tool-call id at this sink**: `_progress_payload_from_callback` (`agent_runtime/profile_runner.py:1852`) is handed `(event, tool_name, invocation, result)` and no id exists in the callback contract at all |

### The measured Stage 6 mechanism, exactly

Two same-named tools start concurrently (`skill_view` × 2, the fire-imp
elements `[0]`/`[1]`). Starts append in order: element 0 carries summary A,
element 1 carries summary B. `_tool_finished` pops the LAST element for that
name, so finish-A lands on element 1 and finish-B lands on element 0. The
finished payload's `tool_input` WINS over the started one
(`payload.get("tool_input") or tool.get("tool_input")`, line 6119), so element
0 ends up with summary A and `tool_input` B, and element 1 the reverse. That is
the crossed pair the plan filed under bucket (f), reproduced from the code
without needing the record.

The fix therefore cannot be "key by call id" — the id does not exist here. The
identity that DOES reach both events is the invocation itself, rendered to the
`tool_input` block by `_attach_tool_io`. Pairing matches on it, and falls back
to FIFO (not LIFO) when a call carries no input record.

## §2 Corrections to the stage text, made deliberately

1. **`--only` on `rows`, and re-running what already landed.** The stage text
   describes `auto` as driving `turnaround → approve-direction --all → rows →
   compose` flatly. Driven flatly it is destructive: `run_turnaround` re-rolls
   every direction reference AND clears the approvals, and `run_rows(only=None)`
   regenerates every authored row including the ones an operator kept. On the
   `reopen`-repair path that plan text throws away the QA the operator just did
   and spends 10–20 minutes of generation doing it. The autopilot therefore
   plans from the draft's STATE — the same two lists the 4b resume hint reads,
   `status --json`'s `missing.turnaround` and `pending.rows` — and reports every
   step it skipped and why.
2. **No `next` hint on a compose refusal.** The obvious hint is
   `compose --accept-handedness <token>`, and that is precisely the override
   R-3 forbids the autopilot from nudging anyone toward. The refusal text
   already names what to look at; the autopilot adds nothing to it.
3. **One framing, no mixed shapes.** `auto` never calls `_characters_error`
   (which emits the pretty `emit_json` block): a stream whose framing is the
   newline cannot emit one multi-line block for a bad `--draft`. Every line
   `auto` writes — receipts, the refusal, the summary — goes through the same
   NDJSON writer, and the LAST line is always the summary.
