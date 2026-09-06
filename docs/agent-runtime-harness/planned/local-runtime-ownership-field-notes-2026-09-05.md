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
