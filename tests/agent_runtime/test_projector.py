"""What survives of the projector's tests after S46 retired the incremental lane.

Five tests here drove ``Projector.apply_pending`` or the lease it took —
``test_replay_equivalence_full_vs_incremental_goal_row``,
``test_replay_equivalence_goal_row_carries_bundle_assignment_and_lane_state``,
``test_apply_pending_is_o_delta_on_rd0_fixture``,
``test_lease_excludes_second_projector``,
``test_registered_event_rebuilds_the_whole_frame`` — a lane with zero production
callers (ledger item 9, operator-ruled RETIRE 2026-08-01). Their absence is
asserted by ``test_s46_incremental_projection_lane_removal.py``.

Nothing they claimed went unpinned. The two replay-equivalence tests asserted
``contract_version == 45``, ``"goals" not in``, and ``"boards" in`` on a rendered
frame; ``test_snapshot.py`` pins all three on ``build_snapshot()`` and
``test_read_model.py::test_apply_full_rebuild_then_render_is_equivalent`` pins
``render_snapshot() == build_snapshot()`` byte-for-byte through the read model,
so the composition covers the round trip via the LIVE path. ``full_rebuild``
itself keeps two live witnesses: the CLI round trip below, and
``test_read_model_frame_source.py::test_both_write_sites_record_the_same_watermark``.

Four module-level helpers went with them — ``_seed_open_task``,
``_write_enterprise_config``, ``_row_diff``, ``_goal``. Re-checked repo-wide
rather than assumed: each was defined here, imported nowhere, and called by no
surviving test. They were the goal-row comparison scaffolding the
replay-equivalence pair needed and had already outlived the goal rows.
"""

from __future__ import annotations

import json
from argparse import Namespace

from hermes_time import now

from agent_runtime.events import EventLog
from agent_runtime.models import Event

# S44: `from agent_runtime.role_envelopes import RoleEnvelopeStore` stood here
# and was never used by a single test in this file — it was the ONLY importer of
# that module anywhere, production or test. An unused import is what a
# module-level reachability gate counts as a caller, which is how the store
# family survived three earlier removal waves.


def test_event_log_iter_from_offset_resumes_at_byte_boundary(isolate_agent_runtime_root):
    log = EventLog()
    log.append(Event(ts=now(), type="persona_instance.created", task_id="t1", run_id=None, persona_id=None))
    first_offset = isolate_agent_runtime_root.joinpath("events.jsonl").stat().st_size
    log.append(Event(ts=now(), type="persona_instance.created", task_id="t2", run_id=None, persona_id=None))

    events = list(log.iter_from_offset(first_offset))

    assert len(events) == 1
    assert events[0][0] == isolate_agent_runtime_root.joinpath("events.jsonl").stat().st_size
    assert events[0][1].task_id == "t2"


def test_rebuild_and_read_projection_cli(isolate_agent_runtime_root, capsys):
    """The projector's ONE production entry point, end to end.

    ``_cmd_rebuild_read_model`` is the only caller of ``Projector.full_rebuild``
    outside tests, which is precisely why the rest of the class was retirable and
    this is not.
    """

    import hermes_cli.harness as harness

    EventLog().append(Event(now(), "persona.updated", None, None, "profile:cli", {}))

    assert harness._cmd_rebuild_read_model(Namespace(json=True)) == 0
    rebuild_payload = json.loads(capsys.readouterr().out)
    assert rebuild_payload["ok"] is True
    assert rebuild_payload["watermark"]["event_offset"] > 0

    assert harness._cmd_read_projection(Namespace(projection="agent_instances", since_offset=None, json=True)) == 0
    read_payload = json.loads(capsys.readouterr().out)
    assert read_payload["projection"] == "agent_instances"
    assert read_payload["rows"] == []
