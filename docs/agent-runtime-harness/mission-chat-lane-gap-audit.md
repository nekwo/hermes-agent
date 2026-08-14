# Mission-chat lane — capability gap audit (2026-07-26)

Status: **decision-ready audit, read-only.** No code changed by this document.
Owner of every seam named here: fork (`agent_runtime/`, `hermes_cli/harness*`,
`tools/terminal_tool.py`).

**Retirement note (S61/S62).** This document is a dated audit, not a current
lane registry. The former harness free-chat entry point was deleted in S61;
mission chat is the sole `GPTPersonaRuntime` chat turn. References below to the
deleted free-chat method describe the 2026-07-26 comparison baseline only.

**Landed since publication.** The audit text below is preserved as historical
analysis; this table records what has since shipped, so nobody re-derives it:

| Gap | State | Seam |
|---|---|---|
| G5 | **LANDED** — every chat-lane cost-policy drop is now a typed `requirement_failures` row (`toolset_dropped_by_chat_lane_policy`, `tool_dropped_by_chat_lane_policy`) on the SAME list as the MCP rows, printed by `harness persona tool-diff`. Restored toolsets emit nothing; `unbounded` emits nothing (it genuinely bypasses the policy). | `agent_runtime/chat_lane_toolsets.py`, `persona_runtime.chat_lane_capability_drops`, `tool_visibility._requirement_failures` |
| G5 — agent-visible half | **LANDED** — the typed drops AND the terminal-envelope refusal classes now ride the runtime situational HUD's **capability block**, so the agent sees its own drops MID-TURN instead of reading them as an unexplained absence and improvising. Two bullets on the runtime-context envelope's volatile tail, beside the wall-budget and MCP lines. Volatile by contract (a `volatile=True` row in `runtime_hud.HUD_FIELDS`) exactly like `turn_budget` (`8e7a37d6d`): a cached `unchanged` body — and an `unavailable` one, which drops the body entirely — must never show a stale capability claim, and a restated drop list must never re-snapshot the stable HUD. Empty account ⇒ no line. MCP denials deliberately stay a separate voice (different lifecycle point + kill-switch gate). | `agent_runtime/runtime_hud.resolve_capability_block` / `render_capability_block` / `capability_block_for_persona`, wired once at `_cmd_mission_chat_message` |
| G6 | **LANDED** — per-persona workdir ladder (`agent_runtime.personas.<id>.workdir` → the `--agents-file` workspace pointer → the persona's `repo_scope` → process cwd), threaded into the EXISTING `AgentRunRequest.workdir` seam. A configured path that does not exist degrades to the safe cwd and emits `mission_chat_workdir_unresolved`; it never fails the turn. | `agent_runtime/mission_chat_workdir.py`, `persona_runtime.mission_chat_reply` |
| G10 | **LANDED** — the 240 s default moved to `agent_runtime.mission_chat.default_max_seconds` (absent ⇒ 240 s, unchanged; clamped to [30 s, 86400 s]). An explicit `--max-seconds` always wins. | `agent_runtime/runtime_config.MissionChatConfig`, `config.resolve_mission_chat_max_seconds` |

G1/G2 (the drops themselves — now accounted for, still applied), G3, G4/G5b,
G7–G9, and G11–G18 remain open exactly as written below.

**Why this exists.** The operator has ruled that the mission-chat lane
(Mission Control persona instances — `hermes harness mission-chat message` →
`GPTPersonaRuntime.mission_chat_reply`) becomes the **primary home for all
agent work**. Historically capabilities landed on other entry lanes first and
the harness/mission-chat lane was excluded — sometimes deliberately (cost,
determinism), sometimes by drift, always silently. This is the full inventory
of what a mission-chat agent *cannot* do that an agent on another lane *can*,
with file:line evidence, a classification, and the seam where each fix lands.

Sibling docs:
[`mission-chat-mcp-admission.md`](mission-chat-mcp-admission.md) (the MCP slice
of this problem, already designed),
[`harness-serve-design.md`](harness-serve-design.md),
[`04-decision-hud-simplification-map.md`](04-decision-hud-simplification-map.md)
("agents work unbounded, the harness reads the work" — the stance this audit
measures reality against).

---

## 0. Executive top 10

Ordered by how hard they block the ruling. "Has it" = the lane(s) where the
capability is live today.

| # | Gap | Has it | mission-chat | Class | Pri |
|---|-----|--------|--------------|-------|-----|
| G1 | **`file` + `terminal` + `code_execution` toolsets are dropped from every mission-chat turn** by the chat-lane cost policy. No read_file / write_file / patch / search_files / terminal / process / execute_code, for **every role**. | `chat`, `acp`, `gateway`, `cron`, harness **worker** lane | ✗ dropped | DELIBERATE (cost policy, pre-ruling) | **P0** |
| G2 | **`browser` + `vision` dropped too** → a QA persona on mission-chat has *no* way to drive a UI or look at a screenshot. | `chat`, `acp`, `gateway`, harness **worker** lane | ✗ dropped | DELIBERATE (collateral of G1) | **P0** |
| G3 | **MCP tools never registered on the harness lane** (`discover_mcp_tools()` gated on `_AGENT_COMMANDS`). Typed as of today; admission still unbuilt. | `chat`, `acp`, `cron run\|tick`, `gateway run`, `oneshot` (backgrounded) | ✗ never registered | **LANDED** — `agent_runtime/mcp_admission.py` shipped through R2 and is wired live at `agent_runtime/persona_runtime.py` (`resolve_mcp_admission(..., lane=LANE_MISSION_CHAT, ...)`) and `agent_runtime/tool_visibility.py`. | **P0** |
| G4 | **Harness terminal safety envelope hard-blocks `git push`, `git restore`, `git checkout -- .`, `rm -rf`, and all non-localhost `curl`/`wget`** — with **no permission-mode escape hatch**. Even `unbounded` cannot lift it. An agent literally cannot land its own work. | `chat`, `acp`, `gateway`, `cron` (envelope inactive) | ✗ blocked | DELIBERATE, but pre-dates the ruling | **P0** — **RESOLVED 2026-07-26 together with G5b**, see [`mission-chat-terminal-envelope-grants.md`](mission-chat-terminal-envelope-grants.md) |
| G5 | **The tool drops are invisible.** `requirement_failures` carries the new MCP row and *nothing else*: G1/G2/G11 emit no typed row, so "I have no terminal" reads to the agent and the operator as an unexplained absence — the exact defect class the MCP row was created to retire. | n/a | ✗ silent | ACCIDENTAL (same class as the MCP invisibility bug) | **P0** |
| G6 | **No workdir / repo grounding.** `mission_chat_reply` passes no `workdir`; `TERMINAL_CWD` is never set. The turn runs in whatever cwd the serve process happens to hold. The worker lane resolves a real repo context; `hermes chat` runs in the operator's cwd. | harness **worker** lane (repo ctx), `chat` (operator cwd) | ✗ none | ACCIDENTAL | **P0** |
| G7 | **Historical role ceiling capped the lane at ≤13 toolsets.** S11 removed the intersection filter; configured persona data now owns the list. | `chat` (~40 tools), `gateway`, `acp` | resolved | REMOVED | P1 |
| G8 | **Core context files skipped by default** (`AGENTS.md` / `CLAUDE.md` / `.hermes.md` / `.cursorrules`). Opt-in exists (`include_core_context_files`) but is root-`config.yaml`-only — no CLI and no Mission Control surface. | `chat`, `acp`, `gateway`, `oneshot` (all load them); `cron` loads SOUL always, project docs only with a job workdir | ✗ skipped | DELIBERATE (T-series cost work) + invisible knob | P1 |
| G9 | **No durable memory, read or write.** `skip_memory` defaults on (MEMORY.md / USER.md not injected) *and* the `memory` tool is globally blocked by `PERSONA_BLOCKED_TOOLS`, so the agent can neither read nor write its own memory. | `chat`, `acp`, `gateway`, `oneshot` (`cron` also skips, deliberately) | ✗ both halves | DELIBERATE (two independent decisions, compounding) | P1 |
| G10 | **Default wall budget is 240 s** → ~180 s of working window after the checkpoint reserve. `hermes chat` is unbounded; the worker lane defaults to 300 s and is config-tunable per persona. A 30-minute task cannot run on mission-chat without the caller overriding `--max-seconds`. | `chat` (unbounded), worker (300 s, tunable) | 240 s CLI default | DELIBERATE | P1 |

---

## 1. Lane map — how each entry point builds its agent

| Lane | Entry | Agent construction | Default toolset surface |
|------|-------|--------------------|-------------------------|
| **chat** (`hermes`, `hermes chat`, `hermes -p X chat`) | `hermes_cli/main.py:2216` → `cli.py::main` (L15669) → `cli_agent_setup_mixin.py:218` | `AIAgent` → `agent/agent_init.py` **directly** | `_get_platform_tools(cfg,"cli")` ⇒ the `hermes-cli` composite = `_HERMES_CORE_TOOLS` (`toolsets.py:31-80, 436-440`), minus `_DEFAULT_OFF_TOOLSETS` (`tools_config.py:118`) |
| **acp** | `acp_adapter/entry.py:260` → `acp_adapter/session.py:656` | `AIAgent` directly | `hermes-acp` composite (`toolsets.py:384-401`) — terminal, file, browser, vision, execute_code, delegate_task |
| **cron run\|tick** | `cron/scheduler.py:2948-2979` | `AIAgent` directly | `hermes-cron` = `_HERMES_CORE_TOOLS`; always disables `cronjob`/`messaging`/`clarify` (`scheduler.py:116-136`) |
| **gateway run** | `gateway/run.py:18026-18057` (+3 more sites) | `AIAgent` directly | per-platform composite, all `_HERMES_CORE_TOOLS`-derived (`toolsets.py:453-478`) |
| **oneshot** (`hermes -z`) | `hermes_cli/oneshot.py:387-411` | `AIAgent` directly | same `"cli"` default as chat |
| **mcp serve** | `mcp_serve.py:543-991` | **none** — this is an MCP *server*, no agent is built | n/a |
| **send** | `hermes_cli/send_cmd.py:298-365` | **none** — "No LLM, no agent loop" | n/a |
| **harness worker** (goal ticks) | `persona_runtime.py::run_tick` → `_invoke_agent` (L118-193) | `profile_runner.py::ProfileAgentRunner.run` | `effective_toolsets(persona)` — role ceiling, **no** chat-lane cost filter |
| **harness free-chat** (historical; deleted S61) | Former callerless free-chat method | same runner | `_enabled_toolsets_for_chat` |
| **mission-chat** (canonical) | `harness.py:1293` → `persona_commands.py::_cmd_mission_chat_message` (L1224) → `mission_chat_reply` (`persona_runtime.py:289`) | same runner | `_enabled_toolsets_for_chat` (`persona_runtime.py:881`) |

Two structural facts that shape everything below:

* **`agent_runtime/profile_runner.py` is the only typed construction path in the
  repo.** Every non-harness lane calls `AIAgent(...)` directly. That is why the
  harness lanes are the only ones that get `blocked_tool_names`, registry
  hygiene, `skill_runtime_scope`, and typed budgets — and equally why they are
  the only ones that can *lose* capability to a policy filter (see §7 R13).
* **`hermes harness serve` is not a lane.** `harness_parts/serve.py:374-403`
  re-parses each NDJSON request's `argv` through the same harness argparse tree
  and calls the same `_cmd_*` handler. It constructs no agent; it inherits
  mission-chat's surface exactly.

Historically, `profile_runner.py::_execute_agent_run` was the shared
construction chokepoint for three harness lanes. After S61, mission chat is the
only surviving `GPTPersonaRuntime` chat lane; the old comparison remains useful
only for understanding why the cost-policy gaps were discovered.

**Not a gap — do not re-derive:** *plugin* discovery is **not** lane-gated. It
runs as a module-level side effect of importing `model_tools`
(`model_tools.py:204-208`), which every lane does. Plugin-registered toolsets
(`web`, `browser`, `image_gen`, `memory`, `context_engine`, …) are present in
the registry on the harness lane; they are lost later, to the role ceiling
(G7) and the cost policy (G1/G2), not to discovery.

---

## 2. The tool-surface gaps

### G1 — `file` / `terminal` / `code_execution` dropped from every mission-chat turn

`agent_runtime/chat_lane_toolsets.py:72-90`:

```python
DEFAULT_CHAT_LANE_EXCLUDED_TOOLSETS: frozenset[str] = frozenset(
    {"browser", "vision", "code_execution", "debugging", "file", "terminal"}
)
```

At publication this was applied to both the former free-chat method and
`mission_chat_reply`. The former method and worker tick were deleted in S61;
the live statement is only that mission chat passes through
`_enabled_toolsets_for_chat`.

**Worked example — the seeded Neko supervisor**
(`agent_runtime/personas.py:239`, toolsets
`file, search, terminal, session_search, code_execution, todo, skills, mission_goal`):

1. `effective_toolsets` preserves the persona's configured toolsets.
2. `_augment_chat_capabilities` (`persona_runtime.py:972`) adds `agent_chat`, `board`, `clarify`.
3. `scope_chat_lane_toolsets` drops `file`, `terminal`, `code_execution`.
4. The retired mission-goal capability is absent for every persona.

**Final mission-chat surface:** `search` (= `web_search` only —
`toolsets.py:103-107`), `session_search`, `todo`, `skills` (minus
`skill_manage`), `agent_chat`, `board`, `clarify`.

Same computation for the other seeded roles:

| Persona | Configured toolsets | Live on mission-chat |
|---|---|---|
| `neko_supervisor` | file, search, terminal, session_search, code_execution, todo, skills, mission_goal | search, session_search, todo, skills\*, agent_chat, board, clarify |
| `dev` / `backend_dev` | file, search, terminal, session_search, code_execution, skills | search, session_search, skills\*, agent_chat, board, clarify |
| `qa` | file, search, terminal, browser, vision, session_search, skills | search, session_search, skills\*, agent_chat, board, clarify |
| `base` (the actually-seeded default, `personas.py:313-331`) | file, search, terminal, code_execution, session_search, skills, agent_chat, board | search, session_search, skills\*, agent_chat, board, clarify |

\* `skills` minus `skill_manage` (G11).

**Why it's a P0 under the new ruling.** The lane's own system prompt promises
the opposite. `agent_runtime/persona_runtime.py:539-542`
(`_mission_chat_operative_rules`) tells the model:

> "You have real tools. When the operator asks you to do something — run a
> command, read or edit a file, check or change state — actually use your tools
> and report the real result. The operator's current permission grant is the
> only gate on what you can do…"

That last clause is **false on this lane**: the cost policy runs *after* the
permission layer and is not a permission grant. The operator sighting of a
persona chat auto-titled *"Terminal Unavailable for Command…"* is exactly this
— the agent was asked to run a command, found no `terminal` tool, and said so.

*Escape hatches that exist today:* per-persona
`agent_runtime.personas.<id>.chat_lane_restore_toolsets` in the **root**
config.yaml (`agent_runtime/config.py:300-330`), or an operator-granted
`unbounded` permission mode, which bypasses the cost policy entirely
(`persona_runtime.py:895-897`). Both are opt-in, neither is discoverable from
Mission Control, and `unbounded` is a blunt instrument (it grants *all*
registered toolsets).

**Classification:** DELIBERATE and well-reasoned *for a supervision chat*
("coordinate and read out"), now directly contradicted by the ruling that this
lane hosts all agent work.

**Seam:** `agent_runtime/chat_lane_toolsets.py`. It is already a pure,
unit-testable policy function with one caller — the right shape. The change is
to make the excluded set a **function of the persona's work posture**
(supervision vs. worker) rather than one global constant, so a Dev/QA instance
on mission-chat resolves the worker surface while a pure coordination chat
keeps the cheap one.

### G2 — `browser` + `vision` dropped ⇒ QA cannot produce visual proof

Same constant, same chokepoint. Combined with G3 (no `launcher_qa` MCP) this
means a QA persona on mission-chat has **zero** paths to a screenshot: no
browser tools, no `vision_analyze`, no MCP. The only working visual-proof lane
is the `qa.request_screenshot` decision contract →
`agent_runtime/stagec_mcp_visual_provider.py`, which is the *worker* lane, not
mission-chat. This is the mechanical cause of the 2026-07-25 "Blocked 21 tools"
escalation.

**[2026-08-14: that last working lane is gone.]** `stagec_mcp_visual_provider.py`
was deleted in `5a1267ef60` and there is no `request_screenshot` entry in
`decision_contract_registry`. G2 is therefore worse than recorded, not better: a
QA persona now has zero paths to a screenshot on **any** lane, not just on
mission-chat. MCP admission (G3) is the only remaining candidate fix.

**Seam:** same as G1, plus MCP admission (G3).

### G3 — MCP never registered (KNOWN; catalogued for completeness)

`hermes_cli/main.py:12350-12355` gates `_prepare_agent_startup` on
`_AGENT_COMMANDS = {None, "chat", "acp", "rl"}` + `_AGENT_SUBCOMMANDS`
(`cron run|tick`, `gateway run`, `mcp serve`). `hermes harness …` is excluded,
so `discover_mcp_tools()` (`main.py:12427-12429`) never runs on this lane.

Typed today as `mcp_not_registered_on_lane`
(`agent_runtime/mcp_lane.py`, emitted through
`agent_runtime/tool_visibility.py:123-124,189`). Admission design:
[`mission-chat-mcp-admission.md`](mission-chat-mcp-admission.md). No further
derivation needed here — **but note it is one instance of G5**: the general
"this lane silently dropped a declared capability" problem now has exactly one
typed member.

### G7 — the role ceiling is an intersection, not a default

`agent_runtime/personas.py:72-93`:

The former per-role mapping was deleted in S11.

`validate_toolsets` (L183-186) **intersects** the persona's configured list with
this set, so a toolset absent from the ceiling can never be enabled on any
harness lane, by any config, in any permission mode short of `unbounded`.
Unreachable classes: `web_extract` (the `web` toolset is supervisor-only, and
even the seeded supervisor doesn't list it), `image_gen`, `tts`,
`computer_use`, `memory`, `context_engine`, `node_control` (root-node only by
design), `project`, `cronjob`, `delegation`.

**2026-07-30 correction:** `node_control` is no longer merely unreachable or
root-only. Its broken `run_node` / `steer_node` tool module was deleted by
`de14b06d2`, and its fork-added toolset registration was removed by
`e69db6e71`. The list above is the audit's historical state.

By contrast `hermes chat` with no `--toolsets` resolves the `hermes-cli`
composite (`cli.py:15805-15810` → `hermes_cli/tools_config.py::_get_platform_tools`
→ `toolsets.py:436-440`, whose `tools` is `_HERMES_CORE_TOOLS`, `toolsets.py:31-80`)
minus `_DEFAULT_OFF_TOOLSETS` (`tools_config.py:118` — homeassistant, spotify,
discord, video, video_gen, x_search). That is ~40 tools including terminal,
process, read/write/patch/search, browser, vision, image_gen, execute_code,
delegate_task, memory, cronjob, computer_use. `acp`, `cron`, `gateway`, and
`oneshot` all resolve the same `_HERMES_CORE_TOOLS`-derived surface.

**Classification:** DELIBERATE (role hygiene for the typed pipeline), but
written for the worker pipeline and never revisited for a lane that is meant to
host *all* work. S11 removed both seed synthesis and the ceiling.

**Resolved seam:** `validate_toolsets` now preserves explicitly configured
toolsets and deduplicates them.

### G11 — `skill_manage` blocked on chat lanes

`agent_runtime/chat_lane_toolsets.py:92`
(`DEFAULT_CHAT_LANE_EXCLUDED_TOOLS = {"skill_manage"}`), applied at
`persona_runtime.py:838`. A mission-chat agent can *read* skills
(`skill_search` / `skill_view` / `skills_list`) but cannot author, edit, or
install one. The worker lane keeps `skill_manage`. Restorable via the same
`chat_lane_restore_toolsets` knob. **DELIBERATE, P2.**

### G12 — `delegate_task` / `memory` / `send_message` / `cronjob` globally blocked

`agent_runtime/personas.py:161-169` (`PERSONA_BLOCKED_TOOLS`) blocks these on
**every** harness lane. `hermes chat` has all four. `delegate_task` and `memory`
are explicitly retained-but-blocked ("parallel-authority surfaces", L134-137) —
a documented, owned decision. Worth re-opening only if the ruling implies
mission-chat agents should spawn subagents. **DELIBERATE, P2.**

### G13 — ordinary persona chat globally strips `mission_goal`

Current policy: every ordinary Mission Control persona-chat turn is chat-only.
`agent_runtime/persona_runtime.py::_enabled_toolsets_for_chat` strips
`mission_goal` for all roles and permission modes unless the caller explicitly adds
`mission-chat message --allow-mission-goal` for that exact turn. This replaces the
older Neko-only strip, which left profile-backed supervisors able to default into a
real task/graph. `intent_hint=assign_work` remains chat coordination and does not opt
in. Existing role eligibility and product disables still apply after explicit opt-in.
**RESOLVED as a global routing guard, 2026-07-28.**

---

## 3. Execution-envelope gaps

### G4 — the harness terminal safety envelope has no escape hatch

> **RESOLVED 2026-07-26**, together with G5b, by
> [`mission-chat-terminal-envelope-grants.md`](mission-chat-terminal-envelope-grants.md).
> The section below records the state as audited. Two corrections the
> implementation established:
>
> * The split G5b describes is not merely "two flavors of no-human-available".
>   The deciding variable is whether the persona binds a `hermes_profile`:
>   `profile_context.py:82-84` early-`yield`s for a profile-less persona, so
>   `HERMES_AGENT_RUNTIME_ROOT` is never exported and the envelope is **inert**
>   — which is how the same lane ran `git push origin main` ungated.
> * The fix is **not** a permission-mode escape hatch. Threading
>   `unbounded` into the envelope would have made a blunt mode lift a specific
>   safety floor. Enforcement is now keyed on a bound
>   `TerminalEnvelopeScope` and lifted only by an explicit, root-config,
>   per-role/per-lane, per-command-class operator grant.

`tools/terminal_tool.py:2046-2058`:

```python
def _harness_safety_block(command: str) -> str | None:
    if not os.getenv("HERMES_AGENT_RUNTIME_ROOT", "").strip():
        return None
    ...
```

The envelope activates on the mere **presence** of `HERMES_AGENT_RUNTIME_ROOT`,
which `profile_runner` sets for every harness run
(`persona_runtime.py:277,428` pass `runtime_root=paths.store_root()`;
`agent_runtime/profile_context.py:109-110` exports it). It is therefore active
on mission-chat and inactive on `hermes chat`.

Blocked patterns (`terminal_tool.py:52-69`):

| Pattern | Reason code |
|---|---|
| `git push` | `git_push_requires_operator_approval` |
| `git reset --hard\|--merge\|--keep`, `git clean -xdf`, `git checkout --force\|-- .`, `git switch -f`, **`git restore`**, `git stash drop\|clear` | `tree_wipe_blocked` |
| `rm -rf`, `Remove-Item -Recurse` | `tree_wipe_blocked` |
| `cat/type/Get-Content` of `.env`/`credentials`/`.netrc`/`.pgpass`/`.npmrc`/`.pypirc` | `credential_read_blocked` |
| `kubectl apply\|delete\|rollout\|scale\|patch`, `terraform apply\|destroy` | `prod_operation_requires_operator_approval` |
| `curl`/`wget`/`iwr`/`Invoke-WebRequest`/`Invoke-RestMethod` to any host outside `{localhost, 127.0.0.1, ::1, host.docker.internal}` | `network_command_requires_allowlist` |

The reason codes say *"requires operator approval"* — **but there is no
approval channel on this lane** (G5b below), and `_harness_safety_block` reads
no permission state at all. `unbounded` does not lift it. Net effect under the
ruling: **an agent whose primary home is mission-chat can never push its own
work, never `git restore` a file it broke, and never fetch a URL.** Given
this repo's own git discipline ("work is not done until it is pushed to
`origin/main`"), that is a hard stop.

**Classification:** DELIBERATE and correct as a *default* for autonomous worker
ticks; wrong as an *absolute* for an operator-supervised chat where a human is
watching every turn.

**Seam:** `tools/terminal_tool.py::_harness_safety_block` — thread the
resolved chat permission mode (or an explicit per-turn grant) in, so
`unbounded`/an operator-approved command lifts the specific pattern, and keep
the audit write at `_log_harness_blocked_attempt` (L2077) as the receipt. The
approval decision itself belongs in `agent_runtime/tool_permissions.py`, next
to the existing modes, not in the tool.

### G5b — no operator approval channel; the dangerous-command guard fails **open**

> **RESOLVED 2026-07-26** for envelope-gated commands on mission-chat —
> [`mission-chat-terminal-envelope-grants.md`](mission-chat-terminal-envelope-grants.md).
> The warning this section ends on ("fixing G4 without fixing this would move
> dangerous commands from hard-blocked to silently auto-approved") was honored:
> both were done in one change, and the ONLY new "allow" is an explicit
> root-config operator grant bounded by a stage floor. Scope note: the
> **general** `approval.py:2594-2601` fail-open default is upstream and
> untouched — what changed is that envelope-gated commands on this lane no
> longer reach it undecided.

**Every other lane has an approval surface. mission-chat has none.**

| Lane | Approval surface for a dangerous command |
|---|---|
| `chat` | interactive TTY prompt (`HERMES_INTERACTIVE=1`, `cli.py:15740`) + smart-approval LLM; `--yolo` bypass |
| `acp` | bridged to the **editor's** permission UI (`acp_adapter/permissions.py:21-60`, `server.py:1421,1489-1490`) |
| `cron` | `HERMES_CRON_SESSION=1` (`cron/scheduler.py:2590`) ⇒ `approvals.cron_mode`, default **deny** (`approval.py:2601-2660`) |
| `gateway` | chat-based "ask surface" approval prompt (`approval.py:2960-2970`) |
| `oneshot` | `HERMES_YOLO_MODE=1` set unconditionally (`oneshot.py:214-215`) ⇒ blanket auto-approve |
| **mission-chat** | **none** |

With `is_cli`, `is_gateway` and `is_ask` all false and no cron marker,
`tools/approval.py:2594-2601` takes the "historical fail-open default" — the
command runs **ungated, and not recorded as an approval event**. The separate
plugin-escalation path (L2081-2088) fails *closed* under the identical
conditions. So the lane has two opposite behaviors for two flavors of "no human
available", and neither is the truth: a human *is* watching a mission chat.

Scope note: this covers **terminal commands only**. `write_file` / `patch` have
no interactive approval gate on *any* lane — only a fixed sensitive-path
refusal list (`tools/file_tools.py:604-660`). The `chat` lane compensates with
inline diffs (`cli_agent_setup_mixin.py:387-388`) and opt-in checkpoints; the
mission-chat lane has neither (G18).

**Classification:** ACCIDENTAL drift. **P1.** Fixing G4 without fixing this
would move dangerous commands from "hard-blocked" to "silently auto-approved".

**Seam:** pass a real `approval_callback` down `AgentRunRequest` (it already
carries `clarify_callback` — the same non-blocking-bridge pattern
`MissionChatClarifyCapture` uses at `persona_runtime.py:346` would work: record
the request, end the turn, surface it as an operator prompt in Mission
Control).

### G6 — no workdir, no `TERMINAL_CWD`

`mission_chat_reply` builds its `AgentRunRequest` without `workdir`
(`persona_runtime.py:363-431` — the field is simply absent), so
`profile_runner._agent_workdir(None)` yields immediately without `os.chdir` and
without exporting `TERMINAL_CWD` (`profile_runner.py:798-802`). Compare:

* worker lane: `_repo_context_for_persona` resolves a real repo, isolates it per
  run, and passes `workdir=repo_ctx.workdir` (`persona_runtime.py:122-131, 195`).
* `hermes chat`: runs in the operator's actual cwd.

**2026-07-30 correction:** the first bullet no longer names live code. S5 removed
the worker lane, and S29 (`4a56bb546`) removed `_repo_context_for_persona` itself
once its `AgentContext` producer was gone — so that bullet and its line numbers
are the audit's historical state. G6 has since been resolved from the other
direction: `agent_runtime/mission_chat_workdir.py` resolves the mission-chat
workdir through its own ladder and hands it to the same `AgentRunRequest.workdir`
seam, which makes this lane the only one in the runtime that still has repo
grounding.

A mission-chat agent therefore has no notion of "which repo am I in", and any
relative path it uses resolves against the serve process's cwd. Compounded by
G8 (no `AGENTS.md`), a mission-chat Dev has neither repo grounding nor repo
doctrine.

**Classification:** ACCIDENTAL — this lane was built for coordination, where a
workdir is meaningless. **P0 under the ruling.**

**Seam:** `hermes_cli/harness.py` mission-chat parser (a `--workdir` /
`--repo` arg, or resolution from the persona's `repo_scope`) →
`AgentRunRequest.workdir`, which already exists and is already honored.

### G14 — shell hooks never registered

`agent/shell_hooks.register_from_config` is called from exactly two places:
`hermes_cli/main.py:12437-12439` (inside the `_AGENT_COMMANDS`-gated
`_prepare_agent_startup`) and `gateway/run.py:6906-6907`. The harness lane
calls neither, so a deployment's configured `hooks:` (pre/post tool-call shell
hooks) fire on `hermes chat` and on the gateway but **never on mission-chat**.

**Classification:** ACCIDENTAL (same gate as MCP, different payload). **P2** —
low blast radius today, but it means any hook-based policy/audit an operator
installs silently does not cover the lane the ruling makes primary.

**Seam:** whatever admission mechanism G3 lands on should carry hooks too —
they are the same "user-configured extension that the harness lane's startup
never reaches" class.

---

## 4. Context, memory, and skills

### G8 — core context files skipped by default

`AgentRunRequest.skip_context_files` defaults `True`
(`profile_runner.py:127`), and all three harness lanes set it from the persona
opt-in (`persona_runtime.py:167` worker, `252` free-chat, `383` mission-chat):

```python
skip_context_files=not bool(getattr(persona, "include_core_context_files", False)),
```

`agent/agent_init.py:320` defaults it **`False`**, and no non-harness lane
overrides it — `acp` (`session.py:656`), `gateway` (`run.py:18026-18057`) and
`oneshot` (`oneshot.py:387-411`, whose docstring says so explicitly at L6-10)
all load `SOUL.md`, `.hermes.md`, `AGENTS.md`, `CLAUDE.md`, `.cursorrules` from
cwd + `HERMES_HOME` (`agent/system_prompt.py:433-443`). `cron` is the one lane
that splits the difference deliberately: `skip_context_files=not bool(job_workdir)`
but `load_soul_identity=True` always (`cron/scheduler.py:2969-2976`) — i.e. it
keeps persona identity while dropping cwd project docs. That is precisely the
shape mission-chat needs and does not have.

The rationale for the harness default is recorded in-line at
`persona_runtime.py:376-382`: the 72 KB `hermes-agent/AGENTS.md` was costing
~20 K tokens per conversational turn.

Two follow-on facts:

* **SOUL is not lost** — mission-chat has its own parallel path,
  `_mission_chat_soul_overlay` (`persona_runtime.py:1560-1573`), which reads the
  bound profile's `SOUL.md` and injects it into the surface message. This is a
  *duplicate authority* for the same content (the standard path is
  `agent.load_soul_identity` / `system_prompt.py:155-162`), worth noting as
  architectural debt even though it currently works.
* **The opt-in is invisible.** `include_core_context_files` /
  `include_profile_memory` are readable only from the root config.yaml
  (`agent_runtime/config.py:387-388, 570`). No CLI flag, no `persona instance
  update-profile` field (`persona_config_sync.py:105-106` syncs them but no
  command sets them), no Mission Control surface.

**Classification:** DELIBERATE (cost) + ACCIDENTAL (unsurfaced knob). **P1.**

**Seam:** `AgentRunRequest.skip_context_files` is already per-request. The
correct fix is *scoped* injection (the workspace `AGENTS.md` lane at
`--agents-file` already proves the pattern — see R9) plus an operator-visible
toggle, not flipping the default back.

### G9 — durable memory is off in both directions

Two independent decisions compound:

1. `skip_memory=not include_profile_memory` (`persona_runtime.py:392`) ⇒
   `MEMORY.md` / `USER.md` are not injected (`agent/agent_init.py:1369-1400`,
   `agent/system_prompt.py:453-462`). Default off; only the seeded `base`
   persona sets `include_profile_memory=True` (`personas.py:330`).
2. The `memory` **tool** is in `PERSONA_BLOCKED_TOOLS`
   (`personas.py:161-169`), and the `memory` toolset is absent from every role
   ceiling — so even with (1) flipped, the agent cannot *write* memory.

`hermes chat` / `acp` / `gateway` / `oneshot` have both halves: `memory_enabled`
and `user_profile_enabled` both default **`True`** in the shipped config
(`hermes_cli/config.py:2103-2106`, `cli-config.yaml.example:545,548` — note the
`.get(..., False)` at `agent_init.py:1372-1373` is only the absent-key
fallback), and the `memory` tool ships in `_HERMES_CORE_TOOLS`. `cron` is the
one lane that also skips memory, and does so deliberately ("cron system prompts
would corrupt user representations", `scheduler.py:2969-2976`).

**Classification:** DELIBERATE ×2 (identity-leak prevention — the "Alice
memory clone" incident — and parallel-authority hygiene). **P1** under a ruling
that makes this lane an agent's permanent home: an agent that lives here can
never accumulate durable knowledge.

**Seam:** `personas.py::PERSONA_BLOCKED_TOOLS` for the write half;
`persona_runtime.py:386-392` + a surfaced per-instance toggle for the read half.

### G15 — skills are surface-filtered on mission-chat and not on `chat`

`mission_chat_reply` pins `skill_surface="mission_chat"`
(`persona_runtime.py:394`); the worker lane pins `"mission_worker"` (L170,
`node_tools.py:301`, `root_node_engine.py:160`). `hermes chat` passes nothing.
`agent/prompt_builder.py:1530-1531, 1586-1590` then drops any skill whose
frontmatter `metadata.hermes.surfaces` excludes the active surface — and drops
**nothing** when the surface is `None`. So a skill declaring
`surfaces: [mission_worker]` is invisible on mission-chat but visible on
`hermes chat`.

**Classification:** DELIBERATE (that's the feature). **P3** — flagged only so
nobody diagnoses a "missing skill" as a bug. Worth an operator-visible receipt
in the same typed-row lane as G5.

### G16 — no slash commands

`/compact`, `/compress`, `/model`, `/skill`, `/checkpoint`, … are
`cli.py`/`gateway/slash_commands.py`/ACP constructs
(`cli.py:3516,6042-6043`). mission-chat has structured CLI analogues for two of
them (`mission-chat queue-skill` at `harness.py:1325`, `persona instance
set-model`) and nothing for the rest — notably no operator-triggered compaction.
**DELIBERATE, P3.**

---

## 5. Budget, lifecycle, and operator affordances

### G10 — the 240 s default working window

`hermes_cli/harness.py:1316` — `--max-seconds`, default `240.0`. The reserve is
`max(60 s, 15 %)` capped so ≥30 s of work survives
(`agent_runtime/turn_budget.py:43-52, 76-88`), so a default turn gets **180 s of
tool-using time** before the checkpoint nudge drains the iteration budget.

| Lane | Time bound | Token / call bound |
|---|---|---|
| `hermes chat` | **none** — only `max_iterations=90` (`cli.py:3851-3862`) | none |
| `acp` | **none** (no timer, no inactivity timeout) | none |
| `oneshot` | **none** | none |
| `cron run\|tick` | **inactivity** 600 s (`HERMES_CRON_TIMEOUT`, `scheduler.py:2981-3030`) | none |
| `gateway run` | **inactivity** 1800 s (`gateway/run.py:19197-19308`) | none |
| harness worker | wall `live_run_max_wall_seconds = 300.0`, per-persona overridable (`runtime_config.py:189`, `config.py:133`, `node_tools.py:202`) | `max_api_calls` / `max_total_tokens` enforced (`profile_runner.py:698-708`) |
| **mission-chat** | **wall 240 s** CLI default; relay hops inherit the shared chain deadline (`persona_commands.py:2004-2019`) | same enforcement available, unset by default |
| free-chat (historical; deleted S61) | formerly wall 120 s | unset |

mission-chat is the only lane with a **wall-clock** bound rather than an
inactivity bound. That is the right primitive (an agent grinding on one long
tool call is still burning the operator's window), but 240 s is a
conversation-shaped number applied to a lane that is now meant to host work.

**Classification:** DELIBERATE. **P1** — the ruling means long-running work
lands here; the default should be a work-shaped budget (or an explicit
"long-run" turn kind), not a conversation-shaped one.

### G17 — no attachments / image input

`hermes chat` accepts `--image` (`hermes_cli/main.py:2363, 2383`,
`main.py:12527`, `cli.py:15968-16035`), auto-attaches a pasted clipboard image
(`cli.py:6302-6315`, `Alt+V` binding at L14109-14117), and supports voice input
(`hermes_cli/voice.py`). The gateway enriches inbound image attachments with a
vision pass before the turn (`gateway/run.py:13166-13171`).

The mission-chat parser (`hermes_cli/harness.py:1296-1322`) has **no**
attachment, image, or file argument, and `mission_chat_reply` has no media
parameter. An operator cannot show a mission-chat agent a screenshot. Combined
with G2 (no `vision_analyze`) the lane is fully blind in both directions.

**Classification:** ACCIDENTAL / never-built. **P2.**

**Seam:** mission-chat parser + `AgentRunRequest.user_message` construction in
`_mission_chat_user_message` (`persona_runtime.py:662`) — noting the
byte-stability invariant documented there.

### G18 — no filesystem checkpoints, no `--worktree` isolation

`hermes chat` has `--checkpoints` (snapshot/rollback via
`tools/checkpoint_manager.py`, wired at `agent/agent_init.py:1308-1309`;
opt-in, off by default — `cli.py:3881`) and `--worktree`
(`hermes_cli/main.py:2364, 2382`). Neither exists on mission-chat in any form.
The worker lane has its own equivalent (`isolated_repo_context_for_run`,
`persona_runtime.py:123-126`), so mission-chat is the only lane with **no**
rollback story for agent file edits. Currently masked by G1 (no file tools);
becomes live the moment G1 is fixed.

**Classification:** ACCIDENTAL. **P2 now, P1 the day G1 lands.**

---

## 6. The meta-gap

### G5 — capability drops on this lane are silent

`agent_runtime/tool_visibility.py::resolve_tool_visibility` (L81-196) emits
`requirement_failures` containing **exactly one** row type today:
`mcp_not_registered_on_lane` (L123-124, L189), produced by
`agent_runtime/mcp_lane.py`. Every other drop in this document — the cost-policy
toolset exclusions (G1/G2), the `skill_manage` cut (G11), the role-ceiling
intersection (G7), the surface-filtered skills (G15), the terminal envelope
(G4) — produces **no typed row at all**. The operator preview does show the
final resolved list (`apply_chat_lane_tool_scope`, `persona_runtime.py:919-961`,
which correctly threads the real chat-lane resolution into the preview), but a
*list of what survived* is not an *account of what was removed and why*.

This is the same defect the MCP work just retired, restated: *"the exclusion is
by design; its invisibility was the defect."* It is now a recurrence, which
makes the shared root cause the finding — **there is no general "this lane
dropped a declared capability" accounting seam**, only a one-off for MCP.

**Classification:** ACCIDENTAL / structural. **P0 and cheap** — it is the
prerequisite for every other item here, because until the drops are typed,
every gap in this document will be rediscovered by an agent burning turns on a
permission-mode goose chase.

**Seam:** generalize `mcp_lane.mcp_lane_requirement_failures` into a
`lane_capability_drops(...)` producer that emits the same
`{code, subject, entry_point_lane, summary, fix_hint}` row shape (already
aligned with `machine_roots.PathTokenIssue.row()`) for every dropper. The
droppers are already pure functions in one place each —
`chat_lane_toolsets.resolve_chat_lane_excluded_toolsets`,
`personas.validate_toolsets`, `chat_lane_toolsets.resolve_chat_lane_excluded_tools`
— so each can return *what it removed* instead of only *what remains*, with no
policy change and no new authority. Suggested codes:
`toolset_dropped_by_chat_lane_policy`, `toolset_not_allowed_for_role`,
`tool_dropped_by_chat_lane_policy`, `skill_filtered_by_surface`,
`command_blocked_by_harness_envelope`.

---

## 7. Reverse gaps — mission-chat has it, other lanes don't

Do not "fix" these by deleting them; several are the reason the lane is
credible as a primary home.

| # | Capability | Evidence | Lanes lacking it |
|---|---|---|---|
| R1 | **Graceful wall-budget checkpoint** + agent-visible budget line + typed `budget_exhausted` terminal state instead of a mid-call kill | `profile_runner.py:179-313, 634-660`; `turn_budget.py`; `persona_commands.py:2004-2019, 2290-2330` | The `WallBudgetCheckpoint` *mechanics* are in the shared runner, so any lane setting `max_wall_seconds` gets them — but the **HUD budget line, the shared relay deadline, and the `budget_exhausted` settle** are mission-chat only. `chat`/`acp`/`gateway`/`cron` have none of it |
| R2 | **Turn journal with write-ahead durability** (`pending → executing → native_committed`, exactly-once, operator `turn-resolve`) | `agent_runtime/mission_chat_turns.py:78-105`; `persona_commands.py:2124-2200` | all others |
| R3 | **Out-of-band steering** — a *separate process* steers a live streamed turn by session id. (`chat`, `acp`, `gateway` all have in-process steering via `AIAgent.steer()` — `cli.py:8765-8788`, `acp_adapter/server.py:1969`, `gateway/run.py:5422` — but only from the same process that owns the turn; `cron`/`oneshot` have none.) | `harness.py:1332-1339`; `agent_runtime/mission_chat_steer.py`; `persona_commands.py:2105-2123` | all others |
| R4 | **Runtime Situation HUD** injected on the user turn (roster, scope, mission, budget) | `agent_runtime/runtime_hud.py:469-540`; `persona_runtime.py:662-700` | all others |
| R5 | **Byte-stable system prompt + codex `cache_scope_id` header routing** (cross-turn prompt-cache preservation) | `persona_runtime.py:611-660`; `profile_runner.py:96-104` | all others |
| R6 | **Resident actor registry** (warm agent reuse) + rotation-based native compression with per-turn overrides | `profile_runner.py:539-583` | all others |
| R7 | **Non-blocking `clarify` bridge** (asks, ends the turn, answer arrives as the next message) | `MissionChatClarifyCapture`, `persona_runtime.py:346, 424`; unblocked at `persona_runtime.py:843-845` | worker lane (inert), `chat` (blocking prompt) |
| R8 | **Agent→agent relay** with depth/cycle/shared-deadline policy | `tools/agent_chat_tool.py:228-234`; `agent_runtime/relay_policy.py` | all others (the toolset is registered globally but the routing is harness-only) |
| R9 | **Targeted workspace `AGENTS.md` injection** (`--agents-file`, one file, receipted) | `harness.py:1308`; `persona_runtime.py:611-660`; `prompt_observability.py:48` | all others (they take the whole cwd context-file sweep or nothing) |
| R10 | **Prompt observability** — per-file in-prompt token attribution, tool-turn history, redaction-safe trace persistence | `agent_runtime/prompt_observability.py`; `agent_runtime/tool_turn_history.py`; `persona_runtime.py:849-878` | all others |
| R11 | **Chat-scoped permission modes** (`profile_default` / `read_only` / `unbounded`) with turn-consuming and expiring grants | `agent_runtime/tool_permissions.py:15-24, 118-142` | all others |
| R12 | **Typed lane capability accounting** (the MCP row) | `agent_runtime/mcp_lane.py` | by construction, only meaningful here |
| R13 | **Per-tool blocklists at all.** `blocked_tool_names` is only ever passed by `profile_runner`; `acp`/`cron`/`gateway`/`oneshot` construct `AIAgent` without it, so they have toolset-level on/off and nothing finer. Registry hygiene (`kanban_*`, `feishu_*`) is likewise harness-only — the "enforced in TWO places so no lane escapes" note at `personas.py:127-133` is true for the agent_runtime family it was written for and **not** for acp/cron/gateway/oneshot, where those tools remain resolvable via `_HERMES_CORE_TOOLS` (`toolsets.py:70-77`). | `profile_runner.py:24-42, 526`; `tool_permissions.py` | all non-harness lanes |
| R14 | **Wall-clock budget as a primitive.** Every other bounded lane uses an *inactivity* timeout (cron 600 s, gateway 1800 s); `chat`/`acp`/`oneshot` have no time bound at all. Only the harness lanes bound total elapsed time, and only they enforce `max_api_calls` / `max_total_tokens` (`profile_runner.py:698-708`). | `profile_runner.py:588-695` | all others |
| R15 | **Skill runtime-surface filtering.** `skill_runtime_scope` has exactly one production call site (`profile_runner.py:463-473`), so a skill's `metadata.hermes.surfaces` declaration is inert everywhere else — `chat`/`acp`/`cron`/`gateway`/`oneshot` always see the unfiltered catalog. Whether that is a mission-chat *gap* (G15) or a mission-chat *feature* depends on the ruling; it is listed on both sides on purpose. | `agent/skill_utils.py:29-55`; `agent/prompt_builder.py:1586-1620` | all others |

---

## 8. Recommended sequencing

Not a decision — a proposal. The operator picks scope.

1. **G5 first (cheap, unblocks diagnosis).** Generalize the typed drop row.
   Every subsequent change becomes verifiable from a `tool-diff` instead of
   from source reading, and agents stop burning turns on permission-mode goose
   chases.
2. **G1 + G6 together (the actual ruling).** A work-posture-aware chat-lane
   policy plus a real workdir. These two are what turn "coordinate and read
   out" into "do the work". Doing G1 without G6 gives an agent file tools with
   no repo to point them at.
3. **G4 + G5b together (never separately).** Give the envelope a
   permission-aware escape hatch *and* a real approval channel in the same
   change — lifting the block without an approval bridge converts hard-blocked
   commands into silently auto-approved ones (`approval.py:2600` fail-open).
4. **G3** per the existing admission design.
5. **G7 / G8 / G9 / G10** — each is a single-constant or single-default
   decision, and each wants an explicit operator ruling on cost vs. capability
   rather than an engineering judgment.
6. **G17 / G18 / G11 / G14 / G13 / G16** — follow-on affordances; G18 becomes
   urgent the day G1 lands.

## Appendix — verified non-gaps

Recorded so they are not re-derived:

* **Plugin discovery** runs on every lane (`model_tools.py:204-208`, module
  import side effect). Plugin toolsets are in the registry on mission-chat.
* **Iteration budget** is identical: 90 on both
  (`profile_runner.py:126`, `agent/agent_init.py:271`, `cli.py:3860-3862`).
* **`SOUL.md`** does reach the mission-chat prompt — via a parallel path
  (`persona_runtime.py:1560-1573`), not the standard `load_soul_identity` one.
* **`session_search`** (cross-session recall) is available on mission-chat.
* **Registry-hygiene blocks** (`kanban`, `feishu_*`) are enforced on every
  harness lane by design and are not a mission-chat-specific drop
  (`personas.py:122-158`, `profile_runner.py:24-42`).
* **`--stream`**, provider/model override, and per-instance `reasoning_effort`
  are all present on mission-chat (`harness.py:1303-1315`,
  `persona_runtime.py:299`, `persona_commands.py:2222-2226`). Streaming is in
  fact **off by default** on `hermes chat` (`cli.py:3740`,
  `hermes_cli/config.py:1731`).
* **`mcp serve` builds no agent.** `mcp_serve.py:543-991` is an MCP *server*
  exposing 9 SessionDB tools; it is in `MCP_REGISTERING_LANES` only because its
  CLI startup path trips the same `_prepare_agent_startup` gate. Treat it as a
  lane label, not a peer agent lane.
* **`send` builds no agent** either (`hermes_cli/send_cmd.py:1-25`).
* **Trajectory capture is off on `hermes chat`** (`save_trajectories=False`;
  `hermes trace upload` is a manual export) — so R10 is a genuine mission-chat
  advantage, not a restoration of parity.

### The `rl` phantom lane

`_AGENT_COMMANDS = {None, "chat", "acp", "rl"}` (`hermes_cli/main.py:12350`)
and `_should_background_mcp_startup` (L12375) both treat `rl` as a live agent
command, and `agent_runtime/mcp_lane.py:55` mirrors it into
`MCP_REGISTERING_LANES`. **There is no `rl` subparser anywhere** — no
`add_parser("rl")`, no alias, in `hermes_cli/_parser.py` or
`hermes_cli/subcommands/`. `hermes rl …` is rejected by argparse today.

Consequence for this audit: `mcp_lane.MCP_REGISTERING_LANES` claims a
registering lane that cannot be invoked, and
`tests/agent_runtime/test_mcp_lane_visibility.py:339-343` guards the mirror
against drift from a source constant that itself carries a dead token. Harmless
today (it can only suppress a row for a lane nobody can reach), but it should
be either removed or given a real subcommand before more policy is keyed on
lane identity. **P3, hygiene.**
