# Field notes — the restart drain fence, hermes half (2026-09-07)

The hermes half of the launcher plan `restart-drain-fence.md` (rulings RS-3 and
RS-4, its Stage 2). Written AS THE WORK HAPPENED, so the order of the sections
below is the order the facts arrived, not a tidy retelling. The launcher half of
these notes lives beside the plan in the launcher repo.

Branch `fix/serve-drain-lock-order`, worktree `drain-lock-order`, from
`c670168049`.

---

## 1. What the shutdown order ACTUALLY is today (RS-3, before any edit)

Read, not guessed, in `hermes_cli/harness_parts/serve.py`:

- the drain op handler calls `ServeSocketServer.begin_drain()` on the loopback
  lane and the gateway lane, which closes the LISTENER and emits
  `serve_socket_draining` (`agent_runtime/serve_socket.py`);
- the drain monitor then waits for the in-flight requests;
- `_finish_drain` runs `frames.emit(frame)` → `_broadcast_lanes(frame)` →
  `_close_socket_lane(reason="drain")` → `_unregister_instance()` →
  `_note_end("drained")` → `_write_end()`;
- `_close_socket_lane` swaps the four lane handles under `lane_lock` and then,
  outside it, closes gateway → hub → server → **`lock.release()`**.

**So the order RS-3 asks for is already the order the code runs**: listener
closed → in-flight work drained → socket lock released → register row
unregistered → ended note → exit. The release is the LAST act of
`_close_socket_lane`, and `_unregister_instance` is the statement after it.

That is the answer to the question the plan asked for out loud: the red-first
order test was written first and it was **GREEN on the pre-fix tree** for its
ordering assertions, and RED for the two facts the order alone does not carry —
the `draining_at` sidecar and the `serve_instance_unregistered` line. The reds
are quoted verbatim in §3.

The field's real defect (plan §0, 16:25:23.613) is therefore NOT a wrong order.
It is that the correct order still holds the lock for the whole in-flight wait
— 14 s in the field — while the listener is already closed, and a contender
arriving in that window cannot tell "alive and leaving" from "alive and
serving". That is exactly the hole RS-4 fills, and RS-3's contribution is the
two facts that make the telling possible: a sidecar that says it is leaving and
a line that says when the row went.

---

## 2. What was built

**`agent_runtime/serve_socket.py`**

- `SOCKET_OWNER_DRAINING_KEY = "draining_at"`, and two module constants:
  `SOCKET_LOCK_DRAIN_WAIT_SECONDS = 25.0` (named once, with the launcher's 20 s
  `drainDeadline` cited beside it) and `SOCKET_LOCK_DRAIN_POLL_SECONDS = 0.25`.
- `SocketLockResult.waited_for_drain_ms`, on the object and — when a wait
  happened at all — on `payload()`, so it rides both endings.
- `SocketOwnerLock.__init__` takes `clock` and `sleep`, defaulted to
  `time.monotonic` / `time.sleep`, the same injection `HelloRateLimiter`
  already uses. Production passes neither; a test pins that (§4).
- `SocketOwnerLock._owner_is_leaving` — live pid AND (`draining_at` stamped OR
  no `serve_instances/<pid>.json`). Anything the probes cannot answer is NOT
  leaving.
- `SocketOwnerLock._wait_for_drain` — sleep-then-try, so the first lap is not a
  duplicate of the attempt that got us here; returns `(handle, failure,
  waited_ms)`.
- `SocketOwnerLock.mark_draining()` — REWRITES the published sidecar (the port
  and the boot id survive; the drain must not blank a record clients are still
  discovering by) with `draining_at`.
- A takeover is now either proof: the owner was already dead, or it let go while
  we waited. Same `took_over_from` word, because the launcher's question is the
  same one.

**`hermes_cli/harness_parts/serve.py`**

- The drain op calls `socket_lock.mark_draining()` between `frames.emit` and the
  `begin_drain()` loop — before the listener closes, which is the whole point.
- `_unregister_instance(reason=…)` emits one `serve_instance_unregistered` line
  on the service log when the row actually goes, and the pre-existing
  `serve_instance_unregister_failed` line grew the same `reason`. The three call
  sites say `drain`, `drain_abandoned`, `shutdown`.

**Canon**: the `serve_socket.py` module docstring's "does not fail and does not
retry" sentence now says when it DOES retry; the `SocketOwnerLock` class
docstring grew a third owner shape ("A LEAVING owner is worth waiting for");
`docs/agent-runtime-harness/03-transport-and-wire.md`'s socket-ownership
paragraph gained the draining sidecar, the new line, and the bounded wait.

---

## 3. The reds, quoted

### 3.1 RS-3, the order — GREEN before the fix, and that is the finding

`tests/hermes_cli/test_harness_serve_drain_order.py::test_drain_releases_the_lock_before_it_drops_the_row`
passed on the first run against the pre-fix tree. The recorder's list, printed
by pytest in the sibling failures' fixture repr, is the receipt:

```
drain_recorder = {..., 'steps': ['listener_closed', 'lock_released', 'row_unregistered', 'ended_note']}
```

Two of the three tests in that file were red, and both are RS-3's *other* half
— the facts a contender reads from outside the process:

```
>       assert isinstance(sidecar.get("draining_at"), str)
E       AssertionError: assert False
E        +  where False = isinstance(None, str)
E        +    where None = <built-in method get of dict object at 0x…>('draining_at')
E        +      where <…>.get = {'boot_id': '2cce4c9…', 'host': '127.0.0.1', 'pid': 25420, 'port': 49695, ...}.get
```

```
>       assert len(rows) == 1, [r.get("event") for r in run["sink"].service_log()]
E       AssertionError: ['serve_socket_accept_loop_exit', 'serve_socket_draining']
E       assert 0 == 1
E        +  where 0 = len([])
```

The second red's message is the whole point in one line: the ONLY structured
events a whole drain put on the service log were the accept loop's exit and the
drain announcement. Nothing marked the moment the register row went.

### 3.2 RS-4, the bounded wait

`tests/agent_runtime/test_serve_socket_drain_wait.py` did not even import:

```
tests\agent_runtime\test_serve_socket_drain_wait.py:37: in <module>
    from agent_runtime.serve_socket import (
E   ImportError: cannot import name 'SOCKET_LOCK_DRAIN_POLL_SECONDS' from 'agent_runtime.serve_socket' (X:\…\agent_runtime\serve_socket.py)
```

An import error is a weak red — it says the constant is missing, not that the
behaviour is. So the two constants, the result field and the injected
clock/sleep landed first, WITHOUT the wait, and the red was taken again:

```
>       assert result.acquired is True
E       AssertionError: assert False is True
E        +  where False = SocketLockResult(outcome='lock_held_by', pid=34888, path='…\serve_socket.lock',
E                          owner_started_at='2026-09-07T16:25:09.771Z', took_over_from=None,
E                          waited_for_drain_ms=None, owner_state='pid_running').acquired
```

```
>       assert result.waited_for_drain_ms == int(SOCKET_LOCK_DRAIN_WAIT_SECONDS * 1000)
E       AssertionError: assert None == 25000
```

`owner_state='pid_running'` with `waited_for_drain_ms=None` beside a sidecar
that says `draining_at` is the field's 16:25:23 line reproduced at the unit
seam: the contender knew the owner was alive, had the word "leaving" on disk in
front of it, and refused without waiting a single lap.

3 failed, 3 passed — the three that passed are the arm that must not change
(a healthy owner refused at once), the constants' pin, and the production
defaults' pin.

---

## 4. The mutation table

Each row: one production behaviour removed or inverted, both new test files run,
the verdict quoted. Taken after green, restored from a scratch copy each time —
the tree ends byte-identical (`git diff --stat` re-checked).

| mutation | which tests red |
|---|---|
| `_wait_for_drain()` call removed from `acquire` | `test_serve_socket_drain_wait.py::test_a_draining_owner_is_waited_out_and_the_lane_is_taken`, `::test_an_owner_that_dropped_its_register_row_counts_as_leaving`, `::test_a_wait_that_expires_degrades_as_today_and_says_how_long_it_gave` — **3 failed, 6 passed** |
| `socket_lock.mark_draining()` not called at drain start | `test_harness_serve_drain_order.py::test_the_sidecar_says_it_is_leaving_from_the_first_drain_event_on` — **1 failed, 8 passed** |
| `_finish_drain` drops the row BEFORE it releases the lock | `test_harness_serve_drain_order.py::test_drain_releases_the_lock_before_it_drops_the_row` — **1 failed, 2 passed**, `At index 1 diff: 'row_unregistered' != 'lock_released'` |
| the register-row arm of `_owner_is_leaving` returns False | `test_serve_socket_drain_wait.py::test_an_owner_that_dropped_its_register_row_counts_as_leaving` — **1 failed, 5 passed**, and only that one, so the two arms are independently pinned |
| `serve_instance_unregistered` renamed | `test_harness_serve_drain_order.py::test_the_moment_the_register_row_goes_is_one_line_on_the_service_log` — **1 failed, 2 passed** |

Nothing here is a kill-proof for the constants themselves; those are pinned by
value in `test_serve_socket_drain_wait.py::test_the_bound_is_one_named_constant_above_the_launchers_drain_deadline`,
and the production defaults by `::test_the_default_wait_uses_real_time_and_is_not_left_to_the_caller`.

---

## 5. Deviations

**1. Three existing tests grew a register row, and it was a REAL red, not a
cosmetic one.** `test_serve_socket_lane.py::test_a_live_owner_is_refused_exactly_as_before_and_nothing_is_taken_over`
took **25.09 s** after the wait landed (pytest `--durations`) — it passed, but
through the drain wait, describing the wrong scenario. Its incumbent was a bare
sidecar naming a live pid with no registry row, which under RS-4 reads as an
owner that has already unregistered. Two more had the same shape:
`::test_the_second_serve_for_a_root_degrades_to_stdio_and_names_the_owner`
(fabricated pid 4242, whose liveness is the box's business — a latent
machine-dependent 25 s) and
`test_serve_gateway_lane.py::test_a_socket_lock_lost_to_a_live_owner_names_the_holder_on_the_gateway_block`.
All three now write `serve_instances/<pid>.json` through a `_serving_row`
helper, which is them saying out loud the thing they always meant: *alive **and
serving***. Timing back to baseline afterwards (three files: 23.8 s on this
branch vs 22.8 s at `c670168049`).

**2. A second module constant.** The plan says "one named constant, module
level" for the 25 s bound; the poll cadence is a second one,
`SOCKET_LOCK_DRAIN_POLL_SECONDS`. The ruling's "once" is about the BOUND — the
number that has to stay in step with the launcher — and a bare `0.25` in a loop
would have been a magic number the tests could not name. The launcher-facing
constant is still spelled exactly once.

**3. The order test was green before the fix.** Stated in §1 and §3.1 rather
than engineered around. The plan explicitly allowed either answer; the honest
one is that the shutdown order was already right and the field defect was the
lock's *duration*, not its *position*.

**4. Line-number cites in three canon docs were repointed.** Inserting into
`hermes_cli/harness_parts/serve.py` shifted six `serve.py:<n>` cites in
`03-transport-and-wire.md`, `04-boot-and-lifecycle.md` and `07-observability.md`
by +21/+33 lines; `tests/scripts/test_doc_cite_adjacency.py` caught all six
(`UNWAIVED FAILURES: 6`) and passes after the repoint. Nothing in those
sentences changed — only the numbers.

**5. Not done, and not in scope.** The plan's Stage 3 (RS-6, `code_tree` on the
register row) is a separate stage and is untouched here. The `serve_instance_unregistered`
line is on the service log only — it is deliberately not a new frame kind, for
the same reason `serve_socket_owner_takeover` is not.

**6. The order test arms the end-reason recorder.** `record_end_reason=True` is
what writes the ended note, and arming it installs a process-wide console
control handler on Windows; the test monkeypatches
`_install_console_ctrl_reason_handler` to a no-op so a unit test does not leave
one on the pytest process.

---

# Stage 3 — hermes half (RS-6, the code tree)

Branch `feat/build-code-tree`, in a fresh hermes worktree, from
`114bfd69e7` (Stage 2's landing).

The question RL-20 was asking was the wrong one. It compared `build.commit`
against the checkout's `git rev-parse HEAD`, so on 2026-09-07 at 16:25:09Z a
**docs-only** hermes landing — the prep-cost plan and three canon cites, not one
runtime file — drained a healthy runtime, and the replacement lost the socket
lock for the rest of the session (plan §0). This half makes the runtime state
WHICH CODE it is running, so the launcher can compare that instead.

---

## 1. What was built

**`agent_runtime/build_identity.py`** (new)

- `NON_RUNTIME_PREFIXES = ("docs/", "tests/", ".github/")` — literal path
  prefixes, each ending in `/` so `docsite/serve.py` and `tests_support/helper.py`
  are KEPT. Defined once, here.
- `NON_RUNTIME_ROOT_SUFFIXES = (".md",)` and the rule that uses it, stated
  precisely: **repo-root markdown is a path containing no `/` at all whose name
  ends, case-insensitively, in a suffix from that tuple.** `README.md` and
  `AGENTS.md` go; `agent_runtime/skills/README.md` and
  `skills/runtime-model/SKILL.md` stay, because a live runtime reads and acts on
  the skills tree and a blanket `*.md` rule would report a skill edit as a docs
  landing. This is the one part of RS-6 that is not expressible as a prefix, and
  it is why `code_tree_rule` is a two-key block rather than a bare list
  (deviation 1).
- `code_tree_digest(entries)` — sha1 over `<path>\0<blob>\n` per surviving
  entry, in ascending order of the path's **UTF-8 bytes**. Both halves are
  mirrored in Dart: the NUL stops a path running into a blob, and sorting by
  bytes rather than by the host language's collation is what makes two languages
  agree at all.
- `code_tree_for(root, head="HEAD")` — runs `git ls-tree -r -z HEAD`, never
  raises, returns a typed `CodeTree(code_tree, reason, entry_count)`.
  `CODE_TREE_TIMEOUT_SECONDS = 8.0`, four times `build_stamp`'s bound, because
  this probe reads every tracked path where that one reads a line.
- `-z` rather than the default output: without it git C-quotes any path with a
  space, a quote or a non-ASCII byte in it, and a digest over quoted paths on
  one machine and unquoted ones on another is two digests for one tree.
- Mode bits are deliberately NOT hashed (a `chmod +x` is not code a runtime
  loads differently); gitlinks ARE (a submodule's commit id is code identity).

**`agent_runtime/build_stamp.py`**

- `BuildStamp` gains `code_tree` and `code_tree_reason`, **without defaults**:
  all six construction sites state their own answer, so a new arm cannot inherit
  a silent `None` that reads like a measurement.
- `frame_payload()` gains three keys — `code_tree`, `code_tree_rule` (the rule
  itself, so the launcher applies the one it was HANDED) and `code_tree_reason`.
  That block is the register row's `build`, the `ready` frame's `build` and the
  socket greeting's `build`, all from one place, so wiring it once wired all
  three (`serve.py`, `build_block` — five uses).
- Non-git sources write **no** digest and say why: `not_git:build_sha_file` for
  a Docker image, `not_git:unknown` for an unresolvable checkout. A digest
  fabricated for a baked sha would be a well-formed wrong answer, which is the
  class this module's whole contract exists to refuse.
- One extra subprocess per process, on the same cached resolution as the commit.
  **Measured on this checkout: 78 ms, three runs, 5,724 runtime entries out of
  9,257 tracked paths.**

**Canon**: `docs/agent-runtime-harness/04-boot-and-lifecycle.md`, Stage 4 item 1
(the `build_stamp().frame_payload()` bullet) — the full rule, both fixture
paths, and the two tests that pin the keys.

---

## 2. The reds, quoted

### 2.1 The module (a weak red, and said so)

`tests/agent_runtime/test_build_identity.py` could not import:

```
tests\agent_runtime\test_build_identity.py:25: in <module>
    from agent_runtime.build_identity import (
E   ModuleNotFoundError: No module named 'agent_runtime.build_identity'
```

An import error says a name is missing, not that a behaviour is — the same weak
red Stage 2 called out. The strong reds are all on the WIRING below, which is
where the behaviour lives: a new pure function has no prior behaviour to be
wrong about, and its 30 tests are the rule's specification rather than a
regression fence.

### 2.2 The wiring — 7 failed, 10 passed on the un-wired stamp

`tests/agent_runtime/test_build_stamp.py`, the census first:

```
>       assert set(build_stamp().frame_payload()) == {
            "commit",
            "dirty",
            "source",
            "resolved_at",
            "code_tree",
            "code_tree_rule",
            "code_tree_reason",
        }
E       AssertionError: assert {'commit', 'd...at', 'source'} == {'code_tree',...lved_at', ...}
E
E         Extra items in the right set:
E         'code_tree'
E         'code_tree_reason'
E         'code_tree_rule'
```

then the reads, and the seam:

```
>       assert block["code_tree"] == code_tree_for(real_repo).code_tree
E       KeyError: 'code_tree'
```

```
>       monkeypatch.setattr(build_stamp_module, "code_tree_for", counted)
E       AttributeError: <module 'agent_runtime.build_stamp' from
E       'X:\…\agent_runtime\build_stamp.py'> has no attribute 'code_tree_for'
```

### 2.3 Two censuses that were NOT in the plan caught the change themselves

Neither is a test I wrote for this stage; both are existing censuses that
red because the frame grew, which is exactly what a census is for:

```
tests/agent_runtime/test_serve_service_foundations.py:77
>       assert set(ready["build"]) == {"commit", "dirty", "source", "resolved_at"}
E       AssertionError: assert {'code_tree',...lved_at', ...} == {'commit', 'd...at', 'source'}
E
E         Extra items in the left set:
E         'code_tree_rule'
E         'code_tree'
E         'code_tree_reason'
```

and the same shape at `tests/agent_runtime/test_serve_socket_lane.py:672`
(`test_a_good_token_gets_the_build_handshake`). Both were widened to the seven
keys, and the socket one now says out loud that a remote client reads that
greeting and nothing else.

---

## 3. The shared parity fixture

`tests/fixtures/build_identity/code_tree_parity_tree.txt` — ten `<path>TAB<blob>`
lines, deliberately unsorted, covering every arm of the rule (root markdown,
`docs/`, `tests/`, `.github/`, nested markdown that STAYS, a `.ps1`, two
`agent_runtime/` files). Its byte-equal copy is
`EterniaLauncher/test/fixtures/build_identity/code_tree_parity_tree.txt`.

Two pins hold the two repos to one answer, and neither can read the other repo:

- **the digest** — `10272b75505713833a1fb812e706b961e1d43a50`, pinned by name in
  both suites. A pinned constant is normally circular; it is not circular for
  the job it does here, which is cross-repo. The rule's own correctness is
  pinned by the filtering and ordering tests, not by that number.
- **the bytes** — sha256 `9acf6529f8095872fae8b4c4fb2458e67a9690cb3bf266b4316ee2dacd015e79`,
  pinned on both sides, plus an explicit "no CR in this file" assertion. Both
  repos are `eol=lf`, so LF is what byte-equality means here.

Header lines in the fixture name BOTH repos' paths, so the two copies stay
byte-equal: a header that named only "the other" repo would differ per side and
break the very equality it documents.

---

## 4. Deviations

**1. `code_tree_rule` is a two-key block, not a bare prefix list.** RS-6 says the
row carries "the prefix list itself". Repo-root markdown is not a prefix and
cannot honestly be spelled as one — `*.md` as a list entry would also match
`docs/x.md` for a naive reader, which is the drift the published rule exists to
prevent. So the value is
`{"prefixes": ["docs/", "tests/", ".github/"], "root_suffixes": [".md"]}`: the
prefix list is there, spelled exactly once, with the second half of the rule
beside it instead of hidden in prose.

**2. A third key, `code_tree_reason`.** RS-6 names two. The third exists because
RS-6 also says a non-git source "writes no `code_tree` and the row says so", and
a null digest beside a rule cannot say anything: "this hermes predates the key",
"this is a Docker image" and "git timed out on the boot path" are three
different facts, and the launcher's `rule=commit` fallback should be able to say
which one it fell back FOR. It follows `BuildStamp.reason`'s existing
typed-token style.

**3. `code_tree_for` takes the checkout as well as the head.** RS-6 spells it
`code_tree_for(head)`. A one-argument form would have to resolve a repo root of
its own, and `build_stamp.repo_root_for` already resolved one — two walkers is
two answers. `head` keeps its name and its default.

**4. The row census landed in two places, not one.** The plan says "the row
census test pins the two keys". `test_build_stamp.py::test_the_frame_block_is_the_keys_the_ready_frame_and_the_register_row_carry`
pins the block at its source; `test_serve_registry.py::test_the_row_carries_the_code_tree_and_the_rule_that_made_it`
registers through the SAME `build_stamp().frame_payload()` call `harness serve`
uses and reads the keys back off the written `serve_instances/<pid>.json`. The
second one is what fails if the block stops riding the row while still being
correct at its source.

**5. Ten line-number cites repointed in three canon docs.** The two inserts into
`hermes_cli/harness_parts/serve.py` (RS-6's keys on the two `stamp_failed`
fallback dicts) shifted `serve.py:<n>` cites in `03-transport-and-wire.md`,
`04-boot-and-lifecycle.md` and `07-observability.md` by +9 and +12;
`tests/scripts/test_doc_cite_adjacency.py` named all ten (`UNWAIVED FAILURES: 10`)
and passes after the repoint. Nothing in those sentences changed — only the
numbers. Same class as Stage 2's deviation 4.

**6. One pre-existing red, untouched by this stage.**
`tests/agent_runtime/test_harness_serve.py::test_ready_line_and_exit_frames`
fails on `assert frames[1]["event"] == "ready"` / `assert 'stderr' == 'ready'`,
because a `serve_registry_pruned action=refused reason=unknown
classification_reason=cmdline_not_serve_like` line lands on stderr between
`booting` and `ready` — the test resolves the LIVE runtime root and the refusal
is about the pytest process's own row. **Verified pre-existing**: the two
production files were copied aside, reverted (`git checkout --` /
`rm build_identity.py`), the test re-run — same failure — and the files
restored. It is not in this stage's gate set and is rowed, not fixed here.

**7. Not done, and not in scope.** The launcher half (the Dart digest, RL-20's
comparison, `serve_build_current`) is the other half of Stage 3 and lands in the
launcher repo. The field gate — one docs-only hermes landing on the operator's
machine reading `serve_build_current reason=code_tree_equal` with the runtime
pid unchanged — is the operator's, and no boot has produced that line yet.
