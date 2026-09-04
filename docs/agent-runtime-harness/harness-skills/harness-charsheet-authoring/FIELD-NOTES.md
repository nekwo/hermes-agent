# Field notes — charsheet authoring (running record)

**What this is.** The accumulating input to `SKILL.md`'s final pass. Every slice in the
console-character-authoring program appends what it LEARNED here as it lands; the skill
is rewritten from this file at the end of the program, not before.

**This is one of two halves.** The launcher lane keeps its own running record at
`EterniaLauncher/docs/spatial/CHARA_CONSOLE_AUTHORING_FIELD_NOTES.md`, because a launcher
slice landing in a launcher worktree cannot commit here without a cross-repo write into a
shared checkout. AF reads BOTH. The split is by which repo the agent is standing in, not
by subject: a finding about a hermes payload, verb or home belongs in THIS file even when
a launcher slice discovers it — the other file cross-references, it does not copy.

**Why it works this way (owner ruling, 2026-08-24).** The skill was written second
(slice A0) and was stale within one commit: A1's polish changed the `thumb` payload and
the crop budget the same day, and the copy the runtime actually serves diverged from the
repo copy immediately. A skill written from a plan encodes what we believed before the
slices taught us anything; a skill written from this file encodes what they taught.
Writing it last also avoids compressing a session's findings twice — once into a slice
report, again into prose — which is where detail goes missing.

**How to append.** One entry per finding, newest section last. Say the FACT, then the
CONSEQUENCE for an authoring agent. Cite code by symbol, never line number. Mark each
entry `[READ]` (verified at the chokepoint that applies it) or `[INFERENCE]`. If a later
slice falsifies an entry, strike it and say why — do not delete it, the falsification is
itself a note.

**What does NOT go here.** Slice mechanics, gate output, commit shas — those live in the
slice reports and the plan. This file holds only what an authoring agent needs to know.

**Where this file lives — the package ROOT, and moving it is a behaviour change.**
A turn receives NOTHING from this file today, which is exactly the point: the raw record
stays out of the turn until AF rewrites the skill from it. That is a property of the ROOT
placement, not of the filename. Measured at
`agent/skill_commands.py::_build_skill_message` [READ, 2026-08-24]: the message body is
`SKILL.md`'s content alone, and the "[This skill has supporting files:]" block is built
from `linked_files` plus — when that is empty — a scan of exactly four subdirectories,
`references/`, `templates/`, `scripts/` and `assets/`. A file at the package root is in
neither path. Move this into `references/` as a tidy-up and it is advertised to **every**
turn that loads the skill, with a `skill_view(file_path=…)` instruction beside it — the
opposite of the owner's reason for the file. If it ever has to move, move it OUT of the
package, never into one of those four. (It is also part of the package content hash the
pre-push gate compares, so a file here is a file the gate reinstalls.)

---

## Environment

- **[READ] A persona's `HERMES_HOME` is not the serve process's.**
  `agent_runtime/profile_context.py::persona_profile_context` rebinds `HERMES_HOME` per
  turn from the persona's own `hermes_profile` binding, via both a ContextVar and
  `os.environ`. The launcher's spawn line is a default and says so in its own comment
  ("one process, N personas, no single HERMES_HOME that is right"). It early-returns
  WITHOUT rebinding when `binding.profile_home is None`, so for a persona with no
  `hermes_profile` the root does follow the process.
  *Consequence:* never assert a home. Echo the one the runtime resolved
  (`harness status --json` → `.runtime_health.hermes_home`) before touching a draft.

- **[READ] A relative `HERMES_HOME` resolves against cwd.**
  `hermes_constants._hermes_home_from_env` returns `Path(val)` as written, and
  `hermes_cli/main.py::_apply_profile_override` trusts any value whose parent dir is
  named `profiles`. A whole `fox-scout` character was authored into the hermes working
  tree this way and had to be gitignored (`f1bd041e8b`).
  *Consequence:* absolute homes only; check what you resolved, not what you passed.

- **[READ] A non-profile-shaped home is silently re-pointed.**
  `HERMES_HOME=X:/Eternia/.hermes` returns the *active profile's* characters via the
  sticky `active_profile` marker, not the root's.
  *Consequence:* a home that "works" may not be the home you named.

- **[READ] The runtime reads the INSTALLED skill, never the repo copy.**
  A canonical shared skill resolves only from `<hermes root>/shared/skills/`;
  `agent/skill_utils.py::_skill_resolution_status` returns `invalid_source` otherwise.
  On 2026-08-24 the installed copy was 14457 B against the repo's 14906 B and a live
  agent read the stale one — omitting a rule added that same day.
  *Consequence:* editing the repo skill changes nothing until it is installed.
  **Gated 2026-08-24 (`91e23bf0c5`):** `.githooks/pre-push` runs
  `scripts/verify_harness_skill_install.py`, which installs every canonical package and
  then fails the push if `harness_skill_hash_mismatches` is non-empty. Arm it once per
  clone with `git config core.hooksPath .githooks`. It earned its keep on the commit that
  installed it: this very file, added to the package directory by a parallel session,
  changed the package hash and was refreshed automatically at push time. The consequence
  above still holds for anyone whose clone is not armed.

- ~~**[INFERENCE, needs a note when confirmed] Trap not yet written into the skill:**~~
  **[READ] Written into the skill 2026-08-24 (`91e23bf0c5`), Preflight probe 2.** After a
  provider plan upgrade the stored token returns `401 token_expired` despite a local
  expiry ~15 h out; a forced refresh fixes it. It fires immediately *after* the operator
  does the right thing about the plan-gating trap, so the skill's old answer — "report the
  image provider is unavailable in this home" — sent them to re-check `auth.json`
  placement they had just fixed.
  *Consequence:* on a 401 right after a plan change, force a refresh and re-probe before
  reporting the provider unavailable.

- **[READ] The image-generation TOOL is `image_generate`; `image_gen` is the toolset that
  carries it.** `toolsets.py` registers `image_gen: {tools: ["image_generate"]}`,
  `tools/image_generation_tool.py` declares `name="image_generate"`, and `image_gen` is
  separately the `config.yaml` section that selects the provider
  (`agent/image_gen_registry`). Nothing is called `image_gen` at the tool level.
  *Consequence:* the provider preflight is one `image_generate` call. Say the schema name,
  not the config section — they differ by one word and only one of them is callable.

- **[READ] `--authored-by` is unvalidated free text, and an agent will not put a persona id
  in it on its own.** The lane does state the id: `persona_runtime`'s identity clause renders
  "You are <display name> (Mission Control persona id: `<id>`)". On the A2 run the id was
  `base` and the agent wrote `authored_by: "chara_a2"` — a slugified copy of its INSTANCE
  display name ("Chara A2 - Tier1 authoring"). `list --json` reports it verbatim as
  `authoredBy`, and it resolves to nothing: not a persona id, not a `hermes_profile`.
  *Consequence:* pass the id from the parenthesis in your own identity line, verbatim, never a
  display name. A consumer that reads `authoredBy` to pick the profile a draft is visible
  under must treat an unmatched value as "unattributed", not as a profile name.

## Verbs and stage gates

- ~~**[READ] A state below 2 frames passes `start` and can never reach `rows`.**~~
  **Struck by A3 (2026-08-25) — the FACT was right and is now false; the reasoning is kept
  because it is why the fix landed where it did.** Two modules held different floors:
  `spec.StateSpec` / `spec.parse_states` accepted `1 <= frames <= MAX_FRAMES_PER_ROW`, while
  the prompt builder raised `frame_count must be at least 2 for an animation row`. Live:
  `start --states idle:1` built a draft, spent the base anchor and three direction
  generations, and only then refused at `rows` with `{"ok": false, "error": "frame_count must
  be at least 2 for an animation row, got 1", "stage": "rows"}`, exit 2 — naming neither the
  row nor the state, and no verb could change `--states` afterwards.

- **[READ] The frame floor is 2, it is one number, and it now refuses at the moment you
  declare the state.** `spec.MIN_FRAMES_PER_ROW` is enforced in `spec.parse_states` — the ONE
  door both `characters start --states` and `characters add-state --state` come through — and
  `prompts.build_directional_row_prompt` READS that constant instead of spelling its own, so
  the two cannot drift apart again. `StateSpec` / `SheetSpec` deliberately still accept 1:
  they are also the deserializers (`draft.spec_from_dict`) for every `draft.json` and
  `character.json` on disk, including the drafts the old gap produced, and refusing those at
  load would break `characters list` outright.
  **Corrected on review, 2026-08-25 — the mechanism this entry gave was read off an `except`
  clause instead of measured, and it understated the damage.** `CharacterDraft.list_drafts`
  does swallow an unreadable draft with a log warning, but that swallow never fires for a
  bad spec: `CharacterDraft.load` reads JSON only and `CharacterDraft.spec` is a LAZY
  property, so `list_drafts` returns the bad draft happily. The raise lands one level up in
  `_characters_draft_summary` (`spec = draft.spec`), inside `_cmd_characters_list`'s own
  `except _CHARACTERS_EXPECTED`, which answers `{"ok": false, "error": …}`, exit 2.
  Measured with the floor raised on `SheetSpec` over a home holding one good draft and one
  `idle:1` draft: `characters list` returned `ok=false` and **zero** drafts — the good draft
  vanishes with the bad one and the whole verb fails. The conclusion is unchanged, and
  stronger than the entry claimed.
  *Consequence:* a one-frame state is now refused before a single generation is spent, on
  both doors, with `frame count 1 for state 'x' out of range; expected 2..8`. The two floors
  answer two different questions and both answers are correct: the SPEC says what a sheet can
  hold, `parse_states` says what you may ask us to draw. A draft already carrying a one-frame
  state still loads and still lists — it simply can never reach `rows`, and that is the
  honest report to give about it.

- **[READ] `thumb` is row-only — at the `turnaround` stage there is no crop verb at all.**
  `Draft.row_thumb` opens with `self._authored_row(row_key)`, so a direction reference is
  outside its vocabulary. Passing the store's own key answers
  `{"ok": false, "error": "'turnaround@s' is not an authored row of this sheet (authored
  rows: idle-s, idle-e, idle-n)", "stage": "turnaround"}`, exit 2. Live, the agent tried
  exactly that, then hand-rolled a Pillow crop: first into the draft's own `thumbs/`
  directory — which does not exist until `thumb` creates it, so the write failed — then into
  `$HERMES_HOME/cache/`.
  *Consequence:* a direction reference is QA'd by declaring the reference itself, or by
  cropping to a path OUTSIDE the draft. `thumbs/` is `thumb`'s namespace; nothing else writes
  there, and nothing writes into a draft with file tools.

- **[READ] `--directions 4` authors THREE directions.** `spec.FOUR_WAY.authored =
  ("s", "e", "n")` against `EIGHT_WAY.authored = ("s", "se", "e", "ne", "n")`. Live, an agent
  reading only the 8-way set quoted the operator "4 direction references + 4 idle row strips"
  for a `--directions 4`, one-state sheet whose real cost is 3 + 3.
  *Consequence:* read `spec.scheme.authored` out of `status --json` before quoting a cost, and
  take row keys from the payload — this sheet's were `idle-s`, `idle-e`, `idle-n`, and nothing
  was named `walk`.

- **[READ] A row is grounded on the APPROVED turnaround attempt, never merely the newest —
  and a `:fixed` row is grounded on the BASE image.** `CharacterDraft._row_reference` branches
  on `row.direction is None`: a directional row answers `store.current(turnaround_item(d))`,
  which is the APPROVED attempt (`store.latest` is a different, public, non-approved answer);
  a fixed row answers `_require_base()`. `run_rows`, `reroll_row` and every row `add-state`
  seeds all come through it.
  *Consequence:* the operator move "reroll `ne`, look at both, keep the ORIGINAL"
  (`approve-direction --direction ne --attempt 0`) is durable — every strip drawn afterwards,
  including a state added later, anchors on the attempt they kept and never on the one they
  rejected. If a later state should be drawn against a different reference, re-approve the
  direction at `turnaround`; do not reroll rows and hope. And never tell an operator that a
  `:fixed` state (`cheer:4:fixed`) is anchored on a turnaround view — it is the base image,
  the same anchor the character started from, prompted in the front view
  (`pipeline.NON_DIRECTIONAL_VIEW`). `add_state`'s own docstring claimed the turnaround for
  every row until 2026-08-25.

- **[READ] `add-state` refuses an empty value in its OWN flag now (fixed 2026-08-25).**
  `--state ''` and `--state '   '` answered `--states is empty; expected e.g.
  'idle:6,walk:8'` — the plural flag, which belongs to `characters start` and which
  `add-state` does not have, illustrated with a two-state list `add-state` refuses one check
  later. `spec.parse_states` now takes the caller's `flag`/`example` spelling; the grammar
  stays one authority. (`characters start --states ''` is not an error at all: an empty value
  there means "the CHAR8 states", taken from `CHAR8` itself.)
  *Consequence:* refusals on this verb are actionable text you can hand the operator
  verbatim. If one ever names a flag the verb does not accept, report it as-is and file it —
  do not silently translate it into the flag you think was meant.

## Looking at a sheet

- **[READ] The console's MEDIA hero card is a fixed 1:1 centre-cover square.**
  `local_image_attachment.dart::_LocalImageBlockViewState._buildPreview` builds an
  unconditional `AspectRatio(kInlineImageUnknownAspectRatio /* 1.0 */)` over
  `Image(fit: BoxFit.cover)` — no dimension probe anywhere above it. The square's side is
  `min(widthCap, heightCap)` and the HEIGHT cap usually binds (440 at 1280×800, 594 at
  1920×1080, 720 at 2560×1440).
  *Consequence:* ~~a tall crop is judged on its middle fifth. A defect outside that square
  is invisible until the image is opened. Declare crops knowing this.~~
  **Restated 2026-08-24 [REPORTED, launcher slice V1 — carried across from the launcher
  half, not re-verified here].** The FACT above stands and was re-verified at the same
  symbol; V1 changed the OPENED view, not the card. But "invisible until the image is
  opened" now understates what opening gives you: `_openPreview` calls
  `showFullscreenImageSet`, which pages the whole set with the arrow keys and enters compare
  with `C`. So: a tall crop is still judged on its middle fifth, and the card was never the
  verdict surface — judge crops by OPENING them.

- **[READ] Side-by-side is what settles a seam; magnification is not.**
  Two independent agents each failed to identify the known seam at 5–6× on a single
  frame, and each had a "sharpest dark row" scan rank real art (the sock band, the skirt
  hem) above the actual defect. Both settled it by rendering attempt N beside attempt N−1,
  aligned.
  *Consequence:* compare, don't zoom. A brightness heuristic finds the character, not the
  defect.

- **[REPORTED, launcher slice V1, 2026-08-24 — carried across from the launcher half, not
  re-verified here; V1's own reviewer has this entry on its list] Compare mode aligns PANES,
  not pixels.** One `TransformationController` spans both panes and side-by-side gives them
  equal `Expanded` widths, but inside each pane the picture is `Image(fit: BoxFit.contain)`,
  letterboxed to that pane. Same-dimension attempts land feature-for-feature — which is the
  case the finding above was drawn from, so that finding stands — but DIFFERENT aspect ratios
  do not, and the viewer will not tell you they did not.
  *Consequence:* comparing a crop against a full sheet, or two attempts at different sizes,
  gives an alignment you may trust for gross differences and not for a seam. Across unequal
  sizes the A/B flip is the stronger instrument: one box, alternating, transform deliberately
  not reset. Until V1's reviewer rules, treat this as unconfirmed — first-hand evidence from
  your own comparison run outranks it.

- **[READ] The seam's contrast is directional.** On the known artifact the band reads
  28.6 against 135.4 immediately BELOW it, while above it the hair darkens gradually over
  ~50 rows. It is a hard lower boundary, not a dark band between two bright regions.
  *Consequence:* "look for a dark band" is the wrong search; look for a hard edge.

- **[READ] The default crop is a reduction only when the row has more than 4 frames.** `thumb`
  slices one cell (`strip_px / frames`) and then multiplies by `scale**2`, so the output is
  `strip_px * scale**2 / frames` and it shrinks only while `scale**2 < frames`. At the default
  `--scale 2` that means `frames > 4`. Measured on an `idle:2` row: the strip is 1774x887 =
  1,573,538 px and the default crop is 1774x1774 = 3,147,076 px — **twice the strip it was
  supposed to make lighter.** CHAR8's own rows are the happy case (walk:8 gives 0.5x, idle:6
  gives 0.67x), which is why the whole-strip defect this verb replaced was only ever measured
  there.
  *Consequence:* on a short row `--scale 1` is the crop and `--scale 2` is an enlargement.
  Divide before you zoom.

- **[READ] A raw row strip and a turnaround reference are both heavier than the sheet, and
  nothing checks either.** The card-weight budget lives inside `thumb` alone. Live, an agent
  declared three raw strips at 1,573,538 px each against a composed sheet of 384x624 =
  239,616 px — 6.6x the sheet per card — plus turnaround references up to 413,404 px.
  *Consequence:* "declare a crop, not a sheet" is a rule the tooling does not enforce on any
  path it does not own. The only self-reporting artifact is `thumb`'s, and the `cardSafe`
  entry below says what that report does not promise.

## Payload

- **[READ] A new payload FLAG is now admitted by a check, not by memory.** The class
  behind the two entries below — a boolean whose guarantee lives only in prose — had its
  instances repaired one at a time and nothing looking at the next one. It has a gate as of
  2026-09-04: `tests/hermes_cli/test_charsheet_payload_flag_admission.py` measures every
  boolean the four read payloads actually PRINT (through
  `charsheet_payload_contract.build_flag_inventory`, which runs the verbs) and requires each
  to be admitted in its table as either a **Guarantee** — naming the pure predicate that
  computes it and a test where it DISAGREES with its neighbour — or **Data**, saying why
  there is no guarantee to drift. An unadmitted flag reds; so does a stale entry for a flag
  nobody publishes any more (proved by planting one: `thumb.plantedFlagBySuite`).
  *Consequence:* when you add a boolean to a `characters` payload, expect that test to stop
  you, and answer it — the answer is the design review the class kept skipping. It is an
  EXISTENCE check on the citation, not proof the named test asserts a disagreement; that
  half is still a reviewer's job.

- **[READ] Attempts are 0-based in payloads and flags, 1-based in human lines.**
  One helper (`_attempt_label`) renders "attempt N of M". Store filenames are
  `attempt-<n+1>.png`.
  *Consequence:* a QA card RELABELS, never renumbers.

- **[READ] `cardSafe` answers the question; the thresholds are not the consumer's.**
  `thumb` carries `cardSafe`, derived from `MAX_CARD_PIXELS`, ~~itself derived from
  `CHAR8.sheet_size()`, so it cannot drift~~ — **struck 2026-08-25: `MAX_CARD_PIXELS` is a
  module CONSTANT sized once from `CHAR8.sheet_size()`. It is fixed; what drifts is the
  draft.** The comment above the constant asserted the opposite in so many words ("a sheet
  that grows moves the budget with it, and the number can never drift from the thing it is
  measured against") and was corrected in the same pass, along with `row_thumb`'s refusal
  message. Above the default scale a crop is written but flagged; at or below it an
  over-budget crop is refused, naming `--scale 1`.
  ~~*Consequence:* only declare a `cardSafe: true` crop with `MEDIA:`.~~ **Struck by A2 —
  `cardSafe: true` does not mean "lighter than this sheet".** The derivation above is right
  and the guarantee it gets read for is wrong: `pipeline.MAX_CARD_PIXELS` is
  `CHAR8.sheet_size()`, the LARGEST sheet the package composes, not the sheet the draft in
  hand will compose. Measured live on a `--directions 4`, `idle:2` draft whose composed sheet
  is 384x624 = 239,616 px: the DEFAULT crop (`thumb --row idle-s --frame 0 --scale 2`) came
  back 1774x1774 = 3,147,076 px carrying `cardSafe: true` — **13.1x the sheet it exists to
  avoid decoding**, clearing the fixed budget by 1.5%. One reroll turn declared four of them:
  12,588,304 px, roughly 48 MiB decoded, every one `cardSafe: true`. `--scale 3` on the same
  row is the first `false`.
  *Consequence (restated):* `cardSafe` is a CEILING, and it is CHAR8's. It answers "will this
  file sink the console", never "is this lighter than my sheet". On any spec smaller than
  CHAR8, weigh the crop against that draft's own `spec.sheet_size()` — and see the frames rule
  above, which is what actually decides whether a crop reduces anything. Still never carry a
  copy of the threshold.

- **[READ] Absence travels as JSON `null`, not `""`** for `history[].path`, `current`,
  `approvedPath` — and, since `91e23bf0c5`, `baseImage` in BOTH `status --json` and
  `list --json`. The parenthetical here ("`baseImage` was still `""` as of the A0
  review") is now spent: it was the fourth path field, in the same response as the three
  that had already been fixed, and the CLI test pinned the old spelling. Every path in
  those payloads goes through one public helper (`draft.path_or_none`).
  *Consequence:* tolerate both on read (older drafts and other payloads may still carry
  `""`); emit neither as a bare `MEDIA:` line.

- **[READ] `add-state` can push a draft's own sheet PAST `MAX_CARD_PIXELS`, and that turns
  A2's `cardSafe` finding around.** The budget is `CHAR8.sheet_size()` = 1536x2080 =
  3,194,880 px, a FIXED number derived from the largest sheet the package's default spec
  composes — not from the draft in hand. A2 measured the small end of that: a `--directions 4`,
  `idle:2` draft whose 239,616-px sheet was 13.1x lighter than a `cardSafe: true` crop.
  Growing a sheet reaches the other end. Measured live 2026-08-25: adding `jumping:6` to the
  8-way `anime-girl` sheet recomposed it at 1536x3120 = 4,792,320 px — **1.50x the fixed
  budget**, and every further state adds another `authored x frame_h` band.
  *Consequence:* on a grown sheet `cardSafe: false` no longer implies "heavier than your
  sheet" — a crop can be refused at the default scale (`--scale 2`) while being genuinely
  lighter than the sheet that draft will compose. **The refusal used to SAY the wrong thing
  as well** — it read "heavier than the sheet this crop exists to avoid decoding" — and as of
  2026-08-25 `CharacterDraft.row_thumb` names the fixed card budget and states outright that
  it is not a comparison against your sheet. Both directions have one answer, the one
  A2 already wrote: weigh a crop against **that draft's own `spec.sheetWidth x
  sheetHeight`** from `status --json`, and read `cardSafe` as the console's ceiling, never as
  a statement about your sheet. Do not carry a copy of the threshold.

- **[OPEN QUESTION for launcher slice B2, recorded 2026-08-25 — a decision nobody has
  taken, not a finding.] ~~OPEN~~ — RULED 2026-08-25, and the ruling took NEITHER of the
  two options below. It SPLIT the boolean: (a)'s fixed console ceiling stays, under the
  name `withinConsoleBudget`, and (b)'s draft-relative reading is added beside it as
  `withinOwnSheet` rather than replacing it. Nothing about the two measurements below is
  falsified — they are the reason the split happened and they are now the test inputs.
  Read the entry below this one for what a consumer does with the pair.** Two options were on the table when `MAX_CARD_PIXELS`'s comment was
  corrected, and only one was taken. **(a) TAKEN:** the budget is a fixed CONSOLE DECODE
  ceiling — what a chat card may render — and the comment plus `row_thumb`'s refusal now say
  exactly that. No semantics changed; `cardSafe` means what it always meant. **(b) NOT taken,
  and it is the owner's call:** make the budget the draft's own `spec.sheet_size()`, so
  `cardSafe` would mean "lighter than MY sheet". That changes what the boolean MEANS to the
  consumer reading it (launcher B2), which is why a fix round declined it. The two
  measurements that make it live, both on drafts that exist: a `--directions 4`, `idle:2`
  sheet is 384x624 = 239,616 px and its DEFAULT crop came back `cardSafe: true` at
  1774x1774 = 3,147,076 px — 13.1x that sheet, and 1.5% under the fixed budget; the
  `add-state`-grown `anime-girl` sheet is 1536x3120 = 4,792,320 px, 1.50x the fixed budget,
  where the default scale can be REFUSED for a crop lighter than the sheet that draft will
  compose. Under (b) both of those answers flip.
  *Consequence:* until an owner rules, `cardSafe` is the console's ceiling and nothing else.
  A consumer that needs "lighter than my sheet" computes it from `status --json` →
  `.status.spec.sheetWidth` x `.status.spec.sheetHeight`. Do not carry a copy of the
  threshold, and do not read the boolean as a comparison.

- **[FIXED 2026-09-02 — `--no-sheet`] `characters sprite <slug> --json` inlines the whole
  sheet as base64 and has no path-only mode.** `draft.sprite_payload` always emitted
  `spritesheetBase64` from the sheet
  bytes and returned no path or directory for it. Measured on the installed CHAR8 `anime-girl`:
  438,972 base64 chars = **428.7 KiB**, in a 441,694-byte payload — 107x the 4,096-byte
  event-payload cap (`agent_runtime/events.EVENT_PAYLOAD_LIMIT_BYTES`) that the "images travel
  as paths" rule is written against. It is also 8.8x the terminal tool's own output cap
  (`tools/tool_output_limits.DEFAULT_MAX_BYTES = 50_000`, unset in `profiles/base`), and
  `tools/terminal_tool.py` truncates by splicing a notice between a 40% head and a 60% tail —
  which for this payload happens to keep the structural fields (they sit in the first 2,202
  and last 498 chars) while making the JSON unparseable and spending roughly 12k tokens of
  context on base64. A small 4-way `idle:2` sheet came to 43,477 bytes and survived intact;
  the size scales with the sheet, so the small case proves nothing about the real one.
  *Consequence:* on the chat lane read `character.json` (or `status --json`) for the shape.
  `sprite --json` is a launcher-side read; do not pipe it into a turn.
  *Cure, 2026-09-02:* `sprite_payload` takes `include_sheet`, and the verb spells it
  `--no-sheet`. That mode omits `spritesheetBase64`, never reads the sheet bytes at all, and
  puts `sheet` — the absolute path, the same key `characters list`'s installed rows use — in
  its slot; `spritesheetRevision` and every geometry/taxonomy key are unchanged, so the
  provenance record a consumer writes after reading the file itself is still available. The
  numbers above are the DEFAULT's and stay true: the default payload is byte-identical,
  key order included, because the two shapes are one conditional entry in the same position
  rather than an append. The "do not pipe it into a turn" rule survives for the default and
  is what `--no-sheet` exists to let an agent step around — the metadata-only payload of the
  small 4-way fixture is under 2 KiB. What is still NOT cured is the pets sibling: `harness
  pets sprite` is served by `_pet_sprite_payload_for_launcher` in `hermes_cli/harness.py`, a
  different function with its own always-inlined `spritesheetBase64`, so the flag was
  deliberately not mirrored onto it.

- **[READ] `CHARSHEET-QA:`'s required `generator` key has no source in any payload, and its
  meaning flips at compose.** `tests/fixtures/charsheet_qa_line.json` puts `generator` in
  `requiredKeys` and illustrates it with `"openai-codex"`, an image provider. The only
  `generator` the pipeline writes is the literal `"charsheet"` in `character.json`'s manifest
  (`draft.compose`), which names the pipeline. No `start` / `status` / `list` / `rows` payload
  carries a provider name at all. Live, one draft emitted `"generator":"openai-codex"` at
  `turnaround` and `rows` and `"generator":"charsheet"` at `composed`, and dropped the key
  from every item-level line — which the fixture makes required.
  *Consequence:* decide once what `generator` means and say the same thing on every line of one
  draft. A consumer cannot key on it today.

- **[READ] `status --json` answers under `.status`, and `rows` / `turnaround` are MAPS keyed by
  item key.** `.status.rows` is `{"idle-s": {...}, ...}` — not a list — each value carrying
  `attempts`, `approved`, `current`, `approvedPath` and a `history` array whose entries are
  `{attempt, created, note, path, rejected}`. `.status.stages` is the ordered stage list and
  `.status.pending` / `.status.missing` are `{rows: [], turnaround: []}`.
  *Consequence:* A1's per-attempt `path` is live and is how attempt N is shown beside N-1
  without a second lookup. The reroll note travels in the same entry, which is what makes an
  attempt reproducible.

## Process

- **[READ] A failed row aborts the batch.** Survivors sit at `attempts: 0`, which is
  indistinguishable in a status dump from "never started". `run_rows(only=None)`
  regenerates every authored row unconditionally.
  *Consequence:* never re-run a bare `rows` to recover; name the survivors with `--only`.

- **[READ] `--only` takes exact comma-separated keys. There is no glob.**
  `run_rows` raises on any key not in the authored set.

- **[READ] `characters add-state --draft <id> --state <name>:<frames>[:fixed]` exists as of
  2026-08-25, and `reopen` is its only door.** It refuses at any stage but `rows`
  (`CharacterDraft.add_state` → `_require_stage`), so on an installed character the sequence
  is `reopen → add-state → rows --only <new keys> → QA → compose`. It takes exactly ONE
  state — a comma-separated list is refused with *"--state takes ONE state"*, because
  `--state` is singular and a list here would be a second spelling of `start --states`. A
  duplicate state name is refused too; there is no remove verb and none is planned as a flag
  (removing a state would delete approved attempts and the notes stored with them).
  *Consequence:* **take the new row keys from the verb's own answer**, never by composing
  `<state>-<direction>` yourself. The payload's `rows` is the list and the human line spells
  the whole `--only` string ready to paste — which matters precisely because `--only` has no
  glob, and because the count is `len(spec.scheme.authored)`: five on an 8-way sheet, three
  on a `--directions 4` sheet.

- **[READ] Adding a state never renumbers a row, and `compose` will not let you install a
  blank one.** The state is APPENDED and `SheetSpec.rows()` is state-major, so every row the
  installed manifest already published keeps its index and the sheet grows DOWNWARD (live:
  the anime-girl sheet went 1536x2080 → 1536x3120 with `idle-*`/`walk-*` untouched — all
  five `idle-*` at one attempt, `walk-n` still at 3 attempts / approved index 2 and
  `walk-ne` at 2 / 1, both still carrying their 2026-08-24 operator notes. **Corrected on
  review, 2026-08-25:** this read "attempts 1/1/1/1/1 and 1/3/2/1/1", which is right only
  in alphabetical row-key order (`e n ne s se`) and wrong in the sheet's own `s se e ne n`
  order, where `walk-*` reads 1/1/1/2/3. Two orders in one figure is how a row gets
  mis-attributed; the keys are named instead). The new rows are "seeded" by
  appearing in the spec — nothing is written to the revision store — so they read
  `attempts: 0`, `approved: null`, and land in `missing.rows`; `compose` then refuses
  while any of them lacks an approved strip, naming every one.
  *Consequence:* a consumer holding the previous `character.json` still addresses the same
  pictures by row index, but the sheet's HEIGHT changes — anything keyed on the sheet's size
  or bytes (`spritesheetRevision`) is stale after the recompose, which is the point. And an
  un-generated new state cannot silently ship: the refusal is the gate.

- **[READ] Row rerolls are stochastic, row-grained and auto-approved.** One row took three
  strips. An unexamined reroll silently becomes the sheet.
  *Consequence:* look at every reroll before moving on.

- **[READ] The default hypothesis is "the model drew it", not "the code broke it".**
  The one real defect found in anger was generated art, and the pipeline-residue
  hypothesis (slicing, keying, palette lock) cost the most time while being wrong.

- **[READ] A clarify chip is emitted only when the turn deliberately reaches for one, and a
  decision phrased as prose bullets looks the same to the agent.** Live: two decision turns
  went out as markdown bullet lists with `clarify_request: null`; after the operator said
  "give me pickable options rather than a prose question" the next turn called `clarify` with
  four choices and the payload carried a `clarify_token`; the turn after that reverted to
  bullets. Nothing refuses a prose question — `MissionChatClarifyCapture` simply has nothing to
  capture, so the console renders text where a one-click answer was the point.
  *Consequence:* the chip is not a formatting preference, it is the operator's only
  one-interaction answer. Reach for `clarify` at every point you stop — including the turn
  where you are reporting a blocker and offering ways out of it.

- **[READ] A stage change with no `CHARSHEET-QA:` line is invisible downstream.** Live,
  `reopen` moved a draft `composed` to `rows` and the reply carried no line, so anything
  keying off the last one still believes the draft is composed.
  *Consequence:* `reopen` IS a stage change. Emit the line for every stage that actually
  moved, and say in prose when a verb failed and left the stage where it was.

- **[READ] The operator trace truncates a command at 500 characters.**
  `agent_runtime/progress._OPERATOR_COMMAND_FULL_MAX` is 500, and a `characters start` carrying
  a concept, a style and an absolute `--base-image` is longer — live, it was cut mid-path in
  the console's own trace row.
  *Consequence:* the trace is not a record of what you ran. Put the draft id, the slug and the
  spec you chose in the REPLY, where the operator can actually read them.

## For AF — the skill rewrite

- **[READ] A served file's falsehood is worse than this file's, and the two are not
  symmetric.** A turn receives `SKILL.md` and receives NOTHING from this file
  (`agent/skill_commands.py::_build_skill_message`, and the placement note at the top of this
  file). So an agent reading a false line in `SKILL.md` never meets the correction struck
  here. Live case: `SKILL.md`'s `--scale` bullet said "at or below the default the crop must
  be lighter than the composed sheet (that is the whole point of cropping)" — a guarantee
  `pipeline.MAX_CARD_PIXELS` has never made, on a sheet 1.50x that budget. Corrected in place
  2026-08-25 rather than left for the rewrite.
  *Consequence for AF:* whenever an entry here is struck, check whether `SKILL.md` repeats
  the struck claim, and fix the served copy in the same commit. The strike reaches nobody;
  the served copy reaches every turn.

- **[READ] `SKILL.md` carries one bullet that repairs nothing false — "A state is added,
  never removed" (added by A3). KEPT deliberately, and recorded here so AF knows it arrived
  outside a repair mandate.** It is true at the code (`CharacterDraft.add_state` has no
  removal path, there is no `--confirm` sibling verb, and the docstring records why) and it
  stops an agent promising an operator a verb that does not exist — the same rule as "no fake
  affordances". Dropping a true, useful line to honour a scope boundary would have been churn
  on a file AF rewrites wholesale.
  *Consequence for AF:* it is a completeness bullet, not a claim needing re-verification.
  Keep it, or fold it into the add-state paragraph.

## Handedness — which way a direction actually faces

- **[READ] "Turned toward the viewer's right" is unambiguous for a FRONT view and
  ambiguous for a BACK one, and that one phrase shipped a broken character.**
  `prompts.VIEW_LANGUAGE["ne"]` read *"seen in three-quarter BACK view turned
  toward the viewer's right: … at most a sliver of the far cheek or jaw"*. Two
  things were wrong with it and only together do they explain the failure. The
  sentence named no side of the FRAME — no leading shoulder, unlike its front
  twin `se` ("the near shoulder leads") — so its only handedness anchor was a
  phrase that flips meaning depending on whether you resolve "turned toward" in
  the frame or in the subject's own body. And "the FAR cheek" is backwards for a
  back three-quarter: the cheek that clears the back of the head is the NEAR one,
  the one on the side rotated toward the camera, so obeying it literally turns
  the head the other way. Measured on the live `anime-girl` draft: `ne` came back
  facing north-WEST in **all three** states it was ever drawn for — `idle` and
  `walk` (2026-08-24) and `jumping` (2026-08-25), three independent generations —
  while `se`, carrying the same "toward the viewer's right" in a FRONT view, was
  right in all three. Face-offset from the body centroid, in the sheet's own
  `s se e ne n` order: `+0.0 +5.2 +10.9 −6.5 −0.3` (idle), `+1.7 +6.2 +9.9 −8.8
  −2.2` (walk), `+0.2 +6.4 +10.5 −9.7 −0.1` (jumping) — a rotation that reverses
  at exactly one step, the same step, every time. Rewritten 2026-08-25 to say the
  same thing three ways (which way the body points IN FRAME, which shoulder is
  nearer, which side the sliver of face may appear on) plus an explicit refusal
  of the mirror; `nw` got the mirrored rewrite in the same pass.
  *Consequence:* when you write a note about facing, write it in FRAME terms —
  "the body angled up and to the RIGHT of frame, the sliver of face on the
  viewer's RIGHT, never the mirror of this". "Turn her the other way" and "face
  north-east" both leave the model the same coin flip that produced this.

- **[READ] One mirrored authored row breaks TWO directions, and the six others
  look perfect — which is why it survives QA.** The sheet composes authored rows
  only and the consumer derives `nw` by flipping `ne` (launcher ADR 0024 ruling
  3-B; `pipeline.compose_draft_frames` has no flip in it, and `agent/charsheet/
  __init__.py` said "derived at compose time" until 2026-08-25, which had been
  false since that ruling). So `ne` drawn as NW makes `nw` render as NE: both
  rear diagonals wrong, `s`/`n` symmetric enough to look fine, `e`/`w`/`se`/`sw`
  genuinely fine. The operator's report was "forward-left and forward-right are
  inverted, standard forward is fine" — a description that fits a launcher
  sector-mapping bug far better than it fits one bad row, and the launcher's
  sector/mirror code was checked end to end and is correct.
  *Consequence:* when an operator reports two opposite directions wrong and the
  rest right, suspect ONE authored row and its mirror before suspecting the
  consumer. Ask which of the eight are wrong: a consumer bug rarely spares six.

- **[READ] The approved turnaround reference does NOT determine the row's
  facing — the row prompt does.** Measured on the same draft, where nothing was
  ever rerolled at `turnaround` (every direction is `attempt-1`, `approved: 0`,
  and the files in `turnaround/` are byte-identical to the store's attempts): the
  approved `e` reference is a LEFT-facing profile (face offset −44.8 px; the
  shoes point left), and every `e` row generated FROM it — idle, walk and jumping
  — is right-facing (+10.9, +9.9, +10.5). `ne`'s reference is likewise
  left-facing (−23.5) and there the rows followed it. So the reference carries
  identity, and `build_directional_row_prompt`'s FACING clause carries the
  facing; when they disagree the text usually wins.
  *Consequence:* approving a turnaround does not certify any row's handedness,
  and a wrong-facing reference does not doom the rows. QA the ROWS. It also
  means the reverse repair works: reroll the row with the facing spelled out
  rather than chasing the reference first.

- **[READ] There is now a compose-time gate, it REFUSES rather than warns, and
  its blind spot is exact.**
  **~~"its blind spot is exact"~~ — STRUCK 2026-08-25 (round two). This entry
  describes the UNREGISTERED gate. Its numbers were measured by a ratio that read
  placement as handedness, its "~1 s" is 0.08 s, and its blind-spot list was
  three items short. Everything below the line still happened and the diagnosis
  still stands; the figures and the boundary have moved. Read the round-two
  entries at the end of this section for the measure that ships.** `pipeline.detect_mirrored_art`, called from
  `validate_sheet` (so it blocks `compose` → install), walks each directional
  state's authored rows in turnaround order and asks per row: *would flipping
  this one row horizontally make the seams it touches fit better?* Flag at
  `MIRROR_GAIN_THRESHOLD` = 8%. Measured: the defective sheet scores 19% / 13% /
  13% on `idle-ne` / `walk-ne` / `jumping-ne`; the corrected sheet and every
  other row of both, plus a second character (4-way robot, satchel over one
  shoulder — deliberately asymmetric), score at most 3.9%. Pillow only, no
  numpy — numpy is not a dependency of this package and is absent from the venv
  the runtime actually executes in. ~~~1 s on a 15-row sheet~~ — measured 0.08 s
  unregistered, ~4.0 s registered (round two).
  It judges only rows with a neighbour on EACH side (`se`, `e`, `ne` on an 8-way
  sheet); the ends are reported in `handedness.unjudged` with the reason, because
  one seam cannot say which of its two rows is the mirrored one — an earlier
  draft that judged them refused the correct robot character over an 11%
  single-seam reading. **Two things pass it by construction, and both are
  pinned by tests so they stay known:** a sheet mirrored on EVERY row (the
  measure is invariant under a global flip — only the world outside a sheet says
  which way is east), and the interior of a contiguous block of mirrored rows.
  A 4-way sheet is nearly blind too: its one interior row's neighbours are both
  near-symmetric views, and mirroring it moved the live 4-way character's score
  from +1.9% to −1.9%.
  **Two more found by review on 2026-08-25, neither pinned by a test.** (a) A
  whole directional STATE mirrored passes clean — the fixed point is per state,
  because the chain never leaves one, so `add_state` generating all five rows of
  a new state against a bad reference is invisible here. Measured: mirroring all
  five rows of `idle`, `walk` or `jumping` on the repaired sheet gives `ok=True`,
  nothing flagged, while the evidence sits unused one row away — `idle-e` against
  a mirrored `jumping-e` prefers the flip by +22.4%, and on correct art the same
  comparison reads −15.7% to −68.9%. Comparing the SAME direction across states
  would close it. (b) The gate names extra rows as readily as the right one: a
  mirrored row pushes BOTH its neighbours toward the line, and on the live
  character mirroring `idle-e` alone flags `idle-se` (13.1%) and `idle-ne` (20.8%)
  beside it. Every flagged row is an error, the message tells the operator to
  `reroll-row` each of them, and a reroll auto-approves (see the one-way-door
  entry) — so following the refusal literally spends correct art. Reroll the
  LARGEST gain first and re-compose before touching another row.
  And the measure has no registration step, so it reads PLACEMENT as handedness:
  on the repaired sheet an 8 px horizontal shift of `idle-e` in a 192 px frame —
  art untouched — moves it from −38.2% to +9.4% and refuses the install. What
  holds that at bay is `normalize_cells` centring each row on its union bbox
  (measured residual ±3 px), which is upstream pet code this package imports and
  does not own. A prop hanging ~10% of the frame width off one side of ONE row
  displaces the body by half its width and crosses the line: a 16 px prop scores
  +5.2% (installs, two thirds of the margin gone), a 20 px prop +8.0% (refused).
  *Consequence:* if `compose` refuses with "looks drawn as the MIRROR of", do not
  argue with it and do not hand-flip the sheet — the next compose overwrites any
  hand edit. Reroll the row. And a clean pass is not a certificate: it never saw
  the front and back views, and it cannot see a consistently mirrored character
  or a consistently mirrored STATE.

- **[READ] A wrong turnaround reference is PERMANENT once the draft leaves
  `turnaround`, and every row for that direction then has to fight it.**
  `CharacterDraft.reroll_direction` and `approve_direction` both
  `_require_stage(..., "turnaround")`, and the only backward verb in the package
  is `reopen`, which goes `composed` → `rows`. There is no path back to
  `turnaround` and no verb that replaces an approved reference afterwards. Live:
  `anime-girl`'s `ne` reference is a north-WEST view and has been since the
  single turnaround generation on 2026-08-24 (one attempt, approved index 0, no
  reroll) — so the fix had to be made entirely in the row prompts, against a
  reference pulling the other way, for the whole life of the draft.
  *Consequence:* QA the turnaround strip HARD, before `approve-direction --all`.
  Approving it is the last moment a reference can be changed. If a reference is
  wrong and the draft has moved on, say so plainly — the repair is row-side and
  costs more attempts, or the character starts again.

- **[READ] For a handedness reroll, spelling the target facing is not enough;
  naming the CONFLICT with the reference is what works.** Measured on the three
  `ne` rows, 2026-08-25, with the corrected `VIEW_LANGUAGE` already in the
  prompt. A note that spelled the target in frame terms ("body angled
  up-and-to-the-RIGHT in frame, the sliver of face on the viewer's RIGHT, never
  the mirror of this") landed **one of three** — `idle-ne` came back correct,
  `walk-ne` and `jumping-ne` came back north-WEST again, and a second, more
  emphatic version of the same note ("HANDEDNESS OVERRIDE… ignore which way the
  reference is turned") also failed on `walk-ne`. What landed both remaining rows
  on the first try was a note that told the model what to DO with the reference
  rather than what to ignore: *"MIRROR THE REFERENCE'S TURN. The attached
  reference is turned the WRONG way for this row — copy its design, colours, hair
  and proportions, then draw it turned the OTHER way,"* plus one anatomical
  anchor that cannot be read two ways — *"her right ear is the only ear visible
  and it sits on the RIGHT-HAND SIDE of her head as you look at the picture."*
  Six generations for three rows.
  *Consequence:* when the grounding reference disagrees with the facing you want,
  say so in the note, in those words. "Ignore the reference" leaves the image
  there doing its work; "mirror it" gives the model an operation. And anchor the
  side on a body part with a name (the visible ear, the near shoulder), not on
  the direction token.

- **[READ] A row reroll is a ONE-WAY door for the approved attempt.**
  `CharacterDraft.reroll_row` ends with `store.propose` then `store.approve`
  unconditionally, and there is no `approve_row` method and no `approve-row`
  CLI verb — `approve-direction` is for references only. So a reroll that comes
  back worse than what it replaced cannot be un-approved; the only way back is
  another reroll that happens to be good. (The history is not lost: earlier
  attempts and their operator notes stay in `state.json`, and `walk-ne` came out
  of this repair at 5 attempts / approved 4 with its 2026-08-24 note intact.)
  *Consequence:* look at the strip BEFORE you spend the next reroll, and tell the
  operator the pointer moves whether or not the new attempt is better. On a row
  you are unsure about, that is the difference between one generation and four.

- **[READ] The handedness gate answers WHICH SIDE, never HOW FAR.** After the
  repair the sheet passes clean, and the face-offset progression `s se e ne n`
  reads `+0.2 +6.4 +10.5 +15.1 -0.1` for `jumping` — `ne` further from centre
  than the profile `e` it should sit inside, i.e. a rear diagonal drawn closer to
  side-on than the rotation wants. `detect_mirrored_art` has nothing to say about
  that and should not: it compares a row against its neighbours' MIRRORS, and a
  view that is turned too far is still turned the right way.
  *Consequence:* a clean compose does not mean the turnaround is even. Look at
  the five views together — the offsets should rise to the profile and fall
  again — and reroll on evenness separately if the operator cares.

- **[READ] The two-neighbour rule proposed from the launcher side does not hold
  on the whole sheet, and the state it was measured on is why.** The proposal was
  to flag a row only when it is closer to BOTH authored neighbours mirrored than
  unmirrored. On the defective sheet's `walk` state it fires, which is where it
  was measured. On `idle` it does NOT: `idle-ne` vs `idle-n` reads d=11.84
  against dmir=12.10 — the unmirrored art is 2.1% closer, pure noise off a
  near-symmetric back view — so one neighbour votes "ok" and vetoes the other
  neighbour, whose seam prefers the mirror by **31.8%** (`idle-ne` vs `idle-e`,
  d=18.46 against dmir=12.59; re-measured on the preserved pre-fix sheet
  2026-08-25 — this entry and `1175d3db90`'s body both said 29%, which does
  not reproduce). The row was mirrored. Requiring both neighbours is what keeps the
  rule off legitimately asymmetric art, and it is also what lets a symmetric
  neighbour veto real evidence; summing the seams instead lets that neighbour
  dilute the ratio without voting, which is what the shipped gate does.
  *Consequence:* a rule measured on ONE state of one character is measured on one
  sample. Re-run it over every state before believing it.

- **[READ] The gate now REGISTERS before it measures, and that is the difference
  between measuring handedness and measuring placement.** Round one paired cells
  by column index with no alignment step, so any horizontal displacement between
  two neighbouring rows entered the distance and therefore the ratio. Measured on
  the REPAIRED (correct) live sheet by sliding `idle-e` sideways and touching
  nothing else: −7 px in a 192 px frame scored +9.81% and REFUSED the install,
  −24 px scored +18.75% — past every genuine reading on the defective sheet. The
  reachable driver is not a stray offset, it is a PROP: `normalize_cells` centres
  each row on its union bbox, so a bag, a cape or a sheathed sword hanging off
  one side of ONE row widens that row's box and moves the BODY by half the prop's
  width. A 24 px prop (12.5% of the frame) refused; a 48 px one refused harder.
  `_registered_distance` now takes the MINIMUM over a symmetric integer shift
  grid of ±`frame_w/12` (16 px on the 192 px cell), for the direct and the
  flipped comparison alike, so a translation cancels out of the ratio. After it:
  the same slides read −18.05% and install, and a one-sided prop up to a quarter
  of the frame (48 px, body −24 px) still installs at +5.19%.
  *It is a minimum over a SYMMETRIC grid and not a cross-correlation peak on
  purpose.* `distance(shift(flip a, d), flip b) == distance(shift(a, −d), b)`, so
  a symmetric grid's score SET is unchanged by a global flip and its minimum is
  exactly equal — which is what keeps "a sheet mirrored on every row is a fixed
  point" true of the registered measure. A peak with a first-wins tie-break picks
  a different shift on symmetric art and breaks that equality.
  *Consequence:* the window BOUNDS the blindness, it does not remove it. A pure
  translation past the window still crosses (−24 px reads +10.91% even
  registered — **that figure is the untouched NEIGHBOUR `idle-ne`; the displaced
  row `idle-e` itself reads +9.38%**, corrected 2026-08-25 by review). The
  crossing band is also bounded on BOTH sides, which "past the window still
  crosses" hides: sliding `idle-e` reads −15.38% at −8 and −16 px (registered
  away), +9.38% at −24, +17.92% at −32, +18.75% at −40 and +7.76% at −56 —
  installing again. It is a BAND of roughly −20 to −48 px on this art, not a
  threshold. And it is not free — see the two entries below.

- **[READ] `normalize_cells` is load-bearing for the handedness gate, and it is
  upstream pet code this package does not own.** `agent/charsheet/pipeline.py`
  imports it from `agent.pet.generate.atlas`; it is what puts neighbouring rows
  in comparable positions at all, and round one's gate only survived contact with
  real characters because of it. It is weaker than it looks: it pins each row's
  union BOX to the frame centre (measured residual ±0.5 px — pure rounding), not
  the row's BODY, and the body still lands **up to 10 px apart between adjacent
  rows** on art that passes (measured with a column-profile cross-correlation
  across all three states of the live sheet; the largest was `jumping` `s|se`).
  Round one charged all of that to handedness. Nothing in this package would
  notice if an upstream retune changed the centring rule.
  *Consequence:* if `normalize_cells` ever stops registering, the handedness gate
  degrades silently — it does not fail, it starts refusing correct characters.
  The registration window is now the local defence, sized against that 10 px, not
  against the ±0.5 px the bbox suggests.

- **[READ] Registration costs about half the true signal, and the honest reading
  is that half of round one's "handedness" was placement.** Re-measured on real
  art on 2026-08-25, both bands moved:
  `| population | round one (no registration) | round two (window 16) |`
  `| true, pre-fix ne rows | +18.53 / +13.05 / +13.01% | +12.28 / +8.52 / +7.37% |`
  `| true, each interior row of the REPAIRED sheet mirrored one at a time | floor +8.51% | floor +4.33% |`
  `| false, the repaired sheet's nine interior rows | ≤ −9.30% | ≤ −4.53% |`
  `| false, cobalt-robot-courier (asymmetric, correct) | +1.85% | +1.72% |`
  `| false, correct art displaced 8 px | +11.01% REFUSED | −4.53% installs |`
  Note which number barely moved: the deliberately asymmetric correct character.
  Registration takes the placement out of the TRUE readings and leaves the
  genuinely-asymmetric false one where it was, so the separation narrows from
  about 7x to about 2.5x. The threshold stayed at 8% — set for specificity, since
  a false refusal used to be permanent — which means it now misses part of the
  true band by design: three of those twelve single-row mirrors fall under it.
  **"About 2.5x apart" is STRUCK 2026-08-25 (round three): it is measured
  against the wrong false population.** The false row in that table is a
  deliberately asymmetric CORRECT character, which registration handles by
  design. The false population that matters is correct art DISPLACED, and it
  reaches **+18.75%** — above every true reading in the same table. On one basis
  the two populations overlap, so there is no separation figure to quote and no
  value of the threshold that produces one.
  *Consequence:* a clean handedness pass is weaker evidence than it was, in
  exchange for a refusal that is much better evidence. Two things buy the
  sensitivity back — the cross-state pass, and the operator's eye. Neither is
  optional. **And a single-basis refusal is no longer "much better evidence"; it
  is a warning now. See the round-three entries at the end of this section.**

- **[READ] A mirrored row makes its NEIGHBOURS read high, and obeying a
  multi-row refusal spends correct approved art.** Measured on the repaired live
  sheet, mirroring one row at a time: `idle-e` alone puts `idle-e` at +13.33% and
  `idle-ne` at +11.87%; `walk-e` alone puts `walk-e` at +15.86% and `walk-ne` at
  +14.09%. Round one made every one of those a separate ERROR and every error
  said `characters reroll-row --row <that row>` — and `reroll_row` proposes then
  approves unconditionally with no `approve-row` verb anywhere in the repo, so an
  operator obeying a three-row refusal spends two correct approved attempts and
  cannot get them back. ~~The gate now reports only the LOCAL MAXIMUM of each
  contiguous run of flagged rows; the rest ride in the same finding as
  `corroborating`, and the message says "Do NOT re-roll them".~~ **The
  local-maximum rule is STRUCK in turn, 2026-08-25 (round three): the rotation's
  ranking now names NOBODY.** A run of two or more adjacent flagged rows is
  reported as "one of these N" unless a second, independent basis convicts one
  of them; only a LONE flagged row is named off the rotation alone. The
  `corroborating` list and its "Do NOT re-roll them" survive, but only on a
  finding that actually named a culprit. ~~In every case
  measured the culprit was the run's maximum.~~ **STRUCK 2026-08-25 (review):
  the culprit is the run's maximum only when exactly ONE row of the run is at
  fault, and both other reachable shapes INVERT it.** (i) A CORRECT `idle-e`
  slid −24 px — pure placement, art untouched — reads +9.38% while its untouched
  neighbour `idle-ne` reads +10.91%, so the run maximum, and therefore the row
  the refusal tells the operator to re-roll, is the innocent neighbour; the
  disturbed row rides under "Do NOT re-roll them". A 48 px one-sided prop
  inverts the same way (propped `idle-e` +0.79%, neighbour `idle-ne` +4.89%).
  (ii) With TWO mirrored rows FLANKING a correct one — `idle-se` and `idle-ne`
  mirrored, `idle-e` correct — the correct middle row wins the rotation run at
  +13.33% and is reported as a standalone `rotation` culprit; the evidence that
  it is correct (its cross-state reading of −75.71%) sits unused in the same
  payload. That shape is reachable: it is what a SECOND badly-worded diagonal in
  `VIEW_LANGUAGE` produces, the way `ne` alone produced round one's.
  *Consequence:* when a refusal names one row and mentions others, re-roll the
  named one and compose again. The synthetic glyph fixture never reproduced this
  — it flags exactly one row every time — which is why round one's tests were
  systematically more benign than reality here.

- **[READ] The same direction across STATES is a second, stronger read, and it
  is what sees a whole state drawn backwards.** The rotation pass is a fixed
  point per state — mirror all five rows of `idle`, `walk` or `jumping` and the
  chain still fits itself, `ok=True`, nothing flagged — which matters because
  `add-state` generates one state's five rows in a single batch against one
  reference and one prompt, exactly the shape of the generation that drew `ne`
  backwards three times. Comparing the same direction across states sees it
  loudly: on correct art the pairs read −8.27% to −75.71%, and with one state
  mirrored the pairs touching it read +7.64% to +43.09%. A row is convicted only
  when it still reads over the threshold against a MAJORITY of the other states,
  so it needs THREE — across one pair a disagreement cannot say which side is
  wrong, the end-row rule on the other axis. Restricted to the directions the
  rotation already judges (`se`, `e`, `ne`): `s` and `n` read ±0.2% either way
  and would only add noise.
  It also recovers what registration cost: mirroring `idle-se` alone reads only
  +4.33% in the rotation (missed) and +11.76% across states (caught).
  *Consequence:* the default `idle:6, walk:8` sheet gets NOTHING from this pass —
  it wakes up at the third state. And it is a consensus, so a character whose
  every state is mirrored is a fixed point of it too, and a 2-of-3 split convicts
  the minority. Say so rather than reporting it as certainty.
  **An EVEN split convicts EVERYBODY — added 2026-08-25 by review.**
  `majority = len(pairs) // 2 + 1` is 2 of 3 pairs on a FOUR-state sheet, so a
  2-2 split leaves every row with two disagreeing pairs. Measured on a 4-state
  sheet built from the live art with two of the four `e` rows mirrored: ALL FOUR
  are flagged as errors, the two CORRECT ones with basis `states` at +14.31%,
  none marked `corroborating`, none carrying "Do NOT re-roll them" — while their
  rotation readings sit at −15.97%. The states pass never goes through
  `_run_findings`, so it has no attribution step at all, and `SKILL.md` tells the
  agent that a multi-row `states` refusal is "the one case where re-rolling
  several rows IS right". One `add-state` takes the live character from three
  states to four.

- **[READ] One blank row used to make its whole state unjudged, silently, on a
  sheet that still installs.** `validate_sheet` records an empty row as a
  WARNING as long as one row is filled, and round one's blank guard then marked
  the entire chain unjudged and moved on — so a sheet with one empty row
  installed with no handedness answer for the other four, and said so only in a
  payload nothing read. The guard was also untested: mutating `if blank:` to
  `if False:` left all 47 tests green. A row is now judged whenever it has a
  measurable seam on EACH side, so a blank row costs its two neighbours and
  nothing else. The same rule now covers the rotation's end rows and the
  short-chain case, which retired `if len(chain) < 3:` — the other mutation round
  one left green — by making it unreachable rather than by testing it.
  *Consequence:* three special cases became one rule. If you are adding a fourth
  reason a row cannot be judged, add it as a seam that does not exist, not as a
  new branch.

- **[READ] The threshold is now pinned as an ORDERING, and the RED evidence is
  checked into the repo.** `MIRROR_GAIN_THRESHOLD` had no test at all: round
  one's suite was green from ~0.03 to at least 0.25, because the synthetic glyph
  fixture's true positives scored 29–36% — an order above the live band — so
  raising the number past `walk-ne` at 13.05%, the exact defect it shipped for,
  changed nothing anybody could see. Worse, the only real defective art in
  existence lived in one operator's hermes home under a hand-made
  `…backup-2026-08-25-nefix` folder that nothing protected. Both are now
  `tests/fixtures/charsheet/handedness_8way.webp` (the repaired sheet's three
  states at 3 frames, with the genuinely mirrored `idle-ne` in place — one true
  positive at +12.05% rotation / +15.40% states, eight correct rows, loudest
  false +5.03%) and `handedness_4way.webp` (cobalt-robot-courier, byte-copied —
  the only independent false signal anyone has measured). The test asserts
  `loudest false < MIRROR_GAIN_THRESHOLD < quietest true`, so the number reddens
  the moment it stops separating the populations, in either direction.
  *Consequence:* when you re-tune the threshold, you are re-tuning it against
  that fixture and the test will tell you. When you have a THIRD character, add
  its sheet — the false ceiling is still one asymmetric character measured once.

- **[READ] `handedness.unjudged` reached nobody, and a refusal threw the whole
  payload away.** The docstrings promised rows were "named with the reason rather
  than silently dropped" and that "a caller can always see which rows this could
  not answer for", and grep found nothing outside `pipeline.py` and its tests
  reading the key. `_cmd_characters_compose` printed `WxH`; the payload needed
  `--json`; and on the REFUSAL path `compose()` raises, so the payload was
  discarded exactly when an operator was deciding how much to trust the check.
  `pipeline.handedness_summary` is now one line on both paths — the compose
  sentence carries it on success, and the raised `ValueError` carries it plus "a
  refusal is not a full audit". On the live sheet it reads *handedness: 9 row(s)
  judged, 6 unjudged (idle-n, idle-s, jumping-n, jumping-s, walk-n, walk-s)*.
  *Consequence:* read that line back to the operator. Six of fifteen unjudged is
  the normal state of an 8-way sheet, not a fault — but it is the difference
  between "the check passed" and "the check passed on the nine rows it can see".

- **[READ] There IS a way past a refusal now, it names rows, and it is recorded.**
  Round one shipped no override on the reasoning that the only correct answers
  are "reroll" or "fix the gate". The premise changed: the false-positive driver
  is FRAMING, not asymmetry, registration bounds that class rather than removing
  it, and the two populations are 2.5x apart on the two characters anyone has
  ever measured. `compose --accept-handedness idle-e,walk-ne` installs despite
  those rows' findings, writes them to the installed manifest as
  `handednessAccepted`, keeps every other flagged row refusing, and turns the
  accepted findings into warnings that still carry the whole refusal text.
  Naming a row that was NOT flagged is itself an error, so the flag cannot be
  carried in a command line as boilerplate and quietly disarm the check the day
  a row is genuinely wrong.
  *Consequence:* it is per ROW and it is a record, not a bypass. Use it only
  after looking at the strip with the operator, and say in the turn what you saw.

- **[READ] Three round-one figures do not reproduce; here are the measured
  ones.** (a) "~1 s on a 15-row sheet" was **0.08 s** for the unregistered gate
  and is ~~**~4.0 s**~~ **5.7–10.7 s** (three clean runs, 2026-08-25 review:
  10.70 / 5.74 / 6.31 s for `detect_mirrored_art`; `validate_sheet` end to end
  6.46 s, which is what an operator actually waits for on `compose`)
  for the registered one on the live 15-row sheet — the shift
  search is 33 distance evaluations per frame pair per orientation. It errs
  safe either way, but say the real number. (b) The call-site comment said the
  check runs "only on a sheet whose geometry already holds"; it runs
  UNCONDITIONALLY after the collapse/outlier/residue checks — only the wrong-SIZE
  early return short-circuits it. (c) `if as_drawn <= 0: continue` dropped a row
  from `flagged` AND from `unjudged`, the one place the module broke its own
  accounting rule; such a row is now unjudged with the reason, because a row that
  simply vanishes from the payload reads exactly like a clean one.

- **[READ] A single mirrored row can now ship CLEAN on real three-state art —
  measured, not inferred.** Mirroring each of the live sheet's nine interior rows
  one at a time, 2026-08-25: `jumping-se` reads **+6.78% in the rotation and
  +7.64% across the states** and is flagged by NEITHER pass, so that sheet
  composes, installs and bundles with no refusal and no warning. `idle-se` is the
  near-miss the round-two entries already name (+4.33% rotation, recovered at
  +11.76% across states); `jumping-se` is the one the cross-state pass does not
  recover, because 8% sits inside its band too. The other seven are caught
  (+9.12% … +15.86%). And the row the gate was BUILT for is only just inside it:
  on the preserved pre-fix sheet the three genuinely mirrored `ne` rows read
  +12.28 / +8.52 / **+7.37%** — the third is UNDER the threshold, and the
  install is refused only because the first two are over. The cross-state pass
  says nothing there at all (−80.67%), because all three states were mirrored
  the same way and a unanimous consensus convicts nobody.
  *Consequence:* "compose passed" now means "no single row cleared 8% on the two
  neighbourhoods it can see". Say the sentence, not the word "clean". Look at
  `se` rows in particular — every miss measured so far is a `se`.

- **[READ] On the DEFAULT two-state sheet the gate sees ONE isolated mirrored
  row and nothing else.** `characters start` creates `idle:6, walk:8`, and the
  cross-state pass needs three states, so on the default sheet only the rotation
  answers. Measured on a two-state cut of the live art: a WHOLE state mirrored
  scores **bit-identical** to the correct sheet (−4.53 / −15.38 / −13.63% …,
  nothing flagged) — it is a fixed point per state; and two ADJACENT mirrored
  rows (`idle-e` + `idle-ne`) also pass clean, their rotation gains going
  NEGATIVE (−4.75%, −13.47%) because a contiguous block is only visible at its
  edges. Both are caught the moment a third state exists (+17.00% / +10.25%
  across states).
  *Consequence:* a two-state character gets the weakest form of this check.
  If the operator cares, the cheapest sensitivity available is a third state —
  say that rather than quoting the gate's separation figures at them.

- **[READ] Two things the code promises are not pinned by any test, and both
  survived a round trip.** Re-run of the sixteen-sabotage exercise on
  `1b95ce0b62`, 2026-08-25, over `tests/agent/test_charsheet_pipeline.py`:
  (a) `REGISTRATION_WINDOW_DIVISOR = 12` → `6` (window 32 px) and → `3`
  (window 64 px) both leave **all 62 tests green**, while moving the
  false-refusal boundary on correct art from −24 px to −40 px to past −56 px.
  Nothing asserts the window's size, and nothing asserts the thing it is FOR —
  that a slide LARGER than the window is still caught.
  `test_sliding_a_correct_row_sideways_is_not_handedness` slides 8 px, inside
  every one of those windows.
  (b) `_finding_from_run`'s `culprit = max(run, key=…gain)` → `run[0][1]` also
  leaves all 62 green: in the fixture the mirrored row is the FIRST of its run,
  so "the run's maximum" and "the first of the run" are the same row and the
  fixture cannot tell the repair from the bug — the same too-weak-pair defect
  `1b95ce0b62` fixed twice, recurring a third time one function over.
  Also measured: the threshold pin
  (`test_the_threshold_sits_between_the_two_measured_populations`) tolerates any
  value in **0.0503 … 0.1205** on its fixture. Raising 0.08 → 0.11 does go red,
  but through `test_an_end_row_is_not_blamed_…` (cobalt's 10.48% single seam)
  and `test_sliding_a_correct_row_…`, not through the test written for it: the
  fixture's only true positive is a single loud row at +12.05%, and the real true
  floor on live art is +4.33%. The fixture does not contain the band where the
  two populations actually meet.
  *Consequence:* if you re-tune the window or the attribution, the suite will not
  tell you. Measure on the live sheet and write the number down here.

- **[READ] `normalize_cells`'s residual eats 10 of the 16 px window on art that
  PASSES — confirmed, with the right instrument.** The "up to 10 px" figure in
  the entry above is right, and an alpha-weighted column centroid does NOT
  reproduce it (that reads 3.7 px). The number that matters is the shift
  `_registered_distance` itself chooses: measured over every adjacent seam of the
  live sheet, direct shifts run −5 … **+10** px, the largest on `jumping s|se` —
  the pair the entry names. So ordinary correct art already consumes 62% of the
  window, leaving ~6 px of headroom before a real displacement starts eating
  judgement. Nothing in this package pins that: the charsheet fixtures are
  PRE-COMPOSED sheets, so an upstream retune of the centring rule cannot redden
  them — only live composes would drift.
  *Consequence:* ~~the cheap pin is a charsheet-side test that composes a
  multi-row set through `compose_draft_frames` and asserts the per-row registered
  shift stays inside `registration_window(frame_w)`.~~ **STRUCK 2026-08-25
  (round three): that test was written, and it does NOT detect a
  `normalize_cells` regression.** Replacing `normalize_cells` with a fit that
  centres nothing leaves every composed shift at **0**, because
  `extract_strip_frames` content-crops each frame per slot BEFORE the centring
  runs — on this path EXTRACTION, not centring, is what puts neighbouring rows
  in comparable positions. The test is worth having for what it does measure —
  the composed shift budget on real art, 9 px of 16 with a 32 px prop
  saturating the window — and its detection floor was measured by injecting a
  per-row drift: red at 6 px, green at 4 px. Read it as a pin on the composition
  path's shift budget, not on the upstream centring rule. "If `normalize_cells`
  ever stops registering" still has no detector, and now we know why one is hard
  to write.

- **[READ] THE RULING: the threshold is not the lever, so it did not move. What
  moved is what ONE reading is allowed to DO.** Measured in both directions, the
  two populations do not separate at 8% and no other value separates them
  either. The true floor is BELOW the line on real art — mirroring each interior
  row of the repaired sheet one at a time, `jumping-se` reads **+6.78% rotation
  / +7.64% states** and is flagged by NEITHER pass, so that sheet composes,
  installs and bundles clean. The founding defect sits AT the line: the pre-fix
  sheet's third mirrored row, `jumping-ne`, reads **+7.37%**, and the install was
  refused only because two of three cleared it. And the false ceiling is far
  ABOVE that floor once art is stressed — a CORRECT `idle-e` slid sideways reads
  +9.38% at −24 px, +17.92% at −32, **+18.75%** at −40 and +7.76% at −56, a BAND
  of roughly −20 to −48 px rather than a threshold. `max(false) ≈ +18.75%` and
  `min(true) = +7.64%`: the line sits inside both populations. So a
  **rotation-only or states-only** finding is now a **WARNING** that does not
  block `compose`, and **ERROR** is reserved for **two independent bases
  agreeing about the same row** (`basis: "rotation and states"`).
  *Consequence:* say the sentence, not the word "clean" — and say the cost out
  loud. `characters start` creates `idle:6, walk:8`; two states have no
  cross-state pass; so **on the default character this gate can only ever
  warn**. That is the right admission rather than a regression: measured on a
  two-state cut of the live art, a whole mirrored state scores bit-identical to
  the correct sheet and two adjacent mirrored rows pass with their gains going
  NEGATIVE. It was already nearly blind there, and blocking on one weak reading
  bought confidence it did not have. **Read the warnings out to the operator** —
  `compose` prints every one on the human line now, and nothing else stands
  between a single-basis reading and a shipped mirrored row.

- **[READ] The rotation's RANKING never names a row, because in two reachable
  shapes the loudest row of a run is the innocent one.** Both reproduce on the
  checked-in fixture and both are now tests. (i) PLACEMENT: `walk-e` slid −24 px
  — correct art, a displacement half again the 16 px window — reads +10.48% and
  drags its UNTOUCHED neighbour `walk-ne` to +10.68%, so "the run's maximum"
  named the innocent neighbour and filed the disturbed row under "Do NOT re-roll
  them". (ii) FLANKED: mirror `idle-se` and `idle-ne` and leave `idle-e`
  correct — what a SECOND badly-worded diagonal in `VIEW_LANGUAGE` produces —
  and the correct middle row wins the run at **+14.28%** while the evidence
  exonerating it, a cross-state reading of **−97.62%**, sat unused in the same
  payload. The rule now: a run of two or more adjacent flagged rows is named
  only when a second, independent basis convicts exactly one of them; otherwise
  it is reported as *"one of N rows … and this pass cannot say which"* with the
  rows ranked and NO `reroll-row` command. A lone flagged row is still named,
  which is what keeps the founding defect attributable.
  *Consequence:* three message shapes now, and they mean different things. A
  REFUSAL names a culprit and hands you the `reroll-row` line. A WARNING names
  one row and asks you to crop it. An UNATTRIBUTED finding names none. Never
  re-roll off the third, and never off a row under "Do NOT re-roll them".

- **[READ] A negative cross-state reading is NOT a character reference, and the
  obvious version of this repair would have retired the defect the gate was
  built for.** "A row the other states vouch for cannot be the rotation culprit"
  reads well and is wrong: a direction mirrored in EVERY state is a fixed point
  of the cross-state pass and reads strongly negative there PRECISELY BECAUSE it
  is consistently wrong. Measured on the fixture, the two cases are numerically
  identical in shape — flanked leaves the correct `idle-e` at −97.62%, and the
  founding defect (`ne` mirrored in all three states) leaves each mirrored row
  at −111.32%. What separates them is not the sign: it is that `ne` is over the
  line in the rotation of THREE states out of three there, and `e` is over it in
  ONE of three in both inversion shapes. So a contradicted row is demoted UNLESS
  its direction is suspected in a strict majority of the rotations.
  *Consequence:* when you are tempted to exonerate a row with the other pass's
  silence, check whether that pass CAN speak about it. A unanimous consensus and
  a clean bill of health produce the same number.

- **[READ] The cross-state "majority" was not a majority on an EVEN number of
  states, and one `add-state` reaches four.** `majority = len(pairs) // 2 + 1`
  counts the OTHER states, so on a four-state sheet it is 2 of 3 and a 2-2 split
  leaves every row inside a "majority". Measured on a four-state sheet built
  from the live art with two of four `e` rows mirrored: **all four flagged as
  errors**, the two CORRECT ones at **+14.31%** basis `states` with no
  corroborating marker and no "Do NOT re-roll them", while their rotation
  readings sat at −15.97% — and `SKILL.md` told the agent that a multi-row
  `states` refusal is "the one case where re-rolling several rows IS right". It
  is now `len(drawn) // 2 + 1`: a strict majority of ALL the states, i.e. a row
  is convicted only when its camp is a strict MINORITY. An even split convicts
  nobody and the direction is reported unjudged with *"the states split
  evenly"*. The states pass also runs through the same attribution step as the
  rotation now, and its `len(ranked) < needed` case is an `unjudged` entry
  rather than a bare `continue` — the last silent drop in the module.
  *Consequence:* a four-state character gets a WEAKER cross-state read than a
  three-state one, not a stronger one, whenever the mirror is even. Five states
  is the next number that cannot tie.

- **[READ] Two knobs were pinned by nothing and a third was over-specified.**
  Re-run of the sabotage exercise, 2026-08-25. `REGISTRATION_WINDOW_DIVISOR`
  12 → 6 and 12 → 3 both left all 62 tests green while moving the false-refusal
  boundary from −24 px to past −56 px; the one test that slides a row moved it
  8 px, inside every candidate window. It is now asserted directly
  (`registration_window(192) == 16`) plus the complement the window exists FOR:
  a slide of exactly the window registers away to within two points of the
  unslid reading, and a slide half again the window crosses the line. (A slide
  is not a pure translation, incidentally — sliding a row inside its own cell
  clips the art at the cell edge, so "registers away exactly" is not available
  and the test says so.) The culprit rule was the second, and it is gone rather
  than re-pinned. The third: `max(convicted, ...)` ranked two cross-state
  convictions inside one run — a branch NO fixture can reach, because two
  ADJACENT mirrored rows never form a rotation run at all (`idle-e` + `idle-ne`
  mirrored read −5.30% / −15.36%, a block being visible only at its edges). It
  is now `len(convicted) == 1`, so the unreachable ranking is deleted instead of
  left untested.
  *Consequence:* when a fixture cannot reach a branch, DELETE the branch. The
  too-weak-fixture defect has now recurred four times in this module; three of
  those four were a knob that no reachable input could distinguish, and the
  repair each time was to make the code say the thing the fixture can see.

- **[READ] The override records what it let through, and the record now
  reaches somebody.** Three things were wrong with round two's half. The human
  path never showed the refusal text — `_characters_emit` prints one line and
  `validation["warnings"]` needs `--json`, so a successful `--accept-handedness`
  printed a row count and nothing else; `compose` now prints every warning on
  the human line, which matters twice as much since single-basis findings live
  there too. `handednessAccepted` had ZERO readers anywhere in either repo; it
  is now republished by `characters list` and by `sprite_payload`, which is what
  lets the launcher's `bundle_character.dart` read it without decoding a pixel.
  And it recorded row keys only, so accepting a +40% finding and an +8.1% one
  were indistinguishable afterwards; it is `{row, gain, basis}` now (old bare-key
  manifests still parse, as `basis: "unrecorded"`).
  **The grammar changed with it:** a row refused on two bases is refused by two
  independent bodies of evidence, and one row name waived both — so an operator
  overriding a PLACEMENT reading also silenced the cross-state one, which
  placement cannot explain. The token is `<row>:rotation+states`; a bare row is
  refused with the spelling it needs, and a WARNING cannot be accepted at all
  because it never blocked.
  *Consequence:* `--accept-handedness idle-e` from a shell history no longer
  works even on a row that is genuinely refused. Read the refusal, name the
  bases, and say in the turn what you saw on the strip.

- **[READ] The timing figures, re-measured, and what was done about them.**
  Round two said "~4.0 s registered"; the review measured 5.74 / 6.31 / 10.70 s
  for `detect_mirrored_art` and 6.46 s for `validate_sheet` end to end. On the
  3-frame checked-in fixture here: `detect_mirrored_art` **2.28 s**,
  `validate_sheet` **3.0 s**, of which the RGB-residue scan was **0.39 s** — a
  pixel-by-pixel Python loop over 1.8M pixels running on every compose. It is
  now three Pillow band operations, **0.031 s**, 12x, with identical counts on a
  clean sheet and on one with residue injected. The suites also cache each
  distinct MUTATED fixture sheet instead of re-deriving it per test. Net on the
  five charsheet suites: round two 357 passed in 311.96 s, round three **377
  passed in 228.20 s**, worst single test **8.73 s** against the 30 s cap (29%,
  from 58%).
  *Consequence:* the headroom question is answered by making the work cheaper,
  not by raising the cap. If you add a test that composes or validates a real
  sheet, put its mutation through the `variant_*` cache — two tests wanting the
  same broken sheet should pay for it once.

---

## The two 2026-08-25 owner rulings (appended by the hermes charsheet slice)

- **[READ] `cardSafe` is GONE. Two booleans ride in every `thumb` payload, and the
  consumer rule is BOTH.** The owner ruled 2026-08-25 that one name was carrying two
  guarantees, and split it rather than re-aiming it:
  - **`withinConsoleBudget`** — under `pipeline.MAX_CONSOLE_CARD_PIXELS` (the old
    `MAX_CARD_PIXELS`, same value, honest name): a module constant sized once from
    `CHAR8.sheet_size()` = 1536x2080 = 3,194,880 px. It does NOT move with a draft.
    *Will this file sink the console?* This is the only one that REFUSES anything — a
    crop at `--scale 2` or below over it is refused, exactly as before.
  - **`withinOwnSheet`** — `pipeline.fits_own_sheet(w, h, spec)`, computed from THIS
    draft's own `spec.sheet_size()` every time. *Did cropping buy anything?* — launcher
    risk D.3's actual check. It refuses nothing and is reported at every scale.
  Both are `<=`, both are always present, and neither may be inferred from the other.
  *Consequence:* declare a crop with `MEDIA:` — and draw it in a launcher card — only
  when BOTH are true. Otherwise route it to the fullscreen viewer, and say WHICH bound
  it missed: over the console ceiling is an unsafe decode, over your own sheet is a
  safe picture that mitigated nothing. Stop computing the second one by hand from
  `status --json`; that instruction (which this file gave twice) is retired by the flag.

- **[MEASURED] The two flags disagree in BOTH directions, on drafts that exist — which
  is the whole proof that one boolean could not carry both.** Re-verified at the code
  2026-08-25 before implementing, and both are now pinned as tests:
  - `--directions 4`, `idle:2` composes 384x624 = **239,616 px**; its DEFAULT crop
    (`--frame 0 --scale 2`) is 1774x1774 = **3,147,076 px** →
    `withinConsoleBudget: true`, `withinOwnSheet: false`. **13.1x its own sheet**, and
    1.5% under the console ceiling.
  - `add-state --state jumping:6` on the 8-way 2-state sheet recomposes at 1536x3120 =
    **4,792,320 px = exactly 1.50x** the console ceiling, which does not move. A crop
    there can be `withinConsoleBudget: false` and `withinOwnSheet: true` at once (a
    2400x1000 `jumping-e` strip at `--scale 3` measures 3,600,000 px, between the two).
  *Consequence:* a change where the two flags always agree has not been tested. The
  sabotage that proves it: make `fits_own_sheet` return `fits_console_budget` (the
  pre-split state) — both measurements above go red.

- **[READ] A WHOLE mirrored STATE is now an ERROR, on one basis or two.** The rule the
  round-three test file proposed in its own docstring — "a `states` finding covering
  EVERY judged row of one state is an error, which is a second-order consensus and not
  a second basis" — is the code as of 2026-08-25. Two guards, both load-bearing:
  **at least two rows** (a 4-way scheme cross-state-judges exactly ONE row per state,
  and the ruling explicitly leaves a single mirrored row a WARNING), and **every judged
  row, never a majority** (one row of the state judged CLEAN is a contiguous block of
  bad rows, not a bad state — the sheet is contradicting the conclusion). Flagged rows
  carry `wholeState`: that state's flagged rows in sheet order, so the message names
  the fault rather than one row of it.
  *Consequence:* when the cross-state pass fires on every row of a state, suspect the
  state's REFERENCE and re-roll the state. Do not wait for the rotation to agree — it
  is a fixed point of a wholly mirrored state and will never agree.

- **[READ] The acceptance grammar had NO legal spelling for a one-basis error, and that
  was a wall.** Before this change `ACCEPT_BASIS_TOKEN = "rotation+states"` was a single
  hardcoded constant and `validate_sheet` refused every other token (there is a test
  asserting `idle-ne:states` is answered with "is spelled idle-ne:rotation+states").
  Promoting a `states`-only finding to an error without touching that would have
  produced an error with no reachable override, on the verb that has no other door.
  The token is now DERIVED from the finding — `pipeline.accept_basis_token(basis)` —
  and the refusal that demands it and the message that teaches it call the same
  function, so they cannot drift.
  *Consequence:* **what an operator types for a whole-state refusal is
  `--accept-handedness jump-se:states,jump-e:states,jump-ne:states` — one token per
  row, spelled `states`, never a blanket.** Take the token from the refusal text; it
  prints the one the validator will accept.

- **[MEASURED] The default two-state character reaches NEITHER refusal, and the
  whole-state rule does not change that.** Verified at the code before implementing:
  `CHAR8` is `idle:6, walk:8`, the cross-state pass takes its `len(drawn) < 3` unjudged
  branch for every direction, `state_flagged` stays empty, so no finding can reach
  `basis: "rotation and states"` and no whole-state consensus exists either. Mirror an
  ENTIRE state on a two-state cut and the check still only warns.
  *Consequence:* this is not a gap the ruling failed to close — it is the same algebra
  as the rotation's end rows. With one witness there is no consensus to take. The
  cheapest sensitivity available is still a third state; say that to the operator
  rather than quoting separation figures.

- **[INFERENCE, not yet measured on real art] The whole-state rule buys blocking power
  and takes on a whole-state FALSE positive with it.** The measured false population is
  a CORRECT row displaced sideways, up to +18.75%, and `normalize_cells` centres each
  row's union box — so a prop drawn on one side in EVERY direction of one state (a bag,
  a cape, a sheathed sword added with the state) displaces every row of that state at
  once, which is the exact shape this rule convicts. Nobody has produced that sheet.
  *Consequence:* this is why the override had to work for the whole-state error, and
  why it is per row. If you hit a whole-state refusal on art you have LOOKED at and
  believe, accept it row by row and say in the turn what you saw on the strips.

- **[READ] `Path.write_text` flips an LF file to CRLF on Windows, and three of these
  source files are LF.** Patching `pipeline.py`, `draft.py` and `harness.py` through
  `read_text`/`write_text` produced a 17,000-line whole-file diff before a single real
  change was visible in `git diff --stat`. `write_text` opens with `newline=None`,
  which translates `\n` to `os.linesep`.
  *Consequence:* patch these files with `read_bytes`/`write_bytes` (or an explicit
  `newline=`), and check `git diff --stat` before believing a diff. In this package:
  `pipeline.py`, `draft.py`, `harness.py`, `SKILL.md`, `FIELD-NOTES.md` and
  `tests/agent/test_charsheet_pipeline.py` are **LF**;
  `tests/agent/test_charsheet_draft.py`, `tests/hermes_cli/test_harness_characters_cli.py`
  and `scripts/verify_harness_skill_install.py` are **CRLF**.

- **[READ] A `SheetSpec` is unhashable, so a fixture helper that takes one cannot be
  `lru_cache`d.** `DirectionScheme.mirrored` is a dict; `four_state_sheet` gets away
  with the cache only because it takes the mutation tuple and rebuilds the spec inside.
  *Consequence:* a new fixture helper that accepts a spec drops the cache (or keys on
  the fixture NAME the way `variant` does).


---

## Appended by slice

### The handedness refusal as a MESSAGE (2026-08-26, found by running the verb)

- **[MEASURED] The refusal was ONE line of 1206 characters, and 1519 when the operator
  spelled the acceptance wrong — of which 1206 was the refusal they had just read.**
  Measured on a throwaway copy of the live alice home with `walk-e`'s approved attempt
  flipped, `compose` refusing at exit 2. Plain refusal: 1206 chars, 1 line, longest line
  1206. `--accept-handedness walk-e` (a bare row name): 1519 chars, 1 line — the
  acceptance complaint, then the ENTIRE finding diagnostic again underneath it, because
  the two were separate entries in `validation["errors"]` about one row and `draft.py`
  joined them with `"; "`. So the message for getting the flag wrong was LONGER than the
  message that taught the flag, and 79% of it was a second copy. After the repair:
  12 lines, longest **286**, and the bare-acceptance case grows by the complaint only
  (13 lines, longest 334) because the complaint folds into the block for the row it
  names. One row is one block.
  *Consequence:* the launcher console card (slice B2) renders exactly this text. When you
  add a fact to a finding, add a LINE, never a clause — and never a second `errors` entry
  about a row that already has one.

- **[READ] THE DECISION on the escape hatch: it is SHOWN, on both blocking shapes.**
  It was reachable only by guessing the flag existed on the shape an operator actually
  hits. This was worth deciding rather than patching, because not advertising an override
  that lets bad art ship is a defensible design — but that was not what shipped. The
  whole-state branch of `mirrored_art_error` spelled `--accept-handedness` and the
  two-basis branch did not, so what existed was a DRIFT between two spellings of one
  thing, which is the class `accept_basis_token` exists to be the only copy of. Three
  things settled it. (1) Ruling 18 tightens the gate *because* the row-named override is
  reachable; an override an operator can only find by guessing is not reachable in any
  sense that argument can use, and `validate_sheet`'s own docstring already says an error
  with no way past it is a wall and `compose` has no other door. (2) The refusal can be
  wrong — `max(false) ≈ +18.75%` against `min(true) = +7.64%`, and two bases can be
  fooled by ONE displacement — so an operator who has LOOKED at the strip is better
  evidence than the reading, and telling them only to re-roll spends the correct approved
  art they just judged. (3) The costs run the other way from the prose: a re-roll is
  private, silent, auto-approving and has no undo verb, while an acceptance is a
  permanent public record (`{row, gain, basis}` on the manifest, republished by
  `characters list`, `sprite_payload` and the launcher's bundle warnings, decision 19).
  The message was promoting the irreversible unrecorded action and hiding the recorded
  one. **How it is shown so it is not the easy door:** after the `re-roll` line, never
  before it (asserted); opening "only if you have LOOKED at this row's strip"; carrying
  its own price on an `on record` line; and absent entirely from warnings and from
  unattributed findings, which say to reach for it only on a finding that actually
  blocks.
  *Consequence:* take the token from the refusal, never from shell history — it prints
  the one the validator will accept, per finding. And say in the turn what you saw on the
  strip; the acceptance outlives the session and names you nowhere.

- **[READ] The cost of obeying a wrong name rode on an OPTIONAL clause, and the founding
  defect's own shape did not get it.** `_REROLL_IS_ONE_WAY` ("a re-roll auto-approves and
  there is no approve-row verb to undo it") was quoted by the warning branch, the
  unattributed branch and the corroborating tail — but the corroborating tail only exists
  when a neighbour rode along. The checked-in 8-way fixture's `idle-ne` finding has
  `corroborating == []`, and that is exactly the founding `ne` defect: one isolated
  flagged row per state with clean neighbours. So the two ERROR branches — the only two
  that hand over `characters reroll-row` — told an operator to spend approved art and
  never said the spending was one-way. It is now on every branch's action line, stated
  once per block.
  *Consequence:* when you add a branch that names a verb, the one-way sentence goes on
  the line that names it, not on a tail that may not render.

- **[READ] The message does NOT choose a wrap width, and must not.** The shape is a
  headline that stands alone plus one `label: value` line per fact, with no hard wrap.
  A column count picked inside `pipeline.py` is right for an 80-column terminal and wrong
  for every other consumer, and a card that re-wraps hard-wrapped text looks worse than
  one that never received it. What both consumers actually need is SEPARABLE FACTS: the
  terminal soft-wraps each field, the card wraps each on its own width or shows the
  headline and discloses the rest. The raise in `draft.compose` leads with the failure
  and then the handedness accounting, so a surface that shows two lines shows what failed
  and how far the check could see — that sentence used to be 1100+ characters into the
  single line, the worst place for it on the surface with the least room.
  *Consequence:* `mirrored_art_error` returns a BLOCK. `hermes_cli/harness.py` indents
  continuation lines under `  warning: `; anything else that prints it must do the same
  or the block falls back to column zero and reads as separate output.

- **[MEASURED] The trap is still live, and it is a message pin standing in for a severity
  pin.** `test_the_whole_state_message_names_the_state_and_the_override_it_accepts`
  PASSES against the sabotage that deletes the whole-state refusal outright — remove
  `or finding.get("wholeState")` from the severity rule and a wholly mirrored state stops
  blocking, while that test stays green, because `mirrored_art_error` branches on
  `wholeState` BEFORE it looks at severity, so the text still says WHOLE STATE and still
  prints the token. Verified applied and reverted 2026-08-26: the sabotage reddens
  `test_a_whole_state_drawn_mirrored_is_caught_across_the_states`,
  `test_the_whole_state_refusal_is_overridable_row_by_row` and the new
  `test_a_refusal_hands_over_a_token_that_actually_reopens_the_compose[whole-state]` —
  the message test is one of the three that survive it.
  *Consequence:* a test that reads `mirrored_art_error` is testing the SENTENCE. If the
  guarantee is "this refuses", assert `validate_sheet(...)["ok"]` and the severity in the
  same test, or the sentence will keep being true after the refusal is gone.

- **[READ] The round trip is the honest test of an override, and a grep for the flag name
  is not.** `test_a_refusal_hands_over_a_token_that_actually_reopens_the_compose` pulls
  every `--accept-handedness <token>` out of the printed refusal with a regex, feeds
  exactly those back to `validate_sheet`, and requires the install to go through. It
  fails both ways a message can lie — by not offering a token, and by offering one the
  validator rejects. Measured: replacing `accept_basis_token(...)` with a hardcoded
  `rotation+states` reddens it on the whole-state shape (plus two older tests), which a
  grep for `--accept-handedness` would not.
  *Consequence:* if a third blocking basis is ever added, this test covers its override
  for free as soon as the shape is added to its parametrize list. Add the shape, not a
  new assertion about spelling.

### AF — the rewrite itself (2026-08-27, the last slice)

- **[READ] `SKILL.md` claimed a backticked `MEDIA:` line renders nothing, and the launcher
  had already stopped agreeing.** `local_document_reference.dart::parseDeclaredMediaLine`
  runs `_unwrapWholeLineCodeSpan` FIRST: a line that is EXACTLY one inline-code span —
  `` `MEDIA:...` `` and nothing else — is unwrapped and still renders, on the stated
  reasoning that the backticks are formatting habit rather than content. What genuinely
  un-declares a path is a FENCE (fenced lines never reach the parser at all), a backtick
  span inside a sentence, or two spans on one line. It also tolerates a trailing `.`, `,`
  or `;`, requires the payload to be exactly one path, and renders `png jpg jpeg gif webp`
  as images while routing anything else to the document lane — the skill said "`.png` and
  `.webp`".
  *Consequence:* the served copy was scaring an agent off a shape that works and was silent
  about the three that do not. Corrected in the rewrite. This is the class the "For AF"
  entry above names — a false line in `SKILL.md` reaches every turn and the strike reaches
  nobody — except that here the falsification arrived from the OTHER repo, which is the
  half that file's rule does not cover.

- **[READ] The launcher's QA card cites a `SKILL.md` section by NAME, and until this commit
  the name was wrong.** `mission_sheet_qa_row.dart`'s `kMissionSheetQaPrefix` doc comment
  reads *"The skill emits the upper-case form (`harness-charsheet-authoring/SKILL.md`,
  'Rendering contract')"*. There was no section called that — the lines lived under
  "Talking to the operator". Nothing on either side of the seam could notice: the hermes
  gate pins STRINGS inside the skill, never headings, and a Dart doc comment is prose.
  *Consequence:* the rewrite names the section "Rendering contract" so the citation is
  true. A cross-repo reference to a HEADING is a pin nothing holds; if that section is ever
  renamed again, the launcher comment goes stale silently.

- **[READ] The card DISPATCHES, so a draft can move between your turns.**
  `MissionSheetQaScope` hands every card a dispatcher bound to the console's own intent
  chokepoint, and `resolveMissionSheetQaCapability` lowers five sealed arms onto
  `characters.approve-direction`, `.reroll-direction`, `.reroll-row`, `.reopen` and
  `.compose`. No agent turn is involved. Two consequences the skill now teaches: re-read
  `status --json` at the start of a turn rather than trusting your memory of the stage
  (a reroll auto-approves, so the approved pointer can move without you), and the card's
  Compose carries **no** `--accept-handedness` — overriding a refusal is the one thing only
  the agent can do.

- **[MEASURED] The rewrite took the package from 26,042 B to 44,478 B, and nothing measures
  that.** This is a `required_preload` skill: `mission_chat_turn_context` puts `SKILL.md`'s
  whole body into every turn of every persona that lists it, so the file's size is a
  per-turn context cost paid forever. The pre-push install gate compares HASHES, the policy
  test asserts strings, and neither has an opinion about length — a 70% growth landed with
  nothing reporting it.
  *Consequence:* when you add to this file, delete something. Filed on the launcher's
  Mission Control queue as the missing measurement it is.

- **[READ] Risk D.5's cross-repo pin still has only its hermes half.**
  `tests/fixtures/charsheet_qa_line.json` says in its own `note` that the twin at
  `EterniaLauncher/test/fixtures/charsheet/charsheet_qa_line.json` is B2's to add. B2 has
  landed (the QA card, the lift, the refusal parser) and that file does not exist —
  `test/fixtures/charsheet/` holds `handedness_refusal.txt`, `list.json`, `sprite.json`,
  `status.json` and `thumb.json`, and nothing else. So the `CHARSHEET-QA:` key set is
  pinned on the PRODUCER side only: change what the skill promises and the hermes fixture
  moves with it, while the launcher's parser goes on accepting a shape nobody emits.
  *Consequence:* the line's contract is one-sided today. Filed launcher-side.

- **[NOT VERIFIED — say this out loud] Nothing in this program has been watched working.**
  AF is a documentation slice and it did not change that. The skill's gate ran (25 tests,
  including the live-argparse verb-table pin, round-tripped over a planted `add-states`),
  the install hash matched, and **no live authoring turn was driven and no Stage C capture
  was taken**. Every sentence in `SKILL.md` about what the operator SEES is read off
  launcher source, not off a screen. An agent that reads this file should treat the
  console-side claims as code-derived, and an agent that gets to run the loop should append
  what actually happened here.

### W5 — the correctly-attributed proof draft (2026-08-27, operational, no code)

Strip §W5 of the launcher's `docs/spatial/CHARA_GAP_CLOSURE_WAVE_2026-08-27.md`: no
worktree, no source change. This entry is the strip's only commit. The strip exists
because every draft on disk failed an EARLIER gate than the one that wave is fixing, so
`CharacterResumeThisLane` could not be proven on any of them.

- **[VERIFIED] The bare-shell home trap is not an UNSET variable — it is a set one, and
  that is why it is easy to miss.** This shell had `HERMES_HOME=X:\Eternia\.hermes` — the
  runtime ROOT, not a profile — and `harness status --json` answered
  `runtime_health.hermes_home = X:\Eternia\.hermes\profiles\alice`,
  `hermes_profile = alice`. Nothing is missing and nothing warns; the resolver simply
  falls through to the active profile. Re-run with
  `HERMES_HOME=X:\Eternia\.hermes\profiles\base` and the same field answers
  `X:\Eternia\.hermes\profiles\base` / `base`.
  *Consequence:* "the variable is set" is not evidence you are in the launcher's home.
  Echo `runtime_health.hermes_home` — the RESOLVED value — before and after any write.

- **[VERIFIED] The launcher lane's home is `…\profiles\base` by the launcher's own
  construction, not by convention.** `HermesProcessIdentity.hermesProfile` defaults to
  `'base'` and `resolvedProfile` falls back to `'base'` on a blank setting;
  `toProcessEnvironment()` builds `HERMES_HOME` as `<root>\profiles\<resolvedProfile>`.
  So a launcher with no profile override reads exactly the home this draft was written to.

- **[VERIFIED] `base` resolves — checked against the launcher's OWN definition of
  "resolves", not against the file listing.** The refusal
  `CharacterResumePersonaUnresolved` fires when
  `resolver.resolveAgentTarget(personaId).consoleTargetId` is null, and that resolver is
  built from the frame's ROSTER OF INSTANCES — not from `agent-runtime/agents/*.json`.
  Two live reads, both under the base home: `harness agent list --json` carries
  `{"id": "base", "name": "Base Agent", "state": "available", "role": "profile"}`, and
  `harness snapshot --json` carries `persona_instances.personainst_base` with
  `persona_id: "base"`, `state: idle`, `skills: [harness-runtime-model,
  harness-charsheet-authoring]`. A persona-keyed id with at least one instance row
  resolves through channel (3) of `resolveAgentTarget`, so `consoleTargetId` is non-null.
  *Consequence:* the roster to verify against is the snapshot's `persona_instances`, and
  the check is "does some row carry `persona_id: <what I am about to write>`". Reading
  `agents/` answers a NEARBY question.

- **[VERIFIED] `chara_a2` is a slug fragment of a REAL instance id, which is exactly why
  it fails.** The snapshot holds `persona_instances.personainst_chara_a2_7b31d0e4`, whose
  `persona_id` is **`base`** and whose `display_name` is "Chara A2 - Tier1 authoring". So
  the agent that wrote `authored_by: "chara_a2"` was not inventing a name: it wrote the
  middle of its own instance id (or a slug of its display name — the two coincide here),
  dropping both the `personainst_` prefix and the `_7b31d0e4` suffix. `chara_a2` is
  therefore not an instance id, not a persona id and not a role, and
  `missionIdLooksLikeInstance` will not even keep it verbatim.
  *Consequence:* the skill's rule ("copy what is inside the parenthesis") is right, and
  the near-miss is the dangerous shape — a value that LOOKS like provenance because it is
  built out of real characters from a real id. Both `chara_a2` drafts stay on disk,
  untouched, as the teaching exhibit.

- **[MEASURED] Receipt 1 — the home echo, taken immediately before the write.**
  `harness status --json` →
  `runtime_health.hermes_home = X:\Eternia\.hermes\profiles\base`,
  `hermes_profile = base`, `runtime_root = X:\Eternia\.hermes\agent-runtime`.

- **[MEASURED] Receipt 2 — the red-first analog: the list BEFORE.**
  `harness characters list --json` in that home answered exactly two drafts,
  `20260825-025720-b9f5ae` and `20260825-030335-2f653e`, both `"authoredBy": "chara_a2"`,
  plus one installed character `cobalt-robot-courier`. Zero correctly-attributed drafts
  existed anywhere the launcher lane reads. That is the state the delta below proves
  against.

- **[MEASURED] Receipt 3 — the start payload.**
  `harness characters start --concept "A tall lantern-keeper in a long teal coat with
  brass goggles pushed up on the forehead, carrying a glass storm lantern; readable at
  small size." --display-name "Teal Lantern Keeper" --authored-by base --json` →
  `{"draft": "20260827-150945-7ba0cb", "ok": true, "stage": "turnaround"}`, with
  `summary.authoredBy = "base"`, `slug = "teal-lantern-keeper"`,
  `directory = X:\Eternia\.hermes\profiles\base\characters\.drafts\20260827-150945-7ba0cb`,
  `directions = 8`, `rows = 10`, `style = "auto"`, `baseImage = null`. No `--slug` was
  passed; `teal-lantern-keeper` is the slugified display name, as documented.

- **[MEASURED] Receipt 4 — the list AFTER.** The same verb now answers three drafts; the
  new row, verbatim:
  `{"authoredBy": "base", "authoredRows": 10, "baseImage": null, "concept": "A tall
  lantern-keeper in a long teal coat with brass goggles pushed up on the forehead,
  carrying a glass storm lantern; readable at small size.", "directions": 8,
  "directory": "X:\Eternia\.hermes\profiles\base\characters\.drafts\20260827-150945-7ba0cb",
  "displayName": "Teal Lantern Keeper", "id": "20260827-150945-7ba0cb", "rows": 10,
  "slug": "teal-lantern-keeper", "stage": "turnaround", "style": "auto"}`.

- **[VERIFIED] `start` really does generate nothing, and the disk says so.** The draft
  directory holds ONE file — `draft.json`, 1,118 bytes — with `"authored_by": "base"`,
  `"base_image": ""`, `"stage": "turnaround"` and the CHAR8 spec (authored
  `s se e ne n`, mirrored `nw sw w`, states `idle:6` + `walk:8`). No provider was called
  and no tokens were spent. A draft at any stage lists, so `turnaround` is enough for the
  console to see it.

- **[VERIFIED] Fact 4 of the wave plan re-taken, and it still holds.** The two live serve
  children (pids 30248 stdio, 30740 stdio+socket:61629, both commit `1295212f2e`,
  `dirty: false`) classified `live` before and after this strip — nothing here restarted,
  killed or touched them. Their records' key set is
  `argv_hint boot_id build port schema_version socket_started_at started_at
  started_at_ticks store_root transport`: `store_root` present, **`hermes_home` absent**.
  From outside, the home a running serve child resolved is still unknowable; §W4 is the
  fix and it had not landed when this was taken.

- **[READ] Both repos have moved past the wave plan's stated baselines.** The plan pins
  hermes at `1295212f2e` and launcher at `2d7c7c8e0`; at the time of this strip hermes
  HEAD was `194cb3d0ab` (S8b-b, placement) and launcher HEAD was `3a5fbacfb` (S8b-b), with
  the wave's launcher FIELD-NOTES file already created by `6bb28eebc`. The hermes working
  tree also carried two unrelated modified docs from another session
  (`docs/agent-runtime-harness/03-transport-and-wire.md`,
  `docs/agent-runtime-harness/planned/remote-gateway.md`) — left alone; this strip
  commits only this file.
  *Consequence:* a strip that re-takes a plan fact by HASH will mis-fire in this program's
  shared checkouts. Re-take facts at the file, as the wave's own preamble instructs.

- **[READ] A hazard W6 should expect: `--authored-by base` resolves, but it ELECTS among
  two rows.** Both `personainst_base` (realm null) and `personainst_chara_a2_7b31d0e4`
  (realm `realm_codex-test-realm_cad6d4`, which is the snapshot's ACTIVE realm) carry
  `persona_id: "base"`, and neither matches `_deliberatePlacementSuffix`
  (`_agent_(\d+|[0-9a-f]{8})$` — the `chara_a2` id ends `_7b31d0e4`, not
  `_agent_7b31d0e4`), so both sit in the canonical partition and the tier table decides
  between them. Both are idle chat rows, so the winner comes down to mode classification
  and list order.
  *Consequence:* Resume on this draft opens a real console either way — which is the
  strip's goal — but the chat it lands in may be titled "Chara A2 - Tier1 authoring"
  rather than "Base Agent". If a capture needs the row to name the Base Agent, pin the
  exact instance id at authoring time instead of the persona id. Do not "fix" it by
  renaming anything, and do not change this draft.

- **[VERIFIED] The draft is home-SCOPED, and proving that turned up a second defect:
  `characters list` can answer the SAME draft id twice.** From a bare shell (alice home)
  the new draft is correctly invisible — `list --json` answers only the anime-girl draft.
  It answers it **twice**, both rows `id: 20260824-140756-cd645a` with
  `authoredBy: null`, because `.drafts/` holds both `20260824-140756-cd645a` and
  `20260824-140756-cd645a.backup-2026-08-25-nefix`, and the walk reads any subdirectory's
  `draft.json` without caring that the id inside it already appeared. A sibling backup
  taken by copying the directory is enough to do it.
  *Consequence:* `id` is not a key in the `drafts` array. The launcher's `laneDraftIds`
  is a `Set`, so liveness is unaffected, but any consumer that RENDERS the array (the
  adopt door's `_DraftList`, the review lane) will show one draft twice, with no way to
  tell the rows apart. Not fixed here — filed as found, in the home the wave told this
  strip not to touch.

- **[VERIFIED] The install gate hashes the PACKAGE, so this very file reds it.**
  `verify_harness_skill_install.py --check` calls
  `skill_package_content_hash(source.parent, source)` — the package DIRECTORY, not just
  `SKILL.md` — and `X:\Eternia\.hermes\shared\skills\harness-charsheet-authoring\`
  carries an installed copy of `FIELD-NOTES.md` beside `SKILL.md`. After this append the
  gate reports `harness-charsheet-authoring: repo 44478 B … | installed 44478 B … |
  DIVERGED` while the two `SKILL.md` files are byte-identical (`cmp` clean, same sha256)
  — the size in that line is `SKILL.md`'s, the hash is the package's, and the pair reads
  like a contradiction until you know that.
  *Consequence for §W3:* its red-first is "edit `SKILL.md`, watch the gate red". Appending
  to these field notes reds it identically, so the red alone does not prove the SKILL.md
  edit is the cause. The gate's own repair mode (a push, or the script without `--check`)
  closes both. This strip did not push and did not run the installer, so the machine's
  installed package is one field-notes entry behind this repo, on purpose.

- **[NOT VERIFIED — say this out loud] This strip put nothing on a screen.** It proves a
  correctly-attributed draft EXISTS in the home the launcher lane reads. Whether the
  Resume row renders `Live in this lane` still depends on W1's observed-home writer, W2's
  merge rule and a launcher relaunched from that build. W6 owes the capture; until then
  nobody has watched `CharacterResumeThisLane` fire.

### W4 — `hermes_home` on serve-instance records (2026-08-27, gap-closure wave, D-3)

Strip §W4 of launcher `docs/spatial/CHARA_GAP_CLOSURE_WAVE_2026-08-27.md`. Hermes-only,
observability-only, no behaviour branches on the new key. Worktree cut at `origin/main` =
`1295212f2e`; `origin/main` moved twice while the strip ran (S8b-b, then W5's note above),
so it landed rebased onto `d4dbd4f2f5`. The wave's branching rule earned its keep here —
local `main` was ahead of `origin/main` by another session's unpushed commit at cut time,
which is exactly the shape that caused the 2026-08-26/27 cross-session incident.

- **[VERIFIED] The hole was real, and I read it off the operator's live runtime, not off
  source.** Both live serve children — `30248` (stdio) and `30740` (stdio+socket, port
  61629), both `commit 1295212f2e`, `dirty: false` — had records under
  `X:\Eternia\.hermes\agent-runtime\serve_instances\` carrying `store_root` and **no**
  `hermes_home`. That is fact 4 of the wave plan confirmed on disk at 14:32Z.

- **[THE DISTINCTION WORTH KEEPING] `store_root` is not the home, and one does not imply
  the other.** They are separate axes. A serve child spawned with `HERMES_HOME` pointed at
  `profiles\base` writes the *same* `store_root` as one that resolved `profiles\alice` —
  which is exactly the trap the repo-paths memory already records ("the running Launcher's
  serve spawns with `HERMES_HOME=profiles/base`, not alice; measuring under alice measures a
  different runtime"). `harness status --json` answers the home live, but only for a process
  you can already talk to; that is the wrong end of the question when you are staring at a
  directory of records. Now the record answers it.

- **[RULED, D-3] Always-written, nullable, `schema_version` still 1.** Three states, three
  spellings, and a reader must keep them apart: a path says *this home*; `null` says *this
  serve could not resolve one*; an **absent key** says *this entry predates the field*. The
  `port` / `socket_started_at` precedent, reused verbatim. The empty string is a fourth
  spelling of the second that reads like a PATH — pinned against, and the pin is
  load-bearing: mutating the writer to `""` reds
  `test_an_unresolvable_home_is_written_as_null_never_as_an_empty_string` with
  `assert '' is None`.

- **[VERIFIED — the absent-key case is not hypothetical, it is on disk today.]** The two
  live children above registered before this landed, so their records will carry no
  `hermes_home` for as long as they run. A reader that treated the missing key as a signal
  would have reclassified two live serves on the day it shipped. So nothing classifies on
  it: `test_a_record_written_before_the_field_existed_classifies_exactly_as_before` strips
  the key back out of a written record and pins `live` / reason `""`. That test is a
  **control, not a red** — it passed before the field existed and after, which is the point.

- **[BOUNDARY] The registry resolves nothing.** `agent_runtime/serve_registry.py` imports no
  `hermes_constants` (the only two mentions of the name in that file are prose saying so);
  the value is computed by the caller in `hermes_cli/harness_parts/serve.py` as
  `str(get_hermes_home())`, wrapped so a resolution failure degrades to `None` rather than
  failing registration — a registry entry is bookkeeping, and bookkeeping must not be able
  to fail a boot. The field is therefore unit-testable against an injected string.

- **[SAY THE LIMIT OUT LOUD] It is a BOOT-time observation, not per-turn authority.** The
  runtime may rebind a home for a single turn and this key will not have moved. Anyone
  reading it as "the home this serve is using right now" is misusing it. The field's own
  comment and the module docstring both say so, because this is precisely the kind of fact
  that gets over-read six months later.

- **[VERIFIED, not inferred] A real fresh serve boot writes it.** Not just the unit test
  with an injected string: I spawned `python -m hermes_cli.main harness serve --ndjson` as a
  subprocess against an **isolated** `HERMES_AGENT_RUNTIME_ROOT` + `HERMES_HOME` under Temp,
  read the record off disk while it served, and got
  `"hermes_home": "…\\w4boot-dmu628u4\\profiles\\base"` beside its `store_root` of
  `…\\w4boot-dmu628u4\\agent-runtime` — two different paths in one record, which is the
  whole point. Clean shutdown then removed the record (exit 0, directory empty), so the
  unregister path is unchanged. The two live children were never signalled, restarted or
  read-locked; I checked their pids and record bytes afterwards and both were untouched.
  Isolation mattered here for a second reason: my child took the socket lock at port 57217,
  and that lock is per-root — against the live root it would have contended with pid 30740.

- **[TRAP FOR THE NEXT AGENT — cost me a diff of 1069 lines] `Path.write_text()` from a
  Python one-liner silently converts this repo's files LF → CRLF on Windows.** `core.autocrlf`
  is **false** here and the index is LF, so a round-trip through `read_text`/`write_text`
  rewrites every line of the file as far as git is concerned. Two files went from a genuine
  `+43 / +69` to `556 / 411` changed lines and I nearly committed it. `git diff --numstat
  --ignore-cr-at-eol` is how you see through it, and `git ls-files --eol` (`i/lf w/crlf`) is
  how you confirm it. Use the Edit tool, or `write_bytes`, or pass `newline="\n"`.

- **[FOUND, contradicts nothing but worth recording] The wave plan's gate command is not
  this repo's canonical runner.** §W4 names `python -m pytest …`; `AGENTS.md` says **ALWAYS**
  use `scripts/run_tests.sh` (CI-parity: cleaned env, `TZ=UTC`, `PYTHONHASHSEED=0`, per-file
  subprocess isolation). I ran both and both are green at 49/49 — but the plan-named command
  is the weaker of the two, and a future strip quoting a bare `pytest` gate should say so.
  The runner needs `HERMES_PYTHON` set in a fresh worktree, which has no `.venv`.

### W3 — the skill catches up with the launcher's new meaning (2026-08-27, gap-closure wave)

- **[READ, at launcher `origin/main`] The launcher field this skill described for three
  sections stopped meaning what the skill said.** `CharaDraftBinding.home` was documented as
  *the home the authoring turn resolved* — a fact nothing in that launcher has ever known,
  which is why it had no production writer and every real row short-circuited. Owner
  decision §13.24 redefined it as **a home the launcher OBSERVED the draft readable in**,
  renamed the sealed family `CharaAuthoringHome` → `CharaDraftHome` (arm names and every
  wire key — `home`, `state: observed|unknown`, `path` — unchanged, so bundles on disk
  decode identically), and gave it exactly one production writer: `_DraftList._adopt` in
  `adopt_character_dialog.dart`, which stamps `CharaHomeObserved(path)` only from a
  `HermesLaneHomeResolved` and keeps unknown on every other arm. §13.25 then flipped
  `mergeCharaDraftBinding`'s home arm to `seen.home is CharaHomeObserved ? seen.home :
  stored.home` — freshest sighting wins, unknown never clobbers observed.
  **Consequence for an authoring agent:** the only home the launcher can ever hold is a
  *sighting by the launcher*, never the home your turn authored from, and it is written by
  an operator's click rather than by anything you emit. Your prose is still the sole
  carrier of the authored home. §13.22 is untouched — the `CHARSHEET-QA:` line still
  carries no home and is not to grow one, and §13.24 says so explicitly.

- **[READ] The resume seed's home line is a two-repo contract, and it is quoted verbatim in
  both.** `MissionCharacterResumeSeed.message` composes
  `last observed home: <path>`, or `last observed home: never observed by the launcher`
  when there is no sighting, and closes with *"Echo the home you resolve; do not assume
  it."* Its own doc comment says the spelling "is a contract across two repos rather than
  launcher copy". **Consequence:** a resume turn is handed a possibly-stale sighting and a
  standing instruction not to trust it. Re-run the preflight probes, echo
  `.runtime_health.hermes_home`, and if the draft is not in your list say which home you
  are in and which home the seed named — never author a second copy over a home
  disagreement. The launcher fills nothing in when it has no sighting; `never observed by
  the launcher` is a value, not a bug.

- **[TRAP FOR THE NEXT SLICE — it will poison a red-first if you let it]
  `verify_harness_skill_install.py --check` hashes the PACKAGE DIRECTORY, and FIELD-NOTES.md
  lives in it.** So an append to *this file alone* reports
  `harness-charsheet-authoring: … DIVERGED` while the two `SKILL.md` copies are byte-identical
  — I proved it in isolation before touching SKILL.md (`diff -q` said identical; the gate
  said DIVERGED). The gate's own report line is what makes this confusing: it prints
  `repo 44478 B … | installed 44478 B …` with two different hashes, because the SIZE column is
  `source.stat().st_size` (SKILL.md alone) while the hash is
  `skill_package_content_hash(source.parent, source)` over the whole directory. Equal sizes
  beside unequal hashes is the signature of a sibling-file change, not a SKILL.md change.
  **Consequence:** never read a DIVERGED as evidence about `SKILL.md`, and never make this
  gate a strip's red-first — plan §W3 names it as one and it cannot serve. Own the red in
  this repo instead: pin the sentences in
  `tests/agent_runtime/test_persona_skill_policy.py`, where the failure names the phrase.
  The divergence self-heals anyway — `.githooks/pre-push` runs the script in repair mode,
  so it installs from the repo and re-verifies on every push.

- **[TRAP, cost me one red] A cross-repo quoted string must not be markdown-wrapped.**
  My pin on `never observed by the launcher` reded against a SKILL.md that contained the
  phrase — with a newline and two spaces of indent inside it. A quoted contract is a
  contiguous string or it is not the contract; reflow the sentence around it rather than
  through it.

## The recorded home (appended by the H1 slice, 2026-08-27)

- **[READ] `characters start` now records the home it ran in, and you do not have to say
  it.** `CharacterDraft.create` writes `hermes_home = str(get_hermes_home())` into
  `draft.json` unconditionally, beside `authored_by` — unconditional because there is no
  caller to withhold it and nothing to guess. It is legitimate for the same reason
  `drafts_dir()` is: `get_hermes_home()` was already resolved two statements earlier and
  the directory was just created under it, so the draft IS sitting where the key says. This
  is hermes stating a fact about its own filesystem, not a consumer slicing a profile name
  out of a path it was handed — the derivation ban binds READERS of a home, never the
  authority recording where it put the file. **Consequence:** the home is now on the draft
  itself, so a later reader (or a launcher that has never listed that home) can learn where
  a draft was authored without anyone having typed it into a QA line.

- **[READ] It is a WRITE, not an announcement — `characters start` still emits no event.**
  Nothing is pushed anywhere; the draft became self-describing and that is all.
  **Consequence:** a launcher learns the value at its next sighting of the draft and not
  before, so do not tell an operator that starting a draft "notified" anything.

- **[READ] The payload key is `hermesHome`, in all three payloads that carry `authoredBy`:
  the `start --json` summary, `status --json`, and every `list --json` draft row.** It is a
  `str` or JSON `null`, **never `""`** — the same path-field rule `baseImage` and
  `history[].path` were fixed to follow. It is deliberately NOT copied into the installed
  `character.json` manifest and NOT on the `CHARSHEET-QA:` line, and `SCHEMA` stays 1:
  nothing must read it to be correct, so a schema-1 reader that ignores it renders exactly
  what it rendered before. **Consequence:** read `hermesHome` from any of the three and get
  the same answer; if you get `null`, the draft predates the field — that is a readable
  fact, which is the whole reason absence is not `""`.

- **[READ] A recorded home may be honestly stale, and that is not a defect to fix.** The
  value means *the home hermes recorded when the draft was created* (or, for a backfilled
  draft, the home it sat under when the backfill ran). A copied or backed-up draft carries
  its ORIGINAL home, and nothing ever rewrites a value that is already there.
  **Consequence:** never "correct" a `hermes_home` that disagrees with where the file is
  now — it is answering "where was this authored", not "where does it live today". The
  second question is the launcher's own observation and a different field entirely; do not
  substitute one for the other in a resume decision.

- **[READ] Drafts that predate the field are filled in by ONE explicit verb:
  `harness characters backfill-home [--json]`.** It walks the drafts under the currently
  resolved home, stamps only the ones whose home is absent (a blank counts as absent), and
  skips the rest; the receipt is `{ok, home, stamped, skipped}` where each row is
  `{id, directory}` — directories are named because two drafts really can carry the same
  `id` (a copied draft keeps the id inside its own `draft.json`) and an id-only receipt
  could not say which directory was written. It is idempotent: a second run stamps nothing.
  **Consequence:** run the verb, take the receipt as the evidence, and never hand-edit a
  `draft.json` to add the key.

- **[READ — and it corrects the plan that sent me] adding a `characters` VERB is always a
  `SKILL.md` edit, whatever a plan says.**
  `tests/agent_runtime/test_persona_skill_policy.py::test_charsheet_skill_documents_exactly_the_characters_verbs_hermes_has`
  builds the live argparse tree and asserts the skill's verb table equals it **as a set, in
  both directions** — so a verb hermes grows and a verb the skill invents fail identically.
  The H1 plan ruled the hermes skill UNTOUCHED and "no `SKILL.md` edit → no install-hash
  cycle owed", reasoning correctly about the new draft FIELD (which really does need no
  teaching, since `start` writes it automatically) and not at all about the new VERB in the
  same strip. The verb landed the table row; the ruling was half-right about a strip that
  did two things. **Consequence:** whenever a strip adds, renames or removes a
  `harness characters` subparser, the skill's verb table moves in the same commit and the
  install-hash cycle is owed — the pin exists precisely so that cannot be deferred. Reading
  the field half of such a ruling as covering the verb half is the mistake to avoid.

- **[TRAP — it would silently falsify every dormant exhibit] `_save()` stamps `updated`
  with "now", so a backfill must not go through it.** `CharacterDraft.record_home()`
  writes via `_write_json_atomic` directly for exactly this reason, and the two pins that
  say so red under a `_save`-routed implementation (proved by planting it). The drafts this
  verb reaches are the dormant ones whose timeline — and whose mis-attributed
  `authored_by` — is the evidence we keep them for; a backfill that bumped them all to the
  moment an operator ran it would destroy what it was auditing. **Consequence:** any future
  provenance stamp on an existing draft takes the same route. If you find yourself adding a
  field to a draft that already exists, ask what `updated` is being used to prove before
  you call `_save`.

## Running the backfill (appended by the OP slice, 2026-08-27)

The plan's OP strip, run against the live runtime from a worktree at `c2ab3628b0` (H1).
No code changed; this section is the receipt and what running it taught.

- **[READ] The receipts, verbatim.** Two homes, three runs each: the stamping run, a second
  `--json` run to show idempotence, and a third plain run for the human line.

  `HERMES_HOME=X:\Eternia\.hermes\profiles\base`, `harness characters backfill-home --json`:

  ```json
  {
    "home": "X:\\Eternia\\.hermes\\profiles\\base",
    "ok": true,
    "skipped": [],
    "stamped": [
      {
        "directory": "X:\\Eternia\\.hermes\\profiles\\base\\characters\\.drafts\\20260825-025720-b9f5ae",
        "id": "20260825-025720-b9f5ae"
      },
      {
        "directory": "X:\\Eternia\\.hermes\\profiles\\base\\characters\\.drafts\\20260825-030335-2f653e",
        "id": "20260825-030335-2f653e"
      },
      {
        "directory": "X:\\Eternia\\.hermes\\profiles\\base\\characters\\.drafts\\20260827-150945-7ba0cb",
        "id": "20260827-150945-7ba0cb"
      }
    ]
  }
  ```

  Second run, same shell:

  ```json
  {
    "home": "X:\\Eternia\\.hermes\\profiles\\base",
    "ok": true,
    "skipped": [
      {
        "directory": "X:\\Eternia\\.hermes\\profiles\\base\\characters\\.drafts\\20260825-025720-b9f5ae",
        "hermesHome": "X:\\Eternia\\.hermes\\profiles\\base",
        "id": "20260825-025720-b9f5ae"
      },
      {
        "directory": "X:\\Eternia\\.hermes\\profiles\\base\\characters\\.drafts\\20260825-030335-2f653e",
        "hermesHome": "X:\\Eternia\\.hermes\\profiles\\base",
        "id": "20260825-030335-2f653e"
      },
      {
        "directory": "X:\\Eternia\\.hermes\\profiles\\base\\characters\\.drafts\\20260827-150945-7ba0cb",
        "hermesHome": "X:\\Eternia\\.hermes\\profiles\\base",
        "id": "20260827-150945-7ba0cb"
      }
    ],
    "stamped": []
  }
  ```

  Third run, no `--json`:

  ```text
  0 draft(s) stamped with X:\Eternia\.hermes\profiles\base; 3 already recorded
    skipped 20260825-025720-b9f5ae  already X:\Eternia\.hermes\profiles\base
    skipped 20260825-030335-2f653e  already X:\Eternia\.hermes\profiles\base
    skipped 20260827-150945-7ba0cb  already X:\Eternia\.hermes\profiles\base
  ```

  `HERMES_HOME=X:\Eternia\.hermes\profiles\alice`, stamping run:

  ```json
  {
    "home": "X:\\Eternia\\.hermes\\profiles\\alice",
    "ok": true,
    "skipped": [],
    "stamped": [
      {
        "directory": "X:\\Eternia\\.hermes\\profiles\\alice\\characters\\.drafts\\20260824-140756-cd645a",
        "id": "20260824-140756-cd645a"
      },
      {
        "directory": "X:\\Eternia\\.hermes\\profiles\\alice\\characters\\.drafts\\20260824-140756-cd645a.backup-2026-08-25-nefix",
        "id": "20260824-140756-cd645a"
      }
    ]
  }
  ```

  Second run:

  ```json
  {
    "home": "X:\\Eternia\\.hermes\\profiles\\alice",
    "ok": true,
    "skipped": [
      {
        "directory": "X:\\Eternia\\.hermes\\profiles\\alice\\characters\\.drafts\\20260824-140756-cd645a",
        "hermesHome": "X:\\Eternia\\.hermes\\profiles\\alice",
        "id": "20260824-140756-cd645a"
      },
      {
        "directory": "X:\\Eternia\\.hermes\\profiles\\alice\\characters\\.drafts\\20260824-140756-cd645a.backup-2026-08-25-nefix",
        "hermesHome": "X:\\Eternia\\.hermes\\profiles\\alice",
        "id": "20260824-140756-cd645a"
      }
    ],
    "stamped": []
  }
  ```

  Third run, no `--json`:

  ```text
  0 draft(s) stamped with X:\Eternia\.hermes\profiles\alice; 2 already recorded
    skipped 20260824-140756-cd645a  already X:\Eternia\.hermes\profiles\alice
    skipped 20260824-140756-cd645a  already X:\Eternia\.hermes\profiles\alice
  ```

- **[READ] Before and after, from `characters list --json`.** Every `hermesHome` was `null`
  before and carries this home after; no `authoredBy` moved.

  | home | draft id | directory leaf | `authoredBy` | `hermesHome` before to after |
  | --- | --- | --- | --- | --- |
  | base | `20260825-025720-b9f5ae` | `20260825-025720-b9f5ae` | `"chara_a2"` | `null` to `...\profiles\base` |
  | base | `20260825-030335-2f653e` | `20260825-030335-2f653e` | `"chara_a2"` | `null` to `...\profiles\base` |
  | base | `20260827-150945-7ba0cb` | `20260827-150945-7ba0cb` | `"base"` | `null` to `...\profiles\base` |
  | alice | `20260824-140756-cd645a` | `20260824-140756-cd645a` | `null` | `null` to `...\profiles\alice` |
  | alice | `20260824-140756-cd645a` | `20260824-140756-cd645a.backup-2026-08-25-nefix` | `null` | `null` to `...\profiles\alice` |

  The two `chara_a2` rows are the mis-attribution exhibits and they are untouched: the
  string `"chara_a2"` still resolves to no roster persona, which is the lesson they are
  kept for. A home stamp does not disturb it. The base home held THREE drafts, not the two
  the plan's fact 8 named — `teal-lantern-keeper` (`20260827-150945-7ba0cb`,
  `authored_by: "base"`) was created by a later strip on the same day, and the plan's own
  "plus any W5-era additions" clause covers it.

- **[READ] The exhibits' `updated` and `authored_by` survived byte-for-byte, and that is
  checkable rather than asserted.** Each `draft.json` was hashed before the run. After it,
  deleting the single `"hermes_home": ...` line from the file reproduces the pre-run
  SHA-256 exactly, on all five:

  ```text
  20260825-025720-b9f5ae                          ece79d75721e6261bbbf968205dab5cd54b5e5f547d55cd26653a0921b91527b
  20260825-030335-2f653e                          0575bc123145a9a03b82ecabb99a40bf4b331d74f71825da0844d47177360fa7
  20260827-150945-7ba0cb                          2deddfdc810b8770bc901f145ce20fd2857643cfd85f0e911bb9cc4a7b33515f
  20260824-140756-cd645a                          56112a8697953b6f0b57760babc510972343e0aef618379ed9ca15fb60c09274
  20260824-140756-cd645a.backup-2026-08-25-nefix  1e6ed66c9c4dc2cea818df78d258b390e542f105d264c38c4fc4440d7ef61b95
  ```

  **Consequence:** this is the check to run after any provenance stamp, and it is stronger
  than reading `updated` back — it proves nothing ELSE moved either. Do not re-serialise
  the file to build the comparison (see the CRLF trap below); delete the one line from the
  bytes you already have.

- **[TRAP — it is silent, and it answers a home you did not ask for] a bare shell can have
  `HERMES_HOME` pointing at the runtime ROOT, and `harness status --json` still answers a
  PROFILE.** With `HERMES_HOME=X:\Eternia\.hermes` — the root, not a profile — the status
  payload reports `...\profiles\alice` and warns about nothing. Every command in this run
  therefore set `HERMES_HOME` explicitly and read `.runtime_health.hermes_home` back out of
  `harness status --json` before doing anything: it answered
  `X:\Eternia\.hermes\profiles\base` and `X:\Eternia\.hermes\profiles\alice` respectively.
  **Consequence:** for a verb that WRITES, "which home am I in" is not a thing to infer
  from the shell — ask the runtime and put the answer in the receipt. The `backfill-home`
  receipt's own `home` key is the second copy of that answer, and it agreed with the
  read-back both times.

- **[TRAP] the installed `hermes` shim runs the PRIMARY checkout, not your worktree.** The
  venv at `X:\Eternia\.hermes\venvs\hermes-agent` carries an editable install pointing at
  `X:\Eternia\hermes-agent`, whose `main` was two commits *sideways* of `origin/main` and
  did not contain H1 at all — so `hermes harness characters backfill-home` would have been
  an unknown verb, or, for a verb that already existed, silently the old code. Run the
  venv's interpreter with the worktree as the working directory instead:
  `X:\Eternia\.hermes\venvs\hermes-agent\Scripts\python.exe -m hermes_cli.main harness ...`
  puts the worktree first on `sys.path` and keeps every dependency. **Consequence:** when a
  strip's whole point is "this build has the new producer in it", prove which code ran —
  `python -c "import hermes_cli; print(hermes_cli.__file__)"` from that same cwd, plus
  `git log -1` in that tree — before trusting the output.

- **[READ] Two drafts, one id: only the JSON receipt tells them apart.** The alice home
  holds `anime-girl` twice — `20260824-140756-cd645a` and its
  `...-cd645a.backup-2026-08-25-nefix` sibling — and both `draft.json` files carry the same
  `id`. The `--json` receipt's `{id, directory}` rows name both distinctly and both appear
  in `stamped`. **Corrected at the code by the D slice, 2026-08-27: it is one ARM of the
  human line, not the whole line.** `_cmd_characters_backfill_home` prints
  `f"  stamped {row['id']}  {row['directory']}"` — which disambiguates fine — and
  `f"  skipped {row['id']}  already {row['hermesHome']}"`, which puts the home where the
  directory would have gone and therefore cannot. That is why the receipts above show the
  identical pair only on the third run (`skipped 20260824-140756-cd645a  already ...` twice):
  the stamping run's human line was never captured, because it was taken with `--json`.
  **Consequence:** the verb is idempotent, so every run after the first is ENTIRELY the
  skipped arm — the arm that loses the directory is the one an operator sees most. Take the
  `--json` receipt when the question is WHICH file was written, and if you are fixing this,
  it is one f-string carrying both. Filed on the launcher's Mission Control queue under
  "From D, the recorded-home wave's closure strip".

- **[TRAP — it will make a byte comparison lie] `draft.json` is CRLF on Windows.**
  `_write_json_atomic` opens its temp file in text mode (`"w"`), so `json.dump`'s `\n`
  becomes `\r\n` on the way out and the files on disk are 100% CRLF. A "reconstruct the
  original by re-dumping the parsed dict without the new key" check therefore fails on all
  five drafts even though nothing but the new key changed — the reconstruction is LF.
  **Consequence:** compare draft bytes textually (drop the line, hash what is left), never
  by re-serialising. Nothing was wrong with the files; the checker was.

- **[READ — it matters to anyone capturing a payload as a fixture] `--json` output is
  already `ensure_ascii=False`, pretty-printed and key-sorted at source.**
  `agent_runtime.cli_format.emit_json` is
  `json.dumps(..., indent=2, ensure_ascii=False, sort_keys=True)`, so an em dash in a QA
  note is emitted as the literal character and never as a `\uXXXX` escape.
  **Consequence:** a captured payload needs no post-processing at all, and any capture
  carrying `\uXXXX` escapes went through a re-serialising step someone added — which is the
  moment a "verbatim capture" stops being one. Capture the stdout and commit it. The
  launcher's `test/fixtures/charsheet/status.json` was carrying exactly those escapes; the
  2026-08-27 re-capture removed them by not re-serialising.

## The wave closes (appended by the D slice, 2026-08-27)

D is a LAUNCHER strip — owner decision §13.26, the wave doc's LANDED/OWED
annotation, the queue rows and one correction to the launcher's doc 09 — and its
running record is in
`EterniaLauncher/docs/spatial/CHARA_CONSOLE_AUTHORING_FIELD_NOTES.md`. Only what
it learned about a hermes verb or home is here, per the split rule at the bottom
of this file. Two things, and the first is the correction to OP's own entry above.

- **[READ] The `backfill-home` human line: the STAMPED arm names the directory, the
  SKIPPED arm does not.** Corrected in place in the OP section above rather than
  restated here.

- **[READ] Nothing that lands on hermes `origin/main` reaches the RUNNING system until
  the primary checkout fast-forwards.** OP recorded that the venv's editable install
  points at `X:\Eternia\hermes-agent` and that a worktree must be run explicitly. The
  half worth stating as a standing fact: the launcher's serve child is spawned from that
  same install, so it runs the PRIMARY's code. Measured again 2026-08-27 after this wave
  landed — the primary is **8 commits ahead of `origin/main` and 2 behind**, and the two
  it lacks are exactly `c2ab3628b0` (H1) and `11e8894e0c` (OP): `hermes_home` is not in
  its `agent/charsheet/draft.py` and `harness characters backfill-home` is an unknown
  verb there. **Consequence:** "landed on `origin/main`" and "the running launcher can
  see it" are two different claims, separated by one `git merge --ff-only` in a checkout
  every session is told not to touch. Say which one you mean. Filed on the launcher's
  Mission Control queue with the finder module's path table as evidence.

- **[READ] The serve answers `characters` reads with whatever `HERMES_HOME` process-global
  `os.environ` holds AT THAT INSTANT — which during a persona prewarm is another persona's
  home.** Measured 2026-08-27 during the W6 Stage C walk: the launcher respawned its serve
  onto `alice` at 20:55; the serve's boot prewarm bound every placed persona in turn
  (`persona_chat_actor_prewarm`, four instances bound to `launcher-qa`, the first held for
  11.25 s), and each bind writes the persona's `HERMES_HOME`/`HOME`/`HERMES_AUTH_HOME` into
  `os.environ` (`persona_profile_context`, `export_env=True` — the mirror is process-global
  by design). A concurrent `characters status --draft` argv request resolved
  `profiles\launcher-qa` and reported a `base`-authored draft as nonexistent, path and all,
  on the operator's screen. The context-local machinery already exists
  (`persona_profile_scope`, the ContextVar home override); either prewarm binds scope-locally
  or the charsheet paths resolve through the ContextVar, never bare `os.environ`.
  **Consequence:** any argv read through the serve is only as home-stable as the quietest
  moment of the persona lanes sharing its process.

- **[READ] `harness characters list --json` through the serve socket can go silent
  indefinitely while `harness status --json` streams on the same serve.** Measured
  2026-08-27: one authenticated socket connection sent the argv and read ZERO frames for
  >120 s; a fresh connection minutes later got the identical argv complete (three drafts,
  exit 0) in ~6 s. Same serve pid, same home, no restart between. Not transport — `status`
  answered instantly throughout. Unreproduced-on-demand; recorded so the next silent
  `characters` read is recognized as this and not as "no drafts".

- **[READ] The QA persona's `launcher_qa` MCP admission alternated 3/0/3/0 servers across
  four consecutive `mission-chat message` turns to the SAME session** (2026-08-27,
  `personainst_qa_agent_c1a70d19`). The Stage C skill's retry-once rule happens to mask a
  strict alternation perfectly — which is exactly why it went unmeasured until four turns
  ran back to back. Whatever flips (a lease, a keepalive, a per-turn registry rebuild)
  flips per-turn, not per-session.

## 2026-08-27 — the env bleed, fixed at the consumer (envbleed slice)

Standing in the hermes repo, worktree `X:/wt/envbleed` off `4ab953df89`. Sent to fix the
cross-persona `HERMES_HOME` bleed the three entries above measured. Four things, and the
first two correct the plan I was handed rather than confirming it.

- **[READ] `persona_chat_actor_prewarm` has NO bind site of its own.** The plan said "the
  prewarm switches its bind to `persona_profile_scope`", and there is no bind there to
  switch: the module resolves a `PersonaProfileBinding`, puts `profile=binding.hermes_profile`
  on an `AgentRunRequest`, and hands it to `ProfileAgentRunner.prewarm`. The bind happens one
  layer down, in `_execute_agent_run` (`agent_runtime/profile_runner.py:857`), inside the
  `with` stack a REAL TURN uses — same `_WORKDIR_LOCK`, same `persona_profile_context`,
  same everything, deliberately, because that module's whole premise is that an actor built
  under different scopes is a different actor. So there is no prewarm-only knob; the only
  available spelling is `export_env=not request.prewarm_only` on the shared body.
  **Consequence:** any statement of the form "the prewarm does X to the environment" is
  really a statement about `_execute_agent_run`, and applies to every chat turn too.

- **[READ] Turning the mirror off for the prewarm is NOT sound, and the blocker is a
  thread, not a subprocess.** I went looking for the spawn the plan told me to check for
  and found something narrower and worse: `mcp_admission.admit_mcp_servers` runs its
  registrar on a thread it starts itself (`threading.Thread(..., name="mcp-admission")`,
  `agent_runtime/mcp_admission.py:1226`), and the cold path from there is
  `register_mcp_servers`, which spawns. A ContextVar crosses neither boundary — not the
  thread, not the spawn — so the `os.environ` mirror is the ONLY channel by which an
  admitted MCP server learns which persona home it is serving. Drop the mirror for the
  prewarm and a prewarmed chat's servers come up on the head home while the turn's come up
  on the persona's: the prewarmed actor stops byte-matching, `acquire()` rebuilds, and the
  module has paid 3 s to produce something it then throws away. **Consequence:** "a prewarm
  only builds an actor, it cannot need a global mirror" is false — construction includes
  admission, and admission spawns. The mirror stays.

- **[READ] The fix therefore belongs at the CONSUMER, and the repo had already litigated
  this exact class twice.** `core_cache`'s HC-1 note (`agent_runtime/core_cache.py:1268`)
  argues it at length for the snapshot fingerprint — including the part that matters most
  here, that resolving through `get_hermes_head_home()` is *necessary and not sufficient*,
  because its first authority is a ContextVar and the bleeding thread has none. Its answer
  is capture-once at a declared boot instant. `chat_live_log`'s module docstring has the
  same finding under the heading "THE HERMES_HOME TRAP" and the same answer. The argv lane
  simply never got its capture. It has one now: `serve_loop` captures `get_hermes_home()`
  beside `capture_fingerprint_home()` — the boot instant that provably precedes every
  persona scope — and `_run` binds it per request through a new
  `profile_context.process_home_scope`. A ContextVar out-ranks the env var in
  `get_hermes_home()`'s ladder, so the argv lane wins without taking anything away from the
  lane that needs the global. Not the head home, deliberately: under the launcher's
  `HERMES_HEAD_HOME` the head and the runtime home are different directories on purpose,
  and an argv request belongs to the runtime one.

- **[READ] What I did NOT do, so the next slice does not assume it.** (i) The mirror is
  untouched — `persona_profile_context` still writes process-global env for every turn and
  every prewarm, and every OTHER unbound thread in the serve is still a passenger on it.
  I fixed the lane that was measured, not the class. The snapshot fingerprint and the chat
  live log have their own captures; anything else that grows a thread in this process needs
  its own. (ii) Only `HERMES_HOME` is pinned. `HOME` has no context-scoped hook at all and
  is unobservable on native Windows anyway (`ntpath.expanduser` reads `USERPROFILE`);
  `HERMES_AUTH_HOME`'s mirror is written with the HEAD auth home rather than the persona's,
  so it was never a cross-persona bleed. (iii) A subprocess spawned from inside an argv
  request still inherits ambient `os.environ`, mirror and all — the ContextVar does not
  reach it. (iv) I did not reproduce the original `characters status --draft` failure
  end-to-end against a live serve; the regression test drives the real `serve_loop` seam
  with an injected dispatch that resolves `drafts_dir()`, which is the same four-frame-deep
  reader the incident hit, but Stage C on a running launcher is still owed — and per the
  entry above, the primary checkout has to fast-forward before the running serve has this
  code at all.

## The two serve-lane defects the W6 walk left behind (appended by the serve-diagnosis slice, 2026-08-27)

Both were recorded above as measured-but-unexplained: the `characters list` that
went silent through the serve socket, and the `launcher_qa` MCP admission that
alternated 3/0/3/0. Both now have a mechanism, and both mechanisms were
reproducible in a test — neither needed the live serve to be caught in the act.
Worktree `X:/wt/servediag` off `4ab953df89`; nothing pushed, primary untouched.

- **[FOUND — the silent `characters list` was POOL EXHAUSTION, and the pool is
  drained by ABANDONED STREAMS.]** The serve request pool is
  `DEFAULT_POOL_SIZE = 4`. `harness stream` is an argv request that never
  returns and holds a worker for its entire life; the cancel op's own comment
  prices this exactly — "otherwise four watchdog cycles exhaust the entire serve
  pool with abandoned streams". The only thing that ever set the cancel event
  was `{"op":"cancel"}`, sent by a launcher that came BACK. A client that simply
  died, or a socket session that closed, left its stream running forever:
  `_release_subscription` is the disconnect path and its docstring says "A
  client left. Unsubscribe it, and do NOTHING else". `agent_runtime/
  request_control.py` states the opposite as the contract in its own module
  docstring — the stream handler "must release its worker when its consumer
  disconnects" — and nothing implemented the disconnect half. **Consequence:**
  every dropped stream is one of four workers gone for the life of the process,
  and the fourth one turns every subsequent argv request into unbounded silence.
  Reproduced as a test: a socket client opens a stream, disconnects, and a
  following stdio request never runs — 20 s, zero frames carrying its id.

- **[READ — and this is why it looked like a transport fault] an argv request
  emits NO frame until its HANDLER writes one.** `_handle_message` registers the
  request and calls `pool.submit`; a request sitting in the executor's queue is
  byte-identical on the wire to one whose handler has wedged, and to a dead
  service. The `busy` frame the liveness pump emits carries a count and no ids,
  so it cannot answer the only question a waiting client has. **Consequence:**
  "no drafts", "still queued" and "the serve is gone" were one observation. The
  loop now emits `{"id":…,"event":"request_progress","state":"queued"|"running",
  "waited_ms":…,"running_ms":…,"pending":…,"pool_size":…}` on the lane that
  asked, once a request has produced nothing for `_REQUEST_SILENCE_SECONDS`
  (15 s — longer than a warm `snapshot`, so the normal path pays no frames at
  all). `state` is the field that matters: `queued` means no handler code has
  run and a retry is free.

- **[FOUND — the anti-silence pump was never wired to the socket lane at all.]**
  `_liveness_pump` emits its `busy` frame to `frames` — stdout — and nowhere
  else, while its own comment names the launcher's stream watchdog ("no frames
  for N seconds") as the thing it exists to satisfy. The drain path had already
  learned this lesson and broadcasts through `_broadcast_lanes` with the reason
  written down: "the socket client IS such a watchdog: it reads with a finite
  timeout and reports `transport_failed` on silence." The pump was left behind.
  **Consequence:** a socket client attached to a busy serve reads NOTHING — the
  measured ">120 s of zero frames" was literally true even though the pump was
  emitting the whole time, to a stream that client cannot see. Reproduced by
  attaching a real socket client and reading until its timeout. Now broadcast.

- **[FOUND — the MCP 3/0/3/0 is a two-turn cycle, and the fault is one early
  `return`.]** R2 keeps the transport warm across turns and tears the registry
  scope down after each, so a server can sit in `tools/mcp_tool._servers` with
  `session is None` and no registered tools. `_live_mcp_sessions` calls that
  COLD and routes it to `register_mcp_servers` on a written belief — "it has
  dedicated wake handling for exactly that case". It has a wake and no
  REGISTRATION: the name is already in `_servers`, so `new_servers` is empty, so
  it fires a fire-and-forget `_signal_reconnect` (deliberately NOT the
  `_signal_reconnect_and_wait` sibling the tool-call path uses) and returns
  `_existing_tool_names()` — a STALE list of tools it did not register.
  `admit_mcp_servers` rightly does not trust that return and re-reads
  `registered_mcp_server_names()`, which is empty: **admitted 0**, three
  `mcp_not_registered_on_lane` denials, a turn with no MCP surface. The nudge
  lands a second later on the background loop, so the NEXT turn finds a live
  session, takes the warm path and admits 3 — and that turn is the only one that
  registers a teardown and the only one that can kill the transport by using it,
  so the turn after it is parked again. The two turns do opposite things, which
  is what makes it alternate rather than settle. **Consequence:** the parked
  server is now woken by the admission layer itself — nudged, then waited on for
  a bounded `_PARKED_WAKE_TIMEOUT_SECONDS` (5 s, with every nudge sent before the
  first wait so N servers reconnect in parallel) — and whatever comes back is
  re-registered off its live session. Whatever does not falls through to the
  cold path unchanged.

- **[TRAP — the receipt's own denial text misdescribes this.]** On the 0-turn
  the denial reads "did not connect or advertised no tools". The server was
  connected minutes ago, is cached, and is reconnecting as the line is written;
  it advertised its tools on the previous turn. **Consequence:** that sentence
  is why this read as a mystery rather than as a park. A checkable
  discriminator, if the count ever drops again: on a parked turn
  `profile_timing.mcp_admission_ms` is TINY — the early return costs
  microseconds — where a real connect failure costs the ~20 s connect timeout.
  Small ms plus `cold` labels is this defect, not a timeout and not lane-busy.

- **[READ] `mcp_admission_transport` is a SNAPSHOT, not an outcome, and it is
  worth knowing which.** It is classified before registration, so a parked
  server reads `cold` there even now that the registrar may wake it and
  re-register it warm. That is honest for the question the label was added to
  answer ("what did this turn have to pay for" — a wake is a cost on the cold
  side of that line either way), but the comment claiming the label "can never
  disagree with the path actually taken" was corrected in place: the registrar
  logs the parked names it woke, and that log is where the finer distinction
  lives.

- **[NOT DONE, and deliberately]** Neither defect was re-measured on the LIVE
  serve — the primary checkout is other sessions' surface and this slice never
  touched it, so everything above is reproduced at the `serve_loop` and
  `_default_registrar` seams instead. Two things remain owed as field gates:
  whether a real launcher client tolerates the additive `request_progress` frame
  (it is additive and unknown events are ignorable, but that is an argument, not
  a measurement), and whether the parked-wake actually ends the alternation
  across four consecutive live turns. Neither the pool SIZE nor the `harness
  stream` design was changed, and no mutation or chat turn was made
  interruptible: reclaiming a worker is limited to the one request shape the
  cancel path already calls the sole safe cooperative exception.

## The one library (appended by the shared-library slice, 2026-08-27)

### H1 — the resolver, and the fifteen verbs that followed it for free

- **[READ] Fact 1 of the plan held exactly, and it is the reason this strip was small.** Two
  files spell the characters location in the whole of hermes — `agent/charsheet/draft.py`
  and `hermes_cli/harness.py`, and the second only by importing `characters_dir` from the
  first. So head-homing the library was one function body plus the authority it delegates
  to, and every one of the fifteen `harness characters` verbs moved without being touched.
  A grep for `"characters"` as a path segment across `agent/`, `agent_runtime/`,
  `hermes_cli/` and `hermes_constants.py` still returns one site after the change. That is
  worth saying out loud because the same claim was false for `hermes_home` two waves ago,
  and the difference was that this location had already been consolidated by someone else.

- **[MEASURED] The planted defect is the only test in the file that can tell the two
  implementations apart, and it does.** `get_shared_characters_dir()` computes the same
  `<root>/profiles/<name>` → `<root>` mapping `get_default_hermes_root()` already
  implements, so an implementation that reuses it passes three of the four resolver pins.
  Built that way on purpose first, the ContextVar control reds and nothing else does:

      assert foreign_root == other_root / "shared" / "characters"
      E  AssertionError: assert WindowsPath('.../process/shared/characters')
                             == WindowsPath('.../other/shared/characters')

  With `HERMES_HOME` at `<process>/profiles/base` and a `set_hermes_home_override()` naming
  `<other>/profiles/neko`, the bare-env derivation answers the PROCESS root — the
  cross-persona bleed the serve lane retired last week, re-imported one directory later.
  Riding `get_hermes_home()` answers `<other>`. **Consequence:** if a future reader is
  tempted to collapse the two resolvers into one, that test is the argument, not the
  docstring.

- **[MEASURED] The env-bleed regression test lost its observable to this change, and the
  retarget is itself the reversal's headline claim.**
  `tests/agent_runtime/test_serve_request_home_isolation.py` reproduced the incident by
  resolving `drafts_dir()` inside a bled window and asserting it named the serve's home.
  After the head-home that assertion cannot fail for the reason it was written: `alice` and
  `launcher-qa` sit under one root and now compute one library. It went red as
  `.../profiles/alice/characters/.drafts` != the new library path — a red that means "the
  probe stopped being sensitive", not "the fix regressed". The probe now resolves
  `get_hermes_home()` itself (still bleed-sensitive, and what every other profile-scoped
  reader on that lane rides) and asserts the library's invariance beside it. **Consequence:**
  the W6 finding "a serve resolving a home nobody selected broke a characters read" is now
  a statement about the LANE and no longer about characters at all — which is §A-1's
  argument 1, mechanised.

- **[READ] The `create()` comment fact 8 flagged was worse than "going false" — it was the
  whole justification.** It read "the draft IS sitting where this key says it is, so
  recording it is hermes stating a fact about its own filesystem rather than a consumer
  deriving one from a path". The first clause is now false and the second is still true,
  and they were welded into one sentence. Re-derived per §A-3 rather than deleted: hermes
  asks its own resolver which home this turn answered, the draft does not sit under it, and
  that divergence IS the field's meaning. The `hermes_home` property docstring gained an
  explicit "it is not an address, and asking it for one gets the wrong answer by
  construction" so the next reader does not have to reconstruct the reversal from a diff.

- **[READ] What I did NOT do.** (i) No `SCHEMA` bump and no wire change — the plan's fact 9
  is right that this wave adds no key, and the launcher fixtures stay contract-valid.
  (ii) `backfill-home` STAYS (§A-5), with its help text re-derived; its population after
  the OP run is empty but it is still the stamp path for a draft that arrives without the
  key. (iii) Nothing migrates anything: after this strip a populated legacy
  `<home>/characters` tree is simply invisible to the verbs. That window is real and H2/OP
  are what close it — §C's second row says the same, and it is why H1→OP wants to be
  same-day.

### H2 — `migrate-home`, and the three defects worth planting

- **[MEASURED] Stamping through `_save()` is the defect the byte pin exists for, and it is
  invisible to a dict comparison.** Built wrong first, the red is exactly the key the ruling
  is about:

      assert dropped + "\n" == before
      E  - "2026-08-24T14:07:56+00:00"
      E  + "2026-08-28T02:55:08.776734+00:00"

  That is `updated` on a dormant exhibit, rewritten to the moment the migration ran. The
  assertion is textual — read the landed file, drop the `"hermes_home"` line, compare to the
  bytes the source held — because a parsed-dict comparison passes through a re-serialisation
  without noticing and would have let this land. Same lesson as the recorded-home wave's
  §E.8, one verb later, and it earned its second outing.

- **[MEASURED] The other two planted defects red on exactly one pin each and nothing else.**
  Stamp-always: `assert receipt["stamped"] == []` against a draft that already named
  `/somewhere/else/profiles/original` — a relocation is not a re-attribution, and the drafts
  whose provenance is most interesting are the ones an unconditional stamp destroys first.
  Overwrite-on-collision: `assert receipt["moved"] == []` while the source directory was
  gone and the destination held the migration's copy. That second one is the shape worth
  naming, because both directories carry the same id: a `list` afterwards looks IDENTICAL
  whether the verb refused or ate a character. The receipt and the surviving source
  directory are the only two things that can tell them apart.

- **[READ] The source is spelled literally in the handler, and the helper refuses the
  degenerate case anyway.** After H1, `characters_dir()` answers the DESTINATION — so a verb
  that resolved its source through the ordinary authority would be asking to move the library
  onto itself. The handler writes `get_hermes_home() / "characters"` out and says why in a
  comment; `migrate_characters_home` compares resolved source and destination and returns an
  empty receipt if they match, with a test standing on that. Belt and braces on purpose: the
  handler's comment is the thing a future reader deletes, and the guard is the thing that
  survives them doing it.

- **[READ] A non-character directory under a legacy store is SKIPPED, not swept along.**
  "Installed character" means "a directory carrying `character.json`" — the same definition
  the CLI's installed rows already use — and anything else lands in `skipped` with a reason.
  It is left where it is rather than guessed at, which is the archive-never-delete instinct
  applied to a thing the verb does not recognise. A cross-volume rename, a lock or a
  permission error is reported the same way, per entry, so one stuck directory cannot strand
  the rest of the store half-migrated.

- **[MEASURED] The verb-table pin does the §E.5 job without being asked twice.** Adding the
  subparser reds `test_charsheet_skill_documents_exactly_the_characters_verbs_hermes_has`
  with `Extra items in the right set: 'migrate-home'` — the live parser tree against the
  skill's table. The `SKILL.md` row therefore rides this commit, not a later one. **Small
  correction while there:** the table's header said "Fourteen, flat" and the table held
  fifteen rows before this strip — a drift `backfill-home` introduced and nothing pins,
  because the test counts the ROWS and not the sentence. It says sixteen now.

- **[READ] What I did NOT do.** (i) The verb migrates ONE home per invocation and never
  enumerates profiles — the operator runs it per home, which is what keeps the receipt
  attributable. (ii) Nothing is deleted, the emptied `characters/` tree included; it is the
  tombstone the receipt's `from` refers to, and a test stands on it still being there after
  two runs. (iii) I did not run the migration on the live install — that is the OP strip's
  operator-visible step, and this branch is not merged.

### H3 — the skill catches up, and a pin nobody listed was the one that fought back

- **[MEASURED] The plan's §D disposition table under-counted the hermes side: the seed
  contract is pinned HERE, not only in the launcher.**
  `tests/agent_runtime/test_persona_skill_policy.py` is listed as "RETARGET — the verb-table
  set pin gains `migrate-home`", and that is true and was cheap. What the table does not say
  is that the same file holds the pin on that seed line too. Its name when this entry was
  written, `test_charsheet_skill_states_the_launcher_bindings_home_in_its_landed_meaning`,
  is gone. The inversion below is what landed, and the surviving pin is
  `test_persona_skill_policy.py::test_charsheet_skill_teaches_one_install_wide_library_and_no_home_scoping`,
  which pins the skill's copy of the resume seed line **verbatim** — `"last observed home:"`,
  `"never observed by the launcher"`, `"observed the draft readable in"` — as the hermes end
  of the two-repo contract §13.26's rejection (d) created. §A-8 retires that line, so the
  pin does not "retarget": it INVERTS, and it does so in this strip whether the plan said so
  or not. The red is unambiguous:

      assert "observed the draft readable in" in text.lower()
      E  AssertionError

  **Consequence:** a launcher builder doing L1 against §A-8 will find the launcher-side seed
  test and may believe that is the whole contract. It is one end of it. This file is the
  other, and the two must move in the same wave or the skill an agent preloads teaches a
  message the seed no longer composes.

- **[READ] The inverted pin is written to be unpassable by deletion, because the old one
  was.** The retired strings are banned outright and the surviving ones are asserted
  positively: `install-wide`, the library path, §13.27, §13.22 (its reader half still
  stands), `legacy` (a stored observed home is preserved and labelled, never read), the
  seed's closing sentence verbatim, and `provenance, not an address` for the draft's own
  `hermes_home`. That last one is the trap that REPLACED the old one and is the reason the
  field survives §A-3: the key still exists, still carries a real path, and now names a home
  the draft is not under. An agent that reads it as an address chases a directory that has
  nothing in it.

- **[MEASURED] The blanket ban caught my own explanatory prose, and tightening the prose was
  the right answer rather than loosening the ban.** The first draft of the skill's resume
  bullet said "the `last observed home:` line retired with the sighting it quoted" — honest
  history, and it reds the ban. A ban with a carve-out for "but only when you are explaining
  that it is gone" is a ban an agent can pattern-match its way through. The bullet now says
  the seed "used to carry a home line quoting the launcher's most recent sighting" without
  spelling it, and adds the instruction that actually matters: do not wait for one, and do
  not read its absence as the seed being incomplete.

- **[READ] The one teaching that survived the reversal is the one written for a reason that
  no longer applies.** *"Echo the home you resolve; do not assume it."* was a scoping check —
  proof the agent could see the operator's draft. Under one library a wrong PROFILE is
  harmless and a wrong ROOT is a different install, so the same sentence now surfaces a
  mis-resolved root instead. The skill says that explicitly rather than leaving the sentence
  standing with its original justification underneath it, which is the same failure mode as
  the `create()` comment H1 had to rewrite: a true instruction welded to a reason that went
  false.

- **[OWED] The install-hash cycle is captured RED and is not discharged.** After the edits,

      harness-skill-install: FAILED — the installed package differs from this repo for:
      harness-charsheet-authoring
      home X:\Eternia\.hermes  source X:\wt\sharedlib\docs\...  installed X:\Eternia\.hermes\shared\skills

  Repair mode was proved to close the cycle against a throwaway root
  (`ETERNIA_HERMES_HOME=<tmp>` → `refreshed from the repo: ...` → `--check` exit 0), so the
  install takes. It was deliberately NOT run against the live `X:\Eternia\.hermes`, for two
  reasons that point the same way. (i) The gate is discharged on push and this branch is not
  pushed; the pre-push hook runs repair mode and is where a repo edit and a machine are
  supposed to meet. (ii) More important: the live install's hermes is the PRIMARY checkout,
  which is pre-H1 — its `characters list` still answers per-home. Installing a skill that
  teaches one install-wide library onto a runtime that does not have one yet is exactly the
  W3 failure this strip exists to prevent, pointed the other way: the preloaded skill would
  contradict the screen from the moment it landed until the merge. **Owed to whoever lands
  this branch:** run the install (or push, which runs it) AFTER the merge and after OP, and
  re-run `--check`.

- **[READ] What I did NOT do.** (i) The plan's §A-8 wording is what the skill was written
  against, not L1's diff — L1 had not landed when this was written, and §A-8 says the frozen
  text is the authority. (ii) No `CHARSHEET-QA:` change: the line still carries no home, and
  under one library that stopped being a withholding and became "there is nothing to carry".
  (iii) The skill's §13.24/§13.25 references survive as "re-deriving" pointers rather than
  being deleted, so a reader who arrives from the register still lands somewhere — but
  §13.27 is what the sentences now state, and strip D is what makes that register entry
  exist. Until D lands, those pointers are forward references.

### OP + the closing pass — the migration ran, and the thing that proved the design was a turn, not a test

- **[MEASURED] The migration ran on the live install, operator-authorized, and its inventory was
  exactly fact 4's.** Two homes had legacy stores. `base`: three drafts — the two dormant
  `cobalt-robot-courier` exhibits authored by `chara_a2` and W5's `teal-lantern-keeper` — plus the
  installed `cobalt-robot-courier`. `alice`: the id-collision pair `20260824-140756-cd645a` and its
  `.backup-2026-08-25-nefix` sibling, both listing under one id, plus the installed `anime-girl`.
  Everything landed under `X:\Eternia\.hermes\shared\characters`. The sweep of the remaining
  nine profiles turned up nothing. **The second run of each home moved nothing** — `moved: []`,
  `stamped: []` — which is §A-4's idempotence claim measured on a populated install rather than
  on a tmpdir.

- **[MEASURED] `updated` came through byte-identical, and the exhibit that proves it is the one
  that would have been destroyed.** `anime-girl`'s draft still reads
  `2026-08-26T01:01:00.818441+00:00` after the move. That is the recorded-home wave's §E.8 lesson
  holding across a second verb and a real filesystem: the stamp writer never goes through
  `_save()`, and the dormant drafts — the ones whose whole value is that nothing has touched them
  — are exactly the population an unconditional re-serialisation eats first. `authored_by` on the
  two `chara_a2` exhibits is likewise unchanged; they travelled as mis-attribution evidence, which
  is what they are for.

- **[MEASURED] The proof that the design COMPOSES is a live agent turn, and no test in this wave
  came close to it.** After the skill was reinstalled, the QA persona instance ran an authoring
  turn and created draft `20260828-052440-cc39bb`. It landed in the shared library — install-wide,
  as ruled — carrying `hermes_home: X:\Eternia\.hermes\profiles\launcher-qa`, the profile whose
  turn authored it. **That single row is §A-3 and §A-1 composing on one file:** the location
  stopped being per-home and the provenance stayed truthful about a home the draft is *not* under.
  It is also the honest answer to the question H1's entry left hanging — whether a field
  re-derived by argument would read as a mistake once real data hit it. It does not. It reads as
  the only record saying which profile stood behind that turn.

- **[MEASURED] The H3 install-hash cycle is DISCHARGED, and its precondition was met in the right
  order.** H3 captured its red and deliberately left the install un-run because the live hermes
  was still pre-H1 and a skill teaching one library onto a runtime without one is the W3 failure
  pointed backwards. That ordering held: the merge landed, OP ran, and only then was the skill
  installed against the live `X:\Eternia\.hermes`, with `--check` exiting 0 afterwards. The
  owed-to-whoever-lands-this note in H3's entry above is closed by this paragraph. **Then this
  strip paid the cycle a second time and on purpose:** marking `migrate-home`'s operational note
  EXECUTED is a `SKILL.md` edit, so `--check` went `DIVERGED` (repo `2a134c39c2b24252` vs
  installed `1b62edd62b2c67b3`) before the install and `ok — every canonical package installed and
  current` after it. Cheap, and the point is that it is unavoidable: any true sentence added to
  the skill costs an install, which is the property that keeps the runtime's copy honest.

- **[MEASURED] The MCP admission field gate CLOSED: four consecutive live turns admitted 0 → 3 →
  3 → 3.** The serve-diagnostics slice left this owed in as many words — "whether the parked-wake
  actually ends the alternation across four consecutive live turns" — and the OP run plus the
  authoring turn generated the traffic to answer it. The leading zero is the parked wake being
  paid after idle, then stable; the pre-fix behaviour alternated 3/0/3/0 indefinitely. The
  discriminator that entry filed (a tiny `profile_timing.mcp_admission_ms` on a parked turn) is
  what makes that first zero readable as a park rather than a regression.

- **[STILL OWED] The other field gate from that slice did NOT close, and nothing here touched
  it.** Whether a real launcher client tolerates the additive `request_progress` frame is still an
  argument (unknown events are ignorable) and not a measurement. No live launcher has been driven
  against a serve emitting it since. It stays owed, in those words, and this paragraph exists so
  that the closure of the admission gate above is not read as closing both.

- **[MEASURED, and it cost the OP run its cleanest receipt] A docstring is parsed, and this one
  had an invalid escape.** Every import of `agent.charsheet.draft` printed
  `SyntaxWarning: invalid escape sequence '\ '` — the migrate-home docstring wrote
  ``:class:`~pathlib.Path`\ s``, the Sphinx idiom for gluing a suffix onto a role, inside a plain
  (non-raw) string. It was observed on the operator's own screen during the migration, which is
  the only reason anyone saw it: nothing gates warnings on import. Reproduced as a hard red with
  `python -W error::SyntaxWarning -c "import agent.charsheet.draft"` (a `SyntaxError` naming line
  198) and fixed by rewording the sentence so the role ends the phrase and a plain word follows
  it — "Path arguments" — rather than by making the docstring raw: no other docstring in the file
  carries a backslash, so a lone raw prefix would have been a second convention on one function. **Consequence worth keeping:** the class here is not "escape
  sequences"; it is that prose written for a documentation renderer this project does not run got
  into a file the interpreter parses on every import.

- **[READ] What I did NOT do.** (i) Nothing is pushed — both repos are landed on local `main` only,
  and the pre-push hook's own install-hash discharge is still the operator's. (ii) No Stage C: the
  wave built no on-screen proof of the one library and said so up front, and the row is filed on
  the launcher's Mission Control queue rather than skipped quietly. (iii) The authoring instance
  `personainst_chara_a2_7b31d0e4` was moved off `opencode-zen/big-pickle` to
  `openai-codex/gpt-5.6-terra` at reasoning-effort medium after a provider 500 was measured live
  — operator-ruled, verified answering, and recorded here because a model binding that changed
  under a wave is the kind of fact a later reader will otherwise attribute to the wave.

## 2026-08-28 — the walk-se seam split, and what actually cut the pose (slicer slice)

Standing in the hermes repo, worktree off `b20fa8daf9`. Sent to fix a real row failure:
`row 'walk-se' produced no sliceable strip in 3 attempts; last failure: frame 3 contains
multiple separated subjects`. Two cached artifacts reproduced it exactly
(`…_135943_81776461.png` frame 3, `…_135855_76c03b99.png` frame 6). The fix I was handed
was right about the repair and wrong about the wound.

- **[READ] Nothing drew a seam, and the chroma key did not cut anything.** The brief said
  the provider drew faint horizontal lines that `remove_background` keyed out. Measured, the
  keyed strip has exactly **8 connected components** — one per pose, each whole. The severing
  happens later and entirely inside our own code: the gutter path crops each pose into a
  narrow ~221px column, and `_isolate_slot_subject` runs `_erase_long_axis_lines` on that
  column. That helper deletes thin rows spanning ≥85% of the image as drawn floors/dividers —
  a sound rule against a 1774px strip, a destructive one against a 221px slot where the
  character's own body spans the width. On frame 3 it deleted rows 426 and 450-451 (the only
  wide-row groups ≤4 rows tall) and cut one pose into three slabs, y 303-426 / 427-450 /
  452-581, gaps of 1px and 2px. **Consequence:** the artifact is not evidence of a bad roll.
  Do not re-roll a row on this error, and do not go looking for seam lines in the PNG.

- **[READ] The guard was catching the worst frames, not the defect.** Every frame in both
  artifacts was severed — 2 slabs in most, 3 in two of them. `_validate_extracted_frames`
  only raises at ≥3 subjects, so six frames per strip were sailing through as half-poses and
  would have composed into the sheet as such. The row that failed loudly was the lucky one.
  **Consequence:** any charsheet built from a strip that took the gutter path before today is
  suspect even if it never raised.

- **[FIXED] `_merge_related_boxes` is now symmetric.** It had one rule — overlap vertically,
  hairline gap horizontally — for capes and held props, and no mirror, so stacked slabs with
  near-total x-overlap and a 1px y-gap stayed three subjects forever. It now also merges on
  `h_overlap >= min_w * 0.45 and y_gap <= max(14, min_h * 0.22)`. Both artifacts now yield 8
  frames, each a single subject at full pose height (278-283px and 295-306px, spread ≤11px),
  where before the accepted frames were slabs.

- **[FIXED] `auto` finally means what both call sites always said it meant.**
  `_validate_extracted_frames` ran unconditionally, so `method="auto"` — documented in
  `pipeline.generate_row_strip` and `orchestrate` as the lenient last attempt that never
  raises — raised anyway, which is what promoted one flaky roll to a dead row. It now takes
  `strict=`, and `extract_strip_frames` passes `strict=(method == "components")`. Only the two
  judgement-call checks downgrade to a logged warning; **wrong frame count and empty frame
  still raise under both methods**, because best-effort must never mean installing blank
  cells. This also closes a second door nobody had hit yet: `compose_draft_frames` re-slices
  already-APPROVED strips with `method="auto"`, so a soft check could fail a sheet build long
  after every row passed its gate.

- **[READ] What I did NOT do.** (i) `_erase_long_axis_lines` is untouched. The real cure is to
  stop applying a strip-scale heuristic to a single-pose slot — either skip it below some
  width, or require the erased row to span the STRIP rather than the crop. I repaired the
  damage instead of preventing it, because the merge is also the right answer for a genuine
  keyed seam, but the next slice into this file should consider fixing it at the source.
  (ii) The regression tests live in `tests/agent/test_pet_generate.py`, which is skipped
  unless `HERMES_RUN_SLOW_PET_TESTS=1` — so this bug's guard does NOT run in a default suite.
  That is where atlas coverage already lives and I did not restructure the gate, but it means
  a re-break will be silent. (iii) No ruff: it is not installed in any interpreter on this
  machine (not the runtime venv, not `C:\Python312`, not on PATH), so the changed files are
  test-verified only. (iv) Not proven end-to-end through a live `characters` generation — the
  proof is the two cached artifacts plus synthetic strips, not a fresh provider roll.

### Appendix, same day — the root got fixed, and the fixture lied twice

The entry above closed with "the next slice should consider fixing it at the source". The
coordinator promoted that, so it is done. Four things worth keeping, two of them about how
hard it was to write an HONEST test for this.

- **[FIXED] Line erasure is hoisted to strip scale; the slot crop never repeats it.**
  `_isolate_slot_subject` no longer calls `_erase_long_axis_lines`. `extract_strip_frames`
  calls it once, on the whole strip, at the moment it falls off the clean path — lazily,
  because it is a full per-pixel pass and the happy path should not pay for it. Both
  slot-cutting routes (`_slot_crops` and the gutter path) now consume that already-cleaned
  strip. Chose caller-hoisting over the two alternatives I was offered: an explicit
  `erase_lines=` flag would have been dead weight (both callers of `_isolate_slot_subject`
  are slot-scale, so every call site would pass `False` forever), and an aspect/width
  threshold would have been a magic number guessing at the very thing the caller already
  knows for certain. The caller does decide — it just decides once, at the top, where the
  strip still exists.

- **[MEASURED] The real repair, on the two cached artifacts.** Interior transparent rows
  inside each pose's own bbox, before → after: `…_81776461.png` **12 rows across 5 of 8
  frames → 0**, `…_76c03b99.png` **15 rows across 4 of 8 frames → 0**. Frame 3 of the first
  file listed rows `426, 450, 451` — the exact rows named in the entry above, which is the
  cleanest confirmation available that the mechanism was identified correctly.

- **[READ] The defect was not only scanlines; it silently beheaded poses.** Building the
  synthetic case I found a frame with NO scanline that was 23px shorter than its neighbours
  (256 vs 279). When the erased row falls near one end of a pose, the smaller slab is left
  below `_isolate_slot_subject`'s keep threshold and is dropped as noise, so the pose loses
  its head and the frame closes up around the loss. A hole is detectable; this is not. The
  test asserts uniform pose height as well as absence of scanlines, because the
  scanline-only assertion passed on a frame that had lost its head.

- **[READ] What actually discriminates a floor from a body row is ALIGNMENT, not width.**
  My first two fixtures were wrong and both were wrong in the instructive direction. Eight
  identical poses each carrying a wide bar at the SAME height really do span the strip —
  that is a floor by any definition available to us, and the eraser removing it is correct
  behaviour, not a bug. Drawing the poses narrower to dodge that just routed the strip down
  the uniform-slot path, where the slot width IS the strip width over eight and the two
  scales cannot disagree, so the test passed for no reason. The fixture only became honest
  once poses varied frame to frame, which is what real art does and precisely why the live
  strip had nothing erased at strip scale while its slots were being cut to ribbons.
  **Consequence:** if a provider ever draws the same wide feature at the same height in all
  eight poses, we will erase it and we will be right to. Do not "fix" that later.

### Appendix 2, same day — the corollary was wrong within the hour

The appendix above ended: "if a provider ever draws the same wide feature at the same
height in all eight poses, we will erase it and we will be right to. Do not fix that
later." A compose run inside the hour produced exactly that shape and we were **wrong to**.
Recording it in full, because the reasoning that produced the bad corollary was the good
reasoning of the entry before it, applied one step too far.

- **[MEASURED] The shape is anatomy, and it is only at the diagonals.** The installed
  moss-golem sheet had interior transparent scanlines in the two SE rows and nowhere else.
  On the source strips (`…/20260828-052440-cc39bb/strips/`): `idle-se-1.png` has wide-row
  runs `(485,489)` — four rows — and `(522,613)`; `walk-se-1.png` has `(386,390)` and
  `(394,429)`. In both, the thin band that got erased sits a handful of rows above a thick
  band that did not. That is one continuous silhouette, not a line near a body. The `s`
  control has a single 85-row run and no gaps at all. At a diagonal the chin/shoulder
  contour aligns across all eight poses; head-on it does not, which is why only SE and NE
  can produce this.

- **[READ] Coverage-thinness cannot discriminate a floor from anatomy. Full stop.** The two
  are the same measurement: a thin band covering >=85% of the strip. The previous appendix
  reasoned that eight aligned bars "really do span the strip, so that is a floor by any
  definition available to us" — true about the definition, false about the world. The
  definition was the defect.

- **[FIXED] The discriminator is background context, and it separates cleanly.** A drawn
  floor crosses the BACKGROUND between poses; aligned anatomy has body directly above and
  below every column of it, because it is the silhouette's own widest row. `_erase_long_axis_lines`
  now locates a band by width exactly as before, then erases only the columns whose local
  vertical context (3px, judged against the ORIGINAL mask — the erase must not eat its own
  evidence) is empty. Measured: `idle-se` band 1320/1342 columns are body-context (98.4%),
  `walk-se` 1857/1871 (99.3%), a synthetic drawn floor 0/1664 (100% background-crossing).
  Not a threshold that needs tuning — the populations do not overlap.

- **[READ] Why not the segmentation-only design.** The other option on the table was to use
  the erase purely as a segmentation mask and crop output frames from the unerased strip,
  letting `_isolate_slot_subject`/`_drop_side_bleed` drop floor fragments as detached
  components. Measured, it does not work: a floor touching the feet makes the whole strip
  ONE component and the slot crop ONE component, so nothing is detached and the floor
  survives into every frame. It would have traded a scanline for a bar across every cell.

- **[READ] Accepted residue, stated plainly.** A floor's stub directly under a pose's own
  feet has body above it and is kept. It merges into the pose it touches and bridges
  nothing, so segmentation is unaffected; it is cosmetic and it is the price of never
  cutting anatomy again. The `s`-row control and both walk-se cache artifacts from the
  earlier entries still extract clean, and pose heights are unchanged (no beheading).

- **[READ] The lesson under both appendices.** Twice now the fix was right and the stated
  justification for its BOUNDARY was wrong — first "a slot-scale wide row is a floor", then
  "a strip-scale aligned wide row is a floor". Both times the boundary was drawn from what
  the code could measure rather than from what the artifact is. If the next slice finds
  itself writing "X is Y by any definition available to us", that sentence is the bug.

## 2026-08-28 — the QA crop had its own frame grammar, and it cut characters in half

An operator opened `walk-e-attempt-1-frame-1-x1.png` fullscreen and found half a character.
The mechanism was not in the picture code at all: the strip had **two boundary rules**, and
the QA surface was reading the wrong one.

- **[MEASURED] The severing, on the strip that shipped it.**
  `revisions/row@walk-e/attempt-1.png` is 2172x724, 8 frames. The poses the model drew sit at
  x `(66,298) (340,578) (630,839) (900,1094) (1139,1359) (1390,1626) (1684,1877) (1927,2116)`.
  The even-slot boundaries are `0,272,543,814,1086,1358,1629,1900,2172`. Frame 0's slot ends at
  272 against a pose reaching 298, so the crop stopped 26 columns into the body and the cut
  edge stood as a **205px column of body pixels flush against the frame's right side**. Six of
  the eight frames were severed on at least one side — measured edge columns L/R before:
  `0/205, 199/237, 232/183, 170/93, 91/52, 42/0, 0/0, 0/0`. After: `0/0` on all eight. Frame 0
  recovered 3163 body pixels (43517 → 46680).

- **[READ] The defect is a duplicated grammar, not a bad constant.** `pipeline.frame_cell`
  divided the strip's width by the frame count and called the result a frame. The real frame
  extraction (`atlas.extract_strip_frames`) has been content-aware since it was written,
  *precisely because* even slots are wrong on real strips — that is the same lesson the two
  appendices above are about. So the package already knew; the knowledge just was not reachable
  from the surface whose entire job is to show an operator the truth. This is the launcher's
  same-day bug in a different repo: a second hand-rolled grammar standing beside the real one.

- **[FIXED] One authority, exported as a value.** `atlas.frame_x_bounds(strip, frame_count)`
  returns the per-frame `(left, right)` in strip coordinates, running exactly `method="auto"`'s
  order — key the background, erase strip-spanning floors, `_frame_x_ranges` (gutters merged
  down to the frame count), sever expected boundaries and retry, and only then `_slot_bounds`.
  `frame_cell` crops the SOURCE at those bounds, so the operator still sees the provider's own
  pixels, magenta field and all; `extract_strip_frames` is untouched. The x-bounds are a value
  and not a set of frames because a QA crop cannot use extracted frames at all — those have been
  keyed, isolated and re-fitted, which is three edits away from what the provider returned.

- **[READ] Width is the pose's, height is the strip's, and the asymmetry is deliberate.** A
  frame boundary is a fact about the row that can be read off the pixels. A subject's top and
  bottom are not — trimming to them would be the module guessing which pixels the operator came
  to look at. Full strip height stays.

- **[READ] The pad is clamped to the neighbour's content edge, and the extraction's is not.**
  Both add ~4% of a slot as breathing room. The extraction can afford a raw pad because it
  cleans slivers out of every crop afterwards; a caller cropping the source verbatim cannot, so
  `frame_x_bounds` clamps the pad so it can never reach another pose's pixels. Where poses truly
  touch, the margin goes to zero and content sits flush — honestly, because the source does.

- **[MEASURED] Four existing tests asserted the defect.** `round(source.width / frames)` was
  written into two size assertions, and two deep-zoom tests only cleared the console budget
  because the old cell was a whole slot wide (`--scale 10` on a 63px-wide content cell is
  1.2M px, under the ceiling; on the old 256px slot it was 4.9M). The zoom factor is now
  computed from the cell the row actually crops to. **The tests were measuring the fixture's
  slot arithmetic and calling it a contract** — the same failure mode as the 2026-08-24 mutation
  audit, where a +3px shift on both bounds left the whole suite green because every assertion
  read the cell's SIZE. Size assertions cannot see this class of defect. The new tests read pose
  mass per column.

- **[READ] Cost, since it is now a keying pass and not a division.** 0.371s for one
  `frame_cell` on the 2172x724 strip, decode included — the strip is decoded once and the open
  image handed to the geometry. `thumb` writes one crop, so this is not on a hot path.

- **[READ] Left alone, on purpose.** `extract_strip_frames`'s lenient branch still composes
  those same helpers inline rather than calling `frame_x_bounds`. The boundary RULE is shared
  (`_frame_x_ranges`), so the two cannot disagree about where a gutter is, but the composition
  is written twice and could drift. Folding it in means touching the generation gate, which was
  fenced out of this slice.

## 2026-08-29 — the square hero crop, and payloads that name their own successor (Stage 4 slice)

Stage 4a and 4b of `planned/charsheet-turn-efficiency-2026-08-29.md`, built as two commits on
top of the same-day frame-geometry fix above. Both are about the same currency: a charsheet
turn's cost is API round-trips, and a round-trip late in a heavy turn re-sends the whole
context at 60–120k prompt tokens. 4a removes a *looking* round-trip (open the fullscreen
viewer to see what the card cropped away); 4b removes a *finding out what to run next*
round-trip.

### 4a — `thumb --square`

- **[READ] The card is not the defect; the shape mismatch is.** The console hero card is a
  fixed 1:1 centre-cover square (§13.17, ruled — it is not moving) and a character cell is
  taller than it is wide. Centre-cover on a tall crop draws the middle of the frame and calls
  it the frame, so an operator glancing at a card sees a torso zoom. The card was ruled "never
  the verdict surface" and that ruling stands; the confusion is still real, and it is cheaper
  to fix on the hermes side than to argue about which surface is authoritative.

- **[FIXED] One pad step, last, and it is the only step in the looking procedure that cannot
  remove a pixel.** `pipeline.pad_to_square` centres the FINISHED crop on a square field of
  `QA_BACKDROP` — the same ground `upscale_on_backdrop` composites on — with
  `side = max(width, height)`. It runs *after* the NEAREST upscale on purpose: a pad applied
  before would be enlarged along with the art, and the margins would stop being a known flat
  colour. It sits downstream of `frame_cell`'s content-aware bounds and touches none of that
  geometry. The default stays tall, because a compare pair's panes align on today's shapes and
  a pad changes the aspect.

- **[MEASURED] On the real strip, not a fixture.** `walk-e` attempt 1 of the live fire-imp
  draft (2172x724, 8 frames): bare crop **508x1448**, square **1448x1448**, margins **470px on
  each side**, top/bottom 0 (height was already the longer axis). The interior window is
  byte-identical to the bare crop, the margins are exactly `(18, 18, 22, 255)` and nothing
  else, alpha is 255 everywhere, and the content's horizontal centre is **724.0** against an
  image centre of **724.0**. Opened fullscreen: the pose is whole.

- **[FIXED] Both budget booleans are weighed on the PADDED output, and so is the refusal.**
  Padding raises the pixel count and the file a consumer decodes is the padded one, so
  computing the flags on the intermediate crop would mean declaring a card on the strength of
  a picture nobody wrote. The consequence is real and worth stating: **a square crop can be
  refused at the default scale where the bare crop of the same cell passes.** Measured on a
  1536x3120 draft, one cell at `--scale 3`: bare 750x2400 = 1.8M px, both flags true; square
  2400x2400 = 5.76M px, **both false** — over the fixed console ceiling AND heavier than the
  sheet the crop exists to avoid decoding. The refusal names the padded size and adds "or drop
  `--square`" to its escapes, because arguing about the unpadded size would be arguing about a
  file nobody asked for. `pad_to_square` checks the write ceiling before it allocates.

- **[READ] Two shapes, two files, one cell.** The filename gains `-sq`
  (`walk-e-attempt-1-frame-1-x2-sq.png`) so a hero crop and a compare crop can sit in
  `thumbs/` at the same time, and the payload carries `square` **unconditionally** — a
  consumer deciding *where* to draw a crop cannot infer the shape from a filename, and the
  default is a shape too. Same rule as the two budget booleans beside it.

### 4b — `next`, the machine hint

- **[MEASURED] What this is paying for.** The fire-imp one-shot spent `characters --help` plus
  SIX per-verb `--help`s working out which verb came next and how to spell it — every one a
  full round-trip re-sending the turn. The verb table answers all of it, but only for a caller
  that has the skill in context, and the persona that ran it did not.

- **[FIXED] The hint is a COMMAND, not a verb name.** `{"verb": ..., "cmd": ...}`, additive and
  optional. A bare verb name still costs a `--help` to turn into a command line, which is the
  round-trip being removed. Spelled with the `hermes` entrypoint the skill teaches, not
  `python -m hermes_cli.main` — both resolve, but the long one is noise in every trace row and
  the operator trace truncates at 500 chars.

- **[FIXED] Three arms, and each one reads STATE rather than a plan.**
  `start` → `turnaround`, except a draft started without `--base-image` (the CS-5 repair
  shape), which is pointed at `base --image <image>` instead, because `turnaround` refuses
  without the anchor and a hint naming a verb the draft would refuse costs exactly the
  round-trip this key exists to save. `approve-direction` → `rows` **only when the approval
  advanced the stage**; a payload whose hint disagrees with its own `advanced: false` is a
  payload arguing with itself. A failed `rows` → `reroll-row --row <the row it died on>` with
  the resume `rows --only <the rows that never landed>` as `alternatives[0]`.

- **[READ] The failed-`rows` rows come off the draft's pending list, intersected with what was
  asked for.** Not off the error text: parsing a message for a row key is a grammar that drifts
  from the message. And the intersection matters — a caller who ran `--only walk-e` must not be
  handed a resume naming eleven rows they never wanted. Spec order makes `pending[0]` the row
  the loop stopped on.

- **[MEASURED] The first cut of the failed-`rows` hint was wrong, and an existing test caught
  it.** `rows` refuses for two different reasons, and only one of them is a generation failure:
  called at stage `turnaround` it bounces as OUT OF ORDER, at which point every row is
  "pending" and the hint cheerfully offered `reroll-row` — which is exactly as illegal at that
  stage as the `rows` that just bounced. A hint sending the caller at a second refusal is worse
  than no hint. `test_an_out_of_order_verb_reports_the_pets_error_shape` failed because it
  asserts the refusal payload dict EXACTLY, not with `>=`; a subset assertion would have let it
  through. The hint is now gated on `draft.stage == "rows"`. Worth stating twice: my own
  targeted `-k` filter ran green over this the whole time — the full suite is what found it.

- **[READ] Absence is an omitted key, not `null`.** A verb with no next step omits `next`. A
  refusal with no pending-row story keeps the flat pets error shape exactly — `ok`, `error`,
  `draft`, `stage` and nothing else — which is what a shipped launcher panel parses. `next` is
  additive under the superset rule (SKILL.md:206–208): it adds a key and takes nothing away.

- **[READ] `compose`'s refusal was left alone.** It already hands its verb in prose, and making
  it uniform was not free. Two more gaps stand open, deliberately out of this slice's scope:
  `turnaround` hands no hint (its successor is `approve-direction`, so the spine has a hole in
  the middle), and no hint carries `--square` — the crop verb has no successor to name.

- **[MEASURED] The failure fixture is the production path.** The failed-`rows` test drives a
  draftsman that returns a bare chroma field for `walk-e`, so `generate_row_strip` genuinely
  exhausts its three attempts ("could not segment 2 padded sprites from strip", then "frame 0
  is empty") and raises. Stubbing the raise would have tested the hint against a failure shape
  the code cannot actually produce.

- **[READ] For the skill (handoff, not written here — SKILL.md has another owner tonight).**
  The reply contract should now recommend **`--square` for hero-card thumbs and bare crops for
  compare pairs**, and it can stop teaching agents to probe for the next verb: the payloads
  carry it.
## 2026-08-29 — the wait cadence changed: 600 s on this lane (Stage 3a slice)

- **[READ] `process wait` now blocks up to 600 s on the mission-chat lane, not 180.** The
  clamp lives in `ProcessRegistry.wait` and reads its ceiling from the new
  `wait_ceiling_seconds`: `TERMINAL_TIMEOUT` (180 s by default) everywhere, raised to at
  least `MISSION_CHAT_WAIT_MAX_SECONDS` (600) when the run's terminal envelope scope names
  the mission-chat lane. **Consequence for an authoring agent: pass `timeout: 600`, not
  180 and never 60.** A 10-strip `rows` batch at 1–2 min per generation is 10–20 min of
  wall; at 180 s that is 4–7 forced round-trips, at 600 s it is 2–3, and every avoided
  round-trip is a whole re-sent context (~60–120k prompt tokens on a heavy turn).

- **[READ] The clamp still tells you when it bit.** `timeout_note` is unchanged in shape —
  "Requested wait of Ns was clamped to configured limit of Ms". If you see 180 in that note
  on a charsheet turn, you were NOT running under the mission-chat envelope scope, which is
  worth reporting: it means the lane identity did not reach the tool.

- **[READ] Nothing outside this lane moved, deliberately.** The raise is a `max()` against
  the configured `TERMINAL_TIMEOUT`, applied only when the envelope scope says
  `mission_chat`. A deployment that configured 900 s keeps 900 s; every other lane keeps
  exactly the old number. The turn-efficiency plan named a blanket raise a non-goal because
  no other lane's turn budget was measured.

- **[READ] The 1800 s turn wall — not this clamp — is what bounds a runaway.** Two 600 s
  blocks still leave headroom inside a default charsheet turn budget. There is no timer of
  this lane's own, and there should not be one.

## 2026-08-29 — you can now END the turn on a generation: `process notify` (Stage 3b slice)

- **[READ] `process notify <session_id>` arms a process-exit delivery, and then you STOP.**
  Fire the long verb with `terminal(background=true)`, call
  `process` with `action: "notify"` on the returned session id, and end your turn. When
  that process exits, a NEW turn arrives in this same chat thread carrying the exit code
  and the output tail, opening with `[BACKGROUND PROCESS COMPLETE — …]` and a line saying
  you asked for it. **Consequence: the wait-poll bucket goes to zero on a staged
  generation** — and the operator's console is free during the 10–20 minutes a `rows` batch
  runs, instead of being held by a turn that is only polling.

- **[READ] It rides the delivery road that already existed, not a new one.** The exit
  publishes onto the same `process_registry.completion_queue` the serve drain
  (`dispatch_delivery.drain_background_completions`) has always consumed, and the drain
  forges the same kind of turn a detached `agent_chat_send` reply comes back in. So the
  same rules apply, unchanged: the delivery lands only when your thread is IDLE (it is
  never spliced into a turn in flight), and it is deduped by the chat lane's own
  idempotency.

- **[READ] The four edges, so you can trust it.** (1) If the process ALREADY exited when
  you call notify, the receipt is queued immediately — the wake-up is not lost. (2) Calling
  notify twice on one session is one row and one delivery. (3) If the process NEVER exits,
  nothing is delivered — there is deliberately no timer; the turn wall is the guard, and
  the fallback for an agent that would rather block is the 600 s wait ceiling above.
  (4) If your persona instance is retired before the process exits, the request is dropped
  with a logged line rather than delivered into a thread nobody owns.

- **[READ] Notify is refused, honestly, when there is no thread to deliver into.**
  `status: "unavailable"` with a message pointing at `process wait` — that happens off the
  mission-chat lane, or when the chat root no longer resolves to a live persona instance.
  A refusal is not a failure of the run; take the wait instead.

- **[READ] The durable record is a drain file beside `processes.json`** in the
  background-work home (`process_notify_requests.json`), carrying `{turn_id, session_id,
  persona_instance_id}` plus the chat root it must be delivered into. Recorded at request
  time because that is the only moment the run knows who is asking — the same reason the
  dispatch lane records a sender before its target starts working.

## 2026-08-31 — one command drives the whole pipeline: `characters auto` (Stage 5 slice)

R-3 was the last open ruling on the turn-efficiency plan and it is ruled YES (decision-close
wave, RD-10). The verb exists. What follows is what it does, what it deliberately does NOT
do, and the two readings of the stage text that would have made it destructive.

- **[DO] Use it only when the operator asked for the whole thing in one go.** `auto` approves
  the turnaround itself, and approving the turnaround is the last moment a reference can
  change. A staged ask ("make the turnaround, show me") still gets the staged verbs. This is
  a rule about the ASK, not about your confidence.

- **[DO] Fire it in the background, `notify`, end your turn.** One process, 10–20 minutes,
  and every receipt is flushed the moment its stage lands — so `process log` mid-run shows
  real progress, not silence. That pairing is the whole point: the measured 27-call fire-imp
  turn spent twelve calls asking a blocked pipeline whether it was done.

- **[READ] The output is a STREAM, and it is the only `characters` verb that is.** With
  `--json` every line is ONE compact object — `emit_json`'s indented block would make the
  newline framing meaningless — and the LAST line is always the summary
  (`"step": "auto"`), carrying `ran`, `skipped` (each with its reason), `through`, and on a
  failure `stopped_at` and `error`. Every other line is the payload its own verb prints
  today plus a `step` key naming the verb. Read the last line for the verdict; read the rest
  for the images.

- **[DO] Emit a `CHARSHEET-QA:` line for every receipt line.** One command is still three or
  four stage changes, and the reply contract is per stage change, not per command. An `auto`
  run reported as a single "done" is the same invisible turn the contract exists to prevent —
  and it is now easier to write, because every stage handed you its payload on its own line.

- **[READ] It resumes; it does not restart.** The stage text reads like a flat script, and a
  flat script here is destructive twice: `run_turnaround` re-rolls every direction reference
  AND clears the approvals, and `rows` with no `--only` regenerates every approved strip. So
  the plan comes off the draft's own state — `status --json`'s `missing.turnaround` and
  `pending.rows`, the same two lists the `next` resume hint reads. A reopened, complete draft
  costs ONE step (compose), generates nothing, and says on the summary why it skipped the
  other three. This is what makes `auto` the one-command repair after `reopen`.

- **[READ] It cannot override a handedness refusal, and it will not suggest one.** There is
  no `--accept-handedness` on this verb, and its compose refusal carries no `next` hint —
  the only hint available would be the override itself, and an autopilot nudging an operator
  to waive the mirrored-art gate unseen is exactly why the stage was gated. When compose
  refuses, look at the rows it named, then run `compose --accept-handedness` yourself, by
  hand, per row.

- **[READ] Refusals stay inside the stream.** A bad `--draft`, a composed draft, a row batch
  that dies mid-flight: each writes a refusal LINE (flat `ok`/`error`/`draft`/`step`, plus
  the 4b `next` hints where an honest one exists) and the summary still follows it, so the
  last line is the verdict even when the run failed. Exit code 2, like every other refusing
  charsheet verb.

- **[READ] `--through` stops it early** at `turnaround`, `approve-direction`, `rows` or
  `compose` (default). Useful when the operator wants the references drawn but reserves the
  approval for themselves: `--through turnaround` generates and stops, approving nothing.

<!-- A2, A3, R1 and any slice standing in the HERMES repo: append your entries above this
     line, under the matching heading, or add a heading if none fits. Then say in your
     slice report that you did.

     Standing in the LAUNCHER repo? Write to
     EterniaLauncher/docs/spatial/CHARA_CONSOLE_AUTHORING_FIELD_NOTES.md instead — unless
     what you learned is about a hermes payload, verb or home, which belongs here even
     then. Do not write to both. -->
