# Field notes — skill-install trigger relocation (hermes-agent)

Running record for the build of
`docs/agent-runtime-harness/planned/skill-install-trigger-relocation.md`
(plan committed `1bd7f05805`). Written as things are found, newest at the
bottom of each stage.

## Pre-build reconnaissance

- **The plan's `_cmd_serve` anchor is right but the literal placement is
  wrong.** `_cmd_serve` (`hermes_cli/harness_parts/serve.py:4630`) calls
  `_claim_protocol_pipes()` (4648) *before* `serve_loop`, and that function
  `dup2`s the null device onto **fd 1**. A `print()` there is not "raw
  stdout" — it is silently discarded. Worse, doing the install before
  `serve_loop` delays the `booting` frame, which this module emits
  deliberately "before ANY heavy boot work" because a launcher watchdog that
  kills a child before `booting` respawns into a cold-boot loop (the
  2026-07-26 kill-loop incident, cited at serve.py:1701-1714). So the install
  runs *inside* `serve_loop`, after `booting`, wired from `_cmd_serve` by the
  same injection contract every other production-only step in this file uses
  (`root_anchor`, `snapshot_prewarm`, `provider_prewarm`, `actor_prewarm`).
- **Why injection and not an unconditional call.** `serve_loop` has a large
  unit-test surface. An unconditional installer would make every one of those
  tests write into the *machine's real* `get_shared_skills_dir()`. The file
  already states this rule for `root_anchor`: "Injected and OFF unless the
  real entry point turns it on … so the loop's unit tests can never write the
  machine-global config." Skill install is the same hazard with the same
  answer.
- **stderr at the chosen site is the real process stderr.** The site is
  before line 2180 (`original_stdout, original_stderr = sys.stdout,
  sys.stderr`), so `sys.stderr` is still the inherited descriptor — the serve
  log lane the launcher captures — and nothing can reach the NDJSON stdout
  from there. That satisfies the plan's stdout discipline by construction
  rather than by care.

- **FALSIFIED — Stage H3's `tests/cli/test_worktree_sync_base.py:165` row.**
  The plan reads that comment as a reference to `.githooks/pre-push` (the
  skill-install hook) and asks for it to be re-pointed at the serve-boot
  install. It is not that hook. The comment is about a **stale worktree base
  ref**, and it echoes `cli.py:1495` — "Genuine staleness is backstopped by
  the pre-push **stale-base** gate" — a different, unrelated thing (there is
  no stale-base hook in `.githooks/` either; the only file there is the
  skill-install `pre-push`). Rewriting it to "backstopped by the serve-boot
  install" would make it false: booting serve does nothing about a worktree
  branched from an old `origin/main`. Left as-is; reported.
- **Doc census is smaller than the plan assumed.** `docs/agent-runtime-harness/08-*.md`
  has **zero** pre-push / skill-install mentions. Only `01-system-architecture.md:320-338`
  needs the correction.
- **`scripts/verify_harness_skill_install.py` is NOT the serve-boot caller.**
  The plan's H1 body is the two installer calls, not a shell-out to the script,
  and that is right: the script's whole first half is the
  refuse-to-guess-`HERMES_HOME` ladder, which exists because a git hook
  inherits an arbitrary shell. A serve process has already resolved its home.
  Shelling out would have paid a second Python start to re-derive an answer the
  boot already holds. The script keeps exactly one caller (`post-merge`), and
  its docstring now says so.
- `docs/agent-runtime-harness/harness-skills/harness-charsheet-authoring/FIELD-NOTES.md`
  mentions the pre-push hook at lines 43, 77, 1372, 1683, 2390, 2479. Left
  untouched on purpose: it is a **dated historical running record** (each
  entry states what was true on its date), and it is a file *inside an
  installed skill package* — editing it changes the package content hash and
  forces a reinstall of `harness-charsheet-authoring` on every machine.

## Stage H1 — install at serve start

- Landed at `4f74f47fe8`. `install_harness_skills_at_boot`
  (`hermes_cli/harness_parts/serve.py`), injected into `serve_loop` as
  `skill_install=` and wired only from `_cmd_serve`.
- Six focused tests in `tests/agent_runtime/test_serve_boot_skill_install.py`,
  all green, plus the 35 pre-existing serve tests unchanged.
- The test that earns the most: `test_boot_installs_before_the_first_request_is_dispatched`
  asserts BOTH halves at once — ordering (`["install", "dispatch"]`) and stdout
  discipline (the summary is in captured stderr and the string "skill install"
  appears in NO frame on the NDJSON buffer).
- `test_cmd_serve_wires_the_real_installer` is the one that would have caught
  the "helper exists, nothing calls it" failure. It fakes `_claim_protocol_pipes`
  (which `dup2`s the null device onto fd 0/1 and would otherwise eat pytest's
  capture) and `serve_loop`, then asserts the kwarg identity.

## Stage H2 — install on git pull

- Landed at `fb7ce83b70`. `.githooks/post-merge`, mode 100755 (set explicitly
  with `git update-index --chmod=+x`; Windows checkouts do not carry the bit).
- Executed for real before committing: `sh .githooks/post-merge` → exit 0,
  `ok — every canonical package installed and current`, home
  `X:\Eternia\.hermes (via env HERMES_HOME)`, 4 canonical packages.
- Dropped `set -e` from the pre-push body when porting it. It was there for the
  ref-reading `while read` loop, which post-merge has no equivalent of; the
  explicit `exit 1` guards and the trailing script invocation give the same
  exit status without it.

## Stage H3 — retire the pre-push hook

- `.githooks/pre-push` deleted. A missing hook file under `hooksPath` is a
  no-op, so no clone needs re-arming; `git config core.hooksPath .githooks` now
  arms `post-merge` instead.
- `scripts/verify_harness_skill_install.py`: the "so this runs from the pre-push
  hook" paragraph now names BOTH new callers and states that serve boot does not
  come through this file at all. Two operator-facing strings that told the
  reader to "push again" now say "run this again" / "restart `harness serve`" —
  the old wording would have sent an operator to a gate that no longer exists.
- `tests/agent_runtime/test_persona_skill_policy.py`: renamed
  `test_installed_canonical_skill_drift_fails_the_pre_push_gate` →
  `…_fails_the_install_verifier`, docstring updated to say the CALLER moved and
  the verifier's behaviour did not. Coverage unchanged — post-merge runs it.
- `docs/agent-runtime-harness/01-system-architecture.md`: the "and the pre-push
  hook is what makes that reliable" sentence is replaced by the four-row trigger
  census, plus the reason boot is the strongest site and the `--rebase` hole.
- `tests/cli/test_worktree_sync_base.py:165` NOT touched — see the falsified
  assumption at the top of this file.

## Pre-existing red, NOT this lane's

- `tests/agent_runtime/test_persona_skill_policy.py` has **three failures that
  predate this work**:
  `test_charsheet_skill_teaches_the_looking_procedure_not_just_the_verbs`,
  `test_charsheet_skill_teaches_all_three_environment_traps`,
  `test_charsheet_skill_teaches_one_install_wide_library_and_no_home_scoping`.
  They assert strings in
  `docs/agent-runtime-harness/harness-skills/harness-charsheet-authoring/SKILL.md`
  (e.g. "Echo the home you resolve; do not assume it.") that the file no longer
  contains — `grep -c` returns 0. That SKILL.md was last edited at `1667841451`
  ("docs(skills): charsheet QA lines — forward-slash paths + scoped action-turn
  replies"), the commit immediately before this lane's base, and the test file
  had not moved since `c58c616227`. This lane's diff to that test file is two
  docstrings and one rename, and it did not touch the skill package at all
  (`git status` on `harness-skills/` is empty). Reported, not fixed — out of
  the plan's scope.
