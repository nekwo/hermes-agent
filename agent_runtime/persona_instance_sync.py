"""Portable persona-INSTANCE projection for realm sync — the record split.

A realm pull already delivers the desk (``office_sync``), the persona definition
(``persona_config_sync``), the profile home and its files
(``profile_artifact_sync``) and the skills the persona names. It does not
deliver the **agent**: ``grep persona_instance agent_runtime/realm_sync.py``
returned nothing before this module existed, so a pulled placement landed on a
receiving machine with no runtime instance behind it and the launcher badged the
desk "Not linked here — this machine has no runtime instance for it".

The operator's ruling (2026-08-31, plan
``docs/mission_control/planned/instance-replication.md`` §0):

    Workspaces are SHARED live objects across realm members. Syncing one must
    bring working agents. A pulled desk whose instance is absent locally should
    mint/recreate the instance here — identity and definition travel; runtime
    state (sessions, worktrees, credentials, machine roots) is born fresh
    locally and never travels.

That sentence is a SPLIT of one 32-field record, and a category ("runtime
state") is exactly the kind of shorthand that lets one field drift to the wrong
side of a door. So the split is spelled per FIELD, three disjoint sets whose
union is every field of :class:`~agent_runtime.models.PersonaInstance`:

* :data:`PERSONA_INSTANCE_ALLOWED_KEYS` — 14 fields of realm-wide identity and
  authored definition. These and only these leave the machine.
* :data:`PERSONA_INSTANCE_DERIVED_KEYS` — 6 fields the MINT re-derives locally
  (this box's store root, the pulled definition's role, the profile the pull's
  own artifact lane materialized, a fresh idle state, a fresh durable chat root,
  now()).
* :data:`PERSONA_INSTANCE_LOCAL_ONLY_KEYS` — 12 fields that are neither carried
  nor re-derived: live execution bindings, writer-less telemetry, the
  conversation-adjacent steer text, the structural version.

:data:`PERSONA_INSTANCE_NEVER_TRAVELS_KEYS` is the union of the last two (18),
because "does this leave the machine" and "is this re-derived on arrival" are
two different questions about the same field and both have to be answerable.

``tests/agent_runtime/test_persona_instance_sync.py`` asserts the partition is
TOTAL over ``dataclasses.fields(PersonaInstance)``. A field added tomorrow
cannot compile green without somebody classifying it here — which is the whole
point of this stage, and the one guard that keeps the split from rotting the way
an English category would.

Everything in this module is pure with respect to its inputs (records are
injectable, nothing is read from disk, no git) so the allowlist, the projection,
the hash and the admission grammar are unit-testable without a store.

Where the projection is PUBLISHED and how it is merged on pull live in
``realm_sync`` (the publish scan) and ``persona_instance_pull`` semantics inside
``realm_sync.apply_persona_instance_pull`` — this module owns only the contract.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import MISSING, dataclass, field, fields
from typing import Any

import yaml

from .models import PersonaInstance, looks_like_persona_instance_id

# --- projection contract ---------------------------------------------------

PROJECTION_KIND = "realm_persona_instances"
PROJECTION_SCHEMA_VERSION = 1

#: Published relative path of the synthesized projection inside a realm subtree.
#:
#: Unknown to every older hermes: ``_destination_for_sync_path`` returns ``None``
#: for it through the final fallthrough, so an older member SKIPS the artifact
#: rather than writing it somewhere wrong. Degrading to "no instance
#: replication" is the whole version-skew story on the pull side, and it is what
#: the launcher's L1/L2 stages key their badge demotion on.
PROJECTION_RELATIVE_PATH = "store/persona_instances.yaml"

#: The ONLY persona-instance fields that may leave this machine (plan §1.1).
#:
#: Opt-in, never opt-out: a key outside this set is DROPPED with accounting on
#: publish and REFUSED at the pull door (``unexpected_key``). Every entry earns
#: its place, and the four that look like runtime state but are not:
#:
#: - ``mode`` is the instance's LANE (``configured`` / ``free_floating``), read
#:   by ``operator_channels`` / ``persona_chat_history`` /
#:   ``persona_instance_identity``. Definitional, not live.
#: - ``spawned_by`` is a PROVENANCE scalar, not a steering edge —
#:   ``PersonaInstance.__post_init__`` already guards it from being read as one.
#: - ``realm_id`` / ``workspace_id`` are scope-provenance and are realm-wide by
#:   construction. The launcher's scope policy REFUSES on their absence
#:   (``realmOnly`` / ``foreignWorkspace``), so a mint without them would swap
#:   one badge for another rather than link the desk.
#: - ``model_override_issued_at`` is a supersession CLOCK and travels WITH the
#:   four override fields it orders, or a stale local write silently wins on the
#:   receiver (``StaleModelOverrideWrite``).
PERSONA_INSTANCE_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "api_mode",
        "display_name",
        "id",
        "mode",
        "model",
        "model_override_issued_at",
        "persona_id",
        "provider",
        "realm_id",
        "reasoning_effort",
        "skill_overrides",
        "spawned_by",
        "steered_by",
        "workspace_id",
    }
)

#: Never travels, and RE-DERIVED locally by the mint (plan §1.3).
#:
#: Distinct from :data:`PERSONA_INSTANCE_LOCAL_ONLY_KEYS` because these six are
#: not merely withheld — a replica that left them empty would not be a working
#: agent. ``role`` and ``profile_id`` are derived rather than carried on purpose:
#: travelling either would let a stale copy shadow the persona definition that
#: arrived in the SAME pull.
PERSONA_INSTANCE_DERIVED_KEYS: frozenset[str] = frozenset(
    {
        "default_chat_session_id",
        "profile_id",
        "role",
        "runtime_root",
        "state",
        "updated_at",
    }
)

#: Never travels and never re-derived: born at the dataclass default (plan §1.2).
#:
#: Live execution bindings (``active_run_id`` and its four siblings) are the
#: sharp ones — ``_has_live_binding`` reads them to refuse retiring a working
#: agent, so importing a peer's binding would make a replica look busy with a
#: run this machine has never heard of. ``current_chat_goal`` is here by the
#: 2026-08-31 ruling (`[AUDIT]` in the plan): it is conversation-adjacent like
#: ``chat_head_home``, and a wrong "travels" is a clobber where a wrong "never"
#: is only an absence.
PERSONA_INSTANCE_LOCAL_ONLY_KEYS: frozenset[str] = frozenset(
    {
        "active_run_id",
        "chat_head_home",
        "current_assignment_id",
        "current_chat_goal",
        "current_task_id",
        "goal_id",
        "last_heartbeat_at",
        "returned_to",
        "schema_version",
        "session_id",
        "skill_manifest_hash",
        "token_budget_used",
    }
)

#: Everything that does not travel — the union of the two sets above.
PERSONA_INSTANCE_NEVER_TRAVELS_KEYS: frozenset[str] = (
    PERSONA_INSTANCE_DERIVED_KEYS | PERSONA_INSTANCE_LOCAL_ONLY_KEYS
)

#: The value an ADOPT writes into a travelling field the remote body omits.
#:
#: Adoption takes the remote's travelling SURFACE wholesale — a field the
#: publisher cleared must clear here too, or a locally-stale override would
#: outlive the write that removed it upstream. Defaults come from the dataclass
#: itself so this table cannot disagree with the record; ``display_name`` is the
#: one travelling field with no default (it is a bare ``str``), and its
#: structural empty is ``""``.
_TRAVEL_CLEARED_WITHOUT_DEFAULT: dict[str, Any] = {"display_name": ""}


def _dataclass_defaults() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in fields(PersonaInstance):
        if item.default is not MISSING:
            out[item.name] = item.default
        elif item.default_factory is not MISSING:  # type: ignore[misc]
            out[item.name] = item.default_factory()  # type: ignore[misc]
    return out


def cleared_travel_value(name: str) -> Any:
    """What an adopt writes for a travelling field the remote body omits.

    ``id`` and ``persona_id`` never reach here — an admitted body must carry
    both (:func:`refuse_persona_instance`), because they are the merge key and
    the definition pointer, not overrides that can be cleared.
    """

    defaults = _dataclass_defaults()
    if name in defaults:
        value = defaults[name]
        return list(value) if isinstance(value, list) else value
    return _TRAVEL_CLEARED_WITHOUT_DEFAULT[name]


# NO import-time assertion of the partition, deliberately. The totality gate is
# ``tests/agent_runtime/test_persona_instance_sync.py::
# test_every_persona_instance_field_is_classified``, and an assert here would
# pre-empt it: an unclassified field would surface as a COLLECTION error on
# every suite that imports this module rather than as one named failure saying
# which field nobody classified. The plan calls that test "the point of this
# stage"; a guard that steals its red makes the stage's own signal unreadable.


# --- refusal codes (plan §4) -------------------------------------------------

#: The id is not instance-shaped, or would be MANGLED into a different filename
#: by ``paths.safe_path_token`` — which is the same thing as the merge unit
#: being written under a key that is not the key the realm agreed on.
REFUSAL_INVALID_INSTANCE_ID = "invalid_instance_id"
#: A key outside :data:`PERSONA_INSTANCE_ALLOWED_KEYS`. The session/run family
#: (``default_chat_session_id``, ``session_id``, ``chat_head_home``,
#: ``active_run_id``, ``current_task_id``, ``current_assignment_id``,
#: ``goal_id``) lands here.
REFUSAL_UNEXPECTED_KEY = "unexpected_key"
#: A peer published a CANONICAL channel row. Canonical rows are derived locally
#: on every machine from a persona id that already travels, so a peer writing
#: one is either an older id scheme or an attack. Replication is scoped to
#: placement-backed rows (plan §2), which is what keeps the queued
#: global-singleton redesign un-blocking.
REFUSAL_CANONICAL_CHANNEL = "canonical_channel_not_replicable"
#: A ``steered_by`` entry that is not instance-shaped — the same guard
#: ``__post_init__`` spends to keep a principal from rendering as a parent edge.
REFUSAL_STEERING_SHAPE = "steering_parent_not_instance_shaped"
#: The body has no ``id``/``persona_id``, so there is nothing to key or build
#: from. Refused rather than accounted: unlike a persona definition, an instance
#: row with no persona pointer cannot be adopted at all.
REFUSAL_INCOMPLETE = "incomplete_instance"


# --- projection ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PersonaInstanceProjection:
    """The synthesized, publishable persona-instance document.

    ``instances`` is the pruned + allowlisted map. Everything else is accounting,
    on the ``PersonaConfigProjection.dropped_keys`` precedent — a publish must
    never be able to ship a partial record and report a clean result.

    - ``dropped_keys`` — every field the allowlist removed, as a dotted path.
      ``runtime_root`` appears here on every publish, and that is the point: the
      most portability-hostile field on the record is reported as withheld
      rather than silently absent.
    - ``skipped_canonical`` — ids excluded because they ARE the persona's
      canonical operator channel. Not a refusal: every machine derives its own,
      so withholding them is the scoping ruling working (plan §2).
    - ``refused`` — records this machine would not project: a travelling field
      holding a machine-shaped value, or an id that is not instance-shaped. Rows
      are ``{key, code, message}``, the shared ``Refusal`` shape.
    - ``missing`` — wanted ids with no resolvable record.
    """

    instances: dict[str, dict[str, Any]] = field(default_factory=dict)
    dropped_keys: list[str] = field(default_factory=list)
    skipped_canonical: list[str] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def document(self) -> dict[str, Any]:
        return {
            "instances": self.instances,
            "kind": PROJECTION_KIND,
            "schema_version": PROJECTION_SCHEMA_VERSION,
        }

    def to_bytes(self) -> bytes:
        """Deterministic bytes: sorted keys, block style, LF. Republishing an
        unchanged projection is a byte-for-byte no-op, so the publish
        change-detector (``_published_artifacts_differ``) stays honest."""

        text = yaml.safe_dump(
            self.document(),
            sort_keys=True,
            default_flow_style=False,
            allow_unicode=True,
            width=4096,
        )
        return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")

    def hashes(self) -> dict[str, str]:
        return {
            instance_id: persona_instance_def_hash(body)
            for instance_id, body in self.instances.items()
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "instances": sorted(self.instances),
            "dropped_keys": sorted(set(self.dropped_keys)),
            "skipped_canonical": sorted(set(self.skipped_canonical)),
            "refused": list(self.refused),
            "missing": sorted(set(self.missing)),
        }


def persona_instance_def_hash(body: Any) -> str:
    """Semantic content hash of one projected instance (key-order independent).

    Nothing timestamp-shaped is in the projection except
    ``model_override_issued_at``, which is DELIBERATELY hashed: it is the
    supersession clock for the override tier, so a body whose clock moved is a
    body that changed. ``updated_at`` — the local write clock — never enters,
    for the reason ``office_models._HASH_EXCLUDE`` states.
    """

    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _wire_value(value: Any) -> Any:
    """One field as it travels: plain YAML/JSON scalars and containers only.

    ``datetime`` is rendered through ``serde.to_jsonable``'s spelling (ISO-8601
    microseconds, ``Z``) rather than refused, because ``model_override_issued_at``
    MUST travel and ``from_jsonable`` parses exactly that shape back. Anything
    else exotic raises ``TypeError`` and is dropped with accounting — determinism
    is load-bearing for the change detector and the content hash.
    """

    from datetime import datetime

    if isinstance(value, datetime):
        from .serde import to_jsonable

        return to_jsonable(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_wire_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _wire_value(item) for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))}
    raise TypeError(type(value).__name__)


def project_persona_instance(record: Any, *, dropped: list[str] | None = None) -> dict[str, Any]:
    """Project ONE persona-instance record through the allowlist.

    Field names come FROM the record (dataclass fields when it is one), never
    from a hard-coded list — the ``_record_field_names`` precedent, and what
    keeps a newly added ``PersonaInstance`` field accounted as a drop the day it
    lands rather than silently invisible.

    ``None`` and structurally-empty values are omitted: on this record ``None``
    means "inherit" for the override tier and "runtime-global" for the scope
    pointers, so writing the absence would only make two equivalent bodies hash
    differently. ``id`` and ``persona_id`` are always present — they are the
    merge key and the definition pointer.
    """

    accounting = dropped if dropped is not None else []
    instance_id = str(getattr(record, "id", "") or "")
    try:
        names = sorted(item.name for item in fields(record))
    except TypeError:
        names = sorted(name for name in dir(record) if not name.startswith("_"))
    body: dict[str, Any] = {}
    for name in names:
        if name not in PERSONA_INSTANCE_ALLOWED_KEYS:
            accounting.append(f"instances.{instance_id}.{name}")
            continue
        value = getattr(record, name, None)
        if name not in ("id", "persona_id") and (value is None or value == [] or value == {}):
            continue
        try:
            body[name] = _wire_value(value)
        except TypeError:
            accounting.append(f"instances.{instance_id}.{name}")
    return body


def project_persona_instances(
    instance_ids: list[str] | set[str],
    *,
    records: dict[str, Any] | None = None,
) -> PersonaInstanceProjection:
    """Build the portable projection for exactly ``instance_ids``.

    Pruning to the wanted set is part of the contract: the projection must carry
    the instances the published office actors actually reference and nothing
    else. ``realm_sync._office_publish_scan`` resolves that set in the SAME walk
    that resolves the persona ids — one walk, one authority — because a second
    glob is how the artifact list and the persona list came apart last time.

    Two exclusions, and they are different facts:

    * a CANONICAL channel row is skipped (``skipped_canonical``). Every member
      derives its own; publishing one would be a peer asserting a row the
      receiver's own ``ensure_for_personas`` already owns.
    * a record whose projected body still carries a machine-shaped value is
      REFUSED (``nonportable_path``). Withholding ``runtime_root`` is the
      allowlist's job; catching an absolute path that reached the wire through
      an authored ``display_name`` is this one's.
    """

    from .persona_assignments import is_canonical_persona_channel
    from .persona_config_sync import find_nonportable_values

    records = records or {}
    instances: dict[str, dict[str, Any]] = {}
    dropped: list[str] = []
    skipped_canonical: list[str] = []
    refused: list[dict[str, str]] = []
    missing: list[str] = []
    for instance_id in sorted({str(item) for item in instance_ids}):
        record = records.get(instance_id)
        if record is None:
            missing.append(instance_id)
            continue
        if not valid_persona_instance_id(instance_id):
            refused.append(
                {
                    "key": instance_id,
                    "code": REFUSAL_INVALID_INSTANCE_ID,
                    "message": "instance id is not a safe persona-instance path token",
                }
            )
            continue
        try:
            if is_canonical_persona_channel(record):
                skipped_canonical.append(instance_id)
                continue
        except Exception:  # noqa: BLE001 — a record that cannot answer is not projectable
            refused.append(
                {
                    "key": instance_id,
                    "code": REFUSAL_INCOMPLETE,
                    "message": "record could not be classified against the canonical channel",
                }
            )
            continue
        body = project_persona_instance(record, dropped=dropped)
        if not body.get("persona_id"):
            refused.append(
                {
                    "key": instance_id,
                    "code": REFUSAL_INCOMPLETE,
                    "message": "record carries no persona_id to build from",
                }
            )
            continue
        offenders = find_nonportable_values(body, prefix=f"instances.{instance_id}")
        if offenders:
            refused.append(
                {
                    "key": instance_id,
                    "code": "nonportable_path",
                    "message": "machine-shaped value(s): "
                    + ", ".join(row["key"] for row in offenders),
                }
            )
            continue
        instances[instance_id] = body
    return PersonaInstanceProjection(
        instances=instances,
        dropped_keys=sorted(set(dropped)),
        skipped_canonical=sorted(set(skipped_canonical)),
        refused=refused,
        missing=sorted(set(missing)),
    )


def read_projection_document(data: Any) -> dict[str, dict[str, Any]] | None:
    """Parse a pulled ``store/persona_instances.yaml`` document.

    ``None`` means "this subtree carries no instance projection" — an older
    publisher, or a realm that publishes no placement-backed rows. Absence is
    never a removal (plan §3.3 ``upstream_absent``), and the caller keys its
    whole version-skew story on this distinction.
    """

    if not isinstance(data, dict) or data.get("kind") != PROJECTION_KIND:
        return None
    raw = data.get("instances")
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


# --- admission (plan §4) ------------------------------------------------------


def instance_relative_path(instance_id: str) -> str:
    """The relative path an instance row occupies under the store root.

    Synthesized so the SHARED ``sync_admission.path_refusal`` — which already
    covers traversal, absolute/drive-letter/UNC shapes and Windows reserved
    device names — can be asked about an untrusted id, rather than this lane
    growing a second spelling of those rules.
    """

    return f"persona_instances/{instance_id}.json"


def valid_persona_instance_id(instance_id: str) -> bool:
    """Is this id both instance-shaped and safe as its own filename?

    The second half is the load-bearing one and it is asked by ROUND-TRIP rather
    than by regex: ``paths.persona_instance_path`` writes through
    ``safe_path_token``, so an id that token would rewrite is an id whose row
    lands under a key that is NOT the key the realm agreed on — the merge unit
    silently renamed, which is exactly the non-convergence Option B was refused
    for. Asking the real sanitizer instead of a second pattern means the two can
    never disagree.
    """

    from . import paths
    from .sync_admission import path_refusal

    text = str(instance_id or "")
    if not looks_like_persona_instance_id(text):
        return False
    if paths.safe_path_token(text) != text:
        return False
    return path_refusal(instance_relative_path(text)) is None


def refuse_persona_instance(instance_id: str, body: Any):
    """One admission decision for one pulled instance row, or ``None``.

    Order is deliberate: the ID first (a body keyed by an unsafe id has nothing
    worth scanning), then the SHARED ``sync_admission`` door — secret-shaped
    assignments through BOTH scanner passes and the machine-shaped-value walk —
    and only then the three rules this family adds on top of it. That ordering
    is what makes the plan's §4 table literally true: a ``runtime_root`` in the
    body reports ``nonportable_path`` (the door saw it first) while a
    ``session_id`` reports ``unexpected_key`` (nothing shared objects to it).

    ``prose_keys=frozenset()`` because an instance body is 100% wiring — its
    keys ARE an allowlist — so nothing here is exempt prose, the persona-
    definition lane's argument for the same choice.

    Per-entity isolation: the caller refuses THIS row, names it, and keeps
    pulling.
    """

    from .persona_assignments import persona_instance_id_for
    from .sync_admission import Refusal, refuse_entity

    if not valid_persona_instance_id(instance_id):
        return Refusal(
            instance_id,
            REFUSAL_INVALID_INSTANCE_ID,
            f"not a safe persona-instance id: {instance_id!r}",
        )
    if not isinstance(body, dict):
        return Refusal(instance_id, REFUSAL_INCOMPLETE, "instance body is not a mapping")

    refusal = refuse_entity(
        instance_id,
        payload=body,
        prefix=f"instances.{instance_id}",
        prose_keys=frozenset(),
    )
    if refusal is not None:
        return refusal

    unexpected = sorted(str(key) for key in body if str(key) not in PERSONA_INSTANCE_ALLOWED_KEYS)
    if unexpected:
        return Refusal(
            instance_id,
            REFUSAL_UNEXPECTED_KEY,
            "key(s) outside the instance allowlist: " + ", ".join(unexpected),
        )

    persona_id = str(body.get("persona_id") or "")
    declared_id = str(body.get("id") or "")
    if not persona_id or not declared_id:
        return Refusal(
            instance_id, REFUSAL_INCOMPLETE, "instance body carries no id/persona_id"
        )
    if declared_id != instance_id:
        return Refusal(
            instance_id,
            REFUSAL_INVALID_INSTANCE_ID,
            f"body id {declared_id!r} disagrees with its published key",
        )
    if instance_id == persona_instance_id_for(persona_id):
        return Refusal(
            instance_id,
            REFUSAL_CANONICAL_CHANNEL,
            (
                f"{instance_id} is the canonical operator channel for {persona_id!r}; "
                "canonical rows are derived locally on every machine and never replicate"
            ),
        )

    steered_by = body.get("steered_by")
    if steered_by is not None:
        if not isinstance(steered_by, list) or not all(
            looks_like_persona_instance_id(item) for item in steered_by
        ):
            return Refusal(
                instance_id,
                REFUSAL_STEERING_SHAPE,
                "steered_by must be a list of persona-instance ids",
            )
    return None


# --- baseline sidecar (never synced, never published) ------------------------
#
# The one IO in this module, and it sits below the line on purpose: everything
# above is pure so the allowlist, the projection, the hash and the admission
# grammar stay unit-testable without a store. This is the same two-halves shape
# ``persona_config_sync`` has, for the same reason — one module per synced
# family beats a pure module and a sidecar module that can drift apart.


def read_persona_instance_baseline(realm_id: str) -> dict[str, str]:
    from . import paths

    path = paths.persona_instance_baseline_path(realm_id)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    return {str(k): str(v) for k, v in entries.items()} if isinstance(entries, dict) else {}


def write_persona_instance_baseline(realm_id: str, entries: dict[str, str]) -> None:
    from utils import atomic_json_write

    from . import paths

    atomic_json_write(
        paths.persona_instance_baseline_path(realm_id),
        {"schema_version": 1, "entries": entries},
        indent=2,
        sort_keys=True,
    )


def read_dropped_steering_ledger(realm_id: str) -> dict[str, dict[str, Any]]:
    """Edges phase two dropped for an ABSENT PARENT, and what they were dropped
    against — ``{instance_id: {"parents": [...], "remote_hash": "..."}}``.

    The durable half of the heal (the H3 known gap, closed 2026-09-02). Read
    beside the baseline, written by the same pass, never synced and never
    published.

    A malformed or unreadable file yields ``{}``, and that is the safe
    direction: the ledger is a repair aid, so its failure mode must be "no heal
    is attempted", never "an edge is re-applied on the word of a body nobody can
    vouch for". Entries missing either half are dropped for the same reason —
    the parent alone cannot say whether the realm has moved since.
    """

    from . import paths

    path = paths.persona_instance_dropped_steering_path(realm_id)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}
    ledger: dict[str, dict[str, Any]] = {}
    for instance_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        parents = [str(item) for item in (entry.get("parents") or []) if item]
        remote_hash = entry.get("remote_hash")
        if not parents or not isinstance(remote_hash, str):
            continue
        ledger[str(instance_id)] = {"parents": parents, "remote_hash": remote_hash}
    return ledger


def write_dropped_steering_ledger(realm_id: str, entries: dict[str, dict[str, Any]]) -> None:
    """Replace the heal ledger with what the pass that just ran phase two saw.

    REBUILT, never merged, and that is what keeps it from rotting: an entry
    survives only while the same pass drops the same edge again, so a healed
    edge, a row the realm stopped carrying, a row that went to HOLD and a row
    whose remote body moved all clear themselves with no expiry rule and no
    second pruning walk. The two callers that return BEFORE phase two — an older
    peer's absent projection, and an unreadable one — deliberately do NOT call
    this: neither is evidence about a dropped edge, and forgetting every one of
    them on a single pull in a rotation would silently end the heal.
    """

    from utils import atomic_json_write

    from . import paths

    atomic_json_write(
        paths.persona_instance_dropped_steering_path(realm_id),
        {"schema_version": 1, "entries": entries},
        indent=2,
        sort_keys=True,
    )


def instance_baseline_key(instance_id: str) -> str:
    """This family's baseline key. ``instance:<id>``, namespaced because the
    drift/revert lane addresses rows by ``FAMILY:CONTAINER:KEY`` and a bare id
    would be indistinguishable from a container token."""

    return f"instance:{instance_id}"


def instance_conflict_path(realm_id: str, instance_id: str):
    """Where a HELD row's remote body is parked.

    Under the realm-sync root, beside the baseline, so it is never-synced and
    never-published by construction. The office lane's ``conflicts/`` precedent:
    a hold that leaves no copy of what it refused to adopt makes the operator's
    only exit "pull again and hope".
    """

    from . import paths

    return (
        paths.realm_sync_root()
        / paths.safe_path_token(realm_id)
        / "persona_instance_conflicts"
        / f"{paths.safe_path_token(instance_id)}.json"
    )


def update_persona_instance_baseline_after_publish(
    realm_id: str, projection: PersonaInstanceProjection
) -> None:
    """Record the published bodies' hashes as the new baseline.

    The ``_published_profile_file_hashes`` precedent: a member who publishes and
    then pulls must see local == baseline, or their own publish comes straight
    back as a HOLD on every row they just shipped.
    """

    baseline = read_persona_instance_baseline(realm_id)
    for instance_id, body_hash in projection.hashes().items():
        baseline[instance_baseline_key(instance_id)] = body_hash
    write_persona_instance_baseline(realm_id, baseline)


# --- pull: the mint door (plan §3) -------------------------------------------


@dataclass(slots=True)
class PersonaInstancePullSummary:
    """Typed accounting for the persona-instance pull — the ONE contract seam
    between this repo and the launcher (plan §6), carried on a pull ack as
    ``result["persona_instance_sync"]``.

    Every outcome is a row of the §3.3 decision table, and nothing is silently
    written or silently skipped:

    - ``replicated`` — the desk arrived and the agent did not exist here. Minted
      through the store door, with the §1.3 fields derived locally.
    - ``adopted`` — the row was already here, unchanged since the last sync, and
      the realm moved its travelling surface forward. Only travelling fields are
      written; every §1.2 field is preserved by construction.
    - ``converged`` — local already equals remote (no write).
    - ``kept_local`` — this machine edited the row and the realm did not. Stays
      local and unpublished. The plan's table folds this into "not held"; it is
      named separately because the H4 drift lane reports exactly these rows.
    - ``held`` — BOTH sides moved. The local row is left untouched and the
      remote body is parked in a conflict sidecar. Divergent content is never
      clobbered.
    - ``upstream_absent`` — the realm's projection does not carry a row this
      baseline says it published. **NOT a delete** (plan §3.3, §5.2): absence is
      short-answer-shaped, and this subsystem has already paid for inferring
      deletion from a short answer. The baseline is KEPT so a repaired publish
      still converges.
    - ``refused`` — a row the admission door would not admit, or one whose local
      file will not decode. Per-entity isolation: the row is untouched, the
      refusal is named, the pull continues.
    - ``steering_dropped`` — phase-two edges naming a parent this machine does
      not have. Accounted, never silent, and re-accounted on every later pull
      for as long as the parent stays absent: a drop announced once and then
      silent reads as repaired.
    - ``steering_healed`` — edges an EARLIER pull dropped, re-applied here
      because the parent has since arrived and the local body was still exactly
      "remote minus the dropped edge". A row that healed is counted in
      ``adopted`` beside this, because a travelling field did move forward onto
      an existing row; a row whose local body diverged anywhere else is an
      operator re-steer and stays ``kept_local``, untouched.
    - ``retired`` / ``retire_held`` — §5.2's retire-follows-the-DESK arm, and
      the desks whose agent could not be archived (a live run binding above
      all). These are driven by the office lane's own ``remote_removed``
      archives, never by an instance's absence.

    ``source`` is ``None`` when the pulled subtree carries no instance
    projection at all — an older publisher. That is the version-skew fact the
    launcher's badge demotion keys on, and it is deliberately distinct from an
    EMPTY projection.
    """

    replicated: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    converged: list[str] = field(default_factory=list)
    kept_local: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    upstream_absent: list[str] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    steering_dropped: list[dict[str, str]] = field(default_factory=list)
    #: Edges a PREVIOUS pull dropped for an absent parent, re-applied now that
    #: the parent exists — ``{key, parent}``, the mirror of a
    #: ``steering_dropped`` row. Additive on the wire (2026-09-02) and the fact
    #: the launcher's ``AGENT LINKS DROPPED`` group needs before it can stop
    #: saying the edge "will not re-apply": it now does, on the pull after the
    #: parent arrives, and the row that healed says so by name.
    steering_healed: list[dict[str, str]] = field(default_factory=list)
    #: Replicas archived because their DESK left (plan §5.2). Never derived from
    #: an instance's absence — only from the office lane having ARCHIVED the
    #: actor for the same key in this same pull, which is authored intent that
    #: already propagated. Empty is the normal case.
    retired: list[str] = field(default_factory=list)
    #: Desks that left while their agent could NOT be archived — a live run
    #: binding above all (``instance_active``). Held, named, and re-decided on
    #: the next pull: a working agent is never archived out from under an
    #: operator, and the count says so rather than implying the retire happened.
    retire_held: list[dict[str, str]] = field(default_factory=list)
    #: The realm still publishes this agent, but its DESK is archived on this
    #: machine, so no replica is minted or revived. The office surface's
    #: ``archived_actor_keys`` ledger is the resurrection guard the office family
    #: already uses, and the actor key IS the instance id — so this lane asks the
    #: SAME ledger rather than growing a second one. Without it a retire-follows-
    #: the-desk in one pull would be undone by the very next pull, and a desk the
    #: operator deleted would keep coming back with an agent behind it.
    desk_archived: list[str] = field(default_factory=list)
    source: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.replicated or self.adopted or self.retired)

    def as_dict(self) -> dict[str, Any]:
        return {
            "replicated": sorted(set(self.replicated)),
            "adopted": sorted(set(self.adopted)),
            "converged": sorted(set(self.converged)),
            "kept_local": sorted(set(self.kept_local)),
            "held": sorted(set(self.held)),
            "upstream_absent": sorted(set(self.upstream_absent)),
            "refused": list(self.refused),
            "steering_dropped": list(self.steering_dropped),
            "steering_healed": list(self.steering_healed),
            "retired": sorted(set(self.retired)),
            "retire_held": list(self.retire_held),
            "desk_archived": sorted(set(self.desk_archived)),
            "source": self.source,
        }


def read_remote_persona_instances(subtree) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Instance bodies carried by a pulled realm subtree.

    ``(bodies, source)``; ``source`` is ``None`` when the subtree has no
    projection — an older publisher, or a realm that publishes no
    placement-backed rows. There is deliberately NO legacy fallback: unlike
    persona definitions, instances have never travelled in any other shape, so
    an absent projection means exactly one thing.
    """

    from pathlib import Path

    path = Path(subtree).joinpath(*PROJECTION_RELATIVE_PATH.split("/"))
    if not path.is_file():
        return {}, None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        # A projection that exists and will not decode is NOT absence: absence
        # drives ``upstream_absent`` for every baselined row, and reading a parse
        # error as absence would be a delete-shaped decision taken on a read
        # failure (the ``RemoteOffice.unreadable`` argument). The caller reports
        # it as a refusal and touches nothing.
        return {}, "unreadable"
    parsed = read_projection_document(data)
    if parsed is None:
        return {}, None
    return parsed, "projection"


def _refusal_row(instance_id: str, code: str, message: str) -> dict[str, str]:
    return {"key": instance_id, "code": code, "message": message}


def _local_projection_hash(store, instance_id: str):
    """``(hash, record, refusal_code)`` for the LOCAL row.

    A row whose file EXISTS and will not decode is refused rather than reported
    absent. Absent is what drives the MINT arm, so folding a parse error into it
    would overwrite a row that might carry a live run binding — the same
    unreadable-is-not-absent rule ``apply_office_pull`` spends on its own local
    scan.
    """

    from . import paths

    path = paths.persona_instance_path(instance_id)
    try:
        record = store.get(instance_id)
    except FileNotFoundError:
        return None, None, None
    except Exception:
        if path.exists():
            return None, None, "local_row_unreadable"
        return None, None, None
    return persona_instance_def_hash(project_persona_instance(record)), record, None


def _locally_archived_actor_keys() -> set[str]:
    """Every actor key this machine has archived, across every office surface.

    Read from the office store's own ``archived_actor_keys`` ledger — the SAME
    resurrection guard ``_reconcile_actors`` passes to the classifier — because
    the actor key IS the instance id for an instance-bound actor. Growing a
    second ledger for the same fact is how two authorities over one identity
    start disagreeing.

    A store that cannot be read yields an EMPTY set, which is the permissive
    direction, and that is the right one here: the alternative is refusing to
    replicate anything on a projection fault, and the resurrection this guards
    against is bounded (one desk comes back and the operator deletes it again),
    while the refusal would strand every agent in the realm.
    """

    try:
        from .office_store import OfficeStore

        store = OfficeStore()
        keys: set[str] = set()
        for workspace_token in store.list_workspaces():
            try:
                surface = store.get_surface(workspace_token)
            except Exception:  # noqa: BLE001 — one unreadable surface is not the others'
                continue
            keys.update(str(key) for key in (surface.archived_actor_keys or []))
        return keys
    except Exception:  # noqa: BLE001 — see the docstring: permissive on a fault
        return set()


def _retire_replicas_for_removed_desks(
    store,
    summary: PersonaInstancePullSummary,
    baseline: dict[str, str],
    *,
    realm_id: str,
    desks_removed,
) -> None:
    """§5.2: the replica follows the DESK, never the absence.

    ``desks_removed`` is the actor keys the office lane ARCHIVED in this same
    pull — the ``remote_removed`` arm, which is authored intent that already
    propagated from the machine the operator clicked on. That is a different
    fact from an instance being missing from the projection, which is
    ``upstream_absent`` and never a delete: a desk's removal is a decision a
    peer made, while a row's absence is equally consistent with the publisher's
    own office scan having come back short.

    The actor key IS the canonical instance id for an instance-bound actor
    (``_canonical_actor_key``), so a key with no matching row is simply a
    persona-keyed desk and there is nothing to retire.
    """

    from . import paths

    for actor_key in sorted({str(key) for key in (desks_removed or []) if key}):
        if not paths.persona_instance_path(actor_key).exists():
            continue
        try:
            store.retire_replica(actor_key, reason="remote_removed", realm_id=realm_id)
        except Exception as exc:  # noqa: BLE001 — accounted, never silent
            code = getattr(exc, "code", None) or type(exc).__name__
            summary.retire_held.append({"key": actor_key, "code": str(code)})
            # The baseline entry STAYS. Dropping it would tell the next pull
            # there is no baseline for a row that is still live, and a row with
            # no baseline reads as a local ADD — the failed archive would come
            # back as something to publish (``_reconcile_actors``' C2 lesson).
            continue
        summary.retired.append(actor_key)
        baseline.pop(instance_baseline_key(actor_key), None)


def _remote_body_without_edges(
    remote_body: dict[str, Any], parents: list[str]
) -> dict[str, Any]:
    """The remote body as it would look with ``parents`` never applied.

    ``project_persona_instance`` OMITS structurally-empty values, so a row whose
    every edge was dropped carries no ``steered_by`` key at all — dropping to
    ``[]`` instead would hash as a different body and the heal would never
    recognise its own handiwork.
    """

    body = dict(remote_body)
    unwanted = set(parents)
    remaining = [str(item) for item in (body.get("steered_by") or []) if str(item) not in unwanted]
    if remaining:
        body["steered_by"] = remaining
    else:
        body.pop("steered_by", None)
    return body


def _healable_dropped_parents(
    ledger: dict[str, dict[str, Any]],
    instance_id: str,
    remote_body: dict[str, Any],
    remote_hash: str,
    local_hash: str | None,
) -> list[str]:
    """The parents a ``kept_local`` row's local drift is FULLY explained by.

    This is the whole of "tell a dropped edge from an authored re-steer", and it
    asks two questions rather than trusting the ledger alone:

    * the realm has not moved since the drop (the recorded ``remote_hash`` is
      still the remote hash) — otherwise the ledger describes a body that no
      longer exists and the row's drift may be about something else entirely;
    * the local body is EXACTLY the remote body minus those edges. An operator
      who re-steered, renamed, or re-pointed the model changed the body
      somewhere the dropped edge cannot account for, and the answer is to leave
      it alone. That is the one thing ``kept_local`` exists to protect, and it
      is why re-running phase two for every ``kept_local`` row was rejected.

    Empty means "not a heal candidate", and the caller reports ``kept_local``
    exactly as it did before this existed.
    """

    entry = ledger.get(instance_id)
    if not entry or local_hash is None:
        return []
    if entry.get("remote_hash") != remote_hash:
        return []
    parents = [str(item) for item in (entry.get("parents") or [])]
    if not parents:
        return []
    expected = persona_instance_def_hash(_remote_body_without_edges(remote_body, parents))
    return parents if expected == local_hash else []


def apply_persona_instance_pull(
    realm_id: str,
    subtree,
    *,
    event_log: Any = None,
    desks_removed=(),
) -> PersonaInstancePullSummary:
    """THE mint door: a pulled desk that has no agent here gets one.

    Runs inside ``pull_realm_sync`` AFTER the persona-definition and
    profile-file lanes (the mint reads the definition to derive ``role`` and
    ``profile_id``, and a mint from a definition that has not landed yet builds
    the wrong agent) and BEFORE the workspace-tombstone lane (so a replica is
    never minted into a workspace the same pull is about to archive).

    Two phases, per plan §3.4. Phase one writes every row with ``steered_by``
    empty; phase two applies the edges, because an edge may name a parent this
    same pass has not minted yet. Without the split the outcome would depend on
    the alphabetical order of instance ids.

    **The dropped-edge HEAL (2026-09-02), which is a third thing phase one
    does.** An edge whose parent is absent is dropped and accounted, and the row
    is then locally divergent from a remote body the baseline still holds the
    hash of — so every later pull classified it ``kept_local`` and phase two
    never re-ran for it. The edge was gone for good even on a realm that
    published the parent one pull later. Phase one now consults the durable
    drop ledger (:func:`read_dropped_steering_ledger`) on exactly the
    ``kept_local`` rows and re-enters phase two for the ones whose local body is
    still EXACTLY "remote minus the dropped edge" against the SAME remote hash
    the drop was taken against. Anything else — an operator's own re-steer, a
    renamed display name, a moved model override — is left alone, because that
    divergence is what ``kept_local`` exists to protect and re-running phase two
    for all of it was considered and rejected.

    Every write goes through ``PersonaInstanceStore.replicate_instance`` — a
    store door, never a raw file write — so the delta patch, the §1.3
    derivations and the event all happen in one place. A pull that GIVES you an
    agent reaches the same live consumers as one that takes one away.
    """

    from .persona_assignments import PersonaInstanceStore
    from .sync_merge import PullAction, classify_three_way_pull

    summary = PersonaInstancePullSummary()
    remote, source = read_remote_persona_instances(subtree)
    summary.source = source
    store = (
        PersonaInstanceStore(event_log=event_log)
        if event_log is not None
        else PersonaInstanceStore()
    )
    baseline = read_persona_instance_baseline(realm_id)
    dropped_ledger = read_dropped_steering_ledger(realm_id)

    # §5.2 FIRST, and independent of the projection. The trigger is the office
    # lane's own ``remote_removed`` archive, not anything in this document — a
    # peer can retire a desk in the same pull where their projection is absent,
    # unreadable, or unchanged, and the replica has to follow the desk in all
    # three cases.
    _retire_replicas_for_removed_desks(
        store, summary, baseline, realm_id=realm_id, desks_removed=desks_removed
    )

    if source is None:
        # Not published. Never a removal — no baselined row is touched, so an
        # older peer in the rotation cannot strand this machine's replicas.
        if summary.retired:
            write_persona_instance_baseline(realm_id, baseline)
        return summary
    if source == "unreadable":
        summary.refused.append(
            _refusal_row(
                PROJECTION_RELATIVE_PATH,
                "unreadable_projection",
                "the pulled instance projection exists and would not decode",
            )
        )
        if summary.retired:
            write_persona_instance_baseline(realm_id, baseline)
        return summary

    prefix = instance_baseline_key("")
    baselined_ids = {key[len(prefix):] for key in baseline if key.startswith(prefix)}
    written: list[tuple[str, dict[str, Any]]] = []
    #: ``{instance_id: [parent, ...]}`` — rows phase one classified ``kept_local``
    #: whose drift is fully explained by edges an earlier pull dropped. They
    #: re-enter phase two and their OUTCOME is decided there, because whether
    #: the parent has actually arrived is phase two's question, not phase one's.
    heal_pending: dict[str, list[str]] = {}
    # The resurrection guard, from TWO sources that answer the same question at
    # different ranges. The office ledger covers desks archived in any earlier
    # pass; ``summary.retired`` covers the ones THIS pass just archived, which
    # the ledger would also carry in production but which must not depend on
    # another store's state for a guarantee this function makes about itself. A
    # retire undone by phase one of the same pull would be incoherent.
    archived_keys = _locally_archived_actor_keys() | set(summary.retired)

    # --- phase one: the rows ------------------------------------------------
    for instance_id in sorted(set(remote) | baselined_ids):
        remote_body = remote.get(instance_id)
        key = instance_baseline_key(instance_id)

        if remote_body is not None:
            refusal = refuse_persona_instance(instance_id, remote_body)
            if refusal is not None:
                # Untouched, named, and the pull continues — the store never
                # learns this row existed.
                summary.refused.append(refusal.as_dict())
                continue

        local_hash, local_record, local_refusal = _local_projection_hash(store, instance_id)
        if local_refusal is not None:
            summary.refused.append(
                _refusal_row(instance_id, local_refusal, "local instance row will not decode")
            )
            continue

        if remote_body is None:
            # THE row that is not a delete. The realm stopped carrying a row
            # this baseline says it published, and that is exactly as consistent
            # with "the publisher's office scan came back short" as with "the
            # operator deleted it". Only the DESK's own removal is authored
            # intent (plan §5.2). Hold it, name it, and KEEP the baseline so a
            # repaired publish still converges.
            if local_hash is not None:
                summary.upstream_absent.append(instance_id)
            else:
                baseline.pop(key, None)
            continue

        remote_hash = persona_instance_def_hash(remote_body)
        decision = classify_three_way_pull(
            local_hash,
            remote_hash,
            baseline.get(key),
            # THE resurrection guard, and it is the office family's own ledger
            # rather than a second one: the actor key IS the instance id, so a
            # desk the operator archived here answers for its agent too. Without
            # it, a retire-follows-the-desk taken in one pull would be undone by
            # the very next pull, because the retired row reads as locally absent
            # while the realm still publishes it.
            locally_archived=instance_id in archived_keys,
        )
        if decision.reason in ("archived_local", "archive_vs_edit"):
            summary.desk_archived.append(instance_id)
            if decision.action == PullAction.CONFLICT:
                # The realm EDITED an agent whose desk is archived here. Nothing
                # is written, but the body is parked so the divergence is not
                # simply lost between two correct local states.
                _write_conflict_sidecar(
                    realm_id, instance_id, decision.reason, remote_body, local_hash, remote_hash
                )
            continue

        if decision.action == PullAction.NOOP:
            summary.converged.append(instance_id)
            written.append((instance_id, remote_body))
            baseline[key] = remote_hash
            continue
        if decision.action == PullAction.KEEP_LOCAL:
            # THE heal seam. A row whose local drift is exactly the edges an
            # earlier pull dropped is not an operator's edit at all — it is this
            # lane's own unfinished write, and re-entering phase two is what
            # finishes it. Everything else stays ``kept_local`` untouched.
            healable = _healable_dropped_parents(
                dropped_ledger, instance_id, remote_body, remote_hash, local_hash
            )
            if healable:
                heal_pending[instance_id] = healable
                written.append((instance_id, remote_body))
            else:
                summary.kept_local.append(instance_id)
            continue
        if decision.action == PullAction.CONFLICT:
            summary.held.append(instance_id)
            _write_conflict_sidecar(
                realm_id, instance_id, decision.reason, remote_body, local_hash, remote_hash
            )
            continue
        if decision.action == PullAction.ARCHIVE_LOCAL:
            # Unreachable while ``remote_body is not None`` — the classifier only
            # takes this arm on an absent remote — and stated rather than left to
            # fall through: this family has NO archive arm in the pull at all.
            # Retirement follows the DESK (H4/§5.2), never the absence.
            summary.upstream_absent.append(instance_id)
            continue

        # WRITE_REMOTE. ``converged`` here means both sides moved to the SAME
        # content and only the baseline needs to catch up — never a rewrite.
        if decision.reason == "converged":
            summary.converged.append(instance_id)
            written.append((instance_id, remote_body))
            baseline[key] = remote_hash
            continue
        try:
            store.replicate_instance(remote_body, realm_id=realm_id, adopt_existing=local_record)
        except Exception as exc:  # noqa: BLE001 — accounted; the pull continues
            summary.refused.append(_refusal_row(instance_id, "mint_failed", type(exc).__name__))
            continue
        if local_record is None:
            summary.replicated.append(instance_id)
        else:
            summary.adopted.append(instance_id)
        written.append((instance_id, remote_body))
        # THE baseline-alignment property (plan §3.3): keyed off the REMOTE hash,
        # never re-derived from the local write, so a fresh replica reads ZERO
        # drift immediately. Without it the very next `realm sync status` reports
        # the replica as an unpublished local addition and the revert lane offers
        # to archive correct state.
        baseline[key] = remote_hash

    # --- phase two: the authored steering edges -----------------------------
    #
    # Only rows this pass WROTE or found converged, plus the HEAL candidates
    # phase one re-entered. A ``held`` row must not have its graph rewritten by
    # the body it refused to adopt, and a ``kept_local`` row's steering is this
    # machine's own edit — unless the ledger says the drift IS a drop this lane
    # took, which is the one exception and it is decided in phase one.
    next_ledger: dict[str, dict[str, Any]] = {}
    for instance_id, remote_body in written:
        parents = [str(item) for item in (remote_body.get("steered_by") or [])]
        pending = heal_pending.get(instance_id)
        try:
            applied, dropped = store.apply_replicated_steering(
                instance_id, parents, realm_id=realm_id
            )
        except Exception as exc:  # noqa: BLE001 — accounted; a bad edge never fails a pull
            summary.steering_dropped.append(
                {"key": instance_id, "parent": "", "reason": type(exc).__name__}
            )
            if pending is not None:
                # The heal did not happen, so the row is what phase one would
                # have called it, and the ledger entry is carried forward
                # UNCHANGED — a store fault is not evidence that the edge was
                # re-applied or that its parent arrived.
                summary.kept_local.append(instance_id)
                next_ledger[instance_id] = dict(dropped_ledger[instance_id])
            continue
        for row in dropped:
            summary.steering_dropped.append({"key": instance_id, **row})
        # Only ``parent_absent`` is recorded for a later heal. A self edge and a
        # cycle are refusals of the remote GRAPH — no parent is ever going to
        # arrive and make them valid — so re-entering phase two for them every
        # pull would re-report a verdict that cannot change.
        still_absent = sorted(
            {str(row.get("parent") or "") for row in dropped if row.get("reason") == "parent_absent"}
            - {""}
        )
        if still_absent:
            next_ledger[instance_id] = {
                "parents": still_absent,
                # The hash the drop was taken AGAINST, so a realm that moves the
                # body afterwards invalidates this entry by construction rather
                # than by an expiry rule.
                "remote_hash": persona_instance_def_hash(remote_body),
            }
        if pending is None:
            continue
        healed = [parent for parent in pending if parent in applied]
        for parent in healed:
            summary.steering_healed.append({"key": instance_id, "parent": parent})
        if healed:
            # ``adopted`` and not ``converged``: a travelling field DID move
            # forward onto an existing row, and ``converged`` promises no write.
            summary.adopted.append(instance_id)
        else:
            # Nothing arrived. The row is exactly what phase one would have
            # called it, and the re-entry cost one store read.
            summary.kept_local.append(instance_id)

    write_persona_instance_baseline(realm_id, baseline)
    write_dropped_steering_ledger(realm_id, next_ledger)
    return summary


def _write_conflict_sidecar(
    realm_id: str,
    instance_id: str,
    kind: str,
    remote_body: dict[str, Any],
    local_hash: str | None,
    remote_hash: str | None,
) -> None:
    """Park the body a HOLD refused to adopt.

    Best-effort: a sidecar this machine cannot write is not a reason to clobber
    the row the hold exists to protect.
    """

    from utils import atomic_json_write

    try:
        atomic_json_write(
            instance_conflict_path(realm_id, instance_id),
            {
                "schema_version": 1,
                "realm_id": realm_id,
                "persona_instance_id": instance_id,
                "kind": kind,
                "local_hash": local_hash,
                "remote_hash": remote_hash,
                "remote_body": remote_body,
            },
            indent=2,
            sort_keys=True,
        )
    except Exception:  # noqa: BLE001 — the HOLD stands with or without its receipt
        pass
