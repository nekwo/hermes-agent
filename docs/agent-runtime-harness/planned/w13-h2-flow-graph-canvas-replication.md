# Flow-graph CANVAS replication — staged plan (w13/h2, 2026-09-04)

**Ruling this builds:** *REPLICATE the canvas with the agent — nodes, layout and
non-owner edges travel in the same sync the steering already rides. Authored work
silently missing on a replica is worse than the sync cost.* (operator,
2026-09-04, adopting option **B** of
[`flow-graph-replication-ruling.md`](flow-graph-replication-ruling.md).)

The ruling settles §4 Q1 of that brief. Q2 (whole-document versus per-node
granularity) is not re-opened here: option B answers it in its own text —
*"three-way merge on node positions has no natural resolution, so realistically
it is adopt-or-hold at whole-document granularity"* — and adopting the option
adopts its granularity. Q3 (the accounting is not optional) is stage 5.

## 0. The starting position, re-measured

Everything in the ruling brief's §5 table still holds at this base. The one
thing worth restating, because every stage below depends on it: the flow graph
is not excluded from sync, it is **outside every door**. `grep flow_graph`
matches nothing in `agent_runtime/realm_sync.py`,
`agent_runtime/persona_config_sync.py` or
`agent_runtime/persona_instance_sync.py`. So this is not a gate to open; it is a
fourth projection to build on the pattern the other three already share.

The pattern being copied, named once so no stage has to re-derive it:

| concern | the persona-INSTANCE family's answer |
|---|---|
| wire path | `PROJECTION_RELATIVE_PATH` = `store/persona_instances.yaml` — a path `_destination_for_sync_path` maps to `None`, so an old member SKIPS it and degrades to "no replication" |
| what travels | an allowlist (`PERSONA_INSTANCE_ALLOWED_KEYS`), never the record |
| accounting | `dropped_keys` / `unexpected_key` / `rows_unreadable` on the publish row |
| change detection | a never-synced per-realm baseline sidecar of semantic hashes |
| merge | `sync_merge.classify_three_way_pull` → adopt / keep-local / archive / **conflict**, never last-write-wins |
| scope | exactly `office_scan.instance_ids` — the desks this publish already ships |

## 1. What travels, and what deliberately does not

The stored doc is `{graph_id, doc, updated_at, requested_by}`
(`FlowGraphStore.set_doc`). Only `doc` is authored; the other three are local
provenance.

**Travels** (the operator's drawing):

- `nodes[]` — `id`, `x`, `y`, `agent`. `x`/`y` ARE the ruling's "layout".
- `edges[]` — `from`, `to`, order preserved (edge order is fan-in priority).
  Non-owner edges included, per the ruling: ingest reports and never applies
  them, and they are exactly the "local context" the operator drew.

**Does NOT travel, and each has a reason at its site:**

- `viewport` (`{x, y, zoom}`). The launcher's own contract calls it *"a VIEW
  preference, not part of the wiring … never a steering fact"*
  (`lib/features/mission_control/flow/agent_flow_graph.dart`, `AgentFlowViewport`).
  It is the window-position half of the ruling brief's option A, and it stays
  there. **It must be dropped with accounting, not silently** — an unaccounted
  drop is the same silence this row exists to close.
- `updated_at` / `requested_by`. Local provenance, and `updated_at` would make
  every hash differ on every save.
- Any unknown top-level key. Hermes stores unknown keys verbatim today; the
  projection allowlists, so an unknown key is `dropped_keys` on the publish row
  rather than an unreviewed field on the wire.

## 2. Stages

Each stage: the files, the red-first test, the check, and what it must not
change. One commit per stage.

---

### S1 — the projection and its accounting (pure, no I/O)

**Files:** new `agent_runtime/flow_graph_sync.py`; new
`tests/agent_runtime/test_flow_graph_sync.py`.

**Build:**

- `FLOW_GRAPH_PROJECTION_RELATIVE_PATH = "store/flow_graphs.yaml"` — an old
  member maps it to `None` and skips, exactly like the instance projection.
- `FLOW_GRAPH_ALLOWED_KEYS` / `FLOW_GRAPH_NODE_KEYS` / `FLOW_GRAPH_EDGE_KEYS`,
  frozensets, one spelling each.
- `project_flow_graph(stored, *, dropped)` → the allowlisted body, appending to
  `dropped` every key it refused (`viewport` included, named).
- `project_flow_graphs(owner_instance_ids, *, docs)` → `FlowGraphProjection`
  with `graphs`, `dropped_keys`, `unreadable`, `to_bytes()`, `as_dict()`.
- `flow_graph_def_hash(body)` — the semantic content hash the baseline and the
  three-way classifier key on. Timestamp-free by construction, because
  `updated_at` never enters the body.

**Red-first:** `test_the_viewport_is_dropped_and_named_on_the_row` — a stored
doc carrying a viewport must project without it AND report `viewport` in
`dropped_keys`. Written before the allowlist exists; it fails first on the
missing module, then on the missing accounting.

**Check:** `scripts/run_tests.sh tests/agent_runtime/test_flow_graph_sync.py`.

**Must not change:** `agent_runtime/flow_graph.py`. S1 reads stored docs; it
does not touch parse, ingest, reconcile, or reap.

---

### S2 — publish the projection

**Files:** `agent_runtime/realm_sync.py` (`_resolve_artifacts_with_projection`,
a `_flow_graph_artifact` beside `_persona_instance_artifact`, a
`_flow_graph_row` beside `_persona_instance_row`);
`tests/agent_runtime/test_realm_sync.py`.

**Build:** resolve graphs for exactly `office_scan.instance_ids` — the same list
the instance projection is pruned to, never a second enumeration — via
`FlowGraphStore.get(f"runtime:{instance_id}")`. Append the artifact only when
the projection is non-empty. Report `flow_graph_projection` on the publish
result and update a per-realm baseline after a real (non-dry-run) publish.

**Red-first:** publish a realm with one desk whose owner has a stored graph;
assert the artifact list carries `store/flow_graphs.yaml` and that the
projection's bytes contain the node ids. Fails before the resolver arm exists.

**Check:** the touched suites, plus `scripts/dump_cli_contract.py --check`
(unchanged — no argparse moves in this stage).

**Must not change:** the artifact set for a realm with no stored graphs. A realm
that never drew one must publish byte-identically to today, so the existing
publish goldens do not move.

---

### S3 — pull: adopt-or-hold at whole-document granularity

**Files:** `agent_runtime/flow_graph_sync.py` (`apply_flow_graph_pull`),
`agent_runtime/realm_sync.py` (call it from `pull_realm_sync`),
`tests/agent_runtime/test_flow_graph_sync_pull.py`.

**Build:** per graph id, `classify_three_way_pull(local_hash, remote_hash,
baseline_hash)` and act on the returned `PullAction`:

- `WRITE_REMOTE` → write through `FlowGraphStore.set_doc` so the doc is
  validated by `parse_flow_graph_doc` on the way in, never written raw.
- `KEEP_LOCAL` / `NOOP` → accounted, nothing written.
- `CONFLICT` → **hold**, with a conflict sidecar on the instance family's
  pattern (`_write_conflict_sidecar`). Two operators, two canvases, one graph
  id is the case the ruling brief names, and a drawing has no natural
  three-way resolution — so it is a loud hold, never a merge.

**Dangling node→agent references** (ruling brief §3, cost 2). A pulled canvas
may bind nodes to instances that did not travel. Ingest already refuses to
create instances, so this pull must NOT either. The rule: the document is
written whole, and the unresolvable bindings are reported as
`unbound_node_agents` rows. A node whose agent is absent is an authored node
with a binding this machine cannot resolve YET — the instance may arrive on the
next pull — and dropping it would silently edit the operator's drawing, which is
the failure mode this whole row is about.

**Red-first:** `test_a_pulled_canvas_with_an_absent_agent_is_written_whole_and_reported`
and `test_two_diverged_canvases_hold_rather_than_merge`.

**Must not change:** steering. `reconcile_flow_graph_steering` is NOT run by the
pull — the steering already travels on `steered_by`
(`PERSONA_INSTANCE_ALLOWED_KEYS`), and running ingest here would let a pulled
drawing rewrite a peer's instance records. **This is the boundary of the
stage and the one thing a reviewer should check first.**

---

### S4 — the reap ordering

**Files:** `agent_runtime/realm_sync.py` (pull order),
`agent_runtime/flow_graph.py` if the reap needs a fence;
`tests/agent_runtime/test_flow_graph.py`.

**The hazard** (ruling brief §3, cost 3): owner-liveness reaping
(`classify_graph_owner_liveness` → `reconcile_departed_agents`) archives a graph
whose owner instance is gone. A canvas pulled before its owner instance is
minted looks exactly like that and is reaped on arrival.

**Build:** run the flow-graph pull AFTER `apply_persona_instance_pull` in
`pull_realm_sync`, so the owner exists before any liveness check can see the
graph, and pin the ORDER with a test rather than a comment.

**Red-first:** a pull carrying both an instance and its canvas, with the reap
running in the same pass; the canvas must survive. Reverse the order and it is
archived — that is the red.

---

### S5 — the accounting surface (the part of the ruling that is not optional)

**Files:** `agent_runtime/realm_sync.py` (`realm_sync_status` envelope),
`scripts/generate_agent_runtime_response_fixtures.py` if the envelope grows a
key the launcher parses; `tests/agent_runtime/test_response_contract_fixture.py`.

**Build:** `flow_graph_sync` rows on the pull result and a `flow_graphs`
count/drift family on the status envelope, so "this chart is empty because the
canvas did not travel / is held / conflicts" is answerable from the envelope.
Every other sync lane accounts for what it dropped; this one now does too.

**Cross-repo:** the launcher renders these rows. That half is a separate row —
this stage only produces them, and the response-envelope fixture family (landed
in this same lane) is how the launcher learns their shape.

---

### S6 — docs

`docs/agent-runtime-harness/06-office-and-board.md` (the sync contracts) and
`02-runtime-data-and-shapes.md` (the flow graph's place) gain the fourth
projection. Retire `flow-graph-replication-ruling.md` to the ruled state by
pointing it at this plan. Every coverage claim added must NAME a test that
exists (`tests/test_coverage_claims_resolve.py`).

## 3. What this plan does not decide

- **The launcher half.** Rendering the held/conflict rows, and whether a pulled
  canvas opens read-only until its owner is minted, are launcher rows.
- **`flow_graphs_stale/`.** Archived docs stay local. Replicating a reaped
  drawing has no requester.
- **Per-node merge.** Explicitly out, per §0 above. If a later row wants it, it
  is a new ruling, not an extension of this one.
