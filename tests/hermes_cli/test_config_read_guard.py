"""Lint guard: no new raw yaml.safe_load(config.yaml) reads outside owner modules.

The drift class this kills: scattered ``yaml.safe_load`` reads of the user's
``config.yaml`` silently miss the managed-scope overlay, ``${ENV_VAR}``
expansion, profile-aware pathing, and root-model normalization. Each new
config feature has historically required an N-site sweep (incident chain:
9cbcc0c9c8 → 732293cf87 → b0e47a98f9 → 1928aa0443).

Canonical owners:

  * ``hermes_cli/config.py`` — ``load_config()`` / ``load_config_readonly()``
    (merged + managed + env-expanded), ``read_raw_config()`` and
    ``read_user_config_raw()`` (the ONLY legal raw primitives: write-back
    round-trips + raw-file diagnostics).
  * ``gateway/config.py`` — the gateway's ``load_gateway_config`` owner.
  * ``gateway/run.py`` — ``_load_gateway_config()``'s monkeypatched-home
    fallback path (delegates to ``read_raw_config`` when paths agree).

Everything else must import one of those. If this test fails on your new
code, use ``load_config()``/``load_config_readonly()`` for behavioral reads,
or ``read_user_config_raw()`` for write-back round-trips — do not add your
file to the allowlist without a reason of the same class.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Files where a yaml.safe_load near a config.yaml reference is legal.
# Keep this list SHORT and justified:
ALLOWLIST = {
    # Canonical loader owners.
    "hermes_cli/config.py",
    "gateway/config.py",
    # _load_gateway_config()'s fallback path for tests that monkeypatch
    # gateway.run._hermes_home (delegates to read_raw_config otherwise).
    "gateway/run.py",
    # Reads the MANAGED-scope config.yaml (/etc/hermes/...), not the user's —
    # it IS the overlay source; the canonical loaders call into it.
    "hermes_cli/managed_scope.py",
    # Parse-health probe: intentionally answers "does the raw file parse?".
    "gateway/readiness.py",
    # Reads a PULLED REALM SUBTREE's profiles/<name>/config.yaml — a foreign,
    # published document that happens to share the filename, not this machine's
    # user config. Same class as managed_scope.py above. The canonical loaders
    # resolve the LIVE profile config and cannot address an arbitrary subtree
    # path, and routing this read through them would be a bug, not a fix: it
    # would apply this machine's managed-scope overlay and ${ENV} expansion to
    # somebody else's realm document. read_remote_persona_defs() exists
    # precisely to strip the publisher's machine paths / venv pointers /
    # mcp_servers at the door, so overlaying local state onto it is the thing
    # it is defending against.
    "agent_runtime/persona_config_sync.py",
}

# Directories that never count (tests may build fixture configs freely).
EXCLUDED_DIR_PARTS = {
    "tests", ".venv", ".git", ".worktrees", "node_modules", "website",
    "docs", "scripts", "examples", "apps",
}

# Marker file every PEP-405 virtual environment carries at its root. The
# subject of this guard is FIRST-PARTY source: ``.venv`` was already excluded
# by name above, but an operator-named sibling (``.venv-ci``, ``.venv-py313``,
# ``venv/``) is the same thing under a different spelling and was being walked
# and read in full. That was not just slow — it made the guard non-hermetic:
# the offender set depended on which third-party packages happened to be
# installed on the box, and any vendored library shipping a ``safe_load`` near
# a ``"config.yaml"`` string would have failed OUR guard. Interpreter
# environments are pruned by marker, not by name.
VENV_MARKER = "pyvenv.cfg"

# A safe_load within this many lines of a config.yaml reference is treated
# as a raw user-config read.
PROXIMITY = 6

SAFE_LOAD_RE = re.compile(r"\bsafe_load\s*\(")
CONFIG_YAML_RE = re.compile(r"""["']config\.yaml["']""")


def _iter_source_files():
    # os.walk with in-place ``dirnames`` pruning rather than ``rglob`` + a
    # post-filter: the post-filter still paid to enumerate every excluded
    # subtree. Pruning a directory name here is exactly equivalent to the old
    # "any part of the relative path is excluded" test, because a pruned
    # directory can contribute no descendants.
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        here = Path(dirpath)
        dirnames[:] = [
            name
            for name in dirnames
            if name not in EXCLUDED_DIR_PARTS
            and not (here / name / VENV_MARKER).is_file()
        ]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = here / name
            yield path.relative_to(REPO_ROOT), path


def test_no_raw_config_yaml_reads_outside_owner_modules():
    offenders: list[str] = []
    for rel, path in _iter_source_files():
        rel_str = str(rel).replace("\\", "/")
        if rel_str in ALLOWLIST:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        cfg_lines = [i for i, ln in enumerate(lines) if CONFIG_YAML_RE.search(ln)]
        if not cfg_lines:
            continue
        for i, ln in enumerate(lines):
            if not SAFE_LOAD_RE.search(ln):
                continue
            # Comment/docstring mentions don't count.
            stripped = ln.strip()
            if stripped.startswith("#"):
                continue
            if any(abs(i - j) <= PROXIMITY for j in cfg_lines):
                offenders.append(f"{rel_str}:{i + 1}: {stripped}")

    assert not offenders, (
        "Raw yaml.safe_load of config.yaml outside allowlisted owner modules.\n"
        "Behavioral reads must use hermes_cli.config.load_config()/"
        "load_config_readonly() (or gateway _load_gateway_config); write-back "
        "round-trips and raw-file diagnostics must use "
        "hermes_cli.config.read_user_config_raw().\nOffenders:\n  "
        + "\n  ".join(offenders)
    )


def test_read_user_config_raw_exists_and_documented():
    """The shared raw primitive must exist and carry its legality docstring."""
    from hermes_cli.config import read_user_config_raw

    doc = read_user_config_raw.__doc__ or ""
    assert "ONLY legal for write-back round-trips and raw-file diagnostics" in doc
    assert "load_config()" in doc
