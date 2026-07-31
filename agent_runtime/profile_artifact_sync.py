"""Merge-on-pull for the per-profile FILE artifacts a persona carries.

``c905569c1`` retired the wholesale-overwrite defect for ONE artifact kind
(``persona_config``). Four more mapped to real destinations in
``realm_sync._destination_for_sync_path`` and were still overwritten blind by the
generic write-loop in ``pull_realm_sync`` — no merge, no baseline, no hold, no
accounting:

===================  =========================================================
``profile_memory``   ``<profile_home>/memories/MEMORY.md``
``core_context``     ``<profile_home>/{AGENTS.md,CLAUDE.md,GEMINI.md}``
``system_prompt``    ``<profile_home>/personas/<basename>``
``soul_overlay``     ``<profile_home>/personas/<basename>``
===================  =========================================================

**A member's accumulated ``MEMORY.md`` was destroyed by a realm pull.** That is
the most destructive payload in the system and the reason this module exists.

Two adjacent defects are fixed in the same pass:

(a) **Prompt destinations were keyed by the filename only.**
    ``profiles/<p>/personas/<persona>/system_prompt/<file>`` mapped to
    ``<profile_home>/personas/<basename>`` — the persona was read out of the
    published path and then dropped. Two personas on one profile with
    same-named prompt files clobbered each other (Office realm-sync plan §5.1,
    ``docs/mission_control/OFFICE_LAYOUT_REALM_SYNC_PLAN_2026-07-17.md`` in the
    launcher repo, named this; W-H4 fixed profile scoping but not the filename
    keying). Worse, the basename-keyed destination did not even round-trip: a
    publisher whose ``soul_overlay_path`` is ``personas/neko/SOUL.md`` published
    a file the member wrote to ``personas/SOUL.md`` — a path the member's own
    persona definition does not point at, so the pulled file was **orphaned and
    dead on arrival**.

(b) **Pull-side secret scanning had a hole.** ``_assert_no_secret_artifacts``
    only covers the artifacts the generic loop maps; every specialized applier
    bypassed it. Closed by :mod:`agent_runtime.sync_admission`, which this lane
    and the board / office / skill lanes all now run.

Design
------

**One shape, not a second one.** Decisions come from the SHARED
``sync_merge.classify_three_way_pull`` against a never-synced baseline sidecar,
with the same adopt / converge / kept_local / held / retained / refused
vocabulary and per-entity refusal isolation the persona-config lane uses.

**Member-accumulated state is never overwritten.** ``MEMORY.md`` and the
core-context files are not definitions — they are what a member's agent
accumulated. The semantics are adopt-if-absent, converge-if-identical, and
**hold on divergence**: a member's copy that differs from the recorded baseline
is left untouched and surfaced, forever if need be, until the operator resolves
it (``hermes harness realm sync resolve``). There is deliberately no prose
merge: markdown is not structured config and a 3-way text merge here would be
worse than a hold. :func:`_may_write` is the single invariant every write passes
through, and ``test_profile_artifact_sync.py`` sabotage-tests it.

**Published path.** The four kinds now publish under
``store/profile_files/<profile>/<profile-relative destination>`` (see
:data:`PROFILE_FILES_ROOT`), NOT under ``profiles/<p>/personas/<persona>/…``.
Two reasons, both load-bearing:

1. The published tail IS the destination, so a prompt round-trips to exactly the
   path the persona definition names — retiring the orphan class in (a).
2. An older hermes maps ``profiles/<p>/personas/<persona>/memories/MEMORY.md``
   onto ``<profile_home>/memories/MEMORY.md`` and overwrites it wholesale.
   ``store/profile_files/…`` is an unknown path to every older client
   (``_destination_for_sync_path`` → ``None`` → the artifact is skipped), so an
   old member degrades to "no profile files" instead of losing their memory.
   This is exactly the trade ``c905569c1`` made for ``store/personas.yaml``, and
   moving the path is the ONLY lever a publisher has to stop destroying an old
   member's ``MEMORY.md``. The legacy paths are deliberately NOT published in
   parallel: a dual publish would keep destroying old members AND give new
   members two sources for one destination.

Version tolerance is bidirectional and explicit — see
:func:`read_remote_profile_files`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sync_merge import PullAction, classify_three_way_pull

# --- contract ---------------------------------------------------------------

#: Root of the published profile-file family. Unknown to every older client.
PROFILE_FILES_ROOT = "store/profile_files"

#: Legacy published root (an older publisher). Read on pull, never written.
LEGACY_PROFILES_ROOT = "profiles"

KIND_PROFILE_MEMORY = "profile_memory"
KIND_CORE_CONTEXT = "core_context"
KIND_PERSONA_PROMPT = "persona_prompt"

MEMORY_DESTINATION = "memories/MEMORY.md"
CORE_CONTEXT_FILENAMES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")
PERSONA_PROMPT_DIR = "personas"

#: Bound on an untrusted remote destination depth (a realm cannot make a member
#: materialize an arbitrarily deep tree).
_MAX_DESTINATION_DEPTH = 8

#: A prompt/overlay destination must be a text document. This — not a directory
#: prefix — is what keeps the prompt lane from reaching anything dangerous in a
#: profile home: ``config.yaml``, ``.env``, ``*.db``, ``plugins/**/*.py``,
#: ``skins/*.yaml`` all fail it. Restricting prompts to ``personas/**`` instead
#: was WRONG and shipped a silent one-way loss: ``soul_overlay_path`` and
#: ``system_prompt_path`` are profile-relative to ANYWHERE in the home
#: (``soul.md`` at the root is a real, supported shape — see
#: ``realm_sync._profile_relative_file``), so publish emitted ``soul.md`` and
#: pull refused it as ``destination_not_allowed``. Caught 2026-07-25 by the
#: rebind-delta suite; ``test_publish_and_pull_agree_on_every_destination`` is
#: the standing guard that the two sides can never disagree again.
_PROMPT_SUFFIXES: frozenset[str] = frozenset({".md", ".txt"})


def classify_destination(dest_rel: str) -> str | None:
    """The artifact kind a profile-relative destination denotes, or ``None`` when
    the destination is not admissible.

    This is the whole safety story for an untrusted remote path: without it a
    realm could publish ``config.yaml`` or ``.env`` into a member's profile home
    — the exact clobber class this module retires.

    Member-accumulated state is a CLOSED set of exactly four destinations
    (``memories/MEMORY.md`` + the three core-context files at the profile root),
    which is what makes the classification unambiguous without a kind marker in
    the path. Everything else is a prompt/overlay, admitted only as a text
    document (:data:`_PROMPT_SUFFIXES`) that is not shadowing a member-state
    destination and carries no hidden/dot component.
    """

    text = str(dest_rel or "").replace("\\", "/").strip("/")
    if not text:
        return None
    parts = tuple(text.split("/"))
    if len(parts) > _MAX_DESTINATION_DEPTH:
        return None
    if any(not part or part.startswith(".") for part in parts):
        return None
    if text == MEMORY_DESTINATION:
        return KIND_PROFILE_MEMORY
    if len(parts) == 1 and parts[0] in CORE_CONTEXT_FILENAMES:
        return KIND_CORE_CONTEXT
    # A prompt may not shadow a member-state destination: ``memories/anything``
    # and a core-context filename at the root belong to the closed set above, and
    # a prompt-kind write must never be able to reach them.
    if parts[0] == "memories" or (len(parts) == 1 and parts[0] in CORE_CONTEXT_FILENAMES):
        return None
    if Path(parts[-1]).suffix.lower() in _PROMPT_SUFFIXES:
        return KIND_PERSONA_PROMPT
    return None


def entity_key(profile_token: str, dest_rel: str) -> str:
    """The merge unit: one DESTINATION on one profile home.

    Keyed on the destination — not on the persona — on purpose. Several personas
    on one profile publish the SAME ``MEMORY.md``/``AGENTS.md`` file; they must
    reconcile as one entity, not race each other. And two personas whose prompts
    would land on one path are then structurally visible as a collision rather
    than a silent last-write-wins.
    """

    return f"{profile_token}:{str(dest_rel).replace(chr(92), '/')}"


def split_entity_key(key: str) -> tuple[str, str] | None:
    profile, sep, dest = str(key or "").partition(":")
    if not sep or not profile or not dest:
        return None
    return profile, dest


def published_relative_path(profile_token: str, dest_rel: str) -> str:
    return f"{PROFILE_FILES_ROOT}/{profile_token}/{str(dest_rel).replace(chr(92), '/')}"


def content_hash(data: bytes) -> str:
    """Semantic content hash: EOL-canonical so a member's CRLF file and a
    publisher's LF artifact converge instead of conflicting forever."""

    from .realm_sync import _canonicalize_text_bytes

    return hashlib.sha256(_canonicalize_text_bytes(data)).hexdigest()


# --- baseline sidecar (never synced, never published) ------------------------


def read_profile_artifact_baseline(realm_id: str) -> dict[str, str]:
    import json

    from . import paths

    path = paths.profile_artifact_baseline_path(realm_id)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    return {str(k): str(v) for k, v in entries.items()} if isinstance(entries, dict) else {}


def write_profile_artifact_baseline(realm_id: str, entries: dict[str, str]) -> None:
    from utils import atomic_json_write

    from . import paths

    atomic_json_write(
        paths.profile_artifact_baseline_path(realm_id),
        {"schema_version": 1, "entries": entries},
        indent=2,
        sort_keys=True,
    )


def update_profile_artifact_baseline_after_publish(realm_id: str, published: dict[str, str]) -> None:
    """Record the published content hashes as the new baseline so the next pull
    sees local == baseline (no spurious conflict on my own publish)."""

    baseline = read_profile_artifact_baseline(realm_id)
    baseline.update(published)
    write_profile_artifact_baseline(realm_id, baseline)


# --- remote read -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RemoteProfileFile:
    key: str
    profile: str
    destination: str
    kind: str
    layout: str  # "profile_files" | "legacy_profiles"
    data: bytes

    @property
    def content_hash(self) -> str:
        return content_hash(self.data)


def _legacy_destination(parts: tuple[str, ...]) -> str | None:
    """Map a LEGACY published tail to a profile-relative destination.

    ``profiles/<p>/personas/<persona>/<segment>/<tail…>``; ``parts`` is
    ``(<segment>, *tail)``. Only the shapes an older publisher actually emitted
    are mapped — the basename-keyed prompt destination included, unchanged, so a
    new member pulling an old publisher lands the file exactly where the old
    member would have. Nothing new is invented for a legacy layout.
    """

    if not parts:
        return None
    segment, tail = parts[0], parts[1:]
    if segment == "memories" and tail == ("MEMORY.md",):
        return MEMORY_DESTINATION
    if segment == "context" and len(tail) == 1 and tail[0] in CORE_CONTEXT_FILENAMES:
        return tail[0]
    if segment in ("system_prompt", "soul_overlay") and len(tail) == 1:
        return f"{PERSONA_PROMPT_DIR}/{tail[0]}"
    return None


def read_remote_profile_files(
    subtree: Path,
) -> tuple[dict[str, RemoteProfileFile], list[dict[str, str]], str | None, list[str]]:
    """Profile files carried by a pulled realm subtree.

    Bidirectional version tolerance lives here:

    - **New publisher** → ``store/profile_files/<profile>/<destination>``. The
      published tail IS the destination and is re-validated against
      :func:`classify_destination` on ingest — a publisher is never trusted to
      have filtered correctly.
    - **Older publisher** → ``profiles/<p>/personas/<persona>/{memories,context,
      system_prompt,soul_overlay}/<file>``. Mapped by :func:`_legacy_destination`
      to the SAME destination the old generic write-loop used, then merged
      through the same decision table — so an old realm's files still travel,
      and the member's copies are held instead of clobbered.

    When a subtree carries both (only possible from a hand-edited repo — publish
    rebuilds the subtree wholesale), the new layout wins per key and the legacy
    entry is ignored: never a double write to one destination.

    Returns ``(entries, refusals, source, profile_tokens)``. ``source`` is
    ``None`` when the subtree carries no profile files at all — absence is "not
    published", never "removed".
    """

    from .sync_admission import Refusal, content_refusal, path_refusal

    subtree = Path(subtree)
    entries: dict[str, RemoteProfileFile] = {}
    refusals: list[Refusal] = []
    profiles: list[str] = []
    seen_new = seen_legacy = False
    collisions: set[str] = set()

    def _ingest(profile: str, dest: str, layout: str, source_path: Path) -> None:
        key = entity_key(profile, dest)
        kind = classify_destination(dest)
        if kind is None:
            refusals.append(
                Refusal(key, "destination_not_allowed", f"destination is not on the profile-file allowlist: {dest}")
            )
            return
        found = path_refusal(f"{profile}/{dest}")
        if found is not None:
            refusals.append(Refusal(key, found[0], found[1]))
            return
        try:
            data = source_path.read_bytes()
        except OSError as exc:
            refusals.append(Refusal(key, "unreadable_artifact", f"could not read published file: {exc}"))
            return
        found = content_refusal(data)
        if found is not None:
            refusals.append(Refusal(key, found[0], found[1]))
            return
        prior = entries.get(key)
        if prior is not None:
            if prior.layout == "profile_files" and layout == "legacy_profiles":
                return  # new layout wins; the legacy twin is ignored, never written
            if prior.content_hash != content_hash(data):
                collisions.add(key)
            return
        entries[key] = RemoteProfileFile(
            key=key, profile=profile, destination=dest, kind=kind, layout=layout, data=data
        )

    new_root = subtree.joinpath(*PROFILE_FILES_ROOT.split("/"))
    if new_root.is_dir():
        for profile_dir in sorted(p for p in new_root.iterdir() if p.is_dir()):
            seen_new = True
            profile = profile_dir.name
            profiles.append(profile)
            for source in sorted(p for p in profile_dir.rglob("*") if p.is_file()):
                dest = source.relative_to(profile_dir).as_posix()
                _ingest(profile, dest, "profile_files", source)

    legacy_root = subtree / LEGACY_PROFILES_ROOT
    if legacy_root.is_dir():
        for profile_dir in sorted(p for p in legacy_root.iterdir() if p.is_dir()):
            profile = profile_dir.name
            personas_root = profile_dir / "personas"
            if not personas_root.is_dir():
                continue
            for persona_dir in sorted(p for p in personas_root.iterdir() if p.is_dir()):
                for source in sorted(p for p in persona_dir.rglob("*") if p.is_file()):
                    parts = source.relative_to(persona_dir).parts
                    dest = _legacy_destination(parts)
                    if dest is None:
                        continue  # not a file family this lane owns (config.yaml et al.)
                    seen_legacy = True
                    if profile not in profiles:
                        profiles.append(profile)
                    _ingest(profile, dest, "legacy_profiles", source)

    for key in sorted(collisions):
        entries.pop(key, None)
        refusals.append(
            Refusal(
                key,
                "destination_collision",
                "two published files claim one destination with different content",
            )
        )

    source = "profile_files" if seen_new else ("legacy_profiles" if seen_legacy else None)
    return (
        entries,
        [row.as_dict() for row in refusals],
        source,
        sorted(set(profiles)),
    )


# --- pull --------------------------------------------------------------------


@dataclass(slots=True)
class ProfileArtifactPullSummary:
    """Typed accounting for the profile-file pull merge.

    Nothing is silently dropped, overwritten, or held:

    - ``adopted`` — the member had no copy, or a copy byte-identical to the
      recorded baseline (nothing of theirs is lost); written.
    - ``converged`` — local already equals remote (no write).
    - ``kept_local`` — the member edited it and the realm did not; stays local.
    - ``held`` — BOTH sides changed (or edit-vs-remove): the member's file is
      left UNTOUCHED and surfaced for an explicit resolve
      (``hermes harness realm sync resolve``). This is the row that keeps a
      member's ``MEMORY.md`` alive.
    - ``retained`` — the realm stopped publishing it. A member's memory / context
      / prompt file is NEVER deleted by a sync; it is kept and reported.
    - ``refused`` — the guarded door would not admit it (destination off the
      allowlist, unsafe path, secret-shaped content, destination collision).
      Per-entity isolation: one bad file can never abort a pull.
    - ``superseded`` — a legacy flat prompt file left behind on disk after this
      pull adopted the same prompt at its real, definition-addressable path. It
      is reported, never deleted, so it cannot masquerade as live unnoticed.
    """

    adopted: list[str] = field(default_factory=list)
    converged: list[str] = field(default_factory=list)
    kept_local: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    superseded: list[dict[str, str]] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)
    created_profiles: list[str] = field(default_factory=list)
    kinds: dict[str, int] = field(default_factory=dict)
    source: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.adopted)

    def as_dict(self) -> dict[str, Any]:
        return {
            "adopted": sorted(set(self.adopted)),
            "converged": sorted(set(self.converged)),
            "kept_local": sorted(set(self.kept_local)),
            "held": sorted(set(self.held)),
            "retained": sorted(set(self.retained)),
            "refused": list(self.refused),
            "superseded": list(self.superseded),
            "profiles": sorted(set(self.profiles)),
            "created": sorted(set(self.created_profiles)),
            "kinds": dict(sorted(self.kinds.items())),
            "source": self.source,
        }


def _may_write(local_hash: str | None, baseline_hash: str | None) -> bool:
    """THE invariant every profile-file write passes through.

    A destination may be written only when the member has nothing there
    (``local_hash is None``) or their copy is byte-identical to what the last
    sync recorded (``local_hash == baseline_hash``) — i.e. the member has not
    accumulated anything since. Any other state is member-authored content and
    is HELD.

    ``classify_three_way_pull`` already implies this, but the invariant is
    asserted here, at the single write site, so it is local, unit-testable, and
    provably the guard: restore the wholesale write and
    ``test_profile_artifact_sync.py::test_diverged_member_memory_is_never_overwritten``
    goes red.
    """

    return local_hash is None or local_hash == baseline_hash


def _profile_home(token: str) -> Path | None:
    from .realm_sync import _profile_home_for_token

    return _profile_home_for_token(token)


def _local_hash(path: Path) -> str | None:
    try:
        return content_hash(path.read_bytes())
    except OSError:
        return None


def _created_profile_tokens(profiles: list[str]) -> list[str]:
    """Profile homes this pull would MATERIALIZE (W-H4, plan §5.1): adoption is a
    typed row, never a silent side effect."""

    from .realm_sync import _safe_token
    from .profile_context import active_profile_name

    active = _safe_token(active_profile_name())
    created: list[str] = []
    for token in profiles:
        if token == active:
            continue
        try:
            from hermes_cli.profiles import normalize_profile_name, profile_exists

            if not profile_exists(normalize_profile_name(token)):
                created.append(token)
        except Exception:  # noqa: BLE001 — an unresolvable profile is simply not "created"
            continue
    return created


def apply_profile_artifact_pull(
    realm_id: str,
    subtree: Path,
    *,
    dry_run: bool = False,
) -> ProfileArtifactPullSummary:
    """Merge pulled profile files into their profile homes, per destination.

    Owns the whole lane: ``_destination_for_sync_path`` returns ``None`` for
    ``profiles/*`` and ``store/profile_files/*``, so the generic overwrite loop
    never touches these files again — the same exclusion precedent as
    ``store/boards/*``, ``store/office/*``, ``skills/*`` and
    ``store/personas.yaml``.

    ``dry_run`` classifies without writing anything — not the destinations and
    not the baseline. ``realm_sync_status`` uses it to surface held rows.
    """

    summary = ProfileArtifactPullSummary()
    remote, refusals, source, profiles = read_remote_profile_files(Path(subtree))
    summary.refused.extend(refusals)
    summary.source = source
    summary.profiles = profiles
    if source is None:
        # No profile files at all. Do NOT touch the baseline: absence here is
        # "this realm publishes none", never "the realm removed mine".
        return summary
    summary.created_profiles = _created_profile_tokens(profiles)
    for entry in remote.values():
        summary.kinds[entry.kind] = summary.kinds.get(entry.kind, 0) + 1

    baseline = read_profile_artifact_baseline(realm_id)
    adopted_destinations: set[str] = set()

    for key in sorted(set(remote) | set(baseline)):
        split = split_entity_key(key)
        if split is None:
            summary.refused.append(
                {"key": key, "code": "invalid_entity_key", "message": "baseline entry is not a profile:destination key"}
            )
            continue
        profile_token, dest_rel = split
        home = _profile_home(profile_token)
        if home is None:
            summary.refused.append(
                {"key": key, "code": "unsafe_profile_token", "message": f"profile token is not a safe home: {profile_token}"}
            )
            continue
        entry = remote.get(key)
        if entry is None and classify_destination(dest_rel) is None:
            # A stale baseline entry for a destination no longer on the allowlist.
            baseline.pop(key, None)
            continue
        destination = home.joinpath(*dest_rel.split("/"))
        local_hash = _local_hash(destination)
        remote_hash = entry.content_hash if entry is not None else None
        decision = classify_three_way_pull(local_hash, remote_hash, baseline.get(key))

        if decision.action == PullAction.NOOP:
            if entry is not None:
                summary.converged.append(key)
            else:
                baseline.pop(key, None)  # absent_both — retire the stale entry
            continue
        if decision.action == PullAction.KEEP_LOCAL:
            summary.kept_local.append(key)
            continue
        if decision.action == PullAction.ARCHIVE_LOCAL:
            # The realm stopped publishing it. A member's memory / context /
            # prompt file is never removed by a sync.
            summary.retained.append(key)
            baseline.pop(key, None)
            continue
        if decision.action == PullAction.CONFLICT:
            summary.held.append(key)
            continue
        if decision.action == PullAction.WRITE_REMOTE and entry is not None:
            if decision.reason == "converged":
                summary.converged.append(key)
                baseline[key] = remote_hash or ""
                continue
            if not _may_write(local_hash, baseline.get(key)):
                # Unreachable through the classifier; asserted anyway. A member's
                # divergent content is HELD, never overwritten — including if a
                # future refactor loosens the decision table above.
                summary.held.append(key)
                continue
            summary.adopted.append(key)
            adopted_destinations.add(key)
            if not dry_run:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(entry.data)
            baseline[key] = remote_hash or ""
            continue

    summary.superseded = _superseded_flat_prompts(remote, adopted_destinations)
    if not dry_run:
        write_profile_artifact_baseline(realm_id, baseline)
    return summary


def _superseded_flat_prompts(
    remote: dict[str, RemoteProfileFile], adopted: set[str]
) -> list[dict[str, str]]:
    """Legacy flat prompt files this pull just made obsolete.

    Before this change a prompt published as ``…/system_prompt/<basename>``
    landed at ``<home>/personas/<basename>`` regardless of where the publisher
    kept it. Once the same prompt is adopted at its real, definition-addressable
    path (``personas/<persona>/<basename>``), the flat file is a leftover. It is
    REPORTED, never deleted — deleting a member's file on a sync is the very
    class this module retires — so it cannot masquerade as live unnoticed.
    """

    rows: list[dict[str, str]] = []
    for key in sorted(adopted):
        entry = remote.get(key)
        if entry is None or entry.kind != KIND_PERSONA_PROMPT:
            continue
        parts = entry.destination.split("/")
        if len(parts) < 3:
            continue  # already flat — it IS the destination
        flat_rel = f"{PERSONA_PROMPT_DIR}/{parts[-1]}"
        flat_key = entity_key(entry.profile, flat_rel)
        if flat_key in remote:
            continue  # the realm still publishes the flat file too; not a leftover
        home = _profile_home(entry.profile)
        if home is None:
            continue
        flat_path = home.joinpath(*flat_rel.split("/"))
        if flat_path.exists():
            rows.append(
                {
                    "key": flat_key,
                    "reason": "legacy_flat_layout",
                    "superseded_by": key,
                    "message": "left in place; the realm now publishes this prompt at its profile-relative path",
                }
            )
    return rows


# --- operator resolution -----------------------------------------------------


class ProfileArtifactResolveError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def resolve_profile_artifact(
    realm_id: str,
    subtree: Path,
    key: str,
    *,
    take: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Resolve ONE held profile-file entity.

    Both takes record the realm's current hash as the new baseline — that is what
    "I have seen the realm's version" means, and it is what stops the hold from
    re-reporting forever:

    - ``--take remote`` writes the realm's file, so the next pull is a NOOP.
    - ``--take local`` writes nothing, so the next pull classifies the member's
      copy as ``kept_local`` (local changed vs an unchanged remote) instead of a
      conflict. The member's content is never touched.

    ``dry_run`` writes NOTHING — not the destination, not the baseline. The verb
    is registered with ``_add_stage42_global_args(mutation=True)`` and reads
    ``args.dry_run`` at this chokepoint; ``test_profile_artifact_sync.py`` pins
    that the store is byte-identical after a dry run.
    """

    if take not in ("local", "remote"):
        raise ProfileArtifactResolveError("invalid_request", "take must be 'local' or 'remote'")
    split = split_entity_key(key)
    if split is None:
        raise ProfileArtifactResolveError("invalid_request", f"not a profile-file entity key: {key}")
    profile_token, dest_rel = split
    home = _profile_home(profile_token)
    if home is None:
        raise ProfileArtifactResolveError("invalid_request", f"profile token is not a safe home: {profile_token}")
    remote, _refusals, source, _profiles = read_remote_profile_files(Path(subtree))
    entry = remote.get(key)
    if entry is None:
        raise ProfileArtifactResolveError(
            "not_found",
            f"the realm does not publish {key} (source={source}); nothing to resolve against",
        )
    destination = home.joinpath(*dest_rel.split("/"))
    remote_hash = entry.content_hash
    if not dry_run:
        if take == "remote":
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(entry.data)
        baseline = read_profile_artifact_baseline(realm_id)
        baseline[key] = remote_hash
        write_profile_artifact_baseline(realm_id, baseline)
    return {
        "key": key,
        "kind": entry.kind,
        "profile": profile_token,
        "destination": dest_rel,
        "take": take,
        "changed": take == "remote",
        "remote_hash": remote_hash,
    }
