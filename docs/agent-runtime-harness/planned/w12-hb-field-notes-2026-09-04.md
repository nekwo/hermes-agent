# w12/hb — field notes, 2026-09-04

Lane `w12/hb`, worktree `_worktrees/w12-hb` (named by slug — the launcher's
`ProjectRoot`/`Eternia` drive path is not repeated here), branch `w12/hb`,
base hermes main `dcba382f0a`. Six rows from `mission-control-queue.md`
(`w12_rows_hb.md`). Two shipped, three handed back as open decisions, one is
launcher-only and out of hermes' reach.

---

## Row 87 — uninstall banner omits the git-history warning

**Asked:** neither the CLI uninstaller nor the Electron Desktop confirm step
warns that the code checkout's git history is deleted and not backed up by
this tool; add one sentence to each.

**Measured:** re-read `hermes_cli/uninstall.py`'s `_print_uninstall_dry_run`
(prints "Code checkout: {project_root}") and the interactive preamble
("Current Installation") — neither mentions git history, and the printed
warnings otherwise name only `$HERMES_HOME`. Confirmed the same gap in
`apps/desktop/src/app/settings/uninstall-section.tsx`'s confirm step, whose
`consequence` strings for the `lite`/`full` modes ("the Chat GUI and the
Hermes agent…") never mention the checkout's history either.

**Red-first:** `tests/hermes_cli/test_uninstall_git_history_warning.py` — two
tests (dry-run plan, interactive preamble) asserting `"git history"` and
`"not backed up"` appear in the printed output. Both red before the patch
(no match against captured stdout), green after.
`apps/desktop/src/app/settings/uninstall-section.test.tsx` — two tests
(warning present for an agent-removing mode, absent for GUI-only) written
against the existing pattern in `controls.test.tsx`.

**Changed:**
- `hermes_cli/uninstall.py`: one line in `_print_uninstall_dry_run` after the
  "Code checkout" bullet, one paragraph in `run_uninstall`'s "Current
  Installation" preamble.
- `apps/desktop/src/app/settings/uninstall-section.tsx`: a conditional
  paragraph in the confirm step, shown only when `pendingOption.needsAgent`
  (the `lite`/`full` modes — `gui`-only never touches the checkout).

**Commands + exit codes:**
- `bash scripts/run_tests.sh tests/hermes_cli/test_uninstall_git_history_warning.py tests/hermes_cli/test_uninstall_dry_run.py tests/hermes_cli/test_gui_uninstall.py tests/hermes_cli/test_uninstall_node_symlinks.py` — 4 files, 8 tests passed, 0 failed.
- `python scripts/dump_cli_contract.py --check` — fresh, 191 command paths (untouched by this row; Lane A sanity).
- The TSX test could NOT be run to a real pass/fail in this worktree: no
  `node_modules` existed (only Python setup is part of this lane's
  provisioning), and after `npm install --workspace apps/desktop` (2m,
  1221 packages) `vitest` failed at startup on a pre-existing environment
  defect unrelated to this change — first a missing optional native binding
  (`@rolldown/binding-win32-x64-msvc`, the documented npm optional-deps bug,
  npm/cli#4828), then after installing that package directly, a second,
  deeper one: `html-encoding-sniffer` doing `require()` of an ESM module
  (`@exodus/bytes/encoding-lite.js`) under Node 20.17, `ERR_REQUIRE_ESM`.
  Neither is caused by this patch (both errors are startup-time, before any
  test file is even loaded) — this is recorded as a **verification gap**,
  not a claimed green. The change was reviewed by hand against the
  component's existing conditional-rendering pattern (the adjacent
  `summary?.running_app_path &&` block) and is narrow (one new conditional
  paragraph, no new state, no new props). `package-lock.json` was touched by
  both npm operations and reverted before committing — neither belongs to
  this row.

**Row 87: DELETE.** Both hermes-side halves (CLI + Electron/React) land in
commit `010d1e7377`. The row named no launcher-side remainder, and none was
found.

---

## Row 89 — `deleted_workspace_ids` lift does not propagate under union merge

**Asked:** decide whether a lift wants its own propagating machinery (a lift
marker, mirroring `restored_at` for skills) or whether local-until-age-out is
correct.

**Measured:** both lift sites are unchanged from the sweep's snapshot —
`agent_runtime/default_scope.py:133-136` and `:528-531` still rebuild
`deleted_workspace_ids` by a local filter with no marker, so a peer's
set-union merge still re-adds a lifted id until the cap ages it out. This is
the RULED union behaviour (RD-11), not a regression.

**Not actionable as a code change.** The row is explicitly framed as a design
question with two named answers and no ruling either way; picking one
unilaterally would be deciding an operator call, not fixing a bug. No code
was touched.

**Row 89: KEEP (open decision — needs an operator ruling between "propagating
lift marker" and "local-until-age-out"; sweep confirms the premise is
unchanged at hermes `dcba382f0a`).**

---

## Row 97 — second revert of a subtree-absent `changed` item archives it

**Asked:** decide whether the UI should re-confirm on the second revert pass,
given the reclassification (`changed` → `added` after
`baseline_entry_dropped`) is the plan's ruled, honest consequence.

**Measured:** `agent_runtime/realm_revert.py:162` `classify_revert` still
carries the reclassification; grepped both `docs/mission_control/planned/`
and this repo's `docs/agent-runtime-harness/` for any ruling on the
re-confirm question — none exists. No UI re-confirm landed in the launcher's
revert path either (per the sweep note; I cannot inspect Dart from here, only
confirm hermes carries no equivalent server-side flag for it).

**Not actionable as a code change.** Genuinely a product decision (does a
second revert on this shape need a confirm dialog) spanning both repos, with
the hermes half (`classify_revert`) already doing exactly what the plan
ruled it should. Nothing to fix without the operator's answer.

**Row 97: KEEP (open decision, both repos — no ruling found anywhere).**

---

## Row 106 — H-H12 bucket-4 narrowing: no census run against a real store

**Asked:** run a census against a real store to confirm the live-binding
clause still catches the measured live shape after `_office_item_id_shape`'s
deletion; also unruled: whether `actors_unreadable` should join the fold's
derivation table (§V1).

**Measured:** `agent_runtime/harness_doctor.py:728` still documents that
`_desk_litter_reason` "replaced ``_office_item_id_shape``"; confirmed no
census artifact exists anywhere under `docs/` (`grep -r` for the row's own
language and for `desk_kind_agent_binding` census wording turned up nothing
beyond the plan and the row itself). The AX5 sub-bullet's §V1 question
(whether `actors_unreadable` should join the fold's derivation table) is
likewise still unruled in `agent_runtime/office_store.py`'s
`_emit_actor_patch` docstring.

**Not actionable as a code change.** A census against "a real store" is an
operational/verification action requiring a live office store this lane does
not have access to (a fresh worktree has no realm data), not a patch. The
§V1 sub-question is a design decision, not a defect.

**Row 106: KEEP (both halves are open — the census is an operational task
needing a real store, and §V1 needs an operator ruling; deletion in source
confirmed, nothing further to fix in code).**

---

## Row 100 — one argparse walker exists twice, in two repos

**Asked:** retire the launcher's copy
(`tool/hermes_cli_contract/dump_hermes_cli_contract.py`) toward hermes'
`scripts/dump_cli_contract.py`, which the launcher's Dart runner should exec
via `--hermes-root`, with a parity gate to keep them equal until then.

**Measured:** this row is entirely `launcher`-scoped — the row's own header
names only the `launcher` repo, and the retirement direction it names ("the
launcher's Dart runner should exec hermes' script") is Dart-side work this
lane cannot make. Checked whether hermes' side of that retirement already
exists, since a one-sided readiness gap would be worth fixing here:
`scripts/dump_cli_contract.py`'s `main()` already accepts `--hermes-root`
(line 343-346) specifically so an external caller can point it at any
checkout — confirmed by reading the option's help text and the file's own
"WHAT THE TWO DUMPS ARE TO EACH OTHER" docstring section, which documents
this exact retirement path as the intended one. So the hermes side of the
prerequisite is already shipped; nothing to build here.

**Row 100: KEEP, hand-back verbatim — no hermes-side action possible or
needed.** The launcher-side patch this row wants, described verbatim for
whoever lands it:

> `tool/hermes_cli_contract/dump_hermes_cli_contract.py` should be deleted,
> and the launcher's Dart runner (whatever calls that script today — the
> `hermes_cli_contract` test/build step) should instead exec hermes'
> `scripts/dump_cli_contract.py --hermes-root <path-to-hermes-checkout>
> --check` (or `--write`), where `<path-to-hermes-checkout>` is however the
> launcher already locates its paired hermes checkout for other cross-repo
> tooling. Until that lands, a parity test in the launcher (e.g. under
> `tool/hermes_cli_contract/` or `test/`) should diff the two scripts' output
> byte-for-byte against a shared fixture repo, so the two copies cannot
> silently diverge before the retirement happens. Evidence for the hermes
> side already being ready: `scripts/dump_cli_contract.py` lines ~338-360
> (`main()`'s `--hermes-root` argument and its docstring).

---

## Summary of hand-backs (verbatim, per the brief's closing protocol)

- **Row 87: DELETE.** Fixed in commit `010d1e7377` (hermes_cli + Electron
  React, both halves, no launcher remainder).
- **Row 89: KEEP** (open decision — lift-marker vs local-until-age-out under
  RD-11; unruled).
- **Row 97: KEEP** (open decision — UI re-confirm on second revert; unruled,
  both repos).
- **Row 106: KEEP** (census needs a real store — operational, not code; §V1
  derivation-table question unruled).
- **Row 100: KEEP**, hand-back verbatim above — launcher-only row, hermes
  prerequisite (`--hermes-root`) already shipped.

## Commits

- `010d1e7377` — `fix(uninstall): warn that the deleted code checkout's git history is not backed up`
- `b4a8d3ae3e` — `tool(scripts): detect drift between the canonical test venv and the live install`

## What is left

- Row 17's helper (`scripts/check_test_env_drift.py`) is a hand-run report,
  never wired into a gate, matching the row's own ask. It was not run
  against the real live/test venvs from this lane (that would need the
  operator's live install path); its pure diff logic is what is tested.
- The TSX test for row 87 is unverified by a real test-runner pass in this
  worktree (see row 87 above) — a genuine gap, not a claimed green. Whoever
  next has a working `apps/desktop` node environment should run
  `npx vitest run src/app/settings/uninstall-section.test.tsx` to close it.
- No main red was found that isn't mine; nothing else observed while working
  these six rows.
