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
  load would make such a draft unreadable rather than repaired — `CharacterDraft.list_drafts`
  drops an unreadable draft with a log warning, so it would vanish from `characters list`
  instead of being explained.
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
  `thumb` carries `cardSafe`, derived from `MAX_CARD_PIXELS` (itself derived from
  `CHAR8.sheet_size()`, so it cannot drift). Above the default scale a crop is written but
  flagged; at or below it an over-budget crop is refused, naming `--scale 1`.
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
  sheet" — a crop can be refused at the default scale (`--scale 2`), with a message saying it
  is "heavier than the sheet this crop exists to avoid decoding", while being genuinely
  lighter than the sheet that draft will compose. Both directions have one answer, the one
  A2 already wrote: weigh a crop against **that draft's own `spec.sheetWidth x
  sheetHeight`** from `status --json`, and read `cardSafe` as the console's ceiling, never as
  a statement about your sheet. Do not carry a copy of the threshold.

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
  the anime-girl sheet went 1536x2080 → 1536x3120 with `idle-*`/`walk-*` untouched at
  attempts 1/1/1/1/1 and 1/3/2/1/1). The new rows are "seeded" by appearing in the spec —
  nothing is written to the revision store — so they read `attempts: 0`, `approved: null`,
  and land in `missing.rows`; `compose` then refuses while any of them lacks an approved
  strip, naming every one.
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

---

## Appended by slice

<!-- A2, A3, R1 and any slice standing in the HERMES repo: append your entries above this
     line, under the matching heading, or add a heading if none fits. Then say in your
     slice report that you did.

     Standing in the LAUNCHER repo? Write to
     EterniaLauncher/docs/spatial/CHARA_CONSOLE_AUTHORING_FIELD_NOTES.md instead — unless
     what you learned is about a hermes payload, verb or home, which belongs here even
     then. Do not write to both. -->
