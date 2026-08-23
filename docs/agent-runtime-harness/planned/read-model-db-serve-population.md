# Planned — decide the fate of `read_model.db` on the serve path

**Status:** the lane is built, configured on, and never exercised in production.
**Domain:** runtime data and shapes. **Opened:** 2026-08-22.

## The finding

`agent_runtime/read_model.py` is complete: schema v3, WAL journal, a
`FrameSource` enum that names degradation, a `render_snapshot()` that returns
`None` rather than a lying `{}`. The live root `config.yaml` sets
`read_model.enabled: true` and `read_model.delta_patches: true`.

Neither `read_model.db` nor `snapshot.json` exists in the live store root
`X:\Eternia\.hermes\agent-runtime\` (verified 2026-08-22).

## Why

Both files are written by `write_snapshot()` (`snapshot.py:2311`) — the
`snapshot.json` boot cache unconditionally, and `ReadModel().apply_full_rebuild()`
behind the `read_model_enabled()` gate.

`write_snapshot()` has exactly **one** non-test caller in the repo:

```
agent_runtime/read_model.py:167:    built = write_snapshot(build_snapshot())
```

— inside `resolve_snapshot_frame()`, whose only non-test caller is
`_cmd_snapshot` in `hermes_cli/harness_parts/runtime_commands.py:473,489`. That
is the `hermes harness snapshot` CLI verb.

The serve path does not go through it, deliberately, and says so:

> It still writes no STORE state (`write_snapshot`, the `snapshot.json`
> boot-cache writer, remains uninvolved) — `hermes_cli/harness_parts/serve.py:972`

`hermes serve` builds cores via `build_snapshot()` and persists them through
`core_cache.write_back()` into `serve_read_model/`, which is a different store
with a different validity model. So on an operator machine that boots the
launcher and never runs `harness snapshot`, both the boot cache and the read
model stay absent regardless of what the config says.

## What is live instead

`serve_read_model/` holds a generation directory with the trio
`core.json` / `sidecar.json` / `entries.json` and a `live.json` pointer. Live
generation 2026-08-22 15:46: `gen-18ce384b65db9564-8c1b0e7f`, core 869,604 bytes,
entries 343,268 bytes, sidecar 582 bytes, `fingerprint_entries: 2461`.

The name collision is a hazard for anyone reading this domain cold:
`CORE_CACHE_DIRNAME = "serve_read_model"` (`core_cache.py:244`) is **not** the
read model. They are two independent caches of the same object with different
keys — a stat fingerprint versus a stored watermark — and only one is on the
serve path.

## The decision this plan exists to force

Three outcomes are coherent; the current state is none of them.

1. **Wire it.** Give the serve path a `write_snapshot()` call so
   `read_model.enabled: true` means what it says. Cost: a second cache of the
   same core, written on every build, with a second validity authority beside
   the fingerprint — the exact shape `core_cache.py` argues against for itself.
2. **Retire it.** Delete `read_model.py`, `projector.py`,
   `read_model_schema.sql`, the config block and the CLI verbs
   (`harness rebuild-read-model`, `harness read`), and let `serve_read_model/`
   own the name unambiguously. Precedent: S46 retired the incremental projector
   for having zero production callers.
3. **Demote it to an explicitly operator-only tool.** Keep it, but change the
   default so `enabled` does not read as a production claim, and document it as
   what it is — a CLI-invoked cache warmer and inspection surface.

Whichever is chosen, `read_model.enabled: true` in a live config that populates
nothing is an advertised-but-inert control, which this codebase has an explicit
rule against.

## The gate

- A ruling on which of the three outcomes applies.
- If (1): the second-authority drift argument answered in writing, and the
  equivalence between the two caches tested, not assumed.
- If (2): a grep-clean retirement including the two CLI verbs and
  `tests/agent_runtime/test_read_model*.py` — but NOT `ReadModelConfig`
  wholesale: its `delta_patches` field gates the LIVE S7-A patch-producer lane
  (`SHIPPED_DELTA_PATCHES`, resolved by `state_patches.delta_patches_enabled`;
  the launcher's base seed writes `read_model.delta_patches: true`, so the YAML
  key path is cross-repo wire). Retire only the dead-lane fields `enabled`,
  `serve_snapshot_from_db`, `db_filename`; the class, the `read_model:` YAML
  block, and `delta_patches` stay.
- If (3): the default flipped and `harness status` reporting the lane's actual
  reach.

## Note for whoever takes this

`snapshot.json`'s absence rides along with the same finding but is a separate
question — it is the launcher's documented boot cache, and its being absent on a
machine that has never run `harness snapshot` deserves its own check against the
launcher's boot path before anything is concluded.
