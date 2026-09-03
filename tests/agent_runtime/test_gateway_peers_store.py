"""``gateway/peers.json`` and the code discipline both ceremonies now share.

Two claims are load-bearing here and the rest support them.

1. **A device credential and a peer credential are never interchangeable**, and
   that is proved at the STORE rather than only at the wire: a device pairing
   code will not redeem as a peer and a peer code will not redeem as a device,
   because the pending entry carries its kind and the match is against the kind.
   The wire test (``test_serve_gateway_peer_lane.py``) proves the same thing over
   a real socket; this one proves the layer under it, so a wire refusal cannot
   silently become the only thing holding the rule up.
2. **The rate-limiting state is SHARED and the credentials are not.** One
   `pairing.json`, one lockout, one pending cap across both ceremonies — because
   a guesser grinding codes is grinding one space through one listener, and two
   counters would mean a lockout on one ceremony left the other's budget intact.

Every test takes an explicit ``store_root`` (``tmp_path``) rather than resolving
one, which is the module's own rule and the reason it exists: Stage 6's subject
is two roots at once, and a store free to re-derive its own root could pair a
peer against one install and answer for another.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime.gateway_pairing_codes import (
    CODE_LENGTH,
    KIND_DEVICE,
    KIND_PEER,
    MAX_FAILED_REDEEMS,
    MAX_PENDING_CODES,
)
from agent_runtime.gateway_peers import (
    MAX_ENDPOINTS,
    PEER_AUTH_BAD_PROOF,
    PEER_AUTH_EXPIRED,
    PEER_AUTH_OK,
    PEER_AUTH_REVOKED,
    PEER_AUTH_UNKNOWN,
    PEER_ROW_CACHE_FIELDS,
    PEER_ROW_TRUST_FIELDS,
    PeerCredential,
    PeerPairingCode,
    PeerRecord,
    clean_endpoints,
    list_peers,
    lookup_peer,
    mint_peer_code,
    note_peer_seen,
    peer_proof,
    peer_secret_verifier,
    peer_store_path,
    record_peer,
    redeem_peer_code,
    revoke_peer,
    verify_peer_proof,
)
from agent_runtime.serve_gateway_auth import (
    CREDENTIAL_TTL_SECONDS_INTRODUCED,
    DeviceCredential,
    StoreRefusal,
    device_proof,
    mint_pairing_code,
    redeem_pairing_code,
)

PEER_A = "inst_aaaaaaaaaaaa"
PEER_B = "inst_bbbbbbbbbbbb"


def _pair(root, *, peer_install_id: str = PEER_B, **kwargs) -> PeerCredential:
    """Run the store half of the ceremony: mint on A, redeem for B."""

    code = mint_peer_code(root, note="a laptop")
    assert isinstance(code, PeerPairingCode)
    credential = redeem_peer_code(
        root, code.code, peer_install_id=peer_install_id, **kwargs
    )
    assert isinstance(credential, PeerCredential), credential
    return credential


# ── the ceremony's store half ────────────────────────────────────────────────


def test_a_minted_code_writes_no_peer_row_and_returns_the_plaintext_once(tmp_path):
    """A code is an INVITATION. An install that never redeems one leaves nothing
    behind, which is what makes minting a cheap and repeatable operator move."""

    code = mint_peer_code(tmp_path, note="a laptop")

    assert isinstance(code, PeerPairingCode)
    assert len(code.code) == CODE_LENGTH
    assert code.note == "a laptop"
    assert code.expires_in_seconds() > 0
    assert list_peers(tmp_path) == []
    assert not peer_store_path(tmp_path).exists()
    # The plaintext is nowhere on disk — only a salted digest is.
    pairing = (tmp_path / "gateway" / "pairing.json").read_bytes().decode()
    assert code.code not in pairing


def test_a_redeem_writes_the_row_and_hands_the_secret_back_exactly_once(tmp_path):
    credential = _pair(
        tmp_path,
        display_name="the laptop",
        endpoints=[{"host": "10.0.0.9", "port": 8765}],
        cert_fingerprint="ab" * 32,
    )

    assert credential.peer_install_id == PEER_B
    assert len(credential.secret) == 64
    record = lookup_peer(tmp_path, PEER_B)
    assert isinstance(record, PeerRecord)
    assert record.display_name == "the laptop"
    assert record.endpoints == ({"host": "10.0.0.9", "port": 8765},)
    assert record.cert_fingerprint == "ab" * 32
    assert record.revoked is False
    assert record.last_seen is None
    assert record.approved_at


def test_the_row_shape_is_exactly_trust_fields_plus_cache_fields(tmp_path):
    """R-IP14's split, made machine-readable so it cannot rot into a comment.

    ``peers.json`` holds two KINDS of fact: credentials this install decided
    (trust) and copies of what the far install told us (cache). S2c moves the
    second kind to ``peers_cache.json``; for that move to be mechanical rather
    than archaeological, the classification has to live beside ``_row`` and be
    checked. A field added without being classified fails here — which is the
    whole point, because the alternative is a new key that nobody ever decides
    the authority for.
    """

    credential = _pair(
        tmp_path,
        display_name="the laptop",
        endpoints=[{"host": "10.0.0.9", "port": 8765}],
        cert_fingerprint="ab" * 32,
    )
    stored = json.loads(peer_store_path(tmp_path).read_bytes().decode())
    keys = set(stored["peers"][PEER_B])

    assert keys == PEER_ROW_TRUST_FIELDS | PEER_ROW_CACHE_FIELDS
    assert not (PEER_ROW_TRUST_FIELDS & PEER_ROW_CACHE_FIELDS)
    # The two facts the split is FOR, spelled out rather than left to the
    # reader: the credential is trust, and the far install's own name is not.
    assert "secret_verifier" in PEER_ROW_TRUST_FIELDS
    assert "display_name" in PEER_ROW_CACHE_FIELDS
    # S2's one new field, classified out loud. TRUST because the sharpest
    # version of the reason applies: a peer that could push its own expiry out
    # would hold a credential with no end, which is exactly the authority
    # ``revoked`` denies it.
    assert "expires_at" in PEER_ROW_TRUST_FIELDS
    assert credential.secret not in json.dumps(stored)


def test_record_and_redeem_write_the_same_key_set(tmp_path):
    """Both write paths go through ``_row``, so the two halves of one edge
    cannot end up with differently-shaped rows — the divergence that only shows
    up months later, on the side nobody tested."""

    _pair(tmp_path / "a")
    record_peer(
        tmp_path / "b",
        peer_install_id=PEER_A,
        secret="f" * 64,
        display_name="workstation",
        endpoints=[{"host": "10.0.0.4", "port": 9000}],
        cert_fingerprint="cd" * 32,
    )

    def keys(root, peer_install_id):
        raw = json.loads(peer_store_path(root).read_bytes().decode())
        return set(raw["peers"][peer_install_id])

    assert keys(tmp_path / "a", PEER_B) == keys(tmp_path / "b", PEER_A)
    assert keys(tmp_path / "b", PEER_A) == (
        PEER_ROW_TRUST_FIELDS | PEER_ROW_CACHE_FIELDS
    )


def test_the_stored_row_holds_the_digest_and_never_the_secret(tmp_path):
    """The one thing an auditor reading `peers.json` must be able to confirm."""

    credential = _pair(tmp_path)

    raw = peer_store_path(tmp_path).read_bytes().decode()
    assert credential.secret not in raw
    stored = json.loads(raw)["peers"][PEER_B]
    assert stored["secret_verifier"] == peer_secret_verifier(credential.secret)
    # And the TYPE that renders a peer to a human cannot carry it at all.
    assert "secret_verifier" not in lookup_peer(tmp_path, PEER_B).payload()
    assert "secret" not in json.dumps(lookup_peer(tmp_path, PEER_B).payload())


def test_the_field_name_differs_from_the_device_rows_so_a_copied_row_is_inert(
    tmp_path,
):
    """Two credential stores in one directory. A row moved between them by a
    script, a merge, or a hand edit must decode to nothing — not to something
    that works. Three independent guards: the key name, the id field, and the
    proof prefix."""

    code = mint_pairing_code(tmp_path)
    device = redeem_pairing_code(tmp_path, code.code)
    assert isinstance(device, DeviceCredential)
    device_row = json.loads(
        (tmp_path / "gateway" / "devices.json").read_bytes().decode()
    )["devices"][device.device_id]

    # The device row's credential lives under `verifier`; the peer reader looks
    # for `secret_verifier`, so this row authenticates nobody as a peer.
    assert "verifier" in device_row and "secret_verifier" not in device_row
    # Written into the peer store verbatim, it does not even decode to a record.
    peer_store_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    peer_store_path(tmp_path).write_bytes(
        json.dumps({"contract": 1, "peers": {device.device_id: device_row}}).encode()
    )
    assert lookup_peer(tmp_path, device.device_id) is None
    assert list_peers(tmp_path) == []


def test_record_peer_writes_the_other_half_from_the_secret_it_was_given(tmp_path):
    """The joining side does not mint. An edge has exactly ONE secret, and a
    side that minted its own would be describing a different credential."""

    joiner = tmp_path / "b"
    record = record_peer(
        joiner,
        peer_install_id=PEER_A,
        secret="f" * 64,
        display_name="workstation",
        endpoints=[{"host": "10.0.0.4", "port": 9000}],
        cert_fingerprint="cd" * 32,
    )

    assert isinstance(record, PeerRecord)
    stored = json.loads(peer_store_path(joiner).read_bytes().decode())["peers"][PEER_A]
    assert stored["secret_verifier"] == peer_secret_verifier("f" * 64)


def test_both_halves_of_one_edge_hold_the_same_verifier(tmp_path):
    """The property the whole symmetric design rests on, asserted end to end
    rather than assumed from two functions that look alike."""

    a_root, b_root = tmp_path / "a", tmp_path / "b"
    credential = _pair(a_root, peer_install_id=PEER_B, display_name="laptop")
    record_peer(b_root, peer_install_id=PEER_A, secret=credential.secret)

    a_side = json.loads(peer_store_path(a_root).read_bytes().decode())["peers"][PEER_B]
    b_side = json.loads(peer_store_path(b_root).read_bytes().decode())["peers"][PEER_A]
    assert a_side["secret_verifier"] == b_side["secret_verifier"]
    # …and each row names the OTHER install, never itself.
    assert a_side["peer_install_id"] == PEER_B
    assert b_side["peer_install_id"] == PEER_A


def test_a_repair_replaces_the_row_rather_than_adding_a_second_edge(tmp_path):
    """An install keeps the id it minted at Stage 0, so a re-pair UPDATES an
    edge. Refusing it would mean a rebuilt machine could never be re-paired
    without an operator hand-editing JSON."""

    first = _pair(tmp_path, display_name="old name")
    second = _pair(tmp_path, display_name="new name")

    assert first.secret != second.secret
    assert [record.peer_install_id for record in list_peers(tmp_path)] == [PEER_B]
    assert lookup_peer(tmp_path, PEER_B).display_name == "new name"


# ── the two ceremonies do not trade codes ────────────────────────────────────


def test_a_device_code_does_not_redeem_as_a_peer(tmp_path):
    code = mint_pairing_code(tmp_path)

    outcome = redeem_peer_code(tmp_path, code.code, peer_install_id=PEER_B)

    assert isinstance(outcome, StoreRefusal)
    assert outcome.reason == "invalid_code"
    assert list_peers(tmp_path) == []
    # The device code is untouched and still redeems as what it is.
    assert isinstance(redeem_pairing_code(tmp_path, code.code), DeviceCredential)


def test_a_peer_code_does_not_redeem_as_a_device(tmp_path):
    code = mint_peer_code(tmp_path)

    outcome = redeem_pairing_code(tmp_path, code.code)

    assert isinstance(outcome, StoreRefusal)
    assert outcome.reason == "invalid_code"
    # And it still redeems as what it is.
    assert isinstance(
        redeem_peer_code(tmp_path, code.code, peer_install_id=PEER_B), PeerCredential
    )


def test_the_two_kinds_share_one_pending_map_and_one_cap(tmp_path):
    """The cap is over the OPERATOR's outstanding codes, not over a per-ceremony
    resource: three unredeemed codes at once is a mistake either way."""

    # Two device codes and one peer code — three outstanding, which is the cap,
    # reached ACROSS the two ceremonies rather than three-per-ceremony.
    assert isinstance(mint_pairing_code(tmp_path).code, str)
    assert isinstance(mint_peer_code(tmp_path), PeerPairingCode)
    assert isinstance(mint_pairing_code(tmp_path).code, str)

    fourth = mint_peer_code(tmp_path)
    assert isinstance(fourth, StoreRefusal)
    assert fourth.reason == "too_many_pending"
    assert str(MAX_PENDING_CODES) in fourth.detail
    # …and the DEVICE ceremony is capped by the same three, which is the half a
    # per-ceremony cap would have got wrong in the more permissive direction.
    assert isinstance(mint_pairing_code(tmp_path), StoreRefusal)

    state = json.loads((tmp_path / "gateway" / "pairing.json").read_bytes().decode())
    kinds = sorted(entry["kind"] for entry in state["pending"].values())
    assert kinds == [KIND_DEVICE, KIND_DEVICE, KIND_PEER]


def test_failed_peer_redeems_lock_out_the_device_ceremony_too(tmp_path):
    """One listener, one code space, one failure budget. Two counters would mean
    an attacker locked out of one ceremony simply ground the other."""

    for _ in range(MAX_FAILED_REDEEMS):
        assert isinstance(
            redeem_peer_code(tmp_path, "ZZZZZZZZ", peer_install_id=PEER_B),
            StoreRefusal,
        )

    device = mint_pairing_code(tmp_path)
    assert isinstance(device, StoreRefusal)
    assert device.reason == "locked_out"
    peer = mint_peer_code(tmp_path)
    assert isinstance(peer, StoreRefusal)
    assert peer.reason == "locked_out"


def test_a_successful_peer_redeem_clears_the_shared_failure_streak(tmp_path):
    """Isolated typos must not accumulate for the life of the store and trip a
    lockout on one fresh mistake — ``pairing.py``'s own correction."""

    for _ in range(MAX_FAILED_REDEEMS - 1):
        redeem_peer_code(tmp_path, "ZZZZZZZZ", peer_install_id=PEER_B)
    _pair(tmp_path)

    state = json.loads((tmp_path / "gateway" / "pairing.json").read_bytes().decode())
    assert state["failed_redeems"] == 0
    assert not state["locked_until"]


def test_a_peer_code_is_one_shot(tmp_path):
    code = mint_peer_code(tmp_path)
    assert isinstance(redeem_peer_code(tmp_path, code.code, peer_install_id=PEER_B), PeerCredential)

    again = redeem_peer_code(tmp_path, code.code, peer_install_id="inst_other")
    assert isinstance(again, StoreRefusal)
    assert again.reason == "invalid_code"
    assert lookup_peer(tmp_path, "inst_other") is None


def test_a_redeem_that_names_no_install_is_refused_before_the_code_is_spent(tmp_path):
    """The edge is symmetric, so a row keyed by nothing is a peer this install
    could never dial back and never recognise again. Refused BEFORE the code is
    consumed, so an operator's next attempt still has a code to use."""

    code = mint_peer_code(tmp_path)

    outcome = redeem_peer_code(tmp_path, code.code, peer_install_id="   ")

    assert isinstance(outcome, StoreRefusal)
    assert outcome.reason == "invalid_peer_id"
    assert isinstance(
        redeem_peer_code(tmp_path, code.code, peer_install_id=PEER_B), PeerCredential
    )


# ── the proof ────────────────────────────────────────────────────────────────


def test_a_peer_proves_itself_with_the_verifier_both_sides_hold(tmp_path):
    credential = _pair(tmp_path)
    verifier = peer_secret_verifier(credential.secret)

    auth = verify_peer_proof(
        tmp_path,
        PEER_B,
        peer_proof(verifier, "n0nce", port=9000, peer_install_id=PEER_B),
        "n0nce",
        port=9000,
    )

    assert auth.outcome == PEER_AUTH_OK
    assert auth.record.peer_install_id == PEER_B


def test_the_proof_binds_the_port_the_nonce_and_the_install_id(tmp_path):
    """Three bindings, three holes. A relay to another port, a replayed
    transcript, and a proof presented under a different install's name."""

    credential = _pair(tmp_path)
    verifier = peer_secret_verifier(credential.secret)
    good = peer_proof(verifier, "n0nce", port=9000, peer_install_id=PEER_B)

    for nonce, port in (("n0nce", 9001), ("other", 9000)):
        assert (
            verify_peer_proof(tmp_path, PEER_B, good, nonce, port=port).outcome
            == PEER_AUTH_BAD_PROOF
        )
    # And under another install's name, with that install genuinely paired: the
    # proof was minted naming B and does not verify as A.
    record_peer(tmp_path, peer_install_id=PEER_A, secret=credential.secret)
    assert (
        verify_peer_proof(tmp_path, PEER_A, good, "n0nce", port=9000).outcome
        == PEER_AUTH_BAD_PROOF
    )


def test_a_device_proof_is_not_a_peer_proof_even_with_a_colliding_credential(tmp_path):
    """The ``pwv``/``gwv`` prefix split, earning its keep: the ONE case where the
    two derivations could otherwise agree is a device and a peer holding the
    same bytes, which is exactly what is constructed here."""

    credential = _pair(tmp_path)
    shared = credential.secret

    device_shaped = device_proof(shared, "n0nce", port=9000, device_id=PEER_B)
    assert (
        verify_peer_proof(tmp_path, PEER_B, device_shaped, "n0nce", port=9000).outcome
        == PEER_AUTH_BAD_PROOF
    )


def test_every_missing_input_is_a_refusal_rather_than_a_degrade(tmp_path):
    """Reachable by an unauthenticated peer on a listener bound beyond loopback,
    so "nothing configured, let everyone in" is the failure mode that turns a
    hardening into a bypass."""

    credential = _pair(tmp_path)
    verifier = peer_secret_verifier(credential.secret)
    good = peer_proof(verifier, "n0nce", port=9000, peer_install_id=PEER_B)

    assert verify_peer_proof(tmp_path, "", good, "n0nce", port=9000).ok is False
    assert verify_peer_proof(tmp_path, "x" * 129, good, "n0nce", port=9000).ok is False
    assert verify_peer_proof(tmp_path, PEER_B, None, "n0nce", port=9000).ok is False
    assert verify_peer_proof(tmp_path, PEER_B, good, "", port=9000).ok is False
    assert (
        verify_peer_proof(tmp_path, "inst_never_paired", good, "n0nce", port=9000).outcome
        == PEER_AUTH_UNKNOWN
    )


def test_a_hostile_non_ascii_proof_is_merely_wrong(tmp_path):
    """``hmac.compare_digest`` RAISES on a non-ASCII str operand, and this input
    is attacker-controlled on a path no credential is needed to reach. One
    accented character used to unwind out of the loopback handshake entirely."""

    _pair(tmp_path)

    auth = verify_peer_proof(tmp_path, PEER_B, "é" * 64, "n0nce", port=9000)

    assert auth.outcome == PEER_AUTH_BAD_PROOF


def test_a_corrupt_store_authenticates_nobody_rather_than_raising(tmp_path):
    credential = _pair(tmp_path)
    verifier = peer_secret_verifier(credential.secret)
    peer_store_path(tmp_path).write_bytes(b"{ this is not json")

    auth = verify_peer_proof(
        tmp_path,
        PEER_B,
        peer_proof(verifier, "n0nce", port=9000, peer_install_id=PEER_B),
        "n0nce",
        port=9000,
    )

    assert auth.outcome == PEER_AUTH_UNKNOWN
    assert list_peers(tmp_path) == []


# ── revocation ───────────────────────────────────────────────────────────────


def test_revocation_is_checked_after_the_proof_so_ids_cannot_be_probed(tmp_path):
    """Checking it first would let an unauthenticated caller learn which install
    ids are revoked — and by difference, which are live — holding nothing."""

    credential = _pair(tmp_path)
    verifier = peer_secret_verifier(credential.secret)
    assert isinstance(revoke_peer(tmp_path, PEER_B), PeerRecord)

    with_credential = verify_peer_proof(
        tmp_path,
        PEER_B,
        peer_proof(verifier, "n0nce", port=9000, peer_install_id=PEER_B),
        "n0nce",
        port=9000,
    )
    without = verify_peer_proof(tmp_path, PEER_B, "0" * 64, "n0nce", port=9000)

    assert with_credential.outcome == PEER_AUTH_REVOKED
    assert without.outcome == PEER_AUTH_BAD_PROOF


def test_a_revoked_row_is_kept_and_listed(tmp_path):
    """"Never paired" and "thrown out" must not be the same answer: the second
    is the one an operator auditing a decommissioned machine needs."""

    _pair(tmp_path)
    revoke_peer(tmp_path, PEER_B)

    rows = list_peers(tmp_path)
    assert [row.peer_install_id for row in rows] == [PEER_B]
    assert rows[0].revoked is True
    assert rows[0].revoked_at


def test_revoke_is_idempotent_and_refuses_an_install_nobody_paired(tmp_path):
    _pair(tmp_path)
    first = revoke_peer(tmp_path, PEER_B)
    second = revoke_peer(tmp_path, PEER_B)

    assert isinstance(first, PeerRecord) and isinstance(second, PeerRecord)
    assert first.revoked_at == second.revoked_at

    missing = revoke_peer(tmp_path, "inst_never_paired")
    assert isinstance(missing, StoreRefusal)
    assert missing.reason == "unknown_peer"


def test_note_peer_seen_stamps_and_never_raises(tmp_path):
    _pair(tmp_path)
    note_peer_seen(tmp_path, PEER_B)
    assert lookup_peer(tmp_path, PEER_B).last_seen

    # Bookkeeping must never be the thing that fails an authentication.
    note_peer_seen(tmp_path, "inst_never_paired")
    note_peer_seen(tmp_path, "")


# ── endpoints ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({"host": "10.0.0.4", "port": 9000}, ({"host": "10.0.0.4", "port": 9000},)),
        ([{"host": "10.0.0.4", "port": "9000"}], ({"host": "10.0.0.4", "port": 9000},)),
        ([{"host": " 10.0.0.4 ", "port": 9000}], ({"host": "10.0.0.4", "port": 9000},)),
        ([{"host": "", "port": 9000}], ()),
        ([{"host": "10.0.0.4", "port": 0}], ()),
        ([{"host": "10.0.0.4", "port": 70000}], ()),
        ([{"host": "10.0.0.4"}], ()),
        (["10.0.0.4:9000"], ()),
        ("nonsense", ()),
        (None, ()),
    ],
)
def test_endpoints_arriving_over_the_wire_are_bounded_and_typed(raw, expected):
    """This runs on data the OTHER install sent, before it has proven anything."""

    assert clean_endpoints(raw) == expected


def test_a_malformed_endpoint_is_dropped_rather_than_failing_the_pairing():
    """Three good addresses and one bad one is an install with three addresses.
    Failing the whole ceremony over the fourth would make a machine with an
    unusual interface unpairable for a reason nobody could see."""

    cleaned = clean_endpoints(
        [
            {"host": "10.0.0.4", "port": 9000},
            {"host": "", "port": 1},
            {"host": "fe80::1", "port": 9000},
        ]
    )

    assert cleaned == (
        {"host": "10.0.0.4", "port": 9000},
        {"host": "fe80::1", "port": 9000},
    )


def test_the_endpoint_list_is_capped_and_deduplicated():
    cleaned = clean_endpoints(
        [{"host": "10.0.0.4", "port": 9000}] * 3
        + [{"host": f"10.0.0.{n}", "port": 9000} for n in range(20)]
    )

    assert len(cleaned) == MAX_ENDPOINTS
    assert len(set(tuple(sorted(row.items())) for row in cleaned)) == MAX_ENDPOINTS


def test_a_fingerprint_that_is_not_one_reads_as_absent_not_as_a_pin(tmp_path):
    """``None`` and "" must not be spelled the same: the dialer branches on
    presence, and "pin nothing" is a real and much weaker posture than
    "pin this"."""

    record = record_peer(
        tmp_path, peer_install_id=PEER_A, secret="f" * 64, cert_fingerprint="not-a-hash"
    )

    assert isinstance(record, PeerRecord)
    assert record.cert_fingerprint is None
    assert (
        record_peer(
            tmp_path, peer_install_id=PEER_A, secret="f" * 64, cert_fingerprint="AB" * 32
        ).cert_fingerprint
        == "ab" * 32
    )


# ── S2: expiry, and the code that is scoped to one requester ────────────────


def test_a_peer_code_scoped_to_an_install_refuses_any_other_install_and_charges_a_failure(
    tmp_path,
):
    """R-S2-4. A code is a bearer for its ten minutes; a code minted with
    ``for_install_id`` is a bearer only the named install can spend, because the
    join hello has to name the redeemer's own id in the same frame.

    The refusal is byte-identical to "no such code" and charges the same
    failure, so the wrong install cannot use the difference to learn that a
    pairing is in flight. And the pending entry SURVIVES: the code was not
    spent, so the install it was meant for can still redeem it inside the window
    instead of having to re-run the ceremony because somebody guessed at it.
    """

    from agent_runtime.gateway_pairing_codes import pending_codes
    from agent_runtime.serve_gateway_auth import _read_pairing

    minted = mint_peer_code(tmp_path, for_install_id=PEER_B)
    assert isinstance(minted, PeerPairingCode)

    wrong = redeem_peer_code(tmp_path, minted.code, peer_install_id=PEER_A)
    assert isinstance(wrong, StoreRefusal)
    assert wrong.reason == "invalid_code"
    assert "no pending peer code matches" in wrong.detail

    state = _read_pairing(tmp_path)
    assert int(state.get("failed_redeems") or 0) == 1
    assert len(pending_codes(state)) == 1
    assert list_peers(tmp_path) == []

    # …and the install it WAS for still gets its edge.
    right = redeem_peer_code(tmp_path, minted.code, peer_install_id=PEER_B)
    assert isinstance(right, PeerCredential)
    assert [row.peer_install_id for row in list_peers(tmp_path)] == [PEER_B]


def test_an_unscoped_code_is_still_spendable_by_anybody_which_is_the_manual_ceremony(
    tmp_path,
):
    """The manual verbs mint without a scope and must keep working exactly as
    they did: an operator carrying eight characters to a machine they are
    standing at IS the provenance, and there is nothing for the store to check."""

    minted = mint_peer_code(tmp_path)
    assert isinstance(minted, PeerPairingCode)

    credential = redeem_peer_code(tmp_path, minted.code, peer_install_id=PEER_A)
    assert isinstance(credential, PeerCredential)
    assert credential.expires_at is None


def test_a_peer_code_minted_with_a_ttl_redeems_into_a_row_that_expires(tmp_path):
    """The stamp is computed at REDEEM and not at mint, so a code that sits for
    nine of its ten minutes does not spend nine minutes of the credential's
    thirty days."""

    import time

    minted = mint_peer_code(
        tmp_path, credential_ttl_seconds=CREDENTIAL_TTL_SECONDS_INTRODUCED
    )
    assert isinstance(minted, PeerPairingCode)

    at = time.time()
    credential = redeem_peer_code(
        tmp_path, minted.code, peer_install_id=PEER_B, now=at
    )
    assert isinstance(credential, PeerCredential)
    assert credential.expires_at is not None

    record = lookup_peer(tmp_path, PEER_B)
    assert record.expires_at == credential.expires_at
    assert record.expired is False
    # ~30 days out, checked as a window rather than an equality so a second of
    # clock drift inside the call is not a failure.
    from datetime import datetime, timezone

    delta = datetime.fromisoformat(record.expires_at) - datetime.fromtimestamp(
        at, tz=timezone.utc
    )
    assert abs(delta.total_seconds() - CREDENTIAL_TTL_SECONDS_INTRODUCED) < 5


def test_an_expired_peer_is_refused_after_the_proof_with_its_own_reason(tmp_path):
    """Ordering, asserted rather than assumed: a BAD proof on an expired row
    answers ``bad_proof``, and only a GOOD proof learns the row has lapsed.

    Checking expiry first would let an unauthenticated peer sweep install ids
    and learn which are expired — and, by difference, which are live — holding
    nothing at all. Same argument as the revocation arm, same position.
    """

    minted = mint_peer_code(tmp_path, credential_ttl_seconds=60)
    credential = redeem_peer_code(
        tmp_path, minted.code, peer_install_id=PEER_B, now=1_000.0
    )
    assert isinstance(credential, PeerCredential)

    verifier = peer_secret_verifier(credential.secret)
    good = peer_proof(verifier, "nonce-1", port=9000, peer_install_id=PEER_B)

    import time as _time

    class _Clock:
        """Freeze "now" past the expiry without touching the stored row."""

    # The row was written against ``now=1000`` with a 60s TTL, so its stamp is
    # in 1970. Every wall clock this test could run under is past it.
    fresh = verify_peer_proof(tmp_path, PEER_B, good, "nonce-1", port=9000)
    assert fresh.outcome == PEER_AUTH_EXPIRED
    assert fresh.record is not None

    bad = verify_peer_proof(tmp_path, PEER_B, "00" * 32, "nonce-1", port=9000)
    assert bad.outcome == PEER_AUTH_BAD_PROOF
    assert bad.record is None
    assert _time is not None and _Clock is not None  # keep the imports honest


def test_a_revoked_and_expired_row_reports_revoked_because_a_decision_outranks_a_clock(
    tmp_path,
):
    minted = mint_peer_code(tmp_path, credential_ttl_seconds=60)
    credential = redeem_peer_code(
        tmp_path, minted.code, peer_install_id=PEER_B, now=1_000.0
    )
    revoke_peer(tmp_path, PEER_B)

    verifier = peer_secret_verifier(credential.secret)
    good = peer_proof(verifier, "n", port=9000, peer_install_id=PEER_B)

    assert verify_peer_proof(tmp_path, PEER_B, good, "n", port=9000).outcome == (
        PEER_AUTH_REVOKED
    )


def test_record_peer_stores_the_expiry_the_far_side_minted(tmp_path):
    """The joining side takes the stamp it was HANDED rather than deriving one.
    Two ends of an edge that each computed their own would lapse minutes — or,
    with a skewed clock, days — apart."""

    outcome = record_peer(
        tmp_path,
        peer_install_id=PEER_A,
        secret="f" * 64,
        display_name="workstation",
        endpoints=[{"host": "10.0.0.4", "port": 9000}],
        expires_at="2099-01-01T00:00:00+00:00",
    )
    assert isinstance(outcome, PeerRecord)
    assert outcome.expires_at == "2099-01-01T00:00:00+00:00"
    assert outcome.expired is False

    lapsed = record_peer(
        tmp_path,
        peer_install_id=PEER_B,
        secret="e" * 64,
        expires_at="2000-01-01T00:00:00+00:00",
    )
    assert lapsed.expired is True


def test_a_row_without_an_expiry_never_expires_so_pre_s2_stores_keep_working(tmp_path):
    """The whole migration story: the only new fact has a legal absent value, so
    there is no rewrite pass and no contract bump."""

    _pair(tmp_path)
    record = lookup_peer(tmp_path, PEER_B)

    assert record.expires_at is None
    assert record.expired is False
    assert record.payload()["expires_at"] is None
    assert record.payload()["expired"] is False


def test_dial_peer_refuses_an_expired_row_before_it_opens_a_socket(tmp_path, monkeypatch):
    """Beside the revocation refusal and for its reason: a credential this side
    already knows is dead costs no attempt. Proved with a client that raises if
    it is constructed, so the assertion reads the ORDERING rather than trusting a
    comment."""

    from agent_runtime import gateway_peers, serve_socket

    record_peer(
        tmp_path,
        peer_install_id=PEER_A,
        secret="f" * 64,
        display_name="workstation",
        endpoints=[{"host": "10.0.0.4", "port": 9000}],
        cert_fingerprint="ab" * 32,
        expires_at="2000-01-01T00:00:00+00:00",
    )
    gateway_peers.record_peer  # the module is the one under test

    def _explode(*args, **kwargs):
        raise AssertionError("dial_peer opened a socket for an expired row")

    monkeypatch.setattr(serve_socket, "ServeSocketClient", _explode)

    with pytest.raises(ConnectionError) as raised:
        gateway_peers.dial_peer(tmp_path, PEER_A)

    assert "expired" in str(raised.value)
