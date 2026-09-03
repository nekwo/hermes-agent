"""The pairing-code DISCIPLINE, shared by both gateway ceremonies.

``serve_gateway_auth`` pairs a DEVICE (a phone against this install);
``gateway_peers`` pairs an INSTALL against another install. The two mint
different credentials into different stores and the wire never lets one be
spent as the other — but the eight characters an operator reads off a terminal
obey exactly the same rules in both, and this module is those rules, once.

Why the rules are shared and the credentials are not
----------------------------------------------------

The thing a code has to survive is GUESSING, and a guesser does not care which
ceremony a code belongs to: it is the same 32^8 space, reached through the same
listener, charged to the same handshake budget. So the failure counter, the
lockout it arms, and the cap on outstanding codes are **one set of numbers over
one pending map** (``<store_root>/gateway/pairing.json``), not two. Two counters
would mean the lockout gated half the guessing, which is to say it gated
nothing: an attacker locked out of the device ceremony would simply grind the
peer one, with the failure budget reset.

What is NOT shared is what a code redeems INTO. Every pending entry carries a
``kind`` (:data:`KIND_DEVICE` / :data:`KIND_PEER`), :func:`match_pending` will
only match a code against the kind that was asked for, and a mismatch is
accounted as a failed redeem rather than as a wrong-store hint. So a device
pairing code presented on the peer hello is refused, and a peer code presented
on the device hello is refused — the plan's "device-tier and peer-tier
credentials are never interchangeable", enforced at the point the code is
looked up rather than by two disjoint files that a future refactor could merge.

State in, state out — the lock stays with the caller
-----------------------------------------------------

Every function here takes the already-read ``pairing.json`` state dict and
mutates it in place; none of them opens a file, takes a lock, or resolves a
root. That is what lets each ceremony keep its redeem ATOMIC: the device
redeem's "delete the pending entry and write the device row" and the peer
redeem's "delete the pending entry and write the peer row" each happen under
ONE hold of the gateway directory's lock, exactly as Stage 1 shipped it. A
module that owned the lock as well would have forced a second acquisition
between the two halves, and a crash in that window burns a code for nothing.

The rules themselves, and where they came from
-----------------------------------------------

Imitated from ``gateway/pairing.py`` — the MESSAGING gateway's — and not
imported from it, for the reasons ``serve_gateway_auth``'s docstring gives at
length (that module is keyed by ``(platform, user_id)``, resolves its own
HERMES home rather than taking a store root, and mirrors approvals into
per-platform env allowlists). What is worth reusing is its rules, and they are
restated here with the same numbers and the same reasons:

* 8-character codes from a 32-character unambiguous alphabet (no ``0``/``O``,
  no ``1``/``I``), chosen with :func:`secrets.choice`.
* The code is NEVER stored in plaintext — a random 16-byte salt and
  ``sha256(salt + code)`` are, and redemption compares in constant time.
* A short TTL, a cap on pending codes, and a lockout after repeated failed
  redeems, checked BEFORE the pending lookup so a lockout blocks a code that
  was already issued (``pairing.py`` fixed exactly that bug, #10195).
* A successful redeem clears the failure streak, so isolated typos cannot
  accumulate into a spurious lockout.
* **Codes are never logged.** Nothing here writes one anywhere except the
  return value handed to its caller.

Two numbers deviate from ``pairing.py``'s, both downward, both because the
channel is different. Its code is DM'd to a stranger who may take an hour to
read it; this one is printed on the operator's own terminal for them to type or
scan within seconds, so the TTL is ten minutes rather than an hour and the
window in which a guess is worth anything shrinks with it. And there is no
per-requester rate limit on MINTING, because the requester is the operator at
the install's own console — the party both tiers trace their authority to.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from typing import Any

__all__ = [
    "CODE_ALPHABET",
    "CODE_LENGTH",
    "CODE_TTL_SECONDS",
    "KIND_DEVICE",
    "KIND_PEER",
    "LOCKOUT_SECONDS",
    "MAX_FAILED_REDEEMS",
    "MAX_PENDING_CODES",
    "expire_pending",
    "hash_code",
    "lockout_remaining",
    "match_pending",
    "mint_into",
    "note_failed_redeem",
    "pending_codes",
]


#: ``gateway/pairing.py``'s alphabet, verbatim: excludes ``0``/``O`` and
#: ``1``/``I`` so a code read off a screen and typed into a phone cannot be
#: mistyped into a DIFFERENT valid code.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
#: Ten minutes, not ``pairing.py``'s hour — see the module docstring. The code
#: is on the operator's own screen and is meant to be used now.
CODE_TTL_SECONDS = 600
#: Three unredeemed codes at once is a mistake, not a workflow — and the cap is
#: over BOTH ceremonies' pending entries, because it is the operator's own
#: attention that is being capped, not a per-ceremony resource.
MAX_PENDING_CODES = 3
#: 32^8 is ~1.1e12, so this is not what makes guessing infeasible — it is what
#: stops a peer spending the runtime's threads trying, and what makes a burst of
#: failures visible to an operator who looks.
MAX_FAILED_REDEEMS = 5
LOCKOUT_SECONDS = 3600

#: What a pending entry redeems into. Stored on the entry, checked at match
#: time: a code minted for one ceremony is not a credential for the other.
KIND_DEVICE = "device"
KIND_PEER = "peer"


def hash_code(code: str, salt: bytes) -> str:
    """``sha256(salt + UPPERCASE(code))``. ONE derivation, both ceremonies."""

    return hashlib.sha256(salt + str(code).upper().encode("utf-8")).hexdigest()


def pending_codes(state: dict[str, Any]) -> dict[str, Any]:
    """The pending map, created if absent. Shared across kinds — see above."""

    pending = state.setdefault("pending", {})
    if not isinstance(pending, dict):
        pending = {}
        state["pending"] = pending
    return pending


def expire_pending(state: dict[str, Any], *, now: float) -> None:
    """Drop expired entries and lift a lapsed lockout. Both kinds, one sweep."""

    pending = pending_codes(state)
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


def lockout_remaining(state: dict[str, Any], *, now: float) -> int:
    """Seconds left on an armed lockout; ``0`` when there is none.

    Returned as a NUMBER rather than a boolean because both callers have to
    tell the operator when to try again, and a predicate that answered only
    "yes" would make each of them recompute the arithmetic — which is how two
    refusals start disagreeing about the same clock.
    """

    remaining = float(state.get("locked_until") or 0.0) - now
    return int(remaining) if remaining > 0 else 0


def note_failed_redeem(state: dict[str, Any], *, now: float) -> None:
    """Charge one failure and arm the lockout at the cap. One streak, both kinds."""

    count = int(state.get("failed_redeems") or 0) + 1
    state["failed_redeems"] = count
    if count >= MAX_FAILED_REDEEMS:
        state["locked_until"] = now + LOCKOUT_SECONDS


def mint_into(
    state: dict[str, Any], *, kind: str, extra: dict[str, Any], now: float
) -> tuple[str, str, float]:
    """Add one pending entry. Returns ``(code, request_id, expires_at)``.

    The plaintext ``code`` is RETURNED and never stored: the entry holds a
    random 16-byte salt and ``sha256(salt + code)``. A caller that loses the
    return value has to mint another one, which is the intended failure mode
    and the reason the TTL can be short.

    ``extra`` is whatever the ceremony needs at redemption time — the device
    ceremony's tier and device name, the peer ceremony's note. It is merged
    UNDER the fixed keys rather than over them, so a caller cannot overwrite
    ``kind``, the salt or the digest by passing a colliding key.

    S2 (``harness gateway introduce``) puts three more here, on BOTH kinds, and
    they are named rather than left to be discovered in two call sites:

    * ``credential_ttl_seconds`` — how long the CREDENTIAL this code redeems
      into lives, or ``None`` for never. Not the code's own TTL, which is
      :data:`CODE_TTL_SECONDS` and is unchanged; the redeeming side computes
      ``expires_at`` from this at redemption, so the code's ten minutes are not
      charged against the credential's thirty days.
    * ``for_install_id`` — **checked, and only on the PEER kind.** A join hello
      names the redeemer's own install id in the same frame, so
      ``gateway_peers.redeem_peer_code`` can refuse a mismatch as ``invalid_code``
      and charge a failure. There is no equivalent on the device kind: a device
      id is minted AT redemption, so a device code stays a plain bearer for its
      600 seconds and ``for_device_id`` below is a label rather than a check.
    * ``for_device_id`` — the ACCOUNT's device id, copied onto the device row as
      ``account_device_id`` at redemption. A join key for an operator's sheet,
      never a credential.
    * ``correlation`` — the backend grant id (R-IP17), kept so the redeem-time
      event can name the errand. Never a row field and never a secret.
    """

    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    salt = os.urandom(16)
    request_id = secrets.token_hex(8)
    expires_at = now + CODE_TTL_SECONDS
    entry = dict(extra)
    entry.update(
        {
            "kind": str(kind),
            "salt": salt.hex(),
            "hash": hash_code(code, salt),
            "created_at": now,
            "expires_at": expires_at,
        }
    )
    pending_codes(state)[request_id] = entry
    return code, request_id, expires_at


def match_pending(
    state: dict[str, Any], candidate: str, *, kind: str
) -> tuple[str, dict[str, Any]] | None:
    """The pending entry this code redeems, or ``None``. Constant-time compare.

    The KIND is part of the match, not a check afterwards. An entry of the
    wrong kind is skipped exactly as a non-matching digest is, so a caller
    cannot learn "that code exists, but for the other ceremony" — and, more to
    the point, cannot be tempted to act on it. The wire's rule that device and
    peer credentials are never interchangeable is enforced here, at the lookup,
    rather than by two files a refactor could merge.
    """

    wanted = str(candidate or "").strip().upper()
    if not wanted:
        return None
    for request_id, entry in pending_codes(state).items():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("kind") or KIND_DEVICE) != str(kind):
            continue
        raw_salt = entry.get("salt")
        stored = entry.get("hash")
        if not isinstance(raw_salt, str) or not isinstance(stored, str):
            continue
        try:
            salt = bytes.fromhex(raw_salt)
        except ValueError:
            continue
        if secrets.compare_digest(hash_code(wanted, salt), stored):
            return request_id, entry
    return None
