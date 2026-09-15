"""Parking the two scope pointers — ONE implementation, two doors.

`harness workspace use` / `harness realm use` (argv) and
``runtime.workspace.use`` / ``runtime.realm.use`` (the method lane, plan WS4)
are the SAME operation reached two ways. Before this module the argv verbs owned
the whole decision inline in ``hermes_cli/harness.py`` — the ``set_active``
call, the superseded/duplicate arms, the realm switch's workspace reconcile and
the row each answer renders — and a second door would have had to re-say all of
it. Two implementations that agree today is exactly the shape this repo keeps
retiring (the S48 row consolidation is the same lesson one layer up), so the
decision moved HERE and both doors call it.

**Why the ROW projections moved too.** ``_workspace_row`` / ``_realm_row`` were
CLI-private, and the temptation was to leave them there and let the method lane
render something smaller. It should not: "the same accept semantics" is a claim
about the ANSWER as much as the decision, and a method whose result had its own
shape would be a second contract nobody decided on. They are re-keys of
``agent_runtime.snapshot``'s own summary builders (S48, ledger item 4), so
agent_runtime is where they always belonged; ``hermes_cli.harness`` imports
them back under their old private names and every existing CLI call site is
unchanged.

**What is NOT here.** Authorization. The method lane's tier is declared at the
registration (``serve_rpc.method(..., tier=TIER_CONSOLE)``) and evaluated at the
chokepoint (``call_authorization.authorize_call``) before dispatch, which is
where Ruling A put it. A service-level second check is deliberately absent:
:func:`service_backstop` exists for the two verbs that mutate a LEVEL, and a
scope pointer is not one — it is a two-scalar preference this install's own
operator sets, and the argv door reaches these functions with no caller object
at all.

**The invariant: the two pointers never durably straddle realms.**
(Operator ruling, 2026-09-01 — "newest explicit gesture wins".) There are two
EXPLICIT gestures, :func:`activate_workspace` and :func:`activate_realm`, and
they are ordered against each other by the ``issued_at`` basis the launcher
stamps once at the gesture. After ANY interleaving of the two, both pointers
land in the scope of the gesture with the strictly newer basis, and the loser
answers ``applied: false, reason: "superseded"``.

The reconcile's own ``set_active`` is NOT a gesture: it carries its realm
intent's basis, so a late-delivered realm switch cannot clobber a newer
workspace selection through it. That protection is what OPENS the hole this
invariant closes — a refused reconcile used to be discarded, leaving the realm
pointer in the realm the (older) realm gesture asked for and the workspace
pointer in the realm the (newer) workspace gesture asked for, with both verbs
having answered ``applied: true`` and nothing on the tree able to heal it. Each
door therefore carries one arm:

* :func:`activate_realm` — when the reconcile comes back ``superseded``, the
  workspace gesture is newer, so it wins the whole scope: the realm pointer is
  re-parked to the WINNING workspace's realm under that workspace's stored
  basis, and the realm verb answers ``superseded``.
* :func:`activate_workspace` — when an explicit workspace selection applies,
  the realm pointer follows it under the SAME basis.

Both arms write through ``*Store.set_active``, never around it, so the scope
patch and the ``*.activated`` event ride along exactly as they do for a plain
switch. Neither arm hand-writes a pointer file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from .store import RealmStore, WorkspaceStore

__all__ = [
    "WORKSPACE_USE_METHOD",
    "REALM_USE_METHOD",
    "RECONCILE_KEPT",
    "workspace_row",
    "realm_row",
    "activation_outcome_row",
    "reconcile_active_workspace_to_realm",
    "activate_workspace",
    "activate_realm",
    "ScopeActivationRefusal",
    "ScopeActivationOutcome",
    "perform_scope_activation",
]

_log = logging.getLogger(__name__)

#: The method names, spelled once. The launcher gates its lowering on membership
#: of these exact strings in the ``rpc.methods`` manifest set (the D12 pattern),
#: so a rename here is a wire change and reads as one in a diff.
WORKSPACE_USE_METHOD = "runtime.workspace.use"
REALM_USE_METHOD = "runtime.realm.use"


# ── the row projections (moved from hermes_cli/harness.py, verbatim) ──────────


def workspace_row(workspace, *, full: bool = False) -> dict:
    """`workspace list|show|…` row — a RE-KEY of the snapshot's own workspace
    builder, never a second projection (S48, ledger item 4).

    The hand-rolled twin this replaces is what shipped the ``tasks`` NameError
    (`a21ab1a2a`): a field the snapshot row had already dropped survived here
    because nothing tied the two together. Every value below now comes from
    ``_workspace_summary``; the CLI owns only the key SUBSET (skinny vs
    ``--full``).

    Deliberate deviations, each with a reason:

    * ``created_at`` is CLI-only — the wire never carried it, and an operator
      reading ``workspace show`` does. It is a plain timestamp off the model,
      not a re-derivation of anything the builder computes.
    * timestamps stay as ``datetime`` rather than the builder's value, because
      the Stage-42 printer (``emit_json`` -> ``to_jsonable``) is the
      serialization authority for this lane; pre-serializing here would change
      the ``--output table`` rendering for no reason. ``_workspace_summary``
      passes ``updated_at`` through unconverted anyway, so this is a no-op for
      workspaces and kept only for symmetry with the board/office rows.

    The builder import is FUNCTION-LOCAL on purpose (all six rows do this): a
    module-level ``from … import _workspace_summary`` binds whichever
    definition existed at CLI import time, which is itself a second reference
    to the authority. Resolving through the module on every call means there is
    exactly one live definition and it is the snapshot module's.
    """

    from agent_runtime.persona_assignments import PersonaInstanceStore
    from agent_runtime.snapshot import _workspace_summary

    summary = _workspace_summary(workspace, persona_instances=PersonaInstanceStore().list_all())
    row = {
        key: summary[key]
        for key in (
            "id",
            "name",
            "realm_id",
            "agents",
            "agent_ids",
            "live_scoped_agent_count",
            "live_scoped_agent_ids",
            "roster_agent_count",
            "roster_agent_ids",
            "isolation",
        )
    }
    row["updated_at"] = workspace.updated_at
    if full:
        row.update({key: summary[key] for key in ("kind", "slug", "default_blueprint_id", "max_concurrent_lanes", "archived")})
        row["created_at"] = workspace.created_at
    return row


def realm_row(realm, *, full: bool = False) -> dict:
    """`realm list|show|…` row — a RE-KEY of the snapshot's own realm builder
    (S48, ledger item 4).

    The hand-rolled twin this replaces is where ``"sync": "in_sync"`` was
    hardcoded (`a21ab1a2a`) — the exact fake ``_realm_summary`` forbids. The
    honest sidecar read now happens in ONE place, so the CLI cannot drift from
    the wire again. CLI-only additions (never on the wire, both plain model
    scalars): ``sync_manifest_ref`` and ``created_at``.
    """

    from agent_runtime.snapshot import _realm_summary

    summary = _realm_summary(realm, workspaces=WorkspaceStore().list_all(include_archived=True))
    row = {
        key: summary[key]
        for key in ("id", "name", "server_id", "default_workspace_id", "default_workspace_version", "workspaces", "sync")
    }
    row["updated_at"] = realm.updated_at
    if full:
        row.update({key: summary[key] for key in ("kind", "slug", "workspace_ids", "default_workspace_name", "archived")})
        row["sync_manifest_ref"] = realm.sync_manifest_ref
        row["created_at"] = realm.created_at
    return row


# ── the decision (moved from hermes_cli/harness.py, verbatim) ────────────────


def activation_outcome_row(store, row_builder, outcome: dict, key: str) -> dict:
    """Envelope row for a set_active call the store declined. The row shows
    the pointer's CURRENT owner (what stays active); `applied`/`reason` tell
    the client why its request did not take. `superseded` = a strictly newer
    intent owns the pointer (client should drop its optimistic state);
    `duplicate` = this exact intent already applied (client treats as
    success). Exit code stays 0 — both are valid protocol outcomes, not
    errors."""
    current_id = outcome.get(key)
    try:
        row = row_builder(store.get(current_id)) if current_id else {"id": None, "name": None}
    except Exception:
        row = {"id": current_id, "name": None}
    row["applied"] = False
    row["reason"] = outcome.get("reason")
    row["superseded"] = outcome.get("reason") == "superseded"
    row[f"requested_{key}"] = outcome.get(f"requested_{key}")
    return row


#: What :func:`reconcile_active_workspace_to_realm` answers when the active
#: workspace ALREADY belonged to the target realm and no write was attempted.
#:
#: Every other arm answers ``WorkspaceStore.set_active``'s own dict verbatim,
#: where ``applied``/``reason`` describe the WRITE. This token keeps "there was
#: nothing to do" apart from "the store declined", for the caller and for the
#: reader.
#:
#: It is NOT what stops the straddle heal firing on a kept pointer, and the
#: comment here said so until a mutation run corrected it (2026-09-01: the
#: claim "a kept arm mislabelled ``superseded`` would drag the realm pointer
#: back" SURVIVED, because it is false). A kept workspace belongs to the target
#: realm BY DEFINITION, so ``_heal_realm_pointer_to_winning_workspace``'s
#: ``winning_realm_id == requested_realm_id`` check turns the mislabelling into
#: a wasted store read and nothing else. What the arm's early return really
#: buys is the pointer itself: without it the ladder re-derives a workspace and
#: can move the operator off the one they chose.
RECONCILE_KEPT = "kept"


def reconcile_active_workspace_to_realm(realm, *, issued_at: str | None = None) -> dict:
    """Switching realms must not leave the active workspace pointing into
    another realm. Keep it when it already belongs; otherwise fall to the
    realm's declared default, then its configured order, then listing order,
    choosing only unarchived workspaces; clear it when the realm has none.

    Returns the OUTCOME rather than nothing, because the caller has a decision
    to make on it. ``set_active`` can decline this write — a strictly newer
    explicit workspace selection owns the pointer — and a discarded refusal is
    precisely the straddle the module docstring's invariant forbids.
    :func:`activate_realm` reads ``reason == "superseded"`` off this dict and
    heals; ``_cmd_workspace_delete`` (the other caller, where a delete of the
    active workspace reconciles for the same reason a realm switch does) ignores
    it, which is correct there: no gesture is racing a delete's fallback.

    Shape: ``{"workspace_id": …, "applied": …, "reason": …}`` — ``set_active``'s
    dict on the write arms, and ``reason == RECONCILE_KEPT`` on the early return.
    """
    store = WorkspaceStore()
    active_id = store.active_id()
    if active_id:
        try:
            active = store.get(active_id)
        except Exception:
            active = None
        if active is not None and getattr(active, "realm_id", None) == realm.id:
            return {"workspace_id": active_id, "applied": False, "reason": RECONCILE_KEPT}
    candidates = [
        workspace
        for workspace in store.list_all()
        if getattr(workspace, "realm_id", None) == realm.id and not workspace.archived
    ]
    configured_order = {wid: index for index, wid in enumerate(getattr(realm, "workspace_ids", None) or [])}
    default_workspace_id = getattr(realm, "default_workspace_id", None)
    candidates.sort(
        key=lambda workspace: (
            0 if workspace.id == default_workspace_id else 1,
            configured_order.get(workspace.id, len(configured_order)),
            workspace.id,
        )
    )
    next_workspace = candidates[0] if candidates else None
    # set_active emits workspace.activated (or {"cleared": true}) at the
    # store chokepoint — Stage 12. The realm intent's basis rides along so a
    # late-delivered realm switch cannot clobber a newer explicit workspace
    # selection through its reconcile. When it IS clobbered, the caller heals —
    # see the module docstring's invariant.
    return store.set_active(next_workspace.id if next_workspace else None, issued_at=issued_at)


def _follow_realm_of_selected_workspace(workspace, *, issued_at: str | None) -> None:
    """The workspace door's half of the invariant: an APPLIED explicit workspace
    selection pulls the realm pointer to that workspace's realm.

    This closes the race order where the realm switch fully applies FIRST and
    the newer workspace selection lands after, pointing into another realm —
    the realm pointer would otherwise sit in the realm the older gesture asked
    for while the workspace pointer sits in the newer gesture's, and no
    reconcile ever runs again to notice.

    Three no-ops, each deliberate:

    * a workspace with no ``realm_id`` has no realm to follow;
    * a realm pointer already parked there needs no write, and writing anyway
      would emit a second ``realm.activated`` on every ordinary workspace
      switch — the non-race paths stay byte-identical because of this check;
    * a ``realm_id`` naming a realm the store cannot read (a deleted realm, a
      half-migrated store) is logged and skipped rather than raised: the
      workspace selection ITSELF landed, and turning it into a
      ``realm_not_found`` refusal would report a failure for a write that
      succeeded.

    The write carries THIS gesture's basis, so ``RealmStore.set_active``'s own
    compare-and-set still protects it against a strictly newer realm intent —
    the follow is a consequence of the gesture, never an override of the
    ordering.
    """

    realm_id = getattr(workspace, "realm_id", None)
    if not realm_id:
        return
    realms = RealmStore()
    if realms.active_id() == realm_id:
        return
    try:
        realms.set_active(realm_id, issued_at=issued_at)
    except Exception:  # noqa: BLE001 — the workspace write already landed
        _log.warning(
            "workspace %s selected but its realm %s could not be parked",
            getattr(workspace, "id", None),
            realm_id,
            exc_info=True,
        )


def activate_workspace(workspace_id: str, *, issued_at: str | None = None) -> dict:
    """Park the workspace pointer and answer with the row BOTH doors render.

    The three outcomes the store can produce are all here and none is an error:
    ``applied: true`` with the workspace's own row, and the declined arms
    (``superseded`` / ``duplicate``) rendered by :func:`activation_outcome_row`
    against whatever currently owns the pointer.

    On the APPLIED arm the realm pointer follows the selection
    (:func:`_follow_realm_of_selected_workspace`) — half of the module
    docstring's straddle invariant. The declined arms do not: a gesture that
    lost the pointer must not move the other one either.
    """

    store = WorkspaceStore()
    outcome = store.set_active(workspace_id, issued_at=issued_at)
    if not outcome.get("applied", True):
        return activation_outcome_row(store, workspace_row, outcome, "workspace_id")
    active_id = outcome.get("workspace_id")
    if not active_id:
        # ``--clear``: the pointer is empty, so there is no workspace to render
        # and no realm to follow. Reading ``store.get(None)`` here is what the
        # old spelling did, and it raised.
        return {"id": None, "name": None, "applied": True}
    workspace = store.get(active_id)
    _follow_realm_of_selected_workspace(workspace, issued_at=issued_at)
    row = workspace_row(workspace)
    row["applied"] = True
    return row


def _heal_realm_pointer_to_winning_workspace(requested_realm_id: str) -> dict | None:
    """The realm door's half of the invariant, run only when the reconcile was
    refused ``superseded``.

    A refusal means a strictly newer explicit workspace selection owns the
    workspace pointer. Under "newest explicit gesture wins" that selection wins
    the whole scope, so the REALM pointer is re-parked to the winning
    workspace's realm — under the WORKSPACE pointer's stored basis, which is the
    newer one and therefore the only one the realm CAS will accept. The realm
    verb then answers ``superseded`` through the row shape the protocol already
    has; the launcher's client drops its optimistic state on that arm.

    Returns ``None`` — meaning "no straddle, keep today's applied answer" — when
    the winning workspace already belongs to the target realm, or when the
    pointer is cleared or unreadable and there is no realm to follow.
    """

    workspaces = WorkspaceStore()
    winner_id = workspaces.active_id()
    if not winner_id:
        return None
    try:
        winner = workspaces.get(winner_id)
    except Exception:  # noqa: BLE001 — an unreadable pointer heals nothing
        _log.warning("active workspace %s is unreadable; not healing", winner_id, exc_info=True)
        return None
    winning_realm_id = getattr(winner, "realm_id", None)
    if not winning_realm_id or winning_realm_id == requested_realm_id:
        return None
    realms = RealmStore()
    try:
        realms.set_active(winning_realm_id, issued_at=workspaces.active_intent_issued_at())
    except Exception:  # noqa: BLE001 — report the pointer's real owner below
        _log.warning("could not re-park the realm pointer to %s", winning_realm_id, exc_info=True)
    # The pointer is re-READ rather than assumed: if an even newer realm intent
    # took it between the reconcile and here, the row must name what actually
    # owns it, not what this heal asked for.
    return activation_outcome_row(
        realms,
        realm_row,
        {
            "realm_id": realms.active_id(),
            "applied": False,
            "reason": "superseded",
            "requested_realm_id": requested_realm_id,
        },
        "realm_id",
    )


def activate_realm(realm_id: str, *, issued_at: str | None = None) -> dict:
    """Park the realm pointer, reconcile the workspace under it, answer the row.

    The reconcile is INSIDE the shared implementation rather than at the two
    doors, and that is the whole reason this function exists rather than a bare
    ``set_active``: a realm switch that skipped it would leave the active
    workspace pointing into the realm the operator just left, and a second door
    that forgot the call would be a bug only reachable from one lane.

    The reconcile's own answer is now READ. When the workspace pointer is owned
    by a strictly newer explicit selection the reconcile is refused, and this
    realm switch has lost the scope: :func:`_heal_realm_pointer_to_winning_workspace`
    puts the realm pointer back where that selection implies and this call
    answers ``superseded``. Discarding the refusal — what the code did before
    2026-09-01 — is what left the two pointers straddling realms with both verbs
    reporting success.
    """

    store = RealmStore()
    outcome = store.set_active(realm_id, issued_at=issued_at)
    if not outcome.get("applied", True):
        return activation_outcome_row(store, realm_row, outcome, "realm_id")
    item = store.get(realm_id)
    reconciled = reconcile_active_workspace_to_realm(item, issued_at=issued_at)
    if reconciled.get("reason") == "superseded":
        healed = _heal_realm_pointer_to_winning_workspace(realm_id)
        if healed is not None:
            return healed
    row = realm_row(item)
    row["applied"] = True
    return row


# ── the method lane's shim (plan WS4) ────────────────────────────────────────


@dataclass(frozen=True)
class ScopeActivationRefusal:
    """A typed refusal, in the shape ``serve_rpc.err`` wants."""

    code: int
    message: str
    data: dict[str, Any]


@dataclass(frozen=True)
class ScopeActivationOutcome:
    """Exactly one of the two is set."""

    result: dict[str, Any] | None = None
    refusal: ScopeActivationRefusal | None = None


#: verb -> (params key, the function that performs it). A MAP rather than a
#: branch, for the reason the method registry is a dict: a test can iterate it
#: and assert both verbs are covered, and adding a third scope pointer (there is
#: no third today) is a row rather than an arm.
_VERBS: dict[str, tuple[str, Callable[..., dict]]] = {
    WORKSPACE_USE_METHOD: ("workspace_id", activate_workspace),
    REALM_USE_METHOD: ("realm_id", activate_realm),
}


def perform_scope_activation(params: dict, *, verb: str) -> ScopeActivationOutcome:
    """The method lane's door onto :func:`activate_workspace` /
    :func:`activate_realm`.

    Params: the id (``workspace_id`` or ``realm_id``, required);
    ``issued_at`` and ``correlation_id`` optional. ``issued_at`` is the SAME
    supersede basis the argv verb takes as ``--issued-at``, threaded through
    unchanged — the launcher stamps it once at the gesture and both lanes
    present the original instant, so a late-delivered switch loses to a newer
    applied one instead of clobbering it (Stage 13 write-path integrity).

    A DECLINED activation is a RESULT, not a refusal, exactly as it is on the
    argv lane where both arms exit 0. ``superseded`` and ``duplicate`` are
    protocol outcomes the client is expected to read off ``applied``; rendering
    them as JSON-RPC errors would make the launcher's accept path treat a
    correctly-ordered switch as a failure and raise the R-A parked-elsewhere
    surface for something that worked.

    A missing id and an unknown id are the only refusals, and they are the argv
    lane's own two failures: ``harness workspace use`` with no positional is an
    argparse error, and one naming an id the store has no row for raises
    ``NotFound``.
    """

    from .errors import NotFound
    from .serde import safe_id, to_jsonable
    from .serve_rpc import ERR_INVALID_PARAMS, ERR_NOT_FOUND

    key, perform = _VERBS[verb]
    raw = params.get(key) if isinstance(params, dict) else None
    scope_id = safe_id(raw)
    if not scope_id:
        return ScopeActivationOutcome(
            refusal=ScopeActivationRefusal(
                code=ERR_INVALID_PARAMS,
                message=f"{key} is required",
                data={"reason": f"{key}_required"},
            )
        )
    issued_at = params.get("issued_at")
    issued_at = issued_at if isinstance(issued_at, str) and issued_at.strip() else None
    try:
        row = perform(scope_id, issued_at=issued_at)
    except NotFound:
        entity = key.removesuffix("_id")
        return ScopeActivationOutcome(
            refusal=ScopeActivationRefusal(
                code=ERR_NOT_FOUND,
                message=f"{entity} not found: {scope_id}",
                data={"reason": f"{entity}_not_found", key: scope_id},
            )
        )
    # ``to_jsonable`` is the SAME serialization the argv lane's Stage-42 printer
    # applies to this row (``emit_json`` -> ``to_jsonable``), called here rather
    # than inside :func:`activate_workspace` so the two doors render one row
    # through one serializer. The row carries a ``datetime`` (``updated_at``);
    # without this the method lane would answer a frame ``json.dumps`` refuses.
    result = dict(to_jsonable(row))
    correlation_id = params.get("correlation_id")
    if isinstance(correlation_id, str) and correlation_id.strip():
        result["correlation_id"] = correlation_id.strip()
    return ScopeActivationOutcome(result=result)
