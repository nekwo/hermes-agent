# 8-way character sheets — a data-driven sheet spec, a human QA gate, and a reusable image-revision store (Plan H, 2026-08-17)

> **Home.** Hermes repo, beside `MISSION_BOOT_WINDOW_PLAN_2026-08-17.md` (Plan G) and the
> profile-binding plan family. Built in worktree `charsheet-8way`, branch
> `worktree-charsheet-8way`, base `ca19df1e48` (local main tip — deliberately NOT
> `origin/main`, which was two merges stale when the worktree was cut; RAN `git reset --hard
> main`). Launcher read at `X:/Unreal Engine/Engine/Launcher/EterniaLauncher` (files READ,
> no hash pinned — no launcher change ships in this plan).
>
> **Contract authority (2026-08-18, C5).** The sheet contract — row keys, direction
> tokens, row order, cell size, roster — lives ONLY in EterniaLauncher
> `docs/spatial/CHARACTER_8WAY_SPRITE_FORMAT_SPEC_2026-08-17.md`. Hermes conforms to
> it and does not restate it; where this document used to carry a second copy of
> those facts, it now points. What stays here is what is hermes-specific: the
> packaging boundary, the revision store, the QA gate, the CLI verbs, the prompts.

**Evidence tags** (the family's discipline): **READ** (file:line inspected this session) ·
**RAN** (command executed this session) · **RELAYED** (operator statement) · **INFERRED**
(follows from READ code, not executed) · **ASSUMPTION A-n** (unverified; CS-0 verifies
before anything builds on it).

**The operator's ask (RELAYED, across several messages):** 8-way directional character
sprites from the petdex atlas generator; author five directions (N, NE, E, SE, S), mirror
the other three; direction count is *data that travels with the sheet*, "if you find
yourself writing `if 8:` anywhere, that's the wrong shape"; do not break the shipped
petdex payload; upstream code is never edited in the fork. Then, refining: *"1 and 2 sound
good — instead of 3 a QA UI for image editing and selection would be cool in the launcher
mission control … first part the cardinal directions are looked at and then after the
animations … then i can regenerate parts of it if invalid"*; *"when it makes the image
editing module i'd like that re-usable for eternia studio"*; *"i'm thinking of packaging
this into the hermes package that bakes it into the app not just the full mission control
hermes"*; implementation by Opus subagents whose work is checked to production grade.

## 0. Verdict up front — four facts that shaped the plan

1. **`agent/pet/` is upstream code; the launcher's payload builder is not.** RAN
   `git ls-tree upstream/main agent/pet/` — all files present upstream; `atlas.py`,
   `constants.py`, `render.py` byte-identical to upstream (RAN `git diff --stat`, only
   `orchestrate.py` diverges, −30 lines). But `hermes_cli/harness.py` — where
   `_pet_sprite_payload_for_launcher` lives (`:2888-2911` READ) — does not exist upstream
   (RAN ls-tree, empty). The producer of the launcher contract is fork-owned; the pixel
   machinery is upstream and will be **imported, never edited**.
2. **The launcher transport is the CLI, not RPC.** `petdex_client.dart:104,150-163` READ —
   the Flutter launcher shells `hermes harness pets sprite <slug> --json` as a one-shot
   stdout subprocess. RAN `grep -n "pets" agent_runtime/serve_rpc.py` — zero hits. So the
   QA UI's backend contract is *new CLI verbs with `--json`*, exactly like pets; no RPC
   surface is needed for v1 and none is built.
3. **`agent_runtime` is not in the wheel.** `pyproject.toml:370-371` READ —
   `[tool.setuptools.packages.find] include` lists `agent.*`, `hermes_cli.*`, `gateway.*`,
   `tools.*`, `tui_gateway.*`, `cron.*`, `acp_adapter`, `plugins.*`, `providers.*` — no
   `agent_runtime`. The operator wants this baked into the plain hermes app, therefore the
   core CANNOT live under `agent_runtime/` and CANNOT import from it (no `store_root()`,
   no `agent_runtime.locks`). Placement: **`agent/charsheet/`**, a new fork-owned
   subpackage inside the upstream-namespace `agent/` package — the same precedent as
   fork-owned `harness.py` inside upstream's `hermes_cli/`. Ships automatically via the
   existing `agent.*` glob.
4. **The row taxonomy is baked in exactly four places, and the read side is a trap.**
   `atlas.ROW_SPECS` + module constants derived at import (`atlas.py:43-58` READ),
   `compose_atlas` (`:1052-1068`), `validate_atlas` (`:1078-1183`), and the orchestrator's
   row loop (`orchestrate.py:229-316` READ). Everything else in the pixel pipeline is
   row-agnostic. The trap: `constants.state_rows_for_grid` (`constants.py:147-156` READ)
   *infers* taxonomy from sheet height (≥9 rows → Codex, else legacy) — a 10-row character
   sheet pushed through any pet read path would be silently misread as a 9-row pet.
   Character sheets therefore carry their row list explicitly and **never** flow through
   pet readers.

## 1. Substrate as read (what the generator does today)

- **Generation:** base drafts → per-state row strips on magenta, grounded on the chosen
  base via reference-capable providers only (`imagegen.py:27,182-251` READ; both
  `reference_images` and `reference_image_urls` kwargs sent, `:217`; transparent-background
  rejection falls back to opaque + chroma key, `:236-243`).
- **Extraction:** `extract_strip_frames(strip, count, fit=False)` — chroma key, component
  detection with merge/lobe/line heuristics, strict-padding first then lenient fallbacks,
  multi-pose validator (`atlas.py:810-877` READ).
- **Mirror:** `running-left` is never generated; it is a per-frame horizontal flip of the
  approved `running-right` row (`orchestrate.py:283-316`, `atlas.py:1038-1049` READ). This
  is the exact mechanism NW/W/SW reuse.
- **Normalize:** cross-correlation body registration per row, union-crop, ONE global scale
  `K` across all states (uniform apparent size), bottom-anchor
  (`atlas.py:908-1004` READ).
- **Compose/validate:** pack per `ROW_SPECS`, zero RGB under transparency, lossless WebP;
  validate geometry/occupancy/collapse/residue (`atlas.py:1052-1183` READ).
- **Payload:** `_pet_sprite_payload_for_launcher` (`harness.py:2888-2911` READ) — `slug,
  displayName, description, mime, spritesheetBase64, spritesheetRevision, frameW, frameH,
  framesPerState, framesByState, framesByRow, loopMs, scale, stateRows`. The Dart client
  parses named keys and ignores unknown ones (`petdex_client.dart:424-437` READ) and has a
  named diagnostic for the Pillow-degraded empty-metadata payload (`:229-233` READ).
- **Read-side cap:** `render.py` steps at most `FRAMES_PER_STATE=6` columns (`:167` READ),
  so `framesByState` ≤ 6 while `framesByRow` carries true counts; desktop and launcher
  prefer `framesByRow` (`pet-sprite.tsx:283` READ). Characters use per-row counts only.
- **Store:** pets install to `get_hermes_home()/pets/<slug>/{pet.json, spritesheet.webp}`
  (`store.py:1-60` READ). Profile-scoped, plain-hermes compatible.
- **Environment:** Pillow 12.1.1 imports on this machine's Python 3.12 (RAN); no `.venv`
  in the checkout — `scripts/run_tests.sh` probes are CS-0's to verify (A-1).

## 2. The data model — directions as data

New module `agent/charsheet/spec.py`:

**The tokens, the row keys and the row order are NOT defined here** — they are the
launcher's, and the authority is its spec, §A (layout), §C (candidate chains) and §D
(`directionSectors` derivation). What this section defines is the SHAPE that carries
them:

- `StateSpec(name, frames, directional)` — e.g. `("walk", 8, True)`, `("idle", 6, True)`.
  State names are `[a-z][a-z0-9_]*`: `-` is **reserved**, because it is the row key's
  state/direction separator and the launcher splits on the LAST one.
- `DirectionScheme(order, authored, mirrored)` — `order` is the direction vocabulary the
  CONSUMER resolves, `authored` the generated subset and (since H1) the sheet's ROW
  order, `mirrored` a `{derived: source}` map. Every direction
  in `order` must be authored or mirrored, and every token must be one of the eight the
  launcher deriver reads (`spec.DIRECTION_TOKENS`). **The count is `len(order)` —
  nothing tests for 8.** Petdex is expressible as the 2-direction case (`running` ×
  right/left, left ← right) — proof of shape, not a migration; pets stay on their own
  path untouched.
- `SheetSpec(states, scheme, frame_w, frame_h)` with `rows()` → ordered
  `RowSpec(index, state, direction, frames, key)` — state-major, the AUTHORED directions
  in `authored` order (H1; it was every direction in `order`); non-directional states
  contribute one row with `direction=None`.
- Default spec `CHAR8`: `idle` + `walk`, both directional, 8-way. States are
  CLI-overridable — the `--states` grammar is `name:frames[:fixed]`, comma-separated,
  where `:fixed` marks a non-directional state that contributes exactly one row.

## 3. Module boundaries — what is reusable for Eternia Studio

```
agent/charsheet/
├── __init__.py
├── spec.py         # §2 — pure data, no Pillow import needed at module load
├── revisions.py    # ImageRevisionStore — GENERIC, the Eternia-Studio-reusable piece
├── prompts.py      # turnaround / single-view / direction-aware row prompts
├── palette.py      # palette extraction + lock (quantize to approved palette)
├── pipeline.py     # turnaround → refs → rows → compose; imports agent.pet.generate.*
└── draft.py        # staged draft store + stage machine (charsheet-specific)
```

**`revisions.py` is the image-editing module the operator wants reusable.** Its API knows
nothing about characters: a store rooted at any directory, holding *items* keyed by
opaque strings, each item a list of *attempts* (image file + optional operator note +
timestamp) with at most one *approved* attempt. Verbs: `propose(key, image, note="")`,
`approve(key, attempt=-1)`, `reject(key, attempt)`, `current(key)`, `pending()`,
`history(key)`. Writes are atomic (tmp + `os.replace`); state is one JSON per item.
Charsheet uses keys `turnaround@n` / `row@walk-e`; Eternia Studio can mount the same
store over any image set with its own keys. No `agent_runtime` import, no charsheet
import, stdlib + Pillow only.

`pipeline.py` imports from upstream `agent.pet.generate.atlas`: `extract_strip_frames`,
`normalize_cells`, `mirror_frames`, `remove_background`, `atlas_to_webp_bytes`, plus two
private helpers (`_fit_to_cell`, `_clear_transparent_rgb`) — centralized in ONE import
block with a comment naming the drift risk (upstream rename breaks loudly at import;
tests cover). Generalized `compose_sheet(spec, cells_by_key)` and
`validate_sheet(spec, image)` are thin spec-driven loops written fresh in `pipeline.py`
(the upstream versions are welded to `ROW_SPECS` module globals; READ §0.4).

## 4. The staged QA flow (drafts, and what the launcher drives)

Storage (profile-scoped, plain-hermes compatible):

```
$HERMES_HOME/characters/
├── .drafts/<draft-id>/
│   ├── draft.json          # schema:1, slug, concept, style, stage, spec, base ref
│   ├── base.png            # the identity anchor image
│   ├── revisions/…         # ImageRevisionStore root (turnaround@<dir> and
│   │                       #   row@<state>-<dir> items — the leading `<kind>@` is the
│   │                       #   STORE's item-kind separator, not the sheet's)
│   └── strips/…            # raw provider strips (kept for deterministic re-extraction)
└── <slug>/
    ├── character.json      # manifest: §5 payload minus base64, plus spec
    └── sheet.webp
```

Stage machine in `draft.json`: `turnaround → rows → composed`. Verbs refuse
out-of-order calls with a stated reason (`{"ok": false, "error": …, "stage": …}`).

1. **Stage `turnaround` (the cardinal-directions gate, first as the operator asked).**
   One landscape strip: the character standing neutral in the 5 authored views, drawn in
   turnaround convention order front → back (`s, se, e, ne, n` left→right). Sliced with
   `extract_strip_frames(strip, 5, fit=False)`; each cutout is re-composited onto the
   magenta backdrop (A-2, §7.3) and proposed into the revision store as `turnaround@<dir>`.
   Per-direction re-roll = a single square-canvas generation of that one view, grounded on
   the base, operator note appended to the prompt. Approval of all 5 advances the stage.
2. **Stage `rows` (the animations, second).** Per directional state × authored direction:
   one strip grounded on that direction's APPROVED turnaround ref, direction-aware prompt
   language, geometry auto-retry kept from the pet flow (touching poses / multi-pose
   frames are rejected mechanically — no human should QA obviously-broken slices;
   `orchestrate.py:245-265` pattern READ). Strips land in the revision store as
   `row@<state>-<dir>`; re-roll with note per row. Non-directional states ground on the
   base. Mirrored directions are NOT generated and NOT QA items — and since H1 they are
   not composed either: the consumer flips an authored row at draw time.
3. **Stage `composed`.** Extract frames from every approved strip (H1: no mirror-derive
   step — the sheet is the authored rows), `normalize_cells` across ALL rows at once
   (one shared scale — the
   character must not change size as it turns), palette-lock every cell (§7.2), compose
   per spec, validate per spec, write `sheet.webp` + `character.json`.

## 5. CLI verbs + payload (the launcher contract)

Under the existing fork-owned `harness` parser (`harness.py:1438-1459` pattern READ), new
`characters` namespace, every verb with `--json`:

- `characters start --concept … [--slug …] [--style …] [--states …] [--directions 8|4]
  [--base-image <path>]` — no base image → generate base drafts (reuse
  `generate_base_drafts`, READ) and stop for selection; `characters base pick --draft
  <id> --index N`.
- `characters status --draft <id>` — full draft state incl. revision items + stage.
- `characters turnaround generate|reroll --direction ne [--note …]|approve
  [--direction …|--all]`.
- `characters rows generate [--only walk-e]|reroll --row walk-e [--note …]`.
- `characters compose --draft <id>` — installs; `characters list`.
- `characters sprite <slug> --json` — payload with the SAME field names/meanings as pets
  (`spritesheetBase64, spritesheetRevision, frameW, frameH, framesByRow, loopMs=1100,
  scale, mime`) **plus** `directions` (the scheme, incl. authored/mirrored), `states`,
  and `rows: [{row, state, direction, frames, key}]`; `stateRows` = flat row-key list so
  the Dart pattern of index-mapping still applies. Since H1 those three row-carrying
  fields describe the AUTHORED rows only — ten of them for CHAR8. No `framesPerState` cap semantics —
  characters are per-row only (§1 read-side cap). The FIELD LIST is hermes-specific
  packaging and belongs here; the row keys and the `stateRows` order inside it do not —
  those follow the launcher spec.

Image bytes for QA display: `status`/`reroll` responses carry file paths AND
base64 thumbnails (bounded, e.g. ≤256px) so the launcher renders without touching
hermes-home paths directly.

**As built (CS-5 corrections to this section).** The shipped verbs are flat, not
nested: `start`, `list`, `status`, `base` (sets/replaces the identity image —
added when CS-5 found a draft started without `--base-image` could never
advance), `turnaround`, `reroll-direction`, `approve-direction`, `rows`,
`reroll-row`, `compose`, `sprite`. Errors follow **pets parity** (flat
`{"ok": false, "error": …}`, exit 2 — not the Stage-42 envelope), because the
launcher already parses that shape for pets. Thumbnails are NOT in v1 responses;
payloads carry file paths (the launcher runs on the same machine as
`HERMES_HOME`) — thumbnails are a follow-up if the panel wants them.

## 6. Stages (CS-n), each with a gate

- **CS-0 Baseline + assumption kill.** Capture `harness pets sprite --json` byte-baseline
  on a synthetic pet fixture (the `test_harness_pets_cli.py:_write_pet` shape, READ);
  verify `scripts/run_tests.sh tests/hermes_cli/test_harness_pets_cli.py` actually runs on
  this Windows checkout (A-1); verify Pillow quantize-to-fixed-palette round-trip (A-3).
  *Gate:* baseline file recorded; one green test run; palette script output shown.
- **CS-1 `spec.py`.** Data model of §2 + `CHAR8` + parser for `--states`/`--directions`.
  *Gate:* import-clean; invariant script (row count, mirror closure — every mirrored
  source ∈ authored, key uniqueness) prints PASS.
- **CS-2 `revisions.py`.** Generic store of §3. *Gate:* script exercises
  propose/approve/reject/reload-from-disk; atomicity by crash-simulation (tmp file left
  behind ≠ corrupted state).
- **CS-3 `prompts.py`.** Turnaround prompt (5 views, magenta, registration language reused
  from `prompts.py:140-183` READ), single-view re-roll prompt with note injection,
  row prompt with per-direction view language (N = "seen from behind, back of head, no
  face"). *Gate:* script prints all prompts for CHAR8; every direction's language present;
  note passthrough shown.
- **CS-4 `pipeline.py` + `palette.py` + `draft.py`.** §4 flow end-to-end with a provider
  injection seam (a fake provider returning deterministic synthetic strips — arrow glyphs
  per direction — drives everything offline). *Gate:* fake-provider run produces a
  validated 16-row sheet + manifest; mirrored rows verified pixel-equal to
  `mirror_frames(source)` in-script. **(H1: the sheet is 10 rows and that pixel-equality
  gate is the one deleted — see "As built" below.)**
- **CS-5 CLI verbs + payload.** §5 wired in `harness.py` (fork-owned; pets code paths
  untouched). *Gate:* full flow driven headless through the CLI with the fake provider;
  `pets sprite --json` byte-identical to the CS-0 baseline.
- **CS-6 Proof pass (session-owner, not subagents).** Open the synthetic sheet, confirm
  row placement matches the manifest, confirm mirrored ≠ duplicated (arrows must point
  the other way — **H1: retired with the baked rows**); a real provider hatch if
  credentials are available this session.
  The launcher now ships its own synthetic in `tool/spatial/placeholder_character/`,
  but it is not a substitute: it emits a finished atlas + sidecar for the CONSUMER
  side, where the fake provider here drives the extractor, the geometry gate, the
  geometry gate and the palette lock — the pipeline's INPUT side. Keep both.
- **CS-7 Tests, written after the code works.** `tests/agent/test_charsheet_*.py` +
  CLI tests beside the pets ones; every test proven red by breaking the covered line,
  then green on restore, with the failure recorded in the PR/commit message. House rules
  apply: no change-detector tests, no source-reading tests (AGENTS.md READ).

## 7. Consistency mechanics (the operator's #1/#2 + the two nuances)

1. **Turnaround as identity anchor.** Cross-call identity drift becomes within-image
   consistency (models hold identity far better inside one generation); every animation
   row is grounded on a view the operator already approved.
2. **Palette lock.** `palette.py` builds a ≤48-color palette from the approved turnaround
   cutouts' opaque pixels, then quantizes every composed cell to it
   (`Image.quantize(palette=…, dither=NONE)` on RGB, alpha carried separately). Color
   drift across rows becomes structurally impossible. Cost accepted: novel accent colors
   snap to nearest base color.
3. **Magenta re-composite (A-2).** Sliced refs are transparent cutouts, but the row prompt
   tells the model to reuse "the same background as the attached reference"
   (`prompts.py:158-159` READ) — refs are therefore re-composited onto flat magenta
   before grounding, or that instruction silently loses its anchor.
4. **Direction-explicit prompt language** — free, and prevents faces on back views.

## 8. Subagent deployment map (the operator's build order)

Wave 1 (parallel, Opus): CS-1, CS-2, CS-3 — independent modules, no shared files.
Wave 2 (Opus): CS-4 — depends on all of wave 1. Wave 3 (Opus): CS-5 — depends on CS-4.
CS-0 runs before wave 1 (session-owner). CS-6 and the red-green audit of CS-7 are
session-owner work — subagent-written tests are only accepted with a reproduced
red-green log. Every wave's diff is reviewed against: fork boundary (no upstream file
edited — RAN `git diff --name-only upstream/main` must show only new files +
`harness.py` + this doc), packaging boundary (no `agent_runtime` import under `agent/`),
house test rules, and the §5 payload contract.

## 9. Deliberately left out, and why

- **The launcher QA panel (Flutter).** Separate repo; consumes §5 verbs; follow-up plan.
- **RPC methods.** The launcher speaks CLI for pets today (§0.2); adding RPC now is
  speculative surface (AGENTS.md footprint rubric READ).
- **Pixel-level editing.** v1 editing = note-driven re-roll; a paint surface is Eternia
  Studio's call later, over the same revision store.
- **Terminal rendering of character sheets.** No consumer; the pet renderer's inference
  trap (§0.4) makes accidental reuse harmful, so character sheets deliberately never
  enter pet read paths.
- **Migrating petdex onto `SheetSpec`.** Provable equivalence, zero shipped value, real
  regression risk on a byte-frozen contract.

## 10. Adversarial pass — what I most expect to be wrong

- **A-1 (env):** `run_tests.sh` may not run on this Windows checkout (no venv found; RAN).
  CS-0 settles it before any subagent writes a test.
- **A-2 (refs):** providers may key ref-image *content* but ignore its background; the
  magenta re-composite may matter less than §7.3 claims. Harmless if so.
- **A-3 (palette):** quantize-to-fixed-palette on RGBA needs the alpha split; a naive
  call flattens transparency. CS-0 proves the round-trip before CS-4 depends on it.
- **A-4 (turnaround quality):** the model may not deliver 5 clean distinct views in one
  strip (worst case: near-duplicate side views). Mitigation is the QA gate itself +
  per-direction re-roll; if rerolls dominate, switch authored views to 5 independent
  square generations grounded on base (same store shape, one flag in pipeline).
- **A-5 (global scale coupling):** `normalize_cells` computes one global K over every row
  (16 as written; 10 for CHAR8 since H1) —
  one degenerate row could shrink the whole character. The spec-driven validator keeps
  upstream's collapse guards per row; a failed row re-enters QA rather than shipping.
- **A-6 (private-helper drift):** upstream rename of `_fit_to_cell` /
  `_clear_transparent_rgb` breaks import at the next sync — loudly, in one place, covered
  by CS-7 tests. Accepted over copying (copies drift silently).
- **A-7 (concurrent draft mutation):** launcher + CLI could mutate one draft
  concurrently. v1 relies on atomic JSON replace + last-writer-wins per item;
  `agent_runtime.locks` is NOT available here (§0.3). If real contention appears, a
  charsheet-local lockfile is the follow-up, not a blocker.

## 11. Verification log (RAN this session)

`git ls-tree upstream/main agent/pet/ hermes_cli/harness.py` · `git diff --stat
upstream/main..HEAD -- agent/pet/ tui_gateway/server.py` · `grep pets
agent_runtime/serve_rpc.py` (zero hits) · launcher grep for transport + payload parse ·
`python -c "import PIL"` (12.1.1) · worktree rebase onto local main (`ca19df1e48`) ·
READ: atlas.py (full), constants.py (full), render.py (full), orchestrate.py (partial),
imagegen.py (full), prompts.py (full), store.py (head), harness.py (pets region),
petdex_client.dart (payload+transport regions), pet-sprite.tsx (grep), pyproject.toml
(packaging), AGENTS.md (full), locks.py (head), paths.py (head),
test_harness_pets_cli.py (head).

## 12. C5 conformance (2026-08-18)

The launcher's 8-way work (its C1–C4) made the consumer real, and it reads sheets through
`AvatarSpriteSheet._deriveDirectionSectors`. Hermes had drifted from it in three ways, all
now closed; the authority for every fact below is the launcher spec named in the header.

- **Row keys are hyphenated.** `row_key` emits `<state>-<direction>` (`walk-ne`), not
  `<state>@<direction>`. The deriver splits on the last hyphen, so `@`-keyed rows covered
  zero sectors and every 8-way sheet would have degraded to two-way at runtime — silently,
  with no error and no log.
- **Row order is front-first, authored-first.** `EIGHT_WAY.order` is
  `s se e ne n nw w sw` (authored `s se e ne n`) and `FOUR_WAY.order` is `s e n w`
  (authored `s e n`). Two consequences, both wanted: row 0 of `CHAR8` is `idle-s`, so the
  launcher's degenerate row-0 fallback reads front-facing; and the authored prefix is
  exactly the order `pipeline.turnaround_order` already produced out of the
  `prompts.VIEW_LANGUAGE` ring, so the prompt lane did not move. Row order is deliberately
  NOT the θ order of the direction tokens — the launcher addresses rows by NAME.
- **`-` is reserved in state names.** `_STATE_NAME_RE` is `[a-z][a-z0-9_]*` and
  `parse_states` refuses a hyphen by name. Closed by construction: with the launcher
  splitting on the LAST hyphen, allowing `spin-kick` would make row parsing depend on
  which tokens happen to be in the direction table on any given day.
- **`spec.DIRECTION_TOKENS`** is the eight-token vocabulary, exported from the package
  barrel, and `DirectionScheme.__post_init__` rejects any `order` entry outside it. The
  check runs LAST so every pre-existing validation keeps its own message.
- **Mirror baking stayed, as a tolerated divergence — RETIRED at H1** (the launcher's
  RAM-cap decision landed; see "As built" below). What C5 recorded, for the record: the
  launcher spec asks for authored rows only; hermes-composed sheets still baked the
  mirrored rows at compose time. The runtime was indifferent — every candidate chain
  tries the exact row before its mirror, and the deriver mirror-closes either way — so
  the only cost was decoded RAM (~+60% per sheet). Retiring it deletes CS-4's shipped
  pixel-equality gate; the right time to spend that is when the launcher's RAM-cap
  decision lands, not before.
- **Nothing to migrate.** `$HERMES_HOME/characters/` does not exist on the build box:
  no installed sheets, no drafts, no revision-store keys in the field. The rename is
  therefore a code change only.
- **Revision-store keys are unchanged.** They still read `row@walk-e` / `turnaround@n`:
  the leading `<kind>@` is the STORE's item-kind separator, the store never parses keys,
  and `_KEY_RE` already admitted the hyphen. Do not "fix" it to match the sheet.

### As built — H1: authored-only since `d870fbbd44` (2026-08-18)

The C5 tolerated mirror-baking divergence above is **retired**. Owner ruling, launcher
ADR 0024 (`Launcher_Brain/30 — Decisions/0024 — 8-way character sheets — serving lane,
memory budget, mirror baking.md`) decision 3 = B: hermes emits authored-only rows, one
format, and the CS-4 pixel-equality mirror gate is DELETED rather than moved. Option C
(a `--authored-only` compose flag, both shapes alive) was considered and ruled out —
there is no flag, no parameter and no legacy path.

- **`SheetSpec.rows()` is state-major over `scheme.authored`.** CHAR8 is **10 rows at
  1536x2080**, where it was 16 rows at 1536x3328; row 0 is still `idle-s`. Decoded cost
  12.19 MiB against 19.50 MiB baked — the +60% ADR 0024 priced, and the reason decision
  2-C's byte budget fits ~7 characters instead of ~4.
- **`compose_draft_frames` has no mirror step**, and `agent/charsheet/` no longer calls
  `mirror_frames` anywhere (it left the upstream import block with it). `normalize_cells`
  still runs over every row at once, so the one shared scale is unchanged in effect: a
  horizontal flip preserves every bounding box that scale is derived from.
- **`sprite_payload`** carries the authored rows only in `framesByRow` / `stateRows` /
  `rows`. `directions` (order/authored/mirrored) and `spritesheetRevision`
  (`mtime_ns:size`) are unchanged. The launcher reads its sector count off the row NAMES
  and mirror-closes the set (`AvatarSpriteSheet._deriveDirectionSectors`), so an
  authored-only sheet still resolves all eight sectors with no launcher change.
- **`authored_rows()` now delegates to `rows()`** (every composed row is authored; two
  separately filtered lists is how they drift apart), and **`mirrored_rows()` returns
  `(derived key, source row)`** — a derived direction has no row to hand back, and left
  as written it would have returned `[]` forever.
- **Nothing to migrate, again.** `$HERMES_HOME/characters/` is still empty on the build
  box. Sheets composed before H1 stay readable either way: the launcher tries the exact
  row before its mirror, and the backend sidecar validator checks geometry, not
  authoredness.
- **Where the old shape still appears in this plan**, it is the build log of the stages
  that produced it and is marked in place rather than rewritten: §6's CS-4 gate (the
  "validated 16-row sheet", whose pixel-equality check is the deleted gate), §6's CS-6
  mirror-derivation proof, and §10's A-5 global-K figure.
