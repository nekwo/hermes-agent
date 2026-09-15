# W15 lane ha — field notes (hermes, 2026-09-04)

Two rows, both RULED before dispatch, both landed. Branch `w15/ha`, cut from
`74bb73a124`, worked in its own worktree. Nothing pushed; the operator lands.

| row | queue | commit |
|---|---|---|
| the too-weak-fixture CLASS — coverage in the canonical env + the cheap detector | `spatial-queue.md`:110 | `5fb5cf09c0` |
| the palette swatch strip is blocked on hermes persisting the compose-time palette | `studio-queue.md`:52 | `b5d77d9f72` |

---

## 1. `coverage` in the canonical env, and the one-armed-branch report — `5fb5cf09c0`

**Asked.** RULED (operator, recommended answer adopted): add `coverage` to the
canonical hermes test env — pyproject dev extra plus the venv recipe in the
canonical-test-env note — and build the cheap detector: a per-module
unreachable-branch report over the charsheet pipeline suite, **reported, not
gated**. The expensive half the row also names (the sabotage exercise made
runnable over a declared knob list) is a program and stays out.

### The install, exactly as run

`<test venv>` is `$HOME/.venvs/hermes-test` — the probe's default candidate,
reached on this box through the junction the canonical-test-env note records.

```
<test venv>\Scripts\python.exe -m pip install coverage
# Successfully installed coverage-7.16.0
<test venv>\Scripts\python.exe -m pip check
# No broken requirements found.
```

Pinned `coverage==7.16.0` in `pyproject.toml`'s `[dev]` extra, on the existing
line so nothing shifted, and `uv lock` regenerated: it reported exactly
`Added coverage v7.16.0`, the diff is that one package's 56 lines, and
`uv lock --check` exits 0. Nothing else in 252 resolved packages moved.

### The drift report, and the 15 lines that are not mine

`scripts/check_test_env_drift.py` compares the canonical test venv against the
live install and allowlists what is *supposed* to differ. Run before the
allowlist entry:

```
python scripts/check_test_env_drift.py --live <live venv> --test <test venv>
-> check_test_env_drift: 16 difference(s) between live and test venvs
```

`coverage` was line 4 of 16. It is now in `TEST_ONLY_DISTRIBUTIONS` — the live
install is RIGHT not to carry it, since no module the product ships imports it —
and the same command answers 15.

**Those 15 are a pre-existing finding, recorded and not fixed.** They are
`modal==1.5.5` and its dependency closure (`boto3`, `botocore`, `cbor2`,
`grpclib`, `h2`, `hpack`, `hyperframe`, `jmespath`, `protobuf`, `s3transfer`,
`synchronicity`, `toml`, `types-certifi`, `types-toml`) sitting in the TEST venv
and absent from the live one. Nothing in this lane installed them, and the
canonical-test-env note's own open question 1 — *nothing rebuilds this venv from
the live freeze* — is the hole they came through. A lane adding one pin does not
get to reshape the shared environment. What matters for the next person: this
report is not at zero today, and a run answering 15 is the known state.

### Where the measurement runs, and why it is not its own environment

The report runs the suite **through `scripts/run_tests.sh`**. That is the whole
design decision. Coverage needs to wrap each pytest invocation, and the two ways
to get there are:

* re-implement the runner's hermetic environment inside the report (`env -i`,
  the credential drop, `TZ`/`LANG`/`PYTHONHASHSEED`, the Windows location
  variables, the gateway fence's real-store root, per-file subprocess
  isolation), or
* forward one variable through the runner that already does all of it.

The first produces branch numbers measured under pins and variables no suite
actually runs with, and a second spelling of that env block to keep in sync
forever. So: `HERMES_TEST_COVERAGE_RC` names a coverage config file;
`run_tests.sh` forwards it with the same `${VAR:+…}` guard every other opt-in
uses, and `run_tests_parallel.py::_pytest_argv` turns each per-file spawn into
`python -m coverage run --rcfile=<it> -m pytest <file>`. Unset, the argv is
byte-identical to what it always was — asserted, because a plain `VAR="$VAR"`
would hand the child an empty value and make **every** run a traced one.

The seam carries one path and no policy: `branch`, `parallel`, `data_file` and
`source` all live in the config file the report writes into a temp directory, so
changing what is measured never means changing the runner, and nothing is ever
written into the repo.

**Red-first.** The base tree has no `_pytest_argv` and no `_COVERAGE_RC_ENV`
(`git show HEAD:scripts/run_tests_parallel.py` → both `False`), and
`scripts/run_tests.sh` at `74bb73a124` does not mention the variable at all
(`grep -c` → 0). The three seam tests in `tests/test_run_tests_parallel.py`
cannot pass there.

### The report's one judgement call

`coverage`'s `missing_branches` mixes two facts in one bag, and summed together
the useful one is buried:

* an arc off a line the suite **never executed** — the whole function is cold,
  and the finding is "nothing calls this";
* an arc off a line that **did** execute — the predicate was evaluated, over and
  over, and one arm never once won.

The second is the class the row is about: a knob no fixture distinguishes. The
report splits them (`one-armed` against `cold`), sorts modules by the *finding*
rather than by total missing arcs, and prints the caveat with every run —
"unreachable" means unreached **by the suite that was traced**, so a branch
another suite covers reads identically and `--suite` must be widened before
anyone concludes a branch is dead.

`tests/scripts/test_unreachable_branch_report.py` pins that split on fixtures,
including the ordering: a module with one one-armed branch outranks one that
simply never ran.

### The first measurement

```
python scripts/unreachable_branch_report.py -j 4
```

`6 files, 408 tests passed, 0 failed in 225.8s (4 workers)` under tracing — a
green suite, so the numbers mean what they say.

```
module                                   stmts  cold  brs  one-armed  cold brs
agent/charsheet/pipeline.py                599    17  220         19         0
agent/charsheet/draft.py                   589    37  136         12         0
agent/charsheet/palette.py                  76     9   30          8         0
agent/charsheet/prompts.py                  55     4   12          3         0
agent/charsheet/draft_lock.py               92     8   14          2         0
agent/charsheet/revisions.py               169     7   42          1         0
agent/charsheet/spec.py                    158     1   74          1         0
agent/charsheet/__init__.py                  2     0    0          0         0
agent/charsheet/errors.py                   10     0    0          0         0
```

**46 one-armed branches across the package, and the shape of them is the
finding.** Nearly every one points at a line the suite never executed — the
`if X:` ran on every case and `X` was never true. Three examples, picked because
they are three different kinds and the difference matters to whoever triages
them:

* `palette.py:130` — `if used:` in `palette_colors`, whose FALSE arm never
  runs. Every palette this package builds comes out of `Image.quantize`, which
  always populates `.palette.colors`. This is the class exactly: a knob no
  reachable input distinguishes, and the standing answer is to find the input or
  delete the arm.
* `pipeline.py:481` — a `raise ValueError` type guard on `frames`. Never fired,
  and that is fine: a guard against a caller's mistake is not required to have a
  case, though a cheap one costs little.
* `draft.py:1985` — `_handedness_accepted`'s tolerance for the ROUND-TWO
  manifest spelling. Deliberate back-compat with no fixture. A case is worth
  writing here rather than deleting the arm, since the population it protects is
  installed characters on disk.

So the report does what the row wanted and no more: it turns "is any branch in
this module unreachable today" from unanswerable into a 46-line list. Deciding
each of the 46 is triage nobody has done, and is what the row narrows to.

Cost, for whoever runs it next: 225.8s wall at `-j 4` (350.8s subprocess
CPU-wall), dominated by `test_charsheet_pipeline.py`. Tracing is not free, which
is why the report raises the per-test timeout to 120s — the repo's `addopts`
passes `--timeout=30` unconditionally, and a traced image-pipeline test can
cross it on a busy box and report a timeout as a failure, making the whole
measurement look red for a reason that is not about branches.

---

## 2. The compose-time palette on both payloads — `b5d77d9f72`

**Asked.** RULED (operator): option B, ship the swatch strip display-only. The
launcher lane (w14/t1) re-measured at `c66ab9a04` that neither `CharaDraftStatus`
nor `CharaInstalledSheet` carries a palette, so its half could not start, and
wrote the hermes patch verbatim into its field note §5: persist the quantized
colour table as `palette.json`, ordered by descending pixel count, publish it on
the draft-status envelope and each installed `characters list` row, and keep an
absent key absent rather than `[]`.

### Measured first — the pointer in the row was close, not right

The launcher note pointed at "the same step `atlas_to_webp_bytes` is called
from, in `agent/pet/generate/atlas.py`'s neighbourhood", and asked the hermes
lane to confirm rather than trust it. Confirmed and corrected:
`atlas_to_webp_bytes` is an **encoder** and knows nothing about a palette. The
quantization is `agent/charsheet/palette.py`, the sheet-level call site is
`pipeline.compose_draft_frames` (`build_sheet_palette` → `lock_to_palette` per
cell), and the only place that holds both the finished sheet and the two
directories it belongs to is `CharacterDraft.compose`. That is where the write
went.

### The table is measured off the sheet, not off the palette image

`build_palette` returns a `P`-mode image. It can be asked which colours it *can*
produce (`palette_colors`) but not how much of the character each one is — and a
slot the median cut allocated that no cell ever used is not a colour of this
character; as a swatch it would name a colour that is nowhere on the sheet. So
`palette_table` histograms the composed sheet: its distinct opaque colours ARE
the locked table, and the counts come free in the same pass.

Two consequences worth writing down because they are not obvious from the row:

* **Alpha is `ff` on every entry.** The locked palette is an opaque RGB table —
  `lock_to_palette` carries alpha separately, byte for byte, so one jacket blue
  appears at dozens of alphas along an antialiased edge. Grouping by RGBA would
  answer thousands of entries for a 48-colour sheet. The wire spelling the
  launcher asked for is `#RRGGBBAA`, and it gets it; the alpha byte is a
  constant and says so in the docstring.
* **Pixels at or below `ALPHA_FLOOR` vote for nothing** — the same floor
  `build_palette` samples with, so the table cannot name a colour the palette
  never saw.

Ties break on the colour itself, so the same sheet answers the same table run
after run and a consumer diffing two tables reads a real change rather than a
histogram's iteration order.

### Two homes, one measurement

`compose` writes `palette.json` beside the **draft** and beside the **install**,
from one in-memory table. Not a duplication to be collapsed later: they are
different objects whose lifetimes do not agree. `status --json` answers from the
draft while an operator is still working; `characters list` answers from the
install afterwards; and a slug can be installed from a draft that is not this
one — which is exactly why `compose` already carries a clobber guard. Neither
directory can answer for the other, and one reader (`read_palette`) serves both.

### Absent is not empty, and this is where the difference is made

`read_palette` answers `None` for a missing, unreadable or malformed file, and
both payloads spell the key as a conditional entry in a fixed position:

* `None` → **no `palette` key at all**. The character was composed before the
  table existed, or the draft has not composed.
* `[]` → would mean a sheet with no opaque pixels, which validation refuses. It
  is not a value any composed character can carry.

Flattening the two would hand the launcher one value for two facts, and the
strip would render "no colours" for a character that merely predates the field.
`read_palette` is also total on purpose: a colour table is a display detail, and
taking `characters list` down over a corrupt one is the worse outcome.

### `sprite` is deliberately untouched

The row named two payloads and this change carries two. The sprite payload's
consumer byte-copies the sheet (`bundle_character.dart` decodes nothing), it is
already the heaviest payload in the family — 468.8 KiB of base64 on the live
three-state character — and no surface in the strip's plan reads it. Adding a
third publisher of the same fact would be three places to keep in step for a
reader that does not exist.

### The payload contract now SEES the key

This is the part the row did not ask for and the repo demands. The launcher's
`sidecarDisagreementsWithHermes` walks **every** payload key by default-deny;
`handednessAccepted` was added blind once and threw for every character on every
machine with hermes installed. `hermes_cli/charsheet_payload_contract.py` exists
so a producer move lands in a file instead of at runtime — but it derives the key
set by RUNNING the verbs, and its probe drafts are never composed. A conditional
key no probe produces is a key the contract is silent about.

So `status` and `list` are each probed twice now, with and without a table on
disk (`_palette_files`, which writes the file the way `compose` writes it and
removes it again — nothing here can run a real compose). Both new paths come
back marked conditional:

```
list   modes ['default', 'palette']   characters[].palette  conditional, modes ['palette']
status modes ['default', 'palette']   status.palette        conditional, modes ['palette']
```

The dump is still byte-stable across runs (verified: two consecutive dumps
`diff` clean), because the probe removes what it wrote.

### Red-first, and the killing mutation the row named

The row named it: reverse or drop the sort, and the case must go red on the
ORDER, not merely on the length. Applied to `palette_table`'s sort key
(`-counts[color]` → `counts[color]`) and run:

```
FAILED tests/agent/test_charsheet_pipeline.py::test_the_table_is_the_colours_in_DESCENDING_pixel_count
FAILED tests/agent/test_charsheet_pipeline.py::test_a_third_colour_lands_where_its_coverage_puts_it
=== Summary: 1 files, 1 tests passed, 2 failed ===
```

then reverted. Two colours can be ordered by luck, so the second case carries
three. The order is also asserted end to end against counts recomputed from the
real installed sheet — and that assertion is given teeth first: a sheet whose
colours all covered the same area would pass any order, so
`len(set(ordered)) > 1` runs before the ordering check.

---

## Verification

Everything through `scripts/run_tests.sh` (canonical runner, canonical venv
`~/.venvs/hermes-test`, nothing set).

| scope | result |
|---|---|
| `tests/agent/test_charsheet_draft.py tests/agent/test_charsheet_pipeline.py` | 260 passed, 0 failed |
| `tests/hermes_cli/test_harness_characters_cli.py tests/agent/test_charsheet_spec.py` | 187 passed, 0 failed |
| `tests/hermes_cli/test_charsheet_payload_contract.py` + `…_flag_admission.py` | 15 passed, 0 failed |
| `tests/test_run_tests_parallel.py` | 17 passed, 0 failed |
| `tests/test_coverage_claims_resolve.py tests/scripts` | 19 files, 160 passed, 0 failed |
| `tests/test_line_endings.py` | 3 passed, 0 failed |

Lane A:

```
python scripts/dump_cli_contract.py --check
-> CLI contract fresh: 191 command paths, sha256 4a30a35fbcf67d7c   (exit 0)

python scripts/doc_cite_adjacency.py --exclude archive --exclude planned
-> UNWAIVED FAILURES: 0, STALE WAIVERS: 0   (exit 0)
```

The cite gate went red once, by five citations, and every one was this lane's
own doing: the palette commit adds 11 lines to `hermes_cli/harness.py` above
line 4400, and five doc cites address that file by line number below it
(`01-system-architecture.md:22` and `:755`, `07-observability.md:637`). Each was
verified to have been ADJACENT at `74bb73a124` before re-pointing, so none of
them is a pre-existing red being papered over. No baseline entry was added; the
numbers moved instead.

No toolset registrar and no argparse changed, so
`dump_toolset_manifest.py --check` and `emit_harness_tool_inventory.py --check`
are not in scope. The `characters payload-contract` verb's parser is untouched —
what moved is the document it prints.

## What the launcher owes (row 2's other half)

Nothing here is a hermes change; it is the handback for the launcher lane.

* `CharaDraftStatus` (`packages/eternia_charsheet/lib/src/review/chara_review_lane.dart`)
  gains an optional `palette` — a `List<String>` of `#RRGGBBAA`, **nullable, not
  defaulted to `[]`**; absent on the wire must stay absent in the model, because
  the strip renders nothing for an old character and a defect for a colourless
  one.
* `CharaInstalledSheet` gains the same field, read off the `characters list`
  row.
* Re-vendor the payload contract (`tool/charsheet_payload_contract/`): `list`
  and `status` each carry a second mode named `palette`, and two new key paths
  `characters[].palette` / `status.palette`, both `conditional: true`. Key
  counts move `list` 28→29 and `status` 71→72; `sprite` (34) and `thumb` (17)
  are unchanged.
* Then Studio stages 2–3 of `docs/studio/planned/w14-t1-palette-swatch-strip.md`.
  Stage 4 (palette ops) is a later row by the ruling's own ordering.
