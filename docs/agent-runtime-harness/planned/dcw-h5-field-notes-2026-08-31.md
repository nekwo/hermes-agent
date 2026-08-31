# W2-H5 field notes — skill-tombstone union merge (hermes)

Running record for the decision-close wave's W2-H5 stage: build
`planned/realm-skill-tombstone-merge.md` whole, R-D upgraded LWW → UNION by
RD-11. Worktree `X:/Eternia/_worktrees/dcw-h5`, branch
`feat/dcw-h5-tombstone-union`, based on `301946bc57`.

## 1. Premise re-measurement, before any code

The plan was written 2026-08-28; the realm-sync drift lane landed since. Read
`agent_runtime/realm_sync.py` as it IS. Four premises checked:

| premise | verdict |
|---|---|
| `skill_tombstones` is adopted LWW on pull | **HOLDS** — `_pulled_artifact_bytes` writes the incoming realm JSON wholesale except for a fixed authority-field set; the ledger is not in that set. |
| the merge belongs at `_pulled_artifact_bytes` | **HOLDS**, with a wrinkle the plan does not mention (§2). |
| the shipped suite stays unchanged | **FAILS** (§3). |
| `deleted_workspace_ids` has "no restore verb" | **FAILS** (§4). |

## 2. The wrinkle: the function did not run for local realms

`_pulled_artifact_bytes` opened with

    if artifact.kind != "realm" or not realm.server_id or not artifact.destination.exists():

— i.e. it returned early for a realm with no `server_id`, and
`test_realm_sync_skill_inbox.py`'s S2 section header documented that as a
deliberate asymmetry ("one fewer guard, same LWW posture"). The asymmetry is
correct for AUTHORITY fields, which only a backend binding can own. It is wrong
for a resurrection guard: a local-only realm with a sync repo drops a tombstone
in exactly the same way, and losing one is not a question about identity. So
the authority half stays gated on `server_id` and the ledger half is
unconditional. That is claim `s4-the-ledger-union-is-not-gated-on-server-id`,
whose mutation is precisely the restored gate — it kills.

Second-order consequence, handled: running the function for every realm means
re-serializing records that previously passed through byte-for-byte, and the
pull's change detection is a byte compare. `_pulled_artifact_bytes` therefore
returns the SOURCE BYTES when neither reconciliation changed anything
(`merged == incoming`), which is strictly safer than the old behavior (which
re-dumped a server-bound realm's JSON even when every authority field matched).
Pinned by `test_a_pull_that_reconciles_nothing_leaves_the_record_byte_identical`.

## 3. "the shipped S2 suite unchanged" — not achievable, and misnamed

The plan says to keep `tests/agent_runtime/test_realm_skill_tombstones.py`
unchanged and calls it the S2 suite. Two problems:

- it is the **S1** suite by its own docstring (store chokepoints); S2 is
  `test_realm_sync_skill_inbox.py`;
- three of its assertions pin the exact mechanism `restored_at` replaces:
  `assert stored.skill_tombstones == []` after a restore, the slug-list after a
  lift, and an exact-dict pin on the serialized record (which gains
  `"restored_at": None`).

Amended to the register's semantics with the ruling named at each site. The
distinction the amendments preserve: **the ACTIVE ledger is what "no tombstone"
means to an operator**, and the register is bookkeeping the merge needs. Two
more sites elsewhere pinned the same mechanism and were amended the same way:

- `tests/hermes_cli/test_skills_delete_verbs.py`'s `_ledger` helper — re-keyed
  to the active ledger, which is what all ten of its call sites are asserting
  about;
- `test_pull_adopts_an_arriving_ledger_on_a_local_realm` used `restore_skill`
  as a way to BLANK the local ledger before a pull. Under RD-11 that leaves a
  restore marker which correctly beats the arriving delete — the test's
  simulation mechanism became a different test. Blanked on the record instead,
  and the case it accidentally became now exists deliberately as
  `test_a_pull_does_not_undo_this_members_restore_with_a_stale_delete`.

Receipt shape is UNCHANGED: `skill_tombstone_rows` filters to active entries,
so the status envelope, the sidecar and `realm skills show` keep their three
keys and a restored entry never appears as a "deleted from realm" row with a
restore affordance for something that is not blocked. No launcher consumer
exists (`grep skill_tombstones lib/ test/` in EterniaLauncher: zero hits), so
nothing downstream had to move.

## 4. "no restore verb" for `deleted_workspace_ids` is false

Two sites LIFT an id off that ledger:

- `agent_runtime/default_scope.py:133-139` (`ensure_default_scope`)
- `agent_runtime/default_scope.py:528-534` (the fixed-id reconcile winner)

Measured effect of the union on them: **nothing on the inbound pull** — LWW
already re-added a lifted id that a peer still carried, so that outcome is
unchanged — but the lift can no longer PROPAGATE. Under LWW it travelled on the
next publish; under a union it stays local until the id ages out of
`DELETED_WORKSPACE_LEDGER_CAP`. Both sites act on the local default scope,
which refuses a server binding, so this is not the shared-realm lane. Built as
RD-11 ruled, with the boundary written at
`realm_sync.merge_deleted_workspace_ledgers` (a future reader of that function
is who needs it) and reported for the orchestrator to row if wanted.

## 5. Rules chosen where the plan left the design open

- **Merge key = the exact slug**, not the bare-name-covers-categorized match
  rule. That rule is a MATCH rule (`store.skill_tombstoned`); applying it as a
  merge key would silently fold `doomed` and `category/doomed` — two distinct
  entries with distinct lifecycles — into one.
- **Rank by the LATER of `deleted_at`/`restored_at`**, not by `deleted_at`. This
  is what makes restore-vs-stale-delete and re-delete-vs-stale-restore ONE
  comparison. It also forces the store's re-delete to REPLACE the entry (it
  already did) so a stale `restored_at` can never ride a fresh delete.
- **Ties go to the DELETE.** Asymmetric on purpose: a restore that loses a
  microsecond tie is one explicit verb away from being re-run; a block that
  loses one is a resurrection, which is the whole reason the ledger exists.
- **Unreadable stamps lose; slug-less rows are dropped.** These are a peer's
  bytes. An entry whose stamps will not parse costs that entry its rank and
  nothing more; a row with no usable slug blocks nothing (`skill_tombstoned`
  reads `entry.slug`) and carrying it forward only risks breaking the next
  realm load.
- **The cap prunes settled history first.** `restored_at` entries linger by
  design, so a plain `[-cap:]` would let inert history push a live block off the
  front. `store.prune_settled_ledger` is shape-agnostic so the store's records
  and the merge's raw rows share ONE rule.

Both cap tests were written non-discriminating on the first pass (the settled
entry happened to be the oldest, so a plain tail-bound gave the same answer) and
the mutation SURVIVED. Caught by the hand-proof, not by reading. Fixed by
placing the settled entry between two live blocks. This is the second time on
this program that a cap test agreed with the mutation by accident — worth
remembering that an eviction-ORDER test must put the thing under test somewhere
the naive rule would keep.

## 6. Tests and gates

New suite `tests/agent_runtime/test_realm_sync_ledger_union.py` (18 cases): the
plan's merge matrix against the pure functions, plus the same shapes end to end
through `pull_realm_sync` on a local realm.

Merge matrix, all green:

| row | result |
|---|---|
| local-only entry survives an incoming ledger without it | pass (THE regression) |
| incoming-only entry adopted | pass |
| both travel, oldest-transition-first | pass |
| both sides, differing timestamps → newer wins, either direction | pass |
| restore vs stale delete, either direction → restore | pass |
| stale restore vs fresh delete → delete | pass |
| equal stamps → delete | pass |
| unreadable stamp never outranks a readable one | pass |
| slug-less / non-dict rows dropped, non-list ledger tolerated | pass |
| cap evicts settled before a live block | pass |
| `deleted_workspace_ids` union dedupes, keeps local order, drops blanks | pass |

Focused suites run green (never a full suite): `test_realm_skill_tombstones`
(23), `test_realm_sync_ledger_union` (18), `test_realm_sync_skill_inbox` (23),
`test_skills_delete_verbs` (22), `test_realm_sync` + `_eol` +
`_profile_destinations` + `test_realm_membership` (104),
`test_goal_workspace_realm_stage42` + `test_default_scope` + `test_store` +
`test_curator_backup` (41), `test_workspace_lifecycle` +
`test_office_actor_local_eviction` (21).

Seven mutation claims added, all kills hand-proven by splicing the mutation into
the symbol's own span and running the named node. `--claims-for` preflight over
`_pulled_artifact_bytes`, `restore_skill`, `tombstone_skill`,
`skill_tombstoned`, `agent_runtime/store.py` and `agent_runtime/realm_sync.py`
before touching anything: zero pre-existing claims anchored to any symbol this
stage rewrites (the file's one prior claim is on `_office_publish_scan`).

## 7. For the live cross-machine realm test running today

Nothing in this stage touched `X:\Eternia\.hermes\` or any live store — all work
is in the worktree and in per-test tempdirs. Two things the live test should
watch for, both consequences of this change once it LANDS (not before):

1. **A pulled realm record can now differ from the publisher's bytes.** Any live
   check that compares a member's `store/realms/<id>.json` against the subtree
   copy byte-for-byte will see a legitimate difference whenever the two ledgers
   differed. Compare the parsed ledgers, not the bytes.
2. **A tombstone can no longer be cleared by publishing a realm record without
   it.** If the live test uses "republish a clean realm JSON" as a reset between
   runs, that reset now fails silently for these two ledgers — the union keeps
   what the other side holds. Reset by writing the record with an empty ledger
   on EVERY member, or by using `skills restore` (which now leaves a visible
   `restored_at` marker that wins the merge, which is the point).

Also worth knowing while operating today: `realm skills show` / the status
envelope still list ACTIVE entries only, so a restored slug simply disappears
from the tombstone rows — it does not appear as a lifted row. If an operator
wants to see that a lift happened, that is in the realm record's
`skill_tombstones`, not in the receipt.
