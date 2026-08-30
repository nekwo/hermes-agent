from __future__ import annotations

import re
from typing import Any, Callable

from hermes_time import now

from .delivery_directive import reap_orphan_worktrees
from .events import EventLog, event_log_health
from .snapshot import build_snapshot


DEFAULT_WORKTREE_MIN_AGE_SECONDS = 3600

# The health vocabulary every doctor section reports itself in. The verdict is
# DERIVED from these — no section may be examined without contributing one.
#
# ``unknown`` is the load-bearing member and follows the orphan-sweep precedent
# in ``cron/executions.py`` (``status='unknown'`` + "whether side effects ran is
# unknown"): a section whose probe RAISED did not observe health, so it reports
# what it knows — nothing — instead of a plausible default. A defaulted ``ok``
# here is worse than a missing check, because the doctor is the tool an operator
# runs to decide whether to keep investigating.
HEALTH_OK = "ok"
HEALTH_NOTICE = "notice"  # examined, informational only; never moves the verdict
HEALTH_DEFECT = "defect"  # examined, actionable defect observed
HEALTH_UNKNOWN = "unknown"  # NOT examined — the probe failed


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:320]


def run_harness_doctor(
    *,
    fix: bool = False,
    dry_run: bool = False,
    worktree_min_age_seconds: int = DEFAULT_WORKTREE_MIN_AGE_SECONDS,
    include_worktrees: bool = True,
    event_log: EventLog | None = None,
    snapshot_builder: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Report surviving chat-runtime health without reviving mission records.

    Checks: orphan worktrees, snapshot null-id rows, event-log health, model
    authority, persona/profile binding, root-only config misplacement, and the
    roster/office placement census. The mission-era threshold/store parameters
    and the event-compaction switch were removed with the mission lane (doc 16);
    the CLI stopped passing them in 126976088.

    **The verdict spans every section.** ``ok`` was a hardcoded ``True`` on every
    path and ``needs_fix`` was derived from two of the five sections, so a report
    documenting a broken event log, an unreadable model-authority config, or
    diverged persona bindings still announced ``ok: true, needs_fix: false``.
    That is the worst shape in this class: the doctor is the TRIAGE tool, so a
    false all-clear here terminates the investigation that would have found the
    real defect. Both flags are now derived from ``summary.section_health``:

    * ``needs_fix`` — some section observed an actionable defect.
    * ``ok`` — every section was examined AND none observed a defect. A section
      whose probe raised reports ``health: unknown`` with its error, which
      clears ``ok`` without claiming a defect it never saw.

    ``notice`` sections (stale/duplicate model pins) are informational by
    design and move neither flag.
    """

    ref = now()
    event_log = event_log or EventLog()
    snapshot_builder = snapshot_builder or build_snapshot
    if include_worktrees:
        worktrees = _worktree_report(
            min_age_seconds=max(0, int(worktree_min_age_seconds or 0)),
            event_log=event_log,
            dry_run=not fix or dry_run,
        )
    else:
        worktrees = {
            "reaped": [],
            "kept": [],
            "dry_run": True,
            "skipped": "worktree_scan_disabled",
            "health": HEALTH_OK,
        }

    snapshot_defects, snapshot_build = _snapshot_null_id_defects(snapshot_builder)
    event_health = _event_log_report()
    model_authority = _model_authority_report()
    persona_binding = _persona_binding_report()
    root_config = _root_config_misplacement_report()
    placement_census = _placement_census_report()
    # A count is an OBSERVATION. When the probe for a class did not run, the
    # honest count is ``None`` ("not observed"), never ``0`` ("observed none") —
    # a zero here is what sends an investigator hunting a defect class the
    # doctor never actually looked at.
    finding_counts = {
        "orphan_worktrees": (
            None
            if worktrees.get("health") == HEALTH_UNKNOWN
            else len(worktrees.get("reaped") or [])
        ),
        "snapshot_null_id_rows": (
            None if snapshot_build["health"] == HEALTH_UNKNOWN else len(snapshot_defects)
        ),
        "misplaced_root_only_keys": (
            None
            if root_config.get("health") == HEALTH_UNKNOWN
            else len(root_config.get("misplaced") or [])
        ),
        # The census contributes TWO counts because they are two different
        # verdicts: an orphan actor is a defect and an unplaced row is a legal
        # state of a supported door. Folding them into one number would make the
        # doctor's headline count climb every time the roster-only recovery
        # door is used correctly.
        "orphan_actors": (
            None
            if placement_census.get("health") == HEALTH_UNKNOWN
            else len(placement_census.get("orphan_actors") or [])
        ),
        "unplaced_rows": (
            None
            if placement_census.get("health") == HEALTH_UNKNOWN
            else len(placement_census.get("unplaced_rows") or [])
        ),
        # THREE, since DL-H1. Desk litter is a third verdict again: not a
        # defect (nothing mis-renders — the desk is authored content standing
        # where its agent no longer is) and not a legal door either. It rides
        # the census's own report dict rather than a new section precisely so
        # this is the ONLY roster the extension touches — ``section_health``,
        # ``findings`` and the CLI's ``detail_sources`` already carry a
        # ``placement_census`` row each. The four-hand-maintained-rosters
        # weakness stays open and is deliberately not worked here.
        "desk_litter": (
            None
            if placement_census.get("health") == HEALTH_UNKNOWN
            else len(placement_census.get("desk_litter") or [])
        ),
    }
    section_health = {
        "orphan_worktrees": worktrees.get("health", HEALTH_UNKNOWN),
        "snapshot_null_id_rows": snapshot_build["health"],
        "event_log": event_health.get("health", HEALTH_UNKNOWN),
        "model_authority": model_authority.get("health", HEALTH_UNKNOWN),
        "persona_binding": persona_binding.get("health", HEALTH_UNKNOWN),
        "root_config_misplacement": root_config.get("health", HEALTH_UNKNOWN),
        "placement_census": placement_census.get("health", HEALTH_UNKNOWN),
    }
    defective = sorted(k for k, v in section_health.items() if v == HEALTH_DEFECT)
    unexamined = sorted(k for k, v in section_health.items() if v == HEALTH_UNKNOWN)
    repairs = {
        "worktrees_reaped": (
            [item.get("worktree") for item in (worktrees.get("reaped") or []) if item.get("worktree")]
            if fix and not dry_run
            else []
        ),
        "dry_run": bool(dry_run),
    }
    return {
        # 3: ``ok``/``needs_fix`` became derived, ``summary.section_health`` /
        # ``defective_sections`` / ``unexamined_sections`` are new, a
        # ``finding_counts`` value may now be ``None`` (class not observed), and
        # ``findings.snapshot_build`` separates a snapshot CRASH from an
        # observation of null-id rows.
        # 4: ``findings.root_config_misplacement`` is new — root-only keys an
        # operator set in a PROFILE config, where the reader never looks. It is
        # a DEFECT rather than a notice because the value is silently inert:
        # the 2026-08-13 case left the S7-A patch producer dark for its whole
        # life while ``harness status`` reported the flag as on.
        # 5: ``findings.placement_census`` is new — the roster/office join
        # (plan D8), read-only, with ``summary.finding_counts`` gaining
        # ``orphan_actors`` and ``unplaced_rows``.
        # 6: ``findings.placement_census.desk_litter`` is new (plan DL-H1) —
        # the ITEM-level desk sweep the actor-level join is blind to, with
        # ``summary.finding_counts`` gaining ``desk_litter``. No new section:
        # the census's own health absorbs it, at ``notice``.
        "schema_version": 6,
        "generated_at": ref,
        "ok": not defective and not unexamined,
        "mode": {"fix": bool(fix), "dry_run": bool(dry_run)},
        "thresholds": {
            "worktree_min_age_seconds": int(worktree_min_age_seconds),
            "include_worktrees": bool(include_worktrees),
        },
        "summary": {
            "finding_counts": finding_counts,
            "section_health": section_health,
            "defective_sections": defective,
            "unexamined_sections": unexamined,
            "needs_fix": bool(defective),
            "repairs_applied": bool(fix and not dry_run),
            "preserved_evidence": True,
            "product_repos_modified": False,
        },
        "findings": {
            "orphan_worktrees": worktrees,
            "snapshot_null_id_rows": snapshot_defects,
            "snapshot_build": snapshot_build,
            "event_log": event_health,
            "root_config_misplacement": root_config,
            "placement_census": placement_census,
        },
        "model_authority": model_authority,
        "persona_binding": persona_binding,
        "repairs": repairs,
    }


def _worktree_report(
    *, min_age_seconds: int, event_log: EventLog, dry_run: bool
) -> dict[str, Any]:
    """Orphan-worktree sweep, with a failed sweep reported as unexamined.

    The sweep shells out to git across every registered worktree, so it is I/O
    that can fail (a deleted checkout, a locked index, a git that is not on
    PATH). It ran unguarded, which meant a failure either crashed the whole
    doctor or — worse, once wrapped naively — would have read as "no orphans".
    """

    try:
        report = dict(
            reap_orphan_worktrees(
                min_age_seconds=min_age_seconds,
                event_log=event_log,
                dry_run=dry_run,
            )
        )
    except Exception as exc:
        return {
            "health": HEALTH_UNKNOWN,
            "error": _error_text(exc),
            # NOT ``[]`` — the sweep enumerated nothing, it did not find nothing.
            "reaped": None,
            "kept": None,
            "dry_run": bool(dry_run),
        }
    report["health"] = HEALTH_DEFECT if (report.get("reaped") or []) else HEALTH_OK
    return report


def _event_log_report() -> dict[str, Any]:
    """Event-log health, which rode the payload but never moved the verdict.

    ``event_log_health`` stats the live slice and the rotation manifest, so on
    this runtime's platform it can raise under AV/share-violation contention.
    Unguarded, that crashed the doctor outright; the section now reports what it
    could not read.
    """

    try:
        health = dict(event_log_health())
    except Exception as exc:
        return {"health": HEALTH_UNKNOWN, "error": _error_text(exc)}
    health["health"] = HEALTH_OK if health.get("index_health") == "ok" else HEALTH_DEFECT
    return health


def _persona_binding_report() -> dict[str, Any]:
    try:
        from .persona_profile_binding import binding_index

        index = binding_index()
    except Exception as exc:
        return {
            "ok": False,
            "health": HEALTH_UNKNOWN,
            "error": _error_text(exc),
            "diverged": [],
        }
    diverged = [binding.as_row() for binding in index.values() if binding.diverged]
    return {
        "ok": True,
        # Divergence carries a remediation string precisely because it happens,
        # which makes it actionable — so it moves the verdict.
        "health": HEALTH_DEFECT if diverged else HEALTH_OK,
        "resolved_by": "store_wins",
        "agent_count": len(index),
        "diverged_count": len(diverged),
        "diverged": sorted(diverged, key=lambda row: row["persona_id"]),
        "remediation": "harness agent set-profile <persona_id> --profile <name> (moves the store) or edit config.yaml (moves the declaration)",
    }


def _root_config_misplacement_report() -> dict[str, Any]:
    """Root-only config keys an operator set in a PROFILE, where nothing reads them.

    Four keys resolve through :func:`config.harness_root_config_path` and never
    consult the active profile (``read_model.delta_patches``, ``mcp_admission``,
    and the per-persona ``chat_lane_restore_toolsets`` / ``workdir``). YAML
    accepts them anywhere, and profile-aware surfaces like ``harness status``
    report them back, so a value written one layer below its reader looks
    applied and does nothing.

    Severity splits on whether the ROOT also carries the key:

    * ``defect`` — profile only. The operator's instruction is INERT. Measured
      cost of the 2026-08-13 instance: the S7-A patch producer stayed dark, so
      one field change shipped an 822,671-byte delta instead of a 486-byte
      patch, while ``harness status`` reported the flag on the whole time.
    * ``notice`` — set in both. The live value is correct; the profile copy is a
      redundant leftover worth deleting but not worth failing a health check
      over.
    """

    from .config import find_misplaced_root_only_keys

    try:
        rows = find_misplaced_root_only_keys()
    except Exception as exc:
        return {"available": False, "health": HEALTH_UNKNOWN, "error": _error_text(exc)}

    inert = [row for row in rows if not row.get("set_in_root")]
    redundant = [row for row in rows if row.get("set_in_root")]
    if inert:
        health = HEALTH_DEFECT
    elif redundant:
        health = HEALTH_NOTICE
    else:
        health = HEALTH_OK
    return {
        "available": True,
        "health": health,
        "misplaced": rows,
        "inert": inert,
        "redundant": redundant,
        "notices": [
            f"{row['key']} set in profile '{row['profile']}' is ignored — "
            f"{row['read_only_by']} reads {row['root_config_path']}"
            for row in inert
        ]
        + [
            f"{row['key']} in profile '{row['profile']}' duplicates the root value and is unread"
            for row in redundant
        ],
    }


def _model_authority_report() -> dict[str, Any]:
    from .config import describe_runtime_default_authority

    try:
        authority = describe_runtime_default_authority()
    except Exception as exc:
        return {"available": False, "health": HEALTH_UNKNOWN, "error": _error_text(exc)}
    override = authority.get("harness_override", {})
    pins = authority.get("persona_pins", []) or []
    redundant_pins = [p for p in pins if p.get("matches_runtime_default") is True]
    provider_only_pins = [p for p in pins if p.get("provider_pinned_without_model")]
    notices: list[str] = []
    if override.get("model_state") == "shadowing":
        notices.append(
            f"agent_runtime.default_model ({override.get('model')}) shadows the runtime default from model.default"
        )
    elif override.get("model_state") == "redundant":
        notices.append("agent_runtime.default_model duplicates model.default and is unmaintained")
    if redundant_pins:
        notices.append(
            "persona pins duplicate the runtime default: "
            + ", ".join(sorted(p.get("persona_id", "?") for p in redundant_pins))
        )
    if provider_only_pins:
        notices.append(
            "persona provider pinned without a model: "
            + ", ".join(sorted(p.get("persona_id", "?") for p in provider_only_pins))
        )
    return {
        "available": True,
        # Stale/duplicate pins are informational by contract (a pin never turns
        # the doctor into a fix job), so notices are examined-but-not-actionable.
        "health": HEALTH_NOTICE if notices else HEALTH_OK,
        "resolved": authority.get("resolved", {}),
        "top_level": authority.get("top_level", {}),
        "harness_override": override,
        "persona_pins": pins,
        "divergent": override.get("model_state") == "shadowing",
        "notices": notices,
    }


def _snapshot_null_id_defects(
    snapshot_builder: Callable[[], dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Null-id rows observed in the frame, plus whether the frame built at all.

    A build CRASH used to be returned as one ``snapshot_null_id_rows`` defect —
    the counter naming a defect class nobody observed, sending an investigator
    hunting null-id rows in a frame that never existed. The two facts are now
    separate: the defect list only ever holds rows actually inspected, and the
    build outcome rides its own ``snapshot_build`` section as ``unknown`` + the
    error, which clears the report's ``ok`` without inventing a finding.
    """

    try:
        snapshot = snapshot_builder()
    except Exception as exc:
        return [], {
            "health": HEALTH_UNKNOWN,
            "observed": False,
            "error": _error_text(exc),
        }
    expected = {
        "agents": "persona_id",
        "persona_instances": "persona_instance_id",
    }
    defects: list[dict[str, Any]] = []
    for collection, id_key in expected.items():
        rows = snapshot.get(collection)
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if isinstance(row, dict) and not row.get(id_key):
                defects.append({"collection": collection, "index": index, "id_key": id_key})
    return defects, {
        "health": HEALTH_DEFECT if defects else HEALTH_OK,
        "observed": True,
    }


# -- the roster/office join, as a READ (plan D1/D8) ---------------------------
#
# Two stores answer two different questions and neither is folded into the
# other: the persona-instance row answers "does this agent exist" (the
# roster-only recovery door ``persona instance create --add-instance``
# legitimately mints rows that were never placed), and the instance-keyed
# office actor answers "is it on this level". Nothing had ever looked at the
# JOIN — ``harness doctor`` reported six sections and none of them was this
# one, and ``persona instance reconcile`` prunes orphan ROWS without ever
# opening the office. So a half-state (a retired instance whose actor survived,
# a placement whose compensation archived the row and not the desk) was
# representable, durable, and invisible to the tool an operator runs to find it.
#
# This section is a READ and only a read. The repairs already exist and are
# deliberate operator gestures — a retire for an orphan actor, a resumed create
# for an unplaced row — so a doctor that silently reconciled them would be
# choosing which of the two stores was wrong on the operator's behalf, on
# evidence it can only see one snapshot of.


def _census_instance_key(instance: Any) -> str:
    """The one spelling both stores are compared in.

    ``OfficeStore.upsert_actor`` stores ``persona_instance_id`` through
    ``canonical_persona_instance_id`` (via ``_canonical_actor_key``), so a
    roster row still carrying a legacy spelling would read as an orphan against
    its own actor if the two sides were compared raw. Routing BOTH sides through
    the single derivation authority is what keeps this census from inventing
    findings out of the id drift that ``persona instance reconcile`` exists to
    fold.
    """

    from .persona_assignments import canonical_persona_instance_id

    raw = str(getattr(instance, "id", "") or "").strip()
    canonical = canonical_persona_instance_id(
        raw, persona_id=getattr(instance, "persona_id", None)
    )
    return canonical or raw


def _census_unknown(detail: str, *, unreadable: list[str] | None = None) -> dict[str, Any]:
    """The census as UNEXAMINED. Every count is ``None``, never ``0``/``[]``.

    A store this section could not read leaves it with no world to count, and an
    empty list here would read to an operator as "looked, found none" — the
    false all-clear the whole doctor is written against.

    A SHORT world takes this path too, and that is the subtle half. Both scans
    return the rows they could read beside a count of the ones they could not
    (``PersonaInstanceScan`` / ``ActorScan`` carry that count for exactly this
    reason). A census that partitioned the readable remainder would report a
    perfectly healthy placement as an ORPHAN — because the file that would not
    decode is its roster row — inventing a defect out of an outage and pointing
    the operator's remediation at the wrong store. So a partition is computed
    only over a world that was read in full.
    """

    report: dict[str, Any] = {
        "health": HEALTH_UNKNOWN,
        "error": detail,
        "observed": False,
        "placed": None,
        "unplaced_rows": None,
        "orphan_actors": None,
        # Desk litter inherits the rule rather than reasoning about it again.
        # Every one of its four buckets is a statement about ABSENCE ("no live
        # agent item for this persona", "no roster row for this binding"), and
        # a file that would not open is exactly what absence is
        # indistinguishable from. So the whole census answers unknown — not
        # "unknown for the workspace whose directory was short" — and this key
        # is ``None`` beside the other three.
        "desk_litter": None,
        "workspaces": None,
    }
    if unreadable:
        report["unreadable"] = sorted(unreadable)
    return report


# ── desk litter: the item-level sweep the actor-level join cannot see ─────────
#
# Four reasons, four different faults, deliberately NOT collapsed (plan DL-H1
# §0/§6). The first day of the lane was spent conflating the mis-kinded agent
# with the widowed desk, which have opposite cures — one is re-placed, one is
# reaped — so folding them into a single "desk litter" count would reproduce
# the confusion the census exists to end. A row carries exactly one of these,
# and the classifier below is total over its inputs.

#: No live ``kind: "agent"`` item exists for this desk's persona anywhere in the
#: workspace. Era litter: placement used to mint agent+desk pairs, and
#: ``OfficeStore.archive_actors_for_instance`` still archives only the
#: INSTANCE-bound actor, so a roster-side retire leaves the class-keyed desk
#: actor live. The reap's target.
DESK_LITTER_AGENT_MISSING = "agent_missing"

#: Live agent items exist for the persona, and every one of them rides an actor
#: whose instance binding no live roster row backs. The store-side shadow of the
#: launcher's projection scope drop: the agent half vanishes from the canvas and
#: the desk renders on. Overlaps ``orphan_actors`` ON PURPOSE — that row names
#: the actor, this one names the desk left standing; two pointers to one fault.
DESK_LITTER_AGENT_SCOPE_STALE = "agent_scope_stale"

#: The persona itself is gone: no live roster row carries it and no retirement
#: tombstone names an instance of it either. A refinement of
#: ``agent_missing`` — the desk is not merely widowed, there is nothing left to
#: re-place — and it is reported separately because the cures differ.
DESK_LITTER_PERSONA_RETIRED = "persona_retired"

#: The item is structurally an AGENT's that persisted with ``kind: "desk"``:
#: it rides an actor whose ``persona_instance_id`` names a LIVE roster row, or
#: its ``item_id`` carries an agent marker. This is the shape MEASURED on the
#: live store 2026-08-30, and it is kept out of ``agent_missing`` by design —
#: an operator reading "widowed desk" goes looking for a reap, when what this
#: row wants is a re-place.
DESK_LITTER_DESK_KIND_AGENT_BINDING = "desk_kind_agent_binding"

DESK_LITTER_REASONS = (
    DESK_LITTER_AGENT_MISSING,
    DESK_LITTER_AGENT_SCOPE_STALE,
    DESK_LITTER_PERSONA_RETIRED,
    DESK_LITTER_DESK_KIND_AGENT_BINDING,
)

#: What an ``item_id`` says about the kind it was MINTED for. A tri-state, not a
#: bool, because "carries no marker either way" is a real and common answer
#: (operator-authored ids, peer ids from another client) and must never be
#: silently folded into "not an agent" — see :func:`_office_item_id_shape`.
ITEM_ID_SHAPE_AGENT = "agent"
ITEM_ID_SHAPE_DESK = "desk"
ITEM_ID_SHAPE_UNKNOWN = "unknown"


def _office_item_id_shape(item_id: Any) -> str:
    """Which kind the launcher MINTED this item id for, or ``unknown``.

    The markers are the launcher's own minting conventions, and there are only
    three sites:

    * ``MissionOfficeLayout.addItem`` — ``<persona>_<kind>`` with a ``_<n>``
      collision suffix (``qa_agent``, ``qa_agent_2``, ``qa_desk``);
    * ``MissionOfficeLayout.materializeAgentDesk`` /
      ``mission_office_authoring_policy`` — ``desk-<agentItemId>``;
    * ``agent_create.placement_actor_payload`` — the item id IS the persona
      instance id (``personainst_qa_agent_2``), which is why the bare
      ``personainst`` head counts as an agent marker.

    THE DESK MARKER WINS, and that ordering is load-bearing: ``desk-qa_agent``
    carries both, and it is a desk. Reading it as an agent would file the one
    id the desk-materializer mints under ``desk_kind_agent_binding`` — every
    legitimately materialized desk on every store.

    The agent test is POSITIVE — an id must carry an agent marker — never
    "not desk-shaped". An id this function cannot read is ``unknown`` and falls
    through to the absence buckets, where the desk is judged on whether its
    agent actually exists rather than on how somebody spelled a string. Over-
    claiming here is the expensive direction: it would fold widowed desks into
    the mis-kinded bucket, which is the exact conflation §0 of the plan was
    written to stop.

    Pure, and the only thing in this section that reads a name rather than a
    fact — which is why the classifier below consults it SECOND, after the live
    instance binding, a fact no spelling can forge.
    """

    text = str(item_id or "").strip().lower()
    if not text:
        return ITEM_ID_SHAPE_UNKNOWN
    tokens = [token for token in re.split(r"[-_]", text) if token]
    if not tokens:
        return ITEM_ID_SHAPE_UNKNOWN
    tail = tokens[-1]
    if len(tokens) > 1 and tail.isdigit():
        # ``addItem``'s collision disambiguator, which is appended AFTER the
        # kind token: ``qa_desk_2`` is a desk, not an id ending in a number.
        tail = tokens[-2]
    if tokens[0] == ITEM_ID_SHAPE_DESK or tail == ITEM_ID_SHAPE_DESK:
        return ITEM_ID_SHAPE_DESK
    if tail == ITEM_ID_SHAPE_AGENT or tokens[0] == "personainst":
        return ITEM_ID_SHAPE_AGENT
    return ITEM_ID_SHAPE_UNKNOWN


def _desk_litter_reason(
    *,
    item_id: Any,
    on_live_instance_actor: bool,
    agent_item_bindings: tuple[str, ...],
    live_instance_ids: frozenset[str],
    persona_known: bool,
) -> str | None:
    """Which of the four faults this desk item is, or ``None`` when it is fine.

    Pure — every store read the decision needs has already happened, and been
    gated on a fully-read world, before this is called. That is deliberate: the
    partition is the part worth unit-testing, and it must not be reachable only
    through a filesystem fixture.

    ``agent_item_bindings`` is the instance binding of EVERY live
    ``kind: "agent"`` item that shares this desk's persona in this workspace,
    ``""`` for the class-keyed ones. Empty means the desk is widowed.

    THE ORDER IS THE DESIGN:

    1. ``desk_kind_agent_binding`` first, because a mis-kinded agent item is
       also, by construction, a persona with no live agent item — so any other
       order silently reports every one of them as widowed.
    2. Then the agent items themselves. Some binding that a live roster row
       backs (including a class-keyed one, which is not instance-bound and
       therefore cannot scope-drop) means the pair is whole: no row.
    3. ``persona_retired`` before ``agent_missing`` — it is the narrower
       statement of the same absence, and the operator's next move differs
       (nothing to re-place versus an agent to re-place).

    ``live_instance_ids`` is the only staleness test, and the retirement
    archive is deliberately NOT unioned into it. Retirement is the ARCHIVE half
    of the predicate whose live half a roster row already answers, and
    ``retired_persona_instance_ids``' own contract is that a live row wins; a
    union would call a re-created instance stale on the strength of its own
    tombstone. The archive is still read — it is what makes ``persona_retired``
    distinguishable from ``agent_missing`` — just not here.
    """

    if on_live_instance_actor or _office_item_id_shape(item_id) == ITEM_ID_SHAPE_AGENT:
        return DESK_LITTER_DESK_KIND_AGENT_BINDING
    if agent_item_bindings:
        if all(
            binding and binding not in live_instance_ids
            for binding in agent_item_bindings
        ):
            return DESK_LITTER_AGENT_SCOPE_STALE
        return None
    if not persona_known:
        return DESK_LITTER_PERSONA_RETIRED
    return DESK_LITTER_AGENT_MISSING


def _persona_has_retired_instance(persona_id: str, retired: frozenset[str]) -> bool:
    """Did any instance of this persona carry a retirement tombstone?

    The join is on the id SCHEME, because a retirement archive keeps ids, not
    persona pointers: every instance of a persona is either that persona's
    canonical operator channel (``persona_instance_id_for``) or a
    placement-derived id built by extending it with ``_<placement>``. Both are
    minted by the one derivation authority, so the prefix test asks that
    authority's question rather than inventing a second spelling rule.

    Used for ONE discrimination — telling ``persona_retired`` (nothing of this
    persona was ever, or is any longer, on the roster) from ``agent_missing``
    (the persona is alive, its agent item is not). A false NEGATIVE here
    reports the softer of the two reasons, which is the safe direction: the
    desk is still counted, still named, still reaped by the same verb.
    """

    from .persona_assignments import persona_instance_id_for

    canonical = persona_instance_id_for(persona_id)
    if not canonical:
        return False
    return any(
        instance_id == canonical or instance_id.startswith(f"{canonical}_")
        for instance_id in retired
    )


def _placement_census_report() -> dict[str, Any]:
    """Per-workspace roster/office join: placed, unplaced rows, orphan actors.

    The definitions are the plan's (D1), stated once here because three
    different readings of "placed" is how the two stores drifted in the first
    place:

    * ``placed`` — a LIVE instance-keyed actor whose ``persona_instance_id``
      names a LIVE roster row. Both halves present is the only whole shape.
    * ``unplaced_rows`` — a live placement-backed row (i.e. NOT a canonical
      persona channel, per ``is_canonical_persona_channel``) that no live actor
      references. LEGAL, not a defect: the roster-only door mints exactly this,
      on purpose. Reported as a ``notice`` so an operator can see them without
      the doctor calling a supported gesture broken.
    * ``orphan_actors`` — a live instance-keyed actor whose instance is retired
      or missing. A DEFECT: it renders on the level as an agent nothing can
      message.
    * ``desk_litter`` (plan DL-H1) — a live ``kind: "desk"`` ITEM whose agent
      half is missing, stale, personaless, or never was a desk at all, one of
      the four ``DESK_LITTER_*`` reasons each. A ``notice``, never a defect:
      unlike an orphan actor, nothing here mis-renders — the desk is authored
      furniture standing where its agent no longer is, and the operator's act
      is the reap (DL-H2), not a repair the doctor could suggest inline.

      It is a SEPARATE finding from the three above rather than an extension of
      them because the join above is actor-level and instance-keyed, and desk
      litter is neither: a desk minted by ``materializeAgentDesk`` carries the
      persona CLASS id and no binding, so it lands in the class-keyed actor
      that the join skips by construction, while its agent lands in the
      instance-keyed one. That split — the pairing is persona-level, the
      storage is actor-level — is why this walk is over ITEMS and why it joins
      per workspace on ``persona_id``.

    ``health`` is ``unknown`` — never ``ok`` — when either store could not be
    read in full. That includes a scan that returned rows AND a nonzero
    ``unreadable`` count: a census computed over a short world reports an actor
    as orphaned because its roster row is the file that would not decode, which
    is the exact false finding this doctor's None-not-zero counting rule exists
    to forbid.
    """

    # ``_normalize_persona_id`` is the STORE's own spelling of a persona id, and
    # it is imported rather than re-derived for the reason
    # ``office_class_key_guard`` states at its own import of it: a second
    # normalization beside the one the write path used is how the two halves of
    # a join come to disagree about the same persona.
    from .office_store import OfficeStore, _normalize_persona_id
    from .persona_assignments import (
        PersonaInstanceStore,
        is_canonical_persona_channel,
        retired_persona_instance_ids,
    )

    unreadable: list[str] = []

    try:
        roster = PersonaInstanceStore().scan_all()
    except Exception as exc:
        return _census_unknown(_error_text(exc))
    if roster.unreadable:
        unreadable.append(f"persona_instances:{roster.unreadable}")

    live_rows = {_census_instance_key(row): row for row in roster.instances}

    store = OfficeStore()
    try:
        workspace_ids = list(store.list_workspaces())
    except Exception as exc:
        return _census_unknown(_error_text(exc))

    placed: list[dict[str, Any]] = []
    orphan_actors: list[dict[str, Any]] = []
    desk_litter: list[dict[str, Any]] = []
    per_workspace: dict[str, dict[str, Any]] = {}
    referenced: set[str] = set()

    scans: list[tuple[str, Any]] = []
    for workspace_id in workspace_ids:
        try:
            scan = store.scan_actors(workspace_id)
        except Exception as exc:
            unreadable.append(f"office:{workspace_id} ({_error_text(exc)})")
            continue
        if scan.unreadable:
            unreadable.append(f"office:{workspace_id}:{scan.unreadable}")
        scans.append((workspace_id, scan))

    # EVERY scan first, the partition second, and the gate between them. One
    # unreadable file anywhere in either store is enough to make the JOIN — not
    # merely one row of it — untrustworthy, because the census's two findings
    # are both statements about ABSENCE ("no live actor references this row",
    # "no live row backs this actor") and absence is precisely what a file that
    # would not open is indistinguishable from.
    if unreadable:
        return _census_unknown(
            "unreadable: " + ", ".join(sorted(unreadable)), unreadable=unreadable
        )

    # Read ONCE for the whole census, never per row. The archive is one
    # directory per retire, forever, and ``retired_persona_instance_ids`` says
    # so at its own docstring. It NEVER raises — a listing it could not walk
    # answers the empty set — so it cannot re-open the unreadable gate above,
    # and the consequence of that outage is only that ``persona_retired``
    # degrades to ``agent_missing``: the softer reason, same row, same reap.
    retired_instances = retired_persona_instance_ids()
    live_persona_ids = {
        normalized
        for normalized in (
            _normalize_persona_id(row.persona_id) for row in roster.instances
        )
        if normalized
    }
    live_instance_ids = frozenset(live_rows)

    for workspace_id, scan in scans:
        ws_placed: list[dict[str, Any]] = []
        ws_orphans: list[dict[str, Any]] = []
        live_actors = [actor for actor in scan.actors if actor.state != "archived"]
        for actor in live_actors:
            instance_id = str(actor.persona_instance_id or "").strip()
            if not instance_id:
                # A class-keyed actor answers no roster question: it is keyed on
                # the persona, not on an instance, so it is out of this join by
                # construction rather than by omission.
                continue
            referenced.add(instance_id)
            row = {
                "workspace_id": workspace_id,
                "actor_key": actor.actor_key,
                "persona_id": actor.persona_id,
                "persona_instance_id": instance_id,
            }
            if instance_id in live_rows:
                ws_placed.append(row)
            else:
                ws_orphans.append(row)

        # The desk sweep, over the SAME live actors of the SAME fully-read
        # world. Two passes and not one: the second pass must be able to ask
        # "does an agent item for this persona exist ANYWHERE in this
        # workspace", and a single pass could only ask "…in an actor I have
        # already read", which is an answer that depends on directory order.
        agent_bindings: dict[str, list[str]] = {}
        for actor in live_actors:
            binding = str(actor.persona_instance_id or "").strip()
            for item in actor.items or ():
                if getattr(item, "kind", None) != "agent":
                    continue
                persona = _normalize_persona_id(item.persona_id) or _normalize_persona_id(
                    actor.persona_id
                )
                if persona:
                    agent_bindings.setdefault(persona, []).append(binding)

        ws_litter: list[dict[str, Any]] = []
        for actor in live_actors:
            binding = str(actor.persona_instance_id or "").strip()
            for item in actor.items or ():
                if getattr(item, "kind", None) != "desk":
                    continue
                persona = _normalize_persona_id(item.persona_id) or _normalize_persona_id(
                    actor.persona_id
                )
                if not persona:
                    # A persona-less desk answers no pairing question — there is
                    # nothing to pair it WITH. Out of the sweep by construction,
                    # which is also the shape the parked "desks become standalone
                    # artifacts" ruling would make the common one.
                    continue
                reason = _desk_litter_reason(
                    item_id=item.item_id,
                    on_live_instance_actor=bool(binding) and binding in live_instance_ids,
                    agent_item_bindings=tuple(agent_bindings.get(persona, ())),
                    live_instance_ids=live_instance_ids,
                    persona_known=(
                        persona in live_persona_ids
                        or _persona_has_retired_instance(persona, retired_instances)
                    ),
                )
                if reason is None:
                    continue
                ws_litter.append(
                    {
                        "workspace_id": workspace_id,
                        "actor_key": actor.actor_key,
                        "item_id": item.item_id,
                        "persona_id": persona,
                        "persona_instance_id": binding or None,
                        "reason": reason,
                    }
                )

        placed.extend(ws_placed)
        orphan_actors.extend(ws_orphans)
        desk_litter.extend(ws_litter)
        per_workspace[workspace_id] = {
            "placed": len(ws_placed),
            "unplaced_rows": [],
            "orphan_actors": ws_orphans,
            "desk_litter": ws_litter,
            "observed": True,
        }

    unplaced_rows: list[dict[str, Any]] = []
    for key, row in sorted(live_rows.items()):
        if key in referenced:
            continue
        if is_canonical_persona_channel(row):
            # The persona's global operator channel is not a placement and was
            # never meant to hold one. Counting it would report one "unplaced"
            # row per persona on every healthy runtime — a finding the operator
            # can never clear, which is how a census stops being read.
            continue
        entry = {
            "persona_instance_id": key,
            "persona_id": row.persona_id,
            "workspace_id": row.workspace_id,
        }
        unplaced_rows.append(entry)
        bucket = per_workspace.get(row.workspace_id or "")
        if isinstance(bucket, dict) and isinstance(bucket.get("unplaced_rows"), list):
            bucket["unplaced_rows"].append(entry)

    if orphan_actors:
        health = HEALTH_DEFECT
    elif unplaced_rows or desk_litter:
        # Litter raises the census to ``notice`` and NEVER past it. An orphan
        # actor is a defect because it renders as an agent nothing can message;
        # a litter desk renders as exactly what it is — a desk. Promoting it
        # would turn ``needs_fix`` on for a store whose only fault is furniture,
        # and the doctor's whole contract is that its flags mean something.
        health = HEALTH_NOTICE
    else:
        health = HEALTH_OK
    report: dict[str, Any] = {
        "health": health,
        "observed": True,
        "placed": len(placed),
        "placed_actors": placed,
        "unplaced_rows": unplaced_rows,
        "orphan_actors": orphan_actors,
        "desk_litter": desk_litter,
        "workspaces": per_workspace,
        "remediation": (
            "an orphan actor is cleared by retiring or re-creating its agent; "
            "an unplaced row is either awaiting a placement or is the "
            "roster-only recovery door working as designed; a desk_litter row "
            "reading desk_kind_agent_binding wants a re-place, and the other "
            "three want a reap"
        ),
    }
    return report
