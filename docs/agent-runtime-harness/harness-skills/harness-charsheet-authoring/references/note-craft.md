# Note craft — a note is a drawing instruction

**When to open:** before you write any `--note` on a `reroll-row` or
`reroll-direction`. Three notes proven live are recorded here; start from them
rather than from a description of what is wrong.

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
