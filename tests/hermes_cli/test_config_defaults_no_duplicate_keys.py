"""No two keys in ``DEFAULT_CONFIG`` may be spelled the same way.

Why this needs a test at all, and why the test reads the SOURCE
--------------------------------------------------------------

``hermes_cli/config_defaults.py`` is a single ~3000-line dict literal, and a
duplicate key in a Python dict literal is not an error, not a warning, and not
observable from the loaded object: the later entry wins and the earlier one is
discarded at parse time. So the failure mode is a block of config that reads
perfectly, reviews perfectly, and does not exist.

That is not hypothetical. The remote gateway's Stage 0a landed
``"gateway": {"listen": False, "port": 0}`` into this file with receipts saying
the keys were "declared, read by nothing" — and ``"gateway"`` was already a
top-level key further down (the messaging gateway's). The declaration was
silently dropped, and "read by nothing" is precisely the condition under which
nobody could tell: a key no caller reads and a key that is not there behave
identically until someone reads it. Stage 1 became that reader.

The check has to run against the AST rather than the loaded dict, because by the
time the dict exists the evidence is gone. It walks every dict literal in the
module — not only the top level — since a duplicate nested inside
``"agent"`` or ``"platforms"`` loses exactly the same way and is harder to see.
"""

from __future__ import annotations

import ast
from pathlib import Path

import hermes_cli.config_defaults as config_defaults


def _duplicate_keys(node: ast.Dict) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for key in node.keys:
        # ``**spread`` entries have a ``None`` key and no name to collide on.
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            continue
        if key.value in seen:
            duplicates.append(key.value)
        seen.add(key.value)
    return duplicates


def test_no_dict_literal_in_config_defaults_declares_a_key_twice():
    source = Path(config_defaults.__file__).read_bytes().decode("utf-8")
    tree = ast.parse(source)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for name in _duplicate_keys(node):
                offenders.append(f"line {node.lineno}: {name!r}")

    assert not offenders, (
        "duplicate keys in a dict literal are silently discarded (the later one "
        "wins), so these declarations do not exist at runtime: "
        + "; ".join(offenders)
    )


def test_the_remote_gateway_block_survives_into_the_loaded_defaults():
    """The regression the AST check generalises, asserted on the object too.

    Both halves are worth keeping. The AST test would catch a future duplicate
    anywhere in the file; this one catches the narrower thing that actually
    happened — a block that exists in the source and not in the config — and it
    is the assertion Stage 0a's receipts believed they were making.
    """

    block = config_defaults.DEFAULT_CONFIG["remote_gateway"]

    assert block["listen"] is False
    assert block["port"] == 0
    # And the messaging gateway is still itself, unmerged and untouched: the two
    # lanes share the word and must never share the key.
    assert "listen" not in config_defaults.DEFAULT_CONFIG["gateway"]
    assert "delivery_ledger" in config_defaults.DEFAULT_CONFIG["gateway"]
