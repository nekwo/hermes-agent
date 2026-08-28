# Skill-tombstone ledger union merge (was realm-skill-delete S4)

**Status: designed, NOT built. Gated on an operator ruling that upgrades R-D
from "LWW acceptable" to "union".** Extracted at the 2026-08-28 canon fold of
`planned/realm-skill-delete.md` (S1–S3 shipped as `45abf82803`, `32f41be19f`,
`dfc18b882f` and live in
[01-system-architecture.md §Skills](../01-system-architecture.md)).

## The gap this closes

`Realm.skill_tombstones` merges last-writer-wins on pull, like
`skill_selection`: two members publishing concurrently can silently drop a
tombstone entry, resurrecting a deleted skill's publishability. R-D ruled LWW
acceptable for v1 — the delete receipt's `next` guidance keeps the window
short — but nothing fences it.

## The design

**Touches:** `agent_runtime/realm_sync.py::_pulled_artifact_bytes`.

**Builds:** per-slug newest-timestamp-wins merge of `skill_tombstones`
(local ∪ incoming). A restore must be representable in a union world, so this
stage ALSO changes `restore_skill` to write a `restored_at` marker instead of
removing the entry — entries become a tiny per-slug state register, and
`SKILL_TOMBSTONE_LEDGER_CAP` prunes settled history. Apply the same union to
`deleted_workspace_ids` (plain set-union is safe there — unique ids, no
restore verb) in the same commit or file it as its own row.

**Tests:** merge matrix (local-only, incoming-only, both with differing
timestamps, restore-vs-stale-delete), plus the shipped S2 suite
(`tests/agent_runtime/test_realm_skill_tombstones.py`) unchanged.

**Do not build ahead of the ruling** — the v1 LWW posture is coherent on its
own and matches `skill_selection`.
