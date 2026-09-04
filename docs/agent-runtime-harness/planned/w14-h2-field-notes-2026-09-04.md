# w14/h2 field notes — 2026-09-04

Four rows, hermes half only. Branch `w14/h2`, base `3d3a33be3e`. Commit per row
and per stage; nothing pushed.

---

## Row 1 — flow-graph CANVAS replication (RULED replicate; stages S1–S6)

**What the row asked.** Build the staged plan already on main
(`planned/w13-h2-flow-graph-canvas-replication.md`, hermes `d427c6f5d6`) from
stage 1, red-first, one commit per stage. The previous lane's uncommitted files
were dropped at landing and were not looked for.

**What was measured first.** The plan's §0 premise still holds at this base:
`grep flow_graph` matches nothing in `realm_sync.py`, `persona_config_sync.py`
or `persona_instance_sync.py`. The flow graph is outside every sync door, not
excluded from one — a fourth projection to build, not a gate to open.

### S1 — the projection and its accounting (`bf634062df`)

New `agent_runtime/flow_graph_sync.py` + `tests/agent_runtime/test_flow_graph_sync.py`.
Allowlist, deterministic LF bytes, timestamp-free semantic hash. Nodes travel
with `x`/`y`; edges keep drawn order (fan-in priority); the viewport, the store
envelope's `updated_at`/`requested_by` and every unknown key at document, node
or edge level are dropped with accounting.

Red-first: `test_the_viewport_is_dropped_and_named_on_the_row`, written before
the module existed (collection error, then the missing accounting). 13 tests.
`flow_graph.py` untouched, per the stage.

### S2 — publish (`ea36d1d38b`)

`_flow_graph_projection` / `_flow_graph_artifact` / `_flow_graph_row` in
`realm_sync.py`, `paths.flow_graph_baseline_path` +
`paths.flow_graph_conflict_path`, and the family's baseline helpers. Scoped to
`office_scan.instance_ids`; artifact appended only when non-empty, so a realm
that never drew a canvas publishes byte-identically to before. `store/
flow_graphs.yaml` classifies as `flow_graph_config` and resolves to `None` in
`_destination_for_sync_path`.

One asymmetry worth recording: the instance projection reads its store through
`scan_all` and spends the `unreadable` count; the canvas projection does ONE
`get` per owner instead. The graph directory legitimately holds canvases for
owners no realm places (archived desks' drawings are kept), so walking it would
report every one of those as a shortfall.

Checks: `test_flow_graph_publish.py` 7 tests; neighbours re-run green
(`test_persona_instance_publish.py`, `test_realm_sync.py`, `test_flow_graph.py`,
`test_flow_graph_sync.py` — 112 tests); `scripts/dump_cli_contract.py --check`
fresh (exit 0).

### S3 — pull, adopt-or-hold, whole document (`483b156089`)

`read_remote_flow_graphs` / `apply_flow_graph_pull` / `FlowGraphPullSummary`,
called from `pull_realm_sync` immediately after the mint door, ack row
`result["flow_graph_sync"]` emitted unconditionally.

Two decisions the classifier does not make, both recorded at their site:

- a canvas the realm stopped carrying is `upstream_absent`, never deleted
  (`ARCHIVE_LOCAL` and `edit_vs_remove` both land there). Owner-liveness
  reaping, which archives, is the one authority that removes a drawing.
- a node bound to an instance that did not travel is written WHOLE and reported
  as `unbound_node_agents`.

The stage boundary — no `reconcile_flow_graph_steering` — is pinned by a test
that raises if it is called. 10 tests.

### S4 — the reap ordering (`ed8fbba5c8`)

**Half the stage's premise is false at this base, and the row is recorded rather
than "fixed".** The reap does NOT run inside `pull_realm_sync`:
`_prune_owner_less_flow_graphs` is phase 5 of `reconcile_persona_instances`,
whose only caller is `hermes_cli/harness_parts/runtime_commands.py` (the
`harness runtime reconcile` verb). So no pull archives a canvas in its own pass,
whatever order its lanes run in, and the plan's "reverse the order and it is
archived" red is not reproducible.

What was pinned instead: the canvas lane runs after the mint door, verified red
by swapping the two calls in `pull_realm_sync` (the ordering assertion fails,
then restored with `git checkout --`), because the canvas lane resolves the live
instance set for `unbound_node_agents` and would otherwise name every binding
the same pull was about to satisfy. The hazard's real home — the liveness
classifier holding a canvas whose owner resolves and calling the identical
canvas stale when it does not — is asserted directly. 2 tests.

### S5 — the accounting surface (`9b7346e51d`)

Top-level `flow_graphs` key on `realm_sync_status`:
`{publishable, unpublished, held, unreadable}`.

**Deviation from the plan, measured.** The stage asked for a `store_drift`
family. `store_drift` rows are exactly the set `realm_revert` addresses, and it
sorts them through `_PROCESS_ORDER[row.family]` — a direct subscript — and
dispatches on family for the upstream lookup, the baseline and the store door. A
family added there without a revert arm hands `revert --all` a `KeyError` and
offers the operator an exit that does not exist. A count with no revert arm is
honest; a drift row with none is not. The canvas revert arm is a new row.

`scripts/generate_agent_runtime_response_fixtures.py` regenerated two
`realm_sync_status` fixtures (additive key only). The launcher mirror under
`EterniaLauncher/test/fixtures/hermes_responses/` is the cross-repo half and is
NOT done here.

### S6 — docs

`01-system-architecture.md` gains "And the CANVAS travels with the agent" beside
the instance-replication section (that is where the three projections are
documented; `06-office-and-board.md` and `02-runtime-data-and-shapes.md`, which
the plan named, carry no projection contracts). Every coverage claim names a
test file that exists. `flow-graph-replication-ruling.md` is flipped to RULED
and BUILT and points at the plan, with the §3 reap-cost correction stated.

**Left.** The launcher half: rendering the held/conflict rows, the response
fixture mirror, and whether a pulled canvas opens read-only until its owner is
minted. The canvas revert arm. `flow_graphs_stale/` still does not replicate, by
the plan's §3.

---

## Row 2 — the `profile:` spelling the launcher places without the ownership check (`4f79e484bd`)

**What the row asked.** `snapshot._available_persona_summary` mints
`persona_id = f"profile:{profile_name}"` for every profile template
unconditionally, bypassing `agent_create.accepted_persona_spellings`. Route the
row through that authority and carry the list.

**What was measured.** Confirmed end to end. For a profile TWO personas declare,
`profile_persona_resolution` returns no match (`PROFILE_CHAT_TOOLSET_AMBIGUOUS`),
and `_persona_by_id`'s synthesis lane then builds an `AgentPersona` from config
defaults with `toolsets=[]` — the create succeeds and mints a defaults-less,
toolset-less agent under a name that reads like the persona beside it. The
zero-owner case is the opposite: decision **D-U1** exempts every `profile:` id
from the roster check on purpose, because the launcher's template browser sends
exactly those and the synthesis is intended.

**What changed.** `persona_spellings` on each row, asked of
`accepted_persona_spellings` once per owner, plus the unowned case that
per-persona authority cannot answer. Same rule applied to `backs_persona_id`,
which was built by a dict comprehension keyed on profile name and silently kept
whichever owner iterated LAST (measured: `backs_persona_id: 'bob'` for a profile
alice and bob both declare).

Red-first: 4 tests in `tests/agent_runtime/test_available_persona_spellings.py`,
all four red before the change. Neighbours green:
`test_snapshot_catalog_memo.py`, `test_snapshot_normalize.py`,
`test_response_contract_fixture.py`.

**Left.** The launcher half is now worth filing: the Presets lane should place
from `persona_spellings` rather than from `persona_id`, and refuse (or warn) on
a row whose `persona_id` is absent from its own spellings list.

---

## Row 3 — asset format policy ratification (`a6ae02c206`)

**What the row asked.** RULED option A: flip
`docs/studio/ASSET_FORMAT_FOUNDATIONS.md` from "policy owed" to ratified and add
a capture/storage/export declaration stub.

**What was measured.** That document lives in the LAUNCHER repo; hermes has no
copy. The hermes half is the seam the policy actually governs:
`agent/pet/generate/atlas.py`'s `atlas_to_webp_bytes`, whose one-line docstring
named none of it.

**What changed.** The encoder now states that it IS the storage seam, why each
save flag is load-bearing (`exact=True` is the anti-fringing one), and what the
seam does not decide — capture depth as the one-way door, export meeting the
consumer — so a deep-born asset class is told not to reach this encoder at all.
No test exists for this module; the change is documentation at the site.

**Left.** The launcher half (the status flip and the stub) — verbatim patch in
the lane's final message.

---

## Row 4 — the neutral-cwd paragraph in `AGENTS.md` (`5a08846f0d`)

**What the row asked.** RULED option 1: mirror the launcher's neutral-cwd rule
into hermes' git-discipline section, then delete the row.

**What was measured.** `AGENTS.md` has no git-discipline section and no
commit-pathspec rules — the launcher paragraph's "the two commit-pathspec rules
above" has no antecedent here, so the mirror is adapted rather than copied. The
nearest home is Known Pitfalls, beside "Squash merges from stale branches
silently revert recent fixes".

**What changed.** A new Known Pitfalls subsection carrying the incident
(2026-08-31, W1-H1), both tells that lied, the repair (`2638504f9b`), the
corollary for whoever is landing, and the fact that no hook enforces it in
either repo.

`scripts/doc_cite_adjacency.py --exclude archive --exclude planned` exit 0 (it
covers `docs/agent-runtime-harness/`, not `AGENTS.md`). No absolute paths added.

---

## Main reds that are not mine

`tests/agent_runtime/test_stream_contract_fixture.py` — two failures,
`test_committed_goldens_are_the_generators_bytes` and
`test_every_generated_golden_has_the_producer_shape`, both on
`delta_agent_create_narrow_profile.json` over `core.agents[].skill_hash_absent`.
Proven to predate this branch: restored `agent_runtime/snapshot.py` to
`3d3a33be3e` and the same two failed. Left alone.

`tests/test_coverage_claims_resolve.py::test_every_coverage_claim_names_a_test_that_exists`
— one failure, and it is a FALSE POSITIVE of the gate rather than a rotted
citation. `planned/w13-h1-field-notes-2026-09-04.md:246` quotes a flaky pytest
log line verbatim (`FAILED …::test_a_cleared_binding_is_not_stale_because…`) and
the extractor reads the quoted line as a coverage claim. The file is unchanged
from the base sha (landed by `8f9f0b8ac3`), so the red predates this branch.
Left alone: the fix is either the quoted line's spelling in that lane's notes or
the extractor's handling of quoted log output, and neither is this row.
