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
"""

from __future__ import annotations

import ast
import pathlib

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


def _dests_read_on_the_lane() -> set[str]:
    """`args.<name>` and `getattr(args, "<name>")` across the whole lane.

    Attribute access and the `getattr` string form ONLY. Bare string constants
    are deliberately excluded: counting them would let the retirement note that
    NAMES a removed flag keep that flag's gate green — the vacuous-gate class
    the tombstone registry's own scanner exists to avoid.
    """

    reads: set[str] = set()
    for path in _LANE:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                reads.add(node.attr)
            elif (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "getattr"
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
            ):
                reads.add(node.args[1].value)
    return reads


def test_every_harness_flag_has_a_reader():
    registrations = _registrations()
    assert len(registrations) > 300, (
        f"only {len(registrations)} argparse registrations found in "
        f"{HARNESS} — the scan is not seeing the parser, so this gate would "
        "pass on any tree"
    )
    reads = _dests_read_on_the_lane()
    assert len(reads) > 500, "the reader scan found almost nothing — vacuous"

    unread = sorted(
        {f"{HARNESS.name}:{line} {owner}.{dest}" for owner, dest, line in registrations if dest not in reads}
    )
    assert unread == [], (
        "these harness flags are advertised in --help and read by no handler. "
        "An operator who reaches for one gets the unfiltered answer with no "
        "signal that it was ignored:\n" + "\n".join("  " + row for row in unread)
    )


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
