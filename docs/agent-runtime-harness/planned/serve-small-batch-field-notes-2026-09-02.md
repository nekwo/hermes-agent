# Serve small batch — field notes, 2026-09-02

Three rows off `mission-control-queue`, worked in
`_worktrees/w5-serve-small` on `fix/media-get-off-reader-cold-import-census-split`
(cut from `2fc6df259b`). Written as they were found, so the two rows whose
premise did not survive contact say so before they say what was done.

---

## 1. `runtime.media.get`'s proxy arm dialled a peer on the reader loop

### The premise, and the two things the row got wrong about it

The row said to find the arm in `hermes_cli/harness_parts/serve.py`, "the media
family dispatch". **There is no media dispatch in `serve.py`.** The whole
family lives in `agent_runtime/serve_rpc.py` (`_runtime_media_index`,
`_runtime_media_get`, `_peer_media_get`); what `serve.py` owns is the ONE line
that emits whatever `serve_rpc.handle_request` returned. That distinction is
not pedantry — it is why the fix needed both files, and why it needed a seam
rather than an edit.

The row also said the stall is "up to 5 s". It was up to 35: `media_proxy` has
TWO timeouts, `PEER_DIAL_TIMEOUT_SECONDS` (5 s, to give up dialling) and
`PEER_READ_TIMEOUT_SECONDS` (30 s, for an install that accepted the connection
and then went quiet). The 5 s number is the one P4's own gap row quoted, and it
was already the smaller half of its own module's answer.

Everything else in the row held, and reproduced first try. The red test opens
one client, sends `runtime.media.get` for a handle that resolves to a
`RemoteMediaArtifact` whose dial is parked on an `Event`, then sends
`runtime.media.index` on the same connection: the second request never came
back, and the failure was the client's own socket timeout.

### The shape, and why it is the one the request model supports

Two candidates were on the row: move the dial to a pool worker, or bound it on
the reader with a typed `media_peer_unreachable` frame — "whichever the file's
request model supports without a second clock". The parenthetical decides it.
A reader-side bound IS a second clock: `media_proxy` already carries the only
two this work has, and the long-run lane's rule (canon 03, `held_by_long_runs`)
is that a wedged generation is bounded by `PROVIDER_TIMEOUT_SECONDS` at its
source rather than by a watchdog over it. So: the pool.

The method lane is answered inline and `handle_request` always returns a frame,
so "the pool" needed a seam. The precedent was already in the file:
`RpcContext.spawn_chat_turn`, which hands the pool a whole request and acks.
The new `RpcContext.spawn_reply` is that argument made general — it hands the
pool the TAIL of one request, a zero-argument callable returning the frame the
handler would have returned:

* `serve_rpc.DEFERRED` — a sentinel a handler returns instead of a frame.
  Compared by IDENTITY (`is_deferred`), never by shape. Not `None`, which is
  what a handler that forgot to return produces, and not an exception, which
  `handle_request`'s boundary would have turned into `-32000`.
* `serve_rpc.deferred_reply(rid, method, build)` — the same try/except
  `handle_request` has, carried to the worker. The boundary has already
  returned by the time the deferred body runs, and a raise on a pool worker is
  a client waiting forever for a frame nobody writes.
* `serve.py`'s `_spawn_reply` — submits to the SAME pool the chat lane uses,
  declines while draining (a drain is waiting for that pool to empty), and
  declines on a `RuntimeError` from a shutting-down pool. Declining means the
  handler answers inline, which is exactly the pre-existing behaviour.

**What did not move: the frame.** A proxied reply is byte-identical to a local
one, refusals included — the property P4 shipped and the reason the arm is
invisible to a client. `_media_get_frame` was lifted out so the reader arm and
the worker arm build it from one body; a second copy is how that property
quietly stops being true.

**What did move: arrival order.** A deferred reply can land after a later
request's. JSON-RPC 2.0 §6 already requires a client to correlate on `id`, and
every client on this lane already does — `media_proxy._ask` skips frames whose
id is not its own, and so does `test_serve_gateway_lane._rpc`. The e2e test
additionally asserts the sentinel never reached the wire: an id-less frame is
invisible to a correlating client and would be a silent contract breach rather
than a failure.

### Proof

`tests/agent_runtime/test_serve_rpc_media.py` (29 tests, was 27):

* `test_a_peer_that_never_answers_no_longer_delays_the_next_request` — real
  `serve_loop`, real socket, real client. Red before the change (the second
  request timed out on the socket); green after, with the media refusal still
  arriving on its own id once the dial is released.
* `test_a_deferred_handler_fault_is_the_typed_frame_it_would_be_inline`.

Regression set, all green: `test_serve_socket_lane` (60), `test_serve_gateway_lane`,
`test_gateway_media_fetch_e2e`, `test_gateway_peer_cross_install_media_e2e`,
`test_serve_rpc_chat_turn`, `test_cross_install_media`,
`test_serve_drain_accounting` — 161 tests, 0 failed.

### Not done

No live two-machine shot. The proof is a parked dial in-process, which is the
right instrument for "the reader is not waiting" and the wrong one for "a real
switched-off install behaves this way". The existing cross-install e2e covers
the wire; nothing here covers the two together.

---

## 2. Cold import in a single-test pytest run

### Premise verdict: the cost is real, the attribution is wrong, and the failure does not reproduce

The row (quoting `0aab190d4`) said a single-test run "costs 10–38 s of cold
import, almost all of it the cold `discover_builtin_tools` AST walk that
`perform_agent_create` drags in on first import", and that this crosses the
suite's `--timeout=30`. Three corrections, all measured on this box today.

**(a) The AST walk is not the cost.** `tools/registry.discover_builtin_tools`
has memoized its per-file AST verdicts on disk, keyed on `(mtime_ns, size)`,
for some time. Measured: the cold AST scan is **1.11 s** over 112 files; with
the cache warm it is **0.055 s**. What the function actually costs is the
`importlib.import_module` of the **38** modules that do register — measured
**3.36 s** with the scan fully cached, and it pulls 691 modules into
`sys.modules`.

**(b) It is not "at import" of anything `perform_agent_create` imports.** The
BW-H3 deferral already moved `model_tools` off every module scope on that path.
The import is reached at RUNTIME, and the chain is worth writing down because
nothing in the row would let you find it:

```
perform_agent_create
  → persona_assignments.add_instance → open_chat
  → state_patches.emit_persona_instance_create
  → state_patches.project_persona_instance_full_wire_row
  → persona_assignments.persona_instance_summary
  → persona_runtime.apply_chat_lane_tool_scope
  → personas.all_registered_toolsets
  → from model_tools import get_available_toolsets
```

Measured with `-X importtime -s` on
`test_two_live_actors_holding_one_desk_id_are_seen`: `model_tools` is **5.47 s
cumulative** (2.65 s self), the single largest entry in the run, against
`agent_runtime.harness_doctor`'s 3.53 s.

**(c) The cap is not being crossed.** Ten claim tests carry `--timeout=180`;
run WITHOUT that override (so the suite's own `--timeout=30` applies), five of
them, three fresh interpreters each:

| test | pytest-reported, median | max |
|---|---|---|
| `test_two_live_actors_holding_one_desk_id_are_seen` | 4.54 s | 7.77 s |
| `test_a_same_instance_duplicate_is_a_defect` | 5.24 s | 5.67 s |
| `test_a_pulled_actor_with_a_legacy_id_spelling_is_not_an_orphan` | 9.76 s | 12.80 s |
| `test_an_unreadable_office_actor_file_is_named_in_the_unreadable_list` | 6.04 s | 6.43 s |
| `test_harness_doctor_human_branch_says_nothing_about_an_unexamined_census` | 2.41 s | 2.90 s |

Every one exits 0 under the 30 s cap. So the stated failure mode does not
reproduce here, warm.

### The cut that was in scope, and the one that was not

`all_registered_toolsets` wanted a list of STRINGS and asked
`get_available_toolsets()`, which answers the same key set — both fold
`entry.toolset` over one registry snapshot, so the sets are equal by
construction, and this was checked empirically as well — PLUS an `available`
boolean per toolset. That boolean is the whole cost: it runs every toolset's
`check_fn`, which probes binaries, reads env and builds external clients.

Interleaved A/B, 5 fresh interpreters each, measuring the first call including
the `model_tools` import:

| | samples | median | min |
|---|---|---|---|
| before (`get_available_toolsets().keys()`) | 2.72 3.08 3.24 3.32 3.01 | **3.08 s** | 2.72 s |
| after (`get_registered_toolset_names()`) | 1.61 1.69 1.97 1.65 1.51 | **1.65 s** | 1.51 s |

−1.43 s, −46%, on `perform_agent_create`'s own path — a production saving, not
only a test one. The counter that makes it a test rather than a stopwatch is
`tools.registry.probe_rounds_this_thread()`: **25 → 0** availability rounds for
one name lookup.

What was NOT done, deliberately: the row's other candidate, "a cached manifest
keyed on the `tools/` tree hash". It would buy the 1.1 s AST scan and none of
the 3.4 s of module imports, and the cache would be cold in the suite anyway —
`_discovery_cache_path()` is under `get_hermes_home()`, which `run_tests.sh`
points at a fresh temp dir per file. Making toolset NAMES answerable without
importing the modules that register them is a stage with a generated in-tree
artifact and a gate to keep it honest, not a row.

### The consequence nobody asked for, and it is the biggest thing in this batch

Three tests went red on that four-line change, in two files, and every one of
them was asserting that the waste EXISTS:

* `test_a_cold_create_pays_probe_rounds_and_the_next_one_pays_none` (retired, in
  `test_persona_prewarm.py`) — replaced by
  `tests/agent_runtime/test_persona_prewarm.py::test_the_prewarm_fills_the_PERSONA_keyed_readiness_memo_the_create_reads`
* `test_without_the_prewarm_the_same_create_pays_the_rounds_again` (retired, same
  file) — replaced by the same per-persona gate
* `test_an_unwarmed_mint_bills_its_probe_rounds_to_the_chat_lane_scope_read`
  (retired, in `test_agent_create_subphases.py`) — replaced by
  `tests/agent_runtime/test_agent_create_subphases.py::test_a_mint_bills_no_probe_rounds_to_any_projection_read`

*(The three retired names are written WITHOUT their `file::test` form on purpose:
`tests/test_coverage_claims_resolve.py` scans `planned/` and reds on a
doubled-colon reference that no longer resolves, which is exactly right — a
coverage claim naming a deleted test is a claim with nothing behind it. The
history stays readable; the resolvable references point at what replaced them.
Repointed 2026-09-03 by the S0a builder, which is when the gate was found red on
`main`.)*

They are the counted half of **Stage 3a of
`mission-control-agent-drop-latency-2026-08-21`** and of **W3-H1**, and between
them they convicted `apply_chat_lane_tool_scope` as the read that paid every
probe round an unwarmed create ran (`instance_ms` 2,781, of which
`chat_lane_scope_ms` 2,421 and `tool_visibility_ms` 0). That conviction was
correct. The rounds are simply gone now: the ONLY way that call reached the
sweep was `all_registered_toolsets`, and a create against a deliberately holed
`check_fn` cache now bills **zero** rounds to all three named projection reads.

Two GATES therefore became vacuous —
`test_after_a_prewarm_the_create_pays_no_probe_rounds` and
`test_a_warmed_mint_probes_nothing_so_the_warm_fills_the_key_the_create_reads`
both read zero with the prewarm deleted — and in each case it was their
anti-vacuity twin that said so by becoming unsatisfiable. That is the pair
design working, and it is the reason the vacuous gates were removed rather than
kept green: an unrun gate is indistinguishable from a passing one
([07 — Observability](../07-observability.md#an-unrun-gate-is-indistinguishable-from-a-passing-one),
the canonical statement of that principle).

What replaced them, in each file, is the stronger single statement neither could
make: *a create against a holed `check_fn` cache probes nothing*. What was NOT
replaced with something weaker: the prewarm's surviving per-persona gate,
`test_the_prewarm_fills_the_PERSONA_keyed_readiness_memo_the_create_reads`,
which was always the honest witness for "the warm primed THIS type" and says so
in its own docstring; and `test_the_warm_fills_the_exact_toolset_key_the_create_reads`.

**This is a row for the 3a stage's owner and it is not answered here.** The
prewarm still fills the lru and readiness memos, and the registry POPULATE — the
38 module imports under `tools/`, the larger half of that 2,421 ms — is
untouched and is still what a first create pays cold. But one of the four memos
in its header is now struck through, and whether the remaining three justify a
background worker, an RPC verb and a launcher trigger is a measurement nobody
has taken since. Docstrings annotated rather than rewritten, in
`persona_prewarm.py`, `chat_lane_bundle.py`, `mission_chat_turn_context.py` and
`persona_runtime.py` — the live receipts they quote (`registry_probe_rounds=27`
per context build) were true and are now historical.

### The nine `--timeout=180` claims: KEEP them

They should not drop, and the measurement is why. The numbers above are a warm
box with a warm filesystem; one run in this same session took **68 s of wall**
for a single `test_agent_create_service` test on a cold cache, and the commit
that added the override measured 10–38 s on this same machine three days ago.
`--timeout=30` is a HANG detector, not a performance budget, and the failure it
produced when it fired was not a slow test — it was the claim lane failing its
BASELINE and reporting "mutation result would be meaningless" instead of any
verdict at all. An unrun gate is indistinguishable from a passing one
([07 — Observability](../07-observability.md#an-unrun-gate-is-indistinguishable-from-a-passing-one));
that is the sentence `0aab190d4` was written under and it still holds. Re-evaluate when
the toolset-manifest stage lands and the import itself is gone.

---

## 3. `_placement_census_report` was four sweeps in one loop

Straight extraction, no behaviour change. The read and the GATE stay in
`_placement_census_report` — they are the part that must happen once, in order,
for the whole census, and the gate's argument (one unreadable file anywhere
makes the JOIN untrustworthy, because both findings are statements about
ABSENCE) is a property of the whole world and not of a workspace. What moved is
everything after them:

| function | answers |
|---|---|
| `_census_live_actor_bindings(scan)` | the workspace's live actors, each with its canonical instance binding |
| `_census_join_workspace(...)` | `(placed, orphans, referenced)` |
| `_census_agent_item_bindings(bindings)` | per persona, the bindings of every live `agent` ITEM |
| `_census_desk_litter(...)` | the desk rows, given the above |
| `_census_duplicate_placements(...)` | item ids held by more than one live actor |

Two notes on the seams:

* **`_census_join_workspace` takes a `receipt_for` resolver, not a store.** The
  memo stays the caller's (`_retire_receipt_for`), which is what keeps the read
  per orphaned INSTANCE across the whole census rather than per workspace, and
  what lets the sweep be asked the retire question with no retirement archive on
  disk.
* **`referenced` is returned, not mutated through.** "Which rows did this
  workspace claim" is an answer; the caller folds it.

### Anchors

Six mutation claims pointed into `_placement_census_report`. Five still resolve
there (`a4-…`, `ax7-…` on the remediation string; `hh8-the-census-opens-actor-
items-for-duplicate-holders` and `hh8-a-same-instance-duplicate-moves-the-
verdict` on the fold and the verdict; `hh11-a-short-office-scan-forces-the-
census-unknown` on the gate). One MOVED with its block and was re-anchored by
symbol: `hh11-the-census-canonicalizes-the-actor-side-of-the-join` now names
`_census_live_actor_bindings`, with its `find`/`replace` re-indented from 12
columns to 8. `--list` selects all six.

### Proof

`tests/agent_runtime/test_harness_doctor.py` 48 → 52. The four new cases ask
each sweep the one thing it decides, on a world handed to it — which is what
this extraction buys and what the loop never allowed:

* the binding sweep drops archived actors and KEEPS the class-keyed one (it is
  out of the join, not out of the desk and duplicate sweeps);
* the join sweep touches the receipt resolver exactly once, for the orphan;
* the desk sweep is paired by an agent item on an actor it has not reached —
  the case that makes the agent-item pass a separate, earlier pass rather than a
  fold, and the one a single pass would answer by directory order;
* one actor listing an id twice is ONE holder.

`test_harness_doctor` (52), `tests/hermes_cli/test_doctor.py` (49),
`tests/hermes_cli/test_harness_cli.py` (61) — 162 tests, 0 failed.

---

## Gate state

* Lane A: `doc_cite_adjacency.py --exclude archive --exclude planned` → 0
  unwaived, 0 stale (16 cites in canon 03/04/07 followed this batch's line
  shifts in `serve.py` and `serve_rpc.py`); `dump_cli_contract.py --check` →
  fresh, 189 command paths, unchanged sha.
* Mutation claims: 8 added, 1 re-anchored. `--list` selects 15 against the
  fork point `2fc6df259b`, over the default cap of 12, so the gate runs with
  an explicit `--max-candidates 40` — the house number for a multi-row landing.

**`origin/main` has moved past this branch's fork point** (`a855a8e3bd` vs
`2fc6df259b`) while this batch was in flight, and `tests/mutation_claims.json`
is one of the files that moved. `--base origin/main` therefore selects the
OTHER branch's claims and reports this diff as deleting their work; the correct
base for this branch is the merge-base, and that is what every number above was
taken against.
