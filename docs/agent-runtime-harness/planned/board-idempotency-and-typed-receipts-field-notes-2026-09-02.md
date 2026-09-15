# Board idempotency and typed receipts — field notes (2026-09-02)

Running record for a four-row queue batch in `agent_runtime/`.
Branch `fix/board-idempotency-per-verb`, cut from `origin/main` at `b9e7a27988`.
Every premise was re-verified against code before anything was written; where a
row was wrong about its own location, that is recorded rather than quietly
corrected.

## Premise verdicts, before anything was built

| row | claim as filed | what re-verifying said |
|---|---|---|
| 1. board idempotency keys are per-board, not per-verb | `_record_idempotency` writes `{idempotency_key, card_id, recorded_at}`; `_idempotent_replay` is consulted by all three card verbs against one `boards/<id>/idempotency/` namespace | **VERIFIED, exactly as filed.** All three call sites confirmed. A key crossing verbs replays the other verb's card and the second write silently does not happen. |
| 2. `replay_ack` leaves `state: "accepted"` on a SETTLED replay | `chat_turn_reservations.py` copies `record.ack` and stamps `settled`/`exit_code` but never `state` | **VERIFIED.** `chat_turn.py`'s accept path writes `"state": STATE_ACCEPTED` **into** the ack, so the frozen word is real, not hypothetical. |
| 3. `mcp_not_registered_on_lane` cannot separate "no MCP client" from "the server failed", and its hint sends you to the healthy half | filed as: `agent_runtime/mcp_admission.py` reads `_MCP_AVAILABLE` | **SUBSTANCE VERIFIED, LOCATION WRONG.** `mcp_admission.py` never read `_MCP_AVAILABLE` — the flag lives in `tools/mcp_tool.py` and reaches admission only as an empty registrar return, which is precisely why the two were indistinguishable. See below. |
| 4. a recorded cold-spawn measurement is used as a discriminator it cannot support | `TRANSPORT_COLD`'s docstring claims ~3,200 ms; `launcher_qa`'s real cold spawn is ~100 ms | **VERIFIED on the prose, FALSIFIED on the code.** No production code compares an elapsed count. Three prose sites carried the stale rule; the operator-facing skill doc had already been corrected on 2026-08-26 and the repo's own docstrings had not caught up. |

## Row 3: the location was wrong and the wrongness was the bug

The row said admission "reads `_MCP_AVAILABLE`". It did not. An AST-free grep
over `agent_runtime/`, `tools/` and `hermes_cli/` finds the flag defined and read
only in `tools/mcp_tool.py` (`:204`, `:217`, and four guards). Admission's whole
knowledge of the SDK arrived as `register_mcp_servers` returning `[]` — the same
empty list a server that failed to connect produces.

That is not a nitpick about where a symbol lives; it is the mechanism. The two
conditions were indistinguishable **because** nothing in `agent_runtime` ever
asked the question directly. So the fix is not "raise where the flag is read" —
there was no such place on this side — it is to CREATE one:
`mcp_admission.mcp_sdk_available()`, the single seam through which
`agent_runtime` reads that flag, consulted in `admit_mcp_servers` when a server
missed the registry.

The cost of the old silence is on the record and is why this got its own code
rather than a better hint on the old one: a standing "the chat lane admits
nothing" gap was planned against for weeks while admission, declaration,
resolution and policy were all healthy and the venv simply had no `mcp` extra
(`harness-skills/harness-runtime-model/references/operations.md`, "Scope note
(2026-08-26)"; `…/references/proof.md` lists it as the Stage-C hazard to check
FIRST).

Scope held deliberately: `mcp_lane.mcp_lane_requirement_failures` also emits
`mcp_not_registered_on_lane`, but its row answers a different question ("MCP
tools are not registered on the `<lane>` lane") and its hint names the lane, not
the server. It was left alone.

## Row 1: why a sibling class, not a reuse

The row offered either. `IdempotentReplayUnresolved` is mapped into the CLI's
**retryable** set (`harness_support.py` — `retryable=… or error_code in {…,
"idempotent_replay_unresolved"}`) and its exit family is 7. That is correct for
what it means: a damaged FILE, which an operator repairs before the identical
call succeeds.

A key presented to two verbs is the opposite: the request is wrong, retrying it
unchanged is refused identically forever, and the cure is a distinct key. Sharing
the code would have made one half of the envelope lie about half the calls
carrying it — so `IdempotencyKeyVerbMismatch`, code
`idempotency_key_verb_mismatch`, exit family 2 (the fault is in the request and
the operator's next move is to change what they typed — the family rule the
`skill_slug_invalid` / `skill_installer_owned` rows already record), not
retryable, with its own hint that does NOT say "repair the receipt file".

Both refusals sit ahead of the `AgentRuntimeError` catch-all in
`_error_code_for_exception`, for the reason the three rows above them record: a
refusal is not a harness crash.

### The compatibility decision, and where it is written down

A receipt written before this change carries no `verb`, and it is honoured
exactly as before — replayed for whichever verb presents the key. Refusing an
unlabelled receipt would turn every in-flight key on every existing board into a
hard failure at upgrade, to protect against a crossing that already happened and
that refusing cannot undo. Receipts are per-board scratch state; the fence closes
as they are rewritten. Recorded in `_idempotent_replay`'s docstring, in canon 06,
and pinned by
`test_a_receipt_written_before_the_verb_field_is_still_honoured`.

## Row 2: the launcher reads no `state` at all

Before touching the wire, the launcher was read (read-only) for consumers of the
ack. There is exactly ONE decoder —
`lib/features/mission_control/remote/mission_chat_turn_rpc.dart`,
`MissionChatTurnAck.fromFrame` — and it reads `turn_request_id`, `request_id`,
`idempotent_replay`, `settled`, `exit_code`, `correlation_id`. It reads **no**
`state`, no `accepted`, no `verb`.

What it branches on:

- **`idempotent_replay`** is the only ack field that drives behaviour
  (`chat_turn_outbox.dart` counts a replay as a success);
- **acceptance** is `result` present with a non-empty `turn_request_id` —
  the wire's `accepted: true` bool is ignored;
- `settled` and `exit_code` are decoded and carried but never branched on;
- faults branch on `error.data.reason`, not on any ack field.

So `settled` stays the live discriminator, untouched — including its pairing with
a recorded `exit_code`, which answers the narrower question "is there a terminal
outcome to read". `state` is now re-stamped from the record. This corrects a
field nothing branches on today, precisely so a reader who DOES branch on it
later is not lied to — the launcher's OFFICE lane already treats
`result['state']` as "the discriminator the whole adoption branches on"
(`mission_office_rpc.dart`), and this ack was the one place that word meant
something else.

One launcher-side test fixture carries `'state': 'accepted'`
(`test/features/mission_control/remote/chat_turn_outbox_test.dart`) and asserts
nothing about it; a live-serve acceptance reads the runtime's on-disk receipt and
`_record`s `receipt['state']` without comparing it. Nothing there goes red.

**Two launcher findings filed rather than fixed** (this session cannot write the
launcher vault), both handed back with the queue target:

1. the launcher builds `runtime.chat.message` / `runtime.chat.steer`, and there
   is no launcher caller of `peer.chat.execute` at all;
2. because acceptance is "a `result` with a `turn_request_id`" and `state` is
   unread, a refusal returned as a `result` rather than an `error` would decode
   as an ACCEPT and the outbox would delete the row. Refusals are survivable only
   while hermes keeps putting them in the JSON-RPC `error` member.

## Row 4: the prose was stale in three places, the code never was

Searched for any clock-as-discriminator in code. There is none:
`profile_runner` counts `mcp_admission_cold_servers` by counting the
`TRANSPORT_COLD` **label**, and the label comes from
`classify_admission_transport`, which reads the live session map. `duration_ms`
is recorded and reported, never compared. So nothing was rewired.

Three prose sites carried the rule the row is about, all corrected:

- `mcp_admission.TRANSPORT_COLD`'s docstring — the "~400x the warm path" framing
  invited the threshold reading;
- `profile_runner.py`'s T2 comment — "a cold spawn is ~3,200 ms";
- canon `05-chat-turn-lane.md`'s Unverified carry-forward row.

The correction states the discriminator positively — a non-empty admitted SET
(`registered_mcp_server_names()` / `mcp_admitted_servers`) — and the fence that
keeps it true is
`test_no_admission_code_branches_on_an_elapsed_millisecond_count`, an AST walk
over `mcp_admission.py` for any `Compare` touching an elapsed name. A grep would
have matched the corrected comments themselves; the AST does not.

## Lesson worth keeping

**Two of the four rows were right that something was broken and wrong about
where.** Row 3's "location" was the defect itself (the question was never asked
on this side of the boundary), and row 4's "used as a discriminator" was true of
the documentation and false of the code — which matters, because "replace it with
the set check" would have been a change to code that was already correct. The
re-measure-before-fixing discipline paid twice in four rows.
