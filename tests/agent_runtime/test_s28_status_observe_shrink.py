"""S28 finishes the S21 hollow-seam cut across the CLI boundary.

S21 (``c12e6850d``) removed every status/observe field that had become a
constant by construction, but stopped short of two cuts and said so in its
commit body under ``RETAINED ON PURPOSE``: ``build_status`` kept ``open_tasks``
and ``running_runs``, and ``build_observability`` kept its ``tasks`` /
``proofs`` / ``daemon_status`` parameters. Both were held for the same reason --
the only code still reading them lived in
``hermes_cli/harness_parts/runtime_commands.py``, a module another lane owned at
the time, and dropping the fields without that one-line edit would have broken
``harness status``. The reasoning was pinned in-code at ``status.py:64-71``.
That module is free now, so both halves land together.

What goes is not a cosmetic trim -- each of these is a literal wearing the shape
of a measurement:

* ``tasks`` was a ``[]`` literal in BOTH callers (``status.build_status`` line
  ``tasks = []`` since S8, and ``_cmd_observe``), so ``signals.open_tasks``, the
  repeated-context-request and issue-discovery counters, the three intervention
  families those tasks produced, and the task half of ``_self_heal_signals``
  could only ever report ``0`` / ``[]``.
* ``proofs`` was a ``[]`` literal in both callers and the body only measured its
  length: ``signals.proofs_total`` has read ``0`` since the ``proofs/`` store
  went in S6.
* ``daemon_status`` was ``None`` in both callers. The Mission Daemon was retired
  before this wave (``status.py`` hardcodes ``execution_mode = "manual"``), so
  every ``freshness.daemon_*`` row, the ``stale_daemon`` signal, and the three
  daemon interventions were derived from a ``{"state": "offline"}`` default.

``open_tasks`` / ``running_runs`` on the status payload are the same class: the
task list is a literal, and no production path constructs an ``AgentRun`` or
opens a run. S33 retired ``_attach_repo_baseline`` after ``a54e802cd`` proved it
had had zero callers since S5, leaving ``progress.RunProgressSink`` as the sole
production ``RunStore().update`` caller, so the RUNNING count can never move.

Retargeted here, citing this wave (their subject was the daemon lane, which no
longer reaches observability at all):

* ``tests/agent_runtime/test_observability.py::test_non_offline_daemon_without_heartbeat_is_critical``
* ``tests/agent_runtime/test_observability.py::test_manual_mode_does_not_page_stale_idle_daemon_status``
* ``tests/agent_runtime/test_observability.py::test_daemon_mode_treats_offline_daemon_as_critical``
"""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from hermes_time import now

from agent_runtime.models import AgentRun, Incident
from agent_runtime.observability import build_observability
from agent_runtime.states import RunState
from agent_runtime.status import build_status


# --------------------------------------------------------------------------
# status.py — the last two fields computed over the empty task list
# --------------------------------------------------------------------------


def test_status_drops_the_two_fields_s21_could_not_reach(isolate_agent_runtime_root):
    data = build_status()

    assert "open_tasks" not in data
    assert "running_runs" not in data


def test_status_keeps_the_run_counters_a_reader_can_still_learn_from(isolate_agent_runtime_root):
    """Keep-side pin: only the RUNNING split went.

    ``active_runs`` / ``queued_runs`` / ``waiting_runs`` / ``stale_runs`` read
    the same persisted rows, and a historical row in any of those states is
    still information about the store. This cut is about the two names S21
    named, not about run accounting in general.
    """

    data = build_status()

    for key in ("active_runs", "queued_runs", "waiting_runs", "stale_runs", "open_incidents"):
        assert key in data, f"S28 dropped a live status field: {key}"




# --------------------------------------------------------------------------
# observability.py — three parameters both callers passed as literals
# --------------------------------------------------------------------------


def test_build_observability_no_longer_accepts_the_literal_fed_parameters():
    parameters = inspect.signature(build_observability).parameters

    for name in ("tasks", "proofs", "daemon_status"):
        assert name not in parameters, f"build_observability still accepts {name!r}"
    # The thresholds that only the removed lanes read go with them.
    for name in ("daemon_stale_after_seconds", "repeated_context_requests_threshold"):
        assert name not in parameters, f"build_observability still accepts {name!r}"


def test_passing_a_removed_parameter_is_a_hard_error():
    """Fail loud, never silently ignore: a caller that still believes in the
    daemon lane must break, not read a fabricated ``offline``."""

    with pytest.raises(TypeError):
        build_observability(runs=[], incidents=[], daemon_status={"state": "running"})
    with pytest.raises(TypeError):
        build_observability(runs=[], incidents=[], tasks=[])
    with pytest.raises(TypeError):
        build_observability(runs=[], incidents=[], proofs=[])


def test_the_envelope_drops_every_row_those_parameters_fed():
    envelope = build_observability(runs=[], incidents=[])

    for key in ("open_tasks", "proofs_total", "stale_daemon", "repeated_context_request_tasks", "untriaged_issue_discoveries"):
        assert key not in envelope["signals"], f"signals still publishes constant {key!r}"
    for key in ("daemon_heartbeat_at", "daemon_heartbeat_age_seconds", "daemon_stale_threshold_seconds"):
        assert key not in envelope["freshness"], f"freshness still publishes constant {key!r}"
    # The one freshness row with a live subject stays.
    assert envelope["freshness"]["stalled_run_threshold_seconds"] == 900


def test_the_self_heal_signal_keeps_only_its_run_sourced_counters():
    """``_self_heal_signals`` read ``task.harness_self_heal`` for seven of its
    ten counters. With no task list, only the three progress-sourced counters
    can still move."""

    envelope = build_observability(runs=[], incidents=[])
    self_heal = envelope["signals"]["self_heal"]

    assert set(self_heal) == {"skill_fanout", "failed_proof_reused", "failed_proof_ignored"}


def test_the_run_sourced_self_heal_counters_still_count():
    ts = now()
    run = AgentRun(
        id="run_s28",
        persona_id="dev",
        task_id=None,
        stage_id=None,
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
        progress={"skill_fanout_count": 2, "failed_proof_reused": True},
    )

    self_heal = build_observability(runs=[run], incidents=[], reference_time=ts)["signals"]["self_heal"]

    assert self_heal == {"skill_fanout": 2, "failed_proof_reused": 1, "failed_proof_ignored": 0}




def test_the_surviving_interventions_still_fire():
    """Keep-side: incidents and stalled runs are fed by live parameters and are
    the whole reason ``harness observe`` exists. (S56 removed the third member of
    this set, the ``worker_stale_heartbeat`` family, with the ``worker_sessions=``
    parameter that fed it.)"""

    ts = now()
    incident = Incident(
        id="inc_s28",
        task_id=None,
        run_id=None,
        kind="model_invalid_output",
        summary="private marker should never leak",
        detail_path=None,
        opened_at=ts,
    )

    envelope = build_observability(runs=[], incidents=[incident], reference_time=ts)

    assert [item["kind"] for item in envelope["interventions"]] == ["open_incident"]
    assert envelope["health"]["status"] == "critical"
    assert envelope["signals"]["open_incidents"] == 1


def test_the_live_run_signals_are_untouched():
    ts = now()
    runs = [
        AgentRun(id="run_q", persona_id="dev", task_id=None, stage_id=None, state=RunState.QUEUED, started_at=ts, last_heartbeat_at=ts),
        AgentRun(id="run_r", persona_id="dev", task_id=None, stage_id=None, state=RunState.RUNNING, started_at=ts, last_heartbeat_at=ts),
    ]

    signals = build_observability(runs=runs, incidents=[], reference_time=ts)["signals"]

    assert signals["active_runs"] == 2
    assert signals["queued_runs"] == 1
    assert signals["running_runs"] == 1
    # INVERTED at S56 (was `signals["active_worker_sessions"] == 0`). The
    # `worker_sessions=` parameter and both worker signals went with the
    # WorkerSessionStore write lane; the run signals beside them are the live
    # half this case exists to protect and are unchanged.
    assert "active_worker_sessions" not in signals
    assert "stale_worker_sessions" not in signals


def test_status_still_embeds_a_working_observability_envelope(isolate_agent_runtime_root):
    """The verb must keep answering — this is the cross-module wiring pin."""

    envelope = build_status()["observability"]

    assert envelope["schema_version"] == 1
    assert envelope["execution_mode"] == "manual"
    assert "open_tasks" not in envelope["signals"]
