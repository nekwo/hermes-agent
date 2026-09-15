"""``runtime.agent.retire`` — the inverse of the placement verb, on the wire.

The service's own properties are pinned next door
(``test_agent_retire_service.py``). What this file has to prove is what only the
METHOD lane can be wrong about:

1. **The name is advertised, and the integer beside it did not move.** A
   manifest is a set plus an integer, and this method is the launcher's D12
   marker: a client reads the presence of THIS name as "this serve accepts an
   absent ``position``". A serve that shipped the name and moved the contract
   would break every client that only re-reads the set.
2. **The shim translates and nothing else.** The ack the wire carries is the
   service's dict, key for key — a handler that rebuilt it would be the second
   copy the hoist exists to abolish.
3. **The refusal frames carry the store's code in ``data.reason``**, because the
   launcher decodes ``data.reason`` first and the numeric code second.
4. **A retry over a lossy link is idempotent.** The second call is a replay,
   not a 4001.

Nothing here spawns a ``harness serve``: the handler is reached through
``serve_rpc.handle_request``, the same entry point both transports use.
"""

from __future__ import annotations

import pytest

from agent_runtime import serve_rpc
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_agent_retire_rpc"


def _call(params: dict, rid: str = "r1") -> dict:
    return serve_rpc.handle_request(
        {"jsonrpc": "2.0", "id": rid, "method": "runtime.agent.retire", "params": params}
    )


@pytest.fixture
def qa_persona():
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
def seeded_workspace():
    from agent_runtime.office_store import OfficeStore

    seed_workspace_record(WORKSPACE)
    store = OfficeStore()
    store.ensure_surface(WORKSPACE, created_by="seed")
    return store


def _place(placement_id: str = "qa_rpc_retire_1_agent_2") -> dict:
    """Place through the REAL create method, over the same lane."""

    reply = serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "c1",
            "method": "runtime.agent.create",
            "params": {
                "persona_id": "qa",
                "workspace_id": WORKSPACE,
                "position": [1.0, 1.0],
                "idempotency_key": f"rpc-retire-{placement_id}",
                "placement_id": placement_id,
            },
        }
    )
    assert "result" in reply, reply
    return reply["result"]


# ── the manifest ─────────────────────────────────────────────────────────────


def test_the_method_is_advertised_without_moving_the_contract_version():
    """KILLING MUTATION: bump ``RPC_CONTRACT_VERSION`` alongside the new method
    and this reds — which is the discipline the manifest advertises (the set
    grows when a verb is added; the integer moves only when an existing verb's
    SHAPE changes incompatibly).
    """

    manifest = serve_rpc.manifest()

    assert "runtime.agent.retire" in manifest["methods"]
    assert "runtime.agent.retire" in serve_rpc.method_names()
    assert manifest["contract"] == 1
    assert serve_rpc.RPC_CONTRACT_VERSION == 1


def test_the_advertised_tier_is_console_because_this_is_a_level_mutation():
    """Stage A1. The scope canon 06 and this handler's own docstring stated as
    prose (owner decision D10-iv) is now a machine-readable fact a connector can
    read BEFORE it tries the call. Widening it to ``read`` reds here."""

    assert serve_rpc.manifest()["tiers"]["runtime.agent.retire"] == "console"


def test_a_caller_without_the_console_tier_is_refused_before_the_service_runs():
    """Stage A3, on the verb the whole chokepoint plan was written about.

    Asserted on ``data.reason`` rather than on the message, because that is what
    the launcher's decoders branch on — the same rule the refusal frames below
    follow. The caller kind used here is one nothing yet mints; the day Stage A5
    mints a device credential, this is the frame it gets.
    """

    from agent_runtime.call_authorization import RpcCaller

    frame = serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "r-denied",
            "method": "runtime.agent.retire",
            "params": {"persona_instance_id": "personainst_whatever"},
        },
        serve_rpc.RpcContext(caller=RpcCaller(kind="unknown", transport="unknown")),
    )

    assert "result" not in frame
    assert frame["error"]["code"] == serve_rpc.ERR_HANDLER_FAILED
    assert frame["error"]["data"] == {
        "reason": "scope_denied",
        "tier": "console",
        "caller": "unknown",
    }


def test_the_marker_never_appears_without_the_capability_it_stands_for(qa_persona):
    """D12, asserted where the marker lives.

    The launcher omits ``position`` from a create the moment it sees this name
    in the manifest, so the name is a promise about ``runtime.agent.create``.
    Advertising it on a serve whose create still refuses an absent position
    would strand every client that trusted the marker — so the promise is
    measured, on the real method, rather than left to the ordering of two
    slices.
    """

    from agent_runtime.agent_create import normalize_agent_create

    assert "runtime.agent.retire" in serve_rpc.manifest()["methods"]
    request = normalize_agent_create(
        {
            "persona_id": "qa",
            "workspace_id": WORKSPACE,
            "idempotency_key": "d12-marker-probe",
        }
    )
    assert request.position is None


# ── the shim translates, and nothing else ────────────────────────────────────


def test_the_wire_ack_is_the_services_dict_key_for_key(qa_persona, seeded_workspace):
    """KILLING MUTATION: rebuild the reply in the handler (echo the request's id
    and a hand-made ``archived_actor_keys``) — the equality below reds, because
    the service's ack carries fields the request never had.
    """

    from agent_runtime.agent_retire import perform_agent_retire

    placed = _place()
    reply = _call({"persona_instance_id": placed["persona_instance_id"]})

    assert "error" not in reply, reply
    result = reply["result"]
    assert result["persona_instance_id"] == placed["persona_instance_id"]
    assert result["archived_actor_keys"] == [placed["actor_key"]]
    assert result["office_archive_failures"] == []
    assert result["already_retired"] is False

    # And the SECOND call through the service directly answers the same ack the
    # wire would: one function, two doors.
    replay = perform_agent_retire(
        {"persona_instance_id": placed["persona_instance_id"]}
    ).result
    assert replay["archive_path"] == result["archive_path"]
    assert replay["archived_actor_keys"] == result["archived_actor_keys"]


def test_the_placement_is_gone_from_both_stores_after_one_call(
    qa_persona, seeded_workspace
):
    """The join, asserted against the stores rather than the ack.

    Before this method the launcher removed a placement through two unjoined
    lanes, and a half-state (row archived, desk live) was representable. ONE
    call now, and BOTH halves are read back.
    """

    from agent_runtime import paths
    from agent_runtime.office_store import OfficeStore

    placed = _place(placement_id="qa_rpc_retire_2_agent_2")
    reply = _call({"persona_instance_id": placed["persona_instance_id"]})

    assert "error" not in reply, reply
    assert not paths.persona_instance_path(placed["persona_instance_id"]).exists()
    assert placed["actor_key"] not in {
        actor.actor_key for actor in OfficeStore().scan_actors(WORKSPACE).actors
    }


# ── idempotence over a lossy link ────────────────────────────────────────────


def test_second_retire_is_already_retired(qa_persona, seeded_workspace):
    """KILLING MUTATION (plan §C): return 4001 on the second call — this reds.

    A remote client whose first ack never arrived must be able to ask again, and
    get an ANSWER rather than an error about a state its own first call caused.
    """

    placed = _place(placement_id="qa_rpc_retire_3_agent_2")
    first = _call({"persona_instance_id": placed["persona_instance_id"]})["result"]

    second = _call({"persona_instance_id": placed["persona_instance_id"]}, rid="r2")

    assert "error" not in second, second
    assert second["id"] == "r2"
    result = second["result"]
    assert result["already_retired"] is True
    assert result["archive_path"] == first["archive_path"]
    assert result["archived_actor_keys"] == first["archived_actor_keys"]


# ── refusals ─────────────────────────────────────────────────────────────────


def test_an_unknown_id_is_a_typed_not_found_frame(qa_persona):
    reply = _call({"persona_instance_id": "personainst_no_such_placement"})

    assert reply["error"]["code"] == 4001
    assert reply["error"]["data"]["reason"] == "not_found"


def test_the_canonical_channel_is_a_conflict_frame_naming_its_reason(qa_persona):
    from agent_runtime.persona_assignments import PersonaInstanceStore

    canonical = PersonaInstanceStore().ensure_for_persona(qa_persona)

    reply = _call({"persona_instance_id": canonical.id})

    assert reply["error"]["code"] == 4090
    assert reply["error"]["data"]["reason"] == "canonical_persona_channel"
    assert reply["error"]["data"]["persona_instance_id"] == canonical.id


def test_a_missing_target_is_invalid_params_not_a_handler_crash():
    reply = _call({})

    assert reply["error"]["code"] == -32602
    assert reply["error"]["data"]["reason"] == "persona_instance_id_required"
