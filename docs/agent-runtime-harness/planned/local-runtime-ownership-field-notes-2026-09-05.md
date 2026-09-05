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
