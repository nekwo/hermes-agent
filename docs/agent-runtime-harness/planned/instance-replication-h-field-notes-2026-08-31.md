# Field notes — instance replication, hermes stages H1–H4 (2026-08-31)

Running record written AS the work happened, per the field-notes ruling
(`feedback_field_notes_lane`): one file, this repo, this agent. The authority is
the launcher repo's `docs/mission_control/planned/instance-replication.md`; the
mandate is `docs/mission_control/planned/dispatch-instance-replication-2026-08-31.md`.
Nothing here amends either — deltas are reported for the orchestrator to stamp.

**Base recorded:** `origin/main` at `51b96505f0a5457f1702877770ff08ffdb23cc6f`
("test(realm-sync): the revert verb's routing, its --yes gate and its envelope
shape", 2026-08-31 21:16:49 -0400) — the exact sha the dispatch expected, so no
plan cite needed re-verification for drift.
**Worktree:** `X:/Eternia/_worktrees/repl-h` on `feat/instance-replication-hermes`.
Every measurement below is against that branch, never `origin/main`.

## Plan premises re-measured before building

All of these were checked against live code at the recorded base, not taken from
the plan header.

| Premise | Verdict |
|---|---|
| `PersonaInstance` has 32 dataclass fields | **HOLDS** — `len(dataclasses.fields(PersonaInstance)) == 32`. |
| `classify_three_way_pull` at `sync_merge.py:54` | **HOLDS**, exact line. |
| Applier order: profile-file lane `realm_sync.py:516`, workspace tombstones `:522` | **HOLDS** — the insertion seam is between `apply_profile_artifact_pull` and `_apply_workspace_tombstones`. |
| `_destination_for_sync_path` returns `None` for an unknown `store/*` path (final fallthrough `:2218`) | **HOLDS** — and `store/persona_instances.yaml` therefore already resolves to `None` at the base sha, before any change of mine. Pinned by a test. |
| `OfficeActor` carries `persona_instance_id` (`models.py:306`) | **HOLDS** — so `_office_publish_scan`'s existing single walk over `scan.actors` can yield the instance ids beside the persona ids with no second glob. |
| `_office_publish_scan` at `realm_sync.py:1525`, one walk for artifacts + persona ids + refusals | **HOLDS**. |
| `sync_admission.refuse_entity` at `:207`, both scanner passes at `:186-192` | **HOLDS**. |
| `is_canonical_persona_channel` / `persona_instance_id_for` at `persona_assignments.py:2727` / `:2719` | **HOLDS**. |
| `_validate_no_steering_cycle` at `persona_assignments.py:1406` | **HOLDS**. |
| s55 gate asserts every registered event has an emitter; pins `persona_instance.created` | **HOLDS** — registry is `agent_runtime/decision_contract_registry.py` (the plan does not name the file; recorded here). |

### Premise that did NOT survive re-measurement (documentation only)

**The plan header's "18 never-travel, of which 5 are re-derived on mint" is a
miscount of its own §1.3 table, which lists SIX rows** (`runtime_root`, `role`,
`profile_id`, `state`, `default_chat_session_id`, `updated_at`). The tables are
the binding artifact and they are internally consistent — 14 travel + 18 never =
32, with 6 of the 18 re-derived and 12 born at their dataclass default. H1
implements the TABLES. The header's "5" is the same class of error as the
survey's "40 fields" that the audit already corrected one line above it.
No behavioural consequence; rowed for the orchestrator to fix in place.

## H1 — the record split as code

`agent_runtime/persona_instance_sync.py`. Pure: no IO, no git, records injected.

The split is spelled as **three disjoint sets** rather than the two the ruling's
sentence suggests, because "does this leave the machine" and "is this re-derived
on arrival" are different questions about the same field and both have to be
answerable:

* `PERSONA_INSTANCE_ALLOWED_KEYS` — 14, travels.
* `PERSONA_INSTANCE_DERIVED_KEYS` — 6, re-derived by the mint.
* `PERSONA_INSTANCE_LOCAL_ONLY_KEYS` — 12, neither carried nor re-derived.
* `PERSONA_INSTANCE_NEVER_TRAVELS_KEYS` = derived ∪ local-only = 18.

`14 + 6 + 12 = 32`, and the totality test asserts exactly that partition over
`dataclasses.fields(PersonaInstance)`.

### Three decisions taken while building, with their arguments

1. **No import-time assertion of the partition.** The first cut had one. It was
   removed: an unclassified field would then surface as a COLLECTION error on
   every suite that imports the module rather than as one named failure saying
   which field nobody classified. The plan calls the totality test "the point of
   this stage"; a guard that steals its red makes the stage's own signal
   unreadable.
2. **Id safety is asked by ROUND-TRIP, not by a second regex.**
   `valid_persona_instance_id` refuses any id where
   `paths.safe_path_token(id) != id`. `persona_instance_path` writes through
   that sanitizer, so an id the sanitizer would rewrite is an id whose row lands
   under a key that is NOT the key the realm agreed on — the merge unit silently
   renamed, which is precisely the non-convergence Option B was refused for.
   Asking the real sanitizer means a bespoke pattern cannot disagree with it.
   (It also happens to catch `..`, `/`, `\` and `:` for free.)
3. **`model_override_issued_at` is hashed, `updated_at` never enters.** The
   clock is the supersession order for the override tier, so a body whose clock
   moved IS a changed body. The local write clock is excluded for the reason
   `office_models._HASH_EXCLUDE` states.

### Red-proofs run (implementation first, tests after, each watched red)

Each sabotage was applied to the source, the suite run, the source restored.

| Sabotage | Test that went red |
|---|---|
| Add a new unclassified field to `PersonaInstance` | `test_every_persona_instance_field_is_classified` |
| Move `current_chat_goal` to travels | `…_the_fields_the_ruling_named_…`, `…_a_live_binding_never_reaches_the_wire` |
| Publish stops skipping canonical rows | `test_a_canonical_row_is_skipped_not_refused` |
| Pull door stops refusing canonical ids | `…_refuses_a_canonical_channel_id_from_a_peer` |
| Unexpected-key rule removed | `…_refuses_session_and_run_state_as_unexpected_keys` |
| `safe_path_token` round-trip check removed | `…_refuses_traversal_and_non_instance_shaped_ids[personainst_a/b, personainst_x:y]` |
| `steered_by` shape guard removed | `…_refuses_a_non_instance_shaped_steering_parent` |
| `prose_keys=frozenset()` reverted to the shared default | `…_scans_display_name_because_an_instance_body_is_all_wiring` |
| Publish-side portability scan removed | `…_a_machine_shaped_display_name_refuses_the_record_…` |
| `model_override_issued_at` dropped from travels | `…_the_supersession_clock_travels_as_a_parseable_timestamp` |

**Two sabotages initially survived, and both were real gaps, not false alarms:**

* Reverting `prose_keys=frozenset()` changed nothing, because the only
  portability test used `runtime_root` — a key the shared `PROSE_KEYS` set does
  not exempt. `display_name` IS in that set. Closed by a test that asserts the
  shared default exempts it and that this lane does not.
* `sort_keys=False` changed nothing. Investigated rather than patched over:
  determinism has TWO redundant mechanisms (the walk sorts ids and field names;
  the dump sorts keys). Removing either alone is invisible; removing BOTH reds
  `test_the_projection_is_deterministic_and_lf` (measured). The test claims the
  observable property rather than one of the two lines, and the docstring
  records the pair so the next reader does not conclude it is uncovered.
