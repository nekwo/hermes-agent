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

- **[READ] `characters sprite <slug> --json` inlines the whole sheet as base64 and has no
  path-only mode.** `draft.sprite_payload` always emits `spritesheetBase64` from the sheet
  bytes and returns no path or directory for it. Measured on the installed CHAR8 `anime-girl`:
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

<!-- A2, A3, R1 and any slice standing in the HERMES repo: append your entries above this
     line, under the matching heading, or add a heading if none fits. Then say in your
     slice report that you did.

     Standing in the LAUNCHER repo? Write to
     EterniaLauncher/docs/spatial/CHARA_CONSOLE_AUTHORING_FIELD_NOTES.md instead — unless
     what you learned is about a hermes payload, verb or home, which belongs here even
     then. Do not write to both. -->
