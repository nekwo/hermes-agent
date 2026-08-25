# Field notes — charsheet authoring (running record)

**What this is.** The accumulating input to `SKILL.md`'s final pass. Every slice in the
console-character-authoring program appends what it LEARNED here as it lands; the skill
is rewritten from this file at the end of the program, not before.

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

## Looking at a sheet

- **[READ] The console's MEDIA hero card is a fixed 1:1 centre-cover square.**
  `local_image_attachment.dart::_LocalImageBlockViewState._buildPreview` builds an
  unconditional `AspectRatio(kInlineImageUnknownAspectRatio /* 1.0 */)` over
  `Image(fit: BoxFit.cover)` — no dimension probe anywhere above it. The square's side is
  `min(widthCap, heightCap)` and the HEIGHT cap usually binds (440 at 1280×800, 594 at
  1920×1080, 720 at 2560×1440).
  *Consequence:* a tall crop is judged on its middle fifth. A defect outside that square
  is invisible until the image is opened. Declare crops knowing this.

- **[READ] Side-by-side is what settles a seam; magnification is not.**
  Two independent agents each failed to identify the known seam at 5–6× on a single
  frame, and each had a "sharpest dark row" scan rank real art (the sock band, the skirt
  hem) above the actual defect. Both settled it by rendering attempt N beside attempt N−1,
  aligned.
  *Consequence:* compare, don't zoom. A brightness heuristic finds the character, not the
  defect.

- **[READ] The seam's contrast is directional.** On the known artifact the band reads
  28.6 against 135.4 immediately BELOW it, while above it the hair darkens gradually over
  ~50 rows. It is a hard lower boundary, not a dark band between two bright regions.
  *Consequence:* "look for a dark band" is the wrong search; look for a hard edge.

## Payload

- **[READ] Attempts are 0-based in payloads and flags, 1-based in human lines.**
  One helper (`_attempt_label`) renders "attempt N of M". Store filenames are
  `attempt-<n+1>.png`.
  *Consequence:* a QA card RELABELS, never renumbers.

- **[READ] `cardSafe` answers the question; the thresholds are not the consumer's.**
  `thumb` carries `cardSafe`, derived from `MAX_CARD_PIXELS` (itself derived from
  `CHAR8.sheet_size()`, so it cannot drift). Above the default scale a crop is written but
  flagged; at or below it an over-budget crop is refused, naming `--scale 1`.
  *Consequence:* only declare a `cardSafe: true` crop with `MEDIA:`. Never carry a copy of
  the threshold.

- **[READ] Absence travels as JSON `null`, not `""`** for `history[].path`, `current`,
  `approvedPath` — and, since `91e23bf0c5`, `baseImage` in BOTH `status --json` and
  `list --json`. The parenthetical here ("`baseImage` was still `""` as of the A0
  review") is now spent: it was the fourth path field, in the same response as the three
  that had already been fixed, and the CLI test pinned the old spelling. Every path in
  those payloads goes through one public helper (`draft.path_or_none`).
  *Consequence:* tolerate both on read (older drafts and other payloads may still carry
  `""`); emit neither as a bare `MEDIA:` line.

## Process

- **[READ] A failed row aborts the batch.** Survivors sit at `attempts: 0`, which is
  indistinguishable in a status dump from "never started". `run_rows(only=None)`
  regenerates every authored row unconditionally.
  *Consequence:* never re-run a bare `rows` to recover; name the survivors with `--only`.

- **[READ] `--only` takes exact comma-separated keys. There is no glob.**
  `run_rows` raises on any key not in the authored set.

- **[READ] Row rerolls are stochastic, row-grained and auto-approved.** One row took three
  strips. An unexamined reroll silently becomes the sheet.
  *Consequence:* look at every reroll before moving on.

- **[READ] The default hypothesis is "the model drew it", not "the code broke it".**
  The one real defect found in anger was generated art, and the pipeline-residue
  hypothesis (slicing, keying, palette lock) cost the most time while being wrong.

---

## Appended by slice

<!-- A2, A3, B0, B1, B2, D1.1, P1, R1: append your entries above this line, under the
     matching heading, or add a heading if none fits. Then say in your slice report that
     you did. -->
