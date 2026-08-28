# Planned — Skill deletion propagation for realm sync

**Owner domain:** system architecture ([01-system-architecture.md](../01-system-architecture.md)),
with the pull/publish mechanics belonging to the realm-sync material referenced
from it.
**Status:** surveyed + staged 2026-08-28. **GATED on rulings R-A…R-G below.**
Stages S1–S3 are buildable the moment the rulings land; S4 is optional
hardening behind R-D.
**Trigger:** on 2026-08-28 the operator deleted 7 skills and renamed 1
(hermes `2eb9d93cd1`) and the only fences against resurrection were the
canonical constant and "hope nobody pulls first." Workspaces already have a
first-class delete (`deleted_workspace_ids` + `_apply_workspace_tombstones`);
skills do not.

---

## 1. Survey findings (measured 2026-08-28, all against HEAD `b20fa8daf9`)

### 1.1 Why a locally deleted skill resurrects today

- `pull_realm_sync` (`agent_runtime/realm_sync.py:389`) runs
  `apply_skill_inbox_pull(realm, subtree)` (`:439`), which mirrors
  `subtree/skills/**` into the resolver-invisible per-realm inbox
  (`shared/skills/.realm_inbox/<realm-token>/…`,
  `agent_runtime/skill_promotion.py:156-161`) and then classifies each package
  through `classify_promotion` (`skill_promotion.py:233`). A package with **no
  canonical copy** classifies `promote_new` and is **auto-adopted**
  (`realm_sync.py:2123-2131`, provenance
  `source={"kind": "realm", "realm_id": …}`). So: delete a skill locally,
  pull, and the realm's still-published copy walks straight back in through
  the front door.
- The same pull then **reinstalls the entire canonical set from repo source**:
  `install_harness_skills(skills=sorted(HARNESS_SKILLS))` +
  `install_harness_skills_for_personas(...)` (`realm_sync.py:470-473`).
  `HARNESS_SKILLS = CANONICAL_SHARED_SKILL_IDS` (`agent_runtime/skill_install.py:12`),
  the frozenset at `hermes_constants.py:19-27` (currently 5 ids:
  `harness-dev-delivery`, `harness-continuity`, `harness-qa-verdict`,
  `harness-runtime-model`, `harness-charsheet-authoring`). For THOSE ids the
  delete lane is the constant + `docs/agent-runtime-harness/harness-skills/`
  repo source (exactly what `2eb9d93cd1` did) — no realm ledger can win an
  argument with an installer that re-copies from the repo on every pull.
- Publish IS a full-subtree replace: `publish_realm_sync` does
  `shutil.rmtree(subtree)` + rewrite (`realm_sync.py:293-300`), and
  `_skill_artifacts` (`:787`) walks the live canonical root — so a publish
  from the machine that deleted the skill DOES prune the server copy. But
  every member who pulled before that publish holds a live adopted canonical
  copy, and **their** next publish re-walks it into the subtree:
  resurrection. The only standing fence is `sync_behind` — publish refuses
  when `git["behind"] > 0` (`realm_sync.py:249-250`) — which forces a stale
  member to pull first, but today the pull gives them nothing that removes
  their local copy. (The inbox mirror prunes packages gone from the subtree —
  `removed` in `_mirror_realm_skill_inbox`, `realm_sync.py:2215-2217` — but
  that only cleans the *mirror*; the already-adopted canonical copy is
  untouched.)

### 1.2 The workspace precedent (the shape we are lifting)

- Ledger: `Realm.deleted_workspace_ids: list[str]`
  (`agent_runtime/models.py:117-122`), bounded by
  `DELETED_WORKSPACE_LEDGER_CAP = 500` (`agent_runtime/store.py:24-28`),
  written at the ONE delete chokepoint `WorkspaceStore.delete`
  (`store.py:304`, ledger append at `:378-380`, which also removes the id
  from `realm.workspace_ids` at `:377`).
- Pull enforcement: `_apply_workspace_tombstones` (`realm_sync.py:542-590`) —
  hard-delete through the store chokepoint, **degrade to archive** when
  `WorkspaceDeleteBlocked` says live-store evidence would be destroyed
  (`:569-575`), typed warnings, never silent, never aborts the pull.
- Publish enforcement: `_workspaces_for_realm` subtracts the ledger
  (`realm_sync.py:705-708`) — defense-in-depth against a publish racing its
  pull.
- Un-tombstone precedent: a deliberate re-adopt REMOVES the id from the
  ledger at the adopting chokepoint (`agent_runtime/default_scope.py:133-139`;
  same idiom at `:516-519`).
- Workspace ids are freshly minted (near-)unique, so a bare-id ledger never
  blocks a legitimate re-creation. **Skill slugs are re-creatable names** —
  that is the one place the lift cannot be verbatim (see §2.2).

### 1.3 The skills lane's own invariants (which the delete must not break)

- "**Never delete — archive.**" Displaced canonical packages move to
  `shared/skills/.archive/<UTC ts>/<slug-flattened>/`
  (`skill_promotion.py:17-19`, `_archive_package` `:390-395`); `.archive` is
  resolver-invisible (in `EXCLUDED_SKILL_DIRS`) and publish-invisible
  (dot-dirs skipped, `realm_sync.py:836-838`). The installer uses the same
  convention (`skill_install.py:19`, `_archive_replaced_package` `:137`).
- One guarded promotion door: `classify_promotion` / `execute_promotion`
  (`skill_promotion.py`), slug validation `_validate_slug` (`:179-215`),
  occupancy + TOCTOU guards, installer-ownership refusal with typed
  `reason_code` (`:513-525` via `skill_publishability.promotion_refusal`).
- Held-divergent resolution is an explicit operator verb:
  `hermes harness skills promote --adopt-divergent`
  (`hermes_cli/harness.py:759-772`); inbox listing is
  `hermes harness skills inbox` (`:751-757`).
- Per-realm publish selection: `Realm.skill_publish_mode` / `skill_selection`
  (`models.py:123-132` — "realm truth, converged **last-publisher-wins**"),
  written only at `RealmStore.set_skill_selection` (`store.py:554-593`),
  CLI `hermes harness realm skills show|set` (`harness.py:615-630`), envelope
  `realm_skill_selection/v1` (`harness.py:3033`).

### 1.4 Realm-record mechanics that constrain the design

- The realm JSON itself travels as a sync artifact
  (`store/realms/<token>.json`, `realm_sync.py:911-917`) and is pulled
  through the generic overwrite loop with ONE merge rule:
  `_pulled_artifact_bytes` (`realm_sync.py:1516-1546`) preserves the eight
  backend-authority fields (`id`, `name`, `slug`, `server_id`,
  `default_workspace_*`, `sync_manifest_ref`) from the local copy and takes
  **everything else wholesale from the incoming snapshot** — including
  `deleted_workspace_ids` and `skill_selection`. Consequence measured, not
  guessed: a member who mutates realm truth locally and pulls **before**
  publishing has that mutation overwritten by the older snapshot. This is a
  live (pre-existing) loss window for workspace tombstones too; for skills it
  is R-D's subject.
- Serde is strict on version, lax on fields: `serde.upgrade` **raises** on
  `schema_version != 1` (`agent_runtime/serde.py:38-45`), while `_coerce`
  silently drops unknown keys when loading a dataclass (`serde.py:97-104`)
  and `to_jsonable` emits only declared fields (`:15-17`). So: bumping the
  realm's `schema_version` bricks every old member's realm load; adding a
  field is ignored gracefully on load — but an old member's next local realm
  **save** strips the field from their file, and their next publish then
  publishes a realm JSON without the ledger. §5 owns this.

### 1.5 Backend (`X:\Unreal Engine\Engine\EterniaBackend\eternia-backend\realms`)

- The Django `Realm` model (`realms/models.py:28-79`) owns: server FK, slug,
  name, grants (`grant_on_join`, `granted_role_ids`, `publisher_role_ids`),
  the default-workspace pointer triple, `sync_repo_ref`, soft-delete
  (`deleted_at`). Its module docstring is explicit: "**The backend never
  stores artifact contents**" (`models.py:6-10`); membership is derived, no
  RealmMember table (`:15-19`).
- Routes (`realms/routes.py`): realm CRUD, git-host binding
  (Forgejo/GitHub, `ServerGitHost` `models.py:82-137` — "a **location**, not
  a permission"), `sync_permission` (`:399`), `sync_credential` (`:430`), and
  `sync_published` (`:446`) whose docstring is "**counts only, never
  contents**" (`:447-448`) — it fans a `realm.sync.published` notify out on
  `realm:{id}` with a commit id and artifact counts.
- **Git-host provider seam** (Stage 43, backend
  `docs/nexus/STAGE_43_GIT_HOST_PROVIDER.md`): `realms/git_host.py` defines
  the `GitHostProvider` protocol (`git_host.py:90-146`) with exactly four
  methods — `ensure_server_repo`, `mint_scoped_token`, `clone_url_for`,
  `git_authorization` — behind per-server selection in `build_provider_for`
  (`:172-189`; a `ServerGitHost` binding of kind `github` selects
  `realms/github.py`, everything else defaults to `realms/forgejo.py`). The
  BYO GitHub lane (the one in production use) is App-brokered: an Eternia
  GitHub App mints **repo-scoped, natively expiring installation tokens**
  (`git_host.py:13-15`, `:70-72`); "Django remains the single membership
  authority… `broker_credential` remains the only mint chokepoint. A
  `ServerGitHost` row grants nothing" (`git_host.py:21-28`); members are
  never GitHub collaborators (`:29-36`). `sync_permission`
  (`services.py:220-227`) is "the one decision function";
  `broker_credential` (`services.py:690`) mints the contract-frozen
  credential JSON.
- **Conclusion (1): the skill-tombstone ledger needs NO backend and NO
  git-host involvement.** It is realm-record content, and the realm record is
  hermes-side truth that already rides the sync repo as bytes
  (`store/realms/<token>.json`, §1.4) through whichever provider hosts the
  repo (production: `sync_manifest_ref:
  https://github.com/nekwo/testtest.git`). Every `GitHostProvider` method is
  about repo location and credential minting — none inspects content, so the
  ledger passes through both lanes unexamined by construction. No backend
  column, no endpoint, no provider change. (If the launcher later wants a
  push-notified "skill deleted" toast, the existing `sync_published` notify
  already fires on the propagating publish — the seam exists; nothing new.)
- **Conclusion (2): if server-side enforcement is ever wanted** (refusing a
  publish that resurrects a tombstoned slug), **the git host is the wrong
  chokepoint and the credential broker cannot be one.** A publish token is
  just a repo-scoped git *write* token (`mint_scoped_token(scope=WRITE)`) —
  minted before any bytes exist, it can gate WHO writes, never WHAT is
  written. GitHub.com offers no pre-receive hook for App-brokered pushes, so
  the BYO lane structurally cannot veto content at push time; Forgejo (Eternia
  infra) could host a pre-receive hook, but a fence that exists on one lane
  and not the other is a fence nobody may rely on. Server-side enforcement
  would therefore need a NEW backend chokepoint — e.g. upgrading
  `sync_published` (`routes.py:446`) from a post-hoc counts-only notify into
  a verdict that fetches and inspects the pushed commit, or a backend sweeper
  that reverts violating commits — both post-hoc, both new machinery, both
  requiring the backend to start reading artifact contents it deliberately
  never stores. **Not recommended and not scoped**: the hermes-side chain
  (`sync_behind` refusal → pull applies ledger → publish filter, §2.5) is the
  enforcement, and every writer is a hermes running this code.
- **Related-but-separate row (NOT scoped here):** local (non-server-bound)
  realms cannot use the GitHub App lane at all today — filed as
  **REALM-GITHOST-LOCAL-1** in the backend brain Backlog 2026-08-28. This
  plan only guarantees the tombstone design never assumes a server exists
  (§5, local-realm note).

### 1.6 Test terrain

- `tests/agent_runtime/test_realm_sync_skill_inbox.py` — the skill-pull suite;
  fixtures `_local_realm`, `_write_subtree_skill`, `_seed_canonical`,
  `_snapshot` (`:55-162`) are directly reusable; existing cases cover mirror
  prune (`:164`), auto-adopt (`:197`), converge (`:220`), hold (`:263`),
  occupancy refusals (`:357`, `:388`), reserved names (`:419`), dry-run
  (`:456`).
- `tests/agent_runtime/test_skill_promotion.py`,
  `test_skill_publishability.py`, `test_realm_sync.py`,
  `test_workspace_lifecycle.py` (workspace-tombstone behavior),
  `tests/hermes_cli/test_skills_subparser.py` (CLI parser shape precedent).

---

## 2. Design

### 2.1 The ledger

New field on `Realm` (`agent_runtime/models.py`, after
`deleted_workspace_ids`):

```python
@dataclass(slots=True)
class SkillTombstone:
    slug: str                 # validated canonical slug (bare or <cat>/<name>)
    deleted_at: datetime      # when the tombstone was minted
    deleted_hash: str | None  # skill_package_content_hash at delete time, when a
                              # local package existed to hash — evidence, not authority

# on Realm:
    skill_tombstones: list[SkillTombstone] = field(default_factory=list)
```

- Name: `skill_tombstones`, not `deleted_skill_ids` — the workspace ledger's
  name says "ids" because its entries ARE bare unique ids; these entries are
  records precisely because slugs are re-creatable (§1.2). Nested dataclass
  round-trips through `serde._coerce` / `to_jsonable` with zero new serde
  code (`serde.py:97-104`, `:15-17`).
- Bounded: `SKILL_TOMBSTONE_LEDGER_CAP = 200` beside
  `DELETED_WORKSPACE_LEDGER_CAP` (`store.py:24-28`), oldest-first eviction at
  the write chokepoint, same argument (by eviction time every member has long
  since pulled).
- `schema_version` stays **1** (§1.4: a bump bricks old members). The field
  is additive; old-member behavior is a documented constraint (§5).

### 2.2 Re-add semantics — tombstone vs a future same-name skill

The workspace ledger never faces this (unique ids). For slugs, the house
style answers it: **discriminable refusals and explicit resurrection doors**
— the office delete lane's JSON-RPC 4090 `actor_archived` fence with the
`resurrect=True` door (`agent_runtime/office_store.py:757`, `:1470-1505`:
"drop the tombstone… (`harness office actor-restore`, or `--resurrect`,
re-adds it)"), and the workspace ledger's removal-at-the-readopting-chokepoint
(`default_scope.py:133-139`).

So (pending R-C):

- A tombstone blocks the slug **absolutely** until explicitly lifted. No
  hash/timestamp auto-supersede: `deleted_hash` is carried for receipts and
  forensics ("the thing you are restoring is/isn't the bytes you deleted"),
  never consulted to auto-admit. An auto-supersede would mean any member
  authoring a same-name skill silently overrides a realm-wide delete — the
  exact ambiguity ("gone means gone… but does it?") the house style refuses.
- The door is a verb: `hermes harness skills restore <slug> --realm <id>`
  removes the ledger entry (and only that — restoring *content* is the
  existing `skills promote --from-path shared/skills/.archive/<ts>/<slug>`
  lane, or a fresh publish from a member who has it). Every refusal the
  tombstone causes names this verb in its message, the way the 4090 fence
  names `actor-restore`.

### 2.3 Write chokepoints (RealmStore)

Two new methods beside `set_skill_selection` (`store.py:554`), same
discipline (validate → mutate → `save(emit_event=False)` →
`_append_store_event("realm.updated", change=…)`, `dry_run` returns the
would-be realm without saving):

- `RealmStore.tombstone_skill(realm_id, slug, *, deleted_hash=None, dry_run=False) -> Realm`
  - Slug validation: reuse `skill_promotion._validate_slug` semantics (shape
    only — promote it to a shared helper or call it; do NOT re-spell the
    alphabet). Refusals raise typed errors (an `errors.py` exception carrying
    a `code`, matching `WorkspaceDeleteBlocked`'s style):
    - `skill_installer_owned` — slug ∈ `CANONICAL_SHARED_SKILL_IDS`
      (`hermes_constants.py:19`). Message points at the constant + repo
      source as the delete lane for those (the `2eb9d93cd1` lane). This is
      R-B; without it the tombstone and `install_harness_skills`
      (`realm_sync.py:470-473`) fight forever, the installer winning every
      pull.
    - `skill_slug_invalid` — shape refusal, `_validate_slug`'s reason string.
  - Dedupes by slug (re-tombstoning refreshes `deleted_at`), appends, trims
    to cap.
  - Also removes the slug from `realm.skill_selection` (workspace precedent:
    delete prunes membership at the same write, `store.py:377-380`) — R-F.
  - Event: `change="skill_tombstoned"`, plus `slug`.
- `RealmStore.restore_skill(realm_id, slug, dry_run=False) -> Realm` —
  removes the entry (no error if absent: idempotent, reports `restored:
  false`); event `change="skill_tombstone_restored"`.

Helper used by every enforcement point (ONE spelling of the match rule):

```python
def skill_tombstoned(realm: Realm, slug: str) -> SkillTombstone | None
```

Match is on the exact slug **and**, for a categorized `<cat>/<child>` slug,
the bare child name — mirroring `_skill_slug_selected`
(`realm_sync.py:855-863`), because the selection and the tombstone must
agree about what a name means or a slug can be simultaneously "selected" and
"not the thing that was deleted."

### 2.4 Enforcement point 1 — pull

New `_apply_skill_tombstones(realm) -> dict` in `realm_sync.py`, called from
`pull_realm_sync` immediately **after** the realm JSON overwrite loop has run
and **before/with** `apply_skill_inbox_pull` (the realm re-read gives us the
freshly pulled ledger, exactly like `_apply_workspace_tombstones`'s re-read
comment at `:553`). Two halves:

1. **Inbox half (inside `apply_skill_inbox_pull`).** Pass the ledger into
   the mirror: `_mirror_realm_skill_inbox` drops any top-level package whose
   slug is tombstoned from its `desired` set (`realm_sync.py:2202-2208`), so
   a stale publish's copy is pruned from the mirror exactly like a `removed`
   package, and the promotion loop never sees it — **a tombstoned slug
   arriving in the inbox can never auto-adopt**. Reported in a new
   `SkillSyncSummary.tombstoned: list[str]` field (`:2033-2062`; additive
   key in `as_dict()` — the launcher's realm-sync sheet is absent-tolerant
   per the `store_drift` precedent, `:222-227`).
2. **Canonical half (`_apply_skill_tombstones` proper).** For each ledger
   entry whose canonical dir exists
   (`skill_promotion._canonical_dir_for(slug)` has a `SKILL.md`):
   - **Archive, never rmtree** (R-A): `skill_promotion._archive_package`
     moves it to `.archive/<UTC ts>/<slug>/` — the skills lane's own
     never-delete invariant (§1.3), which is *stronger* than the workspace
     lane's hard-delete-with-archive-degrade because a skill package IS
     operator/agent-authored content, i.e. always "evidence." This also makes
     restore-with-content a first-class two-step (un-tombstone + promote from
     archive) instead of a data-recovery incident.
   - Skip + warn (never archive) when the slug is installer-owned
     (`CANONICAL_SHARED_SKILL_IDS`) — belt to R-B's suspenders, for a ledger
     written by a newer/older peer that didn't enforce it.
   - Per-slug isolation: one failed archive is a typed warning row, never an
     aborted pull (the `_apply_workspace_tombstones` posture, `:569-589`).
   - Summary `{"archived": [...], "skipped_installer_owned": [...],
     "warnings": [...]}` rides the pull result as `skill_tombstones` (same
     conditional-emission style as `workspace_tombstones`, `:481-482`), and
     flips `changed` when non-empty.
   - The `.provenance/<slug>.json` sidecar is left in place (it already
     records the last promotion; a future promotion overwrites it —
     `_write_provenance`, `skill_promotion.py:432-450`). Note in the code
     comment: provenance is history, not liveness.

Ordering note for the builder: run the canonical half AFTER
`apply_skill_inbox_pull` in `pull_realm_sync` (mirror pruning is independent;
the archive step must not race the promotion loop's occupancy guard), and
BEFORE `install_harness_skills` (which is a no-op for tombstonable slugs by
R-B anyway, but keep the order legible).

### 2.5 Enforcement point 2 — publish

- `_skill_artifacts` (`realm_sync.py:815-820`) gains one line beside the
  selection filter: `if skill_tombstoned(realm, slug): continue` — the
  workspace `workspace_ids -= set(realm.deleted_workspace_ids)` idiom
  (`:705-708`). Defense-in-depth: normally the canonical copy is already
  archived by the pull, but a copy re-materialized out-of-band (a stray
  `promote --from-path`, a manual copy) must not ride the next publish.
- The stale-member resurrection race is closed by the existing fence chain,
  now made *effective*: stale member publishes → `sync_behind` refusal
  (`:249-250`) → they pull → the pull hands them the ledger (realm JSON) →
  `_apply_skill_tombstones` archives their copy → their retried publish
  neither contains the skill nor could include it (filter above). Write this
  chain into the `_skill_artifacts` comment — it is the argument for why no
  server-side hook is needed.
- Restore direction: after `restore_skill`, the very next publish from a
  member who still *has* the content re-publishes it (mode/selection
  permitting) and every peer re-adopts through the normal `promote_new`
  door. No new machinery.

### 2.6 Enforcement point 3 — status/receipts

- `realm_sync_status` (`:171`) and the sidecar (`_write_sync_sidecar`)
  additively gain `skill_tombstones: [{slug, deleted_at, deleted_hash}]`
  (sorted by slug). This is the launcher seam (§4): the realm-sync sheet
  already consumes this envelope absent-tolerantly.

---

## 3. Rulings — ADOPTED 2026-08-28 (operator delegated to the recommendations)

All seven rulings below are ADOPTED as recommended (operator order 2026-08-28:
"make it implementation ready and have opus sub agents implement it"). S4
stays gated OFF (R-D = LWW v1). The §4 open sub-question is also decided:
`skill_unknown` is a **warning, not a refusal** — a tombstone records intent,
and intent is valid even when no copy currently exists on this machine.

Two operator challenges from the same review, answered here so the rationale
travels with the plan:

- **"Pushes can delete natively."** True for the repo bytes, and the design
  leans on exactly that — publish is a full-subtree replace, so the deleter's
  push removes the file and every member's pull removes it from their clone.
  What git cannot reach is the ADOPTED copy in each member's canonical skills
  root (not a git file), and that copy rides the member's next publish right
  back into the realm. The ledger is the instruction git absence cannot carry.
- **"Why tombstones, why not just delete it?"** The bytes ARE deleted —
  everywhere. The tombstone is a one-line intent record distinguishing
  *deleted everywhere* from *merely unpublished here*: without it, narrowing a
  realm's `skill_selection` would be indistinguishable from a delete and would
  destroy members' local copies that were never meant to die. Same house
  pattern as the office lane's `actor_archived` fence + explicit `--resurrect`
  door — gone means gone, and STAYS gone, without collateral deletes.

| # | Question | Ruling (adopted as recommended) |
|---|---|---|
| R-A | Pull posture for the local canonical copy: archive vs hard-delete? | **Archive** to `.archive/<ts>/<slug>` — the skills lane's own standing invariant (`skill_promotion.py:17-19`); disk cost is bounded and the operator already accepted the archive-never-delete ruling for diff artifacts. |
| R-B | Can a `CANONICAL_SHARED_SKILL_IDS` id be tombstoned? | **No — typed refusal** `skill_installer_owned` pointing at the constant + `docs/agent-runtime-harness/harness-skills/` (the `2eb9d93cd1` lane). Anything else is a fight with `realm_sync.py:470-473` that the installer wins every pull. |
| R-C | Re-add semantics: explicit un-tombstone verb, or hash/timestamp auto-supersede? | **Explicit verb only** (`skills restore`); `deleted_hash` is receipt evidence, never an authority. Matches the office 4090 fence + `--resurrect` door. |
| R-D | Ledger merge on pull: accept whole-field last-publisher-wins (the `skill_selection` precedent, and what `_pulled_artifact_bytes` does today), or per-slug newest-`deleted_at`-wins state merge? | **LWW for v1** (S1–S3); it inherits a narrow, pre-existing loss window (tombstone locally → pull before publishing → older snapshot overwrites the ledger — the same window `deleted_workspace_ids` already has, §1.4). S4 offers the per-slug merge if the window ever bites. Delete verb receipts say "publish to propagate" to keep the window short. |
| R-E | `skills delete <slug>` with no `--realm`: tombstone every non-archived realm that currently publishes the slug, or require the flag? | **Default = all publishing realms** (one canonical root serves all realms — a copy deleted locally is deleted for every realm that published it; leaving one realm un-tombstoned would resurrect the local copy on that realm's next pull). `--realm` narrows; receipt lists every realm written. |
| R-F | Does tombstoning prune the slug from `realm.skill_selection`? | **Yes** — workspace precedent (`store.py:377-380`); a selection naming a tombstoned slug is a standing contradiction. Restore does NOT re-add it (selection is a separate, deliberate act via `realm skills set`). |
| R-G | Backend involvement? | **None** (§1.5 evidence: "backend never stores artifact contents", `sync_published` is "counts only", and the git-host seam mints location-scoped credentials that cannot inspect content). Ledger lives in the hermes realm record and travels through the sync repo on both git-host lanes unchanged. Server-side resurrection-refusal would need a new post-hoc backend chokepoint (§1.5 conclusion 2) — recommend declining it. |

---

## 4. Operator surface

Under the existing `hermes harness skills` group (`harness.py:751+`), Stage-42
envelope discipline (`--json`, `--dry-run` where marked):

- `hermes harness skills delete <slug> [--realm <id> ...] [--json] [--dry-run]`
  1. resolve target realms (R-E), 2. `RealmStore.tombstone_skill` per realm,
  3. archive the local canonical package (once — shared root) via
  `_archive_package`, 4. prune the slug from each target realm's inbox mirror,
  5. receipt: `{"schema_version": 1, "kind": "skill_delete", "skill": slug,
  "realms": [{realm_id, tombstoned, selection_pruned}], "archived_to": path|null,
  "deleted_hash": …, "next": "hermes harness realm sync publish <realm> to propagate"}`.
  Refusals exit non-zero with the typed code in the envelope
  (`skill_installer_owned`, `skill_slug_invalid`, `skill_unknown` when neither
  a canonical copy nor a realm publishing it exists — the last is a warning,
  not a refusal, if a tombstone is still wanted; decide in review).
- `hermes harness skills restore <slug> --realm <id> [--json]` — un-tombstone
  only; envelope `kind: "skill_restore"`, includes `content_hint`: whether a
  matching package sits in `.archive` (path if so) and the promote command to
  re-admit its bytes.
- `hermes harness realm skills show` (existing, `harness.py:617`) — envelope
  gains additive `tombstones` array.
- **Launcher seam (out of scope to build, named here):** the realm-sync sheet
  reads `realm_sync_status` / the sidecar; the additive `skill_tombstones`
  field is all it needs for a "deleted from realm" row with a restore
  affordance; the propagating publish already fires the backend
  `sync_published` notify (`routes.py:446`) for a toast.

---

## 5. Migration / compat

- **No `schema_version` bump** (serde refuses ≠1, §1.4). `skill_tombstones`
  is additive; old members load a pulled realm JSON fine (unknown key
  dropped, `serde.py:101-103`).
- **Documented constraint:** an old member (code without the field) strips
  the ledger from its local file on its next realm save, and — being the
  last publisher — publishes a realm JSON without it (LWW). Delete
  propagation is therefore only guaranteed once **every member runs ≥ S1
  code**. This is precisely the constraint `deleted_workspace_ids` shipped
  under; the member fleet is the operator's own machines. State it in the
  delete verb's receipt docs; do not build a version fence for it.
- The pre-existing pull-before-publish LWW loss window (R-D / §1.4) applies
  to `deleted_workspace_ids` as much as to the new ledger; if S4 is ruled in,
  fix both in the same union pass — until then it is a known, filed edge.
- **Local realms (no `server_id`) honor tombstones identically — verify,
  don't assume.** Nothing in the design touches `realm.server_id`: the
  ledger lives on the realm dataclass, the store chokepoints don't consult
  binding, `pull_realm_sync` already runs against a remoteless repo
  (`_has_remote` guard, `realm_sync.py:402`), and `_apply_skill_tombstones`
  reads only the local realm record. One asymmetry to carry into S2's tests:
  `_pulled_artifact_bytes`'s authority-field merge only runs when
  `realm.server_id` is set (`realm_sync.py:1524`), so a LOCAL realm's pulled
  JSON overwrites wholesale — same LWW posture, one fewer guard. The local
  `default` realm must pass the S2 suite with `server_id=None` (the
  `_local_realm` fixture in `test_realm_sync_skill_inbox.py:67` already
  builds exactly that). The GitHub-App-for-local-realms gap is
  REALM-GITHOST-LOCAL-1 (§1.5) and stays out of scope.
- Nothing changes for realms that never publish skills
  (`skill_publish_mode`/selection untouched except via R-F).

---

## 6. Stages

Every stage lands independently, hermes-repo only (R-G: no backend stage
exists). Tests run with the project venv's pytest; the realm-sync suites
(`tests/agent_runtime/test_realm_sync*.py`, `test_skill_promotion.py`) are
the regression floor for every stage.

### S1 — Ledger + store chokepoints (model layer)

**Touches:** `agent_runtime/models.py`, `agent_runtime/store.py`,
`agent_runtime/errors.py` (typed refusals),
`agent_runtime/decision_contract_registry.py` if the `realm.updated` change
codes are enumerated there (check: `skill_selection` uses `realm.updated`
with a `change` field — follow whatever it did).
**Builds:** `SkillTombstone`, `Realm.skill_tombstones`,
`SKILL_TOMBSTONE_LEDGER_CAP`, `RealmStore.tombstone_skill` /
`restore_skill`, the shared `skill_tombstoned(realm, slug)` matcher
(categorized-child rule §2.3), slug-validation reuse.
**Tests (new `tests/agent_runtime/test_realm_skill_tombstones.py`):**
mint/dedupe/cap-trim; `skill_installer_owned` and `skill_slug_invalid`
refusals; selection pruning (R-F); restore idempotence; serde round-trip of
the nested dataclass; old-member simulation — load a realm JSON carrying the
field with a `Realm` copy lacking it is not writable in-tree, so instead
assert the documented halves: unknown-key drop on load and
declared-fields-only on save.
**Acceptance:** suite green; `python -c` smoke minting a tombstone in a temp
HERMES_HOME and reading it back.

### S2 — Pull + publish enforcement (sync layer)

**Touches:** `agent_runtime/realm_sync.py` only.
**Builds:** `SkillSyncSummary.tombstoned` + mirror filtering (§2.4.1);
`_apply_skill_tombstones` canonical-archive half with per-slug isolation,
installer-owned skip, warnings, result-row + `changed` wiring (§2.4.2);
`_skill_artifacts` tombstone filter (§2.5); status/sidecar additive field
(§2.6).
**Tests (extend `test_realm_sync_skill_inbox.py` with its fixtures):**
(a) tombstoned slug present in subtree → not mirrored, not adopted,
reported `tombstoned`, pull completes; (b) existing local canonical copy +
pulled ledger → archived to `.archive`, resolver no longer sees it
(`resolve_skill` miss), pull `changed=true`; (c) publish from a member with a
live copy + ledger → artifact set excludes the slug (the `_snapshot`
byte-compare style of `test_publish_excludes_inbox_and_provenance`, `:301`);
(d) installer-owned slug in a ledger → skipped with warning, package intact;
(e) restore → next pull re-adopts a republished package via `promote_new`;
(f) categorized `<cat>/<child>` tombstone matches by child name (mirror of
`test_publish_categorized_package_and_selected_matching`, `:319`); (g) dry-run
pull still writes nothing (extend `:456`).
**Acceptance:** full `test_realm_sync*` + `test_skill_promotion` files green;
two-HERMES_HOME round-trip script (member A deletes+publishes, member B pulls
→ B's canonical copy archived; B publishes → server subtree still lacks the
slug) run once and its transcript kept with the stage notes.

### S3 — Operator verbs + receipts (CLI layer)

**Touches:** `hermes_cli/harness.py` (+ the harness_parts module the skills
commands live in, if split), docs for the verb.
**Builds:** `skills delete` / `skills restore` per §4, including the local
archive step, multi-realm resolution (R-E), inbox-mirror prune, envelopes,
exit codes, `--dry-run` on delete.
**Tests:** parser shape (`tests/hermes_cli/test_skills_subparser.py`
precedent); envelope snapshot tests for delete (single-realm, all-realms,
installer-owned refusal, unknown slug) and restore (present/absent entry,
archive `content_hint`).
**Acceptance:** live smoke on the real profile against a scratch local realm:
delete a throwaway skill, `realm skills show` lists the tombstone, publish,
restore, receipts captured.

### S4 — Ledger merge hardening (OPTIONAL, gated on R-D ruling "union")

**Touches:** `agent_runtime/realm_sync.py::_pulled_artifact_bytes`.
**Builds:** per-slug newest-timestamp-wins merge of `skill_tombstones`
(local ∪ incoming; a restore must be representable, so this stage ALSO
changes restore to write a `restored_at` marker instead of removing the
entry — entries become a tiny per-slug state register, and the cap prunes
settled history). Apply the same union to `deleted_workspace_ids` (plain
set-union is safe there — unique ids, no restore verb) in the same commit or
file it as its own row.
**Tests:** merge matrix (local-only, incoming-only, both with differing
timestamps, restore-vs-stale-delete), plus the S2 suite unchanged.
**Do not build ahead of the ruling** — the v1 LWW posture is coherent on its
own and matches `skill_selection`.

### S5 — Canon fold

When S1–S3 are live-verified, fold the shipped truth into the owning domain
doc (realm sync's home under [01-system-architecture.md](../01-system-architecture.md))
per the index's plan-graduation rule, and retire this file.
