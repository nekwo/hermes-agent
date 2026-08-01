"""Fractional order-key math for board cards — a pure, unit-tested seam.

A card's ``order_key`` is a base-36 fraction string compared lexicographically.
A move allocates the midpoint between its new neighbours, so a drag rewrites one
card, never a whole column, and two machines inserting concurrently interleave
instead of colliding. Equal keys tiebreak by ``card_id`` ascending, deterministic
on every machine. When the key space between two neighbours is exhausted (keys
grow without bound under adversarial repeated midpoint inserts at one spot) the
column is rebalanced in a single sweep.
"""

from __future__ import annotations

DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
BASE = len(DIGITS)  # 36

# A key longer than this signals pathological midpoint growth → rebalance.
MAX_KEY_LENGTH = 24


def _digit_value(text: str, index: int, *, default: int) -> int:
    if index < len(text):
        return DIGITS.index(text[index])
    return default


def allocate_between(before: str | None, after: str | None) -> str:
    """A key strictly between ``before`` and ``after`` (both exclusive).

    ``before=None`` means "start of column" (lower bound), ``after=None`` means
    "end of column" (open upper bound). Raises ``ValueError`` if the neighbours
    are out of order — the store passes ordered neighbours, so this is a guard.
    """

    lo = before or ""
    hi = after
    if hi is not None and lo >= hi:
        raise ValueError(f"order keys out of order: {before!r} !< {after!r}")
    prefix: list[str] = []
    index = 0
    while True:
        lo_d = _digit_value(lo, index, default=0)
        hi_d = _digit_value(hi, index, default=BASE) if hi is not None else BASE
        if lo_d == hi_d:
            # Neighbours share this digit; descend and keep the shared digit.
            prefix.append(DIGITS[lo_d])
            index += 1
            continue
        mid = (lo_d + hi_d) // 2
        if mid > lo_d:
            prefix.append(DIGITS[mid])
            return "".join(prefix)
        # Adjacent digits (hi_d == lo_d + 1): no room here. Keep lo's digit and
        # descend with the upper bound now open — every further digit stays
        # above ``before`` and below ``after``.
        prefix.append(DIGITS[lo_d])
        index += 1
        hi = None


def _encode(value: int, width: int) -> str:
    out = []
    for _ in range(width):
        value, rem = divmod(value, BASE)
        out.append(DIGITS[rem])
    return "".join(reversed(out))


def rebalance(count: int) -> list[str]:
    """``count`` strictly-increasing, evenly-spaced fixed-width keys.

    Used to reset a column whose keys have grown/collided. Fixed width keeps
    every key short and mutually ordered; the caller reassigns them in the
    column's current (order_key, card_id) order so the visible order is
    preserved.
    """

    if count <= 0:
        return []
    width = 1
    while BASE ** width < count + 2:
        width += 1
    span = BASE ** width
    step = span // (count + 1)
    keys = [_encode(min(span - 1, (i + 1) * step), width) for i in range(count)]
    # Guarantee strict monotonicity even if integer division collided at the
    # low end (tiny spans): nudge any equal/inverted neighbour up by one.
    for i in range(1, len(keys)):
        if keys[i] <= keys[i - 1]:
            keys[i] = _encode(min(span - 1, DIGITS.index(keys[i - 1][-1]) + (i + 1)), width)
    return keys


# S54 removed ``needs_rebalance``: a rebalance predicate no board write path
# consults. ``rebalance``/``MAX_KEY_LENGTH`` stay -- they have live callers.

