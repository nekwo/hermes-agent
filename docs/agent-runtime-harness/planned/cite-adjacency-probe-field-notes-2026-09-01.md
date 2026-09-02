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

- **Bare `:N` continuation cites are invisible to the probe.** The canon writes
  `` `harness.py:1873`, `_cmd_characters_auto` at `:4776` `` constantly, and the
  second half carries no path. Eight were found and fixed only because they sat
  in sentences the probe had already flagged. Resolving them against the nearest
  preceding path cite is buildable and is the next increment.
- **`planned/` carries 128 failures of its own** (294 cites, 235 checkable),
  which is why the whole-root walk reads 194 after the sweep where the gated
  canon reads 66. Out of gate scope by the ruling above; the number is here so
  nobody re-derives it.
- **Ambiguous bare names (35 live, 51 whole-root) are counted, never guessed.**
  Same refusal as the resolution report. Spelling a cite as `agent_runtime/
  models.py` instead of `models.py` moves it into scope for free.
