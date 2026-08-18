"""Generic, filesystem-backed image revision store.

An :class:`ImageRevisionStore` holds *items* keyed by opaque strings.  Each item
is an append-only list of *attempts* (an image file, an operator note, a
timestamp) with at most one *approved* attempt.  The store never opens, decodes
or interprets the images — bytes are copied verbatim — and it never parses the
keys.  Charsheet mounts it with keys like ``turnaround@n`` / ``row@walk-e``;
Eternia Studio can mount the same store over any image set with its own keys.
Nothing in this module knows what a character, a sheet or a direction is.

Disk layout, one directory per item::

    <root>/<key>/attempt-1.png
    <root>/<key>/attempt-2.png
    <root>/<key>/state.json      {"schema": 1,
                                  "attempts": [{"file": "attempt-1.png",
                                                "note": "...",
                                                "created": "<iso8601 UTC>",
                                                "rejected": true}],
                                  "approved": <int index or null>}

**Key constraint.** Keys must match ``[a-z0-9_@-]+``; anything else is rejected
with :class:`ValueError`.  That character set is directory-safe on Windows and
POSIX alike and contains no case variants, path separators, drive/extension
punctuation or trailing dots/spaces, so the key→directory mapping is the
identity — no escaping, therefore no two distinct keys can collide on one
directory.  The only names inside the set that are not usable as directories are
the Windows reserved device names (``con``, ``nul``, ``com1`` …); those are
rejected too rather than failing later with an opaque ``OSError``.

**Atomicity.** Every ``state.json`` write goes to a temporary file in the *same*
directory, is flushed and fsynced, and is then moved into place with
:func:`os.replace` — readers therefore see either the whole previous state or
the whole next one, never a partial file.  Image bytes are copied (also via
tmp + ``os.replace``) *before* the state that names them is written.  The
invariant is one-directional: a crash in the write window can leave an
unreferenced image file or a stray ``*.tmp`` behind, but state can never
reference an image that is missing or half-written.  Stray temporary files are
ignored on read and the attempt slot is simply reused by the next ``propose``.

All reads hit the disk fresh — the store caches nothing between calls, so
several store instances (or processes) over one root always agree.  Concurrent
writers are last-writer-wins per item; there is no lock (by design: this module
must not import ``agent_runtime``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1

STATE_FILENAME = "state.json"

_KEY_RE = re.compile(r"^[a-z0-9_@-]+$")

_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,8}$")

_TMP_SUFFIX = ".tmp"

_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{digit}" for digit in "0123456789"}
    | {f"lpt{digit}" for digit in "0123456789"}
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _attempt_suffix(image_path: Path) -> str:
    suffix = image_path.suffix.lower()
    return suffix if _SUFFIX_RE.match(suffix) else ".png"


def _empty_state() -> dict:
    return {"schema": SCHEMA, "attempts": [], "approved": None}


class ImageRevisionStore:
    """Attempt/approval history for images, keyed by opaque strings."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- keys

    @staticmethod
    def validate_key(key: str) -> str:
        """Return ``key`` unchanged, or raise :class:`ValueError` with a reason."""
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise ValueError(
                f"invalid item key {key!r}: keys must be non-empty and match [a-z0-9_@-]+"
            )
        if key in _RESERVED_NAMES:
            raise ValueError(
                f"invalid item key {key!r}: reserved device name, unusable as a directory"
            )
        return key

    def keys(self) -> list[str]:
        """Every item key present on disk, sorted."""
        if not self.root.is_dir():
            return []
        found = []
        for child in self.root.iterdir():
            name = child.name
            if not child.is_dir() or not _KEY_RE.match(name) or name in _RESERVED_NAMES:
                continue
            if (child / STATE_FILENAME).is_file():
                found.append(name)
        return sorted(found)

    def pending(self) -> list[str]:
        """Keys with at least one attempt and no approval, sorted."""
        waiting = []
        for key in self.keys():
            state = self._read_state(key)
            if state["attempts"] and state["approved"] is None:
                waiting.append(key)
        return waiting

    # ------------------------------------------------------------- reading

    def history(self, key: str) -> list[dict]:
        """The item's attempts as recorded; ``[]`` for a key with no history."""
        return [dict(record) for record in self._read_state(key)["attempts"]]

    def current(self, key: str) -> Path | None:
        """Path of the approved attempt's image, or ``None`` if nothing is approved."""
        state = self._read_state(key)
        index = state["approved"]
        if index is None:
            return None
        return self._item_dir(key) / str(state["attempts"][index]["file"])

    def approved_index(self, key: str) -> int | None:
        """Index of the approved attempt, or ``None``."""
        return self._read_state(key)["approved"]

    def latest(self, key: str) -> Path | None:
        """Path of the most recent attempt's image, approved or not.

        A pending item has no ``current()`` but is exactly what a QA surface must
        display, so the newest attempt needs a public path — callers must not
        reach into the on-disk layout for it.
        """
        state = self._read_state(key)
        if not state["attempts"]:
            return None
        return self._item_dir(key) / str(state["attempts"][-1]["file"])

    # ------------------------------------------------------------- writing

    def propose(self, key: str, image_path: Path, note: str = "") -> int:
        """Copy ``image_path`` into the next attempt slot; return its 0-based index.

        Proposing is always allowed and always clears any approval: a new
        candidate reopens QA for the item.
        """
        directory = self._item_dir(key)
        source = Path(image_path)
        if not source.is_file():
            raise ValueError(f"image_path {source} is not an existing file")
        directory.mkdir(parents=True, exist_ok=True)
        state = self._read_state(key)
        index = len(state["attempts"])
        filename = f"attempt-{index + 1}{_attempt_suffix(source)}"
        self._copy_into(source, directory, filename)
        state["attempts"].append(
            {"file": filename, "note": str(note), "created": _utc_now()}
        )
        state["approved"] = None
        self._write_state(key, state)
        return index

    def approve(self, key: str, attempt: int = -1) -> int:
        """Approve an attempt (``-1`` = latest); return the approved 0-based index."""
        state = self._read_state(key)
        index = self._resolve(key, state, attempt)
        record = state["attempts"][index]
        if record.get("rejected"):
            raise ValueError(
                f"attempt {index} of {key!r} was rejected and cannot be approved"
            )
        image = self._item_dir(key) / str(record.get("file", ""))
        if not image.is_file():
            raise ValueError(f"attempt {index} of {key!r} has no image on disk at {image}")
        state["approved"] = index
        self._write_state(key, state)
        return index

    def reject(self, key: str, attempt: int) -> None:
        """Flag an attempt rejected.

        History is immutable: nothing is deleted, the attempt simply becomes
        ineligible for approval.  Rejecting the currently approved attempt also
        clears the approval, so the recorded state never contradicts itself.
        """
        state = self._read_state(key)
        index = self._resolve(key, state, attempt)
        state["attempts"][index] = {**state["attempts"][index], "rejected": True}
        if state["approved"] == index:
            state["approved"] = None
        self._write_state(key, state)

    # ------------------------------------------------------------ internals

    def _item_dir(self, key: str) -> Path:
        return self.root / self.validate_key(key)

    def _resolve(self, key: str, state: dict, attempt: int) -> int:
        attempts = state["attempts"]
        if not attempts:
            raise ValueError(f"unknown key {key!r}: no attempts have been proposed")
        if isinstance(attempt, bool) or not isinstance(attempt, int):
            raise ValueError(f"attempt must be an int, got {attempt!r}")
        index = attempt + len(attempts) if attempt < 0 else attempt
        if not 0 <= index < len(attempts):
            raise IndexError(
                f"attempt {attempt} out of range for {key!r}: {len(attempts)} attempt(s) recorded"
            )
        return index

    def _read_state(self, key: str) -> dict:
        path = self._item_dir(key) / STATE_FILENAME
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _empty_state()
        try:
            loaded = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"corrupt state file {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"corrupt state file {path}: expected a JSON object")
        attempts = [
            record for record in loaded.get("attempts") or [] if isinstance(record, dict)
        ]
        approved = loaded.get("approved")
        if isinstance(approved, bool) or not isinstance(approved, int):
            approved = None
        elif not 0 <= approved < len(attempts):
            approved = None
        return {
            "schema": loaded.get("schema", SCHEMA),
            "attempts": [dict(record) for record in attempts],
            "approved": approved,
        }

    def _write_state(self, key: str, state: dict) -> None:
        directory = self._item_dir(key)
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": SCHEMA,
            "attempts": state["attempts"],
            "approved": state["approved"],
        }
        fd, tmp_name = tempfile.mkstemp(
            dir=directory, prefix=f"{STATE_FILENAME}.", suffix=_TMP_SUFFIX
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, directory / STATE_FILENAME)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _copy_into(self, source: Path, directory: Path, filename: str) -> None:
        fd, tmp_name = tempfile.mkstemp(
            dir=directory, prefix=f"{filename}.", suffix=_TMP_SUFFIX
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as target, source.open("rb") as handle:
                shutil.copyfileobj(handle, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(tmp, directory / filename)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
