# 18 — Upstream Merge Rehearsal (2026-07-30)

> **Status: reference.** Results of a full `git merge upstream/main` rehearsal run in
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
