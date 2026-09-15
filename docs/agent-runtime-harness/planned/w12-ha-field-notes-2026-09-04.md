# Field notes — w12/ha (hermes, 6 rows, 2026-09-04)

Worktree `w12-ha`, branch `w12/ha`, base hermes main `dcba382f0a`. Python:
`X:/Eternia/.venvs/hermes-test` via `scripts/run_tests.sh` with
`HERMES_PYTHON=X:/Eternia/.venvs/hermes-test/Scripts/python.exe`. Nothing
pushed; nothing outside this worktree touched.

## Row 13 — retire the persona-prewarm worker

> **NOT LANDED (operator, 2026-09-04).** The retirement commit was dropped from this lane at landing. The same afternoon, lane w12/m5 re-took the prewarm measurement on a cold process and found the FIRST warm absorbs ~1.4-1.5 s of registry import that the first create would otherwise pay (the create-shaped resolve behind it is ~10 ms); only a warm for a SECOND persona type is worth 1-2 ms. Two same-day measurements now argue opposite ways about the worker, so retiring it is an operator ruling, not a small row. The section below records what the lane BUILT and verified; none of it is on main. The launcher still fires `runtime.persona.prewarm` from `mission_persona_prewarm.dart`, which is the other reason the verb cannot go first.

**Re-measured before fixing.** Confirmed at this base:
`agent_runtime/persona_prewarm.py` and the `runtime.persona.prewarm` RPC verb
both still existed, exactly as the row said, and S0a's A6b field-note number
(warm buys 1-2 ms, plan's keep-rule is 300 ms) stood unchallenged.

**Fixed.** Deleted `agent_runtime/persona_prewarm.py`, the
`runtime.persona.prewarm` `@method` block and its tier-classification
paragraph in `agent_runtime/serve_rpc.py`, and
`tests/agent_runtime/test_persona_prewarm.py`. Scrubbed the retired verb out
of the manifest/tier expectation lists in `test_serve_rpc_method_tiers.py`,
`test_serve_rpc_office.py` (×2 pairs), `test_serve_rpc_office_subscribe.py`,
`test_serve_rpc_office_upsert.py` (×2 pairs) and a stale present-tense mention
in `test_agent_retire_service.py`. Fixed an unused
`from agent_runtime import agent_create_phases, persona_prewarm` import and
three stale docstring citations in `test_agent_create_subphases.py`. Rewrote
doc 04's Stage 9 as a short retirement note, dropped the dead receipt row and
citations from doc 07, dropped the tier-explanation paragraph and table row
from doc 03, and annotated the debt-ledger and S0a-cleanup-plan mentions in
doc 08 / `archive/s0a-atlas-cleanup.md` rather than leaving them dangling.
Removed 4 now-stale entries from `cite-adjacency-baseline.json` (the citations
they waived no longer exist) — the baseline shrank 75 → 71, no new waivers
added.

**Red-first.** No new production behavior to falsify (a pure deletion); the
gates that matter here are the two ratchets — `doc_cite_adjacency.py` and
`test_coverage_claims_resolve.py` — which I ran before AND after every doc
edit to prove I was not growing either. Coverage claims: baseline 5
(pre-existing, in `planned/s2-introduce-directory-push*.md`, not touched, not
grown), confirmed by rerun after each edit round. Cite-adjacency: found 1
pre-existing crash-then-red I did not cause (see "Main red found" below) and
fixed 3 reds and a hard crash that MY OWN edits introduced, down to net zero
new reds.

**Verify.**
`scripts/run_tests.sh tests/agent_runtime/test_serve_rpc_method_tiers.py
tests/agent_runtime/test_serve_rpc_office.py
tests/agent_runtime/test_serve_rpc_office_subscribe.py
tests/agent_runtime/test_serve_rpc_office_upsert.py
tests/agent_runtime/test_agent_retire_service.py
tests/agent_runtime/test_peer_authorization.py
tests/agent_runtime/test_serve_rpc_chat_turn.py
tests/agent_runtime/test_agent_create_subphases.py` → 232 passed, 0 failed
(combined with rows 33/129 below in the final run). `dump_cli_contract.py
--check` → fresh (no parser touched by this row).
`doc_cite_adjacency.py --exclude archive --exclude planned` → 1 unwaived
failure, proven pre-existing (below). `test_coverage_claims_resolve.py` → 5
failures, byte-identical to the pre-existing five.

**Left for the operator / launcher repo.** The launcher's per-chip palette
trigger for `runtime.persona.prewarm` is a separate row: the launcher side
should stop calling the now-deleted verb. Patch description for the launcher
session: find the palette-open call site that fires `runtime.persona.prewarm`
per persona chip (grep the launcher repo for `persona.prewarm` /
`prewarm`), delete the call and its RPC client method, and delete or update
any launcher test asserting the verb is in the manifest.

Commit: `abfdcc7b4b`.

## Row 33 — `harness pets sprite` metadata-only mode

**Re-measured.** Confirmed `pets_sprite` declared only `slug`/`--json` (no
`--no-sheet`) while `characters_sprite` already had one, and the pet payload
still lived in the separate `_pet_sprite_payload_for_launcher` — verbatim as
filed.

**Red-first.** Added
`test_harness_pets_sprite_no_sheet_is_metadata_only` to
`tests/hermes_cli/test_harness_pets_cli.py` first; it failed on `--no-sheet`
being an unrecognized argparse flag (`SystemExit: 2`), proving the row's
premise as a test rather than just a grep.

**Fixed.** Added `--no-sheet` to the `pets sprite` subparser, threaded
`include_sheet` through `_cmd_pets_sprite` into
`_pet_sprite_payload_for_launcher`, and gave that function the same
conditional-slot shape `characters sprite --no-sheet` uses (`spritesheetBase64`
XOR `sheet`). Noted in the docstring why pets differ from characters here:
`framesByRow`/`framesByState`/`stateRows` still open the sheet file even in
metadata-only mode, because a pet carries no per-row frame count in a
manifest the way a character draft does — only the whole-sheet base64 encode
is skipped. The default path is untouched: `_sprite_payload_for_baseline`'s
byte-stable sha assertion in the same test file still passes unmodified,
confirming the existing behaviour did not move.

**Verify.** `test_harness_pets_cli.py` + `test_harness_characters_cli.py` →
112 passed, 0 failed. `dump_cli_contract.py --write` (new flag = new contract
row, 190 → 191 command paths) then `--check` → fresh. My insertion shifted
line numbers below it in `hermes_cli/harness.py`; re-anchored the four
`doc_cite_adjacency`-checked citations that moved (`01-system-architecture.md`
`:22`→harness.py `6322→6347`, `:694`→`4890→4915` ×2, `07-observability.md`
`:631`→`5555-5573,5792-5804`→`5588-5599,5819-5826`) and reran the probe to
confirm zero new reds.

Commit: `b8a4462411`.

## Row 129 — hardcoded reader name in the flag-binding boundary test

**Re-measured.** Confirmed `_flags_read_as_absent_or_given` still matched the
literal string `"list_flag_or_absent"` at its one comparison site, rather than
deriving from `flag_binding.__all__` or any other canonical source —
verbatim as filed.

**Red-first.** Refactored `_flags_read_as_absent_or_given` to take
`modules`/`reader_names` parameters (defaulting to the real walk and
`flag_binding.ABSENCE_PRESERVING_READERS`), then added
`test_a_second_absence_preserving_reader_joins_the_gate_by_declaration_alone`,
which points the walk at a synthetic module calling a synthetic reader name
via the new parameters. This call shape does not exist on the pre-fix
function signature at all (`TypeError: unexpected keyword argument`), so the
test is red against the row's own defect, not against a scenario I invented
afterward.

**Fixed.** Declared `ABSENCE_PRESERVING_READERS = frozenset({"list_flag_or_absent"})`
in `hermes_cli/flag_binding.py`, next to `__all__`, with a docstring
explaining it is the canonical source the boundary test derives from. Did
not fold every name in `__all__` into it — `list_flag_or_empty` and
`flag_given` are not absence-preserving, so a blanket `frozenset(__all__)`
would have been wrong, not just imprecise.

**Verify.** `test_flag_binding_boundary.py` → 13 passed (12 existing + 1 new),
0 failed. No parser or doc citation touched.

Commit: `7e52fa12a5`.

## Row 78 — office_store's dead plan citation

**Re-measured.** `agent_runtime/office_store.py`'s `DuplicateDeskRefused`
docstring (line drifted from the row's stated `:146` to `:100` at this base —
noted by the sweep) still read `` `agent-placement-verb` F9``, and
`docs/agent-runtime-harness/planned/` carries no file matching `placement` —
the plan `1295212f2e` deleted stays gone. Both halves of the premise held.

**Fixed.** Re-pointed the citation at
`docs/agent-runtime-harness/06-office-and-board.md` §Supersedes, which already
carries the deleted plan's sha, the `git log --diff-filter=D` recipe to
recover it, and where each of its decisions (D1-D12, the gateway table, the
killing-mutation table) lives on. No code changed — docstring only.

**Verify.** `python -c "import agent_runtime.office_store"` → imports clean.
`doc_cite_adjacency.py --exclude archive --exclude planned` → no new
failures (this edit adds a `.md` reference, not a `.py:N` cite, so it is
outside what the probe checks at all).

Commit: `e8b9f9faf1`.

## Rows 31 and 81 — hand back, cannot edit the launcher vault

Both rows are `launcher`-side (`Launcher_Brain/`, `.dart` test suites); a
hermes agent cannot touch either.

- **Row 31 — `KEEP`.** `drafts[].id` non-uniqueness is documented but not
  handled: `hermes_character_client.dart:133-135` and
  `charsheet_payloads.dart:448` (both launcher files) already carry the
  hazard comment and explicitly decline a name-shaped heuristic;
  `ensure_character_project.dart:119`'s `ensureCharacterProject` is still
  idempotent on draft id, unchanged. The deliberate de-duplication the row
  asks for (naming which copy survives) has not been written. Nothing in the
  hermes repo can fix this — the fix, if taken, is a launcher-side
  de-duplication in the QA surface that keys on `CharaDraftSummary.directory`
  instead of `id`, with an explicit "kept newest by mtime" (or similar) rule
  stated in the same commit. Re-file or dispatch to a launcher lane.

- **Row 81 — `KEEP`.** The tautology-sweep gap is real and unfixed: the
  `group('charaDraftBindingFoldWouldLearn is the fold, asked as a question',
  ...)` comment in
  `test/features/studio/domain/character_pipeline_binding_test.dart:571-577`
  explains why the pair exists but never states what it does NOT cover (the
  home/persona rules, carried by two ordinary cases beside it). The ask is a
  one-sentence addition to that comment — narrow, launcher-only, not a
  re-file.

## Main red found, not mine

`docs/agent-runtime-harness/07-observability.md:628` cites
`agent_runtime/serve_rpc.py:885-886` for `baseline_offset`/`event_offset_of`/
`patch`/`watermark`, but that code is actually at `serve_rpc.py:1031` on this
base — a drift of ~146 lines, not explained by anything in this lane's
commits. Verified pre-existing by diffing `git show dcba382f0a:` of both the
doc and `agent_runtime/serve_rpc.py`: at HEAD the same citation already
pointed at unrelated `already_subscribed` docstring prose, ~140 lines short of
the real target, before any of this lane's edits touched `serve_rpc.py`.
Left alone — not this lane's row, and the house rule is prove-and-leave, not
silently re-anchor or baseline it away.
