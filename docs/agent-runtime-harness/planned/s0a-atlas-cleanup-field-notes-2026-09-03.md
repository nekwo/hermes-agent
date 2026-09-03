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

---

# Field notes — S0a atlas cleanup, BUILD pass (2026-09-03)

Running record of the Opus builder that implemented `s0a-atlas-cleanup.md`.
Worktree `X:/wt/s0a-atlas`, branch `feat/s0a-atlas-cleanup`, from
`b2a69f0cc3` (the plan commit). Nothing outside this worktree was written:
the primary checkout, the live venv, and every file under `X:\Eternia\.hermes`
were read only. Nothing pushed.

Python for tests: `X:/Eternia/.venvs/hermes-test/Scripts/python.exe` (the
canonical test env, built 2026-09-03), through `scripts/run_tests.sh` with
`HERMES_PYTHON` — never bare pytest. Live measurements: the same interpreter
with `HERMES_HOME=X:\Eternia\.hermes` from the worktree root, read-only. The
plan's numbers were taken with `C:/Python312/python.exe`; every baseline it
states reproduced exactly on this interpreter, so the two agree where it
matters.

## Commits, in build order

Shas below are POST-REBASE onto `origin/main` `b4a383a1e8` (see "The rebase"
below); the pre-rebase shas the earlier draft carried are dead.

| sha | what |
|---|---|
| `61dd8fbbc3` | A1 — `harness_core` + `expand_toolset_names` + `declared_lane_toolsets`; both permission modes start from the declaration; canon 01/05 |
| `cfb1fbf6d5` | A2 — the persona-level lists go inert and visible (projections, `tool-diff` text mode, one log line per config load) |
| `162b1e7a0d` | A3 — admitted MCP is not a failure (R-S0a-4) + `admitted_mcp_servers` + the ratchet test + the canon 08 ledger row |
| `d5dd25f630` | A4 — the emitter, the three generated artifacts, the tools-first Operate table, the drift gate |
| `ba7157801d` | A6 — `persona_skill_sources` + two `agent list` keys; A6a/A6b measurements recorded |
| `029f6a79ad` | the gate repair the plan named: three stale coverage claims in `planned/serve-small-batch-field-notes-2026-09-02.md` |
| `99ead2f2d1` | the cross-repo stream goldens regenerated for `toolset_declaration` — **launcher byte mirror OWED** |
| `bb780ab1df` | these field notes |
| `18c0720f2b` | nine canon cites re-anchored after the rebase (replaces the pre-rebase `6083c9cca7`, which the rebase dropped as empty — main had re-anchored the same cites for its own shift) |

## The rebase (2026-09-03, after the build)

`origin/main` moved 16 commits while S0a was being built and the branch was
rebased onto `b4a383a1e8`. **Every conflict was a line NUMBER, not a fact**:
main's `characters payload-contract` command (`1455e03c2d`) added +35 lines to
`hermes_cli/harness.py` and +46 to `persona_commands.py`, and main re-anchored
the canon cites for its own shift (`63dade94e2`, `2dc6805041`) — the same cites
S0a had re-anchored for A2's +16 and A6c's +18. Resolution rule: take main's
TEXT on every conflicted hunk (so main's payload-contract work is kept whole),
then re-derive every affected line number from the tree rather than from either
side's arithmetic (`18c0720f2b`). Nine cites moved; the gate is the check, not
the argument.

Both sides' code is present and neither was reverted: `git diff origin/main`
over `hermes_cli/` is exactly S0a's +22 / +16, and `characters payload-contract`
resolves in the CLI contract dump. Regenerating what the rebase moved:

- `scripts/dump_cli_contract.py --write` → **byte-identical** to what main
  committed (`190 command paths, sha256 3473f55341db72d5`). S0a adds no parser,
  so the 190th path is main's new command and nothing of S0a's is missing.
- `scripts/emit_harness_tool_inventory.py --write` → **byte-identical**
  (`43 tools across 15 toolsets, sha256 fb01011e909ab4dc`). Main registered no
  tool into a `harness_core` member toolset, so the inventory did not move.
- The live after-table below re-measured unchanged after the rebase.

## The after-table (live, `HERMES_HOME=X:\Eternia\.hermes`, no profile edited)

`python -m hermes_cli.main harness persona tool-diff <persona> --explain-mcp --json`

| persona | tools | withheld | requirement failures | `model_tool_tokens` | declaration | `admitted_mcp_servers` |
|---|---|---|---|---|---|---|
| `neko_supervisor` | **43** (was 79) | **0** (was 17) | **0** (was 1) | **1149** (was 2142) | `harness_core` / `lane_default` | `[launcher_qa]` |
| `dev` | **43** | **0** | **0** (was 2) | **1149** | `harness_core` / `lane_default` | `[dart, launcher_qa]` |
| `backend_dev` | **43** | **0** | **0** (was 1) | **1149** | `harness_core` / `lane_default` | `[launcher_qa]` |
| `qa` | **43** | **0** | **0** (was 3) | **1149** | `harness_core` / `lane_default` | `[dart, launcher_qa, marionette]` |

`harness persona list --json` agrees: all eleven roster rows report
`tool_count: 43` and the 15 member toolsets (plus `mcp-launcher_qa`, which
`scope_toolsets_to_admission` appends for an admitted persona — pre-existing
behavior, unchanged).

## Deviations from the plan, and why

1. **`harness_core` has FIFTEEN members, not fourteen — `browser-cdp` is named.**
   The plan's A1 snippet listed 14 includes without `browser-cdp`, but its own
   census (§0.1: "`browser` 10 · `browser-cdp` 2 … = the 43 the plan keeps"),
   its 43/1149 ratchet numbers and A4's "15-row table" all count it. Measured
   with the 14: **41 tools / 1096 tokens** in the preview, and a preview/runtime
   DISAGREEMENT — `browser_cdp` and `browser_dialog` are registered under the
   `browser-cdp` toolset while the static `browser` entry also lists them, so
   `model_tools.get_tool_definitions` (via `resolve_toolset`, static ∪ registry)
   would ship 43 on the turn against 41 in the preview. Naming the toolset makes
   both lenses answer 43. The T9b parity test asserts they agree.
2. **The static resolve is 31, not the 36 the plan projected.**
   `resolve_toolset("harness_core", include_registry=False)` = 31 (the 15
   members' static tool lists, minus `agent_chat`/`board` which are registry-only
   and minus the `web_search` overlap between `web` and `browser`). A projection,
   not a decision; the tests assert the measured shape.
3. **The stale-explicit ratchet arm declares `kanban`/`feishu_*`, not
   `hermes-cli`.** The plan expected `[hermes-cli, spotify]` to red `withheld`
   at 17. It does not, and the reason is a real finding (below): the preview
   resolves tools by REGISTRY membership and nothing is registered under the
   name `hermes-cli`, so that declaration previews as 7 tools (spotify only)
   while a turn would ship 62. The anti-vacuity arm therefore names the hygiene
   TOOLSETS directly — 17 withheld, all `registry_hygiene` — and a second test
   pins the `hermes-cli` asymmetry as a documented limit rather than leaving it
   as a surprise.
4. **Tests the plan did not list had to move.** `test_personas.py` (two cases
   asserting `effective_toolsets == persona.toolsets`),
   `test_persona_runtime_fake.py::test_profile_role_sentinel_resolves_to_supervisor_capabilities`,
   `test_state_patches.py::test_open_chat_create_resolves_the_backing_persona_like_the_snapshot_does`
   and `test_unbounded_default_posture.py::test_persona_safety_tools_are_visible_again_under_the_default`
   all asserted the retired posture. Each was repointed at what it was really
   about (the normalizer, the role sentinel, create/snapshot parity, the
   persona-safety unblock) rather than deleted or weakened — the `cronjob` case
   is the sharpest: it is absent now for want of a DECLARATION, not for want of
   permission, so the test asserts it is un-blocked instead of asserting it is
   present.
5. **Two doc-gate repairs the plan did not scope.** Code edits move line
   numbers, and `scripts/doc_cite_adjacency.py` judges a cite by its line: A2's
   +16 lines in `persona_commands.py` and A6c's +18 in `harness.py` broke nine
   canon cites between them. Re-anchored in the commits that moved them
   (`93764979ad`, `6083c9cca7`), gate green in both directions.

## The measurements the queue rows asked for

**A6a — toolset names without the registrars.** `declared_lane_toolsets` reads
static `TOOLSETS` plus the mtime-cached profile YAML and never imports
`model_tools`; asserted in a subprocess (`"model_tools" not in sys.modules`)
in both `tests/agent_runtime/test_toolset_declaration.py` and
`tests/test_toolsets.py`. Timings, this box, hermes-test interpreter:

| what | measured |
|---|---|
| `import model_tools` cold, fresh interpreter | **2597 / 1713 / 2028 ms** (median 2028; the plan recorded 1.61 s on `C:/Python312`) |
| `declared_lane_toolsets` first call (cold profile machinery) | **88.4 ms** |
| `declared_lane_toolsets` warm, mean of 100 | **1.2 ms** |

The tool NAMES still need the populated registry (`final_model_tools`), so
`model_tools` remains on the create path for that half. The manifest that would
take it off is still its own row.

**A6b — prewarm re-measure (the decision rule's input).** Fresh interpreter,
hermetic root, `sample_personas()`; `apply_chat_lane_tool_scope` +
`resolve_tool_visibility`, three runs:

| | run 1 | run 2 | run 3 |
|---|---|---|---|
| cold first resolve (`neko_supervisor`) | 1028.9 ms | 1026.9 ms | 1048.3 ms |
| `warm_persona_memos(dev)` | 9.3 ms | 8.5 ms | 8.5 ms |
| warmed resolve (`dev`) | 7.8 ms | 6.8 ms | 6.6 ms |
| **un**warmed resolve (`backend_dev`) | 9.9 ms | 8.1 ms | 7.5 ms |

The delta a warm buys for a second persona type is **1-2 ms**, far under the
plan's 300 ms threshold. The ~1030 ms of the first resolve is the registry
import, which the prewarm does not remove from a cold process — it only moves
it. **Recorded, not acted on**: the plan says the orchestrator decides whether
that retires the `persona_prewarm` worker, the `runtime.persona.prewarm` verb
and the launcher trigger. Nothing was retired here.

**A6c — skills wait, and the accounting shipped.** `persona_skill_sources`
(`agent_runtime/config.py`) plus two `harness agent list --json` keys. Against
the operator's live config, four of five personas carry config `skills:`
entries their store rows do not have — `dev` six of them
(`aaa-feature-delivery`, `frontend-backend-contract-handoff`,
`launcher-stagec-mcp-screenshot`, `launcher-analyze-proof`,
`harness-handoff-recovery`, `subagent-driven-development`), `backend_dev` five,
`neko_supervisor` three, `qa` one — silently, with no surface that said so
before this row. No write: the store keeps its supersede clock and
`persona set-skills` stays the only writer.

## Findings the build turned up that the plan did not have

1. **The preview and the turn resolve tools through different lenses.**
   `tool_visibility._tool_names_for_toolsets` walks the registry and asks each
   tool which toolset it was REGISTERED into; `model_tools.get_tool_definitions`
   resolves each declared name through `resolve_toolset` (static list ∪
   registry). They agree for a bundle of toolset NAMES (`harness_core`) and
   disagree for a bundle of TOOL names (`hermes-cli`: preview 0, turn 62). This
   never showed while `unbounded` resolved all 32 toolsets. Pinned as a limit in
   `test_harness_core_ratchet.py`; unifying the two resolvers is a row for
   whoever owns it, not this stage.
2. **`blocked_tools_count` on the roster row still reads 17.** The hygiene names
   are no longer CANDIDATES (withheld 0) but they are still in the resolved
   block set, so they count as `policy_tools` and the launcher's drawer scalar is
   unchanged. Not wrong, but "17 blocked / 0 withheld" is a number an operator
   can misread; a row for whoever owns the drawer copy.
3. **`toolset_declaration` on the instance summary is a CROSS-STACK artifact.**
   Five committed stream goldens carry the `persona_instances` row shape, and
   the launcher commits byte-identical copies. The gate caught it; the
   regeneration is `1cf3f80cad`; the launcher mirror is owed in the landing wave
   (the files are named in `tests/fixtures/stream_frames/README.md`). No Dart
   change is needed — the launcher parses those rows by key.
4. **The `dev` and `qa` personas have no chat-mode placement on this box** —
   only `configured` rows (`personainst_dev`, `personainst_qa`). A5's three-role
   recipe therefore cannot run as written without minting, which is why the
   preparation below names `backend_dev` as the second role.

## What I deliberately did NOT do

- Did not tidy the double declaration read. `resolve_tool_visibility` calls
  `declared_lane_toolsets` once for the row and once more through
  `effective_toolsets`; both are the same mtime-cached read (1.2 ms warm), so
  threading one through would save noise and cost a re-run of a green suite.
  Left as-is, recorded here.
- Did not retire anything on the A6b measurement. The number is the
  orchestrator's input, not the builder's licence.
- Did not touch the launcher repo, the primary checkout, the live venv, or any
  operator file under `X:\Eternia\.hermes`. The optional A2 item-5 profile edits
  (delete the persona-level lists, write `toolsets: [harness_core]`) are the
  operator's, and R-S0a-2 is what makes them optional rather than blocking.
- Did not push.

## A5 — navigation proof: PREPARED, NOT RUN (OPERATOR/ORCHESTRATOR-RUN)

A5 needs a live serve this session does not own, and the plan puts it after the
primary is fast-forwarded and the serve restarted. Everything short of the turn
was done: the roster was read, the instances chosen, and the numbers the proof
is measured against are in the after-table above (`model_tool_tokens` 2142 →
1149).

Run per role, from the PRIMARY checkout after the ff, `HERMES_HOME=X:\Eternia\.hermes`:

```powershell
python -m hermes_cli.main harness mission-chat message `
  --persona <persona_id> --persona-instance-id <instance> `
  --new-session --title "S0a nav proof <role>" `
  --client-message-id s0a-nav-<role>-<yyyymmddHHMM> `
  --message "Who are your teammates right now? Send one of them the message 'ping from <role> nav proof' and tell me exactly what they said back." --json
```

Instances that exist today (chat mode, idle):

| role | instance | default chat root |
|---|---|---|
| `neko_supervisor` | `personainst_neko_supervisor_agent_2e94fab3` (or `_f6844ba8`) | `persona_chat_personainst_neko_supervisor_…` |
| `backend_dev` (stands in for `dev`, which has no chat placement) | `personainst_backend_dev_agent_9033415e` | `persona_chat_personainst_backend_dev_age…` |
| `qa` | **none — mint one** with `persona instance open-chat --persona qa --new-session --idempotency-key <key>` | — |

Then read the trace lane, not the prose: `harness snapshot --json` →
`.persona_chat_trace[]` rows with `session_id == <root>`, collecting `tool_name`
for the turn.

**Pass** = the tool-name set ⊆ {`agent_chat_threads`, `agent_chat_send`,
`agent_chat_open`, `agent_chat_log_path`, `agent_chat_dispatches`} ∪
{`read_file`, `search_files`}, contains `agent_chat_threads` AND
`agent_chat_send`, and contains none of `terminal` / `process` /
`execute_code`; the reply names the teammate and quotes its answer.
**Fail** = any terminal row, or a reply that reports the roster without
messaging anyone — and a fail is a MANUAL defect (A4), not a model defect: fix
the row the agent ignored, `python scripts/emit_harness_tool_inventory.py
--write`, rerun that one role. Do not coach the message.

Record, in this file: the tool-name list, `client_message_id`, `turn_id`, root,
and the turn record under `X:\Eternia\.hermes\agent-runtime\mission_chat_turns\`
verbatim.

## What the launcher's Agent Command Atlas regenerates from

`docs/agent-runtime-harness/harness-skills/harness-runtime-model/references/tool-inventory.json`
(schema_version 1; `declared`, `toolsets[]`, `tools[]` with
`name/toolset/mutating/gated/description`, `counts` = 43 tools / 15 toolsets /
1149 tokens, `cli_only_verbs`). The existing artifact
(https://claude.ai/code/artifact/ae544ce0-e881-43a8-9ddf-8b9d68589448) was
hand-built from the 79-tool diff and is now wrong in every count; regenerating
it from this file is the orchestrator's step after landing.

## Verification

| command | result |
|---|---|
| `scripts/emit_harness_tool_inventory.py --check` | `tool inventory fresh: 43 tools across 15 toolsets, sha256 fb01011e909ab4dc` (exit 0) |
| `scripts/doc_cite_adjacency.py --root docs/agent-runtime-harness --exclude=archive/ --exclude=planned/` | `UNWAIVED FAILURES: 0` / `STALE WAIVERS … 0` / probe passed |
| `scripts/dump_cli_contract.py --check` | `CLI contract fresh: 190 command paths, sha256 3473f55341db72d5` (post-rebase; 190 = main's new `characters payload-contract`, and S0a adds no parser) |
| plan §4 unit batch + the new/extended files, pre-rebase (21 files) | `=== Summary: 21 files, 428 tests passed, 0 failed (100% complete) in 112.6s (6 workers) ===` |
| the same batch + `test_cli_contract_dump.py`, POST-REBASE (22 files) | `=== Summary: 22 files, 432 tests passed, 1 failed (100% complete) in 133.9s (6 workers) ===` — the one failure is main's own, classified below |
| `scripts/run_tests.sh tests/scripts tests/hermes_cli tests/agent_runtime -j 8` | `=== Summary: 1019 files, 11716 tests passed, 5 failed (100% complete) in 3604.1s (8 workers) ===`, plus 24 files whose per-test 30 s timeout tripped under load and never ran. Every one re-run and classified below. |

### Every red, classified

Two of the five were MINE and are fixed; the rest are this box under load, or a
pre-existing red on `main`. Nothing was weakened or baselined to make a number.

| red | verdict |
|---|---|
| `test_persona_skill_policy.py::test_charsheet_skill_documents_exactly_the_characters_verbs_hermes_has` (post-rebase) | **RED ON `origin/main` ITSELF, not mine — reported, not fixed.** `AssertionError … Extra items in the right set: 'payload-contract'`. Main's `characters payload-contract` verb (`1455e03c2d`) shipped without adding the verb to the charsheet skill doc this test gates. Classified by checking out `origin/main` (`b4a383a1e8`) in this worktree and running the single test: `1 failed, 25 deselected in 1.76s`. S0a touches neither the `characters` parser nor `harness-charsheet-authoring`. One line in that skill's verb list closes it; it belongs to whoever landed the verb. |
| `test_stream_contract_fixture.py` (2 tests) | **MINE, fixed** (`1cf3f80cad`). A2's `toolset_declaration` is a new key path inside `core.persona_instances.<id>`, and five cross-repo goldens carry that row. The gate caught exactly what it exists to catch. Regenerated with `scripts/generate_agent_runtime_stream_fixtures.py`; **the launcher's byte mirror is OWED** — see the CROSS-STACK COPY STATUS note the commit adds to `tests/fixtures/stream_frames/README.md`. |
| `test_serve_drain_accounting.py` · `test_serve_stream_hub.py` · `test_persona_config_projection.py` | **Contention.** All three passed on a clean re-run of the four files: `4 files, 91 tests passed, 0 failed in 52.5s`. Two are wall-clock serve tests ("no `drain_abandoned` frame within 20.0s"); the third had timed out mid-`git commit` inside `publish_realm_sync`. My own fault for running a second suite beside the first — recorded so the next builder does not. |
| 23 of the 24 "no tests ran" files | **The 30 s per-test timeout under load** (`pyproject.toml` `addopts = --timeout=30`), not failures. Re-run in two clean batches: `24 files, 370 tests passed, 1 failed` then `5 files, 99 tests passed, 1 failed` — the only remaining failure is the row below. The four stubbornest are AST-walk-the-whole-tree tests that take 38-49 s of real work each; run directly, uncontended, they pass: `test_s29_snapshot_dead_local_removal` 6 passed in 43.79 s, `test_s27_snapshot_orphan_tree_removal` + `test_s50_launcher_process_hygiene_removal` 9 passed in 49.37 s, `test_stream_stale_first_routing` 8 passed in 54.50 s. **Classified against the untouched tree**: at `b2a69f0cc3` (the plan commit, checked out in this worktree) the same three files behave identically — two of them only pass on the runner's 1-worker retry. Not caused by S0a. |
| `test_web_server.py::TestBuildSchemaFromConfig::test_no_single_field_categories` | **Was a pre-existing red on `main`; main fixed it in `7c8ace9851` and it is GREEN on the rebased branch** (`1 passed` on a direct run). Original finding kept for the record: **not mine.** `AssertionError: Category 'charsheet' has only 1 field(s) — should be merged`. It reads `hermes_cli.web_server.CONFIG_SCHEMA` and nothing else; S0a touches no file in that module's blast radius (`git diff --name-only b2a69f0cc3..HEAD` has zero `web_server` entries). Deterministic — `1 failed, 99 passed, 4 skipped in 28.95 s` on a direct uncontended run. Reported, not fixed: whoever added the single-field `charsheet` category owns it. |
