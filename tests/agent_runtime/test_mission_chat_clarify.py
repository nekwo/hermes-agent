from agent_runtime.mission_chat_clarify import MAX_CHOICES, MissionChatClarifyCapture


def test_capture_records_first_question_and_choices():
    cap = MissionChatClarifyCapture()
    assert not cap.requested
    assert cap.request is None

    sentinel = cap.callback("Which dev — launcher or backend?", ["launcher", "backend"])

    assert cap.requested
    assert cap.request == {
        "question": "Which dev — launcher or backend?",
        "choices": ["launcher", "backend"],
    }
    # Sentinel tells the model to end its turn with the question, and echoes the
    # offered options so the reply prose can present them.
    assert "end your turn" in sentinel.lower()
    assert "launcher" in sentinel and "backend" in sentinel


def test_capture_open_ended_question_has_no_choices():
    cap = MissionChatClarifyCapture()
    cap.callback("What repo is this in?")
    assert cap.request == {"question": "What repo is this in?"}


def test_capture_trims_to_max_choices_and_drops_blanks():
    cap = MissionChatClarifyCapture()
    cap.callback("Pick one", ["a", "   ", "b", "c", "d", "e"])
    assert cap.request["choices"] == ["a", "b", "c", "d"]
    assert len(cap.request["choices"]) == MAX_CHOICES


def test_capture_first_call_wins():
    # The model is told to stop after asking; if it asks twice anyway, the
    # question we already reported to the caller stays authoritative.
    cap = MissionChatClarifyCapture()
    cap.callback("first?")
    cap.callback("second?", ["x"])
    assert cap.request == {"question": "first?"}


def test_capture_empty_question_records_nothing():
    cap = MissionChatClarifyCapture()
    out = cap.callback("   ")
    assert not cap.requested
    assert cap.request is None
    assert "non-empty" in out


# ── the operator readout ────────────────────────────────────────────────────
#
# The clarify token's rollout step, per the design: watch echo adoption climb
# WITHOUT new event kinds (telemetry is not the EventLog here). Everything the
# readout reports is read back from state the binding already records — the
# per-turn `clarify_binding.bound_via` is mirrored onto the ticket at settle, so
# the ticket files alone answer it.


def _clarify_tickets(**kwargs):
    """Run the readout in-process and return its parsed envelope."""

    import json as _json
    from types import SimpleNamespace

    import hermes_cli.harness as harness

    args = SimpleNamespace(
        json=True, output=None, quiet=False, fields=None, sort=None,
        limit=None, cursor=None, session_id=None, state=None,
    )
    for key, value in kwargs.items():
        setattr(args, key, value)
    import io
    import contextlib

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert harness._cmd_mission_chat_clarify_tickets(args) == 0
    return _json.loads(buffer.getvalue())


def _seed_ticket(session_id="persona_chat_personainst_dev_abcdef123456", **overrides):
    from agent_runtime.persona_chat_continuity import PersonaChatClarifyTicketStore

    token = PersonaChatClarifyTicketStore().mint(
        chat_session_id=session_id,
        persona_instance_id="personainst_dev",
        persona_id="dev",
        asked_by_client_message_id="agent-relay-aaaaaaaaaaaa",
        **overrides,
    )
    assert token
    return token


def test_the_readout_counts_the_bound_via_ladder(isolate_agent_runtime_root):
    """``bound_via.none`` is the number that matters.

    It counts questions whose answer landed in a thread the caller neither named
    nor bound — the original defect, still happening. ``clarify_token`` climbing
    against it is the whole point of the feature, and ``unsettled`` is a question
    nobody has answered yet, which is evidence for neither."""

    from agent_runtime.persona_chat_continuity import PersonaChatClarifyTicketStore

    store = PersonaChatClarifyTicketStore()
    bound = _seed_ticket()
    complied = _seed_ticket()
    lost = _seed_ticket()
    _seed_ticket()  # never answered

    store.settle(bound, client_message_id="cm-1", bound_via="clarify_token")
    store.settle(complied, client_message_id="cm-2", bound_via="session_id")
    store.settle(lost, client_message_id="cm-3", bound_via="none")

    payload = _clarify_tickets()

    assert payload["ok"] is True
    assert payload["capability_id"] == "mission.chat.clarify_tickets"
    assert payload["total"] == 4
    assert payload["bound_via"] == {
        "clarify_token": 1,
        "session_id": 1,
        "none": 1,
        "unsettled": 1,
    }
    assert payload["states"] == {"answered": 3, "open": 1}
    # Every bucket is present even at zero, so a dashboard reading `none` never
    # has to tell "no occurrences" apart from "key not emitted".
    assert set(payload["bound_via"]) >= {"clarify_token", "session_id", "none", "unsettled"}


def test_the_readout_reports_age_and_session_binding_per_ticket(isolate_agent_runtime_root):
    token = _seed_ticket()

    row = _clarify_tickets()["items"][0]

    assert row["id"] == token
    assert row["state"] == "open"
    assert row["chat_session_id"] == "persona_chat_personainst_dev_abcdef123456"
    assert row["persona_instance_id"] == "personainst_dev"
    assert row["age_seconds"] >= 0
    assert row["expired"] is False


def test_expiry_is_reported_beside_state_never_as_one(isolate_agent_runtime_root):
    """TTL governs GC only — one rule, no cliff.

    An expired ticket is one the sweep MAY prune; it still binds until the file
    is actually gone. Folding that into ``state`` would claim a cliff the store
    does not have."""

    import json
    import time

    from agent_runtime.persona_chat_continuity import (
        CLARIFY_TICKET_TTL_SECONDS,
        PersonaChatClarifyTicketStore,
    )

    store = PersonaChatClarifyTicketStore()
    token = _seed_ticket()
    path = store._path(token)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["created_at"] = time.time() - (CLARIFY_TICKET_TTL_SECONDS * 2)
    path.write_text(json.dumps(record), encoding="utf-8")

    payload = _clarify_tickets()

    assert payload["expired"] == 1
    assert payload["states"] == {"open": 1}
    row = payload["items"][0]
    assert row["expired"] is True
    assert row["state"] == "open"


def test_filtering_the_listing_never_moves_the_counts(isolate_agent_runtime_root):
    # A filtered view is a lens on the population, not a redefinition of it. An
    # adoption ratio that moved because the operator asked to see fewer rows
    # would be a lying metric.
    _seed_ticket()
    _seed_ticket(session_id="persona_chat_personainst_qa_bbbbbbbbbbbb")

    payload = _clarify_tickets(session_id="persona_chat_personainst_qa_bbbbbbbbbbbb")

    assert payload["count"] == 1
    assert payload["items"][0]["chat_session_id"] == "persona_chat_personainst_qa_bbbbbbbbbbbb"
    assert payload["total"] == 2
    assert payload["bound_via"]["unsettled"] == 2


def test_the_readout_never_mutates_the_store(isolate_agent_runtime_root):
    """Read-only means no sweep either.

    A readout that pruned would silently change the population it reports on,
    and an operator checking adoption twice would get two different
    denominators."""

    import json
    import time

    from agent_runtime.persona_chat_continuity import (
        CLARIFY_TICKET_TTL_SECONDS,
        PersonaChatClarifyTicketStore,
    )

    store = PersonaChatClarifyTicketStore()
    token = _seed_ticket()
    path = store._path(token)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["created_at"] = time.time() - (CLARIFY_TICKET_TTL_SECONDS * 2)
    path.write_text(json.dumps(record), encoding="utf-8")

    assert _clarify_tickets()["total"] == 1
    assert _clarify_tickets()["total"] == 1
    assert store.resolve(token) is not None
    assert store.resolve(token)["state"] == "open"


def test_the_verb_is_reachable_and_read_only_from_the_cli(isolate_agent_runtime_root):
    # Registered with the NON-mutating stage42 args: it never mints, settles, or
    # sweeps, so it has no --dry-run to honor and nothing to confirm.
    import argparse

    from hermes_cli import harness

    parser = argparse.ArgumentParser()
    harness.build_parser(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["harness", "mission-chat", "clarify-tickets", "--json"])

    assert args.func is harness._cmd_mission_chat_clarify_tickets
    assert not hasattr(args, "dry_run")
    # …and the stage42 listing flags every read verb carries are there.
    assert hasattr(args, "limit") and hasattr(args, "sort") and hasattr(args, "fields")
