"""The SKILL family's half of the three-way realm-sync model.

Added 2026-09-12. Authority:
``EterniaLauncher/docs/mission_control/planned/held-skill-publish-direction.md``
§4 (diagnosis in §1–2). Sibling modules — ``board_sync``, ``office_sync``,
``persona_config_sync``, ``persona_instance_sync``, ``flow_graph_sync``,
``level_sync``, ``profile_artifact_sync`` — each own one family's baseline
sidecar and one applier; this is the skill family's, and it is the LAST family
to get one.

**What was structurally wrong before it existed.** Every other family decides
through the ONE shared classifier :func:`sync_merge.classify_three_way_pull`
against a never-synced baseline: local vs baseline vs remote. The skill family
decided through :func:`skill_promotion.classify_promotion`, a TWO-way compare of
canonical vs inbox, so:

1. it had no DIRECTION — "I edited it" and "they edited it" were both
   ``hold_divergent``, which is why the launcher's SKILLS HELD card could only
   offer "adopt theirs";
2. a realm-side update was a HOLD for every member that already had the skill,
   including members who had never touched it (other families fast-forward that
   case);
3. a publish of a real local edit left the hold standing, because nothing
   recorded a baseline and nothing refreshed the inbox;
4. a local skill edit lit nothing — ``store_drift`` had no skill family, so the
   sheet said "In sync" while the canonical copy differed from the realm's.

This module holds the baseline sidecar (1–3), the ONE classifier both the pull
loop and the held-status read go through (so status and pull cannot disagree),
and the operator's resolve verb. The drift family (4) and the appliers live in
``realm_sync`` / ``realm_revert`` beside their siblings.

**The hash is the SYNC hash** — :func:`skill_promotion.skill_package_sync_hash`,
EOL-agnostic. See its docstring for why the byte hash could not answer this
question.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The baseline/selector key prefix for this family. A skill's key is
#: ``skill::<slug>`` — unambiguous against the profile-file family's
#: ``<profile>:<path>`` keys (which never carry a doubled colon), which is what
#: lets ``realm sync resolve --key`` dispatch on the prefix alone, and what makes
#: the drift row's own ``FAMILY:CONTAINER:KEY`` spec (``skill::<slug>``, empty
#: container) the same token.
SKILL_KEY_PREFIX = "skill::"

#: The pull buckets this family reports, in the order the summary lists them.
#: ``converged`` / ``adopted`` / ``held`` predate the three-way model; ``updated``
#: and ``kept_local`` are what the model adds — the two verdicts the two-way
#: compare could not express.
BUCKET_CONVERGED = "converged"
BUCKET_ADOPTED = "adopted"
BUCKET_UPDATED = "updated"
BUCKET_KEPT_LOCAL = "kept_local"
BUCKET_HELD = "held"
#: Not a three-way verdict: the promotion door's structural refusals (invalid
#: slug, a canonical slot occupied by a non-package, a categorized child under a
#: bare skill). Kept OUT of the held set for the reason ``SkillSyncSummary``
#: already states — ``skills_drift`` is the set an operator RESOLVES.
BUCKET_REFUSED = "refused"

#: ``classify_three_way_pull`` reason → bucket. Total over the reasons this
#: family can reach; the unreachable ones are mapped anyway, and deliberately to
#: ``held``, because ``held`` is the only bucket that writes nothing.
#:
#: ``remote_removed`` / ``edit_vs_remove`` cannot occur inside the pull loop: the
#: loop iterates the MIRRORED INBOX, so a package absent from the subtree was
#: already pruned before classification, and the local canonical is never
#: archived on de-selection (tombstones are the delete lane). ``absent_both`` is
#: unreachable for the same reason. ``archived_local`` / ``archive_vs_edit`` need
#: a ``locally_archived`` ledger this family does not have.
_REASON_BUCKETS = {
    "unchanged": BUCKET_CONVERGED,
    "converged": BUCKET_CONVERGED,
    "adopt_remote": BUCKET_ADOPTED,
    "take_remote": BUCKET_UPDATED,
    "unpublished": BUCKET_KEPT_LOCAL,
    "new_local": BUCKET_KEPT_LOCAL,
    "both_changed": BUCKET_HELD,
    "new_both": BUCKET_HELD,
}

#: The buckets whose baseline entry advances to the REMOTE hash on a pull. A
#: ``kept_local`` package deliberately does NOT advance: its baseline is the
#: still-true statement "the realm's copy has not moved since I last saw it", and
#: advancing it would erase the operator's unpublished edit from the drift
#: accounting.
BASELINE_ADVANCING_BUCKETS = frozenset({BUCKET_CONVERGED, BUCKET_ADOPTED, BUCKET_UPDATED})


def skill_baseline_key(slug: str) -> str:
    """``skill::<slug>`` — this family's baseline/selector key for one package."""

    return f"{SKILL_KEY_PREFIX}{slug}"


def split_skill_baseline_key(key: str) -> str | None:
    """The slug out of a ``skill::<slug>`` key, or ``None`` for anything else.

    Used to tell this family's keys from the profile-file family's at the CLI
    door, and to walk the baseline's own entries in the drift scan.
    """

    text = str(key or "")
    if not text.startswith(SKILL_KEY_PREFIX):
        return None
    slug = text[len(SKILL_KEY_PREFIX):].strip()
    return slug or None


# --- baseline sidecar (never synced, never published) ------------------------


def read_skill_baseline(realm_id: str) -> dict[str, str]:
    import json

    from . import paths

    path = paths.skill_baseline_path(realm_id)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    return {str(k): str(v) for k, v in entries.items()} if isinstance(entries, dict) else {}


def write_skill_baseline(realm_id: str, entries: dict[str, str]) -> None:
    from utils import atomic_json_write

    from . import paths

    atomic_json_write(
        paths.skill_baseline_path(realm_id),
        {"schema_version": 1, "entries": entries},
        indent=2,
        sort_keys=True,
    )


def update_skill_baseline_after_publish(realm_id: str, published: dict[str, str]) -> None:
    """Record the published package hashes as the new baseline.

    ``published`` maps slug → sync hash for every package THIS publish shipped.
    The sibling families' ``update_*_baseline_after_publish`` all exist for one
    reason — so my own publish never comes back as a pull hold — and this one
    carries a second: without it a local edit stayed a phantom hold until the
    next pull, which is defect (3) in this module's header.

    **Entries for packages this publish did not ship are DROPPED**, which is
    where this differs from the profile-file family's ``dict.update``. A publish
    walk is the complete statement of what the realm now carries from this
    member, so a slug that was de-selected, tombstoned or deleted locally has no
    baseline to keep: leaving one would report a ``removed`` drift row for a
    package the realm was never asked to carry, and offer a revert that would
    reinstall it.
    """

    write_skill_baseline(realm_id, {skill_baseline_key(slug): value for slug, value in published.items()})


def seed_converged_skill_baselines(
    baseline: dict[str, str], pairs: dict[str, tuple[str | None, str | None]]
) -> list[str]:
    """Record a missing baseline entry ONLY where local and inbox already agree.

    The migration rule for installs that predate the sidecar (2026-09-12), and
    deliberately the NARROW one. ``pairs`` maps slug → ``(local_hash,
    inbox_hash)``; for every slug the ``baseline`` does not name whose two hashes
    are equal and present, that hash is recorded in place (the caller decides
    whether to persist). Returns the seeded slugs.

    Why not "the inbox stands in for the baseline" outright: after a pull that
    HELD a package the inbox carries a version this member never accepted, and
    seeding from it would read an untouched-but-stale local copy as *my edit* —
    ``kept_local``, a ``changed`` drift row, and a Publish that regresses the
    realm's newer copy. Measured 2026-09-12 as seven reds across the hold and
    resolve suites the moment the broad rule was tried. Convergence is the one
    fact both sides have already agreed on, so recording it is idempotent and
    cannot invent a direction; where they disagree and nothing is recorded, the
    honest verdict stays ``held`` and the operator's resolve records the baseline.

    The live case it exists for: the operator's own realm had an inbox and no
    sidecar, and after the EOL fix local and inbox agreed — so the very next
    status read records that agreement, and their first local edit after it reads
    ``changed`` (push or revert) instead of ``held``.
    """

    seeded: list[str] = []
    for slug, (local_hash, inbox_hash) in pairs.items():
        key = skill_baseline_key(slug)
        if key in baseline or not local_hash or local_hash != inbox_hash:
            continue
        baseline[key] = inbox_hash
        seeded.append(slug)
    return sorted(seeded)


def record_converged_skill_baselines(realm_id: str, inbox_dir: Path) -> list[str]:
    """Persist :func:`seed_converged_skill_baselines` over ``inbox_dir`` against
    the canonical root — the STATUS read's door, and the pull's PRE-mirror door.

    Writes the sidecar only when something was seeded. Hashes are the sync hash
    on both sides, so a CRLF working copy of an LF inbox package counts as
    agreement (the phantom the operator measured).
    """

    from hermes_constants import get_shared_skills_dir

    from .skill_promotion import _iter_packages, skill_package_sync_hash

    root = get_shared_skills_dir()
    pairs: dict[str, tuple[str | None, str | None]] = {}
    for slug, package_dir in _iter_packages(inbox_dir):
        canonical = root.joinpath(*slug.split("/"))
        local_hash = (
            skill_package_sync_hash(canonical) if (canonical / "SKILL.md").is_file() else None
        )
        pairs[slug] = (local_hash, skill_package_sync_hash(package_dir))
    baseline = read_skill_baseline(realm_id)
    seeded = seed_converged_skill_baselines(baseline, pairs)
    if seeded:
        write_skill_baseline(realm_id, baseline)
    return seeded


# --- the ONE classifier -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkillInboxDecision:
    """One inbox package's three-way verdict.

    ``bucket`` is the pull summary list this package lands in; ``reason`` is the
    shared classifier's own reason string, kept so a receipt can say WHY rather
    than only WHERE.
    """

    skill: str
    bucket: str
    reason: str
    local_hash: str | None
    remote_hash: str | None
    baseline_hash: str | None

    @property
    def held(self) -> bool:
        return self.bucket == BUCKET_HELD


def classify_inbox_package(
    realm_id: str,
    slug: str,
    source_dir: Path,
    *,
    plan: Any | None = None,
    baseline: dict[str, str] | None = None,
) -> SkillInboxDecision:
    """THE skill-family pull decision for one inbox package.

    ONE function, used by BOTH the pull loop (``apply_skill_inbox_pull``) and the
    held-status read (``_held_skill_packages_for_realm`` via
    ``list_inbox_packages``), because the sheet's SKILLS HELD card and the pull's
    own result must not be able to disagree — they did, for the three months the
    family had no baseline and publish left a stale inbox behind.

    ``plan`` is an already-computed :class:`skill_promotion.PromotionPlan` for the
    same package; passing it avoids hashing the package twice, and its
    ``source_hash`` / ``canonical_hash`` ARE the sync hashes (``_package_hash`` is
    EOL-agnostic since 2026-09-12). ``baseline`` likewise avoids re-reading the
    sidecar per package in a loop.

    A structurally refused plan classifies :data:`BUCKET_REFUSED` and no further:
    its ``canonical_hash`` is ``None`` even where a canonical path exists, so
    feeding it to the three-way classifier would read as "no local copy" and
    propose an adopt over something the write door is about to refuse anyway.
    """

    from .skill_promotion import classify_promotion
    from .sync_merge import classify_three_way_pull

    if plan is None:
        plan = classify_promotion(slug, source_dir)
    baseline_map = read_skill_baseline(realm_id) if baseline is None else baseline
    baseline_hash = baseline_map.get(skill_baseline_key(slug))

    if plan.action == "refuse_invalid":
        return SkillInboxDecision(
            skill=slug,
            bucket=BUCKET_REFUSED,
            reason=plan.reason,
            local_hash=plan.canonical_hash,
            remote_hash=plan.source_hash,
            baseline_hash=baseline_hash,
        )

    decision = classify_three_way_pull(plan.canonical_hash, plan.source_hash, baseline_hash)
    return SkillInboxDecision(
        skill=slug,
        bucket=_REASON_BUCKETS.get(decision.reason, BUCKET_HELD),
        reason=decision.reason,
        local_hash=plan.canonical_hash,
        remote_hash=plan.source_hash,
        baseline_hash=baseline_hash,
    )


# --- operator resolution ----------------------------------------------------


class SkillResolveError(RuntimeError):
    """Typed failure of :func:`resolve_held_skill`. Shaped exactly like
    ``profile_artifact_sync.ProfileArtifactResolveError`` so the CLI's two
    resolve arms emit the same error envelope."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def resolve_held_skill(
    realm_id: str,
    key: str,
    *,
    take: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Resolve ONE held skill package — the operator choosing a direction.

    ``key`` is ``skill::<slug>``. **Both takes record the realm's current hash as
    the new baseline**, which is the profile-file lane's exact semantics ("I have
    seen the realm's version") and the thing that stops a hold re-reporting
    forever:

    - ``--take remote`` installs the inbox copy over canonical through the ONE
      guarded door (the previous canonical is ARCHIVED, never deleted), so the
      next pull is ``unchanged`` → ``converged``.
    - ``--take local`` writes NOTHING to the package. The baseline advance is the
      whole act: the next status then classifies the package ``changed``
      (unpublished drift, with a revert row) instead of held, and the next
      publish ships it. That is "publish mine" — the launcher runs this verb and
      then the ONE credentialed publish path, rather than a second per-skill
      publish verb.

    ``dry_run`` writes NOTHING — not the package, not the baseline.
    """

    from .skill_promotion import (
        classify_promotion,
        execute_promotion,
        realm_inbox_dir,
        skill_package_sync_hash,
    )

    if take not in ("local", "remote"):
        raise SkillResolveError("invalid_request", "take must be 'local' or 'remote'")
    slug = split_skill_baseline_key(key)
    if slug is None:
        raise SkillResolveError("invalid_request", f"not a skill entity key: {key}")

    source_dir = realm_inbox_dir(realm_id).joinpath(*slug.split("/"))
    if not (source_dir / "SKILL.md").is_file():
        raise SkillResolveError(
            "not_found",
            f"the realm does not publish skill {slug!r} into this member's inbox; "
            "nothing to resolve against (pull first)",
        )

    plan = classify_promotion(slug, source_dir)
    if plan.action == "refuse_invalid":
        raise SkillResolveError("invalid_request", plan.reason)
    remote_hash = plan.source_hash or skill_package_sync_hash(source_dir)
    local_hash = plan.canonical_hash

    archived_previous_to: str | None = None
    if not dry_run:
        if take == "remote" and plan.action != "noop_identical":
            result = execute_promotion(
                plan,
                source={"kind": "realm", "realm_id": realm_id},
                adopt_divergent=True,
                move_source=False,
            )
            if result.action != "promoted":
                # The door refused (installer-owned, or the canonical slot moved
                # under us). Never report ``changed`` for a write that did not
                # happen, and never advance the baseline on it: the hold is real
                # and stays visible.
                raise SkillResolveError(
                    result.reason_code or "skill_promotion_refused", result.reason
                )
            archived_previous_to = (
                str(result.archived_previous_to) if result.archived_previous_to else None
            )
        baseline = read_skill_baseline(realm_id)
        baseline[skill_baseline_key(slug)] = remote_hash
        write_skill_baseline(realm_id, baseline)

    return {
        "id": skill_baseline_key(slug),
        "skill": slug,
        "take": take,
        "changed": take == "remote" and local_hash != remote_hash,
        "local_hash": local_hash,
        "remote_hash": remote_hash,
        "archived_previous_to": archived_previous_to,
    }
