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


def test_work_list_honors_the_stage42_limit_and_declares_truncation(one_row, capsys):
    code, payload = _run(["harness", "work", "list", "--json", "--limit", "0"], capsys)

    assert code == 0
    assert payload["truncated"] is False


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
