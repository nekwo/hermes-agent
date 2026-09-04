# Wave 12, lane m4 — field notes (2026-09-04)

Seven medium rows off `mission-control-queue.md`, all hermes. Base
`dcba382f0a`, branch `w12/m4`, worktree slug `w12-m4`. Four rows produced
code; three are handed back unbuilt with the reason and the measurement.

The lane's own headline: **two of the three "this needs a decision" rows did
not need one.** Row 124 named an env-contract ruling as its cure and the real
cause was four lines away from the test; row 126 asked for a class gate and the
measurement says a gate here would be born red. Both are the `re-measure review
rows` pattern — right that something was wrong, wrong about why.

---

## Row 123 — the doc-cite report's per-sha `git` spawns

**Asked:** batch the two-processes-per-sha existence and ancestry probes.

**Measured at base**, same corpus (`--root docs/agent-runtime-harness
--exclude archive/ --base HEAD`), counting `subprocess.run` calls:

| | spawns | seconds |
|---|---|---|
| `dcba382f0a` | 454 | 46.2 |
| after | 3 | 2.5 |

Report output **byte-identical** between the two (diffed).

**Changed:** `scripts/doc_cite_report.py` grows `_classify_shas`, which
collects the walk's shas and answers all of them in two calls — one
`git cat-file --batch-check` peeling every cite through `^{commit}` on stdin,
one `git rev-list --stdin` with `^<base>`. The batch-check's FULL oid is what
makes the second call expressible at all: a 7-hex cite cannot be compared
against `rev-list` output. The walk now collects `(where, sha)` pairs in doc
order and classifies after, so the row order is unchanged.

Preserved deliberately: a `--base` this clone cannot resolve still reads as
`offline` for every sha, exactly as the per-sha `merge-base --is-ancestor`
did. Batching is a cost change; redesigning that failure mode belongs to
whoever decides this report may exit non-zero.

**Red-first:** `_classify_shas` does not exist at HEAD (asserted directly
against `git show HEAD:scripts/doc_cite_report.py` loaded as a module), and the
new `_SpawnCounter` test asserts `count == 2` for four cites of three distinct
shas alongside the three verdicts. The base version answers 454.

**Verify:** `scripts/run_tests.sh tests/scripts/test_doc_cite_report.py` — 8
passed, 8.0 s (was ~46 s of git before). Commit `022d4b64c8`.

---

## Row 26 — a registration-gated gate reports nothing when nobody registers

**Asked:** make an unregistered slice distinguishable from one needing nothing.

**Measured at base:** `.github/workflows/tests.yml` Select step greps
`^mutation candidates: 0 ` and every later step is `if: run == 'true'`. Both
zeros — "no production source changed" and "seven changed, none registered" —
printed the same line and skipped the same way. S5's own landing is the
instance (`--base 748687daa3^ --list` → 0).

**Changed:** every run now prints a census under the candidate list —

```
changed production sources: N (M carry no registered claim)
  NO CLAIM ANCHORS HERE: <path>
```

sourced from one `git diff --name-only --diff-filter=d <base> -- '*.py'` minus
`tests/` and `tests-js/`. A file is "registered" if ANY claim anchors in it,
selected or not; deletions are excluded because a file that is gone cannot
carry an anchor. On a zero, the workflow turns the count into a
`::warning::` annotation so the skip is legible from the run page.

**Reported, never enforced, and a warning rather than a failure.** Whether a
given changed file MUST carry a claim is unruled and has no honest default;
a gate that guessed it would be switched off inside a week. `mutation
candidates:` stays the first line — CI's grep depends on it, and
`test_every_claim_unselected_still_reports_zero_candidates_first` pins the
whole ordered output.

**Live check** on this branch's own first commit:

```
mutation candidates: 2 (cap 12)
  s4b-the-inventory-drops-the-claims-it-did-not-select: ... (selected by symbol)
  hh14-the-mutant-is-spliced-at-the-first-occurrence-again: ... (selected by symbol)
changed production sources: 2 (1 carry no registered claim)
  NO CLAIM ANCHORS HERE: scripts/doc_cite_report.py
```

**Test churn this forced:** the census asks the REAL repo what a diff touched,
and every gate test injects its changed-line set against a base spelled
`"BASE"`. `tests/scripts/conftest.py` gains an autouse default of "no
production source changed" (the census's own three tests override it), and
`tests/test_mutation_gate_worktree_lock.py`'s `gate` fixture stubs it beside
the `_partition_claims` stub it already carried.

**Verify:** the five gate suites green —
`tests/scripts/test_changed_line_mutation_check.py` (8),
`test_mutation_claim_anchoring.py`, `test_mutation_claim_platforms.py`,
`test_mutation_selection_follows_the_symbol.py`,
`tests/test_mutation_gate_worktree_lock.py` (9). Commit `b4284195d1`.

---

## Row 124 — `test_run_tests_parallel.py` red on Windows

**Asked:** decide the runner's env contract, defaulting `HERMES_TEST_TMP_ROOT`.

**Re-measured at base.** `scripts/run_tests.sh tests/test_run_tests_parallel.py`:
timed out at 8 workers, passed only on the runner's 1-worker file retry —
185.8 s total, 111.8 s of it the retry. With `HERMES_TEST_TMP_ROOT` pointed at
a dedicated dir: green first attempt, 52.7 s. So the row's cure works.

**Then the mechanism, isolated.** A bare probe dir under the system temp:

```
python -m pytest --collect-only <probe>     58.5 s, rc=2
  FileNotFoundError: ... Temp\hermes-serve-frames-5gwgoe43
```

The same probe with an empty `pytest.ini` beside it: **1.0 s, rc=0.**

That is the whole thing. With no ini file at or above the probe — the system
temp has none — pytest's rootdir falls back ABOVE the probe, the collection
tree is rooted over the shared temp directory, and the inner session walks
every other test process's hermetic home while those are being created and
deleted. `HERMES_TEST_TMP_ROOT` "worked" by handing the same walk an emptier
tree to walk. It is not the cure; it is a smaller version of the disease.

**Changed:** `tests/test_run_tests_parallel.py` gains `_root_the_probe`, which
writes `[pytest]\n` into a probe tree, called at the three sites that spawn an
inner pytest (the grandchild leaker's dir, `_make_probe_dir`, and the flaky
retry probe, which is a bare file so `tmp_path` itself is what gets anchored).
A new test reads the rootdir back out of pytest's own header, so a `pytest.ini`
that pytest declined to honour would fail it.

**Verify:** `scripts/run_tests.sh tests/test_run_tests_parallel.py` — 14
passed, **first attempt, no retry, 72.3 s** at 8 workers, with
`HERMES_TEST_TMP_ROOT` UNSET. Commit `6767666dd1`.

**No env decision is needed.** The row's decision half is answered by not
having to make it.

---

## Row 126 — a sync bound above `--timeout=30` can never report

**Asked:** the class — every multi-process test in the tree — with no gate.

**Measured.** `pyproject.toml:399` is unchanged
(`--timeout=30 --timeout-method=thread`). Then the question the row does not
answer: is a gate writable? An AST scan over `tests/test_*.py` for a numeric
wait bound above 30 in a module carrying no `pytest.mark.timeout` flags **51
modules**. Reading them, nearly all are SAFETY VALVES — a
`subprocess.run(..., timeout=60)` around a call that returns in two seconds is
not a wait any test expects to reach. (Three of the 51 are in this lane's own
`tests/test_run_tests_parallel.py`, all valves.) Telling a valve from a bound
is semantic, so a literal gate here is born red and lands an allowlist, which
the house rules forbid outright.

**Changed:** the rule goes where an author would look. `AGENTS.md` §Testing
never mentioned the 30 s cap at all — a new paragraph states it, states that
raising a wall-clock bound without `@pytest.mark.timeout(N)` trades one bad
failure mode for a worse one, names the three worked examples in the tree
(`tests/hermes_cli/test_active_sessions.py`,
`tests/hermes_cli/test_relay_shared_metrics.py`,
`tests/scripts/test_doc_cite_report.py`), and records the 51-module
measurement as the reason there is no gate.

**Verify:** `scripts/doc_cite_adjacency.py --exclude archive --exclude planned`
→ 0 unwaived, 0 stale, exit 0. Commit `d28b04421a`.

---

## Rows handed back without code

### Row 29 — the `find` needle is still a source spelling

Confirmed verbatim at `_anchor_claim` (`scripts/changed_line_mutation_check.py`):
`_symbol_span` resolves the symbol, then `needle = str(claim["find"])` and
`_candidate_offsets(span, needle)` match on the spelling inside that span.
What the code already refuses: a needle that is gone, a needle occurring more
than once in the span, an unresolvable or ambiguous symbol. The residue is
exactly the one text matching cannot see — the needle still occurs once, and
the code around it changed so that the mutation is no longer the killing
mutation for the guarantee anybody registered.

Closing it needs a **provenance decision**, which is an operator ruling, not a
patch: `mutation_claims.json` refuses unknown fields by design
(`_partition_claims`), so recording when a claim was last re-derived means a
schema change plus a backfill of 289 rows, plus a policy on what a stale
`derived` marker DOES (report? refuse? refuse only when the claim was selected
by symbol rather than by lines?). Naming a default here would be guessing.
Stopped, per the brief.

### Row 105 — the CI cap

`.github/workflows/tests.yml` still hard-codes `--max-candidates 20`, and the
local-landing doctrine in the script's docstring is untouched. The open half
is real and the decision the row names — whether the cap keys on stages rather
than claims — is unruled. Worth adding for whoever rules it: the exposure is
PR-shaped, not push-shaped. On a push the job's base is `HEAD~1`, one commit,
which is why this branch's own commits select 2 and 2; the 27 / 64 / 104
measurements come from a merge-base against a whole branch. A cap that is a
RUNTIME bound ("one baseline plus one mutant run per candidate", per the
workflow's own comment) expressed in candidate COUNT is arguably the wrong
unit and wants to be a wall-clock budget — but that is the ruling, not a patch.

### Row 127 — the three `dashboard_auth` files

Could not reproduce today. All three green on the first attempt, no retry:

* the three files alone at 8 workers — 20 passed, 17.6 s;
* the three files inside a **63-file** `tests/hermes_cli` lane at 8 workers —
  540 passed, 0 failed, 128.0 s, and the three finished at 23.2 / 19.3 /
  17.2 s.

That is not the row's evidence, which is the full 597-file lane, so this is
NOT grounds to delete the row. Reading the three files also argues against the
obvious mechanisms: they are in-process FastAPI `TestClient` tests with no
ports, no sleeps and no wall-clock waits. Worth re-running the full
`tests/hermes_cli` lane once before any more is spent on it.

---

## A main red that is not mine

`tests/test_coverage_claims_resolve.py::test_every_coverage_claim_names_a_test_that_exists`
fails at base on five claims in
`docs/agent-runtime-harness/planned/s2-introduce-directory-push.md` (`:583`,
`:588`, `:589`, `:593`) and
`.../s2-introduce-directory-push-field-notes-2026-09-03.md:107`. Named in the
wave brief as pre-existing; confirmed to be exactly those five and nothing of
this lane's. The other three tests in that file pass.
