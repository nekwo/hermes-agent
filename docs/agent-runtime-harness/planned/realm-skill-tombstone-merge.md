# Skill-tombstone ledger union merge (was realm-skill-delete S4)

**Status: IN BUILD (W2-H5, decision-close wave 2026-08-31).** The gate is open:
**R-D UPGRADED TO UNION 2026-08-31** by RD-11 of the launcher repo's
`docs/mission_control/planned/decision-close-wave-2026-08-31.md`, which rules
this document buildable whole — union merge, `restored_at` markers, and the
`deleted_workspace_ids` set-union in the same commit. Extracted at the
2026-08-28 canon fold of
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
own and matches `skill_selection`. (RD-11 gave the ruling; this line is kept as
the record of what the gate was.)

## Corrections found while building (W2-H5, 2026-08-31)

Two of this document's own statements did not survive re-measurement:

- **"plus the shipped S2 suite … unchanged" is not achievable, and the file it
  names is S1.** `tests/agent_runtime/test_realm_skill_tombstones.py` is the S1
  store-chokepoint suite (S2 is `test_realm_sync_skill_inbox.py`), and three of
  its assertions pin the exact mechanism `restored_at` replaces: two read
  `skill_tombstones` for "is it lifted", and one is an exact-dict pin on the
  serialized record. They are amended to the register's semantics, not
  weakened. Two more elsewhere pinned the same mechanism
  (`test_skills_delete_verbs.py`'s `_ledger` helper, and an inbox case that used
  `restore_skill` as a way to blank the local ledger before a pull).
- **"`deleted_workspace_ids` … no restore verb" is false.**
  `default_scope.ensure_default_scope` and the fixed-id reconcile path both LIFT
  an id off that ledger. Both act on the local default scope, which refuses a
  server binding, so the union was still built as ruled — but what it changes
  for those two sites is recorded at
  `realm_sync.merge_deleted_workspace_ledgers`, and the orchestrator owns
  whether that wants a row.
