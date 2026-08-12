"""ONE authority for *which* chat ``SessionDB`` is the operator-visible one.

Background: ``docs/agent-runtime-harness/chat-session-presence-authority.md``
(stage P1 — "one acquisition, zero policy change"). This module is the single
place the runtime decides which ``state.db`` holds persona-chat transcripts, so
a new consumer can no longer re-decide it wrongly.

The live defect this retires (2026-07-27, the cockpit read-lane gap)
-------------------------------------------------------------------

The persona-instance store DELIBERATELY collapses a per-profile
``HERMES_HOME`` onto the shared runtime root: ``resolution._default_hermes_root``
maps ``<root>/profiles/<x>`` back to ``<root>``, so every profile shares ONE
``persona_instances/`` directory and therefore ONE
``default_chat_session_id`` per instance.

The chat ``SessionDB`` those pointers dereference did **not** collapse.
``hermes_constants.get_hermes_head_home()`` falls back to
``get_hermes_home()`` whenever no head authority is named — and only the
Launcher names one (``HERMES_HEAD_HOME=<root>/profiles/base``, set in
``mission_control_settings.dart``). So a CLI-lane turn running under a
profile home minted its transcript into ``<root>/profiles/<active>/state.db``
while writing the binding into the SHARED store. The Mission Control read lane
then opened the head DB, failed to find the session, and dropped the row as
``session_not_in_db``.

Live evidence at the time of writing: the three most recently updated
persona-instance bindings in ``X:\\Eternia\\.hermes\\agent-runtime`` pointed at
sessions that existed only in ``profiles/alice/state.db``; the cockpit — reading
``profiles/base/state.db`` — listed only the older bindings whose sessions
happened to live there.

The fix is to give the shared runtime root a **recorded head pointer**. The
process that legitimately knows the operator head (``harness serve``, which the
Launcher always starts with an explicit ``HERMES_HEAD_HOME``) publishes it once
into the shared store; every later process that names no head of its own reads
it instead of degrading to its own profile home. Env and relay context always
win over the pointer, so this can only ever *narrow* the ambient fallback.

Resolution ladder (highest first)
---------------------------------

======================  =====================================================
``RELAY_CONTEXT``       the ``_HERMES_HEAD_HOME`` ContextVar — a nested relay
                        turn can never escape the operator that started it.
``ENV_HEAD_HOME``       ``HERMES_HEAD_HOME`` — what the Launcher supplies.
``INSTANCE_RECORDED``   the chat head the persona instance bound to this
                        conversation RECORDED at bind/turn time
                        (``PersonaInstance.chat_head_home``). Consulted only
                        when the caller names a ``session_id`` — the chat head
                        is a PER-CONVERSATION fact, so this rung answers for
                        one conversation, never for the process. Requires the
                        store root to be resolvable, which the machine root
                        anchor (``root_anchor.py``) guarantees for ambient
                        processes on an anchored machine.
``SHARED_ROOT_POINTER`` ``<store_root>/chat_head_home.json`` — published by an
                        explicitly-headed process for this runtime root.
``AMBIENT_HOME``        degraded: ``get_hermes_home()``. NOT authoritative;
                        this is the state the 2026-07-25 binding massacre and
                        the 2026-07-27 read-lane gap were both computed in.
======================  =====================================================

The ``INSTANCE_RECORDED`` rung does DOUBLE DUTY — resolve and verify. When an
AUTHORITATIVE rung other than the instance record names a DIFFERENT head than
the record (the explicit env/relay head above it, or the shared-root pointer
below it), the scope carries a typed :class:`ChatScopeMismatch` instead of
silently preferring one side: one of the two authorities is wrong, and a read
served from either could be answering from the wrong store. The AMBIENT guess
disagreeing with the record is NOT a mismatch — a guess losing to a recorded
fact is this rung doing its job (that rescue is the 2026-08-12 incident
retired). Absence is not a mismatch either: an instance predating the stamp
(``chat_head_home is None``) falls through to the pointer exactly as before.

Two postures ride the scope, and callers declare which one they need rather
than re-deriving a guard:

* ``authoritative`` — some authority named this head (anything but
  ``AMBIENT_HOME``). Enough to READ a transcript or MINT one.
* ``explicitly_named`` — THIS process named it (relay context or env). The
  destructive lane (``persona_assignments`` binding repair) requires this, so
  its behavior is byte-identical to the shipped ``8c3942a21`` guard: a recorded
  pointer must never be enough to clear a live binding.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "AMBIENT_CHAT_READS_ENV",
    "CANONICAL_SESSION_PERSISTENCE_ATTR",
    "CHAT_HEAD_POINTER_FILENAME",
    "CHAT_SESSION_DB_FILENAME",
    "ChatHeadSource",
    "ChatScopeMismatch",
    "ChatSessionScope",
    "InstanceChatHead",
    "InstanceChatHeadStatus",
    "ambient_chat_reads_allowed",
    "chat_session_db_path",
    "is_canonical_session_persistence",
    "open_chat_session_db",
    "publish_chat_head_home",
    "recorded_chat_head_home",
    "recorded_instance_chat_head",
    "resolve_chat_session_scope",
]

#: The opt-in marker a NON-``hermes_state`` object sets to declare that it is
#: real, queryable chat persistence. See
#: :func:`is_canonical_session_persistence` for why it is opt-in and not opt-out.
CANONICAL_SESSION_PERSISTENCE_ATTR = "__hermes_canonical_session_persistence__"


def is_canonical_session_persistence(session_db: Any) -> bool:
    """Is *session_db* real chat persistence whose SILENCE is a fact?

    The one predicate behind the ``unknown_chat_session`` and
    ``foreign_chat_session`` guards. Those guards refuse a turn because a session
    id names no row, so they may only run against a store where "no row" MEANS
    "no such session". Against a stub, a partial double, or anything that merely
    resembles the interface, the same silence means "this object cannot answer" —
    and refusing on it would reject legitimate sessions for a reason nobody
    actually checked.

    Why a predicate and not four inline ``__module__`` comparisons: the sniff was
    hand-spelled at each guard, so "may I refuse on this store?" had four answers
    free to drift apart, and there was no seam a caller could opt into. A double
    that needed the production guard exercised had to SPOOF
    ``__module__ = "hermes_state"`` — claiming to be a module it is not, in order
    to be believed. A test asset lying about its identity to reach behavior is
    the shape that makes a guard untestable honestly.

    Two ways to be canonical, in this order:

    1. **The opt-in marker.** Any object may set
       ``__hermes_canonical_session_persistence__ = True`` to declare that it
       backs the full session surface these guards interrogate. A double then
       says so under its own name instead of impersonating a module.
    2. **The module fallback, for the real store only.** ``hermes_state`` is
       UPSTREAM in this fork and cannot be edited to carry the marker, so this
       is not legacy tolerance — it is the fork boundary. It stays narrow: the
       class's defining module, nothing inferred by duck-typing.

    An unmarked object is NOT canonical, deliberately. Defaulting the other way
    would quietly make every stub in the codebase eligible to have turns refused
    against it, and that failure arrives as a wrongly-rejected send rather than
    as a loud one.
    """

    if session_db is None:
        return False
    if bool(getattr(session_db, CANONICAL_SESSION_PERSISTENCE_ATTR, False)):
        return True
    return getattr(type(session_db), "__module__", "") == "hermes_state"

#: Lives beside ``persona_instances/`` in the SHARED runtime store root, because
#: the pointer answers a question about that root: "which chat database do the
#: bindings in this store dereference against?"
CHAT_HEAD_POINTER_FILENAME = "chat_head_home.json"

CHAT_SESSION_DB_FILENAME = "state.db"


class ChatHeadSource(str, Enum):
    """Where the resolved chat head home came from. Ordered strongest-first."""

    RELAY_CONTEXT = "relay_context"
    ENV_HEAD_HOME = "env_head_home"
    INSTANCE_RECORDED = "instance_recorded"
    SHARED_ROOT_POINTER = "shared_root_pointer"
    AMBIENT_HOME = "ambient_home"


_EXPLICIT_SOURCES = frozenset(
    {ChatHeadSource.RELAY_CONTEXT, ChatHeadSource.ENV_HEAD_HOME}
)


class InstanceChatHeadStatus(str, Enum):
    """Every answer the per-conversation instance-record lookup can give.

    Modeled as an enum rather than ``Path | None`` because three distinct
    kinds of "no head" exist and only conflating them reproduces the silent
    classes this module retires: a lookup that could not run says nothing
    about the record, and an absent record says nothing about the store.
    """

    #: The instance bound to this session records a chat head home.
    RECORDED = "recorded"
    #: The instance records a head home whose directory no longer exists.
    #: Honoring it would strand the conversation in a directory nothing
    #: writes (the stale-pointer posture, mirrored); it falls through like
    #: an absent record, but says so under its own name.
    RECORDED_HOME_MISSING = "recorded_home_missing"
    #: The instance exists but carries no stamp — a row predating the stamp,
    #: or one whose every bind ran under a degraded ambient scope. NOT a
    #: mismatch; falls through to the pointer exactly as before the rung.
    UNRECORDED = "unrecorded"
    #: No persona instance binds this session id (child/unbound sessions).
    NO_INSTANCE = "no_instance"
    #: The persona-instance store could not be read from this process
    #: (unresolvable store root, IO failure). Says nothing about the record.
    UNRESOLVABLE = "unresolvable"


@dataclass(frozen=True, slots=True)
class InstanceChatHead:
    """The per-conversation instance-record lookup result, typed."""

    status: InstanceChatHeadStatus
    head_home: Path | None = None
    instance_id: str | None = None

    @property
    def recorded(self) -> bool:
        return self.status is InstanceChatHeadStatus.RECORDED

    def payload(self) -> dict[str, Any]:
        block: dict[str, Any] = {"status": self.status.value}
        if self.head_home is not None:
            block["head_home"] = str(self.head_home)
        if self.instance_id:
            block["instance_id"] = self.instance_id
        return block


@dataclass(frozen=True, slots=True)
class ChatScopeMismatch:
    """Two AUTHORITIES disagree about where this conversation lives.

    ``recorded_head`` is what the persona instance stamped at bind/turn time;
    ``resolved_head`` is what the disagreeing rung (explicit env/relay above,
    or the shared-root pointer below) names. Neither side is silently
    preferred: a read that would serve data from either store surfaces this
    instead, because one of them is the wrong store and a wrong store answers
    with a well-formed empty page.
    """

    recorded_head: Path
    resolved_head: Path
    resolved_source: ChatHeadSource

    def payload(self) -> dict[str, Any]:
        return {
            "recorded_head": str(self.recorded_head),
            "resolved_head": str(self.resolved_head),
            "resolved_source": self.resolved_source.value,
        }


@dataclass(frozen=True, slots=True)
class ChatSessionScope:
    """The resolved chat-database scope for this process/context.

    ``instance_head`` / ``mismatch`` are populated only when the caller named a
    ``session_id`` (per-conversation resolution); ``None`` means the lookup was
    not performed, which is different from the lookup finding nothing.
    """

    head_home: Path
    source: ChatHeadSource
    instance_head: InstanceChatHead | None = None
    mismatch: ChatScopeMismatch | None = None

    @property
    def db_path(self) -> Path:
        return self.head_home / CHAT_SESSION_DB_FILENAME

    @property
    def explicitly_named(self) -> bool:
        """True when THIS process named the head (relay context or env).

        The destructive binding-repair lane requires this: a recorded pointer
        or instance record is good enough to read or mint a transcript, never
        good enough to clear a live binding.
        """

        return self.source in _EXPLICIT_SOURCES

    @property
    def authoritative(self) -> bool:
        """True when SOME authority named this head — i.e. not the degraded
        ambient fallback."""

        return self.source is not ChatHeadSource.AMBIENT_HOME

    def payload(self) -> dict[str, Any]:
        """Stable, machine-readable scope block for ``--json`` envelopes."""

        block: dict[str, Any] = {
            "head_home": str(self.head_home),
            "db_path": str(self.db_path),
            "source": self.source.value,
            "authoritative": self.authoritative,
            "explicitly_named": self.explicitly_named,
        }
        if self.instance_head is not None:
            block["instance_head"] = self.instance_head.payload()
        if self.mismatch is not None:
            block["mismatch"] = self.mismatch.payload()
        return block


def resolve_chat_session_scope(*, session_id: str | None = None) -> ChatSessionScope:
    """Resolve the chat-database scope. Never raises.

    With a ``session_id`` the resolution is PER-CONVERSATION: the
    ``INSTANCE_RECORDED`` rung is consulted between the explicit env/relay
    rungs and the shared-root pointer, and the returned scope carries the
    lookup result plus any :class:`ChatScopeMismatch` between authorities.
    Without one, the ladder is exactly the process-level ladder it has always
    been — every existing caller is byte-identical.
    """

    from hermes_constants import (
        get_hermes_head_home,
        get_hermes_home,
        hermes_head_home_is_authoritative,
    )

    instance_head = (
        recorded_instance_chat_head(session_id) if session_id else None
    )

    def _mismatch_against(head: Path, source: ChatHeadSource) -> ChatScopeMismatch | None:
        if (
            instance_head is not None
            and instance_head.recorded
            and instance_head.head_home is not None
            and not _same_path(instance_head.head_home, head)
        ):
            return ChatScopeMismatch(
                recorded_head=instance_head.head_home,
                resolved_head=head,
                resolved_source=source,
            )
        return None

    try:
        explicit = bool(hermes_head_home_is_authoritative())
    except Exception:  # pragma: no cover - defensive
        explicit = False

    if explicit:
        try:
            head = Path(get_hermes_head_home())
        except Exception:  # pragma: no cover - defensive
            explicit = False
        else:
            source = _explicit_source(head)
            # The explicit head still WINS resolution — env/relay outrank the
            # record — but a disagreeing record is surfaced, never dropped:
            # either the stamp is stale or the environment is wrong, and only
            # the caller's posture knows whether that must refuse.
            return ChatSessionScope(
                head,
                source,
                instance_head=instance_head,
                mismatch=_mismatch_against(head, source),
            )

    pointer = recorded_chat_head_home()

    if (
        instance_head is not None
        and instance_head.recorded
        and instance_head.head_home is not None
    ):
        mismatch = (
            _mismatch_against(pointer, ChatHeadSource.SHARED_ROOT_POINTER)
            if pointer is not None
            else None
        )
        return ChatSessionScope(
            instance_head.head_home,
            ChatHeadSource.INSTANCE_RECORDED,
            instance_head=instance_head,
            mismatch=mismatch,
        )

    if pointer is not None:
        return ChatSessionScope(
            pointer, ChatHeadSource.SHARED_ROOT_POINTER, instance_head=instance_head
        )

    try:
        ambient = Path(get_hermes_head_home())
    except Exception:  # pragma: no cover - defensive
        ambient = Path(get_hermes_home())
    return ChatSessionScope(
        ambient, ChatHeadSource.AMBIENT_HOME, instance_head=instance_head
    )


def recorded_instance_chat_head(session_id: str) -> InstanceChatHead:
    """The chat head the persona instance bound to *session_id* recorded.

    Reads the ONE persona-instance store (via :mod:`agent_runtime.paths`'
    resolved store root — no parallel lookup path), matching on the canonical
    ``default_chat_session_id`` binding. Never raises; every failure mode is a
    typed :class:`InstanceChatHeadStatus`, because "the lookup could not run"
    must never masquerade as "no record exists".
    """

    normalized = str(session_id or "").strip()
    if not normalized:
        return InstanceChatHead(InstanceChatHeadStatus.NO_INSTANCE)
    try:
        from .persona_assignments import PersonaInstanceStore

        instances = PersonaInstanceStore().list_all()
    except Exception:
        return InstanceChatHead(InstanceChatHeadStatus.UNRESOLVABLE)
    match = None
    for instance in instances:
        if getattr(instance, "default_chat_session_id", None) == normalized:
            match = instance
            break
    if match is None:
        return InstanceChatHead(InstanceChatHeadStatus.NO_INSTANCE)
    instance_id = str(getattr(match, "id", "") or "") or None
    recorded = str(getattr(match, "chat_head_home", None) or "").strip()
    if not recorded:
        return InstanceChatHead(
            InstanceChatHeadStatus.UNRECORDED, instance_id=instance_id
        )
    candidate = Path(recorded).expanduser()
    try:
        home_exists = candidate.is_dir()
    except OSError:  # pragma: no cover - defensive
        home_exists = False
    if not home_exists:
        # The stale-pointer posture, mirrored: a recorded home that no longer
        # exists must not strand the conversation in a directory nothing
        # writes — but it falls through under its OWN name, never silently.
        return InstanceChatHead(
            InstanceChatHeadStatus.RECORDED_HOME_MISSING,
            head_home=candidate,
            instance_id=instance_id,
        )
    return InstanceChatHead(
        InstanceChatHeadStatus.RECORDED,
        head_home=candidate,
        instance_id=instance_id,
    )


#: Escape hatch for a deliberately single-root, serve-less setup: chat READS
#: that would resolve the degraded AMBIENT_HOME rung are refused by default
#: (see ``persona_chat_history.persona_chat_session_messages``), because an
#: ambient read answers from whichever ``state.db`` the process happens to
#: resolve — the well-formed-empty class of the 2026-08-12 incident. An
#: operator whose runtime genuinely lives at the platform default (no serve,
#: no anchor, no pointer) can accept the guess explicitly.
AMBIENT_CHAT_READS_ENV = "HERMES_ALLOW_AMBIENT_CHAT_READS"

_TRUTHY = frozenset({"1", "true", "yes", "on", "allow"})


def ambient_chat_reads_allowed(env: Any = None) -> bool:
    source = os.environ if env is None else env
    return str(source.get(AMBIENT_CHAT_READS_ENV) or "").strip().lower() in _TRUTHY


def chat_session_db_path() -> Path:
    """The resolved chat ``state.db`` path, WITHOUT opening or creating it.

    Fingerprint lanes want the path, not a database handle: constructing a
    ``SessionDB`` to read ``.db_path`` opened (and could create) a file on every
    poll.
    """

    return resolve_chat_session_scope().db_path


def open_chat_session_db(scope: ChatSessionScope | None = None) -> Any | None:
    """Open the operator-visible chat ``SessionDB``; ``None`` when unavailable.

    Callers that must fail loudly wrap the ``None`` in their own typed error —
    the acquisition is shared, the failure posture is not.
    """

    resolved = scope or resolve_chat_session_scope()
    try:
        from hermes_state import SessionDB

        return SessionDB(db_path=resolved.db_path)
    except Exception:
        return None


def recorded_chat_head_home() -> Path | None:
    """The head home recorded in the shared runtime store root, if usable.

    A pointer at a home that no longer exists is ignored rather than honored: a
    stale pointer must not strand every chat in a directory nothing writes.
    """

    path = _pointer_path()
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    recorded = str(raw.get("head_home") or "").strip()
    if not recorded:
        return None
    candidate = Path(recorded).expanduser()
    try:
        if not candidate.is_dir():
            return None
    except OSError:  # pragma: no cover - defensive
        return None
    return candidate


def publish_chat_head_home(scope: ChatSessionScope | None = None) -> Path | None:
    """Record this process's EXPLICIT head home for the shared runtime root.

    The single writer of the pointer, called once from the long-running
    ``harness serve`` boot — the process the Launcher always starts with an
    explicit ``HERMES_HEAD_HOME``. Returns the published path, or ``None`` when
    nothing was published (no explicit head, no resolvable store root, or the
    pointer already says this).

    Best effort by contract: publishing is a convenience for OTHER processes and
    must never be able to fail the process that happens to know the answer.
    """

    resolved = scope or resolve_chat_session_scope()
    if not resolved.explicitly_named:
        return None
    path = _pointer_path()
    if path is None:
        return None
    existing = recorded_chat_head_home()
    if existing is not None and _same_path(existing, resolved.head_home):
        return None
    payload = {
        "head_home": str(resolved.head_home),
        "source": resolved.source.value,
    }
    try:
        from hermes_time import now

        payload["recorded_at"] = now().isoformat()
    except Exception:  # pragma: no cover - a timestamp is not load-bearing
        pass
    if not _atomic_write_json(path, payload):
        return None
    return resolved.head_home


# ── internals ───────────────────────────────────────────────────────────────


def _explicit_source(head: Path) -> ChatHeadSource:
    """Attribute an explicit head to the env when the env named exactly it.

    Deliberately derived by comparison rather than by reaching into
    ``hermes_constants``' private ContextVar: a relay head that differs from the
    env value is precisely the nesting case ``RELAY_CONTEXT`` exists to name.
    """

    configured = os.environ.get("HERMES_HEAD_HOME", "").strip()
    if configured and _same_path(Path(configured).expanduser(), head):
        return ChatHeadSource.ENV_HEAD_HOME
    return ChatHeadSource.RELAY_CONTEXT


def _pointer_path() -> Path | None:
    try:
        from . import paths

        return paths.store_root() / CHAT_HEAD_POINTER_FILENAME
    except Exception:
        return None


def _same_path(left: Path, right: Path) -> bool:
    return _normalized(left) == _normalized(right)


def _normalized(path: Path) -> str:
    try:
        return str(path.resolve(strict=False)).casefold()
    except OSError:  # pragma: no cover - defensive
        return str(path.absolute()).casefold()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(handle.name, path)
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise
    except Exception:
        return False
    return True
