from __future__ import annotations

import json

from .serde import to_jsonable


def emit_json(data) -> str:
    return json.dumps(to_jsonable(data), indent=2, ensure_ascii=False, sort_keys=True)


def emit_json_line(data) -> str:
    """ONE line of the same JSON :func:`emit_json` writes.

    A verb that emits a payload per stage as the stage lands is a stream, and a
    stream's framing is the newline — so its encoder may not be the indenting
    one. Everything else is deliberately identical: the same ``to_jsonable``
    normalization, the same key order, the same escaping, so a consumer that
    parses a line here and a block there gets the same object out of both.

    The return value can never contain a newline: ``json.dumps`` escapes a
    ``\\n`` inside a string value as ``\\\\n``, which is what makes a payload
    carrying a multi-line refusal (charsheet ``compose`` writes several) safe to
    frame this way at all.
    """
    return json.dumps(
        to_jsonable(data), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
