"""S27 removes the ``Proof`` record and ``ProofType`` from the model layer.

``ProofType``'s own docstring declared the deadline: "retained until task records
leave in S8. Proof evaluation and execution were removed in S6; this enum remains
only so the still-readable mission records can be deserialized between stages."
S8 removed the task records. Nothing has deserialized a ``Proof`` since — there
is no ``from_jsonable(Proof, ...)`` anywhere in the repo and the ``proofs/``
store went with S6.

What kept the pair importable was three annotations and an export, none of which
were a use:

* ``context_builder`` imported ``Proof`` for ``_safe_proof_record`` — removed
  with the tick-context lane earlier in this wave.
* ``observability.build_observability(proofs: list[Proof])`` is LIVE, but both
  of its callers (``status.build_status`` and ``harness observe``) pass a ``[]``
  literal, and the body only ever computes ``len(proofs)`` -> ``0``. The
  annotation named a record no store can produce, so it was retyped honestly
  rather than deleted; the parameter and its ``proofs_total`` row stayed because
  the wire shape was unchanged. **S28 finished this**: once the CLI module that
  owned the ``proofs=[]`` keyword was free, the parameter and the row went with
  it — a row that can only read ``0`` is a literal, not a measurement. See
  ``tests/agent_runtime/test_s28_status_observe_shrink.py``.
* ``agent_runtime/__init__`` re-exported ``Proof``, and three test modules
  imported it without using it.

The keep-side names one bare-word grep away are pinned below: the ``proof_ids``
refs the continuity lane renders into the parent chat, and ``parity`` /
``patch_coverage`` (doc 16's named keeps) are all unaffected.
"""

from __future__ import annotations

import inspect

import pytest

from agent_runtime import models, observability


def test_the_proof_record_and_its_type_enum_are_gone():
    assert not hasattr(models, "Proof")
    assert not hasattr(models, "ProofType")


def test_importing_proof_from_the_package_root_fails():
    import agent_runtime

    assert not hasattr(agent_runtime, "Proof")
    assert "Proof" not in agent_runtime.__all__
    with pytest.raises(ImportError):
        from agent_runtime.models import Proof  # noqa: F401


def test_observability_no_longer_annotates_against_the_removed_record():
    assert not hasattr(observability, "Proof")
    assert "list[Proof]" not in inspect.getsource(observability)


def test_the_observability_proofs_parameter_and_row_are_gone():
    """S28 retarget: this asserted the parameter and its ``proofs_total`` row
    were UNCHANGED, because S27 could only retype the annotation while
    ``_cmd_observe`` still passed the ``proofs=[]`` keyword from another lane.
    S28 owns both sides now, so the constant row goes with the record."""

    parameters = inspect.signature(observability.build_observability).parameters
    assert "proofs" not in parameters

    envelope = observability.build_observability(runs=[], incidents=[])
    assert "proofs_total" not in envelope["signals"]


def test_the_lookalike_keep_set_survives():
    """``proof`` has a keep-side meaning (doc 16 Hazards): the proof-ref lines
    the operator reads, and the read-model accounting named like the removal."""

    from agent_runtime import continuity, parity, patch_coverage

    assert callable(parity.ProjectionAccountant)
    assert patch_coverage is not None
    message = continuity._format_parent_message(
        "personainst_dev", "summary", proof_ids=["proof_a"], artifact_refs=[]
    )
    assert "Proof refs: proof_a" in message
