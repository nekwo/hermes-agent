# Planned — S0a atlas cleanup: one harness toolset, one authority, a manual generated from the registry

**Status: PLANNED — no code touched. Build plan for an Opus builder; written
2026-09-03 against hermes `504953f6ad` (`origin/main`, worktree
`X:/wt/s0a-atlas`, branch `feat/s0a-atlas-cleanup`) and the launcher's
`docs/mission_control/planned/same-account-instant-pairing.md` at `c4b67fcab`
(§1 R-IP13 ADOPTED, §3 "S0a — Atlas cleanup" rows A1–A6, §7 order).**
Consumes **R-IP13** ("Clean the atlas before adding to it? Yes — S0a gates
S2b"). Field notes for this plan:
`s0a-atlas-cleanup-field-notes-2026-09-03.md` beside it. Every number below
was measured on this box on 2026-09-03 with `HERMES_HOME=X:\Eternia\.hermes`
and `C:/Python312/python.exe`; the commands are in §4 so the orchestrator can
re-take them.

The one-paragraph version. On the harness lane nothing reads a profile's
`toolsets:` declaration today: the runtime default posture is `unbounded`, and
`unbounded` resolves **every toolset registered in the process** (32 names, 79
callable tools, 17 withheld, one to three MCP requirement failures) for every
persona alike — Neko, both devs and QA have byte-identical tool surfaces. The
per-persona `toolsets:` lists exist in three places (profile config, the store
rows under `agent-runtime/agents/`, the realm-sync allowlist) and are consulted
by no admission path under that posture. The manual routes agents to the
terminal for things that are tools. S0a makes ONE declaration the authority
(`harness_core`, with a repo-side lane default so the operator's HERMES_HOME
needs no edit), makes the preview's failure accounting truthful (admitted MCP
is not a failure), ratchets withheld and requirement failures to zero with the
per-persona count pinned, and generates the manual's tool inventory from the
registry with a drift gate.

---

## 0. Ground truth (surveyed read-only at the shas above)

### 0.1 The baseline, measured

`python -m hermes_cli.main harness persona tool-diff <persona> --json`, repo
root `X:/wt/s0a-atlas`, `HERMES_HOME=X:\Eternia\.hermes` (active profile
`alice`; the persona rows come from the store under
`X:\Eternia\.hermes\agent-runtime\agents\`, which wins over the config catalog
— §0.4). Raw JSON kept in the field notes' scratch listing.

| persona | `hermes_profile` | `permission_mode` | `configured_toolsets` = `effective_toolsets` | `final_tool_count` | `withheld_tools` | `requirement_failures` | `model_tool_tokens` | `persona_toolsets` (store row, unread by admission) |
|---|---|---|---|---|---|---|---|---|
| `neko_supervisor` | `neko` | `unbounded` (source `cli_preview`) | 32 registered toolsets | **79** | **17** (all `registry_hygiene`: 12 `kanban_*`, 5 `feishu_*`) | **1** — `mcp_not_registered_on_lane` `launcher_qa` | 2142 | 10: file, search, terminal, code_execution, web, browser, session_search, skills, todo, vision |
| `dev` | `gpt-launcher` | `unbounded` | 32 | **79** | **17** | **2** — `dart`, `launcher_qa` | 2142 | 6: file, search, terminal, session_search, code_execution, skills |
| `backend_dev` | `backend-dev` | `unbounded` | 32 | **79** | **17** | **1** — `launcher_qa` | 2142 | 6 (same as dev) |
| `qa` | `launcher-qa` | `unbounded` | 32 | **79** | **17** | **3** — `dart`, `launcher_qa`, `marionette` | 2142 | 7: file, search, terminal, browser, vision, session_search, skills |

The 79 callable tools by registered toolset (identical for all four):
`agent_chat` 5 · `board` 2 · `browser` 10 · `browser-cdp` 2 · `clarify` 1 ·
`code_execution` 1 · `delegation` 1 · `file` 4 · `memory` 1 · `session_search`
1 · `skills` 4 · `terminal` 7 · `todo` 1 · `vision` 1 · `web` 2 (these fifteen
= the 43 the plan keeps) plus `bfl` 6 · `computer_use` 1 · `cronjob` 1 ·
`discord` 1 · `discord_admin` 1 · `hermes-yuanbao` 5 · `homeassistant` 4 ·
`image_gen` 1 · `project` 3 · `spotify` 7 · `tts` 1 · `video` 1 · `video_gen`
3 · `x_search` 1 (36 nobody declared) — and the 17 withheld are `kanban` 12 +
`feishu_doc` 1 + `feishu_drive` 4. 43 + 36 + 17 = 96 = every tool in the
registry. `model_tool_tokens` is the name-envelope heuristic
`_estimate_model_tool_tokens` (`agent_runtime/tool_visibility.py:435`), not a
provider bill.

### 0.2 How the 79 happen — the chain, with the surprise

1. **The posture.** `default_permission_mode` (`agent_runtime/tool_permissions.py:221`)
   reads the ROOT `config.yaml` (`X:\Eternia\.hermes\config.yaml`) —
   `agent_runtime.tool_permissions.default_mode` is absent there, so the
   shipped default `unbounded` applies (canon 05 §4).
2. **The chokepoint.** `_enabled_toolsets_for_chat`
   (`agent_runtime/persona_runtime.py:684`) has two branches: `unbounded` →
   `all_registered_toolsets()`; bounded → `_augment_chat_capabilities(persona,
   effective_toolsets(persona))` → `scope_chat_lane_toolsets` (the cost
   policy). Then `scope_toolsets_to_admission` (`agent_runtime/mcp_admission.py:1013`)
   strips non-admitted `mcp-*` toolsets. The preview walks the same fork:
   `_resolved_toolsets` (`agent_runtime/tool_visibility.py:619`) returns
   `all_registered_toolsets()` when unbounded, else `effective_toolsets(persona)`.
3. **`all_registered_toolsets`** (`agent_runtime/personas.py:249`) is
   `{entry.toolset for entry in registry}` via
   `model_tools.get_registered_toolset_names` (`model_tools.py:1526`) — 32
   names in this checkout, every registrar module that `discover_builtin_tools`
   (`tools/registry.py:68`) imported.
4. **Nothing on this lane reads the profile's `toolsets:` key.** The
   `hermes-cli` alias (`toolsets.py:462`, `"tools": _HERMES_CORE_TOOLS`) is
   consumed by the CLI lane only — `cli.py:4449` stores the config list as
   `self.enabled_toolsets` for `hermes chat`; `hermes_cli/config_defaults.py`
   makes `["hermes-cli"]` the value of an unset key. So R-IP13's sentence "the
   neko profile admits the upstream `hermes-cli` alias, so Neko gets 79 tools"
   is right about the number and wrong about the mechanism: the 79 come from
   `unbounded` = the whole registry, and `hermes-cli` would give 62 (its
   `_HERMES_CORE_TOOLS` list at `toolsets.py:31` minus hygiene). Every
   profile on this box declares `toolsets: [hermes-cli]` (alice,
   aliceimagecron, backend-dev, gpt-launcher, launcher-dev, launcher-qa, neko,
   qa, unbounded); `base` has no `toolsets:` key at all. The correction changes
   the design in one way: **the declaration has to be given a reader on the
   harness lane before it can be the authority** (A1).
5. **`agent_chat`, `board`, `clarify`** ride on both branches:
   `_CHAT_CAPABILITY_TOOLSETS` (`agent_runtime/persona_runtime.py:947`) is
   appended by `_augment_chat_capabilities` on the bounded branch and is simply
   inside "everything" on the unbounded one. `tools/agent_chat_tool.py:1479`
   registers `agent_chat_send` under `toolset="agent_chat"` (and
   `agent_chat_dispatches`, `agent_chat_threads`, `agent_chat_open`,
   `agent_chat_log_path` after it); `tools/board_tool.py:173` registers
   `board_card_add` and `board_cards` under `toolset="board"`. Neither name is
   in the static `TOOLSETS` dict (`toolsets.py:101`) — they are registry-only
   toolsets, which `get_toolset` (`toolsets.py:618`) resolves through
   `_get_plugin_toolset_names` when `include_registry=True` and reports as
   `None` in the static view.
6. **The 17 withheld** are `REGISTRY_HYGIENE_BLOCKED_TOOLS`
   (`agent_runtime/personas.py:81`), unioned at agent construction on every lane by
   `_blocked_tool_names_with_registry_hygiene` (`agent_runtime/profile_runner.py:33`)
   and, for the preview, chosen at `agent_runtime/tool_visibility.py:213` inside
   `resolve_tool_visibility` (`REGISTRY_HYGIENE_BLOCKED_TOOLS if unbounded else
   blocked_tool_names()`). They are withheld every turn only because
   "everything" includes `kanban`/`feishu_*`; a declared set that never names
   them withholds nothing. Hygiene itself is kept untouched.
7. **The requirement failures are all for ADMITTED servers.** `--explain-mcp`
   on the same command: neko `admitted: [launcher_qa]`, dev `[dart,
   launcher_qa]`, qa `[dart, launcher_qa, marionette]`, `denied: []` for all
   three (root config `agent_runtime.mcp_admission.enabled: true`;
   `_requested_servers` (`agent_runtime/mcp_admission.py:838`) = the persona's
   `required_mcp_servers` ∪ the profile's `mcp_servers:` block via
   `declared_mcp_server_names` (`agent_runtime/profile_readiness.py:429`)).
   `admission_requirement_failures` (`agent_runtime/mcp_admission.py:1118`)
   suppresses a row only when the server is admitted **and registered in this
   process** (`frozenset(admission.server_names) & registered`). The CLI preview
   process registers no MCP, and inside the serve `teardown_mcp_admission`
   removes the run's registry scope at the end of every admitted run (canon 05
   §5), so "admitted, not currently registered" is the STEADY state — and it is
   reported as a failure. The fix_hint still says the harness lane "never calls
   discover_mcp_tools()", which predates admission (R1). This is the whole of
   A3's `requirement_failures == 0`: not pruning declarations, but stopping the
   preview from calling an admitted server a failure.

### 0.3 The persona-level lists — three copies, no reader

- **Profile config.** `agent_runtime.personas.neko_supervisor.toolsets` carries
  20 entries in `profiles/{neko,base,alice}/config.yaml` (including `kanban`,
  `messaging`, `moa`, `browser-cdp`, `launcher_qa`-shaped names; `messaging`
  and `moa` are not toolsets — `get_toolset` returns `None` for both);
  `profiles/unbounded/config.yaml` carries a different 5-entry list for the
  same persona; `pm` carries `[]`; `dev`/`backend_dev`/`qa` carry none. Read
  by `persona_records_from_config` (`agent_runtime/config.py:532`) into
  `AgentPersona.toolsets` through `validate_toolsets`
  (`agent_runtime/personas.py:125`, a normalizer — "There is NO role ceiling").
- **Store rows.** `X:\Eternia\.hermes\agent-runtime\agents\{neko_supervisor,
  dev,backend_dev,qa,base}.json` each carry a `toolsets` list (the last column
  of the table in §0.1; `base.json` has 8 including `agent_chat`, `board`).
  `ensure_persisted_personas` (`agent_runtime/config.py:578`) merges
  `{**catalog, **stored}` at `:586` — **store wins** — so the 20-entry config
  list is never what the runtime sees; the preview's `persona_toolsets` shows
  the store row. Nothing in the repo writes `toolsets` to a store row after
  first materialization (`AgentStore.save` at `agent_runtime/store.py:205` is
  reached from `persona set-skills`, `set-model` and the profile-binding
  cascade, none of which touch `toolsets`).
- **Realm sync.** `toolsets` is in `PERSONA_DEF_ALLOWED_KEYS`
  (`agent_runtime/persona_config_sync.py`), so the config list travels to the
  Mac on publish and `merge_persona_def` (`:680`) writes the remote body over
  the member's local one. Deleting the key locally therefore also deletes it on
  the next publish/pull — which is the direction we want, and the reason the
  field cannot be dropped from the MODEL in this stage (§1 R-S0a-3).
- **Readers that DISPLAY it.** `snapshot.py:2564` (`"toolsets":
  effective_toolsets(agent)` on the agents drawer → launcher
  `agent_profile_card.dart` "Toolsets" tag block), `persona_assignments.py:3635`
  (instance summary `"toolsets"` from `visibility_persona`),
  `tool_visibility.py:206` (`persona_toolsets` inside `resolve_tool_visibility`),
  and `PERSONA_IDENTITY_FIELDS` in `agent_runtime/mission_chat_turn_context.py`
  (a change to the field rotates the resident-actor key — harmless, one rebuild).
- **Readers that ADMIT by it.** Only the bounded branch of
  `_enabled_toolsets_for_chat` and `_resolved_toolsets` (via
  `effective_toolsets`, `agent_runtime/personas.py:153`), and the worker/dev task
  lanes ("resolve toolsets via `effective_toolsets` directly", per the
  chokepoint docstring). Under the shipped default none of these run.

### 0.4 The manual, measured against the registry

`docs/agent-runtime-harness/harness-skills/harness-runtime-model/SKILL.md`
(218 lines) — the preloaded model for every mission-chat persona:

- The **Operate** table (line 152) routes "find the on-level chat instances to
  message" to `hermes harness persona list --json` (a terminal call) when
  `agent_chat_threads` answers it in-turn with no mint; "track follow-up work"
  to `hermes harness board card add …` when `board_card_add` is a tool; and
  names exactly ONE tool in the whole table (`agent_chat_send`). It never names
  `agent_chat_threads`, `agent_chat_open`, `agent_chat_dispatches`,
  `agent_chat_log_path`, `board_cards`, `clarify`, `delegate_task`.
- The **View** table (line 108) row "an instance's resolved tools and blocks"
  gives `hermes harness persona tool-diff --json` — the verb requires a
  positional `persona_id` (`hermes_cli/harness.py:1013`, `persona_tool_diff`),
  so the row as written exits 2.
- There is no inventory of in-turn tools anywhere in the package
  (`references/operations.md`'s "Inspecting the runtime" table is CLI-only).
- Pins that must survive any edit (they exist and are green today):
  `tests/agent_runtime/test_persona_skill_policy.py::test_stage59_hud_skill_sections_exist_in_role_skills`
  (heading `## Delegation — helpers without context bloat` must remain) and
  `::test_runtime_model_skill_documents_graph_and_level_agent_commands` (the
  literal strings `hermes harness task show <id> --json`, `` `.mission_plan` ``,
  `mcp_launcher_qa_get_buttons`, `scope=mission_control.agent`,
  `mcp_launcher_qa_get_widget_state`, `widget=mission_control.graph`,
  `status.agents`, `configured/installed Harness agents`, `Neko scope → Backend
  Dev → Launcher Dev`, `QA is a node only if the selected blueprint binds it`
  must remain in SKILL.md).
- The Agent Command Atlas artifact
  (https://claude.ai/code/artifact/ae544ce0-e881-43a8-9ddf-8b9d68589448) was
  hand-built from the 2026-09-03 tool-diff; its "full inventory · 79 callable
  tools for Neko" table is the thing A4's emitter must be able to regenerate.

### 0.5 The three folded queue rows, re-measured

- **Toolset names without importing the registrars.** `import model_tools`
  cold in a fresh interpreter, this box, today: **1.61 s** (the row filed
  3.36 s on 2026-09-02; the AST-verdict cache under `cache/tool_discovery_cache.json`
  was warm both times). It is reached on the create path only through
  `all_registered_toolsets` (chain in
  `planned/serve-small-batch-field-notes-2026-09-02.md` §2). A1 removes that
  call from the default path (the declared set is static), so the toolset-NAME
  half of a create's wire row becomes import-free; the tool-NAME half
  (`final_model_tools`) still needs the registry. The full manifest is "a stage
  not a row" and stays out of S0a — §2 A6a says exactly what ships.
- **Prewarm memo re-measure.** `warm_persona_memos`
  (`agent_runtime/persona_prewarm.py:193`) runs `apply_chat_lane_tool_scope` +
  `resolve_tool_visibility` on a worker to fill process memos. Since 2026-09-02
  the `check_fn` sweep is off the path; what remains warm-able is the
  `_cached_tool_names_for_toolsets` lru (`agent_runtime/tool_visibility.py:521`),
  the 15 s readiness memo and the 60 s provider-issue memo. A1 changes the
  memo KEY (the declared set, not the registry set) so the re-measure must be
  taken after A1 lands — §2 A6b gives the recipe. (The decision it fed is
  RULED 2026-09-04: keep the worker, and no per-persona warm beyond the first.)
- **Persona→skill seed.** `persona_records_from_config` DOES merge config
  `skills:` additions onto catalog defaults (`agent_runtime/config.py:532`,
  the `"skills" in overrides` branch) — the row's "`_persona_from_overrides`
  has no `skills=`" is true of that helper and false of the record; the real
  mechanism is the same `{**catalog, **stored}` store-wins merge as toolsets.
  Unlike toolsets, skills HAVE a store-writing verb with its own supersede
  clock (`_cmd_persona_set_skills`,
  `hermes_cli/harness_parts/persona_commands.py:5795`;
  `AgentPersona.skills_override_issued_at` in `agent_runtime/models.py`) and
  the launcher's Skills console writes through it, so for skills the STORE is
  the authority by design. §2 A6c says why skills wait and what ships instead.

---

## 1. Sub-rulings the builder must not decide alone

Each carries a recommendation; the orchestrator (or operator) confirms before
the builder starts the stage that consumes it. Everything else in §2 is
decided.

| id | question | recommendation | consumed by |
|---|---|---|---|
| **R-S0a-1** | Which profiles switch to `harness_core`? | The four persona-bound profiles (`neko`, `gpt-launcher`, `backend-dev`, `launcher-qa`) and `base` (the serve child's HERMES_HOME). Operator CLI profiles (`alice`, `unbounded`, `qa`, `launcher-dev`, `aliceimagecron`) keep `hermes-cli` — they are `hermes chat` homes, and the CLI lane is out of scope. **With R-S0a-2 adopted the operator need not edit any of them** to land S0a; the explicit `toolsets: [harness_core]` write is recommended for legibility, not required. | A1, §5 |
| **R-S0a-2** | Repo-side lane default: what does the harness lane do when a profile's `toolsets:` is absent, empty, or exactly the upstream default `["hermes-cli"]`? | Resolve `harness_core` and report `toolset_declaration.source = "lane_default"`. `["hermes-cli"]` is what `hermes_cli/config_defaults.py` writes for an unset key, so treating the bare default as "undeclared" is reading the upstream default as the default it is, not rewriting an operator's choice; any other list (including `[hermes-cli, spotify]`) is honored verbatim as `source = "profile_config"` — and a list that still names `hermes-cli` resolves `kanban` and reds the A3 ratchet on `withheld`, which is the operator's cue to write the explicit set. This is what makes the HERMES_HOME edit unnecessary on this box AND on the Mac replica. | A1, A2 |
| **R-S0a-3** | Delete the `toolsets` field from `AgentPersona` now? | **No — this stage makes it inert and visible, a follow-up row deletes it.** Deleting the field touches the store schema, `PERSONA_DEF_ALLOWED_KEYS` (realm sync with the Mac), `PERSONA_IDENTITY_FIELDS`, three wire projections and the launcher's `MissionAgentInstance.toolsets`. In S0a: admission stops reading it (A1), every projection that displayed it reports the declared set and labels the legacy list (`persona_toolsets_in_force: false`), the operator deletes the config lists (5 files) as an optional hygiene step, and the store rows are left alone (no writes on read). Field notes carry the reasoning R-IP13 asked for ("deleted or a red when it diverges" — a list with no reader has nothing to diverge FROM). | A2 |
| **R-S0a-4** | `requirement_failures` for an ADMITTED MCP server that is not registered in the resolving process — row or no row? | **No row.** Precedence 1 of `admission_requirement_failures` becomes "admitted (and not denied) ⇒ no row"; per-run registration is receipted where it happens (`mcp_admitted_servers` / `mcp_admission_transport` on the turn record, canon 05 §5). The preview reports the admitted names under a new additive key `admitted_mcp_servers` so the operator still sees them. A denied server keeps its denial row; an undeclared-and-unadmitted server keeps the R0 row. Cross-persona silencing is impossible because `admission.server_names` is already per-persona. | A3 |
| **R-S0a-5** | Where does the generated inventory live, and does the manual carry all 43 rows? | In SKILL.md as a new `## In-turn tools` section between generated markers, **one row per toolset with the tool names inline** (15 rows), not 43 rows — SKILL.md is preloaded into every turn (`load_policy: required_preload`) and 43 rows with descriptions is prompt weight the operator pays per turn. The 43-row table WITH descriptions goes to `references/tool-inventory.md` (generated, same emitter), and the machine copy to `references/tool-inventory.json` (the Atlas regenerates from it). Both files and the SKILL.md block are `--check`-gated. | A4 |
| **R-S0a-6** | `TOOL_VISIBILITY_SCHEMA_VERSION` (`agent_runtime/tool_visibility.py:35`, currently 2) — bump for the additive keys? | Do not bump. `toolset_declaration`, `admitted_mcp_servers`, `persona_toolsets_in_force` are additive; the launcher reads `effective_toolsets`/`configured_toolsets` by key (`mission_control_snapshot.dart:4830`) and ignores unknown keys. Bump only if a test in `tests/agent_runtime/test_tool_visibility.py` pins the key SET (the builder checks; none was found in this survey). | A3 |

---

## 2. Stages

Order: **A1 → A2 → A3 → A4 → A6 → A5.** A5 is the acceptance and runs last,
against the live serve, after the primary is fast-forwarded. Each stage is its
own commit in the worktree; the orchestrator lands them together.

### A1 · `harness_core`: one declared toolset, read on the harness lane

**Files:** `toolsets.py`, `agent_runtime/personas.py`,
`agent_runtime/persona_runtime.py`, `agent_runtime/tool_visibility.py`,
`agent_runtime/mcp_admission.py` (docstring only),
`agent_runtime/persona_prewarm.py` (docstring only), canon
`docs/agent-runtime-harness/05-chat-turn-lane.md` §4.

1. **`toolsets.py`** — add to `TOOLSETS` (`toolsets.py:101`), beside the other
   composite entries:

   ```python
   "harness_core": {
       "description": (
           "Mission Control harness lane: the fork's agent-to-agent chat and board "
           "tools plus the conversational core. The ONE toolset an Eternia persona "
           "profile declares; integrations (spotify, discord, homeassistant, "
           "yuanbao, bfl, video_gen, computer_use, cronjob, image_gen) are opt-in by "
           "name beside it. Membership is by toolset so a tool registered into one "
           "of these later joins without an edit here."
       ),
       "tools": [],
       "includes": [
           "agent_chat", "board", "clarify", "delegation", "terminal", "file",
           "web", "browser", "skills", "memory", "todo", "session_search",
           "vision", "code_execution",
       ],
   },
   ```

   `agent_chat` and `board` are registry-only names; `resolve_toolset`
   (`toolsets.py:719`) already resolves an include through `get_toolset(...,
   include_registry=True)`, so with the registry populated `harness_core`
   resolves to **43 tools** (measured: agent_chat 5, board 2, clarify 1,
   delegation 1, terminal 7, file 4, web 2, browser 13 incl. `web_search`,
   `browser_cdp`, `browser_dialog`, skills 4, memory 1, todo 1, session_search
   1, vision 1, code_execution 1; union 43; `_estimate_model_tool_tokens` →
   **1149**). In the static view (`include_registry=False`) it resolves to 36
   (the two registry-only includes contribute nothing) — the existing
   `TestResolveToolsetIncludeRegistry` shape in `tests/test_toolsets.py`.

   Add a pure helper (no registry access):

   ```python
   def expand_toolset_names(names: Iterable[str]) -> List[str]:
       """Replace a composite toolset that has ``includes`` and no direct ``tools``
       with its member toolset NAMES, recursively, order-preserving, deduped.
       Leaf toolsets, registry-only names and unknown names pass through unchanged.
       Static: reads only ``TOOLSETS``."""
   ```

   `expand_toolset_names(["harness_core"])` → the 14 include names. This is
   what lets the bounded lane's cost policy (`scope_chat_lane_toolsets`,
   `agent_runtime/chat_lane_toolsets.py:321`, drops by toolset NAME) and the
   preview's per-toolset accounting keep working on a bundle — a list
   `["harness_core"]` would slip past a policy that removes `browser`.

2. **`agent_runtime/personas.py`** — the ONE declaration reader:

   ```python
   HARNESS_LANE_DEFAULT_TOOLSETS: tuple[str, ...] = ("harness_core",)

   @dataclass(frozen=True)
   class ToolsetDeclaration:
       toolsets: tuple[str, ...]          # expanded member names, validated
       declared: tuple[str, ...]          # what the config literally said (or the default)
       source: str                        # "profile_config" | "lane_default" | "profile_unresolved"
       profile: str | None
       config_path: str | None
       def row(self) -> dict[str, Any]: ...  # the wire shape

   def declared_lane_toolsets(persona: AgentPersona) -> ToolsetDeclaration:
   ```

   Reads `<profile_home>/config.yaml` top-level `toolsets:` through the SAME
   reader `profile_readiness` uses (`resolve_persona_profile(persona)` +
   `cached_yaml_file(binding.profile_home / "config.yaml")`, the pattern at
   `agent_runtime/profile_readiness.py:148`). Rule (R-S0a-2): value absent,
   not a list, empty, or equal (as a set) to
   `hermes_cli.config_defaults.DEFAULT_CONFIG["toolsets"]` → `declared =
   HARNESS_LANE_DEFAULT_TOOLSETS`, `source = "lane_default"`; profile home
   unresolvable → same default, `source = "profile_unresolved"`; anything else
   → `source = "profile_config"`, verbatim. `toolsets =
   validate_toolsets(expand_toolset_names(declared))`. Never raises; a YAML
   fault resolves to the lane default (fail toward the narrow known set, the
   same asymmetry `default_permission_mode` applies).

   `effective_toolsets` (`agent_runtime/personas.py:153`) becomes:
   `return list(declared_lane_toolsets(persona).toolsets)`. That retires the
   persona field as an admission input in ONE place — every existing caller
   (bounded chat branch, `_resolved_toolsets`, snapshot agents drawer, worker
   lanes) follows. `all_registered_toolsets` stays (the `mcp_admission`
   isolation tests and the A6a measurement use it) but leaves the default path.

3. **`agent_runtime/persona_runtime.py`** — `_enabled_toolsets_for_chat`
   (`:684`): both branches start from the declaration:

   ```python
   declared = list(effective_toolsets(persona))
   resolved = _augment_chat_capabilities(persona, declared)   # idempotent: all three are core members
   if not permission_mode_is_unbounded(options.permission_mode):
       resolved = scope_chat_lane_toolsets(resolved, restore=chat_lane_restore_toolsets(persona.id))
   ```

   `unbounded` still bypasses the cost policy and the blocklist
   (`_blocked_tool_names_for_chat`, `:628`, unchanged); what it no longer does
   is resolve the whole registry. `apply_chat_lane_tool_scope` (`:879`):
   `configured = _augment_chat_capabilities(persona, list(effective_toolsets(persona)))`
   on both modes (the `all_registered_toolsets()` arm goes). Rewrite the three
   docstrings that say "unbounded resolves `all_registered_toolsets()`" (the
   chokepoint, `apply_chat_lane_tool_scope`, `chat_lane_capability_drops` at
   `:733`) and the same sentence in `scope_toolsets_to_admission`
   (`agent_runtime/mcp_admission.py:1013`) and in the `persona_prewarm` module
   docstring. The admission scope stays LAST and unchanged — the isolation
   property ("no permission mode widens the admitted MCP set") is now trivially
   true because the declared set contains no `mcp-*` name unless admitted.

4. **`agent_runtime/tool_visibility.py`** — `_resolved_toolsets` (`:619`):
   `options.enabled_toolsets` if set, else `effective_toolsets(persona)` for
   BOTH modes. In `resolve_tool_visibility` (`:194`) add the additive keys
   `toolset_declaration: declared_lane_toolsets(persona).row()` and
   `persona_toolsets_in_force: False` (always false after this stage; the key
   exists so the launcher can stop rendering `persona_toolsets` as authority).
   Keep `persona_toolsets` as-is (legacy, display).

5. **Canon** — `05-chat-turn-lane.md` §4 (line 131 "Tool access posture"):
   replace the sentences at `:154-158` and `:165` (the `configured_toolsets =
   all_registered_toolsets()` clause) with the declaration rule and the
   `lane_default` spelling; §5 invariant 3 (`:264-266`) says the scoping is now
   defensive rather than load-bearing. Add a §4c "The declared toolset" of no
   more than twelve lines with the 43/1149 numbers dated. `01-system-architecture.md`
   "personas and profiles as data" gets one sentence: the profile's `toolsets:`
   is the harness lane's capability declaration; the persona field is legacy
   display until R-S0a-3's follow-up.

**After A1, `tool-diff` reports** (all four personas, no HERMES_HOME edit):
`configured_toolsets == effective_toolsets ==` the 14 member names,
`final_tool_count 43`, `withheld_tools []`, `model_tool_tokens 1149`,
`toolset_declaration.source "lane_default"`, `toolset_declaration.declared
["harness_core"]`; `requirement_failures` unchanged until A3. The CLI lane
(`hermes -p neko chat`) is untouched: it still reads `toolsets: [hermes-cli]`.

**Tests A1 must update** (they assert the old posture and are green today):
`tests/agent_runtime/test_unbounded_default_posture.py::test_unbounded_default_reaches_the_chat_lane_block_and_toolsets`,
`tests/agent_runtime/test_chat_lane_toolsets.py::test_unbounded_permission_mode_is_not_scoped`,
`tests/agent_runtime/test_tool_visibility.py::test_unbounded_permission_mode_expands_neko_visibility`,
`tests/agent_runtime/test_persona_runtime_fake.py` (its `_unbounded_toolsets`
helper at `:111-113` returns `all_registered_toolsets()`; change it to the
declared set), and the docstrings in
`tests/agent_runtime/test_persona_prewarm.py::test_the_warm_fills_the_exact_toolset_key_the_create_reads`.
Run `tests/agent_runtime/test_mcp_admission.py`,
`test_mcp_admission_r2.py`, `test_chat_lane_bundle.py`,
`test_mission_chat_turn_context.py`, `test_agent_create_subphases.py`,
`test_tool_visibility_import_deferral.py` and `tests/test_toolsets.py`
unchanged — they must stay green (the import-deferral file in particular:
`declared_lane_toolsets` must not import `model_tools` at module scope).

### A2 · One authority: the persona-level lists go inert and visible

**Files:** `agent_runtime/tool_visibility.py`, `agent_runtime/snapshot.py`,
`agent_runtime/persona_assignments.py`,
`hermes_cli/harness_parts/persona_commands.py`. **Operator data (not the
repo):** `X:\Eternia\.hermes\profiles\{neko,base,alice,unbounded,aliceimagecron}\config.yaml`.

1. **Projections stop presenting the field as authority.** `snapshot.py:2564`:
   `"toolsets": effective_toolsets(agent)` already follows A1 (it now shows the
   declared member names — the launcher's "Toolsets" tag block becomes truthful
   with no launcher change). `persona_assignments.py:3635`: the instance
   summary's `"toolsets"` from `visibility_persona` → `effective_toolsets(...)`
   for the same reason, and add `"toolset_declaration"` beside it. In
   `resolve_tool_visibility`, `persona_toolsets` stays (legacy) and
   `toolset_declaration.row()` gains `persona_list: [...]` (the stale store
   list, verbatim) so the divergence is VISIBLE in one object without being a
   failure.
2. **`tool-diff` text mode** (`_cmd_persona_tool_diff`,
   `hermes_cli/harness_parts/persona_commands.py:216`): after the `N tools`
   line print `toolsets: <declared> (<source>, <config_path>)` and, when
   `persona_list` is non-empty, `persona-level toolsets list ignored (legacy;
   delete it from agent_runtime.personas.<id>.toolsets)`.
3. **`persona_records_from_config`** (`agent_runtime/config.py:532`): keep
   reading `overrides["toolsets"]` (deleting the reader would make a config
   that still carries the key silently different from one that never did);
   log ONE `logger.info` per load when a persona override carries a
   non-empty `toolsets` list naming the persona id and this plan's title. No
   store write, no config write.
4. **`kanban`, `messaging`, `moa`, `browser-cdp`, `launcher_qa`** in the
   persona lists: nothing to do in code — they were never resolved
   (`messaging`/`moa` are not toolsets; `kanban` is hygiene-blocked;
   `launcher_qa` is an MCP alias that admission handles). They disappear when
   the operator deletes the lists.
5. **Operator step (optional, recommended, NOT a landing blocker):** delete
   the `toolsets:` key under `agent_runtime.personas.<id>` in the five profile
   configs named above, and write `toolsets: [harness_core]` at the top level
   of the four persona-bound profiles + `base` (R-S0a-1). Realm sync carries
   the deletion to the Mac on the next publish (`PERSONA_DEF_ALLOWED_KEYS`
   includes `toolsets`; `merge_persona_def` writes the remote body over the
   local shared surface). The store rows keep their lists; they are read by no
   admission path and are labelled `persona_list` in every projection.
6. **Skills** are answered in A6c (they wait, with the reason).

**After A2, `tool-diff` reports** the same numbers as A1 plus
`toolset_declaration.persona_list` = the store row's list (10/6/6/7 names)
until the follow-up row that deletes the field; after the operator's optional
step the `declared` shows `["harness_core"]` with `source: "profile_config"`
and the config no longer carries a persona-level list.

### A3 · The ratchet: withheld 0, requirement failures 0, count pinned

**Files:** `agent_runtime/mcp_admission.py`, `agent_runtime/tool_visibility.py`,
`agent_runtime/mcp_lane.py` (fix_hint text), new test file (§3), canon 08
ledger row.

1. **Admitted is not a failure (R-S0a-4).** In
   `admission_requirement_failures` (`agent_runtime/mcp_admission.py:1118`)
   precedence 1 becomes `if name in admission.server_names: continue` — drop
   the `& registered` intersection and its comment; replace with the teardown
   reasoning from §0.2 item 7. `resolve_tool_visibility` gains the additive
   key `admitted_mcp_servers: sorted(admission.server_names)` when admission is
   enabled (else `[]`); `_requirement_failures` (`tool_visibility.py:324`)
   already resolves the admission — thread it out rather than resolving twice.
   The `mcp_not_registered_on_lane` fix_hint in
   `mcp_lane_requirement_failures` (`agent_runtime/mcp_lane.py:153`) gets one
   added sentence: "With `agent_runtime.mcp_admission.enabled: true` a server
   the persona declares is admitted per run and does not produce this row;
   check `--explain-mcp` and the turn record's `mcp_admitted_servers`."
2. **The ratchet test** (§3, `tests/agent_runtime/test_harness_core_ratchet.py`)
   pins, per sample persona under the hermetic root, `final_tool_count == 43`,
   `availability_counts.withheld == 0`, `requirement_failures == []`,
   `toolset_declaration.source == "lane_default"`, and with a profile home
   that writes `toolsets: [harness_core]` → `source == "profile_config"` and
   the same numbers; a third case writes `toolsets: [hermes-cli, spotify]` and
   asserts `withheld == 12 + 5` and `source == "profile_config"` — the shape
   an operator's stale explicit list produces, so the red is a diff, not a
   surprise. A fourth case enables admission in a hermetic ROOT config with a
   declared `launcher_qa` server and asserts `requirement_failures == []`,
   `admitted_mcp_servers == ["launcher_qa"]`; a fifth denies it (a typed
   denial) and asserts the denial row survives.
3. **Ledger.** `08-performance-and-debt-ledger.md` "Landed optimizations" gets
   one row: `harness_core replaces the registry-wide unbounded set` — 79 → 43
   tools, 17 → 0 withheld, 1–3 → 0 requirement failures, `model_tool_tokens`
   2142 → 1149 (heuristic), per persona, dated, with the landing sha.

### A4 · The manual tells the truth — generated inventory, tools-first Operate table, Atlas JSON

**Files:** new `scripts/emit_harness_tool_inventory.py`; SKILL.md;
new generated `references/tool-inventory.md` and `references/tool-inventory.json`
under `docs/agent-runtime-harness/harness-skills/harness-runtime-model/`;
`references/operations.md` (two rows); new test file (§3).

1. **The emitter** (`scripts/emit_harness_tool_inventory.py`, same contract as
   `scripts/dump_cli_contract.py`: stdout is the artifact, stderr diagnostics,
   `--check` exits 1 on drift with a unified diff, `--write` regenerates,
   newline-explicit LF writes). It imports `model_tools` (populates the
   registry), takes `declared = expand_toolset_names(HARNESS_LANE_DEFAULT_TOOLSETS)`,
   and for each member toolset `registry.get_tool_names_for_toolset(name)`
   (`tools/registry.py:476`) with each entry's `description` (the registry
   entry field, `tools/registry.py:166`), `check_fn is not None` → `gated`, and
   `mutating` from `_mutating_tools` in `tool_visibility`. It refuses to run if
   `REGISTRY_HYGIENE_BLOCKED_TOOLS ∩ tools` is non-empty (the inventory must
   never list a withheld tool). Deterministic: sorted, no timestamps; the
   registry's gating is process-independent for these 43 (desktop tools are
   gated by `HERMES_DESKTOP` env — the emitter records `gated: true`, never
   the verdict).

   It emits three things:
   - **SKILL.md block** between `<!-- BEGIN GENERATED: harness_core inventory -->`
     and `<!-- END GENERATED: harness_core inventory -->` inside a new
     `## In-turn tools` section placed BEFORE `## Operate`: a 15-row table
     `| toolset | tools | use it for |` where the third column is a fixed
     one-line phrase per toolset kept in the emitter (`agent_chat` → "teammates:
     list, message, read, dispatches, transcript path"; `board` → "record
     follow-up work"; …), plus the sentence "43 tools · generated from the
     registry by `scripts/emit_harness_tool_inventory.py` · do not edit by
     hand".
   - **`references/tool-inventory.md`**: the 43-row table `| tool | toolset |
     mutating | gated | description |`, header naming the emitter.
   - **`references/tool-inventory.json`**: `{"schema_version": 1, "declared":
     [...], "toolsets": [{"name", "tools": [...]}], "tools": [{"name",
     "toolset", "mutating", "gated", "description"}], "counts": {"tools": 43,
     "toolsets": 15, "token_estimate": 1149}, "cli_only_verbs": [...]}` — the
     Atlas artifact's "full inventory" section regenerates from this file
     (the current artifact was hand-built from the 79-tool diff; regeneration
     is the orchestrator's step after landing, not the builder's).
   - `cli_only_verbs` is the hand-kept list in the emitter of Operate rows
     that have NO tool: `persona instance open-chat` (mint), `mission-chat
     steer`, `mission-chat turn-resolve`, `mission-chat queue-skill`,
     `persona instance return-summary`, `persona instance steer`, `flow set`.
     The emitter cross-checks: every backticked `agent_chat_*` / `board_*` /
     core tool name that appears anywhere in SKILL.md must be a registered
     name (a renamed tool reds the manual), and every `cli_only_verbs` entry
     must appear in the Operate table.

2. **Operate table reorder** (tools-first; CLI only where no tool exists):

   | Do | In-turn tool (first choice) | CLI (only where no tool exists) |
   |---|---|---|
   | see who your teammates are / which instances you can reach | `agent_chat_threads` (read-only, no mint) | — |
   | message a teammate and get the reply in this turn | `agent_chat_send` (`wait=true`; `wait=false` to dispatch and continue) | `mission-chat message …` is the operator's path, not yours |
   | read what a teammate said | `agent_chat_open` (tail) · `agent_chat_log_path` (full transcript path, then `read_file`/`search_files`) | — |
   | see your background dispatches | `agent_chat_dispatches` | — |
   | track follow-up work | `board_card_add` · `board_cards` — planning state only | — |
   | ask the operator a question | `clarify` | — |
   | hand a bounded subtask to a helper with fresh context | `delegate_task` | — |
   | continue an existing chat root / mint a new server-minted chat | — | `persona instance open-chat …` (rows unchanged) |
   | steer / abandon / queue-skill / return-summary / re-route a steering edge / replace a graph document | — | existing rows unchanged |

   Keep every literal the pins in §0.4 require. The View row "an instance's
   resolved tools and blocks" becomes `hermes harness persona tool-diff
   <persona_id> --json`. Two lines under the table: "If a tool exists for it,
   the tool is the answer; a terminal call for a listed row is a navigation
   failure to report" and a pointer to `references/tool-inventory.md`.
   `references/operations.md` "Inspecting the runtime": the tool-diff row gets
   the positional argument too, and a new first row "what tools YOU have this
   turn" → "the `## In-turn tools` table in SKILL.md; `persona tool-diff` is
   the operator's view of the same set".

3. **Size discipline.** SKILL.md grows by the 15-row table (~1.4 k chars);
   nothing else is added to the preloaded head. The 43-row table lives in
   `references/`.

### A6 · Queue fold-ins

- **A6a · toolset names without the registrars.** Ships INSIDE A1: the
  create's wire row (`persona_instance_summary` → `resolve_tool_visibility`)
  now answers `configured_toolsets`/`effective_toolsets`/`toolset_declaration`
  from `expand_toolset_names` (static `TOOLSETS`) and the profile YAML — no
  registry. `final_model_tools` still needs `_tool_names_for_toolsets` and
  therefore the populated registry, so `model_tools` stays on the create path
  for the tool NAMES. The builder measures, in a fresh interpreter under the
  hermetic root: (i) `declared_lane_toolsets(persona)` wall time with
  `model_tools` NOT in `sys.modules` after the call (assert it — this is the
  A6a gate); (ii) `import model_tools` cold (today 1.61 s on this box; record
  the number). The manifest that would take the tool NAMES off the import
  path is filed as its own row with the measured residual, not built here.
- **A6b · prewarm memo re-measure.** After A1, in a fresh interpreter under
  the hermetic root with `sample_personas()`: time `apply_chat_lane_tool_scope`
  + `resolve_tool_visibility` for `neko_supervisor` cold, then call
  `warm_persona_memos(persona)` for `dev` and time the same resolve for `dev`;
  the delta is what a warm is worth for a SECOND persona type. Record cold/warm
  in the field notes and the ledger. **RULED 2026-09-04 (operator): DONE and
  CLOSED — KEEP the worker.** The decision rule this item carried keyed on the
  SECOND warm's delta (measured 1–2 ms) and was the wrong input: w12/m5 priced
  the FIRST warm on a cold process at ~1.4–1.5 s of registry import, which is
  what the worker removes from the first create. The retire branch is
  WITHDRAWN — the `persona_prewarm` worker, the `runtime.persona.prewarm` verb
  and the launcher trigger all stay. Ruled out with it: any per-persona warm
  beyond the first, so there is no follow-on ambition here to build.
- **A6c · persona→skill seed — skills wait, and here is why.** The toolsets
  answer ("the profile declares, no persona field is consulted") does not
  transfer: skills are written by `persona set-skills` (own supersede clock)
  and by the launcher's Skills console, so the STORE is the designed authority
  and a config-side seed that wins over it would reintroduce the two-writer
  problem. What ships in S0a is visibility: `harness agent list --json` rows
  gain `skills_source: "store" | "catalog"` and `catalog_only_skills: [...]`
  (config `skills:` entries the store row lacks) — accounting, no write. The
  "no re-inherit door" row stays an operator ruling, unchanged.

### A5 · Navigation proof — one real turn per role, tools only

Runs against the live launcher serve (the child spawned with
`HERMES_HOME=X:\Eternia\.hermes\profiles\base`; the CLI below runs from the
PRIMARY checkout after the orchestrator fast-forwards it, with
`HERMES_HOME=X:\Eternia\.hermes` — the roster is home-independent, canon 05).
Roles: `neko_supervisor`, `dev`, `qa`. For each:

1. Pick the on-level instance: `python -m hermes_cli.main harness persona
   list --json` → the chat-mode `personainst_<role>_agent_<hash>` row for the
   persona; note `default_chat_session_id` (not used — a fresh session is
   minted below).
2. Send ONE message on a fresh root, no tool hints in the text (the manual's
   routing is what is under test):

   ```powershell
   python -m hermes_cli.main harness mission-chat message `
     --persona <persona_id> --persona-instance-id <instance> `
     --new-session --title "S0a nav proof <role>" `
     --client-message-id s0a-nav-<role>-<yyyymmddHHMM> `
     --message "Who are your teammates right now? Send one of them the message 'ping from <role> nav proof' and tell me exactly what they said back." --json
   ```

   Capture `session_id` and `turn_id` from the payload.
3. Read the trace lane, not the prose: `python -m hermes_cli.main harness
   snapshot --json` → `.persona_chat_trace[]` rows with `session_id ==
   <root>` (row shape from `_trace_entry`,
   `agent_runtime/persona_chat_history.py:1926`: `event`, `tool_name`,
   `status`, `turn_id`, `ts`; events are `run.tool.started` /
   `run.tool.finished`, the set `_TRACE_EVENT_TYPES` at `:235`). Collect the
   `tool_name` values for the turn.
4. **Pass** = the set of tool names ⊆ {`agent_chat_threads`,
   `agent_chat_send`, `agent_chat_open`, `agent_chat_log_path`,
   `agent_chat_dispatches`} ∪ {`read_file`, `search_files`} (the transcript
   path may be read), contains `agent_chat_threads` and `agent_chat_send`, and
   contains none of `terminal`, `process`, `execute_code`; the reply names the
   teammate and quotes its answer. Record the tool-name list, the
   `client_message_id`, `turn_id`, root, and the turn record under
   `X:\Eternia\.hermes\agent-runtime\mission_chat_turns\` (its token/usage
   fields verbatim) in the field notes, beside `model_tool_tokens` before
   (2142) and after (1149) from `tool-diff`.
5. **Fail** = any `terminal`/`process`/`execute_code` row, or a reply that
   reports the roster without messaging anyone. A fail is a MANUAL defect
   (A4), not a model defect: fix the row the agent ignored, regenerate, rerun
   the one role. Do not coach the message.

The helper the message reaches also records a turn; on `dev`/`qa` the
teammate should be `neko_supervisor` (the supervisor answers pings), on
`neko_supervisor` any dev. Delete nothing afterwards — the fresh roots are the
evidence.

---

## 3. Tests to write (names are proposals; the builder owns the final ids)

| file | what it asserts |
|---|---|
| `tests/test_toolsets.py` (extend `TestResolveToolset` / `TestResolveToolsetIncludeRegistry`) | `harness_core` resolves 43 with the registry and 36 static; `expand_toolset_names(["harness_core"])` is the 14 include names in order; leaf and unknown names pass through; a composite with direct `tools` is NOT expanded; `resolve_toolset("harness_core") ∩ REGISTRY_HYGIENE_BLOCKED_TOOLS == ∅` (the static twin of the emitter's refusal). |
| `tests/agent_runtime/test_toolset_declaration.py` (new) | `declared_lane_toolsets` under the hermetic root with `bundled_persona_profiles`: absent key → `lane_default`; `[hermes-cli]` → `lane_default`; `[]` → `lane_default`; `[harness_core]` → `profile_config`; `[harness_core, spotify]` → `profile_config` with `spotify` kept; malformed YAML → `lane_default`; unresolvable profile → `profile_unresolved`; `effective_toolsets(persona)` ignores `persona.toolsets` entirely (a persona whose field says `["kanban"]` still resolves the declaration); the function never imports `model_tools` (assert `"model_tools" not in sys.modules` after a call in a subprocess, the pattern of `tests/agent_runtime/test_tool_visibility_import_deferral.py`). |
| `tests/agent_runtime/test_harness_core_ratchet.py` (new) | Per persona in `sample_personas()` (`neko_supervisor`, `dev`, `backend_dev`, `qa`) under `unbounded`: `final_tool_count == 43`, `withheld == 0`, `requirement_failures == []`, `configured_toolsets == effective_toolsets == 14 names`, `model_tool_tokens == 1149`; the stale-explicit case (`[hermes-cli, spotify]`) → `withheld == 17`; bounded mode on the same declaration → the cost policy still removes `browser`/`vision`/`file`/`terminal`/`code_execution` unless restored (uses `bounded_chat_session`); the chokepoint and the preview agree byte-for-byte on `final_model_tools` (the T9b parity property, re-asserted on the new authority). |
| `tests/agent_runtime/test_mcp_admission.py` (extend) | `admission_requirement_failures`: admitted + unregistered → no row; admitted + denied (typed) → the denial row; undeclared-unadmitted → R0 row; admission disabled → byte-identical to `mcp_lane_requirement_failures`; `resolve_tool_visibility` carries `admitted_mcp_servers`. The existing isolation tests (`unbounded never widens the admitted set`) stay and must stay green. |
| `tests/agent_runtime/test_harness_tool_inventory.py` (new) | `scripts/emit_harness_tool_inventory.py --check` exits 0 against the committed SKILL.md block, `references/tool-inventory.md` and `.json`; a mutated temp copy of each of the three reds the check (the `test_cli_contract_dump.py` shape: a gate that has only been green is indistinguishable from one that cannot fail); every tool name in the JSON is registered; no hygiene name appears; every `cli_only_verbs` entry appears in the Operate table; every `agent_chat_*`/`board_*` name in SKILL.md is registered. |
| `tests/agent_runtime/test_persona_skill_policy.py` (extend) | The Operate table names `agent_chat_threads`, `agent_chat_open`, `agent_chat_dispatches`, `agent_chat_log_path`, `board_card_add`, `board_cards`, `clarify`, `delegate_task`; the View row for tool-diff carries `<persona_id>`; the `## In-turn tools` heading exists before `## Operate`; the existing literal pins keep passing. |
| `tests/hermes_cli/` (extend the persona command tests) | `tool-diff --json` carries `toolset_declaration` and `persona_toolsets_in_force: false`; text mode prints the declaration line; `harness agent list --json` rows carry `skills_source` and `catalog_only_skills`. |

Docs gates the builder must keep green while editing: every `file.py:N` cite
in the CANON edits (01/05/08) names, in the same sentence, an identifier that
lives within three lines of `N` or the enclosing `def`/`class` —
`scripts/doc_cite_adjacency.py --root docs/agent-runtime-harness` is the gate
(baseline `docs/agent-runtime-harness/cite-adjacency-baseline.json`; its test
excludes `planned/` and `archive/`, so this file is not gated but was written
to the same rule); and `tests/test_coverage_claims_resolve.py` reds on any
doubled-colon test reference that does not resolve — it DOES scan `planned/`
— so write a new test's full id into a doc only after the test exists. At the
time of writing that gate is already red on main for three stale references
in `planned/serve-small-batch-field-notes-2026-09-02.md` (lines 190–192,
tests retired by the 2026-09-02 prewarm change); the builder repoints those
three at the tests that replaced them in the same landing.

---

## 4. Acceptance — what the orchestrator runs

All from the worktree root `X:/wt/s0a-atlas` unless stated;
`HERMES_PYTHON=C:/Python312/python.exe` for the runner.

```powershell
# 1. Unit + ratchet + gates (hermetic)
$env:HERMES_PYTHON = "C:/Python312/python.exe"
bash scripts/run_tests.sh tests/test_toolsets.py tests/agent_runtime/test_toolset_declaration.py `
  tests/agent_runtime/test_harness_core_ratchet.py tests/agent_runtime/test_harness_tool_inventory.py `
  tests/agent_runtime/test_mcp_admission.py tests/agent_runtime/test_mcp_admission_r2.py `
  tests/agent_runtime/test_unbounded_default_posture.py tests/agent_runtime/test_chat_lane_toolsets.py `
  tests/agent_runtime/test_tool_visibility.py tests/agent_runtime/test_tool_visibility_import_deferral.py `
  tests/agent_runtime/test_persona_runtime_fake.py tests/agent_runtime/test_persona_prewarm.py `
  tests/agent_runtime/test_chat_lane_bundle.py tests/agent_runtime/test_agent_create_subphases.py `
  tests/agent_runtime/test_persona_skill_policy.py tests/agent_runtime/test_registry_hygiene.py `
  tests/test_coverage_claims_resolve.py tests/scripts/test_doc_cite_adjacency.py -j 6
C:/Python312/python.exe scripts/emit_harness_tool_inventory.py --check
C:/Python312/python.exe scripts/doc_cite_adjacency.py --root docs/agent-runtime-harness
C:/Python312/python.exe scripts/dump_cli_contract.py --check   # argparse untouched → must still be 0

# 2. Live numbers, no HERMES_HOME edit (R-S0a-2)
$env:HERMES_HOME = "X:\Eternia\.hermes"
foreach ($p in "neko_supervisor","dev","backend_dev","qa") {
  C:/Python312/python.exe -m hermes_cli.main harness persona tool-diff $p --explain-mcp --json |
    C:/Python312/python.exe -c "import json,sys; d=json.load(sys.stdin); t=d['tool_visibility']; print(t['persona_id'], t['final_tool_count'], t['availability_counts']['withheld'], len(t['requirement_failures']), t['model_tool_tokens'], t['toolset_declaration']['source'], t['toolset_declaration']['declared'], t.get('admitted_mcp_servers'))"
}
# expected: <persona> 43 0 0 1149 lane_default ['harness_core'] [<admitted names>]  — four lines
```

Then the A5 recipe (§2) after the primary is fast-forwarded and the serve
restarted; three roles, three trace lanes, zero terminal rows. Landing order
per the house loop: review → the commands above → rebase → push (instant, no
hooks) → ff primary → fill the pairing plan's §5 ledger row "S0a atlas cleanup
A1–A5" with the sha → regenerate the Atlas artifact from
`references/tool-inventory.json`.

---

## 5. Risks, reversibility, and who edits what

**Repo vs HERMES_HOME.** The builder edits ONLY repo files: `toolsets.py`,
`agent_runtime/{personas,persona_runtime,tool_visibility,mcp_admission,mcp_lane,
snapshot,persona_assignments,config}.py`, `hermes_cli/harness_parts/persona_commands.py`
(+ the `agent list` row projection), `scripts/emit_harness_tool_inventory.py`,
the two generated `references/` files, SKILL.md, `references/operations.md`,
canon 01/05/08, and tests. Everything under `X:\Eternia\.hermes\profiles\*`
and `X:\Eternia\.hermes\agent-runtime\agents\*.json` is operator data and is
NOT touched by the builder or by any code path this stage adds (no write on
read, no migration). With R-S0a-2 the landing is complete with zero
HERMES_HOME edits; the operator's optional step (A2 item 5) is legibility.

**The Mac.** Its profiles carry `toolsets: [hermes-cli]` (or nothing) exactly
like this box → `lane_default` → identical 43. Its store rows (minted from the
Windows publish 2026-09-01) carry persona lists → inert, labelled. Nothing in
the realm publish changes until the operator deletes the persona-level lists
here, at which point the deletion travels — additive in effect (the Mac loses a
key nobody read).

**Behavioral change for a turn.** A persona loses `cronjob`, `image_generate`,
`text_to_speech`, `computer_use`, `project_*`, `x_search`, `video_*`, `bfl_*`
and the integrations on the harness lane. Anyone who wants one back writes
`toolsets: [harness_core, <name>]` in that persona's profile — explicit,
per-profile, visible in `tool-diff` as `profile_config`. The CLI lane is
unchanged. The bounded lane's cost policy and `chat_lane_restore_toolsets`
keep working because the declaration is expanded to member names before
scoping.

**Reversal.** Revert the A1 commit → `unbounded` resolves the registry again;
no data was written anywhere, so there is nothing to migrate back. The
generated files and the emitter are additive. The MCP precedence change (A3)
reverts independently.

**What could break.** (1) A test that asserts a non-core tool is present under
`unbounded` — the list in A1 names the ones found; the builder runs the named
files and fixes forward, never baselines. (2) The resident-actor key rotates
once per chat root after landing (`tool_contract` component moves) — one
rebuild, receipted as `resident_rebuild_component_tool_contract`, then steady.
(3) A plugin that registers into `terminal`/`file`/... joins `harness_core`
by membership — intended, and the emitter's `--check` reds until the manual is
regenerated, which is the drift gate doing its job. (4) `persona_chat_actor_prewarm`
warms under the new key — no change needed, it calls the same chokepoint.
(5) `hermes -p neko chat` still gets `hermes-cli`; if the operator later
writes `toolsets: [harness_core]` into `neko`, the CLI lane narrows too — that
is the documented consequence of R-S0a-1, said out loud in the field notes.
