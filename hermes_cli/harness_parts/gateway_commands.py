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
    "cmd_gateway_introduce",
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
#: * a WRITE this machine could not perform — ``store_unwritable`` (family 1),
#:   R-D14. See :data:`_STORE_WRITE_REASONS` for why these three left family 7.
#: * every remaining I/O condition on the root — retryable in the sense family 7
#:   already means (an AV hold releases, the identical call then succeeds).
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

#: The ``os_error_reason`` words that mean **this machine could not write its own
#: store** — R-D14, and the one thing on this lane that is not about the network.
#:
#: D3 run #1 (2026-09-04, 18:06:20) is why they have their own code. The
#: handshake completed, the far install answered, and ``record_peer`` came back
#: ``permission_denied`` from a ``[WinError 5]`` on ``peers.json``. That fell
#: through this table to ``runtime_unavailable``, the launcher's fulfiller mapped
#: ``runtime_unavailable`` to ``no_route``, and the sheet told the operator the
#: Mac was unreachable — a claim about the network, for a DACL on a local
#: directory, retried once a minute for four minutes to the identical result.
#:
#: ``root_missing`` is deliberately NOT here and keeps family 7. An absent
#: directory is the one condition the writer creates for itself on its next call
#: (``write_secure_json`` mkdirs its parents), so "retry" really is the cure —
#: which is the whole distinction family 1 and family 7 encode.
_STORE_WRITE_REASONS = frozenset(
    {"permission_denied", "unwritable", "root_not_a_directory"}
)
for _reason in _STORE_WRITE_REASONS:
    _REFUSAL_CODES[_reason] = "store_unwritable"
del _reason


def _refusal(refusal: Any, *, args, store_path: Any = None) -> int:
    """One ``StoreRefusal`` as a harness error, with BOTH words on it.

    R-D6. The mapping above is many-to-one on purpose — the family answers "what
    do I do next", and nine I/O conditions genuinely share one next move — but a
    reader who only has the family cannot say what happened. The launcher's
    fulfiller maps ``runtime_unavailable`` to ``no_route``, so S4's 12:00:40
    receipt recorded "no route" for a refusal whose actual reason existed and
    was thrown away one process earlier.

    So ``reason`` rides beside ``code``: the store's own word, unmapped,
    untranslated, and never a substitute for the family an exit code is derived
    from.

    ``store_path`` is R-D14's other half. A write refusal carries an OS message
    like ``[WinError 5] Access is denied: '.peers.json.ntk1yca6.tmp' ->
    'peers.json'`` — two BASENAMES, which name no directory an operator could go
    and fix. This is the one caller that knows which file the verb was writing,
    so it is the one place that can put the absolute path in front of them.
    """

    code = _REFUSAL_CODES.get(refusal.reason, "runtime_unavailable")
    detail = refusal.detail or refusal.reason
    reason = refusal.reason
    message = detail
    if code == "store_unwritable":
        # ONE word on the wire for this condition (R-D14), because the launcher
        # renders it as a sheet sentence of its own — three spellings would be
        # three sentences for one fault. The store's own word is not lost: it
        # rides in the message, beside the OSError text, which is where an
        # operator reads "which failure was it" anyway.
        reason = "store_unwritable"
        where = f" at {store_path}" if store_path is not None else ""
        message = (
            f"could not write this install's own gateway store{where}: {detail} "
            f"({refusal.reason}). Nothing on the other machine is wrong — the "
            "handshake it answered is lost because this side could not record "
            "it. Give the user this process runs as write and delete permission "
            "on that file and the directory holding it, then run the command "
            "again."
        )
    return emit_harness_error(
        RuntimeError(refusal.reason),
        args=args,
        code=code,
        message=message,
        reason=reason,
    )


def _store_write_refusal(exc: OSError, *, args, store_path: Any) -> int:
    """An ``OSError`` that ESCAPED a store call, as the same R-D14 refusal.

    The store functions on this lane catch ``OSError`` around their locked
    read-modify-write and return a ``StoreRefusal``, which :func:`_refusal`
    already classifies. This covers what that ``try`` does not span — the event
    append and the cache touch that ``record_peer`` runs after its lock is
    released, and any future write that acquires a raise on the way out.

    One helper rather than four, and both doors give the identical code, message
    shape and exit: an operator hitting a permission problem must not get two
    different stories depending on which line inside the store it surfaced on.
    """

    from agent_runtime.store_file_io import os_error_reason

    reason = os_error_reason(exc)
    if reason not in _STORE_WRITE_REASONS:
        reason = "unwritable"
    return _refusal(
        _StoreWriteRefusal(reason, str(exc)), args=args, store_path=store_path
    )


class _StoreWriteRefusal:
    """A ``StoreRefusal``-shaped pair, so :func:`_refusal` has one input type.

    Not the real class: importing ``serve_gateway_auth`` here would pull the
    whole device-store module into every verb that only needs two strings, and
    the only contract ``_refusal`` reads is ``.reason`` / ``.detail``.
    """

    __slots__ = ("reason", "detail")

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail


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


#: The one sentence every verb on this lane uses for "nothing is listening".
#: ``peers pair`` printed it as a NOTE and :func:`cmd_gateway_introduce` raises
#: it as a REFUSAL; the words are identical on purpose, because an operator
#: reading a launcher's error and an operator reading a terminal note are
#: looking at the same condition and should not have to recognise two spellings
#: of it.
LISTENER_OFF_SENTENCE = (
    "remote_gateway.listen is off for this root: nothing will accept this code "
    "until an interface is configured and the runtime restarts."
)

#: The OTHER "nowhere to dial", and it is a different condition from the one
#: above rather than a rewording of it: the lane is ON, a listener is up or
#: configured, and the bind names every interface — but interface enumeration
#: came back empty, so this machine cannot say which address the far side should
#: use. R-D1's refusal, and the reason it exists is that the alternative is
#: writing the BIND into the payload, which is what S4's first hardware attempt
#: did: Windows dialled ``0.0.0.0:8765`` and reported ``runtime_unavailable``,
#: which reads exactly like the far install being down.
NO_DIAL_HOST_SENTENCE = (
    "the listener is bound to every interface but this machine offers no "
    "address to dial"
)

#: What :func:`cmd_gateway_introduce` prints, compactly, for a launcher to POST
#: as the backend grant's ``payload``. Declared as a tuple rather than left
#: implicit in a dict literal so the backend contract (S1 packet §4.1) has one
#: name on this side and a test can assert the key set without restating it.
GRANT_PAYLOAD_KEYS = (
    "peer_join_payload",
    "device_pair_payload",
    "install_id",
    "endpoints",
    "cert_fingerprint",
    "correlation",
)

#: The backend's own ceiling on a fulfil payload (S1 §4.1: compact, ≤ 4096
#: bytes). Asserted HERE, at the only place that builds the object, so an
#: envelope that would be rejected on POST is refused on this side with a reason
#: an operator can act on instead of failing as an opaque 400 later.
GRANT_PAYLOAD_MAX_BYTES = 4096


def cmd_gateway_pair(args) -> int:
    """``harness gateway pair`` — mint a short-TTL code and the QR payload.

    Both halves of R3 from one mint, which is the point: a code shown as text
    and a code shown as a QR that disagree would be two ceremonies wearing one
    name.
    """

    from agent_runtime import paths
    from agent_runtime.gateway_identity import ensure_install_identity
    from agent_runtime.gateway_tls import ensure_certificate
    from agent_runtime.serve_gateway_auth import (
        StoreRefusal,
        mint_pairing_code,
        pairing_store_path,
    )

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
            reason=certificate.state,
            args=args,
            code="runtime_unavailable",
            message=(
                f"{certificate.cert_path}: the gateway certificate is "
                f"{certificate.state}, so there is no fingerprint for a device "
                "to pin. Pairing without one would tell the device to trust "
                "whatever certificate answers."
            ),
        )

    # The dial host is decided BEFORE the mint, so a root that cannot say where
    # a phone should dial refuses without having burned one of the three pending
    # codes the operator is allowed.
    endpoint = _endpoint(root)
    dial, endpoints, failure = _dial_target(root, endpoint, args=args)
    if failure:
        return failure

    code = mint_pairing_code(root, tier=tier, name=name)
    if isinstance(code, StoreRefusal):
        # R-D14: a pairing.json this machine cannot write is not the network's
        # fault, and the refusal now says which file to fix.
        return _refusal(code, args=args, store_path=pairing_store_path(root))

    payload = {
        # R-D1: a DIALABLE address, never the bind. ``endpoint`` below still
        # reports the bind, because that is the honest answer to "what is this
        # listener on" — what changed is that the bind stopped being what a
        # phone is told to dial.
        "host": dial[0] if dial else None,
        "port": dial[1] if dial else None,
        # R-D3's list, on the payload a phone scans. ``host``/``port`` remain and
        # equal ``endpoints[0]``, so a scanner that predates this key reads the
        # same first candidate it always did.
        "endpoints": endpoints,
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
            else LISTENER_OFF_SENTENCE + " The code is valid either way."
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
    from agent_runtime.serve_gateway_auth import (
        StoreRefusal,
        device_store_path,
        revoke_device,
    )

    device_id = str(getattr(args, "device_id", "") or "").strip()
    if bool(getattr(args, "dry_run", False)):
        from agent_runtime.serve_gateway_auth import lookup_device

        record = lookup_device(paths.store_root(), device_id)
        if record is None:
            return emit_harness_error(
                RuntimeError("unknown_device"),
                reason="unknown_device",
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
        return _refusal(
            outcome, args=args, store_path=device_store_path(paths.store_root())
        )
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
            reason=identity.state,
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
            reason=certificate.state,
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


#: Addresses this machine offers a peer, capped at ``gateway_peers.MAX_ENDPOINTS``.
#: Not a config knob: the cap is the peer row's, and a list longer than the row
#: can hold would advertise addresses that silently vanish at the far end.
MAX_CANDIDATE_ENDPOINTS = 4

#: Hosts that are never worth offering another machine. Loopback is this box
#: talking to itself, link-local is an address that only means something on the
#: segment that assigned it, and a wildcard is a BIND and not an address at all.
#: Prefixes rather than a netmask calculation, because this is a filter over
#: strings the stdlib handed us, and half an address library here would be a
#: second address model to keep true.
_UNOFFERABLE_PREFIXES = ("127.", "169.254.", "fe80:")
_WILDCARD_HOSTS = {"0.0.0.0", "::", "*", ""}

#: Where the default-route probe *points*. A PUBLIC unicast address, and that is
#: the whole of R-D2's first half: the probe asks the kernel "which of my
#: addresses would carry traffic to the INTERNET", and the answer is only that
#: question if the far address is on the internet. This used to be
#: ``10.255.255.255``, which asks "which address reaches 10/8" — and on a machine
#: with a private 10.x adapter beside the Wi-Fi (a VM host bridge, a corporate
#: virtual NIC) the kernel correctly answers with the 10.x address, so the
#: router-granted LAN address was ranked below an interface with no gateway at
#: all. Measured on the operator's Windows PC 2026-09-04: the LAN address
#: 192.168.1.203 came out THIRD.
#:
#: ``1.1.1.1:53`` resolves nothing and is never contacted — see the connect
#: comment below — so this is a routing-table lookup wearing a socket, not a
#: dependency on that resolver being up or on this machine having a route at all.
_DEFAULT_ROUTE_PROBE = ("1.1.1.1", 53)

#: RFC1918, written out. The filter above is deliberately prefix-based ("half an
#: address library here would be a second address model to keep true") but the
#: ORDER needs real arithmetic — "shares a /24 with the default route" is not a
#: string test — so the ordering asks the stdlib's address library rather than
#: growing a second one out of octet slicing.
_RFC1918_CIDRS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")

#: How long ONE routing-table command (R-D8) may take before it is abandoned.
#: The read runs inside ``gateway id``, which the launcher's sheet calls on a
#: timer, so the failure worth defending against is not a wrong answer — the
#: R-D2 probe still ranks behind it — but a wedged ``route.exe`` holding the
#: sheet. Two seconds is far above the ~30 ms these commands actually cost.
_ROUTE_COMMAND_TIMEOUT_SECONDS = 2.0


def _ipv4(host: str):
    """The host as an ``IPv4Address``, or ``None`` if it is not one.

    ``None`` for every v6 address and for anything that will not parse, because
    both answers mean the same thing to the ranking below: this row is not a
    private-LAN candidate and cannot share a /24 with one.
    """

    import ipaddress

    try:
        address = ipaddress.ip_address(str(host))
    except ValueError:
        return None
    return address if address.version == 4 else None


def _is_rfc1918(host: str) -> bool:
    import ipaddress

    address = _ipv4(host)
    if address is None:
        return False
    return any(
        address in ipaddress.ip_network(cidr) for cidr in _RFC1918_CIDRS
    )


def _shares_24(host: str, other: str) -> bool:
    left, right = _ipv4(host), _ipv4(other)
    if left is None or right is None:
        return False
    return int(left) >> 8 == int(right) >> 8


def _run_route_command(argv: list[str]) -> str | None:
    """One routing-table command's stdout, or ``None``. Never raises.

    Every way this can fail — the binary missing, a non-zero exit, a hang, a
    localised console codepage, a sandbox that forbids spawning at all — means
    the same thing to the caller: *the table did not answer, fall back to the
    R-D2 probe.* So they collapse to one ``None`` rather than a taxonomy nobody
    would branch on. ``check=False`` and the bare ``except`` are the point of
    this function, not a shortcut taken inside it.
    """

    import subprocess
    import sys

    extra: dict[str, Any] = {}
    if sys.platform == "win32":
        # ``route.exe`` is a console program and this CLI is routinely spawned
        # by a windowless launcher process; without this the operator would see
        # a console blink every time the sheet refreshes.
        flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if flag:
            extra["creationflags"] = flag
    try:
        completed = subprocess.run(
            argv,
            # This CLI is spoken to over stdio by a launcher (see
            # ``CALLER_STDIO_OWNER``), so a child that inherited stdin could eat
            # a frame addressed to us. ``route``/``ip``/``ifconfig`` read none.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_ROUTE_COMMAND_TIMEOUT_SECONDS,
            check=False,
            **extra,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout or ""


def _windows_default_route_address(text: str) -> str | None:
    """The ``Interface`` column of ``route print -4``'s true default row.

    The row shape is the anchor, not the ``Active Routes:`` header, which is
    localised on a non-English Windows. Five whitespace-separated fields, the
    first two exactly ``0.0.0.0``, the fourth a v4 address, the fifth an
    integer — which is precisely the Active Routes shape and precisely not the
    Persistent Routes one (four fields, metric ``Default``).

    **The netmask test is the whole of R-D8.** A full-tunnel VPN client like
    PIA installs ``0.0.0.0/1`` + ``128.0.0.0/1``, which cover every address and
    beat ``0.0.0.0/0`` on specificity, so the datagram probe answers with the
    tunnel on a machine whose LAN address is the one a peer can reach. Those
    rows carry netmask ``128.0.0.0`` and are rejected here by string equality;
    only an owner of ``0.0.0.0/0`` is the router-granted address.

    Several default rows (two NICs on one LAN) are decided by the lowest Metric,
    which is the kernel's own tie-break. Any doubt at all — a row that will not
    parse — drops that row rather than guessing at it.
    """

    best: tuple[int, str] | None = None
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 5:
            continue
        destination, netmask, _gateway, interface, metric = fields
        if destination != "0.0.0.0" or netmask != "0.0.0.0":
            continue
        if _ipv4(interface) is None or interface in _WILDCARD_HOSTS:
            continue
        try:
            cost = int(metric)
        except ValueError:
            continue
        if best is None or cost < best[0]:
            best = (cost, interface)
    return None if best is None else best[1]


def _macos_default_route_interface(text: str) -> str | None:
    """The ``interface:`` line of ``route -n get default`` — a NAME, not an
    address, which is why macOS needs the second command below."""

    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == "interface":
            return value.strip() or None
    return None


def _first_inet_address(text: str) -> str | None:
    """The first ``inet <address>`` of an ``ifconfig``/``ip addr`` block.

    One reader for both platforms: macOS writes ``inet 192.168.1.5 netmask
    0xffffff00`` and Linux writes ``inet 192.168.1.42/24 brd …``, and the only
    difference that matters is the prefix length, stripped here. ``inet6`` does
    not match the token. A first row that will not parse as v4 answers ``None``
    rather than skipping to a later one — R-D8's rule is "any parse doubt →
    ``None``", and a second address on that interface is a different address
    than the one the table named.
    """

    for line in text.splitlines():
        fields = line.split()
        for index, field in enumerate(fields[:-1]):
            if field != "inet":
                continue
            candidate = fields[index + 1].split("/", 1)[0]
            return candidate if _ipv4(candidate) else None
    return None


def _linux_default_route(text: str) -> tuple[str | None, str | None]:
    """``(src, dev)`` from the first ``default`` line of ``ip -4 route show
    default``.

    ``src`` is the address the kernel will put on packets leaving by that route
    and is therefore the answer outright when present; ``dev`` is the fallback
    the caller turns into an address with a second command. ``ip`` prints
    default routes in metric order, so the first line is the lowest-cost one —
    the same tie-break the Windows reader does arithmetic for.
    """

    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] != "default":
            continue
        source: str | None = None
        device: str | None = None
        for index, field in enumerate(fields[:-1]):
            if field == "src" and source is None:
                source = fields[index + 1]
            elif field == "dev" and device is None:
                device = fields[index + 1]
        if source is not None and _ipv4(source) is None:
            source = None
        return source, device
    return None, None


def _default_route_address() -> str | None:
    """R-D8: the address that owns ``0.0.0.0/0``, read from the routing table.

    The operator's sentence — *"the exact router-granted address"* — made
    mechanical, and the correction D1's field notes earned: the datagram probe
    of R-D2 asks "which of my addresses reaches the internet", and with a
    full-tunnel VPN up the honest answer is the tunnel's. The routing TABLE can
    still be asked the different question "who owns the default route", and on
    the machine that motivated this it answers Wi-Fi while every probe
    destination answers PIA.

    Stdlib subprocess, one command per platform (two on macOS and on a Linux
    route without ``src``), each bounded by
    :data:`_ROUTE_COMMAND_TIMEOUT_SECONDS`. ``None`` on any doubt whatsoever,
    which the caller reads as "rank by R-D2 alone" — this ruling only ever
    promotes an address ahead of the probe's, it never removes one, so a
    silent failure here costs the pre-D1b ordering and nothing more.
    """

    import sys

    if sys.platform == "win32":
        printed = _run_route_command(["route", "print", "-4"])
        return _windows_default_route_address(printed) if printed else None

    if sys.platform == "darwin":
        printed = _run_route_command(["route", "-n", "get", "default"])
        interface = _macos_default_route_interface(printed) if printed else None
        if not interface:
            return None
        printed = _run_route_command(["ifconfig", interface])
        return _first_inet_address(printed) if printed else None

    printed = _run_route_command(["ip", "-4", "route", "show", "default"])
    if not printed:
        return None
    source, device = _linux_default_route(printed)
    if source:
        return source
    if not device:
        return None
    printed = _run_route_command(["ip", "-4", "-o", "addr", "show", "dev", device])
    return _first_inet_address(printed) if printed else None


def _address_rank(
    host: str, default_route: str | None, table_route: str | None = None
) -> int:
    """R-D2's order with R-D8 in front of it, as a sort key. Lower dials first.

    0. **The routing table's owner of ``0.0.0.0/0``** (R-D8). The one source
       that can tell a LAN apart from a full-tunnel VPN, because the VPN's
       ``/1`` pair is what the probe below follows.
    1. **The probe's answer** — the address traffic actually leaves from. First
       until D1b, and still first whenever the table declines to answer.
    2. **RFC1918 addresses on the default route's own /24.** A second address on
       the same segment is the next best guess when the first is filtered by a
       firewall rule that does not cover the whole subnet.
    3. **Other RFC1918.** A private address somewhere, which is what a LAN peer
       is most likely to be able to reach.
    4. **Everything else v4** — a public address, and the Hamachi/Tailscale-class
       overlays that hand out addresses outside 1918. Sorted here BY RULE and
       not filtered by adapter name (R-D2): a machine whose only address is one
       of those still gets offered, because refusing to offer it would make an
       overlay-only machine unpairable in the name of tidiness.
    5. **Global v6**, last: a v6 address that works is excellent and a v6
       address that does not is a dial that hangs before the v4 one is tried.

    The /24 arithmetic follows whichever of the two sources came first, so on a
    VPN'd machine "the default route's own subnet" means the LAN's subnet and
    not the tunnel's — the ranks below 2 would otherwise contradict rank 0.
    """

    if table_route and host == table_route:
        return 0
    if default_route and host == default_route:
        return 1
    if ":" in host:
        return 5
    primary = table_route or default_route
    if _is_rfc1918(host):
        return 2 if primary and _shares_24(host, primary) else 3
    return 4


def _machine_addresses() -> list[str]:
    """This machine's dialable addresses, best-effort, stdlib only, IN DIAL ORDER.

    Called ONLY when the listener bound a wildcard — the case where the config
    says "every interface" and therefore names none. Three sources, deduped:

    0. **The routing table's owner of ``0.0.0.0/0``**
       (:func:`_default_route_address`, R-D8), which is the only source that
       tells a LAN apart from a full-tunnel VPN. Silent when the table declines,
       and then the two below are exactly what D1 shipped.
    1. **The default-route probe**, using the UDP-connect trick: a
       ``SOCK_DGRAM`` socket is *connected* to :data:`_DEFAULT_ROUTE_PROBE` and
       asked what local address the kernel would use. **No packet is sent** —
       connect on a datagram socket only fixes the peer — so this costs no
       traffic, needs no reachability, and answers with the cable unplugged.
    2. **The hostname's records**, v4 then v6, which is what the machine calls
       itself and is usually right on a LAN with mDNS or a DHCP-registering DNS.

    What S4's first hardware attempt changed is that DISCOVERY ORDER is no
    longer OFFER ORDER. The two sources answer "which addresses exist" in
    whatever order the resolver feels like, and the list is then capped at
    :data:`MAX_CANDIDATE_ENDPOINTS` — so on a machine with several adapters the
    router-granted address could be truncated away entirely by addresses nobody
    can reach. :func:`_address_rank` decides the order, the cap is applied
    AFTER it, and the first row is therefore the one R-D4's sheet prints.

    Every source is wrapped: name resolution on a laptop that has just changed
    networks raises in ways not worth a taxonomy, and this function's honest
    failure is an empty list — the same answer as "no address to offer", which
    the ack already knows how to say.
    """

    import socket

    found: list[str] = []

    def _keep(value) -> str | None:
        # Zone index off FIRST (``fe80::1%eth0``): a scoped address is
        # meaningless to the machine we would hand it to, and the prefix test
        # below has to see the address rather than the interface name.
        host = str(value or "").strip().lower().split("%", 1)[0]
        if not host or host in _WILDCARD_HOSTS or host == "::1":
            return None
        if host.startswith(_UNOFFERABLE_PREFIXES):
            return None
        if host not in found:
            found.append(host)
        return host

    # R-D8 first, because it is the authority the probe only approximates: it
    # is kept like any other discovered address (deduped, and dropped if it is
    # loopback or link-local), and it takes rank 0 from :func:`_address_rank`.
    # A machine whose hostname resolves to nothing and whose probe names the
    # tunnel therefore still offers its LAN address, because this source found
    # it rather than merely reordering what the other two found.
    table_route = _keep(_default_route_address())

    default_route: str | None = None
    probe = None
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(_DEFAULT_ROUTE_PROBE)
        default_route = _keep(probe.getsockname()[0])
    except Exception:
        pass
    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass

    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, family):
                _keep(info[4][0])
        except Exception:
            continue

    # Stable, so two addresses of the same rank keep the order they were
    # discovered in — the sort decides between KINDS of address and never
    # reorders within one, which is what makes a two-adapter machine's answer
    # reproducible run to run.
    ordered = sorted(
        found, key=lambda host: _address_rank(host, default_route, table_route)
    )
    return ordered[:MAX_CANDIDATE_ENDPOINTS]


def _candidate_endpoints(store_root) -> list[dict]:
    """Where the OTHER install should dial this one, as a peer row's list.

    Built from :func:`_endpoint` — the same three sources, the same confidence
    ordering — and reduced to the shape ``gateway_peers.clean_endpoints`` keeps.
    Empty when this root has no address to offer, which is a real state and not
    an error: an install that has never opened its gateway listener can still
    JOIN another install and talk to it. The edge simply works in one direction
    until it listens, and the ack says so rather than leaving the operator to
    find out when a call from the far side never arrives.

    **The wildcard case is where S2 changed the answer, and it is a widening
    rather than a fix to something wrong.** ``0.0.0.0`` used to return ``[]``
    with a correct argument attached — a bind is not an address, and writing one
    into a peer row produces a dial that always fails and looks like the peer
    being down. What was missing was the other half: an operator who binds a
    wildcard has not declined to be reachable, they have declined to CHOOSE, and
    this machine can answer that question itself. So a wildcard now enumerates
    :func:`_machine_addresses`; a concrete host is still exactly one row;
    ``unknown`` is still ``[]``.

    Computed in the CLI process, at CLI time, and deliberately NOT put on the
    greeting frame: interface enumeration is a question whose answer changes
    when somebody joins a wifi network, and a frame minted at boot would carry a
    stale one for the life of the serve.
    """

    endpoint = _endpoint(store_root)
    host, port = endpoint.get("host"), endpoint.get("port")
    if not host or not port:
        return []
    port = int(port)
    if str(host).strip().lower() in _WILDCARD_HOSTS:
        return [{"host": address, "port": port} for address in _machine_addresses()]
    # A listener pinned to loopback is kept as a row rather than filtered: the
    # two-roots lane is exactly that shape — two installs on one box, pairing
    # over 127.0.0.1 on purpose. The filter above exists for ENUMERATED
    # addresses, where a loopback row would be noise beside a real one.
    return [{"host": str(host), "port": port}]


def _dial_host(endpoints: list[dict]) -> tuple[str, int] | None:
    """The ONE address every payload writer names, or ``None``.

    R-D1: *a payload host is a dialable address or the verb refuses.* Before
    this existed, four writers each took ``_endpoint(root)["host"]`` — the
    LISTENER'S BIND — and a wildcard bind therefore put the literal ``0.0.0.0``
    into a join payload, a QR payload and a grant. That is not an address: on
    Windows dialling it fails with ``WSAEADDRNOTAVAIL`` and on macOS it resolves
    to the dialler's own loopback, so both machines in S4's hardware attempt
    reported the far install as unreachable when nothing was wrong with either
    listener.

    The answer is simply the first of :func:`_candidate_endpoints`, which after
    R-D2 is the default-route address. Defined as its own function rather than
    inlined four times because "which address do we hand out" must have exactly
    one answer — the same reason :func:`_self_endpoints` exists — and because
    ``gateway id`` prints it as ``dial_host`` for the launcher's sheet to read
    (R-D4). ``None`` when the list is empty, which the callers distinguish from
    "the lane is off" using the endpoint's own ``source``.

    **It takes the LIST, not the root, and D1b is why.** D1 wrote it as
    ``_dial_host(store_root)``, which enumerated a second time; enumerating was
    two socket calls then and is a routing-table process spawn now (~0.4 s on
    the operator's Windows PC), and both callers — ``_dial_target`` and
    ``gateway id`` — were already holding the list they asked for again. Same
    single answer, one enumeration per command instead of two.
    """

    if not endpoints:
        return None
    first = endpoints[0]
    return str(first["host"]), int(first["port"])


def _dial_target(store_root, endpoint: dict, *, args):
    """What a payload writer needs: the dial host, the full list, or a refusal.

    Returns ``(dial, endpoints, 0)`` or ``(None, [], exit_code)`` — the error
    branch is already rendered when it returns, so callers propagate the int.

    The refusal fires on exactly one condition: the listener is ``live`` or
    ``config`` — so this root IS reachable in principle — and enumeration
    produced nothing. ``unknown`` is deliberately NOT refused here, because each
    verb already answers that its own way and they disagree on purpose:
    ``introduce`` refuses it (its consumer is a machine that will dial), while
    ``pair`` and ``peers pair`` print :data:`LISTENER_OFF_SENTENCE` as a note and
    still mint (their consumer is a human who can go turn the listener on, and
    a code minted before the first boot is a legitimate thing to have).
    """

    endpoints = _candidate_endpoints(store_root)
    dial = _dial_host(endpoints)
    if dial is None and endpoint.get("source") in {"live", "config"}:
        return (
            None,
            [],
            emit_harness_error(
                RuntimeError("no_dial_host"),
                reason="no_dial_host",
                args=args,
                code="runtime_unavailable",
                message=(
                    f"{NO_DIAL_HOST_SENTENCE}. Writing the bind "
                    f"({endpoint.get('host')!r}) into the payload would tell the "
                    "other machine to dial an address that is not one. Name a "
                    "reachable interface in remote_gateway.listen, or fix why "
                    "this host enumerates none."
                ),
            ),
        )
    return dial, endpoints, 0


def _self_endpoints(store_root) -> list[dict]:
    """Backwards-compatible spelling of :func:`_candidate_endpoints`.

    Kept as a name rather than as a body: ``peers join`` and ``peers pair`` both
    call it, and S2's point is that the endpoints a hello ADVERTISES and the
    endpoints ``gateway id`` PRINTS are one list. Two functions with two answers
    is how an install ends up advertising an address it does not print.
    """

    return _candidate_endpoints(store_root)


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
    from agent_runtime.serve_gateway_auth import StoreRefusal, pairing_store_path

    root = paths.store_root()
    resolved, code_or_error = _install_and_certificate(args)
    if resolved is None:
        return code_or_error
    identity, certificate = resolved

    # Before the mint, for ``gateway pair``'s reason: a refusal that had already
    # written a pending entry burns one of the operator's three.
    endpoint = _endpoint(root)
    dial, endpoints, failure = _dial_target(root, endpoint, args=args)
    if failure:
        return failure

    minted = mint_peer_code(root, note=getattr(args, "note", None))
    if isinstance(minted, StoreRefusal):
        # Into ``pairing.json``, the same file ``gateway pair`` mints into: one
        # store, one cap, one lockout across both ceremonies (R-D14).
        return _refusal(minted, args=args, store_path=pairing_store_path(root))

    payload = {
        # R-D1 / R-D3, exactly as ``gateway pair`` writes them: a dialable first
        # candidate, the whole ordered list beside it, and never the bind.
        "host": dial[0] if dial else None,
        "port": dial[1] if dial else None,
        "endpoints": endpoints,
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
            else LISTENER_OFF_SENTENCE + " The code is valid either way."
        )

    envelope = attach_root_observability(_object_envelope("gateway_peer_pairing", row))
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def cmd_gateway_introduce(args) -> int:
    """``harness gateway introduce`` — one envelope a launcher can post as a grant.

    **A COMPOSITION, not a third ceremony.** It calls ``mint_peer_code`` and
    ``mint_pairing_code`` — the same two functions ``peers pair`` and ``pair``
    call, under the same lockout, the same pending cap and the same ten-minute
    TTL — and prints their two codes in one object shaped the way the backend's
    fulfil endpoint wants it (S1 packet §4.1). Nothing is stored that those two
    verbs do not store, and there is no new credential kind. What is new is the
    scoping: both halves are minted FOR a named requester, and the peer half is
    genuinely refused to anybody else (``gateway_peers.redeem_peer_code``).

    **Why it refuses when the listener is off, where ``peers pair`` prints a
    note.** ``peers pair``'s consumer is a human who can read a note, shrug, and
    go turn the listener on. This verb's consumer is a machine: a launcher posts
    the envelope to the backend, the requester reads it once and dials, and the
    dial fails against a door that was never open. A note in that chain is a
    string nobody renders. So an ``unknown`` endpoint is ``runtime_unavailable``
    (family 7, retryable — the identical command succeeds after a restart), with
    ``peers pair``'s own sentence, and a ``config`` endpoint is ALLOWED with a
    note, because "the serve has not booted yet" is a real and recoverable
    ordering rather than a lane that is off.

    **Two mints, not one atomic pair.** The pending cap
    (``MAX_PENDING_CODES = 3``, counted across both ceremonies) can legitimately
    refuse the second half after the first has been written, and holding one
    lock across both would not change that — it would only make the refusal
    arrive with a half-built envelope and no way to report which half failed.
    So each mint is its own atomic write, the envelope carries ``null`` for a
    half that did not mint, ``refusals`` names it, and the exit is 0 only when
    every requested half is present.

    **The codes appear exactly here.** In ``peer.peer_code`` / ``device.code``
    and inside the two payload strings, on stdout, once. Never in an event,
    never in a row, never in a log line — the codes discipline
    (``gateway_pairing_codes``), unchanged, and the reason its ten-minute TTL is
    allowed to be short.
    """

    from agent_runtime import paths
    from agent_runtime.gateway_capabilities import GATEWAY_CAPABILITIES
    from agent_runtime.gateway_peers import mint_peer_code
    from agent_runtime.serve_gateway_auth import (
        CREDENTIAL_TTL_SECONDS_INTRODUCED,
        StoreRefusal,
        mint_pairing_code,
        pairing_store_path,
    )
    from agent_runtime.state_patches import (
        CORRELATION_ID_MAX_LEN,
        normalize_correlation_id,
    )

    root = paths.store_root()
    for_install_id = str(getattr(args, "for_install", "") or "").strip()
    for_device_id = str(getattr(args, "for_device", "") or "").strip()
    if not for_install_id and not for_device_id:
        # At least one, never both required: a PHONE has no install id (the
        # device half is all there is to mint for it), and an install being
        # re-introduced after a rebuild may have no account device row yet. The
        # parent plan's "both that apply" is exactly this.
        return emit_harness_error(
            RuntimeError("no_requester"),
            reason="no_requester",
            args=args,
            code="invalid_payload",
            message=(
                "gateway introduce needs at least one of --for-install (another "
                "hermes install, which gets the peer half) or --for-device (an "
                "account device, which gets the device half). With neither there "
                "is nobody to scope the codes to, and an unscoped code is what "
                "`harness gateway pair` and `peers pair` already mint."
            ),
        )

    raw_correlation = getattr(args, "correlation", None)
    correlation = None
    if raw_correlation is not None and str(raw_correlation).strip():
        # The SAME fence the RPC lane applies to ``correlation_id``
        # (``serve_rpc._correlation_id_param`` → ``state_patches``), read from
        # the module that owns the rule rather than restated here — R-IP17 says
        # the grant id is one token and every party writes it, which is only
        # true if every party agrees what a legal one looks like. Refused and
        # never repaired: a sanitized id would print a value neither the backend
        # nor this install used.
        correlation = normalize_correlation_id(raw_correlation)
        if correlation is None:
            return emit_harness_error(
                RuntimeError("correlation_id_invalid"),
                reason="correlation_id_invalid",
                args=args,
                code="invalid_payload",
                message=(
                    "--correlation is the backend grant id and must be a "
                    f"generated token of at most {CORRELATION_ID_MAX_LEN} "
                    "characters from [A-Za-z0-9_.:-]"
                ),
            )

    resolved, code_or_error = _install_and_certificate(args)
    if resolved is None:
        return code_or_error
    identity, certificate = resolved

    endpoint = _endpoint(root)
    if endpoint["source"] == "unknown":
        return emit_harness_error(
            RuntimeError("gateway_listener_off"),
            reason="gateway_listener_off",
            args=args,
            code="runtime_unavailable",
            message=LISTENER_OFF_SENTENCE,
        )

    dial, endpoints, failure = _dial_target(root, endpoint, args=args)
    if failure:
        return failure
    note = getattr(args, "note", None)

    peer_block = None
    device_block = None
    refusals: list[dict[str, str]] = []
    first_refusal = None

    if for_install_id:
        minted = mint_peer_code(
            root,
            note=note,
            credential_ttl_seconds=CREDENTIAL_TTL_SECONDS_INTRODUCED,
            for_install_id=for_install_id,
            correlation=correlation,
        )
        if isinstance(minted, StoreRefusal):
            refusals.append({"half": "peer", "reason": minted.reason})
            first_refusal = first_refusal or minted
        else:
            peer_block = {
                "peer_code": minted.code,
                "expires_in_seconds": minted.expires_in_seconds(),
                "join_payload": json.dumps(
                    {
                        # R-D1: the DIAL host, not ``endpoint["host"]``. This is
                        # the line S4's hardware attempt died on — a wildcard
                        # bind put ``0.0.0.0`` here, the far install dialled it,
                        # and the receipt said ``runtime_unavailable``.
                        "host": dial[0] if dial else None,
                        "port": dial[1] if dial else None,
                        "endpoints": endpoints,
                        "install_id": identity.install_id,
                        "cert_fingerprint": certificate.fingerprint,
                        "peer_code": minted.code,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            }

    if for_device_id:
        minted_device = mint_pairing_code(
            root,
            name=note,
            # ``console`` and not the operator's choice: an introduction is the
            # account saying "this is my own device on my own machine", which is
            # the exact provenance ``DEFAULT_DEVICE_TIER``'s ruling names. A
            # ``--tier`` flag here would be a knob whose only safe setting is
            # the default.
            tier="console",
            credential_ttl_seconds=CREDENTIAL_TTL_SECONDS_INTRODUCED,
            for_device_id=for_device_id,
            correlation=correlation,
        )
        if isinstance(minted_device, StoreRefusal):
            refusals.append({"half": "device", "reason": minted_device.reason})
            first_refusal = first_refusal or minted_device
        else:
            device_block = {
                "code": minted_device.code,
                "tier": minted_device.tier,
                "expires_in_seconds": minted_device.expires_in_seconds(),
                "qr_payload": json.dumps(
                    {
                        # The device half's copy of the same correction, and it
                        # has to be the same object: one introduction is one
                        # machine, and two halves that named different addresses
                        # would be an introduction to two different places.
                        "host": dial[0] if dial else None,
                        "port": dial[1] if dial else None,
                        "endpoints": endpoints,
                        "install_id": identity.install_id,
                        "cert_fingerprint": certificate.fingerprint,
                        "code": minted_device.code,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            }

    if first_refusal is not None:
        # The first refusal's family, not a generic one: a pending cap, a
        # lockout and a store this machine cannot write are three different next
        # moves, and the exit code is how a launcher tells them apart without
        # parsing prose. Both halves mint into ``pairing.json`` — one file, one
        # cap, one lockout across the two ceremonies — so one path answers for
        # whichever half refused first.
        return _refusal(
            first_refusal, args=args, store_path=pairing_store_path(root)
        )

    # **One writer of the backend's shape.** The launcher POSTs this object
    # verbatim; building it here rather than letting the launcher assemble it
    # from the envelope's other keys is what keeps "what fulfil receives" a
    # decision this repo made once. Its key set is :data:`GRANT_PAYLOAD_KEYS`.
    grant_payload = {
        "peer_join_payload": (peer_block or {}).get("join_payload"),
        "device_pair_payload": (device_block or {}).get("qr_payload"),
        "install_id": identity.install_id,
        "endpoints": endpoints,
        "cert_fingerprint": certificate.fingerprint,
        "correlation": correlation,
    }
    compact = json.dumps(grant_payload, separators=(",", ":"), sort_keys=True)
    if len(compact.encode("utf-8")) > GRANT_PAYLOAD_MAX_BYTES:
        # Unreachable at four endpoints and two ~200-byte payloads; asserted
        # anyway because the alternative is discovering the ceiling as an opaque
        # 400 from a service this process cannot see.
        return emit_harness_error(
            RuntimeError("grant_payload_too_large"),
            reason="grant_payload_too_large",
            args=args,
            code="invalid_payload",
            message=(
                f"the grant payload is {len(compact.encode('utf-8'))} bytes and "
                f"the backend accepts {GRANT_PAYLOAD_MAX_BYTES}. Reduce the "
                "advertised endpoints (remote_gateway.listen can name one "
                "interface instead of a wildcard)."
            ),
        )

    row = {
        "install_id": identity.install_id,
        "display_name": identity.display_name,
        "cert_fingerprint": certificate.fingerprint,
        "endpoints": endpoints,
        "endpoints_source": endpoint["source"],
        "capabilities": list(GATEWAY_CAPABILITIES),
        "correlation": correlation,
        "for_install_id": for_install_id or None,
        "for_device_id": for_device_id or None,
        "credential_ttl_seconds": CREDENTIAL_TTL_SECONDS_INTRODUCED,
        "peer": peer_block,
        "device": device_block,
        "grant_payload": grant_payload,
    }
    if refusals:
        row["refusals"] = refusals
    if endpoint["source"] != "live":
        row["note_endpoint"] = (
            "no running serve advertised a gateway listener for this root, so "
            "the endpoint in these payloads is what the config says the NEXT "
            "boot will use. The codes are valid either way; a requester that "
            "dials before this root boots simply fails to connect."
        )

    envelope = attach_root_observability(_object_envelope("gateway_introduction", row))
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
                reason="payload_not_json",
                message=f"the join payload is not JSON: {exc}",
            )
            return None
        if not isinstance(decoded, dict):
            emit_harness_error(
                RuntimeError("payload_not_an_object"),
                reason="payload_not_an_object",
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
    overridden = bool(getattr(args, "host", None)) or bool(getattr(args, "port", None))
    for flag, key in (
        ("host", "host"),
        ("port", "port"),
        ("fingerprint", "cert_fingerprint"),
    ):
        value = getattr(args, flag, None)
        if value:
            fields[key] = value

    code = str(fields.get("peer_code") or "").strip().upper()
    # R-D3's list, read here so the dial loop below never has to know which
    # spelling of a payload it was given. Three sources, in this order:
    #
    # * ``--host/--port`` — a SINGLE candidate. An operator (or the launcher's
    #   redeemer, which passes one candidate per run) who names an address means
    #   that address and not a list to fall back through.
    # * the payload's ``endpoints``, which is what every writer on this lane has
    #   emitted since R-D3.
    # * the payload's legacy ``host``/``port`` as a one-row list, which is what
    #   an install that predates this key sends. Not a compatibility shim to
    #   delete later: a bare code typed with --host/--port arrives the same way.
    candidates = _clean_candidates(fields.get("endpoints"))
    host = str(fields.get("host") or "").strip()
    try:
        port = int(fields.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if candidates and not (host and port):
        # A payload carrying only the list still names a first candidate, and
        # ``host``/``port`` are that row by contract — so filling them in here is
        # reading the contract, not guessing.
        host, port = candidates[0]["host"], candidates[0]["port"]
    if overridden or not candidates:
        candidates = [{"host": host, "port": port}] if host and port else []
    missing = [
        name
        for name, value in (("peer_code", code), ("host", host), ("port", port))
        if not value
    ]
    if missing:
        emit_harness_error(
            RuntimeError("incomplete_payload"),
            reason="incomplete_payload",
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
        "endpoints": candidates,
        "install_id": str(fields.get("install_id") or "").strip() or None,
        "cert_fingerprint": str(fields.get("cert_fingerprint") or "").strip() or None,
    }


def _clean_candidates(raw: Any) -> list[dict]:
    """A payload's ``endpoints`` as rows this verb can dial, order preserved.

    Deliberately forgiving about the CONTENTS and strict about the SHAPE: a row
    without a usable host or port is dropped rather than refusing the whole
    payload, because the list is advertisement — the far install offered every
    address it could think of — and one unusable row is not a reason to refuse
    an edge that three good rows would have made. A payload with nothing usable
    in it falls through to the legacy ``host``/``port`` above and is refused
    there, by name, if those are absent too.
    """

    if not isinstance(raw, list):
        return []
    rows: list[dict] = []
    for item in raw[:MAX_CANDIDATE_ENDPOINTS]:
        if not isinstance(item, dict):
            continue
        host = str(item.get("host") or "").strip()
        try:
            port = int(item.get("port") or 0)
        except (TypeError, ValueError):
            continue
        if not host or host.lower() in _WILDCARD_HOSTS or not port:
            continue
        row = {"host": host, "port": port}
        if row not in rows:
            rows.append(row)
    return rows


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
    * a dial that fails against EVERY advertised address, as
      ``runtime_unavailable`` (family 7, retryable), because a listener that is
      not up yet is exactly the condition where the identical command succeeds
      five seconds later. Since R-D3 the payload carries the far install's whole
      candidate list and this verb walks it in order, stopping at the first
      handshake; ``--host``/``--port`` still collapse that to one candidate, and
      the refusal names every address it tried with the exception class beside
      it, because "runtime_unavailable" alone is the same sentence for a
      listener that is down, a firewall that dropped the SYN, and an address
      that was never dialable.

    S2 adds a fourth, and it fires BEFORE any socket is opened: with
    ``--expect-fingerprint`` (R-S2-6), a payload whose ``cert_fingerprint``
    disagrees with the value the ACCOUNT attests is refused as
    ``tls_fingerprint_mismatch`` with nothing dialled and nothing written.
    Without the flag the verb keeps its trust-on-first-use pin and the ack says
    ``fingerprint_attested: false`` — a weaker posture that announces itself.
    """

    from agent_runtime import paths
    from agent_runtime.gateway_peers import peer_store_path, record_peer
    from agent_runtime.serve_gateway_auth import StoreRefusal
    from agent_runtime.serve_socket import ServeSocketClient

    root = paths.store_root()
    parsed = _parse_join_payload(getattr(args, "payload", None), args)
    if parsed is None:
        return 2

    # ── the attested pin (R-S2-6), decided BEFORE anything is dialled ────────
    #
    # Without ``--expect-fingerprint`` this verb is trust-on-first-use: it pins
    # whatever fingerprint the payload carried, which is exactly as strong as
    # the channel the operator carried the payload through. That is the manual
    # ceremony's posture and it stays unchanged — but the ack now SAYS so
    # (``fingerprint_attested: false``), because a weaker posture nobody
    # announces is a weaker posture nobody notices.
    #
    # With the flag, the fingerprint came from the account (S3 passes
    # ``DeviceOut.gateway_cert_fingerprint``, which the backend holds because
    # the far install told the account, signed in). A payload that disagrees is
    # refused HERE, before a socket exists: dialling first and comparing after
    # would hand an impostor a completed TLS handshake and a timing signal, and
    # would burn an attempt on an answer that cannot change.
    expected = str(getattr(args, "expect_fingerprint", "") or "").strip().lower()
    attested = bool(expected)
    if attested:
        if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
            return emit_harness_error(
                RuntimeError("tls_fingerprint_invalid"),
                reason="tls_fingerprint_invalid",
                args=args,
                code="invalid_payload",
                message=(
                    # The reason word LEADS the sentence rather than riding a
                    # field, because ``emit_harness_error`` carries only the
                    # exception CLASS in ``safe_details`` — so the message is
                    # the one channel this lane has for a machine-readable
                    # reason, and R-IP17 asks for one enumerated set of them.
                    "tls_fingerprint_invalid: --expect-fingerprint is the "
                    "account's attested certificate fingerprint and must be 64 "
                    f"lowercase hex characters (sha256); got {len(expected)}."
                ),
            )
        offered = str(parsed["cert_fingerprint"] or "").strip().lower()
        if offered != expected:
            return emit_harness_error(
                RuntimeError("tls_fingerprint_mismatch"),
                reason="tls_fingerprint_mismatch",
                args=args,
                code="invalid_payload",
                message=(
                    # R-IP17's word first, for the reason above.
                    "tls_fingerprint_mismatch: the join payload offers "
                    f"certificate {offered or '(none)'} and the account attests "
                    f"{expected}. Nothing was dialled and no row was written: a "
                    "payload whose fingerprint disagrees with the account is "
                    "either stale or is not the install it names."
                ),
            )
        # From here the PIN is the attested value, not the payload's — they are
        # equal, and taking the attested one is what makes that an invariant
        # rather than a coincidence a later edit could break.
        parsed["cert_fingerprint"] = expected

    raw_correlation = getattr(args, "correlation", None)
    correlation = None
    if raw_correlation is not None and str(raw_correlation).strip():
        from agent_runtime.state_patches import (
            CORRELATION_ID_MAX_LEN,
            normalize_correlation_id,
        )

        correlation = normalize_correlation_id(raw_correlation)
        if correlation is None:
            return emit_harness_error(
                RuntimeError("correlation_id_invalid"),
                reason="correlation_id_invalid",
                args=args,
                code="invalid_payload",
                message=(
                    "--correlation is the backend grant id and must be a "
                    f"generated token of at most {CORRELATION_ID_MAX_LEN} "
                    "characters from [A-Za-z0-9_.:-]"
                ),
            )

    resolved, code_or_error = _install_and_certificate(args)
    if resolved is None:
        return code_or_error
    identity, certificate = resolved

    endpoints = _self_endpoints(root)

    # ── the dial, over the candidate LIST (R-D3) ─────────────────────────────
    #
    # One address used to be the whole of it, and that address was whatever the
    # payload's ``host`` said — which, before R-D1, could be a bind. Now the
    # payload carries every address the far install can offer, in the order IT
    # ranked them, and this loop takes the first handshake that completes.
    #
    # Two failure kinds, and telling them apart is the point of the loop rather
    # than a refinement of it:
    #
    # * a DIAL failure (refused, timed out, no route) is about THIS ADDRESS, and
    #   the next candidate may be on a segment that works. Recorded and moved
    #   past.
    # * a certificate that does not match the pin is about the far INSTALL'S
    #   IDENTITY, and no address can change the answer. Terminal, immediately,
    #   because retrying it means offering the same wrong certificate three more
    #   chances and burning three timeouts to reach the same refusal.
    #
    # ``--timeout`` is PER CANDIDATE, not a budget for the whole loop: an
    # operator who allows twenty seconds for a handshake means twenty seconds
    # for a handshake, and dividing it by a list length they did not write would
    # make the flag mean something different on every payload.
    from agent_runtime.serve_socket import ServeCertificatePinMismatch

    attempts: list[str] = []
    reply = None
    dialled: dict | None = None
    for candidate in parsed["endpoints"]:
        connection = ServeSocketClient(
            candidate["host"],
            candidate["port"],
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
            dialled = candidate
        except ServeCertificatePinMismatch as exc:
            return emit_harness_error(
                exc,
                args=args,
                code="invalid_payload",
                reason="tls_fingerprint_mismatch",
                message=(
                    # R-IP17's reason word first, as the pre-dial check spells
                    # it, so one enumerated vocabulary covers both the payload
                    # that disagreed with the account and the certificate that
                    # disagreed with the payload.
                    "tls_fingerprint_mismatch: "
                    f"{candidate['host']}:{candidate['port']} presented a "
                    "certificate that is not the one this payload pins. No "
                    "other address was tried and no row was written: a "
                    "mismatched certificate is a statement about the install, "
                    "not about the address it answered on."
                ),
            )
        except Exception as exc:
            attempts.append(
                f"{candidate['host']}:{candidate['port']} ({type(exc).__name__})"
            )
        finally:
            connection.close()
        if reply is not None:
            break

    if reply is None or dialled is None:
        # Every address tried, named, with the exception class beside it — the
        # receipt S4's hardware attempt did not have. "runtime_unavailable" on
        # its own is unattributable from the far machine: it reads identically
        # whether the listener is down, the firewall dropped the SYN, or (the
        # actual answer that day) the address was never dialable in the first
        # place.
        tried = ", ".join(attempts) or "(none — the payload offered no address)"
        return emit_harness_error(
            RuntimeError("no_candidate_answered"),
            reason="no_candidate_answered",
            args=args,
            code="runtime_unavailable",
            message=(
                "could not complete the peer handshake with any advertised "
                f"address. Tried: {tried}. The other install's gateway listener "
                "must be running, reachable, and presenting the certificate "
                "whose fingerprint is in the payload."
            ),
        )

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
            reason="no_peer_secret",
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
            reason="install_id_mismatch",
            args=args,
            code="invalid_payload",
            message=(
                f"the payload names install {parsed['install_id']!r} but "
                f"{dialled['host']}:{dialled['port']} answered as {remote_id!r}. "
                "Something else is on that address; the row was NOT written."
            ),
        )

    # ── recording it: the half that failed on hardware (R-D14) ───────────────
    #
    # Everything above this line worked on the operator's PC on 2026-09-04. The
    # code was granted, the dial reached 192.168.1.39:8765, the far install
    # redeemed and returned the secret — and then this write raised
    # ``[WinError 5]``, four times in five minutes. It reached the sheet as
    # ``runtime_unavailable`` → ``no_route`` → "Unreachable": a claim about the
    # network, for a DACL on a local directory.
    #
    # So a write refusal here is its OWN reason with its own family, and the
    # message names the file. Both doors give the same answer — ``record_peer``
    # catches OSError around its locked write and returns a refusal, but the
    # cache touch and the event append it runs after releasing the lock are
    # outside that span, and an operator must not get two different stories
    # depending on which line inside the store surfaced the same permission.
    peers = peer_store_path(root)
    try:
        outcome = record_peer(
            root,
            peer_install_id=remote_id or parsed["install_id"] or "",
            secret=str(peered["peer_secret"]),
            display_name=remote.get("display_name"),
            # The candidate that ANSWERED, not the payload's first row. The row
            # is this install's memory of where the far one is, so recording an
            # address the loop walked past would make every later dial start
            # with a failure this run already proved.
            endpoints=[dict(dialled)],
            cert_fingerprint=parsed["cert_fingerprint"],
            # Read off the frame, never derived. The far side computed it at
            # redemption; a second derivation here would make the two ends of
            # one edge lapse at two different moments. ``None`` on every edge
            # the manual ceremony mints, and on every far install predating S2.
            expires_at=peered.get("expires_at"),
        )
    except OSError as exc:
        return _store_write_refusal(exc, args=args, store_path=peers)
    if isinstance(outcome, StoreRefusal):
        return _refusal(outcome, args=args, store_path=peers)

    row = outcome.payload()
    # Which posture wrote this row, said out loud on the ack. An operator (and
    # S3's request loop) reading a stored edge should not have to remember which
    # flags the join was run with to know whether the pin was attested by the
    # account or merely copied off a payload.
    row["fingerprint_attested"] = attested
    if correlation is not None:
        row["correlation"] = correlation
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
    from agent_runtime.gateway_peers import list_peers, read_peer_cache, usable_peers

    root = paths.store_root()
    cache = read_peer_cache(root)
    usable = {peer.record.peer_install_id: peer for peer in usable_peers(root)}

    rows = []
    for record in list_peers(root):
        row = record.payload()
        cached = cache.get(record.peer_install_id)
        # **The trust/cache split, visible in the ack** (S2c). Nesting the
        # second half rather than flattening it is the point: an operator
        # reading this list can see which facts this install DECIDED and which
        # ones a peer TOLD it — the same question the two files answer, and one
        # that would be lost if both halves sat side by side as flat keys.
        row["cache"] = cached.payload() if cached is not None else None
        # The live stamp comes from the cache when there is one. The top-level
        # key is KEPT rather than removed — a launcher and an operator both read
        # it today — and now answers with the fact instead of with the legacy
        # residue left in the trust row.
        if cached is not None and cached.last_seen:
            row["last_seen"] = cached.last_seen
        peer = usable.get(record.peer_install_id)
        # ONE predicate, rendered. So the sheet's "removed" group is a filter
        # over this list rather than a fourth place deciding for itself which
        # edges are alive.
        row["usable"] = peer is not None
        row["ref"] = peer.ref if peer is not None else None
        row["unusable_reason"] = (
            None if peer is not None else _unusable_reason(record, cached)
        )
        rows.append(row)

    envelope = attach_root_observability(_list_envelope("gateway_peer", rows))
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def _unusable_reason(record, cached) -> str:
    """Why one row is not in ``usable_peers``, in the resolver's own vocabulary.

    The SAME words ``gateway_targets`` refuses with, so an operator comparing a
    list against a failed send reads one vocabulary rather than two. Ordered as
    the predicate orders them: a decision this operator made outranks a clock,
    and both outrank the far side's decision.
    """

    from agent_runtime.gateway_targets import (
        REASON_PEER_EXPIRED,
        REASON_PEER_REVOKED,
        REASON_PEER_REVOKED_YOU,
    )

    if record.revoked:
        return REASON_PEER_REVOKED
    if record.expired:
        return REASON_PEER_EXPIRED
    if cached is not None and cached.revoked_you:
        return REASON_PEER_REVOKED_YOU
    return ""


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

    **S2c adds a courtesy and not an authority, and the distinction is the whole
    of it.** Before the local write, this verb tells the peer ``revoked_you``
    through ``peer.announce`` — a write into the far install's own CACHE, which
    that install applies to its own row under its own rules and which gates
    nothing there. It is still not a revocation reaching across the wire: the
    far side's credential for us is untouched and its operator's decision is
    untouched; all it learns is that ours changed. What it buys is that the news
    arrives in seconds rather than at that install's next call — which, for an
    agent mid-conversation, is the difference between a refusal and a message
    written to nobody.
    """

    from agent_runtime import paths
    from agent_runtime.gateway_peers import lookup_peer, peer_store_path, revoke_peer
    from agent_runtime.serve_gateway_auth import StoreRefusal

    peer_install_id = str(getattr(args, "peer_install_id", "") or "").strip()
    if bool(getattr(args, "dry_run", False)):
        record = lookup_peer(paths.store_root(), peer_install_id)
        if record is None:
            return emit_harness_error(
                RuntimeError("unknown_peer"),
                reason="unknown_peer",
                args=args,
                code="not_found",
                message=f"no install {peer_install_id!r} is paired with this root",
            )
        row = record.payload()
        # What the WRITE would land, not what is there now.
        row["revoked"] = True
        # A preview sends nothing over the wire: a dry run that announced would
        # have told the far install about a revocation that never happened.
        row["announced"] = False
        envelope = attach_root_observability(_object_envelope("gateway_peer", row))
        envelope["dry_run"] = True
        _print_stage42(envelope, args=args, default_output="json")
        return 0

    # **Announce BEFORE the local write** (R-S2-15), and the order is
    # load-bearing rather than tidy. The announce is a CALL to the peer, and a
    # peer we have already revoked would be refused at our own door on the way
    # back — so announcing first means the far install learns it was cut while
    # the edge still works, and announcing after would mean the news went out
    # over a connection this install had just closed to it.
    #
    # A failed announce never blocks the revoke. That is the whole posture: the
    # push makes the news arrive in seconds, and nothing depends on it arriving
    # — the far side still learns at its next dial's refusal, exactly as it did
    # before this edge existed. The ack says which of the two happened.
    announced = False
    if not bool(getattr(args, "no_announce", False)):
        from agent_runtime.gateway_announce import announce_to_peers

        receipts = announce_to_peers(
            paths.store_root(),
            {"revoked_you": True},
            only=[peer_install_id],
        )
        announced = any(receipt.ok for receipt in receipts)

    peers = peer_store_path(paths.store_root())
    try:
        outcome = revoke_peer(
            paths.store_root(), peer_install_id, announced=announced
        )
    except OSError as exc:
        # ``revoke_peer`` catches OSError around its own locked write and
        # returns a refusal; the event append it runs AFTER releasing the lock
        # is outside that span. Both doors, one answer (R-D14).
        return _store_write_refusal(exc, args=args, store_path=peers)
    if isinstance(outcome, StoreRefusal):
        return _refusal(outcome, args=args, store_path=peers)
    row = outcome.payload()
    row["takes_effect"] = "next_handshake"
    row["scope"] = "this_install_only"
    # Whether the other machine was TOLD, not whether we tried. An operator who
    # believes a revocation was heard when it was not is the gap this closes.
    row["announced"] = announced
    row["note"] = (
        f"{peer_install_id} can no longer reach this install. Its own store "
        "still holds a row for this one — run `harness gateway peers revoke` "
        "over there to cut the edge in both directions."
        + (
            ""
            if announced
            else " It was NOT reachable to be told, so it will discover this at "
            "its next call."
        )
    )
    envelope = attach_root_observability(_object_envelope("gateway_peer", row))
    _print_stage42(envelope, args=args, default_output="json")
    return 0
