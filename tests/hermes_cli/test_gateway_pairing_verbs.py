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
        "host": "0.0.0.0",
        "port": 8765,
        "install_id": payload["install_id"],
        "cert_fingerprint": payload["cert_fingerprint"],
        "code": payload["code"],
    }
    # The typed fallback needs the endpoint beside the code, on one screen.
    assert payload["endpoint"] == {"host": "0.0.0.0", "port": 8765, "source": "config"}


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
