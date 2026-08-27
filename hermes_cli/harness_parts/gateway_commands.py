"""The operator's door onto the gateway's credentials (Stage 1 devices, Stage 6 peers).

Seven verbs — ``harness gateway pair``, ``devices list``, ``devices revoke
<device_id>``, and Stage 6's ``peers pair`` / ``peers join`` / ``peers list`` /
``peers revoke <peer_install_id>`` — sitting beside Stage 0b's ``id`` /
``rename`` in the same subtree, because they answer questions about THIS
machine's runtime root rather than about anything in the store.

They hold no rule of their own. What a code may be, how long it lives, how many
may be outstanding, what a lockout is, and when a device is refused are all
decided in :mod:`agent_runtime.serve_gateway_auth`, because the serve process
enforces the same rules over the wire and two answers to "is this code still
good" is the whole failure this lane exists to prevent.

R3, as ruled: **QR plus typed-code fallback, CLI-first.** So one run prints
both — the eight characters an operator reads onto a phone by hand with the
``host:port`` they need beside them, and the JSON payload a phone scans:
``{host, port, install_id, cert_fingerprint, code}``. Same code, same endpoint,
one mint. The launcher's own pairing screen arrives with the Stage 4/5 UI and
will call the same store.

**The code is a short-TTL channel, and stdout is the channel.** It is printed
once, to the operator who asked, and it is never logged, never written to the
store in the clear, and never recoverable — a lost code is re-minted, which is
the intended failure mode and the reason the TTL can be short.

No authorization gate, and — as in Stage 0b — that is written down rather than
left absent. These verbs have ONE door: there is no ``gateway.*`` RPC method for
them, so there is no wire twin that could answer differently, which is the exact
condition the A4 mirror exists to prevent. The caller is the operator at the
install's own shell, and under Ruling A that operator IS the account-auth trace
the device tier descends from: a `CLI_CONSOLE` check here would gate a door
against a predicate that allows every caller that can reach it. When a paired
DEVICE may pair another device, the door is a `gateway.*` method with a tier
declaration and the gate goes there, where the caller is something the transport
proved.

Stage 6 and "agents can never mint peers" (R5) — what the CLI-only shape buys
------------------------------------------------------------------------------

R5 is ADOPTED (primary plan §5): every install⇄install edge is explicitly
approved on BOTH sides, and agents can never initiate pairing. The four peer
verbs are the operator's half of that, and their being CLI verbs is what
enforces the second clause **against remote callers, structurally**:

* there is no ``gateway.*`` RPC method for any of them, so the method lane has
  nothing to call — a peer or device holding any tier finds no name;
* and the argv lane, which is where a CLI verb would otherwise be reachable
  over the wire, is REFUSED outright to every gateway connection (Stage 1,
  ``serve.py``'s ``argv_lane_unavailable``). So "send the verb as argv" is not
  a second door standing beside a missing method; it is a door that answers one
  typed error.

Together those close the remote half completely: no caller on the gateway
listener, at any tier, on either lane, can mint a peer anywhere.

**The residual, named rather than claimed closed.** A LOCAL agent with shell
access on this machine can run these verbs — exactly as it can run ``harness
gateway pair``, read ``serve_auth_token``, or open ``peers.json`` in an editor.
Every tool-using agent on an install already holds the machine owner's
authority; that is what ``CALLER_STDIO_OWNER``'s docstring says, and it is no
less true here. So the accurate claim is: *no agent on install A can cause
install B to trust it* — because minting a code on A does nothing until a human
at B types it into ``peers join`` — *and no remote caller of any tier can mint a
peer anywhere.* An agent that has already taken over the machine its operator is
sitting at was never a case this ceremony could fix, and writing "agents cannot
mint peers" without that sentence beside it would put a false claim in the file
an auditor would read first.
"""

from __future__ import annotations

import json
from typing import Any

from agent_runtime.root_observability import attach_root_observability

from hermes_cli.harness_support import (
    _list_envelope,
    _object_envelope,
    _print_stage42,
    emit_harness_error,
)

__all__ = [
    "cmd_gateway_pair",
    "cmd_gateway_devices_list",
    "cmd_gateway_devices_revoke",
    "cmd_gateway_peers_pair",
    "cmd_gateway_peers_join",
    "cmd_gateway_peers_list",
    "cmd_gateway_peers_revoke",
]


#: ``StoreRefusal.reason`` → the harness error taxonomy, split on the operator's
#: next MOVE, which is what the exit families mean:
#:
#: * ``too_many_pending`` / ``locked_out`` — the store is telling you to wait,
#:   for a code to expire or a lockout to lapse. Family 6 (precondition), not a
#:   fault: nothing is broken and the identical command succeeds later.
#: * ``invalid_tier`` / ``invalid_device_id`` / ``invalid_code`` — the argument
#:   was wrong (2).
#: * ``unknown_device`` — nothing to act on (3).
#: * every I/O condition on the root — retryable in the sense family 7 already
#:   means (an AV hold releases, a permission is fixed, the identical call then
#:   succeeds).
_REFUSAL_CODES = {
    "too_many_pending": "pairing_codes_pending",
    "locked_out": "pairing_locked_out",
    "invalid_tier": "invalid_payload",
    "invalid_device_id": "invalid_payload",
    "invalid_code": "invalid_payload",
    "unknown_device": "not_found",
    "store_corrupt": "store_corrupt",
    # Stage 6's peer refusals, split on the same rule — the operator's next
    # MOVE. A malformed install id or a secret the remote never returned is a
    # bad argument or a bad exchange (2); an install nobody paired is nothing to
    # act on (3). The shared refusals above (`locked_out`, `too_many_pending`,
    # every I/O condition) are shared because the store is shared: one
    # `pairing.json`, one lockout, one cap across both ceremonies.
    "invalid_peer_id": "invalid_payload",
    "invalid_secret": "invalid_payload",
    "unknown_peer": "not_found",
}


def _refusal(refusal: Any, *, args) -> int:
    code = _REFUSAL_CODES.get(refusal.reason, "runtime_unavailable")
    return emit_harness_error(
        RuntimeError(refusal.reason),
        args=args,
        code=code,
        message=refusal.detail or refusal.reason,
    )


def _endpoint(store_root) -> dict[str, Any]:
    """Where a phone should dial, and HOW CONFIDENT this answer is.

    Three sources, and the block says which one answered, because they are not
    equally good and an operator reading a port needs to know whether it is the
    one a listener is actually on:

    * ``live`` — the running serve's ownership sidecar. Authoritative, and the
      only source that can name an EPHEMERAL port, which exists nowhere else.
    * ``config`` — ``remote_gateway.*``. What the next boot will use. Correct
      whenever the port is pinned, and silent about whether anything is
      listening.
    * ``unknown`` — the lane is off, or configured and not yet started.

    Deliberately not an error. Pairing before the first boot is a legitimate
    thing to do (``gateway rename`` already supports it), and refusing to mint a
    code because a serve is not up would make the ceremony depend on an ordering
    nobody chose.
    """

    from agent_runtime.serve_socket import read_socket_owner

    from hermes_cli.harness_parts.serve import gateway_listen_config

    try:
        owner = read_socket_owner(store_root) or {}
    except Exception:
        owner = {}
    live = owner.get("gateway") if isinstance(owner, dict) else None
    if isinstance(live, dict) and live.get("port"):
        return {
            "host": live.get("host"),
            "port": int(live["port"]),
            "source": "live",
        }
    host, port = gateway_listen_config()
    if host is None:
        return {"host": None, "port": None, "source": "unknown"}
    return {"host": host, "port": port or None, "source": "config"}


def cmd_gateway_pair(args) -> int:
    """``harness gateway pair`` — mint a short-TTL code and the QR payload.

    Both halves of R3 from one mint, which is the point: a code shown as text
    and a code shown as a QR that disagree would be two ceremonies wearing one
    name.
    """

    from agent_runtime import paths
    from agent_runtime.gateway_identity import ensure_install_identity
    from agent_runtime.gateway_tls import ensure_certificate
    from agent_runtime.serve_gateway_auth import StoreRefusal, mint_pairing_code

    root = paths.store_root()
    tier = str(getattr(args, "tier", None) or "console")
    name = getattr(args, "name", None)

    # ENSURE, not read, and for both of these. `pair` is a write verb — it is
    # already minting a credential channel — and an operator must be able to run
    # it against a root that has never booted, which `gateway rename` already
    # established. The identity and the certificate are exactly what the payload
    # has to name, so a `pair` that could not produce them would print a payload
    # with holes in it.
    identity = ensure_install_identity(root)
    certificate = ensure_certificate(
        root, common_name=identity.display_name if identity.ok else None
    )
    if not certificate.ok:
        # R1 ruled encrypt, so a payload without a fingerprint is a payload that
        # tells a phone to trust any certificate it is handed. Refused rather
        # than printed with a null.
        return emit_harness_error(
            RuntimeError(certificate.state),
            args=args,
            code="runtime_unavailable",
            message=(
                f"{certificate.cert_path}: the gateway certificate is "
                f"{certificate.state}, so there is no fingerprint for a device "
                "to pin. Pairing without one would tell the device to trust "
                "whatever certificate answers."
            ),
        )

    code = mint_pairing_code(root, tier=tier, name=name)
    if isinstance(code, StoreRefusal):
        return _refusal(code, args=args)

    endpoint = _endpoint(root)
    payload = {
        "host": endpoint["host"],
        "port": endpoint["port"],
        "install_id": identity.install_id,
        "cert_fingerprint": certificate.fingerprint,
        "code": code.code,
    }
    row = {
        # The typed fallback: eight characters, and the endpoint beside them so
        # an operator typing by hand has everything on one screen.
        "code": code.code,
        "expires_in_seconds": code.expires_in_seconds(),
        "tier": code.tier,
        "device_name": code.name,
        "install_id": identity.install_id,
        "cert_fingerprint": certificate.fingerprint,
        "endpoint": endpoint,
        # The QR half. A STRING, not a nested object: what a QR encodes is bytes,
        # and handing the operator the exact bytes to encode removes the chance
        # that two renderers serialise the same object differently and only one
        # of them scans.
        "qr_payload": json.dumps(payload, separators=(",", ":"), sort_keys=True),
    }
    if endpoint["source"] != "live":
        # Stated, never silent: a code minted against a lane nobody is listening
        # on is still a valid code, and an operator who does not know that will
        # blame the code.
        row["note"] = (
            "no running serve advertised a gateway listener for this root, so "
            "the endpoint above is what the config says the NEXT boot will use. "
            "The code is valid either way."
            if endpoint["source"] == "config"
            else "remote_gateway.listen is off for this root: nothing will "
            "accept this code until an interface is configured and the runtime "
            "restarts. The code is valid either way."
        )

    envelope = attach_root_observability(_object_envelope("gateway_pairing", row))
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def cmd_gateway_devices_list(args) -> int:
    """``harness gateway devices list`` — every paired device, revoked included.

    Revoked rows are SHOWN. A list that hid them would make "never paired" and
    "thrown out" the same answer, and the second is the one an operator auditing
    a lost phone needs.
    """

    from agent_runtime import paths
    from agent_runtime.serve_gateway_auth import list_devices

    rows = [record.payload() for record in list_devices(paths.store_root())]
    envelope = attach_root_observability(_list_envelope("gateway_device", rows))
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def cmd_gateway_devices_revoke(args) -> int:
    """``harness gateway devices revoke <device_id>`` — refuse it from now on.

    Takes effect on the NEXT handshake, not on connections already open, and
    that is stated in the ack rather than left for an operator to discover: a
    revocation someone believes is immediate, applied to a device that is
    currently attached, is the gap between what they think they did and what
    happened. Closing an in-flight session is a separate decision — the operator
    can drain the runtime — and inventing it here would make revoke a verb that
    disconnects people.
    """

    from agent_runtime import paths
    from agent_runtime.serve_gateway_auth import StoreRefusal, revoke_device

    device_id = str(getattr(args, "device_id", "") or "").strip()
    if bool(getattr(args, "dry_run", False)):
        from agent_runtime.serve_gateway_auth import lookup_device

        record = lookup_device(paths.store_root(), device_id)
        if record is None:
            return emit_harness_error(
                RuntimeError("unknown_device"),
                args=args,
                code="not_found",
                message=f"no device {device_id!r} is paired with this root",
            )
        row = record.payload()
        # What the WRITE would land, not what is there now.
        row["revoked"] = True
        envelope = attach_root_observability(_object_envelope("gateway_device", row))
        envelope["dry_run"] = True
        _print_stage42(envelope, args=args, default_output="json")
        return 0

    outcome = revoke_device(paths.store_root(), device_id)
    if isinstance(outcome, StoreRefusal):
        return _refusal(outcome, args=args)
    row = outcome.payload()
    row["takes_effect"] = "next_handshake"
    envelope = attach_root_observability(_object_envelope("gateway_device", row))
    _print_stage42(envelope, args=args, default_output="json")
    return 0


# ── Stage 6: peers ───────────────────────────────────────────────────────────


def _install_and_certificate(args):
    """This root's install identity and gateway certificate, both ENSURED.

    Shared by ``peers pair`` and ``peers join`` because both need the same two
    facts about THIS install and for the same reason ``pair`` needs them: a peer
    edge is symmetric, so each side has to be nameable and dialable by the
    other, and a payload with a hole in it is a payload that tells the far side
    to trust whatever certificate answers.

    Returns ``(pair, 0)`` or ``(None, exit_code)`` — the error branch is already
    rendered when it returns, so callers propagate the int.
    """

    from agent_runtime import paths
    from agent_runtime.gateway_identity import ensure_install_identity
    from agent_runtime.gateway_tls import ensure_certificate

    root = paths.store_root()
    identity = ensure_install_identity(root)
    if not identity.ok or not identity.install_id:
        return None, emit_harness_error(
            RuntimeError(identity.state),
            args=args,
            code="runtime_unavailable",
            message=(
                f"{identity.path}: this root's install identity is "
                f"{identity.state}, so it has no id to name itself to a peer. "
                "A peer edge is keyed by install id on both sides; there is "
                "nothing to pair without one."
            ),
        )
    certificate = ensure_certificate(root, common_name=identity.display_name)
    if not certificate.ok:
        return None, emit_harness_error(
            RuntimeError(certificate.state),
            args=args,
            code="runtime_unavailable",
            message=(
                f"{certificate.cert_path}: the gateway certificate is "
                f"{certificate.state}, so there is no fingerprint for the other "
                "install to pin. Pairing without one would tell it to trust "
                "whatever certificate answers."
            ),
        )
    return (identity, certificate), 0


def _self_endpoints(store_root) -> list[dict]:
    """Where the OTHER install should dial this one, as a peer row's list.

    Built from :func:`_endpoint` — the same three sources, the same confidence
    ordering — and reduced to the shape ``gateway_peers.clean_endpoints`` keeps.
    Empty when this root has no address to offer, which is a real state and not
    an error: an install that has never opened its gateway listener can still
    JOIN another install and talk to it. The edge simply works in one direction
    until it listens, and the ack says so rather than leaving the operator to
    find out when a call from the far side never arrives.
    """

    endpoint = _endpoint(store_root)
    host, port = endpoint.get("host"), endpoint.get("port")
    if not host or not port:
        return []
    if str(host) in {"0.0.0.0", "::"}:
        # A wildcard bind is what this install LISTENS on, never an address
        # another machine can dial. Passing it through would write a row whose
        # every dial fails, and fails in a way that looks like the peer being
        # down. The operator has to name a reachable address themselves.
        return []
    return [{"host": str(host), "port": int(port)}]


def cmd_gateway_peers_pair(args) -> int:
    """``harness gateway peers pair`` — mint a PEER code plus the join payload.

    Run on install A. The operator carries the payload (or the eight characters)
    to install B and runs ``peers join`` there. Nothing is written to
    ``peers.json`` by this verb: a code is an invitation, and an install that
    never redeems it leaves no row behind.

    R3's two halves from one mint, exactly as ``gateway pair`` does it — and the
    payload's code field is ``peer_code`` rather than ``code``, so a device
    payload pasted into ``peers join`` (or the reverse) is refused for its shape
    rather than half-parsed into the wrong ceremony.
    """

    from agent_runtime import paths
    from agent_runtime.gateway_peers import mint_peer_code
    from agent_runtime.serve_gateway_auth import StoreRefusal

    root = paths.store_root()
    resolved, code_or_error = _install_and_certificate(args)
    if resolved is None:
        return code_or_error
    identity, certificate = resolved

    minted = mint_peer_code(root, note=getattr(args, "note", None))
    if isinstance(minted, StoreRefusal):
        return _refusal(minted, args=args)

    endpoint = _endpoint(root)
    payload = {
        "host": endpoint["host"],
        "port": endpoint["port"],
        "install_id": identity.install_id,
        "cert_fingerprint": certificate.fingerprint,
        "peer_code": minted.code,
    }
    row = {
        "peer_code": minted.code,
        "expires_in_seconds": minted.expires_in_seconds(),
        "note": minted.note,
        "install_id": identity.install_id,
        "display_name": identity.display_name,
        "cert_fingerprint": certificate.fingerprint,
        "endpoint": endpoint,
        # A STRING, not a nested object, for ``gateway pair``'s reason: what a QR
        # encodes is bytes, and handing the operator the exact bytes removes the
        # chance that two renderers serialise the same object differently and
        # only one of them scans.
        "join_payload": json.dumps(payload, separators=(",", ":"), sort_keys=True),
        # Said out loud on the mint, because this is the half an operator can get
        # wrong silently: a code that is never carried to the other machine pairs
        # nothing, and R5 is the reason there is no way around that.
        "next_step": (
            "run `harness gateway peers join <join_payload>` on the OTHER "
            "install. Both sides approve an edge; nothing here can pair on its "
            "own."
        ),
    }
    if endpoint["source"] != "live":
        row["note_endpoint"] = (
            "no running serve advertised a gateway listener for this root, so "
            "the endpoint in the payload is what the config says the NEXT boot "
            "will use. The joining install dials it — if nothing is listening "
            "there when they run `join`, the code is still valid and the join "
            "will simply fail to connect."
            if endpoint["source"] == "config"
            else "remote_gateway.listen is off for this root: nothing will "
            "accept this code until an interface is configured and the runtime "
            "restarts. The code is valid either way."
        )

    envelope = attach_root_observability(_object_envelope("gateway_peer_pairing", row))
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def _parse_join_payload(raw: Any, args) -> dict[str, Any] | None:
    """The join payload as fields, or ``None`` with the refusal already printed.

    Accepts BOTH halves of R3: the JSON blob a QR carries, and a bare
    eight-character code with ``--host`` / ``--port`` / ``--fingerprint``
    supplied beside it. One parser, so the typed and the scanned paths cannot
    disagree about what a payload means.
    """

    text = str(raw or "").strip()
    fields: dict[str, Any] = {}
    if text.startswith("{"):
        try:
            decoded = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            emit_harness_error(
                exc,
                args=args,
                code="invalid_payload",
                message=f"the join payload is not JSON: {exc}",
            )
            return None
        if not isinstance(decoded, dict):
            emit_harness_error(
                RuntimeError("payload_not_an_object"),
                args=args,
                code="invalid_payload",
                message="the join payload must be a JSON object",
            )
            return None
        fields = decoded
    else:
        fields = {"peer_code": text}

    # Flags OVERRIDE the payload rather than filling in behind it. An operator
    # who typed --host did so because the payload's address is wrong for their
    # network (a second interface, a NAT, a machine that moved), and a merge that
    # preferred the payload would silently ignore the correction.
    for flag, key in (
        ("host", "host"),
        ("port", "port"),
        ("fingerprint", "cert_fingerprint"),
    ):
        value = getattr(args, flag, None)
        if value:
            fields[key] = value

    code = str(fields.get("peer_code") or "").strip().upper()
    host = str(fields.get("host") or "").strip()
    try:
        port = int(fields.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    missing = [
        name
        for name, value in (("peer_code", code), ("host", host), ("port", port))
        if not value
    ]
    if missing:
        emit_harness_error(
            RuntimeError("incomplete_payload"),
            args=args,
            code="invalid_payload",
            message=(
                f"the join payload is missing {', '.join(missing)}. Paste the "
                "join_payload string from `harness gateway peers pair` on the "
                "other install, or pass the code with --host/--port. A payload "
                "carrying `code` rather than `peer_code` is a DEVICE pairing "
                "payload and cannot be joined as a peer."
            ),
        )
        return None
    return {
        "peer_code": code,
        "host": host,
        "port": port,
        "install_id": str(fields.get("install_id") or "").strip() or None,
        "cert_fingerprint": str(fields.get("cert_fingerprint") or "").strip() or None,
    }


def cmd_gateway_peers_join(args) -> int:
    """``harness gateway peers join <payload|code>`` — redeem, and record BOTH halves.

    Run on install B, the second of R5's two operators. It dials install A's
    gateway listener with a PEER hello carrying the code and B's own identity, A
    redeems it and writes B's row, and the ``hello_ok`` carries the symmetric
    secret back so B can write A's row. One command, two stores, one edge.

    Three things it refuses rather than papers over:

    * a payload that names an ``install_id`` the far side does not turn out to
      have — that is a different install answering on that address, and
      recording the row anyway would pin a credential to the wrong machine;
    * a ``hello_ok`` with no ``peered`` block — the code did not redeem, and a
      row written on hope is a row whose every dial fails;
    * any dial failure at all, as ``runtime_unavailable`` (family 7, retryable),
      because a listener that is not up yet is exactly the condition where the
      identical command succeeds five seconds later.
    """

    from agent_runtime import paths
    from agent_runtime.gateway_peers import record_peer
    from agent_runtime.serve_gateway_auth import StoreRefusal
    from agent_runtime.serve_socket import ServeSocketClient

    root = paths.store_root()
    parsed = _parse_join_payload(getattr(args, "payload", None), args)
    if parsed is None:
        return 2
    resolved, code_or_error = _install_and_certificate(args)
    if resolved is None:
        return code_or_error
    identity, certificate = resolved

    endpoints = _self_endpoints(root)
    connection = ServeSocketClient(
        parsed["host"],
        parsed["port"],
        timeout_seconds=float(getattr(args, "timeout", None) or 20.0),
        tls=True,
        cert_fingerprint=parsed["cert_fingerprint"],
    )
    try:
        connection.connect()
        reply = connection.peer_join_hello(
            peer_code=parsed["peer_code"],
            peer_install_id=identity.install_id,
            display_name=identity.display_name,
            endpoints=endpoints,
            cert_fingerprint=certificate.fingerprint,
        )
    except Exception as exc:
        return emit_harness_error(
            exc,
            args=args,
            code="runtime_unavailable",
            message=(
                f"{parsed['host']}:{parsed['port']}: could not complete the peer "
                f"handshake ({type(exc).__name__}: {exc}). The other install's "
                "gateway listener must be running, reachable, and presenting the "
                "certificate whose fingerprint is in the payload."
            ),
        )
    finally:
        connection.close()

    if not isinstance(reply, dict) or reply.get("event") != "hello_ok":
        reason = (reply or {}).get("reason") or "no hello_ok"
        return emit_harness_error(
            RuntimeError(str(reason)),
            args=args,
            code="invalid_payload",
            message=(
                f"the other install refused the join ({reason}). Every credential "
                "failure on that lane reports the same reason on purpose, so the "
                "cause is one of: the code expired, it was already redeemed, it "
                "was a DEVICE code, or the install is locked out after repeated "
                "failed attempts. Mint a fresh code with `harness gateway peers "
                "pair` over there."
            ),
        )

    peered = reply.get("peered")
    remote = reply.get("install") if isinstance(reply.get("install"), dict) else {}
    remote_id = str(remote.get("install_id") or "").strip()
    if not isinstance(peered, dict) or not peered.get("peer_secret"):
        return emit_harness_error(
            RuntimeError("no_peer_secret"),
            args=args,
            code="invalid_payload",
            message=(
                "the other install completed a handshake but returned no peer "
                "secret, so this side has no credential to store. That frame is "
                "the only time the secret is ever sent; re-run the ceremony with "
                "a fresh code."
            ),
        )
    if parsed["install_id"] and remote_id and parsed["install_id"] != remote_id:
        return emit_harness_error(
            RuntimeError("install_id_mismatch"),
            args=args,
            code="invalid_payload",
            message=(
                f"the payload names install {parsed['install_id']!r} but "
                f"{parsed['host']}:{parsed['port']} answered as {remote_id!r}. "
                "Something else is on that address; the row was NOT written."
            ),
        )

    outcome = record_peer(
        root,
        peer_install_id=remote_id or parsed["install_id"] or "",
        secret=str(peered["peer_secret"]),
        display_name=remote.get("display_name"),
        endpoints=[{"host": parsed["host"], "port": parsed["port"]}],
        cert_fingerprint=parsed["cert_fingerprint"],
    )
    if isinstance(outcome, StoreRefusal):
        return _refusal(outcome, args=args)

    row = outcome.payload()
    # What the OTHER side now holds about us, so one ack answers "is this edge
    # symmetric" without an operator walking to the other machine to check.
    row["this_install"] = {
        "install_id": identity.install_id,
        "display_name": identity.display_name,
        "endpoints": endpoints,
        "cert_fingerprint": certificate.fingerprint,
    }
    if not endpoints:
        row["note"] = (
            "this root advertised no dialable gateway endpoint, so the other "
            "install recorded the edge with no address for it. Calls from here "
            "to there work; calls from there to here will not until this root's "
            "remote_gateway.listen names a reachable interface and a `peers "
            "join` is re-run."
        )
    envelope = attach_root_observability(_object_envelope("gateway_peer", row))
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def cmd_gateway_peers_list(args) -> int:
    """``harness gateway peers list`` — every paired install, revoked included.

    Revoked rows are SHOWN, for ``devices list``'s reason: a list that hid them
    would make "never paired" and "thrown out" the same answer, and the second is
    the one an operator auditing a decommissioned machine needs. The credential
    has no field here and none on ``PeerRecord`` either.
    """

    from agent_runtime import paths
    from agent_runtime.gateway_peers import list_peers

    rows = [record.payload() for record in list_peers(paths.store_root())]
    envelope = attach_root_observability(_list_envelope("gateway_peer", rows))
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def cmd_gateway_peers_revoke(args) -> int:
    """``harness gateway peers revoke <peer_install_id>`` — refuse it from here on.

    Takes effect on the NEXT handshake, not on connections already open, and —
    the fact that distinguishes this from ``devices revoke`` — it is ONE-SIDED.
    A peer edge holds a credential at both ends, so revoking here stops that
    install from reaching this one and does nothing to the row it keeps about us.
    The ack says so rather than leaving an operator to assume symmetry: a
    revocation that reached across the wire would be one install writing into
    another's credential store, which is the authority R5 says an install never
    has over another.
    """

    from agent_runtime import paths
    from agent_runtime.gateway_peers import lookup_peer, revoke_peer
    from agent_runtime.serve_gateway_auth import StoreRefusal

    peer_install_id = str(getattr(args, "peer_install_id", "") or "").strip()
    if bool(getattr(args, "dry_run", False)):
        record = lookup_peer(paths.store_root(), peer_install_id)
        if record is None:
            return emit_harness_error(
                RuntimeError("unknown_peer"),
                args=args,
                code="not_found",
                message=f"no install {peer_install_id!r} is paired with this root",
            )
        row = record.payload()
        # What the WRITE would land, not what is there now.
        row["revoked"] = True
        envelope = attach_root_observability(_object_envelope("gateway_peer", row))
        envelope["dry_run"] = True
        _print_stage42(envelope, args=args, default_output="json")
        return 0

    outcome = revoke_peer(paths.store_root(), peer_install_id)
    if isinstance(outcome, StoreRefusal):
        return _refusal(outcome, args=args)
    row = outcome.payload()
    row["takes_effect"] = "next_handshake"
    row["scope"] = "this_install_only"
    row["note"] = (
        f"{peer_install_id} can no longer reach this install. Its own store "
        "still holds a row for this one — run `harness gateway peers revoke` "
        "over there to cut the edge in both directions."
    )
    envelope = attach_root_observability(_object_envelope("gateway_peer", row))
    _print_stage42(envelope, args=args, default_output="json")
    return 0
