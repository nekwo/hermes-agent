import json

from hermes_time import now
from agent_runtime.burn_in import certification_ledger_path, recursive_supervision_certification_allows_production
from agent_runtime.production_envelope import production_envelope_status
from agent_runtime.runtime_config import RuntimeConfig, SupervisionConfig, SwarmConfig
from agent_runtime.status import _recursive_supervision_enabled


def test_recursive_supervision_certification_blocks_until_green(isolate_agent_runtime_root):
    allowed, cert = recursive_supervision_certification_allows_production()

    assert allowed is False
    assert cert["state"] == "red"
    assert cert["consecutive_green"] == 0


def test_recursive_supervision_certification_override_is_explicit(isolate_agent_runtime_root):
    allowed, cert = recursive_supervision_certification_allows_production(
        allow_uncertified_recursive_supervision=True,
    )

    assert allowed is True
    assert cert["override"] is True


def test_production_envelope_gates_enabled_recursive_supervision(isolate_agent_runtime_root):
    cfg = RuntimeConfig(
        supervision=SupervisionConfig(
            child_events_enabled=True,
            recursive_enabled=True,
            hierarchical_budget_enabled=True,
            deploy_verification_enabled=True,
        ),
        swarm=SwarmConfig(enabled=True, requires_certification=True),
    )

    envelope = production_envelope_status(cfg)

    recursive = next(item for item in envelope["items"] if item["id"] == "recursive_supervision")
    assert envelope["production_ready"] is False
    assert recursive["status"] == "gated"
    assert recursive["blockers"]
    assert recursive["flags"]["certification_state"] == "red"


def test_production_envelope_allows_recursive_supervision_after_ten_green(isolate_agent_runtime_root):
    path = certification_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "state": "green",
                "consecutive_green": 10,
                "required_consecutive_green": 10,
                "certified_at": str(now()),
                "updated_at": str(now()),
            }
        ),
        encoding="utf-8",
    )
    cfg = RuntimeConfig(
        supervision=SupervisionConfig(child_events_enabled=True, recursive_enabled=True),
        swarm=SwarmConfig(enabled=True, requires_certification=True),
    )

    envelope = production_envelope_status(cfg)

    recursive = next(item for item in envelope["items"] if item["id"] == "recursive_supervision")
    assert recursive["status"] == "implemented"
    assert recursive["blockers"] == []


def test_recursive_supervision_status_enabled_follows_control_flags():
    assert _recursive_supervision_enabled(RuntimeConfig()) is False
    assert _recursive_supervision_enabled(RuntimeConfig(supervision=SupervisionConfig(deploy_verification_enabled=True))) is True
