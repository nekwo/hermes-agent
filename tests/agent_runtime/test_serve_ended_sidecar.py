"""RL-16: the runtime says why it ended, and no reader mistakes it for a row.

Two claims live here, and they are separable on purpose.

1. **The sidecar is a record with a shape.** ``serve_instances/<pid>.ended.json``
   carries exactly four keys, is written through the same ACL-safe atomic
   writer the registry row uses, survives the row's removal, and is pruned to a
   bounded newest-N on boot so a machine that runs a runtime a hundred times a
   day does not accumulate a hundred files a day forever.

2. **Every reader of ``serve_instances/`` ignores it.** The directory grew a
   second file shape, and the scan that finds rows globs ``*.json`` — so a
   sidecar left by the previous runtime would have been read as a registry
   record with no pid, no port and no classification, by the registry lister,
   by socket-target resolution (which is what ``harness serve connect`` and the
   launcher's attach lane both go through), by the boot prune, and by
   ``harness status --json``. One test per reader.

The reason vocabulary's *sources* — the console control handler, the signal
handlers, the drain and shutdown paths, an uncaught exception — are proven
against real spawned processes in ``test_serve_ended_sidecar_child_e2e.py``.
This file holds the seam: the mapping tables and the record, without a boot.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agent_runtime.serve_registry import (
    SERVE_ENDED_RETENTION,
    SERVE_ENDED_SUFFIX,
    list_serve_ended,
    list_serve_instances,
    prune_serve_ended,
    prune_stale_serve_instances,
    read_serve_ended,
    register_serve_instance,
    serve_ended_path,
    serve_instance_path,
    serve_instances_dir,
    write_serve_ended,
)


def _sidecar(root: Path, pid: int, *, reason: str = "drained", at: str | None = None):
    return write_serve_ended(
        root,
        reason=reason,
        boot_id=f"boot{pid}",
        pid=pid,
        at=at or "2026-09-05T00:00:00.000Z",
    )


# ── 1. the record ───────────────────────────────────────────────────────────


def test_the_sidecar_carries_exactly_the_four_ruled_keys(tmp_path: Path) -> None:
    """RL-16 names four keys; a fifth is a contract change nobody agreed to.

    *Killing mutation:* add any key to the record and this fails.
    """

    assert _sidecar(tmp_path, 4242, reason="ctrl_close", at="2026-09-05T12:00:00.000Z")
    path = serve_ended_path(tmp_path, 4242)
    assert path.name == f"4242{SERVE_ENDED_SUFFIX}"
    record = json.loads(path.read_bytes())
    assert record == {
        "reason": "ctrl_close",
        "at": "2026-09-05T12:00:00.000Z",
        "boot_id": "boot4242",
        "pid": 4242,
    }


def test_the_sidecar_is_written_lf_canonical_like_every_other_store_record(
    tmp_path: Path,
) -> None:
    """Same writer as the registry row, which on Windows means no CRLF.

    ``write_json_atomic`` pins ``newline="\\n"`` precisely because these files
    are read back and compared as bytes; a second writer that used Python's
    default text mode would put CRLF in this one file and nowhere else.
    """

    _sidecar(tmp_path, 4242)
    raw = serve_ended_path(tmp_path, 4242).read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")


def test_removing_the_registry_row_leaves_the_sidecar(tmp_path: Path) -> None:
    """The whole point: the row goes on a clean exit, the reason stays.

    *Killing mutation:* name the sidecar ``<pid>.json`` and the row's own
    removal deletes the reason it was written to preserve.
    """

    from agent_runtime.serve_registry import unregister_serve_instance

    register_serve_instance(tmp_path, pid=os.getpid(), boot_id="b1")
    _sidecar(tmp_path, os.getpid(), reason="drained")
    assert unregister_serve_instance(tmp_path, pid=os.getpid()) is True
    assert not serve_instance_path(tmp_path, os.getpid()).exists()
    assert read_serve_ended(tmp_path, os.getpid()) == {
        "reason": "drained",
        "at": "2026-09-05T00:00:00.000Z",
        "boot_id": f"boot{os.getpid()}",
        "pid": os.getpid(),
    }


def test_reading_a_sidecar_that_was_never_written_is_none_not_an_error(
    tmp_path: Path,
) -> None:
    """Absence is the reading for a ``TerminateProcess`` — it must not raise."""

    assert read_serve_ended(tmp_path, 999999) is None
    serve_instances_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    (serve_instances_dir(tmp_path) / f"55{SERVE_ENDED_SUFFIX}").write_bytes(b"{not json")
    assert read_serve_ended(tmp_path, 55) is None


def test_the_write_never_raises_and_reports_what_it_did(tmp_path: Path) -> None:
    """A runtime on its way down must not fail on bookkeeping."""

    blocked = tmp_path / "blocked"
    blocked.write_text("i am a file, not a directory\n", encoding="utf-8")
    assert write_serve_ended(blocked, reason="drained", boot_id="b", pid=1) is False


# ── retention ───────────────────────────────────────────────────────────────


def test_retention_keeps_the_newest_twenty_sidecars(tmp_path: Path) -> None:
    """A long-lived machine must not accumulate thousands.

    *Killing mutation:* drop the prune and this directory keeps all 25.
    """

    assert SERVE_ENDED_RETENTION == 20
    for index in range(25):
        _sidecar(tmp_path, 1000 + index, at=f"2026-09-05T00:00:{index:02d}.000Z")
    report = prune_serve_ended(tmp_path)
    assert report["deleted_count"] == 5
    survivors = {row["pid"] for row in list_serve_ended(tmp_path)}
    assert survivors == set(range(1005, 1025))


def test_retention_orders_by_at_then_filename_not_by_mtime(tmp_path: Path) -> None:
    """``at`` is the record's own clock; mtime is the filesystem's guess.

    Written newest-first on disk, so an mtime-ordered prune would delete the
    wrong two.
    """

    for index in reversed(range(4)):
        _sidecar(tmp_path, 2000 + index, at=f"2026-09-05T00:00:{index:02d}.000Z")
    report = prune_serve_ended(tmp_path, keep=2)
    assert report["deleted_count"] == 2
    assert {row["pid"] for row in list_serve_ended(tmp_path)} == {2002, 2003}


def test_retention_never_touches_a_registry_row(tmp_path: Path) -> None:
    """Two file shapes, one directory: the prune that bounds one must be blind
    to the other, or a boot's own tidying deletes the live service's row."""

    register_serve_instance(tmp_path, pid=777, boot_id="live")
    for index in range(25):
        _sidecar(tmp_path, 3000 + index, at=f"2026-09-05T00:00:{index:02d}.000Z")
    prune_serve_ended(tmp_path, keep=1)
    assert serve_instance_path(tmp_path, 777).exists()


def test_a_sidecar_with_an_unreadable_at_sorts_oldest_and_is_not_a_crash(
    tmp_path: Path,
) -> None:
    """Hand-edited, half-written, or from a future shape: still prunable."""

    serve_instances_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    (serve_instances_dir(tmp_path) / f"10{SERVE_ENDED_SUFFIX}").write_bytes(b"{ nope")
    _sidecar(tmp_path, 11, at="2026-09-05T00:00:01.000Z")
    _sidecar(tmp_path, 12, at="2026-09-05T00:00:02.000Z")
    report = prune_serve_ended(tmp_path, keep=2)
    assert report["deleted_count"] == 1
    assert {row["pid"] for row in list_serve_ended(tmp_path)} == {11, 12}


# ── 2. one test per reader of serve_instances/ ──────────────────────────────


def test_the_registry_lister_ignores_the_sidecar(tmp_path: Path) -> None:
    """Reader 1: ``list_serve_instances``, the one scan every other reader is
    built on.

    *Killing mutation:* restore the bare ``glob("*.json")`` and this returns two
    rows, the second an ``unknown``/``record_unreadable``-shaped ghost with no
    pid.
    """

    register_serve_instance(tmp_path, pid=os.getpid(), boot_id="b1")
    _sidecar(tmp_path, 4242, reason="ctrl_close")
    rows = list_serve_instances(tmp_path)
    assert [row["pid"] for row in rows] == [os.getpid()]


def test_the_registry_lister_ignores_a_directory_of_nothing_but_sidecars(
    tmp_path: Path,
) -> None:
    """The state after a machine's last runtime died: no rows, several reasons."""

    for pid in (11, 22, 33):
        _sidecar(tmp_path, pid)
    assert list_serve_instances(tmp_path) == []


def test_socket_target_resolution_ignores_the_sidecar(tmp_path: Path) -> None:
    """Reader 2: ``resolve_socket_target`` — the lane ``harness serve connect``
    and the launcher's ``local_serve_attach`` both go through.

    A sidecar read as a row is a row with no port and no classification, and
    ``allow_stale=True`` (the diagnostic path the connect verb uses to NAME what
    it is refusing) is exactly where a portless ghost would surface.
    """

    from agent_runtime.serve_socket import resolve_socket_target

    for pid in (11, 22):
        _sidecar(tmp_path, pid)
    assert resolve_socket_target(tmp_path) is None
    assert resolve_socket_target(tmp_path, allow_stale=True) is None


def test_the_boot_prune_ignores_the_sidecar(tmp_path: Path) -> None:
    """Reader 3: ``prune_stale_serve_instances``, which runs on every boot.

    It deletes only rows it has classified ``stale_dead_pid``; a sidecar read as
    a row would be pid-less and classify ``unknown``, which is KEPT — so the
    visible symptom is not deletion but an ``unknown`` record in the boot's
    prune report for every runtime that ever ended on this machine.
    """

    _sidecar(tmp_path, 4242)
    report = prune_stale_serve_instances(tmp_path)
    assert report == {"deleted": [], "kept": [], "deleted_count": 0}
    assert read_serve_ended(tmp_path, 4242) is not None


def test_harness_status_ignores_the_sidecar(
    monkeypatch, capsys, isolate_agent_runtime_root
) -> None:
    """Reader 4: ``_attach_runtime_service_blocks``, which is what
    ``harness status`` (human and ``--json``) counts its ``serves=`` from.

    Driven through the real verb because that block reads ``paths.store_root()``
    itself. Three sidecars and no rows is the state of a machine whose last
    three runtimes ended: the honest reading is ``serves=0/0``.
    """

    import argparse

    from hermes_cli.harness import build_parser

    for pid in (11, 22, 33):
        _sidecar(isolate_agent_runtime_root, pid)
    monkeypatch.setattr(
        "hermes_cli.harness.build_status",
        lambda: {
            "open_incidents": 0,
            "dirty_summary": "runtime=clean",
            "runtime_health": {"ok": True},
        },
    )
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["harness", "status"])

    assert args.func(args) == 0
    assert capsys.readouterr().out.strip().endswith("serves=0/0")


# ── the handler mapping tables ──────────────────────────────────────────────


def test_the_console_control_mapping_table_is_the_closed_vocabulary() -> None:
    """``CTRL_CLOSE_EVENT`` cannot be generated from a test — nothing in the
    Windows API sends one, it is what the OS sends when a console window's X is
    clicked — so the mapping is pinned here instead, and the child e2e proves
    the handler is installed and that the ones that CAN be generated arrive.

    *Killing mutation:* change any value and this fails; the five numbers are
    ``wincon.h``'s and cannot drift.
    """

    from hermes_cli.harness_parts.serve import CONSOLE_CTRL_END_REASONS

    assert CONSOLE_CTRL_END_REASONS == {
        0: "ctrl_c",  # CTRL_C_EVENT
        1: "ctrl_c",  # CTRL_BREAK_EVENT — the operator interrupted it
        2: "ctrl_close",  # CTRL_CLOSE_EVENT — the console window's X
        5: "logoff",  # CTRL_LOGOFF_EVENT
        6: "logoff",  # CTRL_SHUTDOWN_EVENT
    }


def test_the_signal_mapping_table_is_the_closed_vocabulary() -> None:
    from hermes_cli.harness_parts.serve import SIGNAL_END_REASONS

    assert SIGNAL_END_REASONS == {"SIGTERM": "sigterm", "SIGHUP": "logoff"}


def test_every_word_the_runtime_can_write_is_in_the_ruled_vocabulary() -> None:
    """The union of both tables plus the paths' own words, held closed."""

    from hermes_cli.harness_parts.serve import (
        CONSOLE_CTRL_END_REASONS,
        END_REASON_VOCABULARY,
        SIGNAL_END_REASONS,
    )

    assert END_REASON_VOCABULARY == frozenset(
        {
            "drained",
            "shutdown_op",
            "stdin_eof",
            "ctrl_close",
            "ctrl_c",
            "sigterm",
            "logoff",
            "unknown_exit",
        }
    )
    assert set(CONSOLE_CTRL_END_REASONS.values()) <= END_REASON_VOCABULARY
    assert set(SIGNAL_END_REASONS.values()) <= END_REASON_VOCABULARY


# ── the recorder's own behaviour ────────────────────────────────────────────


def _recorder(root: Path, pid: int = 4242):
    from hermes_cli.harness_parts.serve import _ServeEndReason

    return _ServeEndReason(root, boot_id="deadbeef", pid=pid)


def test_the_first_reason_wins(tmp_path: Path) -> None:
    """A CTRL_CLOSE that arrives while the drain is finishing must not be
    overwritten by the drain's own word, and vice versa: whichever cause got
    there first is the cause."""

    recorder = _recorder(tmp_path)
    recorder.note("ctrl_close")
    recorder.note("drained")
    recorder.write()
    assert read_serve_ended(tmp_path, 4242)["reason"] == "ctrl_close"


def test_the_record_is_written_once(tmp_path: Path) -> None:
    """``atexit`` runs after the drain path has already written; a second write
    would restamp ``at`` with a time the process was already dead at."""

    recorder = _recorder(tmp_path)
    assert recorder.write("drained") is True
    first = read_serve_ended(tmp_path, 4242)
    assert recorder.write("unknown_exit") is False
    assert read_serve_ended(tmp_path, 4242) == first


def test_nothing_latched_writes_unknown_exit(tmp_path: Path) -> None:
    """The ``atexit`` fallback. Not a failure — a runtime that ended by a route
    nobody taught this recorder about, said plainly."""

    recorder = _recorder(tmp_path)
    recorder.write()
    assert read_serve_ended(tmp_path, 4242)["reason"] == "unknown_exit"


def test_an_unknown_word_is_refused_rather_than_written(tmp_path: Path) -> None:
    """The vocabulary is closed so a launcher can switch on it. A caller that
    invents a word gets the fallback, not a new word on the operator's sheet."""

    recorder = _recorder(tmp_path)
    recorder.note("exploded")
    recorder.write()
    assert read_serve_ended(tmp_path, 4242)["reason"] == "unknown_exit"


def test_uncaught_carries_the_exception_type(tmp_path: Path) -> None:
    """``uncaught:<Type>`` is the one open-ended word, and the type is the whole
    value of it."""

    recorder = _recorder(tmp_path)
    recorder.note("uncaught:ZeroDivisionError")
    recorder.write()
    assert read_serve_ended(tmp_path, 4242)["reason"] == "uncaught:ZeroDivisionError"


def test_a_reason_bearing_type_name_cannot_smuggle_a_path(tmp_path: Path) -> None:
    """``<pid>.ended.json`` is derived from the pid, never from the reason —
    but the reason is a STRING from an exception type, so pin that a hostile
    one lands in the value and never in a filename."""

    recorder = _recorder(tmp_path)
    recorder.note("uncaught:../../evil")
    recorder.write()
    record = read_serve_ended(tmp_path, 4242)
    assert record["reason"] == "unknown_exit"


def test_the_recorder_never_raises_when_the_root_is_gone(tmp_path: Path) -> None:
    """Called from an ``atexit`` hook and from an OS-owned console handler
    thread, both of which run while the world is being taken apart."""

    recorder = _recorder(tmp_path / "does" / "not" / "exist" / "and-is-a-file")
    (tmp_path / "does").mkdir()
    (tmp_path / "does" / "not").write_text("file\n", encoding="utf-8")
    assert recorder.write("drained") is False


@pytest.mark.skipif(sys.platform != "win32", reason="console handlers are Windows-only")
def test_the_console_handler_installs_with_no_console(tmp_path: Path) -> None:
    """RL-17 puts the runtime behind ``CREATE_NO_WINDOW``; the seam-level claim
    is that installation is not conditional on a console existing. The process
    running this test HAS one, so this proves only that the call succeeds and
    reports it — the real no-console proof is the ``DETACHED_PROCESS`` arm of
    the child e2e."""

    from hermes_cli.harness_parts.serve import _install_console_ctrl_reason_handler

    handle = _install_console_ctrl_reason_handler(_recorder(tmp_path))
    assert handle is not None


def test_the_console_handler_writes_the_word_and_declines_to_handle(
    tmp_path: Path,
) -> None:
    """The handler's job is a record, not a policy: it returns FALSE so the
    default processing still runs — Python's own handler still raises
    KeyboardInterrupt on Ctrl-C, and the OS still terminates on a close.

    *Killing mutation:* return TRUE and a Ctrl-C on the operator's console
    silently stops working.
    """

    from hermes_cli.harness_parts.serve import _console_ctrl_reason_callback

    recorder = _recorder(tmp_path)
    callback = _console_ctrl_reason_callback(recorder)
    assert callback(2) == 0
    assert read_serve_ended(tmp_path, 4242)["reason"] == "ctrl_close"


def test_the_console_handler_ignores_an_event_it_has_no_word_for(
    tmp_path: Path,
) -> None:
    from hermes_cli.harness_parts.serve import _console_ctrl_reason_callback

    recorder = _recorder(tmp_path)
    assert _console_ctrl_reason_callback(recorder)(99) == 0
    assert read_serve_ended(tmp_path, 4242) is None


# ── the wiring ──────────────────────────────────────────────────────────────


def test_the_serve_entry_point_turns_the_recorder_on(tmp_path: Path) -> None:
    """The loop's default is OFF — a ``serve_loop`` unit test must never
    register an ``atexit`` hook or touch this process's signal disposition — so
    the production wiring is what makes the sidecar real.

    *Killing mutation:* drop the kwarg from ``_cmd_serve`` and every reason ships
    dead with the whole unit suite still green (the AST precedent:
    ``test_root_anchor``'s ``root_anchor`` pin).
    """

    import ast
    import inspect

    from hermes_cli.harness_parts import serve as serve_module

    tree = ast.parse(inspect.getsource(serve_module))
    body = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_cmd_serve"
    )
    kwargs = {
        keyword.arg
        for node in ast.walk(body)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "serve_loop"
        for keyword in node.keywords
    }
    assert "record_end_reason" in kwargs, (
        "_cmd_serve must pass record_end_reason=True to serve_loop; without it "
        "RL-16 ships dead"
    )


def test_the_boot_fault_seam_is_inert_without_its_environment_variable(
    monkeypatch,
) -> None:
    """The seam that lets the e2e produce a real ``uncaught:`` and a real
    ``os._exit``. Inert is the contract: no variable, no behaviour."""

    from hermes_cli.harness_parts.serve import _maybe_inject_boot_fault

    monkeypatch.delenv("HERMES_SERVE_BOOT_FAULT", raising=False)
    assert _maybe_inject_boot_fault() is None

    monkeypatch.setenv("HERMES_SERVE_BOOT_FAULT", "raise")
    with pytest.raises(RuntimeError):
        _maybe_inject_boot_fault()

    monkeypatch.setenv("HERMES_SERVE_BOOT_FAULT", "exit")
    with pytest.raises(SystemExit):
        _maybe_inject_boot_fault()
