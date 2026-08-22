# Realm Skill Inbox + Hash-Guarded Promotion — Implementation Design (2026-07-24)

Status: **SHIPPED** (design implementation-ready 2026-07-24, operator-approved
by Tony; landed in full plus two follow-on slices). `classify_promotion` /
`execute_promotion` / `list_inbox_packages` are in
`agent_runtime/skill_promotion.py`, `apply_skill_inbox_pull` in
`agent_runtime/realm_sync.py`, and `skills inbox|promote|publishable` are
registered CLI verbs. **Read the Problem section and the "do not re-derive"
fact block below as a 2026-07-24 snapshot, not as current state** — this
design is what changed both.

## Problem

Skills currently enter the live namespace through unguarded lanes:

1. `pull_realm_sync` writes incoming realm `skills/…` artifacts **directly into
   the canonical shared root** (`_destination_for_sync_path`,
   `agent_runtime/realm_sync.py:884`) — a realm pull can silently overwrite the
   local canonical copy of a same-named skill.
2. Nothing prevents the same skill id existing in two resolver roots. The
   resolver (`agent/skill_utils.py resolve_skill/resolve_skills`) correctly
   refuses to pick a winner → `skill_collision` readiness failures. The
   2026-07-24 incident (dev/qa amber dots, launcher skills-editor "unresolved"
   drag-block) was exactly this class: legacy per-profile seed copies vs the
   shared root.
3. `_skill_artifacts` (realm_sync.py:536) publishes only **top-level** dirs of
   the shared root with a `SKILL.md`. A categorized skill such as
   `software-development/hermes-agent` (selected BY PATH by personas) never
   publishes to realms at all.

## Target model (operator's design, refined)

- **One live namespace**: the canonical shared root
  (`hermes_constants.get_shared_skills_dir()`, `<hermes_root>/shared/skills`).
  Publish ships it whole (existing per-realm selection filter).
- **Downloads land in a per-realm inbox**, resolver-invisible:
  `shared/skills/.realm_inbox/<realm-token>/<skill-slug>/…` — a byte-faithful
  mirror of that realm's current skill packages. Quarantine + provenance, never
  live.
- **One guarded door**: promotion. The only way content becomes canonical:
  - pull-time **auto-promote** for packages with **no canonical copy** (keeps
    today's "pull gives you the realm's skills" UX; strictly safer than today
    because *existing* canonical skills can no longer be silently overwritten);
  - pull-time **converged** no-op when content hashes match;
  - pull-time **hold** when hashes diverge — canonical untouched, surfaced as
    drift, operator resolves explicitly;
  - explicit `hermes harness skills promote` for held/authored/profile-local
    copies, with a content-hash guard.
- **Re-share to another realm**: once canonical, the existing per-realm publish
  selection carries it. A provenance sidecar records origin realm + hash.

## Established codebase facts (verified 2026-07-24, do not re-derive)

- `EXCLUDED_SKILL_DIRS` (`agent/skill_utils.py:65`) is an **explicit**
  frozenset (`.git`, `.hub`, `.archive`, …) — NOT a generic dot-prune.
  `iter_skill_index_files` prunes via this set; direct lookups (`root / name`)
  can never enter a dotted dir because slugs never start with `.`.
- Publish (`_skill_artifacts`) skips top-level dotdirs (`skill.startswith(".")`)
  and refuses to publish any skill whose id does not resolve uniquely from
  `shared_core` (`skill_authority_conflict`). So `.realm_inbox`/`.provenance`
  are publish-invisible for free.
- Pull pipeline: `pull_realm_sync` (realm_sync.py:271) → `_artifacts_from_subtree`
  → per-artifact `_destination_for_sync_path` → generic canonical-text-compare
  overwrite loop. Specialized appliers own excluded classes (`board_sync.apply_board_pull`,
  `office_sync.apply_office_pull` — `_destination_for_sync_path` returns `None`
  for their paths). `--dry-run` is honored; `realm.sync.pulled` EventLog event
  is appended after mutation; `_write_sync_sidecar` records `skills_drift`.
- `_skills_drift_for_artifacts` (realm_sync.py:1308) currently compares pulled
  artifact bytes vs destination (which is the shared root today).
- Content hashing: `agent.skill_utils.skill_package_content_hash(skill_dir, skill_md)`
  (aggregate-mtime-keyed cache).
- CLI: realm sync verbs live in `hermes_cli/harness.py` (`hermes harness …`).
- Atomic writes: `utils.atomic_replace`.
- Tests: pytest under `tests/agent_runtime/` (`test_realm_sync*.py` shows the
  fixture patterns: tmp hermes root via env, fake realm store, subtree dirs).
- Fork boundary: `agent_runtime/` and the `hermes harness` CLI surface are
  fork-owned. `agent/skill_utils.py` may be touched ONLY for the
  `EXCLUDED_SKILL_DIRS` additions (one-line-class change).
- CI gate (stage42): every mutation verb MUST honor `--dry-run`.
- Realm-sync rule: sync mutations must emit an EventLog event (pull already
  emits `realm.sync.pulled`; extend its payload additively, no new event type
  needed).

## Components

### C1 — Resolver invisibility (agent/skill_utils.py, minimal touch)

Add `".realm_inbox"` and `".provenance"` to `EXCLUDED_SKILL_DIRS`.
Test: a `SKILL.md` under `shared/skills/.realm_inbox/r1/foo/` neither resolves
`foo` nor turns an existing canonical `foo` into a collision (assert via
`resolve_skills(["foo"])`).

### C2 — Promotion core (`agent_runtime/skill_promotion.py`, NEW)

Pure decision + guarded execution. Pinned API (Agent 2 codes against this):

```python
@dataclass(frozen=True)
class PromotionPlan:
    skill: str                  # canonical slug; at most ONE '/' (category form)
    action: str                 # 'promote_new' | 'noop_identical' | 'hold_divergent'
                                # | 'refuse_ambiguous_source' | 'refuse_invalid'
    source_dir: Path | None
    source_hash: str | None
    canonical_dir: Path | None
    canonical_hash: str | None
    reason: str                 # human-readable, hash-bearing for divergence

def classify_promotion(skill: str, source_dir: Path) -> PromotionPlan
def execute_promotion(plan: PromotionPlan, *, source: dict, adopt_divergent: bool = False,
                      dry_run: bool = False, move_source: bool = False) -> PromotionResult

@dataclass(frozen=True)
class PromotionResult:
    skill: str
    action: str                 # 'promoted' | 'noop' | 'held' | 'refused' | 'dry_run'
    archived_previous_to: Path | None
    provenance_path: Path | None
    reason: str

def realm_inbox_root() -> Path                    # shared/skills/.realm_inbox
def realm_inbox_dir(realm_token: str) -> Path    # …/.realm_inbox/<token>
def list_inbox_packages(realm_token: str | None = None) -> list[dict]
def promotion_provenance(skill: str) -> dict | None
```

Rules:
- Slug validation: `^[A-Za-z0-9_.-]{1,120}(/[A-Za-z0-9_.-]{1,120})?$`; refuse
  `.`-leading components, traversal, absolute, drive-letter (mirror
  `_profile_home_for_token` hygiene). Invalid → `refuse_invalid`.
- Source must contain `SKILL.md` at its root, else `refuse_invalid`.
- Hash = `skill_package_content_hash(dir, dir / "SKILL.md")`.
- Canonical location: `get_shared_skills_dir() / <slug parts>` (category form
  nests one level).
- `classify_promotion` compares source hash vs canonical hash (if canonical
  exists). It does NOT consult profile packs — profile-local duplicates are the
  *caller's* source, not an authority.
- `execute_promotion`:
  - `promote_new` → copy source → canonical; write provenance.
  - `hold_divergent` + `adopt_divergent=True` → archive current canonical to
    `shared/skills/.archive/<UTC ts>/<slug-flattened>/`, then copy source →
    canonical; provenance records `previous_hash`.
  - `hold_divergent` without adopt → `held`, no writes.
  - `noop_identical` → `noop`; if `move_source=True` the (redundant) source dir
    is archived to the same `.archive` scheme — this is the dedupe lane for
    profile-local duplicates.
  - `move_source=True` on `promoted` archives the source after a successful
    copy (promotion from a profile retires the duplicate — the collision guard).
    Promotion **from an inbox never moves** (the inbox is a realm mirror;
    callers pass `move_source=False`).
  - `dry_run=True` → no filesystem writes, `action='dry_run'`, plan echoed.
  - All writes atomic (write to temp sibling + `os.replace` per file, or copy
    to a temp dir + rename; follow `utils.atomic_replace` conventions). Never
    delete — archive.
- Provenance sidecar: `shared/skills/.provenance/<slug with '/'→'__'>.json`:
  `{"skill", "content_hash", "source" {"kind": "realm"|"profile"|"path", "realm_id"?, "profile"?, "path"?}, "promoted_at" (UTC ISO), "previous_hash"?}`.
  MUST live outside skill dirs (a file inside would change the package hash).

### C3 — Pull inbox routing (agent_runtime/realm_sync.py)

Follow the board/office precedent:
- `_destination_for_sync_path`: `skills/…` now returns `None` (leaves the
  generic loop). Keep traversal hygiene where it moves to.
- New `apply_skill_inbox_pull(realm, subtree) -> SkillSyncSummary` called from
  `pull_realm_sync` beside `apply_board_pull`/`apply_office_pull`:
  1. **Mirror**: `subtree/skills/**` → `.realm_inbox/<_safe_token(realm.id)>/…`
     byte-faithful. Prune inbox files/packages no longer present in the subtree
     (true mirror). Reuse `_canonicalize_text_bytes` compare to avoid EOL-only
     rewrites (see `test_realm_sync_eol.py`).
  2. **Reconcile** each inbox package (top-level dir, or `<cat>/<name>` one
     level deep, containing `SKILL.md`) via `classify_promotion`:
     - `promote_new` → `execute_promotion(..., source={"kind": "realm", "realm_id": realm.id}, move_source=False)` → adopted.
     - `noop_identical` → converged.
     - `hold_divergent` → held (NO canonical write).
  3. Summary dataclass with `adopted/converged/held/removed` lists and
     `as_dict()`; merged into the pull result as `result["skill_sync"]`;
     `changed=True` when adopted/removed non-empty.
- Dry-run: `pull_realm_sync(dry_run=True)` must not write the inbox either
  (mirror step is a mutation). Return the would-be summary if cheap, else omit.
- `skills_drift` (sidecar + result key consumed by the Launcher realm-sync
  sheet): redefine as the **held** package list (canonical differs from realm).
  Keep the key name and `list[str]` shape stable.
- `install_harness_skills` calls in `pull_realm_sync` are unrelated (harness
  skill self-install); leave them.

### C4 — CLI verbs (hermes_cli/harness.py)

- `hermes harness skills inbox [--realm <id>] --json` — read-only: list inbox
  packages with `{skill, realm, action(classification), source_hash, canonical_hash}`.
- `hermes harness skills promote <skill> [--from-realm <id> | --from-profile <name> | --from-path <dir>] [--adopt-divergent] [--move-source] [--dry-run] --json`
  - Exactly one source; if only `<skill>` is given and exactly one realm inbox
    holds it, that inbox is implied; otherwise typed refusal listing candidates.
  - `--from-profile` resolves `<profile_home>/skills/**/<skill>` (direct path
    then one category level); implies `--move-source` (retire the duplicate).
  - Divergent without `--adopt-divergent` → exit non-zero with BOTH hashes in
    the typed payload.
  - `--dry-run` prints the classification and writes nothing (stage42 gate).
  - Follow the existing harness verb registration/output-envelope pattern.

### C5 — Categorized skill publish (realm_sync.py `_skill_artifacts`)

Top-level dirs WITHOUT `SKILL.md` and not dot-prefixed: recurse ONE level; each
child dir WITH `SKILL.md` publishes as slug `<parent>/<child>` with
`relative_path=f"skills/{parent}/{child}/{rel}"`. Authority check:
`resolve_skill("<parent>/<child>")` must resolve uniquely from `shared_core`
(pass the categorized id as the identifier; direct path lookup handles it).
Selection matching (`mode == "selected"`): match either the categorized id or
the bare child name against `realm.skill_selection`. Inbox mirror and
promotion already support the one-level category form (C2/C3). Known follow-up
(out of scope): the Launcher skill picker / `skills_inventory` may not offer
categorized slugs for selection; note it in the commit message.

### C6 — Tests (tests/agent_runtime/)

New `test_skill_promotion.py`:
- classify: new / identical / divergent / invalid slug (traversal, dotted,
  two-level) / missing SKILL.md.
- execute: promote_new writes canonical + provenance; adopt_divergent archives
  previous (content preserved) and records `previous_hash`; held writes
  nothing; `move_source` archives source; `dry_run` writes nothing (assert
  filesystem snapshot unchanged).
- categorized slug round-trip (`software-development/hermes-agent`-shaped).

New `test_realm_sync_skill_inbox.py`:
- pull mirrors subtree skills into `.realm_inbox/<realm>/` and prunes removed
  files.
- new package auto-promotes (canonical + provenance + `skill_sync.adopted`).
- identical package converges (no rewrite; EOL-only difference converges).
- divergent package is HELD: canonical bytes untouched, `skills_drift` lists
  it, `skill_sync.held` lists it.
- resolver invisibility: after a pull, `resolve_skills` sees exactly one
  candidate for a skill that exists canonical + inbox (C1).
- publish excludes `.realm_inbox`/`.provenance`; publishes categorized
  `<cat>/<name>` package; selected-mode matches categorized id.
- `pull_realm_sync(dry_run=True)` leaves the filesystem untouched.

Update existing tests that assert skills pull straight into the shared root
(`test_realm_sync.py` — adjust expectations to inbox + auto-promote).

## Acceptance

1. `python -m pytest tests/agent_runtime -x -q` green (plus the two new files).
2. Full-suite spot check: `python -m pytest tests -q` no NEW failures vs main
   (record any pre-existing failures first).
3. stage42 dry-run gate: every new mutation verb honors `--dry-run`.
4. No new EventLog event types; `realm.sync.pulled` payload extended additively
   at most.
5. `skills_drift` sidecar key remains `list[str]` (Launcher compat).

## Out of scope (recorded, do not build)

- Mission Control inbox UI (Launcher-side slice; later).
- Multi-level (>1 category) skill nesting.
- Provenance-driven update notifications.
- skills_inventory/Launcher picker support for categorized selection slugs.

## Post-review hardening (2026-07-24, adversarial review confirmed 4 defects)

Fixes landed on `feat/skill-inbox-promotion` after an adversarial reviewer
reproduced four defects. They tighten C2/C3 without changing the target model:

- **Symmetric occupancy rule in `classify_promotion` (F1a + F2).** The canonical
  target *and*, for a categorized slug, its parent must each be
  *unoccupied-or-compatible* before an adopt is safe:
  - a bare slug whose canonical path exists as a **non-skill-package** (a
    category dir without `SKILL.md`, or a file) → `refuse_invalid` (previously
    `promote_new`, which then `os.replace`d onto the non-empty dir and raised —
    aborting the whole pull; repro: canonical `software-development/hermes-agent`
    vs a realm publishing bare `software-development`);
  - a categorized `<cat>/<child>` whose **parent** `<cat>` is an existing bare
    skill package → `refuse_invalid` (previously wrote the child *inside* the
    parent, changing the parent's content hash and injecting an ungated
    resolvable skill).
- **TOCTOU guard in `execute_promotion` (F1b).** Before `_atomic_install`, if the
  canonical slot is unexpectedly occupied (a concurrent writer, or a non-package
  dir that slipped past classification) it returns a typed `refused`
  `PromotionResult` instead of raising; the install itself is wrapped so any
  `OSError` also degrades to `refused`, never an exception.
- **Per-package isolation in `apply_skill_inbox_pull` (F1c).** Each inbox package
  is classified/executed inside its own `try`; a refusing *or* raising package is
  recorded and reconciliation continues — one bad package can never abort the
  pull. `SkillSyncSummary` gains an additive **`refused`** list (in `as_dict()`
  and `result["skill_sync"]`) beside `adopted/converged/held/removed`. Refused
  packages are deliberately **kept out of `skills_drift`** (which stays the
  held-divergent set, `list[str]` shape unchanged) — they surface only in
  `refused`.
- **Windows reserved device names (F3).** `_validate_slug` rejects any component
  whose stem is a reserved device (`con`/`prn`/`aux`/`nul`/`com1-9`/`lpt1-9`,
  case-insensitive, extension-ignoring — `con.md` too), via the shared
  `is_windows_reserved_component` helper. The pull **mirror** skips (and reports
  as `refused`) any package whose path components include a reserved name
  *before* any write, on every platform, so a realm publishing `con` cannot
  crash the mirror.

`result["skill_sync"]` shape after this work:
`{"adopted": [...], "converged": [...], "held": [...], "removed": [...], "refused": [...]}`.

## Out of scope (recorded, do not build)

- Mission Control inbox UI (Launcher-side slice; later).
- Multi-level (>1 category) skill nesting.
- Provenance-driven update notifications.
- skills_inventory/Launcher picker support for categorized selection slugs.

## Publishability + installer-ownership (2026-07-25)

Follow-on slice. New module `agent_runtime/skill_publishability.py` — the ONE
authority for "can this resolvable skill reach a realm, and may it be promoted".

**Frozen ruling: the shared root stays the SOLE publish source.** Publishing
`profile_local` was considered and rejected — the installer already distributes
the bundled catalog to every member, so a realm republishing it creates a second
authority for the same artifact, turns every hermes version bump into
HELD-divergent noise on every member's pull, and re-creates the multi-root
collision class the shared-skills-root migration retired. Do not add a second
publish source; the answer for a profile-local skill is promotion through the
existing guarded door.

- **Reported, never inferred.** `publishable` + a typed `publishable_reason`
  (`shared_root` / `profile_local_only` / `external_dir_only` / `unknown_root`)
  now ride on: `skills_inventory/v1` `skills[]` rows, a new
  `resolvable_skills[]` sweep across ALL three resolver tiers with a
  `publishability_summary` tally, the per-persona
  `prompt_observability.available_skills_context` rows (which previously carried
  an empty `realm_sync` list for an unpublishable skill — indistinguishable from
  "no realms"), and the new read-only verb
  `hermes harness skills publishable [--source-kind …] [--unpublishable-only]`.
- **Installer-owned packages are never offered and never admitted.** A profile
  package tracked by `<profile_home>/skills/.bundled_manifest` classifies as
  `installer_owned_pristine` (on-disk hash == recorded origin hash),
  `installer_owned_edited` (differs — the same test `hermes update` uses to
  decide "user-modified, keep"), or `installer_owned_state_unknown` (v1 entry
  with no origin hash, or the upstream hash primitive unavailable → fail-closed).
  Bundled-state detection imports `tools.skills_sync._dir_hash` /
  `_read_skill_name` rather than copying them, so the manifest's hash contract
  can never drift; if those move, everything classifies `…_state_unknown`.
- **Edited-bundled is a distinct class, still refused.** Promoting an edited
  bundled package under the installer's own name forks the catalog AND
  guarantees a `skill_collision` (installer copy at resolver index 0 vs the
  promoted shared copy). The refusal text names the non-forking route: author
  the work as a package of your own under a distinct slug outside the profile's
  installer-managed tree, then `promote --from-path`.
- **Reserved target slugs.** A promotion whose target slug (or the leaf of a
  `<category>/<name>` slug) is a name the installer manages in ANY profile is
  refused (`installer_catalog_slug_reserved`) whatever the source — this also
  covers realm pulls, where a realm publishing a bundled-named package would
  otherwise auto-promote into the shared root and break resolution for every
  persona. The pull already isolates refusals per package, so no `realm_sync.py`
  change was needed.
- **Enforced at the write paths only.** `classify_promotion` stays a pure
  comparison (unchanged; no new `PLAN_ACTIONS`). `execute_promotion` consults
  `promotion_refusal` for `promote_new` / adopt, BEFORE the `dry_run`
  short-circuit so a preview never promises a promotion the real run refuses,
  and returns `PromotionResult(action="refused", reason_code=<typed code>)`.
  `noop_identical` is exempt (it writes nothing).
- `_iter_packages` is now public as `iter_skill_packages`; the private name
  remains as an alias because `realm_sync.py` imports it.

Known live condition found while building this (NOT fixed — live state is
operator-owned): `writing-plans` exists BOTH in the shared root and as an
installer-tracked package at `<home>/skills/software-development/writing-plans`,
so `resolve_skill("writing-plans")` is a `collision` today.

## Deploy note

The live runtime runs from the installed venv (`.hermes\venvs\hermes-agent`),
not this repo checkout. After landing on `main`, the fork-update lane (MC
one-click update or manual venv reinstall) must run before the live serve
observes the new behavior.
