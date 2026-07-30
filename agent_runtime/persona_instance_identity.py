"""Persona-instance identity aliases + store reconciliation.

The persona-instance store historically persisted the SAME logical operator
channel under several id-derivation schemes (live evidence 2026-07-10:
``personainst_<persona>``, ``persona_personainst_<persona>``,
``personainst_operator_<hash>``, ``personainst_profile_<name>``; the
neko_supervisor channel had three rows, profile:alice two). Consumers then
grew per-surface dedup heuristics.

This module retires the drift at the source:

- :func:`identity_aliases_for_rows` — the legacy-id -> canonical-id map
  emitted as ``identity_map`` on the snapshot and stream (registry file plus
  structurally derivable aliases for rows still live).
- :func:`reconcile_persona_instances` — the one-shot store repair: archives
  (never deletes) legacy rows onto their canonical channel, records aliases
  durably, emits ``persona_instance.reconciled`` events, and is idempotent. Its
  last phase reaches one store further: a runtime flow graph is keyed on its
  owner instance's id, so reaping a row leaves an owner-less canvas behind, and
  the reconciler archives those too (``flow_graph.pruned``).

Creation-path canonicalization lives in
:func:`agent_runtime.persona_assignments.canonical_persona_instance_id` —
new drifted rows can no longer be minted; this module cleans up the rows
minted before that chokepoint existed.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from typing import Any

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .events import EventLog
from .flow_graph import (
    GRAPH_HELD_REASON_OWNER_ALIASED,
    FlowGraphStore,
    bound_agent_ids_of_stored,
    classify_graph_owner_liveness,
    reconcile_departed_agents,
)
from .models import Event, PersonaInstance
from .personas import MOTHBALLED_PERSONA_IDS, MOTHBALLED_ROLE_TOKENS
from .persona_assignments import (
    PersonaInstanceStore,
    canonical_persona_instance_id,
    persona_instance_id_for,
    safe_assignment_token,
)
from .serde import from_jsonable, to_jsonable

# The retired operator-channel id scheme: personainst_operator_<hex>. Rows in
# this scheme are the persona's operator channel persisted under a session
# hash; they fold onto persona_instance_id_for(persona_id).
_LEGACY_OPERATOR_PREFIX = "personainst_operator_"

# Modes that represent a conversational channel (vs a task-bound worker
# projection, which is legitimately distinct per task and never folded).
_CONVERSATIONAL_MODES = frozenset({"chat", "free_floating", "configured", ""})

# Orphan-prune reasons (typed, single-sourced — no boolean soup). A row is prunable
# only when it is orphan-shaped AND carries none of the protections below.
PRUNE_REASON_NO_PROFILE = "orphan-no-profile"
PRUNE_REASON_LEGACY_ROLE = "legacy-role"
# Held = orphan-shaped but protected from prune; surfaced for accounting, never reaped.
HELD_REASON_ACTIVE = "active-binding"
HELD_REASON_TASK_BOUND = "task-bound"
HELD_REASON_HEARTBEAT = "fresh-heartbeat"
HELD_REASON_RECENT = "recently-updated"
HELD_REASON_LEGACY_SEEDED = "legacy-role-still-seeded"

# Liveness grace windows. A genuine orphan tombstone is weeks stale; these only protect
# rows that are actively being written (races) from being reaped mid-flight.
_HEARTBEAT_FRESH_SECONDS = 24 * 3600
_UPDATED_MIN_AGE_SECONDS = 48 * 3600


def load_persona_instance_aliases() -> dict[str, str]:
    path = paths.persona_instance_aliases_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    aliases: dict[str, str] = {}
    for key, value in raw.items():
        alias = safe_assignment_token(key)
        canonical = safe_assignment_token(value)
        if alias and canonical and alias != canonical:
            aliases[alias] = canonical
    return aliases


def _save_persona_instance_aliases(aliases: dict[str, str]) -> None:
    atomic_json_write(
        paths.persona_instance_aliases_path(),
        dict(sorted(aliases.items())),
        indent=2,
        sort_keys=True,
    )


def _canonical_for_row(instance_id: str, persona_id: str | None, mode: str | None) -> str:
    """Canonical id for a persisted row, including the legacy operator scheme.

    Extends the structural :func:`canonical_persona_instance_id` with the
    store-level rule that a ``personainst_operator_<hash>`` row in a
    conversational mode IS the persona's operator channel.
    """
    canonical = canonical_persona_instance_id(instance_id, persona_id=persona_id) or instance_id
    if (
        canonical == instance_id
        and instance_id.startswith(_LEGACY_OPERATOR_PREFIX)
        and persona_id
        and (mode or "").lower() in _CONVERSATIONAL_MODES
    ):
        return persona_instance_id_for(persona_id)
    return canonical


def identity_aliases_for_rows(rows: list[dict[str, Any]] | None) -> dict[str, str]:
    """The ``identity_map`` for a snapshot: durable registry aliases plus the
    structurally derivable aliases of rows still live in this snapshot."""
    aliases = load_persona_instance_aliases()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        instance_id = str(row.get("persona_instance_id") or row.get("id") or "").strip()
        if not instance_id:
            continue
        persona_id = str(row.get("persona_id") or "").strip() or None
        mode = str(row.get("mode") or "").strip() or None
        canonical = _canonical_for_row(instance_id, persona_id, mode)
        if canonical != instance_id:
            aliases.setdefault(instance_id, canonical)
    return aliases


def duplicate_persona_instance_groups(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Groups of live rows that alias to one canonical id — each group is a
    duplicate-agent-cards bug in waiting and a pending reconciler run."""
    by_canonical: dict[str, list[str]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        instance_id = str(row.get("persona_instance_id") or row.get("id") or "").strip()
        if not instance_id:
            continue
        persona_id = str(row.get("persona_id") or "").strip() or None
        mode = str(row.get("mode") or "").strip() or None
        canonical = _canonical_for_row(instance_id, persona_id, mode)
        by_canonical.setdefault(canonical, []).append(instance_id)
    return [
        {"canonical_id": canonical, "instance_ids": sorted(ids)}
        for canonical, ids in sorted(by_canonical.items())
        if len(ids) > 1
    ]


def _extract_field(item: Any, *keys: str) -> str | None:
    for key in keys:
        value = item.get(key) if isinstance(item, dict) else getattr(item, key, None)
        text = str(value or "").strip()
        if text:
            return text
    return None


def backed_persona_identity(
    agents: Any = None,
    profile_names: Any = None,
) -> tuple[set[str], set[str]]:
    """The persona-id / profile-name universe that a persona instance can legitimately
    back onto: the persisted agent store and live profile templates. Single-sourced
    so the reconcile and snapshot
    lanes classify orphans identically. ``agents`` accepts ``AgentPersona`` objects or
    snapshot agent-summary dicts; ``profile_names`` is the profile-template name list."""
    persona_ids: set[str] = set()
    profile_set: set[str] = set()
    if agents is None:
        try:
            from .store import AgentStore

            agents = AgentStore().list_all()
        except Exception:
            agents = []
    for agent in agents or []:
        pid = _extract_field(agent, "id", "persona_id")
        if pid:
            persona_ids.add(pid)
        hp = _extract_field(agent, "hermes_profile")
        if hp:
            profile_set.add(hp)
    for name in profile_names or []:
        text = str(name or "").strip()
        if text:
            profile_set.add(text)
    return persona_ids, profile_set


def _row_is_backed(
    persona_id: str,
    profile_id: str | None,
    backed_persona_ids: set[str],
    backed_profile_names: set[str],
) -> bool:
    pid = (persona_id or "").strip()
    if pid and pid in backed_persona_ids:
        return True
    if pid.startswith("profile:"):
        name = pid.split(":", 1)[1].strip()
        if name and name in backed_profile_names:
            return True
    prof = (profile_id or "").strip()
    if prof and prof in backed_profile_names:
        return True
    return False


def _within(current: datetime, ts: datetime | None, seconds: float) -> bool:
    if ts is None:
        return False
    delta = (_recency(current) - _recency(ts)).total_seconds()
    return 0 <= delta < seconds


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _isoformat(value: Any) -> str | None:
    dt = _as_datetime(value)
    return dt.isoformat() if dt is not None else None


def _row_get(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def classify_orphan_persona_instances(
    rows: list[dict[str, Any]] | None,
    *,
    backed_persona_ids: Any,
    backed_profile_names: Any,
    now: datetime | None = None,
    profile_catalog_authoritative: bool = True,
    heartbeat_fresh_seconds: float = _HEARTBEAT_FRESH_SECONDS,
    updated_min_age_seconds: float = _UPDATED_MIN_AGE_SECONDS,
) -> dict[str, list[dict[str, Any]]]:
    """Pure classifier: split orphan-shaped persona-instance rows into ``prunable`` and
    ``held``, each entry carrying a typed ``reason``.

    A row is an *orphan candidate* when its backing persona/profile is absent from the
    backed universe (``orphan-no-profile``) OR its role/persona is mothballed
    (``legacy-role``). A real product agent (backed and not mothballed) is skipped
    entirely. A candidate is HELD (never pruned) when it shows any live activity, is
    task-bound (owned by the task-bound sweep), has a fresh heartbeat, was updated inside
    the min-age grace, or is a still-seeded mothballed persona; otherwise it is prunable.

    ``profile_catalog_authoritative`` guards the ``profile:<name>`` lane: when the profile
    template catalog could not be positively enumerated (empty/failed read), a missing
    template is indistinguishable from an unreadable catalog, so ``profile:*`` rows are
    NEVER classified as ``orphan-no-profile`` — a blind catalog must not reap real
    profile channels. ``legacy-role`` and plain-id resolution are unaffected.
    """
    from hermes_time import now as _clock

    current = now or _clock()
    backed_ids = set(backed_persona_ids or ())
    backed_profiles = set(backed_profile_names or ())
    prunable: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        instance_id = str(_row_get(row, "persona_instance_id", "id") or "").strip()
        if not instance_id:
            continue
        persona_id = str(_row_get(row, "persona_id") or "").strip()
        role = str(_row_get(row, "role") or "").strip()
        profile_id = str(_row_get(row, "profile_id", "source_profile_id", "backing_profile") or "").strip() or None
        mode = str(_row_get(row, "mode", "lifecycle_mode") or "").strip().lower()

        is_backed = _row_is_backed(persona_id, profile_id, backed_ids, backed_profiles)
        if not is_backed and persona_id.startswith("profile:") and not profile_catalog_authoritative:
            # Blind catalog: can't confirm the template is truly gone — never reap it.
            is_backed = True
        is_mothballed = role in MOTHBALLED_ROLE_TOKENS or persona_id in MOTHBALLED_PERSONA_IDS
        if is_backed and not is_mothballed:
            continue  # real product agent — not an orphan, not even held

        entry = {
            "persona_instance_id": instance_id,
            "persona_id": persona_id or None,
            "role": role or None,
            "profile_id": profile_id,
            "updated_at": _isoformat(_row_get(row, "updated_at")),
        }

        active = any(
            str(_row_get(row, key) or "").strip()
            for key in (
                "active_worker_session_id",
                "active_run_id",
                "current_assignment_id",
                "current_work_assignment_id",
                "current_task_id",
                "attached_task_id",
            )
        )
        if active:
            held_reason: str | None = HELD_REASON_ACTIVE
        elif mode == "task_bound":
            held_reason = HELD_REASON_TASK_BOUND
        elif _within(current, _as_datetime(_row_get(row, "last_heartbeat_at")), heartbeat_fresh_seconds):
            held_reason = HELD_REASON_HEARTBEAT
        elif _within(current, _as_datetime(_row_get(row, "updated_at")), updated_min_age_seconds):
            held_reason = HELD_REASON_RECENT
        elif is_mothballed and persona_id in backed_ids:
            held_reason = HELD_REASON_LEGACY_SEEDED
        else:
            held_reason = None

        if held_reason is not None:
            held.append({**entry, "reason": held_reason})
        else:
            entry["reason"] = PRUNE_REASON_LEGACY_ROLE if is_mothballed else PRUNE_REASON_NO_PROFILE
            prunable.append(entry)

    return {"prunable": prunable, "held": held}


def _profile_template_names() -> list[str]:
    try:
        from hermes_cli.profiles import available_profile_templates

        return [str(getattr(t, "name", "") or "").strip() for t in available_profile_templates()]
    except Exception:
        return []


def reconcile_persona_instances(*, apply: bool = True, event_log: EventLog | None = None) -> dict[str, Any]:
    """Collapse legacy-id persona-instance rows onto their canonical channel.

    For every live row whose id is not canonical: when the canonical row also
    exists, the newer conversational surface (session/display/profile) is
    kept on the canonical row and the legacy row is archived; when it does
    not, the row is rewritten under its canonical id. Legacy files are moved
    to ``persona_instances_archive/<ts>/`` — never deleted — and every
    action emits ``persona_instance.reconciled``. Aliases are recorded in the
    durable registry regardless, so the ``identity_map`` keeps resolving old
    ids in archived history. Running twice is a no-op the second time.

    Five phases run in order: (1) legacy-id fold, (2) orphan / legacy-role
    prune, (3) missing steering-parent repair, (4) missing chat-session-binding
    repair, (5) owner-less flow-graph prune. ``apply=False`` (the CLI's
    ``--dry-run``) reports every phase and writes nothing — no store rows, no
    graph docs, no events.

    Phase 5 is LAST by referential ordering, not by convenience. Phases 1-2 are
    the only phases that add or remove rows and phases 3-4 only repair pointers
    inside surviving rows, so by phase 5 the live-instance set is final and the
    "does this graph's owner still exist?" question is asked exactly once
    against it. Running after phase 3 also makes phase 5's departure settlement
    idempotent instead of competing with it: phase 3's liveness repair has
    already dropped a departed owner from every child's ``steered_by``, so the
    graph-scoped pass reports ``changed: False`` rather than racing the same
    write from a second authority.
    """
    store = PersonaInstanceStore(event_log=event_log)
    instances = store.list_all()
    by_id = {instance.id: instance for instance in instances}
    aliases = load_persona_instance_aliases()
    actions: list[dict[str, Any]] = []
    archive_dir = paths.persona_instances_archive_dir() / now().strftime("%Y%m%dT%H%M%SZ_reconcile")

    for instance in instances:
        canonical = _canonical_for_row(instance.id, instance.persona_id, instance.mode)
        if canonical == instance.id:
            continue
        target = by_id.get(canonical)
        if target is not None and target.persona_id != instance.persona_id:
            actions.append(
                {
                    "from_id": instance.id,
                    "to_id": canonical,
                    "action": "skipped_persona_conflict",
                    "detail": f"canonical row belongs to {target.persona_id}, legacy row to {instance.persona_id}",
                }
            )
            continue
        action = "merged" if target is not None else "renamed"
        actions.append({"from_id": instance.id, "to_id": canonical, "action": action})
        if not apply:
            continue
        if target is None:
            renamed = from_jsonable(PersonaInstance, {**to_jsonable(instance), "id": canonical})
            store.update(renamed)
            by_id[canonical] = store.get(canonical)
        else:
            legacy_newer = _recency(instance.updated_at) > _recency(target.updated_at)
            legacy_conversational = (instance.mode or "").lower() in _CONVERSATIONAL_MODES
            target_conversational = (target.mode or "").lower() in _CONVERSATIONAL_MODES
            if legacy_newer and legacy_conversational and target_conversational:
                target.session_id = instance.session_id or target.session_id
                target.display_name = instance.display_name or target.display_name
                target.profile_id = instance.profile_id or target.profile_id
                if (instance.mode or "").lower() in {"chat", "free_floating"}:
                    target.mode = instance.mode
                store.update(target)
                by_id[canonical] = store.get(canonical)
        _archive_row(instance.id, archive_dir)
        by_id.pop(instance.id, None)
        aliases[instance.id] = canonical
        store._event(  # noqa: SLF001 — the reconciler IS store maintenance
            "persona_instance.reconciled",
            instance,
            {"from_id": instance.id, "to_id": canonical, "action": action},
        )

    if apply and any(item["action"] in {"merged", "renamed"} for item in actions):
        _save_persona_instance_aliases(aliases)

    # Phase 2 — orphan / legacy-role prune. Rows whose backing persona/profile is absent
    # (or a mothballed role) project as phantom "on level" agents. Archive them (never
    # delete), emit a typed ``persona_instance.pruned`` event, and account the held ones.
    # Runs over the rows that survived the duplicate fold; live activity is always
    # protected, re-verified against the cross-store binding check as a belt.
    surviving = store.list_all()
    surviving_by_id = {instance.id: instance for instance in surviving}
    template_names = _profile_template_names()
    backed_persona_ids, backed_profile_names = backed_persona_identity(profile_names=template_names)
    classified = classify_orphan_persona_instances(
        [to_jsonable(instance) for instance in surviving],
        backed_persona_ids=backed_persona_ids,
        backed_profile_names=backed_profile_names,
        profile_catalog_authoritative=bool(template_names),
    )
    held_actions = list(classified["held"])
    pruned_actions: list[dict[str, Any]] = []
    prune_archive_dir = paths.persona_instances_archive_dir() / now().strftime("%Y%m%dT%H%M%SZ_prune")
    for candidate in classified["prunable"]:
        instance = surviving_by_id.get(candidate["persona_instance_id"])
        if instance is None:
            continue
        if store._has_live_binding(instance):  # noqa: SLF001 — reconciler IS store maintenance
            held_actions.append({**candidate, "reason": HELD_REASON_ACTIVE})
            continue
        pruned_actions.append(dict(candidate))
        if not apply:
            continue
        _archive_row(instance.id, prune_archive_dir)
        by_id.pop(instance.id, None)
        store._event(  # noqa: SLF001 — the reconciler IS store maintenance
            "persona_instance.pruned",
            instance,
            {
                "reason": candidate["reason"],
                "role": instance.role or None,
                "profile_id": instance.profile_id,
                "updated_at": candidate.get("updated_at"),
            },
        )

    # Phase 3 — referential integrity. Shape-valid rows can still point at an
    # owner that was retired, reaped, or manually removed. Repair those
    # foreign-key misses after the archive phases so the next snapshot cannot
    # retain a random missing owner.
    steering_repairs = store.repair_missing_steering_references(apply=apply)

    # Phase 4 — chat-session referential integrity. Same class, different
    # pointer: a row can still name a chat session SessionDB no longer has
    # (deleted through a path that does not own the instance store, or scrubbed).
    # The snapshot projection is READ-ONLY, so it can only hide the row and
    # account a permanent ``session_not_in_db`` parity drop — one amber unit per
    # orphan, forever. This is the write-path repair that retires them.
    session_binding_repairs = store.repair_missing_chat_session_bindings(apply=apply)

    # Phase 5 — flow-graph referential integrity. A runtime flow graph IS one
    # instance's blueprint (``runtime:<owner>``), so a graph whose owner no
    # longer resolves is an operator canvas addressed to an agent this
    # reconciler just archived — the launcher still opens it and every consumer
    # that reads it re-materializes a departed agent. Same contract as phase 2:
    # archive (never delete), typed event, held/pruned accounting, dry-run
    # writes nothing. The live-instance set is derived (not re-read) so the
    # dry-run preview matches what an applied run would do: rows phase 1 folds
    # away and rows phase 2 prunes are already discounted, which on ``apply``
    # they are anyway.
    live_instance_ids = {instance.id for instance in surviving}
    for item in actions:
        if item["action"] in {"merged", "renamed"}:
            live_instance_ids.discard(item["from_id"])
            live_instance_ids.add(item["to_id"])
    live_instance_ids -= {item["persona_instance_id"] for item in pruned_actions}
    graph_prune = _prune_owner_less_flow_graphs(
        store=store,
        live_instance_ids=live_instance_ids,
        apply=apply,
    )

    return {
        "applied": bool(apply),
        "actions": actions,
        "merged_count": sum(1 for item in actions if item["action"] == "merged"),
        "renamed_count": sum(1 for item in actions if item["action"] == "renamed"),
        "skipped_count": sum(1 for item in actions if item["action"].startswith("skipped")),
        "pruned": pruned_actions,
        "held": held_actions,
        "pruned_count": len(pruned_actions),
        "held_count": len(held_actions),
        "steering_repairs": steering_repairs["repaired"],
        "steering_repaired_count": steering_repairs["repaired_count"],
        "session_binding_repairs": session_binding_repairs["repaired"],
        "session_binding_repaired_count": session_binding_repairs["repaired_count"],
        "session_binding_held": session_binding_repairs.get("held") or [],
        "session_binding_skipped": session_binding_repairs.get("skipped"),
        "graphs_pruned": graph_prune["pruned"],
        "graphs_held": graph_prune["held"],
        "graphs_pruned_count": len(graph_prune["pruned"]),
        "graphs_held_count": len(graph_prune["held"]),
        "graph_departed_steering": graph_prune["departed_steering"],
        "graph_departed_steering_count": sum(
            1 for item in graph_prune["departed_steering"] if item.get("changed")
        ),
        "alias_count": len(aliases),
        "archive_dir": str(archive_dir) if apply and actions else None,
        "prune_archive_dir": str(prune_archive_dir) if apply and pruned_actions else None,
        "graph_prune_archive_dir": graph_prune["archive_dir"],
        "remaining_instance_ids": sorted(by_id.keys()),
    }


def _instance_resolves(store: PersonaInstanceStore, instance_id: str) -> bool:
    """Whether the store can still reach a row for [instance_id] — the same
    try/get idiom the flow-graph ingest uses for an unknown reference. Broader
    than literal id membership on purpose: ``get`` also resolves the actor-token
    / legacy-alias drift this module records, and the graph reap's keep side
    must be the forgiving one."""

    try:
        store.get(instance_id)
    except Exception:
        return False
    return True


def _prune_owner_less_flow_graphs(
    *,
    store: PersonaInstanceStore,
    live_instance_ids: set[str],
    apply: bool,
) -> dict[str, Any]:
    """Phase 5 body: archive every stored flow graph whose OWNER instance no
    longer resolves, and settle the steering the reaped map asserted.

    Owner liveness is the whole rule — never emptiness. The launcher creates a
    single self-node, zero-edge canvas on demand (``requested_by: launcher``)
    the moment an operator opens an agent's graph; that doc is intended, and a
    "looks empty" heuristic would delete a live agent's canvas between the open
    and the first drawn edge. So a graph whose owner resolves is held however
    empty, and a graph whose owner does not is reaped however richly drawn.

    Departure settlement: a reaped map's owner is gone, so the children it drew
    keep an inbound edge from a departed parent. Those are handed to the
    owner-scoped [reconcile_departed_agents], which strips ONLY that owner and
    preserves every other parent. Phase 3's liveness repair normally got there
    first (a departed owner is not a live parent), so these entries usually
    report ``changed: False`` — that is the phases agreeing, and the entries are
    accounted rather than dropped so a disagreement would be visible.
    """

    graph_store = FlowGraphStore()
    classified = classify_graph_owner_liveness(
        graph_store.list_ids(), live_instance_ids=live_instance_ids
    )
    held: list[dict[str, Any]] = list(classified["held"])
    pruned: list[dict[str, Any]] = []
    departed_steering: list[dict[str, Any]] = []
    archive_dir = graph_store.stale_dir() / now().strftime("%Y%m%dT%H%M%SZ_graph_prune")

    for candidate in classified["stale"]:
        owner = candidate["owner_instance_id"]
        if owner and _instance_resolves(store, owner):
            held.append({**candidate, "reason": GRAPH_HELD_REASON_OWNER_ALIASED})
            continue
        drawn = bound_agent_ids_of_stored(graph_store.get(candidate["graph_id"])) - {owner}
        entry = {**candidate, "drawn_agent_count": len(drawn)}
        pruned.append(entry)
        if not apply:
            continue
        if drawn:
            departed_steering.extend(
                {**item, "graph_id": candidate["graph_id"]}
                for item in reconcile_departed_agents(
                    departed=drawn, owner_id=owner, store=store
                )
            )
        archived = graph_store.archive(candidate["graph_id"], archive_dir)
        entry["archived_to"] = str(archived) if archived is not None else None
        store.event_log.append(
            Event(
                ts=now(),
                type="flow_graph.pruned",
                task_id=None,
                run_id=None,
                persona_id=None,
                payload={
                    "graph_id": candidate["graph_id"],
                    "owner_instance_id": owner,
                    "reason": candidate["reason"],
                    "drawn_agent_count": len(drawn),
                    "archived_to": entry["archived_to"],
                },
            )
        )

    return {
        "pruned": pruned,
        "held": held,
        "departed_steering": departed_steering,
        "archive_dir": str(archive_dir) if apply and pruned else None,
    }


def _archive_row(instance_id: str, archive_dir) -> None:
    source = paths.persona_instance_path(instance_id)
    if not source.exists():
        return
    archive_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(archive_dir / source.name))


def _recency(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


__all__ = [
    "backed_persona_identity",
    "classify_orphan_persona_instances",
    "duplicate_persona_instance_groups",
    "identity_aliases_for_rows",
    "load_persona_instance_aliases",
    "reconcile_persona_instances",
]
