# W1-H3 field notes — mutation-gate hygiene batch (2026-08-31)

Running record for the decision-close wave's hermes mutation/gate stage.
Branch `feat/dcw-h3-mutation-hygiene`, cut from `origin/main` at `0c744aa586`.
Every row below was re-measured before it was built, per the wave's discipline;
where the queue row was wrong about its own mechanism, that is recorded rather
than quietly corrected.

## What was measured before anything was written

| row | claim as filed | what re-measuring said |
|---|---|---|
| claim-to-symbol index | "a claim-to-symbol index would have made this a pre-flight check" | Correct and unbuilt. 113 claims, 22 distinct paths; the only way to answer "which claims anchor here" was reading the registry. |
| selection-side AST anchoring | "H-H2 rewrote `perform_agent_create`'s reply construction … the claim was not selected" | The MISS is real and reproduced exactly. The row names the wrong symbol: the claim is `hh2-the-one-reply-builder-stops-observing-the-revision` on **`_reply`**, which H-H2 extracted out of `perform_agent_create`. See below. |
| deletion blindness (plan §3, "a claim whose symbol no longer exists") | must fail loudly | **Already built** by H-H14. `_symbol_span` raises `symbol not found`, `_anchor_or_raise` raises `target missing`, and `_partition_claims` anchors EVERY claim before partitioning, so a stale symbol is a configuration error today. Pinned by `test_a_symbol_that_no_longer_exists_fails_and_names_the_one_that_holds_it`. Nothing to build. |
| deletion blindness (queue row, `4bf4387760`) | "`_changed_lines` sums only each hunk's `+` count" | Correct and unbuilt — this is the real open deletion blindness, and it is what got built. |
| POSIX-only schema | "a Windows-local run reports it SURVIVED" | Reproduced: `--base 36dc77c68d^` with that claim exits **1**, `SURVIVED: hh6-posix-file-lock-ignores-its-deadline`. |
| 31 CRLF blobs | "31 `.py` blobs" | Off by one and short by eight: 25 pure-CRLF `.py` + 5 MIXED `.py` = 30 `.py`; 38 CR-carrying blobs in total. |
| doc-cite rot | "four line-cites point at unrelated text"; "348 cites, 61 of 91 fail adjacency" | Both stand. This stage builds the RESOLUTION checker; the adjacency probe is still unbuilt and is the harder half. |

## The H-H2 selection miss, reproduced and closed

Measured against the real blobs at the real sha, not a fixture:

```
changed lines in agent_runtime/agent_create.py at 0ecb921b9d : 82
claim   hh2-the-one-reply-builder-stops-observing-the-revision  (symbol: _reply)
anchor lines            : 1170, 1171
anchor lines ∩ changed  : {}            <- NOT SELECTED, the row's miss
_reply spans lines      : 1094..1180
symbol span ∩ changed   : 41 lines      <- selected, after the fix
```

Git rendered the two lines the claim anchors on as unchanged CONTEXT inside a
function whose body was otherwise rewritten. Both halves are now pinned in
`tests/scripts/test_mutation_selection_follows_the_symbol.py` against
`0ecb921b9d` itself, so if the case ever changes the pin says so instead of
quietly proving nothing.

`module`-scope claims (2 of 113) deliberately keep line selection: their span
is the whole file, so widening them would select them on any diff that touched
the module.

**Measured cost of the widening**, since the candidate cap is a runtime bound:

| base | line-selected | + symbol-selected | total |
|---|---|---|---|
| `origin/main` (this branch) | 0 | 4 | 4 |
| `9d12e17299` (H-H store batch) | 6 | 21 | 27 |
| `0ecb921b9d` (H-H2) | 32 | 32 | 64 |
| `a68994d014` (S2) | 98 | 6 | 104 |

CI's declared cap is 20. The next batch landing will exceed it and will have to
raise it visibly or split. The queue's open "a whole-batch landing exceeds the
mutation selector's candidate cap" row now has numbers under it. **Not decided
here** — it is the orchestrator's call whether batch landings get a declared
higher cap or whether the cap should key on stages rather than claims.

## A defect found on the way, red on pristine `main`

`tests/scripts/test_mutation_claim_anchoring.py::test_the_mutation_is_spliced_at_the_anchor_not_at_the_first_occurrence`
fails on `origin/main` (`0c744aa586`) on this host: *"holders.py changed after
the anchor resolved"*. Verified by stashing the branch's changes and running
the pristine file.

Two readers, one file. `_anchor_or_raise` read the target with `read_text`
(universal newlines — a CRLF file decodes with LF) and the mutate loop with
`read_bytes().decode("utf-8")` (raw). Against any CRLF file every anchor offset
was one byte short per preceding line. **Not Windows-only:** 25 tracked `.py`
blobs carried CRLF at that sha, and a Linux checkout of one of those is CRLF
too — a claim anchored in `agent_runtime/stream.py` would have hit this on CI.
Fixed with one reader (`_read_source`), regression watched red first.

## Line-ending census, byte level, at `0c744aa586`

**Totals:** 8862 `i/lf`, 33 `i/crlf`, 5 `i/mixed`, 99 binary, 50 no-endings.
The naive per-blob scan also reported 93 "mixed" — 88 of them were PNG/JPG/ICO
binaries. `git ls-files --eol` classifies binaries correctly and runs in under
a second where the per-blob loop took over two minutes; use it.

**Deliberate keepers: NONE.** Every one of the 38 was a minority outlier in its
own neighbourhood:

- `docs/agent-runtime-harness/archive/`: 2 CRLF vs 64 LF
- `docs/agent-runtime-harness/` overall: 5 CRLF vs 122 LF
- repo root: 2 CRLF vs 125 LF, and both (`.launcher_run.log`,
  `htasks_open.json`) arrived in the fork-foundation import `0ad80754fa` with
  **no reader anywhere in the tree**
- every Windows-only script (`*.ps1`, `*.cmd`, 6 files) was **already LF**
- the one place bytes are a contract — `tests/fixtures/office_layout/**`,
  `-text`, digest-matched to the launcher — was already LF and is untouched

**Normalized (38):** `.launcher_run.log`, `htasks_open.json`,
`tests/source_grep_debt.txt`; `agent_runtime/{decision_contract_registry,
gateway_targets, media_handles, persona_chat_continuity,
serve_office_subscriptions, stream}.py`;
`docs/agent-runtime-harness/03-transport-and-wire.md`,
`docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/{env-determinism-audit,
neko_SOUL_mission-era-draft_2026-07-19}.md`,
`docs/agent-runtime-harness/harness-skills/harness-runtime-model/references/{persona-chat,
proof}.md`; `tests/agent/test_charsheet_draft.py`;
`tests/agent_runtime/{test_board_store*, test_gateway_peer_cross_install_chat_e2e,
test_launcher_qa_template_drift, test_office_store, test_response_contract_fixture,
test_running_work*, test_s47_wire_constant_field_removal,
test_s55_registered_events_have_emitters, test_s57_unruled_config_debt_removal,
test_serve_promotion_two_subscribers, test_serve_rpc_office,
test_serve_rpc_office_subscribe, test_serve_rpc_office_upsert, test_snapshot,
test_snapshot_coalesce*, test_snapshot_history_eviction*, test_stage19_visibility,
test_stage38_fixtures*, test_stream_contract_fixture, test_stream_resume,
test_sync_admission}.py`; `tests/hermes_cli/test_harness_characters_cli.py`;
`tests/test_coverage_claims_resolve.py` (`*` = the five that were MIXED).

Per file: bytes in, `b"\r\n"` → `b"\n"`, bytes out. Asserted that the decoded
text was otherwise identical, that no lone CR existed anywhere (none did), that
every `.py` still parses and every `.json` still loads.

**Two mechanisms, on purpose.** `* text=auto eol=lf` stops the next accident at
check-in — measured: with it in place, staging a deliberately re-CRLF'd file
silently normalizes it back, which is why the gate had to be red-proven by
pinning that file `-text` first. `tests/test_line_endings.py` catches what
attributes cannot see: blobs already in the object store, which is exactly how
these 38 sat there for months. It reads the INDEX, never the working tree.

Post-state: **0 CRLF, 0 mixed, 8904 LF.**

## Doc-cite report — the one run

`python scripts/doc_cite_report.py --root docs/agent-runtime-harness --exclude archive/`

```
over 60 docs
  line cites resolved      : 538
  sha cites checked        : 239 (129 distinct)
  bare names left ambiguous: 54
  cross-repo/elided, skipped: 79

PATH DOES NOT RESOLVE: 6
  planned/duplicate-implementation-retirement.md:224  read_model.py:339
  planned/duplicate-implementation-retirement.md:231  read_model.py:167
  planned/duplicate-implementation-retirement.md:235  projector.py:28
  planned/duplicate-implementation-retirement.md:248  read_model.py:167
  planned/remote-gateway-field-notes.md:35   docs/mission_control/planned/universal-remote-gateway.md:524
  planned/remote-gateway-field-notes.md:812  universal-remote-gateway.md:740

LINE IS PAST THE END OF THE FILE: 0

SHA IS NOT A COMMIT IN THIS CLONE: 43   (several are LAUNCHER shas — e.g.
  `e38bb108c`, `3a5fbacfb` — real over there, unknown here, and not
  structurally separable from rot)

SHA EXISTS BUT IS NOT AN ANCESTOR OF origin/main: 4
  harness-skills/harness-runtime-model/references/debugging.md:60    77410af53
  harness-skills/harness-runtime-model/references/persona-chat.md:107  3254b6853
  harness-skills/harness-runtime-model/references/persona-chat.md:134  8d7b4bab8
  harness-skills/harness-runtime-model/references/persona-chat.md:134  ac62bbca8
```

Including `archive/` (126 docs): 41 dead paths, 10 lines past end of file, 196
unknown shas, 123 non-ancestor shas — frozen copy, expected, not worth a sweep.

**The finding worth acting on is the last four.** Those shas exist in this
clone and are NOT ancestors of `origin/main` — the pre-rebase-sha trap the
queue named at its own round-five entry, where `d01e5f7bc … 7c35e3963` lived
only in a local reflog. They will be garbage-collected. The three
`planned/duplicate-implementation-retirement.md` rows cite `read_model.py` and
`projector.py`, both RETIRED (the projector retirement is the standing
architecture per the docs-canon consolidation), so that doc is citing a world
that no longer exists.

The report deliberately does NOT check whether a resolving cite points at the
right text. That is the adjacency probe, it is the harder half, and 61 of 91
machine-checkable cites reportedly fail it. Still unbuilt, still rowed.

## Pre-existing reds on `origin/main`, reported not folded in

1. `tests/scripts/test_mutation_claim_anchoring.py::test_the_mutation_is_spliced_at_the_anchor_not_at_the_first_occurrence`
   — the two-reader defect above. **Fixed here** (it is this stage's subject).
2. `tests/test_coverage_claims_resolve.py::test_every_coverage_claim_names_a_test_that_exists`
   — 6 coverage claims name tests that do not exist. Verified **identical on
   pristine `origin/main`** in a separate detached worktree, so the line-ending
   normalization did not cause it. Two of the six are truncated test names
   (`test_a_re_add_over_an_unreadable_archive_`,
   `test_deleting_the_stores_fence_`), which looks like a doc line-wrap eating
   the tail. Same class as the doc-cite rot above; NOT fixed here — it is a
   docs sweep, not a gate change.
3. `tests/test_coverage_claims_resolve.py` needed `--timeout=600` to finish on
   this host at all; at the default it timed out inside
   `difflib.get_close_matches` while BUILDING its failure message. A failure
   report expensive enough to trip the hang detector reads as a hang.

## Proof run

`python scripts/changed_line_mutation_check.py --base origin/main` → exit 0.

```
mutation candidates: 4 (cap 12)
  s4b-the-inventory-drops-the-claims-it-did-not-select      (selected by symbol)
  hh14-the-anchor-escapes-the-symbol-it-names               (selected by symbol)
  hh14-a-stale-symbol-goes-back-to-passing-in-silence       (selected by symbol)
  hh14-the-mutant-is-spliced-at-the-first-occurrence-again  (selected by symbol)
KILLED: all four
```

All four are the gate's own claims, and all four were selected by the widening
this branch introduced — under the old predicate this branch would have run
zero mutants against a rewrite of the selector itself.

Focused suites: `tests/scripts/` 82 passed; the 18 normalized test modules 582
passed; `tests/test_line_endings.py` 3 passed and red-proven.

## Rebase onto W1-H1, and the one thing worth carrying from it

`origin/main` moved from `0c744aa586` to `2638504f9b` mid-stage — W1-H1 landed
four commits. Rebased; **only the two predicted files conflicted**, both from
this stage's normalization commit meeting W1-H1's edits inside them:
`tests/agent_runtime/test_office_store.py` and
`tests/agent_runtime/test_serve_rpc_office.py`. Resolved by taking the UPSTREAM
content (W1-H1's edits, byte-verified equal to `origin/main`'s blob) and
re-applying the normalization to it — 1929 and 895 CRLF respectively — rather
than by resolving hunks, which for a whole-file ending change would have been
a coin flip per hunk. Both re-parsed as Python and both suites re-run green.

`tests/mutation_claims.json` merged with no conflict at all: W1-H1's 8
`dcw-h1-*` claims append at the end, this stage's edit adds a field to `hh6-…`
in the middle. Registry now 121 claims, still LF, still one claim declaring
`platforms`.

**The carryable finding:** W1-H1 edited two CRLF files and correctly kept them
CRLF, which is the discipline working. It also means that without this stage's
normalization landing, every subsequent wave would keep paying that tax — and
the two agents' branches would have kept colliding on it. The two mechanisms
are now in place, so the next agent to edit those files gets LF for free.

Post-rebase re-verification, all green: `origin/main` is an ancestor of HEAD
(ff-only landable); repo census 8908 LF / 0 CRLF / 0 mixed; 171 focused tests
passed (`tests/scripts/`, `tests/test_line_endings.py`, and both rebase-touched
modules); `--base origin/main` exit 0, the same 4 claims selected by symbol and
all 4 killed. Of the 49 files this branch touches, 4 were already LF and stay
LF, 38 are the deliberate normalization, 7 are new and LF — nothing left
non-LF, no unintended renormalization.
