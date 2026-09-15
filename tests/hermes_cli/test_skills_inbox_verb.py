"""`hermes harness skills inbox` reports the THREE-WAY verdict, not only the
two-way `action`.

Since 2026-09-12 (the skill family joined the three-way model —
``EterniaLauncher/docs/mission_control/planned/held-skill-publish-direction.md``
section 4.3) ``list_inbox_packages`` rows carry ``decision`` and
``baseline_hash``. The listing verb projected a hand-picked key set and dropped
both, so an operator reading the verb saw ``hold_divergent`` for a package the
pull would fast-forward. Killing mutation: drop ``decision`` from the projection
in ``_cmd_skills_inbox`` → ``KeyError: 'decision'`` here.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from agent.skill_utils import _content_hash_cache_clear
from agent_runtime import paths
from agent_runtime.skill_promotion import realm_inbox_dir
from hermes_constants import get_shared_skills_dir


@pytest.fixture(autouse=True)
def hermetic_runtime_root(tmp_path, monkeypatch):
    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    monkeypatch.delenv("HERMES_SHARED_SKILLS", raising=False)
    resolved = paths.store_root().resolve()
    assert resolved == root.resolve() or root.resolve() in resolved.parents
    shared = get_shared_skills_dir().resolve()
    assert tmp_path.resolve() in shared.parents, (
        f"get_shared_skills_dir() resolved to {shared}, OUTSIDE {tmp_path}"
    )
    _content_hash_cache_clear()
    yield root
    _content_hash_cache_clear()


def _write_package(base: Path, slug: str, body: str) -> Path:
    pkg = base / slug
    pkg.mkdir(parents=True, exist_ok=True)
    # BYTES, not write_text: text mode on Windows rewrites "\r\n" as "\r\r\n",
    # which is a different document after canonicalization, not an EOL variant.
    (pkg / "SKILL.md").write_bytes(f"---\nname: {slug}\n---\n{body}".encode("utf-8"))
    return pkg


def _run(capsys, argv: list[str]) -> tuple[int, dict]:
    from hermes_cli import harness

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers()
    harness.build_parser(subs)
    args = parser.parse_args(argv)
    code = args.func(args)
    return code, json.loads(capsys.readouterr().out.strip())


def test_inbox_verb_reports_the_three_way_decision_beside_the_two_way_action(capsys):
    shared = get_shared_skills_dir()
    inbox = realm_inbox_dir("realm_x")
    # Both sides differ with no baseline → the two-way action is hold_divergent
    # AND the three-way decision is held (new_both).
    _write_package(shared, "diverged", "# mine\n")
    _write_package(inbox, "diverged", "# theirs\n")
    # Positive control: identical content (CRLF locally, LF in the inbox) → the
    # two-way action AND the three-way decision both say converged, and the
    # decision column is what says it.
    _write_package(shared, "same", "# same\r\n")
    _write_package(inbox, "same", "# same\n")

    code, envelope = _run(capsys, ["harness", "skills", "inbox", "--realm", "realm_x", "--json"])

    assert code == 0
    rows = {row["skill"]: row for row in envelope["items"]}
    assert rows["diverged"]["action"] == "hold_divergent"
    assert rows["diverged"]["decision"] == "held"
    assert rows["diverged"]["baseline_hash"] is None
    assert rows["same"]["decision"] == "converged"
    assert rows["same"]["action"] == "noop_identical"
