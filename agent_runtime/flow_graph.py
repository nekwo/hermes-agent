"""Operator flow-graph documents — the authored agent map, ingested WHOLE.

The launcher's Agent Flow chart is one JSON document: nodes that may reference
persona instances, and edges that draw "A steers B". Historically the launcher
decomposed that drawing into N per-agent ``persona instance steer`` CLI calls —
N process spawns, N independent failure points. This module is the replacement
lane (2026-07-16, operator-decided shape):

1. The doc arrives whole (``hermes harness flow set --graph <json>``) and is
   stored verbatim as the runtime's copy of the operator's map.
2. The referenced instances — which ALREADY EXIST in the runtime — have their
   steering relations set from the doc's OWNER edges, in-process, in one pass.
   **Ingest never creates, starts, or deletes an instance**, and it never
   touches goal membership: a chart states who steers whom, nothing else.
3. The situational HUD (runtime_hud.py) then reads the very relations the doc
   set, so the agent's fed ``## Runtime Situation``, the CONTEXT peek, and the
   operator's HUD strip all agree with the drawing.

**Per-instance blueprint ownership (2026-07-18).** Graph identity IS the owner
instance's id: a doc's ``graph_id`` is ``runtime:<owner>``, and that map is that
one instance's blueprint. A map therefore asserts ONLY its owner's outbound
edges (``owner → child``): it may set or clear the OWNER in each referenced
child's ``steered_by`` and nothing else. Parents set by another lead's map, or by
the goal engine, are preserved — so a child steered by A AND B keeps both, and
two leads' maps COMPOSE into fan-in instead of clobbering one another. This is
the fix for the live "two Neko instances show the SAME blueprint" bug, whose root
was a whole-doc ingest with no ownership scope letting two maps fight over one
child's steering. Non-owner edges present in a doc (a deeper tree drawn on one
lead's canvas) are the operator's local layout on that map; they are reported
(never silently dropped) and never applied — the referenced parent's OWN
blueprint is where such an edge becomes steering.

**Owner-liveness reaping (2026-07-30).** Because graph identity IS the owner
instance's id, a stored doc outlives its owner: reaping the instance leaves the
map behind, addressed to an agent that no longer exists. The persona-instance
reconciler's last phase settles that (see [classify_graph_owner_liveness] and
[FlowGraphStore.archive]) — archive into ``flow_graphs_stale/``, never delete,
and strictly on OWNER liveness: an empty launcher-created canvas whose owner is
live is intended, not garbage.

Wire format (exactly the launcher flow store's persisted shape, plus the store
key): ``{"graph_id": str, "nodes": [{"id": str, "agent": str|null, ...}],
"edges": [{"from": str, "to": str}]}``. Unknown node keys (x/y layout, future
fields) are ignored but preserved in the stored doc — layout is the operator's,
not the runtime's.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .persona_assignments import PersonaInstanceStore, safe_assignment_token

# A flow doc is a small authored drawing; anything near this size is not one.
MAX_FLOW_DOC_BYTES = 256 * 1024

# Owner-liveness reap reasons (typed, single-sourced — the reconciler reports
# them verbatim, no boolean soup). A graph is one instance's blueprint, so the
# only question asked of a stored doc is whether its owner still resolves.
GRAPH_PRUNE_REASON_OWNER_NOT_LIVE = "graph-owner-not-live"
GRAPH_HELD_REASON_OWNER_LIVE = "graph-owner-live"
# Held by the reconciler's belt: the owner is absent from the literal live-id
# set but the store still RESOLVES it through the identity aliases (a canvas
# saved under an actor-token / legacy spelling of a live instance).
GRAPH_HELD_REASON_OWNER_ALIASED = "graph-owner-resolves-through-alias"


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
    """The WHOLE drawing's runtime intent: for every BOUND agent, the ordered list
    of bound parent agent ids (fan-in aware, edge order preserved, deduped).

    Pure. Unbound nodes have no runtime identity: they neither receive an entry
    nor contribute as parents — an authored placeholder is a position on the
    map, not a steering edge yet. A bound agent with no resolvable drawn
    parents maps to ``[]`` (drawn standalone).

    NOTE: this is the whole-doc projection. It is NOT what [ingest_flow_graph]
    applies — ingest is OWNER-SCOPED (a map asserts only its owner's edges; see
    [reconcile_flow_graph_steering]). Kept as a pure helper / analysis primitive.
    """

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


def owner_instance_id_of(graph_id: str) -> str:
    """The owner instance id a [graph_id] addresses.

    Graph identity IS the owner instance's id: the launcher opens instance X's
    console on graph ``runtime:<X>``, so the owner is the token after the
    ``runtime`` prefix. Note [parse_flow_graph_doc] runs the graph id through
    ``safe_assignment_token``, which rewrites the ``:`` separator to ``_`` — so by
    the time this sees a parsed doc's id the prefix reads ``runtime_``. Both the
    raw (``runtime:``) and normalized (``runtime_``) spellings are stripped. A
    graph id without either prefix is treated as the owner token verbatim
    (defensive — a hand-authored or legacy id). The owner scopes this ingest's
    steering authority: a map asserts ONLY edges originating from its owner."""

    token = (graph_id or "").strip()
    for prefix in ("runtime:", "runtime_"):
        if token.startswith(prefix):
            return token[len(prefix):].strip()
    return token


def classify_graph_owner_liveness(
    graph_ids: Iterable[str],
    *,
    live_instance_ids: Collection[str],
) -> dict[str, list[dict[str, Any]]]:
    """Pure: split stored graph ids into ``stale`` and ``held`` on OWNER LIVENESS
    ALONE, each entry carrying a typed reason.

    Graph identity IS the owner instance's id ([owner_instance_id_of]), so a
    stored doc whose owner no longer resolves is an operator canvas addressed to
    an agent the runtime already archived: the launcher can still open it, and
    every consumer that reads it re-materializes a departed agent. That is the
    whole rule.

    **Emptiness is never a signal.** The launcher auto-creates a single
    self-node, zero-edge graph (``requested_by: launcher``) the moment an
    operator opens an agent's canvas — that doc is intended, and reaping
    "empty-looking" graphs would delete a live agent's canvas in the window
    between opening it and drawing the first edge. A graph whose owner resolves
    is ALWAYS held however empty; a graph whose owner does not is stale however
    richly drawn.

    ``live_instance_ids`` is the caller's settled live-row set. This function
    does no id resolution of its own: a caller holding a store can hold back a
    candidate whose owner resolves only through the identity aliases (see
    [GRAPH_HELD_REASON_OWNER_ALIASED]) — the keep side is deliberately the
    forgiving one.
    """

    live = set(live_instance_ids or ())
    stale: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    for graph_id in sorted(graph_ids or []):
        owner = owner_instance_id_of(graph_id)
        entry = {"graph_id": graph_id, "owner_instance_id": owner}
        if owner and owner in live:
            held.append({**entry, "reason": GRAPH_HELD_REASON_OWNER_LIVE})
        else:
            stale.append({**entry, "reason": GRAPH_PRUNE_REASON_OWNER_NOT_LIVE})
    return {"stale": stale, "held": held}


def bound_agent_ids(doc: FlowGraphDoc) -> set[str]:
    """Every persona instance bound to a node in [doc] (unbound nodes excluded)."""

    return {agent for agent in doc.node_bindings.values() if agent}


def owner_scoped_children(doc: FlowGraphDoc, owner_id: str) -> dict[str, bool]:
    """For every BOUND non-owner agent in [doc], whether the OWNER draws a
    steering edge to it — the whole of a map's steering authority.

    Instance X's blueprint declares ``X → child`` edges and nothing else. A child
    the owner draws to maps to ``True`` (assert owner IS in ``steered_by``); a
    bound agent the owner does NOT draw to maps to ``False`` (assert owner is NOT
    in ``steered_by``) — so un-drawing the owner's edge retracts exactly that one
    parent while leaving parents set by OTHER maps intact. Non-owner edges (a
    source node bound to someone other than the owner) never appear here; they are
    accounted separately and never applied.

    The owner is never its own child, so it gets no entry — a map does not steer
    its own owner (whoever steers X does so from THEIR map)."""

    owner_nodes = {
        node_id
        for node_id, agent_id in doc.node_bindings.items()
        if agent_id == owner_id
    }
    steered: set[str] = set()
    for src, dst in doc.edges:
        if src not in owner_nodes:
            continue
        child_agent = doc.node_bindings.get(dst)
        if child_agent and child_agent != owner_id:
            steered.add(child_agent)
    return {
        agent_id: (agent_id in steered)
        for agent_id in bound_agent_ids(doc)
        if agent_id != owner_id
    }


def ignored_non_owner_edges(doc: FlowGraphDoc, owner_id: str) -> list[dict[str, Any]]:
    """Edges the owner-scoped ingest deliberately does NOT apply, each with an
    accounted reason — so a non-owner edge in a (possibly legacy) doc is a
    reported no-op, never a silent drop.

    An edge is non-owner when its source node is bound to a persona OTHER than the
    owner: it is the operator's local layout on THIS map (e.g. a deeper tree drawn
    on one lead's canvas), and the referenced parent's OWN blueprint
    (``runtime:<parent>``) is where it becomes steering. An edge from an UNBOUND
    source has no runtime identity and is reported with its own reason. The
    owner's own edges are applied (see [reconcile_flow_graph_steering]) and never
    listed here."""

    ignored: list[dict[str, Any]] = []
    for src, dst in doc.edges:
        parent_agent = doc.node_bindings.get(src)
        if parent_agent == owner_id:
            continue  # the owner's own edge — applied, not ignored
        child_agent = doc.node_bindings.get(dst)
        if parent_agent is None:
            reason = "edge from an unbound node has no runtime identity"
        else:
            reason = (
                "non-owner edge: a map asserts only its owner's steering; this "
                f"edge belongs to {parent_agent}'s own blueprint"
            )
        ignored.append(
            {
                "from_node": src,
                "to_node": dst,
                "parent_agent": parent_agent,
                "child_agent": child_agent,
                "reason": reason,
            }
        )
    return ignored


def _steered_by_with_owner(
    current: list[str], owner_id: str, *, present: bool
) -> list[str]:
    """[current] steering parents with [owner_id] forced present or absent, every
    OTHER parent preserved in place (order kept).

    This is what makes two leads' maps compose on one child: each map only ever
    toggles its OWN owner in the child's parent list, so A's ingest and B's ingest
    accumulate to ``[A, B]`` instead of overwriting each other."""

    if present:
        return current if owner_id in current else [*current, owner_id]
    return [p for p in current if p != owner_id]


def reconcile_flow_graph_steering(
    doc: FlowGraphDoc,
    *,
    store: PersonaInstanceStore | None = None,
    owner_id: str | None = None,
) -> list[dict[str, Any]]:
    """Assert the OWNER's steering edges from [doc] onto the existing instances it
    references — and ONLY the owner's edges.

    Owner-scoped by design (2026-07-18 per-instance-blueprint ruling):

      * **The owner comes from the graph id** (``runtime:<owner>``, via
        [owner_instance_id_of]) unless passed explicitly. A map is one instance's
        blueprint; it may set or clear ONLY the ``owner → child`` edge in each
        referenced child's ``steered_by``. Parents set by another lead's map (or
        by the goal engine) are preserved — so a child steered by A AND B keeps
        both, and two maps COMPOSE into fan-in instead of clobbering each other.
      * **Never creates.** The chart references instances the runtime already
        made; an unknown reference is reported (``ok: false``), not spawned.
      * **Never touches goal membership.** Writes go through the goal-preserving
        [PersonaInstanceStore.set_parents] / [PersonaInstanceStore.clear_parents]
        — never ``detach_parents`` (which strips the agent's live goal binding).
      * **No-ops are skipped**, so re-ingesting an unchanged doc writes nothing
        and emits no events.

    Non-owner edges are not applied here (see [ignored_non_owner_edges]); the
    owner is never steered by its OWN map, so it gets no entry. One agent's
    failure never aborts the rest; the caller gets a complete per-agent report."""

    store = store or PersonaInstanceStore()
    owner = owner_id if owner_id is not None else owner_instance_id_of(doc.graph_id)
    results: list[dict[str, Any]] = []
    for child_id, owner_steers in owner_scoped_children(doc, owner).items():
        entry: dict[str, Any] = {
            "persona_instance_id": child_id,
            "owner": owner,
            "owner_steers": owner_steers,
        }
        try:
            instance = store.get(child_id)
        except Exception:
            entry.update(
                ok=False,
                changed=False,
                desired_parents=[],
                error="unknown persona instance (flow ingest never creates instances)",
            )
            results.append(entry)
            continue
        current = [p for p in (instance.steered_by or []) if p]
        desired = _steered_by_with_owner(current, owner, present=owner_steers)
        # The child's FULL steered_by after this map's owner edge is applied — the
        # launcher reads this to summarise "steered by …" / "standalone".
        entry["desired_parents"] = list(desired)
        try:
            if desired == current:
                entry.update(ok=True, changed=False, steered_by=current)
            elif desired:
                updated = store.set_parents(child_id, desired, goal_id=None)
                entry.update(ok=True, changed=True, steered_by=list(updated.steered_by))
            else:
                updated = store.clear_parents(child_id)
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

    def stale_dir(self) -> Path:
        """Sibling of the live graph dir where reaped docs land. The runtime
        archives an operator's drawing; it never deletes one — a graph is the
        only record of a map somebody authored by hand."""

        return paths.store_root() / "flow_graphs_stale"

    def archive(self, graph_id: str, archive_dir: Path) -> Path | None:
        """Move one stored doc out of the live dir into [archive_dir] (created on
        demand). Returns the doc's new path, or ``None`` when it is already gone
        — a concurrent reap is a no-op here, never an error."""

        source = self._path(graph_id)
        if not source.exists():
            return None
        archive_dir.mkdir(parents=True, exist_ok=True)
        target = archive_dir / source.name
        shutil.move(str(source), str(target))
        return target


def bound_agent_ids_of_stored(stored: dict[str, Any] | None) -> set[str]:
    """The bound-agent id set of a previously stored doc — best-effort: a
    missing/corrupt prior doc contributes no departures rather than failing
    the read.

    Two callers, one meaning ("who did this doc name?"): the ingest's departure
    set (the doc a new one REPLACES) and the reconciler's graph reap (the doc it
    is about to archive)."""

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
    owner_id: str,
    store: PersonaInstanceStore,
) -> list[dict[str, Any]]:
    """An agent whose node was REMOVED from this map loses ONLY the owner's edge
    (operator decision 2026-07-16: "removing it from the chart removes its
    steering" — narrowed 2026-07-18 to the map's OWNER edge under per-instance
    blueprint ownership).

    Owner-scoped like the main pass: a map has authority over its owner's edges
    alone, so a departure strips ``owner_id`` from the agent's ``steered_by`` and
    preserves every OTHER parent — a foreign parent (the goal engine, another
    lead's map) survives, so two leads' maps never strip each other's edges. Goal
    membership is never touched (goal-preserving clear_parents / set_parents)."""

    results: list[dict[str, Any]] = []
    for agent_id in sorted(departed):
        entry: dict[str, Any] = {
            "persona_instance_id": agent_id,
            "removed_from_map": True,
            "owner": owner_id,
        }
        try:
            instance = store.get(agent_id)
        except Exception:
            # Departed from the map AND gone from the runtime: nothing to strip.
            entry.update(ok=True, changed=False, steered_by=[])
            results.append(entry)
            continue
        current = [p for p in (instance.steered_by or []) if p]
        remaining = [p for p in current if p != owner_id]
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
    stored doc but absent from the new one get the map's OWNER edge stripped
    (see [reconcile_departed_agents]) — so deleting a node on the chart removes
    the owner's steering relation instead of orphaning it runtime-side, while
    parents set by another lead's map survive.

    All steering here is OWNER-SCOPED (the graph id names the owner): the ingest
    asserts only ``owner → child`` edges and reports every non-owner edge it did
    NOT apply (``ignored_non_owner_edges`` — no silent drops)."""

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
    owner = owner_instance_id_of(doc.graph_id)
    graph_store = FlowGraphStore()
    # Departure set comes from the doc this one REPLACES — read before storing.
    previous_bound = bound_agent_ids_of_stored(graph_store.get(doc.graph_id))
    graph_store.set_doc(doc, requested_by=requested_by)

    store = store or PersonaInstanceStore()
    reconciled = reconcile_flow_graph_steering(doc, store=store, owner_id=owner)
    next_bound = bound_agent_ids(doc)
    # The owner is never a "departed child" of its OWN map: whoever steers the
    # owner does so from THEIR map, so this map never strips the owner's parents.
    departed = (previous_bound - next_bound) - {owner}
    if departed:
        reconciled.extend(
            reconcile_departed_agents(
                departed=departed,
                owner_id=owner,
                store=store,
            )
        )
    ignored = ignored_non_owner_edges(doc, owner)
    failed = [entry for entry in reconciled if not entry.get("ok")]
    return {
        "ok": True,
        "graph_id": doc.graph_id,
        "owner_instance_id": owner,
        "stored": True,
        "node_count": len(doc.node_bindings),
        "edge_count": len(doc.edges),
        "bound_agent_count": len(next_bound),
        "removed_from_map_count": len(departed),
        "ignored_non_owner_edge_count": len(ignored),
        "ignored_non_owner_edges": ignored,
        "reconciled": reconciled,
        "changed_count": sum(1 for e in reconciled if e.get("changed")),
        "failed_count": len(failed),
    }
