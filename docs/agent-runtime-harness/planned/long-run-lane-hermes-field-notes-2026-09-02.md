# Long-run lane — the hermes half (2026-09-02, running record)

Branch `feat/longrun-hermes-half`, worktree `X:/Eternia/_worktrees/w1-longrun`,
cut from `origin/main` at `b9e7a27988`. The three rows are the launcher design
note's §4 decision 5 (`EterniaLauncher/docs/mission_control/planned/
long-run-lane-design-2026-09-02.md`), filed there as "hermes rows to file now
(cross-repo, do not block W1b)". Written as the work happened; every claim below
was measured in this worktree.

## Row (i) — a per-draft lock in `CharacterDraft`

### The premise, and the packaging claim inside it

The row quotes `agent/charsheet/draft.py`'s own module docstring: *"There is no
lock — `agent_runtime.locks` is not in the shipped wheel — so two writers on one
draft are last-writer-wins per item."* Both halves check out, and the second one
is the more interesting:

- `agent_runtime/locks.py` **exists** and is a good cross-platform lock — one
  retry loop, `msvcrt` on Windows and `fcntl` elsewhere, a deadline, and a
  docstring recording that the two-arm version (bounded on one host, unbounded
  on the other) was the defect it was written to remove.
- It is nonetheless **not in the wheel**: `[tool.setuptools.packages.find]
  .include` in `pyproject.toml` lists `agent`, `tools`, `hermes_cli`, `gateway`,
  `tui_gateway`, `cron`, `acp_adapter`, `plugins`, `providers` — and no
  `agent_runtime`. The docstring is CURRENT, not stale.
- It is also not merely a packaging accident. Three modules in this package say
  the same thing in their own words as a design boundary:
  `agent/charsheet/__init__.py` ("nothing here imports `agent_runtime`"),
  `spec.py` ("no `agent_runtime` (not in the shipped wheel)") and `revisions.py`
  ("there is no lock (by design: this module must not import
  `agent_runtime`)"). The one `agent/` → `agent_runtime` import that exists
  (`skill_utils.py`'s `parse_cache`) is outside this package.

So the lock is built inside `agent/charsheet/`, on stdlib, and the docstring
was corrected only where it was now describing the OLD behaviour.

### Mechanism, and why not `msvcrt`/`fcntl`

`agent/charsheet/draft_lock.py`: an `O_EXCL` create of `generation.lock` beside
`draft.json`, holding `{draft, verb, pid, host, started}`; unlinked in a
`finally`; a second writer refused `DraftBusy`. The OS-advisory-lock alternative
was considered and rejected for two reasons specific to this lane:

1. **The refusal has to name the holder.** A byte-range lock answers "taken" and
   nothing else. A 15-minute `rows` batch and a crashed one are indistinguishable
   without the holder record, and the launcher's `next` hint would have nothing
   to say. A file whose CONTENT is the holder answers it.
2. **Pid liveness is not available on both hosts, so it is used on neither.**
   `os.kill(pid, 0)` KILLS the process on Windows — `tool/test_quality/README.md`
   records the mutation gate refusing exactly that probe for exactly that reason.
   Probing on POSIX and ageing out on Windows would rebuild the two-contracts
   defect `agent_runtime/locks._file_lock` was rewritten to remove.

The honest cost: an OS lock is released by the kernel when the holder dies, and
this one is not. A Reap & Restart mid-generation therefore leaves a lock file,
and the draft is exclusive until the ceiling laps it. The refusal names the file
to delete, in the message and in `safe_details.lock`, which is the same cure the
mutation gate prints for its own crashed-holder case.

### The ceiling: 45 minutes

`STALE_HOLDER_SECONDS = 45 * 60`, pinned as a RELATIONSHIP rather than a number
(`test_the_ceiling_clears_the_launcher_long_run_ticket_it_must_not_break`):

- above the launcher's 30-minute `kHarnessLongRunCeiling`, so hermes can never
  break a generation whose launcher ticket is still valid — the one thing this
  lock must not do;
- above hermes's own worst-case `auto` (11–22 min by the rate estimate in
  `_characters_auto_write` / `_characters_auto_plan`);
- low enough that a crashed generation clears itself inside one operator break.

### Where the lock is taken, enumerated from the write paths

| verb | writes | locked |
|---|---|---|
| `run_turnaround` | provider call + `store.propose` × N + `_save` | **yes** |
| `run_rows` | per row: provider call + `propose` + `approve`; then `_save` | **yes**, for the WHOLE batch |
| `reroll_direction` | reads the attempt count, then `propose` writes it | **yes** |
| `reroll_row` | same read-then-write on the attempt count | **yes** |
| `auto` (CLI) | calls the four step bodies | **yes**, once around the whole plan |
| `compose` | reads `store.current`; writes the INSTALL + the stage | **no** — see below |
| `approve_direction`, `approve_all_directions`, `add_state`, `reopen` | one small store/spec write | **no** |

`compose` is out on the row's own conditional ("`compose` if it writes
revisions"). It writes no revision: it reads the approved current of every row,
and its failure mode against a batch still running is an explicit refusal
naming the rows with no approved strip. Two composes racing write the same
install atomically. Under `auto` it runs inside the plan-wide lock anyway.

The two re-rolls are IN even though the row named only the three batch verbs.
They are the same bug class through a sibling path, with a sharper edge: the
attempt count is read to name the output file and then written by `propose`, so
two re-rolls of one row pick the same filename and the second overwrites the
first's bytes.

The approvals are OUT deliberately. Each is one small write an operator makes by
hand from the QA surface; refusing a click because a batch is running would cost
more than it protects.

### Re-entrancy is per THREAD

`auto` holds the lock and then calls the verbs that take it, so the holder must
re-enter. A different thread is a second writer — that is the serve pool's
shape, `ThreadPoolExecutor(max_workers=4)`, one worker per request — and takes
the file like any other process. The registry keys on `threading.get_ident()`
and only ever short-circuits for the holder; the nested exit does not unlink
(`test_the_same_thread_re_enters_and_the_nested_exit_does_not_release`).

### DIVERGENCE from the row: `_error_code_for_exception` was NOT touched

The row asks for `DraftBusy` to be "mapped in
`hermes_cli/harness_support.py::_error_code_for_exception` AHEAD of the
catch-all". It was not, and this is deliberate.

`_error_code_for_exception` has exactly one caller, `emit_harness_error`
(receiver-checked: no other call site in production). **No `characters` verb ever
calls `emit_harness_error`** — the whole lane emits the flat pets error shape
through `_characters_error` (`{"ok": false, "error", "draft", "stage", "next"?}`,
exit 2) and says in its own docstring why: a launcher panel that already parses
the pets shape should not have to learn the Stage-42 envelope for its sibling
verbs.

A mapping row there would therefore be unreachable — and this repo has a dated
ruling against exactly that, written into the function it would sit in: *"Four
rows left this tuple on 2026-08-19 … each class existed to be mapped here and
nowhere else. A mapping row for an exception nothing throws is not defensive, it
is a claim about the runtime that is false."*

What the lane actually reads is `_CHARACTERS_EXPECTED`, and that is what changed:
it now catches `CharsheetRefusal`, and `_characters_refusal_extra` puts the
typed `code` on the flat payload beside `busy` (the holder) and a `next` hint.
So the token a consumer branches on exists and travels — through the lane that
exists rather than the one that does not.

`ERROR_EXIT_CODES` was left alone for the same reason. Had a row been added it
would have been family 6, beside `pairing_codes_pending` / `pairing_locked_out`,
whose recorded reason is this one exactly: the operator's next MOVE is to WAIT,
nothing is broken, and the identical command succeeds later unchanged.

### `next` carries no `alternatives`, on purpose

`_characters_next`'s docstring: *"`alternatives` carries the second legal move
when there genuinely are two"*, and *"a verb with no next step omits `next`
rather than carrying an empty one"*. For a busy draft there is one move —
hermes cannot cancel a running generation (`cancel_denied`), so the answer is
wait, and `characters status` is the command that says what has landed so far.
The rows-resume hint is explicitly overridden for this refusal: `on_error` runs
first, `_characters_refusal_extra` last, because a batch that was never admitted
has no rows that "never landed".

## Row (ii) — the drain and `is_chat_turn` hold for the generate verbs

Premise verified at `b9e7a27988`: `_CHAT_TURN_COMMANDS` was
`mission-chat message|steer` and nothing else; `_ArgvRequest.__init__` derived
`is_chat_turn` from it; `_busy_frame` counted only those; and the drain
monitor's expiry frame set `"terminal": not chat_turn_ids`. A `characters rows`
request was invisible to every one of them.

Built: `_LONG_RUN_COMMANDS` beside `_CHAT_TURN_COMMANDS`, a sibling
`is_long_run` derived by the same prefix match on the same argv tail, and both
counts reported ADDITIVELY —

- `busy` gains `long_runs`; `chat_turns` keeps its name and meaning;
- `drain_timeout` gains `held_by_long_runs` + `long_run_request_ids`;
  `held_by_chat_turns` + `chat_turn_request_ids` keep theirs;
- `terminal` becomes `not (chat_turn_ids or long_run_ids)`, and the
  keep-serving re-arm branch takes either.

A sibling flag rather than a `holds_drain` union because the frame reports the
two SEPARATELY: "held by 1 chat turn" and "held by 1 `characters rows`" are the
same `terminal: false` with very different waits behind them.

**Cancel semantics are unchanged.** A running request still answers
`cancel_denied`; `harness stream` is still the sole cooperative exception.

### The launcher-reader findings (read-only grep over `EterniaLauncher/lib/`)

| key | readers in `lib/` |
|---|---|
| `held_by_chat_turns` | **none** |
| `drain_timeout` | **none** — the launcher decodes no drain frame at all |
| `chat_turns` (on `busy`) | **one**: `mission_control_serve_session_io.dart`, the `busy` case → `MissionServeBusySignal.chatTurns` → `mission_transport_health.noteServeBusy` |
| `request_progress` | mentioned in two comments; no handler |
| `{"op":"drain"}` sender | `lib/features/mission_control/remote/lan_socket_connector.dart` sends `{'op':'drain','force':true}` |

So keeping `held_by_chat_turns` is a contract obligation rather than a live-reader
one, and it is kept anyway: the drain lane IS exercised by the launcher (the LAN
socket connector sends the op), and a supervisor that learned a renamed key would
be one that stopped reading the old one mid-upgrade. The key with a live decoder
is `chat_turns` on `busy`, and it is untouched.

### The hold is only bounded because row (iii) exists

A drain held by a wedged generation would never end. Before this branch a
generation had no hermes-side bound at all, so extending the hold without row
(iii) would have traded a killed generation for a wedged drain. The two changes
are one change, and the module docstring and canon 03 both say so.

## Row (iii) — a provider-call timeout in `pipeline.py`

Premise verified: `grep -n timeout agent/charsheet/pipeline.py` → nothing, and
`_generate_image` is the one seam. Two things the row does not say, both
measured here:

- **`imagegen.generate` has no `timeout` parameter** to wire a real one through
  (`agent/pet/generate/imagegen.py`: `prompt, n, reference_images, provider,
  prefix, aspect_ratio`), so "prefer wiring the real one" has nothing to wire.
- **The backends do each set one, and they disagree by an order of magnitude**:
  `openrouter` `_REQUEST_TIMEOUT = 300.0`; `openai-codex`
  `httpx.Timeout(300.0, read=300.0)`; `krea` `_POLL_TIMEOUT_SECONDS = 180.0`;
  `xai` 120. **`openai` constructs a bare `openai.OpenAI()`** with no timeout —
  the SDK default is 600 s with retries, so ONE row could hold a serve pool
  worker (one of four, with the stream subscription already holding another) for
  half an hour. That is the hole, and it is provider-specific rather than
  universal.

Built: `PROVIDER_TIMEOUT_SECONDS = 300.0`, read through
`provider_timeout_seconds()` from `charsheet.provider_timeout_seconds` in
`config.yaml` (`hermes_cli/config_defaults.py`; `.env` is for secrets and every
behavioural setting including a timeout belongs in `config.yaml`, per AGENTS.md).
`_within_deadline` runs the call on a **daemon** thread and joins with the
budget; expiry raises `ProviderTimeout`. A non-positive budget runs inline, on
this thread, byte-identical to the old behaviour.

**Why 300 s:** the largest ceiling any shipped backend sets for itself, so
hermes never cuts off a provider still inside its own contract; and far past the
1–2 min a healthy generation takes, so a slow roll is not mistaken for a wedged
one. It converts "unbounded" into "one row, bounded". It is deliberately not a
bound on the BATCH — a different knob and a different decision — and the test
pins the relationship that matters (one wedged call cannot spend the launcher's
whole 30-minute ticket).

**The honest cost:** the abandoned worker is a leak until the backend's own
socket timeout fires. Nothing here can interrupt a blocking HTTP read. What the
deadline buys is the released pool worker, a process that can still exit
(daemon), and a typed refusal the verbs already know how to report — `rows` gets
its existing resume hint for free, because `ProviderTimeout` is caught by
`_CHARACTERS_EXPECTED` and `on_error` still runs.

## Tests

| property | test |
|---|---|
| a second writer is refused, and told who holds it | `tests/agent/test_charsheet_draft_lock.py::test_a_second_writer_on_one_draft_is_refused_and_told_who_holds_it` |
| the same defect through the real verb, mid-batch | `tests/agent/test_charsheet_draft.py::test_a_second_generation_on_one_draft_is_refused_rather_than_interleaved` |
| `auto` holds once, around the whole plan | `tests/agent/test_charsheet_draft_lock.py::test_the_same_thread_re_enters_and_the_nested_exit_does_not_release`, `tests/hermes_cli/test_harness_characters_cli.py::test_the_autopilot_refuses_a_busy_draft_before_it_runs_one_step` |
| a crashed generation does not wedge the draft | `…test_charsheet_draft_lock.py::test_a_stale_holder_is_broken_by_the_age_ceiling_and_not_by_a_pid_probe`, `…test_harness_characters_cli.py::test_a_stale_holder_does_not_wedge_the_draft_forever` |
| a drain deadline expiring during a generate verb re-arms instead of terminating | `tests/agent_runtime/test_serve_socket_lane.py::test_a_live_character_generation_survives_a_drain_the_way_a_chat_turn_does` |
| a `characters` READ does not hold the drain | `…test_serve_socket_lane.py::test_a_read_that_is_not_a_long_run_does_not_hold_the_drain` |
| a provider that never returns yields the typed refusal within the deadline | `tests/agent/test_charsheet_provider_deadline.py::test_a_provider_that_never_answers_is_refused_within_the_deadline` |
| a real provider fault is not lost behind the wrapper | `…test_charsheet_provider_deadline.py::test_the_providers_own_failure_still_reaches_the_caller` |

## What the mutation gate found in this branch's own code

The first gate run had one SURVIVOR:
`longrun-nested-release-hands-the-draft-away-mid-auto`, which mutated
`released = held[1] <= 0` to `released = True` in the lock's release path and
changed nothing any test could see.

It was right, and the code was wrong. The reentrant acquisition **returns
before** that block, so nothing can ever reach the release path at depth > 0 —
the guard could not be false, and a branch that cannot be false is a claim about
the runtime that is not true. The `finally` now pops the registry entry and
unlinks unconditionally, with the reason written beside it, and the claim was
re-aimed at the property a test CAN distinguish: deny same-thread re-entry
(`if entry is not None and entry[0] == ident:` → `if False:`) and the nested
`with` inside `auto` raises `DraftBusy` at itself.

Second run: 10 candidates, 10 killed, exit 0.

## Two pre-existing mutation claims re-anchored (not this branch's work)

`scripts/changed_line_mutation_check.py --list` refused to run at all on
`b9e7a27988` for two claims rotted by `a3b48a06a2` ("absent is not empty"),
which changed the lines they named without moving them:

- `s4-an-omitted-skill-flag-clears-every-instance-override` → the block is now
  `skills=requested_skills,`;
- `s4-the-cli-create-door-drops-its-skill-flag` → now
  `requested_skills = list_flag_or_absent(args, "skills")`.

Both were re-anchored to the current source with their guarantees unchanged,
because the gate cannot run past a configuration error and this branch is
required to run it. Two `dcw-h4-*` claims were re-anchored to
`_characters_auto_steps`, which IS this branch's doing (the `auto` body moved
into that function so the lock can refuse before the plan runs).

**A resolver blind spot worth a row of its own:** `_drain_monitor` is defined
inside a `try:` block inside `serve_loop`, and `_qualified_definitions` walks
only `ast.iter_child_nodes`, so a def nested in any non-`def` block is invisible
to the anchor. The claim is spelled `serve_loop/_drain_monitor terminal`
(resolve the outer function, prose names the line) as the available workaround.
