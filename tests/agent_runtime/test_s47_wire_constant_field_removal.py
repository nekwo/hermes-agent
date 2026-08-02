"""S47 cuts the two snapshot-wire fields that could only ever report constants.

Both are operator-ruled removals from the 2026-07-31 deferred-debt ledger
(``docs/agent-runtime-harness/19-deferred-debt-ledger.md`` items 8 and 5). They
land together because they are the same defect on the same wire: a field an
operator can read, whose value no code path can move.

**Item 8 — ``runtime_config.role_envelope``.** S44 (``4e7aa0066``) deleted the
``role_envelopes`` / ``role_checklists`` store family whole. What it did NOT
take was the config lane that used to govern it: ``RoleEnvelopeConfig`` (11
fields), ``RuntimeConfig.role_envelope``, the ``config.py``
``_role_envelope_config`` loader, and five ``migrations.py`` validators that
range-check knobs nothing reads. Because ``effective_config_summary`` is
``asdict(cfg)``, the whole block shipped on ``harness snapshot --json`` — on the
live alice root with ``enabled: true``, because an operator once deliberately
turned it on (the dataclass default is ``False``). A knob reading "on" for a
feature that no longer exists is not residue; it is a wire telling an operator
something false.

**Item 5 — ``workspaces[].goals``.** ``_build_snapshot_uncoalesced`` seeded
``tasks = []`` and fed it to three projections. With the seed permanently empty,
each could only emit a constant:

* ``_workspace_summary`` published ``"goals": len(goals)`` — always ``0``.
* ``snapshot_prompt_observability`` built ``tasks_by_id`` — always ``{}``, so
  ``resolve_situational_hud`` always received ``task=None, goal_task=None``.
* ``operator_channel_summary`` built a ``_TaskLookup`` — always empty, so
  ``task`` was always ``None`` and the synthetic "Goal:" input message the
  conversation could mint was unreachable.

``status.py`` passed its own ``tasks = []`` literal to the same parameter, so
after this cut no production caller can supply a task at all: the parameters go
with the projections, per the settled ledger-item-2 rule that a lane whose whole
non-test caller set is empty is a closed loop, not covered code.

**Contract:** unlike S18/S27/S29 (which removed unreachable internals and
therefore explicitly asserted "not a contract move"), this removes FIELDS from
the emitted frame. That is the S9/S10 shape — ``docs/agent-runtime-harness/
16-mission-lane-removal.md:188`` bumped ``contract_version`` 44 -> 45 for the
same reason and S10 bumped the Launcher's ``kSupportedMissionContractVersion``
in lockstep. So: **45 -> 46**, with the Launcher lockstep landing beside it.

-----------------------------------------------------------------------------
MIGRATED TO ``test_tombstone_registry.py`` (2026-08-01)
-----------------------------------------------------------------------------

Three pure-ABSENCE cases left this file. Each was a NAME scan, and each of the
three was scanning through ``inspect.getsource``, which returns COMMENTS AND
DOCSTRINGS — the vacuous/false-positive gate class the registry's shared AST
scanner retires. The registry re-states them wider and comment-immune:

* ``test_role_envelope_config_class_is_gone`` -> ``ATTR`` row
  ``RoleEnvelopeConfig`` over ``agent_runtime.runtime_config`` AND
  ``agent_runtime.config``, plus a repo-wide ``CODE`` row for the same name.
* ``test_the_config_loader_plumbing_is_gone`` -> ``ATTR`` row
  ``_role_envelope_config`` (``agent_runtime.config``) + the ``CODE`` row above.
* ``test_the_five_range_validators_are_gone`` -> five repo-wide ``CODE`` rows,
  ``role_envelope.max_same_session_continuations`` and its four siblings.

The three SOURCE-SHAPE assertions those two cases also carried —
``raw.get("role_envelope")`` and ``role_envelope=`` in ``config.py``,
``getattr(cfg, "role_envelope"`` in ``migrations.py`` — are covered by the bare
``CODE`` row ``role_envelope``, which bans the name repo-wide instead of in two
hand-picked modules. That row is only SAFE because the scanner drops comments:
``role_envelope`` survives in six production files and every one of them is a
retirement note.

``_TaskLookup`` also has an ``ATTR`` row now; the case that names it here
(``test_the_two_cross_module_projections_dropped_the_dead_parameter``) STAYS
because its real subject is the ``tasks=`` PARAMETER, which no name scan can
honestly assert.

WHY THE SURVIVORS STAYED. This file is the wire file. What is left is the
emitted frame (``build_snapshot`` field/section presence), the
``effective_config_summary`` produced dict, the ``RuntimeConfig``
``__dataclass_fields__`` set, the load-and-ignore proof for a stale operator
yaml, the AST parameter/keyword/local-binding pins over
``_workspace_summary`` / ``snapshot_prompt_observability`` /
``operator_channel_summary`` / ``build_status``, and the import smoke pass. The
registry's own docstring names parameter absence, wire-key absence and exact
key-set pins as deliberately NOT rows.

**This file also holds ``CURRENT_CONTRACT_VERSION``** — the only live pin on the
emitted contract value, floored against by
``test_s56_worker_session_lane_removal``. It is not migratable and does not move.
"""

from __future__ import annotations

import ast
import inspect
from importlib import import_module

import pytest

from agent_runtime import config as config_module
from agent_runtime import migrations, operator_channels, prompt_observability, runtime_config, snapshot, status


#: The contract this wave moved to. Two emitted fields left the frame.
S47_CONTRACT_VERSION = 46

#: S56 moved the contract again, 46 -> 47, for the same S9/S10 reason: it removes
#: FIELDS from the emitted frame (the worker-session lane, the repo-bundle
#: projection rows, and the production envelope). S57 moved it 47 -> 48 for the
#: same reason again: 29 reader-less ``runtime_config`` scalars and
#: ``migration.counts.repo_bundles`` leave the frame. This file carries the only
#: live pin on the emitted value, so it tracks the CURRENT contract while
#: ``S47_CONTRACT_VERSION`` stays as this wave's historical mark -- the frame may
#: never go back below it.
CURRENT_CONTRACT_VERSION = 51


def _function(module, name: str) -> ast.FunctionDef:
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {module.__name__}")


def _params(module, name: str) -> set[str]:
    node = _function(module, name)
    args = node.args
    return {
        arg.arg
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)
    } | {a.arg for a in (args.vararg, args.kwarg) if a is not None}


# ---------------------------------------------------------------------------
# Item 8 — the role_envelope config lane
# ---------------------------------------------------------------------------


def test_runtime_config_no_longer_declares_the_field():
    cfg = runtime_config.RuntimeConfig()
    assert not hasattr(cfg, "role_envelope")
    assert "role_envelope" not in {field.name for field in cfg.__dataclass_fields__.values()}


def test_an_operator_config_that_still_sets_the_block_loads_without_it(tmp_path, monkeypatch):
    """Forward tolerance: the live alice root still has ``role_envelope:`` in
    its yaml. Loading must ignore it, not crash and not resurrect it."""

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "agent_runtime:\n"
        "  role_envelope:\n"
        "    enabled: true\n"
        "    max_no_progress_repeats: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    cfg = config_module.load_agent_runtime_config()
    assert not hasattr(cfg, "role_envelope")
    assert migrations.effective_config_summary(cfg).get("role_envelope") is None


def test_the_block_is_off_the_effective_config_summary():
    summary = migrations.effective_config_summary()
    assert "role_envelope" not in summary


# ---------------------------------------------------------------------------
# Item 5 — the tasks seed and its three constant projections
# ---------------------------------------------------------------------------


def test_the_snapshot_tasks_seed_is_gone():
    builder = _function(snapshot, "_build_snapshot_uncoalesced")
    bound = {
        target.id
        for statement in ast.walk(builder)
        if isinstance(statement, ast.Assign)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }
    assert "tasks" not in bound
    source = ast.get_source_segment(inspect.getsource(snapshot), builder) or ""
    assert "tasks=tasks" not in source


def test_workspace_summary_no_longer_takes_or_publishes_goals():
    """AST, not grep: the explanatory comments left behind name the removed
    field on purpose, so the gate reads the emitted dict keys instead."""

    assert "tasks" not in _params(snapshot, "_workspace_summary")
    emitted = {
        key.value
        for node in ast.walk(_function(snapshot, "_workspace_summary"))
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert "goals" not in emitted
    # Negative gate: the row's other counts are untouched.
    assert {"agents", "roster_agent_count", "live_scoped_agent_count"} <= emitted


def test_the_two_cross_module_projections_dropped_the_dead_parameter():
    assert "tasks" not in _params(prompt_observability, "snapshot_prompt_observability")
    assert "tasks" not in _params(operator_channels, "operator_channel_summary")
    assert not hasattr(operator_channels, "_TaskLookup")
    bound = {
        target.id
        for node in ast.walk(_function(prompt_observability, "snapshot_prompt_observability"))
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "tasks_by_id" not in bound


def test_status_no_longer_forwards_a_task_list_to_the_channel_projection():
    """Scoped to the channel projection: ``build_dirty_state`` keeps its own
    ``tasks=`` argument — a different lane, not this wave's."""

    calls = [
        node
        for node in ast.walk(_function(status, "build_status"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "operator_channel_summary"
    ]
    assert calls, "build_status must still project operator channels"
    for call in calls:
        assert "tasks" not in {kw.arg for kw in call.keywords}


def test_the_live_frame_drops_both_fields_at_the_new_contract(isolate_agent_runtime_root):
    frame = snapshot.build_snapshot()
    # S56 advanced the pin 46 -> 47 (see the module constants). S47's own two
    # field cuts below are untouched; only the version moved.
    assert frame["parity"]["contract_version"] == CURRENT_CONTRACT_VERSION
    assert frame["parity"]["contract_version"] >= S47_CONTRACT_VERSION
    assert "role_envelope" not in frame["runtime_config"]
    for row in frame["workspaces"]:
        assert "goals" not in row, row.get("id")


def test_the_keep_sections_and_a_clean_parity_envelope_survive(isolate_agent_runtime_root):
    """Negative gate: this is a field cut, not a section cut."""

    frame = snapshot.build_snapshot()
    for section in ("boards", "offices", "workspaces", "realms", "agents", "runtime_config"):
        assert section in frame, f"{section} is a KEEP frame and must survive"
    for section in ("goals", "archived_tasks", "proofs", "incidents", "runs", "stage_verification"):
        assert section not in frame
    # The workspace row keeps every OTHER count it publishes — the cut is
    # scoped to the one field no code could move.
    for row in frame["workspaces"]:
        for key in ("agents", "agent_ids", "roster_agent_count", "live_scoped_agent_count"):
            assert key in row, key


@pytest.mark.parametrize(
    "module_name",
    ("agent_runtime.snapshot", "agent_runtime.status", "agent_runtime.config", "agent_runtime.migrations"),
)
def test_the_touched_modules_still_import(module_name):
    assert import_module(module_name) is not None
