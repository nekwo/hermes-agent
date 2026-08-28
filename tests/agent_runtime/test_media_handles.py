"""Gateway Stage 8: the handle namespace, before any transport is involved.

What is worth pinning here is the PREDICATE — what becomes a handle, what a
handle refuses, and the order the refusals are decided in. The wire half (a
paired device over real TLS fetching real bytes) is
``test_gateway_media_fetch_e2e.py``; a unit test cannot falsify that, and a wire
test cannot isolate this.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from agent_runtime import media_handles as mh


# ── the fixtures ────────────────────────────────────────────────────────────


def _png(size: int = 64) -> bytes:
    """Bytes that are not a real PNG and do not need to be — nothing in this
    lane decodes an image; the extension is what selects the media type."""

    return b"\x89PNG\r\n\x1a\n" + b"x" * max(0, size - 8)


def _log(directory, name: str, texts: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"ts": "2026-08-28T00:00:00Z", "kind": "log_opened"})]
    for text in texts:
        lines.append(
            json.dumps(
                {"ts": "2026-08-28T00:00:01Z", "kind": "message", "role": "agent", "text": text}
            )
        )
    (directory / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_memo():
    mh.reset_digest_memo()
    yield
    mh.reset_digest_memo()


# ── the declaration grammar ─────────────────────────────────────────────────


def test_a_declaration_is_a_whole_line_an_absolute_path_and_an_image():
    assert mh.parse_media_declaration(r"MEDIA:X:\shots\a.png") == r"X:\shots\a.png"
    assert mh.parse_media_declaration("MEDIA:/var/shots/a.png") == "/var/shots/a.png"
    # Trailing sentence punctuation and a whole-line backtick wrap, exactly as
    # the launcher's parser tolerates them.
    assert mh.parse_media_declaration(r"MEDIA:X:\shots\a.png.") == r"X:\shots\a.png"
    assert mh.parse_media_declaration(r"`MEDIA:X:\shots\a.png`") == r"X:\shots\a.png"
    assert mh.parse_media_declaration(r"MEDIA:'X:\a b\c.png'") == r"X:\a b\c.png"


def test_the_prose_that_TEACHES_the_protocol_never_becomes_a_handle():
    """The runtime's own prompt and half the skill docs contain this literal
    string. It is neither absolute nor an image, and both gates catch it."""

    assert mh.parse_media_declaration("MEDIA:<absolute path> line verbatim") is None
    assert mh.parse_media_declaration("MEDIA:` path could be produced.") is None


def test_a_non_image_declaration_is_not_in_the_namespace_at_all():
    """The decision the module docstring argues: the largest MEDIA-delivered
    artifact on this machine is a 1.1 GB MP4."""

    assert mh.parse_media_declaration(r"MEDIA:X:\clips\proof.mp4") is None
    assert mh.parse_media_declaration("MEDIA:/home/me/.ssh/id_rsa") is None
    assert mh.parse_media_declaration(r"MEDIA:X:\docs\report.pdf") is None


def test_a_relative_path_is_not_a_declaration_because_it_names_no_one_file():
    assert mh.parse_media_declaration("MEDIA:shots/a.png") is None
    assert mh.parse_media_declaration("MEDIA:../../../etc/a.png") is None


def test_lowercase_is_prose_the_way_both_parsers_already_agree():
    assert mh.parse_media_declaration(r"media:X:\shots\a.png") is None


def test_declarations_come_back_in_order_and_deduplicated():
    text = (
        "before\n"
        r"MEDIA:X:\shots\a.png" + "\n"
        "middle\n"
        r"MEDIA:X:\shots\b.png" + "\n"
        r"MEDIA:X:\shots\a.png" + "\n"
    )
    assert mh.media_declarations(text) == [r"X:\shots\a.png", r"X:\shots\b.png"]
    assert mh.media_declarations(None) == []
    assert mh.media_declarations("no sentinel here") == []


# ── the derivation ──────────────────────────────────────────────────────────


def test_a_declared_image_on_disk_becomes_a_handle_over_its_CONTENT(tmp_path):
    shot = tmp_path / "shots" / "proof.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(_png(1024))
    logs = tmp_path / "chat_live_logs"
    _log(logs, "session_a", [f"here it is\n\nMEDIA:{shot}"])

    scope = mh.build_media_scope(root=logs)

    assert len(scope.artifacts) == 1
    artifact = next(iter(scope.artifacts.values()))
    assert artifact.handle == "sha256:" + hashlib.sha256(_png(1024)).hexdigest()
    assert artifact.reference == str(shot)
    assert artifact.media_type == "image/png"
    assert artifact.size_bytes == 1024
    assert artifact.fetchable is True
    assert scope.logs_scanned == 1
    assert scope.declarations_seen == 1
    assert scope.truncated is False


def test_the_handle_moves_when_the_bytes_move_which_is_the_cache_contract(tmp_path):
    """Content addressing is only worth anything if a rewritten file cannot keep
    its old name — that is what lets a client cache a handle forever."""

    shot = tmp_path / "proof.png"
    shot.write_bytes(_png(64))
    logs = tmp_path / "chat_live_logs"
    _log(logs, "s", [f"MEDIA:{shot}"])

    first = next(iter(mh.build_media_scope(root=logs).artifacts))
    import os
    import time

    time.sleep(0.01)
    shot.write_bytes(_png(128))
    os.utime(shot, None)
    second = next(iter(mh.build_media_scope(root=logs).artifacts))

    assert first != second


def test_a_declared_file_this_disk_does_not_have_is_not_an_error_and_not_a_handle(
    tmp_path,
):
    """This machine's charsheet lane sweeps its drafts directory, so 11 of the
    17 real declarations in the live corpus point at files that are gone."""

    logs = tmp_path / "chat_live_logs"
    _log(logs, "s", [r"MEDIA:X:\gone\never-existed.png"])

    scope = mh.build_media_scope(root=logs)

    assert scope.artifacts == {}
    assert scope.declarations_seen == 1


def test_a_scope_with_no_mirror_is_empty_rather_than_raising(tmp_path):
    """The failure direction matters: a broken projection must mean "no
    pictures", never "any bytes you like"."""

    scope = mh.build_media_scope(root=tmp_path / "not-here")

    assert scope.artifacts == {}
    assert scope.logs_scanned == 0


def test_the_artifact_cap_truncates_and_SAYS_it_truncated(tmp_path):
    logs = tmp_path / "chat_live_logs"
    texts = []
    for index in range(5):
        shot = tmp_path / f"shot{index}.png"
        shot.write_bytes(_png(32 + index))
        texts.append(f"MEDIA:{shot}")
    _log(logs, "s", texts)

    scope = mh.build_media_scope(root=logs, max_artifacts=2)

    assert len(scope.artifacts) == 2
    assert scope.truncated is True


def test_only_the_tail_of_a_rotating_mirror_is_read_and_a_split_line_is_dropped(
    tmp_path,
):
    """A mirror rotates at 10 MB. A partial first line after the seek is not
    decodable NDJSON, and a decoder that repaired it would invent a message."""

    shot = tmp_path / "recent.png"
    shot.write_bytes(_png(48))
    logs = tmp_path / "chat_live_logs"
    logs.mkdir()
    old = json.dumps(
        {"kind": "message", "role": "agent", "text": "MEDIA:X:\\old\\gone.png", "pad": "z" * 4000}
    )
    new = json.dumps({"kind": "message", "role": "agent", "text": f"MEDIA:{shot}"})
    (logs / "s.jsonl").write_text(old + "\n" + new + "\n", encoding="utf-8")

    scope = mh.build_media_scope(root=logs, tail_bytes=len(new) + 8)

    assert len(scope.artifacts) == 1
    assert scope.declarations_seen == 1


# ── the boundary ────────────────────────────────────────────────────────────


def _one_artifact_scope(tmp_path, *, size: int = 256):
    shot = tmp_path / "proof.png"
    shot.write_bytes(_png(size))
    logs = tmp_path / "chat_live_logs"
    _log(logs, "s", [f"MEDIA:{shot}"])
    return mh.build_media_scope(root=logs), shot


def test_a_path_shaped_argument_is_refused_by_the_GRAMMAR_before_any_disk_touch(
    tmp_path, monkeypatch
):
    """The ordering IS the security property. If a path could reach a ``stat``
    at all there would be a traversal surface to argue about; there is not one,
    because nothing downstream of the grammar check ever runs."""

    scope, shot = _one_artifact_scope(tmp_path)

    def _explode(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("a refused handle reached the filesystem")

    monkeypatch.setattr("pathlib.Path.stat", _explode)
    monkeypatch.setattr("pathlib.Path.open", _explode)

    for hostile in (
        str(shot),
        "../../../etc/passwd",
        r"C:\Windows\win.ini",
        "sha256:" + "z" * 64,
        "sha256:" + "a" * 63,
        "",
        None,
        7,
    ):
        refusal = mh.resolve_handle(hostile, scope)
        assert isinstance(refusal, mh.MediaRefusal), hostile
        assert refusal.reason == mh.REASON_HANDLE_INVALID, hostile


def test_a_well_formed_handle_nobody_declared_is_unknown_and_says_nothing_more(
    tmp_path,
):
    """Guessed, deleted-since and never-existed are ONE answer on purpose: three
    would let a caller probe the install by watching which one it got."""

    scope, _ = _one_artifact_scope(tmp_path)

    refusal = mh.resolve_handle("sha256:" + "0" * 64, scope)

    assert isinstance(refusal, mh.MediaRefusal)
    assert refusal.refusal_data() == {"reason": mh.REASON_UNKNOWN_HANDLE}


def test_an_over_cap_artifact_is_indexed_refused_and_NAMES_the_cap(tmp_path):
    big = tmp_path / "huge.png"
    big.write_bytes(_png(mh.MAX_FETCH_BYTES + 1))
    logs = tmp_path / "chat_live_logs"
    _log(logs, "s", [f"MEDIA:{big}"])

    scope = mh.build_media_scope(root=logs)
    artifact = next(iter(scope.artifacts.values()))

    assert artifact.fetchable is False
    assert artifact.describe()["fetchable"] is False
    assert artifact.describe()["size_bytes"] == mh.MAX_FETCH_BYTES + 1

    refusal = mh.resolve_handle(artifact.handle, scope)

    assert isinstance(refusal, mh.MediaRefusal)
    assert refusal.refusal_data() == {
        "reason": mh.REASON_ARTIFACT_TOO_LARGE,
        "cap_bytes": mh.MAX_FETCH_BYTES,
        "size_bytes": mh.MAX_FETCH_BYTES + 1,
    }


def test_the_read_re_checks_both_the_size_and_the_digest(tmp_path):
    """A scope is a stat from a moment ago. A file that GREW must not be read
    whole into this process, and a file that CHANGED must not be served under a
    handle that promised other bytes — a poisoned content-addressed cache has no
    invalidation protocol that can ever reach it."""

    scope, shot = _one_artifact_scope(tmp_path)
    artifact = next(iter(scope.artifacts.values()))

    assert mh.read_artifact_bytes(artifact) == _png(256)

    shot.write_bytes(_png(300))
    changed = mh.read_artifact_bytes(artifact)
    assert isinstance(changed, mh.MediaRefusal)
    assert changed.reason == mh.REASON_UNKNOWN_HANDLE

    shot.write_bytes(_png(mh.MAX_FETCH_BYTES + 2))
    grew = mh.read_artifact_bytes(artifact)
    assert isinstance(grew, mh.MediaRefusal)
    assert grew.reason == mh.REASON_ARTIFACT_TOO_LARGE

    shot.unlink()
    gone = mh.read_artifact_bytes(artifact)
    assert isinstance(gone, mh.MediaRefusal)
    assert gone.reason == mh.REASON_ARTIFACT_UNREADABLE


# ── the constants this module borrowed rather than minted ───────────────────


def test_the_image_set_and_the_cap_have_not_drifted_from_the_gateways():
    """``agent_runtime`` does not import ``gateway`` on any hot path, so the
    parity is asserted HERE. A comment saying "copied from" is a comment; this
    fails when somebody edits one of the two."""

    api_server = pytest.importorskip("gateway.platforms.api_server")

    assert mh.IMAGE_EXTENSIONS == frozenset(api_server._MEDIA_IMG_EXT)
    assert mh.MEDIA_TYPES == api_server._MEDIA_MIME
    assert mh.MAX_FETCH_BYTES == api_server._MEDIA_DATA_URL_MAX_BYTES


def test_the_mint_is_spelled_once():
    assert mh.handle_for_bytes(b"abc") == "sha256:" + hashlib.sha256(b"abc").hexdigest()
    assert mh.HANDLE_RE.match(mh.handle_for_bytes(b""))
