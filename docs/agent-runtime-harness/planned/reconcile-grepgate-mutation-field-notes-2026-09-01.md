# Field notes — reconcile basis race, grep-gate exemptions, mutation-gate hygiene

**Lane C**, 2026-09-01. Branch `fix/hermes-reconcile-grepgate-mutation-hygiene`,
cut from the pinned baseline `98d43d0c8604adeebfeacd074382f66022e0f5dd` into the
worktree `X:\wt\hyg`. Three queue rows discharged, one queue row corrected.

These are running notes: what was re-measured, what the rulings turned out to be
wrong about, and what the next lane should not have to rediscover.

---

## Part 1 — the reconcile basis race

### Re-measurement at the branch base (before building)

The hole is real and still exactly as the row describes. Read at
`98d43d0c86`:

- `agent_runtime/store.py::_resolve_activation_write` is a per-pointer CAS on
  `intent_issued_at`, `apply` / `superseded` / `duplicate`, and it fails OPEN
  (either side unparseable → `apply`).
- `reconcile_active_workspace_to_realm` returned `None`. Its
  `store.set_active(..., issued_at=<the REALM intent's basis>)` could be refused
  `superseded`, and the refusal was **discarded** — there was no caller-visible
  outcome at all.
- `activate_realm` called it for effect only and then answered `applied: true`
  unconditionally.

So order A straddles exactly as filed: realm pointer in B, workspace pointer in
a workspace of A, both verbs reporting success, nothing on the tree able to
notice afterwards.

**One correction to the brief's own account of order A.** The brief's worked
example has the explicit workspace selection (T2) land first and then the realm
switch (T1) apply to the realm pointer. That sequence is NOT reachable once the
follow arm (arm 2) exists — and it was only reachable before it in the narrow
case where the workspace gesture did not move the realm pointer, i.e. the
selected workspace already belonged to the realm the pointer held. With both
arms built, the arrangement that reaches the heal is:

1. realm pointer parked at T0,
2. an explicit workspace selection at T2 **inside that same realm** (so the
   follow arm no-ops and the realm pointer keeps its older T0 basis),
3. the late realm switch at T1 (T0 < T1 < T2) wins the realm CAS and then loses
   the reconcile.

That is what `test_order_a_a_late_realm_switch_loses_the_scope_to_a_newer_workspace_selection`
builds, and it is why the fixture needs **two** workspaces in realm A. A test
written to the brief's literal sequence would have been vacuous — the realm
switch would have been refused at the first CAS and never reached the reconcile.

### What was built

`agent_runtime/scope_activation.py`:

- `reconcile_active_workspace_to_realm` now RETURNS its outcome — `set_active`'s
  dict on the write arms, `{"reason": RECONCILE_KEPT}` on the kept-belonging
  early return. The `applied` key describes the WRITE, so a new token was needed
  to keep "nothing to do" apart from "the store declined"; reading them as the
  same thing would send the heal on the most ordinary realm switch there is.
- `activate_realm` reads it. On `superseded` it calls
  `_heal_realm_pointer_to_winning_workspace`, which re-parks the realm pointer to
  the winning workspace's realm **under that workspace pointer's stored basis**,
  and answers `superseded` through the existing `activation_outcome_row` shape.
- `activate_workspace` gained `_follow_realm_of_selected_workspace` on the
  applied arm, and a `--clear` arm that answers a cleared row instead of calling
  `store.get(None)` (which is what it did before, and which raised).
- `agent_runtime/store.py` gained `WorkspaceStore.active_intent_issued_at()` —
  the pointer file's `intent_issued_at`, which `set_active` already writes and
  nothing could read back. The heal must not hand-read the pointer file, and this
  keeps it going through the store.

The invariant is stated in the module docstring and at both arms. Every write
goes through `*Store.set_active`, so the scope patch + `*.activated` emissions
ride along unchanged; no pointer file is hand-written.

### No wire change

Confirmed: no new field on any answer row. `superseded` / `requested_realm_id` /
`applied` already exist and the launcher already handles the superseded arm.
The cross-stack check was run read-only from the launcher primary (below).

### Two behaviour changes that fell out, both deliberate

1. **A workspace selection now emits a second pair of events** when it pulls the
   realm pointer. `test_the_method_lane_inherits_WS1s_scope_patch_for_free` in
   `test_scope_use_methods.py` pinned the two-event list and was updated to four,
   with the last scope row carrying the settled pair. This is the same shape a
   realm switch has emitted since WS1.
2. **`tests/agent_runtime/test_stage13_write_path_integrity.py` asserted the
   straddle.** The since-renamed
   `test_realm_use_reconcile_carries_the_realm_intents_basis` claimed
   "each pointer is owned by the newest intent that touched it" and asserted the
   realm pointer in A with the workspace pointer in a workspace of B — which is
   the defect, written down as the expectation. Renamed to
   `test_a_late_realm_use_loses_the_whole_scope_to_a_newer_workspace_choice` and
   rewritten to the ruling; its surviving half (the late realm switch does not
   drag the workspace pointer) is still asserted. **This is the one place a
   reviewer should look hardest**: a test was changed to match new behaviour, and
   the reason it is legitimate is that the ruling supersedes the claim the test
   was written to, not that the test was inconvenient.

### A residual, stated rather than hidden

A three-way race — the heal's own re-park refused by an even newer realm intent —
can still leave the pointers apart for that instant. The heal re-READS the
pointer after writing so the answer names the real owner rather than what it
asked for, but it does not loop. Two racing writers is the filed scope; three is
not, and a retry loop against a fail-open CAS is a worse shape than an honest
row. Not filed as a queue row because it is a property of the CAS, not of this
change; noted here so the next lane does not read the invariant as unconditional.

`test_the_follow_arm_never_overrides_a_strictly_newer_realm_intent` documents
the adjacent fact: a state where the follow is refused is reachable only by
hand-built store writes, never by a pair of gestures.

---

## Part 2 — the grep-gate ruled-exemptions register

### The 11 → 12 correction, with evidence

The queue row says 11 offenders. **It is 12.** Measured at the branch base by
running the gate itself:

```
python -m pytest tests/test_no_source_grep_assertions.py -q -k no_new_positive
→ 1 failed, 21 deselected
```

and its failure output enumerates twelve keys, which match the brief's list
exactly (the brief had already re-measured and carried 12; the row is what is
stale). Files: `test_agent_create_service.py` (3), `test_office_class_key_one_fence.py`
(6), `test_persona_assignments.py` (2), `test_s29_persona_runtime_context_lane_removal.py`
(1). The brief's note that `…test_s29…::'_blocked_tool_names_for_run(request)'…`
is already `source_grep_debt.txt` line 52 and is NOT an offender is confirmed —
it does not appear in the failure output.

**Orchestrator: the queue row wants amending from 11 to 12.** The row's own
evidence note (`iws-ws4-field-notes-2026-09-01.md`) carries the 11.

### What was built

- `tests/source_grep_ruled_exemptions.txt` — twelve entries, each
  `path::qualname::assertion # <one-line reason>`, every reason written by
  READING the test rather than restating the ruling. Header declares the count
  and declares the register closed to additions.
- `tests/test_no_source_grep_assertions.py` — `_read_ruled_exemptions()` (splits
  the reason off at the LAST ` # `, because the key half is `ast.unparse` output
  this gate does not control and may legally contain that sequence inside a
  string literal); `test_no_new_positive_source_grep_assertion` subtracts
  `ledgered ∪ ruled_exempt`; three new tests —
  `test_ruled_exemptions_have_no_stale_entries`,
  `test_ruled_exemptions_header_count_matches_its_entries`,
  `test_every_ruled_exemption_carries_a_reason`. The failure copy now says both
  files are closed to additions.

A reasonless line is **not** admitted as an exemption, so it fails twice: once
for being reasonless, once because its offender is unaccounted for. That was a
design choice, and it is proven below.

### Both directions proven, each applied AND reverted

| proof | mutation | result |
|---|---|---|
| new positive grep still blocked | wrote a scratch test with `assert "RECONCILE_KEPT" in inspect.getsource(scope_activation)` | `test_no_new_positive_source_grep_assertion` RED, naming the scratch file, remedy citing both registers; file removed, green |
| reason requirement live | stripped the ` # reason` tail off one entry | 3 RED (`no_new_positive`, `header_count`, `carries_a_reason`); restored, green |
| staleness check live | mangled one entry's qualname so it matches no live assertion | 2 RED (`no_new_positive`, `have_no_stale_entries`); restored, green |

Final: `25 passed` (was `1 failed, 21 passed`). Register verified byte-clean
afterwards — no BOM, LF-only, 51 lines, trailing newline.

No production code moved and none of the twelve tests were rewritten: the ruling
was allowlist, not migrate.

---

## Part 3 — mutation-gate hygiene

### Facts re-verified at the baseline

- default `--max-candidates 12` — confirmed in `main()`'s argparse.
- refusal printed `candidate cap exceeded; split the diff or raise the cap
  visibly`, exit 2, **no numbers** — confirmed.
- CI passes `--max-candidates 20` — confirmed at `.github/workflows/tests.yml`
  (the brief is right).
- The mutate loop rewrites `target.write_text(mutated)` and restores
  `target.write_bytes(original)` in a `finally` — confirmed.

### Falsified: `tool/test_quality/README.md` said 16

The README's opening paragraph claimed the gate "applies at most 16 mutations"
and that "CI's `mutation-claims` job passes `--max-candidates 16`". CI passes
**20**, and has since 2026-08-30 — the workflow's own comment says so, two
paragraphs down from the number the README quotes. The README even contains the
sentence "this paragraph said 12 while the gate ran 16", which is the same
defect one revision earlier. It now names no number of its own for CI and points
at the command line instead, with a new §"Which cap, on which lane" carrying the
three lanes (per-stage 12 / landing 40 / CI 20).

This is a **documentation-drift class, not an instance**: the paragraph has now
been wrong twice in a row, both times by copying a number that lives somewhere
else. The structural answer would be a gate that reads the number out of
`.github/workflows/tests.yml` and fails when the README disagrees. Not built —
out of scope for this lane, and it wants a ruling on whether a docs-vs-CI pin is
worth a gate. **Flagged for the orchestrator as a candidate queue row.**

### What was built

- **3a — cap.** Default stays 12. The refusal now reads
  `candidate cap exceeded: <N> selected > --max-candidates <M>; split the diff,
  or pass the cap visibly (a multi-stage LANDING run passes its own cap, e.g.
  --max-candidates 40)`, still exit 2. The landing-lane rule is written in the
  module docstring, the argparse `description`/`epilog`, the `--max-candidates`
  help text, and `tool/test_quality/README.md`. The CI comment that quoted the
  old refusal copy verbatim was updated so it does not misquote.
- **3b — worktree hazard.** The warning is in the module docstring, the argparse
  description, and the README (§"Never share the worktree with a test run").
  `_acquire_gate_lock()` exclusive-creates `REPO_ROOT/.mutation_gate.lock` with
  pid + ISO start + argv; a collision prints the holder in a fenced block with
  the exact path to delete and exits 2. The lock wraps the **whole** run
  including the baselines — they read the tree too. Released in a `finally`.
  No PID-liveness probe: `os.kill(pid, 0)` KILLS on Windows, and this gate's
  primary host is Windows, so the stale-lock exit is deliberately manual.
  `.mutation_gate.lock` added to `.gitignore`.
- `tests/test_mutation_gate_worktree_lock.py` — 9 tests. Driven through `run()`
  with `REPO_ROOT`/`LOCK_PATH` re-rooted onto a temp tree and the git/subprocess
  ends stubbed, because the guarantee is about the lock's LIFECYCLE across a
  whole run (taken before the baseline, released on every exit including a
  raise) and a unit test of `_acquire_gate_lock` alone would leave exactly the
  join under test untested.

**A loader trap worth carrying.** `importlib.util.module_from_spec(...)` +
`exec_module` WITHOUT registering the module in `sys.modules` first makes every
`@dataclass` in the loaded module raise `AttributeError: 'NoneType' object has
no attribute '__dict__'` — `dataclasses._is_type` resolves string annotations
through `sys.modules[cls.__module__]`. Nine tests errored on `ClaimAnchor`
before the module was registered. The final file does not load by path at all
(`scripts/` is a namespace package and `from scripts import
changed_line_mutation_check` is what the gate's own tests already use), but any
future test that DOES load a `scripts/*.py` by path hits this.

### The lock broke the gate's own tests, and that is a finding

The first real gate run of this branch reported **`baseline failed`**. Cause:
`tests/scripts/test_mutation_claim_anchoring.py` and
`tests/scripts/test_changed_line_mutation_check.py` drive
`gate.run(..., list_only=False)` — genuinely mutating runs — so they now take
the lock at the REAL repo root. Two consequences, both real:

1. they exclusive-created `.mutation_gate.lock` in the working tree as a side
   effect they never had before, and
2. **nested inside a live gate run they were REFUSED**, which is exactly what
   happens whenever a diff touches `run` and the gate baselines its own tests.
   Every future change to `run` would have hit this.

Fixed at the shared level rather than per test: a new `tests/scripts/conftest.py`
with one autouse fixture re-rooting `gate.LOCK_PATH` into `tmp_path`. Re-rooting
rather than disabling, so the lock's own behaviour stays under test in
`tests/test_mutation_gate_worktree_lock.py`, which sets its `LOCK_PATH`
explicitly. The lesson generalises: a guard keyed on a module-level path taken
from `REPO_ROOT` will be taken by that module's own tests too.

### The gate falsified a claim in this change's own comments

First full run: **9 selected, 8 KILLED, 1 SURVIVED** —
`iws-straddle-a-kept-workspace-is-not-a-refusal`, whose mutation was
`RECONCILE_KEPT` → `"superseded"`.

It survived because the claim behind it was **false**, and the comment I wrote
beside `RECONCILE_KEPT` asserted it: "a kept pointer must not drag the realm
pointer back". It cannot. A kept workspace belongs to the target realm BY
DEFINITION, so even when the heal is entered its
`winning_realm_id == requested_realm_id` check returns `None` and nothing moves.
The token is worth having for honesty and readability; it is not load-bearing
for the heal, and saying it was would have shipped a comment that reads like a
guarantee and is not one.

Corrected in three places (the `RECONCILE_KEPT` comment, the test docstring that
repeated it, and the claim itself). The claim was re-aimed at the guarantee that
IS there — `iws-straddle-the-kept-arm-still-returns-early`, mutation `return …`
→ `pass`, killed by
`test_the_reconcile_keeps_a_workspace_that_already_belongs_and_says_so`, because
without the early return the ladder re-derives a workspace and can move the
operator off the one they chose.

**This is the gate working as designed**, and it is worth recording as the
positive case: a claim written green-first from the author's belief, caught by
the one mechanism that asks the code instead of the author.

### Red-first proof for the lock

Removed the `finally: LOCK_PATH.unlink(missing_ok=True)`:
`test_the_lock_is_released_after_a_green_run`,
`test_the_lock_is_released_when_the_baseline_refuses` and
`test_the_lock_is_released_even_when_the_run_raises` go red; restored, green.

---

## Verification run list

Recorded so the next lane can repeat exactly what was run, and see what was not.

| command | result |
|---|---|
| `pytest tests/test_no_source_grep_assertions.py -q` | 25 passed (baseline: 1 failed, 21 passed) |
| `pytest tests/test_mutation_gate_worktree_lock.py -q` | 9 passed |
| `pytest tests/agent_runtime/test_scope_straddle_invariant.py -q` | 14 passed |
| `pytest tests/agent_runtime -k "scope or store or activation or realm or workspace or straddle" -q` | 1268 passed / 1 failed → the stage-13 straddle test above; re-run green |
| `pytest tests/{stage13,scope_use_methods,scope_straddle_invariant,scope_patch_coverage,mutation_gate_worktree_lock,no_source_grep_assertions}` | 99 passed |
| `pytest tests/test_mutation_gate_worktree_lock.py tests/scripts -q` | 91 passed, no stray lock left in the tree |
| `python scripts/changed_line_mutation_check.py --base 98d43d0c86 --max-candidates 40` | **9 selected, 9 KILLED, exit 0** (run 1: `baseline failed`; run 2: 8 killed / 1 SURVIVED; run 3 after both corrections: clean) |

Cross-stack, read-only from the launcher primary, both pointed at this worktree:

| command | result |
|---|---|
| `python tool/hermes_serve_frames/generate.py --check --hermes-root X:/wt/hyg --python <hermes venv>` | exit 0 — every serve-frame fixture matches; the notes are only "captured at an older sha" |
| `python tool/test_quality/check_producer_contracts.py --hermes-root X:/wt/hyg --no-generate` | exit 0 — "producer contract fixtures match Hermes: stream frames + response envelopes" |

That is the wire claim discharged with evidence rather than by reading: Part 1
adds no field to any answer row, and no launcher pin moves.

**A collection hazard, not a defect.** `pytest tests -k "<pattern>"` over the
whole tree fails collection on this host with 11 pre-existing errors —
`tests/acp/*`, `tests/acp_adapter/*` (the `acp` package is not installed in the
system interpreter) and `tests/tools/test_search_hidden_dirs.py` (no ripgrep on
PATH). None are reachable from this change; the run costs 5½ minutes before
reporting them. Scope the path (`pytest tests/agent_runtime -k …`) rather than
the pattern.

Interpreter: `C:\Python312\python.exe`. The live venv at
`X:\Eternia\.hermes\venvs\hermes-agent\Scripts\python.exe` has **no pytest** —
worth knowing before a lane spends a round trip on it.
`HERMES_TEST_TMP_ROOT=X:\Eternia\test-tmp` set on every run.

## Amendments owed to the queue (orchestrator reconciles at landing)

1. `test_no_source_grep_assertions` row: **11 → 12** offenders, and the row can
   close — the register plus the three new gate tests are the answer the ruling
   asked for.
2. Mutation `--max-candidates` row: closes. Default stays 12; the landing lane
   passes its own cap; the refusal names its numbers.
3. Mutation-gate/pytest worktree row: closes. Docstring + `--help` + README +
   an enforced lockfile.
4. `activate_realm` reconcile-basis row: closes, with the residual three-way
   race noted above (not a new row — a property of the fail-open CAS).
5. **New candidate row** (Part 3): `tool/test_quality/README.md` has quoted a
   stale CI cap number twice in a row. Recurrence is the finding; the structural
   answer is a pin against `.github/workflows/tests.yml` rather than a third
   correction.
