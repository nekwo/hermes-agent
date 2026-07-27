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
``SHARED_ROOT_POINTER`` ``<store_root>/chat_head_home.json`` — published by an
                        explicitly-headed process for this runtime root.
``AMBIENT_HOME``        degraded: ``get_hermes_home()``. NOT authoritative;
                        this is the state the 2026-07-25 binding massacre and
                        the 2026-07-27 read-lane gap were both computed in.
======================  =====================================================

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
    "CHAT_HEAD_POINTER_FILENAME",
    "CHAT_SESSION_DB_FILENAME",
    "ChatHeadSource",
    "ChatSessionScope",
    "chat_session_db_path",
    "open_chat_session_db",
    "publish_chat_head_home",
    "recorded_chat_head_home",
    "resolve_chat_session_scope",
]

#: Lives beside ``persona_instances/`` in the SHARED runtime store root, because
#: the pointer answers a question about that root: "which chat database do the
#: bindings in this store dereference against?"
CHAT_HEAD_POINTER_FILENAME = "chat_head_home.json"

CHAT_SESSION_DB_FILENAME = "state.db"


class ChatHeadSource(str, Enum):
    """Where the resolved chat head home came from. Ordered strongest-first."""

    RELAY_CONTEXT = "relay_context"
    ENV_HEAD_HOME = "env_head_home"
    SHARED_ROOT_POINTER = "shared_root_pointer"
    AMBIENT_HOME = "ambient_home"


_EXPLICIT_SOURCES = frozenset(
    {ChatHeadSource.RELAY_CONTEXT, ChatHeadSource.ENV_HEAD_HOME}
)


@dataclass(frozen=True, slots=True)
class ChatSessionScope:
    """The resolved chat-database scope for this process/context."""

    head_home: Path
    source: ChatHeadSource

    @property
    def db_path(self) -> Path:
        return self.head_home / CHAT_SESSION_DB_FILENAME

    @property
    def explicitly_named(self) -> bool:
        """True when THIS process named the head (relay context or env).

        The destructive binding-repair lane requires this: a recorded pointer is
        good enough to read or mint a transcript, never good enough to clear a
        live binding.
        """

        return self.source in _EXPLICIT_SOURCES

    @property
    def authoritative(self) -> bool:
        """True when SOME authority named this head — i.e. not the degraded
        ambient fallback."""

        return self.source is not ChatHeadSource.AMBIENT_HOME

    def payload(self) -> dict[str, Any]:
        """Stable, machine-readable scope block for ``--json`` envelopes."""

        return {
            "head_home": str(self.head_home),
            "db_path": str(self.db_path),
            "source": self.source.value,
            "authoritative": self.authoritative,
            "explicitly_named": self.explicitly_named,
        }


def resolve_chat_session_scope() -> ChatSessionScope:
    """Resolve the chat-database scope. Never raises."""

    from hermes_constants import (
        get_hermes_head_home,
        get_hermes_home,
        hermes_head_home_is_authoritative,
    )

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
            return ChatSessionScope(head, _explicit_source(head))

    pointer = recorded_chat_head_home()
    if pointer is not None:
        return ChatSessionScope(pointer, ChatHeadSource.SHARED_ROOT_POINTER)

    try:
        ambient = Path(get_hermes_head_home())
    except Exception:  # pragma: no cover - defensive
        ambient = Path(get_hermes_home())
    return ChatSessionScope(ambient, ChatHeadSource.AMBIENT_HOME)


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
