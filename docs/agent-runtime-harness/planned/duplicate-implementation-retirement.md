# Planned — duplicate-implementation retirement

**Status:** audit complete, nothing executed. **Audited:** 2026-08-22 against
HEAD `b2eb1a15db` (working tree clean). **Owner domain:** spans 01/02/08; this
file is the delete schedule, the domain docs stay the truth of what exists.

Scope ruling for this audit: **fork-owned code only.** The fork boundary is the
doc 17 §2 path filter — `agent/`, `agent_runtime/`, `hermes_cli/harness*` —
plus the fork-created `tools/` files the same check excludes by name
(`tools/agent_chat_tool.py`, `tools/board_tool.py`, `tools/tool_full_descriptions.py`;
doc 17 §1 rows, "never existed upstream"). Every conviction below carries a
FORK-OWNED line citing one of those two authorities. Upstream-owned code is
report-only and appears here only as an acquittal reason.

This plan **links, and does not duplicate**, three sibling planned files that
already carry half the docket:
[read-model-db-serve-population.md](read-model-db-serve-population.md),
[task-bound-vocabulary-retirement.md](task-bound-vocabulary-retirement.md),
[writerless-goal-lane-residue.md](writerless-goal-lane-residue.md). Where a
stage below depends on one of their rulings, the stage says so instead of
re-arguing it.

---

## Convictions

Value order: cheapest-safest first. A stage is not "delete on sight" — it is
the evidence, what breaks, the kill proof, and the class of decision it needs.

### Stage 1 — `PERSONA_CHAT_SESSION_SOURCE` defined twice, same value

- **WHAT:** one wire-vocabulary constant, two definition sites:
  `agent_runtime/persona_chat_continuity.py:38` and
  `agent_runtime/persona_chat_history.py:44`, both `"agent_runtime_persona_chat"`.
  Doc 08's debt register already names the pair.
- **PROOF of duplication:** repo grep (2026-08-22) shows every production
  consumer outside `persona_chat_history.py` resolves the *continuity* copy —
  `persona_chat_durability.py:54`, `hermes_cli/harness.py:111-116`,
  `hermes_cli/harness_support.py:56` (re-exported at `:61`, consumed by
  `persona_commands.py:131-136`). Only `persona_chat_history.py:329,356` read
  the local copy. Neither module imports the other
  (`persona_chat_continuity.py` import block `:25-33`; `persona_chat_history.py`
  `:10-37`), so the duplicate is not cycle-avoidance — it is drift waiting for
  a one-sided edit.
- **WHAT DELETION BREAKS:** four test files import the name *from history*
  (`tests/agent_runtime/test_agent_chat_tool.py:862`,
  `test_persona_chat_continuity.py:1105,1227,1541`). A bare delete of the
  history copy breaks them; a re-export does not.
- **KILL PROOF:** `persona_chat_history.py` replaces its `= "…"` line with
  `from .persona_chat_continuity import PERSONA_CHAT_SESSION_SOURCE` (test
  imports keep resolving). Gate: grep for
  `PERSONA_CHAT_SESSION_SOURCE\s*=\s*"` returns exactly one hit, in
  `persona_chat_continuity.py`. Rider: `harness.py` imports the name twice
  (`:116` from continuity, `:143` via `harness_support`) — drop the `:116` copy
  in the same commit; the `harness_support` re-export is the one the exec'd
  parts already use.
- **RISK CLASS:** mechanical.
- **FORK-OWNED:** all files under `agent_runtime/` (doc 03 invariant 8) and
  `hermes_cli/harness*` (doc 17 §2 filter).

### Stage 2 — `_ReadModelCache` in the serve: a response cache wearing the read model's name

- **WHAT:** `hermes_cli/harness_parts/serve.py:616` `class _ReadModelCache`
  (+ `_ReadModelCacheEntry` `:604`, `_READ_CACHE_MAX_AGE_SECONDS` `:408`,
  docstring pointer `:36`). It is a per-serve-loop **stdout-payload replay
  cache** for `status --json` / `snapshot --json` behind a runtime-state stat
  fingerprint and a 20 s TTL — doc 08 open row 3 already had to spend a
  paragraph saying it is *not* the read model and *not* a model cache.
- **PROOF:** not a dead shell and not a functional duplicate — the conviction
  is the NAME. "Read model" now means three unrelated things in this repo:
  (1) `read_model.py`/`read_model.db`, (2) the core-cache dir
  `serve_read_model/` (`core_cache.py:244`), (3) this response cache. A test is
  already confused by it: `tests/agent_runtime/test_running_work.py:1377`
  `test_a_process_starting_invalidates_the_serve_read_model_cache` targets
  cache (3) via `serve._runtime_state_fingerprint()` while its name says (2).
- **WHAT RENAME BREAKS:** nothing on the wire — the class is module-private;
  the wire-visible artifacts are the `served_from_cache` / `cache_age_ms`
  stamps (`serve.py:1308-1319`), which do not change. Doc 08 open row 3's
  anchor text updates in the same commit (rule 4, staleness).
- **KILL PROOF:** rename to `_PollResponseCache` (entry class and the
  `test_running_work.py` test name/docstring with it). Gate: grep for
  `_ReadModelCache` returns zero hits outside `docs/**/archive/`.
- **RISK CLASS:** mechanical (name-only; no behavior, no wire bytes).
- **FORK-OWNED:** `hermes_cli/harness_parts/serve.py` matches the
  `hermes_cli/harness` boundary prefix (doc 17 §2).

### Stage 3 — mirrored constants without the fence the house pattern requires

- **WHAT:** two deliberate same-value mirror pairs in `agent_runtime/` that,
  unlike the `ERR_*` precedent, have **no equality fence**:
  - `LANE_MISSION_CHAT = "mission_chat"` — `mcp_admission.py:153` and
    `terminal_envelope.py:134` (the latter's comment `:131-133` declares the
    mirror: "Same spelling as `mcp_admission.LANE_MISSION_CHAT`").
  - `REPLY_LIMIT = 8000` — `dispatch_delivery.py:105`, `dispatch_store.py:133`,
    and a third spelling `_REPLY_LIMIT = 8000` at `tools/agent_chat_tool.py:71`;
    `dispatch_store.py:129-131` documents the mirror in prose only.
- **PROOF of the defect class:** the repo's own precedent convicts the shape:
  `agent_create.py:526-533` duplicates four `ERR_*` codes from `serve_rpc.py`
  *and pins them* — "`test_agent_create_service.py` asserts each constant
  equals `serve_rpc`'s same-named one, so a change to either goes red." The two
  pairs above are the same shape minus the fence: a comment is not a gate, and
  a one-sided edit to a wire token (`"mission_chat"` rides admission decisions)
  or a truncation bound fails silently. No import cycle forces the duplication
  (neither module of either pair imports the other — verified by grep), but the
  cheap, precedent-matching fix is the fence, not single-homing.
- **WHAT BREAKS:** nothing — the change is additive (one small test module
  asserting the three equalities).
- **KILL PROOF:** the fence test IS the kill proof: any future divergence goes
  red with the file names in the assertion message, exactly like
  `test_agent_create_service.py`.
- **RISK CLASS:** mechanical (additive test only).
- **FORK-OWNED:** `agent_runtime/*` (doc 03 invariant 8);
  `tools/agent_chat_tool.py` is fork-created (doc 17 §2 exclusion list).

### Stage 4 — `PersonaInstanceStore.create_free_floating`: production-callerless mint

- **WHAT:** `agent_runtime/persona_assignments.py:426`
  `create_free_floating(persona_or_template) -> PersonaInstance`.
- **PROOF of deadness:** grep across `agent_runtime/ hermes_cli/ tools/ scripts/`
  (2026-08-22) finds exactly one non-test occurrence — the definition itself.
  Doc 08's debt register carries the same finding ("production-callerless but
  is the mint fixture for ~10 test files across flow-graph/checkpoint/
  state-patch suites").
- **WHAT DELETION BREAKS:** those ~10 test files. This is why the stage is a
  *fold*, not a delete: move the mint into a shared test-support helper
  (`tests/agent_runtime/` conftest or support module), point the suites at it,
  then delete the store method.
- **KILL PROOF:** grep gate — `create_free_floating` has zero hits under
  `agent_runtime/` after the fold; a tombstone `Form.ATTR` row in
  `tests/agent_runtime/test_tombstone_registry.py` (the registry that already
  pins removed `PersonaAssignmentStore.*` names at `:2311-2314,2555-2556,2797-2798`
  is the natural home).
- **RISK CLASS:** mechanical with test churn (~10 files, no production path).
- **FORK-OWNED:** `agent_runtime/persona_assignments.py` (doc 03 invariant 8).

### Stage 5 — `--message` on `persona instance create`: inert, and `required=True`

- **WHAT:** `hermes_cli/harness.py:922-927` — the flag is accepted, marked
  DEPRECATED in its own help text, ignored by the handler (S70), and still
  **required**: a CLI caller must supply a message that nothing reads.
- **PROOF:** the argument's own comment is the confession: "accepted for
  launcher wire-compat only and is NOT acted on … Removing the flag needs a
  lockstep launcher change: mission_control_bridge.dart emits it on every
  persona.instance.create / persona.profile.instantiate call." Doc 08's debt
  register carries the row; `--title` (`:921`) is NOT part of this conviction —
  it is live as the display-name fallback (`persona_commands.py:599,607`).
- **WHAT DELETION BREAKS:** every launcher build that still emits `--message`
  gets an argparse error — a hard cross-repo break. Order is forced: launcher
  stops emitting first (its own commit), hermes demotes `required=True` →
  optional-ignored in the same window, and only then deletes the flag.
- **KILL PROOF:** grep gates on both repos — `--message` absent from
  `harness.py`'s `persona_instance_create` block and from
  `mission_control_bridge.dart`'s create/instantiate argv builders; the
  launcher-side registry `allowedArgs` row (doc 08 launcher-owned handoff)
  drops in the same launcher change.
- **RISK CLASS:** needs-lockstep (cross-repo argv compat; not a snapshot
  contract bump — the flag never lands on the wire envelope).
- **FORK-OWNED:** `hermes_cli/harness.py` (doc 17 §2 filter). The launcher half
  is out of this repo and is the gate, not the work.

### Stage 6 — the `read_model.db` lane: built, enabled, and serving no one

- **WHAT:** the whole RD2 shell — `agent_runtime/read_model.py`,
  `agent_runtime/read_model_schema.sql` (sole reference `read_model.py:339`),
  `agent_runtime/projector.py` (29 lines, `full_rebuild()` only), the two CLI
  verbs `harness rebuild-read-model` / `harness read`
  (`harness.py:1402-1409` → `runtime_commands.py:553,566`), and the
  `ReadModelConfig` block (`runtime_config.py:87`).
- **PROOF of deadness/duplication:**
  - `write_snapshot()` (`snapshot.py:2311`) has exactly one non-test caller —
    `read_model.py:167` inside `resolve_snapshot_frame`, reached only from
    `_cmd_snapshot` (`runtime_commands.py:473-489`). The serve path bypasses it
    by design (`serve.py:969` docstring). Neither `read_model.db` nor
    `snapshot.json` exists in the live store root (doc 02 open row 3).
  - `Projector.full_rebuild()` (`projector.py:28-29`) and `write_snapshot()`'s
    gated `ReadModel().apply_full_rebuild(snapshot)` (`snapshot.py:2326-2333`)
    are **two production writers of the same database over the same
    `build_snapshot()` output** — one gated on `read_model_enabled()`, one not.
    A genuine double implementation, both reachable only from manual CLI verbs.
  - **New evidence this audit adds** (it closes the check the sibling planned
    file left open): the launcher's `snapshot.json` cold-paint lane is
    **retired** — `mission_control_snapshot.dart:187` ("`cached` was retired in
    MC-7 / P11 with the on-disk `snapshot.json` lane") and
    `mission_control_bridge.dart:1086` (past tense); a repo grep of the
    launcher's `lib/` finds no reader. `write_snapshot`'s boot-cache write
    serves no boot.
  - The lane cannot even save work as shaped: `resolve_snapshot_frame`
    (`read_model.py:167-173`) **builds the full core first** and only then
    decides whether to serve the cached frame — `FrameSource.CACHE` costs one
    full build plus a DB read.
- **WHAT DELETION BREAKS:** `tests/agent_runtime/test_read_model*.py`,
  `test_projector.py`, and `test_s46_incremental_projection_lane_removal.py`
  (which pins projector.py's *current* 29-line shape — it must be rewritten to
  pin absence, in the tombstone style it already uses); `_cmd_snapshot` keeps
  working with `resolve_snapshot_frame` collapsed to `build_snapshot()` plus
  the `frame_source=built` parity stamp (`frame_source` stays on the envelope —
  additive wire rule, no bump); `snapshot_watermark()`'s never-`0` rule
  (doc 02 invariant 1) must survive wherever its callers land.
- **KILL PROOF:** the retirement gate already written in
  [read-model-db-serve-population.md](read-model-db-serve-population.md) —
  grep-clean including both CLI verbs and the test files — plus `MODULE`
  tombstone rows for `agent_runtime.read_model` and `agent_runtime.projector`
  in `test_tombstone_registry.py` (the S46 precedent row pattern).
  **CORRECTION (coordinator review, 2026-08-22): `ReadModelConfig` is NOT
  grep-clean deletable.** The class is overloaded: `delta_patches`
  (`runtime_config.py:103`) gates the LIVE S7-A patch-producer lane (ships on
  via `SHIPPED_DELTA_PATCHES`, resolved by `state_patches.delta_patches_enabled`,
  and the launcher's `kMissionControlBaseSeedConfigYaml` seeds
  `read_model.delta_patches: true` — the YAML key path is cross-repo wire).
  Retire only the dead-lane fields — `enabled`, `serve_snapshot_from_db`,
  `db_filename` — and keep the class, the `read_model:` YAML block, and
  `delta_patches` untouched.
- **RISK CLASS:** operator-ruling-needed. The ruling and its three outcomes
  belong to the sibling planned file; this plan does not fork them. This
  audit's verdict input: outcome (2) **retire** — the O(world) standing
  architecture (doc 02) is served by `serve_read_model/`'s core cache; outcome
  (1) would wire a second validity authority `core_cache.py:20-40` argues
  against, into a resolver that builds anyway. If the operator instead rules
  revive-and-wire, this stage inverts to KEEP and the naming stage below
  activates.
- **FORK-OWNED:** every named file is under `agent_runtime/` or
  `hermes_cli/harness*` (doc 03 invariant 8; doc 17 §2).

### Stage 7 — `serve_read_model/` dirname: contingent rename, sequenced after Stage 6

- **WHAT:** `CORE_CACHE_DIRNAME = "serve_read_model"` (`core_cache.py:244`) —
  the core cache's on-disk home named after the unrelated read model.
- **PROOF the collision is real:** doc 02 needs a bold "**It is not the read
  model below**" (`02:216`) and "**`serve_read_model/` is not this**" (`02:311`);
  the sibling planned file calls it "a hazard for anyone reading this domain
  cold".
- **WHAT A RENAME BREAKS (checked, not guessed):** launcher: comment-only
  references (two test-file comments; zero `lib/` hits — grep 2026-08-22).
  Hermes: the constant plus prose at `core_cache.py:715,1572`,
  `state_patches.py:868`, `serve.py:969`, and the Stage-2 test name. On disk:
  the dir is a **cache, never authority** (doc 02 invariant 5), so the old dir
  orphans and the next boot pays one demote-priced rebuild — real money while
  open row 2 stands (11,980 ms vs 911 ms), which is why this stage does not
  run before a boot the operator can afford it on, and why a consult-time
  adopt-or-rebuild of the old dirname is the preferred spelling.
- **KILL PROOF:** grep gate — `serve_read_model` appears nowhere outside
  `docs/**/archive/` and the one migration/adoption line; one
  `snapshot_core_cache` demote receipt with the migration reason on the first
  boot after.
- **RISK CLASS:** mechanical, **contingent**: if Stage 6 rules retire, the
  sibling file's own outcome-2 text says "let `serve_read_model/` own the name
  unambiguously" — then this stage is CANCELLED as unnecessary churn. It runs
  only under outcomes (1) or (3), where two live "read model"s would coexist.
- **FORK-OWNED:** `agent_runtime/core_cache.py` (doc 03 invariant 8).

---

## Acquitted

Cleared with evidence, so nobody re-audits them.

- **`agent_runtime/delivery_directive.py` — the module is live, only its name
  is historical.** Every line of code in the file serves
  `reap_orphan_worktrees` (`:38`) and its two helpers; callers:
  `harness_doctor.py:7,181` (`doctor --fix`) and `runtime_commands.py:35-37`
  (`harness worktree reap`), plus a 500-line pinning suite
  (`test_delivery_directive.py`). The docstring accurately narrates the removed
  half (`:1-18`). A module rename would touch 4 production files + tests to fix
  a filename; not worth it. No dead weight to delete.
- **`agent_runtime/task_store_stub.py` docstring vs `tools/board_tool.py`
  reality — already on the docket elsewhere.** Verified: `_resolve_board_target`
  imports only `board_models` / `BoardStore` / `WorkspaceStore`
  (`board_tool.py:82-84`); the stub survives on the single re-export
  `store.py:149` and ~12 pinning test files. R-3 stands as issued (doc 17 §3),
  and [task-bound-vocabulary-retirement.md](task-bound-vocabulary-retirement.md)
  gate 1 already requests the re-ruling — see Operator rulings below; nothing
  is deleted here and nothing is duplicated there.
- **`PersonaAssignmentStore` — live, not a shell; retirement stays blocked.**
  The 6 production importers, enumerated: `hermes_cli/harness.py:58` (exec-
  namespace binding for the parts), `persona_commands.py:75` (used
  `:187,358,1300,6342`), `agent_runtime/status.py:13→153` (`list_all` onto the
  status wire), `agent_runtime/snapshot.py:40→879` (`list_all` into the core),
  `persona_profile_binding.py:270→278` (`find_active` on the binding ladder),
  and `persona_assignments.py:1551` (`scan_all` inside the instance repair
  lane). Two of the six put rows on the wire, so retirement is a snapshot
  contract bump gated on launcher consumers — exactly doc 08's row. Deletable
  NOW from this cluster: only Stage 4's `create_free_floating`.
- **`agent_runtime/` has zero orphan modules.** An import-graph scan (regex
  over `from .X` / `agent_runtime.X` across `agent_runtime/ hermes_cli/ tools/
  acp_adapter/ scripts/ agent/`, tests excluded) found **no** module with zero
  production importers — even `projector.py` has its one CLI caller. The dead
  shells in this repo hide behind live imports, not missing ones.
- **`ERR_INVALID_PARAMS` / `ERR_HANDLER_FAILED` / `ERR_NOT_FOUND` /
  `ERR_CONFLICT` duplicated in `agent_create.py:530-533` vs
  `serve_rpc.py:134-139`** — deliberate dependency-inversion (serve_rpc imports
  agent_create) and fenced: `test_agent_create_service.py` asserts equality.
  This is the *pattern* Stage 3 extends, not a defect.
- **`PHASE_ORDER`** (`agent_create_phases.py` vs `mission_chat_phases.py`) —
  same name, different lanes, different tuples. Not one question.
- **`ARCHIVED_LEDGER_CAP = 5000`** (`board_store.py` vs `office_store.py`) —
  per-store retention bounds that may legitimately diverge. Not one question.
- **`scope_for_persona`** (`coordinator_permissions.py:35` vs
  `terminal_envelope.py:1132`) and **`consult` / `reset_process_state`**
  (`core_cache.py` vs `demote_core_reuse.py`) — same names over different
  domains (spawn permissions vs terminal scope; two caches with a parallel API
  by design). Callers always module-qualify. No action.
- **`_legacy` / `_old` / `_v2` sweep** — every hit is a live migration or
  adoption lane (`default_scope.py` legacy-realm adoption,
  `mission_chat_turns.py` monolith split, `profile_artifact_sync.py:251`
  legacy-layout mapping, `terminal_envelope.record_legacy_block`). No dormant
  `_v2` twins found in fork code.
- **`tools/mission_goal_tool.py`** — already deleted; the doc 17 filter entry
  is historical.
- **Refusals honored:** nothing above re-proposes doc 08's
  refusals-with-measurement (the `profile_readiness_for_persona` memo, prewarm
  concurrency) nor its refused-with-evidence rows (persona-chat append seam,
  `contract_manifest`/`contracts dump`, R11, H-CLI-5 follow-ons).

---

## Operator rulings needed

1. **The `read_model.db` lane's fate** (Stage 6) — the ruling itself lives in
   [read-model-db-serve-population.md](read-model-db-serve-population.md).
   This audit adds: the launcher-side check its "Note for whoever takes this"
   asked for is now done — the `snapshot.json` cold-paint lane was retired at
   MC-7/P11 and no launcher reader remains — and the cache path builds the full
   core before consulting the cache. Both findings weigh for outcome (2),
   retire. Stage 7 is cancelled or activated by this same ruling.
2. **R-3 re-ruling on `TaskStoreStub`** — owned by
   [task-bound-vocabulary-retirement.md](task-bound-vocabulary-retirement.md)
   gate 1. The stub's stated cause (an unguarded upstream import) is verified
   gone; either the docstring is corrected to "insurance", or the stub, the
   `store.py:149` re-export, and the s1 keep-set item retire together.
3. **The two `status.py` writer-less keys** (`foreground_runtime` /
   `runtime_instances`, `status.py:110,113` — the *same call twice*) — owned by
   [writerless-goal-lane-residue.md](writerless-goal-lane-residue.md) gate 1,
   which requires a launcher reader check first. Subsumed by reference; not
   restaged here.
