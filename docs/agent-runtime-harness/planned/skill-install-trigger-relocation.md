# Planned — skill-install trigger relocation (pre-push → serve start + git pull)

**Status:** not built. **Ruling (operator, 2026-08-30):** running
`harness install-harness-skills` from a pre-push hook is the wrong trigger — it
repairs the machine at the moment the *producer* publishes, and never at the
moments a *consumer* acquires drift. "It should do it on launcher or hermes
start or git pull." Relocate it.

## Trigger census today (verified 2026-08-30)

| Trigger | Where | Covers |
|---|---|---|
| explicit CLI | `hermes_cli/harness.py:4652` `_cmd_install_harness_skills` | manual only |
| realm-sync pull | `agent_runtime/realm_sync.py:509-511` | realm members, on realm pull |
| pre-push hook | `.githooks/pre-push` → `scripts/verify_harness_skill_install.py` | the pusher's machine, at push |

**The gap:** a machine that `git pull`s this repo and boots `harness serve`
(launcher-spawned or manual) is repaired by *nothing* until it happens to push
or realm-pull.

**Why serve start is the strongest spot:** the pre-push hook's entire
refuse-to-guess-HERMES_HOME contortion (exit 2, names what to set) exists
because a hook inherits an arbitrary pushing shell. `serve` runs with an
explicitly pinned home — the ambiguity does not exist at boot. Cost is seven
file hashes in an already-running Python.

## Stage H1 — install at serve start

In `hermes_cli/harness_parts/serve.py` `_cmd_serve` (dispatcher shim at
`hermes_cli/harness.py:5766`), at boot, before the request loop accepts its
first request: run exactly what realm-sync pull runs
(`agent_runtime/realm_sync.py:509-511`):

```python
install_harness_skills(skills=sorted(HARNESS_SKILLS))
install_harness_skills_for_personas(ensure_persisted_personas(load_agent_runtime_config()))
```

- **stdout discipline:** serve's stdout is the NDJSON bridge and the module
  owns sys.stdout/sys.stderr swaps (`hermes_cli/harness.py:5767-5768` comment).
  The one-line summary (installed/changed/failed counts) goes to the serve
  log lane / stderr, never raw stdout.
- **Failure posture:** loud, never fatal. A failed install must not stop chat
  from booting — print every failed `SkillInstallResult` line and continue.
  (Contrast with the push gate, which blocked; the boot analogue of "an
  install that did not take fails the push" is a loud boot line, because the
  next boot retries for free.)
- Focused test: serve boot invokes both installers (monkeypatch, mirror the
  shape of `tests/agent_runtime/test_persona_skill_policy.py:597-604`).

## Stage H2 — install on git pull

New `.githooks/post-merge` (hooksPath is already `.githooks`, per-clone
`git config core.hooksPath .githooks` — same install note as the old pre-push
header). Body: the interpreter-resolution + invoke logic from the current
pre-push tail (python/python3 resolution, fail-loud-on-missing-interpreter),
running `scripts/verify_harness_skill_install.py`.

- post-merge cannot block (the merge already happened) — non-zero just prints.
  That is acceptable: it is a repair-and-report lane, and H1 backstops it at
  next boot.
- Known hole, accept and note in the hook header: `git pull --rebase` does not
  fire post-merge. Serve start (H1) covers rebase pulls.
- The script's exit-2-without-a-pinned-home behavior stays as is — it names
  what to set, and that is the correct answer in an unpinned shell.

## Stage H3 — retire the pre-push hook

- Delete `.githooks/pre-push` (relocation, not addition — the ruling calls
  push "a weird place"). A missing hook file under hooksPath is a no-op.
- Update the stale references:
  - `scripts/verify_harness_skill_install.py:26` docstring (names the
    pre-push hook as its caller — now post-merge + serve boot).
  - `tests/agent_runtime/test_persona_skill_policy.py:293,344,355` — rename
    `test_installed_canonical_skill_drift_fails_the_pre_push_gate` and fix
    its docstring; the verifier test itself stays (post-merge now runs it).
  - `tests/cli/test_worktree_sync_base.py:165` comment ("backstopped by the
    pre-push gate" → backstopped by serve-boot install).
  - grep docs (`docs/agent-runtime-harness/01-*.md`, `08-*.md`) for
    pre-push/install mentions and correct them.

## Evidence bar

Focused pytest over the changed modules (serve boot test +
`test_persona_skill_policy.py`), plus one live receipt: run
`scripts/verify_harness_skill_install.py` once by hand and show the ok line.
Commit narrowly (shared-index house rule: stage and commit in one breath). Do
NOT push — operator pushes on order.
