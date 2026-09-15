# Changed-line mutation claims

`python scripts/changed_line_mutation_check.py --base <merge-base>` selects
only explicit claims whose exact production source overlaps the diff, baselines
their focused commands once, applies each mutation within a wall-clock budget,
and requires each focused test to fail. The number a run enforces is the one on
the command line, so read it there — this paragraph said 12 while the gate ran
16, then said 16 while CI ran 20. The original bytes are restored in `finally`
after every candidate.

## The bound is wall clock, and the count is only reported

Ruled 2026-09-04, replacing `--max-candidates`. The cap was always a proxy for
runtime — one baseline plus one mutant test run per candidate — and the proxy
kept mis-reading the thing it stood for:

- **Symbol-overlap selection raises the count by design.** Measured on W1-H3's
  own diffs: 6 → 27, 32 → 64, 98 → 104 candidates against a cap of 20.
- **The same work selects wildly different counts depending on the base.** The
  27/64/104 figures come from a merge-base over a whole branch; on a push the
  base is `HEAD~1` and a two-commit branch selects 2 and 2.

Neither of those moves how long a run takes, which is the only thing the bound
was protecting. So:

- **`--wall-budget-seconds` is the bound.** Default `900` — fifteen minutes,
  the ceiling CI's own job timeout already enforced. It is checked before the
  mutating phase and again between claims, never inside one (a run stopped
  mid-mutation would leave a spliced file on disk), so an overrun STOPS with a
  report naming what ran and what remains instead of being killed by a timeout.
- **The candidate count is printed on every run and never asserted.** It is the
  most useful number in the report; it is not a verdict.
- **CI** passes `--wall-budget-seconds 600` in the `mutation-claims` job, with
  the reason written in the comment beside the call site. That one line is
  PINNED: `tests/scripts/test_mutation_cap_matches_workflow.py` parses the
  number out of the workflow's own step and out of this bullet and fails when
  they disagree. It hand-types neither, so changing CI's budget reds the pin
  until this line is updated — the mechanism the paragraph above was asking for
  after being wrong twice. Keep the bullet's shape (**CI**, the job id, and the
  flag on one line); that is what the test reads.
- **A long multi-stage LANDING run passes its own budget**, the same way and
  for the same reason: splitting the diff is not available to a landing whose
  whole point is that its stages land together.

The doctrine is unchanged and only its unit moved: the enforced number is
readable beside the command that enforces it, with the reason in the command
line rather than buried in a default. It is a RUNTIME bound, never a quality
bound — dropping claims to fit is the failure this gate exists to prevent.

## Never share the worktree with a test run

The gate **rewrites source files in place** for the duration of a run. Anything
else reading the tree meanwhile — a pytest run in another terminal, an editor's
test-on-save, a watcher — reads sabotaged source and reds tests that pass in
isolation. At the console that is indistinguishable from a real defect; the H
landing lost ten minutes to exactly that before the concurrency was identified.

A second *mutating* run against the same tree is refused: the script
exclusive-creates `.mutation_gate.lock` at the repo root, prints the holder's
pid and start time on a collision, and exits 2. There is deliberately no
liveness probe on the recorded pid (`os.kill(pid, 0)` KILLS the process on
Windows), so a lock left behind by a crashed run is cleared by hand — the
refusal prints the exact path to delete. `--list` and `--claims-for` never take
the lock; they touch no source file.

Claims live in `tests/mutation_claims.json`. Defect/ruling tests should add a
claim in the same change as the production fix. A stale claim fails
configuration instead of mutating a similar-looking line silently.

## The anchor: `symbol` first, then the block (H-H14, 2026-08-30)

`symbol` is **load-bearing**, not a label. It is a dotted definition path —
`upsert_actor`, `OfficeStore.archive_actors_for_instance`, a module constant —
optionally followed by `/<prose>` naming which line inside it the claim is about
(`build_parser/work_list`); only the half before the `/` is resolved. `module`
(or `module scope`) means the whole file and is the right spelling for a claim
on an import or a module-level binding.

Two things follow, and both replace a silence:

- **`find` must occur exactly once INSIDE that symbol**, not once in the file.
  A sibling function spelling the same line is no longer a configuration error,
  and the mutation is spliced at the resolved offset rather than handed to
  `str.replace`, which would rewrite whichever copy came first.
- **A `symbol` that no longer resolves is fatal**, and the error names the
  symbol that does hold the block ("it is in `normalize_agent_create` — re-anchor
  the claim's symbol"). Before this, `symbol` was never read at all:
  `r1-create-stops-fencing-the-supplied-placement` named a `_parse_request` that
  `agent_create.py` had not had for months, and the gate said nothing.

A block that MOVED or was **re-indented** inside its symbol still anchors: the
registered `find` is matched again with every line shifted by one constant, up
to `MAX_REINDENT_COLUMNS`, and the replacement is re-indented onto the block's
current column. Relative nesting is preserved by construction, so an extraction
out of a nested `try` (two levels of dedent) re-anchors and a block whose inner
structure changed does not. A re-anchored run says so — `RE-ANCHORED: <id> …
re-indented -8 columns` — after the `mutation candidates:` line, for selected
and unselected claims alike.

## `derived_at`: the needle is a spelling, and the quiet failure is silence

`find` is a source SPELLING inside the anchored symbol, so a semantic edit that
leaves the spelling standing runs a mutation nobody re-derived — and the run
goes green on a guarantee that may no longer be the guarantee. The loud version
of this is impossible to miss (`mutation source must occur exactly once; found
0`, paid by S8b and by the S5 landing); the quiet one had no surface at all.

Ruled 2026-09-04, and all three halves are deliberate:

- **One optional field.** A claim may carry `derived_at`, the commit its needle
  was last derived at. Nothing else records provenance, and the schema had to be
  taught the key: unknown claim fields are refused by design, so this is not a
  convention an author could have adopted on their own.
- **No backfill.** ABSENCE means "written before this schema" and says nothing
  about a claim's health. The rows that predate the field are not silently
  asserted to be fresh.
- **A stale marker is a WARNING, never a failure.** A selected claim whose file
  has moved since its `derived_at` prints
  `WARNING: stale derivation: <id> was derived at <sha> and <path> has moved in
  <n> commit(s) since` on stdout, beside the rest of the report, and the run's
  exit code is untouched. Staleness is a suspicion, not a defect — a claim whose
  file moved underneath it is usually still correct, and a gate that refuses on
  suspicion is one that gets turned off.

The count is of commits to the FILE, not to the symbol: a per-symbol read would
need the anchor resolved at the old commit to say something the report is not
entitled to say anyway. The line is a prompt to a human re-derivation.

Honest exceptions live in `mutation_exemptions.yaml` (JSON syntax is valid YAML
and keeps the pre-install CI selector dependency-free). Every exception must
name its path, symbol, operator, reason, owner, issue/evidence, and expiry.
Allowed reasons are `equivalent`, `observability-only`, `generated`, and
`contract-out-of-scope`; expired entries fail the check.
