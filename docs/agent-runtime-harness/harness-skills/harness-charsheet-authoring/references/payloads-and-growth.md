# Payload shapes, stage refusals, and growing a character

**When to open:** you are about to read a value out of a payload and are not
certain where it lives; a verb refused with a stage; you need an attempt number;
the operator asks for a NEW STATE on an installed character (`add-state`), or
asks how big a sheet is. Everything here is operative — the verb table in
`SKILL.md` says what the verbs do; this says what their answers mean.

Stages run `turnaround → rows → composed`. An out-of-order verb refuses with a
flat `{"ok": false, "error": …, "stage": …}` and exit 2, and the error names the
stage order — read it instead of guessing. These refusals are actionable text you
can hand the operator verbatim; if one ever names a flag the verb does not
accept, report it as written rather than translating it into the flag you think
was meant.

**Attempt numbers are 0-based** in every payload and in `--attempt` (`-1` =
latest). Human-facing lines and the store's own filenames are 1-based
(`attempt-1.png` is attempt `0`). **A QA card relabels, never renumbers**: say
"attempt 3 of 3" and pass `--attempt 2`. Mirrored directions are never
generated, never composed and never QA items.

**Read the payload you were handed, not the list you remember.** These payloads
are supersets and they grow additively — `thumb` alone gained two keys and lost
one inside a single day. Absence travels as JSON `null` (older payloads may still
carry `""`); tolerate both on read and emit neither.

**`status --json` answers under `.status`**, and `.status.rows` /
`.status.turnaround` are MAPS keyed by item key, not lists. Each value carries
`attempts`, `approved`, `current`, `approvedPath` and a `history` array of
`{attempt, created, note, path, rejected}`. `.status.stages` is the ordered stage
list; `.status.pending` / `.status.missing` are `{rows: [], turnaround: []}`. The
per-attempt `path` is how you show attempt N beside attempt N−1 without a second
lookup, and the reroll note rides in the same entry — which is what makes an
attempt reproducible. `.status.spec` is also the **only** payload anywhere that
carries `sheetWidth` / `sheetHeight`: the installed `character.json` does not and
neither does `sprite`. Answering "how big is this draft's sheet" needs a DRAFT
id, and it is never computed from `frameW × frameH × rows`.

**`sprite --json` inlines the whole sheet as base64 and has no path-only mode.**
On the installed `anime-girl` that was `spritesheetBase64` = 438,972 characters
(428.7 KiB) as a two-state sheet and 480,040 characters (**468.8 KiB**) once a
third state existed — against a 4,096-byte event cap and the terminal tool's own
50,000-byte output cap (`tools/tool_output_limits.DEFAULT_MAX_BYTES`, unset in
`profiles/base`). Truncation splices a notice between a 40% head and a 60% tail,
which keeps the structural fields while making the JSON unparseable and spending
roughly 12k tokens of context on base64. A small 4-way sheet survives intact,
which proves nothing about a real one. **On the chat lane read `character.json`
(the whole taxonomy, ~3 KiB) or `status --json`.** `characters list --json`
already carries `directory` and an absolute `sheet` per installed character, so
you rarely need more.

**`list --json`'s `drafts[].id` is NOT a key, and on this machine `list` really
does print the same draft id twice.** A draft directory and a hand-made
`.backup-…` copy of it both list, both carrying
`"id": "20260824-140756-cd645a"`; `directory` is what differs. Key on
`directory`, or de-duplicate deliberately and say which one you kept — otherwise
you can silently pick the backup and author into it.

### Growing a character

Adding a state to a character that is already installed is
`reopen → add-state → rows --only <the new keys> → QA → compose`. `reopen` is the
only door: `add-state` refuses at any stage but `rows`. It takes exactly ONE
state (a comma-separated list is refused — `--state` is singular), refuses a
duplicate name, touches no approved attempt and never renumbers a row. The state
is APPENDED, so the sheet grows DOWNWARD and every row the old manifest published
keeps its index — but the sheet's HEIGHT changes, so anything keyed on the
sheet's size or bytes (`spritesheetRevision`) is deliberately stale afterwards.

**Take the new row keys from the verb's own answer** — `rows` in the payload, and
the human line spells the whole `--only` string ready to paste. `--only` has no
glob, and the count is `len(spec.scheme.authored)`: five on an 8-way sheet,
**three on a `--directions 4` sheet**. The new rows read `attempts: 0` and land
in `missing.rows`; `compose` refuses while any of them lacks an approved strip,
naming every one, so an un-generated state cannot silently ship.

**"Touches no approved row" is true of the ATTEMPTS and false of the picture, and
this surprised everyone.** Composition scales the whole sheet by ONE global
factor taken over every row (`normalize_cells`), so a state with a taller
envelope — a jump — pulls that factor down for rows drawn months earlier.
Measured when `jumping:6` was added to the live character: every `idle` and
`walk` cell came back at **0.818×** its previous linear size, art-fill fraction
0.9327 → 0.8029, and the installed character drew **18.2% shorter** — 1.192 m,
3 ft 11, where she had been 1.458 m. Nothing went red. So: an operator's "she
looks right" judgement from before an `add-state` does not transfer, the cost is
whole-sheet, and it is worth saying before you spend the generations. The same
run took the sheet from 10 rows to 15 and its decoded size from 12,779,520 B to
19,169,280 B — exactly 1.50×.

**A state is added, never removed** (§13.8, ruled add-only). There is no remove
verb and none is planned as a flag: dropping a state would delete approved
attempts and the operator notes stored with them, which are the durable QA
record. If the operator wants a state gone, say that plainly rather than
promising a verb that does not exist.

**An authored state is not automatically a reachable one.** A row the runtime
never requests is installed and silent — the live character's five `jumping-*`
rows are installed and unaskable, because the airborne request is a sector-blind
`jump` and no consumer spells `jumping-<sector>`. Say so when the operator names
a state outside the runtime's motion vocabulary.

Frames are **2..8**, on both doors, refused at the moment you declare the state:
`frame count 1 for state 'x' out of range; expected 2..8`. A draft that already
carries a one-frame state (an older gap allowed it) still loads and still lists;
it simply can never reach `rows`, and that is the honest report to give about it.

**A row is grounded on the APPROVED turnaround attempt, never merely the newest**
(`CharacterDraft._row_reference` → `store.current`, which is not `store.latest`),
and a `:fixed` row is grounded on the BASE image in the front view — never on a
turnaround. So "reroll `ne`, look at both, keep the original"
(`approve-direction --direction ne --attempt 0`) is durable: every strip drawn
afterwards, including a state added months later, anchors on the attempt they
kept. And **there is no way back to `turnaround`** — `reopen` goes
`composed → rows` and nothing else does, and no verb replaces an approved
reference. Approving the turnaround is the last moment a reference can change, so
QA that strip hard before `approve-direction --all`.
