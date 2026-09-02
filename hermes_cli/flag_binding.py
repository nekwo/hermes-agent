"""absent is not empty — the argparse -> store seam, spelled once.

WHAT WENT WRONG, AND WHY A HELPER RATHER THAN A PATCH
=====================================================

``persona instance update-profile`` read its repeatable ``--skill`` as
``list(getattr(args, "skills", None) or [])``. ``action="append"`` yields
``None`` when the flag is absent, so that expression handed the store an empty
LIST on every call that never mentioned skills. ``PersonaInstanceStore.
update_profile``'s contract is ``if skills is not None or clear_skills:`` --
correct, and correctly read as "the caller sent a list, write it". So renaming
an agent cleared every skill it was assigned, silently. The store was never
wrong. The collapse happened in the layer whose entire job at that line is to
translate "absent" into "absent".

That instance is fixed. This module exists because the CLASS was not:

* the collapse has no single spelling. ``getattr(args, X, None) or []`` and
  ``args.X or []`` were both live, in eleven and seven places, and
  ``or list()`` is a third that nothing would have caught. A rule enforced
  against one spelling is a rule against a spelling, not a rule.
* two sites had already worked it out by hand and written the correct thing
  (``_cmd_persona_instance_update_profile``,
  ``_validated_set_skills_request``), each with its own paragraph explaining
  why. Nothing linked them, so the reasoning had to be re-derived per site --
  and the eighteen that did not re-derive it are exactly the collapses.
* absent-vs-empty is INVISIBLE in ``or []``. It is not that the sites chose
  wrong; it is that they never had to choose. Every one of the eighteen was
  checked when this landed and every one is safe today, which is the point:
  this is a trap, not an outage, and the next verb to walk into it will look
  exactly like the last eighteen did.

THE TWO READERS BELOW ARE RULINGS, NOT UTILITIES. Their names are the whole
mechanism: picking one is a decision a reviewer can see and check, where
``or []`` was a decision nobody knew they were making.

WHERE THE DISTINCTION HAS TO EXIST FIRST
========================================

A reader cannot recover what the parser threw away. ``add_argument(...,
action="append", default=[])`` makes ``args.X`` an empty list when the flag is
absent, so no command layer downstream can tell absence from emptiness -- and
argparse appends into that one default LIST OBJECT, shared by every parse in the
process. Any flag read through :func:`list_flag_or_absent` therefore has to be
declared ``default=None``, and that is checked against the real parser tree at
runtime by ``tests/hermes_cli/test_flag_binding_boundary.py`` rather than
asserted here in a comment.

SCOPE. List-valued flags only. The string form (``(getattr(args, X, None) or
"").strip()``) is a different question -- an empty string is a legal value far
less often than an empty list is -- and folding it in here would make one helper
answer two questions.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "flag_given",
    "list_flag_or_absent",
    "list_flag_or_empty",
]

_MISSING = object()


def _raw(args: Any, name: str) -> Any:
    """The attribute as argparse left it, with "no such attribute" folded into
    ``None``.

    A missing attribute and a ``None`` one are the same fact -- this parser has
    nothing to say about that flag -- and several handlers are shared by two
    parsers where only one declares the flag. That is why the call sites spell
    ``getattr(args, X, None)`` rather than ``args.X``; both forms arrive here.
    """

    value = getattr(args, name, _MISSING)
    return None if value is _MISSING else value


def _as_list(value: Any) -> list:
    """Normalize a given value to a list.

    A lone string is wrapped rather than iterated: ``nargs`` and ``append`` both
    produce lists, but a hand-built ``Namespace`` (a test, an RPC shim) can
    carry the bare value, and ``list("skill")`` silently becomes six
    single-character skills. Two call sites already carried their own
    ``isinstance(x, str)`` guard for exactly this.
    """

    if isinstance(value, str):
        return [value]
    return list(value)


def flag_given(args: Any, name: str) -> bool:
    """Did the caller supply this flag at all?

    The predicate the two readers below are built on, exported because a handler
    that has to branch on presence should ask this question in these words
    instead of re-deriving ``is not None`` beside its own default.
    """

    return _raw(args, name) is not None


def list_flag_or_absent(args: Any, name: str) -> list | None:
    """The values, or ``None`` when the flag was not given.

    THE READER FOR ANY FLAG WHOSE VALUE REACHES A STORE THAT TREATS
    ``is not None`` AS AN INSTRUCTION. ``None`` means "the caller said nothing
    about this, leave it alone"; ``[]`` means "the caller said: none", which for
    a replace-semantics field is a CLEAR and is a different write.

    Choose this one whenever you cannot show that the consumer is blind to the
    difference. It is the conservative half: at worst a store learns that a
    caller stayed silent, which it may then ignore.
    """

    value = _raw(args, name)
    if value is None:
        return None
    return _as_list(value)


def list_flag_or_empty(args: Any, name: str) -> list:
    """The values, with ABSENT DELIBERATELY COLLAPSED TO ``[]``.

    Correct only where the consumer genuinely cannot tell the two apart -- it
    iterates the list, or refuses on empty, or adds the values to something.
    Reviewing a call to this function means checking that claim about the
    consumer, which is precisely the check ``or []`` made invisible.

    If the values are handed to a store that writes a field, this is the wrong
    reader. Use :func:`list_flag_or_absent`.
    """

    value = _raw(args, name)
    if value is None:
        return []
    return _as_list(value)
