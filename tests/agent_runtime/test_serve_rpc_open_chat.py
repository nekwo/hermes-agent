"""``runtime.persona.instance.open_chat`` — the verb that had no method.

Plan ``EterniaLauncher/docs/mission_control/planned/remote-chat-parity.md``
stage C1h, ruling **R-C5**. Four kinds of claim, and they are in one file
because they are the same claim from four sides:

1. **It is on the lane, at the tier, in the manifest.** The launcher gates its
   lowering on membership of the name in the greeting's ``rpc.methods`` set (the
   D12 pattern), so manifest membership is the wire fact C2l depends on — not a
   nicety.
2. **One implementation, two doors.** The strong form is not "a dict comes
   back": it is *the method's row is the CLI verb's row, key for key*. A fork
   would have to change one of them to pass.
3. **Every refusal the CLI can print is translated, not re-spelled.** Each arm
   is driven into its own failure and the frame is asserted against the CLI's
   own ``error_kind`` / ``next_expected`` — the strings a launcher renders.
4. **The gate admits a console device and refuses a read one.** That is the
   whole point of the stage: a paired console device may open a chat on the
   install it is aimed at, and the name is deliberately NOT in
   ``LOCAL_CONSOLE_METHODS``.

Nothing here spawns a ``harness serve`` child. The measurement of what a real
serve's LANES carry is a different question and lives in
``test_serve_gateway_chat_reply_lanes.py``.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent_runtime import serve_rpc
from agent_runtime.call_authorization import (
    CALLER_DEVICE,
    LOCAL_CONSOLE,
    LOCAL_CONSOLE_METHODS,
    PEER_METHOD_ALLOWLIST,
    TIER_CONSOLE,
    TIER_READ,
    RpcCaller,
    authorize_call,
    caller_for_connection,
)
from agent_runtime.mission_chat_outcome import ChatErrorKind
from agent_runtime.persona_assignments import PersonaInstanceStore
from agent_runtime.persona_open_chat import (
    OPEN_CHAT_METHOD,
    perform_persona_instance_open_chat,
    refusal_codes_by_kind,
)
from agent_runtime.serve_rpc import (
    ERR_CONFLICT,
    ERR_INVALID_PARAMS,
    ERR_NOT_FOUND,
    RpcContext,
)
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_open_chat_test"
PERSONA = "qa"


class _Connection:
    """The duck ``caller_for_connection`` reads through ``getattr`` — the same
    stand-in ``test_scope_use_methods`` uses, and for its reason: the predicate
    must never import the socket module."""

    def __init__(self, **fields):
        from agent_runtime.call_authorization import TRANSPORT_GATEWAY

        self.key = fields.pop("key", "conn-1")
        self.transport = fields.pop("transport", TRANSPORT_GATEWAY)
        self.authenticated = fields.pop("authenticated", True)
        self.device_id = None
        self.device_tier = None
        self.peer_install_id = None
        for name, value in fields.items():
            setattr(self, name, value)


def _device(tier: str, device_id: str = "dev_phone") -> RpcCaller:
    return caller_for_connection(_Connection(device_id=device_id, device_tier=tier))


def _call(params: dict, *, rid: str = "oc-1", caller: RpcCaller | None = None) -> dict:
    """One request through ``handle_request`` — the DISPATCHER, not the handler,
    because the authorization is at the dispatcher and a test that called the
    handler directly would prove nothing about the gate."""

    return serve_rpc.handle_request(
        {"jsonrpc": "2.0", "id": rid, "method": OPEN_CHAT_METHOD, "params": params},
        context=RpcContext(caller=caller if caller is not None else LOCAL_CONSOLE),
    )


@pytest.fixture
def qa_persona(isolate_agent_runtime_root):
    """One roster persona. An isolated root ships none, so the subject of every
    test below has to be put there first."""

    from agent_runtime.models import AgentPersona
    from agent_runtime.store import AgentStore

    persona = AgentPersona(
        id=PERSONA,
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
def placed_agent(qa_persona):
    """One real placement, minted by the verb that owns minting.

    ``runtime.agent.create`` rather than a hand-built store row: open-chat's
    subject is an instance that already exists, and seeding it through the
    method that places one keeps this file from inventing a second way for a
    persona instance to come into being.
    """

    from agent_runtime.office_store import OfficeStore

    seed_workspace_record(WORKSPACE)
    OfficeStore().ensure_surface(WORKSPACE, created_by="seed")
    reply = serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "seed",
            "method": "runtime.agent.create",
            "params": {
                "persona_id": PERSONA,
                "workspace_id": WORKSPACE,
                "position": [1.0, 2.0],
                "idempotency_key": "open-chat-seed",
            },
        }
    )
    assert "result" in reply, reply
    return reply["result"]


# ── registration, the manifest, and the door it is NOT behind ────────────────


def test_the_method_is_registered_at_the_console_tier():
    assert OPEN_CHAT_METHOD in serve_rpc.method_names()
    assert serve_rpc.method_tier(OPEN_CHAT_METHOD) == TIER_CONSOLE


def test_the_manifest_carries_the_name_and_the_contract_integer_does_not_move():
    """Adding a method grows the SET; the integer means an incompatible SHAPE
    change (``serve_rpc``'s own header). The launcher's C2l lowering is a
    membership test against exactly this set, so this is the wire fact the
    launcher half depends on."""

    manifest = serve_rpc.manifest()

    assert OPEN_CHAT_METHOD in manifest["methods"]
    assert manifest["tiers"][OPEN_CHAT_METHOD] == TIER_CONSOLE
    assert manifest["contract"] == serve_rpc.RPC_CONTRACT_VERSION == 1


def test_the_name_is_not_in_the_local_console_set_or_the_peer_allowlist():
    """Both absences are decisions and both are asserted.

    NOT ``LOCAL_CONSOLE_METHODS``: that set is for verbs whose subject is the
    machine owner's own session, and a paired console device opening a chat on
    the install it is aimed at is the FEATURE this stage ships.

    NOT ``PEER_METHOD_ALLOWLIST``: another install's agents do not open chats
    here. That holds by construction — the set admits nothing it does not name —
    and ``test_peer_authorization``'s registry walk is what enforces it without
    an edit; this line is the readable restatement.
    """

    assert OPEN_CHAT_METHOD not in LOCAL_CONSOLE_METHODS
    assert OPEN_CHAT_METHOD not in PEER_METHOD_ALLOWLIST


# ── the gate ─────────────────────────────────────────────────────────────────


def test_a_console_device_is_admitted_and_a_read_device_is_refused():
    """The stage's own sentence, as a predicate. The ``read`` half is the tier
    equality; the ``console`` half is the one that would have failed had the
    name joined ``LOCAL_CONSOLE_METHODS`` by reflex."""

    admitted = authorize_call(TIER_CONSOLE, _device(TIER_CONSOLE), method=OPEN_CHAT_METHOD)
    refused = authorize_call(TIER_CONSOLE, _device(TIER_READ), method=OPEN_CHAT_METHOD)

    assert admitted.ok
    assert admitted.caller_kind == CALLER_DEVICE
    assert not refused.ok
    assert refused.caller_kind == CALLER_DEVICE


def test_the_dispatcher_refuses_a_read_device_before_the_handler_runs(placed_agent):
    """End to end through ``handle_request``, with the half that matters: the
    instance's chat binding does NOT move."""

    instance_id = placed_agent["persona_instance_id"]
    before = PersonaInstanceStore().get(instance_id).default_chat_session_id

    reply = _call(
        {"persona_id": PERSONA, "new_session": True, "idempotency_key": "refused-1"},
        caller=_device(TIER_READ),
    )

    assert "result" not in reply
    assert reply["error"]["data"]["caller"] == CALLER_DEVICE
    assert PersonaInstanceStore().get(instance_id).default_chat_session_id == before


# ── the accept ───────────────────────────────────────────────────────────────


def test_a_new_session_open_mints_a_root_and_answers_the_two_keys_the_bridge_reads(
    placed_agent,
):
    """``session_id`` and ``persona_instance_id`` are not two convenient fields:
    they are the exact pair the launcher's bridge reads back off
    ``persona.instance.open_chat`` today, and this method exists so that reader
    is fed unchanged when the gesture is aimed at another install."""

    instance_id = placed_agent["persona_instance_id"]

    reply = _call(
        {
            "persona_id": PERSONA,
            "persona_instance_id": instance_id,
            "new_session": True,
            "idempotency_key": "gesture-1",
        }
    )
    row = reply["result"]

    assert row["ok"] is True
    assert row["persona_instance_id"] == instance_id
    assert row["session_id"]
    assert row["session_id"] == row["mission_chat_root_id"]
    assert row["new_session"] is True
    assert row["idempotent_replay"] is False
    assert row["selected"] is True
    # The binding really moved, read back off the store rather than off the ack.
    assert PersonaInstanceStore().get(instance_id).session_id == row["session_id"]


def test_the_method_row_is_the_argv_verbs_row_key_for_key(placed_agent, capsys):
    """ONE implementation, proven by comparing the two doors' answers rather
    than by reading the source. The argv row is taken through the real CLI
    parser and handler, so every default the parser supplies is in the
    comparison — which is what makes a namespace this service builds by hand a
    translation rather than a second contract.

    The SAME idempotency key is used on both sides deliberately: the second call
    is a replay of the first, so the two rows are only comparable if the replay
    is honest about being one. ``idempotent_replay`` is therefore excluded from
    the comparison and asserted separately, as the one key that MUST differ.
    """

    from hermes_cli import harness

    instance_id = placed_agent["persona_instance_id"]

    reply = _call(
        {
            "persona_id": PERSONA,
            "persona_instance_id": instance_id,
            "new_session": True,
            "idempotency_key": "one-gesture",
        }
    )
    method_row = reply["result"]

    root = argparse.ArgumentParser()
    harness.build_parser(root.add_subparsers(dest="cmd"))
    args = root.parse_args(
        [
            "harness",
            "persona",
            "instance",
            "open-chat",
            "--persona",
            PERSONA,
            "--persona-instance-id",
            instance_id,
            "--new-session",
            "--idempotency-key",
            "one-gesture",
            "--json",
        ]
    )
    assert args.func(args) == 0
    argv_row = json.loads(capsys.readouterr().out)

    assert method_row["idempotent_replay"] is False
    assert argv_row["idempotent_replay"] is True
    volatile = {"idempotent_replay"}
    assert {k: v for k, v in method_row.items() if k not in volatile} == {
        k: v for k, v in argv_row.items() if k not in volatile
    }


def test_the_same_idempotency_key_twice_mints_one_root_and_replays_it(placed_agent):
    """The CLI's own reservation, reached through the method lane rather than
    duplicated by it: a second key for one fact is the drift R-C2 exists to
    end, one verb over."""

    instance_id = placed_agent["persona_instance_id"]
    params = {
        "persona_id": PERSONA,
        "persona_instance_id": instance_id,
        "new_session": True,
        "idempotency_key": "replay-me",
    }

    first = _call(params, rid="oc-first")["result"]
    second = _call(params, rid="oc-second")["result"]

    assert first["session_id"] == second["session_id"]
    assert first["idempotent_replay"] is False
    assert second["idempotent_replay"] is True
    assert second["mint_receipt_state"] == "bound"


def test_a_correlation_id_rides_the_ack_and_only_when_sent(placed_agent):
    instance_id = placed_agent["persona_instance_id"]

    with_id = _call(
        {
            "persona_id": PERSONA,
            "persona_instance_id": instance_id,
            "new_session": True,
            "idempotency_key": "corr-1",
            "correlation_id": "gesture_7f",
        }
    )["result"]
    without = _call(
        {
            "persona_id": PERSONA,
            "persona_instance_id": instance_id,
            "new_session": True,
            "idempotency_key": "corr-2",
        },
        rid="oc-2",
    )["result"]

    assert with_id["correlation_id"] == "gesture_7f"
    assert "correlation_id" not in without


def test_client_message_id_is_accepted_as_the_replay_key_alias(placed_agent):
    """The launcher spells this ``idempotency_key`` on THIS verb and
    ``client_message_id`` on the send lane and on ``persona instance create``.
    Both reach one CLI flag, so one client vocabulary does not fork by verb."""

    instance_id = placed_agent["persona_instance_id"]

    keyed = _call(
        {
            "persona_id": PERSONA,
            "persona_instance_id": instance_id,
            "new_session": True,
            "idempotency_key": "alias-1",
        }
    )["result"]
    aliased = _call(
        {
            "persona_id": PERSONA,
            "persona_instance_id": instance_id,
            "new_session": True,
            "client_message_id": "alias-1",
        },
        rid="oc-alias",
    )["result"]

    assert aliased["session_id"] == keyed["session_id"]
    assert aliased["idempotent_replay"] is True


def test_an_explicit_session_id_rebinds_and_answers_the_rebind_arms_row(placed_agent):
    """The other arm. It is reachable only because ``session_id`` is a param —
    the plan's stage list did not name one, and without it every
    ``new_session: false`` call would refuse and the arm would be dead."""

    minted = _call(
        {
            "persona_id": PERSONA,
            "persona_instance_id": placed_agent["persona_instance_id"],
            "new_session": True,
            "idempotency_key": "rebind-seed",
        }
    )["result"]

    reply = _call(
        {"persona_id": PERSONA, "session_id": minted["session_id"]}, rid="oc-rebind"
    )
    row = reply["result"]

    assert row["ok"] is True
    assert row["session_id"] == minted["session_id"]
    assert row["persona_instance_id"] == placed_agent["persona_instance_id"]
    # The rebind arm's own keys, which the new-session arm does not carry.
    assert row["binding_receipt"]["session_id"] == minted["session_id"]
    assert "previous_session_id" in row


def test_requested_by_defaults_to_the_calling_devices_id(placed_agent, monkeypatch):
    """Provenance comes off what the TRANSPORT proved, never off ``params`` — a
    request that could name its own requester would name its own provenance.
    Asserted at the namespace the service builds, because the open-chat row does
    not echo the field."""

    seen: list = []
    from hermes_cli import harness

    original = harness._cmd_persona_instance_open_chat

    def _record(args):
        seen.append(args.requested_by)
        return original(args)

    monkeypatch.setattr(harness, "_cmd_persona_instance_open_chat", _record)

    _call(
        {"persona_id": PERSONA, "new_session": True, "idempotency_key": "who-1"},
        caller=_device(TIER_CONSOLE, device_id="dev_the_mac"),
    )
    _call(
        {"persona_id": PERSONA, "new_session": True, "idempotency_key": "who-2"},
        rid="oc-local",
    )
    _call(
        {
            "persona_id": PERSONA,
            "new_session": True,
            "idempotency_key": "who-3",
            "requested_by": "launcher",
        },
        rid="oc-explicit",
        caller=_device(TIER_CONSOLE, device_id="dev_the_mac"),
    )

    # A device caller's own id; the CLI's own default for a caller with no
    # device identity; and an explicit value winning over both.
    assert seen == ["dev_the_mac", "cli", "launcher"]


# ── the refusals, one per CLI arm ────────────────────────────────────────────


def test_a_missing_persona_id_is_refused_before_the_handler_is_entered():
    outcome = perform_persona_instance_open_chat({})

    assert outcome.result is None
    assert outcome.refusal.code == ERR_INVALID_PARAMS
    assert outcome.refusal.data == {"reason": "persona_id_required"}


def test_new_session_with_no_replay_key_is_the_mints_own_typed_refusal(placed_agent):
    reply = _call(
        {
            "persona_id": PERSONA,
            "persona_instance_id": placed_agent["persona_instance_id"],
            "new_session": True,
        }
    )

    error = reply["error"]
    assert error["code"] == ERR_INVALID_PARAMS
    assert error["data"]["reason"] == "idempotency_key_required"
    # The CLI's own operator-facing sentence rides through unchanged.
    assert error["data"]["next_expected"]


def test_new_session_together_with_a_session_id_is_refused_as_invalid_request(
    placed_agent,
):
    reply = _call(
        {
            "persona_id": PERSONA,
            "persona_instance_id": placed_agent["persona_instance_id"],
            "new_session": True,
            "session_id": "persona_chat_whatever",
            "idempotency_key": "both-1",
        }
    )

    error = reply["error"]
    assert error["code"] == ERR_INVALID_PARAMS
    assert error["data"]["reason"] == ChatErrorKind.INVALID_REQUEST
    assert "session_id must be omitted" in error["message"]


def test_an_unknown_instance_is_not_found_rather_than_invented(placed_agent):
    reply = _call(
        {
            "persona_id": PERSONA,
            "new_session": True,
            "persona_instance_id": "personainst_qa_deadbeef",
            "idempotency_key": "ghost-1",
        }
    )

    error = reply["error"]
    assert error["code"] == ERR_NOT_FOUND
    assert error["data"]["reason"] == ChatErrorKind.PERSONA_INSTANCE_NOT_FOUND
    assert error["data"]["persona_instance_id"] == "personainst_qa_deadbeef"


def test_an_unknown_chat_root_is_not_found(placed_agent):
    reply = _call({"persona_id": PERSONA, "session_id": "persona_chat_qa_00000000"})

    error = reply["error"]
    assert error["code"] == ERR_NOT_FOUND
    assert error["data"]["reason"] == ChatErrorKind.UNKNOWN_CHAT_SESSION


def test_a_missing_session_id_on_the_rebind_arm_is_the_untyped_refusals_default(
    placed_agent,
):
    """The CLI prints this one WITHOUT an ``error_kind`` — it is argparse-level
    wrongness that predates the typed vocabulary. The shim does not invent a
    kind for it; it falls to the default code and says so in ``reason``."""

    reply = _call({"persona_id": PERSONA})

    error = reply["error"]
    assert error["code"] == ERR_INVALID_PARAMS
    assert error["data"]["reason"] == "open_chat_refused"
    assert "session_id is required" in error["message"]


def test_a_chat_root_owned_by_another_instance_is_a_conflict(placed_agent):
    """The ``foreign_chat_session`` fence, reached with a root that exists and
    belongs to somebody else. A second placement is minted for the owner so the
    root is genuinely another instance's rather than merely unknown."""

    other = serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "seed-2",
            "method": "runtime.agent.create",
            "params": {
                "persona_id": PERSONA,
                "workspace_id": WORKSPACE,
                "position": [4.0, 5.0],
                "idempotency_key": "open-chat-seed-2",
                "placement_id": "qa_agent_a1b2c3d4",
            },
        }
    )["result"]
    foreign_root = other["default_chat_session_id"]
    assert other["persona_instance_id"] != placed_agent["persona_instance_id"]

    reply = _call(
        {
            "persona_id": PERSONA,
            "persona_instance_id": placed_agent["persona_instance_id"],
            "session_id": foreign_root,
        }
    )

    error = reply["error"]
    assert error["code"] == ERR_CONFLICT
    assert error["data"]["reason"] == ChatErrorKind.FOREIGN_CHAT_SESSION
    assert error["data"]["next_expected"]


def test_a_retired_instance_answers_its_tombstone_rather_than_unknown(placed_agent):
    """Retiring a placement archives the row and deliberately leaves its chat on
    disk, so re-opening that thread has a typed end-of-life answer. The method
    lane must carry it — a launcher that got ``unknown_chat_session`` here would
    offer the wrong next step."""

    instance_id = placed_agent["persona_instance_id"]
    root = placed_agent["default_chat_session_id"]
    retire = serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "retire",
            "method": "runtime.agent.retire",
            "params": {"persona_instance_id": instance_id, "reason": "test"},
        }
    )
    assert "result" in retire, retire

    reply = _call({"persona_id": PERSONA, "session_id": root})

    error = reply["error"]
    assert error["code"] == ERR_CONFLICT
    assert error["data"]["reason"] == ChatErrorKind.RETIRED_PERSONA_INSTANCE


# ── the translation table itself ─────────────────────────────────────────────


def test_every_code_in_the_refusal_table_is_one_serve_rpc_declares():
    """The table is the shim's ONLY authored vocabulary, and it authors no
    strings — only the join from the CLI's kinds to numbers ``serve_rpc``
    already owns. A number that is not one of those would be an error code no
    client's decoder has a branch for."""

    declared = {
        serve_rpc.ERR_INVALID_PARAMS,
        serve_rpc.ERR_NOT_FOUND,
        serve_rpc.ERR_CONFLICT,
        serve_rpc.ERR_HANDLER_FAILED,
    }
    table = refusal_codes_by_kind()

    assert table, "an empty table would pass every membership assertion below"
    assert set(table.values()) <= declared
    assert all(isinstance(kind, str) for kind in table)
