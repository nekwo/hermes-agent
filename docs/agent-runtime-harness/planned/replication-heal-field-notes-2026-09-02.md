# Field notes — the dropped-edge heal + the create ack's last frozen block (2026-09-02)

Running record written AS the work happened, per the field-notes ruling
(`feedback_field_notes_lane`): one file, this repo, this agent. Two queue rows,
both landed on one branch because they share nothing but a worktree and neither
is large enough to be its own wave.

**Base recorded:** `origin/main` at `2fc6df259b` ("fix(gates): nine rotted
coverage claims, a preload nobody measured, and a test whose wall was the box's
process table").
**Worktree:** `X:/Eternia/_worktrees/w5-replication` on
`fix/replication-steering-heal-skills-replay`.

---

## Row 1 — a dropped replication steering edge never heals itself

### The premise, MEASURED before anything was written

The row's account of the mechanism is exact, and it was re-run rather than
believed. A throwaway test drove three pulls of a realm publishing one child
whose `steered_by` names a parent the realm does not publish, and printed the
ack each time:

| pull | remote | ack |
|---|---|---|
| 1 | child only | `replicated: [child]`, `steering_dropped: [{child, parent, parent_absent}]` |
| 2 | child only (unchanged) | `kept_local: [child]`, `steering_dropped: []` |
| 3 | child **+ parent** | `replicated: [parent]`, `kept_local: [child]`, `steering_dropped: []` |

and `PersonaInstanceStore().get(child).steered_by == []` after all three. **The
third row is the whole defect**: the parent is standing on this machine, the
realm publishes the edge, and nothing re-applies it — ever. VERDICT: the premise
HOLDS, in the exact shape H3's field notes filed it.

The chain, line by line at the base sha:

* `apply_replicated_steering` drops a parent whose
  `paths.persona_instance_path(parent)` does not exist and returns the row;
  `applied` is then `[]`, equals the instance's own `steered_by`, and the early
  return means **no write happens** — so the local row keeps an empty edge set.
* `project_persona_instance` omits structurally-empty values, so the local body
  carries no `steered_by` key at all while the remote body carries `[parent]`.
  The hashes differ.
* `baseline[key]` was set from the REMOTE hash in phase one (the
  baseline-alignment property H3 built on purpose), so the next pull reads
  local `changed` × remote `unchanged` → `classify_three_way_pull` →
  `KEEP_LOCAL "unpublished"`.
* `written` — phase two's input — carries only rows phase one wrote or found
  converged. `kept_local` rows are not in it.

### What was NOT done, and why

Re-running phase two for every `kept_local` row is the one-line cure and it
stays REJECTED, for the reason H3 recorded: `kept_local` means "this machine
edited the row and the realm did not", so re-asserting the remote graph over it
clobbers an operator's own re-steer. The row exists to protect exactly that.

### The shape that landed

Two pieces, and the second is the one that matters.

1. **A durable record of the drop.**
   `paths.persona_instance_dropped_steering_path(realm_id)` —
   `persona_instance_dropped_steering.json` beside the baseline it is always
   read with, never synced, never published. Entries are
   `{instance_id: {"parents": [...], "remote_hash": "..."}}`.
2. **A discriminator, `_healable_dropped_parents`,** which is the whole of "tell
   a dropped edge from an authored re-steer". It asks two questions, and the
   second is the load-bearing one: the recorded `remote_hash` is still the
   remote hash (the realm has not moved the body since the drop), AND the local
   body is **exactly** the remote body minus those parents. Anything else — a
   re-steer, a renamed display name, a moved model override — differs somewhere
   the dropped edge cannot account for, and the answer is to leave it alone.

Phase one consults it on the `KEEP_LOCAL` arm only. A candidate re-enters phase
two and its OUTCOME is decided there, because "has the parent actually arrived"
is phase two's question: healed edges are named on the new `steering_healed`
list and the row is counted in `adopted`; a row where nothing arrived is
`kept_local` exactly as before, having cost one store read.

### Four decisions taken while building, with their arguments

1. **The ledger is REBUILT by the pass that ran phase two, never merged.** An
   entry survives only while the same pass drops the same edge again. That is
   what makes a healed edge, a HELD row, a row the realm stopped carrying and a
   row whose remote body moved all clear themselves with **no expiry rule and no
   second pruning walk** — the alternative is a ledger that accumulates and then
   needs its own garbage collector, which is a second authority over the same
   fact. The cost is one hard requirement, stated as a test: the two arms that
   return BEFORE phase two (an older peer's absent projection, an unreadable
   one) must not write it. Neither is evidence about a dropped edge, and
   forgetting every one of them on a single pull in a rotation would silently
   end the heal.
2. **Only `parent_absent` is recorded.** A self edge and a cycle are refusals of
   the remote GRAPH — no parent is ever going to arrive and make them valid — so
   recording them would re-enter phase two every pull to re-report a verdict
   that cannot change. Pinned, because the mutation that widens the filter to
   every drop reason passes every other test in the file.
3. **A healed row is `adopted`, not `converged`.** `converged` promises no
   write, and phase two writes. `adopted` is documented as "the travelling
   surface moved forward onto an existing row", which is literally what
   happened — and it keeps `summary.changed` true without a fourth counter.
4. **The empty edge set is spelled as an ABSENT key, not `[]`.**
   `_remote_body_without_edges` pops `steered_by` when the remaining list is
   empty, because `project_persona_instance` omits structurally-empty values. A
   version that dropped to `[]` would hash as a different body and the heal
   would never recognise its own handiwork. This was found by reading the
   projector, not by a failing test — the test that would have caught it is the
   heal's own, so it would have looked like the heal not working at all.

### Red-proofs

Each test was written and watched red against the base before the production
change existed, then watched green after.

| Case | Test |
|---|---|
| a dropped edge re-applies once the parent arrives, then converges | `test_a_dropped_steering_edge_re_applies_itself_once_its_parent_arrives` |
| an authored re-steer is never clobbered | `test_an_operator_re_steer_is_left_alone_by_the_heal` |
| a parent that never arrives stays dropped AND stays accounted, every pass | `test_a_parent_that_never_arrives_stays_dropped_and_stays_accounted` |
| a self edge is not a heal candidate | `test_a_self_edge_is_not_a_heal_candidate` |
| an older peer's absent projection never clears the ledger | `test_an_older_publisher_never_clears_the_heal_ledger` |
| a ledger entry taken against a body the realm has since moved heals nothing | `test_a_ledger_entry_taken_against_a_body_the_realm_has_since_moved_heals_nothing` |

**One guard is UNREACHABLE through the applier and is kept anyway**, on the H3
precedent (`replicate_instance`'s own allowlist). `KEEP_LOCAL` is only reached
when the remote hash equals the baseline, and the baseline is the hash the
ledger entry was written beside — so the `remote_hash` comparison can never
decide anything from inside `apply_persona_instance_pull`. It is not redundant:
the ledger is a SEPARATE durable file from the baseline, and nothing guarantees
the next caller kept the two in step. Closed the way H3 closed its twin — by a
test that calls the helper DIRECTLY with a stale entry, and asserts in the same
breath that the aligned entry does heal, so the assertion is about the guard and
not about the fixture.

### The wire delta (additive), and the answer to the question the row asked

> report whether the healed case needs a new envelope field (additive) so the
> launcher can stop saying "will not re-apply"

**Yes, and it is one list.** `result["persona_instance_sync"]` gains:

```
steering_healed: [{key, parent}]   # an edge an EARLIER pull dropped, re-applied
```

sorted by construction (it is built in the order phase two walks `written`),
additive, and absent-key-means-older-hermes exactly like every other list in the
block. A healed row ALSO appears in `adopted`.

One behaviour change the launcher should know about beside the new field:
`steering_dropped` now **repeats** for as long as a parent stays absent, where
before it was emitted once on the pull that dropped the edge and then went
silent. That is the honest reading — a drop announced once and then silent reads
as repaired — and it is what lets `AGENT LINKS DROPPED` be a live group rather
than a one-shot notice.

---

## Row 2 — `agent_create`'s `skills` block, the last verbatim-replayed mutable field

### Premise

HOLDS. `_reply`'s own docstring lists `skills` beside `persona_instance_id` /
`placement_id` / `default_chat_session_id` / `actor_key` as "IDENTITY and the
recorded decision", and the block is not one thing:

* `assigned` is read BACK off `PersonaInstance.skill_overrides` at build time —
  `run_skills_phase` says so in the comment beside its own `return`, and calls
  that read "what keeps it a guarantee instead of a claim". That field is
  mutated afterwards by `update_profile(skills=…)` / `clear_skills=True`.
* `installed[].installed_hash` is `SkillInstallResult.installed_hash`, the hash
  of the package under `harness_skill_destination` — which the next
  `install_harness_skill` of the same id displaces (it literally archives the
  old package under `.archive/` and re-hashes).

Both are OBSERVATIONS. The `STATE_DONE` arm replays them verbatim, which is the
same defect `actor`/`position`/`revision` had before S4/S4b, one key over.

### The split that landed

`_observed_skills` re-reads the two observations and returns a freshness bool;
`_reply` stamps it as `skills_fresh`, and the arms that BUILD a block go through
`_stamp_fresh_skills` so the flag has one shape on every reply the way
`actor_fresh` does.

**What deliberately stays verbatim, and this is the interesting half:**

* `inherited` — a statement about the REQUEST (was a `skills` key sent at all),
  not about the row. `_inherited_skills_ack` already spends a paragraph on why
  re-deriving it from `skill_overrides` would make the ack a second authority
  for what this key decided, and that argument does not weaken just because the
  block's neighbours became observations. The visible consequence is that a
  replay CAN answer `inherited: true` beside a non-empty `assigned` — a create
  that overrode nothing, on a row somebody has since given skills to. That pair
  is not a contradiction; it is the decision and the observation, side by side,
  which is the whole point of the split.
* `installed[].skill` and `installed[].changed` — `changed` is what THIS install
  did, a fact about an event, not about the world now.

### Decision worth recording

**The valve went at the TOP LEVEL (`skills_fresh`), not inside the block.**
Three tests assert the block's exact dict (`{"assigned": [], "installed": [],
"inherited": True}`) and those assertions are load-bearing — they are the
`inherited` contract's own pins. A key added inside would have forced all three
to be rewritten in the same change that alters the behaviour they pin, which is
how a pin stops being one. `skills_fresh` sits beside `actor_fresh`, which is
also where the queue row said to put it.

### Red-proofs

| Case | Test |
|---|---|
| a replay re-reads `assigned` and `installed_hash`, keeps `inherited` / `changed` | `test_an_idempotent_replay_re_reads_the_skills_block_instead_of_echoing_the_receipt` |
| a replay whose instance row is gone says `skills_fresh: false` and returns the block UNCHANGED | `test_a_replay_whose_instance_is_gone_returns_the_recorded_skills_unchanged` |

Anti-vacuity in the first: the override is moved through the REAL store verb
(`update_profile(skills=[])`) and the installed package through a REAL byte
append, so both re-reads have to differ from the receipt rather than
coincidentally agreeing with it.

### One new read-only helper

`skill_install.installed_harness_skill_hash(skill)` — the read half of the
`installed_hash` `install_harness_skill` computes at the end of a write. It
existed nowhere: `harness_skill_hash_states` answers *matches / mismatch /
not_installed / no_source* and never hands back the number. `None` for a
non-canonical id and for an absent destination, which is the same absence
`SKILL_HASH_NOT_INSTALLED` names.

---

## Numbers

* `tests/agent_runtime/test_persona_instance_pull.py` — 25 → **31 passed**
  (6 new).
* `tests/agent_runtime/test_agent_create_service.py` — 45 → **47 passed**
  (2 new).
* Adjacent suites, unchanged and green: `test_persona_instance_sync.py` (30),
  `test_persona_instance_publish.py` (13), `test_persona_instance_drift.py`
  (18), `test_persona_instance_unreadable_row.py` (5),
  `test_serve_rpc_agent_create.py` (62), `test_agent_create_reservations.py`
  (5), `test_agent_create_subphases.py` (7); the eight `tests/agent_runtime/
  test_realm_*.py` files, **211 passed**.
* Mutation: 10 new claims (`ir-heal-*` ×6, `ac-skills-*` ×4).

## One process note

`test_an_unwarmed_mint_bills_its_probe_rounds_to_the_chat_lane_scope_read`
(`test_agent_create_subphases.py`) reported **FLAKY** — failed once, passed on
the runner's automatic file retry — during the adjacent-suite run. It is a
wall-clock probe-round test and nothing on this branch touches its subject.
Not investigated here; rowed rather than swallowed, because a FLAKY report is a
bug and not noise.
