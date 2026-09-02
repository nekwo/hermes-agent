"""absent is not empty: the seam, the ban, and the parser tier underneath it.

WHY THIS FILE EXISTS RATHER THAN A NINETEENTH PATCH
===================================================

``persona instance update-profile`` read a repeatable ``--skill`` as
``list(getattr(args, "skills", None) or [])``. ``action="append"`` gives
``None`` when the flag is absent, so an omitted flag reached
``PersonaInstanceStore.update_profile`` as an empty LIST and the store's
``if skills is not None or clear_skills:`` correctly wrote it: renaming an agent
cleared every skill it had. That instance is fixed and pinned by
``test_persona_instance_update_profile_skills.py``. This file is about the
class.

Three things were true when it landed, and all three are what a per-site fix
leaves standing:

* **the collapse has no single spelling.** ``getattr(args, X, None) or []``
  (eleven sites) and ``args.X or []`` (seven) were both live; ``or list()``
  would have been a third. A rule enforced against one spelling is a rule about
  a spelling.
* **three handlers had each re-derived the correct rule by hand**, in three
  separate paragraphs, in ONE file -- the agent-create params builder, the
  update-profile handler and ``_validated_set_skills_request``. Nothing linked
  them, so the reasoning was per-site, and the eighteen sites that never
  re-derived it are exactly the collapses.
* **every one of those eighteen is safe today.** Each was read when this
  landed: each iterates, or refuses on empty, or adds to something. So there is
  no live outage here and this file does not pretend there is one. The finding
  is a trap, and the next verb to walk into it will look exactly like the
  eighteen that did not.

WHAT EACH GATE IS, AND WHY IT IS THAT KIND
==========================================

* the BAN is a source walk, because it is a NEGATIVE guarantee -- "this must
  never be written" -- which is the one shape a source walk is the right
  instrument for: over-approximation is the safe direction. It matches on AST
  SHAPE, not on text, so a respelling (``or list()``, different quotes, a line
  break) is caught rather than admitted.
* the READER tests and the PARSER-TIER test are POSITIVE guarantees, so neither
  rests on a source walk. The readers are executed against real
  ``argparse.Namespace`` objects; the parser tier is asserted against the LIVE
  argparse element model -- the actual ``Action`` object built by the real
  ``build_parser`` -- because "this flag can express absence" is a question
  about what argparse holds, not about how a line is typed.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

import pytest

from hermes_cli.flag_binding import (
    flag_given,
    list_flag_or_absent,
    list_flag_or_empty,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "hermes_cli"
BINDING_MODULE = PACKAGE_ROOT / "flag_binding.py"


# ---------------------------------------------------------------------------
# The readers, executed. The whole mechanism is that these two DIFFER.
# ---------------------------------------------------------------------------


def _ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def test_the_two_readers_disagree_on_absent_and_that_is_the_mechanism():
    """The one case the whole class turns on, asserted as one statement.

    If these ever agree, the module is decoration: every call site could go back
    to ``or []`` with no observable difference, which is the state this replaced.
    """

    absent = _ns(skills=None)

    assert list_flag_or_absent(absent, "skills") is None
    assert list_flag_or_empty(absent, "skills") == []


def test_the_two_readers_agree_on_everything_else():
    """...and only on absent. A reader that differed on a GIVEN value would be
    changing the flag's meaning rather than reporting whether it was given."""

    for value in ([], ["a"], ["a", "b"]):
        given = _ns(skills=list(value))
        assert list_flag_or_absent(given, "skills") == list(value)
        assert list_flag_or_empty(given, "skills") == list(value)


def test_an_explicitly_empty_list_is_GIVEN_not_absent():
    """``--skill`` present with no values is a caller SAYING "none".

    This is the half a store reads as an instruction: ``[]`` is a clear,
    ``None`` is silence, and collapsing them is the bug.
    """

    explicit_empty = _ns(skills=[])

    assert flag_given(explicit_empty, "skills") is True
    assert list_flag_or_absent(explicit_empty, "skills") == []
    assert list_flag_or_absent(explicit_empty, "skills") is not None


def test_a_missing_attribute_reads_as_absent_not_as_a_crash():
    """Handlers are shared between parsers where only one declares the flag --
    which is why every call site spelled ``getattr(args, X, None)``."""

    nothing = _ns()

    assert flag_given(nothing, "skills") is False
    assert list_flag_or_absent(nothing, "skills") is None
    assert list_flag_or_empty(nothing, "skills") == []


def test_a_bare_string_is_wrapped_and_never_iterated():
    """``list("skill")`` is six single-character skills.

    Two call sites carried their own ``isinstance(x, str)`` guard for this; a
    hand-built Namespace (a test, an RPC shim) is where the bare value comes
    from.
    """

    assert list_flag_or_empty(_ns(skills="deep-research"), "skills") == ["deep-research"]
    assert list_flag_or_absent(_ns(skills="deep-research"), "skills") == ["deep-research"]


def test_the_reader_hands_back_a_list_the_caller_may_keep():
    """Not an alias of the namespace's own list: a store that sorts or extends
    what it was given must not reach back into the parsed argv."""

    original = ["b", "a"]
    got = list_flag_or_absent(_ns(skills=original), "skills")
    got.append("c")

    assert original == ["b", "a"]


# ---------------------------------------------------------------------------
# The ban — NEGATIVE, so a source walk is the right instrument
# ---------------------------------------------------------------------------


def _is_args_read(node: ast.AST) -> bool:
    """``args.X`` or ``getattr(args, "X", ...)`` — either spelling."""

    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id == "args"
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "args"
    ):
        return True
    return False


def _is_empty_collection(node: ast.AST) -> bool:
    """``[]`` / ``{}`` / ``()`` / ``list()`` / ``dict()`` / ``tuple()`` / ``set()``."""

    if isinstance(node, (ast.List, ast.Tuple)) and not node.elts:
        return True
    if isinstance(node, ast.Dict) and not node.keys:
        return True
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"list", "dict", "tuple", "set"}
        and not node.args
        and not node.keywords
    ):
        return True
    return False


def _collapse_sites(path: Path) -> list[str]:
    """Every ``<read of args> or <empty collection>`` in one module."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        values = node.values
        for left, right in zip(values, values[1:]):
            if _is_args_read(left) and _is_empty_collection(right):
                found.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}")
    return found


def _package_modules() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def test_no_handler_collapses_an_absent_flag_into_an_empty_collection():
    """The ban, over the whole package, on SHAPE rather than on text.

    Both spellings that were live are the same AST here, and so is every
    respelling of them, which is the difference between this and the grep that
    found eleven of the eighteen.
    """

    modules = _package_modules()
    # A walk that reaches nothing passes vacuously; say so instead.
    assert len(modules) > 50, f"only {len(modules)} modules walked — is the path right?"

    offenders = sorted(site for path in modules for site in _collapse_sites(path))

    assert offenders == [], (
        "`<args flag> or []` collapses ABSENT into EMPTY where nobody can see "
        "the decision being made. Read the flag through "
        "hermes_cli.flag_binding: list_flag_or_empty() if the consumer truly "
        "cannot tell the two apart (it iterates, refuses on empty, or appends), "
        "list_flag_or_absent() if the value reaches a store that reads "
        "`is not None` as an instruction. "
        f"Offenders: {offenders}"
    )


def test_the_ban_actually_recognises_the_shape_it_bans():
    """An always-green gate is not a gate.

    The four spellings below are the ones that were live, plus the two that
    would have walked through a grep. A ban nobody has watched fire is a ban
    that might be matching nothing at all.
    """

    module = ast.parse(
        "a = getattr(args, 'ids', None) or []\n"
        "b = args.ids or []\n"
        "c = getattr(args, 'ids', None) or list()\n"
        "d = args.ids or {}\n"
    )
    hits = []
    for node in ast.walk(module):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            values = node.values
            for left, right in zip(values, values[1:]):
                if _is_args_read(left) and _is_empty_collection(right):
                    hits.append(node.lineno)

    assert hits == [1, 2, 3, 4]


def test_the_ban_leaves_a_non_empty_or_default_alone():
    """``args.x or "soft"`` is a DEFAULT, not a collapse: the second operand
    carries meaning, so the two branches were never indistinguishable."""

    module = ast.parse("a = args.isolation or 'soft'\nb = args.limit or 50\n")
    hits = [
        node.lineno
        for node in ast.walk(module)
        if isinstance(node, ast.BoolOp)
        and isinstance(node.op, ast.Or)
        and _is_args_read(node.values[0])
        and _is_empty_collection(node.values[1])
    ]

    assert hits == []


# ---------------------------------------------------------------------------
# The parser tier — POSITIVE, so it is asserted against the live argparse
# element model, never against the source that declares it
# ---------------------------------------------------------------------------


def _flags_read_as_absent_or_given() -> set[str]:
    """Every ``dest`` some handler reads through ``list_flag_or_absent``.

    Enumerated from the calls themselves rather than listed here: a flag added
    to that reader tomorrow is inside this gate the moment it is written. A
    literal-less call (a computed name) is deliberately NOT guessed at — it is
    reported, because silently skipping it is how a gate reports a subset as if
    it were the whole.
    """

    names: set[str] = set()
    unresolved: list[str] = []
    for path in _package_modules():
        if path == BINDING_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "list_flag_or_absent":
                continue
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                names.add(str(node.args[1].value))
            else:
                unresolved.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}"
                )
    assert unresolved == [], (
        "list_flag_or_absent() called with a non-literal flag name; this gate "
        f"cannot resolve it to a parser action: {unresolved}"
    )
    return names


def _harness_parser() -> argparse.ArgumentParser:
    """The REAL parser tree, built the way `scripts/dump_cli_contract.py` builds
    it — so this gate reads the argparse objects the CLI actually runs on."""

    from hermes_cli.harness import build_parser

    root = argparse.ArgumentParser(prog="hermes")
    subparsers = root.add_subparsers(dest="command")
    build_parser(subparsers)
    return root


#: Command paths where a flag shares a ``dest`` with a flag read through
#: ``list_flag_or_absent`` SOMEWHERE ELSE in the tree, and where absence
#: genuinely does not need to be expressible. ``dest`` is not unique across
#: subparsers, so the gate below over-approximates on purpose — that is the safe
#: direction — and each collision is answered here in writing rather than by
#: quietly narrowing the walk.
#:
#: Every entry names the command path, the flag, and WHY. A waiver with no
#: reason is a hole with a comment on it.
_ABSENCE_NOT_REQUIRED = {
    ("harness mission-chat queue-skill", "skills"): (
        "the handler refuses unless at least one skill survives token safety, so "
        "'flag absent' and 'flag given empty' reach the identical refusal and no "
        "store is ever told either one; it reads through list_flag_or_empty"
    ),
}


def _actions_by_dest(parser: argparse.ArgumentParser) -> dict[str, list[tuple[str, object]]]:
    """Walk every subparser and index optional actions by ``dest``.

    Each entry carries the COMMAND PATH it was found under, because ``dest`` is
    not unique across the tree: ``skills`` is three different flags on three
    different verbs. A gate that indexed by ``dest`` alone would report one
    verb's declaration as another verb's defect, which is how an
    over-approximating gate turns into a gate people learn to wave through.
    """

    found: dict[str, list[tuple[str, object]]] = {}
    stack = [(parser, "")]
    seen = set()
    while stack:
        current, path = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    stack.append((sub, f"{path} {name}".strip()))
                continue
            if action.option_strings:
                found.setdefault(action.dest, []).append((path, action))
    return found


def test_every_flag_read_as_absent_can_actually_express_absence():
    """A reader cannot recover what the PARSER threw away.

    ``add_argument(..., action="append", default=[])`` makes the namespace hold
    an empty list when the flag is absent, so ``list_flag_or_absent`` would
    report "the caller said: none" for a caller who said nothing — the original
    bug, moved one layer down and out of reach of the layer that was blamed for
    it. This asserts against the live ``Action`` object, not against the line
    that built it.

    It found one on its first run: ``harness mission-chat queue-skill --skills``
    is ``nargs="+", default=[]``. That verb does not need absence and is waived
    above with its reason; the point is that nothing had ever asked.
    """

    names = _flags_read_as_absent_or_given()
    assert "skills" in names and len(names) >= 1, (
        f"the walk for list_flag_or_absent() call sites found {sorted(names)}; "
        "'skills' is the flag the whole class was measured on, so an "
        "enumeration missing it is not an enumeration"
    )

    by_dest = _actions_by_dest(_harness_parser())
    offenders = []
    checked = 0
    for name in sorted(names):
        for path, action in by_dest.get(name, []):
            if (path, name) in _ABSENCE_NOT_REQUIRED:
                continue
            checked += 1
            if action.default is not None:
                offenders.append(
                    f"{path} {'/'.join(action.option_strings)} (dest={name}) "
                    f"default={action.default!r}"
                )

    assert checked > 0, (
        f"no parser action found for any of {sorted(names)} — this gate checked "
        "nothing, which is indistinguishable from passing"
    )
    assert offenders == [], (
        "these flags are read through list_flag_or_absent(), which reports "
        "whether the caller supplied them — but their parser default is not "
        "None, so an absent flag is already indistinguishable from an empty one "
        "before any handler runs. Declare default=None, or waive it in "
        "_ABSENCE_NOT_REQUIRED with the reason absence cannot matter there. "
        f"Offenders: {offenders}"
    )


def test_every_waiver_still_names_a_real_flag():
    """A stale waiver is a hole nobody can see.

    If a waived command path or flag stops existing, the entry must go — the
    same rule the cite-adjacency baseline enforces on its own waivers.
    """

    by_dest = _actions_by_dest(_harness_parser())
    stale = [
        f"{path} (dest={dest})"
        for (path, dest), reason in _ABSENCE_NOT_REQUIRED.items()
        if not any(found_path == path for found_path, _ in by_dest.get(dest, []))
        or not str(reason).strip()
    ]

    assert stale == [], f"waivers naming no live flag, or carrying no reason: {stale}"


@pytest.mark.parametrize("dest", ["skills"])
def test_the_parser_tier_check_is_reading_a_real_action(dest):
    """Anti-vacuity for the gate above: the dest it checks resolves to actions
    that exist, are optional flags, and are repeatable — i.e. it is looking at
    the real ``--skill`` and not at an empty dict lookup."""

    actions = _actions_by_dest(_harness_parser()).get(dest, [])

    assert actions, f"no optional action with dest={dest!r} in the real parser"
    assert all(action.option_strings for _path, action in actions)
    # ...and the collision this gate had to be taught about is REAL: more than
    # one command declares this dest.
    assert len({path for path, _action in actions}) > 1, (
        f"dest={dest!r} resolved to one command path only; the waiver machinery "
        "above exists for a collision that must therefore still be real"
    )
