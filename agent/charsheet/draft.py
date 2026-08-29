"""Staged character drafts — the QA state machine, the install, the payload.

A draft is a directory under ``<hermes_root>/shared/characters/.drafts/<id>/``
holding ``draft.json`` (schema 1), the base identity image, an
:class:`~agent.charsheet.revisions.ImageRevisionStore` of every attempt, and the
accepted row strips. Installing writes
``<hermes_root>/shared/characters/<slug>/{character.json, sheet.webp}``.

**The library is install-wide, not profile-scoped.** Every persona profile under
one hermes root resolves the SAME directory (see :func:`characters_dir`), so a
draft id names a draft for the whole install and a turn that resolved a home
nobody selected still reads the library the operator meant.

**The stage machine is the operator's QA order, and it is enforced.**
``turnaround`` → ``rows`` → ``composed``: the cardinal directions are approved
before any animation is generated, because every animation row is grounded on an
approved direction reference and re-rolling rows after changing a reference would
waste the expensive half of the flow. Every stage verb refuses an out-of-order
call with a :class:`ValueError` naming the current stage and what it requires.

**What is auto-approved, and why.** Direction references are the identity gate,
so they are proposed and wait for a human. Row strips are proposed *and*
immediately approved: their failure mode is geometric (touching poses, merged
frames) and that is already rejected mechanically before a strip is accepted
(:func:`agent.charsheet.pipeline.generate_row_strip`), so what remains is a
judgement call — "that walk looks wrong" — which the operator makes by looking at
the status payload and re-rolling. Making 10+ rows individually approvable would
add a click per row without adding a decision. A re-rolled row is auto-approved
for the same reason; the gate is visual, and a re-roll always replaces what the
sheet will use.

**Concurrency.** Every JSON write is tmp + :func:`os.replace` (the revision
store's discipline), so a reader sees one whole state or the other. There is no
lock — ``agent_runtime.locks`` is not in the shipped wheel — so two writers on
one draft are last-writer-wins per item (plan §A-7).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from agent.charsheet import pipeline
from agent.charsheet.revisions import ImageRevisionStore
from agent.charsheet.spec import (
    CHAR8,
    DEFAULT_FRAME_H,
    DEFAULT_FRAME_W,
    DirectionScheme,
    SheetSpec,
    StateSpec,
    parse_states,
)
from agent.pet.constants import DEFAULT_SCALE, LOOP_MS
from hermes_constants import get_hermes_home, get_shared_characters_dir

logger = logging.getLogger(__name__)

# One number, three documents: `draft.json`, the `status --json` payload, and
# the INSTALLED `character.json` manifest the launcher parses. That is why
# neither `authored_by` NOR `hermes_home` bumped it — an optional, draft-only
# provenance field that a schema-1 reader already tolerates (it is absent when
# unset, and unknown keys were always ignored) is not worth relabelling an
# installed sheet's manifest for. Both fields clear that bar the same way:
# neither is copied into the manifest, and no consumer branches on either to be
# correct — a reader that ignores them renders exactly what it renders today. A
# field that a consumer must read to be correct is a different case and would
# bump all three, which is the argument for splitting them first.
SCHEMA = 1

# turnaround → rows → composed. Order is the tuple order; nothing branches on a
# stage count.
STAGES: tuple[str, ...] = ("turnaround", "rows", "composed")

DRAFTS_DIRNAME = ".drafts"
DRAFT_FILENAME = "draft.json"
MANIFEST_FILENAME = "character.json"
SHEET_FILENAME = "sheet.webp"
REVISIONS_DIRNAME = "revisions"
THUMBS_DIRNAME = "thumbs"

# QA crops: 2x is what made a one-pixel seam legible in a chat card during the
# 2026-08-24 run, so it is the default. What bounds a crop is not a scale
# ceiling but the output's pixel count, and it is a refusal rather than a clamp
# — a caller who asks for 40x has made a mistake and should be told, not
# silently given 8x and left to wonder why the crop is small.
#
# This constant is also the line between the two pixel bounds (see
# `pipeline.MAX_CONSOLE_CARD_PIXELS` / `pipeline.MAX_THUMB_PIXELS`): at or
# below the default a crop must clear the console's decode ceiling, because it
# is the crop a caller who just asked for a picture gets and the one an agent
# declares to a card. Above it, the caller has asked for a deep zoom on purpose
# and gets one — bounded by the write ceiling, and labelled in the payload.
#
# The SHEET bound (`pipeline.fits_own_sheet`) is not a line here at all: it is
# reported at every scale and refuses nothing. A crop 13.1x its own draft's
# sheet is still a legal picture of one frame — what it is not is a mitigation,
# and the payload is where that gets said.
DEFAULT_THUMB_SCALE = 2

# Which frame cell a crop shows when the caller does not say. Frame 0 is the
# first pose of the strip and the one an operator reaches for first; the point
# of a default is that `thumb --row walk-n` crops SOMETHING, never the whole
# strip (which removes no pixels at all — see `pipeline.frame_cell`).
DEFAULT_THUMB_FRAME = 0

_SLUG_RE = re.compile(r"[^a-z0-9]+")


# ─────────────────────────────── locations ───────────────────────────────


def characters_dir() -> Path:
    """The ONE install-wide character library (created on demand).

    Delegates to :func:`hermes_constants.get_shared_characters_dir` and adds
    nothing but the mkdir. This is the single site in hermes that spells the
    characters location: ``drafts_dir``, ``create``, ``load``, ``list_drafts``,
    the install writer and the CLI's installed-character rows all resolve
    through it, which is why head-homing the library was this one delegation and
    not a per-verb edit across fifteen verbs.

    It is deliberately NOT ``get_hermes_home() / "characters"`` any more: a
    per-profile library made "can this lane see that draft" a home comparison,
    and every wrong answer to it — a bare shell resolving the sticky profile, a
    serve prewarm mirroring another persona home mid-read — became a characters
    incident. One directory per root has no such question to get wrong.
    """
    path = get_shared_characters_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def drafts_dir() -> Path:
    """Where in-progress drafts live (created on demand)."""
    path = characters_dir() / DRAFTS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def stamp_recorded_home(directory: Path, home: str) -> bool:
    """Write ``hermes_home`` onto a draft that carries none. Return what happened.

    The value is an ARGUMENT, which is the difference between this and
    :meth:`CharacterDraft.record_home`: the backfill stamps the home the run
    resolved, and a migration stamps the home the draft is LEAVING — a fact the
    directory itself is about to stop witnessing. Same two rules otherwise, and
    both are load-bearing:

    * **It never rewrites.** A draft that already states a home keeps it. A
      relocation is not a re-attribution, and the drafts whose provenance is
      most interesting are exactly the ones an unconditional stamp destroys.
    * **It does not go through** :meth:`CharacterDraft._save`. ``_save`` stamps
      ``updated`` with "now", and the drafts this reaches are dormant exhibits
      whose timeline is the evidence they are kept for. This writes the file
      directly, so every other byte is left as it was found.
    """
    path = Path(directory) / DRAFT_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if str(data.get("hermes_home", "") or "").strip():
        return False
    data["hermes_home"] = str(home)
    _write_json_atomic(path, data)
    return True


def _migration_entry_id(directory: Path) -> str:
    """The id a draft directory lists under — from the FILE, not the leaf name.

    They differ in the case the live disk actually holds: an id-collision pair
    (``<id>/`` beside ``<id>.backup-…/``) whose two ``draft.json`` files carry
    the SAME id. The receipt names directories beside ids for that reason, the
    same reason the backfill's does.
    """
    try:
        data = json.loads((Path(directory) / DRAFT_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Path(directory).name
    if not isinstance(data, dict):
        return Path(directory).name
    return str(data.get("id", "") or Path(directory).name)


def migrate_characters_home(source: Path, destination: Path, *, source_home: str) -> dict:
    """Move a legacy per-home character store into the install-wide library.

    Explicit source and destination :class:`~pathlib.Path` arguments, so the
    rules below are unit-testable without env games and so the CALLER owns the
    decision of which home is being migrated. That matters more than it looks: after the
    library head-homed, ``characters_dir()`` answers the DESTINATION, so a verb
    that resolved its source through it would be asking to move the library onto
    itself. The handler spells the legacy location literally and this function
    refuses the degenerate case anyway.

    Four rules:

    * **Stamp before move.** A draft with no ``hermes_home`` is stamped with the
      SOURCE home (:func:`stamp_recorded_home`) before it is relocated — after
      the move the directory no longer witnesses where the draft lived, and this
      is the last chance to record it first-party. A present key is never
      rewritten.
    * **Move, never copy-and-delete.** One :func:`os.replace` per entry. Draft
      directories keep their leaf names (so a stored binding's ``draftId`` and
      ``load()`` both keep resolving) and installed characters keep their slugs.
    * **A collision is a per-entry refusal.** A destination that already holds
      the leaf or the slug lands in ``skipped`` with a reason and its source is
      left untouched — never a merge, never an overwrite. Archive-never-delete
      makes that the only available answer: a move that lands intact destroys
      nothing, and a move that cannot land must destroy nothing either.
    * **Nothing is deleted, the emptied tree included.** The source
      ``characters/`` directory is left standing as its own tombstone — it is
      the only thing left saying a per-home store was ever there, and it is what
      the receipt's ``from`` refers to.

    Idempotent: a second run finds no sources and moves nothing.
    """
    source = Path(source)
    destination = Path(destination)
    moved: list[dict] = []
    stamped: list[dict] = []
    skipped: list[dict] = []
    receipt = {
        "ok": True,
        "from": str(source),
        "to": str(destination),
        "moved": moved,
        "stamped": stamped,
        "skipped": skipped,
    }
    if not source.is_dir() or source.resolve() == destination.resolve():
        return receipt

    def _relocate(child: Path, target: Path, row: dict) -> None:
        if target.exists():
            skipped.append({**row, "reason": f"destination already exists: {target}"})
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(child, target)
        except OSError as exc:
            # A cross-volume rename, a lock, a permission — the entry stays where
            # it is and the receipt says why. Reported rather than raised so one
            # stuck entry cannot strand the rest of the store half-migrated.
            skipped.append({**row, "reason": f"could not move: {exc}"})
            return
        moved.append({**row, "from": str(child), "to": str(target)})

    src_drafts = source / DRAFTS_DIRNAME
    for child in sorted(src_drafts.iterdir()) if src_drafts.is_dir() else []:
        if not child.is_dir() or not (child / DRAFT_FILENAME).is_file():
            continue
        draft_id = _migration_entry_id(child)
        if stamp_recorded_home(child, source_home):
            stamped.append({"id": draft_id, "directory": str(child)})
        _relocate(
            child,
            destination / DRAFTS_DIRNAME / child.name,
            {"kind": "draft", "id": draft_id},
        )

    for child in sorted(source.iterdir()):
        if child.name == DRAFTS_DIRNAME or not child.is_dir():
            continue
        manifest_path = child / MANIFEST_FILENAME
        if not manifest_path.is_file():
            # The same definition of "an installed character" the CLI's
            # installed rows use. A directory that is not one is left where it
            # is rather than swept along — an unrecognised tree under a
            # characters store is exactly the thing a move should not guess at.
            skipped.append(
                {
                    "kind": "installed",
                    "slug": child.name,
                    "directory": str(child),
                    "reason": f"no {MANIFEST_FILENAME}: not an installed character",
                }
            )
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        if not isinstance(manifest, dict):
            manifest = {}
        slug = str(manifest.get("slug", "") or child.name)
        _relocate(child, destination / child.name, {"kind": "installed", "slug": slug})

    return receipt


def slugify(name: str) -> str:
    """Lowercase, hyphenate and strip *name* into one filesystem path segment."""
    slug = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    return slug or "character"


def _safe_segment(value: str) -> str:
    """One bare path segment — a slug/id can never escape its parent directory."""
    return Path(str(value).strip()).name


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(path: Path, payload: dict) -> None:
    """tmp + fsync + :func:`os.replace` — a reader never sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ──────────────────────────── spec round-trip ────────────────────────────


def spec_to_dict(spec: SheetSpec) -> dict:
    """JSON form of a :class:`SheetSpec` — states, scheme and cell geometry.

    The sheet's taxonomy must travel *with* the sheet: the pet readers infer row
    lists from image height, and a 16-row character sheet pushed through that
    inference is silently misread as a 9-row pet (plan §0.4).
    """
    return {
        "states": [
            {"name": state.name, "frames": state.frames, "directional": bool(state.directional)}
            for state in spec.states
        ],
        "scheme": {
            "order": list(spec.scheme.order),
            "authored": list(spec.scheme.authored),
            "mirrored": dict(spec.scheme.mirrored),
        },
        "frameW": spec.frame_w,
        "frameH": spec.frame_h,
    }


def spec_from_dict(data: dict) -> SheetSpec:
    """Rebuild a :class:`SheetSpec` from :func:`spec_to_dict` output."""
    if not isinstance(data, dict):
        raise ValueError(f"spec must be a JSON object, got {type(data).__name__}")
    raw_states = data.get("states")
    if not isinstance(raw_states, list) or not raw_states:
        raise ValueError("spec.states must be a non-empty list")
    states = []
    for entry in raw_states:
        if not isinstance(entry, dict):
            raise ValueError(f"spec.states entry must be an object, got {entry!r}")
        try:
            states.append(
                StateSpec(
                    name=str(entry["name"]),
                    frames=int(entry["frames"]),
                    directional=bool(entry["directional"]),
                )
            )
        except KeyError as exc:
            raise ValueError(f"spec.states entry {entry!r} is missing {exc.args[0]!r}") from None

    raw_scheme = data.get("scheme")
    if not isinstance(raw_scheme, dict):
        raise ValueError("spec.scheme must be a JSON object")
    try:
        scheme = DirectionScheme(
            order=tuple(str(d) for d in raw_scheme["order"]),
            authored=tuple(str(d) for d in raw_scheme["authored"]),
            mirrored={str(k): str(v) for k, v in dict(raw_scheme.get("mirrored") or {}).items()},
        )
    except KeyError as exc:
        raise ValueError(f"spec.scheme is missing {exc.args[0]!r}") from None

    return SheetSpec(
        states=tuple(states),
        scheme=scheme,
        frame_w=int(data.get("frameW", DEFAULT_FRAME_W)),
        frame_h=int(data.get("frameH", DEFAULT_FRAME_H)),
    )


# ───────────────────────────── revision keys ─────────────────────────────


def turnaround_item(direction: str) -> str:
    """Revision-store key for a direction reference."""
    return f"turnaround@{direction}"


def row_item(key: str) -> str:
    """Revision-store key for a row strip (``row@walk-e``).

    The leading ``row@`` is the store's ITEM-KIND separator — it is not the
    sheet's row-key separator, which is the hyphen. Both live in one string on
    purpose: the store never parses keys, so the two namespaces cannot collide.
    """
    return f"row@{key}"


def _strip_filename(key: str, attempt: int) -> str:
    return f"{key}-{attempt}.png"


def path_or_none(path: Path | None) -> str | None:
    """One spelling of "there is no file here" for the whole payload.

    A path field is a ``str`` or ``None``; ``""`` is neither, and it is what a
    consumer gets when a ``Path | None`` is coerced through ``str(x or "")``.
    Every path in ``status --json`` goes through here so absence cannot acquire
    a second spelling one field at a time.

    **Public because the rule is not this module's alone.** It shipped private
    and ``hermes_cli.harness._characters_draft_summary`` — the ``list`` row,
    carrying the same ``baseImage`` field — kept its own ``str(x) if x else ""``
    for exactly as long. A one-module helper enforcing a payload-wide rule is
    how the fourth field got missed; the CLI imports this one now.
    """
    return str(path) if path is not None else None


# ─────────────────────────────── the draft ───────────────────────────────


class CharacterDraft:
    """One in-progress character: its spec, its stage, and its QA history."""

    def __init__(self, directory: Path, data: dict) -> None:
        self.directory = Path(directory)
        self._data = data

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def create(
        cls,
        *,
        concept: str,
        slug: str = "",
        display_name: str = "",
        style: str = "auto",
        spec: SheetSpec = CHAR8,
        base_image=None,
        authored_by: str = "",
    ) -> CharacterDraft:
        """Start a draft at stage ``turnaround``.

        *base_image* is the identity anchor everything is grounded on; it is
        copied into the draft so a later stage can never be invalidated by the
        caller moving or deleting the original. It may be supplied later
        (:meth:`set_base_image`) — the base-draft pick flow has not chosen one
        yet at ``characters start`` time — but no generation verb runs without it.

        *authored_by* is PROVENANCE and nothing else (launcher companion doc §13
        decision 6, which is the single statement of the home rule — this
        docstring points at it and does not restate it): it records which persona
        drove the authoring run so a later reader can ask "whose draft is this".
        It does not scope where the draft lives — nothing does any more: the
        library is install-wide (:func:`characters_dir`), one directory per
        hermes root, whatever persona or profile runs the authoring turn. It is
        not an owner, and no verb checks it. What it does make possible is
        checking: a consumer resuming a draft can ask whether the persona it is
        about to open is bound to the profile that authored it, instead of
        discovering the mismatch as an empty ``status``. Nothing infers it: a
        caller that does not say stores nothing, because a guessed author is
        worse than an absent one.

        "Stores nothing" is literal — the KEY is absent, not present-and-empty.
        An empty string would be a third spelling of "no author" that reads as a
        value, and it is the spelling that survives ``.get(..., "")`` all the way
        into the payload, where a consumer can no longer tell a draft with no
        author recorded from one authored by ``""`` and a backfill can no longer
        select the drafts that need filling in.

        ``hermes_home`` is the OTHER provenance field, and it is written every
        time because nobody has to supply it: it is ``str(get_hermes_home())``,
        the home this run RESOLVED. It is provenance of the run and not a
        locator — the draft sits in the install-wide library, which is not under
        the home this key names, and the library address is a constant every
        reader already knows. What no other record carries is which profile turn
        authored the draft: ``authored_by`` names the persona, this names the
        profile side of the same turn. See :attr:`hermes_home` for what the
        value means once it is stale, and :meth:`record_home` for the drafts
        that arrive without it.
        """
        concept = str(concept or "").strip()
        if not concept:
            raise ValueError("a draft needs a concept: the character description to generate")
        # Validate the anchor BEFORE any directory exists: a bad path must fail
        # the start cleanly, not leave an orphan draft dir behind (CS-5 finding).
        if base_image is not None and not Path(base_image).is_file():
            raise ValueError(f"base image {Path(base_image)} is not an existing file")
        display = str(display_name or "").strip() or concept
        chosen_slug = slugify(slug or display)
        draft_id = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        directory = drafts_dir() / draft_id
        directory.mkdir(parents=True, exist_ok=False)

        now = _utc_now()
        data = {
            "schema": SCHEMA,
            "id": draft_id,
            "slug": chosen_slug,
            "display_name": display,
            "concept": concept,
            "style": str(style or "auto"),
            "stage": STAGES[0],
            "created": now,
            "updated": now,
            "spec": spec_to_dict(spec),
            "base_image": "",
        }
        author = str(authored_by or "").strip()
        if author:
            data["authored_by"] = author
        # Written UNCONDITIONALLY, unlike `authored_by`: there is no caller to
        # withhold it and nothing to guess — hermes asks its own resolver which
        # home this turn answered and records that. The draft does NOT sit under
        # it (the library is install-wide, `directory` is under
        # `<root>/shared/characters`), and that divergence is the field's
        # re-derived meaning rather than a defect: provenance of the RUN, not a
        # locator. It is still a first-party fact hermes states about itself,
        # never a path a consumer sliced a profile name out of.
        data["hermes_home"] = str(get_hermes_home())
        draft = cls(directory, data)
        draft._save()
        if base_image is not None:
            draft.set_base_image(base_image)
        logger.info("charsheet draft %s created (slug %r)", draft_id, chosen_slug)
        return draft

    @classmethod
    def load(cls, draft_id: str) -> CharacterDraft:
        """Load a draft by id, or raise :class:`FileNotFoundError` with the path."""
        directory = drafts_dir() / _safe_segment(draft_id)
        path = directory / DRAFT_FILENAME
        if not path.is_file():
            raise FileNotFoundError(f"no draft {draft_id!r}: {path} does not exist")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"corrupt draft file {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"corrupt draft file {path}: expected a JSON object")
        return cls(directory, data)

    @classmethod
    def list_drafts(cls) -> list[CharacterDraft]:
        """Every readable draft, oldest id first (ids sort chronologically)."""
        out: list[CharacterDraft] = []
        root = drafts_dir()
        for child in sorted(root.iterdir()) if root.is_dir() else []:
            if not (child / DRAFT_FILENAME).is_file():
                continue
            try:
                out.append(cls.load(child.name))
            except (OSError, ValueError) as exc:
                logger.warning("skipping unreadable draft %s: %s", child.name, exc)
        return out

    # ------------------------------------------------------------ accessors

    @property
    def id(self) -> str:
        return str(self._data.get("id", self.directory.name))

    @property
    def slug(self) -> str:
        return str(self._data.get("slug", ""))

    @property
    def display_name(self) -> str:
        return str(self._data.get("display_name", "") or self.slug)

    @property
    def concept(self) -> str:
        return str(self._data.get("concept", ""))

    @property
    def style(self) -> str:
        return str(self._data.get("style", "auto") or "auto")

    @property
    def stage(self) -> str:
        return str(self._data.get("stage", STAGES[0]))

    @property
    def authored_by(self) -> str | None:
        """The persona this draft was authored by, or ``None`` — provenance only.

        ``None`` and not ``""``: absence is a fact a consumer must be able to
        read. It travels to the payload as JSON ``null``, so B2/P1 can render
        "unattributed" honestly and a later backfill can select exactly the
        drafts that carry no author — neither of which is possible once absence
        has been flattened into an empty string.
        """
        author = str(self._data.get("authored_by", "") or "").strip()
        return author or None

    @property
    def hermes_home(self) -> str | None:
        """The home the authoring RUN resolved, or ``None``.

        ``None`` and not ``""``, for exactly the reason ``authored_by`` gives
        above: the drafts written before this key existed have to stay
        selectable by the backfill, and a consumer has to be able to READ that
        no home was ever recorded rather than receive a value that renders as a
        blank path.

        **It is not an address, and asking it for one gets the wrong answer by
        construction.** The draft lives in the install-wide library
        (:func:`characters_dir`) whatever home created it, so this key answers
        "which profile turn authored this" — the profile-side complement of
        ``authored_by``'s persona. Where the file is, is ``directory``.

        **What it means when it disagrees with the home resolving now.** It is
        provenance about a PAST fact — the home hermes recorded when the draft
        was created, or the source home a ``migrate-home`` run stamped it with
        on the way into the library. A draft authored under one profile still
        names that profile when read from every other one, and that is the field
        being honest. Nothing rewrites a value once it is here.
        """
        home = str(self._data.get("hermes_home", "") or "").strip()
        return home or None

    @property
    def spec(self) -> SheetSpec:
        return spec_from_dict(self._data.get("spec") or {})

    @property
    def base_image(self) -> Path | None:
        name = str(self._data.get("base_image", "") or "")
        if not name:
            return None
        path = self.directory / name
        return path if path.is_file() else None

    @property
    def store(self) -> ImageRevisionStore:
        return ImageRevisionStore(self.directory / REVISIONS_DIRNAME)

    # --------------------------------------------------------- provenance

    def record_home(self) -> bool:
        """Fill in a missing ``hermes_home``; return whether anything was written.

        The stamp path for a draft that arrives in the library without the key
        — one restored from quarantine, one hand-copied in — and the second site
        that writes it (``create`` is the first). Two rules, and both are
        load-bearing:

        **It never rewrites.** A draft that already states a home keeps it, even
        when that home is not the one resolving now — see :attr:`hermes_home`.
        Stamping unconditionally would overwrite the history the field exists to
        keep, silently, on every run, and it is the copied and backed-up drafts
        — the ones whose recorded home is most interesting — that it would
        destroy first. A present-but-blank value counts as absent, because the
        accessor already rules that ``""`` is not a home.

        **It does not go through :meth:`_save`.** ``_save`` stamps ``updated``
        with "now", and the drafts this exists for are dormant exhibits whose
        timeline is evidence: a backfill that bumped every one of them to the
        moment an operator ran it would falsify exactly what those drafts are
        kept to show. This writes the file directly, so every other byte —
        ``updated`` and ``authored_by`` included — is left as it was found.
        """
        if self.hermes_home is not None:
            return False
        self._data["hermes_home"] = str(get_hermes_home())
        _write_json_atomic(self.directory / DRAFT_FILENAME, self._data)
        logger.info("charsheet draft %s: recorded home %s", self.id, self._data["hermes_home"])
        return True

    # ------------------------------------------------------------- internals

    def _save(self) -> None:
        self._data["updated"] = _utc_now()
        _write_json_atomic(self.directory / DRAFT_FILENAME, self._data)

    def _set_stage(self, stage: str) -> None:
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}; expected one of {list(STAGES)}")
        self._data["stage"] = stage
        self._save()

    def _require_stage(self, verb: str, *expected: str) -> None:
        if self.stage not in expected:
            wanted = " or ".join(repr(name) for name in expected)
            raise ValueError(
                f"{verb} requires draft stage {wanted}, but draft {self.id} is at "
                f"stage {self.stage!r} (stage order: {' -> '.join(STAGES)})"
            )

    def _require_base(self) -> Path:
        base = self.base_image
        if base is None:
            raise ValueError(
                f"draft {self.id} has no base image; pick one before generating "
                "(every direction and row is grounded on it)"
            )
        return base

    def _require_authored_direction(self, direction: str) -> str:
        authored = self.spec.scheme.authored
        if direction not in authored:
            raise ValueError(
                f"direction {direction!r} is not authored for this sheet "
                f"(authored: {', '.join(authored)}); mirrored directions are "
                "never drawn and never composed, so they are never QA items"
            )
        return direction

    def _authored_row(self, key: str):
        for row in self.spec.authored_rows():
            if row.key == key:
                return row
        raise ValueError(
            f"{key!r} is not an authored row of this sheet (authored rows: "
            f"{', '.join(row.key for row in self.spec.authored_rows())})"
        )

    def set_base_image(self, image_path) -> Path:
        """Copy *image_path* into the draft as its identity anchor."""
        source = Path(image_path)
        if not source.is_file():
            raise ValueError(f"base image {source} is not an existing file")
        suffix = source.suffix.lower() or ".png"
        target = self.directory / f"base{suffix}"
        # Bytes are copied verbatim rather than re-encoded: the base is what the
        # provider is shown, and a re-encode would change what identity is
        # grounded on.
        _write_bytes_atomic(target, source.read_bytes())
        self._data["base_image"] = target.name
        self._save()
        return target

    # ------------------------------------------------- stage 1: turnaround

    def run_turnaround(self, provider=None) -> dict:
        """One strip → an unapproved reference per authored direction.

        Re-running proposes a fresh attempt for every direction and clears the
        approvals (the revision store's rule), which is the intended behaviour for
        "the whole turnaround was bad".
        """
        self._require_stage("run_turnaround", "turnaround")
        spec = self.spec
        base = self._require_base()
        refs = pipeline.generate_turnaround(
            spec,
            self.concept,
            base,
            style=self.style,
            provider=provider,
            out_dir=self.directory / "turnaround",
        )
        store = self.store
        out: dict[str, dict] = {}
        for direction in pipeline.turnaround_order(spec.scheme.authored):
            path = refs[direction]
            attempt = store.propose(turnaround_item(direction), path)
            out[direction] = {"attempt": attempt, "path": str(path), "approved": False}
        self._save()
        return {"stage": self.stage, "turnaround": out}

    def reroll_direction(self, direction: str, note: str = "", provider=None) -> dict:
        """Re-generate ONE direction reference on a square canvas, with *note*.

        Proposed unapproved: this is the identity gate, and a re-roll is a new
        candidate for the operator to look at.
        """
        self._require_stage("reroll_direction", "turnaround")
        self._require_authored_direction(direction)
        base = self._require_base()
        store = self.store
        key = turnaround_item(direction)
        attempts = len(store.history(key))
        out_path = self.directory / "turnaround" / f"reroll-{direction}-{attempts + 1}.png"
        path = pipeline.generate_direction_view(
            direction,
            self.concept,
            base,
            style=self.style,
            note=note,
            provider=provider,
            out=out_path,
        )
        attempt = store.propose(key, path, note=note)
        self._save()
        return {
            "direction": direction,
            "attempt": attempt,
            "attempts": attempt + 1,
            "path": str(path),
            "note": note,
            "approved": False,
        }

    def approve_direction(self, direction: str, attempt: int = -1) -> dict:
        """Approve a direction reference; advances the stage once all are approved."""
        self._require_stage("approve_direction", "turnaround")
        self._require_authored_direction(direction)
        index = self.store.approve(turnaround_item(direction), attempt)
        advanced = self._advance_if_directions_approved()
        return {"direction": direction, "approved": index, "stage": self.stage, "advanced": advanced}

    def approve_all_directions(self) -> dict:
        """Approve the latest attempt of every authored direction, then advance."""
        self._require_stage("approve_all_directions", "turnaround")
        store = self.store
        approved: dict[str, int] = {}
        for direction in self.spec.scheme.authored:
            key = turnaround_item(direction)
            if not store.history(key):
                raise ValueError(
                    f"direction {direction!r} has no attempt to approve; run the "
                    "turnaround first"
                )
            approved[direction] = store.approve(key)
        advanced = self._advance_if_directions_approved()
        return {"approved": approved, "stage": self.stage, "advanced": advanced}

    def _advance_if_directions_approved(self) -> bool:
        store = self.store
        pending = [
            direction
            for direction in self.spec.scheme.authored
            if store.current(turnaround_item(direction)) is None
        ]
        if pending:
            return False
        self._set_stage("rows")
        logger.info("charsheet draft %s: all directions approved → stage 'rows'", self.id)
        return True

    # ------------------------------------------------------ stage 2: rows

    def run_rows(self, only: list[str] | None = None, provider=None) -> dict:
        """Generate the animation strips for the authored rows.

        Each accepted strip is proposed AND approved — see the module docstring:
        the mechanical gate already ran, so what is left is a visual judgement the
        operator makes from the status payload.

        *only* restricts the run to the given row keys (``["walk-e"]``).
        """
        self._require_stage("run_rows", "rows")
        spec = self.spec
        rows = spec.authored_rows()
        if only is not None:
            wanted = [str(key) for key in only]
            known = {row.key for row in rows}
            unknown = [key for key in wanted if key not in known]
            if unknown:
                raise ValueError(
                    f"unknown row key(s) {unknown}; authored rows: {sorted(known)}"
                )
            rows = [row for row in rows if row.key in set(wanted)]

        store = self.store
        out: dict[str, dict] = {}
        for row in rows:
            ref = self._row_reference(row)
            key = row_item(row.key)
            attempts = len(store.history(key))
            out_path = self.directory / "strips" / _strip_filename(row.key, attempts + 1)
            path = pipeline.generate_row_strip(
                row,
                self.concept,
                ref,
                style=self.style,
                provider=provider,
                out=out_path,
            )
            attempt = store.propose(key, path)
            store.approve(key, attempt)
            out[row.key] = {
                "attempt": attempt,
                "path": str(path),
                "approved": True,
                "reference": str(ref),
            }
        self._save()
        return {"stage": self.stage, "rows": out}

    def _row_reference(self, row) -> Path:
        """What a row is grounded on: its approved direction ref, else the base."""
        if row.direction is None:
            return self._require_base()
        ref = self.store.current(turnaround_item(row.direction))
        if ref is None:
            raise ValueError(
                f"row {row.key!r} needs the approved {row.direction!r} turnaround "
                "reference, which is not approved"
            )
        return ref

    def reroll_row(self, row_key: str, note: str = "", provider=None) -> dict:
        """Re-generate one row strip; the new strip is auto-approved.

        Same reason as :meth:`run_rows`: a re-roll always replaces what the sheet
        will use, and the operator's gate is visual (look at the strip, re-roll
        again if it is still wrong).
        """
        self._require_stage("reroll_row", "rows")
        row = self._authored_row(row_key)
        store = self.store
        key = row_item(row.key)
        attempts = len(store.history(key))
        ref = self._row_reference(row)
        out_path = self.directory / "strips" / _strip_filename(row.key, attempts + 1)
        path = pipeline.generate_row_strip(
            row,
            self.concept,
            ref,
            style=self.style,
            note=note,
            provider=provider,
            out=out_path,
        )
        attempt = store.propose(key, path, note=note)
        store.approve(key, attempt)
        self._save()
        return {
            "row": row.key,
            "attempt": attempt,
            "attempts": attempt + 1,
            "path": str(path),
            "note": note,
            "approved": True,
        }

    def add_state(self, state_text: str) -> dict:
        """Grow the sheet by ONE state. No approved row is touched.

        The owner ask this exists for: add ``jumping:6`` to a character that is
        already composed and installed, without re-authoring it. The operator
        sequence is ``reopen`` -> ``add-state`` -> ``rows --only <the new rows>``
        -> QA -> ``compose``, and the recomposed manifest carries the new state.

        **Stage ``rows`` only, which is why this verb has no stage logic of its
        own.** :meth:`reopen` is the one door back from ``composed``; refusing
        every other stage here means the two verbs cannot disagree about when a
        spec may change. At ``turnaround`` the answer is ``--states`` on
        ``start``, which has not been spent yet.

        **The spec is REPLACED, never mutated.** :class:`SheetSpec` and
        :class:`StateSpec` are frozen on purpose, and the new state is APPENDED,
        so :meth:`SheetSpec.rows` — which is state-major — keeps every existing
        row at the index the installed manifest already published. The sheet
        grows downward; nothing above the new rows moves.

        **Nothing is written into the revision store.** A row is "seeded" by
        appearing in the spec: its store key has no history, so the status
        payload reports ``attempts: 0`` and lists it under ``missing.rows``,
        which is exactly what an un-generated row looks like everywhere else.
        Writing a placeholder attempt would invent an image nobody drew.

        **What a new row is grounded on depends on the state, and
        :meth:`_row_reference` decides it — not this verb.** A DIRECTIONAL
        state's rows ground on the turnaround reference the operator already
        APPROVED for that direction (``store.current``, never ``store.latest``:
        an operator who rerolls a direction and then keeps the older attempt has
        to get the older attempt). The stage machine guarantees each of those is
        approved, because that approval is the only thing that advances a draft
        to ``rows``. A ``:fixed`` state has ONE row with no direction at all, so
        it grounds on the BASE image, exactly as a fixed row declared at
        ``start`` does. This paragraph said "the turnaround references" for every
        row until 2026-08-25 — ``:fixed`` is advertised in the CLI help and in
        the skill's verb table, and it has never used one.

        **Add only** (owner decision 8). Removing a state would delete approved
        attempts and the operator notes stored with them — the durable QA record
        — for the benefit of a coverage number. If it is ever wanted it is its
        own ``--confirm`` verb, never a flag here.

        *state_text* is parsed by :func:`~agent.charsheet.spec.parse_states`, so
        the grammar, the reserved ``-``, the name shape and the frame range are
        one authority shared with ``start --states``; a state below
        :data:`~agent.charsheet.spec.MIN_FRAMES_PER_ROW` frames is refused HERE,
        rather than four generations later at ``rows``.
        """
        self._require_stage("add_state", "rows")
        # The grammar stays in ONE place; only the SPELLING of the flag being
        # refused travels, because `--states` (plural, with a two-state example)
        # is not a flag this verb has and `add-state` refuses a list one check
        # later. A refusal that names a flag the caller cannot pass is worse
        # than no refusal message at all.
        added = parse_states(state_text, flag="--state", example="jumping:6")
        if len(added) != 1:
            # `--state` is singular and the launcher registry renders one value
            # for it. A comma-separated list would make this a second, quieter
            # spelling of `start --states`, and it would apply half an operator's
            # request under one review. Two states are two calls.
            raise ValueError(
                f"--state takes ONE state, got {len(added)} "
                f"({', '.join(state.name for state in added)}); add them one "
                "call at a time so each new state's rows are reviewed on their own"
            )
        state = added[0]
        spec = self.spec
        existing = [current.name for current in spec.states]
        if state.name in existing:
            raise ValueError(
                f"state {state.name!r} is already on this sheet "
                f"(states: {', '.join(existing)}); add-state only ADDS — to "
                f"redraw its strips use `reroll-row`, and to change its frame "
                "count start a new draft"
            )
        grown = SheetSpec(
            states=spec.states + (state,),
            scheme=spec.scheme,
            frame_w=spec.frame_w,
            frame_h=spec.frame_h,
        )
        self._data["spec"] = spec_to_dict(grown)
        self._save()
        new_rows = [row.key for row in grown.authored_rows() if row.state == state.name]
        logger.info(
            "charsheet draft %s: state %r added (%d frames) → %d new row(s): %s",
            self.id,
            state.name,
            state.frames,
            len(new_rows),
            ", ".join(new_rows),
        )
        return {
            "state": {
                "name": state.name,
                "frames": state.frames,
                "directional": bool(state.directional),
            },
            "states": [current.name for current in grown.states],
            "rows": new_rows,
        }

    # ------------------------------------------------------- looking at rows

    def row_thumb(
        self,
        row_key: str,
        *,
        attempt: int = -1,
        frame: int = DEFAULT_THUMB_FRAME,
        scale: int = DEFAULT_THUMB_SCALE,
        square: bool = False,
    ) -> dict:
        """Write a card-size QA crop of ONE frame of ONE row attempt.

        The §F.2 looking procedure as a verb, and the procedure is *crop, then
        upscale* — in that order, because the crop is the half that removes
        pixels. A row strip shown whole in a chat column is a false negative
        machine: the 2026-08-24 seam was invisible at fit-to-window scale in the
        very strip that carried it, and "I looked at the strip and it's fine" was
        reliably wrong. Enlarging that same strip does not fix it — measured
        live, a whole-strip 2x thumb and the raw attempt are the same picture at
        card width (≤2/255 per channel), while costing 24 MiB decoded against
        the 12 MiB installed sheet the crop exists to avoid decoding.

        So the default is ONE frame cell (:data:`DEFAULT_THUMB_FRAME`), sliced
        from the strip by the row's own frame count, and only then upscaled with
        NEAREST (no filter averages the defect away) onto a flat dark backdrop
        with the chroma field keyed out (a seam over magenta reads as nothing,
        and so does one over transparency). The whole-strip view still exists and
        needs no verb: it is the attempt file itself, which the payload names as
        ``source``.

        Stage-free on purpose: looking is never out of order. A composed draft is
        exactly when an operator goes back to find what went wrong, and refusing
        to render a picture at that point is the wall ``reopen`` was built to
        remove.

        **Two bounds, two booleans, and the payload carries both.** A crop is
        weighed against two different things and they disagree on real drafts,
        so one boolean could never have answered for both — it answered for one
        and was READ as the other:

        * ``withinConsoleBudget`` — the crop is under
          :data:`pipeline.MAX_CONSOLE_CARD_PIXELS`, a FIXED console decode
          ceiling sized once from ``CHAR8``. It does not move with a spec. This
          is the bound that is ENFORCED: a crop taken at
          :data:`DEFAULT_THUMB_SCALE` or below — the crop a caller gets by
          asking for a picture, and the one an agent declares with a ``MEDIA:``
          line — is REFUSED when it exceeds it. A deliberate deeper zoom is a
          different artifact with a different reader: allowed up to the write
          ceiling and labelled ``withinConsoleBudget: false``. A boolean rather
          than a silent clamp, because a caller who asked for 8x wants 8x — they
          just must not be told it is a card.
        * ``withinOwnSheet`` — the crop is no larger than the sheet THIS draft
          composes, from its own ``spec.sheet_size()``
          (:func:`pipeline.fits_own_sheet`). It moves with the draft. Nothing is
          refused on it; it is reported at every scale, because a crop heavier
          than its own sheet is a legal picture that simply mitigated nothing.

        **THE CONSUMER RULE, for the launcher card (B2) and for any agent
        declaring a crop: draw it inline ONLY when BOTH are true. Otherwise
        route it to the fullscreen viewer** — ``withinConsoleBudget: false``
        because the decode would sink the surface, ``withinOwnSheet: false``
        because cropping bought nothing and the card may as well have opened
        the sheet.

        Measured both ways, which is why they are two: a ``--directions 4``,
        ``idle:2`` draft's default crop came back 1774x1774 = 3,147,076 px —
        ``withinConsoleBudget: true``, ``withinOwnSheet: false`` at 13.1x its
        239,616-px sheet; and an ``add-state``-grown sheet (1536x3120 =
        4,792,320 px, 1.50x the fixed budget) can take a crop the other way
        round, over the console ceiling and still lighter than the sheet that
        draft will compose. ``cardSafe``, which this payload carried until
        2026-08-25, was the first of these two wearing the second one's name.

        **``square`` is the hero-card shape, and it is opt-in.** The console
        card is a fixed 1:1 centre-cover square (§13.17, ruled: the card is not
        moving), and a character cell is taller than it is wide — so the default
        crop renders there as a torso zoom, which is real confusion even though
        the card was never the verdict surface. With *square*, the finished crop
        is centred on a square field of the same flat dark backdrop
        (:func:`pipeline.pad_to_square`, side = the longer edge) so the card
        draws the whole frame; the filename gains ``-sq`` and the payload says
        ``square: true``. The DEFAULT stays tall: a compare pair aligns its
        panes, and padding changes the aspect the compare guidance assumes. Use
        ``--square`` for a card, bare crops for a comparison.

        **Both bounds are weighed on the PADDED output**, because padding adds
        pixels and the file a consumer decodes is the padded one. A square crop
        can therefore be refused at the default scale where the bare crop of the
        same cell is fine — the refusal names the padded size, since arguing
        about the unpadded one would be arguing about a file nobody asked for.

        Returns a PATH and never bytes (plan A-4): the launcher runs on this
        machine, and the trace lane that would carry an inline image is capped at
        4 KiB.
        """
        row = self._authored_row(row_key)
        store = self.store
        # This draft's OWN spec, which is the whole point of the second bound:
        # the sheet a crop is weighed against is the one THIS draft composes,
        # never the package's largest.
        spec = self.spec
        key = row_item(row.key)
        if not store.history(key):
            raise ValueError(
                f"row {row.key!r} has no attempt to crop yet; generate it first "
                f"(`characters rows --only {row.key}`)"
            )
        # The store resolves -1 → the newest index and refuses out-of-range, so
        # the number in the filename is the number the payload reports.
        index = store.attempt_index(key, attempt)
        source = store.attempt_path(key, index)
        if source is None or not source.is_file():
            raise ValueError(
                f"attempt {index} of row {row.key!r} has no image on disk"
                + (f" at {source}" if source is not None else "")
            )
        cell = pipeline.frame_cell(source, frame=frame, frames=row.frames)
        # Both bounds are read off the OUTPUT size before anything is allocated:
        # the write ceiling inside `upscale_on_backdrop`, the card budget here,
        # where the default is known. Refusing after the resize would already
        # have paid for the picture nobody may use. The scale is gated first
        # through the same helper `upscale_on_backdrop` uses — weighing an
        # output means multiplying by it, and `512 * "2"` is a string.
        scale = pipeline.require_scale(scale)
        square = bool(square)
        out_w, out_h = cell.width * scale, cell.height * scale
        # The PADDED size when one is coming: `--square` adds margin to the
        # shorter axis, and every number below — both booleans, the refusal, the
        # payload — is about the file a consumer will decode, not about the
        # intermediate crop that is never written.
        if square:
            out_w = out_h = max(out_w, out_h)
        within_console_budget = pipeline.fits_console_budget(out_w, out_h)
        within_own_sheet = pipeline.fits_own_sheet(out_w, out_h, spec)
        if not within_console_budget and scale <= DEFAULT_THUMB_SCALE:
            raise ValueError(
                f"scale {scale} on this {cell.width}x{cell.height} frame of row "
                f"{row.key!r} would write "
                + ("a square " if square else "")
                + f"{out_w}x{out_h} "
                f"= {out_w * out_h:,} pixels, over the "
                f"{pipeline.MAX_CONSOLE_CARD_PIXELS:,}-pixel console budget — the "
                "fixed ceiling on what a chat card may decode, which is NOT a "
                "comparison against this draft's own sheet (the payload answers "
                "that separately as withinOwnSheet); "
                "ask for --scale 1, or a row with more frames to slice, or "
                "--scale 3 or more to take it as a viewer artifact carrying "
                "withinConsoleBudget: false"
                + (", or drop --square to take the cell unpadded" if square else "")
            )
        image = pipeline.upscale_on_backdrop(cell, scale=scale)
        if square:
            # ONE pad step, last: the crop is finished before the margin is
            # added, so nothing the looking procedure did is enlarged, keyed or
            # resampled a second time.
            image = pipeline.pad_to_square(image)
        # The filename is a HUMAN surface — an operator correlating a crop back
        # to the attempt it came from — so it counts the way the store's own
        # filenames count: `walk-n-attempt-3-frame-1-x2.png` sits beside
        # `revisions/row@walk-n/attempt-3.png`. The payload below stays 0-based
        # machine truth. A QA surface relabels; it never renumbers.
        # `-sq` because the two shapes are two artifacts of the same cell: a card
        # crop and a compare crop must be able to sit in the thumbs directory at
        # once, and an operator must be able to tell which is which by name.
        out = self.directory / THUMBS_DIRNAME / (
            f"{row.key}-attempt-{index + 1}-frame-{frame + 1}-x{scale}"
            f"{'-sq' if square else ''}.png"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        image.save(out, format="PNG")
        logger.info(
            "charsheet draft %s: row %s attempt %d frame %d cropped at %dx%d → %s",
            self.id,
            row.key,
            index,
            frame,
            image.width,
            image.height,
            out,
        )
        return {
            "row": row.key,
            "attempt": index,
            "attempts": len(store.history(key)),
            "frame": frame,
            "frames": row.frames,
            "scale": scale,
            # Unconditional, like the two booleans below and for the same
            # reason: a consumer deciding WHERE to draw a crop cannot infer the
            # shape from a filename, and the default is a shape too.
            "square": square,
            "source": str(source),
            "path": str(out),
            "width": image.width,
            "height": image.height,
            # Both, always, at every scale — see the docstring's consumer rule.
            # A consumer that reads one and infers the other is the defect this
            # split exists to retire.
            "withinConsoleBudget": within_console_budget,
            "withinOwnSheet": within_own_sheet,
        }

    # -------------------------------------------------- stage 3: composed

    def reopen(self) -> dict:
        """Reopen a composed draft for fixes; returns to stage ``rows``.

        Nothing is deleted and the installed sheet stays installed: compose
        always re-runs from the approved strips, so the next ``compose`` after
        a fix simply overwrites the install. Without this verb the only path
        back was hand-editing ``draft.json`` (proven live on 2026-08-24).
        """
        self._require_stage("reopen", "composed")
        self._set_stage("rows")
        return {"stage": self.stage}

    def compose(self, accept_handedness: Sequence[str] = ()) -> dict:
        """Compose, validate and install the sheet; advances to ``composed``.

        Refuses unless every authored row has an approved strip: composing from a
        partially approved draft would install a sheet with blank rows that the
        consumer's spec claims are filled.

        *accept_handedness* names ``<row>:<basis>`` tokens whose mirrored-art
        REFUSAL the operator has looked at and is overriding — see
        :func:`pipeline.validate_sheet`, and take the spelling from the refusal
        itself (:func:`pipeline.accept_basis_token`). It is per ROW and never
        blanket, and it applies to both refusing shapes: a row BOTH passes agree
        about (``<row>:rotation+states``) and a row carried by a whole mirrored
        STATE (``<row>:states``), the latter accepted one row at a time like any
        other. A single-basis finding about a single row is a warning and does
        not block, so there is nothing to accept about it; an accepted row that
        was not flagged is itself a refusal; and the honoured list is written
        into the installed manifest as ``{row, gain, basis}`` so the override
        survives as a fact about the character rather than as a refusal nobody
        can see any more.
        """
        self._require_stage("compose", "rows")
        spec = self.spec
        store = self.store

        strips: dict[str, Path] = {}
        missing: list[str] = []
        for row in spec.authored_rows():
            current = store.current(row_item(row.key))
            if current is None:
                missing.append(row.key)
            else:
                strips[row.key] = current
        if missing:
            raise ValueError(
                f"cannot compose draft {self.id}: {len(missing)} row(s) have no "
                f"approved strip ({', '.join(missing)})"
            )

        palette_sources: list[Path] = []
        for direction in pipeline.turnaround_order(spec.scheme.authored):
            ref = store.current(turnaround_item(direction))
            if ref is None:
                raise ValueError(
                    f"cannot compose draft {self.id}: direction {direction!r} has "
                    "no approved reference to take the palette from"
                )
            palette_sources.append(ref)

        cells = pipeline.compose_draft_frames(spec, strips, palette_sources)
        sheet = pipeline.compose_sheet(spec, cells)
        validation = pipeline.validate_sheet(
            spec, sheet, accept_handedness=accept_handedness
        )
        if not validation["ok"]:
            # The handedness accounting rides on the REFUSAL too. Without it the
            # payload carrying "and here are the six rows nobody judged" is
            # discarded at exactly the moment an operator is deciding whether to
            # trust the check.
            # SCOPE FIRST, then the findings, one block apiece. The accounting
            # used to trail a semicolon-joined run-on, so the sentence saying
            # how much of the sheet was actually judged was the last thing on a
            # 1200-character line — furthest from the eye on the surface with
            # the least room. A consumer that shows only the head of this now
            # shows what failed and how far the check could see.
            raise ValueError(
                f"composed sheet for draft {self.id} failed validation.\n"
                + pipeline.handedness_summary(validation["handedness"])
                + "; a refusal is not a full audit.\n\n"
                + "\n\n".join(validation["errors"])
            )

        directory = characters_dir() / _safe_segment(slugify(self.slug))
        # Clobber guard: re-composing THIS draft may overwrite its own install,
        # but a colliding slug from a different draft is a different character.
        existing_manifest = directory / MANIFEST_FILENAME
        if existing_manifest.is_file():
            try:
                prior = json.loads(existing_manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                prior = {}
            prior_draft = str(prior.get("draftId", "")) if isinstance(prior, dict) else ""
            if prior_draft and prior_draft != self.id:
                raise ValueError(
                    f"slug {slugify(self.slug)!r} is already installed from draft "
                    f"{prior_draft}; composing draft {self.id} over it would replace "
                    "a different character — pick another slug"
                )
        directory.mkdir(parents=True, exist_ok=True)
        sheet_path = directory / SHEET_FILENAME
        _write_bytes_atomic(sheet_path, pipeline.atlas_to_webp_bytes(sheet))

        rows = [_row_json(row) for row in spec.rows()]
        manifest = {
            "schema": SCHEMA,
            "slug": slugify(self.slug),
            "displayName": self.display_name,
            "concept": self.concept,
            "style": self.style,
            "spec": spec_to_dict(spec),
            "rows": rows,
            "frameW": spec.frame_w,
            "frameH": spec.frame_h,
            "loopMs": LOOP_MS,
            "scale": DEFAULT_SCALE,
            "generator": "charsheet",
            "draftId": self.id,
            "created": _utc_now(),
        }
        if validation["handedness"].get("accepted"):
            # ``{row, gain, basis}``, not bare row keys: accepting a +40% finding
            # and an +8.1% one used to be indistinguishable the moment the
            # compose was over, and this manifest is the only place the fact
            # survives. ``sprite_payload`` and ``characters list`` both republish
            # it, so no consumer has to open this file to learn that a character
            # carries a mirrored row its operator looked at and accepted.
            manifest["handednessAccepted"] = [
                dict(entry) for entry in validation["handedness"]["accepted"]
            ]
        _write_json_atomic(directory / MANIFEST_FILENAME, manifest)
        self._set_stage("composed")
        logger.info(
            "charsheet draft %s composed → %s (%dx%d)",
            self.id,
            sheet_path,
            validation["width"],
            validation["height"],
        )
        return {
            "slug": manifest["slug"],
            "displayName": manifest["displayName"],
            "directory": str(directory),
            "sheet": str(sheet_path),
            "manifest": str(directory / MANIFEST_FILENAME),
            "validation": validation,
            "rows": rows,
            "stage": self.stage,
        }

    # ---------------------------------------------------------- reporting

    def status_payload(self) -> dict:
        """Everything a QA UI needs: stage, spec summary, per-item history.

        JSON-safe by construction. ``current`` is the approved image when there is
        one and the latest attempt otherwise, so a pending item is still
        displayable.

        **Every path in this payload is a ``str`` or JSON ``null``** — including
        ``baseImage``, which is the one ``path_or_none`` did NOT reach when the
        rule was first written. It answered ``""`` for a draft with no base image
        beside ``authoredBy: null`` and ``history[].path: null`` in the same
        response, which is exactly the two-spellings defect the helper exists to
        retire, one field later. ``list`` carries the same field
        (``_characters_draft_summary``) and answers the same way.
        """
        spec = self.spec
        store = self.store
        width, height = spec.sheet_size()
        base = self.base_image

        turnaround = {
            direction: self._item_status(store, turnaround_item(direction))
            for direction in pipeline.turnaround_order(spec.scheme.authored)
        }
        rows = {
            row.key: self._item_status(store, row_item(row.key))
            for row in spec.authored_rows()
        }
        return {
            "schema": SCHEMA,
            "id": self.id,
            "slug": self.slug,
            "displayName": self.display_name,
            "concept": self.concept,
            "style": self.style,
            "authoredBy": self.authored_by,
            # The two provenance fields travel together, and both spell absence
            # `null`. `hermesHome` is a PATH field, so it is also bound by the
            # rule this docstring states: a `str` or JSON `null`, never `""`.
            "hermesHome": self.hermes_home,
            "stage": self.stage,
            "stages": list(STAGES),
            "created": str(self._data.get("created", "")),
            "updated": str(self._data.get("updated", "")),
            "baseImage": path_or_none(base),
            "spec": {
                **spec_to_dict(spec),
                "rows": [_row_json(row) for row in spec.rows()],
                "sheetWidth": width,
                "sheetHeight": height,
            },
            "turnaround": turnaround,
            "rows": rows,
            "pending": {
                "turnaround": [
                    direction for direction, item in turnaround.items() if item["approved"] is None
                ],
                "rows": [key for key, item in rows.items() if item["approved"] is None],
            },
            "missing": {
                "turnaround": [
                    direction for direction, item in turnaround.items() if not item["attempts"]
                ],
                "rows": [key for key, item in rows.items() if not item["attempts"]],
            },
        }

    @staticmethod
    def _item_status(store: ImageRevisionStore, key: str) -> dict:
        """One QA item: its counts, its current image, and every attempt's file.

        ``history[].path`` is the store's own answer for that index, not a
        filename re-derived from the attempt number — a QA surface that wants to
        show attempt 2 beside attempt 3 has to address them individually, and
        re-spelling the store's layout here is how the two would drift apart.

        **Every path here is a ``str`` or JSON ``null``, never ``""``.** Same
        reasoning as ``authored_by`` above, and the same payload: absence is a
        fact a consumer must be able to READ. An empty string is not a path, and
        a consumer that receives one cannot tell "no image was recorded for this
        attempt" from any other empty value — while an agent following the
        ``MEDIA:<path>`` protocol interpolates it and emits a bare ``MEDIA:``
        line. ``attempt_path``/``current``/``latest`` all return a typed
        ``Path | None``; flattening that at the payload boundary destroyed the
        only distinction the store took care to make.
        """
        history = store.history(key)
        approved = store.approved_index(key)
        approved_path = store.current(key)
        # A pending item's newest attempt is what QA has to look at.
        current = approved_path if approved_path is not None else store.latest(key)
        return {
            "key": key,
            "attempts": len(history),
            "approved": approved,
            "approvedPath": path_or_none(approved_path),
            "current": path_or_none(current),
            "rejected": [i for i, record in enumerate(history) if record.get("rejected")],
            "history": [
                {
                    "attempt": index,
                    "path": path_or_none(store.attempt_path(key, index)),
                    "note": str(record.get("note", "")),
                    "created": str(record.get("created", "")),
                    "rejected": bool(record.get("rejected")),
                }
                for index, record in enumerate(history)
            ],
        }


def _row_json(row) -> dict:
    return {
        "row": row.index,
        "state": row.state,
        "direction": row.direction,
        "frames": row.frames,
        "key": row.key,
    }


# ──────────────────────────── installed sheets ────────────────────────────


def _sheet_revision(path: Path) -> str:
    """``mtime_ns:size`` — the pet payload's cache key, same meaning."""
    try:
        stat = path.stat()
    except OSError:
        return ""
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def sprite_payload(slug: str) -> dict:
    """The launcher payload for an installed character.

    Field names and meanings match the pet sprite payload where they overlap, so
    the Dart client's parse path is unchanged; the additions (``directions``,
    ``states``, ``rows``) are what let a consumer read a directional sheet
    without inferring the taxonomy from its height — the pet inference trap
    (§0.4). There is no ``framesPerState``: character rows carry true per-row
    counts only.

    ``framesByRow``, ``stateRows`` and ``rows`` describe the AUTHORED rows only
    (ten of them for ``CHAR8``), because since ruling 3-B those are the only
    rows in the sheet. ``directions.mirrored`` still names the flips the consumer
    derives: the launcher needs the SECTORS, and the row names carry them.

    Row keys and the ``stateRows`` order follow the launcher spec (EterniaLauncher
    ``docs/spatial/CHARACTER_8WAY_SPRITE_FORMAT_SPEC_2026-08-17.md``): a directional
    row is ``<state>-<direction>``, and row 0 is the front-facing idle.
    """
    safe = _safe_segment(slugify(slug))
    directory = characters_dir() / safe
    manifest_path = directory / MANIFEST_FILENAME
    sheet_path = directory / SHEET_FILENAME
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"character {slug!r} is not installed: no manifest at {manifest_path}"
        )
    if not sheet_path.is_file():
        raise FileNotFoundError(
            f"character {slug!r} is installed but has no sheet at {sheet_path}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"corrupt manifest {manifest_path}: {exc}") from exc

    spec = spec_from_dict(manifest.get("spec") or {})
    rows = [_row_json(row) for row in spec.rows()]
    raw = sheet_path.read_bytes()
    return {
        "slug": str(manifest.get("slug", safe)),
        "displayName": str(manifest.get("displayName", "") or safe),
        "mime": "image/webp",
        "spritesheetBase64": base64.standard_b64encode(raw).decode("ascii"),
        "spritesheetRevision": _sheet_revision(sheet_path),
        "frameW": spec.frame_w,
        "frameH": spec.frame_h,
        "framesByRow": {row.key: row.frames for row in spec.rows()},
        "loopMs": int(manifest.get("loopMs", LOOP_MS)),
        "scale": float(manifest.get("scale", DEFAULT_SCALE)),
        "directions": {
            "order": list(spec.scheme.order),
            "authored": list(spec.scheme.authored),
            "mirrored": dict(spec.scheme.mirrored),
        },
        "states": [
            {"name": state.name, "frames": state.frames, "directional": bool(state.directional)}
            for state in spec.states
        ],
        "rows": rows,
        "stateRows": [row["key"] for row in rows],
        # The one fact about this sheet the pixels cannot carry: an operator
        # looked at a two-basis mirrored-art refusal and overrode it, per row,
        # with its gain and its bases. Empty for every character composed
        # without an override, which is nearly all of them. It rides here so a
        # consumer that byte-copies the sheet (the launcher's
        # `bundle_character.dart` decodes nothing) can still read it; whether
        # that consumer refuses, warns or records is its own ruling, but it
        # could not make one at all while this lived only inside the manifest.
        "handednessAccepted": _handedness_accepted(manifest),
    }


def _handedness_accepted(manifest: dict) -> list[dict]:
    """The manifest's accepted mirrored-art findings, JSON-safe and total.

    Tolerates the ROUND-TWO spelling — a bare list of row keys — because a
    character installed by that build is still installed, and a payload that
    raised on it would take the whole sprite down over a provenance field.
    """
    raw = manifest.get("handednessAccepted") or []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if isinstance(entry, dict):
            out.append(
                {
                    "row": str(entry.get("row", "")),
                    "gain": float(entry.get("gain", 0.0) or 0.0),
                    "basis": str(entry.get("basis", "") or "unrecorded"),
                }
            )
        else:
            out.append({"row": str(entry), "gain": 0.0, "basis": "unrecorded"})
    return out
