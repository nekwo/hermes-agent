"""``harness gateway pair`` / ``devices list`` / ``devices revoke`` (Stage 1, R3).

Every test drives the REAL argparse tree and dispatches through ``args.func``,
the rule ``test_gateway_verbs.py`` carries for the same reason: a handler nothing
routes to is a verb no operator can run, and registration is exactly the half a
handler test cannot see.

The store's own rules — TTL, pending cap, lockout, constant-time compare — are
tested at ``tests/agent_runtime/test_serve_gateway_auth.py``. What is tested HERE
is the part that suite cannot see: that argparse routes to these handlers, that
R3's two halves come out of ONE mint and agree, that a code appears on stdout and
nowhere else, and that a typed refusal becomes the right exit family instead of a
traceback.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent_runtime import paths


@pytest.fixture(autouse=True)
def hermetic_runtime_root(tmp_path, monkeypatch):
    """Pin the runtime root INSIDE this test's tmp dir, and prove it landed.

    These tests MINT CREDENTIALS. A resolution regression would pair a device
    against the operator's own install — the failure this fixture exists to make
    impossible rather than unlikely.
    """

    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    resolved = paths.store_root().resolve()
    assert resolved == root.resolve() or root.resolve() in resolved.parents, (
        f"store_root() resolved to {resolved}, OUTSIDE {root}: this test would "
        "pair a device against a runtime root nobody in this repo controls."
    )
    return root


@pytest.fixture(autouse=True)
def gateway_configured(monkeypatch):
    """An operator who has turned the lane on, so the endpoint is answerable."""

    from hermes_cli.harness_parts import serve as serve_module

    monkeypatch.setattr(
        serve_module, "gateway_listen_config", lambda: ("0.0.0.0", 8765)
    )


@pytest.fixture(autouse=True)
def enumerated_addresses(monkeypatch):
    """This box's interfaces, stubbed, because the bind above is a WILDCARD.

    The fixture bind is ``0.0.0.0``, and since R-D1 that means every payload
    host comes from :func:`_machine_addresses` rather than from the bind — so
    without this stub these assertions would be about whatever adapters the
    machine running the suite happens to have, which is a test that fails on a
    laptop that changed networks. The two values are the operator's own
    measured pair, in R-D2's order.
    """

    from hermes_cli.harness_parts import gateway_commands

    monkeypatch.setattr(
        gateway_commands,
        "_machine_addresses",
        lambda: ["192.168.1.203", "10.97.7.100"],
    )


def _dispatch(argv: list[str]) -> int:
    from hermes_cli import harness

    root = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(root.add_subparsers(dest="command"))
    args = root.parse_args(argv)
    return args.func(args)


def _run(capsys, *argv: str) -> tuple[int, dict]:
    code = _dispatch(["harness", "gateway", *argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out)


# ── pair: R3's two halves, from one mint ────────────────────────────────────


def test_pair_prints_a_typed_code_and_a_qr_payload_that_agree(capsys):
    """R3 as ruled — QR plus typed-code fallback. A code shown as text and a
    code shown as a QR that disagreed would be two ceremonies wearing one name,
    so the assertion is that they came from ONE mint."""

    code, payload = _run(capsys, "pair", "--name", "the phone")

    assert code == 0
    assert payload["kind"] == "gateway_pairing"
    assert len(payload["code"]) == 8
    assert payload["tier"] == "console"
    assert payload["device_name"] == "the phone"
    assert 0 < payload["expires_in_seconds"] <= 600

    scanned = json.loads(payload["qr_payload"])
    assert scanned == {
        # R-D1, and this used to read ``0.0.0.0``: the bind was written into the
        # payload verbatim, so a phone was handed an address it cannot dial (on
        # Windows ``WSAEADDRNOTAVAIL``; on macOS its own loopback). The host is
        # now the first candidate and the list is beside it.
        "host": "192.168.1.203",
        "port": 8765,
        "endpoints": [
            {"host": "192.168.1.203", "port": 8765},
            {"host": "10.97.7.100", "port": 8765},
        ],
        "install_id": payload["install_id"],
        "cert_fingerprint": payload["cert_fingerprint"],
        "code": payload["code"],
    }
    # …and ``host`` is exactly ``endpoints[0]``, which is the contract the
    # launcher's scanner reads: a client that predates the list keeps working.
    assert (scanned["host"], scanned["port"]) == (
        scanned["endpoints"][0]["host"],
        scanned["endpoints"][0]["port"],
    )
    # The typed fallback needs the endpoint beside the code, on one screen — and
    # this block still reports the BIND, because "what is this listener on" is a
    # different question from "what should a phone dial" and answering the first
    # with the second would hide a wildcard from the operator who chose it.
    assert payload["endpoint"] == {"host": "0.0.0.0", "port": 8765, "source": "config"}


def test_no_payload_a_pair_writes_can_carry_a_bind_address(capsys, monkeypatch):
    """R-D1 as a rule rather than as one asserted value.

    Every wildcard spelling, one at a time, and the QR payload is scanned for
    the literal in each. This is the test that would have caught S4's hardware
    attempt before it reached two machines.
    """

    from hermes_cli.harness_parts import serve as serve_module

    for bind in ("0.0.0.0", "::", "*"):
        monkeypatch.setattr(
            serve_module, "gateway_listen_config", lambda bind=bind: (bind, 8765)
        )
        _code, payload = _run(capsys, "pair")
        scanned = json.loads(payload["qr_payload"])
        assert scanned["host"] == "192.168.1.203", bind
        assert bind not in payload["qr_payload"], bind


def test_a_wildcard_bind_with_nothing_to_enumerate_refuses_rather_than_minting(
    capsys, monkeypatch
):
    """The other half of R-D1: when there is no dialable address, there is no
    payload. Family 7 (retryable — plugging in a cable makes the identical
    command succeed), and NOTHING is minted, because a code burned on a refusal
    is one of the three the operator is allowed."""

    from agent_runtime.serve_gateway_auth import pairing_store_path
    from hermes_cli.harness_parts import gateway_commands
    from hermes_cli.harness_parts.gateway_commands import NO_DIAL_HOST_SENTENCE
    from hermes_cli.harness_support import ERROR_EXIT_CODES

    monkeypatch.setattr(gateway_commands, "_machine_addresses", lambda: [])

    code = _dispatch(["harness", "gateway", "pair", "--json"])
    out = capsys.readouterr()

    assert code == ERROR_EXIT_CODES["runtime_unavailable"]
    assert NO_DIAL_HOST_SENTENCE in (out.out + out.err)
    assert not pairing_store_path(paths.store_root()).exists()


def test_the_qr_payload_is_a_string_so_two_renderers_cannot_disagree(capsys):
    """What a QR encodes is BYTES. Handing the operator the exact bytes removes
    the chance that two renderers serialise one object differently and only one
    of them scans."""

    _code, payload = _run(capsys, "pair")

    assert isinstance(payload["qr_payload"], str)
    assert json.loads(payload["qr_payload"])["code"] == payload["code"]


def test_pair_mints_the_identity_and_the_certificate_it_has_to_name(capsys):
    """An operator must be able to pair a root that has never booted — the
    precedent ``gateway rename`` set. A `pair` that could not produce a
    fingerprint would print a payload with a hole where the trust decision
    goes."""

    from agent_runtime.gateway_identity import install_record_path
    from agent_runtime.gateway_tls import certificate_path, read_certificate

    assert not certificate_path(paths.store_root()).exists()

    code, payload = _run(capsys, "pair")

    assert code == 0
    assert install_record_path(paths.store_root()).is_file()
    assert (
        payload["cert_fingerprint"] == read_certificate(paths.store_root()).fingerprint
    )


def test_the_code_appears_on_stdout_and_nowhere_else(capsys):
    """The code is a short-TTL CHANNEL and stdout is the channel: printed once,
    to the operator who asked, and never recoverable. A lost code is re-minted."""

    from agent_runtime.serve_gateway_auth import pairing_store_path

    _code, payload = _run(capsys, "pair")

    stored = pairing_store_path(paths.store_root()).read_bytes().decode("utf-8")
    assert payload["code"] not in stored


def test_a_read_tier_pairing_is_representable_from_the_command_line(capsys):
    """R11 shipped both spellings; a tier only reachable by editing a file is a
    tier nobody will use."""

    _code, payload = _run(capsys, "pair", "--tier", "read")

    assert payload["tier"] == "read"
    assert json.loads(payload["qr_payload"])["code"] == payload["code"]


def test_the_fourth_outstanding_code_is_refused_as_a_precondition(capsys):
    """A store refusal becomes an exit family, never a traceback — and the
    family is chosen on the operator's next MOVE. "You already have three" is
    "redeem one or wait", i.e. a precondition (6), not a fault."""

    for _ in range(3):
        assert _run(capsys, "pair")[0] == 0

    code = _dispatch(["harness", "gateway", "pair", "--json"])
    capsys.readouterr()

    assert code == 6


def test_an_unknown_tier_is_refused_by_argparse_rather_than_reaching_the_store(
    capsys,
):
    """The choices list is the first gate. It matters that this is a parse error:
    an operator who types `--tier admin` should be told at the door, not have a
    code minted at some other tier."""

    with pytest.raises(SystemExit):
        _dispatch(["harness", "gateway", "pair", "--tier", "admin", "--json"])


def test_pair_says_so_when_nothing_is_listening_for_the_code(capsys, monkeypatch):
    """A code minted against a lane nobody is listening on is still a valid code,
    and an operator who does not know that will blame the code."""

    from hermes_cli.harness_parts import serve as serve_module

    monkeypatch.setattr(serve_module, "gateway_listen_config", lambda: (None, 0))

    code, payload = _run(capsys, "pair")

    assert code == 0
    assert payload["endpoint"]["source"] == "unknown"
    assert "remote_gateway.listen is off" in payload["note"]
    assert payload["code"]


def test_a_live_listener_is_preferred_over_the_config(capsys):
    """The running serve's sidecar is the only source that can name an EPHEMERAL
    port — it exists nowhere else — so it has to win."""

    from agent_runtime.serve_socket import socket_owner_path
    from agent_runtime.serde import write_json_atomic

    write_json_atomic(
        socket_owner_path(paths.store_root()),
        {
            "pid": 1,
            "port": 111,
            "gateway": {
                "host": "192.168.1.40",
                "port": 54321,
                "cert_fingerprint": "unused",
            },
        },
    )

    _code, payload = _run(capsys, "pair")

    assert payload["endpoint"] == {
        "host": "192.168.1.40",
        "port": 54321,
        "source": "live",
    }
    assert json.loads(payload["qr_payload"])["port"] == 54321
    # The fingerprint comes from the CERTIFICATE, never from the sidecar: the
    # sidecar is a convenience, and a client pins what the file says.
    assert payload["cert_fingerprint"] != "unused"


# ── devices: list, and revoke ───────────────────────────────────────────────


def test_devices_list_is_empty_before_anything_is_paired(capsys):
    code, payload = _run(capsys, "devices", "list")

    assert code == 0
    assert payload["kind"] == "list"
    assert payload["item_kind"] == "gateway_device"
    assert payload["items"] == []


def test_devices_list_shows_a_paired_device_and_never_its_credential(capsys):
    from agent_runtime.serve_gateway_auth import (
        DeviceCredential,
        redeem_pairing_code,
    )

    _code, minted = _run(capsys, "pair", "--name", "the phone", "--tier", "read")
    credential = redeem_pairing_code(paths.store_root(), minted["code"])
    assert isinstance(credential, DeviceCredential)

    code, payload = _run(capsys, "devices", "list")

    assert code == 0
    (row,) = payload["items"]
    assert row["device_id"] == credential.device_id
    assert row["name"] == "the phone"
    assert row["tier"] == "read"
    assert row["revoked"] is False
    assert credential.token not in json.dumps(payload)
    assert "verifier" not in json.dumps(payload)


def test_revoke_refuses_the_device_and_keeps_the_row(capsys):
    from agent_runtime.serve_gateway_auth import (
        DeviceCredential,
        lookup_device,
        redeem_pairing_code,
    )

    _c, minted = _run(capsys, "pair")
    credential = redeem_pairing_code(paths.store_root(), minted["code"])
    assert isinstance(credential, DeviceCredential)

    code, payload = _run(capsys, "devices", "revoke", credential.device_id)

    assert code == 0
    assert payload["revoked"] is True
    # Stated in the ack rather than left for an operator to discover: a
    # revocation somebody believes is immediate, applied to a device that is
    # attached right now, is the gap between what they think they did and what
    # happened.
    assert payload["takes_effect"] == "next_handshake"
    assert lookup_device(paths.store_root(), credential.device_id).revoked is True
    # …and the row survives, so an audit can tell "thrown out" from "never
    # paired".
    assert _run(capsys, "devices", "list")[1]["count"] == 1


def test_the_device_half_reports_an_unwritable_store_as_its_own_reason(
    capsys, monkeypatch
):
    """R-D14 through the same helper the peer verbs use.

    ``pair`` and ``devices revoke`` write ``pairing.json`` and ``devices.json``
    with the writer whose narrowing wedged the peer store, so they carry the
    same latent fault and must not report it as ``runtime_unavailable`` — the
    family that means "try again in five seconds", and the one the launcher's
    fulfiller turns into a claim about the network."""

    from agent_runtime import serve_gateway_auth
    from agent_runtime.serve_gateway_auth import (
        StoreRefusal,
        device_store_path,
        pairing_store_path,
    )
    from hermes_cli.harness_support import ERROR_EXIT_CODES

    denied = StoreRefusal("permission_denied", "[WinError 5] Access is denied")
    monkeypatch.setattr(serve_gateway_auth, "mint_pairing_code", lambda *a, **k: denied)
    code, envelope = _run(capsys, "pair")

    assert code == ERROR_EXIT_CODES["store_unwritable"] == 1
    assert envelope["error"]["code"] == "store_unwritable"
    assert envelope["error"]["reason"] == "store_unwritable"
    assert str(pairing_store_path(paths.store_root())) in envelope["error"]["message"]

    monkeypatch.setattr(serve_gateway_auth, "revoke_device", lambda *a, **k: denied)
    code, envelope = _run(capsys, "devices", "revoke", "dev_x")

    assert code == 1
    assert envelope["error"]["code"] == "store_unwritable"
    assert str(device_store_path(paths.store_root())) in envelope["error"]["message"]


def test_revoking_an_unknown_device_is_not_found_rather_than_a_traceback(capsys):
    code = _dispatch(["harness", "gateway", "devices", "revoke", "dev_nope", "--json"])
    capsys.readouterr()

    assert code == 3


def test_a_dry_run_revoke_shows_the_row_it_would_cut_and_cuts_nothing(capsys):
    from agent_runtime.serve_gateway_auth import (
        DeviceCredential,
        lookup_device,
        redeem_pairing_code,
    )

    _c, minted = _run(capsys, "pair", "--name", "the phone")
    credential = redeem_pairing_code(paths.store_root(), minted["code"])
    assert isinstance(credential, DeviceCredential)

    code, payload = _run(
        capsys, "devices", "revoke", credential.device_id, "--dry-run"
    )

    assert code == 0
    assert payload["dry_run"] is True
    assert payload["name"] == "the phone"
    # What the WRITE would land…
    assert payload["revoked"] is True
    # …and what is actually on disk, which is the assertion that matters: the
    # kill-mutation here returns a perfectly plausible ack having revoked a
    # device the operator was only asking about.
    assert lookup_device(paths.store_root(), credential.device_id).revoked is False


def test_every_verb_stamps_which_root_answered(capsys):
    """The credentials are per STORE ROOT, so "which root answered" is not
    decoration — a pair run against the wrong root mints a code for a runtime
    the operator did not mean."""

    for argv in (("pair",), ("devices", "list")):
        _code, payload = _run(capsys, *argv)
        assert payload["resolution"]
