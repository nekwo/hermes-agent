"""Operator flow-graph documents — the authored agent map, ingested WHOLE.

The launcher's Agent Flow chart is one JSON document: nodes that may reference
persona instances, and edges that draw "A steers B". Historically the launcher
decomposed that drawing into N per-agent ``persona instance steer`` CLI calls —
N process spawns, N independent failure points. This module is the replacement
lane (2026-07-16, operator-decided shape):

1. The doc arrives whole (``hermes harness flow set --graph <json>``) and is
   stored verbatim as the runtime's copy of the operator's map.
2. The referenced instances — which ALREADY EXIST in the runtime — have their
   steering relations set from the doc's edges, in-process, in one pass.
   **Ingest never creates, starts, or deletes an instance**, and it never
   touches goal membership: a chart states who steers whom, nothing else.
3. The situational HUD (runtime_hud.py) then reads the very relations the doc
   set, so the agent's fed ``## Runtime Situation``, the CONTEXT peek, and the
   operator's HUD strip all agree with the drawing.

Wire format (exactly the launcher flow store's persisted shape, plus the store
key): ``{"graph_id": str, "nodes": [{"id": str, "agent": str|null, ...}],
"edges": [{"from": str, "to": str}]}``. Unknown node keys (x/y layout, future
fields) are ignored but preserved in the stored doc — layout is the operator's,
not the runtime's.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .persona_assignments import PersonaInstanceStore, safe_assignment_token

# A flow doc is a small authored drawing; anything near this size is not one.
MAX_FLOW_DOC_BYTES = 256 * 1024


class FlowGraphDocError(ValueError):
    """The submitted document is not a valid flow graph. The message is
    operator-facing (rendered verbatim by the launcher's sync status)."""


@dataclass(frozen=True)
class FlowGraphDoc:
    """A parsed, validated flow-graph document."""

    graph_id: str
    # node id -> bound persona instance id (None = authored-but-unbound node).
    node_bindings: dict[str, str | None]
    # (from_node_id, to_node_id), order preserved (edge order is fan-in
    # priority: the first drawn parent stays the primary parent).
    edges: list[tuple[str, str]]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def parse_flow_graph_doc(payload: Any) -> FlowGraphDoc:
    """Validate the wire payload into a [FlowGraphDoc]. Raises
    [FlowGraphDocError] with an operator-readable reason on any defect."""

    if not isinstance(payload, dict):
        raise FlowGraphDocError("flow graph must be a JSON object")
    graph_id = safe_assignment_token(payload.get("graph_id"))
    if not graph_id:
        raise FlowGraphDocError("graph_id is required (the map's stable key)")

    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list):
        raise FlowGraphDocError("nodes must be a list")
    node_bindings: dict[str, str | None] = {}
    for entry in raw_nodes:
        if not isinstance(entry, dict):
            raise FlowGraphDocError("every node must be a JSON object")
        node_id = safe_assignment_token(entry.get("id"))
        if not node_id:
            raise FlowGraphDocError("every node needs a non-empty id")
        if node_id in node_bindings:
            raise FlowGraphDocError(f"duplicate node id: {node_id}")
        agent = entry.get("agent")
        agent_id = safe_assignment_token(agent) if agent is not None else None
        node_bindings[node_id] = agent_id or None

    raw_edges = payload.get("edges")
    if raw_edges is None:
        raw_edges = []
    if not isinstance(raw_edges, list):
        raise FlowGraphDocError("edges must be a list")
    edges: list[tuple[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()
    for entry in raw_edges:
        if not isinstance(entry, dict):
            raise FlowGraphDocError("every edge must be a JSON object")
        src = safe_assignment_token(entry.get("from"))
        dst = safe_assignment_token(entry.get("to"))
        if not src or not dst:
            raise FlowGraphDocError("every edge needs from and to node ids")
        if src not in node_bindings or dst not in node_bindings:
            raise FlowGraphDocError(
                f"edge references an unknown node: {src} -> {dst}"
            )
        if src == dst:
            raise FlowGraphDocError(f"a node cannot steer itself: {src}")
        key = (src, dst)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append(key)

    # One agent may live at ONE node — the launcher's bindAgent guarantees it,
    # and the reconcile below depends on it (two nodes claiming one agent would
    # make "this agent's drawn parents" ambiguous).
    claimed: dict[str, str] = {}
    for node_id, agent_id in node_bindings.items():
        if agent_id is None:
            continue
        prior = claimed.get(agent_id)
        if prior is not None:
            raise FlowGraphDocError(
                f"agent {agent_id} is bound to two nodes ({prior}, {node_id})"
            )
        claimed[agent_id] = node_id

    return FlowGraphDoc(
        graph_id=graph_id,
        node_bindings=node_bindings,
        edges=edges,
        raw=payload,
    )


def desired_parents_by_agent(doc: FlowGraphDoc) -> dict[str, list[str]]:
    """The drawing's runtime intent: for every BOUND agent, the ordered list of
    bound parent agent ids (fan-in aware, edge order preserved, deduped).

    Pure. Unbound nodes have no runtime identity: they neither receive an entry
    nor contribute as parents — an authored placeholder is a position on the
    map, not a steering edge yet. A bound agent with no resolvable drawn
    parents maps to ``[]`` (drawn standalone)."""

    desired: dict[str, list[str]] = {}
    for node_id, agent_id in doc.node_bindings.items():
        if agent_id is None:
            continue
        parents: list[str] = []
        for src, dst in doc.edges:
            if dst != node_id:
                continue
            parent_agent = doc.node_bindings.get(src)
            if parent_agent is None or parent_agent == agent_id:
                continue
            if parent_agent not in parents:
                parents.append(parent_agent)
        desired[agent_id] = parents
    return desired


def reconcile_flow_graph_steering(
    doc: FlowGraphDoc,
    *,
    store: PersonaInstanceStore | None = None,
) -> list[dict[str, Any]]:
    """Set each referenced EXISTING instance's steering parents from the doc.

    Parents only, by design:

      * **Never creates.** The chart references instances the runtime already
        made; an unknown reference is reported (``ok: false``), not spawned.
      * **Never touches goal membership.** A drawn-standalone agent gets
        [PersonaInstanceStore.clear_parents] — which preserves goal_id /
        current_task_id / mode — NOT ``detach_parents`` (that verb is "leave
        the mission" and would strip the root agent's live goal binding).
      * **No-ops are skipped**, so re-ingesting an unchanged doc writes nothing
        and emits no events.

    One agent's failure never aborts the rest; the caller gets a complete
    per-agent report."""

    store = store or PersonaInstanceStore()
    results: list[dict[str, Any]] = []
    for agent_id, parents in desired_parents_by_agent(doc).items():
        entry: dict[str, Any] = {
            "persona_instance_id": agent_id,
            "desired_parents": list(parents),
        }
        try:
            instance = store.get(agent_id)
        except Exception:
            entry.update(
                ok=False,
                changed=False,
                error="unknown persona instance (flow ingest never creates instances)",
            )
            results.append(entry)
            continue
        current = [p for p in (instance.steered_by or []) if p]
        try:
            if parents == current:
                entry.update(ok=True, changed=False, steered_by=current)
            elif parents:
                updated = store.set_parents(agent_id, parents, goal_id=None)
                entry.update(ok=True, changed=True, steered_by=list(updated.steered_by))
            else:
                updated = store.clear_parents(agent_id)
                entry.update(ok=True, changed=True, steered_by=list(updated.steered_by))
        except ValueError as exc:
            entry.update(ok=False, changed=False, error=str(exc))
        except Exception as exc:  # defensive: report, never abort the pass
            entry.update(ok=False, changed=False, error=f"steer failed: {exc}")
        results.append(entry)
    return results


class FlowGraphStore:
    """Durable runtime copies of authored flow-graph docs, one JSON file per
    graph id under ``<runtime_root>/flow_graphs/``. The doc is stored verbatim
    (layout and all) plus ingest metadata — the runtime holds the operator's
    drawing, it does not reinterpret it."""

    def _dir(self) -> Path:
        return paths.store_root() / "flow_graphs"

    def _path(self, graph_id: str) -> Path:
        token = safe_assignment_token(graph_id)
        if not token:
            raise FlowGraphDocError("graph_id is required")
        return self._dir() / f"{token}.json"

    def set_doc(self, doc: FlowGraphDoc, *, requested_by: str | None = None) -> dict[str, Any]:
        stored = {
            "graph_id": doc.graph_id,
            "doc": doc.raw,
            "updated_at": now().isoformat(),
            "requested_by": safe_assignment_token(requested_by) or "operator",
        }
        directory = self._dir()
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json_write(self._path(doc.graph_id), stored, indent=2, sort_keys=True)
        return stored

    def get(self, graph_id: str) -> dict[str, Any] | None:
        path = self._path(graph_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def list_ids(self) -> list[str]:
        directory = self._dir()
        if not directory.exists():
            return []
        return sorted(path.stem for path in directory.glob("*.json"))


def _bound_agents_of_stored(stored: dict[str, Any] | None) -> set[str]:
    """The bound-agent id set of a previously stored doc — best-effort: a
    missing/corrupt prior doc contributes no departures rather than failing
    this ingest."""

    if not isinstance(stored, dict):
        return set()
    try:
        prev = parse_flow_graph_doc(stored.get("doc"))
    except FlowGraphDocError:
        return set()
    return {agent for agent in prev.node_bindings.values() if agent}


def reconcile_departed_agents(
    *,
    departed: set[str],
    map_member_ids: set[str],
    store: PersonaInstanceStore,
) -> list[dict[str, Any]]:
    """An agent REMOVED from the map loses the map's wiring (operator decision
    2026-07-16: "removing it from the chart removes its steering" — off-map no
    longer means untouched once the agent was previously part of THIS doc).

    Scope of authority stays the document's own membership: only parents that
    are/were members of this map are stripped; a foreign parent (set by the
    goal engine or another surface) is preserved. Goal membership is never
    touched — same clear_parents/set_parents semantics as the main pass."""

    results: list[dict[str, Any]] = []
    for agent_id in sorted(departed):
        entry: dict[str, Any] = {
            "persona_instance_id": agent_id,
            "removed_from_map": True,
        }
        try:
            instance = store.get(agent_id)
        except Exception:
            # Departed from the map AND gone from the runtime: nothing to strip.
            entry.update(ok=True, changed=False, steered_by=[])
            results.append(entry)
            continue
        current = [p for p in (instance.steered_by or []) if p]
        remaining = [p for p in current if p not in map_member_ids]
        try:
            if remaining == current:
                entry.update(ok=True, changed=False, steered_by=current)
            elif remaining:
                updated = store.set_parents(agent_id, remaining, goal_id=None)
                entry.update(ok=True, changed=True, steered_by=list(updated.steered_by))
            else:
                updated = store.clear_parents(agent_id)
                entry.update(ok=True, changed=True, steered_by=list(updated.steered_by))
        except ValueError as exc:
            entry.update(ok=False, changed=False, error=str(exc))
        except Exception as exc:  # defensive: report, never abort the pass
            entry.update(ok=False, changed=False, error=f"steer failed: {exc}")
        results.append(entry)
    return results


def ingest_flow_graph(
    payload_text: str,
    *,
    requested_by: str | None = None,
    store: PersonaInstanceStore | None = None,
) -> dict[str, Any]:
    """The whole ingest, from wire text to per-agent report. Raises
    [FlowGraphDocError] on an invalid document (nothing stored, nothing
    written); a stored doc with per-agent reconcile failures is NOT an error —
    the report carries them.

    Ingest also settles DEPARTURES: agents bound in this graph's previously
    stored doc but absent from the new one get the map's wiring stripped
    (see [reconcile_departed_agents]) — so deleting a node on the chart
    removes its steering relations instead of orphaning them runtime-side."""

    raw_bytes = payload_text.encode("utf-8", errors="replace")
    if len(raw_bytes) > MAX_FLOW_DOC_BYTES:
        raise FlowGraphDocError(
            f"flow graph document too large ({len(raw_bytes)} bytes > {MAX_FLOW_DOC_BYTES})"
        )
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise FlowGraphDocError(f"flow graph is not valid JSON: {exc}") from exc

    doc = parse_flow_graph_doc(payload)
    graph_store = FlowGraphStore()
    # Departure set comes from the doc this one REPLACES — read before storing.
    previous_bound = _bound_agents_of_stored(graph_store.get(doc.graph_id))
    graph_store.set_doc(doc, requested_by=requested_by)

    store = store or PersonaInstanceStore()
    reconciled = reconcile_flow_graph_steering(doc, store=store)
    next_bound = set(desired_parents_by_agent(doc))
    departed = previous_bound - next_bound
    if departed:
        # A departed agent sheds edges to ANY member this map ever declared —
        # previous members included, so removing two wired nodes in one edit
        # cannot leave them wired to each other.
        reconciled.extend(
            reconcile_departed_agents(
                departed=departed,
                map_member_ids=previous_bound | next_bound,
                store=store,
            )
        )
    failed = [entry for entry in reconciled if not entry.get("ok")]
    return {
        "ok": True,
        "graph_id": doc.graph_id,
        "stored": True,
        "node_count": len(doc.node_bindings),
        "edge_count": len(doc.edges),
        "bound_agent_count": len(next_bound),
        "removed_from_map_count": len(departed),
        "reconciled": reconciled,
        "changed_count": sum(1 for e in reconciled if e.get("changed")),
        "failed_count": len(failed),
    }
