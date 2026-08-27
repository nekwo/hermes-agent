"""R1's hermes half: the self-signed per-install certificate and its fingerprint.

What is worth pinning is what a paired device will lean on and cannot re-derive:
that the certificate is minted ONCE and never moves under a device that pinned
it, that the fingerprint is stable across processes and equals what a client
computes off the DER bytes it is handed, and that every failure is a typed state
rather than an exception on the boot path.

The root is an INPUT, so these tests hand it a ``tmp_path`` and read what landed.
"""

from __future__ import annotations

import hashlib
import os
import socket
import ssl
import threading
from pathlib import Path

import pytest

from agent_runtime.gateway_tls import (
    VALIDITY_DAYS,
    certificate_fingerprint,
    certificate_path,
    ensure_certificate,
    private_key_path,
    read_certificate,
    server_ssl_context,
)


# ── mint, then never again ──────────────────────────────────────────────────


def test_a_fresh_root_mints_a_certificate_and_says_it_minted(tmp_path: Path):
    identity = ensure_certificate(tmp_path, common_name="workstation")

    assert identity.state == "minted"
    assert identity.ok is True
    assert len(identity.fingerprint) == 64
    assert certificate_path(tmp_path).is_file()
    assert private_key_path(tmp_path).is_file()
    assert certificate_path(tmp_path) == tmp_path / "gateway" / "tls_cert.pem"


def test_the_second_call_loads_rather_than_re_mints(tmp_path: Path):
    """Re-minting under a device that pinned the first fingerprint is a lockout,
    which is the argument ``serve_auth`` makes about its own token."""

    first = ensure_certificate(tmp_path)
    before = certificate_path(tmp_path).read_bytes()

    second = ensure_certificate(tmp_path)

    assert second.state == "loaded"
    assert second.fingerprint == first.fingerprint
    assert certificate_path(tmp_path).read_bytes() == before


def test_the_fingerprint_is_the_sha256_of_the_der_a_client_would_see(tmp_path: Path):
    """The pin the launcher will compare at Stage 2 through
    ``badCertificateCallback``: it holds DER bytes, not a path, so this value has
    to be derivable from the wire alone."""

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    identity = ensure_certificate(tmp_path)
    parsed = x509.load_pem_x509_certificate(certificate_path(tmp_path).read_bytes())
    der = parsed.public_bytes(serialization.Encoding.DER)

    assert identity.fingerprint == hashlib.sha256(der).hexdigest()
    assert identity.fingerprint == certificate_fingerprint(der)
    # Lowercase hex, no separators: this rides a JSON payload and is compared for
    # equality by a Dart client. A format with punctuation is one two ends
    # eventually normalise differently.
    assert identity.fingerprint == identity.fingerprint.lower()
    assert ":" not in identity.fingerprint


def test_the_read_only_half_never_mints(tmp_path: Path):
    """The pairing verb and Stage 4's picker run against roots they do not own.
    The kill-mutation — route ``read`` at ``ensure`` — returns a plausible
    fingerprint, so the assertion has to be on the FILESYSTEM."""

    identity = read_certificate(tmp_path)

    assert identity.state == "error:absent"
    assert identity.ok is False
    assert identity.fingerprint is None
    assert not certificate_path(tmp_path).exists()
    assert not private_key_path(tmp_path).exists()


def test_a_certificate_that_will_not_parse_is_typed_and_is_never_overwritten(
    tmp_path: Path,
):
    """The asymmetry ``gateway_identity._decode`` documents, on this file: those
    bytes may be the ones a paired device pinned."""

    ensure_certificate(tmp_path)
    certificate_path(tmp_path).write_bytes(b"-----BEGIN CERTIFICATE-----\nnope\n")

    identity = ensure_certificate(tmp_path)

    assert identity.state == "error:malformed_certificate"
    assert identity.fingerprint is None
    assert b"nope" in certificate_path(tmp_path).read_bytes()


def test_a_certificate_without_its_key_reads_absent_rather_than_half_valid(
    tmp_path: Path,
):
    """Mint order is key-then-cert precisely so the crash-in-between state is one
    nothing has ever pinned. This asserts the READ agrees."""

    ensure_certificate(tmp_path)
    private_key_path(tmp_path).unlink()

    assert read_certificate(tmp_path).state == "error:absent"


def test_an_unwritable_root_is_a_typed_state_rather_than_an_exception(tmp_path: Path):
    """A runtime that cannot mint must still boot and SAY the lane did not come
    up — the rule the ``auth`` / ``socket`` / ``install`` blocks already follow."""

    blocked = tmp_path / "not-a-directory"
    blocked.write_bytes(b"")

    identity = ensure_certificate(blocked)

    assert identity.state.startswith("error:")
    assert identity.ok is False


def test_the_certificate_is_self_signed_ec_p256_and_long_lived(tmp_path: Path):
    """EC because keygen runs on serve's boot path and P-256 is the curve the
    launcher's own key store already speaks; long-lived because a
    fingerprint-pinned certificate is not made safer by expiring."""

    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import ec

    ensure_certificate(tmp_path, common_name="workstation")
    parsed = x509.load_pem_x509_certificate(certificate_path(tmp_path).read_bytes())

    assert parsed.issuer == parsed.subject
    assert isinstance(parsed.public_key(), ec.EllipticCurvePublicKey)
    assert parsed.public_key().curve.name == "secp256r1"
    span = parsed.not_valid_after_utc - parsed.not_valid_before_utc
    assert span.days >= VALIDITY_DAYS
    assert "workstation" in parsed.subject.rfc4514_string()


@pytest.mark.skipif(os.name == "nt", reason="mode bits are not a permission on Windows")
def test_the_private_key_is_0600_where_that_means_something(tmp_path: Path):
    ensure_certificate(tmp_path)

    assert (private_key_path(tmp_path).stat().st_mode & 0o777) == 0o600


# ── the context, and a real TLS handshake over it ───────────────────────────


def test_the_server_context_refuses_to_exist_without_a_certificate(tmp_path: Path):
    """There is no path where this returns something that would serve plaintext.
    Its caller has to decide between "bound" and a typed error, and a context
    object has no honest empty value."""

    blocked = tmp_path / "not-a-directory"
    blocked.write_bytes(b"")

    with pytest.raises(RuntimeError, match="gateway certificate unavailable"):
        server_ssl_context(blocked)


def test_a_real_client_completes_a_tls_handshake_and_sees_the_pinned_fingerprint(
    tmp_path: Path,
):
    """The end-to-end claim R1 actually rests on: a client that trusts NOTHING —
    no CA, no hostname check, exactly the launcher's ``badCertificateCallback``
    posture — still gets the bytes whose digest is the fingerprint the pairing
    payload carried, and the link is encrypted.
    """

    identity = ensure_certificate(tmp_path)
    context = server_ssl_context(tmp_path)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    seen: list[bytes] = []

    def _serve() -> None:
        raw, _ = listener.accept()
        try:
            with context.wrap_socket(raw, server_side=True) as tls:
                seen.append(tls.recv(64))
                tls.sendall(b"pong\n")
        except OSError:  # pragma: no cover - the client closed early
            pass

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_context.check_hostname = False
        client_context.verify_mode = ssl.CERT_NONE
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
            with client_context.wrap_socket(raw) as tls:
                presented = tls.getpeercert(binary_form=True)
                tls.sendall(b"ping\n")
                assert tls.recv(64) == b"pong\n"
        assert certificate_fingerprint(presented) == identity.fingerprint
        assert seen == [b"ping\n"]
    finally:
        thread.join(10)
        listener.close()
