"""Nothing on the harness surface may be advertised and unreachable.

Two gates, one rule, two levels of the same ladder.

LEVEL 1 — the flag (`test_every_harness_flag_has_a_reader`)
============================================================
`_add_stage42_global_args`' own docstring already states the rule for the
GLOBAL flags: *"a flag registered here is a PROMISE made on every one of ~60
verbs at once... An operator who reaches for it gets the unfiltered answer and
no signal that the flag was ignored — the failure mode is a WRONG ANSWER
believed, not an error seen."* `--filter` and `--watch` were removed in 2026-07
for exactly that.

`test_harness_cli.py::test_every_stage42_global_flag_is_honored` enforces it —
but only across the stage42 registrations. `harness install-harness-skills`
never calls `_add_stage42_global_args`, so it is not a stage42 verb, so its own
`add_argument` calls were invisible to that gate. It carried
`--all-persona-profiles`, self-described in its help text as a "Compatibility
flag", whose dest no handler read. It was advertised for as long as it existed
and did nothing. This gate reads **every** `add_argument` on **every**
subparser in `harness.py`, stage42 or not.

LEVEL 2 — the control token (`test_every_stage42_control_token_has_a_caller`)
=============================================================================
One level up from a flag: `controls=frozenset({...})` decides which verbs a
global flag is even offered to. A token branched on inside
`_add_stage42_global_args` that no call site ever names is an option no verb
can take — the same "advertised, unreachable" defect, one indirection higher
and correspondingly harder to see. `cursor` and `since` were both that, from
the day the helper was written until 2026-08-19: 58 call sites, five tokens
between them, and neither of those two ever appeared.

Both gates read the AST rather than the source text, because the subject in
each case is a name in a specific syntactic position — a `dest`, a branch
subject, a call argument — and a text scan cannot tell those from the same
word in a docstring explaining why the flag was removed.

THE THIRD READER SPELLING (2026-09-02)
======================================
Level 1's census read two spellings — ``args.<dest>`` and the ``getattr(args,
"<dest>")`` string form — and `a3b48a06a2` invented a third. That commit routed
twenty-five flag reads through :mod:`hermes_cli.flag_binding`, where the dest is
a STRING ARGUMENT to a named reader (``list_flag_or_empty(args, "proof_ids")``)
and the ``getattr`` happens one frame down inside ``flag_binding._raw`` against
a variable. Six live flags — ``persona instance return``'s ``--proof-id`` /
``--artifact-ref``, ``roots migrate``'s ``--config`` / ``--root``,
``workspace create``'s ``--agent`` and ``skills delete``'s ``--realm`` — went
from read to invisible without a single reader being removed, and this gate
reported six working flags as advertised-and-unreachable.

The class the census missed is therefore **a dest named as a string constant in
the name slot of a declared reader**, and the fix is not to start counting bare
string constants: that is still the vacuous-gate hole this file's original
docstring names, and admitting it would let a retirement note keep a dead flag's
gate green. Only calls to the readers ``hermes_cli.flag_binding`` exports count,
the name is read from the argument position that function's signature calls
``name``, and a call whose name is computed rather than literal is REPORTED
rather than skipped — a census that silently drops what it cannot resolve is
back to reporting a subset as if it were the whole.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from hermes_cli import flag_binding

HARNESS_ROOT = pathlib.Path(__file__).resolve().parents[2] / "hermes_cli"
HARNESS = HARNESS_ROOT / "harness.py"

#: Where a handler can legitimately read `args.<dest>`: the parser file, the
#: shared support module, and the six exec'd command parts.
_LANE = [
    HARNESS,
    HARNESS_ROOT / "harness_support.py",
    *sorted((HARNESS_ROOT / "harness_parts").glob("*.py")),
]


def _harness_tree() -> ast.Module:
    return ast.parse(HARNESS.read_text(encoding="utf-8"))


def _registrations() -> list[tuple[str, str, int]]:
    """`(subparser var, dest, lineno)` for every `add_argument` in harness.py."""

    out: list[tuple[str, str, int]] = []
    for node in ast.walk(_harness_tree()):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            continue
        owner = getattr(node.func.value, "id", None)
        dest = None
        for kw in node.keywords:
            if kw.arg == "dest" and isinstance(kw.value, ast.Constant):
                dest = kw.value.value
        if dest is None:
            flags = [
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            if not flags:
                continue
            long = [flag for flag in flags if flag.startswith("--")] or flags
            dest = long[0].lstrip("-").replace("-", "_")
        if owner:
            out.append((owner, dest, node.lineno))
    return out


def _flag_binding_readers() -> dict[str, int]:
    """`{reader name: index of its flag-name argument}`, from the real module.

    Read off ``flag_binding.__all__`` and the live signatures rather than
    hardcoded here, so a fourth reader added to that seam tomorrow is inside
    this census the moment it is written — the drift that produced the six-flag
    red in the first place was exactly a list of spellings maintained by hand.
    The signature is what supplies the argument index, and a reader that does
    not take ``(args, name)`` fails configuration below instead of being counted
    at a position it does not have.
    """

    readers: dict[str, int] = {}
    for name in flag_binding.__all__:
        params = list(inspect.signature(getattr(flag_binding, name)).parameters)
        assert params[:2] == ["args", "name"], (
            f"hermes_cli.flag_binding.{name} exports the shape "
            f"{params} — this census reads the flag name out of the `name` "
            "argument, so a reader with a different shape has to be taught here "
            "rather than silently counted at the wrong position"
        )
        readers[name] = 1
    return readers


def _dests_read_on_the_lane() -> tuple[set[str], list[str]]:
    """The reads across the whole lane, plus the reader calls it could not resolve.

    Three spellings, and no fourth. Attribute access, the `getattr` string form,
    and a string in the `name` slot of a `hermes_cli.flag_binding` reader. Bare
    string constants ANYWHERE ELSE are deliberately excluded: counting them
    would let the retirement note that NAMES a removed flag keep that flag's
    gate green — the vacuous-gate class the tombstone registry's own scanner
    exists to avoid. A reader is different from a note precisely because it is a
    call that runs.
    """

    reads: set[str] = set()
    unresolved: list[str] = []
    for path in _LANE:
        found, blind = _reads_in_source(path.read_text(encoding="utf-8", errors="replace"))
        reads |= found
        unresolved += [f"{path.name}:{row}" for row in blind]
    return reads, unresolved


def _reads_in_source(source: str) -> tuple[set[str], list[str]]:
    """One module's worth of the census, over source text so it can be proven."""

    readers = _flag_binding_readers()
    reads: set[str] = set()
    unresolved: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            reads.add(node.attr)
            continue
        if not isinstance(node, ast.Call):
            continue
        called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if called == "getattr":
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                reads.add(node.args[1].value)
            continue
        if called not in readers:
            continue
        slot = readers[called]
        if len(node.args) > slot and isinstance(node.args[slot], ast.Constant):
            reads.add(node.args[slot].value)
        else:
            unresolved.append(f"{node.lineno} {called}(...)")
    return reads, unresolved


def test_every_harness_flag_has_a_reader():
    registrations = _registrations()
    assert len(registrations) > 300, (
        f"only {len(registrations)} argparse registrations found in "
        f"{HARNESS} — the scan is not seeing the parser, so this gate would "
        "pass on any tree"
    )
    reads, unresolved = _dests_read_on_the_lane()
    assert len(reads) > 500, "the reader scan found almost nothing — vacuous"

    unread = sorted(
        {f"{HARNESS.name}:{line} {owner}.{dest}" for owner, dest, line in registrations if dest not in reads}
    )
    hint = ""
    if unread and unresolved:
        hint = (
            "\nNOTE: these flag_binding reader calls name their flag with "
            "something other than a string literal, so this census could not "
            "credit whatever dest they read — check them before believing the "
            "list above:\n" + "\n".join("  " + row for row in unresolved)
        )
    assert unread == [], (
        "these harness flags are advertised in --help and read by no handler. "
        "An operator who reaches for one gets the unfiltered answer with no "
        "signal that it was ignored:\n" + "\n".join("  " + row for row in unread) + hint
    )


def test_the_census_credits_a_flag_binding_reader_and_still_ignores_a_bare_string():
    """Both directions of the third spelling, because it only has one job.

    The left half is the six-flag red: a dest named to a declared reader IS a
    reader. The right half is the hole this file has always refused — the
    retirement note, the tombstone row, the docstring naming a flag that is
    gone. If the second assertion ever flips, this gate stops being able to
    catch a deleted reader at all, which is the only defect it exists for.
    """

    credited, unresolved = _reads_in_source(
        "proof_ids = list_flag_or_empty(args, 'proof_ids')\n"
        "realms = flag_binding.list_flag_or_absent(args, 'realms')\n"
        "given = flag_given(args, 'root')\n"
    )
    assert {"proof_ids", "realms", "root"} <= credited
    assert unresolved == []

    noted, _ = _reads_in_source(
        "#: --all-persona-profiles was removed in 2026-07\n"
        "TOMBSTONES = ['all_persona_profiles']\n"
        "helper('all_persona_profiles')\n"
    )
    assert "all_persona_profiles" not in noted


def test_the_census_reports_a_reader_whose_flag_name_it_cannot_resolve():
    """A computed name is a read the census cannot credit — say so, don't drop it.

    Silently skipping it would leave the flag looking unread and the failure
    message pointing at the parser instead of at the call that made the census
    blind.
    """

    reads, unresolved = _reads_in_source("v = list_flag_or_empty(args, name)\n")

    assert unresolved == ["1 list_flag_or_empty(...)"]
    assert "name" not in reads


def _control_tokens_branched_on() -> set[str]:
    """The tokens `_add_stage42_global_args` tests `controls` membership for."""

    for node in ast.walk(_harness_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == "_add_stage42_global_args":
            return {
                test.left.value
                for test in ast.walk(node)
                if isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Constant)
                and isinstance(test.left.value, str)
                and any(isinstance(op, ast.In) for op in test.ops)
            }
    raise AssertionError("_add_stage42_global_args not found — the gate is vacuous")


def _control_tokens_asked_for() -> tuple[set[str], int]:
    """Every token any `_add_stage42_global_args(...)` call site names, + count."""

    tokens: set[str] = set()
    sites = 0
    for node in ast.walk(_harness_tree()):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_add_stage42_global_args":
            sites += 1
            for kw in node.keywords:
                if kw.arg != "controls":
                    continue
                tokens.update(
                    child.value
                    for child in ast.walk(kw.value)
                    if isinstance(child, ast.Constant) and isinstance(child.value, str)
                )
    return tokens, sites


def test_every_stage42_control_token_has_a_caller():
    branched = _control_tokens_branched_on()
    asked, sites = _control_tokens_asked_for()
    assert sites > 40, f"only {sites} call sites found — the gate is vacuous"
    assert branched, "no control branches found — the gate is vacuous"

    unreachable = sorted(branched - asked)
    assert unreachable == [], (
        "_add_stage42_global_args branches on these control tokens and no call "
        f"site names any of them, so the flags behind them can never be "
        f"registered on any of the {sites} verbs: {unreachable}. Either wire a "
        "verb to ask for it, or delete the branch — an option no caller can "
        "take is the advertised-and-unreachable defect one level up from an "
        "unread flag."
    )


def test_the_control_gate_notices_a_token_that_is_asked_for_but_not_branched_on():
    """The other direction, so this pair cannot only say yes.

    A call site naming a token the helper does not branch on is the same
    promise broken from the caller's end: the verb believes it opted in.
    """

    branched = _control_tokens_branched_on()
    asked, _ = _control_tokens_asked_for()
    orphaned = sorted(asked - branched)
    assert orphaned == [], (
        "these verbs ask for control tokens `_add_stage42_global_args` does "
        f"not branch on, so the opt-in silently does nothing: {orphaned}"
    )
