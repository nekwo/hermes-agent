# 18 — Upstream Merge Rehearsal (2026-07-30)

> **Status: EXECUTED 2026-07-31 — see the executed-merge record at the end of this
> doc.** The conflict table below was the work order for the real sync (merge
> `b9721809e`, upstream `126ff7071`) and is now historical. Original status:
> results of a full `git merge upstream/main` rehearsal run in
> an isolated clone (both rehearsed merges aborted, clone deleted, nothing pushed,
> the working repo never written). Companion to
> [17 — Upstream Boundary Ledger](17-upstream-boundary-ledger.md): doc 17 records
> what the mission-lane removal changed; this doc records the **whole fork's**
> accumulated integration burden.

## Rehearsed refs

| Ref | SHA |
|---|---|
| Fork | `0b7dadf0c` |
| Upstream | `36e41c09e` (current `upstream/main`) |
| Merge base | `9de9c25f6` |
| Divergence | fork 612 commits · upstream 4,745 commits |
| Doc-17 snapshot | `e0233f8fc` — **297 upstream commits behind** current |

> **History folded 2026-07-31.** `main` was rewritten into 7 thematic commits whose
> final tree is byte-identical to the pre-fold tip (`4f06910b5` → fold `0c9d48d9f`).
> The rehearsed fork ref `0b7dadf0c` now resolves only via the archive refs
> `archive/pre-fold-main-20260731` / tag `pre-fold-main-20260731` (both on origin).
> The fold does not change this doc's substance: the merge base is still `9de9c25f6`
> (verified post-fold) and the conflict inventory is a property of the *trees*, which
> are unchanged — a re-rehearsal from folded `main` hits the same conflicts, minus
> whatever upstream has moved since (see Standing guidance #2).

**Result: 59 conflicted files** — 58 content + 1 modify/delete. Only one is named
by doc 17. Cross-check: rehearsing against doc 17's older upstream SHA already
produced 37 conflicts, so 36 predate the doc-17 snapshot and 22 arrived in the
297 newer commits.

## Verdict

**The next sync is high-risk and is not a two-file merge.** Doc 17 remains
accurate about the narrow mission-lane deletion intent, but the fork carries a
much larger pre-existing integration burden. The most dangerous resolutions are
in conversation execution, prompt/cache handling, gateway dispatch, Windows
environment discovery, configuration, and the upstream profile-router refactor —
several require *composing* behaviors rather than choosing ours-or-theirs.
Recommended: a dedicated integration branch plus focused agent, gateway, profile,
Windows, and skill-registry test passes before landing.

## Doc-17 deletion verification (the good news)

- `hermes_cli/profiles.py` — **not conflicted.** The removed
  `_profile_bound_in_live_runtime` / `TaskState` / `mission_plan` references are
  absent on all sides; the deletion merges clean.
- `hermes_cli/web_server.py` — the blueprint deletions (`BlueprintRunRequest`,
  `GET /api/blueprints`, `POST /api/blueprints/{id}/run`) are absent on all
  sides. **But the file still conflicts for a different reason:** upstream
  extracted the profile endpoints to `web_routers/profiles.py`.
  **⚠ At sync time the fork's `/api/profiles/{name}/promote` endpoint and its
  extended profile-creation behavior must be explicitly ported into the new
  profiles router** — the permanent `agent_runtime/blueprints/resolve.py`
  promotion shim only keeps the import alive; the route itself must move.
  Taking either whole side loses functionality.

## Fork-owned collision audit — clean

Current `upstream/main` contains **zero** names under `tools/board_tool.py`,
`tools/tool_full_descriptions.py`, `tools/agent_chat_tool.py`, `agent_runtime/*`,
`hermes_cli/harness*` (tree, change set, and full history all checked). No silent
overwrite risk for the 167 fork-owned paths.

## Conflict inventory

One modify/delete: `hermes_cli/subcommands/postinstall.py` — fork enhanced
Windows Git Bash provisioning; upstream deleted the command. **Product decision
required**: migrate Windows provisioning into the surviving installer before
accepting the deletion, or retain the command.

### Pre-existing fork divergence (36)

| File | Conflict | Recommended resolution |
|---|---|---|
| `agent/chat_completion_helpers.py` | fork cache-scope override vs upstream request/base-URL construction | preserve fork cache-scope header inside upstream's request construction |
| `agent/codex_runtime.py` | fork timing/metering vs upstream relay streaming + interrupt handling (4 hunks) | upstream relay/interrupt flow, fork timing/token-metering hooks around it |
| `agent/conversation_compression.py` | fork session-continuity/lease vs upstream compression recovery | combine continuity with upstream copy-on-write image recovery + current lease semantics |
| `agent/conversation_loop.py` | fork reused user rows/timing vs upstream MoA/cache decoration, transport preflight, reactions (4 hunks) | compose both; keep durable persona-chat reuse, adopt upstream prompt/cache lifecycle |
| `agent/prompt_builder.py` | fork runtime skill metadata/brief-description policy/cache format/project context vs upstream org skills, provenance, fallback-root hardening (6 hunks) | base on upstream registry/provenance, reapply fork runtime metadata + T6b guidance + deterministic context sources |
| `agent/system_prompt.py` | fork TOOL_DESCRIBE_GUIDANCE vs upstream rich-message/static-prefix | include both, preserve upstream prefix-cache ordering |
| `agent/transports/codex.py` | fork cache_scope_id precedence/fingerprints vs upstream bounded prompt-cache keys (3 hunks) | header-only scope + redaction-safe observability via upstream's bounded-key helper |
| `agent/turn_context.py` | replay-safe current-user reuse vs upstream MoA flag + affection reaction | preserve both, update call signature |
| `gateway/platforms/base.py` | fork quoted/null `MEDIA:` handling vs upstream wider marker support | upstream marker tolerance + fork null-byte/path-safety checks |
| `gateway/run.py` | fork queue/notify/session vs upstream per-turn config, slash busy policies, egress, STT caching, voice-only (7 hunks) | subsystem-by-subsystem; upstream dispatch/config APIs, reapply fork queue/completion/reply-anchor guarantees |
| `hermes_cli/commands.py` | fork deprecated aliases + queue commands vs upstream registry-owned busy policies (4 hunks) | upstream CommandDef execution/busy fields, retain fork aliases + queue commands |
| `hermes_cli/config.py` | fork inline defaults vs upstream `config_defaults` extraction | take the extraction, port fork defaults into `config_defaults.py` |
| `hermes_cli/kanban_db.py` | fork crash-artifact capture vs upstream completion heuristics/WAL | keep crash capture alongside upstream reliability logic |
| `hermes_cli/main.py` | fork postinstall/harness commands + return handling vs upstream built-ins + exit propagation (3 hunks) | retain harness; adopt upstream built-ins/return codes; coordinate with the postinstall modify/delete |
| `hermes_cli/mcp_config.py` | fork dotenv/machine-root resolution vs upstream secret scoping | machine-root resolution inside upstream secret scope, no weakened credential isolation |
| `hermes_cli/status.py` | fork provider-key reporting vs upstream alternate env-var tuples | typed fork visibility fields + tuple/first-found resolution |
| `run_agent.py` | fork persistence/persona context vs upstream api_content/display metadata/portal tags | fork continuity fields + upstream row/display/portal fields |
| `scripts/run_tests.sh` | fork Windows/Git-Bash venv handling vs upstream native-Windows probing, glyph-safe I/O, precompile (3 hunks) | upstream runner structure with fork `Scripts/python.exe` + cygpath safeguards |
| `tools/browser_tool.py` | fork brief wire description vs upstream refactoring | concise schema text on upstream implementation |
| `tools/clarify_tool.py` | fork operator/choice semantics vs upstream shared error envelopes | richer clarification contract + upstream `tool_error()` conventions |
| `tools/environments/local.py` | fork Git Bash discovery/native PATH repair vs upstream portable-Git ordering + Windows env construction (5 hunks) | compose; upstream candidate validation first, retain machine-root + native-command reachability |
| `tools/mcp_tool.py` | fork safe env merge order vs upstream secret-source injection + cancellation | document/preserve precedence, retain upstream secret scope + cancellation propagation |
| `tools/session_search_tool.py` | fork concise schema vs upstream call-time DB-path resolution | brief description + live path resolution |
| `tools/skill_manager_tool.py` | fork brief authoring schema vs upstream sync/client changes | brief schema atop upstream actions |
| `tools/skills_sync.py` | fork safe relative install path vs upstream lock-name/install-path tracking | traversal safety + upstream duplicate/bind-mount logic |
| `tools/skills_tool.py` | fork ordered registry/duplicate rejection vs upstream live-profile/org resolution | upstream live registry sources + deterministic duplicate-name refusal |
| `tools/tool_search.py` | fork tool_describe behavior/JSON errors vs upstream deferrable-tool validation (2 hunks) | adopt `tool_error()` + deferrable validation, retain full untrimmed description lookup |
| `tools/voice_mode.py` | fork WSL forwarding detection vs upstream reachable-server + PowerShell-TTS detection | prefer upstream detection, retain fork gateway reporting |
| `tui_gateway/server.py` | fork background-agent gate vs upstream notification requeue/pending-model | fork gate around upstream requeue lifecycle |
| `hermes_cli/subcommands/postinstall.py` | **modify/delete** (see above) | explicit product decision |
| + 6 test files (`test_skill_utils`, `test_system_prompt`, `test_turn_finalizer_final_response_persistence`, `test_platform_base`, `test_subcommands_batch`, `test_hermes_state`) | fork regressions vs upstream suite changes | preserve fork regression cases, adapt to resolved APIs |

### New upstream changes since the doc-17 snapshot (22)

| File | Conflict | Recommended resolution |
|---|---|---|
| `hermes_cli/gateway.py` | fork profile-command matcher vs new upstream Windows command normalization | upstream normalization before the fork-aware matcher |
| `hermes_cli/subcommands/skills.py` | fork `skills link-external` vs upstream moving sync verbs under `hermes sync` | port link-external into the new parser, don't restore retired HSP commands |
| `hermes_constants.py` | fork shared-skills resolver vs upstream `get_hermes_dir()` API change | rebase shared-skills resolution on the new helper |
| `hermes_state.py` | fork live default-path + deterministic ordering vs upstream `_row_id` opt-in/journal changes (2 hunks) | preserve fork path/ordering guarantees inside the new row/journal model |
| + 18 test files (fork regressions vs upstream test pruning — incl. `test_profiles.py`'s deletion-no-longer-blocked test, which **directly verifies doc 17's intended behavior — keep**) | | retain fork regression coverage, adapt fixtures/expectations |

## Standing guidance

1. Do the sync on a **dedicated integration branch**, not on `main`.
2. Re-run this rehearsal first — upstream moves ~300 commits/month against this
   fork; this table decays.
3. The mission-lane deletions themselves are a non-event at merge time; the real
   work is the 36 pre-existing composition conflicts.
4. Port the promotion route into `web_routers/profiles.py` and prove
   `/api/profiles/{name}/promote` green before landing.
5. **Post-sync (operator decision 2026-07-30): consolidate the per-profile
   `mcp_servers` blocks.** After R-1 ("profile declares the server" is the whole
   admission rule) the same `launcher_qa` block is copy-pasted across ~9 profile
   configs — drift between copies is silent. Consolidating requires touching
   upstream-owned config loading (`hermes_cli/config.py` / `mcp_config.py`, both
   already in the conflict table), so it deliberately waits until after this
   sync lands — upstream's `config_defaults.py` extraction may already provide
   the layering. Interim guard candidate: a data-only test asserting every
   profile's `launcher_qa` block is field-identical to `launcher-dev`'s.

## Executed merge — 2026-07-31

Merged `upstream/main = 126ff7071` into the fork (pre-merge tip `1adf0404f`) as
merge commit `b9721809e` on `sync/upstream-20260731`, resolved in a dedicated
short-root worktree by five composition agents with exclusive file ownership,
then hardened by five triage/fix waves. Landed onto `main` ff-only.

### Fresh rehearsal delta vs the table above

Re-rehearsed same-day in an isolated clone before touching anything real:
**63 conflicts vs the table's 59** (merge base `9de9c25f6` unchanged; 86 new
upstream commits since `36e41c09e`). All 35 named non-test files still
conflicted; `postinstall.py` still the lone modify/delete; the fork-owned
collision audit still clean (zero upstream touches on `agent_runtime/`,
`hermes_cli/harness*`, the three fork tools files). New conflicts:
`scripts/run_tests_parallel.py` (fork worker cap vs upstream's new one-shot
file retry — composed with disjoint retry ownership: in-pool retry never
retries timeouts, the fork straggler pass owns them) plus three test files.

### Non-negotiables, as landed

- **Promote endpoint ported** into upstream's new `hermes_cli/web_routers/profiles.py`
  (`ProfilePromoteRequest` declared in the router; registration order preserved
  byte-for-byte; S11 template-vs-defaults behavior verified at the
  `agent_runtime/personas.promote_profile_to_persona` sink; legacy re-export
  block extended). The `agent_runtime/blueprints/resolve.py` shim stays,
  docstring retargeted off `web_server.py:12671`.
- **`postinstall` retained** (fork file + `main.py` wiring restored — upstream's
  clean deletion had auto-merged away the import, registration, and
  `_BUILTIN_SUBCOMMANDS` entry outside the conflict hunks). Retention was cheap;
  no product decision needed. Coherence note: upstream removed pip self-update
  (`d84e11af4`); the command's provisioning work is independent and still valid.
- Doc-17 deletions verified still absent (`_profile_bound_in_live_runtime`,
  `TaskState`, `mission_plan`: zero hits). `task_store_stub.py` untouched.

### Merge-resolution defects found by the gates (all fixed on the branch)

1. `hermes_cli/dep_ensure.py` — merge dropped `import os` (`6ffea44be`).
2. `agent/prompt_builder.py` — merge dropped the fork's guarded
   `skill_matches_environment` import fallback; caught by a fork-owned test
   (`c7dcee760`).
3. `gateway/slash_commands.py` — fork's `/queue-status` handler called the raw
   sync session store from async code; caught by upstream's new AST guard
   (`9df513f70`).
4. `tools/environments/local.py` + `file_operations.py` — upstream's new
   `_bash_safe_path()` argv rewrite contradicted `MSYS_NO_PATHCONV=1`, breaking
   `search_files` content search on Windows AND corrupting backslash-bearing
   patterns; fixed by splitting consumer classes (`_shell_arg_safe_path` emits
   `C:/...` for argv; script constructs keep `/c/...`) (`93dc56bd9`). **The
   contradiction exists in upstream itself — upstream-PR candidate #1.**

### Upstream evolution reconciled (not defects)

`VALID_REASONING_EFFORTS` gained `"ultra"` (fork invalid-sentinel test retargeted
to `"turbo"` + premise assert); the kanban toolset grew 3 attachment verbs
(blocked on agent-runtime lanes like their siblings); upstream's hermetic
test-DB pin (`tests/conftest.py` step 3b) made a fork continuity test's
redundant HERMES_HOME override self-defeating (`39f18bb4a`); the T6b mirror
count was corrected to 34 (`mission_goal_create` was the retired 35th)
(`217df138e`).

### Test-suite reconciliation

- Upstream's two prune waves (`6b81590c5`, `39975613b`: 46,820 → 19,757 test
  functions) drove most test-file conflicts and the post-merge collection drop.
  **Audited: all 8,023 test names dropped across tests/gateway + tests/agent are
  merge-base-owned; zero fork-authored tests lost; the one fork-modified test
  restored** (`e7b32ec18`).
- Env-gap fences rebuilt honestly: `tests/hermes_cli` `_ENV_GAPS` restructured to
  per-cause groups and purged of 163 orphaned + 4 stale rows (55 files/256 rows
  to 45/93) (`ee0426693`); new shared `tests/_env_gap_fence.py` registry now
  also covers tests/agent, tests/gateway, tests/tools (`001338b94`,
  `a9b962ed2`); two Windows rows retired at the source instead of fenced
  (`98cca0334`). Marks deselect only under `-m`; registered tests still run and
  fail loudly on plain pytest, and stale (passing) rows are printed by the
  registry's own detector.

### Final gates at the landed tip

- `python -m pytest tests/agent_runtime -q` → **3,145 passed / 0 failed** (exact
  pre-merge baseline).
- `python scripts/run_tests_parallel.py tests/<dir> -q -m "not windows_env_gap
  and not host_dependency_gap"` → hermes_cli **3,660/0 exit 0** · gateway
  **4,402/0 exit 0** · agent **3,421/0 exit 0** · tools **5,004/0 exit 0**.
  (Count drops vs pre-merge are upstream's prune, per the audit above.)
- Live checkpoints against the merged tree (live venv python +
  `PYTHONPATH=<worktree>`, `HERMES_HOME=X:\Eternia\.hermes\profiles\alice`):
  snapshot schema 2 / 2 boards / 0 warnings; `persona list` 15 total / 11 chat;
  one real Dev mission-chat turn (`run_ids: []`, exact reply, Neko→Dev
  `steered_by` edge in the HUD); Stage C MCP screenshot returned a real
  2560×1400 `MEDIA:` PNG through the QA agent's launcher_qa lane.
- Known operational caveat: `run_tests_parallel.py`'s per-file timeout is
  load-sensitive on this host — concurrent-load reds (e.g. the `lark_oapi`
  import chain) are suspect until rerun serially.

### Upstream-PR candidates extracted from this sync

**Status 2026-08-01: prepared and pushed as fork branches cut from
`upstream/main` (`470cf66b0`), awaiting `gh` install/auth to open the PRs
(ready script + bodies staged in the session scratchpad; branches
`upstream-pr/*` on nekwo/hermes-agent).**

1. `_bash_safe_path` vs `MSYS_NO_PATHCONV` argv contradiction (breaks
   `search_files` on native Windows in upstream too).
   → `upstream-pr/windows-search-arg-pathconv` (`ba1ff49a9`).
2. `run_tests_parallel.py --files` splits Windows drive letters — upstream's own
   retry test fails on Windows without the fix.
   → `upstream-pr/windows-drive-letter-file-list` (`e181dc559`).
3. `tests/hermes_cli/test_gateway.py` module-level `import pty` kills collection
   of the whole file on Windows. **OBSOLETE upstream-ward: upstream fixed it
   themselves (`05504bd9f`, function-local import). FLOWS FORK-WARD instead —
   the fork still carries the module-level import at test_gateway.py:20; adopt
   at the next sync (currently fenced by the env-gap markers, so invisible in
   the canonical runner).**
4. Git-Bash discovery: WSL-stub rejection on the PATH lookup (upstream dropped
   it; a WSL stub passes `_bash_starts()` and then fails every Windows path).
   → `upstream-pr/reject-wsl-bash-stub` (`322c3635c`), stub live-probed on this
   host.
5. Retry-ownership split for the runner (in-pool flake retry re-running timeouts
   pays the full file-timeout twice under contention).
   → `upstream-pr/runner-retry-ownership` (`30d3f0d1e`), deliberately scoped to
   the guard only — the fork's 8-worker cap excluded as host tuning, the serial
   straggler-isolation pass offered in the body as follow-up.

### Post-sync follow-ups (carried forward)

- Standing guidance #5 (consolidate per-profile `launcher_qa` `mcp_servers`
  blocks) is now unblocked: upstream's `config_defaults.py` extraction landed.
- `hermes_state.py` carries two identical default-DB-path resolvers
  (`_resolve_default_db_path` fork / `_default_db_path` upstream) — collapse to
  one authority with an alias.
- `tools/code_execution_tool.py` `build_execute_code_schema` computes
  `tool_lines`/`cwd_note` and never uses them (T6b static-brief residue).
- `tools/skills_sync.py`: upstream's rename-recovery helpers use import-time
  `SKILLS_DIR` while the fork migrated to live `_skills_dir()` — divergent after
  a profile switch; needs a deliberate migration pass.
- `tools/environments/singularity.py:27` resolves `get_hermes_home()` at module
  import time — imports of `tools.terminal_tool` fail under a scrubbed env.
- Packaging: `psutil`/`fire` declared but absent on the ambient interpreter the
  test fences were built against; `markdown` used by the matrix adapter but
  declared nowhere. The env-gap registries are pinned to the ambient
  `C:\Python312` — do not rerun the sweeps under the runtime venv and read the
  stale-row report as truth.
- Fork path-form translators (`_windows_to_msys_path`, `_bash_safe_path`,
  `_shell_arg_safe_path`, `_msys_to_windows_path`) would be safer as one
  `PathForm` value object with explicit consumer classes.
- `gateway/platforms/base.py:1407` `_path_under_denied_prefix` uses bare
  `expanduser("~")` while its sibling honors `$HOME` — aligning widens a denial
  carve-out; operator call.
