# 17 — Upstream Boundary Ledger

> **Status: reference ledger, 2026-07-30.** Records every file outside the fork's
> agreed edit boundary that the mission-lane removal (doc 16, S0–S12) touched, with
> ownership evidence and merge guidance for the next `upstream/main` sync. Also records
> the verified revert recipe for the S12 security commit.

Upstream remote: `https://github.com/NousResearch/hermes-agent`, checked at
`upstream/main = e0233f8fc` (2026-07-29). Refactor range: `e471c23d2..25e2651ac`.

## 1. Ownership correction to doc 16

Doc 16's stage rows (S3, S4, S6) describe five files as "upstream-owned" edits made
under operator authorization. Ownership was verified per file with
`git log upstream/main -- <path>` after the fact: **only two of the five are upstream
files.** The other three have never existed in upstream history — they are
fork-created files that merely live outside the `agent_runtime/`/`hermes_cli/harness*`
path prefix, so the per-stage boundary filter surfaced them.

| File | Refactor diff | Nature | Stage | In `upstream/main`? | Merge risk |
|---|---|---|---|---|---|
| `hermes_cli/profiles.py` | −43 | Deletes `_profile_bound_in_live_runtime` and its guard in `delete_profile` (blueprint-binding check) | S7 | **yes** | Low: pure deletion of a fork-added block. Conflicts only if upstream edits the surrounding `delete_profile` region. On conflict, keep the deletion — the guard reads `TaskState`/`mission_plan`, which no longer exist. |
| `hermes_cli/web_server.py` | −79 | Deletes `BlueprintRunRequest`, `GET /api/blueprints`, `POST /api/blueprints/{id}/run` | S7 | **yes** | Low: pure deletion of fork-added endpoints. `/api/profiles/{name}/promote` is untouched and must keep resolving through `agent_runtime/blueprints/resolve.py` (permanent shim). |
| `tools/board_tool.py` | −10/+3 | Drops `mission_goal` docstring pointers and the `linked_goal_id` projection field | S3/S4 | **no — never existed upstream** | None. Fork-created. Note: `TaskStore` is still imported unguarded at `:85` — the permanent `agent_runtime/task_store_stub.py` seam (ruling R-3) serves it. |
| `tools/tool_full_descriptions.py` | net −3 | Removes the `mission_goal_create` entry and its cross-references | S4 | **no — never existed upstream** | None. Fork-created; its "fork-owned mirror" docstring is correct. |
| `scripts/cert_streak.py` | −217 (deleted) | Whole-file delete of the certification streak runner | S6 | **no — never existed upstream** | None. Fork-created. |

**Net fork-sync debt from the mission-lane removal: two files**
(`hermes_cli/profiles.py`, `hermes_cli/web_server.py`), both deletion-shaped.

## 2. Boundary check result (final)

```bash
git diff --name-only e471c23d2..HEAD \
  | grep -vE '^(agent/|agent_runtime/|hermes_cli/harness)' \
  | grep -vE '^tools/(agent_chat_tool|mission_goal_tool)\.py$'
```

Verified 2026-07-30: 168 paths, all accounted for — 8 × `docs/`, 155 × `tests/`
(including regenerated stream-frame fixtures), and the five files in §1. **Zero
unexpected paths.** The baseline must remain `e471c23d2`, not `upstream/main`
(which carries 122 pre-existing out-of-boundary paths from fork history — see doc 16,
final-gate correction).

## 3. Ruling R-3 context note

R-3 ("the `TaskStore` stub is permanent because upstream `tools/board_tool.py` cannot
be edited further") was made on the premise that `board_tool.py` is upstream-owned.
§1 shows it is fork-created. The ruling **stands as issued** — the stub remains — but
the operator may wish to revisit it knowing the import site is editable without merge
risk. Recorded here; no action taken.

## 4. Restoring secret blocking — the verified revert recipe for `933aa3d97`

Commit `933aa3d97` (S12, ruling R-2) removed the `credential_read` /
`credential_exfil` / `prod_operation` hard floors as a single revertable commit.
Revertability was live-verified on 2026-07-30 in an isolated worktree at HEAD
`25e2651ac`:

- `git revert --no-commit 933aa3d97` applies **cleanly for all code, test, and
  companion-doc files** (`agent_runtime/terminal_envelope.py`,
  `terminal_envelope_explain.py`, both test files, both mission-chat docs).
- **One conflict**: `docs/agent-runtime-harness/16-mission-lane-removal.md` — the
  later acceptance commit `25e2651ac` rewrote the same S11/S12 stage-table rows the
  revert wants to restore. Resolve by keeping the HEAD side:

  ```bash
  git revert --no-commit 933aa3d97
  git checkout --ours docs/agent-runtime-harness/16-mission-lane-removal.md
  git add docs/agent-runtime-harness/16-mission-lane-removal.md
  ```

  then amend the stage table by hand to note the floors were restored.
- Under the applied revert, the restored suites pass
  (`tests/agent_runtime/test_terminal_envelope_grants.py` +
  `test_terminal_envelope_explain.py` → **87 passed**) and
  `python -c "import tools.terminal_tool"` succeeds.

Any commit that later touches `terminal_envelope.py`'s grant/floor region should
re-verify this recipe and update this section — the doc conflict above is exactly the
kind of drift that accumulates.
