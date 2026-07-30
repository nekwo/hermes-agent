# Mission-chat per-turn context — the assembly, the tail, the HUD field contract

*Landed 2026-07-26 (wave 4). Structure refactor: the composed per-turn output is
byte-identical for standard turns; what changed is where the assembly lives and
what can be asserted about it.*

## The problem this retires

A mission-chat turn carries far more than the operator's sentence. Before the
model is called the harness resolves the wall budget, the skills to preload (and
their delivery envelope), the workspace `AGENTS.md`, the resident-actor runtime
signature, this lane's capability account, the situational HUD and its
snapshot/unchanged delivery, and the volatile tail the agent reads every turn.

All of it lived inside `_cmd_mission_chat_message` in
`hermes_cli/harness_parts/persona_commands.py` — a command part `exec`-loaded
into `harness.py`'s globals (`harness._load_command_parts`), not an importable
module. The consequence is specific and expensive: **the assembly was not
reachable by a unit test.** Everything guarding it had to be an AST source-shape
assertion —

> "this function calls `render_capability_block` and puts the result in a list
> literal named `volatile_lines`"

— which pins the SHAPE of the code and says nothing about the BYTES the agent
receives. A refactor that kept the shape and broke the output passed; a
refactor that changed the shape and kept the output failed. Both are wrong.

Three structural defects rode along with it:

1. **The volatile tail had no roster.** Contributors were positional entries in
   an anonymous list; who contributes — and therefore what an agent is
   guaranteed to be told each turn — was knowable only by reading the CLI body.
2. **The tail had no bound.** A widened capability policy or a chatty admission
   line could turn three bullets into a wall competing with the operator's own
   message. (And the naive fix — a slice — would be a silent truncation on the
   one channel whose whole purpose is "what is true this turn".)
3. **HUD volatility was declared in one place and enforced in none.** A
   `_VOLATILE_HUD_KEYS` frozenset was read by the revision hash; the body
   renderer merely *promised in a docstring* that it would never touch those
   keys.

## What landed

### `agent_runtime/mission_chat_turn_context.py` — the builder

`build_mission_chat_turn_context(...)` performs the whole assembly and returns
one frozen `MissionChatTurnContext`. The CLI body keeps only composition:
gather inputs, call the builder, feed the runtime, send.

Impurity is confined to `MissionChatTurnResolvers` — one named field per seam
(consume queued skills, load the preload, load `AGENTS.md`, resolve the
capability block / HUD / admission line / tool contract / permission state /
store root), each defaulting to **the same authority the turn itself called
inline**. Production passes nothing; a unit test passes fakes and drives the
builder end to end with no runtime root.

Order inside the builder is load-bearing and documented there: skills (the
queue consume is a mutation and happens exactly once) → workspace agents →
runtime signature → wall budget → capability → HUD + delivery → tail. The
runtime-context envelope is deliberately *not* a field: it needs the
`context_id` minted by the observability row, which is built from this object,
so `MissionChatTurnContext.runtime_context_envelope(context_id=…)` closes that
loop without inviting a second HUD/tail resolution.

### `agent_runtime/volatile_tail.py` — registered, budgeted contributors

Contributors register by **name** with their own **byte budget**. Over-budget
content is truncated (or dropped, below a useful-prefix floor) and the shortfall
is stated **twice**: in band, so the agent reads that it was not told everything
and must treat the rest as UNKNOWN rather than "nothing to report"; and as a
typed accounting row, so an operator/observability consumer never has to grep
rendered prose to learn a fact was clipped.

| contributor | budget (bytes) | renderer |
| --- | --- | --- |
| `turn_budget` | 1024 | `turn_budget.render_turn_budget_line` |
| `capability` | 4096 | `runtime_hud.render_capability_block` |
| `mcp_admission` | 2048 | `persona_runtime.mission_chat_admission_line` |

Budgets are **per contributor, not global**, so a long capability account cannot
squeeze out the countdown and a chatty admission line cannot squeeze out the
capability account. Each is set several times its renderer's realistic maximum
(all three are hard-capped upstream), which is why a standard turn composes
byte-identically to the hand-joined list this replaced — pinned by
`test_the_composed_tail_is_byte_identical_to_the_hand_joined_lines`.

Duplicate contributor names and non-positive budgets raise: a duplicate would
shadow the first contributor's accounting, which is the silent-loss class the
module exists to retire.

### `runtime_hud.HUD_FIELDS` — volatility declared once

Each HUD key is a `HudField(key, volatile, summary)` row. `volatile` is stated
there and nowhere else, and **both** consumers derive from it via
`stable_hud_fields(hud)`:

* `situational_hud_revision` hashes the stable fields (a countdown never
  re-snapshots the stable block);
* `render_situational_hud_block` *renders from* the stable fields — so a
  volatile fact is not merely "not rendered by convention", it is **absent from
  the dict the renderer reads** and cannot be rendered there by a later edit.

An undeclared key defaults to **stable**. That direction is the safe one: the
worst case is an extra re-snapshot, whereas defaulting to volatile would
silently drop a new fact out of the revision and let a cached body go stale.
`test_every_key_the_resolver_can_emit_is_declared` keeps the roster from falling
behind `resolve_situational_hud`.

### `--explain-envelope` — the operator half of the envelope posture

`hermes harness persona tool-diff <persona> --explain-envelope [--json]`, the
sibling of `--explain-mcp`. Renders, from
`agent_runtime/terminal_envelope_explain.py`: whether the lane binds an envelope
scope, the disposition (`deterministic` vs `legacy_ambient` — the fail-open /
fail-closed coin flip the governed lane retired), the exact ROOT-config grant
key, the live grants, and the refusal set split into operator-grantable and
hard-floor categories. Ruling R-2 leaves the hard-floor category empty while
retaining the wire shape for future code-owned floors.

Everything is read from the canonical authorities (`explain_terminal_envelope`,
`hard_floor_command_classes`, `scope_for_persona`) — no taxonomy, floor or
grant rule is re-derived. `hard_floor_command_classes()` was added to
`terminal_envelope.py` as a pure read accessor over the two existing sets.

## Pinned semantics (do not "tidy" these)

* **The MCP admission line stays a SEPARATE voice from the capability block.**
  Its denials resolve at a different lifecycle point (execution-time
  degradations reach the agent through `agent.steer`, after the envelope is
  sealed) and it is gated on the admission kill switch. Folding it in gives one
  fact two voices, which is how an agent learns to discount both. Two
  contributors, two budgets, two independent failure modes.
* **Wall-budget visibility rides the volatile tail** (`8e7a37d6d`).
* **Capability drops + envelope grants/refusals ride the volatile tail**
  (`ddc5af110`) *and* the HUD dict, so the operator's CONTEXT peek shows the
  same account the agent was told.
* **One resolve, one object.** The budget the agent is told is the object the
  runner's clamp enforces; the capability account recorded for the operator is
  the object rendered for the agent.

## Which guards moved, and which stayed

Replaced by output assertions in `tests/agent_runtime/`:

* `test_mission_chat_turn_context.py` — tail roster/order/budgets, the
  byte-identity golden, the separate-voice non-merge, tail-on-every-delivery,
  HUD-vs-body placement, envelope well-formedness, skill preload + envelope,
  workspace pointer, runtime-signature sensitivity.
* `test_volatile_tail.py` — budgets, truncation, drop, UTF-8 safety, accounting.
* `test_runtime_hud_field_contract.py` — the one declaration, both consumers.
* `test_terminal_envelope_explain.py` — the payload, the text render, the CLI verb.

Kept as AST guards, because the property genuinely lives in the exec'd CLI
body's source shape:

* `tests/hermes_cli/test_mission_chat_capability_visibility.py` — the body calls
  the builder **once** and resolves **no per-turn policy of its own** (a
  parametrized ban-list); the fed envelope comes off `turn_context`.
* `tests/hermes_cli/test_mission_chat_budget_payload.py` — the typed
  `chat_turn_budget_exhausted` payload literals (no resolve language, typed
  fields), and that the runner's window is derived from
  `turn_context.wall_budget` rather than a second resolve.
* `tests/hermes_cli/test_mission_chat_records_injection.py` — record-at-injection:
  the observability row records `turn_context.situational_hud*`, and the fed
  envelope is rendered off the same instance.
