"""The typed refusals the charsheet lane raises — the two a caller can act on.

Pure stdlib, and deliberately **not** subclasses of
:class:`agent_runtime.errors.AgentRuntimeError`. This package is fork-owned but
lives inside the upstream ``agent`` namespace so it ships with the plain hermes
wheel, and ``agent_runtime`` does not: it is absent from
``[tool.setuptools.packages.find].include`` in ``pyproject.toml``, which is the
packaging boundary :mod:`agent.charsheet`, :mod:`agent.charsheet.spec` and
:mod:`agent.charsheet.revisions` each record in their own words. An exception
class that could not be imported by an installed wheel is not a refusal, it is
an ``ImportError`` at the worst possible moment.

What IS borrowed is the *convention*: a class-level :attr:`code`, spelled the
same way ``agent_runtime/errors.py`` spells one, and a ``safe_details`` dict for
the facts a consumer may render. So the refusal reads identically on either
side of the boundary without the import crossing it.

Both refusals are things an operator can act on rather than bugs, which is why
``hermes_cli/harness.py``'s ``_CHARACTERS_EXPECTED`` catches this base class:
they travel in the flat ``{"ok": false, "error": …}`` payload the character
verbs already emit — now carrying ``code`` so a consumer branches on a token
rather than on the message text — and never as a traceback.
"""

from __future__ import annotations


class CharsheetRefusal(RuntimeError):
    """Base for a charsheet refusal an operator can act on.

    ``RuntimeError`` and not ``ValueError`` on purpose. The character verbs
    already raise ``ValueError`` for "your request was wrong" — an out-of-order
    stage, an unauthored direction, an unknown row key — and
    ``_error_code_for_exception`` reads a bare ``ValueError`` as
    ``invalid_request`` for that reason. Neither subclass below is a wrong
    request: the identical command succeeds later, unchanged.
    """

    code = "charsheet_refused"

    def __init__(self, message: str, *, safe_details: dict | None = None):
        super().__init__(message)
        self.safe_details = dict(safe_details or {})


class DraftBusy(CharsheetRefusal):
    """Another generation already holds this draft.

    The condition the per-draft lock exists to name (see
    :mod:`agent.charsheet.draft_lock`). ``safe_details`` carries the holder —
    ``pid``, ``host``, ``verb``, ``started``, ``age_seconds`` — and the ``lock``
    path, because the one recovery an operator may have to perform by hand is
    deleting a lock a crashed generation left behind, and a refusal that
    withheld the path would leave them guessing at it.

    The operator's next MOVE is to WAIT, which is why the character verbs hand
    back ``characters status`` as the ``next`` hint: it is the one command that
    answers what the running generation has landed so far.
    """

    code = "draft_busy"


class ProviderTimeout(CharsheetRefusal):
    """One image-provider call ran past the charsheet deadline.

    Raised by :func:`agent.charsheet.pipeline._generate_image`, the single
    provider seam. ``safe_details`` carries ``seconds`` (the deadline that
    expired) and ``prefix`` (which generation it was), so a receipt says which
    call was abandoned and how long it was given.

    Retryable in the plain sense: the same command run again may well succeed,
    and for ``rows`` the resume hint the batch already builds
    (``_characters_rows_next``) names the rows that never landed.
    """

    code = "provider_timeout"
