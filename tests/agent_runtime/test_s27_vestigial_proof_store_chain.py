"""S27 removes the ``proof_store`` parameter chain.

``ProofStore`` was deleted in S6 with the proof-gate machinery. The *parameter*
survived across five signatures because it spanned three modules and every hop
threaded it through without ever reading it:

    snapshot.build_snapshot -> _build_snapshot_uncoalesced
        -> prompt_observability.snapshot_prompt_observability
            -> runtime_hud.resolve_situational_hud            (never read)
Zero production call sites passed anything but ``None``; the terminal consumer
had no body reference at all. A parameter that only ever carries ``None`` into a
function that never reads it is a signature lying about what the lane needs.

``build_snapshot``'s copy went with the rest: it was inert except for widening
the ``custom_stores`` coalescing predicate, and nothing passed it — so the
predicate's behaviour is unchanged for every real caller.
"""

from __future__ import annotations

import inspect

from agent_runtime import prompt_observability, runtime_hud, snapshot


PROOF_STORE_FREE_SIGNATURES = (
    (snapshot, "build_snapshot"),
    (snapshot, "_build_snapshot_uncoalesced"),
    (prompt_observability, "snapshot_prompt_observability"),
    (runtime_hud, "resolve_situational_hud"),
    (runtime_hud, "situational_hud_for_instance"),
)


def test_no_signature_in_the_chain_still_declares_proof_store():
    offenders = [
        f"{module.__name__}.{name}"
        for module, name in PROOF_STORE_FREE_SIGNATURES
        if "proof_store" in inspect.signature(getattr(module, name)).parameters
    ]
    assert offenders == []


def test_no_module_in_the_chain_still_mentions_proof_store():
    """The passer at the snapshot -> prompt_observability hop went in the same
    commit as the signatures, so no half-removed keyword can survive."""

    offenders = [
        module.__name__
        for module in (snapshot, prompt_observability, runtime_hud)
        if "proof_store" in inspect.getsource(module)
    ]
    assert offenders == []


def test_the_chain_still_works_end_to_end(isolate_agent_runtime_root):
    """Negative gate: the HUD wrapper and the snapshot still build. Dropping an
    inert parameter must not change a single observable output."""

    from types import SimpleNamespace

    instance = SimpleNamespace(
        id="personainst_neko",
        persona_id="neko_supervisor",
        role="supervisor",
        display_name="Neko Mission Lead",
        goal_id=None,
        current_task_id=None,
        state="idle",
        mode="configured",
    )
    hud = runtime_hud.resolve_situational_hud(instance, roster=[instance])
    assert isinstance(hud, dict) and hud
    assert snapshot.build_snapshot()["parity"]["contract_version"] == 51
