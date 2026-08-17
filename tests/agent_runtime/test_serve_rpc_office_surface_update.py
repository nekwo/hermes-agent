"""The METHOD lane's FOLDER leg: ``runtime.office.surface.update``.

Sibling of ``test_serve_rpc_office_remove.py`` and built on
``test_serve_rpc_office.py``'s helpers rather than a second copy of them.

What a SURFACE write has to prove that neither actor verb does:

1. **The reply echoes the store's NORMALIZED list, not the caller's input.**
   ``_normalize_folders`` prepends the defaults, drops duplicates and blanks and
   stops at 64 — so an echo of the input would be a lie the launcher then adopts
   as server truth, leaving the two permanently disagreeing and the folder
   branch re-firing on every flush forever. Fed deliberately unnormalized input,
   and asserted against what the store really holds rather than against a
   literal this test and the handler happen to agree on.

2. **An unknown workspace AUTHORS NOTHING.** This is the sharpest version of the
   upsert's ruling: ``update_surface`` calls ``ensure_surface`` on its own, so a
   handler missing the existence check does not merely answer wrongly — it
   creates an office on disk for a typo the READ leg refuses. Pinned by the
   absence of ``office.json``, which no reply-shape assertion can see.

3. **A non-list ``folders`` is a REFUSAL, not a reset.** The store answers
   garbage with the DEFAULT list and no error, so a handler that passed it
   through would silently wipe an operator's taxonomy and ack it.

4. **Every refusal writes nothing** — each error test re-reads the surface and
   pins ``folders`` and ``revision`` unchanged.
"""

from __future__ import annotations

from agent_runtime import serve_rpc
from tests.agent_runtime.test_serve_rpc_office import (
    SHUTDOWN,
    _reply,
    _rpc,
    _run,
)

WORKSPACE = "ws_rpc_surface_test"

#: The store prepends these to every normalized list, always, in this order.
DEFAULTS = ["Agents", "Desks"]


# ── seeding ─────────────────────────────────────────────────────────────────


def _store():
    from agent_runtime.office_store import OfficeStore

    return OfficeStore()


def _seed(workspace_id: str = WORKSPACE):
    """An authored office whose surface sits at revision 2.

    TWO writes on top of the create's own revision 1, so the acked revision is
    3 — a number no other quantity in this fixture equals, which an off-by-one
    in either direction cannot land on by accident.
    """

    store = _store()
    store.ensure_surface(workspace_id, created_by="seed")
    store.update_surface(workspace_id, folders=["Design"], updated_by="seed-operator")
    surface = store.get_surface(workspace_id)
    assert surface.revision == 2
    assert list(surface.folders) == [*DEFAULTS, "Design"]
    return store


def _surface(workspace_id: str = WORKSPACE):
    return _store().get_surface(workspace_id)


def _update(rid: str, params: dict) -> dict:
    return _reply(
        _run([_rpc(rid, "runtime.office.surface.update", params), SHUTDOWN]), rid
    )


# ── advertisement ───────────────────────────────────────────────────────────


def test_the_method_is_advertised_without_moving_the_contract_integer():
    """The launcher gates per METHOD NAME, never on release notes.

    A handler the manifest does not name is a handler no client will ever
    reach; and the set grows without the integer moving, which is what lets a
    fielded launcher keep writing folders on the argv lane while it learns this
    one.
    """

    manifest = serve_rpc.manifest()
    assert "runtime.office.surface.update" in manifest["methods"]
    assert manifest["contract"] == 1


# ── the echo, which is the whole point of the reply ─────────────────────────


def test_the_reply_echoes_the_stores_normalized_list_and_the_bumped_revision():
    """THE anti-rewrite-loop pin.

    The input is deliberately unnormalized in FOUR independent ways — a
    duplicate of a default, a duplicate of itself, a blank, and an
    out-of-declaration-order default — so an echo of the input differs from the
    store's answer in shape, not merely in whitespace. Asserted as a whole
    frame, and then against the surface the store really holds: a handler that
    echoed its own input would pass a literal comparison written from the same
    misunderstanding.
    """

    _seed()
    reply = _update(
        "s1",
        {
            "workspace_id": WORKSPACE,
            "folders": ["Ops", "Desks", "Ops", "   ", "Design"],
        },
    )

    assert reply == {
        "jsonrpc": "2.0",
        "id": "s1",
        "result": {
            "workspace_id": WORKSPACE,
            "folders": ["Agents", "Desks", "Ops", "Design"],
            "revision": 3,
        },
    }
    # …and the list is the STORE's, not one this test and the handler agree on.
    surface = _surface()
    assert list(surface.folders) == reply["result"]["folders"]
    assert surface.revision == reply["result"]["revision"]


def test_the_defaults_survive_a_caller_that_names_none_of_them():
    """The store always prepends ``DEFAULT_FOLDERS``.

    Separate from the test above because it kills a different mutation: a
    handler that echoed ``params['folders']`` verbatim answers the same list the
    caller sent, which HERE is missing both defaults — the launcher would then
    hold a two-folder-short taxonomy as server truth and re-write it every
    flush.
    """

    _seed()
    reply = _update("s-def", {"workspace_id": WORKSPACE, "folders": ["Ops"]})

    assert reply["result"]["folders"] == ["Agents", "Desks", "Ops"]
    assert list(_surface().folders) == ["Agents", "Desks", "Ops"]


def test_an_over_long_list_is_capped_and_the_echo_says_so():
    """``MAX_FOLDERS`` is 64 INCLUDING the two prepended defaults.

    The cap is the normalization difference a client could not predict without
    duplicating the store's constant, which is exactly why the echo exists.
    """

    _seed()
    reply = _update(
        "s-cap",
        {
            "workspace_id": WORKSPACE,
            "folders": [f"F{i:03d}" for i in range(200)],
        },
    )

    folders = reply["result"]["folders"]
    assert len(folders) == 64
    assert folders[:2] == DEFAULTS
    assert folders[-1] == "F061"
    assert list(_surface().folders) == folders


def test_a_guarded_write_presenting_the_current_revision_is_accepted():
    """``expect_revision`` is passed THROUGH, not swallowed.

    Without this the stale test below is satisfied by a handler that refuses
    every guarded write, and the launcher's guard would ship onto a lane that
    only ever says no.
    """

    _seed()
    reply = _update(
        "s-guarded",
        {"workspace_id": WORKSPACE, "folders": ["Ops"], "expect_revision": 2},
    )

    assert reply["result"]["revision"] == 3
    assert list(_surface().folders) == ["Agents", "Desks", "Ops"]


# ── refusals: each one writes nothing ───────────────────────────────────────


def test_an_unknown_workspace_is_refused_and_NO_office_is_authored():
    """The ruling this method exists to keep, and the one it would break loudest.

    ``update_surface`` calls ``ensure_surface`` unconditionally, so a handler
    without the existence check answers ``ok`` AND leaves a whole office on disk
    for a workspace the read leg refuses. The ``office.json`` assertion is what
    kills that mutation; the reason assertion beside it kills the cheaper one of
    answering the wrong 4001 story.
    """

    from agent_runtime import paths

    _seed()
    reply = _update("s-ws", {"workspace_id": "ws_typo", "folders": ["Ops"]})

    assert reply["error"]["code"] == 4001
    assert reply["error"]["data"]["reason"] == "workspace_not_found"
    assert not paths.office_surface_path("ws_typo").exists()
    # The real workspace is untouched — the refusal did not reach a store write
    # aimed at the wrong place either.
    assert list(_surface().folders) == [*DEFAULTS, "Design"]
    assert _surface().revision == 2


def test_a_non_list_folders_is_refused_rather_than_silently_reset():
    """The store does not refuse this; it RESETS.

    ``_normalize_folders`` answers anything that is not a list/tuple with the
    bare defaults and no error, so a handler that passed a string through would
    wipe the operator's taxonomy, bump the revision and ack it as a success.
    Pinned by the surviving folder list, not by the error code alone.
    """

    _seed()
    reply = _update("s-str", {"workspace_id": WORKSPACE, "folders": "Ops,Design"})

    assert reply["error"]["code"] == -32602
    assert reply["error"]["data"]["reason"] == "folders_invalid"
    assert list(_surface().folders) == [*DEFAULTS, "Design"]
    assert _surface().revision == 2


def test_a_non_string_folder_entry_is_refused_rather_than_stringified():
    """``_safe_folder`` stringifies whatever it is handed.

    So an integer would be written as the folder ``"7"`` — a name the operator
    never typed, echoed back as though they had. One reason with the list check
    above, because the cure is the same: fix the payload, it is a launcher bug.
    """

    _seed()
    reply = _update("s-int", {"workspace_id": WORKSPACE, "folders": ["Ops", 7]})

    assert reply["error"]["code"] == -32602
    assert reply["error"]["data"]["reason"] == "folders_invalid"
    assert list(_surface().folders) == [*DEFAULTS, "Design"]


def test_a_missing_folders_key_is_refused_and_does_not_bump_the_revision():
    """``update_surface(folders=None)`` is a legal STORE call — it bumps the
    revision, emits ``change=saved`` and moves no folders.

    That is right for the CLI (``harness office set-folders`` with no value) and
    wrong for this lane: a client that forgot the parameter would spend a
    revision, invalidate its own guard token and change nothing.
    """

    _seed()
    reply = _update("s-none", {"workspace_id": WORKSPACE})

    assert reply["error"]["code"] == -32602
    assert reply["error"]["data"]["reason"] == "folders_invalid"
    assert _surface().revision == 2


def test_a_stale_expect_revision_is_refused_4090_with_no_revision_in_data():
    """The guard, and the number deliberately withheld.

    ``data`` carries no current revision on purpose: handing it back invites a
    retry with it, which is exactly the lost update the guard just refused. The
    number rides ``message``, where an operator can read it and a decoder will
    not.
    """

    _seed()
    reply = _update(
        "s-stale",
        {"workspace_id": WORKSPACE, "folders": ["Ops"], "expect_revision": 0},
    )

    assert reply["error"]["code"] == 4090
    data = reply["error"]["data"]
    assert data["reason"] == "stale_revision"
    assert "revision" not in data, data
    assert data["expect_revision"] == 0
    # The refusal wrote nothing.
    assert list(_surface().folders) == [*DEFAULTS, "Design"]
    assert _surface().revision == 2


def test_a_boolean_expect_revision_is_refused_rather_than_read_as_one():
    """``True`` is an ``int`` in Python and would silently mean revision 1.

    A wrong guard is worse than no guard: it refuses the writes it should pass
    and passes the one it should refuse.
    """

    _seed()
    reply = _update(
        "s-bool",
        {"workspace_id": WORKSPACE, "folders": ["Ops"], "expect_revision": True},
    )

    assert reply["error"]["code"] == -32602
    assert reply["error"]["data"]["reason"] == "expect_revision_invalid"
    assert _surface().revision == 2


def test_a_missing_workspace_id_spends_the_same_reason_every_office_verb_does():
    _seed()
    reply = _update("s-nows", {"folders": ["Ops"]})

    assert reply["error"]["code"] == -32602
    assert reply["error"]["data"]["reason"] == "workspace_id_required"
