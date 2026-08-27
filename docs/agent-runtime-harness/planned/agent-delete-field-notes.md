# Agent delete + roster identity — hermes field notes

Running record for the hermes half of
`agent-delete-and-roster-identity.md` (Stages R1, D1, D2, and the hermes half of
D4). House rule: a falsified assumption is written here the moment it falsifies,
not at the end.

Base ref: `01b6ad1813` (the plan commit). Worktree branch:
`worktree-agent-a32a0ca2d070be5db`.

## F0 — the worktree did not contain the plan (environment, not plan)

The worktree branch was cut from `origin/main` (`081c4e0fc4`, the charsheet
recorded-home lane), which is NOT an ancestor of local `main` (`01b6ad1813`,
where the plan lives). The two have diverged: three charsheet commits on
`origin/main`, twenty-six gateway/persona commits on local `main`.

Reset the (clean, unstarted) worktree branch to `01b6ad1813` so that the plan is
present and `--base 01b6ad1813` names a real ancestor. Nothing was lost: the
three charsheet commits remain on `origin/main` and on branch
`chara/recorded-home-D-hermes`, and a fourth worktree (`X:/wt/op-backfill`)
still stands on them.

## F1 — the server's own mint is NOT discriminable (plan assumption falsified)

The plan states `agent_create.mint_placement_id` (line 338) "mints the proper
shape only when it is omitted", and R1's acceptance asks for "acceptance for
launcher-minted shapes and for omitted (server-minted) ids" — both of which
presume the server mint already clears the discriminator.

Measured: it does not.

* launcher mint (`mission_agent_identity.dart:163`) — `'${personaToken}_agent_${hex8()}'`
* launcher discriminator (`mission_agent_identity.dart:121`) — `RegExp(r'_agent_(\d+|[0-9a-f]{8})$')`
* hermes mint (`agent_create.py:338`) — `f"{token}_{uuid.uuid4().hex[:8]}"`

The hermes mint has no `_agent_` marker, so `agent create --persona profile:alice`
with `--placement-id` OMITTED mints `profile_alice_5f3a9c21`, derives
`personainst_profile_alice_5f3a9c21`, and that id fails the launcher
discriminator exactly as the hand-typed `known_alice` did. The mint's own
docstring claims it is "shaped like the launcher's own" and names
`missionMintDeliberatePlacementId` as the peer; the claim was never true.

So R1's incident is reachable through the door the plan treats as the safe one.
A fence that only refuses caller-supplied ids would leave the server mint
minting non-discriminable ids forever.

**Adaptation:** fix the mint to `f"{token}_agent_{uuid.uuid4().hex[:8]}"` as part
of R1. Nothing pins the old shape (no test, no fixture, no caller references
`mint_placement_id` outside `agent_create.py:538`), and ids already minted keep
working — the fence is on new ids only.

## F2 — R1's fence has THREE boundaries, not one (plan assumption falsified)

The plan says to fence "at the service boundary (where caller-supplied placement
ids are first validated in `agent_create.py`)", while its acceptance asks for a
refusal on `persona instance create`, `persona instance open`, AND `agent create`.

Measured: the two persona-instance verbs do not pass through `agent_create.py`
at all.

| verb | placement id first normalised | reaches |
| --- | --- | --- |
| `agent create` / `runtime.agent.create` | `agent_create.py:540` (`_parse_request`) | `AgentCreateRequest` |
| `persona instance create --add-instance` | `persona_commands.py:772` | `PersonaInstanceStore.add_instance` (`persona_assignments.py:2270`) |
| `persona instance open-chat --add-instance` | `persona_commands.py:966` | `PersonaInstanceStore.add_instance` |

A single fence in `agent_create.py` would satisfy one third of R1's own
acceptance list.

**Adaptation:** the REGEX and the refusal text are written once (one shape
authority beside `looks_like_persona_instance_id`, the module the codebase
already names as the single id-shape authority) and called from the three
boundaries in each lane's existing refusal idiom.

Deliberately NOT fenced at the store (`add_instance`), even though the class-key
fence's EG-6.6 argument favours store-level guards: that guard protects a store
INVARIANT, while this one validates caller-supplied INPUT. Rows whose ids
predate the discriminator (every canonical `personainst_<persona>` channel, and
every id the old mint produced) are legitimate and must keep resolving; a
store-level refusal would refuse them on read-modify-write paths that never
accepted operator input at all.

## F4 — R1's real cost is fixture churn the plan did not price

R1's acceptance reads as four new tests. Measured: turning the fence on reddened
**42 existing tests** across 13 suites, because the fixtures place agents with
ad-hoc tokens (`qa_nows`, `qa_phase`, `scene_child_1`, `sender`) that the new
contract makes illegal. That is not incidental — those fixtures were minting
exactly the ids the incident was made of.

Migrated 244 literals in the 13 files that DECLARE a placement id, by appending
`_agent_2` to each token. Two things this migration got wrong on the first pass
and had to be corrected:

* a first version scoped tokens globally rather than per file, so the generic
  token `sender` was rewritten in unrelated gateway and photon-plugin suites.
  Reverted and re-scoped: a token is rewritten in a file only if THAT file
  declares it as a placement id.
* the rewrite corrupted two deliberately-malformed fixtures (`"///"` and
  `"!!!"`, which exist to drive the `placement_id_invalid` arm) into
  `"///_agent_2"`. Restored by hand; they must stay un-tokenisable or the arm
  above the new one goes uncovered.

Three fixtures escaped the regex entirely because they spell the default with an
annotation (`placement_id: str = "qa_retire_1"`); fixed by hand in
`test_agent_retire_service.py`, `test_serve_rpc_agent_retire.py`, and
`test_agent_retire_verb.py`.

`test_agent_create_service.py::test_every_invalid_arm_has_a_case` is an AST walk
over the module's refusal arms, so the new reason had to be parametrised there —
that test is what stops a new arm from shipping uncovered, and it worked.

## F3 — D1 caller audit: the sanctioned resurrection verb does NOT use the arm

Audited every path that can reach the archived-key re-add arm in
`OfficeStore.upsert_actor` (`office_store.py:747`; arm at `812-830` for the
archive read and `859-863` for the ledger clear + `archived_path.unlink`).

| caller | lane | designed re-add? |
| --- | --- | --- |
| `serve_rpc._runtime_office_upsert` (`serve_rpc.py:1282`) | wire `runtime.office.upsert` | **No — mechanical.** A stale launcher canvas re-sends a removed actor on the next save. Its own docstring already refuses to take a consent parameter ("a parameter is not consent") and points operator intent at `actor-restore`. This is the lane that caused the live incident. |
| `office_cli._cmd_office_actor_upsert` (`office.py:238`, bare) | CLI `harness office actor-upsert` | **No — mechanical.** Also the launcher's own save path (`harness_capability_registry.dart:581`). |
| `office_cli._cmd_office_actor_upsert` (`office.py:297`, the `--allow-class-key` replay) | CLI, after a refusal the operator read | **Yes — the only one.** The operator has been shown `class_key_collision` (whose message names `resurrects_archived_class_key`) and has typed an override meaning "bring this back". |
| `agent_create.perform_agent_create` (`agent_create.py:1720`) | `agent create` / `runtime.agent.create` | Borderline. `persona_instance_id` is derived deterministically from `placement_id`, so re-creating a retired agent at the SAME placement re-upserts an archived key. Judged NOT a resurrection gesture — see F5. |
| `workspace_template._copy_office` (`workspace_template.py:130`) | template apply | **No — mechanical.** Says so itself: "a template apply holds no operator intent about THIS destination". |
| `scripts/office_actor_rekey_to_instance.py:189` | migration | Never in the arm by construction (upserts the NEW key, archives the OLD). |

**Verdict:** `harness office actor-restore` — the verb the codebase documents as
the sanctioned un-archive — does **not** route through `upsert_actor`.
`_cmd_office_actor_restore` (`office.py:325`) calls `OfficeStore.restore_actor`
(`office_store.py:933`), which moves the archived bytes back itself. So the
plan's "if a designed deliberate re-add gesture exists, thread `resurrect=True`
through exactly that gesture" resolves to: **the sanctioned gesture exists and
needs nothing**, because it was never on this arm. The door still gets its key,
and exactly one caller turns it — the `--allow-class-key` consent replay at
`office.py:297`.

Also noted, out of scope but worth the coordinator's attention:
`office_sync.apply_office_pull` (`office_sync.py:414`) writes actor files with
`atomic_json_write`, bypassing `upsert_actor` entirely. It is the blindest
resurrection loop in the system and this fence does not cover it; it is already
pinned as a known hole in `test_office_class_key_one_fence.py`
(`CARVED_OUT_ACTOR_WRITERS`).

A second stale fact found in passing: `serve_rpc.py:41` advises running
`harness office actor-resolve`, a verb that does not exist. The real one is
`harness office resolve-conflict`. Corrected in place while adding the fourth
4090 reason to that same doc block.

## F5 — D1: the plan's "thread resurrect through the designed gesture" resolves
to a NEW flag, because the two consents are different questions

The plan says to thread `resurrect=True` through the designed deliberate re-add
if one exists. Per F3 the sanctioned gesture (`actor-restore`) was never on this
arm, so nothing needed threading. But the audit surfaced a real hazard: the only
caller reaching the arm with operator intent was the `--allow-class-key` consent
replay (`office.py:297`), and leaving it un-threaded would make that flag a dead
end for the commonest case in the program (the class→instance migration archives
every class key, so "class-keyed AND archived" is ordinary, not a corner).

Rejected making `allow_class_key` imply `resurrect`. They answer different
questions — one is "may this write use a class key", the other "may this write
raise the dead" — and an operator who consented to the first was never asked the
second. Added `--resurrect` as its own flag instead. A write that is both now
spells both and gets two warnings on the record, which is strictly more
informative than the single silent override it replaced.

Three consequences worth recording:

* **The tombstone fence sits ABOVE the archive read.** Without consent the write
  is refused whatever the archive decodes to, so decoding first would only mean
  answering `archive_unreadable` ("ask again once the file is readable") to a
  caller whose write can never be accepted. This re-points
  `test_serve_rpc_office_upsert.py::test_a_re_add_over_an_unreadable_archive_...`
  from `archive_unreadable` to `actor_archived` on the WIRE lane;
  `archive_unreadable` keeps its coverage on the consented store-level path,
  where the revision token it protects is actually read.
* **The fence had to become a named method.** As an inline block it silently
  broke `test_office_class_key_one_fence.py::test_deleting_the_stores_fence_
  unguards_every_lane_at_once`, which isolates the class-key fence's claim by
  monkeypatching it out — every write in that test is also a re-add, so the
  second fence refused them all and the class-key claim read as proven no matter
  what. Extracted to `OfficeStore._guard_archived_actor`, matching the store's
  existing `_guard_*` idiom, so a test isolating one fence can stand the other
  down.
* **The refusal escapes through the except block.** The class-key fence runs
  FIRST, so on the `--allow-class-key` path the tombstone fence is reached
  inside the `except ClassKeyedPlacementRefused` handler — where the sibling
  `except` arms cannot catch it. The first cut of this reported `internal_error`
  for the commonest override run in the program. The arm is repeated inside that
  handler, with a comment saying why.

**Not fixed, and the coordinator should know:** `office_sync.apply_office_pull`
(`office_sync.py:414`) writes actor files with `atomic_json_write`, bypassing
`upsert_actor` and therefore this fence entirely. It is the blindest
resurrection loop in the system. Out of D1's scope (the plan names the upsert
arm), already pinned as a known hole in `test_office_class_key_one_fence.py`'s
`CARVED_OUT_ACTOR_WRITERS`, and a candidate for its own stage.
