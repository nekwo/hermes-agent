# Planned — forward-only read-model schema migrations (archived RD6)

**Status:** not implemented. **Domain:** runtime data and shapes.
**Opened:** 2026-08-22.

## Verification

`agent_runtime/read_model_migrations.py` does not exist. `ReadModelSchemaTooNew`
returns zero hits across the repo. `READ_MODEL_SCHEMA_VERSION = 3`
(`agent_runtime/read_model.py:26`).

## What happens today instead

`ReadModel._ensure_schema` (`read_model.py:325`) runs on every `connect()` and
handles version change by **destruction**, not migration:

- an unconditional `DROP TABLE IF EXISTS goals / stage_verification / runs /
  proofs / incidents` (`read_model.py:330-338`) — the mission-lane tables,
  removed 2026-07-30, dropped on every connect so a pre-removal database
  reclaims their pages;
- a targeted `DROP TABLE agent_instances` when the table still carries a
  `task_id` column (`read_model.py:328`);
- `_reset_for_schema_change(conn, stored_version)` (`read_model.py:357`)
  whenever the stored version differs from `READ_MODEL_SCHEMA_VERSION`
  (`read_model.py:341`) — an inequality test, not a direction test. It deletes
  every row table, deletes `projections_misc`, drops the watermark so the next
  `render_snapshot` reports a miss, and `VACUUM`s.

The v2→v3 comment states the intent plainly (`read_model.py:19-25`): version 3
removed per-section duplicate rows, so "bumping the version makes `_ensure_schema`
clear those DBs and reclaim the pages, rather than leaving stale duplicates to be
read."

**Clearing is a defensible choice for this store** — the read model holds no
authority, only a projection, and `render_snapshot()` returns `None` for a
cleared database, which routes callers to a rebuild rather than to a lie. So
this is not a data-loss bug. It is a missing capability: there is no way to
evolve the schema *without* discarding the cache, and no fail-closed behaviour
when older code opens a newer database.

## The two gaps

1. **No forward migration path.** Every version bump costs a full rebuild for
   every install. Acceptable while the projection is cheap to rebuild; not
   acceptable if the read model ever holds something a rebuild cannot
   reconstruct.
2. **No fail-closed on a newer schema.** Older code opening a v4 database today
   sees `stored_version != READ_MODEL_SCHEMA_VERSION` and **clears it** —
   silently destroying a newer process's cache rather than refusing. On a
   single-process install this is invisible. On a mixed-version install (an
   operator running an older CLI beside a newer serve) it is a mutual-clobber
   loop with no receipt.

Gap 2 is the one worth acting on first, and it is cheap: a version comparison
that raises instead of resetting when `stored_version > READ_MODEL_SCHEMA_VERSION`.

## What the archived plan specified

`meta.schema_version` plus `read_model_migrations.py` — ordered, forward-only,
each `def migrate_v<N>_to_v<N+1>(conn)` in one transaction. Opening a newer DB
with older code raises a typed `ReadModelSchemaTooNew` (fail closed). Golden
per-schema fixture databases; CI migrates oldest→head and byte-diffs the
rendered envelope.

## The gate to open this

- **Gap 2 alone** ships as soon as someone rules on the mixed-version case: a
  typed refusal plus a receipt naming both versions. No migration framework
  needed.
- **Gap 1** should not be built until the read model holds state a rebuild
  cannot reconstruct. Building an ordered migration framework for a pure cache
  is the "advertised but inert" shape this codebase has a rule against — and it
  is a prerequisite the archived RD8 stage named for promoting the lane to the
  production envelope, which is itself gated on
  [read-model-certification-gates.md](read-model-certification-gates.md).
- Note the dependency: whether *any* of this is worth building depends on the
  ruling in [read-model-db-serve-population.md](read-model-db-serve-population.md).
  If the read model is retired, both gaps close by deletion.
