"""The two read projections a paired install may ask for (S2b, R-IP9).

What is being tested is a BOUNDARY, not a query. A roster is not "the instance
table over the wire" — it is the answer to *who is addressable from this scope*,
and the scope rules belong to the install answering. A thread read is not "any
transcript by session id" — it is the tail of one lane the caller was already
handed a pointer to, behind the same guard the local read uses.

So the tests here mostly ask: can a far caller reach something it was not
given? The answers are no, and each no has its own word.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime.peer_directory import (
    PEER_ROSTER_CONTRACT,
    ROSTER_ROW_CAP,
    installs_hud_block,
    peer_roster_projection,
    read_chat_lane_tail,
    resolve_far_target_scope,
)


# ── the roster ───────────────────────────────────────────────────────────────


def test_roster_rows_carry_exactly_the_projection_fields_and_only_sendable_instances(
    tmp_path, monkeypatch
):
    """Every row is proven REACHABLE before it is offered — ``agent_chat_threads``'
    rule, and it matters more across a machine boundary: an agent that addresses
    an unreachable row loses a turn AND a network round trip finding out."""

    projection = peer_roster_projection(scope_workspace_id=None)

    assert projection["contract"] == PEER_ROSTER_CONTRACT
    assert projection["at"]
    assert projection["truncated"] is False
    assert projection["count"] == len(projection["rows"])
    for row in projection["rows"]:
        assert set(row) == {
            "handle",
            "persona_id",
            "label",
            "is_canonical_primary",
            "last_turn_at",
            "workspace_id",
        }
        # No transcript, no session id, no path, no credential: the projection is
        # a directory and nothing else.
        assert "session_id" not in row
        assert "messages" not in row


def test_the_roster_is_capped_and_says_truncated(monkeypatch):
    """Bounded because the caller is on another machine and the cost of a large
    answer is paid on a link this install does not own. ``truncated`` says so
    rather than the list silently ending — a consumer that could not tell the
    difference would render "6 agents" for a machine that has 200."""

    from agent_runtime import peer_directory

    class _Instance:
        def __init__(self, index: int) -> None:
            self.id = f"personainst_dev_agent_{index}"
            self.persona_id = "dev"
            self.display_name = f"dev {index}"
            self.workspace_id = None

    monkeypatch.setattr(
        peer_directory,
        "peer_roster_projection",
        peer_directory.peer_roster_projection,
    )
    monkeypatch.setattr(
        "agent_runtime.workspace_scope.addressable_roster",
        lambda instances, **kwargs: [_Instance(i) for i in range(ROSTER_ROW_CAP + 5)],
    )
    monkeypatch.setattr(
        "hermes_cli.harness._resolve_mission_chat_persona_id",
        lambda a, b: "dev",
    )

    projection = peer_roster_projection(scope_workspace_id="ws-1")

    assert projection["truncated"] is True
    assert projection["count"] == ROSTER_ROW_CAP


def test_the_scope_for_a_bare_target_is_the_active_workspace(monkeypatch):
    """Because that is what a bare ``@B/dev`` turn already resolves in: a peer
    turn carries no ``--workspace-id``, so ``sender_scope_workspace_id`` falls
    back to the active workspace. A roster scoped one way and a send resolved
    another would offer a teammate the very next message could not reach."""

    monkeypatch.setattr(
        "agent_runtime.store.WorkspaceStore.active_id", lambda self: "ws-active"
    )

    assert resolve_far_target_scope(None) == "ws-active"
    assert resolve_far_target_scope("dev") == "ws-active"


def test_the_scope_for_a_resident_handle_is_that_instances_own_workspace(monkeypatch):
    """An explicit handle is deliberate targeting, and the workspace that
    matters is the one the instance is placed in — not whichever the far
    operator happens to be looking at."""

    from agent_runtime import peer_directory

    class _Instance:
        id = "personainst_dev_agent_2"
        workspace_id = "ws-theirs"

    monkeypatch.setattr(
        "agent_runtime.store.WorkspaceStore.active_id", lambda self: "ws-active"
    )
    monkeypatch.setattr(
        "agent_runtime.persona_assignments.PersonaInstanceStore.list_all",
        lambda self: [_Instance()],
    )

    assert peer_directory.resolve_far_target_scope("personainst_dev_agent_2") == (
        "ws-theirs"
    )
    # A handle nothing here holds falls back to the active workspace rather than
    # answering ``None``: an unknown target is still going to be refused by the
    # reader, and a null scope would hide the whole roster instead.
    assert peer_directory.resolve_far_target_scope("personainst_nobody") == "ws-active"


# ── the thread ───────────────────────────────────────────────────────────────


def test_thread_read_refuses_an_unknown_target_with_unsupported_persona(monkeypatch):
    """The tool's own refusal ENVELOPE (a JSON string, because
    ``_resolve_chat_lane_target`` predates this function) is decoded here rather
    than changed there — so the local tool's bytes are unchanged and the peer
    door gets a dict with the same word in it."""

    from agent_runtime import peer_directory

    monkeypatch.setattr(
        "tools.agent_chat_tool._resolve_chat_lane_target",
        lambda persona_id, **kwargs: (
            None,
            json.dumps(
                {
                    "ok": False,
                    "error": "unsupported persona nobody_at_all",
                    "error_kind": "unsupported_persona",
                }
            ),
        ),
    )

    data = peer_directory.read_chat_lane_tail(
        "nobody_at_all", session_id="persona_chat_x_0123456789ab"
    )

    assert data["ok"] is False
    assert data["error_kind"] == "unsupported_persona"


def test_thread_read_applies_the_same_lane_guard_as_agent_chat_open(monkeypatch):
    """The guard is what stands between "review our thread" and "read any
    transcript on this machine". It runs in ONE function that both doors call,
    because a second copy for the far door would be a second place to widen it
    by accident."""

    from types import SimpleNamespace

    from agent_runtime import peer_directory

    monkeypatch.setattr(
        "tools.agent_chat_tool._resolve_chat_lane_target",
        lambda persona_id, **kwargs: (
            SimpleNamespace(
                persona="dev",
                instance_id="personainst_dev",
                handle="personainst_dev",
                default_session="persona_chat_personainst_dev_0123456789ab",
                store=None,
            ),
            None,
        ),
    )

    data = peer_directory.read_chat_lane_tail(
        "dev", session_id="persona_chat_somebody_else_0123456789ab"
    )

    assert data["ok"] is False
    assert data["error_kind"] == "foreign_session"


def test_thread_read_clamps_limit_to_forty(monkeypatch):
    """Exactly as the local tool clamps it — one rule, and the far door does not
    get a bigger window than the near one."""

    from types import SimpleNamespace

    from agent_runtime import peer_directory

    seen: list[int] = []
    monkeypatch.setattr(
        "tools.agent_chat_tool._resolve_chat_lane_target",
        lambda persona_id, **kwargs: (
            SimpleNamespace(
                persona="dev",
                instance_id="personainst_dev",
                handle="personainst_dev",
                default_session="persona_chat_personainst_dev_0123456789ab",
                store=None,
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        "agent_runtime.persona_chat_history.persona_chat_session_messages",
        lambda *, session_id, limit: (seen.append(limit) or {"ok": True, "messages": []}),
    )

    peer_directory.read_chat_lane_tail("dev", limit=9999)
    peer_directory.read_chat_lane_tail("dev", limit=0)
    peer_directory.read_chat_lane_tail("dev", limit="nonsense")

    assert seen == [40, 20, 20]


def test_thread_read_surfaces_a_scope_refusal_and_never_sets_the_env(monkeypatch):
    """§0.10's fact 1: a transcript read inside the serve process is new, and
    ``resolve_chat_session_scope``'s ambient rung is env-gated. This door FAILS
    CLOSED with the reader's own word rather than setting that variable — a peer
    door that widened its own scope would hold more than the local tool does.

    And it never answers with an empty page: ``count: 0`` over an unread
    transcript is the single most misleading result available here.
    """

    import os
    from types import SimpleNamespace

    from agent_runtime import peer_directory

    monkeypatch.setattr(
        "tools.agent_chat_tool._resolve_chat_lane_target",
        lambda persona_id, **kwargs: (
            SimpleNamespace(
                persona="dev",
                instance_id="personainst_dev",
                handle="personainst_dev",
                default_session="persona_chat_personainst_dev_0123456789ab",
                store=None,
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        "agent_runtime.persona_chat_history.persona_chat_session_messages",
        lambda *, session_id, limit: {
            "ok": False,
            "error_kind": "chat_scope_mismatch",
            "error": "this head does not own that session",
        },
    )
    monkeypatch.delenv("HERMES_ALLOW_AMBIENT_CHAT_READS", raising=False)

    data = peer_directory.read_chat_lane_tail("dev")

    assert data["ok"] is False
    assert data["error_kind"] == "chat_scope_mismatch"
    assert "NOT an empty thread" in data["error"]
    assert "HERMES_ALLOW_AMBIENT_CHAT_READS" not in os.environ


def test_a_teammate_with_no_thread_answers_honestly_and_mints_nothing(monkeypatch):
    from types import SimpleNamespace

    from agent_runtime import peer_directory

    monkeypatch.setattr(
        "tools.agent_chat_tool._resolve_chat_lane_target",
        lambda persona_id, **kwargs: (
            SimpleNamespace(
                persona="dev",
                instance_id="personainst_dev",
                handle="personainst_dev",
                default_session=None,
                store=None,
            ),
            None,
        ),
    )

    data = peer_directory.read_chat_lane_tail("dev")

    assert data == {
        "ok": True,
        "target_persona": "dev",
        "handle": "personainst_dev",
        "session_id": None,
        "has_thread": False,
        "count": 0,
        "messages": [],
    }


# ── the handlers ─────────────────────────────────────────────────────────────


def _call(method: str, params: dict, *, caller_peer="inst_far"):
    from agent_runtime import serve_rpc
    from agent_runtime.call_authorization import RpcCaller

    caller = (
        None
        if caller_peer is None
        else RpcCaller(kind="peer", peer_install_id=caller_peer)
    )
    return serve_rpc._METHODS[method](
        "r-1", params, serve_rpc.RpcContext(caller=caller)
    )


def test_both_verbs_refuse_a_non_peer_with_distinct_codes_from_a_bad_read():
    """``peer_identity_required`` is the verb saying it has no provenance to run
    under; ``thread_unreadable`` is this install's storage answering. A client
    that could not tell them apart would retry the first forever."""

    from agent_runtime.serve_rpc import PEER_CHAT_NOT_A_PEER_REASON

    for method, params in (
        ("peer.roster.list", {}),
        ("peer.thread.read", {"target": "dev", "session_id": "s"}),
    ):
        reply = _call(method, params, caller_peer=None)
        assert reply["error"]["data"]["reason"] == PEER_CHAT_NOT_A_PEER_REASON


def test_thread_read_requires_both_the_target_and_the_session():
    """``target`` is the security decision in that handler: with only a session
    id this would be a transcript reader — a caller could spend any session id
    it ever saw against any thread on this machine."""

    for params in (
        {"session_id": "persona_chat_x_0123456789ab"},
        {"target": "dev"},
        {"target": "dev", "session_id": ""},
        {"target": "x" * 400, "session_id": "s"},
    ):
        reply = _call("peer.thread.read", params)
        assert "error" in reply, params
        assert reply["error"]["code"] == -32602


def test_a_far_read_that_fails_is_thread_unreadable_and_carries_the_readers_word(
    monkeypatch,
):
    from agent_runtime import peer_directory, serve_rpc

    monkeypatch.setattr(
        peer_directory,
        "read_chat_lane_tail",
        lambda *a, **k: {
            "ok": False,
            "error_kind": "chat_scope_unresolved",
            "error": "the head could not resolve that session",
        },
    )

    reply = _call(
        "peer.thread.read", {"target": "dev", "session_id": "persona_chat_x_0123456789ab"}
    )

    assert reply["error"]["data"]["reason"] == serve_rpc.PEER_THREAD_UNREADABLE_REASON
    assert reply["error"]["data"]["error_kind"] == "chat_scope_unresolved"


def test_the_roster_reply_echoes_the_caller_and_the_correlation(monkeypatch):
    reply = _call("peer.roster.list", {"correlation_id": "g-1"})

    assert reply["result"]["peer"] == "inst_far"
    assert reply["result"]["correlation_id"] == "g-1"
    assert reply["result"]["contract"] == PEER_ROSTER_CONTRACT


# ── the HUD block ────────────────────────────────────────────────────────────


def test_installs_hud_block_reads_two_files_and_dials_nothing(tmp_path, monkeypatch):
    """A prompt block whose assembly depended on a machine that might be asleep
    would make every turn's opening as slow as the slowest peer."""

    from agent_runtime import serve_socket
    from agent_runtime.gateway_peers import (
        apply_peer_announce,
        cache_peer_roster,
        record_peer,
    )

    record_peer(
        tmp_path,
        peer_install_id="inst_mac",
        secret="a" * 64,
        display_name="mac",
        endpoints=[{"host": "10.0.0.4", "port": 9000}],
    )
    cache_peer_roster(
        tmp_path,
        "inst_mac",
        workspace_id="ws-1",
        rows=[{"handle": "personainst_dev", "persona_id": "dev"}],
    )
    apply_peer_announce(tmp_path, "inst_mac", {"display_name": "the mac"})

    monkeypatch.setattr(
        serve_socket,
        "ServeSocketClient",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("the HUD dialled")),
    )

    rows = installs_hud_block(tmp_path)

    assert rows == [
        {
            "ref": "mac",
            "install_id": "inst_mac",
            # The ANNOUNCED name wins over the pairing-time one: the install is
            # the authority for what it calls itself.
            "display_name": "the mac",
            "reachability": "unknown",
            "roster_fetched_at": rows[0]["roster_fetched_at"],
            "roster": [{"handle": "personainst_dev", "persona_id": "dev"}],
        }
    ]


def test_the_block_is_empty_when_no_peer_is_usable(tmp_path):
    from agent_runtime.gateway_peers import record_peer, revoke_peer

    assert installs_hud_block(tmp_path) == []

    record_peer(tmp_path, peer_install_id="inst_gone", secret="a" * 64, display_name="x")
    revoke_peer(tmp_path, "inst_gone")

    assert installs_hud_block(tmp_path) == []


def test_an_unreadable_store_answers_with_no_rows_rather_than_raising(monkeypatch):
    monkeypatch.setattr(
        "agent_runtime.gateway_peers.usable_peers",
        lambda root: (_ for _ in ()).throw(OSError("gone")),
    )

    assert installs_hud_block("/nowhere") == []
