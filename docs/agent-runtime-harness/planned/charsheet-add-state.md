# Planned — `characters add-state`: more strips on an installed character

**Owner domain:** system architecture ([01-system-architecture.md](../01-system-architecture.md)) —
the charsheet lane is named there as a live sub-lane of visual identity.
**Status:** not built. No verb exists.
**Raised / verified:** 2026-08-24 against `2a21cd26b6`.
**Origin:** owner ask on the 2026-08-24 authoring run; slice **A3** of the
launcher-side plan `EterniaLauncher/docs/spatial/CHARA_CONSOLE_AUTHORING_QA_PLAN_2026-08-24.md`,
shaped by `docs/mission_control/planned/console-character-authoring-architecture.md` §12 step 7.
Both live in the launcher repo; this file is the hermes half.

## The ask

Add a state — say `jumping:6` — to a character that is already composed and
installed, without starting the draft over and without touching a single
approved row.

## What is true today

The `characters` parser tree is `start list status base turnaround
reroll-direction approve-direction rows reroll-row thumb compose reopen sprite`
(`hermes_cli/harness.py`, the `characters_subs` block). There is no `add-state`,
and the state vocabulary is fixed at `start`: `--states` is parsed once into
`SheetSpec.states` (`agent/charsheet/spec.py:parse_states` → frozen
`StateSpec(name, frames, directional)` tuples) and written into `draft.json`.
`reopen` returns a composed draft to stage `rows` for the *existing* spec only
(`agent/charsheet/draft.py:reopen`).

So the loop an operator can run today ends at "re-author the character".

## The shape

`characters add-state --draft <id> --state jumping:6[:fixed]` — flags, not
positionals, matching every other draft verb. It:

- parses the state with the existing `parse_states` grammar (one authority for
  `name:frames[:fixed]`, including its rejection of `-` in a state name);
- **replaces** `spec.states` with a new frozen tuple that appends it — the
  dataclasses are frozen on purpose, so this is a new value, never a mutation;
- seeds the new rows at `attempts: 0`, anchored to the already-approved
  turnaround references, and touches no approved row;
- refuses a duplicate state name, and refuses at any stage but `rows` — which
  makes `reopen` the only door, so `add-state` needs no stage logic of its own.

The operator sequence on an installed character is then
`reopen → add-state → rows --only <new rows> → QA → compose`, and the recomposed
`character.json` carries the new state with its frame count and row indices.

**Add only** (owner decision 8, default). Removal would delete approved attempts
and the notes stored with them — the durable QA record — and its only consumer is
coverage. If it is ever wanted it is a separate verb with `--confirm`, never a
flag on `add-state`.

## What already holds its end, and must keep holding it

- **The state vocabulary is open on both sides.** hermes states are strings; the
  launcher resolves rows through fallback chains and pins "no enum of sheet
  states" as a boundary test. Nothing may start branching on `idle`/`walk` or on
  the row count 10.
- **Rows are a keyed set.** `status --json` items, the launcher's client models
  and the QA card's row list all iterate `spec.states × authored directions` from
  the payload.
- **`thumb` is state-agnostic** — it takes any authored row key, so new rows get
  crops for free.
- **The launcher's registry entry is reserved, not stubbed.** `characters.add-state`
  is registered the day this verb lands and not before; the launcher's
  argparse-shape gate walks a committed dump of this parser tree and refuses a
  capability naming a command hermes does not have.
- **A new state is installed, not necessarily reachable.** The spatial runtime
  only asks for names its state policy maps a motion to, so an authored `cheer`
  row is silent until something requests it. The authoring skill
  (`harness-skills/harness-charsheet-authoring/SKILL.md`) says so when an operator
  names a state outside that vocabulary; wiring custom-state triggers is a
  spatial follow-up, not this verb's.

## Gate to open this

Focused suites green — `tests/agent/test_charsheet_draft.py`,
`tests/hermes_cli/test_harness_characters_cli.py` — plus a new test that:

1. adds a state to a draft at stage `rows` and asserts the approved rows keep
   their attempt counts, their approved index and their notes;
2. asserts a duplicate state name and a wrong stage are both refused in the flat
   pets error shape (`{"ok": false, "error": …}`, exit 2), never clamped;
3. asserts the recomposed `character.json` lists three states.

Then live on the `anime-girl` draft: `reopen`, `add-state --state jumping:6`,
`rows --only jumping-*`, `compose`, and read three states out of the installed
manifest.
