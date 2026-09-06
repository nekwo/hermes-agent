# Local runtime ownership — L-h field notes (hermes), 2026-09-05

Running record for stage 8.4 L-h of the launcher plan
`local-runtime-ownership-and-retry-safety.md`, written as the work happened.
Worktree `wt/l-serve-service`, branch `feat/l-serve-service-mode`, from main
`997900010e`. Nothing here touched the operator's live store or the live venv:
every run was `scripts/run_tests.sh`, and every spawned child in the e2e file
gets its own sandboxed `LOCALAPPDATA` / `HOME` / `USERPROFILE` / `HERMES_HOME`
alongside a `tmp_path` runtime root.

## 0. Revalidation, before anything was changed

The plan's §8.1 row for F1 is accurate against `997900010e`. Read and confirmed:

| Claim | Verdict at pickup |
|---|---|
| the stdio reader IS the main loop; `for raw in reader:` → `finally: pool.shutdown` → `_close_socket_lane("shutdown")` → `_unregister_instance()` → 0 | **still_present**, exactly as described |
| `serve` flags are `--ndjson`, `--pool-size`, `--no-socket`; no service/detach flag | **still_present** |
| loopback `ServeSocketServer`, per-root token HMAC hello, argv + JSON-RPC + `subscribe` on the socket | present — reused, not rebuilt |
| `{"op":"drain","force":true}` over the socket already ends the service | present — it is the stop verb this stage parks on |
| `shutdown` is the stdio owner's verb only | present — and now the one thing that still means "stop" before EOF |
| `SocketOwnerLock` excludes one SOCKET owner per root; the loser can still serve stdio | present — and that is precisely the hazard item 2 closes for `--service` |

Nothing in F1 had shipped. No production code was reconstructed and no
primitive was duplicated: the whole stage is a lifetime decision plus five
published fields.

## 1. What landed, item by item

| L-h item | Commit | What landed |
|---|---|---|
| 1 — the service loop | `e8d22cc036` | `--service` flag; `serve_loop(service=…)`; `_FrameWriter(detachable=…)` + `detach()`; `_install_service_stop_signal` / `_restore_service_stop_signal`; `service_stop` event set by the drain's terminal path; the detach receipt and the park; `_cmd_serve` wiring and the `--service --no-socket` refusal; the module docstring's "Service mode" section |
| 2 — the lock-loser exit | `ccb5c92186` | the `serve_owner_exists` branch in the socket-lane block: frame + service-log line, sidecar release, `return 0` before pool / registry / ready |
| 3 — the published fields | `25b540fae8` | `service` + `starter_pid` on the registry row, `ready`, `hello_ok`, `version`; `service` on the `ops` manifest for every transport; both lifted to the top of the `serve connect --probe` report; registry and manifest-golden tests |
| 4 — the seam tests | `c1e8ebcaf3` | `tests/agent_runtime/test_serve_service_mode.py`, 10 tests |
| 4 — the child e2e | `3b21ca2ec9` | four arms in `tests/agent_runtime/test_serve_socket_child_e2e.py` |
| these notes | this commit | — |

No wire contract moved. `OPS_CONTRACT_VERSION` is still 1 and
`SERVE_SCHEMA_VERSION` is still 1: every change here is an ADDED key on a block
that already exists, which is the set-plus-integer rule those blocks already
document.

## 2. Exactly how the main thread waits, and what wakes it

This is the question the launcher half (L-l) has to build against, so it is
stated in full.

**Where it waits.** Inside `serve_loop`'s reader `try:`, immediately after the
`for raw in reader:` loop ends and BEFORE the `finally:` that sets
`reader_unwound` and joins the pool. That placement is the whole design: the
park sits above the finalization, so releasing it runs the untouched teardown in
the untouched order rather than a second copy of it.

```
for raw in reader: ...            # EOF, or `break` on a stdio shutdown
if service and not stdio_shutdown: # EOF only
    _detach_stdio_owner()
    _park_until_service_stop()
finally:
    reader_unwound.set()
    pool.shutdown(wait=pool_shutdown_wait)
```

**How it waits.** `while not service_stop.wait(_SERVICE_PARK_POLL_SECONDS)`,
with `_SERVICE_PARK_POLL_SECONDS = 0.5`, breaking early if
`drain_terminal_published` is set. The **event is the mechanism**; the poll is a
bound, not a substitute — a missed wakeup would otherwise be a runtime that
drained, published its terminal frame, and then sat there forever. Half a second
of a sleeping thread costs nothing and turns that class of bug into a half-second
delay.

**What wakes it, all three:**

1. **`{"op":"drain","force":true}` over the socket.** `_finish_drain` sets
   `service_stop` in the same breath as `drain_finished`, immediately before the
   existing `drain_wakeup()` — the same instant, the same ordering, so a service
   drain and a stdio drain unwind identically. On a stdio serve the wakeup
   closes the protocol descriptor and the reader falls out; on a service the
   reader is already gone and this event is the lever.
2. **`SIGTERM`**, via a handler installed only for the duration of the park and
   restored when it ends. See §5 for what this is and is not worth on Windows.
3. **A stdio `{"op":"shutdown"}` received BEFORE EOF** — which never reaches the
   park at all. It breaks the reader loop with `stdio_shutdown = True`, so the
   detach and the park are both skipped and the runtime ends exactly as it
   always has. This is the sentence that keeps `--service` safe for Update /
   Repair to adopt: they still own the pipe, so they still own the verb.

**What detaching does, in order.** `_service_log(detached)` first (so a starter
that is merely slow to close still sees it), then `_broadcast_lanes(detached)`
(so an attached socket client learns its runtime's starter has gone), then
`frames.detach()`. After that the stdio `_FrameWriter` drops frames instead of
writing them, and a write that loses the pipe between the check and the syscall
latches the same state from its `BrokenPipeError` rather than raising. Service
logs route through `sys.stderr`, which is the line-frame proxy, which is the
same writer — so after detach the service log is silent too. That is deliberate
(see §5, open item 2).

**What the exit path does not change.** Drain accounting, the terminal-frame
latch, `_force_exit_after_drain`'s deadline (which waits on `drain_finished` AND
`reader_unwound`, both of which a released park still sets), the
`drain_abandoned` grace window, the exit codes. All untouched.

## 3. The lock loser, and why exit 0

`SocketOwnerLock` was already the right primitive and is unchanged. What changed
is what a `--service` loser does with the answer.

A stdio loser has an owner waiting on a pipe, so "keep serving stdio" is a real
job and stays exactly as it is (pinned by a test). A service loser has nobody:
it was asked to be *the* runtime for this root, one already exists, and carrying
on would make it a second execution process against one store that nothing
discovers, nothing drains and nothing knows to stop — F1's "extra stdio
executor", stated literally.

So it emits `{"event":"serve_owner_exists","pid":<winner>,"port":<winner's>,…}`
as **both** a frame and a service-log line, releases the lock object, and
returns 0 — from inside the socket-lane block, which is before the request pool,
the registry row and the ready frame. Both channels, because either can be the
only one present: a starter that spawned it over pipes reads the frame, and a
starter that spawned it detached (the normal case under RL-2) has no stdout at
all.

The winner's port comes from `read_socket_owner` (the sidecar), because
`SocketLockResult.payload()` carries pid and `owner_started_at` but not the
port — the sidecar is the only place a loser can learn where to attach.

Exit **0** is the deliberate part. Losing this race is the ORDINARY outcome of
two starters, and RL-4 says the caller's next act is to re-read the registry and
attach to the winner. A nonzero code would read as "the runtime failed to start"
about a root that has a perfectly healthy runtime.

## 4. Proof, and the red proofs

Focused suites, all green on the final tree (`scripts/run_tests.sh`,
`--file-timeout 900`):

| Suite | Result |
|---|---|
| `test_serve_drain_accounting.py` | 9 passed — the EOF arms untouched |
| `test_serve_socket_lane.py` | 71 passed |
| `test_serve_registry.py` | 29 passed (27 + 2 new) |
| `test_serve_stream_lane_parity.py` | 19 passed (two manifest goldens extended) |
| `test_serve_service_foundations.py` | 27 passed |
| `test_serve_rpc_method_tiers.py` | 7 passed |
| `test_harness_serve.py` | 33 passed |
| `test_introduce_is_unreachable.py` | 6 passed |
| `test_serve_service_mode.py` | 10 passed (new) |
| `test_serve_socket_child_e2e.py` | 7 passed (3 + 4 new) |

A 10-file run of the above totalled **216 passed, 0 failed**.

**Red proof 1 — the park.** Disabling `if service and not stdio_shutdown:`
returns the loop to today's behaviour. Three tests go red on a missing detach
receipt: the survival test, the quiet-pipe test, and the drain-a-detached-service
test. Restored, all green.

**Red proof 2 — the lock-loser exit.** Disabling the `if service:` branch makes
the loser a full stdio serve that reaches EOF and PARKS — and the file **times
out** rather than failing an assertion. That is the finding demonstrating
itself: the sabotaged build produces exactly the undiscoverable, undrainable
second executor the branch exists to prevent.

**One thing the seam tests deliberately do not assert.** A `serve_loop` running
inside pytest has a pytest command line, so `classify_serve_instance` answers
`unknown` — its fail-safe direction working correctly on a process that is not a
`hermes serve`. Live classification is proven where the process really is one:
the e2e arm reads `target.classification == "live"` off a real
`serve connect --probe`.

## 5. Deviations from the plan text, and open items

**Deviation 1 — `--service --no-socket` is refused.** The plan did not mention
the combination. It asks for a process with no way in and no way out: the park
outlives stdin and the socket lane is the only lane a drain can arrive on, so on
Windows the result would be unkillable short of a forced terminate. Refused at
the CLI (exit 2, `unsupported_combination`) rather than accepted and degraded.
Each flag remains valid on its own, and `serve_loop(service=True,
socket_lane=False)` is still constructible for a unit test.

**Deviation 2 — the detach receipt is broadcast as well as logged.** The plan
says "log". It is logged, and additionally broadcast to attached socket clients,
because a client holding a runtime sheet at the moment of detach wants that fact
and would otherwise have to poll for it. Additive, and unknown events are
already ignored by every consumer on that lane.

**Deviation 3 — `service` / `starter_pid` are top-level keys on the three
frames, not a nested block.** The plan says "the `ready` / `hello_ok` /
`version` blocks gain". They sit beside `pid` and `boot_id`, which is where a
reader of those frames looks for facts about the process. The `ops` manifest
gets its own `service` key as specified, and that one is read two ways on
purpose: PRESENCE is RL-2's membership gate (a hermes predating this stage
carries no `service` key anywhere), and the value is this process's own
lifetime.

**Open item 1 — Windows SIGTERM is unproven and very nearly undeliverable.**
The handler is installed, and on POSIX `kill <pid>` is an ordinary stop. On
Windows nothing in the OS delivers SIGTERM to another process:
`os.kill(pid, SIGTERM)` is `TerminateProcess`, which never runs a handler. **No
test asserts SIGTERM behaviour on either platform** — this repo's CI is the
Windows machine, and a test that cannot run there would be a test that passes by
being skipped. The Windows stop verbs are therefore the socket ones, which is
what the launcher will use and what the e2e arm exercises. `signal.signal` also
refuses to run off the main thread, which is where every `serve_loop` unit test
calls it from; that degrades to "no handler", i.e. today's behaviour, and is why
the installer never raises.

**Open item 2 — a detached service's service log goes nowhere.** After
`frames.detach()`, `_service_log` routes through the stderr line-frame proxy
into a null sink, so `serve_socket_owner_takeover`, connection logs and drain
lines are lost for the rest of that runtime's life. Writing them to the real
fd 2 instead was considered and rejected: a detached child's fd 2 may be an
inherited pipe nobody reads, and a full pipe buffer would block the runtime —
trading a lost log line for a hang. A file-backed service log is the right fix
and is out of this stage's scope.

**Open item 3 — no idle exit.** RL-1 says so explicitly: no idle exit by
default, and `--idle-exit-seconds` needs its own ruling. Nothing here adds one.

**Open item 4 — the reaper.** RL-3 (both launcher reap policies must SPARE a
pid with a live registry row) is L-l's, and it is BLOCKING for the launcher
half: a detached service has a dead ancestor chain by construction, so today's
`ServeOrphanReapPolicy` would kill the runtime this stage creates at every
launcher boot. Nothing on the hermes side can prevent that; the row is named
here so it cannot be lost between the two halves.

**Open item 5 — two machines and a real detached spawn.** Everything here runs
on one machine with children this repo spawned. The proof that a launcher can
close, leave the runtime up, and re-attach to the SAME pid is L-l step 7 and is
the operator's.

---

# L-h-b — RL-13, standing subscriptions are not "busy" (2026-09-05)

Second hermes stage on the same plan (`local-runtime-ownership-and-retry-safety.md`
§8.8), written as the work happened. Worktree `wt/l-serve-service`, branch
`feat/lh-busy-subscriptions`, from main `286a29db04`. One file of product code
(`hermes_cli/harness_parts/serve.py`), one test module, one canon doc. Nothing
touched the operator's live store or the live venv: the whole stage is held at
the `serve_loop` seam with injected streams, so no child process and no real
`HERMES_HOME` were involved at all.

## 1. Revalidation, before anything was changed

§8.8 item 4 describes the mechanism exactly. Read at pickup:

| Claim | Verdict at `286a29db04` |
|---|---|
| `_ArgvRequest.is_runtime_stream` is `tail[0] == "stream"` | present |
| `_report_quiet_requests` skips `is_runtime_stream`, and its docstring says why ("it is the infinite subscription, it is silent between events BY DESIGN") | present |
| `_liveness_pump` guards on `if not pending: continue` and nothing else | present |
| `_busy_frame` returns `event` / `chat_turns` / `long_runs` / `pending` only | present |
| the launcher decodes `chat_turns` **and** `pending` off `busy` by NAME | confirmed in `EterniaLauncher/lib/features/mission_control/data/mission_control_serve_session_io.dart`, the `busy` case — and the switch reads no other key, so additive keys are ignored by construction |

So the split had to be additive, and `pending` had to keep meaning *everything
in flight* rather than quietly becoming the new `work`.

## 2. The red, measured

Three tests written first, as a new DEFECT D section of
`tests/agent_runtime/test_serve_request_silence.py`. That module is the right
home and not a convenient one: it exists because the pump was too QUIET, and
this row is the same pump being too LOUD — same seam, same lane, opposite
failure. The module docstring now pins four defects instead of three.

| Test | Expected | Actual before the fix |
|---|---|---|
| `test_standing_subscriptions_alone_never_wake_the_liveness_pump` | no `busy` frame across three pump intervals, with two `harness stream` requests in flight and no work | **six** frames, every one `{'event': 'busy', 'chat_turns': 0, 'long_runs': 0, 'pending': 2}` — the operator's pasted terminal, reproduced verbatim at the seam |
| `test_one_chat_turn_behind_two_subscriptions_pumps_work_one` | a pump frame with `work: 1`, `subscriptions: 2`, `pending: 3` | `KeyError: 'work'` |
| `test_ping_on_an_idle_service_still_answers_with_every_count` | `ping` answers `work: 0`, `subscriptions: 2`, `pending: 2` | `KeyError: 'work'` |

`3 failed, 5 passed` on the module before the fix; `8 passed` after.

The first red is the one worth keeping. It is not a missing-key failure — it is
the product defect printed six times in 350 ms. A launcher that stays attached
for an hour reads 720 of them.

## 3. The fix

`_busy_frame` counts `subscriptions` in the same locked pass that already counts
`chat_turns` and `long_runs` — one lock acquisition, not four — and returns
`subscriptions` and `work` (`pending − subscriptions`) beside the three existing
keys. `_liveness_pump` wraps its two emissions (stdout `frames` and
`_broadcast_lanes`) in `if busy_frame["work"] > 0`. Three deliberate
non-changes:

- **`ping` is outside the guard.** The `ping` branch of `_handle_message` calls
  `_busy_frame()` and always emits it. A supervisor that ASKS gets all five
  counts, including the subscriptions it is holding itself; an answer that hid
  them would make "attached" and "not attached" identical on the wire.
- **`_report_quiet_requests(pending)` still gets the FULL pending list**, and
  still runs on every lap, including a subscription-only one. It does its own
  stream exclusion, and a lap with `work == 0` can still be the lap on which
  some *other* argv request crosses the silence budget. Moving that call inside
  the guard would have re-introduced DEFECT A for exactly the case DEFECT A is
  about.
- **`pending` keeps its meaning.** Redefining it as `work` would have been the
  smaller diff and the larger break: the launcher reads it by name today.

One behavioural footnote, stated because it is a real change and not a
side-effect anybody would look for: the pump's `except Exception: return`
("writer gone") now only runs on a lap that actually emits. A
subscription-only service whose stdout writer has died therefore keeps its pump
thread alive instead of returning early. It is a daemon thread on a process
whose main loop is already on its way down, so this costs nothing, and a second
guard to preserve the old exit would have been machinery in service of a
thread's tidiness.

## 4. The frame, for the launcher's fixture

`{"op":"ping"}` on an idle serve with nothing attached now answers:

    {"event":"busy","chat_turns":0,"long_runs":0,"pending":0,
     "subscriptions":0,"work":0}

`EterniaLauncher/tool/hermes_serve_frames/generate.py` captures that frame from
a real spawned child (its `busy` case pings an idle serve) and
`test/fixtures/hermes_serve_frames/busy.json` pins the body with sorted keys, so
the fixture REGENERATES rather than being hand-edited — and `MANIFEST.sha256`
with it. Not touched from this side: it is the launcher's gate and the
launcher's regeneration.

## 5. Gates

| Gate | Result |
|---|---|
| `scripts/run_tests.sh` on the six serve modules (`test_serve_request_silence`, `test_harness_serve`, `test_serve_drain_accounting`, `test_serve_socket_lane`, `test_serve_service_mode`, `test_serve_stream_lane_parity`) | **150 passed, 0 failed** in 22.4 s |
| `scripts/run_tests.sh tests/agent_runtime tests/hermes_cli` | **12,482 passed, 4 failed** (1,030 files, 1,798.7 s) — all four pre-existing on main, named below |
| `ruff check` 0.15.10 (the version pinned in `pyproject.toml`'s dev extra) on `hermes_cli/harness_parts/serve.py` and the test module | **All checks passed** |

**The four reds are not this stage's, and one of them is this PLAN's.** None of
the four touch `serve.py`'s busy frame; the branch's whole diff is `serve.py`,
`tests/agent_runtime/test_serve_request_silence.py` and
`docs/agent-runtime-harness/03-transport-and-wire.md`.

1. `tests/hermes_cli/test_cli_contract_dump.py::test_the_committed_dump_matches_the_live_parsers` —
   **owed by L-h.** `harness.py:1767` declares `serve --service` (landed
   `df4865679a`, the previous stage of this same plan) and
   `tests/fixtures/hermes_cli_contract.json` was never regenerated:
   `git show origin/main:tests/fixtures/hermes_cli_contract.json | grep -c -- '"--service"'` → `0`.
   The fix is `python scripts/dump_cli_contract.py --write` **plus** the
   launcher's mirrored fixture and the note in
   `EterniaLauncher/tool/hermes_cli_contract/README.md`, per the failure's own
   instructions — a two-repo move, deliberately not made silently here.
2. `tests/agent_runtime/test_duplicate_helper_bodies.py` —
   `agent_runtime/realm_sync.py::_ledger_time == agent_runtime/store.py::_stamp`,
   from the realm-sync canvas lane. Neither file is on this branch.
3. `tests/agent_runtime/test_no_midtest_monkeypatch_undo.py` —
   `tests/scripts/test_changed_line_mutation_check.py:527` calls
   `monkeypatched.undo()`. Not on this branch.
4. `tests/hermes_cli/test_harness_json_root_observability.py::test_ledger_does_not_rot` —
   the ledger entry `_cmd_persona_instance_open_chat` no longer emits JSON.
   Not on this branch.

Three files were FLAKY under 8-way contention and passed on retry
(`test_serve_stream_lane_parity`, `test_serve_rpc_office_subscribe_live_hub`,
`test_active_sessions`), and a fourth, `test_stream_stale_first_routing`,
timed out inside its whole-tree `ast.parse` walk on both attempts and then
passed alone: `8 passed in 32.4 s`. Contention, not content.

`ruff` is not installed in this box's interpreter; it was run through
`uvx ruff@0.15.10`, which first failed twice to install with `os error 32`
("the process cannot access the file… being used by another process") while
cleaning its own cache. Pointing `UV_CACHE_DIR` at a scratch directory fixed it.
That is the same Defender-exclusion row the suite-perf program already owes the
operator, showing up in a new place.

## 6. Open item — the pump's silence is not the launcher's silence

The pump going quiet on an idle service is the point of RL-13, and it is also a
change in what an attached socket client READS: previously a `busy` frame every
5 s, now nothing at all while idle. That is correct for a watchdog keyed on
"working vs gone" only if the launcher's stream watchdog does not itself key on
the pump. It does not — `childSilenceCeiling`'s own comment prices the pump at
"~18 times inside this window" for a child *running our chat turn*, which is
still true, and the DEFECT-B fix was about a BUSY serve starving the stream
generator, which an idle serve is not. But the sentence is written down here
because it is the assumption this stage rests on, and the proof is the
launcher-side read the operator already has queued: attached and idle should now
print nothing, and print `work: 1` the moment a turn starts.

---

# L-h-c — the runtime says why it ended (RL-16, 2026-09-05)

Third hermes stage on the same plan (`local-runtime-ownership-and-retry-safety.md`
§8.8b, ruling RL-16, hermes half). Worktree `wt/l-serve-service`, branch
`feat/lh-ended-sidecar`, from main `28e502e286`. Two files of product code
(`agent_runtime/serve_registry.py`, `hermes_cli/harness_parts/serve.py`), two new
test modules, one existing test module corrected, three canon docs. Nothing
touched the operator's live store or the live venv: every real serve this stage
spawned ran under a temp root with `HERMES_AGENT_RUNTIME_ROOT`, `HERMES_HOME`,
`HERMES_HEAD_HOME`, `HOME`, `USERPROFILE`, `LOCALAPPDATA` and `APPDATA` all
pinned inside `tmp_path`.

## 1. What was measured, before anything was changed

Read at pickup, at `28e502e286`:

| Claim | Verdict |
|---|---|
| every reader of `serve_instances/` funnels through `serve_registry.list_serve_instances` | true for PRODUCT code — `serve_socket.resolve_socket_target`, `runtime_commands._attach_runtime_service_blocks` and `prune_stale_serve_instances` all call it; `gateway_peers` only names the directory in prose |
| …and nowhere else scans that directory | **false in tests.** `tests/agent_runtime/test_serve_socket_child_e2e.py` globs `serve_instances/*.json` in two places to mean "the registry row is gone" |
| `list_serve_instances` globs `*.json` | true — so `<pid>.ended.json` would have matched it |
| an ACL-safe atomic store writer already exists for this directory | `agent_runtime/serde.write_json_atomic`, whose docstring already argues why it must not be folded into upstream's `atomic_json_write`: this one pins `newline="\n"`, and every record in this directory is read back and compared as LF-canonical bytes |
| the drain's exit runs `atexit` hooks | **no.** `_finish_drain` ends in `hard_exit`, which `_cmd_serve` wires to `os._exit`; the timeout path always takes it |
| `_install_service_stop_signal` is the only thing that touches signal disposition | true — and it REPLACES whatever is installed, for the duration of the `--service` park, then restores it |
| a serve child's pid is the pid of the process you spawned | **false on Windows.** `sys.executable` inside a venv is a redirector that launches the base interpreter as its own child: `Popen.pid` names the redirector, the runtime is one below it. The same two-process shape §8.8c measured on the live chain — and it made the first e2e run red on every pid assertion until they were re-keyed to the `ready` frame's own `pid` |

## 2. The red, measured

Both new modules were written first and run against `origin/main`'s code:

| Test | Expected | Actual before the fix |
|---|---|---|
| `test_serve_ended_sidecar.py` (29 tests) | 29 pass | `ImportError: cannot import name 'SERVE_ENDED_RETENTION' from 'agent_runtime.serve_registry'` — collection error, 0 collected |
| `test_serve_ended_sidecar_child_e2e.py::test_a_drain_says_drained` | a `drained` sidecar for the runtime's pid | `ImportError: cannot import name 'read_serve_ended'` — after the child had booted, drained and exited 0, so the boot half was never the missing part |

A second red arrived after the writer landed and before the reader tests were
satisfied, and it is the one worth keeping:
`test_serve_socket_child_e2e.py::test_without_service_a_child_still_ends_when_its_stdin_closes`
asserted `_registry_rows(env) == []` and got
`[WindowsPath('…/serve_instances/38944.ended.json')]`, plus three siblings. A
helper that meant *the row is gone* had been reading *the directory is empty* —
and the new file exists precisely to survive the row's removal. Both call sites
now filter `SERVE_ENDED_SUFFIX` and say why in place.

## 3. What changed

**`agent_runtime/serve_registry.py`.** `SERVE_ENDED_SUFFIX = ".ended.json"`,
`SERVE_ENDED_RETENTION = 20`, and five functions: `serve_ended_path`,
`write_serve_ended`, `read_serve_ended`, `list_serve_ended` (newest first, by the
record's own `at` then filename — never mtime, which in a copied or restored
store says when it was moved) and `prune_serve_ended`. One line of behaviour
change in existing code: `list_serve_instances` filters the suffix out of its
glob. The record, exactly as written by a real drained child:

```json
{
  "reason": "drained",
  "at": "2026-09-06T01:26:35.245Z",
  "boot_id": "1cb3ba7986944cff9968e67f7124bb67",
  "pid": 39584
}
```

Four keys, no `schema_version`, LF-canonical — and the registry row for that pid
is gone, so the directory holds the sidecar alone.

**`hermes_cli/harness_parts/serve.py`.** `_ServeEndReason` (latch first-wins,
write once, never raises), `CONSOLE_CTRL_END_REASONS`, `SIGNAL_END_REASONS`,
`END_REASON_VOCABULARY`, `_console_ctrl_reason_callback` +
`_install_console_ctrl_reason_handler`, `_install_signal_reason_handlers`,
`_maybe_inject_boot_fault`, and a `record_end_reason` kwarg wired ON only from
`_cmd_serve`. The recorder is armed immediately after the registry row and its
prune — it writes into the directory that row just created, and from `ready`
onward the runtime can be killed. Reasons latch at five sites: `_finish_drain`,
the `drain_abandoned` branch, the shutdown/EOF path, `except KeyboardInterrupt`
and `except Exception`. `_install_service_stop_signal` gained a `note=` callback
for the same reason it exists at all: it owns the SIGTERM disposition while the
park holds it, so without this a signalled service would have recorded the
ordinary shutdown word.

Three choices inside that are not obvious:

* **The console handler returns FALSE for every event, including the ones it
  recorded.** TRUE would mean this process had taken responsibility for the
  event: Ctrl-C would stop raising `KeyboardInterrupt`, a close would stop
  closing. The handler's only job is to leave a record inside the few seconds
  Windows allows before it kills the process anyway.
* **The signal handlers re-raise.** Record, `signal.signal(sig, SIG_DFL)`,
  `os.kill(self, sig)`. A handler that merely latched would silently make a
  serve immune to `kill` — a lifetime change nobody asked for.
* **The ctypes callback is held at module scope.** Windows keeps only the
  function pointer, so a garbage-collected trampoline is an access violation the
  next time the operator presses Ctrl-C.

## 4. Mutation record

| Test | Mutation | Result |
|---|---|---|
| `the_registry_lister_ignores_the_sidecar` | restore the bare `glob("*.json")` | red — two rows, the second a pid-less ghost |
| `removing_the_registry_row_leaves_the_sidecar` | name the sidecar `<pid>.json` | red — `unregister_serve_instance` deletes the reason it was written to preserve |
| `socket_target_resolution_ignores_the_sidecar` | same restore, on the lane `serve connect` and `local_serve_attach` use | red — `allow_stale=True` surfaces a portless ghost |
| `retention_keeps_the_newest_twenty_sidecars` | drop `prune_serve_ended` | red — all 25 survive |
| `a_boot_prunes_the_sidecar_directory_to_the_newest_twenty` | drop the prune from serve boot | red — the 25 planted records survive a real boot |
| `the_console_handler_writes_the_word_and_declines_to_handle` | return TRUE from the handler | red — and live, Ctrl-C silently stops working |
| `the_serve_entry_point_turns_the_recorder_on` | drop `record_end_reason=True` from `_cmd_serve` | red — every reason ships dead with the whole unit suite still green (the `test_root_anchor` precedent) |
| `a_drain_says_drained` | write the record from `atexit` only | red — `os._exit` runs no hook, and the drain is the launcher's restart verb |
| `a_hard_exit_writes_nothing_and_the_absence_is_the_reading` | write `unknown_exit` anywhere earlier | red — the absence that means *hard kill* is erased |
| `the_handler_installs_on_a_runtime_with_no_console_at_all` | let the install raise instead of report | red — the child never reaches `ready` |
| `an_unknown_word_is_refused_rather_than_written` | pass the caller's string through | red — an unvetted word reaches the operator's sheet |
| `the_boot_fault_seam_is_inert_without_its_environment_variable` | fire the seam unconditionally | red — and the production serve dies at boot |

## 5. Deviations from RL-16, and why

1. **A ninth word, `stdin_eof`.** RL-16's vocabulary has `shutdown_op` for "the
   `{"op":"shutdown"}` path". In this loop that path is shared with stdin EOF on
   a non-service serve — same finalization, same `shutdown` frame — but they are
   two different facts, and service mode exists *because* they are (an ORDER vs
   an OBSERVATION, canon `04`). Collapsing them would report an order nobody
   gave; leaving EOF to the `atexit` fallback would report `unknown_exit` for the
   most ordinary exit there is. The word is safe for the launcher's reader by
   construction: under `--service` EOF parks instead of exiting, so a service
   runtime can never write it.
2. **`CTRL_BREAK_EVENT` shares `ctrl_c`'s word.** Both are "the operator
   interrupted it from the console". Break is also the only one of the five that
   `GenerateConsoleCtrlEvent` can aim at a single process group — Ctrl-C with a
   non-zero group id succeeds and delivers nothing, and group 0 would have
   Ctrl-C'd the test runner — so it is what makes the handler provable at all.
3. **`SIGHUP` maps to `logoff`** rather than taking a sixth word. A hangup is the
   session going away, which is what `logoff` already means on the other
   platform; one vocabulary that reads the same on both is worth more than a word
   that can only ever appear on one.
4. **All three drain outcomes write `drained`** — complete, timed out, abandoned.
   The sidecar answers *why did this runtime end*, and the answer is "somebody
   drained it". HOW the drain went is already on the wire in the terminal frame,
   with the counters that make it meaningful; a sidecar saying `drain_timeout`
   would be a second, poorer copy of that.
5. **The recorder is armed by an injected kwarg rather than unconditionally.**
   RL-16 says "every serve unless you find a reason not to", and every serve does
   get it — `_cmd_serve` is the only thing that runs one. The gate exists against
   `serve_loop`'s in-process unit tests, where arming it would register an
   `atexit` hook and take over pytest's own signal disposition. Same contract,
   same reason, as `root_anchor`, `skill_install` and `hard_exit` beside it.
6. **A new test seam, `HERMES_SERVE_BOOT_FAULT`.** There was no existing fault
   seam to reuse. It is one line on the production path, read once after the
   `ready` frame, inert without the variable (pinned by its own test), and it
   buys the three endings a test cannot otherwise ask a real child for: an
   uncaught exception, a plain `SystemExit`, and an `os._exit` that must write
   nothing.

Not a deviation but worth stating plainly: **nothing on the wire moved.** No
frame, no op, no `rpc`/`ops` manifest key, no argparse flag. The CLI contract
fixture and the launcher's serve-frame fixtures are untouched, and the launcher
reads the FILE beside the stale row it has already found.

## 6. Gates

The two new modules, then the six serve modules plus every module touched, then
the whole of `tests/agent_runtime` + `tests/hermes_cli`:

```
=== Summary: 1 files, 29 tests passed, 0 failed (100% complete) in 4.7s (8 workers) ===
=== Summary: 1 files, 9 tests passed, 0 failed (100% complete) in 37.8s (8 workers) ===
=== Summary: 11 files, 262 tests passed, 0 failed (100% complete) in 79.5s (8 workers) ===
=== Summary: 1 files, 7 tests passed, 0 failed (100% complete) in 56.7s (8 workers) ===
=== Summary: 1033 files, 12543 tests passed, 14 failed (100% complete) in 2500.2s (8 workers) ===
```

`uvx ruff@0.15.10 check` clean on every changed file (with `UV_CACHE_DIR` pointed
at a scratch directory — the same `os error 32` cache collision the L-h-b notes
name, which is the Defender-exclusion row this program already owes).

**Nine tests failed across eight files. None are this stage's.** Four are the
reds already named in the L-h-b section above, unchanged:
`test_duplicate_helper_bodies`, `test_no_midtest_monkeypatch_undo`,
`test_cli_contract_dump` (still this plan's own two-repo `--service` fixture
debt) and `test_harness_json_root_observability::test_ledger_does_not_rot`. A
fifth was new on main and not on this branch:
`test_serve_stream_lane_parity::test_the_advertisement_grew_and_no_contract_integer_moved`
went red at `7ea3ac94ea`, when `serve_rpc.manifest()` grew a `params` key the pin
had not learned — `git diff origin/main --name-only` does not contain
`serve_rpc.py`. Main fixed it at `c3f339df1a` while this stage was running; the
branch was replayed onto it and the whole serve set re-run there:
`15 files, 326 tests passed, 0 failed (100% complete) in 91.6s (8 workers)`,
covering the six serve modules, both new modules and every module touched.

The remaining four are 8-way contention, and were re-run together in isolation:
`6 files, 141 tests passed, 0 failed`.

| File | Shape under contention | Alone |
|---|---|---|
| `test_serve_rpc_office_subscribe_live_hub` | 10 × `timed out after 5.0s waiting for: a patch on the re-joined lane` | flaky — failed once, passed on retry, both in the gate and in isolation; already named as flaky in the L-h-b section |
| `test_stream_stale_first_routing` | the 30 s cap expired inside its whole-tree `ast.parse` walk | green; named in L-h-b for the same reason |
| `test_harness_cli` | the 30 s cap expired inside `_cmd_verify`'s `subprocess.run` | green |
| `test_kanban_boards` | the 30 s cap expired inside `boards create`'s `subprocess.run` | green |

Two further files were flaky WITHIN the gate and passed on their own retry
(`test_serve_stream_hub`, `test_session_recovery`).

One flake worth naming because it is on a path this stage touched and is
therefore the one that had to be ruled out:
`test_serve_drain_accounting::test_a_drain_the_reader_outran_is_declared_abandoned_in_a_frame`
failed once in the 11-file batch and passed on retry. That test waits out
`_DRAIN_ABANDON_GRACE_SECONDS = 5.0`. Run alone three times: `9 tests passed`
each time. The stage's addition to that branch is `_note_end` + `_write_end`,
both of which are no-ops in a `serve_loop` unit test (`record_end_reason`
defaults False, so the recorder is `None`), so it cannot be the cause — but the
check was made rather than assumed.

## 7. What is NOT proven

* **`ctrl_close`.** No Windows API generates `CTRL_CLOSE_EVENT` — it is what the
  OS sends when a console window's X is clicked, and nothing else sends it. The
  mapping is pinned as a table
  (`test_the_console_control_mapping_table_is_the_closed_vocabulary`), and the
  handler's installation and firing are proven by the `ctrl_c` arm through the
  identical callback. The end-to-end claim — *close the black `cmd.exe` window,
  get `ctrl_close`* — is an operator gesture and stays owed. RL-17 will make it
  nearly unreachable anyway (`CREATE_NO_WINDOW`: no window to close), which is
  why the no-console install arm sits beside it.
* **`logoff`.** Same reason, one step worse: producing it means logging out of
  Windows or shutting the machine down.
* **`sigterm`.** The arm exists and is skip-marked on Windows with its reason.
  It has never run — this is a Windows box — so the POSIX half of RL-16 is code
  review plus a table, not a measurement.
* **A real hard kill.** `os._exit` is the in-process stand-in for
  `TerminateProcess`; a genuine `taskkill /F` against a real runtime was not
  performed, because the arms that would need it are the operator's own machine.
  What IS proven is that nothing in this runtime writes a placeholder on that
  path, which is the property `ended=absent` rests on.
* **The launcher half.** `ended=absent`, the `serve_attach outcome=staleOwner
  ended=<reason>` receipt and the runtime sheet's *"the last runtime ended: …"*
  line are L-l-d's. Until they land, the sidecar is written and read by nobody.
* **The venv redirector's own death.** Every e2e here waits on the redirector
  (`Popen.pid`) and asserts on the runtime (`ready["pid"]`). A kill that reached
  only the redirector would leave the runtime up and write no sidecar —
  correctly, but that arrangement was not exercised.

---

## Q-h — the parser owns its failure; one live device row (RL-24, RL-23)

Queue follow-ups from the launcher plan's §8.10 (verdicts 3, 4 and 10), built in
worktree `wt/c1-open-chat`, branch `feat/queue-hermes-followups`, from main
`f7b89826eb`. Two rulings, one commit each. Nothing here touched the operator's
live store or the live venv: RL-24 is held at the `serve_loop` seam with injected
streams and a monkeypatched parser factory, and every RL-23 test hands the store
functions a `tmp_path` root, which is what those functions take as an argument.

### Q-h.1 Revalidation, before anything was changed

| §8.10 claim | Verdict at `f7b89826eb` |
|---|---|
| `dispatch_argv` builds the HARNESS parser only | present (`_build_harness_parser` → `hermes_cli.harness.build_parser`) |
| `parser.parse_args(argv)` and `func(args)` share one `SystemExit` path | present — `dispatch_argv` re-raised the handler's `SystemExit` and the loop's one `except SystemExit` arm framed it `argv_parse_failed` |
| a non-`harness` root is an argparse rejection | present — reproduced as the third red below |
| `hermes profile delete` exists as a CLI verb | present (`hermes_cli/subcommands/profile.py`) — so the lane, not the verb, is what was refusing |
| the redeem writes a row and revokes nothing | present (`serve_gateway_auth.redeem_pairing_code`) |
| no shipped handler calls `sys.exit()` | true today — grep over `hermes_cli/harness_parts/` and `hermes_cli/harness.py` finds none, which is why the defect was latent and not reported |

### Q-h.2 RL-24 — the red, measured

Three tests in `tests/agent_runtime/test_harness_serve.py`, all through the real
`dispatch_argv` and the real `serve_loop`:

```
FAILED test_a_handler_that_exits_is_handler_exit_and_not_a_parse_failure
FAILED test_a_non_harness_root_is_refused_before_any_parser_is_built
  AssertionError: assert ['argv_parse_failed'] == ['argv_root_unsupported']
2 failed, 1 passed, 32 deselected in 13.61s
```

The one that passed is `test_real_dispatch_argv_parse_failure` — the frame whose
bytes must NOT change, run in the same command as the two reds so that "the new
words landed" and "the old word is untouched" are one measurement.

### Q-h.3 RL-24 — what changed

`dispatch_argv` now has three exits and each raises its own type:

* `ArgvRootUnsupported(root)` before a parser is built, when `argv[0] != "harness"`;
* a bare `SystemExit` out of either `parse_args` call — the parser's own refusal,
  and the ONLY one the loop still calls `argv_parse_failed`;
* `HandlerExit(code)`, converted from a `SystemExit` escaping `func(args)`.

Both new types subclass `SystemExit` deliberately: `hermes_cli.main` and the test
harness CLI catch that, and neither should change process behaviour because the
serve lane learned to tell two exits apart. The request loop's `except` arms are
ordered new-types-first for the same reason. `_system_exit_code` is the loop's
old `None → 0, non-int → 2` normalisation, moved out so both call sites share it.

The three frames, verbatim from the run:

```json
{"id":"bad","event":"error","error":"argv_parse_failed","detail":"argparse rejected the request argv; usage was forwarded as stderr frames"}
{"id":"h","event":"error","error":"handler_exit","code":3,"detail":"the request handler exited; any effect it had already happened and must not be replayed"}
{"id":"p","event":"error","error":"argv_root_unsupported","root":"profile","detail":"the serve argv lane owns the 'harness' parser only; this root is a CLI verb the caller runs itself"}
```

Each is followed by the request's own `exit` frame with the same code (2, 3, 2),
which is what makes the two new words additive: the launcher's error router
ignores kinds it does not know, and the exit frame settles the request exactly as
it does today. `root` is cleaned before it is framed (printable, one line, 16
chars) — the value comes off the wire, and a refusal that echoes an unbounded
caller string is a write amplifier.

### Q-h.4 RL-24 — mutation record

* Restore the old shared path (delete the `except SystemExit → HandlerExit`
  conversion in `dispatch_argv`): the handler test goes red with
  `assert ['argv_parse_failed'] == ['handler_exit']` — i.e. the effect landed and
  the frame invited the launcher to replay it. Restored.
* Delete the root guard: the third test goes red with
  `assert ['argv_parse_failed'] == ['argv_root_unsupported']` — the same red
  as before the fix, i.e. the harness parser is reached and rejects a verb it
  does not own. Restored.

### Q-h.5 RL-23 — the red, measured

Six tests in `tests/agent_runtime/test_serve_gateway_auth.py` plus one boot
wiring test in `tests/agent_runtime/test_serve_service_mode.py`. Run against the
product file as it stood at `2b60814566` (the RL-24 commit), i.e. with no part of
RL-23 in it:

```
FAILED test_a_second_redeem_for_one_account_device_leaves_exactly_one_live_row
  AssertionError: assert ['dev_9c7ba12...417d17ae954e'] == ['dev_59e8417d17ae954e']
FAILED test_the_superseded_credential_is_refused_with_the_honest_word
  AssertionError: assert 'ok' == 'device_revoked'
FAILED test_the_boot_prune_deletes_only_revoked_rows_older_than_thirty_days
  ImportError: cannot import name 'REVOKED_ROW_RETENTION_SECONDS'
FAILED test_the_prune_never_touches_a_live_row_or_an_unreadable_stamp
  ImportError: cannot import name 'prune_revoked_devices'
4 failed, 37 passed in 14.39s
```

The first line IS the defect, in the currency the queue row reported it in: two
live rows for one account device, and the second is *"the old credential still
authenticates"* — the reason five stale rows were not merely untidy.

Two of the six were green before the change and are meant to be: the
different-account test and the unlabelled-row test pin what must NOT move, and a
supersession scoped by anything other than the label would have turned them red.

### Q-h.6 RL-23 — what changed

* `redeem_pairing_code` revokes prior non-revoked rows carrying the same
  `account_device_id` in the SAME store write as the new row, stamping
  `revoked_reason: "superseded"` and `superseded_by: <new device_id>`. Skipped
  entirely when the label is absent.
* `DeviceRecord` learns both fields (absent reads as `None` — the migration story
  `expires_at` already has), and `payload()` carries them to the operator
  surfaces.
* `prune_revoked_devices(store_root, *, retention_seconds, now)` deletes revoked
  rows past `REVOKED_ROW_RETENTION_SECONDS` (30 days), reusing `stamp_passed`
  against a shifted cutoff so there is ONE stamp reader in this store. It never
  raises, never touches a live row, and keeps a row whose `revoked_at` will not
  parse.
* `serve_loop`'s boot calls it beside the two `serve_instances` prunes, gated on
  `store_root_path is not None` and NOT on `record_end_reason`: the device store
  belongs to the root, not to service mode.

`note_device_seen`, `verify_device_proof` and `revoke_device` are untouched. The
row pair after a second redeem, from a throwaway root (verifiers elided here,
never written by the test):

```json
"dev_f4d213de52568f52": {
  "created_at": "…T23:53:21+00:00", "revoked": true,
  "revoked_at": "…T23:53:21+00:00", "account_device_id": "ad3b6525",
  "revoked_reason": "superseded", "superseded_by": "dev_3c62b5f865a2f530"
},
"dev_3c62b5f865a2f530": {
  "created_at": "…T23:53:21+00:00", "revoked": false, "revoked_at": null,
  "account_device_id": "ad3b6525"
}
```

The revoked row's `revoked_at` equals its successor's `created_at` — the same
write, which is the property that stops any reader from seeing two live rows.

### Q-h.7 RL-23 — mutation record

* Product file reverted to its pre-RL-23 state: the four reds above. Restored.
* Boot prune call replaced with `pass`: the wiring test fails with
  `assert ['dev_2a3391c…', 'dev_bfa9bb6e7411fd6d'] == ['dev_bfa9bb6e7411fd6d']`
  — the ancient revoked row survives the boot. Restored.

### Q-h.8 Deviations, and what is NOT proven

* **`handler_exit` is unreachable in production today.** No shipped handler calls
  `sys.exit()` (grep, recorded above), so the frame is proven by a parser
  injected in the test and not by a hermes verb. That is the point of landing it
  now: the launcher's fallback stops depending on a property of the handler set.
* **No launcher fixture was captured.** `tool/hermes_serve_frames/generate.py`
  captures `error_argv_parse_failed` from a real child and that frame is
  byte-unchanged, so `--check` stays green. The two new words have no fixture
  and no launcher decode yet; both are Q-l's, and the frames are pinned on this
  side by `test_harness_serve.py` in the meantime.
* **The supersession was never run against a real pairing over the wire.** Every
  RL-23 test drives the store functions directly, which is what those functions
  take a root for; the gateway handshake path is unchanged and is proven by its
  own existing tests, not re-proven here.
* **The five live rows on the operator's machine are still there.** Nothing in
  this stage rewrites an existing store: supersession applies from the next
  redeem, and the prune only removes rows something revoked. Collapsing the
  existing five is an operator action (re-pair, or `harness gateway revoke`).
