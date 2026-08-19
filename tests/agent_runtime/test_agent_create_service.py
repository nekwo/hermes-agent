"""``agent_create.perform_agent_create`` — the create sequence, off the wire.

``tests/agent_runtime/test_serve_rpc_agent_create.py`` is the fence that the
JSON-RPC lane still behaves exactly as it did; this file is the fence for the
property that lane no longer owns: the sequence RUNS with no ``serve_rpc`` in
the picture at all. That is what makes ``harness agent create`` (UC-H3), a cron
script and any future MCP wrapper the same create rather than three.

Anti-vacuity notes are attached to each test, because the failure mode this
repo keeps re-discovering is a probe the mutated path also satisfies.
"""

from __future__ import annotations

import pytest

# Deliberately NOT importing ``agent_runtime.serve_rpc`` at module scope. The
# whole claim under test is that the sequence needs no RPC surface; a module
# level import would make every test below pass even if the shim were the only
# real implementation and the service a dead copy.
from agent_runtime import paths
from agent_runtime.agent_create import perform_agent_create
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_agent_create_service"


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
        "idempotency_key": "service-1",
    }
    params.update(overrides)
    return params


def _actors(workspace_id: str = WORKSPACE) -> dict:
    from agent_runtime.office_store import OfficeStore

    return {actor.actor_key: actor for actor in OfficeStore().list_actors(workspace_id)}


@pytest.fixture
def qa_persona():
    """A persona whose configured display name differs from its title-cased id."""

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


# ── the sequence, with no serve in the picture ───────────────────────────────


def test_the_service_places_an_agent_with_no_rpc_layer_involved(qa_persona):
    """UC-H1's load-bearing test.

    ANTI-VACUITY. The kill-mutation is "the shim keeps its own inline copy and
    ``perform_agent_create`` is dead code". Under that mutant this call writes
    NOTHING, so the probes below — the instance FILE on disk under
    ``paths.persona_instances_dir()`` and the office actor's ``revision`` — are
    unreachable, not merely different: the mutated path never executes, so it
    cannot also set them. Both probes are read back off the STORES rather than
    off the returned dict, so a service that fabricated a plausible reply
    without writing would also fail.
    """

    _seed_workspace()

    outcome = perform_agent_create(_params(placement_id="qa_service"))

    assert outcome.refusal is None
    assert outcome.refusal is None
    result = outcome.result
    assert result["persona_instance_id"] == "personainst_qa_service"
    assert result["idempotent_replay"] is False

    # Witness 1 — the roster row is a FILE, not a reply field.
    assert paths.persona_instance_path("personainst_qa_service").exists()
    assert (
        paths.persona_instance_path("personainst_qa_service").parent
        == paths.persona_instances_dir()
    )

    # Witness 2 — the placement landed, at its first revision.
    actors = _actors()
    assert result["actor_key"] in actors
    assert actors[result["actor_key"]].revision == 1
    assert actors[result["actor_key"]].persona_instance_id == "personainst_qa_service"

    # Witness 3 — the shared naming rule came with the sequence, not with the
    # RPC handler. "Qa" here means the hoist dropped the policy layer.
    assert result["display_name"] == "QA Agent"
    assert actors[result["actor_key"]].items[0].display_name == "QA Agent"


def test_the_service_refuses_a_bad_position_without_a_reply_envelope(qa_persona):
    """A refusal is a typed object, not a JSON-RPC frame, and still writes nothing."""

    _seed_workspace()
    before = set(_actors())

    outcome = perform_agent_create(_params(position="3,4", placement_id="qa_badpos"))

    assert outcome.refusal is not None
    assert outcome.result is None
    assert outcome.refusal.data["reason"] == "position_invalid"
    assert not paths.persona_instance_path("personainst_qa_badpos").exists()
    assert set(_actors()) == before


def test_a_replay_through_the_service_writes_nothing(qa_persona):
    """ANTI-VACUITY. The witness is the actor's REVISION, never the ids: the
    ids derive from the placement id, so a duplicating replay returns them
    unchanged while ``upsert_actor`` bumps the revision monotonically. A
    mutation that bypasses the reservation cannot re-write the actor without
    moving the probed field."""

    _seed_workspace()
    first = perform_agent_create(_params(placement_id="qa_svc_replay")).result

    second = perform_agent_create(_params(placement_id="qa_svc_replay")).result

    assert second["idempotent_replay"] is True
    assert second["persona_instance_id"] == first["persona_instance_id"]
    assert _actors()[first["actor_key"]].revision == 1


# ── the roster refusal, on the lane with no wire (UC-H2) ─────────────────────


def test_the_service_itself_refuses_an_unknown_bare_persona(qa_persona):
    """The refusal must live in the SERVICE, not in the RPC handler — otherwise
    ``harness agent create`` and every future MCP wrapper fail open while the
    wire lane fails closed, which is the two-spellings bug one layer up.

    ANTI-VACUITY: absence probes, and the kill-mutation's whole effect is to
    make those absences exist. See the sibling test in
    ``test_serve_rpc_agent_create.py`` for the three-witness argument.
    """

    _seed_workspace()

    outcome = perform_agent_create(
        _params(persona_id="qa_agent", placement_id="qa_agent_svc")
    )

    assert outcome.refusal is not None
    assert outcome.refusal.data["reason"] == "persona_not_found"
    assert not paths.persona_instance_path("personainst_qa_agent_svc").exists()
    assert _actors() == {}


def test_a_caller_supplied_persona_object_settles_the_question(qa_persona):
    """The CLI resolves personas through its own richer ``_persona_by_id``
    (profile synthesis, instance-id spellings). When it hands one over, the
    narrower :func:`resolve_persona` must not overrule it — otherwise the CLI
    lane refuses ids it can itself resolve."""

    from agent_runtime.models import AgentPersona

    _seed_workspace()
    synthesized = AgentPersona(
        id="only_in_the_callers_hand",
        display_name="Synthesised Agent",
        role="profile",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
    )

    outcome = perform_agent_create(
        _params(persona_id="only_in_the_callers_hand", placement_id="handed_in"),
        persona=synthesized,
    )

    assert outcome.refusal is None
    assert outcome.result["display_name"] == "Synthesised Agent"


# ── the shim is a shim ───────────────────────────────────────────────────────


def test_the_rpc_handler_holds_no_second_copy_of_the_sequence():
    """The single-copy gate, made precise.

    The plan's version of this was "``add_instance``/``upsert_actor`` appear
    zero times in serve_rpc.py", which cannot work: ``runtime.office.upsert``
    legitimately calls ``upsert_actor`` in the same file. So the gate reads the
    HANDLER's own source instead of the module's.

    ANTI-VACUITY. The kill-mutation is "leave the inline sequence behind beside
    the extracted function". The probe is the handler's source text, which the
    mutant necessarily contains — the mutation IS the presence of those calls,
    so it cannot pass by also satisfying the probe.
    """

    import inspect

    from agent_runtime import serve_rpc

    source = inspect.getsource(serve_rpc._runtime_agent_create)

    for forbidden in (
        "add_instance",
        "upsert_actor",
        "reserve_agent_create",
        "mark_instance_minted",
        "mark_done",
        "class_key_collision",
        "placement_actor_payload",
    ):
        assert forbidden not in source, (
            f"{forbidden!r} is back inside the RPC handler: the create sequence "
            "has a second copy again, and the two lanes can now drift."
        )
    assert "perform_agent_create" in source
    # Guard the guard: a handler that had been emptied entirely would satisfy
    # every assertion above.
    assert "err(" in source and "ok(" in source


def test_the_services_error_codes_are_serve_rpcs_error_codes():
    """The four constants are re-spelled in ``agent_create`` so a CLI process
    need not import the RPC registry. This is the fence that keeps the two
    spellings from drifting into two different wire contracts."""

    from agent_runtime import agent_create, serve_rpc

    assert agent_create.ERR_INVALID_PARAMS == serve_rpc.ERR_INVALID_PARAMS
    assert agent_create.ERR_HANDLER_FAILED == serve_rpc.ERR_HANDLER_FAILED
    assert agent_create.ERR_NOT_FOUND == serve_rpc.ERR_NOT_FOUND
    assert agent_create.ERR_CONFLICT == serve_rpc.ERR_CONFLICT


# ── the roster fault is not the roster's answer (reviewer addition) ──────────
#
# UC-H2 made the roster load-bearing: before it, a config this process could not
# read degraded quietly to a title-cased display name. After it, routing that
# fault through the same ``None`` as "no such persona" would have refused EVERY
# bare-id create on EVERY lane with a message blaming the operator's id.
#
# ANTI-VACUITY. Each test probes the ``reason`` STRING, and the two tests demand
# DIFFERENT values from the same call shape — a mutant that collapses the two
# branches (either direction) can satisfy at most one of them. Probing "was the
# create refused?" alone would pass under the collapse, which is exactly the
# vacuous witness this file's other tests were careful to avoid.


def test_an_unreadable_roster_is_its_own_reason_not_persona_not_found(
    monkeypatch, qa_persona
):
    """A runtime fault must not wear a bad id's costume."""

    from agent_runtime import agent_create

    # Patched at the CONFIG loader, not at ``persona_roster`` — so the real
    # wrapping runs and the test proves the typed fault is actually minted
    # rather than assuming it. Patching the wrapper would have tested nothing
    # but the wrapper's absence.
    from agent_runtime import config as runtime_config

    def _explode(*args, **kwargs):
        raise OSError("config file is locked by another process")

    monkeypatch.setattr(runtime_config, "load_agent_runtime_config", _explode)

    with pytest.raises(agent_create.AgentCreateInvalid) as caught:
        agent_create.normalize_agent_create(
            {
                "persona_id": "qa",
                "workspace_id": "ws_probe",
                "position": [0.0, 0.0],
                "idempotency_key": "k-roster-fault",
            }
        )

    assert caught.value.reason == agent_create.PERSONA_ROSTER_UNAVAILABLE_REASON
    assert caught.value.reason != agent_create.PERSONA_NOT_FOUND_REASON
    # The message must not send the operator hunting a typo.
    assert "not in the agent roster" not in str(caught.value)


def test_an_empty_roster_that_LOADED_still_refuses_as_persona_not_found(
    monkeypatch, qa_persona
):
    """An empty roster is a real answer, and keeps the actionable reason.

    The counterpart to the test above: this is what stops the fix from being a
    blanket excuse that reopens the hole UC-H2 closed.
    """

    from agent_runtime import agent_create

    monkeypatch.setattr(agent_create, "persona_roster", lambda: [])

    with pytest.raises(agent_create.AgentCreateInvalid) as caught:
        agent_create.normalize_agent_create(
            {
                "persona_id": "qa",
                "workspace_id": "ws_probe",
                "position": [0.0, 0.0],
                "idempotency_key": "k-empty-roster",
            }
        )

    assert caught.value.reason == agent_create.PERSONA_NOT_FOUND_REASON
    assert caught.value.reason != agent_create.PERSONA_ROSTER_UNAVAILABLE_REASON
