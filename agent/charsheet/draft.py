"""Staged character drafts — the QA state machine, the install, the payload.

A draft is a directory under ``$HERMES_HOME/characters/.drafts/<id>/`` holding
``draft.json`` (schema 1), the base identity image, an
:class:`~agent.charsheet.revisions.ImageRevisionStore` of every attempt, and the
accepted row strips. Installing writes
``$HERMES_HOME/characters/<slug>/{character.json, sheet.webp}`` — profile-scoped
and plain-hermes compatible, the same convention as ``agent.pet.store``.

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
)
from agent.pet.constants import DEFAULT_SCALE, LOOP_MS
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

SCHEMA = 1

# turnaround → rows → composed. Order is the tuple order; nothing branches on a
# stage count.
STAGES: tuple[str, ...] = ("turnaround", "rows", "composed")

DRAFTS_DIRNAME = ".drafts"
DRAFT_FILENAME = "draft.json"
MANIFEST_FILENAME = "character.json"
SHEET_FILENAME = "sheet.webp"
REVISIONS_DIRNAME = "revisions"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


# ─────────────────────────────── locations ───────────────────────────────


def characters_dir() -> Path:
    """The profile-scoped characters directory (created on demand)."""
    path = get_hermes_home() / "characters"
    path.mkdir(parents=True, exist_ok=True)
    return path


def drafts_dir() -> Path:
    """Where in-progress drafts live (created on demand)."""
    path = characters_dir() / DRAFTS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


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
    """Revision-store key for a row strip (``row@walk@e``)."""
    return f"row@{key}"


def _strip_filename(key: str, attempt: int) -> str:
    return f"{key.replace('@', '-')}-{attempt}.png"


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
    ) -> CharacterDraft:
        """Start a draft at stage ``turnaround``.

        *base_image* is the identity anchor everything is grounded on; it is
        copied into the draft so a later stage can never be invalidated by the
        caller moving or deleting the original. It may be supplied later
        (:meth:`set_base_image`) — the base-draft pick flow has not chosen one
        yet at ``characters start`` time — but no generation verb runs without it.
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
                "derived at compose and are never QA items"
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

        *only* restricts the run to the given row keys (``["walk@e"]``).
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

    # -------------------------------------------------- stage 3: composed

    def compose(self) -> dict:
        """Compose, validate and install the sheet; advances to ``composed``.

        Refuses unless every authored row has an approved strip: composing from a
        partially approved draft would install a sheet with blank rows that the
        consumer's spec claims are filled.
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
        validation = pipeline.validate_sheet(spec, sheet)
        if not validation["ok"]:
            raise ValueError(
                f"composed sheet for draft {self.id} failed validation: "
                + "; ".join(validation["errors"])
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

        JSON-safe by construction (paths are strings). ``current`` is the approved
        image when there is one and the latest attempt otherwise, so a pending item
        is still displayable.
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
            "stage": self.stage,
            "stages": list(STAGES),
            "created": str(self._data.get("created", "")),
            "updated": str(self._data.get("updated", "")),
            "baseImage": str(base) if base else "",
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
        history = store.history(key)
        approved = store.approved_index(key)
        approved_path = store.current(key)
        # A pending item's newest attempt is what QA has to look at.
        current = approved_path if approved_path is not None else store.latest(key)
        return {
            "key": key,
            "attempts": len(history),
            "approved": approved,
            "approvedPath": str(approved_path) if approved_path else "",
            "current": str(current) if current else "",
            "rejected": [i for i, record in enumerate(history) if record.get("rejected")],
            "history": [
                {
                    "attempt": index,
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
    ``states``, ``rows``) are what let a consumer read a 16-row sheet without
    inferring the taxonomy from its height — the pet inference trap (§0.4). There
    is no ``framesPerState``: character rows carry true per-row counts only.
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
    }
