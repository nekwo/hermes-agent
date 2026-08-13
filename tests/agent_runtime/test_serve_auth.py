"""``agent_runtime.serve_auth`` — the per-root secret minted before the socket.

The invariants under test are the ones a later transport slice will lean on:
the token is minted once and never rewritten, it is scoped to the root it was
asked about, ``verify`` fails CLOSED when there is nothing to compare against,
and the value itself never leaves the module.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime import serve_auth
from agent_runtime.serve_auth import (
    SERVE_AUTH_TOKEN_FILENAME,
    ensure_token,
    read_token,
    serve_auth_token_path,
    verify,
)


def test_first_boot_mints_a_256_bit_token(tmp_path):
    status = ensure_token(tmp_path)

    assert status.state == "minted"
    assert status.ok is True
    token = read_token(tmp_path)
    assert token is not None
    assert len(token) == 64  # 32 bytes, hex
    assert int(token, 16) >= 0  # it really is hex
    assert serve_auth_token_path(tmp_path).name == SERVE_AUTH_TOKEN_FILENAME


def test_a_second_boot_adopts_the_existing_token_byte_for_byte(tmp_path):
    """Rewriting would lock out every client already holding the value."""

    ensure_token(tmp_path)
    original = serve_auth_token_path(tmp_path).read_bytes()

    status = ensure_token(tmp_path)

    assert status.state == "present"
    assert serve_auth_token_path(tmp_path).read_bytes() == original


def test_each_runtime_root_gets_its_own_token(tmp_path):
    """QA lanes and worktree roots coexist; one root's token must not open another."""

    left = tmp_path / "root-a"
    right = tmp_path / "root-b"
    ensure_token(left)
    ensure_token(right)

    assert read_token(left) != read_token(right)
    assert verify(read_token(left), right) is False


def test_verify_accepts_the_token_and_rejects_everything_else(tmp_path):
    ensure_token(tmp_path)
    token = read_token(tmp_path)

    assert verify(token, tmp_path) is True
    assert verify(token[:-1] + ("0" if token[-1] != "0" else "1"), tmp_path) is False
    assert verify(token[:32], tmp_path) is False  # a prefix is not a match
    assert verify("", tmp_path) is False
    assert verify(None, tmp_path) is False


def test_verify_fails_closed_when_no_token_exists(tmp_path):
    """The failure mode that turns a hardening into a bypass is "no token
    configured, let everyone in". There is no such branch."""

    assert not serve_auth_token_path(tmp_path).exists()

    assert verify("anything", tmp_path) is False
    assert verify(None, tmp_path) is False
    assert read_token(tmp_path) is None


def test_the_comparison_goes_through_the_constant_time_primitive(tmp_path, monkeypatch):
    ensure_token(tmp_path)
    token = read_token(tmp_path)
    seen: list[tuple[str, str]] = []
    real = serve_auth.hmac.compare_digest

    def recording(left, right):
        seen.append((left, right))
        return real(left, right)

    monkeypatch.setattr(serve_auth.hmac, "compare_digest", recording)

    assert verify(token, tmp_path) is True
    assert seen == [(token, token)]


def test_the_status_payload_carries_the_posture_and_never_the_secret(tmp_path):
    status = ensure_token(tmp_path)
    token = read_token(tmp_path)

    payload = status.payload()

    assert payload == {"token_file": "minted"}
    assert token not in json.dumps({**payload, "path": status.path})


def test_an_unwritable_root_is_reported_as_a_typed_error_not_an_exception(tmp_path):
    """Boot must survive a root it cannot write; the state IS the report."""

    blocked = tmp_path / "not-a-directory"
    blocked.write_bytes(b"i am a file\n")

    status = ensure_token(blocked / "root")

    assert status.state.startswith("error:")
    assert status.ok is False
    assert verify("anything", blocked / "root") is False


def test_an_interrupted_mint_self_heals_instead_of_wedging_the_root(tmp_path):
    """An EMPTY token file is not a token — and it must not be permanent.

    A process killed between the ``O_EXCL`` create and the write leaves a
    zero-byte file. Under mint-iff-absent, NOTHING ever repaired it: every
    later boot saw a file that exists, reported ``error:empty_token_file``, and
    every ``verify`` failed closed against a token nobody could hold. Replacing
    it is safe precisely because it is empty — an empty file's value is held by
    no client, so this cannot lock anyone out the way rotating a real token
    would.

    This test replaces one that asserted the wedge (it pinned the report, which
    was right, and the permanence, which was not).
    """

    path = serve_auth_token_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")

    status = ensure_token(tmp_path)

    assert status.state == "minted"
    token = read_token(tmp_path)
    assert token
    # The root is USABLE again, which is the whole point of healing it.
    assert verify(token, tmp_path) is True
    # And a later boot is an ordinary no-op, not a second heal.
    assert ensure_token(tmp_path).state == "present"
    assert read_token(tmp_path) == token


def test_two_healers_converge_on_one_token(tmp_path):
    """Both healers replace; the second rename wins the file. Neither may
    report a token it does not hold, so both read the file back."""

    path = serve_auth_token_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")

    first = serve_auth._heal_empty(path)
    second = serve_auth._heal_empty(path)
    on_disk = read_token(tmp_path)

    assert second == on_disk
    # The first healer's value lost the file; what matters is that the file
    # holds exactly one token and it verifies.
    assert first != on_disk
    assert verify(on_disk, tmp_path) is True


def test_an_empty_token_file_we_cannot_replace_is_still_reported(tmp_path, monkeypatch):
    """The typed wedge state stays for the boot that cannot heal — a root we
    cannot write must say so rather than claim a secret."""

    path = serve_auth_token_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")

    def refuse(*args, **kwargs):
        raise PermissionError("read-only root")

    monkeypatch.setattr(serve_auth.os, "replace", refuse)

    status = ensure_token(tmp_path)

    assert status.state == "error:empty_token_file"
    assert read_token(tmp_path) is None
    # No temp files left behind by the failed heal.
    assert not [
        entry.name
        for entry in tmp_path.iterdir()
        if entry.name.startswith(f".{path.name}.")
    ]


def test_a_token_saved_with_crlf_still_verifies(tmp_path):
    """The repo's standing EOL trap, applied to a secret: an operator (or a
    Windows editor) rewriting the file must not silently invalidate it."""

    ensure_token(tmp_path)
    path = serve_auth_token_path(tmp_path)
    token = read_token(tmp_path)
    path.write_bytes(token.encode("utf-8") + b"\r\n")

    assert read_token(tmp_path) == token
    assert verify(token, tmp_path) is True


@pytest.mark.skipif(
    serve_auth.os.name == "nt", reason="POSIX mode bits are meaningless on Windows"
)
def test_posix_mints_the_token_0600(tmp_path):
    ensure_token(tmp_path)

    mode = serve_auth_token_path(tmp_path).stat().st_mode & 0o777

    assert mode == 0o600
