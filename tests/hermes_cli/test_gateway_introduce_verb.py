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
    # is exactly the non-determinism this test exists without.
    monkeypatch.setattr(
        socket, "socket", lambda *a, **k: (_ for _ in ()).throw(OSError("no socket"))
    )

    assert gateway_commands._machine_addresses() == ["10.0.0.4", "2001:db8::5"]


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
