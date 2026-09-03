# Field notes — S0a atlas cleanup, planning pass (2026-09-03)

Running record of the planning agent that wrote `s0a-atlas-cleanup.md`.
Worktree `X:/wt/s0a-atlas`, branch `feat/s0a-atlas-cleanup`, at hermes
`504953f6ad` (`origin/main`). Read-only everywhere else; nothing under
`X:\Eternia\.hermes` was written. Python for every measurement:
`C:/Python312/python.exe`; `HERMES_HOME=X:\Eternia\.hermes` for the live
reads.

## What I read, in order

1. The pairing plan (launcher `docs/mission_control/planned/same-account-instant-pairing.md`):
   §1 R-IP9..R-IP13, §3 S0a rows A1–A6, §7 order + queue sweep, §5 ledger.
2. `toolsets.py`: `_HERMES_CORE_TOOLS` (`:31`, 62 names incl. the kanban and
   HA verbs), `TOOLSETS` (`:101`), the `hermes-cli` entry (`:462`),
   `get_toolset` / `resolve_toolset` / `_get_plugin_toolset_names` /
   `get_toolset_names`.
3. Live profiles: every `X:\Eternia\.hermes\profiles\*\config.yaml`
   (`toolsets:`, `agent.disabled_toolsets`, `mcp_servers`, and every
   `agent_runtime.personas.<id>` block), the root `config.yaml`, and the store
   rows `X:\Eternia\.hermes\agent-runtime\agents\*.json`.
4. The admission chain: `agent_runtime/persona_runtime.py`
   (`_blocked_tool_names_for_chat`, `_enabled_toolsets_for_chat`,
   `chat_lane_capability_drops`, `apply_chat_lane_tool_scope`,
   `_CHAT_CAPABILITY_TOOLSETS`), `agent_runtime/tool_visibility.py`
   (`resolve_tool_visibility`, `_requirement_failures`, `_resolved_toolsets`,
   `_estimate_model_tool_tokens`), `agent_runtime/personas.py`
   (`REGISTRY_HYGIENE_BLOCKED_TOOLS`, `PERSONA_BLOCKED_TOOLS`,
   `validate_toolsets`, `effective_toolsets`, `profile_persona_resolution`,
   `all_registered_toolsets`), `agent_runtime/profile_runner.py`
   (`_blocked_tool_names_with_registry_hygiene`, `_enabled_toolsets_for_run`),
   `agent_runtime/mcp_admission.py` (`scope_toolsets_to_admission`,
   `resolve_mcp_admission`, `_requested_servers`,
   `admission_requirement_failures`), `agent_runtime/mcp_lane.py`
   (`mcp_lane_requirement_failures`, `HARNESS_LANE`),
   `agent_runtime/profile_readiness.py` (`declared_mcp_server_names`, the
   `cached_yaml_file(binding.profile_home / "config.yaml")` reader),
   `agent_runtime/tool_permissions.py` (`default_permission_mode`),
   `agent_runtime/config.py` (`load_agent_runtime_config`,
   `persona_records_from_config`, `ensure_persisted_personas`,
   `_persona_from_overrides`, `chat_lane_restore_toolsets`,
   `harness_root_config_path`), `agent_runtime/persona_config_sync.py`
   (`PERSONA_DEF_ALLOWED_KEYS`, `merge_persona_def`),
   `agent_runtime/mission_chat_turn_context.py` (`PERSONA_IDENTITY_FIELDS`),
   `agent_runtime/store.py` (`AgentStore`), `agent_runtime/models.py`
   (`AgentPersona`), `agent_runtime/persona_prewarm.py`
   (`warm_persona_memos`), `tools/registry.py` (`discover_builtin_tools`,
   `get_registered_toolset_names`, `get_tool_names_for_toolset`),
   `model_tools.py` (`_compute_tool_definitions`,
   `get_registered_toolset_names`), `cli.py` (`self.enabled_toolsets`),
   `hermes_cli/config_defaults.py`.
5. Tools: `tools/agent_chat_tool.py` (five `registry.register(...,
   toolset="agent_chat")` calls from `:1479`), `tools/board_tool.py` (two,
   `:173`).
6. The manual: `harness-runtime-model/SKILL.md` whole, `references/operations.md`
   ("Inspecting the runtime", "Operating the live chat lane"), the trace
   projection (`agent_runtime/persona_chat_history.py`: `_TRACE_EVENT_TYPES`,
   `persona_chat_trace_summary`, `_trace_entry`), `agent_runtime/progress.py`
   (`ChatProgressSink`).
7. The CLI: `hermes_cli/harness.py` (`tool-diff`, `set-model`, `snapshot`
   parsers), `hermes_cli/harness_parts/persona_commands.py`
   (`_cmd_persona_tool_diff`, `_persona_by_id`, `_cmd_persona_set_skills`),
   `hermes_cli/harness_parts/runtime_commands.py` (`_cmd_snapshot`).
8. Tests and gates that constrain the build: `tests/test_toolsets.py`,
   `tests/agent_runtime/test_registry_hygiene.py`,
   `test_unbounded_default_posture.py`, `test_chat_lane_toolsets.py`,
   `test_tool_visibility.py`, `test_tool_visibility_import_deferral.py`,
   `test_persona_runtime_fake.py`, `test_persona_prewarm.py`,
   `test_persona_skill_policy.py`, `tests/agent_runtime/conftest.py`
   (fixtures `bundled_persona_profiles`, `bounded_chat_session`),
   `tests/agent_runtime/persona_samples.py`, `tests/hermes_cli/test_cli_contract_dump.py`
   (the emitter + drift-test shape to copy), `tests/test_coverage_claims_resolve.py`
   and `scripts/doc_cite_adjacency.py` (the two doc gates that scan `planned/`).
9. Queue sources: launcher `Launcher_Brain/20 — Active Initiatives/mission-control-queue.md`
   (the three folded rows at lines ~476, ~2020, ~2022) and hermes
   `planned/serve-small-batch-field-notes-2026-09-02.md` §2.

## What I measured

- `tool-diff --json` for `neko_supervisor`, `dev`, `backend_dev`, `qa`
  (JSON in the session scratchpad `tooldiff_<persona>.json`): all four
  `permission_mode unbounded`, `configured_toolsets == effective_toolsets` = 32
  registered toolset names, `final_tool_count 79`, `withheld 17` (all
  `registry_hygiene`), `model_tool_tokens 2142`; `requirement_failures` 1 / 2
  / 1 / 3 rows, every one `mcp_not_registered_on_lane`, servers `launcher_qa`
  (all), `dart` (dev, qa), `marionette` (qa). `persona_toolsets` = the STORE
  row's list (10 / 6 / 6 / 7), not the 20-entry config list.
- `--explain-mcp`: neko `admitted [launcher_qa]`, dev `[dart, launcher_qa]`,
  qa `[dart, launcher_qa, marionette]`, `denied []` for all — root config
  `mcp_admission.enabled: true`. Every requirement-failure row is therefore
  for an ADMITTED server not registered in the CLI process.
- Registry census with `model_tools` imported: 32 toolsets, 96 tools. The 14
  toolsets the plan names for `harness_core` resolve to **43** tools
  (browser contributes 13 incl. `web_search`, `browser_cdp`, `browser_dialog`;
  terminal 7 incl. the five desktop-gated verbs); 34 of the 43 carry a
  `check_fn`; hygiene ∩ core = ∅; `_estimate_model_tool_tokens` → **1149**.
  Outside the core: 17 toolsets, 53 tools (36 callable today + 17 withheld).
  43 + 53 = 96.
- `import model_tools` cold in a fresh interpreter: **1.61 s** (the queue row
  filed 3.36 s on 2026-09-02).
- `get_toolset("messaging")`, `("moa")`, `("launcher_qa")` → `None`;
  `browser-cdp` is a registry-only toolset (2 tools) already inside `browser`'s
  static list.
- Store rows: `agents/{neko_supervisor,dev,backend_dev,qa,base}.json` all
  carry `toolsets`; `base.json` includes `agent_chat` and `board`. None carries
  `updated_at`.
- `harness agent list --json` resolution trace: `default won:
  X:\Eternia\.hermes\agent-runtime` (store root), config read from
  `profiles/alice/config.yaml` (active profile) — the CLI preview measures the
  alice home's config and the shared store; the serve child runs under
  `profiles/base`. Roster/store answers are home-independent; the profile
  `toolsets:` reader I am specifying resolves the PERSONA's bound profile
  (`resolve_persona_profile`), not the active home, so the two agree.

## What surprised me

1. **`hermes-cli` is not why Neko has 79 tools.** The harness lane never reads
   a profile's `toolsets:`; `unbounded` resolves the whole registry
   (`all_registered_toolsets`). R-IP13's number was right, its mechanism was
   the CLI lane's. Consequence: A1 must ADD a reader for the declaration, not
   swap an alias.
2. **All four personas have byte-identical tool surfaces.** The per-persona
   lists (config and store) are consulted by nothing under the default
   posture; `tool-diff`'s `persona_toolsets` is display. The "two declarations
   that disagree" class is really "three copies, zero readers".
3. **Store wins over config, for toolsets exactly as for skills.**
   `ensure_persisted_personas` merges `{**catalog, **stored}`; the 20-entry
   neko list in `profiles/{neko,base,alice}/config.yaml` has never been what
   the runtime saw — the store row's 10-entry list is. Deleting the config
   lists therefore changes nothing on its own; the code must stop reading the
   field (A1 does, via `effective_toolsets`), and the field's DELETION from
   the model is realm-sync work (R-S0a-3).
4. **Every requirement failure is for an admitted server.** The R1 precedence
   "admitted AND registered in this process ⇒ no row" makes the preview report
   the steady state (admitted, torn down between runs) as a failure. A3's zero
   is a precedence fix in `admission_requirement_failures`, not a pruning of
   `mcp_servers:` blocks.
5. **The manual's tool-diff row cannot run as written** (`persona tool-diff
   --json` lacks the positional persona id).
6. **The skills row's mechanism was mis-stated**: `persona_records_from_config`
   does merge config `skills:` additions; the store-wins merge is what hides
   them. And skills have a store verb with a supersede clock, so the toolsets
   answer does not transfer — recorded in the plan as A6c "skills wait".
7. **`messaging` and `moa` in the persona lists are not toolsets** — they have
   been silently ignored by `validate_toolsets` (a normalizer) since they were
   written.

## Decisions I recommended rather than took

R-S0a-1 (which profiles), R-S0a-2 (repo-side lane default for the bare
upstream `[hermes-cli]`), R-S0a-3 (field deletion deferred), R-S0a-4
(admitted ≠ failure), R-S0a-5 (15-row table in the preloaded head, 43-row
table in `references/`), R-S0a-6 (no schema bump). Each has its
recommendation in the plan's §1.

## Things I did not do

- Did not run the suite; the plan's §4 names the files the builder runs with
  `scripts/run_tests.sh` and `HERMES_PYTHON=C:/Python312/python.exe`.
- Did not run the A5 turns — they are the acceptance, after landing.
- Did not measure the prewarm delta — its memo key changes with A1, so the
  measurement is only meaningful after A1 (A6b recipe in the plan).
- Did not touch the primary checkout, the live venv, or any file under
  `X:\Eternia\.hermes`.
