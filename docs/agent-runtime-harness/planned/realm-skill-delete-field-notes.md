# Field notes — realm skill-delete, stages S1 + S2

Running record for the build of [realm-skill-delete.md](realm-skill-delete.md)
stages S1 (ledger + store chokepoints) and S2 (pull + publish enforcement).
Written from the worktree, against `b20fa8daf9`'s tree. S3 (operator verbs) is
NOT in these commits.

---

## What landed

**S1 — `agent_runtime/{models,store,errors,skill_promotion}.py`, new
`tests/agent_runtime/test_realm_skill_tombstones.py` (21 cases).**
`SkillTombstone(slug, deleted_at, deleted_hash)`; `Realm.skill_tombstones`;
`SKILL_TOMBSTONE_LEDGER_CAP = 200`; `RealmStore.tombstone_skill` /
`restore_skill`; `store.skill_tombstoned` as the ONE match rule;
`SkillTombstoneRefused` carrying `skill_installer_owned` / `skill_slug_invalid`;
`skill_promotion._validate_slug` promoted to the public `validate_skill_slug`
(private alias kept for its in-module callers).

**S2 — `agent_runtime/realm_sync.py` only, plus seven cases (a)–(g) appended to
`tests/agent_runtime/test_realm_sync_skill_inbox.py`.**
`SkillSyncSummary.tombstoned` + mirror filtering; `_apply_skill_tombstones`
(archive-never-delete, per-slug isolation, installer-owned skip with warning);
`_skill_artifacts` publish filter; `skill_tombstones` rows on the status
envelope and the sidecar (read back absent-tolerantly).

Tests: S1 suite 21 passed; regression floor
(`test_realm_sync*.py`, `test_skill_promotion.py`,
`test_realm_skill_tombstones.py`, `test_realm_membership.py`) 161 passed at
baseline → 182 after S1 → 190 after S2. No pre-existing reds.

Mutation sanity, both flipped once and restored: neutering the categorized-child
half of `_tombstone_blocks` reddened 3 S1 cases; deleting the `_skill_artifacts`
tombstone filter reddened `test_publish_excludes_a_tombstoned_skill`.

---

## Where the plan was wrong about the code (followed the code, saying so)

1. **§2.4.1's "drop any top-level package whose slug is tombstoned" would take
   innocent siblings.** The mirror works on FILES; its only notion of a package
   is `rel_parts[0]`, which for a categorized package is the CATEGORY dir. A
   tombstone on `hermes-agent` would therefore have dropped every skill under
   `software-development/`. Built `_subtree_package_slug()` instead, which
   answers with the same package-shape rules `iter_skill_packages` /
   `_iter_publishable_skill_packages` use, and filtered per package. Pinned by
   test (f): the sibling under the same category mirrors and adopts normally.

2. **§2.4.2's "for each ledger entry whose canonical dir exists" is a SECOND
   spelling of the match rule §2.3 says must exist once.** Walking ledger
   entries archives only exact slugs, while the publish filter — which asks
   `skill_tombstoned` — blocks `<cat>/<child>` for a bare-name entry too. Two
   answers to one question, in the two halves of the same fence. The canonical
   half instead walks the publishable packages and asks the matcher, so pull and
   publish agree by construction.

3. **The realm object `pull_realm_sync` holds is STALE by the time the skill
   lane runs.** The plan anticipated a re-read for the canonical half (§2.4,
   citing `_apply_workspace_tombstones`'s comment) but not for the inbox half —
   and the inbox half is the one that now needs the ledger. `pull_realm_sync`
   reads the realm at the top, then the generic overwrite loop rewrites
   `store/realms/<token>.json` from the subtree. Without the re-read a
   freshly-arrived tombstone would be mirrored, auto-adopted, and archived
   inside one pull. One `realm = RealmStore().get(realm.id)` before the skill
   lane fixes both halves; the sidecar/result/event below it get the pulled
   record too, which is also more honest than what they had.

4. **`decision_contract_registry.py` needed no change** (S1's touch-list said
   "check"). `realm.updated` is registered once with summary fields
   `("realm_id", "change")` and does not enumerate change codes; extra payload
   keys ride freely — `set_skill_selection` already passes `mode` /
   `selection_count`, neither of which is declared.

---

## Findings that are not ours (pre-existing, filed here so they are not lost)

- **A second member's FIRST pull dies on `.gitattributes`.**
  `_ensure_sync_repo` → `_ensure_repo_gitattributes` writes the managed
  `.gitattributes` into the member's clone *before* `git pull --ff-only` runs.
  Once any member has published (which commits that file), every other member
  whose clone predates the publish has it as an UNTRACKED file, and git refuses:

  ```
  error: The following untracked working tree files would be overwritten by merge:
  	.gitattributes
  Please move or remove them before you merge.
  ```

  The bytes are identical; git refuses anyway. Hit on the first round-trip
  attempt, reproduced by hand, nothing to do with the tombstone lane (both
  functions are untouched by S1/S2). The round-trip script works around it by
  seeding the file into the server repo so both clones have it tracked. Worth a
  row of its own: the real cure is committing it on clone, or writing it only
  after the pull.

- **A pull INSTALLS the five `CANONICAL_SHARED_SKILL_IDS` into the puller's
  shared root, and that member's next publish then exports them.** Visible in
  the transcript's last line: member A published 2 artifacts and never held a
  harness skill; member B pulled (installer ran) and B's publish carries all
  five. Pre-existing `install_harness_skills` × `_skill_artifacts` interaction,
  and precisely why R-B refuses to tombstone those ids.

---

- **Tooling trap for the next agent working here.** The mutation-sanity flips
  were applied with a throwaway `Path.write_text()` script, which on Windows
  translates every `\n` to CRLF — the repo's Python files are LF, so `store.py`
  and `realm_sync.py` came back as whole-file rewrites (a 4930-line diffstat for
  a 212-line change). Caught before the hand-off; the commits were redone after
  normalizing. Use `write_bytes`, or `newline=""`.

## Two-HERMES_HOME round-trip (S2 acceptance)

One bare git repo as "the server", two homes each with their own clone, every
member step in its own process pinned to its own `HERMES_HOME`. Transcript
verbatim (`scratchpad/roundtrip.py`):

```
=== S2 round-trip: A deletes a skill, B pulls the delete ===
server: …/scratchpad/rt/server.git
A realm: realm_roundtrip
B realm: realm_roundtrip

--- A authors and publishes 'doomed' ---
A seed   : doomed seeded
A publish: published changed=True 2 artifacts
server   : ['manifest.json', 'skills/doomed/SKILL.md', 'store/realms/realm_roundtrip.json']
B pull   : {"changed": true, "skill_sync": {"adopted": ["doomed"], "converged": [], "held": [], "refused": [], "removed": [], "tombstoned": []}, "skill_tombstones": null, "state": "pulled"}
B state  : {"archived": [], "canonical_doomed": true, "ledger": [], "resolver": "resolved"}

--- A deletes it and publishes the delete ---
A delete : tombstoned doomed hash=46f08dfa8550 archived to .archive/20260828T200303_316296Z/doomed
A state  : {"archived": ["doomed"], "canonical_doomed": false, "ledger": [["doomed", "46f08dfa8550"]], "resolver": "missing"}
A publish: published changed=True 1 artifacts
server   : ['manifest.json', 'store/realms/realm_roundtrip.json']

--- B pulls: B still holds a LIVE canonical copy ---
B pull   : {"changed": true, "skill_sync": {"adopted": [], "converged": [], "held": [], "refused": [], "removed": ["doomed"], "tombstoned": []}, "skill_tombstones": {"archived": ["doomed"], "skipped_installer_owned": [], "warnings": []}, "state": "pulled"}
B state  : {"archived": ["doomed"], "canonical_doomed": false, "ledger": [["doomed", "46f08dfa8550"]], "resolver": "missing"}

--- B publishes: the subtree must still lack the slug ---
B publish: published changed=True 7 artifacts
server   : ['manifest.json', 'skills/harness-charsheet-authoring/FIELD-NOTES.md', 'skills/harness-charsheet-authoring/SKILL.md', 'skills/harness-continuity/SKILL.md', 'skills/harness-dev-delivery/SKILL.md', 'skills/harness-qa-verdict/SKILL.md', 'skills/harness-runtime-model/SKILL.md', 'store/realms/realm_roundtrip.json']
```

What it proves, line by line:

- B's ledger after the delete-pull carries **A's** `deleted_hash`
  (`46f08dfa8550`) — the record travelled in the realm JSON, not just the
  intent.
- B's pull reports `skill_tombstones.archived: ["doomed"]` and B's canonical
  copy is gone from the live namespace (`resolver: missing`) but present in
  `.archive` — the never-delete invariant held on the sync path.
- B's `skill_sync` reports the package as `removed`, not `tombstoned`: A's
  publish had already pruned the bytes from the subtree, so the mirror had
  nothing to drop. `tombstoned` is for the stale-publisher case, where the
  package is still IN the subtree (test (a)).
- B's publish — the resurrection that had nothing stopping it before — carries
  no `skills/doomed/…` path.

## Left for S3

`hermes harness skills delete|restore` per §4: multi-realm resolution (R-E), the
local archive step, inbox-mirror prune, envelopes/exit codes, `--dry-run`, and
`realm skills show`'s additive `tombstones` array. The store returns the realm,
not a `restored: bool` — S3 asks `skill_tombstoned` before calling
`restore_skill` to report that. `skill_unknown` stays a CLI-level warning (§3
preamble): nothing in S1/S2 refuses a tombstone for a slug with no local
package, because a tombstone records intent and intent is valid without a copy
on this machine.

---

# Field notes — stage S3 (operator verbs + receipts)

Written from a worktree branched at `c58c616227` and fast-forwarded onto main's
`32f41be19f` (S1+S2). Everything below is the CLI layer only.

## What landed

**`hermes_cli/harness.py`** — `skills delete <slug> [--realm …] [--json]
[--dry-run]` and `skills restore <slug> --realm <id> [--json]`, registered in
the existing `skills {inventory,catalog,publishable,inbox,promote}` group with
its own idiom (`_add_stage42_global_args`, `set_defaults(func=…)`), plus the
additive `tombstones` array on `realm skills show`'s
`realm_skill_selection` envelope.

**`hermes_cli/harness_support.py`** — `skill_slug_invalid` and
`skill_installer_owned` join `ERROR_EXIT_CODES` at family **2** (the fault is in
the request; the next move is to change what was typed) with hints of their own,
because the default hint points at `safe_details`, which here carries only the
slug the operator already typed.

**Two private helpers got their public name** (the `validate_skill_slug` /
`_validate_slug` idiom S1 established), so the CLI does not re-spell a rule:

- `store.skill_tombstone_matches` (was `_tombstone_blocks`). The delete verb has
  to ask the match rule in the direction `skill_tombstoned` cannot be asked —
  holding a CANDIDATE entry slug with no ledger yet: *which canonical packages
  would this tombstone cover, and which realms publish one of them?* Since S1
  deliberately made the tombstone rule identical to the SELECTION rule, the same
  function also answers "is this package selected"
  (`any(skill_tombstone_matches(entry, slug) for entry in selection)`), which is
  how R-E's resolution avoids importing `realm_sync._skill_slug_selected`.
- `realm_sync.skill_tombstone_rows` (was `_skill_tombstone_rows`) — `realm
  skills show` renders the same rows the sync status envelope and the sidecar
  carry, rather than a second shape free to drift.

**Tests** — `tests/hermes_cli/test_skills_delete_verbs.py`, 25 cases, every one
driving the REAL argparse tree and dispatching through `args.func`
(`test_agent_retire_verb.py`'s precedent). Parser shape (3), delete happy paths
(6), refusals/warnings (5 + a 4-way parametrize), `--dry-run` (1), restore (5),
`realm skills show` (2).

## Where the plan was wrong about the code (followed the code, saying so)

1. **§4's `archived_to` / `deleted_hash` scalars cannot describe a bare-name
   delete.** The match rule is one-to-many by construction: an entry `foo`
   covers a top-level `foo` AND a categorized `bar/foo`, so one delete can
   archive two packages. The receipt therefore carries an `archived` ARRAY of
   `{slug, archived_to, deleted_hash}` as the truth, with the §4 scalars kept as
   the single-package convenience (first row, or `null`). Pinned by
   `test_a_bare_name_delete_covers_the_categorized_package_it_names`.

2. **§4's per-realm row `{realm_id, tombstoned, selection_pruned}` reads like
   two booleans; `selection_pruned` is a LIST.** A bool cannot say WHICH
   selection entry a bare-name tombstone took, and R-F prunes through the same
   one-to-many rule. Two additive keys ride beside them because the operator
   otherwise cannot tell a real write from a no-op: `refreshed` (the realm
   already carried this exact entry — the store dedupes by slug and refreshes
   `deleted_at`) and `inbox_pruned`.

3. **"Currently publishing the slug" (R-E) has to be answered the way
   `_skill_artifacts` answers it, and the two publish modes answer differently.**
   Mode `all` publishes whatever the canonical root HOLDS, so such a realm
   publishes the slug exactly when a local package is covered by it — otherwise a
   bare `skills delete ghost` would tombstone every mode-`all` realm on the box
   for a name nothing has. Mode `selected` names the slug explicitly, which is a
   standing statement about the NAME and holds with no local copy. With no local
   package the selection entry is only a name and cannot say whether `foo` and
   `cat/foo` are the same package, so that one arm accepts the match in either
   direction rather than silently missing a realm.

4. **The inbox mirror is UNLINKED, not archived — and that is not a breach of
   R-A.** The never-delete invariant protects AUTHORED content; the inbox is a
   byte-faithful cache of what the realm publishes, rebuilt from the subtree on
   every pull, and `_mirror_realm_skill_inbox` already unlinks a package the
   realm stopped publishing. The canonical (authored) copy is what gets archived.
   Without the prune the operator deletes a skill and `skills inbox` still offers
   it as promotable until the next pull — proven live below.

5. **`--realm` honors a realm that does not publish the slug (and an archived
   one).** The operator named it, and §3's ruling is that a tombstone records
   INTENT. The receipt is honest about it: a `skill_no_local_package` warning
   says the entry was written and nothing was archived, distinct from
   `skill_unknown`, which fires only when there is neither a copy here nor a
   realm publishing it and therefore nothing was written at all.

6. **`restore` reports `restored` from the ledger read BEFORE the write, and
   asks for the EXACT slug** — that is the entry `restore_skill` removes. A
   `skill_tombstoned` match is the wrong question here: it can return a
   bare-name entry that merely COVERS the slug. That case gets its own
   `skill_still_tombstoned` warning naming the blocking entry, because a receipt
   that said only "restored" while the package stayed blocked would be a lie of
   omission.

7. **The plan never mentions root observability, and this lane is that gate's
   own defect class.** `test_harness_json_root_observability.py` reddened on
   both new verbs, and it was right to: a delete resolved against the WRONG
   shared root finds no package, resolves no publishing realm, and reports a
   well-formed `skill_unknown` — the operator reads "already gone" from a verb
   that never looked in the right place. Same for `content_hint.archived:
   false` on restore. Both success envelopes now go through
   `attach_root_observability`; the ledger in that test explicitly forbids
   adding new verbs to its backlog, and it should not have been asked to.

## Mutation sanity

R-E's all-realms resolution flipped to first-realm-only (`[:1]` on the resolved
list) once:
`test_delete_with_no_realm_flag_hits_every_publishing_realm` reddened
(`['realm_mode-all_…'] != ['realm_mode-all_…', 'realm_selected-with_…']`),
24 others stayed green. Restored, 25 green.

## Regression floor

`test_realm_skill_tombstones.py`, `test_realm_sync*.py` (4 files),
`test_skill_promotion.py`, `tests/hermes_cli/` (4455 tests) — **4509 passed,
100 skipped, 1 xfailed, 5 failed** on the final run, and every red across both
runs was triaged:

| red | verdict |
|---|---|
| `test_error_exit_code_producers::test_the_kept_unspendable_baseline_still_describes_the_code` | PRE-EXISTING (`runtime_unavailable` now has a producer) |
| `test_completion::TestGenerateBash::test_valid_bash_syntax` | PRE-EXISTING |
| `test_xai_provider_labels::test_xai_oauth_provider_label_is_not_collapsed_to_api_key_label` | PRE-EXISTING (`xai` vs `xAI`) |
| `test_dashboard_admin_endpoints::TestPairingEndpoints::test_approve_pending_request_id` | xdist-parallel flake — passes alone, with S3 applied |
| `test_win_pty_bridge::TestWinPtyBridgeIO::test_write_sends_to_child_stdin` | xdist-parallel flake — passes alone, with S3 applied |
| `test_web_server_cron_profiles::test_fire_cron_job_scopes_store_and_runtime_home_together` | xdist-parallel flake — passes alone, with S3 applied |

The flake set is not stable: two runs of the identical command produced
`win_pty_bridge` on one and `web_server_cron_profiles` on the other, with the
three pre-existing rows constant across both. That instability IS the evidence —
a regression does not move between files run to run.

The three pre-existing rows were verified by stashing the ENTIRE S3 change and
re-running the same node ids at `32f41be19f`; all three fail identically there.
None is fixed here (the `runtime_unavailable` row says the launcher owns that
spelling — deleting it is a cross-stack call).

A sixth red WAS ours and is fixed, not fenced: see row 7 above
(`test_harness_json_root_observability`).

**Run the floor with `-n 8 --dist loadfile`.** `tests/hermes_cli` is 4455 tests
and takes ~50 minutes serially on this box; xdist brings it to **3 minutes**.
Two of the five reds above are the price (they flake under parallelism), and
re-running a red node alone is how you tell a flake from a regression.

## Live smoke (§6 S3 acceptance)

Scratch LOCAL realm (`server_id=None`) in a throwaway `HERMES_HOME` +
`HERMES_AGENT_RUNTIME_ROOT`, one bare git repo as the server, every verb in its
own process through the real `hermes harness …` argv
(`scratchpad/s3_smoke.py`). Receipts abridged to the load-bearing keys:

```
local realm  : realm_smoke-realm_0e6580 (server_id=None)
seeded skill : smoke-doomed (shared/skills/smoke-doomed)

$ realm sync publish --yes      → subtree: skills/smoke-doomed/SKILL.md
$ realm sync pull               → skill_sync converged, inbox mirrored
$ skills inbox --realm …        → 1 item: smoke-doomed (noop_identical)

$ skills delete smoke-doomed --dry-run                      (exit 0)
  dry_run: true, archived: [{smoke-doomed, archived_to: null, deleted_hash: f121b61e…}]
  realms:  [{realm_…, tombstoned: true, selection_pruned: [], inbox_pruned: []}]
  → canonical still present: True

$ skills delete smoke-doomed                                (exit 0)
  archived_to: ".archive/20260828T202852_780802Z/smoke-doomed"
  deleted_hash: "f121b61e207d02826ea10e39d70c63c6f4e4b299c383cf98bb90f2d2be72e63a"
  realms: [{realm_…, tombstoned: true, refreshed: false, inbox_pruned: ["smoke-doomed"]}]
  next:   "hermes harness realm sync publish realm_smoke-realm_0e6580 to propagate"

$ skills inbox --realm …        → count 0
$ realm skills show             → tombstones: [{slug: smoke-doomed,
                                    deleted_at: 2026-08-28T20:28:05…Z,
                                    deleted_hash: f121b61e…}]
$ realm sync publish --yes      → subtree NO LONGER carries skills/smoke-doomed/…

$ skills delete harness-runtime-model                       (exit 2)
  error.code: skill_installer_owned, hint names the constant + repo source
$ skills delete ghost-skill                                 (exit 0)
  realms: [], warnings: [skill_unknown]

$ skills restore smoke-doomed --realm …                     (exit 0)
  restored: true, tombstones: []
  content_hint: {archived: true, candidates: 1,
    path: "shared/skills/.archive/20260828T202852_780802Z/smoke-doomed",
    promote_command: "hermes harness skills promote smoke-doomed --from-path …"}
$ realm skills show             → tombstones: []
$ skills restore smoke-doomed   (again)                     (exit 0)
  restored: false, warnings: [skill_not_tombstoned]
```

Two notes on the transcript:

- **The publishes exit 7 (`sync_remote_unreachable`), and the proof still
  stands.** Pointing `sync_manifest_ref` straight at the bare repo makes the
  publish's working clone BE the server, so it commits and then finds no push
  remote. The commit lands, and `git ls-tree -r HEAD` is what the subtree lines
  above read. A smoke-harness shortcut, not a finding — S2's two-home round-trip
  already exercised the real push path.
- **The delete-propagating publish carries the four
  `CANONICAL_SHARED_SKILL_IDS`** that the intervening pull installed into the
  scratch shared root. Same pre-existing `install_harness_skills` ×
  `_skill_artifacts` interaction the S1/S2 notes filed, reproduced here — and
  precisely why R-B refuses to tombstone those ids.

## For the S5 canon fold

- The verb surface to document is five keys wide on delete
  (`realms[]`, `archived[]`, `archived_to`, `deleted_hash`, `next`) and the §4
  text should be corrected to the array-plus-scalar shape (row 1 above) rather
  than transcribed.
- Refusal codes `skill_slug_invalid` / `skill_installer_owned` are now part of
  the exit taxonomy at family 2; the canon's error-code table needs both rows.
- The **launcher seam is still unbuilt** and is now fully served: the sidecar,
  `realm_sync_status`, and `realm skills show` all carry the identical
  `{slug, deleted_at, deleted_hash}` rows, and `skills restore --json` is the
  restore affordance's exact call.
- Standing gap: nothing in S1–S3 fences the **LWW loss window** (R-D). A member
  who deletes and pulls before publishing loses the ledger to the older
  snapshot. The delete receipt's `next` keeps the window short by naming the
  publish; S4 is the real fix if it ever bites.
