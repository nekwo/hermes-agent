# w13/h1 — field notes, 2026-09-04

Five RULED rows, all hermes. Worktree branch `w13/h1`, base `9ea840bb90`
(`origin/main` at claim time; the brief named `e6dbcbb40c`, which the worktree
had already moved past — recorded, not corrected).

Every row's ruling was taken as the spec. Nothing here re-opens a ruled
question.

---

## Row 1 — persona prewarm: KEEP the worker (`5c212ac2b8`)

**Asked.** Record the ruling where the worker and the S0a A6b note recommend
retirement; delete the per-persona-warm ambition from the plan; leave the verb
and the launcher caller alone.

**Measured.** Nothing to re-measure — the row's own premise is that the
measurement half is DONE twice over, and both numbers are already written down
in the tree (A6b's 1–2 ms second warm in the S0a field notes; w12/m5's
1,418–1,543 ms first warm in the module docstring). Confirmed both are present
and agree before writing anything.

**Changed.** The ruling is now recorded in the four places a reader could have
walked away with the opposite conclusion:

- `agent_runtime/persona_prewarm.py` — the docstring's re-take block gains the
  ruling: KEEP, the two reads never conflicted, and any per-persona warm beyond
  the first is ruled out.
- `docs/agent-runtime-harness/planned/s0a-atlas-cleanup-field-notes-2026-09-03.md`
  — the A6b block's "Recorded, not acted on" is answered in place.
- `docs/agent-runtime-harness/planned/s0a-atlas-cleanup.md` — §2 A6b's decision
  rule is marked DONE and its retire branch WITHDRAWN, with the reason (it keyed
  on the second warm, which was the wrong input); the §"Prewarm memo re-measure"
  pointer says the decision is ruled.
- `docs/agent-runtime-harness/08-performance-and-debt-ledger.md` — a new entry
  under *Refusals with a measurement — do NOT re-propose*, and
  `04-boot-and-lifecycle.md` Stage 9 carries the same in one paragraph.

No code behaviour changed. The verb and the launcher caller were not touched.

**Commands.** `scripts/run_tests.sh tests/agent_runtime/test_persona_prewarm.py`
→ 13 passed. `scripts/dump_cli_contract.py --check` → 0.
`tests/test_coverage_claims_resolve.py` → 0.

**Cite fallout.** The docstring insertion moved five cites; all re-anchored
(`07-observability.md` ×3, `04-boot-and-lifecycle.md` ×1, and `:255-258` →
`:298-302`). The cite-adjacency waiver `07-observability.md|persona_prewarm.py:130`
went stale and was DELETED — the baseline shrank 75 → 74, never grew.

---

## Row 2 — `tool_visibility` reads the toolset manifest (`9ef1324634`)

**Asked.** l3's stage R135.4 with the ruled union:
`_cached_tool_names_for_toolsets` / `get_toolset_for_tool` read
`manifest ∪ (registry after discover_plugins())`, and nothing that decides
runnability reads the manifest.

**Measured.** The premise held at base: both call sites went through
`_ensure_tool_registry_populated()`, whose only job is to import `model_tools`
(38 registrar modules, 3.16 s on this checkout per the reader's own header), and
the existing deferral test asserted that import as the REPAIR.

**Changed.**

- `agent_runtime/tool_visibility.py` — module-scope import of
  `tools.toolset_manifest` (a JSON reader; imports no registrar module, which is
  why it may sit where the thing it replaced could not); new
  `_ensure_plugin_tools_registered()` carrying the lifecycle statement;
  both name answers repointed onto the union.
- `_ensure_tool_registry_populated` STAYS and is re-documented as the CAPABILITY
  door and nothing else.
- `tools/toolset_manifest.py` — the "ships UNWIRED / awaiting a ruling" header is
  replaced by the ruling and by the line it draws: nothing that decides whether a
  tool can RUN may read the module.
- The l3 plan's §R135.4 is marked RULED + BUILT with what shipped.

**Red-first.** `test_the_name_answer_never_imports_a_registrar_module` — red at
`assert ['model_tools'] == []` before the change, green after.
`test_a_plugin_tool_is_still_in_the_union_the_manifest_cannot_see` — a tool that
only a stubbed `discover_plugins` registers still reaches the answer.

**One existing case changed hands rather than being deleted.**
`test_the_first_visibility_resolve_populates_the_registry_and_returns_tools`
pinned the BW-H3 repair as "`model_tools` IS in `sys.modules` afterwards", which
this ruling inverts. It now pins the property that was always the point — a real,
non-empty answer — plus the ABSENCE of the import, and is renamed
`..._answers_real_tools_without_the_import`. Deleting it would have dropped the
"answers nothing, silently" guard that the first draft of BW-H3 actually shipped.

**Commands.** `test_tool_visibility_import_deferral.py` 9 passed;
`test_tool_visibility` + `test_chat_lane_toolsets` + `test_toolset_declaration` +
`test_turn_visibility` + `test_stage19_visibility` + `tests/tools/test_toolset_manifest.py`
117 passed; `test_chat_lane_bundle` + `test_persona_assignments` +
`test_mcp_lane_visibility` + `test_runtime_hud_capability_visibility` 184 passed;
`test_harness_core_ratchet` + `tests/tools/test_registry.py` 46 passed.
`dump_toolset_manifest.py --check` → fresh (90 tools / 31 toolsets);
`emit_harness_tool_inventory.py --check` → fresh (44 tools / 15 toolsets);
`dump_cli_contract.py --check` → fresh.

---

## Row 3 — a backup draft names what it shadows (`b022755b52`)

**Asked.** Option (ii): a backup directory stays a ROW and carries a `shadows`
field naming the draft id it copies. Hermes owns the field; the launcher owns the
dedupe.

**Measured.** Premise confirmed by construction rather than by the live pair:
`list_drafts` appends every subdirectory holding a `draft.json` and `id` reads
the file's own `id`, so a copied directory answers the original's id. Reproduced
in a hermetic root with `shutil.copytree`, which is what the live
`.backup-2026-08-25-nefix` pair is.

**Changed.** `CharacterDraft.shadows` (`agent/charsheet/draft.py`) and a
`"shadows"` key on `_characters_draft_summary` (`hermes_cli/harness.py`), so it
rides `list --json`, `status --json` and the `start --json` summary alike.

DERIVED, never stored, and that is the load-bearing choice: nothing in the module
writes a backup, so there is no writer to teach and no backfill to run. The fact
is already on disk in the disagreement between the directory name and the
recorded id, and `create()` puts a draft at `drafts_dir()/<id>` — so agreement is
an invariant a real draft holds by construction and a copy necessarily breaks.

**Red-first, twice.** The property
(`test_a_backup_directory_is_a_row_that_names_the_draft_it_shadows`, red on
`AttributeError: no attribute 'shadows'`) and the wire key
(`test_list_json_names_which_row_a_backup_directory_shadows`, which also spells
out the launcher's dedupe so the payload is provably enough to do it alone).

**Commands.** `test_charsheet_draft.py` + `test_harness_characters_cli.py`
→ 236 passed. `dump_cli_contract.py --check` → fresh.

**Launcher half (not mine to write).** In the drafts fold, drop every row whose
`shadows` is a non-null string and keep the un-shadowed row for that id; the fold
must tolerate the field's ABSENCE (an older hermes answers no key at all), in
which case the current behaviour — both rows — is correct and not a regression.

---

## Row 5 — `derived_at` on a mutation claim (`7c554b0c68`)

Written up before row 4 because it landed first; the two share a file.

**Asked.** ONE optional field holding the commit a claim was derived at; NO
backfill (absence means pre-schema); a stale marker is a WARNING in the gate's
report, never a failure.

**Measured.** Premise confirmed verbatim: the schema refuses unknown claim fields
by design (`unknown = sorted(set(claim) - required - {"platforms"})`), so
provenance was not a convention an author could adopt without a schema change.

**Changed.** `DERIVED_AT_KEY`, the schema allowance, `_commits_since_derivation`
(git log over the claim's FILE since the recorded commit; `None` for "nothing to
say" — no field, or a sha this checkout cannot resolve), and one `WARNING:`
line per selected stale claim on stdout. Documented in
`tool/test_quality/README.md`.

**Red-first**, four cases, including the one that matters: the warning and
`code == 0` are asserted IN THE SAME CASE, so an implementation that reported
staleness by refusing — the obvious wrong answer, and the one the ruling names —
fails there even though it "detected" the same thing.

**The field's first user, found the hard way.** Row 2's edit invalidated
`bwh3-registry-read-before-the-deferred-import-populates-it` (the gate went to
`mutation source not found`). The guarantee it pinned survives with a new
mechanism — the ordering barrier is now the plugin-discovery call — so the claim
was RE-DERIVED onto it, renamed to say what it kills, and stamped with
`derived_at`. Exactly the loud half of this row's own class, paid inside the same
wave.

**Commands.** `tests/scripts/test_changed_line_mutation_check.py` → 12 passed
after the change (4 red before); the four sibling mutation suites → 46 passed.

---

## Row 4 — a wall-clock budget replaces the candidate cap (`6f4e917eb3`)

**Asked.** A WALL-CLOCK budget replaces the candidate-count cap, the same ruling
the launcher's discovered-extras count took (`kRepoLaneWallBudgetSeconds`); the
count stays reported, never asserted.

**Measured.** Premise confirmed at base: `.github/workflows/tests.yml` hard-coded
`--max-candidates 20`, and the script's refusal was `len(claims) > max_candidates`.

**Changed.**

- `--wall-budget-seconds`, default `DEFAULT_WALL_BUDGET_SECONDS = 900` — fifteen
  minutes, which is the ceiling CI's own `timeout-minutes: 15` already enforced,
  said out loud where a local run can read it.
- The count line is now `mutation candidates: N (reported, not capped; wall
  budget Bs)`. The trailing parenthetical is deliberate: CI's selector greps
  `^mutation candidates: 0 ` **with the trailing space**, so the line keeps a
  token after the count whatever the bound is called.
- Checked in two places, and both are load-bearing. Before the mutating phase
  (so a refused run holds no lock — the property the cap refusal had) and
  between claims, never INSIDE one, because a run stopped mid-mutation would
  leave a spliced file on disk, which is the one thing this gate may never do.
- `--list` no longer refuses on size. Under the cap, a diff too big to run was
  also refused the inventory — the one thing it needed to know was the one thing
  it could not ask.
- CI passes `--wall-budget-seconds 600` with the reason beside the call site;
  `tool/test_quality/README.md` rewritten; the pin test repointed at the new flag.

**Red-first**, three new cases: the count reported and never refused, a spent
budget refusing BEFORE the lock with no command run and both cures named (budget
raise first, because splitting is not available to a landing), and `--list`
never spending the budget.

**Two existing cases changed hands** rather than being deleted, because the
property under each survived the unit change:
`test_the_cap_still_refuses_even_while_the_inventory_prints` becomes "a big
inventory prints both halves and refuses nothing", and
`test_a_skipped_claim_still_counts_toward_the_cap` becomes "…still counts in the
report" — a report whose number moved with the host would still be lying about
what the diff put on the hook.

---

## Reds I did not cause

1. **`scripts/doc_cite_adjacency.py` has 4 unwaived failures at base.** Proved by
   stashing every change and re-running before the first commit:
   `01-system-architecture.md:694` (×2, `harness.py:4915` →
   `_cmd_characters_auto`) and `07-observability.md:636` (×2, two
   `hermes_cli/harness.py` ranges). Left exactly as found.

2. **The mutation gate is a configuration error on `main`.**
   `iws-ws1-the-activate-events-free-ride-at-an-undeclaring-client` anchors
   `"realm.activated": SCOPE_ENTITY,\n}` in `agent_runtime/patch_coverage.py`,
   and w12/l3 inserted an entry after that line on `main` (`721d08758e`), so the
   needle no longer occurs and NO claim can run until it is re-derived. Not mine
   — my branch does not touch that file — and left for whoever owns the S/WS
   lane. It is the loud half of exactly row 5's class.

3. **`tests/test_coverage_claims_resolve.py::test_every_coverage_claim_names_a_test_that_exists`
   is red at base.** Every failing citation is in
   `docs/agent-runtime-harness/planned/s2-introduce-directory-push.md` — a file
   this branch does not touch, and the five claims w13/h4 was dispatched to
   repoint. Proved to predate my first commit: the same run over a tree carrying
   only row 1's edits was already `1 failed, 3 passed`.

4. **One flaky line, not a failure.** A run of
   `tests/agent_runtime/test_persona_assignments.py` printed
   a FAILED line for the cleared-binding-is-not-stale case
   while the same run's summary read `184 tests passed, 0 failed` and the runner
   exited 0 — a retry that passed. Recorded because a reader of the log would
   otherwise see a name with no verdict.

## What is left

- The launcher half of row 3 (the `shadows` dedupe) — l1's row, and the fold
  must tolerate the field's absence.
- Nothing else. All five rows are DELETE hand-backs.
