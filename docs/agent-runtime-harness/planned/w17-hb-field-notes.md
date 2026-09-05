# W17 lane hb — field notes (hermes, 2026-09-05)

Three rows, branch `w17/hb`, cut from `4e8c053a0a`, worked in its own worktree.
Nothing pushed; the operator lands.

| row | queue | outcome |
|---|---|---|
| the seven remaining one-armed branches in `agent/charsheet` | `spatial-queue.md`:96 | CLOSED — 7 → 0 |
| flow-graph canvas replication: the two hermes follow-ups | `mission-control-queue.md`:409 | see §2 |
| the coverage-claim gate: is the lane ENABLED anywhere | `mission-control-queue.md`:594 | see §3 |

---

## 1. The seven named-but-unwritten arcs — 7 → 0

**Asked.** Each of the seven arcs w16/ha left is named with the suite that
already has the machinery. Give each a case or delete it as provably
indistinguishable, then re-run the report and state before/after.

### Re-measured first, at this base

```
$ python scripts/unreachable_branch_report.py -j 4        # before
7 files, 463 tests passed, 0 failed in 197.0s (4 workers) — verdict exit 0
   draft 4 | draft_lock 2 | pipeline 1                                   = 7
```

Module for module and arc for arc, w16/ha's hand-off reproduces exactly:
`draft.py` 1100→1101, 1355→1356, 1433→1434, 1627→1628; `draft_lock.py`
189→191, 221→222; `pipeline.py` 752→753.

One practical note for whoever runs it next: the report must be run with the
canonical test venv's interpreter (`~/.venvs/hermes-test/Scripts/python.exe`),
not whatever `python` is on PATH. `coverage` lives in the venv, and the script
refuses (exit 2) rather than reporting from an interpreter that lacks it —
correctly, but the refusal is easy to read as "the tool is broken".

### `pipeline.py` 752→753 — BRANCH DELETED, provably indistinguishable

The arc was `if len(cutouts) != len(order)` after
`extract_strip_frames(strip, len(order), fit=False)`. Writing the case the row
named — a provider stub whose strip does not divide into the authored count —
is what proved the guard could never speak:

`extract_strip_frames` ends in `_validate_extracted_frames`
(`agent/pet/generate/atlas.py`), whose FIRST tier is a hard
`len(frames) != frame_count` raise under BOTH methods, and the only thing after
it is a per-frame `_fit_to_cell` map. So the call answers exactly `len(order)`
cutouts or it raises. Measured both directions on the real extractor:

| strip the stub returned | answer |
|---|---|
| one pose short (2 for 3 authored) | `ValueError: frame 1 is empty` |
| one pose over (4 for 3 authored) | exactly 3 cutouts |
| a single pose | `ValueError: frame 0 is empty` |

Deleted rather than given a `# pragma: no cover`, because the two OTHER
`extract_strip_frames` call sites in the same module
(`generate_row_strip`, `compose_draft_frames`) already trust that contract
instead of re-checking it — this one was the odd one out, not one half of a
twin. The docstring's promise ("slicing failures raise") is unchanged; the
refusal now comes from the module that owns the contract, with the count in its
message.

**Proof that it is indistinguishable, not just untested:** the new test
`test_a_turnaround_strip_that_cannot_be_cut_into_the_authored_directions_is_refused`
passes IDENTICALLY against `HEAD`'s pipeline.py (guard present) and against the
edited one (guard gone). Same input, same verdict, both ways — which is what
"no reachable input distinguishes the arms" means.

### `draft_lock.py` 189→191 — CASE, and the premise was wrong

Filed as a two-process race. It is not one: `_REGISTRY` is process-local, and
what empties it early is an exit order that is not LIFO. The outermost
acquisition's `finally` pops the key unconditionally, so ANY release arriving
after it finds nothing — an `ExitStack` unwound in the wrong order, a nested
context manager kept alive past its owner, a generator holding one that is
closed at collection time rather than at its `with`.

Driven through the raw context-manager protocol, because that is the only way to
SPELL a non-LIFO release. Nothing is faked and `_REGISTRY` is never touched.
Mutation-checked: with the guard removed the nested release raises
`TypeError: 'NoneType' object is not subscriptable` inside a `finally` (which
would replace whatever exception was already travelling), and the new test goes
red.

### `draft_lock.py` 221→222 — CASE, with the interleaving controlled and nothing else

A genuine race, and the window (`path.unlink()` → retry `_claim`) is three lines
with no seam of its own. What the test controls is WHEN the rival runs; every
participant is real:

* the rival is a second real thread taking the real lock through the public
  `draft_generation_lock`, so the holder record is its own;
* the file it leaves is a real file, so the retry `_claim` fails for the real
  reason (`O_EXCL` on a path that exists);
* the refusal is read back off that file by `_read_holder`, which is why the
  message names `compose` and not the stale `rows` this call broke.

The seam is `Path.unlink`, the last instruction before the window opens. A test
that reached into `_REGISTRY` or stubbed `_claim` would be asserting the stub —
w16/ha's objection, and it stands. Mutation-checked: drop the retry check and
the call is admitted while it does not own the file (`DID NOT RAISE`).

**Rejected first, and worth recording:** making the retry lose deterministically
by leaving a DIRECTORY at the lock path. On Windows `os.open(dir, O_CREAT |
O_EXCL | O_WRONLY)` raises `PermissionError`, not `FileExistsError`, so `_claim`
propagates instead of answering False — a different arm, on one host only.
(That asymmetry is a latent finding about `_claim`, not this row's.)

### `draft.py` 1100→1101 and 1627→1628 — CASE, one mechanism, two refusals

Both read as unreachable because stage `rows` is only entered once EVERY
authored direction is approved. Both are reachable, because the stage is read
off the frame's OWN `draft.json` snapshot and two frames over one draft is the
case this package exists for (the serve child runs four pool workers in one
process).

A second real `CharacterDraft` loaded BEFORE the advance honestly holds stage
`turnaround`, so `reroll_direction` is a legal verb for it; `propose` clears the
approval (a new candidate reopens QA); the revision store caches nothing between
calls, so the advanced frame reads `current() is None` on its next generation.
The generation lock does not stand in the way — the verbs are serial, and its
contract is one WRITER at a time, not one frame per draft.

Two tests, not one parametrize: the row loop and the palette loop answer
different questions and only one is about rows. Compose passes its
"row(s) have no approved strip" check and still has nothing to build the sheet
palette from, which is exactly why the second guard is not redundant.

### `draft.py` 1355→1356 and 1433→1434 — CASE, recorded history and no file

Not the "no attempt to crop yet" refusal one guard above: history is intact, so
`attempt_index` resolves and the store hands back the path it recorded. What is
gone is the file — a `characters delete` under a frame that still holds the
draft, a half-restored backup, a quarantine, a sync that dropped a blob. The
store's own invariant is one-directional ("state can never reference an image
that is missing"), so this is the one direction it can break in, and the refusal
has to name the path an operator would go looking for.

Two tests again, for the reason the pair above needed two: a single case could
not tell `row@…` from `turnaround@…` apart, and whichever arm it did not take
would stay exactly as unreached as it was.

### Before and after

```
$ python scripts/unreachable_branch_report.py -j 4        # before
7 files, 463 tests passed, 0 failed in 197.0s (4 workers) — verdict exit 0
   draft 4 | draft_lock 2 | pipeline 1 | palette 0 | prompts 0 | revisions 0 | spec 0   = 7

$ python scripts/unreachable_branch_report.py -j 4        # after
7 files, 470 tests passed, 0 failed in 209.6s (4 workers) — verdict exit 0
   every module 0                                                                       = 0
```

**7 → 0.** Six arcs given a case, one branch deleted. Every module in
`agent/charsheet` now reports zero one-armed branches. COLD statements fell as a
side effect and were not the target: `draft.py` 29 → 25, `draft_lock.py` 8 → 6,
`pipeline.py` 1 → 0.

### What is left

* **COLD statements are still untriaged** — `draft.py` 25, `draft_lock.py` 6,
  `revisions.py` 6. A different finding from one-armed (nothing in this suite
  calls the code at all), and still nobody's row.
* The report is still a REPORT: nothing consumes its exit code, and this lane
  did not change that.

---

## 2. The flow-graph canvas: the two hermes follow-ups

**Asked.** The row lists exactly two, both hermes-side: the canvas REVERT arm
(`store_drift` deliberately does not carry this family until it exists, or
`revert --all` KeyErrors on `_PROCESS_ORDER`), and `flow_graphs_stale/`, which
still does not replicate per the plan's §3.

### (a) The canvas revert arm — BUILT

The blocker was real and w14/h2 stated it exactly: `revert_realm_sync` sorts
selected rows through `_PROCESS_ORDER[row.family]` — a direct subscript — and
dispatches on family for the upstream lookup, the baseline and the store door.
So the family joins the drift set and gains its arm in ONE change, never two.

What landed, family-shaped like the persona-INSTANCE family beside it:

| concern | the canvas family's answer |
|---|---|
| drift rows | `_flow_graph_store_drift_items`, scoped to `_office_publish_scan(...).instance_ids` — the desks a publish already ships, never a second walk of the graph directory (which legitimately holds drawings for owners no realm places) |
| counts | `store_drift.flow_graphs` = `canvases_added` / `canvases_changed` / `canvases_removed`, additive |
| upstream | `read_remote_flow_graphs(subtree)` cached as ONE document, so `unreadable` is a property of the projection and never of a row |
| admission | `parse_flow_graph_doc` and only it |
| adopt / restore | `FlowGraphStore.set_doc` |
| local-only | `FlowGraphStore.archive(graph_id, stale_dir())` |
| baseline | `flow_graph_baseline_key`, realigned per item from the STORE's post-write content |
| order | LAST (`_PROCESS_ORDER` rank 2) |

**Three decisions worth the ink.**

*The container is the OWNER INSTANCE id, not the workspace.* Written with the
workspace first, and the tests caught it in the shape that matters: a
`removed` row is a drawing whose desk is GONE, so there is no instance record
to read a workspace off, the container came back `""`, and
`flow_graph::runtime_x` is refused by `parse_item_spec` — the exact rows an
operator most needs to revert would have been reachable only through `--all`.
Derived from the graph id instead (`runtime_<instance id>` → the owner), it
cannot be blank by construction. **The persona-instance family has the same
latent hole** — `_persona_instance_store_drift_items` leaves `container=""` for
a removed instance with no record, and `--item` cannot address it. Not touched
here; it is a row.

*The admission door is `parse_flow_graph_doc` and NOT the shared
`refuse_entity` scan every other family adds.* The lane's contract is "a revert
writes nothing a pull could not have written", and `apply_flow_graph_pull`
holds only the parser. Adding the shared door here would refuse the operator a
drawing that already landed on this machine through the pull — a worse
inconsistency than either door alone.

*An ABSENT projection drives the archive arm, and that is deliberate.* A realm
where nobody has drawn anything publishes no `store/flow_graphs.yaml` at all
(the publish appends the artifact only when the projection is non-empty), so
absence is the NORMAL shape for a genuinely local-only drawing. It is also what
an older publisher looks like — the version-skew case — and the two are
indistinguishable from here. The cost is bounded by archive-never-delete: the
drawing lands in `flow_graphs_stale/` and is recoverable by hand, which is the
same place owner-liveness reaping puts one.

**Two claims that were true when written and are now false**, corrected in the
same change rather than left: `_flow_graph_status_row`'s docstring and
`test_flow_graph_status.py`'s "the canvas family is NOT a store_drift family"
assertion. The top-level `flow_graphs` row stays — it answers what a drift row
cannot (`publishable`, `held` sidecars, `unreadable`) — and its `unpublished`
is now pinned as `canvases_added + canvases_changed`, which is NOT the row
total: a reaped canvas is a drift row with nothing left to publish.

Response-envelope fixtures regenerated through
`scripts/generate_agent_runtime_response_fixtures.py` (additive key only, two
files). **The launcher mirror under
`EterniaLauncher/test/fixtures/hermes_responses/` is the cross-repo half and is
NOT done here** — it is in this lane's hand-back.

Measured: `scripts/run_tests.sh` over the 11 touched suites — 242 passed, 0
failed. 8 new tests, the first of which is the one that would have caught the
original hazard: `set(_PROCESS_ORDER) == FAMILIES`.

### (b) `flow_graphs_stale/` — a DECISION, not work, and it is handed back

The plan's §3 is headed *"What this plan does not decide"* and its whole entry
is: *"Archived docs stay local. Replicating a reaped drawing has no
requester."* That is a statement of the current behaviour plus the reason
nobody built the alternative — not a ruling either way.

This lane did not build it, and the argument for not building it is
`AGENTS.md`'s own: **speculative infrastructure — a hook with no concrete
consumer — is rejected even when well built.** Nothing on either repo reads a
stale canvas: `flow_graphs_stale/` has exactly one writer (`FlowGraphStore
.archive`, from owner-liveness reaping and now from a local-only revert) and
zero readers outside `tests/agent_runtime/test_s25_graph_prune_on_reap.py`.
Replicating it would put a second machine's reaped drawings on the wire for a
surface that does not exist.

What it is NOT is settled, and the honest shape of the remaining question is
one sentence: *when a member reaps a canvas whose owner departed, should the
drawing survive on the peers that never saw the departure?* Today it does —
each member reaps its own copy on its own liveness view, so a drawing is
archived once per machine and never realm-wide. That is arguably correct
(archive is local diagnostic state, exactly like the office family's) and it is
what the code does. **Handed back as a decision row, with the recommendation:
close it as a non-goal until a reader exists.**

---

## 3. Does CI actually run the coverage-claim gate? It dispatches it and it never reaches a test

**Asked.** Option 1 first: read `ci.yml`, `tests.yml` and the slice generator,
check the run history, and either wire failure notification for main-push runs
and delete the row, or hand it back narrowed to option 3.

### The ruling's premise, checked line by line — and it holds

| claim | measured |
|---|---|
| `ci.yml` fires on `push: branches: [main]` | yes, and the `tests` job is gated on `detect.outputs.python` |
| `detect` fails open on push | yes — `.github/actions/detect-changes` only gates `pull_request`; every other event leaves `CHANGED` empty and every lane runs |
| the slice matrix's discovery reaches the two scopes | yes. `python3 scripts/run_tests_parallel.py --generate-slices 8` on this tree: **3,040 files** over 8 slices, including `tests/test_coverage_claims_resolve.py` and all **18** files under `tests/scripts/` |

So "these gates sit outside the lane" is false of CI, exactly as the ruling
said. Every conclusion above is from the artifacts, not from reading intent.

### And then the run history, which answers a different question

`gh run list --repo nekwo/hermes-agent` (the fork these gates live on; plain
`gh run list` resolves to `NousResearch/hermes-agent` and answers about
somebody else's code):

```
60 CI runs on main, 2026-09-02 → 2026-09-05:  55 failure, 5 cancelled, 0 success
```

Every one of those runs dispatched all 8 test slices. Every slice failed. The
reason is the same one every time, and it is not a test:

```
File "scripts/run_tests_parallel.py", line 162, in _split_path_list
OSError: [Errno 36] File name too long: 'tests/plugins/test_kanban_dashboard_plugin.py:tests/…'
```

`--files` hands the runner ONE colon-joined slice — ~380 files, roughly 30 KB —
and `_split_path_list` opened with `if Path(raw).exists()`, a fast path for
"the argument IS one path that contains a colon". A joined list is not a path,
and the two hosts disagree about how to say so: Linux's `stat()` answers
`ENAMETOOLONG` for anything past `PATH_MAX` and `pathlib` does not ignore that
errno, so the probe RAISES; Windows folds the same overflow into its ignored
winerror set and answers `False`. **The identical call is green on every
developer's Windows box and fatal on every CI runner.**

Bisected against the history: the 2026-07-03 and 2026-07-25 runs collect and
run files (red for ordinary reasons); the 2026-08-04 run — the first main push
after `0c9d48d9fb` (2026-07-31) added the probe — carries the `ENAMETOOLONG`
signature, and so does every run since, through 2026-09-05.

**So the honest answer to the row is neither of its two options.** CI *runs the
job*; the job dies 8 seconds in, before pytest is invoked, in the runner's
argument parsing. `tests/test_coverage_claims_resolve.py` has been in scope and
unexecuted for a month, and so has every other test in the repo.

### What this lane did about it

Fixed the crash, because it is one line and it is the only thing standing
between the ruling's option 1 and being true: the `exists()` probe is wrapped
in `try/except OSError`, with the asymmetry written at the site so the next
author does not re-add it.

Red-first, and the test asserts both halves because either alone is a
half-test — the real over-`PATH_MAX` list (which is what CI passes, and enough
on POSIX) and a forced `OSError` from `Path.exists` (which is what makes a
Windows author trip here rather than shipping the same asymmetry again).
Verified red at `HEAD` with the exact CI error, green after.

### Why the notification half was NOT wired, and what to wire instead

There is no failure-notification mechanism in this repo at all. The only
notifier in `.github/workflows/` is `comment-live`, gated
`github.event_name == 'pull_request'`; nothing matches `failure()`,
`actions/github-script`, an issue-opening action or a webhook. GitHub's own
default — an email to the pusher when a workflow fails on the default branch —
is therefore the entire mechanism, and it has fired on 55 consecutive main
pushes without anyone acting on it.

That is the argument against arming a second channel today: a notifier pointed
at a lane that is red on 100% of pushes trains its reader to ignore it, which
is the failure mode the row is about, one layer further out. **Arm it after the
first green run, not before.**

The smallest one, for when that happens: a job in `ci.yml` with
`needs: [all-checks-pass]`, `if: failure() && github.event_name == 'push' &&
github.ref == 'refs/heads/main'`, using `actions/github-script` to upsert ONE
issue titled `CI red on main` (search by label, reopen and comment rather than
opening a second) with `all-checks-pass`'s `needs-json` output — which already
exists and already names the failed jobs — in the body. It needs only
`issues: write`, no secret and no external service, and the upsert is what
keeps a month of red from becoming a month of issues.

### The row, and why it is narrowed rather than deleted

Not deleted: the fix is unverified where it matters. This lane cannot push, so
the claim "CI now runs the gate" is a prediction until a main push turns the
slices green — and the same runs are also red on `ruff enforcement`, the
`Changed-line mutation claims` job and `Desktop E2E`, none of which this lane
touched and each of which will still be red behind the fixed slices.

Not option 3 either: a second, post-landing local runner is a second runner for
work CI does once the crash is fixed, and the ruling already said so. What
survives is one verification step and one conditional build. The verbatim text
is in this lane's hand-back.
