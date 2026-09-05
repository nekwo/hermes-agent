# w17/ha — field notes, 2026-09-05

Lane ha of wave 17, hermes worktree `w17/ha`. Two rows: the stream golden's
prompt-body cut (ruled today), and peer discovery's `reached_at` (ruled today,
hermes half only). Running record; the report is written from this file.

---

## Row 1 — the stream golden's prompt-body cut

Plan: `w13-h4-stream-golden-prompt-body-budget.md`. R-1 ruled 2026-09-05:
evict with an accounting stub.

### What I did

**Stage 1 (built).** `_prompt_layer_content_stub` + `_evict_prompt_layer_content`
in `agent_runtime/prompt_observability.py`, called from
`snapshot_prompt_observability` on the line after `_evict_final_model_input`.
Four tests written first in `tests/agent_runtime/test_snapshot_prompt_hoist.py`
(the file that already tests the three sibling applications of this pattern),
all four red with `AttributeError: … has no attribute
'_evict_prompt_layer_content'` before the function existed.

**Stage 2, hermes half (built).** Regenerated the stream goldens through
`scripts/generate_agent_runtime_stream_fixtures.py`. Exactly one frame moved,
`delta_agent_create_narrow_profile.json`, plus its `MANIFEST.sha256` row.

**Stage 3 + stage 2's launcher mirror: NOT built.** Launcher repo; handed back
as one launcher row, written out verbatim in the plan's stage 3.

### What I measured

Raw-byte brace-scan slices of the committed golden (not a re-serialisation):

| slice | before | after |
|---|---|---|
| whole frame | 56,627 | **36,990** (−34.7%) |
| `core.prompt_observability` | 32,459 (57.3%) | **12,799 (34.6%)** |
| `…chat_contexts` (2 entries) | 32,016 | 12,356 |
| each entry's `prompt_layers` | 12,937 | 3,107 |
| each entry's `sum(len(layer.content))` | 10,094 | 0 |

§0 of the plan reported 33,143 / 32,666 for the first two. That is the same
bytes measured a different way — re-encoding the decoded value with
`ensure_ascii=True` gives 32,639, with `False` 32,459. The file itself is
32,459. Recorded so the next reader does not chase a phantom regression.

### Two things I changed against the plan's letter, both deliberate

1. **`content_ref`, not a re-typed `content`.** The plan wrote "`content` is
   replaced by `{evicted, chars, sha256}`". Doing that literally would have been
   silent data loss: the launcher parses `content` with `_nullableString`, so a
   map there decodes to `null` and the accounting never lands. `content` leaves
   the layer instead (degrading to the null `MissionPromptLayer.content` already
   declares) and the stub rides beside it under this file's existing `*_ref`
   convention.
2. **Tests in `test_snapshot_prompt_hoist.py`,** not `test_prompt_observability.py`
   as the plan named — that is where `_evict_final_model_input`'s own tests live.

Also corrected in the plan: stage 3 said "render the `preview` plus a fetch
affordance". There IS no preview on the two evicted layers — `preview` is
emitted only on the `surface` layer, which carries no `content`. Adding one
would put ~4.8 KB back and would be answer 2 (truncate), which the ruling
declined.

### The thing I did not expect

The structural diff of old vs new golden shows nine changes: eight expected
`content` → `content_ref` swaps, and `core/agents[0]/skill_hash_absent` ADDED.
That one is **not mine**. `2fde2a0c56` (2026-09-04) added the field to
`snapshot.py:2568`; the golden was last regenerated 2026-09-03 (`36a9be9b32`).
Proven, not reasoned: with `prompt_observability.py` checked back out to `HEAD`
and only the generator run, this same golden and manifest still come back
modified.

So the launcher's `hermes-cli-contract` job (checks out hermes, runs the
generator, byte-compares the mirror) was **already red on main before this
lane**, and the mirror re-vendor was already owed. This wave does not turn a
green job red; it adds a second reason to a red one. Filed in the plan under
stage 2.

### Doc cites I had to re-anchor

Inserting 67 lines into `prompt_observability.py` drifted three checked cites in
`07-observability.md` (the gate caught all three). Re-anchored to the real
current lines rather than blind-shifted. While in that paragraph I also fixed
two bare `:N` continuations that were **already** stale on `HEAD` — `:1185-1187`
for the retention constant pointed at `_store_skills_catalog`'s docstring, and
`:1217-1220` for "honest absence" pointed at a comment block. They are now
`PROMPT_OBSERVABILITY_RETAIN_PER_LANE` `:1287-1289` and `:1319-1321`. The gate
does not check bare continuations, which is how they rotted unseen.

`07-observability.md` also gained a short paragraph recording the shipped
eviction (hermes half is truth now, so it belongs in the domain doc, not only
in `planned/`).

### Left

Stage 3 and stage 2's launcher mirror. Both in the launcher row.

---
