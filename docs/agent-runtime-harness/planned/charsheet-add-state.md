# Planned — `characters add-state`: more strips on an installed character

**Owner domain:** system architecture ([01-system-architecture.md](../01-system-architecture.md)) —
the charsheet lane is named there as a live sub-lane of visual identity.
**Status:** BUILT 2026-08-25. `characters add-state --draft <id> --state <name>:<frames>[:fixed]`
ships; the parser tree is now fourteen verbs. What follows is kept as the design
record with the as-built corrections marked; where this doc and the code
disagreed, the code won and the correction is filed below in the same commit.
**Raised / verified:** 2026-08-24 against `2a21cd26b6`; built and re-verified
2026-08-25.
**Origin:** owner ask on the 2026-08-24 authoring run; slice **A3** of the
launcher-side plan `EterniaLauncher/docs/spatial/CHARA_CONSOLE_AUTHORING_QA_PLAN_2026-08-24.md`,
shaped by `docs/mission_control/planned/console-character-authoring-architecture.md` §12 step 7.
Both live in the launcher repo; this file is the hermes half.

## The ask

Add a state — say `jumping:6` — to a character that is already composed and
installed, without starting the draft over and without touching a single
approved row.

## What is true today

*(This section describes the state before 2026-08-25 and is kept for the
reasoning; `add-state` now sits between `reopen` and `sprite`.)*

The `characters` parser tree was `start list status base turnaround
reroll-direction approve-direction rows reroll-row thumb compose reopen sprite`
(`hermes_cli/harness.py`, the `characters_subs` block). There was no `add-state`,
and the state vocabulary was fixed at `start`: `--states` is parsed once into
`SheetSpec.states` (`agent/charsheet/spec.py:parse_states` → frozen
`StateSpec(name, frames, directional)` tuples) and written into `draft.json`.
`reopen` returns a composed draft to stage `rows` for the *existing* spec only
(`agent/charsheet/draft.py:reopen`).

So the loop an operator can run today ends at "re-author the character".

## The shape

`characters add-state --draft <id> --state jumping:6[:fixed]` — flags, not
positionals, matching every other draft verb. It:

- parses the state with the existing `parse_states` grammar (one authority for
  `name:frames[:fixed]`, including its rejection of `-` in a state name) — and
  **refuses more than one entry**: `--state` is singular, the launcher registry
  renders one value for it, and a comma-separated list here would be a second,
  quieter spelling of `start --states` that applied half an operator's request
  under one review;
- **replaces** `spec.states` with a new frozen tuple that appends it — the
  dataclasses are frozen on purpose, so this is a new value, never a mutation;
- seeds the new rows at `attempts: 0`, anchored to the already-approved
  turnaround references, and touches no approved row;
- refuses a duplicate state name, and refuses at any stage but `rows` — which
  makes `reopen` the only door, so `add-state` needs no stage logic of its own.

The operator sequence on an installed character is then
`reopen → add-state → rows --only <new rows> → QA → compose`, and the recomposed
`character.json` carries the new state with its frame count and row indices.

**As built — four facts this doc did not have, taken at the code:**

- **The new state is APPENDED, and that is load-bearing.** `SheetSpec.rows()` is
  state-major, so appending leaves every row the installed manifest already
  published at the index it published — the sheet grows downward. Prepending or
  inserting would silently renumber rows a consumer is already addressing.
- **"Seeds the new rows at `attempts: 0`" writes nothing.** A row is seeded by
  appearing in the spec: its revision-store key has no history, so
  `status --json` reports `attempts: 0`, `approved: null` and lists it under
  `missing.rows` — which is what an un-generated row already looks like
  everywhere else. Writing a placeholder attempt would invent an image nobody
  drew. The payload is `{state: {name, frames, directional}, states: [...],
  rows: [...]}` on top of the house `{ok, draft, stage}` envelope.
- **A state below two frames is refused HERE, not four generations later.**
  `spec.parse_states` accepted `1` while `prompts.build_directional_row_prompt`
  demanded `2`, so `start --states idle:1` built a draft, spent the base anchor
  and three direction generations, and only died at `rows` (measured live
  2026-08-24, recorded in the skill's FIELD-NOTES). `add-state` would have been a
  second door into that trap. The number is now
  `spec.MIN_FRAMES_PER_ROW`, enforced in `parse_states` — the ONE door both
  `start --states` and `add-state --state` come through — and READ by the prompt
  builder instead of re-spelled. It is deliberately NOT raised on `StateSpec` /
  `SheetSpec`: those are also the deserializers for every `draft.json` and
  `character.json` on disk, including the drafts the old gap produced, and
  refusing them at load would take `characters list` down with them. The two
  floors answer two questions: the spec says what a sheet can HOLD,
  `parse_states` says what an operator may ask us to DRAW.
  **Corrected on review, 2026-08-25 — this bullet named a mechanism the code
  does not have, and it understated the damage.** It read
  "`CharacterDraft.list_drafts` drops an unreadable draft with a log line, so it
  would vanish from `characters list` instead of being explained", which was
  taken off `list_drafts`' `except` clause rather than measured at the
  chokepoint that applies it. That swallow never fires for a bad spec:
  `CharacterDraft.load` reads JSON only and `CharacterDraft.spec` is computed on
  ACCESS, so `list_drafts` returns the bad draft. The raise lands one level up
  in `_characters_draft_summary` (`spec = draft.spec`), inside
  `_cmd_characters_list`'s own `except _CHARACTERS_EXPECTED` → `{"ok": false,
  "error": …}`, exit 2. Measured over a home holding one good draft and one
  `idle:1` draft: `ok=false` and ZERO drafts — **the whole verb fails and every
  draft vanishes, not one.** The ruling is unchanged and stronger than the
  bullet claimed.
- **The human line hands over the `--only` list.** Because `--only` has no glob
  (below), the verb that knows the new row keys is the verb that spells them:
  `Draft <id>: state jumping added (6 frames, directional); 5 new row(s) to
  generate — `characters rows --draft <id> --only jumping-s,jumping-se,...``

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

## Gate — as run (2026-08-25)

Focused suites green — `tests/agent/test_charsheet_draft.py`,
`tests/hermes_cli/test_harness_characters_cli.py`, plus
`tests/agent/test_charsheet_spec.py` and `tests/agent/test_charsheet_pipeline.py`
(the frame floor moved through `spec` and `prompts`, so their own suites are part
of the gate) and `tests/hermes_cli/test_harness_pets_cli.py` (the standing pets
sprite byte-baseline, because `harness.py` was touched) — plus new tests that:

1. adds a state to a draft at stage `rows` and asserts the approved rows keep
   their attempt counts, their approved index and their notes;
2. asserts a duplicate state name and a wrong stage are both refused in the flat
   pets error shape (`{"ok": false, "error": …}`, exit 2), never clamped;
3. asserts the recomposed `character.json` lists three states.

**Review round, 2026-08-25 — what the first pass' tests did not hold.** The
anchoring test asserted `result["rows"][key]["reference"] ==
str(store.current(turnaround_item(direction)))` — the expression
`_row_reference` itself computes — over a fixture where no direction was ever
rerolled, so `current()` and `latest()` named the same file and the mutation
`store.current` → `store.latest` left all 65 tests green. It now builds the
divergence it exists to catch: reroll one direction, approve the OLDER attempt
(`approve-direction --attempt 0`), and assert the new row's reference is the
attempt the operator KEPT and is not `store.latest(...)`. A `:fixed` state
gained a test at the same time — its row has no direction, so `_row_reference`
grounds it on the BASE image, which is what `add_state`'s docstring denied.

Then live on the `anime-girl` draft: `reopen`, `add-state --state jumping:6`,
`rows --only jumping-s,jumping-se,jumping-e,jumping-ne,jumping-n`, `compose`,
and read three states out of the installed manifest.

**Live, as run (2026-08-25, home read back from `harness status --json` →
`.runtime_health.hermes_home` = `X:\Eternia\.hermes\profiles\alice`, never
asserted).** `reopen` → `rows`; `add-state --state jumping:6` returned the five
keys `jumping-s,jumping-se,jumping-e,jumping-ne,jumping-n` — read off the
draft's own `spec.scheme.authored` (`s se e ne n`), not copied from this file —
and `status --json` then showed `idle-*` at 1 attempt each and `walk-n` still at
3 attempts / approved index 2, `walk-ne` at 2 / 1: the reroll history from the
2026-08-24 run, untouched. The five new rows sat at `attempts: 0`,
`approved: null`, in `missing.rows`. `rows --only <the five>` generated all five
on their FIRST attempt, each grounded on `revisions/turnaround@<d>/attempt-1.png`
— the reference approved before any row was ever drawn, which is the anchoring
claim proven live. `compose` then validated 15 filled rows at 1536x3120 and
installed a `character.json` listing three states, with `idle-*`/`walk-*` still
at row indices 0..9 and `jumping-*` appended at 10..14.

**The keys are spelled out because `--only` has no glob.**
`_cmd_characters_rows` splits the value on commas and hands the parts to
`run_rows`, which matches row keys EXACTLY — `unknown = [key for key in wanted
if key not in known]` raises `ValueError` naming the authored rows. An earlier
wording of this step said `--only jumping-*`; that is not a wildcard, it is one
unknown row key, and the gate would have died on the first draft it was run
against. The five keys are `row_key("jumping", d)` over `EIGHT_WAY.authored`
(`"s", "se", "e", "ne", "n"` — verified in `agent/charsheet/spec.py`), which is
the scheme the live `anime-girl` draft carries. A `--directions 4` draft
authors three (`FOUR_WAY.authored` = `"s", "e", "n"`) and its list is shorter by
two — so read the draft's own `spec.scheme.authored` rather than copying this
line.
