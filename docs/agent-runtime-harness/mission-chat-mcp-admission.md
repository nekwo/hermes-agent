# Selective MCP admission for mission-chat agents — design (2026-07-26)

Status: **R0 + R1 + R2 code shipped, flag OFF; R2's LIVE PROOF still open.**
Owner: fork (`agent_runtime/`, plus one config key). R0 — the typed
`mcp_not_registered_on_lane` failure this document is the **producer** of —
landed first and stands alone (`agent_runtime/mcp_lane.py`, commit `b6277e023`).
R1 landed the admission module, the role/lane policy, the per-run registration
at the `profile_runner` seam, and the `--explain-mcp` operator verb
(`agent_runtime/mcp_admission.py`) with
`agent_runtime.mcp_admission.enabled` defaulting to **false**, so no deployment
changes behavior until an operator turns it on. R2 landed the per-run registry
**teardown** (transport stays warm), the compiled positive `tools.include` with
a hash-pinned parity fixture against the launcher's own allowlist — which pulls
most of R3 forward — and the §D3 agent-visible denial line. **R2's remaining
obligation is the live 6-row acceptance-matrix proof from the harness lane**;
until that runs the flag stays off. See §6 for what each stage actually
contains.

Sibling docs: `harness-serve-design.md` (the warm-process lane this design
depends on), `04-decision-hud-simplification-map.md` (the "agents work
unbounded, the harness reads the work" stance), `12-read-path-freshness-hardening.md`
(the "make the violation impossible or self-announcing" ruling).
Launcher counterpart: `EterniaLauncher docs/stages/qa-reboot/` (Stage C
MCP-only operator path).

---

## The escalation

A Mission Control QA agent, running on the mission-chat lane, was asked to
drive the Launcher and produce visual proof. It could not. The console showed
"Blocked 21 tools", which read like a permission problem and was chased as
one — including a live `hermes harness persona tool-diff qa --permission-mode unbounded`,
which changed nothing.

The finding (2026-07-25): the "Blocked 21" was a coincidence (kanban/feishu/
file-write internals, not launcher_qa's tools). **MCP tools are never
registered on the harness lane at all.** No permission mode can expose a tool
that is absent from the registry. The two working paths are the CLI chat lane
(`hermes -p launcher-qa chat`, where discovery *does* run — proven live, the
agent got and used `mcp__launcher_qa__*` tools) and the `qa.request_screenshot`
decision-contract → `VisualProofRunner` lane.

The exclusion is **deliberate and correct as a default**. What is wrong is
that it is (a) invisible, (b) absolute, and (c) contradicted by three
declarations elsewhere in the same runtime that say a QA persona requires
`launcher_qa`.

---

## Established codebase facts (verified 2026-07-26, do not re-derive)

### The lane gate

`hermes_cli/main.py:12350-12355`:

```python
_AGENT_COMMANDS = {None, "chat", "acp", "rl"}
_AGENT_SUBCOMMANDS = {
    "cron": ("cron_command", {"run", "tick"}),
    "gateway": ("gateway_command", {"run"}),
    "mcp": ("mcp_action", {"serve"}),
}
```

Consulted in **exactly one place** — `_prepare_agent_startup`
(`main.py:12378`), lines 12382-12387:

```python
    _sub_attr, _sub_set = _AGENT_SUBCOMMANDS.get(args.command, (None, None))
    if not (
        args.command in _AGENT_COMMANDS
        or (_sub_attr and getattr(args, _sub_attr, None) in _sub_set)
    ):
        return
```

`"harness"` **is** a registered builtin subcommand
(`_BUILTIN_SUBCOMMANDS`, `main.py:12246-12265`, listed at `:12252`) but is
absent from both sets, so the function returns before plugin discovery
(`:12393`), background MCP discovery (`:12411-12416`), inline
`discover_mcp_tools()` (`:12427-12429`), and shell-hook registration
(`:12439`). `_prepare_agent_startup` is called at `main.py:12509`, `:12537`,
and `:14611`.

### What discovery costs

- `tools/mcp_tool.py:5092` `discover_mcp_tools()` → `_load_mcp_config()`
  (`:5108`) → **delegates to `register_mcp_servers(servers)` (`:5120`)**.
- `tools/mcp_tool.py:4972` `register_mcp_servers(servers: Dict[str, dict])`
  is the real worker: `_filter_suspicious_mcp_servers` (`:4988`), `enabled`
  handling (`:4999`), stale-server wake (`:5006-5022`), parallel-safety
  tracking (`:5015-5019`), then `_discover_and_register_server` (`:4941`) →
  `_connect_server` (`:3883`) → `MCPServerTask.start` (`:2953`).
- Per server: **child-process spawn** `stdio_client(...)` (`:2178`),
  `session.initialize()` (`:2226`), `session.list_tools()` (`:1847`).
- Bounds: outer `_run_on_mcp_loop(..., timeout=120)` (`:5070`);
  `_DEFAULT_CONNECT_TIMEOUT = 60` (`:328`); `list_tools` 30s (`:1930`);
  `_DEFAULT_TOOL_TIMEOUT = 300` (`:327`).
- Importing `tools.mcp_tool` "transitively pulls the full MCP SDK … ~200ms
  on macOS" (`tui_gateway/entry.py:309-315`).
- CLI/TUI lanes background it and join with `mcp_discovery_timeout`
  (**default 1.5s**, `hermes_cli/config.py:1302`; `hermes_cli/mcp_startup.py:53-100`).
- Idempotent within a process: `_servers` (`:3041`) short-circuits already-
  connected servers to `_existing_tool_names()` (`:4818`).
- **There is no persisted cache of discovered MCP tool schemas.** Every fresh
  process pays a full spawn + handshake.

### The mission-chat run chain

1. `hermes_cli/harness_parts/persona_commands.py:1213` `_cmd_mission_chat_message`
2. → `persona_commands.py:2131-2136` `GPTPersonaRuntime(...).mission_chat_reply(...)`
3. → `agent_runtime/persona_runtime.py:289` `mission_chat_reply` builds
   `AgentRunRequest`; toolsets at `:372-373` via
   `_enabled_toolsets_for_chat` (`:881`) and `_blocked_tool_names_for_chat`
   (`:825`)
4. → **`agent_runtime/profile_runner.py:302` `_execute_agent_run`** —
   `with persona_profile_context(...)` at **`:309`**, agent construction at
   **`:343-369`**
5. → `profile_runner.py:2070` `_default_agent_factory` → `AIAgent(**kwargs)`
6. → `agent/agent_init.py:1192` `agent.tools = _ra().get_tool_definitions(...)`
7. → `model_tools.py:363` `_compute_tool_definitions` — reads the live
   `tools.registry.registry`

`persona_profile_context` (`agent_runtime/profile_context.py:83-119`) swings
`HERMES_HOME` to the persona's profile home (`:105`) — so a later
`_load_mcp_config()` *would* read the persona's `mcp_servers` block — but
**nothing on this lane ever calls it**. `hermes_cli/harness_parts/persona_commands.py`
contains zero occurrences of "mcp"; `agent_runtime/` contains zero
`discover_mcp_tools` call sites.

### The three declarations that already exist

| Declaration | Site | What it says |
| --- | --- | --- |
| `AgentPersona.required_mcp_servers: list[str]` | `agent_runtime/models.py:397` | the per-persona MCP requirement |
| `_effective_required_mcp_servers(persona, task, stage)` | `agent_runtime/profile_readiness.py:379-384` | **role policy already written**: `role == "qa"` and `_visual_proof_required(...)` ⇒ append `"launcher_qa"` |
| `_child_enabled_toolsets(persona)` | `agent_runtime/node_tools.py:360-373` | QA nodes already request the `"launcher_qa"` **toolset name** |

The toolset name only resolves via an alias registered *at discovery time* —
`registry.register_toolset_alias(name, f"mcp-{name}")`
(`tools/mcp_tool.py:4936`, `tools/registry.py:281`). The ordering dependency
is documented for the CLI lane at `cli.py:3868-3871`:

```python
            # Validate each toolset — MCP server names are resolved via
            # live registry aliases (registered during discover_mcp_tools),
            # but discovery hasn't run yet at this point, so exclude them.
```

On the harness lane discovery never runs, so `"launcher_qa"` in
`enabled_toolsets` resolves to nothing, silently.

### The hardcoded lie

`agent_runtime/tool_visibility.py:173`:

```python
        "requirement_failures": [],
```

The operator-facing tool-visibility preview — the surface that answers "does
this agent have what it needs?" — is structurally incapable of reporting a
missing requirement.

### Existing controls (reuse, do not duplicate)

- **Permission modes** — `agent_runtime/tool_permissions.py:15-24`:
  `profile_default | read_only | unbounded`; `READ_ONLY_BLOCKS` (`:14`);
  store `ChatToolPermissionStore` (`:40`) at
  `paths.store_root()/tool_permissions.json`. `unbounded` ⇒
  `all_registered_toolsets()` and `blocked_tool_names = []`
  (`persona_runtime.py:881`, `:825`).
- **Per-MCP-server tool allowlist (a real allowlist, config-driven)** —
  `mcp_servers.<name>.tools.include` / `.exclude`, implemented at
  `tools/mcp_tool.py:4854-4863` (`_normalize_name_filter`, `_should_register`);
  include wins over exclude.
- **Name denylist** — `blocked_tool_names` threaded
  `AgentRunRequest` → `profile_runner._blocked_tool_names_with_registry_hygiene`
  (`:22`) → `agent_init:1192` → `model_tools.get_tool_definitions`.
- **Config resolution + typed path/platform gating** —
  `agent_runtime/machine_roots.py`: `resolve_mcp_servers` (`:583`),
  `PathTokenIssue` (`:99`, `.row()` at `:109`), `MachineRootError` (`:119`),
  codes at `:75-79` (`unbound_root`, `root_target_missing`,
  `invalid_root_token`, `invalid_registry`, `platform_unsupported`).
- **Readiness taxonomy** — `agent_runtime/profile_readiness.py:15-37`,
  including **`READINESS_MCP_ATTENTION = "mcp_attention"`** (`:19`), already
  ranked in `_SEVERITY` and already emitting `missing_mcp_servers`.
- **Per-session dynamic registration precedent** —
  `acp_adapter/server.py:792-820` `_register_session_mcp_servers`: builds a
  config map from the session and calls
  `await asyncio.to_thread(register_mcp_servers, config_map)`. **The only
  existing per-session registration surface in the repo, and the model for
  this design.**
- **Late binding** — `tools/mcp_tool.py:5327` `refresh_agent_mcp_tools`
  (generation-guarded via `registry._generation`).
- **Cheap inspection without registering** — `tools/mcp_tool.py:5236`
  `probe_mcp_server_tools` (connect, list, disconnect) and `:5159`
  `get_mcp_status` (reports `connected|disabled|connecting|failed|configured`
  without connecting).
- **No call-time approval for MCP tools.** MCP dispatch goes through
  `_make_tool_handler` (`mcp_tool.py:4890`) with **no permission
  consultation**. The tool list is the entire control surface.
- **`mcp_not_registered_on_lane` does not exist anywhere in the repo**
  (0 matches). There is no `failures.py`. This code is net-new.
- **No test asserts MCP tools are, or are not, present on the mission-chat /
  harness lane.** The behavior is an untested emergent property of a set
  literal ~12,000 lines from the runner that suffers it.

### The launcher_qa surface

- Declared in a persona profile's `config.yaml` under `mcp_servers.launcher_qa`;
  canonical portable form in
  `agent_runtime/docs/machine_roots_path_portability.md:40-47` (`platforms: [windows]`,
  `${roots.eternia_launcher}` tokens, `${exe_suffix}`).
- **25 tools** today (`EterniaLauncher tool/stagec_qa_mcp_server/lib/tools.dart:78`,
  `kStageCQaMcpTools`). CLAUDE.md's "21-tool surface" is the Stage 18 count;
  the surface has grown.
- A **second, hand-rolled MCP client for the same server already lives in
  `agent_runtime/`**: `stagec_mcp_visual_provider.py` — `StageCMcpJsonRpcClient`
  (`:78`), `resolve_launcher_qa_mcp_config` (`:310`, typed
  `StageCMcpConfigResolution` at `:58` with codes
  `missing | config_unreadable | platform_unsupported | <machine-root code> | ready`),
  `smoke_launcher_qa_mcp` (`:386`, codes
  `command_missing | not_ready | tool_missing | ready`),
  `default_launcher_qa_visual_provider` (`:293`). Defaults `timeout: 260s`,
  `connect_timeout: 60s` (`:45-46`). This is the `qa.request_screenshot` /
  `VisualProofRunner` lane.
- **The launcher already ships a per-profile per-tool allowlist**:
  `EterniaLauncher docs/stages/qa-reboot/launcher_qa_profile_allowlists.yaml`
  (v1, 2026-05-17) — full glob `mcp_launcher_qa_*` for
  `launcher-qa` / `launcher-qa-direct` / `gpt_launcher` / `claude_launcher_qa`;
  read-only subsets for `alice` / `pm` / `reviewer` with
  `mcp_launcher_qa_kill_launcher`, `..._launch_or_attach`, `..._click_button`
  explicitly denied. Its own header states:

  > The MCP server does **NOT** enforce this (Hermes does not natively gate
  > per-tool); workers consume the file as an advisory allowlist and log
  > warnings when a denied tool is invoked. **Native enforcement is a
  > separate Hermes-core initiative.**

---

## 1. What is weak, and why it will recur

**W1 — Three declarations, zero enforcement, one hardcoded "no problems".**
The runtime says a QA persona requires `launcher_qa` in
`models.py:397`, decides it by role in `profile_readiness.py:379-384`,
requests its toolset in `node_tools.py:360-373` — and then the lane that
actually runs the persona registers nothing, and the preview an operator
reads reports `requirement_failures: []` (`tool_visibility.py:173`). A
capability drop that three subsystems predicted and a fourth denies is the
textbook silent drop.

**W2 — The exclusion is expressed 12,000 lines from the code it governs, and
is untested.** `_AGENT_COMMANDS` is an argparse-level set literal in
`hermes_cli/main.py`; the victim is `ProfileAgentRunner` in `agent_runtime/`.
Nothing connects them, nothing documents the relationship, and no test pins
it in either direction. Anyone "fixing" this by adding `"harness"` to the set
would enable blanket discovery on the **snapshot poll lane** — a 120s-bounded
spawn storm on the path the launcher polls continuously. The current shape
makes the correct fix and the catastrophic fix look equally plausible.

**W3 — The absence is invisible to the agent, which then improvises.** A QA
agent asked to drive the Launcher sees a tool list with no
`mcp__launcher_qa_*` and no explanation. Being a language model, it invents
alternatives — which is precisely how PowerShell invocations end up in agent
output, and why the launcher repo now needs a Stage 25 grep gate
(`tool/stagec_qa_mcp_server/test/no_agent_ps1_test.dart`) forbidding agents
from writing `pwsh -File` / `.ps1` call-operator forms. **The lane exclusion
sits upstream of a class of agent misbehavior another repo is separately
policing.** Telling the agent the truth is cheaper than fencing every
workaround it can invent.

**W4 — Each workaround is a second authority.** Two exist:
- `hermes -p launcher-qa chat` — an entire second process and lane. Its
  result is not linked to the mission's proof record, and its profile config
  carries a stale model (`gpt-5.5` → anthropic 404; operators must pass
  `--provider openai-codex -m gpt-5.6-sol`). Two lanes for one capability is
  the parallel-authority smell the master prompt forbids.
- `agent_runtime/stagec_mcp_visual_provider.py` — a **hand-rolled stdio
  JSON-RPC MCP client inside `agent_runtime/`**, with its own config
  resolution, its own smoke test, its own timeouts, its own spawn, parallel
  to `tools/mcp_tool.py`. It works and it is well-built, but it is a second
  MCP client that will drift from the first.

By the master prompt's rule — *if a feature appears to require bypassing an
existing layer, stop and redesign at the correct abstraction level* — the
escalation is correctly raised.

**Why it will recur.** The forcing function is not going away: Stage C QA is
**MCP-only** for agents by policy (CLAUDE.md, post-Stage-18). Every future QA
capability lands as MCP tools; every one of them will be unavailable to the
lane that the harness actually runs QA agents on; and each gap will be
diagnosed from scratch as a permission problem, because that is what the
console appears to say. The 2026-07-25 investigation burned an afternoon on
"Blocked 21 tools" before finding a set literal.

---

## 2. Target shape

**Selective, declared, per-run admission — never a lane blanket flip.**

**`_AGENT_COMMANDS` stays exactly as it is.** The harness process never
performs blanket discovery, and the snapshot/status/stream poll lanes stay
MCP-free forever. Admission happens at the one place that already knows
*which persona is about to run under which profile*:
`ProfileAgentRunner._execute_agent_run` (`profile_runner.py:302`) — inside
`persona_profile_context` (entered `:309`, so `HERMES_HOME` already points at
the persona's profile) and before `self._agent_factory(...)` (`:343`).
Modeled directly on `acp_adapter/server.py:792-820`.

### A. Admission policy — role → allowed servers, deny-by-default

NEW `agent_runtime/mcp_admission.py`:

```python
@dataclass(frozen=True, slots=True)
class McpAdmissionDenial:
    server: str
    code: str          # mcp_not_admitted_for_role | mcp_not_registered_on_lane
                       # | mcp_admission_disabled | mcp_admission_lane_busy
                       # | mcp_admission_timeout | <machine_roots code>
    summary: str
    fix_hint: str = ""

    def row(self) -> dict: ...      # same shape as PathTokenIssue.row()


@dataclass(frozen=True, slots=True)
class McpAdmission:
    server_names: tuple[str, ...]
    denied: tuple[McpAdmissionDenial, ...]
    tool_include: Mapping[str, tuple[str, ...]]   # per-server allowlist


def resolve_mcp_admission(persona, *, task=None, stage=None,
                          lane: Lane, permission_mode: str) -> McpAdmission
```

Resolution runs in order, and **every step can only narrow**:

1. **Requested** = `_effective_required_mcp_servers(persona, task=task, stage=stage)`
   — the existing function (`profile_readiness.py:379-384`), promoted from a
   readiness *reporter* to the actual *request*. The role policy
   (`qa` + visual proof ⇒ `launcher_qa`) is already written there; **do not
   write a second copy.**
2. **Admitted** = intersect with the lane's role allowlist, declared in root
   runtime config, deny-by-default:
   ```yaml
   agent_runtime:
     mcp_admission:
       enabled: false                    # global kill switch
       connect_timeout_seconds: 20       # well under the chat turn budget
       roles:
         qa:
           mission_chat: [launcher_qa]
   ```
   A role with no entry admits **nothing**. An unknown lane admits
   **nothing**. There is no wildcard.
3. **Resolvable** = each admitted name must resolve out of the persona
   profile's `config.yaml` through the **existing** resolver,
   `machine_roots.resolve_mcp_servers` (`:583`), reusing its typed codes
   (`machine_roots.py:75-79`). Anything unresolvable becomes a typed denial —
   never a silent drop, which is what `resolve_mcp_servers` does today
   (`_log_issue`, `:637`).
4. **Tool-scoped** = the per-server allowlist compiles into the **existing**
   `mcp_servers.<name>.tools.include` mechanism (`mcp_tool.py:4854-4863`).
   No new filtering code path.

### B. The launcher's advisory allowlist finally gets an enforcer

`launcher_qa_profile_allowlists.yaml` already declares, per profile, which of
the 25 tools are permitted — and says in its own header that nothing enforces
it. **This design is that "separate Hermes-core initiative"**, with one
correction: enforcement belongs on the **admission** side, compiled into
`tools.include`, so a denied tool is never in the model's tool list at all.
An advisory list that an autonomous agent is trusted to respect is not a
control; it is a comment.

### C. Registration is scoped, single-flight, and torn down

`tools.registry.registry` and `tools/mcp_tool._servers` are **process-global**,
and a harness process is multi-persona: it builds snapshots, runs reconciles,
ticks the daemon, and executes N persona turns under N different
`persona_profile_context` scopes — under `serve`, in a
`ThreadPoolExecutor(4)` (`harness-serve-design.md` §Concurrency). Therefore:

- Admission takes a **process-wide mutex**. A second concurrent admission
  request is refused with `mcp_admission_lane_busy` rather than interleaving
  two personas' MCP scopes against one global registry. QA turns are long and
  rare; serializing them is cheap and correct.
- On run exit, the admitted **registry scope** is removed (tool entries +
  toolset alias) so the next persona's turn cannot inherit it. Whether the
  *connection* is also torn down is a performance/isolation trade — see
  §Performance and **open question 2, which is the single most important
  unknown in this design.**
- `register_mcp_servers({name: cfg})` is used for the admitted set only.
  **`discover_mcp_tools()` is never called anywhere in this design** — it
  would register everything in the profile's config, which is exactly the
  blast radius we are refusing.

### D. The silent drop becomes typed and visible — on three surfaces

1. **`tool_visibility.py:173`** — `requirement_failures` becomes real,
   carrying `mcp_not_registered_on_lane` plus any admission denials. Contract:
   a list of `{code, server, summary, fix_hint}` — deliberately the same
   shape as `PathTokenIssue.row()` (`machine_roots.py:109`) so existing
   operator surfaces already know how to render it.
2. **Readiness** — reuse `READINESS_MCP_ATTENTION` (`profile_readiness.py:19`),
   already severity-ranked and already emitting `missing_mcp_servers`. No new
   taxonomy.
3. **The agent's own turn context** — when a declared server is denied or
   unavailable, the turn states it plainly, with the sanctioned alternative:
   *"launcher_qa is declared for this role but is not available on this lane
   (`<code>`: `<summary>`). Use the `qa.request_screenshot` decision contract
   for visual proof."* This retires **W3**: the agent stops improvising and
   uses the lane that works.

### Relationship to the parallel `mcp_not_registered_on_lane` work

That work should **land first and independently**. It converts today's
silent drop into an honest refusal, needs none of this design, and is a
strict improvement over `[]` on its own. This design then converts the honest
refusal into an honest *admission* for exactly one role. **If admission is
never scheduled, the typed failure still stands** — that ordering is
deliberate.

### Deliberate non-goals

- Never widen `_AGENT_COMMANDS`, and never call `discover_mcp_tools()` on the
  harness lane.
- No change to the launcher_qa MCP server or its 25 tools.
- `qa.request_screenshot` / `VisualProofRunner` is **not** retired — it stays
  the fallback, and the only lane on platform-unsupported hosts.
- No new permission-mode; admission composes with the existing three.
- No per-tool call-time approval gate (a much larger, upstream-touching
  change; the tool list stays the control surface).

---

## 3. Security analysis

**Threat model.** A mission-chat agent is a language model under adversarial
pressure (prompt injection via repo content, web content, or a relayed
message from another agent) and under mechanical pressure (loops — the
harness already ships an AS0 liveness watchdog and `no_freeze_monitor`
precisely because indefinite hangs are real). Admission must be safe under
both. "The agent is well-behaved" is not a control.

**What admitting `launcher_qa` actually grants** (25 tools,
`tools.dart:78`):

| Class | Tools | Risk if looped or injected |
| --- | --- | --- |
| **Process control** | `launch_or_attach`, `open_app_tab`, **`kill_launcher`** | **Highest.** Live precedent, 2026-07-25: a QA agent's `open_app_tab(reap_stale=true)` name-reaped every `eternia_launcher.exe` — the user's live session included — and killed the serve child executing its own turn, freezing that turn at `executing`. Mitigated launcher-side by manifest-scoped reap (`5ff6b295`) and job-escape spawn (`48cfed89`), but the capability class is real. |
| **UI mutation** | `set_tab`, `scroll`, `scroll_to`, `scroll_to_fixture`, `click_button`, `dismiss_hashtag_onboarding` | Drives a real authenticated app window. |
| **Credential-adjacent** | `begin_pkce_login` | Initiates an OAuth flow. |
| **Capture** | `screenshot_window`, `capture_screenshot` | Screen content of an authenticated session. |
| **Read-only** | `get_*` (×9), `wait_for_state`, `read_trace`, `read_artifact_index`, `run_redaction_scan` | Low. |

**The containment that does the heavy lifting is launcher-side, and this
design must not weaken it.** The Stage C control server binds only under
`main_marionette` **and** `kDebugMode` **and** `ETERNIA_QA_CONTROL=1`
**and** `profile == stagec-smoke`
(`stagec_direct_qa_control_server.dart:133-157`), and the tool schema enums
hardcode `stagec-smoke` (`tools.dart:1101/1199/1214`). Auth is
profile-namespaced, so an attached user session is a *different identity*.
**Consequence: an admitted `launcher_qa` cannot drive the user's real
session.** That is the single most important security fact here, and
"drive the user's live launcher" remains an unmade product decision, out of
scope for this design.

**Interaction with permission modes — two hard rules.**

The chat-lane permission system (`tool_permissions.py`) is a *toolset/name*
system, not a call-time approval system. That produces two requirements:

1. **`unbounded` must never widen the admitted MCP set.** `unbounded` sets
   `enabled_toolsets = all_registered_toolsets()` and
   `blocked_tool_names = []` (`persona_runtime.py:881`, `:825`). If admitted
   tools were ordinary registry entries, an unrelated persona running
   `unbounded` in the same process would inherit every admitted server.
   Therefore the admitted set is resolved from the **persona's own
   declaration** and intersected *after* `_enabled_toolsets_for_chat`.
   **Pin this with an explicit test**: an `unbounded` non-QA persona sees no
   `mcp__launcher_qa_*` tool even while a QA admission is live in the same
   process.
2. **`read_only` must subtract.** A `read_only` QA turn admits `launcher_qa`
   with the **reviewer-shaped** tool include list (already written in the
   launcher allowlist YAML: `get_*`, `read_trace`, `read_artifact_index`,
   `run_redaction_scan`; `kill_launcher` / `launch_or_attach` / `click_button`
   denied), not the full glob. Permission mode selects which allowlist row
   compiles into `tools.include` — a clean composition, no new concept.

**Why deny-by-default stays.**

- MCP registration is **process-global** and the harness process is
  multi-persona. A default-open policy is a cross-persona capability leak by
  construction, not by accident.
- There is **no call-time approval gate** for MCP tools
  (`_make_tool_handler`, `mcp_tool.py:4890`). The tool list is the entire
  control surface, so a capability in the list is a capability that will
  eventually be called.
- MCP servers are **arbitrary local executables** (`command`/`args`/`env`).
  `_filter_suspicious_mcp_servers` (`:3792`) and `hermes_cli/mcp_security.py`
  screen at save and spawn time, but admitting an arbitrary configured server
  to an autonomous lane is a materially different act from a human running
  `hermes chat` and watching the output.
- **The blast radius is asymmetric.** Cost of denying: a QA agent uses
  `qa.request_screenshot`, which already works. Cost of over-admitting: a
  looping agent holding process-control tools. There is no symmetry argument
  for defaulting open.

**Residual risks, accepted with mitigations:**

- *Loop → repeated launches/kills.* Per-run admission budget (max tool calls
  per admitted server per run), surfaced as typed
  `mcp_admission_budget_exhausted`; plus the existing AS0 liveness watchdog.
- *Compromised MCP server binary.* Out of scope — same trust boundary as the
  repo checkout the agent already edits. Note `smoke_launcher_qa_mcp`
  (`stagec_mcp_visual_provider.py:386`) verifies the surface *advertises*
  `mcp_launcher_qa_open_app_tab`; it is not an integrity check.
- *Screenshot leakage.* Capture already routes through the redaction lane
  (`run_redaction_scan`, `raw_local_sanitizer.dart`); admission changes
  nothing there.
- *Admission itself as a DoS.* A persona that repeatedly triggers admission
  of a slow server holds the single-flight mutex. Bounded by
  `connect_timeout_seconds` (20s) + typed `mcp_admission_timeout`.

---

## 4. Performance

Measured facts, not estimates (sources in §Established facts):

| Cost | Value | Source |
| --- | --- | --- |
| Import `tools.mcp_tool` (MCP SDK) | ~200ms | `tui_gateway/entry.py:309-315` |
| Per-server spawn + `initialize` | bounded 60s (`_DEFAULT_CONNECT_TIMEOUT`) | `mcp_tool.py:328`, `:2178`, `:2226` |
| Per-server `list_tools` | bounded 30s | `mcp_tool.py:1930` |
| Whole discovery | bounded 120s | `mcp_tool.py:5070` |
| `launcher_qa`'s own declared bounds | `timeout: 260s`, `connect_timeout: 60s` | `stagec_mcp_visual_provider.py:45-46` |
| CLI/TUI join bound | 1.5s default | `hermes_cli/config.py:1302` |
| Repeat registration, same process | near-free (`_servers` short-circuit) | `mcp_tool.py:3041`, `:4818` |
| Cross-process schema cache | **none exists** | — |

Design consequences:

1. **Never on the poll lane.** Snapshot / status / stream argv stay MCP-free.
   Admission is triggered by a persona **run**, which is exactly why the
   insertion point is `profile_runner`, not `_AGENT_COMMANDS`.
2. **Persistent-within-serve, not per-turn.** Under `harness serve` the
   process is long-lived, so the first admitted QA turn pays spawn + handshake
   once and later turns hit `_servers` for free. **This is a strong argument
   for rolling out on the serve lane first** and against the one-shot CLI
   fallback lane, which would re-pay full cost every turn (and, for
   `launcher_qa`, potentially re-attach a Flutter window).
3. **Bound admission well under the turn budget.** Proposal: 20s connect for
   a mission-chat admission (vs the 60s default), degrading to a typed
   `mcp_admission_timeout` + the `qa.request_screenshot` fallback rather than
   blocking. A QA turn that stalls 120s waiting for a launcher to boot is a
   worse outcome than one that reports honestly and takes the proof lane.
4. **Teardown vs warmth.** Full teardown restores isolation but forfeits the
   warm-process win. **Proposal: keep the server connected, tear down the
   registry scope** — leave `_servers` warm while removing the tool entries
   and toolset alias, so a non-admitted persona cannot see them. This depends
   on whether `tools/registry.py` supports scoped removal (**open question 2**).
   If it does not, R2 must either tear down the connection (accepting the
   re-spawn cost per QA turn) or rely on single-flight alone as the isolation
   boundary — which is weaker and should be stated plainly rather than
   assumed. — **IMPLEMENTED as proposed (R2).** Measured alternative: ~489 ms
   for an admission that spawns, observed live on this host. The warm path pays
   a registry re-register only. The cost that actually decided it is not the
   milliseconds but the *side effect*: re-spawning `launcher_qa` per turn means
   re-attaching a Flutter window per turn, which is the capability class behind
   the 2026-07-25 reap incident.

---

## 5. Blast radius

| File | Change |
| --- | --- |
| `agent_runtime/mcp_admission.py` | **NEW** — policy resolution, typed denials, single-flight, scope lifecycle |
| `agent_runtime/profile_runner.py` | `_execute_agent_run` (`:302`): admission between `persona_profile_context` (`:309`) and `_agent_factory` (`:343`); scope teardown in the same `finally` |
| `agent_runtime/persona_runtime.py` | `_enabled_toolsets_for_chat` (`:881`) intersects the admitted set **after** permission-mode resolution (the `unbounded` rule) |
| `agent_runtime/tool_visibility.py` | `requirement_failures` (`:173`) becomes real |
| `agent_runtime/profile_readiness.py` | `_effective_required_mcp_servers` (`:379-384`) promoted to the request path; no logic change |
| `agent_runtime/config.py` | `agent_runtime.mcp_admission` root config block |
| `hermes_cli/harness.py` + `harness_parts/` | `persona tool-diff --explain-mcp` prints the resolved admission without registering |
| `docs/agent-runtime-harness/00-index.md` | link this doc under Operator forensics *(follow-up; not in this commit)* |

**Not touched:** `hermes_cli/main.py` (`_AGENT_COMMANDS` unchanged),
`tools/mcp_tool.py`, `tools/registry.py`, `model_tools.py`, `agent/agent_init.py`,
the launcher_qa server, `qa.request_screenshot` / `VisualProofRunner`,
`stagec_mcp_visual_provider.py`. Upstream (non-fork) files stay untouched —
the fork boundary (`agent_runtime/` + `hermes_cli/harness.py`) holds, which
is deliberate: this is the reason admission goes in `profile_runner` rather
than in the upstream `tools/mcp_tool.py` registration path.

**R2 addendum — the upstream surface this fork now CALLS (still no upstream
edit).** `registry.deregister` / `get_tool_names_for_toolset` /
`get_toolset_alias_target` are public. `tools/mcp_tool._servers` and
`_register_server_tools` are private, called only from
`mcp_admission._default_registrar`'s warm path. That is a deliberate, documented
coupling with three mitigations: it is the ONLY such call site, it fails closed
(no seam ⇒ no tools ⇒ typed row), and
`tests/agent_runtime/test_mcp_admission_r2.py::test_the_upstream_warm_registration_seam_exists`
pins the symbols, the signature, and the short-circuit the design depends on, so
upstream drift fails a test rather than the QA lane.

**Contracts:** `requirement_failures` gains real rows (was always `[]`, so
any consumer already tolerates a list — additive). Root runtime config gains
one optional block, absent ⇒ disabled. Readiness reuses an existing code. No
snapshot/stream envelope version bump. **No launcher change is required.**

---

## 6. Rollout

### R0 — Typed failure only *(the parallel work; ships alone)*
`requirement_failures` carries `mcp_not_registered_on_lane`;
`mcp_admission.enabled: false`; no registration anywhere. Zero behavior
change, strictly more honest. **Independently valuable; do not couple it to
the rest.**

### R1 — Admission module + policy + per-run registration, flag OFF *(SHIPPED)*
Full resolution, typed denials, the `--explain-mcp` operator verb, **and** the
registration side effect at the `profile_runner` seam — single-flight, bounded,
degrading to typed rows. Scoped by the operator to include registration (rather
than the originally-drafted inspect-only R1) because a policy nobody can execute
cannot be live-proven, and the kill switch makes shipping it dark equivalent to
shipping it inert. `agent_runtime.mcp_admission.enabled` defaults to `false`, so
with no config edit the behavior is byte-identical to R0.

What R1 contains:

- `agent_runtime/mcp_admission.py` — `resolve_mcp_admission` (pure; zero
  spawns, test-pinned), `admit_mcp_servers` (the only side-effecting entry
  point), `scope_toolsets_to_admission` (the `unbounded` rule),
  `admission_requirement_failures` (R0 rows + typed denials, one row per
  declared server).
- `agent_runtime/profile_runner.py` — admission runs inside
  `persona_profile_context` and before `_agent_factory`; two fork-owned
  chokepoints at agent construction (`_enabled_toolsets_for_run`,
  `_blocked_tool_names_for_run`) that no call site can opt out of.
- `agent_runtime/persona_runtime.py` — one admission resolved per mission-chat
  turn, threaded into both the toolset scope and the run request so they can
  never disagree; `_enabled_toolsets_for_chat` applies the scope AFTER
  permission-mode resolution.
- `agent_runtime/config.py` + `runtime_config.py` — the
  `agent_runtime.mcp_admission` root block, deny-by-default at every parse step.
- An **R1 stage floor** (`R1_ADMISSIBLE_ROLES = {"qa"}`) that refuses any role
  outside it even when the config names one. R4 retires the floor; until then a
  config edit alone cannot widen the role set.

**Not in R1:** teardown (see open question 2, now answered — R2 owns it), and
the compiled positive `tools.include` with launcher-YAML parity (R3). R1's
`read_only` composition is the SUBTRACTION half: a `tools.exclude` of the
mutating tools compiled into the existing per-server filter, plus the same names
in `blocked_tool_names` as the warm-process backstop.

### R2 — Teardown + positive include + §D3, flag still OFF *(CODE SHIPPED; live proof owed)*

What R2 contains:

- **Scoped teardown** (`teardown_mcp_admission`) at the end of every admitted
  run, wired into `profile_runner._execute_agent_run` through an `ExitStack`
  entered LAST — so it unwinds FIRST, on the raised path as well as the returned
  one, while the run still holds `_WORKDIR_LOCK` and is still inside
  `persona_profile_context`. It deregisters the `mcp-<server>` tools; the
  registry drops the toolset check **and the bare-server-name alias** with the
  last tool. **Registry only — the transport stays warm.**
- **Warm-aware registration.** Teardown made the R1 registrar insufficient:
  `register_mcp_servers` short-circuits on a connected server, so a torn-down
  warm server could never get its tools back. `_default_registrar` now splits
  the admitted set — warm servers re-register off their live session through the
  upstream `_register_server_tools` seam (no spawn, no handshake), cold ones go
  through `register_mcp_servers`. Both apply the per-run tool filter.
- **The `profile_default` → `read_only` sequence therefore subtracts for real,
  at registration time** — the R1 consequence recorded in open question 2 is
  retired. `blocked_tool_names` stays as the resident-actor backstop.
- **Positive `tools.include` + parity fixture** (most of R3, pulled forward
  because teardown is what made a positive include meaningful):
  `READ_ONLY_INCLUDED_TOOLS` is the launcher YAML's `reviewer` row, pinned by a
  vendored, hash-recorded snapshot at
  `tests/agent_runtime/fixtures/launcher_qa_profile_allowlists.yaml`. Hermes owns
  the policy (open question 6) — the launcher file is never read at admission
  time.
- **§D3 agent-visible line** (`render_mcp_admission_line`) on the runtime-context
  envelope's volatile tail, beside the wall-budget line.

**Live proof still required** (this is a harness project — code inspection is
not proof): a mission-chat QA agent drives the 6-row acceptance matrix from
`STAGEC_AGENT_MCP_RECIPES_2026-05-17.md` **from the harness lane**, against the
persistent QA window (`48cfed89`), with the user's own launcher session
verifiably untouched. Attach the screenshots to the goal's proof record — which
is the whole point of doing this on the mission lane rather than in a side CLI.
Until then the flag stays **off**; turning it on is the config edit in the Log
below.

### R3 — Remaining permission-mode work
The `read_only` half shipped in R2. What is left: a `pm` / `alice`-shaped row
(those launcher profiles allow `capture_screenshot` / `screenshot_window` /
`wait_for_state`, which `reviewer` denies) if a non-reviewer restricted shape is
ever wanted on this lane, and retiring the "not enforced" caveat in the launcher
YAML's own header now that hermes compiles an equivalent policy.

### R4 — Additional roles/servers *(only on request)*
Each new `(role, lane, server)` triple is a deliberate **config** edit plus a
written security note — never a code change. If adding a server requires
touching `mcp_admission.py`, the policy shape is wrong.

---

## 7. Test plan

**R0 — honesty**
- A QA persona with `required_mcp_servers: ["launcher_qa"]` running on the
  mission-chat lane produces `requirement_failures` containing
  `mcp_not_registered_on_lane` naming `launcher_qa`.
- The same persona on the CLI chat lane (discovery run) produces `[]`.
- **Lane pin (the missing test):** assert that `discover_mcp_tools` is *not*
  called for `args.command == "harness"` — the invariant that has never been
  tested. Mirrors `tests/cron/test_scheduler_mcp_init.py:46-51`, which
  already asserts the positive and negative for cron.

**R1 — policy, no side effects**
- Deny-by-default table: unknown role ⇒ `()`; role present, lane absent ⇒
  `()`; `enabled: false` ⇒ `()` with `mcp_admission_disabled`.
- Requested-but-not-admitted ⇒ `mcp_not_admitted_for_role`.
- Admitted-but-unresolvable ⇒ the **existing** `machine_roots` code
  (`platform_unsupported` on a non-Windows host, `unbound_root` with no
  registry entry) — proving reuse, not a parallel resolver.
- **`resolve_mcp_admission` performs zero spawns** (assert with a patched
  `register_mcp_servers` that fails the test if called).
- `--explain-mcp` output is stable and machine-readable.

**R2 — registration + isolation**
- Admitted QA run: `mcp__launcher_qa_*` present in
  `model_tools.get_tool_definitions`; the `launcher_qa` toolset alias
  resolves.
- **Cross-persona isolation:** a non-QA persona running in the same process,
  **with `permission_mode=unbounded`**, sees **no** `mcp__launcher_qa_*`
  tool — during a live QA admission and after teardown. *This is the security
  acceptance test.*
- **Single-flight:** two concurrent admissions → the second returns
  `mcp_admission_lane_busy`, never a partially-registered interleave.
- Teardown: after the run, the registry has no `launcher_qa` tools or alias
  (and, per open question 2, the connection state matches whichever choice
  was made).
- Timeout: a stalled server yields `mcp_admission_timeout` within the
  configured bound; **the turn still completes** with the fallback stated in
  context.
- Budget: exceeding the per-run call budget yields
  `mcp_admission_budget_exhausted` and no further dispatch.

*Shipped as `tests/agent_runtime/test_mcp_admission_r2.py` (38 tests). Beyond
the list above it also pins: teardown removes the toolset AND the
bare-server-name alias while the transport stays warm; a following admission
re-registers cleanly (the lifecycle is a cycle, not a one-way door); the
`profile_default` → `read_only` sequence subtracts at REGISTRATION time,
exercised through the real `_register_server_tools`; teardown failure and a
teardown that cannot take the admission mutex are typed and non-fatal; the
runner tears down on the RAISED path as well as the returned one; the upstream
warm-registration seam exists and fails closed when it does not; and the §D3
line is present per denial code, absent on a clean admission, and volatile.
**Not implemented: the per-run call budget** (`mcp_admission_budget_exhausted`)
— still owed, and the AS0 liveness watchdog plus single-flight are what bound a
looping agent until it lands.*

**R3 — permission composition**
- `read_only` QA ⇒ `tools.include` is the reviewer subset;
  `mcp_launcher_qa_kill_launcher` is **absent from the model's tool list**
  (not merely blocked).
- `profile_default` QA ⇒ the role's row.
- Parity check: the compiled include for each profile equals the launcher
  YAML's resolved allow-set for that profile (denied wins over allowed;
  trailing-`*` glob only) — a fixture-based drift test mirroring the
  launcher's own Stage 22 drift test.

*The `read_only` / `profile_default` / parity rows shipped early, in R2 — a
positive include only becomes meaningful once teardown makes re-registration
real, so splitting them across two stages would have shipped an include list
that could not take effect. The fixture is hash-pinned and vendored, so the test
never depends on the launcher checkout existing.*

**Gates.** Full `tests/agent_runtime` + `tests/hermes_cli/test_harness_cli.py`
green at every stage. Do not adopt or mask pre-existing failures. **Commit
before any sabotage-verify run** (standing rule from the transport
workstream).

---

## 8. Rollback

**Kill switch:** `agent_runtime.mcp_admission.enabled: false` in root runtime
config — a config edit, honored on the next persona run; on a live serve it
needs at most a serve-child recycle, never a redeploy or a venv change.

| Stage | Rollback | Residue |
| --- | --- | --- |
| R0 | revert; `requirement_failures` returns to `[]` | none — but note this **removes honesty**; prefer keeping R0 even if R1+ are reverted |
| R1 | revert the module + config block | none (no side effects were ever taken) |
| R2 | `enabled: false` (config), or revert the `profile_runner` hook | a warm MCP child may outlive the flag flip until the serve child recycles — bounded, and the registry scope is already torn down per run |
| R3 | drop back to the R2 include lists | none |
| R4 | remove the config triple | none |

**Reversibility property:** every stage is a strict *widening* of capability
from a deny-by-default floor, so a rollback can only reduce what an agent can
reach. The failure mode of a bad rollout is "the QA agent falls back to
`qa.request_screenshot`" — the lane that works today. There is no state
migration, no schema change, and no persisted artifact to unwind.

**One asymmetry to state plainly:** R2 is the first time an autonomous
mission-chat agent can spawn a local executable that controls a GUI process.
If that turns out to be wrong, the rollback is instant, but any *actions
already taken* by an admitted agent (a launched or killed QA window) are not
undone by it. That is the reason for single-flight, the per-run budget, and
the launcher-side manifest-scoped reap (`5ff6b295`) being a **precondition**
of R2 rather than a nice-to-have.

---

## 9. Open questions for the operator

1. **Is `launcher_qa` the only server that will ever be admitted to
   mission-chat, or is this a general mechanism?** The design is written as
   general (role → lane → servers) with a QA-first policy. If the answer is
   "only ever launcher_qa", the policy could collapse into a single boolean —
   simpler, but it hardcodes the one thing most likely to change.
   *Recommendation: keep it general; the config is three lines either way.*

2. **Does `tools/registry.py` support scoped removal of one server's tools +
   toolset alias without disturbing others?** — **ANSWERED (R1 audit,
   2026-07-26): yes.** `registry.deregister(name)` (`registry.py:450`) removes a
   single tool, **exempts `mcp-*` toolsets from the plugin-ownership gate**
   (`:469`, "MCP toolsets are exempt: dynamic tool discovery legitimately
   nukes-and-repaves its own tools"), and when the removed tool was the last of
   its toolset it drops the toolset check **and every alias pointing at that
   toolset** (`:505-514`). So the design's preferred shape is available: keep the
   connection warm in `_servers`, tear down only the registry scope. **R2 owns
   that teardown.**

   **CLOSED (R2, 2026-07-26).** `teardown_mcp_admission` removes the scope at
   the end of every admitted run and the transport stays warm, exactly as
   proposed. R1's two recorded follow-ups: (a) the `profile_default` →
   `read_only` sequence now subtracts at registration time — closed, with the
   sequence itself as a test; (b) a lane resolving toolsets **outside**
   `ProfileAgentRunner` would still bypass the scope — none does today (every
   `AgentRunRequest` is built in `agent_runtime/`), so this stays a standing
   invariant rather than a bug.

   **What R2 discovered, and it is the load-bearing part:** teardown alone would
   have broken admission outright. `register_mcp_servers` returns
   `_existing_tool_names()` for any server already in `_servers`, so a warm
   server whose registry scope has been removed can never get it back through
   that entry point — the second admitted turn would have registered *nothing*.
   The warm/cold split in `_default_registrar` (re-register off the live session
   via the upstream `_register_server_tools`, spawn only when cold) is what makes
   "keep the transport, drop the scope" actually work, and it is also what makes
   the per-run tool filter real rather than advisory. It is the single upstream
   private this design depends on; it fails **closed** and is pinned by a drift
   test (`test_the_upstream_warm_registration_seam_exists`).

3. **Single-flight, or per-worker registries under serve's pool of 4?**
   Single-flight is proposed as the safe default. A per-thread registry is
   materially better for throughput but is a large change to a **global
   upstream module** — a fork-boundary risk
   (`project_hermes_fork_boundary`). *Recommendation: single-flight; revisit
   only if QA throughput actually bites.*

4. **Should admission auto-launch the QA window, or require it running?**
   Auto-launch makes the agent self-sufficient; it is also precisely the
   capability behind the 2026-07-25 reap incident.
   *Recommendation: admission does NOT auto-launch. The persistent QA window
   (`48cfed89`) is a precondition, and its absence is a typed denial with a
   fix hint.* This keeps the highest-risk capability human-initiated while
   still giving the agent the 25-tool surface once a window exists.

5. **Does the CLI chat lane (`hermes -p launcher-qa chat`) get retired once
   R2 soaks, or kept as an escape hatch?** It carries a stale model config
   today and its results never reach the mission's proof record. Two lanes
   for one capability is the parallel-authority smell.
   *Recommendation: deprecate it for QA once R2 has soaked — but fix its
   model config regardless, since it is the fallback until then.*

6. **Where does the compiled tool-include list live?** Hermes root config, or
   keep reading the launcher repo's
   `launcher_qa_profile_allowlists.yaml`? A cross-repo file read at admission
   time is fragile (path resolution, deploy skew, a missing checkout).
   *Recommendation: hermes owns the policy; the launcher YAML becomes
   documentation plus a CI parity fixture, so the two can never silently
   diverge.* — **ANSWERED / IMPLEMENTED (R2, 2026-07-26): hermes owns it.**
   `READ_ONLY_INCLUDED_TOOLS` / `READ_ONLY_EXCLUDED_TOOLS` live in
   `mcp_admission.py`; the YAML is vendored as a hash-pinned fixture at
   `tests/agent_runtime/fixtures/launcher_qa_profile_allowlists.yaml` and read
   only by the parity test, never at admission time. Refresh procedure:
   `tests/agent_runtime/fixtures/README.md`.

7. **Should `stagec_mcp_visual_provider.py`'s hand-rolled MCP client be
   retired onto `tools/mcp_tool.py` once admission exists?** It would remove
   the second MCP client (W4), but it is currently the only lane that works
   on the goal path and it has bespoke launcher-preflight logic
   (marionette-target detection, auto-rebuild) that the generic client has no
   place for. *Recommendation: keep both for now, record the duplication as
   debt, and revisit after R3 — do not bundle it into this workstream.*

---

## Log

- **2026-07-26 (R2)** — teardown, warm-aware registration, the compiled positive
  `tools.include` with a parity fixture, and the §D3 agent line. Flag still OFF;
  **the live 6-row acceptance-matrix proof from the harness lane is still owed**
  and is what R2 is not finished without.

  **Teardown shape chosen: warm transport, run-scoped registry.**
  `teardown_mcp_admission` deregisters the admitted `mcp-<server>` tools and
  nothing else; `tools/mcp_tool._servers` keeps the connection. Cost of the
  alternative (full transport teardown + respawn each turn) is the measured
  ~489 ms admission-with-spawn observed live on this host; the warm path pays a
  registry re-register only (no process spawn, no `initialize`, no `list_tools`).
  Since a QA turn is minutes and the serve process is long-lived, the warm path
  is not chosen for speed alone — it is chosen because it is the only shape that
  keeps a launcher window attached across turns.

  **The consequence that made this non-trivial:** teardown breaks
  `register_mcp_servers`. It short-circuits on connected servers
  (`if not new_servers: return _existing_tool_names()`), so a warm server with a
  torn-down scope would never re-register. `_default_registrar` therefore splits
  warm from cold and re-registers warm servers via the upstream
  `_register_server_tools`. That is the one upstream private this design uses; it
  fails **closed** (an unavailable seam registers nothing ⇒ typed
  `mcp_not_registered_on_lane`) and a drift test pins it.

  **Divergence adopted deliberately (narrowing):** R1's `read_only` exclude list
  was three names short of the launcher's `reviewer` row —
  `mcp_launcher_qa_capture_screenshot`, `..._screenshot_window`,
  `..._wait_for_state`. All three drive a LIVE launcher window (restore +
  foreground + PrintWindow; a polling loop that counts against the fixture
  mutex's queue depth), which is why the launcher denies them to `reviewer`. R2
  adopts the row verbatim, so `read_only` on this lane is strictly narrower than
  it was in R1. `profile_default` is unchanged and compiles **no** include filter,
  because the launcher's full-capability rows are the glob `mcp_launcher_qa_*` —
  compiling that as a 25-name list would silently deny the next tool the launcher
  ships. No other divergence: include == the row's resolved allow-set, exclude ==
  the row's `denied`, and the two partition the launcher's tool surface exactly.

  **The parity fixture caught real drift before R2 even landed, which is the
  strongest argument for its shape.** The first snapshot taken during this work
  (launcher `a856f2b0`, 25 tools) was stale within the hour: the launcher shipped
  `mcp_launcher_qa_run_actions` (`3e3feff0`) — a capability **multiplexer** that
  executes an ordered list of other verbs in ONE call — and denied it to every
  restricted profile, precisely because a name-matching allowlist cannot see
  inside a batch. Hermes adopted the denial; the snapshot is now launcher
  `3e3feff0`, 26 tools. Note which direction the two shapes fail in, because it
  settles the R1-vs-R2 question on its own: under a **positive include** the new
  tool was denied by construction and the pin only had to make us *record* it;
  under R1's **exclude list** it would have been silently admitted to `read_only`
  the day it shipped, and nobody would have noticed until an agent batched a
  `click_button`.

  **§D3 line format** (one line, on the runtime-context envelope's volatile tail,
  beside the wall-budget line; empty on a clean admission):

  ```
  - MCP tools: launcher_qa (mcp_admission_timeout) — declared for this persona but
    NOT available on this turn, so no mcp__<server>__* tools for it are in your
    tool list. This is a capability fact, not a permission problem: do not retry,
    do not hunt for a permission mode, and do not substitute a shell/PowerShell
    workaround or a second lane. Use the server's harness-side contract instead
    (for launcher_qa: the qa.request_screenshot decision contract), and say plainly
    in your reply that the tools were unavailable.
  ```

  Two delivery lanes, mirroring `turn_budget` exactly. **Resolution-time**
  denials (`mcp_not_admitted_for_role`, `mcp_server_not_configured`,
  `mcp_admission_disabled`, the `machine_roots` codes) ride the volatile envelope
  tail, rendered by `persona_runtime.mission_chat_admission_line` at the
  mission-chat command — the same place `render_turn_budget_line` is rendered.
  **Execution-time** degradations (`mcp_admission_timeout`,
  `mcp_admission_lane_busy`, admitted-but-did-not-register) are only known after
  that envelope is sealed, so they ride `agent.steer` from the runner — the same
  in-band lane the wall-budget checkpoint nudge uses. Known limit, stated rather
  than hidden: a steer only lands on the next tool result, so an agent that calls
  no tool at all never sees it; an agent that starts reaching for tools does.
  Known limit 2: with the flag OFF the line is not rendered at all (the flag-off
  path stays byte-identical and pays no profile read), so a declared-but-dropped
  server is reported to the OPERATOR via R0 `requirement_failures` but not to the
  agent. Turning the flag on is what makes the agent-facing half live.

- **2026-07-26 (R1)** — admission implemented and landed with the flag OFF.
  `agent_runtime/mcp_admission.py` + the `profile_runner` seam + the
  `agent_runtime.mcp_admission` root-config block + `--explain-mcp`.
  Enable on the live runtime with, in the ROOT `config.yaml` (the one under the
  Hermes root the harness/serve process runs with — `X:\Eternia\.hermes\config.yaml`
  on the Launcher's runtime host; `%LOCALAPPDATA%\hermes\config.yaml` when
  `HERMES_HOME` is unset — **not** a profile's `config.yaml`, which
  `load_root_runtime_config` deliberately refuses to read for this policy):

  ```yaml
  agent_runtime:
    mcp_admission:
      enabled: true
      connect_timeout_seconds: 20
      roles:
        qa:
          mission_chat: [launcher_qa]
  ```

  Inspect before flipping — this resolves policy only and never connects:
  `hermes harness persona tool-diff qa --explain-mcp --json`.
  Open question 2 answered in place (scoped removal EXISTS; R2 owns teardown,
  and the R1 no-teardown consequence is written up there rather than left
  implicit). Tests: `tests/agent_runtime/test_mcp_admission.py`.
- **2026-07-26** — design written against `main @ f58d1be81`. Not
  implemented at the time of writing. Key finding during the audit: the role→server admission policy
  **already exists** (`profile_readiness.py:379-384`), the persona
  declaration **already exists** (`models.py:397`), and QA nodes **already
  request** the `launcher_qa` toolset (`node_tools.py:360-373`) — the missing
  piece is registration on the run path, not a new policy language. The
  design is therefore "make the existing declaration load-bearing", not
  "invent an allowlist".
