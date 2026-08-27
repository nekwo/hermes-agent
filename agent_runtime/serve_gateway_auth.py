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
  revoked_at}``.
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

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .call_authorization import TIER_CONSOLE, TIER_READ, TIERS
from .gateway_identity import gateway_dir

if os.name == "nt":  # pragma: no cover - platform split
    import errno as _errno
    import msvcrt
else:  # pragma: no cover - platform split
    import fcntl

__all__ = [
    "CODE_ALPHABET",
    "CODE_LENGTH",
    "CODE_TTL_SECONDS",
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

#: ``gateway/pairing.py``'s alphabet, verbatim: excludes ``0``/``O`` and
#: ``1``/``I`` so a code read off a screen and typed into a phone cannot be
#: mistyped into a DIFFERENT valid code.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
#: Ten minutes, not ``pairing.py``'s hour — see the module docstring. The code
#: is on the operator's own screen and is meant to be used now.
CODE_TTL_SECONDS = 600
#: Three unredeemed codes at once is a mistake, not a workflow.
MAX_PENDING_CODES = 3
#: 32^8 is ~1.1e12, so this is not what makes guessing infeasible — it is what
#: stops a peer spending the runtime's threads trying, and what makes a burst of
#: failures visible to an operator who looks.
MAX_FAILED_REDEEMS = 5
LOCKOUT_SECONDS = 3600

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

    def payload(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "tier": self.tier,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "revoked": self.revoked,
            "revoked_at": self.revoked_at,
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
    try:
        with _store_lock(store_root):
            state = _read_pairing(store_root)
            _expire_pending(state, now=stamp)
            locked_until = float(state.get("locked_until") or 0.0)
            if locked_until > stamp:
                return StoreRefusal(
                    "locked_out",
                    f"too many failed pairing attempts; retry in "
                    f"{int(locked_until - stamp)}s",
                )
            pending = state.setdefault("pending", {})
            if len(pending) >= MAX_PENDING_CODES:
                return StoreRefusal(
                    "too_many_pending",
                    f"{len(pending)} pairing codes are already outstanding "
                    f"(max {MAX_PENDING_CODES}); redeem or wait for them to expire",
                )
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            salt = os.urandom(16)
            request_id = secrets.token_hex(8)
            pending[request_id] = {
                "salt": salt.hex(),
                "hash": _hash_code(code, salt),
                "tier": tier,
                "name": _clean_name(name) or None,
                "created_at": stamp,
                "expires_at": stamp + CODE_TTL_SECONDS,
            }
            _write_pairing(store_root, state)
            return PairingCode(
                code=code,
                request_id=request_id,
                tier=tier,
                name=_clean_name(name) or None,
                expires_at=stamp + CODE_TTL_SECONDS,
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
            _expire_pending(state, now=stamp)
            locked_until = float(state.get("locked_until") or 0.0)
            if locked_until > stamp:
                _write_pairing(store_root, state)
                return StoreRefusal(
                    "locked_out",
                    f"too many failed pairing attempts; retry in "
                    f"{int(locked_until - stamp)}s",
                )
            pending = state.setdefault("pending", {})
            matched_id: str | None = None
            matched: dict[str, Any] | None = None
            for request_id, entry in pending.items():
                if not isinstance(entry, dict):
                    continue
                raw_salt = entry.get("salt")
                stored = entry.get("hash")
                if not isinstance(raw_salt, str) or not isinstance(stored, str):
                    continue
                try:
                    salt = bytes.fromhex(raw_salt)
                except ValueError:
                    continue
                if secrets.compare_digest(_hash_code(candidate, salt), stored):
                    matched_id, matched = request_id, entry
                    break
            if matched_id is None or matched is None:
                _note_failed_redeem(state, now=stamp)
                _write_pairing(store_root, state)
                return StoreRefusal(
                    "invalid_code", "no pending pairing code matches (or it expired)"
                )

            del pending[matched_id]
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
            }
            _write_devices(store_root, rows)
            return DeviceCredential(
                device_id=device_id, token=token, tier=tier, name=name
            )
    except OSError as exc:
        return StoreRefusal(_os_reason(exc), str(exc))


# ── internals ────────────────────────────────────────────────────────────────


def _hash_code(code: str, salt: bytes) -> str:
    return hashlib.sha256(salt + str(code).upper().encode("utf-8")).hexdigest()


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


def _iso(now: float | None) -> str:
    when = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(
        float(now), tz=timezone.utc
    )
    return when.isoformat()


def _expire_pending(state: dict[str, Any], *, now: float) -> None:
    pending = state.setdefault("pending", {})
    for request_id in [
        request_id
        for request_id, entry in pending.items()
        if not isinstance(entry, dict)
        or float(entry.get("expires_at") or 0.0) <= now
    ]:
        del pending[request_id]
    if float(state.get("locked_until") or 0.0) <= now and state.get("locked_until"):
        state["locked_until"] = 0.0
        state["failed_redeems"] = 0


def _note_failed_redeem(state: dict[str, Any], *, now: float) -> None:
    count = int(state.get("failed_redeems") or 0) + 1
    state["failed_redeems"] = count
    if count >= MAX_FAILED_REDEEMS:
        state["locked_until"] = now + LOCKOUT_SECONDS


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
    )


def _read_json(path: Path) -> dict[str, Any]:
    """The file as a dict, ``{}`` when absent/empty/undecodable. Never raises.

    A corrupt store reads as EMPTY rather than as an exception, and that is the
    fail-closed direction: an empty device store authenticates nobody, while a
    raised OSError on the handshake path is a peer that learns nothing and a
    traceback on a stream ``serve_loop`` has redirected onto the NDJSON protocol.
    """

    try:
        if not path.is_file():
            return {}
        # read_bytes + decode, never read_text: the repo's standing EOL rule.
        raw = path.read_bytes().decode("utf-8", errors="replace").strip()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


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


def _write_secure(path: Path, payload: dict[str, Any]) -> None:
    """Temp file + atomic replace, ``0600`` where that is meaningful.

    Atomic because a reader on the handshake path must see either the whole old
    store or the whole new one — a half-written ``devices.json`` reads as ``{}``
    (see :func:`_read_json`), which fails every device closed until the write
    finishes. Correct, but a paired device that intermittently cannot connect is
    the worst kind of bug to chase.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, default=str, indent=2) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "wb", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    try:
        with handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            # Meaningful here and NOT on Windows, where it only toggles the
            # read-only attribute while reporting success — ``serve_auth.py``'s
            # note, unchanged. The Windows narrowing is ``_narrow_windows_acl``.
            try:
                os.chmod(handle.name, 0o600)
            except OSError:
                pass
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    if os.name == "nt":
        _narrow_windows_acl(path)


def _narrow_windows_acl(path: Path) -> str:
    """Best-effort DACL narrowing on Windows. Returns the outcome, never raises.

    ``serve_auth.py`` recorded that Windows mode bits are not a permission and
    declined to fix it, on the grounds that the real control belongs with the
    transport slice that introduces the exposure. This IS that slice: the file
    below gates a listener reachable from another machine. So the narrowing is
    attempted — ``icacls`` with inheritance removed and a single grant to the
    current user — and its outcome is RETURNED rather than assumed, because a
    permission posture that cannot be enforced must never be claimed.

    Not fatal on failure by design: a device store that could not be narrowed is
    still a device store, and refusing to write one would take the lane down over
    a hardening.
    """

    user = os.environ.get("USERNAME") or ""
    if not user:
        return "skipped:no_username"
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{user}:(R,W)",
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"error:{type(exc).__name__}"
    return "narrowed" if completed.returncode == 0 else f"error:rc{completed.returncode}"


def _os_reason(exc: OSError) -> str:
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, FileNotFoundError):
        return "root_missing"
    if isinstance(exc, NotADirectoryError):
        return "root_not_a_directory"
    return "unwritable"


@contextlib.contextmanager
def _store_lock(store_root: Path | str, *, timeout_seconds: float = 10.0) -> Iterator[None]:
    """Serialise read-modify-write across PROCESSES, on this root's gateway dir.

    A real concurrency, not a theoretical one: ``harness gateway pair`` runs in
    the operator's shell while the serve process redeems and stamps ``last_seen``
    from its own. ``agent_runtime/locks.py`` holds the same OS primitive but
    resolves its directory through ``paths.lock_dir()`` — i.e. it re-derives a
    root — which is exactly what every module in this lane is forbidden to do,
    so the primitive is restated here over a path the caller supplied.

    Falls through WITHOUT the lock rather than raising if it cannot be taken:
    the alternative is a pairing verb that fails because a lock file is on a
    filesystem that will not lock, and the writes below are atomic-replace
    either way, so the loss is a lost update and not a corrupt store.
    """

    path = gateway_dir(store_root) / "devices.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+b")
    except OSError:
        yield
        return
    try:
        deadline = time.monotonic() + float(timeout_seconds)
        locked = False
        if os.name == "nt":  # pragma: no cover - platform split
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError as exc:
                    if exc.errno not in {_errno.EACCES, _errno.EDEADLK, 13, 36}:
                        break
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
        else:  # pragma: no cover - platform split
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                locked = True
            except OSError:
                locked = False
        try:
            yield
        finally:
            if locked:
                try:
                    if os.name == "nt":  # pragma: no cover - platform split
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:  # pragma: no cover - platform split
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    finally:
        try:
            handle.close()
        except OSError:
            pass
