"""`hermes harness level {show,set}` — the two verbs the launcher drives.

THE contract these hold, and it is the only interesting thing about them: the
launcher owns the level document's FORMAT and hermes owns its transport. So
``set`` then ``show --full`` must return the operator's bytes unchanged — not
"a document that parses the same", the same bytes — because the launcher writes
the document indented one prop per line so a realm-sync diff is per-prop hunks,
and a hermes that re-serialized would undo that on every machine that pulled.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime import paths as runtime_paths
from agent_runtime.level_sync import LevelStore
from agent_runtime.store import WorkspaceStore

from tests.agent_runtime._harness_cli import run_harness_in_process

WS = "ws_launcher"


def _document(*, prop_x: float = 1.0) -> str:
    return json.dumps(
        {
            "version": 6,
            "scene": {"id": f"ws-{WS}", "props": [{"id": "a1b2c3d4", "x": prop_x, "y": 2.0}]},
        },
        indent=2,
        sort_keys=True,
    )


def _run(*args: str):
    return run_harness_in_process("level", *args)


def _payload(result):
    return json.loads(result.stdout)


def test_set_then_show_full_returns_the_operators_bytes_unchanged(
    isolate_agent_runtime_root, tmp_path
):
    document = _document()
    path = tmp_path / "level.json"
    path.write_text(document, encoding="utf-8", newline="")

    written = _run("set", "--workspace", WS, "--document", str(path), "--json")
    assert written.returncode == 0
    assert _payload(written)["changed"] is True

    read_back = _run("show", "--workspace", WS, "--full", "--json")
    assert read_back.returncode == 0
    body = _payload(read_back)

    assert body["present"] is True
    assert body["version"] == 6
    assert body["document"] == document
    # The property the round trip is actually about, stated as bytes.
    assert body["document"].encode("utf-8") == path.read_bytes()
    assert body["sha256"] == _payload(written)["sha256"]


def test_set_accepts_the_document_inline_as_well_as_by_path(
    isolate_agent_runtime_root,
):
    result = _run("set", "--workspace", WS, "--document", _document(), "--json")

    assert result.returncode == 0
    assert LevelStore().read(WS).decode("utf-8") == _document()


def test_show_on_a_workspace_with_no_level_is_an_honest_empty_not_an_error(
    isolate_agent_runtime_root,
):
    result = _run("show", "--workspace", "ws_never_authored", "--full", "--json")

    assert result.returncode == 0
    body = _payload(result)
    assert body["present"] is False
    assert body["document"] is None
    assert body["version"] is None


def test_set_refuses_a_document_with_no_version_and_names_which_refusal(
    isolate_agent_runtime_root,
):
    """``code`` is a FAMILY (``invalid_payload`` covers four different
    documents); ``reason`` is the word the store door actually used, which is
    what lets the launcher say WHICH one rather than "the level was rejected"."""

    result = _run("set", "--workspace", WS, "--document", '{"scene": {}}', "--json")

    assert result.returncode != 0
    body = _payload(result)
    assert body["error"]["code"] == "invalid_payload"
    assert body["error"]["reason"] == "missing_version"
    assert LevelStore().read(WS) is None


def test_set_refuses_bytes_that_are_not_json_at_all(isolate_agent_runtime_root):
    result = _run("set", "--workspace", WS, "--document", "{not json", "--json")

    assert result.returncode != 0
    assert _payload(result)["error"]["reason"] == "unreadable_document"


def test_dry_run_validates_and_reports_without_writing(isolate_agent_runtime_root):
    result = _run("set", "--workspace", WS, "--document", _document(), "--dry-run", "--json")

    assert result.returncode == 0
    body = _payload(result)
    assert body["dry_run"] is True
    assert body["changed"] is True
    # The whole point: nothing is on disk.
    assert LevelStore().read(WS) is None


def test_dry_run_over_an_identical_document_says_nothing_would_change(
    isolate_agent_runtime_root,
):
    LevelStore().write(WS, _document().encode("utf-8"))

    body = _payload(_run("set", "--workspace", WS, "--document", _document(), "--dry-run", "--json"))

    assert body["changed"] is False


def test_the_verbs_fall_back_to_the_active_workspace(isolate_agent_runtime_root):
    workspace = WorkspaceStore().create(name="Active")
    WorkspaceStore().set_active(workspace.id)

    assert _run("set", "--document", _document(), "--json").returncode == 0
    assert LevelStore().read(workspace.id).decode("utf-8") == _document()
    assert _payload(_run("show", "--json"))["workspace_id"] == workspace.id


def test_show_names_which_root_answered(isolate_agent_runtime_root):
    """A wrong runtime root returns a well-formed EMPTY answer — ``present:
    false`` — indistinguishable from a workspace that genuinely has no level.
    The resolution block is what makes the envelope say which root it read."""

    body = _payload(_run("show", "--workspace", WS, "--json"))

    assert "resolution" in body


def test_a_stored_document_that_stops_reading_is_still_handed_back(
    isolate_agent_runtime_root,
):
    """The repair path. A ``show`` that refused an unreadable stored document
    would leave the operator with no way to see what is in the file that broke
    — so the row stays, ``version`` goes null, and ``--full`` still carries the
    bytes."""

    path = runtime_paths.level_path(WS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{half a document")

    result = _run("show", "--workspace", WS, "--full", "--json")

    assert result.returncode == 0
    body = _payload(result)
    assert body["present"] is True
    assert body["version"] is None
    assert body["document"] == "{half a document"
