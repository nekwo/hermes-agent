"""S19 retires the dead goal/task HUD cluster inside ``context_builder``.

Every symbol named here lost its callers when the mission lane came out
(S4-S12) and the orphan modules followed (S13-S18): the decision menus, the
shape index, the "next required move" role table, the simplified agent HUD and
its evidence/verification readers, the stage/visual predicates, and the
``Task``-declared delivery-directive HUD line. They could only be reached from
each other, so they are removed as a cluster rather than refactored.

Three hollow rings go with them:

* ``mission_hud_preview`` — its two HUD entry points
  (``prompt_observability.snapshot_prompt_observability`` and
  ``runtime_hud.resolve_situational_hud``) can only ever pass ``task=None``
  (``snapshot.py`` builds with ``tasks = []``; the chat lane resolves through
  the permanent ``TaskStoreStub``, whose ``.get()`` always raises ``NotFound``).
  The ``mission_hud`` HUD field row goes with the producer.
* ``_delivery_directive_line`` — residue per the liveness ruling in
  ``docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/delivery-directive.md``: with no ``Task`` to
  declare one, it always rendered ``DEFAULT_DELIVERY_DIRECTIVE``.
* ``_RECENT_CONTEXT_EVENT_TYPES`` — seven of its eight rows name event types
  S15 de-registered, so no surviving code can emit them.

The keep-side name that survives is the live chat-lane HUD: it must keep
rendering steering edges.

**Corrected by S27.** This stage also kept a "tick-context builder keep set"
(``_mission_hud`` / ``_stage_records`` / ``_safe_packet_projection`` /
``_recent_relevant_events`` / ``_skill_reference_for_action``) on the premise
that they sat on a live render path. They did not: ``build_context`` /
``render_context`` lost their only caller in S5, so the whole path was
test-reachable only. S27 removed the lane.

**Superseded by S29.** ``agent_runtime.context_builder`` no longer exists. S27
kept ``AgentContext`` because ``persona_runtime`` annotated eight helpers with
it; S29 removed those helpers (they had no producer and no caller), which left
the dataclass with neither, so the module went whole. Five gates here asserted
"name X is not an attribute of that module" — a deleted module satisfies all of
them vacuously, so they are removed rather than left as decoration. The module
retirement is now contracted in one place:
``tests/agent_runtime/test_s29_agent_context_shape_retirement.py``. What stays
below is the half that was never about ``context_builder``'s own contents: the
``mission_hud_preview`` ENTRY POINTS in ``prompt_observability`` / ``runtime_hud``
and the live chat-lane HUD.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_runtime.runtime_hud import render_situational_hud_block, resolve_situational_hud


def _instance(**overrides) -> SimpleNamespace:
    base = dict(
        id="personainst_neko",
        persona_id="neko_supervisor",
        role="supervisor",
        display_name="Neko Mission Lead",
        goal_id=None,
        current_task_id=None,
        state="idle",
        mode="configured",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── the removal ─────────────────────────────────────────────────────────────




# ── the keep set ────────────────────────────────────────────────────────────


def test_the_live_chat_lane_hud_still_renders_steering_edges():
    """The single HUD authority. Removing the mission slice must not touch the
    steering edges an ordinary chat turn renders."""

    lead = _instance(id="personainst_neko", display_name="Neko Mission Lead")
    child = _instance(id="personainst_dev", display_name="Dev", steered_by=["personainst_neko"])

    child_block = render_situational_hud_block(resolve_situational_hud(child, roster=[lead, child]))
    assert "- Steered by: Neko Mission Lead (@personainst_neko)" in child_block

    lead_block = render_situational_hud_block(resolve_situational_hud(lead, roster=[lead, child]))
    assert "- Steers: Dev (@personainst_dev)" in lead_block
