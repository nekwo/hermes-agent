# Planned — chat-lane context, memory and operator affordances

**Status:** not started. **Owner doc:** [`../05-chat-turn-lane.md`](../05-chat-turn-lane.md).
**Source:** the G-rows of
[`../archive/2026-08-22-pre-consolidation/mission-chat-lane-gap-audit.md`](../archive/2026-08-22-pre-consolidation/mission-chat-lane-gap-audit.md)
that the 2026-08-09 unbounded-default ruling did NOT close, re-checked against HEAD
2026-08-22.

The gap audit's tool-surface rows (G1/G2/G9's tool half, G11/G12/G13) are resolved for
the default posture: `_blocked_tool_names_for_chat` returns `[]` under `unbounded`
(`agent_runtime/persona_runtime.py:583-586`) and `_enabled_toolsets_for_chat` bypasses
the cost policy (`:639-660`). What remains open is everything the ruling did not touch —
context, memory injection, and operator affordances.

---

## R1 — Core context files and profile memory have no operator surface

**Evidence.** Both are per-persona boolean fields defaulting `False`
(`agent_runtime/models.py:266-267`), read once per turn into the run request:
`skip_context_files=not include_core_context_files` (`persona_runtime.py:239`) and
`skip_memory=not include_profile_memory` (`:248`). They are parsed from persona
overrides at `agent_runtime/config.py:544-545` and `:594`, honoured by the profile
binding (`persona_profile_binding.py:341-345`), reported to the operator read-only in
the snapshot (`snapshot.py:2555`) and in prompt observability
(`prompt_observability.py:34-36`, `:527`), and travel through realm sync as allowed
persona-def keys (`persona_config_sync.py:105-106`).

**The gap.** There is no CLI setter and no Mission Control surface. Grep over
`hermes_cli/` finds only reads and one create-time default
(`persona_commands.py:6282`, `:6303-6304`); grep over the launcher's `lib/` finds
nothing at all. An operator who wants a persona to read `AGENTS.md` or its own
`MEMORY.md` must hand-edit a config file.

**Why the defaults are what they are, and why that stays.** `skip_context_files`
defaults on because an isolated persona must not auto-inject the process-cwd repo docs
into every conversational turn — the hermes-agent `AGENTS.md` alone is ~72 KB, truncated
to ~65 K chars ≈ 16 K tokens of fixed per-turn overhead regardless of persona
(`persona_runtime.py:231-239`). `skip_memory` defaults on because profile memory is
identity-adjacent: a persona bound to a supervisor profile for its *capabilities* must
not also inherit that profile's memory-model — the concrete failure was Alice inheriting
the "goal→Neko→Dev" mental model and making Neko relay to itself (`:240-248`).

**Gate.** A surface that sets these per persona, with the cost and the identity
consequence stated at the point of setting. Not a global default flip.

---

## R2 — No shell hooks on the lane

**Evidence.** Grep for `register_shell_hooks` / `shell_hooks` / `PreToolUse` across
`agent_runtime/` returns nothing. The gap audit filed this as G14.

**Gate.** Establish whether the chat lane wants hooks at all before building a
registration path. The terminal envelope (`agent_runtime/terminal_envelope.py`) already
occupies the "decide whether this command may run" seam deterministically, so a hook
lane must not become a second, ambient answer to the same question — that is exactly the
coin-flip the envelope was built to retire.

---

## R3 — No slash commands

**Evidence.** Grep for `slash_command` across `agent_runtime/` and
`hermes_cli/harness_parts/persona_commands.py` returns nothing. Filed as G16.

**Gate.** Unspecified. Low priority relative to R1.

---

## R4 — Attachments: output half shipped, input half absent

**Evidence.** The agent→operator direction works: a `MEDIA:<absolute image path>` line
is a declaration the operator's console renders as a titled image attachment card, and
the lane's system prompt carves it out as content that must be relayed VERBATIM
(`persona_runtime.py:420-426`). The operator→agent direction has no counterpart —
nothing in the turn-context assembly (`agent_runtime/mission_chat_turn_context.py`) or
the send admission path accepts an image. Filed as G17, which predates the output half.

**Gate.** A send path that carries an image needs a durability answer first: the turn
store bounds text (`_MAX_TEXT = 20000`, `mission_chat_turns.py`) and would need a
by-reference scheme rather than inline bytes.

---

## R5 — No filesystem checkpoints, no `--worktree` isolation on the lane

**Evidence.** Worktree machinery exists in the harness but is a janitor and a CLI verb,
not a per-turn isolation mode: `agent_runtime/delivery_directive.py:1-14`
(`reap_orphan_worktrees`, driven by `hermes harness worktree reap` and
`harness_doctor`). Nothing in the chat turn path opens one. Filed as G18.

**Gate.** Depends on R2's answer about how much real mutating work this lane is meant to
do unattended. With `unbounded` shipping as the default (`terminal_envelope.py:66`,
`permission_modes.py:69`) the lane can already push and delete, and the compensating
control is detective (the receipt) rather than preventive — which raises, not lowers, the
value of an isolated tree.

---

## R6 — Skills are surface-filtered on this lane

**Evidence.** The chat lane passes `skill_surface="mission_chat"` and
`skill_root_node_mode=False` (`persona_runtime.py:250-251`), so the skill set differs
from `hermes chat`. Filed as G15.

**Gate.** Confirm the filter is still wanted post-unbounded-ruling, since its original
justification was the same cost policy the ruling bypasses. If it is wanted, it needs a
typed drop row on the same list as the MCP and capability drops — the G5 pattern
(`persona_runtime.chat_lane_capability_drops:688`) — so a missing skill is an explained
absence rather than an unexplained one.
