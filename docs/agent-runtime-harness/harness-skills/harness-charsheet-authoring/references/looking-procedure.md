# Looking is a procedure

**When to open:** before you judge any generated image — the first time in a
draft, and again whenever you are about to say a row is fine. A glance is a
reliable false negative; this file is the procedure that is not one.

## Why this is a procedure, and where the accumulated record lives

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
