"""The workspace LEVEL family: verbatim bytes in, whole-document 3-way out.

Two properties this file exists to hold, because everything else about the
family is the canvas family's shape one door over:

1. **hermes reformats NOTHING.** The launcher's `SceneSerializer` writes the
   document indented one prop per line so a realm-sync diff is per-prop hunks
   rather than one hunk over one line; a hermes that re-serialized on the way to
   disk would silently undo that on every machine that pulled. So the round-trip
   assertions here are BYTE assertions, not "it parses back the same".
2. **Identity is the JSON VALUE, not the formatting.** Two publishers whose
   bytes differ only in whitespace must CONVERGE, not conflict — the merge is
   about who edited the environment, and nobody edited it by re-indenting it.

Those two pull in opposite directions, which is exactly why both are pinned.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime import paths
from agent_runtime.level_sync import (
    MAX_LEVEL_DOCUMENT_BYTES,
    REFUSAL_MISSING_VERSION,
    REFUSAL_NOT_AN_OBJECT,
    REFUSAL_TOO_LARGE,
    REFUSAL_UNREADABLE_DOCUMENT,
    LevelDocumentError,
    LevelStore,
    apply_level_pull,
    level_baseline_key,
    level_document_hash,
    published_relative_path,
    read_level_baseline,
    workspace_token_for_published_path,
    write_level_baseline,
)

WS = "ws_launcher"


def _document(*, prop_x: float = 1.0, indent: int | None = 2) -> bytes:
    """A level document shaped like the launcher's: version, scene, props."""

    body = {
        "version": 6,
        "scene": {
            "id": f"ws-{WS}",
            "props": [{"id": "a1b2c3d4", "x": prop_x, "y": 2.0, "asset": 0}],
        },
        "zones": [],
    }
    return json.dumps(body, indent=indent, sort_keys=True).encode("utf-8")


def _publish(tmp_path, **levels: bytes):
    subtree = tmp_path / "subtree"
    root = subtree / "store" / "levels"
    root.mkdir(parents=True, exist_ok=True)
    for token, raw in levels.items():
        (root / f"{token}.json").write_bytes(raw)
    return subtree


# ── the two facts hermes reads, and the three it refuses ────────────────────


def test_a_document_with_a_version_is_accepted_and_one_without_is_refused_by_name():
    from agent_runtime.level_sync import validate_level_document

    assert validate_level_document(_document())["version"] == 6

    with pytest.raises(LevelDocumentError) as missing:
        validate_level_document(b'{"scene": {}}')
    assert missing.value.code == REFUSAL_MISSING_VERSION

    with pytest.raises(LevelDocumentError) as shape:
        validate_level_document(b"[1, 2, 3]")
    assert shape.value.code == REFUSAL_NOT_AN_OBJECT

    with pytest.raises(LevelDocumentError) as garbage:
        validate_level_document(b"{not json")
    assert garbage.value.code == REFUSAL_UNREADABLE_DOCUMENT


def test_a_boolean_version_is_not_a_version():
    """``isinstance(True, int)`` is True in Python, so ``version: true`` walks
    through a naive numeric check — and a document whose version is a boolean is
    one no store on the other end can compare against its supported version."""

    from agent_runtime.level_sync import validate_level_document

    with pytest.raises(LevelDocumentError) as exc:
        validate_level_document(b'{"version": true}')
    assert exc.value.code == REFUSAL_MISSING_VERSION


def test_a_document_larger_than_the_stores_bound_is_refused_before_it_is_parsed():
    oversized = b'{"version": 6, "pad": "' + b"x" * MAX_LEVEL_DOCUMENT_BYTES + b'"}'

    from agent_runtime.level_sync import validate_level_document

    with pytest.raises(LevelDocumentError) as exc:
        validate_level_document(oversized)
    assert exc.value.code == REFUSAL_TOO_LARGE


# ── verbatim, which is the contract ─────────────────────────────────────────


def test_the_store_hands_back_the_exact_bytes_it_was_given(isolate_agent_runtime_root):
    """The launcher's indented, one-prop-per-line document survives the round
    trip byte for byte — including its LF endings on a host whose default text
    write would have made them CRLF."""

    raw = _document(indent=2)
    store = LevelStore()

    assert store.write(WS, raw)["changed"] is True
    assert store.read(WS) == raw
    assert b"\r\n" not in store.read(WS)
    assert paths.level_path(WS).read_bytes() == raw


def test_rewriting_the_identical_document_reports_no_change(isolate_agent_runtime_root):
    store = LevelStore()
    store.write(WS, _document())

    assert store.write(WS, _document())["changed"] is False


def test_a_workspace_with_no_level_reads_absent_rather_than_raising(isolate_agent_runtime_root):
    assert LevelStore().read("ws_never_authored") is None


# ── identity is the VALUE, not the formatting ───────────────────────────────


def test_two_indentations_of_one_document_hash_the_same(isolate_agent_runtime_root):
    assert level_document_hash(_document(indent=2)) == level_document_hash(
        _document(indent=None)
    )


def test_a_moved_prop_hashes_differently(isolate_agent_runtime_root):
    assert level_document_hash(_document(prop_x=1.0)) != level_document_hash(
        _document(prop_x=9.0)
    )


# ── the published path grammar, both directions ─────────────────────────────


def test_the_published_path_and_its_inverse_agree():
    rel = published_relative_path(WS)
    assert rel == f"store/levels/{WS}.json"
    assert workspace_token_for_published_path(rel) == WS


def test_a_nested_path_under_the_prefix_is_not_a_level_address():
    """Over-approximating here would let a subdirectory somebody committed by
    hand be adopted as a workspace's environment."""

    assert workspace_token_for_published_path("store/levels/a/b.json") is None
    assert workspace_token_for_published_path("store/levels/") is None
    assert workspace_token_for_published_path("store/office/ws.json") is None


# ── the pull's arms ─────────────────────────────────────────────────────────


def test_a_level_this_machine_never_had_is_adopted_whole(isolate_agent_runtime_root, tmp_path):
    raw = _document()
    subtree = _publish(tmp_path, **{WS: raw})

    summary = apply_level_pull("realm_a", subtree)

    assert summary.adopted == [WS]
    assert summary.source == "subtree"
    # The ADOPT writes the publisher's bytes, not a re-serialization of them.
    assert LevelStore().read(WS) == raw
    assert read_level_baseline("realm_a")[level_baseline_key(WS)] == level_document_hash(raw)


def test_an_unchanged_local_level_takes_the_realms_edit(isolate_agent_runtime_root, tmp_path):
    original = _document(prop_x=1.0)
    LevelStore().write(WS, original)
    write_level_baseline("realm_a", {level_baseline_key(WS): level_document_hash(original)})
    edited = _document(prop_x=9.0)

    summary = apply_level_pull("realm_a", _publish(tmp_path, **{WS: edited}))

    assert summary.adopted == [WS]
    assert LevelStore().read(WS) == edited


def test_a_locally_edited_level_the_realm_did_not_touch_is_kept(isolate_agent_runtime_root, tmp_path):
    published = _document(prop_x=1.0)
    write_level_baseline("realm_a", {level_baseline_key(WS): level_document_hash(published)})
    mine = _document(prop_x=5.0)
    LevelStore().write(WS, mine)

    summary = apply_level_pull("realm_a", _publish(tmp_path, **{WS: published}))

    assert summary.kept_local == [WS]
    assert summary.adopted == []
    assert LevelStore().read(WS) == mine


def test_both_sides_edited_is_a_loud_hold_with_the_remote_parked(
    isolate_agent_runtime_root, tmp_path
):
    base = _document(prop_x=1.0)
    write_level_baseline("realm_a", {level_baseline_key(WS): level_document_hash(base)})
    mine = _document(prop_x=5.0)
    theirs = _document(prop_x=9.0)
    LevelStore().write(WS, mine)

    summary = apply_level_pull("realm_a", _publish(tmp_path, **{WS: theirs}))

    assert summary.held == [WS]
    assert summary.adopted == []
    # The hold protects MY environment...
    assert LevelStore().read(WS) == mine
    # ...and leaves a copy of what it refused, or the operator's only exit is
    # "pull again and hope".
    sidecar = json.loads(paths.level_conflict_path("realm_a", WS).read_text(encoding="utf-8"))
    assert sidecar["remote_document"].encode("utf-8") == theirs
    assert sidecar["remote_hash"] == level_document_hash(theirs)


def test_two_publishers_who_only_disagree_about_whitespace_converge(
    isolate_agent_runtime_root, tmp_path
):
    """The positive control for the hold above: the SAME two-sided-divergence
    inputs, one variable changed — the two documents mean the same thing — and
    the answer must be adopt, not a conflict sidecar."""

    base = _document(prop_x=1.0)
    write_level_baseline("realm_a", {level_baseline_key(WS): level_document_hash(base)})
    LevelStore().write(WS, _document(prop_x=9.0, indent=2))

    summary = apply_level_pull(
        "realm_a", _publish(tmp_path, **{WS: _document(prop_x=9.0, indent=None)})
    )

    assert summary.held == []
    assert summary.adopted == [WS]
    assert not paths.level_conflict_path("realm_a", WS).exists()


def test_a_realm_that_stopped_publishing_a_level_never_deletes_it(
    isolate_agent_runtime_root, tmp_path
):
    mine = _document()
    LevelStore().write(WS, mine)
    write_level_baseline("realm_a", {level_baseline_key(WS): level_document_hash(mine)})

    summary = apply_level_pull("realm_a", _publish(tmp_path, other_ws=_document()))

    assert summary.upstream_absent == [WS]
    assert LevelStore().read(WS) == mine
    # The baseline is KEPT so a repaired publish still converges.
    assert level_baseline_key(WS) in read_level_baseline("realm_a")


def test_a_published_level_that_will_not_read_is_refused_not_treated_as_removed(
    isolate_agent_runtime_root, tmp_path
):
    """The two look identical from the pull's side — no usable remote body — and
    conflating them turns one member's corrupt file into an ``upstream_absent``
    row about an environment that is right there."""

    mine = _document()
    LevelStore().write(WS, mine)
    write_level_baseline("realm_a", {level_baseline_key(WS): level_document_hash(mine)})

    summary = apply_level_pull("realm_a", _publish(tmp_path, **{WS: b"{not json"}))

    assert [row["key"] for row in summary.refused] == [WS]
    assert summary.upstream_absent == []
    assert summary.adopted == []
    assert LevelStore().read(WS) == mine


def test_an_unreadable_local_level_is_refused_rather_than_adopted_over(
    isolate_agent_runtime_root, tmp_path
):
    """Absent drives the ADOPT arm, so folding a broken local read into it would
    overwrite an environment nobody on this machine could read — the one case
    where the operator most needs the file left alone."""

    paths.level_path(WS).parent.mkdir(parents=True, exist_ok=True)
    paths.level_path(WS).write_bytes(b"{half a document")

    summary = apply_level_pull("realm_a", _publish(tmp_path, **{WS: _document()}))

    assert [row["code"] for row in summary.refused] == ["unreadable_local_level"]
    assert summary.adopted == []
    assert paths.level_path(WS).read_bytes() == b"{half a document"


def test_a_subtree_with_no_level_directory_touches_nothing_and_says_so(
    isolate_agent_runtime_root, tmp_path
):
    """The version-skew arm: an older publisher carries no ``store/levels/`` at
    all, and ``source: null`` is how the launcher tells that apart from a realm
    that published an empty one. Never a removal."""

    mine = _document()
    LevelStore().write(WS, mine)
    write_level_baseline("realm_a", {level_baseline_key(WS): level_document_hash(mine)})
    empty = tmp_path / "subtree"
    empty.mkdir()

    summary = apply_level_pull("realm_a", empty)

    assert summary.source is None
    assert summary.as_dict()["source"] is None
    assert summary.upstream_absent == []
    assert LevelStore().read(WS) == mine
