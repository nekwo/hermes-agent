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
- `docs/agent-runtime-harness/harness-skills/harness-charsheet-authoring/FIELD-NOTES.md`
  mentions the pre-push hook at lines 43, 77, 1372, 1683, 2390, 2479. Left
  untouched on purpose: it is a **dated historical running record** (each
  entry states what was true on its date), and it is a file *inside an
  installed skill package* — editing it changes the package content hash and
  forces a reinstall of `harness-charsheet-authoring` on every machine.
