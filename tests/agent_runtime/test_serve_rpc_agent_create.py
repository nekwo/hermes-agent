"""``runtime.agent.create`` — one call places an agent (Plan A, AC-1).

What this suite has to prove is not "a dict comes back". It is the four
properties the two-call flow provably lacks, each asserted against a witness the
mutated path does NOT also write:

1. **Both rows are durable, or neither is.** Every failure test re-reads the
   persona-instance directory AND the office store. A typed error frame in front
   of a store that kept the roster row is R#37 with a nicer error code.
2. **A replay writes nothing.** The witness is the actor's REVISION, not the
   returned ids: a re-mint would return the same ids (they are derived from the
   same placement id) while bumping the revision to 2. Asserting the ids alone
   is the vacuity trap D3 hit.
3. **The events are the two-call flow's events.** Built as two real flows into
   the real event log and compared as a sequence. An extra emit here would
   change what every connected launcher folds.
4. **The honest default display name survives the lane change.** An omitted name
   must become "QA Agent" (the persona's configured name), never "Qa" (the store
   template's title-cased id). That string is what the launcher's conversational
   fold keys on, so getting it wrong folds a new placement onto a channel it
   does not belong to.

Nothing here spawns a ``harness serve`` child: the handler is called through
``serve_rpc.handle_request``, which is the same entry point both transports use.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime import serve_rpc
from tests.agent_runtime.office_seed import seed_workspace_record
from tests.agent_runtime.test_serve_rpc_office import (
    SHUTDOWN,
    _argv,
    _lines,
    _reply,
    _rpc,
    _run,
)

WORKSPACE = "ws_agent_create_test"


# ── helpers ──────────────────────────────────────────────────────────────────


def _call(params: dict, rid: str = "c1") -> dict:
    return serve_rpc.handle_request(
        {"jsonrpc": "2.0", "id": rid, "method": "runtime.agent.create", "params": params}
    )


def _seed_workspace(workspace_id: str = WORKSPACE):
    from agent_runtime.office_store import OfficeStore

    store = OfficeStore()
    seed_workspace_record(workspace_id)
    store.ensure_surface(workspace_id, created_by="seed")
    return store


def _params(**overrides) -> dict:
    params = {
        "persona_id": "qa",
        "workspace_id": WORKSPACE,
        "position": [3.5, -1.25],
        "idempotency_key": "gesture-1",
    }
    params.update(overrides)
    return params


def _instances() -> dict:
    from agent_runtime.persona_assignments import PersonaInstanceStore

    return {instance.id: instance for instance in PersonaInstanceStore().list_all()}


def _actors(workspace_id: str = WORKSPACE) -> dict:
    from agent_runtime.office_store import OfficeStore

    return {actor.actor_key: actor for actor in OfficeStore().list_actors(workspace_id)}


def _event_types_since(marker: int) -> list[str]:
    """Event types appended since ``marker`` events were in the log."""

    from agent_runtime.events import EventLog

    events = EventLog().tail(400)
    return [event.type for event in events[marker:]]


def _event_count() -> int:
    from agent_runtime.events import EventLog

    return len(EventLog().tail(400))


@pytest.fixture
def qa_persona():
    """A persona whose configured display name differs from its title-cased id.

    Load-bearing: "QA Agent" vs "Qa" is the ONLY way to tell the shared naming
    rule from the store template's fallback, and a persona named "Qa" would make
    the naming test pass either way.
    """

    from agent_runtime.models import AgentPersona
    from agent_runtime.store import AgentStore

    persona = AgentPersona(
        id="qa",
        display_name="QA Agent",
        role="qa",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
    )
    AgentStore().save(persona)
    return persona


@pytest.fixture
def dev_persona():
    """A SECOND roster persona, for the tests whose subject is not ``qa``."""

    from agent_runtime.models import AgentPersona
    from agent_runtime.store import AgentStore

    persona = AgentPersona(
        id="dev",
        display_name="Dev Agent",
        role="dev",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
    )
    AgentStore().save(persona)
    return persona


# ── the happy path ───────────────────────────────────────────────────────────


def test_create_returns_both_rows_and_a_phase_envelope(qa_persona):
    _seed_workspace()
    reply = _call(_params(placement_id="qa_agent_2"))

    result = reply["result"]
    assert result["persona_instance_id"] == "personainst_qa_agent_2"
    assert result["persona_id"] == "qa"
    assert result["placement_id"] == "qa_agent_2"
    assert result["workspace_id"] == WORKSPACE
    assert result["revision"] == 1
    assert result["idempotent_replay"] is False
    assert result["default_chat_session_id"].startswith(
        "persona_chat_personainst_qa_agent_2_"
    )
    assert set(result["phases"]) == {"instance_ms", "placement_ms", "total_ms"}

    # BOTH rows, read back off disk rather than off the reply.
    instances = _instances()
    assert "personainst_qa_agent_2" in instances
    assert instances["personainst_qa_agent_2"].workspace_id == WORKSPACE

    actors = _actors()
    assert result["actor_key"] in actors
    placed = actors[result["actor_key"]]
    assert placed.persona_instance_id == "personainst_qa_agent_2"
    assert [item.item_id for item in placed.items] == ["personainst_qa_agent_2"]
    assert [float(v) for v in placed.items[0].position] == [3.5, -1.25]


def test_the_method_is_advertised_on_the_manifest():
    # The launcher gates on the advertisement, never on its own release notes.
    assert "runtime.agent.create" in serve_rpc.manifest()["methods"]
    assert serve_rpc.manifest()["contract"] == 1


def test_the_method_answers_through_the_REAL_serve_loop_and_the_argv_lane_is_untouched(
    qa_persona,
):
    """Every other test here calls ``handle_request`` directly. That proves the
    handler and proves nothing about REACHABILITY: a method the dispatcher never
    routes to is a method no launcher can call.

    So this one runs the real ``serve_loop`` — the same entry point stdio and the
    socket both use — with an argv request beside the create, and compares the
    argv lane's emitted lines BYTE FOR BYTE against a run without it. "Additive"
    is a claim; this is the proof.
    """

    _seed_workspace()

    argv_only = _run([_argv("a1", ["harness", "--version"]), SHUTDOWN])
    out = _run(
        [
            _argv("a1", ["harness", "--version"]),
            _rpc(
                "create-1",
                "runtime.agent.create",
                {
                    "persona_id": "qa",
                    "workspace_id": WORKSPACE,
                    "position": [3.5, -1.25],
                    "idempotency_key": "serve-loop-1",
                    "placement_id": "qa_via_serve",
                },
            ),
            SHUTDOWN,
        ]
    )

    result = _reply(out, "create-1")["result"]
    assert result["persona_instance_id"] == "personainst_qa_via_serve"
    # The row is durable, not merely answered.
    assert "personainst_qa_via_serve" in _instances()
    assert result["actor_key"] in _actors()

    # The argv lane's own lines, byte for byte, with a create beside them.
    assert [
        line for line in _lines(out) if json.loads(line).get("id") == "a1"
    ] == [line for line in _lines(argv_only) if json.loads(line).get("id") == "a1"]


def test_an_unknown_method_name_is_still_unknown():
    """Guards the registration itself: a decorator typo would make every test
    above green against a method the wire cannot name."""

    out = _run([_rpc("nope", "runtime.agent.created", {}), SHUTDOWN])
    error = _reply(out, "nope")["error"]
    assert error["code"] == serve_rpc.ERR_METHOD_NOT_FOUND
    assert "runtime.agent.create" in error["data"]["methods"]


def test_an_explicit_display_name_wins(qa_persona):
    _seed_workspace()
    reply = _call(_params(placement_id="qa_two", display_name="QA Agent (2)"))
    assert reply["result"]["display_name"] == "QA Agent (2)"
    assert _instances()["personainst_qa_two"].display_name == "QA Agent (2)"


def test_an_omitted_display_name_falls_back_to_the_persona_not_the_template(
    qa_persona,
):
    """The rule with teeth. "Qa" here means the RPC lane dropped the CLI's policy
    layer and went straight to the store template."""

    _seed_workspace()
    reply = _call(_params(placement_id="qa_solo"))
    assert reply["result"]["display_name"] == "QA Agent"
    # Independent witness: the name the OFFICE item carries, which is what the
    # canvas paints — not just the roster row the reply echoes.
    actors = _actors()
    assert actors[reply["result"]["actor_key"]].items[0].display_name == "QA Agent"


# ── idempotency ──────────────────────────────────────────────────────────────


def test_replay_returns_the_same_reply_and_writes_nothing(qa_persona):
    _seed_workspace()
    first = _call(_params(placement_id="qa_replay"))["result"]

    before = _instances()["personainst_qa_replay"].updated_at
    events_before = _event_count()

    second = _call(_params(placement_id="qa_replay"), rid="c2")["result"]

    assert second["idempotent_replay"] is True
    assert second["persona_instance_id"] == first["persona_instance_id"]
    assert second["default_chat_session_id"] == first["default_chat_session_id"]
    # The witnesses that a re-mint could NOT have faked. The ids are derived
    # from the placement id, so a duplicating replay returns them unchanged; the
    # revision and the row timestamp are what actually move when a write lands.
    assert _actors()[first["actor_key"]].revision == 1
    assert _instances()["personainst_qa_replay"].updated_at == before
    assert _event_count() == events_before


def test_reusing_a_key_for_a_different_persona_is_refused(qa_persona, dev_persona):
    """UC-H2 note: ``dev`` is now SEEDED rather than assumed.

    The roster refusal runs before the reservation, so an unseeded ``dev``
    would refuse ``persona_not_found`` and this test would pass for the wrong
    reason — never reaching the scope check it exists to prove.
    """

    _seed_workspace()
    _call(_params(placement_id="qa_scope"))
    reply = _call(
        _params(persona_id="dev", placement_id="dev_scope"), rid="c2"
    )
    assert reply["error"]["data"]["reason"] == "idempotency_conflict"
    assert reply["error"]["code"] == serve_rpc.ERR_CONFLICT
    # Nothing was written for the second persona.
    assert "personainst_dev_scope" not in _instances()


def test_a_crash_after_the_mint_replays_into_the_placement(qa_persona):
    """The strongest anti-R#37 property: the seam between the two writes.

    Note the SCOPED monkeypatch: the suite's own hermetic-root fixture shares
    the function-scoped ``monkeypatch``, so calling ``undo()`` on it would put
    HERMES_HOME back and silently read a different store than the one just
    written.
    """

    from agent_runtime.office_store import OfficeStore

    _seed_workspace()

    def _boom(self, *args, **kwargs):
        raise KeyboardInterrupt("process died between the two writes")

    with pytest.MonkeyPatch.context() as crashed:
        crashed.setattr(OfficeStore, "upsert_actor", _boom)
        with pytest.raises(KeyboardInterrupt):
            _call(_params(placement_id="qa_resume"))

        # Half-state 1, exactly as a crash leaves it: roster row, no placement.
        assert "personainst_qa_resume" in _instances()
        assert _actors() == {}
        crashed_row = _instances()["personainst_qa_resume"]
        minted_at = crashed_row.updated_at
        minted_session = crashed_row.default_chat_session_id

    reply = _call(_params(placement_id="qa_resume"), rid="c2")["result"]

    assert reply["persona_instance_id"] == "personainst_qa_resume"
    assert reply["actor_key"] in _actors()
    # It RESUMED rather than re-minting. The witness is the CHAT ROOT, not the
    # instance id: the id is derived from the placement id and a re-mint returns
    # it unchanged, while ``add_instance`` mints a fresh random session id every
    # time — so a duplicate create is visible here and nowhere else in the reply.
    assert reply["default_chat_session_id"] == minted_session
    assert _instances()["personainst_qa_resume"].updated_at == minted_at
    # And exactly one instance exists for this persona placement.
    assert len([i for i in _instances() if i.startswith("personainst_qa_resume")]) == 1


# ── compensation ─────────────────────────────────────────────────────────────


def _refuse_placement(monkeypatch, exc: Exception):
    from agent_runtime.office_store import OfficeStore

    def _refuse(self, *args, **kwargs):
        raise exc

    monkeypatch.setattr(OfficeStore, "upsert_actor", _refuse)


def test_a_refused_placement_retires_the_instance_and_says_so(qa_persona, monkeypatch):
    from agent_runtime.errors import SyncConflict

    _seed_workspace()
    _refuse_placement(monkeypatch, SyncConflict("unresolved realm sync sidecar"))

    reply = _call(_params(placement_id="qa_rollback"))
    data = reply["error"]["data"]

    assert reply["error"]["code"] == serve_rpc.ERR_CONFLICT
    assert data["reason"] == "placement_failed"
    assert data["phase"] == "placement"
    assert data["rolled_back"] is True

    # Independent witness for the rollback: the row is GONE from the live
    # directory and a retirement tombstone exists. `rolled_back: true` is the
    # handler's own claim about itself and proves nothing on its own.
    from agent_runtime import paths
    from agent_runtime.persona_assignments import PersonaInstanceStore

    assert "personainst_qa_rollback" not in _instances()
    assert not paths.persona_instance_path("personainst_qa_rollback").exists()
    assert (
        PersonaInstanceStore().retired_instance_archive_path("personainst_qa_rollback")
        is not None
    )
    assert _actors() == {}


def test_a_rolled_back_key_refuses_its_own_replay(qa_persona):
    """D-A3: the retirement tombstone BURNS the placement id, so this key can
    never complete. Answering the recorded refusal is the only honest option —
    a resume would have to invent an id the client never predicted."""

    from agent_runtime.errors import SyncConflict

    _seed_workspace()
    with pytest.MonkeyPatch.context() as refused:
        _refuse_placement(refused, SyncConflict("unresolved realm sync sidecar"))
        _call(_params(placement_id="qa_burned"))

    reply = _call(_params(placement_id="qa_burned"), rid="c2")
    data = reply["error"]["data"]
    assert data["reason"] == "placement_failed"
    assert data["idempotent_replay"] is True
    # And it really did not re-attempt: no roster row, no placement.
    assert "personainst_qa_burned" not in _instances()
    assert _actors() == {}


def test_a_compensation_that_itself_fails_is_reported_honestly(
    qa_persona, monkeypatch
):
    """The §6 worst case. It must NEVER report ``rolled_back: true`` — a claimed
    rollback that did not happen is R#37 with a lie on top."""

    from agent_runtime.errors import SyncConflict
    from agent_runtime.persona_assignments import PersonaInstanceStore

    _seed_workspace()
    _refuse_placement(monkeypatch, SyncConflict("unresolved realm sync sidecar"))

    def _retire_refuses(self, *args, **kwargs):
        raise RuntimeError("archive directory is read-only")

    monkeypatch.setattr(PersonaInstanceStore, "retire", _retire_refuses)

    reply = _call(_params(placement_id="qa_orphan"))
    data = reply["error"]["data"]

    assert data["rolled_back"] is False
    assert data["persona_instance_id"] == "personainst_qa_orphan"
    assert "read-only" in data["rollback_error"]
    # The orphan is real and is NAMED on disk, which is the whole point.
    assert "personainst_qa_orphan" in _instances()


def test_an_unexpected_store_fault_still_compensates(qa_persona, monkeypatch):
    """``handle_request``'s boundary would turn a raising store into a -32000
    with the roster row stranded. That is the half-state, typed."""

    _seed_workspace()
    _refuse_placement(monkeypatch, OSError("disk went away"))

    reply = _call(_params(placement_id="qa_fault"))
    assert reply["error"]["code"] == serve_rpc.ERR_HANDLER_FAILED
    assert reply["error"]["data"]["rolled_back"] is True
    assert "personainst_qa_fault" not in _instances()


# ── refusals that must write NOTHING ─────────────────────────────────────────


def test_an_unknown_workspace_is_refused_before_any_write(qa_persona):
    # Deliberately NOT seeded.
    reply = _call(_params(workspace_id="ws_never_authored", placement_id="qa_nows"))
    assert reply["error"]["code"] == serve_rpc.ERR_NOT_FOUND
    assert reply["error"]["data"]["reason"] == "workspace_not_found"
    assert "personainst_qa_nows" not in _instances()
    # And it left NO reservation: a client fixing a typo must not be answered
    # with its own stale error under the same key.
    _seed_workspace("ws_never_authored")
    retry = _call(
        _params(workspace_id="ws_never_authored", placement_id="qa_nows"), rid="c2"
    )
    assert retry["result"]["persona_instance_id"] == "personainst_qa_nows"


@pytest.mark.parametrize(
    "params,reason",
    [
        ({"persona_id": ""}, "persona_id_required"),
        ({"persona_id": "!!!"}, "persona_id_required"),
        ({"workspace_id": ""}, "workspace_id_required"),
        ({"position": [1.0]}, "position_invalid"),
        ({"position": "3,4"}, "position_invalid"),
        ({"position": [float("inf"), 0.0]}, "position_invalid"),
        ({"position": [True, False]}, "position_invalid"),
        ({"idempotency_key": ""}, "idempotency_key_required"),
        ({"idempotency_key": "k" * 241}, "idempotency_key_invalid"),
        ({"placement_id": "///"}, "placement_id_invalid"),
    ],
)
def test_malformed_params_are_refused_with_a_typed_reason(qa_persona, params, reason):
    _seed_workspace()
    before = set(_instances())
    reply = _call(_params(**params))
    assert reply["error"]["data"]["reason"] == reason
    assert reply["error"]["code"] == serve_rpc.ERR_INVALID_PARAMS
    # A validation refusal that wrote a row is worse than one that raised.
    assert set(_instances()) == before
    assert _actors() == {}


# ── the roster refusal (UC-H2) ───────────────────────────────────────────────


def test_an_unknown_bare_persona_is_refused_and_provably_wrote_nothing(qa_persona):
    """The defect this stage exists to close: ``--persona qa_agent`` (there is
    no such persona; the roster has ``qa``) minted a roster row, a chat root and
    — on the lanes that place — an office actor, all bound to nothing.

    ANTI-VACUITY. Every probe here is an ABSENCE, and the kill-mutation (delete
    the roster branch) makes the create PROCEED, whose entire effect is to make
    those absences exist. The mutant cannot satisfy a probe whose satisfaction
    is defined by the mutant's own writes not happening. Three independent
    witnesses, because one absence could be explained by a create that failed
    for an unrelated reason:

    1. no instance file — the mint did not run;
    2. no reservation receipt for the key's digest — the create never got past
       normalisation, which is a STRICTLY earlier point than the mint;
    3. the event log did not grow — nothing downstream was told anything.

    Witness 2 is the one that pins the ORDER. A refusal placed after the
    reservation would still satisfy 1 and 3 while leaving a receipt that
    poisons the key, so a client fixing its typo would be answered with its own
    stale error forever.
    """

    import hashlib

    from agent_runtime import paths

    _seed_workspace()
    before_instances = set(_instances())
    before_events = _event_count()

    reply = _call(_params(persona_id="qa_agent", placement_id="qa_agent_probe"))

    assert reply["error"]["code"] == serve_rpc.ERR_INVALID_PARAMS
    assert reply["error"]["data"]["reason"] == "persona_not_found"
    # The message names the cure, because "qa_agent" looks plausible.
    assert "harness agent list" in reply["error"]["message"]

    assert set(_instances()) == before_instances
    assert not paths.persona_instance_path("personainst_qa_agent_probe").exists()
    digest = hashlib.sha256("gesture-1".encode("utf-8")).hexdigest()
    assert not paths.agent_create_reservation_path(digest).exists()
    assert _event_count() == before_events
    assert _actors() == {}


def test_a_seeded_persona_still_creates(qa_persona):
    """The over-broad-guard witness. A guard that refused everything would make
    the test above pass forever."""

    _seed_workspace()
    reply = _call(_params(placement_id="qa_still_works"))
    assert reply["result"]["persona_instance_id"] == "personainst_qa_still_works"
    # And the persona lookup that gated it is the SAME one that supplies the
    # honest default name, so a guard consulting a different roster than the
    # namer would show up here.
    assert reply["result"]["display_name"] == "QA Agent"


def test_a_profile_id_for_a_profile_that_owns_nothing_still_creates(qa_persona):
    """Decision D-U1, with the witness the plan says it must have.

    The launcher's template/preset browser sends ``profile:<token>`` ids for
    profiles that have NO persona row — the CLI resolver synthesises one. That
    lane had no test at all, which is exactly why making validation uniform
    would have broken it silently.

    ANTI-VACUITY. The kill-mutation is "apply the bare-id roster check to
    ``profile:`` ids too". The probe is a durable instance FILE plus a placed
    actor; under the mutant the create refuses and writes neither, so the
    mutant cannot set the probed state. Note the profile token deliberately
    matches NO persona and NO profile — ``profile:qa`` would pass even under
    the mutant, since ``qa`` is seeded, and would prove nothing.
    """

    from agent_runtime import paths

    _seed_workspace()
    reply = _call(
        _params(persona_id="profile:nosuchprofile", placement_id="profile_lane")
    )

    result = reply["result"]
    assert result["persona_id"] == "profile:nosuchprofile"
    assert paths.persona_instance_path("personainst_profile_lane").exists()
    assert result["actor_key"] in _actors()


def test_an_unusable_persona_id_is_still_a_SYNTAX_refusal_not_a_roster_one(qa_persona):
    """Order matters: ``!!!`` tokenises to nothing, and the launcher's decoder
    branches on the reason. Re-labelling it ``persona_not_found`` would send a
    client that sent garbage down the "add the persona" path."""

    _seed_workspace()
    reply = _call(_params(persona_id="!!!", placement_id="qa_garbage"))
    assert reply["error"]["data"]["reason"] == "persona_id_required"


# ── event parity with the two-call flow ──────────────────────────────────────


def test_the_emitted_events_equal_the_two_call_flow(qa_persona):
    """The anti-goal, pinned: no new event types and no extra emissions.

    This is what lets the method COMPOSE with D3 rather than collide with it —
    the handler calls the same chokepoints, so it inherits whatever they emit.
    Asserted against the chokepoints' LIVE output (both flows are run here),
    never against pinned bytes, so it re-bases automatically when an emitter
    changes rather than turning into a fixture nobody dares regenerate.
    """

    from agent_runtime.office_store import OfficeStore
    from agent_runtime.persona_assignments import PersonaInstanceStore

    _seed_workspace()

    # Flow A — what the launcher does today: the argv lane's store calls, then
    # the office lane's, with the client-side gap in between.
    marker = _event_count()
    instance = PersonaInstanceStore().add_instance(
        persona_id="qa",
        placement_id="qa_two_call",
        display_name=None,
        default_display_name="QA Agent",
        workspace_id=WORKSPACE,
        realm_id=None,
    )
    instance.spawned_by = "operator"
    PersonaInstanceStore().update(instance)
    OfficeStore().upsert_actor(
        WORKSPACE,
        {
            "persona_id": "qa",
            "persona_instance_id": instance.id,
            "items": [
                {
                    "item_id": instance.id,
                    "persona_id": "qa",
                    "kind": "agent",
                    "position": [3.5, -1.25],
                    "folder": "Agents",
                    "display_name": "QA Agent",
                }
            ],
        },
        updated_by="operator",
    )
    two_call = _event_types_since(marker)

    # Flow B — one call.
    marker = _event_count()
    _call(_params(placement_id="qa_one_call"))
    one_call = _event_types_since(marker)

    assert one_call == two_call
    # Guard the guard: a comparison of two empty lists would pass forever.
    assert len(one_call) >= 2
    assert "persona_instance.chat_opened" in one_call


# ── the chat root this lane hands back must RESOLVE ──────────────────────────
#
# Live 2026-08-20. An operator dragged a QA agent into the office and every
# message to it came back
#
#   UNKNOWN EXPLICIT PERSONA CHAT ROOT:
#   persona_chat_personainst_qa_agent_03ba2049_67d5a1a6921f
#
# That root was written into the persona-instance row (as BOTH
# ``default_chat_session_id`` and ``session_id``), into the create
# reservation's recorded result, and into the serve read-model — and into no
# SessionDB on the machine. ``persona_chat_session_id_for`` is a bare
# ``uuid4``; nothing on this lane created its row, because chat-root durability
# lived entirely in ``hermes_cli/harness_parts/persona_commands.py`` and this
# lane is not in ``hermes_cli``.
#
# It never self-heals: ``mission-chat message`` refuses any explicit root with
# no SessionDB row (correctly — that guard's job is refusing fabricated roots),
# and ``resolve_default_chat_session_id_for_instance`` re-offers the stored
# pointer forever on shape alone. So the agent is born undeliverable.
#
# What these rows assert is therefore not "a root came back" — the broken lane
# returned one too — but that the exact root in the reply DEREFERENCES, and
# that a store which cannot persist one leaves no agent behind at all.


def _chat_row(session_id: str):
    """Read one row out of the OPERATOR-visible chat store, and close it."""

    from agent_runtime.persona_chat_durability import default_persona_session_db

    session_db = default_persona_session_db()
    try:
        return session_db.get_session(session_id)
    finally:
        session_db.close()


def test_the_root_the_create_returns_resolves_in_the_operator_chat_store(qa_persona):
    _seed_workspace()
    reply = _call(_params(placement_id="qa_agent_durable"))
    result = reply["result"]
    root = result["default_chat_session_id"]

    # The reply's root, the row's pointer and the SessionDB row are ONE fact.
    assert _instances()[result["persona_instance_id"]].default_chat_session_id == root
    assert _chat_row(root) is not None, (
        f"the create returned {root!r} as this agent's chat pointer and no "
        "SessionDB row for it exists: every mission-chat send to this agent "
        "will be refused unknown_chat_session, forever"
    )


def test_the_created_root_carries_its_persona_ownership(qa_persona):
    """A row is not enough — the projection reads its ownership block.

    Without this the previous row could be satisfied by any bare insert, and
    ``_persona_chat_session_owner`` (the ``foreign_chat_session`` guard's
    reader) would answer nothing for a session the agent legitimately owns.
    """

    _seed_workspace()
    reply = _call(_params(placement_id="qa_agent_owned"))
    result = reply["result"]

    row = _chat_row(result["default_chat_session_id"])
    assert row is not None
    assert row["source"] == "agent_runtime_persona_chat"
    assert json.loads(row["model_config"])["persona_instance_id"] == (
        result["persona_instance_id"]
    )


def test_a_chat_store_that_cannot_persist_leaves_no_agent_behind(
    qa_persona, monkeypatch
):
    """The failure path, pinned: refuse loudly rather than bind a phantom.

    The defect this whole section exists for was not "the row was missing" — it
    was a mint treating "could not persist" as "carry on and bind anyway". So
    the store failing must produce NO instance row, NO placement, and a typed
    refusal, not an ``ok`` reply naming a root nothing can dereference.
    """

    from agent_runtime import persona_chat_durability

    def _no_store():
        raise persona_chat_durability.PersonaChatPersistenceError("session_db_acquire")

    monkeypatch.setattr(
        persona_chat_durability, "default_persona_session_db", _no_store
    )

    _seed_workspace()
    reply = _call(_params(placement_id="qa_agent_no_store"))

    assert "result" not in reply
    error = reply["error"]
    assert error["data"]["reason"] == "chat_session_persist_failed"
    assert error["data"]["persistence_operation"] == "session_db_acquire"
    # BOTH rows absent — the same "durable, or neither" property every other
    # failure row here asserts.
    assert "personainst_qa_agent_no_store" not in _instances()
    assert _actors() == {}
