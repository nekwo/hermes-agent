# Field notes — coverage claims, the preload ceiling, and one load-flake (2026-09-02)

**Status:** record of a three-row branch, `fix/coverage-claims-skill-ceiling`, cut
from `88da26dfd3`. Not a plan; nothing here is open work.
**Scope:** hermes only. The launcher rows this branch owed are handed back verbatim
in the report, not written here — this session cannot write that vault.

---

## Row 1 — the coverage-claim registry

- **[VERIFIED] The gate is red on `main`, and it is red by NINE, not six.**
  `_unresolved()` over the full corpus returns nine dangling claims. Two of them are
  what the row predicted. One was written from a WRAPPED doc line: the name of the
  class-key fence test in `test_office_class_key_one_fence.py` broke across two lines
  mid-identifier, so the gate read the half before the wrap and found nothing. One was
  written from an ELIDED line — the unreadable-archive re-add test in
  `test_serve_rpc_office_upsert.py` was cited with a trailing ellipsis standing in for
  the rest of its name. A third is the same class in a shape the row did not name: the
  honcho local-setup JWT test in `test_cli.py` was cited with a trailing glob star, and
  the star was the truncation. (None of those three names is spelled here in citable
  form on purpose — this gate reads docs, and an example of a broken citation is
  indistinguishable to it from a broken citation.) **The registry can be broken by
  punctuation**, and all three of those reds came from prose formatting, not from any
  test moving.

- **[VERIFIED] Two of the nine are honest citations that a later DELETION rotted.**
  `tests/agent_runtime/test_projector.py` and
  `test_read_model.py::test_apply_full_rebuild_then_render_is_equivalent` both existed
  at `fac754194e^` and were deleted by `fac754194e` when the read model and the
  projector were retired. Checked, not assumed: `git show fac754194e^:<path>` finds the
  test, `git show fac754194e:<path>` does not find the file. So the sentences were TRUE
  when written, and the docs that carry them (two archived pre-consolidation docs and a
  CLOSED delete schedule) are records. They are annotated with the deletion rather than
  re-pointed, which is what the gate's own failure text asks for.

- **[MEASURED — the row's premise about the SLOWNESS is false] `difflib` is not why
  this needs `--timeout=600`. The corpus walk is.** Three runs, same shell:
  `collect_claims()` 27.4 / 31.0 / 31.3 s (197 md + 4031 py files); `_unresolved()`
  over every claim 1.0 / 1.1 / 1.2 s; the ENTIRE failure report for nine bad claims,
  every `_suggest()` call included, 1.2 / 1.2 / 1.2 s. **The failure path costs 1.2 s
  more than the pass path, not minutes.** What actually kills the file is
  `pyproject.toml`'s repo-wide `addopts` carrying `--timeout=30`: the scan lives in a
  module-scoped fixture, so the FIRST test in the file dies in fixture setup at 30 s,
  green or red, and the only way anyone has run this gate is by hand-passing a bigger
  cap. That is the same unrun-gate shape `AGENTS.md` Testing built the push lane for,
  so the fix is a DECLARED `pytestmark = pytest.mark.timeout(180)` with the measurement
  written beside it, not a flag someone has to remember.

- **[MEASURED] `difflib` is nonetheless the only per-claim cost, and it was unbounded.**
  `get_close_matches` at cutoff 0.5 over the 3002 test filenames: 1.221 s for nine
  calls, 0.135 s each. Sorting that pool nine times: 0.003 s — so the `sorted(by_name)`
  inside `_suggest` was never the cost, and raising the cutoff to 0.85 collapses the
  call to 0.032 s, which locates the expense in the ratio pass rather than the pool
  build. The failure report was O(dangling claims) in that 0.135 s, so a sweep that
  rots fifty citations at once would spend seven seconds on hints nobody reads to the
  bottom of. Bounded with `_SUGGEST_BUDGET = 25`: every unresolved claim is still
  reported in full, only the nearest-name hint is budgeted, and the rows past the
  budget say so in place of the hint.

- **[READ] One of the nine was a WINDOW mis-attribution, and fixing it improved the
  doc.** `tests/scripts/conftest.py` names
  `test_the_mutation_is_spliced_at_the_anchor_not_at_the_first_occurrence` as a bare
  backticked id; the MEMBER arm attached it to
  `tests/test_mutation_gate_worktree_lock.py`, the only resolvable path within three
  lines. The test actually lives in `tests/scripts/test_mutation_claim_anchoring.py`,
  which the docstring never named. Spelling the citation in full fixes the gate AND
  tells the next reader where to look.

---

## Row 2 — a `required_preload` skill's SIZE

- **[VERIFIED] Only `SKILL.md` is in the turn. `references/` are NOT, and the row's
  "SKILL.md + references" would have priced a cost no turn pays.**
  `skill_commands.py::_build_skill_message` appends `content.strip()` — the SKILL.md
  body — verbatim, and then, for `references/`, `templates/`, `scripts/` and `assets/`,
  appends one line per file under `[This skill has supporting files:]` with the
  instruction to fetch one via `skill_view`. The reference BODIES never enter the
  prompt. Counting them in a ceiling would have charged authors for the exact move the
  reference layout exists to reward, and pushed them to inline instead. Measured today:
  `harness-charsheet-authoring` is 22,449 B of SKILL.md inside a 281,553 B package, of
  which 208,687 B is `FIELD-NOTES.md` — installed with the package, in no turn ever.
  **"the package got bigger" is not the question; "the PRELOAD got bigger" is.**

- **[MEASURED] Today's four preloads.** `harness-charsheet-authoring` 22,449 B;
  `harness-runtime-model` 14,414 B; `harness-dev-delivery` 12,932 B;
  `harness-qa-verdict` 10,155 B. Ceilings declared at the next KiB boundary with the
  headroom stated in each entry's reason: 24,576 / 16,384 / 14,336 / 11,264 B.

- **[VERIFIED] The size verdict had to sit NEXT TO the hash verdict, not after it.**
  The first shape returned early on a hash failure and only then read sizes, so a
  package that was both diverged and over budget reported one of the two. Both are now
  computed before the failure block and reported together, and the two advice lines are
  separately gated so a hash-clean overage does not print "restart harness serve".

- **[VERIFIED red-first] The gate reds on a package whose two copies are BYTE-IDENTICAL.**
  That assertion is the one carrying the row: after a repair the hash lane has nothing
  to say (`harness_skill_hash_mismatches` empty) and `main([])` still returns 1. A size
  gate that only fired alongside a divergence would be the hash gate wearing a hat.
  The planted proof ran the other direction too — a ceiling set below today's real
  charsheet size reds the real gate on the real tree.

- **[READ] The live install on this box is ALREADY diverged, and it is not this
  branch's doing.** `--check` against `HERMES_HOME=X:\Eternia\.hermes\profiles\base`
  reports `harness-charsheet-authoring: repo 22449 B | installed 22875 B | DIVERGED`.
  This worktree never touched that `SKILL.md`; the installed copy is 426 B AHEAD of
  `88da26dfd3`, i.e. another branch's work is what is installed. Left alone
  deliberately — repair mode would have overwritten the live shared root every persona
  on this machine reads, with a copy from a worktree that is behind `main`.

---

## Row 3 — `tests/hermes_cli/test_update_autostash.py` at the file timeout

- **[MEASURED] The walk is real, it is `psutil.process_iter`, and the file's wall was
  a function of the BOX, not of the test.** `cmd_update` on Windows calls
  `_pause_windows_gateways_for_update()` and then `_detect_venv_python_processes()`
  (`update_cmd.py`, which runs
  `psutil.process_iter(["pid", "exe", "name", "cmdline", "cwd"])`) for real, and
  `test_cmd_update_skips_stash_restore_when_reset_fails` stubbed neither. Timed in the
  same shell, seconds apart: that walk is **7.46 s over 513 processes** with `cwd`,
  **4.02 s** without it, and **0.01 s** for `pid` plus `name` alone. Per-test durations
  before: 9.09 s of a 10.17 s file, in one test. After stubbing the two seams:
  **0.03 s, file 1.01 s.** Nothing about autostash changed.

- **[READ] Stubbing costs no coverage, and that is checkable rather than asserted.**
  The venv-holder guard's own behaviour — the refuse-and-exit-2 path, the `--force-venv`
  escape, the `--force` NON-bypass — is `tests/hermes_cli/test_update_venv_health.py`,
  which stubs the same two seams for the same reason and was the pattern followed here.

- **[NOT VERIFIED — say it out loud] The six-file batch timeout was not reproduced.**
  The row's symptom (timeout before collection in a six-file batch) is taken from the
  report that filed it. What is proven here is the mechanism and its removal: a
  measured 7.46 s load-dependent walk inside a test with no interest in it, now gone.
  A timeout is a threshold crossing, and taking 9 s out of a 10 s file moves it a long
  way from any threshold, but the original batch was not re-run.

---

## One thing that applies to all three rows

Two of the three premises handed to this branch were right that something was wrong and
wrong about why. The claims row blamed `difflib` for a cost that is 1.2 s against a 31 s
scan; the ceiling row assumed `references/` ride the turn when the code lists their
paths instead. Both were cheap to check and both changed what got built — a declared
timeout instead of a matcher rewrite, and a SKILL.md-only budget instead of a
package-wide one that would have punished the right refactor. `AGENTS.md`'s
verify-the-premise section keeps being paid for, and the measurement that settles it is
usually one `time.time()` apart.
