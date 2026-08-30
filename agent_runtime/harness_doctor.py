from __future__ import annotations

from dataclasses import dataclass, field
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


# ── the section table: ONE declaration, four derived rosters ─────────────────
#
# A doctor section used to be added by editing four hand-maintained lists of one
# set — ``finding_counts``, ``section_health`` and ``findings`` here, plus
# ``detail_sources`` in the CLI printer — with only ``section_health``'s key set
# pinned by a test. A section added to three of the four was therefore counted
# and verdicted while rendering NO operator line, and nothing failed. That is
# the same shape the derived-verdict rework already fixed once by hand (``ok``
# and ``needs_fix`` were computed from two of five sections), which is why this
# one is fixed structurally instead: every roster below is DERIVED from
# :data:`DOCTOR_SECTIONS`, so a new section is one table row and a missing one
# is unspellable.
#
# The table lives at the BOTTOM of this module, where the probes it names are
# defined. Read it first anyway — it is the index to everything above it.


@dataclass(frozen=True)
class _DoctorProbeContext:
    """Every input a probe may read, so all probes share ONE signature.

    A uniform signature is what lets the table hold the probe: a roster of
    sections that could not also name how to run them would be a fifth list to
    keep in step with the other four.
    """

    fix: bool
    dry_run: bool
    worktree_min_age_seconds: int
    include_worktrees: bool
    event_log: EventLog
    snapshot_builder: Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class DoctorSection:
    """One doctor section, declared once.

    * ``name`` — its key in ``summary.section_health``, and the name an
      operator sees on the CLI's per-section line.
    * ``probe`` — the callable that observes it. It must return a dict carrying
      a ``health`` from the vocabulary above; a section whose probe answers
      without one reads ``unknown``, never ``ok``. A probe that reads NOTHING
      from the context declares it optional (``_context=None``), which says so
      in the signature and keeps the probe callable as a bare seam — several
      suites unit-test one section by calling its probe directly, and making
      them build a context to hand a function that ignores it would be
      ceremony, not clarity.
    * ``publish`` — where its report lands in the payload, as
      ``(dotted destination, key inside the report)``. ``None`` publishes the
      whole report dict; a key publishes that member, which is how
      ``snapshot_null_id_rows`` (the row list) and ``snapshot_build`` (whether
      the frame built at all) stay two payload keys from one probe.
    * ``counts`` — the ``summary.finding_counts`` entries it contributes, as
      ``(count name, list key inside the report)``. A count is an OBSERVATION:
      an unexamined section's counts are ``None``, never ``0`` — see
      :func:`run_harness_doctor`.
    * ``detail_source`` — the dotted payload path whose ``error`` the CLI
      prints beside a non-ok section. Usually the section's own report; the
      exception is ``snapshot_null_id_rows``, a bare list whose build outcome
      lives one key over.
    """

    name: str
    probe: Callable[[_DoctorProbeContext], dict[str, Any]]
    publish: tuple[tuple[str, str | None], ...]
    detail_source: str
    counts: tuple[tuple[str, str], ...] = field(default=())


def _payload_at(payload: dict[str, Any], path: str) -> Any:
    head, _, tail = path.partition(".")
    value = payload.get(head)
    if not tail:
        return value
    return value.get(tail) if isinstance(value, dict) else None


def _publish_at(payload: dict[str, Any], path: str, value: Any) -> None:
    head, _, tail = path.partition(".")
    if not tail:
        payload[head] = value
        return
    nested = payload.setdefault(head, {})
    if not isinstance(nested, dict):  # pragma: no cover - the table names dicts
        raise TypeError(f"cannot publish {path}: {head} is not a mapping")
    nested[tail] = value


def doctor_detail_sources(report: dict[str, Any]) -> dict[str, Any]:
    """Where each section keeps its own error text, keyed by section name.

    The CLI's fourth roster, derived rather than re-typed: a section added to
    the table renders its detail line the day it is added, and a section that
    keeps its error somewhere unusual says so once, in the table.
    """

    return {
        section.name: _payload_at(report, section.detail_source)
        for section in DOCTOR_SECTIONS
    }


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

    **Every roster here is derived from :data:`DOCTOR_SECTIONS`.** Which
    sections exist, what each contributes to ``finding_counts``, and where each
    report lands in the payload are declared once in that table; this function
    only spends it.
    """

    ref = now()
    context = _DoctorProbeContext(
        fix=bool(fix),
        dry_run=bool(dry_run),
        worktree_min_age_seconds=max(0, int(worktree_min_age_seconds or 0)),
        include_worktrees=bool(include_worktrees),
        event_log=event_log or EventLog(),
        snapshot_builder=snapshot_builder or build_snapshot,
    )

    reports = {section.name: section.probe(context) for section in DOCTOR_SECTIONS}
    section_health = {
        section.name: reports[section.name].get("health", HEALTH_UNKNOWN)
        for section in DOCTOR_SECTIONS
    }
    # A count is an OBSERVATION. When the probe for a class did not run, the
    # honest count is ``None`` ("not observed"), never ``0`` ("observed none") —
    # a zero here is what sends an investigator hunting a defect class the
    # doctor never actually looked at. The rule is applied HERE, once, for every
    # count in the table: it used to be re-typed per entry, which is a rule
    # copied six times and free to be forgotten on the seventh.
    finding_counts: dict[str, Any] = {}
    for section in DOCTOR_SECTIONS:
        unexamined_section = section_health[section.name] == HEALTH_UNKNOWN
        for count_name, list_key in section.counts:
            finding_counts[count_name] = (
                None
                if unexamined_section
                else len(reports[section.name].get(list_key) or [])
            )
    defective = sorted(k for k, v in section_health.items() if v == HEALTH_DEFECT)
    unexamined = sorted(k for k, v in section_health.items() if v == HEALTH_UNKNOWN)
    worktrees = reports["orphan_worktrees"]
    repairs = {
        "worktrees_reaped": (
            [item.get("worktree") for item in (worktrees.get("reaped") or []) if item.get("worktree")]
            if fix and not dry_run
            else []
        ),
        "dry_run": bool(dry_run),
    }
    payload: dict[str, Any] = {
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
        # 7: ``findings.placement_census.duplicate_placements`` is new (H-H8) —
        # item ids held by more than one live actor, with
        # ``summary.finding_counts`` gaining ``duplicate_placements``. No new
        # section again, and the census's health absorbs it at ``notice``
        # EXCEPT for the ``same_instance`` reason, which is a defect.
        "schema_version": 7,
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
        # Seeded empty and filled from the table below, so the sections that
        # publish here need no second listing.
        "findings": {},
    }
    for section in DOCTOR_SECTIONS:
        report = reports[section.name]
        for destination, key in section.publish:
            _publish_at(payload, destination, report if key is None else report.get(key))
    payload["repairs"] = repairs
    return payload


def _worktree_report(context: _DoctorProbeContext) -> dict[str, Any]:
    """Orphan-worktree sweep, with a failed sweep reported as unexamined.

    The sweep shells out to git across every registered worktree, so it is I/O
    that can fail (a deleted checkout, a locked index, a git that is not on
    PATH). It ran unguarded, which meant a failure either crashed the whole
    doctor or — worse, once wrapped naively — would have read as "no orphans".

    ``include_worktrees=False`` is the caller's own choice not to scan, so it
    reports ``ok`` + ``skipped`` rather than ``unknown``: nothing failed to be
    observed, the observation was declined.
    """

    if not context.include_worktrees:
        return {
            "reaped": [],
            "kept": [],
            "dry_run": True,
            "skipped": "worktree_scan_disabled",
            "health": HEALTH_OK,
        }
    dry_run = not context.fix or context.dry_run
    try:
        report = dict(
            reap_orphan_worktrees(
                min_age_seconds=context.worktree_min_age_seconds,
                event_log=context.event_log,
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


def _event_log_report(_context: _DoctorProbeContext | None = None) -> dict[str, Any]:
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


def _persona_binding_report(_context: _DoctorProbeContext | None = None) -> dict[str, Any]:
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


def _root_config_misplacement_report(_context: _DoctorProbeContext | None = None) -> dict[str, Any]:
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


def _model_authority_report(_context: _DoctorProbeContext | None = None) -> dict[str, Any]:
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


def _snapshot_null_id_report(context: _DoctorProbeContext) -> dict[str, Any]:
    """Null-id rows observed in the frame, plus whether the frame built at all.

    A build CRASH used to be returned as one ``snapshot_null_id_rows`` defect —
    the counter naming a defect class nobody observed, sending an investigator
    hunting null-id rows in a frame that never existed. The two facts are now
    separate: the defect list only ever holds rows actually inspected, and the
    build outcome rides its own ``snapshot_build`` section as ``unknown`` + the
    error, which clears the report's ``ok`` without inventing a finding.

    They are two PAYLOAD keys from this one probe — ``rows`` and ``build`` —
    published by the table, which is why the section still contributes exactly
    one ``health`` and one count.
    """

    try:
        snapshot = context.snapshot_builder()
    except Exception as exc:
        build = {
            "health": HEALTH_UNKNOWN,
            "observed": False,
            "error": _error_text(exc),
        }
        return {"health": HEALTH_UNKNOWN, "rows": [], "build": build}
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
    health = HEALTH_DEFECT if defects else HEALTH_OK
    return {
        "health": health,
        "rows": defects,
        "build": {"health": health, "observed": True},
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


def _census_instance_key(raw_id: Any, *, persona_id: Any = None) -> str:
    """The one spelling BOTH sides of the join are compared in.

    ``OfficeStore.upsert_actor`` stores ``persona_instance_id`` through
    ``canonical_persona_instance_id`` (via ``_canonical_actor_key``), so a
    roster row still carrying a legacy spelling would read as an orphan against
    its own actor if the two sides were compared raw. Routing BOTH sides through
    the single derivation authority is what keeps this census from inventing
    findings out of the id drift that ``persona instance reconcile`` exists to
    fold.

    **Both sides means both.** Until H-H11 only the roster side was routed
    through here, and the actor side was read raw off the file — which is not
    the same set of ids, because ``upsert_actor`` is not the only writer: the
    realm pull's ``adopt_remote_actor`` writes a PEER's row verbatim, legacy
    spelling and all, and that actor then reported as an ``orphan_actor``
    against a roster row it names correctly. A defect invented out of a
    spelling, in the section whose whole contract is not to do that.

    ``""`` for an id that is absent or unreadable — a class-keyed actor, which
    is out of the join by construction rather than by omission.
    """

    from .persona_assignments import canonical_persona_instance_id

    raw = str(raw_id or "").strip()
    if not raw:
        return ""
    canonical = canonical_persona_instance_id(raw, persona_id=persona_id)
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
        # Same rule again, and for a reason of its own: "no OTHER live actor
        # holds this id" is the absence a duplicate sweep asserts, and a file
        # that would not open is one that might be holding it.
        "duplicate_placements": None,
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


# ── duplicate placements: one item id, two live actor rows (H-H8) ────────────
#
# The residual the two write fences leave between them, stated in doc 06's
# write-verbs section: the class-key fence guards class-keyed payloads only (an
# instance-keyed write "IS the migration's shape"), and the duplicate-desk fence
# counts DISTINCT desk ids per persona, so an instance-keyed write claiming an
# item id another live actor already holds passes both. Until now nothing
# server-side could see it — the census joins on ``persona_instance_id`` and
# never opened ``actor.items``, so both holders counted as ``placed`` and the
# section reported ``ok``, leaving the launcher's render-time ``duplicate_desk``
# warning as the only detector.
#
# This is a READER and deliberately not a third fence: doc 06's D6 ruling says
# the persona-keyed desk fence must NOT be re-keyed toward instances, because
# desks are a placeholder for standalone artifacts and the invariant should stop
# existing rather than move. A census row moves no fence.

#: Every holder is bound to the SAME live-ish instance — one instance's
#: placement claimed by two live actor rows. A DEFECT: nothing legitimate mints
#: it, and it is the two-fences residual in the shape that actually costs
#: something (the realm-pulled actor file written under a peer's actor key, or a
#: legacy id spelling that canonicalizes onto a key already held).
DUPLICATE_PLACEMENT_SAME_INSTANCE = "same_instance"

#: The holders are different instances. Reported, never a defect: D6 rules that
#: "duplicate desks are fine and only a duplicate on the SAME INSTANCE is not —
#: it's an instantiated system", and item ids are minted persona-scoped
#: (``<persona>_<kind>``), so two instances of one persona each authoring a desk
#: produce exactly this. Calling it a defect would re-key this predicate to the
#: persona, which is the move the ruling forbids.
DUPLICATE_PLACEMENT_CROSS_INSTANCE = "cross_instance"

#: At least one holder is CLASS-KEYED (no instance binding). Reported, never a
#: defect: this is the class→instance re-key migration's own transient —
#: ``scripts/office_actor_rekey_to_instance.py::_apply`` mints the instance-keyed
#: actor with the class-keyed actor's items copied verbatim and only then
#: archives the old key, so both rows briefly claim every id. A census that
#: called it a defect would report the one operator script whose whole job is to
#: move a placement.
DUPLICATE_PLACEMENT_UNBOUND_HOLDER = "unbound_holder"

DUPLICATE_PLACEMENT_REASONS = (
    DUPLICATE_PLACEMENT_SAME_INSTANCE,
    DUPLICATE_PLACEMENT_CROSS_INSTANCE,
    DUPLICATE_PLACEMENT_UNBOUND_HOLDER,
)


def _duplicate_placement_reason(bindings: tuple[str, ...]) -> str:
    """Which duplicate this is, from the holders' instance bindings alone.

    ``bindings`` is one entry per HOLDER, ``""`` for a class-keyed actor. Pure,
    total over its input, and — like the desk classifier — the part worth unit
    testing, so it must not be reachable only through a filesystem fixture.

    THE ORDER IS THE DESIGN: the unbound arm is asked first, because a
    class-keyed holder beside an instance-keyed one is also, trivially, a set of
    bindings that is not all-equal, so any other order would file the re-key
    migration's legal transient under ``cross_instance`` and lose the one
    distinction an operator acts on.
    """

    if any(not binding for binding in bindings):
        return DUPLICATE_PLACEMENT_UNBOUND_HOLDER
    if len(set(bindings)) == 1:
        return DUPLICATE_PLACEMENT_SAME_INSTANCE
    return DUPLICATE_PLACEMENT_CROSS_INSTANCE


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


def _placement_census_report(_context: _DoctorProbeContext | None = None) -> dict[str, Any]:
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

    * ``duplicate_placements`` (H-H8) — an ITEM id held by more than one live
      actor, with every holder named and one of the three
      ``DUPLICATE_PLACEMENT_*`` reasons. A defect only for ``same_instance``;
      the other two are reported at ``notice``, and the reasons are where the
      D6 ruling is spent. The join above could not see any of them: it is
      actor-level, so both holders counted as ``placed`` and the section
      reported ``ok``.

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

    live_rows = {
        _census_instance_key(row.id, persona_id=row.persona_id): row
        for row in roster.instances
    }

    store = OfficeStore()
    try:
        workspace_ids = list(store.list_workspaces())
    except Exception as exc:
        return _census_unknown(_error_text(exc))

    placed: list[dict[str, Any]] = []
    orphan_actors: list[dict[str, Any]] = []
    desk_litter: list[dict[str, Any]] = []
    duplicate_placements: list[dict[str, Any]] = []
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
        # ONE canonical binding per live actor, resolved once here and read by
        # every sweep below, so the actor side of every comparison in this
        # workspace is spelled the way the roster side is. See
        # :func:`_census_instance_key` for what a raw read cost.
        live_actor_bindings = [
            (actor, _census_instance_key(actor.persona_instance_id, persona_id=actor.persona_id))
            for actor in scan.actors
            if actor.state != "archived"
        ]
        for actor, instance_id in live_actor_bindings:
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
        for actor, binding in live_actor_bindings:
            for item in actor.items or ():
                if getattr(item, "kind", None) != "agent":
                    continue
                persona = _normalize_persona_id(item.persona_id) or _normalize_persona_id(
                    actor.persona_id
                )
                if persona:
                    agent_bindings.setdefault(persona, []).append(binding)

        ws_litter: list[dict[str, Any]] = []
        for actor, binding in live_actor_bindings:
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

        # The duplicate-placement sweep (H-H8), over the SAME live actors of the
        # SAME fully-read world. This is the pass that opens ``actor.items`` for
        # the JOIN's sake rather than the desk sweep's: the join above is
        # actor-level, so two live actors holding one item id were both counted
        # ``placed`` and the section reported ``ok``.
        #
        # Distinct HOLDERS per id, which is the mirror of the write fence's
        # "distinct ids per persona": one actor listing an id twice is one
        # holder, because the fault named here is two ROWS claiming one
        # placement.
        holders: dict[str, list[dict[str, Any]]] = {}
        for actor, binding in live_actor_bindings:
            seen_in_actor: set[str] = set()
            for item in actor.items or ():
                item_id = str(getattr(item, "item_id", "") or "").strip()
                if not item_id or item_id in seen_in_actor:
                    continue
                seen_in_actor.add(item_id)
                holders.setdefault(item_id, []).append(
                    {
                        "actor_key": actor.actor_key,
                        "persona_instance_id": binding or None,
                        "kind": str(getattr(item, "kind", "") or ""),
                    }
                )

        ws_duplicates: list[dict[str, Any]] = []
        for item_id, rows in sorted(holders.items()):
            if len(rows) < 2:
                continue
            ws_duplicates.append(
                {
                    "workspace_id": workspace_id,
                    "item_id": item_id,
                    "kinds": sorted({row["kind"] for row in rows if row["kind"]}),
                    "holders": rows,
                    "reason": _duplicate_placement_reason(
                        tuple(str(row["persona_instance_id"] or "") for row in rows)
                    ),
                }
            )

        placed.extend(ws_placed)
        orphan_actors.extend(ws_orphans)
        desk_litter.extend(ws_litter)
        duplicate_placements.extend(ws_duplicates)
        per_workspace[workspace_id] = {
            "placed": len(ws_placed),
            "unplaced_rows": [],
            "orphan_actors": ws_orphans,
            "desk_litter": ws_litter,
            "duplicate_placements": ws_duplicates,
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

    same_instance_duplicates = [
        row
        for row in duplicate_placements
        if row.get("reason") == DUPLICATE_PLACEMENT_SAME_INSTANCE
    ]
    if orphan_actors or same_instance_duplicates:
        health = HEALTH_DEFECT
    elif unplaced_rows or desk_litter or duplicate_placements:
        # Litter raises the census to ``notice`` and NEVER past it. An orphan
        # actor is a defect because it renders as an agent nothing can message;
        # a litter desk renders as exactly what it is — a desk. Promoting it
        # would turn ``needs_fix`` on for a store whose only fault is furniture,
        # and the doctor's whole contract is that its flags mean something.
        #
        # A duplicate placement splits on the SAME line, and the D6 ruling is
        # where the line comes from — see :func:`_duplicate_placement_reason`.
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
        "duplicate_placements": duplicate_placements,
        "workspaces": per_workspace,
        # A4. The orphan half used to read "retiring or re-creating its agent",
        # and for the orphan this census reports most often that names the ONE
        # verb that cannot work. A realm-pulled placement is born orphaned —
        # office actors sync, persona instances are per-install by ruling — so
        # its instance has never existed here, and `agent retire` refuses
        # `not_found` terminally. The refusal is correct; prescribing it was
        # not. Both repairs are named, keyed on the fact that decides between
        # them (does this install hold the instance), never on the id's shape.
        #
        # AX7. The pulled-orphan repair names `--local-only`, and that is not a
        # detail. A doctor remediation is DIAGNOSTIC intent: the operator asked
        # what is wrong with THIS install's projection, not to delete a
        # placement on every machine in the realm. Prescribing the tombstoning
        # form would have this report quietly authoring realm-wide deletes on
        # the operator's behalf — the authored form stays available, and stays
        # for the moment the operator actually means it.
        "remediation": (
            "an orphan actor whose instance this install still holds is cleared "
            "by retiring or re-creating its agent; one whose instance this "
            "install never held — a realm-pulled placement, whose instance "
            "stayed on the peer — has nothing to retire, and is evicted from "
            "this install with `harness office actor-remove --workspace <ws> "
            "--actor <key> --local-only` (drop --local-only only if you mean to "
            "delete the placement realm-wide, which is what the launcher's own "
            "delete does); an unplaced row is either awaiting a "
            "placement or is the roster-only recovery door working as designed; "
            "a desk_litter row reading desk_kind_agent_binding wants a re-place, "
            "and the other three want a reap; a duplicate_placements row reading "
            "same_instance is one instance's placement claimed by two live actor "
            "rows — remove or re-place one holder, whose actor_key is named"
        ),
    }
    return report


# ── THE table ─────────────────────────────────────────────────────────────────
#
# Adding a section is one row here and nothing else: ``summary.section_health``,
# ``summary.finding_counts``, the payload placement, and the CLI's per-section
# detail line are all derived from it (see :class:`DoctorSection`). Order is the
# operator-facing order of ``finding_counts``, so keep a new section where its
# findings read naturally rather than appending by habit.
DOCTOR_SECTIONS: tuple[DoctorSection, ...] = (
    DoctorSection(
        name="orphan_worktrees",
        probe=_worktree_report,
        publish=(("findings.orphan_worktrees", None),),
        detail_source="findings.orphan_worktrees",
        counts=(("orphan_worktrees", "reaped"),),
    ),
    DoctorSection(
        name="snapshot_null_id_rows",
        probe=_snapshot_null_id_report,
        publish=(
            ("findings.snapshot_null_id_rows", "rows"),
            ("findings.snapshot_build", "build"),
        ),
        # A bare list of rows carries no error text; the build outcome does.
        detail_source="findings.snapshot_build",
        counts=(("snapshot_null_id_rows", "rows"),),
    ),
    DoctorSection(
        name="event_log",
        probe=_event_log_report,
        publish=(("findings.event_log", None),),
        detail_source="findings.event_log",
    ),
    # ``model_authority`` and ``persona_binding`` publish at the payload ROOT
    # rather than under ``findings``. That predates the derived verdict and is
    # kept because both are read by name off the JSON by operator tooling; the
    # table is where the exception is stated instead of being a thing you had to
    # already know.
    DoctorSection(
        name="model_authority",
        probe=_model_authority_report,
        publish=(("model_authority", None),),
        detail_source="model_authority",
    ),
    DoctorSection(
        name="persona_binding",
        probe=_persona_binding_report,
        publish=(("persona_binding", None),),
        detail_source="persona_binding",
    ),
    DoctorSection(
        name="root_config_misplacement",
        probe=_root_config_misplacement_report,
        publish=(("findings.root_config_misplacement", None),),
        detail_source="findings.root_config_misplacement",
        counts=(("misplaced_root_only_keys", "misplaced"),),
    ),
    # The census contributes FOUR counts because they are four different
    # verdicts: an orphan actor is a defect, an unplaced row is a legal state of
    # a supported door, a litter desk is authored furniture standing where its
    # agent no longer is, and a duplicate placement is two live actors claiming
    # one item id. Folding them into one number would make the doctor's headline
    # count climb every time the roster-only recovery door is used correctly.
    DoctorSection(
        name="placement_census",
        probe=_placement_census_report,
        publish=(("findings.placement_census", None),),
        detail_source="findings.placement_census",
        counts=(
            ("orphan_actors", "orphan_actors"),
            ("unplaced_rows", "unplaced_rows"),
            ("desk_litter", "desk_litter"),
            ("duplicate_placements", "duplicate_placements"),
        ),
    ),
)
