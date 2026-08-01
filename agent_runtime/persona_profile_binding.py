"""Persona ⇄ Hermes-profile binding: the one door that moves it.

A persona's ``hermes_profile`` decides which Hermes profile home backs it —
which system prompt, soul overlay, profile memory, skills, credentials and MCP
servers the agent actually runs with. Until this module existed the binding had
**no CLI mutator at all**:

* ``harness persona set-model`` moves provider/model only.
* ``harness persona instance update-profile`` — despite the name — writes
  ``display_name`` / ``current_chat_goal`` / ``goal_id`` / ``skills`` and never
  touches ``profile_id``.
* Persona/profile bindings come only from persisted or configured persona data.
* ``personas.promote_profile_to_persona`` mints a NEW persona.

So the only way to rebind was to drive :meth:`AgentStore.save` from a Python
one-liner, which left three defects behind every time (live incident,
2026-07-25):

1. **Silent config/store divergence.** ``ensure_persisted_personas`` resolves
   ``{**catalog, **stored}`` — the STORE record wins wholesale. Editing
   ``config.yaml`` alone is inert and nothing said so.
   :func:`resolve_effective_binding` is the single statement of that rule and
   :func:`diverged_bindings` makes the disagreement visible.
2. **``persona_instance.profile_id`` drifts.** It is a PROJECTION of the
   persona's ``hermes_profile``, but only the canonical
   ``personainst_<persona_id>`` row self-heals (``ensure_for_persona``); the
   ``personainst_<persona>_agent_<hex>`` placement rows drift forever, and
   ``_profile_visibility_persona`` will happily mint a synthetic persona off a
   stale ``profile_id``. :func:`rebind_persona_profile` cascades every row in
   the same operation.
3. **No accounting.** The rebind emitted nothing typed, so no consumer could
   see it. :data:`REBIND_EVENT_TYPE` is registered in the
   ``decision_contract_registry`` event table (an unregistered type is rejected
   outright by ``EventLog.append``).

Rules this module holds:

* ``AgentStore.save`` stays the ONE persona write path — this module calls it,
  it does not replace it, and the existing ``persona.updated`` event still fires.
* A busy instance BLOCKS the whole operation. Rebinding under a live run/worker
  would swap the prompt+credentials out from under an in-flight turn.
* ``--dry-run`` validates everything, writes nothing and emits nothing
  (``_add_stage42_global_args(mutation=True)`` auto-registers ``--dry-run``, and
  a verb that does not READ it silently mutates on a preview — this repo has
  shipped that bug twice).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from hermes_time import now

from .errors import AgentRuntimeError
from .events import EventLog
from .models import AgentPersona, Event

#: Typed operation event. MUST stay registered in
#: ``decision_contract_registry._EVENT_CONTRACTS`` — ``EventLog.append`` raises
#: ``unknown event type`` for anything else, so an unregistered emit fails every
#: rebind.
#:
#: Deliberately the ONLY event the cascade emits (no per-instance
#: ``persona_instance.profile_updated``): that type is in
#: ``patch_coverage.COVERED_DOMAIN_EVENT_TYPES``, so a batch carrying it without
#: a paired ``state.patched`` would ship as a patch frame that folds nothing.
#: ``persona.profile_rebound`` is uncovered, so the batch honestly degrades to a
#: full-core rebuild, and this one event names every row the operation moved.
REBIND_EVENT_TYPE = "persona.profile_rebound"

#: Which side won the effective binding.
BINDING_SOURCE_STORE = "store"
BINDING_SOURCE_CONFIG = "config"
BINDING_SOURCE_UNBOUND = "unbound"

#: Typed busy reasons. Ordered most- to least-authoritative; the first match wins.
BUSY_LIVE_BINDING = "live_binding"
BUSY_ACTIVE_RUN = "active_run"
# S56 removed BUSY_ACTIVE_WORKER with the worker session store: its arm read
# ``active_worker_session_id``, a field no writer can set any more.
BUSY_ASSIGNMENT_IN_FLIGHT = "assignment_in_flight"
BUSY_TASK_BOUND = "task_bound"
BUSY_NON_IDLE_STATE = "non_idle_state"

#: Persona-instance states that are not "in flight". Everything else
#: (``assigned``/``running``/``waiting_on_*``/``self_healing``/``possessed``)
#: is a live lane; the terminal states (``completed``/``blocked``/``closed``)
#: are also idle for rebinding purposes — nothing is executing.
_IDLE_STATES = frozenset({"", "idle", "completed", "blocked", "closed"})

#: Terminal status of an apply. ``partially_applied`` = the persona authority
#: moved but at least one instance projection could not be written; those rows
#: are STRANDED (placement rows have no self-heal), so the operation must never
#: report a clean success.
STATUS_APPLIED = "applied"
STATUS_PARTIALLY_APPLIED = "partially_applied"

#: Event payloads are capped at 4KB (``EVENT_PAYLOAD_LIMIT_BYTES``). Bound the
#: per-instance detail lists and account the overflow rather than blowing the
#: cap. The failure list gets a smaller cap because each entry also carries a
#: reason string.
_EVENT_INSTANCE_CAP = 24
_EVENT_FAILED_CAP = 8


class PersonaProfileRebindError(AgentRuntimeError):
    """Typed refusal from :func:`rebind_persona_profile`.

    ``code`` is the machine-readable reason that rides the CLI error envelope
    verbatim; ``details`` carries operator-safe identity/context only (persona
    ids, profile names, instance ids) — never file content or credentials.
    """

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


# --------------------------------------------------------------------------- #
# Effective binding (pure)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class EffectiveBinding:
    """Which Hermes profile a persona is ACTUALLY bound to, and who decided."""

    persona_id: str
    config_profile: str | None
    store_profile: str | None
    config_declared: bool
    store_row_present: bool
    effective_profile: str | None
    source: str
    diverged: bool

    def as_row(self) -> dict[str, Any]:
        return {
            "persona_id": self.persona_id,
            "config_profile": self.config_profile,
            "store_profile": self.store_profile,
            "config_declared": self.config_declared,
            "store_row_present": self.store_row_present,
            "effective_profile": self.effective_profile,
            "binding_source": self.source,
            "binding_diverged": self.diverged,
        }


def resolve_effective_binding(
    persona_id: str,
    *,
    config_profile: str | None,
    store_profile: str | None,
    config_declared: bool,
    store_row_present: bool,
) -> EffectiveBinding:
    """PURE: resolve ``(config says X, store says Y) -> effective binding``.

    ``ensure_persisted_personas`` merges ``{**catalog, **stored}`` — a stored
    record wins **wholesale**, not per field. So a persisted row with an EMPTY
    ``hermes_profile`` still beats a config that names one (the persona then
    inherits the active Harness profile). That is why ``store_row_present`` is a
    separate input from ``store_profile``.

    ``diverged`` requires BOTH sides to have an opinion: a store-only persona
    (no config declaration) is not a disagreement, it is simply store-owned.
    """

    config_value = _clean(config_profile)
    store_value = _clean(store_profile)
    if store_row_present:
        return EffectiveBinding(
            persona_id=str(persona_id),
            config_profile=config_value,
            store_profile=store_value,
            config_declared=bool(config_declared),
            store_row_present=True,
            effective_profile=store_value,
            source=BINDING_SOURCE_STORE,
            diverged=bool(config_declared) and config_value != store_value,
        )
    return EffectiveBinding(
        persona_id=str(persona_id),
        config_profile=config_value,
        store_profile=None,
        config_declared=bool(config_declared),
        store_row_present=False,
        effective_profile=config_value,
        source=BINDING_SOURCE_CONFIG if config_value else BINDING_SOURCE_UNBOUND,
        diverged=False,
    )


def binding_index(cfg=None) -> dict[str, EffectiveBinding]:
    """Every persona id the runtime can resolve → its effective binding.

    Reads both sides once: the config catalog (``persona_records_from_config``,
    which is what ``config.yaml`` actually resolves to, supervisor head-profile
    override included) and the persisted agent store.

    Deliberately does NOT swallow a read failure: an empty index and a failed
    read are indistinguishable to a caller, and "no divergences" would then be a
    lie. Callers that can degrade (an ``agent list`` row) catch it and drop the
    diagnostic columns; the doctor catches it and reports ``ok: false``.
    """

    from .config import load_agent_runtime_config, persona_records_from_config
    from .store import AgentStore

    cfg = cfg or load_agent_runtime_config()
    config_rows = {persona.id: persona for persona in persona_records_from_config(cfg)}
    store_rows = {persona.id: persona for persona in AgentStore().list_all()}
    index: dict[str, EffectiveBinding] = {}
    for persona_id in sorted({*config_rows, *store_rows}):
        config_row = config_rows.get(persona_id)
        store_row = store_rows.get(persona_id)
        index[persona_id] = resolve_effective_binding(
            persona_id,
            config_profile=getattr(config_row, "hermes_profile", None),
            store_profile=getattr(store_row, "hermes_profile", None),
            config_declared=config_row is not None,
            store_row_present=store_row is not None,
        )
    return index


def diverged_bindings(cfg=None) -> list[EffectiveBinding]:
    """The config-vs-store disagreements, so they stop being silent.

    Deliberately does NOT repair either side: which one is right is operator
    judgment (``harness agent set-profile`` moves the store; editing
    ``config.yaml`` moves the declaration). Detect and label — never silently
    "fix" one side.
    """

    return [binding for binding in binding_index(cfg).values() if binding.diverged]


# --------------------------------------------------------------------------- #
# Instance busy-ness (pure classifier + store-backed collector)
# --------------------------------------------------------------------------- #
def instance_busy_reason(
    row: Mapping[str, Any],
    *,
    live_binding: bool = False,
    assignment_in_flight: bool = False,
) -> str | None:
    """PURE: the typed reason this persona-instance row is in flight, or None.

    ``live_binding`` is the store-verified liveness check
    (``PersonaInstanceStore._has_live_binding``: the referenced run is actually
    in an active state) and outranks the raw pointers, which can be stale. ``goal_id`` is deliberately NOT a busy signal — a chat instance can
    carry a goal pointer while nothing is executing.
    """

    if live_binding:
        return BUSY_LIVE_BINDING
    if _clean(row.get("active_run_id")):
        return BUSY_ACTIVE_RUN
    if assignment_in_flight or _clean(row.get("current_assignment_id")):
        return BUSY_ASSIGNMENT_IN_FLIGHT
    if _clean(row.get("current_task_id")):
        return BUSY_TASK_BOUND
    state = _state_text(row.get("state"))
    if state not in _IDLE_STATES:
        return BUSY_NON_IDLE_STATE
    return None


def _instance_rows_for_persona(persona_id: str, *, event_log: EventLog | None = None) -> list[dict[str, Any]]:
    """Every live persona-instance row bound to ``persona_id``, each classified.

    Covers the canonical ``personainst_<persona>`` channel, the
    ``personainst_<persona>_agent_<hex>`` placement/Agent-Profile rows, and any
    other row whose ``persona_id`` matches — the whole projection, not just the
    one row ``ensure_for_persona`` heals.
    """

    from .persona_assignments import PersonaAssignmentStore, PersonaInstanceStore
    from .serde import to_jsonable

    store = PersonaInstanceStore(event_log=event_log)
    instances = [item for item in store.list_all() if str(getattr(item, "persona_id", "") or "") == persona_id]
    try:
        active_assignment_instance_ids = {
            str(getattr(assignment, "persona_instance_id", "") or "")
            for assignment in PersonaAssignmentStore(event_log=event_log).find_active(persona_id=persona_id)
        }
    except Exception as exc:  # noqa: BLE001 — fail CLOSED: this is a safety gate
        # An unreadable assignment store cannot prove nothing is in flight.
        # Degrading to "no active assignments" would silently weaken the guard
        # that stops a rebind from swapping the prompt+credentials out from
        # under a running assignment, so refuse instead.
        raise PersonaProfileRebindError(
            "assignment_store_unreadable",
            f"cannot verify in-flight assignments for {persona_id}: {_safe_text(exc)}",
            details={"persona_id": persona_id},
        ) from exc
    rows: list[dict[str, Any]] = []
    for instance in instances:
        raw = to_jsonable(instance)
        try:
            # ``_has_live_binding`` already fails soft internally; when it cannot
            # resolve the worker/run the raw pointer checks below still catch the
            # row, so the gate never opens on an unverifiable instance.
            live = store._has_live_binding(instance)  # noqa: SLF001 — the rebind gate IS store maintenance
        except Exception:
            live = False
        rows.append(
            {
                "persona_instance_id": instance.id,
                "persona_id": instance.persona_id,
                "mode": instance.mode,
                "profile_id": instance.profile_id,
                "state": _state_text(raw.get("state")),
                "busy_reason": instance_busy_reason(
                    raw,
                    live_binding=live,
                    assignment_in_flight=instance.id in active_assignment_instance_ids,
                ),
            }
        )
    return sorted(rows, key=lambda row: row["persona_instance_id"])


# --------------------------------------------------------------------------- #
# Consequence report: what the new binding actually resolves to
# --------------------------------------------------------------------------- #
def binding_files(persona: AgentPersona) -> dict[str, Any]:
    """The files a persona's binding resolves to, with existence, not guesses.

    ``profile_memory`` / ``core_context`` are only reported when the persona
    opts into them (``include_profile_memory`` / ``include_core_context_files``)
    — reporting a path the runtime will never load would be a fake affordance.
    """

    from .profile_context import resolve_persona_profile
    from .prompt_sources import resolve_persona_system_prompt_path

    binding = resolve_persona_profile(persona)
    profile_home = binding.profile_home
    files: dict[str, Any] = {
        "hermes_profile": binding.hermes_profile,
        "profile_home": _path_text(profile_home),
        "readiness": binding.readiness,
        "system_prompt": _file_entry(resolve_persona_system_prompt_path(persona)),
        "soul_overlay": _file_entry(_profile_relative(profile_home, persona.soul_overlay_path)),
        "profile_memory": (
            _file_entry(profile_home / "memories" / "MEMORY.md")
            if persona.include_profile_memory and profile_home is not None
            else None
        ),
    }
    if persona.include_core_context_files and profile_home is not None:
        files["core_context"] = [
            _file_entry(profile_home / name)
            for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md")
        ]
    return files


def _persona_realm_artifacts(persona_id: str) -> list[dict[str, Any]]:
    """Per realm, the profile-file artifacts this persona currently publishes.

    Read-only consumption of ``realm_sync.resolve_realm_sync_artifacts`` (the
    single authority for "what does this realm publish"); a realm that fails to
    resolve is reported with its error instead of being silently dropped.

    Selection is on the artifact's EXPLICIT ``persona_id`` attribution, not on a
    ``/personas/<persona>/`` substring of the published path. That substring was
    a guess about the publish grammar, and when the grammar moved to
    ``store/profile_files/<profile>/<destination>`` (2026-07-25) it matched
    nothing — so this delta, which IS the ``agent set-profile`` confirmation
    output, silently reported an empty move on both the dry-run projection and
    the measured apply. A confirmation surface that quietly goes blank is exactly
    the failure mode this workstream exists to retire, so the coupling is now a
    typed field the producer sets, not a path convention two modules must agree
    on by hand.

    The substring fallback is deliberately NOT retained. It cannot help: this
    reads the LOCAL publish-side resolver, which always emits the current layout,
    so a legacy-layout path can never appear here (legacy paths exist only inside
    a PULLED subtree, which this never reads, and the pull-side
    ``legacy_flat_layout``/``superseded`` row reports a leftover LOCAL file, not a
    published artifact). And it can actively harm: a persona whose prompt lives
    in a directory named after a DIFFERENT persona would be mis-attributed.
    """

    from .realm_sync import resolve_realm_sync_artifacts
    from .store import RealmStore

    realms: list[dict[str, Any]] = []
    try:
        catalog = RealmStore().list_all(include_archived=False)
    except Exception as exc:  # noqa: BLE001 — a broken realm store must not block a rebind
        return [{"realm_id": None, "error": _safe_text(exc)}]
    for realm in catalog:
        try:
            artifacts = resolve_realm_sync_artifacts(realm.id)
        except Exception as exc:  # noqa: BLE001 — report, never swallow
            realms.append({"realm_id": realm.id, "name": getattr(realm, "name", None), "error": _safe_text(exc)})
            continue
        entries = [
            {"kind": artifact.kind, "relative_path": artifact.relative_path, "source": _path_text(artifact.source)}
            for artifact in artifacts
            if getattr(artifact, "persona_id", None) == persona_id
        ]
        if entries:
            realms.append(
                {
                    "realm_id": realm.id,
                    "name": getattr(realm, "name", None),
                    "artifacts": sorted(entries, key=lambda entry: entry["relative_path"]),
                }
            )
    return realms


def _artifact_delta(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """MEASURED delta: per realm, which relative paths disappeared / appeared."""

    by_realm: dict[str | None, dict[str, Any]] = {}
    for side, rows in (("before", before), ("after", after)):
        for row in rows:
            entry = by_realm.setdefault(
                row.get("realm_id"),
                {"realm_id": row.get("realm_id"), "name": row.get("name"), "before": set(), "after": set()},
            )
            if row.get("error"):
                entry["error"] = row["error"]
            entry[side] = {item["relative_path"] for item in row.get("artifacts") or []}
    delta: list[dict[str, Any]] = []
    for realm_id in sorted(by_realm, key=lambda value: str(value or "")):
        entry = by_realm[realm_id]
        row = {
            "realm_id": entry["realm_id"],
            "name": entry.get("name"),
            "disappears": sorted(entry["before"] - entry["after"]),
            "appears": sorted(entry["after"] - entry["before"]),
        }
        if entry.get("error"):
            row["error"] = entry["error"]
        delta.append(row)
    return delta


def _projected_artifact_delta(
    before: list[dict[str, Any]],
    *,
    old_home: Path | None,
    new_home: Path | None,
    old_profile: str | None,
    new_profile: str,
) -> list[dict[str, Any]]:
    """PROJECTED delta for ``--dry-run``, derived from MEASURED artifacts.

    The publish grammar is ``store/profile_files/<profile>/<destination>``, where
    the destination is the file's path RELATIVE TO the profile home — so a rebind
    moves exactly one segment and the tail is invariant. The projection stays a
    transform on the measured ``relative_path``s, never a re-derivation of the
    grammar (which would drift from ``realm_sync._persona_artifacts``), and both
    prefixes come from ``profile_artifact_sync.published_relative_path`` so the
    grammar is spelled in ONE place. It used to be hard-coded here as
    ``profiles/<profile>/``; when the layout moved, that produced an empty
    projection — the same silent-blank defect as the selector above, one layer up.

    An artifact whose SOURCE lives under the old profile home only reappears if
    the equivalent file exists under the new one: a persona bound to a profile
    with no ``memories/MEMORY.md`` loses that artifact rather than moving it.
    Sources outside the profile home (a repo-bundled role prompt) are unchanged
    and always reappear.
    """

    from .profile_artifact_sync import published_relative_path

    old_prefix = published_relative_path(_artifact_token(old_profile), "") if old_profile else None
    new_prefix = published_relative_path(_artifact_token(new_profile), "")
    delta: list[dict[str, Any]] = []
    for row in before:
        disappears: list[str] = []
        appears: list[str] = []
        for artifact in row.get("artifacts") or []:
            relative = str(artifact["relative_path"])
            disappears.append(relative)
            if old_prefix is None or not relative.startswith(old_prefix):
                continue
            projected_source = _reproject_source(artifact.get("source"), old_home=old_home, new_home=new_home)
            if projected_source is None:
                continue
            appears.append(new_prefix + relative[len(old_prefix) :])
        entry = {
            "realm_id": row.get("realm_id"),
            "name": row.get("name"),
            "disappears": sorted(disappears),
            "appears": sorted(appears),
        }
        if row.get("error"):
            entry["error"] = row["error"]
        delta.append(entry)
    return delta


def _reproject_source(source: str | None, *, old_home: Path | None, new_home: Path | None) -> str | None:
    """The artifact's source under the new binding, or None when it stops existing."""

    if not source:
        return None
    path = Path(source)
    if old_home is None or new_home is None:
        return source
    try:
        relative = path.relative_to(old_home)
    except ValueError:
        return source  # repo-bundled / absolute source — the binding does not move it
    candidate = new_home / relative
    return str(candidate) if candidate.exists() else None


# --------------------------------------------------------------------------- #
# The chokepoint
# --------------------------------------------------------------------------- #
def rebind_persona_profile(
    persona_id: str,
    *,
    profile: str,
    dry_run: bool = False,
    actor: str = "operator",
    event_log: EventLog | None = None,
) -> dict[str, Any]:
    """Move a persona's ``hermes_profile`` and every instance projection with it.

    Validation order (all refusals are typed and happen before ANY write):

    1. ``persona_not_persisted`` — only store-persisted personas can be rebound.
       A dormant catalog-only persona has no row to move; writing one would mint
       exactly the config/store divergence this module exists to surface.
    2. ``invalid_value`` / ``profile_missing`` — the target profile must be named
       and must exist on disk (``profile_exists``).
    3. ``profile_not_ready`` — ``resolve_persona_profile`` must report ``ready``
       for the persona under the NEW binding.
    4. ``instances_busy`` — any in-flight instance blocks the WHOLE operation and
       is named in the error. Never silently skipped: a half-rebound persona is
       the drift this verb exists to retire.

    ``dry_run=True`` runs every check, computes the full consequence report, and
    returns with ``dry_run: True`` having written nothing and emitted nothing.
    """

    from hermes_cli.profiles import normalize_profile_name, profile_exists
    from .profile_context import resolve_persona_profile
    from .store import AgentStore

    persona_id = str(persona_id or "").strip()
    log = event_log or EventLog()
    store = AgentStore(event_log=log)

    if not persona_id:
        raise PersonaProfileRebindError("persona_not_persisted", "persona_id is required")
    if persona_id.lower().startswith("profile:"):
        raise PersonaProfileRebindError(
            "persona_not_persisted",
            f"{persona_id} is a synthetic profile channel, not a stored persona; there is no binding to move",
            details={"persona_id": persona_id},
        )
    try:
        persona = store.get(persona_id)
    except Exception as exc:  # noqa: BLE001 — surface as a typed refusal
        raise PersonaProfileRebindError(
            "persona_not_persisted",
            f"agent bindings can only be moved on store-persisted agents; {persona_id} is not in the agent store",
            details={"persona_id": persona_id, "reason": _safe_text(exc)},
        ) from exc

    raw_profile = str(profile or "").strip()
    if not raw_profile:
        raise PersonaProfileRebindError(
            "invalid_value", "--profile is required", details={"persona_id": persona_id}
        )
    target_profile = normalize_profile_name(raw_profile)
    if not profile_exists(target_profile):
        raise PersonaProfileRebindError(
            "profile_missing",
            f"Hermes profile '{target_profile}' does not exist",
            details={"persona_id": persona_id, "profile": target_profile, "known_profiles": _known_profiles()},
        )

    probe = replace(persona, hermes_profile=target_profile)
    target_binding = resolve_persona_profile(probe)
    if target_binding.readiness != "ready":
        raise PersonaProfileRebindError(
            "profile_not_ready",
            f"Hermes profile '{target_profile}' is not ready: {target_binding.summary}",
            details={
                "persona_id": persona_id,
                "profile": target_profile,
                "readiness": target_binding.readiness,
                "summary": target_binding.summary,
            },
        )

    from_profile = _clean(persona.hermes_profile)
    instance_rows = _instance_rows_for_persona(persona_id, event_log=log)
    busy = [row for row in instance_rows if row["busy_reason"]]
    if busy:
        raise PersonaProfileRebindError(
            "instances_busy",
            "cannot rebind while agent instances are in flight: "
            + ", ".join(f"{row['persona_instance_id']} ({row['busy_reason']})" for row in busy),
            details={
                "persona_id": persona_id,
                "profile": target_profile,
                "busy_instances": [
                    {"persona_instance_id": row["persona_instance_id"], "reason": row["busy_reason"], "state": row["state"]}
                    for row in busy
                ],
            },
        )

    old_binding = resolve_persona_profile(persona)
    moving = [row for row in instance_rows if _clean(row["profile_id"]) != target_profile]
    moving_ids = {row["persona_instance_id"] for row in moving}
    _mode_by_id = {row["persona_instance_id"]: row["mode"] for row in instance_rows}
    persona_changes = from_profile != target_profile
    before_artifacts = _persona_realm_artifacts(persona_id)

    envelope: dict[str, Any] = {
        "ok": True,
        "persona_id": persona_id,
        "display_name": persona.display_name,
        "from_profile": from_profile,
        "to_profile": target_profile,
        "actor": _safe_text(actor, limit=80),
        "persona_changed": persona_changes,
        "instances_total": len(instance_rows),
        "instances_moved": [
            {
                "persona_instance_id": row["persona_instance_id"],
                "from_profile": _clean(row["profile_id"]),
                "to_profile": target_profile,
                "mode": row["mode"],
            }
            for row in moving
        ],
        "instances_already_bound": [
            row["persona_instance_id"] for row in instance_rows if row["persona_instance_id"] not in moving_ids
        ],
        # Always present so a consumer never has to distinguish "no failures"
        # from "this envelope shape does not report failures".
        "instances_failed": [],
        "binding_files": binding_files(probe),
        "previous_binding_files": binding_files(persona),
    }

    if dry_run:
        envelope["dry_run"] = True
        envelope["changed"] = bool(persona_changes or moving)
        envelope["realm_artifact_delta"] = {
            "measured": False,
            "projection_note": (
                "derived by re-pathing the realm's CURRENT persona artifacts onto the new profile; "
                "re-run without --dry-run for the measured delta"
            ),
            "realms": _projected_artifact_delta(
                before_artifacts,
                # Mirror ``realm_sync._persona_artifacts``: an unresolvable
                # profile home falls back to the active one, so the path
                # arithmetic below matches the artifacts actually measured.
                old_home=old_binding.profile_home or _active_home(),
                new_home=target_binding.profile_home or _active_home(),
                old_profile=from_profile,
                new_profile=target_profile,
            ),
        }
        envelope["next_expected"] = "no store write and no event were emitted; re-run without --dry-run to apply"
        return envelope

    # Authority first, projections second. Note what this ordering does and does
    # NOT buy: if a projection write fails, ``ensure_for_persona`` will later
    # re-heal the canonical ``personainst_<persona_id>`` row toward the new
    # binding, but the ``personainst_<persona>_agent_<hex>`` placement rows have
    # NO self-heal at all (live evidence 2026-07-25 — that permanent drift is the
    # whole reason this verb exists). So the rows that most need the cascade are
    # exactly the ones no later pass repairs. A failed row is therefore recorded
    # and reported, never swallowed and never allowed to abort the rest.
    if persona_changes:
        persona.hermes_profile = target_profile
        store.save(persona)
    moved, failed = _cascade_instance_profiles(target_profile, moving, event_log=log)
    changed = bool(persona_changes or moved)
    envelope["dry_run"] = False
    envelope["changed"] = changed
    # On apply, `instances_moved` reports what ACTUALLY moved, not the plan the
    # envelope was seeded with — a stranded row must never appear as moved.
    envelope["instances_moved"] = [
        {**row, "mode": _mode_by_id.get(row["persona_instance_id"])} for row in moved
    ]
    envelope["instances_failed"] = failed
    envelope["realm_artifact_delta"] = {
        "measured": True,
        "realms": _artifact_delta(before_artifacts, _persona_realm_artifacts(persona_id)),
    }
    if changed or failed:
        # Only a real mutation emits. A no-op rebind (already bound, nothing
        # drifted) wrote nothing, so an event here would be pure watermark noise
        # that forces every gated consumer into a full-core rebuild for nothing.
        # A PARTIAL run always emits: the event is the evidence channel, and
        # losing it on the exact run that stranded rows is the worst case.
        _emit_rebind_event(
            log,
            persona_id=persona_id,
            from_profile=from_profile,
            to_profile=target_profile,
            actor=actor,
            moved=moved,
            failed=failed,
        )
    if failed:
        envelope["ok"] = False
        envelope["status"] = STATUS_PARTIALLY_APPLIED
        envelope["error_code"] = "cascade_partial_failure"
        envelope["error"] = (
            "the persona binding moved but "
            f"{len(failed)} instance projection(s) did not: "
            + ", ".join(f"{item['persona_instance_id']} ({item['reason']})" for item in failed)
        )
        envelope["next_expected"] = (
            "these rows are STRANDED on the old profile and placement rows have no self-heal; "
            "re-run the same command to retry only the rows that are still drifted"
        )
        return envelope
    envelope["status"] = STATUS_APPLIED
    envelope["next_expected"] = (
        "refresh the Harness snapshot; this agent's next chat turn and mission run resolve the new profile's "
        "prompt, soul overlay, memory, skills and credentials"
    )
    return envelope


def _cascade_instance_profiles(
    target_profile: str,
    moving: list[dict[str, Any]],
    *,
    event_log: EventLog,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Re-point every drifted instance projection; ``(moved, failed)``.

    Per-row isolation, mirroring ``skill_promotion.apply_skill_inbox_pull``'s
    per-package isolation: one unwritable row can never abort the rest of the
    cascade. Aborting mid-loop would leave the persona authority already moved
    (it is written first) with an arbitrary suffix of rows stranded, no event
    emitted, and no envelope returned — the operator would get a traceback and
    no record of the partial state. A failed row is recorded with its typed
    reason and reported; the caller degrades the whole operation to a partial
    success rather than claiming a clean one.
    """

    from .persona_assignments import PersonaInstanceStore

    store = PersonaInstanceStore(event_log=event_log)
    moved: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for row in moving:
        instance_id = str(row["persona_instance_id"])
        before = _clean(row.get("profile_id"))
        try:
            instance = store.get(instance_id)
            before = _clean(instance.profile_id)
            store.set_backing_profile(instance.id, target_profile)
        except Exception as exc:  # noqa: BLE001 — one bad row must not strand the others
            failed.append(
                {
                    "persona_instance_id": instance_id,
                    "from_profile": before,
                    "reason": _safe_text(exc, limit=200),
                }
            )
            continue
        moved.append(
            {
                "persona_instance_id": instance.id,
                "from_profile": before,
                "to_profile": target_profile,
            }
        )
    return moved, failed


def _emit_rebind_event(
    event_log: EventLog,
    *,
    persona_id: str,
    from_profile: str | None,
    to_profile: str,
    actor: str,
    moved: list[dict[str, Any]],
    failed: list[dict[str, Any]] | None = None,
) -> None:
    """The single evidence record for one rebind operation.

    Always names what ACTUALLY moved, and on a partial run also names the rows
    that did not — a run that stranded rows is precisely the run whose evidence
    must not go missing. Both lists are bounded by the 4KB payload cap with the
    overflow accounted, never silently dropped.
    """

    stranded = list(failed or [])
    detail = [
        {"persona_instance_id": row["persona_instance_id"], "from_profile": row["from_profile"]}
        for row in moved[:_EVENT_INSTANCE_CAP]
    ]
    payload: dict[str, Any] = {
        "persona_id": persona_id,
        "from_profile": from_profile,
        "to_profile": to_profile,
        "actor": _safe_text(actor, limit=80),
        "instance_count": len(moved),
        "instances": detail,
    }
    if len(moved) > len(detail):
        payload["instances_truncated"] = len(moved) - len(detail)
    if stranded:
        failed_detail = [
            {"persona_instance_id": row["persona_instance_id"], "reason": _safe_text(row.get("reason"), limit=120)}
            for row in stranded[:_EVENT_FAILED_CAP]
        ]
        payload["status"] = STATUS_PARTIALLY_APPLIED
        payload["failed_count"] = len(stranded)
        payload["failed"] = failed_detail
        if len(stranded) > len(failed_detail):
            payload["failed_truncated"] = len(stranded) - len(failed_detail)
    event_log.append(Event(now(), REBIND_EVENT_TYPE, None, None, persona_id, payload))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _state_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _path_text(value: Any) -> str | None:
    return str(value) if value is not None else None


def _file_entry(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        exists = path.is_file()
    except OSError:
        exists = False
    return {"path": str(path), "exists": exists}


def _profile_relative(profile_home: Path | None, raw: str | None) -> Path | None:
    """Profile-relative overlay path, mirroring ``realm_sync._profile_relative_file``:
    absolute or escaping paths are not profile-owned and resolve to nothing."""

    if not raw or profile_home is None:
        return None
    path = Path(str(raw))
    if path.is_absolute() or ".." in path.parts:
        return None
    return profile_home / path


def _artifact_token(value: str | None) -> str:
    """Mirror of ``realm_sync._safe_token`` for matching published relative paths."""

    text = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in str(value or "").strip())
    return text.strip("._")[:120] or "item"


def _safe_text(value: Any, *, limit: int = 320) -> str:
    return str(value).replace("\n", " ").strip()[:limit]


def _active_home() -> Path | None:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home()
    except Exception:
        return None


def _known_profiles() -> list[str]:
    try:
        from hermes_cli.profiles import list_profiles

        return sorted(str(profile.name) for profile in list_profiles())
    except Exception:
        return []


__all__ = [
    "BINDING_SOURCE_CONFIG",
    "BINDING_SOURCE_STORE",
    "BINDING_SOURCE_UNBOUND",
    "BUSY_ACTIVE_RUN",
    "BUSY_ASSIGNMENT_IN_FLIGHT",
    "BUSY_LIVE_BINDING",
    "BUSY_NON_IDLE_STATE",
    "BUSY_TASK_BOUND",
    "EffectiveBinding",
    "PersonaProfileRebindError",
    "REBIND_EVENT_TYPE",
    "STATUS_APPLIED",
    "STATUS_PARTIALLY_APPLIED",
    "binding_files",
    "binding_index",
    "diverged_bindings",
    "instance_busy_reason",
    "rebind_persona_profile",
    "resolve_effective_binding",
]
