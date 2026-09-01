"""Stage 13 write-path intent integrity tests.

The Mission Control bridge can deliver a scope mutation twice (serve timeout →
the caller re-runs the same argv through the one-shot CLI) or late (a wedged
serve child drains an abandoned request minutes afterward). Arrival order is
therefore meaningless for active-pointer writes — the intent's ``--issued-at``
basis decides ownership: a pointer owned by a strictly newer intent rejects a
late lander as ``superseded``, and an exact duplicate applies once with one
event. Diagnosed live 2026-07-09 (realm/workspace scope ping-pong minutes
after Stage 12 landed — the read path finally rendered the write races
faithfully).
"""

import json
from types import SimpleNamespace

from agent_runtime.events import EventLog


def _event_count() -> int:
    import agent_runtime.paths as paths

    path = paths.events_path()
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def _args(**kwargs) -> SimpleNamespace:
    return SimpleNamespace(json=True, **kwargs)


def test_stale_realm_intent_is_superseded_without_write_or_event():
    from agent_runtime.store import RealmStore

    store = RealmStore()
    realm_a = store.create(name="Realm A")
    realm_b = store.create(name="Realm B")
    assert store.set_active(realm_a.id, issued_at="2026-07-09T12:00:10Z")["applied"] is True
    before = _event_count()

    outcome = store.set_active(realm_b.id, issued_at="2026-07-09T12:00:05Z")

    assert outcome == {
        "realm_id": realm_a.id,
        "applied": False,
        "reason": "superseded",
        "requested_realm_id": realm_b.id,
    }
    assert store.active_id() == realm_a.id
    assert _event_count() == before


def test_duplicate_workspace_intent_applies_once_with_one_event():
    from agent_runtime.store import WorkspaceStore

    store = WorkspaceStore()
    workspace = store.create(name="Dup WS")
    assert store.set_active(workspace.id, issued_at="2026-07-09T12:00:10Z")["applied"] is True
    before = _event_count()

    outcome = store.set_active(workspace.id, issued_at="2026-07-09T12:00:10Z")

    assert outcome["applied"] is False
    assert outcome["reason"] == "duplicate"
    assert store.active_id() == workspace.id
    assert _event_count() == before


def test_newer_intent_applies_and_no_basis_caller_always_wins():
    from agent_runtime.store import RealmStore

    store = RealmStore()
    realm_a = store.create(name="Realm A")
    realm_b = store.create(name="Realm B")
    assert store.set_active(realm_a.id, issued_at="2026-07-09T12:00:10Z")["applied"] is True
    assert store.set_active(realm_b.id, issued_at="2026-07-09T12:00:11Z")["applied"] is True
    # A caller with no basis (human at a terminal, legacy code) is stamped
    # now(), so manual actions always win over any recorded intent.
    assert store.set_active(realm_a.id)["applied"] is True
    assert store.active_id() == realm_a.id


def test_unparseable_basis_fails_open():
    from agent_runtime.store import RealmStore

    store = RealmStore()
    realm_a = store.create(name="Realm A")
    realm_b = store.create(name="Realm B")
    assert store.set_active(realm_a.id, issued_at="2026-07-09T12:00:10Z")["applied"] is True
    # A malformed basis must never wedge scope switching; it just loses
    # supersede protection for that one write.
    assert store.set_active(realm_b.id, issued_at="not-a-timestamp")["applied"] is True
    assert store.active_id() == realm_b.id


def test_superseded_events_never_reach_the_stream():
    """No write means no event: the launcher must not even see a flicker from
    a rejected late lander (the whole point — the 2026-07-09 flip WAS the
    event feed faithfully rendering both writers)."""
    from agent_runtime.store import RealmStore

    store = RealmStore()
    realm_a = store.create(name="Realm A")
    realm_b = store.create(name="Realm B")
    store.set_active(realm_a.id, issued_at="2026-07-09T12:00:10Z")
    store.set_active(realm_b.id, issued_at="2026-07-09T12:00:01Z")

    activations = [event for event in EventLog().tail(10) if event.type == "realm.activated"]
    assert [event.payload["realm_id"] for event in activations] == [realm_a.id]


# ── CLI verb integration: the live 2026-07-09 flip scenario ─────────────────


def test_realm_use_late_lander_cannot_clobber_newer_selection(capsys):
    """User switches to realm B (19:52:53 live); their EARLIER click on realm
    A replays from the transport (19:53:23 live — serve timeout + CLI
    fallback). The realm pointer must stay on B and the late lander's
    reconcile must not move the workspace either."""
    from agent_runtime.store import RealmStore, WorkspaceStore
    from hermes_cli.harness import _cmd_realm_use

    realm_a = RealmStore().create(name="Realm A")
    realm_b = RealmStore().create(name="Realm B")
    WorkspaceStore().create(name="WS A", realm_id=realm_a.id)
    ws_b = WorkspaceStore().create(name="WS B", realm_id=realm_b.id)

    assert _cmd_realm_use(_args(realm_id=realm_b.id, issued_at="2026-07-09T19:52:53Z")) == 0
    capsys.readouterr()

    assert _cmd_realm_use(_args(realm_id=realm_a.id, issued_at="2026-07-09T19:52:43Z")) == 0
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["applied"] is False
    assert envelope["superseded"] is True
    assert envelope["id"] == realm_b.id
    assert envelope["requested_realm_id"] == realm_a.id
    assert RealmStore().active_id() == realm_b.id
    assert WorkspaceStore().active_id() == ws_b.id


def test_a_late_realm_use_loses_the_whole_scope_to_a_newer_workspace_choice(capsys):
    """A realm switch issued BEFORE a subsequent explicit workspace choice loses
    to it — and loses the realm pointer too, not only the workspace one.

    This test used to assert the other half ("each pointer is owned by the
    newest intent that touched IT"), and what it asserted was a STRADDLE: the
    realm pointer in A while the workspace pointer sat in a workspace of B, both
    verbs having answered success, and nothing on the tree able to heal it. The
    2026-09-01 operator ruling replaced per-pointer ownership with **newest
    explicit gesture wins** — the two pointers never durably straddle realms, so
    the newer workspace gesture takes both. The invariant and its two arms are
    documented at ``agent_runtime/scope_activation.py``'s module docstring and
    tested in ``tests/agent_runtime/test_scope_straddle_invariant.py``.

    The reconcile-basis property this test was written for SURVIVES, and the
    workspace assertion still pins it: the late realm switch does not drag the
    workspace pointer with it. What changed is that it no longer keeps the realm
    pointer either — the workspace choice's own follow write moved the realm
    pointer first, so the late switch is refused at the realm compare-and-set
    and never reaches its reconcile at all.
    """
    from agent_runtime.store import RealmStore, WorkspaceStore
    from hermes_cli.harness import _cmd_realm_use, _cmd_workspace_use

    realm_a = RealmStore().create(name="Realm A")
    realm_b = RealmStore().create(name="Realm B")
    WorkspaceStore().create(name="WS A", realm_id=realm_a.id)
    ws_b = WorkspaceStore().create(name="WS B", realm_id=realm_b.id)

    assert _cmd_workspace_use(_args(workspace_id=ws_b.id, issued_at="2026-07-09T12:00:30Z")) == 0
    capsys.readouterr()
    assert _cmd_realm_use(_args(realm_id=realm_a.id, issued_at="2026-07-09T12:00:20Z")) == 0
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["applied"] is False
    assert envelope["superseded"] is True
    assert envelope["requested_realm_id"] == realm_a.id
    assert RealmStore().active_id() == realm_b.id
    assert WorkspaceStore().active_id() == ws_b.id


def test_workspace_use_duplicate_reports_applied_false_duplicate(capsys):
    """The CLI fallback re-running the exact argv after a successful serve
    execution is the DESIGNED retry path — it must read as a clean no-op
    (reason: duplicate), never as an error and never as a second event."""
    from agent_runtime.store import WorkspaceStore
    from hermes_cli.harness import _cmd_workspace_use

    workspace = WorkspaceStore().create(name="Retry WS")
    assert _cmd_workspace_use(_args(workspace_id=workspace.id, issued_at="2026-07-09T12:00:10Z")) == 0
    capsys.readouterr()
    before = _event_count()

    assert _cmd_workspace_use(_args(workspace_id=workspace.id, issued_at="2026-07-09T12:00:10Z")) == 0
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["applied"] is False
    assert envelope["reason"] == "duplicate"
    assert envelope["superseded"] is False
    assert envelope["id"] == workspace.id
    assert _event_count() == before
