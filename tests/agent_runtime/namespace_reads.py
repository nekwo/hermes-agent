"""Which argparse dests a handler READS — asked once, in every spelling.

A gate that says "this retired flag is no longer read" has to recognise a read,
and a read of an argparse namespace has THREE spellings in this tree:

* ``args.<dest>`` — attribute access;
* ``getattr(args, "<dest>", ...)`` — the string form, used wherever one handler
  is shared by two parsers and only one of them declares the flag;
* ``list_flag_or_empty(args, "<dest>")`` — a dest named as a string constant in
  the ``name`` slot of a reader :mod:`hermes_cli.flag_binding` exports, where
  the ``getattr`` happens a frame down against a variable.

The third one arrived with ``a3b48a06a2``, which re-spelled twenty-five reads at
once. Every census in the tree keyed on one of the first two went blind that
day: ``test_harness_flag_and_control_reachability.py`` reported six live flags
as unreachable (fixed in ``858c12c7a0``), and the retirement gates in
``test_s26_retired_mission_chat_task_goal_flags.py`` /
``test_s27_tool_diff_task_goal_flags.py`` — which assert a read is GONE — would
have stayed green through a re-spelled read of the very flag they retire. Each
of those two knew only ONE spelling: s26 the ``getattr`` form, s27 the
attribute form, each matching whatever happened to be typed in the handler when
it was written.

So the rule lives here rather than three times over, and the reader names are
read off ``flag_binding.__all__`` and the live signatures rather than typed out:
a list of spellings maintained by hand is precisely what went blind. The scope
is deliberately narrow — reads of the ``args`` namespace, in a function's own
body. Bare string constants anywhere else are NOT reads: crediting one would
let a retirement note that names a removed flag keep that flag's gate green,
which is the vacuous half of the same defect.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from hermes_cli import flag_binding

#: ``{reader name: index of its flag-name argument}``, from the real module.
#: A reader whose first two parameters are not ``(args, name)`` fails
#: configuration here rather than being counted at a position it does not have.
_READERS: dict[str, int] = {}
for _name in flag_binding.__all__:
    _params = list(inspect.signature(getattr(flag_binding, _name)).parameters)
    assert _params[:2] == ["args", "name"], (
        f"hermes_cli.flag_binding.{_name} exports the shape {_params} — this "
        "census reads the dest out of the `name` argument, so a reader with a "
        "different shape has to be taught here rather than silently counted at "
        "the wrong position"
    )
    _READERS[_name] = 1

FLAG_BINDING_READERS = frozenset(_READERS)


def namespace_reads(source: str, *, namespace: str = "args") -> set[str]:
    """Every dest *source* reads off ``args``, in all three spellings.

    *source* is one function's text (``inspect.getsource``) or a module's. A
    reader call whose flag name is computed rather than literal is not guessed
    at — :func:`unresolved_reader_calls` reports it, so a caller can refuse
    rather than silently treat a subset as the whole.
    """

    reads: set[str] = set()
    for node in ast.walk(ast.parse(_dedent(source))):
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == namespace:
                reads.add(node.attr)
            continue
        if not isinstance(node, ast.Call):
            continue
        called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if called == "getattr":
            if (
                len(node.args) > 1
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == namespace
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                reads.add(node.args[1].value)
            continue
        slot = _READERS.get(called)
        if slot is None:
            continue
        if len(node.args) > slot and isinstance(node.args[slot], ast.Constant):
            reads.add(str(node.args[slot].value))
    return reads


def unresolved_reader_calls(source: str) -> list[str]:
    """``flag_binding`` reader calls whose flag name is not a string literal.

    A census that silently drops what it cannot resolve reports a subset as if
    it were the whole. A gate asserting a read is GONE must treat one of these
    as "I cannot tell", never as "absent".
    """

    unresolved: list[str] = []
    for node in ast.walk(ast.parse(_dedent(source))):
        if not isinstance(node, ast.Call):
            continue
        called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        slot = _READERS.get(called)
        if slot is None:
            continue
        if len(node.args) <= slot or not isinstance(node.args[slot], ast.Constant):
            unresolved.append(f"{node.lineno} {called}(...)")
    return unresolved


def _dedent(source: str) -> str:
    """``inspect.getsource`` of a method is indented and would not parse."""

    return textwrap.dedent(source)
