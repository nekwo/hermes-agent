from __future__ import annotations

"""The runtime context shape one persona turn is described by.

S27 removed the tick-context BUILDER (``build_context`` / ``render_context`` and
their ~39 private helpers): S5 deleted the dispatch loop that called them, so
their only surviving liveness was the import line in ``persona_runtime``.

What remains is the SHAPE. ``persona_runtime`` still annotates its repo-grounding
and tool-budget helpers against ``AgentContext``. With the builder gone nothing
produces one, so those helpers are a second-order orphan — recorded as follow-up
debt, not silently kept alive by a producer that no longer exists.
"""

from dataclasses import dataclass, field
from typing import Any

from .models import AgentRun

#: The ``Task`` record was deleted with the mission lane (S8), so there is no
#: type left to name here. ``AgentContext`` holds a duck-typed, task-shaped
#: object read exclusively through ``getattr``/``.id``-style access; ``TaskLike``
#: states that honestly instead of annotating a name that resolves to nothing
#: (``from __future__ import annotations`` hid the NameError, but
#: ``typing.get_type_hints`` on these signatures would still raise).
TaskLike = Any


@dataclass(slots=True)
class AgentContext:
    task: TaskLike
    run: AgentRun
    current_stage: object | None = None
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    proof_ids: list[str] = field(default_factory=list)
    requires_repair: bool = False
    repair_error: str | None = None
    context_bundles: list[dict[str, Any]] = field(default_factory=list)
    proof_records: list[dict[str, Any]] = field(default_factory=list)
    incident_records: list[dict[str, Any]] = field(default_factory=list)
    repo_context: dict[str, Any] | None = None
    mission_hud: dict[str, Any] | None = None
    autonomy_packet: dict[str, Any] | None = None
    latest_handoff_packet: dict[str, Any] | None = None
    latest_delivery: dict[str, Any] | None = None
    latest_qa_review: dict[str, Any] | None = None


