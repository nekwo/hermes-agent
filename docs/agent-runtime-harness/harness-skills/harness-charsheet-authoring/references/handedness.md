# Handedness — the measurement narrative

**When to open:** a `compose` refusal or warning names handedness; an operator
reports two opposite directions wrong and the rest right; you are deciding
between a reroll and `--accept-handedness`; you want to know what the check
could NOT see. The operative half — what blocks, what warns, the token rule —
is in `SKILL.md`; the measurements that force those rules are here.

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
