# Wave 10, lane `hermes` — field notes (2026-09-03)

Worktree `X:/Eternia/_worktrees/w10-hermes`, branch `w10/hermes`, base `504953f6ad`.
Six rows, six commits (`501f66c25a`, `7b4a985261`, `22718696da`, `a7cf27ee79`,
`2dc6805041`, `c544782022`). Focused `pytest` only throughout, per the wave's
verification rule; `docs/agent-runtime-harness/planned/hermes-suite-perf.md`
and the retired pre-push hook's own text were the sources for row 3's docs
and row 4's baseline.

## Row 1 — "Owed when the kind is known"

**Claimed:** route the three pre-lease guard refusals in
`_cmd_mission_chat_message` (`unknown_chat_session`, `foreign_chat_session`,
`retired_persona_instance`) through the existing
`_publish_persona_chat_send_refused_event`, carrying `error_kind` +
`client_message_id`, never the operator's text.

**Measured:** the docstring at `_publish_persona_chat_send_refused_event`
(persona_commands.py:2014) named exactly this as owed and unrouted. Found
FOUR call sites producing these three kinds inside `_cmd_mission_chat_message`
(before the chat-root lease is acquired, matching the docstring's
"pre-lease" framing): the explicit-session `UNKNOWN_CHAT_SESSION` fence
(~L2900), the explicit-session `FOREIGN_CHAT_SESSION` fence (~L2946), the
no-session premint gate's retired-target arm
(`_mission_chat_retired_target_refusal`, ~L3052), and the mint's own
`RetiredPersonaInstanceError` catch (a race-defense arm, ~L3097). All four
are before `with persona_chat_root_lease(...)` (~L3208) — confirmed by
reading `_mission_chat_commit_turn`'s own docstring ("Runs under the
chat-root lease") and its caller.

**Changed:** added a `_publish_persona_chat_send_refused_event(...)` call at
each of the four sites, using the already-resolved `session_id` /
`client_message_id` / `normalized_persona` / `persona_instance_id` in scope
(the premint-gate site has no `session_id` yet — passed `None`, matching the
existing "this turn never established one" convention used a few lines away
for the persist-failure arm). The premint-gate site is guarded to fire only
when the refusal's `error_kind` is `RETIRED_PERSONA_INSTANCE` — its sibling
helper `_mission_chat_caller_refusal` can also return `INVALID_REQUEST` /
`INVALID_CHAT_MODEL_OVERRIDE` refusals through the same code path, which are
argument-validation, not chat-root-ownership, and are out of this row's
three named kinds. Updated the docstring's stale "today the sole caller is
chat_busy" claim.

**Tests:** `tests/agent_runtime/test_mission_chat_send_refused_guard_events.py`
(new) — one test per refusal kind (unknown/foreign/retired) asserting exactly
one `persona_chat.send_refused` row lands with the right `error_kind`, the
right `client_message_id`, and the operator's message text nowhere in the
payload; plus one shared test that the registered event contract validates
clean (`validate_event_payload(...) == ()`).

**Commands + exits:**
```
pytest tests/agent_runtime/test_mission_chat_send_refused_guard_events.py -p no:cacheprovider -q
  -> 4 passed
pytest tests/agent_runtime/test_persona_assignments.py tests/agent_runtime/test_persona_spelling_authority.py \
       tests/agent_runtime/test_chat_lease_finalization_tail.py tests/agent_runtime/test_relay_session_lifecycle.py \
       tests/agent_runtime/test_s15_event_contract_pruning.py -p no:cacheprovider -q
  -> 240 passed
pytest tests/agent_runtime/test_agent_chat_dispatch.py tests/agent_runtime/test_agent_chat_log_path.py \
       tests/agent_runtime/test_agent_chat_tool.py tests/agent_runtime/test_mission_chat_turn_context.py \
       tests/agent_runtime/test_mission_chat_turn_run_budget.py tests/agent_runtime/test_persona_chat_actor_prewarm.py \
       tests/agent_runtime/test_s26_retired_mission_chat_task_goal_flags.py \
       tests/agent_runtime/test_s30_retired_mission_chat_task_id_response.py \
       tests/hermes_cli/test_created_agent_first_message.py tests/hermes_cli/test_mission_chat_budget_payload.py \
       tests/hermes_cli/test_mission_chat_capability_visibility.py tests/hermes_cli/test_mission_chat_display_name.py \
       tests/hermes_cli/test_mission_chat_hud_body_live_turn.py tests/hermes_cli/test_mission_chat_records_injection.py \
       tests/hermes_cli/test_mission_chat_relay_guard.py tests/hermes_cli/test_mission_chat_title_offpath.py \
       tests/hermes_cli/test_mission_chat_turn_envelope.py tests/hermes_cli/test_mission_chat_turn_phases.py \
       tests/hermes_cli/test_mission_chat_turn_phase_attribution.py tests/hermes_cli/test_mission_chat_turn_visibility.py \
       tests/hermes_cli/test_mission_chat_usage_single_writer.py -p no:cacheprovider -q
  -> 336 passed
```

**Pre-existing reds proven at base:** none encountered.

**CLOSED.** sha `501f66c25a`.

## Row 2 — `hermes doctor` executes the live profile's `agent-browser.CMD`

**Claimed:** stub doctor's browser resolver for the tests that only want
doctor's report (a seam on `run_doctor`, injected in `test_doctor.py`); then
try deleting the gateway-fence exemption — if the 19 reds go, delete it; if
not, say which remain and why.

**Measured (before):** `hermes_constants.agent_browser_runnable` genuinely
spawns `subprocess.run([path, "--version"], ...)`. `run_doctor` calls it up
to 3 times (PATH rung, managed-node rung, legacy rung) via
`shutil.which("agent-browser")` off the real, unredirected PATH. ~29 of
`test_doctor.py`'s plain-report tests called `run_doctor` with no mock on
this at all.

**Changed:** `run_doctor(args, *, agent_browser_runnable_override=None)` —
`None` (every production call site) falls through to the module global
exactly as before, so the file's two tests that already
`monkeypatch.setattr(doctor_mod, "agent_browser_runnable", ...)` are
untouched. `test_doctor.py` gained `_no_op_agent_browser_runnable` (returns
`False`, spawns nothing) and a `_run_doctor(args, **kwargs)` wrapper that
injects it by default; 29 of the ~32 `run_doctor(...)` call sites in the file
were converted to `_run_doctor(...)` (mechanical, verified against the exact
line list before/after the edit). The 3 left calling `doctor_mod.run_doctor`
directly are the ones that deliberately drive real agent-browser-resolution
behavior (`_run_doctor_with_managed_agent_browser` and the two identical
`test_run_doctor_termux_does_not_mark_browser_available_without_agent_browser`
definitions — an unrelated pre-existing exact duplicate in this file, not
touched) — they already mock `shutil.which` and/or `agent_browser_runnable`
themselves and need the real fallback to keep working.

**Red-first proof, and the "if not, say which remain" half:** temporarily
deleted the exemption clause in `tests/hermes_cli/_gateway_fence.py`
(`and not _is_agent_browser_version_probe(tokens)`), then ran:
```
pytest tests/hermes_cli/test_doctor.py tests/hermes_cli/test_gateway_spawn_fence.py \
       tests/hermes_cli/test_doctor_command_install.py tests/hermes_cli/test_update_autostash.py \
       -p no:cacheprovider -q
  -> 1 failed, 85 passed, 4 skipped (the 1 red is test_the_agent_browser_capability_probe_is_not_refused,
     which exists to pin the exemption and is EXPECTED to red when it is deleted)
```
The 19 `test_doctor.py` reds the fence's own comment named are confirmed
gone. But `hermes_cli/dep_ensure.py`'s `_DEP_CHECKS` (reached from
`cmd_postinstall`, exercised unmocked by
`test_postinstall_noninteractive.py::test_postinstall_json_emits_summary_as_final_line`)
and `hermes_cli/nous_subscription.py` (exercised unmocked by
`test_nous_subscription.py::test_apply_nous_managed_defaults_writes_video_gen_config`)
both call `agent_browser_runnable` straight off `shutil.which` with no seam
of their own:
```
pytest tests/hermes_cli/test_dep_ensure.py tests/hermes_cli/test_dep_ensure_noninteractive_spawn.py \
       tests/hermes_cli/test_nous_subscription.py tests/hermes_cli/test_postinstall_noninteractive.py \
       -p no:cacheprovider -q
  -> 2 failed, 16 passed (both GatewayFenceViolation on the real agent-browser.CMD spawn)
```
**Conclusion: the exemption stays.** Restored the clause (`git diff` on that
file is empty at commit time other than the comment update below). Updated
the exemption's own comment with this measurement and the two remaining
callers by name, so the next pass does not have to re-derive it.

**Tests:** no new test file; `test_doctor.py`'s existing ~49 tests all still
pass, now spawning nothing.

**Commands + exits:**
```
pytest tests/hermes_cli/test_doctor.py -p no:cacheprovider -q -> 49 passed
pytest tests/hermes_cli/test_doctor.py tests/hermes_cli/test_gateway_spawn_fence.py \
       tests/hermes_cli/test_doctor_command_install.py tests/hermes_cli/test_dep_ensure.py \
       tests/hermes_cli/test_dep_ensure_noninteractive_spawn.py tests/hermes_cli/test_nous_subscription.py \
       tests/hermes_cli/test_postinstall_noninteractive.py tests/hermes_cli/test_update_autostash.py \
       -p no:cacheprovider -q  (exemption restored)
  -> 104 passed, 4 skipped
```

**CLOSED, narrower than filed** — the seam landed as asked; the exemption
deletion did NOT land (blocked on two callers with no seam of their own,
named above, not fixed here). sha `7b4a985261`.

## Row 3 — the runner's whole-tree default vs. the validated 4-dir scope

**Claimed:** name the validated four directories in AGENTS.md §Testing's
how-to as the documented default's scope, and give the three environmental
failure classes a written triage. Docs change; measure nothing new.

**Changed:** AGENTS.md §Testing's "Python" subsection gained a "Validated
scope vs. what the runner discovers by default" paragraph naming
`tests/agent_runtime tests/hermes_cli tests/cli tests/state` as what "the
validated suite" means everywhere in the doc, and a triage of the three
classes (provider-network hangs, WSL-bash PATH shadow, acp/ripgrep
dependency holes) with the operational meaning of each — sourced from
`docs/agent-runtime-harness/planned/hermes-suite-perf.md`'s own
2026-09-01 residual entry and the retired pre-push hook's text (`git show
000bbd6179:.githooks/pre-push`), not re-measured.

**Nothing new measured**, per the row's own instruction. No tests touched.

**CLOSED.** sha `22718696da`.

## Row 4 — the mutation-gate timeout tests die inside `ast.walk`

**Claimed:** `test_s27_snapshot_orphan_tree_removal`,
`test_s29_snapshot_dead_local_removal`, `test_s49_operator_control_removal`,
`test_s50_launcher_process_hygiene_removal` (files, not bare function names —
each contains the one heavy test) die inside `ast.walk` at the 30s timeout;
make them cheap and measure before/after.

**Measured (before):** all four together, cold: **94.30s total**, individual
call times 26.84s / 23.94s / 19.79s / 15.21s — under 30s in isolation on this
box, but `tests/agent_runtime/_tree_index.py` already exists (a shared
parse/text/git-lines cache) with an autouse fixture that CLEARS it at every
module boundary specifically to bound memory (785 MB measured 2026-09-01 if
retained for a whole run) — so these four each pay a full, independent
production-tree re-parse.

**Changed:** `tests/agent_runtime/conftest.py`'s
`_drop_tree_index_between_modules` fixture now defers the clear for exactly
these four modules (`_SHARED_TREE_WALK_MODULES`) until every family member
`request.session.items` actually collected has finished — correct regardless
of run order or a partial `-k` selection, and every other `_tree_index`-using
module (5 others) is completely unaffected (still clears every time). All
four also gained `@pytest.mark.timeout(60)` on their heavy test, since any
one of them can be the cold, first-to-run walker depending on collection
order/-k selection and the sharing fix alone does not guarantee that one
stays under 30s under load.

**Measured (after):**
```
the four alone: 70.79s total (was 94.30s). test_s29 5.68s (was 23.94s), test_s50 3.99s (was 15.21s) —
  both reused the OTHER file's warm cache within their errors= cache-key group (strict vs "replace" — two
  groups, not one four-way share, because _tree_index.parsed's lru_cache key includes `errors`).
  test_s27 24.43s and test_s49 26.80s stayed close to their original cost — they are each their
  group's first/cold walker; a full production-tree parse has a real I/O+CPU floor this fix cannot
  remove, hence the timeout bump rather than a claim that they are now cheap in isolation.

the four plus 5 other _tree_index-touching modules (9 files, more load): 115 passed in 187.30s.
  test_s49 measured 32.51s and test_s27 32.23s call time -- genuinely OVER the default 30s ceiling,
  which is exactly the row's claim reproduced under load. Both completed under their new 60s mark
  instead of failing. This IS the red-first proof: without the @pytest.mark.timeout(60), this exact
  9-file run would have reported these two FAILED (timeout).
```

**Tests:** no new test files; `@pytest.mark.timeout(60)` added to the one
heavy test in each of the four files, `import pytest` added to the three
that lacked it.

**Commands + exits:**
```
pytest tests/agent_runtime/test_s27_snapshot_orphan_tree_removal.py tests/agent_runtime/test_s29_snapshot_dead_local_removal.py \
       tests/agent_runtime/test_s49_operator_control_removal.py tests/agent_runtime/test_s50_launcher_process_hygiene_removal.py \
       -p no:cacheprovider -q --durations=15
  -> 21 passed in 70.79s
pytest tests/agent_runtime/test_hermes_home_env_gate.py tests/agent_runtime/test_persona_roster_bypass_contract.py \
       tests/agent_runtime/test_s46_incremental_projection_lane_removal.py tests/agent_runtime/test_s56_runtime_config_reader_gate.py \
       tests/agent_runtime/test_stream_stale_first_routing.py tests/agent_runtime/test_s27_snapshot_orphan_tree_removal.py \
       tests/agent_runtime/test_s29_snapshot_dead_local_removal.py tests/agent_runtime/test_s49_operator_control_removal.py \
       tests/agent_runtime/test_s50_launcher_process_hygiene_removal.py -p no:cacheprovider -q --durations=5
  -> 115 passed in 187.30s
```

**Pre-existing reds proven at base:** none — the row's claimed failure mode
(exceeding 30s under load) was reproduced live in the 9-file run above rather
than proven separately at base, since it is a timing threshold, not a
deterministic red.

**CLOSED.** sha `a7cf27ee79`.

## Rows 5 & 6 — the mutation-gate blind spot + unattended suite runs

Filed as two rows, closed together: the second ("where does the suite run
unattended") is the delivery vehicle for the first ("no lane catches a moved
claim").

**Claimed:** build the unattended run — a Windows scheduled-task definition
under `scripts/` that runs `scripts/run_tests.sh` on the validated lane plus
`scripts/changed_line_mutation_check.py --list --base origin/main`, writing
a dated report to a fixed path under `qa-artifacts/`. The operator enables
the task; this lane does not register it.

**Changed:**
* `scripts/unattended_suite_run.ps1` — resolves Git Bash explicitly (never
  trusts `bash` on PATH, per row 3's WSL-shadow triage) and a python
  interpreter (`$env:HERMES_PYTHON`, then `python`/`python3`, mirroring the
  retired pre-push hook's own resolution order), runs both commands, and
  writes `qa-artifacts/unattended-suite-<UTC timestamp>.md` plus each
  command's raw log beside it. Exit code is informational only (nothing
  gates on it) — the row's own framing: "a scheduled run that REPORTS, not a
  hook that blocks."
* `scripts/hermes-unattended-suite-task.xml` — a Windows Scheduled Task
  definition (daily trigger, 2h execution limit for Lane B's ~18 min +
  margin) with two `REPLACE-ME` markers for the primary checkout's path.
  Its own header comment carries the full import run-book. **Not
  registered** — no `schtasks /Create` was run.
* `qa-artifacts/.gitkeep` + a `.gitignore` entry (`/qa-artifacts/*` with
  `!/qa-artifacts/.gitkeep`) so the directory exists in the tree but its
  dated reports (machine/run-local) are never committed.
* AGENTS.md §Testing gained an "Unattended reporting" subsection between the
  push-gate and CLI-contract-dump subsections, naming both files and what
  each command answers.

**Verified (smoke test, not the real 18-minute scope):** copied the script,
swapped Lane B's arg list for one fast test file, ran it end to end against
the real worktree. Report + both per-command logs written correctly; the
mutation inventory section correctly listed 3 real candidates (this
worktree's own 4 commits ahead of `origin/main` — the exact rows 1/2 landed
here), confirming the inventory lane does real, meaningful work rather than
reporting vacuously. Deleted the throwaway output afterward; nothing under
that test run was committed.

**Collateral, discovered and fixed in the same lane:** row 1's edits added
~46 net lines to `persona_commands.py`, and running
`scripts/doc_cite_adjacency.py --exclude archive --exclude planned` (the
Lane-A gate this whole wave is about — checked here because rows 5/6 are
literally "make the unattended lane meaningful," and shipping it red would
be the same defect one level up) found 3 UNWAIVED FAILURES it caused:
`persona_commands.py:6884` (now `:6930`), `:6416` (now `:6462`), `:4495`
(now `:4541`), cited from `docs/agent-runtime-harness/05-chat-turn-lane.md`
and `07-observability.md`. Verified each old citation matched the base
commit's actual content before re-deriving the new line from a fresh grep,
then confirmed the probe returns to 0 unwaived. Committed separately
(`2dc6805041`) so it reads as its own fix, not folded into row 1 or rows
5/6.

**Commands + exits:**
```
[xml]::Parser -> OK: no parse errors (scripts/hermes-unattended-suite-task.xml)
smoke test: & unattended_suite_run_test.ps1 -RepoRoot <worktree>
  -> "Report written: ...\qa-artifacts\unattended-suite-2026-09-03_160016.md" (deleted after inspection)
python scripts/doc_cite_adjacency.py --exclude archive --exclude planned
  -> UNWAIVED FAILURES: 0 (after the re-anchor commit; was 3 before)
python scripts/dump_cli_contract.py --check -> CLI contract fresh: 189 command paths, sha256 d4aec8ab76ba4ed6
```

**CLOSED.** sha `c544782022` (unattended lane), `2dc6805041` (the doc-cite
collateral fix it surfaced).

## What is left

* Row 2's exemption in `tests/hermes_cli/_gateway_fence.py` still stands —
  `hermes_cli/dep_ensure.py` and `hermes_cli/nous_subscription.py` need a
  seam of their own (or their two exercising tests need one) before it can
  be deleted. Not attempted here; the row asked for the doctor seam and the
  measurement, both delivered.
* Row 3 is docs-only by instruction; the underlying scope-widening decision
  it points at is still an open row in the Mission Control queue, unchanged
  by this pass.
* Rows 5/6's Scheduled Task is not registered on this machine — the
  operator does that, per the wave's instruction, using the XML's own
  run-book.
* Two pre-existing, unrelated findings noticed in passing and NOT touched
  (out of this wave's scope): `test_doctor.py` has an exact duplicate
  function definition (`test_run_doctor_termux_does_not_mark_browser_
  available_without_agent_browser` at two locations, and
  `test_run_doctor_accepts_bare_custom_provider` similarly) — the second
  definition silently shadows the first in pytest collection. Worth its own
  row; not filed here since it is a launcher-facing vault action and this
  lane cannot write `Launcher_Brain/`.

## Row deletions handed back (VERBATIM, for the operator to file/delete in the vault)

Per the wave brief: hermes agents cannot write `Launcher_Brain/`. The
following six row lines in
`Launcher_Brain/20 — Active Initiatives/mission-control-queue.md` are
CLOSED and should be deleted in the same commit as this note's landing
(each carries `**TAKEN 2026-09-03 w10/hermes**`, discharged by the six
commits above):

1. The "Owed when the kind is known" bullet under "OPEN BUG 2026-08-22 —
   first send into a fresh NEW chat rejects, cause unproven" (starts
   "**Owed when the kind is known:** · **TAKEN 2026-09-03 w10/hermes** the
   fix, plus (if it reproduces)..." through its `NARROWED 2026-09-02` text
   ending "...proves which refusal fired.") — CLOSED, sha `501f66c25a`.
2. The "hermes doctor executes the live profile's agent-browser.CMD..." row —
   CLOSED with a NARROWED result: the seam landed and the 19 test_doctor.py
   reds are gone, but the gateway-fence exemption itself was NOT deleted
   (two other unmocked callers, named in the row's own text once filed —
   see this note's Row 2 section for the exact names). If the vault keeps a
   narrowed version rather than deleting, use: "the doctor-side seam landed
   (hermes 7b4a985261); the gateway-fence exemption stays — deleting it also
   reds `test_postinstall_noninteractive.py::
   test_postinstall_json_emits_summary_as_final_line` and
   `test_nous_subscription.py::
   test_apply_nous_managed_defaults_writes_video_gen_config`, both reaching
   `agent_browser_runnable` with no seam of their own."
3. The "scripts/run_tests_parallel.py default-discovers the WHOLE tests
   tree..." row — CLOSED, sha `22718696da`.
4. The "hermes' push lane B cannot go GREEN on the operator box — four whole-
   tree gates time out at base" row — CLOSED, sha `a7cf27ee79`.
5. The "A refactor that moves a claimed line reds hermes' mutation gate for
   everyone, and no lane catches it" row — CLOSED, sha `c544782022`.
6. The "Where does hermes' suite run unattended, now that no push gate runs
   it?" row (under "Same-account instant pairing and the upstream Hermes
   dividend — filed 2026-09-03") — CLOSED, sha `c544782022`. Note for the
   filer: the Scheduled Task is delivered but NOT registered on this
   machine; that is a deliberate operator step, not a gap.
