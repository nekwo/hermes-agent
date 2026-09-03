# w11/hermes — field notes, 2026-09-03

The hermes half of wave 11's `hermes` lane. Four rows, all RULED by the operator
the same morning; the launcher half of two of them is in
`EterniaLauncher/docs/tooling/W11_HERMES_L_FIELD_NOTES_2026-09-03.md`.

Worktree slug `w11-hermes`, branch `w11/hermes`, base `368387ae0b`. Every run
below is `scripts/run_tests.sh` on the canonical venv the base commit landed
(`$HOME/.venvs/hermes-test`), from the worktree, with `HERMES_PYTHON` and
`HERMES_TEST_VENV` unset — i.e. the probe picking it up on its own, which is the
thing that landing was for.

---

## 1. The board pull unions its tombstone ledger — `0630ab9b30`

**What the ruling asked.** Port `merge_archived_ledgers`' rule into
`BoardStore.adopt_remote_board`, red-first, with a test that a locally-archived
card id survives a pull that omits it.

**What I measured before touching it.** `adopt_remote_board` wrote the peer's
`archived_card_ids` verbatim and said so in its own docstring, which named the
asymmetry as deliberate and rowed. `apply_board_pull` reads
`local_board.archived_card_ids` into `archived_ids` BEFORE calling the adopt, so
the pull that erases the ledger still protects the cards on that same pass — the
damage is entirely to the NEXT pull. That is why this was invisible.

**Red-first.** The union test failed with
`AssertionError: assert [] == ['card_0f7a317e…']`. The second test I wrote —
peer-order-leads / hash-neutrality — was GREEN at base, because a verbatim write
happens to produce the peer's list. I did not leave that unexamined: it was
falsified by reversing the two arguments in the finished `adopt_remote_board`
(local-first), which reds it with
`At index 0 diff: 'card_46ac42c4…' != 'card_peer_only'`. It pins the property
the union must not lose, and the mutation that would lose it is the plausible
wrong port, not an invented one.

**Where the rule went, and why not where the ruling literally said.**
`merge_archived_ledgers` moved from `office_store.py` to `sync_merge.py` rather
than being called across the two stores or copied a second time. `sync_merge` is
already the shared home of `classify_three_way_pull` — lifted there from
`board_sync` on 2026-07-17 when the office family became its second consumer —
and this is the same lift in the same direction: the classifier READS
`locally_archived`, and this is the function that keeps that input alive across a
pull. `cap` became a required keyword argument so each family keeps owning its
own `ARCHIVED_LEDGER_CAP`; `office_store.merge_archived_ledgers` stays as a
two-argument wrapper so every existing office caller and every cite is unchanged.

**The thing that caught the move was the tool, not a reader.**
`scripts/changed_line_mutation_check.py --list` refused outright:
`mutation source not found in agent_runtime/office_store.py::merge_archived_ledgers`.
That is the drift AGENTS.md §Unattended reporting warns about, working. The
claim was re-pointed and a board twin added
(`c1-board-pull-adopts-the-peer-ledger-wholesale`).

**Canon.** 01 and 06 both stated that the board family does NOT take the union;
both are now false and both were corrected. 07 carried three
`agent_runtime/office_store.py:NNN` cites that drifted when the function left
that file — the cite probe went 0 → 3 unwaived failures. Two were re-pointed at
their true lines; the third is prose that QUOTES a line range precisely to say
it had drifted, and it is now spelled without its numbers, because a cite naming
the drift drifts with it. No baseline row.

**Commands.** `scripts/run_tests.sh` over `test_board_sync`, `test_board_store`,
`test_office_store`, `test_office_sync`, `test_sync_admission`,
`test_serve_rpc_office`, `test_office_state_patches`,
`test_persona_instance_drift`, `test_mutation_claim_anchoring`,
`test_mutation_claim_preflight` — 10 files, **281 passed, 0 failed**, exit 0.
Lane A: cite probe 0 unwaived / 0 stale, `dump_cli_contract.py --check` fresh.

**Left.** Nothing for this row. The office family's own C1 note about a
receiver-side repair being indistinguishable from an authored delete travelled
into the shared docstring and is still an open question for the delete lane, not
for this merge.

---

## 2. `--inherit-skills` — `d457569174`

**What the ruling asked.** A separate arm on `persona instance update-profile`
that writes `skill_overrides = null`, the capability as an arg, and the launcher
showing the state.

**What I measured, and the one place the row's premise was wrong.** The row said
"the roster row carries the template's `skills`, not the instance's override
state". True of the LAUNCHER's reader; false of the wire.
`persona_instance_summary` (`persona_assignments.py:3617`) and
`state_patches._persona_instance_wire_row` both already emit `skill_overrides`
as `None`-or-list beside the RESOLVED `skills`, and the launcher's own live
fixtures carry it (`patch_agent_create.json`, `live_repro_snap.json`). So no
wire field was added. Reading the fixture before adding the field is what kept
this from being a redundant key.

**Two seams had collapsed `None` onto `[]`, and finding them is most of the
work.** The store write itself is four lines.

* `_profile_patch_snapshot` stored `list(skill_overrides or [])`. That dict's
  ONLY job is the `!=` that decides whether a field moved, so an inherit write
  off an emptied agent diffed to nothing and this chokepoint shipped no
  `state.patched` field at all. Red-first proof: with the collapse restored the
  new test fails `IndexError: list index out of range` — no patch was emitted
  AT ALL, which is sharper than the red I wrote it for (the `clear_skills` write
  that set up the fixture had also emitted nothing, for the same reason).
* the `persona_instance.profile_updated` payload spelled both states `[]`.

**The refusal is at the STORE, not at argparse.** `harness call` and the RPC
lane reach `update_profile` without going through the parser, so a conflict rule
that lived in the handler would be a rule two of three doors do not have.

**Commands.** `scripts/run_tests.sh` over
`test_persona_instance_update_profile_skills`, `test_state_patches`,
`test_persona_assignments`, `test_harness_cli_contract`,
`test_flag_binding_boundary`, `test_stream_patch` — **204 passed, 0 failed**.
Contract regenerated (`--write`, 190 command paths, sha256 `a220d7e7…`) and
re-vendored byte-identical into the launcher.

**A red I did not cause.** `tests/agent_runtime/test_persona_skill_policy.py::
test_charsheet_skill_documents_exactly_the_characters_verbs_hermes_has` — the
charsheet skill's verb table is missing `payload-contract`. Proven at base
`368387ae0b` by stashing this diff and re-running: same failure, same extra
item. Recorded, left, not rowed by me.

**Left.** Nothing owed on this row. The prose in 05 about the S4 `None -> []`
collapse is still accurate and was not touched.

---

## 3. Four canon leftovers — `c3250ec92b` (hermes half)

Three of the four are hermes-side; the fourth (the hand-copied ratchet numbers)
is launcher-only.

**(1) The parity-warning canon.** Moved the office instances —
`actors_truncated` / `actors_unreadable`, `conflict_guessed_keys`,
`office_actor_conflict`'s `guessed` field, `office_actors_unreadable` — out of
07's honesty-contract rule-1 block and into 06 as its own section. What stayed in
07 is the RULE they are instances of, plus a pointer. The split is on the axis
the ruling named: 07 owns "a list that was shortened is as much a silent zero as
a phase that defaulted"; 06 owns what the office does about it.

**(2) Realm sync's own section.** 01 §Realms and workspaces was defining the
ENTITIES and then explaining the LANE, which is why the material kept growing
where it did not belong. Four blocks moved into a new `## Realm sync` with four
sub-heads (the ledger union, the per-half authorization, drift and revert, the
instance-replication lane); §Realms and workspaces keeps Realm/Workspace, the
two ledgers as fields, publish modes, workspace scoping and `workspace_template`,
plus one forward pointer on "fail closed" because which half of which verb that
gates is a lane fact. 06's adopt-arms section says it holds what those two
families do on a pull and points at the lane. 00-index's 01 row names the new
home so the split is discoverable from the index rather than only from 01.

**(4) The unrun-gate principle.** Named section at the top of 07, argued from
the conjunction (a green report is "it ran" AND "it passed"; the reader sees one
verdict), evidenced by `6979bad59`, with three consequences the canon acts on.
Both citing paragraphs in `planned/serve-small-batch-field-notes-2026-09-02.md`
now link it instead of restating the sentence.

**Commands.** Cite probe 0 unwaived / 0 stale after every edit;
`dump_cli_contract.py --check` fresh.

---

## 4. The gateway fence's real-store arm — already CLOSED at base

**What I found.** The row's own header said `CLOSED 2026-09-03 (hermes
368387ae0b)`, which is this lane's BASE commit. So the work was landed by the
canonical-venv commit before the wave started, including both tests
(`test_gateway_spawn_fence.py` +32, `test_run_tests_script.py` +71).

**What I verified rather than assumed.** Ran the two files under the runner from
this worktree: the banner printed
`▶ real store root handed to the gateway fence: X:\Eternia\.hermes` and both
files were green (39 tests, 0 failed). That banner is the whole claim — under
the old code the fence resolved the session TEMPDIR, and
`test_classifier_refuses_a_hermes_run_pointed_at_the_real_store` was asserting
against it. The ruling's "a test that the fence classifies the live root under
the runner's env" is satisfied by that pre-existing test becoming non-vacuous,
plus the new explicit-wins test beside it.

**One naming note worth keeping.** The ruling said `HERMES_REAL_ROOT`; the
landing used `HERMES_TEST_REAL_ROOT`, and its commit message argues why —
`HERMES_REAL_HOME` is a production variable that `tests/conftest.py` blanks per
test, and forwarding `HERMES_HOME` would reinstate the hazard the fence exists
for. The deviation is deliberate and documented at the constant
(`_gateway_fence.REAL_ROOT_ENV`), and a test asserts the name.

**Left.** Nothing. Discharged by deleting the row.

---

## Standing notes for the next lane

* **The mutation-claim tool is the drift detector for a refactor, and it is not
  in any automatic lane.** It caught a moved function in this wave only because
  I ran it by hand. `changed_line_mutation_check.py --list --base <base>` is
  cheap and should be part of any wave that moves a symbol.
* **A doc cite is a line number and a code move breaks it silently.** The cite
  probe caught three in this wave, all mine, all from a `-48`-line edit two
  files away from the docs. Run Lane A after any diff that changes a module's
  length, not only after a doc edit.
* **Read the fixture before adding a wire field.** The one field this wave was
  asked to add was already on the wire and had been for months.
