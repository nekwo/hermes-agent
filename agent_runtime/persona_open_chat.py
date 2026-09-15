"""Opening a chat on the method lane — ONE implementation, two doors.

Plan ``EterniaLauncher/docs/mission_control/planned/remote-chat-parity.md``,
stage C1h, ruling **R-C5**: *"Opening a chat is a method."* Until this module
existed the runtime registered nineteen ``runtime.*`` verbs and none of them
opened a chat, so a console aimed at another install could place an agent, move
it, retire it and send it a turn — and could not start the conversation. The
launcher's only lowering was argv, and the argv lane is refused to a remote aim
on purpose (it would open a chat on the machine the operator is sitting at,
under a gesture aimed at the other one).

**A TRANSLATION SHIM, in ``_runtime_agent_create``'s discipline and not a second
implementation.** That discipline has one rule and it is the whole point: the
sequence lives in a function BOTH doors call, so a future edit cannot move one
without the other. For ``runtime.agent.create`` that function is
``agent_create.perform_agent_create``; here the shim lands one step lower, the
way ``runtime.chat.message``'s does, because the sequence is an argparse handler
rather than a ``perform_*``: ``_cmd_persona_instance_open_chat`` owns the
coordinator budget, the ``--add-instance`` mint, the retirement tombstone, the
``foreign_chat_session`` ownership fence, the ``--new-session`` mint reservation
and the actor prewarm, and hoisting ~350 lines of it would have produced exactly
the second implementation the discipline exists to forbid. So this door BUILDS
THE HANDLER'S OWN NAMESPACE and calls it, and the row it answers is the row the
CLI prints.

**How the row comes back, and the one way it must not.** Through
``args.payload_sink`` — the seam ``persona_commands._emit_persona_open_chat_payload``
now carries, added for this door and modelled on the send lane's own
(``_emit_mission_chat_payload``, whose docstring records why). The alternative
is ``contextlib.redirect_stdout``, and it is not available here: this handler
runs INLINE on a serve's reader loop, ``redirect_stdout`` rebinds ``sys.stdout``
process-globally, and a serve whose stdout is the frame protocol every other
client reads would have that protocol stolen for the length of the call.

**What is NOT here.** Authorization. The tier is declared at the registration
(``serve_rpc.method(..., tier=TIER_CONSOLE)``) and evaluated at the chokepoint
(``call_authorization.authorize_call``) before dispatch, which is where Ruling A
put it. There is deliberately no :func:`call_authorization.service_backstop`
call either: that backstop is for the two verbs that mutate a LEVEL, and opening
a chat mutates a pointer on one persona instance and mints a transcript row.
See :data:`OPEN_CHAT_METHOD` for why the name is NOT in
``LOCAL_CONSOLE_METHODS`` — a paired console device opening a chat on the
machine it is looking at is the feature this plan exists to ship.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

__all__ = [
    "OPEN_CHAT_METHOD",
    "PersonaOpenChatRefusal",
    "PersonaOpenChatOutcome",
    "refusal_codes_by_kind",
    "perform_persona_instance_open_chat",
]


#: The method name, spelled once. The launcher gates its lowering on membership
#: of this exact string in the ``rpc.methods`` manifest set (the D12 pattern
#: ``mission_scope_use_rpc.dart`` already uses for the scope pointers), so a
#: rename here is a wire change and reads as one in a diff.
#:
#: NOT a member of ``call_authorization.LOCAL_CONSOLE_METHODS``, and that
#: absence is the decision rather than an omission. That set is for verbs whose
#: subject is the MACHINE OWNER'S OWN SESSION — the scope pointers a remote
#: credential must not park, the peer directory that is the operator's map of
#: their own network. Opening a chat on the install a paired console device is
#: aimed at is the opposite: it is the gesture the pairing was built for, and
#: ``runtime.chat.message`` (which can run an agent with tools) already sits at
#: the same tier with the same reasoning written out.
OPEN_CHAT_METHOD = "runtime.persona.instance.open_chat"


@dataclass(frozen=True)
class PersonaOpenChatRefusal:
    """A typed refusal, in the shape ``serve_rpc.err`` wants."""

    code: int
    message: str
    data: dict[str, Any]


@dataclass(frozen=True)
class PersonaOpenChatOutcome:
    """Exactly one of the two is set."""

    result: dict[str, Any] | None = None
    refusal: PersonaOpenChatRefusal | None = None


def refusal_codes_by_kind() -> dict[str, int]:
    """``error_kind`` -> JSON-RPC code, resolved against ``serve_rpc``'s numbers.

    Every typed ``error_kind`` this verb's two arms can emit. A row whose kind
    is not here (the untyped ``{"ok": False, "error": …}`` refusals — a missing
    ``session_id``, and the ``--add-instance`` arm's own two, which this door
    cannot reach) falls to ``ERR_INVALID_PARAMS``: an untyped refusal from this
    handler is always the request being wrong rather than the runtime failing.

    A MAP rather than a branch, for the reason the method registry is a dict: a
    test can iterate it, and a kind this verb learns to emit later is a row.
    The import is function-local so this module stays free of the dispatcher —
    the same direction ``scope_activation`` and ``chat_turn`` import their codes
    in, and the reason is the same: a service that imported the dispatcher at
    module scope would make the dispatcher unimportable without the service.
    """

    from .mission_chat_outcome import ChatErrorKind
    from .serve_rpc import ERR_CONFLICT, ERR_HANDLER_FAILED, ERR_INVALID_PARAMS, ERR_NOT_FOUND

    codes = {
        # -- the request was not sayable ------------------------------------
        ChatErrorKind.INVALID_REQUEST: ERR_INVALID_PARAMS,
        # ``PersonaChatMintError`` codes: the new-session arm's replay key.
        # They are the mint's own strings, not ``ChatErrorKind`` members, and
        # they reach the row through ``_emit_persona_open_chat_error``'s
        # ``error_kind=exc.code``.
        "idempotency_key_required": ERR_INVALID_PARAMS,
        "idempotency_key_invalid": ERR_INVALID_PARAMS,
        # -- the thing asked for is not there --------------------------------
        ChatErrorKind.PERSONA_INSTANCE_NOT_FOUND: ERR_NOT_FOUND,
        ChatErrorKind.UNKNOWN_CHAT_SESSION: ERR_NOT_FOUND,
        # -- it is there and it is somebody else's ---------------------------
        ChatErrorKind.PERSONA_INSTANCE_MISMATCH: ERR_CONFLICT,
        ChatErrorKind.FOREIGN_CHAT_SESSION: ERR_CONFLICT,
        ChatErrorKind.RETIRED_PERSONA_INSTANCE: ERR_CONFLICT,
        "mint_lock_unavailable": ERR_CONFLICT,
        # -- the runtime could not do it -------------------------------------
        ChatErrorKind.CHAT_SESSION_PERSIST_FAILED: ERR_HANDLER_FAILED,
        ChatErrorKind.CHAT_SESSION_DB_UNAVAILABLE: ERR_HANDLER_FAILED,
    }
    # Plain strings out. ``ChatErrorKind`` is a ``StrEnum`` so a member already
    # hashes and compares as its value, but the map is READ against a row's raw
    # ``error_kind`` text and is iterated by tests — a mixed-type key set would
    # be a shape nobody decided on.
    return {str(kind): code for kind, code in codes.items()}


def _text(params: dict, *names: str) -> str | None:
    """First non-blank string among ``names``, stripped. ``None`` otherwise."""

    for name in names:
        value = params.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _caller_device_id(caller: Any | None) -> str | None:
    """The device id, and ONLY for a caller the transport proved is a device.

    Read off the :class:`call_authorization.RpcCaller` the dispatcher built,
    never off ``params`` — a request that could name its own requester would be
    a request that authorizes its own provenance. Non-device callers have no id
    here by construction, and get the CLI's own ``"cli"`` default so the two
    doors keep answering with the same word.
    """

    from .call_authorization import CALLER_DEVICE

    if caller is None or getattr(caller, "kind", None) != CALLER_DEVICE:
        return None
    device_id = getattr(caller, "device_id", None)
    return device_id.strip() if isinstance(device_id, str) and device_id.strip() else None


def perform_persona_instance_open_chat(
    params: dict, *, caller: Any | None = None
) -> PersonaOpenChatOutcome:
    """The method lane's door onto ``harness persona instance open-chat``.

    Params: ``persona_id`` (required); ``persona_instance_id``, ``session_id``,
    ``new_session``, ``kill_active``, ``idempotency_key`` (alias
    ``client_message_id``), ``requested_by``, ``correlation_id`` (all optional).

    Result: the CLI's own success row, verbatim — including the two keys the
    launcher's bridge reads (``session_id`` and ``persona_instance_id``), plus
    ``correlation_id`` when the caller sent one.

    Refusals are the CLI's own rows, translated: the row's ``error`` becomes the
    JSON-RPC message, its ``error_kind`` becomes ``data.reason`` and picks the
    code out of :data:`REFUSAL_CODES_BY_KIND`, and every other key on the row
    rides ``data`` unchanged — ``next_expected`` in particular, which is the
    field an operator-facing client renders and which no RPC vocabulary of our
    own would have carried.

    **Idempotency is the CLI's, not a second one.** ``--new-session`` is already
    keyed by ``persona_chat_mints.reserve_persona_chat_mint``: a replay with the
    same key answers the SAME root with ``idempotent_replay: true`` rather than
    minting a second conversation, and a missing key is that module's own typed
    refusal. Nothing here adds a reservation on top — a second key for one fact
    is the drift this plan exists to end (R-C2, one verb over).

    **What this door deliberately cannot reach.** ``--add-instance`` and its
    ``placement_id`` / ``display_name`` / ``workspace_id`` / ``realm_id``
    companions, and the coordinator-budget arm that only ``--add-instance`` or
    ``--kill-active`` with a ``coordinator:`` requester can open. MINTING a
    placement over the method lane is ``runtime.agent.create``'s job and it does
    the whole of it (roster row, chat root and placement together, with a
    reservation); a second minting door here would be exactly the two-lanes-one-
    fact shape ``runtime.agent.retire`` was built to close. So those flags are
    pinned ``False``/``None`` below rather than read off ``params``, and a
    client that sends them is not refused — it is answered by a verb that never
    looked at them.
    """

    from .serde import to_jsonable
    from .serve_rpc import ERR_INVALID_PARAMS

    if not isinstance(params, dict):
        params = {}

    persona_id = _text(params, "persona_id")
    if not persona_id:
        return PersonaOpenChatOutcome(
            refusal=PersonaOpenChatRefusal(
                code=ERR_INVALID_PARAMS,
                message="persona_id is required",
                data={"reason": "persona_id_required"},
            )
        )

    payloads: list[dict] = []
    args = SimpleNamespace(
        persona_id=persona_id,
        persona_instance_id=_text(params, "persona_instance_id"),
        session_id=_text(params, "session_id"),
        new_session=bool(params.get("new_session")),
        kill_active=bool(params.get("kill_active")),
        # The launcher spells the replay key ``idempotency_key`` (its
        # ``ArgRef.idempotencyKey`` rides ``--idempotency-key`` on this verb),
        # and ``client_message_id`` is the name the same value carries on the
        # SEND lane and on ``persona instance create``. Both are accepted, in
        # that order, so one client vocabulary reaches one CLI flag.
        idempotency_key=_text(params, "idempotency_key", "client_message_id"),
        # Provenance, defaulted from what the TRANSPORT proved. ``"cli"`` is the
        # argparse default this verb has always had, kept for a caller with no
        # device identity so the two doors do not answer with different words.
        requested_by=(
            _text(params, "requested_by") or _caller_device_id(caller) or "cli"
        ),
        # The minting arm, closed. See the docstring.
        add_instance=False,
        placement_id=None,
        display_name=None,
        workspace_id=None,
        realm_id=None,
        # The coordinator-budget arm reads these through ``getattr`` and is
        # unreachable with ``add_instance``/``kill_active`` off anyway; they are
        # spelled so no arm of the handler ever sees a missing attribute.
        coordinator_id=None,
        coordinator_max_spawns=None,
        coordinator_spawns_used=0,
        coordinator_may_kill_own=None,
        coordinator_no_kill_own=None,
        coordinator_may_kill_others=None,
        # ``json`` is what the handler would print WITHOUT a sink; with one it
        # prints nothing at all. It is set true so a sink that somehow went
        # missing degrades to a JSON line rather than to prose.
        json=True,
        payload_sink=payloads.append,
    )

    from hermes_cli import harness as _harness

    exit_code = _harness._cmd_persona_instance_open_chat(args)
    row = payloads[-1] if payloads else None
    if not isinstance(row, dict):
        # Unreachable while every arm goes through the seam, and asserted rather
        # than assumed: a handler that grew an exit with no payload would
        # otherwise answer this lane with a silent success.
        from .serve_rpc import ERR_HANDLER_FAILED

        return PersonaOpenChatOutcome(
            refusal=PersonaOpenChatRefusal(
                code=ERR_HANDLER_FAILED,
                message="the open-chat handler exited without a payload",
                data={"reason": "open_chat_payload_missing", "exit_code": exit_code},
            )
        )

    row = dict(to_jsonable(row))
    correlation_id = _text(params, "correlation_id")

    if exit_code == 0 and row.get("ok") is True:
        if correlation_id is not None:
            row["correlation_id"] = correlation_id
        return PersonaOpenChatOutcome(result=row)

    reason = _text(row, "error_kind") or "open_chat_refused"
    data = {key: value for key, value in row.items() if key not in {"ok", "error"}}
    data["reason"] = reason
    data["exit_code"] = exit_code
    if correlation_id is not None:
        data["correlation_id"] = correlation_id
    message = _text(row, "error", "status") or f"open-chat refused: {reason}"
    return PersonaOpenChatOutcome(
        refusal=PersonaOpenChatRefusal(
            code=refusal_codes_by_kind().get(reason, ERR_INVALID_PARAMS),
            message=message,
            data=data,
        )
    )
