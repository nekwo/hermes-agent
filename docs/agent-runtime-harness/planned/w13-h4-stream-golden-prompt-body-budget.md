# w13/h4 — a projection budget for `prompt_layers[].content` in the patch/delta lane

**Status: R-1 RULED 2026-09-05 (evict with an accounting stub). Stages 1 and 2
are BUILT on the hermes side by w17/ha. Stage 3 and the launcher half of stage 2
(the mirror re-vendor) are NOT built — they are a launcher row. This file stays
in `planned/` until that row lands; §4 is the ledger.**

Filed 2026-09-04 for the mission-control queue row "A stream golden carries
another subsystem's churn — and the bulk is the prompt BODIES, not the
descriptor table the row blamed". The row had already been re-measured and
corrected once (w12/m2); this plan is what its corrected diagnosis asks for,
written rather than built because the cut it names changes a cross-repo wire
that a launcher screen reads.

## §0 What was re-measured, at this base

`tests/fixtures/stream_frames/delta_agent_create_narrow_profile.json`,
2026-09-04, hermes `9ea840bb90`:

| slice | bytes | share of the 56,627-byte frame |
|---|---|---|
| whole frame | 56,627 | 100% |
| `core.prompt_observability` | 33,143 | 58.5% |
| `…prompt_observability.chat_contexts` (2 entries) | 32,666 | 57.7% |
| each entry's `prompt_layers` (8 layers) | 13,056 | 23.1% each |
| each entry's `sum(len(layer.content))` | 10,125 | **17.9% each, 35.8% together** |
| next-largest core section (`persona_instances`) | 6,334 | 11.2% |

The w12/m2 numbers are confirmed with small drift (33,143 vs 32,639;
10,125 vs 10,285 — the table has kept growing). The row's own correction
stands: `prompt_layers[].content` is the bulk, and the descriptor fields it
was originally filed about (`name`/`summary`/`order`/`owner`/
`injection_location`) are ~12% of one layer table — fixing them alone buys
almost nothing.

## §0.1 The mechanism this wants ALREADY EXISTS one field over

`agent_runtime/prompt_observability.py::_evict_final_model_input` does exactly
the thing this row asks for, to the field beside this one:

* it replaces a heavy `final_model_input` with `_final_model_input_stub` —
  accounting plus "tiny address metadata only";
* it mutates the FRAME copy only ("the persisted row on disk is untouched");
* the body is fetched on demand, and the stub names the verb.

The same file already carries two more instances of the pattern
(`_hoist_skills_catalogs` + `available_skills_ref`/`accessible_skills_ref`, and
`chat_contexts_ref`'s eviction accounting with its
`harness prompt-context show --context-id <id> --json` fetch verb). So this is
not a new idea to design; it is the fourth application of a settled one, to the
field that is now the largest.

## §0.2 Why it is not a drive-by

The launcher reads `prompt_layers[].content`. One site:
`EterniaLauncher/lib/features/mission_control/agent_chat/initial_chat_context_dialog.dart`
(`final captured = layer.content?.trim();`), fed through
`MissionPromptLayer.fromJson` in
`lib/features/mission_control/data/mission_control_snapshot.dart`.

Two facts make this tractable but still a ruling:

* `MissionPromptLayer.content` is ALREADY nullable (`this.content`), and the
  class carries a separate `preview` field — so an evicted body degrades to a
  null the model already expects rather than to a crash;
* but a screen that renders the captured body would go blank, and the launcher
  has no fetch path wired for it today.

The row's other half is confirmed and unchanged: the launcher's own
`mission_stream_contract_fixture_test.dart` reads the mirrored golden for the
FOLD only and touches no `prompt_observability` field, so the launcher pays a
cross-repo byte regeneration for a subsystem that test does not read.

## §1 The ruling this was blocked on

**R-1 — does `prompt_layers[].content` leave the frame? RULED 2026-09-05
(operator): answer 1, evict with an accounting stub.**

Three answers, in ascending cost:

1. **Evict, with a stub** (this plan's recommendation). `content` is replaced
   by `{"evicted": true, "chars": n, "sha256": "…"}` and the row's existing
   `chat_contexts_ref.fetch` verb serves the body. Buys ~36% of this frame and
   the same share of every live delta. Costs one launcher screen a fetch.
2. **Truncate to a budget** (e.g. first 512 chars + `truncated: true` +
   `chars`). Buys most of the bytes, keeps the screen readable with no fetch
   path, and is a lie of a different kind — the launcher would render a body
   that is not the body.
3. **Leave it.** The frame stays 57% one subsystem, and the number keeps
   growing (it has, twice, between two measurements of this same row).

A fourth option the row names — "a fixture persona whose summary resolves no
prompt context" — is NOT recommended and is recorded here so it is not
re-proposed: it shrinks the GOLDEN and changes nothing about the live wire, and
a golden that no longer carries the thing the producer really emits stops being
a contract. The bytes are a symptom; the projection is the defect.

**R-2 — who regenerates the launcher mirror?** The fixture README's update rule
is "fixtures change only in a cross-stack change that lands both repos
together", so whichever wave takes stage 2 takes the launcher half with it.

## §2 Stages

### Stage 1 — the eviction, red-first (hermes) — **BUILT 2026-09-05, w17/ha**

* **Files:** `agent_runtime/prompt_observability.py`
  (new `_evict_prompt_layer_content`, called from
  `snapshot_prompt_observability` beside the existing
  `_evict_final_model_input`).
* **Red-first test:** `tests/agent_runtime/test_prompt_observability.py` — a
  projection built from a context whose layers carry known bodies asserts each
  frame layer has no `content`, carries `chars` equal to the original length,
  and that the PERSISTED row on disk still has the body. Red before the
  function exists.
* **Must not change:** the persisted context files; `preview`; any descriptor
  field; `final_model_input`'s own stub; the `*_ref` hashes.
* **Check:** `scripts/run_tests.sh tests/agent_runtime/test_prompt_observability.py`.

**As built, and the two deviations, both deliberate:**

1. **The stub is a SIBLING key, `content_ref` — not a re-typed `content`.** The
   plan's §1 answer 1 wrote "`content` is replaced by `{evicted, chars,
   sha256}`". Building it that way would have been silent data loss: the
   launcher's `MissionPromptLayer.fromJson` reads `content` through
   `_nullableString`, so a map parked under that name decodes to `null` and the
   accounting never reaches the screen. `content` therefore LEAVES the layer
   (degrading to exactly the null `this.content` already declares) and the stub
   rides beside it as `content_ref` — the same `*_ref` convention
   `available_skills_ref` / `chat_contexts_ref` / `skills_catalogs_ref` already
   use in this file. `chars` and `token_estimate` stay on the layer untouched,
   so the launcher's token attribution never loses its number.
2. **The tests live in `tests/agent_runtime/test_snapshot_prompt_hoist.py`,** not
   `test_prompt_observability.py` as the plan wrote. That is where the three
   sibling applications of this pattern are already tested
   (`_hoist_skills_catalogs`, `_evict_final_model_input`, and the durability
   read-back), and the fourth belongs with them.

Four tests, all red before the function existed (`AttributeError: module
'agent_runtime.prompt_observability' has no attribute
'_evict_prompt_layer_content'`): the stub's shape and the untouched descriptors;
idempotence plus a present-but-empty body as a real `0`; the snapshot
integration proving the persisted row on disk keeps the exact body; and a
measured shrink assertion (evicted rows are less than half the un-evicted
bytes). `PROMPT_LAYER_CONTENT_FETCH` is the SAME verb `chat_contexts_ref`
already advertises — eviction adds a pointer into an existing fetch lane, not a
new lane.

### Stage 2 — the golden, and the launcher mirror (both repos, one wave) — **hermes half BUILT 2026-09-05, w17/ha; launcher mirror OWED**

* **Files:** regenerate `tests/fixtures/stream_frames/*` via
  `scripts/generate_agent_runtime_stream_fixtures.py`, refresh
  `MANIFEST.sha256`, and copy byte-identically into the launcher's
  `test/fixtures/harness_stream/`.
* **Check:** `scripts/run_tests.sh tests/agent_runtime/test_stream_contract_fixture.py`
  green in hermes; `mission_stream_contract_fixture_test.dart` green in the
  launcher. Record the new frame size beside the 56,627 above.
* **Must not change:** which frames exist (`GENERATED_FRAME_FILES`), the
  promotion decision the two agent-create goldens exist to pin, or any
  non-`prompt_observability` core section's bytes.

**What actually changed, and the one thing that was already broken.** Exactly
one golden moved — `delta_agent_create_narrow_profile.json`, the only frame that
carries a full core with `prompt_observability.chat_contexts` — plus its
`MANIFEST.sha256` row. A structural diff of the old and new frames reports nine
differences: the eight expected `content` → `content_ref` swaps (two layers ×
two chat contexts, removed and added), and ONE that is not mine:
`core/agents[0]/skill_hash_absent` was ADDED.

That key is pre-existing drift, not this change. `2fde2a0c56` (2026-09-04,
"the persona and status rows project `skill_hash_absent`") added the field to
`snapshot.py:2568`, and the golden was last regenerated 2026-09-03
(`36a9be9b32`). Proven rather than assumed: with `prompt_observability.py`
reverted to `HEAD` and only the generator run, this same golden and manifest
still come back modified. So the launcher's `hermes-cli-contract` CI job — which
checks out hermes, runs this generator and byte-compares — was ALREADY red before
w17/ha touched anything, and the mirror re-vendor was already owed. This
regeneration carries that fix along with the eviction.

### Stage 3 — the launcher's read (launcher) — **NOT BUILT; handed back as a launcher row**

* **Files:**
  `lib/features/mission_control/agent_chat/initial_chat_context_dialog.dart`
  — when `layer.content` is absent and the layer says `evicted`, render the
  fetch affordance rather than the "exact text unavailable" notice.
* **Check:** the touched suite plus `flutter analyze` on the touched paths.
* **Must not change:** `MissionPromptLayer`'s constructor arity (`content` is
  already optional).

**Correction to this stage as written: there is no `preview` to render.** The
plan said "render the `preview` plus a fetch affordance". The two layers that
carry a body — `runtime_identity` and `operator_channel_rules` — carry no
`preview` at all; `preview` is emitted only on the `surface` layer, which has no
`content` and is not evicted. Adding a preview to the evicted layers would put
~4.8 KB back into this golden and would BE answer 2 (truncate), which the ruling
declined. So the launcher renders the summary it already has plus the eviction
receipt, and does not invent a body prefix.

The exact launcher work, in one place:

* `MissionPromptLayer` gains a parsed `contentRef` off `json['content_ref']`
  (`{evicted: bool, chars: int, sha256: String, fetch: String}`). `content` and
  its existing nullable parse stay — a persisted row fetched on demand still
  carries the real body, and old frames still decode.
* `_promptLayerDocument`'s fallback (the branch after
  `final captured = layer.content?.trim();`) must distinguish two states it
  currently collapses into one. Today an absent body always yields the subtitle
  "Layer metadata · exact text unavailable in this captured turn" — which after
  this change is FALSE for an evicted layer: the text is available, it is on
  disk, and the frame says exactly where. When `layer.contentRef?.evicted == true`,
  render the summary plus the receipt (`chars`, the short `sha256`, and the
  verb `harness prompt-context show --context-id <id> --json` with the row's
  own `context_id` substituted) and say the body was evicted from the frame, not
  that it was never captured. The "never captured" wording stays for layers with
  neither `content` nor `content_ref`.
* Re-vendor `test/fixtures/harness_stream/delta_agent_create_narrow_profile.json`
  and `MANIFEST.sha256` byte-identically from hermes (this is stage 2's launcher
  half, and it is separately owed for the `skill_hash_absent` drift above).
* Red-first: a widget/unit test on `_promptLayerDocument` for an evicted layer,
  and `mission_stream_contract_fixture_test.dart` green on the re-vendored bytes.

## §3 What this plan deliberately does not do

Nothing in stages 1–3 touches the descriptor fields the row was ORIGINALLY
filed about. They are 12% of one layer table and the row's own re-measurement
already ruled that cut not worth its churn.

## §4 Ledger — the frame, re-measured after the cut

`tests/fixtures/stream_frames/delta_agent_create_narrow_profile.json`, hermes
w17/ha, 2026-09-05. Measured by slicing the RAW committed bytes with a brace
scan, so every number is what the golden costs on disk — no re-serialisation
drift. That is also why the "before" column differs slightly from §0's
33,143/32,666: §0 re-encoded the decoded value (`ensure_ascii=True` gives
32,639 for the same bytes), while this table reads the file.

| slice | before | after | delta |
|---|---|---|---|
| whole frame | 56,627 | **36,990** | **−19,637 (−34.7%)** |
| `core.prompt_observability` | 32,459 (57.3%) | **12,799 (34.6%)** | −19,660 |
| `…chat_contexts` (2 entries) | 32,016 (56.5%) | 12,356 (33.4%) | −19,660 |
| each entry's `prompt_layers` (8 layers) | 12,937 (22.8%) | 3,107 (8.4%) | −9,830 each |
| each entry's `sum(len(layer.content))` | 10,094 | 0 | −10,094 each |
| next-largest core section (`persona_instances`) | 5,870 | 5,870 | unchanged |

Two layers per entry carried a body (`runtime_identity`,
`operator_channel_rules`); both now carry `content_ref` instead. Every
non-`prompt_observability` core section is byte-unchanged except
`core.agents[0]`, which gained the pre-existing `skill_hash_absent` key
documented under stage 2 — drift from `2fde2a0c56`, not from this cut.

`prompt_observability` is no longer the majority of the frame: it went from
57.3% to 34.6%, and the frame is now a third smaller for every subscriber that
takes a full-core demote. The same share comes off every live delta carrying
this projection, which was always the point — the golden is the instrument, not
the target.

**Still open after this wave:** stage 3 and stage 2's launcher mirror, both in
the launcher repo, both above. Until the mirror is re-vendored the launcher's
`hermes-cli-contract` byte-compare is red — it already was, for the
`skill_hash_absent` reason, so this does not make a green job red; it adds a
second reason to the same red.
