"""`harness work list|peek|cancel` — parser wiring + the guards on the mutation verb.

These verbs sit between an operator (or a Launcher HUD) and a tree-kill, so the
tests here are about REFUSALS as much as results: an unconfirmed cancel must
name what it would destroy, a replayed cancel must be superseded rather than
applied, and every failure must arrive as a typed envelope with the exit code
the caller can branch on — never a stack trace or a bare non-zero.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_runtime import running_work
from hermes_cli.harness import build_parser


def parser():
    top = argparse.ArgumentParser()
    subs = top.add_subparsers(dest="command")
    build_parser(subs)
    return top


def _run(argv, capsys):
    args = parser().parse_args(argv)
    code = args.func(args)
    captured = capsys.readouterr().out.strip()
    return code, (json.loads(captured) if captured else None)


@pytest.fixture
def one_row(monkeypatch):
    """One cancellable terminal row, with the lanes reporting mixed health."""

    row = {
        "work_id": "terminal:sess-1",
        "kind": "terminal",
        "label": "npm run build",
        "command": "npm run build",
        "pid": 4242,
        "pid_verified": True,
        "owner": {
            "persona_id": "dev",
            "persona_instance_id": "personainst_neko",
            "session_id": "sess-key",
        },
        "status": "running",
        "started_at": "2026-08-03T10:00:00+00:00",
        "elapsed_seconds": 42,
        "progress": {
            "api_calls": None,
            "in_tool": None,
            "seconds_since_progress": None,
            "source": "unavailable",
        },
        "tail_preview": "compiling…",
        "source_lane": "live",
        "cancellable": True,
    }
    payload = {
        "rows": [row],
        "sources": {
            "terminal": {"status": "ok", "lane": "live"},
            "delegation": {"status": "ok", "lane": "durable"},
            "chat_turn": {"status": "ok", "lane": "durable"},
            "mcp_server": {"status": "unavailable", "lane": "live", "reason": "not_in_process"},
            "cron_job": {"status": "unavailable", "lane": "live", "reason": "not_in_process"},
        },
        "counts": {"total": 1, "running": 1, "unavailable_sources": 2},
    }
    monkeypatch.setattr(running_work, "build_running_work", lambda *a, **k: payload)
    return row


# --- list -------------------------------------------------------------------


def test_work_list_emits_rows_with_per_source_health(one_row, capsys):
    code, payload = _run(["harness", "work", "list", "--json"], capsys)

    assert code == 0
    assert payload["kind"] == "list"
    assert payload["item_kind"] == "running_work"
    assert [item["work_id"] for item in payload["items"]] == ["terminal:sess-1"]
    # The health block rides the SAME envelope as the rows: a consumer that got
    # rows without it would read an unreadable lane as "nothing running".
    assert payload["sources"]["mcp_server"]["status"] == "unavailable"
    assert payload["counts"]["unavailable_sources"] == 2


def test_work_list_filters_by_kind(one_row, capsys):
    code, payload = _run(["harness", "work", "list", "--kind", "delegation", "--json"], capsys)

    assert code == 0
    assert payload["items"] == []


def test_work_list_refuses_an_unknown_kind_instead_of_returning_everything(one_row, capsys):
    """An ignored filter is a WRONG ANSWER believed, not an error seen."""

    code, payload = _run(["harness", "work", "list", "--kind", "nonsense", "--json"], capsys)

    assert code == 2
    assert payload["error"]["code"] == "invalid_request"
    assert "terminal" in payload["error"]["safe_details"]["supported"]


def test_work_list_honors_the_stage42_limit_and_declares_truncation(
    monkeypatch, one_row, capsys
):
    """Actually exercise truncation.

    The first version of this test passed ``--limit 0`` and asserted nothing was
    truncated — which a completely broken ``--limit`` also satisfies. A test that
    cannot fail is worse than no test: it reads as coverage.
    """

    monkeypatch.setattr(
        running_work,
        "build_running_work",
        lambda *a, **k: {
            "rows": [
                {**one_row, "work_id": f"terminal:sess-{index}"} for index in range(5)
            ],
            "sources": {},
            "counts": {"total": 5},
        },
    )

    code, payload = _run(["harness", "work", "list", "--json", "--limit", "2"], capsys)

    assert code == 0
    assert len(payload["items"]) == 2
    assert payload["truncated"] is True


def test_work_list_below_the_limit_is_not_declared_truncated(one_row, capsys):
    code, payload = _run(["harness", "work", "list", "--json", "--limit", "10"], capsys)

    assert code == 0
    assert len(payload["items"]) == 1
    assert payload["truncated"] is False


def test_work_list_reports_its_own_completeness(monkeypatch, capsys):
    """The CLI lane accounts its drops too, or it sheds rows in silence."""

    def _build(accountant=None):
        assert accountant is not None, "the CLI lane must pass an accountant"
        accountant.consider(3)
        accountant.include(1)
        accountant.drop("process_exited", count=2, by_design=True)
        return {"rows": [], "sources": {}, "counts": {"total": 0}}

    monkeypatch.setattr(running_work, "build_running_work", _build)

    code, payload = _run(["harness", "work", "list", "--json"], capsys)

    assert code == 0
    assert payload["completeness"]["considered"] == 3
    assert payload["completeness"]["reasons"] == {"process_exited": 2}
    assert "process_exited" in payload["completeness"]["by_design"]


# --- peek -------------------------------------------------------------------


def test_work_peek_returns_a_bounded_read_only_payload(one_row, monkeypatch, capsys):
    monkeypatch.setattr(
        running_work,
        "peek_work",
        lambda work_id: {
            "work_id": work_id,
            "work_kind": "terminal",
            "found": True,
            "row": one_row,
            "tail_available": True,
            "tail": "compiling…",
            "tail_limit": running_work.PEEK_TAIL_LIMIT,
            "consumed": False,
        },
    )

    code, payload = _run(["harness", "work", "peek", "terminal:sess-1", "--json"], capsys)

    assert code == 0
    # The envelope's own kind survives: a row-level `kind` key here would
    # overwrite the discriminator every consumer branches on.
    assert payload["kind"] == "work_peek"
    assert payload["work_kind"] == "terminal"
    assert payload["consumed"] is False
    assert payload["tail_limit"] == 2048


def test_work_peek_reports_a_missing_row_as_not_found(one_row, capsys):
    code, payload = _run(["harness", "work", "peek", "terminal:ghost", "--json"], capsys)

    assert code == 3
    assert payload["error"]["code"] == "not_found"


def test_work_peek_rejects_a_malformed_id_as_invalid_request(one_row, capsys):
    code, payload = _run(["harness", "work", "peek", "garbage", "--json"], capsys)

    assert code == 2
    assert payload["error"]["code"] == "invalid_request"


# --- cancel -----------------------------------------------------------------


def test_cancel_without_yes_names_exactly_what_would_die(one_row, capsys):
    """Exit 8 + a target block. Confirming a SENTENCE is how the wrong thing dies."""

    code, payload = _run(["harness", "work", "cancel", "terminal:sess-1", "--json"], capsys)

    assert code == 8
    assert payload["error"]["code"] == "confirmation_required"
    details = payload["error"]["safe_details"]
    assert details["work_id"] == "terminal:sess-1"
    assert details["label"] == "npm run build"
    assert details["pid"] == 4242
    assert details["owner_persona_instance_id"] == "personainst_neko"
    assert "npm run build" in payload["error"]["message"]


def test_cancel_dry_run_previews_without_touching_anything(one_row, monkeypatch, capsys):
    def _never(*_args, **_kwargs):
        raise AssertionError("--dry-run must not reach the interrupt seam")

    monkeypatch.setattr(running_work, "cancel_work", _never)

    code, payload = _run(
        ["harness", "work", "cancel", "terminal:sess-1", "--json", "--dry-run"], capsys
    )

    assert code == 0
    assert payload["dry_run"] is True
    assert payload["cancelled"] is False
    assert payload["would_cancel"]["work_id"] == "terminal:sess-1"


def test_cancel_with_yes_routes_through_the_interrupt_seam(one_row, monkeypatch, capsys):
    seen = {}

    def _cancel(work_id, *, reason):
        seen.update(work_id=work_id, reason=reason)
        return {"status": "cancelled", "code": "", "work_id": work_id, "kind": "terminal"}

    monkeypatch.setattr(running_work, "cancel_work", _cancel)

    code, payload = _run(
        ["harness", "work", "cancel", "terminal:sess-1", "--json", "--yes", "--reason", "operator_stop"],
        capsys,
    )

    assert code == 0
    assert payload["cancelled"] is True
    assert payload["target"]["pid"] == 4242
    assert seen == {"work_id": "terminal:sess-1", "reason": "operator_stop"}


def test_cancel_issued_before_the_work_started_is_superseded_not_applied(
    one_row, monkeypatch, capsys
):
    """Replay guard: work ids are stable per spawn, so a late-arriving cancel
    aimed at a PREVIOUS incarnation must never kill the current one."""

    def _never(*_args, **_kwargs):
        raise AssertionError("a superseded cancel must not reach the interrupt seam")

    monkeypatch.setattr(running_work, "cancel_work", _never)

    code, payload = _run(
        [
            "harness", "work", "cancel", "terminal:sess-1", "--json", "--yes",
            "--issued-at", "2026-08-03T09:00:00Z",
        ],
        capsys,
    )

    assert code == 4
    assert payload["error"]["code"] == "stale_revision"
    assert payload["error"]["safe_details"]["started_at"] == "2026-08-03T10:00:00+00:00"


def test_cancel_issued_after_the_work_started_proceeds(one_row, monkeypatch, capsys):
    monkeypatch.setattr(
        running_work,
        "cancel_work",
        lambda work_id, *, reason: {"status": "cancelled", "code": ""},
    )

    code, payload = _run(
        [
            "harness", "work", "cancel", "terminal:sess-1", "--json", "--yes",
            "--issued-at", "2026-08-03T10:00:01Z",
        ],
        capsys,
    )

    assert code == 0
    assert payload["cancelled"] is True


def test_cancel_reports_unknown_work_as_not_found(one_row, capsys):
    code, payload = _run(["harness", "work", "cancel", "terminal:ghost", "--json", "--yes"], capsys)

    assert code == 3
    assert payload["error"]["code"] == "not_found"


def test_the_replay_guard_does_not_refuse_a_legitimate_cancel_across_timezones(
    monkeypatch, one_row, capsys
):
    """Regression: naive-local `started_at` compared against a UTC `--issued-at`.

    The projection now anchors every `started_at` to UTC, so this comparison is
    between two stamps in the same frame. Before it was not: a naive local stamp
    read as UTC landed hours in the FUTURE in any UTC-plus timezone, and a
    cancel issued *right now* compared as "issued before the work started" and
    was refused `stale_revision`. The row below carries the real wire shape — an
    offset-bearing stamp — and a plainly-later `--issued-at` must go through.
    """

    started = datetime(2026, 8, 3, 10, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        running_work,
        "build_running_work",
        lambda *a, **k: {
            "rows": [{**one_row, "started_at": started.isoformat()}],
            "sources": {},
            "counts": {"total": 1},
        },
    )
    monkeypatch.setattr(
        running_work,
        "cancel_work",
        lambda work_id, *, reason: {"status": "cancelled", "code": ""},
    )

    code, payload = _run(
        [
            "harness", "work", "cancel", "terminal:sess-1", "--json", "--yes",
            "--issued-at", (started + timedelta(minutes=5)).isoformat(),
        ],
        capsys,
    )

    assert code == 0
    assert payload["cancelled"] is True


@pytest.mark.parametrize(
    "code_in, exit_code",
    [
        ("cancel_unsupported", 6),
        ("cancel_unavailable", 7),
        ("cancel_failed", 7),
    ],
)
def test_a_refused_cancel_arrives_typed_with_its_own_exit_code(
    one_row, monkeypatch, capsys, code_in, exit_code
):
    monkeypatch.setattr(
        running_work,
        "cancel_work",
        lambda work_id, *, reason: {"status": "error", "code": code_in, "detail": "nope"},
    )

    code, payload = _run(["harness", "work", "cancel", "terminal:sess-1", "--json", "--yes"], capsys)

    assert code == exit_code
    assert payload["error"]["code"] == code_in
    assert payload["error"]["safe_details"]["work_id"] == "terminal:sess-1"


# --- parser shape -----------------------------------------------------------


def test_work_requires_a_subcommand():
    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "work"])


def test_only_cancel_carries_the_mutation_flags():
    listed = parser().parse_args(["harness", "work", "list", "--json"])
    assert not hasattr(listed, "yes")

    cancelled = parser().parse_args(["harness", "work", "cancel", "terminal:x", "--json"])
    assert cancelled.yes is False
    assert cancelled.dry_run is False
    assert cancelled.issued_at is None


@pytest.mark.parametrize(
    "argv, flag",
    [
        # `work list` is a point-in-time census: no page to resume, no history
        # to filter.
        (["harness", "work", "list"], "--cursor"),
        (["harness", "work", "list"], "--since"),
        # peek/cancel answer about ONE row.
        (["harness", "work", "peek", "terminal:x"], "--sort"),
        (["harness", "work", "peek", "terminal:x"], "--limit"),
        (["harness", "work", "peek", "terminal:x"], "--cursor"),
        (["harness", "work", "peek", "terminal:x"], "--since"),
        (["harness", "work", "cancel", "terminal:x"], "--sort"),
        (["harness", "work", "cancel", "terminal:x"], "--limit"),
        (["harness", "work", "cancel", "terminal:x"], "--cursor"),
        (["harness", "work", "cancel", "terminal:x"], "--since"),
        # Replay protection on cancel is `--issued-at`; a second unread key
        # would imply a guarantee nothing here provides.
        (["harness", "work", "cancel", "terminal:x"], "--idempotency-key"),
    ],
)
def test_a_flag_these_verbs_cannot_honor_is_refused_not_swallowed(argv, flag):
    """An accepted-but-ignored flag is a wrong answer believed, not an error seen."""

    with pytest.raises(SystemExit):
        parser().parse_args([*argv, flag, "x"])


def test_the_flags_these_verbs_do_honor_are_still_accepted():
    listed = parser().parse_args(
        ["harness", "work", "list", "--json", "--sort", "label", "--limit", "3"]
    )
    assert listed.sort == "label"
    assert listed.limit == 3

    peeked = parser().parse_args(
        ["harness", "work", "peek", "terminal:x", "--fields", "work_id", "--quiet"]
    )
    assert peeked.fields == "work_id"
    assert peeked.quiet is True


def test_omitting_a_flag_is_scoped_to_the_verb_that_asked():
    """`omit` must not leak: other verbs keep the full shared contract."""

    listed = parser().parse_args(
        ["harness", "workspace", "list", "--json", "--cursor", "abc", "--since", "x"]
    )
    assert listed.cursor == "abc"
    assert listed.since == "x"
