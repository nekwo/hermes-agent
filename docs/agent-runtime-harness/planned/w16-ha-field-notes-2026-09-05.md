# W16 lane ha — field notes (hermes, 2026-09-05)

Two rows, branch `w16/ha`, cut from `065d40ec9c`, worked in its own worktree.
Nothing pushed; the operator lands.

| row | queue | commit |
|---|---|---|
| the charsheet payload contract is dumped on demand and never committed | `mission-control-queue.md`:711 | `a8046c8838` |
| the 46 one-armed branches in `agent/charsheet` have never been triaged | `spatial-queue.md`:90 | `a0db90db91` |

---

## 1. The payload contract is committed here and gated here — `a8046c8838`

**Asked.** The end state is the argv lane's: commit
`tests/fixtures/charsheet_payload_contract.json`, give
`scripts/dump_payload_contract.py` a `--check` mode, run it in Lane A beside
`dump_cli_contract.py --check`, and pin it the way `hermes_cli_contract.json` is
pinned.

### Re-measured first

The premise held at the base. `scripts/dump_payload_contract.py` printed the
document and stopped there; nothing under `tests/fixtures/` carried it, and
`grep -rn dump_payload_contract` found no `--check` anywhere. The only committed
copy was the launcher's vendored `tool/charsheet_payload_contract/`, which is
the hole: a hermes-side producer move leaves every hermes test green while that
copy lies, and the repo that did NOT move is the only one that can go red.

### Red-first

`--check` on the tree before the fixture existed:

```
$ python scripts/dump_payload_contract.py --check
PAYLOAD CONTRACT: the committed dump is MISSING at …/tests/fixtures/charsheet_payload_contract.json.
  Regenerate it:  python scripts/dump_payload_contract.py --write
-> exit 1
```

That is also the third test in the pin
(`test_the_check_reds_when_the_committed_dump_is_missing`): a gate whose input
has vanished must fail, never pass by absence.

### Why a BYTE comparison is the right check here

The dump is key paths only — never a value — even though the probes behind it
build a throwaway character library in a temp directory and carry temp paths,
`mtime_ns` revisions and generated draft ids. So the document is stable across
runs, machines and clocks, and two consecutive runs on this box came back
`cmp`-identical at 28,686 bytes before anything was committed. A fuzzy check
would be tolerating drift the artifact does not have.

The rendering is pinned in one place (`render()`) rather than at each call site,
for the reason `dump_cli_contract.py` pins its own: the artifact's whole value
is a byte comparison, against the previous commit's and against the launcher's
vendored copy.

### The shape is deliberately the argv lane's, not a new one

`--check` / `--write` / `--fixture`, the same `--hermes-root` semantics, the
same "read the diff before regenerating" refusal text. The one difference is
what the diff MEANS, and the refusal says so: for the CLI contract a removed
flag is an operator button that now exits 2; here a removed KEY is a launcher
reader left acting on a stale default, which is the worse half of the pair
(`4659127eba`'s `cardSafe` removal left every live crop read as unjudged, while
`34a8dad32e`'s added `handednessAccepted` merely threw).

### Where it runs

Not a CI job, because the CLI contract's gate is not one either: nothing in
`.github/workflows/` runs `dump_cli_contract.py`, and the repo has no pre-push
hooks. Its mechanism is a test inside the validated four-directory suite lane,
so the suite reaches it. This gate is the same:
`tests/hermes_cli/test_payload_contract_dump.py` (6 cases) sits in
`tests/hermes_cli/`, and AGENTS.md gains the gate's own section beside the CLI
dump's for the Lane A hand run.

### Measured

| command | result |
|---|---|
| `scripts/run_tests.sh tests/hermes_cli/test_payload_contract_dump.py` | 6 passed, 0 failed |
| `scripts/run_tests.sh tests/test_coverage_claims_resolve.py tests/test_line_endings.py tests/hermes_cli/test_charsheet_payload_contract.py tests/hermes_cli/test_cli_contract_dump.py` | 4 files, 23 passed, 0 failed |
| `scripts/doc_cite_adjacency.py --exclude archive --exclude planned` | exit 0 — 0 unwaived, 0 stale |
| `scripts/dump_cli_contract.py --check` | exit 0 — fresh, 191 command paths |
| `scripts/dump_payload_contract.py --check` | exit 0 — 4 kinds, 152 keys, sha256 `622681856d6c12e8` |

The fixture: 152 keys over 4 kinds (`list` 29, `sprite` 34, `status` 72,
`thumb` 17), LF, one trailing newline, sha256
`622681856d6c12e8e5c1c2ae701cb64b1c6caefaa14b2adf8d8dcce146833b21`.

### Owed, in the OTHER repo

The launcher's refresh should become a `cmp` against this fixture rather than a
re-derivation. The verbatim patch is in this lane's hand-back.

---

## 2. The 46 one-armed branches, triaged — 46 → 7 — `a0db90db91`

**Asked.** For each of the 46 one-armed branches
`scripts/unreachable_branch_report.py` found in `agent/charsheet`, name the case
that would reach it; where none can, delete the branch. Report the before/after
count.

### Re-measured, and the first run was not a measurement

The w15 numbers reproduce exactly at this base — 46 one-armed over a green
408-test suite, 235.6s at `-j 4`, module for module and arc for arc. But the
first run of this lane came back RED, and the reason is worth the paragraph:

```
310.08s  tests\agent\test_charsheet_pipeline.py
--- tests\agent\test_charsheet_pipeline.py ---
(timed out after 300s; process tree terminated)
=== 1 file where no tests ran ===
```

`scripts/run_tests_parallel.py` caps each FILE at 300s. That is generous for an
untraced file and not for a traced one: 110 pipeline tests under branch tracing
took 310s, were SIGKILL'd mid-collection, and the report then answered 81
one-armed branches instead of 46 — every number wrong, for a reason that has
nothing to do with branches. The script already raises the PER-TEST timeout for
exactly this hazard (`DEFAULT_TEST_TIMEOUT = 120` against the repo's
unconditional `--timeout=30`); it now raises the per-FILE cap the same way
(`DEFAULT_FILE_TIMEOUT = 1500`, with `--file-timeout` to override).

**And the first fix was wrong, which is the more useful half.** The obvious seam
is `HERMES_TEST_FILE_TIMEOUT`, which `run_tests_parallel.py`'s own argparse
reads as that flag's default. Setting it changes nothing: `run_tests.sh` execs
the runner under `env -i` and forwards exactly the variables it names, and that
one is not among them. So the variable is accepted, dropped at the fence, and
the run reds again with the 300s default — measured twice, once by hand and once
by the script. The cap rides the ARGV, where `"$@"` carries it through.
`tests/scripts/test_unreachable_branch_report.py` pins both facts, the second
one explicitly, because the failure is silent: a variable that is read on one
side of a fence and never crosses it looks exactly like a variable that works.

**Read this as the report's own caveat turned on itself:** a red suite makes the
report's numbers suspect, the report says so on every run, and the first two
things that made it red were the report's own defaults.

### The verdict table

Arc notation is the report's: `line→destination`, so `L→L+1` is a guard's TRUE
arm and a jump past the body is the FALSE one. "CASE" means a test in
`tests/agent/test_charsheet_branch_triage.py` now reaches it; the file is inside
the report's own `tests/agent/test_charsheet_*.py` glob on purpose, since a case
parked outside it closes a branch the report keeps reporting.

#### `agent/charsheet/palette.py` — 8 → 0

| arc | the predicate | verdict |
|---|---|---|
| `56→57` | `_as_rgba`'s path arm | CASE — every fixture passed images; `compose` passes the approved references off disk, which is the documented half of the contract |
| `75→76` | `max_colors` not an int (`bool` clause included) | CASE — `max_colors=True` would otherwise mean a one-colour palette |
| `77→78` | `max_colors` outside 2..256 | CASE |
| `81→82` | no source images at all | CASE — a scheme with an empty authored set hands this `[]` |
| `91→92` | `getcolors` answered `None` | **NO CASE EXISTS** — the cap is the pixel count, and an image cannot hold more distinct colours than pixels. Answered in the code with the `# pragma: no cover — unreachable by the line above` its identical twin in `palette_table` already carries, rather than deleting a raise whose absence would be a `TypeError` on `None` |
| `100→101` | no opaque pixels in the sources | CASE — a reference whose subject was keyed out entirely |
| `125→126` | `palette_colors`' mode guard | CASE — the caller's mistake is passing the reference IMAGE where the palette belongs, and Pillow's own answer for that is a silently empty list |
| `130→134` | `if used:` FALSE arm | **BRANCH DELETED.** w15 named it the clearest deletion candidate on the theory that `Image.quantize` always populates `.palette.colors`. Re-measured, the reason is different and stronger: `getpalette()` reads out of the SAME palette object, so bytes imply a non-empty `.colors` and no bytes imply an empty `triples` — the two arms returned the identical list for every reachable image and the false one only reached `[]` the long way round. `used` now defaults to `{}` and the filter is unconditional; both inputs are asserted |

#### `agent/charsheet/prompts.py` — 3 → 0, `spec.py` — 1 → 0

| arc | the predicate | verdict |
|---|---|---|
| `prompts:164→165` | a state token that cannot become a row key | CASE — the generic fallback below it is the common path, so the line between "no tuned language" and "not a token at all" had none |
| `prompts:231→232` | a direction with no camera-view language | CASE, through the public builder |
| `prompts:288→289` | a turnaround of zero directions | CASE |
| `spec:460→461` | `--directions` absent | CASE — turns an `AttributeError` inside `strip()` into a message naming the flag |

#### `agent/charsheet/revisions.py` — 1 → 0

| arc | the predicate | verdict |
|---|---|---|
| `111→112` | `keys()` with no root directory | CASE, **after the premise was corrected**: "before anything was written" does NOT reach it, because the constructor `mkdir`s the root. What reaches it is the root vanishing UNDER a live store — what `characters delete` does to a draft another frame still holds |

#### `agent/charsheet/pipeline.py` — 19 → 1

| arc | the predicate | verdict |
|---|---|---|
| `481→482` | `frames` not a positive int | CASE — `frames=True` would otherwise mean "one frame" |
| `492→493` | a strip too narrow to split | CASE — a model that answered with a thumbnail |
| `521→522` | `_column_centroid`'s empty alpha profile | CASE. Looked unreachable — `getbbox()` on RGBA is alpha-only, so a box implies alpha — and is not: the profile is the alpha channel resized to ONE row, and a subject surviving only at alpha 1 across a tall crop averages to zero under that downsample |
| `559→560` | body centroid absent | CASE, same input |
| `563→564` | head-band centroid absent | CASE, asserted separately with the body centroid proved non-`None`, or one "returns None" case could not tell the two guards apart |
| `637→639` | `chroma_key is not None` | CASE — the parameter DEFAULTS to `MAGENTA` and every fixture passed `None`, so the first step of the §F.2 looking procedure had never run |
| `673→674` | the padded square over the write budget | CASE — the budget is on the SQUARE, so an upstream check on the source cannot catch a wide short strip |
| `679→680` | an already-square crop | CASE — must not be composited onto the backdrop, or its transparent margins become QA ground |
| `695→696` | `VIEW_LANGUAGE` lost the front view | CASE by monkeypatch — the trap asserted before the repair, since the only cause is a future edit one module over |
| `752→753` | cutouts ≠ authored directions | **NAMED, NOT WRITTEN.** The case is a provider stub returning a strip whose width does not divide into the authored count; it belongs beside the other provider-stub cases in `tests/agent/test_charsheet_pipeline.py`, not in a triage file that builds no provider |
| `1131→1132` | a seam between rows with no frames | CASE |
| `1301→1302` | a GAP between over-threshold rows | CASE — every fixture's flagged rows were contiguous, so the run was only ever closed by the end of the list. Asserted against its opposite, because the arm means nothing alone: two adjacent flagged rows raised each other and are one unattributable finding, two isolated ones are each named |
| `2227→2229` | the summary SKIPS the unjudged clause | CASE. Aimed at the wrong arm first and the report caught it: the one-armed side is the FALSE one — no fixture had ever produced a summary for a sheet the check answered completely |
| `2385→2386` | sheet median height under the collapse floor | CASE, on a PAINTED sheet |
| `2392→2393` | a row carrying one box | CASE, same sheet — a one-element sample has no median to compare against itself |
| `2398→2399` | a multi-pose frame outlier | CASE, on a painted sheet with one very wide cell |
| `2400→2391` | `if global_med_w and global_med_h:` FALSE arm | **BRANCH DELETED.** Reaching that line means `boxes_by_row` holds a row with ≥2 boxes; every box is a `getbbox()` and spans ≥1 pixel; and a non-empty `boxes_by_row` is exactly what makes `all_widths`/`all_heights` non-empty, which is what assigns both medians. Both are ≥1 there, and the zeroes they are initialised to are only read by the check ABOVE the loop |
| `2401→2404` (now `2409→2412`) | a row collapsed against the sheet median | CASE, same painted sheet — renumbered by the deletion above |
| `2431→2432` | a blank `--accept-handedness` token | CASE — `--accept-handedness ""` and a trailing comma both arrive as an empty string, which would otherwise be reported as an acceptance of a row that does not exist |

The sheets are PAINTED, not composed: the geometry checks read nothing but each
cell's bounding box, so a rectangle per frame is the whole input they need and a
provider run would only make the case slower and harder to read.

#### `agent/charsheet/draft.py` — 12 → 4

| arc | the predicate | verdict |
|---|---|---|
| `192→193`, `213→214`, `637→638` | three readers' `not isinstance(data, dict)` arms | CASE — one fact about one file (a `draft.json` holding a JSON list parses fine and then answers `.get` with an `AttributeError` several frames away), three different right answers: refuse to write, fall back to the leaf name, raise "corrupt" naming the path |
| `297→298` | a drafts child that is not a draft | CASE — a loose file beside the draft directories, and a directory with no `draft.json` (an interrupted create). Neither swept along, neither aborting the entries around it |
| `330→331` | an installed manifest that is not an object | CASE — still an installed character by the definition the CLI uses, so it must MOVE with the slug falling back to the directory name |
| `828→829` | `_set_stage`'s vocabulary guard | CASE — asserted with the file too, because the point is that it raises BEFORE `_save` writes a stage nothing can advance from |
| `1985→1986` | `handednessAccepted` is not a list | CASE |
| `1989→1998` | the ROUND-TWO bare-row-key spelling | CASE, **after the premise was corrected**: those entries are NORMALISED, not dropped — the row survives and the two facts round two never recorded are spelled `gain: 0.0, basis: "unrecorded"` |
| `1100→1101` | a row whose approved direction reference is missing | **NAMED, NOT WRITTEN** — needs a staged draft with a revision store; belongs in `tests/agent/test_charsheet_draft.py` beside its fixtures |
| `1355→1356` | a row attempt with no image on disk | NAMED — the case is an approved attempt whose file was deleted under the draft |
| `1433→1434` | a direction attempt with no image on disk | NAMED — same shape, the direction arm |
| `1627→1628` | compose with no approved reference to take the palette from | NAMED — a draft advanced to compose with one direction unapproved |

#### `agent/charsheet/draft_lock.py` — 2 → 2

| arc | the predicate | verdict |
|---|---|---|
| `189→191` | the re-entrant exit finds no registry entry | **NAMED, NOT WRITTEN** — a genuine race: the registry entry is dropped between the reentrant `yield` and its `finally`. A unit test can only fake it by reaching into `_REGISTRY`, which asserts the fake and not the race |
| `221→222` | a stale lock is broken and another writer takes it first | NAMED — two processes breaking the same stale lock. Reachable only with a second real process; the module's existing tests already spawn threads for the busy case, and this one wants processes |

### Before and after

```
$ python scripts/unreachable_branch_report.py -j 4     # before
6 files, 408 tests passed, 0 failed in 235.6s (4 workers) — verdict exit 0
   pipeline 19 | draft 12 | palette 8 | prompts 3 | draft_lock 2 | revisions 1 | spec 1   = 46

$ python scripts/unreachable_branch_report.py -j 4     # after
7 files, 463 tests passed, 0 failed in 328.6s (4 workers) — verdict exit 0
   draft 4 | draft_lock 2 | pipeline 1 | palette 0 | prompts 0 | revisions 0 | spec 0     = 7
```

**46 -> 7.** Two branches deleted, one answered with a pragma naming why nothing
can reach it, 36 given a case, and 7 named with the case that would reach them
and the reason this lane did not write it (four want a staged draft's fixtures,
two want a second process, one wants a provider stub, and each belongs in the
suite that already has that machinery rather than in a triage file that would
have to grow its own).

`agent/charsheet/palette.py`, `prompts.py`, `spec.py` and `revisions.py` now
report zero one-armed branches AND zero cold statements. `pipeline.py` is down
to one of each.

Cost, for whoever runs it next: the after-run is 7 files rather than 6 — the new
triage file is inside the report's glob, which is what the glob is for.

### What is left

* The seven named-but-unwritten arcs above, each with the suite it belongs in.
* The report is still a REPORT: nothing consumes its exit code, and this lane
  did not change that.
* `agent/charsheet/pipeline.py` and `draft.py` still carry COLD lines (whole
  functions this suite never calls). Cold is a different finding from one-armed
  and this row was about the second; nobody has triaged the first.
