"""The mutation gate refuses to share a worktree with a second mutating run.

Measured 2026-08-31 during the H landing: the gate rewrites source files IN
PLACE to apply each mutant, so anything else reading the tree while it runs
reads sabotaged source. Ten minutes went into "test pollution" before the
concurrency was the answer, and the phantom red — an
``is_canonical_persona_channel`` sabotage bleeding into an unrelated suite —
was indistinguishable at the console from a real defect.

A concurrent *pytest* is not something this script can see, so that half is
written down (module docstring, ``--help``, `tool/test_quality/README.md`). A
concurrent *mutating run* it can and does refuse, which is what this file pins.

The run is driven through :func:`run` with the git-and-subprocess ends stubbed
and ``REPO_ROOT`` pointed at a temp tree, because the guarantee is about the
LOCK's lifecycle across a whole run — taken before the baseline, released on
every exit — and a unit test of ``_acquire_gate_lock`` alone would leave exactly
the join that was broken untested.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts import changed_line_mutation_check as module


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    """The gate module, re-rooted onto a temp tree with one production file and
    one claim that mutates it. Every command it would run is recorded rather
    than executed.

    The module is imported as a package member — ``scripts/`` is a namespace
    package and the gate's own tests under ``tests/scripts/`` import it this
    way. Loading it by path instead (``spec_from_file_location`` +
    ``exec_module``) works only if the module is registered in ``sys.modules``
    FIRST: ``dataclasses`` resolves a string annotation through
    ``sys.modules[cls.__module__]``, so a module executed outside the table
    raises ``AttributeError: 'NoneType' object has no attribute '__dict__'`` on
    its own ``ClaimAnchor``.
    """

    target = tmp_path / "prod.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")

    claim = {
        "id": "stub-claim",
        "path": "prod.py",
        "symbol": "module",
        "operator": "flip-the-constant",
        "find": "VALUE = 1",
        "replace": "VALUE = 2",
        "test": ["stub-test"],
        module.ANCHOR_KEY: module.ClaimAnchor(
            offset=0, lines={1}, find="VALUE = 1", replace="VALUE = 2", shift=0
        ),
    }

    ran: list[list[str]] = []

    def _stub_run(command):
        ran.append(list(command))
        # Non-zero throughout: the FIRST call is the baseline, so this default
        # stub reaches "baseline failed" and stops. Tests that need to get past
        # it re-stub with :func:`_baseline_then_killed`.
        return 1

    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "LOCK_PATH", tmp_path / ".mutation_gate.lock")
    monkeypatch.setattr(module, "_validate_exemptions", lambda path: None)
    monkeypatch.setattr(module, "_partition_claims", lambda base, path: ([claim], []))
    # The unregistered-source census is a real `git diff` against the base, and
    # this file's base is the string "BASE" over a temp tree with no repository
    # in it. Stubbed for the same reason `_partition_claims` is: what these
    # tests are about is the LOCK, not what the diff contained.
    monkeypatch.setattr(module, "_changed_sources", lambda base: [])
    monkeypatch.setattr(module, "_command", lambda claim: ["stub-test"])
    monkeypatch.setattr(module, "_run_command", _stub_run)

    module._stub_runs = ran
    module._stub_target = target
    return module


def _baseline_then_killed(gate, monkeypatch):
    """Baseline green, mutant red — the gate's own success path, by call order:
    the first command a run issues is the baseline, every later one is a
    mutant's."""

    calls: list[list[str]] = []

    def _run_command(command):
        calls.append(list(command))
        return 0 if len(calls) == 1 else 1

    monkeypatch.setattr(gate, "_run_command", _run_command)
    return calls


def _run(gate, **kwargs) -> int:
    return gate.run(
        kwargs.get("base", "BASE"),
        Path("claims.json"),
        Path("exemptions.yaml"),
        kwargs.get("max_candidates", 40),
        kwargs.get("list_only", False),
    )


def test_a_held_lock_refuses_the_run_and_names_the_file_to_delete(gate, capsys):
    """The refusal is the whole feature: a second run must stop before it can
    splice a mutant into a tree the first run is already rewriting."""

    gate.LOCK_PATH.write_text("pid: 4242\nstarted: 2026-08-31T00:00:00+00:00\n", encoding="utf-8")

    assert _run(gate) == 2

    stderr = capsys.readouterr().err
    assert str(gate.LOCK_PATH) in stderr
    assert "pid: 4242" in stderr
    # Copy-pasteable: the holder's details arrive fenced, not inline in prose.
    assert stderr.count("```") == 2
    # And nothing ran — not even the baseline, which reads the tree too.
    assert gate._stub_runs == []


def test_the_lock_is_released_after_a_green_run(gate, monkeypatch):
    """Released on the way out, so a finished run does not obstruct the next
    one. This is the ordinary green exit: baseline passes, the mutant turns its
    claimed test red, exit 0."""

    calls = _baseline_then_killed(gate, monkeypatch)

    assert _run(gate) == 0
    assert len(calls) == 2, calls
    assert not gate.LOCK_PATH.exists()
    # And the tree is back: the mutant was restored, so the next reader of this
    # worktree sees the committed bytes.
    assert gate._stub_target.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_the_lock_is_released_when_the_baseline_refuses(gate):
    """The other exit that leaves early — a failed baseline returns 2 from
    inside the locked block."""

    assert _run(gate) == 2
    assert not gate.LOCK_PATH.exists()


def test_the_lock_is_released_even_when_the_run_raises(gate, monkeypatch):
    """A crash inside the mutate loop must not leave the worktree locked — a
    lock that outlives its run turns the guard into the obstruction it exists
    to prevent. There is no liveness probe to clean up after it."""

    monkeypatch.setattr(gate, "_run_command", lambda command: 0)
    monkeypatch.setattr(gate, "_read_source", lambda target: (_ for _ in ()).throw(OSError("boom")))

    with pytest.raises(OSError):
        _run(gate)

    assert not gate.LOCK_PATH.exists()


def test_the_lock_is_taken_before_the_baseline_and_holds_across_it(gate, monkeypatch):
    """The baseline runs do not mutate, but they READ the tree and belong to the
    same run — a second run's mutants would corrupt them exactly as they corrupt
    the mutation runs. So the lock is held from before the first baseline."""

    seen: list[bool] = []

    def _record(command):
        seen.append(gate.LOCK_PATH.exists())
        return 1

    monkeypatch.setattr(gate, "_run_command", _record)

    _run(gate)

    assert seen and all(seen), "the baseline ran outside the lock"


def test_a_list_only_run_never_touches_the_lock(gate):
    """``--list`` is an inventory question that rewrites nothing, so locking it
    would refuse a harmless read for a reason that does not apply to it."""

    gate.LOCK_PATH.write_text("pid: 4242\n", encoding="utf-8")

    assert _run(gate, list_only=True) == 0
    assert gate.LOCK_PATH.read_text(encoding="utf-8") == "pid: 4242\n"


def test_the_lock_records_who_holds_it(gate):
    """The refusal can only name the holder if the holder wrote itself down."""

    assert gate._acquire_gate_lock() is True
    try:
        held = gate.LOCK_PATH.read_text(encoding="utf-8")
        assert f"pid: {os.getpid()}" in held
        assert "started: " in held
        # And the second attempt loses, atomically.
        assert gate._acquire_gate_lock() is False
    finally:
        gate.LOCK_PATH.unlink(missing_ok=True)


# ── the cap refusal names its numbers ────────────────────────────────────────


def test_the_cap_refusal_names_the_count_the_cap_and_the_landing_cure(gate, capsys):
    """An exit 2 that means "your change is big" used to read as "your claims
    are bad" — the wrong signal on the wrong lane, measured on the H1-H4
    landing, which selected 30 against a default of 12."""

    assert _run(gate, max_candidates=0) == 2

    stderr = capsys.readouterr().err
    assert "1 selected > --max-candidates 0" in stderr
    assert "--max-candidates 40" in stderr
    assert gate._stub_runs == []


def test_the_cap_refusal_precedes_the_lock(gate):
    """A refused run holds nothing: the cap is checked before the lock is taken,
    so a capped-out run cannot leave a lock behind for the split-up runs that
    follow it."""

    assert _run(gate, max_candidates=0) == 2
    assert not gate.LOCK_PATH.exists()
