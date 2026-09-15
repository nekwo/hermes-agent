# Skill three-way sync — the mutation record, 2026-09-12

Evidence for the eighteen checks added with the skill family's three-way lane
(commit `feat(realm sync): the SKILL family joins the three-way model`). Written
because the repo's rule is EVIDENCE, not intent: when you add or change a gate,
a fixture or an assertion, state the one-line mutation that kills it, APPLY that
mutation, record the failure output, revert, and cite the record. An unrecorded
red is a belief.

Design authority for the lane and for the two mandated mutations (§4.4 "drop the
re-mirror", "drop the baseline write"):
`EterniaLauncher/docs/mission_control/planned/held-skill-publish-direction.md`.

## How this was taken

Each entry below is one mutation applied to the tree, the named test(s) run
against it, and the file restored from its pre-mutation bytes in a `finally`.
The driver re-hashed every mutated file at the end and printed `RESTORED` for
each — that line is part of the record on purpose: a sabotage round that cannot
prove it reverted has left running code behind (the repo's
`sabotage-needs-round-trip` rule).

Runner: `python -m pytest -q <node ids>` from the worktree with
`PYTHONPATH` pointing at it. The worktree's own code was proven to be what runs
first, by a separate throwaway sabotage of
`agent_runtime/sync_text.canonicalize_text_bytes` that reddened
`test_pull_converges_eol_only_difference` (exit 1) and was then reverted — the
venv in use has the PRIMARY checkout installed editable, so a mutation that did
not red would have meant the wrong tree was under test.

## What the first round got WRONG, and why it is written down

M8 and M9 — the two the design note MANDATES — were first applied by renaming
their calls to undefined names. Both "reddened", with a `NameError`. **That is a
crash, not a measurement**: it proves the line is executed, and says nothing
about what the assertion detects, so a test asserting nothing at all would have
passed that round identically. They were redone as genuine DELETIONS of the two
blocks, and the second round is below. The failure output is the point of the
difference: M8 now reds on a missing `skill::foo` baseline entry and M9 on the
inbox still holding the realm's pre-publish bytes.

## Round 1 — eighteen mutations, eighteen kills

```text
### M1 sync hash: drop the EOL canonicalization in skill_package_sync_hash
    file: agent_runtime/skill_promotion.py
    tests: test_pull_converges_crlf_canonical_against_lf_arrival test_sync_hash_is_eol_agnostic_where_the_byte_hash_is_not test_classify_promotion_converges_a_crlf_canonical_against_an_lf_source
    pytest exit: 1
tests\agent_runtime\test_skill_promotion.py:701: AssertionError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_realm_sync_skill_inbox.py::test_pull_converges_crlf_canonical_against_lf_arrival
FAILED tests/agent_runtime/test_skill_promotion.py::test_sync_hash_is_eol_agnostic_where_the_byte_hash_is_not
FAILED tests/agent_runtime/test_skill_promotion.py::test_classify_promotion_converges_a_crlf_canonical_against_an_lf_source
3 failed in 1.51s

### M2 sync hash: collapse it to a constant (positive control for M1)
    file: agent_runtime/skill_promotion.py
    tests: test_sync_hash_still_separates_real_content_differences
    pytest exit: 1
E        +  where 'constant' = skill_package_sync_hash(WindowsPath('X:/Eternia/test-tmp/run-e_jh3z9h/pytest-of-beast/pytest-0/test_sync_hash_still_separates-0/a/foo'))
E        +  and   'constant' = skill_package_sync_hash(WindowsPath('X:/Eternia/test-tmp/run-e_jh3z9h/pytest-of-beast/pytest-0/test_sync_hash_still_separates-0/b/foo'))
tests\agent_runtime\test_skill_promotion.py:672: AssertionError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_skill_promotion.py::test_sync_hash_still_separates_real_content_differences
1 failed in 0.95s

### M3 canonicalize_text_bytes: drop the NUL binary guard
    file: agent_runtime/sync_text.py
    tests: test_sync_hash_passes_binary_assets_through_untouched
    pytest exit: 1
E        +  where 'd836e182c4fb9a748887872618ddf6b6f97cb5834cbf2ee0531e1b1e94cebec3' = skill_package_sync_hash(WindowsPath('X:/Eternia/test-tmp/run-utbrhg7c/pytest-of-beast/pytest-0/test_sync_hash_passes_binary_a-0/one/foo'))
E        +  and   'd836e182c4fb9a748887872618ddf6b6f97cb5834cbf2ee0531e1b1e94cebec3' = skill_package_sync_hash(WindowsPath('X:/Eternia/test-tmp/run-utbrhg7c/pytest-of-beast/pytest-0/test_sync_hash_passes_binary_a-0/two/foo'))
tests\agent_runtime\test_skill_promotion.py:684: AssertionError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_skill_promotion.py::test_sync_hash_passes_binary_assets_through_untouched
1 failed in 0.93s

### M4 classifier: map the 'unpublished' reason to held instead of kept_local
    file: agent_runtime/skill_sync.py
    tests: test_pull_keeps_my_edit_and_reports_it_as_unpublished_drift
    pytest exit: 1
E         Right contains one more item: 'foo'
E         Use -v to get more diff
tests\agent_runtime\test_realm_sync_skill_inbox.py:853: AssertionError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_realm_sync_skill_inbox.py::test_pull_keeps_my_edit_and_reports_it_as_unpublished_drift
1 failed in 1.26s

### M5 classifier: map the 'take_remote' reason to held instead of updated
    file: agent_runtime/skill_sync.py
    tests: test_pull_fast_forwards_a_realm_change_over_an_untouched_local_copy
    pytest exit: 1
E         Right contains one more item: 'foo'
E         Use -v to get more diff
tests\agent_runtime\test_realm_sync_skill_inbox.py:883: AssertionError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_realm_sync_skill_inbox.py::test_pull_fast_forwards_a_realm_change_over_an_untouched_local_copy
1 failed in 1.27s

### M6 classifier: map the 'both_changed' reason to updated instead of held
    file: agent_runtime/skill_sync.py
    tests: test_pull_holds_only_when_both_sides_moved_since_the_baseline
    pytest exit: 1
E         Right contains one more item: 'foo'
E         Use -v to get more diff
tests\agent_runtime\test_realm_sync_skill_inbox.py:916: AssertionError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_realm_sync_skill_inbox.py::test_pull_holds_only_when_both_sides_moved_since_the_baseline
1 failed in 1.32s

### M7 drift walk: drop the skill family from store_drift_items
    file: agent_runtime/realm_sync.py
    tests: test_a_never_published_local_package_is_added_drift
    pytest exit: 1
>       assert status["store_drift"]["skills"]["skills_added"] == 1
E       assert 0 == 1
tests\agent_runtime\test_realm_sync_skill_inbox.py:937: AssertionError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_realm_sync_skill_inbox.py::test_a_never_published_local_package_is_added_drift
1 failed in 1.20s

### M8 publish: DROP THE BASELINE WRITE (design note section 4.4)
    file: agent_runtime/realm_sync.py
    tests: test_publish_records_the_baseline_so_a_later_edit_is_drift_not_a_hold
    pytest exit: 1
    _MUTATION_dropped_baseline_write(
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
NameError: name '_MUTATION_dropped_baseline_write' is not defined
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_realm_sync_skill_inbox.py::test_publish_records_the_baseline_so_a_later_edit_is_drift_not_a_hold
1 failed in 2.27s

### M9 publish: DROP THE RE-MIRROR (design note section 4.4)
    file: agent_runtime/realm_sync.py
    tests: test_publish_remirrors_the_inbox_so_the_hold_does_not_survive_it
    pytest exit: 1
    _MUTATION_dropped_remirror(
    ^^^^^^^^^^^^^^^^^^^^^^^^^^
NameError: name '_MUTATION_dropped_remirror' is not defined
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_realm_sync_skill_inbox.py::test_publish_remirrors_the_inbox_so_the_hold_does_not_survive_it
1 failed in 2.57s

### M10 resolve: skip the baseline advance on --take local
    file: agent_runtime/skill_sync.py
    tests: test_resolve_take_local_turns_the_hold_into_publishable_drift
    pytest exit: 1
E         Left contains one more item: 'foo'
E         Use -v to get more diff
tests\agent_runtime\test_realm_sync_skill_inbox.py:1043: AssertionError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_realm_sync_skill_inbox.py::test_resolve_take_local_turns_the_hold_into_publishable_drift
1 failed in 1.44s

### M11 resolve: --take remote without adopt_divergent (no install, no archive)
    file: agent_runtime/skill_sync.py
    tests: test_resolve_take_remote_installs_the_realm_copy_and_archives_mine
    pytest exit: 1
                    )
E                   agent_runtime.skill_sync.SkillResolveError: canonical differs from source â€” held for explicit resolution (source=9131834f2714 canonical=4129115c9dee)
agent_runtime\skill_sync.py:331: SkillResolveError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_realm_sync_skill_inbox.py::test_resolve_take_remote_installs_the_realm_copy_and_archives_mine
1 failed in 1.18s

### M12 resolve: ignore --dry-run
    file: agent_runtime/skill_sync.py
    tests: test_resolve_dry_run_writes_nothing_at_all
    pytest exit: 1
>       assert row["archived_previous_to"] is None
E       AssertionError: assert 'X:\\Eternia\\test-tmp\\run-escix3zs\\pytest-of-beast\\pytest-0\\test_resolve_dry_run_writes_no-0\\hermes_test\\shared\\skills\\.archive\\20260913T020836_859893Z\\foo' is None
tests\agent_runtime\test_realm_sync_skill_inbox.py:1081: AssertionError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_realm_sync_skill_inbox.py::test_resolve_dry_run_writes_nothing_at_all
1 failed in 1.24s

### M13 list_inbox_packages: report the two-way action as the decision
    file: agent_runtime/skill_promotion.py
    tests: test_list_inbox_packages_carries_the_three_way_decision
    pytest exit: 1
E         - held
E         + hold_divergent
tests\agent_runtime\test_skill_promotion.py:718: AssertionError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_skill_promotion.py::test_list_inbox_packages_carries_the_three_way_decision
1 failed in 1.03s

### M14 revert: install a skill without adopt_divergent (no archive of mine)
    file: agent_runtime/realm_revert.py
    tests: test_a_changed_skill_reverts_to_the_realms_copy_and_archives_mine
    pytest exit: 1
>       assert result["reverted"] == 1
E       assert 0 == 1
tests\agent_runtime\test_realm_revert.py:945: AssertionError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_realm_revert.py::test_a_changed_skill_reverts_to_the_realms_copy_and_archives_mine
1 failed in 1.14s

### M15 revert: the added arm archives nothing
    file: agent_runtime/realm_revert.py
    tests: test_an_added_skill_reverts_by_archiving_it_and_mints_no_tombstone
    pytest exit: 1
E        +  where True = exists()
E        +    where exists = WindowsPath('X:/Eternia/test-tmp/run-ut660xdb/pytest-of-beast/pytest-0/test_an_added_skill_reverts_by-0/hermes_test/shared/skills/foo').exists
tests\agent_runtime\test_realm_revert.py:972: AssertionError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_realm_revert.py::test_an_added_skill_reverts_by_archiving_it_and_mints_no_tombstone
1 failed in 1.05s

### M16 revert: the skill upstream is always absent
    file: agent_runtime/realm_revert.py
    tests: test_a_removed_skill_reverts_by_reinstalling_the_realms_copy
    pytest exit: 1
E         At index 0 diff: 'baseline_entry_dropped' != 'restored_from_upstream'
E         Use -v to get more diff
tests\agent_runtime\test_realm_revert.py:998: AssertionError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_realm_revert.py::test_a_removed_skill_reverts_by_reinstalling_the_realms_copy
1 failed in 1.13s

### M17 revert: the skill family reads the wrong baseline dict
    file: agent_runtime/realm_revert.py
    tests: test_a_removed_skill_the_realm_no_longer_carries_drops_the_stale_baseline
    pytest exit: 1
E         {'skill::foo': 'deadbeef'}
E         Use -v to get more diff
tests\agent_runtime\test_realm_revert.py:1015: AssertionError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_realm_revert.py::test_a_removed_skill_the_realm_no_longer_carries_drops_the_stale_baseline
1 failed in 1.28s

### M18 revert: ignore --dry-run
    file: agent_runtime/realm_revert.py
    tests: test_a_skill_dry_run_writes_nothing
    pytest exit: 1
E         At index 20 diff: b'R' != b'M'
E         Use -v to get more diff
tests\agent_runtime\test_realm_revert.py:1039: AssertionError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_realm_revert.py::test_a_skill_dry_run_writes_nothing
1 failed in 1.18s

=== revert check ===
    agent_runtime/realm_revert.py: RESTORED 9f41f625035ff298
    agent_runtime/realm_sync.py: RESTORED 07a3aa24eb4161a6
    agent_runtime/skill_promotion.py: RESTORED 5f2c7d11cdbc3c48
    agent_runtime/skill_sync.py: RESTORED c35a606ebf6b6847
    agent_runtime/sync_text.py: RESTORED ca92e4ce075cddcd

ALL MUTATIONS KILLED
```

## Round 2 — M8 and M9 redone as deletions

```text
### M8 publish: DELETE THE BASELINE WRITE (design note section 4.4)
    file: agent_runtime/realm_sync.py  (the block DELETED, not renamed)
    test: test_publish_records_the_baseline_so_a_later_edit_is_drift_not_a_hold
    pytest exit: 1
        over content only this member ever touched, and offers no revert row at all.
        """
        realm, _repo = _realm_with_remote(tmp_path)
        _seed_canonical("foo", body="# Mine v1\n")
        publish_realm_sync(realm.id)
>       assert read_skill_baseline(realm.id)[skill_baseline_key("foo")] == skill_package_sync_hash(
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
            _canonical("foo")
        )
E       KeyError: 'skill::foo'
tests\agent_runtime\test_realm_sync_skill_inbox.py:959: KeyError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_realm_sync_skill_inbox.py::test_publish_records_the_baseline_so_a_later_edit_is_drift_not_a_hold
1 failed in 2.13s
    revert: RESTORED 07a3aa24eb4161a6

### M9 publish: DELETE THE RE-MIRROR (design note section 4.4)
    file: agent_runtime/realm_sync.py  (the block DELETED, not renamed)
    test: test_publish_remirrors_the_inbox_so_the_hold_does_not_survive_it
    pytest exit: 1
        _mirror_realm_skill_inbox(_subtree_skills(repo, realm), realm_inbox_dir(realm.id))
        _seed_canonical("foo", body="# Mine v2\n")
        assert realm_sync_status(realm.id)["skills_drift"] == ["foo"], "precondition: a real hold"
        publish_realm_sync(realm.id)
        canonical_bytes = (_canonical("foo") / "SKILL.md").read_bytes()
>       assert (realm_inbox_dir(realm.id) / "foo" / "SKILL.md").read_bytes() == canonical_bytes
E       AssertionError: assert b'---\nname: ...n# Realm v1\n' == b'---\nname: ...\n# Mine v2\n'
E         
E         At index 20 diff: b'R' != b'M'
E         Use -v to get more diff
tests\agent_runtime\test_realm_sync_skill_inbox.py:1000: AssertionError
=========================== short test summary info ===========================
FAILED tests/agent_runtime/test_realm_sync_skill_inbox.py::test_publish_remirrors_the_inbox_so_the_hold_does_not_survive_it
1 failed in 2.56s
    revert: RESTORED 07a3aa24eb4161a6

BOTH MANDATED MUTATIONS KILLED BEHAVIOURALLY
```
