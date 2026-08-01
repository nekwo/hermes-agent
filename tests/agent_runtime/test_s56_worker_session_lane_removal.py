"""S56 removes the worker-session lane WHOLE, and the wire it fed.

Operator-ruled CUT on 2026-08-01. Unlike S52/S53 (write-lane cuts that left a
live read side), this is a MODULE removal: once the write lane went, the only
importers left for ``agent_runtime/worker_sessions.py`` were its own tests and
the worker-derived persona-instance surface, which went with it.

The ruling was GUARDED. ``derive_from_workers`` / ``update_from_worker`` /
``_worker_carries_live_binding`` / ``ACTIVE_PERSONA_WORKER_STATES`` and the
worker half of ``ChatBusyError`` / ``_terminate_live_chat_bindings`` were only
cuttable if no live persona instance carried a non-null
``active_worker_session_id`` AND the store could no longer produce one.

**What the re-verification found, and why the first answer was wrong.** A scan
of ``X:\\Eternia\\.hermes\\profiles\\alice\\agent_runtime\\persona_instances``
reports THREE of four instances carrying a worker id
(``personainst_backend_dev`` -> ``worker_237134aae4bd``, ``_neko_supervisor`` ->
``worker_ff10543cffdc``, ``_qa`` -> ``worker_e392c8d410a2``), beside a
``worker_sessions/`` directory holding sixteen rows. That tree is NOT the live
runtime root. ``paths.store_root()`` under ``HERMES_HOME=...\\profiles\\alice``
resolves to ``X:\\Eternia\\.hermes\\agent-runtime`` — the root
``runtime_commands`` pins for ``live-tony`` — where there are FIFTEEN persona
instances, ALL with ``active_worker_session_id: null``, and NO
``worker_sessions/`` directory at all. The profile-scoped tree was last written
2026-06-18 and is an orphan the runtime does not resolve to. The guard's
condition therefore held on the root that matters, and the surface was cut.

The method note is worth more than the result: a profile directory that LOOKS
like a store root is not evidence about the store root. Ask ``paths``.

**Count: 68 -> 58.** All ten ``worker_session.*`` contracts are de-registered in
the same commit as their emitters, because S55's gate asserts every registered
type HAS an emitter — splitting them would have turned the cut red instead of
letting it land silently. ``events.OPERATOR_SUMMARY_EVENT_TYPES`` never named
one of the ten, so unlike S52 there is no summary arm to retire alongside.
"""

from __future__ import annotations

import inspect
from importlib.util import find_spec

import pytest

from agent_runtime import (
    checkpoint,
    dirty_state,
    locks,
    models,
    observability,
    paths,
    persona_assignments,
    persona_profile_binding,
    repo_bundles,
    snapshot,
    status,
)
from agent_runtime.decision_contract_registry import event_catalog
from agent_runtime.events import ALLOWED_EVENT_TYPES, OPERATOR_SUMMARY_EVENT_TYPES
from agent_runtime.persona_assignments import PersonaInstanceStore


# --------------------------------------------------------------------------
# The module, the store, and everything it exported
# --------------------------------------------------------------------------


def test_the_worker_session_module_is_gone():
    assert find_spec("agent_runtime.worker_sessions") is None


def test_the_worker_session_model_is_gone():
    assert not hasattr(models, "WorkerSession")


def test_the_worker_session_state_enum_survives_because_persona_instances_use_it():
    """The STATE vocabulary is not the store. ``PersonaInstance.state`` is typed
    on it, so cutting it would be a different (and wrong) change."""
    from agent_runtime.states import WorkerSessionState

    assert WorkerSessionState.IDLE.value == "idle"
    fields = {field.name: field for field in models.PersonaInstance.__dataclass_fields__.values()}
    assert "state" in fields


def test_the_persona_instance_worker_field_is_gone():
    assert "active_worker_session_id" not in models.PersonaInstance.__dataclass_fields__


@pytest.mark.parametrize(
    "name",
    [
        "worker_sessions_dir",
        "worker_session_path",
        "worker_context_dir",
        "proof_sandbox_root",
    ],
)
def test_the_orphaned_path_helpers_are_gone(name):
    assert not hasattr(paths, name)


def test_the_worker_session_lock_is_gone():
    assert not hasattr(locks, "worker_session_lock")


def test_the_writer_less_checkpoint_rows_are_gone():
    names = set(checkpoint.ENTITY_CLASS_NAMES)
    assert "worker_sessions" not in names
    # S52 left ``repo_bundles`` writer-less; S56 left it reader-less too.
    assert "repo_bundles" not in names


def test_runtime_instances_keeps_its_checkpoint_row_on_purpose():
    """Also writer-less (S53) — but its rows still ship on the status wire, so a
    checkpoint that omitted them would drop state a reader can still see."""
    assert "runtime_instances" in set(checkpoint.ENTITY_CLASS_NAMES)


# --------------------------------------------------------------------------
# Event contracts
# --------------------------------------------------------------------------


def test_every_worker_session_contract_is_de_registered():
    assert [name for name in event_catalog() if name.startswith("worker_session.")] == []
    assert [name for name in ALLOWED_EVENT_TYPES if name.startswith("worker_session.")] == []


def test_no_worker_session_type_was_an_operator_summary_type():
    assert [name for name in OPERATOR_SUMMARY_EVENT_TYPES if name.startswith("worker_session.")] == []


def test_the_event_count_delta_is_ten():
    """Delta only. The ABSOLUTE count lives in exactly one place —
    ``test_s15_event_contract_pruning.SURVIVING_EVENT_COUNT`` — because holding a
    second copy is what broke S44/S49/S52/S53 every time a later cut landed."""
    from tests.agent_runtime.test_s15_event_contract_pruning import SURVIVING_EVENT_COUNT

    assert len(event_catalog()) == SURVIVING_EVENT_COUNT
    assert SURVIVING_EVENT_COUNT == 68 - 10


# --------------------------------------------------------------------------
# The worker-derived persona-instance surface
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "ACTIVE_PERSONA_WORKER_STATES",
        "_worker_carries_live_binding",
        "_live_chat_bindings",
        "_terminate_live_chat_bindings",
        "persona_instance_runtime_enabled",
        "persona_assignment_store_enabled",
    ],
)
def test_the_worker_derived_surface_is_gone(name):
    assert not hasattr(persona_assignments, name)


@pytest.mark.parametrize("name", ["derive_from_workers", "update_from_worker", "_goal_id_for_worker"])
def test_the_worker_derived_store_methods_are_gone(name):
    assert not hasattr(PersonaInstanceStore, name)


def test_the_derivation_is_now_a_worker_free_ensure_pass():
    """``ensure_for_personas`` is the SAME behaviour under an honest name: the
    ``workers`` half could not be exercised on the live tree (``build_snapshot``
    had been passing a ``workers = []`` literal since S47), so every persona
    already fell through to the configured/idle reset."""
    assert callable(PersonaInstanceStore.ensure_for_personas)
    params = list(inspect.signature(PersonaInstanceStore.ensure_for_personas).parameters)
    assert params == ["self", "personas"]


def test_chat_busy_error_carries_only_the_run_binding():
    params = list(inspect.signature(persona_assignments.ChatBusyError.__init__).parameters)
    assert params == ["self", "instance", "active_run_id"]


def test_the_surviving_chat_guard_is_the_run_arm():
    assert callable(persona_assignments._live_chat_binding)
    assert callable(persona_assignments._terminate_live_chat_binding)
    # Text gate over CODE only — the comment recording the removal must not
    # satisfy or trip its own contract (the s45/S46 rule).
    for fn in (persona_assignments._live_chat_binding, persona_assignments._terminate_live_chat_binding):
        code = [
            line.split("#", 1)[0]
            for line in inspect.getsource(fn).splitlines()
            if not line.strip().startswith("#")
        ]
        assert "worker" not in " ".join(code).lower()


def test_the_busy_reason_vocabulary_lost_its_worker_arm():
    assert not hasattr(persona_profile_binding, "BUSY_ACTIVE_WORKER")
    assert "BUSY_ACTIVE_WORKER" not in persona_profile_binding.__all__


def test_a_stale_row_still_carrying_the_removed_field_cannot_resurrect_a_reader():
    """Fixture keeps SENDING the removed field on purpose (s53 precedent): the
    busy resolver must ignore it rather than grow the arm back."""
    row = {"state": "idle", "active_worker_session_id": "worker_deadbeef"}
    assert persona_profile_binding.instance_busy_reason(row) is None


# --------------------------------------------------------------------------
# The wire (contract 47)
# --------------------------------------------------------------------------


def test_the_snapshot_contract_version_moved():
    src = inspect.getsource(snapshot._parity_envelope)
    assert '"contract_version": 47,' in src
    assert '"contract_version": 46,' not in src


def test_build_snapshot_no_longer_accepts_a_worker_store():
    for fn in (snapshot.build_snapshot, snapshot._build_snapshot_uncoalesced):
        assert "worker_session_store" not in inspect.signature(fn).parameters


def test_build_status_no_longer_accepts_a_worker_store():
    assert "worker_session_store" not in inspect.signature(status.build_status).parameters


def test_build_observability_no_longer_accepts_worker_sessions():
    assert "worker_sessions" not in inspect.signature(observability.build_observability).parameters


def test_build_dirty_state_no_longer_accepts_workers():
    assert "workers" not in inspect.signature(dirty_state.build_dirty_state).parameters


def test_the_observability_envelope_dropped_every_worker_row():
    data = observability.build_observability(runs=[], incidents=[], events=[])
    assert "worker_sessions" not in data
    assert "active_worker_sessions" not in data["signals"]
    assert "stale_worker_sessions" not in data["signals"]
    assert [item for item in data["interventions"] if item.get("subject") == "worker_session"] == []


def test_the_dirty_state_envelope_dropped_every_worker_row():
    runtime = dirty_state.build_dirty_state(tasks=[], runs=[], repos=[])["runtime"]
    for key in (
        "active_worker_sessions",
        "possessed_worker_sessions",
        "active_worker_session_ids",
        "possessed_worker_session_ids",
    ):
        assert key not in runtime


@pytest.mark.parametrize(
    "name",
    [
        "repo_bundle_summary",
        "repo_bundle_delivery_summary",
        "bundle_queue_summary",
        "repo_lock_summary",
        "REPO_BUNDLE_DELIVERY_CONTRACT",
        "REPO_BUNDLE_CHECKOUT_STATUS",
    ],
)
def test_the_constant_only_repo_bundle_projections_are_gone(name):
    """doc 19 filed ``repo_lock_summary`` as a wire that could only ever report
    ``{"lock_count": 0, "locks": []}`` after S52 deleted both lock writers. The
    other three had the same shape for the same reason. ``status.py`` was the
    sole caller of all four."""
    assert not hasattr(repo_bundles, name)


def test_the_repo_bundle_store_read_side_survives():
    assert callable(repo_bundles.RepoBundleStore.list_all)
    assert callable(repo_bundles.RepoBundleStore.get)


def test_the_production_envelope_is_gone():
    """Deleted, not reworded. Every ``controls`` entry was hand-written prose
    with no executable backing, and several were FALSE against this tree: H6's
    "worker.pause and worker.resume capabilities are registered" (only
    ``worker.resume`` was ever registered, and both verbs' implementations went
    with the write lane), H6's "daemon stop/kill paths" and H8/H9's ticker and
    daemon-queue claims (the Mission Daemon was retired), H8's "role envelopes
    ... are file-backed" (S44 deleted that store), H9's "repo bundle queueing
    gates dependent handoffs" (S52 deleted every writer), and H7/H9's swarm
    budget arms (the enforcement never existed). What remained after the config
    cuts was prose keyed on flags that no longer exist."""
    assert find_spec("agent_runtime.production_envelope") is None
    assert not hasattr(status, "_swarm_budget_summary")


def test_the_status_frame_dropped_every_constant_wire_row():
    from agent_runtime.migrations import effective_config_summary

    assert "production_envelope" not in effective_config_summary()
    src = inspect.getsource(status.build_status)
    for key in (
        '"worker_sessions"',
        '"active_worker_sessions"',
        '"repo_bundles"',
        '"repo_bundle_closeout"',
        '"bundle_queue"',
        '"repo_locks"',
        '"lanes"',
        '"production_envelope"',
        '"swarm"',
        '"swarm_budget"',
    ):
        assert f"{key}:" not in src, key


def test_runtime_instances_keeps_its_own_lanes_sub_key():
    """Only the DUPLICATE top-level ``lanes`` row went. The projection block is
    unchanged, so no number an operator reads became unreachable."""
    from agent_runtime.runtime_instances import runtime_instances_summary

    assert "lanes" in runtime_instances_summary([])
