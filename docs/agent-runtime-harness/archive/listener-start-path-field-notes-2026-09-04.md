# L1 — the listener start path: field notes (2026-09-04)

Stage L1 of `reachable-switch-lifecycle.md` (launcher repo,
`docs/mission_control/planned/`). Base `fe742b60c2`, branch
`feat/l1-listener-start-path`, worktree `X:\wt\l1-listener`. Rulings consumed:
**R-L1** (the greeting is never silent about the gateway) and **R-L2** (a dead
socket owner yields). Nothing pushed.

The lane's headline, and it is not the one the plan's §0 expected: **the two
defects L1 owns are one mechanism seen twice.** The socket lane and the LAN
listener are coupled by design — the serve that owns a root's socket is the
serve that opens that root's doors — so R-L2's stale sidecar is *why* R-L1's
block said `disabled`. Fixing the lock alone would have closed the operator's
window and left the next one unreadable; fixing the block alone would have
described a failure nobody had to have. Both landed, in that order, and the
e2e asserts them together on one boot.

---

## Step (a) — the shared liveness helper + `SocketOwnerLock` takeover

Commit `0fe67683f3` — *feat(serve_socket): a DEAD socket owner yields the lane,
with a receipt (R-L2)*.

**Measured first, read-only, from the operator's own store** (never written,
never served against):

```
X:\Eternia\.hermes\agent-runtime\serve_socket.owner.json
  {"pid": 25672, "boot_id": "64e5de2e…", "host": "127.0.0.1", "port": 60045,
   "started_at": "2026-09-04T10:28:16.817Z", …}
serve_instances\25672.json   transport "stdio+socket", port 60045
serve_instances\31968.json   transport "stdio",        port null, socket_started_at null
```

That is the plan's §0 row at 10:29:41 with the file still on disk: the sidecar
names 25672, the successor 31968 booted stdio-only. `owner_started_at` is that
sidecar's `started_at`, which is why the block carries it under that name.

**Changed:**

- `serve_registry._pid_alive` → **public `pid_alive`**, with the reason for the
  rename in its docstring. It is the probe behind `stale_dead_pid`; the lock
  imports it lazily rather than copying it. This is the "factor one function
  both call" the stage asked for, and it is asserted rather than assumed — a
  test patches `serve_registry.pid_alive` and requires the LOCK's verdict to
  change. If this module ever grows its own probe, that patch lands on nothing
  and the test reddens.
- `SocketLockResult` gains `owner_started_at`, `took_over_from` (both on
  `payload()`, absent when unknown) and `owner_state` (diagnostic; deliberately
  NOT on the wire — the wire gets outcomes, debugging gets words).
- `SocketOwnerLock(store_root, *, log=None)`; `serve.py` passes `_service_log`,
  so a takeover lands on the same channel as `serve_instances_pruned`,
  correlatable by `boot_id` against that boot's `ready`.
- `_read_owner_record` splits "no file" from "half a file" (`read_socket_owner`
  still collapses both to `{}` — that is right for discovery and wrong for a
  lock that must be able to log what it stepped over).

**The finding that shaped the implementation.** The obvious reading of R-L2 is
"steal the lock from a dead owner". You cannot, and you do not have to: *the OS
lock is not the stale part.* Both `msvcrt.locking` and `flock` are released by
the kernel when the holder dies, so a sidecar naming a corpse cannot describe
the current holder of a lock that is still held. That leaves exactly two shapes:

1. **lock FREE, stale sidecar beside it** — the ordinary crash/kill leftover, and
   the common case. `acquire()` already succeeded here before this stage; what
   was missing was the *receipt*. It now reads the sidecar BEFORE
   `publish_owner` overwrites it (the only moment the previous owner's identity
   is still on disk) and records `took_over_from`.
2. **lock HELD, sidecar names a corpse** — the exit is genuinely in flight. One
   retry, bounded at one: a lock we could not take on the second attempt is held
   by something the sidecar does not describe, and this class does not spin.

So R-L2 is implemented as *prove, then re-attempt*, not as *break*. A LIVE owner
is refused byte-for-byte as before — that refusal is the whole reason the lock
exists.

**Fail-safe direction is the OPPOSITE of the registry's**, and it is stated in
both docstrings so the next reader does not "fix" one to match the other.
`serve_registry` refuses to call an unreadable probe `live`; the lock refuses to
call one *dead*. Publishing `took_over_from` about a process that is still
serving would be a receipt for something that did not happen.

**Red-first / tests** (`tests/agent_runtime/test_serve_socket_lane.py`, 8 new):
dead owner → acquired + `took_over_from` + one `serve_socket_owner_takeover`
line; live foreign owner → `lock_held_by` + `owner_started_at`, nothing taken;
held lock + corpse sidecar → still `lock_held_by`; absent sidecar → acquired,
silent, and the payload is exactly the three keys it always had; corrupt sidecar
→ acquired, one `serve_socket_owner_stale` line naming `sidecar_malformed`, and
`read_socket_owner` still `{}`; the shared-probe test above; the unreadable-probe
test; and the `ready` frame carrying the takeover.

A live foreign pid is needed by two files, so it is a conftest fixture
(`live_foreign_pid`) rather than two spawn helpers that can drift on how a
process is made and reaped. `os.getpid()` cannot be used — it classifies `self`.

**Verify:** `bash scripts/run_tests.sh tests/agent_runtime/test_serve_socket_lane.py`
→ `1 files, 68 tests passed, 0 failed (100% complete) in 18.4s`.

---

## Step (b) — the gateway block on every boot path (R-L1)

Commit `83bd858bd7` — *feat(serve): the gateway block says WHY there is no
listener (R-L1)*.

**The defect, stated exactly.** "No LAN listener" had four causes and one word.
`remote_gateway.listen` off, no runtime root, a socket lane that never came up,
and a bind/certificate failure — the first three all arrived as
`{"outcome": "disabled"}`, the word for *the operator never asked*. On
2026-09-04 the launcher had just asked.

**Changed:** `gateway_block_when_no_listener(socket_block, *, root_resolved)`,
module-level and pure beside `start_gateway_listener` for the same reason that
one is (four outcomes worth reading in one screen; a table-driven test of them
should not boot four runtimes). The `if socket_server is not None` guard grows
an `else` and nothing else moved: the block still rides `ready`, `hello_ok` and
`version` from the same variable.

**The vocabulary a launcher must decode** (`gateway.outcome`):

| outcome | means | carries |
|---|---|---|
| `listening` | the door is open | `host`, `port`, `started_at`, `cert_fingerprint` |
| `disabled` | `remote_gateway.listen` is off | — |
| `socket_unavailable` | **the config asked and the socket lane is why not** | `reason`, `pid`, `owner_started_at`, `host`, `port` |
| `error:<Type>` | bind or certificate failed | `host`, `port` |

`capabilities` (R-IP16) rides all four, unchanged. `reason` is the `socket`
block's own outcome **verbatim** — `disabled` \| `lock_held_by` \|
`error:<token>` — so the two blocks on one frame cannot tell different stories
about one boot, and the launcher decodes one vocabulary instead of two.
`pid` / `owner_started_at` are always present on `socket_unavailable`, null when
unknown: this block IS the explanation, and a reader of an explanation should
not have to tell "the field is missing" from "the field is empty".

**Two decisions the ruling left open, and how they were taken:**

- **Config-off wins over socket-unavailable.** When both are true the block says
  `disabled`. That is the actionable answer (turn it on); `socket_unavailable`
  on a runtime nobody asked to listen would send a launcher chasing a lock for a
  door it was never told to open. Pinned by
  `test_config_off_wins_over_a_socket_lane_that_is_also_down`.
- **`host`/`port` ride `socket_unavailable`** — the door that was ASKED for, the
  way the `error:*` outcomes already carry them. A launcher keys on `outcome`;
  this is the config echoed back, not a door that opened.

**A consequence worth naming before the launcher hits it:**
`socket_unavailable` can only ever ride `ready` and a **stdio** `version` reply.
In that state there is no loopback listener, so there is no `hello_ok` to carry
it. R-L1's "and `hello_ok`" is satisfied for the other three outcomes and is
vacuous for this one — which is precisely why `ready` has to be complete.

**Tests** (`tests/agent_runtime/test_serve_gateway_lane.py`, 5 new): socket lane
off + config on → `socket_unavailable`/`reason: disabled` with the **exact key
set** pinned (the pinned key-set test was extended, never weakened — the
`listening` block's own key-set assertion is untouched); lock lost to a live
owner → `socket_unavailable`/`reason: lock_held_by` naming the pid, and the
`version` reply carrying the identical block; config-off precedence; `hello_ok`
carrying the same block as `ready`; and a table-driven test of the pure helper
including the operator's own boot reconstructed from his sidecar (pid 25672,
`2026-09-04T10:28:16.817Z`).

**Verify:** `bash scripts/run_tests.sh tests/agent_runtime/test_serve_gateway_lane.py tests/agent_runtime/test_serve_socket_lane.py`
→ `2 files, 96 tests passed, 0 failed (100% complete) in 18.1s`.

---

## Step (c) — the two-roots e2e

Commit `209676478f` — *test(serve): two real serves, one killed — the successor
takes the lane and opens the door*
(`tests/agent_runtime/test_serve_socket_child_e2e.py`).

The operator's session reproduced against **two real `harness serve --ndjson`
children and a real kill**, in the file that already carries the
`live_system_guard_bypass` marker and the sandboxed `LOCALAPPDATA`/`HOME`/
`HERMES_HOME` env. This is the one claim the in-process tests cannot make: the
first serve holds a real OS lock, and it is KILLED — no atexit, no `release()`,
no unlink — so the sidecar is a genuine leftover rather than a file a test wrote.

`remote_gateway.listen: 127.0.0.1` / `port: 0` is written into the sandbox
home's `config.yaml` rather than monkeypatched, because the config read is half
of what failed: the launcher's write path was correct and the greeting still
said off.

Asserted on the second boot: `socket.outcome == listening`,
`socket.took_over_from == <first pid>`, `socket.owner_started_at == <the first
boot's own socket.started_at>`, `gateway.outcome == listening` with a host, an
int port and a `cert_fingerprint`, and the sidecar now naming the living owner
so a third boot inherits a truthful file. Before this stage that boot read
`socket: lock_held_by` / `gateway: disabled`.

**Verify:** `bash scripts/run_tests.sh tests/agent_runtime/test_serve_socket_child_e2e.py`
→ `1 files, 3 tests passed, 0 failed (100% complete) in 32.8s`.

---

## Step (d) — regeneration and canon

Commit `f774da1701`.

- **`scripts/dump_cli_contract.py --check`** → `CLI contract fresh: 191 command
  paths, sha256 86837537988fdfcf`, exit 0. No verb text moved, as expected —
  L1 changes frame CONTENT, not the argv surface. Nothing written.
- **Stream goldens: none moved, and that is a checked claim, not an assumption.**
  `tests/fixtures/stream_frames/` holds `hydrate` / `delta` / `patch` /
  `heartbeat` frames only (its README's generated/pinned split names all
  seventeen). There is no `ready` frame in any of them, and no `socket` or
  `gateway` block — grepped. So `MANIFEST.sha256` is untouched and **no launcher
  golden mirror is owed for this stage.**
- **`scripts/doc_cite_adjacency.py --exclude archive --exclude planned`** → 0
  unwaived, 0 stale, exit 0. Getting there took work worth recording: the base
  `fe742b60c2` is clean, and L1's inserts into `serve.py` (+4 in the module
  docstring, +68 for `gateway_block_when_no_listener`, ~+30 spread through
  `serve_loop`) moved **22 cites** off their symbols across four canon docs. They
  were re-anchored mechanically from a `difflib` old→new line map of the base
  file rather than by hand, so none of them is a guess; the bare `` `:N` ``
  continuations were rewritten in the same pass. One waiver key
  (`04-boot-and-lifecycle.md|serve.py:1761`) moved with its cite to
  `serve.py:1832`; its reason text is unchanged, because the code it declines to
  re-anchor is unchanged.
- **Canon.** `03-transport-and-wire.md` gains the R-L2 rule beside the one-owner
  lock and replaces the gateway block's three-outcome sentence with the
  four-outcome table above (plus why `socket_unavailable` had to exist and why it
  can never ride a `hello_ok`). `04-boot-and-lifecycle.md` says both where boot
  stage 3 and stage 6 describe them, and records the lesson the stage cost:
  **present is not the same as informative** — the block was always there and
  always carried an outcome, and three different failures produced the word for
  "nobody asked", so absence had merely moved inside the block.

---

## Step (e) — the suites, and what was classified

Both suites green, **0 failed**, run separately (the runner's per-file
parallelism makes one combined invocation no cheaper): `tests/agent_runtime`
418 files / 7689 tests / 755.7s, `tests/hermes_cli` 598 files / 4555 tests /
1227.0s. See the verify block at the end of this file.

**One flake observed and classified, not carried.** On the first combined run of
`test_serve_gateway_lane.py` + `test_serve_socket_lane.py`,
`test_a_disconnect_unsubscribes_and_does_nothing_else` failed
`assert summary["subscriptions"]["subscribers"] == 0` → `1 == 0`. The runner's
own retry passed it, and two later runs of the same pair passed it. It is a
subscriber-teardown timing race in a test this stage does not touch — no lock,
no owner sidecar, no gateway block anywhere in its path — and the same file's
other 67 tests were green in the same process, and the whole-suite run above hit
it zero times.

**One retry inside the hermes_cli run, and it is the runner's own rule, not a
red.** `test_harness_cli.py` tripped the per-file timeout at 8-worker contention
and the runner re-ran it at 1-worker isolation: `RETRY PASS … (59.0s at 1
worker)`. It is a cold-interpreter cost under load in a file this stage does not
touch.

---

## Deviations from the stage brief

1. **`tests/agent_runtime/test_serve_socket.py` does not exist**; the lock tests
   went to `test_serve_socket_lane.py` (the brief's "or the nearest") and the
   gateway-block tests to `test_serve_gateway_lane.py`. Likewise
   `agent_runtime/serve_instances.py` does not exist — the `stale_dead_pid`
   classification lives in `agent_runtime/serve_registry.py`, and that is where
   the shared probe was made public.
2. **The takeover is implemented as prove-then-reattempt, not as break** (see
   step (a)). R-L2's text — "reads the sidecar; if the pid is not running it
   takes the lock over" — is satisfied, and the mechanism is the only sound one:
   there is no API for stealing an OS lock, and there does not need to be,
   because the kernel has already released it.
3. **`owner_state` is deliberately not on the wire.** R-L2 asks for the takeover
   to be recorded and logged; the sidecar's classification (`absent`,
   `sidecar_malformed`, `pid_running`, `pid_not_running`,
   `liveness_unreadable`, `pid_missing`, `self`) is a debugging word and rides
   the log line and the result object only. Adding a fifth vocabulary to the
   greeting would be a fifth thing to keep true.

---

## OWED at landing

- **Launcher mirrors: NONE for goldens or the stream manifest.** No fixture under
  `tests/fixtures/stream_frames/` moved (checked, see step (d)), so
  `test/fixtures/harness_stream/` and both manifests are untouched by L1. The
  standing S0a/WS1 mirror debts recorded in that README are unchanged and remain
  someone else's.
- **L2 is what consumes this stage.** The launcher must decode the new
  `gateway.outcome` word `socket_unavailable` (with `reason` / `pid` /
  `owner_started_at`) as *runtime present, listener not up* — R-L5's sentence,
  never `unsupported` — and may read `socket.took_over_from` to tell a recovered
  restart from a boot into an empty root. R-L3's bounded wait for the owner
  sidecar to clear is now belt-and-braces rather than load-bearing: hermes closes
  the window on its own side regardless of who spawned the successor.
- **Not pushed.** Branch `feat/l1-listener-start-path` on base `fe742b60c2`.
- **Unrun, and why:** the LAN bind itself (a non-loopback host string), the
  Windows Defender prompt, and a second machine. Every test here binds
  `127.0.0.1` — the LAN bind is this same call with a different config string,
  and what CI cannot exercise is named here rather than faked. That is L3's, on
  the operator's hardware.

---

## Verify (verbatim)

```
$ bash scripts/run_tests.sh tests/agent_runtime/test_serve_socket_lane.py
=== Summary: 1 files, 68 tests passed, 0 failed (100% complete) in 18.4s (8 workers) ===

$ bash scripts/run_tests.sh tests/agent_runtime/test_serve_gateway_lane.py tests/agent_runtime/test_serve_socket_lane.py
=== Summary: 2 files, 96 tests passed, 0 failed (100% complete) in 18.1s (8 workers) ===

$ bash scripts/run_tests.sh tests/agent_runtime/test_serve_socket_child_e2e.py
=== Summary: 1 files, 3 tests passed, 0 failed (100% complete) in 32.8s (8 workers) ===

$ bash scripts/run_tests.sh tests/agent_runtime
=== Summary: 418 files, 7689 tests passed, 0 failed (100% complete) in 755.7s (8 workers) ===

$ bash scripts/run_tests.sh tests/hermes_cli
Retrying 1 timeout-affected file at 1-worker isolation (single bounded retry):
  RETRY tests\hermes_cli\test_harness_cli.py
  RETRY PASS tests\hermes_cli\test_harness_cli.py (59.0s at 1 worker)
=== Summary: 598 files, 4555 tests passed, 0 failed (100% complete) in 1227.0s (8 workers) ===

$ python scripts/dump_cli_contract.py --check
CLI contract fresh: 191 command paths, sha256 86837537988fdfcf

$ python scripts/doc_cite_adjacency.py --exclude archive --exclude planned
  UNWAIVED FAILURES: 0
  STALE WAIVERS (no longer failing - delete the entry): 0
  cite-adjacency probe passed (baseline capped, nothing new, nothing stale).
```
