from __future__ import annotations

import json
import os
import tempfile
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
from pathlib import Path
from types import NoneType, UnionType
from typing import Any, get_args, get_origin, get_type_hints


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, datetime):
        value = value.astimezone(timezone.utc)
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, frozenset):
        return [to_jsonable(item) for item in sorted(value, key=str)]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    return value


def from_jsonable(cls: type[Any], raw: Any) -> Any:
    return _coerce(cls, raw)


def upgrade(raw: dict[str, Any]) -> dict[str, Any]:
    """Schema upgrade hook. Stage 1 only ships v1 records."""
    if not isinstance(raw, dict):
        return raw
    version = raw.get("schema_version", 1)
    if version != 1:
        raise ValueError(f"unsupported schema_version: {version}")
    return raw


@lru_cache(maxsize=None)
def _dataclass_type_hints(cls: type[Any]) -> dict[str, Any]:
    return get_type_hints(cls)


def _coerce(annotation: Any, raw: Any) -> Any:
    if raw is None:
        return None

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is UnionType or origin is getattr(__import__("typing"), "Union"):
        non_none = [arg for arg in args if arg is not NoneType]
        if raw is None:
            return None
        for arg in non_none:
            try:
                return _coerce(arg, raw)
            except Exception:
                continue
        return raw

    if origin is list:
        item_type = args[0] if args else Any
        return [_coerce(item_type, item) for item in raw]

    if origin is dict:
        value_type = args[1] if len(args) == 2 else Any
        return {str(key): _coerce(value_type, value) for key, value in raw.items()}

    if origin is frozenset:
        item_type = args[0] if args else Any
        return frozenset(_coerce(item_type, item) for item in raw)

    if annotation is Any:
        return raw

    if annotation is datetime:
        if isinstance(raw, datetime):
            return raw
        text = str(raw)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)

    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(raw)

    if isinstance(annotation, type) and is_dataclass(annotation):
        upgraded = upgrade(raw)
        hints = _dataclass_type_hints(annotation)
        kwargs = {}
        for field in fields(annotation):
            if field.name in upgraded:
                kwargs[field.name] = _coerce(hints.get(field.name, Any), upgraded[field.name])
        return annotation(**kwargs)

    return raw


# ---------------------------------------------------------------------------
# Wire-shape coercion — ONE home, so the next helper has somewhere to land.
#
# Pass 2 of the dead-code audit found four of these copied across the
# transport/projection cluster, three of them hidden by word-order-inverted or
# differently-abbreviated names (`_atomic_write_json` vs `_write_json_atomic`;
# `_text` vs `_optional_text`). A duplicate is only invisible while it has
# nowhere obvious to live. This module is a stdlib-only leaf with no relative
# imports, so anything in the package may depend on it without risking a
# cycle — which is exactly why it is the home.
# ---------------------------------------------------------------------------


def section_rows(value: Any) -> list:
    """A snapshot section S4 emits as an id-keyed map, read as an ordered list
    of rows (map values).

    Also accepts a plain list (sections S4 does not key) and ``None`` (absent
    section), so a reader never has to branch on which shape it got.
    """

    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, list):
        return list(value)
    return []


def optional_text(value: Any) -> str | None:
    """``None`` in, ``None`` out; blank-after-strip is also ``None``.

    The honest name for what three copies of ``_text`` were doing: the return
    is optional, and a whitespace-only wire value is an absent one.
    """

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def safe_id(value: Any) -> str | None:
    """Sanitize a wire id into one safe to use as a filename or map key.

    ``None`` for anything that sanitizes away entirely, so a caller cannot
    silently address a row keyed on the empty string.
    """

    text = str(value or "").strip()
    if not text:
        return None
    cleaned = "".join(ch if ch.isalnum() or ch in "_.:-" else "_" for ch in text)
    return cleaned.strip("._:-")[:120] or None


def write_json_atomic(path: Path, record: dict[str, Any]) -> None:
    """tmp + rename, so a reader never sees a half-written record.

    The temp file is created in the DESTINATION directory (``os.replace`` is
    only atomic within a filesystem) and is unlinked on any failure, including
    ``KeyboardInterrupt`` — hence ``BaseException``.

    NOT the same helper as upstream's ``utils.atomic_json_write``, and it must
    not be folded into it. That one fsyncs and preserves mode/owner (correct
    for secret-bearing files) but opens the temp file in Python's default text
    mode, so on Windows it writes CRLF. Every record written through THIS one —
    the serve registry, the socket owner lock — is read back and compared as
    LF-canonical bytes, which is why ``newline="\\n"`` is pinned here. Two
    writers, two different guarantees, deliberately.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, default=str, indent=2) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(payload)
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
