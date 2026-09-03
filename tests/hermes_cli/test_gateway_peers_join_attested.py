"""``peers join --expect-fingerprint`` — the attested pin (S2, R-S2-6).

The join has always been trust-on-first-use: it pins whatever fingerprint the
payload carried, which is exactly as strong as the channel the operator carried
the payload through. That is the right posture for the MANUAL ceremony and it is
unchanged. What S2 adds is the automated path's posture — the fingerprint comes
from the ACCOUNT (``DeviceOut.gateway_cert_fingerprint``, which the backend
holds because the far install told it while signed in), and a payload that
disagrees is not a payload from that install.

The load-bearing word in every test here is **before**. The refusal fires before
a socket exists, and that is asserted with a ``ServeSocketClient`` stand-in that
RAISES if it is constructed at all — an assertion about ordering that reads the
ordering rather than trusting a comment.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent_runtime import paths


@pytest.fixture(autouse=True)
def hermetic_runtime_root(tmp_path, monkeypatch):
    """Pin the runtime root INSIDE this test's tmp dir, and prove it landed."""

    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    resolved = paths.store_root().resolve()
    assert resolved == root.resolve() or root.resolve() in resolved.parents
    return root


@pytest.fixture(autouse=True)
def gateway_configured(monkeypatch):
    from hermes_cli.harness_parts import serve as serve_module

    monkeypatch.setattr(
        serve_module, "gateway_listen_config", lambda: ("10.0.0.4", 8765)
    )


@pytest.fixture
def no_dial_allowed(monkeypatch):
    """A ``ServeSocketClient`` that fails the test if anything constructs it.

    The whole claim of ``--expect-fingerprint`` is that a mismatch costs no
    connection: no TLS handshake for an impostor to time, no attempt burned on
    an answer that cannot change. A test that asserted only on the exit code
    would pass just as well if the refusal happened AFTER a completed dial,
    which is the version of this feature that does not work.
    """

    from hermes_cli.harness_parts import gateway_commands

    def _explode(*args, **kwargs):
        raise AssertionError(
            "peers join opened a connection: the fingerprint mismatch must be "
            "answered before any dial"
        )

    monkeypatch.setattr(gateway_commands, "ServeSocketClient", _explode, raising=False)
    # ``cmd_gateway_peers_join`` imports the class inside the function, so the
    # module attribute above is not the one it reads. Patch the source module
    # too — belt and braces, and the one that actually fires.
    from agent_runtime import serve_socket

    monkeypatch.setattr(serve_socket, "ServeSocketClient", _explode)


def _dispatch(argv: list[str]) -> int:
    from hermes_cli import harness

    root = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(root.add_subparsers(dest="command"))
    args = root.parse_args(argv)
    return args.func(args)


def _payload(fingerprint: str) -> str:
    return json.dumps(
        {
            "host": "10.0.0.9",
            "port": 9000,
            "install_id": "install-far",
            "cert_fingerprint": fingerprint,
            "peer_code": "ABCD2345",
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def test_join_with_expect_fingerprint_refuses_a_mismatch_before_any_dial(
    capsys, no_dial_allowed
):
    """``tls_fingerprint_mismatch`` — R-IP17's word, as the error's own reason —
    and family 2, because the argument was wrong and retrying it cannot help."""

    from hermes_cli.harness_support import ERROR_EXIT_CODES

    code = _dispatch(
        [
            "harness",
            "gateway",
            "peers",
            "join",
            _payload("ab" * 32),
            "--expect-fingerprint",
            "cd" * 32,
            "--json",
        ]
    )
    out = capsys.readouterr()
    text = out.out + out.err

    assert code == ERROR_EXIT_CODES["invalid_payload"]
    assert "tls_fingerprint_mismatch" in text
    # …and nothing was written. A row recorded on a payload we just refused
    # would be a credential pinned to a machine we declined to talk to.
    from agent_runtime.gateway_peers import list_peers

    assert list_peers(paths.store_root()) == []


def test_a_malformed_expect_fingerprint_is_refused_rather_than_compared(
    capsys, no_dial_allowed
):
    """A 64-hex value or nothing. Comparing an eight-character typo against a
    real fingerprint would answer ``mismatch`` for a reason that is not a
    mismatch, and an operator would go looking at the wrong machine."""

    from hermes_cli.harness_support import ERROR_EXIT_CODES

    code = _dispatch(
        [
            "harness",
            "gateway",
            "peers",
            "join",
            _payload("ab" * 32),
            "--expect-fingerprint",
            "abc123",
            "--json",
        ]
    )
    out = capsys.readouterr()

    assert code == ERROR_EXIT_CODES["invalid_payload"]
    assert "tls_fingerprint_invalid" in (out.out + out.err)


def test_join_without_the_flag_pins_the_payload_and_says_fingerprint_attested_false(
    capsys, monkeypatch
):
    """The manual posture, unchanged — and now ANNOUNCED. A weaker pin that
    nobody states is a weaker pin nobody notices, and an operator reading a
    stored edge should not have to remember which flags the join was run with.

    The handshake is stubbed at the client seam rather than run against a real
    listener: the happy path across two installs belongs to the two-roots
    acceptance, and a version of it that dialled this same root would pair an
    install with itself.
    """

    _stub_successful_join(monkeypatch)

    code = _dispatch(
        ["harness", "gateway", "peers", "join", _payload("ab" * 32), "--json"]
    )
    out = capsys.readouterr().out
    payload = json.loads(out[out.find("{") :])

    assert code == 0, out
    assert payload["fingerprint_attested"] is False
    assert payload["cert_fingerprint"] == "ab" * 32
    assert "correlation" not in payload


def test_join_prints_the_correlation_it_was_given_and_records_the_far_expiry(
    capsys, monkeypatch
):
    """Two facts one grant needs to stay one grant: the id every party writes
    (R-IP17), and the expiry the FAR side computed — read off the frame rather
    than derived here, so the two ends of the edge lapse together."""

    _stub_successful_join(monkeypatch, expires_at="2026-10-03T00:00:00+00:00")

    code = _dispatch(
        [
            "harness",
            "gateway",
            "peers",
            "join",
            _payload("ab" * 32),
            "--expect-fingerprint",
            "ab" * 32,
            "--correlation",
            "grant-1",
            "--json",
        ]
    )
    out = capsys.readouterr().out
    payload = json.loads(out[out.find("{") :])

    assert code == 0, out
    assert payload["fingerprint_attested"] is True
    assert payload["correlation"] == "grant-1"
    assert payload["expires_at"] == "2026-10-03T00:00:00+00:00"
    assert payload["expired"] is False


def test_a_far_install_that_names_no_expiry_records_a_row_that_never_expires(
    capsys, monkeypatch
):
    """Every edge the manual ceremony mints, and every far install that predates
    S2. ``None`` is the legal absent value, which is the whole reason this
    change needs no migration pass."""

    _stub_successful_join(monkeypatch, expires_at=None)

    _dispatch(["harness", "gateway", "peers", "join", _payload("ab" * 32), "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out[out.find("{") :])

    assert payload["expires_at"] is None
    assert payload["expired"] is False


def _stub_successful_join(monkeypatch, *, expires_at: str | None = None):
    """A ``ServeSocketClient`` that completes one peer join and nothing else."""

    from agent_runtime import serve_socket

    peered = {"peer_install_id": "install-me", "peer_secret": "f" * 64}
    if expires_at is not None:
        peered["expires_at"] = expires_at

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            self.closed = False

        def connect(self) -> None:
            return None

        def peer_join_hello(self, **kwargs):
            return {
                "event": "hello_ok",
                "install": {
                    "install_id": "install-far",
                    "display_name": "the far one",
                },
                "peered": peered,
            }

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(serve_socket, "ServeSocketClient", _Client)
