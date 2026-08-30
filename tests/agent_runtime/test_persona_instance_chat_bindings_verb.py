"""H4 — `harness persona-instance chat-bindings` is read-only by construction.

Plan: ``docs/agent-runtime-harness/archive/realm-pull-live-projection.md``.

The verb answers the ONE question behind Mission Control's amber
``projection drops N`` chip when the reason code is ``session_not_in_db``:
which persona instance points at a chat session SessionDB no longer holds.
Before it existed the answers were a Python join over a saved snapshot, or
``persona-instance reconcile --dry-run`` — a verb that DEFAULTS TO APPLY and
runs five phases that archive rows, prune flow graphs and append events the
moment the flag is forgotten.

So the pin here is not "it prints the right line". It is that the verb HAS NO
WRITE MODE: no ``--apply``, no ``--dry-run``, and a run over a store with a
stale binding leaves every row byte-identical and the event log the same length.
"""

from __future__ import annotations

import argparse
import json

import pytest
from hermes_time import now

from agent_runtime import paths
from agent_runtime.events import EventLog
from agent_runtime.models import PersonaInstance
from agent_runtime.persona_assignments import PersonaInstanceStore
from agent_runtime.serde import to_jsonable
from agent_runtime.states import WorkerSessionState
from hermes_cli.harness import build_parser

pytestmark = pytest.mark.usefixtures("isolate_agent_runtime_root")


class _FakeSessionDB:
    """Knows a fixed set of live session ids — the same stand-in the store's own
    binding tests use."""

    def __init__(self, session_ids):
        self.session_ids = list(session_ids)

    def list_sessions_rich(self, **kwargs):
        limit = int(kwargs.get("limit") or 20)
        return [{"id": session_id} for session_id in self.session_ids][:limit]

    def get_session(self, session_id):
        return {"id": session_id} if session_id in self.session_ids else None


def _parser():
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    return parser


def _args(*extra):
    return _parser().parse_args(
        ["harness", "persona-instance", "chat-bindings", *extra]
    )


def _bind(instance_id: str, *, persona_id: str, session_id: str):
    """A chat-bound store row written verbatim, both pointers set — the shape
    ``repair_missing_chat_session_bindings``'s own tests seed."""

    instance = PersonaInstance(
        id=instance_id,
        persona_id=persona_id,
        role=persona_id,
        display_name=instance_id,
        profile_id=None,
        runtime_root=str(paths.store_root()),
        state=WorkerSessionState.IDLE,
        mode="chat",
        session_id=session_id,
        updated_at=now(),
    )
    instance.default_chat_session_id = session_id
    path = paths.persona_instance_path(instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(to_jsonable(instance), indent=2, sort_keys=True), encoding="utf-8"
    )


def _store_fingerprint() -> dict[str, tuple[int, int]]:
    """Every persona-instance row's size and mtime — the "mutated nothing" pin."""

    root = paths.persona_instances_dir()
    return {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.glob("*.json"))
    }


def _event_count() -> int:
    return len(EventLog().tail(2000))


@pytest.fixture()
def one_stale_two_healthy(monkeypatch):
    _bind("personainst_gone", persona_id="dev", session_id="persona_chat_gone")
    _bind("personainst_live_a", persona_id="backend_dev", session_id="persona_chat_a")
    _bind("personainst_live_b", persona_id="qa", session_id="persona_chat_b")
    # ``_session_presence_probe`` fails closed on two preconditions before it
    # will judge anything: THIS process must have named the head home, and the
    # database must positively enumerate. Both are satisfied here so the test
    # measures the verb, not the guard — the guard has its own test below.
    monkeypatch.setattr(
        "agent_runtime.persona_chat_history._default_session_db",
        lambda *a, **k: _FakeSessionDB(["persona_chat_a", "persona_chat_b"]),
    )
    monkeypatch.setattr(
        "agent_runtime.chat_session_scope.resolve_process_chat_scope",
        lambda *a, **k: type("_Scope", (), {"explicitly_named": True})(),
    )


def test_the_verb_names_exactly_the_stale_binding(one_stale_two_healthy, capsys):
    args = _args("--json")
    assert args.func(args) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["read_only"] is True
    assert data["answered"] is True
    assert data["stale_count"] == 1
    assert data["stale"][0]["persona_instance_id"] == "personainst_gone"
    assert data["stale"][0]["session_id"] == "persona_chat_gone"
    assert data["held"] == []


def test_the_verb_mutates_nothing(one_stale_two_healthy, capsys):
    """THE property. A row it reports as stale is a row it must not touch —
    including the mode demotion ``clear_chat_session_binding`` performs on the
    repair path, and including the event that write emits.

    Kill-mutation: call ``repair_missing_chat_session_bindings`` with the default
    ``apply=True``.
    """

    before_rows = _store_fingerprint()
    before_events = _event_count()

    args = _args("--json")
    assert args.func(args) == 0
    capsys.readouterr()

    assert _store_fingerprint() == before_rows
    assert _event_count() == before_events
    still_bound = PersonaInstanceStore().get("personainst_gone")
    assert still_bound.session_id == "persona_chat_gone"
    assert still_bound.default_chat_session_id == "persona_chat_gone"
    assert still_bound.mode == "chat"


def test_the_verb_has_no_write_mode_to_forget(one_stale_two_healthy):
    """``reconcile`` defaults to APPLY, which is why this verb exists beside it.
    A verb whose whole safety property is "it cannot write" must not grow the
    flag that would let it."""

    for flag in ("--apply", "--dry-run"):
        with pytest.raises(SystemExit):
            _args(flag)


def test_the_human_render_names_the_instance_and_the_repair(one_stale_two_healthy, capsys):
    args = _args()
    assert args.func(args) == 0

    out = capsys.readouterr().out
    assert "stale=1 held=0" in out
    assert "nothing was written" in out
    assert "- stale: personainst_gone -> missing session persona_chat_gone" in out
    assert "persona-instance reconcile" in out


def test_a_question_it_cannot_answer_is_not_reported_as_a_clean_store(monkeypatch, capsys):
    """The skip is the point. ``repair_missing_chat_session_bindings`` fails
    closed unless THIS process named the head home — a maintenance verb run under
    a profile home probes that profile's POPULATED database and reads every
    operator chat as absent (the live 2026-07-25 incident cleared 10 healthy
    bindings that way). A read-only view over a misrouted database would NAME ten
    innocent instances instead, which is the same lie one step earlier.

    So a skip must not exit 0 with an empty list, which is indistinguishable from
    a healthy store.

    Kill-mutation: return 0 unconditionally.
    """

    monkeypatch.setattr(
        "agent_runtime.persona_assignments.PersonaInstanceStore"
        ".repair_missing_chat_session_bindings",
        lambda self, **kwargs: {
            "applied": False,
            "dry_run": True,
            "skipped": "head_home_not_authoritative",
            "repaired": [],
            "repaired_count": 0,
            "held": [],
            "held_count": 0,
        },
    )

    args = _args("--json")
    assert args.func(args) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["answered"] is False
    assert data["skipped"] == "head_home_not_authoritative"

    plain = _args()
    assert plain.func(plain) == 1
    out = capsys.readouterr().out
    assert "NOT ANSWERED (head_home_not_authoritative)" in out
    assert "HERMES_HEAD_HOME" in out
