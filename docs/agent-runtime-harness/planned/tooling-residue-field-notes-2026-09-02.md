# Tooling residue, 2026-09-02 — five rows, and the two premises that were wrong

Branch `fix/hermes-tooling-probe-anchor-runner`, cut from `origin/main` at
`10d0d3a41c`. Five rows filed the same day: the adjacency probe's backtick
pairing, the mutation gate's symbol anchor, the runner's straggler summary, a
test that could not finish inside the per-test cap, and one wrong paragraph in
`AGENTS.md`.

Two of the five carried a diagnosis that does not survive measurement. Both are
recorded here with the numbers rather than quietly implemented, because a row
that is right about the symptom and wrong about the cause is the shape this
repo keeps meeting (`feedback_remeasure_review_rows`).

A third premise, carried in the dispatch rather than in a row, is also wrong and
worth killing here so it does not travel: **`scripts/` changes are NOT
unclaimable by the mutation gate.** `tests/mutation_claims.json` already carried
six claims on `scripts/` paths, five of them on
`changed_line_mutation_check.py` itself — the gate mutates its own source and
runs the focused test in a fresh subprocess, so mutating it is exactly as safe
as mutating anything else. All three changes in this wave therefore carry
claims (`w7-*`), and all three were selected and KILLED at the branch base.

---

## 1. The adjacency probe pairs backticks per LINE

**The row.** `BACKTICKED` pairs greedily across prose; a line with an odd
backtick count hands `subjects()` a span of ordinary English; six (in fact
eight) of the 2026-09-02 waivers are `BACKTICK-SPAN NOISE` on cites read and
confirmed CORRECT.

**Confirmed, and the mechanism is one step earlier than the row says.** The
odd-count line is usually not the writer's typo. Dumped at the call site, all
three noise cites I could reproduce by hand have the same shape: the SUBJECT
WINDOW's own cut lands inside a code span, so the first backtick the window
sees is a CLOSING one and every pair after it is inverted. `07|snapshot.py:437`
is the cleanest — the window opens mid-`` `_format_ttfb_token` `` and the
"identifiers" come out as `detect`, `downstream`, `instantaneous`, `reader`,
`which`. The doc's real subject, `_log_agents_readiness_split`, sits at 437-454
and was never read. Same for the two table-row cites. Markdown fences are the
other source: 88 of the canon's 269 odd-backtick lines are ``` fences.

**The rule.** `_spans()` splits on `\n` and matches within each line. A span
never crosses a boundary — which is exactly "refuses to pair across a line whose
backticks do not balance", since a balanced line never needs to.

**The variant that keeps hard wraps was tried and is worse.** This canon
hard-wraps at ~80 columns, so ~90 of its code spans are written across two
lines and the strict rule stops reading them. The obvious repair — carry an
unclosed span into the next line when THAT line is also unbalanced — preserves
those, and re-creates the junk wherever the window's cut leaves both boundary
lines odd. Measured over the gated canon:

| rule | checked | adjacent | in-symbol | FAILED | unchecked (no subject) | new findings | waivers retired |
|---|---|---|---|---|---|---|---|
| current (whole-text) | 289 | 190 | 9 | 90 | 61 | — | — |
| carry-into-odd-line | 286 | 194 | 10 | 82 | 64 | 3 | 11 |
| **per line (shipped)** | **283** | **193** | **10** | **80** | **67** | **4** | **14** |

The carry variant's "extra" green is a false pass: it keeps
`07|agent_runtime/stream.py:135-173` passing on `advanced` / `having` /
`honest` — and that cite is real rot, pointing sixty lines away from its
emitter. A rule whose only advantage is a coincidence is not the rule.

Losing a subject can only make a cite UNCHECKED or FAILED, never turn a red
cite green, which is the same safety direction `MAX_SUBJECT_OCCURRENCES` rests
on. That direction is what makes the ~90 lost wraps a price rather than a hole.

**The four findings the junk had been hiding, read one by one.** All four were
answered in the canon; none was waived.

| doc | cite | verdict |
|---|---|---|
| 04:48 | `hermes_cli/main.py:5039` | ROT. 5039 is a bare `#:` line in a constant's comment block; the stale-lock break is `_break_stale_bytecode_sweep_lock` at 5056. Re-anchored AND the symbol named, which is what makes it checkable. |
| 04:221 | `tool_visibility.py:466` | CORRECT (466 IS `_PROFILE_READINESS_TTL_SECONDS = 15.0`), but the function it belongs to starts at 471, two lines outside the window. Widened to `:466-471`. |
| 07:167 | `agent_runtime/stream.py:135-173` | ROT. That range is `_defer_demote_build_for_agent_runs`' docstring; the `snapshot_build` receipt is built at 264-267 in `_log_snapshot_build` (196). Its sibling `stream.py:100-101` ("a launcher in the field still parses `elapsed_ms`") was rotted the same way — the sentence it cites is at 229-230. Both re-anchored. |
| 07:176 | `boot_timeline.py:173-178` | CORRECT (`BootTimeline.log_line`), failing as a TABLE ROW blind spot. Cured by naming the symbol, which is the gate's own advice rather than a waiver. |

**The re-baseline.** 90 → 75 waived; 15 deleted, 0 added. 13 of the 15 now PASS
on their real subject; `02|mission_chat_steer.py:328` is now an honest
UNCHECKED (its table row's only backtick is the cite's own path token, which is
never its own subject); `07|stream.py:100` stopped existing when its cite was
re-anchored. The `_comment` records all of it.

---

## 2. A block is not a naming scope

**The row.** `_qualified_definitions` walks `ast.iter_child_nodes` only, so
`serve_loop._drain_monitor` — a `def` inside a `try:` inside `serve_loop` —
resolves to nothing, and the claim has to be spelled
`serve_loop/_drain_monitor terminal`. Confirmed exactly: the AST path is
`FunctionDef:serve_loop > Try > _drain_monitor`.

The consequence is not cosmetic. Anchoring at the outer function puts the
`find` back inside `serve_loop`'s whole span — 2,600 lines — with every
sibling's copy of the same line available to collide with, which is the
"must occur exactly once" failure the symbol anchor was built to end.

**The rule.** The walk descends through `If` / `Try` / `TryStar` / `With` /
`AsyncWith` / `For` / `AsyncFor` / `While` / `Match` / `match_case` /
`ExceptHandler` with the SAME prefix: a block adds nesting, not a name.
`serve_loop._drain_monitor` now anchors, and the bare `_drain_monitor` resolves
through the existing suffix rule.

**The note this makes half-stale was written before the descent existed and was
right.** `WHOLE_MODULE_SYMBOLS`' comment argued that teaching the walk to
descend would not help `agent_runtime/locks.py`, because its two
`if os.name == "nt"` arms would then collide and an ambiguous symbol is refused
by design. That is now observable rather than predicted, and it is pinned:
`test_a_name_defined_once_per_arm_of_a_platform_fork_is_refused_not_guessed`.
`hh6-posix-file-lock-ignores-its-deadline` keeps its `module` anchor.

**Nothing else moved.** `--base origin/main --list` resolves all 283 claims,
which is the whole-repo preflight: no existing symbol became ambiguous under
the descent.

---

## 3. The straggler's collection was invisible to the nothing-ran guard

`tests_collected` was accumulated only inside `_on_done`, the POOL's callback.
The straggler pass — which re-runs a timeout-shaped file once at 1-worker
isolation — updated `tests_passed` and `tests_failed` beside it and left
`tests_collected` alone. So a single-file run whose only file is killed at 8
workers and passes at isolation prints `RETRY PASS … (95 tests)`,
`Summary: 95 tests passed, 0 failed`, and then
`✗ NO TESTS RAN — 0 collected across 1 file. This is NOT a pass.` The zero is
the killed attempt's, and it is the only one the guard ever saw.

One line, the same key set as `_on_done` (skips, errors and xfails count as
collection — an all-skipped platform-gated file DID collect).

**The tests are in-process on purpose, and the reason is a real defect in the
file they live in.** The first cut drove the runner in a subprocess, the way
that file's older tests do, and died on the repo-wide `--timeout=30`. Chased
down: one inner-runner invocation over a two-test probe costs **63.2 s on an
idle box**, and the probe's own collection is what costs it —
`python -m pytest --collect-only <probe>` over a file under `tmp_path` runs
**137 s** and then errors:

```
FileNotFoundError: [WinError 2] … 'C:\Users\beast\AppData\Local\Temp\hermes-test-home-nb3pj1n_'
```

`tmp_path` is under the system Temp, so pytest's rootdir becomes the Temp
directory and session collection walks the whole thing — including the
`hermes-test-home-*` directories other test processes are creating and deleting
underneath it. The cost is proportional to how much junk the box's Temp holds,
which is why this reads as flake.

`_load_runner_module()` plus a scripted `_run_one_file_once` gives the same
three assertions in **3.3 s**, including the anti-vacuity case (retry killed
too → the guard still fires) and the skips-count case.

**Pre-existing red, filed not fixed.** `tests/test_run_tests_parallel.py` is red
on this box independent of this change: with all three new tests deselected
(`-k "not straggler"`) the file still dies at
`test_bare_value_flag_keeps_its_value`, the second test in the file. Raising its
cap would only hide a 63 s probe; the cure AGENTS.md already names is
`HERMES_TEST_TMP_ROOT` pointed at a dedicated throwaway dir, which is a runner
env-contract decision rather than a test edit.

---

## 4. `test_doc_cite_report.py` — the premise, corrected

**The row.** It "times out before collection (the per-file cap; find why — a
slow import or a slow module fixture)".

**Neither, and there is no module fixture in the file at all.** Measured on the
Windows dev box:

| what | cost |
|---|---|
| `import scripts.doc_cite_report` | 0.54 s |
| `report.main` over the gated canon, three runs | 26.6 / 40.0 / 46.0 s |
| — of which `git` subprocesses | 32.1 s across 356 calls |
| — of which `_line_count` | 0.9 s across 634 calls |
| the whole file under `run_tests.sh`, idle box | 25.7 s, 6 passed |

The cost is one test, `test_the_report_exits_zero_over_the_live_canon`, and
within it two `git` spawns per distinct sha (`cat-file -e`, then
`merge-base --is-ancestor`) at ~90 ms each on Windows. Against `pyproject.toml`'s
`--timeout=30` that is green on an idle box and dead under an 8-worker suite —
and what the reporter saw is `run_tests_parallel`'s bucket line, whose wording
is "collection/import error, timeout before collection, etc.". That is a label
covering several causes, not a diagnosis.

**Bounded, as `2fc6df259b` bounded the claims gate**: `pytest.mark.timeout(180)`
on the one test, with the measurement in the constant's comment beside it, so
the gate stops depending on someone remembering `--timeout=600`.

RED FIRST in both directions, because "the marker wins" needed proving and a
downward proof alone would not distinguish "the marker wins" from "the smaller
number wins":

* the marker at 5 s reds that test with a `Timeout` while the other five pass;
* a throwaway 35 s test under `@pytest.mark.timeout(60)` PASSES under the same
  `--timeout=30` addopts (48.9 s wall, file deleted afterwards).

**Left open, filed:** the 32.1 s of per-sha `git` spawns is the actual cost and
is batchable — one `cat-file --batch-check` for existence, one `rev-list … --not
<base>` for ancestry, two processes instead of 356. That is a change to what the
report DOES, not a bound on it, so it is a row rather than a smuggled rewrite.

---

## 5. `AGENTS.md` item 3 was wrong for every test process

The claim — module-level constants caching `get_hermes_home()` are fine because
the constant is bound after `_apply_profile_override()` — holds only when the
process is a hermes CLI entrypoint. `_profile_bootstrap.is_hermes_cli_entrypoint`
gates the override off elsewhere on purpose (an import must not re-parse
pytest's argv, and must not point the whole session at the operator's live
profile), the module is imported at COLLECTION, and the autouse hermetic-home
fixture redirects `HERMES_HOME` afterwards.

The tree already disagreed in three places: `tests/test_no_frozen_hermes_home.py`
is a ratchet that treats every such constant as debt (51 frozen names, ledgered,
red on a stale entry); `hermes_state._resolve_default_db_path` is the call-time
pattern that keeps `monkeypatch.setattr` isolation working; and
`hermes_cli/doctor.py` states the whole argument at its own module top after its
`PRAGMA integrity_check` ran against the developer's real `state.db`.

Item 3 now says resolve at CALL time, shows the `_resolve_default_db_path`
shape, points at the ratchet, and keeps the one honest exception: a frozen name
that is a LABEL and never a filesystem read (`display_hermes_home()`).

The same false premise appears one more time, under "Adding New Tools" → "Path
references in tool schemas". A schema string IS the label case, so the guidance
was right and its reason was not; both that sentence and the "State files"
bullet beside it now point at rule 3.

---

---

## 6. Two reds that arrived with the base, not with this branch

* **`test_every_coverage_claim_names_a_test_that_exists` is red by three**, all
  in `planned/serve-small-batch-field-notes-2026-09-02.md:190-192`, which landed
  in the branch base `10d0d3a41c` itself and is byte-identical here
  (`git diff origin/main` on it is empty). The three named tests were retired by
  the persona-prewarm change the vault row describes — the gates went vacuous
  and were replaced — and the field note was not re-pointed. Owner is that
  landing, not this one.
* **`tests/test_run_tests_parallel.py`** — §3 above.

Both are FILED rather than swept into this commit: one belongs to another
session's landing, the other is an env-contract decision.

---

## What is not done

* The `git`-spawn batching in §4 — filed, not built.
* `HERMES_TEST_TMP_ROOT` for the runner's own subprocess tests — filed, not
  built.
* Nothing here has been run under a full-suite pass; the proof is the owned
  files plus the two Lane A gates and the mutation gate.
