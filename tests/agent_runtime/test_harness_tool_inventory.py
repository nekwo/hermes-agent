"""The manual's in-turn tool inventory is GENERATED, and its gate can red.

Plan: ``docs/agent-runtime-harness/archive/s0a-atlas-cleanup.md`` §2 A4.

`SKILL.md` is preloaded into every mission-chat turn, so it is the routing model
every harness agent reads before it acts — and on 2026-09-03 it named exactly ONE
in-turn tool in its whole Operate table while routing two rows that tools answer
(`agent_chat_threads`, `board_card_add`) to the terminal. The inventory is
therefore generated from the registry and gated here, the same shape as
`tests/hermes_cli/test_cli_contract_dump.py`.

The mutation cases are the point: a gate that has only ever been green is
indistinguishable from one that cannot fail. Each of the three artifacts is
mutated in a temp copy of the repo docs and the check must red for each.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "emit_harness_tool_inventory.py"


def _load_module():
    """Import the script by path — ``scripts/`` is not a package."""

    spec = importlib.util.spec_from_file_location("emit_harness_tool_inventory", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def emitter():
    return _load_module()


@pytest.fixture
def repo_copy(tmp_path, emitter):
    """A temp root carrying only the three artifacts, at their real paths."""

    for relative in (emitter.SKILL_MD, emitter.INVENTORY_MD, emitter.INVENTORY_JSON):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, destination)
    return tmp_path


# ── the gate's green case ────────────────────────────────────────────────────


def test_the_committed_inventory_matches_the_live_registry(emitter):
    assert emitter.main(["--check"]) == 0


def test_every_tool_in_the_json_is_registered_and_none_is_hygiene_blocked(emitter):
    import model_tools  # noqa: F401 - the import IS the registration
    from agent_runtime.personas import REGISTRY_HYGIENE_BLOCKED_TOOLS
    from tools.registry import registry

    inventory = json.loads((REPO_ROOT / emitter.INVENTORY_JSON).read_text(encoding="utf-8"))
    names = {row["name"] for row in inventory["tools"]}

    assert names <= set(registry.get_all_tool_names())
    assert not (names & REGISTRY_HYGIENE_BLOCKED_TOOLS)
    # 43 -> 44: S2b registered ``agent_chat_installs`` into the ``agent_chat``
    # toolset, which ``harness_core`` includes by NAME. Re-measured with the
    # ratchet in the same commit, never adjusted to make a red go green.
    assert inventory["counts"]["tools"] == len(names) == 44
    assert inventory["counts"]["toolsets"] == 15
    assert inventory["declared"] == ["harness_core"]


def test_the_json_is_what_the_atlas_regenerates_from(emitter):
    """The launcher's Agent Command Atlas artifact was hand-built from a
    tool-diff of the 79-tool posture. It regenerates from THIS file, so the
    fields it needs are pinned by name."""

    inventory = json.loads((REPO_ROOT / emitter.INVENTORY_JSON).read_text(encoding="utf-8"))

    assert set(inventory) == {
        "schema_version", "declared", "toolsets", "tools", "counts", "cli_only_verbs",
    }
    assert set(inventory["tools"][0]) == {
        "name", "toolset", "mutating", "gated", "description",
    }
    assert all(row["description"] for row in inventory["tools"])
    # The mutation boundary travels, so the Atlas can mark the dangerous ones.
    # It is ``tool_permissions.READ_ONLY_BLOCKS`` verbatim — the one definition —
    # which is why ``execute_code`` is NOT in it (that set is the read-only
    # block list, and the sandbox verb has never been on it).
    mutating = {row["name"] for row in inventory["tools"] if row["mutating"]}
    assert {"write_file", "patch", "terminal"} <= mutating
    assert "read_file" not in mutating


# ── the gate can red: one mutation per artifact ──────────────────────────────
#
# Every mutation below writes with ``newline=""``. The check compares the
# committed bytes (read with ``newline=""``) against a rendering that came
# through ``read_text``'s universal newlines, so a plain ``write_text`` on
# Windows re-writes the whole artifact CRLF and reds the check on the LINE
# ENDINGS rather than on the mutation — every one of these would then pass
# whatever it had mutated, which is exactly the vacuity the module docstring
# says these cases exist to prevent.


def test_a_mutated_skill_block_reds_the_check(emitter, repo_copy, capsys):
    """The mutation has to land INSIDE the generated block.

    ``splice_skill`` regenerates only what lies between the markers, so a
    rename in the surrounding prose leaves the artifacts byte-identical and
    reds through ``cross_check`` instead — which is the sibling lane
    ``test_a_manual_that_names_an_unregistered_tool_reds`` already owns, on the
    very same line 35 occurrence this used to hit with ``replace(..., 1)``. It
    read as green only where the accidental CRLF rewrite manufactured a DRIFT;
    on Linux (CI run 33969282189, slice 2) the stderr carried no DRIFT at all.
    """

    path = repo_copy / emitter.SKILL_MD
    head, marker, rest = path.read_text(encoding="utf-8").partition(emitter.BEGIN_MARKER)
    assert marker, "the fixture's SKILL.md must carry the generated markers"
    path.write_text(
        head + marker + rest.replace("`agent_chat_send`", "`agent_chat_yell`", 1),
        encoding="utf-8",
        newline="",
    )

    assert emitter.main(["--check", "--root", str(repo_copy)]) == 1
    assert "DRIFT" in capsys.readouterr().err


def test_a_mutated_reference_table_reds_the_check(emitter, repo_copy, capsys):
    path = repo_copy / emitter.INVENTORY_MD
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("| `board_cards` |", "| `board_cardz` |", 1),
        encoding="utf-8",
        newline="",
    )

    assert emitter.main(["--check", "--root", str(repo_copy)]) == 1
    assert "tool-inventory.md" in capsys.readouterr().err


def test_a_mutated_json_reds_the_check(emitter, repo_copy, capsys):
    path = repo_copy / emitter.INVENTORY_JSON
    inventory = json.loads(path.read_text(encoding="utf-8"))
    inventory["counts"]["tools"] = 999
    path.write_text(
        json.dumps(inventory, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="",
    )

    assert emitter.main(["--check", "--root", str(repo_copy)]) == 1
    assert "tool-inventory.json" in capsys.readouterr().err


def test_a_missing_artifact_reds_the_check(emitter, repo_copy, capsys):
    (repo_copy / emitter.INVENTORY_JSON).unlink()

    assert emitter.main(["--check", "--root", str(repo_copy)]) == 1
    assert "MISSING" in capsys.readouterr().err


# ── the manual-vs-registry cross-checks ──────────────────────────────────────


def test_a_manual_that_names_an_unregistered_tool_reds(emitter, repo_copy, capsys):
    """A renamed tool must red the MANUAL, not just the generated block: the
    prose routes agents by name too."""

    path = repo_copy / emitter.SKILL_MD
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("the in-model `agent_chat_send` tool", "the in-model `agent_chat_holler` tool", 1),
        encoding="utf-8",
        newline="",
    )

    assert emitter.main(["--check", "--root", str(repo_copy)]) == 1
    assert "agent_chat_holler" in capsys.readouterr().err


def test_a_cli_only_verb_without_its_operate_row_reds(emitter, repo_copy, capsys):
    """The other direction: if a verb grows a tool (or its row is deleted) the
    hand-kept CLI-only list must stop claiming it."""

    path = repo_copy / emitter.SKILL_MD
    text = path.read_text(encoding="utf-8")
    assert "mission-chat queue-skill" in text
    path.write_text(
        text.replace("mission-chat queue-skill", "mission-chat queue_skill"),
        encoding="utf-8",
        newline="",
    )

    assert emitter.main(["--check", "--root", str(repo_copy)]) == 1
    assert "CLI-only" in capsys.readouterr().err


def test_the_emitter_refuses_to_list_a_withheld_tool(emitter, monkeypatch):
    """The emitter's own floor: hygiene ∩ declared must be empty, and the
    refusal is a hard exit rather than a quietly longer table."""

    import agent_runtime.personas as personas

    monkeypatch.setattr(
        personas, "REGISTRY_HYGIENE_BLOCKED_TOOLS", frozenset({"terminal"})
    )

    with pytest.raises(SystemExit) as excinfo:
        emitter.collect(REPO_ROOT)

    assert "REFUSED" in str(excinfo.value)
