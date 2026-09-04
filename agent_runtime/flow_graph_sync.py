"""The operator's CANVAS as a realm-syncable projection — the fourth family.

**The ruling this implements** (operator, 2026-09-04): *replicate the canvas
with the agent — nodes, layout and non-owner edges travel in the same sync the
steering already rides. Authored work silently missing on a replica is worse
than the sync cost.* Staged plan:
``docs/agent-runtime-harness/planned/w13-h2-flow-graph-canvas-replication.md``.

Before this module the flow graph was not EXCLUDED from sync, it was outside
every door: ``grep flow_graph`` matched nothing in ``realm_sync``,
``persona_config_sync`` or ``persona_instance_sync``. So this is not a gate
being opened; it is a fourth projection built on the pattern the other three
already share — an allowlist rather than the record, a never-synced per-realm
baseline of semantic hashes, and
:func:`sync_merge.classify_three_way_pull` instead of a clock.

**What travels.** ``nodes[]`` with ``id``/``x``/``y``/``agent`` — ``x``/``y`` ARE
the ruling's layout — and ``edges[]`` with ``from``/``to`` **in the order they
were drawn**, because edge order is fan-in priority. Non-owner edges travel too,
per the ruling: ingest reports and never applies them, and they are exactly the
"local context" the operator drew on that canvas.

**What does not, and why each is refused HERE rather than silently absent.**

* ``viewport`` — the launcher's own contract calls it "a VIEW preference, not
  part of the wiring … never a steering fact"
  (``EterniaLauncher/lib/features/mission_control/flow/agent_flow_graph.dart``,
  ``AgentFlowViewport``). Where the window sat is not the drawing.
* ``updated_at`` / ``requested_by`` — the store envelope's local provenance.
  ``updated_at`` would also make every hash differ on every save, which would
  hand the change detector a permanent edit over identical content.
* any unknown key, at the document, node or edge level. The store keeps unknown
  keys verbatim on purpose (layout is the operator's, not the runtime's), so the
  allowlist is where an unreviewed field is stopped from reaching the wire.

Every one of those is DROPPED WITH ACCOUNTING. An unaccounted drop is the same
silence the replication row exists to close, so ``dropped_keys`` naming
``viewport`` on a publish is the feature, not noise.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import yaml

from .flow_graph import owner_instance_id_of

# --- projection contract ---------------------------------------------------

PROJECTION_KIND = "realm_flow_graphs"
PROJECTION_SCHEMA_VERSION = 1

#: Published relative path of the synthesized projection inside a realm subtree.
#:
#: Unknown to every older hermes: ``_destination_for_sync_path`` returns ``None``
#: for it through the final fallthrough, so an older member SKIPS the artifact
#: rather than writing it somewhere wrong. Degrading to "no canvas replication"
#: is the whole version-skew story, and it is the same door the persona-instance
#: projection walked through.
FLOW_GRAPH_PROJECTION_RELATIVE_PATH = "store/flow_graphs.yaml"

#: The only top-level document keys that may leave this machine.
FLOW_GRAPH_ALLOWED_KEYS: frozenset[str] = frozenset({"edges", "graph_id", "nodes"})

#: The only node fields that travel. ``x``/``y`` are the ruling's layout;
#: ``agent`` is the binding the receiver may or may not be able to resolve yet
#: (see the pull's ``unbound_node_agents``).
FLOW_GRAPH_NODE_KEYS: frozenset[str] = frozenset({"agent", "id", "x", "y"})

#: The only edge fields that travel. An edge is a pair, and nothing else.
FLOW_GRAPH_EDGE_KEYS: frozenset[str] = frozenset({"from", "to"})

#: The store blob is not a mapping, or its ``doc`` is not one. A corrupt or
#: hand-edited file must not fail a whole publish.
REFUSAL_UNREADABLE_DOCUMENT = "unreadable_document"
#: No ``graph_id`` on the stored envelope: nothing to key the merge on.
REFUSAL_MISSING_GRAPH_ID = "missing_graph_id"
#: The stored ``graph_id`` names a DIFFERENT owner than the desk it was resolved
#: for. Graph identity IS the owner instance's id, so publishing this under the
#: requested owner would replicate a mislabel.
REFUSAL_GRAPH_OWNER_MISMATCH = "graph_owner_mismatch"


@dataclass(frozen=True, slots=True)
class FlowGraphProjection:
    """The synthesized, publishable canvas document plus its accounting.

    ``graphs`` is keyed by graph id — the merge key, and the same key the store
    files under. Everything else answers "what did this publish not ship":

    - ``dropped_keys`` — every field the allowlist removed, as a dotted path.
      ``viewport`` and the two provenance fields appear here whenever they were
      present, and that is deliberate.
    - ``unreadable`` — owners whose stored blob could not be projected, as
      ``{key, code, message}`` (the shared ``Refusal`` shape). An owner with no
      canvas at all is NOT one of these: most desks never had one drawn, so
      absence is the normal case and silence is correct for it.
    """

    graphs: dict[str, dict[str, Any]] = field(default_factory=dict)
    dropped_keys: list[str] = field(default_factory=list)
    unreadable: list[dict[str, str]] = field(default_factory=list)

    def document(self) -> dict[str, Any]:
        return {
            "graphs": self.graphs,
            "kind": PROJECTION_KIND,
            "schema_version": PROJECTION_SCHEMA_VERSION,
        }

    def to_bytes(self) -> bytes:
        """Deterministic bytes: sorted keys, block style, LF. Republishing an
        unchanged projection is a byte-for-byte no-op, so the publish
        change-detector (``_published_artifacts_differ``) stays honest.

        Node and edge LISTS keep their order — ``sort_keys`` sorts mappings, not
        sequences — which is what preserves fan-in priority on the wire.
        """

        text = yaml.safe_dump(
            self.document(),
            sort_keys=True,
            default_flow_style=False,
            allow_unicode=True,
            width=4096,
        )
        return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")

    def hashes(self) -> dict[str, str]:
        return {graph_id: flow_graph_def_hash(body) for graph_id, body in self.graphs.items()}

    def as_dict(self) -> dict[str, Any]:
        return {
            "graphs": sorted(self.graphs),
            "dropped_keys": sorted(set(self.dropped_keys)),
            "unreadable": list(self.unreadable),
        }


def flow_graph_def_hash(body: Any) -> str:
    """Semantic content hash of one projected canvas (key-order independent).

    Timestamp-free BY CONSTRUCTION rather than by exclusion list: ``updated_at``
    never enters the projected body in the first place, so there is no clock
    here to remember to skip. List ORDER is inside the hash on purpose — two
    canvases with the same edges drawn in a different order have different
    fan-in priority and are genuinely different drawings.
    """

    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def graph_id_for_owner(owner_instance_id: str) -> str:
    """The store key for a desk's canvas. Graph identity IS the owner's id."""

    return f"runtime:{str(owner_instance_id or '').strip()}"


def _wire_scalar(value: Any) -> Any:
    """One node/edge field as it travels: plain JSON scalars only.

    Anything else — a datetime, a nested object someone wedged into a node — is
    a ``TypeError`` here and a dropped key at the caller. Determinism is
    load-bearing for both the change detector and the content hash, and a canvas
    has no field that needs richer shapes.
    """

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(type(value).__name__)


def _project_entries(
    entries: Any,
    *,
    allowed: frozenset[str],
    prefix: str,
    label_of,
    dropped: list[str],
) -> list[dict[str, Any]]:
    """Allowlist one ordered list of node/edge mappings, accounting every drop.

    Order is preserved because it is meaning: edge order is fan-in priority, and
    node order is the order the operator laid them out.
    """

    if not isinstance(entries, list):
        if entries is not None:
            dropped.append(prefix)
        return []
    projected: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            dropped.append(f"{prefix}[{index}]")
            continue
        label = label_of(entry) or f"[{index}]"
        body: dict[str, Any] = {}
        for key in sorted(str(name) for name in entry):
            if key not in allowed:
                dropped.append(f"{prefix}.{label}.{key}")
                continue
            try:
                body[key] = _wire_scalar(entry[key])
            except TypeError:
                dropped.append(f"{prefix}.{label}.{key}")
        projected.append(body)
    return projected


def project_flow_graph(stored: Any, *, dropped: list[str] | None = None) -> dict[str, Any]:
    """Project ONE stored canvas through the allowlist.

    ``stored`` is the store's envelope — ``{graph_id, doc, updated_at,
    requested_by}`` — of which only ``doc`` is authored. Raises ``ValueError``
    on a blob that cannot be keyed at all; :func:`project_flow_graphs` is the
    caller that turns that into an accounted refusal rather than a failed
    publish.
    """

    accounting = dropped if dropped is not None else []
    if not isinstance(stored, dict):
        raise ValueError(REFUSAL_UNREADABLE_DOCUMENT)
    graph_id = str(stored.get("graph_id") or "").strip()
    if not graph_id:
        raise ValueError(REFUSAL_MISSING_GRAPH_ID)
    doc = stored.get("doc")
    if not isinstance(doc, dict):
        raise ValueError(REFUSAL_UNREADABLE_DOCUMENT)

    prefix = f"graphs.{graph_id}"
    for key in sorted(str(name) for name in stored):
        if key not in ("doc", "graph_id"):
            accounting.append(f"{prefix}.{key}")

    body: dict[str, Any] = {"graph_id": graph_id}
    for key in sorted(str(name) for name in doc):
        if key not in FLOW_GRAPH_ALLOWED_KEYS:
            accounting.append(f"{prefix}.{key}")
    body["nodes"] = _project_entries(
        doc.get("nodes"),
        allowed=FLOW_GRAPH_NODE_KEYS,
        prefix=f"{prefix}.nodes",
        label_of=lambda entry: str(entry.get("id") or "").strip(),
        dropped=accounting,
    )
    body["edges"] = _project_entries(
        doc.get("edges"),
        allowed=FLOW_GRAPH_EDGE_KEYS,
        prefix=f"{prefix}.edges",
        label_of=lambda entry: (
            f"{str(entry.get('from') or '').strip()}->{str(entry.get('to') or '').strip()}"
        ),
        dropped=accounting,
    )
    return body


def project_flow_graphs(
    owner_instance_ids,
    *,
    docs: dict[str, Any] | None = None,
) -> FlowGraphProjection:
    """Build the portable canvas projection for exactly ``owner_instance_ids``.

    ``docs`` maps OWNER INSTANCE ID to that desk's stored blob, so the store
    lookup stays with the caller that holds the store and this stays pure. The
    owner set is the publish's own desk list — never a second walk of the graph
    directory, which is how an artifact list and its subject came apart last
    time.
    """

    resolved = docs or {}
    graphs: dict[str, dict[str, Any]] = {}
    dropped: list[str] = []
    unreadable: list[dict[str, str]] = []
    for owner in sorted({str(item) for item in (owner_instance_ids or ())}):
        stored = resolved.get(owner)
        if stored is None:
            continue
        # Ownership is checked BEFORE the projection runs: a mislabelled blob
        # must not leave its dropped keys on a publish row that then refuses it.
        stored_graph_id = str(stored.get("graph_id") or "").strip() if isinstance(stored, dict) else ""
        if stored_graph_id and owner_instance_id_of(stored_graph_id) != owner_instance_id_of(owner):
            unreadable.append(_refusal(owner, REFUSAL_GRAPH_OWNER_MISMATCH))
            continue
        try:
            body = project_flow_graph(stored, dropped=dropped)
        except ValueError as exc:
            unreadable.append(_refusal(owner, str(exc)))
            continue
        graphs[body["graph_id"]] = body
    return FlowGraphProjection(graphs=graphs, dropped_keys=dropped, unreadable=unreadable)


_REFUSAL_MESSAGES = {
    REFUSAL_UNREADABLE_DOCUMENT: "stored canvas is not a readable document",
    REFUSAL_MISSING_GRAPH_ID: "stored canvas has no graph_id to key on",
    REFUSAL_GRAPH_OWNER_MISMATCH: "stored canvas names a different owner instance",
}


def _refusal(key: str, code: str) -> dict[str, str]:
    return {"key": key, "code": code, "message": _REFUSAL_MESSAGES.get(code, code)}
