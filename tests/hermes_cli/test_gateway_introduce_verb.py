"""``harness gateway introduce`` and ``gateway id``'s new endpoint block (S2).

Same harness as its two siblings (``test_gateway_pairing_verbs.py`` /
``test_gateway_peer_verbs.py``): the REAL argparse tree, dispatched through
``args.func``, because a handler nothing routes to is a verb no operator can run
and registration is the half a handler test cannot see.

What is proved here is the part neither the store tests nor the two-roots e2e
can see: that ONE verb composes the two existing mints into ONE envelope shaped
the way the backend's fulfil endpoint wants it, that it refuses rather than
notes when nothing is listening, that a half which could not mint is REPORTED
rather than silently absent, and that the plaintext codes appear on stdout and
in no store, event or log line.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent_runtime import paths


@pytest.fixture(autouse=True)
def hermetic_runtime_root(tmp_path, monkeypatch):
    """Pin the runtime root INSIDE this test's tmp dir, and prove it landed.

    This file MINTS CREDENTIALS, twice per run. A resolution regression would
    introduce a stranger to the operator's own install — the failure this
    fixture exists to make impossible rather than unlikely.
    """

    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    resolved = paths.store_root().resolve()
    assert resolved == root.resolve() or root.resolve() in resolved.parents, (
        f"store_root() resolved to {resolved}, OUTSIDE {root}: this test would "
        "mint credentials against a runtime root nobody in this repo controls."
    )
    return root


@pytest.fixture(autouse=True)
def gateway_configured(monkeypatch):
    """An operator who has turned the lane on, so the endpoint is answerable."""

    from hermes_cli.harness_parts import serve as serve_module

    monkeypatch.setattr(
        serve_module, "gateway_listen_config", lambda: ("10.0.0.4", 8765)
    )


def _dispatch(argv: list[str]) -> int:
    from hermes_cli import harness

    root = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(root.add_subparsers(dest="command"))
    args = root.parse_args(argv)
    return args.func(args)


def _run(capsys, *argv: str) -> tuple[int, dict]:
    code = _dispatch(["harness", "gateway", *argv, "--json"])
    out = capsys.readouterr()
    text = out.out
    start = text.find("{")
    return code, (json.loads(text[start:]) if start >= 0 else {"_stderr": out.err})


def _introduce(capsys, *extra: str) -> tuple[int, dict]:
    return _run(capsys, "introduce", *extra)


# ── the envelope ─────────────────────────────────────────────────────────────


def test_introduce_mints_both_halves_and_prints_one_envelope_whose_grant_payload_is_the_backend_shape(
    capsys,
):
    """The whole point of the verb: ONE object a launcher POSTs verbatim.

    ``grant_payload``'s key set is asserted EXACTLY against the backend S1
    packet §4.1's recommended keys, because "recommended" on the other side of a
    repo boundary is only a contract if one side pins it. Building it here — and
    not letting the launcher assemble it from the envelope's other keys — is
    what keeps "what fulfil receives" a decision made once.
    """

    from hermes_cli.harness_parts.gateway_commands import (
        GRANT_PAYLOAD_KEYS,
        GRANT_PAYLOAD_MAX_BYTES,
    )

    code, payload = _introduce(
        capsys,
        "--for-install",
        "install-a",
        "--for-device",
        "dev-acct-1",
        "--correlation",
        "grant-1",
    )

    assert code == 0, payload
    assert payload["kind"] == "gateway_introduction"
    assert set(payload["grant_payload"]) == set(GRANT_PAYLOAD_KEYS)

    # Both halves are present, and each carries the code its own ceremony
    # redeems — the peer half as ``peer_code``, the device half as ``code``, so
    # a payload pasted into the wrong verb is refused for its SHAPE.
    assert len(payload["peer"]["peer_code"]) == 8
    assert len(payload["device"]["code"]) == 8
    assert payload["device"]["tier"] == "console"
    assert "refusals" not in payload

    join = json.loads(payload["grant_payload"]["peer_join_payload"])
    pair = json.loads(payload["grant_payload"]["device_pair_payload"])
    assert join["peer_code"] == payload["peer"]["peer_code"]
    assert "code" not in join
    assert pair["code"] == payload["device"]["code"]
    assert "peer_code" not in pair
    # Both payloads name the SAME install and the SAME certificate: one
    # introduction is one machine, and two halves that disagreed about which
    # would be an introduction to two different places.
    assert join["install_id"] == pair["install_id"] == payload["install_id"]
    assert join["cert_fingerprint"] == pair["cert_fingerprint"]

    compact = json.dumps(
        payload["grant_payload"], separators=(",", ":"), sort_keys=True
    )
    assert len(compact.encode("utf-8")) <= GRANT_PAYLOAD_MAX_BYTES


def test_both_nested_payloads_carry_the_candidate_list_and_a_dialable_first_row(
    capsys, monkeypatch
):
    """R-D1 + R-D3 on the two payloads that are actually POSTed to the backend.

    Before this, both were built from ``_endpoint(root)["host"]`` — the
    listener's BIND — while the grant's own top-level ``endpoints`` list was
    already correct. So one envelope carried a good list and two payloads
    carrying ``0.0.0.0``, and the two things that get dialled were the wrong
    ones. They are now the same answer three times, which is what makes the
    contract checkable in one assertion.
    """

    from hermes_cli.harness_parts import gateway_commands, serve as serve_module

    monkeypatch.setattr(
        serve_module, "gateway_listen_config", lambda: ("0.0.0.0", 8765)
    )
    monkeypatch.setattr(
        gateway_commands,
        "_machine_addresses",
        lambda: ["192.168.1.203", "10.97.7.100"],
    )

    code, payload = _introduce(
        capsys, "--for-install", "install-a", "--for-device", "dev-acct-1"
    )
    assert code == 0

    expected = [
        {"host": "192.168.1.203", "port": 8765},
        {"host": "10.97.7.100", "port": 8765},
    ]
    join = json.loads(payload["grant_payload"]["peer_join_payload"])
    pair = json.loads(payload["grant_payload"]["device_pair_payload"])

    for nested in (join, pair):
        assert nested["endpoints"] == expected
        assert (nested["host"], nested["port"]) == ("192.168.1.203", 8765)
    # The grant's own list, the two nested lists, and the envelope's: one answer.
    assert payload["endpoints"] == payload["grant_payload"]["endpoints"] == expected
    assert "0.0.0.0" not in json.dumps(payload)


def test_introduce_refuses_a_wildcard_bind_that_enumerates_no_address(
    capsys, monkeypatch
):
    """R-D1's refusal, and it is a DIFFERENT condition from the listener being
    off: the lane is on and a bind exists, but this machine cannot say which
    address the requester should use. A launcher that got the listener-off
    sentence here would send an operator to a config key that is already set."""

    from agent_runtime.serve_gateway_auth import pairing_store_path
    from hermes_cli.harness_parts import gateway_commands, serve as serve_module
    from hermes_cli.harness_parts.gateway_commands import (
        LISTENER_OFF_SENTENCE,
        NO_DIAL_HOST_SENTENCE,
    )
    from hermes_cli.harness_support import ERROR_EXIT_CODES

    monkeypatch.setattr(
        serve_module, "gateway_listen_config", lambda: ("0.0.0.0", 8765)
    )
    monkeypatch.setattr(gateway_commands, "_machine_addresses", lambda: [])

    code = _dispatch(
        ["harness", "gateway", "introduce", "--for-install", "install-a", "--json"]
    )
    out = capsys.readouterr()
    text = out.out + out.err

    assert code == ERROR_EXIT_CODES["runtime_unavailable"]
    assert NO_DIAL_HOST_SENTENCE in text
    assert LISTENER_OFF_SENTENCE not in text
    assert not pairing_store_path(paths.store_root()).exists()


def test_introduce_stamps_the_correlation_on_the_envelope_and_refuses_an_unfit_token(
    capsys,
):
    """R-IP17: the grant id is the correlation id and every party writes it.
    Every party can only write ONE token if every party agrees what a legal one
    is, so the fence is the RPC lane's own (``state_patches``) rather than a
    second spelling — and an unfit token is REFUSED rather than repaired,
    because a sanitized id would print a value neither side used."""

    from hermes_cli.harness_support import ERROR_EXIT_CODES

    code, payload = _introduce(capsys, "--for-install", "install-a", "--correlation", "g-abc.1")
    assert code == 0
    assert payload["correlation"] == "g-abc.1"
    assert payload["grant_payload"]["correlation"] == "g-abc.1"

    code = _dispatch(
        [
            "harness",
            "gateway",
            "introduce",
            "--for-install",
            "install-a",
            "--correlation",
            "not a token",
            "--json",
        ]
    )
    out = capsys.readouterr()
    assert code == ERROR_EXIT_CODES["invalid_payload"]
    assert "correlation" in (out.out + out.err)


def test_introduce_requires_at_least_one_for_flag(capsys):
    """Neither half means nobody to scope the codes TO — and an unscoped code is
    exactly what ``gateway pair`` and ``peers pair`` already mint, so this verb
    would be a third spelling of them."""

    from hermes_cli.harness_support import ERROR_EXIT_CODES

    code = _dispatch(["harness", "gateway", "introduce", "--json"])
    out = capsys.readouterr()

    assert code == ERROR_EXIT_CODES["invalid_payload"]
    assert "--for-install" in (out.out + out.err)


def test_introduce_with_only_for_device_mints_the_device_half_only(capsys):
    """A phone has no install id. The device half is all there is to mint for
    it, and the peer half is ``null`` rather than absent so a reader never
    branches on a missing key."""

    code, payload = _introduce(capsys, "--for-device", "dev-acct-1")

    assert code == 0
    assert payload["peer"] is None
    assert payload["grant_payload"]["peer_join_payload"] is None
    assert len(payload["device"]["code"]) == 8
    assert payload["for_install_id"] is None
    assert payload["for_device_id"] == "dev-acct-1"


def test_introduce_with_only_for_install_mints_the_peer_half_only(capsys):
    code, payload = _introduce(capsys, "--for-install", "install-a")

    assert code == 0
    assert payload["device"] is None
    assert payload["grant_payload"]["device_pair_payload"] is None
    assert len(payload["peer"]["peer_code"]) == 8


def test_introduce_refuses_when_the_listener_is_off_with_peers_pairs_sentence(
    capsys, monkeypatch
):
    """Where ``peers pair`` prints a NOTE, this refuses — and the difference is
    the consumer. A note is for a human who can go turn the listener on; this
    verb's envelope is POSTed to a backend and read by a machine that will dial
    a door nobody opened. Family 7, because the identical command succeeds after
    a restart."""

    from hermes_cli.harness_parts import serve as serve_module
    from hermes_cli.harness_parts.gateway_commands import LISTENER_OFF_SENTENCE
    from hermes_cli.harness_support import ERROR_EXIT_CODES

    monkeypatch.setattr(serve_module, "gateway_listen_config", lambda: (None, 0))

    code = _dispatch(
        ["harness", "gateway", "introduce", "--for-install", "install-a", "--json"]
    )
    out = capsys.readouterr()

    assert code == ERROR_EXIT_CODES["runtime_unavailable"]
    assert LISTENER_OFF_SENTENCE in (out.out + out.err)
    # Nothing was minted: a refusal that had already written a pending entry
    # would burn one of the three the operator is allowed.
    from agent_runtime.serve_gateway_auth import pairing_store_path

    assert not pairing_store_path(paths.store_root()).exists()


def test_a_config_only_endpoint_is_allowed_with_a_note_because_the_serve_may_simply_not_have_booted(
    capsys,
):
    """``config`` is a real and recoverable ordering — the lane is on, the
    process is not up yet — and refusing it would make the ceremony depend on a
    boot order nobody chose. Stated on the envelope rather than silent."""

    code, payload = _introduce(capsys, "--for-install", "install-a")

    assert code == 0
    assert payload["endpoints_source"] == "config"
    assert "NEXT boot" in payload["note_endpoint"]


def test_introduce_when_the_second_mint_hits_the_pending_cap_reports_the_half_that_refused(
    capsys,
):
    """Two atomic mints, not one atomic pair. The shared cap (three, across both
    ceremonies) can legitimately refuse the second half after the first landed,
    and holding one lock across both would not change that — it would only make
    the refusal arrive with a half-built envelope and no way to say which half
    failed. So the exit is the refusal's own family and the operator learns
    WHICH."""

    from hermes_cli.harness_support import ERROR_EXIT_CODES

    for _ in range(3):
        _dispatch(["harness", "gateway", "peers", "pair", "--json"])
    capsys.readouterr()

    code = _dispatch(
        [
            "harness",
            "gateway",
            "introduce",
            "--for-install",
            "install-a",
            "--for-device",
            "dev-acct-1",
            "--json",
        ]
    )
    out = capsys.readouterr()

    assert code == ERROR_EXIT_CODES["pairing_codes_pending"]
    assert "outstanding" in (out.out + out.err)


def test_three_introduces_for_one_requester_leave_one_pending_row_per_half(capsys):
    """R-D5 through the verb that actually trips the cap.

    The launcher retries a stalled pair edge at 2 s, 8 s and 30 s
    (``kMissionPairRetryBackoff``) and each ``introduce`` mints TWO codes, so
    against ``MAX_PENDING_CODES = 3`` the SECOND attempt for one device was
    already refused ``too_many_pending`` — which the launcher rendered as
    ``refused``, which is why S4's receipts show the loop giving up at 12:00:24
    with both machines healthy. Retrying is now free.
    """

    from agent_runtime.gateway_pairing_codes import KIND_DEVICE, KIND_PEER
    from agent_runtime.serve_gateway_auth import _read_pairing

    for _ in range(3):
        code, payload = _introduce(
            capsys, "--for-install", "install-a", "--for-device", "dev-acct-1"
        )
        assert code == 0, payload

    pending = _read_pairing(paths.store_root())["pending"]
    kinds = sorted(entry["kind"] for entry in pending.values())
    assert kinds == [KIND_DEVICE, KIND_PEER]
    assert {entry.get("for_install_id") for entry in pending.values()} == {
        None,
        "install-a",
    }


def test_a_fourth_requester_still_trips_the_cap_because_supersede_is_not_a_hole(
    capsys,
):
    """The clause that keeps the cap meaningful. What R-D5 bounds is how many
    DIFFERENT parties hold an outstanding invitation from this install — a
    stranger cannot mint past it by asking twice, and a second requester finds
    the map exactly as full as the first left it."""

    from hermes_cli.harness_support import ERROR_EXIT_CODES

    for _ in range(3):
        code, payload = _introduce(
            capsys, "--for-install", "install-a", "--for-device", "dev-acct-1"
        )
        assert code == 0, payload

    # Two rows stand (one per half). The second requester's peer half fits as
    # the third; its device half is the fourth and is refused.
    code = _dispatch(
        [
            "harness",
            "gateway",
            "introduce",
            "--for-install",
            "install-b",
            "--for-device",
            "dev-acct-2",
            "--json",
        ]
    )
    out = capsys.readouterr()

    assert code == ERROR_EXIT_CODES["pairing_codes_pending"]
    assert "outstanding" in (out.out + out.err)


def test_the_codes_appear_in_the_envelope_and_nowhere_else(capsys):
    """The codes discipline, unchanged and re-asserted for the new verb: the
    plaintext exists on stdout, once, for the caller who asked. ``pairing.json``
    holds a salted digest; no event, no row and no stderr line carries one — and
    that is the reason the ten-minute TTL is allowed to be short."""

    from agent_runtime.serve_gateway_auth import pairing_store_path

    code, payload = _introduce(
        capsys, "--for-install", "install-a", "--for-device", "dev-acct-1"
    )
    assert code == 0

    peer_code = payload["peer"]["peer_code"]
    device_code = payload["device"]["code"]
    stored = pairing_store_path(paths.store_root()).read_bytes().decode()

    assert peer_code not in stored
    assert device_code not in stored
    # …and the store did learn the two INTRODUCE labels, which are not secret:
    # the scoping check and the row's account join key both live on the pending
    # entry, and neither is a credential.
    assert "install-a" in stored
    assert "dev-acct-1" in stored


def test_the_credential_ttl_rides_the_envelope_so_a_launcher_can_show_when_it_lapses(
    capsys,
):
    """R-IP15 as amended, made visible: thirty days, named on the envelope, so a
    sheet can render "expires in N days" without the launcher hardcoding a
    number this repo owns."""

    from agent_runtime.serve_gateway_auth import CREDENTIAL_TTL_SECONDS_INTRODUCED

    _code, payload = _introduce(capsys, "--for-install", "install-a")

    assert payload["credential_ttl_seconds"] == CREDENTIAL_TTL_SECONDS_INTRODUCED
    assert CREDENTIAL_TTL_SECONDS_INTRODUCED == 30 * 86400


# ── gateway id ───────────────────────────────────────────────────────────────


@pytest.fixture
def named_install(capsys):
    """A root that has an identity, because ``gateway id`` refuses to mint one.

    The read-only contract is the reason: a probe that minted would leave a side
    effect on a root it was only asked about, and Stage 4's install picker runs
    this verb against roots it does not own. So the tests below name the install
    first, exactly as an operator would.
    """

    _dispatch(["harness", "gateway", "rename", "workstation", "--json"])
    capsys.readouterr()


def test_gateway_id_prints_candidate_endpoints_without_loopback_and_the_capabilities(
    capsys, named_install
):
    """Four facts a caller currently opens a socket to learn, answered by a cold
    CLI process — which is what lets S3's request loop feature-detect over the
    loopback argv lane against a serve that may not be listening at all."""

    from agent_runtime.gateway_capabilities import GATEWAY_CAPABILITIES

    code, payload = _run(capsys, "id")

    assert code == 0
    assert payload["capabilities"] == list(GATEWAY_CAPABILITIES)
    assert payload["endpoints"] == [{"host": "10.0.0.4", "port": 8765}]
    assert payload["endpoints_source"] == "config"
    assert payload["listener"] == {
        "host": "10.0.0.4",
        "port": 8765,
        "source": "config",
    }
    # The listener block is the live one MINUS nothing secret, and there is no
    # field for a key: a greeting carries the posture, never the credential.
    assert "verifier" not in json.dumps(payload)
    assert "PRIVATE KEY" not in json.dumps(payload)


def test_gateway_id_names_the_dial_host_and_keeps_the_listener_block_on_the_bind(
    capsys, monkeypatch, named_install
):
    """R-D4's source. The launcher paints ``Windows PC (192.168.1.203)`` and
    ``Listening on 192.168.1.203:8765 · all interfaces`` from this one key.

    It is a key and not a slice of ``endpoints`` because two surfaces computing
    "the first one" independently is how a sheet ends up naming an address no
    payload contains — and it is separate from ``listener`` because that block
    still reports the BIND. "What is this listener on" and "what should another
    machine dial" are different questions, and ``0.0.0.0`` is the honest answer
    to the first and never to the second.
    """

    from hermes_cli.harness_parts import gateway_commands, serve as serve_module

    monkeypatch.setattr(
        serve_module, "gateway_listen_config", lambda: ("0.0.0.0", 8765)
    )
    monkeypatch.setattr(
        gateway_commands,
        "_machine_addresses",
        lambda: ["192.168.1.203", "10.97.7.100"],
    )

    _code, payload = _run(capsys, "id")

    assert payload["dial_host"] == {"host": "192.168.1.203", "port": 8765}
    assert payload["dial_host"] == payload["endpoints"][0]
    assert payload["listener"]["host"] == "0.0.0.0"


def test_gateway_id_says_null_rather_than_a_bind_when_there_is_nothing_to_dial(
    capsys, monkeypatch, named_install
):
    """``null`` is a state the sheet renders (`no address published`), and it is
    the only honest answer for a wildcard that enumerates nothing. A verb that
    filled the hole with the bind would put ``0.0.0.0`` on a launcher label,
    which is the sentence the operator asked never to see again."""

    from hermes_cli.harness_parts import gateway_commands, serve as serve_module

    monkeypatch.setattr(
        serve_module, "gateway_listen_config", lambda: ("0.0.0.0", 8765)
    )
    monkeypatch.setattr(gateway_commands, "_machine_addresses", lambda: [])

    code, payload = _run(capsys, "id")

    # Still exit 0: ``gateway id`` is read-only by contract and Stage 4's picker
    # runs it against roots it does not own, so "nowhere to dial" is reported,
    # not refused. The PAYLOAD WRITERS are the ones that refuse.
    assert code == 0
    assert payload["dial_host"] is None
    assert payload["endpoints"] == []


def test_a_wildcard_bind_enumerates_interfaces_and_a_concrete_host_is_one_row(
    capsys, monkeypatch, named_install
):
    """The one behaviour S2 changed, and it is a widening rather than a fix.

    ``0.0.0.0`` used to answer ``[]`` with a correct argument attached: a bind
    is not an address. What was missing was the other half — an operator who
    binds a wildcard has not declined to be reachable, they have declined to
    CHOOSE — so this machine answers the question itself. The enumeration is
    stubbed here because a test that asserted on this box's real addresses would
    be a test that fails on a laptop that changed networks.
    """

    from hermes_cli.harness_parts import gateway_commands, serve as serve_module

    monkeypatch.setattr(serve_module, "gateway_listen_config", lambda: ("0.0.0.0", 8765))
    monkeypatch.setattr(
        gateway_commands, "_machine_addresses", lambda: ["10.0.0.4", "10.0.0.5"]
    )

    _code, payload = _run(capsys, "id")
    assert payload["endpoints"] == [
        {"host": "10.0.0.4", "port": 8765},
        {"host": "10.0.0.5", "port": 8765},
    ]

    monkeypatch.setattr(serve_module, "gateway_listen_config", lambda: ("10.0.0.9", 8765))
    _code, payload = _run(capsys, "id")
    assert payload["endpoints"] == [{"host": "10.0.0.9", "port": 8765}]


def test_no_listener_means_an_empty_endpoint_list_not_an_error(
    capsys, monkeypatch, named_install
):
    """``gateway id`` is read-only by contract and Stage 4's install picker runs
    it against roots it does not own. "Nowhere to dial" is a real state and an
    empty list is how it is said."""

    from hermes_cli.harness_parts import serve as serve_module

    monkeypatch.setattr(serve_module, "gateway_listen_config", lambda: (None, 0))

    code, payload = _run(capsys, "id")

    assert code == 0
    assert payload["endpoints"] == []
    assert payload["endpoints_source"] == "unknown"


def test_the_enumerator_drops_loopback_link_local_and_wildcards(monkeypatch):
    """The filter, tested directly rather than through a bind: every address it
    keeps is one another machine could actually dial, and every address it drops
    is one that would produce a dial failure indistinguishable from the peer
    being down."""

    import socket

    from hermes_cli.harness_parts import gateway_commands

    def _fake_getaddrinfo(host, port, family=0, *args, **kwargs):
        rows = {
            socket.AF_INET: [
                (socket.AF_INET, 0, 0, "", ("127.0.0.1", 0)),
                (socket.AF_INET, 0, 0, "", ("169.254.1.1", 0)),
                (socket.AF_INET, 0, 0, "", ("10.0.0.4", 0)),
            ],
            socket.AF_INET6: [
                (socket.AF_INET6, 0, 0, "", ("::1", 0, 0, 0)),
                (socket.AF_INET6, 0, 0, "", ("fe80::1%eth0", 0, 0, 0)),
                (socket.AF_INET6, 0, 0, "", ("2001:db8::5", 0, 0, 0)),
            ],
        }
        return rows.get(family, [])

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    # The default-route probe would answer with this box's real address, which
    # is exactly the non-determinism this test exists without. R-D8's routing
    # table read is silenced for the same reason: it shells out to this
    # machine's own ``route``/``ip``, and the filter is what is under test.
    monkeypatch.setattr(
        socket, "socket", lambda *a, **k: (_ for _ in ()).throw(OSError("no socket"))
    )
    monkeypatch.setattr(gateway_commands, "_default_route_address", lambda: None)

    assert gateway_commands._machine_addresses() == ["10.0.0.4", "2001:db8::5"]


def test_the_default_route_probe_asks_the_internet_and_its_answer_is_offered_first(
    monkeypatch,
):
    """R-D2, on the operator's own measured fixture set.

    This Windows PC has four addresses: a router-granted ``192.168.1.203`` on
    Wi-Fi, a ``10.97.7.100`` "Local Area Connection" with no gateway, a
    Hamachi-class ``25.3.92.221``, and a global v6. The old probe connected to
    ``10.255.255.255``, so the kernel named the 10.x interface, and the only
    address a machine on the same LAN can reach came out THIRD — which is to say
    not the one the payload writers hand out.

    The probe's far address is asserted here rather than left implicit: the
    whole correction is *which question the kernel was asked*, and a test that
    checked only the resulting order would still pass if a later edit pointed it
    back at a private range that happened to select the right adapter on the box
    the suite ran on.
    """

    import socket

    from hermes_cli.harness_parts import gateway_commands

    asked: list[tuple] = []

    class _Probe:
        def connect(self, address):
            asked.append(address)

        def getsockname(self):
            return ("192.168.1.203", 0)

        def close(self):
            pass

    def _fake_getaddrinfo(host, port, family=0, *args, **kwargs):
        rows = {
            socket.AF_INET: [
                (socket.AF_INET, 0, 0, "", ("10.97.7.100", 0)),
                (socket.AF_INET, 0, 0, "", ("25.3.92.221", 0)),
                (socket.AF_INET, 0, 0, "", ("192.168.1.203", 0)),
            ],
            socket.AF_INET6: [
                (socket.AF_INET6, 0, 0, "", ("2620:9b::1903:5cdd", 0, 0, 0)),
            ],
        }
        return rows.get(family, [])

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    monkeypatch.setattr(socket, "socket", lambda *a, **k: _Probe())
    # D1b: with the routing table silent this is still exactly D1's answer, so
    # the probe's own contract keeps being asserted on its own terms.
    monkeypatch.setattr(gateway_commands, "_default_route_address", lambda: None)

    assert gateway_commands._machine_addresses() == [
        "192.168.1.203",
        "10.97.7.100",
        "25.3.92.221",
        "2620:9b::1903:5cdd",
    ]
    assert asked == [("1.1.1.1", 53)]


def test_a_second_address_on_the_default_routes_own_subnet_outranks_other_private_ones(
    monkeypatch,
):
    """Rank 1 of R-D2, which the operator's own machine happens not to exercise.

    Two addresses on the segment the default route is on are both reachable by a
    peer on that segment; a private address on some other segment is a guess. So
    "same /24 as the first" sits between "the first" and "private somewhere" —
    and it is asserted rather than assumed because it is the only rank with
    arithmetic in it.
    """

    import socket

    from hermes_cli.harness_parts import gateway_commands

    class _Probe:
        def connect(self, address):
            pass

        def getsockname(self):
            return ("192.168.1.203", 0)

        def close(self):
            pass

    def _fake_getaddrinfo(host, port, family=0, *args, **kwargs):
        if family != socket.AF_INET:
            return []
        return [
            (socket.AF_INET, 0, 0, "", ("172.20.5.5", 0)),
            (socket.AF_INET, 0, 0, "", ("192.168.1.77", 0)),
            (socket.AF_INET, 0, 0, "", ("192.168.1.203", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    monkeypatch.setattr(socket, "socket", lambda *a, **k: _Probe())
    monkeypatch.setattr(gateway_commands, "_default_route_address", lambda: None)

    assert gateway_commands._machine_addresses() == [
        "192.168.1.203",
        "192.168.1.77",
        "172.20.5.5",
    ]


def test_the_cap_is_applied_after_the_order_so_the_lan_address_survives_it(
    monkeypatch,
):
    """The half of the S4 failure that a reordering alone would not have fixed.

    :data:`MAX_CANDIDATE_ENDPOINTS` is four, and it is the peer ROW's cap — a
    list longer than the row holds advertises addresses that vanish at the far
    end. Truncating a discovery-ordered list can therefore delete the one
    address that works; truncating a ranked one deletes the worst guesses.
    """

    import socket

    from hermes_cli.harness_parts import gateway_commands

    class _Probe:
        def connect(self, address):
            pass

        def getsockname(self):
            return ("192.168.1.203", 0)

        def close(self):
            pass

    def _fake_getaddrinfo(host, port, family=0, *args, **kwargs):
        if family != socket.AF_INET:
            return []
        # Five overlay-class addresses discovered BEFORE the LAN one, which is
        # how a machine with several virtual adapters enumerates.
        return [
            (socket.AF_INET, 0, 0, "", (f"25.3.92.{n}", 0)) for n in range(1, 6)
        ] + [(socket.AF_INET, 0, 0, "", ("192.168.1.203", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    monkeypatch.setattr(socket, "socket", lambda *a, **k: _Probe())
    monkeypatch.setattr(gateway_commands, "_default_route_address", lambda: None)

    offered = gateway_commands._machine_addresses()
    assert len(offered) == gateway_commands.MAX_CANDIDATE_ENDPOINTS
    assert offered[0] == "192.168.1.203"


# ── R-D8: the routing table names the first candidate ───────────────────────

#: The operator's own Windows PC, ``route print -4``, captured 2026-09-04 with
#: Private Internet Access connected — the machine and the moment that motivated
#: R-D8. Both halves of PIA's split default are here (``0.0.0.0/1`` as the
#: ``0.0.0.0 128.0.0.0`` row and ``128.0.0.0/1`` as the last row), and so is the one
#: true ``0.0.0.0 0.0.0.0`` row, on Wi-Fi. Kept verbatim rather than trimmed to
#: the rows under test: what the reader has to get right is telling these
#: particular rows APART, and a fixture with only the answer in it would prove
#: nothing about the netmask comparison.
_WINDOWS_ROUTE_PRINT = """\
===========================================================================
Active Routes:
Network Destination        Netmask          Gateway       Interface  Metric
          0.0.0.0          0.0.0.0      192.168.1.1    192.168.1.203     35
          0.0.0.0        128.0.0.0        10.97.0.1      10.97.7.100      3
       10.0.0.243  255.255.255.255        10.97.0.1      10.97.7.100      3
        10.97.0.0      255.255.0.0         On-link       10.97.7.100    259
        127.0.0.0        255.0.0.0         On-link         127.0.0.1    331
        128.0.0.0        128.0.0.0        10.97.0.1      10.97.7.100      3
===========================================================================
"""

#: ``route -n get default`` on macOS. NOT captured on this machine — there is no
#: Mac in this worktree — but the documented shape of the command, which is a
#: key/value block and is why the macOS arm needs a second command to turn the
#: interface NAME into an address.
_MACOS_ROUTE_GET_DEFAULT = """\
   route to: default
destination: default
       mask: default
    gateway: 192.168.1.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PREFIXES>
 recvpipe  sendpipe  ssthresh  rtt,msec    rttvar  hopcount      mtu     expire
       0         0         0         0         0         0      1500         0
"""

#: ``ifconfig en0`` on macOS, documented shape, not from this machine. The ``inet6``
#: line sits ABOVE the ``inet`` one, which is the reason the reader matches the
#: token exactly instead of looking for a substring.
_MACOS_IFCONFIG_EN0 = """\
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\toptions=400<CHANNEL_IO>
\tether f0:18:98:1a:2b:3c
\tinet6 fe80::14b3:2f1c:8a4d:9e02%en0 prefixlen 64 secured scopeid 0xc
\tinet 192.168.1.87 netmask 0xffffff00 broadcast 192.168.1.255
\tnd6 options=201<PERFORMNUD,DAD>
\tmedia: autoselect
\tstatus: active
"""

#: ``ip -4 route show default`` on Linux, documented shape, not from this machine.
#: The DHCP form carries ``src``, which is the address the kernel will stamp on
#: packets leaving by this route — the answer outright.
_LINUX_IP_ROUTE_WITH_SRC = (
    "default via 192.168.1.1 dev wlan0 proto dhcp src 192.168.1.42 metric 600\n"
)

#: The same command on a statically configured route, which carries no ``src`` and
#: therefore costs a second command.
_LINUX_IP_ROUTE_WITHOUT_SRC = "default via 10.0.0.1 dev eth0 proto static metric 100\n"

#: ``ip -4 -o addr show dev eth0``, documented shape, not from this machine. The
#: ``-o`` flag folds the continuation onto one line with a literal backslash,
#: which is why this is a raw string.
_LINUX_IP_ADDR_ETH0 = (
    r"2: eth0    inet 10.0.0.57/24 brd 10.0.0.255 scope global dynamic eth0\       "
    "valid_lft 84455sec preferred_lft 84455sec" + "\n"
)


def test_the_windows_table_names_the_lan_and_never_the_vpns_split_default():
    """R-D8 on the capture that produced it.

    With PIA up, EVERY datagram probe on this machine answers ``10.97.7.100``,
    because ``0.0.0.0/1`` + ``128.0.0.0/1`` cover the whole address space and beat
    ``0.0.0.0/0`` on specificity — that is D1's field-note correction, and it is
    why no probe destination could have fixed the ordering. The table still
    knows: exactly one row has netmask ``0.0.0.0``, and it is on Wi-Fi.
    """

    from hermes_cli.harness_parts.gateway_commands import (
        _windows_default_route_address,
    )

    assert _windows_default_route_address(_WINDOWS_ROUTE_PRINT) == "192.168.1.203"


def test_dropping_the_true_default_row_leaves_the_vpn_rows_unable_to_answer():
    """The negative half of the assertion above, and the one that would catch a
    reader rewritten to match on destination alone: the same capture WITHOUT its
    ``0.0.0.0 0.0.0.0`` row must answer ``None`` — never ``10.97.7.100`` — so that a
    VPN-only machine falls back to R-D2 rather than being told a tunnel address
    is its router-granted one."""

    from hermes_cli.harness_parts.gateway_commands import (
        _windows_default_route_address,
    )

    without_default = "\n".join(
        line
        for line in _WINDOWS_ROUTE_PRINT.splitlines()
        if line.split()[:2] != ["0.0.0.0", "0.0.0.0"]
    )

    assert "128.0.0.0" in without_default
    assert _windows_default_route_address(without_default) is None


def test_two_default_rows_are_decided_by_the_lowest_metric():
    """A machine with two NICs on one LAN has two ``0.0.0.0/0`` rows and the
    kernel picks by Metric. Picking the first printed instead would hand out the
    address of whichever adapter Windows happened to enumerate first."""

    from hermes_cli.harness_parts.gateway_commands import (
        _windows_default_route_address,
    )

    printed = (
        "Active Routes:\n"
        "Network Destination        Netmask          Gateway       Interface  Metric\n"
        "          0.0.0.0          0.0.0.0      192.168.1.1    192.168.1.9     45\n"
        "          0.0.0.0          0.0.0.0      192.168.1.1    192.168.1.203     35\n"
    )

    assert _windows_default_route_address(printed) == "192.168.1.203"


def test_a_persistent_route_row_is_not_an_active_one():
    """``route print`` prints a second table below the first, and a persistent
    default there names a GATEWAY where the active table names an interface
    address. Its row has four fields and the word ``Default`` where a metric goes,
    so the shape test rejects it — which is also what makes the reader safe on a
    Windows whose section headers are localised."""

    from hermes_cli.harness_parts.gateway_commands import (
        _windows_default_route_address,
    )

    printed = (
        "Persistent Routes:\n"
        "  Network Address          Netmask  Gateway Address  Metric\n"
        "          0.0.0.0          0.0.0.0     192.168.1.1  Default\n"
    )

    assert _windows_default_route_address(printed) is None


def test_the_macos_arm_reads_an_interface_name_and_then_its_first_inet():
    """Two commands, because ``route -n get default`` on macOS names ``en0`` and a
    peer cannot dial an interface name."""

    from hermes_cli.harness_parts.gateway_commands import (
        _first_inet_address,
        _macos_default_route_interface,
    )

    assert _macos_default_route_interface(_MACOS_ROUTE_GET_DEFAULT) == "en0"
    assert _first_inet_address(_MACOS_IFCONFIG_EN0) == "192.168.1.87"


def test_the_linux_arm_prefers_src_and_falls_back_to_the_devices_address():
    """``src`` is the kernel's own answer to "which of my addresses leaves by this
    route", so it is taken whole; ``dev`` is the fallback that costs a second
    command."""

    from hermes_cli.harness_parts.gateway_commands import (
        _first_inet_address,
        _linux_default_route,
    )

    assert _linux_default_route(_LINUX_IP_ROUTE_WITH_SRC) == (
        "192.168.1.42",
        "wlan0",
    )
    assert _linux_default_route(_LINUX_IP_ROUTE_WITHOUT_SRC) == (None, "eth0")
    assert _first_inet_address(_LINUX_IP_ADDR_ETH0) == "10.0.0.57"


@pytest.mark.parametrize(
    "platform, replies, expected, expected_argv",
    [
        (
            "win32",
            [_WINDOWS_ROUTE_PRINT],
            "192.168.1.203",
            [["route", "print", "-4"]],
        ),
        (
            "darwin",
            [_MACOS_ROUTE_GET_DEFAULT, _MACOS_IFCONFIG_EN0],
            "192.168.1.87",
            [["route", "-n", "get", "default"], ["ifconfig", "en0"]],
        ),
        (
            "linux",
            [_LINUX_IP_ROUTE_WITH_SRC],
            "192.168.1.42",
            [["ip", "-4", "route", "show", "default"]],
        ),
        (
            "linux",
            [_LINUX_IP_ROUTE_WITHOUT_SRC, _LINUX_IP_ADDR_ETH0],
            "10.0.0.57",
            [
                ["ip", "-4", "route", "show", "default"],
                ["ip", "-4", "-o", "addr", "show", "dev", "eth0"],
            ],
        ),
    ],
    ids=["windows", "macos", "linux-src", "linux-dev"],
)
def test_each_platform_asks_its_own_command_and_stops_as_soon_as_it_can(
    monkeypatch, platform, replies, expected, expected_argv
):
    """The dispatch, argv included. The argv is asserted because these are the
    strings that decide whether the answer is a routing table at all — a reader
    pointed at ``route print`` without ``-4`` gets a v6 table stapled below the v4
    one — and because the second command must not run when the first already
    answered."""

    import sys

    from hermes_cli.harness_parts import gateway_commands

    asked: list[list[str]] = []
    remaining = list(replies)

    def _fake_run(argv):
        asked.append(list(argv))
        return remaining.pop(0)

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(gateway_commands, "_run_route_command", _fake_run)

    assert gateway_commands._default_route_address() == expected
    assert asked == expected_argv


def test_a_command_this_machine_does_not_have_answers_none_rather_than_raising(
    monkeypatch,
):
    """Never raising is the contract ``_machine_addresses`` leans on: this runs
    inside ``gateway id``, and an exception here would turn a ranking preference
    into a CLI that cannot print its own identity. Proved against a real spawn
    of a binary that does not exist, because the failure being defended against
    is ``FileNotFoundError`` out of the OS and not a mocked one."""

    from hermes_cli.harness_parts import gateway_commands

    argv = ["hermes-no-such-routing-tool", "--version"]
    assert gateway_commands._run_route_command(argv) is None

    monkeypatch.setattr(gateway_commands, "_run_route_command", lambda argv: None)
    assert gateway_commands._default_route_address() is None


def _pia_getaddrinfo(family_module):
    """The operator's four addresses, in the order this PC enumerates them."""

    def _fake(host, port, family=0, *args, **kwargs):
        rows = {
            family_module.AF_INET: [
                (family_module.AF_INET, 0, 0, "", ("10.97.7.100", 0)),
                (family_module.AF_INET, 0, 0, "", ("25.3.92.221", 0)),
                (family_module.AF_INET, 0, 0, "", ("192.168.1.203", 0)),
            ],
            family_module.AF_INET6: [
                (family_module.AF_INET6, 0, 0, "", ("2620:9b::1903:5cdd", 0, 0, 0)),
            ],
        }
        return rows.get(family, [])

    return _fake


class _PiaProbe:
    """The datagram probe as it actually answers on this PC with PIA up: the
    tunnel, whatever public address it is pointed at."""

    def connect(self, address):
        pass

    def getsockname(self):
        return ("10.97.7.100", 0)

    def close(self):
        pass


def test_the_table_outranks_the_probe_so_the_lan_address_is_offered_first(
    monkeypatch,
):
    """D1b's whole point, on D1's own fixture set.

    D1 landed with ``dial_host 10.97.7.100`` on this machine — the probe's honest
    answer with a full tunnel up, and an address no machine on this LAN can
    reach. The table says Wi-Fi, so the LAN address moves to rank 0 and the
    tunnel keeps rank 1 rather than being dropped: it is still a real address,
    and on a run where PIA is the only network it is the only one there is.
    """

    import socket
    import sys

    from hermes_cli.harness_parts import gateway_commands

    monkeypatch.setattr(socket, "getaddrinfo", _pia_getaddrinfo(socket))
    monkeypatch.setattr(socket, "socket", lambda *a, **k: _PiaProbe())
    # Pinned to Windows so the suite reads the Windows capture on any machine
    # that runs it — the ranking is the subject here, the dispatch is proved
    # above.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        gateway_commands, "_run_route_command", lambda argv: _WINDOWS_ROUTE_PRINT
    )

    assert gateway_commands._machine_addresses() == [
        "192.168.1.203",
        "10.97.7.100",
        "25.3.92.221",
        "2620:9b::1903:5cdd",
    ]


def test_a_table_that_declines_to_answer_leaves_d1s_order_exactly_as_it_was(
    monkeypatch,
):
    """The fallback, asserted end to end rather than by stubbing the helper this
    stage added: every routing command fails, and the list is byte-for-byte the
    one D1 shipped — the probe's address first, the LAN second. R-D8 only ever
    promotes an address ahead of the probe's, so its silence must cost the
    pre-D1b ordering and nothing else."""

    import socket

    from hermes_cli.harness_parts import gateway_commands

    monkeypatch.setattr(socket, "getaddrinfo", _pia_getaddrinfo(socket))
    monkeypatch.setattr(socket, "socket", lambda *a, **k: _PiaProbe())
    monkeypatch.setattr(gateway_commands, "_run_route_command", lambda argv: None)

    assert gateway_commands._machine_addresses() == [
        "10.97.7.100",
        "192.168.1.203",
        "25.3.92.221",
        "2620:9b::1903:5cdd",
    ]


def test_the_endpoints_gateway_id_prints_are_the_endpoints_a_join_payload_advertises(
    capsys, named_install
):
    """One list, two surfaces. Two functions with two answers is how an install
    ends up advertising an address it does not print — which an operator debugs
    by comparing two commands that were never the same query."""

    from hermes_cli.harness_parts.gateway_commands import (
        _candidate_endpoints,
        _self_endpoints,
    )

    _code, payload = _run(capsys, "id")

    root = paths.store_root()
    assert payload["endpoints"] == _candidate_endpoints(root) == _self_endpoints(root)
