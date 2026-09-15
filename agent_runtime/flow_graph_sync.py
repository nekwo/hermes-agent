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


def _refusal(key: str, code: str, *, message: str | None = None) -> dict[str, str]:
    return {"key": key, "code": code, "message": message or _REFUSAL_MESSAGES.get(code, code)}


# --- baseline sidecar (never synced, never published) ------------------------
#
# The one IO in this module, below the line on purpose: everything above is pure
# so the allowlist, the projection and the hash stay unit-testable without a
# store. Same two-halves shape as ``persona_instance_sync``, for the same
# reason — one module per synced family beats a pure module and a sidecar module
# that can drift apart.


def flow_graph_baseline_key(graph_id: str) -> str:
    """This family's baseline key. ``flow_graph:<graph id>``, namespaced because
    the drift/revert lane addresses rows by ``FAMILY:CONTAINER:KEY`` and a bare
    graph id would be indistinguishable from a container token."""

    return f"flow_graph:{graph_id}"


def read_flow_graph_baseline(realm_id: str) -> dict[str, str]:
    from . import paths

    path = paths.flow_graph_baseline_path(realm_id)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    return {str(k): str(v) for k, v in entries.items()} if isinstance(entries, dict) else {}


def write_flow_graph_baseline(realm_id: str, entries: dict[str, str]) -> None:
    from utils import atomic_json_write

    from . import paths

    atomic_json_write(
        paths.flow_graph_baseline_path(realm_id),
        {"schema_version": 1, "entries": entries},
        indent=2,
        sort_keys=True,
    )


def update_flow_graph_baseline_after_publish(realm_id: str, projection: FlowGraphProjection) -> None:
    """Record the published canvases' hashes as the new baseline.

    Without it a member who publishes and then pulls reads every canvas they
    just shipped as locally-edited-and-remotely-changed. For this family that is
    worse than a hold on a record: the pull's answer to a two-sided divergence
    is a CONFLICT sidecar, so the publisher would be handed a held drawing over
    content nobody disagreed about.
    """

    baseline = read_flow_graph_baseline(realm_id)
    for graph_id, body_hash in projection.hashes().items():
        baseline[flow_graph_baseline_key(graph_id)] = body_hash
    write_flow_graph_baseline(realm_id, baseline)


# --- pull: adopt or hold, at whole-document granularity ----------------------


def read_projection_document(data: Any) -> dict[str, dict[str, Any]] | None:
    """Parse a pulled ``store/flow_graphs.yaml`` document.

    ``None`` means "this subtree carries no canvas projection" — an older
    publisher, or a realm whose desks never had one drawn. Absence is never a
    removal, and the launcher's version-skew story keys on the distinction.
    """

    if not isinstance(data, dict) or data.get("kind") != PROJECTION_KIND:
        return None
    raw = data.get("graphs")
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def read_remote_flow_graphs(subtree) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Canvas bodies carried by a pulled realm subtree.

    ``(bodies, source)``. ``source`` is ``None`` for an absent projection and
    ``"unreadable"`` for one that exists and will not decode — a distinction the
    caller spends: absence touches nothing, and a parse error is a named refusal
    rather than a delete-shaped decision taken on a read failure.
    """

    from pathlib import Path

    path = Path(subtree).joinpath(*FLOW_GRAPH_PROJECTION_RELATIVE_PATH.split("/"))
    if not path.is_file():
        return {}, None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}, "unreadable"
    parsed = read_projection_document(data)
    if parsed is None:
        return {}, None
    return parsed, "projection"


#: The pulled body is not a canvas ``parse_flow_graph_doc`` will accept.
REFUSAL_INVALID_REMOTE_DOCUMENT = "invalid_remote_canvas"
#: The LOCAL stored canvas exists and will not project. Refused rather than
#: reported absent: absent drives the ADOPT arm, and adopting over a file this
#: machine could not read would overwrite a drawing nobody has seen.
REFUSAL_UNREADABLE_LOCAL = "unreadable_local_canvas"
#: The projection exists in the subtree and will not decode.
REFUSAL_UNREADABLE_PROJECTION = "unreadable_projection"


@dataclass(slots=True)
class FlowGraphPullSummary:
    """Typed accounting for the canvas pull — the contract seam the launcher
    renders, carried on a pull ack as ``result["flow_graph_sync"]``.

    - ``adopted`` — the realm's drawing was written whole, through the store
      door so ``parse_flow_graph_doc`` validated it on the way in.
    - ``converged`` — local already equals remote; nothing written.
    - ``kept_local`` — this machine drew on it and the realm did not.
    - ``held`` — BOTH sides drew. The local canvas is untouched and the remote
      body is parked in a conflict sidecar. A drawing has no natural three-way
      resolution, so this is a loud hold, never a merge.
    - ``upstream_absent`` — the realm no longer carries a canvas this baseline
      says it published. **Never a delete.** A canvas is the only record of a
      map somebody authored by hand, and owner-liveness reaping — which
      ARCHIVES, never deletes — is the one authority that removes one.
    - ``refused`` — a remote body ``parse_flow_graph_doc`` rejects, an
      unreadable local canvas, or an unreadable projection. Per-entity
      isolation: nothing is written, the refusal is named, the pull continues.
    - ``unbound_node_agents`` — nodes bound to instances that did not travel.
      Reported, NEVER dropped: the instance may arrive on the next pull, and
      editing the operator's drawing to fit what this machine happens to know is
      the exact silence this lane exists to close.

    ``source`` is ``None`` when the subtree carries no projection at all.
    """

    adopted: list[str] = field(default_factory=list)
    converged: list[str] = field(default_factory=list)
    kept_local: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    upstream_absent: list[str] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    unbound_node_agents: list[dict[str, str]] = field(default_factory=list)
    source: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.adopted)

    def as_dict(self) -> dict[str, Any]:
        return {
            "adopted": sorted(set(self.adopted)),
            "converged": sorted(set(self.converged)),
            "kept_local": sorted(set(self.kept_local)),
            "held": sorted(set(self.held)),
            "upstream_absent": sorted(set(self.upstream_absent)),
            "refused": list(self.refused),
            "unbound_node_agents": list(self.unbound_node_agents),
            "source": self.source,
        }


def apply_flow_graph_pull(
    realm_id: str,
    subtree,
    *,
    live_instance_ids=None,
) -> FlowGraphPullSummary:
    """Adopt, keep or HOLD each pulled canvas — whole document, never merged.

    Runs inside ``pull_realm_sync`` AFTER ``apply_persona_instance_pull``, and
    the order is the argument rather than a preference: owner-liveness reaping
    archives a canvas whose owner instance is gone, and a canvas that landed
    before its owner was minted looks exactly like that. The ordering is pinned
    by a test, not by this docstring.

    **What this deliberately does not do: steering.** The steering relations
    already travel on ``steered_by`` in the persona-instance family, and running
    ``reconcile_flow_graph_steering`` here would let a pulled drawing rewrite a
    peer's instance records. Non-owner edges in a pulled canvas are the local
    context the operator drew, exactly as they are in a locally authored one:
    reported, never applied.

    ``live_instance_ids`` is the caller's settled live-row set, injected so this
    stays testable without a store; ``None`` asks the instance store.
    """

    from .flow_graph import FlowGraphDocError, FlowGraphStore, parse_flow_graph_doc
    from .sync_merge import PullAction, classify_three_way_pull

    summary = FlowGraphPullSummary()
    remote, source = read_remote_flow_graphs(subtree)
    summary.source = source
    if source is None:
        # Not published. Never a removal — no baselined canvas is touched, so an
        # older peer in the rotation cannot strand this machine's drawings.
        return summary
    if source == "unreadable":
        summary.refused.append(
            _refusal(FLOW_GRAPH_PROJECTION_RELATIVE_PATH, REFUSAL_UNREADABLE_PROJECTION)
        )
        return summary

    store = FlowGraphStore()
    baseline = read_flow_graph_baseline(realm_id)
    live = set(live_instance_ids) if live_instance_ids is not None else _live_instance_ids()
    prefix = flow_graph_baseline_key("")
    baselined = {key[len(prefix):] for key in baseline if key.startswith(prefix)}
    for graph_id in sorted(set(remote) | baselined):
        remote_body = remote.get(graph_id)
        remote_hash = flow_graph_def_hash(remote_body) if remote_body is not None else None
        try:
            local_hash = _local_canvas_hash(store, graph_id)
        except ValueError:
            summary.refused.append(_refusal(graph_id, REFUSAL_UNREADABLE_LOCAL))
            continue
        if remote_body is None:
            # ARCHIVE_LOCAL and edit-vs-remove are the same fact for this family:
            # the realm stopped carrying a drawing this machine still has. Held
            # and named, baseline KEPT so a repaired publish still converges.
            summary.upstream_absent.append(graph_id)
            continue
        decision = classify_three_way_pull(
            local_hash, remote_hash, baseline.get(flow_graph_baseline_key(graph_id))
        )
        if decision.action is PullAction.NOOP:
            summary.converged.append(graph_id)
            continue
        if decision.action is PullAction.KEEP_LOCAL:
            summary.kept_local.append(graph_id)
            continue
        if decision.action is PullAction.CONFLICT:
            summary.held.append(graph_id)
            _write_conflict_sidecar(realm_id, graph_id, remote_body, local_hash, remote_hash)
            continue
        try:
            doc = parse_flow_graph_doc(remote_body)
        except FlowGraphDocError as exc:
            summary.refused.append(
                _refusal(graph_id, REFUSAL_INVALID_REMOTE_DOCUMENT, message=str(exc))
            )
            continue
        store.set_doc(doc, requested_by="realm-sync")
        baseline[flow_graph_baseline_key(graph_id)] = remote_hash
        summary.adopted.append(graph_id)
        for node_id in sorted(doc.node_bindings):
            agent_id = doc.node_bindings[node_id]
            if agent_id and agent_id not in live:
                summary.unbound_node_agents.append(
                    {"graph_id": graph_id, "node": node_id, "agent": agent_id}
                )
    if summary.adopted:
        write_flow_graph_baseline(realm_id, baseline)
    return summary


def _live_instance_ids() -> set[str]:
    """The instance ids this machine can resolve, best-effort.

    A store this process cannot read yields an empty set, which makes every
    binding ``unbound`` — over-reporting, and the safe direction: these rows are
    accounting, and nothing is dropped on their word.
    """

    try:
        from .persona_assignments import PersonaInstanceStore

        return {instance.id for instance in PersonaInstanceStore().scan_all().instances}
    except Exception:  # noqa: BLE001 — accounting, never a failed pull
        return set()


def _local_canvas_hash(store, graph_id: str) -> str | None:
    """The LOCAL canvas's semantic hash, or ``None`` when there is none.

    Raises ``ValueError`` for a stored file that EXISTS and will not project —
    absent is what drives the adopt arm, so folding a broken read into it would
    overwrite a drawing nobody could read.
    """

    stored = store.get(graph_id)
    if stored is None:
        return None
    return flow_graph_def_hash(project_flow_graph(stored, dropped=[]))


def _write_conflict_sidecar(
    realm_id: str,
    graph_id: str,
    remote_body: dict[str, Any],
    local_hash: str | None,
    remote_hash: str | None,
) -> None:
    """Park the drawing a HOLD refused to adopt.

    Best-effort: a sidecar this machine cannot write is not a reason to clobber
    the canvas the hold exists to protect.
    """

    from utils import atomic_json_write

    from . import paths

    try:
        atomic_json_write(
            paths.flow_graph_conflict_path(realm_id, graph_id),
            {
                "schema_version": 1,
                "realm_id": realm_id,
                "graph_id": graph_id,
                "local_hash": local_hash,
                "remote_hash": remote_hash,
                "remote_body": remote_body,
            },
            indent=2,
            sort_keys=True,
        )
    except Exception:  # noqa: BLE001 — the HOLD stands with or without its receipt
        pass
