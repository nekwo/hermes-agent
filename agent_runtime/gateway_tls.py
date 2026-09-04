"""The self-signed per-install certificate the gateway listener wraps itself in.

Ruling B / R1, ruled 2026-08-27: **encrypt.** Plaintext-plus-HMAC was rejected —
the HMAC challenge-response already prevents impersonation, so the whole
question was confidentiality, and chat bodies and media in cleartext on shared
WiFi is not defensible. The mechanism is option (a): a self-signed per-install
certificate whose fingerprint travels in the pairing payload and is PINNED by
the client. No CA, no Keycloak, nothing reachable required.

What this file is and is not
----------------------------

It is the hermes half, and only the hermes half. The R1 survey's findings —
that the launcher owns a real ES256 key-custody stack and **zero** X.509 or
certificate machinery, that `asn1lib`/`x509` are transitive and imported by
nothing, that there is no `badCertificateCallback` precedent anywhere in that
repo — are findings about the CLIENT side. They are not this module's problem
to solve and they are not this module's excuse either: what they mean is that
the launcher will pin the fingerprint below through `badCertificateCallback` at
Stage 2, rather than teaching Dart to build a trust store.

Two consequences the survey drew that this file deliberately honours:

* **TLS is confidentiality here, never authentication.** The survey's bullet 3
  is that R1(a) bundles two lifts that are not the same lift, and it is right.
  A device is authenticated by :mod:`agent_runtime.serve_gateway_auth`'s HMAC
  proof over a fresh nonce, bound to the dialled port — which is what it was
  before this file existed and what it would still be if TLS were stripped out
  tomorrow. Nothing about the link's encryption is allowed to become an
  authorization fact; a peer that completes a TLS handshake has proven only
  that it can speak TLS.
* **One key, not two.** The survey's bullet 4 warns that a second keypair means
  two revocation stories and a pairing that can be half-valid. So the
  certificate below is the install's ONE gateway link identity: minted once,
  never rotated silently, and its fingerprint is the single value the pairing
  payload carries.

Why EC P-256 and not RSA
------------------------

Two reasons, one of them not about cryptography. Keygen for P-256 is
instantaneous where RSA-2048 costs a visible fraction of a second on every cold
boot that has to mint — and this runs inside serve's boot path, which the
latency baselines already watch. And P-256 is the curve the launcher's existing
`SoftwareDeviceKeyStore` already speaks (`ECDomainParameters('prime256v1')`), so
the day the survey's bullet 4 is actually taken — derive the cert FROM the
device key rather than minting a second identity — the curve is not also a
migration.

Validity, and the honest tension in it
--------------------------------------

Ten years. A certificate the client validates by FINGERPRINT is not made safer
by expiring: the pin is the trust decision, and an expiry is a second one that
can only ever fire as a lockout on an operator who has not been told to expect
it. But a long-lived key on disk is a real thing, so this is written down rather
than defaulted into: rotation, when it is wanted, is an explicit verb that
re-pairs every device, exactly as ``serve_auth`` says of its own token
("rotating it under them is a lockout, not a hardening"). Stage 1 does not ship
that verb; deleting the two files and restarting is the manual path, and it
invalidates every pin.

Contract, the same one every module in this lane states
-------------------------------------------------------

* **The root is an INPUT.** Never resolves a root, never reads ``HERMES_HOME``.
* **Mint iff absent.** An existing certificate is never rewritten — a paired
  device holds its fingerprint, and re-minting under it is a lockout.
* **Never raises.** A typed ``state`` (``loaded`` / ``minted`` /
  ``error:<reason>``) is the observability, on the "stated either way, never
  inferred from absence" rule the ``auth``, ``socket`` and ``install`` blocks on
  ``ready`` already follow. A runtime that cannot mint a certificate must still
  boot, run its loopback lane byte-identically, and SAY that the gateway lane
  did not come up.

File permissions
----------------

The private key is written ``0600`` where that means something and narrowed
with a best-effort ``icacls`` on Windows where it does not — the same treatment,
for the same reason, as ``serve_gateway_auth``'s device store, whose docstring
carries the full note. The certificate itself is public by construction: it
travels to every client that dials the listener.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import os
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .gateway_identity import gateway_dir
from .store_file_io import narrow_windows_acl as _narrow_windows_acl
from .store_file_io import os_error_reason as _os_reason
from .store_file_io import prepare_windows_replace as _prepare_windows_replace

__all__ = [
    "CERTIFICATE_FILENAME",
    "PRIVATE_KEY_FILENAME",
    "VALIDITY_DAYS",
    "GatewayCertificate",
    "certificate_path",
    "certificate_fingerprint",
    "ensure_certificate",
    "private_key_path",
    "read_certificate",
    "server_ssl_context",
]

#: Beside ``install.json`` / ``devices.json`` under ``<store_root>/gateway/``.
CERTIFICATE_FILENAME = "tls_cert.pem"
PRIVATE_KEY_FILENAME = "tls_key.pem"

#: See the module docstring — a fingerprint-pinned certificate is not made safer
#: by expiring, and an expiry it cannot renew is a lockout with a date on it.
VALIDITY_DAYS = 3650

STATE_LOADED = "loaded"
STATE_MINTED = "minted"


@dataclass(frozen=True, slots=True)
class GatewayCertificate:
    """The link identity — and what happened when we went to find it."""

    #: ``loaded`` | ``minted`` | ``error:<reason>``
    state: str
    #: Lowercase hex sha-256 of the certificate's DER bytes. ``None`` only in an
    #: error state; never invented.
    fingerprint: str | None
    cert_path: str
    key_path: str

    @property
    def ok(self) -> bool:
        return self.state in (STATE_LOADED, STATE_MINTED)

    def payload(self) -> dict[str, Any]:
        """What a greeting block or a pairing payload may carry.

        The fingerprint and nothing else. A path is an operator's business and
        means nothing on the machine a phone is holding; the private key is
        never named on any surface that leaves this process.
        """

        return {"state": self.state, "fingerprint": self.fingerprint}


def certificate_path(store_root: Path | str) -> Path:
    return gateway_dir(store_root) / CERTIFICATE_FILENAME


def private_key_path(store_root: Path | str) -> Path:
    return gateway_dir(store_root) / PRIVATE_KEY_FILENAME


def certificate_fingerprint(der: bytes) -> str:
    """``sha256`` of the DER bytes, lowercase hex.

    ONE derivation. Lowercase hex with no separators rather than the colon-
    delimited uppercase form OpenSSL prints, because this value's job is to ride
    a JSON pairing payload and be compared for equality by a Dart client — and a
    format with punctuation in it is a format two ends will eventually normalise
    differently. A client that wants to show it to a human can add the colons.
    """

    return hashlib.sha256(der).hexdigest()


def read_certificate(store_root: Path | str) -> GatewayCertificate:
    """Read the pair WITHOUT minting one. ``error:absent`` when there is none.

    The read-only half, for the pairing verb and for probes that must not leave
    a keypair on a root they were only asked about.
    """

    cert = certificate_path(store_root)
    key = private_key_path(store_root)
    try:
        if not cert.is_file() or not key.is_file():
            return _error(store_root, "absent")
        der = _der_from_pem(cert.read_bytes())
    except OSError:
        return _error(store_root, "unreadable")
    except Exception:
        return _error(store_root, "malformed_certificate")
    if der is None:
        return _error(store_root, "malformed_certificate")
    return GatewayCertificate(
        state=STATE_LOADED,
        fingerprint=certificate_fingerprint(der),
        cert_path=str(cert),
        key_path=str(key),
    )


def ensure_certificate(
    store_root: Path | str, *, common_name: str | None = None
) -> GatewayCertificate:
    """Load the certificate, minting one if absent. Never raises.

    ``common_name`` is cosmetic — it is what an operator inspecting the file
    sees, and the install's display name is the useful answer. Nothing verifies
    it: the client pins the fingerprint, and a self-signed subject line is a
    claim the certificate makes about itself.
    """

    existing = read_certificate(store_root)
    if existing.ok:
        return existing
    if not existing.state.endswith(":absent"):
        # A certificate that EXISTS and will not parse is never overwritten, on
        # the asymmetry ``gateway_identity._decode`` documents for the install
        # record: those bytes may be the ones a paired device pinned, and
        # re-minting to make a boot look tidy destroys the only copy of the join
        # value. An operator reads the typed reason and decides.
        return existing
    try:
        return _mint(store_root, common_name=common_name)
    except ImportError:
        # `cryptography` is a declared dependency (pyproject pins 48.0.1, pulled
        # in by PyJWT[crypto] anyway), so this arm is a broken install rather
        # than a missing feature — and it degrades to "the gateway lane does not
        # come up", never to "the gateway lane comes up in the clear".
        return _error(store_root, "cryptography_unavailable")
    except OSError as exc:
        return _error(store_root, _os_reason(exc))
    except Exception:  # pragma: no cover - defensive; boot must not fail here
        return _error(store_root, "mint_failed")


def server_ssl_context(store_root: Path | str) -> ssl.SSLContext:
    """A server-side context for the gateway listener. Raises if there is none.

    Deliberately RAISES where the rest of this module returns a typed state: its
    caller is the listener's construction, which already has to decide between
    "bound" and a typed ``error:<reason>`` outcome on the ready frame, and a
    context object has no honest empty value. There is no path where this
    returns something that would serve plaintext.

    ``TLS_SERVER`` with the interpreter's own minimum version rather than a
    hand-pinned one: pinning a floor here would mean this file has an opinion
    about protocol versions that it does not maintain, and Python's default has
    been TLS 1.2+ for every version this runtime supports.
    """

    identity = ensure_certificate(store_root)
    if not identity.ok:
        raise RuntimeError(f"gateway certificate unavailable: {identity.state}")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # No client certificates: the client proves itself with the device HMAC over
    # the application handshake, one layer up. Asking for one here would be a
    # second, weaker identity story that nothing checks.
    context.verify_mode = ssl.CERT_NONE
    context.load_cert_chain(certfile=identity.cert_path, keyfile=identity.key_path)
    return context


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _mint(store_root: Path | str, *, common_name: str | None) -> GatewayCertificate:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    name = (common_name or "").strip() or "hermes-gateway"
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name[:64])])
    now = _datetime.datetime.now(_datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        # Self-signed: issuer IS subject. There is no CA in this design and no
        # place to put one — the pairing payload's fingerprint is the whole
        # trust decision.
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # A minute of backdating, because two machines on a LAN routinely
        # disagree about the clock by more than zero and a certificate that is
        # not yet valid is the most confusing possible first impression.
        .not_valid_before(now - _datetime.timedelta(minutes=1))
        .not_valid_after(now + _datetime.timedelta(days=VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path = certificate_path(store_root)
    key_path = private_key_path(store_root)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    # The KEY first. If the two writes are interleaved the other way and the
    # process dies between them, the next boot finds a certificate with no key,
    # reads ``absent`` on the pair, and mints a second identity — under a device
    # that may have pinned the first. Key-then-cert means the incomplete state is
    # "a key with no certificate", which ``read_certificate`` also calls absent
    # and which nothing has ever pinned.
    _write_private(
        key_path,
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    _write_public(cert_path, certificate.public_bytes(serialization.Encoding.PEM))
    return GatewayCertificate(
        state=STATE_MINTED,
        fingerprint=certificate_fingerprint(
            certificate.public_bytes(serialization.Encoding.DER)
        ),
        cert_path=str(cert_path),
        key_path=str(key_path),
    )


def _der_from_pem(pem: bytes) -> bytes | None:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    certificate = x509.load_pem_x509_certificate(pem)
    return certificate.public_bytes(serialization.Encoding.DER)


def _write_private(path: Path, payload: bytes) -> None:
    """0600 where meaningful, narrowed where it is not, atomic either way.

    Carries R-D9 through the shared helper rather than a second copy of the
    reasoning: this writer narrows-then-replaces exactly as
    ``store_file_io.write_secure_json`` does, so it wedged a renewed key on the
    same volumes for the same reason and is repaired by the same call.
    """

    handle = tempfile.NamedTemporaryFile(
        "wb", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            try:
                os.chmod(handle.name, 0o600)
            except OSError:
                pass
        _prepare_windows_replace(Path(handle.name), path)
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    if os.name == "nt":
        _narrow_windows_acl(path)


def _write_public(path: Path, payload: bytes) -> None:
    handle = tempfile.NamedTemporaryFile(
        "wb", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


# ``_narrow_windows_acl`` / ``_os_reason`` bodies live in ``store_file_io``
# (one authority); the alias imports above keep the conventional names.


def _error(store_root: Path | str, reason: str) -> GatewayCertificate:
    return GatewayCertificate(
        state=f"error:{reason}",
        fingerprint=None,
        cert_path=str(certificate_path(store_root)),
        key_path=str(private_key_path(store_root)),
    )