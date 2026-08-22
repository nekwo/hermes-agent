"""BO-4: the demote receipts are finally read, and the census cannot flatter them.

THE ABSENCE THIS CLOSES
=======================

``core_cache``'s channel table says it in its own words: "``CoreDecision.reason``
is read by no caller today… a field census of WHY a cache demoted has exactly one
source: this line." The ``fingerprint_mismatch`` tail is good instrumentation
nobody consumed — which is how a cache that invalidated itself on every boot ran
unnoticed until somebody hand-diffed mtimes against a build window. The gap was
consumption, never emission.

WHAT MAKES THESE CASES NON-VACUOUS
==================================

**Every line under test is emitted by the real producer.** ``core_cache._log_demote``
builds it, with its real format string and its real tail helpers; the tests
choose the demote's REASON and, in one case, the diff's CONTENT — never the
line's spelling. A hand-typed fixture would let a producer rename leave this
census measuring a permanent, confident zero, which is the same class of failure
as the zero-parse guard one level down.

Each of the table's census rules gets a case that fails if the census breaks it,
and each case names the reading the census must NOT produce.
"""

from __future__ import annotations

import logging

import pytest

from agent_runtime import core_cache
from agent_runtime.core_cache_census import (
    DIFF_TAIL_ABSENT,
    PATH_RUNTIME_AUTHORED,
    PATH_STORE,
    VERDICT_DIFF_UNAVAILABLE,
    VERDICT_NO_LINES,
    VERDICT_SELF_PERTURBATION,
    VERDICT_STORE_CHURN,
    census_demotes,
    classify_path,
    format_census,
    parse_demote_line,
)

_LOGGER = "agent_runtime.core_cache"

#: One of the four names the channel table lists verbatim as runtime-authored.
#: Taken from the module that OWNS it, never spelled, for the reason
#: ``core_cache``'s exclusion set records: a comment naming a constant is not a
#: reference to it, and the last hand-spelling in this family named a file that
#: had never existed.
from agent_runtime.dispatch_delivery import DRAIN_STATE_FILENAME  # noqa: E402


def _emit(caplog, **kwargs) -> list[str]:
    """Run the REAL demote receipt and hand back the lines it wrote."""

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        core_cache._log_demote(**kwargs)
    return [record.getMessage() for record in caplog.records]


# --------------------------------------------------------------------------- #
# 1. self-perturbation is FLAGGED, by name
# --------------------------------------------------------------------------- #
def test_a_diff_naming_a_runtime_authored_path_is_flagged_by_name(caplog):
    """The finding this census exists for, on a line the producer wrote.

    The diff's CONTENT is chosen (through the producer's own ``_diff_detail``,
    so the tail is spelled by the code that ships it); everything else — the
    family token, the field order, the fact that ``diff=`` goes last — is the
    real receipt.

    *Kill:* bucket every diff path as store churn. The verdict drops to
    ``store_churn`` and the operator reads "the cache is working as designed"
    about a runtime perturbing its own key — which is the report the last one of
    these got for months.
    """

    perturbing = f"C:/hermes/store/{DRAIN_STATE_FILENAME}"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            core_cache,
            "_demote_diff_detail",
            lambda key, sidecar: core_cache._diff_detail(
                core_cache.DIFF_SCOPE_LAST_PAIR,
                [perturbing, "C:/hermes/store/workspaces/ws_operator.json"],
            ),
        )
        lines = _emit(
            caplog,
            caller="hub",
            reason=core_cache.DEMOTE_FINGERPRINT_MISMATCH,
            key=None,
            sidecar=None,
        )

    report = census_demotes(lines)
    assert report["verdict"] == VERDICT_SELF_PERTURBATION, report
    mismatch = report["fingerprint_mismatch"]
    assert mismatch["self_perturbation_lines"] == 1
    assert mismatch["runtime_authored_paths"] == {DRAIN_STATE_FILENAME: 1}
    # The operator-facing text has to NAME it — a count with no name sends the
    # reader back to the log this tool exists to save them from.
    text = format_census(report)
    assert DRAIN_STATE_FILENAME in text, text
    assert "SELF-PERTURBATION" in text, text
    # The store path on the SAME line is still reported, and still as store
    # churn: the line is true about both, and collapsing them would lose the
    # distinction the whole classification is.
    assert "C:/hermes/store/workspaces/ws_operator.json" in mismatch["store_paths"]


def test_a_diff_of_only_store_paths_is_not_a_defect(caplog):
    """The other direction, and the reason this is a census and not an alarm.

    The table's most important sentence about this receipt: the scope is
    ``last_pair`` BY CONSTRUCTION, so on a busy store the diff legitimately
    names files that are simply moving and the receipt is TRUE without naming a
    defect. A census that flagged those would be retrained-away within a week.

    *Kill:* flag every ``fingerprint_mismatch``. This case reds, and with it the
    census's whole claim to be worth reading.
    """

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            core_cache,
            "_demote_diff_detail",
            lambda key, sidecar: core_cache._diff_detail(
                core_cache.DIFF_SCOPE_LAST_PAIR,
                ["C:/hermes/store/workspaces/ws_a.json", "C:/hermes/store/offices/o.json"],
            ),
        )
        lines = _emit(
            caplog,
            caller="cli",
            reason=core_cache.DEMOTE_FINGERPRINT_MISMATCH,
            key=None,
            sidecar=None,
        )

    report = census_demotes(lines)
    assert report["verdict"] == VERDICT_STORE_CHURN, report
    assert report["fingerprint_mismatch"]["self_perturbation_lines"] == 0
    assert report["fingerprint_mismatch"]["store_only_lines"] == 1
    assert report["fingerprint_mismatch"]["runtime_authored_paths"] == {}


@pytest.mark.parametrize(
    "path, expected",
    [
        ("C:/store/serve_socket.owner.json", PATH_RUNTIME_AUTHORED),
        ("C:/store/serve_socket.lock", PATH_RUNTIME_AUTHORED),
        (f"C:/store/{DRAIN_STATE_FILENAME}", PATH_RUNTIME_AUTHORED),
        ("C:/store/state.db-wal", PATH_RUNTIME_AUTHORED),
        ("/home/x/.hermes/store/state.db-wal", PATH_RUNTIME_AUTHORED),
        ("C:/store/state.db", PATH_STORE),
        ("C:/store/state.db-journal", PATH_STORE),
        ("C:/store/workspaces/serve_socket.owner.json.bak", PATH_STORE),
        ("C:/store/personas/dev.json", PATH_STORE),
    ],
)
def test_the_classification_is_the_tables_list_and_not_a_substring_match(
    path, expected
):
    """All four names the table lists, both separators, and the near-misses.

    The near-misses are the point. A ``in path`` implementation passes every
    positive row above and then flags ``serve_socket.owner.json.bak`` — an
    operator's own backup — as the runtime perturbing itself, which is a false
    alarm on the one signal this census has.

    ``state.db-journal`` is deliberately STORE: the table names the ``-wal``
    sibling, and ``core_cache``'s own mask exists because a WAL commit that has
    not checkpointed is the mtime-blind case. The journal's presence is real
    information about the store, not about the runtime writing beside it.
    """

    assert classify_path(path) == expected


# --------------------------------------------------------------------------- #
# 2. "could not measure" never reads as "nothing moved"
# --------------------------------------------------------------------------- #
def test_a_diff_unavailable_arm_reports_unavailable_and_never_clean(caplog):
    """C16's lesson, executed.

    ``_log_demote`` with no key takes the real ``no_entries`` arm and emits
    ``diff_scope=none changed=0 diff_reason=no_entries diff=diff_unavailable`` —
    the producer saying, in its own words, that a diff was owed here and could
    not be computed. An empty ``diff=`` would have read to a census exactly like
    "we looked and nothing moved"; so must this.

    *Kill:* treat a ``diff_reason=`` line as a mismatch with no changed paths —
    i.e. as clean. The verdict becomes ``no_fingerprint_mismatch`` and the
    report tells an operator the window was fine when nothing in it was
    measured. Watched.
    """

    lines = _emit(
        caplog,
        caller="prewarm",
        reason=core_cache.DEMOTE_FINGERPRINT_MISMATCH,
        key=None,
        sidecar=None,
    )
    assert f"diff_reason={core_cache.DIFF_UNAVAILABLE_NO_ENTRIES}" in lines[0], lines
    assert f"diff={core_cache.DIFF_UNAVAILABLE}" in lines[0], lines

    report = census_demotes(lines)
    assert report["verdict"] == VERDICT_DIFF_UNAVAILABLE, report
    mismatch = report["fingerprint_mismatch"]
    assert mismatch["lines"] == 1
    assert mismatch["diff_unavailable_lines"] == 1
    assert mismatch["diff_unavailable_reasons"] == {
        core_cache.DIFF_UNAVAILABLE_NO_ENTRIES: 1
    }
    assert mismatch["self_perturbation_lines"] == 0
    assert mismatch["store_only_lines"] == 0

    text = format_census(report)
    assert "UNAVAILABLE" in text
    assert "never 'nothing moved'" in text
    assert "clean" not in text.lower(), text


def test_a_mismatch_line_with_no_tail_at_all_is_also_unmeasured():
    """An install predating the tail. Still not clean.

    The producer grew the ``changed=``/``diff=`` tail after this receipt had
    been shipping for a while, so a real log can carry ``fingerprint_mismatch``
    lines with nothing after ``inputs=``. That is not "we looked and nothing
    moved" either, and it gets its own arm rather than borrowing ``no_entries``
    — the two say different things about what to do next.
    """

    line = (
        "2026-08-01 10:00:00 INFO agent_runtime.core_cache: snapshot_core_cache "
        f"core_source={core_cache.CORE_SOURCE_REBUILT} caller=hub "
        f"reason={core_cache.DEMOTE_FINGERPRINT_MISMATCH} inputs=23107"
    )
    report = census_demotes([line])
    assert report["verdict"] == VERDICT_DIFF_UNAVAILABLE, report
    assert report["fingerprint_mismatch"]["diff_unavailable_reasons"] == {
        DIFF_TAIL_ABSENT: 1
    }


# --------------------------------------------------------------------------- #
# 3. only fingerprint_mismatch is owed a tail
# --------------------------------------------------------------------------- #
def test_a_build_stamp_mismatch_is_never_treated_as_a_missing_diff(caplog):
    """The table: ``fingerprint_mismatch`` ALONE grows a tail.

    A diff on a ``build_stamp_mismatch`` would name every file the operator's
    upgrade touched and read to a census as store churn, which is a measurement
    that would be true of the wrong thing — so the producer does not compute
    one. A census that expected one would report every upgrade boot as an
    unmeasured window and bury the arm that matters.

    *Kill:* bucket every demote reason's tail. The ``build_stamp_mismatch`` line
    lands in ``diff_unavailable`` and the verdict flips away from the one this
    window earned.
    """

    lines = _emit(
        caplog,
        caller="hydrate",
        reason=core_cache.DEMOTE_BUILD_STAMP_MISMATCH,
        key=None,
        sidecar=None,
    )
    assert "diff_scope" not in lines[0], lines
    assert "diff=" not in lines[0], lines

    report = census_demotes(lines)
    assert report["reasons"] == {core_cache.DEMOTE_BUILD_STAMP_MISMATCH: 1}
    assert report["demotes_parsed"] == 1
    mismatch = report["fingerprint_mismatch"]
    assert mismatch["lines"] == 0
    assert mismatch["diff_unavailable_lines"] == 0
    assert mismatch["runtime_authored_paths"] == {}


# --------------------------------------------------------------------------- #
# 4. "no demote line" is never "no demote"
# --------------------------------------------------------------------------- #
def test_a_window_with_no_receipts_is_a_failure_not_a_pass():
    """``absent`` is deliberately never logged, so silence is UNMEASURED.

    The ordinary cold start would print a line on every build in every process,
    so ``core_cache`` prints none — which means the only trace of an ``absent``
    demote is the ABSENCE of a line. A census that returned "healthy" for an
    empty scan would be indistinguishable from one measuring a healthy runtime,
    and would answer a wrong log path, a rotated log or a renamed family token
    with a confident zero.

    *Kill:* return ``ok`` when nothing parsed. The tool then passes on a typo in
    ``--log``.
    """

    report = census_demotes(["nothing relevant here", "2026-08-01 INFO x: hello"])
    assert report["verdict"] == VERDICT_NO_LINES
    assert report["demotes_parsed"] == 0
    text = format_census(report)
    assert "NOT A CLEAN BILL" in text, text
    assert "UNMEASURED" in text, text


def test_the_script_exits_non_zero_on_an_unmeasured_window(tmp_path, capsys):
    """The exit code is the part a checklist or a QA lane actually reads."""

    from scripts.core_cache_demote_census import (
        EXIT_OK,
        EXIT_SELF_PERTURBATION,
        EXIT_UNMEASURED,
        main,
    )

    empty = tmp_path / "agent.log"
    empty.write_text("2026-08-01 10:00:00 INFO x: unrelated\n", encoding="utf-8")
    assert main(["--log", str(empty)]) == EXIT_UNMEASURED

    missing = tmp_path / "not-here.log"
    assert main(["--log", str(missing)]) == EXIT_UNMEASURED

    perturbing = tmp_path / "perturbing.log"
    perturbing.write_text(
        "2026-08-21 15:33:01 INFO agent_runtime.core_cache: snapshot_core_cache "
        f"core_source={core_cache.CORE_SOURCE_REBUILT} caller=hub "
        f"reason={core_cache.DEMOTE_FINGERPRINT_MISMATCH} inputs=23107 "
        f"{core_cache._diff_detail(core_cache.DIFF_SCOPE_LAST_PAIR, ['C:/s/' + DRAIN_STATE_FILENAME])}\n",
        encoding="utf-8",
    )
    assert main(["--log", str(perturbing)]) == EXIT_SELF_PERTURBATION

    churn = tmp_path / "churn.log"
    churn.write_text(
        "2026-08-21 15:33:01 INFO agent_runtime.core_cache: snapshot_core_cache "
        f"core_source={core_cache.CORE_SOURCE_REBUILT} caller=hub "
        f"reason={core_cache.DEMOTE_FINGERPRINT_MISMATCH} inputs=23107 "
        f"{core_cache._diff_detail(core_cache.DIFF_SCOPE_LAST_PAIR, ['C:/s/workspaces/w.json'])}\n",
        encoding="utf-8",
    )
    assert main(["--log", str(churn)]) == EXIT_OK
    capsys.readouterr()


# --------------------------------------------------------------------------- #
# 5. the census parses what the producer emits — driven end-to-end
# --------------------------------------------------------------------------- #
def test_the_census_reads_a_receipt_the_real_cache_lane_wrote(
    isolate_agent_runtime_root, caplog
):
    """No hand-typed line anywhere on this path.

    A real isolated store, a real converged persisted core, a real event-less
    durable write, and a real ``consult()`` that demotes. If the receipt's
    family token, field order or tail placement ever moves, this reds — where
    every case above would keep passing on lines the producer no longer writes.

    *Kill:* rename the family token, or move ``diff=`` off the end of the line.
    """

    from agent_runtime.snapshot import build_snapshot
    from agent_runtime.store import WorkspaceStore
    from utils import atomic_json_write
    import json as _json

    from agent_runtime import paths

    core_cache.reset_process_state()
    try:
        workspace = WorkspaceStore().create(name="census-subject")
        for _ in range(4):
            core_cache.core_path().unlink(missing_ok=True)
            core_cache.sidecar_path().unlink(missing_ok=True)
            core_cache.reset_process_state()
            build_snapshot()
            if core_cache.read_persisted_core().matched:
                break
        else:
            raise AssertionError(
                "the persisted key never converged, so no demote below would be "
                "the one this case is written about"
            )

        # Event-less durable write: the store moves, the log does not.
        path = paths.workspace_path(workspace.id)
        payload = _json.loads(path.read_text(encoding="utf-8"))
        payload["name"] = "census-subject-renamed"
        atomic_json_write(path, payload)

        core_cache.reset_process_state()
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            decision = core_cache.consult(caller="census")
        assert decision.demoted, "the lane did not demote, so no receipt was written"
    finally:
        core_cache.reset_process_state()

    lines = [record.getMessage() for record in caplog.records]
    report = census_demotes(lines)

    assert report["demotes_parsed"] == 1, lines
    assert report["reasons"] == {core_cache.DEMOTE_FINGERPRINT_MISMATCH: 1}
    parsed = parse_demote_line(
        next(line for line in lines if "core_source=rebuilt" in line)
    )
    assert parsed is not None
    assert parsed["caller"] == "census"
    assert parsed["reason"] == core_cache.DEMOTE_FINGERPRINT_MISMATCH

    # The store path the test itself moved must be the one the diff names, and
    # it must be bucketed as store churn — an operator's own write, correctly
    # read as the cache working as designed.
    mismatch = report["fingerprint_mismatch"]
    assert mismatch["lines"] == 1
    assert mismatch["runtime_authored_paths"] == {}, mismatch
    assert mismatch["diff_unavailable_lines"] == 0, mismatch
    assert any(
        str(path).replace("\\", "/") == named.replace("\\", "/")
        for named in mismatch["store_paths"]
    ), mismatch
