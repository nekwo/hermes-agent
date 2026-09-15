"""Target-ambiguity policy for agent-chat / mission-chat targeting.

Single authority for the ``ambiguous_target`` refusal. A BARE persona id
(``qa``) names a *persona*, not a specific instance. When that persona runs
more than one live instance on the level, the omitted-session default resolver
silently threads the message onto the persona's canonical *primary* instance
and DROPS it for every sibling — no error, no warning, no accounting (live
2026-07-19: two ``qa`` instances, a bare-``qa`` relay landed only in
``personainst_qa``'s session; ``personainst_qa_agent_2`` received nothing).

This module decides, at the canonical persona chokepoint in the mission-chat
handler (AFTER persona-id canonicalization), whether that silent resolution is
ambiguous. The handler supplies the CANDIDATE list (the persona's live
instances, read from the instance store) and a ``caller_pinned_instance`` flag
(True when the call already names a specific instance by any unambiguous means
— a ``personainst_*`` target, an explicit ``persona_instance_id``, or a
caller-chosen session id). An already-disambiguated call is never refused, so
the operator console — which always carries an instance-bearing session id —
keeps reaching both siblings.

Sibling of :mod:`agent_runtime.relay_policy`: relay_policy owns the relay
CHAIN dimension (depth / cycle / budget); this owns the TARGET-instance
dimension. Both are evaluated at the same handler chokepoint and both emit the
same typed-refusal envelope carrying ``relay_chain`` for provenance parity.

Pure and stdlib-only: no harness imports, no I/O — the decision table is
unit-testable in isolation. ``candidates`` are plain value objects, not store
rows, so the policy never touches the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass

AMBIGUOUS_TARGET = "ambiguous_target"


@dataclass(frozen=True)
class TargetCandidate:
    """One live persona instance a bare-persona send could plausibly mean.

    ``instance_id`` is the canonical ``personainst_*`` handle (the exact address
    the caller retries with); ``display_name`` is what the operator/roster shows
    (e.g. "QA Agent" / "QA Agent (2)")."""

    instance_id: str
    display_name: str

    def as_dict(self) -> dict[str, str]:
        return {"persona_instance_id": self.instance_id, "display_name": self.display_name}


@dataclass(frozen=True)
class TargetDecision:
    allowed: bool
    error_kind: str | None = None  # ambiguous_target
    reason: str | None = None
    persona_id: str | None = None
    candidates: tuple[TargetCandidate, ...] = ()
    chain: tuple[str, ...] = ()


def _format_candidates(candidates: tuple[TargetCandidate, ...]) -> str:
    return ", ".join(f"{c.display_name} (@{c.instance_id})" for c in candidates)


def evaluate_target(
    *,
    persona_id: str,
    candidates,
    caller_pinned_instance: bool,
    is_profile_target: bool = False,
    relay_chain=(),
) -> TargetDecision:
    """Decide whether a bare-persona-id chat target is unambiguous.

    Refuse ``ambiguous_target`` ONLY when the target is a bare persona id, the
    caller pinned no specific instance, the persona is not a ``profile:<name>``
    (out of scope by contract), and MORE THAN ONE live instance exists. Every
    other case — a pinned instance, a profile target, or a single live instance
    (the common case) — resolves as before.

    ``candidates`` is the sequence of live :class:`TargetCandidate`s for
    ``persona_id``. ``relay_chain`` is echoed into the decision so the refusal
    envelope carries the same ``relay_chain`` provenance the relay refusals do.
    """
    chain = tuple(relay_chain or ())
    persona = str(persona_id or "").strip()
    live = tuple(candidates)

    # A caller that already named a specific instance, or a profile target, is
    # unambiguous by construction — never refuse. This is what keeps the operator
    # console (instance-bearing session id) and @personainst_* targets working.
    if caller_pinned_instance or is_profile_target:
        return TargetDecision(allowed=True, persona_id=persona, chain=chain)

    # Zero or one live instance: the bare persona id resolves to exactly one
    # target, exactly as today. THE common case must not regress.
    if len(live) <= 1:
        return TargetDecision(allowed=True, persona_id=persona, chain=chain)

    return TargetDecision(
        allowed=False,
        error_kind=AMBIGUOUS_TARGET,
        reason=(
            f"'{persona}' runs {len(live)} live instances on the level, so a bare persona id is "
            f"ambiguous — the message would silently thread onto only the primary instance and be "
            f"dropped for the others. Re-send to the specific instance by its @handle: "
            f"{_format_candidates(live)}."
        ),
        persona_id=persona,
        candidates=live,
        chain=chain,
    )
