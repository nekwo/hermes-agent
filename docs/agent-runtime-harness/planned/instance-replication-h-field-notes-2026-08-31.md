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

## H2 — the publish arm

`OfficePublishScan` grows a FOURTH fact, `instance_ids`, off the walk it already
takes. The plan says "one walk, one authority"; the concrete reason is that the
refusal gate cannot speak for a walk it did not take, so a second
`actors_dir.glob` would ship agents the gate never cleared. `actor.persona_instance_id`
is read from the PAYLOAD rather than from the actor key, because payload is
truth and the key is routing — even though `_canonical_actor_key` makes them
equal for instance-bound actors today.

New: `paths.persona_instance_baseline_path`, the sidecar read/write/update trio
at the bottom of `persona_instance_sync.py` (the module's only IO, below a line
that says so), `_persona_instance_projection` + `_persona_instance_artifact` +
`_persona_instance_row` in `realm_sync.py`, the `_kind_for_sync_path` row, and
an explicit `_destination_for_sync_path` branch.

### Decisions taken while building

1. **The instance publish arm does NOT refuse on a short read, and the office
   arm does.** Deliberate asymmetry with an argument: publish replaces the
   subtree wholesale, so for the OFFICE family an absence is a desk removal on
   every peer — which is why `_office_publish_scan` refuses a workspace whole.
   For the INSTANCE family an absence is `upstream_absent` (plan §3.3/§5.2),
   which is explicitly held and accounted, never a delete. So the shortfall is
   REPORTED (`rows_unreadable` on the publish row) rather than escalated to a
   refusal. Escalating would cost a whole realm's replication for one
   quarantined row and buy nothing.
2. **The `_destination_for_sync_path` branch is behaviour-neutral and lands
   anyway.** `store/persona_instances.yaml` already reached `None` through the
   final fallthrough at the base sha (pinned by a test written BEFORE the
   branch). The branch records OWNERSHIP the way the `store/personas.yaml` one
   above it does — otherwise the next reader has to prove the fallthrough.
3. **No empty document is ever published.** A realm with no instance-backed
   desks publishes no artifact at all, so a realm that has never placed an agent
   gains no byte-churning file.

### Red-proofs

| Sabotage | Test that went red |
|---|---|
| Instance ids re-globbed past the refusal gate | `…_come_off_the_gated_scan_never_a_second_directory_walk` |
| Projection stops pruning to the referenced set | `…_carries_exactly_the_instances_the_desks_reference` (+2) |
| Publish baseline never recorded | `…_leaves_the_publisher_with_nothing_to_hold` |
| `_kind_for_sync_path` row removed | `…_classifies_as_its_own_kind` |
| Store shortfall zeroed | `…_carries_the_projections_accounting_and_the_store_shortfall` |

**One sabotage initially survived and it was a TEST-selection error, not a
coverage gap:** the "second authority" mutant passed because the only refusal
fixture blinds an actor file, and a second glob fails to decode that file too —
the two authorities agreed by accident. The only way they can disagree without
the gate refusing first is a row the scan does not return while its file sits
readable, so the divergence is INJECTED (monkeypatched `scan_actors`), exactly
as the existing C3 test does for the artifact list.

### Mutation claims

Ten new claims registered in `tests/mutation_claims.json`
(`ir-h1-*` ×6, `ir-h2-*` ×4). One initially SURVIVED —
`ir-h1-publish-projects-a-machine-shaped-body` was pointed at
`test_runtime_root_never_reaches_the_published_bytes`, which stays green under
that mutant because the ALLOWLIST already drops `runtime_root`; the scan it
actually claims is the one catching a machine path in an AUTHORED field, so the
claim was repointed at
`test_a_machine_shaped_display_name_refuses_the_record_rather_than_shipping_it`.
Gate: **11 candidates selected, 11 killed, exit 0** against the branch base.

## H3 — the mint door (the heart)

`PersonaInstanceStore.replicate_instance` (+ `apply_replicated_steering`), the
new `persona_instance.replicated` contract, `apply_persona_instance_pull`, and
the wiring at the plan's exact seam — between `apply_profile_artifact_pull` and
`_apply_workspace_tombstones`.

### Decisions taken while building

1. **A `kept_local` row the plan's five-row table does not have.** Local drifted,
   remote did not: `classify_three_way_pull` returns KEEP_LOCAL "unpublished".
   The plan folds this into "not held"; it is named because silently adopting
   over an unpublished local edit is the clobber the whole lane exists to end,
   and because H4's drift lane reports exactly these rows.
2. **The remote-absent arm is decided BEFORE the classifier.** For any row where
   `remote_body is None` the answer is `upstream_absent`, full stop — the
   classifier's ARCHIVE_LOCAL and edit-vs-remove CONFLICT arms are both
   delete-shaped, and this family has no archive arm in the pull at all.
   Retirement follows the DESK (H4), never the absence.
3. **Local unreadable is not local absent.** Absent is what drives the MINT arm,
   so a parse error folded into it would overwrite a row that may carry a live
   run binding. Refused per row (`local_row_unreadable`), store untouched.
4. **An unreadable REMOTE projection is refused, not read as absence** — absence
   drives `upstream_absent` for every baselined row, so a parse error read as
   absence is a delete-shaped decision taken on a read failure.
5. **The chat root is made durable INSIDE the store door and a failure refuses
   the row.** `_durable_chat_root` raises rather than returning a bool precisely
   because "could not persist, carry on and bind anyway" is the defect it was
   written to close. The applier catches it as `mint_failed` and keeps pulling;
   the alternative — minting with a pointer to a SessionDB row this machine does
   not have — is the `unknown_chat_session` defect, reproduced.
6. **`persona_instance.replicated` is NOT added to
   `patch_coverage.LIVE_COVERED_DOMAIN_EVENT_TYPES`.** Leaving it uncovered
   routes a replication batch down the full-core lane with no new code and no
   new failure mode; covering it would need the §V1 derivability audit, and the
   paired create patch is a create-on-absent, which that list's own comment says
   rides the full core anyway. The row still reaches every live consumer — the
   plan's actual requirement — either way.
7. **`SURVIVING_EVENT_COUNT` 58 to 59** in `test_s15_event_contract_pruning.py`,
   with the reason written beside it. That counter going red on a legitimate new
   contract is the counter working; it is moved deliberately, never quietly.

### Known gap, named rather than improvised past

**A dropped steering edge leaves its row reading as local drift, and does not
self-heal on the next pull.** Phase two drops an edge whose parent is absent
(refused, unpublished, or canonical) and ACCOUNTS it; the row's local body then
differs from the remote body while the baseline holds the REMOTE hash — so the
next pull classifies it `kept_local`, and phase two does not re-run for those.
Re-running phase two for `kept_local` rows was considered and rejected: it would
clobber an operator's own local re-steer, which is the one thing `kept_local`
exists to protect. The narrowness is real (both ends of an edge are normally
published in the same projection, so this needs a refused or unpublished
parent), but it is a genuine non-convergence and it is rowed for the queue
rather than papered over.

### Red-proofs (ten, each watched red by reverting the line it claims)

baseline re-derived from the local write; upstream_absent turned into a delete;
HOLD clobbering; the mint posing as an authored create; the delta patch dropped;
the store door writing the whole remote body; steering collapsed to one phase;
the cycle validator skipped; an unreadable projection read as absence; an
unreadable local row minted over.

**One survived, and where it survived was the useful part:** "the store door
writes the whole remote body" is UNREACHABLE through the applier, because the
admission door already refuses any body carrying a non-allowlisted key. That
makes the door's own allowlist look redundant — and it is not, because
`replicate_instance` is a public store verb and the next caller may not be the
pull. Closed by a test that calls the door directly with a body carrying
`active_run_id` / `chat_head_home` / `runtime_root` and asserts the local record
keeps its own.

### Mutation

Ten more claims (`ir-h3-*`). **21 candidates now select against the branch base,
which exceeds the script's DEFAULT `--max-candidates 12` and exits 2.** With
`--max-candidates 30`: 21 selected, 21 KILLED, exit 0. Per-stage bases stay
under the default cap. The orchestrator needs the raised cap (or a per-stage
base) for the landing gate — this is a selector budget, not a failing claim.

## H4 — drift, revert, and retire-follows-the-desk

`DRIFT_FAMILY_PERSONA_INSTANCE` + `_persona_instance_store_drift_items` +
`_PERSONA_INSTANCE_DRIFT_COUNTS`; `classify_revert`'s existing table gains the
family (no new table — it was already total over family × kind); the revert
lane's `_Upstream.lookup` / restore / adopt / archive arms; and
`PersonaInstanceStore.retire_replica`.

### Decisions taken while building

1. **`record_tombstone=False` is STRUCTURAL for this family, not a parameter.**
   The plan asks for "`record_tombstone=False` semantics". A persona-instance
   record has no realm-visible ledger of its own — the only place this lane
   could mint one is the office half, via `_archive_office_placements` →
   `remove_actor` with its DEFAULT `record_tombstone=True`. So `retire_replica`
   simply does not run the office half, and that IS the whole of the semantics.
   Adding a parameter would have implied a ledger that does not exist.
2. **`restore` and `adopt` are the same write for this family.** A replicated
   agent has no un-archive verb and does not need one: the store door mints a
   row for an id with no live file, deriving this machine's §1.3 half exactly as
   the pull would. The archived copy stays where archive-never-delete put it.
3. **The revert routes through the FAMILY's admission door**, not just the
   shared `refuse_entity` scan — allowlist totality, canonical-id and
   steering-shape refusals included. Otherwise a revert would admit a body its
   own pull would have turned away.
4. **A resurrection guard was needed and is NOT new machinery.** Retire-follows-
   the-desk archives a row the realm still publishes, so the very next pull
   would mint it back — the retire undone in one round trip, and a desk the
   operator deleted returning with an agent behind it. The office surface's
   `archived_actor_keys` ledger already answers this, and the actor key IS the
   instance id, so the lane asks that ledger (`locally_archived=` into the shared
   classifier) rather than growing a second one. A new `desk_archived` outcome
   names the state.
   The guard is the union of that ledger and `summary.retired`, so the guarantee
   holds within a single pass without depending on another store's state.
5. **`desks_removed` comes from the office summary's `archive_outcomes`, not from
   a re-derivation.** Only that arm knows which archives it actually took: the
   ones it FENCED (`delete_fenced`, unreadable remote) and the ones it tried and
   could not are both absent from the list by construction. Retiring an agent
   for a desk removal that did not happen is the worst mistake available here.
6. **The retire arm runs BEFORE the projection is even read**, because its
   trigger is the office lane's archive rather than anything in the document — a
   peer can retire a desk in the same pull where their projection is absent,
   unreadable, or unchanged, and the replica must follow in all three cases.

### The red-proof that took three attempts, and what it revealed

`_retire_replicas_for_removed_desks` keeps the baseline entry when a retire is
HELD (the C2 lesson: dropping the entry for a still-live row re-classifies it as
a local ADD, so a failed archive comes back as something to publish). Sabotaging
that line survived twice:

* with the projection ABSENT, the baseline file is never written on the held
  path at all, so the in-memory pop never reaches disk;
* with the projection PRESENT and matching, phase one's `converged` arm silently
  re-records the entry two blocks later.

The only shape where the drop survives to disk is a locally-edited replica whose
remote ALSO moved — the `held` arm, which writes the baseline and re-records
nothing. That is the shape the test now pins, and the docstring says why, because
this is exactly how a guarantee ends up believed and untested.

### Other red-proofs (seven, each watched red)

canonical rows entering the drift walk; the unreadable-store guard removed;
retire-follows-the-desk removed; the live-binding guard removed; the
resurrection guard removed; `retire_replica` running the office half; the family
dropped from the revert selector.

### Wire additions in this stage

`store_drift.persona_instances` = `{instances_added, instances_changed,
instances_removed}` (additive; `_any_store_drift` sums it, so a locally-authored
agent nobody published now lights "unpublished changes" — the honest answer, and
the same reason the office family was added on 2026-08-29); `store_drift.items`
gains rows with `family: "persona_instance"`; the pull ack's summary gains
`retired`, `retire_held`, `desk_archived`.
