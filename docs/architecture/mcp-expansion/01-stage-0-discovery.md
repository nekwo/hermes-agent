# Stage 0 — Discovery

> Parent: [`../mcp-expansion-roadmap.md`](../mcp-expansion-roadmap.md) §Recommended staged build → Stage 0.
> Audit context: [`00-deep-audit.md`](00-deep-audit.md) §A.

## Goal

Before any custom MCP server is written, lock down what Hermes already exposes so Stages 1–5 don't reinvent surfaces or — worse — create a parallel one. The roadmap calls this out explicitly because two of the deliverables (kanban control, brain-bridge messaging) overlap with shipped code that is not yet discoverable as a peer MCP server.

## Audit deliverables (output: a markdown handoff)

Produce a single `STAGE0_HERMES_MCP_SURFACE_AUDIT_<DATE>.md` in `docs/architecture/mcp-expansion/handoffs/` that answers each question below with a code reference, a one-line summary, and a "covered / partial / gap" verdict.

### Q1 — Does Hermes already expose control-plane tools?

For each roadmap family in [`00-deep-audit.md`](00-deep-audit.md#a2-cli-surfaces-that-can-be-lifted-into-typed-mcp-tools), confirm:

1. **CLI exists** (yes, all 6 families).
2. **In-process tool exists** (e.g. `tools/cronjob_tools.py:cronjob`) — required for stateless MCP dispatch.
3. **Already exposed via an MCP server** (no — only kanban subset via `hermes-tools-as-MCP` for codex-runtime only).

Output a 6-row table: Kanban | Profiles+workers | Cron | Sessions+memory | Skills | Tools+health.

### Q2 — Can `hermes mcp serve` or `agent/transports/hermes_tools_mcp_server.py` cover part of the design?

- `hermes mcp serve` ([`mcp_serve.py`](../../../mcp_serve.py)) → messaging-only; **not** a fit for control-plane reuse.
- `hermes_tools_mcp_server.py` → kanban verbs + web/browser/vision/image/skills/TTS. Two open questions:
  1. Can we factor `EXPOSED_TOOLS` into a new "control-plane preset" list that swaps web/browser for kanban+cron+profiles+sessions+skills+health?
  2. Or should Stage 2 be a fresh server with a smaller blast radius? (Recommended — see Stage 2 doc §Decision-1.)

### Q3 — Does the Codex MCP preset or Codex app-server runtime reduce custom implementation?

Audit [`agent/transports/codex_app_server.py`](../../../agent/transports/codex_app_server.py), `codex_app_server_session.py`, `codex_event_projector.py`, and the `mcp_servers.codex` preset in [`hermes_cli/mcp_config.py`](../../../hermes_cli/mcp_config.py#L35).

Specifically:

- Does `codex mcp-server` already publish kanban control tools? (Suspected no — Codex publishes its own native tools.)
- Does the Codex app-server runtime allow a Hermes-side server to inject control tools into the Codex tool list? (Already does for the hermes-tools tools — same pathway is reusable.)

Output: a "yes/no/partial" verdict and a code reference for each tool family.

### Q4 — What must remain private to Arcadia/Eternia?

Apply the boundary already encoded by the `.local.md` / `.local.example.md` pattern in [`x:\Unreal Engine\Engine\ArcadiaLabs_Brain\Parent Brain.local.example.md`](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Parent%20Brain.local.example.md).

Classify each candidate tool family:

| Layer | Classification | Why |
|---|---|---|
| Hermes Control MCP — kanban/cron/profiles/sessions/skills/tools/health | **upstream-worthy** | Generic Hermes control plane. |
| `arcadia_brain_mcp` | **private** | Encodes ArcadiaLabs_Brain vault layout + handoff conventions. |
| `arcadia_agentops_mcp` | **private** | Encodes profile names (`spark_*`, `claude_launcher_qa`, etc.) and worker doctrine. |
| `arcadia_pm_mcp` | **private** | Encodes Tony's escalation rules (the `NEEDS_FIX` taxonomy in [`Agent QA & Release Doctrine.md`](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md)). |
| `arcadia_release_mcp` | **private** | Same — wraps Stage C / backend gates and the company release classification. |
| `eternia_launcher_mcp` | **product-private** | Already shipped per-product at [`tool/stagec_qa_mcp_server`](../../../../../Unreal%20Engine/Engine/Launcher/EterniaLauncher/tool/stagec_qa_mcp_server). |
| `eternia_backend_mcp` | **product-private** | Wraps `scripts/test.sh` + k8s staging deploy. |

## Acceptance

Stage 0 is done when:

1. The audit doc above is committed under `docs/architecture/mcp-expansion/handoffs/`.
2. Each of Q1–Q4 has a yes/no/partial verdict with a code reference.
3. The verdicts feed concrete `KEEP` / `WRAP` / `BUILD` flags into each of Stage 1–5 (e.g. Stage 2: WRAP existing `cronjob_tools.cronjob`, BUILD profile-spawn MCP wrapper).
4. The audit is reviewed against [`Agent QA & Release Doctrine.md`](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md) — every "covered" verdict must cite a real path; no schema-only assertions.

## Risks

- **Re-implementing kanban in Stage 2.** Mitigation: the audit must reference `hermes_cli/kanban.py` + `kanban_db.py` and require Stage 2 to dispatch through `kanban_db`, not raw SQL.
- **Discovering codex-app-server already preempts Stage 2.** Mitigation: the audit's Q3 verdict gates Stage 2 start. If Codex already exposes everything, Stage 2 shrinks to a config preset.
- **Re-encoding the brain `.local.md` convention.** Mitigation: Stage 3 doc references the existing template files instead of restating the rule.

## Out of scope

- No code is written in Stage 0. Purely an audit + verdict matrix.
- No new MCP server is registered. (`hermes mcp add` calls are forbidden until Stage 1 is closed.)
