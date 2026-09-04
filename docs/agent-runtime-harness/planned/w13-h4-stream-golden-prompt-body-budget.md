# w13/h4 — a projection budget for `prompt_layers[].content` in the patch/delta lane

**Status: PLAN ONLY. Nothing here is implemented. Stage 1 is blocked on an
operator ruling (R-1 below).**

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

## §1 The ruling this is blocked on

**R-1 — does `prompt_layers[].content` leave the frame?**

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

## §2 Stages (each blocked until R-1 is answered)

### Stage 1 — the eviction, red-first (hermes)

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

### Stage 2 — the golden, and the launcher mirror (both repos, one wave)

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

### Stage 3 — the launcher's read (launcher)

* **Files:**
  `lib/features/mission_control/agent_chat/initial_chat_context_dialog.dart`
  — when `layer.content` is absent and the layer says `evicted`, render the
  `preview` plus a fetch affordance rather than a blank.
* **Check:** the touched suite plus `flutter analyze` on the touched paths.
* **Must not change:** `MissionPromptLayer`'s constructor arity (`content` is
  already optional).

## §3 What this plan deliberately does not do

Nothing in stages 1–3 touches the descriptor fields the row was ORIGINALLY
filed about. They are 12% of one layer table and the row's own re-measurement
already ruled that cut not worth its churn.
