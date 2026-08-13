"""The per-conversation INSTANCE_RECORDED chat-scope rung, and the typed
refusals that retire the silent-empty class (2026-08-12 ambient chat-history
incident).

The incident: ``harness persona chat history --json`` returned
``ok: true, count: 0`` for a session whose rows were on disk, because the
ambient process resolved a DIFFERENT ``state.db`` than the one the
conversation lived in. Every probe that resolved its own root confirmed
health; the investigation lost a full operator day to two wrong root causes.

Two changes close the class:

1. **The chat head is recorded PER CONVERSATION** — ``PersonaInstance``
   stamps the head home its conversation was bound against (at creation,
   re-affirmed each turn via ``open_chat``), and
   ``resolve_chat_session_scope(session_id=...)`` consults that record above
   the shared-root pointer. The rung resolves AND verifies: a disagreeing
   AUTHORITY is a typed ``ChatScopeMismatch``, never a silent preference.
2. **The ambient rung stops being an answer for chat reads** —
   ``persona_chat_session_messages`` refuses (``chat_scope_unresolved``)
   instead of reading whichever ``state.db`` ambient resolution guessed.
"""

from __future__ import annotations

import pytest

from agent_runtime.chat_session_scope import (
    ChatHeadSource,
    InstanceChatHeadStatus,
    publish_chat_head_home,
    recorded_instance_chat_head,
    resolve_chat_session_scope,
    resolve_process_chat_scope,
)
from agent_runtime.persona_assignments import PersonaInstanceStore
from agent_runtime.persona_chat_history import (
    CHAT_SCOPE_MISMATCH,
    CHAT_SCOPE_UNRESOLVED,
    PERSONA_CHAT_SESSION_SOURCE,
    persona_chat_session_messages,
)

SESSION_ID = "persona_chat_personainst_qa_agent_deadbeef"


@pytest.fixture
def runtime_root(tmp_path, monkeypatch):
    """A shared runtime root with two profile homes, like the live layout."""

    root = tmp_path / ".hermes"
    store_root = root / "agent-runtime"
    head_home = root / "profiles" / "base"
    other_home = root / "profiles" / "alice"
    for path in (store_root, head_home, other_home):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(store_root))
    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)
    monkeypatch.delenv("HERMES_ALLOW_AMBIENT_CHAT_READS", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(head_home))
    return {
        "root": root,
        "store_root": store_root,
        "head_home": head_home,
        "other_home": other_home,
    }


def _serve_lane(monkeypatch, runtime_root) -> None:
    monkeypatch.setenv("HERMES_HOME", str(runtime_root["head_home"]))
    monkeypatch.setenv("HERMES_HEAD_HOME", str(runtime_root["head_home"]))


def _ambient_lane(monkeypatch, runtime_root, home_key: str = "other_home") -> None:
    monkeypatch.setenv("HERMES_HOME", str(runtime_root[home_key]))
    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)


class _CaptureLog:
    """Event capture double: real ``_event`` shape, no store-root EventLog."""

    def __init__(self) -> None:
        self.events: list = []

    def append(self, event) -> None:
        self.events.append(event)


def _bind(monkeypatch, runtime_root, *, lane=_serve_lane, session_id: str = SESSION_ID):
    """Bind a persona instance to *session_id* through the REAL chokepoint."""

    lane(monkeypatch, runtime_root)
    log = _CaptureLog()
    store = PersonaInstanceStore(event_log=log)
    instance = store.open_chat(persona_id="qa", session_id=session_id)
    return store, instance, log


# ── stamping (item 1: at creation, re-affirmed each turn) ────────────────────


def test_open_chat_stamps_the_authoritative_head_on_the_instance(
    monkeypatch, runtime_root
):
    _, instance, log = _bind(monkeypatch, runtime_root)

    assert instance.chat_head_home == str(runtime_root["head_home"])
    opened = [event for event in log.events if event.type == "persona_instance.chat_opened"]
    assert opened and opened[-1].payload["chat_head_home"] == str(runtime_root["head_home"])


def test_open_chat_under_an_ambient_scope_leaves_the_row_unrecorded(
    monkeypatch, runtime_root
):
    """A degraded guess must never be laundered into a recorded authority."""

    _, instance, _ = _bind(monkeypatch, runtime_root, lane=_ambient_lane)

    assert instance.chat_head_home is None
    assert (
        recorded_instance_chat_head(SESSION_ID).status
        is InstanceChatHeadStatus.UNRECORDED
    )


def test_reopening_with_the_same_head_stays_an_idempotent_no_op(
    monkeypatch, runtime_root
):
    """The send path re-enters ``open_chat`` every turn; an unchanged stamp must
    not rewrite the row or emit an event (fingerprint/stream-delta churn)."""

    store, first, log = _bind(monkeypatch, runtime_root)
    events_before = len(log.events)

    second = store.open_chat(persona_id="qa", session_id=SESSION_ID)

    assert second.chat_head_home == first.chat_head_home
    assert second.updated_at == first.updated_at
    assert len(log.events) == events_before


def test_a_rebind_under_a_different_authoritative_head_restamps_and_is_audited(
    monkeypatch, runtime_root
):
    """A re-stamp is deliberate and AUDITED, never silent: the event carries
    both the new and the previous head."""

    store, _, log = _bind(monkeypatch, runtime_root)

    monkeypatch.setenv("HERMES_HEAD_HOME", str(runtime_root["other_home"]))
    rebound = store.open_chat(persona_id="qa", session_id=SESSION_ID)

    assert rebound.chat_head_home == str(runtime_root["other_home"])
    opened = [event for event in log.events if event.type == "persona_instance.chat_opened"]
    assert opened[-1].payload["chat_head_home"] == str(runtime_root["other_home"])
    assert opened[-1].payload["previous_chat_head_home"] == str(
        runtime_root["head_home"]
    )


# ── the ladder with the new rung ─────────────────────────────────────────────


def test_the_instance_record_beats_the_ambient_guess(monkeypatch, runtime_root):
    """THE INCIDENT'S RESOLUTION SHAPE: no head named, no pointer published —
    the recorded conversation home still wins over the ambient guess."""

    _bind(monkeypatch, runtime_root)
    _ambient_lane(monkeypatch, runtime_root)

    scope = resolve_chat_session_scope(session_id=SESSION_ID)

    assert scope.source is ChatHeadSource.INSTANCE_RECORDED
    assert scope.head_home == runtime_root["head_home"]
    assert scope.authoritative
    assert not scope.explicitly_named
    assert scope.mismatch is None


def test_an_agreeing_pointer_verifies_rather_than_conflicts(
    monkeypatch, runtime_root
):
    _bind(monkeypatch, runtime_root)
    _serve_lane(monkeypatch, runtime_root)
    publish_chat_head_home()
    _ambient_lane(monkeypatch, runtime_root)

    scope = resolve_chat_session_scope(session_id=SESSION_ID)

    assert scope.source is ChatHeadSource.INSTANCE_RECORDED
    assert scope.mismatch is None


def test_a_disagreeing_pointer_is_a_typed_mismatch_not_a_silent_preference(
    monkeypatch, runtime_root
):
    _bind(monkeypatch, runtime_root)
    # Publish a pointer naming a DIFFERENT head than the instance recorded.
    monkeypatch.setenv("HERMES_HEAD_HOME", str(runtime_root["other_home"]))
    publish_chat_head_home()
    _ambient_lane(monkeypatch, runtime_root)

    scope = resolve_chat_session_scope(session_id=SESSION_ID)

    assert scope.mismatch is not None
    assert scope.mismatch.recorded_head == runtime_root["head_home"]
    assert scope.mismatch.resolved_head == runtime_root["other_home"]
    assert scope.mismatch.resolved_source is ChatHeadSource.SHARED_ROOT_POINTER


def test_a_config_declaration_disagreeing_with_the_record_is_a_typed_mismatch(
    monkeypatch, runtime_root
):
    """The CONFIG_DECLARED rung is verified exactly like the pointer.

    It is a recorded machine fact, not a guess, so a recorded instance head
    disagreeing with it is two AUTHORITIES contradicting each other — the same
    finding the pointer produces, never the silent preference that lets a read
    answer from the wrong store.
    """

    _bind(monkeypatch, runtime_root)
    # No pointer at all: the declaration is the recorded rung that answers.
    (runtime_root["other_home"] / "config.yaml").write_bytes(
        f"agent_runtime:\n  head_home: '{runtime_root['other_home']}'\n".encode("utf-8")
    )
    monkeypatch.delenv("HERMES_AGENT_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(runtime_root["other_home"]))

    scope = resolve_chat_session_scope(session_id=SESSION_ID)

    # The record still WINS resolution — it is the per-conversation fact.
    assert scope.source is ChatHeadSource.INSTANCE_RECORDED
    assert scope.head_home == runtime_root["head_home"]
    assert scope.mismatch is not None
    assert scope.mismatch.recorded_head == runtime_root["head_home"]
    assert scope.mismatch.resolved_head == runtime_root["other_home"]
    assert scope.mismatch.resolved_source is ChatHeadSource.CONFIG_DECLARED


def test_an_explicit_head_disagreeing_with_the_record_is_a_typed_mismatch(
    monkeypatch, runtime_root
):
    """Env/relay still outrank the record for RESOLUTION — but the disagreement
    is surfaced, because one of the two authorities is wrong."""

    _bind(monkeypatch, runtime_root)
    monkeypatch.setenv("HERMES_HEAD_HOME", str(runtime_root["other_home"]))

    scope = resolve_chat_session_scope(session_id=SESSION_ID)

    assert scope.source is ChatHeadSource.ENV_HEAD_HOME
    assert scope.head_home == runtime_root["other_home"]
    assert scope.mismatch is not None
    assert scope.mismatch.recorded_head == runtime_root["head_home"]
    assert scope.mismatch.resolved_source is ChatHeadSource.ENV_HEAD_HOME


def test_an_explicit_head_agreeing_with_the_record_carries_no_mismatch(
    monkeypatch, runtime_root
):
    _bind(monkeypatch, runtime_root)
    _serve_lane(monkeypatch, runtime_root)

    scope = resolve_chat_session_scope(session_id=SESSION_ID)

    assert scope.source is ChatHeadSource.ENV_HEAD_HOME
    assert scope.mismatch is None
    assert scope.instance_head is not None and scope.instance_head.recorded


def test_absence_is_not_a_mismatch_an_unrecorded_row_falls_through(
    monkeypatch, runtime_root
):
    """An instance predating the stamp must resolve exactly as before the rung
    existed: pointer, then ambient. Modeled explicitly as UNRECORDED."""

    _bind(monkeypatch, runtime_root, lane=_ambient_lane)  # binds WITHOUT a stamp
    _serve_lane(monkeypatch, runtime_root)
    publish_chat_head_home()
    _ambient_lane(monkeypatch, runtime_root)

    scope = resolve_chat_session_scope(session_id=SESSION_ID)

    assert scope.source is ChatHeadSource.SHARED_ROOT_POINTER
    assert scope.mismatch is None
    assert scope.instance_head is not None
    assert scope.instance_head.status is InstanceChatHeadStatus.UNRECORDED


def test_a_session_bound_to_no_instance_falls_through(monkeypatch, runtime_root):
    _ambient_lane(monkeypatch, runtime_root)

    scope = resolve_chat_session_scope(session_id="persona_chat_nobody")

    assert scope.source is ChatHeadSource.AMBIENT_HOME
    assert scope.instance_head is not None
    assert scope.instance_head.status is InstanceChatHeadStatus.NO_INSTANCE


def test_a_recorded_home_that_vanished_falls_through_under_its_own_name(
    monkeypatch, runtime_root
):
    """The stale-pointer posture, mirrored: a recorded head whose directory is
    gone must not strand the conversation — and must not fall through as if no
    record had ever existed."""

    import shutil

    _bind(monkeypatch, runtime_root)
    shutil.rmtree(runtime_root["head_home"])
    _ambient_lane(monkeypatch, runtime_root)

    scope = resolve_chat_session_scope(session_id=SESSION_ID)

    assert scope.source is ChatHeadSource.AMBIENT_HOME
    assert scope.instance_head is not None
    assert (
        scope.instance_head.status is InstanceChatHeadStatus.RECORDED_HOME_MISSING
    )


def test_the_process_lane_never_consults_the_instance_rung(
    monkeypatch, runtime_root
):
    """A caller with no conversation in hand resolves the process ladder only:
    no lookup, no mismatch, no instance block — even with a record present.

    This is now spelled by NAME (``resolve_process_chat_scope``) rather than by
    omitting an argument, so the session-less lane is a decision a reader can
    grep for instead of an oversight nobody can distinguish from one."""

    _bind(monkeypatch, runtime_root)
    _ambient_lane(monkeypatch, runtime_root)

    scope = resolve_process_chat_scope()

    assert scope.source is ChatHeadSource.AMBIENT_HOME
    assert scope.instance_head is None
    assert scope.mismatch is None
    assert "instance_head" not in scope.payload()


# ── the read refusals (item 2) ───────────────────────────────────────────────


def _mint_transcript(runtime_root, home_key: str = "head_home") -> None:
    from hermes_state import SessionDB

    db = SessionDB(db_path=runtime_root[home_key] / "state.db")
    db.ensure_session(SESSION_ID, source=PERSONA_CHAT_SESSION_SOURCE)
    db.append_message(SESSION_ID, "user", "where is my background task update?")
    db.append_message(SESSION_ID, "assistant", "it completed; here is the summary")


def test_the_incident_is_unrepresentable_an_ambient_read_finds_the_recorded_store(
    monkeypatch, runtime_root
):
    """THE ACCEPTANCE TEST. The 2026-08-12 shape verbatim: the transcript and
    the stamp live in the head profile; a process that names NO head and has
    NO pointer reads the conversation — and gets the messages instead of the
    well-formed empty page that cost the day."""

    _bind(monkeypatch, runtime_root)
    _mint_transcript(runtime_root)
    _ambient_lane(monkeypatch, runtime_root)

    data = persona_chat_session_messages(session_id=SESSION_ID, limit=40)

    assert data["ok"] is True
    assert data["total_count"] >= 2
    assert data["chat_scope"]["source"] == "instance_recorded"
    assert any(
        "background task" in str(message.get("text") or "")
        for message in data["messages"]
    )


def test_an_ambient_chat_read_refuses_with_a_typed_reason(
    monkeypatch, runtime_root
):
    """Reaching ambient on a chat read means "I do not know where to look" —
    the envelope must say so, not render as "no messages"."""

    _ambient_lane(monkeypatch, runtime_root)
    scope = resolve_chat_session_scope(session_id="persona_chat_nobody")
    assert scope.source is ChatHeadSource.AMBIENT_HOME  # probe, not assumption

    data = persona_chat_session_messages(session_id="persona_chat_nobody", limit=40)

    assert data["ok"] is False
    assert data["error_kind"] == CHAT_SCOPE_UNRESOLVED
    assert data["chat_scope"]["source"] == "ambient_home"
    assert "count" not in data and "messages" not in data
    # The remedy is named in the refusal, not left to archaeology.
    assert "HERMES_HEAD_HOME" in data["error"]


def test_the_ambient_refusal_has_an_explicit_opt_out_for_single_root_setups(
    monkeypatch, runtime_root
):
    _mint_transcript(runtime_root, home_key="other_home")
    _ambient_lane(monkeypatch, runtime_root)
    monkeypatch.setenv("HERMES_ALLOW_AMBIENT_CHAT_READS", "1")

    data = persona_chat_session_messages(session_id=SESSION_ID, limit=40)

    assert data["ok"] is True
    assert data["total_count"] >= 2
    assert data["chat_scope"]["source"] == "ambient_home"


def test_a_scope_mismatch_refuses_the_read_instead_of_picking_a_side(
    monkeypatch, runtime_root
):
    """A read that would serve data from the wrong store is the silent-empty
    class again with extra steps; two disagreeing authorities refuse, naming
    both heads."""

    _bind(monkeypatch, runtime_root)
    _mint_transcript(runtime_root)
    monkeypatch.setenv("HERMES_HEAD_HOME", str(runtime_root["other_home"]))

    data = persona_chat_session_messages(session_id=SESSION_ID, limit=40)

    assert data["ok"] is False
    assert data["error_kind"] == CHAT_SCOPE_MISMATCH
    mismatch = data["chat_scope"]["mismatch"]
    assert mismatch["recorded_head"] == str(runtime_root["head_home"])
    assert mismatch["resolved_head"] == str(runtime_root["other_home"])
    assert "count" not in data and "messages" not in data


def test_a_caller_supplied_session_db_is_never_second_guessed(
    monkeypatch, runtime_root
):
    """Passing ``session_db`` means the caller owns the acquisition: no scope
    resolution, no refusal, no ``chat_scope`` claim about provenance."""

    _ambient_lane(monkeypatch, runtime_root)

    class FakeSessionDB:
        def get_messages(self, session_id, include_inactive=False):
            return [{"id": "m1", "role": "operator", "content": "hello"}]

    data = persona_chat_session_messages(
        session_id=SESSION_ID, limit=40, session_db=FakeSessionDB()
    )

    assert data["ok"] is True
    assert data["count"] == 1
    assert "chat_scope" not in data


def test_a_healthy_explicit_read_now_states_its_frame_of_reference(
    monkeypatch, runtime_root
):
    """The success envelope carries the ACTUAL per-conversation scope, so an
    empty page can never again hide which state.db answered."""

    _bind(monkeypatch, runtime_root)
    _mint_transcript(runtime_root)
    _serve_lane(monkeypatch, runtime_root)

    data = persona_chat_session_messages(session_id=SESSION_ID, limit=40)

    assert data["ok"] is True
    assert data["chat_scope"]["source"] == "env_head_home"
    assert data["chat_scope"]["authoritative"] is True


def test_the_per_conversation_lane_requires_a_session_id() -> None:
    """Omitting the conversation is a TypeError, not a silent process-lane read.

    The default used to be ``session_id=None``, which made the WRONG answer the
    one you got by typing less: a caller holding a session id could drop it and
    resolve some other frame of reference. Because a wrong root answers
    ``ok: true, count: 0`` rather than failing, nothing above ever noticed. The
    signature now refuses, so the mistake cannot survive to runtime.
    """

    with pytest.raises(TypeError):
        resolve_chat_session_scope()  # type: ignore[call-arg]


def test_a_blank_session_id_never_adopts_another_conversations_head(
    monkeypatch, runtime_root
):
    """A blank id is the same omission wearing a different hat.

    It resolves — this function never raises — but on the PROCESS ladder, with
    the instance rung skipped. Falling through to the rung with an empty id
    would be strictly worse than the old default: it would let whatever the
    lookup happened to return stand in for a conversation nobody named.
    """

    _bind(monkeypatch, runtime_root)
    _ambient_lane(monkeypatch, runtime_root)

    for blank in ("", "   "):
        scope = resolve_chat_session_scope(session_id=blank)
        assert scope.source is ChatHeadSource.AMBIENT_HOME
        assert scope.instance_head is None, (
            "a blank id must skip the instance rung entirely, not consult it"
        )
        assert scope.mismatch is None
