"""S72 / HA-1 — the parameters that were accepted and thrown away.

The tombstone registry deliberately cannot hold these. Its own docstring says
so: *"parameter absence — that is a signature fact, checked by calling; a name
scan would fire on any unrelated local called ``tasks``."* This file is the
"checked by calling" half.

Why each one mattered
=====================

``build_snapshot(task_store=, run_store=, incident_store=)``
------------------------------------------------------------
Not merely unused — a **fake affordance with a comment claiming the
opposite**. The three names were threaded through
``_build_snapshot_uncoalesced`` into ``_build_snapshot_in_runtime_scope``,
where an AST ``Name(Load)`` walk finds them never loaded (``agent_store``,
``event_log`` and ``prompt_skills_catalogs`` beside them ARE). Their only
surviving effect was on ``custom_stores``, the flag that BYPASSES coalescing —
so passing ``task_store=<fixture>`` bought you an uncoalesced build of the
DEFAULT stores while the comment above it read:

    Injected stores (tests, doctors) must observe exactly their own fixtures

A test that injected a task store and asserted on the frame was asserting on
the real store's data and being told it was looking at its fixture. That is
the failure mode the audit calls a wrong answer believed, and it is why the
fix is deletion rather than wiring: ``status.build_status`` is the function
that genuinely reads those three, and it still does.

``_risk_if_ignored(kind, severity)`` — ``kind`` never loaded; the body
branches on ``severity`` alone. A reader of the call site would reasonably
believe the risk sentence is kind-specific. It is not.

``emit_persona_instance_remove(reason=)`` — ``emit_state_patch`` has no field
to carry a reason, so the caller computed ``safe_reason`` and handed it to a
function that dropped it. The WHY travels on the paired domain event
(``persona_instance.retired``), which does have a place for it. NOTE: the
audit that found this said the caller-side edit could be deferred to a later
stage. It could not — a caller passing a kwarg the callee no longer accepts is
a ``TypeError`` at the one moment a persona instance retires. Both halves land
together, and that is what the third test below pins.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from agent_runtime import observability, snapshot as snapshot_mod, state_patches
from agent_runtime import persona_assignments


_SNAPSHOT_BUILDERS = (
    snapshot_mod.build_snapshot,
    snapshot_mod._build_snapshot_uncoalesced,
    snapshot_mod._build_snapshot_in_runtime_scope,
)


@pytest.mark.parametrize("dropped", ["task_store", "run_store", "incident_store"])
def test_no_snapshot_builder_accepts_a_store_it_never_reads(dropped: str):
    """The whole chain, not just the public door.

    Pinned at all three levels on purpose: the parameter was a pass-through,
    so re-adding it to the public function alone would restore the lie, and
    re-adding it to the private tail alone would restore the dead weight.
    """

    for builder in _SNAPSHOT_BUILDERS:
        params = inspect.signature(builder).parameters
        assert dropped not in params, (
            f"{builder.__qualname__} accepts `{dropped}` again. It is not read "
            "by _build_snapshot_in_runtime_scope; its only effect is to set "
            "`custom_stores` and bypass coalescing, which makes the caller "
            "believe it is observing its own fixture while the frame is built "
            "from the default stores."
        )


def test_the_stores_a_snapshot_builder_does_read_are_still_accepted():
    """The other half — this gate must be able to fail for the right reason.

    Without it, deleting `agent_store` or `event_log` too would leave the test
    above green, which would make it a gate that only ever says yes.
    """

    params = inspect.signature(snapshot_mod.build_snapshot).parameters
    for kept in ("agent_store", "event_log", "prompt_skills_catalogs"):
        assert kept in params, (
            f"`{kept}` is READ by _build_snapshot_in_runtime_scope — dropping "
            "it is a behaviour change, not a dead-parameter cleanup"
        )


def test_build_snapshot_refuses_a_store_it_cannot_honour():
    """A caller who reaches for the old spelling gets an error, not silence."""

    with pytest.raises(TypeError):
        snapshot_mod.build_snapshot(task_store=object())


def test_risk_if_ignored_takes_only_what_it_branches_on():
    params = list(inspect.signature(observability._risk_if_ignored).parameters)
    assert params == ["severity"], (
        "`kind` is back on _risk_if_ignored. The body branches on `severity` "
        "alone, so a `kind` parameter advertises a kind-specific risk sentence "
        f"the function does not produce (params: {params})"
    )


def test_persona_instance_remove_takes_no_reason_and_no_caller_passes_one():
    """Both halves of the same fact, in one test, deliberately.

    Splitting them is how the caller-side edit gets deferred to "a later
    stage" and the retire path raises TypeError in between.
    """

    params = inspect.signature(state_patches.emit_persona_instance_remove).parameters
    assert "reason" not in params, (
        "emit_persona_instance_remove accepts `reason` again — `emit_state_patch` "
        "has no field to carry it, so it can only be computed and discarded"
    )
    # AST, not a source grep: the question is whether any CALL passes a
    # `reason` keyword, and a text scan cannot tell a call from the comment
    # explaining why the kwarg went.
    tree = ast.parse(inspect.getsource(persona_assignments))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
        == "emit_persona_instance_remove"
    ]
    assert calls, "no call site left — the retire path lost its remove patch"
    for call in calls:
        assert not any(kw.arg == "reason" for kw in call.keywords), (
            "a caller passes `reason=` to emit_persona_instance_remove again. "
            "The callee no longer accepts it, so this is a TypeError at the "
            "moment a persona instance retires."
        )
