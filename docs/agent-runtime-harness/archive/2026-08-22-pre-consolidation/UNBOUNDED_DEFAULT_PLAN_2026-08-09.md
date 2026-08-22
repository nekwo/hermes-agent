# Unbounded-by-Default Tool Access — Implementation Plan (2026-08-09)

> **IMPLEMENTED 2026-08-09.** The plan below is retained verbatim as the ruling
> record and the design rationale. What shipped, section by section:
>
> | Plan section | Landed as |
> |---|---|
> | §3.1 default at the chokepoint | `agent_runtime/permission_modes.py` (new leaf owning the mode vocabulary, so `config` / `runtime_config` / `terminal_envelope` can read it without closing an import cycle through `tool_permissions` → `tool_visibility`); `runtime_config.ToolPermissionConfig` + `config._tool_permission_config` (unknown value ⇒ typed issue + `profile_default`); `tool_permissions.default_permission_mode()` — the ONE config reader — consumed by `permission_options_for_chat` (`permission_source="runtime_default"`) and by `ToolVisibilityOptions`' default factory, so the no-options snapshot previews describe the same posture a turn gets. |
> | §3.1 stored `profile_default` = "no opinion" | `tool_permissions._record_expresses_opinion`. A corrupted/unknown stored mode clamps to `bounded`, never to the sentinel — a damaged record must not widen a session. |
> | §3.2 envelope under the new posture | `TerminalEnvelopeScope.permission_mode`, stamped by `persona_runtime.mission_chat_reply` from the SAME resolve the schema plane uses; `envelope_decision` grants `GRANTABLE_COMMAND_CLASSES` under `unbounded`; `GRANT_SOURCE_CONFIG` / `GRANT_SOURCE_PERMISSION_MODE`; every receipt carries `granted_by` / `grant_source` / `permission_mode`. The hard-floor branch is untouched. |
> | §3.3 MCP | no code change. `agent_runtime.mcp_admission.enabled: true` was ALREADY set in the live ROOT config (operator ruling 2026-07-26), so the rollout flip was a verified no-op rather than an assumed one. `scope_toolsets_to_admission` untouched. |
> | §3.4 what is NOT unbounded | registry hygiene now reports honestly under `unbounded`: `blocked_tools_count` 22 → **17**, not 0. The previous empty answer was a preview lie against what `profile_runner` strips at agent construction on every lane. |
> | §3.5 store → restriction lane | `PERMISSION_MODE_BOUNDED` added (option (a), pinned); `consume_turn` decrements for ANY opinionated mode — the turns-bounded `read_only` never-expired bug; expiry writes `operator:restriction_expired` vs `operator:elevation_expired`; CLI `--mode` gains `bounded` and its help is reworded around restriction. |
> | §5 honest surfaces | `runtime_hud.resolve_capability_block` gains an explicit `posture` block and rendered line for `unbounded`; `explain_terminal_envelope` is mode-aware (`granted_by_config` / `granted_by_permission_mode`); `persona tool-diff --permission-mode` defaults to the RUNTIME default (an explicit `profile_default` still previews the bounded shape); the mission-chat operative rules no longer tell the model a per-role grant is required for every gated class. |
> | §6.4 docs | this header, `mission-chat-terminal-envelope-grants.md` §2.1 + §5, `00-index.md`, and the doc-19 debt entry (`memory` parallel authority + per-turn schema cost, both un-deferred by this change). |
> | §7 tests | `tests/agent_runtime/test_unbounded_default_posture.py`, each test proven red by reverting the exact line it pins. |
>
> Contract note: `runtime_config.tool_permissions` is an ADDITION to a frame
> section consumers parse key-by-key — "merely unread", not "invisible" — so the
> snapshot `contract_version` does not move, per the ledger's own 52-KEPT rule.
>
> ---
>
> **Original status: decision-ready plan, not yet implemented.** Operator (Tony) ruling:
> every agent/persona in this runtime gets full tool access by default — no
> per-tool blocking as the standing posture, no per-session escalation ritual.
> This document maps every gating layer that exists today, states exactly where
> the default changes, how the layers relate afterward, what becomes possible
> that is not possible today, and the red-then-green test plan. A separate
> implementation agent executes this plan; nothing in this doc has been landed.
>
> Authored from a full read of the enforcement code paths (not from names):
> `tool_permissions.py`, `tool_visibility.py`, `personas.py`,
> `chat_lane_toolsets.py`, `persona_runtime.py`, `profile_runner.py`,
> `terminal_envelope.py`, `mcp_admission.py`, `mcp_lane.py`,
> `coordinator_permissions.py`, `runtime_hud.py`, `snapshot.py`,
> `agent/agent_init.py`, `hermes_cli/harness_parts/persona_commands.py`.

## 1. The layer map — what actually gates what today

There is no single "permissions system". There are **seven distinct mechanisms**,
gating **different planes** (model tool schema, terminal command classes, MCP
registration, graph actions). Only some of them are permission tiers; two are
cost/hygiene policies that merely look like permissions from the outside.

| # | Layer | Owner file(s) | What it gates | Driven by | Does `unbounded` bypass it today? |
|---|-------|---------------|---------------|-----------|-----------------------------------|
| 1 | **Chat permission mode** | `agent_runtime/tool_permissions.py` | Which of the other schema-plane layers apply to a chat turn | `ChatToolPermission` records in `<store_root>/tool_permissions.json`, keyed `persona_id::session_id`; dataclass default `profile_default`; set via `harness persona permission set` | It IS the bypass lever (session-scoped, turn/expiry-bounded) |
| 2 | **Persona safety blocklist** | `agent_runtime/personas.py` — `PERSONA_BLOCKED_TOOLS` (5 names: `delegate_task`, `clarify`, `memory`, `send_message`, `cronjob`) | Model tool **schema** on the persona chat lanes (`clarify` is re-unblocked on the operator/mission chat lane by `persona_runtime._blocked_tool_names_for_chat` because that lane has a non-blocking clarify bridge) | Code constant | **YES** — `_blocked_tool_names_for_chat` returns `[]` and `tool_visibility.resolve_tool_visibility` sets `persona_blocked = frozenset()` when unbounded |
| 3 | **Registry hygiene** | `agent_runtime/personas.py` — `REGISTRY_HYGIENE_BLOCKED_TOOLS` (17 names: 12 kanban + 5 feishu) | Model tool schema on **every** lane | Code constant, unioned at the `profile_runner._blocked_tool_names_with_registry_hygiene` agent-construction chokepoint | **NO** — deliberate; it is upstream-junk deregistration, not a permission tier |
| 4 | **Chat-lane cost policy** | `agent_runtime/chat_lane_toolsets.py` | Toolsets (`browser`, `vision`, `code_execution`, `debugging`, `file`, `terminal`) + single tool `skill_manage` dropped from **conversational** lanes only, for per-turn schema cost | Code constants + per-persona restore knob `agent_runtime.personas.<id>.chat_lane_restore_toolsets` in ROOT config | **YES** — unbounded resolves `all_registered_toolsets()` unfiltered |
| 5 | **Terminal safety envelope** | `agent_runtime/terminal_envelope.py` (+ legacy mirror `tools/terminal_tool.py::_HARNESS_BLOCK_PATTERNS`) | Terminal **command classes** at execution time on the governed `mission_chat` lane: `git_push`, `destructive_git`, `recursive_delete`, `network_egress` | ROOT config `agent_runtime.terminal_envelope.grants.<role>.<lane>: [classes]` — deny-by-default, no wildcard; all 4 classes grantable since ruling R-2 (hard floor is empty) | **NO** — `envelope_decision` never reads permission mode. An unbounded agent still gets `git push` refused unless its ROLE holds a grant |
| 6 | **MCP admission** | `agent_runtime/mcp_admission.py` + `mcp_lane.py` | Which profile-declared MCP servers register per run; `read_only` narrows to the reviewer tool subset | ROOT config `agent_runtime.mcp_admission.enabled` (default **false**) + profile `mcp_servers` declarations | **Deliberately NO** on the cross-persona boundary: `scope_toolsets_to_admission` strips every `mcp-*` toolset the run was not admitted, *after* permission-mode resolution — invariant 3 of that module |
| 7 | **Coordinator permissions** | `agent_runtime/coordinator_permissions.py` | Graph actions (`persona.instance.create/close/retire`) by coordinator **agents** (operator actors bypass) | `agent_runtime.coordinator_permissions` config + persona `autonomy` | **NO** — different plane entirely (graph mutation, not tools) |

Two more modules are involved but are **not** authorities:

- `agent_runtime/tool_visibility.py` — pure projection/accounting. It resolves
  the same policy the runtime enforces so previews/HUD/snapshot cannot lie, and
  it mints the reason strings (`persona_safety_policy`, `registry_hygiene`,
  `turn_runtime_block`, `session_tool_policy`). **`persona_safety_policy` is
  not a store or a config — it is the reason label `_blocked_tool_entries`
  attaches to any block whose name is in `PERSONA_BLOCKED_TOOLS`** (layer 2).
  The live QA instance's `blocked_tools_count: 22` = 5 (layer 2) + 17 (layer 3).
- `agent_runtime/runtime_hud.py` — renders the capability account
  (`capability.toolsets_dropped` / `tools_dropped` / `envelope.granted` /
  `refused_grantable` / `refused_hard_floor`) on the volatile envelope tail of
  every chat turn, from the same resolvers (`chat_lane_capability_drops`,
  `explain_terminal_envelope`).

### 1.1 The enforcement path, traced end to end (how `delegate_task` is refused)

1. Mission-chat turn: `persona_runtime.GPTPersonaRuntime` resolves
   `permission_options_for_chat(persona, session_id=perm_session_id)` →
   store lookup → no record (or expired) → mode `profile_default`.
2. `_blocked_tool_names_for_chat` returns
   `PERSONA_BLOCKED_TOOLS ∪ extra_blocked_tools_for_permission_mode(mode) ∪
   chat_lane_blocked_tools(...)` minus `clarify` → contains `delegate_task`.
3. That list rides `AgentRunRequest.blocked_tool_names` into
   `profile_runner`, which unions `REGISTRY_HYGIENE_BLOCKED_TOOLS` and the
   admission MCP backstop (`_blocked_tool_names_for_run`) and passes the result
   to the agent factory.
4. `agent/agent_init.py:1423` — `get_tool_definitions(..., blocked_tool_names=…)`
   **removes the tool from the model schema**. The model never sees
   `delegate_task`; a hallucinated call fails the `valid_tool_names` check.
   **Blocking is schema-plane, not per-call refusal** — there is no runtime
   interceptor that refuses `delegate_task` by name.
5. Terminal commands are the exception: they ARE gated per-call, inside the
   terminal tool, by the envelope (layer 5), via the
   `terminal_envelope_scope` ContextVar bound around the whole run in
   `profile_runner._execute_agent_run`.
6. After the run: `ChatToolPermissionStore().consume_turn(...)` decrements
   `turns_remaining` **only when the stored mode is `unbounded`**
   (`tool_permissions.py:129`) — a detail that matters in §4.3.

### 1.2 The one default that exists today

The default is **hardcoded twice in `tool_permissions.py`** and nowhere else:
`ChatToolPermission.mode = PERMISSION_MODE_PROFILE_DEFAULT` (dataclass default)
and the `permission_options_for_chat` fallback when no record exists. There is
**no config knob, no persona field, no profile field** for it today. Every
consumer — chat toolset resolution, blocked-name resolution, MCP admission
resolve, HUD capability accounting, `persona tool-diff` previews,
`persona_assignments` summaries — flows through `permission_options_for_chat`.
That chokepoint is the lever, and it means **no per-persona migration is
needed**: change what the chokepoint answers and every persona inherits it.

## 2. Duplicate / legacy findings

Honest finding first: **very little of this surface is dead.** The seven layers
gate different planes and each has live callers; the audits' usual "parallel
resolver" smell mostly does not apply here — `tool_visibility` deliberately
re-resolves the chat lane's answer for preview parity, with the chat-lane
chokepoint threading its authoritative block verbatim (T9b). What IS
duplicated or newly-dead:

1. **`READ_ONLY_BLOCKS` (`tool_permissions.py:14`) and `_MUTATING_TOOLS`
   (`tool_visibility.py:37`) are the same 7-name set maintained in two files**
   (`apply_patch`, `edit_file`, `file.edit`, `file.write`, `patch`, `terminal`,
   `write_file`). They serve different roles (block set vs mutation-boundary
   labeling) but will drift. **DELETE the duplication**: keep ONE constant
   (suggest `tool_permissions.READ_ONLY_BLOCKS`, imported by
   `tool_visibility` — the import edge already exists in that direction), and
   derive `_MUTATING_TOOLS` from it.
2. **The escalation half of the session store becomes dead under the new
   default.** `consume_turn`'s unbounded-only decrement, the
   `operator:elevation_expired` writeback, and the "grant me unbounded"
   direction of `harness persona permission set` no longer do anything useful
   once unbounded is the standing answer. **Do not delete the store** — §4.3
   repurposes it as the *restriction* lane — but the unbounded-specific
   condition in `consume_turn` must be generalized or it is silently wrong in
   the new world (see §4.3).
3. **`tools/terminal_tool.py::_HARNESS_BLOCK_PATTERNS` legacy path on
   ungoverned lanes.** For governed (mission-chat) runs the envelope module
   answers first and the pattern table is never consulted. The table remains
   live for ungoverned lanes (`hermes chat`, cron, gateway, acp) and for the
   drift-guard test. **Not deletable** in this change; note only that the
   already-documented one-line delegation of `_log_harness_blocked_attempt` to
   `terminal_envelope.record_legacy_block` (see that docstring) is still owed.
   **Since shipped:** `tools/terminal_tool.py::_log_harness_blocked_attempt`
   now imports and calls `record_legacy_block`.
4. **Nothing else qualifies.** `PERSONA_BLOCKED_TOOLS` (the 5-name set) stays:
   it is the definition of what `profile_default` blocks, and `profile_default`
   survives as the downgrade tier. The `read_only` MCP tables
   (`READ_ONLY_INCLUDED_TOOLS`/`READ_ONLY_EXCLUDED_TOOLS`) stay for the same
   reason. `coordinator_permissions` is a different plane and untouched.

## 3. Target design

**Decision (recommended, and this plan commits to it rather than leaving it to
the implementer): the layers stay DISTINCT, and `unbounded` becomes the
runtime-wide default that every *permission-tier* layer honors consistently.
The two non-permission layers — registry hygiene (3) and MCP cross-persona
scoping (6's invariant) — deliberately do NOT yield to unbounded, exactly as
today.**

Why not collapse into one authority: the planes are genuinely different
(schema composition vs per-command execution refusal vs process-global MCP
registration vs graph actions). Collapsing them would mean one module owning a
schema filter, a regex command classifier, an MCP registrar and a spawn
counter — a god-module, and a rewrite of four audited, receipt-producing
mechanisms to change one default. The defect Tony is fixing is not "too many
modules", it is "the default posture is deny and only a per-session ritual
lifts it". Fix the default; keep the seams.

### 3.1 Where the default changes (the core edit)

Add a typed config block and flow it through the ONE chokepoint:

- `agent_runtime/runtime_config.py`: new
  `@dataclass ToolPermissionConfig: default_mode: str = "unbounded"` field on
  `RuntimeConfig` (`tool_permissions: ToolPermissionConfig`). The shipped
  default is `unbounded` **per this operator ruling** — this is Tony's fork and
  his runtime; a deployment that wants the old posture sets
  `agent_runtime.tool_permissions.default_mode: profile_default` in the ROOT
  `config.yaml`. Loader: `agent_runtime/config.py::load_root_runtime_config`,
  same parse-and-clamp style as `McpAdmissionConfig` (unknown value → falls
  back to `profile_default`, with a typed config issue — a config fault must
  never resolve to MORE capability than the operator wrote, so the fault
  fallback is the bounded mode even though the shipped default is unbounded).
- `agent_runtime/tool_permissions.py::permission_options_for_chat`: when the
  store has **no applicable record**, answer the configured default instead of
  the hardcoded `PERMISSION_MODE_PROFILE_DEFAULT`, with
  `permission_source="runtime_default"` (new spelling; today's
  `"persona_role_policy"` names an authority S61/S64 already retired).
- **Store-record semantics change (load-bearing):** a stored record whose
  `mode == profile_default` must resolve as **"no opinion — fall through to
  the configured default"**, NOT as an explicit restriction. Reason: the live
  `tool_permissions.json` contains expiry writebacks
  (`consume_turn` sets `mode=profile_default`, `source=operator:elevation_expired`)
  — under the new default those stale rows would silently pin those sessions to
  the bounded tier forever. Explicit restriction is expressed with the
  restriction modes (§4.3), never with `profile_default`. Read `ROOT`
  config once per resolve (it is already `parse_cache`d).

Because `permission_options_for_chat` feeds `_enabled_toolsets_for_chat`,
`_blocked_tool_names_for_chat`, `resolve_mcp_admission`'s mode argument,
`chat_lane_capability_drops`, `apply_chat_lane_tool_scope` previews,
`persona_assignments` summaries and `permission_state_for_chat`, this single
edit flips the schema plane everywhere at once, for every existing persona,
with **zero persona/profile file migration**.

`tool_visibility.ToolVisibilityOptions.permission_mode`'s dataclass default
(`"profile_default"`) also changes to resolve through the same config, or —
simpler and preferred — callers that construct options without going through
`permission_options_for_chat` (snapshot `_agent_summary` calls
`resolve_tool_visibility(agent)` with no options) are updated to thread the
configured default. Pick the implementation that keeps ONE reader of the
config knob (suggest a `default_permission_mode()` accessor in
`tool_permissions.py` that both use).

### 3.2 Terminal envelope under the new posture

Unbounded today does not touch the envelope, so "full tool access" is not
delivered by §3.1 alone — an agent would have every tool *schema* and still be
refused `git push`. Change: **thread the effective permission mode into the
envelope scope and let unbounded grant the grantable classes, with receipts.**

- `TerminalEnvelopeScope` gains `permission_mode: str = ""` (it already
  carries persona/session/lane/role). `persona_runtime` stamps it from the
  same `permission_options_for_chat` resolve the turn already performs
  (`terminal_envelope_scope_for_persona` / `scope_for_persona` gain the
  parameter).
- `envelope_decision`: when the scope's mode is unbounded and the class is in
  `GRANTABLE_COMMAND_CLASSES`, return `OUTCOME_GRANTED` with a distinct
  provenance (`config_key=None`, new field or summary text naming
  `permission_mode=unbounded (runtime default)`), so
  `record_envelope_decision` **still writes the receipt** to
  `terminal_envelope_decisions.jsonl`. The audit trail is the one part of the
  deny posture this plan refuses to give up: every formerly-refused command
  class that now runs is recorded with WHY it ran. `ENVELOPE_COMMAND_NOT_GRANTABLE`
  behavior is unchanged (the hard-floor set is empty post-R-2, but the code
  path stays for any future floor).
- The per-role `grants.<role>.<lane>` table stays as-is: it remains the lever
  for a *bounded* (downgraded) session and the documented narrow-grant
  mechanism. No role stanzas need to be written for the default posture —
  the mode grant supersedes the table when unbounded.
- Ungoverned lanes (`hermes chat`, cron, gateway, acp) are **out of scope and
  unchanged**: no scope bound ⇒ `envelope_decision` returns `None` ⇒ legacy
  behavior byte-for-byte. This ruling covers harness personas, not every
  Hermes surface on the machine.

### 3.3 MCP under the new posture

- **Keep invariant 3 unchanged**: `scope_toolsets_to_admission` continues to
  strip non-admitted `mcp-*` toolsets even for unbounded. This is
  cross-persona isolation in a warm multi-persona process, not a restriction
  on Tony's intent — without it, "full tools" for persona A silently includes
  persona B's admitted launcher-control tools mid-process.
- **Flip `agent_runtime.mcp_admission.enabled: true` in the ROOT config as
  part of the rollout** (config change, not code): full-tooling-by-default is
  hollow if declared MCP servers still resolve to typed denials. Admission
  stays declaration-driven — a persona still only gets servers its profile
  declares; that is capability wiring, not permission.
- `read_only` admission narrowing stays, for downgraded sessions.

### 3.4 What is deliberately NOT unbounded

State these in the implementation and tests so nobody "fixes" them later:

- **Registry hygiene (17 kanban/feishu names)** — junk deregistration, unioned
  at agent construction on every lane, unaffected by any mode. `blocked_tools`
  surfaces will honestly show these 17 entries under the new default.
- **MCP cross-persona scoping** (§3.3).
- **Coordinator permissions** — graph-plane; operator actors already bypass;
  agent coordinators keep their spawn/kill scopes. Widening agent-initiated
  instance kill/spawn is a separate ruling if Tony wants it; it is not "tool
  access".
- **Delegation role policy** (upstream `tools/delegate_tool.py` leaf/orchestrator
  shapes, `delegation.*` config caps) — subagent internals, untouched.

### 3.5 The session store: from escalation lane to restriction lane

`ChatToolPermissionStore`, `harness persona permission set`,
`turns_remaining`/`expires_at` all survive **inverted**: the operator's
temporary lever is now *downgrading* a session (`read_only` or
`profile_default`-tier blocking) rather than escalating it. Required changes:

- `consume_turn` (`tool_permissions.py:129`): generalize
  `if record.mode != PERMISSION_MODE_UNBOUNDED …` to decrement
  `turns_remaining` for **any non-default stored mode**. Today a turns-bounded
  `read_only` grant never decrements — an existing latent bug that becomes the
  main path once restriction is the store's purpose. Expiry writeback should
  delete the record or write the sentinel meaning "no opinion" (§3.1), with
  `source="operator:restriction_expired"` when the expiring mode was a
  restriction.
- Because `profile_default` records become "no opinion", the store needs an
  explicit spelling for "restrict this session to the old bounded tier". Two
  options; the implementer picks ONE and pins it: (a) introduce
  `PERMISSION_MODE_BOUNDED` as an explicit alias mode that resolves exactly as
  `profile_default` used to (recommended — no ambiguity with legacy rows), or
  (b) keep `profile_default` as explicit-when-source-is-operator and
  no-opinion-when-source-is-expiry (rejected: fragile, source-string-keyed
  policy).
- CLI help text for `persona permission set` re-worded around restriction.

## 4. Safety tradeoffs — what becomes possible that is not possible today

This section is the record Tony decides on. None of it is a veto; all of it is
real. Today's posture on the mission-chat lane (the primary work lane, where
personas bind file-write + terminal via worker lanes or restored toolsets):

**4.1 The terminal envelope refusals go away by default.** Every persona on
mission-chat — including QA and any future persona — can by default:

- `git push` (the class does not distinguish `--force`; a force-push to any
  remote the profile's credentials reach is the same grant),
- `git reset --hard` / `git clean -xdf` / forced checkout — the exact
  worktree-bulldozer class that destroyed in-flight work in this stack once
  already (2026-08-01 precedent),
- `rm -rf` / `Remove-Item -Recurse` any path the process can write — including
  `HERMES_HOME`, the runtime store, SessionDBs, and sibling repos,
- `curl`/`wget`/`iwr` to arbitrary hosts. **This is the one to read twice:**
  ruling R-2 already removed the secret-read floor from the envelope taxonomy,
  so `network_egress` was the remaining brake between "agent read a secret"
  and "agent transmitted it". With both gone, a prompt-injected turn (web
  content, MCP output, a poisoned skill file — anything any persona reads) is
  one tool call from exfiltrating `.env` contents or pushing poisoned commits.
  The mitigation this plan keeps is **detective, not preventive**: every such
  command still writes a receipt with mode provenance
  (`terminal_envelope_decisions.jsonl`), and the HUD states the posture openly.

**4.2 The five persona-safety tools unblock on chat lanes.** `delegate_task`
(subagent spawning — cost and recursion bounded only by `delegation.*` caps),
`cronjob` (durable, session-outliving scheduled execution — an injected
instruction can now persist), `send_message` (outbound messages on configured
platforms as the operator), `memory` (upstream memory writes — the operator
ruling that kept it blocked names an unresolved parallel-authority conflict
with profile memory; enabling by default un-defers that reconciliation debt,
it does not resolve it).

**4.3 Cost, not just safety.** `chat_lane_toolsets` exists because tool schemas
ride every API call. Unbounded resolves the FULL registry on every
conversational turn: the browser/vision/code-exec/file/terminal schemas the
cost policy cut (10+ tools' worth on the live lane) come back on every persona
chat turn, permanently. Expect a measurable per-turn token increase on every
chat persona and a one-time prompt-cache invalidation per session as modes
flip. If bills spike, the correct follow-up is a narrow knob (e.g. an
`unbounded_keep_chat_lane_cost_cuts` option), NOT quietly re-blocking tools —
but do not build it speculatively.

**4.4 Blast-radius multiplication.** Today the operator grants unbounded to one
session, time-bounded, on purpose. Afterward, every persona, every session, all
the time, is at that level — the safety envelope becomes opt-in-per-session
instead of opt-out-per-session. The honest summary for the operator: **this
trades a preventive control for an auditable one across the whole roster.**

## 5. Surfaces that must render the new state honestly

- **HUD capability block** (`runtime_hud.resolve_capability_block` /
  `render_capability_block`): today an unbounded turn returns `{}` → `""` —
  documented as "honest silence". Under a *default* of unbounded, silence is no
  longer honest: an agent (and the operator reading the CONTEXT peek) should
  see the posture, not an absence. Add one explicit line when the effective
  mode is unbounded, e.g.
  `- No lane restrictions: permission mode 'unbounded' (runtime default). Terminal envelope classes granted by mode; every such command is receipted.`
  Keep it on the volatile tail (never hashed), same as today's capability
  lines.
- **`permission_state_for_persona` / snapshot `_agent_summary`**: the
  `permission_mode` scalar will read `unbounded` and `blocked_tools_count`
  drops 22 → 17 (hygiene only). `permission_source` gains the
  `runtime_default` spelling — the Launcher Mission Control HUD strip and
  agents drawer render these fields; a launcher-side copy check (does any
  widget special-case `profile_default` or treat `unbounded` as an alarm
  state?) is a **cross-repo follow-up to file with the Launcher, not part of
  this change**.
- **`persona tool-diff`** previews: no code change needed beyond §3.1 (they
  resolve through the same chokepoint), but verify the `--permission-mode`
  hypothetical-preview flag still allows previewing `profile_default` bounded
  shapes after the default flips.
- **Refusal fix-hints** that today tell agents "no permission mode you can
  reach restores it" (`chat_lane_toolsets.ChatLaneDrop.row`,
  `render_capability_block`) stay literally true for *bounded* sessions but
  should be re-read during implementation for wording that presumes bounded is
  the default.

## 6. Migration / compatibility

1. **Personas/profiles: nothing to edit.** No persona YAML/field carries a
   permission mode; the default flows from the chokepoint (§3.1).
2. **Live `tool_permissions.json`:** no migration script needed given the
   "profile_default = no opinion" rule (§3.1). Stale unbounded grants become
   no-ops; stale expiry writebacks become no-ops; any operator-set `read_only`
   rows keep restricting. Optional hygiene: a one-shot prune of records whose
   resolution equals the default.
3. **Root config:** ship `agent_runtime.tool_permissions.default_mode` in the
   config parse; flip `mcp_admission.enabled: true` in the live root config
   (operator action, documented in the rollout note).
4. **Docs:** update `mission-chat-terminal-envelope-grants.md` and the
   operative-rules text the envelope fix-hints reference; add this doc to
   `00-index.md` if that index enumerates non-numbered docs.

## 7. Test plan (red-then-green, per the standing convention)

Write after implementation; prove each red by reverting the exact line it pins.

1. **Default resolution:** with an empty store and no config override,
   `permission_options_for_chat(...).permission_mode == "unbounded"` and
   `permission_source == "runtime_default"`. Red by reverting the chokepoint
   fallback.
2. **Config override narrows:** root config `default_mode: profile_default`
   restores today's bounded resolution (blocked list contains
   `delegate_task`, chat-lane toolsets exclude `browser`). Red by reverting
   the config read.
3. **Unknown config value fails BOUNDED:** `default_mode: banana` resolves
   `profile_default` + typed config issue, never unbounded. Red by reverting
   the clamp.
4. **Stale-record semantics:** a stored `profile_default` record (expiry
   writeback shape, `source=operator:elevation_expired`) resolves to the
   configured default, not to bounded. Red by reverting the no-opinion rule.
5. **Restriction still bites:** an operator-set `read_only` record blocks
   `READ_ONLY_BLOCKS` and narrows MCP admission to the reviewer subset under
   the new default. Red by reverting the record-wins branch.
6. **Restriction expiry:** a `read_only` record with `turns_remaining=1`
   decrements on `consume_turn` and expires back to the default. Red by
   reverting the generalized decrement (this pins the §4.3 latent bug fix).
7. **Envelope mode grant + receipt:** `envelope_decision` on a governed scope
   with `permission_mode="unbounded"` returns `OUTCOME_GRANTED` for `git_push`
   AND `record_envelope_decision` writes a row whose provenance names the
   mode. Red by reverting the scope-mode branch. Companion negative: a bounded
   scope still gets `ENVELOPE_COMMAND_REQUIRES_GRANT` — red by reverting the
   grants-table path.
8. **Invariants that must NOT move:** (a) unbounded run's final toolsets never
   include a non-admitted `mcp-*` toolset (exists — extend to the new default
   path); (b) `REGISTRY_HYGIENE_BLOCKED_TOOLS` absent from `final_model_tools`
   under the default posture. Red by reverting the respective union/strip.
9. **HUD honesty:** `render_capability_block` emits the explicit
   no-restrictions line for the default posture and stays byte-stable for a
   bounded session. Red by reverting the posture line.
10. **Preview parity (T9b) still holds:** `persona tool-diff` `final_model_tools`
    byte-matches the chat lane's constructed schema under the new default.
    Existing parity test re-run; extend its fixture to the unbounded default.

## 8. Out-of-scope (recorded so they are not silent)

- Widening ungoverned lanes (`hermes chat`, gateway, cron, acp) — unchanged.
- Coordinator/graph-action permissions — separate ruling if wanted.
- The `memory` parallel-authority reconciliation (§4.2) — un-deferred by this
  change; file it in the deferred-debt ledger (doc 19) at implementation time.
- Launcher-side rendering copy for the `unbounded` scalar — cross-repo
  follow-up.
- Any preventive replacement for the egress brake (allowlists, secret-scoped
  env) — Tony's call after reading §4.1; nothing here builds one.
