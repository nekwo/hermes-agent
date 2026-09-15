# w12/m5 — field notes, 2026-09-04

Six medium rows off the Mission Control queue, worked in a linked worktree on
branch `w12/m5` from hermes `dcba382f0a`. Nothing pushed. Python: the canonical
test env named in `AGENTS.md` §Testing, reached through `scripts/run_tests.sh`.

Two of the six turned out to be **already answered at base** and are handed back
as deletions with the check named. Two more were **right that something was
wrong and wrong about why**, which changed what got built. One is an owner
decision that stays owed. One landed as asked.

---

## Row 41 — persona→skill assignment has no repo-side seed

**Asked:** `_persona_from_overrides` has no `skills=` field and
`ensure_persisted_personas` merges `{**catalog, **stored}` so the store wins, so
A0's install is not reproducible on a fresh machine.

**Measured.** All three legs fail:

1. **The constructor's missing `skills=` is not a defect.** `_persona_from_overrides`
   does omit it, but `persona_records_from_config` applies config `skills:` /
   `skills_remove` in the override loop that runs for every persona, over the
   dataclass default `[]`. Config skills DO reach the catalog record. Pinned by
   `tests/hermes_cli/test_agent_list_skill_sources.py::test_a_config_only_persona_reports_the_catalog_and_no_difference`
   — run here, 4 passed, 8.3 s.
2. **The store-wins merge is a RULING, not an oversight.** S0a A6c settled it and
   wrote the reasoning into `config.persona_skill_sources`' docstring: skills have
   a store-writing verb with its own supersede clock (`persona set-skills` →
   `AgentPersona.skills_override_issued_at`) and the launcher's Skills console
   writes through it, so for skills the store is the authority BY DESIGN; a
   config seed that won over it would reintroduce the two-writer problem that
   clock arbitrates. Accounting shipped instead (`skills_source`,
   `catalog_only_skills`).
3. **A cross-machine seed already exists.** `skills` and `skills_remove` are both
   in `persona_config_sync.PERSONA_DEF_ALLOWED_KEYS`, and the pull writes them
   back into `agent_runtime.personas.<id>` via `atomic_roundtrip_yaml_update`. A
   fresh machine has no store row, so the catalog wins and the pulled skills
   apply.

**Changed:** nothing. **Verdict:** DELETE.

---

## Row 94 — the prep-cost stages' missing per-stage gate

**Asked:** a stage may not name a remedy site until an instrument bills that
site; §6 of `planned/chat-turn-prep-cost.md` requires this of the plan as a
whole, not of each stage.

**Measured.** True and unfixed: §6 is one plan-wide opening gate, discharged by
the first stage through it, and nothing in `scripts/` or `tests/` re-asks it.
Reading the seven stages against the live record reproduces the row's count
exactly — Stage 3's site bills 1 ms warm, Stage 4's ~6 ms against its own 100 ms
threshold, Stage 5's (`builds_overlapped` 1–3, led `build_ms=3979`) survived.

**Red-first.** Wrote `tests/agent_runtime/test_prep_cost_stage_gate.py` before
touching the plan: 3 failed / 2 passed, the two passing ones being the
anti-vacuity row that pins §5's seven-stage shape (so a regex matching nothing
cannot make the gate trivially green) and the checker's own falsifiability row.

**Changed.** `chat-turn-prep-cost.md` gains §6.1 (the rule, the three-stage
recurrence table, and why the two saves were the dispatch brief's doing rather
than this document's), and each of the seven stages gains one
`*Billing gate: …*` line taking `BILLED` (naming its receipt), `NO REMEDY SITE`,
or `NOT BILLED`. The test then reds on a stage with no line, on a verdict outside
the closed set, on a `BILLED` that names no receipt, and on Stages 3 or 4 being
laundered into `BILLED`.

**Verified.** `scripts/run_tests.sh tests/agent_runtime/test_prep_cost_stage_gate.py`
→ 5 passed. `doc_cite_adjacency.py --exclude archive --exclude planned` exit 0;
`dump_cli_contract.py --check` exit 0. Commit `9eec32ae2d`.

**Verdict:** DELETE.

---

## Row 96 — flow-graph ownership does not replicate with its agent

**Asked:** a ruling; the row's mechanism was `blueprints`/`blueprint_runs` being
in `realm_sync.HARD_EXCLUDED_PATH_PARTS` deliberately.

**Measured — the mechanism is wrong three ways.**

1. The runtime flow graph is stored in `flow_graphs/` (`flow_graph.FlowGraphStore`,
   archive `flow_graphs_stale/`). `blueprints`/`blueprint_runs` are the REMOVED
   mission lane's directories — the same pair `snapshot.py` records as
   zero-reader frame sections.
2. `grep flow_graph` returns nothing in `realm_sync.py`,
   `persona_config_sync.py` or `persona_instance_sync.py`. The graph is not
   excluded; it was never considered — which is why nothing on either machine
   says the canvas did not travel.
3. `steered_by` AND `id` are both in
   `persona_instance_sync.PERSONA_INSTANCE_ALLOWED_KEYS`. Ingest's semantic
   effect is writing the owner into each child's `steered_by`, so a replicated
   agent's STEERING is correct on machine 2 and `graph_id` (`runtime:<owner id>`)
   would address the right owner verbatim. What is missing is the authored
   canvas: node ids, layout, node→agent bindings, non-owner context edges.

So the symptom is "the second machine shows a blank canvas for a working agent",
not "the replica arrives broken" — a smaller and different problem than filed.

**Changed.** `planned/flow-graph-replication-ruling.md`: the three corrections,
what an operator sees today, three options (leave-local-and-say-so / travel the
document adopt-or-hold / re-derive from `steered_by`) with their costs, and the
one question the ruling turns on. Nothing built — this row needs an operator
ruling and stops here, per the brief. Commit `6b14f0e55f`.

**Verdict:** NARROW (text below).

---

## Row 107 — `root_config_misplacement` asymmetry, 9 vs 2

**Asked:** same finding class on both stores, 4.5× apart; the class wants one
owner, not two hand-fixes.

**Measured.** The finding was missing both halves of what that reading needs.
Its sibling `_persona_binding_report` has carried a `remediation` string since it
shipped; this one had per-row `notices` describing the symptom and nothing
saying what to do, so every row was an independent hand-fix on whichever machine
reported it. And a row is (profile × concrete key) with two of the four
`ROOT_ONLY_CONFIG_KEYS` patterns per-persona
(`personas.*.workdir`, `personas.*.chat_lane_restore_toolsets`), so the count
scales with how many profiles and personas a machine HAS — comparing two
machines' raw counts compares their inventories, not their health. Nothing said
so.

**Red-first.** Four tests appended to
`tests/agent_runtime/test_root_config_misplacement.py` (the cure; the cure being
present on a CLEAN store, so it is the class's and not an incident note; the
denominator across three profiles; and the denominator coming from the rows' own
walk) — 4 failed / 6 passed before the fix.

**Changed.** `find_misplaced_root_only_keys` → `scan_misplaced_root_only_keys`,
returning `{"rows": …, "scope": …}` from ONE walk, and
`_root_config_misplacement_report` gains `remediation` and `scope`. The
remediation says plainly that no automated repair exists, because rewriting an
operator's `config.yaml` is not a write this doctor takes on its own. Doctor
`schema_version` 8 → 9, its numbered note added, and the deliberate pin in
`test_harness_doctor.py` edited beside its own changelog. The list-returning
wrapper was dropped rather than left callerless (it would have been dead code
against the ratchet).

**Verified.** `test_root_config_misplacement.py` + `test_harness_doctor.py` +
`test_root_config_pinning.py` → 68 passed. `doc_cite_adjacency` initially red by
1 — my 20-line insert moved `config.py:532`, cited from
`01-system-architecture.md:50`; re-anchored to `:552` (and the already-stale
`:578` beside it to `:651`), then exit 0. `dump_cli_contract --check` exit 0.
Commit `3ad5c392ed`.

**Verdict:** NARROW (text below).

---

## Row 112 — the gateway fence's doctor exemption

**Asked:** two callers (`dep_ensure.py`, `nous_subscription.py`) still reach
`agent_browser_runnable` with no seam; two seams of the same shape as
`run_doctor`'s close it, then the exemption is deleted.

**Measured — the row's premise holds and its remedy does not.** Both named
callers are unmocked, and the doctor-side seam did land as the narrowing says.
But deleting the exemption reds **more than the two named tests**: also
`test_tools_config.py::test_first_install_nous_auto_configures_video_gen`, two in
`test_image_gen_picker.py` and two in `test_tts_picker.py`, all reaching
`nous_subscription._has_agent_browser` through `tools_config`'s `tools_command`
/ `_visible_providers` — and two of those tests do not even take `monkeypatch`.
The reachers are a family, not two sites.

**Built and reverted, deliberately.** I first built the two seams the row
specifies (`_browser_available` / `dependency_status` /
`ensure_dependency(agent_browser_runnable_override=…)` threaded from
`cmd_postinstall`; the same on `_has_agent_browser` / `_local_browser_runnable` /
`get_nous_subscription_features` / `apply_nous_managed_defaults`). Two things
came out of that:

* the nous half broke a dozen existing zero-argument `_has_agent_browser` stubs
  the moment it took a positional parameter, and
* it still left the five picker/tools_config reds, because those call sites are
  reached from surfaces that would each need the parameter threaded too.

Both changes were reverted. **Zero production code changed for this row.**

**Changed.** One autouse fixture in `tests/hermes_cli/conftest.py`,
`_agent_browser_probe_never_spawns`, patching `agent_browser_runnable` on every
module that binds it by value plus `hermes_constants` (which covers every
function-local importer and every module imported after the fixture runs).
Default `False` — the deterministic answer; whether the developer's box has the
CLI installed is not a thing any test here means to assert. Opt out with
`@pytest.mark.real_agent_browser_probe`, registered in `pyproject.toml` beside
its sibling opt-outs. The exemption, its `_is_agent_browser_version_probe`
helper and the `_AGENT_BROWSER_BASENAMES` table are deleted and the real-store
arm is unconditional again; the test that pinned the exemption is rewritten into
its inverse (the probe naming the real store IS refused, and the reason says
"REAL store"), and its parametrized sibling re-aimed at both historical spellings
of the escape.

**Verified.** Every `tests/hermes_cli` file mentioning nous_subscription /
dep_ensure / cmd_postinstall / browser / doctor — 59 files, 782 tests, 0 failed,
with the exemption gone. Commit `b565499fff`.

**Found and NOT fixed** (the row names it, and it needs its own row): five
duplicate test function names in `tests/hermes_cli/test_doctor.py`. They are not
copies — the later definition shadows the earlier, so the SECOND block
(`:772`–`:990`) is what runs and the first (`:447`–`:712`) is dead. What is
silently lost: the dead block patches `auth.get_nous_auth_status_local` where the
live one patches `get_nous_auth_status`, and its provider parametrization covers
`nvidia` and `moa` where the live one covers `ai-gateway`. Both spellings and all
three providers exist in `hermes_cli/auth.py` today, so this wants a union, not a
delete — a judgment call outside this row.

**Verdict:** NARROW (text below).

---

## Row 134 — persona-prewarm Stage 3a, re-measure whether it earns its worker

**Asked:** the `check_fn` sweep left `perform_agent_create`'s path
(`10d0d3a41c`), which retired the counted half of the stage's conviction;
"nobody has measured what a warm is worth since"; owner decides keep / shrink /
retire.

**Measured — the premise is false, twice.**

* S0a A6b measured it on 2026-09-03 (`ba7157801d`, whose tree contains
  `10d0d3a41c` — checked with `git merge-base --is-ancestor`): the delta a warm
  buys for a SECOND persona type is 1–2 ms.
* A re-take here, three runs each on a fresh interpreter and a hermetic root,
  reproduces that (0.8 / 4.1 / 2.0 ms) and takes the half A6b did not — the
  FIRST warm, which is the number the decision turns on:

      warm_persona_memos(dev) on a cold process : 1458 / 1543 / 1418 ms
      the create-shaped resolve that follows    :   13 /    9 /   12 ms

The registry populate is what costs, it is process-wide, and paying it on the
worker keeps ~1.4–1.5 s off the FIRST create's critical path in a serve process.
That is intact; the sweep took the per-persona half, which the module's own
W2-H3 block had already priced at ~10–16 ms.

**Changed.** The measurement is recorded in
`agent_runtime/persona_prewarm.py`'s docstring, in the module's own convention
(it already carries the W2-H3 numbers). No behavior change. `doc_cite_adjacency`
red by 1 afterwards — `07-observability.md:193` cited `persona_prewarm.py:139`
for `PREWARM_DONE_RECEIPT`, now `:163`; re-anchored, exit 0.

**Verified.** `test_persona_prewarm.py` + `test_agent_create_subphases.py` → 19
passed. Commits `4a40b90dd3`, `3f3457ba0a`.

**Verdict:** DELETE the measurement half; the keep/shrink/retire call is the
owner's and now has its number. Recommended answer, unasked-for: KEEP, and do
not build a per-persona warm beyond the first.

---

## Reds not mine

* `tests/test_coverage_claims_resolve.py::test_every_coverage_claim_names_a_test_that_exists`
  is red at base by claims in `planned/s2-introduce-directory-push*.md`, as the
  dispatch brief said. Re-run after my changes: the failure set is unchanged —
  nothing here added to it.

## Housekeeping

The shared scratchpad this session is not per-agent: another lane's `queue.py`
sat in it and shadowed the stdlib `queue` module for any script run from that
directory, which broke the first prewarm re-take with a traceback that pointed
at `persona_prewarm.py:120`. Worth knowing before the next measurement script.
