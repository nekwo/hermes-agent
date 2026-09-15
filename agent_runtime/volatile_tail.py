"""The runtime-context envelope's VOLATILE TAIL, as a registered, budgeted value object.

Why this module exists
----------------------
The tail is the one part of the per-turn envelope that is emitted on EVERY
delivery — snapshot, ``unchanged`` and ``unavailable`` alike (see
``runtime_hud.render_runtime_context_envelope``). It is where a fact goes when it
must be true THIS turn: the wall-clock countdown, this lane's capability account,
the MCP servers this turn did not get.

Until this module it was assembled by hand at the mission-chat command::

    volatile_lines = [
        render_turn_budget_line(wall_budget),
        render_capability_block(capability),
        mission_chat_admission_line(persona, session_id=session_id),
    ]
    volatile_content = "\\n".join(line for line in volatile_lines if line)

That has three structural problems, all of which are the failure classes this
harness has already paid for elsewhere:

1. **No roster.** A contributor is a positional entry in an anonymous list. Who
   contributes to the tail — and therefore what an agent is guaranteed to be
   told each turn — is knowable only by reading the CLI body.
2. **No bound.** Every contributor renders whatever it likes. A capability
   account with a widened toolset set, or an admission line naming twenty
   servers, silently turns three bullets into a wall that competes with the
   operator's actual message for the model's attention.
3. **No accounting.** If a bound were ever added the naive form of it is a
   slice, and a silent truncation on the ONE channel whose whole purpose is
   "what is true this turn" is worse than no channel: the agent reads a partial
   capability account as a complete one.

This module fixes all three at once. Each contributor REGISTERS by name with its
own byte budget; over-budget content is truncated or dropped, and either way the
shortfall is stated IN BAND (the agent reads that it was not told everything)
and recorded as a typed accounting row (the operator can see it too). Nothing is
ever dropped silently — the same no-silent-caps rule the roster/capability caps
in ``runtime_hud`` already follow.

Budgets are per contributor, not global, on purpose: a long capability account
must not be able to squeeze out the wall-budget line, and a chatty admission
line must not be able to squeeze out the capability account. Each fact owns its
own room.

Pure and stdlib-only: no I/O, no policy. Contributors resolve their own content
from their own authorities and hand it here already rendered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

#: Emitted whole, within budget.
STATUS_EMITTED = "emitted"
#: The contributor had nothing to say. Honest silence costs nothing and is not a
#: shortfall — a lane with no drops and no denials pays no line.
STATUS_EMPTY = "empty"
#: Over budget; a prefix was emitted plus an in-band truncation note.
STATUS_TRUNCATED = "truncated"
#: Over budget and too small to truncate usefully; only the in-band note was
#: emitted. The agent is told the fact EXISTS and that it did not fit — never
#: left to read the absence as "nothing to report".
STATUS_DROPPED = "dropped"

#: Below this many bytes a truncated body is no longer a useful partial account —
#: a fragment of a capability bullet reads as a complete sentence and misleads.
#: Under it we drop the body and emit only the note.
TRUNCATION_MIN_BODY_BYTES = 160


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def _truncate_bytes(text: str, limit: int) -> str:
    """Longest UTF-8-safe prefix of ``text`` that fits in ``limit`` bytes."""

    if limit <= 0:
        return ""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


@dataclass(frozen=True, slots=True)
class VolatileContribution:
    """One registered contributor to the tail: a name, its content, its budget.

    ``name`` is the stable identifier used in the accounting row and in the
    in-band shortfall note, so an operator reading either can tell WHICH fact was
    clipped without matching on prose.
    """

    name: str
    content: str
    budget_bytes: int


@dataclass(frozen=True, slots=True)
class VolatileTailEntry:
    """What actually happened to one contributor. The accounting, typed.

    ``short`` is the single question every consumer asks: was the agent told
    less than this contributor had to say?
    """

    name: str
    status: str
    budget_bytes: int
    original_bytes: int
    emitted_bytes: int

    @property
    def short(self) -> bool:
        return self.status in (STATUS_TRUNCATED, STATUS_DROPPED)

    def row(self) -> dict[str, Any]:
        """``{name, status, budget_bytes, original_bytes, emitted_bytes}`` — the
        same flat typed-row shape the rest of the runtime's accounting emits, so
        an observability consumer needs no new case."""

        return {
            "name": self.name,
            "status": self.status,
            "budget_bytes": self.budget_bytes,
            "original_bytes": self.original_bytes,
            "emitted_bytes": self.emitted_bytes,
        }


@dataclass(frozen=True, slots=True)
class VolatileTail:
    """The composed tail plus the full account of how it was composed."""

    content: str
    entries: tuple[VolatileTailEntry, ...] = ()

    @property
    def total_bytes(self) -> int:
        return _byte_len(self.content)

    @property
    def shortfalls(self) -> tuple[VolatileTailEntry, ...]:
        """Contributors the agent was told less than in full."""

        return tuple(entry for entry in self.entries if entry.short)

    @property
    def complete(self) -> bool:
        """True when every contributor said everything it had to say."""

        return not self.shortfalls

    def rows(self) -> list[dict[str, Any]]:
        """Accounting rows for every contributor, including the silent ones.

        Includes ``empty`` contributors deliberately: "the capability account had
        nothing to say" and "the capability account was dropped" are different
        facts, and a row set that only listed what was emitted could not tell
        them apart.
        """

        return [entry.row() for entry in self.entries]


def _shortfall_note(entry: VolatileTailEntry) -> str:
    """The IN-BAND note an agent reads when it was not told everything.

    Written for the model, not the operator: it names the fact, says plainly
    that the account is incomplete, and closes the door the harness has had to
    close repeatedly — an agent that reads an absence as "nothing to report"
    improvises around it.
    """

    if entry.status == STATUS_TRUNCATED:
        return (
            f"- [harness] The '{entry.name}' runtime note did not fit this turn's "
            f"volatile-tail budget and was TRUNCATED "
            f"({entry.emitted_bytes} of {entry.original_bytes} bytes shown; budget "
            f"{entry.budget_bytes}). What you see above is incomplete — treat the "
            "missing part as UNKNOWN, not as absent, and say so if it matters."
        )
    return (
        f"- [harness] The '{entry.name}' runtime note ({entry.original_bytes} bytes) did "
        f"not fit this turn's volatile-tail budget ({entry.budget_bytes}) and was "
        "DROPPED. It was NOT empty: treat this fact as UNKNOWN for this turn rather "
        "than as 'nothing to report', and ask the operator if it matters."
    )


def compose_volatile_tail(
    contributions: Iterable[VolatileContribution],
) -> VolatileTail:
    """Render the registered contributions into one tail, with full accounting.

    Order is registration order — the roster is the contract, so the agent sees
    the same facts in the same order every turn.

    A contributor whose content fits is emitted byte-identically to what it
    rendered: this composition changes nothing for a standard turn, which is the
    whole point of choosing budgets far above the real maxima.
    """

    entries: list[VolatileTailEntry] = []
    parts: list[str] = []
    seen: set[str] = set()

    for contribution in contributions or ():
        name = str(contribution.name or "").strip()
        if not name:
            raise ValueError("a volatile-tail contribution must be named")
        if name in seen:
            # A duplicate name would shadow the first contributor's accounting —
            # exactly the silent-loss class this module exists to retire. The
            # roster is fixed and small, so this can only be a programming error.
            raise ValueError(f"duplicate volatile-tail contributor: {name}")
        seen.add(name)

        budget = int(contribution.budget_bytes)
        if budget <= 0:
            raise ValueError(
                f"volatile-tail contributor '{name}' must declare a positive byte budget"
            )

        content = str(contribution.content or "").strip()
        original = _byte_len(content)
        if not content:
            entries.append(
                VolatileTailEntry(
                    name=name,
                    status=STATUS_EMPTY,
                    budget_bytes=budget,
                    original_bytes=0,
                    emitted_bytes=0,
                )
            )
            continue

        if original <= budget:
            entries.append(
                VolatileTailEntry(
                    name=name,
                    status=STATUS_EMITTED,
                    budget_bytes=budget,
                    original_bytes=original,
                    emitted_bytes=original,
                )
            )
            parts.append(content)
            continue

        body = _truncate_bytes(content, budget).rstrip()
        kept = _byte_len(body)
        status = STATUS_TRUNCATED if kept >= TRUNCATION_MIN_BODY_BYTES else STATUS_DROPPED
        if status == STATUS_DROPPED:
            body = ""
            kept = 0
        entry = VolatileTailEntry(
            name=name,
            status=status,
            budget_bytes=budget,
            original_bytes=original,
            emitted_bytes=kept,
        )
        entries.append(entry)
        note = _shortfall_note(entry)
        parts.append(f"{body}\n{note}" if body else note)
        # Operator-side half of the same accounting: the in-band note tells the
        # agent, this tells whoever is reading harness logs. Never silent on
        # either side.
        logger.warning(
            "volatile tail contributor %s %s (%s/%s bytes, budget %s)",
            name,
            status,
            kept,
            original,
            budget,
        )

    return VolatileTail(content="\n".join(parts), entries=tuple(entries))


class VolatileTailBuilder:
    """Registration surface for the tail: ``add(...)`` per contributor, then ``build()``.

    Deliberately a builder rather than a bare list comprehension at the call
    site: registering forces every contributor to state its NAME and its BUDGET,
    which is what makes the roster readable and the accounting attributable.
    """

    __slots__ = ("_contributions",)

    def __init__(self) -> None:
        self._contributions: list[VolatileContribution] = []

    def add(self, name: str, content: Any, *, budget_bytes: int) -> "VolatileTailBuilder":
        self._contributions.append(
            VolatileContribution(
                name=str(name),
                content="" if content is None else str(content),
                budget_bytes=int(budget_bytes),
            )
        )
        return self

    @property
    def contributions(self) -> Sequence[VolatileContribution]:
        return tuple(self._contributions)

    def build(self) -> VolatileTail:
        return compose_volatile_tail(self._contributions)


__all__ = [
    "STATUS_DROPPED",
    "STATUS_EMITTED",
    "STATUS_EMPTY",
    "STATUS_TRUNCATED",
    "TRUNCATION_MIN_BODY_BYTES",
    "VolatileContribution",
    "VolatileTail",
    "VolatileTailBuilder",
    "VolatileTailEntry",
    "compose_volatile_tail",
]
