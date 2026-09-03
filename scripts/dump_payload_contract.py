"""Print the character payload contract as JSON, for a launcher to vendor.

WHY A SCRIPT AND NOT JUST THE VERB
----------------------------------
`hermes harness characters payload-contract --json` is the operator-facing door
and prints exactly the same document. This is the door for a TOOL in another
repo: the launcher's dumper execs THIS file out of its `--hermes-root`, so the
contract it vendors comes from the checkout it was pointed at rather than from
whatever `hermes` happens to be on PATH. Going through `hermes_cli.main` would
route a machine-readable dump through the whole CLI's config resolution for no
gain.

It is deliberately the shape `scripts/dump_cli_contract.py` already has, minus
the freshness gate: that dump has a hermes-side fixture to keep fresh, this one
does not. The COMMITTED artifact lives in the launcher, because the launcher is
the repo that has to notice when it goes stale — see
`tool/charsheet_payload_contract/README.md` there.

CONTRACT
--------
stdout is exactly one JSON object; every diagnostic goes to stderr. The encoder
is the house one (`agent_runtime.cli_format.emit_json`: sorted keys, two-space
indent), so a hand run of the verb and a run of this script are the same bytes.

    python scripts/dump_payload_contract.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
    args = parser.parse_args(argv)

    root = Path(args.hermes_root).expanduser().resolve()
    if not (root / "hermes_cli" / "__init__.py").is_file():
        print(f"not a hermes checkout: {root}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(root))

    from agent_runtime.cli_format import emit_json
    from hermes_cli.charsheet_payload_contract import build_payload_contract

    print(f"[dump] hermes root: {root}", file=sys.stderr)
    document = build_payload_contract()
    kinds = document["payloads"]
    print(
        "[dump] "
        + ", ".join(f"{name}: {len(kind['keys'])} keys" for name, kind in sorted(kinds.items())),
        file=sys.stderr,
    )
    # Through `.buffer`, not `print`: Python's text-mode writes flip LF->CRLF on
    # Windows, and a dump piped straight to a file is an LF artifact compared
    # byte for byte. Same reason `scripts/dump_cli_contract.py` is
    # newline-explicit.
    sys.stdout.buffer.write((emit_json(document) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
