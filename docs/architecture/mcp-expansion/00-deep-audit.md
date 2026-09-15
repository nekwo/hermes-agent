# MCP Expansion — Deep Audit (pass 1)

> Companion to [`../mcp-expansion-roadmap.md`](../mcp-expansion-roadmap.md). This document captures the audit findings that ground the per-stage docs in this folder. Stage docs reference back here so they don't repeat surface-area inventory.

Audit date: 2026-05-15 · Audit scope: `X:\Eternia\hermes-agent`, `X:\Eternia\.hermes` runtime, `x:\Unreal Engine\Engine\Launcher\EterniaLauncher\tool\stagec_qa_*`, `x:\Unreal Engine\Engine\EterniaBackend\eternia-backend\scripts`, `x:\Unreal Engine\Engine\ArcadiaLabs_Brain`.

---

## A. What already exists in Hermes

### A.1 MCP server entry points

| Path | Surface | Notes |
|---|---|---|
| [`mcp_serve.py`](../../../mcp_serve.py) | `hermes mcp serve` — messaging bridge | Exposes 10 tools (`conversations_list`, `conversation_get`, `messages_read`, `attachments_fetch`, `events_poll`, `events_wait`, `messages_send`, `channels_list`, `permissions_list_open`, `permissions_respond`). Stdio FastMCP. This is the closest analog to "Hermes Control MCP" today, but it is messaging-only — no kanban / cron / profiles / sessions / skills tools. |
| [`agent/transports/hermes_tools_mcp_server.py`](../../../agent/transports/hermes_tools_mcp_server.py) | hermes-tools-as-MCP (for embedded Codex runtime) | Exposes 22 tools (web/browser/vision/image/skills/TTS + 9 kanban verbs). Stateless dispatch via `model_tools.handle_function_call`. Already includes `kanban_create / show / list / complete / block / unblock / comment / heartbeat / link` — Stage 2's read-only kanban surface is largely already wired here, just not as a standalone discoverable server. |
| [`hermes_cli/mcp_config.py`](../../../hermes_cli/mcp_config.py) | `hermes mcp add/remove/list/test/configure` | Manages `mcp_servers` blocks in profile `config.yaml`. The Stage 0 question "does Hermes already cover discovery/config?" — yes, this is the wiring path. |
| [`tools/mcp_tool.py`](../../../tools/mcp_tool.py) | MCP-client side discovery/dispatch | What workers use when they call out to a configured MCP server. |
| [`tools/mcp_oauth.py`](../../../tools/mcp_oauth.py), [`tools/mcp_oauth_manager.py`](../../../tools/mcp_oauth_manager.py) | MCP OAuth helpers | Relevant if any new server gates calls on Tony's auth (likely Stage 3 brain MCP). |

### A.2 CLI surfaces that can be lifted into typed MCP tools

These are the "candidate tool families" from the roadmap §Layer 1, mapped to existing implementations. Stage 2 should wrap these, not reimplement them.

| Roadmap family | Backing CLI module | Notes |
|---|---|---|
| Kanban | [`hermes_cli/kanban.py`](../../../hermes_cli/kanban.py) (35+ verbs) + [`hermes_cli/kanban_db.py`](../../../hermes_cli/kanban_db.py) | Surface is bigger than the roadmap suggests: `boards`, `create`, `list/ls`, `show`, `assign`, `reclaim`, `reassign`, `diag`, `link/unlink`, `claim`, `comment`, `complete`, `edit`, `block/unblock`, `archive`, `tail`, `dispatcher`, `daemon`, `watch`, `stats`, `subscriptions`, `log`, `runs`, `heartbeat`, `ctx`, `specify`, `gc`. The dispatch tool ([`hermes_cli/kanban_diagnostics.py`](../../../hermes_cli/kanban_diagnostics.py)) and dispatcher daemon already exist. |
| Profiles + workers | [`hermes_cli/profiles.py`](../../../hermes_cli/profiles.py), `hermes_cli/profile_distribution.py` | Profile dirs live at `~/.hermes/profiles/<name>/`. Per-profile worker spawn currently routes through the kanban dispatcher + `~/.hermes/profiles/<name>/processes.json`. |
| Cron | [`hermes_cli/cron.py`](../../../hermes_cli/cron.py), [`cron/jobs.py`](../../../cron/jobs.py), [`cron/scheduler.py`](../../../cron/scheduler.py), [`tools/cronjob_tools.py`](../../../tools/cronjob_tools.py) | Jobs persisted per-profile at `~/.hermes/profiles/<name>/cron/jobs.json` — confirmed live on Alice profile (7 jobs, kanban watchers + image-gen). `cronjob_tools.cronjob(**kwargs)` is the JSON-returning API Stage 2 should wrap. |
| Sessions + memory | `hermes_cli/sessions*`, `agent/memory_manager.py`, `agent/memory_provider.py` | Session DB at `~/.hermes/profiles/<name>/state.db` + `sessions/`. |
| Skills | [`hermes_cli/skills_hub.py`](../../../hermes_cli/skills_hub.py), `hermes_cli/skills_config.py`, `agent/skill_commands.py`, `agent/skill_utils.py` | Skills mounted per-profile under `~/.hermes/profiles/<name>/skills/`. |
| Tools + toolsets | [`hermes_cli/tools_config.py`](../../../hermes_cli/tools_config.py), `toolset_distributions.py`, `toolsets.py` | Toolset enable/disable already gated per-platform in profile config. |
| Health | [`hermes_cli/doctor.py`](../../../hermes_cli/doctor.py), [`hermes_cli/status.py`](../../../hermes_cli/status.py), [`hermes_cli/logs.py`](../../../hermes_cli/logs.py) | `hermes doctor` covers most of what `hermes_doctor` MCP needs. |

### A.3 Profile runtime layout (observed)

```
~/.hermes/profiles/
  alice/                    # broad orchestration, max_turns=90, kawaii personality
    config.yaml             # has mcp_servers: dart + marionette
    cron/jobs.json          # 7 jobs incl. eternia-launcher kanban watchers
    state.db                # sessions / kanban-per-profile state
    plans/                  # per-profile plan files
    skills/                 # mounted skill copies
    processes.json          # PID/lock for spawn supervision
  pm/                       # PM workflow (kanban + brain + release)
  reviewer/                 # reviewer doctrine
  claude_launcher           # Claude worker, launcher repo scope
  claude_launcher_qa        # Claude worker, QA scope
  claude_backend            # Claude worker, backend scope
  gpt-launcher / gpt_backend      # Codex/GPT workers, same split
  spark_launcher / spark_backend / spark_docs / spark_logreader / spark_testwriter
  brain-writer              # write access to ArcadiaLabs_Brain / TonyBrain
  launcher-qa, launcher-qa-direct   # active Stage C launcher QA profiles
  alice-img                 # image-only personality bound to image_gen toolset
```

Eight worker-class profiles cover the roadmap's "Claude/Codex dev workers" and "Spark/logreader" personas. Stage 2's permission model must distinguish at least these five classes (Alice / PM / QA / dev-worker / log-worker) per §Permission model in the parent roadmap.

### A.4 Standalone existing MCP server for a product

[`x:\Unreal Engine\Engine\Launcher\EterniaLauncher\tool\stagec_qa_mcp_server`](../../../../../Unreal%20Engine/Engine/Launcher/EterniaLauncher/tool/stagec_qa_mcp_server) is a **complete reference implementation** for what the roadmap calls `eternia_launcher_mcp`. It already:

- Implements 10 typed tools with JSON-Schema-2020-12, `additionalProperties: false`, and `serialised: true` flags for fixture/media operations:
  - `mcp_launcher_qa_get_runtime_state`, `..._get_auth_state`, `..._begin_pkce_login`, `..._get_navigation_state`, `..._set_tab`, `..._dismiss_hashtag_onboarding`, `..._scroll_to_fixture`, `..._get_feed_fixture_state`, `..._get_media_playback_state`, `..._capture_screenshot`.
- Maps every tool to a Stage 34 `ext.eternia.qa.<verb>` bus call (except `capture_screenshot` which is Win32 PrintWindow).
- Ships a single source of truth ([`lib/tools.dart`](../../../../../Unreal%20Engine/Engine/Launcher/EterniaLauncher/tool/stagec_qa_mcp_server/lib/tools.dart)) consumed by both `tools/list` and dispatch.
- Pairs with [`tool/stagec_qa_marionette`](../../../../../Unreal%20Engine/Engine/Launcher/EterniaLauncher/tool/stagec_qa_marionette) (Dart VM-service client) for runtime control.

This means **Stage 1 is largely about hardening + closure, not greenfield build**. See [`02-stage-1-launcher-mcp.md`](02-stage-1-launcher-mcp.md).

### A.5 PowerShell harness around the launcher MCP

`docs/stages/qa-reboot/scripts/` in the launcher repo holds the operator-facing wrapper:

- `Invoke-StageCBrowserLogin.ps1` (headless PKCE),
- `Invoke-StageCMarionetteCommand.ps1` (VM-service bridge),
- `Invoke-StageCProcessHygiene.ps1` (process reap),
- `Test-StageCHermesMcpFullParity.ps1` (the full-parity gate referenced in commit `baca5406`),
- `Test-StageCMcpParityDiff.ps1` (direct-vs-MCP envelope diff),
- `Capture-StageCWindowScreenshot.ps1`, `Invoke-RedactionScan.ps1`, `New-StageCPhaseTimer.ps1`, `Start-StageCDirectExe.ps1`, …

These are the "direct runner" half of the "**debug direct, certify MCP**" doctrine recorded in [Agent QA & Release Doctrine](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md). They are not replaced by the MCP server — they are the human/CI fallback transport that the MCP must stay parity-tested against.

### A.6 Backend gate surface

[`x:\Unreal Engine\Engine\EterniaBackend\eternia-backend\scripts\test.sh`](../../../../../Unreal%20Engine/Engine/EterniaBackend/eternia-backend/scripts/test.sh) already implements the doctrine in [Agent QA & Release Doctrine](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md):

- `scripts/test.sh` (no flag) — Postgres + Redis + Centrifugo via `docker-compose.test.yml`, runs `manage.py check`, `makemigrations --check --dry-run`, `migrate`, then `test`. This is the **full gate**.
- `--sqlite` — escape hatch, skips every `@requires_*` test, **explicit Tony-only**.
- `--keycloak` — adds Keycloak realm to the stack.
- `--infra-only` / `--infra-down` / `--teardown` — lifecycle.

Stage 5 backend MCP should wrap these flags, not reimplement them.

### A.7 Brain network on disk

```
x:\Unreal Engine\Engine\
  ArcadiaLabs_Brain\           # parent / company brain
    Agent QA & Release Doctrine.md   # canonical PASS/NEEDS_FIX/FAIL rubric (audited)
    Brain Index.md
    Decisions.md
    Environment & Staging Policy.md
    Management Ideal — Tony.md
    Operating Doctrine.md
    Parent Brain.local.md          # gitignored personal links
    Parent Brain.local.example.md  # committed template
  Launcher\EterniaLauncher\Launcher_Brain\   # child brain (referenced from CLAUDE.md)
  EterniaBackend\eternia-backend\EterniaBackend_Brain\   # sibling child brain
```

The roadmap §Layer 2 / `arcadia_brain_mcp` rule "TonyBrain remains personal, ArcadiaLabs_Brain is the shared operative layer, personal parent paths stay private/gitignored" is **already encoded on disk** via the `.local.md` + `.local.example.md` pattern. Stage 3 must preserve that boundary, not invent a new one.

---

## B. Gaps the roadmap closes

What the audit confirms **does not yet exist** and what each stage uniquely contributes:

1. **No discoverable Hermes Control MCP.** The kanban verbs exist as in-process tools inside `hermes-tools` (codex-runtime MCP only) and the CLI, but no standalone `hermes-control` server published as a peer of the messaging MCP. Stage 2 fills this.
2. **No typed brain MCP.** Today, brain edits are raw markdown writes from inside profiles. Stage 3 adds vault-allowlisted, append-only typed tools.
3. **No standardized agentops MCP.** Worker spawn happens via kanban dispatcher and ad-hoc PowerShell. Stage 4 collapses spawn / log-tail / stale-reap into one server with PID tracking.
4. **No release MCP.** Release classification (`PASS` / `NEEDS_FIX` / `FAIL_NON_BLOCKING_TOOLING_PARITY` / `NOT_RUN_MISSING_CONTEXT`) is doctrine in markdown, not a typed tool. Stage 5 makes it programmatic.
5. **Stage 1 is "harden + close" not "build".** The Launcher MCP server already passed full-parity locally on commit `0654d2c` (artifact `qa-artifacts/stagec_hermes_mcp_full_parity_20260515_061058`, 5 labels, 0 parity mismatches — per [Agent QA & Release Doctrine](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md)). The remaining work is portability to next-week's Windows build, plus discovery/closure manifests.

---

## C. Cross-cutting findings that affect every stage

1. **Profile is the permission boundary.** Hermes already gates `mcp_servers` per profile config. Stage 2's permission model maps cleanly onto adding/removing `mcp_servers.<name>` entries in profile YAML, not inventing new ACLs.
2. **`mcp_servers` allows arbitrary stdio commands.** This is powerful but means **a single misconfigured profile can grant a worker every write tool**. Stage 4 (agentops) must spawn workers under profiles that **explicitly exclude** mutating control-plane MCPs.
3. **Redaction is already wired** at multiple points: `agent/redact.py`, [`docs/stages/qa-reboot/scripts/Invoke-RedactionScan.ps1`](../../../../../Unreal%20Engine/Engine/Launcher/EterniaLauncher/docs/stages/qa-reboot/scripts/Invoke-RedactionScan.ps1), `redact_secrets: true` on every profile. Every new MCP tool must funnel through one of these — there is no acceptable "I'll add redaction later" path.
4. **Audit log target is already chosen** by precedent: kanban events table + per-profile `state.db`. New mutating tools should write event rows there, not invent a new audit store.
5. **Stage 0 discovery cost is low.** The current Hermes tree already exposes `hermes mcp list / add / remove / test` and the codex-runtime hermes-tools MCP. The "what's left private vs upstreamable" decision is informed, not blocked.

---

## D. Cron job context (relevant to Stage 4 agentops)

Live jobs observed on `alice` profile at audit time (`X:\Eternia\.hermes\profiles\alice\cron\jobs.json`):

- `869cd2d3e5c3` — eternia-launcher kanban completion ASCII watcher, 1m, 2490 runs
- `fc03632d612e` — eternia-launcher kanban 15m status heartbeat, 88/96 runs
- `a5803c276895` — alice catgirl image set, 15m, 40 runs (image-gen toolset)
- plus 4 paused jobs (backend audit watcher, stage34 watchdog, all-tasks-finished notifier, direct image cron)

Stage 4 doc must explain how `arcadia_pm_route_qa_gate` and friends compose with these cron watchers without doubling notifications.

---

## E. What this audit does NOT cover (handled in pass 2)

- Per-profile `state.db` schema dump → audit-log column reuse.
- Exact `model_tools.handle_function_call` dispatch contract (Stage 2 dispatch must follow it).
- Codex-app-server runtime — whether the codex MCP preset reduces Stage 2's standalone server need.
- Spark / spark_logreader profile details (Stage 4 worker classes).
- The hosted Stage C credential contract (`qa-stagec-smoke`, `eternia-staging/stagec-smoke-credentials`) — relevant to Stage 1 closure + Stage 5.

These are revisited in [`08-second-pass-audit-and-expansion.md`](08-second-pass-audit-and-expansion.md).
