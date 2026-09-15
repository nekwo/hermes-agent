"""Walk hermes's own argparse tree and emit the CLI contract as JSON.

WHY THIS LIVES IN HERMES
------------------------
The launcher's Mission Control renders every hermes argv from a DECLARED
template (`HarnessCapabilitySpec` / `HarnessArgvTemplate`) and checks each
rendered vector against a COMMITTED dump of this tree
(`test/features/mission_control/fixtures/hermes_cli_contract.json`). That gate
found seven shipped operator buttons that hermes rejected with argparse exit 2.

It has one structural hole, and the hole is on THIS side of the wire: the dump
is committed in the launcher, so **a hermes-side argparse change leaves every
launcher test green while the fixture lies.** Only a hand-run refresh notices,
and a hand-run refresh is not a mechanism -- measured 2026-08-30, when the
`--message` deletion on `persona instance create` (`ab6254643`) left the
fixture stale for three days across five hermes commits and six changes, and
was found by a human going looking.

A gate cannot watch a repo it cannot see move. So hermes commits its own dump
of its own parsers and checks it on its own push lane: the day a parser moves
without a regen, **hermes** goes red, in the repo that moved. The launcher's
fixture then has a same-repo artifact to be compared against on sync instead of
a personal checkout path nobody else can reproduce.

WHAT THE TWO DUMPS ARE TO EACH OTHER
------------------------------------
This walker and the launcher's `tool/hermes_cli_contract/
dump_hermes_cli_contract.py` are the SAME walker and emit byte-identical JSON
for one hermes checkout; that equality was verified when this file landed and
is what makes an on-sync comparison meaningful. Two copies of one walker is a
duplicated authority and is not the end state: the launcher's runner should
exec THIS script out of `--hermes-root` and delete its copy, which retires the
duplication in the direction of the repo that owns the parsers. That handback
is rowed in the launcher's Mission Control queue. Until it lands, **a change to
the walk belongs in both files or in neither** -- and the on-sync comparison is
what says so out loud.

CONTRACT
--------
stdout is exactly one JSON object; every diagnostic goes to stderr. Writing is
newline-explicit (``newline="\\n"``), because Python text-mode writes flip
LF->CRLF on Windows and the fixture is an LF artifact compared byte-for-byte.

    python scripts/dump_cli_contract.py                 # JSON to stdout
    python scripts/dump_cli_contract.py --check         # gate: fixture is fresh
    python scripts/dump_cli_contract.py --write         # regenerate the fixture

`--hermes-root` defaults to this script's own repo, which is the checkout whose
parsers it is walking. It stays overridable so the launcher's runner can point
at a chosen checkout without an editable install silently becoming the
authority for someone else's fixture refresh.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]

#: The committed dump this repo gates on. It is a TEST FIXTURE, not a doc: the
#: only reader that must never drift from it is the freshness check below.
DEFAULT_FIXTURE = "tests/fixtures/hermes_cli_contract.json"


def _register_builders(subparsers: argparse._SubParsersAction) -> list[str]:
    """Attach every top-level parser the launcher can emit into.

    Add a builder here the day the launcher learns to emit a new top-level
    hermes command; a path the dump does not cover is a path the conformance
    test cannot police.
    """
    registered: list[str] = []

    from hermes_cli.harness import build_parser as build_harness_parser

    build_harness_parser(subparsers)
    registered.append("harness")

    # `persona.profile.delete` emits `hermes profile delete <name> --yes`,
    # which is NOT under `harness`.
    from hermes_cli.subcommands.profile import build_profile_parser

    build_profile_parser(subparsers, cmd_profile=lambda args: None)
    registered.append("profile")

    return registered


def _nargs(action: argparse.Action) -> Any:
    """argparse's nargs, normalized to something JSON can hold.

    `None` means "exactly one value" for both options and positionals; 0 means a
    flag that stores a constant (`--rescope`, `--json`). Preserving the
    difference is what lets the conformance test tell a flag's VALUE apart from
    a positional when it walks an emitted argv.
    """
    return action.nargs if action.nargs is not None else 1


def _choices(action: argparse.Action) -> Any:
    """The literal values argparse will accept, when it constrains them.

    Emitting a value outside `choices=` is the same exit-2 class as an unknown
    flag ("invalid choice"), and the launcher hardcodes several of these
    literals (`--intent-hint chat`, `--mode unbounded`, `--state created`).
    """
    if action.choices is None:
        return None
    # Subparser choices are commands, handled separately.
    if isinstance(action.choices, dict):
        return None
    return sorted(str(choice) for choice in action.choices)


def _type(action: argparse.Action) -> Any:
    """The name of argparse's `type=` coercion, or None when it takes strings.

    argparse runs `type(value)` on every value it consumes and exits 2 when the
    call raises: `--expect-revision abc` is
    "argument --expect-revision: invalid int value: 'abc'" -- the same dead
    button as an unknown flag. Recording the CALLABLE'S NAME rather than the
    callable keeps the fixture JSON and keeps the reader honest: a `type=` the
    consumer does not simulate is visible in the dump instead of being
    indistinguishable from `type=None`.
    """
    if action.type is None:
        return None
    return getattr(action.type, "__name__", None) or str(action.type)


def _action_names(parser: argparse.ArgumentParser) -> dict[type, str]:
    """argparse's OWN class -> action-name map, read off the parser's registry.

    Not a table this file maintains. `add_argument(action="append")` resolves
    through `parser._registries["action"]`, so inverting that registry gives
    argparse's exact vocabulary ("store", "append", "store_true", "count", ...)
    rather than a second spelling of it that is free to drift. A custom action
    class the registry does not name falls back to the class name in
    [_action], which is the same "record it, do not guess it" rule `_type`
    follows.

    `None` is registered alongside `"store"` for the same class and is skipped:
    the default action has a name, and it is `"store"`.
    """
    names: dict[type, str] = {}
    for name, cls in parser._registries.get("action", {}).items():  # noqa: SLF001
        if not isinstance(name, str):
            continue
        names.setdefault(cls, name)
    return names


def _action(action: argparse.Action, names: dict[type, str]) -> str:
    """What argparse DOES with each occurrence of this argument.

    The rejection classes this dump records are all exit 2. This one is not:
    repeating a `store` flag is exit 0 with the earlier value silently
    OVERWRITTEN, while repeating an `append` flag accumulates. Without this key
    `--skill` (append) and any plain `store` flag are the same row.
    """
    return names.get(type(action)) or type(action).__name__


def _describe_positional(
    action: argparse.Action, action_names: dict[type, str]
) -> dict[str, Any]:
    return {
        "name": action.dest,
        "nargs": _nargs(action),
        "required": action.required,
        "choices": _choices(action),
        "type": _type(action),
        "action": _action(action, action_names),
    }


def _exclusive_groups(parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
    """The parser's mutually-exclusive groups, as `{required, opts}`.

    argparse rejects TWO members of one group ("argument --all: not allowed
    with argument --direction") and, when the group is `required=True`, rejects
    ZERO of them ("one of the arguments --direction --all is required"). Both
    are the same exit-2 class the rest of this dump covers.

    Members are recorded by their CANONICAL (longest) spelling, matching the
    `canonical` field on each option, so an alias pair reports as one member
    rather than several.
    """
    groups: list[dict[str, Any]] = []
    for group in parser._mutually_exclusive_groups:  # noqa: SLF001
        opts = sorted(
            {
                max(action.option_strings, key=len)
                for action in group._group_actions  # noqa: SLF001
                if action.option_strings
            }
        )
        # A group of positionals is not expressible in argparse, and a group of
        # one constrains nothing; neither can produce a violation.
        if len(opts) < 2:
            continue
        groups.append({"required": bool(group.required), "opts": opts})
    groups.sort(key=lambda entry: entry["opts"])
    return groups


def _walk(
    parser: argparse.ArgumentParser,
    path: list[str],
    out: dict[str, Any],
    action_names: dict[type, str],
) -> None:
    options: dict[str, Any] = {}
    positionals: list[dict[str, Any]] = []
    subparser_actions: list[argparse._SubParsersAction] = []

    for action in parser._actions:  # noqa: SLF001 -- argparse exposes no public walk.
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            subparser_actions.append(action)
            continue
        if action.option_strings:
            nargs = _nargs(action)
            choices = _choices(action)
            # `required=True` on an OPTION: omitting it is argparse "the
            # following arguments are required", exit 2 -- the same
            # operator-visible failure as an unknown flag. The canonical flag
            # (the longest, e.g. `--reason` over `-r`) carries the requirement
            # so the test can name what to send.
            canonical = max(action.option_strings, key=len)
            coercion = _type(action)
            kind = _action(action, action_names)
            for flag in action.option_strings:
                options[flag] = {
                    "nargs": nargs,
                    "choices": choices,
                    "required": bool(action.required),
                    "canonical": canonical,
                    "type": coercion,
                    "action": kind,
                }
        else:
            positionals.append(_describe_positional(action, action_names))

    subcommands: list[str] = []
    for action in subparser_actions:
        subcommands.extend(action.choices.keys())

    out[" ".join(path)] = {
        "path": list(path),
        "options": dict(sorted(options.items())),
        "positionals": positionals,
        "subcommands": sorted(subcommands),
        "exclusive_groups": _exclusive_groups(parser),
    }

    for action in subparser_actions:
        for name, subparser in action.choices.items():
            _walk(subparser, [*path, name], out, action_names)


def build_contract(root: Path, *, quiet: bool = False) -> dict[str, Any]:
    """Import `root`'s parsers and return the contract object.

    The root is validated and pushed to the FRONT of `sys.path` so an unrelated
    editable install cannot silently become the authority for a dump.
    """
    if not (root / "hermes_cli" / "__init__.py").is_file():
        raise SystemExit(
            f"invalid Hermes checkout {root}: hermes_cli/__init__.py is missing"
        )
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    parser = argparse.ArgumentParser(prog="hermes", add_help=True)
    subparsers = parser.add_subparsers(dest="command")
    registered = _register_builders(subparsers)

    action_names = _action_names(parser)
    commands: dict[str, Any] = {}
    for name in registered:
        _walk(subparsers.choices[name], [name], commands, action_names)

    if not quiet:
        print(f"[dump] hermes checkout: {root}", file=sys.stderr)
        print(
            f"[dump] roots: {', '.join(registered)} -- {len(commands)} command paths",
            file=sys.stderr,
        )

    return {
        # v2 (2026-08-24): every command gained `exclusive_groups`. The bump
        # is deliberate -- a v1 fixture has no way to say "this command has
        # no groups" apart from "this dump could not see groups at all",
        # and the Dart reader refuses the older schema rather than treat
        # silence as absence.
        #
        # v3 (2026-08-24): every option and positional gained `type`, for
        # the same reason one bump later. A v2 fixture is silent about
        # argparse's `type=` coercion, and a reader that treated silence as
        # `type=None` would report `board card move --expect-revision abc`
        # as clean.
        #
        # v4 (2026-09-01): every option and positional gained `action`.
        # The dump recorded what argparse would REJECT and nothing about
        # what it silently ACCEPTS: `--skill` (action="append") and a
        # plain `store` flag were the same row.
        #
        # The version string is shared with the launcher's reader, which
        # refuses an older schema rather than defaulting. Do not bump it on
        # one side only.
        "schema": "hermes_cli_contract/v4",
        "roots": registered,
        "commands": commands,
    }


def render(contract: dict[str, Any]) -> str:
    """The exact bytes the fixture holds: sorted keys, indent 2, one trailing LF.

    Pinned here rather than at each call site because the whole point of the
    artifact is a byte comparison -- against the previous commit's, and against
    the launcher's copy of the same walk.
    """
    buf = io.StringIO()
    json.dump(contract, buf, indent=2, sort_keys=True)
    buf.write("\n")
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    own_parser = argparse.ArgumentParser(
        prog="dump_cli_contract.py",
        description="Dump hermes's argparse tree; gate the committed fixture on it.",
    )
    own_parser.add_argument(
        "--hermes-root",
        default=str(REPO_ROOT),
        help="checkout whose parsers to walk (default: this script's repo)",
    )
    own_parser.add_argument(
        "--fixture",
        default=DEFAULT_FIXTURE,
        help=f"committed dump, relative to --hermes-root (default: {DEFAULT_FIXTURE})",
    )
    mode = own_parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the committed fixture does not match the live parsers",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="regenerate the committed fixture from the live parsers",
    )
    own_args = own_parser.parse_args(argv)

    root = Path(own_args.hermes_root).expanduser().resolve()
    contract = build_contract(root, quiet=own_args.check)
    rendered = render(contract)

    if not (own_args.check or own_args.write):
        sys.stdout.write(rendered)
        return 0

    fixture = (root / own_args.fixture).resolve()

    if own_args.write:
        fixture.parent.mkdir(parents=True, exist_ok=True)
        with open(fixture, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
        digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        print(f"[dump] wrote {fixture}", file=sys.stderr)
        print(f"[dump] sha256 {digest}", file=sys.stderr)
        return 0

    # --check: the freshness gate.
    if not fixture.is_file():
        print(
            f"CLI CONTRACT: the committed dump is MISSING at {fixture}.\n"
            "  Regenerate it:  python scripts/dump_cli_contract.py --write",
            file=sys.stderr,
        )
        return 1

    with open(fixture, "r", encoding="utf-8", newline="") as handle:
        committed = handle.read()

    if committed == rendered:
        digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        paths = len(contract["commands"])
        print(
            f"CLI contract fresh: {paths} command paths, sha256 {digest[:16]}",
            file=sys.stderr,
        )
        return 0

    diff = list(
        difflib.unified_diff(
            committed.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=f"{own_args.fixture} (committed)",
            tofile="live argparse tree",
            n=2,
        )
    )
    print(
        "CLI CONTRACT DRIFT -- hermes's parsers moved and the committed dump did not.\n",
        file=sys.stderr,
    )
    sys.stderr.writelines(diff[:400])
    if len(diff) > 400:
        print(f"  ... {len(diff) - 400} more diff lines", file=sys.stderr)
    print(
        "\n  Regenerate:  python scripts/dump_cli_contract.py --write\n"
        "\n  Then READ the diff. A removed command or flag is not a fixture\n"
        "  update -- it is a launcher operator button that now exits 2. The\n"
        "  launcher's own fixture\n"
        "  (test/features/mission_control/fixtures/hermes_cli_contract.json)\n"
        "  must be re-synced against this one in the same wave, and the sync\n"
        "  recorded in tool/hermes_cli_contract/README.md.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
