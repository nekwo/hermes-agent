"""The METHOD lane's RESOLVE leg: ``runtime.office.resolve_conflict``.

The eighth method and the last office WRITE verb to reach the wire — the
unfinished main path, not a fallback deletion (Plan EG §4.5 / RD Correction 4).
Sibling of ``test_serve_rpc_office_remove.py`` and built on
``test_serve_rpc_office.py``'s helpers rather than a second copy of them; the
class-key fixtures come from ``test_office_class_key_guard.py`` for the same
reason, because a hand-built imitation of post-migration state is exactly what
that suite exists not to trust.

What a RESOLVE has to prove that neither actor verb does:

1. **The ack distinguishes the two outcomes of one success.** A resolution
   either leaves an ACTOR (``revision`` present) or leaves a TOMBSTONE (the
   edit-vs-remove arm — ``state: "archived"``, no ``revision``). Both are
   successes; a client that read the second as a failure would leave the strip's
   conflict pill lit on a row that no longer exists. Asserted as WHOLE frames,
   including the ABSENCE of the ``revision`` key.

2. **The class-key fence is reached, not re-implemented.** ``take="remote"``
   writes a peer's actor past ``upsert_actor``, so its fence lives inside
   ``OfficeStore._guard_class_keyed_adoption`` — and this lane is the second
   caller through that door. Pinned three ways: the refusal itself, the
   inertness of an ``allow_class_key`` key on the wire (the contract rule — a
   wire parameter is not consent), and a chokepoint test that poisons every
   sibling store writer and asserts no actor file is written outside
   ``resolve_conflict``'s own dynamic extent.

3. **A repeat resolve is a typed 4001, not a second adoption.** The remove verb
   answers a repeat with ``ok`` because the store is already in the state the
   caller asked for. A repeat resolve has no conflict left to read, and the only
   way to answer ``ok`` would be to invent one — so this is the one place the
   two write verbs deliberately disagree, and the actor's BYTES are what pin it.

4. **Every refusal writes nothing.** Each error test re-reads the store and pins
   the actor, the sidecar, or both unchanged. A typed refusal in front of a
   store that adopted the peer anyway is the worst outcome available.

5. **The two lanes refuse the same fault on the same fixture.** ``harness office
   resolve-conflict`` and this method run against ONE seeded conflict and both
   refuse it, naming the same collision evidence from the same authority. The
   two code vocabularies are different by design (the CLI's stage-42 exit
   taxonomy vs. JSON-RPC's ``data.reason``) and that test pins the MAPPING
   rather than pretending they are one string, so a rename on either side reds.

6. **The gesture token, on this verb.** EG-2.3's ``correlation_id`` is pinned per
   verb in ``test_correlation_id.py``, but that suite's "every write verb" group
   predates this one and names three — upsert, remove, surface.update. Resolve
   was the fourth write verb to reach the wire and arrived after it, so its
   accept/carry/echo lived undefended in the canonical suite (ML-1). The group at
   the end of this file closes that: the echo (both arms, since the tombstone
   builds its own result dict), the CARRY into the store, the absent-key shape a
   pre-token client still reads, and the boundary refusal.
"""

from __future__ import annotations

import json

from agent_runtime import serve_rpc
from tests.agent_runtime.office_seed import seed_workspace_record
from tests.agent_runtime.test_serve_rpc_office import (
    SHUTDOWN,
    _reply,
    _rpc,
    _run,
)

WORKSPACE = "ws_rpc_resolve_test"

#: The instance-bound actor the seed places, and the key the store canonicalizes
#: its identity triple onto.
QA_INSTANCE = "personainst_qa_agent_9c8a382f"

#: A SECOND instance key, used only by the ack-names-the-store's-key test: it is
#: what a peer record can carry while the sidecar is filed under the first one.
QA_INSTANCE_PEER = "personainst_qa_agent_51d0c4b7"

#: The peer's revision in every sidecar below. The store adopts ``max(n, 1) + 1``,
#: so 7 acks as 8 — a number nothing else in these fixtures equals.
REMOTE_REVISION = 7


# ── seeding ─────────────────────────────────────────────────────────────────


def _store():
    from agent_runtime.office_store import OfficeStore

    return OfficeStore()


def _actor_payload(persona_id: str = "qa", *, instance: str = QA_INSTANCE) -> dict:
    return {
        "persona_id": persona_id,
        "persona_instance_id": instance,
        "items": [
            {
                "item_id": instance,
                "kind": "agent",
                "position": [-8.0, -2.0],
                "folder": "Agents",
                "display_name": "QA Agent",
            },
            {
                "item_id": f"{persona_id}_desk",
                "kind": "desk",
                "position": [-8.0, -4.5],
                "folder": "Desks",
            },
        ],
    }


def _seed(workspace_id: str = WORKSPACE):
    """One instance-bound actor at revision 2, in an authored office.

    TWO writes, not one, so the local revision is 2 and the adopted remote's is
    8 — three distinguishable numbers (2, 7, 8) across the fixture, which is
    what stops an off-by-one or a wrong-side ack landing on a value some other
    quantity here happens to equal.
    """

    store = _store()
    seed_workspace_record(workspace_id)
    store.ensure_surface(workspace_id, created_by="seed")
    store.upsert_actor(workspace_id, _actor_payload(), updated_by="seed-operator")
    store.upsert_actor(workspace_id, _actor_payload(), updated_by="seed-operator")
    assert store.get_actor(workspace_id, QA_INSTANCE).revision == 2
    return store


def _seed_conflict(
    workspace_id: str = WORKSPACE,
    sidecar_key: str = QA_INSTANCE,
    *,
    remote_key: str | None = QA_INSTANCE,
    persona_id: str = "qa",
) -> None:
    """A realm-sync conflict sidecar, written through the REAL sidecar writer.

    ``remote_key=None`` writes the REMOVAL tombstone shape — no ``remote_actor``
    at all, which is what ``apply_office_pull`` leaves when the peer deleted what
    this side edited.
    """

    from agent_runtime.models import OfficeActor, OfficeItem
    from agent_runtime.office_sync import _write_conflict_sidecar

    remote = None
    if remote_key is not None:
        remote = OfficeActor(
            actor_key=remote_key,
            workspace_id=workspace_id,
            persona_id=persona_id,
            persona_instance_id=remote_key,
            items=[
                OfficeItem(
                    item_id=remote_key,
                    persona_id=persona_id,
                    kind="agent",
                    position=[9.0, 9.0],
                    folder="Agents",
                )
            ],
            revision=REMOTE_REVISION,
        )
    _write_conflict_sidecar(
        workspace_id,
        sidecar_key,
        kind="archive_vs_edit",
        remote_actor=remote,
        local_hash=None,
        remote_hash="remote-hash",
    )


def _live_keys(workspace_id: str = WORKSPACE) -> list[str]:
    return [a.actor_key for a in _store().scan_actors(workspace_id).actors]


def _actor_bytes(workspace_id: str = WORKSPACE, actor_key: str = QA_INSTANCE):
    from agent_runtime import paths

    path = paths.office_actor_path(workspace_id, actor_key)
    return path.read_bytes() if path.exists() else None


def _sidecar_exists(workspace_id: str = WORKSPACE, actor_key: str = QA_INSTANCE) -> bool:
    from agent_runtime import paths

    return paths.office_conflict_path(workspace_id, actor_key).exists()


def _resolve(rid: str, params: dict) -> dict:
    return _reply(
        _run([_rpc(rid, "runtime.office.resolve_conflict", params), SHUTDOWN]), rid
    )


# ── advertisement ───────────────────────────────────────────────────────────


def test_the_method_is_advertised_without_moving_the_contract_integer():
    """The launcher gates per METHOD NAME, never on release notes.

    A handler the manifest does not name is a handler no client will ever reach;
    and the set grows without the integer moving, which is what lets a fielded
    launcher keep resolving on the argv lane while it learns this one. The
    eighth entry — the exact list is pinned by
    ``test_serve_rpc_office.py``/``_subscribe``/``_upsert``, so a name that
    drifted from the plan's spelling reds there too.
    """

    manifest = serve_rpc.manifest()
    assert "runtime.office.resolve_conflict" in manifest["methods"]
    assert manifest["contract"] == 1


# ── the two shapes of success ───────────────────────────────────────────────


def test_take_local_keeps_the_local_actor_and_acks_its_own_revision():
    """The strip's ordinary case: the operator says the local canvas wins.

    The revision acked is the LOCAL actor's — 2, the number the store already
    holds — and not the peer's 7 or the adoption's 8. Asserted as a whole frame,
    then against the store, because a handler that acked the wrong side's number
    would hand the client a guard token for a row it never had.
    """

    store = _seed()
    _seed_conflict()
    before = _actor_bytes()

    reply = _resolve(
        "r-local",
        {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE, "take": "local"},
    )

    assert reply == {
        "jsonrpc": "2.0",
        "id": "r-local",
        "result": {
            "actor_key": QA_INSTANCE,
            "take": "local",
            "state": "active",
            "revision": 2,
        },
    }
    # Keeping the local side really means keeping it: the row is byte-identical,
    # and the conflict is consumed so the strip's pill clears.
    assert _actor_bytes() == before
    assert store.get_actor(WORKSPACE, QA_INSTANCE).revision == 2
    assert not _sidecar_exists()


def test_take_remote_adopts_the_peers_copy_and_acks_the_bumped_revision():
    """The other side, and the one the fence guards.

    ``revision`` is 8 — the peer's 7 bumped by the adoption — and the adopted
    position is the peer's, which is what proves the write happened rather than
    the sidecar merely being consumed.
    """

    _seed()
    _seed_conflict()

    reply = _resolve(
        "r-remote",
        {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE, "take": "remote"},
    )

    assert reply == {
        "jsonrpc": "2.0",
        "id": "r-remote",
        "result": {
            "actor_key": QA_INSTANCE,
            "take": "remote",
            "state": "active",
            "revision": 8,
        },
    }
    adopted = _store().get_actor(WORKSPACE, QA_INSTANCE)
    assert adopted.revision == 8
    assert [item.position for item in adopted.items] == [[9.0, 9.0]]
    assert not _sidecar_exists()


def test_take_remote_on_a_removal_tombstone_acks_archived_with_no_revision():
    """The edit-vs-remove arm: the peer deleted what this side edited.

    ``OfficeStore.resolve_conflict`` returns ``None`` here, and that is a
    SUCCESS — the operator's conflict is resolved and the local row is archived.
    The missing ``revision`` key is the whole signal: it tells the client to stop
    guarding a row that no longer exists. Asserted by key ABSENCE, because a
    handler that filled in ``None`` or ``0`` would pass a shape check and then
    have the launcher present ``expect_revision: 0`` on its next write.
    """

    _seed()
    _seed_conflict(remote_key=None)

    reply = _resolve(
        "r-tomb",
        {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE, "take": "remote"},
    )

    assert reply == {
        "jsonrpc": "2.0",
        "id": "r-tomb",
        "result": {
            "actor_key": QA_INSTANCE,
            "take": "remote",
            "state": "archived",
        },
    }
    assert "revision" not in reply["result"]
    assert _live_keys() == []
    assert QA_INSTANCE in _store().get_surface(WORKSPACE).archived_actor_keys
    assert not _sidecar_exists()


def test_the_ack_names_the_key_the_store_wrote_not_the_one_the_caller_sent():
    """``_write_actor`` routes on the peer RECORD's key, never on the sidecar
    filename — "payload is truth; the filename is routing only"
    (``office_sync._read_remote_office``). So when the two disagree, the ack has
    to carry the key the store actually wrote, or the client settles a row that
    does not exist and never hears about the one that does.

    Both keys here are INSTANCE-keyed, so the class-key fence correctly stands
    aside and the adoption really runs — the disagreement is the only variable.
    """

    _seed()
    _seed_conflict(sidecar_key=QA_INSTANCE, remote_key=QA_INSTANCE_PEER)

    reply = _resolve(
        "r-split",
        {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE, "take": "remote"},
    )

    assert reply["result"]["actor_key"] == QA_INSTANCE_PEER
    assert reply["result"]["revision"] == 8
    assert sorted(_live_keys()) == sorted([QA_INSTANCE, QA_INSTANCE_PEER])
    # The sidecar the operator named is the one consumed.
    assert not _sidecar_exists(actor_key=QA_INSTANCE)


def test_the_take_is_echoed_normalized_so_the_ack_names_the_side_that_won():
    """The store lowercases and trims ``take``; the ack must report the value the
    store acted on, not the spelling the client happened to send. A client that
    keyed its reconciliation off its own string would be right by luck."""

    _seed()
    _seed_conflict()

    reply = _resolve(
        "r-case",
        {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE, "take": "  LOCAL "},
    )

    assert reply["result"]["take"] == "local"
    assert reply["result"]["revision"] == 2


# ── the class-key fence, reached rather than re-implemented ──────────────────


def _migrated_conflict(name: str):
    """A migrated office plus a peer republishing the archived CLASS key.

    THE scenario the re-key migration named as the one operator action it makes
    dangerous, built by the guard suite's own fixtures (which run the REAL
    migration script) rather than by an imitation of the state it leaves.
    """

    from tests.agent_runtime.test_office_class_key_guard import (
        _migrated_workspace,
        _seed_remote_conflict,
    )

    workspace, item_ids = _migrated_workspace(name)
    _seed_remote_conflict(
        workspace, "backend_dev", persona_id="backend_dev", item_ids=item_ids
    )
    return workspace


def test_a_class_keyed_adoption_is_refused_4090_and_the_sidecar_survives():
    """The fence, through the wire.

    Four things are asserted together because each one alone is satisfiable by a
    wrong handler: the CODE (a 4090, the family a client already branches on),
    the REASON (``class_key_collision`` — the same string
    ``runtime.office.upsert`` spends, so one client branch covers both verbs),
    the EVIDENCE (the collision's own reasons and conflicting keys, which only
    ``class_key_collision()`` produces), and the STORE (one placement, ledger
    intact, sidecar still there so ``take: "local"`` remains available — a
    refusal that consumed the conflict it refused would strand the operator).

    The message is BUILT for this lane, not passed through: the store's sentence
    offers ``--allow-class-key``, which is advice this caller cannot follow, and
    the upsert's arm already ruled that advice a caller cannot follow is worse
    than none. So the wire message must name ``take: "local"`` and must NOT name
    the flag.
    """

    from tests.agent_runtime.test_office_class_key_guard import INSTANCE, _keys

    workspace = _migrated_conflict("RPC Refused Office")

    reply = _resolve(
        "r-fence",
        {"workspace_id": workspace, "actor_key": "backend_dev", "take": "remote"},
    )

    assert reply["error"]["code"] == 4090
    data = reply["error"]["data"]
    assert data["reason"] == "class_key_collision"
    assert sorted(data["reasons"]) == [
        "duplicate_item_placement",
        "resurrects_archived_class_key",
    ]
    assert data["conflicting_actor_keys"] == [INSTANCE]
    assert data["persona_id"] == "backend_dev"
    assert data["class_actor_key"] == "backend_dev"
    assert data["take"] == "remote"
    assert 'take: "local"' in reply["error"]["message"]
    assert "--allow-class-key" not in reply["error"]["message"]
    # Nothing written, nothing consumed.
    assert _keys(workspace) == {INSTANCE}
    assert "backend_dev" in _store().get_surface(workspace).archived_actor_keys
    assert _sidecar_exists(workspace, "backend_dev")


def test_an_allow_class_key_key_on_the_wire_is_inert():
    """THE contract rule of this stage: a wire PARAMETER is not consent.

    The CLI's ``--allow-class-key`` is an operator who read the refusal and typed
    the override. The same value in a JSON frame is a constant in a client build,
    set once by whoever was debugging the day resolves started failing and
    thereafter sent by every install on every resolution with no human in any
    loop. So the param does not exist, is not forwarded, and — asserted here —
    sending it anyway changes nothing.

    A handler that threaded it would answer ``ok`` at revision 8 with the class
    key back on the canvas beside its instance-keyed sibling: the exact double
    placement the re-key migration exists to remove, reached through the one
    door the fence did not cover.
    """

    from tests.agent_runtime.test_office_class_key_guard import INSTANCE, _keys

    workspace = _migrated_conflict("RPC Override Attempt Office")

    reply = _resolve(
        "r-force",
        {
            "workspace_id": workspace,
            "actor_key": "backend_dev",
            "take": "remote",
            "allow_class_key": True,
        },
    )

    assert reply["error"]["code"] == 4090
    assert reply["error"]["data"]["reason"] == "class_key_collision"
    assert _keys(workspace) == {INSTANCE}
    assert _sidecar_exists(workspace, "backend_dev")


def test_both_lanes_refuse_the_same_class_keyed_adoption_naming_the_same_fault():
    """Refusal PARITY across the two lanes, on ONE fixture.

    Both lanes run against the same seeded conflict in the same workspace — the
    RPC first, then the CLI — which is only possible because neither refusal
    writes or consumes anything. That is half the assertion.

    The other half is the mapping. The two lanes do NOT spend the same string,
    and pretending otherwise would be the drift this test is supposed to catch:
    the CLI answers in the stage-42 exit taxonomy (``duplicate_conflict``, exit
    4 — ``emit_harness_error`` reads an unnamed ``AgentRuntimeError`` as
    ``internal_error``, so the CLI catches this one explicitly) while the wire
    answers in JSON-RPC's ``data.reason`` (``class_key_collision``, 4090). What
    must be identical is the FAULT: the same collision reasons, from the one
    authority (``office_class_key_guard.class_key_collision``), naming the same
    conflicting key. The code pair is pinned as a pair so a rename on either
    side reds here rather than silently splitting the two lanes' stories.
    """

    from tests.agent_runtime.test_office_class_key_guard import (
        INSTANCE,
        _keys,
        _run_harness,
    )

    workspace = _migrated_conflict("Two Lane Office")

    rpc = _resolve(
        "r-parity",
        {"workspace_id": workspace, "actor_key": "backend_dev", "take": "remote"},
    )
    cli = _run_harness(
        "office", "resolve-conflict", "--workspace", workspace,
        "--actor", "backend_dev", "--take", "remote", "--json",
    )

    assert rpc["error"]["code"] == 4090
    assert rpc["error"]["data"]["reason"] == "class_key_collision"
    assert cli.returncode == 4, cli.stdout + cli.stderr
    cli_error = json.loads(cli.stdout)["error"]
    assert cli_error["code"] == "duplicate_conflict"

    # The fault itself, identical on both lanes.
    for reason in ("resurrects_archived_class_key", "duplicate_item_placement"):
        assert reason in rpc["error"]["data"]["reasons"]
        assert reason in cli_error["message"]
    assert rpc["error"]["data"]["conflicting_actor_keys"] == [INSTANCE]
    assert INSTANCE in cli_error["message"]

    # Two refusals, nothing written, and the conflict still available to the
    # exit both lanes point at.
    assert _keys(workspace) == {INSTANCE}
    assert _sidecar_exists(workspace, "backend_dev")


def test_the_rpc_resolve_writes_only_through_the_stores_fenced_chokepoint(monkeypatch):
    """THE anti-drift pin behind "no new unfenced writer".

    ``take="remote"`` is the one office write that reaches ``_write_actor``
    without passing ``upsert_actor``, which is why its class-key fence lives
    INSIDE ``OfficeStore.resolve_conflict`` — and why a handler that reached for
    any other writer, or wrote the peer's actor itself, would be unfenced by
    construction while every reply-shape test stayed green.

    So this test does not check a shape. It poisons every sibling ``OfficeStore``
    write verb, wraps ``resolve_conflict`` to know when control is inside it, and
    guards ``_write_actor`` with the assertion that an actor file may only be
    written from within that extent. The happy path must still succeed, and the
    write must have really happened — a guard that fires zero times proves
    nothing.

    The sibling list is asserted to EXIST before it is poisoned, so a store
    method renamed out from under this test reds instead of silently
    un-poisoning; a NEW store writer has to be added here by hand, which is the
    cost of the pin and cheaper than the hole.
    """

    from agent_runtime import office_store as office_store_module

    #: Every other write entry point on the store. None of them is on
    #: ``resolve_conflict``'s own call path (it archives through
    #: ``_archive_actor_locked``, not ``remove_actor``).
    SIBLING_WRITERS = (
        "upsert_actor",
        "remove_actor",
        "restore_actor",
        "update_surface",
        "archive_orphaned_surface",
        "archive_actors_for_instance",
    )

    _seed()
    _seed_conflict()

    state = {"depth": 0, "writes": 0}
    real_resolve = office_store_module.OfficeStore.resolve_conflict
    real_write = office_store_module._write_actor

    def _tracked_resolve(self, *args, **kwargs):
        state["depth"] += 1
        try:
            return real_resolve(self, *args, **kwargs)
        finally:
            state["depth"] -= 1

    def _guarded_write(actor):
        assert state["depth"] > 0, (
            "an actor file was written outside OfficeStore.resolve_conflict — "
            "this lane grew a writer that the class-key fence does not cover"
        )
        state["writes"] += 1
        return real_write(actor)

    def _poison(name):
        def _refuse(*_args, **_kwargs):
            raise AssertionError(f"the resolve handler called OfficeStore.{name}")

        return _refuse

    monkeypatch.setattr(
        office_store_module.OfficeStore, "resolve_conflict", _tracked_resolve
    )
    monkeypatch.setattr(office_store_module, "_write_actor", _guarded_write)
    for name in SIBLING_WRITERS:
        assert hasattr(office_store_module.OfficeStore, name), name
        monkeypatch.setattr(office_store_module.OfficeStore, name, _poison(name))

    reply = _resolve(
        "r-choke",
        {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE, "take": "remote"},
    )

    assert "error" not in reply, reply
    assert reply["result"]["revision"] == 8
    assert state["writes"] == 1, "the adoption did not reach the store's writer"
    assert state["depth"] == 0


# ── refusals: each one writes nothing ───────────────────────────────────────


def test_a_nonsense_take_is_refused_before_the_store_is_touched(monkeypatch):
    """``take`` is an enum and its refusal is a typed ``-32602``, checked in the
    handler.

    The store would answer with a bare ``ValueError("invalid_request")`` carrying
    no field name, which this handler could only spend its ``actor_invalid``
    reason on — telling the client to inspect a whole payload whose only fault is
    one word.

    "Before any store call" is asserted rather than assumed: BOTH store entry
    points the handler uses are poisoned, so a handler that validated ``take``
    after opening the workspace would come back ``-32000`` instead. A missing
    ``take`` rides the same reason — an omitted enum and a misspelled one have
    the same cure.
    """

    from agent_runtime import office_store as office_store_module

    def _refuse(*_args, **_kwargs):
        raise AssertionError("the store was touched before take was validated")

    _seed()
    _seed_conflict()
    monkeypatch.setattr(office_store_module.OfficeStore, "surface_exists", _refuse)
    monkeypatch.setattr(office_store_module.OfficeStore, "resolve_conflict", _refuse)

    for rid, params in (
        (
            "r-sideways",
            {
                "workspace_id": WORKSPACE,
                "actor_key": QA_INSTANCE,
                "take": "sideways",
            },
        ),
        ("r-notake", {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE}),
    ):
        reply = _resolve(rid, params)
        assert reply["error"]["code"] == -32602, reply
        assert reply["error"]["data"]["reason"] == "take_invalid"

    assert _sidecar_exists()


def test_an_unknown_workspace_is_refused_workspace_not_found_and_authors_nothing():
    """The upsert's ruling, reached from the resolve side: the read leg refuses a
    typo, so no write verb may author an office for one.

    The REASON is what kills the ordering mutation — with the existence check
    moved below the store call the reply arrives with the same 4001 and the wrong
    story (``conflict_not_found``: "refetch the projection" instead of "this
    workspace has no office").
    """

    from agent_runtime import paths

    _seed()
    _seed_conflict()
    reply = _resolve(
        "r-ws",
        {"workspace_id": "ws_typo", "actor_key": QA_INSTANCE, "take": "local"},
    )

    assert reply["error"]["code"] == 4001
    assert reply["error"]["data"]["reason"] == "workspace_not_found"
    assert not paths.office_surface_path("ws_typo").exists()
    # The real workspace is untouched — the refusal did not land on the wrong
    # place either.
    assert _live_keys() == [QA_INSTANCE]
    assert _sidecar_exists()


def test_an_already_resolved_conflict_is_a_typed_4001_and_adopts_nothing_twice():
    """THE stale-conflict pin, and the one place this verb deliberately disagrees
    with ``runtime.office.remove``.

    A repeat ARCHIVE is an ``ok``: the store is already in the state the caller
    named and nothing is written either way. A repeat RESOLVE has no sidecar
    left to read — the store's own guard refuses with ``no_conflict:<key>`` — and
    the only way to answer ``ok`` would be to invent a remote copy. Answering
    4090 ``sync_conflict`` (the reason the same exception class earns on the
    upsert, where a sidecar EXISTING is what blocks the write) would be worse
    still: it would send the client for an operator to resolve a conflict that
    is already resolved.

    Pinned by BYTES. Dropping the store's sidecar guard, or having the handler
    retry, shows up here as a second adoption bumping the revision to 9 — which
    a "there was an error" assertion could not see.
    """

    _seed()
    _seed_conflict()
    first = _resolve(
        "r-1",
        {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE, "take": "remote"},
    )
    assert first["result"]["revision"] == 8
    adopted = _actor_bytes()

    second = _resolve(
        "r-2",
        {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE, "take": "remote"},
    )

    assert second["error"]["code"] == 4001
    assert second["error"]["data"] == {
        "reason": "conflict_not_found",
        "workspace_id": WORKSPACE,
        "actor_key": QA_INSTANCE,
    }
    assert _actor_bytes() == adopted, "the second resolve re-adopted the peer"


def test_a_key_that_never_had_a_conflict_spends_the_same_4001():
    """Same reason for the same cure. A live actor with no sidecar and a key the
    office never held are both "there is nothing here to resolve", and the
    client's answer to both is to refetch the projection."""

    _seed()

    live = _resolve(
        "r-noconflict",
        {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE, "take": "local"},
    )
    unknown = _resolve(
        "r-nokey",
        {"workspace_id": WORKSPACE, "actor_key": "no_such_key", "take": "local"},
    )

    for reply in (live, unknown):
        assert reply["error"]["code"] == 4001, reply
        assert reply["error"]["data"]["reason"] == "conflict_not_found"
    assert _live_keys() == [QA_INSTANCE]


def test_an_unreadable_archive_surfaces_the_shared_typed_reason_not_a_crash():
    """EG-1.5 / RD-H4 on this verb — a TRANSLATION-boundary test, and honest
    about being one.

    No path inside ``OfficeStore.resolve_conflict`` raises ``ArchiveUnreadable``
    today (the two writers that read an archive copy are ``upsert_actor`` and
    ``remove_actor``), so the condition is injected at the store boundary rather
    than staged on disk. What is being pinned is the ENVELOPE, and it is not
    hypothetical: ``ArchiveUnreadable`` is an ``AgentRuntimeError`` and NOT a
    ``ValueError``, so with no arm for it the refusal falls through every
    ``except`` here into ``handle_request``'s catch-all and reaches the client as
    ``-32000 handler_failed`` — a corrupt file on the server reported as "the
    handler crashed", with no reason to branch on and nothing an operator could
    act on. Removing the arm shows exactly that, which is why the CODE is
    asserted beside the reason.

    The reason string is the SAME one the archive and upsert legs spend, taken
    from the exception class rather than retyped: one condition gets one name
    across the write verbs, not one name per verb.
    """

    from agent_runtime import office_store as office_store_module
    from agent_runtime.errors import ArchiveUnreadable

    _seed()
    _seed_conflict()

    def _unreadable(self, *_args, **_kwargs):
        raise ArchiveUnreadable(f"archive_unreadable:{QA_INSTANCE} (JSONDecodeError)")

    original = office_store_module.OfficeStore.resolve_conflict
    office_store_module.OfficeStore.resolve_conflict = _unreadable
    try:
        reply = _resolve(
            "r-unreadable",
            {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE, "take": "remote"},
        )
    finally:
        office_store_module.OfficeStore.resolve_conflict = original

    assert reply["error"]["code"] == -32600
    assert reply["error"]["data"] == {
        "reason": ArchiveUnreadable.code,
        "workspace_id": WORKSPACE,
        "actor_key": QA_INSTANCE,
    }
    assert reply["error"]["data"]["reason"] == "archive_unreadable"
    # Nothing repaired itself behind the refusal, and the conflict is still
    # there for an operator who fixes the file.
    assert _live_keys() == [QA_INSTANCE]
    assert _sidecar_exists()


def test_a_blank_actor_key_is_a_launcher_bug_not_a_missing_conflict():
    """``-32602``, so the client fixes its payload instead of its office.
    Separate from ``conflict_not_found`` because the cures differ in kind: one is
    a refetch, the other is a bug report."""

    _seed()
    _seed_conflict()
    reply = _resolve(
        "r-blank", {"workspace_id": WORKSPACE, "actor_key": "   ", "take": "local"}
    )

    assert reply["error"]["code"] == -32602
    assert reply["error"]["data"]["reason"] == "actor_key_required"
    assert _sidecar_exists()


def test_a_missing_workspace_id_spends_the_same_reason_every_office_verb_does():
    _seed()
    reply = _resolve("r-nows", {"actor_key": QA_INSTANCE, "take": "local"})

    assert reply["error"]["code"] == -32602
    assert reply["error"]["data"]["reason"] == "workspace_id_required"


def test_a_non_string_updated_by_is_refused_rather_than_stringified():
    _seed()
    _seed_conflict()
    reply = _resolve(
        "r-by",
        {
            "workspace_id": WORKSPACE,
            "actor_key": QA_INSTANCE,
            "take": "local",
            "updated_by": 7,
        },
    )

    assert reply["error"]["code"] == -32602
    assert reply["error"]["data"]["reason"] == "updated_by_invalid"
    assert _sidecar_exists()


# ── the gesture token: echoed, carried, refused ─────────────────────────────

#: Two tokens in the shape the launcher mints (``g-<lane>-<micros>-<rand4>``),
#: distinct because every echo probe below is driven with BOTH. A handler that
#: echoed a constant — or echoed whatever token its last write happened to carry
#: — satisfies a one-token assertion, and that mutant is what this group exists
#: to convict.
GESTURE = "g-office-1755400000123456-a1b2"
GESTURE_SECOND = "g-office-1755400000987654-c3d4"


def _conflict_resolved_tokens(workspace_id: str = WORKSPACE) -> list[str | None]:
    """The token on each ``office.actor.conflict_resolved`` event, in order.

    Read through a fresh ``EventLog`` rather than a seed store's handle, for the
    reason the sibling correlation suite records: the RPC handlers construct
    their own ``OfficeStore``, so a seed store's log handle names a different
    reader of the same file and would only happen to agree. Filtered by
    workspace so a test driving two offices reads each one's own history.
    """

    from agent_runtime.events import EventLog

    return [
        event.payload.get("correlation_id")
        for _, event in EventLog().iter_from_offset(0)
        if event.type == "office.actor.conflict_resolved"
        and event.payload.get("workspace_id") == workspace_id
    ]


def test_the_resolve_ack_echoes_the_callers_correlation_id():
    """The ack names the token the WRITE carried, not the one the client
    remembers sending — the difference between "we think we sent this" and "the
    server wrote this", which is the whole point of echoing it at all.

    Driven with TWO distinct tokens across two calls, one per ``take`` side: a
    handler that echoed a constant, or the token of whatever it wrote last,
    passes a single-token probe. Whole frames, because the key has to land
    BESIDE the existing ack — a client reading ``revision`` as its next guard
    token must find it unchanged.
    """

    _seed()
    _seed_conflict()

    local = _resolve(
        "r-corr-local",
        {
            "workspace_id": WORKSPACE,
            "actor_key": QA_INSTANCE,
            "take": "local",
            "correlation_id": GESTURE,
        },
    )

    assert local == {
        "jsonrpc": "2.0",
        "id": "r-corr-local",
        "result": {
            "actor_key": QA_INSTANCE,
            "take": "local",
            "state": "active",
            "revision": 2,
            "correlation_id": GESTURE,
        },
    }

    # A SECOND gesture on the same row: the first call consumed the sidecar, so
    # this is a new conflict, resolved the other way, under a new token.
    _seed_conflict()
    remote = _resolve(
        "r-corr-remote",
        {
            "workspace_id": WORKSPACE,
            "actor_key": QA_INSTANCE,
            "take": "remote",
            "correlation_id": GESTURE_SECOND,
        },
    )

    assert remote == {
        "jsonrpc": "2.0",
        "id": "r-corr-remote",
        "result": {
            "actor_key": QA_INSTANCE,
            "take": "remote",
            "state": "active",
            "revision": 8,
            "correlation_id": GESTURE_SECOND,
        },
    }


def test_the_token_the_ack_echoes_is_the_token_the_store_recorded():
    """The JOIN, which neither half proves alone.

    The echo above reads a handler LOCAL; this reads what was actually threaded
    into ``OfficeStore.resolve_conflict`` and emitted on
    ``office.actor.conflict_resolved``. Drop the ``correlation_id=`` argument at
    the store call and every echo assertion in this file still passes while the
    event lane — the one a diagnostician greps — goes silent for this verb, which
    is precisely the "both halves right, the join untested" shape.

    Asserted as an ORDERED list of two distinct tokens: a constant cannot match
    it, and neither can a carry that survives on only one of the two arms.
    """

    _seed()
    _seed_conflict()
    first = _resolve(
        "r-carry-local",
        {
            "workspace_id": WORKSPACE,
            "actor_key": QA_INSTANCE,
            "take": "local",
            "correlation_id": GESTURE,
        },
    )
    assert "error" not in first, first

    _seed_conflict()
    second = _resolve(
        "r-carry-remote",
        {
            "workspace_id": WORKSPACE,
            "actor_key": QA_INSTANCE,
            "take": "remote",
            "correlation_id": GESTURE_SECOND,
        },
    )
    assert "error" not in second, second

    assert _conflict_resolved_tokens() == [GESTURE, GESTURE_SECOND]


def test_a_resolve_with_no_gesture_behind_it_omits_the_key_entirely():
    """The fence that keeps the token additive: a client that never learned this
    key reads exactly the frame it always did.

    Asserted as a whole frame AND by key absence, because an always-echo handler
    — one stamping ``correlation_id: null`` or an empty string — satisfies "the
    token is right when present" while putting a null in front of every existing
    decoder. The store side is pinned the same way: no gesture, no key on the
    event either.
    """

    _seed()
    _seed_conflict()

    reply = _resolve(
        "r-corr-absent",
        {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE, "take": "local"},
    )

    assert reply == {
        "jsonrpc": "2.0",
        "id": "r-corr-absent",
        "result": {
            "actor_key": QA_INSTANCE,
            "take": "local",
            "state": "active",
            "revision": 2,
        },
    }
    assert "correlation_id" not in reply["result"]
    assert _conflict_resolved_tokens() == [None]


def test_a_malformed_correlation_id_is_refused_before_the_store_is_touched(monkeypatch):
    """``-32602`` with THE shared reason, and the store never opened.

    Two malformed shapes, because they arrive from different bugs and must land
    on one client branch: a NON-STRING (a client that sent its own id object
    where a token belongs) and an OVER-LONG token — ``CORRELATION_ID_MAX_LEN``
    plus one otherwise-legal character, so what is read here is the LENGTH fence
    rather than the character class.

    Refused rather than sanitized, which is this lane's asymmetry against
    ``normalize_correlation_id``'s silent drop: a repaired id would print a value
    neither side used. "Before the store" is asserted the way the ``take``
    refusal asserts it — both store entry points poisoned — so a handler that
    validated the token after opening the workspace comes back ``-32000``
    instead; and the conflict is still there for the client that fixes its
    payload.
    """

    from agent_runtime import office_store as office_store_module
    from agent_runtime.state_patches import CORRELATION_ID_MAX_LEN

    _seed()
    _seed_conflict()
    before = _actor_bytes()

    def _refuse(*_args, **_kwargs):
        raise AssertionError(
            "the store was touched before correlation_id was validated"
        )

    monkeypatch.setattr(office_store_module.OfficeStore, "surface_exists", _refuse)
    monkeypatch.setattr(office_store_module.OfficeStore, "resolve_conflict", _refuse)

    for rid, token in (
        ("r-corr-typed", 12345),
        ("r-corr-long", "g" * (CORRELATION_ID_MAX_LEN + 1)),
    ):
        reply = _resolve(
            rid,
            {
                "workspace_id": WORKSPACE,
                "actor_key": QA_INSTANCE,
                "take": "local",
                "correlation_id": token,
            },
        )

        assert reply["error"]["code"] == -32602, reply
        assert reply["error"]["data"] == {
            "reason": serve_rpc.CORRELATION_ID_INVALID_REASON,
            "workspace_id": WORKSPACE,
        }
        # The spelling as well as the constant: the launcher branches on this
        # literal string, so a renamed VALUE behind the constant reds here.
        assert reply["error"]["data"]["reason"] == "correlation_id_invalid"

    assert _actor_bytes() == before
    assert _sidecar_exists()


def test_the_tombstone_arm_carries_the_token_and_the_store_shows_the_archive():
    """The OTHER echo site. The edit-vs-remove arm builds its own result dict, so
    it is a second place the key can be forgotten — and one no assertion about
    the actor arm can see.

    Two workspaces, two tokens, for the reason the echo test gives. Each frame is
    asserted WHOLE, so the token has to land beside ``state: "archived"`` without
    a ``revision`` key arriving with it: the absence is what tells the client to
    stop guarding a row that no longer exists. The store read-back is what makes
    this a tombstone rather than a shape — the row really is archived, and the
    store's own event carries the same token the ack echoed.
    """

    for rid, workspace, token in (
        ("r-tomb-corr", WORKSPACE, GESTURE),
        ("r-tomb-corr-second", f"{WORKSPACE}_second", GESTURE_SECOND),
    ):
        _seed(workspace)
        _seed_conflict(workspace, remote_key=None)

        reply = _resolve(
            rid,
            {
                "workspace_id": workspace,
                "actor_key": QA_INSTANCE,
                "take": "remote",
                "correlation_id": token,
            },
        )

        assert reply == {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "actor_key": QA_INSTANCE,
                "take": "remote",
                "state": "archived",
                "correlation_id": token,
            },
        }
        assert "revision" not in reply["result"]
        assert _live_keys(workspace) == []
        assert QA_INSTANCE in _store().get_surface(workspace).archived_actor_keys
        assert _conflict_resolved_tokens(workspace) == [token]


# ── coverage, deliberately unmoved ──────────────────────────────────────────


def test_the_batch_a_resolve_lands_in_still_demotes_for_every_client(monkeypatch):
    """The stage's "no coverage change" claim, pinned rather than asserted in
    prose.

    ``office.actor.conflict_resolved`` is deliberately absent from
    ``LIVE_COVERED_DOMAIN_EVENT_TYPES`` and must stay absent: the adoption writes
    a peer's row with ``_write_actor``, past the ``_emit_actor_patch`` chokepoint,
    so NO patch carries the adopted actor. A client that folded this batch would
    render the row it had before the peer's copy landed — the demote is what
    keeps it honest.

    Asserted from both ends: the batch really carries the domain event, it
    carries no ``office_actor`` patch at all, and it is uncoverable even for a
    client declaring every fold token this runtime knows.

    The tombstone arm is the POSITIVE CONTROL, and it is why this test covers two
    scenarios instead of one. ``_archive_actor_locked`` emits its paired remove
    patch even with the domain event suppressed, so that batch DOES carry a
    patch — which proves patches are genuinely enabled here and that "no patch"
    above is a finding rather than an artifact of the default config. It still
    demotes, on its own uncovered ``conflict_resolved``, exactly as the coverage
    module's comment block says it should.
    """

    from agent_runtime import state_patches as sp
    from agent_runtime.config import load_agent_runtime_config
    from agent_runtime.events import EventLog
    from agent_runtime.patch_coverage import (
        HISTORICAL_FOLD_ENTITIES,
        OFFICE_ACTOR_LIFECYCLE_CAPABILITY,
        OFFICE_SURFACE_FOLD_CAPABILITY,
        batch_is_patch_coverable,
    )
    from agent_runtime.state_patches import (
        OFFICE_ACTOR_ENTITY,
        STATE_PATCHED_EVENT_TYPE,
    )

    def _loader(*args, **kwargs):
        cfg = load_agent_runtime_config(*args, **kwargs)
        cfg.read_model.delta_patches = True
        return cfg

    monkeypatch.setattr(sp, "load_root_runtime_config", _loader)

    _seed()
    _seed_conflict()
    before = max((o for o, _ in EventLog().iter_from_offset(0)), default=0)

    reply = _resolve(
        "r-cover",
        {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE, "take": "remote"},
    )
    assert "error" not in reply, reply

    batch = [e for _, e in EventLog().iter_from_offset(before)]
    types = [e.type for e in batch]
    assert "office.actor.conflict_resolved" in types, types
    assert [
        e.payload["entity"] for e in batch if e.type == STATE_PATCHED_EVENT_TYPE
    ] == [], types

    widened = HISTORICAL_FOLD_ENTITIES | {
        OFFICE_ACTOR_ENTITY,
        OFFICE_ACTOR_LIFECYCLE_CAPABILITY,
        OFFICE_SURFACE_FOLD_CAPABILITY,
    }
    assert not batch_is_patch_coverable(batch, fold_entities=widened), types

    # The positive control: the tombstone arm DOES emit a patch, so the producer
    # is live in this run — and that batch demotes too.
    tombstone_workspace = f"{WORKSPACE}_tombstone"
    _seed(tombstone_workspace)
    _seed_conflict(tombstone_workspace, remote_key=None)
    before = max((o for o, _ in EventLog().iter_from_offset(0)), default=0)

    reply = _resolve(
        "r-cover-tomb",
        {
            "workspace_id": tombstone_workspace,
            "actor_key": QA_INSTANCE,
            "take": "remote",
        },
    )
    assert "error" not in reply, reply

    batch = [e for _, e in EventLog().iter_from_offset(before)]
    types = [e.type for e in batch]
    rows = [e.payload for e in batch if e.type == STATE_PATCHED_EVENT_TYPE]
    assert [(r["entity"], r["op"]) for r in rows] == [
        (OFFICE_ACTOR_ENTITY, "remove")
    ], rows
    assert not batch_is_patch_coverable(batch, fold_entities=widened), types
