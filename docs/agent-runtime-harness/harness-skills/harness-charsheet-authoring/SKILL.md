---
name: harness-charsheet-authoring
description: Author, QA and repair an 8-way character sheet from a chat turn — the staged `harness characters` verbs, the crop-and-compare looking procedure, the handedness gate and how to read its refusal, reroll-note craft, and the operator-observability report contract (the MEDIA / CHARSHEET-QA lines the console renders into live cards — a turn that advanced a draft MUST end with them; prose-only reports are a failed turn). Use whenever a turn is asked to make, fix, add a state to, resume or inspect a character sheet.
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

## Turn zero — this is the whole standard path

- **This skill is in your context.** Never re-read `SKILL.md` or
  `FIELD-NOTES.md` from disk, and never run `--help` on a `characters` verb —
  the verb table below is complete, flags included, and each verb's payload
  carries a `"next"` object naming the follow-on command; read it instead of
  reasoning about what comes next.
- **This is not a repo task.** No `CLAUDE.md`, no `git status`, no file
  searches, no `pwd`/`ls` orientation. Nothing in an authoring ask names a file
  in a checkout.
- **The entrypoint is `hermes`.** Bare, on every command.
- **The runtime already rebound `HERMES_HOME` to your persona's own profile
  home.** Never `export` it and never prefix a command with it. The character
  library is install-wide — one `<hermes_root>/shared/characters` for every
  persona and every profile (§13.27) — so the profile does not scope it and
  every draft on this install is in your list.
- **Preflight is ONE command pair, run ONCE:**

  ```
  hermes harness status --json      # → .runtime_health.hermes_home
  hermes harness characters list --json
  ```

  **Echo that home in your reply, in prose** — nothing else carries it, so your
  transcript is the only place the home your turn resolved will ever exist. Do
  not re-run the pair to reassure yourself. If a draft the operator names is not
  in the list, the draft does not exist on this install or your ROOT is wrong,
  and those are the only two possibilities left
  (`references/homes-and-migration.md`).
- **Spend one cheap `image_generate` call before committing to a 16-generation
  run.** A plan-gated account fails politely: HTTP 200, the tool silently
  stripped, a model that says it has no such tool. Never discover it on
  generation twelve.
- **Pass `--authored-by` verbatim** from your own identity clause — the Mission
  Control persona id inside the parenthesis, never a slug of your display name.
  A draft with no resolvable `authored_by` refuses to resume at all.
- **Wait cadence.** One generation is 1–2 minutes; a full `rows` batch is 10–20.
  Fire the batch with `terminal(background=true)`, call `process` with
  `action: "notify"` and the returned `session_id`, and **end your turn** — a
  new turn arrives when the process exits, opening with
  `[BACKGROUND PROCESS COMPLETE — …]`; it waits for your thread to go idle, and
  a duplicate or post-exit notify is safe. If notify answers
  `status: "unavailable"`, fall back to blocking: pass `timeout: 600` and never
  less — every expiry is a full API round-trip that re-sends the whole prompt.
  Between waits do QA that is already available — thumb the rows that already
  landed — rather than polling empty-handed. When the operator asked for the
  WHOLE thing in one go, `characters auto` is that same shape as one process:
  fire it in the background, `notify`, end your turn, and report every receipt
  line it printed.
- **Batch: many verbs, one tool call.** Ten thumbs in one command —

  ```
  for f in 1 2 3 4 5 6 7 8; do hermes harness characters thumb --draft <id> --row walk-n --frame $f --json; done
  ```

  — and chain a stage boundary you have already decided:
  `... approve-direction --draft <id> --all --json && ... rows --draft <id> --json`.
- **Never pipe a bare `sprite --json` into a turn** — it inlines the whole sheet
  as base64 (468.8 KiB live), past the tool output cap, and truncation makes it
  unparseable. Add **`--no-sheet`** when you want the shape: it drops
  `spritesheetBase64`, carries `sheet` (the absolute path) in its place, keeps
  `spritesheetRevision` and every geometry/taxonomy key, and is small enough to
  read in a turn. `character.json` and `status --json` still answer the same
  question offline.

**Where the rulings live.** Citations of the form **§13.n** are owner decisions
recorded once, in the launcher companion
`docs/mission_control/planned/console-character-authoring-architecture.md` §13.
This file carries the operative half only and never restates the reasoning — if
the two ever disagree, the register is right.

Always `--json`. Never touch
`draft.json`, the revision store or the sheet with file tools — every state
change has a verb, hand-editing the draft is what `reopen` was landed to
retire, and the next `compose` overwrites any hand edit to the sheet anyway.

## The verbs

Seventeen, flat, all with `--json`. Every draft verb takes `--draft <id>` as a
**required flag**; only `sprite` takes a positional `<slug>`. **The draft id is
not the slug** (`20260824-140756-cd645a` vs `anime-girl`).

| Verb | What it does | Stage it needs |
|---|---|---|
| `start --concept … [--slug] [--display-name] [--style] [--states] [--directions] [--base-image] [--authored-by]` | Creates the draft. Generates nothing. | — |
| `list` | Drafts + installed characters, with their directories. | — |
| `backfill-home` | Records `hermes_home` — provenance of the authoring RUN, not an address — on library drafts that carry none, using the home THIS run resolved, and on no others. An already-recorded home is never rewritten; `updated` is left untouched. Idempotent, receipted. **Operator-run — do not fire it as part of an authoring flow.** | — |
| `migrate-home` | Moves THIS home's legacy `<HERMES_HOME>/characters` store into the install-wide library. Drafts keep their directory leaf names and installed characters keep their slugs; a draft carrying no `hermes_home` is stamped with the SOURCE home before it moves. A destination collision is a per-entry refusal, never an overwrite, and nothing is deleted. Idempotent, receipted. **Operator-run — do not fire it as part of an authoring flow.** | — |
| `payload-contract` | Prints the character PAYLOAD key set hermes publishes, measured from the producers (`--json` for the machine door the launcher's wire-contract gate reads). Read-only; touches no draft and no library. **Tooling — not an authoring verb; never fire it in an authoring flow.** | — |
| `status --draft <id>` | Stage, spec, per-item QA history with every attempt's `path`. | any |
| `base --draft <id> --image <path>` | Sets/replaces the identity anchor. | any |
| `turnaround --draft <id>` | One generation per **authored** direction (`s se e ne n` for the 8-way scheme). | `turnaround` |
| `reroll-direction --draft <id> --direction <d> [--note …]` | Re-draws one direction reference. | `turnaround` |
| `approve-direction --draft <id> (--direction <d> [--attempt n] \| --all)` | Approving every authored direction advances to `rows`. **The last moment a reference can be changed.** | `turnaround` |
| `rows --draft <id> [--only a,b]` | One generation per **row strip** — never per frame. | `rows` |
| `reroll-row --draft <id> --row <key> [--note …]` | Re-draws one strip. **Auto-approved, and there is no undo.** | `rows` |
| `thumb --draft <id> (--row <key> [--frame n] \| --direction <d>) [--attempt n] [--scale n]` | Writes a card-size QA crop of ONE frame of a row, or of ONE turnaround direction REFERENCE. Both arms carry the same two budget booleans; a reference is one pose, so `--frame` does not apply to it and is refused rather than ignored. | **any** |
| `compose --draft <id> [--accept-handedness <row>:<basis>,…]` | Composes, validates, installs; advances to `composed`. | `rows` |
| `auto --draft <id> [--through turnaround\|approve-direction\|rows\|compose]` | **The whole pipeline in ONE process**: turnaround → approve every direction → generate the rows that are missing → compose and install, printing a receipt as each stage lands (`--json` = one compact object per LINE; the last line is always the summary). **Only for an operator's explicit "drive it all the way" ask** — it auto-approves the turnaround, which is the last moment a reference can change. It stops on a handedness refusal and has no way to override one; it resumes rather than restarts (a stage whose work already exists is skipped, with the reason on the summary), so it is also the one-command repair after `reopen`; and it writes the same per-attempt history, so crops and `reopen` behave exactly as after a hand-driven run. **One command is still many stage changes: emit a `CHARSHEET-QA:` line for every receipt line it printed**, plus the `MEDIA:` lines. | `turnaround` or `rows` |
| `reopen --draft <id>` | Back to `rows` for fixes. Installed sheet untouched. | `composed` |
| `add-state --draft <id> --state <name>:<frames>[:fixed]` | Adds ONE state; seeds its rows un-generated, touches no approved attempt. | `rows` |
| `sprite <slug> [--no-sheet]` | The installed payload the launcher reads. **Never pipe the bare form into a turn** — see above. `--no-sheet` is the metadata-only shape: no `spritesheetBase64`, plus `sheet` (the absolute path) so you can read the bytes yourself. | — |

## The reply is the operator's only window

The console shows the operator your tool rows while you work — commands, waits,
pass/fail chips — and nothing else. Every generated image, every stage change,
every QA verdict is invisible until your reply lands, and your reply is the ONLY
thing that carries them. Measured live 2026-08-28: an ~18-minute one-message
full run that ended in a prose-only reply read as *frozen* to the operator
mid-turn and as *finished with nothing to show* at the end — the pipeline had
succeeded and the report still failed. **The report shape is the deliverable,
not polish.** A turn that advanced a draft ends with the visual story of every
stage it completed:

- **`CHARSHEET-QA:{json}`** — one unfenced line of its own, upper case, emitted
  after `start` and after **every** stage change (`reopen` IS a stage change).
  The console keys the character's Studio project off it, so prose cannot
  substitute and a stage change with no line is invisible downstream.

  `{"draft":"<id>","slug":"<slug>","displayName":"<name>","stage":"<stage>","generator":"<image provider>"}`

  plus `"item":"<row or direction key>","path":"<absolute path>"` when one image
  changed. **`path` uses FORWARD slashes** (`X:/Eternia/...`) — measured live
  2026-08-29: hand-escaped backslash paths in this JSON double-escape under
  pressure and the console's text formatter mangles `\t` sequences, so the line
  fails to parse and dumps as a raw text wall. Forward slashes have no escape
  hazard in JSON and Windows accepts them. Take `draft`/`slug`/`displayName`/
  `stage` from the verb's own payload — never retype them. `generator` names the
  IMAGE PROVIDER, which no payload sources: pick one spelling and keep it for
  the whole draft. Re-emitting for the same draft is safe — project creation is
  idempotent on the draft id. **The line carries no home and is not to grow
  one** (§13.22) — the home goes in your prose.
- **An action turn replies SCOPED.** When the operator's message is a single
  card action — reroll one row, reopen, compose, one direction's verb — the
  reply carries ONLY the affected item's `CHARSHEET-QA:` line and its `MEDIA:`
  line(s) (plus the installed sheet if the action composed). Never re-dump the
  full media wall the completion report already delivered: measured live
  2026-08-29, two reroll clicks re-sent the entire 14-image wall twice and the
  operator read it as the console malfunctioning. The full wall belongs to the
  run-completion reply, once.
- **`MEDIA:<absolute path>`** — on its own line, absolute, one path and nothing
  else on the line. A fenced block never reaches the parser and a path retyped
  into a paragraph previews untitled, so write it bare. One line for each thing
  a stage produced: the base image; each approved turnaround direction; each
  row's first-frame thumb (the `thumbs/` path read off `status --json`, **never
  a rebuilt path**); and, when you composed, the installed `sheet.webp` as the
  LAST media line, so the finished character is the card the operator sees
  first. Ten row thumbs is not too many cards; **zero is a bug.** For hero-card
  thumbs pass `--square` — the console card is a 1:1 centre-cover square, and a
  bare tall crop shows only its middle band; keep bare crops for compare pairs,
  which assume today's shapes.
- **Declare a crop only when BOTH `thumb` booleans are true.** They answer
  different questions and disagree in both directions: `withinConsoleBudget`
  (will this file sink the console — the only one that refuses anything) and
  `withinOwnSheet` (did cropping buy anything). Never infer one from the other,
  never carry a copy of either threshold, and say WHICH bound was missed —
  over the console ceiling is an unsafe decode, over your own sheet is a safe
  picture that mitigated nothing.
- **A QA finding travels with its picture.** A handedness warning — blocking or
  not — names the row, attaches the flagged item's thumb or crop as `MEDIA:`,
  and states the operator's next verbs. "One non-blocking warning on walk-e,
  recorded for review" with no image gives the operator nothing to review.
- **Clarify chips at every point you stop**, including the turn where you report
  a blocker. Use the `clarify` tool with `choices` (up to 4), kept as verbs, not
  sentences: `Approve all` · `Reroll ne` · `Reroll with a note` · `Show another
  frame`. A chip is not a formatting preference — it is the operator's only
  one-interaction answer, and nothing refuses a prose question; the console
  simply renders text where one click was the point. On this channel `clarify`
  does not block: it ends your turn and the answer arrives as the next message.
- **Put the draft id, the slug and the spec you chose in the REPLY.** The
  operator trace truncates a command at 500 characters — it is not a record of
  what you ran.

**Prefer a stage per turn when the operator is present.** Each stage boundary
then renders its cards immediately and the operator can redirect before the next
stage builds on it. Drive stages back-to-back in one turn only when asked for a
one-shot run — and then the closing reply must reconstruct ALL of it.

## Reading a refusal, and the handedness gate

Stages run `turnaround → rows → composed`. An out-of-order verb refuses with a
flat `{"ok": false, "error": …, "stage": …}` and exit 2, and the error names the
stage order — read it instead of guessing. Refusals are actionable text you hand
the operator verbatim; if one names a flag the verb does not accept, report it as
written rather than translating it into the flag you think was meant.

`compose` runs `detect_mirrored_art` on every compose and emits a **block**: a
headline that stands alone, then one `label: value` line per fact. Do not reflow
it, do not compress it into a paragraph, and do not quote half of it — the
console's QA card lays those fields out one per row.

- **ONE reading WARNS; TWO agreeing REFUSE** (§13.14). Not a softening: on a
  single basis the true and false populations overlap, so no threshold separates
  them.
- **A WHOLE mirrored STATE is an ERROR on ONE basis** (§13.18) — a fully
  mirrored state is a fixed point of the rotation pass and can never reach a
  second reading however wrong it is. Those findings carry `wholeState`. It is
  the shape `add-state` produces, so suspect the state's REFERENCE and re-roll
  the STATE, not one row of it.
- **A WARNING names one row and does not block — read every warning out to the
  operator**, with the row's crop. Nothing else stands between a single-basis
  reading and a shipped mirrored row.
- **An UNATTRIBUTED finding names nobody** — *"one of N rows … and this pass
  cannot say which"*, ranked, with no `reroll-row` command. **Never re-roll off
  this shape**, and never re-roll a row listed under "Do NOT re-roll them". The
  rotation's loudest row is measurably not reliably the culprit; a correct row
  can win the run while the reading that exonerates it sits in the same payload.
- **Obeying a wrongly-named row spends correct approved art.** `reroll-row`
  proposes and approves unconditionally, there is **no `approve-row` verb**, and
  the approved pointer only ever moves forward.
- **Read the handedness sentence out, including what it could not see.**
  `compose` prints `handedness: N row(s) judged, M unjudged (…)` on the success
  path AND inside the refusal. Six of fifteen unjudged is the normal state of an
  8-way sheet, not a fault — but it is the difference between "the check passed"
  and "the check passed on the nine rows it can see". **A clean pass is not a
  certificate**; say the sentence, not the word "clean".
- **On the DEFAULT two-state character neither refusal is reachable — say so.**
  The cross-state pass needs three states, so the rotation is the only reading
  there will ever be, and a fully mirrored state is invisible. The cheapest
  sensitivity available is a third state.
- **`--accept-handedness <row>:<basis>` is one token per row, never blanket**,
  and **the token comes out of the refusal text** — the refusal and the message
  that teaches the spelling both call `pipeline.accept_basis_token`, so they
  cannot drift. A bare row name is refused (it would waive two independent bodies
  of evidence at once), a row nothing flagged is refused, and a WARNING cannot be
  accepted at all because it never blocked.
- **The costs run opposite to how they read.** A re-roll sounds careful and is
  private, silent, auto-approving and undoable-never; an acceptance sounds like
  the cheat and is a permanent public fact recorded on the manifest as
  `handednessAccepted`, republished by `list`, by `sprite_payload` and by the
  launcher's bundle warnings for the life of the character (§13.19). So crop the
  row, look at it WITH the operator, say what you saw, and only then choose.
- **QA the ROWS, not the reference.** A wrong-facing approved reference does not
  doom the rows drawn from it — the reference carries identity, the row prompt
  carries facing, and when they disagree the text usually wins — and a
  right-looking turnaround certifies nothing about the rows. The repair is
  row-side.
- **Approving the turnaround is the last moment a reference can change.**
  `reopen` goes `composed → rows` and nothing goes back to `turnaround`, so QA
  that strip hard before `approve-direction --all`.
- **Write facing in FRAME terms, three ways at once** — which way the body points
  in frame, which shoulder is nearer, which side a sliver of face may appear on —
  plus an explicit refusal of the mirror. "The viewer's right" is ambiguous on a
  back view; "turn her the other way" and "face north-east" leave the same coin
  flip that shipped a mirrored `ne`.
- **When an operator reports two opposite directions wrong and the other six
  right, suspect ONE authored row and its mirror**, not the consumer. Only the
  authored directions are drawn; the consumer flips them, so one bad row breaks
  exactly two. A consumer bug rarely spares six.
- **If a verb cannot run, the session is restricted — not broken.** Confirm with
  one cheap verb, then say **"this session is restricted"** and name the way
  back: `agent_runtime.personas.<id>.chat_lane_restore_toolsets: [terminal]`. Do
  not diagnose the pipeline, and never tell the operator the feature is broken.

## Between your turns, the console acts without you

The QA card's buttons call hermes directly — **approve-direction,
reroll-direction, reroll-row, reopen, compose** — with no agent turn involved.
So **re-read `status --json` at the start of a turn** rather than trusting your
memory of the stage: a row may have been re-rolled, a direction approved, or the
draft reopened or composed since you last spoke, and because a reroll
auto-approves, the approved pointer may have moved without you. The card's
Compose carries no `--accept-handedness`; overriding a refusal is yours alone.

## The references — open the one that matches, and only that one

These files sit beside this one and are NOT in your context. Read the matching
one from disk when its trigger fires; never read `SKILL.md` or `FIELD-NOTES.md`.

- **`references/payloads-and-growth.md`** — attempt numbering, the maps
  `status --json` keeps under `.status`, the duplicate-draft-id hazard in `list`,
  and the whole `reopen → add-state → rows --only` growth loop with the
  whole-sheet rescale an added state causes. **Open before reading a value out of
  a payload you are not certain of, and before any `add-state`.**
- **`references/looking-procedure.md`** — crop one frame, compare don't zoom, the
  `--scale` arithmetic that makes a default crop BIGGER than its strip, the two
  weight flags in full, the hero card's 1:1 geometry, and why a pass/fail art
  scanner is ruled against. **Open before you judge the first image of a draft.**
- **`references/handedness.md`** — the measurements behind the rules above: what
  each basis can and cannot see, and why the rotation refuses to rank. **Open
  when a refusal or warning names handedness, or when you are choosing between a
  re-roll and an acceptance.**
- **`references/note-craft.md`** — the three reroll notes proven live, including
  the "MIRROR THE REFERENCE'S TURN" phrasing that landed a facing fix after two
  failures. **Open before writing any `--note`.**
- **`references/homes-and-migration.md`** — ROOT and home resolution in full, the
  relative-`HERMES_HOME` second-library trap, the `401 token_expired` stale-token
  trap on the provider probe, and the operator-only `backfill-home` /
  `migrate-home` verbs. **Open when a named draft is missing from your list, when
  a probe fails, or when asked to migrate.**
- **`references/console-and-costs.md`** — the rendering contract in full with its
  parser edge cases, what a resume seeds you with, generation counts and 4-way
  arithmetic, batch-failure recovery, and compose timings. **Open when the
  contract above did not answer a line-shape question, or before quoting a cost.**

## One good turn, in one line

Echo the home and pass the persona id → probe the provider → `start` → generate →
**declare a crop, not a sheet, and only when both flags are true** → ask with
clarify chips → reroll with an art-phrased note → look before you spend the next
one → `compose`, and read the handedness sentence out including what it could not
see → and when the operator spots something later, `reopen` without apology,
because the loop was built for exactly that.
