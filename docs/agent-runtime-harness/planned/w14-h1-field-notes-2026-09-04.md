# w14/h1 — field notes, 2026-09-04

Seven rows, all hermes-side, worked in the `w14-h1` worktree on branch `w14/h1`
off hermes main `3d3a33be3e`. Three carried operator rulings from the evening
sheet; four were unruled weakness/recurrence rows to be re-measured first.

Every row below records what it asked, what was measured before anything was
touched, what changed, and the commands with their exit codes.

---

## Row 1 — a turnaround DIRECTION reference had no crop verdict

**Asked (RULED, option C falling back to A):** grow a crop lane for direction
references — the card was drawing a tile through the whole turnaround stage.
Next: add the direction/item arm to `_cmd_characters_thumb` + `CharacterDraft`,
register it in `charsheet_payload_contract.py::_thumb_kind`, bind the launcher
card's turnaround arm to it.

**Measured first.** `characters thumb` took `--row` as a REQUIRED argument and
had no other way to name an item, so the premise held exactly as written. The
ruling's arithmetic also held: `generate_direction_view` draws on a square
canvas, and a 384-square reference at the default `--scale 2` is 589,824 px
against a 3,194,880-px console ceiling — the tile was never protecting the
console from anything.

**Built.** `CharacterDraft.direction_thumb`, and the shared tail both kinds now
run through (`_finish_thumb`: both bounds, the refusal, the two backdrops, the
pad order, the write). `pipeline.reference_cell` is `frame_cell`'s "nothing to
slice" counterpart, so pixels are still decoded only in the module that owns
them. The CLI arm is `--direction`, in a required mutually-exclusive group with
`--row`; `--frame` asked of a direction is REFUSED, not silently answered with
the whole picture. The payload carries `direction` and omits `row`/`frame`/
`frames` rather than faking `0 of 1`.

**Which option this is.** C was taken as far as the ruling's own fallback
allows: the arm is keyed by DIRECTION, not by a raw revision-store item key.
The item-key surface would have put `turnaround@e` on the command line and in a
cross-repo caller's hands, and the launcher does not hold one — its card holds
`declaration.bareItemKey` plus a typed `MissionSheetQaItemKind`, which is a
direction and a kind, exactly what `--direction` takes. Judged too wide,
per the ruling's clause.

**Red-first.** Six draft-level tests written against a method that did not
exist: `AttributeError: 'CharacterDraft' object has no attribute
'direction_thumb'` (6 failed), then green.

| command | result |
| --- | --- |
| `scripts/run_tests.sh tests/agent/test_charsheet_draft.py` | 141 passed |
| `scripts/run_tests.sh tests/hermes_cli/test_harness_characters_cli.py` | 104 passed |
| `scripts/run_tests.sh tests/hermes_cli/test_charsheet_payload_contract.py` | 9 passed |
| `python scripts/dump_cli_contract.py --write` then `--check` | exit 0 |
| `python scripts/doc_cite_adjacency.py --exclude archive --exclude planned` | exit 0 (after re-anchoring, below) |

Commit `9bcd454173`.

**One trap for the next lane.** Every `harness.py` insertion shifts the line
numbers four docs cite. Both commits that touched `harness.py` failed the cite
probe until the cites were re-anchored (`01-system-architecture.md` ×2,
`07-observability.md` ×3, twice over). The probe names the failures precisely;
compute the shift from `git diff -U0` hunk headers and move the numbers.

---

## Row 2 — approving a turnaround certified nothing about the direction

**Asked (RULED, option 1):** compute the reference's face offset in
`approve_direction`, surface it in the payload and the CLI receipt, and add a
test that a reference disagreeing with its rows is visible at approve time.
Option 2 (a stage-regression verb) is separate later work.

**Measured first.** The permanence the row complains about is real at HEAD:
`reroll_direction` requires stage `turnaround` and `CharacterDraft.reopen` only
walks `composed` back to `rows`, so a shipped draft cannot return. And the
quantity the 2026-08-25 notes quote by hand (`face offset −44.8`) exists nowhere
in the tree — `grep -rn "face_offset\|face offset"` over `*.py`/`*.md` hits
exactly one line, in `FIELD-NOTES.md` prose. Nothing computed it.

**Built.** `pipeline.face_offset`: the alpha-weighted horizontal centroid of the
top `FACE_BAND` (25%) of the subject's own bbox, minus the centroid of the whole
subject, after the chroma field is keyed out. Signed — positive is a head to the
right of frame — and `None`, never `0.0`, for a picture with nothing on it. Both
centroids come off a one-row resize (the column-profile trick `normalize_cells`
registers with), so it is O(width) rather than O(pixels).

It rides on BOTH approval arms. `--all` is the path `auto` takes and the path
the anime-girl draft was approved through; a measurement absent from the
unattended path would be absent from exactly the population nobody watches.

**A branch deleted rather than left untested.** The first draft of
`_face_offset` guarded "the approved attempt has no image on disk" — and the
store's own `approve` refuses that attempt before the guard can run, which the
test proved by failing with `attempt 0 of 'turnaround@e' has no image on disk`.
The guard is gone; the reachable `None` is a reference with nothing drawn on it
(a bare chroma field), and that is what the test now uses.

**Red-first.** Five pipeline tests against a missing function
(`AttributeError: module 'agent.charsheet.pipeline' has no attribute
'face_offset'`), then green.

| command | result |
| --- | --- |
| `scripts/run_tests.sh tests/agent/test_charsheet_pipeline.py` | 103 passed |
| `scripts/run_tests.sh` over the four charsheet/CLI files | 362 passed |
| `python scripts/dump_cli_contract.py --check` | exit 0 |

Commit `6428616daf`.

---

## Row 3 — `status` and `list` were outside the vendored payload contract

**Asked (RULED, option 1):** add `_status_kind` / `_list_kind` with dynamic-key
placeholders, re-dump the fixture, widen the launcher's gate.

**Measured first.** `build_payload_contract` emitted exactly `sprite` and
`thumb`, and `_agreed_shape` DROPPED every child of a map measured to be keyed
by data. That is why the two were left out and not an oversight: every QA item
in `status` hangs off `turnaround` (keyed by direction) or `rows` (keyed by row
key), so the old rule dropped the item record whole.

**Built.** `DYNAMIC_KEY` (`{}`), the map-level counterpart of the `[]` a list's
elements already collapse to, applied in a second walk once the probes have
disagreed. `status` is 71 keys under `status`, `list` is 28 in the envelope, and
`hermesHome` — the field the row went looking for — is in both.

**One deviation from the ruling's letter, and why.** The ruling names
`{direction}` / `{row}` placeholders; the placeholder that shipped is `{}` for
every data-keyed map. WHICH vocabulary a map is keyed by is not something this
module measures — it measures only that the keys moved when the sheet vocabulary
moved — so a named placeholder would have to be hand-declared per path, which is
the one thing this module refuses to do anywhere else ("nothing declares which
maps are dynamic; the disagreement is the measurement"). A declared name is also
free to be WRONG about a path that later stops being dynamic. The generalisation
the ruling describes is intact; only the spelling is generic.

`sprite` gains exactly two keys as a consequence (`framesByRow.{}` and
`directions.mirrored.{}`); `thumb` gains none.

| command | result |
| --- | --- |
| `scripts/run_tests.sh tests/hermes_cli/test_charsheet_payload_contract.py` | 11 passed |
| `scripts/run_tests.sh tests/agent_runtime/test_persona_skill_policy.py` | green (verb list unchanged) |

Commit `a3ceb5dfe9`.

---

## Row 4 — no admission check exists for a NEW payload flag

**Asked (narrowed 2026-09-02):** the two instances are repaired, the CLASS is
not — nothing anywhere admits a new payload boolean, so the next one ships on
the author's memory.

**Measured first.** Confirmed: `grep` finds no test, script or gate over payload
booleans; the prescribed shape (`fits_console_budget` / `fits_own_sheet`, both
exported, disagreement pinned in both directions) is live for the split pair and
for nothing else.

**Built.** `tests/hermes_cli/test_charsheet_payload_flag_admission.py`, split
along what each side is good at:

- the POPULATION is measured — `build_flag_inventory()` re-reads the same probe
  run the contract dump comes from and collects every boolean the four read
  payloads actually print (twelve today), so a flag cannot fail to reach the
  test;
- the ADMISSION is declared, because it is a judgement: `Guarantee` (naming the
  pure predicate and a test where the flag DISAGREES with its neighbour) or
  `Data` (saying why there is nothing to drift). Both directions red: an
  unadmitted flag, and a stale entry for a flag nobody publishes.

Two further arms check that a `Guarantee`'s predicate imports and is callable,
and that its disagreement test resolves by AST (not by substring — a name in a
docstring is not a test that exists).

**Proof it detects.** Planted `plantedFlagBySuite: True` in `row_thumb`:
`AssertionError: unadmitted flag(s): ['thumb.plantedFlagBySuite']`. Reverted:
4 passed.

The probe machinery was refactored so ONE run answers both readers (`_probe`
builds the temp library, redirects `HERMES_SHARED_CHARACTERS` and restores it in
one place — a second copy of that dance is a second chance to probe an
operator's real characters).

Commit `1b9f748671`.

---

## Row 5 — two bases can be fooled by ONE displacement

**Asked:** a weakness, argued from the shared registration. The 2026-09-02 sweep
struck the row's own number ("a CORRECT `walk-e` slid −32 px reads `rotation and
states` and blocks") as never having had a fixture, and told the next reader not
to re-quote it.

**Re-measured, and the struck number is TRUE.** Swept slides of the
shipped-correct art through `detect_mirrored_art`:

| slide of `walk-e` | reading |
| --- | --- |
| −16 px | nothing flagged |
| −24 px | `walk-ne` (a NEIGHBOUR), basis `rotation`, warning, +10.7% |
| **−32 px** | **`walk-e`, basis `rotation and states`, attribution `both`, severity `error`, +18.2%** |
| −48 px | nothing flagged (on `idle-e`: `contradicted`, warning) |
| −64 px and past | nothing flagged |

The sweep was struck for having no fixture behind it; it had no fixture, and it
was right. It has one now: a pure displacement of correct art, twice the 16 px
registration window, reaches the tier that REFUSES an install, while the unslid
sheet flags nothing. The band being narrow is not reassurance — an operator does
not choose how far a prop hangs off one row.

The test also exercises `--accept-handedness walk-e:<token>` clearing it, which
is the row's own point about that door.

Commit `0fed4fc503`.

---

## Row 6 — "if `normalize_cells` ever stops registering" had no detector

**Asked:** the cheap pin the round-two review prescribed was written, measured
and CANNOT see the centring removed, because `extract_strip_frames`
content-crops each frame per slot BEFORE the centring runs.

**Re-measured.** The row's diagnosis of why the composition-path test is blind
is correct and unchanged at HEAD. What was wrong was the conclusion drawn from
it: the detector was being looked for on the wrong path. Against the FUNCTION it
is one assertion.

**Built.** `test_normalize_cells_still_registers_every_state_on_the_cell_s_centre`
hands `atlas.normalize_cells` two states drawn hard against opposite edges of
their own canvases and requires both to land centred in the same 192 px cell.
Removing the centring (`px = 0`) reds it: `idle is not centred in its cell: 0
left, 102 right`.

**Where it lives, and why not beside the function.** `tests/agent/
test_pet_generate.py` is opt-in — its whole file skips without
`HERMES_RUN_SLOW_PET_TESTS=1` (measured: `2 skipped, 28 deselected`), so a pin
placed there would not run in the sweep that deleted the line it protects. It
sits in the charsheet suite, which is what the removal actually breaks.

Commit `c9809bed31`.

---

## Row 7 — the too-weak-fixture CLASS (four recurrences in one module)

**Re-measured, nothing built, and the reason is recorded rather than assumed.**

The class's three standing answers are written down where the next author of
this module reads (`FIELD-NOTES.md`, the "Two knobs were pinned by nothing"
entry): assert the trap before the repair; delete a branch no fixture can reach;
express a masking branch off the same knob it masks. They are applied, not
merely stated — this lane applied two of them without planning to: row 5 asserts
the trap (a displacement reaching the ERROR tier) before anything argues about
it, and row 2 DELETED an unreachable guard the moment a fixture could not reach
it (the store refuses the input the branch was written for).

What would close the class as a mechanism is the sabotage exercise made
runnable: mutate a declared list of constants and require the suite to red. It
is not a row — it is a program, and the declared list is itself a hand-written
list free to rot, which is the shape the class is about.

The cheaper half — "is there an unreachable branch in this module TODAY" — could
not be measured here: `coverage` is not installed in the canonical test env
(`import coverage` → ModuleNotFoundError) and installing into it is out of
bounds for a lane. Stdlib `trace` would run the 200-second charsheet suite at
roughly ten times that. Whether to add a coverage dependency is an operator
call, and it is the one thing that would turn this row into work.

Row handed back KEEP, with the above as its measurement.

---

## A red that is not mine

`tests/test_coverage_claims_resolve.py::test_every_coverage_claim_names_a_test_that_exists`
fails on `origin/main` at `3d3a33be3e`, before this lane's first commit:
`docs/agent-runtime-harness/planned/w13-h1-field-notes-2026-09-04.md:246` cites
`test_coverage_claims_resolve.py::test_a_cleared_binding_is_not_stale_because_its_own_event_demotes_the_batch`,
which that file does not define. It is a previous lane's field note citing a
test into the wrong file. Recorded, not fixed — every other run of that gate in
this lane reported the same single failure and nothing else.
