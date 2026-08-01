"""S56 removes six runtime-config blocks, prunes a seventh, and retires the
roster gate that hid inside one of them.

Operator-ruled CUT on 2026-08-01. Every block below shipped on the live
``runtime_config`` wire (``effective_config_summary`` is ``asdict(cfg)``), so
each one was telling an operator that a knob governed something. None did.

| block                        | what actually read it                       |
| ---------------------------- | ------------------------------------------- |
| ``repo_bundle_routing``      | NOTHING — not even a validator that acted   |
| ``continuous_role_sessions`` | its own loader + three range validators     |
| ``normal_worker_flow``       | its own loader + one range validator        |
| ``swarm``                    | report-only: ``status.swarm`` /             |
|                              | ``swarm_budget`` / ``production_envelope``  |
| ``simplified_agent_contract``| report-only since S27 (``production_envelope``) |
| ``enterprise_worker_sessions``| 12 fields: nothing. 3 fields: the roster gate |
| ``supervision``              | ONE live field (``child_events_enabled``,   |
|                              | read by ``continuity.py:88``); 3 dead       |

``_apply_enterprise_role_session_compat`` went with them, and it is worth
naming on its own: it mapped ONE unread block (``enterprise_worker_sessions``)
onto ANOTHER unread block (``continuous_role_sessions``) — a compat shim
between two things nothing consulted.

**The roster gate (the dangerous one).** ``persona_instances`` — the identity
substrate every Mission Control surface keys on — was gated on
``enterprise_worker_sessions.enabled AND .persona_instance_runtime``, and the
``persona_assignments`` section on a third field of the same block. Both are now
unconditional. The counterfactual is pinned below: a config that still sets the
old block to ``false`` no longer suppresses the roster.
"""

from __future__ import annotations

import inspect
from dataclasses import fields

import pytest
import yaml

from agent_runtime import config as config_module
from agent_runtime import migrations, runtime_config, snapshot, status
from agent_runtime.config import load_agent_runtime_config
from agent_runtime.runtime_config import RuntimeConfig


REMOVED_BLOCKS = (
    "continuous_role_sessions",
    "enterprise_worker_sessions",
    "normal_worker_flow",
    "repo_bundle_routing",
    "simplified_agent_contract",
    "swarm",
)

REMOVED_DATACLASSES = (
    "ContinuousRoleSessionConfig",
    "EnterpriseWorkerSessionsConfig",
    "NormalWorkerFlowConfig",
    "RepoBundleRoutingConfig",
    "SimplifiedAgentContractConfig",
    "SwarmConfig",
)

REMOVED_LOADERS = (
    "_continuous_role_sessions_config",
    "_enterprise_worker_sessions_config",
    "_normal_worker_flow_config",
    "_repo_bundle_routing_config",
    "_simplified_agent_contract_config",
    "_swarm_config",
    "_apply_enterprise_role_session_compat",
)


@pytest.mark.parametrize("name", REMOVED_BLOCKS)
def test_the_dataclass_field_is_gone(name):
    assert name not in {field.name for field in fields(RuntimeConfig)}


@pytest.mark.parametrize("name", REMOVED_DATACLASSES)
def test_the_config_dataclass_is_gone(name):
    assert not hasattr(runtime_config, name)


@pytest.mark.parametrize("name", REMOVED_LOADERS)
def test_the_config_loader_is_gone(name):
    assert not hasattr(config_module, name)


@pytest.mark.parametrize("name", REMOVED_BLOCKS)
def test_the_block_left_the_wire(name):
    assert name not in migrations.effective_config_summary()


def test_supervision_is_pruned_to_its_one_live_field():
    """``child_events_enabled`` is live — ``continuity.py`` reads it to decide
    whether ``child.*`` events are emitted. The other three flags had no reader
    at all, and one of them (``recursive_enabled``) was widening the
    ``status.recursive_supervision`` verdict with nothing acting on it."""
    assert {field.name for field in fields(runtime_config.SupervisionConfig)} == {"child_events_enabled"}


def test_the_migration_validators_for_the_removed_blocks_are_gone():
    """A range check on a knob no code path reads validates nothing — it only
    makes a dead field look governed. Twenty went with their blocks."""
    src = inspect.getsource(migrations.validate_runtime_config)
    for token in (
        "continuous_role_sessions.",
        "enterprise_worker_sessions.",
        "normal_worker_flow.",
        "swarm.",
    ):
        code = "\n".join(line for line in src.splitlines() if not line.strip().startswith("#"))
        assert token not in code, token


def test_an_operator_yaml_that_still_sets_a_removed_block_loads_and_is_ignored(tmp_path, monkeypatch):
    """S47 precedent. The live alice/neko/base/unbounded profiles all still set
    ``enterprise_worker_sessions``, ``repo_bundle_routing``,
    ``normal_worker_flow``, ``simplified_agent_contract`` and (three of them)
    ``swarm``; none may start refusing to load."""
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "agent_runtime": {
                    "enterprise_worker_sessions": {
                        "enabled": True,
                        "mode": "enforce",
                        "persona_instance_runtime": True,
                        "persona_assignment_store": True,
                        "worker_heartbeat_seconds": 5,
                    },
                    "repo_bundle_routing": {"enabled": True, "auto_create_from_mission_plan": True},
                    "continuous_role_sessions": {"enabled": True, "max_proofs_per_envelope": 2},
                    "normal_worker_flow": {"enabled": True, "auto_final_gate_after_delivery": True},
                    "simplified_agent_contract": {"enabled": True, "allow_legacy_decision_aliases": True},
                    "swarm": {"enabled": True, "max_active_lanes": 4},
                    "supervision": {"child_events_enabled": True, "recursive_enabled": True},
                    "root_node_mode": True,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    config_module.load_agent_runtime_config.cache_clear() if hasattr(
        config_module.load_agent_runtime_config, "cache_clear"
    ) else None

    cfg = config_module.load_agent_runtime_config()

    for name in REMOVED_BLOCKS:
        assert not hasattr(cfg, name), name
    # The surviving keys in the SAME yaml still load, so this is an ignore, not
    # a parse failure.
    assert cfg.root_node_mode is True
    assert cfg.supervision.child_events_enabled is True
    assert not hasattr(cfg.supervision, "recursive_enabled")
    assert migrations.validate_runtime_config(cfg)["ok"] is True


# --------------------------------------------------------------------------
# The roster gate
# --------------------------------------------------------------------------


def test_the_roster_gate_helpers_are_gone():
    from agent_runtime import persona_assignments

    assert not hasattr(persona_assignments, "persona_instance_runtime_enabled")
    assert not hasattr(persona_assignments, "persona_assignment_store_enabled")


def test_no_production_module_still_calls_the_gate():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    hits = []
    for package in ("agent_runtime", "hermes_cli", "tools"):
        for path in (root / package).rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            code = "\n".join(
                line.split("#", 1)[0] for line in text.splitlines() if not line.strip().startswith("#")
            )
            if "persona_instance_runtime_enabled" in code or "persona_assignment_store_enabled" in code:
                hits.append(str(path.relative_to(root)))
    assert hits == []


def test_the_roster_section_is_emitted_unconditionally():
    """COUNTERFACTUAL PROOF. Under the old gate, a config whose
    ``enterprise_worker_sessions`` block was absent (the DEFAULT: ``enabled``
    and ``persona_instance_runtime`` both default ``False``) produced
    ``persona_instance_runtime: {"enabled": False}`` and NO ``persona_instances``
    section at all. Six live profiles — qa, launcher-dev, backend-dev,
    launcher-qa, gpt-launcher, aliceimagecron — carry exactly that shape, so the
    change is not vacuous: they gain the roster. The four that set the block
    (alice, neko, base, unbounded) are unaffected, because they set it true."""
    src = inspect.getsource(snapshot._build_snapshot_uncoalesced)
    code = "\n".join(line for line in src.splitlines() if not line.strip().startswith("#"))
    assert "persona_instance_runtime_enabled" not in code
    assert '"persona_instance_runtime"] = {' in code or '"persona_instance_runtime"]' in code
    assert '{"enabled": False}' not in code

    status_src = inspect.getsource(status.build_status)
    status_code = "\n".join(
        line for line in status_src.splitlines() if not line.strip().startswith("#")
    )
    assert "persona_instance_runtime_enabled" not in status_code
    assert '{"enabled": False}' not in status_code


def test_the_wire_block_survives_and_reports_the_truth():
    """The Launcher bridge reads ``persona_instance_runtime``
    (``mission_control_bridge.dart``), so the KEY stays — it now reports what is
    actually true rather than echoing a flag."""
    data = status.build_status()
    assert data["persona_instance_runtime"] == {"enabled": True, "assignment_store_enabled": True}
    assert isinstance(data["persona_instances"], list)


def test_an_operator_config_that_still_disables_the_old_block_does_not_suppress_the_roster(
    tmp_path, monkeypatch
):
    """The other half of the counterfactual: setting the retired flag FALSE must
    not change behaviour either, or the removal would have introduced a silent
    config trap."""
    home = tmp_path / "disabled"
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "agent_runtime": {
                    "enterprise_worker_sessions": {
                        "enabled": False,
                        "persona_instance_runtime": False,
                        "persona_assignment_store": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    cfg = load_agent_runtime_config()
    assert not hasattr(cfg, "enterprise_worker_sessions")

    data = status.build_status()
    assert data["persona_instance_runtime"]["enabled"] is True
    assert "persona_instances" in data
