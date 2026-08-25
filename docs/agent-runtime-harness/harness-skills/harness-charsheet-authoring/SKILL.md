---
name: harness-charsheet-authoring
description: Author, QA and repair an 8-way character sheet from a chat turn — the staged `harness characters` verbs, the crop-and-compare looking procedure, reroll-note craft, and the MEDIA / CHARSHEET-QA lines the operator's console renders. Use whenever a turn is asked to make, fix, add a state to, or inspect a character sheet.
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

**The verb list was never the hard part.** This skill exists because a competent
operator with full repo access authored a character end to end, shipped it with a
one-pixel seam through the hair of two rows, and then spent most of the fix on the
wrong hypothesis. Everything below is the record of that run. The three things
that actually cost time: *looking* is a procedure and a glance is a reliable false
negative; the fix loop is stage-gated and costs whole generations; and a reroll
note is a drawing instruction, not a complaint.

`hermes` == `python -m hermes_cli.main`. Always `--json`. Never touch
`draft.json`, the revision store or the sheet with file tools — every state
change has a verb, and hand-editing the draft is the thing `reopen` was landed to
retire.

## Preflight — two probes, before any generation

**1. Which home am I authoring into?** The draft root is
`$HERMES_HOME/characters` (`characters_dir()`), drafts under
`.drafts/<draft-id>/`, installed sheets under `<slug>/`. Your turn's
`HERMES_HOME` is **your persona's own profile home** — the runtime binds it for
the duration of the turn from the persona's `hermes_profile`
(`profile_context.persona_profile_context`), so it is *not* necessarily the home
the console's `harness serve` process was launched with. Echo it in the first
turn:

```
hermes harness status --json      # → .runtime_health.hermes_home
hermes harness characters list --json
```

Three ways this goes wrong, all seen live:

- **A relative `HERMES_HOME` resolves against the shell's cwd.** `HERMES_HOME`
  is used as written (`hermes_constants._hermes_home_from_env`), and the CLI
  trusts any value whose immediate parent directory is named `profiles`. So
  `HERMES_HOME=profiles/base` run from a repo checkout authors *into the repo
  working tree*. A whole character sits in one today. Always absolute.
- **A home that is not profile-shaped is re-pointed at the sticky active
  profile.** If `HERMES_HOME` names the hermes root (or is unset), the CLI reads
  the `active_profile` marker and rewrites the home to that profile
  (`hermes_cli/main.py:_apply_profile_override`). Same command, two answers,
  depending on a file you did not set.
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
`image_gen` call before committing to a 16-generation run. If nothing comes back,
report "the image provider is unavailable in this home" and name the home —
never start the pipeline and discover it on generation twelve.

- **A `401 token_expired` right after a plan change is a STALE STORED TOKEN, not
  a missing credential.** An account upgrade invalidates the token already on
  disk, and it keeps a local expiry hours out — so the probe fails while
  `auth.json` looks perfectly healthy. This fires immediately after the operator
  did the right thing about the trap above, which makes it the most confusing
  one: sending them back to check `auth.json` placement is the correct answer
  for a home with no credentials and the wrong answer here. Force a refresh and
  re-probe **before** reporting the provider unavailable; the refreshed token
  reads the new plan.

## The verbs

Fourteen, flat, all with `--json`. Every draft verb takes `--draft <id>` as a
**required flag**; only `sprite` takes a positional `<slug>`. **The draft id is
not the slug** (`20260824-140756-cd645a` vs `anime-girl`).

| Verb | What it does | Stage it needs |
|---|---|---|
| `start --concept … [--slug] [--display-name] [--style] [--states] [--directions] [--base-image] [--authored-by]` | Creates the draft. Generates nothing. | — |
| `list` | Drafts + installed characters, with their directories. | — |
| `status --draft <id>` | Stage, spec, per-item QA history with every attempt's `path`. | any |
| `base --draft <id> --image <path>` | Sets/replaces the identity anchor. | any |
| `turnaround --draft <id>` | One generation per **authored** direction (`s se e ne n` for the 8-way scheme). | `turnaround` |
| `reroll-direction --draft <id> --direction <d> [--note …]` | Re-draws one direction reference. | `turnaround` |
| `approve-direction --draft <id> (--direction <d> [--attempt n] \| --all)` | Approving every authored direction advances to `rows`. | `turnaround` |
| `rows --draft <id> [--only a,b]` | One generation per **row strip** — never per frame. | `rows` |
| `reroll-row --draft <id> --row <key> [--note …]` | Re-draws one strip. **Auto-approved.** | `rows` |
| `thumb --draft <id> --row <key> [--attempt n] [--frame n] [--scale n]` | Writes a card-size QA crop of ONE frame. | **any** |
| `compose --draft <id> [--accept-handedness a,b]` | Composes, validates, installs; advances to `composed`. `--accept-handedness` overrides the mirrored-art refusal for the rows you NAME, after looking at them — never blanket, and a row nothing flagged is itself refused. | `rows` |
| `reopen --draft <id>` | Back to `rows` for fixes. Installed sheet untouched. | `composed` |
| `add-state --draft <id> --state <name>:<frames>[:fixed]` | Adds ONE state; seeds its rows un-generated, touches no approved row. | `rows` |
| `sprite <slug>` | The installed payload the launcher reads. | — |

**Adding a state to a character that is already installed** is
`reopen → add-state → rows --only <the new keys> → QA → compose`. `reopen` is the
only door: `add-state` refuses at any stage but `rows`, so an installed character
is reopened first. It touches no approved row and it never renumbers one — the
new state is appended, so the sheet grows downward and every row the old manifest
published keeps its index. **Take the new row keys from the verb's own answer**
(`rows` in the payload; the human line spells the whole `--only` list) rather
than composing them yourself: the count depends on the draft's own
`spec.scheme.authored`, and `--only` has no glob.

Frames are **2..8**. A one-frame state is refused by `add-state` and by
`start --states` at the moment you declare it — it used to be accepted and then
die at `rows`, several generations later, with no verb able to change it.

Stages run `turnaround → rows → composed`. An out-of-order verb refuses with a
flat `{"ok": false, "error": …, "stage": …}` and exit 2, and the error names the
stage order — read it instead of guessing.

**Attempt numbers are 0-based** in every payload and in `--attempt` (`-1` =
latest). Human-facing lines and the store's own filenames are 1-based
(`attempt-1.png` is attempt `0`). **A QA card relabels, never renumbers**: say
"attempt 3 of 3" and pass `--attempt 2`. Mirrored directions are never generated,
never composed and never QA items.

Pass `--authored-by <your persona id>` on `start`. It is provenance and it is
absent (`null`), not `""`, when you omit it.

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
  `<draft>/thumbs/<row>-attempt-<n+1>-frame-<f+1>-x<scale>.png`. Its payload is
  a **superset** — `{ok, draft, stage, row, attempt, attempts, frame, frames,
  scale, source, path, width, height, cardSafe}` today, and it grows additively,
  so read the response you were handed rather than the list you remember.
  `source` is the attempt file the crop came from, the same string
  `status --json` reports as that attempt's `path`.
- **`--frame 0` is a default, not an answer.** A defect is hunted frame by frame;
  walk `--frame` across `frames`.
- `--scale` (default 2) is bounded by **output**-pixel budgets and is
  **refused, never clamped** — the refusal names the source dimensions. There
  are two bounds: at or below the default the crop must clear the card budget
  (`pipeline.MAX_CARD_PIXELS`), and a deeper zoom is allowed but comes back
  **`cardSafe: false`**.
- **That budget is a FIXED console ceiling. It is not a comparison against YOUR
  sheet, and it never was.** `MAX_CARD_PIXELS` is a module constant sized from
  `CHAR8` — 1536×2080 = 3,194,880 px — and it does not move with a draft.
  Measured at both ends:
  - **A grown sheet.** `add-state --state jumping:6` recomposed the live
    `anime-girl` sheet at 1536×3120 = 4,792,320 px, **1.50× the budget**. On
    such a draft the default scale can be REFUSED for a crop that is genuinely
    lighter than the sheet it will compose — so read the refusal as "over the
    console ceiling", not as "heavier than your sheet".
  - **A small sheet.** A `--directions 4`, `idle:2` draft composes 384×624 =
    239,616 px, and its DEFAULT crop came back `cardSafe: true` at **13.1× that
    whole sheet**.
  So take your own size from `status --json` →
  `.status.spec.sheetWidth` × `.status.spec.sheetHeight` and weigh the crop
  against it yourself. Never carry a copy of the threshold.
- **Only declare a `cardSafe: true` crop with `MEDIA:`** — a false one is a
  fullscreen-viewer artifact and hands the console a decode far past the
  ceiling for a 420px square. Say the path and tell the operator to open it
  instead. `cardSafe: true` is necessary, not sufficient: it says the file will
  not sink the console, not that cropping bought you anything.
- **The single most reliable read is attempt N beside attempt N−1**, same row,
  same frame, declared in one turn and labelled. Two independent agents each
  failed to see this seam at 5–6× magnification on a single frame; side by side,
  both saw it immediately.
- **Do not build a pass/fail scanner** *for defects in the art* — seams, stray
  pixels, anatomy. A refined "opaque dark pixels with light pixels above"
  predicate flagged all ten rows including the two known-good rerolled ones
  (18–85 hits each), and a "sharpest dark row" rank put the sock band and the
  skirt hem — real art, present in the clean strip too — above the actual seam.
  A scan is *relative triage* at best: rank rows, then look. The operator's eye
  is the gate.
  **One measurement is not that, and it is already built: handedness.** It asks
  a structural question with an exact null hypothesis — *would flipping this row
  make it fit the rotation better?* — instead of guessing what a defect looks
  like, and `compose` refuses on it (see below). Do not write a second one.
- **Default hypothesis: the model drew it.** Pipeline residue (slicing, keying,
  the palette lock) was the first guess and cost the most; the defect was in the
  generated art and a reroll fixed it with zero pipeline change.

**Card geometry, so you declare crops knowing it.** The console's MEDIA hero card
is a fixed **1:1 centre-cover square** — `AspectRatio(kInlineImageUnknownAspectRatio)`
with `BoxFit.cover`, tile 420–720 px — and it does **not** read the file's real
dimensions. A tall crop is judged on its middle square; anything outside that is
only seen when the operator taps the card open (the fullscreen viewer zooms to
8×). So crop the frame you are hunting in, say where in the frame the defect
sits, and tell the operator to open the card when it sits off-centre.

## Note craft

A note is appended to the generation prompt **and stored with the attempt** — it
is the durable QA record and the reason an attempt is reproducible. Phrase it as
a positive instruction about the art plus an explicit prohibition, scoped to
every frame. Never as a complaint about the artifact: "fix the line in row
walk-n" describes the operator's feeling and gives the model nothing to draw.

Two notes proven live — start from these:

- Recovering a row the slicer rejected ("frame 4 contains multiple separated
  subjects"): *"Keep the character as ONE connected subject in every frame — no
  detached hair pieces, props, shadows or duplicate figures; each frame cell
  contains exactly one full character."*
- Removing a seam: *"Hair must be drawn as continuous unbroken strands from head
  to hair-tips in EVERY frame — absolutely no horizontal dark line or seam
  crossing the hair or body at any height."*

Always show the note back to the operator with the result.

## Talking to the operator

Three line shapes leave your reply, and two of them are parsed.

- **`MEDIA:<absolute path>`** — unfenced, on its own line, absolute. The console
  renders it as a hero image card. Wrapped in backticks or a fence it is
  *un-declared* and nothing renders; retyped into a sentence it previews
  untitled and steals the message's small preview budget. Declare turnaround
  references, row crops and the composed sheet as they land. `.png` and `.webp`
  both render.
- **`CHARSHEET-QA:{json}`** — one unfenced line of its own, emitted after `start`
  and after **every** stage change. The launcher keys the character's project off
  it, so prose cannot substitute:
  `{"draft":"<id>","slug":"<slug>","displayName":"<name>","stage":"<stage>","generator":"<image provider>"}`
  plus `"item":"<row or direction key>","path":"<absolute path>"` when one image
  changed. Take `draft`/`slug`/`displayName`/`stage` from the verb's own payload
  — never retype them.
- **Clarify chips** — use the `clarify` tool with `choices` (up to 4) so the
  answers render as pickable rows directly under the card. Keep them verbs, not
  sentences: `Approve all` · `Reroll ne` · `Reroll with a note` · `Show another
  frame`. On this channel `clarify` does not block: it ends your turn and the
  answer arrives as the operator's next message.

## Cost, batches, and what breaks

- **Say the cost before a sweep.** The default sheet (`idle:6, walk:8`, 8-way) is
  1 seed + 5 direction references + 10 row strips = **16 generations minimum**,
  each a real generation at roughly one to two minutes, plus rerolls (the live
  run took three extra). Each added state costs ONE strip per authored
  direction — 5 on that 8-way sheet, **3 on a `--directions 4` sheet**. Read
  `spec.scheme.authored` out of `status --json` before quoting it; the 8-way
  five is not a constant.
- **A failed row aborts the batch, and the survivors look untouched.** A strip
  the slicer rejects is retried three times internally, then `rows` stops. The
  rows that never ran read `attempts: 0` — indistinguishable in a status dump
  from "not started". Read the failure message (it names the row and the reason),
  `reroll-row` that row alone with a note, then resume with
  `rows --only <the rest>`. **Never re-run a bare `rows`** — it regenerates rows
  that already passed.
- **A reroll is auto-approved, and it is a ONE-WAY door.** `reroll_row` ends with
  `propose` then `approve` unconditionally, and there is **no `approve-row`
  method and no `approve-row` verb** — `approve-direction` is for turnaround
  references only. So a reroll that comes back WORSE than the attempt it replaced
  cannot be un-approved; the only way back is another reroll that happens to be
  good. Crop and look BEFORE you spend the next one, and tell the operator the
  pointer moves whether or not the new attempt is better. (Nothing is lost:
  earlier attempts and their notes stay in `state.json` and `--attempt n` still
  renders them. It is the *approved* pointer that only moves forward.) This is
  why a refusal naming several rows is dangerous to obey literally — see the
  compose bullet below.
- **Rerolls are stochastic and row-grained.** There is no per-frame
  regeneration: the frames of a strip share an identity because they were drawn
  together, so one bad frame costs its whole row. Budget two or three attempts on
  a bad row and tell the operator that up front.
- **`compose` refuses a row drawn as the MIRROR of the direction it claims**, by
  name, with its numbers. Only the AUTHORED directions are drawn, and the
  consumer builds the other three by flipping them, so a mirrored row corrupts
  two directions at once — this happened: `anime-girl`'s `ne` was drawn facing
  north-WEST in `idle`, `walk` and later `jumping`, which broke `ne` AND `nw`
  while the other six stayed right, and nothing caught it until a human saw the
  character in a 3D scene. The repair is a `reroll-row` with the facing spelled
  in FRAME terms ("the body angled up and to the RIGHT of frame; the sliver of
  face on the viewer's RIGHT; never the mirror of this") — not a nudge, and never
  a hand-flip of the sheet, which the next compose overwrites.
  **Re-roll ONLY the rows the refusal names as faults.** A mirrored row pulls the
  seams of the rows on BOTH sides of it toward the line, so more rows read high
  than are wrong. The refusal reports the culprit and lists the others as
  corroborating, in the same message, with the words "Do NOT re-roll them" —
  obey that literally, because a reroll auto-approves and cannot be undone, so
  re-rolling a corroborating row spends correct approved art for nothing.
  **What it does NOT see, so you still look.** It judges a row only when it has a
  neighbour on each side (`se`, `e`, `ne` on an 8-way sheet — the front and back
  views are excluded, and every row it could not answer for is named in
  `handedness.unjudged` and counted in the sentence `compose` prints). A 4-way
  sheet gives it almost nothing. A character mirrored on EVERY row passes
  perfectly, because a sheet cannot tell from inside itself which way is east —
  and so does a character whose every STATE is mirrored. It answers WHICH SIDE,
  never HOW FAR: a rotation turned too far is still turned the right way.
- **The same check reads a whole STATE across the others**, which is the pass
  that catches `add-state` generating five rows against a reference that turns
  the wrong way. It needs THREE states before it can say anything (across one
  pair, a disagreement cannot say which of the two is wrong), so the default
  `idle`+`walk` sheet gets nothing from it and the third state is where it wakes
  up. When it fires, the refusal names each row of that state with basis
  `states` — that is the one case where re-rolling several rows IS right, and the
  reference is worth suspecting before the rows are.
- **A refusal you believe is wrong has exactly one door, and it names rows.**
  `compose --accept-handedness idle-e,walk-ne` installs despite those rows'
  findings, records them on the character's manifest as `handednessAccepted`, and
  keeps every other flagged row refusing. Naming a row that was NOT flagged is
  itself an error, so the flag cannot be carried along in a command line as
  boilerplate. Use it only after looking at the strip with the operator and
  saying what you saw — the check separates its two populations by about 2.5x on
  the two characters ever measured, which makes it a strong signal and not a
  proof.
- **`composed` is not terminal.** The post-install fix loop is
  `reopen → reroll-row → compose`, and the post-install GROWTH loop is
  `reopen → add-state → rows --only … → compose`. Both are non-destructive — the
  installed sheet stands until the next compose overwrites it — and you will need
  them more than once per character. An agent that treats `composed` as final
  tells the operator a fixable sheet is finished.
- **A state is added, never removed.** There is no remove verb and none is
  planned as a flag: dropping a state would delete approved attempts and the
  operator notes stored with them, which is the durable QA record. If the
  operator wants a state gone, say that plainly.
- **An authored state is not automatically a reachable one.** A row the runtime
  never requests is installed and silent. Say so when the operator names a state
  outside the runtime's motion vocabulary.

## If the verbs are missing, the session is restricted — not broken

The chat lane's default posture is `unbounded`, which short-circuits toolset
resolution to every registered toolset, `terminal` included
(`persona_runtime._enabled_toolsets_for_chat`). An operator who restricts a
session to `read_only` or `bounded` re-arms the cost policy's exclusion set and
`terminal` disappears — possibly mid-draft.

So when a verb cannot run, run one cheap verb to confirm, then say **"this
session is restricted"** and name the way back:
`agent_runtime.personas.<id>.chat_lane_restore_toolsets: [terminal]`. Do not
diagnose the pipeline, and never tell the operator the feature is broken.

## One good turn, in one line

Preflight the home and the provider → `start` → generate → **declare a crop, not
a sheet** → ask with clarify chips → reroll with an art-phrased note →
re-inspect → `compose` → and when the operator spots something later, `reopen`
without apology, because the loop was built for exactly that.
