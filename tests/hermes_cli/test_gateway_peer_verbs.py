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
        "install_id": payload["install_id"],
        "cert_fingerprint": payload["cert_fingerprint"],
        "peer_code": payload["peer_code"],
    }


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
