# w18/hc — the three non-test CI jobs of run 33969282189, and the notifier

Lane hc of wave 18. The shared row is the mission-control CI row ("hermes CI on
`main` has not reached a test since 2026-08-04"); lanes ha and hb take the 33
red tests, this lane takes the other three red jobs plus step (2), the notifier.

Every claim below was measured in this worktree unless it says otherwise.

---

## Job 1 — `Python lints / ruff enforcement (blocking)` (job 101314827803)

### The red, reproduced

`ruff check .` on Windows with ruff 0.16.6 reports the SAME 43 errors the
runner did, so nothing here is Linux-only. The blocking gate is literally
`ruff check .` (`.github/workflows/lint.yml`, `ruff-blocking`), and
`pyproject.toml` selects two rules: `PLW1514` and `F821`.

All 43 are `F821` (undefined-name). None are `PLW1514`.

### Verdicts — all 43, no `noqa` anywhere

The row allowed a per-file `# ruff: noqa: F821` header *only if the name really
is bound by the assembler*. **None of the 43 earned one.** Read
`hermes_cli/harness_support.py`'s module docstring and `e887cdf265` first: the
parts are exec'd into `harness.py`'s globals, but since 2026-08-01 each part
carries an **explicit import header** precisely so it does not depend on that.
A part reading a name it did not import is the defect that commit removed,
growing back. So each of the 38 part-file hits is a header omission, and the
cure is the import, not the silencer. The remaining 5 are a plain missing
`typing` import.

| # | File | Name | Count | Verdict |
|---|------|------|-------|---------|
| 1 | `agent_runtime/snapshot.py` | `Any` (317:35, 317:53, 349:29, 369:80, 459:15) | 5 | **BUG — fixed.** `from typing import NamedTuple` never brought `Any`. Annotation-only under `from __future__ import annotations`, so nothing raises today; the first `typing.get_type_hints()` on the module turns four signatures into a NameError. Introduced by `c0dd043ab6`. Fix: import it. |
| 2 | `hermes_cli/harness_parts/persona_commands.py` | `attach_root_observability` (597, 609, 769, 782, 4927, 4941, 4966, 4984, 5974, 5981, 5987, 6008) | 12 | **BUG — fixed.** Resolves at runtime only because `harness.py:82` imports it into the globals the part is exec'd into — the implicit inheritance `e887cdf265` ended. Four commits added calls without extending the header (`a4fc2ed7bc`, `cf35fc59eb`, `748687daa3`, `9c79143346`). Fix: the part imports it, as `harness_parts/gateway_commands.py` already does. |
| 3 | `hermes_cli/harness_parts/runtime_commands.py` | `_print_stage42` (714, 763, 772, 784, 798, 842, 869, 882, 893) | 9 | **BUG — fixed** (same class; see below). |
| 4 | same | `ERROR_EXIT_CODES` (723, 781, 807, 855, 866, 891) | 6 | **BUG — fixed.** |
| 5 | same | `_error_envelope` (715, 773, 799, 843, 883) | 5 | **BUG — fixed.** |
| 6 | same | `_object_envelope` (785, 870, 897) | 3 | **BUG — fixed.** |
| 7 | same | `_sort_rows` (732) | 1 | **BUG — fixed.** |
| 8 | same | `_list_envelope` (740) | 1 | **BUG — fixed.** |
| 9 | same | `_require_yes` (858) | 1 | **BUG — fixed.** |

Rows 3–9 are one defect with seven names. When `e887cdf265` derived
`runtime_commands.py`'s header from its then-real free-name set, the file used
none of the Stage-42 envelope surface, so `harness_repo_root` was its whole
`harness_support` import. `9b2e3696b5` then added the three `harness work`
verbs (list / peek / cancel), which reach for all seven by free name. All seven
are `hermes_cli/harness_support.__all__` exports that `harness.py` imports from
the same module, so re-importing them binds the identical object and the
post-load namespace does not move.

**Total: 43 accounted for, 43 fixed, 0 silenced.**

### The finding worth more than the fix

`harness_support.py`'s docstring said both halves of the contract — that the
header is present, and that it rebinds identically — "are checked by
`tests/hermes_cli/test_harness_parts_namespace.py`", and all six parts repeated
the claim. **Only the second half is true.** That test asks whether a part's
free names resolve in the POST-LOAD namespace, and `harness.py`'s own imports
always supply them, so an incomplete header is invisible to it: 38 undeclared
free names accumulated between 2026-08-01 and 2026-09-05 with all five of its
cases green the whole way.

The gate that catches an incomplete header is **ruff F821**, which reads each
part as the standalone file it is not yet. The docstring now says so, and the
six part headers point at it.

### Commits and gate

- `fa0fb62788` — `agent_runtime/snapshot.py`
- `f29ad6ca6c` — `harness_parts/persona_commands.py`
- `3af990fca7` — `harness_parts/runtime_commands.py`
- `6c629b9122` — the docstring correction (7 files, comment/docstring only)

`ruff check .` → **exit 0, "All checks passed!"** (was exit 1, 43 errors).
`tests/hermes_cli/test_harness_parts_namespace.py` 5 passed;
`tests/agent_runtime/test_snapshot.py` + `test_snapshot_build_logging.py` 23 passed.

---

## Job 2 — `Python tests / Changed-line mutation claims` (job 101314827777)

### What the log actually says

The failing tail is one line, from the `--list` inventory step:

```
mutation-check configuration error: ir-h4-the-instance-family-is-not-addressable-by-revert:
  mutation source not found in agent_runtime/realm_revert.py::FAMILIES
##[error]Process completed with exit code 2.
```

### Which is wrong, the exit or the claim: **the claim.**

Exit 2 is correct and stays. A needle that no longer resolves guards nothing,
and the mutating phase must not start over a registry it cannot anchor — the
gate is right to refuse. There is nothing to relax.

### Four claims, not one

`--list` anchors claims in file order and raises on the first, so the job names
one. Anchoring all 298 against the tree (a throwaway sweep over
`_anchor_claim`) found **four**, three of which were already dead at the CI
commit `e0c01aa4ac` and one that arrived in the four days since:

| Claim | Why the needle stopped resolving |
|---|---|
| `ir-h4-the-instance-family-is-not-addressable-by-revert` | `realm_revert.FAMILIES` gained `DRIFT_FAMILY_FLOW_GRAPH` after `DRIFT_FAMILY_PERSONA_INSTANCE`, so the closing `}` the needle included moved. |
| `iws-ws1-the-activate-events-free-ride-at-an-undeclaring-client` | `TOKEN_GATED_DOMAIN_EVENT_TYPES` gained `office.actor.conflict_resolved` after the two activate rows — same shape. |
| `bipv-the-crossing-refusal-escapes-the-catch-all` | the 2026-09-04 catch-all ruling DELETED the four hand-placed `isinstance` rows the needle named. The same guarantee now lives in the declared-code branch that replaced them, so that is what the sabotage has to break. |
| `dcw-h4-the-autopilot-stream-is-framed-in-indented-blocks` | `ea75d01b77` gave every autopilot receipt the draftsman stamp, so the call is `emit_json_line({**data, **_characters_draftsman()})` now. |

Each repaired needle was proven, not assumed: spliced through the gate's own
`_anchor_claim` and the claim's own test command run against the mutant.

| Claim | baseline | mutant | verdict |
|---|---|---|---|
| `dcw-h4-…-indented-blocks` | exit 0, 1 passed | exit 1, `test_every_autopilot_receipt_is_exactly_one_line` FAILED | KILLED |
| `ir-h4-…-addressable-by-revert` | exit 0, 1 passed | exit 1, `test_the_revert_table_is_total_over_the_new_family` FAILED | KILLED |
| `iws-ws1-…-undeclaring-client` | exit 0, 1 passed | exit 1, `test_the_two_activate_events_are_covered_and_gated_on_the_scope_entity` FAILED | KILLED |
| `bipv-…-the-catch-all` | exit 0, 1 passed | exit 1, `test_the_cross_verb_refusal_is_its_own_exit_family_not_the_unresolved_one` FAILED | KILLED |

Sources were restored in a `finally` and the restore verified byte-for-byte;
`git status` after the four runs showed only `tests/mutation_claims.json`.

### The missing detector

The only thing that noticed four dead guarantees was a CI job that names one
per run — four red main pushes to learn four facts. So
`tests/scripts/test_mutation_claims_still_anchor.py` anchors the whole registry
in one read and reports EVERY drifted claim at once, with the gate's own
re-anchor hint per row.

- **Red recorded** against the pre-repair registry: one failure naming all four.
- **Green** after: 4 passed; the four mutation-gate suites together, 39 passed.
- **Killing mutation:** a real claim's needle lengthened by a line the file does
  not contain is reported by id (`test_a_needle_the_code_moved_past_is_reported_by_name`);
  a claim naming a deleted file is reported rather than crashing the walk.
- Anti-vacuity floors (`MINIMUM_CLAIMS`, `MINIMUM_DISTINCT_PATHS`) are floors,
  not counts, and were met at 298 claims across 111 files.

`derived_at` was deliberately NOT added to the four repaired rows: the honest
sha would be this branch's head, and the landing session rebases, which would
leave four dangling references the derivation reader answers `None` to anyway.

Commit `abaace6a87`. `scripts/changed_line_mutation_check.py --base HEAD~1 --list`
→ **exit 0** (was exit 2).

---

## Job 3 — `Desktop E2E / Playwright E2E (Linux)` (job 101314827640) — HANDED BACK

**Not environmental.** The Electron system deps install, xvfb runs at
1280x1024, and the run reached `34 passed, 2 failed, 1 flaky, 7 skipped` in
4.8 min. There is no workflow step to fix.

**Product assertion, named.** Both failures are the same assertion on the same
locator:

```js
await expect
  .poll(() => page.locator('[aria-label="Background task running"]').count(),
        { timeout: 30_000, message: 'background dot should appear' })
  .toBeGreaterThan(0)          // Expected: > 0   Received: 0
```

- `apps/desktop/e2e/sidebar-states.spec.ts:215` — in *cross-session dot
  transition › background dot transitions to finished when viewing another
  session* (spec at :204)
- `apps/desktop/e2e/tile-unread-bug.spec.ts:61` — in the shared helper
  `startTurnAndSwitchAway`, reached from *tab (hidden) unread is correct*
  (spec at :125)

Both failed on the first attempt AND the retry, so this is not the retry-flake
class: in the same run `image-attachment-resume.spec.ts:167` failed once and
passed on retry, and it was reported as `1 flaky` rather than failed.

**Where to start reading, offered as a lead and NOT as a diagnosis.**
`sessionDotState` (`apps/desktop/src/app/chat/sidebar/session-row-state.ts:12`)
ranks `isWorking` ABOVE `hasBackground`, so the `Background task running` label
can only render once the LLM turn is no longer working —
`session-status-dot.tsx`'s `DOT_VARIANTS` gives each state its own aria-label
and only one dot renders. Both failing call sites poll that label as a
"the turn is running" signal (see the comment at `tile-unread-bug.spec.ts:60`),
which the priority table contradicts. Two sibling polls of the SAME locator
(`sidebar-states.spec.ts:88` and `:154`) passed in the same run, so whatever it
is is narrower than "the dot never appears". The dot became one shared
primitive in `87aaf87748` and was memoized per-session in `fc3af6095f` /
`6fc01f8ab8`; those three commits and the `E2E_SIDEBAR_CROSS` mock scenario
(held background process + subagent, `apps/desktop/e2e/mock-server.ts`) are what
a picker-up should read first.

Nothing was changed for this job. It needs a desktop/TypeScript lane, not a
hermes-Python one.

---

## Step (2) — the notifier (`notify-main-red`)

`.github/workflows/ci.yml`, commit `028aceb9c6`. Exactly the row's shape:
`needs: [all-checks-pass]`,
`if: failure() && github.event_name == 'push' && github.ref == 'refs/heads/main'`,
`actions/github-script` (v9.0.0, pinned by sha) upserting ONE issue titled
`CI red on main` from `all-checks-pass`'s existing `needs-json` output;
`issues: write` at the job and nothing else; no secret, no external service.

No checkout: the job runs zero repository code, which is what lets it hold
`issues: write` in a workflow that also runs on PR-controlled branches.
`needs-json` reaches the script through `env` and is read with `process.env`,
never interpolated into the script body.

Proven offline by extracting the script from the YAML and driving it against
stubbed `github` / `context` / `core` with run 33969282189's real failure set:

- no existing issue → `issues.create`, body naming `` `e2e-desktop` ``,
  `` `lint` ``, `` `tests` ``;
- issue #42 already open → `issues.update` + `issues.createComment`, with an
  open PULL REQUEST carrying the same title correctly ignored;
- malformed `needs-json` → `core.warning` and the issue still opens, with the
  run link and "the gate named none".

`node --check` passes; the workflow parses.

**IT FIRES ON THE NEXT RED MAIN PUSH AND MUST BE WATCHED.** Its condition is a
failure, so the run that lands it cannot exercise it. Two things only a live
run can confirm: that `needs.all-checks-pass.outputs.needs-json` resolves
through the hyphenated output name (the same shape `steps.ci-timings-html.outputs.artifact-url`
already uses in this file), and that the runner honours github-script v9's
`node24`. If either is wrong the job fails visibly in the run — it cannot make
a red run look green.
