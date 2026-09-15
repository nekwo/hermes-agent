"""The patch diff artifact: what gets written, what gets counted, what is kept.

Every test here runs against the ``tests/agent_runtime`` conftest's isolated
store root (``isolate_agent_runtime_root`` pins ``HERMES_AGENT_RUNTIME_ROOT``
into ``tmp_path``), so the live store is never touched — which matters more for
this module than most, because it is the one that writes files.
"""

from datetime import datetime, timezone

import json

import pytest

from agent_runtime import paths, patch_diff_artifacts
from agent_runtime.patch_diff_artifacts import (
    PATCH_DIFF_MAX_BYTES,
    PATCH_DIFF_RETAIN,
    PATCH_DIFF_TRUNCATION_MARKER,
    diff_counts,
    record_patch_diff,
    result_diff,
)

_DIFF = (
    "--- a/lib/main.dart\n"
    "+++ b/lib/main.dart\n"
    "@@ -1,4 +1,5 @@\n"
    " void main() {\n"
    "-  print('old');\n"
    "+  print('new');\n"
    "+  print('extra');\n"
    " }\n"
)


def _artifacts() -> list:
    root = paths.patch_diffs_dir()
    return sorted(root.glob("*.diff")) if root.exists() else []


def test_dict_result_writes_the_exact_diff_and_counts_it():
    fields = record_patch_diff({"success": True, "diff": _DIFF})

    assert fields is not None
    # +++/--- are file headers, not content: two adds and one delete.
    assert fields["patch_adds"] == 2
    assert fields["patch_dels"] == 1
    written = _artifacts()
    assert len(written) == 1
    assert str(written[0]) == fields["patch_artifact"]
    assert written[0].read_text(encoding="utf-8") == _DIFF


def test_json_string_result_is_parsed():
    """The shape the REAL lane delivers.

    ``patch_tool`` returns ``json.dumps(result_dict)`` and the agent loop hands
    the callback that string verbatim (``agent/tool_executor.py`` measures it
    with ``len()``), so a dict-only reader would have produced no artifact on
    any real patch call. Both shapes are supported; this is the one that ships.
    """

    fields = record_patch_diff(json.dumps({"success": True, "diff": _DIFF}))

    assert fields is not None
    assert fields["patch_adds"] == 2
    assert _artifacts()[0].read_text(encoding="utf-8") == _DIFF


@pytest.mark.parametrize(
    "result",
    [
        None,
        "",
        "not json at all",
        # A result truncated past the tool's 100K result cap: json.loads fails.
        '{"success": true, "diff": "--- a/x\\n+++ b/x\\n+one',
        {"success": False, "error": "patch did not apply"},
        {"success": True, "diff": ""},
        {"success": True, "diff": "   \n  \n"},
        ["not", "a", "dict"],
        42,
    ],
)
def test_a_result_with_no_readable_diff_writes_nothing(result):
    """Honest absence, never a broken tile: every degraded shape yields None
    and leaves no file behind, so the tile simply renders without the viewer."""

    assert record_patch_diff(result) is None
    assert _artifacts() == []


def test_counts_ignore_file_headers_and_total_a_multi_file_diff():
    multi = (
        "--- a/one.py\n+++ b/one.py\n@@ -1 +1 @@\n-a\n+b\n"
        "--- a/two.py\n+++ b/two.py\n@@ -1 +1,2 @@\n-c\n+d\n+e\n"
    )

    assert diff_counts(multi) == (3, 2)


def test_result_diff_reads_both_shapes_and_refuses_the_rest():
    assert result_diff({"diff": _DIFF}) == _DIFF
    assert result_diff(json.dumps({"diff": _DIFF})) == _DIFF
    assert result_diff({"diff": 17}) is None
    assert result_diff(object()) is None


def test_an_oversized_diff_is_truncated_but_its_counts_stay_honest():
    """The file is bounded; the numbers on the tile are not.

    Counts come from the FULL diff, so an operator reading ``+40000`` beside a
    truncated artifact learns the true size of the change and is told, in the
    file itself, that they are not seeing all of it."""

    line = "+" + ("x" * 79) + "\n"
    huge = "--- a/big.py\n+++ b/big.py\n@@ -0,0 +1 @@\n" + line * 8000
    full_adds, _ = diff_counts(huge)
    assert len(huge.encode("utf-8")) > PATCH_DIFF_MAX_BYTES

    fields = record_patch_diff({"success": True, "diff": huge})

    assert fields is not None
    assert fields["patch_adds"] == full_adds == 8000
    body = _artifacts()[0].read_text(encoding="utf-8")
    assert len(body.encode("utf-8")) <= PATCH_DIFF_MAX_BYTES
    assert body.endswith(f"{PATCH_DIFF_TRUNCATION_MARKER}\n")
    # Truncated at a LINE boundary — never mid-line, which would render as a
    # corrupt diff row rather than an honestly short one.
    for candidate in body.split("\n")[:-2]:
        assert candidate in {"--- a/big.py", "+++ b/big.py", "@@ -0,0 +1 @@"} or (
            candidate == line.rstrip("\n")
        )


def test_the_same_diff_written_twice_is_one_file(monkeypatch):
    """Content-hash naming makes a double emission idempotent.

    The clock is pinned because the filename's timestamp prefix is what makes a
    directory listing a recency sort; without pinning, the second write could
    land in the next second and the test would be a race rather than a claim.
    """

    monkeypatch.setattr(
        patch_diff_artifacts,
        "_utc_now",
        lambda: datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc),
    )

    first = record_patch_diff({"success": True, "diff": _DIFF})
    second = record_patch_diff({"success": True, "diff": _DIFF})

    assert first == second
    assert len(_artifacts()) == 1


def test_retention_moves_the_oldest_out_and_never_deletes_them():
    """Archive-never-delete, the same discipline
    ``prompt_observability._index_and_retain_after_persist`` runs on: the live
    dir is bounded, eviction MOVES, and nothing is ever destroyed."""

    live = paths.patch_diffs_dir()
    live.mkdir(parents=True, exist_ok=True)
    stale = [f"2020010{i // 100}T{i % 100:02d}0000Z_{i:012x}.diff" for i in range(410)]
    for name in stale:
        (live / name).write_text("old\n", encoding="utf-8")
    assert len(_artifacts()) == 410

    fields = record_patch_diff({"success": True, "diff": _DIFF})

    assert fields is not None
    remaining = _artifacts()
    assert len(remaining) == PATCH_DIFF_RETAIN
    # The freshly written one is newest by timestamp prefix, so it survives.
    assert str(fields["patch_artifact"]) in {str(item) for item in remaining}
    archived = sorted(paths.patch_diffs_archive_dir().glob("*.diff"))
    assert len(archived) == 411 - PATCH_DIFF_RETAIN
    # Evicted, not destroyed: the bytes are still readable from the archive.
    assert archived[0].read_text(encoding="utf-8") == "old\n"


def test_the_artifact_is_lf_on_every_platform():
    """A unified diff's line ending is part of its grammar, not the host's
    convention — the launcher's renderer splits on ``\\n`` and a stray ``\\r``
    would ride into every rendered line."""

    record_patch_diff({"success": True, "diff": _DIFF})

    raw = _artifacts()[0].read_bytes()
    assert b"\r\n" not in raw
    assert raw == _DIFF.encode("utf-8")


def test_a_store_that_refuses_the_write_costs_the_affordance_not_the_turn(
    monkeypatch,
):
    """Observability must never be able to break a turn."""

    def _boom(*_args, **_kwargs):
        raise OSError("disk is having a day")

    monkeypatch.setattr(patch_diff_artifacts.paths, "patch_diffs_dir", _boom)

    assert record_patch_diff({"success": True, "diff": _DIFF}) is None
