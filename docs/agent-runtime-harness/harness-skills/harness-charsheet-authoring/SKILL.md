---
name: harness-charsheet-authoring
description: Author, QA and repair an 8-way character sheet from a chat turn — the staged `harness characters` verbs, the crop-and-compare looking procedure, the handedness gate and how to read its refusal, reroll-note craft, and the MEDIA / CHARSHEET-QA lines the operator's console renders into a live card. Use whenever a turn is asked to make, fix, add a state to, resume or inspect a character sheet.
metadata:
  hermes:
    surfaces: [mission_chat]
    modes: [standard]
    load_policy: required_preload
---

# Harness Charsheet Authoring

You author a character by talking. The operator describes someone; you run the
staged `hermes harness characters` verbs, show every generated image as a card,
ask one question per decision, and hand back an installed sheet.

**The verb list was never the hard part.** This file is written from
`FIELD-NOTES.md` beside it — the accumulated record of running this loop, kept
because a claim written from a plan is a claim nobody has paid for yet. A
competent operator with full repo access authored a character end to end and
shipped it with a one-pixel seam through the hair of two rows *and* with one
diagonal drawn facing the wrong way. Neither was caught until a human saw the
character standing in a 3D scene.

Three things cost the time, and none of them is a verb: *looking* is a
procedure and a glance is a reliable false negative; the fix loop is
stage-gated, one-way in places, and costs whole generations; and a note is a
drawing instruction, not a complaint.

**Where the rulings live.** Citations of the form **§13.n** are owner decisions
recorded once, in the launcher companion
`docs/mission_control/planned/console-character-authoring-architecture.md` §13.
This file carries the operative half only and never restates the reasoning — if
the two ever disagree, the register is right.

`hermes` == `python -m hermes_cli.main`. Always `--json`. Never touch
`draft.json`, the revision store or the sheet with file tools — every state
change has a verb, hand-editing the draft is what `reopen` was landed to
retire, and the next `compose` overwrites any hand edit to the sheet anyway.

## Preflight — three probes, before any generation

**1. Which home am I authoring into?** The draft root is
`$HERMES_HOME/characters` (`characters_dir()`), drafts under
`.drafts/<draft-id>/`, installed sheets under `<slug>/`. Your turn's
`HERMES_HOME` is **your persona's own profile home** — the runtime rebinds it
for the duration of the turn from the persona's `hermes_profile`
(`profile_context.persona_profile_context`, which rebinds a ContextVar *and*
`os.environ`), so it is *not* necessarily the home the console's `harness serve`
process was launched with. It early-returns without rebinding for a persona that
declares no profile at all, and then the home really does follow serve — so this
is two answers, not one. §13.6 is the single statement of the rule; the
operative instruction is *read back the home the runtime resolved, never assert
one*:

```
hermes harness status --json      # → .runtime_health.hermes_home
hermes harness characters list --json
```

**Echo that path in your first reply, in prose.** Nothing else carries it. The
`CHARSHEET-QA:` line deliberately does not (§13.22), so the console mints the
character's binding with its home UNKNOWN, and your transcript is the only place
the home your turn resolved will ever exist. The launcher does record a home of
its own on that binding — but it is a SIGHTING, *a home the launcher OBSERVED
the draft readable in* (§13.24), written when an operator opens Studio's adopt
door and never by anything you emit. It is not the home you authored from, and
nothing downstream will turn it into one. Do not derive a home from a profile
name or from a spawn environment; a derived home looks exactly like an observed
one, and this is the one fact that is not derivable.

Three ways this goes wrong, all seen live:

- **A relative `HERMES_HOME` resolves against the shell's cwd.** `HERMES_HOME`
  is used as written (`hermes_constants._hermes_home_from_env`), and the CLI
  trusts any value whose immediate parent directory is named `profiles`. So
  `HERMES_HOME=profiles/base` run from a repo checkout authors *into the repo
  working tree*. A whole `fox-scout` character sits in one today and had to be
  gitignored. Always absolute.
- **A home that is not profile-shaped is re-pointed at the sticky active
  profile.** If `HERMES_HOME` names the hermes ROOT (or is unset), the CLI reads
  the `active_profile` marker and rewrites the home to that profile
  (`hermes_cli/main.py:_apply_profile_override`). Same command, two answers,
  depending on a file you did not set — and a root is a different SHAPE from a
  profile home, not merely a different value.
- **Never rebuild a path from a slug or an attempt number.** Every payload names
  its own `directory` / `path` / `source`. Read those — and treat an absent path
  as absent whichever way it arrives (`null` or `""`). Never turn one into a
  bare `MEDIA:` line.

If the operator's draft is not in your list, say which home you are in and which
home the draft is in. Do not author a second copy.

**2. Does the image provider actually return an image?** Credentials are
per-home (`auth.json`), and a plan-gated account fails *politely*: HTTP 200, the
`image_generation` tool silently stripped from the request, and a model that says
it has no such tool. It reads exactly like a broken plugin. Spend one cheap
`image_generate` call before committing to a 16-generation run. (`image_generate`
is the TOOL; `image_gen` is the toolset that carries it, and separately the
config section that selects the provider — only one of the two is callable, and
they differ by one word.) If nothing comes back, report "the image provider is
unavailable in this home" and name the home — never start the pipeline and
discover it on generation twelve.

- **A `401 token_expired` right after a plan change is a STALE STORED TOKEN, not
  a missing credential.** An account upgrade invalidates the token already on
  disk, and it keeps a local expiry hours out — so the probe fails while
  `auth.json` looks perfectly healthy. This fires immediately after the operator
  did the right thing about the trap above, which makes it the most confusing
  one: sending them back to check `auth.json` placement is the correct answer
  for a home with no credentials and the wrong answer here. Force a refresh and
  re-probe **before** reporting the provider unavailable; the refreshed token
  reads the new plan.

**3. Who is authoring — and pass it, verbatim.** `--authored-by` on `start` is
free text nobody validates, and it is the only provenance a draft carries. Your
own identity clause states the id you must use: *"You are (display name)
(Mission Control persona id: `<id>`)"*. Copy what is inside that parenthesis. On
a live run an agent instead wrote a slugified copy of its instance DISPLAY name,
and that value resolves to nothing — not a persona id, not a profile.

The cost is downstream and it is not small. The console addresses a draft as
*(persona, draft id)*, so a draft with **no** `authored_by` refuses to resume at
all — a typed refusal that names what it does not know (§13.21) — and a draft
carrying an id no roster holds refuses as "persona unresolved". Omitting the
flag is not neutral: it strands the draft.

## The verbs

Fourteen, flat, all with `--json`. Every draft verb takes `--draft <id>` as a
**required flag**; only `sprite` takes a positional `<slug>`. **The draft id is
not the slug** (`20260824-140756-cd645a` vs `anime-girl`).

| Verb | What it does | Stage it needs |
|---|---|---|
| `start --concept … [--slug] [--display-name] [--style] [--states] [--directions] [--base-image] [--authored-by]` | Creates the draft. Generates nothing. | — |
| `list` | Drafts + installed characters, with their directories. | — |
| `backfill-home` | Records `hermes_home` on drafts under THIS home that predate the field, and on no others. An already-recorded home is never rewritten; `updated` is left untouched. Idempotent, receipted. **Operator-run — do not fire it as part of an authoring flow.** | — |
| `status --draft <id>` | Stage, spec, per-item QA history with every attempt's `path`. | any |
| `base --draft <id> --image <path>` | Sets/replaces the identity anchor. | any |
| `turnaround --draft <id>` | One generation per **authored** direction (`s se e ne n` for the 8-way scheme). | `turnaround` |
| `reroll-direction --draft <id> --direction <d> [--note …]` | Re-draws one direction reference. | `turnaround` |
| `approve-direction --draft <id> (--direction <d> [--attempt n] \| --all)` | Approving every authored direction advances to `rows`. **The last moment a reference can be changed.** | `turnaround` |
| `rows --draft <id> [--only a,b]` | One generation per **row strip** — never per frame. | `rows` |
| `reroll-row --draft <id> --row <key> [--note …]` | Re-draws one strip. **Auto-approved, and there is no undo.** | `rows` |
| `thumb --draft <id> --row <key> [--attempt n] [--frame n] [--scale n]` | Writes a card-size QA crop of ONE frame. | **any** |
| `compose --draft <id> [--accept-handedness <row>:<basis>,…]` | Composes, validates, installs; advances to `composed`. | `rows` |
| `reopen --draft <id>` | Back to `rows` for fixes. Installed sheet untouched. | `composed` |
| `add-state --draft <id> --state <name>:<frames>[:fixed]` | Adds ONE state; seeds its rows un-generated, touches no approved attempt. | `rows` |
| `sprite <slug>` | The installed payload the launcher reads. **Never pipe it into a turn** — see below. | — |

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

## Looking is a procedure

A one-pixel dark line over a full-bleed magenta chroma field is invisible at
fit-to-window scale. "I looked at the strip and it's fine" was wrong on the strip
that carried the defect. So:

- **Never judge from a full sheet or a full row strip.** A row strip *is* one
  row — enlarging it removes no pixels. The unit that survives to card size is
  one **frame cell**.
- `thumb` is that procedure as a verb: it crops one frame by the row's own frame
  count, keys the magenta field out (it is opaque at alpha 255 — compositing
  without the key just replaces the backdrop), NEAREST-upscales it (no filter
  averages a defect away) onto flat dark, and writes
  `<draft>/thumbs/<row>-attempt-<n+1>-frame-<f+1>-x<scale>.png`. `source` in the
  payload is the attempt file the crop came from — the same string
  `status --json` reports as that attempt's `path`. `thumbs/` is `thumb`'s
  namespace; nothing else writes there, and nothing writes into a draft with
  file tools.
- **`thumb` is row-only.** At the `turnaround` stage there is no crop verb at
  all: passing the store's own key answers *"'turnaround@s' is not an authored
  row of this sheet"*, exit 2. QA a direction reference by declaring the
  reference itself, or by cropping to a path OUTSIDE the draft.
- **`--frame 0` is a default, not an answer.** A defect is hunted frame by frame;
  walk `--frame` across `frames`.
- **Divide before you zoom.** `thumb` slices one cell (`strip_px / frames`) and
  then multiplies by `scale**2`, so the output is `strip_px * scale**2 / frames`
  and it SHRINKS only while `scale**2 < frames` — at the default `--scale 2`,
  only when the row has more than 4 frames. Measured on an `idle:2` row: the
  strip is 1774×887 = 1,573,538 px and the default crop is 1774×1774 =
  3,147,076 px — **twice the strip it was meant to make lighter**. On a short row
  `--scale 1` is the crop and `--scale 2` is an enlargement. CHAR8's own rows are
  the happy case (`walk:8` gives 0.5×, `idle:6` gives 0.67×), which is why this
  went unnoticed on the default character.
- `--scale` is bounded by **output**-pixel budgets and is **refused, never
  clamped** — the refusal names the source dimensions and the remedy. Only ONE of
  the two bounds refuses: at or below the default the crop must clear the
  console's decode ceiling; a deeper zoom is allowed as a viewer artifact and
  comes back `withinConsoleBudget: false`.
- **Nothing weighs a raw strip or a turnaround reference.** That budget lives
  inside `thumb` alone. Live, an agent declared three raw strips at 1,573,538 px
  each against a composed sheet of 239,616 px — 6.6× the sheet per card.
  "Declare a crop, not a sheet" is a rule no tool enforces on a path it does not
  own.
- **The single most reliable read is attempt N beside attempt N−1**, same row,
  same frame, declared in one turn and labelled. Two independent agents each
  failed to see the seam at 5–6× magnification on a single frame; side by side,
  both saw it immediately. **Compare, don't zoom.**
- **Comparison aligns PANES, not pixels.** The viewer's compare mode drives both
  panes from one transform, but each picture is letterboxed `BoxFit.contain`
  inside its own pane. Two attempts at the SAME dimensions land
  feature-for-feature — that is the case the finding above was drawn from.
  Different aspect ratios do not, and the viewer will not tell you: the same
  image-relative height sits at two different screen heights. Never compare a
  crop against a full sheet and trust the alignment. Across unequal sizes the A/B
  flip is the stronger instrument — one box, alternating, transform deliberately
  not reset.
- **The seam's contrast is directional.** On the known artifact the band read
  28.6 against 135.4 immediately BELOW it, while above it the hair darkened
  gradually over ~50 rows. It is a hard lower boundary, not a dark band between
  two bright regions — "look for a dark band" is the wrong search; look for a
  hard EDGE.
- **Do not build a pass/fail scanner** *for defects in the art* — seams, stray
  pixels, anatomy. A refined "opaque dark pixels with light pixels above"
  predicate flagged all ten rows including the two known-good rerolled ones
  (18–85 hits each), and a "sharpest dark row" rank put the sock band and the
  skirt hem — real art, present in the clean strip too — above the actual seam. A
  scan is *relative triage* at best: rank rows, then look. The operator's eye is
  the gate. **One structural measurement is not that and is already built —
  handedness. Do not write a second one.**
- **Default hypothesis: the model drew it.** Pipeline residue (slicing, keying,
  the palette lock) was the first guess and cost the most; the defect was in the
  generated art and a reroll fixed it with zero pipeline change.
- **Read a clean measurement as "nothing measurable objects", never as "she looks
  right".** The `ne` defect passed every check that existed and was caught by a
  pair of eyes.

### The two weight flags — both, or it does not go inline

Every `thumb` payload carries two booleans. They answer different questions, they
disagree in BOTH directions on drafts that exist, and neither may be inferred
from the other. **`cardSafe` is GONE** — one name was carrying both guarantees.
If you ever see it, the installed skill or the payload is stale.

- **`withinConsoleBudget`** — the crop is under `pipeline.MAX_CONSOLE_CARD_PIXELS`,
  a module constant sized once from `CHAR8` (1536×2080 = 3,194,880 px). It does
  **not** move with a draft. *Will this file sink the console?* It is the only one
  that refuses anything.
- **`withinOwnSheet`** — the crop is no larger than the sheet THIS draft
  composes, from its own `spec.sheet_size()`. It moves with every draft. *Did
  cropping buy anything?* It refuses nothing and is reported at every scale.

Both ends measured, which is the whole proof one boolean could not carry both. A
`--directions 4`, `idle:2` draft composes 384×624 = 239,616 px and its DEFAULT
crop came back 1774×1774 = 3,147,076 px — `withinConsoleBudget: true`,
`withinOwnSheet: false`, at **13.1× that whole sheet**, clearing the fixed
ceiling by 1.5%. At the other end, `add-state --state jumping:6` recomposed the
live sheet at 1536×3120 = 4,792,320 px, **exactly 1.50×** the console ceiling —
there a crop can be `withinConsoleBudget: false` and `withinOwnSheet: true` at
once, so a refusal at the default scale means "over the console ceiling" and
never "heavier than your sheet".

**Declare a crop with `MEDIA:` only when BOTH are true.** That is the same rule
the launcher's inline card follows (§13.17). Say WHICH bound it missed, because
the remedies differ: over the console ceiling is an unsafe decode; over your own
sheet is a safe picture that mitigated nothing. Read the flags — never carry a
copy of either threshold and never compute the second by hand.

### Card geometry, so you declare crops knowing it

The console's MEDIA hero card is a fixed **1:1 centre-cover square** —
`AspectRatio` at 1.0 over `BoxFit.cover`, tile 420–720 px — with no dimension
probe above it. A tall crop is judged on its middle square. **The card was never
the verdict surface**; opening it is. The opened viewer pages the whole set with
the arrow keys, zooms to 8×, and enters compare with `C`, so the cost of a tall
crop is one keystroke of inspection rather than a re-generate. Crop the frame you
are hunting in, say where in the frame the defect sits, and tell the operator to
open the card when it sits off-centre.

## Handedness — which way a direction actually faces

Only the AUTHORED directions are drawn; the consumer builds the other three by
flipping them. **So one mirrored authored row breaks TWO directions and the six
others look perfect** — which is exactly why it survives QA. Live: `anime-girl`'s
`ne` was drawn facing north-WEST in `idle`, `walk` and later `jumping`, which
broke `ne` AND `nw` while the other six stayed right. The operator's report was
"forward-left and forward-right are inverted, standard forward is fine" — a
description that fits a consumer sector-mapping bug far better than it fits one
bad row, and the consumer's code was checked end to end and was correct.

*When an operator reports two opposite directions wrong and the rest right,
suspect ONE authored row and its mirror before suspecting the consumer. Ask which
of the eight are wrong: a consumer bug rarely spares six.*

**Why it happened, and the phrase never to use.** The prompt said the row was
"turned toward the viewer's right" — unambiguous for a FRONT view and ambiguous
for a BACK one, because it flips meaning depending on whether you resolve it in
the frame or in the subject's own body. Write facing in FRAME terms, three ways
at once: which way the body points in frame, which shoulder is nearer, which side
the sliver of face may appear on — plus an explicit refusal of the mirror. "Turn
her the other way" and "face north-east" both leave the model the same coin flip
that produced this.

**Approving a turnaround is not approving its rows.** Measured on that draft,
where nothing was ever rerolled at `turnaround`: the approved `e` reference is a
**west-facing profile** — the shoes point left — and every `e` row generated FROM
it came back correctly right-facing. The reference carries IDENTITY; the row
prompt carries FACING; when they disagree the text usually wins. So a
wrong-facing reference does not doom the rows, and a right-looking turnaround
certifies nothing about them. **QA the ROWS.** It also means the repair is
row-side: reroll the row with the facing spelled out rather than chasing the
reference first.

### What `compose` checks, and what it cannot see

`validate_sheet` runs `detect_mirrored_art` on every compose. It asks a
structural question with an exact null hypothesis — *would flipping this row make
it fit better?* — on two independent neighbourhoods:

- **the rotation** — a row against its neighbours in the same state. It judges
  only rows with a neighbour on EACH side (`se`, `e`, `ne` on an 8-way sheet), so
  the front and back views are never judged, and a 4-way sheet gives it almost
  nothing.
- **the states** — the same direction across the other states. It needs THREE
  states before it can say anything, and it convicts only a strict MINORITY: an
  even split (four states, two against two — one `add-state` away) convicts
  nobody and reports *"the states split evenly"*. **Five states is the next
  number that cannot tie**, so a four-state character gets a WEAKER cross-state
  read than a three-state one whenever the mirror is even.

**ONE reading WARNS; TWO agreeing REFUSE** (§13.14). That is not a softening, it
is what the measurements force: on one basis the true and false populations
overlap — the quietest true reading on real art is **+6.8%**, and the loudest
FALSE one, correct art displaced sideways, is **+18.8%** — so no threshold
separates them. `MIRROR_GAIN_THRESHOLD = 0.08` decides whether a row is flagged
at all and decides nothing about warn-versus-refuse.

**A WHOLE mirrored STATE is an ERROR on ONE basis** (§13.18). A state mirrored in
its entirety is a fixed point of the rotation pass — flip every row of a state and
its chain still fits itself — so it can only ever reach one basis however wrong
it is, and waiting for a second read means waiting forever. The rule is *a
`states` finding covering EVERY cross-state-judged row of one state, at least two
of them*, and those findings carry `wholeState`: that state's flagged rows in
sheet order. **This is the shape `add-state` produces** — one batch, one
reference, one prompt, five rows drawn against a reference that turns the wrong
way. Suspect the state's REFERENCE and re-roll the STATE rather than one row of
it. One judged row reading clean means a contiguous block of bad rows, not a bad
state, and stays a warning.

**On the DEFAULT character neither refusal is reachable, and you should say so.**
`characters start` creates `idle:6, walk:8`; the cross-state pass needs three
states; so two states leave the rotation as the only reading there will ever be.
No finding can reach two bases and no whole-state consensus can exist — **the
whole-state error is the only thing that can catch a fully flipped state, and a
two-state character can never reach it**. Measured on a two-state cut of the live
art: a whole mirrored state scores **bit-identical** to the correct sheet, and two
adjacent mirrored rows pass with their gains going NEGATIVE, because a contiguous
block is only visible at its edges. **The cheapest sensitivity available is a
third state** — say that rather than quoting separation figures at the operator.

**And a clean pass is not a certificate.** Say the sentence, not the word "clean".
`compose` prints `handedness: N row(s) judged, M unjudged (…)` on the success path
AND inside the refusal — **read that line out**. Six of fifteen unjudged is the
normal state of an 8-way sheet, not a fault, but it is the difference between "the
check passed" and "the check passed on the nine rows it can see". Beyond the
unjudged rows it also cannot see a character mirrored on EVERY row (a sheet cannot
tell from inside itself which way is east), a character whose every state is
mirrored, or HOW FAR — it compares a row against its neighbours' mirrors, so a
rotation turned too far is still turned the right way. A single mirrored row can
ship clean outright: `jumping-se` mirrored reads **+6.78% rotation / +7.64%
states** and is caught by neither pass. Every miss measured so far is a `se`.

### Reading the refusal — three shapes, three meanings

`compose` emits a **block**: a headline that stands alone, then one
`label: value` line per fact, with no wrap width chosen on purpose. Do not reflow
it, do not compress it into a paragraph, and do not quote half of it — the
console's QA card lays those fields out one per row and shows the headline first.
It used to be a single 1206-character line; it is twelve lines now.

- **A REFUSAL** names a culprit and hands you the `reroll-row` command. It arrives
  in the two shapes above — two bases about one row, and a whole mirrored state —
  and they get two texts because they have two remedies.
- **A WARNING** names one row, says it does not block, and asks you to crop it and
  look. **Read every warning out to the operator.** Nothing else stands between a
  single-basis reading and a shipped mirrored row.
- **An UNATTRIBUTED finding names nobody**: *"one of N rows … and this pass cannot
  say which"*, ranked, with no `reroll-row` command. **Never re-roll off this
  shape**, and never re-roll a row listed under "Do NOT re-roll them".

The reason the rotation refuses to rank is measured, and it inverts twice. A
mirrored row pulls the seams of the rows on both sides of it toward the line, so
more rows read high than are wrong — and the loudest is not reliably the culprit.
A CORRECT row slid −24 px reads +9.38% while its untouched neighbour reads
+10.91%, so "the run's maximum" names the innocent one; and with two mirrored rows
FLANKING a correct one, the correct middle row wins the run at +14.28% while the
cross-state reading exonerating it (−97.62%) sits unused in the same payload.

**Obeying a wrongly-named row spends correct approved art.** `reroll-row` proposes
and approves unconditionally, there is **no `approve-row` verb** and none is
coming, so the approved pointer only ever moves forward. The refusal says this
now, on every branch that hands over a verb. Believe it.

### `--accept-handedness` — the door, and why it is the expensive one

`compose --accept-handedness <row>:<basis>` installs despite that row's finding,
keeps every other refused row refusing, and records `{row, gain, basis}` on the
installed manifest as `handednessAccepted` — republished by `characters list`, by
`sprite_payload`, and by the launcher's bundle warnings for the life of the
character (§13.19: the bundler records and warns; it does not refuse). Both
blocking shapes are overridable, **one token per row, never blanket**:

```
compose --accept-handedness jumping-se:states,jumping-e:states,jumping-ne:states
```

**Take the token from the refusal text.** It is derived per finding by
`pipeline.accept_basis_token`, and the refusal that demands the spelling and the
message that teaches it call that same function, so the two cannot drift. A bare
row name is refused (it would waive two independent bodies of evidence at once), a
row nothing flagged is refused, and a WARNING cannot be accepted at all because it
never blocked. `--accept-handedness idle-e` out of shell history does not work
even on a row that is genuinely refused.

**The costs run opposite to how they read, and this is the part to say out loud.**
A re-roll *sounds* like the careful option: it is private, silent, auto-approving,
and it has no undo. An acceptance *sounds* like the cheat: it is a permanent
public fact about the character that outlives the session and names you nowhere.
So the honest sequence is — crop the row, look at it WITH the operator, say what
you saw, and only then choose. Use the override only after looking; use a re-roll
only when the name is one you believe.

## Note craft

A note is appended to the generation prompt **and stored with the attempt** — it
is the durable QA record and the reason an attempt is reproducible. Phrase it as
a positive instruction about the art plus an explicit prohibition, scoped to every
frame. Never as a complaint about the artifact: "fix the line in row walk-n"
describes the operator's feeling and gives the model nothing to draw. Always show
the note back to the operator with the result.

Three notes proven live — start from these:

- Recovering a row the slicer rejected ("frame 4 contains multiple separated
  subjects"): *"Keep the character as ONE connected subject in every frame — no
  detached hair pieces, props, shadows or duplicate figures; each frame cell
  contains exactly one full character."*
- Removing a seam: *"Hair must be drawn as continuous unbroken strands from head
  to hair-tips in EVERY frame — absolutely no horizontal dark line or seam
  crossing the hair or body at any height."*
- **Fixing a facing when the reference pulls the other way — and spelling the
  target alone is NOT enough.** Measured on three rows: a note that spelled the
  target in frame terms landed ONE of three, and a more emphatic version of the
  same note ("ignore which way the reference is turned") failed again. What landed
  both remaining rows on the first try told the model what to DO with the
  reference rather than what to ignore: *"MIRROR THE REFERENCE'S TURN. The
  attached reference is turned the WRONG way for this row — copy its design,
  colours, hair and proportions, then draw it turned the OTHER way,"* plus one
  anatomical anchor that cannot be read two ways — *"her right ear is the only ear
  visible and it sits on the RIGHT-HAND SIDE of her head as you look at the
  picture."* Six generations for three rows. **"Ignore the reference" leaves the
  image there doing its work; "mirror it" gives the model an operation** — and
  anchor the side on a named body part, never on the direction token.

## Rendering contract

Three line shapes leave your reply, and two of them are parsed out of it.

- **`MEDIA:<absolute path>`** — on its own line, absolute, one path and nothing
  else on the line. The console lifts it into a hero image card.
  `.png .jpg .jpeg .gif .webp` render as images; any other file still parses and
  routes to the ordinary document lane. A trailing `.`, `,` or `;` is tolerated,
  and a line that is EXACTLY one inline-code span — `` `MEDIA:…` `` and nothing
  else — is unwrapped and still renders. Everything wider is prose on purpose: a
  **fenced** block never reaches the parser, a backtick span inside a sentence is
  a sentence, and two spans on one line fail the test. So write it bare, on its
  own line, and never retype a path into a paragraph — an inline path previews
  untitled and spends the message's small preview budget. Declare turnaround
  references, row crops and the composed sheet as they land.
- **`CHARSHEET-QA:{json}`** — one unfenced line of its own, upper case, emitted
  after `start` and after **every** stage change. The console keys the character's
  Studio project off it, so prose cannot substitute:
  `{"draft":"<id>","slug":"<slug>","displayName":"<name>","stage":"<stage>","generator":"<image provider>"}`
  plus `"item":"<row or direction key>","path":"<absolute path>"` when one image
  changed. Take `draft`/`slug`/`displayName`/`stage` from the verb's own payload —
  never retype them. `item` may be the bare key (`walk-n`) or the store's
  (`row@walk-n`); the bare form is what the flags take. `generator` names the
  IMAGE PROVIDER, and nothing in any payload sources it — pick one spelling and
  use the same one on every line of one draft, item-level lines included. Nothing
  keys on it; the DRAFT ID is the key, and project creation is idempotent on it,
  so emitting the line again for the same draft is safe by design. **The line
  carries no home and is not to grow one** (§13.22, which §13.24 explicitly left
  standing) — put the home your turn resolved in your prose instead. A parsed
  line is lifted out of the visible text; a malformed one is deliberately left
  in, because a stage change that vanished would be one the operator cannot see
  went wrong.
- **Clarify chips** — use the `clarify` tool with `choices` (up to 4) so the
  answers render as pickable rows directly under the card. Keep them verbs, not
  sentences: `Approve all` · `Reroll ne` · `Reroll with a note` · `Show another
  frame`. On this channel `clarify` does not block: it ends your turn and the
  answer arrives as the operator's next message.

**A chip is not a formatting preference — it is the operator's only
one-interaction answer.** Nothing refuses a prose question; the console simply
renders text where a one-click answer was the point. Live, two decision turns went
out as markdown bullet lists, the operator asked for pickable options, the next
turn called `clarify`, and the turn after that reverted to bullets. Reach for
`clarify` at every point you stop — including the turn where you are reporting a
blocker and offering ways out of it.

**A stage change with no `CHARSHEET-QA:` line is invisible downstream.** Live,
`reopen` moved a draft `composed → rows` with no line, so everything keying off
the last one still believed it was composed. `reopen` IS a stage change. Emit the
line for every stage that actually moved, and say in prose when a verb failed and
left the stage where it was.

**The operator trace truncates a command at 500 characters**, and a
`characters start` carrying a concept, a style and an absolute `--base-image` is
longer — it was cut mid-path in the console's own trace row. The trace is not a
record of what you ran. Put the draft id, the slug and the spec you chose in the
REPLY, where the operator can read them.

## The console acts without you

The QA card is not a report. The console mounts it with a dispatcher, and its
buttons call hermes directly: **approve-direction, reroll-direction, reroll-row,
reopen, compose** — five verbs, no agent turn involved. The console itself is a
shelf that stays open on other tabs, including Studio, so an authoring
conversation outlives leaving the Mission Control tab.

- **Re-read `status --json` at the start of a turn** rather than trusting your
  memory of the stage. A row may have been re-rolled, a direction approved, or a
  draft reopened or composed since you last spoke — and because a reroll
  auto-approves, the approved pointer may have moved without you.
- **The card's Compose carries no `--accept-handedness`.** Overriding a refusal is
  yours alone; it is the one thing the operator cannot do from the card. If they
  press Compose into a refusal they see the refusal block and nothing happens.
- **The character becomes a named Studio project on the first `CHARSHEET-QA:`
  line**, keyed by draft id, and that project — not a drafts listing — is what the
  operator later resumes from. The binding takes its authoring persona from
  `authored_by`, which is the whole reason probe 3 matters, and its home is minted
  UNKNOWN, because the line carries none (§13.22). The one thing that ever writes
  a home onto that binding is an operator opening Studio's adopt door, which
  stamps *a home the launcher OBSERVED the draft readable in* — the home that
  lane read back at that moment, and never the home you authored from (§13.24).
  The fold takes the FRESHEST sighting and an unknown never clobbers an observed
  one (§13.25), so the field is the launcher's most recent sighting and nothing
  more. A draft authored before projects existed is reachable only through that
  door (§13.23); one the console already adopted is offered it again whenever the
  fold would learn something, which is how a moved draft gets corrected.
- **What a resume hands you, and what it does not.** Resuming from the project
  seeds your first message with the draft id and one home line, spelled
  `last observed home: <path>` — or, when no sighting exists,
  `last observed home: never observed by the launcher`. That is the sighting
  above, and a sighting goes STALE: the draft may have been read from another
  home since, and the launcher fills nothing in when it has none. The seed says
  so in its own closing sentence:
  *"Echo the home you resolve; do not assume it."*
  So resume exactly the way you start: run the preflight probes, echo
  `.runtime_health.hermes_home` in prose, and if the draft is not in your list,
  say which home you are in and which home the seed named. Never quote the
  seeded home back as though you had resolved it, and never author a second copy
  because a seeded home disagreed with yours.

## Cost, batches, and what breaks

- **Say the cost before a sweep.** The default sheet (`idle:6, walk:8`, 8-way) is
  1 seed + 5 direction references + 10 row strips = **16 generations minimum**,
  each a real generation at roughly one to two minutes, plus rerolls (the live run
  took three extra). A `--directions 4` sheet authors THREE directions
  (`spec.FOUR_WAY.authored = ("s", "e", "n")`), so a one-state 4-way draft costs
  3 + 3 and its row keys are `idle-s`, `idle-e`, `idle-n` with nothing named
  `walk`. Read `spec.scheme.authored` out of `status --json` before quoting
  anything; the 8-way five is not a constant.
- **A failed row aborts the batch, and the survivors look untouched.** A strip the
  slicer rejects is retried three times internally, then `rows` stops. The rows
  that never ran read `attempts: 0` — indistinguishable in a status dump from "not
  started". Read the failure message (it names the row and the reason),
  `reroll-row` that row alone with a note, then resume with
  `rows --only <the rest>`. **Never re-run a bare `rows`** — it regenerates rows
  that already passed.
- **Rerolls are stochastic and row-grained.** There is no per-frame regeneration:
  the frames of a strip share an identity because they were drawn together, so one
  bad frame costs its whole row. One row took three strips. Budget two or three
  attempts on a bad row and say so up front — and look at every reroll before
  spending the next one, because an unexamined reroll silently becomes the sheet.
- **`compose` is slow, and that is normal.** `detect_mirrored_art` measured
  5.7–10.7 s on the live 15-row sheet and `validate_sheet` ~6.5 s end to end. Say
  so rather than letting the operator think it hung.
- **Re-composing is deterministic — but it is not free of stage.** `compose`
  re-runs from the approved strips, so `reopen → compose` on an installed
  character reproduces the sheet byte for byte and generates nothing. What it is
  not is inert: `reopen` has already moved the draft to `rows`, and a refusal
  leaves it there rather than where you found it. Say that before you run it on
  someone's shipped character.
- **`composed` is not terminal.** The post-install fix loop is
  `reopen → reroll-row → compose`; the post-install growth loop is
  `reopen → add-state → rows --only … → compose`. Both are non-destructive — the
  installed sheet stands until the next compose overwrites it — and you will need
  them more than once per character. An agent that treats `composed` as final
  tells the operator a fixable sheet is finished.

## If the verbs are missing, the session is restricted — not broken

The chat lane's default posture is `unbounded`, which short-circuits toolset
resolution to every registered toolset, `terminal` included
(`persona_runtime._enabled_toolsets_for_chat`). An operator who restricts a
session to `read_only` or `bounded` re-arms the cost policy's exclusion set and
`terminal` disappears — possibly mid-draft.

So when a verb cannot run, run one cheap verb to confirm, then say **"this session
is restricted"** and name the way back:
`agent_runtime.personas.<id>.chat_lane_restore_toolsets: [terminal]`. Do not
diagnose the pipeline, and never tell the operator the feature is broken.

## One good turn, in one line

Echo the home and pass the persona id → probe the provider → `start` → generate →
**declare a crop, not a sheet, and only when both flags are true** → ask with
clarify chips → reroll with an art-phrased note → look before you spend the next
one → `compose`, and read the handedness sentence out including what it could not
see → and when the operator spots something later, `reopen` without apology,
because the loop was built for exactly that.
