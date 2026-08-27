"""Per-PEER credentials for the gateway listener: pair, join, verify, revoke.

The sibling of ``serve_gateway_auth``. Where that module answers "which paired
DEVICE is this" — a phone, a tablet, something with a screen and an operator
holding it — this one answers "which paired INSTALL is this": another hermes
runtime, on another machine, that an operator approved on BOTH sides.

``<store_root>/gateway/peers.json``, beside ``install.json``, ``devices.json``
and ``pairing.json``. One row per peer::

    {peer_install_id, display_name, endpoints, cert_fingerprint,
     secret_verifier, approved_at, last_seen, revoked, revoked_at}

R5, and why the ceremony has two operators in it
-------------------------------------------------

R5 is **ADOPTED at its recommendation** (primary plan §5, under the operator's
"implement it all" directive): *each install⇄install edge is explicitly
approved (both sides), and agents can never initiate pairing.* Everything about
the shape below follows from that sentence.

"Both sides" is not decoration and it is not achieved by asking twice. It is
achieved by making the ceremony physically require a human at each end: install
A's operator runs ``harness gateway peers pair`` and reads eight characters off
their own terminal; install B's operator types those characters into ``harness
gateway peers join`` on a different machine. Neither half can be performed by
the other install, because A never learns B's address until B dials, and B
cannot mint a code in A's store. **There is no verb that pairs an install
without an operator at both ends**, which is a stronger property than an
approval flag on a row: a flag can be set by whatever wrote the row.

"Agents can never initiate pairing" — and the residual, named honestly
---------------------------------------------------------------------

The four peer verbs are CLI verbs and have NO wire twin: there is no
``gateway.*`` RPC method that mints, redeems, lists or revokes a peer. A remote
caller therefore cannot reach them through the method lane (nothing to call)
and cannot reach them through the argv lane either, because **the argv lane is
refused outright to every gateway connection** (Stage 1, ``serve.py``'s
``argv_lane_unavailable``) — so "send the CLI verb as argv" is not a door
standing beside the missing method, it is a door that answers one typed error.
That is what closes the REMOTE half, and it closes it structurally.

What it does not close, and what would be dishonest to claim it closes: **a
local agent with shell access on this machine can run these verbs**, exactly as
it can run ``harness gateway pair``, read ``serve_auth_token``, or edit
``peers.json`` with a text editor. Every tool-using agent on an install already
holds the machine owner's authority — that is what ``CALLER_STDIO_OWNER``'s
docstring says and it is true here too. So the accurate statement of what R5
buys is: *no agent on install A can cause install B to trust it, and no remote
caller of any tier can mint a peer anywhere.* An agent that has already taken
over the machine A's operator is sitting at is not a case this ceremony was
ever going to fix, and pretending otherwise would put a false claim in the one
file an auditor would read.

What is stored, and the honest limit of hashing it
--------------------------------------------------

The peer secret is SYMMETRIC and both installs keep only ``sha256(secret)``,
under the key ``secret_verifier``. The limit is exactly ``serve_gateway_auth``'s
and is restated rather than referenced, because a security note one indirection
away is a security note nobody reads: **the verifier is HMAC-key-equivalent.**
Anyone who can read ``peers.json`` can answer a challenge as that peer, exactly
as anyone who can read ``devices.json`` can answer as any device in it.
Digesting buys one real thing and not two — the bytes that travelled through
the pairing channel are not the bytes on either disk, so a store read cannot
recover the ISSUED secret, only the ability to use it. Store-read resistance
needs an asymmetric scheme (the R1 survey's bullet 2) and would change only
:func:`peer_proof` and this file; the wire says "proof" and nothing about how
it was computed.

Say the mechanism plainly too, because it differs from the device lane in a way
a reader would otherwise get wrong: the digest is not merely what is compared,
it is the working KEY at both ends. A phone holds its device token and digests
it per connection; neither install ever holds a peer secret again after the one
frame that carried it, so both sides key the HMAC with the stored verifier
directly. That is what makes the edge symmetric — either install can dial the
other with the row it already has — and it is why :func:`peer_proof` has one
spelling where the device lane needs two.

One thing is genuinely different from the device store and is worth stating.
The device secret is minted BY the install and held by a phone; the peer secret
is minted by A and held by B, and A keeps a digest of it too. So a peer edge
has two verifier copies rather than one, and revoking on one side does not
revoke on the other — :func:`revoke_peer` refuses the peer at THIS install's
door and says so, and the operator at the other install revokes their own row
if they want the edge gone in both directions. A revocation that reached across
the wire would be a peer-tier write into another install's credential store,
which is precisely the authority R5 says an install never has over another.

Why the field is ``secret_verifier`` and not ``verifier``
----------------------------------------------------------

Deliberately a different key from the device row's. The two stores sit in one
directory and hold rows of a similar shape; a row copied from one file to the
other by a script, a merge, or a hand edit must not accidentally be a valid
credential in its new home. Different key name, different id field, different
proof prefix — three independent reasons a device row read as a peer row (or
the reverse) decodes to nothing rather than to something that works.

Root as INPUT, and never raises for a READ
-------------------------------------------

Both rules are ``serve_gateway_auth``'s, for its reasons. Every function takes
``store_root`` and none resolves one: several roots coexist on this machine and
Stage 6's whole subject is two of them at once, so a credential store free to
re-derive its own root could pair a peer against one install and answer for
another. And the read paths (:func:`lookup_peer`, :func:`list_peers`,
:func:`verify_peer_proof`) answer with a typed outcome and never propagate an
``OSError``: they run on the handshake path, where an exception is a peer that
learns nothing and a runtime that logged a traceback.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .gateway_identity import clean_display_name, gateway_dir
from .gateway_pairing_codes import (
    KIND_PEER,
    MAX_PENDING_CODES,
    expire_pending,
    lockout_remaining,
    match_pending,
    mint_into,
    note_failed_redeem,
    pending_codes,
)
from .serve_gateway_auth import StoreRefusal, _read_pairing, _store_lock, _write_pairing
from .store_file_io import iso_stamp as _iso
from .store_file_io import os_error_reason as _os_reason
from .store_file_io import read_json_object as _read_json
from .store_file_io import write_secure_json as _write_secure

__all__ = [
    "MAX_ENDPOINTS",
    "PEER_AUTH_BAD_PROOF",
    "PEER_AUTH_MALFORMED",
    "PEER_AUTH_OK",
    "PEER_AUTH_REVOKED",
    "PEER_AUTH_UNKNOWN",
    "PEER_PROOF_ALGORITHM",
    "PEER_PROOF_CONTRACT",
    "PEER_SECRET_BYTES",
    "PEER_STORE_CONTRACT",
    "PEER_STORE_FILENAME",
    "PeerAuth",
    "PeerCredential",
    "PeerPairingCode",
    "PeerRecord",
    "clean_endpoints",
    "dial_peer",
    "list_peers",
    "lookup_peer",
    "mint_peer_code",
    "note_peer_seen",
    "peer_proof",
    "peer_secret_verifier",
    "peer_store_path",
    "record_peer",
    "redeem_peer_code",
    "revoke_peer",
]


# ── constants ────────────────────────────────────────────────────────────────

#: Beside ``install.json`` / ``devices.json`` / ``pairing.json``. The DIRECTORY
#: is the unit Stage 0 established and Stage 1 extended; this is its fourth file.
PEER_STORE_FILENAME = "peers.json"
#: Serialised beside the rows so a future migration reads a number rather than
#: guessing from key presence — ``devices.json``'s habit, and its reason.
PEER_STORE_CONTRACT = 1

#: 256 bits, hex-encoded. Not configurable, for ``serve_auth``'s reason: a knob
#: here can only ever be turned down.
PEER_SECRET_BYTES = 32

#: The peer handshake's proof version, and it is a THIRD number beside
#: ``HELLO_CONTRACT_VERSION`` (the frame exchange, shared byte-for-byte) and
#: ``GATEWAY_PROOF_CONTRACT`` (the device derivation). It is separate from the
#: device one for the reason that one is separate from the frame one: they
#: describe different derivations, and a single number covering both would have
#: to move whenever either moved, with no way for a client to tell which half
#: changed. The ``pwv`` prefix below is the other half of that separation —
#: even at equal contract numbers, a device proof and a peer proof over the
#: same nonce and port are different bytes.
PEER_PROOF_CONTRACT = 1
PEER_PROOF_ALGORITHM = "hmac-sha256"

#: How many addresses one peer row may carry. An install can legitimately be
#: reachable at more than one (a wired address and a wireless one), and a row
#: that could only hold one would make an operator choose which half of their
#: LAN the edge works on. Bounded because the list arrives over the wire from
#: the joining install, and an unbounded field on a pre-authorization path is a
#: store an unauthenticated peer can grow.
MAX_ENDPOINTS = 4

#: Typed outcomes of :func:`verify_peer_proof`, mirroring the device store's.
#: The reason IS the classification: the socket lane derives "does this charge
#: the auth rate limiter" from it and from nothing else.
PEER_AUTH_OK = "ok"
PEER_AUTH_UNKNOWN = "unknown_peer"
PEER_AUTH_REVOKED = "peer_revoked"
PEER_AUTH_BAD_PROOF = "bad_proof"
PEER_AUTH_MALFORMED = "hello_malformed"


# ── typed results ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PeerPairingCode:
    """A freshly minted peer code. The plaintext ``code`` exists ONLY here.

    No ``tier`` field, and its absence is the design rather than an omission: a
    peer does not hold a tier. It holds an ALLOWLIST — exactly the methods
    ``call_authorization.PEER_METHOD_ALLOWLIST`` names — so there is nothing for
    an operator to choose at pair time and therefore nothing to store. A ``tier``
    here would be a field that looked like it widened a door and did not.
    """

    code: str
    request_id: str
    note: str | None
    expires_at: float

    @property
    def ok(self) -> bool:
        return True

    def expires_in_seconds(self, *, now: float | None = None) -> int:
        return max(0, int(self.expires_at - (now if now is not None else time.time())))


@dataclass(frozen=True, slots=True)
class PeerCredential:
    """What a successful redemption hands back. ``secret`` appears once, here.

    Returned to the REDEEMING side (install A, whose code it was) so A can put
    it on the one ``hello_ok`` that carries it. The joining side (B) receives
    that frame and calls :func:`record_peer` with the same value. After those
    two writes the plaintext exists nowhere: both stores hold the digest.
    """

    peer_install_id: str
    secret: str
    display_name: str

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class PeerRecord:
    """One paired install as it is stored — WITHOUT anything secret.

    ``secret_verifier`` is deliberately not a field. Every surface that shows a
    peer to a human (``harness gateway peers list``, a log line, a refusal)
    renders one of these, so the type itself is what makes "the credential
    leaked into an operator surface" unrepresentable rather than merely
    unintended — ``DeviceRecord``'s argument, and it holds harder here because a
    peer secret is live at BOTH ends.
    """

    peer_install_id: str
    display_name: str
    endpoints: tuple[dict[str, Any], ...]
    cert_fingerprint: str | None
    approved_at: str
    last_seen: str | None
    revoked: bool
    revoked_at: str | None

    def payload(self) -> dict[str, Any]:
        return {
            "peer_install_id": self.peer_install_id,
            "display_name": self.display_name,
            "endpoints": [dict(endpoint) for endpoint in self.endpoints],
            "cert_fingerprint": self.cert_fingerprint,
            "approved_at": self.approved_at,
            "last_seen": self.last_seen,
            "revoked": self.revoked,
            "revoked_at": self.revoked_at,
        }


@dataclass(frozen=True, slots=True)
class PeerAuth:
    """The handshake's answer about one peer.

    ``record`` is present only on :data:`PEER_AUTH_OK`, so a caller cannot stamp
    a connection with a peer whose proof did not verify.
    """

    outcome: str
    peer_install_id: str | None = None
    record: PeerRecord | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == PEER_AUTH_OK


# ── paths ────────────────────────────────────────────────────────────────────


def peer_store_path(store_root: Path | str) -> Path:
    return gateway_dir(store_root) / PEER_STORE_FILENAME


# ── the derivation ───────────────────────────────────────────────────────────


def peer_secret_verifier(secret: str) -> str:
    """``sha256(secret)`` as lowercase hex — what BOTH installs store.

    ONE derivation, called by the redeeming side to compute what to store, by
    the joining side to compute the same thing, and by either side's client half
    to compute the HMAC key. A second copy of this line is how two ends start
    disagreeing about what a credential is — and here there are genuinely two
    ends holding the same secret, so the risk is not hypothetical.
    """

    return hashlib.sha256(str(secret).encode("utf-8")).hexdigest()


def peer_proof(verifier: str, nonce: str, *, port: int, peer_install_id: str) -> str:
    """The answer to a peer challenge, as lowercase hex. ONE derivation, both ends.

    ``HMAC-SHA256(key=verifier, msg="pwv<contract>|<port>|<peer_install_id>|<nonce>")``
    where ``verifier`` is ``sha256(secret)`` — the value BOTH installs store and
    the value both use as the key. The device lane needs two spellings of this
    (the phone holds a token and digests it; the install holds only the digest);
    a peer edge needs one, because the plaintext secret is discarded by both
    sides the moment the pairing frame has been read. Writing it twice here
    would be two ways to describe one symmetric key, which is how the two ends
    of an edge start disagreeing about what the credential is.

    ``peer_install_id`` is the DIALER's own install id — the name it is asking
    to be recognised under — never the id of the install it is dialling. That
    asymmetry is what lets one symmetric secret serve an edge in both
    directions: A dialling B proves as A against B's row for A, and B dialling A
    proves as B against A's row for B, with the same key and different messages.

    The three bindings are ``device_proof``'s, for its reasons, with the middle
    one carrying extra weight here. The **nonce** is fresh per connection, so a
    captured transcript is not replayable. The **port** is what each end knows
    from its OWN socket, so an impostor relaying a live challenge to the real
    service gets an answer that does not verify there. The **install id** is
    what stops a proof minted for one direction being replayed in the other:
    without it, A's proof to B and B's proof to A over the same nonce would be
    the same bytes, and a relay that bounced one back would authenticate.

    The ``pwv`` prefix — against ``device_proof``'s ``gwv`` — is the last
    guard: even if a device token and a peer secret ever collided, a device
    proof would not verify as a peer proof and the reverse.

    The key is never the message, so the proof discloses nothing about the
    credential. It does not travel, in either direction.
    """

    message = f"pwv{PEER_PROOF_CONTRACT}|{int(port)}|{str(peer_install_id)}|{nonce}"
    return hmac.new(
        str(verifier).encode("ascii"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


# ── endpoints ────────────────────────────────────────────────────────────────


def clean_endpoints(value: Any) -> tuple[dict[str, Any], ...]:
    """Coerce whatever arrived into at most :data:`MAX_ENDPOINTS` addresses.

    This runs on data the OTHER install sent, over a link where it has not yet
    proven anything — the join hello carries the address the joining side says
    it is reachable at. So every field is bounded and typed here rather than
    trusted: a host is a printable single-line string capped at 255 characters
    (the DNS name limit, which is also longer than any address), a port is an
    integer in range, and anything else in the list is dropped rather than
    stored as-is.

    Dropping rather than refusing is deliberate. A peer that offers three good
    addresses and one malformed one is a peer with three good addresses, and
    failing the whole pairing over the fourth would make an install with an
    unusual interface unpairable for a reason nobody could see.
    """

    rows: list[dict[str, Any]] = []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    for item in value:
        if not isinstance(item, dict):
            continue
        host = str(item.get("host") or "").strip()
        host = "".join(ch for ch in host if ch.isprintable() and not ch.isspace())[:255]
        if not host:
            continue
        try:
            port = int(item.get("port") or 0)
        except (TypeError, ValueError):
            continue
        if not 0 < port <= 65535:
            continue
        row = {"host": host, "port": port}
        if row not in rows:
            rows.append(row)
        if len(rows) >= MAX_ENDPOINTS:
            break
    return tuple(rows)


def _clean_fingerprint(value: Any) -> str | None:
    """A sha256 hex fingerprint, or ``None``. Never a half-parsed string.

    ``None`` rather than an empty string, because the dialer branches on
    presence: a row with no fingerprint means "pin nothing", which is a real and
    much weaker posture than "pin this". The two must not be spelled the same.
    """

    text = str(value or "").strip().lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        return None
    return text


# ── peer store: reads ────────────────────────────────────────────────────────


def list_peers(store_root: Path | str) -> list[PeerRecord]:
    """Every paired install, revoked ones included, oldest first.

    Revoked rows are KEPT and shown, for ``list_devices``' reason: a revocation
    that deleted the row would make "never paired" and "thrown out" the same
    answer, and the second is the one an operator auditing a decommissioned
    machine needs.
    """

    rows = _read_peers(store_root)
    records = [record for record in (_decode_peer(row) for row in rows.values()) if record]
    records.sort(key=lambda record: (record.approved_at, record.peer_install_id))
    return records


def lookup_peer(store_root: Path | str, peer_install_id: str) -> PeerRecord | None:
    """One peer by install id, or ``None``. Never raises; never returns a secret."""

    peer_install_id = str(peer_install_id or "").strip()
    if not peer_install_id:
        return None
    row = _read_peers(store_root).get(peer_install_id)
    return _decode_peer(row) if isinstance(row, dict) else None


def verify_peer_proof(
    store_root: Path | str,
    peer_install_id: Any,
    presented: Any,
    nonce: str,
    *,
    port: int,
) -> PeerAuth:
    """Constant-time check of a peer hello's proof. Fails CLOSED, always.

    Every missing input is a refusal rather than a degrade: no store, an unknown
    id, a revoked row, an empty nonce, a non-string proof. "Nothing configured,
    let everyone in" is the failure mode that turns a hardening into a bypass,
    and this function is reachable by an unauthenticated peer on a listener
    bound beyond loopback.

    The outcomes are DISTINGUISHED here and deliberately COLLAPSED on the wire:
    the socket lane answers every one of them with the same typed rejection, and
    with the same reason a DEVICE failure gets — so a peer cannot use the reason
    to learn whether an install id is paired, nor even which of the two
    ceremonies it just failed.
    """

    peer_install_id = str(peer_install_id or "").strip()
    if not peer_install_id or len(peer_install_id) > 128:
        return PeerAuth(outcome=PEER_AUTH_MALFORMED)
    if not isinstance(presented, str) or not presented.strip():
        return PeerAuth(outcome=PEER_AUTH_MALFORMED, peer_install_id=peer_install_id)
    if not nonce:
        return PeerAuth(outcome=PEER_AUTH_MALFORMED, peer_install_id=peer_install_id)
    row = _read_peers(store_root).get(peer_install_id)
    if not isinstance(row, dict):
        return PeerAuth(outcome=PEER_AUTH_UNKNOWN, peer_install_id=peer_install_id)
    record = _decode_peer(row)
    if record is None:
        return PeerAuth(outcome=PEER_AUTH_UNKNOWN, peer_install_id=peer_install_id)
    verifier = str(row.get("secret_verifier") or "").strip()
    if not verifier:
        return PeerAuth(outcome=PEER_AUTH_UNKNOWN, peer_install_id=peer_install_id)
    expected = peer_proof(
        verifier, nonce, port=port, peer_install_id=peer_install_id
    )
    # BYTES, never str: ``hmac.compare_digest`` RAISES TypeError when either str
    # operand is non-ASCII, and ``presented`` is attacker-controlled on a path no
    # credential is needed to reach. Both sibling verifiers carry this note and
    # the same fix; one accented character used to unwind out of the loopback
    # handshake entirely.
    if not hmac.compare_digest(
        presented.strip().lower().encode("utf-8", "replace"), expected.encode("ascii")
    ):
        return PeerAuth(outcome=PEER_AUTH_BAD_PROOF, peer_install_id=peer_install_id)
    # Revocation is checked AFTER the proof, for ``verify_device_proof``'s
    # reason: checking it first would let an unauthenticated peer probe which
    # install ids are revoked (and, by difference, which are live) while holding
    # no credential at all.
    if record.revoked:
        return PeerAuth(
            outcome=PEER_AUTH_REVOKED, peer_install_id=peer_install_id, record=record
        )
    return PeerAuth(
        outcome=PEER_AUTH_OK, peer_install_id=peer_install_id, record=record
    )


# ── peer store: writes ───────────────────────────────────────────────────────


def note_peer_seen(
    store_root: Path | str, peer_install_id: str, *, now: float | None = None
) -> None:
    """Stamp ``last_seen``. Best effort, and never in the handshake's way."""

    peer_install_id = str(peer_install_id or "").strip()
    if not peer_install_id:
        return
    stamp = _iso(now)
    try:
        with _store_lock(store_root):
            rows = _read_peers(store_root)
            row = rows.get(peer_install_id)
            if not isinstance(row, dict):
                return
            row["last_seen"] = stamp
            _write_peers(store_root, rows)
    except Exception:
        return


def revoke_peer(
    store_root: Path | str, peer_install_id: str, *, now: float | None = None
) -> PeerRecord | StoreRefusal:
    """Refuse this peer at THIS install's door. Idempotent; the row is kept.

    One-sided by design, and the CLI ack says so. See the module docstring: a
    revocation that reached across the wire would be one install writing into
    another's credential store, which is the authority R5 says an install never
    has over another.
    """

    peer_install_id = str(peer_install_id or "").strip()
    if not peer_install_id:
        return StoreRefusal("invalid_peer_id", "a peer install id is required")
    try:
        with _store_lock(store_root):
            rows = _read_peers(store_root)
            row = rows.get(peer_install_id)
            if not isinstance(row, dict):
                return StoreRefusal(
                    "unknown_peer",
                    f"no install {peer_install_id!r} is paired with this root",
                )
            if not row.get("revoked"):
                row["revoked"] = True
                row["revoked_at"] = _iso(now)
                _write_peers(store_root, rows)
            record = _decode_peer(row)
            if record is None:  # pragma: no cover - a row we just wrote
                return StoreRefusal("store_corrupt", "the peer row will not decode")
            return record
    except OSError as exc:
        return StoreRefusal(_os_reason(exc), str(exc))


def mint_peer_code(
    store_root: Path | str, *, note: str | None = None, now: float | None = None
) -> PeerPairingCode | StoreRefusal:
    """Mint a short-TTL PEER code. The plaintext is returned, never stored.

    Shares ``pairing.json``'s pending map, cap and lockout with the device
    ceremony — see ``gateway_pairing_codes`` for why the rate-limiting state is
    one and the credentials are two. The entry is stamped
    :data:`~agent_runtime.gateway_pairing_codes.KIND_PEER`, so this code cannot
    be spent on the device hello and a device code cannot be spent on the peer
    one.

    No ``tier`` argument, deliberately: a peer holds an allowlist, not a tier.
    See :class:`PeerPairingCode`.
    """

    stamp = now if now is not None else time.time()
    try:
        with _store_lock(store_root):
            state = _read_pairing(store_root)
            expire_pending(state, now=stamp)
            locked = lockout_remaining(state, now=stamp)
            if locked:
                return StoreRefusal(
                    "locked_out",
                    f"too many failed pairing attempts; retry in {locked}s",
                )
            pending = pending_codes(state)
            if len(pending) >= MAX_PENDING_CODES:
                return StoreRefusal(
                    "too_many_pending",
                    f"{len(pending)} pairing codes are already outstanding "
                    f"(max {MAX_PENDING_CODES}, counted across device and peer "
                    "codes alike); redeem or wait for them to expire",
                )
            cleaned = clean_display_name(note) or None
            code, request_id, expires_at = mint_into(
                state, kind=KIND_PEER, extra={"note": cleaned}, now=stamp
            )
            _write_pairing(store_root, state)
            return PeerPairingCode(
                code=code, request_id=request_id, note=cleaned, expires_at=expires_at
            )
    except OSError as exc:
        return StoreRefusal(_os_reason(exc), str(exc))


def redeem_peer_code(
    store_root: Path | str,
    code: str,
    *,
    peer_install_id: str,
    display_name: Any = None,
    endpoints: Any = None,
    cert_fingerprint: Any = None,
    now: float | None = None,
) -> PeerCredential | StoreRefusal:
    """Turn a peer code into an edge. The secret is returned ONCE, here.

    Run on install A — the side that minted the code — when install B dials in
    with it. A writes B's row and hands the secret back on the one ``hello_ok``
    that carries it; B writes A's row from that frame with :func:`record_peer`.

    The lockout is checked BEFORE the pending lookup, which is the correction
    ``gateway/pairing.py`` had to make (#10195), and a successful redemption
    RESETS the failure streak. Both rules are the shared discipline's; see
    ``gateway_pairing_codes``.

    **A re-pair of an install already in the store REPLACES its row**, secret
    and all, rather than being refused. That is the operator's own move — they
    minted a fresh code and ran ``join`` on the other machine — and refusing it
    would mean an install that was rebuilt, or whose secret was lost, could
    never be re-paired without an operator hand-editing JSON. What is NOT
    replaced is the id: an install keeps the ``install_id`` it minted at Stage
    0, so a re-pair updates an edge rather than creating a second one.
    """

    peer_install_id = str(peer_install_id or "").strip()
    if not peer_install_id or len(peer_install_id) > 128:
        return StoreRefusal(
            "invalid_peer_id", "the joining install named no usable install id"
        )
    stamp = now if now is not None else time.time()
    candidate = str(code or "").strip().upper()
    if not candidate:
        return StoreRefusal("invalid_code", "a pairing code is required")
    try:
        with _store_lock(store_root):
            state = _read_pairing(store_root)
            expire_pending(state, now=stamp)
            locked = lockout_remaining(state, now=stamp)
            if locked:
                _write_pairing(store_root, state)
                return StoreRefusal(
                    "locked_out",
                    f"too many failed pairing attempts; retry in {locked}s",
                )
            found = match_pending(state, candidate, kind=KIND_PEER)
            if found is None:
                note_failed_redeem(state, now=stamp)
                _write_pairing(store_root, state)
                return StoreRefusal(
                    "invalid_code", "no pending peer code matches (or it expired)"
                )
            matched_id, _matched = found

            del pending_codes(state)[matched_id]
            state["failed_redeems"] = 0
            state["locked_until"] = 0.0
            _write_pairing(store_root, state)

            secret = secrets.token_hex(PEER_SECRET_BYTES)
            name = clean_display_name(display_name) or peer_install_id
            rows = _read_peers(store_root)
            rows[peer_install_id] = _row(
                peer_install_id=peer_install_id,
                display_name=name,
                endpoints=clean_endpoints(endpoints),
                cert_fingerprint=_clean_fingerprint(cert_fingerprint),
                secret=secret,
                stamp=stamp,
            )
            _write_peers(store_root, rows)
            return PeerCredential(
                peer_install_id=peer_install_id, secret=secret, display_name=name
            )
    except OSError as exc:
        return StoreRefusal(_os_reason(exc), str(exc))


def record_peer(
    store_root: Path | str,
    *,
    peer_install_id: str,
    secret: str,
    display_name: Any = None,
    endpoints: Any = None,
    cert_fingerprint: Any = None,
    now: float | None = None,
) -> PeerRecord | StoreRefusal:
    """Write the OTHER half of the edge, on the joining install.

    Called by ``harness gateway peers join`` with what came back on the
    ``hello_ok``: the remote install's id and display name, the secret it just
    minted, and the endpoint plus fingerprint the operator pasted in — the same
    values the dialer used, so what is stored is what was proven to work rather
    than what was advertised.

    Takes the secret as an ARGUMENT rather than minting one, and that is the
    whole difference between this function and :func:`redeem_peer_code`. A peer
    edge has exactly one secret; the side that did not mint it must store the
    one it was given or the two rows describe different credentials.
    """

    peer_install_id = str(peer_install_id or "").strip()
    if not peer_install_id or len(peer_install_id) > 128:
        return StoreRefusal("invalid_peer_id", "a peer install id is required")
    if not str(secret or "").strip():
        return StoreRefusal(
            "invalid_secret", "the remote install returned no peer secret"
        )
    stamp = now if now is not None else time.time()
    try:
        with _store_lock(store_root):
            rows = _read_peers(store_root)
            rows[peer_install_id] = _row(
                peer_install_id=peer_install_id,
                display_name=clean_display_name(display_name) or peer_install_id,
                endpoints=clean_endpoints(endpoints),
                cert_fingerprint=_clean_fingerprint(cert_fingerprint),
                secret=str(secret).strip(),
                stamp=stamp,
            )
            _write_peers(store_root, rows)
            record = _decode_peer(rows[peer_install_id])
            if record is None:  # pragma: no cover - a row we just wrote
                return StoreRefusal("store_corrupt", "the peer row will not decode")
            return record
    except OSError as exc:
        return StoreRefusal(_os_reason(exc), str(exc))


# ── dialling ─────────────────────────────────────────────────────────────────


def dial_peer(
    store_root: Path | str,
    peer_install_id: str,
    *,
    client: str = "hermes-peer",
    timeout_seconds: float = 10.0,
) -> tuple[Any, dict[str, Any]]:
    """Connect to a paired install and complete the peer handshake.

    **The endpoints come from the PAIRING RECORD and from nowhere else**, which
    is the plan's Stage 6 risk line made into code: the serve registry
    (``<store_root>/serve_instances/``) names ports on THIS machine, and a
    cross-machine read of it is not merely stale, it is impossible — the file is
    on the other install's disk. So a peer's address is a fact recorded at pair
    time (and refreshed the next time that install joins), and staleness is
    handled by retry posture rather than by discovery (R8).

    Each endpoint is tried in order and the first handshake that succeeds wins.
    The certificate fingerprint is pinned from the same row: without the pin,
    TLS on a self-signed LAN link stops an eavesdropper and stops no impostor.

    Returns ``(client, hello_ok)``. The caller owns the connection and must
    close it. Raises ``ConnectionError`` when no endpoint answered — a
    TRANSPORT failure, which is the distinction Stage 7's retry posture rests
    on: unreachable is not refused.
    """

    from .serve_socket import ServeSocketClient

    root = Path(store_root)
    record = lookup_peer(root, peer_install_id)
    if record is None:
        raise ConnectionError(f"no install {peer_install_id!r} is paired with this root")
    if record.revoked:
        raise ConnectionError(
            f"install {peer_install_id!r} is revoked at this root; re-pair it first"
        )
    # The VERIFIER, not a secret: both ends of a peer edge store ``sha256(secret)``
    # and key the HMAC with it, which is what makes the edge symmetric — either
    # install can dial the other with the row it holds. The plaintext secret
    # exists only for the length of the one frame that delivered it, and neither
    # store has ever held it.
    verifier = _read_peers(root).get(record.peer_install_id, {}).get("secret_verifier")
    if not verifier:
        raise ConnectionError(f"the peer row for {peer_install_id!r} holds no credential")
    if not record.endpoints:
        raise ConnectionError(
            f"the peer row for {peer_install_id!r} names no endpoint to dial"
        )

    from .gateway_identity import read_install_identity

    identity = read_install_identity(root)
    if not identity.ok or not identity.install_id:
        raise ConnectionError(
            "this root has no install identity, so it cannot name itself to a peer"
        )

    failures: list[str] = []
    for endpoint in record.endpoints:
        connection = ServeSocketClient(
            str(endpoint["host"]),
            int(endpoint["port"]),
            timeout_seconds=timeout_seconds,
            tls=True,
            cert_fingerprint=record.cert_fingerprint,
        )
        try:
            connection.connect()
            reply = connection.peer_hello(
                peer_install_id=identity.install_id,
                verifier=str(verifier),
                client=client,
            )
        except Exception as exc:
            failures.append(f"{endpoint['host']}:{endpoint['port']} {type(exc).__name__}")
            connection.close()
            continue
        if not isinstance(reply, dict) or reply.get("event") != "hello_ok":
            failures.append(
                f"{endpoint['host']}:{endpoint['port']} "
                f"{(reply or {}).get('reason') or 'no hello_ok'}"
            )
            connection.close()
            continue
        return connection, reply
    raise ConnectionError(
        f"no endpoint on the {peer_install_id!r} row answered: {'; '.join(failures)}"
    )


# ── internals ────────────────────────────────────────────────────────────────


def _row(
    *,
    peer_install_id: str,
    display_name: str,
    endpoints: tuple[dict[str, Any], ...],
    cert_fingerprint: str | None,
    secret: str,
    stamp: float,
) -> dict[str, Any]:
    """One stored row. The ONE place a peer row's shape is written.

    Both write paths (redeem on A, record on B) go through here, so the two
    halves of an edge cannot end up with differently-shaped rows — which is the
    kind of divergence that only shows up months later, on the side nobody
    tested.
    """

    return {
        "peer_install_id": peer_install_id,
        "display_name": display_name,
        "endpoints": [dict(endpoint) for endpoint in endpoints],
        "cert_fingerprint": cert_fingerprint,
        "secret_verifier": peer_secret_verifier(secret),
        "approved_at": _iso(stamp),
        "last_seen": None,
        "revoked": False,
        "revoked_at": None,
    }


def _decode_peer(row: Any) -> PeerRecord | None:
    if not isinstance(row, dict):
        return None
    peer_install_id = str(row.get("peer_install_id") or "").strip()
    if not peer_install_id:
        return None
    return PeerRecord(
        peer_install_id=peer_install_id,
        display_name=str(row.get("display_name") or peer_install_id),
        endpoints=clean_endpoints(row.get("endpoints")),
        cert_fingerprint=_clean_fingerprint(row.get("cert_fingerprint")),
        approved_at=str(row.get("approved_at") or ""),
        last_seen=(str(row["last_seen"]) if row.get("last_seen") else None),
        revoked=bool(row.get("revoked")),
        revoked_at=(str(row["revoked_at"]) if row.get("revoked_at") else None),
    )


def _read_peers(store_root: Path | str) -> dict[str, Any]:
    payload = _read_json(peer_store_path(store_root))
    rows = payload.get("peers")
    return dict(rows) if isinstance(rows, dict) else {}


def _write_peers(store_root: Path | str, rows: dict[str, Any]) -> None:
    _write_secure(
        peer_store_path(store_root),
        {"contract": PEER_STORE_CONTRACT, "peers": rows},
    )
