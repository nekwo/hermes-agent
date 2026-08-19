from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
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
