# Cite-adjacency probe — field notes, 2026-09-01

The record for the stage that built the second half of the doc-cite pair and
spent its first sweep budget. Companion to
[dcw-h3-field-notes-2026-08-31.md](dcw-h3-field-notes-2026-08-31.md), which
built the first half (the RESOLUTION report) and closed by saying the adjacency
probe was the harder half and still unbuilt.

Commits: `cd9568ec11` (the sweep), `8385fc4693` (the probe, its tests, its
baseline). Cut from `origin/main` at `16fe90fb6a`, rebased onto `d6055dab83`
before landing — the shas above are the LANDED ones, re-read after the rebase
rather than copied from the pre-rebase run, which is the round-five trap the
resolution report checks for.

## The two rows this answers

- *"The hermes canon's line cites have rotted at scale"* — the AX2/Z1 row, which
  asked for a gate with a sweep budget because one that lands red on ~60
  pre-existing cites gets silenced.
- *"Four hermes doc line-cites point at unrelated text, and did so BEFORE this
  wave"* — the H-H row, whose closing sentence was "the ±3-line adjacency probe
  that would catch these four is the unbuilt half".

They are the same class and are answered by one probe.

## What was re-measured, and why the morning's number moved

The row carried "61 of 91 machine-checkable cites fail". That was a hand
measurement with a narrower checkability rule. Re-measured mechanically against
fresh `origin/main`, after the canon-05 profile-timing fold (`c1de3b2d84`) and
the day's other landings:

| | live canon | whole root (incl. `planned/`) |
|---|---|---|
| docs walked | 28 | 75 |
| Python cites seen | 326 | 620 |
| machine-checkable | 249 | 484 |
| passed (adjacent) | 132 | 232 |
| passed (inside the named symbol) | 4 | 11 |
| **FAILED** | **113** | **241** |
| unchecked — sentence names nothing in the file | 42 | 85 |
| unchecked — ambiguous bare name | 35 | 51 |

After the sweep the live canon reads **66 failed / 249 checkable**, all 66
baselined with written reasons.

The failure count is HIGHER than the row's 61 for two reasons, both worth
recording: the probe reads more cites than the hand pass did, and narrowing the
subject scope from the paragraph to the SENTENCE (below) removed cites that had
been passing on a neighbouring sentence's symbol — 99 → 113 on that change
alone, in the same pass that removed two false findings.

## The sentence scope, which was measured rather than assumed

First cut took subjects from the whole paragraph. Two false findings came
straight back:

- `realm_membership.py:1-12`, cited for **fail closed**, was reported rotted
  because the sentence BEFORE it named `RealmStore` and `WorkspaceStore` — real
  symbols in that file, twenty lines below the cited docstring.
- `board_store.py:8-15`, citing a quoted invariant, was reported rotted by the
  sentence AFTER it naming `Board` / `BoardColumn` / `BoardCard`.

Lending a neighbour's subject to a cite is inventing a finding, so the scope is
the sentence the cite sits in, capped by character bounds for prose that never
punctuates. Both cases are pinned as tests.

## The sweep — 47 re-anchored by hand

Selected by "the sentence's nearest preceding backticked identifier has exactly
one `def`/`class` in the cited file", then read one by one. Worked examples:

| doc said | actually at |
|---|---|
| `_cmd_mission_chat_message` at `persona_commands.py:2034` | 2614 |
| its exec-load at `harness.py:4419` | 6004 |
| its argparse wiring at `harness.py:1124` | 1365 |
| `dispatch_argv` at `serve.py:937` | 1464 |
| doc 04's whole boot-stamp block, `serve.py:1149-1218` | 1794–1941 |
| `_cmd_serve` starts its `BootTimeline` at `serve.py:3251-3257` | 4739–4741 |

Eight bare `:N` continuation cites in the same sentences were re-anchored with
them (`_cmd_characters_auto` at `:4776` → 4652 and friends).

**Not re-anchored, deliberately:** the doc 01 paragraph that QUOTES
`office_store.py:113` as an example of a cite that drifted. It is in the
baseline as `quoted rot`; re-pointing it would falsify the record it exists to
keep.

## The baseline, and what is honestly in it

66 keys, each with a reason, in six classes:

| class | n | what it means |
|---|---|---|
| unpinned | 25 | the sentence names no symbol that pins one line — any new number is a guess |
| table | 16 | a table row naming which module owns a directory or log line |
| otherfile | 13 | a paired cite whose named symbol belongs to the OTHER file |
| docstring | 9 | the cite points at a docstring/contract range; the named class is defined below the window |
| reanchored | 2 | fixed in this sweep; still reported because the subject is only named in the module docstring |
| quoted | 1 | quoted rot, above |

**Only the first two classes are unambiguously the docs' problem.** The
`otherfile` and `docstring` classes are the probe's own blind spots, and the
reason strings say so — they are rows to answer with a better rule, not with a
new number. Saying that plainly is the point: a baseline whose reasons all read
"pre-existing" is a suppression list.

The ratchet is what makes the cap safe. The gate is red on any failure not in
the baseline AND on any baseline key that has stopped failing, so a waiver
cannot outlive its rot and the file cannot quietly stop shrinking.

## Scope: `planned/` and `archive/` are out, and that is a ruling

A dated plan or field-notes doc is a record of what was true on its date. Its
cites rot by construction as the code moves, and re-anchoring them would
falsify the record. Gating them would put the canon on a treadmill that ends in
the gate being turned off — the exact failure the resolution half stayed
advisory to avoid. `--exclude` is a flag, not a constant, so the wider walk
(194 failures after this sweep) is one command away for anyone who wants the
whole picture.

## Red-first proof

Three sabotage round-trips, each applied and reverted, tree verified clean
after every one:

1. **A fabricated cite at a wrong line.** `persona_commands.py:2614` → `:99` in
   doc 01. Gate exit 1, naming it: `01-system-architecture.md:21
   hermes_cli/harness_parts/persona_commands.py:99 … names:
   _cmd_mission_chat_message, harness`. Reverted → exit 0.
2. **`verdict` never returns FAILED.** Six tests red, including the wrong-line
   case and both gate-direction tests. Reverted → 18 passed.
3. **`subject_window` widened back to the whole paragraph.** Three tests red,
   including the neighbouring-sentence case. Reverted → 18 passed.

A zero-cite walk was proven FATAL against a real doc root that carries no Python
cites (`upstream-prs/`), not a stubbed walk.

## Residuals, filed rather than fixed

**RESOLVED 2026-09-02** — the first two are built; see the dated section at
the end of this file for the census, the ceiling table, and the re-baseline.
The rest stand.

- **Bare `:N` continuation cites are invisible to the probe.** The canon writes
  `` `harness.py:1873`, `_cmd_characters_auto` at `:4776` `` constantly, and the
  second half carries no path. Eight were found and fixed only because they sat
  in sentences the probe had already flagged. Resolving them against the nearest
  preceding path cite is buildable and is the next increment.
- **`planned/` carries 128 failures of its own** (294 cites, 235 checkable),
  which is why the whole-root walk reads 194 after the sweep where the gated
  canon reads 66. Out of gate scope by the ruling above; the number is here so
  nobody re-derives it.
- **A weak identifier can pass a cite by coincidence.** Within minutes of
  landing, three commits between `16fe90fb6a` and `d6055dab83` moved
  `hermes_constants.py`'s step constant 1481 -> 1533 (the gate caught it, red,
  and it was re-anchored) and simultaneously made
  `07-observability.md|persona_commands.py:3522` PASS — not because
  `slim_chat_final_observability` moved to 3522, it is at 127/4221/4494, but
  because the same sentence also backticks `show`, `final` and `chat`, and one
  of those landed in the window. The subject rule needs an occurrence ceiling:
  an identifier appearing all over a 5000-line file is not a locator. That is
  the second increment, and it will re-shuffle the baseline when it lands.
- **The gate is worth exactly one push-cycle of latency.** The same rebase that
  produced the false pass above produced a genuine red, on a cite this stage had
  itself just re-anchored, from three unrelated commits landing in between. Line
  cites into live modules rot on a timescale of hours here, which is the case
  for naming symbols rather than lines and for keeping this gate on.

- **Ambiguous bare names (35 live, 51 whole-root) are counted, never guessed.**
  Same refusal as the resolution report. Spelling a cite as `agent_runtime/
  models.py` instead of `models.py` moves it into scope for free.

---

# The residuals, answered — 2026-09-02

The two residuals filed above are built, and the third measurement in that list
(the whole-root `planned/` number) is unchanged and still out of scope. Cut from
`origin/main` at `b9e7a27988`.

## Residual 1: bare `:N` continuations are no longer invisible

The census first, because it decides how much this was worth: the gated canon
carries **299 bare `:N` cites against 326 path cites**. Nearly half of every line
number in the canon was ungated, and the eight fixed in the 2026-09-01 sweep were
found only because they shared a sentence with a cite the probe had already
flagged.

`CONTINUATION` reads the token (the whole backtick must BE `:N` / `:N-M`, a
trailing `+` allowed exactly as `CITE` allows it), `continued_path` resolves the
path it wears, and `ContinuedCite` presents it to `verdict` through the slice of
the `re.Match` API that rule reads — so a continuation is judged by the same
rule, with its OWN subject window, and lands in the same baseline key shape.

**Two scope choices, both measured rather than argued.**

*The sentence, not the paragraph.* Where both scopes resolve they never disagree
— 56 of 56. The paragraph would resolve 76 more, and the ones read by hand it
gets WRONG: 01's "`realm_sync.py`: pull applies the ledger
(`_apply_skill_tombstones`, `:613`)" would inherit *store.py* from a cite two
sentences up, and 01's `_mission_chat_user_message` pair would inherit
*models.py*. A wrong path is a fabricated finding; a refusal is only a missed
one, and this canon already counts its refusals rather than guessing.

*A path MENTION, not a path cite.* 03 writes "`hermes_cli/harness.py:1693`,
parsed by `agent_runtime/patch_coverage.py::parse_fold_entities_option`,
`:404`". The `::` form carries no line, so a rule reading only `CITE` skips it
and hands `:404` to *harness.py* — a fabricated finding produced by the very
feature meant to catch real ones. This was caught by reading the first run's
output, not by a test, which is the argument for reading a new gate's findings
before believing its count.

56 → 78 continuations resolved on that change. 221 are still refused for having
no path mention in their sentence; the cheapest fix is on the writing side (name
the file in the sentence), and widening to the paragraph is NOT the answer, for
the reason measured above.

## Residual 2: the occurrence ceiling, and the number the corpus picked

`MAX_SUBJECT_OCCURRENCES = 20`. A subject occurring more than that many times,
whole-word, in the cited file is dropped — it answers "is this file about that",
never "is this LINE". Dropping can only make a cite UNCHECKED or FAILED, so the
rule cannot turn a red cite green; that direction is pinned by test.

**The sweep.** Over the gated canon, with continuations on, at each candidate:

| ceiling | checked | adjacent | in-symbol | FAILED | unchecked (no subject) | pass→FAILED vs off | pass→unchecked | FAILED→unchecked |
|---|---|---|---|---|---|---|---|---|
| 8 | 287 | 152 | 9 | 126 | 74 | 46 | 14 | 8 |
| 12 | 295 | 164 | 8 | 123 | 66 | 39 | 10 | 4 |
| 16 | 298 | 173 | 8 | 117 | 63 | 33 | 7 | 4 |
| **20** | **300** | **179** | **8** | **113** | **61** | **28** | **6** | **3** |
| 40 | 304 | 193 | 8 | 103 | 57 | 16 | 4 | 1 |
| off | 309 | 215 | 6 | 88 | 52 | — | — | — |

(The plain ceiling, before the defined-symbol exemption below.)

**What the table does NOT decide, and what does.** Every candidate flips the
measured coincidence at 07's `persona_commands.py:3522`, so "does it catch 3522"
chooses nothing. What chooses is the cites each end gets wrong, read one by one:

- **40 is too loose.** 01's `harness.py:616` — cited for `realm sync revert`,
  and the line is the `resolve` parser — and 01's `hermes_cli/harness.py:1343` —
  cited for `install-harness-skills`, and the line is a `--max-seconds` argument
  — are real rot, and at 40 both keep passing on `realm` / `sync` / `install`.
- **12 and 8 are too tight.** They refuse `board_id` in `board_tool.py` (13-20
  occurrences), whose cite lands exactly on `_resolve_board_target`, and report
  a correct cite as rot.

20 is the only candidate that catches every confirmed rot and invents no
finding. 16 was swept because it sits inside the same gap; 20 is the top of it,
and a ceiling belongs at the loose end of its safe range.

**The exemption, which the reading forced.** A plain ceiling at 20 still refused
`store_root` in `paths.py` and reported `paths.py:8` — which IS `def
store_root()` — as rot. A name the file DEFINES pins a line by construction
however often it is then called, so `Target.defines` exempts `def`/`class` names
from the ceiling, on the same AST the `in-symbol` verdict already rests on.
Without it the rule invents findings in order to stop inventing findings.
`StoreDriftItem` (01's `realm_sync.py:1315`) and `store_root` are the two it
saves; `board_id` is not a def and is saved by the 20 instead.

## The re-baseline

64 waived → **91**: 29 added, 2 stale deleted, and 17 cites re-anchored in the
canon rather than waived. Lane A green, exit 0.

Re-anchored (symbol unmoved, number refreshed):

| doc | was | now | the symbol |
|---|---|---|---|
| 01 | `:613` | `:712` | `_apply_skill_tombstones` |
| 01 | `:898` | `:1048` | `_skill_artifacts` |
| 01 | `harness.py:616` | `:624-631` | the `revert` parser |
| 01 | `harness.py:1343` | `:1663` | the `install-harness-skills` parser |
| 03 | `:841` | `:1102` | `stream_frames` |
| 03 | `:404` | `:480` | `parse_fold_entities_option` |
| 03, 04 | `:1954` | `:3039` | `_room_wants_stale_first` |
| 04 | `:5119` | `:5120` | `_await_bytecode_sweep_winner` |
| 04 | `:707`, `:733`, `:782` | `:919`, `:945`, `:1003` | `demote_core_reuse`'s three uses |
| 04 | `serve.py:289` | `:327` | `OPS_STDIO_ONLY` |
| 05 | `:6409`, `:6863`, `:2734` | `:6416`, `:6870`, `:3409` | the chat-model override |
| 07 | `snapshot.py:403-411` (fn `:373`, call `:687`) | `:398-408` (fn `:369`, call `:683`) | `_log_snapshot_build_core` |
| 07 | `harness.py:1935` | `:835-841` | the `prompt-context show` parser |
| 07 | `persona_commands.py:3522` | `:4495` | `slim_chat_final_observability` |
| 07 | `:424-452` | `:554-581` | `batch_carries_patch_rows`'s argument |
| 07 | `harness.py:3704-3721`, `:3952-3955` | `:5247-5265`, `:5485-5497` | `_usage_lane_detected` |
| ops | `harness.py:1168` | `:1215` | the `retire`/`delete` alias parser |

Two of those were found by the continuation rule and could not have been found
without it (01's `:613` and `:898`), and two more by the ceiling (01's
`harness.py:616` and `:1343`). That is the wave paying for itself in the same
commit that builds it.

The 29 added waivers carry five classes, and the split is the honest half of the
number: **9 are read-and-confirmed CORRECT cites the probe cannot vouch for** —
`BACKTICK-SPAN NOISE`, where the "identifiers" are English words a mis-paired
backtick span produced, and `RECEIPT-KEY SUBJECT`, where the sentence's subjects
are JSON keys that live at the emitter rather than at the constant. 7 are
`TABLE ROW`, 9 `PAIRED CITE`, 3 `CONTRACT/DOCSTRING RANGE`, 1 has no symbol left
after the ceiling. Only the last class is unambiguously the docs' problem; the
rest are rows for a better rule, said plainly for the same reason the first
sweep said it.

The largest of those rules is now visible and was not before: `BACKTICKED` pairs
backticks greedily across prose, so a line with an odd count hands `subjects()`
a span of ordinary English — `advanced`, `also`, `having`, `honest`, `could`,
`absent`. That is not a frequency problem and the ceiling cannot answer it.

## Residuals still open

- **221 continuations resolve to no path** because their sentence names no file.
  Many are table rows and bullet lists whose file is named in the row above.
- **`BACKTICKED` mis-pairs across prose**, above — the single largest source of
  junk subjects.
- **`--exclude planned/` still hides these field notes' own rot**, by the 2026-09-01
  ruling. The whole-root walk remains one command away.
