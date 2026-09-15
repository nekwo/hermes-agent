"""S29 retires ``agent_runtime.context_builder`` — the last of the tick lane.

The module's own docstring stated the debt this contract discharges:

    "What remains is the SHAPE. ``persona_runtime`` still annotates its
    repo-grounding and tool-budget helpers against ``AgentContext``. With the
    builder gone nothing produces one, so those helpers are a second-order
    orphan — recorded as follow-up debt, not silently kept alive by a producer
    that no longer exists."

S27 (``539bf5813``) kept ``AgentContext`` for exactly one reason: the annotation
in ``persona_runtime``. S29 removed those eight annotated helpers — none had a
caller, and with no producer none could be handed a real context — which left
the dataclass with no producer AND no consumer anywhere in production. A
dataclass reachable only from the tests that construct it is not a shape the
system uses; it is residue with a type. The module held nothing else: an
``AgentRun`` import for the annotation, and ``TaskLike = Any``, which existed
solely to annotate ``AgentContext.task`` after the ``Task`` record went in S8.

Deleted with it, because every assertion in them was about names inside a module
that no longer exists (a deleted module satisfies "does not have attribute X"
vacuously):

* ``tests/agent_runtime/test_s27_context_builder_lane_removal.py`` — **deleted**,
  5 tests.
  Its one gate that was NOT about the module's contents was carried below and
  later retargeted by S39 (``149a9ae53``) when fresh-row ``mission_hud`` writes
  were retired while historical Launcher reads stayed supported.
* Five gates in ``test_s19_context_builder_cluster_removal.py``; that file keeps
  its ``mission_hud_preview`` entry-point and live-HUD halves.
* ``tests/agent_runtime/test_persona_memory_scope.py`` — **deleted with the
  symbol** (4 tests, all of ``_persona_run_uses_memory``). Marked as deleted
  MCF-78 2026-08-20: the bare path read as a live pin.

NOT collateral: ``AgentRun`` (``agent_runtime.models``, live everywhere) and
the distinct live ``situational_hud`` observability projection. Both are pinned
below; S39 separately proves historical ``mission_hud`` rows remain readable
without manufacturing that key on fresh rows.
"""

from __future__ import annotations

import inspect









def test_the_agent_run_model_is_not_collateral():
    """``context_builder`` imported ``AgentRun`` to annotate the removed shape.
    The model itself is live across the runtime and stays, while S33 removed
    persona_runtime's last repo-baseline-only import of it."""

    from agent_runtime.models import AgentRun

    assert AgentRun is not None



def test_the_contract_45_situational_hud_observability_field_survives_s39():
    """Retarget the carried S27 gate to the live projection after S39.

    S39 (``149a9ae53``) intentionally removed fresh ``mission_hud`` writes but
    kept ``situational_hud`` as the distinct runtime/steering projection.
    """

    from agent_runtime import prompt_observability

    source = inspect.getsource(prompt_observability)
    assert '"situational_hud"' in source
