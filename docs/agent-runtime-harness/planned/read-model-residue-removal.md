# Planned — remove the last traces of the retired read-model lane

**Status:** NOT STARTED. The lane's code went at Stage 6 (`fac754194e`,
2026-08-22); this file tracks the pieces that could not go in the same cut, so
they are not forgotten.
**Domain:** runtime data and shapes.
**Owning doc:** [`../02-runtime-data-and-shapes.md`](../02-runtime-data-and-shapes.md)
§ "The read model — RETIRED".

## The remainder, in order

1. **The three dead config fields on the snapshot wire.**
   `ReadModelConfig.enabled` / `.serve_snapshot_from_db` / `.db_filename`
   (`agent_runtime/runtime_config.py`) have no reader anywhere, but
   `migrations.effective_config_summary` does `asdict(cfg)` into
   `core.runtime_config`, so all three are bytes in every snapshot — present in
   six byte-pinned goldens under `tests/fixtures/stream_frames/` AND the
   launcher's byte-identical mirrors under `test/fixtures/harness_stream/`.
   Removing them is a `SNAPSHOT_CONTRACT_VERSION` bump plus producer-side
   golden regeneration and the launcher manifest lockstep (S57 precedent:
   contract 47→48 dropped 29 reader-less scalars the same way).
   **Do not pay a bump just for this — ride the NEXT bump taken for any
   reason.** Whoever bumps the contract next deletes the three fields in the
   same change and checks this row off.
2. **The operator's live config line.** `X:\Eternia\.hermes\config.yaml` still
   carries `read_model.enabled: true` — parsed-and-ignored, but the codebase
   has a rule against advertised-but-inert controls. Operator-owned one-line
   delete; agents do not edit that file. `read_model.delta_patches` on the
   line below it is LIVE and stays.
3. **Shrink the doc section.** After (1) lands, doc 02's RETIRED section
   collapses to one sentence — "`serve_read_model/` is the core cache, ignore
   the name" — plus the `delta_patches` and legacy-file-exclusion notes. The
   naming-trap warning is permanent (renaming the live cache dir was CANCELLED
   deliberately: it costs a demote-priced rebuild and there is nothing left to
   collide with).

## Explicitly not part of this

- `read_model.delta_patches` — live, gates the S7-A patch producer,
  cross-repo wire (the launcher base seed writes it). Never remove.
- `serve_read_model/` dirname — keep (Stage 7 cancellation, argued in the
  Stage 6 docket).
- `paths.snapshot_path()` and `core_cache._EXCLUDED_STORE_ENTRIES`' trio —
  keep while any pre-cut store can exist; they only name legacy artifacts.

## Gate

Row 1 checked off inside whatever change next bumps the contract; row 2 by
the operator; row 3 in the same change set as row 1. File deleted when all
three are done.
