"""Per-PEER credentials for the gateway listener: pair, join, verify, revoke.

The sibling of ``serve_gateway_auth``. Where that module answers "which paired
DEVICE is this" — a phone, a tablet, something with a screen and an operator
holding it — this one answers "which paired INSTALL is this": another hermes
runtime, on another machine, that an operator approved on BOTH sides.

``<store_root>/gateway/peers.json``, beside ``install.json``, ``devices.json``
and ``pairing.json``. One row per peer::

    {peer_install_id, display_name, endpoints, cert_fingerprint,
     secret_verifier, approved_at, last_seen, revoked, revoked_at, expires_at}

Two kinds of field in one row: TRUST, and CACHE (R-IP14)
---------------------------------------------------------

Every key above is one of exactly two things, and the split is worth stating
because the file's own name covers only half of it:

* **Trust** — ``peer_install_id``, ``secret_verifier``, ``approved_at``,
  ``revoked``, ``revoked_at``, ``expires_at``. Written by a ceremony
  (:func:`redeem_peer_code`, :func:`record_peer`) or by :func:`revoke_peer`, and
  by nothing else. The network never moves one of these, and that is what makes
  this a credential store rather than a directory. ``expires_at`` (S2, R-IP15 as
  amended) is trust for the sharpest version of that reason: a peer that could
  push its own expiry out would hold a credential with no end, which is exactly
  the authority ``revoked`` denies it. ``None`` means never and is what the
  manual ceremony keeps minting; a thirty-day stamp is what an ``introduce``
  mints, and BOTH ends of the edge hold the same one because it rides
  ``hello_ok.peered.expires_at`` rather than being recomputed on the far side.
* **Cache** — ``display_name``, ``endpoints``, ``cert_fingerprint``,
  ``last_seen``. What the far install TOLD us, at pairing or on a hello. The
  install itself is the authority for its own name, addresses and certificate;
  these are copies, and a copy that has gone stale is a stale copy rather than
  a wrong answer — provided every reader knows which kind it is holding.

The two sets are declared as :data:`PEER_ROW_TRUST_FIELDS` and
:data:`PEER_ROW_CACHE_FIELDS` beside :func:`_row`, and a test asserts they
partition its keys exactly — so a new field cannot be added without being
classified. That is the whole mechanism: a label nothing checks is a comment.

The honest residue, stated rather than fixed here: ``last_seen`` is a cache
fact the NETWORK writes into a trust file on every verified hello
(:func:`note_peer_seen`). S2c moves it and the other cache fields to a sidecar
(``peers_cache.json``, R-IP12a), at which point this file is trust only. Until
then the write stays exactly where it is — the frozensets are what will let
that move be mechanical rather than archaeological.

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

The peer verbs are CLI verbs and have NO wire twin: there is no ``gateway.*``
RPC method that mints, redeems, lists or revokes a peer. **S2's ``harness
gateway introduce`` is the fifth and changes nothing about that**: it is a
COMPOSITION of :func:`mint_peer_code` and
:func:`~agent_runtime.serve_gateway_auth.mint_pairing_code` in one envelope for
a launcher to post as a backend grant — two existing mints, no third ceremony,
no new store, and no method. It inherits this paragraph's residual exactly (a
local agent with a shell can run it) and adds one real narrowing the manual verb
does not have: a code minted with ``for_install_id`` is spendable only by the
install it names (:func:`redeem_peer_code`), so an intercepted introduction buys
an attacker an edge with nobody. A remote
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
import threading as _threading
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
from .serve_gateway_auth import (
    CREDENTIAL_TTL_SECONDS_INTRODUCED,
    StoreRefusal,
    _read_pairing,
    _store_lock,
    _write_pairing,
)
from .store_file_io import iso_stamp as _iso
from .store_file_io import os_error_reason as _os_reason
from .store_file_io import read_json_object as _read_json
from .store_file_io import stamp_passed as _stamp_passed
from .store_file_io import write_secure_json as _write_secure

__all__ = [
    "MAX_ENDPOINTS",
    "PEER_CACHE_CONTRACT",
    "PEER_CACHE_FILENAME",
    "PEER_CACHE_ROW_FIELDS",
    "PEER_EVENT_REACHABILITY",
    "PEER_EVENT_RECORDED",
    "PEER_EVENT_REVOKED",
    "PEER_EVENT_ROSTER",
    "PEER_EVENT_UPDATED",
    "REACHABILITY_REACHABLE",
    "REACHABILITY_UNKNOWN",
    "REACHABILITY_UNREACHABLE",
    "PeerCacheRow",
    "UsablePeer",
    "apply_peer_announce",
    "cache_peer_hello",
    "cache_peer_roster",
    "note_dial_result",
    "note_peer_store_read",
    "peer_cache_path",
    "peer_store_revision",
    "read_peer_cache",
    "usable_peers",
    "PEER_AUTH_BAD_PROOF",
    "PEER_AUTH_EXPIRED",
    "PEER_AUTH_MALFORMED",
    "PEER_AUTH_OK",
    "PEER_AUTH_REVOKED",
    "PEER_AUTH_UNKNOWN",
    "PEER_PROOF_ALGORITHM",
    "PEER_PROOF_CONTRACT",
    "PEER_ROW_CACHE_FIELDS",
    "PEER_ROW_TRUST_FIELDS",
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
#: A row whose ``expires_at`` has passed. Its own outcome rather than a second
#: spelling of ``peer_revoked``, for :data:`~agent_runtime.serve_gateway_auth.AUTH_EXPIRED`'s
#: reason: an operator re-runs a ceremony for one and does nothing for the other.
PEER_AUTH_EXPIRED = "peer_expired"


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
    #: When the edge this redemption just wrote stops working, ISO-8601 UTC, or
    #: ``None`` for never. Returned so the redeeming side can put it on the ONE
    #: ``hello_ok`` that carries the secret: the joining install has no other
    #: way to learn it, and two ends of one edge that expire on different days
    #: is precisely the divergence :func:`_row` exists to prevent.
    expires_at: str | None = None

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

    Which side each field is on (module docstring, R-IP14):

    * ``peer_install_id``, ``approved_at``, ``revoked``, ``revoked_at`` —
      TRUST. This install decided them; nothing on the wire moves them.
    * ``display_name``, ``endpoints``, ``cert_fingerprint``, ``last_seen`` —
      CACHE. ``display_name`` is the name-at-pairing (the far install's own
      word for itself, from the join hello on A or its ``install`` block on B)
      and is never refreshed here; ``endpoints`` and ``cert_fingerprint`` are
      likewise pairing-time copies, refreshed only by a re-``join``; and
      ``last_seen`` is the one the network writes on every verified hello.
    """

    peer_install_id: str
    display_name: str
    endpoints: tuple[dict[str, Any], ...]
    cert_fingerprint: str | None
    approved_at: str
    last_seen: str | None
    revoked: bool
    revoked_at: str | None
    #: TRUST, and the one new field S2 adds to this row. ``None`` means never,
    #: which is what the manual ceremony keeps writing. The far install holds
    #: the SAME value (it rides ``hello_ok.peered.expires_at``), so both ends of
    #: an edge lapse together rather than one refusing while the other keeps
    #: dialling.
    expires_at: str | None = None

    @property
    def expired(self) -> bool:
        """Has :attr:`expires_at` passed? ``False`` when there is none.

        Fails toward LIVE on an unreadable stamp — see
        ``store_file_io.stamp_passed`` for why that direction, and why it is the
        opposite of the direction ``_decode_device`` fails in for a tier.
        """

        return _stamp_passed(self.expires_at)

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
            "expires_at": self.expires_at,
            "expired": self.expired,
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
    # Expiry sits beside revocation, AFTER the proof, for the same anti-probing
    # reason and in the same order: a row that is both reports ``peer_revoked``,
    # because a decision an operator made outranks a clock. Distinguished here,
    # collapsed on the wire.
    if record.expired:
        return PeerAuth(
            outcome=PEER_AUTH_EXPIRED, peer_install_id=peer_install_id, record=record
        )
    return PeerAuth(
        outcome=PEER_AUTH_OK, peer_install_id=peer_install_id, record=record
    )


# ── peer store: writes ───────────────────────────────────────────────────────


def note_peer_seen(
    store_root: Path | str, peer_install_id: str, *, now: float | None = None
) -> None:
    """A verified hello landed. Stamps the CACHE, never the trust file.

    S2c moved this write, and the move is the point of the sidecar (R-IP12a).
    Before it, the one thing the NETWORK wrote into a credential store was this
    stamp — a cache fact in a trust file, which is exactly the confusion the
    frozensets were added to make visible. Now every field the far install is
    the authority for lives in ``peers_cache.json`` and ``peers.json`` is trust
    only, so "can the network change this?" is answered by which FILE a fact is
    in rather than by remembering a list.

    Best effort, and never in the handshake's way: a peer whose proof verified
    has authenticated whether or not this write lands.
    """

    _touch_cache(
        store_root,
        peer_install_id,
        change="last_seen",
        now=now,
        last_seen=_iso(now),
        last_hello_at=_iso(now),
        reachability=REACHABILITY_REACHABLE,
        unreachable_since=None,
    )


def revoke_peer(
    store_root: Path | str,
    peer_install_id: str,
    *,
    announced: bool = False,
    correlation: Any = None,
    now: float | None = None,
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
                _note_write(store_root)
            record = _decode_peer(row)
            if record is None:  # pragma: no cover - a row we just wrote
                return StoreRefusal("store_corrupt", "the peer row will not decode")
    except OSError as exc:
        return StoreRefusal(_os_reason(exc), str(exc))
    _emit_peer_event(
        PEER_EVENT_REVOKED,
        {
            # Whether the far side was TOLD, not merely whether we tried. An
            # operator reading this line later needs to know if the other
            # install learned at the time or will learn at its next dial.
            "peer_install_id": peer_install_id,
            "announced": bool(announced),
            **({"grant_id": str(correlation)} if correlation else {}),
        },
    )
    return record


def mint_peer_code(
    store_root: Path | str,
    *,
    note: str | None = None,
    credential_ttl_seconds: int | None = None,
    for_install_id: str | None = None,
    correlation: str | None = None,
    now: float | None = None,
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
            # Three ``introduce`` keys on the pending entry, merged UNDER the
            # fixed ones (``mint_into``'s contract), so a caller that passes
            # none of them mints the byte-identical entry a pre-S2 build did.
            #
            # ``for_install_id`` is the one that is genuinely CHECKED — see
            # :func:`redeem_peer_code`. The peer half can be scoped because the
            # join hello names the redeemer's own install id; the device half
            # cannot, and its label says so out loud rather than looking like a
            # check that never fires (R-S2-4).
            code, request_id, expires_at = mint_into(
                state,
                kind=KIND_PEER,
                extra={
                    "note": cleaned,
                    "credential_ttl_seconds": (
                        int(credential_ttl_seconds) if credential_ttl_seconds else None
                    ),
                    "for_install_id": str(for_install_id or "").strip()[:128] or None,
                    "correlation": str(correlation or "").strip()[:64] or None,
                },
                now=stamp,
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
            matched_id, matched = found

            # **The scoping check (R-S2-4), and it is a real one.** A code is a
            # bearer for its ten minutes; an ``introduce`` that named the
            # install it was minted FOR turns it into a bearer only that install
            # can spend, because the join hello has to name the redeemer's own
            # id in the same frame. A mismatch is refused as ``invalid_code``
            # and CHARGES a failure — the same answer, and the same cost, as a
            # code that does not exist — so the wrong install cannot use the
            # difference between "not for you" and "no such code" to learn that
            # a pairing is in flight.
            #
            # The pending entry is left ALONE on this path: the code was not
            # spent, so the operator's own install can still redeem it inside
            # the window rather than having to re-run the ceremony because
            # somebody else guessed at it.
            wanted_install = str(matched.get("for_install_id") or "").strip()
            if wanted_install and wanted_install != peer_install_id:
                note_failed_redeem(state, now=stamp)
                _write_pairing(store_root, state)
                return StoreRefusal(
                    "invalid_code", "no pending peer code matches (or it expired)"
                )

            del pending_codes(state)[matched_id]
            state["failed_redeems"] = 0
            state["locked_until"] = 0.0
            _write_pairing(store_root, state)

            ttl = matched.get("credential_ttl_seconds")
            try:
                ttl_seconds = int(ttl) if ttl else 0
            except (TypeError, ValueError):
                ttl_seconds = 0
            # Computed at REDEEM, not at mint: the credential starts existing
            # now, and the code's own window should not be charged against it.
            expires_at = _iso(stamp + ttl_seconds) if ttl_seconds else None

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
                expires_at=expires_at,
            )
            _write_peers(store_root, rows)
            _note_write(store_root)
            _clear_revoked_you(store_root, peer_install_id, now=stamp)
            _emit_peer_event(
                PEER_EVENT_RECORDED,
                {
                    "peer_install_id": peer_install_id,
                    # ``introduce`` when the code was scoped and correlated, else
                    # the manual ``pair`` ceremony. Derived from the pending
                    # entry rather than passed in, so the word cannot disagree
                    # with what actually minted the code.
                    "source": "introduce" if matched.get("correlation") else "pair",
                    **(
                        {"grant_id": str(matched["correlation"])}
                        if matched.get("correlation")
                        else {}
                    ),
                },
            )
            return PeerCredential(
                peer_install_id=peer_install_id,
                secret=secret,
                display_name=name,
                expires_at=expires_at,
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
    expires_at: Any = None,
    correlation: Any = None,
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
    
    ``expires_at`` travels the same way and for the same reason: it is read off
    ``hello_ok.peered`` (the redeeming side computed it) rather than recomputed
    here. A joining install that derived its own would put a second authority on
    one fact, and the two ends of an edge would lapse minutes — or, with a
    skewed clock, days — apart.
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
                expires_at=(str(expires_at).strip() or None) if expires_at else None,
            )
            _write_peers(store_root, rows)
            _note_write(store_root)
            record = _decode_peer(rows[peer_install_id])
            if record is None:  # pragma: no cover - a row we just wrote
                return StoreRefusal("store_corrupt", "the peer row will not decode")
    except OSError as exc:
        return StoreRefusal(_os_reason(exc), str(exc))
    _clear_revoked_you(store_root, peer_install_id, now=stamp)
    # Emitted OUTSIDE the lock: an EventLog append is another store's write, and
    # holding this directory's lock across it would make two unrelated stores
    # share one contention story.
    _emit_peer_event(
        PEER_EVENT_RECORDED,
        {
            "peer_install_id": peer_install_id,
            "source": "join",
            **({"grant_id": str(correlation)} if correlation else {}),
        },
    )
    return record


# ── dialling ─────────────────────────────────────────────────────────────────


def dial_peer(
    store_root: Path | str,
    peer_install_id: str,
    *,
    client: str = "hermes-peer",
    timeout_seconds: float = 10.0,
) -> tuple[Any, dict[str, Any]]:
    """Connect to a paired install and complete the peer handshake.

    **The endpoints come from THIS INSTALL'S OWN STORES and from nowhere else**,
    which is the plan's Stage 6 risk line made into code: the serve registry
    (``<store_root>/serve_instances/``) names ports on THIS machine, and a
    cross-machine read of it is not merely stale, it is impossible — the file is
    on the other install's disk. So a peer's address is a fact somebody TOLD us,
    and staleness is handled by retry posture rather than by discovery (R8).

    S2c widened *which* store, not where they come from: the cache's addresses
    (what the peer said on its last hello or in an announce) are tried first,
    then the pairing record's, deduped. The PIN is the pairing record's
    fingerprint on every attempt regardless — see the comment at the loop.

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
    # Beside the revocation and BEFORE any socket, for the revocation's reason:
    # a credential this side already knows is dead costs no attempt. Unlike the
    # verifier arms, order does not matter here — nothing is being probed, this
    # is our own store telling us not to bother.
    if record.expired:
        raise ConnectionError(
            f"the credential for install {peer_install_id!r} expired at "
            f"{record.expires_at}; an operator re-introduces the edge to renew it"
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

    # What THIS root advertises about itself, best-effort. Read here rather than
    # threaded in by every caller: a dial that could not answer "where am I
    # reachable" would silently stop refreshing the far side's cache, and the
    # symptom would be a peer that can call us until it reboots.
    self_endpoints: list[dict[str, Any]] = []
    self_fingerprint: str | None = None
    try:
        from .gateway_tls import read_certificate

        certificate = read_certificate(root)
        self_fingerprint = certificate.fingerprint if certificate.ok else None
    except Exception:
        self_fingerprint = None
    try:
        from hermes_cli.harness_parts.gateway_commands import _candidate_endpoints

        self_endpoints = _candidate_endpoints(root)
    except Exception:
        self_endpoints = []

    # **Cache endpoints FIRST, then the trust row's** (S2c, R-IP14). The cache
    # holds what the peer said most recently — on its last hello, or in an
    # announce after it changed networks — and the trust row holds what it said
    # at pairing time. Trying the fresher one first is what lets a laptop that
    # moved keep working without an operator re-running a ceremony; keeping the
    # pairing-time one as a fallback is what stops a bad announce from cutting
    # an edge that still works at its old address.
    #
    # **The PIN is always the trust row's**, on every attempt, whichever list
    # the address came from. An announced fingerprint is a notice
    # (``fingerprint_rotation``) an operator reads, never a value a dial adopts:
    # a peer that could nominate the certificate it is checked against could
    # become a different machine, which is the one thing pinning exists to
    # prevent.
    cached = read_peer_cache(root).get(record.peer_install_id)
    candidates: list[dict[str, Any]] = []
    for endpoint in ((cached.endpoints if cached is not None else ()) + record.endpoints):
        row = {"host": str(endpoint["host"]), "port": int(endpoint["port"])}
        if row not in candidates:
            candidates.append(row)

    failures: list[str] = []
    for endpoint in candidates:
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
                # S2c: this root's own current facts, so the FAR side's cache is
                # refreshed by every hello rather than only by a re-``join``.
                # Optional on the frame and ignored by a peer that predates
                # them, which is what makes the refresh additive.
                display_name=identity.display_name,
                endpoints=self_endpoints,
                cert_fingerprint=self_fingerprint,
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
        note_dial_result(root, record.peer_install_id, ok=True)
        return connection, reply
    detail = "; ".join(failures)
    note_dial_result(root, record.peer_install_id, ok=False, error=detail)
    raise ConnectionError(
        f"no endpoint on the {peer_install_id!r} row answered: {detail}"
    )


# ── internals ────────────────────────────────────────────────────────────────


#: The row's TRUST half: written by a ceremony or by :func:`revoke_peer`, never
#: by the network. See the module docstring (R-IP14).
PEER_ROW_TRUST_FIELDS = frozenset(
    {
        "peer_install_id",
        "secret_verifier",
        "approved_at",
        "revoked",
        "revoked_at",
        # S2. TRUST and not cache, and the classification is the argument: THIS
        # install decided the lifetime at mint time (or was handed it once, on
        # the one frame that carries the secret), and no later hello may move
        # it. A peer that could push its own expiry out would hold a credential
        # with no end — which is the same authority ``revoked`` denies it.
        "expires_at",
    }
)

#: The row's CACHE half: what the far install told us, at pairing or on a
#: hello. The install is the authority for each of these about ITSELF; this is
#: a copy, and S2c moves the copies to ``peers_cache.json`` (R-IP12a).
PEER_ROW_CACHE_FIELDS = frozenset(
    {"display_name", "endpoints", "cert_fingerprint"}
)


def _row(
    *,
    peer_install_id: str,
    display_name: str,
    endpoints: tuple[dict[str, Any], ...],
    cert_fingerprint: str | None,
    secret: str,
    stamp: float,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """One stored row. The ONE place a peer row's shape is written.

    Both write paths (redeem on A, record on B) go through here, so the two
    halves of an edge cannot end up with differently-shaped rows — which is the
    kind of divergence that only shows up months later, on the side nobody
    tested.

    Its keys are exactly :data:`PEER_ROW_TRUST_FIELDS` ∪
    :data:`PEER_ROW_CACHE_FIELDS`, asserted in
    ``tests/agent_runtime/test_gateway_peers_store.py``. A field added here
    without being classified fails that test, which is the point: R-IP14's rule
    is that a fact has one authority and every other copy is a labelled cache,
    and the label has to be machine-readable for S2c's sidecar to be a move
    rather than a re-derivation.
    """

    return {
        "peer_install_id": peer_install_id,
        "display_name": display_name,
        "endpoints": [dict(endpoint) for endpoint in endpoints],
        "cert_fingerprint": cert_fingerprint,
        "secret_verifier": peer_secret_verifier(secret),
        "approved_at": _iso(stamp),
        "revoked": False,
        "revoked_at": None,
        "expires_at": expires_at,
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
        # LEGACY ONLY. S2c moved this fact to ``peers_cache.json``; ``_row`` no
        # longer writes it and :func:`note_peer_seen` no longer touches this
        # file. A row written by a pre-S2c build still carries a value and it is
        # still shown, because deleting a fact an operator can already see is a
        # worse migration than reading one nothing writes. New rows answer
        # ``None`` here and the live stamp is ``cache.last_seen``.
        last_seen=(str(row["last_seen"]) if row.get("last_seen") else None),
        revoked=bool(row.get("revoked")),
        revoked_at=(str(row["revoked_at"]) if row.get("revoked_at") else None),
        # Absent reads as "never expires", so every row a pre-S2 build wrote
        # keeps working untouched. There is no migration pass because the only
        # new fact has a legal absent value.
        expires_at=(str(row["expires_at"]) if row.get("expires_at") else None),
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


# ══ the cache sidecar (S2c, R-IP12a / R-S2-7) ════════════════════════════════
#
# ``<store_root>/gateway/peers_cache.json``, beside ``peers.json`` and under the
# same directory lock. Two files, and the split is the whole mechanism:
#
#   peers.json        what THIS install decided.  Trust.  Written by a ceremony.
#   peers_cache.json  what the NETWORK told us.    Cache.  Written by a hello,
#                                                          a dial, an announce.
#
# The frozensets S0b added made the classification machine-readable; this makes
# it structural. A reader no longer has to remember which keys the network may
# move — it reads the file the fact is in. And a cache writer physically cannot
# reach a credential, because no function below opens ``peers.json`` for writing
# (asserted in ``test_gateway_peers_store.py``).
#
# **Pushed, never polled** (R-IP12). Nothing here runs on a timer. Every row is
# written by an edge that already existed: a verified hello, a dial that
# succeeded or failed, a roster the tool just fetched, an announce a peer sent.
# The one thing that is not push-shaped is an EXTERNAL write — an editor, or a
# build that predates this file — and that is what :func:`peer_store_revision`
# and the process-local memo below are sized for. A stat, not a poll.

PEER_CACHE_FILENAME = "peers_cache.json"
PEER_CACHE_CONTRACT = 1

#: Three words and no fourth. ``unknown`` is the honest state of a row nothing
#: has dialled or heard from yet, and it is DIFFERENT from ``unreachable``: one
#: means "we have not tried", the other means "we tried and it did not answer",
#: and an operator acts differently on each. A boolean would have collapsed them.
REACHABILITY_UNKNOWN = "unknown"
REACHABILITY_REACHABLE = "reachable"
REACHABILITY_UNREACHABLE = "unreachable"

#: The cache row's keys, declared for the same reason the trust row's are: a
#: field added here without being listed fails ``_cache_row``'s partition test,
#: so "which file owns this fact" stays a decision somebody makes rather than a
#: side effect of where a line was typed.
PEER_CACHE_ROW_FIELDS = frozenset(
    {
        "peer_install_id",
        "announced_display_name",
        "endpoints",
        "cert_fingerprint",
        "last_seen",
        "last_hello_at",
        "reachability",
        "unreachable_since",
        "roster",
        "revoked_you",
        "revoked_you_at",
        "fingerprint_rotation",
        "last_announce_at",
        "correlation",
    }
)

#: The five ``gateway.peer.*`` types, as module-level constants. Constants and
#: not literals at the call sites, because the S55 emitter gate resolves a
#: module-level string binding — and because a type spelled twice is a type that
#: eventually gets spelled differently once.
PEER_EVENT_RECORDED = "gateway.peer.recorded"
PEER_EVENT_REVOKED = "gateway.peer.revoked"
PEER_EVENT_UPDATED = "gateway.peer.updated"
PEER_EVENT_ROSTER = "gateway.peer.roster"
PEER_EVENT_REACHABILITY = "gateway.peer.reachability"

#: How many roster rows one cached peer keeps. The HUD shows eight; this is the
#: store's own ceiling so a far install with two hundred agents cannot grow this
#: file without bound through an edge that is supposed to be read-only.
PEER_CACHE_ROSTER_CAP = 64


@dataclass(frozen=True, slots=True)
class PeerCacheRow:
    """What one paired install has TOLD us, as it is cached.

    Every field is the far install's own claim about itself, or this install's
    own observation of trying to reach it. Nothing here is a credential and
    nothing here is consulted by :func:`verify_peer_proof` — which is what makes
    it safe for a peer to write (through :func:`apply_peer_announce`) at all.

    Two fields need their asymmetry stated because a reader would otherwise
    assume symmetry:

    * ``cert_fingerprint`` is the fingerprint the peer ANNOUNCED. The pin a dial
      uses is always the TRUST row's. A rotation is recorded in
      ``fingerprint_rotation`` and shown to an operator; it is never applied,
      because a peer that could rotate its own pin could rotate it to a
      certificate it does not hold and become a different machine (S0b B2 —
      re-pair is the cure).
    * ``revoked_you`` is ONE-WAY. An announce may set it; only a trust write
      (a re-``join``, a re-``redeem``) clears it. An un-revoke that arrived over
      the wire would be an install granting itself access it had been refused.
    """

    peer_install_id: str
    announced_display_name: str | None = None
    endpoints: tuple[dict[str, Any], ...] = ()
    cert_fingerprint: str | None = None
    last_seen: str | None = None
    last_hello_at: str | None = None
    reachability: str = REACHABILITY_UNKNOWN
    unreachable_since: str | None = None
    roster: dict[str, Any] | None = None
    revoked_you: bool = False
    revoked_you_at: str | None = None
    fingerprint_rotation: dict[str, Any] | None = None
    last_announce_at: str | None = None
    correlation: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "peer_install_id": self.peer_install_id,
            "announced_display_name": self.announced_display_name,
            "endpoints": [dict(endpoint) for endpoint in self.endpoints],
            "cert_fingerprint": self.cert_fingerprint,
            "last_seen": self.last_seen,
            "last_hello_at": self.last_hello_at,
            "reachability": self.reachability,
            "unreachable_since": self.unreachable_since,
            "roster": dict(self.roster) if isinstance(self.roster, dict) else None,
            "revoked_you": self.revoked_you,
            "revoked_you_at": self.revoked_you_at,
            "fingerprint_rotation": (
                dict(self.fingerprint_rotation)
                if isinstance(self.fingerprint_rotation, dict)
                else None
            ),
            "last_announce_at": self.last_announce_at,
            "correlation": self.correlation,
        }


@dataclass(frozen=True, slots=True)
class UsablePeer:
    """One peer an address could actually reach, with the spelling that reaches it.

    THE predicate's row type (R-S2-16). Before it, three surfaces each decided
    "is this peer usable" for themselves — the resolver checked ``revoked``, the
    HUD listed everything, and a tool would have had to invent a third rule — so
    an operator could see a peer in one place and be refused it in another with
    no way to tell which was right.
    """

    record: PeerRecord
    cache: PeerCacheRow | None
    #: How this peer must be SPELLED in an ``@install/target`` to resolve: the
    #: display name when it is unique among usable peers, otherwise the install
    #: id. The line an operator reads is therefore always an address a send
    #: would accept, rather than a name the resolver would refuse as ambiguous.
    ref: str

    @property
    def peer_install_id(self) -> str:
        return self.record.peer_install_id


def peer_cache_path(store_root: Path | str) -> Path:
    return gateway_dir(store_root) / PEER_CACHE_FILENAME


def read_peer_cache(store_root: Path | str) -> dict[str, PeerCacheRow]:
    """Every cached peer row, keyed by install id. Never raises.

    Reads the revision as it goes (R-S2-8), so a process that has been handed a
    file somebody edited notices exactly once.
    """

    note_peer_store_read(store_root)
    rows = _read_cache_rows(store_root)
    decoded: dict[str, PeerCacheRow] = {}
    for peer_install_id, row in rows.items():
        cached = _decode_cache(peer_install_id, row)
        if cached is not None:
            decoded[cached.peer_install_id] = cached
    return decoded


def cache_peer_hello(
    store_root: Path | str,
    peer_install_id: str,
    *,
    display_name: Any = None,
    endpoints: Any = None,
    cert_fingerprint: Any = None,
    now: float | None = None,
) -> None:
    """Refresh what a peer says about itself, from a VERIFIED hello.

    Every hello, not only a join. Before this the name, addresses and
    fingerprint were pairing-time snapshots refreshed only by a re-``join``, so
    an install that moved networks became unreachable until an operator re-ran a
    ceremony they had no reason to suspect was needed.

    The fingerprint here is the ANNOUNCED one and never the pin: see
    :class:`PeerCacheRow`. A change is recorded as a rotation notice for an
    operator to act on.
    """

    changes: dict[str, Any] = {
        "last_hello_at": _iso(now),
        "reachability": REACHABILITY_REACHABLE,
        "unreachable_since": None,
    }
    cleaned_name = clean_display_name(display_name) or None
    if cleaned_name:
        changes["announced_display_name"] = cleaned_name
    cleaned_endpoints = clean_endpoints(endpoints)
    if cleaned_endpoints:
        changes["endpoints"] = [dict(endpoint) for endpoint in cleaned_endpoints]
    announced = _clean_fingerprint(cert_fingerprint)
    if announced:
        changes["cert_fingerprint"] = announced
        rotation = _rotation_notice(store_root, peer_install_id, announced, now=now)
        if rotation is not None:
            changes["fingerprint_rotation"] = rotation
    _touch_cache(store_root, peer_install_id, change="display_name", now=now, **changes)


def note_dial_result(
    store_root: Path | str,
    peer_install_id: str,
    *,
    ok: bool,
    error: Any = None,
    now: float | None = None,
) -> None:
    """Record what happened when we tried to reach this peer.

    ``unreachable_since`` is set on the FIRST failure and left alone on the
    ones after it, so "down for twelve minutes" is answerable. A field that was
    restamped on every retry would only ever say "down since the last attempt",
    which is a number nobody can act on.
    """

    if ok:
        _touch_cache(
            store_root,
            peer_install_id,
            change="reachability",
            now=now,
            reachability=REACHABILITY_REACHABLE,
            unreachable_since=None,
        )
        return
    existing = _decode_cache(
        peer_install_id, _read_cache_rows(store_root).get(str(peer_install_id).strip())
    )
    since = (
        existing.unreachable_since
        if existing is not None
        and existing.reachability == REACHABILITY_UNREACHABLE
        and existing.unreachable_since
        else _iso(now)
    )
    _touch_cache(
        store_root,
        peer_install_id,
        change="reachability",
        now=now,
        error=str(error or "")[:200] or None,
        reachability=REACHABILITY_UNREACHABLE,
        unreachable_since=since,
    )


def cache_peer_roster(
    store_root: Path | str,
    peer_install_id: str,
    *,
    workspace_id: Any = None,
    rows: Any = None,
    now: float | None = None,
) -> None:
    """Keep the roster a peer just handed us, so the HUD never has to dial.

    Bounded at :data:`PEER_CACHE_ROSTER_CAP` and stamped with when it was
    fetched, because a roster with no ``fetched_at`` is a claim about the
    present made from an unknown past — and the HUD renders an age from it.
    """

    kept = []
    for row in list(rows or ())[:PEER_CACHE_ROSTER_CAP]:
        if isinstance(row, dict):
            kept.append(dict(row))
    _touch_cache(
        store_root,
        peer_install_id,
        change="roster",
        now=now,
        event_type=PEER_EVENT_ROSTER,
        event_payload={"count": len(kept)},
        event_detail={
            "workspace_id": str(workspace_id or "") or None,
            "fetched_at": _iso(now),
        },
        roster={
            "fetched_at": _iso(now),
            "workspace_id": str(workspace_id or "") or None,
            "rows": kept,
        },
    )


def apply_peer_announce(
    store_root: Path | str,
    caller_peer_install_id: str,
    payload: Any,
    *,
    now: float | None = None,
) -> list[str]:
    """Apply one ``peer.announce`` to the CALLER's own cache row. Returns the fields written.

    **The row written is the CALLER's, and there is no parameter that could say
    otherwise** (R-S2-9). The install id is taken positionally from what the
    transport proved; a payload that names a different install is refused by the
    handler before reaching here, and one that names its own is simply ignored.
    That is ``normalize_peer_chat_execute``'s posture, and its reason: the field
    a peer could type does not exist.

    Three things this cannot do, and each is a property of the code rather than
    a rule: it cannot write a credential (it opens only the cache file), it
    cannot clear ``revoked_you`` or ``revoked`` (both are cleared by a trust
    write alone), and it cannot move the dial pin (an announced fingerprint
    becomes a rotation NOTICE beside the pin, never the pin).
    """

    caller = str(caller_peer_install_id or "").strip()
    if not caller or not isinstance(payload, dict):
        return []

    changes: dict[str, Any] = {"last_announce_at": _iso(now)}
    written: list[str] = []

    name = clean_display_name(payload.get("display_name")) or None
    if name:
        changes["announced_display_name"] = name
        written.append("announced_display_name")

    endpoints = clean_endpoints(payload.get("endpoints"))
    if endpoints:
        changes["endpoints"] = [dict(endpoint) for endpoint in endpoints]
        written.append("endpoints")

    announced = _clean_fingerprint(payload.get("cert_fingerprint"))
    if announced:
        changes["cert_fingerprint"] = announced
        written.append("cert_fingerprint")
        rotation = _rotation_notice(store_root, caller, announced, now=now)
        if rotation is not None:
            changes["fingerprint_rotation"] = rotation
            written.append("fingerprint_rotation")

    if payload.get("roster_changed") is True:
        # DROPPED, not refreshed: this edge carries a notification, never a
        # roster body. Fetching one here would make an inbound announce trigger
        # an outbound dial, which is a loop with two installs in it.
        changes["roster"] = None
        written.append("roster")

    if payload.get("revoked_you") is True:
        changes["revoked_you"] = True
        changes["revoked_you_at"] = _iso(now)
        written.extend(["revoked_you", "revoked_you_at"])

    correlation = str(payload.get("correlation_id") or "").strip()[:64]
    if correlation:
        changes["correlation"] = correlation

    _touch_cache(
        store_root,
        caller,
        change="display_name" if name else "endpoints",
        now=now,
        **changes,
    )
    return written


# ── the predicate every reader shares ────────────────────────────────────────


def usable_peers(store_root: Path | str) -> list[UsablePeer]:
    """The peers an address could actually reach, oldest edge first.

    THE predicate (R-S2-16). Three conditions, and each is a different way for
    an edge to be dead:

    * ``revoked`` — this operator threw the peer out.
    * ``expired`` — the credential lapsed (S2).
    * ``cache.revoked_you`` — the FAR operator threw us out and said so
      (S2c's announce). Learning this from the cache is the whole point of the
      push edge: before it, a revoke on the far side was discovered as the next
      send's refusal, minutes or hours later, by an agent that had already
      written the message.

    ``ref`` is computed across the whole usable set rather than per row, because
    "is this name unique" is a question about the SET. A row that answered it
    alone would hand out a display name that the resolver then refuses as
    ambiguous — a spelling the system itself printed and will not accept.
    """

    records = [record for record in list_peers(store_root)]
    cache = read_peer_cache(store_root)
    live = [
        record
        for record in records
        if not record.revoked
        and not record.expired
        and not (
            (cache.get(record.peer_install_id) or PeerCacheRow("")).revoked_you
        )
    ]
    names: dict[str, int] = {}
    for record in live:
        folded = (record.display_name or "").casefold()
        names[folded] = names.get(folded, 0) + 1
    rows: list[UsablePeer] = []
    for record in live:
        folded = (record.display_name or "").casefold()
        unique = bool(record.display_name) and names.get(folded, 0) == 1
        rows.append(
            UsablePeer(
                record=record,
                cache=cache.get(record.peer_install_id),
                ref=record.display_name if unique else record.peer_install_id,
            )
        )
    return rows


# ── the revision memo (R-S2-8) ───────────────────────────────────────────────
#
# Every write door below emits its own event from its OWN process, which is the
# ``realm_sync`` precedent already working: a CLI ``peers join`` beside a running
# serve advances the EventLog watermark and the serve's stream picks it up with
# no restart and no check. So the revision memo has exactly ONE job left — a
# write that emitted NOTHING. An editor on ``peers.json``; a binary that predates
# S2c. It is a stat taken on reads that were happening anyway, never a timer.

#: Per-process, keyed by the resolved store root. Not a cache of CONTENT — the
#: files are re-read every time — only of "what revision had this process seen".
_LAST_SEEN_REVISION: dict[str, tuple[int, int]] = {}

#: **In-process mutual exclusion for the cache's read-modify-write**, on top of
#: the cross-process file lock and not instead of it.
#:
#: ``store_file_io.store_lock`` documents that it falls through WITHOUT the lock
#: rather than raising when it cannot be taken, and argues correctly that the
#: cost is a lost update rather than a corrupt store. For the credential stores
#: that trade is right: their writers are ceremonies an operator runs one at a
#: time. The CACHE has a different writer set — a handshake on the listener
#: thread, a dial from a tool, an announce fan-out on a background thread, all
#: inside ONE serve process and all merging into one row — so a lost update here
#: is not theoretical, it is what a boot-time announce racing the first hello
#: does, and it silently drops the field the other writer had just set.
#:
#: A module-level lock is the whole fix because every write goes through
#: :func:`_touch_cache`. Held only across the read-modify-write, never across an
#: event append or a dial.
_CACHE_WRITE_LOCK = _threading.Lock()


def peer_store_revision(store_root: Path | str) -> tuple[int, int]:
    """``(trust_mtime_ns, cache_mtime_ns)``; ``0`` for a file that is absent.

    Modification time and not a hash, deliberately: this question is asked on
    every gateway hello and every peer call, and hashing two files on that path
    would put I/O proportional to store size on the handshake. An mtime that did
    not move for a write is a filesystem this repo has bigger problems with.
    """

    def _mtime(path: Path) -> int:
        try:
            return int(path.stat().st_mtime_ns)
        except OSError:
            return 0

    return _mtime(peer_store_path(store_root)), _mtime(peer_cache_path(store_root))


def note_peer_store_read(store_root: Path | str) -> None:
    """Record the revision this process is reading, and emit once if it is new.

    A FRESH process seeds on its first read and emits nothing: it has no
    baseline, so "this changed" is not a claim it can make. A long-lived process
    — the serve, which reads on every hello, every ``peer.*`` call and every
    connections frame — is therefore the one that notices, which is exactly the
    process a stream consumer is attached to.
    """

    key = str(Path(store_root))
    revision = peer_store_revision(store_root)
    previous = _LAST_SEEN_REVISION.get(key)
    _LAST_SEEN_REVISION[key] = revision
    if previous is None or previous == revision:
        return
    store = "trust" if previous[0] != revision[0] else "cache"
    _emit_peer_event(
        PEER_EVENT_UPDATED,
        {
            "store": store,
            "change": "external_write",
            "store_revision": list(revision),
        },
    )


def _note_write(store_root: Path | str) -> None:
    """Adopt the revision THIS process just wrote, so it never reports itself."""

    _LAST_SEEN_REVISION[str(Path(store_root))] = peer_store_revision(store_root)


# ── cache internals ──────────────────────────────────────────────────────────


def _cache_row(peer_install_id: str, fields: dict[str, Any] | None = None) -> dict[str, Any]:
    """One stored cache row. The ONE place its shape is written.

    Its keys are exactly :data:`PEER_CACHE_ROW_FIELDS`, asserted in
    ``test_gateway_peers_store.py`` — so a field added here without being
    classified fails, which is the trust row's rule applied to the other half of
    the split.

    ``fields`` is a DICT and not ``**kwargs``, and that is a bug fix wearing a
    signature. The caller merges the row it read with the fields it is changing
    and passes the result; a splat made ``peer_install_id`` — which is a key in
    every stored row — collide with the positional parameter, so the first write
    to a fresh row succeeded (nothing to merge) and every write after it raised
    ``TypeError`` into :func:`_touch_cache`'s best-effort ``except`` and
    silently did nothing. A dict cannot collide, and the id stays positional
    because it is the one field a caller must not be able to change by merging.
    """

    row: dict[str, Any] = {
        "peer_install_id": peer_install_id,
        "announced_display_name": None,
        "endpoints": [],
        "cert_fingerprint": None,
        "last_seen": None,
        "last_hello_at": None,
        "reachability": REACHABILITY_UNKNOWN,
        "unreachable_since": None,
        "roster": None,
        "revoked_you": False,
        "revoked_you_at": None,
        "fingerprint_rotation": None,
        "last_announce_at": None,
        "correlation": None,
    }
    for key, value in (fields or {}).items():
        if key in PEER_CACHE_ROW_FIELDS:
            row[key] = value
    # Always the id the CALLER named, never one a merged row carried: a cache
    # row that could rename itself through a merge would be a row addressed by
    # one install and stored under another.
    row["peer_install_id"] = peer_install_id
    return row


def _decode_cache(peer_install_id: Any, row: Any) -> PeerCacheRow | None:
    if not isinstance(row, dict):
        return None
    resolved = str(row.get("peer_install_id") or peer_install_id or "").strip()
    if not resolved:
        return None
    reachability = str(row.get("reachability") or REACHABILITY_UNKNOWN)
    if reachability not in {
        REACHABILITY_UNKNOWN,
        REACHABILITY_REACHABLE,
        REACHABILITY_UNREACHABLE,
    }:
        # A word this build does not know reads as ``unknown``, never as
        # ``reachable``: the safe direction for a reachability claim is "we do
        # not know", because the other one is a dial an operator was promised.
        reachability = REACHABILITY_UNKNOWN
    return PeerCacheRow(
        peer_install_id=resolved,
        announced_display_name=(
            str(row["announced_display_name"])
            if row.get("announced_display_name")
            else None
        ),
        endpoints=clean_endpoints(row.get("endpoints")),
        cert_fingerprint=_clean_fingerprint(row.get("cert_fingerprint")),
        last_seen=(str(row["last_seen"]) if row.get("last_seen") else None),
        last_hello_at=(str(row["last_hello_at"]) if row.get("last_hello_at") else None),
        reachability=reachability,
        unreachable_since=(
            str(row["unreachable_since"]) if row.get("unreachable_since") else None
        ),
        roster=row["roster"] if isinstance(row.get("roster"), dict) else None,
        revoked_you=bool(row.get("revoked_you")),
        revoked_you_at=(
            str(row["revoked_you_at"]) if row.get("revoked_you_at") else None
        ),
        fingerprint_rotation=(
            row["fingerprint_rotation"]
            if isinstance(row.get("fingerprint_rotation"), dict)
            else None
        ),
        last_announce_at=(
            str(row["last_announce_at"]) if row.get("last_announce_at") else None
        ),
        correlation=(str(row["correlation"]) if row.get("correlation") else None),
    )


def _read_cache_rows(store_root: Path | str) -> dict[str, Any]:
    payload = _read_json(peer_cache_path(store_root))
    rows = payload.get("peers")
    return dict(rows) if isinstance(rows, dict) else {}


def _write_peer_cache(store_root: Path | str, rows: dict[str, Any]) -> None:
    _write_secure(
        peer_cache_path(store_root),
        {"contract": PEER_CACHE_CONTRACT, "peers": rows},
    )


def _clear_revoked_you(
    store_root: Path | str, peer_install_id: str, *, now: float | None = None
) -> None:
    """The ONE exit from ``revoked_you`` (R-S2-9), taken by a TRUST write.

    ``revoked_you`` is one-way against the wire — an announce may set it and no
    announce may clear it — because an install that could announce itself back
    into an edge would have granted itself access an operator refused. But a
    one-way flag with no exit at all would mean a re-pair produced an edge that
    every reader still treated as dead.

    So the exit is exactly the ceremony that re-establishes trust: the far
    operator paired again, which is the same authority that could revoke. Called
    from :func:`redeem_peer_code` and :func:`record_peer` — the two functions
    that write a credential — and from nowhere else, so "cleared by a trust
    write" is a property of the call graph rather than a comment.

    A no-op when there is no cache row: nothing to clear is not a failure.
    """

    rows = _read_cache_rows(store_root)
    existing = rows.get(str(peer_install_id).strip())
    if not isinstance(existing, dict) or not existing.get("revoked_you"):
        return
    _touch_cache(
        store_root,
        peer_install_id,
        change="display_name",
        now=now,
        revoked_you=False,
        revoked_you_at=None,
    )


def _rotation_notice(
    store_root: Path | str,
    peer_install_id: str,
    announced: str,
    *,
    now: float | None,
) -> dict[str, Any] | None:
    """A fingerprint that disagrees with the PIN, as a notice. Never applied.

    ``None`` when it agrees, or when there is no pin to disagree with. The
    notice is what an operator reads to decide whether to re-pair; applying it
    would let a peer nominate the certificate it is authenticated against, which
    is the one thing pinning exists to prevent.
    """

    record = lookup_peer(store_root, peer_install_id)
    pin = record.cert_fingerprint if record is not None else None
    if not pin or pin == announced:
        return None
    return {"announced_at": _iso(now), "new_fingerprint": announced}


def _touch_cache(
    store_root: Path | str,
    peer_install_id: Any,
    *,
    change: str,
    now: float | None = None,
    event_type: str | None = None,
    event_payload: dict[str, Any] | None = None,
    event_detail: dict[str, Any] | None = None,
    error: Any = None,
    **fields: Any,
) -> None:
    """Merge *fields* into one cache row, write, and emit. Never raises.

    The ONE write door for the sidecar, which is what makes "no cache writer can
    change a trust field" checkable rather than promised: there is exactly one
    function that opens this file for writing, and it opens no other.

    Best effort throughout. Every caller is on a path where the bookkeeping is
    not the work — a verified hello, a completed dial, an accepted announce —
    and a store that will not write must not be the thing that fails an
    authentication.
    """

    resolved = str(peer_install_id or "").strip()
    if not resolved:
        return
    previous_reachability = None
    try:
        with _CACHE_WRITE_LOCK, _store_lock(store_root):
            rows = _read_cache_rows(store_root)
            existing = rows.get(resolved)
            previous = _decode_cache(resolved, existing)
            previous_reachability = (
                previous.reachability if previous is not None else None
            )
            merged = dict(existing) if isinstance(existing, dict) else {}
            merged.update(fields)
            rows[resolved] = _cache_row(resolved, merged)
            _write_peer_cache(store_root, rows)
            _note_write(store_root)
    except Exception:
        return

    if event_type is not None:
        payload = {"peer_install_id": resolved, **(event_payload or {})}
        payload.update({k: v for k, v in (event_detail or {}).items() if v is not None})
        _emit_peer_event(event_type, payload)
        return

    reachability = fields.get("reachability")
    if reachability is not None and reachability != previous_reachability:
        # Emitted on a CHANGE OF WORD only. A peer that answers every thirty
        # seconds would otherwise write one event per hello for a fact that did
        # not move, which is the shape that makes an event log unreadable.
        detail = {
            "peer_install_id": resolved,
            "reachability": reachability,
            "unreachable_since": fields.get("unreachable_since"),
        }
        if error:
            detail["error"] = str(error)[:200]
        _emit_peer_event(PEER_EVENT_REACHABILITY, detail)
        return

    _emit_peer_event(
        PEER_EVENT_UPDATED,
        {"store": "cache", "change": change, "peer_install_id": resolved},
    )


def _emit_peer_event(event_type: str, payload: dict[str, Any]) -> None:
    """Append one ``gateway.peer.*`` event from THIS process. Best effort.

    The ``realm_sync._append_realm_sync_event`` precedent, and its reason: the
    stream/read-model pipeline is watermark-gated on the EventLog, so a store
    write that emits nothing is invisible to every consumer until an unrelated
    event happens to advance the offset. Emitting from the writing process — a
    CLI ``peers join``, the serve's own authenticator — is what makes a join
    beside a running serve visible with no restart.

    **Never a secret, an endpoint or a roster body.** Ids and counts only: the
    payload cap is 4096 bytes and an event log is a place facts go to be read
    later by people who were not there. What the row holds is in the row.
    """

    from hermes_time import now

    from .events import EventLog
    from .models import Event

    try:
        EventLog().append(Event(now(), event_type, None, None, None, dict(payload)))
    except Exception:  # noqa: BLE001 — an evidence channel, never the mutation
        pass

    # S2d. The SAME call site feeds the launcher's push lane, because the
    # launcher's hermes stream carries no events at all (its hydrate core and
    # fold entities have no room for one) and canon 03 invariant 6 routes new
    # server→client push over JSON-RPC notifications instead. Emitting both
    # from here is what stops the two lanes disagreeing about WHEN something
    # changed: one write, one process, one moment.
    #
    # Imported lazily and guarded: this is a credential store, and it must not
    # take a hard dependency on the serve's RPC surface — a CLI ``peers join``
    # runs this function in a process where nobody is subscribed to anything.
    try:
        from .serve_gateway_peers_rpc import publish_peer_event

        publish_peer_event(event_type, dict(payload))
    except Exception:  # noqa: BLE001 — a notification is never the mutation
        pass
