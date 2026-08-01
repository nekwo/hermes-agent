"""S34 — retire ``AgentRun.llm`` with its writer and dead readers.

The writer lane disappeared at S5.  ``a54e802cd`` proved
``_apply_llm_metadata`` has no production caller and is the sole writer of the
field; the 2026-07-30 ruling retires the writer, its eight exclusive callees,
the field, and every branch that consumed it as one lockstep contract.

Persisted v1 run rows may still contain an ``llm`` key.  Serde intentionally
constructs dataclasses from their current fields and ignores unknown keys, so
those historical rows remain readable after the field is removed.
"""

from __future__ import annotations

import inspect

from hermes_time import now

from agent_runtime import (
    observability,
    operator_channels,
    persona_runtime,
    status,
    store,
)
from agent_runtime.models import AgentRun
from agent_runtime.serde import from_jsonable


REMOVED_LLM_METADATA_CLUSTER = (
    "_apply_llm_metadata",
    "_decision_metric_reason",
    "_decision_metrics",
    "_finish_reason_from_result",
    "_record_timing_value",
    "_safe_base_url_host",
    "_safe_nonnegative_int",
    "_safe_run_budget_block",
    "_safe_timing_map",
)


def test_the_caller_free_llm_metadata_cluster_is_gone():
    assert [
        name
        for name in REMOVED_LLM_METADATA_CLUSTER
        if hasattr(persona_runtime, name)
    ] == []


def test_agent_run_no_longer_declares_the_writerless_llm_field():
    assert "llm" not in AgentRun.__dataclass_fields__


def test_the_direct_and_indirect_run_readers_no_longer_consume_llm():
    # S56 deleted ``worker_sessions`` whole; it was the fourth module here.
    for module in (observability, status, store):
        source = inspect.getsource(module)
        assert "run.llm" not in source, module.__name__
        assert 'getattr(run, "llm"' not in source, module.__name__

    assert 'run.get("llm")' not in inspect.getsource(operator_channels)


def test_historical_persisted_llm_payloads_are_ignored_by_current_serde():
    ts = now().isoformat()
    run = from_jsonable(
        AgentRun,
        {
            "id": "run_historical_llm",
            "persona_id": "dev",
            "task_id": "task_historical_llm",
            "stage_id": None,
            "state": "running",
            "started_at": ts,
            "last_heartbeat_at": ts,
            "llm": {"provider": "historical", "total_tokens": 42},
            "schema_version": 1,
        },
    )

    assert run.id == "run_historical_llm"
    assert not hasattr(run, "llm")
