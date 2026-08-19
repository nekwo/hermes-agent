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

-----------------------------------------------------------------------------
MIGRATED TO ``test_tombstone_registry.py`` (2026-08-01)
-----------------------------------------------------------------------------

Eight pure-ABSENCE cases left this file. Everything below the "The wire" banner
stayed, in full:

* ``test_the_worker_session_module_is_gone`` -> ``MODULE`` row
  ``agent_runtime.worker_sessions`` (the registry row also carries the guarded
  sub-step's method note about ``paths.store_root()``).
* ``test_the_worker_session_model_is_gone`` -> ``ATTR`` row ``WorkerSession``
  (``agent_runtime.models``).
* ``test_the_orphaned_path_helpers_are_gone`` -> four ``ATTR`` rows
  (``agent_runtime.paths``).
* ``test_the_worker_session_lock_is_gone`` -> ``ATTR`` row
  ``worker_session_lock`` (``agent_runtime.locks``).
* ``test_the_worker_derived_surface_is_gone`` -> six ``ATTR`` rows
  (``agent_runtime.persona_assignments``); the two roster-gate names ALSO carry
  repo-wide ``CODE`` rows.
* ``test_the_worker_derived_store_methods_are_gone`` -> three ``CLASS_ATTR``
  rows on ``persona_assignments.PersonaInstanceStore``.
* ``test_the_constant_only_repo_bundle_projections_are_gone`` -> the S56
  ``CODE`` rows for the four constant-only summaries plus
  ``REPO_BUNDLE_DELIVERY_CONTRACT`` / ``REPO_BUNDLE_CHECKOUT_STATUS``, and the
  S57 ``MODULE`` row for the module they lived in. The registry matches RENDERED
  code, so it also catches a rebirth that is not spelled ``def name(``.
* ``test_the_production_envelope_is_gone`` -> the S49 ``MODULE`` row
  ``agent_runtime.production_envelope`` + the S56 ``CODE`` row
  ``_swarm_budget_summary``. **Its finding is preserved here rather than lost
  with the case**: the envelope was DELETED, not reworded, because every
  ``controls`` entry was hand-written prose with no executable backing and
  several were FALSE against this tree — H6's "worker.pause and worker.resume
  capabilities are registered" (only ``worker.resume`` was ever registered, and
  both verbs' implementations went with the write lane), H6's "daemon stop/kill
  paths" and H8/H9's ticker and daemon-queue claims (the Mission Daemon was
  retired), H8's "role envelopes ... are file-backed" (S44 deleted that store),
  H9's "repo bundle queueing gates dependent handoffs" (S52 deleted every
  writer), and H7/H9's swarm-budget arms (the enforcement never existed). The
  same reversal is still asserted as an INVERTED pin in
  ``test_s49_operator_control_removal``.

WHY THE SURVIVORS STAYED:

* **The whole wire section (contract 47).** The contract-version FLOOR (the
  current value has one authority,
  ``test_s47_wire_constant_field_removal.CURRENT_CONTRACT_VERSION``), the four
  PARAMETER-absence checks over ``build_snapshot`` /
  ``_build_snapshot_uncoalesced`` / ``build_status`` / ``build_observability`` /
  ``build_dirty_state``, the observability and dirty-state ENVELOPE shapes, the
  ``build_status`` wire-key scan, and the ``runtime_instances["lanes"]`` KEEP.
  The registry's own docstring names parameter absence and wire-key absence as
  deliberately NOT rows.
* ``test_the_persona_instance_worker_field_is_gone`` — a
  ``__dataclass_fields__`` key-set pin.
* ``test_every_worker_session_contract_is_de_registered`` — a PREFIX property
  over the whole catalog, which twelve individual ``EVENT`` rows cannot state
  about a thirteenth type nobody has invented yet. Same for
  ``test_no_worker_session_type_was_an_operator_summary_type``.
* ``test_the_event_count_delta_is_ten`` — agreement with S15's single authority.
* The checkpoint ``ENTITY_CLASS_NAMES`` pins (both the removals and the
  deliberate ``runtime_instances`` KEEP) — membership in a produced collection.
* ``test_the_worker_session_state_enum_survives...``,
  ``test_the_derivation_is_now_a_worker_free_ensure_pass``,
  ``test_chat_busy_error_carries_only_the_run_binding``,
  ``test_the_surviving_chat_guard_is_the_run_arm`` — KEEPs and signature pins.
* ``test_the_busy_reason_vocabulary_lost_its_worker_arm`` — the ``ATTR`` half is
  a registry row, but the ``__all__`` half is not: a name exported and NOT
  defined is its own defect, so the case stays whole.
* ``test_a_stale_row_still_carrying_the_removed_field_cannot_resurrect_a_reader``
  — a load-and-ignore behaviour proof (the s53 precedent).
* ``test_the_repo_bundle_store_read_side_went_at_s57`` — an INVERTED pin
  recording that S56's deliberate KEEP was correct for exactly one commit.
"""

from __future__ import annotations

import ast
import importlib
import inspect

import pytest

from agent_runtime import (
    checkpoint,
    dirty_state,
    models,
    observability,
    persona_assignments,
    persona_profile_binding,
    snapshot,
    status,
)
from agent_runtime.decision_contract_registry import event_catalog
from agent_runtime.events import ALLOWED_EVENT_TYPES, OPERATOR_SUMMARY_EVENT_TYPES
from agent_runtime.persona_assignments import PersonaInstanceStore


# --------------------------------------------------------------------------
# The module, the store, and everything it exported
# --------------------------------------------------------------------------


def test_the_worker_session_state_enum_survives_because_persona_instances_use_it():
    """The STATE vocabulary is not the store. ``PersonaInstance.state`` is typed
    on it, so cutting it would be a different (and wrong) change."""
    from agent_runtime.states import WorkerSessionState

    assert WorkerSessionState.IDLE.value == "idle"
    fields = {field.name: field for field in models.PersonaInstance.__dataclass_fields__.values()}
    assert "state" in fields


def test_the_persona_instance_worker_field_is_gone():
    assert "active_worker_session_id" not in models.PersonaInstance.__dataclass_fields__


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


# --------------------------------------------------------------------------
# The worker-derived persona-instance surface
# --------------------------------------------------------------------------


def test_the_derivation_is_now_a_worker_free_ensure_pass():
    """``ensure_for_personas`` is the SAME behaviour under an honest name: the
    ``workers`` half could not be exercised on the live tree (``build_snapshot``
    had been passing a ``workers = []`` literal since S47), so every persona
    already fell through to the configured/idle reset."""
    assert callable(PersonaInstanceStore.ensure_for_personas)
    params = list(inspect.signature(PersonaInstanceStore.ensure_for_personas).parameters)
    assert params == ["self", "personas"]


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
    """S57 moved the frame again (47 -> 48) for its own removals, so this pin is
    a FLOOR, not a second copy of the current number. The property this file owns
    is that S56's move happened and can never regress; the current value has one
    authority (``agent_runtime.snapshot.SNAPSHOT_CONTRACT_VERSION``) and this is
    deliberately not a second one.

    RE-AIMED 2026-08-09. This used to read ``_parity_envelope``'s SOURCE, take
    every line starting ``"contract_version":`` and ``int()`` the text after the
    colon — which worked only while the value was written there as a literal.
    The authority change made the envelope read a named constant, and the parse
    blew up on ``SNAPSHOT_CONTRACT_VERSION``: a false red against a frame whose
    version had not moved at all.

    Both halves of the guarantee survive, each expressed against what it is
    actually about:

    * the FLOOR is a property of the emitted NUMBER, so it is asserted on the
      number (and on a really-built frame, not on prose about one);
    * "not a second copy" is a property of the envelope's STRUCTURE — that it
      sources the version from the single authority instead of restating it — so
      it is asserted on the AST, where a reformat cannot break it and a
      re-introduced literal cannot hide from it."""

    assert snapshot.SNAPSHOT_CONTRACT_VERSION >= 47
    assert snapshot.build_snapshot()["parity"]["contract_version"] >= 47

    envelope = next(
        node
        for node in ast.walk(ast.parse(inspect.getsource(snapshot._parity_envelope)))
        if isinstance(node, ast.Dict)
        and any(isinstance(k, ast.Constant) and k.value == "contract_version" for k in node.keys)
    )
    values = [
        value
        for key, value in zip(envelope.keys, envelope.values)
        if isinstance(key, ast.Constant) and key.value == "contract_version"
    ]
    assert len(values) == 1, values
    assert isinstance(values[0], ast.Name), (
        "the parity envelope states the contract version as a literal again. It "
        "must read the single authority (snapshot.SNAPSHOT_CONTRACT_VERSION) — a "
        "second copy is how six restatements rotted out of step in the first place."
    )


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
    runtime = dirty_state.build_dirty_state(runs=[], repos=[])["runtime"]
    for key in (
        "active_worker_sessions",
        "possessed_worker_sessions",
        "active_worker_session_ids",
        "possessed_worker_session_ids",
    ):
        assert key not in runtime


def test_the_repo_bundle_store_read_side_went_at_s57():
    """INVERTED at S57 (was ``test_the_repo_bundle_store_read_side_survives``).

    S56 KEPT the store deliberately and said so here. That keep was correct for
    exactly one commit: removing the four projections in the same change left the
    store with zero production importers, which doc 19 recorded as newly-opened
    debt. S57 took it. The pin inverts so the keep and its reversal both stay
    legible — a store kept "because the read side is live" must be re-checked the
    moment its last reader leaves."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_runtime.repo_bundles")
    assert not hasattr(models, "RepoBundle")


#: The ten constant wire rows S56/S57 cut from the status frame.
_RETIRED_STATUS_KEYS = (
    "worker_sessions",
    "active_worker_sessions",
    "repo_bundles",
    "repo_bundle_closeout",
    "bundle_queue",
    "repo_locks",
    "lanes",
    "production_envelope",
    "swarm",
    "swarm_budget",
)


def _status_module_emitted_keys() -> set[str]:
    """Every string key ``agent_runtime.status`` writes into a dict, module-wide.

    Two forms, because the module uses both: ``data["x"] = ...`` (an
    ``ast.Subscript`` store with a string slice, which is how the frame is
    actually assembled) and ``{"x": ...}`` literals. Read off the AST, so the
    retirement COMMENTS this module carries for all ten cut keys — lines 92-104
    — cannot be mistaken for emissions."""

    module = ast.parse(inspect.getsource(status))
    keys: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    keys.add(target.slice.value)
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
    return keys


def test_the_status_frame_dropped_every_constant_wire_row(isolate_agent_runtime_root):
    """RE-AIMED 2026-08-19 (MCF-53 sweep). This was VACUOUS THREE WAYS OVER.

    It read ``inspect.getsource(status.build_status)`` — ONE statement, a
    ``with`` wrapping ``return _build_status_in_runtime_scope(...)``, 12 lines
    and 713 characters. The third instance of the same wrapper-walk defect
    MCF-47 records for ``_build_snapshot_uncoalesced``. Killer evidence: the
    frame this gate is named for demonstrably emits ``operator_channels``, and
    ``"operator_channels"`` does not appear in the source it scanned. If the
    LIVE keys were invisible to it, so were the ten retired ones.

    Second, independent defect: the spelling was wrong even for the right scope.
    The frame is assembled as ``data["operator_channels"] = ...``, and the gate
    looked for ``"operator_channels":`` — the dict-literal form, with a colon.
    Zero of the twelve keys match that form anywhere in ``status.py``.

    Third: all ten tokens DO occur in ``status.py``, every one of them inside the
    retirement comments the cut wrote. A naive re-aim by raw substring goes red
    on its own tombstone — which is why the structural half below is AST, and
    comment-immune by construction.

    The guarantee is a fact about the PRODUCED frame, so that is asserted first
    and directly; the AST half is the structural pin that also covers the arms a
    default-fixture build does not reach."""

    from agent_runtime.migrations import effective_config_summary

    assert "production_envelope" not in effective_config_summary()

    # The wire itself.
    frame = status.build_status()
    assert frame, "build_status produced nothing; the behavioural half is vacuous"
    for key in _RETIRED_STATUS_KEYS:
        assert key not in frame, key

    # The producer, module-wide and comment-immune. Anti-vacuity first: a live
    # key the frame demonstrably carries must be visible to the derivation, or
    # the absence loop below means nothing.
    emitted = _status_module_emitted_keys()
    assert "operator_channels" in emitted, (
        "the key derivation cannot see operator_channels, which build_status "
        f"emits — it is reading the wrong thing. Derived: {sorted(emitted)}"
    )
    for key in _RETIRED_STATUS_KEYS:
        assert key not in emitted, key


def test_runtime_instances_keeps_its_own_lanes_sub_key():
    """Only the DUPLICATE top-level ``lanes`` row went. The projection block is
    unchanged, so no number an operator reads became unreachable."""
    from agent_runtime.runtime_instances import runtime_instances_summary

    assert "lanes" in runtime_instances_summary([])
