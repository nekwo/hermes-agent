# W1-H1 field notes — conflict guesses + unreadable-actor warnings (2026-08-31)

Running record for stage **W1-H1** of the decision-close wave
(`EterniaLauncher/docs/mission_control/planned/decision-close-wave-2026-08-31.md`),
branched from hermes `origin/main` `0c744aa586`, built in
`X:/Eternia/_worktrees/dcw-h1` on `feat/dcw-h1-conflict-surfaces`.

## What the stage was

Three rowed remainders, all of the same family — a runtime that KNOWS something
about how complete its own answer is, and drops the knowing at the last seam:

1. RD-5: `scan_conflicts` knows which conflict keys are FILENAME guesses;
   `office_summary_row` had no field for it, so both readers handed an operator
   a token `office resolve-conflict --actor <it>` may not find.
2. The two readers (snapshot parity warning, `harness office show --full`) mark
   the guesses.
3. AX5 remainder: `workspace_template.copy_workspace_content` dropped source
   actors whose files would not decode with no `warnings` row, while degrading
   every other office fault to one.

## Measurements taken before coding

- **`ConflictScan.keys` and `.outcomes` are appended in lockstep.** Every arm of
  `scan_conflicts` (`office_store.py`) appends exactly one entry to each list —
  the `except` arm, the empty-`actor_key` arm, and the read arm — so a positional
  `zip` is total, not a guess. That is what makes `guessed_keys` a projection of
  `outcomes` rather than a fourth thing to keep in sync.
- **`.resolved.json` sidecars are skipped BEFORE the read**, so they can never
  contribute a guess. Pinned already by `hh3-a-resolved-sidecar-reads-as-a-live-conflict`.
- **Claim pre-flight** (`tests/mutation_claims.json`, `symbol` fields): the only
  claims anchored anywhere near this stage are the three `hh3-*` on
  `scan_conflicts`, `hh3-the-prune-ack…` on `archive_actors_for_instance`, and
  `ax5-the-projection-guard-goes-blind-to-a-short-read` on `_emit_actor_patch`.
  NONE anchors on `office_summary_row`, `_office_parity_warnings`,
  `_office_surface_row` or `_copy_office`, and `scan_conflicts`' body was not
  rewritten — so no existing claim needed re-anchoring.
- **`office_summary_row` has SIX callers, four of them tests** — `snapshot._offices_summary`,
  `hermes_cli/harness_parts/office._office_surface_row`, and direct calls in
  `test_serve_rpc_office.py`, `test_office_state_patches.py` (x2). A
  keyword-REQUIRED parameter breaks all four test calls by construction; each was
  updated to say `conflict_guessed_keys=()` out loud, which is the rule working.
- **The launcher decoder is key-by-key, not exhaustive**
  (`mission_control_snapshot.dart:1895` reads `conflict_actor_keys` by name), so
  the new row key is additive on the wire with no launcher change owed.

## What was built

| file | change |
|---|---|
| `agent_runtime/office_store.py` | `ConflictScan.guessed_keys` — THE derivation, from `outcomes` |
| `agent_runtime/snapshot.py` | `office_summary_row` gains keyword-REQUIRED `conflict_guessed_keys` + the row key; `_offices_summary` derives both lists from ONE `scan_conflicts`; `_office_parity_warnings` marks guessed keys with a `guessed` field and a detail suffix |
| `hermes_cli/harness_parts/office.py` | same ONE-scan rework; `conflict_guessed_keys` rides `--full` beside the keys it qualifies |
| `agent_runtime/workspace_template.py` | `_copy_office` appends an `office_actors_unreadable` warnings row when `scan.unreadable` |

Design calls made inside the spec:

- **One warning CODE, discrimination in a FIELD.** `office_actor_conflict` keeps
  its token — the `orphaned_office` precedent — because a second code
  (`office_actor_conflict_guessed`) would zero every existing census of the
  condition. The warning gains `guessed: bool` (what a program branches on) and
  the detail sentence gains
  `" (filename guess — resolve-conflict will not find this key)"` on guessed
  entries only. A warning that hedged on every key would teach an operator to
  ignore the hedge.
- **The skinny CLI row is untouched.** It carries a `conflicts` COUNT and no
  tokens, so it cannot hand anyone a key that will not resolve. Only `--full`
  prints keys, so only `--full` prints the guess list. The skinny key set stays
  as `test_office_surface_row_key_sets_are_unchanged` pinned it.
- **No subset validation.** A `guessed_keys ⊄ keys` raise was considered and
  dropped: it would be a new failure mode in a row builder whose job is to
  report trouble, and the readers already tolerate a stray entry (they mark by
  membership). Out of the stage's additive-keys-only fence.

## Tests + claims

Focused suites only, per the wave discipline. Eight new claims, all merged BY
CLAIM ID by textual insert (the file stayed byte-identical outside the
insertion) and all hand-proven: `changed_line_mutation_check.py --base origin/main`
selected exactly the 8 and reported KILLED for each, exit 0.

## CRLF census

`tests/agent_runtime/test_office_store.py` and
`tests/agent_runtime/test_serve_rpc_office.py` are deliberately CRLF on
`origin/main`; both were edited byte-wise (read bytes, splice, `write_bytes`)
rather than through a text editor, and every touched file was censused
byte-level against `git show origin/main:<path>` before the push. All ten files
match their base ending exactly. `tests/mutation_claims.json` is LF and stayed
LF.

## For the orchestrator at landing

Queue rows this branch closes (both under the AX/H-H3 remainder block):

- "The conflict-sidecar shortfall dies at the office summary row: `scan_conflicts` knows which"
- "`workspace_template.copy_workspace_content` drops source actors whose files will not decode"

Nothing measured contradicted the plan. The wire change is exactly the two
additive keys the plan specified (`conflict_guessed_keys` on the office row) plus
the additive `guessed` field on an existing warning; no contract version bump is
owed and none was taken.
