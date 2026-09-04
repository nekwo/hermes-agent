"""Per-DEVICE credentials for the gateway listener: pair, verify, revoke.

Where ``serve_auth.py`` mints ONE secret per runtime root — "whoever holds this
is the machine owner" — this module mints one per paired DEVICE, so that
"whoever holds this" finally has a name, a tier, and a revocation. That is the
whole of the gateway plan's Stage 1 credential half and of the authorization
chokepoint's Stage A5: the front-door gate (``call_authorization.py``) already
asks "may this caller run this tier?"; until now the socket lane had one answer
for every peer because it had one secret.

Two files, both under ``<store_root>/gateway/`` beside ``install.json``:

* ``devices.json`` — the paired devices. One row each:
  ``{device_id, name, tier, verifier, created_at, last_seen, revoked,
  revoked_at, expires_at, account_device_id}``. Two kinds of field in that row
  (R-IP14; ``gateway_peers``' module docstring states the same split in full):
  ``name`` and ``last_seen`` are CACHE facts — what the device called itself at
  redemption, and when the network last saw it — while ``device_id``, ``tier``,
  ``verifier``, ``created_at``, ``expires_at`` and the two revocation fields are
  TRUST, written by the ceremony or by a revoke and never by a hello.
  ``account_device_id`` is a LABEL a hello never writes either: it is copied
  from the pending entry an ``introduce`` scoped, and nothing authenticates
  against it (:class:`DeviceRecord`).

  **``expires_at`` is S2's one new credential rule** (R-IP15 as amended).
  ``None`` means never, and that is what every manual ``harness gateway pair``
  keeps minting; a code minted by ``harness gateway introduce`` carries
  :data:`CREDENTIAL_TTL_SECONDS_INTRODUCED` through the pending entry, and the
  stamp is computed at REDEEM so the code's own ten-minute window is not
  charged against the credential's thirty days. :func:`verify_device_proof`
  refuses an expired row AFTER the proof, beside the revocation arm and for the
  same anti-probing reason, and the wire collapses both into one rejection.
* ``pairing.json`` — the SHORT-LIVED half: pending codes (hashed, salted) and
  the failed-redeem counter that arms the lockout. Nothing durable lives here;
  a lost ``pairing.json`` costs an operator one re-run of ``harness gateway
  pair``.

What is stored, and the honest limit of hashing it
--------------------------------------------------

The plaintext device token is returned to its caller EXACTLY ONCE, by
:func:`redeem_pairing_code`, and is never written to either file, to a log
line, to a frame, or to an event. What lands in ``devices.json`` is the
**verifier** — ``sha256(token)`` — and the proof a device presents is an HMAC
keyed by that verifier (:func:`device_proof`).

State the limit plainly, because a security note that overclaims is worse than
none: **the verifier is HMAC-key-equivalent.** A challenge-response where the
server holds no secret at all is an ASYMMETRIC scheme (the device signs, the
server holds only a public key); this is a symmetric one, chosen because the
plan of record specifies "the same HMAC challenge-response as today, keyed by
the device token" and because the loopback lane's :func:`serve_socket.hello_proof`
is the discipline being extended rather than replaced. So anyone who can read
``devices.json`` can impersonate every device in it, exactly as anyone who can
read ``serve_auth_token`` can impersonate the machine owner. Digesting the token
before storing it buys one real thing and not two: the bytes a device holds — the
bytes that travelled through the pairing channel and now sit in a phone's
keystore — are not the bytes on this disk, so a store read cannot recover an
issued credential, only the ability to answer a challenge with it. If
store-read resistance is ever wanted, the upgrade is the R1 survey's bullet 2
(the device holds a P-256 key and the install pins its JWK thumbprint), and it
changes only :func:`device_proof` and this file — never the wire's shape, which
says "proof" and nothing about how it was computed.

File permissions, honestly (the same gap, restated where it now matters more)
-----------------------------------------------------------------------------

``devices.json`` is created ``0600`` where that means something. **On Windows it
does not** — mode bits map only onto the read-only attribute and the file
inherits its parent directory's ACL, so on a default install it is readable by
any process running as this user. ``serve_auth.py`` recorded this gap for the
root token and declined to fix it there; the gap is unchanged, and the exposure
is larger, because the file now gates a listener that is reachable from another
machine. A best-effort ``icacls`` narrowing is applied on Windows and its
outcome is REPORTED rather than assumed — a permission posture this module
cannot enforce is never claimed as enforced.

Pairing discipline
------------------

Imitated from ``gateway/pairing.py``, not imported — and that is a decision
rather than laziness. That module is the MESSAGING gateway's: its store is
resolved from ``get_hermes_home()`` / ``get_hermes_dir()`` (a HERMES home, not a
store root, and this lane's whole Stage 0 finding is that those are provably
different scopes on this machine), it is keyed by ``(platform, user_id)``, and
it mirrors approvals into per-platform allowlist env vars. Importing it would
mean either bending a store-root credential into a platform/user pair or
teaching it a second storage scope; what is worth reusing is its RULES, and
those are restated here with the same numbers and the same reasons:

* 8-character codes from a 32-character unambiguous alphabet (no ``0``/``O``,
  no ``1``/``I``), chosen with :func:`secrets.choice`.
* The code is NEVER stored in plaintext — a random 16-byte salt and
  ``sha256(salt + code)`` are, and redemption compares in constant time.
* A short TTL, a cap on pending codes, and a lockout after repeated failed
  redeems, checked BEFORE the pending lookup so a lockout blocks a code that
  was already issued (``pairing.py`` fixed exactly that bug, #10195).
* A successful redeem clears the failure streak, so isolated typos cannot
  accumulate into a spurious lockout.
* **Codes are never logged.** Nothing in this module writes one anywhere except
  the return value handed to its caller.

Two numbers deviate from ``pairing.py``'s, both downward, both because the
channel is different. Its code is DM'd to a stranger who may take an hour to
read it; this one is printed on the operator's own terminal for them to type or
scan within seconds, so the TTL is ten minutes rather than an hour and the
window in which a guess is worth anything shrinks with it. And there is no
per-requester rate limit on MINTING, because the requester is the operator at
the install's own console — the party the whole tier traces its authority to
(Ruling A: the tier is client security auth, and an operator-run pairing on the
install's machine IS the account-auth trace). The cap on PENDING codes still
applies: three unredeemed codes at once is a mistake, not a workflow.

Root as INPUT
-------------

Every function takes ``store_root``. This module never resolves a root and
never reads ``HERMES_HOME`` — the same rule ``serve_auth.py``,
``gateway_identity.py`` and ``serve_socket.py`` all state, for the same reason:
several roots coexist on this machine, and a credential store free to re-derive
its own root could pair a device against one install and answer for another.

Never raises for a READ
-----------------------

:func:`lookup_device`, :func:`list_devices` and :func:`verify_device_proof`
answer with a typed outcome and never propagate an ``OSError``: they run on the
socket lane's handshake path, where an exception is a peer that learns nothing
and a runtime that logged a traceback. The WRITE paths (mint, redeem, revoke)
do report failure as a typed refusal too, because their caller is a CLI verb
that owes the operator an exit code rather than a stack trace.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .call_authorization import TIER_CONSOLE, TIER_READ, TIERS
from .gateway_identity import gateway_dir
from .gateway_pairing_codes import (
    CODE_ALPHABET,
    CODE_LENGTH,
    CODE_TTL_SECONDS,
    KIND_DEVICE,
    LOCKOUT_SECONDS,
    MAX_FAILED_REDEEMS,
    MAX_PENDING_CODES,
    expire_pending,
    lockout_remaining,
    match_pending,
    mint_into,
    note_failed_redeem,
    pending_codes,
    supersede_pending,
)
from .store_file_io import iso_stamp as _iso
from .store_file_io import os_error_reason as _os_reason
from .store_file_io import read_json_object as _read_json
from .store_file_io import stamp_passed as _stamp_passed
from .store_file_io import store_lock as _file_lock
from .store_file_io import write_secure_json as _write_secure

__all__ = [
    "AUTH_EXPIRED",
    "CODE_ALPHABET",
    "CODE_LENGTH",
    "CODE_TTL_SECONDS",
    "CREDENTIAL_TTL_SECONDS_INTRODUCED",
    "DEFAULT_DEVICE_TIER",
    "DEVICE_STORE_CONTRACT",
    "DEVICE_STORE_FILENAME",
    "DEVICE_TOKEN_BYTES",
    "GATEWAY_PROOF_ALGORITHM",
    "GATEWAY_PROOF_CONTRACT",
    "LOCKOUT_SECONDS",
    "MAX_FAILED_REDEEMS",
    "MAX_PENDING_CODES",
    "PAIRING_STORE_FILENAME",
    "AUTH_BAD_PROOF",
    "AUTH_MALFORMED",
    "AUTH_OK",
    "AUTH_REVOKED",
    "AUTH_UNKNOWN_DEVICE",
    "DeviceAuth",
    "DeviceCredential",
    "DeviceRecord",
    "PairingCode",
    "StoreRefusal",
    "device_proof",
    "device_store_path",
    "device_verifier",
    "list_devices",
    "lookup_device",
    "mint_pairing_code",
    "note_device_seen",
    "pairing_store_path",
    "redeem_pairing_code",
    "revoke_device",
    "verify_device_proof",
]


# ── constants ────────────────────────────────────────────────────────────────

#: Beside ``install.json`` under ``<store_root>/gateway/``. The DIRECTORY is the
#: unit Stage 0 established; Stage 6 adds ``peers.json`` here too.
DEVICE_STORE_FILENAME = "devices.json"
PAIRING_STORE_FILENAME = "pairing.json"
#: Serialised beside the rows so a future migration reads a number rather than
#: guessing from key presence. Same "a set plus an integer" habit the RPC
#: manifest uses; this one is a file, so nothing negotiates on it.
DEVICE_STORE_CONTRACT = 1

#: The code discipline — alphabet, length, TTL, caps, lockout — is
#: ``gateway_pairing_codes``' and is RE-EXPORTED here rather than restated,
#: because Stage 6's peer ceremony obeys the same numbers over the same pending
#: map. The names stay importable from this module: they were this module's API
#: for the whole of Stage 1 and several tests read them here.

#: 256 bits, hex-encoded. Not configurable, for ``serve_auth``'s reason: a knob
#: here can only ever be turned down.
DEVICE_TOKEN_BYTES = 32

#: The gateway handshake's proof version. SEPARATE from
#: ``serve_socket.HELLO_CONTRACT_VERSION`` on purpose — that number describes the
#: FRAME exchange (who speaks first, what fields are present), which the gateway
#: lane shares byte-for-byte; this one describes the DERIVATION, which differs
#: because a device proof also binds the device id. A single number covering both
#: would have to move whenever either moved, and a client could not tell which
#: half changed.
GATEWAY_PROOF_CONTRACT = 1
GATEWAY_PROOF_ALGORITHM = "hmac-sha256"

#: Typed outcomes of :func:`verify_device_proof`. The reason IS the
#: classification: the socket lane derives "does this charge the auth rate
#: limiter" from it, never from a boolean a caller passes in — the shape
#: ``serve_socket._reject`` already had to be corrected into.
AUTH_OK = "ok"
AUTH_UNKNOWN_DEVICE = "unknown_device"
AUTH_REVOKED = "device_revoked"
AUTH_BAD_PROOF = "bad_proof"
AUTH_MALFORMED = "hello_malformed"
#: A row whose ``expires_at`` has passed (R-IP15 as amended: an INTRODUCED
#: credential lives 30 days). Its own outcome rather than a second spelling of
#: ``device_revoked``, because the operator's next move differs: a revoked
#: device was thrown out on purpose and should stay out; an expired one is a
#: device that was fine and needs a fresh introduction. The WIRE still collapses
#: both into one rejection — see :func:`verify_device_proof`.
AUTH_EXPIRED = "device_expired"

#: How long a credential MINTED BY ``harness gateway introduce`` lives, in
#: seconds — thirty days, R-IP15 as amended. ONE constant for both stores
#: (``gateway_peers`` imports it from here, as it already imports the lock and
#: the pairing-state helpers), because a device half and a peer half minted by
#: one ``introduce`` that expired on different days would be an edge whose two
#: ends disagree about when it died.
#:
#: **``None`` is the other legal value and it means "never".** The manual
#: ceremony (``gateway pair`` / ``peers pair``) keeps minting with no TTL, so a
#: row written by a pre-S2 build and a row written by today's manual verbs stay
#: byte-identical: an operator standing at both machines is the provenance, and
#: expiring that on a clock would lock people out of their own workshop. The TTL
#: exists for the credential nobody carried by hand — the one a backend grant
#: introduced — which is exactly the one that should not outlive its errand.
CREDENTIAL_TTL_SECONDS_INTRODUCED = 30 * 86400

#: The tier a pairing run on the install's own machine confers, per R11 as
#: ruled: the operator standing at the console IS the account-auth trace, so the
#: default is the full one and ``read`` is what an operator asks for explicitly.
#: Both spellings are representable from day one — a field that exists but has
#: one value is a field nobody has tested.
DEFAULT_DEVICE_TIER = TIER_CONSOLE


# ── typed results ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StoreRefusal:
    """A write that did not happen, with the reason spelled for a machine."""

    reason: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class PairingCode:
    """A freshly minted code. The plaintext ``code`` exists ONLY here.

    It is returned to the CLI verb that asked for it and is never persisted:
    ``pairing.json`` holds a salted digest. A caller that loses this object has
    to mint another one, which is the intended failure mode.
    """

    code: str
    request_id: str
    tier: str
    name: str | None
    expires_at: float

    @property
    def ok(self) -> bool:
        return True

    def expires_in_seconds(self, *, now: float | None = None) -> int:
        return max(0, int(self.expires_at - (now if now is not None else time.time())))


@dataclass(frozen=True, slots=True)
class DeviceCredential:
    """What a successful redemption hands back. ``token`` appears once, here."""

    device_id: str
    token: str
    tier: str
    name: str

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    """One paired device as it is stored — WITHOUT anything secret.

    The ``verifier`` is deliberately not a field on this dataclass. Every
    surface that shows a device to a human (``harness gateway devices list``, a
    log line, a future ``gateway.*`` method) renders one of these, so the type
    itself is what makes "the credential leaked into an operator surface"
    unrepresentable rather than merely unintended.
    """

    device_id: str
    name: str
    tier: str
    created_at: str
    last_seen: str | None
    revoked: bool
    revoked_at: str | None
    #: When this credential stops working, ISO-8601 UTC, or ``None`` for never
    #: (R-IP15 as amended). TRUST, not cache: THIS install decided it at mint
    #: time from :data:`CREDENTIAL_TTL_SECONDS_INTRODUCED`, and nothing on the
    #: wire can move it — a device that could push its own expiry out would hold
    #: a credential with no end.
    expires_at: str | None = None
    #: The ACCOUNT's device id, when an ``introduce`` named one. A LABEL and not
    #: a check: hermes never learns an account device id at redeem time (the row
    #: id is minted ``dev_<hex>`` here), so this is the join key that relates
    #: this row to the account row an operator sees in the launcher's sheet
    #: (R-IP14, one bookkeeping) — and nothing authenticates against it.
    account_device_id: str | None = None

    @property
    def expired(self) -> bool:
        """Has :attr:`expires_at` passed? ``False`` when there is none.

        Computed rather than stored, for the reason every "is it stale yet"
        predicate is: a boolean written at mint time is a fact about the past
        wearing the tense of the present. An unparseable stamp reads as NOT
        expired — the same direction ``_decode_device`` fails in for an unknown
        tier is the SAFE direction there (least privilege) and the opposite one
        here, because a clock this build cannot read must not silently revoke
        every device an operator paired.
        """

        return _stamp_passed(self.expires_at)

    def payload(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "tier": self.tier,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "revoked": self.revoked,
            "revoked_at": self.revoked_at,
            "expires_at": self.expires_at,
            "expired": self.expired,
            "account_device_id": self.account_device_id,
        }


@dataclass(frozen=True, slots=True)
class DeviceAuth:
    """The handshake's answer about one device.

    ``record`` is present only on :data:`AUTH_OK`, so a caller cannot stamp a
    connection with a device whose proof did not verify — the same reason
    ``SocketConnection.authenticated`` is set after ``verify_hello_proof`` and
    not before.
    """

    outcome: str
    device_id: str | None = None
    record: DeviceRecord | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == AUTH_OK


# ── paths ────────────────────────────────────────────────────────────────────


def device_store_path(store_root: Path | str) -> Path:
    return gateway_dir(store_root) / DEVICE_STORE_FILENAME


def pairing_store_path(store_root: Path | str) -> Path:
    return gateway_dir(store_root) / PAIRING_STORE_FILENAME


# ── the derivation ───────────────────────────────────────────────────────────


def device_verifier(token: str) -> str:
    """``sha256(token)`` as lowercase hex — what the install stores.

    ONE derivation, called by the pairing path to compute what to store and by
    the client half to compute the HMAC key. A second copy of this line is how
    two ends start disagreeing about what a credential is.
    """

    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def device_proof(token: str, nonce: str, *, port: int, device_id: str) -> str:
    """The device's answer to a gateway challenge, as lowercase hex.

    ``HMAC-SHA256(key=sha256(token), msg="gwv<contract>|<port>|<device_id>|<nonce>")``.

    Three things are bound into the message and each closes a different hole:

    * the **nonce** is fresh per connection, so a captured transcript is not
      replayable;
    * the **port** is what each end knows from its OWN socket — the server from
      what it listens on, the client from what it dialled — so an impostor that
      relays a live challenge to the real service gets an answer that does not
      verify there. This is ``serve_socket.hello_proof``'s argument and it is
      copied deliberately: binding to anything the greeting merely ASSERTS
      (``boot_id``, a claimed port) is binding to a number the impostor echoes;
    * the **device id**, so a proof minted by one device cannot be presented
      under another device's name even if the two ever shared a token. They do
      not — each pairing mints its own 256 bits — but a derivation that would
      still be sound if they did is the one worth writing.

    The token is the KEY (through its digest) and never the message, so the
    proof discloses nothing about it. It does not travel, in either direction.
    """

    message = f"gwv{GATEWAY_PROOF_CONTRACT}|{int(port)}|{str(device_id)}|{nonce}"
    return hmac.new(
        device_verifier(token).encode("ascii"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _proof_from_verifier(verifier: str, nonce: str, *, port: int, device_id: str) -> str:
    """The server's half: it holds the verifier, never the token."""

    message = f"gwv{GATEWAY_PROOF_CONTRACT}|{int(port)}|{str(device_id)}|{nonce}"
    return hmac.new(
        str(verifier).encode("ascii"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


# ── device store: reads ──────────────────────────────────────────────────────


def list_devices(store_root: Path | str) -> list[DeviceRecord]:
    """Every paired device, revoked ones included, oldest first.

    Revoked rows are KEPT and shown. A revocation that deleted the row would
    make "this device was never paired" and "this device was thrown out" the
    same answer, and the second one is the one an operator auditing a lost phone
    needs. It also keeps the device id from being re-minted onto a different
    device, which would make a stale log line name the wrong hardware.
    """

    rows = _read_devices(store_root)
    records = [record for record in (_decode_device(row) for row in rows.values()) if record]
    records.sort(key=lambda record: (record.created_at, record.device_id))
    return records


def lookup_device(store_root: Path | str, device_id: str) -> DeviceRecord | None:
    """One device by id, or ``None``. Never raises; never returns a secret."""

    device_id = str(device_id or "").strip()
    if not device_id:
        return None
    row = _read_devices(store_root).get(device_id)
    return _decode_device(row) if isinstance(row, dict) else None


def verify_device_proof(
    store_root: Path | str,
    device_id: Any,
    presented: Any,
    nonce: str,
    *,
    port: int,
) -> DeviceAuth:
    """Constant-time check of a device hello's proof. Fails CLOSED, always.

    Every missing input is a refusal rather than a degrade: no store, an unknown
    id, a revoked row, an empty nonce, a non-string proof. "Nothing configured,
    let everyone in" is the failure mode that turns a hardening into a bypass,
    and this function is reachable by an unauthenticated peer on a listener that
    is bound beyond loopback.

    The outcomes are DISTINGUISHED here and deliberately COLLAPSED on the wire:
    the socket lane answers every one of them with the same typed rejection, so
    a peer cannot use the reason to learn whether a device id exists. The
    distinction is for the runtime's own log and for the rate limiter, which
    charges a bad proof and an unknown device alike.
    """

    device_id = str(device_id or "").strip()
    if not device_id or len(device_id) > 128:
        return DeviceAuth(outcome=AUTH_MALFORMED)
    if not isinstance(presented, str) or not presented.strip():
        return DeviceAuth(outcome=AUTH_MALFORMED, device_id=device_id)
    if not nonce:
        return DeviceAuth(outcome=AUTH_MALFORMED, device_id=device_id)
    row = _read_devices(store_root).get(device_id)
    if not isinstance(row, dict):
        return DeviceAuth(outcome=AUTH_UNKNOWN_DEVICE, device_id=device_id)
    record = _decode_device(row)
    if record is None:
        return DeviceAuth(outcome=AUTH_UNKNOWN_DEVICE, device_id=device_id)
    verifier = str(row.get("verifier") or "").strip()
    if not verifier:
        return DeviceAuth(outcome=AUTH_UNKNOWN_DEVICE, device_id=device_id)
    expected = _proof_from_verifier(verifier, nonce, port=port, device_id=device_id)
    # BYTES, never str: ``hmac.compare_digest`` RAISES TypeError when either str
    # operand is non-ASCII, and ``presented`` is attacker-controlled on a path
    # no credential is needed to reach. One accented character used to unwind
    # out of the loopback handshake entirely (``serve_socket.verify_hello_proof``
    # carries the same note and the same fix); encoding first makes a hostile
    # proof merely wrong.
    if not hmac.compare_digest(
        presented.strip().lower().encode("utf-8", "replace"), expected.encode("ascii")
    ):
        return DeviceAuth(outcome=AUTH_BAD_PROOF, device_id=device_id)
    # Revocation is checked AFTER the proof on purpose. Checking it first would
    # let an unauthenticated peer probe which device ids are revoked (and, by
    # difference, which are live) without holding any credential at all. A
    # revoked device that still holds its token learns it is revoked; nobody
    # else learns anything.
    if record.revoked:
        return DeviceAuth(outcome=AUTH_REVOKED, device_id=device_id, record=record)
    # Expiry sits in the SAME place as revocation and for the same reason: after
    # the proof. Checking a stamp before the HMAC would let an unauthenticated
    # peer sweep device ids and learn which ones have lapsed — and, by
    # difference, which are live — while holding nothing. It also keeps the two
    # end-of-life conditions answering in one order everywhere: a revoked AND
    # expired row reports ``device_revoked``, because that is the decision an
    # operator made rather than a clock running out.
    if record.expired:
        return DeviceAuth(outcome=AUTH_EXPIRED, device_id=device_id, record=record)
    return DeviceAuth(outcome=AUTH_OK, device_id=device_id, record=record)


# ── device store: writes ─────────────────────────────────────────────────────


def note_device_seen(
    store_root: Path | str, device_id: str, *, now: float | None = None
) -> None:
    """Stamp ``last_seen``. Best effort, and never in the handshake's way.

    Bookkeeping must not be the thing that fails an authentication: every error
    is swallowed, because a device whose proof verified has authenticated
    whether or not this write lands.
    """

    device_id = str(device_id or "").strip()
    if not device_id:
        return
    stamp = _iso(now)
    try:
        with _store_lock(store_root):
            rows = _read_devices(store_root)
            row = rows.get(device_id)
            if not isinstance(row, dict):
                return
            row["last_seen"] = stamp
            _write_devices(store_root, rows)
    except Exception:
        return


def revoke_device(
    store_root: Path | str, device_id: str, *, now: float | None = None
) -> DeviceRecord | StoreRefusal:
    """Mark a device revoked. Idempotent; the row is kept (see :func:`list_devices`)."""

    device_id = str(device_id or "").strip()
    if not device_id:
        return StoreRefusal("invalid_device_id", "a device id is required")
    try:
        with _store_lock(store_root):
            rows = _read_devices(store_root)
            row = rows.get(device_id)
            if not isinstance(row, dict):
                return StoreRefusal(
                    "unknown_device", f"no device {device_id!r} is paired with this root"
                )
            if not row.get("revoked"):
                row["revoked"] = True
                row["revoked_at"] = _iso(now)
                _write_devices(store_root, rows)
            record = _decode_device(row)
            if record is None:  # pragma: no cover - a row we just wrote
                return StoreRefusal("store_corrupt", "the device row will not decode")
            return record
    except OSError as exc:
        return StoreRefusal(_os_reason(exc), str(exc))


def mint_pairing_code(
    store_root: Path | str,
    *,
    name: str | None = None,
    tier: str = DEFAULT_DEVICE_TIER,
    credential_ttl_seconds: int | None = None,
    for_device_id: str | None = None,
    correlation: str | None = None,
    now: float | None = None,
) -> PairingCode | StoreRefusal:
    """Mint a short-TTL pairing code. The plaintext is returned, never stored.

    Refuses (rather than queues) when the install is locked out after repeated
    failed redeems, or when :data:`MAX_PENDING_CODES` are already outstanding.
    Both refusals are typed so the CLI verb can tell the operator which it was —
    "wait out the lockout" and "redeem or expire the codes you already made" are
    different next moves.
    """

    tier = str(tier or "").strip() or DEFAULT_DEVICE_TIER
    if tier not in TIERS:
        return StoreRefusal(
            "invalid_tier",
            f"tier must be one of {', '.join(TIERS)}; got {tier!r}",
        )
    stamp = now if now is not None else time.time()
    requester = _clean_name(for_device_id) or None
    try:
        with _store_lock(store_root):
            state = _read_pairing(store_root)
            expire_pending(state, now=stamp)
            # R-D5, the device half. Same ruling and same ordering as
            # ``gateway_peers.mint_peer_code``: before the cap is counted, so a
            # launcher's retry for one device finds the cap where it started
            # instead of being refused by its own previous attempt. Scoped to a
            # named ``--for-device`` only — an unscoped ``harness gateway pair``
            # supersedes nothing, because there is no requester to attribute the
            # earlier code to and dropping a stranger's invitation on a mint
            # nobody asked for would be the cap working backwards.
            if requester:
                supersede_pending(
                    state, kind=KIND_DEVICE, field="for_device_id", value=requester
                )
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
                    f"(max {MAX_PENDING_CODES}); redeem or wait for them to expire",
                )
            # The three ``introduce`` keys ride ``extra`` rather than growing
            # the pending entry's schema, which is exactly what ``mint_into``'s
            # merge-UNDER-the-fixed-keys contract is for: a caller cannot
            # overwrite the kind, the salt or the digest, and a caller that
            # passes none of them mints the byte-identical entry a pre-S2 build
            # did. ``for_device_id`` is a LABEL carried to the row (R-S2-4) and
            # ``correlation`` is the grant id, kept so the redeem-time event can
            # name the errand — never a row field, never a secret.
            code, request_id, expires_at = mint_into(
                state,
                kind=KIND_DEVICE,
                extra={
                    "tier": tier,
                    "name": _clean_name(name) or None,
                    "credential_ttl_seconds": (
                        int(credential_ttl_seconds)
                        if credential_ttl_seconds
                        else None
                    ),
                    "for_device_id": requester,
                    "correlation": _clean_name(correlation) or None,
                },
                now=stamp,
            )
            _write_pairing(store_root, state)
            return PairingCode(
                code=code,
                request_id=request_id,
                tier=tier,
                name=_clean_name(name) or None,
                expires_at=expires_at,
            )
    except OSError as exc:
        return StoreRefusal(_os_reason(exc), str(exc))


def redeem_pairing_code(
    store_root: Path | str,
    code: str,
    *,
    device_name: str | None = None,
    now: float | None = None,
) -> DeviceCredential | StoreRefusal:
    """Turn a code into a paired device. The token is returned ONCE, here.

    The lockout is checked BEFORE the pending lookup, which is the correction
    ``gateway/pairing.py`` had to make (#10195): checking it after would let a
    code that was already issued be redeemed while the lockout was armed, so the
    lockout would gate minting and not guessing — i.e. gate nothing.

    A successful redemption RESETS the failure streak. Without that, isolated
    mistyped codes accumulate for the life of the store and eventually trip a
    lockout on one fresh typo — the same defect, and the same fix, as the module
    this discipline comes from.
    """

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
            found = match_pending(state, candidate, kind=KIND_DEVICE)
            if found is None:
                note_failed_redeem(state, now=stamp)
                _write_pairing(store_root, state)
                return StoreRefusal(
                    "invalid_code", "no pending pairing code matches (or it expired)"
                )
            matched_id, matched = found

            del pending_codes(state)[matched_id]
            state["failed_redeems"] = 0
            state["locked_until"] = 0.0
            _write_pairing(store_root, state)

            token = secrets.token_hex(DEVICE_TOKEN_BYTES)
            device_id = f"dev_{secrets.token_hex(8)}"
            name = (
                _clean_name(device_name)
                or _clean_name(matched.get("name"))
                or device_id
            )
            tier = str(matched.get("tier") or DEFAULT_DEVICE_TIER)
            tier = tier if tier in TIERS else DEFAULT_DEVICE_TIER
            ttl = matched.get("credential_ttl_seconds")
            try:
                ttl_seconds = int(ttl) if ttl else 0
            except (TypeError, ValueError):
                ttl_seconds = 0
            rows = _read_devices(store_root)
            rows[device_id] = {
                "device_id": device_id,
                "name": name,
                "tier": tier,
                "verifier": device_verifier(token),
                "created_at": _iso(stamp),
                "last_seen": None,
                "revoked": False,
                "revoked_at": None,
                # Computed at REDEEM and not at mint: the clock that matters is
                # when the credential started existing, and a code minted ten
                # minutes before it is spent should not lose ten minutes of its
                # thirty days. ``None`` when the mint named no TTL, which is
                # every manual ``harness gateway pair``.
                "expires_at": _iso(stamp + ttl_seconds) if ttl_seconds else None,
                # The account's own device id, copied through from the pending
                # entry. A label — see :class:`DeviceRecord` — and the join key
                # the launcher's sheet and S3's Unpair relate this row by.
                "account_device_id": _clean_name(matched.get("for_device_id")) or None,
            }
            _write_devices(store_root, rows)
            return DeviceCredential(
                device_id=device_id, token=token, tier=tier, name=name
            )
    except OSError as exc:
        return StoreRefusal(_os_reason(exc), str(exc))


# ── internals ────────────────────────────────────────────────────────────────


def _clean_name(value: Any) -> str:
    """Printable, single-line, bounded — the rule ``gateway_identity`` states.

    Not imported from there: that one is capped for a GREETING FRAME's budget
    and this one names a row in a device list. The rule happens to be the same
    today; tying them would mean a change to one silently retunes the other.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    text = "".join(ch for ch in text if ch.isprintable())
    return " ".join(text.split())[:64].strip()


def _decode_device(row: Any) -> DeviceRecord | None:
    if not isinstance(row, dict):
        return None
    device_id = str(row.get("device_id") or "").strip()
    if not device_id:
        return None
    tier = str(row.get("tier") or "").strip()
    # A row whose tier this build does not know is read as the LEAST privileged
    # tier, never as the default. The alternative — treating an unrecognised
    # word as ``console`` because that is the common case — is how a typo or a
    # downgrade widens a door silently.
    tier = tier if tier in TIERS else TIER_READ
    return DeviceRecord(
        device_id=device_id,
        name=str(row.get("name") or device_id),
        tier=tier,
        created_at=str(row.get("created_at") or ""),
        last_seen=(str(row["last_seen"]) if row.get("last_seen") else None),
        revoked=bool(row.get("revoked")),
        revoked_at=(str(row["revoked_at"]) if row.get("revoked_at") else None),
        # Absent reads as ``None`` — "never expires" — so every row written by a
        # build that predates S2 keeps working exactly as it did. That is the
        # whole migration: there is no rewrite pass and no contract bump,
        # because the only new fact has a legal absent value.
        expires_at=(str(row["expires_at"]) if row.get("expires_at") else None),
        account_device_id=(
            str(row["account_device_id"]) if row.get("account_device_id") else None
        ),
    )


def _read_devices(store_root: Path | str) -> dict[str, Any]:
    payload = _read_json(device_store_path(store_root))
    rows = payload.get("devices")
    return dict(rows) if isinstance(rows, dict) else {}


def _write_devices(store_root: Path | str, rows: dict[str, Any]) -> None:
    _write_secure(
        device_store_path(store_root),
        {"contract": DEVICE_STORE_CONTRACT, "devices": rows},
    )


def _read_pairing(store_root: Path | str) -> dict[str, Any]:
    payload = _read_json(pairing_store_path(store_root))
    payload.setdefault("pending", {})
    if not isinstance(payload["pending"], dict):
        payload["pending"] = {}
    return payload


def _write_pairing(store_root: Path | str, state: dict[str, Any]) -> None:
    state["contract"] = DEVICE_STORE_CONTRACT
    _write_secure(pairing_store_path(store_root), state)


# The bodies live in ``store_file_io`` (one authority) — the JSON read, the
# atomic 0600 write, the cross-process lock, the UTC stamp and the ACL
# narrowing, including the "this IS the transport slice that introduces the
# exposure" rationale that used to sit on the ACL helper here. Stage 6's
# ``gateway_peers`` is the second importer and is why the last four moved: two
# credential stores in one directory that each restated the same write is the
# exact group ``test_duplicate_helper_bodies`` would have named next. The
# conventional private names stay so call sites and tests read unchanged.


def _store_lock(store_root: Path | str, *, timeout_seconds: float = 10.0):
    """This root's gateway-directory lock. ONE lock file, both ceremonies.

    ``devices.lock`` is shared with ``gateway_peers`` rather than split per
    store, and deliberately: both ceremonies read-modify-write the SAME
    ``pairing.json`` (one pending map, one lockout, one cap — see
    ``gateway_pairing_codes``), so two lock files would be two names for a
    mutual exclusion that has to be one. The filename is historical; the scope
    it protects is the directory.
    """

    return _file_lock(
        gateway_dir(store_root) / "devices.lock", timeout_seconds=timeout_seconds
    )
