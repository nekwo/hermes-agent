# Planned — the agent console's last two lane-ambiguity gaps

**Status:** not built. **Owner surface:**
[06 — Office and board](../06-office-and-board.md).
**Origin:** `AGENT_CONSOLE_DEAD_LANE_AND_LIMITS_PLAN_2026-08-16.md` (archived),
gaps G-1..G-4, plus the lane map's §3 silent-degrade list.

Most of that plan shipped, and three of its findings are now closed in code — do
not re-open them:

- **G-1 (search hides the lane it is hiding models from) — CLOSED.**
  `searchMissionAgentUnavailableModels`
  (`agent_chat/mission_agent_model_switcher_view_model.dart:726`), rendered as
  disabled rows at `agent_chat/mission_agent_chat_panel_parts/agent_model_menu.dart:467`.
- **G-3 (all-lanes-down collapse is generic) — CLOSED.** The
  `catalogUnavailable` availability carries the lane issues
  (`mission_agent_model_switcher_view_model.dart:497-502`).
- **G-4 (no home provenance) — CLOSED.** `probedHomeCaption` renders in the
  switcher menu (`agent_model_menu.dart:235`).
- **Defect 2 (Limits chip's swallowed 401) — CLOSED.**
  `usage {phase} failed (HTTP {status} — re-auth may be required)`
  (`hermes_cli/harness.py:3840-3843`), with class-name-only discipline preserved
  for non-HTTP failures because a bare status code leaks nothing.
- The lane map's two other silent-degrade sites are closed too, both marked
  RD-L5 / EG-6.2 in code: the bare-token paste path now answers
  `providerNotConnected` naming the lane and its issue
  (`mission_agent_model_switcher_view_model.dart:958-978`), and an empty catalog
  now distinguishes "no models.dev cache on disk" from "cache present, matched
  nothing" (`data/mission_control_hermes_visibility.dart:1444-1470`).

## Row 1 — G-2: a failing lane's catalog is still undiscoverable in browse mode

**Evidence, verified 2026-08-22.** Connected lanes render as `_ProviderSubmenu`;
unconnected ones render as a flat `_ConsoleMenuItem` under a "Not connected"
section label (`agent_model_menu.dart:274-300`). The row is now honest about WHY
— amber icon plus `hermesLaneConsoleReason` as the subtitle, and pressing it opens
Settings anchored to that lane's tile — but it has no submenu and no model count,
so the lane's loaded catalog (91 models on the measured runtime, `big-pickle`
among them) is reachable only by typing a name into search.

**The reason it was deferred, unchanged.** A disabled 91-model submenu is menu
furniture, and the interim diagnostic floor was the right first thing to ship.

**Gate.** Either build the disabled submenu (every row unselectable, the lane's
issue as the section subtitle, pressing any row opening Settings anchored to that
lane), or record a decision that browse-mode discovery of a dead lane's catalog is
not wanted and delete this row. Test either way: a failing lane with a non-empty
catalog must not render a control that can select one of its models.

## Row 2 — the per-home credential and dotenv split

**The standing finding, and the most expensive one in the original
investigation.** The launcher runs every probe with
`HERMES_HOME=<root>/profiles/<profile>`; the operator's shell uses
`X:/Eternia/.hermes`. Same machine, same minute, the two homes disagreed about
credential health, active model, and which usage lanes exist — so `hermes auth
list` in a shell was never a diagnostic for the console, and nothing said so.
G-4 put the probed home on screen, which is the *symptom* fixed; the split itself
is not.

**Consequences that remain.** The root `.env` carried 15 variables including
`ANTHROPIC_TOKEN`; the profile `.env` carried one. The Limits panel under the
launcher's home will keep omitting Anthropic until this is designed.

**Gate.** This belongs to whatever owns first-class provider logins, and the
constraint is stated so it cannot be missed: **any login UI that reads or writes a
different home than the serve child recreates both defects at once.** A design
that unifies the stores, or that writes through to the serve child's home, must
say which it does and prove it with a two-home probe showing identical credential
health.

## Not in scope here

Rotating the dead key, re-authing anything, or touching any credential store.
Credential operations are the operator's hands only.
