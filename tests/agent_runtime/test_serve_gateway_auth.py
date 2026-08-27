"""Stage 1's credential half: per-device tokens, pairing codes, revocation.

The claims worth pinning are the ones the listener will lean on and cannot
re-derive at the handshake: that the plaintext token exists exactly once and
never lands on disk, that a proof is bound to the port AND the device id, that
every missing input fails CLOSED, and that the pairing lifecycle carries
``gateway/pairing.py``'s corrections rather than re-deriving its bugs.

The root is an INPUT to every function here, so these tests need no environment
isolation — they hand it a ``tmp_path`` and read what landed on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_runtime.call_authorization import TIER_CONSOLE, TIER_READ
from agent_runtime.serve_gateway_auth import (
    AUTH_BAD_PROOF,
    AUTH_MALFORMED,
    AUTH_OK,
    AUTH_REVOKED,
    AUTH_UNKNOWN_DEVICE,
    CODE_ALPHABET,
    CODE_LENGTH,
    CODE_TTL_SECONDS,
    LOCKOUT_SECONDS,
    MAX_FAILED_REDEEMS,
    MAX_PENDING_CODES,
    DeviceCredential,
    PairingCode,
    StoreRefusal,
    device_proof,
    device_store_path,
    device_verifier,
    list_devices,
    lookup_device,
    mint_pairing_code,
    note_device_seen,
    pairing_store_path,
    redeem_pairing_code,
    revoke_device,
    verify_device_proof,
)


def pair(root: Path, *, tier: str = TIER_CONSOLE, name: str | None = None, now: float = 1000.0):
    """Mint and redeem in one step — the ceremony, for tests about what follows."""

    code = mint_pairing_code(root, tier=tier, name=name, now=now)
    assert isinstance(code, PairingCode)
    credential = redeem_pairing_code(root, code.code, now=now + 1)
    assert isinstance(credential, DeviceCredential)
    return credential


# ── the code itself ─────────────────────────────────────────────────────────


def test_a_minted_code_uses_the_unambiguous_alphabet_and_is_eight_characters(tmp_path: Path):
    """``gateway/pairing.py``'s alphabet, verbatim: a code read off a screen and
    typed into a phone must not be mistypeable into a DIFFERENT valid code."""

    code = mint_pairing_code(tmp_path, now=1000.0)

    assert isinstance(code, PairingCode)
    assert len(code.code) == CODE_LENGTH == 8
    assert set(code.code) <= set(CODE_ALPHABET)
    assert not (set("01OI") & set(CODE_ALPHABET))
    assert code.tier == TIER_CONSOLE
    assert code.expires_at == 1000.0 + CODE_TTL_SECONDS


def test_the_plaintext_code_is_never_written_to_disk(tmp_path: Path):
    """A salted digest is stored, so reading ``pairing.json`` reveals no code.

    The kill-mutation — store the code as the key, the way a first cut always
    does — passes every behavioural test in this file and fails only this one.
    """

    code = mint_pairing_code(tmp_path, now=1000.0)
    assert isinstance(code, PairingCode)

    raw = pairing_store_path(tmp_path).read_bytes().decode("utf-8")
    assert code.code not in raw
    entry = next(iter(json.loads(raw)["pending"].values()))
    assert set(entry) >= {"salt", "hash"}
    assert len(entry["hash"]) == 64


def test_a_code_expires_and_a_late_redeem_is_refused(tmp_path: Path):
    code = mint_pairing_code(tmp_path, now=1000.0)
    assert isinstance(code, PairingCode)

    late = redeem_pairing_code(tmp_path, code.code, now=1000.0 + CODE_TTL_SECONDS + 1)

    assert isinstance(late, StoreRefusal)
    assert late.reason == "invalid_code"
    assert list_devices(tmp_path) == []


def test_only_three_codes_may_be_outstanding_at_once(tmp_path: Path):
    for _ in range(MAX_PENDING_CODES):
        assert isinstance(mint_pairing_code(tmp_path, now=1000.0), PairingCode)

    refused = mint_pairing_code(tmp_path, now=1000.0)

    assert isinstance(refused, StoreRefusal)
    assert refused.reason == "too_many_pending"
    # …and the cap is on OUTSTANDING codes, not on lifetime mints: once they
    # expire the operator can pair again without touching a file by hand.
    assert isinstance(
        mint_pairing_code(tmp_path, now=1000.0 + CODE_TTL_SECONDS + 1), PairingCode
    )


# ── the lockout, and the two bugs ``pairing.py`` had to fix ─────────────────


def test_five_wrong_codes_arm_a_lockout_that_blocks_an_already_issued_code(tmp_path: Path):
    """The #10195 correction, restated: the lockout is checked BEFORE the pending
    lookup, so it gates GUESSING and not merely minting. Checked after, a code
    already sitting in ``pending`` would still redeem while the lockout was armed
    — which is a lockout that protects nothing."""

    good = mint_pairing_code(tmp_path, now=1000.0)
    assert isinstance(good, PairingCode)
    for _ in range(MAX_FAILED_REDEEMS):
        assert isinstance(redeem_pairing_code(tmp_path, "ZZZZZZZZ", now=1000.0), StoreRefusal)

    blocked = redeem_pairing_code(tmp_path, good.code, now=1000.0)

    assert isinstance(blocked, StoreRefusal)
    assert blocked.reason == "locked_out"
    assert list_devices(tmp_path) == []
    # And minting is blocked too, so an operator cannot paper over a live attack
    # by making a fresh code.
    assert getattr(mint_pairing_code(tmp_path, now=1000.0), "reason", None) == "locked_out"


def test_the_lockout_lapses_and_the_store_recovers(tmp_path: Path):
    good = mint_pairing_code(tmp_path, now=1000.0)
    assert isinstance(good, PairingCode)
    for _ in range(MAX_FAILED_REDEEMS):
        redeem_pairing_code(tmp_path, "ZZZZZZZZ", now=1000.0)

    # Far enough for the lockout, but the code has expired by then too — so mint
    # a fresh one and prove the store recovered rather than wedged.
    after = 1000.0 + LOCKOUT_SECONDS + 1
    fresh = mint_pairing_code(tmp_path, now=after)
    assert isinstance(fresh, PairingCode)
    assert isinstance(redeem_pairing_code(tmp_path, fresh.code, now=after), DeviceCredential)


def test_a_successful_redeem_clears_the_failure_streak(tmp_path: Path):
    """Without this, isolated typos accumulate for the life of the store and one
    fresh mistype eventually trips a spurious lockout — ``pairing.py``'s second
    correction, and the reason its ``_finish_approval`` resets the counter."""

    for _ in range(MAX_FAILED_REDEEMS - 1):
        redeem_pairing_code(tmp_path, "ZZZZZZZZ", now=1000.0)
    pair(tmp_path, now=1000.0)

    # A full fresh budget of failures is available again: if the streak had
    # carried over, the FIRST of these would arm the lockout.
    for _ in range(MAX_FAILED_REDEEMS - 1):
        assert (
            getattr(redeem_pairing_code(tmp_path, "ZZZZZZZZ", now=1000.0), "reason", None)
            == "invalid_code"
        )


def test_a_lowercase_code_redeems_because_a_phone_will_send_one(tmp_path: Path):
    code = mint_pairing_code(tmp_path, now=1000.0)
    assert isinstance(code, PairingCode)

    assert isinstance(
        redeem_pairing_code(tmp_path, code.code.lower(), now=1000.0), DeviceCredential
    )


# ── the credential ──────────────────────────────────────────────────────────


def test_the_token_is_returned_once_and_never_stored(tmp_path: Path):
    """The whole point of the module: what lands on disk is a digest, and the
    digest is what the server needs to verify a proof."""

    credential = pair(tmp_path)

    assert len(credential.token) == 64  # 256 bits, hex
    raw = device_store_path(tmp_path).read_bytes().decode("utf-8")
    assert credential.token not in raw
    row = json.loads(raw)["devices"][credential.device_id]
    assert row["verifier"] == device_verifier(credential.token)
    assert "token" not in row


def test_a_device_record_shown_to_an_operator_carries_no_verifier(tmp_path: Path):
    """The type is what makes the leak unrepresentable, not the caller's care:
    every operator-facing surface renders a ``DeviceRecord``, and it has no field
    for the credential."""

    credential = pair(tmp_path, name="phone")

    (record,) = list_devices(tmp_path)
    assert record.device_id == credential.device_id
    assert record.name == "phone"
    assert record.tier == TIER_CONSOLE
    assert record.revoked is False
    assert "verifier" not in record.payload()
    assert not hasattr(record, "verifier")


def test_the_tier_asked_for_at_mint_is_the_tier_the_device_gets(tmp_path: Path):
    """R11: the field exists day one with both spellings representable. A field
    that exists but has one value is a field nobody has tested."""

    read_only = pair(tmp_path, tier=TIER_READ, now=1000.0)
    console = pair(tmp_path, tier=TIER_CONSOLE, now=2000.0)

    assert lookup_device(tmp_path, read_only.device_id).tier == TIER_READ
    assert lookup_device(tmp_path, console.device_id).tier == TIER_CONSOLE


def test_an_unknown_tier_is_refused_at_mint_rather_than_defaulted(tmp_path: Path):
    refused = mint_pairing_code(tmp_path, tier="admin", now=1000.0)

    assert isinstance(refused, StoreRefusal)
    assert refused.reason == "invalid_tier"


def test_a_row_whose_tier_this_build_does_not_know_reads_as_the_least_privileged(
    tmp_path: Path,
):
    """A typo, or a row written by a future build with a wider vocabulary, must
    narrow a door rather than widen one."""

    credential = pair(tmp_path)
    payload = json.loads(device_store_path(tmp_path).read_bytes().decode("utf-8"))
    payload["devices"][credential.device_id]["tier"] = "superuser"
    device_store_path(tmp_path).write_bytes(json.dumps(payload).encode("utf-8"))

    assert lookup_device(tmp_path, credential.device_id).tier == TIER_READ


# ── the proof ───────────────────────────────────────────────────────────────


def test_a_correct_proof_verifies_and_returns_the_record(tmp_path: Path):
    credential = pair(tmp_path)
    proof = device_proof(
        credential.token, "a" * 64, port=4242, device_id=credential.device_id
    )

    auth = verify_device_proof(
        tmp_path, credential.device_id, proof, "a" * 64, port=4242
    )

    assert auth.outcome == AUTH_OK
    assert auth.ok is True
    assert auth.record is not None and auth.record.tier == TIER_CONSOLE


def test_a_proof_minted_for_another_port_does_not_verify(tmp_path: Path):
    """The relay defence ``serve_socket.hello_proof`` argues for, carried onto the
    device tier: a fresh nonce stops replay but not a LIVE relay, and the port is
    the one value each end takes from its own socket."""

    credential = pair(tmp_path)
    proof = device_proof(
        credential.token, "a" * 64, port=1111, device_id=credential.device_id
    )

    auth = verify_device_proof(
        tmp_path, credential.device_id, proof, "a" * 64, port=2222
    )

    assert auth.outcome == AUTH_BAD_PROOF


def test_a_proof_minted_under_another_device_id_does_not_verify(tmp_path: Path):
    first = pair(tmp_path, now=1000.0)
    second = pair(tmp_path, now=2000.0)
    proof = device_proof(first.token, "a" * 64, port=4242, device_id=first.device_id)

    auth = verify_device_proof(tmp_path, second.device_id, proof, "a" * 64, port=4242)

    assert auth.outcome == AUTH_BAD_PROOF


def test_a_proof_over_another_nonce_does_not_verify(tmp_path: Path):
    credential = pair(tmp_path)
    proof = device_proof(
        credential.token, "a" * 64, port=4242, device_id=credential.device_id
    )

    auth = verify_device_proof(tmp_path, credential.device_id, proof, "b" * 64, port=4242)

    assert auth.outcome == AUTH_BAD_PROOF


@pytest.mark.parametrize(
    "device_id, presented, nonce, expected",
    [
        ("", "deadbeef", "a" * 64, AUTH_MALFORMED),
        ("dev_x", None, "a" * 64, AUTH_MALFORMED),
        ("dev_x", "   ", "a" * 64, AUTH_MALFORMED),
        ("dev_x", "deadbeef", "", AUTH_MALFORMED),
        ("dev_never_paired", "deadbeef", "a" * 64, AUTH_UNKNOWN_DEVICE),
    ],
)
def test_every_missing_input_fails_closed(
    tmp_path: Path, device_id, presented, nonce, expected
):
    """"Nothing configured, let everyone in" is the failure mode that turns a
    hardening into a bypass — and this function is reachable by an
    unauthenticated peer on a listener bound beyond loopback."""

    pair(tmp_path)

    assert (
        verify_device_proof(tmp_path, device_id, presented, nonce, port=4242).outcome
        == expected
    )


def test_a_non_ascii_proof_is_merely_wrong_rather_than_an_exception(tmp_path: Path):
    """``hmac.compare_digest`` RAISES on non-ASCII str operands, and ``presented``
    is attacker-controlled on a path no credential is needed to reach. One
    accented character used to unwind out of the loopback handshake."""

    credential = pair(tmp_path)

    auth = verify_device_proof(tmp_path, credential.device_id, "é" * 64, "a" * 64, port=4242)

    assert auth.outcome == AUTH_BAD_PROOF


def test_an_absent_or_corrupt_store_authenticates_nobody(tmp_path: Path):
    assert (
        verify_device_proof(tmp_path, "dev_x", "deadbeef", "a" * 64, port=1).outcome
        == AUTH_UNKNOWN_DEVICE
    )

    device_store_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    device_store_path(tmp_path).write_bytes(b"{not json")

    assert (
        verify_device_proof(tmp_path, "dev_x", "deadbeef", "a" * 64, port=1).outcome
        == AUTH_UNKNOWN_DEVICE
    )
    assert list_devices(tmp_path) == []


# ── revocation ──────────────────────────────────────────────────────────────


def test_a_revoked_device_is_refused_even_holding_its_token(tmp_path: Path):
    credential = pair(tmp_path)
    assert not isinstance(revoke_device(tmp_path, credential.device_id), StoreRefusal)
    proof = device_proof(
        credential.token, "a" * 64, port=4242, device_id=credential.device_id
    )

    auth = verify_device_proof(tmp_path, credential.device_id, proof, "a" * 64, port=4242)

    assert auth.outcome == AUTH_REVOKED
    assert auth.ok is False


def test_revocation_keeps_the_row_so_an_audit_can_tell_it_from_never_paired(
    tmp_path: Path,
):
    credential = pair(tmp_path, name="lost phone")
    revoke_device(tmp_path, credential.device_id, now=5000.0)

    (record,) = list_devices(tmp_path)
    assert record.revoked is True
    assert record.revoked_at
    assert record.name == "lost phone"
    # Idempotent, and the first revocation's timestamp is the one kept.
    again = revoke_device(tmp_path, credential.device_id, now=9000.0)
    assert getattr(again, "revoked_at", None) == record.revoked_at


def test_revoking_an_unknown_device_is_a_typed_refusal(tmp_path: Path):
    refused = revoke_device(tmp_path, "dev_nope")

    assert isinstance(refused, StoreRefusal)
    assert refused.reason == "unknown_device"


def test_revoking_one_device_leaves_the_other_working(tmp_path: Path):
    keeper = pair(tmp_path, now=1000.0)
    loser = pair(tmp_path, now=2000.0)
    revoke_device(tmp_path, loser.device_id)

    proof = device_proof(keeper.token, "a" * 64, port=7, device_id=keeper.device_id)
    assert verify_device_proof(tmp_path, keeper.device_id, proof, "a" * 64, port=7).ok


# ── bookkeeping ─────────────────────────────────────────────────────────────


def test_last_seen_is_stamped_and_never_fails_a_handshake(tmp_path: Path):
    credential = pair(tmp_path)
    assert lookup_device(tmp_path, credential.device_id).last_seen is None

    note_device_seen(tmp_path, credential.device_id, now=1234.0)
    assert lookup_device(tmp_path, credential.device_id).last_seen

    # Bookkeeping must not be the thing that fails an authentication: an unknown
    # id and an unwritable root are both silent.
    note_device_seen(tmp_path, "dev_nope")
    note_device_seen(tmp_path / "does" / "not" / "exist", "dev_nope")
