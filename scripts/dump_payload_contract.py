"""Print the character payload contract as JSON, and gate the committed dump.

WHY A SCRIPT AND NOT JUST THE VERB
----------------------------------
`hermes harness characters payload-contract --json` is the operator-facing door
and prints exactly the same document. This is the door for a TOOL in another
repo: the launcher's dumper execs THIS file out of its `--hermes-root`, so the
contract it vendors comes from the checkout it was pointed at rather than from
whatever `hermes` happens to be on PATH. Going through `hermes_cli.main` would
route a machine-readable dump through the whole CLI's config resolution for no
gain.

WHY THE DUMP IS COMMITTED HERE TOO
----------------------------------
It was not, until 2026-09-05, and the hole was the one
`scripts/dump_cli_contract.py`'s header describes: the only committed copy of
this document lived in the LAUNCHER (`tool/charsheet_payload_contract/`), so a
hermes-side producer move left every hermes test green while the launcher's
vendored copy lied — the repo that did NOT move was the only one that could go
red. A gate cannot watch a repo it cannot see move. So hermes commits its own
dump of its own producers and checks it here: the day a payload key appears,
vanishes or turns conditional without a regen, **hermes** goes red, in the repo
that moved. The launcher's refresh then becomes a byte comparison against a
same-repo artifact instead of a hand run against a personal checkout.

CONTRACT
--------
stdout is exactly one JSON object; every diagnostic goes to stderr. The encoder
is the house one (`agent_runtime.cli_format.emit_json`: sorted keys, two-space
indent), so a hand run of the verb and a run of this script are the same bytes.
Writing is byte-explicit (through `.buffer`, and `newline="\\n"` for the
fixture), because Python's text-mode writes flip LF->CRLF on Windows and the
fixture is an LF artifact compared byte for byte.

    python scripts/dump_payload_contract.py            # JSON to stdout
    python scripts/dump_payload_contract.py --check    # gate: fixture is fresh
    python scripts/dump_payload_contract.py --write    # regenerate the fixture

The document is key paths only — never a value — so it is stable across runs,
machines and clocks even though the probes behind it carry temp paths, revision
stamps and generated draft ids. That is what makes a byte comparison the right
check rather than a fuzzy one; `tests/hermes_cli/test_payload_contract_dump.py`
pins the stability and proves the gate can actually red.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The committed dump this repo gates on. It is a TEST FIXTURE, not a doc: its
#: readers are the freshness check below and the launcher's vendored copy, which
#: is compared against it on sync.
DEFAULT_FIXTURE = "tests/fixtures/charsheet_payload_contract.json"


def build_contract(root: Path, *, quiet: bool = False) -> dict[str, Any]:
    """Run *root*'s producers and return the contract document.

    The root is pushed to the FRONT of `sys.path` so an unrelated editable
    install cannot silently become the authority for a dump — the same rule
    `scripts/dump_cli_contract.py` follows.
    """
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from hermes_cli.charsheet_payload_contract import build_payload_contract

    document = build_payload_contract()
    if not quiet:
        kinds = document["payloads"]
        print(f"[dump] hermes root: {root}", file=sys.stderr)
        print(
            "[dump] "
            + ", ".join(
                f"{name}: {len(kind['keys'])} keys" for name, kind in sorted(kinds.items())
            ),
            file=sys.stderr,
        )
    return document


def render(document: dict[str, Any]) -> str:
    """The exact bytes the fixture holds: the house encoder, one trailing LF.

    Pinned here rather than at each call site because the whole point of the
    artifact is a byte comparison — against the previous commit's, and against
    the launcher's vendored copy of the same document.
    """
    from agent_runtime.cli_format import emit_json

    return emit_json(document) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dump_payload_contract.py",
        description="Dump the character payload contract; gate the committed fixture on it.",
    )
    parser.add_argument(
        "--hermes-root",
        default=str(REPO_ROOT),
        help=(
            "Checkout whose producers to measure. Defaults to this script's own "
            "repo, which is the checkout it lives in; overridable so a caller "
            "can point at a chosen one without an editable install silently "
            "deciding for it."
        ),
    )
    parser.add_argument(
        "--fixture",
        default=DEFAULT_FIXTURE,
        help=f"committed dump, relative to --hermes-root (default: {DEFAULT_FIXTURE})",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the committed fixture does not match the live producers",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="regenerate the committed fixture from the live producers",
    )
    args = parser.parse_args(argv)

    root = Path(args.hermes_root).expanduser().resolve()
    if not (root / "hermes_cli" / "__init__.py").is_file():
        print(f"not a hermes checkout: {root}", file=sys.stderr)
        return 2

    document = build_contract(root, quiet=args.check)
    rendered = render(document)

    if not (args.check or args.write):
        # Through `.buffer`, not `print`: Python's text-mode writes flip LF->CRLF
        # on Windows, and a dump piped straight to a file is an LF artifact
        # compared byte for byte.
        sys.stdout.buffer.write(rendered.encode("utf-8"))
        return 0

    fixture = (root / args.fixture).resolve()

    if args.write:
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
            f"PAYLOAD CONTRACT: the committed dump is MISSING at {fixture}.\n"
            "  Regenerate it:  python scripts/dump_payload_contract.py --write",
            file=sys.stderr,
        )
        return 1

    with open(fixture, "r", encoding="utf-8", newline="") as handle:
        committed = handle.read()

    if committed == rendered:
        digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        keys = sum(len(kind["keys"]) for kind in document["payloads"].values())
        print(
            f"Payload contract fresh: {len(document['payloads'])} kinds, "
            f"{keys} keys, sha256 {digest[:16]}",
            file=sys.stderr,
        )
        return 0

    diff = list(
        difflib.unified_diff(
            committed.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=f"{args.fixture} (committed)",
            tofile="live producers",
            n=2,
        )
    )
    print(
        "PAYLOAD CONTRACT DRIFT -- hermes's producers moved and the committed "
        "dump did not.\n",
        file=sys.stderr,
    )
    sys.stderr.writelines(diff[:400])
    if len(diff) > 400:
        print(f"  ... {len(diff) - 400} more diff lines", file=sys.stderr)
    print(
        "\n  Regenerate:  python scripts/dump_payload_contract.py --write\n"
        "\n  Then READ the diff. A REMOVED key is the dangerous half: the\n"
        "  launcher's `sidecarDisagreementsWithHermes` walks every payload key\n"
        "  by default-deny, so an added key it does not know about throws and a\n"
        "  removed one leaves it acting on a stale default. Re-vendor the\n"
        "  launcher's copy (`tool/charsheet_payload_contract/`) in the same\n"
        "  wave and record the new sha256 in its README.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
