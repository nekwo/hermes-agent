"""ONE chat-database authority — the read-lane gap, as a regression test.

Live symptom (2026-07-27, confirmed twice): the Mission Control cockpit's Chat
History listed six stale sessions and NONE of the day's CLI-lane sessions.

Root cause. The persona-instance store DELIBERATELY collapses a per-profile
``HERMES_HOME`` onto the shared runtime root (``resolution._default_hermes_root``
maps ``<root>/profiles/<x>`` → ``<root>``), so every profile shares ONE
``default_chat_session_id`` per instance. The chat ``SessionDB`` those pointers
dereference did not collapse: with no head named, ``get_hermes_head_home()``
degrades to ``get_hermes_home()``. Only the Launcher names a head
(``HERMES_HEAD_HOME=<root>/profiles/base``), so a CLI-lane turn minted its
transcript into ``<root>/profiles/<active>/state.db`` while writing the binding
into the SHARED store — and the cockpit, reading the head database, dropped the
row as ``session_not_in_db``.

Neither of the two latent defects the design doc had already found
(``chat-session-presence-authority.md`` D1/D2) explains that: under the
Launcher's own environment ``HERMES_HOME == HERMES_HEAD_HOME``, so both were
dormant. This is a third member of the same class, and it was the live one.
D1/D2 are fixed here too, and pinned below.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime.chat_session_scope import (
    CHAT_HEAD_POINTER_FILENAME,
    ChatHeadSource,
    chat_session_db_path,
    publish_chat_head_home,
    recorded_chat_head_home,
    resolve_chat_session_scope,
)
from agent_runtime.models import PersonaInstance
from agent_runtime.persona_chat_history import (
    PERSONA_CHAT_SESSION_SOURCE,
    persona_chat_history_summary,
)
from agent_runtime.parity import ProjectionAccountant
from agent_runtime.states import WorkerSessionState


@pytest.fixture
def runtime_root(tmp_path, monkeypatch):
    """A shared runtime root with two profile homes, like the live layout.

    ``store_root`` is pinned by env so the persona-instance store (and the head
    pointer beside it) is SHARED by both profiles — exactly the collapse the
    live ``resolution._default_hermes_root`` performs.
    """

    root = tmp_path / ".hermes"
    store_root = root / "agent-runtime"
    head_home = root / "profiles" / "base"
    other_home = root / "profiles" / "alice"
    for path in (store_root, head_home, other_home):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(store_root))
    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(head_home))
    return {
        "root": root,
        "store_root": store_root,
        "head_home": head_home,
        "other_home": other_home,
    }


def _serve_lane(monkeypatch, runtime_root) -> None:
    """The Launcher's serve environment: an EXPLICIT head, profile ``base``."""

    monkeypatch.setenv("HERMES_HOME", str(runtime_root["head_home"]))
    monkeypatch.setenv("HERMES_HEAD_HOME", str(runtime_root["head_home"]))


def _cli_lane(monkeypatch, runtime_root) -> None:
    """A bare CLI turn under another profile: no head named at all."""

    monkeypatch.setenv("HERMES_HOME", str(runtime_root["other_home"]))
    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)


def _instance(session_id: str) -> PersonaInstance:
    return PersonaInstance(
        id="personainst_qa_agent",
        persona_id="qa",
        role="qa",
        display_name="QA Agent",
        profile_id=None,
        runtime_root="test-runtime",
        state=WorkerSessionState.IDLE,
        mode="chat",
        default_chat_session_id=session_id,
    )


# ── the ladder ──────────────────────────────────────────────────────────────


def test_the_env_head_wins_and_is_explicitly_named(monkeypatch, runtime_root):
    _serve_lane(monkeypatch, runtime_root)

    scope = resolve_chat_session_scope()

    assert scope.source is ChatHeadSource.ENV_HEAD_HOME
    assert scope.head_home == runtime_root["head_home"]
    assert scope.db_path == runtime_root["head_home"] / "state.db"
    assert scope.authoritative and scope.explicitly_named


def test_with_no_head_named_the_scope_degrades_to_the_ambient_home(
    monkeypatch, runtime_root
):
    _cli_lane(monkeypatch, runtime_root)

    scope = resolve_chat_session_scope()

    assert scope.source is ChatHeadSource.AMBIENT_HOME
    assert scope.head_home == runtime_root["other_home"]
    # THE BUG, stated as an invariant: an ambient scope is not authoritative and
    # never explicitly named, so no lane may treat its verdicts as truth.
    assert not scope.authoritative
    assert not scope.explicitly_named


def test_the_recorded_pointer_beats_the_ambient_home_but_is_not_explicit(
    monkeypatch, runtime_root
):
    _serve_lane(monkeypatch, runtime_root)
    assert publish_chat_head_home() == runtime_root["head_home"]

    _cli_lane(monkeypatch, runtime_root)
    scope = resolve_chat_session_scope()

    assert scope.source is ChatHeadSource.SHARED_ROOT_POINTER
    assert scope.head_home == runtime_root["head_home"]
    assert scope.authoritative
    # Good enough to READ or MINT a transcript; deliberately NOT good enough to
    # clear a live binding (that lane still requires an explicitly named head).
    assert not scope.explicitly_named


def test_an_explicit_head_always_beats_a_recorded_pointer(monkeypatch, runtime_root):
    _serve_lane(monkeypatch, runtime_root)
    publish_chat_head_home()

    monkeypatch.setenv("HERMES_HEAD_HOME", str(runtime_root["other_home"]))
    scope = resolve_chat_session_scope()

    assert scope.source is ChatHeadSource.ENV_HEAD_HOME
    assert scope.head_home == runtime_root["other_home"]


def test_only_an_explicitly_headed_process_publishes_the_pointer(
    monkeypatch, runtime_root
):
    _cli_lane(monkeypatch, runtime_root)

    assert publish_chat_head_home() is None
    assert not (runtime_root["store_root"] / CHAT_HEAD_POINTER_FILENAME).exists()


def test_publishing_is_idempotent(monkeypatch, runtime_root):
    _serve_lane(monkeypatch, runtime_root)

    assert publish_chat_head_home() == runtime_root["head_home"]
    # Second publish of the SAME head writes nothing — a serve restart must not
    # churn a file every fingerprint lane stats.
    assert publish_chat_head_home() is None

    pointer = runtime_root["store_root"] / CHAT_HEAD_POINTER_FILENAME
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    assert payload["head_home"] == str(runtime_root["head_home"])


def test_a_pointer_at_a_vanished_home_is_ignored_not_honored(
    monkeypatch, runtime_root
):
    """A stale pointer must not strand every chat in a directory nothing writes."""

    pointer = runtime_root["store_root"] / CHAT_HEAD_POINTER_FILENAME
    pointer.write_text(
        json.dumps({"head_home": str(runtime_root["root"] / "profiles" / "gone")}),
        encoding="utf-8",
    )
    _cli_lane(monkeypatch, runtime_root)

    assert recorded_chat_head_home() is None
    assert resolve_chat_session_scope().source is ChatHeadSource.AMBIENT_HOME


# ── the live gap: mint on the CLI lane, read on the serve lane ───────────────


def _mint_on_the_cli_lane(session_id: str) -> None:
    """Mint a persona-chat session through the REAL CLI-lane acquisition.

    ``harness_parts/persona_commands.py`` is exec'd into ``hermes_cli.harness``'s
    globals (``_load_command_parts``), so the harness module IS the import
    surface for its helpers — importing the part file directly would compile it
    without the harness globals it is written against.
    """

    from hermes_cli.harness import _default_persona_session_db

    db = _default_persona_session_db()
    db.ensure_session(session_id, source=PERSONA_CHAT_SESSION_SOURCE)


def _projected_session_ids(instance) -> tuple[list[str], ProjectionAccountant]:
    """The serve/read-model projection, resolving its own database (no injection)."""

    accountant = ProjectionAccountant("persona_chat_history")
    rows = persona_chat_history_summary(
        persona_instances=[instance], accountant=accountant
    )
    return [row.get("session_id") for row in rows], accountant


def _drop_codes(accountant: ProjectionAccountant) -> list[str]:
    return sorted((accountant.summary() or {}).get("reasons") or {})


def test_a_cli_lane_session_is_listed_by_the_serve_projection(
    monkeypatch, runtime_root
):
    """THE CHECK THAT FAILED TWICE LIVE.

    Serve publishes its head; a CLI turn under a DIFFERENT profile home, naming
    no head of its own, mints a transcript; the serve projection lists it.
    """

    session_id = "persona_chat_personainst_qa_agent_deadbeef"

    _serve_lane(monkeypatch, runtime_root)
    publish_chat_head_home()

    _cli_lane(monkeypatch, runtime_root)
    _mint_on_the_cli_lane(session_id)
    # The transcript landed in the HEAD database, not the CLI turn's own profile.
    assert (runtime_root["head_home"] / "state.db").exists()
    assert not (runtime_root["other_home"] / "state.db").exists()

    _serve_lane(monkeypatch, runtime_root)
    session_ids, accountant = _projected_session_ids(_instance(session_id))

    assert session_id in session_ids
    assert "session_not_in_db" not in _drop_codes(accountant)


def test_without_the_pointer_the_same_turn_reproduces_the_gap(
    monkeypatch, runtime_root
):
    """The bug itself, pinned. Remove the pointer and the row goes dark again —
    proof that the published head is what closes the gap, not an accident of
    the fixture."""

    session_id = "persona_chat_personainst_qa_agent_deadbeef"

    _cli_lane(monkeypatch, runtime_root)
    _mint_on_the_cli_lane(session_id)
    assert (runtime_root["other_home"] / "state.db").exists()

    _serve_lane(monkeypatch, runtime_root)
    session_ids, accountant = _projected_session_ids(_instance(session_id))

    assert session_id not in session_ids
    assert "session_not_in_db" in _drop_codes(accountant)


# ── D1: the serve read-model cache key ──────────────────────────────────────


def test_the_serve_fingerprint_keys_on_the_chat_scope_not_hermes_home(
    monkeypatch, runtime_root
):
    """D1. The cache key called a bare ``SessionDB()`` (``HERMES_HOME``) while
    every chat write goes to the chat scope; whenever the two diverge a cached
    snapshot could serve a frozen Chat History for the life of the process."""

    from hermes_cli.harness_parts.serve import _runtime_state_fingerprint

    _cli_lane(monkeypatch, runtime_root)
    monkeypatch.setenv("HERMES_HEAD_HOME", str(runtime_root["head_home"]))
    head_db = runtime_root["head_home"] / "state.db"

    assert chat_session_db_path() == head_db
    before = _runtime_state_fingerprint()
    assert before is not None
    assert any(str(head_db) in str(part[0]) for part in before)

    # A write to the profile database the process's HERMES_HOME points at must
    # NOT move the fingerprint; a write to the head database must.
    (runtime_root["other_home"] / "state.db").write_bytes(b"decoy")
    assert _runtime_state_fingerprint() == before

    head_db.write_bytes(b"chat")
    assert _runtime_state_fingerprint() != before


# ── D2: the continuity summary write ────────────────────────────────────────


def test_continuity_returns_the_summary_to_the_head_database(
    monkeypatch, runtime_root
):
    """D2. ``return_summary_to_parent_session`` used a bare ``SessionDB()``, so a
    child returning to its parent — which runs precisely under a profile-home
    override — wrote the summary into the CHILD's profile database and minted a
    phantom parent-session row there."""

    from agent_runtime import continuity

    _serve_lane(monkeypatch, runtime_root)
    publish_chat_head_home()

    _cli_lane(monkeypatch, runtime_root)
    db = continuity._session_db()

    assert db.db_path == runtime_root["head_home"] / "state.db"
    assert not (runtime_root["other_home"] / "state.db").exists()


# ── the canonical-persistence predicate ─────────────────────────────────────
#
# The `unknown_chat_session` / `foreign_chat_session` guards refuse a turn
# because a session id names no row. That inference is only sound against a
# store where "no row" MEANS "no such session"; against anything else the same
# silence means "cannot answer", and refusing on it rejects legitimate sessions
# for a reason nobody checked. The question used to be spelled by hand as
# `session_db.__class__.__module__ == "hermes_state"` at four separate guards —
# four answers free to drift, and no seam a caller could opt into, so a double
# that needed the production guard exercised had to SPOOF `__module__`.


def test_the_real_store_is_canonical_persistence(tmp_path):
    from hermes_state import SessionDB

    from agent_runtime.chat_session_scope import is_canonical_session_persistence

    # The module fallback, and the reason it must stay: `hermes_state` is
    # UPSTREAM in this fork and cannot be edited to carry the marker. That is
    # the fork boundary, not legacy tolerance.
    assert is_canonical_session_persistence(SessionDB(tmp_path / "state.db")) is True


def test_an_unmarked_double_is_not_canonical_persistence():
    """The default direction is the load-bearing choice.

    Defaulting the other way would quietly make every stub in the codebase
    eligible to have turns refused against it — and that failure arrives as a
    wrongly-rejected send, not as a loud one."""

    from agent_runtime.chat_session_scope import is_canonical_session_persistence

    class _Stub:
        def get_session(self, session_id):
            return None

    assert is_canonical_session_persistence(_Stub()) is False
    assert is_canonical_session_persistence(None) is False


def test_a_double_opts_in_under_its_own_name(monkeypatch):
    """The honest seam that replaced the spoof.

    A double declares that it backs the full session surface with a marker
    ATTRIBUTE. It no longer has to claim membership of a module it was never
    defined in just to be believed by a guard."""

    from agent_runtime.chat_session_scope import (
        CANONICAL_SESSION_PERSISTENCE_ATTR,
        is_canonical_session_persistence,
    )

    class _Double:
        pass

    double = _Double()
    assert is_canonical_session_persistence(double) is False
    setattr(double, CANONICAL_SESSION_PERSISTENCE_ATTR, True)
    assert is_canonical_session_persistence(double) is True
    # …and the marker is what the doubles actually set, not a name drifting
    # apart from the one the predicate reads.
    assert CANONICAL_SESSION_PERSISTENCE_ATTR == "__hermes_canonical_session_persistence__"


def test_no_guard_hand_spells_the_module_sniff_any_more():
    """Recurrence is the finding: one predicate, or four that drift.

    A grep gate rather than a behavior assertion, because the defect this
    retires is a QUESTION ASKED IN FOUR PLACES — a fifth hand-spelling would
    pass every behavioral test in this file and still be the bug."""

    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    # `chat_session_scope.py` is the ONE sanctioned home: the fallback lives
    # inside the predicate, where the fork boundary that forces it is documented.
    predicate_home = repo_root / "agent_runtime" / "chat_session_scope.py"
    offenders = [
        f"{path.relative_to(repo_root).as_posix()}:{number}"
        for path in [
            repo_root / "hermes_cli" / "harness.py",
            *(repo_root / "hermes_cli" / "harness_parts").glob("*.py"),
            *(repo_root / "agent_runtime").glob("*.py"),
        ]
        if path != predicate_home
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "__module__" in line and "hermes_state" in line
    ]

    assert offenders == [], (
        "use agent_runtime.chat_session_scope.is_canonical_session_persistence "
        f"instead of a hand-spelled module sniff: {offenders}"
    )
