"""HC-1/HC-2 — WHEN the fingerprint home is captured, pinned to a named instant.

WHAT THIS FILE EXISTS TO STOP
=============================

``core_cache.resolved_fingerprint_home`` is capture-once-then-frozen, and it used
to be LAZY: the capture instant was whichever build or consult happened to be
first, which is not a point in the process lifecycle at all. A persona turn
installs a context-local home override (``persona_profile_context``), so on the
losing side of that race the home a whole process fingerprints through is the
home of whatever persona was mid-turn.

That is not a hypothesis. ``reason=home_mismatch`` fired on the operator's
SINGLE-PROFILE install twice — 2026-08-21 16:04:32 and 2026-08-22 13:36 — each
time one boot, three callers, all demoted for the same reason, each followed by a
cold ~7.6s build. On a one-profile install there is no second root for two runs
to legitimately disagree about, so that reason is a producer defect and the
runtime's own census rule (MC-2) already says so.

Its sibling file ``test_core_cache_home_closure.py`` pins WHICH home the closure
resolves through once captured, and the residual it names in
``test_a_capture_taken_during_a_persona_scope_pins_that_scopes_home`` is exactly
the state this file narrows: a process that declares a boot instant no longer has
a first-use race to lose.

WHAT MAKES THESE NON-VACUOUS
============================

The mutant is a test that "proves" the capture happened by asking a function
whose answer is "it has now" — every observation below goes through
``fingerprint_home_capture()``, which reports the capture WITHOUT taking one, and
the serve cases assert the state at TWO instants (as the ``booting`` frame is
written, and as ``ready`` is) so the capture is pinned BETWEEN them rather than
merely "sometime during boot". A capture that drifted to first-use would still
satisfy a one-sided "it is captured by the end of boot" assertion, because the
boot's own store reads would have taken it.
"""

from __future__ import annotations

import io
import json
import logging
from types import SimpleNamespace

import pytest

from agent_runtime import core_cache
from hermes_cli.harness_parts.serve import FINGERPRINT_HOME_BOOT_SITE, serve_loop


SHUTDOWN = json.dumps({"op": "shutdown"}) + "\n"


def _warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(
            f"snapshot_core_cache {core_cache.RECEIPT_FINGERPRINT_HOME_LAZY_CAPTURE}"
        )
    ]


def _fields(line: str) -> set[str]:
    """The line's whitespace-separated ``key=value`` fields.

    Fields, not substrings — the same rule the demote pins use, and for the same
    reason: ``site=serve_loop_typo`` still contains ``site=serve_loop``.
    """

    return set(line.split())


# --------------------------------------------------------------------------- #
# 0. Anti-vacuity — the lazy branch is REACHABLE, and it is loud
# --------------------------------------------------------------------------- #
def test_a_lazy_capture_in_a_process_that_declared_an_instant_says_so(caplog):
    """Everything below asserts an ABSENCE (no lazy receipt during boot). This
    case is what makes that absence evidence: the receipt fires, on this logger,
    with this family and this token, whenever a declared instant is skipped."""

    core_cache.declare_fingerprint_home_boot_site("probe:declared_but_never_captured")

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        core_cache.resolved_fingerprint_home()

    lines = _warnings(caplog)
    assert len(lines) == 1, lines
    fields = _fields(lines[0])
    assert "site=probe:declared_but_never_captured" in fields, lines[0]
    assert any(field.startswith("home=") for field in fields), lines[0]
    assert any(
        field in ("authoritative=true", "authoritative=false") for field in fields
    ), lines[0]
    assert core_cache.fingerprint_home_capture().eager is False


def test_a_process_that_declared_nothing_is_not_accused_of_anything(caplog):
    """An ordinary short-lived caller has no boot sequence and no instant to
    miss. If the receipt fired for it, every tool invocation in the fleet would
    report the defect and the count would mean nothing."""

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        core_cache.resolved_fingerprint_home()

    assert _warnings(caplog) == []


def test_the_eager_capture_is_what_silences_it(caplog):
    """The pair to case 0, so the receipt is driven to BOTH states: a constant
    ``warning()`` and a constant ``pass`` each satisfy exactly one of them."""

    core_cache.declare_fingerprint_home_boot_site("probe:declared_and_captured")
    core_cache.capture_fingerprint_home()

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        core_cache.resolved_fingerprint_home()

    assert _warnings(caplog) == []
    state = core_cache.fingerprint_home_capture()
    assert state.eager is True
    assert state.boot_site == "probe:declared_and_captured"


# --------------------------------------------------------------------------- #
# 1. The serve captures at the NAMED instant — gate 2
# --------------------------------------------------------------------------- #
class _StateAtEachFrame:
    """Records the capture state at the moment each boot frame is written.

    The writer is the only place a test can stand INSIDE the boot sequence
    without reaching into it: ``_FrameWriter`` emits one frame per ``write``, so
    a frame arriving here means every statement before its emit has run and no
    statement after it has.
    """

    def __init__(self) -> None:
        self.out = io.StringIO()
        self.at: dict[str, core_cache.FingerprintHomeCapture] = {}

    def write(self, payload: str):
        for event in ("booting", "ready"):
            if f'"event": "{event}"' in payload and event not in self.at:
                self.at[event] = core_cache.fingerprint_home_capture()
        return self.out.write(payload)

    def flush(self):
        return self.out.flush()


def test_the_serve_captures_the_fingerprint_home_at_the_named_boot_instant(caplog):
    """GATE 2. The capture instant is a point in the boot sequence, asserted from
    both sides of it.

    ``booting`` is emitted before any heavy boot work; the capture is the first
    statement after it. So at the ``booting`` write there is nothing captured, and
    at the ``ready`` write there is a capture that names itself EAGER and names
    this site. A regression to lazy first-use fails the second half — either with
    no capture at all (nothing in the boot happened to fingerprint) or with
    ``eager is False`` (something did).
    """

    watcher = _StateAtEachFrame()

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        assert serve_loop(iter([SHUTDOWN]), watcher, dispatch=lambda argv: 0) == 0

    assert set(watcher.at) == {"booting", "ready"}, watcher.at
    assert watcher.at["booting"].home is None, (
        "the fingerprint home was already captured when the booting frame was "
        "written — the capture instant moved EARLIER than the frame that is "
        f"supposed to precede it: {watcher.at['booting']}"
    )
    ready = watcher.at["ready"]
    assert ready.home is not None, (
        "the serve reached its ready frame with no fingerprint home captured. "
        "The eager capture at the named boot instant is gone, so the next build "
        "or consult will take it — on whatever thread wins, under whatever "
        "persona scope is live there. That is the home_mismatch defect."
    )
    assert ready.eager is True, (
        "the home was captured LAZILY during boot rather than at the declared "
        f"instant: {ready}"
    )
    assert ready.boot_site == FINGERPRINT_HOME_BOOT_SITE, ready
    assert _warnings(caplog) == [], _warnings(caplog)


def test_the_capture_precedes_the_prewarm_that_builds_the_read_model(caplog):
    """The prewarm's first act is a full read-model build, i.e. the largest
    fingerprint this process will ever take. Observed from inside the injected
    prewarm rather than after ``serve_loop`` returns, because "captured by the
    time the loop exits" is satisfied by the build itself capturing."""

    seen: list[core_cache.FingerprintHomeCapture] = []

    def prewarm() -> None:
        seen.append(core_cache.fingerprint_home_capture())

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        assert (
            serve_loop(
                iter([SHUTDOWN]),
                io.StringIO(),
                dispatch=lambda argv: 0,
                snapshot_prewarm=prewarm,
            )
            == 0
        )

    assert seen, "the injected prewarm never ran, so this case measured nothing"
    assert seen[0].eager is True and seen[0].home is not None, seen[0]
    assert seen[0].boot_site == FINGERPRINT_HOME_BOOT_SITE, seen[0]
    assert _warnings(caplog) == []


def test_the_serve_captures_the_home_the_process_resolved(two_profiles_for_capture):
    """Non-vacuity for the capture itself: it must record the process's OWN head
    home, not merely record something. Driven on a root whose two profiles are
    distinguishable, so a capture that read the wrong authority is visible."""

    watcher = _StateAtEachFrame()
    assert serve_loop(iter([SHUTDOWN]), watcher, dispatch=lambda argv: 0) == 0

    assert watcher.at["ready"].home == two_profiles_for_capture.head, watcher.at


@pytest.fixture
def two_profiles_for_capture(tmp_path, monkeypatch):
    """A two-profile root with an EXPLICIT head, which is the launcher's layout.

    ``HERMES_HEAD_HOME`` is set here (unlike the closure file's fixture, which
    deliberately drives the weaker unauthoritative shape) because this file is
    about the operator's install: the launcher spawns serve with both variables
    pointing at the base profile.
    """

    root = tmp_path / "hermes"
    head = root / "profiles" / "head"
    other = root / "profiles" / "other"
    for home in (head, other):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("skills:\n  external_dirs: []\n", "utf-8")
    monkeypatch.setenv("HERMES_HOME", str(head))
    monkeypatch.setenv("HERMES_HEAD_HOME", str(head))
    core_cache.reset_fingerprint_home()
    yield SimpleNamespace(root=root, head=head, other=other)
    core_cache.reset_fingerprint_home()


# --------------------------------------------------------------------------- #
# 2. The one-shot CLI captures at command dispatch, same rule
# --------------------------------------------------------------------------- #
def test_the_cli_harness_dispatch_captures_before_the_command_runs():
    """A one-shot CLI is not exempt: ``hermes harness chat send`` runs a persona
    turn inside ``persona_profile_context``, and a first fingerprint taken in
    there writes a sidecar the NEXT boot demotes. The capture is taken at command
    dispatch, before the handler."""

    from hermes_cli.main import (
        _FINGERPRINT_HOME_CLI_BOOT_SITE,
        _capture_core_cache_fingerprint_home,
    )

    _capture_core_cache_fingerprint_home(SimpleNamespace(command="harness"))

    state = core_cache.fingerprint_home_capture()
    assert state.home is not None and state.eager is True, state
    assert state.boot_site == _FINGERPRINT_HOME_CLI_BOOT_SITE, state


def test_a_command_that_cannot_reach_this_lane_pays_nothing_for_it():
    """The fork is scoped on evidence: ``core_cache`` is imported by the harness
    modules and by nothing else under ``hermes_cli``. Every other command would
    pay a ~90ms import of a subtree it never touches."""

    from hermes_cli.main import _capture_core_cache_fingerprint_home

    _capture_core_cache_fingerprint_home(SimpleNamespace(command="chat"))

    assert core_cache.fingerprint_home_capture() == core_cache.FingerprintHomeCapture(
        home=None, authoritative=False, eager=False, boot_site=None
    )


# --------------------------------------------------------------------------- #
# 3. Capture-once still wins
# --------------------------------------------------------------------------- #
def test_an_eager_capture_does_not_overwrite_one_already_taken(caplog):
    """If something beat the boot instant to it, that home is what fingerprints
    have already been taken under. Replacing it here would silently move the
    closure out from under them — a worse fault than the one being fixed — so the
    eager call stands down and the lazy receipt is the record."""

    core_cache.declare_fingerprint_home_boot_site("probe:late")
    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        first = core_cache.resolved_fingerprint_home()
        second = core_cache.capture_fingerprint_home()

    assert second == first
    assert core_cache.fingerprint_home_capture().eager is False, (
        "a capture taken lazily was relabelled as eager by the late call, which "
        "erases the only evidence that the instant was missed"
    )
    assert len(_warnings(caplog)) == 1, _warnings(caplog)
