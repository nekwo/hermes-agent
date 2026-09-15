"""H2 / MCF-27 — a led build CLOSES the chat SessionDB it opened.

WHAT THIS FILE EXISTS TO STOP
=============================

``_build_snapshot_in_runtime_scope`` used to bind
``session_db = _default_persona_session_db()`` into a local and drop it, and
``status.build_status`` did the same. Each call therefore left one live SQLite
connection behind for the life of the process, and a serve does many builds: one
per led rebuild, one per ``forceFresh`` gesture, one per hydrate.

That is not merely untidy, which is why it is gated rather than tidied. SQLite
keeps the ``-wal`` sibling on disk for exactly as long as a connection is open
and unlinks it when the last one closes. So the leak is what kept the chat WAL
PRESENT mid-session — and the read-model cache's fingerprint stats that file.
Which build in a process wrote the sidecar last therefore decided whether the
NEXT boot could hit the cache at all (the ledger's MCF-15 / MCF-27 pair).
Ownership of the handle is a cache-correctness property, not housekeeping.

WHAT MAKES THESE NON-VACUOUS
============================

Nothing here asserts an elapsed time, and nothing infers "closed" from a side
effect that a garbage collector could also produce. The seam is instrumented at
the ACQUISITION — the same function the production scope calls — and the gates
COUNT acquisitions against closes on the real handle the build used. A build that
opens nothing would fail the acquisition assertion before reaching the close one,
so "0 == 0" can never read as a pass.
"""

from __future__ import annotations

import logging

import pytest

from agent_runtime import snapshot as snapshot_mod
from agent_runtime import status as status_mod


class _Ledger:
    """Acquisitions and closes of the projection's chat SessionDB, counted."""

    def __init__(self) -> None:
        self.opened: list[object] = []
        self.closed: list[object] = []

    @property
    def open_handles(self) -> int:
        return len(self.opened) - len(self.closed)


@pytest.fixture
def session_db_ledger(monkeypatch) -> _Ledger:
    """Instrument the REAL acquisition, and the REAL handle it returns.

    The wrapper delegates to production's own ``_default_persona_session_db``, so
    the object under test is the SessionDB the build actually uses — not a stand
    in that could tolerate a close the real one would not. Only ``close`` is
    shadowed, per instance, and it still calls through.
    """

    ledger = _Ledger()
    real_acquire = snapshot_mod._default_persona_session_db

    def counting_acquire():
        handle = real_acquire()
        ledger.opened.append(handle)
        if handle is None:
            return None
        real_close = handle.close

        def counting_close():
            ledger.closed.append(handle)
            return real_close()

        handle.close = counting_close
        return handle

    monkeypatch.setattr(snapshot_mod, "_default_persona_session_db", counting_acquire)
    return ledger


def _assert_one_real_acquisition(ledger: _Ledger, *, lane: str) -> None:
    assert len(ledger.opened) == 1, (
        f"the {lane} build acquired the chat SessionDB {len(ledger.opened)} "
        "times, not once, so the close count below is not about a single owned "
        "handle"
    )
    assert ledger.opened[0] is not None, (
        f"the {lane} build's acquisition answered None in this fixture, so a "
        "close count of zero would be correct and this case would prove nothing "
        "about ownership"
    )


def test_a_led_snapshot_build_closes_the_session_db_it_opened(session_db_ledger):
    """The build's own frame owns the handle for the whole build and releases it.

    Driven through ``_build_snapshot_uncoalesced`` rather than ``build_snapshot``
    deliberately: the ownership scope lives on that frame, and going through the
    cache lane would let a cache HIT satisfy this case without a build happening
    at all.

    *Kill:* delete the ``session_db.close()`` arm from
    ``snapshot.persona_session_db_scope``. One connection is acquired and none
    released, and this reds on the close count.
    """

    data = snapshot_mod._build_snapshot_uncoalesced()
    assert isinstance(data, dict) and data.get("schema_version"), (
        "the build did not return a snapshot, so the counts below describe a "
        "failed build rather than an owned handle"
    )

    _assert_one_real_acquisition(session_db_ledger, lane="snapshot")
    assert len(session_db_ledger.closed) == 1, (
        "the snapshot build opened the chat SessionDB and closed it "
        f"{len(session_db_ledger.closed)} times. Every led build then leaks one "
        "live SQLite connection for the life of the serve, which holds the chat "
        "-wal on disk mid-session — and the read-model cache's fingerprint stats "
        "that file, so which build wrote the sidecar last decides whether the "
        "next boot can hit the cache (MCF-15/MCF-27)."
    )
    assert session_db_ledger.open_handles == 0, (
        "the build ended with a live chat SessionDB handle it opened"
    )


def test_a_status_build_closes_the_session_db_it_opened(session_db_ledger):
    """The SECOND call site, with its own assertion and its own kill.

    ``status.build_status`` shares the acquisition with the snapshot build, which
    is why ownership became a scope rather than a close bolted onto one of them —
    and precisely why it needs its own case: a scope used at one site and
    forgotten at the other is the shape a single gate would miss.

    *Kill:* revert ``status.build_status`` to binding
    ``_default_persona_session_db()`` in ``_build_status_in_runtime_scope``
    without a scope. This reds; the snapshot case above stays green.
    """

    data = status_mod.build_status()
    assert isinstance(data, dict), "build_status did not return a status payload"

    _assert_one_real_acquisition(session_db_ledger, lane="status")
    assert len(session_db_ledger.closed) == 1, (
        "the status build opened the chat SessionDB and closed it "
        f"{len(session_db_ledger.closed)} times. `harness status` is spawned "
        "fresh per call so the process exit hides it there, but the same helper "
        "is what the serve's own status lane uses."
    )
    assert session_db_ledger.open_handles == 0, (
        "the status build ended with a live chat SessionDB handle it opened"
    )


#: The release path's own diagnostic. Both cases below key on it rather than on
#: whether the build raised, and that is not decoration — it is what stops them
#: being vacuous. The release is wrapped in ``except Exception`` (a failed
#: connection release must not cost a frame that is already computed), so an
#: unconditional ``.close()`` on the contractual ``None`` raises ``AttributeError``
#: INSIDE that guard and the build still returns. "The build completed" is
#: therefore true on the defect as well as on the fix, and a case asserting only
#: that would pass either way. The log record is the observable that differs.
_RELEASE_FAILURE_MARKER = "closing the projection chat SessionDB failed"


def _release_failures(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if _RELEASE_FAILURE_MARKER in record.getMessage()
    ]


def test_a_build_completes_and_releases_nothing_when_the_acquisition_answers_None(
    monkeypatch, caplog
):
    """``open_chat_session_db`` returns None BY CONTRACT, and the release must cope.

    Its docstring is explicit that an unavailable database answers ``None``, and
    every consumer of the handle types it ``Any | None``. So the None arm is an
    ordinary, already-handled state, not an error path — and a release written as
    ``contextlib.closing(...)`` over a bare ``None``, or as an unconditional
    ``.close()``, tries to release a handle that was never acquired on exactly the
    stores that are already degraded.

    *Kill:* drop the ``if session_db is not None`` guard in
    ``snapshot.persona_session_db_scope``. The exit calls ``None.close()``, the
    surrounding ``except Exception`` catches the ``AttributeError`` and LOGS it,
    and the second assertion reds. The first assertion stays green under that
    mutant, which is precisely why it cannot be the only one here.
    """

    monkeypatch.setattr(snapshot_mod, "_default_persona_session_db", lambda: None)

    with caplog.at_level(logging.DEBUG, logger=snapshot_mod.__name__):
        data = snapshot_mod._build_snapshot_uncoalesced()

    assert isinstance(data, dict) and data.get("schema_version"), (
        "a build whose chat SessionDB was unavailable did not complete, so an "
        "unreachable chat store now takes the whole projection down with it"
    )
    assert _release_failures(caplog) == [], (
        "the scope tried to RELEASE a handle it never acquired — "
        "``open_chat_session_db`` answers None by contract, and the release path "
        "is now reporting a failure on every degraded store, once per build"
    )


def test_the_release_survives_a_handle_whose_close_raises(monkeypatch, caplog):
    """A release that raises must not fail the build that already succeeded.

    The snapshot is fully computed by the time the scope exits, so turning a
    failed connection release into a failed build would trade a leaked handle for
    a lost frame — the worse of the two. The second assertion is what keeps the
    first from licensing a silent swallow: the failure is REPORTED, so a
    connection that will not close is diagnosable rather than invisible.

    *Kill:* remove the ``try``/``except`` around ``session_db.close()``. The
    ``RuntimeError`` propagates out of the build and this reds, while every other
    case here stays green.
    """

    class _RefusesToClose:
        def close(self):
            raise RuntimeError("connection release refused")

        def __getattr__(self, name):
            # Every projection read of this handle is already exception-guarded;
            # refusing them keeps the case about the RELEASE and nothing else.
            raise AttributeError(name)

    monkeypatch.setattr(
        snapshot_mod, "_default_persona_session_db", lambda: _RefusesToClose()
    )

    with caplog.at_level(logging.DEBUG, logger=snapshot_mod.__name__):
        data = snapshot_mod._build_snapshot_uncoalesced()

    assert isinstance(data, dict) and data.get("schema_version"), (
        "a chat SessionDB that refused to close failed the whole build, so a "
        "release problem now costs the frame as well as the connection"
    )
    assert len(_release_failures(caplog)) == 1, (
        "the refused release was not reported once, so a connection the runtime "
        f"cannot close is invisible (records: {_release_failures(caplog)})"
    )
