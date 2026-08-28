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
