"""The operator's door onto the gateway's device credentials (Stage 1, R3).

Three verbs — ``harness gateway pair``, ``harness gateway devices list``,
``harness gateway devices revoke <device_id>`` — sitting beside Stage 0b's
``id`` / ``rename`` in the same subtree, because they answer questions about
THIS machine's runtime root rather than about anything in the store.

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
