---
name: harness-runtime-model
description: Hermes Agent Runtime mental model AND the operating manual for Mission Control — personas / instances / chats / graph / board / office, the first-class commands to view and operate them, and (in references/) the live operating loop: place-message-delete a QA instance, read MCP admission receipts, triage stalled turns, capture Stage C proof, preserve evidence. Use instead of low-level DB/Python/scripts.
metadata:
  hermes:
    surfaces: [mission_chat]
    modes: [standard]
    load_policy: required_preload
---

# Harness Runtime Model

**Chat is the only lane.** There is no goal, task, mission plan, stage graph, daemon,
worker, run, proof gate, lane, or swarm certification. Those were removed on
2026-07-30; if you remember them, that memory is stale. A request arrives as a chat
turn on an existing persona instance, you do the work with your own tools in that
turn, and you answer in that same chat. Nothing dispatches you, and nothing gates
your reply.

**Model:** **persona** = a profile-backed agent definition (data, in the Hermes
profile — not hardcoded). **persona instance** = a durable placement of that persona
(`personainst_<role>_agent_<hash>`). **chat session** = the durable root a turn runs
on; an instance points at one operator-chat root and can hold several. Roles are
data too: an unknown role is carried, not rejected, and never filters your tools.

**The runtime agent graph is a picture, not a program.** `flow_graph.py` stores one
operator-authored document per owner (`graph_id: runtime:<owner>`) and reconciles
`persona_instances[].steered_by` from it. The Agent Console renders that as the
steering tree. It records *who steers whom*; **it enforces nothing, routes nothing,
and picks no next agent.** Never answer "what runs next?" from it — nothing runs
next. It only ever references instances that already exist; it never creates them
and never binds work.

**Two messaging paths, and only two:** `mission-chat message` (the canonical
operator/CLI path) and the in-model `agent_chat_send` tool (agent → agent). If you
want another agent to do something, message it; there is no assignment surface.

**The Mission Board is planning state only.** Cards record follow-up work for humans
to read. A card never starts, routes, or changes anything.

`hermes` == `python -m hermes_cli.main`. There is no `hermes harness runtime`
command. Always `--json`. Never use raw DB / Python / ad-hoc scripts to inspect.

## Operating loop — load the reference that matches the task

This package absorbed `mission-control-harness` (2026-08-28); it is now the whole
operator manual. This file is the preloaded model. The operating detail is in
`references/`, loaded on demand — read the one that matches BEFORE acting:

- `references/operations.md` — **the operating loop.** Roots and the `base` vs `alice`
  home caveat, the start checklist, the extra inspect/operate rows this file does not
  carry, the fresh-instance-per-QA-task recipe (create · office write · message ·
  delete), the delete lane, MCP admission receipts, stalled-turn triage, evidence
  preservation, and the final report shape. Read it before operating anything live.
- `references/persona-chat.md` — operator-channel behavior, the canonical new-chat
  contract, identity/latency/turn-identity rules, agent-to-agent `agent_chat_send`
  and the relay policy. Read before persona-chat / operator-channel / bridge work.
- `references/proof.md` — Backend and Launcher proof commands, the Stage C MCP
  visual-proof recipe, and its known hazards. Read before attaching or judging proof.
- `references/debugging.md` — the snapshot parity envelope, UI⟷harness divergence
  classes, the MC/UI checklist. Read when the UI disagrees with harness truth.

## Non-negotiables

- Do not trust an agent summary as proof, and never claim something works from code
  inspection. Run it, and cite the receipt (tool-trace row, artifact path,
  `client_message_id`/`turn_id`, `mcp_calls_spent`).
- Check `.parity.runtime_root` and `.parity.profile` in `harness snapshot --json`
  before believing anything. Roster/office/board/graph answers are home-independent;
  **admission, model, and profile answers are not** — the launcher's serve child runs
  under `profiles\base` or the persona's bound profile, not your CLI's `alice`. Say
  which home produced any receipt you report (`references/operations.md`, "Roots").
- Use first-class harness CLI verbs. Never inspect or mutate runtime state with raw
  DB access, ad-hoc Python, or hand-editing store files.
- **Archive-never-delete**, including the verb named `delete` (a full alias of
  `retire`, which archives and preserves chat history). There is no hard-delete verb
  to reach for.
- Visual claims need Stage C proof captured through the MCP path in
  `launcher-mcp-operations` — the only sanctioned one. If it is blocked, record the
  exact blocker and still attach command/code proof. Never kill Tony's live Launcher.
- Image lines pass through UNTOUCHED — reproduce every `MEDIA:<absolute image path>`
  line, and every bare absolute screenshot path, VERBATIM on a line of its own, in
  your own reports and in every relay, quote, and summary. Canonical rule:
  `launcher-mcp-operations` SKILL.md, "Screenshot capture and delivery".
- Never write raw secrets into SessionDB (recall-reachable). Do not overwrite
  unrelated local changes.
- If agents loop, stall, or take the wrong proof path, that is a Harness gap: name
  the command, the typed error, and the missing receipt. "It hung" is not a report.

## View

| See | Command |
|---|---|
| runtime health / diagnostics | `hermes harness status --json` · `hermes harness doctor --json` |
| configured agent definitions | `hermes harness agent list --json` |
| durable persona instances (the roster) | `hermes harness persona list --json` |
| one instance in detail | `hermes harness persona show <persona_instance_id> --json` |
| an instance's resolved tools and blocks | `hermes harness persona tool-diff --json` |
| a chat session's transcript | `hermes harness persona chat history --session-id <root> --json` |
| stored agent-graph documents | `hermes harness flow list --json` · `hermes harness flow show <graph_id> --json` |
| planning boards and cards | `hermes harness board list --json` · `hermes harness board show <board_id> --json` |
| realms / workspaces | `hermes harness realm list --json` · `hermes harness workspace list --json` |
| aggregate read-model (what the Launcher renders) | `hermes harness snapshot --json` |
| shared skills substrate | `hermes harness skills inventory --json` · `skills catalog --json` |
| level agents shown in Mission Control | Stage C MCP `mcp_launcher_qa_get_buttons` with `scope=mission_control.agent` |
| compact Mission Control graph probe | Stage C MCP `mcp_launcher_qa_get_widget_state` with `widget=mission_control.graph` |

**Do not use for level agents:** `status.agents` and `hermes harness agent list --json`
rosters show configured/installed Harness agents. They do not show which instances are
placed on a Mission Control level. Use `persona list --json` for the live roster and
Stage C MCP `mission_control.agent` for the visible level-agent selection surface.

## Removed — unlearn these

These verbs, fields, and rules were removed on 2026-07-30. Do not reach for them, do not
expect their output shapes, and do not repeat them to an operator as if they were live:

- `hermes harness task show <id> --json`, `task list`, `task create --start-daemon`,
  `task history`, `task unblock/cancel/archive` — there are no goals or tasks.
- `hermes harness blueprint list/run --bind`, and the `.mission_plan` field on anything
  (with its `.stages`, `.edges`, `.agent_topology`) — there is no stage graph. The old
  default graph `neko_two_dev_default` = **Neko scope → Backend Dev → Launcher Dev**, and
  the rule that **QA is a node only if the selected blueprint binds it**, are both gone:
  no blueprint binds anyone, and QA is just another agent you can message.
- `run show` / `proof list` / `worker list` / `lane list` / `swarm status|enable`, `tick`,
  and `run-until-settled` — no runs, no proof gates, no worker sessions, no lanes, and no
  burn-in certification gate.
- The `mission_goal_create` tool and the `--allow-mission-goal` opt-in on `mission-chat
  message` — no chat turn can create a goal, because there are no goals.

`harness snapshot --json` is contract 54 and carries no goal, stage, run, proof, or
incident sections. If you are looking for one, it is gone, not missing.

## Operate

| Do | Command |
|---|---|
| find the on-level chat instances to message | `hermes harness persona list --json` → chat-mode `personainst_<role>_agent_<hash>` rows (cross-check Stage C `mission_control.agent` buttons) |
| continue an existing chat root | `hermes harness persona instance open-chat --persona-instance-id <instance> --persona <id> --session-id <root> --json` (`--session-id` is required unless `--new-session` or `--add-instance`) |
| create a new server-minted chat on an existing instance | `hermes harness persona instance open-chat --persona-instance-id <instance> --persona <id> --new-session --idempotency-key <key> --json` |
| message an exact chat root (canonical path) | `hermes harness mission-chat message --persona <id> --persona-instance-id <instance> --session-id <root> --client-message-id <id> --message … --json` |
| message another agent from inside a turn | the `agent_chat_send` tool |
| steer an in-flight streamed turn | `hermes harness mission-chat steer --session-id <root> --client-message-id <id> --message … --json` |
| abandon an outcome-unknown turn | `hermes harness mission-chat turn-resolve --session-id <root> --client-message-id <id> --turn-id <turn> --action abandon --json` |
| load a skill on the next turn | `hermes harness mission-chat queue-skill --persona <id> --session-id <root> --skill <name> --json` |
| re-route a steering edge in the agent graph | `hermes harness persona instance steer …` (supports multi-parent fan-in) |
| replace a whole agent-graph document | `hermes harness flow set …` (reconciles `steered_by` for the instances it references; never creates instances) |
| track follow-up work | `hermes harness board card add …` — planning state only |
| return a child's bounded summary to a parent chat | `hermes harness persona instance return-summary …` |

## Persona chat continuity

**Message the on-level instance.** Persona instances and their chat roots use
one chat lane. Legacy lifecycle metadata does not create a separate routing
class and does not change whether an exact, owned chat root can receive a turn.

`PersonaInstance.default_chat_session_id` is the operator-chat pointer. Hermes
mints every new root; callers may use a local draft identity only while waiting
for the `open-chat --new-session` result.

The pointer can go stale: `mission-chat message` may reject a roster-listed
root with `unknown_chat_session` ("unknown explicit persona chat root"). Do not
keep retrying it — mint a fresh root with `open-chat --new-session
--idempotency-key <key>` and message that. External operators must run the CLI
with the runtime's `HERMES_HOME` (the profile that owns the harness store);
under the wrong home, `persona list` returns an empty roster and chat roots
resolve nowhere.

Treat `session_id` in chat commands as the stable root. Native compression may
rotate `active_session_id`; it does not change the root selected by Mission
Control. Runtime-state projections are observer-qualified: only the owning
long-lived serve process may report `hot`, `busy`, `cold`, or `failed` from its
resident registry; external CLI snapshots report `unknown`.

If a turn returns `chat_turn_outcome_unknown`, do not retry it. Resolve the
exact `(root, client_message_id, turn_id)` tuple with `turn-resolve ...
--action abandon`, then send the text as a new turn with a fresh client
message ID.
