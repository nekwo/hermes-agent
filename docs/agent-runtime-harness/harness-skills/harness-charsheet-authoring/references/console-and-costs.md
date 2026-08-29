# The console, the full rendering contract, and what a run costs

**When to open:** the compressed reply contract in `SKILL.md` did not answer a
question about a line shape or a parser edge case; the operator acted on the QA
card between your turns; you are about to quote a cost or a batch size; a verb
is missing from the session. `SKILL.md` carries the shapes you emit every turn —
this is the long form and the reasons.

## The reply is the operator's only window

The console shows the operator your tool rows while you work — commands, waits,
pass/fail chips — and nothing else. Every generated image, every stage change,
every QA verdict is invisible until your reply lands, and your reply is the ONLY
thing that carries them. Measured live 2026-08-28: a one-message full run
(base → install, ~18 minutes) that ended in a prose-only reply read as *frozen*
to the operator mid-turn and as *finished with nothing to show* at the end — the
pipeline had succeeded and the report still failed. So the report shape is not
optional polish; it is the deliverable:

- **A turn that advanced a draft ends with the visual story of every stage it
  completed.** One `CHARSHEET-QA:` line per stage change (the shape below) AND
  `MEDIA:` lines for what each stage produced: the base image; each approved
  turnaround direction; each row's first-frame thumb (`thumbs/` path off
  `status --json` — never a rebuilt path); and, when you composed, the installed
  `sheet.webp` as the LAST media line, so the finished character is the card the
  operator sees first. Ten row thumbs is not too many cards; zero is a bug.
- **A QA finding travels with its picture.** A handedness warning — blocking or
  not — names the row, attaches the flagged item's thumb or crop as `MEDIA:`,
  and states the operator's next verbs (`approve`, reroll with a note). "One
  non-blocking warning on walk-e, recorded for review" with no image gives the
  operator nothing to review.
- **Long runs: prefer a stage per turn when the operator is present.** Each
  stage boundary then renders its cards immediately and the operator can
  redirect before the next stage builds on it. Drive stages back-to-back in one
  turn only when asked for a one-shot run — and then the closing reply must
  reconstruct ALL of it, per the two rules above.

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
  carries no home and is not to grow one** (§13.22's reader half, which §13.27
  explicitly left standing — with one library there is nothing for it to carry)
  — put the home your turn resolved in your prose instead. A parsed
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
  UNKNOWN, because the line carries none. Nothing writes a home onto it any more:
  the adopt door stopped minting sightings when the library went install-wide
  (§13.27, re-deriving §13.24/§13.25), because "which home can read this draft"
  stopped being a question with more than one answer. A binding that still shows
  an observed home is a LEGACY record of a sighting taken before the reversal —
  the launcher preserves it and labels it as legacy rather than deleting it, and
  nothing reads it to decide anything. A draft authored before projects existed
  is still reachable through the adopt door, which now sees every draft on the
  install rather than only the ones its own home could read.
- **What a resume hands you, and what it does not.** Resuming from the project
  seeds your first message with the draft id and the character's name, and
  nothing else. It used to carry a home line quoting the launcher's most recent
  sighting; that line retired with the sighting itself, so do not wait for one
  and do not treat its absence as the seed being incomplete. What the seed keeps
  is its closing sentence:
  *"Echo the home you resolve; do not assume it."*
  That sentence outlived the scoping it was written for, and under one library it
  is the discipline that surfaces a mis-resolved ROOT — a wrong install — instead
  of letting you assume one. So resume exactly the way you start: run the
  preflight probes, echo `.runtime_health.hermes_home` in prose, and read the
  draft yourself (`status --draft <id> --json`) before doing anything to it. If
  the draft is not in your list, do not author a second copy — say so, and say
  which root you are in.

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
