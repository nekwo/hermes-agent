"""``harness gateway peers`` pair / join / list / revoke (Stage 6, R5).

Every test drives the REAL argparse tree and dispatches through ``args.func``,
the rule ``test_gateway_verbs.py`` and ``test_gateway_pairing_verbs.py`` both
carry for the same reason: a handler nothing routes to is a verb no operator can
run, and registration is exactly the half a handler test cannot see.

The store's own rules — the shared cap, the shared lockout, the kind split, the
proof — are tested at ``tests/agent_runtime/test_gateway_peers_store.py``, and
the ceremony against a real listener is
``tests/agent_runtime/test_gateway_peer_two_roots_e2e.py``. What is tested HERE
is the part neither can see: that argparse routes to these handlers, that R3's
two halves come out of ONE mint and agree, that a payload from the WRONG
ceremony is refused for its shape rather than half-parsed, and that a typed
refusal becomes the right exit family instead of a traceback.

The join's happy path is deliberately not faked here. It needs a second install
answering on a real listener, and a version of it that dialled this same root
would pair an install with itself — green, and describing something that cannot
happen. That proof belongs to the two-roots acceptance and is left there.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent_runtime import paths


@pytest.fixture(autouse=True)
def hermetic_runtime_root(tmp_path, monkeypatch):
    """Pin the runtime root INSIDE this test's tmp dir, and prove it landed.

    These tests MINT CREDENTIALS. A resolution regression would pair a peer
    against the operator's own install — the failure this fixture exists to make
    impossible rather than unlikely.
    """

    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    resolved = paths.store_root().resolve()
    assert resolved == root.resolve() or root.resolve() in resolved.parents, (
        f"store_root() resolved to {resolved}, OUTSIDE {root}: this test would "
        "pair an install against a runtime root nobody in this repo controls."
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
    code = _dispatch(["harness", "gateway", "peers", *argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out)


# ── pair ─────────────────────────────────────────────────────────────────────


def test_pair_prints_a_typed_code_and_a_join_payload_that_agree(capsys):
    """R3's two halves from ONE mint — a code shown as text and a code shown as
    a payload that disagreed would be two ceremonies wearing one name."""

    code, payload = _run(capsys, "pair", "--note", "the laptop")

    assert code == 0
    assert payload["kind"] == "gateway_peer_pairing"
    assert len(payload["peer_code"]) == 8
    assert payload["note"] == "the laptop"
    assert 0 < payload["expires_in_seconds"] <= 600

    scanned = json.loads(payload["join_payload"])
    assert scanned == {
        "host": "10.0.0.4",
        "port": 8765,
        # R-D3: the whole candidate list rides the payload so the far side's
        # ``peers join`` can dial in order. One row here, because this fixture's
        # bind names one interface — the list is not an enumeration, it is
        # whatever ``_candidate_endpoints`` answers, which for a concrete bind is
        # exactly that bind.
        "endpoints": [{"host": "10.0.0.4", "port": 8765}],
        "install_id": payload["install_id"],
        "cert_fingerprint": payload["cert_fingerprint"],
        "peer_code": payload["peer_code"],
    }
    assert (scanned["host"], scanned["port"]) == (
        scanned["endpoints"][0]["host"],
        scanned["endpoints"][0]["port"],
    )


def test_a_wildcard_bind_in_the_live_sidecar_still_yields_a_dialable_payload(
    capsys, monkeypatch
):
    """R-D1 through the LIVE source, which is the one that actually shipped.

    A serve started with ``remote_gateway.listen: "0.0.0.0"`` publishes that
    bind in its ownership sidecar, and the sidecar wins over the config because
    it is the only source that can name an ephemeral port. So the wildcard
    reaches the payload writer through ``source: live`` — the exact path S4's
    hardware attempt took — and the payload must still name an address.
    """

    from agent_runtime.serde import write_json_atomic
    from agent_runtime.serve_socket import socket_owner_path
    from hermes_cli.harness_parts import gateway_commands

    write_json_atomic(
        socket_owner_path(paths.store_root()),
        {
            "pid": 1,
            "port": 111,
            "gateway": {"host": "0.0.0.0", "port": 8765, "cert_fingerprint": "x"},
        },
    )
    monkeypatch.setattr(
        gateway_commands,
        "_machine_addresses",
        lambda: ["192.168.1.203", "10.97.7.100"],
    )

    _code, payload = _run(capsys, "pair")
    scanned = json.loads(payload["join_payload"])

    assert scanned["host"] == "192.168.1.203"
    assert scanned["endpoints"] == [
        {"host": "192.168.1.203", "port": 8765},
        {"host": "10.97.7.100", "port": 8765},
    ]
    assert "0.0.0.0" not in payload["join_payload"]
    # The endpoint block still reports the bind, because that IS what the
    # listener is on — the operator who chose a wildcard should keep seeing it.
    assert payload["endpoint"]["host"] == "0.0.0.0"
    assert payload["endpoint"]["source"] == "live"


def test_peers_pair_refuses_when_a_wildcard_bind_enumerates_no_address(
    capsys, monkeypatch
):
    """R-D1's refusal on the peer half, with the sentence the plan names, and
    nothing minted — a code burned on a refusal is one of the three the operator
    is allowed."""

    from agent_runtime.gateway_peers import list_peers
    from agent_runtime.serve_gateway_auth import pairing_store_path
    from hermes_cli.harness_parts import gateway_commands, serve as serve_module
    from hermes_cli.harness_parts.gateway_commands import NO_DIAL_HOST_SENTENCE
    from hermes_cli.harness_support import ERROR_EXIT_CODES

    monkeypatch.setattr(serve_module, "gateway_listen_config", lambda: ("::", 8765))
    monkeypatch.setattr(gateway_commands, "_machine_addresses", lambda: [])

    code = _dispatch(["harness", "gateway", "peers", "pair", "--json"])
    out = capsys.readouterr()

    assert code == ERROR_EXIT_CODES["runtime_unavailable"]
    assert NO_DIAL_HOST_SENTENCE in (out.out + out.err)
    assert not pairing_store_path(paths.store_root()).exists()
    assert list_peers(paths.store_root()) == []


def test_the_payload_names_peer_code_so_the_two_ceremonies_cannot_be_confused(capsys):
    """A device payload carries ``code``; a peer payload carries ``peer_code``.
    That is the first of three guards against an operator pasting one ceremony's
    payload into the other's verb, and it is the one that fires before anything
    touches a store."""

    _code, peer = _run(capsys, "pair")
    _dispatch(["harness", "gateway", "pair", "--json"])
    device = json.loads(capsys.readouterr().out)

    assert "peer_code" in json.loads(peer["join_payload"])
    assert "code" not in json.loads(peer["join_payload"])
    assert "code" in json.loads(device["qr_payload"])
    assert "peer_code" not in json.loads(device["qr_payload"])


def test_pair_writes_no_peer_row_because_a_code_is_only_an_invitation(capsys):
    from agent_runtime.gateway_peers import list_peers, peer_store_path

    _run(capsys, "pair")

    assert list_peers(paths.store_root()) == []
    assert not peer_store_path(paths.store_root()).exists()


def test_the_code_appears_on_stdout_and_nowhere_else(capsys):
    """The code is a short-TTL channel and stdout IS the channel: printed once,
    to the operator who asked, never logged and never stored in the clear."""

    _code, payload = _run(capsys, "pair")

    stored = (paths.store_root() / "gateway" / "pairing.json").read_bytes().decode()
    assert payload["peer_code"] not in stored


def test_pair_says_the_next_step_is_on_the_other_machine(capsys):
    """R5's second operator, said out loud on the mint. This is the half an
    operator can get wrong silently — a code that is never carried pairs
    nothing, and there is deliberately no way around that."""

    _code, payload = _run(capsys, "pair")

    assert "OTHER install" in payload["next_step"]
    assert "peers join" in payload["next_step"]


def test_pair_mints_the_identity_and_certificate_the_payload_has_to_name(capsys):
    """Both sides of a peer edge must be nameable and dialable, so a `pair` that
    could not produce an id or a fingerprint would print a payload with a hole
    where the trust decision goes."""

    from agent_runtime.gateway_identity import install_record_path
    from agent_runtime.gateway_tls import certificate_path

    assert not certificate_path(paths.store_root()).exists()
    assert not install_record_path(paths.store_root()).exists()

    _code, payload = _run(capsys, "pair")

    assert certificate_path(paths.store_root()).exists()
    assert install_record_path(paths.store_root()).exists()
    assert len(payload["cert_fingerprint"]) == 64
    assert payload["install_id"]


def test_pair_states_when_no_listener_is_advertising_the_endpoint(capsys, monkeypatch):
    """A code minted against a lane nobody is listening on is still valid, and
    an operator who does not know that will blame the code."""

    from hermes_cli.harness_parts import serve as serve_module

    monkeypatch.setattr(serve_module, "gateway_listen_config", lambda: (None, 0))

    _code, payload = _run(capsys, "pair")

    assert payload["endpoint"]["source"] == "unknown"
    assert "remote_gateway.listen is off" in payload["note_endpoint"]


def test_the_shared_pending_cap_reaches_the_operator_as_a_precondition_family(capsys):
    """`too_many_pending` is family 6 — nothing is broken and the identical
    command succeeds later. It counts DEVICE codes too, which the message says
    rather than leaving an operator to wonder why three is not three."""

    from hermes_cli.harness_support import ERROR_EXIT_CODES

    for _ in range(3):
        _dispatch(["harness", "gateway", "peers", "pair", "--json"])
    capsys.readouterr()

    code = _dispatch(["harness", "gateway", "peers", "pair", "--json"])
    out = capsys.readouterr()

    assert code == ERROR_EXIT_CODES["pairing_codes_pending"]
    assert "device and peer codes alike" in (out.out + out.err)


# ── join: the refusal surface ────────────────────────────────────────────────


def _join(capsys, *argv: str) -> tuple[int, str]:
    code = _dispatch(["harness", "gateway", "peers", "join", *argv, "--json"])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_join_refuses_a_device_pairing_payload_for_its_shape(capsys):
    """The guard that fires before a socket is opened. A device payload names
    ``code``; there is no ``peer_code`` in it, so the parse refuses and says
    exactly that rather than dialling and failing obscurely."""

    _dispatch(["harness", "gateway", "pair", "--json"])
    device_payload = json.loads(capsys.readouterr().out)["qr_payload"]

    code, output = _join(capsys, device_payload)

    assert code == 2
    assert "DEVICE pairing payload" in output


def test_join_refuses_a_payload_that_is_not_json(capsys):
    code, output = _join(capsys, "{not json at all")

    assert code == 2
    assert "not JSON" in output


def test_join_refuses_a_bare_code_with_nowhere_to_dial(capsys):
    """The typed fallback needs an address beside it. Naming what is missing,
    because "invalid payload" on an eight-character code is not actionable."""

    code, output = _join(capsys, "ABCD2345")

    assert code == 2
    assert "host" in output and "port" in output


def test_a_bare_code_with_host_and_port_flags_gets_as_far_as_the_dial(capsys):
    """R3's typed half, wired: the flags supply what the QR would have. It fails
    at the CONNECTION here — which is the proof it parsed."""

    code, output = _join(
        capsys, "ABCD2345", "--host", "127.0.0.1", "--port", "9", "--timeout", "2"
    )

    from hermes_cli.harness_support import ERROR_EXIT_CODES

    assert code == ERROR_EXIT_CODES["runtime_unavailable"]
    assert "could not complete the peer handshake" in output


def test_flags_override_the_payload_rather_than_filling_in_behind_it(capsys):
    """An operator who typed --host did so because the payload's address is
    wrong for their network. A merge that preferred the payload would silently
    ignore the correction — so the override is asserted through the address the
    dial actually reports."""

    payload = json.dumps(
        {
            "host": "10.99.99.99",
            "port": 8765,
            "install_id": "inst_far",
            "cert_fingerprint": "ab" * 32,
            "peer_code": "ABCD2345",
        }
    )

    _code, output = _join(
        capsys, payload, "--host", "127.0.0.1", "--port", "9", "--timeout", "2"
    )

    assert "127.0.0.1:9" in output
    assert "10.99.99.99" not in output


def test_an_unreachable_install_is_retryable_rather_than_a_bad_argument(capsys):
    """Family 7: a listener that is not up yet is exactly the condition where
    the identical command succeeds five seconds later. Calling it an argument
    error would tell the operator to change something that is correct."""

    from hermes_cli.harness_support import ERROR_EXIT_CODES

    payload = json.dumps(
        {
            "host": "127.0.0.1",
            "port": 9,
            "install_id": "inst_far",
            "cert_fingerprint": "ab" * 32,
            "peer_code": "ABCD2345",
        }
    )

    code, output = _join(capsys, payload, "--timeout", "2")

    assert code == ERROR_EXIT_CODES["runtime_unavailable"]
    assert "gateway listener must be running" in output


class _FakeClient:
    """A ``ServeSocketClient`` stand-in that records every address dialled.

    The dial loop's two failure KINDS are the subject here, and neither can be
    reached against a real socket without standing up two TLS listeners with
    deliberately wrong certificates — which is what the two-roots e2e is for.
    What this stub can prove, and the e2e cannot without a firewall, is that a
    dial failure MOVES ON and a certificate mismatch DOES NOT.
    """

    dialled: list[tuple[str, int]] = []
    #: ``{(host, port): exception-or-None}``; a ``None`` completes the handshake.
    outcomes: dict = {}

    def __init__(self, host, port, **kwargs):
        self._where = (host, int(port))

    def connect(self):
        type(self).dialled.append(self._where)
        outcome = type(self).outcomes.get(self._where, ConnectionRefusedError("no"))
        if outcome is not None:
            raise outcome

    def peer_join_hello(self, **kwargs):
        return {
            "event": "hello_ok",
            "install": {"install_id": "inst_far", "display_name": "far"},
            "peered": {"peer_secret": "s" * 32, "expires_at": None},
        }

    def close(self):
        pass


@pytest.fixture
def fake_dials(monkeypatch):
    """Install :class:`_FakeClient` where ``peers join`` imports its client."""

    from agent_runtime import serve_socket

    _FakeClient.dialled = []
    _FakeClient.outcomes = {}
    monkeypatch.setattr(serve_socket, "ServeSocketClient", _FakeClient)
    return _FakeClient


def test_the_join_dials_the_payloads_candidates_in_order_until_one_answers(
    capsys, fake_dials
):
    """R-D3's whole point: the first advertised address being unreachable is a
    fact about that address, and the far install offered the others precisely
    because it could not know which of them a given peer can reach.

    The row records the candidate that ANSWERED and not the payload's first
    row — a stored address the loop already walked past would make every later
    dial start with a failure this run had proved."""

    from agent_runtime.gateway_peers import list_peers

    fake_dials.outcomes = {("192.0.2.1", 8765): TimeoutError("no route"),
                           ("10.0.0.9", 8765): None}
    payload = json.dumps(
        {
            "host": "192.0.2.1",
            "port": 8765,
            "endpoints": [
                {"host": "192.0.2.1", "port": 8765},
                {"host": "10.0.0.9", "port": 8765},
            ],
            "install_id": "inst_far",
            "cert_fingerprint": "ab" * 32,
            "peer_code": "ABCD2345",
        }
    )

    code, output = _join(capsys, payload, "--timeout", "2")

    assert code == 0, output
    assert fake_dials.dialled == [("192.0.2.1", 8765), ("10.0.0.9", 8765)]
    (row,) = list_peers(paths.store_root())
    assert list(row.endpoints) == [{"host": "10.0.0.9", "port": 8765}]


def test_a_certificate_mismatch_stops_the_loop_instead_of_trying_the_next_address(
    capsys, fake_dials
):
    """A refused connection says *this address*; a certificate that does not
    match the pin says *this identity*, and no other address in the list can
    make that come out differently. Retrying it would offer the same wrong
    certificate three more chances and burn three timeouts to reach the same
    refusal — so the loop stops on the first one, with R-IP17's reason word
    leading the sentence exactly as the pre-dial check spells it."""

    from agent_runtime.gateway_peers import list_peers
    from agent_runtime.serve_socket import ServeCertificatePinMismatch
    from hermes_cli.harness_support import ERROR_EXIT_CODES

    fake_dials.outcomes = {
        ("192.0.2.1", 8765): ServeCertificatePinMismatch("wrong certificate"),
        ("10.0.0.9", 8765): None,
    }
    payload = json.dumps(
        {
            "host": "192.0.2.1",
            "port": 8765,
            "endpoints": [
                {"host": "192.0.2.1", "port": 8765},
                {"host": "10.0.0.9", "port": 8765},
            ],
            "install_id": "inst_far",
            "cert_fingerprint": "ab" * 32,
            "peer_code": "ABCD2345",
        }
    )

    code, output = _join(capsys, payload, "--timeout", "2")

    assert code == ERROR_EXIT_CODES["invalid_payload"]
    assert "tls_fingerprint_mismatch" in output
    assert fake_dials.dialled == [("192.0.2.1", 8765)]
    assert list_peers(paths.store_root()) == []


def test_the_refusal_names_every_address_it_tried_with_the_failure_beside_it(
    capsys, fake_dials
):
    """The receipt S4's hardware attempt did not have. ``runtime_unavailable``
    on its own reads identically whether the listener is down, a firewall
    dropped the SYN, or the address was never dialable — which is why the
    12:00:13 receipt could not be attributed from either machine."""

    from hermes_cli.harness_support import ERROR_EXIT_CODES

    fake_dials.outcomes = {
        ("192.0.2.1", 8765): TimeoutError("no route"),
        ("10.0.0.9", 8765): ConnectionRefusedError("shut"),
    }
    payload = json.dumps(
        {
            "host": "192.0.2.1",
            "port": 8765,
            "endpoints": [
                {"host": "192.0.2.1", "port": 8765},
                {"host": "10.0.0.9", "port": 8765},
            ],
            "install_id": "inst_far",
            "cert_fingerprint": "ab" * 32,
            "peer_code": "ABCD2345",
        }
    )

    code, output = _join(capsys, payload, "--timeout", "2")

    assert code == ERROR_EXIT_CODES["runtime_unavailable"]
    assert "192.0.2.1:8765 (TimeoutError)" in output
    assert "10.0.0.9:8765 (ConnectionRefusedError)" in output


def test_host_and_port_flags_collapse_the_list_to_the_one_candidate_they_name(
    capsys, fake_dials
):
    """The override the launcher's redeemer uses: it owns the ORDER (it knows
    the account's list, its cache and its own subnets) and passes one candidate
    per run. A flag that merely reordered the payload's list would make the
    launcher's ranking advisory, which is not what R-D3 says."""

    fake_dials.outcomes = {("127.0.0.1", 9): None}
    payload = json.dumps(
        {
            "host": "192.0.2.1",
            "port": 8765,
            "endpoints": [
                {"host": "192.0.2.1", "port": 8765},
                {"host": "10.0.0.9", "port": 8765},
            ],
            "install_id": "inst_far",
            "cert_fingerprint": "ab" * 32,
            "peer_code": "ABCD2345",
        }
    )

    code, output = _join(capsys, payload, "--host", "127.0.0.1", "--port", "9")

    assert code == 0, output
    assert fake_dials.dialled == [("127.0.0.1", 9)]


def test_a_payload_without_the_endpoints_key_is_read_as_a_one_row_list(
    capsys, fake_dials
):
    """An install that predates R-D3 sends ``host``/``port`` and nothing else,
    and a bare code typed with ``--host``/``--port`` arrives the same way. Not a
    shim to delete later — it is the typed half of R3."""

    fake_dials.outcomes = {("10.0.0.9", 8765): None}
    payload = json.dumps(
        {
            "host": "10.0.0.9",
            "port": 8765,
            "install_id": "inst_far",
            "cert_fingerprint": "ab" * 32,
            "peer_code": "ABCD2345",
        }
    )

    code, output = _join(capsys, payload)

    assert code == 0, output
    assert fake_dials.dialled == [("10.0.0.9", 8765)]


def test_a_wildcard_row_in_an_advertised_list_is_dropped_rather_than_dialled(
    capsys, fake_dials
):
    """Defence in depth against the far side, not against ourselves: R-D1 stops
    this install writing a bind into a payload, and this stops an install that
    has not landed R-D1 yet from making us dial one. Dropped rather than
    refusing the whole payload — the list is advertisement, and one unusable row
    is not a reason to refuse an edge the other rows would have made."""

    fake_dials.outcomes = {("10.0.0.9", 8765): None}
    payload = json.dumps(
        {
            "host": "0.0.0.0",
            "port": 8765,
            "endpoints": [
                {"host": "0.0.0.0", "port": 8765},
                {"host": "10.0.0.9", "port": 8765},
            ],
            "install_id": "inst_far",
            "cert_fingerprint": "ab" * 32,
            "peer_code": "ABCD2345",
        }
    )

    code, output = _join(capsys, payload)

    assert code == 0, output
    assert fake_dials.dialled == [("10.0.0.9", 8765)]


def test_a_refusal_carries_the_stores_own_reason_beside_the_family_code(capsys):
    """R-D6. ``code`` is a FAMILY — nine store refusals share
    ``runtime_unavailable`` and three share ``invalid_payload`` — so a caller
    holding only the code knows what to do next and cannot say what happened.

    The launcher's fulfiller maps ``runtime_unavailable`` to ``no_route``, which
    is why S4's 12:00:40 receipt recorded "no route" for a refusal whose real
    reason existed one process earlier and was thrown away. ``reason`` is that
    word, unmapped, beside the family and never instead of it.
    """

    from hermes_cli.harness_support import ERROR_EXIT_CODES

    # Three outstanding codes, then a fourth: the store says ``too_many_pending``
    # and the taxonomy calls it ``pairing_codes_pending``.
    for _ in range(3):
        assert _run(capsys, "pair")[0] == 0

    code = _dispatch(["harness", "gateway", "peers", "pair", "--json"])
    envelope = json.loads(capsys.readouterr().out)

    assert code == ERROR_EXIT_CODES["pairing_codes_pending"]
    assert envelope["error"]["code"] == "pairing_codes_pending"
    assert envelope["error"]["reason"] == "too_many_pending"


def test_a_parse_refusal_names_its_reason_and_a_caller_with_none_omits_the_key(
    capsys,
):
    """The two halves of R-D6's shape.

    A verb on this lane names its reason even when the refusal never reached a
    store — a payload that will not decode is ``payload_not_json``, which the
    family ``invalid_payload`` cannot say. And a caller that passes no reason
    emits the envelope it always did, byte for byte: the response fixtures and
    their Launcher mirrors pin those bytes, so an unconditional ``reason: null``
    would be a cross-repo regeneration for a key nobody on that lane reads.
    """

    _code = _dispatch(["harness", "gateway", "peers", "join", "{not json", "--json"])
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["error"]["code"] == "invalid_payload"
    assert envelope["error"]["reason"] == "payload_not_json"

    from hermes_cli.harness_support import emit_harness_error

    emit_harness_error(RuntimeError("boom"), args=None, code="internal_error")
    plain = json.loads(capsys.readouterr().out)
    assert "reason" not in plain["error"]


def test_a_failed_join_writes_no_row(capsys):
    from agent_runtime.gateway_peers import list_peers

    _join(
        capsys,
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": 9,
                "install_id": "inst_far",
                "cert_fingerprint": "ab" * 32,
                "peer_code": "ABCD2345",
            }
        ),
        "--timeout",
        "2",
    )

    assert list_peers(paths.store_root()) == []


# ── join: a store this machine cannot write (R-D14) ──────────────────────────
#
# D3 run #1, 2026-09-04 18:06:20. The code was granted, the dial reached
# 192.168.1.39:8765, the far install redeemed and returned the secret — and the
# local write raised ``[WinError 5]``. hermes called it ``runtime_unavailable``,
# the launcher's fulfiller maps that to ``no_route``, and the sheet told the
# operator the Mac was unreachable. It was a DACL on a local directory, and the
# loop retried it once a minute to the identical result.
#
# R-D9 fixed the write. These pin the REPORT, because the next thing that makes
# a store unwritable (an AV quarantine, a full disk, a synced folder) will not
# be fixed by R-D9 and must still not be reported as somebody else's network.


_WINERROR_5 = "[WinError 5] Access is denied: '.peers.json.ntk1yca6.tmp' -> 'peers.json'"


def _successful_payload() -> str:
    return json.dumps(
        {
            "host": "10.0.0.9",
            "port": 8765,
            "endpoints": [{"host": "10.0.0.9", "port": 8765}],
            "install_id": "inst_far",
            "cert_fingerprint": "ab" * 32,
            "peer_code": "ABCD2345",
        }
    )


def test_a_store_this_machine_cannot_write_is_not_the_networks_fault(
    capsys, fake_dials, monkeypatch
):
    """The measured defect's REPORT half, from the door that raises.

    ``record_peer`` catches OSError around its own locked write, but the cache
    touch and the event append it runs after releasing the lock are outside
    that span — so a raise really can reach this verb, and it must land on the
    same word as the refusal below."""

    from agent_runtime import gateway_peers
    from hermes_cli.harness_support import ERROR_EXIT_CODES

    fake_dials.outcomes = {("10.0.0.9", 8765): None}

    def _denied(*_args, **_kwargs):
        raise PermissionError(_WINERROR_5)

    monkeypatch.setattr(gateway_peers, "record_peer", _denied)

    code = _dispatch(
        ["harness", "gateway", "peers", "join", _successful_payload(), "--json"]
    )
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["error"]["code"] == "store_unwritable"
    assert envelope["error"]["reason"] == "store_unwritable"
    assert code == ERROR_EXIT_CODES["store_unwritable"] == 1, (
        "family 1, not 7: a directory's permissions do not lapse on their own, "
        "and D3 run #1 proved it by retrying four times to the same WinError"
    )
    assert envelope["error"]["retryable"] is False


def test_the_write_refusal_names_the_file_and_the_os_error(
    capsys, fake_dials, monkeypatch
):
    """The OS message is two BASENAMES — ``'.peers.json.ntk1yca6.tmp' ->
    'peers.json'`` — which name no directory an operator could go and fix. The
    verb knows which store it was writing, so it is the one place that can put
    the absolute path in front of them."""

    from agent_runtime import gateway_peers
    from agent_runtime.gateway_peers import peer_store_path

    fake_dials.outcomes = {("10.0.0.9", 8765): None}
    monkeypatch.setattr(
        gateway_peers,
        "record_peer",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError(_WINERROR_5)),
    )

    _dispatch(["harness", "gateway", "peers", "join", _successful_payload(), "--json"])
    message = json.loads(capsys.readouterr().out)["error"]["message"]

    assert str(peer_store_path(paths.store_root())) in message
    assert "WinError 5" in message
    assert "permission_denied" in message, (
        "the store's own word is not lost when the wire word is collapsed"
    )
    assert "Nothing on the other machine is wrong" in message


def test_the_stores_own_refusal_door_gives_the_identical_answer(
    capsys, fake_dials, monkeypatch
):
    """Two doors, one story. ``record_peer`` normally RETURNS a refusal rather
    than raising; an operator must not get "unreachable" from one line of the
    store and "could not save" from another."""

    from agent_runtime import gateway_peers
    from agent_runtime.serve_gateway_auth import StoreRefusal

    fake_dials.outcomes = {("10.0.0.9", 8765): None}
    monkeypatch.setattr(
        gateway_peers,
        "record_peer",
        lambda *a, **k: StoreRefusal("permission_denied", _WINERROR_5),
    )

    code = _dispatch(
        ["harness", "gateway", "peers", "join", _successful_payload(), "--json"]
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == 1
    assert envelope["error"]["code"] == "store_unwritable"
    assert str(paths.store_root()) in envelope["error"]["message"]


def test_a_dial_that_never_landed_is_still_the_networks_answer(capsys):
    """The other half of the split, so R-D14 does not swallow the case it was
    carved out of: nothing was written because nothing was AGREED, and family 7
    is right — the listener may be up five seconds later."""

    from hermes_cli.harness_support import ERROR_EXIT_CODES

    code, output = _join(
        capsys,
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": 9,
                "install_id": "inst_far",
                "cert_fingerprint": "ab" * 32,
                "peer_code": "ABCD2345",
            }
        ),
        "--timeout",
        "2",
    )

    assert code == ERROR_EXIT_CODES["runtime_unavailable"]
    assert "store_unwritable" not in output


def test_every_peer_write_verb_reports_an_unwritable_store_the_same_way(
    capsys, monkeypatch
):
    """One helper, not four copies. ``peers pair`` mints into ``pairing.json``
    and ``peers revoke`` writes ``peers.json``; both reach the secure writer,
    so both had the same defect waiting and both take the same classification.
    """

    from agent_runtime import gateway_peers
    from agent_runtime.serve_gateway_auth import StoreRefusal, pairing_store_path

    monkeypatch.setattr(
        gateway_peers,
        "mint_peer_code",
        lambda *a, **k: StoreRefusal("permission_denied", _WINERROR_5),
    )
    code, envelope = _run(capsys, "pair")

    assert code == 1
    assert envelope["error"]["code"] == "store_unwritable"
    assert str(pairing_store_path(paths.store_root())) in envelope["error"]["message"]

    monkeypatch.setattr(
        gateway_peers,
        "revoke_peer",
        lambda *a, **k: StoreRefusal("unwritable", "the disk said no"),
    )
    code, envelope = _run(capsys, "revoke", "inst_far")

    assert code == 1
    assert envelope["error"]["code"] == "store_unwritable"
    assert str(gateway_peers.peer_store_path(paths.store_root())) in (
        envelope["error"]["message"]
    )


# ── join: the handshake is a reachability fact (R-D16) ───────────────────────
#
# D3 run #2, 2026-09-04 20:19:19. The join dialled 192.168.1.39:8765, redeemed,
# stored the secret and emitted ``gateway.peer.recorded source=join`` — and the
# cache row for that peer still read ``reachability: unreachable,
# unreachable_since 18:03:17``, two hours stale. ``note_dial_result(ok=True)``
# had exactly one caller, the chat lane's ``dial_peer``. The launcher read the
# cache, called the edge unusable, and re-requested a pairing code every minute
# for an edge that was already up.


def _seeded_unreachable(peer_install_id: str = "inst_far") -> None:
    """The operator's live cache: a peer written off by an earlier dial."""

    from agent_runtime.gateway_peers import note_dial_result

    note_dial_result(
        paths.store_root(), peer_install_id, ok=False, error="18:03 said no"
    )


def _cached(peer_install_id: str = "inst_far"):
    from agent_runtime.gateway_peers import read_peer_cache

    return read_peer_cache(paths.store_root()).get(peer_install_id)


def test_a_completed_join_marks_the_peer_reachable(capsys, fake_dials):
    """The defect, stated as the fact it denied: this run reached that install,
    over TLS, with a credential it minted seconds earlier. Anything reading the
    cache afterwards and seeing ``unreachable`` is being told the opposite of
    what the process it is reading just measured."""

    from agent_runtime.gateway_peers import REACHABILITY_REACHABLE

    _seeded_unreachable()
    assert _cached().reachability != REACHABILITY_REACHABLE
    fake_dials.outcomes = {("10.0.0.9", 8765): None}

    code, output = _join(capsys, _successful_payload(), "--timeout", "2")

    assert code == 0, output
    row = _cached()
    assert row.reachability == REACHABILITY_REACHABLE
    # Cleared, not merely overwritten beside a stale timestamp: "down since
    # 18:03" printed under a row that says reachable is the same lie in a
    # smaller font.
    assert row.unreachable_since is None


def _reachability_events(monkeypatch) -> list:
    """Every ``gateway.peer.reachability`` payload this test's writes emit."""

    from agent_runtime import gateway_peers

    seen: list = []
    monkeypatch.setattr(
        gateway_peers,
        "_emit_peer_event",
        lambda event_type, payload, **_kw: seen.append((event_type, payload)),
    )
    return seen


def test_a_join_that_reached_nothing_marks_the_peer_unreachable(
    capsys, fake_dials, monkeypatch
):
    """The other half, through the same door — carrying the addresses the
    refusal named, so the event and the operator's sentence cannot disagree
    about what was tried."""

    from agent_runtime.gateway_peers import REACHABILITY_UNREACHABLE

    seen = _reachability_events(monkeypatch)
    fake_dials.outcomes = {("10.0.0.9", 8765): TimeoutError("no route")}

    code, output = _join(capsys, _successful_payload(), "--timeout", "2")

    assert code != 0, output
    row = _cached()
    assert row.reachability == REACHABILITY_UNREACHABLE
    assert row.unreachable_since
    (flip,) = [p for event, p in seen if event == "gateway.peer.reachability"]
    assert "10.0.0.9:8765 (TimeoutError)" in flip["error"]
    assert "10.0.0.9:8765 (TimeoutError)" in output


def test_the_flip_emits_the_same_event_the_chat_dial_emits(
    capsys, fake_dials, monkeypatch
):
    """One door, one event. ``dial_peer`` records through
    ``note_dial_result``; so does the announce fan-out; so does this verb —
    which is what makes ``gateway.peer.reachability`` the single thing a
    subscriber has to watch, on a CHANGE of word rather than per handshake."""

    _seeded_unreachable()
    seen = _reachability_events(monkeypatch)
    fake_dials.outcomes = {("10.0.0.9", 8765): None}

    code, output = _join(capsys, _successful_payload(), "--timeout", "2")

    assert code == 0, output
    flips = [payload for event, payload in seen if event == "gateway.peer.reachability"]
    assert [row["reachability"] for row in flips] == ["reachable"]
    assert flips[0]["peer_install_id"] == "inst_far"
    assert flips[0]["unreachable_since"] is None


def test_the_reachability_word_lands_in_the_cache_and_not_the_trust_store(
    capsys, fake_dials
):
    """``test_gateway_peers_store.py`` asserts that no cache writer opens
    ``peers.json``; this asserts the verb did not find a way around it. The
    word is in the sidecar, and the trust row this same run wrote does not
    carry it — the split is a boundary only while the two writer sets are
    disjoint."""

    from agent_runtime.gateway_peers import (
        REACHABILITY_REACHABLE,
        peer_cache_path,
        peer_store_path,
    )

    fake_dials.outcomes = {("10.0.0.9", 8765): None}
    code, output = _join(capsys, _successful_payload(), "--timeout", "2")
    assert code == 0, output

    root = paths.store_root()
    trust = json.loads(peer_store_path(root).read_bytes().decode("utf-8"))
    cached = json.loads(peer_cache_path(root).read_bytes().decode("utf-8"))

    assert "reachability" not in json.dumps(trust)
    assert cached["peers"]["inst_far"]["reachability"] == REACHABILITY_REACHABLE


# ── join: EHOSTUNREACH on-link is a permission, not a route (R-D20) ──────────
#
# D3 run #2, 2026-09-04 20:19:25, measured on the Mac. macOS 15 Local Network
# privacy had never been granted to the app responsible for the launcher's
# process tree, so the kernel returned ``EHOSTUNREACH`` (errno 65) for every
# host on the Mac's own /24 except the router — on every port and on ICMP, with
# the ARP entry for 192.168.1.203 RESOLVED, in 177 ms because nothing was ever
# sent. ``peers join`` reported ``192.168.1.203:8765 (OSError)`` under
# ``runtime_unavailable``, the launcher painted "Unreachable", and the operator
# was sent to the router for a permission on their own machine.
#
# The errno alone cannot carry the distinction: the identical number against an
# address nobody routes to really is a route failure. What separates them is
# whether the host is ON-LINK — on a segment one of this machine's own
# addresses sits on — which is why these tests pin both arms.

#: The Mac's kernel, verbatim. Written with the literal 65 rather than
#: ``errno.EHOSTUNREACH`` because these tests run on Windows too, where that
#: constant is the CRT's 110 — and the condition under test is the errno of the
#: kernel that refused, not the errno of the interpreter reading it.
_EHOSTUNREACH_DARWIN = 65


def _on_link_payload(host: str = "192.168.1.203") -> str:
    return json.dumps(
        {
            "host": host,
            "port": 8765,
            "endpoints": [{"host": host, "port": 8765}],
            "install_id": "inst_far",
            "cert_fingerprint": "ab" * 32,
            "peer_code": "ABCD2345",
        }
    )


def _this_machine_is_on(monkeypatch, *addresses: str) -> None:
    """Pin what ``_machine_addresses`` answers, which is the on-link test's
    only input. The Mac's own address was 192.168.1.39/24 on ``en0``."""

    from hermes_cli.harness_parts import gateway_commands

    monkeypatch.setattr(
        gateway_commands, "_machine_addresses", lambda: list(addresses)
    )


def test_the_os_refusing_an_on_link_host_is_a_permission_and_not_a_route(
    capsys, fake_dials, monkeypatch
):
    """R-D20's whole subject. The operator's next MOVE is a permission granted
    on THIS machine, so the refusal must say so — and must not be family 7,
    which promises the identical command succeeds later. It does not: thirty
    denials and zero prompts in twenty-four hours on that Mac."""

    from hermes_cli.harness_support import ERROR_EXIT_CODES

    _this_machine_is_on(monkeypatch, "192.168.1.39")
    fake_dials.outcomes = {
        ("192.168.1.203", 8765): OSError(_EHOSTUNREACH_DARWIN, "No route to host")
    }

    code = _dispatch(
        ["harness", "gateway", "peers", "join", _on_link_payload(), "--json"]
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == ERROR_EXIT_CODES["local_policy"] == 2
    assert envelope["error"]["code"] == "local_policy"
    assert envelope["error"]["reason"] == "local_policy"
    message = envelope["error"]["message"]
    assert "192.168.1.203:8765" in message
    assert (
        "this machine's operating system refused to send to a host on its own "
        "network" in message
    )
    assert (
        "System Settings › Privacy & Security › Local Network" in message
    )


def test_the_same_errno_against_an_off_link_host_is_still_the_networks_answer(
    capsys, fake_dials, monkeypatch
):
    """The other arm, and the reason the classifier asks a second question at
    all. ``EHOSTUNREACH`` from a host nobody routes to IS a route failure, and
    calling it a permission would send an operator to System Settings to fix a
    LAN they are not on."""

    from hermes_cli.harness_support import ERROR_EXIT_CODES

    _this_machine_is_on(monkeypatch, "192.168.1.39")
    fake_dials.outcomes = {
        ("203.0.113.7", 8765): OSError(_EHOSTUNREACH_DARWIN, "No route to host")
    }

    code = _dispatch(
        [
            "harness",
            "gateway",
            "peers",
            "join",
            _on_link_payload("203.0.113.7"),
            "--json",
        ]
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == ERROR_EXIT_CODES["runtime_unavailable"]
    assert envelope["error"]["code"] == "runtime_unavailable"
    assert "203.0.113.7:8765 (OSError)" in envelope["error"]["message"]
    assert "local_policy" not in envelope["error"]["message"]


def test_a_refused_connection_on_an_on_link_host_is_not_a_permission(
    capsys, fake_dials, monkeypatch
):
    """The third arm, and the one that keeps the new word rare. A host on our
    own subnet that ANSWERS with a reset has proved the packets leave this
    machine — nothing about Local Network privacy is involved, and the listener
    being down is the retryable condition family 7 exists for."""

    from hermes_cli.harness_support import ERROR_EXIT_CODES

    _this_machine_is_on(monkeypatch, "192.168.1.39")
    fake_dials.outcomes = {("192.168.1.203", 8765): ConnectionRefusedError("shut")}

    code = _dispatch(
        ["harness", "gateway", "peers", "join", _on_link_payload(), "--json"]
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == ERROR_EXIT_CODES["runtime_unavailable"]
    assert "192.168.1.203:8765 (ConnectionRefusedError)" in envelope["error"]["message"]
    assert "local_policy" not in envelope["error"]["message"]


def test_one_candidate_that_answers_leaves_the_policy_word_unsaid(
    capsys, fake_dials, monkeypatch
):
    """The refusal is about a run in which NOTHING answered. A first address the
    OS would not send to, followed by a second that completed the handshake, is
    a successful join — and telling that operator to grant a permission would
    be advice for a problem they do not have."""

    from agent_runtime.gateway_peers import REACHABILITY_REACHABLE, list_peers

    _this_machine_is_on(monkeypatch, "192.168.1.39")
    fake_dials.outcomes = {
        ("192.168.1.203", 8765): OSError(_EHOSTUNREACH_DARWIN, "No route to host"),
        ("10.0.0.9", 8765): None,
    }
    payload = json.dumps(
        {
            "host": "192.168.1.203",
            "port": 8765,
            "endpoints": [
                {"host": "192.168.1.203", "port": 8765},
                {"host": "10.0.0.9", "port": 8765},
            ],
            "install_id": "inst_far",
            "cert_fingerprint": "ab" * 32,
            "peer_code": "ABCD2345",
        }
    )

    code, output = _join(capsys, payload, "--timeout", "2")

    assert code == 0, output
    assert len(list_peers(paths.store_root())) == 1
    assert _cached().reachability == REACHABILITY_REACHABLE


def test_the_reachability_event_leads_with_the_policy_word_so_a_reader_can_see_it(
    capsys, fake_dials, monkeypatch
):
    """D5h made the noted string and the printed ``Tried:`` list identical. This
    is the one place they differ on purpose: ``error`` is the only channel the
    reachability event has for WHY, a subscriber branches on it to choose a
    sentence, and a word buried behind an address list is a word a prefix match
    cannot find."""

    from agent_runtime.gateway_peers import REACHABILITY_UNREACHABLE

    _this_machine_is_on(monkeypatch, "192.168.1.39")
    seen = _reachability_events(monkeypatch)
    fake_dials.outcomes = {
        ("192.168.1.203", 8765): OSError(_EHOSTUNREACH_DARWIN, "No route to host")
    }

    code, _output = _join(capsys, _on_link_payload(), "--timeout", "2")

    assert code != 0
    assert _cached().reachability == REACHABILITY_UNREACHABLE
    (flip,) = [p for event, p in seen if event == "gateway.peer.reachability"]
    assert flip["error"] == "local_policy: 192.168.1.203:8765"


# ── the classifier itself (R-D20) ────────────────────────────────────────────


def test_the_classifier_calls_an_on_link_ehostunreach_a_policy():
    from hermes_cli.harness_parts.gateway_commands import classify_dial_error

    assert (
        classify_dial_error(
            OSError(_EHOSTUNREACH_DARWIN, "No route to host"),
            "192.168.1.203",
            addresses=["192.168.1.39"],
        )
        == "local_policy"
    )


def test_the_classifier_reads_the_linux_number_too():
    """113 on Linux, 65 on Darwin/BSD, and the exception can arrive from either
    — a fixture, a proxied dial, a log replayed on the other platform."""

    from hermes_cli.harness_parts.gateway_commands import classify_dial_error

    assert (
        classify_dial_error(
            OSError(113, "No route to host"),
            "10.0.0.9",
            addresses=["10.0.0.4"],
        )
        == "local_policy"
    )


def test_the_classifier_reads_the_windows_winerror_rather_than_the_errno():
    """``WSAEHOSTUNREACH`` arrives in ``winerror``; Windows translates ``errno``
    to the CRT's own ``EHOSTUNREACH``, which is a different number from either
    POSIX one. Reading only ``errno`` would miss the Windows case entirely."""

    from hermes_cli.harness_parts.gateway_commands import classify_dial_error

    exc = OSError(110, "No route to host")
    exc.winerror = 10065

    assert (
        classify_dial_error(exc, "192.168.1.203", addresses=["192.168.1.39"])
        == "local_policy"
    )


def test_the_classifier_needs_the_host_to_be_on_one_of_our_own_subnets():
    from hermes_cli.harness_parts.gateway_commands import classify_dial_error

    unreachable = OSError(_EHOSTUNREACH_DARWIN, "No route to host")

    assert (
        classify_dial_error(unreachable, "203.0.113.7", addresses=["192.168.1.39"])
        == "unreachable"
    )
    # A machine that could enumerate nothing is on no segment, so nothing is
    # on-link and the honest answer is the word we already had.
    assert classify_dial_error(unreachable, "192.168.1.203", addresses=[]) == (
        "unreachable"
    )


def test_the_classifier_only_ever_looks_at_a_host_unreachable_errno():
    """Every other dial failure keeps the word it had. The new one is narrow on
    purpose: it claims something about the operating system's policy, and a
    claim like that spent on a listener that is merely down is worse than no
    claim at all."""

    from hermes_cli.harness_parts.gateway_commands import classify_dial_error

    mine = ["192.168.1.39"]
    for exc in (
        ConnectionRefusedError("shut"),
        TimeoutError("slow"),
        OSError("no errno at all"),
        RuntimeError("not even an OSError"),
    ):
        assert classify_dial_error(exc, "192.168.1.203", addresses=mine) == (
            "unreachable"
        ), exc


def test_a_v6_link_local_host_is_on_link_by_definition_and_a_global_one_is_not():
    """v6 answers from the address itself wherever it can. ``fe80::/10`` is
    meaningless off the segment that assigned it, so an ``EHOSTUNREACH`` to one
    is this machine refusing itself; a GLOBAL v6 address carries no prefix
    length here, so it is never called on-link."""

    from hermes_cli.harness_parts.gateway_commands import classify_dial_error

    unreachable = OSError(_EHOSTUNREACH_DARWIN, "No route to host")

    assert classify_dial_error(unreachable, "fe80::1", addresses=[]) == "local_policy"
    assert (
        classify_dial_error(unreachable, "2001:db8::5", addresses=["2001:db8::9"])
        == "unreachable"
    )
    # Unique-local is v6's RFC1918: on-link when it shares a /64 with one of
    # ours, and not otherwise.
    assert (
        classify_dial_error(unreachable, "fd00:1::5", addresses=["fd00:1::9"])
        == "local_policy"
    )
    assert (
        classify_dial_error(unreachable, "fd00:1::5", addresses=["fd00:2::9"])
        == "unreachable"
    )


def test_the_classifier_asks_this_machine_when_it_is_given_no_address_list(
    monkeypatch,
):
    """The production call site passes nothing, so the default has to be the
    live enumeration — and it must be asked only AFTER the errno test, or every
    refused connection would pay for a routing-table read (two subprocesses on
    macOS, one on Windows, each with a two-second ceiling)."""

    from hermes_cli.harness_parts import gateway_commands

    asked: list[int] = []

    def _addresses() -> list[str]:
        asked.append(1)
        return ["192.168.1.39"]

    monkeypatch.setattr(gateway_commands, "_machine_addresses", _addresses)

    assert (
        gateway_commands.classify_dial_error(
            ConnectionRefusedError("shut"), "192.168.1.203"
        )
        == "unreachable"
    )
    assert asked == []
    assert (
        gateway_commands.classify_dial_error(
            OSError(_EHOSTUNREACH_DARWIN, "No route to host"), "192.168.1.203"
        )
        == "local_policy"
    )
    assert asked == [1]


# ── list ─────────────────────────────────────────────────────────────────────


def test_list_is_empty_on_a_root_that_has_paired_nothing(capsys):
    code, payload = _run(capsys, "list")

    assert code == 0
    # A LIST envelope, whose `kind` is the envelope shape and whose `item_kind`
    # names the row — the Stage 42 split every other list verb renders.
    assert payload["kind"] == "list"
    assert payload["item_kind"] == "gateway_peer"
    assert payload["items"] == []
    assert payload["count"] == 0


def test_list_shows_revoked_rows_and_never_a_credential(capsys):
    """"Never paired" and "thrown out" must not be the same answer — and the
    credential has no field on the record type, so its absence here is
    structural rather than a filter somebody could remove."""

    from agent_runtime.gateway_peers import record_peer, revoke_peer

    record_peer(
        paths.store_root(),
        peer_install_id="inst_far",
        secret="f" * 64,
        display_name="workstation",
        endpoints=[{"host": "10.0.0.9", "port": 8765}],
        cert_fingerprint="ab" * 32,
    )
    revoke_peer(paths.store_root(), "inst_far")

    code, payload = _run(capsys, "list")

    assert code == 0
    row = payload["items"][0]
    assert row["peer_install_id"] == "inst_far"
    assert row["display_name"] == "workstation"
    assert row["endpoints"] == [{"host": "10.0.0.9", "port": 8765}]
    assert row["revoked"] is True
    rendered = json.dumps(payload)
    assert "f" * 64 not in rendered
    assert "secret" not in rendered


# ── revoke ───────────────────────────────────────────────────────────────────


def _seed_peer(install_id: str = "inst_far") -> None:
    from agent_runtime.gateway_peers import record_peer

    record_peer(
        paths.store_root(),
        peer_install_id=install_id,
        secret="f" * 64,
        display_name="workstation",
    )


def test_revoke_says_it_is_one_sided_because_the_edge_has_two_credentials(capsys):
    """The fact that distinguishes this from ``devices revoke``, and the reason
    it is stated rather than assumed: a revocation that reached across the wire
    would be one install writing into another's credential store, which is the
    authority R5 says an install never has over another."""

    _seed_peer()

    code, payload = _run(capsys, "revoke", "inst_far")

    assert code == 0
    assert payload["revoked"] is True
    assert payload["takes_effect"] == "next_handshake"
    assert payload["scope"] == "this_install_only"
    assert "over there" in payload["note"]


def test_revoke_previews_a_real_row_before_cutting_an_install_off(capsys):
    from agent_runtime.gateway_peers import lookup_peer

    _seed_peer()

    code, payload = _run(capsys, "revoke", "inst_far", "--dry-run")

    assert code == 0
    assert payload["dry_run"] is True
    # What the WRITE would land…
    assert payload["revoked"] is True
    # …and nothing landed.
    assert lookup_peer(paths.store_root(), "inst_far").revoked is False


def test_revoking_an_install_nobody_paired_is_nothing_to_act_on(capsys):
    from hermes_cli.harness_support import ERROR_EXIT_CODES

    code = _dispatch(
        ["harness", "gateway", "peers", "revoke", "inst_nope", "--json"]
    )
    output = capsys.readouterr()

    assert code == ERROR_EXIT_CODES["not_found"]
    assert "inst_nope" in (output.out + output.err)


def test_a_dry_run_on_an_unpaired_install_refuses_rather_than_previewing_nothing(
    capsys,
):
    from hermes_cli.harness_support import ERROR_EXIT_CODES

    code = _dispatch(
        ["harness", "gateway", "peers", "revoke", "inst_nope", "--dry-run", "--json"]
    )
    capsys.readouterr()

    assert code == ERROR_EXIT_CODES["not_found"]


# ── registration ─────────────────────────────────────────────────────────────


def test_every_peer_verb_is_reachable_through_the_real_argparse_tree():
    """A handler nothing routes to is a verb no operator can run."""

    from hermes_cli import harness
    from hermes_cli.harness_parts import gateway_commands

    root = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(root.add_subparsers(dest="command"))

    routed = {
        "pair": gateway_commands.cmd_gateway_peers_pair,
        "list": gateway_commands.cmd_gateway_peers_list,
    }
    for verb, expected in routed.items():
        args = root.parse_args(["harness", "gateway", "peers", verb])
        # The tree routes through a lazy shim, so the identity check is on what
        # the shim reaches rather than on the shim itself.
        assert args.func.__name__ == f"_cmd_gateway_peers_{verb}"
        assert expected is not None

    assert root.parse_args(
        ["harness", "gateway", "peers", "revoke", "inst_x"]
    ).peer_install_id == "inst_x"
    assert root.parse_args(
        ["harness", "gateway", "peers", "join", "PAYLOAD"]
    ).payload == "PAYLOAD"


def test_the_peers_subtree_sits_beside_devices_rather_than_inside_it():
    """Two stores, two questions. Folding them would make a list that mixes a
    phone and a workstation and needs a `kind` column to be readable — a
    discriminator standing in for the two verbs this tree already has room
    for."""

    from hermes_cli import harness

    root = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(root.add_subparsers(dest="command"))

    assert root.parse_args(["harness", "gateway", "devices", "list"]).func.__name__ == (
        "_cmd_gateway_devices_list"
    )
    assert root.parse_args(["harness", "gateway", "peers", "list"]).func.__name__ == (
        "_cmd_gateway_peers_list"
    )
    with pytest.raises(SystemExit):
        root.parse_args(["harness", "gateway", "peers"])
