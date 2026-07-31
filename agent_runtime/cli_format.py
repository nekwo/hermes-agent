from __future__ import annotations

import json

from .serde import to_jsonable


def emit_json(data) -> str:
    return json.dumps(to_jsonable(data), indent=2, ensure_ascii=False, sort_keys=True)
