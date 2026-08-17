"""The METHOD lane's ARCHIVE leg: ``runtime.office.remove``.

Sibling of ``test_serve_rpc_office_upsert.py`` and built on
``test_serve_rpc_office.py``'s helpers rather than a second copy of them.

What an ARCHIVE has to prove that an upsert does not:

1. **The acked revision is the POST-archive one.** It is the token a later
   guarded write on this key must present (an archived key carries its number
   forward through a restore), so acking the pre-archive value would hand the
   client a guard that is already one behind. Asserted as a whole frame, and
   against the number the store really holds — not against a literal alone,
   which would pass against a handler that returned a constant.

2. **An already-archived key is an OK.** The launcher re-names a key it has
   already deleted whenever a later save recomputes the same vacated set. The
   store is idempotent there on purpose and this lane must not convert that
   into an error with a rollback behind it. Pinned by BYTES: the archive file
   must be unchanged, so "ok" cannot be reached by re-archiving.

3. **Every refusal WRITES NOTHING.** Each error test re-reads the store and
   pins the actor still LIVE. A typed refusal in front of a store that took the
   archive anyway is the worst outcome available: the client puts the placement
   back on the canvas and the server has deleted it.

4. **The two 4001s are DIFFERENT.** ``workspace_not_found`` and
   ``actor_not_found`` share a code and have different cures (re-author /
   refetch the projection), so the reason string is the branch point and is
   asserted as such.

5. **The RPC path reaches the SAME producer chokepoint the CLI does** — the
   anti-drift pin behind this stage's "no producer, coverage or fixture change
   is needed" claim.
"""

from __future__ import annotations

import json

from agent_runtime import serve_rpc
from tests.agent_runtime.test_serve_rpc_office import (
    SHUTDOWN,
    _reply,
    _rpc,
    _run,
)

WORKSPACE = "ws_rpc_remove_test"

#: The instance-bound actor the seed places, and the key the store canonicalizes
#: its identity triple onto.
QA_INSTANCE = "personainst_qa_agent_9c8a382f"


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

    TWO writes, not one, so the acked revision is 3 rather than 2 — an
    off-by-one in either direction lands on a number no other quantity in the
    fixture equals.
    """

    store = _store()
    store.ensure_surface(workspace_id, created_by="seed")
    store.upsert_actor(workspace_id, _actor_payload(), updated_by="seed-operator")
    store.upsert_actor(workspace_id, _actor_payload(), updated_by="seed-operator")
    assert store.get_actor(workspace_id, QA_INSTANCE).revision == 2
    return store


def _live_keys(workspace_id: str = WORKSPACE) -> list[str]:
    return [a.actor_key for a in _store().list_actors(workspace_id)]


def _archive_bytes(workspace_id: str = WORKSPACE, actor_key: str = QA_INSTANCE):
    from agent_runtime import paths

    path = paths.office_archived_actor_path(workspace_id, actor_key)
    return path.read_bytes() if path.exists() else None


def _remove(rid: str, params: dict) -> dict:
    return _reply(_run([_rpc(rid, "runtime.office.remove", params), SHUTDOWN]), rid)


# ── advertisement ───────────────────────────────────────────────────────────


def test_the_method_is_advertised_without_moving_the_contract_integer():
    """The launcher gates per METHOD NAME, never on release notes.

    A handler the manifest does not name is a handler no client will ever
    reach; and the set grows without the integer moving, which is what lets a
    fielded launcher keep reading while it learns to archive.
    """

    manifest = serve_rpc.manifest()
    assert "runtime.office.remove" in manifest["methods"]
    assert manifest["contract"] == 1


# ── the happy path, and the whole ack ───────────────────────────────────────


def test_the_remove_archives_the_actor_and_acks_the_post_archive_revision():
    """The ack, asserted as a WHOLE frame and against server truth.

    ``revision`` is 3 — the store's number AFTER the archive bump, which is the
    token a later guarded write on this key must present. Acking the 2 the
    caller was holding would be a guard that is permanently one behind.
    """

    store = _seed()
    reply = _remove("r1", {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE})

    assert reply == {
        "jsonrpc": "2.0",
        "id": "r1",
        "result": {
            "actor_key": QA_INSTANCE,
            "revision": 3,
            "state": "archived",
        },
    }
    # …and the number is the store's, not a literal this test and the handler
    # happen to agree on.
    archived = [
        a
        for a in store.list_actors(WORKSPACE, include_archived=True)
        if a.actor_key == QA_INSTANCE
    ]
    assert [a.revision for a in archived] == [3]
    # The archive really happened: an ack in front of a live actor is the one
    # failure a shape assertion cannot see.
    assert _live_keys() == []
    assert QA_INSTANCE in _store().get_surface(WORKSPACE).archived_actor_keys


def test_a_guarded_remove_presenting_the_current_revision_is_accepted():
    """``expect_revision`` is passed THROUGH, not swallowed.

    Without this the stale-revision test below is satisfied by a handler that
    refuses every guarded archive, and D-W1 (guarding the launcher's archive)
    would ship onto a lane that only ever says no.
    """

    _seed()
    reply = _remove(
        "r-guarded",
        {
            "workspace_id": WORKSPACE,
            "actor_key": QA_INSTANCE,
            "expect_revision": 2,
        },
    )

    assert reply["result"]["revision"] == 3
    assert _live_keys() == []


# ── the idempotent arm ──────────────────────────────────────────────────────


def test_an_already_archived_key_is_an_ok_and_writes_nothing():
    """The repeat delete, which the launcher's flush really produces.

    Pinned by BYTES rather than by "the reply had no error": a handler that
    re-ran the archive would also answer ok, at a bumped revision, having
    rewritten the file. The unchanged bytes are what make the second call a
    genuine no-op and not a second archive.
    """

    _seed()
    first = _remove("r1", {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE})
    before = _archive_bytes()
    assert before is not None

    second = _remove("r2", {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE})

    assert "error" not in second, second
    assert second["result"] == first["result"]
    assert _archive_bytes() == before, "the second remove rewrote the archive"


# ── refusals: each one writes nothing ───────────────────────────────────────


def test_an_unreadable_archive_on_the_idempotent_arm_is_typed_not_a_crash():
    """EG-1.5 / RD-H4, the remove half. The idempotent arm's ack CARRIES the
    revision.

    That arm exists so a repeat delete is harmless, and the number it returns is
    the token a later guarded write on this key must present — it can only come
    from the archive copy. An undecodable archive there used to surface as
    whatever the JSON decoder raised — and ``JSONDecodeError`` IS a
    ``ValueError``, so it landed in the ``actor_invalid`` arm and told the client
    to fix its payload for a corrupt file on the server. That is worse than an
    untyped crash: it names the wrong party.

    **Anti-vacuity.** Dropping the guard reproduces exactly that, and the reply
    comes back ``-32602`` — which is why the CODE is probed beside the reason
    rather than just "there was an error".

    Same reason string the upsert leg spends, deliberately: ONE condition gets
    one name, not one name per verb — that is the pairing
    ``test_serve_rpc_office_upsert.py``'s twin pins from the other side.
    """

    from agent_runtime import paths

    _seed()
    _remove("r1", {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE})
    archived_path = paths.office_archived_actor_path(WORKSPACE, QA_INSTANCE)
    archived_path.write_text("{truncated", encoding="utf-8")

    reply = _remove("r2", {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE})

    assert reply["error"]["code"] == -32600
    assert reply["error"]["data"] == {
        "reason": "archive_unreadable",
        "workspace_id": WORKSPACE,
        "actor_key": QA_INSTANCE,
    }
    # Nothing repaired itself behind the refusal: the corrupt archive is left for
    # an operator, and no live actor file was resurrected in its place.
    assert archived_path.read_text(encoding="utf-8") == "{truncated"
    assert _live_keys() == []


def test_both_lanes_refuse_the_same_unreadable_archive_naming_the_same_fault():
    """Refusal PARITY across the two lanes, on ONE staged archive.

    Both lanes run against the same undecodable archive copy in the same
    workspace — the RPC first, then the CLI — which is only possible because
    neither refusal writes or consumes anything. That is half the assertion.

    The other half is the mapping, and the two lanes do NOT spend the same
    string: the wire answers in JSON-RPC's ``data.reason`` (``-32600`` +
    ``archive_unreadable``) while the CLI answers in the stage-42 exit taxonomy
    (``archive_unreadable``, exit 7). What must be identical is the FAULT — one
    condition, one name, one file to repair — so the code pair is pinned AS a
    pair and a rename on either side reds here rather than quietly splitting the
    two lanes' stories. (This is the shape
    ``test_serve_rpc_office_resolve.py::test_both_lanes_refuse_the_same_class_keyed_adoption_naming_the_same_fault``
    records for the class-key refusal; its docstring explains why the
    vocabularies differ at all.)

    Exit 7, not 1, is the point of the test. Until EG-4.5's follow-up
    ``_error_code_for_exception`` had no row for ``ArchiveUnreadable``, so this
    exact call fell through the ``AgentRuntimeError`` catch-all to
    ``internal_error`` at exit 1: the wire named a corrupt file on the server and
    the CLI, from the same store on the same disk, called it a harness crash.

    **Mutation.** Remove the ``ArchiveUnreadable`` row from
    ``hermes_cli/harness_support.py::_error_code_for_exception`` and the CLI half
    comes back ``internal_error`` at exit 1 while the RPC half stays green — which
    is why BOTH lanes are read from one fixture instead of trusting the wire test
    above to speak for the CLI. ``actor-remove`` is the verb on purpose: it holds
    no ``except`` arm of its own (unlike ``actor-upsert``, EG-6.6), so the mapping
    is the only thing standing between it and ``internal_error``.
    """

    from agent_runtime import paths
    from tests.agent_runtime.test_office_class_key_guard import _run_harness

    _seed()
    _remove("r1", {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE})
    archived_path = paths.office_archived_actor_path(WORKSPACE, QA_INSTANCE)
    archived_path.write_text("{truncated", encoding="utf-8")

    rpc = _remove("r2", {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE})
    cli = _run_harness(
        "office", "actor-remove", "--workspace", WORKSPACE,
        "--actor", QA_INSTANCE, "--json",
    )

    assert rpc["error"]["code"] == -32600, rpc
    assert rpc["error"]["data"]["reason"] == "archive_unreadable"
    assert cli.returncode == 7, cli.stdout + cli.stderr
    cli_error = json.loads(cli.stdout)["error"]
    assert cli_error["code"] == "archive_unreadable", cli.stdout
    # The exit family is a claim about the operator's next move, so the envelope
    # has to make the same claim: a 7 beside ``retryable: false`` would have one
    # envelope disagreeing with itself about one fault.
    assert cli_error["retryable"] is True, cli_error
    # And the hint names the FILE rather than the taxonomy's default "correct
    # your request", which is the one cure that cannot work here.
    assert "archive" in cli_error["hint"], cli_error["hint"]

    # The fault itself, identical on both lanes: the same actor key, in the same
    # workspace, from the same undecodable copy.
    assert rpc["error"]["data"]["actor_key"] == QA_INSTANCE
    assert rpc["error"]["data"]["workspace_id"] == WORKSPACE
    assert QA_INSTANCE in cli_error["message"]

    # Two refusals, nothing written, and the file still there for the operator
    # both lanes just pointed at it.
    assert archived_path.read_text(encoding="utf-8") == "{truncated"
    assert _live_keys() == []


def test_an_unknown_workspace_is_refused_workspace_not_found_and_authors_nothing():
    """The upsert's ruling, reached from the archive side.

    The REASON is the assertion, and it is what kills the ordering mutation:
    with the existence check moved below the store call the store raises
    ``NotFound`` about the actor, so the frame arrives with the same 4001 code
    and the wrong story — "resend a different key" instead of "this workspace
    has no office".

    The no-office assertion beside it kills a different mutation (dropping the
    check and letting ``ensure_surface`` lazily author a surface for a typo),
    and is honest about being unable to see the ordering one on its own.
    """

    from agent_runtime import paths

    _seed()
    reply = _remove("r-ws", {"workspace_id": "ws_typo", "actor_key": QA_INSTANCE})

    assert reply["error"]["code"] == 4001
    assert reply["error"]["data"]["reason"] == "workspace_not_found"
    assert not paths.office_surface_path("ws_typo").exists()
    # The real workspace is untouched — the refusal did not reach a store write
    # aimed at the wrong place either.
    assert _live_keys() == [QA_INSTANCE]


def test_an_unknown_actor_in_a_real_office_is_its_OWN_4001():
    """Two 4001s, two cures, so the reason string is the branch point.

    A client that read ``workspace_not_found`` here would conclude its office
    is unauthored and stop reading it; the true cure is to refetch the
    projection, because the key it holds is one the store does not have.
    """

    _seed()
    reply = _remove("r-key", {"workspace_id": WORKSPACE, "actor_key": "no_such_key"})

    assert reply["error"]["code"] == 4001
    assert reply["error"]["data"]["reason"] == "actor_not_found"
    assert reply["error"]["data"]["actor_key"] == "no_such_key"
    assert _live_keys() == [QA_INSTANCE]


def test_a_stale_expect_revision_is_refused_4090_with_no_revision_in_data():
    """The guard, and the number deliberately withheld.

    ``data`` carries no current revision on purpose: handing it back invites a
    retry with it, which is exactly the lost update the guard just refused. The
    number rides ``message``, where an operator can read it and a decoder will
    not.
    """

    _seed()
    reply = _remove(
        "r-stale",
        {
            "workspace_id": WORKSPACE,
            "actor_key": QA_INSTANCE,
            "expect_revision": 1,
        },
    )

    assert reply["error"]["code"] == 4090
    data = reply["error"]["data"]
    assert data["reason"] == "stale_revision"
    assert "revision" not in data, data
    assert data["expect_revision"] == 1
    # The refusal wrote nothing.
    assert _live_keys() == [QA_INSTANCE]
    assert _archive_bytes() is None


def test_a_blank_actor_key_is_a_launcher_bug_not_a_not_found():
    """``-32602``, so the client fixes its payload instead of its office.

    Separate from ``actor_not_found`` because the cures are different in kind:
    one is a refetch, the other is a bug report.
    """

    _seed()
    reply = _remove("r-blank", {"workspace_id": WORKSPACE, "actor_key": "   "})

    assert reply["error"]["code"] == -32602
    assert reply["error"]["data"]["reason"] == "actor_key_required"
    assert _live_keys() == [QA_INSTANCE]


def test_a_boolean_expect_revision_is_refused_rather_than_read_as_one():
    """``True`` is an ``int`` in Python and would silently mean revision 1.

    A wrong guard is worse than no guard: it refuses the writes it should pass
    and passes the one it should refuse.
    """

    _seed()
    reply = _remove(
        "r-bool",
        {
            "workspace_id": WORKSPACE,
            "actor_key": QA_INSTANCE,
            "expect_revision": True,
        },
    )

    assert reply["error"]["code"] == -32602
    assert reply["error"]["data"]["reason"] == "expect_revision_invalid"
    assert _live_keys() == [QA_INSTANCE]


def test_a_missing_workspace_id_spends_the_same_reason_every_office_verb_does():
    _seed()
    reply = _remove("r-nows", {"actor_key": QA_INSTANCE})

    assert reply["error"]["code"] == -32602
    assert reply["error"]["data"]["reason"] == "workspace_id_required"


# ── the producer chokepoint, unchanged ──────────────────────────────────────


def test_the_rpc_archive_reaches_the_same_chokepoint_the_cli_does(monkeypatch):
    """THE ANTI-DRIFT PIN behind "this stage needs no producer change".

    O-H1 put the paired ``office_actor`` remove patch inside
    ``_archive_actor_locked`` and O-H3 covered ``office.actor.removed``. Both
    live UNDER ``OfficeStore``, so a remove that arrives over JSON-RPC gets
    them for free — and this test is what stops that being an assumption. It
    drives the archive entirely through the RPC lane and asserts the batch the
    store produced is foldable for a lifecycle-declared client and still
    demotes for a fielded one.

    Both directions on the SAME batch, deliberately: a coverage assertion that
    only shows promotion is green against a classifier that promotes
    everything.
    """

    from agent_runtime import state_patches as sp
    from agent_runtime.config import load_agent_runtime_config
    from agent_runtime.events import EventLog
    from agent_runtime.patch_coverage import (
        HISTORICAL_FOLD_ENTITIES,
        OFFICE_ACTOR_LIFECYCLE_CAPABILITY,
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
    before = max((o for o, _ in EventLog().iter_from_offset(0)), default=0)

    reply = _remove("r-fold", {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE})
    assert "error" not in reply, reply

    batch = [e for _, e in EventLog().iter_from_offset(before)]
    types = [e.type for e in batch]
    assert "office.actor.removed" in types, types

    rows = [e.payload for e in batch if e.type == STATE_PATCHED_EVENT_TYPE]
    assert [(r["entity"], r["op"]) for r in rows] == [
        (OFFICE_ACTOR_ENTITY, "remove")
    ], rows

    widened = HISTORICAL_FOLD_ENTITIES | {
        OFFICE_ACTOR_ENTITY,
        OFFICE_ACTOR_LIFECYCLE_CAPABILITY,
    }
    assert batch_is_patch_coverable(batch, fold_entities=widened), types
    # A fielded launcher keeps today's wire: the office remove is token-gated.
    assert not batch_is_patch_coverable(
        batch, fold_entities=HISTORICAL_FOLD_ENTITIES | {OFFICE_ACTOR_ENTITY}
    )
